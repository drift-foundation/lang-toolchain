# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Minimal MIR → LLVM IR skeleton for lang2 (ABI v1).

Scope (v1 bring-up):
  - Scalars: Int (i64), Bool (i1 in regs, i8 in aggregates if/when needed)
  - Error ABI: `%DriftError = { i64, ptr, ptr, ptr }` (code + opaque payloads)
  - Internal FnResult<Int, Error>: `%FnResult_Int_Error = { i1, i64, %DriftError }`
  - Supported MIR ops: ConstInt, ConstBool, BinaryOpInstr (add/sub/mul/div),
    Call, Return, ConstructResultOk, ConstructResultErr.

This is a text-IR emitter (no llvmlite dependency). It is deliberately small so
that end-to-end tests can assert on emitted IR or feed it to `clang`/`lli`.

NOTES / TODOs:
  - Parameter typing is restricted to zero-arg functions for now; parameter
    type inference will be added alongside richer signature handling.
  - Control flow beyond straight-line / simple branches is not yet lowered.
  - Error payload construction is stubbed as zero/null pointers; this matches
    the ABI layout but not full runtime semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from lang2.stage2 import (
	BinaryOpInstr,
	Call,
	ConstBool,
	ConstInt,
	ConstructError,
	ConstructResultErr,
	ConstructResultOk,
	MirFunc,
	Return,
)
from lang2.stage1 import BinaryOp


# --- ABI types --------------------------------------------------------------

DRIFT_ERROR_TYPE = "%DriftError"
FNRESULT_INT_ERROR = "%FnResult_Int_Error"


@dataclass
class LlvmModuleBuilder:
	"""
	Textual LLVM IR builder for a module.

	Tracks type declarations and function definitions. This is intentionally
	minimal; richer features (global constants, data layout, target triple)
	will be added as needed.
	"""

	type_decls: List[str] = field(default_factory=list)
	funcs: List[str] = field(default_factory=list)

	def __post_init__(self) -> None:
		# Seed ABI type declarations up front.
		self.type_decls.extend(
			[
				f"{DRIFT_ERROR_TYPE} = type {{ i64, ptr, ptr, ptr }}",
				f"{FNRESULT_INT_ERROR} = type {{ i1, i64, {DRIFT_ERROR_TYPE} }}",
			]
		)

	def emit_func(self, text: str) -> None:
		"""Append a complete function definition."""
		self.funcs.append(text)

	def render(self) -> str:
		"""Render the full LLVM module as a single string."""
		lines: List[str] = []
		lines.extend(self.type_decls)
		lines.append("")  # spacer
		lines.extend(self.funcs)
		lines.append("")
		return "\n".join(lines)


def lower_mir_func_to_llvm(func: MirFunc, can_throw: bool) -> str:
	"""
	Lowers a single MIR function to LLVM IR text.

	Args:
	  func: MIR function to lower (single basic block expected for now).
	  can_throw: whether the function returns FnResult<Int, Error> (True) or
	    a plain Int (False). Later this will come from signatures/TypeEnv.

	Returns:
	  LLVM IR string for the function (function definition only).

	Limitations:
	  - Only supports zero parameters and a single basic block.
	  - Binary operations are mapped assuming Int operands (i64).
	"""
	builder = _FuncBuilder(func=func, can_throw=can_throw)
	return builder.lower()


# --- Internal helpers -------------------------------------------------------


