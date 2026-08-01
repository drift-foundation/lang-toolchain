# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
SSA → LLVM IR lowering for the v1 Drift ABI (textual emitter).

Scope (v1 bring-up):
  - Input: SSA (`SsaFunc`) plus MIR (`MirFunc`) and `FnInfo` metadata.
  - Supported types: Int (isize), Bool (i1 in regs), String ({%drift.size, i8*}),
    Array<T>, and FnResult<ok, Error> where ok ∈ {Int, String, Void-like, Ref<T>,
    Array<T>, Struct, Variant, FnPtr}.
  - Supported ops: ConstInt/Bool/String, AssignSSA aliases, BinaryOpInstr (int),
    Call (Int/String or FnResult return), Phi, ConstructResultOk/Err,
    ConstructError (attrs zeroed), Return, IfTerminator/Goto, Array ops.
  - FnResult lowering requires a TypeTable so we can map ok/error TypeIds to
  LLVM payloads; we fail fast without it for can-throw functions. FnResult
  ok payloads outside {Int, String, Void-like, Ref<T>, Array<T>, Struct, Variant, FnPtr}
  are currently rejected.
  - Control flow: straight-line, if/else, and loops/backedges (general CFGs).

ABI (from doc/design/drift-lang-abi.md):
  - %DriftError is modeled in LLVM as the stable prefix used by codegen:
    { u64 code, %DriftString event_fqn,
      i8* legacy_attrs, usize legacy_attr_count,
      i8* legacy_frames, usize legacy_frame_count }
    Additive JSON fields (`params_json`, `context_json`) live later in
    the C struct (see `lang/compiler_infra/error_dummy.h` — ABI 12
    additive layout) and are accessed only through the
    `drift_error_*_params_json` / `drift_error_*_context_json` helper
    surface — never via direct extractvalue.  Slice 3
    (`Error.encode_compact`) extracts `event_fqn` via the stable prefix
    (field 1) and reads the JSON segments through their helpers.
    ABI 13 (Slice 5) reshapes this layout when the legacy DV path
    deletes; until then, the prefix above is the codegen contract.
  - %FnResult_Int_Error   = { i8 is_err, isize ok, %DriftError* err }
  - %FnResult_String_Error= { i8 is_err, %DriftString ok, %DriftError* err }
  - %FnResult_Void_Error  = { i8 is_err, i8 ok, %DriftError* err } (void-like ok)
  - %DriftString          = { i64, i8* }
  - i64/%drift.usize are word-sized carriers for Int/Uint
  - Drift Int is pointer-sized; Bool is i1 in registers.

This emitter is deliberately small and produces LLVM text suitable for feeding
to `lli`/`clang` in tests. It keeps allocas constrained to entry-block locals and
temporary payload packing where LLVM requires addressable storage. Unsupported
features raise clear errors rather than emitting bad IR.
"""

from __future__ import annotations

import re
import struct
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

from lang.driftc.checker import FnInfo
from lang.driftc import debug as drift_debug
from lang.driftc.core.span import Span
from lang.driftc.core.function_id import FunctionId, FunctionRefId, function_symbol, function_ref_symbol
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.container_ids import ARRAY_CONTAINER_ID, RAW_BUFFER_CONTAINER_ID, STRING_CONTAINER_ID
from lang.driftc.impl_index import ImplMeta
from lang.driftc.type_resolver import resolve_opaque_type

# Implicit runtime dependencies of the OS entry wrapper.  The entry wrapper
# calls these as __impl symbols (codegen body-rename artifacts), so they must
# be present in the lowered MIR for their bodies to be emitted.  In the
# package-consumer path, _build_package_consumer_unit conditionally seeds
# these into pkg_needed ONLY when all their callees are already reachable
# through the user program's natural BFS (K40).  No transitive closure
# walk — avoids K18-class explosions from heavy generic instantiations.
# Keyed by the flag name passed to emit_entry_wrapper / emit_argv_entry_wrapper;
# values are (module, name) pairs matched against FunctionId fields.
ENTRY_WRAPPER_IMPLICIT_DEPS: dict[str, tuple[str, str]] = {
	"install_process_preamble": ("std.io", "install_process_preamble"),
}

ARRAY_LEN_IDX = 0
ARRAY_CAP_IDX = 1
ARRAY_GEN_IDX = 2
ARRAY_PTR_IDX = 3
RAWBUF_PTR_IDX = 0
RAWBUF_CAP_IDX = 1
from lang.driftc.stage1 import BinaryOp, UnaryOp
from lang.driftc.stage1.call_info import CallSig
from lang.driftc.stage2.mir_nodes import Unreachable as MirUnreachable
from lang.driftc.stage2 import (
	ArrayCap,
	ArrayIndexLoad,
	ArrayIndexLoadUnchecked,
	ArrayIndexStore,
	ArrayLen,
	ArrayGen,
	ArrayLit,
	ArrayAlloc,
	ArrayElemInit,
	ArrayElemInitUnchecked,
	ArrayElemAssign,
	ArrayElemDrop,
	ArrayElemTake,
	ArrayDrop,
	ArrayDup,
	ArraySetLen,
	ArraySetGen,
	RawBufferAlloc,
	RawBufferDealloc,
	RawBufferPtrAt,
	RawBufferRead,
	RawBufferWrite,
	PtrFromRef,
	PtrOffset,
	PtrRead,
	PtrWrite,
	PtrIsNull,
	PtrAsMutRef,
	AssignSSA,
	BinaryOpInstr,
	WrappingAddU64,
	WrappingMulU64,
	Call,
	CallIndirect,
	CallIface,
	ConstructStruct,
	ConstructVariant,
	VariantTag,
	VariantTagRef,
	VariantGetField,
	VariantGetFieldAddr,
	StructGetField,
	ConstArray,
	ConstBool,
	ConstVoid,
	ConstInt,
	ConstUint,
	ConstUint64,
	ConstByte,
	IntFromUint,
	UintFromInt,
	CastScalar,
	ConstString,
	FnPtrConst,
	ConstructIface,
	ConstructIfaceValue,
	ConstructIfaceBorrowed,
	IfaceUpcast,
	ArcAsInterface,
	ArcFatGet,
	ZeroValue,
	TombstoneValue,
	StringRetain,
	StringRelease,
	CopyValue,
	DropValue,
	MoveOut,
	AssertLoc,
	ConstructError,
	ErrorRaise,
	ErrorEvent,
	ErrorEventFqn,
	ConstructResultErr,
	ConstructResultOk,
	ExcGetParamsJson,
	ExcSetParamsJson,
	ExcGetContextJson,
	ExcAppendContextFrame,
	LoadLocal,
	AddrOfLocal,
	AddrOfArrayElem,
	AddrOfField,
	LoadRef,
	StoreRef,
	MoveFromRef,
	ResultErr,
	ResultIsErr,
	ResultOk,
	Goto,
	IfTerminator,
	MirFunc,
	Phi,
	Return,
	Unreachable,
	SwitchTerminator,
	StringConcat,
	StringEq,
	StringCmp,
	StringLen,
	StringByteAt,
	StringBytesBase,
	StringFromBool,
	StringFromInt,
	StringFromUint,
	StringFromFloat,
	StoreLocal,
	UnaryOpInstr,
	ConstFloat,
)
from lang.driftc.stage4.ssa import SsaFunc
from lang.driftc.stage4.ssa import CfgKind
from lang.driftc.core.types_core import TypeKind, TypeTable, TypeId
from lang.driftc.core.xxhash64 import hash64

# ABI type names
DRIFT_ERROR_TYPE = "%DriftError"
DRIFT_ERROR_PTR = "ptr"


def _inline_hint_eligible(func, type_table, ret_type_id) -> bool:
	"""STRUCTURAL inlinehint eligibility (2026-07-25 review round):
	SMALL function AND an applicable ACCESSOR SHAPE — deliberately NOT
	a compiler-wide "inline all small functions" policy.

	  small:  hot-path MIR size (instructions outside blocks that
	          terminate in Unreachable — assert/contract fail arms)
	          <= 48 (threshold-swept);
	  shape:  returns a VARIANT type (Result/Optional-style accessors
	          whose error/None arms RETURN and are therefore not
	          Unreachable-cold), OR contains a cold-failure block
	          (an Unreachable-terminated assert/contract arm).

	Ordinary small hot functions (no variant return, no cold arm) are
	NOT hinted — tiny ones inline on LLVM's own cost model anyway.
	The hint only nudges the cost model; LLVM still decides.
	"""
	hot_instrs = 0
	has_cold_block = False
	for b in func.blocks.values():
		if isinstance(b.terminator, MirUnreachable):
			has_cold_block = True
		else:
			hot_instrs += len(b.instructions)
	if hot_instrs > 48:
		return False
	if has_cold_block:
		return True
	if type_table is not None and ret_type_id is not None:
		td = type_table.get(ret_type_id)
		if td is not None and getattr(td, "kind", None) is TypeKind.VARIANT:
			return True
	return False


def _is_ptr_type(ty: str | None) -> bool:
	"""Check whether an LLVM type string denotes a pointer type."""
	if ty is None:
		return False
	return ty == "ptr" or ty.endswith("*")


FNRESULT_INT_ERROR = "%FnResult_Int_Error"
DRIFT_INT_TAG = "drift.int"
DRIFT_UINT_TAG = "drift.uint"
DRIFT_USIZE_TYPE = DRIFT_UINT_TAG
DRIFT_INT_TYPE = DRIFT_INT_TAG
DRIFT_UINT_TYPE = DRIFT_USIZE_TYPE
DRIFT_U64_TYPE = "i64"
DRIFT_ERROR_CODE_TYPE = DRIFT_U64_TYPE
DRIFT_STRING_TYPE = "%DriftString"
DRIFT_IFACE_TYPE = "%DriftIface"
DRIFT_CALLBACK_VTABLE_TYPE = "%DriftCallbackVTable"
DRIFT_FAT_FNPTR_TYPE = "%DriftFatFnPtr"
DRIFT_IFACE_INLINE_WORDS = 4
DRIFT_IFACE_DATA_IDX = 0
DRIFT_IFACE_VTABLE_IDX = 1
DRIFT_IFACE_INLINE_IDX = 2
DRIFT_IFACE_INLINE_FLAG_IDX = 3
# Interface value flag byte is a BITFIELD: bit0 = payload stored inline,
# bit1 = owns a drift_iface_alloc heap block, bit2 = BORROWED view over
# caller-owned storage (0.33.77 — drop is a complete no-op: no payload
# drop thunk, no free). Dispatch reads bit0 only, so borrowed views
# (bit0 clear) dispatch through the data slot with unchanged code.
DRIFT_IFACE_FLAG_BORROWED = 4

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


def _llvm_comdat_sym(name: str) -> str:
	"""
	Render a COMDAT group symbol for LLVM IR.

	Uses the function symbol name with a `$` prefix.
	"""
	return _llvm_fn_sym(name).replace("@", "$", 1)


# Slice 7c-3 (ABI 14, 2026-05-06): `DRIFT_DV_TYPE` constant and
# the `%DriftDiagnosticValue` LLVM type alias are deleted along
# with `TypeKind.DIAGNOSTICVALUE`.
DWARF_LANG = "DW_LANG_Rust"
DW_TAG_POINTER = "DW_TAG_pointer_type"
DW_TAG_STRUCT = "DW_TAG_structure_type"
DW_TAG_MEMBER = "DW_TAG_member"
DW_TAG_UNION = "DW_TAG_union_type"
DW_TAG_ENUM = "DW_TAG_enumeration_type"
DW_ATE_SIGNED = "DW_ATE_signed"
DW_ATE_UNSIGNED = "DW_ATE_unsigned"
DW_ATE_BOOLEAN = "DW_ATE_boolean"
DW_ATE_FLOAT = "DW_ATE_float"


# Public API -------------------------------------------------------------------

def lower_ssa_func_to_llvm(
	func: MirFunc,
	ssa: SsaFunc,
	fn_info: FnInfo,
	fn_infos: Mapping[FunctionId, FnInfo] | None = None,
	type_table: Optional[TypeTable] = None,
	word_bits: int | None = None,
	float_bits: int | None = None,
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
	  - Returns: Int, String, or FnResult<ok, Error> (ok ∈ {Int, String, Void-like, Ref<T>, Array<T>}) in v1.
	  - General CFGs (including loops/backedges) are supported in v1.
	"""
	all_infos = dict(fn_infos) if fn_infos is not None else {fn_info.fn_id: fn_info}
	if word_bits is None:
		raise AssertionError("LLVM codegen requires explicit word_bits")
	mod = LlvmModuleBuilder(word_bits=word_bits, float_bits=float_bits or 64)
	builder = _FuncBuilder(func=func, ssa=ssa, fn_info=fn_info, fn_infos=all_infos, module=mod, type_table=type_table)
	mod.emit_func(builder.lower())
	return mod.render()


def _iface_impl_index_key(type_table: TypeTable, iface_ty: TypeId, value_ty: TypeId) -> tuple[str, str]:
	"""Canonical key for the interface impl index: the exact interface
	INSTANCE (or bare base for non-generic interfaces) plus the target
	type, both as `type_key_string`s. Instance-keying is load-bearing:
	the old `(iface_base, target_tid)` key merged multi-instance impls
	(`implement Sink<Int> for Box` + `implement Sink<String> for Box`)
	first-impl-wins, silently dispatching one instance through the
	other's methods (miscompile on ≤0.33.77). Key strings also carry
	package/module identity, so `pkgA`'s `Sink<Int>` can never collide
	with a local or foreign interface of the same name."""
	inst = type_table.get_interface_instance(iface_ty)
	if inst is not None and getattr(inst, "type_args", None):
		ikey = type_table.type_key_string(iface_ty)
	else:
		base = inst.base_id if inst is not None else iface_ty
		ikey = type_table.type_key_string(base)
	return (ikey, type_table.type_key_string(value_ty))


def _build_interface_impl_index(
	module_exports: Mapping[str, dict[str, object]] | None,
	type_table: Optional[TypeTable],
) -> Dict[tuple[str, str], Dict[str, FunctionId]]:
	if module_exports is None or type_table is None:
		return {}
	index: Dict[tuple[str, str], Dict[str, FunctionId]] = {}
	for exp in module_exports.values():
		if not isinstance(exp, dict):
			continue
		impls = exp.get("impls")
		if not isinstance(impls, list):
			continue
		for impl in impls:
			if not isinstance(impl, ImplMeta):
				continue
			if impl.trait_expr is None:
				continue
			if getattr(impl, "impl_type_params", None):
				continue
			try:
				trait_ty = resolve_opaque_type(impl.trait_expr, type_table, module_id=impl.def_module)
			except Exception:
				continue
			inst = type_table.get_interface_instance(trait_ty)
			iface_base = inst.base_id if inst is not None else trait_ty
			if type_table.get(iface_base).kind is not TypeKind.INTERFACE:
				continue
			key = _iface_impl_index_key(type_table, trait_ty, impl.target_type_id)
			method_map = index.setdefault(key, {})
			for method in list(getattr(impl, "methods", []) or []):
				method_map.setdefault(method.name, method.fn_id)
	return index


def _extern_c_llvm_type(ty_id: TypeId, type_table: Optional[TypeTable], mod: "LlvmModuleBuilder") -> str:
	"""Map a TypeId to an LLVM type string for extern "C" declarations.

	Supports the subset of types valid in C FFI signatures: scalars, pointers,
	and Void.  Falls back to isize (pointer-sized int) for unknown types so
	that opaque handles round-trip safely.
	"""
	if type_table is not None:
		if type_table.is_void(ty_id):
			return "i8"  # void param slot (unused)
		td = type_table.get(ty_id)
		if td.kind is TypeKind.SCALAR:
			_MAP = {
				"Int": DRIFT_INT_TYPE,
				"Uint": DRIFT_USIZE_TYPE,
				"Uint64": DRIFT_U64_TYPE,
				"u64": DRIFT_U64_TYPE,
				"Int32": "i32",
				"Uint32": "i32",
				"Bool": "i1",
				"Byte": "i8",
				"Float": "double",
			}
			if td.name in _MAP:
				return _MAP[td.name]
		if td.kind is TypeKind.RAW_PTR:
			return "ptr"
		if td.kind is TypeKind.REF:
			return "ptr"
		# FORWARD_NOMINAL arises before full type normalization (e.g. RawPtr<T>).
		if td.kind is TypeKind.FORWARD_NOMINAL and td.name == "RawPtr":
			return "ptr"
		if td.kind is TypeKind.FORWARD_NOMINAL and td.name == "Ref":
			return "ptr"
	# Fallback: pointer-sized integer.
	return DRIFT_INT_TYPE


def lower_module_to_llvm(
	funcs: Mapping[FunctionId, MirFunc],
	ssa_funcs: Mapping[FunctionId, SsaFunc],
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: Optional[TypeTable] = None,
	module_exports: Optional[Mapping[str, dict[str, object]]] = None,
	rename_map: Optional[Mapping[FunctionId, str]] = None,
	argv_wrapper: Optional[str] = None,
	word_bits: int | None = None,
	float_bits: int | None = None,
	debug_enabled: bool = True,
	provenance_git_sha: str = "",
	provenance_build_profile: str = "",
	provenance_build_info: Optional[dict] = None,
) -> LlvmModuleBuilder:
	"""
	Lower a set of SSA functions to an LLVM module.

	Args:
	  funcs: name -> MIR function
	  ssa_funcs: name -> SSA wrapper (must align with funcs)
	  fn_infos: FunctionId -> FnInfo for each function
	"""
	if word_bits is None:
		raise AssertionError("LLVM codegen requires explicit word_bits")
	# FINAL MIR boundary: prove the provenance of every unchecked
	# string byte load AFTER all mutating MIR passes — codegen skips
	# its guards for these, so an unproven one must never get here.
	from lang.driftc.stage2.unchecked_load_validator import validate_unchecked_string_loads
	for _fn in funcs.values():
		validate_unchecked_string_loads(_fn)
	mod = LlvmModuleBuilder(word_bits=word_bits, float_bits=float_bits or 64, debug_enabled=debug_enabled)
	# drift-build-info/v1 stamp: ALWAYS emitted (unstamped compiles get
	# artifact null / deps [] / extra {} — the reserved sections are
	# never absent). Inputs come pre-validated from the CLI.
	_bi = provenance_build_info or {}
	mod.emit_build_info(
		git_sha=provenance_git_sha,
		build_profile=provenance_build_profile,
		artifact=_bi.get("artifact"),
		dependencies=_bi.get("dependencies") or {},
		extra=_bi.get("extra") or {},
	)
	mod.iface_impls = _build_interface_impl_index(module_exports, type_table)
	# Check lowered MIR (not fn_infos) for preamble availability — the
	# function must have a body in `funcs` so codegen produces the __impl
	# symbol.  Checking fn_infos would be True even when the function was
	# BFS-pruned (K17/K18).
	install_process_preamble_available = any(
		fn_id.module == "std.io" and fn_id.name == "install_process_preamble"
		for fn_id in funcs
	)

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
	# - Exported symbols use the boundary `Result<ok, Error*>` ABI:
	#   - non-void: `{ ok, err* }`
	#   - void: `err*` (null on success)
	# - Internal functions continue to return `FnResult<ok, Error>` for throw
	#   checks and MIR lowering.
	#
	# This preserves the language semantics ("-> T") while making the
	# module interface uniformly boundary-shaped.
	# Driver-level renames (e.g. argv wrapper name) must not affect call-site
	# binding decisions. Keep them separate from export wrapper renames.
	driver_rename: dict[FunctionId, str] = dict(rename_map or {})
	body_rename: dict[FunctionId, str] = dict(driver_rename)
	export_impl_map: dict[FunctionId, str] = {}
	exported_fns: list[FunctionId] = []
	for fn_id, info in fn_infos.items():
		sig = info.signature
		if sig is None or not bool(getattr(sig, "is_exported_entrypoint", False)):
			continue
		if bool(getattr(sig, "is_method", False)):
			continue
		if bool(getattr(sig, "is_intrinsic", False)):
			continue
		# Only functions that exist in the current module (i.e. present in funcs)
		# can have wrappers emitted here. Imported functions are declared elsewhere.
		if fn_id not in funcs:
			continue
		if fn_id in body_rename:
			# If the driver already renamed this symbol (e.g. argv wrapper), do not
			# add another layer of indirection.
			continue
		impl = f"{function_symbol(fn_id)}__impl"
		body_rename[fn_id] = impl
		export_impl_map[fn_id] = impl
		exported_fns.append(fn_id)

	for fn_id, mir_func in funcs.items():
		ssa = ssa_funcs[fn_id]
		fn_info = fn_infos[fn_id]
		if fn_info.signature is not None and getattr(fn_info.signature, "is_intrinsic", False):
			# Intrinsics lower to dedicated MIR/LLVM ops; skip empty stubs.
			continue
		if fn_info.signature is not None and getattr(fn_info.signature, "is_extern_c", False):
			# extern "C" functions have no Drift body; emit a bare declare.
			mod.add_extern_c_declare(fn_info, type_table)
			continue
		builder = _FuncBuilder(
			func=mir_func,
			ssa=ssa,
			fn_info=fn_info,
			fn_infos=fn_infos,
			module=mod,
			type_table=type_table,
			sym_name=body_rename.get(fn_id),
			rename_map=driver_rename,
			export_impl_map=export_impl_map,
		)
		mod.emit_func(builder.lower())

	# Emit wrappers after all implementation bodies so they can reference the
	# renamed `__impl` symbols.
	for public in sorted(exported_fns, key=function_symbol):
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
			llty = type_builder._llvm_type_for_typeid(ty_id, allow_void_ok=True)
			param_parts.append(f"{type_builder._llty(llty)} %{param_names[i]}")
		params_str = ", ".join(param_parts)

		# Return type: boundary Result for exported entrypoints.
		ok_llty, ok_key = type_builder._llvm_ok_type_for_sig(sig)
		ret_tid = sig.return_type_id
		impl_ret_llty = ok_llty
		ok_abi_llty = ok_llty
		if ret_tid is not None:
			ok_abi_llty = type_builder._llvm_ok_abi_type_for_typeid(ret_tid)
			if type_builder._is_void_typeid(ret_tid):
				impl_ret_llty = "void"
			else:
				impl_ret_llty = type_builder._llvm_type_for_typeid(ret_tid)
		is_void_ret = ret_tid is not None and type_builder._is_void_typeid(ret_tid)
		emit_ok_abi_llty = type_builder._llty(ok_abi_llty)
		emit_impl_ret_llty = type_builder._llty(impl_ret_llty)
		res_llty = DRIFT_ERROR_PTR if is_void_ret else f"{{ {emit_ok_abi_llty}, {DRIFT_ERROR_PTR} }}"

		lines: list[str] = []
		lines.append(f"define {res_llty} {_llvm_fn_sym(function_symbol(public))}({params_str}) {{")
		lines.append("__bb_entry:")
		args = ", ".join(
			f"{type_builder._llty(type_builder._llvm_type_for_typeid(t, allow_void_ok=True))} %{n}"
			for t, n in zip(param_tids, param_names)
		)

		if info.declared_can_throw:
			# Convert internal FnResult<ok, Error*> to boundary Result ABI.
			fnres_llty = mod.fnresult_type(ok_key, ok_llty, ok_typeid=ret_tid)
			lines.append(f"  %res = call {fnres_llty} {_llvm_fn_sym(impl)}({args})")
			if is_void_ret:
				lines.append(f"  %err = extractvalue {fnres_llty} %res, 2")
				lines.append(f"  ret {DRIFT_ERROR_PTR} %err")
			else:
				ok_zero = type_builder._zero_value_for_ok(ok_abi_llty)
				lines.append(f"  %is_err_raw = extractvalue {fnres_llty} %res, 0")
				lines.append(f"  %is_err = icmp ne i8 %is_err_raw, 0")
				lines.append(f"  %ok = extractvalue {fnres_llty} %res, 1")
				lines.append(f"  %err = extractvalue {fnres_llty} %res, 2")
				ok_val = "%ok"
				if ok_llty != ok_abi_llty:
					if ok_llty == "i1" and ok_abi_llty == "i8":
						ok_val = "%ok_abi"
						lines.append(f"  {ok_val} = zext i1 %ok to i8")
					else:
						raise AssertionError("LLVM codegen v1: unsupported ok ABI coercion")
				lines.append(f"  %ok_sel = select i1 %is_err, {ok_zero}, {emit_ok_abi_llty} {ok_val}")
				lines.append(f"  %err_sel = select i1 %is_err, {DRIFT_ERROR_PTR} %err, {DRIFT_ERROR_PTR} null")
				lines.append(f"  %tmp0 = insertvalue {res_llty} zeroinitializer, {emit_ok_abi_llty} %ok_sel, 0")
				lines.append(f"  %tmp1 = insertvalue {res_llty} %tmp0, {DRIFT_ERROR_PTR} %err_sel, 1")
				lines.append(f"  ret {res_llty} %tmp1")
		else:
			if is_void_ret:
				lines.append(f"  call void {_llvm_fn_sym(impl)}({args})")
				lines.append(f"  ret {DRIFT_ERROR_PTR} null")
			else:
				lines.append(f"  %ok = call {emit_impl_ret_llty} {_llvm_fn_sym(impl)}({args})")
				ok_val = "%ok"
				if impl_ret_llty != ok_abi_llty:
					if impl_ret_llty == "i1" and ok_abi_llty == "i8":
						ok_val = "%ok_abi"
						lines.append(f"  {ok_val} = zext i1 %ok to i8")
					else:
						raise AssertionError("LLVM codegen v1: unsupported ok ABI coercion")
				lines.append(f"  %tmp0 = insertvalue {res_llty} zeroinitializer, {emit_ok_abi_llty} {ok_val}, 0")
				lines.append(f"  %tmp1 = insertvalue {res_llty} %tmp0, {DRIFT_ERROR_PTR} null, 1")
				lines.append(f"  ret {res_llty} %tmp1")
		lines.append("}")
		mod.emit_func("\n".join(lines))

	if argv_wrapper is not None:
		array_llty = "%DriftArrayHeader"
		mod.emit_argv_entry_wrapper(
			user_main=argv_wrapper,
			array_type=array_llty,
			install_process_preamble=install_process_preamble_available,
		)
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


def _llvm_md_escape(text: str) -> str:
	"""Escape strings used in LLVM metadata (handles backslash and quotes)."""
	out = []
	for ch in text:
		if ch == "\\":
			out.append("\\\\")
		elif ch == "\"":
			out.append("\\\"")
		else:
			out.append(ch)
	return "".join(out)


@dataclass
class LlvmModuleBuilder:
	"""Textual LLVM module builder with seeded ABI type declarations."""

	word_bits: int
	float_bits: int = 64
	debug_enabled: bool = True
	type_decls: List[str] = field(default_factory=list)
	consts: List[str] = field(default_factory=list)
	funcs: List[str] = field(default_factory=list)
	comdats: set[str] = field(default_factory=set)
	needs_array_helpers: bool = False
	needs_iface_helpers: bool = False
	needs_string_eq: bool = False
	needs_string_cmp: bool = False
	needs_string_concat: bool = False
	needs_string_ffi_bridge: bool = False
	needs_string_observe_guard: bool = False
	needs_string_from_int64: bool = False
	needs_string_from_uint64: bool = False
	needs_string_from_bool: bool = False
	needs_string_from_f64: bool = False
	needs_string_from_utf8_bytes: bool = False
	needs_string_retain: bool = False
	needs_string_release: bool = False
	needs_memcpy: bool = False
	needs_argv_helper: bool = False
	needs_run_main_on_vt: bool = False
	needs_console_runtime: bool = False
	needs_thread_runtime: bool = False
	needs_atomic_runtime: bool = False
	# Slice 7c-2 (ABI 14, 2026-05-06): `needs_dv_runtime` flag
	# deleted — the runtime DV exports are gone, codegen no
	# longer needs a per-module signal to declare them.
	needs_error_runtime: bool = False
	needs_assert_runtime: bool = False
	needs_llvm_trap: bool = False
	# Stage 3 fat `Arc<Interface>` — `ArcAsInterface` lowering emits a
	# direct call to the non-generic `std.concurrent` bump helper with
	# no corresponding MIR `M.Call` / FnInfo path, so there is no
	# natural place to attach the declare.  Set by
	# `_lower_arc_as_interface` and consumed by the module-render pass
	# to emit a `declare void ...(ptr)` for the symbol ONLY when its
	# definition is not present in this LLVM module (package-consumer
	# build where std.concurrent lives in an upstream dep).  LLVM
	# rejects a `declare` plus `define` for the same quoted Drift
	# symbol in one module even when the prototype matches, so the
	# render path must skip the declare in single-module / dev builds
	# where stdlib compiles inline.
	needs_arc_fat_bump_helper: bool = False
	array_string_type: Optional[str] = None
	_fnresult_types_by_key: Dict[str, str] = field(default_factory=dict)
	_fnresult_ok_llty_by_type: Dict[str, str] = field(default_factory=dict)
	_fnresult_ok_typeid_by_type: Dict[str, TypeId] = field(default_factory=dict)
	_fnresult_unwrap_helpers: Dict[str, str] = field(default_factory=dict)
	_struct_types_by_name: Dict[str, str] = field(default_factory=dict)
	_variant_types_by_key: Dict[str, str] = field(default_factory=dict)
	array_drop_helpers: Dict[str, str] = field(default_factory=dict)
	clone_helpers: Dict[str, str] = field(default_factory=dict)
	# Slice 7c-2 (ABI 14): `dv_drop_helper` field deleted.
	iface_drop_helper: str | None = None
	string_literal_cache: Dict[str, tuple[str, str, int]] = field(default_factory=dict)
	const_array_cache: Dict[tuple, tuple[str, str, int]] = field(default_factory=dict)
	iface_vtables: Dict[str, str] = field(default_factory=dict)
	iface_thunks: Dict[str, str] = field(default_factory=dict)
	iface_impls: Dict[tuple[str, str], Dict[str, FunctionId]] = field(default_factory=dict)
	iface_vtable_sizes: Dict[str, int] = field(default_factory=dict)
	nothrow_thunk_cache: Dict[tuple[str, str], str] = field(default_factory=dict)
	_variant_type_cache: Dict[str, bool] = field(default_factory=dict)
	fat_fnptr_wrap_thunks: Dict[str, str] = field(default_factory=dict)
	fat_fnptr_fwd_thunks: Dict[str, str] = field(default_factory=dict)
	_dbg_next_id: int = 0
	_dbg_metadata: List[str] = field(default_factory=list)
	_dbg_compile_unit_id: int | None = None
	_dbg_file_ids: Dict[tuple[str, str], int] = field(default_factory=dict)
	_dbg_subprogram_ids: Dict[str, int] = field(default_factory=dict)
	_dbg_location_ids: Dict[tuple[int, int, int], int] = field(default_factory=dict)
	_dbg_subroutine_type_id: int | None = None
	_dbg_empty_md_id: int | None = None
	_dbg_module_flag_ids: tuple[int, int] | None = None
	_dbg_type_ids: Dict[TypeId, int] = field(default_factory=dict)
	_global_ctors: List[str] = field(default_factory=list)
	_llvm_used: List[str] = field(default_factory=list)
	_dbg_expression_id: int | None = None
	needs_dbg_intrinsics: bool = False
	_extern_c_declares: List[str] = field(default_factory=list)

	def _llty(self, ty: str) -> str:
		if ty in (DRIFT_INT_TYPE, DRIFT_USIZE_TYPE):
			return f"i{self.word_bits}"
		return ty

	def _dbg_new_id(self) -> int:
		self._dbg_next_id += 1
		return self._dbg_next_id

	def _ensure_dbg_empty(self) -> int:
		if self._dbg_empty_md_id is None:
			self._dbg_empty_md_id = self._dbg_new_id()
			self._dbg_metadata.append(f"!{self._dbg_empty_md_id} = !{{}}")
		return self._dbg_empty_md_id

	def _ensure_dbg_subroutine_type(self) -> int:
		if self._dbg_subroutine_type_id is None:
			empty = self._ensure_dbg_empty()
			self._dbg_subroutine_type_id = self._dbg_new_id()
			self._dbg_metadata.append(f"!{self._dbg_subroutine_type_id} = !DISubroutineType(types: !{empty})")
		return self._dbg_subroutine_type_id

	def _ensure_dbg_module_flags(self) -> tuple[int, int]:
		if self._dbg_module_flag_ids is None:
			dwarf_flag = self._dbg_new_id()
			dbg_flag = self._dbg_new_id()
			self._dbg_metadata.append(f"!{dwarf_flag} = !{{i32 2, !\"Dwarf Version\", i32 5}}")
			self._dbg_metadata.append(f"!{dbg_flag} = !{{i32 2, !\"Debug Info Version\", i32 3}}")
			self._dbg_module_flag_ids = (dwarf_flag, dbg_flag)
		return self._dbg_module_flag_ids

	def _ensure_dbg_expression(self) -> int:
		if self._dbg_expression_id is None:
			self._dbg_expression_id = self._dbg_new_id()
			self._dbg_metadata.append(f"!{self._dbg_expression_id} = !DIExpression()")
		return self._dbg_expression_id

	def _ensure_di_file(self, span: Span | None) -> int:
		file_name = "<unknown>"
		dir_name = "."
		if span is not None and span.file:
			file_name = os.path.basename(span.file)
			dir_name = os.path.dirname(span.file) or "."
		key = (file_name, dir_name)
		if key in self._dbg_file_ids:
			return self._dbg_file_ids[key]
		file_id = self._dbg_new_id()
		self._dbg_file_ids[key] = file_id
		self._dbg_metadata.append(
			f"!{file_id} = !DIFile(filename: \"{_llvm_md_escape(file_name)}\", directory: \"{_llvm_md_escape(dir_name)}\")"
		)
		return file_id

	def _ensure_di_compile_unit(self, file_id: int) -> int:
		if self._dbg_compile_unit_id is None:
			empty = self._ensure_dbg_empty()
			self._dbg_compile_unit_id = self._dbg_new_id()
			self._dbg_metadata.append(
				f"!{self._dbg_compile_unit_id} = distinct !DICompileUnit(language: {DWARF_LANG}, file: !{file_id}, producer: \"driftc\", isOptimized: false, runtimeVersion: 0, emissionKind: FullDebug, enums: !{empty}, globals: !{empty})"
			)
		return self._dbg_compile_unit_id

	def get_di_subprogram(self, fn_name: str, linkage_name: str | None, span: Span | None) -> int | None:
		if not self.debug_enabled:
			return None
		if fn_name in self._dbg_subprogram_ids:
			return self._dbg_subprogram_ids[fn_name]
		file_id = self._ensure_di_file(span)
		cu_id = self._ensure_di_compile_unit(file_id)
		sub_type = self._ensure_dbg_subroutine_type()
		line = span.line if span is not None and span.line is not None else 1
		sub_id = self._dbg_new_id()
		linkage = linkage_name or fn_name
		self._dbg_metadata.append(
			f"!{sub_id} = distinct !DISubprogram(name: \"{_llvm_md_escape(fn_name)}\", linkageName: \"{_llvm_md_escape(linkage)}\", scope: !{file_id}, file: !{file_id}, line: {line}, scopeLine: {line}, type: !{sub_type}, unit: !{cu_id}, spFlags: DISPFlagDefinition, retainedNodes: !{self._ensure_dbg_empty()})"
		)
		self._dbg_subprogram_ids[fn_name] = sub_id
		return sub_id

	def get_di_location(self, span: Span | None, scope_id: int | None) -> int | None:
		if not self.debug_enabled or scope_id is None:
			return None
		if span is None or span.line is None:
			return None
		line = span.line
		column = span.column or 1
		# LLVM stores `DILocation.column` as a 16-bit unsigned integer
		# (max 65535). Pathological single-line inputs (e.g. machine-
		# generated long expression chains, robustness probes like
		# `gen_else_if_chain` at d≥2000) can produce column counts that
		# exceed this. Without clamping, the LLVM IR text emission below
		# produces a `column: <overflow>` value that the LLVM IR parser
		# rejects with `value for 'column' too large, limit is 65535`.
		# Clamp to LLVM's maximum so the compile succeeds; the resulting
		# debug info points "near the end of the line", which is lossy
		# but more useful than the alternative (column 0 = unknown).
		# See `issues/llvm-debuginfo-column-overflow/`.
		if column > 65535:
			column = 65535
		key = (scope_id, line, column)
		if key in self._dbg_location_ids:
			return self._dbg_location_ids[key]
		loc_id = self._dbg_new_id()
		self._dbg_location_ids[key] = loc_id
		self._dbg_metadata.append(f"!{loc_id} = !DILocation(line: {line}, column: {column}, scope: !{scope_id})")
		return loc_id

	def __post_init__(self) -> None:
		inline_storage = f"[{DRIFT_IFACE_INLINE_WORDS} x {self._llty(DRIFT_USIZE_TYPE)}]"
		self.type_decls.extend(
			[
				f"{DRIFT_STRING_TYPE} = type {{ {self._llty(DRIFT_INT_TYPE)}, ptr }}",
				f"{DRIFT_ERROR_TYPE} = type {{ {DRIFT_ERROR_CODE_TYPE}, {DRIFT_STRING_TYPE}, ptr, {self._llty(DRIFT_USIZE_TYPE)}, ptr, {self._llty(DRIFT_USIZE_TYPE)} }}",
				f"{DRIFT_IFACE_TYPE} = type {{ ptr, ptr, {inline_storage}, i8, [7 x i8] }}",
				f"{DRIFT_CALLBACK_VTABLE_TYPE} = type [2 x ptr]",
				f"{FNRESULT_INT_ERROR} = type {{ i8, {self._llty(DRIFT_INT_TYPE)}, {DRIFT_ERROR_PTR} }}",
				f"%DriftArrayHeader = type {{ {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, ptr }}",
				f"{DRIFT_FAT_FNPTR_TYPE} = type {{ ptr, ptr }}",
			]
		)
		self._build_info_payload = ""
		self._build_info_doc = None
		# Seed the canonical FnResult types for supported ok payloads.
		self._fnresult_types_by_key["Int"] = FNRESULT_INT_ERROR
		self._fnresult_ok_llty_by_type[FNRESULT_INT_ERROR] = DRIFT_INT_TYPE
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
		cache by a stable, argument-sensitive type key so multiple instantiations
		get distinct LLVM types.

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
		mod = td.module_id or ""
		type_key = type_table.type_key_string(ty_id)
		cache_key = type_key
		if cache_key in self._struct_types_by_name:
			return self._struct_types_by_name[cache_key]
		def _mangle(seg: str) -> str:
			out = []
			for ch in seg:
				if ch.isalnum() or ch == "_":
					out.append(ch)
				else:
					out.append(f"_{ord(ch):02X}")
			return "".join(out) if out else "main"
		safe_mod = _mangle(mod)
		safe_name = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
		suffix = f"{hash64(type_key.encode()):016x}"
		llvm_name = f"%Struct_{safe_mod}_{safe_name}_{suffix}"
		# Insert into cache before mapping fields to allow self-recursive pointer
		# shapes like `struct Node { next: &Node }` to refer to the named type.
		self._struct_types_by_name[cache_key] = llvm_name
		struct_inst = type_table.get_struct_instance(ty_id)
		field_types = list(struct_inst.field_types) if struct_inst is not None else list(td.param_types)
		field_lltys = [map_type(ft) for ft in field_types]
		body = ", ".join(field_lltys) if field_lltys else ""
		self.type_decls.append(f"{llvm_name} = type {{ {body} }}")
		return llvm_name

	def ensure_variant_type(
		self,
		ty_id: TypeId,
		*,
		payload_words: int,
		payload_cell_llty: str,
		payload_align_bytes: int,
		type_table: TypeTable,
	) -> str:
		"""
		Ensure a concrete variant TypeId is declared as a named LLVM type.

		Variant ABI is compiler-private in v1, but we still want a stable,
		readable named type in the emitted module for debugging and to avoid
		repeating literal struct types everywhere.

		Internal representation (v1):
		  %Variant_<module>_<name>_<hash> = type { i8 tag, [pad x i8] pad, [payload_words x <cell>] payload }

		The pad ensures the payload begins at a payload-aligned offset (accounting
		for any wider field alignments such as Float on 32-bit targets).
		"""
		type_key = type_table.type_key_string(ty_id)
		if type_key in self._variant_types_by_key:
			return self._variant_types_by_key[type_key]
		td = type_table.get(ty_id)
		mod = td.module_id or ""
		name = td.name
		def _mangle(seg: str) -> str:
			out = []
			for ch in seg:
				if ch.isalnum() or ch == "_":
					out.append(ch)
				else:
					out.append(f"_{ord(ch):02X}")
			return "".join(out) if out else "main"
		safe_mod = _mangle(mod)
		safe_name = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
		suffix = f"{hash64(type_key.encode()):016x}"
		payload_words = max(1, int(payload_words))
		payload_align_bytes = max(1, int(payload_align_bytes))
		if payload_align_bytes & (payload_align_bytes - 1):
			raise AssertionError("variant payload alignment must be a power of two")
		pad_len = max(0, payload_align_bytes - 1)
		llvm_name = f"%Variant_{safe_mod}_{safe_name}_{suffix}"
		self._variant_types_by_key[type_key] = llvm_name
		self.type_decls.append(
			f"{llvm_name} = type {{ i8, [{pad_len} x i8], [{payload_words} x {payload_cell_llty}] }}"
		)
		return llvm_name

	def fnresult_type(self, ok_key: str, ok_llty: str, ok_typeid: TypeId | None = None) -> str:
		"""
		Return the LLVM struct type for FnResult<ok_llty, Error>.

		We emit named types per ok payload for readability/ABI stability. Supported
		ok payloads in v1 include:
		  - Int (isize), String (%DriftString), Void-like (i8), Ref<T> (T*)
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
		return self._declare_fnresult_named_type(ok_key, ok_llty, ok_typeid=ok_typeid)

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
		emit_ok_llty = self._llty(ok_llty)
		lines.append(f"define {emit_ok_llty} {name}({fnres_llty} %res) {{")
		lines.append("__bb_entry:")
		lines.append(f"  %is_err_raw = extractvalue {fnres_llty} %res, 0")
		lines.append("  %is_err = icmp ne i8 %is_err_raw, 0")
		lines.append("  br i1 %is_err, label %__bb_trap, label %__bb_ok")
		lines.append("__bb_trap:")
		lines.append("  call void @llvm.trap()")
		lines.append("  unreachable")
		lines.append("__bb_ok:")
		lines.append(f"  %okv = extractvalue {fnres_llty} %res, 1")
		lines.append(f"  ret {emit_ok_llty} %okv")
		lines.append("}")
		self.funcs.append("\n".join(lines))
		return name

	def _declare_fnresult_named_type(self, ok_key: str, ok_llty: str, name: str | None = None, *, ok_typeid: TypeId | None = None) -> str:
		"""Declare and cache a named FnResult type for the given ok payload.

		Some callers (notably `_ensure_nothrow_wrap_thunk`) pass the raw
		`type_table.type_key_string(...)` for generic ok payloads, which
		contains `<`, `>`, `:`, `.` — characters illegal in LLVM type
		identifiers.  Sanitize to `_` and append a hash suffix when
		sanitization is non-identity, so distinct keys can't collide on
		the same mangled name.
		"""
		if ok_key in self._fnresult_types_by_key:
			return self._fnresult_types_by_key[ok_key]
		if name is None:
			raw = ok_key.lstrip('%')
			safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in raw)
			if safe == raw:
				type_name = f"%FnResult_{safe}_Error"
			else:
				suffix = f"{hash64(ok_key.encode()):016x}"
				type_name = f"%FnResult_{safe}_{suffix}_Error"
		else:
			type_name = name
		emit_ok_llty = self._llty(ok_llty)
		self.type_decls.append(f"{type_name} = type {{ i8, {emit_ok_llty}, {DRIFT_ERROR_PTR} }}")
		self._fnresult_types_by_key[ok_key] = type_name
		self._fnresult_ok_llty_by_type[type_name] = ok_llty
		if ok_typeid is not None:
			self._fnresult_ok_typeid_by_type[type_name] = ok_typeid
		return type_name

	def llvm_type_for_typeid(self, ty_id: TypeId, type_table: TypeTable) -> str:
		"""Map a TypeId to an LLVM type string using the module's type table.

		Module-level equivalent of _FuncBuilder._llvm_type_for_typeid.
		Covers type kinds needed for wrapper param/return types. Known gaps:
		simplified forward nominal canonicalization, approximate variant layout.
		"""
		from lang.driftc.core.types_core import TypeKind as _TK
		td = type_table.get(ty_id)
		if td.kind is _TK.FORWARD_NOMINAL:
			resolved = (
				type_table.get_nominal(kind=_TK.STRUCT, module_id=td.module_id, name=td.name)
				or type_table.get_nominal(kind=_TK.VARIANT, module_id=td.module_id, name=td.name)
				or type_table.get_nominal(kind=_TK.INTERFACE, module_id=td.module_id, name=td.name)
			)
			if resolved is not None:
				ty_id = resolved
				td = type_table.get(ty_id)
		if type_table.is_void(ty_id):
			return "i8"
		if td.kind is _TK.ARRAY:
			return "%DriftArrayHeader"
		if td.kind is _TK.STRUCT and td.name == "MaybeUninit" and td.module_id == "std.mem":
			inst = type_table.get_struct_instance(ty_id)
			if inst is not None and inst.type_args:
				return self.llvm_type_for_typeid(inst.type_args[0], type_table)
			if td.param_types:
				return self.llvm_type_for_typeid(td.param_types[0], type_table)
		if td.kind is _TK.SCALAR:
			_MAP = {
				"Int": DRIFT_INT_TYPE, "Uint": DRIFT_USIZE_TYPE,
				"Uint64": DRIFT_U64_TYPE, "u64": DRIFT_U64_TYPE,
				"Int32": "i32", "Uint32": "i32",
				"Bool": "i1", "Byte": "i8",
				"Float": "double" if self.float_bits == 64 else "float",
				"String": DRIFT_STRING_TYPE,
			}
			if td.name in _MAP:
				return _MAP[td.name]
		if td.kind is _TK.REF or td.kind is _TK.RAW_PTR:
			return "ptr"
		if td.kind is _TK.FUNCTION:
			# Must mirror _FuncBuilder._llvm_type_for_typeid: throwing Fn is the
			# fat {adapter, env} pair everywhere; nothrow Fn is a thin fn ptr.
			return DRIFT_FAT_FNPTR_TYPE if td.can_throw() else "ptr"
		if td.kind is _TK.STRUCT:
			def _storage_map(tid: TypeId) -> str:
				sty = self.llvm_type_for_typeid(tid, type_table)
				std = type_table.get(tid)
				if std.kind is _TK.SCALAR and std.name == "Bool":
					return "i8"
				return self._llty(sty)
			return self.ensure_struct_type(ty_id, type_table=type_table, map_type=_storage_map)
		if td.kind is _TK.INTERFACE:
			return DRIFT_IFACE_TYPE
		if td.kind is _TK.VARIANT:
			return self._ensure_variant_layout(ty_id, type_table)
		if td.kind is _TK.ERROR:
			return DRIFT_ERROR_PTR
		if td.kind is _TK.FNRESULT and td.param_types and len(td.param_types) >= 2:
			ok_tid = td.param_types[0]
			ok_llty = self.llvm_type_for_typeid(ok_tid, type_table)
			raw_key = type_table.type_key_string(ok_tid)
			ok_key = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in raw_key)
			ok_key = f"{ok_key}_{hash64(raw_key.encode()):016x}"
			return self.fnresult_type(ok_key, ok_llty, ok_typeid=ok_tid)
		raise NotImplementedError(f"LLVM module type mapping: unsupported TypeId {ty_id} kind={td.kind.name} name={td.name}")

	def _ensure_variant_layout(self, ty_id: TypeId, type_table: TypeTable) -> str:
		from lang.driftc.core.types_core import TypeKind as _TK
		td = type_table.get(ty_id)
		key = type_table.type_key_string(ty_id)
		safe_key = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in key)
		suffix = f"{hash64(key.encode()):016x}"
		type_name = f"%Variant_{safe_key}_{suffix}"
		if type_name in self._variant_type_cache:
			return type_name
		inst = type_table.get_variant_instance(ty_id)
		if inst is None:
			schema = type_table.variant_schemas.get(ty_id)
			if schema is not None and not schema.type_params:
				self.type_decls.append(f"{type_name} = type {{ i8, [7 x i8] }}")
				self._variant_type_cache[type_name] = True
				return type_name
			raise NotImplementedError(f"variant {td.name} has no instance for layout")
		max_payload = 0
		for arm in inst.arms:
			arm_size = 0
			for fty in arm.field_types:
				fllty = self.llvm_type_for_typeid(fty, type_table)
				arm_size += self._llvm_type_size_approx(fllty)
			if arm_size > max_payload:
				max_payload = arm_size
		payload_bytes = max(8, ((max_payload + 7) // 8) * 8)
		pad = payload_bytes - 1
		self.type_decls.append(f"{type_name} = type {{ i8, [{pad} x i8] }}")
		self._variant_type_cache[type_name] = True
		return type_name

	def _llvm_type_size_approx(self, llty: str) -> int:
		if llty in ("i1", "i8"):
			return 1
		if llty == "i32":
			return 4
		if llty in (DRIFT_INT_TYPE, "i64", "double", "ptr"):
			return 8
		if llty == DRIFT_FAT_FNPTR_TYPE:
			# Throwing Fn values are the fat {adapter, env} pair — two ptrs.
			return 16
		if llty == DRIFT_STRING_TYPE:
			return 16
		if llty == DRIFT_ERROR_PTR:
			return 8
		if llty.startswith("%DriftArrayHeader"):
			return 32
		if llty.startswith("%DriftIface"):
			return 40
		if llty.startswith("%Struct_") or llty.startswith("%Variant_"):
			return 64
		return 8

	def emit_func(self, text: str) -> None:
		self.funcs.append(text)

	def add_extern_c_declare(self, fn_info: FnInfo, type_table: Optional[TypeTable] = None) -> None:
		"""Emit a bare ``declare`` for an ``extern "C"`` function."""
		sig = fn_info.signature
		if sig is None:
			return
		# Build a lightweight type-mapper so we can resolve TypeId → LLVM type.
		# We only need _llvm_type_for_typeid which lives on _FuncBuilder, but the
		# module already carries _llty and word_bits.  For the extern-C declare we
		# need a minimal _FuncBuilder just for type mapping.
		param_parts: list[str] = []
		param_tids = list(sig.param_type_ids or [])
		for tid in param_tids:
			llty = _extern_c_llvm_type(tid, type_table, self)
			param_parts.append(self._llty(llty))
		params_str = ", ".join(param_parts)
		ret_tid = sig.return_type_id
		if ret_tid is not None and type_table is not None and type_table.is_void(ret_tid):
			ret_llty_str = "void"
		elif ret_tid is not None:
			ret_llty_str = self._llty(_extern_c_llvm_type(ret_tid, type_table, self))
		else:
			ret_llty_str = "void"
		# Use the raw function name (no Drift mangling) as the C symbol.
		c_symbol = sig.name
		decl = f"declare {ret_llty_str} @{c_symbol}({params_str})"
		# Two modules in one compilation unit may declare the same C symbol
		# (e.g. both declare `usleep`).  LLVM rejects a repeated `declare`
		# for the same symbol even when identical — dedup exact repeats.
		# A repeat with a DIFFERENT signature is a genuine conflict and is
		# left to fail at the LLVM level as before.
		if decl in self._extern_c_declares:
			return
		self._extern_c_declares.append(decl)

	def ensure_comdat(self, name: str) -> None:
		self.comdats.add(name)

	def emit_entry_wrapper(self, drift_main: str = "drift_main", install_process_preamble: bool = False, root_vt: bool = True) -> None:
		"""
		Emit a tiny OS entrypoint wrapper that calls `@drift_main` and truncs to i32.

		When root_vt=True (default), wraps the call through drift_run_main_on_vt
		so user main executes on a VT fiber.  When root_vt=False, calls drift_main
		directly (for codegen unit tests that don't link the C runtime).
		"""
		lines = [
			"define i32 @main() {",
			"__bb_entry:",
		]
		if install_process_preamble:
			lines.append("  %pre = call i1 @\"std.io::install_process_preamble__impl\"()")
		if root_vt:
			self.needs_run_main_on_vt = True
			lines.extend(
				[
					f"  %ret = call {self._llty(DRIFT_INT_TYPE)} @drift_run_main_on_vt(ptr {_llvm_fn_sym(drift_main)})",
					f"  %trunc = trunc {self._llty(DRIFT_INT_TYPE)} %ret to i32",
					"  ret i32 %trunc",
					"}",
				]
			)
		else:
			lines.extend(
				[
					f"  %ret = call {self._llty(DRIFT_INT_TYPE)} {_llvm_fn_sym(drift_main)}()",
					f"  %trunc = trunc {self._llty(DRIFT_INT_TYPE)} %ret to i32",
					"  ret i32 %trunc",
					"}",
				]
			)
		self.funcs.append("\n".join(lines))

	def emit_argv_entry_wrapper(self, user_main: str, array_type: str, install_process_preamble: bool = False, root_vt: bool = True) -> None:
		"""
		Emit an OS entry for `main(argv: Array<String>) -> Int`.

		When root_vt=True (default), emits a thunk that captures argc/argv in
		globals and routes through drift_run_main_on_vt so user main runs on a VT.
		"""
		self.needs_argv_helper = True
		self.array_string_type = array_type

		if root_vt:
			self.needs_run_main_on_vt = True
			# Emit globals for passing argc/argv into the thunk.
			self.consts.append(f"@drift_root_argc = internal global i32 0")
			self.consts.append(f"@drift_root_argv = internal global ptr null")

			# Emit thunk: reads argc/argv from globals, builds array, calls user main.
			thunk_lines = [
				f"define internal {self._llty(DRIFT_INT_TYPE)} @drift_main_argv_thunk() {{",
				"__bb_entry:",
				"  %argc = load i32, ptr @drift_root_argc",
				"  %argv = load ptr, ptr @drift_root_argv",
				"  %arr.ptr = alloca %DriftArrayHeader",
				"  call void @drift_build_argv(ptr %arr.ptr, i32 %argc, ptr %argv)",
				"  %arr = load %DriftArrayHeader, ptr %arr.ptr",
				f"  %len = extractvalue %DriftArrayHeader %arr, {ARRAY_LEN_IDX}",
				f"  %cap = extractvalue %DriftArrayHeader %arr, {ARRAY_CAP_IDX}",
				f"  %gen = extractvalue %DriftArrayHeader %arr, {ARRAY_GEN_IDX}",
				f"  %data_raw = extractvalue %DriftArrayHeader %arr, {ARRAY_PTR_IDX}",
				f"  %tmp0 = insertvalue {array_type} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} %len, {ARRAY_LEN_IDX}",
				f"  %tmp1 = insertvalue {array_type} %tmp0, {self._llty(DRIFT_INT_TYPE)} %cap, {ARRAY_CAP_IDX}",
				f"  %tmp2 = insertvalue {array_type} %tmp1, {self._llty(DRIFT_INT_TYPE)} %gen, {ARRAY_GEN_IDX}",
				f"  %argv_typed = insertvalue {array_type} %tmp2, ptr %data_raw, {ARRAY_PTR_IDX}",
				f"  %ret = call {self._llty(DRIFT_INT_TYPE)} {_llvm_fn_sym(user_main)}({array_type} %argv_typed)",
				f"  ret {self._llty(DRIFT_INT_TYPE)} %ret",
				"}",
			]
			self.funcs.append("\n".join(thunk_lines))

			# Emit @main that stores argc/argv and routes through root VT.
			lines = [
				"define i32 @main(i32 %argc, ptr %argv) {",
				"__bb_entry:",
				"  store i32 %argc, ptr @drift_root_argc",
				"  store ptr %argv, ptr @drift_root_argv",
			]
			if install_process_preamble:
				lines.append("  %pre = call i1 @\"std.io::install_process_preamble__impl\"()")
			lines.extend(
				[
					f"  %ret = call {self._llty(DRIFT_INT_TYPE)} @drift_run_main_on_vt(ptr @drift_main_argv_thunk)",
					f"  %trunc = trunc {self._llty(DRIFT_INT_TYPE)} %ret to i32",
					"  ret i32 %trunc",
					"}",
				]
			)
			self.funcs.append("\n".join(lines))
		else:
			lines = [
				"define i32 @main(i32 %argc, ptr %argv) {",
				"__bb_entry:",
			]
			if install_process_preamble:
				lines.append("  %pre = call i1 @\"std.io::install_process_preamble__impl\"()")
			lines.extend(
				[
					"  %arr.ptr = alloca %DriftArrayHeader",
					"  call void @drift_build_argv(ptr %arr.ptr, i32 %argc, ptr %argv)",
					"  %arr = load %DriftArrayHeader, ptr %arr.ptr",
					f"  %len = extractvalue %DriftArrayHeader %arr, {ARRAY_LEN_IDX}",
					f"  %cap = extractvalue %DriftArrayHeader %arr, {ARRAY_CAP_IDX}",
					f"  %gen = extractvalue %DriftArrayHeader %arr, {ARRAY_GEN_IDX}",
					f"  %data_raw = extractvalue %DriftArrayHeader %arr, {ARRAY_PTR_IDX}",
					f"  %tmp0 = insertvalue {array_type} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} %len, {ARRAY_LEN_IDX}",
					f"  %tmp1 = insertvalue {array_type} %tmp0, {self._llty(DRIFT_INT_TYPE)} %cap, {ARRAY_CAP_IDX}",
					f"  %tmp2 = insertvalue {array_type} %tmp1, {self._llty(DRIFT_INT_TYPE)} %gen, {ARRAY_GEN_IDX}",
					f"  %argv_typed = insertvalue {array_type} %tmp2, ptr %data_raw, {ARRAY_PTR_IDX}",
					f"  %ret = call {self._llty(DRIFT_INT_TYPE)} {_llvm_fn_sym(user_main)}({array_type} %argv_typed)",
					f"  %trunc = trunc {self._llty(DRIFT_INT_TYPE)} %ret to i32",
					"  ret i32 %trunc",
					"}",
				]
			)
			self.funcs.append("\n".join(lines))

	def emit_abi_stamp(self) -> None:
		"""Register the ABI version marker via an LLVM module-level constructor.

		This ensures every codegen unit carries the stamp regardless of whether
		an OS entry wrapper is emitted.  Safe to call more than once (idempotent)."""
		if getattr(self, "_abi_version_sym", None):
			return
		from lang.driftc.driftc_versions import DRIFT_RT_ABI_VERSION
		self._abi_version_sym = f"__drift_rt_abi_version_{DRIFT_RT_ABI_VERSION}"
		self._global_ctors.append("@__drift_abi_check")

	def emit_build_info(
		self,
		*,
		git_sha: str = "",
		build_profile: str = "",
		artifact: dict | None = None,
		dependencies: dict | None = None,
		extra: dict | None = None,
	) -> None:
		"""Emit the drift-build-info/v1 stamp (PLAN §2.1/§2.4).

		Two artifacts from one canonical JSON document:
		- `self._build_info_payload` — served by the `std.meta.build_info`
		  intrinsic as a baked string constant;
		- the `.drift_build_info` SECTION constant (exactly the
		  canonical JSON bytes) — the external read path
		  (`drift inspect build-info`), registered in @llvm.used so
		  linking and optimization keep it.
		"""
		if getattr(self, "_build_info_emitted", False):
			return
		self._build_info_emitted = True
		# Function-local import: the schema/section contract lives in
		# backend-neutral lang.build_info (the supported reader
		# must not depend on the LLVM backend); deferring the import
		# keeps the module graph acyclic.
		from lang.build_info import (
			BUILD_INFO_SECTION,
			BUILD_INFO_SYMBOL,
			assemble_build_info,
			encode_build_info,
		)
		import datetime
		build_utc = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
		payload = assemble_build_info(
			git_sha=git_sha,
			word_bits=self.word_bits,
			build_profile=build_profile,
			build_utc=build_utc,
			artifact=artifact,
			dependencies=dependencies or {},
			extra=extra or {},
		)
		self._build_info_payload = payload
		# The parsed document is the single source the scalar accessor
		# intrinsic arms read from (never flags/constants directly).
		import json as _json
		self._build_info_doc = _json.loads(payload)
		# Section contract: EXACTLY the canonical JSON bytes — the
		# executable's section header is the framing (identity, offset,
		# exact length); no magic/version/length/NUL of our own.
		raw = encode_build_info(payload)
		n = len(raw)
		byte_csv = ", ".join(f"i8 {b}" for b in raw)
		self.consts.append(
			f'@{BUILD_INFO_SYMBOL} = internal constant [{n} x i8] '
			f'[{byte_csv}], section "{BUILD_INFO_SECTION}", align 1'
		)
		self._llvm_used.append(f'ptr @{BUILD_INFO_SYMBOL}')

	def render(self) -> str:
		lines: List[str] = []
		lines.extend(self.type_decls)
		lines.append("")
		if self.consts:
			lines.extend(self.consts)
			lines.append("")
		if self.comdats:
			for name in sorted(self.comdats):
				lines.append(f"{_llvm_comdat_sym(name)} = comdat any")
			lines.append("")
		if self.needs_argv_helper:
			array_type = self.array_string_type or f"{{ {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, ptr }}"
			lines.append("declare void @drift_build_argv(ptr, i32, ptr)")
			lines.append("")
		if self.needs_run_main_on_vt:
			lines.append(f"declare {self._llty(DRIFT_INT_TYPE)} @drift_run_main_on_vt(ptr)")
			lines.append("")
		if self.needs_array_helpers:
			lines.extend(
				[
					f"declare ptr @drift_alloc_array({self._llty(DRIFT_USIZE_TYPE)}, {self._llty(DRIFT_USIZE_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					"declare void @drift_free_array(ptr)",
					"declare void @drift_cb_env_free(ptr)",
					f"declare void @drift_bounds_check({DRIFT_STRING_TYPE}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_bounds_check_fail({DRIFT_STRING_TYPE}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_array_byte_commit_init_len(ptr, {self._llty(DRIFT_INT_TYPE)})",
					"",
				]
			)
		if self.needs_iface_helpers:
			lines.extend(
				[
					f"declare ptr @malloc({self._llty(DRIFT_USIZE_TYPE)})",
					"declare void @free(ptr)",
					f"define weak ptr @drift_iface_alloc({self._llty(DRIFT_USIZE_TYPE)} %size, {self._llty(DRIFT_USIZE_TYPE)} %align) {{",
					"__bb_entry:",
					f"  %p = call ptr @malloc({self._llty(DRIFT_USIZE_TYPE)} %size)",
					"  ret ptr %p",
					"}",
					"define weak void @drift_iface_free(ptr %p) {",
					"__bb_entry:",
					"  call void @free(ptr %p)",
					"  ret void",
					"}",
					"",
				]
			)
		if self.needs_memcpy:
			lines.append("declare void @llvm.memcpy.p0.p0.i64(ptr, ptr, i64, i1)")
		if self.needs_string_eq:
			lines.append(f"declare i1 @drift_string_eq({DRIFT_STRING_TYPE}, {DRIFT_STRING_TYPE})")
		if self.needs_string_cmp:
			lines.append(f"declare i32 @drift_string_cmp({DRIFT_STRING_TYPE}, {DRIFT_STRING_TYPE})")
		if self.needs_string_concat:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_concat({DRIFT_STRING_TYPE}, {DRIFT_STRING_TYPE})")
		if self.needs_string_ffi_bridge:
			lines.extend([
				f"declare {self._llty(DRIFT_INT_TYPE)} @drift_string_interior_nul_index(ptr)",
				"declare ptr @drift_string_to_owned_cstr(ptr, ptr)",
				f"declare {{ ptr, {self._llty(DRIFT_INT_TYPE)} }} @drift_string_to_owned_cbytes(ptr)",
				"declare ptr @drift_string_to_owned_cstr_unchecked(ptr)",
				"declare void @drift_cstr_free(ptr)",
				f"declare void @drift_cbytes_free({{ ptr, {self._llty(DRIFT_INT_TYPE)} }})",
			])
		if self.needs_string_observe_guard:
			# B-repr/B5 §2.6 OBSERVATION contract, enforced at the
			# compiler's own three layout-authority observation paths
			# (StringLen / StringByteAt / StringBytesBase): the reserved
			# all-zero tombstone and malformed handles FAIL CLOSED before
			# any length/storage use — matching the runtime accessors.
			# One internal alwaysinline guard per module; LLVM inlines and
			# hoists it, and the cold arms never merge into the hot path.
			word = self._llty(DRIFT_INT_TYPE)
			def _obs_msg(name: str, text: str) -> None:
				data = text.encode("utf-8")
				esc = "".join(_escape_byte(b) for b in data) + "\\00"
				lines.append(
					f"@{name} = private unnamed_addr constant [{len(data) + 1} x i8] c\"{esc}\""
				)
			_obs_msg("__drift_obs_msg_tomb", "String tombstone observed (byte access)")
			_obs_msg("__drift_obs_msg_malformed", "malformed String handle: nonzero len, NULL storage")
			_obs_msg("__drift_obs_msg_neg", "malformed String handle: negative len")
			_obs_msg("__drift_obs_msg_rsv", "String flags: reserved bit set")
			_obs_msg("__drift_obs_msg_si", "String flags: STATIC+IMMORTAL")
			_obs_msg("__drift_obs_msg_orphan", "String flags: HAS_INTERIOR_NUL without NUL_SCANNED")
			lines.extend([
				"declare void @drift_contract_fail(ptr)",
				# COLD, OUTLINED fail dispatch: keeps the six contract
				# messages EXACT while keeping the hot guard's inline
				# cost tiny — with the fail arms inlined into the guard,
				# every small String accessor blew LLVM's inline
				# threshold (cost 260 vs 225 measured on byte_length) and
				# NOTHING in the stdlib inlined into callers.
				"define internal void @__drift_string_observe_fail(i64 %code) noinline cold {",
				"__bb_entry:",
				"  switch i64 %code, label %__bb_c0 [ i64 1, label %__bb_c1 i64 2, label %__bb_c2 i64 3, label %__bb_c3 i64 4, label %__bb_c4 i64 5, label %__bb_c5 ]",
				"__bb_c0:",
				"  call void @drift_contract_fail(ptr @__drift_obs_msg_tomb)",
				"  unreachable",
				"__bb_c1:",
				"  call void @drift_contract_fail(ptr @__drift_obs_msg_malformed)",
				"  unreachable",
				"__bb_c2:",
				"  call void @drift_contract_fail(ptr @__drift_obs_msg_neg)",
				"  unreachable",
				"__bb_c3:",
				"  call void @drift_contract_fail(ptr @__drift_obs_msg_rsv)",
				"  unreachable",
				"__bb_c4:",
				"  call void @drift_contract_fail(ptr @__drift_obs_msg_si)",
				"  unreachable",
				"__bb_c5:",
				"  call void @drift_contract_fail(ptr @__drift_obs_msg_orphan)",
				"  unreachable",
				"}",
				# Hot guard: branch-lean predicates + ONE cold call site.
				# Same checks, same order of precedence, same messages
				# (via the dispatch codes), same fail-closed behavior in
				# both builds.  Flags word: u64 at storage offset 8 (this
				# guard is part of the codegen layout authority); relaxed
				# atomic load.
				f"define internal void @__drift_string_observe_guard({word} %len, ptr %storage) alwaysinline {{",
				"__bb_entry:",
				"  %isnull = icmp eq ptr %storage, null",
				"  br i1 %isnull, label %__bb_null, label %__bb_nonnull",
				"__bb_null:",
				f"  %nz = icmp ne {word} %len, 0",
				"  %code0 = select i1 %nz, i64 1, i64 0",
				"  call void @__drift_string_observe_fail(i64 %code0)",
				"  unreachable",
				"__bb_nonnull:",
				# NEGATIVE length is rejected BEFORE any storage
				# dereference: a malformed {negative len, invalid
				# non-NULL storage} handle must produce the pinned
				# negative-length contract failure, never a fault on
				# the flags load.
				f"  %neg = icmp slt {word} %len, 0",
				"  br i1 %neg, label %__bb_negf, label %__bb_flags",
				"__bb_negf:",
				"  call void @__drift_string_observe_fail(i64 2)",
				"  unreachable",
				"__bb_flags:",
				"  %flags_ptr = getelementptr i8, ptr %storage, i64 8",
				"  %flags = load atomic i64, ptr %flags_ptr monotonic, align 8",
				"  %rsv = and i64 %flags, -16",
				"  %rsv_set = icmp ne i64 %rsv, 0",
				"  %si = and i64 %flags, 3",
				"  %si_both = icmp eq i64 %si, 3",
				"  %nc = and i64 %flags, 12",
				"  %nc_orphan = icmp eq i64 %nc, 8",
				"  %bad1 = or i1 %rsv_set, %si_both",
				"  %bad = or i1 %bad1, %nc_orphan",
				"  br i1 %bad, label %__bb_fail, label %__bb_ok",
				"__bb_fail:",
				# precedence mirrors the original chain: rsv > si > orphan
				"  %c5 = select i1 %nc_orphan, i64 5, i64 0",
				"  %c4 = select i1 %si_both, i64 4, i64 %c5",
				"  %code = select i1 %rsv_set, i64 3, i64 %c4",
				"  call void @__drift_string_observe_fail(i64 %code)",
				"  unreachable",
				"__bb_ok:",
				"  ret void",
				"}",
			])
		if self.needs_string_from_int64:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_int64(i64)")
		if self.needs_string_from_uint64:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_uint64(i64)")
		if self.needs_string_from_bool:
			# Runtime takes an `int` (i32) for portability; caller must extend i1.
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_bool(i32)")
		if self.needs_string_from_f64:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_f64(double)")
		if self.needs_string_from_utf8_bytes:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_from_utf8_bytes(ptr, {self._llty(DRIFT_INT_TYPE)})")
		if self.needs_string_retain:
			lines.append(f"declare {DRIFT_STRING_TYPE} @drift_string_retain({DRIFT_STRING_TYPE})")
		if self.needs_string_release:
			lines.append(f"declare void @drift_string_release({DRIFT_STRING_TYPE})")
		if (
			self.needs_string_eq
			or self.needs_string_cmp
			or self.needs_string_concat
			or self.needs_string_from_int64
			or self.needs_string_from_uint64
			or self.needs_string_from_bool
			or self.needs_string_from_f64
			or self.needs_string_from_utf8_bytes
			or self.needs_string_retain
			or self.needs_string_release
		):
			lines.append("")
		if self.needs_console_runtime:
			lines.extend(
				[
					f"declare void @drift_console_write({DRIFT_STRING_TYPE})",
					f"declare void @drift_console_writeln({DRIFT_STRING_TYPE})",
					f"declare void @drift_console_eprint({DRIFT_STRING_TYPE})",
					f"declare void @drift_console_eprintln({DRIFT_STRING_TYPE})",
					"",
				]
			)
		if self.needs_thread_runtime:
			lines.extend(
				[
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_thread_spawn(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_thread_join({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_thread_join_timeout({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_thread_is_completed({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_thread_cancel({self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_thread_drop({self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_exec_submit_test_override({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_exec_get_running({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_thread_current()",
					f"declare void @drift_thread_park({self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_thread_park_until({self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_thread_set_wait({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_thread_unpark({self._llty(DRIFT_INT_TYPE)})",
					"declare void @drift_thread_yield()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_exec_default_get()",
					f"declare void @drift_exec_default_set({self._llty(DRIFT_INT_TYPE)})",
				f"declare {self._llty(DRIFT_INT_TYPE)} @drift_exec_create({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_exec_submit({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_exec_set_name({self._llty(DRIFT_INT_TYPE)}, {DRIFT_STRING_TYPE})",
					f"declare void @drift_vt_set_op({self._llty(DRIFT_INT_TYPE)}, {DRIFT_STRING_TYPE})",
					"declare void @drift_ffi_enter(ptr)",
					"declare void @drift_ffi_exit()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_default_get()",
					f"declare void @drift_reactor_default_set({self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_reactor_register_io({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_reactor_register_timer({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_check_pending({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_io_charge({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_vt_wait_epoch_begin({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_wait_register({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_reactor_wait_clear({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_wait_collect_pending({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_wait_park({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_stale_epoch_drops_get()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_close_unparks_get()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_reactor_park_blocks_get()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_io_open({DRIFT_STRING_TYPE}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_io_close({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_io_read({self._llty(DRIFT_INT_TYPE)}, ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_io_write({self._llty(DRIFT_INT_TYPE)}, ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_io_errno()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_io_set_nonblocking({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_peek_readable({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_fs_read_dir({DRIFT_STRING_TYPE}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_fs_result_status({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_fs_result_errno({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_fs_result_count({self._llty(DRIFT_INT_TYPE)})",
					f"declare {DRIFT_STRING_TYPE} @drift_fs_result_name({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_fs_result_kind({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_fs_result_free({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_fs_test_walk_entries()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_vt_test_direct_resume_claims()",
					f"declare ptr @drift_runtime_global_registry_ptr()",
					f"declare ptr @drift_runtime_thread_registry_ptr()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_runtime_registry_set(i64, ptr, ptr byval({DRIFT_IFACE_TYPE}) align {self.word_bits // 8})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_runtime_registry_contains(i64)",
					f"declare ptr @drift_runtime_registry_get(i64)",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_runtime_thread_registry_set(i64, ptr, ptr byval({DRIFT_IFACE_TYPE}) align {self.word_bits // 8})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_runtime_thread_registry_contains(i64)",
					f"declare ptr @drift_runtime_thread_registry_get(i64)",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_listen(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_accept({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_connect(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_listener_port({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_peer_addr({self._llty(DRIFT_INT_TYPE)}, ptr, ptr)",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_set_nodelay({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_get_nodelay({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_local_port({self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_bind(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_bind_v6(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_send_to({self._llty(DRIFT_INT_TYPE)}, ptr, {self._llty(DRIFT_INT_TYPE)}, ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_send_to_v6({self._llty(DRIFT_INT_TYPE)}, ptr, {self._llty(DRIFT_INT_TYPE)}, ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_recv_from({self._llty(DRIFT_INT_TYPE)}, ptr, {self._llty(DRIFT_INT_TYPE)}, ptr, ptr)",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_recv_from_v6({self._llty(DRIFT_INT_TYPE)}, ptr, {self._llty(DRIFT_INT_TYPE)}, ptr, ptr)",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_time_now_ms()",
					"declare i64 @drift_time_now_us()",
					"declare i64 @drift_time_now_utc_us()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_test_eventfd_create()",
					f"declare void @drift_test_eventfd_write({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_test_timerfd_create()",
					f"declare void @drift_test_timerfd_set({self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_random_fill(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {DRIFT_STRING_TYPE} @drift_env_get({DRIFT_STRING_TYPE})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_env_has({DRIFT_STRING_TYPE})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_signal_await()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_thread_vtid()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_thread_tid()",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_thread_is_cancelled()",
					"",
				]
			)
		if self.needs_atomic_runtime:
			lines.extend(
				[
					f"declare i8 @drift_atomic_load_bool(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_atomic_store_bool(ptr, i8, {self._llty(DRIFT_INT_TYPE)})",
					f"declare i8 @drift_atomic_exchange_bool(ptr, i8, {self._llty(DRIFT_INT_TYPE)})",
					f"declare i8 @drift_atomic_compare_exchange_bool(ptr, i8, i8, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare i8 @drift_atomic_compare_exchange_observed_bool(ptr, i8, i8, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_load_int(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_atomic_store_int(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_exchange_int(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare i8 @drift_atomic_compare_exchange_int(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_compare_exchange_observed_int(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_add_int(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_sub_int(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_load_uint(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_atomic_store_uint(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_exchange_uint(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare i8 @drift_atomic_compare_exchange_uint(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_compare_exchange_observed_uint(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_add_uint(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_sub_uint(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_load_uint64(ptr, {self._llty(DRIFT_INT_TYPE)})",
					f"declare void @drift_atomic_store_uint64(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_exchange_uint64(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
					f"declare i8 @drift_atomic_compare_exchange_uint64(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
						f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_compare_exchange_observed_uint64(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
						f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_add_uint64(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
						f"declare {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_sub_uint64(ptr, {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)})",
						f"declare void @drift_atomic_thread_fence({self._llty(DRIFT_INT_TYPE)})",
						f"declare void @drift_atomic_signal_fence({self._llty(DRIFT_INT_TYPE)})",
						"",
					]
				)
		# Slice 7c-2 (0.31.64, ABI 14, 2026-05-06): the runtime DV
		# exports are gone (Slice 7c-1) and the dead MIR op classes
		# + handlers + `needs_dv_runtime` flag are deleted (Slice
		# 7c-2).  At ABI 14 production codegen never declares or
		# calls `drift_dv_*`, `__exc_*_get_dv`,
		# `drift_error_add_attr_dv`, `drift_error_add_local_dv`,
		# `drift_error_new_with_payload`, or any DV accessor.
		if self.needs_error_runtime:
			self.needs_llvm_trap = True
			lines.extend(
				[
					f"define weak {DRIFT_ERROR_PTR} @drift_error_new({DRIFT_ERROR_CODE_TYPE} %code, {DRIFT_STRING_TYPE} %event) {{",
					"__bb_entry:",
					f"  ret {DRIFT_ERROR_PTR} null",
					"}",
					f"define weak void @drift_error_release({DRIFT_ERROR_PTR} %err) {{",
					"__bb_entry:",
					"  ret void",
					"}",
					f"declare void @drift_error_raise({DRIFT_ERROR_PTR})",
					# Slice 1+ DV→JSON migration / Slice 7b unification:
					# canonical params JSON dump surface and throw-side setter.
					# Both runtime helpers follow ABI spec §2.3 ownership:
					# getter returns retained DriftString (caller releases),
					# setter takes ownership of the input DriftString.
					f"declare {DRIFT_STRING_TYPE} @drift_error_get_params_json({DRIFT_ERROR_PTR})",
					f"declare void @drift_error_set_params_json({DRIFT_ERROR_PTR}, {DRIFT_STRING_TYPE})",
					f"declare {DRIFT_STRING_TYPE} @drift_error_get_context_json({DRIFT_ERROR_PTR})",
					f"declare void @drift_error_append_context_frame({DRIFT_ERROR_PTR}, {DRIFT_STRING_TYPE})",
				]
			)
			lines.append("")
		if self.needs_assert_runtime:
			lines.extend(
				[
					f"declare void @drift_assert_loc(i1, {DRIFT_STRING_TYPE}, {self._llty(DRIFT_INT_TYPE)}, {DRIFT_STRING_TYPE}, {DRIFT_STRING_TYPE})",
					"",
				]
			)
		if self.needs_llvm_trap:
			lines.append("declare void @llvm.trap()")
			lines.append("")
		if self.needs_arc_fat_bump_helper:
			# Stage 3 fat `Arc<Interface>`: `ArcAsInterface` lowering
			# emits a direct `call` to this non-generic stdlib helper
			# (one symbol serves every `Arc<I>` — I is erased at
			# refcount time).  The helper's body lives in
			# `stdlib/std/core/arc.drift` (relocated from
			# `std/concurrent` at ABI 11); when its definition is NOT
			# in the current LLVM module, emit a prototype so the call
			# site has a matching signature for opaque-pointer
			# verification.  Note: in package-consumer builds the
			# reachability pass at `driftc.py::compile_to_llvm_ir`
			# explicitly seeds `_arc_fat_bump_strong_via_ctrl` into
			# the reachable set when any `M.ArcAsInterface` is in
			# scope, so the helper's body IS pulled into the consumer
			# IR and the `define` branch is taken there too -- the
			# declare-only branch is reserved for the rare case where
			# the seed cannot resolve the helper (e.g. tests that
			# bypass the consumer pipeline).  When the definition IS
			# in this module (dev/source build or seed-pulled
			# package-consumer build), skip the declare -- LLVM
			# rejects `declare` + `define` for the same symbol even
			# with identical prototypes.
			#
			# Definition detection is a literal-prefix search over
			# `self.funcs` (each entry is the full IR text of one
			# emitted function).  This is safe today because
			# `_arc_fat_bump_strong_via_ctrl` is a private non-generic
			# Drift helper emitted without linkage decoration — the
			# define line is exactly the prefix we search for.  If
			# linkage / attributes are ever added (e.g. `linkonce_odr`,
			# `comdat`), tighten this to a set-based
			# `defined_symbols: set[str]` tracker populated by each
			# `self.funcs.append(...)` site, rather than string
			# matching on the serialized IR.
			_bump_define_prefix = 'define void @"std.core.arc::_arc_fat_bump_strong_via_ctrl"'
			if not any(_bump_define_prefix in _fn_text for _fn_text in self.funcs):
				lines.append('declare void @"std.core.arc::_arc_fat_bump_strong_via_ctrl"(ptr)')
				lines.append("")
		if self.debug_enabled and self.needs_dbg_intrinsics:
			lines.extend(
				[
					"declare void @llvm.dbg.declare(metadata, metadata, metadata)",
					"declare void @llvm.dbg.value(metadata, metadata, metadata)",
					"",
				]
			)
		if getattr(self, "_abi_version_sym", None):
			lines.append(f"declare void @{self._abi_version_sym}()")
			lines.append("")
			lines.append("define internal void @__drift_abi_check() {")
			lines.append("__bb_entry:")
			lines.append(f"  call void @{self._abi_version_sym}()")
			lines.append("  ret void")
			lines.append("}")
			lines.append("")
		if self._global_ctors:
			n = len(self._global_ctors)
			entries = ", ".join(f"{{ i32, ptr, ptr }} {{ i32 65535, ptr {fn}, ptr null }}" for fn in self._global_ctors)
			lines.append(f"@llvm.global_ctors = appending global [{n} x {{ i32, ptr, ptr }}] [{entries}]")
			lines.append("")
		if self._llvm_used:
			n = len(self._llvm_used)
			entries = ", ".join(self._llvm_used)
			lines.append(f'@llvm.used = appending global [{n} x ptr] [{entries}], section "llvm.metadata"')
			lines.append("")
		if self._extern_c_declares:
			lines.extend(self._extern_c_declares)
			lines.append("")
		lines.extend(self.funcs)
		if self.debug_enabled and self._dbg_compile_unit_id is not None:
			dwarf_flag, dbg_flag = self._ensure_dbg_module_flags()
			lines.append("")
			lines.append(f"!llvm.dbg.cu = !{{!{self._dbg_compile_unit_id}}}")
			lines.append(f"!llvm.module.flags = !{{!{dwarf_flag}, !{dbg_flag}}}")
			lines.extend(self._dbg_metadata)
		lines.append("")
		return "\n".join(lines)


@dataclass(frozen=True)
class _VariantArmLayout:
	"""Per-constructor payload layout for a concrete variant TypeId."""

	tag: int
	# Concrete Drift TypeIds for fields; used for representation coercions.
	field_tys: list[TypeId]
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
	payload_cell_llty: str
	payload_cell_bytes: int
	payload_align_bytes: int
	arms: list[tuple[str, _VariantArmLayout]]
	arm_by_name: Dict[str, _VariantArmLayout]


@dataclass
class _FuncBuilder:
	func: MirFunc
	ssa: SsaFunc
	fn_info: FnInfo
	fn_infos: Mapping[FunctionId, FnInfo]
	module: LlvmModuleBuilder
	# Name mapping hooks used by the module-level emitter for ABI-boundary export
	# wrappers. These maps are purely about symbol selection at call sites.
	#
	# - `export_impl_map` maps an exported FunctionId to its private implementation
	#   symbol name (e.g. `m::foo__impl`).
	# - `rename_map` is a more general FunctionId -> symbol rename table supplied
	#   by the driver (e.g. argv wrappers). Codegen only uses it defensively for
	#   call sites that must target renamed bodies.
	rename_map: Mapping[FunctionId, str] = field(default_factory=dict)
	export_impl_map: Mapping[FunctionId, str] = field(default_factory=dict)
	type_table: Optional[TypeTable] = None
	tmp_counter: int = 0
	lines: List[str] = field(default_factory=list)
	value_map: Dict[str, str] = field(default_factory=dict)
	value_types: Dict[str, str] = field(default_factory=dict)
	param_value_types: Dict[str, str] = field(default_factory=dict)
	const_values: Dict[str, int] = field(default_factory=dict)
	aliases: Dict[str, str] = field(default_factory=dict)
	# Resolved SSA names (post `_map_value`) produced by `VariantGetFieldAddr`
	# in THIS function.  The `ConstructVariant` payload autoload (which loads a
	# struct value out of a pointer argument when the LLVM types mismatch) is
	# permitted ONLY for these provenance-proven field addresses — the
	# borrowed-match reconstruction `match v { V::N(n) => V::N(n) }`.  An
	# arbitrary pointer arriving where a struct value is expected is a broken
	# lowering contract (it previously masked the typed-catch `Error`-into-
	# native-field defect into a double free), so it raises instead.
	variant_field_addr_ptrs: set[str] = field(default_factory=set)
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
	_iface_tmp_alloca: str | None = None
	string_type_id: Optional[TypeId] = None
	int_type_id: Optional[TypeId] = None
	bool_type_id: Optional[TypeId] = None
	float_type_id: Optional[TypeId] = None
	void_type_id: Optional[TypeId] = None
	# Track whether a fn-ptr SSA value is nothrow or throwing. With opaque
	# pointers both map to "ptr" in value_types, so we need this side channel
	# for ConstructStruct fn-ptr adaptation logic.
	_value_fn_throws: Dict[str, bool] = field(default_factory=dict)
	_nothrow_wrap_thunks: Dict[str, bool] = field(default_factory=dict)
	_nothrow_wrap_for: Dict[str, str] = field(default_factory=dict)
	# Slice 7c-3 (ABI 14): `dv_type_id` field deleted along with
	# `TypeKind.DIAGNOSTICVALUE`.
	sym_name: Optional[str] = None
	# Variant lowering caches (compiler-private ABI).
	_variant_layouts: Dict[TypeId, "_VariantLayout"] = field(default_factory=dict)
	_size_align_cache: Dict[TypeId, tuple[int, int]] = field(default_factory=dict)
	_drop_cache: Dict[TypeId, bool] = field(default_factory=dict)
	_dbg_subprogram_id: int | None = None
	_dbg_default_span: Span | None = None
	_dbg_local_ids: Dict[str, int] = field(default_factory=dict)
	_dbg_local_declared: set[str] = field(default_factory=set)
	_dbg_last_span: Span | None = None
	_dbg_keepalive_allocas: Dict[str, str] = field(default_factory=dict)
	_dbg_keepalive_storage_types: Dict[str, str] = field(default_factory=dict)
	_dbg_entry_anchor_emitted: bool = False
	# Maps MIR block name → actual LLVM block name at the time the terminator
	# is emitted.  Instructions that generate new LLVM blocks (e.g. ArrayDup,
	# variant copy) update _current_effective_block so the mapping is correct.
	_block_exit_names: Dict[str, str] = field(default_factory=dict)
	_current_effective_block: str | None = None
	# Slice 7c-2 (ABI 14, 2026-05-06): `_construct_dv_temps` set
	# and `_release_construct_dv_temp` helper deleted along with
	# `M.ConstructDV` and the DV runtime exports.

	def _scratch_alloca(self, llty: str, prefix: str) -> str:
		"""A NONESCAPING scratch stack slot, registered for ENTRY-block
		placement (LLVM marks functions with non-entry allocas "never
		inline: dynamic alloca").  Owning-site contract: every caller
		of this helper materializes a transient value (variant/struct
		pack-unpack, loop counter, callback slot) that is FULLY
		re-initialized by explicit stores before each use and whose
		address never outlives the emitting lowering — so a single
		entry slot reused across loop iterations is
		semantics-preserving by construction.  Allocas whose address
		may escape must NOT use this helper."""
		name = self._fresh(prefix)
		self.entry_allocas.append(f"  {name} = alloca {llty}")
		return name

	def lower(self) -> str:
		self.entry_allocas = []
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
		order = self.ssa.block_order or sorted(self.func.blocks.keys())
		if order and order[0] != self.func.entry:
			order = [self.func.entry] + [b for b in order if b != self.func.entry]
		for block_name in order:
			self._emit_block(block_name)
		# Fix up PHI predecessor labels: instructions that generate new LLVM
		# basic blocks (ArrayDup, variant copy) may have changed the effective
		# exit block for a MIR block, making PHI references stale.
		self._fixup_phi_predecessors()
		self.lines.append("}")
		# Splice registered scratch allocas into the ENTRY block (right
		# after the first label following the define line).
		if self.entry_allocas:
			define_idx = next(i for i, l in enumerate(self.lines) if l.startswith("define "))
			insert_at = None
			for i in range(define_idx + 1, len(self.lines)):
				if self.lines[i].endswith(":") and not self.lines[i].startswith(" "):
					insert_at = i + 1
					break
			if insert_at is None:
				insert_at = define_idx + 1
			self.lines[insert_at:insert_at] = self.entry_allocas
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
					continue
				if isinstance(instr, CopyValue):
					if self.type_table is None:
						continue
					# Bitcopy CopyValue lowers as a pure alias (no IR instruction).
					# Pre-collect it so φ incoming values can resolve through copies
					# defined in later-emitted predecessor blocks.
					if self.type_table.is_bitcopy(instr.ty):
						self.aliases[instr.dest] = instr.value

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

	def _emit_header(self) -> None:
		ret_ty = self._return_llvm_type()
		emit_ret_ty = self._llty(ret_ty)
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
				llty = self._llvm_type_for_typeid(ty_id, allow_void_ok=True)
				emit_llty = self._llty(llty)
				llvm_name = self._map_value(name)
				self.value_types[llvm_name] = llty
				self.param_value_types[llvm_name] = llty
				param_parts.append(f"{emit_llty} {llvm_name}")
		params_str = ", ".join(param_parts)
		func_name = self.sym_name or self.func.name
		is_instantiation = bool(getattr(sig, "is_instantiation", False))
		linkage = " linkonce_odr" if is_instantiation else ""
		comdat = ""
		if is_instantiation:
			self.module.ensure_comdat(func_name)
			comdat = " comdat"
		dbg_suffix = ""
		if self.module.debug_enabled:
			span = Span.from_loc(getattr(self.fn_info.signature, "loc", None)) if self.fn_info.signature is not None else Span()
			self._dbg_default_span = span
			self._dbg_subprogram_id = self.module.get_di_subprogram(func_name, func_name, span)
			if self._dbg_subprogram_id is not None:
				dbg_suffix = f" !dbg !{self._dbg_subprogram_id}"
		# Structural inlinehint: see _inline_hint_eligible (small +
		# accessor/variant-return/cold-failure shape).
		ret_ty_id = self.fn_info.signature.return_type_id if self.fn_info.signature is not None else None
		hint = " inlinehint" if _inline_hint_eligible(self.func, self.type_table, ret_ty_id) else ""
		self.lines.append(f"define{linkage} {emit_ret_ty} {_llvm_fn_sym(func_name)}({params_str}){hint}{comdat}{dbg_suffix} {{")

	def _dbg_local_var(self, local: str, ty_id: TypeId, span: Span | None) -> int | None:
		if not self.module.debug_enabled or self._dbg_subprogram_id is None:
			return None
		if local in self._dbg_local_ids:
			return self._dbg_local_ids[local]
		file_id = self.module._ensure_di_file(span)
		line = span.line if span is not None and span.line is not None else 1
		di_type = self._dbg_type_for_typeid(ty_id, span)
		if di_type is None:
			return None
		local_id = self.module._dbg_new_id()
		self.module._dbg_metadata.append(
			f"!{local_id} = !DILocalVariable(name: \"{_llvm_md_escape(local)}\", scope: !{self._dbg_subprogram_id}, file: !{file_id}, line: {line}, type: !{di_type})"
		)
		self._dbg_local_ids[local] = local_id
		return local_id

	def _dbg_type_for_typeid(self, ty_id: TypeId, span: Span | None) -> int | None:
		if not self.module.debug_enabled or self.type_table is None:
			return None
		if ty_id in self.module._dbg_type_ids:
			return self.module._dbg_type_ids[ty_id]
		td = self.type_table.get(ty_id)
		file_id = self.module._ensure_di_file(span)
		size_bytes, align_bytes = self._size_align_typeid(ty_id)
		size_bits = max(0, size_bytes * 8)
		align_bits = max(0, align_bytes * 8)
		name = getattr(td, "name", None) or self.type_table.type_key_string(ty_id)
		if td.kind is TypeKind.REF:
			inner = td.param_types[0] if td.param_types else None
			base = self._dbg_type_for_typeid(inner, span) if inner is not None else None
			type_id = self.module._dbg_new_id()
			self.module._dbg_metadata.append(
				f"!{type_id} = !DIDerivedType(tag: {DW_TAG_POINTER}, baseType: !{base}, size: {size_bits}, align: {align_bits})"
			)
			self.module._dbg_type_ids[ty_id] = type_id
			return type_id
		if td.kind is TypeKind.SCALAR:
			lower = (td.name or "").lower()
			if lower == "string":
				int_tid = self.type_table.ensure_int()
				byte_tid = self.type_table.ensure_byte()
				len_di = self._dbg_type_for_typeid(int_tid, span)
				byte_di = self._dbg_type_for_typeid(byte_tid, span)
				data_ptr_di = None
				if byte_di is not None:
					data_ptr_di = self.module._dbg_new_id()
					self.module._dbg_metadata.append(
						f"!{data_ptr_di} = !DIDerivedType(tag: {DW_TAG_POINTER}, baseType: !{byte_di}, size: {self.module.word_bits}, align: {self.module.word_bits})"
					)
				struct_id = self.module._dbg_new_id()
				elements_id = self.module._dbg_new_id()
				self.module._dbg_type_ids[ty_id] = struct_id
				word_bytes = max(1, self.module.word_bits // 8)
				len_size_bits = word_bytes * 8
				len_align_bits = word_bytes * 8
				len_member_id = self.module._dbg_new_id()
				self.module._dbg_metadata.append(
					f"!{len_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"len\", scope: !{struct_id}, file: !{file_id}, line: 0, baseType: !{len_di}, size: {len_size_bits}, align: {len_align_bits}, offset: 0)"
				)
				data_member_id = self.module._dbg_new_id()
				data_offset_bits = word_bytes * 8
				self.module._dbg_metadata.append(
					f"!{data_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"data\", scope: !{struct_id}, file: !{file_id}, line: 0, baseType: !{data_ptr_di}, size: {self.module.word_bits}, align: {self.module.word_bits}, offset: {data_offset_bits})"
				)
				self.module._dbg_metadata.append(f"!{elements_id} = !{{!{len_member_id}, !{data_member_id}}}")
				self.module._dbg_metadata.append(
					f"!{struct_id} = distinct !DICompositeType(tag: {DW_TAG_STRUCT}, name: \"{_llvm_md_escape(name)}\", file: !{file_id}, line: 0, size: {size_bits}, align: {align_bits}, elements: !{elements_id})"
				)
				return struct_id
			if lower in ("error", "diagnosticvalue"):
				type_id = self.module._dbg_new_id()
				self.module._dbg_metadata.append(
					f"!{type_id} = !DICompositeType(tag: {DW_TAG_STRUCT}, name: \"{_llvm_md_escape(name)}\", file: !{file_id}, line: 0, size: {size_bits}, align: {align_bits}, elements: !{self.module._ensure_dbg_empty()})"
				)
				self.module._dbg_type_ids[ty_id] = type_id
				return type_id
			if lower in ("int", "i32", "i64", "isize"):
				encoding = DW_ATE_SIGNED
			elif lower in ("uint", "u32", "u64", "usize", "byte"):
				encoding = DW_ATE_UNSIGNED
			elif lower == "bool":
				encoding = DW_ATE_BOOLEAN
			elif lower == "float":
				encoding = DW_ATE_FLOAT
			else:
				encoding = DW_ATE_UNSIGNED
			type_id = self.module._dbg_new_id()
			self.module._dbg_metadata.append(
				f"!{type_id} = !DIBasicType(name: \"{_llvm_md_escape(name)}\", size: {size_bits}, encoding: {encoding})"
			)
			self.module._dbg_type_ids[ty_id] = type_id
			return type_id
		if td.kind is TypeKind.STRUCT:
			struct_id = self.module._dbg_new_id()
			elements_id = self.module._dbg_new_id()
			self.module._dbg_type_ids[ty_id] = struct_id
			inst = self.type_table.get_struct_instance(ty_id)
			field_names = list(inst.field_names) if inst is not None else []
			field_types = list(inst.field_types) if inst is not None else []
			member_ids: list[int] = []
			offset_bytes = 0
			for idx, fty in enumerate(field_types):
				fsz, fal = self._field_size_align_typeid(fty)
				if fal > 0 and offset_bytes % fal != 0:
					offset_bytes += fal - (offset_bytes % fal)
				member_id = self.module._dbg_new_id()
				fname = field_names[idx] if idx < len(field_names) else f"field{idx}"
				base = self._dbg_type_for_typeid(fty, span)
				fsz_bits = fsz * 8
				fal_bits = max(0, fal * 8)
				off_bits = offset_bytes * 8
				self.module._dbg_metadata.append(
					f"!{member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"{_llvm_md_escape(fname)}\", scope: !{struct_id}, file: !{file_id}, line: 0, baseType: !{base}, size: {fsz_bits}, align: {fal_bits}, offset: {off_bits})"
				)
				member_ids.append(member_id)
				offset_bytes += fsz
			if member_ids:
				members = ", ".join(f"!{mid}" for mid in member_ids)
				self.module._dbg_metadata.append(f"!{elements_id} = !{{{members}}}")
			else:
				self.module._dbg_metadata.append(f"!{elements_id} = !{{}}")
			self.module._dbg_metadata.append(
				f"!{struct_id} = distinct !DICompositeType(tag: {DW_TAG_STRUCT}, name: \"{_llvm_md_escape(name)}\", file: !{file_id}, line: 0, size: {size_bits}, align: {align_bits}, elements: !{elements_id})"
			)
			return struct_id
		if td.kind is TypeKind.ARRAY:
			elem_ty = td.param_types[0] if td.param_types else None
			elem_di = self._dbg_type_for_typeid(elem_ty, span) if elem_ty is not None else None
			elem_ptr_di = None
			if elem_di is not None:
				elem_ptr_di = self.module._dbg_new_id()
				self.module._dbg_metadata.append(
					f"!{elem_ptr_di} = !DIDerivedType(tag: {DW_TAG_POINTER}, baseType: !{elem_di}, size: {self.module.word_bits}, align: {self.module.word_bits})"
				)
			struct_id = self.module._dbg_new_id()
			elements_id = self.module._dbg_new_id()
			self.module._dbg_type_ids[ty_id] = struct_id
			word_bytes = max(1, self.module.word_bits // 8)
			word_bits = word_bytes * 8
			int_tid = self.type_table.ensure_int()
			int_di = self._dbg_type_for_typeid(int_tid, span)
			len_member_id = self.module._dbg_new_id()
			self.module._dbg_metadata.append(
				f"!{len_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"len\", scope: !{struct_id}, file: !{file_id}, line: 0, baseType: !{int_di}, size: {word_bits}, align: {word_bits}, offset: 0)"
			)
			cap_member_id = self.module._dbg_new_id()
			self.module._dbg_metadata.append(
				f"!{cap_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"cap\", scope: !{struct_id}, file: !{file_id}, line: 0, baseType: !{int_di}, size: {word_bits}, align: {word_bits}, offset: {word_bits})"
			)
			gen_member_id = self.module._dbg_new_id()
			self.module._dbg_metadata.append(
				f"!{gen_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"gen\", scope: !{struct_id}, file: !{file_id}, line: 0, baseType: !{int_di}, size: {word_bits}, align: {word_bits}, offset: {word_bits * 2})"
			)
			data_member_id = self.module._dbg_new_id()
			self.module._dbg_metadata.append(
				f"!{data_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"data\", scope: !{struct_id}, file: !{file_id}, line: 0, baseType: !{elem_ptr_di}, size: {word_bits}, align: {word_bits}, offset: {word_bits * 3})"
			)
			self.module._dbg_metadata.append(f"!{elements_id} = !{{!{len_member_id}, !{cap_member_id}, !{gen_member_id}, !{data_member_id}}}")
			self.module._dbg_metadata.append(
				f"!{struct_id} = distinct !DICompositeType(tag: {DW_TAG_STRUCT}, name: \"{_llvm_md_escape(name)}\", file: !{file_id}, line: 0, size: {size_bits}, align: {align_bits}, elements: !{elements_id})"
			)
			return struct_id
		if td.kind is TypeKind.VARIANT:
			inst = self.type_table.get_variant_instance(ty_id)
			if inst is None:
				type_id = self.module._dbg_new_id()
				self.module._dbg_metadata.append(
					f"!{type_id} = !DICompositeType(tag: {DW_TAG_STRUCT}, name: \"{_llvm_md_escape(name)}\", file: !{file_id}, line: 0, size: {size_bits}, align: {align_bits}, elements: !{self.module._ensure_dbg_empty()})"
				)
				self.module._dbg_type_ids[ty_id] = type_id
				return type_id
			layout = self._variant_layout(ty_id)
			variant_id = self.module._dbg_new_id()
			variant_elements_id = self.module._dbg_new_id()
			self.module._dbg_type_ids[ty_id] = variant_id
			payload_size_bytes = layout.payload_words * layout.payload_cell_bytes
			payload_align_bytes = layout.payload_align_bytes
			payload_offset = 1
			if payload_align_bytes > 1 and payload_offset % payload_align_bytes != 0:
				payload_offset += payload_align_bytes - (payload_offset % payload_align_bytes)
			schema = self.type_table.get_variant_schema(inst.base_id)
			tombstone_ctor = schema.tombstone_ctor if schema is not None else None
			arms_sorted = [
				arm for arm in inst.arms
				if tombstone_ctor is None or arm.name != tombstone_ctor
			]
			arms_sorted.sort(key=lambda arm: (arm.tag, arm.name))
			enum_id = self.module._dbg_new_id()
			enum_elements_id = self.module._dbg_new_id()
			enum_members: list[int] = []
			for arm in arms_sorted:
				enum_member_id = self.module._dbg_new_id()
				self.module._dbg_metadata.append(
					f"!{enum_member_id} = !DIEnumerator(name: \"{_llvm_md_escape(arm.name)}\", value: {arm.tag})"
				)
				enum_members.append(enum_member_id)
			if enum_members:
				members = ", ".join(f"!{mid}" for mid in enum_members)
				self.module._dbg_metadata.append(f"!{enum_elements_id} = !{{{members}}}")
			else:
				self.module._dbg_metadata.append(f"!{enum_elements_id} = !{{}}")
			self.module._dbg_metadata.append(
				f"!{enum_id} = distinct !DICompositeType(tag: {DW_TAG_ENUM}, name: \"{_llvm_md_escape(name)}::Tag\", file: !{file_id}, line: 0, size: 8, align: 8, elements: !{enum_elements_id})"
			)
			union_id = self.module._dbg_new_id()
			union_elements_id = self.module._dbg_new_id()
			union_members: list[int] = []
			for arm in arms_sorted:
				arm_struct_id = self.module._dbg_new_id()
				arm_elements_id = self.module._dbg_new_id()
				field_members: list[int] = []
				offset_bytes = 0
				max_align = 1
				for idx, fty in enumerate(arm.field_types):
					fsz, fal = self._size_align_typeid(fty)
					if fal > 1 and offset_bytes % fal != 0:
						offset_bytes += fal - (offset_bytes % fal)
					max_align = max(max_align, fal)
					member_id = self.module._dbg_new_id()
					fname = arm.field_names[idx] if idx < len(arm.field_names) else f"field{idx}"
					base = self._dbg_type_for_typeid(fty, span)
					fsz_bits = fsz * 8
					fal_bits = max(0, fal * 8)
					off_bits = offset_bytes * 8
					self.module._dbg_metadata.append(
						f"!{member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"{_llvm_md_escape(fname)}\", scope: !{arm_struct_id}, file: !{file_id}, line: 0, baseType: !{base}, size: {fsz_bits}, align: {fal_bits}, offset: {off_bits})"
					)
					field_members.append(member_id)
					offset_bytes += fsz
				if max_align > 1 and offset_bytes % max_align != 0:
					offset_bytes += max_align - (offset_bytes % max_align)
				if field_members:
					members = ", ".join(f"!{mid}" for mid in field_members)
					self.module._dbg_metadata.append(f"!{arm_elements_id} = !{{{members}}}")
				else:
					self.module._dbg_metadata.append(f"!{arm_elements_id} = !{{}}")
				arm_name = f"{name}::{arm.name}"
				arm_size_bits = offset_bytes * 8
				arm_align_bits = max_align * 8
				self.module._dbg_metadata.append(
					f"!{arm_struct_id} = distinct !DICompositeType(tag: {DW_TAG_STRUCT}, name: \"{_llvm_md_escape(arm_name)}\", file: !{file_id}, line: 0, size: {arm_size_bits}, align: {arm_align_bits}, elements: !{arm_elements_id})"
				)
				union_member_id = self.module._dbg_new_id()
				self.module._dbg_metadata.append(
					f"!{union_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"{_llvm_md_escape(arm.name)}\", scope: !{union_id}, file: !{file_id}, line: 0, baseType: !{arm_struct_id}, size: {arm_size_bits}, align: {arm_align_bits}, offset: 0)"
				)
				union_members.append(union_member_id)
			if union_members:
				members = ", ".join(f"!{mid}" for mid in union_members)
				self.module._dbg_metadata.append(f"!{union_elements_id} = !{{{members}}}")
			else:
				self.module._dbg_metadata.append(f"!{union_elements_id} = !{{}}")
			union_size_bits = payload_size_bytes * 8
			union_align_bits = payload_align_bytes * 8
			self.module._dbg_metadata.append(
				f"!{union_id} = distinct !DICompositeType(tag: {DW_TAG_UNION}, name: \"{_llvm_md_escape(name)}::payload\", file: !{file_id}, line: 0, size: {union_size_bits}, align: {union_align_bits}, elements: !{union_elements_id})"
			)
			tag_member_id = self.module._dbg_new_id()
			self.module._dbg_metadata.append(
				f"!{tag_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"tag\", scope: !{variant_id}, file: !{file_id}, line: 0, baseType: !{enum_id}, size: 8, align: 8, offset: 0)"
			)
			payload_member_id = self.module._dbg_new_id()
			payload_offset_bits = payload_offset * 8
			self.module._dbg_metadata.append(
				f"!{payload_member_id} = !DIDerivedType(tag: {DW_TAG_MEMBER}, name: \"payload\", scope: !{variant_id}, file: !{file_id}, line: 0, baseType: !{union_id}, size: {union_size_bits}, align: {union_align_bits}, offset: {payload_offset_bits})"
			)
			self.module._dbg_metadata.append(
				f"!{variant_elements_id} = !{{!{tag_member_id}, !{payload_member_id}}}"
			)
			self.module._dbg_metadata.append(
				f"!{variant_id} = distinct !DICompositeType(tag: {DW_TAG_STRUCT}, name: \"{_llvm_md_escape(name)}\", file: !{file_id}, line: 0, size: {size_bits}, align: {align_bits}, elements: !{variant_elements_id})"
			)
			return variant_id
		type_id = self.module._dbg_new_id()
		self.module._dbg_metadata.append(
			f"!{type_id} = !DICompositeType(tag: {DW_TAG_STRUCT}, name: \"{_llvm_md_escape(name)}\", file: !{file_id}, line: 0, size: {size_bits}, align: {align_bits}, elements: !{self.module._ensure_dbg_empty()})"
		)
		self.module._dbg_type_ids[ty_id] = type_id
		return type_id

	def _emit_dbg_value(self, local: str, ty_id: TypeId, value: str, span: Span | None) -> None:
		if not self.module.debug_enabled or self._dbg_subprogram_id is None:
			return
		actual_llty = self.value_types.get(value)
		if actual_llty is None:
			return
		local_id = self._dbg_local_var(local, ty_id, span)
		if local_id is None:
			return
		expr_id = self.module._ensure_dbg_expression()
		self.module.needs_dbg_intrinsics = True
		loc_id = self._dbg_location_for_span(span)
		val_llty = self._llvm_type_for_typeid(ty_id, allow_void_ok=True)
		emit_llty = self._llty(val_llty)
		if emit_llty.startswith("%"):
			return
		if actual_llty is not None and self._llty(actual_llty) != emit_llty:
			return
		line = f"  call void @llvm.dbg.value(metadata {emit_llty} {value}, metadata !{local_id}, metadata !{expr_id})"
		if loc_id is not None:
			line = f"{line}, !dbg !{loc_id}"
		self.lines.append(line)

	def _emit_dbg_declare(self, local: str, ty_id: TypeId, alloca_id: str, store_llty: str, span: Span | None) -> None:
		if not self.module.debug_enabled or self._dbg_subprogram_id is None:
			return
		if local in self._dbg_local_declared:
			return
		local_id = self._dbg_local_var(local, ty_id, span)
		if local_id is None:
			return
		expr_id = self.module._ensure_dbg_expression()
		self.module.needs_dbg_intrinsics = True
		loc_id = self._dbg_location_for_span(span)
		emit_store_llty = self._llty(store_llty)
		line = f"  call void @llvm.dbg.declare(metadata ptr %{alloca_id}, metadata !{local_id}, metadata !{expr_id})"
		if loc_id is not None:
			line = f"{line}, !dbg !{loc_id}"
		self.lines.append(line)
		self._dbg_local_declared.add(local)

	def _dbg_location_for_span(self, span: Span | None) -> int | None:
		if not self.module.debug_enabled or self._dbg_subprogram_id is None:
			return None
		use_span = span
		if use_span is None or use_span == Span():
			use_span = self._dbg_default_span
		loc_id = self.module.get_di_location(use_span, self._dbg_subprogram_id)
		if loc_id is None:
			loc_id = self.module.get_di_location(Span(file="<unknown>", line=1, column=1), self._dbg_subprogram_id)
		return loc_id

	def _declare_array_helpers_if_needed(self) -> None:
		"""Mark the module to emit array helper decls if any array ops are present."""
		has_array = any(
			isinstance(
				instr,
				(
					ArrayLit,
	ArrayAlloc,
	ArrayElemInit,
	ArrayElemInitUnchecked,
	ArrayElemAssign,
	ArrayElemDrop,
	ArrayElemTake,
	ArrayDrop,
	ArrayDup,
	ArrayIndexLoad,
	ArrayIndexLoadUnchecked,
	ArrayIndexStore,
	ArraySetLen,
				),
			)
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

		`MoveFromRef(local=L, ...)` is also addr-taken-equivalent: codegen
		needs stable alloca-backed storage for `L` to write the transferred
		bytes into.  A subsequent `MoveOut(_, L, ty)` then reads the bytes
		back via `LoadLocal(L)` (the normalization MoveOut lowering).  Without alloca-
		backed storage, the SSA rename map silently drops the MoveFromRef
		write and the value never lands in `L`.
		"""
		for block in self.func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, AddrOfLocal):
					self.addr_taken_locals.add(instr.local)
				elif isinstance(instr, MoveFromRef):
					self.addr_taken_locals.add(instr.local)

	def _alloca_name_for_local(self, local: str) -> str:
		"""
		Return a stable SSA name for the alloca pointer for a local.

		We keep it separate from the local name to avoid collisions with SSA
		versioned locals (e.g. `x_1`) and to make IR easier to read.

		We preserve every character that is legal in an unquoted LLVM identifier
		(`[A-Za-z0-9_$.]`). This is a no-op for all source-originated names (the
		grammar restricts identifiers to `[A-Za-z0-9_]`), but it is load-bearing
		for compiler temporaries: those are minted with a `.` marker
		(`MirBuilder.new_temp` → `.t<N>`) precisely so they cannot collide with a
		user local. Collapsing the `.` to `_` here would map a temp like `.t5`
		onto `_t5`, re-introducing exactly the collision the `.` namespace was
		chosen to prevent (a user `var _t5` that is also addr-taken would share
		this alloca name). Only characters outside the LLVM identifier set are
		escaped.
		"""
		safe = "".join(ch if (ch.isalnum() or ch in "_$.") else "_" for ch in local)
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
			if self.module.debug_enabled and not self._dbg_entry_anchor_emitted:
				span = self._dbg_default_span
				if span is None or span.line is None:
					entry_block = self.func.blocks.get(self.func.entry)
					if entry_block is not None:
						for instr in entry_block.instructions:
							span = getattr(instr, "span", None)
							if span is not None and span.line is not None:
								break
						if span is None or span.line is None:
							span = getattr(entry_block.terminator, "span", None)
				if span is None or span.line is None:
					fallback_file = self._dbg_default_span.file if self._dbg_default_span is not None else "<unknown>"
					span = Span(file=fallback_file, line=1, column=1)
				loc_id = self._dbg_location_for_span(span)
				line = "  call void asm sideeffect \"\", \"\"()"
				if loc_id is not None:
					line = f"{line}, !dbg !{loc_id}"
				self.lines.append(line)
				self._dbg_entry_anchor_emitted = True
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
		emit_llty = self._llty(llty)
		self.value_types[self.value_map[alloca_id]] = "ptr"
		self.lines.insert(self._entry_alloca_insert_index, f"  %{alloca_id} = alloca {emit_llty}")
		self._entry_alloca_insert_index += 1
		if llty == DRIFT_STRING_TYPE:
			self.lines.insert(
				self._entry_alloca_insert_index,
				f"  store {emit_llty} zeroinitializer, ptr %{alloca_id}",
			)
			self._entry_alloca_insert_index += 1
		return alloca_id

	def _ensure_iface_tmp_alloca(self) -> str:
		"""Return an entry-block alloca for building DriftIface temporaries.

		A single slot is reused across all ConstructIface/ConstructIfaceValue/
		IfaceUpcast lowerings in a function.  Each use fills then immediately
		loads the slot, so there is no lifetime overlap.  Placing the alloca
		in the entry block avoids stack growth when these instructions appear
		inside loops.
		"""
		if self._iface_tmp_alloca is not None:
			return self._iface_tmp_alloca
		assert self._entry_alloca_insert_index is not None
		name = self._fresh("iface_tmp_entry")
		emit_ty = self._llty(DRIFT_IFACE_TYPE)
		self.lines.insert(self._entry_alloca_insert_index, f"  {name} = alloca {emit_ty}")
		self._entry_alloca_insert_index += 1
		self._iface_tmp_alloca = name
		return name

	def _fresh_iface_alloca(self) -> str:
		"""Return a FRESH entry-block alloca for a DriftIface slot.

		Unlike `_ensure_iface_tmp_alloca` (one shared slot per
		function, safe only for fill-then-immediately-load
		patterns), this returns a new alloca per call.  Used by
		lowerings whose result is a POINTER into the alloca'd
		slot and therefore must outlive the emission site
		(e.g. `M.ArcFatGet`, which returns `ptr` as the borrowed
		`&I` value; a caller in a loop would otherwise repeatedly
		overwrite a shared slot).  Insertion point is the entry
		block — stack growth from loop iterations is avoided
		(each alloca runs once at function entry).
		"""
		assert self._entry_alloca_insert_index is not None
		name = self._fresh("iface_alloca")
		emit_ty = self._llty(DRIFT_IFACE_TYPE)
		self.lines.insert(self._entry_alloca_insert_index, f"  {name} = alloca {emit_ty}")
		self._entry_alloca_insert_index += 1
		return name

	def _dbg_keepalive_alloca_name(self, local: str) -> str:
		safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in local)
		return f"__dbg_keepalive_{safe}__addr"

	def _ensure_dbg_keepalive_storage(self, local: str, llty: str) -> str:
		existing = self._dbg_keepalive_storage_types.get(local)
		if existing is not None:
			if existing != llty:
				raise AssertionError(
					f"debug keepalive storage type mismatch for '{local}': {existing} vs {llty}"
				)
			return self._dbg_keepalive_allocas[local]
		if self._entry_alloca_insert_index is None:
			if self._current_block_name == self.func.entry:
				self._ensure_entry_insertion_point()
			else:
				raise AssertionError("debug keepalive storage requested before entry block insertion point is set")
		alloca_id = self._dbg_keepalive_alloca_name(local)
		emit_llty = self._llty(llty)
		self.lines.insert(self._entry_alloca_insert_index, f"  %{alloca_id} = alloca {emit_llty}")
		self._entry_alloca_insert_index += 1
		self._dbg_keepalive_storage_types[local] = llty
		self._dbg_keepalive_allocas[local] = alloca_id
		return alloca_id

	def _emit_dbg_keepalive_store(self, local: str, ty_id: TypeId, src_val: str, span: Span | None) -> None:
		if not self.module.debug_enabled or self.type_table is None:
			return
		td = self.type_table.get(ty_id)
		if td.kind in {TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL, TypeKind.TYPEVAR}:
			return
		# Keepalive storage currently performs a by-value store without retain/release
		# balancing. Limit it to plain scalar POD types to avoid duplicating ownership
		# for refcounted/non-trivial values (e.g., String/struct/variant/interface).
		if td.kind is not TypeKind.SCALAR or td.name not in {"Int", "Uint", "Uint64", "Byte", "Bool", "Float"}:
			return
		expected_llty = self._llvm_type_for_typeid(ty_id, allow_void_ok=True)
		actual_llty = self.value_types.get(src_val)
		if actual_llty is not None and self._llty(actual_llty) != self._llty(expected_llty):
			return
		store_llty = self._llvm_storage_type_for_typeid(ty_id)
		alloca_id = self._ensure_dbg_keepalive_storage(local, store_llty)
		self._emit_dbg_declare(local, ty_id, alloca_id, store_llty, span)
		val_llty = self.value_types.get(src_val)
		if val_llty is None:
			val_llty = self._llvm_type_for_typeid(ty_id, allow_void_ok=True)
			self.value_types[src_val] = val_llty
		val = src_val
		if self._is_bool_storage_pair(value_llty=val_llty, storage_llty=store_llty):
			tmp = self._fresh("bool_byte")
			self.lines.append(f"  {tmp} = zext i1 {val} to i8")
			val = tmp
		emit_store_llty = self._llty(store_llty)
		line = f"  store {emit_store_llty} {val}, ptr %{alloca_id}"
		loc_id = self._dbg_location_for_span(span)
		if loc_id is not None:
			line = f"{line}, !dbg !{loc_id}"
		self.lines.append(line)

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
			val_llty = self._llvm_type_for_typeid(ty_id, allow_void_ok=True)
			store_llty = self._llvm_storage_type_for_typeid(ty_id)
			alloca_id = self._ensure_local_storage(pname, store_llty)
			assert self._entry_alloca_insert_index is not None
			param_val = self._map_value(pname)
			if self._is_bool_storage_pair(value_llty=val_llty, storage_llty=store_llty):
				tmp = self._fresh("bool_byte")
				self.lines.insert(self._entry_alloca_insert_index, f"  {tmp} = zext i1 {param_val} to i8")
				self._entry_alloca_insert_index += 1
				param_val = tmp
			emit_store_llty = self._llty(store_llty)
			self.lines.insert(
				self._entry_alloca_insert_index,
				f"  store {emit_store_llty} {param_val}, ptr %{alloca_id}",
			)
			self._entry_alloca_insert_index += 1

	def _emit_block(self, block_name: str) -> None:
		# Track current block name so instruction-level helpers can consult SSA maps.
		self._current_block_name = block_name
		self._current_effective_block = block_name
		block = self.func.blocks[block_name]
		self.lines.append(f"{self._bb(block.name)}:")
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
			start_line = len(self.lines)
			self._lower_instr(instr, instr_index=idx)
			# Safety net: intrinsic handlers that produce raw scalar values
			# must be wrapped in FnResult when the MIR Call has can_throw=True.
			# Intrinsic handlers were written for the nothrow path and don't
			# check can_throw; this post-processing catches the mismatch
			# rather than patching each of ~30 individual handlers.
			if isinstance(instr, Call) and instr.can_throw and instr.dest is not None:
				_dk = f"%{instr.dest}"
				_vt = self.value_types.get(_dk)
				if _vt is not None and not _vt.startswith("%FnResult"):
					# Intrinsic produced a raw value; wrap it.
					_raw_tmp = self._fresh("raw_intrinsic")
					# Rewrite the last line that defined _dk to use the temp name.
					for _li in range(len(self.lines) - 1, max(start_line - 1, -1), -1):
						if f"  {_dk} = " in self.lines[_li]:
							self.lines[_li] = self.lines[_li].replace(f"  {_dk} = ", f"  {_raw_tmp} = ", 1)
							break
					self.value_types[_raw_tmp] = _vt
					del self.value_types[_dk]
					self._wrap_ok_fnresult(_raw_tmp, _vt, _dk, hint="intrinsic_ok")
			if len(self.lines) > start_line:
				if not isinstance(instr, AssignSSA):
					self._attach_dbg(start_line, instr)
		term_start = len(self.lines)
		self._lower_term(block.terminator)
		if len(self.lines) > term_start:
			self._attach_dbg(term_start, block.terminator)
		# Record the effective LLVM block that holds the terminator, so PHI
		# fixup can replace stale predecessor labels.
		self._block_exit_names[block_name] = self._current_effective_block
		# Best-effort cleanup; not strictly necessary.
		self._current_block_name = None
		self._current_effective_block = None

	def _attach_dbg(self, line_index: int, instr: object) -> None:
		if not self.module.debug_enabled:
			return
		if self._dbg_subprogram_id is None:
			return
		if line_index < len(self.lines) and ", !dbg !" in self.lines[line_index]:
			return
		target_index = line_index
		while target_index < len(self.lines):
			line = self.lines[target_index].strip()
			if line and not line.endswith(":"):
				break
			target_index += 1
		if target_index >= len(self.lines):
			return
		line_text = self.lines[target_index].lstrip()
		# LLVM 20 occasionally misparses debug-attached insertvalue instructions
		# in larger modules; keep debug locations on surrounding instructions.
		if "= insertvalue " in line_text or line_text.startswith("insertvalue "):
			return
		span = getattr(instr, "span", None)
		if span is None or span == Span():
			span = self._dbg_last_span or self._dbg_default_span
		if span is None or span == Span():
			span = Span(file="<unknown>", line=0, column=0)
		loc_id = self.module.get_di_location(span, self._dbg_subprogram_id)
		if loc_id is None:
			return
		self.lines[target_index] = f"{self.lines[target_index]}, !dbg !{loc_id}"
		self._dbg_last_span = span

	def _lower_phi(self, block_name: str, phi: Phi) -> None:
		dest = self._map_value(phi.dest)
		incomings = []
		incoming_types: set[str] = set()
		for pred, val in phi.incoming.items():
			incomings.append(f"[ {self._map_value(val)}, %{self._bb(pred)} ]")
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
		emit_phi_ty = self._llty(phi_ty)
		self.lines.append(f"  {dest} = phi {emit_phi_ty} {joined}")

	def _fixup_phi_predecessors(self) -> None:
		"""Rewrite PHI predecessor labels that were invalidated by instructions
		generating new LLVM basic blocks within a MIR block."""
		renames = {k: v for k, v in self._block_exit_names.items() if k != v}
		if not renames:
			return
		for idx, line in enumerate(self.lines):
			if " = phi " not in line:
				continue
			changed = False
			for mir_name, llvm_name in renames.items():
				old_label = f"%{self._bb(mir_name)} ]"
				if old_label in line:
					line = line.replace(old_label, f"%{llvm_name} ]")
					changed = True
			if changed:
				self.lines[idx] = line

	def _lower_instr(self, instr: object, instr_index: int | None = None) -> None:
		if isinstance(instr, ConstInt):
			dest = self._map_value(instr.dest)
			self.value_types[dest] = DRIFT_INT_TYPE
			self.const_values[dest] = int(instr.value)
			self.lines.append(f"  {dest} = add {self._llty(DRIFT_INT_TYPE)} 0, {instr.value}")
		elif isinstance(instr, ConstByte):
			dest = self._map_value(instr.dest)
			self.value_types[dest] = "i8"
			self.const_values[dest] = int(instr.value)
			self.lines.append(f"  {dest} = add i8 0, {instr.value}")
		elif isinstance(instr, ConstVoid):
			dest = self._map_value(instr.dest)
			self.value_types[dest] = "i8"
			self.const_values[dest] = 0
			self.lines.append(f"  {dest} = add i8 0, 0")
		elif isinstance(instr, ConstUint):
			dest = self._map_value(instr.dest)
			self.value_types[dest] = DRIFT_UINT_TYPE
			self.const_values[dest] = int(instr.value)
			self.lines.append(f"  {dest} = add {self._llty(DRIFT_UINT_TYPE)} 0, {instr.value}")
		elif isinstance(instr, ConstUint64):
			dest = self._map_value(instr.dest)
			self.value_types[dest] = DRIFT_U64_TYPE
			self.const_values[dest] = int(instr.value)
			self.lines.append(f"  {dest} = add {DRIFT_U64_TYPE} 0, {instr.value}")
		elif isinstance(instr, IntFromUint):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			val_ty = self.value_types.get(val)
			if val_ty != DRIFT_USIZE_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: IntFromUint requires Uint operand (have {val_ty})"
				)
			self.lines.append(f"  {dest} = add {self._llty(DRIFT_INT_TYPE)} {val}, 0")
			self.value_types[dest] = DRIFT_INT_TYPE
		elif isinstance(instr, UintFromInt):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			val_ty = self.value_types.get(val)
			if val_ty != DRIFT_INT_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: UintFromInt requires Int operand (have {val_ty})"
				)
			self.lines.append(f"  {dest} = add {self._llty(DRIFT_USIZE_TYPE)} {val}, 0")
			self.value_types[dest] = DRIFT_USIZE_TYPE
		elif isinstance(instr, CastScalar):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			if self.type_table is not None:
				src_td = self.type_table.get(instr.src_ty)
				dst_td = self.type_table.get(instr.dst_ty)
				src_is_ptr = src_td.kind is TypeKind.RAW_PTR
				dst_is_ptr = dst_td.kind is TypeKind.RAW_PTR
				src_is_uint = src_td.kind is TypeKind.SCALAR and src_td.name == "Uint"
				dst_is_uint = dst_td.kind is TypeKind.SCALAR and dst_td.name == "Uint"
				if src_is_ptr or dst_is_ptr:
					src_ll = self._llty(self._llvm_type_for_typeid(instr.src_ty))
					dst_ll = self._llty(self._llvm_type_for_typeid(instr.dst_ty))
					val_ty = self.value_types.get(val)
					if val_ty is None or self._llty(val_ty) != src_ll:
						raise NotImplementedError(
							f"LLVM codegen v1: CastScalar ptr type mismatch (have {val_ty}, expected {src_ll})"
						)
					if src_is_ptr and dst_is_ptr:
						# Opaque pointers: ptr-to-ptr is identity.
						self.value_map[instr.dest] = val
						self.value_types[dest] = "ptr"
						return
					if src_is_ptr and dst_is_uint:
						self.lines.append(f"  {dest} = ptrtoint {src_ll} {val} to {self._llty(DRIFT_USIZE_TYPE)}")
						self.value_types[dest] = DRIFT_USIZE_TYPE
						return
					if src_is_uint and dst_is_ptr:
						self.lines.append(f"  {dest} = inttoptr {self._llty(DRIFT_USIZE_TYPE)} {val} to {dst_ll}")
						self.value_types[dest] = self._llvm_type_for_typeid(instr.dst_ty)
						return
					raise NotImplementedError("LLVM codegen v1: unsupported pointer cast combination")
			src_info = self._scalar_cast_info(instr.src_ty)
			dst_info = self._scalar_cast_info(instr.dst_ty)
			if src_info is None or dst_info is None:
				raise NotImplementedError("LLVM codegen v1: CastScalar requires scalar types")
			src_tag, src_bits, src_signed = src_info
			dst_tag, dst_bits, _dst_signed = dst_info
			val_ty = self.value_types.get(val)
			if val_ty is None or self._llty(val_ty) != self._llty(src_tag):
				raise NotImplementedError(
					f"LLVM codegen v1: CastScalar type mismatch (have {val_ty}, expected {src_tag})"
				)
			if src_bits == dst_bits:
				self.lines.append(f"  {dest} = add {self._llty(dst_tag)} {val}, 0")
				self.value_types[dest] = dst_tag
				return
			op = "sext" if src_signed else "zext"
			if dst_bits < src_bits:
				op = "trunc"
			self.lines.append(
				f"  {dest} = {op} {self._llty(src_tag)} {val} to {self._llty(dst_tag)}"
			)
			self.value_types[dest] = dst_tag
		elif isinstance(instr, ConstBool):
			dest = self._map_value(instr.dest)
			val = 1 if instr.value else 0
			self.value_types[dest] = "i1"
			self.lines.append(f"  {dest} = add i1 0, {val}")
		elif isinstance(instr, ConstFloat):
			dest = self._map_value(instr.dest)
			# Use Python's repr(...) to preserve sufficient precision for round-trips.
			# LLVM accepts decimal float literals in textual IR.
			lit = repr(instr.value)
			float_llty = self._llvm_float_type()
			self.value_types[dest] = float_llty
			self.lines.append(f"  {dest} = fadd {float_llty} 0.0, {lit}")
		elif isinstance(instr, StringRetain):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			self.module.needs_string_retain = True
			self.lines.append(f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_retain({DRIFT_STRING_TYPE} {val})")
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, StringRelease):
			val = self._map_value(instr.value)
			self.module.needs_string_release = True
			self.lines.append(f"  call void @drift_string_release({DRIFT_STRING_TYPE} {val})")
		elif isinstance(instr, CopyValue):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			# copy-construction: the CopyValue instruction IS the copy.
			copied = self._emit_copy_value(instr.ty, val, dest_hint=dest)
			self.value_map[instr.dest] = copied if copied != dest else dest
			if copied in self.value_types:
				self.value_types[dest] = self.value_types[copied]
		elif isinstance(instr, DropValue):
			val = self._map_value(instr.value)
			self._emit_drop_value(instr.ty, val)
		elif isinstance(instr, MoveOut):
			raise AssertionError("MoveOut should be lowered before LLVM codegen")
		elif isinstance(instr, ZeroValue):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: ZeroValue requires a TypeTable")
			dest = self._map_value(instr.dest)
			self._emit_zero_value(dest, instr.ty)
		elif isinstance(instr, TombstoneValue):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: TombstoneValue requires a TypeTable")
			# Contract enforcement at the MIR instruction boundary: the
			# MIR `TombstoneValue` node is reserved for callers that must
			# produce a drop-SAFE byte pattern for the given type (see
			# the MIR-node docstring).  A struct with a user
			# `core.Destructible` impl has no universally drop-safe byte
			# pattern — `DropValue` will invoke the user destructor on
			# whatever bytes this produces, and zero/tombstone field
			# bytes yield null-bearing receivers whose destructor reads
			# null fields.  Fail loudly at this boundary, rather than
			# inside the shared `_emit_tombstone_value` helper (which is
			# also reached by the `ArrayElemTake` slot-neutralize path
			# where per-element drop trusts the caller's destructor).
			td_instr = self.type_table.get(instr.ty)
			if td_instr.kind is TypeKind.STRUCT:
				destructor_fns = getattr(self.type_table, "destructor_fns", None)
				if isinstance(destructor_fns, dict) and destructor_fns.get(instr.ty) is not None:
					td_name = td_instr.name if td_instr.name is not None else f"typeid={instr.ty}"
					raise AssertionError(
						f"TombstoneValue unsafe for struct '{td_name}' with a "
						f"user Destructible impl: no byte pattern makes the "
						f"user destructor a no-op.  Caller must not emit "
						f"MIR TombstoneValue for custom Destructible structs."
					)
			tomb = self._emit_tombstone_value(instr.ty)
			self.value_map[instr.dest] = tomb
			if tomb in self.value_types:
				self.value_types[self._map_value(instr.dest)] = self.value_types[tomb]
		elif isinstance(instr, UnaryOpInstr):
			self._lower_unary(instr)
		elif isinstance(instr, WrappingAddU64):
			dest = self._map_value(instr.dest)
			left = self._map_value(instr.left)
			right = self._map_value(instr.right)
			left_ty = self.value_types.get(left)
			right_ty = self.value_types.get(right)
			if left_ty is None:
				raise NotImplementedError(
					"LLVM codegen v1: wrapping_add_u64 requires typed operands (left type missing)"
				)
			if left_ty != DRIFT_U64_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: wrapping_add_u64 requires Uint64 operands (have {left_ty})"
				)
			if right_ty is None:
				raise NotImplementedError(
					"LLVM codegen v1: wrapping_add_u64 requires typed operands (right type missing)"
				)
			if right_ty != DRIFT_U64_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: wrapping_add_u64 requires Uint64 operands (have {right_ty})"
				)
			self.value_types[dest] = DRIFT_U64_TYPE
			self.lines.append(f"  {dest} = add {self._llty(DRIFT_U64_TYPE)} {left}, {right}")
		elif isinstance(instr, WrappingMulU64):
			dest = self._map_value(instr.dest)
			left = self._map_value(instr.left)
			right = self._map_value(instr.right)
			left_ty = self.value_types.get(left)
			right_ty = self.value_types.get(right)
			if left_ty is None:
				raise NotImplementedError(
					"LLVM codegen v1: wrapping_mul_u64 requires typed operands (left type missing)"
				)
			if left_ty != DRIFT_U64_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: wrapping_mul_u64 requires Uint64 operands (have {left_ty})"
				)
			if right_ty is None:
				raise NotImplementedError(
					"LLVM codegen v1: wrapping_mul_u64 requires typed operands (right type missing)"
				)
			if right_ty != DRIFT_U64_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: wrapping_mul_u64 requires Uint64 operands (have {right_ty})"
				)
			self.value_types[dest] = DRIFT_U64_TYPE
			self.lines.append(f"  {dest} = mul {self._llty(DRIFT_U64_TYPE)} {left}, {right}")
		elif isinstance(instr, ConstString):
			self._lower_const_string(instr)
		elif isinstance(instr, ArrayAlloc):
			self._lower_array_alloc(instr)
		elif isinstance(instr, ArraySetLen):
			self._lower_array_set_len(instr)
		elif isinstance(instr, ArraySetGen):
			self._lower_array_set_gen(instr)
		elif isinstance(instr, ConstArray):
			self._lower_const_array(instr)
		elif isinstance(instr, ArrayLit):
			self._lower_array_lit(instr)
		elif isinstance(instr, ArrayElemInit):
			self._lower_array_elem_init(instr)
		elif isinstance(instr, ArrayElemInitUnchecked):
			self._lower_array_elem_init_unchecked(instr)
		elif isinstance(instr, ArrayElemAssign):
			self._lower_array_elem_assign(instr)
		elif isinstance(instr, ArrayElemDrop):
			self._lower_array_elem_drop(instr)
		elif isinstance(instr, ArrayElemTake):
			self._lower_array_elem_take(instr)
		elif isinstance(instr, ArrayDrop):
			self._lower_array_drop(instr)
		elif isinstance(instr, ArrayDup):
			self._lower_array_dup(instr)
		elif isinstance(instr, ArrayIndexLoad):
			self._lower_array_index_load(instr)
		elif isinstance(instr, ArrayIndexLoadUnchecked):
			self._lower_array_index_load_unchecked(instr)
		elif isinstance(instr, ArrayIndexStore):
			self._lower_array_index_store(instr)
		elif isinstance(instr, ArrayLen):
			self._lower_array_len(instr)
		elif isinstance(instr, ArrayCap):
			self._lower_array_cap(instr)
		elif isinstance(instr, ArrayGen):
			self._lower_array_gen(instr)
		elif isinstance(instr, RawBufferAlloc):
			self._lower_raw_buffer_alloc(instr)
		elif isinstance(instr, RawBufferDealloc):
			self._lower_raw_buffer_dealloc(instr)
		elif isinstance(instr, RawBufferPtrAt):
			self._lower_raw_buffer_ptr_at(instr)
		elif isinstance(instr, RawBufferWrite):
			self._lower_raw_buffer_write(instr)
		elif isinstance(instr, RawBufferRead):
			self._lower_raw_buffer_read(instr)
		elif isinstance(instr, PtrFromRef):
			self._lower_ptr_from_ref(instr)
		elif isinstance(instr, PtrOffset):
			self._lower_ptr_offset(instr)
		elif isinstance(instr, PtrRead):
			self._lower_ptr_read(instr)
		elif isinstance(instr, PtrWrite):
			self._lower_ptr_write(instr)
		elif isinstance(instr, PtrIsNull):
			self._lower_ptr_is_null(instr)
		elif isinstance(instr, PtrAsMutRef):
			self._lower_ptr_as_mut_ref(instr)
		elif isinstance(instr, StringLen):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			# StringLen is reused for strings and arrays at HIR level; here we assume string.
			storage_tmp = self._fresh("storage")
			self.lines.append(f"  {dest} = extractvalue {DRIFT_STRING_TYPE} {val}, 0")
			self.lines.append(f"  {storage_tmp} = extractvalue {DRIFT_STRING_TYPE} {val}, 1")
			self._emit_string_observe_guard(dest, storage_tmp)
			self.value_types[dest] = DRIFT_INT_TYPE
		elif isinstance(instr, StringByteAt):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			index = self._map_value(instr.index)
			idx_ty = self.value_types.get(index)
			if idx_ty != DRIFT_INT_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: string byte index must be Int, got {idx_ty}"
				)
			len_tmp = self._fresh("len")
			storage_tmp = self._fresh("storage")
			self.lines.append(f"  {len_tmp} = extractvalue {DRIFT_STRING_TYPE} {val}, 0")
			self.lines.append(f"  {storage_tmp} = extractvalue {DRIFT_STRING_TYPE} {val}, 1")
			if not getattr(instr, "unchecked", False):
				# Fully checked form (any producer other than the
				# hir_to_mir guarded-index expansion): observation
				# guard + defense-in-depth C bounds check.  The
				# unchecked form's producer already guarded (StringLen
				# on the same handle) and proved the bounds.
				self._emit_string_observe_guard(len_tmp, storage_tmp)
				self.module.needs_array_helpers = True
				container_id = self._emit_string_literal_value(STRING_CONTAINER_ID)
				self.lines.append(
					f"  call void @drift_bounds_check({DRIFT_STRING_TYPE} {container_id}, {self._llty(DRIFT_INT_TYPE)} {index}, {self._llty(DRIFT_INT_TYPE)} {len_tmp})"
				)
			# ABI 22 (B-repr/B5): field 1 is the DriftRcBytes HEADER
			# pointer; bytes live at +16.  One of exactly three codegen
			# layout-authority lowerings (with the literal emitters and
			# the string_bytes_base intrinsic).
			base_tmp = self._fresh("bytes_base")
			ptr_tmp = self._fresh("ptr")
			idx_llty = self._llty(DRIFT_INT_TYPE)
			self.lines.append(f"  {base_tmp} = getelementptr i8, ptr {storage_tmp}, {idx_llty} 16")
			self.lines.append(f"  {ptr_tmp} = getelementptr i8, ptr {base_tmp}, {idx_llty} {index}")
			self.lines.append(f"  {dest} = load i8, ptr {ptr_tmp}")
			self.value_types[dest] = "i8"
		elif isinstance(instr, StringBytesBase):
			# B5 §3.3 layout-authority lowering (the third of exactly
			# three): borrowed bytes base = storage + 16, NO retain and
			# no stake — the enclosing std.ffi wrapper owns the borrow
			# window.
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			len_tmp = self._fresh("len")
			storage_tmp = self._fresh("storage")
			idx_llty = self._llty(DRIFT_INT_TYPE)
			self.lines.append(f"  {len_tmp} = extractvalue {DRIFT_STRING_TYPE} {val}, 0")
			self.lines.append(f"  {storage_tmp} = extractvalue {DRIFT_STRING_TYPE} {val}, 1")
			self._emit_string_observe_guard(len_tmp, storage_tmp)
			self.lines.append(f"  {dest} = getelementptr i8, ptr {storage_tmp}, {idx_llty} 16")
			self.value_types[dest] = "ptr"
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
		elif isinstance(instr, AssertLoc):
			cond = self._map_value(instr.cond)
			file_val = self._map_value(instr.file)
			line_val = self._map_value(instr.line)
			expr_val = self._map_value(instr.expr)
			msg_val = self._map_value(instr.msg)
			cond_ty = self.value_types.get(cond)
			if cond_ty != "i1":
				raise NotImplementedError(
					f"LLVM codegen v1: assert cond must be Bool (i1), got {cond_ty}"
				)
			file_ty = self.value_types.get(file_val)
			if file_ty != DRIFT_STRING_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: assert file must be String ({DRIFT_STRING_TYPE}), got {file_ty}"
				)
			line_ty = self.value_types.get(line_val)
			if line_ty != DRIFT_INT_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: assert line must be Int ({DRIFT_INT_TYPE}), got {line_ty}"
				)
			msg_ty = self.value_types.get(msg_val)
			if msg_ty != DRIFT_STRING_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: assert msg must be String ({DRIFT_STRING_TYPE}), got {msg_ty}"
				)
			expr_ty = self.value_types.get(expr_val)
			if expr_ty != DRIFT_STRING_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: assert expr must be String ({DRIFT_STRING_TYPE}), got {expr_ty}"
				)
			self.module.needs_assert_runtime = True
			self.lines.append(
				f"  call void @drift_assert_loc(i1 {cond}, {DRIFT_STRING_TYPE} {file_val}, {self._llty(DRIFT_INT_TYPE)} {line_val}, {DRIFT_STRING_TYPE} {expr_val}, {DRIFT_STRING_TYPE} {msg_val})"
			)
		elif isinstance(instr, StringFromInt):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			val_ty = self.value_types.get(val)
			if val_ty != DRIFT_INT_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: StringFromInt requires Int operand (have {val_ty})"
				)
			self.module.needs_string_from_int64 = True
			int64_val = val
			if self.module.word_bits != 64:
				int64_val = self._fresh("int64")
				self.lines.append(f"  {int64_val} = sext {self._llty(DRIFT_INT_TYPE)} {val} to i64")
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_from_int64(i64 {int64_val})"
			)
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, StringFromUint):
			dest = self._map_value(instr.dest)
			val = self._map_value(instr.value)
			val_ty = self.value_types.get(val)
			# Accept both `DRIFT_USIZE_TYPE` (the Uint tag — usual
			# producer) and `DRIFT_U64_TYPE` (raw i64, e.g.
			# `M.ErrorEvent` for DriftErrorCode).  Both pass through
			# at i64 width on word_bits=64 with no semantic change.
			if val_ty != DRIFT_USIZE_TYPE and val_ty != DRIFT_U64_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: StringFromUint requires Uint or u64 operand (have {val_ty})"
				)
			self.module.needs_string_from_uint64 = True
			int64_val = val
			if val_ty == DRIFT_USIZE_TYPE and self.module.word_bits != 64:
				int64_val = self._fresh("uint64")
				self.lines.append(f"  {int64_val} = zext {self._llty(DRIFT_USIZE_TYPE)} {val} to i64")
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_from_uint64(i64 {int64_val})"
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
			float_llty = self._llvm_float_type()
			if val_ty != float_llty:
				raise NotImplementedError(
					f"LLVM codegen v1: StringFromFloat requires {float_llty} operand (have {val_ty})"
				)
			if float_llty == "float":
				ext = self._fresh("fext")
				self.lines.append(f"  {ext} = fpext float {val} to double")
				val = ext
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
			# Normalize to the compiler's Int carrier so downstream comparisons use
			# the same integer pipeline as other BinaryOpInstr nodes.
			if self.module.word_bits == 32:
				self.lines.append(f"  {dest} = add {self._llty(DRIFT_INT_TYPE)} {tmp}, 0")
			else:
				self.lines.append(f"  {dest} = sext i32 {tmp} to {self._llty(DRIFT_INT_TYPE)}")
			self.value_types[dest] = DRIFT_INT_TYPE
		elif isinstance(instr, AssignSSA):
			# AssignSSA is a pure SSA alias. We pre-collect aliases in
			# `_collect_assign_aliases` so Φ lowering can resolve aliases even when
			# the defining AssignSSA appears in a later-emitted block.
			if self.module.debug_enabled and self.type_table is not None:
				debug_name = getattr(instr, "debug_name", None)
				local_name = getattr(instr, "local", None)
				if debug_name is None and local_name is not None:
					debug_name = self.func.debug_local_names.get(local_name)
				ty_id = None
				if local_name is not None:
					ty_id = self.func.local_types.get(local_name)
				if ty_id is None and debug_name is not None:
					ty_id = self.func.local_types.get(debug_name)
				if ty_id is None:
					ty_id = self.func.local_types.get(instr.dest)
				if ty_id is not None:
					td = self.type_table.get(ty_id)
					if td.kind in {TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL, TypeKind.TYPEVAR}:
						if drift_debug.enabled("dbg_unknown_types"):
							import sys
							span_dbg = getattr(instr, "span", None)
							print(
								f"[drift:debug][dbg_unknown_types] fn={self.func.fn_id} local={local_name or instr.dest} ty={ty_id}:{td.kind.name}:{td.name} span={span_dbg}",
								file=sys.stderr,
							)
						return
					span = getattr(instr, "span", None) or self._dbg_default_span
					src_val = self._map_value(instr.src)
					self._emit_dbg_value(debug_name or local_name or instr.dest, ty_id, src_val, span)
					if local_name is not None:
						self._emit_dbg_keepalive_store(local_name, ty_id, src_val, span)
			return
		elif isinstance(instr, LoadLocal):
			# Address-taken locals are materialized as storage. For them, LoadLocal
			# is a real `load` from the local's alloca slot.
			if instr.local in self.addr_taken_locals:
				store_llty = self.local_storage_types.get(instr.local)
				if store_llty is None:
					raise NotImplementedError(
						f"LLVM codegen v1: cannot load from address-taken local '{instr.local}' without a known type"
					)
				alloca_id = self._ensure_local_storage(instr.local, store_llty)
				dest = self._map_value(instr.dest)
				if store_llty == "i8":
					raw = self._fresh("bool_byte")
					self.lines.append(f"  {raw} = load i8, ptr %{alloca_id}")
					self._bool_from_storage(raw, dest=dest)
					self.value_types[dest] = "i1"
				else:
					emit_store_llty = self._llty(store_llty)
					self.lines.append(f"  {dest} = load {emit_store_llty}, ptr %{alloca_id}")
					self.value_types[dest] = store_llty
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
			local_ty = self.func.local_types.get(instr.local)
			orig_val = val
			if local_ty is not None:
				val = self._coerce_value_to_typeid(instr.value, val, local_ty, context=f"local '{instr.local}'")
			if instr.local in self.addr_taken_locals:
				store_llty = self.local_storage_types.get(instr.local)
				if store_llty is None:
					val_llty = self.value_types.get(val)
					if val_llty is None:
						raise NotImplementedError(
							f"LLVM codegen v1: cannot store into address-taken local '{instr.local}' without a typed value"
						)
					store_llty = "i8" if val_llty == "i1" else val_llty
				alloca_id = self._ensure_local_storage(instr.local, store_llty)
				val_llty_actual = self.value_types.get(val, store_llty)
				if self._is_bool_storage_pair(value_llty=val_llty_actual, storage_llty=store_llty):
					val = self._bool_to_storage(val)
				emit_store_llty = self._llty(store_llty)
				self.lines.append(f"  store {emit_store_llty} {val}, ptr %{alloca_id}")
				if self.module.debug_enabled and self.type_table is not None:
					ty_id = self.func.local_types.get(instr.local)
					span = getattr(instr, "span", None) or self._dbg_default_span
					if ty_id is not None:
						self._emit_dbg_declare(instr.local, ty_id, alloca_id, store_llty, span)
				return
			# SSA maps locals to versioned names; no IR emission required here.
			block_name = getattr(self, "_current_block_name", None)
			if instr_index is not None and block_name is not None:
				ssa_name = self.ssa.value_for_instr.get((block_name, instr_index))
			else:
				ssa_name = None
			if ssa_name:
				if val != orig_val:
					self.value_map[ssa_name] = val
				else:
					self.aliases[ssa_name] = instr.value
				if val in self.value_types:
					self.value_types[ssa_name] = self.value_types[val]
			if instr.local in self.value_types and val in self.value_types:
				self.value_types[instr.local] = self.value_types[val]
			if self.module.debug_enabled and self.type_table is not None:
				ty_id = self.func.local_types.get(instr.local)
				span = getattr(instr, "span", None) or self._dbg_default_span
				if ty_id is not None:
					self._emit_dbg_value(instr.local, ty_id, val, span)
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
			emit_llty = self._llty(llty)
			self.value_types[dest] = "ptr"
		elif isinstance(instr, AddrOfArrayElem):
			array = self._map_value(instr.array)
			index = self._map_value(instr.index)
			elem_llty = self._llvm_storage_type_for_typeid(instr.inner_ty)
			emit_elem_llty = self._llty(elem_llty)
			arr_llty = self._llvm_array_header_type()
			ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=emit_elem_llty, arr_llty=arr_llty)
			# Record an alias so later uses resolve to the computed pointer.
			self.aliases[instr.dest] = ptr_tmp[1:] if ptr_tmp.startswith("%") else ptr_tmp
			dest = self._map_value(instr.dest)
			self.value_types[dest] = "ptr"
		elif isinstance(instr, AddrOfField):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: AddrOfField requires a TypeTable")
			base_ptr = self._map_value(instr.base_ptr)
			struct_llty = self._llvm_type_for_typeid(instr.struct_ty)
			have_ptr_ty = self.value_types.get(base_ptr)
			if have_ptr_ty is not None and not _is_ptr_type(have_ptr_ty):
				raise NotImplementedError(
					f"LLVM codegen v1: AddrOfField base pointer type mismatch (have {have_ptr_ty}, expected ptr)"
				)
			field_llty = self._llvm_field_storage_type_for_typeid(instr.field_ty)
			emit_field_llty = self._llty(field_llty)
			dest = self._map_value(instr.dest)
			self.lines.append(
				f"  {dest} = getelementptr inbounds {struct_llty}, ptr {base_ptr}, i32 0, i32 {instr.field_index}"
			)
			self.value_types[dest] = "ptr"
		elif isinstance(instr, ConstructIface):
			self._lower_construct_iface(instr)
		elif isinstance(instr, ConstructIfaceValue):
			self._lower_construct_iface_value(instr)
		elif isinstance(instr, ConstructIfaceBorrowed):
			self._lower_construct_iface_borrowed(instr)
		elif isinstance(instr, IfaceUpcast):
			self._lower_iface_upcast(instr)
		elif isinstance(instr, ArcAsInterface):
			self._lower_arc_as_interface(instr)
		elif isinstance(instr, ArcFatGet):
			self._lower_arc_fat_get(instr)
		elif isinstance(instr, ConstructStruct):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: ConstructStruct requires a TypeTable")
			struct_def = self.type_table.get(instr.struct_ty)
			if struct_def.kind is not TypeKind.STRUCT:
				raise AssertionError("ConstructStruct with non-STRUCT TypeId (MIR bug)")
			struct_inst = self.type_table.get_struct_instance(instr.struct_ty)
			field_types = list(struct_inst.field_types) if struct_inst is not None else list(struct_def.param_types)
			struct_llty = self._llvm_type_for_typeid(instr.struct_ty)
			current = "zeroinitializer"
			if len(instr.args) != len(field_types):
				raise AssertionError("ConstructStruct arg/field length mismatch (MIR bug)")
			if not field_types:
				tmp_ptr = self._scratch_alloca(struct_llty, "struct_tmp")
				self.lines.append(f"  store {struct_llty} zeroinitializer, ptr {tmp_ptr}")
				dest = self._map_value(instr.dest)
				self.lines.append(f"  {dest} = load {struct_llty}, ptr {tmp_ptr}")
				self.value_types[dest] = struct_llty
				return
			for idx, (arg, field_ty) in enumerate(zip(instr.args, field_types)):
				arg_val = self._map_value(arg)
				field_val_llty = self._llvm_type_for_typeid(field_ty)
				field_store_llty = self._llvm_field_storage_type_for_typeid(field_ty)
				have = self.value_types.get(arg_val)
				field_td = self.type_table.get(field_ty)
				is_throwing_fn_field = field_td.kind is TypeKind.FUNCTION and field_td.can_throw() and field_td.param_types
				if is_throwing_fn_field:
					arg_val = self._coerce_throwing_fn_to_fat(
						arg, arg_val, field_ty,
						context=f"struct {struct_def.name} field {idx}",
					)
					emit_field_store_llty = DRIFT_FAT_FNPTR_TYPE
				else:
					if have is not None and have != field_val_llty:
						field_lltys = [self._llvm_type_for_typeid(t) for t in field_types]
						arg_lltys = [self.value_types.get(self._map_value(a)) for a in instr.args]
						raise NotImplementedError(
							f"LLVM codegen v1: struct {struct_def.name} field {idx} type mismatch (have {have}, expected {field_val_llty}); "
							f"fields={field_lltys} args={arg_lltys}"
						)
					if self._is_bool_storage_pair(value_llty=field_val_llty, storage_llty=field_store_llty):
						arg_val = self._bool_to_storage(arg_val)
					emit_field_store_llty = self._llty(field_store_llty)
				is_last = idx == len(field_types) - 1
				tmp = self._map_value(instr.dest) if is_last else self._fresh("struct")
				self.lines.append(
					f"  {tmp} = insertvalue {struct_llty} {current}, {emit_field_store_llty} {arg_val}, {idx}"
				)
				current = tmp
			dest = self._map_value(instr.dest)
			self.value_types[dest] = struct_llty
		elif isinstance(instr, ConstructVariant):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: ConstructVariant requires a TypeTable")
			layout = self._variant_layout(instr.variant_ty)
			variant_llty = layout.llvm_ty
			arm_layout = layout.arm_by_name.get(instr.ctor)
			if arm_layout is None:
				raise NotImplementedError(
					f"LLVM codegen v1: unknown variant constructor '{instr.ctor}' for TypeId {instr.variant_ty}"
				)
			if not arm_layout.field_storage_lltys:
				dest = self._map_value(instr.dest)
				self.lines.append(f"  {dest} = insertvalue {variant_llty} zeroinitializer, i8 {arm_layout.tag}, 0")
				self.value_types[dest] = variant_llty
				return
			# Materialize into a stack slot so we can write into the aligned payload.
			tmp_ptr = self._scratch_alloca(variant_llty, "variant")
			self.lines.append(f"  store {variant_llty} zeroinitializer, ptr {tmp_ptr}")
			tag_ptr = self._fresh("tagptr")
			self.lines.append(
				f"  {tag_ptr} = getelementptr inbounds {variant_llty}, ptr {tmp_ptr}, i32 0, i32 0"
			)
			self.lines.append(f"  store i8 {arm_layout.tag}, ptr {tag_ptr}")
			if arm_layout.field_storage_lltys:
				payload_words_ptr = self._fresh("payload_words")
				self.lines.append(
					f"  {payload_words_ptr} = getelementptr inbounds {variant_llty}, ptr {tmp_ptr}, i32 0, i32 2"
				)
				payload_struct_ptr = payload_words_ptr
				for idx, (arg, field_ty, want_llty, store_llty) in enumerate(
					zip(instr.args, arm_layout.field_tys, arm_layout.field_lltys, arm_layout.field_storage_lltys)
				):
					arg_val = self._map_value(arg)
					arg_val = self._coerce_value_to_typeid(arg, arg_val, field_ty, context=f"variant payload {idx}")
					have = self.value_types.get(arg_val)
					autoloaded_from_storage = False
					if have is not None and have != want_llty:
						# Auto-load pointer-to-value for payload fields.
						# This handles the case where MIR passes a
						# `VariantGetFieldAddr` result (pointer) directly
						# to `ConstructVariant`, e.g. a borrowed-match
						# arm reconstructing the SAME variant case from
						# a Copy-typed payload binder
						# (`match v { V::N(n) => V::N(n) }` over `&V`).
						#
						# The load type MUST be the concrete payload
						# storage LLVM type (`store_llty`), not the
						# abstract `field_lltys[idx]` (which can be a
						# tag like `drift.int` resolved later in IR
						# emission — clang's IR parser rejects it
						# verbatim with "expected type").
						# `field_storage_lltys` is authoritative for
						# the in-memory payload layout.
						#
						# Bool note: `field_storage_lltys[idx]` for a
						# Bool field is `i8` (storage); the value form
						# would be `i1`.  After autoloading at
						# `store_llty=i8`, the value is already in
						# storage form — `autoloaded_from_storage=True`
						# tells the downstream store to skip the
						# i1→i8 conversion.  Pinned by
						# `lang/tests/codegen/test_variant_borrowed_match_construct_int_payload.py`.
						if _is_ptr_type(have):
							# Narrow lowering contract: the autoload is authorized
							# ONLY for a pointer proven to originate from a
							# VariantGetFieldAddr (the borrowed-match reconstruct
							# `match v { V::N(n) => V::N(n) }`).  An arbitrary
							# address-producing value reaching here where a struct
							# value is expected is the masked-bug signature (a
							# typed-catch `Error` projection view fed into a native
							# struct field); the checker now rejects that, so any
							# residual occurrence is a broken contract, not source.
							if arg_val not in self.variant_field_addr_ptrs:
								raise AssertionError(
									f"LLVM codegen v1: internal lowering-contract failure: "
									f"ConstructVariant field {idx} received pointer {arg_val} "
									f"(have {have}, expected {want_llty}) that is not a "
									f"VariantGetFieldAddr result; refusing to autoload an "
									f"unprovenanced address as a struct value"
								)
							loaded = self._fresh("autoload")
							self.lines.append(f"  {loaded} = load {store_llty}, ptr {arg_val}")
							self.value_map[arg] = loaded
							self.value_types[loaded] = store_llty
							arg_val = loaded
							autoloaded_from_storage = True
						else:
							raise NotImplementedError(
								f"LLVM codegen v1: ConstructVariant field {idx} type mismatch (have {have}, expected {want_llty})"
							)
					field_ptr = self._fresh("fieldptr")
					self.lines.append(
						f"  {field_ptr} = getelementptr inbounds {arm_layout.payload_struct_llty}, ptr {payload_struct_ptr}, i32 0, i32 {idx}"
					)
					needs_bool_xform = (
						self._is_bool_storage_pair(value_llty=want_llty, storage_llty=store_llty)
						and not autoloaded_from_storage
					)
					if needs_bool_xform:
						arg_val = self._bool_to_storage(arg_val)
						self.lines.append(f"  store i8 {arg_val}, ptr {field_ptr}")
					else:
						self.lines.append(f"  store {store_llty} {arg_val}, ptr {field_ptr}")
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = load {variant_llty}, ptr {tmp_ptr}")
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
			self.lines.append(f"  {dest} = zext i8 {raw} to {self._llty(DRIFT_UINT_TYPE)}")
			self.value_types[dest] = DRIFT_UINT_TYPE
		elif isinstance(instr, VariantTagRef):
			layout = self._variant_layout(instr.variant_ty)
			variant_llty = layout.llvm_ty
			variant_ptr = self._map_value(instr.variant_ref)
			have = self.value_types.get(variant_ptr)
			if have is not None and not _is_ptr_type(have):
				raise NotImplementedError(
					f"LLVM codegen v1: VariantTagRef value type mismatch (have {have}, expected ptr)"
				)
			tag_ptr = self._fresh("tagptr")
			self.lines.append(
				f"  {tag_ptr} = getelementptr inbounds {variant_llty}, ptr {variant_ptr}, i32 0, i32 0"
			)
			raw = self._fresh("tag8")
			self.lines.append(f"  {raw} = load i8, ptr {tag_ptr}")
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = zext i8 {raw} to {self._llty(DRIFT_UINT_TYPE)}")
			self.value_types[dest] = DRIFT_UINT_TYPE
		elif isinstance(instr, VariantGetField):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: VariantGetField requires a TypeTable")
			layout = self._variant_layout(instr.variant_ty)
			variant_llty = layout.llvm_ty
			arm_layout = layout.arm_by_name.get(instr.ctor)
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
			tmp_ptr = self._scratch_alloca(variant_llty, "variant")
			self.lines.append(f"  store {variant_llty} {val}, ptr {tmp_ptr}")
			payload_words_ptr = self._fresh("payload_words")
			self.lines.append(
				f"  {payload_words_ptr} = getelementptr inbounds {variant_llty}, ptr {tmp_ptr}, i32 0, i32 2"
			)
			payload_struct_ptr = payload_words_ptr
			field_ptr = self._fresh("fieldptr")
			self.lines.append(
				f"  {field_ptr} = getelementptr inbounds {arm_layout.payload_struct_llty}, ptr {payload_struct_ptr}, i32 0, i32 {instr.field_index}"
			)
			store_llty = arm_layout.field_storage_lltys[instr.field_index]
			want_llty = arm_layout.field_lltys[instr.field_index]
			emit_want_llty = self._llty(want_llty)
			dest = self._map_value(instr.dest)
			if self._is_bool_storage_pair(value_llty=want_llty, storage_llty=store_llty):
				raw = self._fresh("field_byte")
				self.lines.append(f"  {raw} = load i8, ptr {field_ptr}")
				self.lines.append(f"  {dest} = icmp ne i8 {raw}, 0")
				self.value_types[dest] = "i1"
			else:
				transfer = self._classify_payload_extract_transfer(instr.field_ty)
				if transfer == "copy-semantic":
					loaded = self._fresh("field")
					self.lines.append(f"  {loaded} = load {emit_want_llty}, ptr {field_ptr}")
					self.value_types[loaded] = want_llty
					# owned-at-extraction: VariantGetField
					copied = self._emit_copy_value(instr.field_ty, loaded)
					# Materialize a real SSA def for `dest` (instead of aliasing via
					# value_map) so downstream drop/phi logic remains consistent.
					self.lines.append(f"  {dest} = select i1 1, {emit_want_llty} {copied}, {emit_want_llty} {copied}")
					self.value_types[dest] = want_llty
				elif transfer == "copy-bitcopy":
					self.lines.append(f"  {dest} = load {emit_want_llty}, ptr {field_ptr}")
					self.value_types[dest] = want_llty
				elif transfer == "move":
					# Fallback-safe move extraction for direct-MIR paths: load payload
					# value and tombstone the source field so subsequent source drop
					# does not double-release moved ownership.
					self.lines.append(f"  {dest} = load {emit_want_llty}, ptr {field_ptr}")
					self.value_types[dest] = want_llty
					zero = self._fresh("zero")
					self._emit_zero_value(zero, instr.field_ty)
					self.lines.append(f"  store {emit_want_llty} {zero}, ptr {field_ptr}")
				else:
					raise AssertionError(
						"internal: VariantGetField reached LLVM with non-copy payload transfer class "
						f"'{transfer}' (stage2/checker bug)"
					)
		elif isinstance(instr, VariantGetFieldAddr):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: VariantGetFieldAddr requires a TypeTable")
			layout = self._variant_layout(instr.variant_ty)
			variant_llty = layout.llvm_ty
			arm_layout = layout.arm_by_name.get(instr.ctor)
			if arm_layout is None or not arm_layout.payload_struct_llty:
				raise NotImplementedError(
					f"LLVM codegen v1: VariantGetFieldAddr unsupported ctor '{instr.ctor}' for TypeId {instr.variant_ty}"
				)
			variant_ptr = self._map_value(instr.variant_ref)
			have = self.value_types.get(variant_ptr)
			if have is not None and not _is_ptr_type(have):
				raise NotImplementedError(
					f"LLVM codegen v1: VariantGetFieldAddr value type mismatch (have {have}, expected ptr)"
				)
			payload_words_ptr = self._fresh("payload_words")
			self.lines.append(
				f"  {payload_words_ptr} = getelementptr inbounds {variant_llty}, ptr {variant_ptr}, i32 0, i32 2"
			)
			# Opaque pointers: no bitcast needed for pointer-to-pointer casts
			field_ptr = self._fresh("fieldptr")
			self.lines.append(
				f"  {field_ptr} = getelementptr inbounds {arm_layout.payload_struct_llty}, ptr {payload_words_ptr}, i32 0, i32 {instr.field_index}"
			)
			# Opaque pointers: field_ptr is already ptr, no bitcast needed.
			# Alias dest to field_ptr.
			raw = field_ptr[1:] if field_ptr.startswith("%") else field_ptr
			self.aliases[instr.dest] = raw
			dest = self._map_value(instr.dest)
			self.value_types[dest] = "ptr"
			# Record provenance: this resolved pointer originates from a
			# VariantGetFieldAddr, so it is an authorized source for the
			# ConstructVariant payload autoload (borrowed-match reconstruct).
			self.variant_field_addr_ptrs.add(dest)
		elif isinstance(instr, StructGetField):
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: StructGetField requires a TypeTable")
			struct_llty = self._llvm_type_for_typeid(instr.struct_ty)
			subject = self._map_value(instr.subject)
			have_struct = self.value_types.get(subject)
			if (
				self.fn_info.signature is not None
				and self.fn_info.signature.param_type_ids is not None
			):
				for param_index, param_name in enumerate(self.func.params):
					if subject not in (f"%{param_name}", self._map_value(param_name)):
						continue
					param_ty_id = self.fn_info.signature.param_type_ids[param_index]
					param_llty = self._llvm_type_for_typeid(param_ty_id)
					if have_struct is None or have_struct == struct_llty:
						have_struct = param_llty
						self.value_types[subject] = have_struct
					break
			emit_struct_llty = self._llty(struct_llty)
			param_llty = self.param_value_types.get(subject)
			if param_llty and _is_ptr_type(param_llty):
				have_struct = param_llty
			if _is_ptr_type(have_struct):
				tmp_struct = self._fresh("structval")
				self.lines.append(f"  {tmp_struct} = load {emit_struct_llty}, ptr {subject}")
				self.value_types[tmp_struct] = struct_llty
				subject = tmp_struct
				have_struct = struct_llty
			if have_struct is not None and have_struct != struct_llty:
				raise NotImplementedError(
					f"LLVM codegen v1: StructGetField subject type mismatch (have {have_struct}, expected {struct_llty})"
				)
			field_val_llty = self._llvm_type_for_typeid(instr.field_ty)
			field_store_llty = self._llvm_field_storage_type_for_typeid(instr.field_ty)
			field_td = self.type_table.get(instr.field_ty)
			is_fat_fn = field_td.kind is TypeKind.FUNCTION and field_td.can_throw()
			dest = self._map_value(instr.dest)
			if self._is_bool_storage_pair(value_llty=field_val_llty, storage_llty=field_store_llty):
				raw = self._fresh("field_byte")
				self.lines.append(f"  {raw} = extractvalue {struct_llty} {subject}, {instr.field_index}")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
			elif is_fat_fn:
				self.lines.append(f"  {dest} = extractvalue {struct_llty} {subject}, {instr.field_index}")
				self.value_types[dest] = DRIFT_FAT_FNPTR_TYPE
			else:
				self.lines.append(f"  {dest} = extractvalue {struct_llty} {subject}, {instr.field_index}")
				self.value_types[dest] = field_val_llty
		elif isinstance(instr, LoadRef):
			ptr = self._map_value(instr.ptr)
			val_llty = self._llvm_type_for_typeid(instr.inner_ty)
			store_llty = self._llvm_storage_type_for_typeid(instr.inner_ty)
			emit_val_llty = self._llty(val_llty)
			emit_store_llty = self._llty(store_llty)
			ptr_ty = "ptr"
			dest = self._map_value(instr.dest)
			if self._is_bool_storage_pair(value_llty=val_llty, storage_llty=store_llty):
				raw = self._fresh("bool_byte")
				self.lines.append(f"  {raw} = load i8, ptr {ptr}")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
			else:
				self.lines.append(f"  {dest} = load {emit_val_llty}, {ptr_ty} {ptr}")
				self.value_types[dest] = val_llty
		elif isinstance(instr, StoreRef):
			ptr = self._map_value(instr.ptr)
			val_llty = self._llvm_type_for_typeid(instr.inner_ty)
			store_llty = self._llvm_storage_type_for_typeid(instr.inner_ty)
			emit_store_llty = self._llty(store_llty)
			ptr_ty = "ptr"
			val = self._map_value(instr.value)
			if self._is_throwing_fn_typeid(instr.inner_ty):
				val = self._coerce_throwing_fn_to_fat(
					instr.value, val, instr.inner_ty,
					context="StoreRef to throwing Fn slot",
				)
				self.lines.append(f"  store {DRIFT_FAT_FNPTR_TYPE} {val}, {ptr_ty} {ptr}")
				return
			have = self.value_types.get(val)
			if have is not None:
				have_emit = self._llty(have)
				want_emit = self._llty(val_llty)
				store_emit = self._llty(store_llty)
				if have_emit != want_emit and have_emit != store_emit:
					raise NotImplementedError(
						f"LLVM codegen v1: StoreRef value type mismatch (have {have}, expected {val_llty})"
					)
			if self._is_bool_storage_pair(value_llty=val_llty, storage_llty=store_llty):
				val = self._bool_to_storage(val)
			self.lines.append(f"  store {emit_store_llty} {val}, {ptr_ty} {ptr}")
		elif isinstance(instr, MoveFromRef):
			# Atomic ownership transfer: read *ptr, tombstone *ptr,
			# transfer the read value into `local` (no retain).  See the
			# `MoveFromRef` MIR docstring for the contract.
			#
			# **Tombstone safety contract is at the CALLER layer.**
			# Unlike `TombstoneValue` (which produces drop-safe bytes
			# for a slot that WILL still get DropValue'd), `MoveFromRef`
			# transfers ownership AWAY from the slot — each caller
			# must guarantee the tombstoned slot is never subsequently
			# DropValue'd.  For user-Destructible struct fields
			# (`destructor_fns[inner_ty]` set), the tombstone bytes
			# are NOT drop-safe under that destructor; each caller's
			# own surrounding chain must preclude the destructor from
			# running on them.  Two callers exist today:
			#   1. `match_cleanup_authoring` — emits MoveFromRef in
			#      the partial-move branch where the whole-variant
			#      DropValue is suppressed (per-field cleanup IS the
			#      drop authority).
			#   2. `IntrinsicKind.REPLACE` lowering in
			#      `hir_to_mir.py` — emits an immediate `StoreRef`
			#      after MoveFromRef, overwriting the tombstone with
			#      the replacement value before any drop can reach it,
			#      then `MoveOut` drains the temp local as the
			#      expression's SSA result.
			# No codegen-level guard; adding one would refuse the
			# legitimate Token-field carrier
			# (`match_subset_bind_leaves_unbound_fields_dropped`).
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: MoveFromRef requires a TypeTable")
			ptr = self._map_value(instr.ptr)
			val_llty = self._llvm_type_for_typeid(instr.inner_ty)
			store_llty = self._llvm_storage_type_for_typeid(instr.inner_ty)
			emit_val_llty = self._llty(val_llty)
			emit_store_llty = self._llty(store_llty)
			# Step 1: load *ptr into a fresh temp.  Must precede the
			# tombstone-write so we capture the live value bytes.
			loaded = self._fresh("mfr_load")
			if self._is_bool_storage_pair(value_llty=val_llty, storage_llty=store_llty):
				raw = self._fresh("mfr_bool8")
				self.lines.append(f"  {raw} = load i8, ptr {ptr}")
				self._bool_from_storage(raw, dest=loaded)
				self.value_types[loaded] = "i1"
			else:
				self.lines.append(f"  {loaded} = load {emit_val_llty}, ptr {ptr}")
				self.value_types[loaded] = val_llty
			# Step 2 + 3: produce tombstone bytes and write them back to
			# *ptr.  Reuses the shared `_emit_tombstone_value` helper —
			# same byte-pattern dispatch as `TombstoneValue`.
			if store_llty == DRIFT_FAT_FNPTR_TYPE:
				tomb = self._fresh("mfr_tomb")
				self.lines.append(
					f"  {tomb} = select i1 1, {DRIFT_FAT_FNPTR_TYPE} zeroinitializer, {DRIFT_FAT_FNPTR_TYPE} zeroinitializer"
				)
				self.value_types[tomb] = DRIFT_FAT_FNPTR_TYPE
			else:
				tomb = self._emit_tombstone_value(instr.inner_ty)
			tomb_emit_llty = self._llty(self.value_types.get(tomb, val_llty))
			self.lines.append(f"  store {tomb_emit_llty} {tomb}, ptr {ptr}")
			# Step 4: transfer the loaded value into `local`.  Mirrors
			# the StoreLocal lowering above (addr-taken alloca path or
			# SSA value-rename), so any later code that takes the
			# local's address or LoadLocal's it sees the transferred
			# bytes.
			if instr.local in self.addr_taken_locals:
				alloca_id = self._ensure_local_storage(instr.local, store_llty)
				val_for_store = loaded
				if self._is_bool_storage_pair(value_llty=val_llty, storage_llty=store_llty):
					val_for_store = self._bool_to_storage(val_for_store)
				self.lines.append(f"  store {emit_store_llty} {val_for_store}, ptr %{alloca_id}")
				if self.module.debug_enabled and self.type_table is not None:
					ty_id = self.func.local_types.get(instr.local)
					span = getattr(instr, "span", None) or self._dbg_default_span
					if ty_id is not None:
						self._emit_dbg_declare(instr.local, ty_id, alloca_id, store_llty, span)
			else:
				# Pure-SSA local: thread the loaded value through the
				# SSA rename map (same shape as StoreLocal's SSA branch).
				block_name = getattr(self, "_current_block_name", None)
				if instr_index is not None and block_name is not None:
					ssa_name = self.ssa.value_for_instr.get((block_name, instr_index))
				else:
					ssa_name = None
				if ssa_name:
					self.aliases[ssa_name] = loaded
					if loaded in self.value_types:
						self.value_types[ssa_name] = self.value_types[loaded]
				if instr.local in self.value_types and loaded in self.value_types:
					self.value_types[instr.local] = self.value_types[loaded]
		elif isinstance(instr, BinaryOpInstr):
			self._lower_binary(instr)
		elif isinstance(instr, FnPtrConst):
			self._lower_fnptr_const(instr)
		elif isinstance(instr, Call):
			self._lower_call(instr)
		elif isinstance(instr, CallIndirect):
			self._lower_call_indirect(instr)
		elif isinstance(instr, CallIface):
			self._lower_call_iface(instr)
		elif isinstance(instr, ResultIsErr):
			dest = self._map_value(instr.dest)
			res = self._map_value(instr.result)
			fnres_llty = self.value_types.get(res)
			if fnres_llty is None:
				raise NotImplementedError("LLVM codegen v1: ResultIsErr requires a typed FnResult value")
			raw = self._fresh("is_err_raw")
			self.lines.append(f"  {raw} = extractvalue {fnres_llty} {res}, 0")
			self.lines.append(f"  {dest} = icmp ne i8 {raw}, 0")
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
			ok_tid = self.module._fnresult_ok_typeid_by_type.get(fnres_llty)
			if ok_tid is not None and self.type_table is not None:
				ok_td = self.type_table.get(ok_tid)
				if ok_td.kind is TypeKind.SCALAR and ok_td.name == "Bool":
					raw = self._fresh("ok_byte")
					self.lines.append(f"  {raw} = extractvalue {fnres_llty} {res}, 1")
					self._bool_from_storage(raw, dest=dest)
					self.value_types[dest] = "i1"
					return
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
				# Surface `return;` in a can-throw `-> Void` function: there is no
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
				ok_tid = self.module._fnresult_ok_typeid_by_type.get(fnres_llty)
				if ok_tid is not None and self.type_table is not None:
					val = self._coerce_value_to_typeid(instr.value, val, ok_tid, context=f"FnResult.Ok in {self.fn_info.name}")
				val_ty = self.value_types.get(val)
				if val_ty is not None and val_ty != ok_llty:
					if ok_tid is not None and self.type_table is not None:
						ok_td = self.type_table.get(ok_tid)
						if ok_td.kind is TypeKind.SCALAR and ok_td.name == "Bool" and ok_llty == "i8" and val_ty == "i1":
							val = self._bool_to_storage(val)
						else:
							raise NotImplementedError(
								f"LLVM codegen v1: ok payload type mismatch for ConstructResultOk in {self.fn_info.name}: "
								f"have {val_ty}, expected {ok_llty}"
							)
					else:
						raise NotImplementedError(
							f"LLVM codegen v1: ok payload type mismatch for ConstructResultOk in {self.fn_info.name}: "
							f"have {val_ty}, expected {ok_llty}"
						)
			tmp0 = self._fresh("ok_a")
			tmp1 = self._fresh("ok_b")
			err_zero = f"{DRIFT_ERROR_PTR} null"
			self.lines.append(f"  {tmp0} = insertvalue {fnres_llty} zeroinitializer, i8 0, 0")
			emit_ok_llty = self._llty(ok_llty)
			self.lines.append(f"  {tmp1} = insertvalue {fnres_llty} {tmp0}, {emit_ok_llty} {val}, 1")
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
			tmp0 = self._fresh("err_a")
			tmp1 = self._fresh("err_b")
			ok_zero = self._zero_value_for_ok(ok_llty)
			self.lines.append(f"  {tmp0} = insertvalue {fnres_llty} zeroinitializer, i8 1, 0")
			self.lines.append(f"  {tmp1} = insertvalue {fnres_llty} {tmp0}, {ok_zero}, 1")
			self.lines.append(f"  {dest} = insertvalue {fnres_llty} {tmp1}, {DRIFT_ERROR_PTR} {err_val}, 2")
		elif isinstance(instr, ConstructError):
			dest = self._map_value(instr.dest)
			code = self._map_value(instr.code)
			event_fqn = self._map_value(instr.event_fqn)
			self.value_types[dest] = DRIFT_ERROR_PTR
			self.module.needs_error_runtime = True
			code_ty = self.value_types.get(code)
			if code_ty != DRIFT_ERROR_CODE_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: error code must be Uint64 (u64), got {code_ty}"
				)
			event_fqn_ty = self.value_types.get(event_fqn)
			if event_fqn_ty != DRIFT_STRING_TYPE:
				raise NotImplementedError(
					f"LLVM codegen v1: event_fqn must be String ({DRIFT_STRING_TYPE}), got {event_fqn_ty}"
				)
			# Slice 7c-1 contract guard: `ConstructError(payload=DV,
			# attr_key=K)` was the legacy DV-attachment shape.  Slice
			# 7b retired it — production lowering always passes
			# `payload=None, attr_key=None` and uses `ExcSetParamsJson`
			# for the JSON-text params.  Reaching codegen with a non-
			# None payload at ABI 14 is a contract failure.
			if instr.payload is not None or instr.attr_key is not None:
				raise AssertionError(
					"M.ConstructError reached LLVM codegen with a "
					"non-empty DV payload at ABI 14.  No production "
					"lowering should emit this shape post-Slice 7b "
					"(the unified Diagnostic owning-throw path always "
					"uses ConstructError(payload=None) + "
					"ExcSetParamsJson).  Compiler bug."
				)
			self.lines.append(
				f"  {dest} = call {DRIFT_ERROR_PTR} @drift_error_new({DRIFT_ERROR_CODE_TYPE} {code}, {DRIFT_STRING_TYPE} {event_fqn})"
			)
		elif isinstance(instr, ErrorRaise):
			self.module.needs_error_runtime = True
			err_val = self._map_value(instr.error)
			self.lines.append(f"  call void @drift_error_raise({DRIFT_ERROR_PTR} {err_val})")
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
			self.value_types[dest] = DRIFT_ERROR_CODE_TYPE
		elif isinstance(instr, ErrorEventFqn):
			# Slice 3 DV→JSON: extract event_fqn (DriftError field 1) and
			# retain so dest is independently owned.  No new runtime
			# helper — reuses drift_string_retain.
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			err_ty = self.value_types.get(err_val)
			if err_ty is None:
				err_ty = DRIFT_ERROR_PTR
				self.value_types[err_val] = err_ty
			if err_ty != DRIFT_ERROR_PTR:
				raise NotImplementedError(
					f"LLVM codegen v1: ErrorEventFqn expects {DRIFT_ERROR_PTR}, got {err_ty}"
				)
			self.module.needs_string_retain = True
			loaded = self._fresh("err_val")
			self.lines.append(f"  {loaded} = load {DRIFT_ERROR_TYPE}, {DRIFT_ERROR_PTR} {err_val}")
			alias = self._fresh("err_fqn_alias")
			self.lines.append(f"  {alias} = extractvalue {DRIFT_ERROR_TYPE} {loaded}, 1")
			self.lines.append(f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_retain({DRIFT_STRING_TYPE} {alias})")
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, ExcGetParamsJson):
			# Phase 1+ DV→JSON migration: read the canonical params JSON
			# string from the runtime; returned String is RETAINED per ABI
			# spec §2.3 (caller releases).
			self.module.needs_error_runtime = True
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_error_get_params_json({DRIFT_ERROR_PTR} {err_val})"
			)
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, ExcSetParamsJson):
			# Phase 1+ DV→JSON migration: store the canonical params JSON
			# string in the runtime; runtime takes ownership of the input
			# String per ABI spec §2.3.
			self.module.needs_error_runtime = True
			err_val = self._map_value(instr.error)
			json_val = self._map_value(instr.json_text)
			self.lines.append(
				f"  call void @drift_error_set_params_json({DRIFT_ERROR_PTR} {err_val}, {DRIFT_STRING_TYPE} {json_val})"
			)
		elif isinstance(instr, ExcGetContextJson):
			# Slice 2 DV→JSON: read the canonical context JSON array
			# string from the runtime; returned String is RETAINED per
			# ABI spec §2.3 (caller releases).
			self.module.needs_error_runtime = True
			dest = self._map_value(instr.dest)
			err_val = self._map_value(instr.error)
			self.lines.append(
				f"  {dest} = call {DRIFT_STRING_TYPE} @drift_error_get_context_json({DRIFT_ERROR_PTR} {err_val})"
			)
			self.value_types[dest] = DRIFT_STRING_TYPE
		elif isinstance(instr, ExcAppendContextFrame):
			# Slice 2 DV→JSON: append a captured-frame JSON object to
			# the stored context array; runtime takes ownership of the
			# input String per ABI spec §2.3.
			self.module.needs_error_runtime = True
			err_val = self._map_value(instr.error)
			frame_val = self._map_value(instr.frame_json)
			self.lines.append(
				f"  call void @drift_error_append_context_frame({DRIFT_ERROR_PTR} {err_val}, {DRIFT_STRING_TYPE} {frame_val})"
			)
		elif isinstance(instr, Phi):
			# Already handled in _lower_phi.
			return
		else:
			raise NotImplementedError(f"LLVM codegen v1: unsupported instr {type(instr).__name__}")

	def _emit_string_observe_guard(self, len_val: str, storage_val: str) -> None:
		"""§2.6 observation contract at the compiler's three layout-authority
		observation lowerings: tombstone/malformed handles fail closed
		BEFORE any length/storage use (mirrors the runtime accessors)."""
		self.module.needs_string_observe_guard = True
		self.lines.append(
			f"  call void @__drift_string_observe_guard({self._llty(DRIFT_INT_TYPE)} {len_val}, ptr {storage_val})"
		)

	def _string_literal_flags(self, utf8_bytes: bytes) -> int:
		"""ABI-22 literal flags, computed at COMPILE TIME from the bytes:
		STATIC (compiler rodata) + NUL_SCANNED (the compiler knows the
		bytes) + HAS_INTERIOR_NUL when applicable.  Values pinned by the
		runtime header (DRIFT_RCBYTES_*); part of the codegen layout
		authority."""
		flags = 1 | 4  # STATIC | NUL_SCANNED
		if b"\x00" in utf8_bytes:
			flags |= 8  # HAS_INTERIOR_NUL
		return flags

	def _emit_empty_singleton_handle(self, dest: str) -> None:
		"""Empty literals lower to {0, @__drift_rt_string_empty} — the ONE
		runtime-owned immortal empty block (ABI-22 §2.6), not a
		per-module constant."""
		if not getattr(self.module, "_empty_string_declared", False):
			self.module.consts.append(
				"@__drift_rt_string_empty = external hidden constant { { i64, i64 }, [1 x i8] }"
			)
			self.module._empty_string_declared = True
		tmp0 = self._fresh("str_a")
		self.lines.append(f"  {tmp0} = insertvalue {DRIFT_STRING_TYPE} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} 0, 0")
		self.lines.append(f"  {dest} = insertvalue {DRIFT_STRING_TYPE} {tmp0}, ptr @__drift_rt_string_empty, 1")
		self.value_types[dest] = DRIFT_STRING_TYPE

	def _lower_const_string(self, instr: ConstString) -> None:
		"""
		Lower a ConstString to a DriftString literal ({len: i64, storage: ptr}).

		ABI 22 (B-repr/B5): the constant is a DriftRcBytes block —
		{strong, flags, [N+1 x i8]} with the header at OFFSET 0 — and the
		handle's pointer targets the HEADER (field 0), not the bytes.
		Flags are computed per literal at compile time (STATIC |
		NUL_SCANNED | maybe HAS_INTERIOR_NUL); retain/release is a no-op
		for static literals.  Empty literals resolve to the runtime empty
		singleton.

		Literals are encoded as UTF-8 and emitted with explicit escapes so that
		non-ASCII and special characters are preserved exactly.
		"""
		dest = self._map_value(instr.dest)
		utf8_bytes = instr.value.encode("utf-8")
		size = len(utf8_bytes)
		if size == 0:
			self._emit_empty_singleton_handle(dest)
			return
		global_name = f"@.str{len(self.module.consts)}"
		header_llty = f"{{ {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, [{size + 1} x i8] }}"
		escaped = "".join(_escape_byte(b) for b in utf8_bytes) + "\\00"
		flags = self._string_literal_flags(utf8_bytes)
		self.module.consts.append(
			f"{global_name} = private unnamed_addr constant {header_llty} "
			f"{{ {self._llty(DRIFT_INT_TYPE)} 1, {self._llty(DRIFT_INT_TYPE)} {flags}, [{size + 1} x i8] c\"{escaped}\" }}"
		)
		ptr = self._fresh("strptr")
		self.lines.append(
			f"  {ptr} = getelementptr inbounds {header_llty}, ptr {global_name}, i32 0, i32 0"
		)
		tmp0 = self._fresh("str_a")
		self.lines.append(f"  {tmp0} = insertvalue {DRIFT_STRING_TYPE} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} {size}, 0")
		self.lines.append(f"  {dest} = insertvalue {DRIFT_STRING_TYPE} {tmp0}, ptr {ptr}, 1")
		self.value_types[dest] = DRIFT_STRING_TYPE

	def _emit_string_literal_value(self, value: str, *, dest_name: str = "") -> str:
		utf8_bytes = value.encode("utf-8")
		size = len(utf8_bytes)
		if size == 0:
			dest = dest_name or self._fresh("str")
			self._emit_empty_singleton_handle(dest)
			return dest
		cache = self.module.string_literal_cache
		if value in cache:
			global_name, header_llty, cached_size = cache[value]
			size = cached_size
		else:
			global_name = f"@.str{len(self.module.consts)}"
			header_llty = f"{{ {self._llty(DRIFT_INT_TYPE)}, {self._llty(DRIFT_INT_TYPE)}, [{size + 1} x i8] }}"
			escaped = "".join(_escape_byte(b) for b in utf8_bytes) + "\\00"
			flags = self._string_literal_flags(utf8_bytes)
			self.module.consts.append(
				f"{global_name} = private unnamed_addr constant {header_llty} "
				f"{{ {self._llty(DRIFT_INT_TYPE)} 1, {self._llty(DRIFT_INT_TYPE)} {flags}, [{size + 1} x i8] c\"{escaped}\" }}"
			)
			cache[value] = (global_name, header_llty, size)
		ptr = self._fresh("strptr")
		self.lines.append(
			f"  {ptr} = getelementptr inbounds {header_llty}, ptr {global_name}, i32 0, i32 0"
		)
		tmp0 = self._fresh("str_a")
		self.lines.append(f"  {tmp0} = insertvalue {DRIFT_STRING_TYPE} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} {size}, 0")
		dest = dest_name or self._fresh("str")
		self.lines.append(f"  {dest} = insertvalue {DRIFT_STRING_TYPE} {tmp0}, ptr {ptr}, 1")
		self.value_types[dest] = DRIFT_STRING_TYPE
		return dest

	def _ensure_ffi_site(self, symbol: str, file_s: str, line_n: int) -> str:
		"""Return the global name of a rodata DriftFfiSite {ptr,ptr,i64}
		for this extern "C" callsite, emitting it (and its two C strings)
		once per unique (symbol, file, line).

		Blocking-FFI observability (ABI 21): `drift_ffi_enter` stores ONE
		pointer to this immortal record on the current VT, so the
		liveness walker's single acquire load always observes a
		consistent triple — the layout is the ABI contract with
		`DriftFfiSite` in liveness_runtime.h."""
		cache = getattr(self.module, "_ffi_site_cache", None)
		if cache is None:
			cache = {}
			setattr(self.module, "_ffi_site_cache", cache)
		key = (symbol, file_s, line_n)
		existing = cache.get(key)
		if existing is not None:
			return existing
		idx = len(cache)
		def _cstr(name: str, text: str) -> str:
			data = text.encode("utf-8", errors="replace")
			esc = "".join(
				chr(b) if 32 <= b < 127 and chr(b) not in ('"', "\\") else f"\\{b:02X}"
				for b in data
			)
			self.module.consts.append(
				f'@{name} = private unnamed_addr constant [{len(data) + 1} x i8] c"{esc}\\00"'
			)
			return name
		sym_g = _cstr(f"__drift_ffi_sym_{idx}", symbol)
		file_g = _cstr(f"__drift_ffi_file_{idx}", file_s)
		site = f"__drift_ffi_site_{idx}"
		self.module.consts.append(
			f"@{site} = private unnamed_addr constant {{ptr, ptr, i64}} "
			f"{{ptr @{sym_g}, ptr @{file_g}, i64 {line_n}}}"
		)
		cache[key] = site
		return site

	def _lower_call(self, instr: Call) -> None:
		if drift_debug.enabled("llvm") and getattr(instr.fn_id, "module", None) == "main":
			print(f"[drift:debug][llvm] call fn={instr.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
		dest = self._map_value(instr.dest) if instr.dest else None
		callee_info = self.fn_infos.get(instr.fn_id)
		callee_sym = function_symbol(instr.fn_id)
		# ---- extern "C" fast-path: direct C ABI call, no FnResult wrapping ----
		callee_sig = callee_info.signature if callee_info is not None else None
		if callee_sig is not None and getattr(callee_sig, "is_extern_c", False):
			c_symbol = callee_sig.name
			param_tids = list(callee_sig.param_type_ids or [])
			arg_parts: list[str] = []
			for i, tid in enumerate(param_tids):
				llty = self._llty(_extern_c_llvm_type(tid, self.type_table, self.module))
				val = self._map_value(instr.args[i])
				arg_parts.append(f"{llty} {val}")
			args_str = ", ".join(arg_parts)
			ret_tid = callee_sig.return_type_id
			is_void = ret_tid is None or (self.type_table is not None and self.type_table.is_void(ret_tid))
			# Blocking-FFI observability (ABI 21): bracket USER-module
			# extern "C" calls with drift_ffi_enter/exit so a liveness
			# snapshot taken while the C call is in flight names the
			# extern symbol and the Drift callsite.  Scope: externs
			# DECLARED in stdlib/toolchain modules (std.*, lang.*) are
			# runtime-owned plumbing on hot paths (e.g. std.codec's
			# drift_codec_*) and are NOT instrumented — user FFI is
			# where operators get stuck; @intrinsic externs never reach
			# this fast path at all.  C cannot unwind, so the
			# straight-line exit covers every edge.
			_decl_mod = getattr(callee_sig, "module", None) or getattr(instr.fn_id, "module", "") or ""
			_instrument_ffi = not (
				_decl_mod == "std" or _decl_mod.startswith("std.")
				or _decl_mod == "lang" or _decl_mod.startswith("lang.")
			)
			if _instrument_ffi:
				span = getattr(instr, "span", None)
				ffi_file = getattr(span, "file", None) if span is not None else None
				ffi_line = getattr(span, "line", None) if span is not None else None
				site = self._ensure_ffi_site(
					c_symbol,
					str(ffi_file) if ffi_file else "<unknown>",
					int(ffi_line) if isinstance(ffi_line, int) else 0,
				)
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_ffi_enter(ptr @{site})")
			if is_void:
				self.lines.append(f"  call void @{c_symbol}({args_str})")
				if _instrument_ffi:
					self.lines.append("  call void @drift_ffi_exit()")
				if dest is not None:
					# Void-returning extern C called in a value context (e.g. FnResult
					# wrap for a can_throw nothrow extern).  Produce a dummy i8 0.
					self.lines.append(f"  {dest} = add i8 0, 0")
					self.value_types[dest] = "i8"
			else:
				ret_llty = self._llty(_extern_c_llvm_type(ret_tid, self.type_table, self.module))
				if dest is None:
					self.lines.append(f"  call {ret_llty} @{c_symbol}({args_str})")
				else:
					self.lines.append(f"  {dest} = call {ret_llty} @{c_symbol}({args_str})")
					self.value_types[dest] = _extern_c_llvm_type(ret_tid, self.type_table, self.module)
				if _instrument_ffi:
					self.lines.append("  call void @drift_ffi_exit()")
			return
		if instr.fn_id.module == "std.core" and instr.fn_id.name == "string_from_utf8_bytes":
			if len(instr.args) != 2:
				raise NotImplementedError(f"LLVM codegen v1: string_from_utf8_bytes expects 2 args, got {len(instr.args)}")
			if dest is None:
				raise NotImplementedError("LLVM codegen v1: string_from_utf8_bytes result must be captured")
			ptr_val = self._map_value(instr.args[0])
			len_val = self._map_value(instr.args[1])
			self.module.needs_string_from_utf8_bytes = True
			if instr.can_throw:
				raw_str = self._fresh("sfub_raw")
				self.lines.append(f"  {raw_str} = call {DRIFT_STRING_TYPE} @drift_string_from_utf8_bytes(ptr {ptr_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})")
				self._wrap_ok_fnresult(raw_str, DRIFT_STRING_TYPE, dest, hint="sfub_ok")
			else:
				self.lines.append(f"  {dest} = call {DRIFT_STRING_TYPE} @drift_string_from_utf8_bytes(ptr {ptr_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})")
				self.value_types[dest] = DRIFT_STRING_TYPE
			return
		if instr.fn_id.module == "std.meta":
			if instr.fn_id.name == "caller":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: caller expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: caller result must be captured")
				if callee_info is None or callee_info.signature is None or callee_info.signature.return_type_id is None:
					raise NotImplementedError("LLVM codegen v1: caller missing signature")
				ret_ty = callee_info.signature.return_type_id
				ret_llty = self._llvm_type_for_typeid(ret_ty, allow_void_ok=False)
				module_s = getattr(self.func.fn_id, "module", None) or "<unknown>"
				span = getattr(instr, "span", None)
				file_s = getattr(span, "file", None) if span is not None else None
				line_n = getattr(span, "line", None) if span is not None else None
				file_s = str(file_s) if file_s else "<unknown>"
				line_n = int(line_n) if isinstance(line_n, int) else 0
				module_v = self._emit_string_literal_value(module_s)
				file_v = self._emit_string_literal_value(file_s)
				if instr.can_throw:
					raw_caller = self._fresh("caller_raw")
					tmp0 = self._fresh("caller0")
					self.lines.append(f"  {tmp0} = insertvalue {self._llty(ret_llty)} zeroinitializer, {DRIFT_STRING_TYPE} {module_v}, 0")
					tmp1 = self._fresh("caller1")
					self.lines.append(f"  {tmp1} = insertvalue {self._llty(ret_llty)} {tmp0}, {DRIFT_STRING_TYPE} {file_v}, 1")
					self.lines.append(f"  {raw_caller} = insertvalue {self._llty(ret_llty)} {tmp1}, {self._llty(DRIFT_INT_TYPE)} {line_n}, 2")
					self._wrap_ok_fnresult(raw_caller, ret_llty, dest, hint="caller_ok")
				else:
					tmp0 = self._fresh("caller0")
					self.lines.append(f"  {tmp0} = insertvalue {self._llty(ret_llty)} zeroinitializer, {DRIFT_STRING_TYPE} {module_v}, 0")
					tmp1 = self._fresh("caller1")
					self.lines.append(f"  {tmp1} = insertvalue {self._llty(ret_llty)} {tmp0}, {DRIFT_STRING_TYPE} {file_v}, 1")
					self.lines.append(f"  {dest} = insertvalue {self._llty(ret_llty)} {tmp1}, {self._llty(DRIFT_INT_TYPE)} {line_n}, 2")
					self.value_types[dest] = ret_llty
				return
			if instr.fn_id.name == "build_info":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: build_info expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: build_info result must be captured")
				payload = getattr(self.module, "_build_info_payload", "")
				if not payload:
					raise NotImplementedError("LLVM codegen v1: build_info requires the stamp (emit_build_info not called)")
				if instr.can_throw:
					raw = self._fresh("bi_raw")
					self._emit_string_literal_value(payload, dest_name=raw)
					self._wrap_ok_fnresult(raw, DRIFT_STRING_TYPE, dest, hint="bi_ok")
				else:
					self._emit_string_literal_value(payload, dest_name=dest)
				return
			# Scalar build-info accessors: every value is read from the
			# SAME assembled document build_info() returns (the parsed
			# dict emit_build_info stored) — never re-derived from
			# flags or version constants, so accessors cannot skew
			# against the stamp. The artifact arms bake "" for an
			# unstamped compile (a private sentinel: the document
			# validator rejects empty artifact identity fields, and the
			# public std.meta accessors wrap "" back into
			# Optional::None).
			_BI_SCALARS = {
				"_bi_toolchain_version": ("toolchain", "driftc"),
				"_bi_artifact_name": ("artifact", "name"),
				"_bi_artifact_version": ("artifact", "version"),
				"_bi_artifact_description": ("artifact", "description"),
				"_bi_artifact_license": ("artifact", "license"),
			}
			if instr.fn_id.name in _BI_SCALARS or instr.fn_id.name == "_bi_runtime_abi":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: {instr.fn_id.name} expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError(f"LLVM codegen v1: {instr.fn_id.name} result must be captured")
				doc = getattr(self.module, "_build_info_doc", None)
				if not doc:
					raise NotImplementedError(f"LLVM codegen v1: {instr.fn_id.name} requires the stamp (emit_build_info not called)")
				if instr.fn_id.name == "_bi_runtime_abi":
					n = int(doc["toolchain"]["abi"])
					if instr.can_throw:
						raw = self._fresh("bi_abi")
						self.lines.append(f"  {raw} = add {self._llty(DRIFT_INT_TYPE)} 0, {n}")
						self.value_types[raw] = DRIFT_INT_TYPE
						self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="bi_abi_ok")
					else:
						self.lines.append(f"  {dest} = add {self._llty(DRIFT_INT_TYPE)} 0, {n}")
						self.value_types[dest] = DRIFT_INT_TYPE
					return
				section, key = _BI_SCALARS[instr.fn_id.name]
				container = doc.get(section)
				value = "" if container is None else str(container[key])
				if instr.can_throw:
					raw = self._fresh("bi_scalar")
					self._emit_string_literal_value(value, dest_name=raw)
					self._wrap_ok_fnresult(raw, DRIFT_STRING_TYPE, dest, hint="bi_scalar_ok")
				else:
					self._emit_string_literal_value(value, dest_name=dest)
				return
		if instr.fn_id.module == "std.ffi" and instr.fn_id.name in (
			"ffi_interior_nul_index", "ffi_string_to_owned_cstr",
			"ffi_string_to_owned_cstr_unchecked",
			"ffi_string_to_owned_cbytes_ptr", "ffi_cstr_free",
			"ffi_cbytes_free",
		):
			# std.ffi C-string bridge intrinsics (B-repr/B5 §3.3): pointer-
			# taking runtime helpers over borrowed &String handles.  Plain
			# std.ffi FUNCTIONS fall through to normal call lowering.
			if instr.fn_id.name == "ffi_interior_nul_index":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: ffi_interior_nul_index expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: ffi_interior_nul_index result must be captured")
				s_val = self._map_value(instr.args[0])
				self.module.needs_string_ffi_bridge = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_string_interior_nul_index(ptr {s_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "ffi_string_to_owned_cstr":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: ffi_string_to_owned_cstr expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: ffi_string_to_owned_cstr result must be captured")
				s_val = self._map_value(instr.args[0])
				self.module.needs_string_ffi_bridge = True
				self.lines.append(
					f"  {dest} = call ptr @drift_string_to_owned_cstr(ptr {s_val}, ptr null)"
				)
				self.value_types[dest] = "ptr"
				return
			if instr.fn_id.name == "ffi_string_to_owned_cbytes_ptr":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: ffi_string_to_owned_cbytes_ptr expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: ffi_string_to_owned_cbytes_ptr result must be captured")
				s_val = self._map_value(instr.args[0])
				self.module.needs_string_ffi_bridge = True
				cb_tmp = self._fresh("cbytes")
				self.lines.append(
					f"  {cb_tmp} = call {{ ptr, {self._llty(DRIFT_INT_TYPE)} }} @drift_string_to_owned_cbytes(ptr {s_val})"
				)
				self.lines.append(f"  {dest} = extractvalue {{ ptr, {self._llty(DRIFT_INT_TYPE)} }} {cb_tmp}, 0")
				self.value_types[dest] = "ptr"
				return
			if instr.fn_id.name == "ffi_string_to_owned_cstr_unchecked":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: ffi_string_to_owned_cstr_unchecked expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: ffi_string_to_owned_cstr_unchecked result must be captured")
				s_val = self._map_value(instr.args[0])
				self.module.needs_string_ffi_bridge = True
				self.lines.append(
					f"  {dest} = call ptr @drift_string_to_owned_cstr_unchecked(ptr {s_val})"
				)
				self.value_types[dest] = "ptr"
				return
			if instr.fn_id.name == "ffi_cbytes_free":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: ffi_cbytes_free expects 2 args, got {len(instr.args)}")
				p_val = self._map_value(instr.args[0])
				len_val = self._map_value(instr.args[1])
				self.module.needs_string_ffi_bridge = True
				cb0 = self._fresh("cbytes")
				cb1 = self._fresh("cbytes")
				cb_ty = f"{{ ptr, {self._llty(DRIFT_INT_TYPE)} }}"
				self.lines.append(f"  {cb0} = insertvalue {cb_ty} zeroinitializer, ptr {p_val}, 0")
				self.lines.append(f"  {cb1} = insertvalue {cb_ty} {cb0}, {self._llty(DRIFT_INT_TYPE)} {len_val}, 1")
				self.lines.append(f"  call void @drift_cbytes_free({cb_ty} {cb1})")
				if dest:
					self.lines.append(f"  {dest} = add i8 0, 0")
					self.value_types[dest] = "i8"
				return
			if instr.fn_id.name == "ffi_cstr_free":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: ffi_cstr_free expects 1 arg, got {len(instr.args)}")
				p_val = self._map_value(instr.args[0])
				self.module.needs_string_ffi_bridge = True
				self.lines.append(f"  call void @drift_cstr_free(ptr {p_val})")
				if dest:
					self.lines.append(f"  {dest} = add i8 0, 0")
					self.value_types[dest] = "i8"
				return
			raise AssertionError(f"unreachable: gated std.ffi intrinsic '{instr.fn_id.name}' fell through its arms")
		if instr.fn_id.module == "lang.thread":
			if instr.fn_id.name == "vt_spawn":
				if callee_info is None or callee_info.signature is None or callee_info.signature.return_type_id is None:
					raise NotImplementedError(f"LLVM codegen v1: missing signature for lang.thread intrinsic {callee_sym}")
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: vt_spawn expects 2 args, got {len(instr.args)}")
				if callee_info.signature.param_type_ids is None or len(callee_info.signature.param_type_ids) != 2:
					raise NotImplementedError("LLVM codegen v1: vt_spawn signature missing param types")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_spawn result must be captured")
				cb_ty = callee_info.signature.param_type_ids[0]
				cb_llty = self._llvm_type_for_typeid(cb_ty, allow_void_ok=True)
				cb_val = self._map_value(instr.args[0])
				exec_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				cb_addr = self._scratch_alloca(self._llty(cb_llty), "cb_addr")
				self.lines.append(f"  store {self._llty(cb_llty)} {cb_val}, ptr {cb_addr}")
				if instr.can_throw:
					raw = self._fresh("spawn_raw")
					self.lines.append(
						f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_spawn(ptr {cb_addr}, {self._llty(DRIFT_INT_TYPE)} {exec_val})"
					)
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="spawn_ok")
				else:
					self.lines.append(
						f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_spawn(ptr {cb_addr}, {self._llty(DRIFT_INT_TYPE)} {exec_val})"
					)
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_join":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_join expects 1 arg, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_join({self._llty(DRIFT_INT_TYPE)} {vt_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vj_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_join returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_join_timeout":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: vt_join_timeout expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_join_timeout result must be captured")
				vt_val = self._map_value(instr.args[0])
				timeout_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("jt_raw")
					self.lines.append(
						f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_join_timeout({self._llty(DRIFT_INT_TYPE)} {vt_val}, {self._llty(DRIFT_INT_TYPE)} {timeout_val})"
					)
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="jt_ok")
				else:
					self.lines.append(
						f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_join_timeout({self._llty(DRIFT_INT_TYPE)} {vt_val}, {self._llty(DRIFT_INT_TYPE)} {timeout_val})"
					)
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_is_completed":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_is_completed expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_is_completed result must be captured")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("vic_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_is_completed({self._llty(DRIFT_INT_TYPE)} {vt_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="vic_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_is_completed({self._llty(DRIFT_INT_TYPE)} {vt_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_cancel":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_cancel expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_cancel result must be captured")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("vc_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_cancel({self._llty(DRIFT_INT_TYPE)} {vt_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="vc_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_cancel({self._llty(DRIFT_INT_TYPE)} {vt_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_drop":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_drop expects 1 arg, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_drop({self._llty(DRIFT_INT_TYPE)} {vt_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vd_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_drop returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_current":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: vt_current expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_current result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("vtc_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_current()")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="vtc_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_current()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_is_cancelled":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: vt_is_cancelled expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_is_cancelled result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("vic_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_is_cancelled()")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="vic_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_is_cancelled()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_park":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_park expects 1 arg, got {len(instr.args)}")
				reason_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_park({self._llty(DRIFT_INT_TYPE)} {reason_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vp_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_park returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_park_until":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_park_until expects 1 arg, got {len(instr.args)}")
				deadline_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_park_until({self._llty(DRIFT_INT_TYPE)} {deadline_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vpu_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_park_until returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "exec_set_name":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: exec_set_name expects 2 args, got {len(instr.args)}")
				exec_val = self._map_value(instr.args[0])
				name_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_exec_set_name({self._llty(DRIFT_INT_TYPE)} {exec_val}, {DRIFT_STRING_TYPE} {name_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="esn_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: exec_set_name returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_set_op":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: vt_set_op expects 2 args, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				label_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_vt_set_op({self._llty(DRIFT_INT_TYPE)} {vt_val}, {DRIFT_STRING_TYPE} {label_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vso_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_set_op returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_set_wait":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: vt_set_wait expects 2 args, got {len(instr.args)}")
				kind_val = self._map_value(instr.args[0])
				id_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_set_wait({self._llty(DRIFT_INT_TYPE)} {kind_val}, {self._llty(DRIFT_INT_TYPE)} {id_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vsw_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_set_wait returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_unpark":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_unpark expects 1 arg, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_unpark({self._llty(DRIFT_INT_TYPE)} {vt_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vu_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_unpark returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_yield":
				self.module.needs_thread_runtime = True
				self.lines.append("  call void @drift_thread_yield()")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vy_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_yield returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "now_ms":
				self.module.needs_thread_runtime = True
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: now_ms expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: now_ms result must be captured")
				if instr.can_throw:
					raw_ms = self._fresh("nms_raw")
					self.lines.append(f"  {raw_ms} = call {self._llty(DRIFT_INT_TYPE)} @drift_time_now_ms()")
					self._wrap_ok_fnresult(raw_ms, DRIFT_INT_TYPE, dest, hint="nms_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_time_now_ms()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "now_us":
				self.module.needs_thread_runtime = True
				assert self.module.word_bits == 64, "now_us requires a 64-bit target"
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: now_us expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: now_us result must be captured")
				if instr.can_throw:
					raw_us = self._fresh("nus_raw")
					self.lines.append(f"  {raw_us} = call i64 @drift_time_now_us()")
					self._wrap_ok_fnresult(raw_us, DRIFT_INT_TYPE, dest, hint="nus_ok")
				else:
					self.lines.append(f"  {dest} = call i64 @drift_time_now_us()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "now_utc_us":
				self.module.needs_thread_runtime = True
				assert self.module.word_bits == 64, "now_utc_us requires a 64-bit target"
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: now_utc_us expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: now_utc_us result must be captured")
				if instr.can_throw:
					raw_utc_us = self._fresh("nutcus_raw")
					self.lines.append(f"  {raw_utc_us} = call i64 @drift_time_now_utc_us()")
					self._wrap_ok_fnresult(raw_utc_us, DRIFT_INT_TYPE, dest, hint="nutcus_ok")
				else:
					self.lines.append(f"  {dest} = call i64 @drift_time_now_utc_us()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "exec_default_get":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: exec_default_get expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: exec_default_get result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw_exec = self._fresh("edg_raw")
					self.lines.append(f"  {raw_exec} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_default_get()")
					self._wrap_ok_fnresult(raw_exec, DRIFT_INT_TYPE, dest, hint="edg_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_default_get()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "exec_default_set":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: exec_default_set expects 1 arg, got {len(instr.args)}")
				exec_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_exec_default_set({self._llty(DRIFT_INT_TYPE)} {exec_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="eds_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: exec_default_set returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "exec_create":
				if len(instr.args) != 6:
					raise NotImplementedError(f"LLVM codegen v1: exec_create expects 6 args, got {len(instr.args)}")
				min_threads = self._map_value(instr.args[0])
				max_threads = self._map_value(instr.args[1])
				queue_limit = self._map_value(instr.args[2])
				timeout_ms = self._map_value(instr.args[3])
				saturation = self._map_value(instr.args[4])
				stack_bytes = self._map_value(instr.args[5])
				self.module.needs_thread_runtime = True
				_ec_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_exec_create({self._llty(DRIFT_INT_TYPE)} {min_threads}, {self._llty(DRIFT_INT_TYPE)} {max_threads}, {self._llty(DRIFT_INT_TYPE)} {queue_limit}, {self._llty(DRIFT_INT_TYPE)} {timeout_ms}, {self._llty(DRIFT_INT_TYPE)} {saturation}, {self._llty(DRIFT_INT_TYPE)} {stack_bytes})"
				if dest is None:
					self.lines.append(f"  {_ec_call}")
					return
				if instr.can_throw:
					raw = self._fresh("ec_raw")
					self.lines.append(f"  {raw} = {_ec_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="ec_ok")
				else:
					self.lines.append(f"  {dest} = {_ec_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "exec_submit":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: exec_submit expects 2 args, got {len(instr.args)}")
				exec_val = self._map_value(instr.args[0])
				vt_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				_es_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_exec_submit({self._llty(DRIFT_INT_TYPE)} {exec_val}, {self._llty(DRIFT_INT_TYPE)} {vt_val})"
				if dest is None:
					self.lines.append(f"  {_es_call}")
					return
				if instr.can_throw:
					raw = self._fresh("es_raw")
					self.lines.append(f"  {raw} = {_es_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="es_ok")
				else:
					self.lines.append(f"  {dest} = {_es_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "exec_submit_test_override":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: exec_submit_test_override expects 1 arg, got {len(instr.args)}")
				code_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_exec_submit_test_override({self._llty(DRIFT_INT_TYPE)} {code_val})"
				)
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="esto_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: exec_submit_test_override returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "exec_get_running":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: exec_get_running expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: exec_get_running result must be captured")
				exec_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("egr_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_get_running({self._llty(DRIFT_INT_TYPE)} {exec_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="egr_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_get_running({self._llty(DRIFT_INT_TYPE)} {exec_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "reactor_default_get":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: reactor_default_get expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: reactor_default_get result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("rdg_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_default_get()")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="rdg_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_default_get()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "reactor_default_set":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: reactor_default_set expects 1 arg, got {len(instr.args)}")
				reactor_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_reactor_default_set({self._llty(DRIFT_INT_TYPE)} {reactor_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="rds_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: reactor_default_set returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "reactor_register_io":
				if len(instr.args) != 4:
					raise NotImplementedError(f"LLVM codegen v1: reactor_register_io expects 4 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				interest_val = self._map_value(instr.args[1])
				vt_val = self._map_value(instr.args[2])
				deadline_val = self._map_value(instr.args[3])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_reactor_register_io({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {interest_val}, {self._llty(DRIFT_INT_TYPE)} {vt_val}, {self._llty(DRIFT_INT_TYPE)} {deadline_val})"
				)
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="rri_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: reactor_register_io returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "reactor_register_timer":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: reactor_register_timer expects 2 args, got {len(instr.args)}")
				deadline_val = self._map_value(instr.args[0])
				vt_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_reactor_register_timer({self._llty(DRIFT_INT_TYPE)} {deadline_val}, {self._llty(DRIFT_INT_TYPE)} {vt_val})"
				)
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="rrt_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: reactor_register_timer returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "reactor_check_pending":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: reactor_check_pending expects 2 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				dir_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: reactor_check_pending result must be captured")
				if instr.can_throw:
					raw = self._fresh("rcp_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_check_pending({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {dir_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="rcp_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_check_pending({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {dir_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "reactor_io_charge":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: reactor_io_charge expects 3 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				dir_val = self._map_value(instr.args[1])
				bytes_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: reactor_io_charge result must be captured")
				if instr.can_throw:
					raw = self._fresh("ric_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_io_charge({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {dir_val}, {self._llty(DRIFT_INT_TYPE)} {bytes_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="ric_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_io_charge({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {dir_val}, {self._llty(DRIFT_INT_TYPE)} {bytes_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			# F3 wait-set intrinsics.
			_f3_int_intrinsics = {
				"vt_wait_epoch_begin": ("drift_vt_wait_epoch_begin", 1, "vweb"),
				"reactor_wait_register": ("drift_reactor_wait_register", 4, "rwr"),
				"reactor_wait_collect_pending": ("drift_reactor_wait_collect_pending", 3, "rwcp"),
				"reactor_wait_park": ("drift_reactor_wait_park", 2, "rwp"),
				"reactor_stale_epoch_drops": ("drift_reactor_stale_epoch_drops_get", 0, "rsed"),
				"reactor_close_unparks": ("drift_reactor_close_unparks_get", 0, "rcu"),
				"reactor_park_blocks": ("drift_reactor_park_blocks_get", 0, "rpb"),
				"net_peek_readable": ("drift_net_peek_readable", 1, "npk"),
			}
			if instr.fn_id.name in _f3_int_intrinsics:
				sym, argc, hint = _f3_int_intrinsics[instr.fn_id.name]
				if len(instr.args) != argc:
					raise NotImplementedError(f"LLVM codegen v1: {instr.fn_id.name} expects {argc} args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError(f"LLVM codegen v1: {instr.fn_id.name} result must be captured")
				self.module.needs_thread_runtime = True
				it = self._llty(DRIFT_INT_TYPE)
				argvals = [self._map_value(a) for a in instr.args]
				arglist = ", ".join(f"{it} {v}" for v in argvals)
				if instr.can_throw:
					raw = self._fresh(f"{hint}_raw")
					self.lines.append(f"  {raw} = call {it} @{sym}({arglist})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint=f"{hint}_ok")
				else:
					self.lines.append(f"  {dest} = call {it} @{sym}({arglist})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "reactor_wait_clear":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: reactor_wait_clear expects 1 arg, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_reactor_wait_clear({self._llty(DRIFT_INT_TYPE)} {vt_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="rwc_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: reactor_wait_clear returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "test_eventfd_create":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: test_eventfd_create expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: test_eventfd_create result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("tec_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_test_eventfd_create()")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="tec_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_test_eventfd_create()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "test_eventfd_write":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: test_eventfd_write expects 2 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				val_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_test_eventfd_write({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {val_val})"
				)
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="tew_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: test_eventfd_write returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "test_timerfd_create":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: test_timerfd_create expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: test_timerfd_create result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("ttc_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_test_timerfd_create()")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="ttc_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_test_timerfd_create()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "test_timerfd_set":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: test_timerfd_set expects 2 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				delay_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_test_timerfd_set({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {delay_val})"
				)
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="tts_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: test_timerfd_set returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "io_open":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: io_open expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_open result must be captured")
				path_val = self._map_value(instr.args[0])
				flags_val = self._map_value(instr.args[1])
				mode_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				_io_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_io_open({DRIFT_STRING_TYPE} {path_val}, {self._llty(DRIFT_INT_TYPE)} {flags_val}, {self._llty(DRIFT_INT_TYPE)} {mode_val})"
				if instr.can_throw:
					raw = self._fresh("ioo_raw")
					self.lines.append(f"  {raw} = {_io_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="ioo_ok")
				else:
					self.lines.append(f"  {dest} = {_io_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_close":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: io_close expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_close result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("ioc_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_close({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="ioc_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_close({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_read":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: io_read expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_read result must be captured")
				fd_val = self._map_value(instr.args[0])
				buf_val = self._map_value(instr.args[1])
				len_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				_ir_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_io_read({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})"
				if instr.can_throw:
					raw = self._fresh("ior_raw")
					self.lines.append(f"  {raw} = {_ir_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="ior_ok")
				else:
					self.lines.append(f"  {dest} = {_ir_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_write":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: io_write expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_write result must be captured")
				fd_val = self._map_value(instr.args[0])
				buf_val = self._map_value(instr.args[1])
				len_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				_iw_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_io_write({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})"
				if instr.can_throw:
					raw = self._fresh("iow_raw")
					self.lines.append(f"  {raw} = {_iw_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="iow_ok")
				else:
					self.lines.append(f"  {dest} = {_iw_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_errno":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: io_errno expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_errno result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("ioe_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_errno()")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="ioe_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_errno()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_set_nonblocking":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: io_set_nonblocking expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_set_nonblocking result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("iosn_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_set_nonblocking({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="iosn_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_set_nonblocking({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "runtime_global_registry_ptr":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: runtime_global_registry_ptr expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: runtime_global_registry_ptr result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("rgrp_raw")
					self.lines.append(f"  {raw} = call ptr @drift_runtime_global_registry_ptr()")
					self._wrap_ok_fnresult(raw, "ptr", dest, hint="rgrp_ok")
				else:
					self.lines.append(f"  {dest} = call ptr @drift_runtime_global_registry_ptr()")
					self.value_types[dest] = "ptr"
				return
			if instr.fn_id.name == "runtime_thread_registry_ptr":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: runtime_thread_registry_ptr expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: runtime_thread_registry_ptr result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("rtrp_raw")
					self.lines.append(f"  {raw} = call ptr @drift_runtime_thread_registry_ptr()")
					self._wrap_ok_fnresult(raw, "ptr", dest, hint="rtrp_ok")
				else:
					self.lines.append(f"  {dest} = call ptr @drift_runtime_thread_registry_ptr()")
					self.value_types[dest] = "ptr"
				return
			if instr.fn_id.name == "runtime_registry_set":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: runtime_registry_set expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: runtime_registry_set result must be captured")
				tag_val = self._map_value(instr.args[0])
				ptr_val = self._map_value(instr.args[1])
				dropper_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				dropper_addr = self._ensure_iface_tmp_alloca()
				self.lines.append(f"  store {DRIFT_IFACE_TYPE} {dropper_val}, ptr {dropper_addr}")
				_rrs_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_runtime_registry_set(i64 {tag_val}, ptr {ptr_val}, ptr byval({DRIFT_IFACE_TYPE}) align {self.module.word_bits // 8} {dropper_addr})"
				if instr.can_throw:
					raw = self._fresh("rrs_raw")
					self.lines.append(f"  {raw} = {_rrs_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="rrs_ok")
				else:
					self.lines.append(f"  {dest} = {_rrs_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "runtime_thread_registry_set":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: runtime_thread_registry_set expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: runtime_thread_registry_set result must be captured")
				tag_val = self._map_value(instr.args[0])
				ptr_val = self._map_value(instr.args[1])
				dropper_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				dropper_addr = self._ensure_iface_tmp_alloca()
				self.lines.append(f"  store {DRIFT_IFACE_TYPE} {dropper_val}, ptr {dropper_addr}")
				_rtrs_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_runtime_thread_registry_set(i64 {tag_val}, ptr {ptr_val}, ptr byval({DRIFT_IFACE_TYPE}) align {self.module.word_bits // 8} {dropper_addr})"
				if instr.can_throw:
					raw = self._fresh("rtrs_raw")
					self.lines.append(f"  {raw} = {_rtrs_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="rtrs_ok")
				else:
					self.lines.append(f"  {dest} = {_rtrs_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "runtime_registry_contains":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: runtime_registry_contains expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: runtime_registry_contains result must be captured")
				tag_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("rrc_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_runtime_registry_contains(i64 {tag_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="rrc_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_runtime_registry_contains(i64 {tag_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "runtime_thread_registry_contains":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: runtime_thread_registry_contains expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: runtime_thread_registry_contains result must be captured")
				tag_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("rtrc_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_runtime_thread_registry_contains(i64 {tag_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="rtrc_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_runtime_thread_registry_contains(i64 {tag_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "runtime_registry_get":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: runtime_registry_get expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: runtime_registry_get result must be captured")
				tag_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("rrg_raw")
					self.lines.append(f"  {raw} = call ptr @drift_runtime_registry_get(i64 {tag_val})")
					self._wrap_ok_fnresult(raw, "ptr", dest, hint="rrg_ok")
				else:
					self.lines.append(f"  {dest} = call ptr @drift_runtime_registry_get(i64 {tag_val})")
					self.value_types[dest] = "ptr"
				return
			if instr.fn_id.name == "runtime_thread_registry_get":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: runtime_thread_registry_get expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: runtime_thread_registry_get result must be captured")
				tag_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("rtrg_raw")
					self.lines.append(f"  {raw} = call ptr @drift_runtime_thread_registry_get(i64 {tag_val})")
					self._wrap_ok_fnresult(raw, "ptr", dest, hint="rtrg_ok")
				else:
					self.lines.append(f"  {dest} = call ptr @drift_runtime_thread_registry_get(i64 {tag_val})")
					self.value_types[dest] = "ptr"
				return
			if instr.fn_id.name == "console_write":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: console_write expects 1 arg, got {len(instr.args)}")
				text_val = self._map_value(instr.args[0])
				self.module.needs_console_runtime = True
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_console_write({DRIFT_STRING_TYPE} {text_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="cw_ok")
				return
			if instr.fn_id.name == "console_writeln":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: console_writeln expects 1 arg, got {len(instr.args)}")
				text_val = self._map_value(instr.args[0])
				self.module.needs_console_runtime = True
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_console_writeln({DRIFT_STRING_TYPE} {text_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="cwl_ok")
				return
			if instr.fn_id.name == "console_eprint":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: console_eprint expects 1 arg, got {len(instr.args)}")
				text_val = self._map_value(instr.args[0])
				self.module.needs_console_runtime = True
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_console_eprint({DRIFT_STRING_TYPE} {text_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="cep_ok")
				return
			if instr.fn_id.name == "console_eprintln":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: console_eprintln expects 1 arg, got {len(instr.args)}")
				text_val = self._map_value(instr.args[0])
				self.module.needs_console_runtime = True
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_console_eprintln({DRIFT_STRING_TYPE} {text_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="cepl_ok")
				return
			if instr.fn_id.name == "net_listen":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: net_listen expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_listen result must be captured")
				ip_val = self._map_value(instr.args[0])
				port_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				_nl_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_net_listen(ptr {ip_val}, {self._llty(DRIFT_INT_TYPE)} {port_val})"
				if instr.can_throw:
					raw = self._fresh("nl_raw")
					self.lines.append(f"  {raw} = {_nl_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="nl_ok")
				else:
					self.lines.append(f"  {dest} = {_nl_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_accept":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: net_accept expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_accept result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("na_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_accept({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="na_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_accept({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_connect":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: net_connect expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_connect result must be captured")
				ip_val = self._map_value(instr.args[0])
				port_val = self._map_value(instr.args[1])
				deadline_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				_nc_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_net_connect(ptr {ip_val}, {self._llty(DRIFT_INT_TYPE)} {port_val}, {self._llty(DRIFT_INT_TYPE)} {deadline_val})"
				if instr.can_throw:
					raw = self._fresh("nc_raw")
					self.lines.append(f"  {raw} = {_nc_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="nc_ok")
				else:
					self.lines.append(f"  {dest} = {_nc_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_listener_port":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: net_listener_port expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_listener_port result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("nlp_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_listener_port({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="nlp_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_listener_port({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_peer_addr":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: net_peer_addr expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_peer_addr result must be captured")
				fd_val = self._map_value(instr.args[0])
				out_ip = self._map_value(instr.args[1])
				out_port = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_peer_addr({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {out_ip}, ptr {out_port})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_set_nodelay":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: net_set_nodelay expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_set_nodelay result must be captured")
				fd_val = self._map_value(instr.args[0])
				enabled_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				_nsn_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_net_set_nodelay({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {enabled_val})"
				if instr.can_throw:
					raw = self._fresh("nsn_raw")
					self.lines.append(f"  {raw} = {_nsn_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="nsn_ok")
				else:
					self.lines.append(f"  {dest} = {_nsn_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_get_nodelay":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: net_get_nodelay expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_get_nodelay result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("ngn_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_get_nodelay({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="ngn_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_get_nodelay({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_udp_local_port":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: net_udp_local_port expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_udp_local_port result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("nulp_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_local_port({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="nulp_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_local_port({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_udp_local_port":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: net_udp_local_port expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_udp_local_port result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw = self._fresh("nulp_raw")
					self.lines.append(f"  {raw} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_local_port({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="nulp_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_local_port({self._llty(DRIFT_INT_TYPE)} {fd_val})")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_udp_bind":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: net_udp_bind expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_udp_bind result must be captured")
				ip_val = self._map_value(instr.args[0])
				port_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				_nub_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_bind(ptr {ip_val}, {self._llty(DRIFT_INT_TYPE)} {port_val})"
				if instr.can_throw:
					raw = self._fresh("nub_raw")
					self.lines.append(f"  {raw} = {_nub_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="nub_ok")
				else:
					self.lines.append(f"  {dest} = {_nub_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_udp_bind_v6":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: net_udp_bind_v6 expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_udp_bind_v6 result must be captured")
				ip_val = self._map_value(instr.args[0])
				port_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				_nubv_call = f"call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_bind_v6(ptr {ip_val}, {self._llty(DRIFT_INT_TYPE)} {port_val})"
				if instr.can_throw:
					raw = self._fresh("nubv_raw")
					self.lines.append(f"  {raw} = {_nubv_call}")
					self._wrap_ok_fnresult(raw, DRIFT_INT_TYPE, dest, hint="nubv_ok")
				else:
					self.lines.append(f"  {dest} = {_nubv_call}")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_udp_send_to":
				if len(instr.args) != 5:
					raise NotImplementedError(f"LLVM codegen v1: net_udp_send_to expects 5 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_udp_send_to result must be captured")
				fd_val = self._map_value(instr.args[0])
				ip_val = self._map_value(instr.args[1])
				port_val = self._map_value(instr.args[2])
				buf_val = self._map_value(instr.args[3])
				len_val = self._map_value(instr.args[4])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_send_to({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {ip_val}, {self._llty(DRIFT_INT_TYPE)} {port_val}, ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_udp_send_to_v6":
				if len(instr.args) != 5:
					raise NotImplementedError(f"LLVM codegen v1: net_udp_send_to_v6 expects 5 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_udp_send_to_v6 result must be captured")
				fd_val = self._map_value(instr.args[0])
				ip_val = self._map_value(instr.args[1])
				port_val = self._map_value(instr.args[2])
				buf_val = self._map_value(instr.args[3])
				len_val = self._map_value(instr.args[4])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_send_to_v6({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {ip_val}, {self._llty(DRIFT_INT_TYPE)} {port_val}, ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_udp_recv_from":
				if len(instr.args) != 5:
					raise NotImplementedError(f"LLVM codegen v1: net_udp_recv_from expects 5 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_udp_recv_from result must be captured")
				fd_val = self._map_value(instr.args[0])
				buf_val = self._map_value(instr.args[1])
				len_val = self._map_value(instr.args[2])
				out_ip = self._map_value(instr.args[3])
				out_port = self._map_value(instr.args[4])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_recv_from({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val}, ptr {out_ip}, ptr {out_port})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_udp_recv_from_v6":
				if len(instr.args) != 5:
					raise NotImplementedError(f"LLVM codegen v1: net_udp_recv_from_v6 expects 5 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_udp_recv_from_v6 result must be captured")
				fd_val = self._map_value(instr.args[0])
				buf_val = self._map_value(instr.args[1])
				len_val = self._map_value(instr.args[2])
				out_ip = self._map_value(instr.args[3])
				out_port = self._map_value(instr.args[4])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_udp_recv_from_v6({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val}, ptr {out_ip}, ptr {out_port})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "array_byte_alloc_uninit":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: array_byte_alloc_uninit expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: array_byte_alloc_uninit result must be captured")
				n_val = self._map_value(instr.args[0])
				self.module.needs_array_helpers = True
				arr_llty = self._llvm_array_header_type()
				tmp_alloc = self._fresh("arr")
				self.lines.append(
					f"  {tmp_alloc} = call ptr @drift_alloc_array({self._llty(DRIFT_USIZE_TYPE)} 1, {self._llty(DRIFT_USIZE_TYPE)} 1, {self._llty(DRIFT_INT_TYPE)} 0, {self._llty(DRIFT_INT_TYPE)} {n_val})"
				)
				tmp0 = self._fresh("arrh0")
				tmp1 = self._fresh("arrh1")
				tmp2 = self._fresh("arrh2")
				raw_arr = self._fresh("arrh3") if instr.can_throw else dest
				self.lines.append(f"  {tmp0} = insertvalue {arr_llty} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_LEN_IDX}")
				self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, {self._llty(DRIFT_INT_TYPE)} {n_val}, {ARRAY_CAP_IDX}")
				self.lines.append(f"  {tmp2} = insertvalue {arr_llty} {tmp1}, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_GEN_IDX}")
				self.lines.append(f"  {raw_arr} = insertvalue {arr_llty} {tmp2}, ptr {tmp_alloc}, {ARRAY_PTR_IDX}")
				if instr.can_throw:
					self.value_types[raw_arr] = arr_llty
					self._wrap_ok_fnresult(raw_arr, arr_llty, dest, hint="abau_ok")
				else:
					self.value_types[dest] = arr_llty
				return
			if instr.fn_id.name == "array_byte_as_mut_ptr":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: array_byte_as_mut_ptr expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: array_byte_as_mut_ptr result must be captured")
				ref_val = self._map_value(instr.args[0])
				arr_llty = self._llvm_array_header_type()
				tmp_arr = self._fresh("arr_load")
				self.lines.append(f"  {tmp_arr} = load {arr_llty}, ptr {ref_val}")
				if instr.can_throw:
					raw_ptr = self._fresh("abamp_raw")
					self.lines.append(f"  {raw_ptr} = extractvalue {arr_llty} {tmp_arr}, {ARRAY_PTR_IDX}")
					self.value_types[raw_ptr] = "ptr"
					self._wrap_ok_fnresult(raw_ptr, "ptr", dest, hint="abamp_ok")
				else:
					self.lines.append(f"  {dest} = extractvalue {arr_llty} {tmp_arr}, {ARRAY_PTR_IDX}")
					self.value_types[dest] = "ptr"
				return
			if instr.fn_id.name == "array_byte_commit_init_len":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: array_byte_commit_init_len expects 2 args, got {len(instr.args)}")
				ref_val = self._map_value(instr.args[0])
				len_val = self._map_value(instr.args[1])
				arr_llty = self._llvm_array_header_type()
				self.module.needs_array_helpers = True
				self.lines.append(
					f"  call void @drift_array_byte_commit_init_len(ptr {ref_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})"
				)
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="abcil_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: array_byte_commit_init_len returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "random_fill":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: random_fill expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: random_fill result must be captured")
				buf_val = self._map_value(instr.args[0])
				len_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_random_fill(ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "env_get_raw":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: env_get_raw expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: env_get_raw result must be captured")
				name_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {DRIFT_STRING_TYPE} @drift_env_get({DRIFT_STRING_TYPE} {name_val})"
				)
				self.value_types[dest] = DRIFT_STRING_TYPE
				return
			if instr.fn_id.name == "env_has_raw":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: env_has_raw expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: env_has_raw result must be captured")
				name_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_env_has({DRIFT_STRING_TYPE} {name_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "fs_read_dir":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: fs_read_dir expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: fs_read_dir result must be captured")
				path_val = self._map_value(instr.args[0])
				deadline_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_fs_read_dir({DRIFT_STRING_TYPE} {path_val}, {self._llty(DRIFT_INT_TYPE)} {deadline_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "fs_test_walk_entries":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: fs_test_walk_entries result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_fs_test_walk_entries()"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_test_direct_resume_claims":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_test_direct_resume_claims result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_vt_test_direct_resume_claims()"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name in ("fs_result_status", "fs_result_errno", "fs_result_count", "fs_result_free"):
				if dest is None:
					raise NotImplementedError(f"LLVM codegen v1: {instr.fn_id.name} result must be captured")
				h_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_{instr.fn_id.name}({self._llty(DRIFT_INT_TYPE)} {h_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "fs_result_name":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: fs_result_name result must be captured")
				h_val = self._map_value(instr.args[0])
				idx_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {DRIFT_STRING_TYPE} @drift_fs_result_name({self._llty(DRIFT_INT_TYPE)} {h_val}, {self._llty(DRIFT_INT_TYPE)} {idx_val})"
				)
				self.value_types[dest] = DRIFT_STRING_TYPE
				return
			if instr.fn_id.name == "fs_result_kind":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: fs_result_kind result must be captured")
				h_val = self._map_value(instr.args[0])
				idx_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_fs_result_kind({self._llty(DRIFT_INT_TYPE)} {h_val}, {self._llty(DRIFT_INT_TYPE)} {idx_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "signal_await":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: signal_await result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_signal_await()"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_id":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_id result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_vtid()"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "kernel_thread_id":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: kernel_thread_id result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_tid()"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
		_atomic_intrinsic_names = {
			"atomic_load_bool", "atomic_store_bool", "atomic_exchange_bool", "atomic_compare_exchange_bool", "atomic_compare_exchange_observed_bool",
			"atomic_load_int", "atomic_store_int", "atomic_exchange_int", "atomic_compare_exchange_int", "atomic_compare_exchange_observed_int", "atomic_fetch_add_int", "atomic_fetch_sub_int",
			"atomic_load_uint", "atomic_store_uint", "atomic_exchange_uint", "atomic_compare_exchange_uint", "atomic_compare_exchange_observed_uint", "atomic_fetch_add_uint", "atomic_fetch_sub_uint",
			"atomic_load_uint64", "atomic_store_uint64", "atomic_exchange_uint64", "atomic_compare_exchange_uint64", "atomic_compare_exchange_observed_uint64", "atomic_fetch_add_uint64", "atomic_fetch_sub_uint64",
			"atomic_thread_fence", "atomic_signal_fence",
		}
		_atomic_intrinsic_symbols = {f"lang.atomic::{n}" for n in _atomic_intrinsic_names}
		is_atomic_intrinsic_call = callee_sym in _atomic_intrinsic_symbols or any(callee_sym.endswith(f"::{n}") for n in _atomic_intrinsic_names)
		if is_atomic_intrinsic_call and callee_info is not None and callee_info.signature is not None:
			if callee_info.signature.param_type_ids is None:
				raise NotImplementedError(f"LLVM codegen v1: missing signature for lang.atomic intrinsic {callee_sym}")
			self.module.needs_atomic_runtime = True
			if instr.fn_id.name == "atomic_thread_fence":
				if len(instr.args) != 1:
					raise NotImplementedError("LLVM codegen v1: atomic_thread_fence expects 1 arg")
				order_val = self._map_value(instr.args[0])
				self.lines.append(f"  call void @drift_atomic_thread_fence({self._llty(DRIFT_INT_TYPE)} {order_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: atomic_thread_fence returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "atomic_signal_fence":
				if len(instr.args) != 1:
					raise NotImplementedError("LLVM codegen v1: atomic_signal_fence expects 1 arg")
				order_val = self._map_value(instr.args[0])
				self.lines.append(f"  call void @drift_atomic_signal_fence({self._llty(DRIFT_INT_TYPE)} {order_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: atomic_signal_fence returns Void; result cannot be captured")
				return
			if len(instr.args) < 2:
				raise NotImplementedError(f"LLVM codegen v1: {callee_sym} expects at least 2 args")
			param_ty = callee_info.signature.param_type_ids[0]
			if self.type_table is None:
				raise NotImplementedError("LLVM codegen v1: atomic lowering requires a TypeTable")
			td = self.type_table.get(param_ty)
			if td.kind is not TypeKind.REF or not td.param_types:
				raise NotImplementedError(f"LLVM codegen v1: {callee_sym} expects ref to atomic type")
			inner_ty = td.param_types[0]
			struct_llty = self._llvm_type_for_typeid(inner_ty)
			ptr_val = self._map_value(instr.args[0])
			field_ptr = self._fresh("atomic_ptr")
			self.lines.append(f"  {field_ptr} = getelementptr inbounds {struct_llty}, ptr {ptr_val}, i32 0, i32 0")
			order_val = self._map_value(instr.args[1])
			if instr.fn_id.name == "atomic_load_bool":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: atomic_load_bool result must be captured")
				raw = self._fresh("abool")
				self.lines.append(f"  {raw} = call i8 @drift_atomic_load_bool(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
				return
			if instr.fn_id.name == "atomic_store_bool":
				if len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_store_bool expects 3 args")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				val = self._bool_to_storage(val)
				self.lines.append(f"  call void @drift_atomic_store_bool(ptr {field_ptr}, i8 {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: atomic_store_bool returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "atomic_exchange_bool":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_exchange_bool expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				val = self._bool_to_storage(val)
				raw = self._fresh("abool")
				self.lines.append(f"  {raw} = call i8 @drift_atomic_exchange_bool(ptr {field_ptr}, i8 {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
				return
			if instr.fn_id.name == "atomic_compare_exchange_bool":
				if dest is None or len(instr.args) != 5:
					raise NotImplementedError("LLVM codegen v1: atomic_compare_exchange_bool expects 5 args and captures result")
				expected_val = self._bool_to_storage(self._map_value(instr.args[1]))
				desired_val = self._bool_to_storage(self._map_value(instr.args[2]))
				success_order_val = self._map_value(instr.args[3])
				failure_order_val = self._map_value(instr.args[4])
				raw = self._fresh("abool")
				self.lines.append(f"  {raw} = call i8 @drift_atomic_compare_exchange_bool(ptr {field_ptr}, i8 {expected_val}, i8 {desired_val}, {self._llty(DRIFT_INT_TYPE)} {success_order_val}, {self._llty(DRIFT_INT_TYPE)} {failure_order_val})")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
				return
			if instr.fn_id.name == "atomic_compare_exchange_observed_bool":
				if dest is None or len(instr.args) != 5:
					raise NotImplementedError("LLVM codegen v1: atomic_compare_exchange_observed_bool expects 5 args and captures result")
				expected_val = self._bool_to_storage(self._map_value(instr.args[1]))
				desired_val = self._bool_to_storage(self._map_value(instr.args[2]))
				success_order_val = self._map_value(instr.args[3])
				failure_order_val = self._map_value(instr.args[4])
				raw = self._fresh("abool")
				self.lines.append(f"  {raw} = call i8 @drift_atomic_compare_exchange_observed_bool(ptr {field_ptr}, i8 {expected_val}, i8 {desired_val}, {self._llty(DRIFT_INT_TYPE)} {success_order_val}, {self._llty(DRIFT_INT_TYPE)} {failure_order_val})")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
				return
			if instr.fn_id.name == "atomic_load_int":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: atomic_load_int result must be captured")
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_load_int(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_store_int":
				if len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_store_int expects 3 args")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  call void @drift_atomic_store_int(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: atomic_store_int returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "atomic_exchange_int":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_exchange_int expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_exchange_int(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_compare_exchange_int":
				if dest is None or len(instr.args) != 5:
					raise NotImplementedError("LLVM codegen v1: atomic_compare_exchange_int expects 5 args and captures result")
				expected_val = self._map_value(instr.args[1])
				desired_val = self._map_value(instr.args[2])
				success_order_val = self._map_value(instr.args[3])
				failure_order_val = self._map_value(instr.args[4])
				raw = self._fresh("abool")
				self.lines.append(f"  {raw} = call i8 @drift_atomic_compare_exchange_int(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {expected_val}, {self._llty(DRIFT_INT_TYPE)} {desired_val}, {self._llty(DRIFT_INT_TYPE)} {success_order_val}, {self._llty(DRIFT_INT_TYPE)} {failure_order_val})")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
				return
			if instr.fn_id.name == "atomic_compare_exchange_observed_int":
				if dest is None or len(instr.args) != 5:
					raise NotImplementedError("LLVM codegen v1: atomic_compare_exchange_observed_int expects 5 args and captures result")
				expected_val = self._map_value(instr.args[1])
				desired_val = self._map_value(instr.args[2])
				success_order_val = self._map_value(instr.args[3])
				failure_order_val = self._map_value(instr.args[4])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_compare_exchange_observed_int(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {expected_val}, {self._llty(DRIFT_INT_TYPE)} {desired_val}, {self._llty(DRIFT_INT_TYPE)} {success_order_val}, {self._llty(DRIFT_INT_TYPE)} {failure_order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_fetch_add_int":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_fetch_add_int expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_add_int(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_fetch_sub_int":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_fetch_sub_int expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_sub_int(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_load_uint":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: atomic_load_uint result must be captured")
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_load_uint(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_store_uint":
				if len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_store_uint expects 3 args")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  call void @drift_atomic_store_uint(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: atomic_store_uint returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "atomic_exchange_uint":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_exchange_uint expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_exchange_uint(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_compare_exchange_uint":
				if dest is None or len(instr.args) != 5:
					raise NotImplementedError("LLVM codegen v1: atomic_compare_exchange_uint expects 5 args and captures result")
				expected_val = self._map_value(instr.args[1])
				desired_val = self._map_value(instr.args[2])
				success_order_val = self._map_value(instr.args[3])
				failure_order_val = self._map_value(instr.args[4])
				raw = self._fresh("abool")
				self.lines.append(f"  {raw} = call i8 @drift_atomic_compare_exchange_uint(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {expected_val}, {self._llty(DRIFT_INT_TYPE)} {desired_val}, {self._llty(DRIFT_INT_TYPE)} {success_order_val}, {self._llty(DRIFT_INT_TYPE)} {failure_order_val})")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
				return
			if instr.fn_id.name == "atomic_compare_exchange_observed_uint":
				if dest is None or len(instr.args) != 5:
					raise NotImplementedError("LLVM codegen v1: atomic_compare_exchange_observed_uint expects 5 args and captures result")
				expected_val = self._map_value(instr.args[1])
				desired_val = self._map_value(instr.args[2])
				success_order_val = self._map_value(instr.args[3])
				failure_order_val = self._map_value(instr.args[4])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_compare_exchange_observed_uint(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {expected_val}, {self._llty(DRIFT_INT_TYPE)} {desired_val}, {self._llty(DRIFT_INT_TYPE)} {success_order_val}, {self._llty(DRIFT_INT_TYPE)} {failure_order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_fetch_add_uint":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_fetch_add_uint expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_add_uint(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_fetch_sub_uint":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_fetch_sub_uint expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_sub_uint(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_load_uint64":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: atomic_load_uint64 result must be captured")
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_load_uint64(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_store_uint64":
				if len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_store_uint64 expects 3 args")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  call void @drift_atomic_store_uint64(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: atomic_store_uint64 returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "atomic_exchange_uint64":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_exchange_uint64 expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_exchange_uint64(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_compare_exchange_uint64":
				if dest is None or len(instr.args) != 5:
					raise NotImplementedError("LLVM codegen v1: atomic_compare_exchange_uint64 expects 5 args and captures result")
				expected_val = self._map_value(instr.args[1])
				desired_val = self._map_value(instr.args[2])
				success_order_val = self._map_value(instr.args[3])
				failure_order_val = self._map_value(instr.args[4])
				raw = self._fresh("abool")
				self.lines.append(f"  {raw} = call i8 @drift_atomic_compare_exchange_uint64(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {expected_val}, {self._llty(DRIFT_INT_TYPE)} {desired_val}, {self._llty(DRIFT_INT_TYPE)} {success_order_val}, {self._llty(DRIFT_INT_TYPE)} {failure_order_val})")
				self._bool_from_storage(raw, dest=dest)
				self.value_types[dest] = "i1"
				return
			if instr.fn_id.name == "atomic_compare_exchange_observed_uint64":
				if dest is None or len(instr.args) != 5:
					raise NotImplementedError("LLVM codegen v1: atomic_compare_exchange_observed_uint64 expects 5 args and captures result")
				expected_val = self._map_value(instr.args[1])
				desired_val = self._map_value(instr.args[2])
				success_order_val = self._map_value(instr.args[3])
				failure_order_val = self._map_value(instr.args[4])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_compare_exchange_observed_uint64(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {expected_val}, {self._llty(DRIFT_INT_TYPE)} {desired_val}, {self._llty(DRIFT_INT_TYPE)} {success_order_val}, {self._llty(DRIFT_INT_TYPE)} {failure_order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_fetch_add_uint64":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_fetch_add_uint64 expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_add_uint64(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			if instr.fn_id.name == "atomic_fetch_sub_uint64":
				if dest is None or len(instr.args) != 3:
					raise NotImplementedError("LLVM codegen v1: atomic_fetch_sub_uint64 expects 3 args and captures result")
				val = self._map_value(instr.args[1])
				order_val = self._map_value(instr.args[2])
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_atomic_fetch_sub_uint64(ptr {field_ptr}, {self._llty(DRIFT_INT_TYPE)} {val}, {self._llty(DRIFT_INT_TYPE)} {order_val})")
				self.value_types[dest] = self._llty(DRIFT_INT_TYPE)
				return
			raise NotImplementedError(f"LLVM codegen v1: unsupported lang.atomic intrinsic {callee_sym}")
			if instr.fn_id.name == "vt_join":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_join expects 1 arg, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_join({self._llty(DRIFT_INT_TYPE)} {vt_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: vt_join returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_join_timeout":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: vt_join_timeout expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_join_timeout result must be captured")
				vt_val = self._map_value(instr.args[0])
				timeout_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_join_timeout({self._llty(DRIFT_INT_TYPE)} {vt_val}, {self._llty(DRIFT_INT_TYPE)} {timeout_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_is_completed":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_is_completed expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_is_completed result must be captured")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_is_completed({self._llty(DRIFT_INT_TYPE)} {vt_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_cancel":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_cancel expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_cancel result must be captured")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_cancel({self._llty(DRIFT_INT_TYPE)} {vt_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_drop":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_drop expects 1 arg, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_drop({self._llty(DRIFT_INT_TYPE)} {vt_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: vt_drop returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_current":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: vt_current expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_current result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_current()")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_is_cancelled":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: vt_is_cancelled expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_is_cancelled result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_is_cancelled()")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_id":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: vt_id result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_vtid()"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "kernel_thread_id":
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: kernel_thread_id result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_thread_tid()"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "now_ms":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: now_ms expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: now_ms result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw_ms = self._fresh("nms_raw")
					self.lines.append(f"  {raw_ms} = call {self._llty(DRIFT_INT_TYPE)} @drift_time_now_ms()")
					self._wrap_ok_fnresult(raw_ms, DRIFT_INT_TYPE, dest, hint="nms_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_time_now_ms()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "now_us":
				assert self.module.word_bits == 64, "now_us requires a 64-bit target"
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: now_us expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: now_us result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw_us = self._fresh("nus_raw")
					self.lines.append(f"  {raw_us} = call i64 @drift_time_now_us()")
					self._wrap_ok_fnresult(raw_us, DRIFT_INT_TYPE, dest, hint="nus_ok")
				else:
					self.lines.append(f"  {dest} = call i64 @drift_time_now_us()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "now_utc_us":
				assert self.module.word_bits == 64, "now_utc_us requires a 64-bit target"
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: now_utc_us expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: now_utc_us result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw_utc_us = self._fresh("nutcus_raw")
					self.lines.append(f"  {raw_utc_us} = call i64 @drift_time_now_utc_us()")
					self._wrap_ok_fnresult(raw_utc_us, DRIFT_INT_TYPE, dest, hint="nutcus_ok")
				else:
					self.lines.append(f"  {dest} = call i64 @drift_time_now_utc_us()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "test_eventfd_create":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: test_eventfd_create expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: test_eventfd_create result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_test_eventfd_create()")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "test_eventfd_write":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: test_eventfd_write expects 2 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				val_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_test_eventfd_write({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {val_val})"
				)
				if dest:
					raise NotImplementedError("LLVM codegen v1: test_eventfd_write returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "test_timerfd_create":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: test_timerfd_create expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: test_timerfd_create result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_test_timerfd_create()")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "test_timerfd_set":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: test_timerfd_set expects 2 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				delay_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_test_timerfd_set({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {delay_val})"
				)
				if dest:
					raise NotImplementedError("LLVM codegen v1: test_timerfd_set returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "io_open":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: io_open expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_open result must be captured")
				path_val = self._map_value(instr.args[0])
				flags_val = self._map_value(instr.args[1])
				mode_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_open({DRIFT_STRING_TYPE} {path_val}, {self._llty(DRIFT_INT_TYPE)} {flags_val}, {self._llty(DRIFT_INT_TYPE)} {mode_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_close":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: io_close expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_close result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_close({self._llty(DRIFT_INT_TYPE)} {fd_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_read":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: io_read expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_read result must be captured")
				fd_val = self._map_value(instr.args[0])
				buf_val = self._map_value(instr.args[1])
				len_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_read({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_write":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: io_write expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_write result must be captured")
				fd_val = self._map_value(instr.args[0])
				buf_val = self._map_value(instr.args[1])
				len_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_write({self._llty(DRIFT_INT_TYPE)} {fd_val}, ptr {buf_val}, {self._llty(DRIFT_INT_TYPE)} {len_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_errno":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: io_errno expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_errno result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_errno()")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "io_set_nonblocking":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: io_set_nonblocking expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: io_set_nonblocking result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_io_set_nonblocking({self._llty(DRIFT_INT_TYPE)} {fd_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "console_write":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: console_write expects 1 arg, got {len(instr.args)}")
				text_val = self._map_value(instr.args[0])
				self.module.needs_console_runtime = True
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_console_write({DRIFT_STRING_TYPE} {text_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="cw_ok")
				return
			if instr.fn_id.name == "console_writeln":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: console_writeln expects 1 arg, got {len(instr.args)}")
				text_val = self._map_value(instr.args[0])
				self.module.needs_console_runtime = True
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_console_writeln({DRIFT_STRING_TYPE} {text_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="cwl_ok")
				return
			if instr.fn_id.name == "console_eprint":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: console_eprint expects 1 arg, got {len(instr.args)}")
				text_val = self._map_value(instr.args[0])
				self.module.needs_console_runtime = True
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_console_eprint({DRIFT_STRING_TYPE} {text_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="cep_ok")
				return
			if instr.fn_id.name == "console_eprintln":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: console_eprintln expects 1 arg, got {len(instr.args)}")
				text_val = self._map_value(instr.args[0])
				self.module.needs_console_runtime = True
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_console_eprintln({DRIFT_STRING_TYPE} {text_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="cepl_ok")
				return
			if instr.fn_id.name == "net_listen":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: net_listen expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_listen result must be captured")
				ip_val = self._map_value(instr.args[0])
				port_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_listen(ptr {ip_val}, {self._llty(DRIFT_INT_TYPE)} {port_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_accept":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: net_accept expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_accept result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_accept({self._llty(DRIFT_INT_TYPE)} {fd_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_connect":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: net_connect expects 3 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_connect result must be captured")
				ip_val = self._map_value(instr.args[0])
				port_val = self._map_value(instr.args[1])
				deadline_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_connect(ptr {ip_val}, {self._llty(DRIFT_INT_TYPE)} {port_val}, {self._llty(DRIFT_INT_TYPE)} {deadline_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_listener_port":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: net_listener_port expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_listener_port result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_listener_port({self._llty(DRIFT_INT_TYPE)} {fd_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_set_nodelay":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: net_set_nodelay expects 2 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_set_nodelay result must be captured")
				fd_val = self._map_value(instr.args[0])
				enabled_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_set_nodelay({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {enabled_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "net_get_nodelay":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: net_get_nodelay expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: net_get_nodelay result must be captured")
				fd_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_net_get_nodelay({self._llty(DRIFT_INT_TYPE)} {fd_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "vt_park":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_park expects 1 arg, got {len(instr.args)}")
				reason_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_park({self._llty(DRIFT_INT_TYPE)} {reason_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vp_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_park returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_park_until":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_park_until expects 1 arg, got {len(instr.args)}")
				deadline_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_park_until({self._llty(DRIFT_INT_TYPE)} {deadline_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vpu_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_park_until returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "exec_set_name":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: exec_set_name expects 2 args, got {len(instr.args)}")
				exec_val = self._map_value(instr.args[0])
				name_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_exec_set_name({self._llty(DRIFT_INT_TYPE)} {exec_val}, {DRIFT_STRING_TYPE} {name_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="esn_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: exec_set_name returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_set_op":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: vt_set_op expects 2 args, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				label_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_vt_set_op({self._llty(DRIFT_INT_TYPE)} {vt_val}, {DRIFT_STRING_TYPE} {label_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vso_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_set_op returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_set_wait":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: vt_set_wait expects 2 args, got {len(instr.args)}")
				kind_val = self._map_value(instr.args[0])
				id_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_set_wait({self._llty(DRIFT_INT_TYPE)} {kind_val}, {self._llty(DRIFT_INT_TYPE)} {id_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vsw_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_set_wait returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_unpark":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: vt_unpark expects 1 arg, got {len(instr.args)}")
				vt_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_thread_unpark({self._llty(DRIFT_INT_TYPE)} {vt_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vu_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_unpark returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "vt_yield":
				self.module.needs_thread_runtime = True
				self.lines.append("  call void @drift_thread_yield()")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="vy_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: vt_yield returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "exec_default_get":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: exec_default_get expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: exec_default_get result must be captured")
				self.module.needs_thread_runtime = True
				if instr.can_throw:
					raw_exec = self._fresh("edg_raw")
					self.lines.append(f"  {raw_exec} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_default_get()")
					self._wrap_ok_fnresult(raw_exec, DRIFT_INT_TYPE, dest, hint="edg_ok")
				else:
					self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_default_get()")
					self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "exec_default_set":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: exec_default_set expects 1 arg, got {len(instr.args)}")
				exec_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_exec_default_set({self._llty(DRIFT_INT_TYPE)} {exec_val})")
				if instr.can_throw and dest:
					self._wrap_ok_fnresult(None, "i8", dest, hint="eds_ok")
				elif dest:
					raise NotImplementedError("LLVM codegen v1: exec_default_set returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "exec_create":
				if len(instr.args) != 6:
					raise NotImplementedError(f"LLVM codegen v1: exec_create expects 6 args, got {len(instr.args)}")
				min_threads = self._map_value(instr.args[0])
				max_threads = self._map_value(instr.args[1])
				queue_limit = self._map_value(instr.args[2])
				timeout_ms = self._map_value(instr.args[3])
				saturation = self._map_value(instr.args[4])
				stack_bytes = self._map_value(instr.args[5])
				self.module.needs_thread_runtime = True
				if dest is None:
					self.lines.append(
						f"  call {self._llty(DRIFT_INT_TYPE)} @drift_exec_create({self._llty(DRIFT_INT_TYPE)} {min_threads}, {self._llty(DRIFT_INT_TYPE)} {max_threads}, {self._llty(DRIFT_INT_TYPE)} {queue_limit}, {self._llty(DRIFT_INT_TYPE)} {timeout_ms}, {self._llty(DRIFT_INT_TYPE)} {saturation}, {self._llty(DRIFT_INT_TYPE)} {stack_bytes})"
					)
					return
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_create({self._llty(DRIFT_INT_TYPE)} {min_threads}, {self._llty(DRIFT_INT_TYPE)} {max_threads}, {self._llty(DRIFT_INT_TYPE)} {queue_limit}, {self._llty(DRIFT_INT_TYPE)} {timeout_ms}, {self._llty(DRIFT_INT_TYPE)} {saturation}, {self._llty(DRIFT_INT_TYPE)} {stack_bytes})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "exec_submit":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: exec_submit expects 2 args, got {len(instr.args)}")
				exec_val = self._map_value(instr.args[0])
				vt_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				if dest is None:
					self.lines.append(
						f"  call {self._llty(DRIFT_INT_TYPE)} @drift_exec_submit({self._llty(DRIFT_INT_TYPE)} {exec_val}, {self._llty(DRIFT_INT_TYPE)} {vt_val})"
					)
					return
				self.lines.append(
					f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_submit({self._llty(DRIFT_INT_TYPE)} {exec_val}, {self._llty(DRIFT_INT_TYPE)} {vt_val})"
				)
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "exec_submit_test_override":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: exec_submit_test_override expects 1 arg, got {len(instr.args)}")
				if dest:
					raise NotImplementedError("LLVM codegen v1: exec_submit_test_override returns Void; result cannot be captured")
				code_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_exec_submit_test_override({self._llty(DRIFT_INT_TYPE)} {code_val})"
				)
				return
			if instr.fn_id.name == "exec_get_running":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: exec_get_running expects 1 arg, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: exec_get_running result must be captured")
				exec_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_exec_get_running({self._llty(DRIFT_INT_TYPE)} {exec_val})")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "reactor_default_get":
				if len(instr.args) != 0:
					raise NotImplementedError(f"LLVM codegen v1: reactor_default_get expects 0 args, got {len(instr.args)}")
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: reactor_default_get result must be captured")
				self.module.needs_thread_runtime = True
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_default_get()")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "reactor_default_set":
				if len(instr.args) != 1:
					raise NotImplementedError(f"LLVM codegen v1: reactor_default_set expects 1 arg, got {len(instr.args)}")
				reactor_val = self._map_value(instr.args[0])
				self.module.needs_thread_runtime = True
				self.lines.append(f"  call void @drift_reactor_default_set({self._llty(DRIFT_INT_TYPE)} {reactor_val})")
				if dest:
					raise NotImplementedError("LLVM codegen v1: reactor_default_set returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "reactor_register_io":
				if len(instr.args) != 4:
					raise NotImplementedError(f"LLVM codegen v1: reactor_register_io expects 4 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				interest_val = self._map_value(instr.args[1])
				vt_val = self._map_value(instr.args[2])
				deadline_val = self._map_value(instr.args[3])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_reactor_register_io({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {interest_val}, {self._llty(DRIFT_INT_TYPE)} {vt_val}, {self._llty(DRIFT_INT_TYPE)} {deadline_val})"
				)
				if dest:
					raise NotImplementedError("LLVM codegen v1: reactor_register_io returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "reactor_register_timer":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: reactor_register_timer expects 2 args, got {len(instr.args)}")
				deadline_val = self._map_value(instr.args[0])
				vt_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				self.lines.append(
					f"  call void @drift_reactor_register_timer({self._llty(DRIFT_INT_TYPE)} {deadline_val}, {self._llty(DRIFT_INT_TYPE)} {vt_val})"
				)
				if dest:
					raise NotImplementedError("LLVM codegen v1: reactor_register_timer returns Void; result cannot be captured")
				return
			if instr.fn_id.name == "reactor_check_pending":
				if len(instr.args) != 2:
					raise NotImplementedError(f"LLVM codegen v1: reactor_check_pending expects 2 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				dir_val = self._map_value(instr.args[1])
				self.module.needs_thread_runtime = True
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: reactor_check_pending result must be captured")
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_check_pending({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {dir_val})")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if instr.fn_id.name == "reactor_io_charge":
				if len(instr.args) != 3:
					raise NotImplementedError(f"LLVM codegen v1: reactor_io_charge expects 3 args, got {len(instr.args)}")
				fd_val = self._map_value(instr.args[0])
				dir_val = self._map_value(instr.args[1])
				bytes_val = self._map_value(instr.args[2])
				self.module.needs_thread_runtime = True
				if dest is None:
					raise NotImplementedError("LLVM codegen v1: reactor_io_charge result must be captured")
				self.lines.append(f"  {dest} = call {self._llty(DRIFT_INT_TYPE)} @drift_reactor_io_charge({self._llty(DRIFT_INT_TYPE)} {fd_val}, {self._llty(DRIFT_INT_TYPE)} {dir_val}, {self._llty(DRIFT_INT_TYPE)} {bytes_val})")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			ret_tid = callee_info.signature.return_type_id
			if instr.can_throw:
				if dest is None:
					raise AssertionError("can-throw calls must preserve their FnResult value (MIR bug)")
				ok_llty, fnres_llty = self._fnresult_types_for_fn(callee_info)
				ok_zero = self._zero_value_for_ok(ok_llty)
				tmp0 = self._fresh("fn0")
				tmp1 = self._fresh("fn1")
				self.lines.append(f"  {tmp0} = insertvalue {fnres_llty} zeroinitializer, i8 0, 0")
				self.lines.append(f"  {tmp1} = insertvalue {fnres_llty} {tmp0}, {ok_zero}, 1")
				self.lines.append(f"  {dest} = insertvalue {fnres_llty} {tmp1}, {DRIFT_ERROR_PTR} null, 2")
				self.value_types[dest] = fnres_llty
				return
			if self.type_table.get(ret_tid).kind is TypeKind.VOID:
				if dest:
					raise NotImplementedError(f"LLVM codegen v1: lang.thread intrinsic {callee_sym} returns Void; result cannot be captured")
				return
			llty = self._llvm_type_for_typeid(ret_tid, allow_void_ok=True)
			if dest is None:
				raise NotImplementedError(f"LLVM codegen v1: lang.thread intrinsic {callee_sym} result must be captured")
			self.lines.append(f"  {dest} = add {self._llty(llty)} 0, 0")
			self.value_types[dest] = llty
			return
		if callee_info is None:
			raise NotImplementedError(f"LLVM codegen v1: missing FnInfo for callee {callee_sym}")

		arg_parts: list[str] = []
		if callee_info.signature and callee_info.signature.param_type_ids is not None:
			sig = callee_info.signature
			if len(sig.param_type_ids) != len(instr.args):
				raise NotImplementedError(
					f"LLVM codegen v1: arg count mismatch for {callee_sym}: "
					f"MIR has {len(instr.args)}, signature has {len(sig.param_type_ids)}"
				)
			for ty_id, arg in zip(sig.param_type_ids, instr.args):
				llty = self._llvm_type_for_typeid(ty_id, allow_void_ok=True)
				emit_llty = self._llty(llty)
				arg_val = self._map_value(arg)
				arg_val = self._coerce_value_to_typeid(arg, arg_val, ty_id, context=f"parameter of {callee_sym}")
				arg_parts.append(f"{emit_llty} {arg_val}")
		else:
			# Legacy fallback: assume all args are Ints.
			arg_parts = [f"{self._llty(DRIFT_INT_TYPE)} {self._map_value(a)}" for a in instr.args]
		args = ", ".join(arg_parts)

		is_exported_entry = bool(
			callee_info.signature is not None and getattr(callee_info.signature, "is_exported_entrypoint", False)
		)
		target_sym, is_cross_module = self._resolve_call_target_symbol(instr.fn_id, callee_info)

		call_can_throw = instr.can_throw

		is_intrinsic = bool(
			callee_info.signature is not None and getattr(callee_info.signature, "is_intrinsic", False)
		)
		if is_exported_entry and is_cross_module and not call_can_throw and not is_intrinsic:
			raise AssertionError(
				"LLVM codegen v1: cross-module exported call lowered as nothrow; "
				"checker must force can-throw at boundary"
			)

		if call_can_throw:
			ok_llty, fnres_llty = self._fnresult_types_for_fn(callee_info)
			if dest is None:
				raise AssertionError("can-throw calls must preserve their FnResult value (MIR bug)")
			if is_exported_entry and is_cross_module:
				ret_tid = callee_info.signature.return_type_id if callee_info.signature else None
				is_void_ret = ret_tid is not None and self._is_void_typeid(ret_tid)
				ok_abi_llty = ok_llty
				if ret_tid is not None:
					ok_abi_llty = self._llvm_ok_abi_type_for_typeid(ret_tid)
				emit_ok_abi_llty = self._llty(ok_abi_llty)
				res_llty = DRIFT_ERROR_PTR if is_void_ret else f"{{ {emit_ok_abi_llty}, {DRIFT_ERROR_PTR} }}"
				res_tmp = self._fresh("res")
				self.lines.append(f"  {res_tmp} = call {res_llty} {_llvm_fn_sym(target_sym)}({args})")
				if is_void_ret:
					err_val = res_tmp
					is_err_i1 = self._fresh("is_err_i1")
					is_err = self._fresh("is_err")
					self.lines.append(f"  {is_err_i1} = icmp ne {DRIFT_ERROR_PTR} {err_val}, null")
					self.lines.append(f"  {is_err} = zext i1 {is_err_i1} to i8")
					ok_zero = self._zero_value_for_ok(ok_llty)
					tmp0 = self._fresh("fn0")
					tmp1 = self._fresh("fn1")
					self.lines.append(f"  {tmp0} = insertvalue {fnres_llty} zeroinitializer, i8 {is_err}, 0")
					self.lines.append(f"  {tmp1} = insertvalue {fnres_llty} {tmp0}, {ok_zero}, 1")
					self.lines.append(f"  {dest} = insertvalue {fnres_llty} {tmp1}, {DRIFT_ERROR_PTR} {err_val}, 2")
				else:
					ok_val = self._fresh("ok")
					err_val = self._fresh("err")
					is_err = self._fresh("is_err")
					ok_zero = self._zero_value_for_ok(ok_llty)
					self.lines.append(f"  {ok_val} = extractvalue {res_llty} {res_tmp}, 0")
					self.lines.append(f"  {err_val} = extractvalue {res_llty} {res_tmp}, 1")
					is_err_i1 = self._fresh("is_err_i1")
					self.lines.append(f"  {is_err_i1} = icmp ne {DRIFT_ERROR_PTR} {err_val}, null")
					self.lines.append(f"  {is_err} = zext i1 {is_err_i1} to i8")
					ok_val_in = ok_val
					if ok_llty != ok_abi_llty:
						if ok_llty == "i1" and ok_abi_llty == "i8":
							ok_val_in = self._fresh("ok_i1")
							self.lines.append(f"  {ok_val_in} = icmp ne i8 {ok_val}, 0")
						else:
							raise AssertionError("LLVM codegen v1: unsupported ok ABI coercion")
					ok_sel = self._fresh("ok_sel")
					emit_ok_llty = self._llty(ok_llty)
					self.lines.append(f"  {ok_sel} = select i1 {is_err_i1}, {ok_zero}, {emit_ok_llty} {ok_val_in}")
					tmp0 = self._fresh("fn0")
					tmp1 = self._fresh("fn1")
					self.lines.append(f"  {tmp0} = insertvalue {fnres_llty} zeroinitializer, i8 {is_err}, 0")
					self.lines.append(f"  {tmp1} = insertvalue {fnres_llty} {tmp0}, {emit_ok_llty} {ok_sel}, 1")
					self.lines.append(f"  {dest} = insertvalue {fnres_llty} {tmp1}, {DRIFT_ERROR_PTR} {err_val}, 2")
				self.value_types[dest] = fnres_llty
			else:
				self.lines.append(f"  {dest} = call {fnres_llty} {_llvm_fn_sym(target_sym)}({args})")
				self.value_types[dest] = fnres_llty
		else:
			ret_tid = None
			if callee_info.signature and callee_info.signature.return_type_id is not None:
				ret_tid = callee_info.signature.return_type_id
			is_void_ret = ret_tid is not None and self._is_void_typeid(ret_tid)
			ret_ty = "void" if is_void_ret else DRIFT_INT_TYPE
			if ret_tid is not None and self.type_table is not None and not is_void_ret:
				ret_ty = self._llvm_type_for_typeid(ret_tid)
			emit_ret_ty = self._llty(ret_ty)
			if dest is None:
				self.lines.append(f"  call {emit_ret_ty} {_llvm_fn_sym(target_sym)}({args})")
			else:
				if ret_ty == "void":
					raise NotImplementedError("LLVM codegen v1: cannot capture result of a void call")
				self.lines.append(f"  {dest} = call {emit_ret_ty} {_llvm_fn_sym(target_sym)}({args})")
				self.value_types[dest] = ret_ty

	def _lower_call_indirect(self, instr: CallIndirect) -> None:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: indirect calls require a TypeTable")
		if instr.can_throw and instr.dest is None:
			raise NotImplementedError("LLVM codegen v1: can-throw indirect call requires a destination")
		if not instr.can_throw and instr.dest is None and not self.type_table.is_void(instr.user_ret_type):
			raise NotImplementedError("LLVM codegen v1: indirect call missing destination for non-void return")
		if len(instr.param_types) != len(instr.args):
			raise NotImplementedError(
				"LLVM codegen v1: indirect call arg count mismatch "
				f"(have {len(instr.args)}, expected {len(instr.param_types)})"
			)

		ret_tid = instr.user_ret_type
		if instr.can_throw:
			err_tid = self.type_table.ensure_error()
			ret_tid = self.type_table.ensure_fnresult(instr.user_ret_type, err_tid)
		if self.type_table.is_void(ret_tid):
			ret_llty = "void"
		else:
			ret_llty = self._llvm_type_for_typeid(ret_tid)
		emit_ret_llty = self._llty(ret_llty)
		callee_val = self._map_value(instr.callee)
		have_ty = self.value_types.get(callee_val)
		if have_ty == DRIFT_IFACE_TYPE or have_ty == self._llty(DRIFT_IFACE_TYPE):
			raise AssertionError("LLVM codegen v1: interface value in CallIndirect (MIR bug)")
		if have_ty == DRIFT_FAT_FNPTR_TYPE:
			adapter_i8 = self._fresh("adapter_i8")
			env_val = self._fresh("env")
			self.lines.append(f"  {adapter_i8} = extractvalue {DRIFT_FAT_FNPTR_TYPE} {callee_val}, 0")
			self.lines.append(f"  {env_val} = extractvalue {DRIFT_FAT_FNPTR_TYPE} {callee_val}, 1")
			adapter_sig = self._fat_adapter_sig_from_call(instr)
			arg_parts: list[str] = [f"ptr {env_val}"]
			for ty_id, arg in zip(instr.param_types, instr.args):
				llty = self._llvm_type_for_typeid(ty_id)
				arg_val = self._map_value(arg)
				arg_val = self._coerce_value_to_typeid(arg, arg_val, ty_id, context="an indirect call parameter")
				arg_parts.append(f"{self._llty(llty)} {arg_val}")
			args = ", ".join(arg_parts)
			if instr.dest:
				dest = self._map_value(instr.dest)
				self.lines.append(f"  {dest} = call {adapter_sig} {adapter_i8}({args})")
				self.value_types[dest] = ret_llty
			else:
				self.lines.append(f"  call {adapter_sig} {adapter_i8}({args})")
			return
		# If the callee is a known nothrow function pointer being called in a
		# can-throw context, substitute the pre-generated can-throw wrapper thunk.
		# Only substitute when _value_fn_throws confirms the callee is nothrow to
		# avoid rewriting unrelated ptr values that happen to share a mapped name.
		if instr.can_throw and self._value_fn_throws.get(callee_val) is False:
			wrap = self._nothrow_wrap_for.get(callee_val)
			if wrap is not None:
				callee_val = wrap
		fn_sig = self._fn_sig_lltype(instr.param_types, instr.user_ret_type, instr.can_throw)
		arg_parts: list[str] = []
		for ty_id, arg in zip(instr.param_types, instr.args):
			llty = self._llvm_type_for_typeid(ty_id)
			arg_val = self._map_value(arg)
			arg_val = self._coerce_value_to_typeid(arg, arg_val, ty_id, context="an indirect call parameter")
			arg_parts.append(f"{self._llty(llty)} {arg_val}")
		args = ", ".join(arg_parts)
		if instr.dest:
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = call {fn_sig} {callee_val}({args})")
			self.value_types[dest] = ret_llty
		else:
			self.lines.append(f"  call {fn_sig} {callee_val}({args})")

	def _lower_call_iface(self, instr: CallIface) -> None:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: interface calls require a TypeTable")
		iface_val = self._map_value(instr.iface)
		iface_llty = self.value_types.get(iface_val) or self.param_value_types.get(iface_val)
		emit_iface_llty = self._llty(DRIFT_IFACE_TYPE)
		if _is_ptr_type(iface_llty):
			loaded = self._fresh("iface_val")
			self.lines.append(f"  {loaded} = load {emit_iface_llty}, ptr {iface_val}")
			self.value_types[loaded] = DRIFT_IFACE_TYPE
			iface_val = loaded
			iface_llty = DRIFT_IFACE_TYPE
		elif iface_llty is None:
			iface_llty = DRIFT_IFACE_TYPE
			self.value_types[iface_val] = DRIFT_IFACE_TYPE
		elif iface_llty != DRIFT_IFACE_TYPE:
			raise NotImplementedError(
				f"LLVM codegen v1: interface call expects iface value (have {iface_llty})"
			)

		data_val = self._fresh("iface_data")
		vtable_val = self._fresh("iface_vtable")
		inline_flag = self._fresh("iface_inline")
		self.lines.append(f"  {data_val} = extractvalue {emit_iface_llty} {iface_val}, {DRIFT_IFACE_DATA_IDX}")
		self.lines.append(f"  {vtable_val} = extractvalue {emit_iface_llty} {iface_val}, {DRIFT_IFACE_VTABLE_IDX}")
		self.lines.append(f"  {inline_flag} = extractvalue {emit_iface_llty} {iface_val}, {DRIFT_IFACE_INLINE_FLAG_IDX}")
		inline_bit = self._fresh("iface_inline_bit")
		is_inline = self._fresh("iface_is_inline")
		self.lines.append(f"  {inline_bit} = and i8 {inline_flag}, 1")
		self.lines.append(f"  {is_inline} = icmp ne i8 {inline_bit}, 0")
		inline_tmp = self._ensure_iface_tmp_alloca()
		self.lines.append(f"  store {emit_iface_llty} {iface_val}, ptr {inline_tmp}")
		inline_field = self._fresh("iface_inline_field")
		inline_word = self._fresh("iface_inline_word")
		inline_i8 = self._fresh("iface_inline_i8")
		inline_storage = f"[{DRIFT_IFACE_INLINE_WORDS} x {self._llty(DRIFT_USIZE_TYPE)}]"
		self.lines.append(
			f"  {inline_field} = getelementptr inbounds {emit_iface_llty}, ptr {inline_tmp}, i32 0, i32 {DRIFT_IFACE_INLINE_IDX}"
		)
		self.lines.append(
			f"  {inline_word} = getelementptr inbounds {inline_storage}, ptr {inline_field}, i32 0, i32 0"
		)
		data_val_eff = self._fresh("iface_data_eff")
		self.lines.append(f"  {data_val_eff} = select i1 {is_inline}, ptr {inline_word}, ptr {data_val}")
		vtable_ptr = vtable_val
		call_slot = self._fresh("call_slot")
		self.lines.append(
			f"  {call_slot} = getelementptr inbounds ptr, ptr {vtable_ptr}, i32 {int(instr.slot_index)}"
		)
		call_ptr_i8 = self._fresh("call_ptr")
		self.lines.append(f"  {call_ptr_i8} = load ptr, ptr {call_slot}")
		fn_sig = self._callback_thunk_sig_llty(
			list(instr.param_types),
			instr.user_ret_type,
			instr.can_throw,
		)

		arg_parts: list[str] = [f"ptr {data_val_eff}"]
		for ty_id, arg in zip(instr.param_types, instr.args):
			llty = self._llvm_type_for_typeid(ty_id)
			arg_val = self._map_value(arg)
			arg_val = self._coerce_value_to_typeid(arg, arg_val, ty_id, context="an interface method parameter")
			arg_parts.append(f"{self._llty(llty)} {arg_val}")
		args = ", ".join(arg_parts)

		ret_tid = instr.user_ret_type
		if instr.can_throw:
			err_tid = self.type_table.ensure_error()
			ret_tid = self.type_table.ensure_fnresult(ret_tid, err_tid)
		if not instr.can_throw and self.type_table.is_void(ret_tid):
			ret_llty = "void"
		else:
			ret_llty = self._llvm_type_for_typeid(ret_tid)
		if instr.dest:
			dest = self._map_value(instr.dest)
			self.lines.append(f"  {dest} = call {fn_sig} {call_ptr_i8}({args})")
			if ret_llty != "void":
				self.value_types[dest] = ret_llty
		else:
			self.lines.append(f"  call {fn_sig} {call_ptr_i8}({args})")

	def _fn_ptr_lltype(self, param_types: list[TypeId], user_ret_type: TypeId, can_throw: bool) -> str:
		"""Opaque pointer type for a function pointer value."""
		return "ptr"

	def _fn_sig_lltype(self, param_types: list[TypeId], user_ret_type: TypeId, can_throw: bool) -> str:
		"""Bare LLVM function signature (no trailing ``*``) for indirect call sites."""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: function pointer types require a TypeTable")
		ret_tid = user_ret_type
		if can_throw:
			err_tid = self.type_table.ensure_error()
			ret_tid = self.type_table.ensure_fnresult(user_ret_type, err_tid)
		if not can_throw and self.type_table.is_void(ret_tid):
			ret_llty = "void"
		else:
			ret_llty = self._llvm_type_for_typeid(ret_tid)
		emit_ret_llty = self._llty(ret_llty)
		arg_lltys = ", ".join(self._llty(self._llvm_type_for_typeid(t, allow_void_ok=True)) for t in param_types)
		return f"{emit_ret_llty} ({arg_lltys})"

	def _fat_adapter_sig(self, field_ty: TypeId) -> str:
		"""Return the bare function signature for a fat fn-ptr adapter (cache key / call annotation)."""
		td = self.type_table.get(field_ty)
		param_tids = list(td.param_types[:-1])
		user_ret_tid = td.param_types[-1]
		err_tid = self.type_table.ensure_error()
		fnres_tid = self.type_table.ensure_fnresult(user_ret_tid, err_tid)
		fnres_llty = self._llty(self._llvm_type_for_typeid(fnres_tid))
		arg_lltys = ["ptr"]
		for t in param_tids:
			arg_lltys.append(self._llty(self._llvm_type_for_typeid(t, allow_void_ok=True)))
		return f"{fnres_llty} ({', '.join(arg_lltys)})"

	def _fat_adapter_sig_from_call(self, instr: CallIndirect) -> str:
		"""Build the bare adapter function signature from a CallIndirect instruction."""
		err_tid = self.type_table.ensure_error()
		fnres_tid = self.type_table.ensure_fnresult(instr.user_ret_type, err_tid)
		fnres_llty = self._llty(self._llvm_type_for_typeid(fnres_tid))
		arg_lltys = ["ptr"]
		for t in instr.param_types:
			arg_lltys.append(self._llty(self._llvm_type_for_typeid(t, allow_void_ok=True)))
		return f"{fnres_llty} ({', '.join(arg_lltys)})"

	def _callback_thunk_ptr_llty(self, param_types: list[TypeId], user_ret_type: TypeId, can_throw: bool) -> str:
		"""Opaque pointer type for a callback thunk function pointer."""
		return "ptr"

	def _callback_thunk_sig_llty(self, param_types: list[TypeId], user_ret_type: TypeId, can_throw: bool) -> str:
		"""Bare function signature for a callback thunk (for indirect call sites)."""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: callback thunks require a TypeTable")
		ret_tid = user_ret_type
		if can_throw:
			err_tid = self.type_table.ensure_error()
			ret_tid = self.type_table.ensure_fnresult(user_ret_type, err_tid)
		if not can_throw and self.type_table.is_void(ret_tid):
			ret_llty = "void"
		else:
			ret_llty = self._llvm_type_for_typeid(ret_tid)
		emit_ret_llty = self._llty(ret_llty)
		arg_lltys = ["ptr"]
		for ty_id in param_types:
			arg_lltys.append(self._llty(self._llvm_type_for_typeid(ty_id, allow_void_ok=True)))
		return f"{emit_ret_llty} ({', '.join(arg_lltys)})"

	def _emit_callback_thunk(self, thunk_name: str, fn_ref: FunctionRefId, call_sig: CallSig, env_ty: TypeId | None) -> None:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: callback thunks require a TypeTable")
		ret_tid = call_sig.user_ret_type
		if call_sig.can_throw:
			err_tid = self.type_table.ensure_error()
			ret_tid = self.type_table.ensure_fnresult(call_sig.user_ret_type, err_tid)
		if not call_sig.can_throw and self.type_table.is_void(ret_tid):
			ret_llty = "void"
		else:
			ret_llty = self._llvm_type_for_typeid(ret_tid)
		emit_ret_llty = self._llty(ret_llty)
		arg_defs = ["ptr %data"]
		call_args: list[str] = []
		for idx, ty_id in enumerate(call_sig.param_types):
			llty = self._llty(self._llvm_type_for_typeid(ty_id, allow_void_ok=True))
			arg_name = f"%a{idx}"
			arg_defs.append(f"{llty} {arg_name}")
			call_args.append(f"{llty} {arg_name}")
		lines: list[str] = []
		lines.append(f"define internal {emit_ret_llty} @{thunk_name}({', '.join(arg_defs)}) {{")
		lines.append("__bb_entry:")
		if env_ty is not None:
			env_ref_ty = self.type_table.ensure_ref(env_ty)
			env_llty = self._llty(self._llvm_type_for_typeid(env_ref_ty))
			env_ptr = "%data"
			call_args.insert(0, f"{env_llty} {env_ptr}")
		target_sym = function_ref_symbol(fn_ref)
		if ret_llty == "void":
			lines.append(f"  call {emit_ret_llty} {_llvm_fn_sym(target_sym)}({', '.join(call_args)})")
			lines.append("  ret void")
		else:
			lines.append(f"  %res = call {emit_ret_llty} {_llvm_fn_sym(target_sym)}({', '.join(call_args)})")
			lines.append(f"  ret {emit_ret_llty} %res")
		lines.append("}")
		self.module.emit_func("\n".join(lines))

	def _emit_nothrow_to_throwing_thunk(self, arg_sym: str, field_ty: TypeId) -> str:
		"""Emit a module-level thunk wrapping a nothrow fn to match a throwing fn-ptr ABI.

		The thunk calls the nothrow function and wraps its return value in a
		FnResult{is_err=false, ok=<result>, err=null}.  The thunk takes an
		``ptr %env`` first parameter (ignored) so it matches the fat fn-ptr
		adapter calling convention.  Returns the thunk LLVM symbol name
		(without leading @).
		"""
		td = self.type_table.get(field_ty)
		param_tids = list(td.param_types[:-1])
		user_ret_tid = td.param_types[-1]
		nothrow_ret_llty = self._llty(self._llvm_type_for_typeid(user_ret_tid, allow_void_ok=True))
		is_void_ret = self.type_table.is_void(user_ret_tid)
		err_tid = self.type_table.ensure_error()
		fnres_tid = self.type_table.ensure_fnresult(user_ret_tid, err_tid)
		fnres_llty = self._llty(self._llvm_type_for_typeid(fnres_tid))
		cache_key = (arg_sym, fnres_llty)
		cached = self.module.nothrow_thunk_cache.get(cache_key)
		if cached is not None:
			return cached
		thunk_name = f"__fnthunk_{arg_sym.lstrip('@').replace('.', '_')}_{len(self.module.nothrow_thunk_cache)}"
		self.module.nothrow_thunk_cache[cache_key] = thunk_name
		arg_defs: list[str] = ["ptr %env"]
		call_args: list[str] = []
		for idx, p_tid in enumerate(param_tids):
			llty = self._llty(self._llvm_type_for_typeid(p_tid, allow_void_ok=True))
			arg_defs.append(f"{llty} %a{idx}")
			call_args.append(f"{llty} %a{idx}")
		lines: list[str] = []
		lines.append(f"define internal {fnres_llty} @{thunk_name}({', '.join(arg_defs)}) {{")
		lines.append("__bb_entry:")
		if is_void_ret:
			lines.append(f"  call void {arg_sym}({', '.join(call_args)})")
			ok_val = "0"
			ok_llty = "i8"
		else:
			lines.append(f"  %raw = call {nothrow_ret_llty} {arg_sym}({', '.join(call_args)})")
			ok_val = "%raw"
			ok_llty = nothrow_ret_llty
		lines.append(f"  %ok0 = insertvalue {fnres_llty} zeroinitializer, i8 0, 0")
		lines.append(f"  %ok1 = insertvalue {fnres_llty} %ok0, {ok_llty} {ok_val}, 1")
		lines.append(f"  %res = insertvalue {fnres_llty} %ok1, {DRIFT_ERROR_PTR} null, 2")
		lines.append(f"  ret {fnres_llty} %res")
		lines.append("}")
		self.module.emit_func("\n".join(lines))
		return thunk_name

	def _ensure_generic_nothrow_wrap_thunk(self, field_ty: TypeId) -> str:
		"""Emit (or return cached) a generic nothrow-to-throwing adapter thunk.

		The adapter loads a nothrow callee from its ``ptr %env`` parameter,
		calls it indirectly, and wraps the result in FnResult.  Cached per
		signature shape so only one thunk exists per distinct fn-ptr type.
		"""
		td = self.type_table.get(field_ty)
		param_tids = list(td.param_types[:-1])
		user_ret_tid = td.param_types[-1]
		nothrow_ret_llty = self._llty(self._llvm_type_for_typeid(user_ret_tid, allow_void_ok=True))
		is_void_ret = self.type_table.is_void(user_ret_tid)
		err_tid = self.type_table.ensure_error()
		fnres_tid = self.type_table.ensure_fnresult(user_ret_tid, err_tid)
		fnres_llty = self._llty(self._llvm_type_for_typeid(fnres_tid))
		nothrow_fn_sig = self._fn_sig_lltype(param_tids, user_ret_tid, can_throw=False)
		cache_key = self._fat_adapter_sig(field_ty)
		cached = self.module.fat_fnptr_wrap_thunks.get(cache_key)
		if cached is not None:
			return cached
		thunk_name = f"__fnthunk_generic_wrap_{len(self.module.fat_fnptr_wrap_thunks)}"
		self.module.fat_fnptr_wrap_thunks[cache_key] = thunk_name
		arg_defs: list[str] = ["ptr %env"]
		call_args: list[str] = []
		for idx, p_tid in enumerate(param_tids):
			llty = self._llty(self._llvm_type_for_typeid(p_tid, allow_void_ok=True))
			arg_defs.append(f"{llty} %a{idx}")
			call_args.append(f"{llty} %a{idx}")
		lines: list[str] = []
		lines.append(f"define internal {fnres_llty} @{thunk_name}({', '.join(arg_defs)}) {{")
		lines.append("__bb_entry:")
		if is_void_ret:
			lines.append(f"  call {nothrow_fn_sig} %env({', '.join(call_args)})")
			ok_val = "0"
			ok_llty = "i8"
		else:
			lines.append(f"  %raw = call {nothrow_fn_sig} %env({', '.join(call_args)})")
			ok_val = "%raw"
			ok_llty = nothrow_ret_llty
		lines.append(f"  %ok0 = insertvalue {fnres_llty} zeroinitializer, i8 0, 0")
		lines.append(f"  %ok1 = insertvalue {fnres_llty} %ok0, {ok_llty} {ok_val}, 1")
		lines.append(f"  %res = insertvalue {fnres_llty} %ok1, {DRIFT_ERROR_PTR} null, 2")
		lines.append(f"  ret {fnres_llty} %res")
		lines.append("}")
		self.module.emit_func("\n".join(lines))
		return thunk_name

	def _ensure_generic_forward_thunk(self, field_ty: TypeId) -> str:
		"""Emit (or return cached) a generic throwing-to-throwing forward thunk.

		The adapter loads a throwing callee from its ``ptr %env`` parameter
		and forwards the call.  Used when storing an already-throwing fn-ptr
		into a throwing struct field.  Cached per signature shape.
		"""
		td = self.type_table.get(field_ty)
		param_tids = list(td.param_types[:-1])
		user_ret_tid = td.param_types[-1]
		err_tid = self.type_table.ensure_error()
		fnres_tid = self.type_table.ensure_fnresult(user_ret_tid, err_tid)
		fnres_llty = self._llty(self._llvm_type_for_typeid(fnres_tid))
		throwing_fn_sig = self._fn_sig_lltype(param_tids, user_ret_tid, can_throw=True)
		cache_key = self._fat_adapter_sig(field_ty)
		cached = self.module.fat_fnptr_fwd_thunks.get(cache_key)
		if cached is not None:
			return cached
		thunk_name = f"__fnthunk_forward_{len(self.module.fat_fnptr_fwd_thunks)}"
		self.module.fat_fnptr_fwd_thunks[cache_key] = thunk_name
		arg_defs: list[str] = ["ptr %env"]
		call_args: list[str] = []
		for idx, p_tid in enumerate(param_tids):
			llty = self._llty(self._llvm_type_for_typeid(p_tid, allow_void_ok=True))
			arg_defs.append(f"{llty} %a{idx}")
			call_args.append(f"{llty} %a{idx}")
		lines: list[str] = []
		lines.append(f"define internal {fnres_llty} @{thunk_name}({', '.join(arg_defs)}) {{")
		lines.append("__bb_entry:")
		lines.append(f"  %res = call {throwing_fn_sig} %env({', '.join(call_args)})")
		lines.append(f"  ret {fnres_llty} %res")
		lines.append("}")
		self.module.emit_func("\n".join(lines))
		return thunk_name

	def _emit_callback_drop_thunk(self, drop_name: str, env_ty: TypeId) -> None:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: callback drop thunks require a TypeTable")
		lines: list[str] = []
		prev_lines = self.lines
		prev_value_types = self.value_types
		self.lines = lines
		self.value_types = {}
		lines.append(f"define internal void @{drop_name}(ptr %data) {{")
		lines.append("__bb_entry:")
		env_llty = self._llvm_type_for_typeid(env_ty)
		emit_env_llty = self._llty(env_llty)
		env_val = self._fresh("env_val")
		lines.append(f"  {env_val} = load {emit_env_llty}, ptr %data")
		self.value_types[env_val] = env_llty
		# CB-DROP LIVENESS FLAGS (2026-07-10): an env struct may carry
		# trailing Int fields named `__live<slot>` (one per MOVE-kind
		# capture whose drop can invoke a user Destructible::destroy —
		# see hir_to_mir._lower_lambda_callback).  The body stores 0 to
		# the flag when it moves the capture out (alongside the value
		# zero-back), and this thunk must then SKIP that slot's drop:
		# the spec (drift-lang-spec §5.11 + §4) promises destroy runs
		# exactly once and only on a fully-formed value, never on the
		# moved-out zero sentinel (Receiver::destroy dereferences its
		# inner Arc and aborts on it).  Envs with no flag fields take
		# the legacy whole-struct drop, byte-identical to before.
		flag_by_slot: dict[int, int] = {}
		env_inst = self.type_table.get_struct_instance(env_ty)
		if env_inst is not None:
			for j, fname in enumerate(env_inst.field_names):
				if fname.startswith("__live"):
					try:
						flag_by_slot[int(fname[len("__live"):])] = j
					except ValueError:
						continue
		if not flag_by_slot:
			self._emit_drop_value(env_ty, env_val)
		else:
			flag_llty = self._llty(DRIFT_INT_TYPE)
			for idx, field_ty in enumerate(env_inst.field_types):
				if env_inst.field_names[idx].startswith("__live"):
					continue
				if not self._type_needs_drop(field_ty):
					continue
				field_llty = self._llvm_type_for_typeid(field_ty)
				field_val = self._fresh("cb_drop_field")
				lines.append(f"  {field_val} = extractvalue {emit_env_llty} {env_val}, {idx}")
				self.value_types[field_val] = field_llty
				flag_idx = flag_by_slot.get(idx)
				if flag_idx is None:
					self._emit_drop_value(field_ty, field_val)
					continue
				flag_val = self._fresh("cb_live")
				lines.append(f"  {flag_val} = extractvalue {emit_env_llty} {env_val}, {flag_idx}")
				cond = self._fresh("cb_live_ne")
				lines.append(f"  {cond} = icmp ne {flag_llty} {flag_val}, 0")
				live_bb = self._fresh("cb_slot_live")
				cont_bb = self._fresh("cb_slot_cont")
				lines.append(f"  br i1 {cond}, label {live_bb}, label {cont_bb}")
				lines.append(f"{live_bb[1:]}:")
				self._emit_drop_value(field_ty, field_val)
				lines.append(f"  br label {cont_bb}")
				lines.append(f"{cont_bb[1:]}:")
		self.module.needs_array_helpers = True
		lines.append("  call void @drift_cb_env_free(ptr %data)")
		lines.append("  ret void")
		lines.append("}")
		self.lines = prev_lines
		self.value_types = prev_value_types
		self.module.emit_func("\n".join(lines))

	def _ensure_callback_vtable(self, fn_ref: FunctionRefId, call_sig: CallSig, env_ty: TypeId | None) -> str:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: callback vtables require a TypeTable")
		parts = [function_ref_symbol(fn_ref), "throw" if call_sig.can_throw else "nothrow"]
		for ty_id in call_sig.param_types:
			parts.append(self.type_table.type_key_string(ty_id))
		parts.append(self.type_table.type_key_string(call_sig.user_ret_type))
		if env_ty is not None:
			parts.append(self.type_table.type_key_string(env_ty))
		else:
			parts.append("no_env")
		raw_key = "|".join(parts)
		existing = self.module.iface_vtables.get(raw_key)
		if existing is not None:
			return existing
		suffix = f"{hash64(raw_key.encode()):016x}"
		thunk_name = f"__drift_cb_thunk_{suffix}"
		drop_name = f"__drift_cb_drop_{suffix}"
		vtable_name = f"__drift_cb_vtable_{suffix}"
		self.module.iface_thunks[raw_key] = thunk_name
		self._emit_callback_thunk(thunk_name, fn_ref, call_sig, env_ty)
		drop_ptr = "ptr null"
		if env_ty is not None:
			self._emit_callback_drop_thunk(drop_name, env_ty)
			drop_ptr = f"ptr @{drop_name}"
		self.module.consts.append(
			f"@{vtable_name} = private constant {DRIFT_CALLBACK_VTABLE_TYPE} [ {drop_ptr}, ptr @{thunk_name} ]"
		)
		self.module.iface_vtables[raw_key] = vtable_name
		return vtable_name

	def _emit_iface_drop_thunk(self, thunk_name: str, value_ty: TypeId) -> None:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: interface drops require a TypeTable")
		lines: list[str] = []
		prev_lines = self.lines
		prev_value_types = self.value_types
		self.lines = lines
		self.value_types = {}
		lines.append(f"define internal void @{thunk_name}(ptr %data) {{")
		lines.append("__bb_entry:")
		val_llty = self._llvm_type_for_typeid(value_ty)
		emit_val_llty = self._llty(val_llty)
		val = self._fresh("val")
		lines.append(f"  {val} = load {emit_val_llty}, ptr %data")
		self.value_types[val] = val_llty
		self._emit_drop_value(value_ty, val)
		self.module.needs_array_helpers = True
		self.module.needs_iface_helpers = True
		lines.append("  ret void")
		lines.append("}")
		self.lines = prev_lines
		self.value_types = prev_value_types
		self.module.emit_func("\n".join(lines))

	def _emit_iface_method_thunk(
		self,
		thunk_name: str,
		fn_id: FunctionId,
		param_types: list[TypeId],
		user_ret_type: TypeId,
		iface_can_throw: bool,
	) -> None:
		"""Emit the per-interface-method dispatcher thunk that the
		vtable slot points at.

		The thunk has TWO distinct ABI surfaces:

		  * **Outer** -- the thunk's own signature.  Matches the
		    *interface* method's ABI (the vtable dispatcher reads
		    this).  `iface_can_throw` drives it.

		  * **Inner** -- the `call` to the impl symbol `fn_id`.  Must
		    match the *impl* method's actual ABI.  Looked up from
		    `self.fn_infos[fn_id].signature.declared_can_throw`.

		When the two bits agree, the thunk is a one-line pass-through.
		When they disagree (a `nothrow` impl satisfying a can-throw
		interface contract -- a permitted subtype relationship), the
		thunk wraps the impl's raw return into an `Ok(...)` FnResult
		to bridge to the interface's ABI.

		Previously (pre-2026-05-17) only one bit was carried and used
		for both surfaces, producing a mis-typed inner call when the
		bits disagreed.  At runtime the caller read the impl's plain
		return as a `FnResult` struct -- garbage in is_err / err_ptr
		slots -- and dereferenced the garbage err pointer.  SIGSEGV
		on every dispatch through `Arc<Interface>.get().method()` in
		the sgw-stub repro; pinned by
		`test_arc_interface_get_dispatch_segfault.py::V3-V6`.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: interface thunks require a TypeTable")
		if not param_types:
			raise AssertionError("interface method thunk missing self param (checker bug)")
		self_ty = param_types[0]
		self_def = self.type_table.get(self_ty)
		if self_def.kind is not TypeKind.REF:
			raise NotImplementedError("interface method self param must be &Self or &mut Self in v1")

		# Resolve the impl's actual can-throw bit.  Read the
		# EFFECTIVE bit from `FnInfo.declared_can_throw`, NOT the
		# surface bit from `FnInfo.signature.declared_can_throw`:
		# the checker normalizes the effective bit based on body
		# analysis (e.g. an impl whose body provably doesn't throw
		# gets effective-nothrow even when written without
		# `nothrow`).  Body emission at
		# `_FuncBuilder._emit_terminator::Return` (~line 7310) reads
		# the same `fn_info.declared_can_throw` to decide whether to
		# emit `ret %FnResult_<...>` or `ret <bare>`.  Reading the
		# surface bit here would let the thunk's inner call disagree
		# with the actual body emission -- the same shape of mismatch
		# that produced the original sgw-stub SIGSEGV, just on a
		# different axis.
		#
		# Default to `iface_can_throw` if `fn_infos` doesn't have an
		# entry -- matches the legacy single-bit behavior so we never
		# silently emit a mis-typed call on an unknown-shape impl.
		impl_info = self.fn_infos.get(fn_id)
		impl_can_throw = (
			bool(impl_info.declared_can_throw)
			if impl_info is not None
			else iface_can_throw
		)

		# !iface_can_throw && impl_can_throw means the impl is
		# LESS strict than the interface contract -- a checker bug
		# that should have been rejected upstream.  Refuse to emit
		# unsafe IR.
		if not iface_can_throw and impl_can_throw:
			raise AssertionError(
				f"interface thunk emission: impl `{function_symbol(fn_id)}` "
				f"is declared can-throw but its interface method is "
				f"declared nothrow.  An impl MUST be at least as "
				f"strict as the interface contract; reaching codegen "
				f"with this shape is a checker bug.  Refusing to "
				f"emit a thunk that would discard the impl's error "
				f"return."
			)

		err_tid = self.type_table.ensure_error()

		# Outer thunk return type -- matches interface ABI.
		if iface_can_throw:
			outer_ret_tid = self.type_table.ensure_fnresult(user_ret_type, err_tid)
			outer_ret_llty = self._llvm_type_for_typeid(outer_ret_tid)
		elif self.type_table.is_void(user_ret_type):
			outer_ret_llty = "void"
		else:
			outer_ret_llty = self._llvm_type_for_typeid(user_ret_type)
		emit_outer_ret_llty = self._llty(outer_ret_llty)

		# Inner call return type -- matches impl ABI.
		if impl_can_throw:
			inner_ret_tid = self.type_table.ensure_fnresult(user_ret_type, err_tid)
			inner_ret_llty = self._llvm_type_for_typeid(inner_ret_tid)
		elif self.type_table.is_void(user_ret_type):
			inner_ret_llty = "void"
		else:
			inner_ret_llty = self._llvm_type_for_typeid(user_ret_type)
		emit_inner_ret_llty = self._llty(inner_ret_llty)

		arg_defs = ["ptr %data"]
		call_args: list[str] = []
		for idx, ty_id in enumerate(param_types[1:]):
			llty = self._llty(self._llvm_type_for_typeid(ty_id))
			arg_name = f"%a{idx}"
			arg_defs.append(f"{llty} {arg_name}")
			call_args.append(f"{llty} {arg_name}")
		lines: list[str] = []
		lines.append(f"define internal {emit_outer_ret_llty} @{thunk_name}({', '.join(arg_defs)}) {{")
		lines.append("__bb_entry:")
		self_llty = self._llty(self._llvm_type_for_typeid(self_ty))
		self_arg = "%data"
		call_args.insert(0, f"{self_llty} {self_arg}")
		target_sym = function_symbol(fn_id)
		target_call_sym = _llvm_fn_sym(target_sym)
		args_str = ", ".join(call_args)

		if iface_can_throw == impl_can_throw:
			# Matched ABI: pass through unchanged.
			if outer_ret_llty == "void":
				lines.append(f"  call {emit_outer_ret_llty} {target_call_sym}({args_str})")
				lines.append("  ret void")
			else:
				lines.append(f"  %res = call {emit_outer_ret_llty} {target_call_sym}({args_str})")
				lines.append(f"  ret {emit_outer_ret_llty} %res")
		else:
			# iface_can_throw && !impl_can_throw -- adapter case.
			# Call impl with its plain ABI, wrap raw return in
			# Ok(...) FnResult to bridge to the interface's ABI.
			#
			# Inline the insertvalue chain (mirrors
			# `_wrap_ok_fnresult` shape).  Don't call that helper
			# directly here: it appends to `self.lines` (the active
			# function body), but this thunk emits into a LOCAL
			# `lines` buffer that's flushed via
			# `self.module.emit_func` at the end.  Mixing the two
			# scribbles thunk IR into the wrong function.
			if inner_ret_llty == "void":
				# impl returns void; wrap as Ok(Void).
				# zeroinitializer already supplies is_err=0 (i8 0
				# at slot 0) and err_ptr=null (ptr null at slot 2);
				# Void's ok-payload slot is an i8 placeholder that
				# stays zero from the initializer.
				lines.append(f"  call {emit_inner_ret_llty} {target_call_sym}({args_str})")
				lines.append(f"  ret {emit_outer_ret_llty} zeroinitializer")
			else:
				lines.append(f"  %raw = call {emit_inner_ret_llty} {target_call_sym}({args_str})")
				lines.append(f"  %ok0 = insertvalue {emit_outer_ret_llty} zeroinitializer, i8 0, 0")
				lines.append(f"  %ok1 = insertvalue {emit_outer_ret_llty} %ok0, {emit_inner_ret_llty} %raw, 1")
				lines.append(f"  %ok2 = insertvalue {emit_outer_ret_llty} %ok1, ptr null, 2")
				lines.append(f"  ret {emit_outer_ret_llty} %ok2")
		lines.append("}")
		self.module.emit_func("\n".join(lines))

	def _ensure_interface_vtable(self, iface_ty: TypeId, value_ty: TypeId) -> tuple[str, int]:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: interface vtables require a TypeTable")
		iface_inst = self.type_table.get_interface_instance(iface_ty)
		iface_base = iface_inst.base_id if iface_inst is not None else iface_ty
		schema = self.type_table.interface_bases.get(iface_base)
		if schema is None:
			raise NotImplementedError("interface schema missing for vtable emission")
		key = f"iface|{self.type_table.type_key_string(iface_ty)}|{self.type_table.type_key_string(value_ty)}"
		existing = self.module.iface_vtables.get(key)
		if existing is not None:
			return existing, self.module.iface_vtable_sizes.get(existing, 0)
		suffix = f"{hash64(key.encode()):016x}"
		vtable_name = f"__drift_iface_vtable_{suffix}"
		drop_name = f"__drift_iface_drop_{suffix}"
		self._emit_iface_drop_thunk(drop_name, value_ty)
		drop_ptr = f"ptr @{drop_name}"
		method_map = self.module.iface_impls.get(_iface_impl_index_key(self.type_table, iface_ty, value_ty))
		if method_map is None:
			raise NotImplementedError("interface impl not found for interface value")
		linear = self.type_table.interface_linearization(iface_base)
		inst_map: dict[TypeId, TypeId] = {}
		try:
			inst_map = self.type_table.interface_instance_view_map(iface_ty)
		except Exception:
			inst_map = {}
		slots: list[str] = []
		for owner_id in linear:
			owner_schema = self.type_table.interface_bases.get(owner_id)
			for m in list(getattr(owner_schema, "methods", []) or []):
				if m.name not in method_map:
					raise NotImplementedError(
						f"interface method '{m.name}' missing in impl for {self.type_table.type_key_string(value_ty)}"
					)
			# Segment: drop slot + method slots
			slots.append(drop_ptr)
			for m in list(getattr(owner_schema, "methods", []) or []):
				thunk_key = f"iface|{suffix}|{owner_schema.name}|{m.name}"
				thunk_name = self.module.iface_thunks.get(thunk_key)
				if thunk_name is None:
					thunk_name = f"__drift_iface_thunk_{hash64(thunk_key.encode()):016x}"
					fn_id = method_map[m.name]
					owner_inst_id = inst_map.get(owner_id)
					owner_inst = self.type_table.get_interface_instance(owner_inst_id) if owner_inst_id is not None else None
					owner_args = list(owner_inst.type_args) if owner_inst is not None else []
					param_types: list[TypeId] = []
					for p in m.params:
						if p.name == "self":
							ref_name = p.type_expr.name if isinstance(p.type_expr, GenericTypeExpr) else ""
							if ref_name == "&mut":
								param_types.append(self.type_table.ensure_ref_mut(value_ty))
							else:
								param_types.append(self.type_table.ensure_ref(value_ty))
							continue
						param_types.append(
							self.type_table._eval_generic_type_expr(p.type_expr, owner_args, module_id=owner_schema.module_id)
						)
					user_ret_type = self.type_table._eval_generic_type_expr(
						m.return_type, owner_args, module_id=owner_schema.module_id
					)
					self._emit_iface_method_thunk(
						thunk_name,
						fn_id,
						param_types=param_types,
						user_ret_type=user_ret_type,
						iface_can_throw=not bool(m.declared_nothrow),
					)
					self.module.iface_thunks[thunk_key] = thunk_name
				owner_inst_id = inst_map.get(owner_id)
				owner_inst = self.type_table.get_interface_instance(owner_inst_id) if owner_inst_id is not None else None
				owner_args = list(owner_inst.type_args) if owner_inst is not None else []
				arg_types = [
					self.type_table._eval_generic_type_expr(p.type_expr, owner_args, module_id=owner_schema.module_id)
					for p in m.params
					if p.name != "self"
				]
				user_ret_type = self.type_table._eval_generic_type_expr(
					m.return_type, owner_args, module_id=owner_schema.module_id
				)
				slots.append(f"ptr @{thunk_name}")
		slot_count = len(slots)
		vtable_llty = f"[{slot_count} x ptr]"
		self.module.consts.append(
			f"@{vtable_name} = private constant {vtable_llty} [ {', '.join(slots)} ]"
		)
		self.module.iface_vtables[key] = vtable_name
		self.module.iface_vtable_sizes[vtable_name] = slot_count
		return vtable_name, slot_count

	# -------------------------------------------------------------------
	# emit_interface_view — canonical "concrete T as interface I" primitive
	# -------------------------------------------------------------------
	#
	# Drift's canonical interface-view ABI is the pair `{data_ptr,
	# vtable_ptr}`.  ANY pointer-like holder that wants to expose a
	# concrete T as interface I should build its view through this
	# primitive, not invent its own vtable-lookup path:
	#
	#   * `Arc<T>.as_interface<I>()`   — attaches the Arc's `ctrl_ptr`
	#                                    to the view pair (Phase 1).
	#   * `&T` → `&I` borrow coercion  — uses the pair unchanged.
	#   * `Box<T>` → `Box<I>`          — uses the pair, box carries
	#                                    ownership separately.
	#
	# Input:
	#   data_ptr_llvm  — an LLVM value of type `ptr` that already
	#                    points at the concrete T instance
	#                    (Arc's `ctrl + sizeof(header)`, a struct
	#                    field address, etc.)
	#   iface_ty       — TypeId of the destination interface I
	#   value_ty       — TypeId of the concrete source T
	# Returns:
	#   (data_ptr_llvm, vtable_sym_llvm) — two LLVM values ready to
	#                                      be plugged into any fat
	#                                      `{data, vtable}` slot.
	#
	# The vtable lookup is memoized in `_ensure_interface_vtable`
	# (which also emits the T-as-I drop thunk if first use), so
	# multiple callers sharing the same (T, I) pair share one
	# vtable symbol.
	#
	# Phase 1 note: `as_interface<I>` is declared as `@intrinsic`
	# and its MIR/LLVM lowering is landed in Stage 3.  This helper
	# is in place now so Stage 3 has one call site to add.  Do NOT
	# add Arc-specific logic here; per-holder concerns
	# (refcount bump, borrow-lifetime tying, ownership transfer)
	# belong at the caller.
	def _emit_interface_view_fields(
		self,
		data_ptr_llvm: str,
		iface_ty: TypeId,
		value_ty: TypeId,
	) -> tuple[str, str]:
		vtable_name, _slot_count = self._ensure_interface_vtable(iface_ty, value_ty)
		return data_ptr_llvm, f"@{vtable_name}"

	def _lower_construct_iface(self, instr: ConstructIface) -> None:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: ConstructIface requires a TypeTable")
		vtable_name = self._ensure_callback_vtable(instr.fn_ref, instr.call_sig, instr.env_ty)
		dest = self._map_value(instr.dest)
		if instr.data is not None:
			if instr.data_ty is None:
				raise AssertionError("LLVM codegen v1: ConstructIface data missing type (compiler bug)")
			data_val = self._map_value(instr.data)
			data_llty = self._llty(self._llvm_type_for_typeid(instr.data_ty))
			data_i8 = data_val
		else:
			data_i8 = "null"
		vtable_i8 = f"@{vtable_name}"
		tmp_ptr = self._ensure_iface_tmp_alloca()
		self.lines.append(f"  store {DRIFT_IFACE_TYPE} zeroinitializer, ptr {tmp_ptr}")
		data_ptr = self._fresh("iface_data_ptr")
		self.lines.append(
			f"  {data_ptr} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_DATA_IDX}"
		)
		self.lines.append(f"  store ptr {data_i8}, ptr {data_ptr}")
		vtable_ptr = self._fresh("iface_vtable_ptr")
		self.lines.append(
			f"  {vtable_ptr} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_VTABLE_IDX}"
		)
		self.lines.append(f"  store ptr {vtable_i8}, ptr {vtable_ptr}")
		flag_ptr = self._fresh("iface_flag_ptr")
		self.lines.append(
			f"  {flag_ptr} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_INLINE_FLAG_IDX}"
		)
		self.lines.append(f"  store i8 0, ptr {flag_ptr}")
		self.lines.append(f"  {dest} = load {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}")
		self.value_types[dest] = DRIFT_IFACE_TYPE

	def _lower_construct_iface_borrowed(self, instr: ConstructIfaceBorrowed) -> None:
		"""Non-owning interface view (0.33.77): fat value whose data slot
		points at CALLER-owned storage. Flag byte = BORROWED (4): bit0
		clear, so existing dispatch code selects the data slot unchanged;
		the drop helper's borrowed early-out skips both the payload drop
		thunk and the free. Never crosses out of its constructing frame as
		an owned value (see the MIR node's docstring), which is what keeps
		this ABI-neutral: only drop helpers emitted by THIS compiler ever
		see the bit."""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: interface views require a TypeTable")
		vtable_name, _slot_count = self._ensure_interface_vtable(instr.iface_ty, instr.value_ty)
		dest = self._map_value(instr.dest)
		data_val = self._map_value(instr.data_ref)
		tmp_ptr = self._ensure_iface_tmp_alloca()
		self.lines.append(f"  store {DRIFT_IFACE_TYPE} zeroinitializer, ptr {tmp_ptr}")
		vtable_ptr = self._fresh("iface_vtable_ptr")
		self.lines.append(
			f"  {vtable_ptr} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_VTABLE_IDX}"
		)
		self.lines.append(f"  store ptr @{vtable_name}, ptr {vtable_ptr}")
		flag_ptr = self._fresh("iface_flag_ptr")
		self.lines.append(
			f"  {flag_ptr} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_INLINE_FLAG_IDX}"
		)
		self.lines.append(f"  store i8 {DRIFT_IFACE_FLAG_BORROWED}, ptr {flag_ptr}")
		data_slot = self._fresh("iface_data_ptr")
		self.lines.append(
			f"  {data_slot} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_DATA_IDX}"
		)
		self.lines.append(f"  store ptr {data_val}, ptr {data_slot}")
		self.lines.append(f"  {dest} = load {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}")
		self.value_types[dest] = DRIFT_IFACE_TYPE

	def _lower_construct_iface_value(self, instr: ConstructIfaceValue) -> None:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: ConstructIfaceValue requires a TypeTable")
		vtable_name, slot_count = self._ensure_interface_vtable(instr.iface_ty, instr.value_ty)
		dest = self._map_value(instr.dest)
		value_val = self._map_value(instr.value)
		value_llty = self._llvm_type_for_typeid(instr.value_ty)
		emit_value_llty = self._llty(value_llty)
		size, align = self._size_align_typeid(instr.value_ty)
		inline_bytes = (self.module.word_bits // 8) * DRIFT_IFACE_INLINE_WORDS
		inline_ok = size <= inline_bytes and align <= (self.module.word_bits // 8)
		tmp_ptr = self._ensure_iface_tmp_alloca()
		self.lines.append(f"  store {DRIFT_IFACE_TYPE} zeroinitializer, ptr {tmp_ptr}")
		vtable_i8 = f"@{vtable_name}"
		vtable_llty = f"[{slot_count} x ptr]"
		vtable_ptr = self._fresh("iface_vtable_ptr")
		self.lines.append(
			f"  {vtable_ptr} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_VTABLE_IDX}"
		)
		self.lines.append(f"  store ptr {vtable_i8}, ptr {vtable_ptr}")
		flag_ptr = self._fresh("iface_flag_ptr")
		self.lines.append(
			f"  {flag_ptr} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_INLINE_FLAG_IDX}"
		)
		if inline_ok:
			self.lines.append(f"  store i8 1, ptr {flag_ptr}")
			inline_field = self._fresh("iface_inline_field")
			inline_word = self._fresh("iface_inline_word")
			inline_storage = f"[{DRIFT_IFACE_INLINE_WORDS} x {self._llty(DRIFT_USIZE_TYPE)}]"
			self.lines.append(
				f"  {inline_field} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_INLINE_IDX}"
			)
			self.lines.append(
				f"  {inline_word} = getelementptr inbounds {inline_storage}, ptr {inline_field}, i32 0, i32 0"
			)
			if size > 0:
				inline_val_ptr = self._fresh("iface_inline_val_ptr")
				inline_val_ptr = inline_word
				self.lines.append(f"  store {emit_value_llty} {value_val}, ptr {inline_val_ptr}")
		else:
			self.lines.append(f"  store i8 2, ptr {flag_ptr}")
			self.module.needs_iface_helpers = True
			tmp_alloc = self._fresh("iface_alloc")
			self.lines.append(
				f"  {tmp_alloc} = call ptr @drift_iface_alloc({self._llty(DRIFT_USIZE_TYPE)} {size}, {self._llty(DRIFT_USIZE_TYPE)} {align})"
			)
			data_ptr = tmp_alloc
			self.lines.append(f"  store {emit_value_llty} {value_val}, ptr {data_ptr}")
			data_slot = self._fresh("iface_data_ptr")
			self.lines.append(
				f"  {data_slot} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_DATA_IDX}"
			)
			self.lines.append(f"  store ptr {tmp_alloc}, ptr {data_slot}")
		self.lines.append(f"  {dest} = load {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}")
		self.value_types[dest] = DRIFT_IFACE_TYPE

	def _lower_iface_upcast(self, instr: IfaceUpcast) -> None:
		iface_val = self._map_value(instr.iface)
		iface_llty = self.value_types.get(iface_val) or self.param_value_types.get(iface_val)
		emit_iface_llty = self._llty(DRIFT_IFACE_TYPE)
		if _is_ptr_type(iface_llty):
			loaded = self._fresh("iface_val")
			self.lines.append(f"  {loaded} = load {emit_iface_llty}, ptr {iface_val}")
			self.value_types[loaded] = DRIFT_IFACE_TYPE
			iface_val = loaded
			iface_llty = DRIFT_IFACE_TYPE
		elif iface_llty is None:
			iface_llty = DRIFT_IFACE_TYPE
			self.value_types[iface_val] = DRIFT_IFACE_TYPE
		elif iface_llty != DRIFT_IFACE_TYPE:
			raise NotImplementedError("LLVM codegen v1: iface upcast expects iface value")

		data_val = self._fresh("iface_data")
		vtable_val = self._fresh("iface_vtable")
		self.lines.append(f"  {data_val} = extractvalue {emit_iface_llty} {iface_val}, {DRIFT_IFACE_DATA_IDX}")
		self.lines.append(f"  {vtable_val} = extractvalue {emit_iface_llty} {iface_val}, {DRIFT_IFACE_VTABLE_IDX}")
		offset_ptr = self._fresh("iface_off")
		self.lines.append(
			f"  {offset_ptr} = getelementptr inbounds ptr, ptr {vtable_val}, i32 {int(instr.slot_offset)}"
		)
		offset_i8 = offset_ptr
		dest = self._map_value(instr.dest)
		tmp_ptr = self._ensure_iface_tmp_alloca()
		self.lines.append(f"  store {DRIFT_IFACE_TYPE} {iface_val}, ptr {tmp_ptr}")
		vtable_slot = self._fresh("iface_vtable_ptr")
		self.lines.append(
			f"  {vtable_slot} = getelementptr inbounds {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_VTABLE_IDX}"
		)
		self.lines.append(f"  store ptr {offset_i8}, ptr {vtable_slot}")
		self.lines.append(f"  {dest} = load {DRIFT_IFACE_TYPE}, ptr {tmp_ptr}")
		self.value_types[dest] = DRIFT_IFACE_TYPE

	def _lower_arc_as_interface(self, instr: ArcAsInterface) -> None:
		"""Stage 3 fat `Arc<I>` construction from thin `&Arc<T=concrete>`.

		Emits the five-step sequence documented on the MIR op:

		1. Extract `ctrl = rawbuffer_ptr(&src.buf)` — the base of
		   the `ArcBox<T>` allocation.  Layout invariant: `buf` is
		   field 0 of the thin `Arc<T>` struct, and `RawBuffer<U>`
		   has `ptr` at `RAWBUF_PTR_IDX = 0`.
		2. Call `_arc_fat_bump_strong_via_ctrl(ctrl)` — the Slice 1
		   non-generic runtime helper that does an atomic fetch-add
		   on `ArcHeader.strong` at offset 0 of the allocation.
		3. Compute `data = getelementptr ArcBox<T>, ptr ctrl, i32 0,
		   i32 1` — concrete `value` payload address.  The T-dependent
		   alignment padding is handled by LLVM's struct-layout GEP,
		   not by us manually computing byte offsets.
		4. Resolve `vtable` via `_ensure_interface_vtable(iface_ty,
		   concrete_ty)` — reuses the existing T-as-I vtable machinery;
		   no Arc-specific vtable namespace.
		5. `insertvalue` chain into the fat `Arc<I>` result:
		   `{ctrl, data, vtable}`.

		Invariants encoded in the IR: exactly one strong bump, exactly
		one allocation, `data` points inside the concrete ArcBox<T>
		(same allocation as `ctrl`), `drop_thunk` is already captured
		at `arc<T=concrete>(value)` time and carried in the ArcHeader
		for last-drop.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: ArcAsInterface requires a TypeTable")

		# Resolve the concrete-T `ArcBox<T>` TypeId via the thin
		# Arc<T> struct's field 0, which is `RawBuffer<ArcBox<T>>`.
		src_arc_inst = self.type_table.get_struct_instance(instr.src_arc_ty)
		if src_arc_inst is None or not src_arc_inst.field_types:
			raise AssertionError(
				"ArcAsInterface: thin Arc<T> source has no struct instance "
				"with fields — compiler bug or wrong src_arc_ty"
			)
		raw_buf_ty = src_arc_inst.field_types[0]
		raw_buf_inst = self.type_table.get_struct_instance(raw_buf_ty)
		if raw_buf_inst is None or not raw_buf_inst.type_args:
			raise AssertionError(
				"ArcAsInterface: thin Arc<T>.buf is not an instantiated "
				"RawBuffer<ArcBox<T>> (compiler bug or layout changed)"
			)
		arc_box_ty = raw_buf_inst.type_args[0]

		src_arc_llty = self._llvm_type_for_typeid(instr.src_arc_ty)
		raw_buf_llty = self._llvm_type_for_typeid(raw_buf_ty)
		arc_box_llty = self._llvm_type_for_typeid(arc_box_ty)
		result_llty = self._llvm_type_for_typeid(instr.result_ty)

		src = self._map_value(instr.src_arc_ref)

		# Step 1: extract ctrl = rawbuffer_ptr from Arc<T>.buf.
		buf_addr = self._fresh("arc_buf_addr")
		self.lines.append(
			f"  {buf_addr} = getelementptr inbounds {src_arc_llty}, ptr {src}, i32 0, i32 0"
		)
		self.value_types[buf_addr] = "ptr"
		buf_val = self._fresh("arc_buf")
		self.lines.append(f"  {buf_val} = load {raw_buf_llty}, ptr {buf_addr}")
		self.value_types[buf_val] = raw_buf_llty
		ctrl = self._fresh("arc_ctrl")
		self.lines.append(
			f"  {ctrl} = extractvalue {raw_buf_llty} {buf_val}, {RAWBUF_PTR_IDX}"
		)
		self.value_types[ctrl] = "ptr"

		# Step 2: atomic strong-bump via the non-generic stdlib helper.
		# The helper is a Drift symbol (module-qualified, quoted) — use
		# the standard `_llvm_fn_sym` spelling rather than a raw
		# literal so the same escaping rules as every other Drift
		# symbol apply.  Flag the module as needing the helper: the
		# helper's definition lives in `stdlib/std/core/arc.drift`
		# (relocated from `std/concurrent` at ABI 11), and the
		# module-render pass in `_emit_module` decides whether to
		# emit a `declare` (no in-module `define`) or skip the
		# declare (define is in this module — LLVM rejects both
		# together).  Package-consumer builds reach the `define`
		# branch via the reachability seed at
		# `driftc.py::compile_to_llvm_ir` that adds the helper to the
		# reachable set whenever ArcAsInterface is in scope.
		#
		# Attach !dbg when the enclosing function has debug info —
		# the LLVM verifier rejects "inlinable function call in a
		# function with debug info" without a !dbg location. Mirrors
		# the same suffix construction used in `_emit_drop_value` and
		# elsewhere. Without this the test path (which defaults to
		# `debug_enabled=True`) fails at clang verifier time with:
		#   inlinable function call in a function with debug info
		#   must have a !dbg location
		#     call void @"std.core.arc::_arc_fat_bump_strong_via_ctrl"(...)
		self.module.needs_arc_fat_bump_helper = True
		bump_sym = _llvm_fn_sym("std.core.arc::_arc_fat_bump_strong_via_ctrl")
		bump_dbg_suffix = ""
		if self.module.debug_enabled and self._dbg_subprogram_id is not None:
			loc_id = self._dbg_location_for_span(self._dbg_last_span or self._dbg_default_span)
			if loc_id is not None:
				bump_dbg_suffix = f", !dbg !{loc_id}"
		self.lines.append(f"  call void {bump_sym}(ptr {ctrl}){bump_dbg_suffix}")

		# Step 3: data = GEP ArcBox<T>, ptr ctrl, i32 0, i32 1.
		data = self._fresh("arc_data")
		self.lines.append(
			f"  {data} = getelementptr inbounds {arc_box_llty}, ptr {ctrl}, i32 0, i32 1"
		)
		self.value_types[data] = "ptr"

		# Step 4: data + T-as-I vtable via the canonical interface-view
		# primitive.  `_emit_interface_view_fields` is the shared
		# "data ptr + existing T-as-I vtable symbol" emitter reused by
		# every fat `{data, vtable}` slot — `&T→&I` borrow coercion,
		# `Box<T>→Box<I>`, and now `arc_as<I>`.  Routing through it
		# enforces the "no Arc-specific vtable path" rule at the
		# type/code level rather than relying on convention.
		data_out, vtable_sym = self._emit_interface_view_fields(
			data_ptr_llvm=data,
			iface_ty=instr.iface_ty,
			value_ty=instr.concrete_ty,
		)

		# Step 5: insertvalue chain into {ctrl, data, vtable}.
		dest = self._map_value(instr.dest)
		tmp0 = self._fresh("fat_arc_tmp")
		self.lines.append(
			f"  {tmp0} = insertvalue {result_llty} zeroinitializer, ptr {ctrl}, 0"
		)
		tmp1 = self._fresh("fat_arc_tmp")
		self.lines.append(
			f"  {tmp1} = insertvalue {result_llty} {tmp0}, ptr {data_out}, 1"
		)
		self.lines.append(
			f"  {dest} = insertvalue {result_llty} {tmp1}, ptr {vtable_sym}, 2"
		)
		self.value_types[dest] = result_llty

	def _lower_arc_fat_get(self, instr: ArcFatGet) -> None:
		"""Stage 3 fat `Arc<I>.get()` — borrowed `&I` from already-resolved
		`{data, vtable}`.  No refcount touch, no new vtable lookup.

		Emits:
		- GEP fat `Arc<I>` field 1 → `data_addr`, load → `data_ptr`.
		- GEP fat `Arc<I>` field 2 → `vtable_addr`, load → `vtable_ptr`.
		- alloca a fresh `DRIFT_IFACE_TYPE` slot (fresh per call so
		  successive `.get()`s don't alias).
		- Store zeroinitializer + the `{data, vtable}` pair + flag=0.
		- `dest = alloca_ptr` — the `&I` representation.

		The returned `ptr` is a `REF` at the Drift type level; its
		lifetime is tied to the receiver's borrow by typecheck.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: ArcFatGet requires a TypeTable")

		src_arc_llty = self._llvm_type_for_typeid(instr.src_arc_ty)
		src = self._map_value(instr.src_arc_ref)
		emit_iface_llty = self._llty(DRIFT_IFACE_TYPE)

		# Load data (field 1) and vtable (field 2) from the fat Arc<I>.
		data_addr = self._fresh("fat_data_addr")
		self.lines.append(
			f"  {data_addr} = getelementptr inbounds {src_arc_llty}, ptr {src}, i32 0, i32 1"
		)
		data_val = self._fresh("fat_data_val")
		self.lines.append(f"  {data_val} = load ptr, ptr {data_addr}")
		self.value_types[data_val] = "ptr"

		vtbl_addr = self._fresh("fat_vtbl_addr")
		self.lines.append(
			f"  {vtbl_addr} = getelementptr inbounds {src_arc_llty}, ptr {src}, i32 0, i32 2"
		)
		vtbl_val = self._fresh("fat_vtbl_val")
		self.lines.append(f"  {vtbl_val} = load ptr, ptr {vtbl_addr}")
		self.value_types[vtbl_val] = "ptr"

		# Fresh entry-block alloca for the borrowed-iface slot (not
		# the shared `_ensure_iface_tmp_alloca` — that one is a
		# one-shot-then-load pattern; we need the ptr to stay live
		# until the caller deref's it, and successive `.get()`
		# results may coexist).  Inserting at entry keeps stack
		# usage flat even when `.get()` appears inside a loop.
		tmp_ptr = self._fresh_iface_alloca()
		self.lines.append(f"  store {emit_iface_llty} zeroinitializer, ptr {tmp_ptr}")

		data_slot = self._fresh("fat_get_data_slot")
		self.lines.append(
			f"  {data_slot} = getelementptr inbounds {emit_iface_llty}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_DATA_IDX}"
		)
		self.lines.append(f"  store ptr {data_val}, ptr {data_slot}")
		vtbl_slot = self._fresh("fat_get_vtbl_slot")
		self.lines.append(
			f"  {vtbl_slot} = getelementptr inbounds {emit_iface_llty}, ptr {tmp_ptr}, i32 0, i32 {DRIFT_IFACE_VTABLE_IDX}"
		)
		self.lines.append(f"  store ptr {vtbl_val}, ptr {vtbl_slot}")
		# inline storage stays zero from zeroinitializer; inline_flag
		# is also already zero, signaling "out-of-line data pointer"
		# which is the correct classification for Arc<I>.get()'s
		# heap-backed data.

		# `dest` IS the alloca pointer — the borrowed `&I`.  No
		# bitcast emitted: with opaque pointers, `ptr` → `ptr` is a
		# no-op and the opaque-pointer audit flags the instruction.
		# Bind `dest`'s MIR id directly to the alloca's LLVM name
		# via `value_map` so later `_map_value(instr.dest)` calls
		# resolve straight to `%iface_alloca<N>`.
		self.value_map[instr.dest] = tmp_ptr
		self.value_types[tmp_ptr] = "ptr"

	def _ensure_nothrow_wrap_thunk(self, sym: str, call_sig) -> str:
		"""Generate a can-throw wrapper thunk for a nothrow function pointer."""
		wrap_name = f"@__nothrow_wrap_{sym.strip('@').replace('::', '_')}"
		if wrap_name in self._nothrow_wrap_thunks:
			return wrap_name
		param_lltys = [self._llvm_type_for_typeid(t) for t in call_sig.param_types]
		ret_tid = call_sig.user_ret_type
		is_void = self.type_table.is_void(ret_tid)
		if is_void:
			ok_llty = "i8"
			ok_key = "Void"
		else:
			ok_llty = self._llvm_type_for_typeid(ret_tid)
			ok_key = self.type_table.type_key_string(ret_tid)
		fnres_llty = self.module._declare_fnresult_named_type(ok_key, ok_llty, ok_typeid=ret_tid if not is_void else None)
		param_strs = ", ".join(f"{self._llty(t)} %a{i}" for i, t in enumerate(param_lltys))
		arg_strs = ", ".join(f"{self._llty(t)} %a{i}" for i, t in enumerate(param_lltys))
		lines = [f"define {fnres_llty} {wrap_name}({param_strs}) {{"]
		lines.append("__bb_entry:")
		if is_void:
			lines.append(f"  call void {_llvm_fn_sym(sym)}({arg_strs})")
			ok_val = "0"
		else:
			emit_ret = self._llty(ok_llty)
			lines.append(f"  %raw = call {emit_ret} {_llvm_fn_sym(sym)}({arg_strs})")
			ok_val = "%raw"
		emit_ok = self._llty(ok_llty)
		lines.append(f"  %ok0 = insertvalue {fnres_llty} zeroinitializer, i8 0, 0")
		lines.append(f"  %ok1 = insertvalue {fnres_llty} %ok0, {emit_ok} {ok_val}, 1")
		lines.append(f"  %res = insertvalue {fnres_llty} %ok1, {DRIFT_ERROR_PTR} null, 2")
		lines.append(f"  ret {fnres_llty} %res")
		lines.append("}")
		self.module.funcs.append("\n".join(lines))
		self._nothrow_wrap_thunks[wrap_name] = True
		return wrap_name

	def _lower_fnptr_const(self, instr: FnPtrConst) -> None:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: function pointer constants require a TypeTable")
		dest = self._map_value(instr.dest)
		sym = function_ref_symbol(instr.fn_ref)
		sym_val = _llvm_fn_sym(sym)
		self.value_types[sym_val] = "ptr"
		self._value_fn_throws[sym_val] = instr.call_sig.can_throw
		expected_ty = self.func.local_types.get(instr.dest)
		if expected_ty is not None and self._is_throwing_fn_typeid(expected_ty):
			fn_ty = expected_ty
		elif instr.call_sig.can_throw:
			fn_ty = self._fn_typeid_from_call_sig(instr.call_sig, can_throw=True)
		else:
			fn_ty = None
		if fn_ty is not None:
			fat = self._coerce_throwing_fn_to_fat(
				instr.dest,
				sym_val,
				fn_ty,
				context="throwing Fn pointer constant",
			)
			self.value_map[instr.dest] = fat
			self.value_types[dest] = DRIFT_FAT_FNPTR_TYPE
			self._value_fn_throws[fat] = True
			self._value_fn_throws[dest] = True
			return
		# Nothrow function values stay as raw function pointers.
		self.value_map[instr.dest] = sym_val
		self.value_types[dest] = "ptr"
		self._value_fn_throws[dest] = False
		# For nothrow functions, pre-generate a can-throw wrapper thunk so the
		# pointer can be safely passed to generic can-throw call sites.
		wrap = self._ensure_nothrow_wrap_thunk(sym, instr.call_sig)
		self._nothrow_wrap_for[sym_val] = wrap
		self._nothrow_wrap_for[dest] = wrap

	def _resolve_call_target_symbol(self, fn_id: FunctionId, callee_info: FnInfo) -> tuple[str, bool]:
		"""
		Resolve the LLVM-level call target symbol for a MIR `Call`.

		Contract:
		- Same-module calls to exported entrypoints may target the private `__impl`
		  body (internal calling convention).
		- Cross-module calls to exported entrypoints must target the public wrapper
		  symbol (Result boundary ABI), never `__impl`.
		- Non-exported callees target their symbol name directly.
		"""
		is_exported_entry = bool(
			callee_info.signature is not None and getattr(callee_info.signature, "is_exported_entrypoint", False)
		)
		caller_mod = (
			getattr(self.fn_info.signature, "module", None) if self.fn_info.signature is not None else None
		) or self.fn_info.fn_id.module
		callee_mod = (
			getattr(callee_info.signature, "module", None) if callee_info.signature is not None else None
		) or callee_info.fn_id.module
		is_cross_module = False
		if is_exported_entry:
			module_packages = getattr(self.type_table, "module_packages", None)
			if module_packages is None:
				raise AssertionError("module_packages missing for boundary check (codegen bug)")
			if caller_mod is None or callee_mod is None:
				raise AssertionError("caller/callee module missing for boundary check (codegen bug)")
			caller_pkg = module_packages.get(caller_mod)
			callee_pkg = module_packages.get(callee_mod)
			if caller_pkg is None or callee_pkg is None:
				raise AssertionError("module_packages missing entry for boundary check (codegen bug)")
			if caller_pkg != callee_pkg:
				pass  # Option B: no boundary ABI for cross-package calls.

		target_sym = function_symbol(fn_id)
		if is_exported_entry and not is_cross_module:
			target_sym = self.export_impl_map.get(fn_id, target_sym)

		# Cross-module calls must never target `__impl`. If this trips, it is a
		# compiler bug (bad module-id inference or bad signature metadata).
		if is_exported_entry and is_cross_module and "__impl" in target_sym:
			raise AssertionError(f"cross-module call resolved to __impl symbol {target_sym} (compiler bug)")

		# Apply any final driver-level renames (e.g. argv wrapper) only for
		# same-module calls to avoid retargeting external symbols.
		if caller_mod == callee_mod:
			target_sym = self.rename_map.get(fn_id, target_sym)
		return target_sym, is_cross_module

	def _emit_nothrow_return_value(self, val: str, ty: str | None) -> None:
		if ty == DRIFT_STRING_TYPE:
			self.lines.append(f"  ret {DRIFT_STRING_TYPE} {val}")
			return
		if ty == DRIFT_IFACE_TYPE:
			self.lines.append(f"  ret {DRIFT_IFACE_TYPE} {val}")
			return
		if ty in (DRIFT_INT_TYPE, DRIFT_UINT_TYPE, DRIFT_U64_TYPE, "i1", "i8", "i32"):
			self.lines.append(f"  ret {self._llty(ty)} {val}")
			return
		if ty in ("double", "float"):
			self.lines.append(f"  ret {ty} {val}")
			return
		if _is_ptr_type(ty):
			# Non-throwing functions may return references (`&T`), lowered as
			# typed pointers (`T*`) in v1.
			self.lines.append(f"  ret {ty} {val}")
			return
		if ty is not None and ty.startswith("%Variant_"):
			# Variants are compiler-private aggregates in v1, but they are still
			# valid surface return types (e.g. `Optional<Int>`). We return them by
			# value using their named struct type.
			self.lines.append(f"  ret {ty} {val}")
			return
		if ty is not None and ty.startswith("%Struct_"):
			# User-defined structs are returned by value in v1.
			self.lines.append(f"  ret {ty} {val}")
			return
		if ty == "%DriftArrayHeader":
			# Builtin Array<T> header is a first-class by-value return type in v1.
			self.lines.append(f"  ret %DriftArrayHeader {val}")
			return
		if ty == DRIFT_FAT_FNPTR_TYPE:
			# Throwing `Fn` values are fat {adapter, env} pairs, returned by
			# value like other first-class aggregates.
			self.lines.append(f"  ret {DRIFT_FAT_FNPTR_TYPE} {val}")
			return
		raise NotImplementedError(
			f"LLVM codegen v1: non-can-throw return must be Int, Float, String, Interface, &T, Fn, Array, Struct, or Variant, got {ty}"
		)

	def _lower_term(self, term: object) -> None:
		if isinstance(term, Goto):
			self.lines.append(f"  br label %{self._bb(term.target)}")
			return

		if isinstance(term, IfTerminator):
			cond = self._map_value(term.cond)
			cond_ty = self.value_types.get(cond, "i1")
			if cond_ty != "i1":
				raise NotImplementedError("LLVM codegen v1: branch condition must be bool (i1)")
			self.lines.append(
				f"  br i1 {cond}, label %{self._bb(term.then_target)}, label %{self._bb(term.else_target)}"
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
			sig = self.fn_info.signature
			ret_tid = sig.return_type_id if sig is not None else None
			if ret_tid is not None and self.type_table is not None:
				# Representation coercion at the return boundary: a nothrow fn
				# returning a throwing `Fn` may hold a thin named-fn pointer
				# that must be widened to the fat {adapter, env} pair.  The
				# can-throw path gets the same treatment in ConstructResultOk.
				val = self._coerce_value_to_typeid(
					term.value, val, ret_tid, context=f"return of {self.func.name}"
				)
			ty = self.value_types.get(val)
			if ty is None:
				# Best-effort fallback: some SSA aliases/loads may not carry a type tag
				# even though the function signature is fully typed.
				if ret_tid is not None and self.type_table is not None:
					ty = self._llvm_type_for_typeid(ret_tid)
					self.value_types[val] = ty
			self._emit_nothrow_return_value(val, ty)
			return

		if isinstance(term, Unreachable):
			self.lines.append("  unreachable")
			return

		if isinstance(term, SwitchTerminator):
			# Scalar `match` dispatch: one LLVM `switch` on the scrutinee; LLVM's
			# backend chooses jump-table / bit-test / compare-tree.  Width comes
			# from the scrutinee's value type (i8 Byte, i32 Int32/Uint32, i64
			# Int/Uint/Uint64); exact-equality cases are signedness-agnostic.
			scrut = self._map_value(term.scrutinee)
			# Resolve the Drift value-type tag (e.g. "drift.int") to its LLVM
			# integer type ("i64"/"i32"/"i8"); already-LLVM tags pass through.
			scrut_llty = self._llty(self.value_types.get(scrut, ""))
			if not scrut_llty.startswith("i") or not scrut_llty[1:].isdigit():
				raise NotImplementedError(
					f"LLVM codegen: switch scrutinee must be an integer type, got {scrut_llty!r}"
				)
			case_specs = " ".join(
				f"{scrut_llty} {int(v)}, label %{self._bb(t)}" for (v, t) in term.cases
			)
			self.lines.append(
				f"  switch {scrut_llty} {scrut}, label %{self._bb(term.default_target)} [ {case_specs} ]"
			)
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
			return DRIFT_INT_TYPE
		# Use the same TypeTable-based mapping as parameters so ref returns are
		# handled consistently (`&T` -> `T*`).
		try:
			return self._llvm_type_for_typeid(rt_id, allow_void_ok=False)
		except NotImplementedError:
			# Legacy fallback: treat unknown surface types as Int.
			return DRIFT_INT_TYPE

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
		of the target word size for all supported field types in v1.
		"""
		if ty_id in self._size_align_cache:
			return self._size_align_cache[ty_id]
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for variant lowering")
		td = self.type_table.get(ty_id)
		if td.kind is TypeKind.FORWARD_NOMINAL:
			mod = td.module_id
			name = td.name
			alias_def = self.type_table.lookup_type_alias(module_id=mod, name=name)
			if alias_def is not None:
				alias_params, alias_target, _loc = alias_def
				if not alias_params:
					resolved = resolve_opaque_type(alias_target, self.type_table, module_id=mod, type_params=None, allow_generic_base=True)
					if resolved != ty_id:
						return self._size_align_typeid(resolved)
			nominal = (
				self.type_table.get_nominal(kind=TypeKind.STRUCT, module_id=mod, name=name)
				or self.type_table.get_nominal(kind=TypeKind.VARIANT, module_id=mod, name=name)
				or self.type_table.get_nominal(kind=TypeKind.INTERFACE, module_id=mod, name=name)
			)
			if nominal is None:
				nominal = (
					self.type_table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=name)
					or self.type_table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=name)
					or self.type_table.find_unique_nominal_by_name(kind=TypeKind.INTERFACE, name=name)
				)
			if nominal is not None and nominal != ty_id:
				return self._size_align_typeid(nominal)
		if td.kind is TypeKind.STRUCT and td.name == "MaybeUninit" and td.module_id == "std.mem":
			inst = self.type_table.get_struct_instance(ty_id)
			if inst is not None and inst.type_args:
				out = self._size_align_typeid(inst.type_args[0])
				self._size_align_cache[ty_id] = out
				return out
			if td.param_types:
				out = self._size_align_typeid(td.param_types[0])
				self._size_align_cache[ty_id] = out
				return out
		word_bytes = self.module.word_bits // 8
		if td.kind is TypeKind.SCALAR:
			if td.name in ("Int", "Uint"):
				out = (word_bytes, word_bytes)
			elif td.name == "Byte":
				out = (1, 1)
			elif td.name == "Float":
				float_bytes = self.module.float_bits // 8
				out = (float_bytes, float_bytes)
			elif td.name == "Bool":
				out = (1, 1)
			elif td.name == "String":
				out = (word_bytes * 2, word_bytes)  # %DriftString = { usize, i8* }
			else:
				out = (8, 8)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind in (TypeKind.REF, TypeKind.ERROR):
			out = (word_bytes, word_bytes)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind is TypeKind.ARRAY:
			# Current array value lowering is a 4-word header (len, cap, gen, data ptr).
			out = (word_bytes * 4, word_bytes)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind is TypeKind.INTERFACE:
			base = (2 + DRIFT_IFACE_INLINE_WORDS) * word_bytes + 1
			if base % word_bytes != 0:
				base = ((base + word_bytes - 1) // word_bytes) * word_bytes
			out = (base, word_bytes)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind is TypeKind.FUNCTION:
			out = (word_bytes * 2, word_bytes) if td.can_throw() else (word_bytes, word_bytes)
			self._size_align_cache[ty_id] = out
			return out
		if td.kind is TypeKind.STRUCT:
			offset = 0
			max_align = 1
			inst = self.type_table.get_struct_instance(ty_id)
			field_types = inst.field_types if inst is not None else td.param_types
			for fty in field_types:
				fsz, fal = self._field_size_align_typeid(fty)
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
			out = (
				layout.payload_align_bytes + layout.payload_words * layout.payload_cell_bytes,
				layout.payload_align_bytes,
			)
			self._size_align_cache[ty_id] = out
			return out
		out = (8, 8)
		self._size_align_cache[ty_id] = out
		return out

	def _field_size_align_typeid(self, fty: TypeId) -> tuple[int, int]:
		"""
		Size/alignment of `fty` as a struct FIELD.

		Kept as the struct-layout call point so member layout and debug-info
		records stay in sync with storage mapping.
		"""
		return self._size_align_typeid(fty)

	def _variant_layout(self, ty_id: TypeId) -> _VariantLayout:
		"""
		Compute and cache the variant layout for a concrete TypeId.

		The variant value type is declared as:
		  %Variant_<module>_<name>_<hash> = type { i8, [pad x i8], [payload_words x usize] }

		Payload packing per constructor uses a literal struct type containing the
		constructor's field storage types (Bool stored as i8).
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for variant lowering")
		ty_id = self._canonical_codegen_typeid(ty_id)
		if ty_id in self._variant_layouts:
			return self._variant_layouts[ty_id]
		inst = self.type_table.get_variant_instance(ty_id)
		if inst is None:
			raise NotImplementedError(f"LLVM codegen v1: missing variant instance for TypeId {ty_id}")
		max_payload_size = 0
		max_payload_align = 1
		arms: list[tuple[str, _VariantArmLayout]] = []
		arm_by_name: Dict[str, _VariantArmLayout] = {}
		for arm in inst.arms:
			field_tys: list[TypeId] = []
			field_lltys: list[str] = []
			field_storage_lltys: list[str] = []
			offset = 0
			max_align = 1
			for fty_raw in arm.field_types:
				fty = self._canonical_codegen_typeid(fty_raw)
				field_tys.append(fty)
				llty = self._llvm_type_for_typeid(fty)
				emit_llty = self._llty(llty)
				field_lltys.append(llty)
				is_bool = self.type_table.get(fty).kind is TypeKind.SCALAR and self.type_table.get(fty).name == "Bool"
				st_llty = "i8" if is_bool else emit_llty
				field_storage_lltys.append(st_llty)
				sz, al = self._size_align_typeid(fty)
				if al > 1:
					offset = ((offset + al - 1) // al) * al
				offset += sz
				max_align = max(max_align, al)
			if max_align > 1:
				offset = ((offset + max_align - 1) // max_align) * max_align
			max_payload_size = max(max_payload_size, offset)
			max_payload_align = max(max_payload_align, max_align)
			payload_struct_llty = ""
			if field_storage_lltys:
				payload_struct_llty = "{ " + ", ".join(field_storage_lltys) + " }"
			arm_layout = _VariantArmLayout(
				tag=arm.tag,
				field_tys=field_tys,
				field_lltys=field_lltys,
				field_storage_lltys=field_storage_lltys,
				payload_struct_llty=payload_struct_llty,
			)
			arms.append((arm.name, arm_layout))
			arm_by_name[arm.name] = arm_layout
		arms.sort(key=lambda item: (item[1].tag, item[0]))
		word_bytes = max(1, self.module.word_bits // 8)
		payload_align_bytes = max(word_bytes, max_payload_align)
		payload_cell_bytes = payload_align_bytes
		payload_cell_llty = f"i{payload_cell_bytes * 8}"
		payload_words = max(1, (max_payload_size + payload_cell_bytes - 1) // payload_cell_bytes)
		llvm_ty = self.module.ensure_variant_type(
			ty_id,
			payload_words=payload_words,
			payload_cell_llty=payload_cell_llty,
			payload_align_bytes=payload_align_bytes,
			type_table=self.type_table,
		)
		layout = _VariantLayout(
			llvm_ty=llvm_ty,
			payload_words=payload_words,
			payload_cell_llty=payload_cell_llty,
			payload_cell_bytes=payload_cell_bytes,
			payload_align_bytes=payload_align_bytes,
			arms=arms,
			arm_by_name=arm_by_name,
		)
		self._variant_layouts[ty_id] = layout
		return layout

	def _resolve_forward_nominal_typeid(self, tid: TypeId) -> TypeId:
		if self.type_table is None:
			return tid
		seen: set[TypeId] = set()
		cur = tid
		while cur not in seen:
			seen.add(cur)
			td_cur = self.type_table.get(cur)
			if td_cur.kind is not TypeKind.FORWARD_NOMINAL:
				return cur
			mod = td_cur.module_id
			name = td_cur.name
			alias_def = self.type_table.lookup_type_alias(module_id=mod, name=name)
			if alias_def is not None:
				alias_params, alias_target, _loc = alias_def
				if not alias_params:
					resolved = resolve_opaque_type(alias_target, self.type_table, module_id=mod, type_params=None, allow_generic_base=True)
					if resolved != cur:
						cur = resolved
						continue
			nominal = (
				self.type_table.get_nominal(kind=TypeKind.STRUCT, module_id=mod, name=name)
				or self.type_table.get_nominal(kind=TypeKind.VARIANT, module_id=mod, name=name)
				or self.type_table.get_nominal(kind=TypeKind.INTERFACE, module_id=mod, name=name)
			)
			if nominal is not None and nominal != cur:
				cur = nominal
				continue
			unique = (
				self.type_table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=name)
				or self.type_table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=name)
				or self.type_table.find_unique_nominal_by_name(kind=TypeKind.INTERFACE, name=name)
			)
			if unique is not None and unique != cur:
				cur = unique
				continue
			return cur
		return cur

	def _canonical_codegen_typeid(self, tid: TypeId) -> TypeId:
		if self.type_table is None:
			return tid
		td_cur = self.type_table.get(tid)
		if td_cur.kind is TypeKind.FORWARD_NOMINAL:
			resolved = self._resolve_forward_nominal_typeid(tid)
			if resolved != tid:
				return self._canonical_codegen_typeid(resolved)
			return tid
		if td_cur.kind is TypeKind.REF and td_cur.param_types:
			inner = self._canonical_codegen_typeid(td_cur.param_types[0])
			return self.type_table.ensure_ref_mut(inner) if td_cur.ref_mut else self.type_table.ensure_ref(inner)
		if td_cur.kind is TypeKind.ARRAY and td_cur.param_types:
			elem = self._canonical_codegen_typeid(td_cur.param_types[0])
			return self.type_table.new_array(elem)
		if td_cur.kind is TypeKind.RAW_PTR and td_cur.param_types:
			inner = self._canonical_codegen_typeid(td_cur.param_types[0])
			return self.type_table.new_ptr(inner, module_id=td_cur.module_id)
		if td_cur.kind is TypeKind.STRUCT:
			inst = self.type_table.get_struct_instance(tid)
			if inst is not None and inst.type_args:
				args = [self._canonical_codegen_typeid(arg) for arg in inst.type_args]
				return self.type_table.ensure_struct_template(inst.base_id, args) if any(self.type_table.has_typevar(arg) for arg in args) else self.type_table.ensure_struct_instantiated(inst.base_id, args)
			return tid
		if td_cur.kind is TypeKind.VARIANT:
			inst = self.type_table.get_variant_instance(tid)
			if inst is not None and inst.type_args:
				args = [self._canonical_codegen_typeid(arg) for arg in inst.type_args]
				return self.type_table.ensure_variant_template(inst.base_id, args) if any(self.type_table.has_typevar(arg) for arg in args) else self.type_table.ensure_variant_instantiated(inst.base_id, args)
			return tid
		return tid

	def _optional_variant_type(self, inner_tid: TypeId) -> TypeId:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: Optional lowering requires a TypeTable")
		base = self.type_table.get_variant_base(module_id="lang.core", name="Optional")
		if base is None:
			raise NotImplementedError("LLVM codegen v1: Optional<T> variant base is missing")
		return self.type_table.ensure_instantiated(base, [inner_tid])

	def _emit_variant_value(self, variant_ty: TypeId, ctor: str, args: list[str]) -> str:
		layout = self._variant_layout(variant_ty)
		variant_llty = layout.llvm_ty
		arm_layout = layout.arm_by_name.get(ctor)
		if arm_layout is None:
			raise NotImplementedError(
				f"LLVM codegen v1: unknown variant constructor '{ctor}' for TypeId {variant_ty}"
			)
		tmp_ptr = self._scratch_alloca(variant_llty, "variant")
		self.lines.append(f"  store {variant_llty} zeroinitializer, ptr {tmp_ptr}")
		tag_ptr = self._fresh("tagptr")
		self.lines.append(
			f"  {tag_ptr} = getelementptr inbounds {variant_llty}, ptr {tmp_ptr}, i32 0, i32 0"
		)
		self.lines.append(f"  store i8 {arm_layout.tag}, ptr {tag_ptr}")
		if arm_layout.field_storage_lltys:
			payload_words_ptr = self._fresh("payload_words")
			self.lines.append(
				f"  {payload_words_ptr} = getelementptr inbounds {variant_llty}, ptr {tmp_ptr}, i32 0, i32 2"
			)
			payload_struct_ptr = payload_words_ptr
			for idx, (arg_val, field_ty, want_llty, store_llty) in enumerate(
				zip(args, arm_layout.field_tys, arm_layout.field_lltys, arm_layout.field_storage_lltys)
			):
				arg_val = self._coerce_value_to_typeid(None, arg_val, field_ty, context=f"variant payload {idx}")
				field_ptr = self._fresh("fieldptr")
				self.lines.append(
					f"  {field_ptr} = getelementptr inbounds {arm_layout.payload_struct_llty}, ptr {payload_struct_ptr}, i32 0, i32 {idx}"
				)
				if self._is_bool_storage_pair(value_llty=want_llty, storage_llty=store_llty):
					arg_val = self._bool_to_storage(arg_val)
					self.lines.append(f"  store i8 {arg_val}, ptr {field_ptr}")
				else:
					self.lines.append(f"  store {store_llty} {arg_val}, ptr {field_ptr}")
		out = self._fresh("variant_val")
		self.lines.append(f"  {out} = load {variant_llty}, ptr {tmp_ptr}")
		self.value_types[out] = variant_llty
		return out

	def _llvm_type_for_typeid(self, ty_id: TypeId, *, allow_void_ok: bool = False) -> str:
		"""
		Map a TypeId to an LLVM type string for parameters/arguments.

		v1 supports Int (isize), String (%DriftString), and Array<T> (by value).
		"""
		if self.type_table is not None:
			ty_id = self._canonical_codegen_typeid(ty_id)
			if self.type_table.is_void(ty_id):
				# Void ok-payloads/params are represented as an unused i8 slot.
				return "i8"
			td = self.type_table.get(ty_id)
			if td.kind is TypeKind.ARRAY and td.param_types:
				return self._llvm_array_header_type()
			if td.kind is TypeKind.STRUCT and td.name == "MaybeUninit" and td.module_id == "std.mem":
				inst = self.type_table.get_struct_instance(ty_id)
				if inst is not None and inst.type_args:
					return self._llvm_type_for_typeid(inst.type_args[0], allow_void_ok=allow_void_ok)
				if td.param_types:
					return self._llvm_type_for_typeid(td.param_types[0], allow_void_ok=allow_void_ok)
			if td.kind is TypeKind.SCALAR and td.name == "Int":
				return DRIFT_INT_TYPE
			if td.kind is TypeKind.SCALAR and td.name == "Uint":
				return DRIFT_USIZE_TYPE
			if td.kind is TypeKind.SCALAR and td.name in ("Uint64", "u64"):
				return DRIFT_U64_TYPE
			if td.kind is TypeKind.SCALAR and td.name in ("Int32", "Uint32"):
				return "i32"
			if td.kind is TypeKind.SCALAR and td.name == "Bool":
				return "i1"
			if td.kind is TypeKind.SCALAR and td.name == "Byte":
				return "i8"
			if td.kind is TypeKind.SCALAR and td.name == "Float":
				return self._llvm_float_type()
			if td.kind is TypeKind.SCALAR and td.name == "String":
				return DRIFT_STRING_TYPE
			if td.kind is TypeKind.REF:
				return "ptr"
			if td.kind is TypeKind.RAW_PTR:
				return "ptr"
			if td.kind is TypeKind.FUNCTION:
				if not td.param_types:
					raise NotImplementedError(
						f"LLVM codegen v1: function type missing param/return types for {self.func.name}"
					)
				return DRIFT_FAT_FNPTR_TYPE if td.can_throw() else "ptr"
			if td.kind is TypeKind.STRUCT:
				return self.module.ensure_struct_type(
					ty_id,
					type_table=self.type_table,
					map_type=self._emit_storage_type_for_typeid,
				)
			if td.kind is TypeKind.INTERFACE:
				return DRIFT_IFACE_TYPE
			if td.kind is TypeKind.VARIANT:
				# Concrete variants lower to a named LLVM struct type that contains a
				# tag byte and an aligned payload buffer.
				return self._variant_layout(ty_id).llvm_ty
			if td.kind is TypeKind.ERROR:
				return DRIFT_ERROR_PTR
			if td.kind is TypeKind.FNRESULT and td.param_types and len(td.param_types) >= 2:
				ok_tid, err_tid = td.param_types[0], td.param_types[1]
				err_def = self.type_table.get(err_tid)
				if err_def.kind is not TypeKind.ERROR:
					raise NotImplementedError(
						f"LLVM codegen v1: FnResult error type for {self.func.name} is {err_def.name}, expected Error"
					)
				ok_llty, ok_key = self._llvm_ok_type_for_typeid(ok_tid)
				return self.module.fnresult_type(ok_key, ok_llty, ok_typeid=ok_tid)
			if self.int_type_id is not None and ty_id == self.int_type_id:
				return DRIFT_INT_TYPE
			if self.float_type_id is not None and ty_id == self.float_type_id:
				return self._llvm_float_type()
			if self.string_type_id is not None and ty_id == self.string_type_id:
				return DRIFT_STRING_TYPE
		raise NotImplementedError(
			f"LLVM codegen v1: unsupported param type id {ty_id!r} for function {self.func.name}"
		)

	def _llvm_storage_type_for_typeid(self, ty_id: TypeId) -> str:
		"""
		Map a TypeId to an LLVM type string for storage in aggregates.

		Bool values are stored as i8 in aggregates per ABI.
		"""
		if self.type_table is not None and self.type_table.is_void(ty_id):
			# Void ok-payloads are represented as an unused i8 slot in aggregates.
			return "i8"
		llty = self._llvm_type_for_typeid(ty_id)
		if self.type_table is None:
			return llty
		td = self.type_table.get(ty_id)
		if td.kind is TypeKind.STRUCT and td.name == "MaybeUninit" and td.module_id == "std.mem":
			inst = self.type_table.get_struct_instance(ty_id)
			if inst is not None and inst.type_args:
				return self._llvm_storage_type_for_typeid(inst.type_args[0])
			if td.param_types:
				return self._llvm_storage_type_for_typeid(td.param_types[0])
		if td.kind is TypeKind.SCALAR and td.name == "Bool":
			return "i8"
		return llty

	def _llvm_field_storage_type_for_typeid(self, field_ty: TypeId) -> str:
		"""
		Map a TypeId to its storage type as a struct field.

		Struct fields now share the same representation as all other storage
		slots; this wrapper remains as the field-level call point.
		"""
		return self._llvm_storage_type_for_typeid(field_ty)

	def _is_throwing_fn_typeid(self, ty_id: TypeId | None) -> bool:
		if ty_id is None or self.type_table is None:
			return False
		td = self.type_table.get(ty_id)
		return td.kind is TypeKind.FUNCTION and td.can_throw()

	def _fn_typeid_from_call_sig(self, call_sig: CallSig, *, can_throw: bool | None = None) -> TypeId:
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: function pointer constants require a TypeTable")
		return self.type_table.ensure_function(
			list(call_sig.param_types),
			call_sig.user_ret_type,
			can_throw=call_sig.can_throw if can_throw is None else bool(can_throw),
		)

	def _coerce_value_to_typeid(self, arg_mir_name: str | None, arg_val: str, target_ty: TypeId, *, context: str) -> str:
		if self._is_throwing_fn_typeid(target_ty):
			return self._coerce_throwing_fn_to_fat(arg_mir_name, arg_val, target_ty, context=context)
		return arg_val

	def _coerce_throwing_fn_to_fat(self, arg_mir_name: str | None, arg_val: str, field_ty: TypeId, *, context: str) -> str:
		"""
		Normalize a throwing-Fn value to the fat %DriftFatFnPtr {adapter, env}
		pair used for throwing Fn values.

		Accepts values that are already fat, thin throwing fn refs (wrapped via
		the generic forward thunk), and nothrow fn refs (wrapped via a
		nothrow→throwing adapter thunk).
		Throw-state is resolved through _value_fn_throws (populated by
		FnPtrConst), value_map aliases, then the MIR local's TypeId — with
		opaque pointers all thin fn values are just "ptr" in value_types.
		"""
		have = self.value_types.get(arg_val)
		if have == DRIFT_FAT_FNPTR_TYPE:
			return arg_val
		arg_throws = self._value_fn_throws.get(arg_val)
		if arg_throws is None:
			resolved = arg_val
			while resolved in self.value_map:
				resolved = self.value_map[resolved]
			arg_throws = self._value_fn_throws.get(resolved)
		if arg_throws is None and arg_mir_name is not None:
			arg_tid = self.func.local_types.get(arg_mir_name)
			if arg_tid is not None:
				arg_td = self.type_table.get(arg_tid)
				if arg_td.kind is TypeKind.FUNCTION:
					arg_throws = arg_td.can_throw()
		if arg_throws is False and arg_val.startswith("@"):
			thunk_name = self._emit_nothrow_to_throwing_thunk(arg_val, field_ty)
			adapter_sym = f"@{thunk_name}"
			env_val = "null"
		elif arg_throws is False:
			thunk_name = self._ensure_generic_nothrow_wrap_thunk(field_ty)
			adapter_sym = f"@{thunk_name}"
			env_val = arg_val
		elif arg_throws is True:
			thunk_name = self._ensure_generic_forward_thunk(field_ty)
			adapter_sym = f"@{thunk_name}"
			env_val = arg_val
		elif have is None:
			raise AssertionError(
				f"LLVM codegen: throwing fn-ptr {context} has no tracked type for arg {arg_val}"
			)
		else:
			raise NotImplementedError(
				f"LLVM codegen v1: {context} fn-ptr throw state unknown for arg {arg_val}"
			)
		fat0 = self._fresh("fat")
		fat1 = self._fresh("fat")
		self.lines.append(f"  {fat0} = insertvalue {DRIFT_FAT_FNPTR_TYPE} zeroinitializer, ptr {adapter_sym}, 0")
		self.lines.append(f"  {fat1} = insertvalue {DRIFT_FAT_FNPTR_TYPE} {fat0}, ptr {env_val}, 1")
		self.value_types[fat1] = DRIFT_FAT_FNPTR_TYPE
		return fat1

	def _type_key(self, ty_id: TypeId) -> str:
		"""Build a stable key string for a TypeId (used for FnResult naming/diagnostics)."""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for FnResult lowering")
		td = self.type_table.get(ty_id)
		raw_key = self.type_table.type_key_string(ty_id)
		def _safe_key(raw: str) -> str:
			safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in raw)
			suffix = f"{hash64(raw.encode()):016x}"
			return f"{safe}_{suffix}"
		if td.kind is TypeKind.SCALAR:
			if td.module_id is not None:
				return _safe_key(raw_key)
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
		if td.kind is TypeKind.RAW_PTR and td.param_types:
			inner_key = self._type_key(td.param_types[0])
			return f"Ptr_{inner_key}"
		if td.kind is TypeKind.STRUCT:
			return _safe_key(raw_key)
		if td.kind is TypeKind.VARIANT:
			return _safe_key(raw_key)
		if td.kind is TypeKind.FUNCTION:
			if not td.param_types:
				return "FnPtr_Unknown"
			args = "_".join(self._type_key(t) for t in td.param_types[:-1]) or "Void"
			ret = self._type_key(td.param_types[-1])
			throw_tag = "CanThrow" if td.fn_throws else "NoThrow"
			return f"FnPtr_{args}_to_{ret}_{throw_tag}"
		return f"{td.kind.name}"

	def _llvm_ok_type_for_typeid(self, ty_id: TypeId) -> tuple[str, str]:
		"""
		Map an Ok TypeId to (ok_llty, ok_key) for FnResult payloads.

		Supported in v1: Int -> isize, String -> %DriftString, Void -> i8, Ref<T> -> T*,
		function pointers, and concrete Struct/Variant values by-value.
		Other kinds are rejected with a clear diagnostic.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for FnResult lowering")
		td = self.type_table.get(ty_id)
		key = self._type_key(ty_id)
		if td.kind is TypeKind.SCALAR and td.name == "Int":
			return DRIFT_INT_TYPE, key
		if td.kind is TypeKind.SCALAR and td.name == "Uint":
			return DRIFT_USIZE_TYPE, key
		if td.kind is TypeKind.SCALAR and td.name in ("Uint64", "u64"):
			return DRIFT_U64_TYPE, key
		if td.kind is TypeKind.SCALAR and td.name == "String":
			return DRIFT_STRING_TYPE, key
		if td.kind is TypeKind.SCALAR and td.name == "Bool":
			return "i8", key
		if td.kind is TypeKind.SCALAR and td.name == "Byte":
			return "i8", key
		if td.kind is TypeKind.SCALAR and td.name == "Float":
			return self._llvm_float_type(), key
		if td.kind is TypeKind.SCALAR and td.name in ("Int32", "Uint32"):
			return "i32", key
		if td.kind is TypeKind.VOID:
			return "i8", key
		if td.kind is TypeKind.REF:
			return "ptr", key
		if td.kind is TypeKind.RAW_PTR:
			return "ptr", key
		if td.kind is TypeKind.FUNCTION:
			return self._llvm_type_for_typeid(ty_id), key
		if td.kind in (TypeKind.ARRAY, TypeKind.STRUCT, TypeKind.VARIANT):
			return self._llvm_type_for_typeid(ty_id), key
		supported = "Int, Uint, Uint64, Bool, Byte, Float, String, Void, Ref<T>, Array<T>, Struct, Variant, FnPtr"
		raise NotImplementedError(
			f"LLVM codegen v1: FnResult ok type {key} is not supported yet; supported ok payloads: {supported}"
		)

	def _llvm_ok_abi_type_for_typeid(self, ty_id: TypeId) -> str:
		"""
		Map an Ok TypeId to its ABI-boundary payload type for Result wrappers.

		Bool uses storage form (i8) at the boundary; other supported ok payloads
		use their normal value representation.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: TypeTable required for ok ABI lowering")
		td = self.type_table.get(ty_id)
		if td.kind is TypeKind.SCALAR and td.name == "Bool":
			return "i8"
		ok_llty, _ = self._llvm_ok_type_for_typeid(ty_id)
		return ok_llty

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

	def _llvm_float_type(self) -> str:
		bits = self.module.float_bits
		if bits == 32:
			return "float"
		if bits == 64:
			return "double"
		raise NotImplementedError("LLVM codegen v1: unsupported float width")

	def _llty(self, ty: str) -> str:
		return self.module._llty(ty)

	def _emit_storage_type_for_typeid(self, ty_id: TypeId) -> str:
		return self._llty(self._llvm_storage_type_for_typeid(ty_id))

	def _scalar_cast_info(self, ty_id: TypeId) -> tuple[str, int, bool] | None:
		if self.type_table is None:
			return None
		td = self.type_table.get(ty_id)
		if td.kind is not TypeKind.SCALAR:
			return None
		if td.name == "Int":
			return (DRIFT_INT_TYPE, self.module.word_bits, True)
		if td.name == "Uint":
			return (DRIFT_USIZE_TYPE, self.module.word_bits, False)
		if td.name == "Uint64":
			return (DRIFT_U64_TYPE, 64, False)
		if td.name == "Int32":
			return ("i32", 32, True)
		if td.name == "Uint32":
			return ("i32", 32, False)
		if td.name == "Byte":
			return ("i8", 8, False)
		if td.name == "Bool":
			return ("i1", 1, False)
		return None

	def _llvm_scalar_type(self) -> str:
		# All lowered values are isize or i1; phis currently assume Int.
		return DRIFT_INT_TYPE

	def _fnresult_typeids_for_fn(self, info: FnInfo | None = None) -> tuple[TypeId, TypeId]:
		"""
		Return (ok, err) TypeIds for the internal can-throw ABI of a function.

		Important: `FnResult` is an *internal* carrier type in lang. Surface
		signatures still declare `-> T`, and can-throw is an effect tracked
		separately (via `FnInfo.declared_can_throw`). Codegen lowers can-throw
		functions to return `FnResult<T, Error>`, deriving `T` from the signature's
		`return_type_id` and `Error` from the shared TypeTable.

		We keep a legacy fallback: older tests may still model can-throw functions
		as explicitly returning `FnResult<_, Error>` at the signature level. In that
		case we extract `(ok, err)` from the signature's return type directly.
		"""
		fn = info or self.fn_info
		# Terminal-throws functions (bare `throws`, no return type) never
		# return a value — they always exit via exception. Use Void as the
		# ok type so FnResult lowering produces a valid ABI carrier.
		if fn.signature is not None and bool(getattr(fn.signature, "declared_terminal_throws", False)):
			ok_tid = self.type_table.ensure_void() if self.type_table is not None else None
			if ok_tid is not None:
				err_tid = self.type_table.ensure_error()
				return ok_tid, err_tid
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
		fnres_llty = self.module.fnresult_type(ok_key, ok_llty, ok_typeid=ok_tid)
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
		if ok_llty == DRIFT_INT_TYPE:
			return f"{self._llty(DRIFT_INT_TYPE)} 0"
		if ok_llty == DRIFT_UINT_TYPE:
			return f"{self._llty(DRIFT_UINT_TYPE)} 0"
		if ok_llty == DRIFT_U64_TYPE:
			return f"{DRIFT_U64_TYPE} 0"
		if ok_llty == "i1":
			return "i1 0"
		if ok_llty == "i8":
			return "i8 0"
		if ok_llty == "double":
			return "double 0.0"
		if _is_ptr_type(ok_llty):
			return "ptr null"
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
		if llty == DRIFT_INT_TYPE:
			return f"{self._llty(DRIFT_INT_TYPE)} 0"
		if llty == "i1":
			return "i1 0"
		if llty == "double":
			return "double 0.0"
		if _is_ptr_type(llty):
			return "ptr null"
		# Arrays/structs (including String-as-aggregate) can be used as constants.
		if td is not None and td.kind in (TypeKind.ARRAY, TypeKind.STRUCT, TypeKind.SCALAR, TypeKind.ERROR):
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
		if llty == DRIFT_INT_TYPE:
			self.lines.append(f"  {dest} = add {self._llty(DRIFT_INT_TYPE)} 0, 0")
			self.value_types[dest] = DRIFT_INT_TYPE
			return
		if llty == DRIFT_UINT_TYPE:
			self.lines.append(f"  {dest} = add {self._llty(DRIFT_UINT_TYPE)} 0, 0")
			self.value_types[dest] = DRIFT_UINT_TYPE
			return
		if llty == DRIFT_U64_TYPE:
			self.lines.append(f"  {dest} = add {self._llty(DRIFT_U64_TYPE)} 0, 0")
			self.value_types[dest] = DRIFT_U64_TYPE
			return
		if llty == "i1":
			self.lines.append(f"  {dest} = add i1 0, 0")
			self.value_types[dest] = "i1"
			return
		if llty == "i8":
			self.lines.append(f"  {dest} = add i8 0, 0")
			self.value_types[dest] = "i8"
			return
		# Remaining fixed-width integer scalars (i16/i32, e.g. Int32/Uint32).
		# i1/i8 and the i64-backed tag types are handled above; everything
		# else of the form i<N> takes the same `add <ty> 0, 0` form.
		if llty.startswith("i") and llty[1:].isdigit():
			self.lines.append(f"  {dest} = add {llty} 0, 0")
			self.value_types[dest] = llty
			return
		# Float scalars: `double` (64-bit) or `float` (32-bit, when float_bits==32).
		if llty in ("double", "float"):
			self.lines.append(f"  {dest} = fadd {llty} 0.0, 0.0")
			self.value_types[dest] = llty
			return
		if td.kind is TypeKind.VOID:
			self.lines.append(f"  {dest} = add i8 0, 0")
			self.value_types[dest] = "i8"
			return
		if _is_ptr_type(llty):
			# Pointer null as an SSA value.
			self.lines.append(f"  {dest} = select i1 1, ptr null, ptr null")
			self.value_types[dest] = "ptr"
			return
		if llty == DRIFT_FAT_FNPTR_TYPE:
			self.lines.append(
				f"  {dest} = select i1 1, {DRIFT_FAT_FNPTR_TYPE} zeroinitializer, {DRIFT_FAT_FNPTR_TYPE} zeroinitializer"
			)
			self.value_types[dest] = DRIFT_FAT_FNPTR_TYPE
			return

		# Array runtime representation is a fixed 4-field aggregate in v1:
		#   { len: i64, cap: i64, gen: i64, data: i8* }
		if td.kind is TypeKind.ARRAY and td.param_types:
			elem_llty = self._emit_storage_type_for_typeid(td.param_types[0])
			arr_llty = self._llvm_array_header_type()
			tmp0 = self._fresh("zero_arr")
			self.lines.append(f"  {tmp0} = insertvalue {arr_llty} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_LEN_IDX}")
			tmp1 = self._fresh("zero_arr")
			self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_CAP_IDX}")
			tmp2 = self._fresh("zero_arr")
			self.lines.append(f"  {tmp2} = insertvalue {arr_llty} {tmp1}, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_GEN_IDX}")
			self.lines.append(f"  {dest} = insertvalue {arr_llty} {tmp2}, ptr null, {ARRAY_PTR_IDX}")
			self.value_types[dest] = arr_llty
			return

		# Structs (including String, which is represented as a scalar TypeId but
		# lowered to `%DriftString` aggregate): materialize field-by-field using
		# constant operands.
		if llty == DRIFT_STRING_TYPE:
			tmp0 = self._fresh("zero_str")
			self.lines.append(f"  {tmp0} = insertvalue {DRIFT_STRING_TYPE} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} 0, 0")
			self.lines.append(f"  {dest} = insertvalue {DRIFT_STRING_TYPE} {tmp0}, ptr null, 1")
			self.value_types[dest] = DRIFT_STRING_TYPE
			return
		if td.kind is TypeKind.INTERFACE:
			self.lines.append(
				f"  {dest} = select i1 1, {DRIFT_IFACE_TYPE} zeroinitializer, {DRIFT_IFACE_TYPE} zeroinitializer"
			)
			self.value_types[dest] = DRIFT_IFACE_TYPE
			return
		if td.kind is TypeKind.VARIANT:
			self.lines.append(f"  {dest} = select i1 1, {llty} zeroinitializer, {llty} zeroinitializer")
			self.value_types[dest] = llty
			return

		if td.kind is TypeKind.STRUCT:
			inst = self.type_table.get_struct_instance(ty)
			if inst is None:
				raise NotImplementedError("LLVM codegen v1: struct zero requires instance metadata")
			if not inst.field_types:
				self.lines.append(
					f"  {dest} = select i1 1, {llty} zeroinitializer, {llty} zeroinitializer"
				)
				self.value_types[dest] = llty
				return
			cur = "zeroinitializer"
			last_idx = len(inst.field_types) - 1
			for idx, fty in enumerate(inst.field_types):
				store_llty = self._llvm_field_storage_type_for_typeid(fty)
				emit_store_llty = self._llty(store_llty)
				if _is_ptr_type(emit_store_llty):
					operand = "ptr null"
				elif emit_store_llty == "double":
					operand = "double 0.0"
				elif emit_store_llty.startswith("i"):
					operand = f"{emit_store_llty} 0"
				else:
					operand = f"{emit_store_llty} zeroinitializer"
				out = dest if idx == last_idx else self._fresh("zero_struct")
				self.lines.append(f"  {out} = insertvalue {llty} {cur}, {operand}, {idx}")
				cur = out
			self.value_types[dest] = llty
			return

		# Fallback: keep this strict so we don't silently invent ABI behavior.
		raise NotImplementedError(f"LLVM codegen v1: cannot materialize zero value for type {td.kind} ({llty})")

	def _emit_tombstone_value(self, ty_id: TypeId) -> str:
		"""
		Emit a non-owning tombstone value for `ty_id`.

		This is used to neutralize slots after ArrayElemTake for droppable types.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: tombstone emission requires a TypeTable")
		if not self._type_needs_drop(ty_id):
			dest = self._fresh("tomb")
			self._emit_zero_value(dest, ty_id)
			return dest
		td = self.type_table.get(ty_id)
		if td.kind is TypeKind.SCALAR and td.name == "String":
			dest = self._fresh("tomb_str")
			self._emit_zero_value(dest, ty_id)
			return dest
		if td.kind is TypeKind.ARRAY and td.param_types:
			dest = self._fresh("tomb_arr")
			self._emit_zero_value(dest, ty_id)
			return dest
		if td.kind is TypeKind.INTERFACE:
			dest = self._fresh("tomb_iface")
			self._emit_zero_value(dest, ty_id)
			return dest
		if td.kind is TypeKind.STRUCT:
			inst = self.type_table.get_struct_instance(ty_id)
			if inst is None:
				raise NotImplementedError("LLVM codegen v1: struct tombstone requires instance metadata")
			llty = self._llvm_type_for_typeid(ty_id)
			if not inst.field_types:
				dest = self._fresh("tomb_struct")
				self._emit_zero_value(dest, ty_id)
				return dest
			cur = "zeroinitializer"
			last_idx = len(inst.field_types) - 1
			for idx, fty in enumerate(inst.field_types):
				store_llty = self._llvm_field_storage_type_for_typeid(fty)
				if self._type_needs_drop(fty):
					field_val = self._emit_tombstone_value(fty)
				elif store_llty == DRIFT_FAT_FNPTR_TYPE:
					# Throwing Fn fields are stored fat; a bare-Fn zero value
					# would be a thin ptr and disagree with the field slot.
					field_val = self._fresh("tomb_zero")
					self.lines.append(
						f"  {field_val} = select i1 1, {DRIFT_FAT_FNPTR_TYPE} zeroinitializer, {DRIFT_FAT_FNPTR_TYPE} zeroinitializer"
					)
					self.value_types[field_val] = DRIFT_FAT_FNPTR_TYPE
				else:
					field_val = self._fresh("tomb_zero")
					self._emit_zero_value(field_val, fty)
				emit_store_llty = self._llty(store_llty)
				if self._is_bool_storage_pair(value_llty=self._llvm_type_for_typeid(fty), storage_llty=store_llty):
					field_val = self._bool_to_storage(field_val)
				out = self._fresh("tomb_struct") if idx != last_idx else self._fresh("tomb_struct_out")
				self.lines.append(f"  {out} = insertvalue {llty} {cur}, {emit_store_llty} {field_val}, {idx}")
				self.value_types[out] = llty
				cur = out
			return cur
		if td.kind is TypeKind.VARIANT:
			inst, ctor = self._resolve_variant_tombstone_ctor(ty_id)
			arm = inst.arms_by_name.get(ctor)
			if arm is None:
				if ctor != "__drift_internal_tombstone":
					raise AssertionError(f"internal: tombstone ctor '{ctor}' missing in variant instance")
				layout = self._variant_layout(ty_id)
				variant_llty = layout.llvm_ty
				tmp_ptr = self._scratch_alloca(variant_llty, "variant_tomb")
				self.lines.append(f"  store {variant_llty} zeroinitializer, ptr {tmp_ptr}")
				tag = inst.internal_tombstone_tag
				if tag is None:
					raise AssertionError("internal: missing internal tombstone tag metadata")
				tag_ptr = self._fresh("variant_tomb_tag")
				self.lines.append(f"  {tag_ptr} = getelementptr inbounds {variant_llty}, ptr {tmp_ptr}, i32 0, i32 0")
				self.lines.append(f"  store i8 {tag}, ptr {tag_ptr}")
				out = self._fresh("variant_tomb_val")
				self.lines.append(f"  {out} = load {variant_llty}, ptr {tmp_ptr}")
				self.value_types[out] = variant_llty
				return out
			if arm.field_types:
				raise AssertionError("internal: tombstone ctor payload must be empty in v1")
			return self._emit_variant_value(ty_id, ctor, [])
		raise NotImplementedError(f"LLVM codegen v1: tombstone unsupported for {td.kind.name}")

	def _resolve_variant_tombstone_ctor(self, ty_id: TypeId) -> tuple[object, str]:
		"""
		Resolve effective tombstone constructor for a concrete variant type.

		Centralizes tombstone selection so all variant tombstone emit paths use
		instantiation metadata first (including synthesized internal tombstones).
		"""
		if self.type_table is None:
			raise AssertionError("internal: variant tombstone requires type table metadata")
		inst = self.type_table.get_variant_instance(ty_id)
		if inst is None:
			raise AssertionError("internal: variant tombstone requires instance metadata")
		schema = self.type_table.get_variant_schema(inst.base_id)
		if schema is None:
			raise AssertionError("internal: variant tombstone requires schema metadata")
		ctor = inst.internal_tombstone_ctor or schema.tombstone_ctor
		if not ctor:
			raise AssertionError(f"internal: variant '{schema.name}' missing tombstone ctor metadata")
		arm = inst.arms_by_name.get(ctor)
		if arm is None:
			if ctor == "__drift_internal_tombstone":
				return inst, ctor
			raise AssertionError(f"internal: tombstone ctor '{ctor}' missing in variant instance")
		if arm.field_types:
			raise AssertionError("internal: tombstone ctor payload must be empty in v1")
		return inst, ctor

	def _fresh(self, hint: str = "tmp") -> str:
		self.tmp_counter += 1
		return f"%{hint}{self.tmp_counter}"

	def _wrap_ok_fnresult(self, raw_val: str | None, ok_llty: str, dest: str, *, hint: str = "okwrap") -> None:
		"""Wrap a raw intrinsic result into an ok FnResult at `dest`.

		For void results, pass raw_val=None and ok_llty="i8" (Void uses i8 placeholder).
		Emits insertvalue chain: { i1 0, <ok_val>, %DriftError* null }.
		"""
		ok_key = ok_llty
		for lbl, lt in [("Int", self._llty(DRIFT_INT_TYPE)), ("String", DRIFT_STRING_TYPE), ("Void", "i8")]:
			if self._llty(ok_llty) == self._llty(lt):
				ok_key = lbl
				break
		if "*" in ok_key:
			ok_key = ok_key.replace("*", "Ptr")
		fnres_llty = self.module._declare_fnresult_named_type(ok_key, ok_llty)
		tmp0 = self._fresh(f"{hint}0")
		self.lines.append(f"  {tmp0} = insertvalue {fnres_llty} zeroinitializer, i8 0, 0")
		if raw_val is not None:
			tmp1 = self._fresh(f"{hint}1")
			self.lines.append(f"  {tmp1} = insertvalue {fnres_llty} {tmp0}, {self._llty(ok_llty)} {raw_val}, 1")
			self.lines.append(f"  {dest} = insertvalue {fnres_llty} {tmp1}, {DRIFT_ERROR_PTR} null, 2")
		else:
			self.lines.append(f"  {dest} = insertvalue {fnres_llty} {tmp0}, {DRIFT_ERROR_PTR} null, 2")
		self.value_types[dest] = fnres_llty

	@staticmethod
	def _bb(block_name: str) -> str:
		"""Map a MIR block name to a compiler-reserved LLVM label.

		Block labels and SSA value names share one namespace in LLVM IR, so a
		block label must not be a string any source identifier could equal. The
		``.bb.`` prefix uses ``.`` — a character outside the grammar's `NAME`
		set ``[A-Za-z0-9_]`` — so no user parameter/local can collide with a
		block label (the same non-source-namespace discipline as
		`MirBuilder.new_temp`'s ``.t<N>`` value temporaries).

		(The earlier ``__bb_`` prefix did NOT guarantee this: ``__bb_entry`` is a
		legal source identifier, so a user local named after a block label could
		collide. Codegen-internal SSA names minted by ``_fresh`` — including the
		sub-block labels it produces — remain in the source-collidable
		``[A-Za-z0-9_]`` space; that is a separate, lower-severity latent issue,
		since it requires an exact accidental name match and fails loudly as an
		LLVM verifier duplicate-name error rather than miscompiling.)
		"""
		return f".bb.{block_name}"

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

	def _map_binop(self, op: BinaryOp, *, unsigned: bool = False) -> str:
		if op == BinaryOp.ADD:
			return "add"
		if op == BinaryOp.SUB:
			return "sub"
		if op == BinaryOp.MUL:
			return "mul"
		if op == BinaryOp.DIV:
			return "udiv" if unsigned else "sdiv"
		if op == BinaryOp.MOD:
			return "urem" if unsigned else "srem"
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
			return "icmp ult" if unsigned else "icmp slt"
		if op == BinaryOp.LE:
			return "icmp ule" if unsigned else "icmp sle"
		if op == BinaryOp.GT:
			return "icmp ugt" if unsigned else "icmp sgt"
		if op == BinaryOp.GE:
			return "icmp uge" if unsigned else "icmp sge"
		raise NotImplementedError(f"LLVM codegen v1: unsupported binary op {op}")

	def _llvm_name(self, val: str) -> str:
		return val if val.startswith("%") else self._map_value(val)

	def _retain_string(self, val: str) -> str:
		self.module.needs_string_retain = True
		out = self._fresh("str_retain")
		self.lines.append(f"  {out} = call {DRIFT_STRING_TYPE} @drift_string_retain({DRIFT_STRING_TYPE} {val})")
		self.value_types[out] = DRIFT_STRING_TYPE
		return out

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
			# Arithmetic negation on Int and Float.
			if ty in (None, DRIFT_INT_TYPE):
				self.lines.append(f"  {dest} = sub {self._llty(DRIFT_INT_TYPE)} 0, {operand}")
				self.value_types[dest] = DRIFT_INT_TYPE
				return
			if ty in ("double", "float"):
				self.lines.append(f"  {dest} = fsub {ty} 0.0, {operand}")
				self.value_types[dest] = ty
				return
			raise NotImplementedError("LLVM codegen v1: neg only supported on Int/Float")
		if instr.op is UnaryOp.BIT_NOT:
			if ty not in (DRIFT_USIZE_TYPE, DRIFT_U64_TYPE):
				raise AssertionError(f"LLVM codegen v1: bitwise not requires Uint or Uint64 (have {ty})")
			self.lines.append(f"  {dest} = xor {self._llty(ty)} {operand}, -1")
			self.value_types[dest] = ty
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

		# Float ops on float/double.
		if left_ty in ("double", "float") and right_ty == left_ty:
			float_ty = left_ty
			if instr.op is BinaryOp.ADD:
				self.lines.append(f"  {dest} = fadd {float_ty} {left}, {right}")
				self.value_types[dest] = float_ty
				return
			if instr.op is BinaryOp.SUB:
				self.lines.append(f"  {dest} = fsub {float_ty} {left}, {right}")
				self.value_types[dest] = float_ty
				return
			if instr.op is BinaryOp.MUL:
				self.lines.append(f"  {dest} = fmul {float_ty} {left}, {right}")
				self.value_types[dest] = float_ty
				return
			if instr.op is BinaryOp.DIV:
				self.lines.append(f"  {dest} = fdiv {float_ty} {left}, {right}")
				self.value_types[dest] = float_ty
				return
			if instr.op is BinaryOp.EQ:
				self.lines.append(f"  {dest} = fcmp oeq {float_ty} {left}, {right}")
				self.value_types[dest] = "i1"
				return
			if instr.op is BinaryOp.NE:
				self.lines.append(f"  {dest} = fcmp one {float_ty} {left}, {right}")
				self.value_types[dest] = "i1"
				return
			if instr.op is BinaryOp.LT:
				self.lines.append(f"  {dest} = fcmp olt {float_ty} {left}, {right}")
				self.value_types[dest] = "i1"
				return
			if instr.op is BinaryOp.LE:
				self.lines.append(f"  {dest} = fcmp ole {float_ty} {left}, {right}")
				self.value_types[dest] = "i1"
				return
			if instr.op is BinaryOp.GT:
				self.lines.append(f"  {dest} = fcmp ogt {float_ty} {left}, {right}")
				self.value_types[dest] = "i1"
				return
			if instr.op is BinaryOp.GE:
				self.lines.append(f"  {dest} = fcmp oge {float_ty} {left}, {right}")
				self.value_types[dest] = "i1"
				return
			raise NotImplementedError(f"LLVM codegen v1: unsupported float binary op {instr.op}")

		# Integer ops on isize/usize.
		int_ty = None
		unsigned = False
		# Some intrinsic lowering paths record concrete LLVM integer types
		# (e.g. i64) while constants keep abstract drift integer kinds.
		# Normalize mixed abstract/concrete pairs before selecting op kind.
		abstract_ints = {DRIFT_INT_TYPE, DRIFT_USIZE_TYPE, DRIFT_U64_TYPE}
		if left_ty in abstract_ints and self._llty(left_ty) == right_ty:
			right_ty = left_ty
		elif right_ty in abstract_ints and self._llty(right_ty) == left_ty:
			left_ty = right_ty
		bitwise_ops = {BinaryOp.BIT_AND, BinaryOp.BIT_OR, BinaryOp.BIT_XOR, BinaryOp.SHL, BinaryOp.SHR}
		if instr.op in bitwise_ops:
			if left_ty == DRIFT_USIZE_TYPE and right_ty == DRIFT_USIZE_TYPE:
				int_ty = DRIFT_USIZE_TYPE
				unsigned = True
			elif left_ty == DRIFT_U64_TYPE and right_ty == DRIFT_U64_TYPE:
				int_ty = DRIFT_U64_TYPE
				unsigned = True
			else:
				raise AssertionError(
					f"LLVM codegen v1: bitwise ops require matched Uint or Uint64 operands (have {left_ty}, {right_ty})"
				)
		elif left_ty == DRIFT_INT_TYPE and right_ty == DRIFT_INT_TYPE:
			int_ty = DRIFT_INT_TYPE
			unsigned = False
		elif left_ty == DRIFT_USIZE_TYPE and right_ty == DRIFT_USIZE_TYPE:
			int_ty = DRIFT_USIZE_TYPE
			unsigned = True
		elif left_ty == DRIFT_U64_TYPE and right_ty == DRIFT_U64_TYPE:
			int_ty = DRIFT_U64_TYPE
			unsigned = True
		elif left_ty == "i8" and right_ty == "i8":
			int_ty = "i8"
			unsigned = True
		elif (
			left_ty == "i32"
			and right_ty == "i32"
			and instr.op in {BinaryOp.EQ, BinaryOp.NE, BinaryOp.LT, BinaryOp.LE, BinaryOp.GT, BinaryOp.GE}
		):
			# Same-width narrow-int (`Int32`/`Uint32`) COMPARISON → `Bool`.
			# `value_types` collapses both to "i32" (no signedness), so the
			# operand signedness is carried on the instruction (`instr.signed`,
			# set by HIR→MIR from the operand type): `Uint32` (signed=False)
			# emits unsigned `icmp u…`, `Int32` (signed=True) signed `icmp s…`.
			# EQ/NE are signedness-agnostic regardless.  Narrow-int arithmetic
			# stays unsupported (the `int_ty is None` raise below rejects `i32`
			# add/sub/etc.).
			int_ty = "i32"
			unsigned = getattr(instr, "signed", None) is False
		if int_ty is None:
			raise NotImplementedError(
				f"LLVM codegen v1: integer binop requires matching Int/Uint operands (have {left_ty}, {right_ty})"
			)
		op_str = self._map_binop(instr.op, unsigned=unsigned)
		dest_ty = int_ty if not op_str.startswith("icmp") else "i1"
		self.value_types[dest] = dest_ty
		emit_int_ty = self._llty(int_ty)
		self.lines.append(f"  {dest} = {op_str} {emit_int_ty} {left}, {right}")

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

	def _lower_const_array(self, instr: ConstArray) -> None:
		"""Lower ConstArray: emit a read-only LLVM global and build a DriftArrayHeader."""
		import struct
		dest = self._map_value(instr.dest)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		count = len(instr.values)
		is_bool = self._is_bool_type(instr.elem_ty)
		is_float = False
		if self.type_table is not None:
			td = self.type_table.get(instr.elem_ty)
			is_float = td.kind is TypeKind.SCALAR and td.name == "Float"
		# Build cache key for dedup
		cache_key = (int(instr.elem_ty), tuple(instr.values))
		cached = self.module.const_array_cache.get(cache_key)
		if cached is not None:
			global_name, arr_type_str, cached_count = cached
		else:
			global_name = f"@.carr{len(self.module.consts)}"
			arr_type_str = f"[{count} x {self._llty(elem_llty)}]"
			# Format element values
			parts = []
			for v in instr.values:
				if is_bool:
					parts.append(f"i8 {1 if v else 0}")
				elif is_float:
					raw = struct.pack(">d", float(v))
					parts.append(f"double 0x{raw.hex().upper()}")
				else:
					parts.append(f"{self._llty(elem_llty)} {v}")
			init = ", ".join(parts)
			self.module.consts.append(f"{global_name} = private unnamed_addr constant {arr_type_str} [{init}]")
			self.module.const_array_cache[cache_key] = (global_name, arr_type_str, count)
		# Build data pointer from global constant.
		data_ptr = self._fresh("cadata")
		data_ptr = global_name
		# Build %DriftArrayHeader struct
		tmp0 = self._fresh("carh0")
		self.lines.append(f"  {tmp0} = insertvalue {arr_llty} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} {count}, {ARRAY_LEN_IDX}")
		tmp1 = self._fresh("carh1")
		self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, {self._llty(DRIFT_INT_TYPE)} {count}, {ARRAY_CAP_IDX}")
		tmp2 = self._fresh("carh2")
		self.lines.append(f"  {tmp2} = insertvalue {arr_llty} {tmp1}, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_GEN_IDX}")
		self.lines.append(f"  {dest} = insertvalue {arr_llty} {tmp2}, ptr {data_ptr}, {ARRAY_PTR_IDX}")
		self.value_types[dest] = arr_llty

	def _lower_array_lit(self, instr: ArrayLit) -> None:
		"""Lower ArrayLit by allocating, storing elements, and building the header struct."""
		dest = self._map_value(instr.dest)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		elem_size, elem_align = self._array_elem_layout(instr.elem_ty, elem_llty)
		count = len(instr.elements)
		# Call drift_alloc_array(elem_size, elem_align, len=0, cap=count)
		len_const = 0
		cap_const = count
		tmp_alloc = self._fresh("arr")
		self.lines.append(
			f"  {tmp_alloc} = call ptr @drift_alloc_array({self._llty(DRIFT_USIZE_TYPE)} {elem_size}, {self._llty(DRIFT_USIZE_TYPE)} {elem_align}, {self._llty(DRIFT_INT_TYPE)} {len_const}, {self._llty(DRIFT_INT_TYPE)} {cap_const})"
		)
		tmp_data = tmp_alloc
		# Build the array struct {len=0, cap, gen=0, data}, then set len after init.
		tmp0 = self._fresh("arrh0")
		tmp1 = self._fresh("arrh1")
		self.lines.append(f"  {tmp0} = insertvalue {arr_llty} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_LEN_IDX}")
		self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, {self._llty(DRIFT_INT_TYPE)} {cap_const}, {ARRAY_CAP_IDX}")
		tmp2 = self._fresh("arrh2")
		self.lines.append(f"  {tmp2} = insertvalue {arr_llty} {tmp1}, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_GEN_IDX}")
		tmp3 = self._fresh("arrh3")
		self.lines.append(f"  {tmp3} = insertvalue {arr_llty} {tmp2}, ptr {tmp_alloc}, {ARRAY_PTR_IDX}")
		# Store elements
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: ArrayLit requires a TypeTable")
		td = self.type_table.get(instr.elem_ty)
		copy_status = self.type_table.copy_status(instr.elem_ty)
		if copy_status is None:
			raise AssertionError("internal: unresolved Copy status for ArrayLit element type in codegen")
		if not copy_status and td.kind is not TypeKind.VOID:
				raise AssertionError("internal: non-Copy ArrayLit reached codegen")
		is_bool = self._is_bool_type(instr.elem_ty)
		needs_string_copy = td.kind is TypeKind.SCALAR and td.name == "String"
		for idx, elem in enumerate(instr.elements):
			elem_val = self._map_value(elem)
			elem_val = self._coerce_value_to_typeid(elem, elem_val, instr.elem_ty, context="array literal element")
			if needs_string_copy:
				# copy-construction: array-literal element ownership stake.
				elem_val = self._emit_copy_value(instr.elem_ty, elem_val)
			if is_bool:
				elem_val = self._bool_to_storage(elem_val)
			tmp_ptr = self._fresh("eltptr")
			self.lines.append(
				f"  {tmp_ptr} = getelementptr inbounds {elem_llty}, ptr {tmp_data}, {self._llty(DRIFT_INT_TYPE)} {idx}"
			)
			self.lines.append(f"  store {elem_llty} {elem_val}, ptr {tmp_ptr}")
		self.lines.append(f"  {dest} = insertvalue {arr_llty} {tmp3}, {self._llty(DRIFT_INT_TYPE)} {count}, {ARRAY_LEN_IDX}")
		self.value_types[dest] = arr_llty

	def _lower_array_alloc(self, instr: ArrayAlloc) -> None:
		"""Lower ArrayAlloc by allocating a backing store and building the header struct."""
		dest = self._map_value(instr.dest)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		elem_size, elem_align = self._array_elem_layout(instr.elem_ty, elem_llty)
		len_val = self._map_value(instr.length)
		len_const = self.const_values.get(len_val)
		if len_const is None or len_const != 0:
			raise AssertionError("LLVM codegen v1: ArrayAlloc length must be constant zero")
		cap_val = self._map_value(instr.cap)
		self.module.needs_array_helpers = True
		# MVP invariant: ArrayAlloc always returns len=0; callers must set len via ArraySetLen.
		tmp_alloc = self._fresh("arr")
		zero_len = self._fresh("len_zero")
		self.lines.append(f"  {zero_len} = add {self._llty(DRIFT_INT_TYPE)} 0, 0")
		self.lines.append(
			f"  {tmp_alloc} = call ptr @drift_alloc_array({self._llty(DRIFT_USIZE_TYPE)} {elem_size}, {self._llty(DRIFT_USIZE_TYPE)} {elem_align}, {self._llty(DRIFT_INT_TYPE)} {zero_len}, {self._llty(DRIFT_INT_TYPE)} {cap_val})"
		)
		tmp_data = tmp_alloc
		tmp0 = self._fresh("arrh0")
		tmp1 = self._fresh("arrh1")
		self.lines.append(f"  {tmp0} = insertvalue {arr_llty} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} {zero_len}, {ARRAY_LEN_IDX}")
		self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, {self._llty(DRIFT_INT_TYPE)} {cap_val}, {ARRAY_CAP_IDX}")
		tmp2 = self._fresh("arrh2")
		self.lines.append(f"  {tmp2} = insertvalue {arr_llty} {tmp1}, {self._llty(DRIFT_INT_TYPE)} 0, {ARRAY_GEN_IDX}")
		self.lines.append(f"  {dest} = insertvalue {arr_llty} {tmp2}, ptr {tmp_alloc}, {ARRAY_PTR_IDX}")
		self.value_types[dest] = arr_llty

	def _lower_array_elem_init(self, instr: ArrayElemInit) -> None:
		"""Lower ArrayElemInit as a direct store into the backing buffer."""
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		value = self._map_value(instr.value)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		value = self._coerce_value_to_typeid(instr.value, value, instr.elem_ty, context="array element")
		if self._is_bool_type(instr.elem_ty):
			value = self._bool_to_storage(value)
		line = f"  store {elem_llty} {value}, ptr {ptr_tmp}"
		if self.module.debug_enabled:
			loc_id = self._dbg_location_for_span(getattr(instr, "span", None))
			if loc_id is not None:
				line = f"{line}, !dbg !{loc_id}"
		self.lines.append(line)

	def _lower_array_elem_init_unchecked(self, instr: ArrayElemInitUnchecked) -> None:
		"""Lower ArrayElemInitUnchecked without bounds checks."""
		if drift_debug.enabled("dbg_array_span") and getattr(self.func.fn_id, "module", None) == "main":
			import sys
			print(f"[drift:debug][dbg_array_span] fn={self.func.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		value = self._map_value(instr.value)
		idx_ty = self.value_types.get(index)
		if idx_ty != DRIFT_INT_TYPE:
			raise AssertionError("LLVM codegen v1: ArrayElemInitUnchecked index must be Int")
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		data_tmp = self._fresh("data")
		self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {array}, {ARRAY_PTR_IDX}")
		data_ptr = data_tmp
		ptr_tmp = self._fresh("eltptr")
		self.lines.append(
			f"  {ptr_tmp} = getelementptr inbounds {elem_llty}, ptr {data_ptr}, {self._llty(DRIFT_INT_TYPE)} {index}"
		)
		value = self._coerce_value_to_typeid(instr.value, value, instr.elem_ty, context="array element")
		if self._is_bool_type(instr.elem_ty):
			value = self._bool_to_storage(value)
		line = f"  store {elem_llty} {value}, ptr {ptr_tmp}"
		if self.module.debug_enabled:
			loc_id = self._dbg_location_for_span(getattr(instr, "span", None))
			if loc_id is not None:
				line = f"{line}, !dbg !{loc_id}"
		self.lines.append(line)

	def _lower_array_elem_assign(self, instr: ArrayElemAssign) -> None:
		"""Lower ArrayElemAssign by dropping the old element then storing the new one."""
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		value = self._map_value(instr.value)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		if self._type_needs_drop(instr.elem_ty):
			old_val = self._fresh("old")
			self.lines.append(f"  {old_val} = load {elem_llty}, ptr {ptr_tmp}")
			self.value_types[old_val] = elem_llty
			self._emit_drop_value(instr.elem_ty, old_val)
		value = self._coerce_value_to_typeid(instr.value, value, instr.elem_ty, context="array element")
		if self._is_bool_type(instr.elem_ty):
			value = self._bool_to_storage(value)
		line = f"  store {elem_llty} {value}, ptr {ptr_tmp}"
		if self.module.debug_enabled:
			loc_id = self._dbg_location_for_span(getattr(instr, "span", None))
			if loc_id is not None:
				line = f"{line}, !dbg !{loc_id}"
		self.lines.append(line)

	def _lower_array_elem_drop(self, instr: ArrayElemDrop) -> None:
		"""Lower ArrayElemDrop by loading and dropping the element."""
		if not self._type_needs_drop(instr.elem_ty):
			return
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		old_val = self._fresh("old")
		self.lines.append(f"  {old_val} = load {elem_llty}, ptr {ptr_tmp}")
		self.value_types[old_val] = elem_llty
		self._emit_drop_value(instr.elem_ty, old_val)

	def _lower_array_elem_take(self, instr: ArrayElemTake) -> None:
		"""Lower ArrayElemTake with bounds checks and a load from data[idx]."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		elem_val_llty = self._llvm_type_for_typeid(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		raw = dest
		if self._is_bool_type(instr.elem_ty):
			raw = self._fresh("bool_byte")
		self.lines.append(f"  {raw} = load {elem_llty}, ptr {ptr_tmp}")
		if self._is_bool_type(instr.elem_ty):
			self._bool_from_storage(raw, dest=dest)
			self.value_types[dest] = "i1"
		else:
			self.value_types[dest] = elem_val_llty
		if self._type_needs_drop(instr.elem_ty):
			tomb = self._emit_tombstone_value(instr.elem_ty)
			self.lines.append(f"  store {elem_llty} {tomb}, ptr {ptr_tmp}")

	def _lower_array_drop(self, instr: ArrayDrop) -> None:
		"""Lower ArrayDrop by dropping elements and freeing the backing store."""
		array = self._map_value(instr.array)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		len_tmp = self._fresh("len")
		data_tmp = self._fresh("data")
		self.lines.append(f"  {len_tmp} = extractvalue {arr_llty} {array}, {ARRAY_LEN_IDX}")
		self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {array}, {ARRAY_PTR_IDX}")
		if self._type_needs_drop(instr.elem_ty):
			data_ptr = data_tmp
			helper = self._ensure_array_drop_helper(instr.elem_ty)
			self.lines.append(f"  call void @{helper}({self._llty(DRIFT_INT_TYPE)} {len_tmp}, ptr {data_ptr})")
		self.module.needs_array_helpers = True
		self.lines.append(f"  call void @drift_free_array(ptr {data_tmp})")

	def _lower_array_dup(self, instr: ArrayDup) -> None:
		"""Lower ArrayDup by allocating a new buffer and copying elements."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		result = self._emit_array_dup_value(instr.elem_ty, array, dest_hint=dest)
		if result != dest:
			self.value_map[instr.dest] = result
			if result in self.value_types:
				self.value_types[dest] = self.value_types[result]

	def _emit_array_dup_value(self, elem_ty_id: "TypeId", array: str, dest_hint: str | None = None) -> str:
		"""Emit an array duplication returning the new array header value."""
		dest = dest_hint if dest_hint is not None else self._fresh("arr_dup")
		elem_llty = self._llvm_array_elem_type(elem_ty_id)
		arr_llty = self._llvm_array_header_type()
		elem_size, elem_align = self._array_elem_layout(elem_ty_id, elem_llty)
		self.module.needs_array_helpers = True
		bitcopy = True
		if self.type_table is not None:
			bitcopy = self.type_table.is_bitcopy(elem_ty_id)
		# Extract len, cap, gen, data
		len_tmp = self._fresh("len")
		cap_tmp = self._fresh("cap")
		gen_tmp = self._fresh("gen")
		data_tmp = self._fresh("data")
		self.lines.append(f"  {len_tmp} = extractvalue {arr_llty} {array}, {ARRAY_LEN_IDX}")
		self.lines.append(f"  {cap_tmp} = extractvalue {arr_llty} {array}, {ARRAY_CAP_IDX}")
		self.lines.append(f"  {gen_tmp} = extractvalue {arr_llty} {array}, {ARRAY_GEN_IDX}")
		self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {array}, {ARRAY_PTR_IDX}")
		data_ptr = data_tmp
		# Allocate backing store (preserve capacity)
		tmp_alloc = self._fresh("arr")
		self.lines.append(
			f"  {tmp_alloc} = call ptr @drift_alloc_array({self._llty(DRIFT_USIZE_TYPE)} {elem_size}, {self._llty(DRIFT_USIZE_TYPE)} {elem_align}, {self._llty(DRIFT_INT_TYPE)} {len_tmp}, {self._llty(DRIFT_INT_TYPE)} {cap_tmp})"
		)
		tmp_data = tmp_alloc
		if bitcopy:
			self.module.needs_memcpy = True
			# memcpy bytes = len * elem_size (skip when len == 0)
			len_is_zero = self._fresh("len_is_zero")
			self.lines.append(f"  {len_is_zero} = icmp eq {self._llty(DRIFT_INT_TYPE)} {len_tmp}, 0")
			zero_block = self._fresh("arr_dup_zero")
			copy_block = self._fresh("arr_dup_copy")
			after_block = self._fresh("arr_dup_done")
			self.lines.append(f"  br i1 {len_is_zero}, label {zero_block}, label {copy_block}")
			self.lines.append(f"{zero_block[1:]}:")
			self.lines.append(f"  br label {after_block}")
			self.lines.append(f"{copy_block[1:]}:")
			bytes_tmp = self._fresh("bytes")
			self.lines.append(f"  {bytes_tmp} = mul {self._llty(DRIFT_INT_TYPE)} {len_tmp}, {elem_size}")
			bytes_i64 = bytes_tmp
			if self.module.word_bits != 64:
				bytes_i64 = self._fresh("bytes_i64")
				self.lines.append(f"  {bytes_i64} = zext {self._llty(DRIFT_INT_TYPE)} {bytes_tmp} to i64")
			src_i8 = data_ptr
			dst_i8 = tmp_data
			self.lines.append(
				f"  call void @llvm.memcpy.p0.p0.i64(ptr {dst_i8}, ptr {src_i8}, i64 {bytes_i64}, i1 false)"
			)
			self.lines.append(f"  br label {after_block}")
			self.lines.append(f"{after_block[1:]}:")
			if self._current_effective_block is not None:
				self._current_effective_block = after_block[1:]
		else:
			# Element-wise copy for non-bitcopy Copy types.
			idx_ptr = self._scratch_alloca(self._llty(DRIFT_INT_TYPE), "idx_ptr")
			self.lines.append(f"  store {self._llty(DRIFT_INT_TYPE)} 0, ptr {idx_ptr}")
			cond_block = self._fresh("arr_dup_cond")
			body_block = self._fresh("arr_dup_body")
			done_block = self._fresh("arr_dup_done")
			self.lines.append(f"  br label {cond_block}")
			self.lines.append(f"{cond_block[1:]}:")
			idx_val = self._fresh("idx")
			self.lines.append(f"  {idx_val} = load {self._llty(DRIFT_INT_TYPE)}, ptr {idx_ptr}")
			cmp = self._fresh("idx_ok")
			self.lines.append(f"  {cmp} = icmp slt {self._llty(DRIFT_INT_TYPE)} {idx_val}, {len_tmp}")
			self.lines.append(f"  br i1 {cmp}, label {body_block}, label {done_block}")
			self.lines.append(f"{body_block[1:]}:")
			idx_val2 = self._fresh("idxv")
			self.lines.append(f"  {idx_val2} = load {self._llty(DRIFT_INT_TYPE)}, ptr {idx_ptr}")
			src_ptr = self._fresh("src_ptr")
			dst_ptr = self._fresh("dst_ptr")
			self.lines.append(
				f"  {src_ptr} = getelementptr inbounds {elem_llty}, ptr {data_ptr}, {self._llty(DRIFT_INT_TYPE)} {idx_val2}"
			)
			self.lines.append(
				f"  {dst_ptr} = getelementptr inbounds {elem_llty}, ptr {tmp_data}, {self._llty(DRIFT_INT_TYPE)} {idx_val2}"
			)
			src_val = self._fresh("src_val")
			self.lines.append(f"  {src_val} = load {elem_llty}, ptr {src_ptr}")
			# copy-construction: array dup copies each element into the new buffer.
			copied_val = self._emit_copy_value(elem_ty_id, src_val)
			self.lines.append(f"  store {elem_llty} {copied_val}, ptr {dst_ptr}")
			next_val = self._fresh("idx_next")
			self.lines.append(f"  {next_val} = add {self._llty(DRIFT_INT_TYPE)} {idx_val2}, 1")
			self.lines.append(f"  store {self._llty(DRIFT_INT_TYPE)} {next_val}, ptr {idx_ptr}")
			self.lines.append(f"  br label {cond_block}")
			self.lines.append(f"{done_block[1:]}:")
			if self._current_effective_block is not None:
				self._current_effective_block = done_block[1:]
		# Build the array struct {len, cap, gen, data}
		tmp0 = self._fresh("arrh0")
		tmp1 = self._fresh("arrh1")
		self.lines.append(f"  {tmp0} = insertvalue {arr_llty} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} {len_tmp}, {ARRAY_LEN_IDX}")
		self.lines.append(f"  {tmp1} = insertvalue {arr_llty} {tmp0}, {self._llty(DRIFT_INT_TYPE)} {cap_tmp}, {ARRAY_CAP_IDX}")
		tmp2 = self._fresh("arrh2")
		self.lines.append(f"  {tmp2} = insertvalue {arr_llty} {tmp1}, {self._llty(DRIFT_INT_TYPE)} {gen_tmp}, {ARRAY_GEN_IDX}")
		self.lines.append(f"  {dest} = insertvalue {arr_llty} {tmp2}, ptr {tmp_alloc}, {ARRAY_PTR_IDX}")
		self.value_types[dest] = arr_llty
		return dest

	_copy_visiting: set | None = None

	def _emit_copy_value(self, ty_id: TypeId, value: str, dest_hint: str | None = None) -> str:
		"""
		Emit a semantic copy of a value, falling back to bitcopy when allowed.
		"""
		if self.type_table is None:
			raise AssertionError("CopyValue requires a TypeTable")
		if self.type_table.is_bitcopy(ty_id):
			return value
		# Cycle detection for self-referential types (e.g. struct Foo(items: Array<Foo>)).
		# When a cycle is detected, delegate to a standalone clone helper function
		# that handles the recursion via normal function calls.
		if self._copy_visiting is None:
			self._copy_visiting = set()
		if ty_id in self._copy_visiting:
			helper = self._ensure_clone_helper(ty_id)
			llty = self._llvm_type_for_typeid(ty_id)
			out = dest_hint if dest_hint is not None else self._fresh("clone")
			self.lines.append(f"  {out} = call {llty} @{helper}({llty} {value})")
			self.value_types[out] = llty
			return out
		self._copy_visiting.add(ty_id)
		try:
			return self._emit_copy_value_inner(ty_id, value, dest_hint)
		finally:
			self._copy_visiting.discard(ty_id)

	def _emit_copy_value_inner(self, ty_id: TypeId, value: str, dest_hint: str | None = None) -> str:
		td = self.type_table.get(ty_id)
		llty = self._llvm_type_for_typeid(ty_id)
		if td.kind is TypeKind.SCALAR and td.name == "String":
			if self.module is not None:
				self.module.needs_string_retain = True
			out = dest_hint if dest_hint is not None else self._fresh("str_retain")
			self.lines.append(f"  {out} = call {DRIFT_STRING_TYPE} @drift_string_retain({DRIFT_STRING_TYPE} {value})")
			self.value_types[out] = DRIFT_STRING_TYPE
			return out
		if td.kind is TypeKind.ARRAY and td.param_types:
			return self._emit_array_dup_value(td.param_types[0], value, dest_hint=dest_hint)
		if td.kind is TypeKind.VARIANT:
			inst = self.type_table.get_variant_instance(ty_id)
			if inst is None:
				raise NotImplementedError("LLVM codegen v1: variant copy requires instance metadata")
			field_types_by_ctor = {arm.name: arm.field_types for arm in inst.arms}
			layout = self._variant_layout(ty_id)
			variant_llty = layout.llvm_ty
			tag_val = self._fresh("var_tag")
			self.lines.append(f"  {tag_val} = extractvalue {variant_llty} {value}, 0")
			result_ptr = self._scratch_alloca(variant_llty, "var_copy")
			done_block = self._fresh("var_done")
			arms = list(layout.arms)
			default_block = self._fresh("var_bad")
			arm_blocks: list[tuple[str, _VariantArmLayout]] = []
			for ctor_name, arm_layout in arms:
				arm_blocks.append((self._fresh(f"var_arm_{ctor_name.lower()}"), arm_layout))
			case_specs = " ".join(
				f"i8 {arm_layout.tag}, label {arm_block}" for (arm_block, arm_layout) in arm_blocks
			)
			self.lines.append(f"  switch i8 {tag_val}, label {default_block} [ {case_specs} ]")
			for (arm_block, arm_layout), (ctor_name, _arm_layout) in zip(arm_blocks, arms):
				self.lines.append(f"{arm_block[1:]}:")
				# Extract payload fields for this arm.
				tmp_ptr = self._scratch_alloca(variant_llty, "variant")
				self.lines.append(f"  store {variant_llty} {value}, ptr {tmp_ptr}")
				args: list[str] = []
				if arm_layout.payload_struct_llty:
					payload_words_ptr = self._fresh("payload_words")
					self.lines.append(
						f"  {payload_words_ptr} = getelementptr inbounds {variant_llty}, ptr {tmp_ptr}, i32 0, i32 2"
					)
					payload_struct_ptr = payload_words_ptr
					for fidx, (want_llty, store_llty) in enumerate(
						zip(arm_layout.field_lltys, arm_layout.field_storage_lltys)
					):
						field_ptr = self._fresh("fieldptr")
						self.lines.append(
							f"  {field_ptr} = getelementptr inbounds {arm_layout.payload_struct_llty}, ptr {payload_struct_ptr}, i32 0, i32 {fidx}"
						)
						if self._is_bool_storage_pair(value_llty=want_llty, storage_llty=store_llty):
							raw = self._fresh("field_byte")
							self.lines.append(f"  {raw} = load i8, ptr {field_ptr}")
							field_val = self._fresh("field")
							self.lines.append(f"  {field_val} = icmp ne i8 {raw}, 0")
							self.value_types[field_val] = "i1"
						else:
							field_val = self._fresh("field")
							emit_want_llty = self._llty(want_llty)
							self.lines.append(f"  {field_val} = load {emit_want_llty}, ptr {field_ptr}")
							self.value_types[field_val] = want_llty
						field_ty = field_types_by_ctor.get(ctor_name, [])[fidx]
						# copy-construction: recursive per-field copy inside the copy machinery.
						copied = self._emit_copy_value(field_ty, field_val)
						args.append(copied)
				copied_val = self._emit_variant_value(ty_id, ctor_name, args)
				self.lines.append(f"  store {variant_llty} {copied_val}, ptr {result_ptr}")
				self.lines.append(f"  br label {done_block}")
			# Default: unreachable tag
			self.lines.append(f"{default_block[1:]}:")
			if self.module is not None:
				self.module.needs_llvm_trap = True
			self.lines.append("  call void @llvm.trap()")
			self.lines.append("  unreachable")
			self.lines.append(f"{done_block[1:]}:")
			if self._current_effective_block is not None:
				self._current_effective_block = done_block[1:]
			out = self._fresh("var_out")
			self.lines.append(f"  {out} = load {variant_llty}, ptr {result_ptr}")
			self.value_types[out] = variant_llty
			return out
		if td.kind is TypeKind.STRUCT:
			inst = self.type_table.get_struct_instance(ty_id)
			if inst is None:
				raise NotImplementedError("LLVM codegen v1: struct copy requires instance metadata")
			current = "zeroinitializer"
			for idx, field_ty in enumerate(inst.field_types):
				field_val_llty = self._llvm_type_for_typeid(field_ty)
				field_store_llty = self._llvm_field_storage_type_for_typeid(field_ty)
				field_raw = self._fresh("copy_field_raw")
				self.lines.append(f"  {field_raw} = extractvalue {llty} {value}, {idx}")
				self.value_types[field_raw] = field_store_llty
				if self._is_bool_storage_pair(value_llty=field_val_llty, storage_llty=field_store_llty):
					field_val = self._bool_from_storage(field_raw)
				else:
					field_val = field_raw
				# copy-construction: recursive per-field copy inside the copy machinery.
				copied = self._emit_copy_value(field_ty, field_val)
				store_val = copied
				if self._is_bool_storage_pair(value_llty=field_val_llty, storage_llty=field_store_llty):
					store_val = self._bool_to_storage(copied)
				tmp = self._fresh("copy_ins")
				emit_field_store_llty = self._llty(field_store_llty)
				self.lines.append(f"  {tmp} = insertvalue {llty} {current}, {emit_field_store_llty} {store_val}, {idx}")
				self.value_types[tmp] = llty
				current = tmp
			return current
		if td.kind is TypeKind.FNRESULT:
			raise NotImplementedError("LLVM codegen v1: CopyValue on FnResult is invalid (FnResult is not Copy)")
		raise NotImplementedError(f"LLVM codegen v1: copy not supported for {td.kind.name}")

	def _ensure_clone_helper(self, ty_id: TypeId) -> str:
		"""Generate a standalone recursive clone function for a type.
		Used for self-referential types that cannot be inline-copied."""
		key = self._type_key(ty_id)
		name = f"__drift_clone_{key}"
		if name in self.module.clone_helpers:
			return name
		# Register BEFORE generating body to break recursion:
		# the body may call _ensure_clone_helper for the same type,
		# which will find the name already registered.
		self.module.clone_helpers[name] = name
		llty = self._llvm_type_for_typeid(ty_id)
		emit_llty = self._llty(llty)
		td = self.type_table.get(ty_id)

		lines: list[str] = []
		entry_allocas: list[str] = []
		tmp_counter = 0

		def fresh(prefix: str) -> str:
			nonlocal tmp_counter
			tmp_counter += 1
			return f"%{prefix}{tmp_counter}"

		def scratch_alloca(alloc_llty: str, prefix: str) -> str:
			# Owning-site entry registration (same contract as
			# _FuncBuilder._scratch_alloca): transient, fully re-stored
			# before use, address never escapes the emitting sequence.
			nm = fresh(prefix)
			entry_allocas.append(f"  {nm} = alloca {alloc_llty}")
			return nm

		def emit_clone(inner_ty: TypeId, val: str) -> str:
			"""Emit a clone of val, returning the cloned SSA value."""
			if self.type_table.is_bitcopy(inner_ty):
				return val
			inner_td = self.type_table.get(inner_ty)
			inner_llty = self._llvm_type_for_typeid(inner_ty)
			emit_inner_llty = self._llty(inner_llty)
			if inner_td.kind is TypeKind.SCALAR and inner_td.name == "String":
				self.module.needs_string_retain = True
				out = fresh("str_retain")
				lines.append(f"  {out} = call {DRIFT_STRING_TYPE} @drift_string_retain({DRIFT_STRING_TYPE} {val})")
				return out
			# For types that already have (or are being generated as) a clone
			# helper, emit a call to break the recursion.
			inner_key = self._type_key(inner_ty)
			inner_helper_name = f"__drift_clone_{inner_key}"
			if inner_helper_name in self.module.clone_helpers:
				out = fresh("clone")
				lines.append(f"  {out} = call {emit_inner_llty} @{inner_helper_name}({emit_inner_llty} {val})")
				return out
			if inner_td.kind is TypeKind.ARRAY and inner_td.param_types:
				return emit_array_clone(inner_ty, inner_td.param_types[0], val)
			if inner_td.kind is TypeKind.STRUCT:
				return emit_struct_clone(inner_ty, val)
			# For other non-bitcopy types, delegate to a clone helper.
			helper = self._ensure_clone_helper(inner_ty)
			out = fresh("clone")
			lines.append(f"  {out} = call {emit_inner_llty} @{helper}({emit_inner_llty} {val})")
			return out

		def emit_struct_clone(struct_ty: TypeId, val: str) -> str:
			struct_llty = self._llvm_type_for_typeid(struct_ty)
			inst = self.type_table.get_struct_instance(struct_ty)
			if inst is None:
				return val
			current = "zeroinitializer"
			for idx, field_ty in enumerate(inst.field_types):
				field_val_llty = self._llvm_type_for_typeid(field_ty)
				field_store_llty = self._llvm_field_storage_type_for_typeid(field_ty)
				raw = fresh("f")
				lines.append(f"  {raw} = extractvalue {struct_llty} {val}, {idx}")
				if self._is_bool_storage_pair(value_llty=field_val_llty, storage_llty=field_store_llty):
					bval = fresh("fb")
					lines.append(f"  {bval} = icmp ne i8 {raw}, 0")
					copied = emit_clone(field_ty, bval)
					store_val = fresh("bs")
					lines.append(f"  {store_val} = zext i1 {copied} to i8")
				else:
					copied = emit_clone(field_ty, raw)
					store_val = copied
				emit_store_llty = self._llty(field_store_llty)
				ins = fresh("ins")
				lines.append(f"  {ins} = insertvalue {struct_llty} {current}, {emit_store_llty} {store_val}, {idx}")
				current = ins
			return current

		def emit_array_clone(arr_ty: TypeId, elem_ty: TypeId, val: str) -> str:
			arr_llty = self._llvm_array_header_type()
			elem_llty = self._llvm_array_elem_type(elem_ty)
			elem_size, elem_align = self._array_elem_layout(elem_ty, elem_llty)
			self.module.needs_array_helpers = True

			len_v = fresh("len")
			cap_v = fresh("cap")
			gen_v = fresh("gen")
			data_v = fresh("data")
			lines.append(f"  {len_v} = extractvalue {arr_llty} {val}, {ARRAY_LEN_IDX}")
			lines.append(f"  {cap_v} = extractvalue {arr_llty} {val}, {ARRAY_CAP_IDX}")
			lines.append(f"  {gen_v} = extractvalue {arr_llty} {val}, {ARRAY_GEN_IDX}")
			lines.append(f"  {data_v} = extractvalue {arr_llty} {val}, {ARRAY_PTR_IDX}")
			src_ptr = data_v

			alloc = fresh("alloc")
			lines.append(
				f"  {alloc} = call ptr @drift_alloc_array("
				f"{self._llty(DRIFT_USIZE_TYPE)} {elem_size}, "
				f"{self._llty(DRIFT_USIZE_TYPE)} {elem_align}, "
				f"{self._llty(DRIFT_INT_TYPE)} {len_v}, "
				f"{self._llty(DRIFT_INT_TYPE)} {cap_v})"
			)
			dst_base = alloc

			bitcopy = self.type_table.is_bitcopy(elem_ty)
			if bitcopy:
				self.module.needs_memcpy = True
				is_zero = fresh("is_zero")
				lines.append(f"  {is_zero} = icmp eq {self._llty(DRIFT_INT_TYPE)} {len_v}, 0")
				zero_lbl = fresh("zero")
				copy_lbl = fresh("copy")
				done_lbl = fresh("done")
				lines.append(f"  br i1 {is_zero}, label {zero_lbl}, label {copy_lbl}")
				lines.append(f"{zero_lbl[1:]}:")
				lines.append(f"  br label {done_lbl}")
				lines.append(f"{copy_lbl[1:]}:")
				nbytes = fresh("nbytes")
				lines.append(f"  {nbytes} = mul {self._llty(DRIFT_INT_TYPE)} {len_v}, {elem_size}")
				lines.append(f"  call void @llvm.memcpy.p0.p0.i64(ptr {dst_base}, ptr {src_ptr}, i64 {nbytes}, i1 false)")
				lines.append(f"  br label {done_lbl}")
				lines.append(f"{done_lbl[1:]}:")
			else:
				idx_ptr = scratch_alloca(self._llty(DRIFT_INT_TYPE), "idx_ptr")
				lines.append(f"  store {self._llty(DRIFT_INT_TYPE)} 0, ptr {idx_ptr}")
				cond_lbl = fresh("cond")
				body_lbl = fresh("body")
				done_lbl = fresh("done")
				lines.append(f"  br label {cond_lbl}")
				lines.append(f"{cond_lbl[1:]}:")
				iv = fresh("iv")
				lines.append(f"  {iv} = load {self._llty(DRIFT_INT_TYPE)}, ptr {idx_ptr}")
				cmp = fresh("cmp")
				lines.append(f"  {cmp} = icmp slt {self._llty(DRIFT_INT_TYPE)} {iv}, {len_v}")
				lines.append(f"  br i1 {cmp}, label {body_lbl}, label {done_lbl}")
				lines.append(f"{body_lbl[1:]}:")
				iv2 = fresh("iv2")
				lines.append(f"  {iv2} = load {self._llty(DRIFT_INT_TYPE)}, ptr {idx_ptr}")
				sp = fresh("sp")
				dp = fresh("dp")
				lines.append(f"  {sp} = getelementptr inbounds {elem_llty}, ptr {src_ptr}, {self._llty(DRIFT_INT_TYPE)} {iv2}")
				lines.append(f"  {dp} = getelementptr inbounds {elem_llty}, ptr {dst_base}, {self._llty(DRIFT_INT_TYPE)} {iv2}")
				sv = fresh("sv")
				lines.append(f"  {sv} = load {elem_llty}, ptr {sp}")
				cv = emit_clone(elem_ty, sv)
				lines.append(f"  store {elem_llty} {cv}, ptr {dp}")
				nv = fresh("nv")
				lines.append(f"  {nv} = add {self._llty(DRIFT_INT_TYPE)} {iv2}, 1")
				lines.append(f"  store {self._llty(DRIFT_INT_TYPE)} {nv}, ptr {idx_ptr}")
				lines.append(f"  br label {cond_lbl}")
				lines.append(f"{done_lbl[1:]}:")

			h0 = fresh("h")
			h1 = fresh("h")
			h2 = fresh("h")
			out = fresh("arr_out")
			lines.append(f"  {h0} = insertvalue {arr_llty} zeroinitializer, {self._llty(DRIFT_INT_TYPE)} {len_v}, {ARRAY_LEN_IDX}")
			lines.append(f"  {h1} = insertvalue {arr_llty} {h0}, {self._llty(DRIFT_INT_TYPE)} {cap_v}, {ARRAY_CAP_IDX}")
			lines.append(f"  {h2} = insertvalue {arr_llty} {h1}, {self._llty(DRIFT_INT_TYPE)} {gen_v}, {ARRAY_GEN_IDX}")
			lines.append(f"  {out} = insertvalue {arr_llty} {h2}, ptr {alloc}, {ARRAY_PTR_IDX}")
			return out

		def emit_variant_clone(var_ty: TypeId, val: str) -> str:
			inst = self.type_table.get_variant_instance(var_ty)
			if inst is None:
				return val
			layout = self._variant_layout(var_ty)
			variant_llty_str = layout.llvm_ty
			field_types_by_ctor = {arm.name: arm.field_types for arm in inst.arms}
			tag_v = fresh("tag")
			lines.append(f"  {tag_v} = extractvalue {variant_llty_str} {val}, 0")
			result_ptr = scratch_alloca(variant_llty_str, "vptr")
			done_lbl = fresh("vdone")
			default_lbl = fresh("vbad")
			arm_info: list[tuple[str, str, object]] = []  # (label, ctor_name, arm_layout)
			for ctor_name, arm_layout in layout.arms:
				lbl = fresh(f"varm_{ctor_name.lower()}")
				arm_info.append((lbl, ctor_name, arm_layout))
			case_specs = " ".join(
				f"i8 {al.tag}, label {lbl}" for lbl, _, al in arm_info
			)
			lines.append(f"  switch i8 {tag_v}, label {default_lbl} [ {case_specs} ]")
			for lbl, ctor_name, arm_layout in arm_info:
				lines.append(f"{lbl[1:]}:")
				tmp_ptr = scratch_alloca(variant_llty_str, "vtmp")
				lines.append(f"  store {variant_llty_str} {val}, ptr {tmp_ptr}")
				args: list[str] = []
				if arm_layout.payload_struct_llty:
					pw_ptr = fresh("pw")
					lines.append(
						f"  {pw_ptr} = getelementptr inbounds {variant_llty_str}, ptr {tmp_ptr}, i32 0, i32 2"
					)
					ps_ptr = pw_ptr
					for fidx, (want_llty, store_llty) in enumerate(
						zip(arm_layout.field_lltys, arm_layout.field_storage_lltys)
					):
						fp = fresh("fp")
						lines.append(
							f"  {fp} = getelementptr inbounds {arm_layout.payload_struct_llty}, ptr {ps_ptr}, i32 0, i32 {fidx}"
						)
						if self._is_bool_storage_pair(value_llty=want_llty, storage_llty=store_llty):
							raw8 = fresh("f8")
							lines.append(f"  {raw8} = load i8, ptr {fp}")
							fv = fresh("fb")
							lines.append(f"  {fv} = icmp ne i8 {raw8}, 0")
							copied = emit_clone(field_types_by_ctor.get(ctor_name, [])[fidx], fv)
							sv = fresh("bs")
							lines.append(f"  {sv} = zext i1 {copied} to i8")
							args.append(sv)
						else:
							ewl = self._llty(want_llty)
							fv = fresh("fv")
							lines.append(f"  {fv} = load {ewl}, ptr {fp}")
							field_ty = field_types_by_ctor.get(ctor_name, [])[fidx]
							copied = emit_clone(field_ty, fv)
							args.append(copied)
				# Reconstruct variant value for this arm.
				# Use alloca + store approach (same as _emit_variant_value).
				out_ptr = scratch_alloca(variant_llty_str, "optr")
				lines.append(f"  store {variant_llty_str} zeroinitializer, ptr {out_ptr}")
				tag_p = fresh("tp")
				lines.append(f"  {tag_p} = getelementptr inbounds {variant_llty_str}, ptr {out_ptr}, i32 0, i32 0")
				lines.append(f"  store i8 {arm_layout.tag}, ptr {tag_p}")
				if arm_layout.field_storage_lltys and args:
					opw = fresh("opw")
					lines.append(f"  {opw} = getelementptr inbounds {variant_llty_str}, ptr {out_ptr}, i32 0, i32 2")
					ops = opw
					for aidx, (arg_val, store_llty) in enumerate(
						zip(args, arm_layout.field_storage_lltys)
					):
						afp = fresh("afp")
						lines.append(f"  {afp} = getelementptr inbounds {arm_layout.payload_struct_llty}, ptr {ops}, i32 0, i32 {aidx}")
						lines.append(f"  store {store_llty} {arg_val}, ptr {afp}")
				ov = fresh("ov")
				lines.append(f"  {ov} = load {variant_llty_str}, ptr {out_ptr}")
				lines.append(f"  store {variant_llty_str} {ov}, ptr {result_ptr}")
				lines.append(f"  br label {done_lbl}")
			# Default: unreachable tag
			lines.append(f"{default_lbl[1:]}:")
			self.module.needs_llvm_trap = True
			lines.append("  call void @llvm.trap()")
			lines.append("  unreachable")
			lines.append(f"{done_lbl[1:]}:")
			out = fresh("vout")
			lines.append(f"  {out} = load {variant_llty_str}, ptr {result_ptr}")
			return out

		# Generate the function body based on type kind.
		lines.append(f"define private {emit_llty} @{name}({emit_llty} %src) {{")
		lines.append("__bb_entry:")
		if td.kind is TypeKind.STRUCT:
			result = emit_struct_clone(ty_id, "%src")
		elif td.kind is TypeKind.ARRAY and td.param_types:
			result = emit_array_clone(ty_id, td.param_types[0], "%src")
		elif td.kind is TypeKind.VARIANT:
			result = emit_variant_clone(ty_id, "%src")
		else:
			raise NotImplementedError(
				f"LLVM codegen v1: clone helper not supported for {td.kind.name}"
			)
		lines.append(f"  ret {emit_llty} {result}")
		lines.append("}")
		if entry_allocas:
			define_idx = next(i for i, l in enumerate(lines) if l.startswith("define "))
			insert_at = define_idx + 1
			# If the body opens with an explicit entry label (this
			# generator emits __bb_entry:), allocas go AFTER it —
			# instructions before the first label are invalid IR.
			if insert_at < len(lines) and lines[insert_at].endswith(":") and not lines[insert_at].startswith(" "):
				insert_at += 1
			lines[insert_at:insert_at] = entry_allocas
		self.module.emit_func("\n".join(lines))
		return name

	def _type_needs_drop(self, ty_id: TypeId) -> bool:
		if self.type_table is None:
			raise AssertionError("drop requires a TypeTable")
		ty_id = self._resolve_forward_nominal_typeid(ty_id)
		cached = self._drop_cache.get(ty_id)
		if cached is not None:
			return cached
		# Check destructor_fns first — authoritative for Destructible impls
		# registered via module exports (has_drop may miss user-defined impls
		# because the trait prover's _destructible_query can fail to resolve
		# them, and has_drop's own cache may be stale from pre-codegen
		# phases where destructor_fns wasn't installed yet).
		destructor_fns = getattr(self.type_table, "destructor_fns", None)
		if isinstance(destructor_fns, dict) and destructor_fns.get(ty_id) is not None:
			self._drop_cache[ty_id] = True
			return True
		if hasattr(self.type_table, "has_drop"):
			needs = bool(self.type_table.has_drop(ty_id))
			self._drop_cache[ty_id] = needs
			return needs
		td = self.type_table.get(ty_id)
		if td.kind is TypeKind.SCALAR:
			needs = td.name == "String"
			self._drop_cache[ty_id] = needs
			return needs
		self._drop_cache[ty_id] = False
		return False

	def _emit_drop_value(self, ty_id: TypeId, value: str) -> None:
		if self.type_table is None:
			raise AssertionError("drop requires a TypeTable")
		ty_id = self._resolve_forward_nominal_typeid(ty_id)
		if not self._type_needs_drop(ty_id):
			return
		call_dbg_suffix = ""
		if self.module.debug_enabled and self._dbg_subprogram_id is not None:
			loc_id = self._dbg_location_for_span(self._dbg_last_span or self._dbg_default_span)
			if loc_id is not None:
				call_dbg_suffix = f", !dbg !{loc_id}"
		destructor_fns = getattr(self.type_table, "destructor_fns", None)
		if isinstance(destructor_fns, dict):
			fn_id = destructor_fns.get(ty_id)
			if fn_id is not None:
				if getattr(self.func, "fn_id", None) == fn_id:
					# Inside destroy(): self's scope-drop lands here.
					# Drop ALL owned fields using the LIVE post-mutation
					# value (read from self at scope exit).
					#
					# We call _emit_drop_value (real destroy) on each
					# field so that nested Destructible fields (e.g.
					# VirtualThread, MutexGuard) execute their destroy()
					# side effects.  Container wrappers (HashSetCore,
					# TreeSet) must delegate cleanup to their inner
					# Destructible field's destroy — not manually dealloc.
					llty = self._llvm_type_for_typeid(ty_id)
					td_self = self.type_table.get(ty_id)
					if td_self.kind is TypeKind.STRUCT:
						inst = self.type_table.get_struct_instance(ty_id)
						if inst is not None:
							for idx, field_ty in enumerate(inst.field_types):
								if not self._type_needs_drop(field_ty):
									continue
								field_llty = self._llvm_type_for_typeid(field_ty)
								field_val = self._fresh("drop_field")
								self.lines.append(f"  {field_val} = extractvalue {llty} {value}, {idx}")
								self.value_types[field_val] = field_llty
								self._emit_drop_value(field_ty, field_val)
					return
				else:
					# Caller-side: just call destroy().  Field cleanup
					# happens inside destroy's own scope-exit drops (the
					# branch above).  We must NOT extractvalue from
					# {value} — it is the pre-call SSA snapshot and may
					# hold freed pointers (stale-SSA UAF).
					llty = self._llvm_type_for_typeid(ty_id)
					sym = function_symbol(fn_id)
					self.lines.append(f"  call void {_llvm_fn_sym(sym)}({llty} {value}){call_dbg_suffix}")
					return
		td = self.type_table.get(ty_id)
		llty = self._llvm_type_for_typeid(ty_id)
		if td.kind is TypeKind.SCALAR and td.name == "String":
			self.module.needs_string_release = True
			self.lines.append(f"  call void @drift_string_release({DRIFT_STRING_TYPE} {value}){call_dbg_suffix}")
			return
		if td.kind is TypeKind.ERROR:
			self.module.needs_error_runtime = True
			self.lines.append(f"  call void @drift_error_release({DRIFT_ERROR_PTR} {value}){call_dbg_suffix}")
			return
		if td.kind is TypeKind.ARRAY and td.param_types:
			elem_ty = td.param_types[0]
			elem_llty = self._llvm_array_elem_type(elem_ty)
			arr_llty = self._llvm_array_header_type()
			len_tmp = self._fresh("len")
			data_tmp = self._fresh("data")
			self.lines.append(f"  {len_tmp} = extractvalue {arr_llty} {value}, {ARRAY_LEN_IDX}")
			self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {value}, {ARRAY_PTR_IDX}")
			if self._type_needs_drop(elem_ty):
				data_ptr = data_tmp
				helper = self._ensure_array_drop_helper(elem_ty)
				self.lines.append(f"  call void @{helper}({self._llty(DRIFT_INT_TYPE)} {len_tmp}, ptr {data_ptr}){call_dbg_suffix}")
			self.module.needs_array_helpers = True
			self.lines.append(f"  call void @drift_free_array(ptr {data_tmp}){call_dbg_suffix}")
			return
		if td.kind is TypeKind.INTERFACE:
			iface_llty = self._llty(DRIFT_IFACE_TYPE)
			helper = self._ensure_interface_drop_helper()
			self.lines.append(f"  call void @{helper}({iface_llty} {value}){call_dbg_suffix}")
			return
		if td.kind is TypeKind.STRUCT:
			inst = self.type_table.get_struct_instance(ty_id)
			if inst is None:
				return
			for idx, field_ty in enumerate(inst.field_types):
				if not self._type_needs_drop(field_ty):
					continue
				field_llty = self._llvm_type_for_typeid(field_ty)
				field_val = self._fresh("drop_field")
				self.lines.append(f"  {field_val} = extractvalue {llty} {value}, {idx}")
				self.value_types[field_val] = field_llty
				self._emit_drop_value(field_ty, field_val)
			return
		if td.kind is TypeKind.VARIANT:
			layout = self._variant_layout(ty_id)
			variant_llty = layout.llvm_ty
			# Single-value drops go through the by-value alwaysinline
			# variant helper (no caller alloca/store, no outlined loop):
			# LLVM folds the tag switch wherever the tag is known, so
			# inactive destructible arms cost nothing on the active path.
			helper = self._ensure_variant_drop_helper(ty_id)
			self.lines.append(f"  call void @{helper}({variant_llty} {value}){call_dbg_suffix}")
			return

	def _ensure_variant_drop_helper(self, ty_id: TypeId) -> str:
		"""Single-VALUE variant drop: `define internal void
		@__drift_variant_drop_<key>(<llty> %v) alwaysinline` — the same
		per-arm release logic as the array element drop, WITHOUT the
		element loop, the caller-side alloca/store, or an outlined call
		boundary.  `alwaysinline` lets LLVM fold the tag switch on
		paths where the tag is known (e.g. a match arm's own edge), so
		a Result::Ok(Copy) drop costs NOTHING — the authority-level
		enum/match cleanup optimization (2026-07-25 review directive):
		inactive destructible arms (Err carrying a String) no longer
		tax the active tag's path, while runtime-unknown tags keep the
		exact same per-arm releases (drop exactly once)."""
		return self._ensure_array_drop_helper(ty_id, single_value=True)

	def _ensure_array_drop_helper(self, elem_ty: TypeId, single_value: bool = False) -> str:
		if self.type_table is None:
			raise AssertionError("array drop helper requires a TypeTable")
		elem_ty = self._resolve_forward_nominal_typeid(elem_ty)
		key = self._type_key(elem_ty)
		name = f"__drift_variant_drop_{key}" if single_value else f"__drift_array_drop_{key}"
		if name in self.module.array_drop_helpers:
			return name
		self.module.array_drop_helpers[name] = name
		elem_llty = self._llvm_array_elem_type(elem_ty)
		lines: list[str] = []
		value_types: dict[str, str] = {}
		entry_allocas: list[str] = []
		tmp_counter = 0

		def fresh(prefix: str) -> str:
			nonlocal tmp_counter
			tmp_counter += 1
			return f"%{prefix}{tmp_counter}"

		def scratch_alloca(llty: str, prefix: str) -> str:
			# Same owning-site contract as _FuncBuilder._scratch_alloca:
			# transient, fully re-stored before use, address never
			# escapes the emitting sequence — placed in the helper's
			# ENTRY so inlining never introduces dynamic allocas into
			# callers.
			name = fresh(prefix)
			entry_allocas.append(f"  {name} = alloca {llty}")
			return name

		def emit_drop(ty_id: TypeId, val: str) -> None:
			ty_id = self._resolve_forward_nominal_typeid(ty_id)
			td = self.type_table.get(ty_id)
			llty = self._llvm_type_for_typeid(ty_id)
			destructor_fns = getattr(self.type_table, "destructor_fns", None)
			if isinstance(destructor_fns, dict):
				fn_id = destructor_fns.get(ty_id)
				if fn_id is not None:
					# Just call destroy(); field cleanup is handled inside
					# destroy's own scope drops.  Do NOT extractvalue from
					# {val} — it is the pre-call SSA snapshot (stale UAF).
					sym = function_symbol(fn_id)
					lines.append(f"  call void {_llvm_fn_sym(sym)}({llty} {val})")
					return
			if td.kind is TypeKind.SCALAR and td.name == "String":
				self.module.needs_string_release = True
				lines.append(f"  call void @drift_string_release({DRIFT_STRING_TYPE} {val})")
				return
			if td.kind is TypeKind.ERROR:
				self.module.needs_error_runtime = True
				lines.append(f"  call void @drift_error_release({DRIFT_ERROR_PTR} {val})")
				return
			if td.kind is TypeKind.INTERFACE:
				helper = self._ensure_interface_drop_helper()
				iface_llty = self._llty(DRIFT_IFACE_TYPE)
				lines.append(f"  call void @{helper}({iface_llty} {val})")
				return
			if td.kind is TypeKind.ARRAY and td.param_types:
				inner_elem = td.param_types[0]
				inner_llty = self._llvm_array_elem_type(inner_elem)
				inner_arr_llty = self._llvm_array_header_type()
				arr_len = fresh("len")
				arr_data = fresh("data")
				lines.append(f"  {arr_len} = extractvalue {inner_arr_llty} {val}, {ARRAY_LEN_IDX}")
				lines.append(f"  {arr_data} = extractvalue {inner_arr_llty} {val}, {ARRAY_PTR_IDX}")
				if self._type_needs_drop(inner_elem):
					arr_ptr = arr_data
					helper = self._ensure_array_drop_helper(inner_elem)
					lines.append(f"  call void @{helper}({self._llty(DRIFT_INT_TYPE)} {arr_len}, ptr {arr_ptr})")
				self.module.needs_array_helpers = True
				lines.append(f"  call void @drift_free_array(ptr {arr_data})")
				return
			if td.kind is TypeKind.STRUCT:
				inst = self.type_table.get_struct_instance(ty_id)
				if inst is None:
					return
				for idx, field_ty in enumerate(inst.field_types):
					if not self._type_needs_drop(field_ty):
						continue
					field_llty = self._llvm_type_for_typeid(field_ty)
					field_val = fresh("field")
					lines.append(f"  {field_val} = extractvalue {llty} {val}, {idx}")
					value_types[field_val] = field_llty
					emit_drop(field_ty, field_val)
				return
			if td.kind is TypeKind.VARIANT:
				inst = self.type_table.get_variant_instance(ty_id)
				if inst is None:
					return
				layout = self._variant_layout(ty_id)
				variant_llty = layout.llvm_ty
				tag_val = fresh("tag")
				lines.append(f"  {tag_val} = extractvalue {variant_llty} {val}, 0")
				done_block = fresh("drop_done")
				arms = list(layout.arms)
				default_block = fresh("drop_bad")
				arm_blocks: list[tuple[str, _VariantArmLayout, str]] = []
				for ctor_name, arm_layout in arms:
					arm_blocks.append((fresh(f"drop_{ctor_name.lower()}"), arm_layout, ctor_name))
				case_entries = [
					f"i8 {arm_layout.tag}, label {arm_block}"
					for (arm_block, arm_layout, _ctor_name) in arm_blocks
				]
				# Synthesized internal-tombstone tag (when the variant has no
				# user-declared `@tombstone` ctor but is droppable): route
				# directly to `done_block` so a drop of a tombstoned variant
				# is a provable no-op.  Without this, tombstoned storage
				# (e.g. from ArrayElemTake or the match-scrutinee
				# `TombstoneValue` store in `_ensure_arm_scrut_ptr`) would
				# hit the `default_block` trap on subsequent DropValue.
				internal_tomb_ctor = inst.internal_tombstone_ctor
				internal_tomb_tag = inst.internal_tombstone_tag
				if (
					internal_tomb_ctor == "__drift_internal_tombstone"
					and internal_tomb_tag is not None
					and all(arm_layout.tag != internal_tomb_tag for _b, arm_layout, _c in arm_blocks)
				):
					case_entries.append(f"i8 {internal_tomb_tag}, label {done_block}")
				case_specs = " ".join(case_entries)
				lines.append(f"  switch i8 {tag_val}, label {default_block} [ {case_specs} ]")
				for arm_block, arm_layout, ctor_name in arm_blocks:
					lines.append(f"{arm_block[1:]}:")
					if arm_layout.payload_struct_llty:
						tmp_ptr = scratch_alloca(variant_llty, "variant")
						lines.append(f"  store {variant_llty} {val}, ptr {tmp_ptr}")
						payload_words_ptr = fresh("payload_words")
						lines.append(
							f"  {payload_words_ptr} = getelementptr inbounds {variant_llty}, ptr {tmp_ptr}, i32 0, i32 2"
						)
						payload_struct_ptr = payload_words_ptr
						field_types = inst.arms_by_name[ctor_name].field_types
						for fidx, (want_llty, store_llty) in enumerate(
							zip(arm_layout.field_lltys, arm_layout.field_storage_lltys)
						):
							field_ty = field_types[fidx]
							if not self._type_needs_drop(field_ty):
								continue
							field_ptr = fresh("fieldptr")
							lines.append(
								f"  {field_ptr} = getelementptr inbounds {arm_layout.payload_struct_llty}, ptr {payload_struct_ptr}, i32 0, i32 {fidx}"
							)
							if self._is_bool_storage_pair(value_llty=want_llty, storage_llty=store_llty):
								raw = fresh("field8")
								lines.append(f"  {raw} = load i8, ptr {field_ptr}")
								field_val = fresh("field")
								lines.append(f"  {field_val} = icmp ne i8 {raw}, 0")
								value_types[field_val] = "i1"
							else:
								field_val = fresh("field")
								emit_want_llty = self._llty(want_llty)
								lines.append(f"  {field_val} = load {emit_want_llty}, ptr {field_ptr}")
								value_types[field_val] = want_llty
							emit_drop(field_ty, field_val)
					lines.append(f"  br label {done_block}")
				lines.append(f"{default_block[1:]}:")
				self.module.needs_llvm_trap = True
				lines.append("  call void @llvm.trap()")
				lines.append("  unreachable")
				lines.append(f"{done_block[1:]}:")
				return

		if single_value:
			sv_llty = self._llvm_type_for_typeid(elem_ty)
			lines.append(f"define internal void @{name}({sv_llty} %v) alwaysinline {{")
		else:
			lines.append(f"define void @{name}({self._llty(DRIFT_INT_TYPE)} %len, ptr %data) {{")
		if not self._type_needs_drop(elem_ty):
			lines.append("  ret void")
			lines.append("}")
			self.module.emit_func("\n".join(lines))
			return name
		if single_value:
			value_types["%v"] = self._llvm_type_for_typeid(elem_ty)
			emit_drop(elem_ty, "%v")
			lines.append("  ret void")
			lines.append("}")
			if entry_allocas:
				insert_at = 1
				if len(lines) > 1 and lines[1].endswith(":") and not lines[1].startswith(" "):
					insert_at = 2
				lines[insert_at:insert_at] = entry_allocas
			self.module.emit_func("\n".join(lines))
			return name
		idx_ptr = scratch_alloca(self._llty(DRIFT_INT_TYPE), "idx_ptr")
		lines.append(f"  store {self._llty(DRIFT_INT_TYPE)} 0, ptr {idx_ptr}")
		cond_block = fresh("arr_drop_cond")
		body_block = fresh("arr_drop_body")
		done_block = fresh("arr_drop_done")
		lines.append(f"  br label {cond_block}")
		lines.append(f"{cond_block[1:]}:")
		idx_val = fresh("idx")
		lines.append(f"  {idx_val} = load {self._llty(DRIFT_INT_TYPE)}, ptr {idx_ptr}")
		cmp = fresh("idx_ok")
		lines.append(f"  {cmp} = icmp slt {self._llty(DRIFT_INT_TYPE)} {idx_val}, %len")
		lines.append(f"  br i1 {cmp}, label {body_block}, label {done_block}")
		lines.append(f"{body_block[1:]}:")
		idx_val2 = fresh("idxv")
		lines.append(f"  {idx_val2} = load {self._llty(DRIFT_INT_TYPE)}, ptr {idx_ptr}")
		ptr_tmp = fresh("eltptr")
		lines.append(
			f"  {ptr_tmp} = getelementptr inbounds {elem_llty}, ptr %data, {self._llty(DRIFT_INT_TYPE)} {idx_val2}"
		)
		old_val = fresh("old")
		lines.append(f"  {old_val} = load {elem_llty}, ptr {ptr_tmp}")
		value_types[old_val] = elem_llty
		emit_drop(elem_ty, old_val)
		next_val = fresh("idx_next")
		lines.append(f"  {next_val} = add {self._llty(DRIFT_INT_TYPE)} {idx_val2}, 1")
		lines.append(f"  store {self._llty(DRIFT_INT_TYPE)} {next_val}, ptr {idx_ptr}")
		lines.append(f"  br label {cond_block}")
		lines.append(f"{done_block[1:]}:")
		lines.append("  ret void")
		lines.append("}")
		if entry_allocas:
			insert_at = 1
			if len(lines) > 1 and lines[1].endswith(":") and not lines[1].startswith(" "):
				insert_at = 2
			lines[insert_at:insert_at] = entry_allocas
		self.module.emit_func("\n".join(lines))
		return name

	# Slice 7c-2 (ABI 14, 2026-05-06): `_ensure_dv_drop_helper`
	# deleted along with the DV runtime exports it generated.

	def _ensure_interface_drop_helper(self) -> str:
		name = self.module.iface_drop_helper
		if name is not None:
			return name
		name = "__drift_iface_drop_helper"
		self.module.iface_drop_helper = name
		self.module.needs_iface_helpers = True
		iface_llty = self._llty(DRIFT_IFACE_TYPE)
		usize_llty = self._llty(DRIFT_USIZE_TYPE)
		inline_storage = f"[{DRIFT_IFACE_INLINE_WORDS} x {usize_llty}]"
		lines = [
			f"define void @{name}({iface_llty} %src) {{",
			"__bb_entry:",
			f"  %iface_tmp = alloca {iface_llty}",
			f"  %iface_data = extractvalue {iface_llty} %src, {DRIFT_IFACE_DATA_IDX}",
			f"  %iface_vtable = extractvalue {iface_llty} %src, {DRIFT_IFACE_VTABLE_IDX}",
			"  %iface_vtable_null = icmp eq ptr %iface_vtable, null",
			"  br i1 %iface_vtable_null, label %__bb_iface_free_done, label %__bb_iface_vtable_ok",
			"__bb_iface_vtable_ok:",
			f"  %iface_inline = extractvalue {iface_llty} %src, {DRIFT_IFACE_INLINE_FLAG_IDX}",
			# Borrowed view (bit 2): the value owns NOTHING — skip both the
			# payload drop thunk and the free. Only this compiler's own
			# helpers ever see the bit (borrowed views cannot escape their
			# constructing frame as owned values), so older artifacts need
			# no rebuild.
			f"  %iface_borrowed_bit = and i8 %iface_inline, {DRIFT_IFACE_FLAG_BORROWED}",
			"  %iface_is_borrowed = icmp ne i8 %iface_borrowed_bit, 0",
			"  br i1 %iface_is_borrowed, label %__bb_iface_free_done, label %__bb_iface_owned",
			"__bb_iface_owned:",
			"  %iface_inline_bit = and i8 %iface_inline, 1",
			"  %iface_owns_bit = and i8 %iface_inline, 2",
			"  %iface_is_inline = icmp ne i8 %iface_inline_bit, 0",

			f"  store {iface_llty} %src, ptr %iface_tmp",
			f"  %iface_inline_field = getelementptr inbounds {iface_llty}, ptr %iface_tmp, i32 0, i32 {DRIFT_IFACE_INLINE_IDX}",
			f"  %iface_inline_word = getelementptr inbounds {inline_storage}, ptr %iface_inline_field, i32 0, i32 0",
			"  %iface_data_eff = select i1 %iface_is_inline, ptr %iface_inline_word, ptr %iface_data",
			"  %iface_drop_slot = getelementptr inbounds ptr, ptr %iface_vtable, i32 0",
			"  %iface_drop_ptr = load ptr, ptr %iface_drop_slot",
			"  %iface_has_drop = icmp ne ptr %iface_drop_ptr, null",
			"  br i1 %iface_has_drop, label %__bb_iface_drop_call, label %__bb_iface_drop_done",
			"__bb_iface_drop_call:",
			"  call void (ptr) %iface_drop_ptr(ptr %iface_data_eff)",
			"  br label %__bb_iface_drop_done",
			"__bb_iface_drop_done:",
			"  %iface_needs_free = icmp ne i8 %iface_owns_bit, 0",
			"  br i1 %iface_needs_free, label %__bb_iface_free, label %__bb_iface_free_done",
			"__bb_iface_free:",
			"  call void @drift_iface_free(ptr %iface_data)",
			"  br label %__bb_iface_free_done",
			"__bb_iface_free_done:",
			"  ret void",
			"}",
		]
		self.module.emit_func("\n".join(lines))
		return name

	def _lower_array_index_load(self, instr: ArrayIndexLoad) -> None:
		"""Lower ArrayIndexLoad with bounds checks and a load from data[idx]."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		elem_val_llty = self._llvm_type_for_typeid(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		raw = dest
		if self._is_bool_type(instr.elem_ty):
			raw = self._fresh("bool_byte")
		self.lines.append(f"  {raw} = load {elem_llty}, ptr {ptr_tmp}")
		if self._is_bool_type(instr.elem_ty):
			self._bool_from_storage(raw, dest=dest)
			self.value_types[dest] = "i1"
		else:
			# owned-at-extraction: ArrayIndexLoad
			copied = self._emit_copy_value(instr.elem_ty, raw)
			if copied != raw:
				self.value_map[instr.dest] = copied
				self.value_types[copied] = elem_val_llty
			else:
				self.value_types[dest] = elem_val_llty

	def _lower_array_index_load_unchecked(self, instr: ArrayIndexLoadUnchecked) -> None:
		"""Lower ArrayIndexLoadUnchecked without bounds checks."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		elem_val_llty = self._llvm_type_for_typeid(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		ptr_tmp = self._lower_array_index_addr_unchecked(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		raw = dest
		if self._is_bool_type(instr.elem_ty):
			raw = self._fresh("bool_byte")
		self.lines.append(f"  {raw} = load {elem_llty}, ptr {ptr_tmp}")
		if self._is_bool_type(instr.elem_ty):
			self._bool_from_storage(raw, dest=dest)
			self.value_types[dest] = "i1"
		else:
			# owned-at-extraction: ArrayIndexLoadUnchecked
			copied = self._emit_copy_value(instr.elem_ty, raw)
			if copied != raw:
				self.value_map[instr.dest] = copied
				self.value_types[copied] = elem_val_llty
			else:
				self.value_types[dest] = elem_val_llty

	def _lower_array_index_store(self, instr: ArrayIndexStore) -> None:
		"""Lower ArrayIndexStore with bounds checks and a store into data[idx]."""
		array = self._map_value(instr.array)
		index = self._map_value(instr.index)
		value = self._map_value(instr.value)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		elem_val_llty = self._llvm_type_for_typeid(instr.elem_ty)
		arr_llty = self._llvm_array_header_type()
		ptr_tmp = self._lower_array_index_addr(array=array, index=index, elem_llty=elem_llty, arr_llty=arr_llty)
		if self._type_needs_drop(instr.elem_ty):
			old_val = self._fresh("old")
			self.lines.append(f"  {old_val} = load {elem_llty}, ptr {ptr_tmp}")
			self.value_types[old_val] = elem_val_llty
			self._emit_drop_value(instr.elem_ty, old_val)
		value = self._coerce_value_to_typeid(instr.value, value, instr.elem_ty, context="array element")
		if self._is_bool_type(instr.elem_ty):
			value = self._bool_to_storage(value)
		self.lines.append(f"  store {elem_llty} {value}, ptr {ptr_tmp}")
		# No dest; ArrayIndexStore returns void.

	def _lower_array_set_len(self, instr: ArraySetLen) -> None:
		"""Lower ArraySetLen by rebuilding the array header with a new len."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		length = self._map_value(instr.length)
		arr_llty = self._type_of(instr.array)
		if arr_llty is None:
			raise AssertionError("ArraySetLen requires array LLVM type")
		tmp0 = self._fresh("arr_len")
		self.lines.append(f"  {tmp0} = insertvalue {arr_llty} {array}, {self._llty(DRIFT_INT_TYPE)} {length}, {ARRAY_LEN_IDX}")
		self.value_map[instr.dest] = tmp0
		self.value_types[self._map_value(instr.dest)] = arr_llty

	def _lower_array_set_gen(self, instr: ArraySetGen) -> None:
		"""Lower ArraySetGen by rebuilding the array header with a new gen."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		gen = self._map_value(instr.gen)
		arr_llty = self._type_of(instr.array)
		if arr_llty is None:
			raise AssertionError("ArraySetGen requires array LLVM type")
		tmp0 = self._fresh("arr_gen")
		self.lines.append(f"  {tmp0} = insertvalue {arr_llty} {array}, {self._llty(DRIFT_INT_TYPE)} {gen}, {ARRAY_GEN_IDX}")
		self.value_map[instr.dest] = tmp0
		self.value_types[self._map_value(instr.dest)] = arr_llty

	def _lower_array_index_addr(self, *, array: str, index: str, elem_llty: str, arr_llty: str) -> str:
		"""
		Compute `&array[index]` with bounds checks and return an `{elem_llty}*`.

		This is used by:
		- ArrayIndexLoad/Store (to avoid duplicating pointer arithmetic), and
		- AddrOfArrayElem (borrow of array element).
		"""
		idx_ty = self.value_types.get(index)
		if idx_ty != DRIFT_INT_TYPE:
			raise NotImplementedError(
				f"LLVM codegen v1: array index must be Int, got {idx_ty}"
			)
		# Extract len and data
		arr_val = array
		arr_ty = self.value_types.get(array)
		if arr_ty is None:
			arr_ty = self.param_value_types.get(array)
		if arr_ty is not None and _is_ptr_type(arr_ty):
			loaded = self._fresh("arrval")
			self.lines.append(f"  {loaded} = load {arr_llty}, ptr {array}")
			self.value_types[loaded] = arr_llty
			arr_val = loaded
		len_tmp = self._fresh("len")
		data_tmp = self._fresh("data")
		self.lines.append(f"  {len_tmp} = extractvalue {arr_llty} {arr_val}, {ARRAY_LEN_IDX}")
		self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {arr_val}, {ARRAY_PTR_IDX}")
		self.module.needs_array_helpers = True
		container_id = self._emit_string_literal_value(ARRAY_CONTAINER_ID)
		self.lines.append(
			f"  call void @drift_bounds_check({DRIFT_STRING_TYPE} {container_id}, {self._llty(DRIFT_INT_TYPE)} {index}, {self._llty(DRIFT_INT_TYPE)} {len_tmp})"
		)
		idx_val = index
		ptr_tmp = self._fresh("eltptr")
		idx_llty = self._llty(DRIFT_INT_TYPE)
		self.lines.append(
			f"  {ptr_tmp} = getelementptr {elem_llty}, ptr {data_tmp}, {idx_llty} {idx_val}"
		)
		return ptr_tmp

	def _lower_array_index_addr_unchecked(self, *, array: str, index: str, elem_llty: str, arr_llty: str) -> str:
		"""
		Compute `&array[index]` without bounds checks and return an `{elem_llty}*`.
		"""
		idx_ty = self.value_types.get(index)
		if idx_ty != DRIFT_INT_TYPE:
			raise NotImplementedError(
				f"LLVM codegen v1: array index must be Int, got {idx_ty}"
			)
		arr_val = array
		arr_ty = self.value_types.get(array)
		if arr_ty is None:
			arr_ty = self.param_value_types.get(array)
		if arr_ty is not None and _is_ptr_type(arr_ty):
			loaded = self._fresh("arrval")
			self.lines.append(f"  {loaded} = load {arr_llty}, ptr {array}")
			self.value_types[loaded] = arr_llty
			arr_val = loaded
		len_tmp = self._fresh("len")
		data_tmp = self._fresh("data")
		self.lines.append(f"  {len_tmp} = extractvalue {arr_llty} {arr_val}, {ARRAY_LEN_IDX}")
		self.lines.append(f"  {data_tmp} = extractvalue {arr_llty} {arr_val}, {ARRAY_PTR_IDX}")
		idx_val = index
		ptr_tmp = self._fresh("eltptr")
		idx_llty = self._llty(DRIFT_INT_TYPE)
		self.lines.append(
			f"  {ptr_tmp} = getelementptr {elem_llty}, ptr {data_tmp}, {idx_llty} {idx_val}"
		)
		return ptr_tmp

	def _lower_array_len(self, instr: ArrayLen) -> None:
		"""Lower ArrayLen by extracting the len field (index 0)."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		arr_llty = self.value_types.get(array)
		if arr_llty is None:
			arr_llty = self.param_value_types.get(array)
		if arr_llty is None:
			raise AssertionError("LLVM codegen v1: ArrayLen missing LLVM type for array value (compiler bug)")
		if _is_ptr_type(arr_llty):
			real_ty = self._llvm_array_header_type()
			arr_val = self._fresh("arrval")
			self.lines.append(f"  {arr_val} = load {real_ty}, ptr {array}")
			self.value_types[arr_val] = real_ty
			array = arr_val
			arr_llty = real_ty
		# ArrayLen now applies only to array values; strings use StringLen MIR.
		self.lines.append(f"  {dest} = extractvalue {arr_llty} {array}, {ARRAY_LEN_IDX}")
		self.value_types[dest] = DRIFT_INT_TYPE

	def _lower_array_cap(self, instr: ArrayCap) -> None:
		"""Lower ArrayCap by extracting the cap field (index 1)."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		arr_llty = self.value_types.get(array)
		if arr_llty is None:
			arr_llty = self.param_value_types.get(array)
		if arr_llty is None:
			raise AssertionError("LLVM codegen v1: ArrayCap missing LLVM type for array value (compiler bug)")
		if _is_ptr_type(arr_llty):
			real_ty = self._llvm_array_header_type()
			arr_val = self._fresh("arrval")
			self.lines.append(f"  {arr_val} = load {real_ty}, ptr {array}")
			self.value_types[arr_val] = real_ty
			array = arr_val
			arr_llty = real_ty
		self.lines.append(f"  {dest} = extractvalue {arr_llty} {array}, {ARRAY_CAP_IDX}")
		self.value_types[dest] = DRIFT_INT_TYPE

	def _lower_array_gen(self, instr: ArrayGen) -> None:
		"""Lower ArrayGen by extracting the gen field (index 2)."""
		dest = self._map_value(instr.dest)
		array = self._map_value(instr.array)
		arr_llty = self.value_types.get(array)
		if arr_llty is None:
			arr_llty = self.param_value_types.get(array)
		if arr_llty is None:
			raise AssertionError("LLVM codegen v1: ArrayGen missing LLVM type for array value (compiler bug)")
		if _is_ptr_type(arr_llty):
			real_ty = self._llvm_array_header_type()
			arr_val = self._fresh("arrval")
			self.lines.append(f"  {arr_val} = load {real_ty}, ptr {array}")
			self.value_types[arr_val] = real_ty
			array = arr_val
			arr_llty = real_ty
		self.lines.append(f"  {dest} = extractvalue {arr_llty} {array}, {ARRAY_GEN_IDX}")
		self.value_types[dest] = DRIFT_INT_TYPE

	def _raw_buffer_value(self, raw_val: str, raw_llty: str) -> tuple[str, str]:
		buf_llty = self.value_types.get(raw_val, raw_llty)
		if _is_ptr_type(buf_llty):
			buf_val = self._fresh("rawbuf")
			self.lines.append(f"  {buf_val} = load {raw_llty}, ptr {raw_val}")
			self.value_types[buf_val] = raw_llty
			return buf_val, raw_llty
		return raw_val, buf_llty

	def _lower_raw_buffer_alloc(self, instr: RawBufferAlloc) -> None:
		dest = self._map_value(instr.dest)
		raw_llty = self._llvm_type_for_typeid(instr.raw_ty)
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		elem_size, elem_align = self._array_elem_layout(instr.elem_ty, elem_llty)
		cap_val = self._map_value(instr.cap)
		self.module.needs_array_helpers = True
		tmp_alloc = self._fresh("raw")
		zero_len = self._fresh("len_zero")
		self.lines.append(f"  {zero_len} = add {self._llty(DRIFT_INT_TYPE)} 0, 0")
		self.lines.append(
			f"  {tmp_alloc} = call ptr @drift_alloc_array({self._llty(DRIFT_USIZE_TYPE)} {elem_size}, {self._llty(DRIFT_USIZE_TYPE)} {elem_align}, {self._llty(DRIFT_INT_TYPE)} {zero_len}, {self._llty(DRIFT_INT_TYPE)} {cap_val})"
		)
		tmp0 = self._fresh("raw_a")
		tmp1 = self._fresh("raw_b")
		self.lines.append(f"  {tmp0} = insertvalue {raw_llty} zeroinitializer, ptr {tmp_alloc}, {RAWBUF_PTR_IDX}")
		self.lines.append(f"  {tmp1} = insertvalue {raw_llty} {tmp0}, {self._llty(DRIFT_INT_TYPE)} {cap_val}, {RAWBUF_CAP_IDX}")
		self.value_map[instr.dest] = tmp1
		self.value_types[tmp1] = raw_llty

	def _lower_raw_buffer_dealloc(self, instr: RawBufferDealloc) -> None:
		raw_val = self._map_value(instr.buffer)
		raw_llty = self._llvm_type_for_typeid(instr.raw_ty)
		buf_val, buf_llty = self._raw_buffer_value(raw_val, raw_llty)
		ptr_tmp = self._fresh("rawptr")
		self.module.needs_array_helpers = True
		self.lines.append(f"  {ptr_tmp} = extractvalue {buf_llty} {buf_val}, {RAWBUF_PTR_IDX}")
		self.lines.append(f"  call void @drift_free_array(ptr {ptr_tmp})")

	def _lower_raw_buffer_ptr_at(self, instr: RawBufferPtrAt) -> None:
		dest = self._map_value(instr.dest)
		raw_val = self._map_value(instr.buffer)
		raw_llty = self._llvm_type_for_typeid(instr.raw_ty)
		buf_val, buf_llty = self._raw_buffer_value(raw_val, raw_llty)
		ptr_tmp = self._fresh("rawptr")
		cap_tmp = self._fresh("rawcap")
		self.lines.append(f"  {ptr_tmp} = extractvalue {buf_llty} {buf_val}, {RAWBUF_PTR_IDX}")
		self.lines.append(f"  {cap_tmp} = extractvalue {buf_llty} {buf_val}, {RAWBUF_CAP_IDX}")
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		idx_val = self._map_value(instr.index)
		self.module.needs_array_helpers = True
		container_id = self._emit_string_literal_value(RAW_BUFFER_CONTAINER_ID)
		self.lines.append(
			f"  call void @drift_bounds_check({DRIFT_STRING_TYPE} {container_id}, {self._llty(DRIFT_INT_TYPE)} {idx_val}, {self._llty(DRIFT_INT_TYPE)} {cap_tmp})"
		)
		ptr_t = ptr_tmp
		self.lines.append(f"  {dest} = getelementptr {elem_llty}, ptr {ptr_t}, {self._llty(DRIFT_INT_TYPE)} {idx_val}")
		self.value_types[dest] = "ptr"

	def _lower_raw_buffer_write(self, instr: RawBufferWrite) -> None:
		raw_val = self._map_value(instr.buffer)
		raw_llty = self._llvm_type_for_typeid(instr.raw_ty)
		buf_val, buf_llty = self._raw_buffer_value(raw_val, raw_llty)
		ptr_tmp = self._fresh("rawptr")
		cap_tmp = self._fresh("rawcap")
		self.lines.append(f"  {ptr_tmp} = extractvalue {buf_llty} {buf_val}, {RAWBUF_PTR_IDX}")
		self.lines.append(f"  {cap_tmp} = extractvalue {buf_llty} {buf_val}, {RAWBUF_CAP_IDX}")
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		idx_val = self._map_value(instr.index)
		self.module.needs_array_helpers = True
		container_id = self._emit_string_literal_value(RAW_BUFFER_CONTAINER_ID)
		self.lines.append(
			f"  call void @drift_bounds_check({DRIFT_STRING_TYPE} {container_id}, {self._llty(DRIFT_INT_TYPE)} {idx_val}, {self._llty(DRIFT_INT_TYPE)} {cap_tmp})"
		)
		ptr_gep = self._fresh("rawgep")
		self.lines.append(f"  {ptr_gep} = getelementptr {elem_llty}, ptr {ptr_tmp}, {self._llty(DRIFT_INT_TYPE)} {idx_val}")
		value = self._map_value(instr.value)
		value = self._coerce_value_to_typeid(instr.value, value, instr.elem_ty, context="raw buffer element")
		if self._is_bool_type(instr.elem_ty):
			value = self._bool_to_storage(value)
		self.lines.append(f"  store {elem_llty} {value}, ptr {ptr_gep}")

	def _lower_raw_buffer_read(self, instr: RawBufferRead) -> None:
		dest = self._map_value(instr.dest)
		raw_val = self._map_value(instr.buffer)
		raw_llty = self._llvm_type_for_typeid(instr.raw_ty)
		buf_val, buf_llty = self._raw_buffer_value(raw_val, raw_llty)
		ptr_tmp = self._fresh("rawptr")
		cap_tmp = self._fresh("rawcap")
		self.lines.append(f"  {ptr_tmp} = extractvalue {buf_llty} {buf_val}, {RAWBUF_PTR_IDX}")
		self.lines.append(f"  {cap_tmp} = extractvalue {buf_llty} {buf_val}, {RAWBUF_CAP_IDX}")
		elem_llty = self._llvm_array_elem_type(instr.elem_ty)
		idx_val = self._map_value(instr.index)
		self.module.needs_array_helpers = True
		container_id = self._emit_string_literal_value(RAW_BUFFER_CONTAINER_ID)
		self.lines.append(
			f"  call void @drift_bounds_check({DRIFT_STRING_TYPE} {container_id}, {self._llty(DRIFT_INT_TYPE)} {idx_val}, {self._llty(DRIFT_INT_TYPE)} {cap_tmp})"
		)
		ptr_gep = self._fresh("rawgep")
		self.lines.append(f"  {ptr_gep} = getelementptr {elem_llty}, ptr {ptr_tmp}, {self._llty(DRIFT_INT_TYPE)} {idx_val}")
		raw_val = dest
		if self._is_bool_type(instr.elem_ty):
			raw_val = self._fresh("rawbool")
		self.lines.append(f"  {raw_val} = load {elem_llty}, ptr {ptr_gep}")
		if self._is_bool_type(instr.elem_ty):
			self._bool_from_storage(raw_val, dest=dest)
			self.value_types[dest] = "i1"
			return
		self.value_types[dest] = self._llvm_type_for_typeid(instr.elem_ty)

	def _lower_ptr_from_ref(self, instr: PtrFromRef) -> None:
		src_val = self._map_value(instr.src)
		dest = self._map_value(instr.dest)
		dest_llty = self._llvm_type_for_typeid(instr.ptr_ty)
		src_llty = self.value_types.get(src_val, dest_llty)
		if src_llty == dest_llty:
			# Opaque pointers: Ref and RawPtr are both ptr — identity.
			self.value_map[instr.dest] = src_val
		else:
			self.lines.append(f"  {dest} = bitcast {src_llty} {src_val} to {dest_llty}")
		self.value_types[dest] = dest_llty

	def _lower_ptr_offset(self, instr: PtrOffset) -> None:
		ptr_val = self._map_value(instr.ptr)
		offset_val = self._map_value(instr.offset)
		ptr_llty = self._llvm_type_for_typeid(instr.ptr_ty)
		elem_llty = self._emit_storage_type_for_typeid(instr.elem_ty)
		dest = self._map_value(instr.dest)
		self.lines.append(f"  {dest} = getelementptr {elem_llty}, ptr {ptr_val}, {self._llty(DRIFT_INT_TYPE)} {offset_val}")
		self.value_types[dest] = ptr_llty

	def _lower_ptr_read(self, instr: PtrRead) -> None:
		ptr_val = self._map_value(instr.ptr)
		elem_llty = self._emit_storage_type_for_typeid(instr.elem_ty)
		dest = self._map_value(instr.dest)
		raw_val = dest
		if self._is_bool_type(instr.elem_ty):
			raw_val = self._fresh("rawbool")
		self.lines.append(f"  {raw_val} = load {elem_llty}, ptr {ptr_val}")
		if self._is_bool_type(instr.elem_ty):
			self._bool_from_storage(raw_val, dest=dest)
			self.value_types[dest] = "i1"
			return
		self.value_types[dest] = self._llvm_type_for_typeid(instr.elem_ty)

	def _lower_ptr_write(self, instr: PtrWrite) -> None:
		ptr_val = self._map_value(instr.ptr)
		val_val = self._map_value(instr.value)
		elem_llty = self._emit_storage_type_for_typeid(instr.elem_ty)
		val_val = self._coerce_value_to_typeid(instr.value, val_val, instr.elem_ty, context="pointer write")
		if self._is_bool_type(instr.elem_ty):
			val_val = self._bool_to_storage(val_val)
		self.lines.append(f"  store {elem_llty} {val_val}, ptr {ptr_val}")

	def _lower_ptr_is_null(self, instr: PtrIsNull) -> None:
		ptr_val = self._map_value(instr.ptr)
		dest = self._map_value(instr.dest)
		ptr_llty = self._llvm_type_for_typeid(instr.ptr_ty)
		self.lines.append(f"  {dest} = icmp eq {ptr_llty} {ptr_val}, null")
		self.value_types[dest] = "i1"

	def _lower_ptr_as_mut_ref(self, instr: PtrAsMutRef) -> None:
		src_val = self._map_value(instr.src)
		dest = self._map_value(instr.dest)
		ref_llty = self._llvm_type_for_typeid(instr.ref_ty)
		src_llty = self.value_types.get(src_val, ref_llty)
		if src_llty == ref_llty:
			# Opaque pointers: RawPtr and Ref are both ptr — identity.
			self.value_map[instr.dest] = src_val
		else:
			self.lines.append(f"  {dest} = bitcast {src_llty} {src_val} to {ref_llty}")
		self.value_types[dest] = ref_llty

	def _llvm_array_header_type(self) -> str:
		return "%DriftArrayHeader"

	def _llvm_array_elem_type(self, elem_ty: int) -> str:
		"""
		Map an element TypeId to an LLVM type string.

		When a TypeTable is available, this supports all element types.
		"""
		if self.type_table is None:
			raise NotImplementedError("LLVM codegen v1: array lowering requires a TypeTable")
		return self._llty(self._llvm_storage_type_for_typeid(elem_ty))

	def _is_bool_type(self, ty_id: int) -> bool:
		if self.type_table is None:
			return False
		td = self.type_table.get(ty_id)
		return td.kind is TypeKind.SCALAR and td.name == "Bool"

	def _is_bool_storage_pair(self, *, value_llty: str, storage_llty: str) -> bool:
		return storage_llty == "i8" and value_llty == "i1"

	def _classify_payload_extract_transfer(self, ty_id: TypeId) -> str:
		"""
		Centralized ownership classification for by-value payload extraction.

		Returns:
		- "copy-bitcopy": plain load/bitcopy is sufficient
		- "copy-semantic": must run semantic copy/retain before source drop
		- "move": non-Copy payload (by-value extract path is invalid)
		- "unknown": unresolved type variable (contract violation if reached)
		"""
		if self.type_table is None:
			raise AssertionError("payload extract transfer classification requires TypeTable")
		copy_status = self.type_table.copy_status(ty_id)
		if copy_status is True:
			if self.type_table.is_bitcopy(ty_id):
				return "copy-bitcopy"
			if self._type_needs_drop(ty_id):
				return "copy-semantic"
			return "copy-bitcopy"
		if copy_status is False:
			return "move"
		td = self.type_table.get(ty_id)
		if td.kind is TypeKind.TYPEVAR:
			return "unknown"
		raise AssertionError("internal: unresolved Copy status at LLVM payload extract boundary")

	def _bool_to_storage(self, value: str) -> str:
		tmp = self._fresh("bool_byte")
		self.lines.append(f"  {tmp} = zext i1 {value} to i8")
		return tmp

	def _bool_from_storage(self, raw: str, *, dest: str | None = None) -> str:
		out = dest or self._fresh("bool")
		self.lines.append(f"  {out} = icmp ne i8 {raw}, 0")
		return out

	def _array_elem_layout(self, elem_ty: int, elem_llty: str) -> tuple[int, int]:
		if self.type_table is None:
			size = self._sizeof(elem_llty)
			if size == 0:
				raise AssertionError("LLVM codegen v1: Array<ZST> unsupported")
			return size, self._alignof(elem_llty)
		size, align = self._size_align_typeid(elem_ty)
		if size == 0:
			raise AssertionError("LLVM codegen v1: Array<ZST> unsupported")
		return size, align

	def _sizeof(self, elem_llty: str) -> int:
		# v1: isize/usize/pointers are word-sized; DriftString is two words.
		word_bytes = self.module.word_bits // 8
		if elem_llty in ("i1", "i8"):
			return 1
		if elem_llty == DRIFT_STRING_TYPE:
			return word_bytes * 2
		return word_bytes

	def _alignof(self, elem_llty: str) -> int:
		word_bytes = self.module.word_bits // 8
		if elem_llty in ("i1", "i8"):
			return 1
		# DriftString is two word-sized fields; align to pointer size.
		if elem_llty == DRIFT_STRING_TYPE:
			return word_bytes
		return word_bytes
