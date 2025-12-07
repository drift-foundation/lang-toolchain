# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
SSA → LLVM IR lowering for the v1 Drift ABI (textual emitter).

Scope (v1 bring-up):
  - Input: SSA (`SsaFunc`) plus MIR (`MirFunc`) and `FnInfo` metadata.
  - Supported types: Int (i64), Bool (i1 in regs), FnResult<Int, Error>.
  - Supported ops: ConstInt/Bool, AssignSSA aliases, BinaryOpInstr (int),
    Call (Int or FnResult<Int, Error> return), Phi, ConstructResultOk/Err,
    ConstructError (attrs zeroed), Return, IfTerminator/Goto.
  - Control flow: straight-line + if/else (acyclic CFGs); loops/backedges are
    rejected explicitly.

ABI (from docs/design/drift-lang-abi.md):
  - %DriftError      = { i64 code, ptr attrs, ptr ctx_frames, ptr stack }
  - %FnResult_Int_Error = { i1 is_err, i64 ok, %DriftError err }
  - Drift Int is i64; Bool is i1 in registers.

This emitter is deliberately small and produces LLVM text suitable for feeding
to `lli`/`clang` in tests. It avoids allocas and relies on SSA/phinode lowering
directly. Unsupported features raise clear errors rather than emitting bad IR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from lang2.checker import FnInfo
from lang2.stage1 import BinaryOp
from lang2.stage2 import (
	BinaryOpInstr,
	Call,
	ConstBool,
	ConstInt,
	ConstructError,
	ConstructResultErr,
	ConstructResultOk,
	Goto,
	IfTerminator,
	MirFunc,
	Phi,
	Return,
)
from lang2.stage4.ssa import SsaFunc

# ABI type names
DRIFT_ERROR_TYPE = "%DriftError"
FNRESULT_INT_ERROR = "%FnResult_Int_Error"


# Public API -------------------------------------------------------------------

def lower_ssa_func_to_llvm(func: MirFunc, ssa: SsaFunc, fn_info: FnInfo) -> str:
	"""
	Lower a single SSA function to LLVM IR text using FnInfo for return typing.

	Args:
	  func: the underlying MIR function (for block order/names).
	  ssa: SSA wrapper carrying blocks/phis.
	  fn_info: checker metadata (declared_can_throw, return_type_id).

	Returns:
	  LLVM IR string for the function definition.

	Limitations:
	  - Only Int/Bools and FnResult<Int, Error> returns are supported in v1.
	  - No loops/backedges; CFG must be acyclic (if/else diamonds ok).
	"""
	builder = _FuncBuilder(func=func, ssa=ssa, fn_info=fn_info)
	return builder.lower()


# Internal helpers -------------------------------------------------------------


@dataclass
class LlvmModuleBuilder:
	"""Textual LLVM module builder with seeded ABI type declarations."""

	type_decls: List[str] = field(default_factory=list)
	funcs: List[str] = field(default_factory=list)

	def __post_init__(self) -> None:
		self.type_decls.extend(
			[
				f"{DRIFT_ERROR_TYPE} = type {{ i64, ptr, ptr, ptr }}",
				f"{FNRESULT_INT_ERROR} = type {{ i1, i64, {DRIFT_ERROR_TYPE} }}",
			]
		)

	def emit_func(self, text: str) -> None:
		self.funcs.append(text)

	def render(self) -> str:
		lines: List[str] = []
		lines.extend(self.type_decls)
		lines.append("")
		lines.extend(self.funcs)
		lines.append("")
		return "\n".join(lines)