@dataclass
class _FuncBuilder:
	func: MirFunc
	can_throw: bool
	tmp_counter: int = 0
	lines: List[str] = field(default_factory=list)
	value_map: Dict[str, str] = field(default_factory=dict)

	def lower(self) -> str:
		"""Lower the MIR function into textual LLVM IR."""
		blocks = list(self.func.blocks.values())
		if len(blocks) != 1:
			raise NotImplementedError("LLVM codegen: only single-block funcs supported in v1 bring-up")
		entry = blocks[0]
		header = self._func_header()
		self.lines.append(f"{header} {{")
		for instr in entry.instructions:
			self._lower_instr(instr)
		self._lower_term(entry.terminator)
		self.lines.append("}")
		return "\n".join(self.lines)

	def _func_header(self) -> str:
		"""Emit the function header based on can_throw/return type."""
		if self.can_throw:
			ret_ty = FNRESULT_INT_ERROR
		else:
			ret_ty = "i64"
		# Parameter support is minimal; assume no params for now.
		return f"define {ret_ty} @{self.func.name}()"

	def _fresh(self, hint: str = "tmp") -> str:
		"""Generate a fresh SSA name."""
		self.tmp_counter += 1
		return f"%{hint}{self.tmp_counter}"

	def _map_value(self, mir_id: str) -> str:
		"""Map a MIR ValueId to an LLVM SSA name, allocating if unseen."""
		if mir_id not in self.value_map:
			self.value_map[mir_id] = f"%{mir_id}"
		return self.value_map[mir_id]

	def _lower_instr(self, instr: object) -> None:
		"""Lower a MIR instruction into LLVM IR lines."""
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
			dest = self._map_value(instr.dest) if instr.dest else None
			args = ", ".join([f"i64 {self._map_value(a)}" for a in instr.args])
			if self.can_throw:
				# Callee assumed to return FnResult<Int, Error> for now.
				tmp = self._fresh("call")
				self.lines.append(f"  {tmp} = call {FNRESULT_INT_ERROR} @{instr.fn}({args})")
				if dest:
					self.lines.append(f"  {dest} = extractvalue {FNRESULT_INT_ERROR} {tmp}, 1")
			else:
				if dest is None:
					self.lines.append(f"  call void @{instr.fn}({args})")
				else:
					self.lines.append(f"  {dest} = call i64 @{instr.fn}({args})")
		elif isinstance(instr, ConstructResultOk):
			# Build FnResult { is_err=0, ok=value, err=zeroinit }
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
			# Attrs/ctx/stack are null for now.
			tmp0 = self._fresh("errc0")
			tmp1 = self._fresh("errc1")
			tmp2 = self._fresh("errc2")
			self.lines.append(f"  {tmp0} = insertvalue {DRIFT_ERROR_TYPE} undef i64 {code}, 0")
			self.lines.append(f"  {tmp1} = insertvalue {DRIFT_ERROR_TYPE} {tmp0} ptr null, 1")
			self.lines.append(f"  {tmp2} = insertvalue {DRIFT_ERROR_TYPE} {tmp1} ptr null, 2")
			self.lines.append(f"  {dest} = insertvalue {DRIFT_ERROR_TYPE} {tmp2} ptr null, 3")
		else:
			raise NotImplementedError(f"LLVM codegen: unsupported instr {type(instr).__name__}")

	def _lower_term(self, term: Optional[object]) -> None:
		"""Lower the terminator."""
		if not isinstance(term, Return):
			raise NotImplementedError("LLVM codegen: only Return terminators supported in v1 bring-up")
		if term.value is None:
			self.lines.append("  ret void")
			return
		val = self._map_value(term.value)
		if self.can_throw:
			# When can_throw is true, the function return type is FnResult<Int, Error>.
			# The MIR should already carry a ConstructResult* value as the return.
			self.lines.append(f"  ret {FNRESULT_INT_ERROR} {val}")
		else:
			self.lines.append(f"  ret i64 {val}")

	def _map_binop(self, op: BinaryOp) -> str:
		"""Map MIR BinaryOp to LLVM opcode (integers only for now)."""
		if op == BinaryOp.ADD:
			return "add"
		if op == BinaryOp.SUB:
			return "sub"
		if op == BinaryOp.MUL:
			return "mul"
		if op == BinaryOp.DIV:
			return "sdiv"
		raise NotImplementedError(f"LLVM codegen: unsupported binary op {op}")
