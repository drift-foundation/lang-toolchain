# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
SSA → LLVM IR lowering for the v1 Drift ABI (textual emitter).

Scope (v1 bring-up):
  - Input: SSA (`SsaFunc`) plus MIR (`MirFunc`) and `FnInfo` metadata.
  - Supported types: Int (i64), Bool (i1 in regs), String ({%drift.size, i8*}),
    FnResult<Int, Error>.
  - Supported ops: ConstInt/Bool/String, AssignSSA aliases, BinaryOpInstr (int),
    Call (Int/String or FnResult<Int, Error> return), Phi, ConstructResultOk/Err,
    ConstructError (attrs zeroed), Return, IfTerminator/Goto, Array ops.
  - Control flow: straight-line + if/else (acyclic CFGs); loops/backedges are
    rejected explicitly.

ABI (from docs/design/drift-lang-abi.md):
  - %DriftError           = { i64 code, ptr attrs, ptr ctx_frames, ptr stack }
  - %FnResult_Int_Error   = { i1 is_err, i64 ok, %DriftError err }
  - %DriftString          = { %drift.size, i8* }
  - %drift.size           = i64 (Uint carrier)
  - Drift Int is i64; Bool is i1 in registers.

This emitter is deliberately small and produces LLVM text suitable for feeding
to `lli`/`clang` in tests. It avoids allocas and relies on SSA/phinode lowering
directly. Unsupported features raise clear errors rather than emitting bad IR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

