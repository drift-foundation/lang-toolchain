# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
SSA → LLVM IR lowering for the v1 Drift ABI (textual emitter).

Scope (v1 bring-up):
  - Input: SSA (`SsaFunc`) plus MIR (`MirFunc`) and `FnInfo` metadata.
  - Supported types: Int (i64), Bool (i1 in regs), String ({%drift.size, i8*}),
    Array<T>, and FnResult<ok, Error> where ok ∈ {Int, String, Void-like, Ref<T>}
    (arrays are supported as values but not as FnResult ok payloads yet).
  - Supported ops: ConstInt/Bool/String, AssignSSA aliases, BinaryOpInstr (int),
    Call (Int/String or FnResult return), Phi, ConstructResultOk/Err,
    ConstructError (attrs zeroed), Return, IfTerminator/Goto, Array ops.
  - FnResult lowering requires a TypeTable so we can map ok/error TypeIds to
    LLVM payloads; we fail fast without it for can-throw functions. FnResult
    ok payloads outside {Int, String, Void-like, Ref<T>} are currently rejected.
  - Control flow: straight-line, if/else, and loops/backedges (general CFGs).

ABI (from docs/design/drift-lang-abi.md):
  - %DriftError           = { i64 code, i8* attrs, i64 attr_count, i8* frames, i64 frame_count }
  - %FnResult_Int_Error   = { i1 is_err, i64 ok, %DriftError* err }
  - %FnResult_String_Error= { i1 is_err, %DriftString ok, %DriftError* err }
  - %FnResult_Void_Error  = { i1 is_err, i8 ok, %DriftError* err } (void-like ok)
  - %DriftString          = { %drift.size, i8* }
  - %drift.size           = i64 (Uint carrier)
  - Drift Int is i64; Bool is i1 in registers.

This emitter is deliberately small and produces LLVM text suitable for feeding
to `lli`/`clang` in tests. It avoids allocas and relies on SSA/phinode lowering
directly. Unsupported features raise clear errors rather than emitting bad IR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

from lang2.driftc.checker import FnInfo
from lang2.driftc.stage1 import BinaryOp, UnaryOp
from lang2.driftc.stage2 import (
	ArrayCap,
	ArrayIndexLoad,
	ArrayIndexStore,
	ArrayLen,
	ArrayLit,
	AssignSSA,
	BinaryOpInstr,
	Call,
	ConstructStruct,
	ConstructVariant,
	VariantTag,
	VariantGetField,
	StructGetField,
	ConstructDV,
	ConstBool,
	ConstInt,
	ConstString,
	ZeroValue,
	ConstructError,
	ErrorAddAttrDV,
	ErrorEvent,
	ConstructResultErr,
	ConstructResultOk,
	DVAsBool,
	DVAsInt,
	DVAsString,
	ErrorAttrsGetDV,
	LoadLocal,
	AddrOfLocal,
	AddrOfArrayElem,
	AddrOfField,
	LoadRef,
	StoreRef,
	OptionalIsSome,
	OptionalValue,
	ResultErr,
	ResultIsErr,
	ResultOk,
	Goto,
	IfTerminator,
	MirFunc,
	Phi,
	Return,
	Unreachable,
	StringConcat,
	StringEq,
	StringCmp,
	StringLen,
	StringFromBool,
	StringFromInt,
	StringFromUint,
	StringFromFloat,
	StoreLocal,
	UnaryOpInstr,
	ConstFloat,
)
from lang2.driftc.stage4.ssa import SsaFunc
from lang2.driftc.stage4.ssa import CfgKind
from lang2.driftc.core.types_core import TypeKind, TypeTable, TypeId

# ABI type names
DRIFT_ERROR_TYPE = "%DriftError"
DRIFT_ERROR_PTR = f"{DRIFT_ERROR_TYPE}*"
FNRESULT_INT_ERROR = "%FnResult_Int_Error"
DRIFT_SIZE_TYPE = "%drift.size"
DRIFT_STRING_TYPE = "%DriftString"

# --- LLVM identifier helpers ---
#
# Drift method symbols are scoped (e.g., `Point::move_by`). In textual LLVM IR,
# such names must be quoted: `@"Point::move_by"`. Keep this logic centralized so
# declarations and call sites stay consistent.
_LLVM_BARE_IDENT_RE = re.compile(r"^[A-Za-z$._][A-Za-z$._0-9]*$")


def _llvm_fn_sym(name: str) -> str:
	"""
	Render a function symbol name for LLVM IR.

	- For simple identifiers (`main`, `drift_console_writeln`), emit `@name`.
	- For names containing punctuation (`Point::move_by`), emit a quoted name:
	  `@"Point::move_by"`.
	"""
	if _LLVM_BARE_IDENT_RE.match(name):
		return f"@{name}"
	escaped = name.replace("\\", "\\5c").replace("\"", "\\22")
	return f"@\"{escaped}\""


def _module_id_from_symbol(sym: str) -> str | None:
	"""
	Best-effort extraction of a module id from a qualified callable symbol.

	In a workspace build we qualify callables as:
	  - free functions: `mod.sub::foo` (except the entrypoint `main`)
	  - methods: `mod.sub::Type::method`

	This helper returns the module id prefix before the first `::`, or `None`
	for unqualified symbols (e.g. `main`).
	"""
	if "::" not in sym:
		return None
	return sym.split("::", 1)[0]