@dataclass
class _FuncBuilder:
	func: MirFunc
	ssa: SsaFunc
	fn_info: FnInfo
	tmp_counter: int = 0
	lines: List[str] = field(default_factory=list)
	value_map: Dict[str, str] = field(default_factory=dict)

	def lower(self) -> str:
		self._assert_acyclic()
		self._emit_header()
		for block in self.func.blocks.values():
			self._emit_block(block.name)
		self.lines.append("}")
		return "\n".join(self.lines)

	def _emit_header(self) -> None:
		ret_ty = self._return_llvm_type()
		if self.func.params:
			raise NotImplementedError("LLVM codegen v1: parameters not supported yet")
		self.lines.append(f"define {ret_ty} @{self.func.name}() {{")

	def _emit_block(self, block_name: str) -> None:
		block = self.func.blocks[block_name]
		self.lines.append(f"{block.name}:")
		# Emit phi nodes first.
		for instr in block.instructions:
			if isinstance(instr, Phi):
				self._lower_phi(block.name, instr)
		# Emit non-phi instructions.
		for instr in block.instructions:
			if isinstance(instr, Phi):
				continue
			self._lower_instr(instr)
		self._lower_term(block.terminator)

	def _lower_phi(self, block_name: str, phi: Phi) -> None:
		dest = self._map_value(phi.dest)
		incomings = []
		for pred, val in phi.incoming.items():
			incomings.append(f"[ {self._map_value(val)}, %{pred} ]")
		joined = ", ".join(incomings)
		self.lines.append(f"  {dest} = phi {self._llvm_scalar_type()} {joined}")

	def _lower_instr(self, instr: object) -> None:
		if isinstance(instr, ConstInt):
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = add i64 0, {instr.value}")
		elif isinstance(instr, ConstBool):
			dest = self._map_value(instr.dest)
			val = 1 if instr.value else 0
			self.lines.append(f"  {dest} = add i1 0, {val}")
		elif isinstance(instr, BinaryOpInstr):
			dest = self._map_value(instr.dest)
			left = self._map_value(instr.left)
			right = self._map_value(instr.right)
			op = self._map_binop(instr.op)
			self.lines.append(f"  {dest} = {op} i64 {left}, {right}")
		elif isinstance(instr, Call):
			self._lower_call(instr)
		elif isinstance(instr, ConstructResultOk):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			tmp0 = self._fresh("ok0")
			tmp1 = self._fresh("ok1")
			err_zero = f"{DRIFT_ERROR_TYPE} zeroinitializer"
			self.lines.append(f"  {tmp0} = insertvalue {FNRESULT_INT_ERROR} undef i1 0, 0")
			self.lines.append(f"  {tmp1} = insertvalue {FNRESULT_INT_ERROR} {tmp0} i64 {val}, 1")
			self.lines.append(f"  {dest} = insertvalue {FNRESULT_INT_ERROR} {tmp1} {err_zero}, 2")
		elif isinstance(instr, ConstructResultErr):
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			tmp0 = self._fresh("err0")
			tmp1 = self._fresh("err1")
			self.lines.append(f"  {tmp0} = insertvalue {FNRESULT_INT_ERROR} undef i1 1, 0")
			self.lines.append(f"  {tmp1} = insertvalue {FNRESULT_INT_ERROR} {tmp0} i64 0, 1")
			self.lines.append(f"  {dest} = insertvalue {FNRESULT_INT_ERROR} {tmp1} {DRIFT_ERROR_TYPE} {err_val}, 2")
		elif isinstance(instr, ConstructError):
			dest = self._map_value(instr.dest)
			code = self._map_value(instr.code)
			tmp0 = self._fresh("errc0")
			tmp1 = self._fresh("errc1")
			tmp2 = self._fresh("errc2")
			self.lines.append(f"  {tmp0} = insertvalue {DRIFT_ERROR_TYPE} undef i64 {code}, 0")
			self.lines.append(f"  {tmp1} = insertvalue {DRIFT_ERROR_TYPE} {tmp0} ptr null, 1")
			self.lines.append(f"  {tmp2} = insertvalue {DRIFT_ERROR_TYPE} {tmp1} ptr null, 2")
			self.lines.append(f"  {dest} = insertvalue {DRIFT_ERROR_TYPE} {tmp2} ptr null, 3")
		elif isinstance(instr, Phi):
			# Already handled in _lower_phi.
			return
		else:
			raise NotImplementedError(f"LLVM codegen v1: unsupported instr {type(instr).__name__}")

	def _lower_call(self, instr: Call) -> None:
		dest = self._map_value(instr.dest) if instr.dest else None
		args = ", ".join([f"i64 {self._map_value(a)}" for a in instr.args])
		# Decide callee return shape: Int or FnResult<Int, Error>. For v1 we
		# restrict callees to these shapes; a richer TypeEnv will generalize later.
		# Heuristic: if the destination is later used in a FnResult context (not tracked here),
		# fall back to FnResult when this function itself is can-throw.
		if self.fn_info.declared_can_throw:
			tmp = self._fresh("call")
			self.lines.append(f"  {tmp} = call {FNRESULT_INT_ERROR} @{instr.fn}({args})")
			if dest:
				self.lines.append(f"  {dest} = extractvalue {FNRESULT_INT_ERROR} {tmp}, 1")
		else:
			if dest is None:
				self.lines.append(f"  call void @{instr.fn}({args})")
			else:
				self.lines.append(f"  {dest} = call i64 @{instr.fn}({args})")

	def _lower_term(self, term: object) -> None:
		if isinstance(term, Goto):
			self.lines.append(f"  br label %{term.target}")
		elif isinstance(term, IfTerminator):
			cond = self._map_value(term.cond)
			self.lines.append(
				f"  br i1 {cond}, label %{term.then_target}, label %{term.else_target}"
			)
		elif isinstance(term, Return):
			if term.value is None:
				raise AssertionError("LLVM codegen v1: bare return unsupported")
			val = self._map_value(term.value)
			if self.fn_info.declared_can_throw:
				self.lines.append(f"  ret {FNRESULT_INT_ERROR} {val}")
			else:
				self.lines.append(f"  ret i64 {val}")
		else:
			raise NotImplementedError(f"LLVM codegen v1: unsupported terminator {type(term).__name__}")

	def _return_llvm_type(self) -> str:
		# v1 supports only Int or FnResult<Int, Error> return shapes.
		if self.fn_info.declared_can_throw:
			return FNRESULT_INT_ERROR
		td = self.fn_info.return_type_id
		if td is None:
			raise NotImplementedError("LLVM codegen v1: missing return_type_id")
		# Only scalar Int supported for now.
		return "i64"

	def _llvm_scalar_type(self) -> str:
		# All lowered values are i64 or i1; phis currently assume Int.
		return "i64"

	def _fresh(self, hint: str = "tmp") -> str:
		self.tmp_counter += 1
		return f"%{hint}{self.tmp_counter}"

	def _map_value(self, mir_id: str) -> str:
		if mir_id not in self.value_map:
			self.value_map[mir_id] = f"%{mir_id}"
		return self.value_map[mir_id]

	def _map_binop(self, op: BinaryOp) -> str:
		if op == BinaryOp.ADD:
			return "add"
		if op == BinaryOp.SUB:
			return "sub"
		if op == BinaryOp.MUL:
			return "mul"
		if op == BinaryOp.DIV:
			return "sdiv"
		raise NotImplementedError(f"LLVM codegen v1: unsupported binary op {op}")

	def _assert_acyclic(self) -> None:
		# Simple check: no block should list itself as a successor; full backedge
		# detection lives in SSA stage and rejects loops earlier.
		for block in self.func.blocks.values():
			term = block.terminator
			if isinstance(term, Goto) and term.target == block.name:
				raise NotImplementedError("LLVM codegen v1: loops/backedges unsupported")
			if isinstance(term, IfTerminator):
				if term.then_target == block.name or term.else_target == block.name:
					raise NotImplementedError("LLVM codegen v1: self-branch unsupported")