from lang2.checker import FnInfo
from lang2.stage1 import BinaryOp
from lang2.stage2 import (
	ArrayIndexLoad,
	ArrayIndexStore,
	ArrayLit,
	ArrayLen,
	ArrayCap,
	BinaryOpInstr,
	AssignSSA,
	Call,
	ConstBool,
	ConstInt,
	ConstString,
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
from lang2.core.types_core import TypeKind, TypeTable, TypeId

"""LLVM codegen for lang2 SSA.

This backend is intentionally narrow for v1: Int/Bool/Array and
FnResult<Int, Error>. As we add String support, we treat it as a two-field
struct { %drift.size, i8* } and lower literals to that shape without runtime
calls.
"""

# ABI type names
DRIFT_ERROR_TYPE = "%DriftError"
FNRESULT_INT_ERROR = "%FnResult_Int_Error"
DRIFT_SIZE_TYPE = "%drift.size"
DRIFT_STRING_TYPE = "%DriftString"


# Public API -------------------------------------------------------------------

def lower_ssa_func_to_llvm(
	func: MirFunc,
	ssa: SsaFunc,
	fn_info: FnInfo,
	fn_infos: Mapping[str, FnInfo] | None = None,
	type_table: Optional[TypeTable] = None,
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
	  - Returns: Int, String, or FnResult<Int, Error> in v1.
	  - No loops/backedges; CFG must be acyclic (if/else diamonds ok).
	"""
	all_infos = dict(fn_infos) if fn_infos is not None else {fn_info.name: fn_info}
	mod = LlvmModuleBuilder()
	builder = _FuncBuilder(func=func, ssa=ssa, fn_info=fn_info, fn_infos=all_infos, module=mod, type_table=type_table)
	mod.emit_func(builder.lower())
	return mod.render()


def lower_module_to_llvm(
	funcs: Mapping[str, MirFunc],
	ssa_funcs: Mapping[str, SsaFunc],
	fn_infos: Mapping[str, FnInfo],
	type_table: Optional[TypeTable] = None,
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
		builder = _FuncBuilder(func=func, ssa=ssa, fn_info=fn_info, fn_infos=fn_infos, module=mod, type_table=type_table)
		mod.emit_func(builder.lower())
	return mod


# Internal helpers -------------------------------------------------------------


def _escape_byte(b: int) -> str:
	"""
	Encode a single byte for an LLVM c\"...\" string literal.

	Printable, non-special ASCII stays as-is; quote and backslash are escaped;
	all other bytes are emitted as \\XX hex escapes.
	"""
	if 32 <= b <= 126 and b not in (34, 92):  # printable ASCII excluding \" and \\
		return chr(b)
	if b == 34:  # double quote
		return "\\22"
	if b == 92:  # backslash
		return "\\5C"
	return f"\\{b:02X}"


@dataclass
class LlvmModuleBuilder:
	"""Textual LLVM module builder with seeded ABI type declarations."""

	type_decls: List[str] = field(default_factory=list)
	consts: List[str] = field(default_factory=list)
	funcs: List[str] = field(default_factory=list)
	needs_array_helpers: bool = False

	def __post_init__(self) -> None:
		self.type_decls.extend(
			[
				f"{DRIFT_SIZE_TYPE} = type i64",
				f"{DRIFT_ERROR_TYPE} = type {{ i64, ptr, ptr, ptr }}",
				f"{FNRESULT_INT_ERROR} = type {{ i1, i64, {DRIFT_ERROR_TYPE} }}",
				f"{DRIFT_STRING_TYPE} = type {{ {DRIFT_SIZE_TYPE}, i8* }}",
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
		if self.consts:
			lines.extend(self.consts)
			lines.append("")
		if self.needs_array_helpers:
			lines.extend(
				[
					"declare ptr @drift_alloc_array(i64, i64, %drift.size, %drift.size)",
					"declare void @drift_bounds_check_fail(%drift.size, %drift.size)",
					"",
				]
			)
		lines.extend(self.funcs)
		lines.append("")
		return "\n".join(lines)


@dataclass
class _FuncBuilder:
	func: MirFunc
	ssa: SsaFunc
	fn_info: FnInfo
	fn_infos: Mapping[str, FnInfo]
	module: LlvmModuleBuilder
	type_table: Optional[TypeTable] = None
	tmp_counter: int = 0
	lines: List[str] = field(default_factory=list)
	value_map: Dict[str, str] = field(default_factory=dict)
	value_types: Dict[str, str] = field(default_factory=dict)
	aliases: Dict[str, str] = field(default_factory=dict)
	string_type_id: Optional[TypeId] = None
	int_type_id: Optional[TypeId] = None

	def lower(self) -> str:
		self._assert_cfg_supported()
		self._prime_type_ids()
		self._emit_header()
		self._declare_array_helpers_if_needed()
		order = self.ssa.block_order or list(self.func.blocks.keys())
		for block_name in order:
			self._emit_block(block_name)
		self.lines.append("}")
		return "\n".join(self.lines)

	def _prime_type_ids(self) -> None:
		if self.type_table is None:
			return
		for ty_id, ty_def in getattr(self.type_table, "_defs", {}).items():  # type: ignore[attr-defined]
			if ty_def.kind is TypeKind.SCALAR and ty_def.name == "String":
				self.string_type_id = ty_id
			if ty_def.kind is TypeKind.SCALAR and ty_def.name == "Int":
				self.int_type_id = ty_id

	def _emit_header(self) -> None:
		ret_ty = self._return_llvm_type()
		params = self.func.params
		sig = self.fn_info.signature
		if params and (sig is None or sig.param_type_ids is None or len(sig.param_type_ids) != len(params)):
			raise NotImplementedError(
				f"LLVM codegen v1: param count/signature mismatch for {self.func.name}: "
				f"MIR has {len(params)}, signature has "
				f"{0 if sig is None or sig.param_type_ids is None else len(sig.param_type_ids)}"
			)
		param_parts: list[str] = []
		if params and sig and sig.param_type_ids is not None:
			for name, ty_id in zip(params, sig.param_type_ids):
				llty = self._llvm_type_for_typeid(ty_id)
				llvm_name = self._map_value(name)
				self.value_types[llvm_name] = llty
				param_parts.append(f"{llty} {llvm_name}")
		params_str = ", ".join(param_parts)
		self.lines.append(f"define {ret_ty} @{self.func.name}({params_str}) {{")

	def _declare_array_helpers_if_needed(self) -> None:
		"""Mark the module to emit array helper decls if any array ops are present."""
		has_array = any(
			isinstance(instr, (ArrayLit, ArrayIndexLoad, ArrayIndexStore))
			for block in self.func.blocks.values()
			for instr in block.instructions
		)
		if not has_array:
			return
		self.module.needs_array_helpers = True

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
		elif isinstance(instr, ConstString):
			self._lower_const_string(instr)
		elif isinstance(instr, ArrayLit):
			self._lower_array_lit(instr)
		elif isinstance(instr, ArrayIndexLoad):
			self._lower_array_index_load(instr)
		elif isinstance(instr, ArrayIndexStore):
			self._lower_array_index_store(instr)
		elif isinstance(instr, ArrayLen):
			self._lower_array_len(instr)
		elif isinstance(instr, ArrayCap):
			self._lower_array_cap(instr)
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
			self.lines.append(f"  {tmp0} = insertvalue {FNRESULT_INT_ERROR} undef, i1 0, 0")
			self.lines.append(f"  {tmp1} = insertvalue {FNRESULT_INT_ERROR} {tmp0}, i64 {val}, 1")
			self.lines.append(f"  {dest} = insertvalue {FNRESULT_INT_ERROR} {tmp1}, {err_zero}, 2")
		elif isinstance(instr, ConstructResultErr):
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			self.value_types[dest] = FNRESULT_INT_ERROR
			tmp0 = self._fresh("err0")
			tmp1 = self._fresh("err1")
			self.lines.append(f"  {tmp0} = insertvalue {FNRESULT_INT_ERROR} undef, i1 1, 0")
			self.lines.append(f"  {tmp1} = insertvalue {FNRESULT_INT_ERROR} {tmp0}, i64 0, 1")
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
			self.lines.append(f"  {tmp0} = insertvalue {DRIFT_ERROR_TYPE} undef, i64 {code}, 0")
			self.lines.append(f"  {tmp1} = insertvalue {DRIFT_ERROR_TYPE} {tmp0}, ptr null, 1")
			self.lines.append(f"  {tmp2} = insertvalue {DRIFT_ERROR_TYPE} {tmp1}, ptr null, 2")
			self.lines.append(f"  {dest} = insertvalue {DRIFT_ERROR_TYPE} {tmp2}, ptr null, 3")
		elif isinstance(instr, Phi):
			# Already handled in _lower_phi.
			return
		else:
			raise NotImplementedError(f"LLVM codegen v1: unsupported instr {type(instr).__name__}")

	def _lower_const_string(self, instr: ConstString) -> None:
		"""
		Lower a ConstString to a DriftString literal ({len: %drift.size, data: i8*}).
		We emit a private unnamed constant for the bytes (with trailing NUL) and
		build the struct inline; no runtime call is needed for literals.

		Literals are encoded as UTF-8 and emitted with explicit escapes so that
		non-ASCII and special characters are preserved exactly.
		"""
		dest = self._map_value(instr.dest)
		utf8_bytes = instr.value.encode("utf-8")
		size = len(utf8_bytes)
		global_name = f"@.str{len(self.module.consts)}"
		escaped = "".join(_escape_byte(b) for b in utf8_bytes) + "\\00"
		self.module.consts.append(
			f"{global_name} = private unnamed_addr constant [{size + 1} x i8] c\"{escaped}\""
		)
		ptr = self._fresh("strptr")
		self.lines.append(
			f"  {ptr} = getelementptr inbounds [{size + 1} x i8], [{size + 1} x i8]* {global_name}, i32 0, i32 0"
		)
		tmp0 = self._fresh("str0")
		self.lines.append(f"  {tmp0} = insertvalue {DRIFT_STRING_TYPE} undef, {DRIFT_SIZE_TYPE} {size}, 0")
		self.lines.append(f"  {dest} = insertvalue {DRIFT_STRING_TYPE} {tmp0}, i8* {ptr}, 1")
		self.value_types[dest] = DRIFT_STRING_TYPE

	def _lower_call(self, instr: Call) -> None:
		dest = self._map_value(instr.dest) if instr.dest else None
		callee_info = self.fn_infos.get(instr.fn)
		if callee_info is None:
			raise NotImplementedError(f"LLVM codegen v1: missing FnInfo for callee {instr.fn}")

		arg_parts: list[str] = []
		if callee_info.signature and callee_info.signature.param_type_ids is not None:
			sig = callee_info.signature
			if len(sig.param_type_ids) != len(instr.args):
				raise NotImplementedError(
					f"LLVM codegen v1: arg count mismatch for {instr.fn}: "
					f"MIR has {len(instr.args)}, signature has {len(sig.param_type_ids)}"
				)
			for ty_id, arg in zip(sig.param_type_ids, instr.args):
				llty = self._llvm_type_for_typeid(ty_id)
				arg_parts.append(f"{llty} {self._map_value(arg)}")
		else:
			# Legacy fallback: assume all args are Ints.
			arg_parts = [f"i64 {self._map_value(a)}" for a in instr.args]
		args = ", ".join(arg_parts)

		if callee_info.declared_can_throw:
			tmp = self._fresh("call")
			self.lines.append(f"  {tmp} = call {FNRESULT_INT_ERROR} @{instr.fn}({args})")
			if dest:
				self.lines.append(f"  {dest} = extractvalue {FNRESULT_INT_ERROR} {tmp}, 1")
				self.value_types[dest] = "i64"
		else:
			ret_ty = "i64"
			if (
				self.string_type_id is not None
				and callee_info.signature
				and callee_info.signature.return_type_id == self.string_type_id
			):
				ret_ty = DRIFT_STRING_TYPE
			if dest is None and ret_ty != "void":
				raise NotImplementedError("LLVM codegen v1: cannot drop non-void call result")
			if dest is None:
				self.lines.append(f"  call void @{instr.fn}({args})")
			else:
				self.lines.append(f"  {dest} = call {ret_ty} @{instr.fn}({args})")
				self.value_types[dest] = ret_ty

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
				ty = self.value_types.get(val)
				if ty == DRIFT_STRING_TYPE:
					self.lines.append(f"  ret {DRIFT_STRING_TYPE} {val}")
				elif ty == "i64":
					self.lines.append(f"  ret i64 {val}")
				else:
					raise NotImplementedError(
						f"LLVM codegen v1: non-can-throw return must be Int or String, got {ty}"
					)
		else:
			raise NotImplementedError(f"LLVM codegen v1: unsupported terminator {type(term).__name__}")

	def _return_llvm_type(self) -> str:
		# v1 supports Int, String, or FnResult<Int, Error> return shapes.
		if self.fn_info.declared_can_throw:
			return FNRESULT_INT_ERROR
		rt_id = None
		if self.fn_info.signature and self.fn_info.signature.return_type_id is not None:
			rt_id = self.fn_info.signature.return_type_id
		if self.string_type_id is not None and rt_id == self.string_type_id:
			return DRIFT_STRING_TYPE
		# Default to Int
		return "i64"

	def _llvm_type_for_typeid(self, ty_id: TypeId) -> str:
		"""
		Map a TypeId to an LLVM type string for parameters/arguments.

		v1 supports only Int (i64) and String (%DriftString) in params.
		"""
		if self.int_type_id is not None and ty_id == self.int_type_id:
			return "i64"
		if self.string_type_id is not None and ty_id == self.string_type_id:
			return DRIFT_STRING_TYPE
		raise NotImplementedError(
			f"LLVM codegen v1: unsupported param type id {ty_id!r} for function {self.func.name}"
		)

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
		# Backend v1 only supports straight-line/acyclic SSA; anything else must bail
		# loudly so we never emit IR for loops/backedges until explicitly supported.
		if cfg_kind not in (CfgKind.STRAIGHT_LINE, CfgKind.ACYCLIC):
			raise NotImplementedError("LLVM codegen v1: loops/backedges are not supported yet")

	def _type_of(self, value_id: str) -> str | None:
		"""Best-effort lookup of an LLVM type string for a value id."""
		name = self._map_value(value_id)
		return self.value_types.get(name)

	def _lower_array_lit(self, instr: ArrayLit) -> None:
		"""Lower ArrayLit by allocating, storing elements, and building the header struct."""
		dest = self._map_value(instr.dest)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_type(elem_llty)
		elem_size = self._sizeof(elem_llty)
		elem_align = self._alignof(elem_llty)
		count = len(instr.elements)
		# Call drift_alloc_array(elem_size, elem_align, len, cap)
		len_const = count
		cap_const = count
		tmp_alloc = self._fresh("arr")
		self.lines.append(
			f"  {tmp_alloc} = call ptr @drift_alloc_array(i64 {elem_size}, i64 {elem_align}, %drift.size {len_const}, %drift.size {cap_const})"
		)
		# Bitcast to elem*
		tmp_data = self._fresh("data")
		self.lines.append(f"  {tmp_data} = bitcast ptr {tmp_alloc} to {elem_llty}*")
		# Store elements
		for idx, elem in enumerate(instr.elements):
			elem_val = self._map_value(elem)
			tmp_ptr = self._fresh("eltptr")
			self.lines.append(f"  {tmp_ptr} = getelementptr inbounds {elem_llty}, {elem_llty}* {tmp_data}, i64 {idx}")
			self.lines.append(f"  store {elem_llty} {elem_val}, {elem_llty}* {tmp_ptr}")
		# Build the array struct {len, cap, data}
		tmp0 = self._fresh("arrh0")
		tmp1 = self._fresh("arrh1")
		self.lines.append(f"  {tmp0} = insertvalue {arr_llty} undef, %drift.size {len_const}, 0")
		self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, %drift.size {cap_const}, 1")
		self.lines.append(f"  {dest} = insertvalue {arr_llty} {tmp1}, {elem_llty}* {tmp_data}, 2")
		self.value_types[dest] = arr_llty

	def _lower_array_index_load(self, instr: ArrayIndexLoad) -> None:
		"""Lower ArrayIndexLoad with bounds checks and a load from data[idx]."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_type(elem_llty)
		# Extract len and data
		len_tmp = self._fresh("len")
		cap_tmp = self._fresh("cap")
		data_tmp = self._fresh("data")
		self.lines.append(f"  {len_tmp} = extractvalue {arr_llty} {array}, 0")
		self.lines.append(f"  {cap_tmp} = extractvalue {arr_llty} {array}, 1")
		self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {array}, 2")
		# Bounds checks: idx < 0 or idx >= len => drift_bounds_check_fail
		neg_cmp = self._fresh("negcmp")
		self.lines.append(f"  {neg_cmp} = icmp slt %drift.size {index}, 0")
		oob_cmp = self._fresh("oobcmp")
		self.lines.append(f"  {oob_cmp} = icmp uge %drift.size {index}, %drift.size {len_tmp}")
		oob_or = self._fresh("oobor")
		self.lines.append(f"  {oob_or} = or i1 {neg_cmp}, {oob_cmp}")
		ok_block = self._fresh("array_ok")
		fail_block = self._fresh("array_oob")
		self.lines.append(f"  br i1 {oob_or}, label {fail_block}, label {ok_block}")
		# Fail block
		self.lines.append(f"{fail_block[1:]}:")
		self.lines.append(
			f"  call void @drift_bounds_check_fail(%drift.size {index}, {len_tmp})"
		)
		self.lines.append("  unreachable")
		# Ok block
		self.lines.append(f"{ok_block[1:]}:")
		ptr_tmp = self._fresh("eltptr")
		self.lines.append(f"  {ptr_tmp} = getelementptr inbounds {elem_llty}, {elem_llty}* {data_tmp}, %drift.size {index}")
		self.lines.append(f"  {dest} = load {elem_llty}, {elem_llty}* {ptr_tmp}")
		self.value_types[dest] = elem_llty

	def _lower_array_index_store(self, instr: ArrayIndexStore) -> None:
		"""Lower ArrayIndexStore with bounds checks and a store into data[idx]."""
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		value = self._map_value(instr.value)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_type(elem_llty)
		# Extract len and data
		len_tmp = self._fresh("len")
		cap_tmp = self._fresh("cap")
		data_tmp = self._fresh("data")
		self.lines.append(f"  {len_tmp} = extractvalue {arr_llty} {array}, 0")
		self.lines.append(f"  {cap_tmp} = extractvalue {arr_llty} {array}, 1")
		self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {array}, 2")
		# Bounds checks
		neg_cmp = self._fresh("negcmp")
		self.lines.append(f"  {neg_cmp} = icmp slt %drift.size {index}, 0")
		oob_cmp = self._fresh("oobcmp")
		self.lines.append(f"  {oob_cmp} = icmp uge %drift.size {index}, %drift.size {len_tmp}")
		oob_or = self._fresh("oobor")
		self.lines.append(f"  {oob_or} = or i1 {neg_cmp}, {oob_cmp}")
		ok_block = self._fresh("array_ok")
		fail_block = self._fresh("array_oob")
		self.lines.append(f"  br i1 {oob_or}, label {fail_block}, label {ok_block}")
		# Fail
		self.lines.append(f"{fail_block[1:]}:")
		self.lines.append(
			f"  call void @drift_bounds_check_fail(%drift.size {index}, {len_tmp})"
		)
		self.lines.append("  unreachable")
		# Ok
		self.lines.append(f"{ok_block[1:]}:")
		ptr_tmp = self._fresh("eltptr")
		self.lines.append(f"  {ptr_tmp} = getelementptr inbounds {elem_llty}, {elem_llty}* {data_tmp}, %drift.size {index}")
		self.lines.append(f"  store {elem_llty} {value}, {elem_llty}* {ptr_tmp}")
		# No dest; ArrayIndexStore returns void.

	def _lower_array_len(self, instr: ArrayLen) -> None:
		"""Lower ArrayLen by extracting the len field (index 0)."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		arr_llty = self.value_types.get(array, self._llvm_array_type("i64"))
		self.lines.append(f"  {dest} = extractvalue {arr_llty} {array}, 0")
		self.value_types[dest] = DRIFT_SIZE_TYPE

	def _lower_array_cap(self, instr: ArrayCap) -> None:
		"""Lower ArrayCap by extracting the cap field (index 1)."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		arr_llty = self.value_types.get(array, self._llvm_array_type("i64"))
		self.lines.append(f"  {dest} = extractvalue {arr_llty} {array}, 1")
		self.value_types[dest] = DRIFT_SIZE_TYPE

	def _llvm_array_type(self, elem_llty: str) -> str:
		return f"{{ %drift.size, %drift.size, {elem_llty}* }}"

	def _llvm_array_elem_type(self, elem_ty: int) -> str:
		"""
		Map an element TypeId to an LLVM type string.

		v1 backend assumes Array<Int> only and maps to i64 element type.
		Raises if an unexpected elem type is seen.
		"""
		return "i64"

	def _sizeof(self, elem_llty: str) -> int:
		# v1: i64 → 8, i1 → 1, ptr -> 8
		if elem_llty == "i1":
			return 1
		return 8

	def _alignof(self, elem_llty: str) -> int:
		if elem_llty == "i1":
			return 1
		return 8