DRIFT_DV_TYPE = "%DriftDiagnosticValue"
DRIFT_OPT_INT_TYPE = "%DriftOptionalInt"
DRIFT_OPT_BOOL_TYPE = "%DriftOptionalBool"
DRIFT_OPT_STRING_TYPE = "%DriftOptionalString"


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
	  - Returns: Int, String, or FnResult<ok, Error> (ok ∈ {Int, String, Void-like, Ref<T>}) in v1;
	    arrays are supported as values but not as FnResult ok payloads yet.
	  - General CFGs (including loops/backedges) are supported in v1.
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
	rename_map: Optional[Mapping[str, str]] = None,
	argv_wrapper: Optional[str] = None,
) -> LlvmModuleBuilder:
	"""
	Lower a set of SSA functions to an LLVM module.

	Args:
	  funcs: name -> MIR function
	  ssa_funcs: name -> SSA wrapper (must align with funcs)
	  fn_infos: name -> FnInfo for each function
	"""
	mod = LlvmModuleBuilder()

	# --- ABI-boundary export wrappers (Milestone 4) --------------------------
	#
	# Drift's language-level type system does not expose ABI shapes like
	# `FnResult<T, Error>`, but package/module boundaries do. We model this at the
	# LLVM emission layer by:
	#
	# 1) Renaming exported function bodies to private `__impl` symbols.
	# 2) Emitting public wrapper functions under the original symbol name.
	#
	# Wrappers are called only across module boundaries (calls from another
	# module to an exported symbol). Internal (same-module) calls are redirected
	# to the `__impl` symbol and therefore keep the internal calling convention.
	#
	# Wrapper calling convention:
	# - If the function is can-throw, the wrapper forwards the internal
	#   `FnResult<ok, Error>` carrier unchanged.
	# - If the function is not can-throw, the wrapper returns a `FnResult<ok, Error>`
	#   with `is_err=false` and `err=null`.
	#
	# This preserves the language semantics ("returns T") while making the
	# module interface uniformly boundary-shaped.
	# Driver-level renames (e.g. argv wrapper name) must not affect call-site
	# binding decisions. Keep them separate from export wrapper renames.
	driver_rename: dict[str, str] = dict(rename_map or {})
	body_rename: dict[str, str] = dict(driver_rename)
	export_impl_map: dict[str, str] = {}
	exported_fns: list[str] = []
	for name, info in fn_infos.items():
		sig = info.signature
		if sig is None or not bool(getattr(sig, "is_exported_entrypoint", False)):
			continue
		if bool(getattr(sig, "is_method", False)):
			continue
		# Only functions that exist in the current module (i.e. present in funcs)
		# can have wrappers emitted here. Imported functions are declared elsewhere.
		if name not in funcs:
			continue
		if name in body_rename:
			# If the driver already renamed this symbol (e.g. argv wrapper), do not
			# add another layer of indirection.
			continue
		impl = f"{name}__impl"
		body_rename[name] = impl
		export_impl_map[name] = impl
		exported_fns.append(name)

	for name, mir_func in funcs.items():
		ssa = ssa_funcs[name]
		fn_info = fn_infos[name]
		builder = _FuncBuilder(
			func=mir_func,
			ssa=ssa,
			fn_info=fn_info,
			fn_infos=fn_infos,
			module=mod,
			type_table=type_table,
			sym_name=body_rename.get(name),
			rename_map=driver_rename,
			export_impl_map=export_impl_map,
		)
		mod.emit_func(builder.lower())

	# Emit wrappers after all implementation bodies so they can reference the
	# renamed `__impl` symbols.
	for public in sorted(exported_fns):
		info = fn_infos[public]
		sig = info.signature
		assert sig is not None
		impl = export_impl_map[public]
		type_builder = _FuncBuilder(
			func=funcs[public],
			ssa=ssa_funcs[public],
			fn_info=info,
			fn_infos=fn_infos,
			module=mod,
			type_table=type_table,
			sym_name=impl,
			rename_map=driver_rename,
			export_impl_map=export_impl_map,
		)
		type_builder._prime_type_ids()
		# Wrapper parameters mirror the internal function exactly.
		param_parts: list[str] = []
		param_names = list(sig.param_names or [])
		param_tids = list(sig.param_type_ids or [])
		if len(param_names) != len(param_tids):
			# Older tests may omit param_names; fall back to positional `p{i}`.
			param_names = [f"p{i}" for i in range(len(param_tids))]
		for i, ty_id in enumerate(param_tids):
			llty = type_builder._llvm_type_for_typeid(ty_id)
			param_parts.append(f"{llty} %{param_names[i]}")
		params_str = ", ".join(param_parts)

		# Return type: always a boundary FnResult for exported entrypoints.
		ok_llty, ok_key = type_builder._llvm_ok_type_for_sig(sig)
		fnres_llty = mod.fnresult_type(ok_key, ok_llty)

		lines: list[str] = []
		lines.append(f"define {fnres_llty} {_llvm_fn_sym(public)}({params_str}) {{")
		lines.append("entry:")
		args = ", ".join(f"{type_builder._llvm_type_for_typeid(t)} %{n}" for t, n in zip(param_tids, param_names))

		if info.declared_can_throw:
			# Forward can-throw carrier result unchanged.
			lines.append(f"  %res = call {fnres_llty} {_llvm_fn_sym(impl)}({args})")
			lines.append(f"  ret {fnres_llty} %res")
		else:
			# Wrap a non-throwing return into FnResult.Ok(...).
			ret_tid = sig.return_type_id
			is_void_ret = ret_tid is not None and type_builder._is_void_typeid(ret_tid)
			if is_void_ret:
				lines.append(f"  call void {_llvm_fn_sym(impl)}({args})")
				ok_val = "0"
			else:
				ret_ty = "i64"
				if ret_tid is not None and type_table is not None:
					ret_ty = type_builder._llvm_type_for_typeid(ret_tid)
				lines.append(f"  %ok = call {ret_ty} {_llvm_fn_sym(impl)}({args})")
				ok_val = "%ok"
			lines.append(f"  %tmp0 = insertvalue {fnres_llty} undef, i1 0, 0")
			lines.append(f"  %tmp1 = insertvalue {fnres_llty} %tmp0, {ok_llty} {ok_val}, 1")
			lines.append(f"  %tmp2 = insertvalue {fnres_llty} %tmp1, {DRIFT_ERROR_PTR} null, 2")
			lines.append(f"  ret {fnres_llty} %tmp2")
		lines.append("}")
		mod.emit_func("\n".join(lines))

	if argv_wrapper is not None:
		array_llty = f"{{ {DRIFT_SIZE_TYPE}, {DRIFT_SIZE_TYPE}, {DRIFT_STRING_TYPE}* }}"
		mod.emit_argv_entry_wrapper(user_main=argv_wrapper, array_type=array_llty)
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
	needs_string_eq: bool = False
	needs_string_cmp: bool = False
	needs_string_concat: bool = False
	needs_string_from_int64: bool = False
	needs_string_from_uint64: bool = False
	needs_string_from_bool: bool = False
	needs_string_from_f64: bool = False
	needs_argv_helper: bool = False
	needs_console_runtime: bool = False
	needs_dv_runtime: bool = False
	needs_error_runtime: bool = False
	needs_llvm_trap: bool = False
	array_string_type: Optional[str] = None
	_fnresult_types_by_key: Dict[str, str] = field(default_factory=dict)
	_fnresult_ok_llty_by_type: Dict[str, str] = field(default_factory=dict)
	_fnresult_unwrap_helpers: Dict[str, str] = field(default_factory=dict)
	_struct_types_by_name: Dict[str, str] = field(default_factory=dict)
	_variant_types_by_id: Dict[int, str] = field(default_factory=dict)

	def __post_init__(self) -> None:
		if DRIFT_SIZE_TYPE.startswith("%"):
			self.type_decls.append(f"{DRIFT_SIZE_TYPE} = type i64")
		self.type_decls.extend(
			[
				f"{DRIFT_STRING_TYPE} = type {{ {DRIFT_SIZE_TYPE}, i8* }}",
				f"{DRIFT_ERROR_TYPE} = type {{ i64, {DRIFT_STRING_TYPE}, i8*, {DRIFT_SIZE_TYPE}, i8*, {DRIFT_SIZE_TYPE} }}",
				f"{FNRESULT_INT_ERROR} = type {{ i1, i64, {DRIFT_ERROR_PTR} }}",
				f"{DRIFT_DV_TYPE} = type {{ i8, [7 x i8], [2 x i64] }}",
				f"{DRIFT_OPT_INT_TYPE} = type {{ i8, i64 }}",
				f"{DRIFT_OPT_BOOL_TYPE} = type {{ i8, i8 }}",
				f"{DRIFT_OPT_STRING_TYPE} = type {{ i8, {DRIFT_STRING_TYPE} }}",
				"%DriftArrayHeader = type { i64, i64, i8* }",
			]
		)
		# Seed the canonical FnResult types for supported ok payloads.
		self._fnresult_types_by_key["Int"] = FNRESULT_INT_ERROR
		self._fnresult_ok_llty_by_type[FNRESULT_INT_ERROR] = "i64"
		self._declare_fnresult_named_type("Void", "i8", "%FnResult_Void_Error")
		self._declare_fnresult_named_type("String", DRIFT_STRING_TYPE, "%FnResult_String_Error")

	def ensure_struct_type(
		self,
		ty_id: TypeId,
		*,
		type_table: TypeTable,
		map_type: callable,
	) -> str:
		"""
		Ensure a nominal struct TypeId is declared as a named LLVM type.

		We declare structs lazily as they are encountered in signatures/IR, and we
		cache by nominal type name so multiple functions share the same LLVM type.

		Args:
		  ty_id: TypeId of the struct (TypeKind.STRUCT).
		  type_table: the shared TypeTable defining struct schemas.
		  map_type: callback `TypeId -> llty` used to map field types.

		Returns:
		  LLVM type name (e.g. `%Struct_Point`).
		"""
		td = type_table.get(ty_id)
		if td.kind is not TypeKind.STRUCT:
			raise AssertionError("ensure_struct_type called with non-STRUCT TypeId")
		name = td.name
		if name in self._struct_types_by_name:
			return self._struct_types_by_name[name]
		safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
		llvm_name = f"%Struct_{safe}"
		# Insert into cache before mapping fields to allow self-recursive pointer
		# shapes like `struct Node { next: &Node }` to refer to the named type.
		self._struct_types_by_name[name] = llvm_name
		field_lltys = [map_type(ft) for ft in td.param_types]
		body = ", ".join(field_lltys) if field_lltys else ""
		self.type_decls.append(f"{llvm_name} = type {{ {body} }}")
		return llvm_name

	def ensure_variant_type(self, ty_id: TypeId, *, payload_words: int) -> str:
		"""
		Ensure a concrete variant TypeId is declared as a named LLVM type.

		Variant ABI is compiler-private in MVP, but we still want a stable,
		readable named type in the emitted module for debugging and to avoid
		repeating literal struct types everywhere.

		Internal representation (v1):
		  %Variant_<id> = type { i8 tag, [7 x i8] pad, [payload_words x i64] payload }

		The 7-byte pad ensures the payload begins at an 8-byte aligned offset,
		which is sufficient for all currently supported field types (Int/Uint,
		Float, pointers, DriftString, and aggregates built from those).
		"""
		if ty_id in self._variant_types_by_id:
			return self._variant_types_by_id[ty_id]
		payload_words = max(1, int(payload_words))
		name = f"%Variant_{ty_id}"
		self._variant_types_by_id[ty_id] = name
		self.type_decls.append(f"{name} = type {{ i8, [7 x i8], [{payload_words} x i64] }}")
		return name

	def fnresult_type(self, ok_key: str, ok_llty: str) -> str:
		"""
		Return the LLVM struct type for FnResult<ok_llty, Error>.

		We emit named types per ok payload for readability/ABI stability. Supported
		ok payloads in v1 include:
		  - Int (i64), String (%DriftString), Void-like (i8), Ref<T> (T*)
		  - concrete Struct and Variant values by-value (compiler-private ABI)

		Error slot is always %DriftError*.
		"""
		if ok_key in self._fnresult_types_by_key:
			return self._fnresult_types_by_key[ok_key]
		if ok_key == "Int":
			return FNRESULT_INT_ERROR
		if ok_key == "String":
			return self._declare_fnresult_named_type(ok_key, ok_llty, "%FnResult_String_Error")
		if ok_key == "Void":
			return self._declare_fnresult_named_type(ok_key, ok_llty, "%FnResult_Void_Error")
		# Other supported ok payloads are emitted as named types lazily.
		return self._declare_fnresult_named_type(ok_key, ok_llty)

	def fnresult_unwrap_ok_or_trap(self, ok_key: str, fnres_llty: str, ok_llty: str) -> str:
		"""
		Emit (or reuse) a tiny helper that unwraps `FnResult.Ok` or traps.

		This is used at ABI boundaries where the surface language expects a plain
		value `T`, but the module interface uses the uniform `FnResult<T, Error*>`
		shape. We must not silently treat an error as a value.
		"""
		# Cache key must include both the stable ok_key and the concrete ok_llty.
		cache_key = f"{ok_key}:{ok_llty}"
		name = self._fnresult_unwrap_helpers.get(cache_key)
		if name is not None:
			return name
		self.needs_llvm_trap = True
		safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in ok_key)
		name = f"@drift_fnresult_unwrap_ok_or_trap_{safe}"
		self._fnresult_unwrap_helpers[cache_key] = name
		lines: list[str] = []
		lines.append(f"define {ok_llty} {name}({fnres_llty} %res) {{")
		lines.append("entry:")
		lines.append(f"  %is_err = extractvalue {fnres_llty} %res, 0")
		lines.append("  br i1 %is_err, label %trap, label %ok")
		lines.append("trap:")
		lines.append("  call void @llvm.trap()")
		lines.append("  unreachable")
		lines.append("ok:")
		lines.append(f"  %okv = extractvalue {fnres_llty} %res, 1")
		lines.append(f"  ret {ok_llty} %okv")
		lines.append("}")
		self.funcs.append("\n".join(lines))
		return name

	def _declare_fnresult_named_type(self, ok_key: str, ok_llty: str, name: str | None = None) -> str:
		"""Declare and cache a named FnResult type for the given ok payload."""
		if ok_key in self._fnresult_types_by_key:
			return self._fnresult_types_by_key[ok_key]
		type_name = name or f"%FnResult_{ok_key}_Error"
		self.type_decls.append(f"{type_name} = type {{ i1, {ok_llty}, {DRIFT_ERROR_PTR} }}")
		self._fnresult_types_by_key[ok_key] = type_name
		self._fnresult_ok_llty_by_type[type_name] = ok_llty
		return type_name

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
					f"  %ret = call i64 {_llvm_fn_sym(drift_main)}()",
					"  %trunc = trunc i64 %ret to i32",
					"  ret i32 %trunc",
					"}",
				]
			)
		)

	def emit_argv_entry_wrapper(self, user_main: str, array_type: str) -> None:
		"""
		Emit an OS entry for `main(argv: Array<String>) returns Int`.

		Builds Array<String> via the runtime helper and truncates the i64 Int
		result to i32 for the process exit code.
		"""
		self.needs_argv_helper = True
		self.array_string_type = array_type
		lines = [
			"define i32 @main(i32 %argc, i8** %argv) {",
			"entry:",
			"  %arr.ptr = alloca %DriftArrayHeader",
			"  call void @drift_build_argv(%DriftArrayHeader* sret(%DriftArrayHeader) %arr.ptr, i32 %argc, i8** %argv)",
			"  %arr = load %DriftArrayHeader, %DriftArrayHeader* %arr.ptr",
			"  %len = extractvalue %DriftArrayHeader %arr, 0",
			"  %cap = extractvalue %DriftArrayHeader %arr, 1",
			"  %data_raw = extractvalue %DriftArrayHeader %arr, 2",
			"  %data = bitcast i8* %data_raw to %DriftString*",
			f"  %tmp0 = insertvalue {array_type} undef, {DRIFT_SIZE_TYPE} %len, 0",
			f"  %tmp1 = insertvalue {array_type} %tmp0, {DRIFT_SIZE_TYPE} %cap, 1",
			f"  %argv_typed = insertvalue {array_type} %tmp1, %DriftString* %data, 2",
			f"  %ret = call i64 {_llvm_fn_sym(user_main)}({array_type} %argv_typed)",
			"  %trunc = trunc i64 %ret to i32",
			"  ret i32 %trunc",
			"}",
		]
		self.funcs.append("\n".join(lines))

	def render(self) -> str:
		lines: List[str] = []
		lines.extend(self.type_decls)
		lines.append("")
		if self.consts:
			lines.extend(self.consts)
			lines.append("")
		if self.needs_argv_helper:
			array_type = self.array_string_type or f"{{ {DRIFT_SIZE_TYPE}, {DRIFT_SIZE_TYPE}, {DRIFT_STRING_TYPE}* }}"
			lines.append(f"declare void @drift_build_argv(%DriftArrayHeader* sret(%DriftArrayHeader), i32, i8**)")
			lines.append("")
		if self.needs_array_helpers:
			lines.extend(
				[
					f"declare i8* @drift_alloc_array(i64, i64, {DRIFT_SIZE_TYPE}, {DRIFT_SIZE_TYPE})",
					f"declare void @drift_bounds_check_fail({DRIFT_SIZE_TYPE}, {DRIFT_SIZE_TYPE})",
					"",
				]
			)
		if self.needs_string_eq:
			lines.append(f"declare i1 @drift_string_eq({DRIFT_STRING_TYPE}, {DRIFT_STRING_TYPE})")
		if self.needs_string_cmp:
			lines.append(f"declare i32 @drift_string_cmp({DRIFT_STRING_TYPE}, {DRIFT_STRING_TYPE})")
		if self.needs_string_concat:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_concat({DRIFT_STRING_TYPE}, {DRIFT_STRING_TYPE})")
		if self.needs_string_from_int64:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_int64(i64)")
		if self.needs_string_from_uint64:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_uint64(i64)")
		if self.needs_string_from_bool:
			# Runtime takes an `int` (i32) for portability; caller must extend i1.
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_bool(i32)")
		if self.needs_string_from_f64:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_f64(double)")
		if (
			self.needs_string_eq
			or self.needs_string_cmp
			or self.needs_string_concat
			or self.needs_string_from_int64
			or self.needs_string_from_uint64
			or self.needs_string_from_bool
			or self.needs_string_from_f64
		):
			lines.append("")
		if self.needs_console_runtime:
			lines.extend(
				[
					f"declare void @drift_console_write({DRIFT_STRING_TYPE})",
					f"declare void @drift_console_writeln({DRIFT_STRING_TYPE})",
					f"declare void @drift_console_eprintln({DRIFT_STRING_TYPE})",
					"",
				]
			)
		if self.needs_dv_runtime:
			lines.extend(
				[
					f"declare void @__exc_attrs_get_dv({DRIFT_DV_TYPE}*, {DRIFT_ERROR_PTR}, {DRIFT_STRING_TYPE})",
					f"declare {DRIFT_DV_TYPE} @drift_dv_missing()",
					f"declare {DRIFT_DV_TYPE} @drift_dv_int(i64)",
					f"declare {DRIFT_DV_TYPE} @drift_dv_bool(i1)",
					f"declare {DRIFT_DV_TYPE} @drift_dv_string({DRIFT_STRING_TYPE})",
					f"declare {DRIFT_OPT_INT_TYPE} @drift_dv_as_int({DRIFT_DV_TYPE}*)",
					f"declare {DRIFT_OPT_BOOL_TYPE} @drift_dv_as_bool({DRIFT_DV_TYPE}*)",
					f"declare {DRIFT_OPT_STRING_TYPE} @drift_dv_as_string({DRIFT_DV_TYPE}*)",
					"",
				]
			)
		if self.needs_error_runtime:
			lines.extend(
				[
					f"declare {DRIFT_ERROR_PTR} @drift_error_new(i64, {DRIFT_STRING_TYPE})",
					f"declare {DRIFT_ERROR_PTR} @drift_error_new_with_payload(i64, {DRIFT_STRING_TYPE}, {DRIFT_STRING_TYPE}, {DRIFT_DV_TYPE})",
					f"declare void @drift_error_add_attr_dv({DRIFT_ERROR_PTR}, {DRIFT_STRING_TYPE}, {DRIFT_DV_TYPE}*)",
					"",
				]
			)
		if self.needs_llvm_trap:
			lines.append("declare void @llvm.trap()")
			lines.append("")
		lines.extend(self.funcs)
		lines.append("")
		return "\n".join(lines)


@dataclass(frozen=True)
class _VariantArmLayout:
	"""Per-constructor payload layout for a concrete variant TypeId."""

	tag: int
	# LLVM value types for fields (used when returning values to SSA).
	field_lltys: list[str]
	# LLVM storage types for fields inside the payload buffer (Bool stored as i8).
	field_storage_lltys: list[str]
	# Literal struct type used to pack/unpack the payload for this constructor.
	# Empty string means "no payload".
	payload_struct_llty: str


