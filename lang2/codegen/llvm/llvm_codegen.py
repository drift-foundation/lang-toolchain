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
from typing import Dict, List, Mapping

from lang2.checker import FnInfo
from lang2.stage1 import BinaryOp
from lang2.stage2 import (
	BinaryOpInstr,
	AssignSSA,
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
from lang2.stage4.ssa import CfgKind

# ABI type names
DRIFT_ERROR_TYPE = "%DriftError"
FNRESULT_INT_ERROR = "%FnResult_Int_Error"


# Public API -------------------------------------------------------------------

def lower_ssa_func_to_llvm(
	func: MirFunc,
	ssa: SsaFunc,
	fn_info: FnInfo,
	fn_infos: Mapping[str, FnInfo] | None = None,
) -> str:
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
	all_infos = dict(fn_infos) if fn_infos is not None else {fn_info.name: fn_info}
	builder = _FuncBuilder(func=func, ssa=ssa, fn_info=fn_info, fn_infos=all_infos)
	return builder.lower()


def lower_module_to_llvm(
	funcs: Mapping[str, MirFunc],
	ssa_funcs: Mapping[str, SsaFunc],
	fn_infos: Mapping[str, FnInfo],
) -> LlvmModuleBuilder:
	"""
	Lower a set of SSA functions to an LLVM module.

	Args:
	  funcs: name -> MIR function
	  ssa_funcs: name -> SSA wrapper (must align with funcs)
	  fn_infos: name -> FnInfo for each function
	"""
	mod = LlvmModuleBuilder()
	for name, func in funcs.items():
		ssa = ssa_funcs[name]
		fn_info = fn_infos[name]
		mod.emit_func(lower_ssa_func_to_llvm(func, ssa, fn_info, fn_infos))
	return mod


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

	def emit_entry_wrapper(self, drift_main: str = "drift_main") -> None:
		"""
		Emit a tiny OS entrypoint wrapper that calls `@drift_main` and truncs to i32.

		This keeps the Drift ABI (i64 Int, FnResult later) distinct from the
		process ABI. Err-mapping is not yet implemented; the wrapper simply
		truncates the i64 return to i32 for exit.
		"""
		self.funcs.append(
			"\n".join(
				[
					f"declare i64 @{drift_main}()",
					"",
					"define i32 @main() {",
					"entry:",
					f"  %ret = call i64 @{drift_main}()",
					"  %trunc = trunc i64 %ret to i32",
					"  ret i32 %trunc",
					"}",
				]
			)
		)

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
	fn_infos: Mapping[str, FnInfo]
	tmp_counter: int = 0
	lines: List[str] = field(default_factory=list)
	value_map: Dict[str, str] = field(default_factory=dict)
	value_types: Dict[str, str] = field(default_factory=dict)
	aliases: Dict[str, str] = field(default_factory=dict)

	def lower(self) -> str:
		self._assert_cfg_supported()
		self._emit_header()
		order = self.ssa.block_order or list(self.func.blocks.keys())
		for block_name in order:
			self._emit_block(block_name)
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
		incoming_types: set[str] = set()
		for pred, val in phi.incoming.items():
			incomings.append(f"[ {self._map_value(val)}, %{pred} ]")
			ty = self._type_of(val)
			if ty is not None:
				incoming_types.add(ty)
		joined = ", ".join(incomings)
		if not incoming_types:
			phi_ty = self._llvm_scalar_type()
		elif len(incoming_types) == 1:
			phi_ty = next(iter(incoming_types))
		else:
			raise NotImplementedError(
				f"LLVM codegen v1: phi with mixed incoming types {incoming_types}"
			)
		self.value_types[dest] = phi_ty
		self.lines.append(f"  {dest} = phi {phi_ty} {joined}")

	def _lower_instr(self, instr: object) -> None:
		if isinstance(instr, ConstInt):
			dest = self._map_value(instr.dest)
			self.value_types[dest] = "i64"
			self.lines.append(f"  {dest} = add i64 0, {instr.value}")
		elif isinstance(instr, ConstBool):
			dest = self._map_value(instr.dest)
			val = 1 if instr.value else 0
			self.value_types[dest] = "i1"
			self.lines.append(f"  {dest} = add i1 0, {val}")
		elif isinstance(instr, AssignSSA):
			# Alias dest to src; no IR emission needed beyond name/type propagation.
			src = self._map_value(instr.src)
			dest = self._map_value(instr.dest)
			self.aliases[instr.dest] = instr.src
			if src in self.value_types:
				self.value_types[dest] = self.value_types[src]
		elif isinstance(instr, BinaryOpInstr):
			dest = self._map_value(instr.dest)
			left = self._map_value(instr.left)
			right = self._map_value(instr.right)
			op = self._map_binop(instr.op)
			self.value_types[dest] = "i64"
			self.lines.append(f"  {dest} = {op} i64 {left}, {right}")
		elif isinstance(instr, Call):
			self._lower_call(instr)
		elif isinstance(instr, ConstructResultOk):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			self.value_types[dest] = FNRESULT_INT_ERROR
			tmp0 = self._fresh("ok0")
			tmp1 = self._fresh("ok1")
			err_zero = f"{DRIFT_ERROR_TYPE} zeroinitializer"
			self.lines.append(f"  {tmp0} = insertvalue {FNRESULT_INT_ERROR} undef i1 0, 0")
			self.lines.append(f"  {tmp1} = insertvalue {FNRESULT_INT_ERROR} {tmp0} i64 {val}, 1")
			self.lines.append(f"  {dest} = insertvalue {FNRESULT_INT_ERROR} {tmp1} {err_zero}, 2")
		elif isinstance(instr, ConstructResultErr):
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			self.value_types[dest] = FNRESULT_INT_ERROR
			tmp0 = self._fresh("err0")
			tmp1 = self._fresh("err1")
			self.lines.append(f"  {tmp0} = insertvalue {FNRESULT_INT_ERROR} undef i1 1, 0")
			self.lines.append(f"  {tmp1} = insertvalue {FNRESULT_INT_ERROR} {tmp0} i64 0, 1")
			self.lines.append(
				f"  {dest} = insertvalue {FNRESULT_INT_ERROR} {tmp1}, {DRIFT_ERROR_TYPE} {err_val}, 2"
			)
		elif isinstance(instr, ConstructError):
			dest = self._map_value(instr.dest)
			code = self._map_value(instr.code)
			self.value_types[dest] = DRIFT_ERROR_TYPE
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
		callee_info = self.fn_infos.get(instr.fn)
		if callee_info is None:
			raise NotImplementedError(f"LLVM codegen v1: missing FnInfo for callee {instr.fn}")
		if callee_info.declared_can_throw:
			tmp = self._fresh("call")
			self.lines.append(f"  {tmp} = call {FNRESULT_INT_ERROR} @{instr.fn}({args})")
			if dest:
				self.lines.append(f"  {dest} = extractvalue {FNRESULT_INT_ERROR} {tmp}, 1")
				self.value_types[dest] = "i64"
		else:
			if dest is None:
				self.lines.append(f"  call void @{instr.fn}({args})")
			else:
				self.lines.append(f"  {dest} = call i64 @{instr.fn}({args})")
				self.value_types[dest] = "i64"

	def _lower_term(self, term: object) -> None:
		if isinstance(term, Goto):
			self.lines.append(f"  br label %{term.target}")
		elif isinstance(term, IfTerminator):
			cond = self._map_value(term.cond)
			cond_ty = self.value_types.get(cond, "i1")
			if cond_ty != "i1":
				raise NotImplementedError("LLVM codegen v1: branch condition must be bool (i1)")
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
				# Enforce scalar return shape for non-can-throw.
				if self.value_types.get(val) != "i64":
					raise NotImplementedError("LLVM codegen v1: non-can-throw return must be Int")
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
		# Resolve aliases (AssignSSA) before mapping to an LLVM name.
		root = mir_id
		seen: set[str] = set()
		while root in self.aliases and root not in seen:
			seen.add(root)
			root = self.aliases[root]
		if root not in self.value_map:
			self.value_map[root] = f"%{root}"
		if mir_id not in self.value_map:
			self.value_map[mir_id] = self.value_map[root]
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
		pass

	def _assert_cfg_supported(self) -> None:
		cfg_kind = self.ssa.cfg_kind or CfgKind.STRAIGHT_LINE
		if cfg_kind is CfgKind.GENERAL:
			raise NotImplementedError("LLVM codegen v1: loops/backedges are not supported yet")

	def _type_of(self, value_id: str) -> str | None:
		"""Best-effort lookup of an LLVM type string for a value id."""
		name = self._map_value(value_id)
		return self.value_types.get(name)