@dataclass(frozen=True)
class _VariantLayout:
	"""Concrete variant layout (compiler-private ABI) for one instantiated TypeId."""

	llvm_ty: str
	payload_words: int
	arms: Dict[str, _VariantArmLayout]


@dataclass
class _FuncBuilder:
	func: MirFunc
	ssa: SsaFunc
	fn_info: FnInfo
	fn_infos: Mapping[str, FnInfo]
	module: LlvmModuleBuilder
	# Name mapping hooks used by the module-level emitter for ABI-boundary export
	# wrappers. These maps are purely about symbol selection at call sites.
	#
	# - `export_impl_map` maps an exported public symbol name (e.g. `m::foo`) to
	#   its private implementation symbol name (e.g. `m::foo__impl`).
	# - `rename_map` is a more general symbol rename table supplied by the driver
	#   (e.g. for argv wrappers). Codegen only uses it defensively for call sites
	#   that must target renamed bodies.
	rename_map: Mapping[str, str] = field(default_factory=dict)
	export_impl_map: Mapping[str, str] = field(default_factory=dict)
	type_table: Optional[TypeTable] = None
	tmp_counter: int = 0
	lines: List[str] = field(default_factory=list)
	value_map: Dict[str, str] = field(default_factory=dict)
	value_types: Dict[str, str] = field(default_factory=dict)
	aliases: Dict[str, str] = field(default_factory=dict)
	# Locals whose address is taken via AddrOfLocal. These locals must be
	# represented as real storage (alloca + load/store) because references
	# require stable pointer identity.
	addr_taken_locals: set[str] = field(default_factory=set)
	# Local storage element type (LLVM type string) for address-taken locals.
	local_storage_types: Dict[str, str] = field(default_factory=dict)
	# LLVM value-id used as the alloca pointer for address-taken locals.
	local_allocas: Dict[str, str] = field(default_factory=dict)
	# Insertion point in `self.lines` for entry-block allocas/stores.
	_entry_alloca_insert_index: int | None = None
	string_type_id: Optional[TypeId] = None
	int_type_id: Optional[TypeId] = None
	bool_type_id: Optional[TypeId] = None
	float_type_id: Optional[TypeId] = None
	void_type_id: Optional[TypeId] = None
	dv_type_id: Optional[TypeId] = None
	opt_int_type_id: Optional[TypeId] = None
	opt_bool_type_id: Optional[TypeId] = None
	opt_string_type_id: Optional[TypeId] = None
	sym_name: Optional[str] = None
	# Variant lowering caches (compiler-private ABI).
	_variant_layouts: Dict[TypeId, "_VariantLayout"] = field(default_factory=dict)
	_size_align_cache: Dict[TypeId, tuple[int, int]] = field(default_factory=dict)

	def lower(self) -> str:
		self._assert_cfg_supported()
		self._prime_type_ids()
		self._scan_addr_taken_locals()
		self._collect_assign_aliases()
		self._emit_header()
		self._declare_array_helpers_if_needed()
		# LLVM defines the function "entry block" as the first basic block in the
		# function body. We rely on this invariant for memory-allocated locals:
		# `alloca` instructions must be placed in the entry block so they dominate
		# all uses and are eligible for canonical LLVM passes.
		#
		# The MIR/SSA layer already has a semantic entry (`self.func.entry`), but
		# the textual emission order can drift (e.g. when SSA doesn't record an
		# explicit block order). Make the invariant explicit here: always emit
		# `self.func.entry` first.
		order = self.ssa.block_order or list(self.func.blocks.keys())
		if order and order[0] != self.func.entry:
			order = [self.func.entry] + [b for b in order if b != self.func.entry]
		for block_name in order:
			self._emit_block(block_name)
		self.lines.append("}")
		return "\n".join(self.lines)

	def _collect_assign_aliases(self) -> None:
		"""
		Collect SSA alias relationships before emitting blocks.

		The SSA stage expresses many local definitions and loads as `AssignSSA`
		instructions. LLVM lowering treats these as *aliases* (no IR emission),
		so we must know the alias map when emitting Φ nodes. In cyclic CFGs (loops)
		and even in acyclic CFGs with forward references, a Φ node can refer to an
		alias that is defined in a block that appears later in textual emission
		order. Pre-collecting the alias map avoids producing undefined LLVM value
		names in Φ incomings.
		"""
		for block in self.func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, AssignSSA):
					self.aliases[instr.dest] = instr.src

	def _prime_type_ids(self) -> None:
		if self.type_table is None:
			return
		for ty_id, ty_def in getattr(self.type_table, "_defs", {}).items():  # type: ignore[attr-defined]
			if ty_def.kind is TypeKind.SCALAR and ty_def.name == "String":
				self.string_type_id = ty_id
			if ty_def.kind is TypeKind.SCALAR and ty_def.name == "Int":
				self.int_type_id = ty_id
			if ty_def.kind is TypeKind.SCALAR and ty_def.name == "Bool":
				self.bool_type_id = ty_id
			if ty_def.kind is TypeKind.SCALAR and ty_def.name == "Float":
				self.float_type_id = ty_id
			if ty_def.kind is TypeKind.VOID:
				self.void_type_id = ty_id
			if ty_def.kind is TypeKind.DIAGNOSTICVALUE:
				self.dv_type_id = ty_id
			if ty_def.kind is TypeKind.OPTIONAL and ty_def.param_types:
				inner = ty_def.param_types[0]
				if inner == self.int_type_id:
					self.opt_int_type_id = ty_id
				if inner == self.bool_type_id:
					self.opt_bool_type_id = ty_id
				if inner == self.string_type_id:
					self.opt_string_type_id = ty_id

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
		func_name = self.sym_name or self.func.name
		self.lines.append(f"define {ret_ty} {_llvm_fn_sym(func_name)}({params_str}) {{")

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

	def _scan_addr_taken_locals(self) -> None:
		"""
		Scan the MIR for locals whose address is taken.

		SSA keeps these locals in memory form (LoadLocal/StoreLocal are not
		rewritten to AssignSSA), and LLVM lowering allocates real storage slots
		for them. This is required for correctness of `&T` / `&mut T`: taking the
		address of a local must point at stable storage, not an SSA name.
		"""
		for block in self.func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, AddrOfLocal):
					self.addr_taken_locals.add(instr.local)

	def _alloca_name_for_local(self, local: str) -> str:
		"""
		Return a stable SSA name for the alloca pointer for a local.

		We keep it separate from the local name to avoid collisions with SSA
		versioned locals (e.g. `x_1`) and to make IR easier to read.
		"""
		safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in local)
		return f"{safe}__addr"

	def _ensure_entry_insertion_point(self) -> None:
		"""
		Ensure we have an insertion point for entry-block allocas/stores.

		Phi nodes (if any) must appear first in a block. We insert allocas after
		phis but before other instructions, and we only emit allocas in the entry
		block (LLVM best practice).
		"""
		# This is only valid while emitting the entry block. Other code should
		# assume the insertion point has already been established by entry-block
		# emission, not create it opportunistically (which could place allocas in
		# the wrong basic block if emission order changes).
		assert self._current_block_name == self.func.entry
		if self._entry_alloca_insert_index is None:
			self._entry_alloca_insert_index = len(self.lines)

	def _ensure_local_storage(self, local: str, llty: str) -> str:
		"""
		Ensure `local` has a dedicated storage slot and return the alloca value id.

		The alloca itself is emitted into the entry block at the recorded insertion
		point so it is in-scope for the whole function.
		"""
		existing = self.local_storage_types.get(local)
		if existing is not None and existing != llty:
			raise NotImplementedError(
				f"LLVM codegen v1: local '{local}' storage type mismatch (have {existing}, expected {llty})"
			)
		self.local_storage_types[local] = llty
		if local in self.local_allocas:
			return self.local_allocas[local]
		# Storage slots must live in the entry block to ensure the address is
		# stable and dominates all uses. The insertion point is established when
		# the entry block is emitted.
		assert self._entry_alloca_insert_index is not None
		alloca_id = self._alloca_name_for_local(local)
		self.local_allocas[local] = alloca_id
		self.value_map.setdefault(alloca_id, f"%{alloca_id}")
		self.value_types[self.value_map[alloca_id]] = f"{llty}*"
		self.lines.insert(self._entry_alloca_insert_index, f"  %{alloca_id} = alloca {llty}")
		self._entry_alloca_insert_index += 1
		return alloca_id

	def _emit_entry_param_inits(self) -> None:
		"""
		Initialize storage for address-taken parameters.

		When a parameter's address is taken (`&param`), we materialize a storage
		slot and store the incoming SSA parameter value into it in the entry block.
		"""
		if not self.addr_taken_locals:
			return
		if self.fn_info.signature is None or self.fn_info.signature.param_type_ids is None:
			return
		for pname, ty_id in zip(self.func.params, self.fn_info.signature.param_type_ids):
			if pname not in self.addr_taken_locals:
				continue
			llty = self._llvm_type_for_typeid(ty_id)
			alloca_id = self._ensure_local_storage(pname, llty)
			assert self._entry_alloca_insert_index is not None
			param_val = self._map_value(pname)
			self.lines.insert(self._entry_alloca_insert_index, f"  store {llty} {param_val}, {llty}* %{alloca_id}")
			self._entry_alloca_insert_index += 1

	def _emit_block(self, block_name: str) -> None:
		# Track current block name so instruction-level helpers can consult SSA maps.
		self._current_block_name = block_name
		block = self.func.blocks[block_name]
		self.lines.append(f"{block.name}:")
		# Emit phi nodes first.
		for instr in block.instructions:
			if isinstance(instr, Phi):
				self._lower_phi(block.name, instr)
		# Ensure entry-block allocas/stores are inserted after any phis.
		if block_name == self.func.entry:
			self._ensure_entry_insertion_point()
			self._emit_entry_param_inits()
		# Emit non-phi instructions.
		for idx, instr in enumerate(block.instructions):
			if isinstance(instr, Phi):
				continue
			self._lower_instr(instr, instr_index=idx)
		self._lower_term(block.terminator)
		# Best-effort cleanup; not strictly necessary.
		self._current_block_name = None

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

	def _lower_instr(self, instr: object, instr_index: int | None = None) -> None:
		if isinstance(instr, ConstInt):
			dest = self._map_value(instr.dest)
			self.value_types[dest] = "i64"
			self.lines.append(f"  {dest} = add i64 0, {instr.value}")
		elif isinstance(instr, ConstBool):
			dest = self._map_value(instr.dest)
			val = 1 if instr.value else 0
			self.value_types[dest] = "i1"
			self.lines.append(f"  {dest} = add i1 0, {val}")
		elif isinstance(instr, ConstFloat):
			dest = self._map_value(instr.dest)
			# Use Python's repr(...) to preserve sufficient precision for round-trips.
			# LLVM accepts decimal `double` literals in textual IR.
			lit = repr(instr.value)
			self.value_types[dest] = "double"
			self.lines.append(f"  {dest} = fadd double 0.0, {lit}")
		elif isinstance(instr, ZeroValue):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: ZeroValue requires a TypeTable")
			dest = self._map_value(instr.dest)
			self._emit_zero_value(dest, instr.ty)
		elif isinstance(instr, UnaryOpInstr):
			self._lower_unary(instr)
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
		elif isinstance(instr, StringLen):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			# StringLen is reused for strings and arrays at HIR level; here we assume string.
			self.lines.append(f"  {dest} = extractvalue {DRIFT_STRING_TYPE} {val}, 0")
			self.value_types[dest] = DRIFT_SIZE_TYPE
		elif isinstance(instr, StringConcat):
			dest = self._map_value(instr.dest)
			left = self._map_value(instr.left)
			right = self._map_value(instr.right)
			self.module.needs_string_concat = True
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_concat("
				f"{DRIFT_STRING_TYPE} {left}, {DRIFT_STRING_TYPE} {right})"
			)
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, StringFromInt):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			val_ty = self.value_types.get(val)
			if val_ty != "i64":
				raise NotImplementedError(
					f"LLVM codegen v1: StringFromInt requires i64 operand (have {val_ty})"
				)
			self.module.needs_string_from_int64 = True
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_from_int64(i64 {val})"
			)
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, StringFromUint):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			val_ty = self.value_types.get(val)
			if val_ty != "i64":
				raise NotImplementedError(
					f"LLVM codegen v1: StringFromUint requires i64 operand (have {val_ty})"
				)
			self.module.needs_string_from_uint64 = True
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_from_uint64(i64 {val})"
			)
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, StringFromBool):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			val_ty = self.value_types.get(val)
			if val_ty != "i1":
				raise NotImplementedError(
					f"LLVM codegen v1: StringFromBool requires i1 operand (have {val_ty})"
				)
			self.module.needs_string_from_bool = True
			ext = self._fresh("bext")
			self.lines.append(f"  {ext} = zext i1 {val} to i32")
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_from_bool(i32 {ext})"
			)
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, StringFromFloat):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			val_ty = self.value_types.get(val)
			if val_ty != "double":
				raise NotImplementedError(
					f"LLVM codegen v1: StringFromFloat requires double operand (have {val_ty})"
				)
			self.module.needs_string_from_f64 = True
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_from_f64(double {val})"
			)
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, StringEq):
			dest = self._map_value(instr.dest)
			left = self._map_value(instr.left)
			right = self._map_value(instr.right)
			self.module.needs_string_eq = True
			self.lines.append(
				f"  {dest} = call i1 @drift_string_eq({DRIFT_STRING_TYPE} {left}, {DRIFT_STRING_TYPE} {right})"
			)
			self.value_types[dest] = "i1"
		elif isinstance(instr, StringCmp):
			dest = self._map_value(instr.dest)
			left = self._map_value(instr.left)
			right = self._map_value(instr.right)
			left_ty = self.value_types.get(left)
			right_ty = self.value_types.get(right)
			if left_ty != DRIFT_STRING_TYPE or right_ty != DRIFT_STRING_TYPE:
				raise NotImplementedError("LLVM codegen v1: StringCmp requires String operands")
			self.module.needs_string_cmp = True
			tmp = self._fresh("strcmp")
			self.lines.append(
				f"  {tmp} = call i32 @drift_string_cmp({DRIFT_STRING_TYPE} {left}, {DRIFT_STRING_TYPE} {right})"
			)
			# Normalize to the compiler's Int carrier (i64) so downstream comparisons
			# use the same integer pipeline as other BinaryOpInstr nodes.
			self.lines.append(f"  {dest} = sext i32 {tmp} to i64")
			self.value_types[dest] = "i64"
		elif isinstance(instr, AssignSSA):
			# AssignSSA is a pure SSA alias. We pre-collect aliases in
			# `_collect_assign_aliases` so Φ lowering can resolve aliases even when
			# the defining AssignSSA appears in a later-emitted block.
			return
		elif isinstance(instr, LoadLocal):
			# Address-taken locals are materialized as storage. For them, LoadLocal
			# is a real `load` from the local's alloca slot.
			if instr.local in self.addr_taken_locals:
				llty = self.local_storage_types.get(instr.local)
				if llty is None:
					raise NotImplementedError(
						f"LLVM codegen v1: cannot load from address-taken local '{instr.local}' without a known type"
					)
				alloca_id = self._ensure_local_storage(instr.local, llty)
				dest = self._map_value(instr.dest)
				self.lines.append(f"  {dest} = load {llty}, {llty}* %{alloca_id}")
				self.value_types[dest] = llty
				return
			# SSA pass already assigned a versioned name; treat this as an alias.
			dest = self._map_value(instr.dest)
			block_name = getattr(self, "_current_block_name", None)
			if instr_index is not None and block_name is not None:
				ssa_name = self.ssa.value_for_instr.get((block_name, instr_index))
			else:
				ssa_name = None
			if ssa_name:
				self.aliases[instr.dest] = ssa_name
				self.value_map.setdefault(ssa_name, f"%{ssa_name}")
				if ssa_name in self.value_types:
					self.value_types[dest] = self.value_types[ssa_name]
			else:
				# Fallback to a simple alias on the local name.
				self.aliases[instr.dest] = instr.local
				src_mapped = self._map_value(instr.local)
				if src_mapped in self.value_types:
					self.value_types[dest] = self.value_types[src_mapped]
		elif isinstance(instr, StoreLocal):
			# Address-taken locals are materialized as storage. For them, StoreLocal
			# is a real `store` into the local's alloca slot.
			val = self._map_value(instr.value)
			if instr.local in self.addr_taken_locals:
				llty = self.value_types.get(val)
				if llty is None:
					raise NotImplementedError(
						f"LLVM codegen v1: cannot store into address-taken local '{instr.local}' without a typed value"
					)
				alloca_id = self._ensure_local_storage(instr.local, llty)
				self.lines.append(f"  store {llty} {val}, {llty}* %{alloca_id}")
				return
			# SSA maps locals to versioned names; no IR emission required here.
			block_name = getattr(self, "_current_block_name", None)
			if instr_index is not None and block_name is not None:
				ssa_name = self.ssa.value_for_instr.get((block_name, instr_index))
			else:
				ssa_name = None
			if ssa_name:
				self.aliases[ssa_name] = instr.value
				if val in self.value_types:
					self.value_types[ssa_name] = self.value_types[val]
			if instr.local in self.value_types and val in self.value_types:
				self.value_types[instr.local] = self.value_types[val]
		elif isinstance(instr, AddrOfLocal):
			# Produce a pointer to a stable local storage slot.
			llty = self.local_storage_types.get(instr.local)
			if llty is None:
				raise NotImplementedError(
					f"LLVM codegen v1: cannot take address of local '{instr.local}' without a known type"
				)
			alloca_id = self._ensure_local_storage(instr.local, llty)
			self.aliases[instr.dest] = alloca_id
			dest = self._map_value(instr.dest)
			self.value_types[dest] = f"{llty}*"
		elif isinstance(instr, AddrOfArrayElem):
			array = self._map_value(instr.array)
			index = self._map_value(instr.index)
			elem_llty = self._llvm_type_for_typeid(instr.inner_ty)
			arr_llty = self._llvm_array_type(elem_llty)
			ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
			# Record an alias so later uses resolve to the computed pointer.
			self.aliases[instr.dest] = ptr_tmp[1:] if ptr_tmp.startswith("%") else ptr_tmp
			dest = self._map_value(instr.dest)
			self.value_types[dest] = f"{elem_llty}*"
		elif isinstance(instr, AddrOfField):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: AddrOfField requires a TypeTable")
			base_ptr = self._map_value(instr.base_ptr)
			struct_llty = self._llvm_type_for_typeid(instr.struct_ty)
			want_ptr_ty = f"{struct_llty}*"
			have_ptr_ty = self.value_types.get(base_ptr)
			if have_ptr_ty is not None and have_ptr_ty != want_ptr_ty:
				raise NotImplementedError(
					f"LLVM codegen v1: AddrOfField base pointer type mismatch (have {have_ptr_ty}, expected {want_ptr_ty})"
				)
			field_llty = self._llvm_type_for_typeid(instr.field_ty)
			dest = self._map_value(instr.dest)
			self.lines.append(
				f"  {dest} = getelementptr inbounds {struct_llty}, {want_ptr_ty} {base_ptr}, i32 0, i32 {instr.field_index}"
			)
			self.value_types[dest] = f"{field_llty}*"
		elif isinstance(instr, ConstructStruct):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: ConstructStruct requires a TypeTable")
			struct_def = self.type_table.get(instr.struct_ty)
			if struct_def.kind is not TypeKind.STRUCT:
				raise AssertionError("ConstructStruct with non-STRUCT TypeId (MIR bug)")
			struct_llty = self._llvm_type_for_typeid(instr.struct_ty)
			current = "undef"
			if len(instr.args) != len(struct_def.param_types):
				raise AssertionError("ConstructStruct arg/field length mismatch (MIR bug)")
			if not struct_def.param_types:
				raise NotImplementedError("LLVM codegen v1: empty struct construction not supported yet")
			for idx, (arg, field_ty) in enumerate(zip(instr.args, struct_def.param_types)):
				arg_val = self._map_value(arg)
				field_llty = self._llvm_type_for_typeid(field_ty)
				have = self.value_types.get(arg_val)
				if have is not None and have != field_llty:
					raise NotImplementedError(
						f"LLVM codegen v1: struct field {idx} type mismatch (have {have}, expected {field_llty})"
					)
				is_last = idx == len(struct_def.param_types) - 1
				tmp = self._map_value(instr.dest) if is_last else self._fresh("struct")
				self.lines.append(
					f"  {tmp} = insertvalue {struct_llty} {current}, {field_llty} {arg_val}, {idx}"
				)
				current = tmp
			dest = self._map_value(instr.dest)
			self.value_types[dest] = struct_llty
		elif isinstance(instr, ConstructVariant):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: ConstructVariant requires a TypeTable")
			layout = self._variant_layout(instr.variant_ty)
			variant_llty = layout.llvm_ty
			arm_layout = layout.arms.get(instr.ctor)
			if arm_layout is None:
				raise NotImplementedError(
					f"LLVM codegen v1: unknown variant constructor '{instr.ctor}' for TypeId {instr.variant_ty}"
				)
			# Materialize into a stack slot so we can write into the aligned payload.
			tmp_ptr = self._fresh("variant")
			self.lines.append(f"  {tmp_ptr} = alloca {variant_llty}")
			tag_ptr = self._fresh("tagptr")
			self.lines.append(
				f"  {tag_ptr} = getelementptr inbounds {variant_llty}, {variant_llty}* {tmp_ptr}, i32 0, i32 0"
			)
			self.lines.append(f"  store i8 {arm_layout.tag}, i8* {tag_ptr}")
			if arm_layout.field_storage_lltys:
				payload_words_ptr = self._fresh("payload_words")
				self.lines.append(
					f"  {payload_words_ptr} = getelementptr inbounds {variant_llty}, {variant_llty}* {tmp_ptr}, i32 0, i32 2"
				)
				payload_i8 = self._fresh("payload_i8")
				self.lines.append(
					f"  {payload_i8} = bitcast [{layout.payload_words} x i64]* {payload_words_ptr} to i8*"
				)
				payload_struct_ptr = self._fresh("payload_struct")
				self.lines.append(
					f"  {payload_struct_ptr} = bitcast i8* {payload_i8} to {arm_layout.payload_struct_llty}*"
				)
				for idx, (arg, want_llty, store_llty) in enumerate(
					zip(instr.args, arm_layout.field_lltys, arm_layout.field_storage_lltys)
				):
					arg_val = self._map_value(arg)
					have = self.value_types.get(arg_val)
					if have is not None and have != want_llty:
						raise NotImplementedError(
							f"LLVM codegen v1: ConstructVariant field {idx} type mismatch (have {have}, expected {want_llty})"
						)
					field_ptr = self._fresh("fieldptr")
					self.lines.append(
						f"  {field_ptr} = getelementptr inbounds {arm_layout.payload_struct_llty}, {arm_layout.payload_struct_llty}* {payload_struct_ptr}, i32 0, i32 {idx}"
					)
					if store_llty == "i8" and want_llty == "i1":
						ext = self._fresh("bool8")
						self.lines.append(f"  {ext} = zext i1 {arg_val} to i8")
						self.lines.append(f"  store i8 {ext}, i8* {field_ptr}")
					else:
						self.lines.append(f"  store {store_llty} {arg_val}, {store_llty}* {field_ptr}")
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = load {variant_llty}, {variant_llty}* {tmp_ptr}")
			self.value_types[dest] = variant_llty
		elif isinstance(instr, VariantTag):
			layout = self._variant_layout(instr.variant_ty)
			variant_llty = layout.llvm_ty
			val = self._map_value(instr.variant)
			have = self.value_types.get(val)
			if have is not None and have != variant_llty:
				raise NotImplementedError(
					f"LLVM codegen v1: VariantTag value type mismatch (have {have}, expected {variant_llty})"
				)
			raw = self._fresh("tag8")
			self.lines.append(f"  {raw} = extractvalue {variant_llty} {val}, 0")
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = zext i8 {raw} to i64")
			self.value_types[dest] = "i64"
		elif isinstance(instr, VariantGetField):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: VariantGetField requires a TypeTable")
			layout = self._variant_layout(instr.variant_ty)
			variant_llty = layout.llvm_ty
			arm_layout = layout.arms.get(instr.ctor)
			if arm_layout is None or not arm_layout.payload_struct_llty:
				raise NotImplementedError(
					f"LLVM codegen v1: VariantGetField unsupported ctor '{instr.ctor}' for TypeId {instr.variant_ty}"
				)
			val = self._map_value(instr.variant)
			have = self.value_types.get(val)
			if have is not None and have != variant_llty:
				raise NotImplementedError(
					f"LLVM codegen v1: VariantGetField value type mismatch (have {have}, expected {variant_llty})"
				)
			tmp_ptr = self._fresh("variant")
			self.lines.append(f"  {tmp_ptr} = alloca {variant_llty}")
			self.lines.append(f"  store {variant_llty} {val}, {variant_llty}* {tmp_ptr}")
			payload_words_ptr = self._fresh("payload_words")
			self.lines.append(
				f"  {payload_words_ptr} = getelementptr inbounds {variant_llty}, {variant_llty}* {tmp_ptr}, i32 0, i32 2"
			)
			payload_i8 = self._fresh("payload_i8")
			self.lines.append(
				f"  {payload_i8} = bitcast [{layout.payload_words} x i64]* {payload_words_ptr} to i8*"
			)
			payload_struct_ptr = self._fresh("payload_struct")
			self.lines.append(
				f"  {payload_struct_ptr} = bitcast i8* {payload_i8} to {arm_layout.payload_struct_llty}*"
			)
			field_ptr = self._fresh("fieldptr")
			self.lines.append(
				f"  {field_ptr} = getelementptr inbounds {arm_layout.payload_struct_llty}, {arm_layout.payload_struct_llty}* {payload_struct_ptr}, i32 0, i32 {instr.field_index}"
			)
			store_llty = arm_layout.field_storage_lltys[instr.field_index]
			want_llty = arm_layout.field_lltys[instr.field_index]
			dest = self._map_value(instr.dest)
			if store_llty == "i8" and want_llty == "i1":
				raw = self._fresh("field8")
				self.lines.append(f"  {raw} = load i8, i8* {field_ptr}")
				self.lines.append(f"  {dest} = icmp ne i8 {raw}, 0")
				self.value_types[dest] = "i1"
			else:
				# For non-bool payload fields, storage and value types are identical.
				self.lines.append(f"  {dest} = load {want_llty}, {want_llty}* {field_ptr}")
				self.value_types[dest] = want_llty
		elif isinstance(instr, StructGetField):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: StructGetField requires a TypeTable")
			struct_llty = self._llvm_type_for_typeid(instr.struct_ty)
			subject = self._map_value(instr.subject)
			have_struct = self.value_types.get(subject)
			if have_struct is not None and have_struct != struct_llty:
				raise NotImplementedError(
					f"LLVM codegen v1: StructGetField subject type mismatch (have {have_struct}, expected {struct_llty})"
				)
			field_llty = self._llvm_type_for_typeid(instr.field_ty)
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = extractvalue {struct_llty} {subject}, {instr.field_index}")
			self.value_types[dest] = field_llty
		elif isinstance(instr, LoadRef):
			ptr = self._map_value(instr.ptr)
			llty = self._llvm_type_for_typeid(instr.inner_ty)
			ptr_ty = f"{llty}*"
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = load {llty}, {ptr_ty} {ptr}")
			self.value_types[dest] = llty
		elif isinstance(instr, StoreRef):
			ptr = self._map_value(instr.ptr)
			llty = self._llvm_type_for_typeid(instr.inner_ty)
			ptr_ty = f"{llty}*"
			val = self._map_value(instr.value)
			have = self.value_types.get(val)
			if have is not None and have != llty:
				raise NotImplementedError(
					f"LLVM codegen v1: StoreRef value type mismatch (have {have}, expected {llty})"
				)
			self.lines.append(f"  store {llty} {val}, {ptr_ty} {ptr}")
		elif isinstance(instr, BinaryOpInstr):
			self._lower_binary(instr)
		elif isinstance(instr, Call):
			self._lower_call(instr)
		elif isinstance(instr, ResultIsErr):
			dest = self._map_value(instr.dest)
			res = self._map_value(instr.result)
			fnres_llty = self.value_types.get(res)
			if fnres_llty is None:
				raise NotImplementedError("LLVM codegen v1: ResultIsErr requires a typed FnResult value")
			self.lines.append(f"  {dest} = extractvalue {fnres_llty} {res}, 0")
			self.value_types[dest] = "i1"
		elif isinstance(instr, ResultOk):
			dest = self._map_value(instr.dest)
			res = self._map_value(instr.result)
			fnres_llty = self.value_types.get(res)
			if fnres_llty is None:
				raise NotImplementedError("LLVM codegen v1: ResultOk requires a typed FnResult value")
			ok_llty = self.module._fnresult_ok_llty_by_type.get(fnres_llty)
			if ok_llty is None:
				raise NotImplementedError(f"LLVM codegen v1: unknown FnResult layout for {fnres_llty}")
			self.lines.append(f"  {dest} = extractvalue {fnres_llty} {res}, 1")
			self.value_types[dest] = ok_llty
		elif isinstance(instr, ResultErr):
			dest = self._map_value(instr.dest)
			res = self._map_value(instr.result)
			fnres_llty = self.value_types.get(res)
			if fnres_llty is None:
				raise NotImplementedError("LLVM codegen v1: ResultErr requires a typed FnResult value")
			self.lines.append(f"  {dest} = extractvalue {fnres_llty} {res}, 2")
			self.value_types[dest] = DRIFT_ERROR_PTR
		elif isinstance(instr, ConstructResultOk):
			if not self.fn_info.declared_can_throw:
				raise NotImplementedError(
					f"LLVM codegen v1: FnResult construction in non-can-throw function {self.fn_info.name} is not allowed"
				)
			dest = self._map_value(instr.dest)
			ok_llty, fnres_llty = self._fnresult_types_for_current_fn()
			self.value_types[dest] = fnres_llty
			if instr.value is None:
				# Surface `return;` in a can-throw `returns Void` function: there is no
				# user-level ok payload. We synthesize a dummy i8 slot value for the
				# internal FnResult ok field.
				if ok_llty != "i8":
					raise NotImplementedError(
						f"LLVM codegen v1: ConstructResultOk(None) is only valid for Void ok payloads; "
						f"function {self.fn_info.name} has ok payload type {ok_llty}"
					)
				val = "0"
			else:
				val = self._map_value(instr.value)
				val_ty = self.value_types.get(val)
				if val_ty is not None and val_ty != ok_llty:
					raise NotImplementedError(
						f"LLVM codegen v1: ok payload type mismatch for ConstructResultOk in {self.fn_info.name}: "
						f"have {val_ty}, expected {ok_llty}"
					)
			tmp0 = self._fresh("ok0")
			tmp1 = self._fresh("ok1")
			err_zero = f"{DRIFT_ERROR_PTR} null"
			self.lines.append(f"  {tmp0} = insertvalue {fnres_llty} undef, i1 0, 0")
			self.lines.append(f"  {tmp1} = insertvalue {fnres_llty} {tmp0}, {ok_llty} {val}, 1")
			self.lines.append(f"  {dest} = insertvalue {fnres_llty} {tmp1}, {err_zero}, 2")
		elif isinstance(instr, ConstructResultErr):
			if not self.fn_info.declared_can_throw:
				raise NotImplementedError(
					f"LLVM codegen v1: FnResult.Err construction in non-can-throw function {self.fn_info.name} is not allowed"
				)
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			ok_llty, fnres_llty = self._fnresult_types_for_current_fn()
			self.value_types[dest] = fnres_llty
			tmp0 = self._fresh("err0")
			tmp1 = self._fresh("err1")
			ok_zero = self._zero_value_for_ok(ok_llty)
			self.lines.append(f"  {tmp0} = insertvalue {fnres_llty} undef, i1 1, 0")
			self.lines.append(f"  {tmp1} = insertvalue {fnres_llty} {tmp0}, {ok_zero}, 1")
			self.lines.append(f"  {dest} = insertvalue {fnres_llty} {tmp1}, {DRIFT_ERROR_PTR} {err_val}, 2")
		elif isinstance(instr, ConstructDV):
			dest = self._map_value(instr.dest)
			self.value_types[dest] = DRIFT_DV_TYPE
			if not instr.args:
				self.module.needs_dv_runtime = True
				# DV_MISSING is a runtime-defined constant (tag=0); call helper to avoid
				# baking layout assumptions here.
				self.lines.append(f"  {dest} = call {DRIFT_DV_TYPE} @drift_dv_missing()")
				return
			if len(instr.args) != 1:
				raise NotImplementedError(
					"LLVM codegen v1: DiagnosticValue constructors support at most one argument (Int/Bool/String)"
				)
			self.module.needs_dv_runtime = True
			arg_val = self._map_value(instr.args[0])
			arg_ty = self.value_types.get(arg_val)
			if arg_ty == "i64":
				self.lines.append(f"  {dest} = call {DRIFT_DV_TYPE} @drift_dv_int(i64 {arg_val})")
				return
			if arg_ty == "i1":
				self.lines.append(f"  {dest} = call {DRIFT_DV_TYPE} @drift_dv_bool(i1 {arg_val})")
				return
			if arg_ty == DRIFT_STRING_TYPE:
				self.lines.append(f"  {dest} = call {DRIFT_DV_TYPE} @drift_dv_string({DRIFT_STRING_TYPE} {arg_val})")
				return
			raise NotImplementedError(
				f"LLVM codegen v1: ConstructDV arg type {arg_ty} not supported (expected Int/Bool/String)"
			)
		elif isinstance(instr, ConstructError):
			dest = self._map_value(instr.dest)
			code = self._map_value(instr.code)
			event_fqn = self._map_value(instr.event_fqn)
			payload = self._map_value(instr.payload) if instr.payload is not None else None
			attr_key = self._map_value(instr.attr_key) if instr.attr_key is not None else None
			self.value_types[dest] = DRIFT_ERROR_PTR
			self.module.needs_error_runtime = True
			code_ty = self.value_types.get(code)
			if code_ty != "i64":
				raise NotImplementedError(
					f"LLVM codegen v1: error code must be Int (i64), got {code_ty}"
				)
			event_fqn_ty = self.value_types.get(event_fqn)
			if event_fqn_ty != DRIFT_STRING_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: event_fqn must be String ({DRIFT_STRING_TYPE}), got {event_fqn_ty}"
				)
			if payload is None or attr_key is None:
				self.lines.append(
					f"  {dest} = call {DRIFT_ERROR_PTR} @drift_error_new(i64 {code}, {DRIFT_STRING_TYPE} {event_fqn})"
				)
			else:
				# Attach payload via runtime helper; payload is expected to be a DiagnosticValue.
				self.lines.append(
					f"  {dest} = call {DRIFT_ERROR_PTR} @drift_error_new_with_payload(i64 {code}, {DRIFT_STRING_TYPE} {event_fqn}, {DRIFT_STRING_TYPE} {attr_key}, {DRIFT_DV_TYPE} {payload})"
				)
		elif isinstance(instr, ErrorAttrsGetDV):
			self.module.needs_dv_runtime = True
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			key_val = self._map_value(instr.key)
			self.value_types[dest] = DRIFT_DV_TYPE
			tmp_ptr = self._fresh("dvptr")
			self.lines.append(f"  {tmp_ptr} = alloca {DRIFT_DV_TYPE}")
			self.lines.append(
				f"  call void @__exc_attrs_get_dv({DRIFT_DV_TYPE}* {tmp_ptr}, {DRIFT_ERROR_PTR} {err_val}, {DRIFT_STRING_TYPE} {key_val})"
			)
			self.lines.append(f"  {dest} = load {DRIFT_DV_TYPE}, {DRIFT_DV_TYPE}* {tmp_ptr}")
		elif isinstance(instr, ErrorAddAttrDV):
			self.module.needs_error_runtime = True
			self.module.needs_dv_runtime = True
			err_val = self._map_value(instr.error)
			key_val = self._map_value(instr.key)
			val = self._map_value(instr.value)
			tmp_ptr = self._fresh("dvptr")
			self.lines.append(f"  {tmp_ptr} = alloca {DRIFT_DV_TYPE}")
			self.lines.append(f"  store {DRIFT_DV_TYPE} {val}, {DRIFT_DV_TYPE}* {tmp_ptr}")
			self.lines.append(
				f"  call void @drift_error_add_attr_dv({DRIFT_ERROR_PTR} {err_val}, {DRIFT_STRING_TYPE} {key_val}, {DRIFT_DV_TYPE}* {tmp_ptr})"
			)
		elif isinstance(instr, ErrorEvent):
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			err_ty = self.value_types.get(err_val)
			if err_ty is None:
				# Unreachable dispatch paths may still reference the synthetic try
				# error slot; default it to the canonical error pointer type.
				err_ty = DRIFT_ERROR_PTR
				self.value_types[err_val] = err_ty
			if err_ty != DRIFT_ERROR_PTR:
				raise NotImplementedError(
					f"LLVM codegen v1: ErrorEvent expects {DRIFT_ERROR_PTR}, got {err_ty}"
				)
			loaded = self._fresh("err_val")
			self.lines.append(f"  {loaded} = load {DRIFT_ERROR_TYPE}, {DRIFT_ERROR_PTR} {err_val}")
			self.lines.append(f"  {dest} = extractvalue {DRIFT_ERROR_TYPE} {loaded}, 0")
			self.value_types[dest] = "i64"
		elif isinstance(instr, OptionalIsSome):
			opt_val = self._map_value(instr.opt)
			opt_ty = self.value_types.get(opt_val)
			if opt_ty not in (DRIFT_OPT_INT_TYPE, DRIFT_OPT_BOOL_TYPE, DRIFT_OPT_STRING_TYPE):
				raise NotImplementedError(
					f"LLVM codegen v1: OptionalIsSome requires Optional<Int|Bool|String>, got {opt_ty}"
				)
			tmp = self._fresh("opt_tag")
			self.lines.append(f"  {tmp} = extractvalue {opt_ty} {opt_val}, 0")
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = icmp ne i8 {tmp}, 0")
			self.value_types[dest] = "i1"
		elif isinstance(instr, OptionalValue):
			opt_val = self._map_value(instr.opt)
			opt_ty = self.value_types.get(opt_val)
			if opt_ty == DRIFT_OPT_INT_TYPE:
				dest = self._map_value(instr.dest)
				self.lines.append(f"  {dest} = extractvalue {opt_ty} {opt_val}, 1")
				self.value_types[dest] = "i64"
				return
			elif opt_ty == DRIFT_OPT_BOOL_TYPE:
				dest = self._map_value(instr.dest)
				raw = self._fresh("opt_val")
				self.lines.append(f"  {raw} = extractvalue {opt_ty} {opt_val}, 1")
				self.lines.append(f"  {dest} = icmp ne i8 {raw}, 0")
				self.value_types[dest] = "i1"
				return
			elif opt_ty == DRIFT_OPT_STRING_TYPE:
				dest = self._map_value(instr.dest)
				self.lines.append(f"  {dest} = extractvalue {opt_ty} {opt_val}, 1")
				self.value_types[dest] = DRIFT_STRING_TYPE
				return
			raise NotImplementedError(
				f"LLVM codegen v1: OptionalValue requires Optional<Int|Bool|String>, got {opt_ty}"
			)
		elif isinstance(instr, (DVAsInt, DVAsBool, DVAsString)):
			self.module.needs_dv_runtime = True
			dest = self._map_value(instr.dest)
			dv_val = self._map_value(instr.dv)
			tmp_ptr = self._fresh("dvarg")
			self.lines.append(f"  {tmp_ptr} = alloca {DRIFT_DV_TYPE}")
			self.lines.append(f"  store {DRIFT_DV_TYPE} {dv_val}, {DRIFT_DV_TYPE}* {tmp_ptr}")
			if isinstance(instr, DVAsInt):
				self.value_types[dest] = DRIFT_OPT_INT_TYPE
				self.lines.append(f"  {dest} = call {DRIFT_OPT_INT_TYPE} @drift_dv_as_int({DRIFT_DV_TYPE}* {tmp_ptr})")
			elif isinstance(instr, DVAsBool):
				self.value_types[dest] = DRIFT_OPT_BOOL_TYPE
				self.lines.append(f"  {dest} = call {DRIFT_OPT_BOOL_TYPE} @drift_dv_as_bool({DRIFT_DV_TYPE}* {tmp_ptr})")
			else:
				self.value_types[dest] = DRIFT_OPT_STRING_TYPE
				self.lines.append(
					f"  {dest} = call {DRIFT_OPT_STRING_TYPE} @drift_dv_as_string({DRIFT_DV_TYPE}* {tmp_ptr})"
				)
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
		# Allow intrinsic console trio even without FnInfo (e.g., prelude).
		if callee_info is None and instr.fn in {"print", "println", "eprintln"}:
			if len(instr.args) != 1:
				raise NotImplementedError(f"LLVM codegen v1: {instr.fn} expects exactly one argument")
			arg_val = self._map_value(instr.args[0])
			self.value_types.setdefault(arg_val, DRIFT_STRING_TYPE)
			runtime_name = {
				"print": "drift_console_write",
				"println": "drift_console_writeln",
				"eprintln": "drift_console_eprintln",
			}[instr.fn]
			self.module.needs_console_runtime = True
			self.lines.append(f"  call void @{runtime_name}({DRIFT_STRING_TYPE} {arg_val})")
			if dest:
				raise NotImplementedError("console intrinsics return Void; result cannot be captured")
			return
		if callee_info is None:
			raise NotImplementedError(f"LLVM codegen v1: missing FnInfo for callee {instr.fn}")

		# Prelude console trio: treat lang.core::print/println/eprintln as runtime intrinsics.
		if callee_info.signature and callee_info.signature.method_name in {"print", "println", "eprintln"}:
			if getattr(callee_info.signature, "module", None) == "lang.core":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: {instr.fn} expects exactly one argument")
				arg_val = self._map_value(instr.args[0])
				self.value_types.setdefault(arg_val, DRIFT_STRING_TYPE)
				runtime_name = {
					"print": "drift_console_write",
					"println": "drift_console_writeln",
					"eprintln": "drift_console_eprintln",
				}[callee_info.signature.method_name]
				self.module.needs_console_runtime = True
				self.lines.append(f"  call void @{runtime_name}({DRIFT_STRING_TYPE} {arg_val})")
				if dest:
					raise NotImplementedError("console intrinsics return Void; result cannot be captured")
				return

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

		target_sym = instr.fn
		is_exported_entry = bool(
			callee_info.signature is not None and getattr(callee_info.signature, "is_exported_entrypoint", False)
		)
		caller_mod = (
			getattr(self.fn_info.signature, "module", None) if self.fn_info.signature is not None else None
		) or _module_id_from_symbol(self.func.name)
		callee_mod = (
			getattr(callee_info.signature, "module", None) if callee_info.signature is not None else None
		) or _module_id_from_symbol(instr.fn)
		# Treat unknown caller/callee module as cross-module to avoid accidentally
		# targeting private `__impl` symbols from entrypoints like `main`.
		is_cross_module = is_exported_entry and (caller_mod is None or callee_mod is None or caller_mod != callee_mod)
		# For same-module calls to exported entrypoints, call the private `__impl`
		# body so the internal calling convention (`returns T` for nothrow fns) is
		# preserved. Cross-module calls use the public wrapper symbol.
		if is_exported_entry and not is_cross_module:
			target_sym = self.export_impl_map.get(instr.fn, instr.fn)
		# Apply any final driver-level renames (e.g. argv wrapper) as a backstop.
		target_sym = self.rename_map.get(target_sym, target_sym)

		if is_exported_entry and is_cross_module and not callee_info.declared_can_throw:
			# ABI boundary wrapper returns FnResult even for nothrow entrypoints.
			# The surface language expects a plain `T`. We must not silently treat an
			# error as a `T`, so unwrap via a helper that traps on `is_err = true`.
			ret_tid = None
			if callee_info.signature and callee_info.signature.return_type_id is not None:
				ret_tid = callee_info.signature.return_type_id
			# Void-returning exported entrypoints still return FnResult<Void, Error*>
			# at the boundary, but the call result must not be captured in MIR.
			is_void_ret = ret_tid is not None and self._is_void_typeid(ret_tid)
			if is_void_ret and dest is not None:
				raise NotImplementedError("LLVM codegen v1: cannot capture result of a void call")
			if self.type_table is None or ret_tid is None:
				raise NotImplementedError(
					f"LLVM codegen v1: exported entrypoint call requires TypeTable and return type (callee {instr.fn})"
				)
			ok_llty, ok_key = self._llvm_ok_type_for_typeid(ret_tid)
			fnres_llty = self.module.fnresult_type(ok_key, ok_llty)
			tmp = self._fresh("entryres")
			self.lines.append(f"  {tmp} = call {fnres_llty} {_llvm_fn_sym(target_sym)}({args})")
			unwrap_fn = self.module.fnresult_unwrap_ok_or_trap(ok_key, fnres_llty, ok_llty)
			if dest is None:
				# Statement position: still validate the carrier (trap on error),
				# then discard the Ok payload.
				self.lines.append(f"  call {ok_llty} {unwrap_fn}({fnres_llty} {tmp})")
				return
			self.lines.append(f"  {dest} = call {ok_llty} {unwrap_fn}({fnres_llty} {tmp})")
			self.value_types[dest] = ok_llty
			return

		if callee_info.declared_can_throw:
			_, fnres_llty = self._fnresult_types_for_fn(callee_info)
			if dest is None:
				raise AssertionError("can-throw calls must preserve their FnResult value (MIR bug)")
			self.lines.append(f"  {dest} = call {fnres_llty} {_llvm_fn_sym(target_sym)}({args})")
			self.value_types[dest] = fnres_llty
		else:
			ret_tid = None
			if callee_info.signature and callee_info.signature.return_type_id is not None:
				ret_tid = callee_info.signature.return_type_id
			is_void_ret = ret_tid is not None and self._is_void_typeid(ret_tid)
			ret_ty = "void" if is_void_ret else "i64"
			if ret_tid is not None and self.type_table is not None and not is_void_ret:
				ret_ty = self._llvm_type_for_typeid(ret_tid)
			if dest is None:
				self.lines.append(f"  call {ret_ty} {_llvm_fn_sym(target_sym)}({args})")
			else:
				if ret_ty == "void":
					raise NotImplementedError("LLVM codegen v1: cannot capture result of a void call")
				self.lines.append(f"  {dest} = call {ret_ty} {_llvm_fn_sym(target_sym)}({args})")
				self.value_types[dest] = ret_ty

	def _lower_term(self, term: object) -> None:
		if isinstance(term, Goto):
			self.lines.append(f"  br label %{term.target}")
			return

		if isinstance(term, IfTerminator):
			cond = self._map_value(term.cond)
			cond_ty = self.value_types.get(cond, "i1")
			if cond_ty != "i1":
				raise NotImplementedError("LLVM codegen v1: branch condition must be bool (i1)")
			self.lines.append(
				f"  br i1 {cond}, label %{term.then_target}, label %{term.else_target}"
			)
			return

		if isinstance(term, Return):
			if self.fn_info.declared_can_throw:
				# Can-throw functions always return the internal `FnResult<ok, Error>`
				# carrier type, even when the surface ok type is `Void`.
				if term.value is None:
					raise AssertionError("can-throw function reached a bare return (MIR bug)")
				val = self._map_value(term.value)
				fnres_llty = self._fnresult_type_for_current_fn()
				self.lines.append(f"  ret {fnres_llty} {val}")
				return

			is_void = self._is_void_return()
			if is_void and term.value is not None:
				raise AssertionError("Void function must not return a value (MIR bug)")
			if not is_void and term.value is None:
				raise AssertionError("non-void bare return reached LLVM codegen (MIR bug)")
			if is_void:
				self.lines.append("  ret void")
				return

			val = self._map_value(term.value)
			ty = self.value_types.get(val)
			if ty == DRIFT_STRING_TYPE:
				self.lines.append(f"  ret {DRIFT_STRING_TYPE} {val}")
			elif ty in ("i64", DRIFT_SIZE_TYPE):
				self.lines.append(f"  ret i64 {val}")
			elif ty == "double":
				self.lines.append(f"  ret double {val}")
			elif ty is not None and (ty == "ptr" or ty.endswith("*")):
				# Non-throwing functions may return references (`&T`), lowered as
				# typed pointers (`T*`) in v1.
				self.lines.append(f"  ret {ty} {val}")
			elif ty is not None and ty.startswith("%Variant_"):
				# Variants are compiler-private aggregates in v1, but they are still
				# valid surface return types (e.g. `Optional<Int>`). We return them by
				# value using their named struct type.
				self.lines.append(f"  ret {ty} {val}")
			elif ty is not None and ty.startswith("%Struct_"):
				# User-defined structs are returned by value in v1.
				self.lines.append(f"  ret {ty} {val}")
			else:
				raise NotImplementedError(
					f"LLVM codegen v1: non-can-throw return must be Int, Float, String, &T, Struct, or Variant, got {ty}"
				)
			return

		if isinstance(term, Unreachable):
			self.lines.append("  unreachable")
			return

		raise NotImplementedError(f"LLVM codegen v1: unsupported terminator {type(term).__name__}")

	def _return_llvm_type(self) -> str:
		# v1 supports Int/Float/Bool/String/Void, user-defined Structs, compiler-private
		# Variants (e.g. Optional<T>), and can-throw FnResult<ok, Error> return shapes
		# (ok ∈ {Int, String, Void-like, Ref<T>}).
		if self.fn_info.declared_can_throw:
			return self._fnresult_type_for_current_fn()
		if self._is_void_return():
			return "void"
		rt_id = None
		if self.fn_info.signature and self.fn_info.signature.return_type_id is not None:
			rt_id = self.fn_info.signature.return_type_id
		if rt_id is None:
			return "i64"
		# Use the same TypeTable-based mapping as parameters so ref returns are
		# handled consistently (`&T` -> `T*`).
		try:
			return self._llvm_type_for_typeid(rt_id, allow_void_ok=False)
		except NotImplementedError:
			# Legacy fallback: treat unknown surface types as Int.
			return "i64"

	def _is_void_return(self) -> bool:
		if self.fn_info.signature and self.fn_info.signature.return_type_id is not None:
			return self._is_void_typeid(self.fn_info.signature.return_type_id)
		return False

	def _is_void_typeid(self, ty_id: TypeId) -> bool:
		if self.type_table is not None:
			return self.type_table.is_void(ty_id)
		return self.void_type_id is not None and ty_id == self.void_type_id

	def _size_align_typeid(self, ty_id: TypeId) -> tuple[int, int]:
		"""
		Best-effort size/alignment model for the compiler-private variant payload.

		This is not a stable external ABI; it only needs to be self-consistent
		within the emitted LLVM module. We keep it simple and assume max alignment
		8 for all supported field types in v1.
		"""
		if ty_id in self._size_align_cache:
			return self._size_align_cache[ty_id]
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for variant lowering")
		td = self.type_table.get(ty_id)
		if td.kind is TypeKind.SCALAR:
			if td.name in ("Int", "Uint", "Float"):
				out = (8, 8)
			elif td.name == "Bool":
				out = (1, 1)
			elif td.name == "String":
				out = (16, 8)  # %DriftString = { i64, i8* }
			else:
				out = (8, 8)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind in (TypeKind.REF, TypeKind.ERROR):
			out = (8, 8)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind is TypeKind.ARRAY:
			# Current array value lowering is a 3-word header (len, cap, data ptr).
			out = (24, 8)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind is TypeKind.STRUCT:
			offset = 0
			max_align = 1
			for fty in td.param_types:
				fsz, fal = self._size_align_typeid(fty)
				if fal > 1:
					offset = ((offset + fal - 1) // fal) * fal
				offset += fsz
				max_align = max(max_align, fal)
			if max_align > 1:
				offset = ((offset + max_align - 1) // max_align) * max_align
			out = (offset, max_align)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind is TypeKind.VARIANT:
			layout = self._variant_layout(ty_id)
			out = (8 + layout.payload_words * 8, 8)
			self._size_align_cache[ty_id] = out
			return out
		out = (8, 8)
		self._size_align_cache[ty_id] = out
		return out

	def _variant_layout(self, ty_id: TypeId) -> _VariantLayout:
		"""
		Compute and cache the variant layout for a concrete TypeId.

		The variant value type is declared as:
		  %Variant_<id> = type { i8, [7 x i8], [payload_words x i64] }

		Payload packing per constructor uses a literal struct type containing the
		constructor's field storage types (Bool stored as i8).
		"""
		if ty_id in self._variant_layouts:
			return self._variant_layouts[ty_id]
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for variant lowering")
		inst = self.type_table.get_variant_instance(ty_id)
		if inst is None:
			raise NotImplementedError(f"LLVM codegen v1: missing variant instance for TypeId {ty_id}")
		max_payload_size = 0
		arms: Dict[str, _VariantArmLayout] = {}
		for arm in inst.arms:
			field_lltys: list[str] = []
			field_storage_lltys: list[str] = []
			offset = 0
			max_align = 1
			for fty in arm.field_types:
				llty = self._llvm_type_for_typeid(fty)
				field_lltys.append(llty)
				is_bool = self.type_table.get(fty).kind is TypeKind.SCALAR and self.type_table.get(fty).name == "Bool"
				st_llty = "i8" if is_bool else llty
				field_storage_lltys.append(st_llty)
				sz, al = self._size_align_typeid(fty)
				if al > 1:
					offset = ((offset + al - 1) // al) * al
				offset += sz
				max_align = max(max_align, al)
			if max_align > 1:
				offset = ((offset + max_align - 1) // max_align) * max_align
			max_payload_size = max(max_payload_size, offset)
			payload_struct_llty = ""
			if field_storage_lltys:
				payload_struct_llty = "{ " + ", ".join(field_storage_lltys) + " }"
			arms[arm.name] = _VariantArmLayout(
				tag=arm.tag,
				field_lltys=field_lltys,
				field_storage_lltys=field_storage_lltys,
				payload_struct_llty=payload_struct_llty,
			)
		payload_words = max(1, (max_payload_size + 7) // 8)
		llvm_ty = self.module.ensure_variant_type(ty_id, payload_words=payload_words)
		layout = _VariantLayout(llvm_ty=llvm_ty, payload_words=payload_words, arms=arms)
		self._variant_layouts[ty_id] = layout
		return layout

	def _llvm_type_for_typeid(self, ty_id: TypeId, *, allow_void_ok: bool = False) -> str:
		"""
		Map a TypeId to an LLVM type string for parameters/arguments.

		v1 supports Int (i64), String (%DriftString), and Array<T> (by value).
		"""
		if self.type_table is not None:
			if self.type_table.is_void(ty_id):
				if allow_void_ok:
					# Void ok-payloads are represented as an unused i8 slot.
					return "i8"
				raise NotImplementedError("LLVM codegen v1: Void is not a valid parameter type")
			td = self.type_table.get(ty_id)
			if td.kind is TypeKind.ARRAY and td.param_types:
				elem_llty = self._llvm_type_for_typeid(td.param_types[0])
				return self._llvm_array_type(elem_llty)
			if td.kind is TypeKind.SCALAR and td.name in {"Int", "Uint"}:
				return "i64"
			if td.kind is TypeKind.SCALAR and td.name == "Bool":
				return "i1"
			if td.kind is TypeKind.SCALAR and td.name == "Float":
				return "double"
			if td.kind is TypeKind.SCALAR and td.name == "String":
				return DRIFT_STRING_TYPE
			if td.kind is TypeKind.REF:
				inner_llty = "i8"
				if td.param_types:
					inner_llty = self._llvm_type_for_typeid(td.param_types[0])
				return f"{inner_llty}*"
			if td.kind is TypeKind.STRUCT:
				return self.module.ensure_struct_type(ty_id, type_table=self.type_table, map_type=self._llvm_type_for_typeid)
			if td.kind is TypeKind.VARIANT:
				# Concrete variants lower to a named LLVM struct type that contains a
				# tag byte and an aligned payload buffer.
				return self._variant_layout(ty_id).llvm_ty
			if td.kind is TypeKind.DIAGNOSTICVALUE:
				return DRIFT_DV_TYPE
			if td.kind is TypeKind.ERROR:
				return DRIFT_ERROR_PTR
			if td.kind is TypeKind.OPTIONAL and td.param_types:
				inner = td.param_types[0]
				inner_def = self.type_table.get(inner)
				if inner_def.kind is TypeKind.SCALAR and inner_def.name == "Int":
					return DRIFT_OPT_INT_TYPE
				if inner_def.kind is TypeKind.SCALAR and inner_def.name == "Bool":
					return DRIFT_OPT_BOOL_TYPE
				if inner_def.kind is TypeKind.SCALAR and inner_def.name == "String":
					return DRIFT_OPT_STRING_TYPE
				raise NotImplementedError(
					f"LLVM codegen v1: Optional<{inner_def.name}> not supported (function {self.func.name})"
				)
			if self.int_type_id is not None and ty_id == self.int_type_id:
				return "i64"
			if self.float_type_id is not None and ty_id == self.float_type_id:
				return "double"
			if self.string_type_id is not None and ty_id == self.string_type_id:
				return DRIFT_STRING_TYPE
		raise NotImplementedError(
			f"LLVM codegen v1: unsupported param type id {ty_id!r} for function {self.func.name}"
		)

	def _type_key(self, ty_id: TypeId) -> str:
		"""Build a stable key string for a TypeId (used for FnResult naming/diagnostics)."""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for FnResult lowering")
		td = self.type_table.get(ty_id)
		if td.kind is TypeKind.SCALAR:
			return td.name
		if td.kind is TypeKind.VOID:
			return "Void"
		if td.kind is TypeKind.ARRAY and td.param_types:
			elem_key = self._type_key(td.param_types[0])
			return f"Array_{elem_key}"
		if td.kind is TypeKind.REF and td.param_types:
			inner_key = self._type_key(td.param_types[0])
			prefix = "RefMut" if td.ref_mut else "Ref"
			return f"{prefix}_{inner_key}"
		if td.kind is TypeKind.STRUCT:
			safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in td.name)
			return f"Struct_{safe}"
		if td.kind is TypeKind.VARIANT:
			# Variant TypeIds are unique per instantiation; include the TypeId to
			# avoid collisions between different instantiations with the same name.
			return f"Variant_{ty_id}"
		return f"{td.kind.name}"

	def _llvm_ok_type_for_typeid(self, ty_id: TypeId) -> tuple[str, str]:
		"""
		Map an Ok TypeId to (ok_llty, ok_key) for FnResult payloads.

		Supported in v1: Int -> i64, String -> %DriftString, Void -> i8, Ref<T> -> T*,
		and concrete Struct/Variant values by-value.
		Other kinds are rejected with a clear diagnostic.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for FnResult lowering")
		td = self.type_table.get(ty_id)
		key = self._type_key(ty_id)
		if td.kind is TypeKind.SCALAR and td.name == "Int":
			return "i64", key
		if td.kind is TypeKind.SCALAR and td.name == "String":
			return DRIFT_STRING_TYPE, key
		if td.kind is TypeKind.VOID:
			return "i8", key
		if td.kind is TypeKind.REF:
			inner_llty = "i8"
			if td.param_types:
				inner_llty = self._llvm_type_for_typeid(td.param_types[0])
			return f"{inner_llty}*", key
		if td.kind in (TypeKind.STRUCT, TypeKind.VARIANT):
			return self._llvm_type_for_typeid(ty_id), key
		supported = "Int, String, Void, Ref<T>, Struct, Variant"
		raise NotImplementedError(
			f"LLVM codegen v1: FnResult ok type {key} is not supported yet; supported ok payloads: {supported}"
		)

	def _llvm_ok_type_for_sig(self, sig: object) -> tuple[str, str]:
		"""
		Return (ok_llty, ok_key) for a surface signature's return type.

		Export wrappers use this to compute the ABI-boundary FnResult carrier type:
		exported entrypoints always return FnResult<Ok, Error*> at the module
		boundary, even if the function body is not can-throw.
		"""
		ret_tid = getattr(sig, "return_type_id", None)
		if ret_tid is None:
			raise NotImplementedError(
				f"LLVM codegen v1: exported entrypoint wrapper requires a return type (function {self.func.name})"
			)
		return self._llvm_ok_type_for_typeid(ret_tid)

	def _llvm_scalar_type(self) -> str:
		# All lowered values are i64 or i1; phis currently assume Int.
		return "i64"

	def _fnresult_typeids_for_fn(self, info: FnInfo | None = None) -> tuple[TypeId, TypeId]:
		"""
		Return (ok, err) TypeIds for the internal can-throw ABI of a function.

		Important: `FnResult` is an *internal* carrier type in lang2. Surface
		signatures still declare `returns T`, and can-throw is an effect tracked
		separately (via `FnInfo.declared_can_throw`). Codegen lowers can-throw
		functions to return `FnResult<T, Error>`, deriving `T` from the signature's
		`return_type_id` and `Error` from the shared TypeTable.

		We keep a legacy fallback: older tests may still model can-throw functions
		as explicitly returning `FnResult<_, Error>` at the signature level. In that
		case we extract `(ok, err)` from the signature's return type directly.
		"""
		fn = info or self.fn_info
		ok_tid: TypeId | None = None
		if fn.signature is not None and fn.signature.return_type_id is not None:
			ok_tid = fn.signature.return_type_id
		elif fn.return_type_id is not None:
			ok_tid = fn.return_type_id
		if ok_tid is None:
			raise NotImplementedError(
				f"LLVM codegen v1: missing return type for can-throw function {fn.name}"
			)
		if self.type_table is None:
			raise NotImplementedError(
				"LLVM codegen v1: FnResult lowering requires a TypeTable for can-throw functions"
			)
		td = self.type_table.get(ok_tid)
		if td.kind is TypeKind.FNRESULT and len(td.param_types) >= 2:
			# Legacy surface model: signature already carries FnResult.
			return td.param_types[0], td.param_types[1]
		err_tid = None
		if fn.signature is not None and fn.signature.error_type_id is not None:
			err_tid = fn.signature.error_type_id
		elif fn.error_type_id is not None:
			err_tid = fn.error_type_id
		else:
			err_tid = self.type_table.ensure_error()
		return ok_tid, err_tid

	def _fnresult_types_for_fn(self, info: FnInfo) -> tuple[str, str]:
		"""Return (ok_llty, fnresult_llty) for the given FnInfo."""
		ok_tid, err_tid = self._fnresult_typeids_for_fn(info)
		ok_llty, ok_key = self._llvm_ok_type_for_typeid(ok_tid)
		fnres_llty = self.module.fnresult_type(ok_key, ok_llty)
		if self.type_table is not None:
			err_def = self.type_table.get(err_tid)
			if err_def.kind is not TypeKind.ERROR:
				raise NotImplementedError(
					f"LLVM codegen v1: FnResult error type for {info.name} is {err_def.name}, expected Error"
				)
		return ok_llty, fnres_llty

	def _fnresult_types_for_current_fn(self) -> tuple[str, str]:
		return self._fnresult_types_for_fn(self.fn_info)

	def _fnresult_type_for_current_fn(self) -> str:
		_, fnres_llty = self._fnresult_types_for_current_fn()
		return fnres_llty

	def _zero_value_for_ok(self, ok_llty: str) -> str:
		"""Return a typed zero literal for the ok payload slot of a FnResult."""
		if ok_llty == "i64":
			return "i64 0"
		if ok_llty == "i1":
			return "i1 0"
		if ok_llty.endswith("*"):
			return f"{ok_llty} null"
		# Structs/arrays and placeholder i8 can use zeroinitializer.
		return f"{ok_llty} zeroinitializer"

	def _zero_operand_for_typeid(self, ty: TypeId) -> str:
		"""
		Return a typed zero constant operand for `ty`.

		This is used when constructing aggregate zeros via `insertvalue`. For
		aggregate fields we prefer `zeroinitializer` because it is a constant and
		does not require emitting additional instructions.
		"""
		llty = self._llvm_type_for_typeid(ty)
		td = self.type_table.get(ty) if self.type_table is not None else None
		if llty == "i64":
			return "i64 0"
		if llty == "i1":
			return "i1 0"
		if llty == "double":
			return "double 0.0"
		if llty.endswith("*"):
			return f"{llty} null"
		# Arrays/structs (including String-as-aggregate) can be used as constants.
		if td is not None and td.kind in (TypeKind.ARRAY, TypeKind.STRUCT, TypeKind.SCALAR, TypeKind.ERROR, TypeKind.DIAGNOSTICVALUE):
			return f"{llty} zeroinitializer"
		return f"{llty} zeroinitializer"

	def _emit_zero_value(self, dest: str, ty: TypeId) -> None:
		"""
		Emit IR that materializes the 0-value of `ty` into `dest`.

		Unlike using a raw `zeroinitializer` constant, we need a real SSA value
		because non-address-taken locals in v1 are tracked via SSA aliases rather
		than via `store` instructions. This helper constructs aggregates via
		`insertvalue` chains.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: zero-value emission requires a TypeTable")
		llty = self._llvm_type_for_typeid(ty)
		td = self.type_table.get(ty)

		# Scalars: cheap constants.
		if llty == "i64":
			self.lines.append(f"  {dest} = add i64 0, 0")
			self.value_types[dest] = "i64"
			return
		if llty == "i1":
			self.lines.append(f"  {dest} = add i1 0, 0")
			self.value_types[dest] = "i1"
			return
		if llty == "double":
			self.lines.append(f"  {dest} = fadd double 0.0, 0.0")
			self.value_types[dest] = "double"
			return
		if llty.endswith("*"):
			# Typed pointer null. We use a bitcast to keep the pattern uniform.
			self.lines.append(f"  {dest} = bitcast {llty} null to {llty}")
			self.value_types[dest] = llty
			return

		# Array runtime representation is a fixed 3-field aggregate in v1:
		#   { len: %drift.size, cap: %drift.size, data: <elem>* }
		if td.kind is TypeKind.ARRAY and td.param_types:
			elem_llty = self._llvm_type_for_typeid(td.param_types[0])
			arr_llty = self._llvm_array_type(elem_llty)
			tmp0 = self._fresh("zero_arr")
			self.lines.append(f"  {tmp0} = insertvalue {arr_llty} undef, {DRIFT_SIZE_TYPE} 0, 0")
			tmp1 = self._fresh("zero_arr")
			self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, {DRIFT_SIZE_TYPE} 0, 1")
			self.lines.append(f"  {dest} = insertvalue {arr_llty} {tmp1}, {elem_llty}* null, 2")
			self.value_types[dest] = arr_llty
			return

		# Structs (including String, which is represented as a scalar TypeId but
		# lowered to `%DriftString` aggregate): materialize field-by-field using
		# constant operands.
		if llty == DRIFT_STRING_TYPE:
			tmp0 = self._fresh("zero_str")
			self.lines.append(f"  {tmp0} = insertvalue {DRIFT_STRING_TYPE} undef, {DRIFT_SIZE_TYPE} 0, 0")
			self.lines.append(f"  {dest} = insertvalue {DRIFT_STRING_TYPE} {tmp0}, i8* null, 1")
			self.value_types[dest] = DRIFT_STRING_TYPE
			return

		if td.kind is TypeKind.STRUCT and td.param_types:
			cur = "undef"
			last_idx = len(td.param_types) - 1
			for idx, fty in enumerate(td.param_types):
				operand = self._zero_operand_for_typeid(fty)
				out = dest if idx == last_idx else self._fresh("zero_struct")
				self.lines.append(f"  {out} = insertvalue {llty} {cur}, {operand}, {idx}")
				cur = out
			self.value_types[dest] = llty
			return

		# Fallback: keep this strict so we don't silently invent ABI behavior.
		raise NotImplementedError(f"LLVM codegen v1: cannot materialize zero value for type {td.kind} ({llty})")

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
		# Always map the original id to the resolved root to keep aliases in sync.
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
		if op == BinaryOp.MOD:
			return "srem"
		if op == BinaryOp.BIT_AND:
			return "and"
		if op == BinaryOp.BIT_OR:
			return "or"
		if op == BinaryOp.BIT_XOR:
			return "xor"
		if op == BinaryOp.SHL:
			return "shl"
		if op == BinaryOp.SHR:
			return "lshr"
		if op == BinaryOp.EQ:
			return "icmp eq"
		if op == BinaryOp.NE:
			return "icmp ne"
		if op == BinaryOp.LT:
			return "icmp slt"
		if op == BinaryOp.LE:
			return "icmp sle"
		if op == BinaryOp.GT:
			return "icmp sgt"
		if op == BinaryOp.GE:
			return "icmp sge"
		raise NotImplementedError(f"LLVM codegen v1: unsupported binary op {op}")

	def _lower_unary(self, instr: UnaryOpInstr) -> None:
		"""Lower unary ops for numeric/boolean operands."""
		dest = self._map_value(instr.dest)
		operand = self._map_value(instr.operand)
		ty = self.value_types.get(operand)
		if instr.op is UnaryOp.NOT:
			# Logical not: only supported on bool (i1).
			if ty != "i1":
				raise NotImplementedError("LLVM codegen v1: logical not only supported on bool")
			self.lines.append(f"  {dest} = xor i1 {operand}, true")
			self.value_types[dest] = "i1"
			return
		if instr.op is UnaryOp.NEG:
			# Arithmetic negation on Int (i64).
			if ty not in (None, "i64"):
				raise NotImplementedError("LLVM codegen v1: neg only supported on Int")
			self.lines.append(f"  {dest} = sub i64 0, {operand}")
			self.value_types[dest] = "i64"
			return
		if instr.op is UnaryOp.BIT_NOT:
			# Bitwise not on Int/Uint (i64 carrier).
			if ty not in (None, "i64"):
				raise NotImplementedError("LLVM codegen v1: bitwise not only supported on Int/Uint")
			self.lines.append(f"  {dest} = xor i64 {operand}, -1")
			self.value_types[dest] = "i64"
			return
		raise NotImplementedError(f"LLVM codegen v1: unsupported unary op {instr.op}")

	def _lower_binary(self, instr: BinaryOpInstr) -> None:
		"""Lower binary ops for ints, bools, and strings."""
		dest = self._map_value(instr.dest)
		left = self._map_value(instr.left)
		right = self._map_value(instr.right)
		left_ty = self.value_types.get(left)
		right_ty = self.value_types.get(right)

		# String ops (concat/eq) handled via runtime helpers.
		if left_ty == DRIFT_STRING_TYPE and right_ty == DRIFT_STRING_TYPE:
			if instr.op is BinaryOp.ADD:
				self.module.needs_string_concat = True
				self.lines.append(
					f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_concat("
					f"{DRIFT_STRING_TYPE} {left}, {DRIFT_STRING_TYPE} {right})"
				)
				self.value_types[dest] = DRIFT_STRING_TYPE
				return
			if instr.op is BinaryOp.EQ:
				self.module.needs_string_eq = True
				self.lines.append(
					f"  {dest} = call i1 @drift_string_eq({DRIFT_STRING_TYPE} {left}, {DRIFT_STRING_TYPE} {right})"
				)
				self.value_types[dest] = "i1"
				return
			raise NotImplementedError(f"LLVM codegen v1: string binary op {instr.op} not supported")

		# Boolean ops on i1.
		if left_ty == "i1" and right_ty == "i1":
			if instr.op is BinaryOp.AND:
				self.lines.append(f"  {dest} = and i1 {left}, {right}")
				self.value_types[dest] = "i1"
				return
			if instr.op is BinaryOp.OR:
				self.lines.append(f"  {dest} = or i1 {left}, {right}")
				self.value_types[dest] = "i1"
				return
			if instr.op is BinaryOp.EQ:
				self.lines.append(f"  {dest} = icmp eq i1 {left}, {right}")
				self.value_types[dest] = "i1"
				return
			if instr.op is BinaryOp.NE:
				self.lines.append(f"  {dest} = icmp ne i1 {left}, {right}")
				self.value_types[dest] = "i1"
				return
			raise NotImplementedError(f"LLVM codegen v1: unsupported bool binary op {instr.op}")

		# Integer ops on i64.
		op_str = self._map_binop(instr.op)
		dest_ty = "i64" if not op_str.startswith("icmp") else "i1"
		self.value_types[dest] = dest_ty
		self.lines.append(f"  {dest} = {op_str} i64 {left}, {right}")

	def _assert_acyclic(self) -> None:
		pass

	def _assert_cfg_supported(self) -> None:
		cfg_kind = self.ssa.cfg_kind or CfgKind.STRAIGHT_LINE
		# Backend v1 supports general SSA CFGs, including loops/backedges.
		if cfg_kind not in (CfgKind.STRAIGHT_LINE, CfgKind.ACYCLIC, CfgKind.GENERAL):
			raise NotImplementedError(f"LLVM codegen v1: unsupported CFG kind {cfg_kind}")

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
			f"  {tmp_alloc} = call i8* @drift_alloc_array(i64 {elem_size}, i64 {elem_align}, {DRIFT_SIZE_TYPE} {len_const}, {DRIFT_SIZE_TYPE} {cap_const})"
		)
		# Bitcast to elem*
		tmp_data = self._fresh("data")
		self.lines.append(f"  {tmp_data} = bitcast i8* {tmp_alloc} to {elem_llty}*")
		# Store elements
		for idx, elem in enumerate(instr.elements):
			elem_val = self._map_value(elem)
			tmp_ptr = self._fresh("eltptr")
			self.lines.append(f"  {tmp_ptr} = getelementptr inbounds {elem_llty}, {elem_llty}* {tmp_data}, i64 {idx}")
			self.lines.append(f"  store {elem_llty} {elem_val}, {elem_llty}* {tmp_ptr}")
		# Build the array struct {len, cap, data}
		tmp0 = self._fresh("arrh0")
		tmp1 = self._fresh("arrh1")
		self.lines.append(f"  {tmp0} = insertvalue {arr_llty} undef, {DRIFT_SIZE_TYPE} {len_const}, 0")
		self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, {DRIFT_SIZE_TYPE} {cap_const}, 1")
		self.lines.append(f"  {dest} = insertvalue {arr_llty} {tmp1}, {elem_llty}* {tmp_data}, 2")
		self.value_types[dest] = arr_llty

	def _lower_array_index_load(self, instr: ArrayIndexLoad) -> None:
		"""Lower ArrayIndexLoad with bounds checks and a load from data[idx]."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_type(elem_llty)
		ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		self.lines.append(f"  {dest} = load {elem_llty}, {elem_llty}* {ptr_tmp}")
		self.value_types[dest] = elem_llty

	def _lower_array_index_store(self, instr: ArrayIndexStore) -> None:
		"""Lower ArrayIndexStore with bounds checks and a store into data[idx]."""
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		value = self._map_value(instr.value)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_type(elem_llty)
		ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		self.lines.append(f"  store {elem_llty} {value}, {elem_llty}* {ptr_tmp}")
		# No dest; ArrayIndexStore returns void.

	def _lower_array_index_addr(self, *, array: str, index: str, elem_llty: str, arr_llty: str) -> str:
		"""
		Compute `&array[index]` with bounds checks and return an `{elem_llty}*`.

		This is used by:
		- ArrayIndexLoad/Store (to avoid duplicating pointer arithmetic), and
		- AddrOfArrayElem (borrow of array element).
		"""
		idx_ty = self.value_types.get(index)
		if idx_ty not in (DRIFT_SIZE_TYPE, "i64"):
			raise NotImplementedError(f"LLVM codegen v1: array index must be Int/Size, got {idx_ty}")
		# Extract len and data
		len_tmp = self._fresh("len")
		data_tmp = self._fresh("data")
		self.lines.append(f"  {len_tmp} = extractvalue {arr_llty} {array}, 0")
		self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {array}, 2")
		# Bounds checks: idx < 0 or idx >= len => drift_bounds_check_fail
		neg_cmp = self._fresh("negcmp")
		self.lines.append(f"  {neg_cmp} = icmp slt {DRIFT_SIZE_TYPE} {index}, 0")
		oob_cmp = self._fresh("oobcmp")
		self.lines.append(f"  {oob_cmp} = icmp uge {DRIFT_SIZE_TYPE} {index}, {len_tmp}")
		oob_or = self._fresh("oobor")
		self.lines.append(f"  {oob_or} = or i1 {neg_cmp}, {oob_cmp}")
		ok_block = self._fresh("array_ok")
		fail_block = self._fresh("array_oob")
		self.lines.append(f"  br i1 {oob_or}, label {fail_block}, label {ok_block}")
		# Fail block
		self.lines.append(f"{fail_block[1:]}:")
		self.lines.append(
			f"  call void @drift_bounds_check_fail({DRIFT_SIZE_TYPE} {index}, {DRIFT_SIZE_TYPE} {len_tmp})"
		)
		self.lines.append("  unreachable")
		# Ok block
		self.lines.append(f"{ok_block[1:]}:")
		ptr_tmp = self._fresh("eltptr")
		self.lines.append(
			f"  {ptr_tmp} = getelementptr inbounds {elem_llty}, {elem_llty}* {data_tmp}, {DRIFT_SIZE_TYPE} {index}"
		)
		return ptr_tmp

	def _lower_array_len(self, instr: ArrayLen) -> None:
		"""Lower ArrayLen by extracting the len field (index 0)."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		arr_llty = self.value_types.get(array, self._llvm_array_type("i64"))
		# ArrayLen now applies only to array values; strings use StringLen MIR.
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
		return f"{{ {DRIFT_SIZE_TYPE}, {DRIFT_SIZE_TYPE}, {elem_llty}* }}"

	def _llvm_array_elem_type(self, elem_ty: int) -> str:
		"""
		Map an element TypeId to an LLVM type string.

		v1 backend supports Array<Int> and Array<String>; other element types
		raise until implemented.
		"""
		if self.int_type_id is None and self.string_type_id is None:
			# Legacy fallback when no TypeTable provided: assume Array<Int>.
			return "i64"
		if self.int_type_id is not None and elem_ty == self.int_type_id:
			return "i64"
		if self.string_type_id is not None and elem_ty == self.string_type_id:
			return DRIFT_STRING_TYPE
		raise NotImplementedError(f"LLVM codegen v1: unsupported array elem type id {elem_ty!r}")

	def _sizeof(self, elem_llty: str) -> int:
		# v1: i64 → 8, i1 → 1, T* -> 8, %DriftString -> 16 (len + ptr)
		if elem_llty == "i1":
			return 1
		if elem_llty == DRIFT_STRING_TYPE:
			return 16
		return 8

	def _alignof(self, elem_llty: str) -> int:
		if elem_llty == "i1":
			return 1
		# DriftString is two word-sized fields; align to pointer size.
		if elem_llty == DRIFT_STRING_TYPE:
			return 8
		return 8
