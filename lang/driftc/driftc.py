# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
lang driftc stub (checker/driver scaffolding).

This is **not** a full compiler. It exists to document how the lang pipeline
should be orchestrated once a real parser/type checker lands:

AST -> HIR (stage0/1)
   -> normalize_hir (stage1) for HIR normalization (no result-try sugar)
   -> HIR->MIR (stage2)
   -> MIR pre-analysis + throw summaries (stage3)
   -> throw checks (stage4) using `declared_can_throw` from the checker

When the real parser/checker is available, this file should grow proper CLI
handling and diagnostics. For now it exposes a single helper
`compile_stubbed_funcs` to drive the existing stages in tests or prototypes.
"""

from __future__ import annotations

import argparse
import copy
import heapq
import json
import os
import struct
from enum import Enum
import sys
import shutil
import subprocess
from ctypes.util import find_library
from collections import ChainMap
from types import MappingProxyType
from pathlib import Path
from dataclasses import replace, dataclass, fields, is_dataclass
from typing import Any, Dict, Mapping, List, Tuple, Callable
from lang.driftc import debug as drift_debug
from lang.driftc.env_flags import env_true as _env_true

# Repository root (lang lives under this).
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

_TEST_TARGET_WORD_BITS: int | None = None


def _target_word_bits(target_word_bits: int | None) -> int:
	"""Return the configured target word size in bits, falling back to host."""
	if target_word_bits is None:
		return struct.calcsize("P") * 8
	return target_word_bits


def _git_short_sha() -> str:
	"""Return the short git SHA of HEAD, or empty string on failure."""
	try:
		res = subprocess.run(
			["git", "rev-parse", "--short", "HEAD"],
			capture_output=True, text=True, cwd=ROOT, timeout=5,
		)
		if res.returncode == 0:
			return res.stdout.strip()
	except Exception:
		pass
	return ""


def _version_string() -> str:
	"""Build the driftc --version output."""
	from lang.driftc.driftc_versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
	git_sha = _git_short_sha()
	parts = [
		f"driftc {DRIFTC_VERSION}",
		f"abi {DRIFT_RT_ABI_VERSION}",
	]
	if git_sha:
		parts.append(f"git {git_sha}")
	parts.append("license GPL-3.0")
	parts.append("The Drift Language Foundation")
	return " | ".join(parts)


from lang.driftc import stage1 as H
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1 import normalize_hir
from lang.driftc.stage1 import closures as C
from lang.driftc.stage1.capture_discovery import discover_captures
from lang.driftc.stage1.closures import sort_captures
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, CallTargetKind, IntrinsicKind
from lang.driftc.call_contract import intrinsic_call_issues
from lang.driftc.stage1.lambda_validate import validate_lambdas_non_retaining
from lang.driftc.stage1.non_retaining_analysis import analyze_non_retaining_params
from lang.driftc.stage2 import HIRToMIR, make_builder, mir_nodes as M
from lang.driftc.stage2.string_arc import insert_string_arc
from lang.driftc.stage3.throw_summary import ThrowSummaryBuilder
from lang.driftc.stage4 import run_throw_checks
from lang.driftc.stage4 import MirToSSA
from lang.driftc.mir_validate import (
	validate_mir_array_alloc_invariants,
	validate_mir_array_copy_invariants,
	validate_mir_call_byvalue_moves,
	validate_mir_call_invariants,
	validate_mir_call_types,
	validate_mir_basic_hygiene,
	validate_mir_concrete_layout_types,
	validate_mir_iface_init_invariants,
	validate_mir_variant_field_invariants,
	validate_mir_wrapping_u64_invariants,
)
from lang.driftc.checker.type_env_builder import build_minimal_checker_type_env
from lang.driftc.checker import (
	Checker,
	CheckerInputsById,
	CheckedProgramById,
	FnSignature,
	FnInfo,
	make_fn_info,
	TypeParam,
)
from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.borrow_checker import PlaceBase, PlaceKind
from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import (
	TypeTable,
	TypeParamId,
	TypeKind,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.function_id import (
	FunctionId,
	function_id_from_obj,
	function_id_to_obj,
	function_symbol,
	method_wrapper_id,
)
from lang.driftc.traits.enforce import collect_used_type_keys, enforce_struct_requires, enforce_fn_requires
from lang.driftc.traits.linked_world import build_require_env, link_trait_worlds, LinkedWorld, RequireEnv
from lang.driftc.traits.world import TypeKey, TraitKey, type_key_from_typeid
from lang.driftc.traits.solver import Env as TraitEnv, ProofStatus, prove_is
from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.language_runtime import (
	build_runtime_archive,
	get_runtime_sources,
	runtime_archive_mode,
	runtime_archive_variant,
)
from lang.driftc.parser import parse_drift_to_hir, parse_drift_files_to_hir, parse_drift_workspace_to_hir
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.type_resolver import resolve_program_signatures
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.type_checker import TypeChecker, ThunkKind
from lang.driftc.method_registry import CallableRegistry, CallableSignature, CallableTemplateSignature, Visibility, SelfMode
from lang.driftc.impl_index import GlobalImplIndex, ImplMeta, find_impl_method_conflicts
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex, validate_trait_scopes
from lang.driftc.fake_decl import FakeDecl
from lang.driftc.packages.dmir_pkg_v0 import canonical_json_bytes, sha256_hex, write_dmir_pkg_v0
from lang.driftc.core.function_key import FunctionKey, function_key_from_obj, function_key_str
from lang.driftc.id_registry import IdRegistry
from lang.driftc.packages.provisional_dmir_v0 import (
	decode_mir_funcs,
	decode_generic_templates,
	decode_trait_expr,
	decode_type_expr,
	compute_template_decl_fingerprint,
	encode_generic_templates,
	encode_module_payload_v0,
	encode_span,
	encode_trait_expr,
	encode_type_expr,
	type_table_fingerprint,
)
from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps
from lang.driftc.packages.provider_v0 import (
	PackageTrustPolicy,
	collect_external_exports,
	discover_package_files,
	load_package_v0,
	load_package_v0_with_policy,
)
from lang.driftc.packages.trust_v0 import (
	TrustStore,
	load_core_trust_store,
	load_trust_store_json,
	merge_trust_stores,
)


def _remap_tid(tid_map: dict[int, int], tid: object) -> object:
	"""
	Remap a TypeId-like integer using `tid_map`.

	This helper is intentionally tiny and defensive. Only fields that are known
	to be TypeIds are remapped, so we don't accidentally rewrite non-TypeId ints
	(e.g., tag values or indices).
	"""
	if isinstance(tid, int):
		return tid_map.get(tid, tid)
	return tid


def _canonicalize_forward_nominal_type_id(
	type_table: TypeTable,
	ty_id: TypeId,
	*,
	_seen: set[tuple[str | None, str]] | None = None,
) -> TypeId:
	"""
	Best-effort canonicalization for zero-parameter aliases/forward nominals.

	This pass is boundary-facing hygiene: stage2/MIR may still carry forward
	nominals for alias names, but MIR validation/codegen require concrete layout
	types. We normalize recursively for wrapper shapes used at boundaries.
	"""
	td = type_table.get(ty_id)
	seen = _seen if _seen is not None else set()
	if td.kind is TypeKind.REF and td.param_types:
		inner = _canonicalize_forward_nominal_type_id(type_table, td.param_types[0], _seen=seen)
		if inner != td.param_types[0]:
			return type_table.ensure_ref_mut(inner) if td.ref_mut else type_table.ensure_ref(inner)
		return ty_id
	if td.kind is TypeKind.ARRAY and td.param_types:
		elem = _canonicalize_forward_nominal_type_id(type_table, td.param_types[0], _seen=seen)
		if elem != td.param_types[0]:
			return type_table.new_array(elem)
		return ty_id
	if td.kind is TypeKind.FNRESULT and len(td.param_types) == 2:
		ok_ty = _canonicalize_forward_nominal_type_id(type_table, td.param_types[0], _seen=seen)
		err_ty = _canonicalize_forward_nominal_type_id(type_table, td.param_types[1], _seen=seen)
		if ok_ty != td.param_types[0] or err_ty != td.param_types[1]:
			return type_table.new_fnresult(ok_ty, err_ty)
		return ty_id
	if td.kind is TypeKind.FUNCTION and td.param_types:
		new_params = [
			_canonicalize_forward_nominal_type_id(type_table, p, _seen=seen) for p in td.param_types[:-1]
		]
		new_ret = _canonicalize_forward_nominal_type_id(type_table, td.param_types[-1], _seen=seen)
		if new_params != td.param_types[:-1] or new_ret != td.param_types[-1]:
			return type_table.new_function(tuple(new_params), new_ret)
		return ty_id
	if td.kind is not TypeKind.FORWARD_NOMINAL:
		return ty_id
	alias_key = (td.module_id, td.name)
	if alias_key in seen:
		return ty_id
	alias_def = type_table.lookup_type_alias(module_id=td.module_id, name=td.name)
	if alias_def is not None:
		alias_params, alias_target, _loc = alias_def
		if not alias_params:
			resolved = resolve_opaque_type(
				alias_target,
				type_table,
				module_id=td.module_id,
				type_params=None,
				allow_generic_base=True,
			)
			if resolved != ty_id:
				return _canonicalize_forward_nominal_type_id(
					type_table, resolved, _seen=seen | {alias_key}
				)
	resolved_nom = (
		type_table.get_nominal(kind=TypeKind.STRUCT, module_id=td.module_id, name=td.name)
		or type_table.get_nominal(kind=TypeKind.VARIANT, module_id=td.module_id, name=td.name)
		or type_table.get_nominal(kind=TypeKind.INTERFACE, module_id=td.module_id, name=td.name)
	)
	return resolved_nom if resolved_nom is not None else ty_id


def _canonicalize_signature_type_ids(signatures_by_id: Mapping[FunctionId, FnSignature], type_table: TypeTable) -> None:
	for sig in signatures_by_id.values():
		sig.param_type_ids = [
			_canonicalize_forward_nominal_type_id(type_table, ty_id) for ty_id in sig.param_type_ids
		]
		sig.return_type_id = _canonicalize_forward_nominal_type_id(type_table, sig.return_type_id)


def _canonicalize_mir_type_ids(mir_funcs_by_id: Mapping[FunctionId, M.MirFunc], type_table: TypeTable) -> None:
	type_field_names = {"ty", "user_ret_type"}
	for func in mir_funcs_by_id.values():
		func.local_types = {
			name: _canonicalize_forward_nominal_type_id(type_table, ty_id)
			for name, ty_id in (getattr(func, "local_types", {}) or {}).items()
		}
		for block in func.blocks.values():
			for instr in block.instructions:
				for attr_name, attr_value in vars(instr).items():
					if isinstance(attr_value, int) and (attr_name in type_field_names or attr_name.endswith("_ty")):
						setattr(
							instr,
							attr_name,
							_canonicalize_forward_nominal_type_id(type_table, attr_value),
						)
					elif attr_name == "param_types" and isinstance(attr_value, list):
						setattr(
							instr,
							attr_name,
							[
								_canonicalize_forward_nominal_type_id(type_table, ty_id)
								for ty_id in attr_value
							],
						)


def _find_trait_key(world: "TraitWorld", *, module: str, name: str) -> TraitKey | None:
	keys = [key for key in world.traits.keys() if key.module == module and key.name == name]
	if not keys:
		return None
	keys.sort(key=lambda k: (k.package_id or "", k.module or "", k.name))
	return keys[0]


def _install_copy_query(type_table: TypeTable, linked_world: LinkedWorld) -> None:
	copy_key = _find_trait_key(linked_world.global_world, module="std.core", name="Copy")
	if copy_key is None:
		std_modules = {"std.core", "std.iter", "std.containers", "std.algo"}
		if any(mod in std_modules for mod in linked_world.trait_worlds.keys()):
			raise ValueError("stdlib missing std.core.Copy trait metadata")
		return
	default_package = getattr(type_table, "package_id", None)
	module_packages = getattr(type_table, "module_packages", None) or {}
	env = TraitEnv(
		default_module=None,
		default_package=default_package,
		module_packages=module_packages,
		type_table=type_table,
	)
	world = linked_world.global_world

	def _query_copy(tid: int) -> bool | None:
		td = type_table.get(tid)
		if td.kind in (TypeKind.TYPEVAR, TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL):
			return None
		if td.kind is TypeKind.REF:
			return False if td.ref_mut else True
		if td.kind is TypeKind.FUNCTION:
			return True
		try:
			subject = type_key_from_typeid(type_table, tid)
		except Exception:
			return None
		res = prove_is(world, env, {}, subject, copy_key)
		if res.status is ProofStatus.PROVED:
			return True
		if res.status is ProofStatus.REFUTED:
			if td.kind in (TypeKind.STRUCT, TypeKind.VARIANT):
				mod = td.module_id or ""
				if not (mod.startswith("std.") or mod.startswith("lang.")):
					return None
			return False
		return None

	type_table.set_copy_query(_query_copy, allow_fallback=False)


def _install_diagnostic_query(type_table: TypeTable, linked_world: LinkedWorld) -> None:
	diag_key = _find_trait_key(linked_world.global_world, module="std.core", name="Diagnostic")
	if diag_key is None:
		std_modules = {"std.core", "std.iter", "std.containers", "std.algo", "std.err"}
		if any(mod in std_modules for mod in linked_world.trait_worlds.keys()):
			raise ValueError("stdlib missing std.core.Diagnostic trait metadata")
		return
	default_package = getattr(type_table, "package_id", None)
	module_packages = getattr(type_table, "module_packages", None) or {}
	env = TraitEnv(
		default_module=None,
		default_package=default_package,
		module_packages=module_packages,
		type_table=type_table,
	)
	world = linked_world.global_world

	def _query_diag(tid: int) -> bool | None:
		try:
			subject = type_key_from_typeid(type_table, tid)
		except Exception:
			return None
		res = prove_is(world, env, {}, subject, diag_key)
		if res.status is ProofStatus.PROVED:
			return True
		if res.status is ProofStatus.REFUTED:
			return False
		return None

	type_table.set_diagnostic_query(_query_diag, allow_fallback=False)


def _install_destructible_query(type_table: TypeTable, linked_world: LinkedWorld) -> None:
	destructible_key = _find_trait_key(linked_world.global_world, module="std.core", name="Destructible")
	if destructible_key is None:
		std_modules = {"std.core"}
		if any(mod in std_modules for mod in linked_world.trait_worlds.keys()):
			def _fallback_destructible(_tid: int) -> bool | None:
				return None
			type_table.set_destructible_query(_fallback_destructible, allow_fallback=True)
		return
	default_package = getattr(type_table, "package_id", None)
	module_packages = getattr(type_table, "module_packages", None) or {}
	env = TraitEnv(
		default_module=None,
		default_package=default_package,
		module_packages=module_packages,
		type_table=type_table,
	)
	world = linked_world.global_world

	def _query_destructible(tid: int) -> bool | None:
		try:
			subject = type_key_from_typeid(type_table, tid)
		except Exception:
			return None
		res = prove_is(world, env, {}, subject, destructible_key)
		if res.status is ProofStatus.PROVED:
			return True
		if res.status is ProofStatus.REFUTED:
			return False
		return None

	type_table.set_destructible_query(_query_destructible, allow_fallback=False)


def _install_destructor_fns(
	type_table: TypeTable | None,
	linked_world: LinkedWorld | None,
	module_exports: Mapping[str, dict[str, object]] | None,
) -> None:
	if type_table is None or linked_world is None or module_exports is None:
		return
	destructible_key = _find_trait_key(linked_world.global_world, module="std.core", name="Destructible")
	if destructible_key is None:
		return
	destructor_fns: dict[TypeId, FunctionId] = {}
	for exp in module_exports.values():
		if not isinstance(exp, dict):
			continue
		impls = exp.get("impls")
		if not isinstance(impls, list):
			continue
		for impl in impls:
			if not isinstance(impl, ImplMeta):
				continue
			if impl.trait_key != destructible_key:
				continue
			target_type_id = getattr(impl, "target_type_id", None)
			if not isinstance(target_type_id, int):
				continue
			if type_table.has_typevar(target_type_id):
				continue
			for method in impl.methods:
				if method.name != "destroy":
					continue
				destructor_fns[target_type_id] = method.fn_id
	if destructor_fns:
		type_table.destructor_fns = destructor_fns


def _build_linked_world(type_table: TypeTable | None) -> tuple[LinkedWorld | None, RequireEnv | None]:
	trait_worlds = getattr(type_table, "trait_worlds", None) if type_table is not None else None
	if not isinstance(trait_worlds, dict):
		return None, None
	linked_world = link_trait_worlds(trait_worlds)
	if type_table is not None:
		_install_copy_query(type_table, linked_world)
		_install_diagnostic_query(type_table, linked_world)
		_install_destructible_query(type_table, linked_world)
	default_package = getattr(type_table, "package_id", None)
	module_packages = getattr(type_table, "module_packages", None)
	return linked_world, build_require_env(
		linked_world,
		default_package=default_package,
		module_packages=module_packages,
	)


def _remap_mir_func_typeids(fn: M.MirFunc, tid_map: dict[int, int]) -> None:
	"""
	Remap TypeId fields in a MirFunc in-place.

	Package payloads are produced independently, so their TypeIds must be mapped
	into the host link-time TypeTable before SSA/LLVM lowering.
	"""
	# Remap per-local type annotations (used by codegen for scope drops).
	for local_name in list(fn.local_types):
		old_ty = fn.local_types[local_name]
		fn.local_types[local_name] = int(_remap_tid(tid_map, old_ty))
	for block in fn.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.ZeroValue):
				instr.ty = int(_remap_tid(tid_map, instr.ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.CopyValue, M.DropValue, M.MoveOut)):
				instr.ty = int(_remap_tid(tid_map, instr.ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.AddrOfArrayElem, M.LoadRef, M.StoreRef)):
				instr.inner_ty = int(_remap_tid(tid_map, instr.inner_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.ConstructStruct):
				instr.struct_ty = int(_remap_tid(tid_map, instr.struct_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.ConstructVariant):
				instr.variant_ty = int(_remap_tid(tid_map, instr.variant_ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.VariantTag, M.VariantTagRef)):
				instr.variant_ty = int(_remap_tid(tid_map, instr.variant_ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.VariantGetField, M.VariantGetFieldAddr)):
				instr.variant_ty = int(_remap_tid(tid_map, instr.variant_ty))  # type: ignore[assignment]
				instr.field_ty = int(_remap_tid(tid_map, instr.field_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.StructGetField):
				instr.struct_ty = int(_remap_tid(tid_map, instr.struct_ty))  # type: ignore[assignment]
				instr.field_ty = int(_remap_tid(tid_map, instr.field_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.AddrOfField):
				instr.struct_ty = int(_remap_tid(tid_map, instr.struct_ty))  # type: ignore[assignment]
				instr.field_ty = int(_remap_tid(tid_map, instr.field_ty))  # type: ignore[assignment]
			elif isinstance(
				instr,
				(
					M.ArrayLit,
					M.ArrayAlloc,
					M.ArrayElemInit,
					M.ArrayElemInitUnchecked,
					M.ArrayElemAssign,
					M.ArrayElemDrop,
					M.ArrayElemTake,
					M.ArrayDrop,
					M.ArrayDup,
					M.ArrayIndexLoad,
					M.ArrayIndexStore,
					M.ArrayIndexLoadUnchecked,
					M.ConstArray,
				),
			):
				instr.elem_ty = int(_remap_tid(tid_map, instr.elem_ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.RawBufferAlloc, M.RawBufferPtrAt, M.RawBufferWrite, M.RawBufferRead)):
				instr.raw_ty = int(_remap_tid(tid_map, instr.raw_ty))  # type: ignore[assignment]
				instr.elem_ty = int(_remap_tid(tid_map, instr.elem_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.RawBufferDealloc):
				instr.raw_ty = int(_remap_tid(tid_map, instr.raw_ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.PtrFromRef, M.PtrIsNull)):
				instr.ptr_ty = int(_remap_tid(tid_map, instr.ptr_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.PtrOffset):
				instr.ptr_ty = int(_remap_tid(tid_map, instr.ptr_ty))  # type: ignore[assignment]
				instr.elem_ty = int(_remap_tid(tid_map, instr.elem_ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.PtrRead, M.PtrWrite)):
				instr.elem_ty = int(_remap_tid(tid_map, instr.elem_ty))  # type: ignore[assignment]


def _inject_prelude(
	signatures: dict[FunctionId, FnSignature],
	fn_ids_by_name: dict[str, list[FunctionId]],
	type_table: TypeTable,
) -> None:
	"""
	Value prelude injection is disabled. Console helpers live in `std.console`
	and must be imported explicitly.
	"""
	_ = signatures
	_ = fn_ids_by_name
	_ = type_table


def _prelude_exports() -> dict[str, object]:
	"""
	Return the external export surface for the built-in prelude module.

	This is used to allow explicit imports (e.g. `import lang.core as core`)
	even when implicit prelude injection is disabled.
	"""
	return {
		"values": [],
		"types": {"structs": [], "variants": [], "exceptions": [], "interfaces": []},
		"consts": [],
		"traits": [],
		"reexports": {
			"types": {"structs": {}, "variants": {}, "exceptions": {}, "interfaces": {}},
			"consts": {},
			"traits": {},
		},
	}


def _should_inject_prelude(
	prelude_enabled: bool,
	module_deps: Mapping[str, set[str]] | None,
) -> bool:
	"""
	Decide whether prelude signatures should be injected.

	Value prelude injection is disabled.
	"""
	_ = prelude_enabled
	_ = module_deps
	return False


@dataclass(frozen=True)
class MethodWrapperSpec:
	wrapper_fn_id: FunctionId
	target_fn_id: FunctionId


def _inject_method_boundary_wrappers(
	*,
	signatures_by_id: Mapping[FunctionId, FnSignature],
	existing_ids: set[FunctionId] | None = None,
	register_derived: Callable[[FunctionId, FnSignature], None],
	type_table: TypeTable,
) -> tuple[list[MethodWrapperSpec], list[str]]:
	"""
	Predeclare Ok-wrap wrappers for public NOTHROW methods.

	Wrappers are provider-emitted and recorded in signatures for package export.
	"""
	specs: list[MethodWrapperSpec] = []
	errors: list[str] = []
	if existing_ids is None:
		existing_ids = set(signatures_by_id.keys())
	for fn_id, sig in list(signatures_by_id.items()):
		if not getattr(sig, "is_method", False):
			continue
		if getattr(sig, "is_wrapper", False):
			continue
		if not getattr(sig, "is_pub", False):
			continue
		if getattr(sig, "declared_can_throw", None) is not False:
			continue
		if sig.param_type_ids is None or sig.return_type_id is None:
			errors.append(f"internal: missing param/return types for method '{sig.name}'")
			continue
		wrapper_id = method_wrapper_id(fn_id)
		if wrapper_id in existing_ids:
			continue
		wrap_sig = FnSignature(
			name=function_symbol(wrapper_id),
			module=fn_id.module,
			method_name=getattr(sig, "method_name", None) or sig.name,
			param_names=list(sig.param_names or []),
			param_type_ids=list(sig.param_type_ids or []),
			return_type_id=sig.return_type_id,
			error_type_id=type_table.ensure_error(),
			is_method=True,
			self_mode=getattr(sig, "self_mode", None),
			impl_target_type_id=getattr(sig, "impl_target_type_id", None),
			impl_target_type_args=getattr(sig, "impl_target_type_args", None),
			is_pub=True,
			is_wrapper=True,
			wraps_target_fn_id=fn_id,
			type_params=list(getattr(sig, "type_params", []) or []),
			impl_type_params=list(getattr(sig, "impl_type_params", []) or []),
			param_types=list(getattr(sig, "param_types", []) or []) if getattr(sig, "param_types", None) else None,
			return_type=getattr(sig, "return_type", None),
			declared_can_throw=True,
		)
		register_derived(wrapper_id, wrap_sig)
		specs.append(MethodWrapperSpec(wrapper_fn_id=wrapper_id, target_fn_id=fn_id))
	return specs, errors


def _assert_signature_map_split(
	*,
	base_signatures_by_id: Mapping[FunctionId, FnSignature],
	derived_signatures_by_id: Mapping[FunctionId, FnSignature],
	context: str,
) -> None:
	overlap = set(base_signatures_by_id.keys()) & set(derived_signatures_by_id.keys())
	if overlap:
		raise AssertionError(f"signature map overlap in {context}: {sorted(overlap)!r}")


def _normalize_func_maps(
	func_hirs: Mapping[FunctionId | str, H.HBlock],
	signatures: Mapping[FunctionId | str, FnSignature] | None,
) -> tuple[dict[FunctionId, H.HBlock], dict[FunctionId, FnSignature], dict[str, list[FunctionId]]]:
	if not func_hirs:
		return {}, {}, {}
	first_key = next(iter(func_hirs.keys()))
	if isinstance(first_key, FunctionId):
		fn_ids_by_name: dict[str, list[FunctionId]] = {}
		for fid in func_hirs:
			fn_ids_by_name.setdefault(function_symbol(fid), []).append(fid)
		signatures_by_id: dict[FunctionId, FnSignature] = {}
		if signatures:
			signatures_by_id = dict(signatures)  # type: ignore[assignment]
		return dict(func_hirs), signatures_by_id, fn_ids_by_name
	func_hirs_by_id: dict[FunctionId, H.HBlock] = {}
	fn_ids_by_name: dict[str, list[FunctionId]] = {}
	name_ord: dict[str, int] = {}
	for name in sorted(func_hirs.keys()):
		block = func_hirs[name]
		ordinal = name_ord.get(name, 0)
		name_ord[name] = ordinal + 1
		fid = FunctionId(module="main", name=name, ordinal=ordinal)
		func_hirs_by_id[fid] = block
		fn_ids_by_name.setdefault(name, []).append(fid)
	signatures_by_id: dict[FunctionId, FnSignature] = {}
	if signatures:
		name_ord.clear()
		for name in sorted(signatures.keys()):
			sig = signatures[name]
			ids = fn_ids_by_name.get(name, [])
			if ids:
				idx = name_ord.get(name, 0)
				if idx >= len(ids):
					idx = len(ids) - 1
				fid = ids[idx]
			else:
				ordinal = name_ord.get(name, 0)
				fid = FunctionId(module="main", name=name, ordinal=ordinal)
				fn_ids_by_name.setdefault(name, []).append(fid)
			name_ord[name] = name_ord.get(name, 0) + 1
			signatures_by_id[fid] = sig
	return func_hirs_by_id, signatures_by_id, fn_ids_by_name


def _ensure_module_packages(
	type_table: TypeTable | None,
	*,
	modules: Iterable[str],
	package_id: str | None,
	allow_fill: bool,
	context: str,
) -> None:
	if type_table is None:
		return
	module_packages = getattr(type_table, "module_packages", None)
	if module_packages is None:
		if not allow_fill:
			raise ValueError(f"{context}: module_packages missing")
		module_packages = {}
		type_table.module_packages = module_packages
	if not isinstance(module_packages, dict):
		raise ValueError(f"{context}: module_packages must be dict")
	default_pkg = package_id or "__local__"
	missing: list[str] = []
	for mod in sorted(set(m for m in modules if isinstance(m, str) and m)):
		if mod not in module_packages:
			if allow_fill:
				module_packages[mod] = default_pkg
			else:
				missing.append(mod)
	if missing:
		raise ValueError(f"{context}: module_packages missing entries for {missing}")


def _collect_call_nodes_by_id(root: H.HNode) -> dict[int, H.HExpr]:
	seen: set[int] = set()
	found: dict[int, H.HExpr] = {}

	def walk(obj: object) -> None:
		obj_id = id(obj)
		if obj_id in seen:
			return
		seen.add(obj_id)

		if isinstance(obj, (H.HCall, H.HMethodCall, H.HInvoke)):
			found[getattr(obj, "node_id", -1)] = obj

		if not _should_descend(obj):
			return
		if is_dataclass(obj):
			for f in fields(obj):
				walk_value(getattr(obj, f.name))
		else:
			for val in vars(obj).values():
				walk_value(val)

	def walk_value(val: object) -> None:
		if val is None:
			return
		if isinstance(val, (list, tuple)):
			for item in val:
				walk_value(item)
			return
		if isinstance(val, dict):
			for item in val.values():
				walk_value(item)
			return
		walk(val)

	def _should_descend(obj: object) -> bool:
		if isinstance(obj, H.HNode):
			return True
		if is_dataclass(obj) and obj.__class__.__module__.startswith("lang.driftc.stage1"):
			return True
		return False

	walk(root)
	return found


def _intrinsic_contract_diag(
	*,
	code: str,
	message: str,
	span: object | None,
	notes: list[str] | None = None,
) -> Diagnostic:
	return Diagnostic(
		message=message,
		code=code,
		phase="typecheck",
		severity="error",
		span=Span.from_loc(span),
		notes=list(notes or []),
	)


def _validate_intrinsic_callinfo(typed_fn: "TypedFn") -> list[Diagnostic]:
	diags: list[Diagnostic] = []

	def _emit(
		*,
		code: str,
		message: str,
		call: object | None,
		notes: list[str] | None = None,
	) -> None:
		diags.append(
			_intrinsic_contract_diag(
				code=code,
				message=message,
				span=getattr(call, "loc", None),
				notes=notes,
			)
		)

	call_nodes = _collect_call_nodes_by_id(typed_fn.body)
	call_info = getattr(typed_fn, "call_info_by_callsite_id", None)
	if not isinstance(call_info, dict):
		return diags
	callsite_to_nodes: dict[int, list[int]] = {}
	for node_id, call in call_nodes.items():
		csid = getattr(call, "callsite_id", None)
		if isinstance(csid, int):
			callsite_to_nodes.setdefault(csid, []).append(node_id)
	for key, info in call_info.items():
		if info.target.kind is not CallTargetKind.INTRINSIC:
			continue
		kind = info.target.intrinsic
		if kind is None:
			_emit(
				code="E_INTRINSIC_CALLINFO_MISSING_KIND",
				message="intrinsic call missing kind in CallInfo",
				call=None,
				notes=[
					f"fn={function_symbol(getattr(typed_fn, 'fn_id', None))}",
					f"callsite_id={key}",
				],
			)
			continue
		node_ids = callsite_to_nodes.get(key) or []
		call = None
		if node_ids:
			call = call_nodes.get(node_ids[0])
			if len(node_ids) > 1:
				def _call_name(n: object) -> str | None:
					if isinstance(n, H.HCall) and isinstance(n.fn, H.HVar):
						return n.fn.name
					if isinstance(n, H.HMethodCall):
						return n.method_name
					return None
				for node_id in node_ids:
					cand = call_nodes.get(node_id)
					name = _call_name(cand)
					if kind is IntrinsicKind.STRING_BYTE_AT and name == "string_byte_at":
						call = cand
						break
					if kind is IntrinsicKind.BYTE_LENGTH and name == "byte_length":
						call = cand
						break
		if call is None:
			_emit(
				code="E_INTRINSIC_CALLINFO_MISSING_NODE",
				message="intrinsic CallInfo is missing source call node",
				call=None,
				notes=[
					f"fn={function_symbol(getattr(typed_fn, 'fn_id', None))}",
					f"callsite_id={key}",
					f"kind={kind.value}",
				],
			)
			continue
		kwargs = getattr(call, "kwargs", None) or []
		# Name disambiguation for overloaded intrinsics.
		if kind is IntrinsicKind.BYTE_LENGTH:
			if not isinstance(call, (H.HCall, H.HMethodCall)):
				continue
			if isinstance(call, H.HCall) and not (isinstance(call.fn, H.HVar) and call.fn.name == "byte_length"):
				continue
			if isinstance(call, H.HMethodCall) and call.method_name != "byte_length":
				continue
		if kind is IntrinsicKind.STRING_BYTE_AT:
			if not isinstance(call, (H.HCall, H.HMethodCall)):
				continue
			if isinstance(call, H.HCall) and not (isinstance(call.fn, H.HVar) and call.fn.name == "string_byte_at"):
				continue
			if isinstance(call, H.HMethodCall) and call.method_name != "string_byte_at":
				continue
		for issue in intrinsic_call_issues(kind, call, kwargs=kwargs):
			_emit(code=issue.code, message=issue.message, call=call)
	return diags


def _typevar_callinfo_diags(
	typed_fn: "TypedFn",
	type_table: "TypeTable | None",
) -> list[Diagnostic]:
	if type_table is None:
		return []
	call_info = getattr(typed_fn, "call_info_by_callsite_id", None)
	if not isinstance(call_info, dict):
		return []
	call_nodes = _collect_call_nodes_by_id(typed_fn.body)
	callsite_to_node: dict[int, int] = {}
	for node_id, call in call_nodes.items():
		csid = getattr(call, "callsite_id", None)
		if isinstance(csid, int):
			callsite_to_node[csid] = node_id

	def _has_typevar(tid: int | None) -> bool:
		if tid is None:
			return False
		return bool(type_table.has_typevar(tid))

	diags: list[Diagnostic] = []
	for csid, info in call_info.items():
		if not isinstance(csid, int):
			continue
		if any(_has_typevar(tid) for tid in info.sig.param_types) or _has_typevar(info.sig.user_ret_type):
			node_id = callsite_to_node.get(csid)
			call = call_nodes.get(node_id) if node_id is not None else None
			span = getattr(call, "loc", None) if call is not None else None
			target_name = "<unknown>"
			if info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
				target_name = function_symbol(info.target.symbol)
			elif info.target.kind is CallTargetKind.TRAIT and info.target.method_name:
				target_name = info.target.method_name
			diags.append(
				Diagnostic(
					message=(
						f"internal: generic call signature survived instantiation for call to '{target_name}'"
					),
					code="E_INTERNAL_GENERIC_CALLINFO",
					severity="error",
					phase="typecheck",
					span=span,
				)
			)
	return diags


def _display_name_for_fn_id(fn_id: FunctionId) -> str:
	# Match parser qualification rules: the default `main` module stays
	# unqualified, other modules use `module::name`.
	if fn_id.module == "main":
		return fn_id.name
	return f"{fn_id.module}::{fn_id.name}"


def _reserved_module_ids(
	func_hirs_by_id: Mapping[FunctionId, object] | None,
	signatures_by_id: Mapping[FunctionId, FnSignature] | None,
) -> list[str]:
	mod_ids: set[str] = set()
	if func_hirs_by_id:
		for fn_id in func_hirs_by_id.keys():
			if isinstance(fn_id.module, str):
				mod_ids.add(fn_id.module)
	if signatures_by_id:
		for fn_id in signatures_by_id.keys():
			if isinstance(fn_id.module, str):
				mod_ids.add(fn_id.module)
	return sorted(mid for mid in mod_ids if mid.startswith(("std.", "lang.", "drift.")))


def _reserved_namespace_diags(module_ids: Iterable[str]) -> list[Diagnostic]:
	return [
		Diagnostic(
			message=f"reserved module namespace '{mid}' requires toolchain trust",
			severity="error",
			phase="package",
			span=Span(),
		)
		for mid in module_ids
	]


class ReservedNamespacePolicy(Enum):
	ENFORCE = "enforce"
	ALLOW_DEV = "allow_dev"


def _assert_all_phased(diags: Iterable[Diagnostic], *, context: str) -> None:
	missing = [d for d in diags if d.phase is None]
	if missing:
		raise AssertionError(f"{context} diagnostics missing phase ({len(missing)})")


def _span_has_location(span: Span | None) -> bool:
	return bool(span is not None and span.line is not None and span.column is not None)


def _first_span_in_hir_block(block: H.HBlock | None) -> Span | None:
	if block is None:
		return None
	seen: set[int] = set()

	def _walk_expr(expr: H.HExpr) -> Span | None:
		obj_id = id(expr)
		if obj_id in seen:
			return None
		seen.add(obj_id)
		loc = Span.from_loc(getattr(expr, "loc", None))
		if _span_has_location(loc):
			return loc
		if isinstance(expr, H.HCall):
			hit = _walk_expr(expr.fn)
			if hit is not None:
				return hit
			for arg in expr.args:
				hit = _walk_expr(arg)
				if hit is not None:
					return hit
			for kw in getattr(expr, "kwargs", []) or []:
				hit = _walk_expr(kw.value)
				if hit is not None:
					return hit
			return None
		if isinstance(expr, H.HMethodCall):
			hit = _walk_expr(expr.receiver)
			if hit is not None:
				return hit
			for arg in expr.args:
				hit = _walk_expr(arg)
				if hit is not None:
					return hit
			for kw in getattr(expr, "kwargs", []) or []:
				hit = _walk_expr(kw.value)
				if hit is not None:
					return hit
			return None
		if isinstance(expr, H.HInvoke):
			hit = _walk_expr(expr.callee)
			if hit is not None:
				return hit
			for arg in expr.args:
				hit = _walk_expr(arg)
				if hit is not None:
					return hit
			for kw in getattr(expr, "kwargs", []) or []:
				hit = _walk_expr(kw.value)
				if hit is not None:
					return hit
			return None
		for child in getattr(expr, "__dict__", {}).values():
			if isinstance(child, H.HExpr):
				hit = _walk_expr(child)
				if hit is not None:
					return hit
			elif isinstance(child, list):
				for item in child:
					if isinstance(item, H.HExpr):
						hit = _walk_expr(item)
						if hit is not None:
							return hit
		return None

	for stmt in block.statements:
		loc = Span.from_loc(getattr(stmt, "loc", None))
		if _span_has_location(loc):
			return loc
		for child in getattr(stmt, "__dict__", {}).values():
			if isinstance(child, H.HExpr):
				hit = _walk_expr(child)
				if hit is not None:
					return hit
			elif isinstance(child, H.HBlock):
				hit = _first_span_in_hir_block(child)
				if hit is not None:
					return hit
			elif isinstance(child, list):
				for item in child:
					if isinstance(item, H.HExpr):
						hit = _walk_expr(item)
						if hit is not None:
							return hit
					elif isinstance(item, H.HBlock):
						hit = _first_span_in_hir_block(item)
						if hit is not None:
							return hit
	return None


def _best_effort_boundary_span(
	*,
	fn_id: FunctionId | None = None,
	signatures_by_id: Mapping[FunctionId, FnSignature] | None = None,
	checked: CheckedProgramById | None = None,
	hir_block: H.HBlock | None = None,
	origin_by_fn_id: Mapping[FunctionId, Path] | None = None,
) -> Span:
	candidates: list[Span] = []
	if fn_id is not None and signatures_by_id is not None:
		sig = signatures_by_id.get(fn_id)
		if sig is not None:
			candidates.append(Span.from_loc(getattr(sig, "loc", None)))
	if fn_id is not None and checked is not None:
		info = checked.fn_infos_by_id.get(fn_id)
		if info is not None:
			candidates.append(Span.from_loc(getattr(info, "span", None)))
			sig = getattr(info, "signature", None)
			if sig is not None:
				candidates.append(Span.from_loc(getattr(sig, "loc", None)))
	if hir_block is not None:
		hit = _first_span_in_hir_block(hir_block)
		if hit is not None:
			candidates.append(hit)
	if signatures_by_id is not None:
		for sig in signatures_by_id.values():
			candidates.append(Span.from_loc(getattr(sig, "loc", None)))
	if checked is not None:
		for info in checked.fn_infos_by_id.values():
			candidates.append(Span.from_loc(getattr(info, "span", None)))
			sig = getattr(info, "signature", None)
			if sig is not None:
				candidates.append(Span.from_loc(getattr(sig, "loc", None)))
	best = next((sp for sp in candidates if _span_has_location(sp)), None)
	if best is None:
		best = next((sp for sp in candidates if sp.file is not None), None)
	if best is None:
		best = Span()
	if best.file is None and fn_id is not None and origin_by_fn_id is not None:
		path = origin_by_fn_id.get(fn_id)
		if path is not None:
			best = replace(best, file=str(path))
	return best


def _append_boundary_contract_diag(
	checked: CheckedProgramById,
	*,
	phase: str,
	prefix: str,
	err: AssertionError,
	fn_id: FunctionId | None = None,
	signatures_by_id: Mapping[FunctionId, FnSignature] | None = None,
	hir_block: H.HBlock | None = None,
	origin_by_fn_id: Mapping[FunctionId, Path] | None = None,
) -> None:
	checked.diagnostics.append(
		Diagnostic(
			message=f"internal: {prefix} ({err})",
			severity="error",
			span=_best_effort_boundary_span(
				fn_id=fn_id,
				signatures_by_id=signatures_by_id,
				checked=checked,
				hir_block=hir_block,
				origin_by_fn_id=origin_by_fn_id,
			),
			phase=phase,
		)
	)


def _validate_codegen_contract(
	mir_funcs: Mapping[FunctionId, M.MirFunc],
	ssa_funcs: Mapping[FunctionId, MirToSSA.SsaFunc] | None,
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable | None,
	*,
	debug_enabled: bool,
) -> None:
	"""
	Validate MIR->LLVM hand-off invariants before entering LLVM lowering.

	This is intentionally conservative and should fail deterministically with a
	clear assertion message so callers can surface a stable `phase=codegen`
	diagnostic instead of propagating deep emitter assertions.
	"""
	if type_table is None:
		raise AssertionError("codegen contract: missing type table")
	if ssa_funcs is None:
		raise AssertionError("codegen contract: missing SSA functions")
	missing_ssa = [fn_id for fn_id in mir_funcs.keys() if fn_id not in ssa_funcs]
	if missing_ssa:
		raise AssertionError(f"codegen contract: missing SSA for {function_symbol(missing_ssa[0])}")
	missing_info = [
		fn_id
		for fn_id in mir_funcs.keys()
		if fn_id not in fn_infos or fn_infos[fn_id].signature is None
	]
	if missing_info:
		raise AssertionError(f"codegen contract: missing FnInfo/signature for {function_symbol(missing_info[0])}")
	for fn in mir_funcs.values():
		for block in fn.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, M.Call) and instr.fn_id not in fn_infos:
					raise AssertionError(
						f"codegen contract: unknown call target {function_symbol(instr.fn_id)} in {function_symbol(fn.fn_id)}"
					)
	# Placeholder for future debug-info contract hardening.
	_ = debug_enabled


def _sig_declared_can_throw(sig: FnSignature) -> bool:
	"""Normalize declared throw-mode for downstream ABI decisions."""
	return True if sig.declared_can_throw is None else bool(sig.declared_can_throw)


def _find_dependency_main(loaded_pkgs: list["LoadedPackage"]) -> tuple[str, Path, str] | None:
	"""
	Detect a dependency package that defines a function named `main`.

	Returns (package_id, package_path, symbol_name) for diagnostics.
	"""
	for pkg in loaded_pkgs:
		man = pkg.manifest
		pkg_id = man.get("package_id") if isinstance(man, dict) else None
		pkg_id_str = pkg_id if isinstance(pkg_id, str) else _package_label()
		for _mid, mod in pkg.modules_by_id.items():
			payload = mod.payload
			if not isinstance(payload, dict):
				continue
			sigs_obj = payload.get("signatures")
			if not isinstance(sigs_obj, dict):
				continue
			for sym, sd in sigs_obj.items():
				if not isinstance(sd, dict):
					continue
				if sd.get("is_method", False):
					continue
				name = str(sd.get("name") or sym)
				local = name.rsplit("::", 1)[-1]
				if local == "main":
					return pkg_id_str, pkg.path, name
	return None


def _encode_trait_metadata_for_module(
	*,
	package_id: str,
	module_id: str,
	exported_traits: list[str],
	trait_world: object | None,
) -> list[dict[str, object]]:
	if not exported_traits or trait_world is None:
		return []
	traits = getattr(trait_world, "traits", None)
	if not isinstance(traits, dict):
		return []
	exported = set(exported_traits)
	out: list[dict[str, object]] = []
	for trait_def in traits.values():
		key = getattr(trait_def, "key", None)
		if key is None or getattr(key, "module", None) != module_id:
			continue
		if getattr(trait_def, "name", None) not in exported:
			continue
		trait_type_params = list(getattr(trait_def, "type_params", []) or [])
		methods: list[dict[str, object]] = []
		for method in getattr(trait_def, "methods", []) or []:
			method_type_params = list(getattr(method, "type_params", []) or [])
			type_param_names = {"Self"}
			type_param_names.update(trait_type_params)
			type_param_names.update(method_type_params)
			params: list[dict[str, object]] = []
			for param in list(getattr(method, "params", []) or []):
				params.append(
					{
						"name": param.name,
						"type": encode_type_expr(
							param.type_expr,
							default_module=module_id,
							type_param_names=type_param_names,
						),
					}
				)
			methods.append(
				{
					"name": getattr(method, "name", ""),
					"type_params": method_type_params,
					"params": params,
					"return_type": encode_type_expr(
						getattr(method, "return_type", None),
						default_module=module_id,
						type_param_names=type_param_names,
					),
					"require": encode_trait_expr(
						getattr(method, "require", None),
						default_module=module_id,
						type_param_names=method_type_params + trait_type_params,
					),
					"span": encode_span(getattr(method, "loc", None)),
				}
			)
		trait_name = getattr(trait_def, "name", "")
		trait_id_obj = {
			"package_id": package_id,
			"module": getattr(key, "module", None) or module_id,
			"name": trait_name,
		}
		out.append(
			{
				"trait_id": trait_id_obj,
				"name": trait_name,
				"type_params": list(getattr(trait_def, "type_params", []) or []),
				"methods": methods,
				"require": encode_trait_expr(
					getattr(trait_def, "require", None),
					default_module=module_id,
					type_param_names=trait_type_params,
				),
				"span": encode_span(getattr(trait_def, "loc", None)),
			}
		)
	return out


def _encode_impl_headers_for_module(
	*,
	module_id: str,
	impls: list[object] | None,
) -> list[dict[str, object]]:
	if not impls:
		return []
	out: list[dict[str, object]] = []
	for impl in impls:
		type_params = list(getattr(impl, "impl_type_params", []) or [])
		target_obj = encode_type_expr(
			getattr(impl, "target_expr", None),
			default_module=module_id,
			type_param_names=set(type_params),
		)
		if target_obj is None:
			continue
		trait_key = getattr(impl, "trait_key", None)
		trait_obj = None
		trait_args_obj: list[dict[str, object] | None] = []
		if trait_key is not None:
			trait_mod = getattr(trait_key, "module", None) or module_id
			trait_name = getattr(trait_key, "name", None)
			if isinstance(trait_name, str):
				trait_obj = {
					"package_id": getattr(trait_key, "package_id", None),
					"module": trait_mod,
					"name": trait_name,
				}
		trait_expr = getattr(impl, "trait_expr", None)
		if trait_expr is not None:
			for arg in list(getattr(trait_expr, "args", []) or []):
				encoded = encode_type_expr(
					arg,
					default_module=module_id,
					type_param_names=set(type_params),
				)
				if encoded is not None:
					trait_args_obj.append(encoded)
		methods: list[dict[str, object]] = []
		for method in list(getattr(impl, "methods", []) or []):
			fn_id = getattr(method, "fn_id", None)
			if not isinstance(fn_id, FunctionId):
				continue
			methods.append(
				{
					"name": getattr(method, "name", ""),
					"fn_id": function_id_to_obj(fn_id),
					"fn_symbol": function_symbol(fn_id),
					"is_pub": bool(getattr(method, "is_pub", False)),
					"span": encode_span(getattr(method, "loc", None)),
				}
			)
		def_module = getattr(impl, "def_module", module_id)
		require_obj = encode_trait_expr(
			getattr(impl, "require_expr", None),
			default_module=module_id,
			type_param_names=type_params,
		)
		decl_fingerprint = sha256_hex(
			canonical_json_bytes(
				{
					"def_module": def_module,
					"trait": trait_obj,
					"trait_args": trait_args_obj,
					"type_params": type_params,
					"target": target_obj,
					"require": require_obj,
				}
			)
		)
		out.append(
			{
				"impl_id": int(getattr(impl, "impl_id", -1)),
				"def_module": def_module,
				"trait": trait_obj,
				"trait_args": trait_args_obj if trait_args_obj else None,
				"type_params": type_params,
				"target": target_obj,
				"require": require_obj,
				"decl_fingerprint": decl_fingerprint,
				"methods": methods,
				"span": encode_span(getattr(impl, "loc", None)),
			}
		)
	return out


def _collect_external_trait_and_impl_metadata(
	*,
	loaded_pkgs: list[object],
	type_table: TypeTable,
	external_signatures_by_id: dict[FunctionId, FnSignature],
	id_registry: IdRegistry | None = None,
) -> tuple[list[object], list[object], set[object], set[str]]:
	from lang.driftc.traits.world import ImplKey, TraitDef, TraitKey
	from lang.driftc.impl_index import ImplMeta, ImplMethodMeta
	from lang.driftc.packages.provisional_dmir_v0 import decode_span
	from lang.driftc.parser import ast as parser_ast, stdlib_root

	trait_defs: list[object] = []
	impl_metas: list[object] = []
	missing_traits: set[object] = set()
	missing_impl_modules: set[str] = set()

	for pkg in loaded_pkgs:
		pkg_id = getattr(pkg, "manifest", {}).get("package_id")
		if not isinstance(pkg_id, str) or not pkg_id:
			pkg_id = str(getattr(pkg, "path", "")) or "<unknown>"
		for mid, mod in getattr(pkg, "modules_by_id", {}).items():
			if not isinstance(mid, str):
				continue
			iface = getattr(mod, "interface", None)
			if not isinstance(iface, dict):
				continue
			exports = iface.get("exports")
			exported_traits: set[str] = set()
			if isinstance(exports, dict):
				traits = exports.get("traits")
				if isinstance(traits, list):
					exported_traits = {t for t in traits if isinstance(t, str)}

			trait_meta = iface.get("trait_metadata")
			seen_trait_names: set[str] = set()
			if isinstance(trait_meta, list):
				for entry in trait_meta:
					if not isinstance(entry, dict):
						continue
					trait_id_obj = entry.get("trait_id")
					if not isinstance(trait_id_obj, dict):
						raise ValueError(f"module '{mid}' trait_metadata missing trait_id")
					trait_pkg = trait_id_obj.get("package_id")
					trait_mod = trait_id_obj.get("module")
					trait_name = trait_id_obj.get("name")
					if not isinstance(trait_pkg, str) or not trait_pkg:
						raise ValueError(f"module '{mid}' trait_metadata invalid trait_id.package_id")
					if not isinstance(trait_mod, str) or not trait_mod:
						raise ValueError(f"module '{mid}' trait_metadata invalid trait_id.module")
					if not isinstance(trait_name, str) or not trait_name:
						raise ValueError(f"module '{mid}' trait_metadata invalid trait_id.name")
					if trait_pkg != pkg_id:
						raise ValueError(f"module '{mid}' trait_metadata trait_id package_id mismatch")
					if trait_mod != mid:
						raise ValueError(f"module '{mid}' trait_metadata trait_id module mismatch")
					name = entry.get("name")
					if not isinstance(name, str) or not name:
						continue
					if name != trait_name:
						raise ValueError(f"module '{mid}' trait_metadata trait_id name mismatch")
					seen_trait_names.add(name)
					methods: list[parser_ast.TraitMethodSig] = []
					for method in entry.get("methods", []) if isinstance(entry.get("methods"), list) else []:
						if not isinstance(method, dict):
							continue
						mname = method.get("name")
						if not isinstance(mname, str) or not mname:
							continue
						type_params_raw = method.get("type_params")
						type_params = (
							[p for p in type_params_raw if isinstance(p, str)]
							if isinstance(type_params_raw, list)
							else []
						)
						params: list[parser_ast.Param] = []
						for param in method.get("params", []) if isinstance(method.get("params"), list) else []:
							if not isinstance(param, dict):
								continue
							pname = param.get("name")
							if not isinstance(pname, str) or not pname:
								continue
							ptype = decode_type_expr(param.get("type"))
							params.append(parser_ast.Param(name=pname, type_expr=ptype))
						ret_type = decode_type_expr(method.get("return_type"))
						if ret_type is None:
							continue
						methods.append(
							parser_ast.TraitMethodSig(
								name=mname,
								params=params,
								return_type=ret_type,
								loc=decode_span(method.get("span")) or None,
								type_params=type_params,
							)
						)
					require = decode_trait_expr(entry.get("require"))
					trait_key = TraitKey(package_id=trait_pkg, module=trait_mod, name=trait_name)
					if id_registry is not None:
						id_registry.intern_trait(trait_key)
					trait_type_params_raw = entry.get("type_params")
					trait_type_params = (
						[p for p in trait_type_params_raw if isinstance(p, str)]
						if isinstance(trait_type_params_raw, list)
						else []
					)
					trait_defs.append(
						TraitDef(
							key=trait_key,
							name=name,
							methods=methods,
							require=require,
							loc=decode_span(entry.get("span")) or None,
							type_params=trait_type_params,
						)
					)
			for name in exported_traits:
				if name not in seen_trait_names:
					missing_traits.add(TraitKey(package_id=pkg_id, module=mid, name=name))

			impl_headers = iface.get("impl_headers")
			if not isinstance(impl_headers, list):
				missing_impl_modules.add(mid)
				continue
			for entry in impl_headers:
				if not isinstance(entry, dict):
					continue
				impl_id = entry.get("impl_id")
				def_module = entry.get("def_module") or mid
				decl_fp = entry.get("decl_fingerprint")
				if not isinstance(impl_id, int) or not isinstance(def_module, str):
					continue
				if not isinstance(decl_fp, str) or not decl_fp:
					raise ValueError(f"module '{mid}' impl_headers missing decl_fingerprint")
				target_expr = decode_type_expr(entry.get("target"))
				if target_expr is None:
					continue
				type_params_raw = entry.get("type_params")
				type_params = [p for p in type_params_raw if isinstance(p, str)] if isinstance(type_params_raw, list) else []
				impl_owner = FunctionId(module="lang.__external", name=f"__impl_{def_module}:{impl_id}", ordinal=0)
				impl_type_param_map = {name: TypeParamId(impl_owner, idx) for idx, name in enumerate(type_params)}
				target_type_id = resolve_opaque_type(
					target_expr,
					type_table,
					module_id=def_module,
					type_params=impl_type_param_map,
				)
				trait_key = None
				trait_obj = entry.get("trait")
				if isinstance(trait_obj, dict):
					tpkg = trait_obj.get("package_id")
					tmod = trait_obj.get("module")
					tname = trait_obj.get("name")
					if not isinstance(tpkg, str) or not tpkg:
						raise ValueError(f"module '{mid}' impl_headers trait missing package_id")
					if isinstance(tmod, str) and isinstance(tname, str):
						trait_key = TraitKey(package_id=tpkg, module=tmod, name=tname)
						if id_registry is not None:
							id_registry.intern_trait(trait_key)
				trait_args_exprs: list[parser_ast.TypeExpr] = []
				trait_args: list[TypeId] = []
				trait_args_obj = entry.get("trait_args")
				if isinstance(trait_args_obj, list):
					for arg_obj in trait_args_obj:
						arg_expr = decode_type_expr(arg_obj)
						if arg_expr is None:
							continue
						trait_args_exprs.append(arg_expr)
						trait_args.append(
							resolve_opaque_type(
								arg_expr,
								type_table,
								module_id=def_module,
								type_params=impl_type_param_map,
							)
						)
				trait_expr = None
				if trait_key is not None:
					trait_expr = parser_ast.TypeExpr(
						name=trait_key.name,
						args=trait_args_exprs,
						module_id=trait_key.module,
					)
				methods: list[ImplMethodMeta] = []
				for method in entry.get("methods", []) if isinstance(entry.get("methods"), list) else []:
					if not isinstance(method, dict):
						continue
					mname = method.get("name")
					fn_symbol = method.get("fn_symbol")
					fn_id_obj = method.get("fn_id")
					if not isinstance(mname, str) or not mname or not isinstance(fn_id_obj, dict):
						raise ValueError(
							f"module '{mid}' impl_headers method '{mname or '<unknown>'}' missing fn_id"
						)
					fn_id = function_id_from_obj(fn_id_obj)
					if not isinstance(fn_id, FunctionId):
						raise ValueError(
							f"module '{mid}' impl_headers method '{mname}' has invalid fn_id"
						)
					if fn_symbol is not None:
						if not isinstance(fn_symbol, str) or not fn_symbol:
							raise ValueError(
								f"module '{mid}' impl_headers method '{mname}' has invalid fn_symbol"
							)
						if fn_symbol != function_symbol(fn_id):
							raise ValueError(
								f"module '{mid}' impl_headers method '{mname}' fn_symbol mismatch"
							)
					if getattr(fn_id, "module", None) != def_module:
						raise ValueError(
							f"module '{mid}' impl_headers method '{mname}' fn_id module mismatch"
						)
					methods.append(
						ImplMethodMeta(
							fn_id=fn_id,
							name=mname,
							is_pub=bool(method.get("is_pub", False)),
							fn_symbol=fn_symbol,
							loc=decode_span(method.get("span")) or None,
						)
					)
					sig = external_signatures_by_id.get(fn_id)
					if sig is not None:
						impl_param_map = {p.name: p.id for p in getattr(sig, "impl_type_params", []) or []}
						if getattr(target_expr, "args", None):
							impl_args = [
								resolve_opaque_type(
									arg,
									type_table,
									module_id=def_module,
									type_params=impl_param_map,
								)
								for arg in list(getattr(target_expr, "args", []) or [])
							]
							external_signatures_by_id[fn_id] = replace(
								sig,
								impl_target_type_args=impl_args,
							)
						else:
							external_signatures_by_id[fn_id] = replace(
								sig,
								impl_target_type_args=[],
							)
				require_expr = decode_trait_expr(entry.get("require"))
				if id_registry is not None:
					target_head = type_key_from_typeid(type_table, target_type_id).head()
					impl_key = ImplKey(
						package_id=pkg_id,
						module=def_module,
						trait=trait_key,
						target_head=target_head,
						decl_fingerprint=decl_fp,
					)
					id_registry.intern_impl(impl_key, preferred=impl_id)
				impl_metas.append(
					ImplMeta(
						impl_id=impl_id,
						def_module=def_module,
						target_type_id=target_type_id,
						trait_key=trait_key,
						trait_expr=trait_expr,
						trait_args=trait_args,
						require_expr=require_expr,
						target_expr=target_expr,
						impl_type_params=type_params,
						methods=methods,
						loc=decode_span(entry.get("span")) or None,
					)
				)

	return trait_defs, impl_metas, missing_traits, missing_impl_modules


def _apply_stdlib_escape_annotations(signatures_by_id: Mapping) -> None:
	"""Annotate known stdlib callable params with escape levels (idempotent).

	This must run before the borrow checker so that escape-level enforcement
	(SCOPED, THREAD, STATIC) applies to stdlib functions like conc.scope,
	conc.spawn, etc.  Called from both compile_stubbed_funcs (e2e/test path)
	and main() (CLI path).
	"""
	from lang.driftc.borrow_checker import EscapeLevel as _EL
	_ANNOTATIONS: dict[tuple[str, str], list] = {
		("std.concurrent", "scope"): [_EL.SCOPED],
		("std.concurrent", "spawn"): [_EL.THREAD],
		("std.concurrent", "spawn_cb"): [_EL.THREAD],
		("std.concurrent", "spawn_on"): [None, _EL.THREAD],
		("std.concurrent", "spawn_future"): [_EL.THREAD],
		("std.concurrent", "spawn_future_on"): [None, _EL.THREAD],
		("lang.thread", "vt_spawn"): [_EL.THREAD, None],
		("lang.thread", "runtime_registry_set"): [None, None, _EL.STATIC],
		("lang.thread", "runtime_thread_registry_set"): [None, None, _EL.STATIC],
	}
	for _fn_id, _sig in signatures_by_id.items():
		_key = (getattr(_fn_id, "module", None), getattr(_fn_id, "name", None))
		_levels = _ANNOTATIONS.get(_key)
		if _levels is not None and _sig.param_escape_level is None:
			_sig.param_escape_level = list(_levels)


def compile_stubbed_funcs(
	func_hirs: Mapping[FunctionId | str, H.HBlock],
	declared_can_throw: Mapping[FunctionId | str, bool] | None = None,
	signatures: Mapping[FunctionId | str, FnSignature] | None = None,
	exc_env: Mapping[str, int] | None = None,
	module_exports: Mapping[str, dict[str, object]] | None = None,
	module_deps: Mapping[str, set[str]] | None = None,
	origin_by_fn_id: Mapping[FunctionId, Path] | None = None,
	package_id: str | None = None,
	generic_templates_by_id: Mapping[FunctionId, H.HBlock] | None = None,
	generic_templates_by_key: Mapping[FunctionKey, H.HBlock] | None = None,
	template_keys_by_fn_id: Mapping[FunctionId, FunctionKey] | None = None,
	external_trait_defs: Sequence[object] | None = None,
	external_impl_metas: Sequence[object] | None = None,
	external_missing_traits: set[object] | None = None,
	external_missing_impl_modules: set[str] | None = None,
	return_checked: bool = False,
	build_ssa: bool = False,
	return_ssa: bool = False,
	type_table: "TypeTable | None" = None,
	run_borrow_check: bool = False,
	prelude_enabled: bool = True,
	emit_instantiation_index: Path | None = None,
	enforce_entrypoint: bool = False,
	entry_module: str = "main",
	entry_name: str = "main",
	allow_unsafe: bool = True,
) -> (
	Dict[FunctionId, M.MirFunc]
	| tuple[Dict[FunctionId, M.MirFunc], CheckedProgramById]
	| tuple[
		Dict[FunctionId, M.MirFunc],
		CheckedProgramById,
		Dict[FunctionId, "MirToSSA.SsaFunc"] | None,
	]
):
	"""
	Lower a set of HIR function bodies through the lang pipeline and run throw checks.

	Args:
	  func_hirs: mapping of function name -> HIR block (body).
	  declared_can_throw: optional mapping of FunctionId/str -> bool; **legacy test shim**.
	    Prefer `signatures` for new tests and treat this as deprecated.
	  signatures: optional mapping of fn name -> FnSignature. The real checker will
	    use parsed/type-checked signatures to derive throw intent; this parameter
	    lets tests mimic that shape without a full parser/type checker.
	  exc_env: optional exception environment (event name -> code) passed to HIRToMIR.
	  origin_by_fn_id: optional mapping of FunctionId -> source path (debug-only).
	  generic_templates_by_id: optional legacy map of FunctionId -> TemplateHIR (from packages).
	  generic_templates_by_key: optional map of FunctionKey -> TemplateHIR (from packages).
	  template_keys_by_fn_id: optional map of FunctionId -> FunctionKey (package templates).
	  return_checked: when True, also return the CheckedProgramById produced by
	    the checker so diagnostics/fn_infos can be asserted in integration tests.
	  build_ssa: when True, also run MIR→SSA and derive a TypeEnv from SSA +
	    signatures so the type-aware throw check path is exercised. Loops/backedges
	    are still rejected by the SSA pass. The preferred path is for the checker
	    to supply `checked.type_env`; when absent we ask the checker to infer one
	    from SSA using its TypeTable/signatures.
	  return_ssa: when True (and return_checked=True), also return the SSA funcs
	    computed here. This keeps downstream helpers (e.g., LLVM codegen tests)
	    from re-running MIR→SSA and ensures they share the same SSA graph used
	    in throw checks.
	  run_borrow_check: when True, run the borrow checker on HIR blocks and append
	    diagnostics; this is a stubbed integration path (coarse regions).
	  emit_instantiation_index: optional path for a deterministic JSON dump of
	    instantiation keys/symbols/ABI flags produced in this run.
	  enforce_entrypoint: when True, validate entrypoint main() semantics after
	    type checking (Int return, nothrow, correct argv shape).
	  # TODO: drop declared_can_throw once all callers provide signatures/parsing.

	Returns:
	  dict of FunctionId -> lowered MIR function. When `return_checked` is
	  True, returns a `(mir_funcs, checked_program_by_id)` tuple.

	Notes:
	  In the driver path, throw-check violations are appended to
	  `checked.diagnostics`; direct calls to `run_throw_checks` without a
	  diagnostics sink still raise RuntimeError in tests. This helper exists
	  for tests/prototypes; a real CLI will build signatures and diagnostics
	  from parsed sources instead of the shims here.
	"""
	func_hirs_by_id, signatures_by_id, fn_ids_by_name = _normalize_func_maps(func_hirs, signatures)
	_timing_enabled = drift_debug.enabled("timing")
	def _timed(label: str):
		if not _timing_enabled:
			class _Noop:
				def __enter__(self_inner):  # type: ignore[no-untyped-def]
					return None
				def __exit__(self_inner, exc_type, exc, tb):  # type: ignore[no-untyped-def]
					return False
			return _Noop()
		import time as _timing_time
		start = _timing_time.perf_counter()
		class _Timing:
			def __enter__(self_inner):  # type: ignore[no-untyped-def]
				return None
			def __exit__(self_inner, exc_type, exc, tb):  # type: ignore[no-untyped-def]
				elapsed = _timing_time.perf_counter() - start
				import sys as _timing_sys
				print(f"[drift:debug][timing] {label}={elapsed:.3f}s", file=_timing_sys.stderr)
				return False
		return _Timing()
	if drift_debug.enabled("try_auto"):
		import sys as _try_auto_sys
		for fn_id, sig in signatures_by_id.items():
			if getattr(fn_id, "module", None) == "m":
				print(f"[try_auto] precheck sig {function_symbol(fn_id)} declared_throws={getattr(sig, 'declared_throws', None)}", file=_try_auto_sys.stderr)
	_required_modules: set[str] = {fid.module for fid in func_hirs_by_id.keys() if isinstance(fid, FunctionId)}
	_required_modules.update({fid.module for fid in signatures_by_id.keys() if isinstance(fid, FunctionId)})
	if module_exports:
		_required_modules.update({m for m in module_exports.keys() if isinstance(m, str)})
	if module_deps:
		_required_modules.update({m for m in module_deps.keys() if isinstance(m, str)})
	for deps in (module_deps or {}).values():
		_required_modules.update({m for m in deps if isinstance(m, str)})
	declared_can_throw_by_id: Dict[FunctionId, bool] | None = None
	if declared_can_throw:
		declared_can_throw_by_id = {}
		for key, val in declared_can_throw.items():
			if isinstance(key, FunctionId):
				declared_can_throw_by_id[key] = bool(val)
				continue
			if isinstance(key, str):
				ids = fn_ids_by_name.get(key)
				if not ids:
					raise AssertionError(f"declared_can_throw provided for unknown function '{key}'")
				if len(ids) > 1:
					raise AssertionError(f"declared_can_throw name '{key}' is ambiguous")
				declared_can_throw_by_id[ids[0]] = bool(val)
				continue
			raise AssertionError(f"declared_can_throw key must be FunctionId or str, got {type(key)!r}")
	from lang.driftc import stage1 as H

	# Guard: signatures with TypeIds must come with a shared TypeTable so TypeKind
	# queries stay coherent end-to-end.
	if signatures_by_id and type_table is None:
		for sig in signatures_by_id.values():
			if sig.return_type_id is not None or sig.param_type_ids is not None:
				raise ValueError("signatures with TypeIds require a shared type_table")

	# Important: run the checker on normalized HIR so it sees canonical forms
	# (structural-only rewrites). We preserve node_ids and then re-use the typed
	# HIR to keep CallInfo alignment; checker-injected annotations (e.g. match
	# binder indices) are preserved during normalization.

	# If no signatures were supplied, resolve basic signatures from the original HIR.
	shared_type_table = type_table
	if shared_type_table is not None and not hasattr(shared_type_table, "_destructible_query"):
		def _fallback_destructible(_tid: int) -> bool | None:
			return None
		shared_type_table.set_destructible_query(_fallback_destructible, allow_fallback=True)
	if not signatures_by_id:
		shared_type_table, base_signatures_by_id = resolve_program_signatures(
			_fake_decls_from_hirs(func_hirs_by_id),
			table=shared_type_table,
		)
	else:
		# Ensure TypeIds are resolved on supplied signatures using a shared table.
		if shared_type_table is None:
			shared_type_table = TypeTable()
		if drift_debug.enabled("type_prov") and shared_type_table is not None:
			shared_type_table.enable_type_provenance()
		resolved_signatures: dict[FunctionId, FnSignature] = {}
		for fn_id, sig in signatures_by_id.items():
			type_param_map: dict[str, object] = {}
			if getattr(sig, "impl_type_params", None) or getattr(sig, "type_params", None):
				for p in (list(getattr(sig, "impl_type_params", []) or []) + list(getattr(sig, "type_params", []) or [])):
					type_param_map[p.name] = p.id
			ret_id = sig.return_type_id
			if sig.return_type is not None and (type_param_map or ret_id is None or shared_type_table.get(ret_id).kind is TypeKind.UNKNOWN):
				ret_id = resolve_opaque_type(sig.return_type, shared_type_table, module_id=getattr(sig, "module", None), type_params=type_param_map or None)
			param_ids = sig.param_type_ids
			if sig.param_types is not None and (type_param_map or param_ids is None):
				param_ids = [resolve_opaque_type(p, shared_type_table, module_id=getattr(sig, "module", None), type_params=type_param_map or None) for p in sig.param_types]
			if param_ids is None and sig.param_types is None:
				param_ids = []
			err_id = sig.error_type_id
			if err_id is None and ret_id is not None:
				td = shared_type_table.get(ret_id)
				if td.kind is TypeKind.FNRESULT and len(td.param_types) >= 2:
					err_id = td.param_types[1]
			declared_can_throw = sig.declared_can_throw
			if declared_can_throw is None and declared_can_throw_by_id is not None:
				if fn_id in declared_can_throw_by_id:
					declared_can_throw = bool(declared_can_throw_by_id[fn_id])
			if declared_can_throw is None:
				declared_can_throw = True
			if declared_can_throw is not False and err_id is None:
				err_id = shared_type_table.ensure_error()
			resolved_signatures[fn_id] = replace(
				sig,
				param_type_ids=param_ids,
				return_type_id=ret_id,
				error_type_id=err_id,
				declared_can_throw=bool(declared_can_throw),
			)
		base_signatures_by_id = resolved_signatures

	if drift_debug.enabled("type_prov") and shared_type_table is not None:
		shared_type_table.enable_type_provenance()

	derived_signatures_by_id: dict[FunctionId, FnSignature] = {}
	base_signatures_by_id = MappingProxyType(dict(base_signatures_by_id))

	def _record_signature_provenance(fn_id: FunctionId, sig: FnSignature) -> None:
		if shared_type_table is None or not shared_type_table.type_provenance_enabled():
			return
		span = getattr(sig, "loc", None)
		note = function_symbol(fn_id)
		for tid in sig.param_type_ids or []:
			shared_type_table.record_type_provenance(
				tid,
				phase="signature",
				kind="sig_param",
				span=span,
				note=note,
			)
		if sig.return_type_id is not None:
			shared_type_table.record_type_provenance(
				sig.return_type_id,
				phase="signature",
				kind="sig_return",
				span=span,
				note=note,
			)
		if sig.error_type_id is not None:
			shared_type_table.record_type_provenance(
				sig.error_type_id,
				phase="signature",
				kind="sig_error",
				span=span,
				note=note,
			)

	if shared_type_table is not None and shared_type_table.type_provenance_enabled():
		for fn_id, sig in base_signatures_by_id.items():
			_record_signature_provenance(fn_id, sig)
	_ensure_module_packages(
		shared_type_table,
		modules=_required_modules,
		package_id=package_id,
		allow_fill=True,
		context="compile_stubbed_funcs",
	)
	signatures_by_id: Mapping[FunctionId, FnSignature] = ChainMap(
		derived_signatures_by_id,
		base_signatures_by_id,
	)
	_assert_signature_map_split(
		base_signatures_by_id=base_signatures_by_id,
		derived_signatures_by_id=derived_signatures_by_id,
		context="compile_stubbed_funcs pre-synthesis",
	)

	def _register_derived_signature_precheck(fn_id: FunctionId, sig: FnSignature) -> None:
		existing = derived_signatures_by_id.get(fn_id) or base_signatures_by_id.get(fn_id)
		if existing is not None:
			if existing != sig:
				raise AssertionError(f"signature collision for '{function_symbol(fn_id)}'")
			return
		_record_signature_provenance(fn_id, sig)
		derived_signatures_by_id[fn_id] = sig

	method_wrapper_specs, wrapper_errors = _inject_method_boundary_wrappers(
		signatures_by_id=base_signatures_by_id,
		existing_ids=set(base_signatures_by_id.keys()) | set(derived_signatures_by_id.keys()),
		register_derived=_register_derived_signature_precheck,
		type_table=shared_type_table,
	)
	if wrapper_errors:
		raise ValueError(wrapper_errors[0])

	# Normalize before typecheck so the checker sees canonical HIR for diagnostics.
	with _timed("normalize_hir"):
		normalized_hirs_by_id: dict[FunctionId, H.HBlock] = {
			fn_id: normalize_hir(hir_block) for fn_id, hir_block in func_hirs_by_id.items()
		}
	if drift_debug.enabled("local_types_trace"):
		for fn_id, block in normalized_hirs_by_id.items():
			if getattr(fn_id, "module", None) != "main" or getattr(fn_id, "name", None) != "run":
				continue
			seen_expr_ids: dict[int, tuple[str, object]] = {}
			def _walk_expr_ids(obj: object) -> None:
				if isinstance(obj, H.HExpr):
					node_id = getattr(obj, "node_id", 0)
					if node_id == 0:
						return
					kind = type(obj).__name__
					span = getattr(obj, "loc", Span())
					prev = seen_expr_ids.get(node_id)
					if prev is None:
						seen_expr_ids[node_id] = (kind, span)
					else:
						prev_kind, prev_span = prev
						if prev_kind != kind:
							print(f"[drift:debug][local_types_trace] fn={fn_id} pre_typecheck_dup_node_id={node_id} prev={prev_kind} now={kind} prev_span={prev_span} now_span={span}", file=sys.stderr)
				if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
					return
				if is_dataclass(obj):
					for f in fields(obj):
						_walk_expr_ids(getattr(obj, f.name))
					return
				if isinstance(obj, (list, tuple)):
					for item in obj:
						_walk_expr_ids(item)
					return
				if isinstance(obj, dict):
					for key in sorted(obj.keys(), key=repr):
						_walk_expr_ids(obj[key])
					return
			_walk_expr_ids(block)

	# candidate_signatures_for_diag removed; no name-keyed fallback map
	unsafe_trusted_modules = set()
	if shared_type_table is not None:
		for mod_id, pkg_id in (getattr(shared_type_table, "module_packages", {}) or {}).items():
			if pkg_id == "std":
				unsafe_trusted_modules.add(mod_id)
	if not unsafe_trusted_modules and module_exports is not None:
		for mod_id in module_exports.keys():
			if isinstance(mod_id, str) and mod_id.startswith("std."):
				unsafe_trusted_modules.add(mod_id)
	if not unsafe_trusted_modules:
		for fn_id, sig in signatures_by_id.items():
			mod_name = getattr(fn_id, "module", None) or getattr(sig, "module", None)
			if isinstance(mod_name, str) and mod_name.startswith("std."):
				unsafe_trusted_modules.add(mod_name)
	type_checker = TypeChecker(type_table=shared_type_table, allow_unsafe=bool(allow_unsafe), unsafe_trusted_modules=unsafe_trusted_modules, allow_unsafe_without_block=True)
	callable_registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	if module_deps:
		all_mods = set(module_deps.keys())
		for deps in module_deps.values():
			all_mods |= set(deps)
		for mid in sorted(all_mods):
			module_ids.setdefault(mid, len(module_ids))
	else:
		for fn_id in signatures_by_id.keys():
			module_ids.setdefault(getattr(fn_id, "module", None), len(module_ids))
	visibility_provenance_by_id: dict[int, tuple[str, ...]] = {}
	for mod_name, mod_id in module_ids.items():
		if mod_name is None:
			continue
		visibility_provenance_by_id[int(mod_id)] = (str(mod_name),)
	def _module_id_with_visibility(name: object) -> int:
		mod_id = module_ids.setdefault(name, len(module_ids))
		if name is not None and int(mod_id) not in visibility_provenance_by_id:
			visibility_provenance_by_id[int(mod_id)] = (str(name),)
		return mod_id
	def _sync_visibility_provenance() -> None:
		for mod_name, mod_id in module_ids.items():
			if mod_name is None:
				continue
			visibility_provenance_by_id.setdefault(int(mod_id), (str(mod_name),))
	function_keys_by_fn_id: dict[FunctionId, FunctionKey] = {}
	if isinstance(template_keys_by_fn_id, dict):
		function_keys_by_fn_id.update(template_keys_by_fn_id)
	requires_by_fn_id: dict[FunctionId, object] = {}
	trait_worlds = getattr(shared_type_table, "trait_worlds", {}) if shared_type_table is not None else {}
	trait_world_diags: list[Diagnostic] = []
	if shared_type_table is not None and (external_trait_defs or external_impl_metas):
		from lang.driftc.traits.world import TraitWorld, ImplDef, type_key_from_expr

		if not isinstance(trait_worlds, dict):
			trait_worlds = {}
		default_package = getattr(shared_type_table, "package_id", None)
		module_packages = getattr(shared_type_table, "module_packages", None)
		def _module_package(mod: str | None) -> str | None:
			if mod is None:
				return default_package
			return (module_packages or {}).get(mod, default_package)

		def _trait_label(trait_key: object) -> str:
			mod = getattr(trait_key, "module", None)
			name = getattr(trait_key, "name", "")
			base = f"{mod}.{name}" if mod else name
			pkg = getattr(trait_key, "package_id", None)
			return f"{pkg}::{base}" if pkg else base

		def _ensure_world(mod: str | None) -> TraitWorld:
			key = mod or "main"
			world = trait_worlds.get(key)
			if world is None:
				world = TraitWorld()
				trait_worlds[key] = world
			return world

		if external_trait_defs:
			for trait_def in external_trait_defs:
				key = getattr(trait_def, "key", None)
				if key is None:
					continue
				world = _ensure_world(getattr(key, "module", None))
				world.traits.setdefault(key, trait_def)

		if external_impl_metas:
			for impl in external_impl_metas:
				if getattr(impl, "trait_key", None) is None:
					continue
				impl_pkg = _module_package(getattr(impl, "def_module", None))
				if impl_pkg != default_package:
					continue
				target_expr = getattr(impl, "target_expr", None)
				if target_expr is None:
					continue
				world = _ensure_world(getattr(impl, "def_module", None))
				target_key = type_key_from_expr(
					target_expr,
					default_module=getattr(impl, "def_module", None),
					default_package=default_package,
					module_packages=module_packages,
				)
				head_key = target_key.head()
				local_pkg = default_package
				trait_pkg = getattr(impl.trait_key, "package_id", None) or local_pkg
				target_pkg = getattr(head_key, "package_id", None) or local_pkg
				def _is_local(pkg: str | None) -> bool:
					return pkg is None or pkg == local_pkg
				if not _is_local(trait_pkg) and not _is_local(target_pkg):
					trait_world_diags.append(
						Diagnostic(
							message=(
								"orphan trait impl is not allowed: "
								f"trait '{_trait_label(impl.trait_key)}' and "
								f"type '{head_key.module}.{head_key.name}' are outside the current package"
							),
							code="E-IMPL-ORPHAN",
							severity="error",
							phase="typecheck",
							span=getattr(impl, "loc", None),
						)
					)
					continue
				existing_ids = world.impls_by_trait_target.get((impl.trait_key, head_key), [])
				impl_trait_args = tuple(
					type_key_from_typeid(shared_type_table, tid)
					for tid in (getattr(impl, "trait_args", []) or [])
				)
				dup = False
				if existing_ids:
					for impl_id in existing_ids:
						existing = world.impls[impl_id]
						if existing.target == target_key and existing.trait_args == impl_trait_args and existing.require == getattr(impl, "require_expr", None):
							dup = True
							break
				if dup:
					continue
				impl_def = ImplDef(
					trait=impl.trait_key,
					trait_args=impl_trait_args,
					target=target_key,
					target_head=head_key,
					methods=[],
					require=getattr(impl, "require_expr", None),
					loc=getattr(impl, "loc", None),
				)
				impl_id = len(world.impls)
				world.impls.append(impl_def)
				world.impls_by_trait.setdefault(impl_def.trait, []).append(impl_id)
				world.impls_by_target_head.setdefault(impl_def.target_head, []).append(impl_id)
				world.impls_by_trait_target.setdefault((impl_def.trait, impl_def.target_head), []).append(impl_id)

		shared_type_table.trait_worlds = trait_worlds
		if hasattr(shared_type_table, "_global_trait_world"):
			delattr(shared_type_table, "_global_trait_world")
	if isinstance(trait_worlds, dict):
		for world in trait_worlds.values():
			for fn_id, req in getattr(world, "requires_by_fn", {}).items():
				requires_by_fn_id[fn_id] = req
	linked_world, require_env = _build_linked_world(shared_type_table)
	_install_destructor_fns(shared_type_table, linked_world, module_exports)

	def _declared_name_from_fn_id(fn_id: FunctionId, module_id: str) -> str:
		sym = function_symbol(fn_id)
		name = sym
		prefix = f"{module_id}::"
		if name.startswith(prefix):
			name = name[len(prefix) :]
		if "#" in name:
			base, ord_text = name.rsplit("#", 1)
			if ord_text.isdigit():
				name = base
		return name

	local_package_id = package_id
	default_package = getattr(shared_type_table, "package_id", None) or package_id
	module_packages = getattr(shared_type_table, "module_packages", None)
	for fn_id, sig in signatures_by_id.items():
		if not (getattr(sig, "type_params", []) or getattr(sig, "impl_type_params", [])):
			continue
		if fn_id in function_keys_by_fn_id:
			continue
		module_id = getattr(sig, "module", None) or getattr(fn_id, "module", None) or "main"
		declared_name = _declared_name_from_fn_id(fn_id, module_id)
		if sig.param_types is None or sig.return_type is None:
			raise ValueError(
				f"TemplateHIR-v1 requires TypeExpr signatures for '{function_symbol(fn_id)}'"
			)
		req_expr = requires_by_fn_id.get(fn_id)
		decl_fp, _layout = compute_template_decl_fingerprint(
			sig,
			declared_name=declared_name,
			module_id=module_id,
			require_expr=req_expr if req_expr is not None else None,
			default_package=default_package,
			module_packages=module_packages,
		)
		function_keys_by_fn_id[fn_id] = FunctionKey(
			package_id=local_package_id,
			module_path=module_id,
			name=declared_name,
			decl_fingerprint=decl_fp,
		)
	next_callable_id = 1
	def _registry_impl_target_type_id(impl_tid: TypeId | None) -> TypeId | None:
		if impl_tid is None or shared_type_table is None:
			return impl_tid
		td = shared_type_table.get(impl_tid)
		if td.kind is TypeKind.REF and td.param_types:
			inner = td.param_types[0]
			inner_def = shared_type_table.get(inner)
			if inner_def.kind is TypeKind.ARRAY:
				return shared_type_table.array_base_id()
			return inner
		if td.kind is TypeKind.ARRAY:
			return shared_type_table.array_base_id()
		if td.kind is TypeKind.VARIANT:
			inst = shared_type_table.get_variant_instance(impl_tid)
			if inst is not None:
				return inst.base_id
		return impl_tid
	def _template_sig_for(sig: FnSignature) -> CallableTemplateSignature | None:
		if not (sig.type_params or getattr(sig, "impl_type_params", [])):
			return None
		if sig.param_types is None or sig.return_type is None:
			return None
		return CallableTemplateSignature(param_types=tuple(sig.param_types), result_type=sig.return_type)
	for fn_id, sig in signatures_by_id.items():
		if sig.return_type_id is None:
			continue
		if getattr(sig, "is_wrapper", False):
			continue
		module_name = getattr(fn_id, "module", None) or getattr(sig, "module", None)
		module_id = module_ids.setdefault(module_name, len(module_ids))
		param_types_tuple = tuple(sig.param_type_ids or [])
		if sig.is_method:
			if sig.impl_target_type_id is None or sig.self_mode is None:
				continue
			self_mode = {
				"value": SelfMode.SELF_BY_VALUE,
				"ref": SelfMode.SELF_BY_REF,
				"ref_mut": SelfMode.SELF_BY_REF_MUT,
			}.get(sig.self_mode)
			if self_mode is None:
				continue
			callable_registry.register_inherent_method(
				callable_id=next_callable_id,
				name=sig.method_name or sig.name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				template_signature=_template_sig_for(sig),
				template_type_params=tuple(tp.name for tp in (sig.type_params or [])),
				template_impl_type_params=tuple(tp.name for tp in (getattr(sig, "impl_type_params", []) or [])),
				fn_id=fn_id,
				impl_id=next_callable_id,
				impl_target_type_id=_registry_impl_target_type_id(sig.impl_target_type_id),
				self_mode=self_mode,
				is_generic=bool(sig.type_params or getattr(sig, "impl_type_params", [])),
			)
			next_callable_id += 1
		else:
			callable_registry.register_free_function(
				callable_id=next_callable_id,
				name=fn_id.name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				template_signature=_template_sig_for(sig),
				template_type_params=tuple(tp.name for tp in (sig.type_params or [])),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
			next_callable_id += 1
	# Optional method/trait resolution support when module exports/deps are available.
	impl_index = None
	trait_index = None
	trait_impl_index = None
	trait_scope_by_module: dict[str, list] | None = None
	if module_exports is not None:
		impl_index = GlobalImplIndex.from_module_exports(
			module_exports=dict(module_exports),
			type_table=shared_type_table,
			module_ids=module_ids,
		)
		trait_index = GlobalTraitIndex.from_trait_worlds(getattr(shared_type_table, "trait_worlds", None))
		trait_impl_index = GlobalTraitImplIndex.from_module_exports(
			module_exports=dict(module_exports),
			type_table=shared_type_table,
			module_ids=module_ids,
		)
		if external_impl_metas:
			for impl in external_impl_metas:
				if getattr(impl, "trait_key", None) is None and impl_index is not None:
					impl_index.add_impl(impl=impl, type_table=shared_type_table, module_ids=module_ids)
				if getattr(impl, "trait_key", None) is not None and trait_impl_index is not None:
					trait_impl_index.add_impl(impl=impl, type_table=shared_type_table, module_ids=module_ids)
		if external_trait_defs and trait_index is not None:
			for trait_def in external_trait_defs:
				if hasattr(trait_def, "key"):
					trait_index.add_trait(trait_def.key, trait_def)
		if external_missing_traits and trait_index is not None:
			for missing_trait in external_missing_traits:
				if hasattr(missing_trait, "module") and hasattr(missing_trait, "name"):
					trait_index.mark_missing(missing_trait)
		if external_missing_impl_modules and trait_impl_index is not None:
			for module_id in external_missing_impl_modules:
				trait_impl_index.mark_missing_module(module_ids.setdefault(module_id, len(module_ids)))
		trait_scope_by_module = {}
		for mod, exp in module_exports.items():
			if isinstance(exp, dict):
				scope = exp.get("trait_scope", [])
				if isinstance(scope, list):
					trait_scope_by_module[mod] = scope
	typed_fns_by_id: dict[FunctionId, object] = {}
	type_diags: list[Diagnostic] = []
	if trait_world_diags:
		type_diags.extend(trait_world_diags)
	if shared_type_table is not None:
		type_checker.validate_interface_schemas(diagnostics=type_diags)
		if module_exports is not None:
			interface_impls: list[ImplMeta] = []
			for exp in module_exports.values():
				if isinstance(exp, dict):
					for impl in exp.get("impls", []) or []:
						if isinstance(impl, ImplMeta):
							interface_impls.append(impl)
			if interface_impls:
				type_checker.validate_interface_impls(
					interface_impls,
					signatures_by_id=signatures_by_id,
					diagnostics=type_diags,
				)
		if module_exports is not None:
			interface_impls: list[ImplMeta] = []
			for exp in module_exports.values():
				if isinstance(exp, dict):
					for impl in exp.get("impls", []) or []:
						if isinstance(impl, ImplMeta):
							interface_impls.append(impl)
			if interface_impls:
				type_checker.validate_interface_impls(
					interface_impls,
					signatures_by_id=signatures_by_id,
					diagnostics=type_diags,
				)
		if module_exports is not None:
			trait_impls: list[ImplMeta] = []
			for exp in module_exports.values():
				if isinstance(exp, dict):
					for impl in exp.get("impls", []) or []:
						if isinstance(impl, ImplMeta):
							trait_impls.append(impl)
			if trait_impls:
				type_checker.validate_trait_impls(
					trait_impls,
					signatures_by_id=signatures_by_id,
					trait_index=trait_index,
					diagnostics=type_diags,
				)
	typecheck_ok_by_fn: dict[FunctionId, bool] = {}
	deferred_guard_diags_by_template: dict[FunctionKey, dict[tuple[object, str], list[Diagnostic]]] = {}
	def _has_error(diags: list[Diagnostic]) -> bool:
		return any(getattr(d, "severity", None) == "error" for d in diags)
	visible_module_names_by_name: dict[str, set[str]] = {}
	prelude_modules: set[str] = set()
	if prelude_enabled:
		for fn_id in signatures_by_id.keys():
			if fn_id.module == "lang.core":
				prelude_modules.add("lang.core")
				break
		if isinstance(module_exports, dict):
			for std_mod in ("std.iter", "std.containers"):
				if std_mod in module_exports:
					prelude_modules.add(std_mod)
	if module_deps is not None:
		def _collect_reexport_targets(mod: str) -> set[str]:
			exp = module_exports.get(mod) if isinstance(module_exports, dict) else None
			if not isinstance(exp, dict):
				return set()
			reexp = exp.get("reexports")
			if not isinstance(reexp, dict):
				return set()
			targets: set[str] = set()
			type_reexp = reexp.get("types") if isinstance(reexp.get("types"), dict) else {}
			for kind in ("structs", "variants", "exceptions", "interfaces"):
				entries = type_reexp.get(kind) if isinstance(type_reexp, dict) else None
				if not isinstance(entries, dict):
					continue
				for info in entries.values():
					if isinstance(info, dict):
						tgt = info.get("module")
						if isinstance(tgt, str):
							targets.add(tgt)
			const_reexp = reexp.get("consts") if isinstance(reexp.get("consts"), dict) else {}
			if isinstance(const_reexp, dict):
				for info in const_reexp.values():
					if isinstance(info, dict):
						tgt = info.get("module")
						if isinstance(tgt, str):
							targets.add(tgt)
			trait_reexp = reexp.get("traits") if isinstance(reexp.get("traits"), dict) else {}
			if isinstance(trait_reexp, dict):
				for info in trait_reexp.values():
					if isinstance(info, dict):
						tgt = info.get("module")
						if isinstance(tgt, str):
							targets.add(tgt)
			value_reexp = reexp.get("values") if isinstance(reexp.get("values"), dict) else {}
			if isinstance(value_reexp, dict):
				for info in value_reexp.values():
					if isinstance(info, dict):
						tgt = info.get("module")
						if isinstance(tgt, str):
							targets.add(tgt)
			return targets

		for mod_name in module_deps.keys():
			imports = set(module_deps.get(mod_name, set()))
			visible = {mod_name}
			if prelude_modules:
				visible |= prelude_modules
			queue = [mod_name]
			while queue:
				cur = queue.pop(0)
				neighbors = set(_collect_reexport_targets(cur))
				if cur == mod_name:
					neighbors |= imports
				for tgt in sorted(neighbors):
					if tgt in visible:
						continue
					visible.add(tgt)
					queue.append(tgt)
			visible_module_names_by_name[mod_name] = visible
	def _typecheck_fn(fn_id: FunctionId, hir_norm: H.HBlock) -> None:
		sig = signatures_by_id.get(fn_id)
		param_types: dict[str, "TypeId"] = {}
		param_mutable: dict[str, bool] | None = None
		if sig is not None and sig.param_names is not None and sig.param_type_ids is not None:
			param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids)}
		if sig is not None and sig.param_names is not None and sig.param_mutable is not None:
			if len(sig.param_names) == len(sig.param_mutable):
				param_mutable = {pname: bool(flag) for pname, flag in zip(sig.param_names, sig.param_mutable)}
		current_file = None
		if origin_by_fn_id is not None and fn_id in origin_by_fn_id:
			current_file = str(origin_by_fn_id.get(fn_id))
		elif sig is not None:
			current_file = Span.from_loc(getattr(sig, "loc", None)).file
		mod_name = getattr(fn_id, "module", None) or "main"
		current_mod = _module_id_with_visibility(mod_name)
		visible_mods = None
		if module_deps is not None:
			visible_mods = tuple(sorted(_module_id_with_visibility(m) for m in module_ids.keys() if m is not None))
		ret_id = sig.return_type_id if sig is not None else None
		type_param_map: dict[str, object] = {}
		if sig is not None and (getattr(sig, "impl_type_params", None) or getattr(sig, "type_params", None)):
			for p in (list(getattr(sig, "impl_type_params", []) or []) + list(getattr(sig, "type_params", []) or [])):
				type_param_map[p.name] = p.id
		if sig is not None and sig.return_type is not None:
			if ret_id is None or (shared_type_table.get(ret_id).kind is TypeKind.UNKNOWN):
				try:
					ret_id = resolve_opaque_type(sig.return_type, shared_type_table, module_id=getattr(sig, "module", None), type_params=type_param_map or None)
				except Exception:
					ret_id = None
		_sync_visibility_provenance()
		result = type_checker.check_function(
			fn_id,
			hir_norm,
			param_types=param_types,
			param_mutable=param_mutable,
			return_type=ret_id,
			preseed_scope_bindings=getattr(hir_norm, "param_binding_ids", None),
			signatures_by_id=signatures_by_id,
			function_keys_by_fn_id=function_keys_by_fn_id,
			callable_registry=callable_registry,
			impl_index=impl_index,
			trait_index=trait_index,
			trait_impl_index=trait_impl_index,
			trait_scope_by_module=trait_scope_by_module,
			linked_world=linked_world,
			require_env=require_env,
			visible_modules=visible_mods,
			current_module=current_mod,
			visibility_provenance=visibility_provenance_by_id,
		)
		type_diags.extend(result.diagnostics)
		typecheck_ok_by_fn[fn_id] = not _has_error(result.diagnostics)
		deferred = getattr(result, "deferred_guard_diags", None)
		if deferred:
			fn_key = function_keys_by_fn_id.get(fn_id) if function_keys_by_fn_id else None
			if fn_key is not None:
				deferred_guard_diags_by_template[fn_key] = dict(deferred)
		typed_fns_by_id[fn_id] = result.typed_fn

	with _timed("typecheck"):
		for fn_id, hir_norm in normalized_hirs_by_id.items():
			if drift_debug.enabled("try_auto") and getattr(fn_id, "module", None) == "m":
				import sys as _try_auto_sys2
				sig_dbg = signatures_by_id.get(fn_id)
				print(f"[try_auto] pre-typecheck {function_symbol(fn_id)} sig_id={id(sig_dbg)} declared_throws={getattr(sig_dbg, 'declared_throws', None)} sig_map_id={id(signatures_by_id)} sig_map_type={type(signatures_by_id).__name__}", file=_try_auto_sys2.stderr)
			_typecheck_fn(fn_id, hir_norm)
	if type_checker.defaulted_phase_count() != 0:
		raise AssertionError(
			f"typecheck diagnostics missing phase (defaulted={type_checker.defaulted_phase_count()})"
		)

	# Use the type-checked HIR directly so callsite ids stay aligned with CallInfo.
	# Pre-typecheck normalization already produced canonical forms; the checker
	# only injects nodes like HFnPtrConst without breaking normalization.
	normalized_hirs_by_id = {}
	for fn_id, typed_fn in typed_fns_by_id.items():
		block = getattr(typed_fn, "body", None)
		if not isinstance(block, H.HBlock):
			continue
		normalized_hirs_by_id[fn_id] = block
	if drift_debug.enabled("local_types_trace"):
		for fn_id, block in normalized_hirs_by_id.items():
			if getattr(fn_id, "module", None) != "main" or getattr(fn_id, "name", None) != "run":
				continue
			seen_expr_ids: dict[int, tuple[str, object]] = {}
			def _walk_expr_ids(obj: object) -> None:
				if isinstance(obj, H.HExpr):
					node_id = getattr(obj, "node_id", 0)
					if node_id == 0:
						return
					kind = type(obj).__name__
					span = getattr(obj, "loc", Span())
					prev = seen_expr_ids.get(node_id)
					if prev is None:
						seen_expr_ids[node_id] = (kind, span)
					else:
						prev_kind, prev_span = prev
						if prev_kind != kind:
							print(f"[drift:debug][local_types_trace] fn={fn_id} post_typecheck_dup_node_id={node_id} prev={prev_kind} now={kind} prev_span={prev_span} now_span={span}", file=sys.stderr)
				if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
					return
				if is_dataclass(obj):
					for f in fields(obj):
						_walk_expr_ids(getattr(obj, f.name))
					return
				if isinstance(obj, (list, tuple)):
					for item in obj:
						_walk_expr_ids(item)
					return
				if isinstance(obj, dict):
					for key in sorted(obj.keys(), key=repr):
						_walk_expr_ids(obj[key])
					return
			_walk_expr_ids(block)
		for fn_id, typed_fn in typed_fns_by_id.items():
			if getattr(fn_id, "module", None) != "main" or getattr(fn_id, "name", None) != "run":
				continue
			block = getattr(typed_fn, "body", None)
			if not isinstance(block, H.HBlock):
				continue
			import sys as _dbg_sys
			print(f"[drift:debug][local_types_trace] fn={fn_id} scan=post_typecheck_typed", file=_dbg_sys.stderr)
			seen_expr_ids: dict[int, tuple[str, object]] = {}
			def _walk_expr_ids_typed(obj: object) -> None:
				if isinstance(obj, H.HExpr):
					node_id = getattr(obj, "node_id", 0)
					if node_id == 0:
						return
					kind = type(obj).__name__
					span = getattr(obj, "loc", Span())
					prev = seen_expr_ids.get(node_id)
					if prev is None:
						seen_expr_ids[node_id] = (kind, span)
					else:
						prev_kind, prev_span = prev
						if prev_kind != kind:
							print(f"[drift:debug][local_types_trace] fn={fn_id} post_typecheck_typed_dup_node_id={node_id} prev={prev_kind} now={kind} prev_span={prev_span} now_span={span}", file=_dbg_sys.stderr)
				if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
					return
				if is_dataclass(obj):
					for f in fields(obj):
						_walk_expr_ids_typed(getattr(obj, f.name))
					return
				if isinstance(obj, (list, tuple)):
					for item in obj:
						_walk_expr_ids_typed(item)
					return
				if isinstance(obj, dict):
					for key in sorted(obj.keys(), key=repr):
						_walk_expr_ids_typed(obj[key])
					return
			_walk_expr_ids_typed(block)
	if drift_debug.enabled("ssa"):
		import sys as _ssa_dbg_sys
		for fn_id, block in normalized_hirs_by_id.items():
			if getattr(fn_id, "module", None) != "main":
				continue
			for stmt in block.statements:
				if isinstance(stmt, H.HReturn):
					span = Span.from_loc(getattr(stmt, "loc", None))
					print(f"[drift:debug][hir] return loc={span}", file=_ssa_dbg_sys.stderr)

	# Instantiation phase: clone generic templates into concrete instantiations
	# and rewrite call targets.
	from lang.driftc.core.type_subst import Subst, apply_subst
	from lang.driftc.instantiation.key import (
		InstantiationKey,
		build_instantiation_key,
		instantiation_key_hash,
		instantiation_key_str,
	)
	from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTargetKind
	from lang.driftc.method_resolver import MethodResolution
	from collections import deque

	def _make_wrapper_template_hir(wrap_sig: FnSignature) -> H.HBlock:
		param_names = list(wrap_sig.param_names or [])
		if not param_names and wrap_sig.param_type_ids is not None:
			param_names = [f"p{i}" for i in range(len(wrap_sig.param_type_ids))]
		receiver = H.HVar(name=param_names[0]) if param_names else H.HVar(name="self")
		args = [H.HVar(name=name) for name in param_names[1:]]
		method_name = getattr(wrap_sig, "method_name", None) or wrap_sig.name
		call_expr = H.HMethodCall(receiver=receiver, method_name=method_name, args=args)
		is_void = bool(wrap_sig.return_type_id is not None and shared_type_table.is_void(wrap_sig.return_type_id))
		if is_void:
			block = H.HBlock(statements=[H.HExprStmt(expr=call_expr), H.HReturn(value=None)])
		else:
			block = H.HBlock(statements=[H.HReturn(value=call_expr)])
		assign_node_ids(block, start=1)
		assign_callsite_ids(block, start=1)
		return block

	template_hirs_by_key: dict[FunctionKey, H.HBlock] = {}
	if isinstance(generic_templates_by_key, dict):
		template_hirs_by_key.update(generic_templates_by_key)

	def _clear_var_binding_ids(block: H.HBlock) -> None:
		def walk_expr(expr: H.HExpr) -> None:
			if isinstance(expr, H.HVar):
				if expr.binding_id is not None:
					expr.binding_id = None
				return
			if isinstance(expr, getattr(H, "HPlaceExpr", ())):
				walk_expr(expr.base)
				for proj in expr.projections:
					if isinstance(proj, H.HPlaceIndex):
						walk_expr(proj.index)
				return
			if isinstance(expr, H.HCall):
				walk_expr(expr.fn)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
				return
			if isinstance(expr, H.HMethodCall):
				walk_expr(expr.receiver)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
				return
			if isinstance(expr, H.HField):
				walk_expr(expr.subject)
				return
			if isinstance(expr, H.HIndex):
				walk_expr(expr.subject)
				walk_expr(expr.index)
				return
			if isinstance(expr, H.HArrayLiteral):
				for elem in expr.elements:
					walk_expr(elem)
				return
			if isinstance(expr, H.HFString):
				for hole in expr.holes:
					walk_expr(hole.expr)
				return
			if isinstance(expr, H.HLambda):
				for param in expr.params:
					if param.binding_id is not None:
						param.binding_id = None
				for cap in expr.explicit_captures or []:
					if cap.binding_id is not None:
						cap.binding_id = None
				if expr.body_expr is not None:
					walk_expr(expr.body_expr)
				if expr.body_block is not None:
					walk_block(expr.body_block)
				return
			if isinstance(expr, H.HResultOk):
				walk_expr(expr.value)
				return
			if isinstance(expr, H.HExceptionInit):
				for arg in expr.pos_args:
					walk_expr(arg)
				for kw in expr.kw_args:
					walk_expr(kw.value)
				return
			if isinstance(expr, H.HTryExpr):
				walk_expr(expr.attempt)
				for arm in expr.arms:
					walk_block(arm.block)
					if arm.result is not None:
						walk_expr(arm.result)
				return
			if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				walk_block(expr.body)
				walk_expr(expr.result)
				return
			if isinstance(expr, H.HMatchExpr):
				walk_expr(expr.scrutinee)
				for arm in expr.arms:
					walk_block(arm.block)
					if arm.result is not None:
						walk_expr(arm.result)
				return

		def walk_stmt(stmt: H.HStmt) -> None:
			if isinstance(stmt, H.HLocalConst):
				return  # literal value
			if isinstance(stmt, H.HLet):
				walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HAssign):
				walk_expr(stmt.target)
				walk_expr(stmt.value)
				return
			if hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
				walk_expr(stmt.target)
				walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HExprStmt):
				walk_expr(stmt.expr)
				return
			if isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HIf):
				walk_expr(stmt.cond)
				walk_block(stmt.then_block)
				if stmt.else_block is not None:
					walk_block(stmt.else_block)
				return
			if isinstance(stmt, H.HLoop):
				walk_block(stmt.body)
				return
			if isinstance(stmt, H.HBlock):
				walk_block(stmt)
				return
			if hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				walk_block(stmt.block)
				return
			if isinstance(stmt, H.HTry):
				walk_block(stmt.body)
				for arm in stmt.catches:
					walk_block(arm.block)
				return
			if isinstance(stmt, H.HThrow):
				walk_expr(stmt.value)
				return

		def walk_block(block: H.HBlock) -> None:
			for stmt in block.statements:
				walk_stmt(stmt)

		walk_block(block)
	if isinstance(generic_templates_by_id, dict):
		for fn_id, hir in generic_templates_by_id.items():
			key = function_keys_by_fn_id.get(fn_id)
			if key is None:
				continue
			template_hirs_by_key.setdefault(key, hir)
	for fn_id, block in normalized_hirs_by_id.items():
		sig = signatures_by_id.get(fn_id)
		if sig and (sig.type_params or getattr(sig, "impl_type_params", [])):
			key = function_keys_by_fn_id.get(fn_id)
			if key is None:
				continue
			template_hirs_by_key.setdefault(key, block)
	if method_wrapper_specs and shared_type_table is not None:
		for spec in method_wrapper_specs:
			wrap_sig = signatures_by_id.get(spec.wrapper_fn_id)
			if wrap_sig is None:
				continue
			if not (getattr(wrap_sig, "type_params", None) or getattr(wrap_sig, "impl_type_params", None)):
				continue
			key = function_keys_by_fn_id.get(spec.wrapper_fn_id)
			if key is None or key in template_hirs_by_key:
				continue
			template_hirs_by_key[key] = _make_wrapper_template_hir(wrap_sig)

	template_sigs_by_key: dict[FunctionKey, FnSignature] = {}
	for fn_id, sig in signatures_by_id.items():
		if not (sig.type_params or getattr(sig, "impl_type_params", [])):
			continue
		key = function_keys_by_fn_id.get(fn_id)
		if key is None:
			continue
		template_sigs_by_key.setdefault(key, sig)

	template_fn_id_by_key: dict[FunctionKey, FunctionId] = {}
	for fn_id, key in function_keys_by_fn_id.items():
		template_fn_id_by_key.setdefault(key, fn_id)

	def _inst_can_throw(sig: FnSignature | None) -> bool:
		if sig is None:
			return True
		return _sig_declared_can_throw(sig)

	def _inst_key(fn_key: FunctionKey, type_args: tuple[TypeId, ...]) -> InstantiationKey:
		return build_instantiation_key(
			fn_key,
			type_args,
			type_table=shared_type_table,
			can_throw=_inst_can_throw(template_sigs_by_key.get(fn_key)),
		)

	def _inst_hash(key: InstantiationKey) -> str:
		return instantiation_key_hash(key)

	def _diag_key(diag: Diagnostic) -> tuple[str, tuple[object, object, object, object, object]]:
		span = getattr(diag, "span", None) or Span()
		span_key = (span.file, span.line, span.column, span.end_line, span.end_column)
		return (diag.message, span_key)

	def _apply_inst_subst(ty: TypeId, impl_subst: Subst | None, fn_subst: Subst | None) -> TypeId:
		out = ty
		if impl_subst is not None:
			out = apply_subst(out, impl_subst, shared_type_table)
		if fn_subst is not None:
			out = apply_subst(out, fn_subst, shared_type_table)
		return out

	def _subst_call_info(info: CallInfo, impl_subst: Subst | None, fn_subst: Subst | None) -> CallInfo:
		sig = info.sig
		new_params = tuple(_apply_inst_subst(t, impl_subst, fn_subst) for t in sig.param_types)
		new_ret = _apply_inst_subst(sig.user_ret_type, impl_subst, fn_subst)
		new_sig = CallSig(param_types=new_params, user_ret_type=new_ret, can_throw=sig.can_throw, includes_callee=sig.includes_callee)
		target = info.target
		if target.kind is CallTargetKind.CONSTRUCTOR:
			if target.variant_type_id is not None:
				new_variant = _apply_inst_subst(target.variant_type_id, impl_subst, fn_subst)
				target = replace(target, variant_type_id=new_variant)
			elif target.struct_type_id is not None:
				new_struct = _apply_inst_subst(target.struct_type_id, impl_subst, fn_subst)
				target = replace(target, struct_type_id=new_struct)
		return CallInfo(target=target, sig=new_sig)

	def _subst_with_owner_map(ty: TypeId, param_map: dict[TypeParamId, TypeId]) -> TypeId:
		if not param_map:
			return ty
		by_owner: dict[FunctionId, dict[int, TypeId]] = {}
		for param_id, concrete in param_map.items():
			owner_map = by_owner.setdefault(param_id.owner, {})
			owner_map[param_id.index] = concrete
		out = ty
		for owner, owner_map in by_owner.items():
			max_idx = max(owner_map.keys(), default=-1)
			args: list[TypeId] = []
			for idx in range(max_idx + 1):
				if idx in owner_map:
					args.append(owner_map[idx])
				else:
					args.append(shared_type_table.ensure_typevar(TypeParamId(owner=owner, index=idx)))
			out = apply_subst(out, Subst(owner=owner, args=args), shared_type_table)
		return out

	@dataclass
	class InstantiationHandle:
		key: InstantiationKey
		template_key: FunctionKey
		type_args: tuple[TypeId, ...]
		fn_id: FunctionId
		status: str  # "pending"|"emitted"|"failed"

	inst_cache: dict[InstantiationKey, InstantiationHandle] = {}
	inst_queue: deque[InstantiationHandle] = deque()

	def _request_instantiation(template_key: FunctionKey, type_args: tuple[TypeId, ...]) -> InstantiationHandle:
		key = _inst_key(template_key, tuple(type_args))
		handle = inst_cache.get(key)
		if handle is not None:
			return handle
		inst_name = f"{template_key.name}__inst__{_inst_hash(key)}"
		inst_fn_id = FunctionId(module=template_key.module_path, name=inst_name, ordinal=0)
		handle = InstantiationHandle(
			key=key,
			template_key=template_key,
			type_args=tuple(type_args),
			fn_id=inst_fn_id,
			status="pending",
		)
		inst_cache[key] = handle
		inst_queue.append(handle)
		return handle

	destructor_fns: dict[TypeId, FunctionId] = {}
	if shared_type_table is not None and module_exports is not None and linked_world is not None:
		destructible_key = _find_trait_key(linked_world.global_world, module="std.core", name="Destructible")
		if destructible_key is not None:
			for exp in module_exports.values():
				if not isinstance(exp, dict):
					continue
				impls = exp.get("impls")
				if not isinstance(impls, list):
					continue
				for impl in impls:
					if not isinstance(impl, ImplMeta):
						continue
					if impl.trait_key != destructible_key:
						continue
					target_type_id = getattr(impl, "target_type_id", None)
					if not isinstance(target_type_id, int):
						continue
					method_fn_id: FunctionId | None = None
					for method in impl.methods:
						if method.name == "destroy":
							method_fn_id = method.fn_id
							break
					if method_fn_id is None:
						continue
					if shared_type_table.has_typevar(target_type_id):
						base_id: TypeId | None = None
						inst = shared_type_table.get_struct_instance(target_type_id)
						if inst is not None:
							base_id = inst.base_id
						else:
							base_id = target_type_id
						for inst_id, inst in shared_type_table.struct_instances.items():
							if inst.base_id != base_id:
								continue
							if shared_type_table.has_typevar(inst_id):
								continue
							key = function_keys_by_fn_id.get(method_fn_id)
							if key is None:
								continue
							handle = _request_instantiation(key, tuple(inst.type_args))
							destructor_fns[inst_id] = handle.fn_id
						continue
					destructor_fns[target_type_id] = method_fn_id
	if destructor_fns and shared_type_table is not None:
		shared_type_table.destructor_fns = destructor_fns

	def _queue_instantiations(typed_fn: object) -> None:
		inst_map = getattr(typed_fn, "instantiations_by_callsite_id", None)
		if not isinstance(inst_map, dict):
			inst_map = {}
		inst_map_by_node = getattr(typed_fn, "instantiations_by_node_id", None)
		if not isinstance(inst_map_by_node, dict):
			inst_map_by_node = {}
		for inst in list(inst_map.values()) + list(inst_map_by_node.values()):
			type_args = tuple(getattr(inst, "type_args", ()) or ())
			if not type_args:
				continue
			template_key = getattr(inst, "target_key", None)
			if not isinstance(template_key, FunctionKey):
				continue
			_request_instantiation(template_key, type_args)

	for _fn_id, typed_fn in sorted(typed_fns_by_id.items(), key=lambda kv: function_symbol(kv[0])):
		_queue_instantiations(typed_fn)

	def _drain_instantiations() -> None:
		while inst_queue:
			handle = inst_queue.popleft()
			if handle.status == "emitted":
				raise AssertionError(
					f"duplicate instantiation emission for '{function_key_str(handle.template_key)}' ({instantiation_key_str(handle.key)})"
				)
			if handle.status != "pending":
				raise AssertionError(f"unexpected instantiation status: {handle.status}")
			template_key = handle.template_key
			type_args = handle.type_args
			sig = template_sigs_by_key.get(template_key)
			template_fn_id = template_fn_id_by_key.get(template_key)
			if sig is None or template_fn_id is None:
				type_diags.append(
					Diagnostic(
						message=f"generic instantiation missing signature for '{function_key_str(template_key)}'",
						code="E_MISSING_TEMPLATE_SIG",
						severity="error",
						phase="typecheck",
						span=None,
					)
				)
				handle.status = "failed"
				continue
			impl_count = len(getattr(sig, "impl_type_params", []) or [])
			fn_count = len(getattr(sig, "type_params", []) or [])
			if len(type_args) != impl_count + fn_count:
				type_diags.append(
					Diagnostic(
						message=(
							"generic instantiation type argument mismatch for "
							f"'{function_key_str(template_key)}' (expected {impl_count + fn_count}, got {len(type_args)})"
						),
						code="E_INSTANTIATION_TYPEARGS",
						severity="error",
						phase="typecheck",
						span=getattr(sig, "loc", None),
					)
				)
				handle.status = "failed"
				continue
			if sig.param_type_ids is None or sig.return_type_id is None:
				type_diags.append(
					Diagnostic(
						message=f"generic instantiation missing type ids for '{function_key_str(template_key)}'",
						code="E_MISSING_TEMPLATE_SIG",
						severity="error",
						phase="typecheck",
						span=getattr(sig, "loc", None),
					)
				)
				handle.status = "failed"
				continue
			impl_args = type_args[:impl_count]
			fn_args = type_args[impl_count:]
			inst_fn_id = handle.fn_id
			inst_param_ids = list(sig.param_type_ids)
			inst_ret_id = sig.return_type_id
			inst_impl_target_id = sig.impl_target_type_id
			impl_subst = None
			fn_subst = None
			if impl_args:
				impl_owner = sig.impl_type_params[0].id.owner
				impl_subst = Subst(owner=impl_owner, args=list(impl_args))
				inst_param_ids = [apply_subst(t, impl_subst, shared_type_table) for t in inst_param_ids]
				inst_ret_id = apply_subst(inst_ret_id, impl_subst, shared_type_table)
				if inst_impl_target_id is not None:
					inst_impl_target_id = apply_subst(inst_impl_target_id, impl_subst, shared_type_table)
			if fn_args:
				fn_subst = Subst(owner=template_fn_id, args=list(fn_args))
				inst_param_ids = [apply_subst(t, fn_subst, shared_type_table) for t in inst_param_ids]
				inst_ret_id = apply_subst(inst_ret_id, fn_subst, shared_type_table)
				if inst_impl_target_id is not None:
					inst_impl_target_id = apply_subst(inst_impl_target_id, fn_subst, shared_type_table)
			inst_impl_target_args = None
			if sig.impl_target_type_args is not None:
				inst_impl_target_args = list(sig.impl_target_type_args)
				if impl_args:
					impl_owner = sig.impl_type_params[0].id.owner
					impl_subst = Subst(owner=impl_owner, args=list(impl_args))
					inst_impl_target_args = [
						apply_subst(t, impl_subst, shared_type_table) for t in inst_impl_target_args
					]
			param_map: dict[TypeParamId, TypeId] = {}
			if getattr(sig, "impl_type_params", None):
				for idx, tp in enumerate(sig.impl_type_params or []):
					if idx < len(impl_args):
						param_map[tp.id] = impl_args[idx]
			if getattr(sig, "type_params", None):
				for idx, tp in enumerate(sig.type_params or []):
					if idx < len(fn_args):
						param_map[tp.id] = fn_args[idx]
			if sig.impl_target_type_args is not None and inst_impl_target_args is not None:
				for template_arg, concrete_arg in zip(sig.impl_target_type_args, inst_impl_target_args):
					template_def = shared_type_table.get(template_arg)
					if template_def.kind is TypeKind.TYPEVAR and template_def.type_param_id is not None:
						param_map[template_def.type_param_id] = concrete_arg
			if inst_impl_target_id is not None:
				inst = shared_type_table.struct_instances.get(inst_impl_target_id)
				if inst is not None:
					schema = shared_type_table.struct_bases.get(inst.base_id)
					if schema is not None and schema.type_params:
						for tp, concrete_arg in zip(schema.type_params, inst.type_args):
							param_map[tp.id] = concrete_arg
			if param_map:
				inst_param_ids = [_subst_with_owner_map(t, param_map) for t in inst_param_ids]
				inst_ret_id = _subst_with_owner_map(inst_ret_id, param_map)
				if inst_impl_target_id is not None:
					inst_impl_target_id = _subst_with_owner_map(inst_impl_target_id, param_map)
			inst_sig = replace(
				sig,
				name=function_symbol(inst_fn_id),
				param_type_ids=inst_param_ids,
				return_type_id=inst_ret_id,
				impl_target_type_id=inst_impl_target_id,
				impl_target_type_args=inst_impl_target_args,
				type_params=[],
				impl_type_params=[],
				param_types=None,
				return_type=None,
				is_exported_entrypoint=False,
				is_instantiation=True,
			)
			_register_derived_signature_precheck(inst_fn_id, inst_sig)
			if require_env is not None and template_fn_id is not None:
				req_expr = require_env.requires_by_fn.get(template_fn_id)
				if req_expr is not None:
					require_env.requires_by_fn[inst_fn_id] = req_expr
			template_hir = template_hirs_by_key.get(template_key)
			if template_hir is None:
				wrap_sig = signatures_by_id.get(inst_fn_id)
				if wrap_sig is not None and getattr(wrap_sig, "is_wrapper", False):
					template_hir = _make_wrapper_template_hir(wrap_sig)
					template_hirs_by_key[template_key] = template_hir
				if template_hir is None:
					type_diags.append(
						Diagnostic(
							message=f"generic instantiation requires a template body for '{function_key_str(template_key)}'",
							code="E_MISSING_TEMPLATE_BODY",
							severity="error",
							phase="typecheck",
							span=getattr(sig, "loc", None),
						)
					)
					handle.status = "failed"
					continue
			inst_hir = normalize_hir(template_hir)
			_clear_var_binding_ids(inst_hir)
			normalized_hirs_by_id[inst_fn_id] = inst_hir
			mod_name = getattr(inst_fn_id, "module", None) or "main"
			current_mod = _module_id_with_visibility(mod_name)
			visible_mods = None
			if module_deps is not None:
				visible = set(visible_module_names_by_name.get(mod_name, {mod_name}))
				def _collect_type_modules(tid: TypeId) -> None:
					try:
						td = shared_type_table.get(tid)
					except Exception:
						return
					if td.kind in {TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.ERROR, TypeKind.INTERFACE}:
						if td.module_id:
							visible.add(td.module_id)
					for child in td.param_types or []:
						_collect_type_modules(child)
				for _tid in list(impl_args) + list(fn_args):
					_collect_type_modules(_tid)
				visible_mods = tuple(sorted(_module_id_with_visibility(m) for m in visible))
			_sync_visibility_provenance()
			current_file = None
			if origin_by_fn_id is not None and template_fn_id in origin_by_fn_id:
				current_file = str(origin_by_fn_id.get(template_fn_id))
			elif sig is not None:
				current_file = Span.from_loc(getattr(sig, "loc", None)).file
			param_mutable = None
			if sig is not None and sig.param_names is not None and sig.param_mutable is not None:
				if len(sig.param_names) == len(sig.param_mutable):
					param_mutable = {pname: bool(flag) for pname, flag in zip(sig.param_names, sig.param_mutable)}
			inst_result = type_checker.check_function(
				inst_fn_id,
				inst_hir,
				param_types={pname: pty for pname, pty in zip(sig.param_names or [], inst_param_ids)},
				param_mutable=param_mutable,
				return_type=inst_ret_id,
				preseed_type_params={**{tp.name: impl_args[idx] for idx, tp in enumerate(sig.impl_type_params or [])}, **{tp.name: fn_args[idx] for idx, tp in enumerate(sig.type_params or [])}},
				preseed_scope_bindings=getattr(inst_hir, "param_binding_ids", None),
				signatures_by_id=signatures_by_id,
				function_keys_by_fn_id=function_keys_by_fn_id,
				callable_registry=callable_registry,
				impl_index=impl_index,
				trait_index=trait_index,
				trait_impl_index=trait_impl_index,
				trait_scope_by_module=trait_scope_by_module,
				linked_world=linked_world,
				require_env=require_env,
				visible_modules=visible_mods,
				current_module=current_mod,
				visibility_provenance=visibility_provenance_by_id,
			)
			if impl_subst is not None or fn_subst is not None:
				new_call_info: dict[int, CallInfo] = {}
				for csid, info in inst_result.typed_fn.call_info_by_callsite_id.items():
					new_call_info[csid] = _subst_call_info(info, impl_subst, fn_subst)
				inst_result.typed_fn.call_info_by_callsite_id = new_call_info
			type_diags.extend(inst_result.diagnostics)
			deferred = deferred_guard_diags_by_template.get(template_key)
			guard_outcomes = getattr(inst_result, "guard_outcomes", None)
			if deferred and isinstance(guard_outcomes, dict):
				existing = {_diag_key(d) for d in inst_result.diagnostics}
				for guard_key, status in guard_outcomes.items():
					branch = None
					if status is ProofStatus.PROVED:
						branch = "then"
					elif status is ProofStatus.REFUTED:
						branch = "else"
					if branch is None:
						continue
					for diag in deferred.get((guard_key, branch), []):
						key = _diag_key(diag)
						if key in existing:
							continue
						inst_result.diagnostics.append(diag)
						type_diags.append(diag)
						existing.add(key)
			typed_fns_by_id[inst_fn_id] = inst_result.typed_fn
			_queue_instantiations(inst_result.typed_fn)
			handle.status = "emitted"
	
	_drain_instantiations()
	def _rewrite_call_targets(typed_fn: object, block: H.HBlock) -> None:
		call_info_map = getattr(typed_fn, "call_info_by_callsite_id", None)
		if not isinstance(call_info_map, dict):
			return
		inst_map = getattr(typed_fn, "instantiations_by_callsite_id", None)
		if not isinstance(inst_map, dict):
			return
		def _set_call_info(csid: int | None, info: CallInfo) -> None:
			if csid is not None:
				call_info_map[csid] = info
		for key, inst in inst_map.items():
			template_key = getattr(inst, "target_key", None)
			type_args = tuple(getattr(inst, "type_args", ()) or ())
			if not isinstance(template_key, FunctionKey) or not type_args:
				continue
			handle = inst_cache.get(_inst_key(template_key, type_args))
			if handle is None:
				handle = _request_instantiation(template_key, type_args)
			if handle.status != "emitted":
				_drain_instantiations()
			if handle.status != "emitted":
				continue
			# Only true callsite ids are eligible for CallInfo target rewrite.
			# Non-call instantiations (map literals/type applications) are tracked
			# separately to avoid node_id collisions with callsite_id integers.
			csid = key if isinstance(key, int) else None
			info = call_info_map.get(csid) if csid is not None else None
			if info is None:
				continue
			inst_sig = signatures_by_id.get(handle.fn_id)
			if inst_sig is None or inst_sig.param_type_ids is None or inst_sig.return_type_id is None:
				new_info = CallInfo(target=CallTarget.direct(handle.fn_id), sig=info.sig)
			else:
				new_info = CallInfo(
					target=CallTarget.direct(handle.fn_id),
					sig=CallSig(
						param_types=tuple(inst_sig.param_type_ids),
						user_ret_type=inst_sig.return_type_id,
						can_throw=_inst_can_throw(inst_sig),
					),
				)
			_set_call_info(csid, new_info)

	for fn_id, typed_fn in typed_fns_by_id.items():
		block = getattr(typed_fn, "body", None)
		if isinstance(block, H.HBlock):
			_rewrite_call_targets(typed_fn, block)
	if drift_debug.enabled("local_types_trace"):
		seen_expr_objs: dict[int, tuple[FunctionId, str, object]] = {}
		for fn_id, block in normalized_hirs_by_id.items():
			if not isinstance(block, H.HBlock):
				continue
			def _walk_shared(obj: object) -> None:
				if isinstance(obj, H.HExpr):
					obj_id = id(obj)
					kind = type(obj).__name__
					span = getattr(obj, "loc", Span())
					prev = seen_expr_objs.get(obj_id)
					if prev is None:
						seen_expr_objs[obj_id] = (fn_id, kind, span)
					else:
						prev_fn, prev_kind, prev_span = prev
						if (getattr(prev_fn, "module", None), getattr(prev_fn, "name", None)) == ("main", "run") or (getattr(fn_id, "module", None), getattr(fn_id, "name", None)) == ("main", "run"):
							import sys as _dbg_sys
							print(f"[drift:debug][local_types_trace] shared_expr_obj id={obj_id} prev_fn={prev_fn} prev_kind={prev_kind} prev_span={prev_span} now_fn={fn_id} now_kind={kind} now_span={span}", file=_dbg_sys.stderr)
				if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
					return
				if is_dataclass(obj):
					for f in fields(obj):
						_walk_shared(getattr(obj, f.name))
					return
				if isinstance(obj, (list, tuple)):
					for item in obj:
						_walk_shared(item)
					return
				if isinstance(obj, dict):
					for key in sorted(obj.keys(), key=repr):
						_walk_shared(obj[key])
					return
			_walk_shared(block)
		for fn_id, typed_fn in typed_fns_by_id.items():
			if getattr(fn_id, "module", None) != "main" or getattr(fn_id, "name", None) != "run":
				continue
			block = getattr(typed_fn, "body", None)
			if not isinstance(block, H.HBlock):
				continue
			import sys as _dbg_sys
			print(f"[drift:debug][local_types_trace] fn={fn_id} scan=post_instantiation", file=_dbg_sys.stderr)
			seen_expr_ids: dict[int, tuple[str, object]] = {}
			def _walk_expr_ids(obj: object) -> None:
				if isinstance(obj, H.HExpr):
					node_id = getattr(obj, "node_id", 0)
					if node_id == 0:
						return
					kind = type(obj).__name__
					span = getattr(obj, "loc", Span())
					prev = seen_expr_ids.get(node_id)
					if prev is None:
						seen_expr_ids[node_id] = (kind, span)
					else:
						prev_kind, prev_span = prev
						if prev_kind != kind:
							print(f"[drift:debug][local_types_trace] fn={fn_id} post_instantiation_dup_node_id={node_id} prev={prev_kind} now={kind} prev_span={prev_span} now_span={span}", file=_dbg_sys.stderr)
				if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
					return
				if is_dataclass(obj):
					for f in fields(obj):
						_walk_expr_ids(getattr(obj, f.name))
					return
				if isinstance(obj, (list, tuple)):
					for item in obj:
						_walk_expr_ids(item)
					return
				if isinstance(obj, dict):
					for key in sorted(obj.keys(), key=repr):
						_walk_expr_ids(obj[key])
					return
			_walk_expr_ids(block)

	if emit_instantiation_index is not None:
		entries: list[dict[str, object]] = []
		for handle in inst_cache.values():
			if handle.status != "emitted":
				continue
			entries.append(
				{
					"key": instantiation_key_str(handle.key),
					"symbol": function_symbol(handle.fn_id),
					"can_throw": bool(handle.key.abi.can_throw),
					"linkage": "linkonce_odr",
					"comdat": True,
				}
			)
		entries.sort(key=lambda e: str(e.get("key", "")))
		emit_instantiation_index.write_text(
			json.dumps(entries, sort_keys=True),
			encoding="utf-8",
		)

	method_wrapper_by_target: dict[FunctionId, FunctionId] = {}
	for sig_id, sig in signatures_by_id.items():
		if getattr(sig, "is_wrapper", False) and getattr(sig, "wraps_target_fn_id", None) is not None:
			method_wrapper_by_target[sig.wraps_target_fn_id] = sig_id

	def _ensure_method_call_info() -> None:
		for fn_id, typed_fn in typed_fns_by_id.items():
			call_info_map = getattr(typed_fn, "call_info_by_callsite_id", None)
			if not isinstance(call_info_map, dict):
				continue
			call_resolutions = getattr(typed_fn, "call_resolutions", None)
			if not isinstance(call_resolutions, dict):
				continue
			caller_mod = fn_id.module
			node_to_callsites: dict[int, list[int]] = {}
			for expr in _collect_call_nodes_by_id(getattr(typed_fn, "body", H.HBlock(statements=[]))).values():
				csid = getattr(expr, "callsite_id", None)
				if isinstance(csid, int):
					node_to_callsites.setdefault(expr.node_id, []).append(csid)
			for node_id, res in call_resolutions.items():
				csid_list = node_to_callsites.get(node_id) or []
				if len(csid_list) != 1:
					continue
				csid = csid_list[0]
				info_key = csid
				if isinstance(res, MethodResolution):
					if info_key in call_info_map:
						info = call_info_map.get(info_key)
						if info is not None and info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
							continue
					decl = res.decl
					target_fn_id = decl.fn_id
					if target_fn_id is None:
						continue
					params = list(decl.signature.param_types)
					ret = res.result_type or decl.signature.result_type
					sig_for_throw = signatures_by_id.get(target_fn_id)
					call_can_throw = True
					if sig_for_throw is not None and sig_for_throw.declared_can_throw is not None:
						call_can_throw = bool(sig_for_throw.declared_can_throw)
					if sig_for_throw is not None and sig_for_throw.is_pub and target_fn_id.module != caller_mod:
						wrapper_id = method_wrapper_by_target.get(target_fn_id)
						if wrapper_id is not None:
							target_fn_id = wrapper_id
							call_can_throw = True
						elif not call_can_throw:
							call_can_throw = True
					call_info_map[info_key] = CallInfo(
						target=CallTarget.direct(target_fn_id),
						sig=CallSig(
							param_types=tuple(params),
							user_ret_type=ret,
							can_throw=bool(call_can_throw),
						),
					)

	_ensure_method_call_info()

	for fn_id, sig in signatures_by_id.items():
		if not (sig.type_params or getattr(sig, "impl_type_params", [])):
			continue
		# Templates are never lowered to MIR/SSA/LLVM.
		normalized_hirs_by_id.pop(fn_id, None)

	def _has_typevar(tid: TypeId) -> bool:
		return bool(shared_type_table.has_typevar(tid))

	for fn_id in sorted(normalized_hirs_by_id.keys(), key=function_symbol):
		name = function_symbol(fn_id)
		sig = signatures_by_id.get(fn_id)
		if sig is not None:
			if sig.type_params or getattr(sig, "impl_type_params", []):
				continue
			for tid in sig.param_type_ids or []:
				if _has_typevar(tid):
					type_diags.append(
						Diagnostic(
							message=f"generic instantiation required: function '{name}' has an unresolved type parameter in its signature",
							severity="error",
							phase="typecheck",
							span=getattr(sig, "loc", None),
						)
					)
					break
			if sig.return_type_id is not None and _has_typevar(sig.return_type_id):
				type_diags.append(
					Diagnostic(
						message=f"generic instantiation required: function '{name}' has an unresolved type parameter in its return type",
						severity="error",
						phase="typecheck",
						span=getattr(sig, "loc", None),
					)
				)
		typed_fn = typed_fns_by_id.get(fn_id)
		call_info = getattr(typed_fn, "call_info_by_callsite_id", None) if typed_fn is not None else None
		if isinstance(call_info, dict):
			for info in call_info.values():
				if any(_has_typevar(t) for t in info.sig.param_types) or _has_typevar(info.sig.user_ret_type):
					type_diags.append(
						Diagnostic(
							message=f"generic instantiation required: call in '{name}' has unresolved type parameters",
							severity="error",
							phase="typecheck",
							span=None,
						)
					)
					break

	# Stage “checker”: obtain declared_can_throw from the checker stub so the
	# driver path mirrors the real compiler layering once a proper checker exists.
	call_info_by_callsite_id: dict[FunctionId, dict[int, CallInfo]] = {}
	for fn_id, typed_fn in typed_fns_by_id.items():
		call_info = getattr(typed_fn, "call_info_by_callsite_id", None)
		if isinstance(call_info, dict):
			call_info_by_callsite_id[fn_id] = dict(call_info)
		else:
			call_info_by_callsite_id.setdefault(fn_id, {})
	check_inputs = CheckerInputsById(
		hir_blocks_by_id=normalized_hirs_by_id,
		signatures_by_id=signatures_by_id,
		call_info_by_callsite_id=call_info_by_callsite_id,
	)
	if drift_debug.enabled("local_types_trace"):
		for fn_id, typed_fn in typed_fns_by_id.items():
			if getattr(fn_id, "module", None) != "main" or getattr(fn_id, "name", None) != "run":
				continue
			block = getattr(typed_fn, "body", None)
			norm_block = normalized_hirs_by_id.get(fn_id)
			import sys as _dbg_sys
			print(f"[drift:debug][local_types_trace] fn={fn_id} pre_checker_body_shared={block is norm_block}", file=_dbg_sys.stderr)
	with _timed("checker"):
		checked = Checker.run_by_id(
			check_inputs,
			declared_can_throw_by_id=declared_can_throw_by_id,
			exception_catalog=exc_env,
			type_table=shared_type_table,
			fn_decls_by_id=signatures_by_id.keys(),
		)
	if drift_debug.enabled("local_types_trace"):
		for fn_id, typed_fn in typed_fns_by_id.items():
			if getattr(fn_id, "module", None) != "main" or getattr(fn_id, "name", None) != "run":
				continue
			block = getattr(typed_fn, "body", None)
			if not isinstance(block, H.HBlock):
				continue
			import sys as _dbg_sys
			print(f"[drift:debug][local_types_trace] fn={fn_id} scan=post_checker", file=_dbg_sys.stderr)
			seen_expr_ids: dict[int, tuple[str, object]] = {}
			def _walk_expr_ids(obj: object) -> None:
				if isinstance(obj, H.HExpr):
					node_id = getattr(obj, "node_id", 0)
					if node_id == 0:
						return
					kind = type(obj).__name__
					span = getattr(obj, "loc", Span())
					prev = seen_expr_ids.get(node_id)
					if prev is None:
						seen_expr_ids[node_id] = (kind, span)
					else:
						prev_kind, prev_span = prev
						if prev_kind != kind:
							print(f"[drift:debug][local_types_trace] fn={fn_id} post_checker_dup_node_id={node_id} prev={prev_kind} now={kind} prev_span={prev_span} now_span={span}", file=_dbg_sys.stderr)
				if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
					return
				if is_dataclass(obj):
					for f in fields(obj):
						_walk_expr_ids(getattr(obj, f.name))
					return
				if isinstance(obj, (list, tuple)):
					for item in obj:
						_walk_expr_ids(item)
					return
				if isinstance(obj, dict):
					for key in sorted(obj.keys(), key=repr):
						_walk_expr_ids(obj[key])
					return
			_walk_expr_ids(block)
	if shared_type_table is not None and shared_type_table.type_provenance_enabled():
		required_type_ids: set[TypeId] = set()
		def _add_type_id(tid: TypeId | None) -> None:
			if tid is None or not isinstance(tid, int) or tid <= 0:
				return
			required_type_ids.add(tid)
		for sig in signatures_by_id.values():
			for tid in sig.param_type_ids or []:
				_add_type_id(tid)
			_add_type_id(sig.return_type_id)
			_add_type_id(sig.error_type_id)
		for typed_fn in typed_fns_by_id.values():
			expr_types = getattr(typed_fn, "expr_types", None)
			if isinstance(expr_types, dict):
				for tid in expr_types.values():
					_add_type_id(tid)
			binding_types = getattr(typed_fn, "binding_types", None)
			if isinstance(binding_types, dict):
				for tid in binding_types.values():
					_add_type_id(tid)
			call_info_by_callsite = getattr(typed_fn, "call_info_by_callsite_id", None)
			if isinstance(call_info_by_callsite, dict):
				for info in call_info_by_callsite.values():
					for tid in info.sig.param_types:
						_add_type_id(tid)
					_add_type_id(info.sig.user_ret_type)
		import sys as _dbg_sys
		missing, phase_counts, kind_counts = shared_type_table.audit_type_provenance(required=required_type_ids)
		print(f"[drift:debug][type_prov] required={len(required_type_ids)} missing={len(missing)} phases={phase_counts} kinds={kind_counts}", file=_dbg_sys.stderr)
		if missing:
			missing_set = set(missing)
			missing_sources: dict[TypeId, list[str]] = {tid: [] for tid in missing}
			for fn_id, sig in signatures_by_id.items():
				fn_label = function_symbol(fn_id)
				for tid in sig.param_type_ids or []:
					if tid in missing_set:
						missing_sources[tid].append(f"signature:param fn={fn_label}")
				if sig.return_type_id in missing_set:
					missing_sources[sig.return_type_id].append(f"signature:return fn={fn_label}")
				if sig.error_type_id in missing_set:
					missing_sources[sig.error_type_id].append(f"signature:error fn={fn_label}")
			for fn_id, typed_fn in typed_fns_by_id.items():
				fn_label = function_symbol(fn_id)
				expr_types = getattr(typed_fn, "expr_types", None)
				if isinstance(expr_types, dict):
					for node_id, tid in expr_types.items():
						if tid in missing_set:
							missing_sources[tid].append(f"expr fn={fn_label} node={node_id}")
				binding_types = getattr(typed_fn, "binding_types", None)
				binding_names = getattr(typed_fn, "binding_names", None)
				if isinstance(binding_types, dict):
					for bid, tid in binding_types.items():
						if tid in missing_set:
							name = binding_names.get(bid) if isinstance(binding_names, dict) else None
							missing_sources[tid].append(f"binding fn={fn_label} id={bid} name={name}")
				call_info_by_callsite = getattr(typed_fn, "call_info_by_callsite_id", None)
				if isinstance(call_info_by_callsite, dict):
					for csid, info in call_info_by_callsite.items():
						for idx, tid in enumerate(info.sig.param_types):
							if tid in missing_set:
								missing_sources[tid].append(f"call_param fn={fn_label} csid={csid} idx={idx}")
						if info.sig.user_ret_type in missing_set:
							missing_sources[info.sig.user_ret_type].append(f"call_ret fn={fn_label} csid={csid}")
			def _type_desc(tid: TypeId) -> str:
				try:
					td = shared_type_table.get(tid)
				except Exception:
					return str(tid)
				name = td.name or "<anon>"
				kind = td.kind.name if hasattr(td, "kind") else "UNKNOWN"
				return f"{tid}:{kind}:{name}"
			sample = ", ".join(_type_desc(tid) for tid in missing[:12])
			print(f"[drift:debug][type_prov] missing_sample={sample}", file=_dbg_sys.stderr)
			for tid in missing[:6]:
				sources = missing_sources.get(tid) or []
				source_desc = "; ".join(sources[:6])
				print(f"[drift:debug][type_prov] missing_sources {tid} {source_desc}", file=_dbg_sys.stderr)
			raise AssertionError("type provenance missing for required TypeIds")
	if enforce_entrypoint and signatures_by_id and shared_type_table is not None:
		from lang.driftc.type_checker import validate_entrypoint
		validate_entrypoint(
			signatures_by_id,
			shared_type_table,
			checked.diagnostics,
			entry_module=entry_module,
			entry_name=entry_name,
		)
	if type_diags:
		checked.diagnostics.extend(type_diags)
	typevar_diags: list[Diagnostic] = []
	for fn_id, typed_fn in typed_fns_by_id.items():
		sig = signatures_by_id.get(fn_id)
		if sig is None:
			continue
		if getattr(sig, "type_params", None) or getattr(sig, "impl_type_params", None):
			continue
		if sig.param_type_ids and any(shared_type_table.has_typevar(t) for t in sig.param_type_ids):
			continue
		if sig.return_type_id is not None and shared_type_table.has_typevar(sig.return_type_id):
			continue
		if sig.error_type_id is not None and shared_type_table.has_typevar(sig.error_type_id):
			continue
		typevar_diags.extend(_typevar_callinfo_diags(typed_fn, shared_type_table))
	if typevar_diags:
		checked.diagnostics.extend(typevar_diags)
		if any(d.severity == "error" for d in checked.diagnostics):
			if return_checked:
				if return_ssa:
					return {}, checked, None
				return {}, checked
			return {}
	if module_exports and shared_type_table is not None:
		def _collect_nominal_types(type_id: TypeId, *, seen: set[TypeId], out: set[TypeId]) -> None:
			if type_id in seen:
				return
			seen.add(type_id)
			td = shared_type_table.get(type_id)
			if td is None:
				return
			if td.kind in (TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.INTERFACE):
				out.add(type_id)
			for child in td.param_types:
				_collect_nominal_types(child, seen=seen, out=out)

		for mid, exports in module_exports.items():
			vals = exports.get("values") if isinstance(exports, dict) else None
			types_obj = exports.get("types") if isinstance(exports, dict) else None
			if not isinstance(vals, list) or not isinstance(types_obj, dict):
				continue
			exported_structs = set(types_obj.get("structs") or [])
			exported_variants = set(types_obj.get("variants") or [])
			exported_interfaces = set(types_obj.get("interfaces") or [])
			for sym in vals:
				if not isinstance(sym, str):
					continue
				for fn_id, sig in signatures_by_id.items():
					if fn_id.module != mid or fn_id.name != sym:
						continue
					nominals: set[TypeId] = set()
					seen: set[TypeId] = set()
					if sig.return_type_id is not None:
						_collect_nominal_types(sig.return_type_id, seen=seen, out=nominals)
					for tid in sig.param_type_ids or []:
						_collect_nominal_types(tid, seen=seen, out=nominals)
					for tid in nominals:
						td = shared_type_table.get(tid)
						if td is None or td.module_id != mid:
							continue
						if td.kind is TypeKind.STRUCT and td.name not in exported_structs:
							checked.diagnostics.append(
								Diagnostic(
									message=(
										f"exported value '{sym}' uses private type '{td.name}' "
										f"in module '{mid}'"
									),
									code="E-PRIVATE-TYPE",
									severity="error",
									phase="typecheck",
									span=None,
								)
							)
						if td.kind is TypeKind.VARIANT and td.name not in exported_variants:
							checked.diagnostics.append(
								Diagnostic(
									message=(
										f"exported value '{sym}' uses private type '{td.name}' "
										f"in module '{mid}'"
									),
									code="E-PRIVATE-TYPE",
									severity="error",
									phase="typecheck",
									span=None,
								)
							)
						if td.kind is TypeKind.INTERFACE and td.name not in exported_interfaces:
							checked.diagnostics.append(
								Diagnostic(
									message=(
										f"exported value '{sym}' uses private type '{td.name}' "
										f"in module '{mid}'"
									),
									code="E-PRIVATE-TYPE",
									severity="error",
									phase="typecheck",
									span=None,
								)
							)
	if run_borrow_check and not any(d.severity == "error" for d in checked.diagnostics):
		_apply_stdlib_escape_annotations(signatures_by_id)
		borrow_diags: list[Diagnostic] = []
		with _timed("borrow_check"):
			for _fn_id, typed_fn in typed_fns_by_id.items():
				bc = BorrowChecker.from_typed_fn(
					typed_fn,
					type_table=shared_type_table,
					signatures_by_id=signatures_by_id,
					enable_auto_borrow=True,
				)
				borrow_diags.extend(bc.check_block(typed_fn.body))
		if borrow_diags:
			_assert_all_phased(borrow_diags, context="borrowcheck")
			checked.diagnostics.extend(borrow_diags)
	if any(d.severity == "error" for d in checked.diagnostics):
		if return_checked:
			if return_ssa:
				return {}, checked, None
			return {}, checked
		return {}
	# Typed-mode guard: every call node must have callsite CallInfo coverage.
	for fn_id, typed_fn in typed_fns_by_id.items():
		block = getattr(typed_fn, "body", None)
		if not isinstance(block, H.HBlock):
			continue
		call_info_by_callsite = getattr(typed_fn, "call_info_by_callsite_id", None)
		if not isinstance(call_info_by_callsite, dict):
			continue
		missing_callsite: list[int] = []
		for expr in _collect_call_nodes_by_id(block).values():
			csid = getattr(expr, "callsite_id", None)
			if not isinstance(csid, int):
				missing_callsite.append(-1)
				continue
			if csid not in call_info_by_callsite:
				missing_callsite.append(csid)
		if missing_callsite:
			missing_desc = ", ".join(str(c) for c in sorted(set(missing_callsite))[:6])
			checked.diagnostics.append(
				Diagnostic(
					message=(
						f"internal: missing CallInfo for callsite ids in '{function_symbol(fn_id)}': {missing_desc}"
					),
					code="E_INTERNAL_MISSING_CALLSITE_CALLINFO",
					severity="error",
					phase="typecheck",
					span=None,
				)
			)
	had_errors = any(d.severity == "error" for d in checked.diagnostics)
	if had_errors:
		if return_checked:
			if return_ssa:
				return {}, checked, None
			return {}, checked
		raise ValueError("compile_stubbed_funcs aborted due to errors")
	# Ensure declared_can_throw is a bool for downstream stages; guard against
	# accidental truthy objects sneaking in from legacy shims.
	for info in checked.fn_infos_by_id.values():
		if info.declared_can_throw is None:
			info.declared_can_throw = True
		elif not isinstance(info.declared_can_throw, bool):
			info.declared_can_throw = bool(info.declared_can_throw)
	declared_by_id = {fn_id: info.declared_can_throw for fn_id, info in checked.fn_infos_by_id.items()}

	# Synthesize Ok-wrap thunks and captureless lambda functions (pre-LLVM).
	def _register_synth_signature(fn_id: FunctionId, sig: FnSignature) -> None:
		existing = derived_signatures_by_id.get(fn_id) or base_signatures_by_id.get(fn_id)
		if existing is not None:
			if existing != sig:
				raise AssertionError(f"signature collision for '{function_symbol(fn_id)}'")
			if fn_id not in checked.fn_infos_by_id:
				info = make_fn_info(fn_id, existing, declared_can_throw=_sig_declared_can_throw(existing))
				checked.fn_infos_by_id[fn_id] = info
				declared_by_id[fn_id] = info.declared_can_throw
			return None
		_record_signature_provenance(fn_id, sig)
		derived_signatures_by_id[fn_id] = sig
		info = make_fn_info(fn_id, sig, declared_can_throw=_sig_declared_can_throw(sig))
		checked.fn_infos_by_id[fn_id] = info
		declared_by_id[fn_id] = info.declared_can_throw
		return None
	# Align call info can-throw flags with inferred callee throw modes so MIR
	# lowering uses a consistent ABI for direct calls.
	for typed_fn in typed_fns_by_id.values():
		callsite_map = getattr(typed_fn, "call_info_by_callsite_id", None)
		if not isinstance(callsite_map, dict):
			continue
		updated_callsite: dict[int, CallInfo] = {}
		for csid, info in callsite_map.items():
			call_can_throw = info.sig.can_throw
			if info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
				target_info = checked.fn_infos_by_id.get(info.target.symbol)
				if target_info is not None:
					call_can_throw = call_can_throw or bool(target_info.declared_can_throw)
			if call_can_throw != info.sig.can_throw:
				info = CallInfo(
					target=info.target,
					sig=CallSig(
						param_types=info.sig.param_types,
						user_ret_type=info.sig.user_ret_type,
						can_throw=bool(call_can_throw),
					),
				)
			updated_callsite[csid] = info
		typed_fn.call_info_by_callsite_id = updated_callsite
	intrinsic_diags: list[Diagnostic] = []
	for typed_fn in typed_fns_by_id.values():
		intrinsic_diags.extend(_validate_intrinsic_callinfo(typed_fn))
	if intrinsic_diags:
		checked.diagnostics.extend(intrinsic_diags)
		if return_checked:
			if return_ssa:
				return {}, checked, None
			return {}, checked
		return {}
	if drift_debug.enabled("local_types_trace"):
		for fn_id, typed_fn in typed_fns_by_id.items():
			if getattr(fn_id, "module", None) != "main" or getattr(fn_id, "name", None) != "run":
				continue
			block = getattr(typed_fn, "body", None)
			if not isinstance(block, H.HBlock):
				continue
			import sys as _dbg_sys
			print(f"[drift:debug][local_types_trace] fn={fn_id} scan=post_callinfo", file=_dbg_sys.stderr)
			seen_expr_ids: dict[int, tuple[str, object]] = {}
			def _walk_expr_ids(obj: object) -> None:
				if isinstance(obj, H.HExpr):
					node_id = getattr(obj, "node_id", 0)
					if node_id == 0:
						return
					kind = type(obj).__name__
					span = getattr(obj, "loc", Span())
					prev = seen_expr_ids.get(node_id)
					if prev is None:
						seen_expr_ids[node_id] = (kind, span)
					else:
						prev_kind, prev_span = prev
						if prev_kind != kind:
							print(f"[drift:debug][local_types_trace] fn={fn_id} post_callinfo_dup_node_id={node_id} prev={prev_kind} now={kind} prev_span={prev_span} now_span={span}", file=_dbg_sys.stderr)
				if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
					return
				if is_dataclass(obj):
					for f in fields(obj):
						_walk_expr_ids(getattr(obj, f.name))
					return
				if isinstance(obj, (list, tuple)):
					for item in obj:
						_walk_expr_ids(item)
					return
				if isinstance(obj, dict):
					for key in sorted(obj.keys(), key=repr):
						_walk_expr_ids(obj[key])
					return
			_walk_expr_ids(block)
	# Prefer the checker's table when the caller did not supply one so TypeIds
	# stay coherent across lowering/codegen.
	if shared_type_table is None and checked.type_table is not None:
		shared_type_table = checked.type_table
	mir_funcs_by_id: Dict[FunctionId, M.MirFunc] = {}
	hidden_lambda_specs: list = []

	def _typed_mode_for(typed_fn: object | None, type_table: TypeTable | None, typecheck_ok: bool) -> str:
		if typed_fn is None:
			return "recover"
		if not typecheck_ok:
			return "recover"
		if type_table is None:
			return "recover"
		expr_types = getattr(typed_fn, "expr_types", None)
		if not isinstance(expr_types, dict) or not expr_types:
			return "recover"
		for tid in expr_types.values():
			if tid is None:
				return "recover"
			if type_table.get(tid).kind is TypeKind.UNKNOWN:
				return "recover"
		return "strict"

	def _collect_hcast_node_ids(body: H.HNode) -> set[int]:
		ids: set[int] = set()
		seen: set[int] = set()
		hir_modules = {H.__name__, "lang.driftc.stage1.closures"}

		def _should_descend(obj: object) -> bool:
			if isinstance(obj, H.HNode):
				return True
			if is_dataclass(obj) and obj.__class__.__module__ in hir_modules:
				return True
			return False

		def walk(obj: object) -> None:
			obj_id = id(obj)
			if obj_id in seen:
				return
			seen.add(obj_id)
			if isinstance(obj, H.HCast):
				node_id = getattr(obj, "node_id", 0)
				if node_id:
					ids.add(node_id)
			if not _should_descend(obj):
				return
			if is_dataclass(obj):
				for f in fields(obj):
					walk_value(getattr(obj, f.name))
			else:
				for val in vars(obj).values():
					walk_value(val)

		def walk_value(val: object) -> None:
			if val is None:
				return
			if isinstance(val, (list, tuple)):
				for item in val:
					walk_value(item)
				return
			if isinstance(val, dict):
				for key in sorted(val.keys(), key=repr):
					walk_value(val[key])
				return
			walk(val)

		walk(body)
		return ids

	if drift_debug.enabled("local_types_trace"):
		for fn_id, typed_fn in typed_fns_by_id.items():
			if getattr(fn_id, "module", None) != "main" or getattr(fn_id, "name", None) != "run":
				continue
			block = getattr(typed_fn, "body", None)
			if not isinstance(block, H.HBlock):
				continue
			import sys as _dbg_sys
			print(f"[drift:debug][local_types_trace] fn={fn_id} scan=pre_lowering", file=_dbg_sys.stderr)
			seen_expr_ids: dict[int, tuple[str, object]] = {}
			def _walk_expr_ids(obj: object) -> None:
				if isinstance(obj, H.HExpr):
					node_id = getattr(obj, "node_id", 0)
					if node_id == 0:
						return
					kind = type(obj).__name__
					span = getattr(obj, "loc", Span())
					prev = seen_expr_ids.get(node_id)
					if prev is None:
						seen_expr_ids[node_id] = (kind, span)
					else:
						prev_kind, prev_span = prev
						if prev_kind != kind:
							print(f"[drift:debug][local_types_trace] fn={fn_id} pre_lowering_dup_node_id={node_id} prev={prev_kind} now={kind} prev_span={prev_span} now_span={span}", file=_dbg_sys.stderr)
				if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
					return
				if is_dataclass(obj):
					for f in fields(obj):
						_walk_expr_ids(getattr(obj, f.name))
					return
				if isinstance(obj, (list, tuple)):
					for item in obj:
						_walk_expr_ids(item)
					return
				if isinstance(obj, dict):
					for key in sorted(obj.keys(), key=repr):
						_walk_expr_ids(obj[key])
					return
			_walk_expr_ids(block)

	hir_to_mir_start = None
	if _timing_enabled:
		import time as _timing_time
		hir_to_mir_start = _timing_time.perf_counter()
	for fn_id, hir_norm in normalized_hirs_by_id.items():
		builder = make_builder(fn_id)
		sig = signatures_by_id.get(fn_id)
		param_types: dict[str, "TypeId"] = {}
		param_names: list[str] = []
		if sig is not None and sig.param_names is not None:
			param_names = list(sig.param_names)
		if sig is not None and sig.param_type_ids is not None and param_names:
			param_types = {pname: pty for pname, pty in zip(param_names, sig.param_type_ids)}
		builder.func.params = list(param_names)
		if sig is not None and getattr(sig, "is_intrinsic", False):
			mir_funcs_by_id[fn_id] = builder.func
			continue
		if sig is not None and sig.param_type_ids is not None:
			if (getattr(sig, "type_params", None) or getattr(sig, "impl_type_params", None)):
				mir_funcs_by_id[fn_id] = builder.func
				continue
			if sig.param_type_ids and any(shared_type_table.has_typevar(t) for t in sig.param_type_ids):
				mir_funcs_by_id[fn_id] = builder.func
				continue
			if sig.return_type_id is not None and shared_type_table.has_typevar(sig.return_type_id):
				mir_funcs_by_id[fn_id] = builder.func
				continue
		if sig.error_type_id is not None and shared_type_table.has_typevar(sig.error_type_id):
			mir_funcs_by_id[fn_id] = builder.func
			continue
		typed_mode = _typed_mode_for(
			typed_fns_by_id.get(fn_id),
			shared_type_table,
			typecheck_ok_by_fn.get(fn_id, False),
		)
		if typed_mode == "strict":
			expr_types = getattr(typed_fns_by_id.get(fn_id), "expr_types", None)
			if not isinstance(expr_types, dict):
				typed_mode = "recover"
			else:
				hcast_ids = _collect_hcast_node_ids(hir_norm)
				if any(node_id not in expr_types for node_id in hcast_ids):
					typed_mode = "recover"
		if drift_debug.enabled("local_types_trace") and getattr(fn_id, "module", None) == "main" and getattr(fn_id, "name", None) == "run":
			typed_fn = typed_fns_by_id.get(fn_id)
			body_dbg = getattr(typed_fn, "body", None)
			expr_types_dbg = getattr(typed_fn, "expr_types", None)
			if isinstance(body_dbg, H.HBlock) and isinstance(expr_types_dbg, dict):
				import sys as _dbg_sys
				seen_dbg: set[int] = set()
				expr_node_kinds: dict[int, str] = {}
				expr_node_spans: dict[int, object] = {}
				def _walk_dbg(obj: object) -> None:
					obj_id = id(obj)
					if obj_id in seen_dbg:
						return
					seen_dbg.add(obj_id)
					if isinstance(obj, H.HExpr):
						kind = type(obj).__name__
						prev = expr_node_kinds.get(obj.node_id)
						if prev is None:
							expr_node_kinds[obj.node_id] = kind
							expr_node_spans[obj.node_id] = getattr(obj, "loc", None)
						elif prev != kind:
							print(f"[drift:debug][local_types_trace] fn={fn_id} lowering_dup_node_id={obj.node_id} prev={prev} now={kind} prev_span={expr_node_spans.get(obj.node_id)} now_span={getattr(obj, 'loc', None)}", file=_dbg_sys.stderr)
					if isinstance(obj, H.HLiteralBool):
						tid = expr_types_dbg.get(obj.node_id)
						if tid is not None:
							td = shared_type_table.get(tid)
							print(f"[drift:debug][local_types_trace] fn={fn_id} typed_fn_literal_bool node_id={obj.node_id} ty={tid}:{td.kind.name}:{td.name} span={getattr(obj, 'loc', None)}", file=_dbg_sys.stderr)
					if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
						return
					if is_dataclass(obj):
						for f in fields(obj):
							_walk_dbg(getattr(obj, f.name))
						return
					if isinstance(obj, (list, tuple)):
						for item in obj:
							_walk_dbg(item)
						return
					if isinstance(obj, dict):
						for key in sorted(obj.keys(), key=repr):
							_walk_dbg(obj[key])
						return
				_walk_dbg(body_dbg)
		lower = HIRToMIR(
			builder,
			type_table=shared_type_table,
			exc_env=exc_env,
			param_types=param_types,
			expr_types=getattr(typed_fns_by_id.get(fn_id), "expr_types", None),
			iface_coercions=getattr(typed_fns_by_id.get(fn_id), "iface_coercions", None),
			signatures_by_id=signatures_by_id,
			current_fn_id=fn_id,
			type_param_subst=getattr(typed_fns_by_id.get(fn_id), "preseed_type_params", None),
			call_info_by_callsite_id=getattr(typed_fns_by_id.get(fn_id), "call_info_by_callsite_id", {}),
			call_resolutions=getattr(typed_fns_by_id.get(fn_id), "call_resolutions", {}),
			can_throw_by_id=declared_by_id,
			return_type=sig.return_type_id if sig is not None else None,
			binding_names=getattr(typed_fns_by_id.get(fn_id), "binding_names", None),
			binding_types=getattr(typed_fns_by_id.get(fn_id), "binding_types", None),
			typed_mode=typed_mode,
		)
		try:
			lower.lower_function_body(hir_norm)
		except AssertionError as err:
			_append_boundary_contract_diag(
				checked,
				phase="mir_validate",
				prefix="MIR lowering contract failure",
				err=err,
				fn_id=fn_id,
				signatures_by_id=signatures_by_id,
				hir_block=hir_norm,
				origin_by_fn_id=origin_by_fn_id,
			)
			_assert_all_phased(checked.diagnostics, context="compile_stubbed_funcs")
			if return_checked:
				if return_ssa:
					return {}, checked, None
				return {}, checked
			return {}
		builder.func.local_types = dict(lower._local_types)
		unknown_ty = shared_type_table.ensure_unknown()
		for local_name in builder.func.locals:
			if local_name not in builder.func.local_types:
				builder.func.local_types[local_name] = unknown_ty
		for spec in lower.synth_sig_specs():
			if spec.kind == "hidden_lambda":
				continue
			_register_synth_signature(spec.fn_id, spec.sig)
		hidden_lambda_specs.extend(lower.hidden_lambda_specs())
		mir_funcs_by_id[fn_id] = builder.func
		if getattr(builder, "extra_funcs", None):
			for extra in builder.extra_funcs:
				extra_id = getattr(extra, "fn_id", None)
				if extra_id is None:
					raise AssertionError(f"extra func missing fn_id for '{extra.name}' (stage2 bug)")
				extra.local_types = dict(getattr(extra, "local_types", {}) or {})
				for local_name in extra.locals:
					if local_name not in extra.local_types:
						extra.local_types[local_name] = unknown_ty
				mir_funcs_by_id[extra_id] = extra
				if extra_id is not None and extra_id not in checked.fn_infos_by_id:
					sig = signatures_by_id.get(extra_id)
					if sig is not None:
						info = make_fn_info(
							extra_id,
							sig,
							declared_can_throw=_sig_declared_can_throw(sig),
						)
						checked.fn_infos_by_id[extra_id] = info
						declared_by_id[extra_id] = info.declared_can_throw
	if _timing_enabled and hir_to_mir_start is not None:
		import time as _timing_time
		import sys as _timing_sys
		print(f"[drift:debug][timing] hir_to_mir={_timing_time.perf_counter() - hir_to_mir_start:.3f}s", file=_timing_sys.stderr)
	def _hidden_lambda_ret_type(
		body: H.HBlock, typed_fn: "TypedFn", type_table: "TypeTable"
	) -> "TypeId":
		if body.statements:
			last = body.statements[-1]
			if isinstance(last, H.HReturn):
				if last.value is None:
					return type_table.ensure_void()
				return typed_fn.expr_types.get(last.value.node_id, type_table.ensure_unknown())
			if isinstance(last, H.HExprStmt):
				return typed_fn.expr_types.get(last.expr.node_id, type_table.ensure_unknown())
		return type_table.ensure_void()

	type_diag_len = len(type_diags)
	hidden_lambda_index = 0
	hidden_lambda_start = None
	if _timing_enabled:
		import time as _timing_time
		hidden_lambda_start = _timing_time.perf_counter()
	while hidden_lambda_index < len(hidden_lambda_specs):
		spec = hidden_lambda_specs[hidden_lambda_index]
		hidden_lambda_index += 1
		if spec.fn_id in mir_funcs_by_id:
			continue
		origin_typed = typed_fns_by_id.get(spec.origin_fn_id) if spec.origin_fn_id is not None else None
		lam = copy.deepcopy(spec.lambda_expr)
		capture_name_to_id: dict[str, int] = {}
		if not getattr(lam, "captures", None):
			discovery = discover_captures(lam)
			lam.captures = discovery.captures
		if not lam.captures and getattr(lam, "explicit_captures", None):
			kind_map = {
				"ref": C.HCaptureKind.REF,
				"ref_mut": C.HCaptureKind.REF_MUT,
				"copy": C.HCaptureKind.COPY,
				"move": C.HCaptureKind.MOVE,
				"auto": C.HCaptureKind.REF,
			}
			if origin_typed is not None and lam.explicit_captures:
				name_to_bid: dict[str, int] = {}
				for bid, name in origin_typed.binding_names.items():
					name_to_bid[name] = int(bid)
				for cap in lam.explicit_captures or []:
					if cap.binding_id is None and cap.name and cap.name in name_to_bid:
						cap.binding_id = name_to_bid[cap.name]
			explicit_list: list[C.HCapture] = []
			for cap in lam.explicit_captures or []:
				if cap.binding_id is None:
					continue
				kind = kind_map.get(cap.kind)
				if kind is None:
					continue
				explicit_list.append(
					C.HCapture(
						kind=kind,
						key=C.HCaptureKey(root_local=cap.binding_id, proj=()),
						span=cap.span,
					)
				)
			lam.captures = explicit_list
		if lam.captures:
			lam.captures = sort_captures(lam.captures)
		capture_id_map: dict[int, int] = {}
		if lam.captures:
			max_existing = 0
			for param in lam.params:
				if getattr(param, "binding_id", None) is not None:
					max_existing = max(max_existing, int(param.binding_id))

			def _scan_binding_ids(obj: object) -> None:
				nonlocal max_existing
				if obj is None:
					return
				bid = getattr(obj, "binding_id", None)
				if bid is not None:
					max_existing = max(max_existing, int(bid))
				if isinstance(obj, H.HExpr):
					for child in obj.__dict__.values():
						_scan_binding_ids(child)
				elif isinstance(obj, H.HStmt):
					for child in obj.__dict__.values():
						_scan_binding_ids(child)
				elif isinstance(obj, H.HBlock):
					for stmt in obj.statements:
						_scan_binding_ids(stmt)
				elif isinstance(obj, list):
					for item in obj:
						_scan_binding_ids(item)
				elif isinstance(obj, dict):
					for item in obj.values():
						_scan_binding_ids(item)

			if lam.body_expr is not None:
				_scan_binding_ids(lam.body_expr)
			if lam.body_block is not None:
				_scan_binding_ids(lam.body_block)
			next_id = max_existing + 1
			for cap in lam.captures:
				orig = int(cap.key.root_local)
				if orig not in capture_id_map:
					capture_id_map[orig] = next_id
					next_id += 1
			new_caps: list[C.HCapture] = []
			for cap in lam.captures:
				new_root = capture_id_map.get(int(cap.key.root_local), int(cap.key.root_local))
				if new_root != cap.key.root_local:
					new_key = C.HCaptureKey(root_local=new_root, proj=cap.key.proj)
					new_caps.append(C.HCapture(kind=cap.kind, key=new_key, span=cap.span))
				else:
					new_caps.append(cap)
			lam.captures = new_caps

			keep_binding_types = (H.HLet,)
			if hasattr(H, "HParam"):
				keep_binding_types = (H.HLet, H.HParam)
			def _remap_ids(obj: object) -> None:
				if obj is None:
					return
				if isinstance(obj, H.HExplicitCapture):
					bid = getattr(obj, "binding_id", None)
					if bid is not None:
						if int(bid) in capture_id_map:
							obj.binding_id = capture_id_map[int(bid)]
						else:
							obj.binding_id = None
				elif isinstance(obj, H.HVar):
					bid = getattr(obj, "binding_id", None)
					if bid is not None:
						if int(bid) in capture_id_map:
							obj.binding_id = capture_id_map[int(bid)]
						else:
							obj.binding_id = None
				elif hasattr(obj, "binding_id") and not isinstance(obj, keep_binding_types):
					obj.binding_id = None
				elif isinstance(obj, H.HPlaceExpr):
					base = obj.base
					if isinstance(base, H.HVar):
						bid = getattr(base, "binding_id", None)
						if bid is not None:
							if int(bid) in capture_id_map:
								base.binding_id = capture_id_map[int(bid)]
							else:
								base.binding_id = None
				if isinstance(obj, H.HExpr):
					for child in obj.__dict__.values():
						_remap_ids(child)
				elif isinstance(obj, H.HStmt):
					for child in obj.__dict__.values():
						_remap_ids(child)
				elif isinstance(obj, H.HBlock):
					for stmt in obj.statements:
						_remap_ids(stmt)
				elif isinstance(obj, list):
					for item in obj:
						_remap_ids(item)
				elif isinstance(obj, dict):
					for item in obj.values():
						_remap_ids(item)

			capture_name_to_id: dict[str, int] = {}
			if lam.explicit_captures:
				for cap in lam.explicit_captures:
					_remap_ids(cap)
			if lam.body_expr is not None:
				_remap_ids(lam.body_expr)
			if lam.body_block is not None:
				_remap_ids(lam.body_block)
			for cap in lam.explicit_captures or []:
				if cap.name and getattr(cap, "binding_id", None) is not None:
					capture_name_to_id[cap.name] = int(cap.binding_id)
			if capture_name_to_id:
				local_names: set[str] = {p.name for p in lam.params}
				capture_spans: dict[str, Span] = {}
				for cap in lam.explicit_captures or []:
					if cap.name:
						capture_spans.setdefault(cap.name, getattr(cap, "span", Span()))
				def _collect_local_names(obj: object) -> None:
					if obj is None:
						return
					if isinstance(obj, H.HLocalConst):
						local_names.add(obj.name)
					elif isinstance(obj, H.HLet):
						local_names.add(obj.name)
					elif isinstance(obj, H.HMatchArm):
						for name in obj.binders:
							local_names.add(name)
					elif isinstance(obj, H.HCatchArm):
						if obj.binder:
							local_names.add(obj.binder)
					elif isinstance(obj, H.HTryExprArm):
						if obj.binder:
							local_names.add(obj.binder)
					if isinstance(obj, H.HExpr):
						for child in obj.__dict__.values():
							_collect_local_names(child)
					elif isinstance(obj, H.HStmt):
						for child in obj.__dict__.values():
							_collect_local_names(child)
					elif isinstance(obj, H.HBlock):
						for stmt in obj.statements:
							_collect_local_names(stmt)
					elif isinstance(obj, list):
						for item in obj:
							_collect_local_names(item)
					elif isinstance(obj, dict):
						for item in obj.values():
							_collect_local_names(item)

				if lam.body_expr is not None:
					_collect_local_names(lam.body_expr)
				if lam.body_block is not None:
					_collect_local_names(lam.body_block)
				collisions = sorted(set(local_names) & set(capture_name_to_id))
				if collisions:
					for name in collisions[:6]:
						type_diags.append(
							Diagnostic(
								message=f"capture name '{name}' collides with a local binding",
								code="E_CAPTURE_NAME_COLLIDES_WITH_LOCAL",
								severity="error",
								phase="typecheck",
								span=capture_spans.get(name, Span()),
							)
						)
					continue
				def _apply_capture_names(obj: object) -> None:
					if obj is None:
						return
					if isinstance(obj, H.HVar):
						if getattr(obj, "binding_id", None) is None and obj.name in capture_name_to_id:
							obj.binding_id = capture_name_to_id[obj.name]
					elif isinstance(obj, H.HPlaceExpr):
						base = obj.base
						if (
							isinstance(base, H.HVar)
							and getattr(base, "binding_id", None) is None
							and base.name in capture_name_to_id
						):
							base.binding_id = capture_name_to_id[base.name]
					if isinstance(obj, H.HExpr):
						for child in obj.__dict__.values():
							_apply_capture_names(child)
					elif isinstance(obj, H.HStmt):
						for child in obj.__dict__.values():
							_apply_capture_names(child)
					elif isinstance(obj, H.HBlock):
						for stmt in obj.statements:
							_apply_capture_names(stmt)
					elif isinstance(obj, list):
						for item in obj:
							_apply_capture_names(item)
					elif isinstance(obj, dict):
						for item in obj.values():
							_apply_capture_names(item)
				if lam.body_expr is not None:
					_apply_capture_names(lam.body_expr)
				if lam.body_block is not None:
					_apply_capture_names(lam.body_block)
		for param in lam.params:
			param.binding_id = None
		if lam.body_expr is not None:
			lambda_body = H.HBlock(statements=[H.HReturn(value=lam.body_expr)])
		elif lam.body_block is not None:
			lambda_body = lam.body_block
		else:
			raise AssertionError("hidden lambda missing body (checker bug)")
		lambda_body = normalize_hir(lambda_body)
		def _apply_capture_names_post(obj: object, name_map: dict[str, int]) -> None:
			if obj is None:
				return
			if isinstance(obj, H.HVar):
				if obj.name in name_map:
					target_id = name_map[obj.name]
					if getattr(obj, "binding_id", None) != target_id:
						obj.binding_id = target_id
			elif isinstance(obj, H.HPlaceExpr):
				base = obj.base
				if (
					isinstance(base, H.HVar)
					and base.name in name_map
				):
					target_id = name_map[base.name]
					if getattr(base, "binding_id", None) != target_id:
						base.binding_id = target_id
			if isinstance(obj, H.HLambda):
				if obj.body_expr is not None:
					_apply_capture_names_post(obj.body_expr, name_map)
				if obj.body_block is not None:
					_apply_capture_names_post(obj.body_block, name_map)
			elif isinstance(obj, H.HExpr):
				for child in obj.__dict__.values():
					_apply_capture_names_post(child, name_map)
			elif isinstance(obj, H.HStmt):
				for child in obj.__dict__.values():
					_apply_capture_names_post(child, name_map)
			elif isinstance(obj, H.HBlock):
				for stmt in obj.statements:
					_apply_capture_names_post(stmt, name_map)
			elif isinstance(obj, list):
				for item in obj:
					_apply_capture_names_post(item, name_map)
			elif isinstance(obj, dict):
				for item in obj.values():
					_apply_capture_names_post(item, name_map)

		if capture_name_to_id:
			_apply_capture_names_post(lambda_body, capture_name_to_id)
		capture_id_set = set(capture_id_map.values()) | set(capture_id_map.keys())
		if capture_name_to_id:
			capture_id_set.update(capture_name_to_id.values())
		if capture_id_set:
			def _remap_lambda_local_collisions(block: H.HBlock, capture_ids: set[int]) -> None:
				max_id = 0
				for stmt in block.statements:
					if isinstance(stmt, H.HLocalConst) and stmt.binding_id is not None:
						max_id = max(max_id, int(stmt.binding_id))
					if isinstance(stmt, H.HLet) and stmt.binding_id is not None:
						max_id = max(max_id, int(stmt.binding_id))
				if capture_ids:
					max_id = max(max_id, max(capture_ids))
				remap_by_name: dict[tuple[int, str], int] = {}
				def _scan_expr(expr: H.HExpr) -> None:
					if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
						for arm in expr.arms:
							for s in arm.block.statements:
								_scan_stmt(s)
						if expr.scrutinee is not None:
							_scan_expr(expr.scrutinee)
					elif hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
						_scan_expr(expr.attempt)
						for arm in expr.arms:
							for s in arm.block.statements:
								_scan_stmt(s)
							if arm.result is not None:
								_scan_expr(arm.result)
					elif hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
						for s in expr.body.statements:
							_scan_stmt(s)
						_scan_expr(expr.result)
					elif isinstance(expr, H.HCall):
						_scan_expr(expr.fn)
						for a in expr.args:
							_scan_expr(a)
						for kw in getattr(expr, "kwargs", []) or []:
							_scan_expr(kw.value)
					elif isinstance(expr, H.HMethodCall):
						_scan_expr(expr.receiver)
						for a in expr.args:
							_scan_expr(a)
						for kw in getattr(expr, "kwargs", []) or []:
							_scan_expr(kw.value)
					elif isinstance(expr, H.HInvoke):
						_scan_expr(expr.callee)
						for a in expr.args:
							_scan_expr(a)
						for kw in getattr(expr, "kwargs", []) or []:
							_scan_expr(kw.value)
					elif isinstance(expr, H.HBinary):
						_scan_expr(expr.left)
						_scan_expr(expr.right)
					elif isinstance(expr, H.HUnary):
						_scan_expr(expr.expr)
					elif isinstance(expr, H.HTernary):
						_scan_expr(expr.cond)
						_scan_expr(expr.then_expr)
						_scan_expr(expr.else_expr)
					elif isinstance(expr, H.HField):
						_scan_expr(expr.subject)
					elif isinstance(expr, H.HIndex):
						_scan_expr(expr.subject)
						_scan_expr(expr.index)
					elif isinstance(expr, H.HBorrow):
						_scan_expr(expr.subject)
					elif hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
						_scan_expr(expr.subject)
					elif hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
						_scan_expr(expr.subject)
					elif isinstance(expr, H.HArrayLiteral):
						for el in expr.elements:
							_scan_expr(el)
					elif isinstance(expr, H.HDVInit):
						for a in expr.args:
							_scan_expr(a)
					elif isinstance(expr, H.HExceptionInit):
						for a in expr.pos_args:
							_scan_expr(a)
						for kw in getattr(expr, "kw_args", []) or []:
							_scan_expr(kw.value)
				def _scan_stmt(stmt: H.HStmt) -> None:
					nonlocal max_id
					if isinstance(stmt, H.HLocalConst) and stmt.binding_id is not None:
						bid = int(stmt.binding_id)
						if bid in capture_ids:
							max_id += 1
							remap_by_name[(bid, stmt.name)] = max_id
							stmt.binding_id = max_id
					elif isinstance(stmt, H.HLet) and stmt.binding_id is not None:
						bid = int(stmt.binding_id)
						if bid in capture_ids:
							max_id += 1
							remap_by_name[(bid, stmt.name)] = max_id
							stmt.binding_id = max_id
					elif isinstance(stmt, H.HLet):
						_scan_expr(stmt.value)
					elif isinstance(stmt, H.HExprStmt):
						_scan_expr(stmt.expr)
					elif isinstance(stmt, H.HIf):
						for s in stmt.then_block.statements:
							_scan_stmt(s)
						if stmt.else_block:
							for s in stmt.else_block.statements:
								_scan_stmt(s)
					elif isinstance(stmt, H.HLoop):
						for s in stmt.body.statements:
							_scan_stmt(s)
					elif isinstance(stmt, H.HTry):
						for s in stmt.body.statements:
							_scan_stmt(s)
						for arm in stmt.catches:
							for s in arm.block.statements:
								_scan_stmt(s)
					elif isinstance(stmt, H.HBlock):
						for s in stmt.statements:
							_scan_stmt(s)
					elif hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
						for s in stmt.block.statements:
							_scan_stmt(s)
				for s in block.statements:
					_scan_stmt(s)

				if not remap_by_name:
					return

				def _remap_expr(expr: H.HExpr) -> None:
					if isinstance(expr, H.HVar):
						bid = getattr(expr, "binding_id", None)
						if bid is not None:
							key = (int(bid), expr.name)
							if key in remap_by_name:
								expr.binding_id = remap_by_name[key]
					elif isinstance(expr, H.HPlaceExpr):
						base = expr.base
						if isinstance(base, H.HVar):
							bid = getattr(base, "binding_id", None)
							if bid is not None:
								key = (int(bid), base.name)
								if key in remap_by_name:
									base.binding_id = remap_by_name[key]
						for proj in expr.projections:
							if isinstance(proj, H.HPlaceIndex):
								_remap_expr(proj.index)
					elif isinstance(expr, H.HBinary):
						_remap_expr(expr.left)
						_remap_expr(expr.right)
					elif isinstance(expr, H.HUnary):
						_remap_expr(expr.expr)
					elif isinstance(expr, H.HTernary):
						_remap_expr(expr.cond)
						_remap_expr(expr.then_expr)
						_remap_expr(expr.else_expr)
					elif isinstance(expr, H.HCall):
						_remap_expr(expr.fn)
						for a in expr.args:
							_remap_expr(a)
						for kw in getattr(expr, "kwargs", []) or []:
							_remap_expr(kw.value)
					elif isinstance(expr, H.HMethodCall):
						_remap_expr(expr.receiver)
						for a in expr.args:
							_remap_expr(a)
						for kw in getattr(expr, "kwargs", []) or []:
							_remap_expr(kw.value)
					elif isinstance(expr, H.HInvoke):
						_remap_expr(expr.callee)
						for a in expr.args:
							_remap_expr(a)
						for kw in getattr(expr, "kwargs", []) or []:
							_remap_expr(kw.value)
					elif isinstance(expr, H.HField):
						_remap_expr(expr.subject)
					elif isinstance(expr, H.HIndex):
						_remap_expr(expr.subject)
						_remap_expr(expr.index)
					elif isinstance(expr, H.HBorrow):
						_remap_expr(expr.subject)
					elif hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
						_remap_expr(expr.subject)
					elif hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
						_remap_expr(expr.subject)
					elif isinstance(expr, H.HArrayLiteral):
						for el in expr.elements:
							_remap_expr(el)
					elif isinstance(expr, H.HDVInit):
						for a in expr.args:
							_remap_expr(a)
					elif isinstance(expr, H.HExceptionInit):
						for a in expr.pos_args:
							_remap_expr(a)
						for kw in getattr(expr, "kw_args", []) or []:
							_remap_expr(kw.value)
					elif hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
						_remap_expr(expr.attempt)
						for arm in expr.arms:
							for s in arm.block.statements:
								_remap_stmt(s)
							if arm.result is not None:
								_remap_expr(arm.result)
					elif hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
						for s in expr.body.statements:
							_remap_stmt(s)
						_remap_expr(expr.result)
					elif hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
						_remap_expr(expr.scrutinee)
						for arm in expr.arms:
							for s in arm.block.statements:
								_remap_stmt(s)
							if arm.result is not None:
								_remap_expr(arm.result)

				def _remap_stmt(stmt: H.HStmt) -> None:
					if isinstance(stmt, H.HLocalConst):
						pass  # literal value, no remapping needed
					elif isinstance(stmt, H.HLet):
						_remap_expr(stmt.value)
					elif isinstance(stmt, H.HAssign):
						_remap_expr(stmt.target)
						_remap_expr(stmt.value)
					elif isinstance(stmt, H.HReturn) and stmt.value is not None:
						_remap_expr(stmt.value)
					elif isinstance(stmt, H.HExprStmt):
						_remap_expr(stmt.expr)
					elif isinstance(stmt, H.HIf):
						_remap_expr(stmt.cond)
						for s in stmt.then_block.statements:
							_remap_stmt(s)
						if stmt.else_block:
							for s in stmt.else_block.statements:
								_remap_stmt(s)
					elif isinstance(stmt, H.HLoop):
						for s in stmt.body.statements:
							_remap_stmt(s)
					elif isinstance(stmt, H.HTry):
						for s in stmt.body.statements:
							_remap_stmt(s)
						for arm in stmt.catches:
							for s in arm.block.statements:
								_remap_stmt(s)
					elif isinstance(stmt, H.HBlock):
						for s in stmt.statements:
							_remap_stmt(s)
					elif hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
						for s in stmt.block.statements:
							_remap_stmt(s)

				for s in block.statements:
					_remap_stmt(s)

			_remap_lambda_local_collisions(lambda_body, capture_id_set)
		lam_param_names = [p.name for p in lam.params]
		if spec.has_captures:
			lam_param_type_ids = list(spec.param_type_ids[1:])
		else:
			lam_param_type_ids = list(spec.param_type_ids)
		param_types = {name: ty for name, ty in zip(lam_param_names, lam_param_type_ids)}
		preseed_scope_env: dict[str, TypeId] = {}
		preseed_scope_bindings: dict[str, int] = {}
		preseed_binding_types: dict[int, TypeId] = {}
		preseed_binding_names: dict[int, str] = {}
		preseed_binding_mutable: dict[int, bool] = {}
		preseed_binding_place_kind: dict[int, PlaceKind] = {}
		remapped_capture_map: dict[C.HCaptureKey, int] = {}
		if origin_typed is not None and lam.explicit_captures:
			name_to_bid: dict[str, int] = {}
			for bid, name in origin_typed.binding_names.items():
				name_to_bid[name] = int(bid)
			for cap in lam.explicit_captures or []:
				if cap.binding_id is None and cap.name and cap.name in name_to_bid:
					cap.binding_id = name_to_bid[cap.name]
		for cap in lam.explicit_captures or []:
			if getattr(cap, "binding_id", None) is not None and cap.name:
				preseed_binding_names.setdefault(int(cap.binding_id), cap.name)
			elif cap.name:
				preseed_scope_env.setdefault(cap.name, shared_type_table.ensure_unknown())
		for key, slot in getattr(spec, "capture_map", {}).items():
			new_root = capture_id_map.get(int(key.root_local), int(key.root_local))
			new_key = C.HCaptureKey(root_local=new_root, proj=key.proj)
			remapped_capture_map[new_key] = slot
		rev_capture_id_map = {new: old for old, new in capture_id_map.items()}
		origin_mir = mir_funcs_by_id.get(spec.origin_fn_id) if spec.origin_fn_id is not None else None
		if origin_typed is not None:
			def _dbg_ty(tid: TypeId | None) -> str:
				if tid is None:
					return "None"
				try:
					td = shared_type_table.get(tid)
					return f"{tid}:{td.kind.name}:{td.name}"
				except Exception:
					return str(tid)
			for cap in lam.captures or []:
				bid = int(cap.key.root_local)
				orig_bid = rev_capture_id_map.get(bid, bid)
				cap_name = origin_typed.binding_names.get(orig_bid, f"__cap_{orig_bid}")
				cap_ty = origin_typed.binding_types.get(orig_bid, shared_type_table.ensure_unknown())
				if spec.env_field_types is not None:
					slot = remapped_capture_map.get(cap.key)
					if slot is not None and slot < len(spec.env_field_types):
						candidate = spec.env_field_types[slot]
						if shared_type_table.has_typevar(cap_ty) or shared_type_table.get(cap_ty).kind is TypeKind.UNKNOWN:
							cap_ty = candidate
						if drift_debug.enabled("lambda_capture"):
							import sys
							print(
								f"[drift:debug][lambda_capture] fn={spec.fn_id} cap_root={cap.key.root_local} orig_bid={orig_bid} slot={slot} origin_ty={_dbg_ty(origin_typed.binding_types.get(orig_bid))} env_ty={_dbg_ty(candidate)} final={_dbg_ty(cap_ty)}",
								file=sys.stderr,
							)
					elif drift_debug.enabled("lambda_capture"):
						import sys
						print(
							f"[drift:debug][lambda_capture] fn={spec.fn_id} cap_root={cap.key.root_local} orig_bid={orig_bid} slot=None origin_ty={_dbg_ty(origin_typed.binding_types.get(orig_bid))} env_ty=None final={_dbg_ty(cap_ty)}",
							file=sys.stderr,
						)
				elif drift_debug.enabled("lambda_capture"):
					import sys
					print(
						f"[drift:debug][lambda_capture] fn={spec.fn_id} cap_root={cap.key.root_local} orig_bid={orig_bid} slot=None origin_ty={_dbg_ty(origin_typed.binding_types.get(orig_bid))} env_ty=None final={_dbg_ty(cap_ty)}",
						file=sys.stderr,
					)
				preseed_binding_types[bid] = cap_ty
				preseed_binding_names[bid] = cap_name
				preseed_binding_mutable[bid] = origin_typed.binding_mutable.get(orig_bid, False)
				preseed_binding_place_kind[bid] = PlaceKind.CAPTURE
		if preseed_binding_names:
			unknown_ty = shared_type_table.ensure_unknown()
			for bid, name in preseed_binding_names.items():
				ty = preseed_binding_types.get(bid, unknown_ty)
				preseed_scope_env.setdefault(name, ty)
		for bid, name in preseed_binding_names.items():
			preseed_scope_bindings.setdefault(name, int(bid))
		if drift_debug.enabled("stage2"):
			import sys
			print(f"[drift:debug] hidden lambda {spec.fn_id} origin={spec.origin_fn_id} captures={len(lam.captures or [])} preseed_bindings={sorted(preseed_binding_types.keys())}", file=sys.stderr)
		mod_name = spec.fn_id.module or "main"
		current_mod = _module_id_with_visibility(mod_name)
		visible_mods = None
		if module_deps is not None:
			visible = visible_module_names_by_name.get(mod_name, {mod_name})
			visible_mods = tuple(sorted(_module_id_with_visibility(m) for m in visible))
		_sync_visibility_provenance()
		current_file = None
		if origin_by_fn_id is not None and spec.origin_fn_id is not None:
			current_file = str(origin_by_fn_id.get(spec.origin_fn_id))
		if current_file is None:
			origin_sig = signatures_by_id.get(spec.origin_fn_id) if spec.origin_fn_id is not None else None
			current_file = Span.from_loc(getattr(origin_sig, "loc", None)).file if origin_sig is not None else None
		param_mutable = None
		if spec.lambda_expr is not None:
			param_mutable = {p.name: bool(getattr(p, "is_mutable", False)) for p in spec.lambda_expr.params}
		hidden_typed = type_checker.check_function(
			fn_id=spec.fn_id,
			body=lambda_body,
			param_types=param_types,
			param_mutable=param_mutable,
			return_type=spec.return_type_id,
			signatures_by_id=signatures_by_id,
			function_keys_by_fn_id=function_keys_by_fn_id,
			callable_registry=callable_registry,
			impl_index=impl_index,
			trait_index=trait_index,
			trait_impl_index=trait_impl_index,
			trait_scope_by_module=trait_scope_by_module,
			linked_world=linked_world,
			require_env=require_env,
			visible_modules=visible_mods,
			current_module=current_mod,
			visibility_provenance=visibility_provenance_by_id,
			visibility_imports=None,
			preseed_binding_types=preseed_binding_types,
			preseed_binding_names=preseed_binding_names,
			preseed_binding_mutable=preseed_binding_mutable,
			preseed_binding_place_kind=preseed_binding_place_kind,
			preseed_scope_env=preseed_scope_env,
			preseed_scope_bindings=preseed_scope_bindings,
		)
		if hidden_typed.diagnostics:
			type_diags.extend(hidden_typed.diagnostics)
			continue
		hidden_typed_fn = hidden_typed.typed_fn
		typed_fns_by_id[spec.fn_id] = hidden_typed_fn
		_rewrite_call_targets(hidden_typed_fn, lambda_body)
		def _patch_hidden_lambda_call_info_from_sigs() -> None:
			call_info_map = getattr(hidden_typed_fn, "call_info_by_callsite_id", None)
			if not isinstance(call_info_map, dict):
				return
			for csid, info in list(call_info_map.items()):
				if info is None:
					continue
				sig = info.sig
				if sig is None:
					continue
				user_ret = sig.user_ret_type
				if user_ret is not None:
					td = shared_type_table.get(user_ret)
					if td.kind is not TypeKind.UNKNOWN and not shared_type_table.has_typevar(user_ret):
						continue
				target = info.target
				if target.kind is not CallTargetKind.DIRECT or target.symbol is None:
					continue
				target_sig = signatures_by_id.get(target.symbol)
				if target_sig is None or target_sig.return_type_id is None:
					continue
				ret_id = target_sig.return_type_id
				ret_def = shared_type_table.get(ret_id)
				if ret_def.kind is TypeKind.UNKNOWN:
					continue
				param_ids = target_sig.param_type_ids or sig.param_types
				new_sig = CallSig(param_types=tuple(param_ids), user_ret_type=ret_id, can_throw=sig.can_throw, includes_callee=sig.includes_callee)
				call_info_map[csid] = CallInfo(target=target, sig=new_sig)
		_patch_hidden_lambda_call_info_from_sigs()
		type_diags.extend(_typevar_callinfo_diags(hidden_typed_fn, shared_type_table))
		hidden_ret_type = spec.return_type_id
		if shared_type_table is not None:
			try:
				if shared_type_table.get(hidden_ret_type).kind is TypeKind.UNKNOWN:
					hidden_ret_type = _hidden_lambda_ret_type(lambda_body, hidden_typed_fn, shared_type_table)
			except Exception:
				hidden_ret_type = _hidden_lambda_ret_type(lambda_body, hidden_typed_fn, shared_type_table)
		hidden_sig = FnSignature(
			name=function_symbol(spec.fn_id),
			param_type_ids=list(spec.param_type_ids),
			param_names=list(spec.param_names),
			return_type_id=hidden_ret_type,
			declared_can_throw=bool(spec.can_throw),
			module=spec.fn_id.module,
		)
		_register_synth_signature(spec.fn_id, hidden_sig)
		builder = make_builder(spec.fn_id)
		builder.func.params = list(spec.param_names)
		binding_types = getattr(hidden_typed_fn, "binding_types", None) or preseed_binding_types
		lower = HIRToMIR(
				builder,
				type_table=shared_type_table,
				exc_env=exc_env,
				param_types=param_types,
				expr_types=getattr(hidden_typed_fn, "expr_types", None),
				iface_coercions=getattr(hidden_typed_fn, "iface_coercions", None),
				signatures_by_id=signatures_by_id,
				current_fn_id=spec.fn_id,
				type_param_subst=getattr(hidden_typed_fn, "preseed_type_params", None),
				call_info_by_callsite_id=hidden_typed_fn.call_info_by_callsite_id,
				can_throw_by_id={**declared_by_id, spec.fn_id: bool(spec.can_throw)},
				return_type=hidden_ret_type,
				binding_types=binding_types,
				typed_mode=_typed_mode_for(hidden_typed_fn, shared_type_table, not _has_error(hidden_typed.diagnostics)),
			)
		lower._lambda_capture_ref_is_value = spec.lambda_capture_ref_is_value
		lower._lambda_is_callback = bool(getattr(spec, "is_callback_lambda", False))
		if spec.has_captures:
			lower._lambda_env_local = spec.param_names[0]
			lower._lambda_env_ty = spec.env_ty
			env_field_types = list(spec.env_field_types)
			if env_field_types and preseed_binding_types:
				for cap in lam.captures or []:
					bid = int(cap.key.root_local)
					slot = remapped_capture_map.get(cap.key)
					if slot is None or slot >= len(env_field_types):
						continue
					cap_ty = preseed_binding_types.get(bid)
					if cap_ty is None:
						continue
					if shared_type_table.get(cap_ty).kind is TypeKind.UNKNOWN:
						continue
					env_field_types[slot] = cap_ty
			lower._lambda_env_field_types = env_field_types
			lower._lambda_capture_slots = remapped_capture_map
			name_to_slot: dict[str, int] = {}
			for key, slot in remapped_capture_map.items():
				name = preseed_binding_names.get(int(key.root_local))
				if name:
					name_to_slot[name] = slot
			lower._lambda_capture_name_to_slot = name_to_slot
			lower._lambda_capture_kinds = list(spec.capture_kinds)
			for bid, name in preseed_binding_names.items():
				lower._binding_names[bid] = name
			for bid, ty in preseed_binding_types.items():
				name = preseed_binding_names.get(bid, f"__b{bid}")
				local_name = lower._canonical_local(bid, name)
				if local_name not in lower._local_types:
					lower._local_types[local_name] = ty
		for param in lam.params:
			if getattr(param, "binding_id", None) is not None:
				lower._binding_names[int(param.binding_id)] = param.name
		lower._seed_lambda_locals_for_inference(lower, lambda_body)
		try:
			ret_val = lower._lower_lambda_block(lower, lambda_body)
		except AssertionError as err:
			_append_boundary_contract_diag(
				checked,
				phase="mir_validate",
				prefix="MIR lowering contract failure",
				err=err,
				fn_id=spec.fn_id,
				signatures_by_id=signatures_by_id,
				hir_block=lambda_body,
				origin_by_fn_id=origin_by_fn_id,
			)
			_assert_all_phased(checked.diagnostics, context="compile_stubbed_funcs")
			if return_checked:
				if return_ssa:
					return {}, checked, None
				return {}, checked
			return {}
		for synth_spec in lower.synth_sig_specs():
			if synth_spec.kind == "hidden_lambda":
				continue
			_register_synth_signature(synth_spec.fn_id, synth_spec.sig)
		if getattr(lower, "hidden_lambda_specs", None):
			hidden_lambda_specs.extend(lower.hidden_lambda_specs())
		if spec.has_captures and spec.env_ty is not None:
			inst = shared_type_table.get_struct_instance(spec.env_ty)
			if inst is None:
				schema = shared_type_table.get_struct_schema(spec.env_ty)
				if schema is not None and not schema.type_params:
					inst_id = shared_type_table.ensure_struct_instantiated(spec.env_ty, [])
					inst = shared_type_table.get_struct_instance(inst_id)
			if inst is not None:
				unknown_ty = shared_type_table.ensure_unknown()
				cap_kind_by_key: dict[C.HCaptureKey, C.HCaptureKind] = {}
				for cap in lam.captures or []:
					cap_kind_by_key[cap.key] = cap.kind
				if not remapped_capture_map and lam.captures:
					for idx, cap in enumerate(lam.captures):
						if idx >= len(inst.field_types):
							continue
						bid = int(cap.key.root_local)
						name = preseed_binding_names.get(bid, f"__b{bid}")
						local_name = lower._canonical_local(bid, name)
						cur_ty = lower._local_types.get(local_name)
						ty = inst.field_types[idx]
						if ty is not None and ty != unknown_ty:
							force_ref = (not spec.lambda_capture_ref_is_value and cap.kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT))
							if force_ref:
								lower._local_types[local_name] = ty
							elif cur_ty is None or cur_ty == unknown_ty:
								lower._local_types[local_name] = ty
				for key, slot in remapped_capture_map.items():
					bid = int(key.root_local)
					name = preseed_binding_names.get(bid, f"__b{bid}")
					local_name = lower._canonical_local(bid, name)
					cur_ty = lower._local_types.get(local_name)
					if slot < len(inst.field_types):
						ty = inst.field_types[slot]
						if ty is not None and ty != unknown_ty:
							kind = cap_kind_by_key.get(key)
							force_ref = (not spec.lambda_capture_ref_is_value and kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT))
							if force_ref:
								lower._local_types[local_name] = ty
							elif cur_ty is None or cur_ty == unknown_ty:
								lower._local_types[local_name] = ty
		builder.func.local_types = dict(lower._local_types)
		unknown_ty = shared_type_table.ensure_unknown()
		for local_name in builder.func.locals:
			if local_name not in builder.func.local_types:
				builder.func.local_types[local_name] = unknown_ty
		if builder.block.terminator is None:
			if ret_val is None:
				_ret_def = shared_type_table.get(spec.return_type_id) if shared_type_table is not None else None
				if _ret_def is not None and _ret_def.kind is TypeKind.VOID:
					ret_val = lower._void_value()
				else:
					raise AssertionError("hidden lambda block must end with a value or return")
			if spec.can_throw:
				ok_dest = builder.new_temp()
				builder.emit(M.ConstructResultOk(dest=ok_dest, value=ret_val))
				ret_val = ok_dest
			builder.set_terminator(M.Return(value=ret_val))
		mir_funcs_by_id[spec.fn_id] = builder.func
	if _timing_enabled and hidden_lambda_start is not None:
		import time as _timing_time
		import sys as _timing_sys
		print(f"[drift:debug][timing] hidden_lambda_lowering={_timing_time.perf_counter() - hidden_lambda_start:.3f}s", file=_timing_sys.stderr)

	if len(type_diags) != type_diag_len:
		checked.diagnostics.extend(type_diags[type_diag_len:])
		if any(d.severity == "error" for d in checked.diagnostics):
			if return_checked:
				if return_ssa:
					return {}, checked, None
				return {}, checked
			return {}

	type_diag_len = len(type_diags)
	for spec in type_checker.thunk_specs():
		if spec.thunk_fn_id in mir_funcs_by_id:
			continue
		param_names = [f"p{i}" for i in range(len(spec.param_types))]
		sig = FnSignature(
			name=function_symbol(spec.thunk_fn_id),
			param_type_ids=list(spec.param_types),
			param_names=param_names,
			return_type_id=spec.return_type,
			declared_can_throw=True,
			module=spec.thunk_fn_id.module,
		)
		_register_synth_signature(spec.thunk_fn_id, sig)
		builder = make_builder(spec.thunk_fn_id)
		builder.func.params = list(param_names)
		if spec.kind is ThunkKind.OK_WRAP:
			call_dest: M.ValueId | None
			if shared_type_table is not None and shared_type_table.is_void(spec.return_type):
				call_dest = None
			else:
				call_dest = builder.new_temp()
			builder.emit(M.Call(dest=call_dest, fn_id=spec.target_fn_id, args=param_names, can_throw=False))
			ok_dest = builder.new_temp()
			builder.emit(M.ConstructResultOk(dest=ok_dest, value=call_dest))
			builder.set_terminator(M.Return(value=ok_dest))
		else:
			call_dest = builder.new_temp()
			builder.emit(M.Call(dest=call_dest, fn_id=spec.target_fn_id, args=param_names, can_throw=True))
			builder.set_terminator(M.Return(value=call_dest))
		mir_funcs_by_id[spec.thunk_fn_id] = builder.func

	if len(type_diags) != type_diag_len:
		checked.diagnostics.extend(type_diags[type_diag_len:])
		if any(d.severity == "error" for d in checked.diagnostics):
			if return_checked:
				if return_ssa:
					return {}, checked, None
				return {}, checked
			return {}

	type_diag_len = len(type_diags)
	for spec in type_checker.lambda_fn_specs():
		if spec.fn_id in mir_funcs_by_id:
			continue
		lam = copy.deepcopy(spec.lambda_expr)
		param_names = [p.name for p in lam.params]
		param_types = {name: ty for name, ty in zip(param_names, spec.param_types)}
		lambda_body: H.HBlock
		if lam.body_expr is not None:
			lambda_body = H.HBlock(statements=[H.HReturn(value=lam.body_expr)])
		elif lam.body_block is not None:
			lambda_body = lam.body_block
		else:
			raise AssertionError("captureless lambda missing body (checker bug)")
		lambda_body = normalize_hir(lambda_body)
		current_mod = _module_id_with_visibility(spec.fn_id.module or "main")
		visible_mods = None
		if module_deps is not None:
			visible = visible_module_names_by_name.get(spec.fn_id.module or "main", {spec.fn_id.module or "main"})
			visible_mods = tuple(sorted(_module_id_with_visibility(m) for m in visible))
		_sync_visibility_provenance()
		current_file = None
		if origin_by_fn_id is not None and spec.origin_fn_id is not None:
			current_file = str(origin_by_fn_id.get(spec.origin_fn_id))
		if current_file is None:
			origin_sig = signatures_by_id.get(spec.origin_fn_id) if spec.origin_fn_id is not None else None
			current_file = Span.from_loc(getattr(origin_sig, "loc", None)).file if origin_sig is not None else None
		if current_file is None:
			current_file = Span.from_loc(getattr(spec.lambda_expr, "loc", None)).file
		param_mutable = None
		if spec.lambda_expr is not None:
			param_mutable = {p.name: bool(getattr(p, "is_mutable", False)) for p in spec.lambda_expr.params}
		lambda_result = type_checker.check_function(
			fn_id=spec.fn_id,
			body=lambda_body,
			param_types=param_types,
			param_mutable=param_mutable,
			return_type=spec.return_type,
			signatures_by_id=signatures_by_id,
			function_keys_by_fn_id=function_keys_by_fn_id,
			callable_registry=callable_registry,
			impl_index=impl_index,
			trait_index=trait_index,
			trait_impl_index=trait_impl_index,
			trait_scope_by_module=trait_scope_by_module,
			linked_world=linked_world,
			require_env=require_env,
			visible_modules=visible_mods,
			current_module=current_mod,
			visibility_provenance=visibility_provenance_by_id,
			visibility_imports=None,
		)
		if lambda_result.diagnostics:
			type_diags.extend(lambda_result.diagnostics)
			continue
		lambda_typed_fn = lambda_result.typed_fn
		_rewrite_call_targets(lambda_typed_fn, lambda_body)
		type_diags.extend(_typevar_callinfo_diags(lambda_typed_fn, shared_type_table))
		call_info_map = getattr(lambda_typed_fn, "call_info_by_callsite_id", None)
		lambda_call_info: dict[int, CallInfo] | None = dict(call_info_map) if isinstance(call_info_map, dict) else None
		lambda_ret_type = _hidden_lambda_ret_type(lambda_body, lambda_typed_fn, shared_type_table)
		sig = FnSignature(
			name=function_symbol(spec.fn_id),
			param_type_ids=list(spec.param_types),
			param_names=list(param_names),
			return_type_id=lambda_ret_type,
			declared_can_throw=bool(spec.can_throw),
			module=spec.fn_id.module,
		)
		_register_synth_signature(spec.fn_id, sig)
		builder = make_builder(spec.fn_id)
		builder.func.params = list(param_names)
		lower = HIRToMIR(
			builder,
			type_table=shared_type_table,
			exc_env=exc_env,
			param_types=param_types,
			expr_types=getattr(lambda_typed_fn, "expr_types", None),
			iface_coercions=getattr(lambda_typed_fn, "iface_coercions", None),
			signatures_by_id=signatures_by_id,
			current_fn_id=spec.fn_id,
			type_param_subst=getattr(lambda_typed_fn, "preseed_type_params", None),
			call_info_by_callsite_id=lambda_call_info or lambda_typed_fn.call_info_by_callsite_id,
			can_throw_by_id={**declared_by_id, spec.fn_id: bool(spec.can_throw)},
			return_type=lambda_ret_type,
			typed_mode=_typed_mode_for(lambda_typed_fn, shared_type_table, not _has_error(lambda_result.diagnostics)),
		)
		for param in lam.params:
			if getattr(param, "binding_id", None) is not None:
				lower._binding_names[int(param.binding_id)] = param.name
		lower._seed_lambda_locals_for_inference(lower, lambda_body)
		try:
			ret_val = lower._lower_lambda_block(lower, lambda_body)
		except AssertionError as err:
			_append_boundary_contract_diag(
				checked,
				phase="mir_validate",
				prefix="MIR lowering contract failure",
				err=err,
				fn_id=spec.fn_id,
				signatures_by_id=signatures_by_id,
				hir_block=lambda_body,
				origin_by_fn_id=origin_by_fn_id,
			)
			_assert_all_phased(checked.diagnostics, context="compile_stubbed_funcs")
			if return_checked:
				if return_ssa:
					return {}, checked, None
				return {}, checked
			return {}
		builder.func.local_types = dict(lower._local_types)
		unknown_ty = shared_type_table.ensure_unknown()
		for local_name in builder.func.locals:
			if local_name not in builder.func.local_types:
				builder.func.local_types[local_name] = unknown_ty
		if builder.block.terminator is None:
			if ret_val is None:
				raise AssertionError("captureless lambda block must end with a value or return")
			if spec.can_throw:
				ok_dest = builder.new_temp()
				builder.emit(M.ConstructResultOk(dest=ok_dest, value=ret_val))
				ret_val = ok_dest
			builder.set_terminator(M.Return(value=ret_val))
		mir_funcs_by_id[spec.fn_id] = builder.func
	if len(type_diags) != type_diag_len:
		checked.diagnostics.extend(type_diags[type_diag_len:])
		if any(d.severity == "error" for d in checked.diagnostics):
			if return_checked:
				if return_ssa:
					return {}, checked, None
				return {}, checked
			return {}

	for spec in method_wrapper_specs:
		if spec.wrapper_fn_id in mir_funcs_by_id:
			continue
		wrap_sig = signatures_by_id.get(spec.wrapper_fn_id)
		if wrap_sig is None or wrap_sig.param_type_ids is None:
			continue
		param_names = list(wrap_sig.param_names or [])
		if len(param_names) != len(wrap_sig.param_type_ids):
			param_names = [f"p{i}" for i in range(len(wrap_sig.param_type_ids))]
		_register_synth_signature(spec.wrapper_fn_id, wrap_sig)
		builder = make_builder(spec.wrapper_fn_id)
		builder.func.params = list(param_names)
		call_dest: M.ValueId | None
		if shared_type_table is not None and shared_type_table.is_void(wrap_sig.return_type_id):
			call_dest = None
		else:
			call_dest = builder.new_temp()
		builder.emit(
			M.Call(
				dest=call_dest,
				fn_id=spec.target_fn_id,
				args=param_names,
				can_throw=False,
			)
		)
		ok_dest = builder.new_temp()
		builder.emit(M.ConstructResultOk(dest=ok_dest, value=call_dest))
		builder.set_terminator(M.Return(value=ok_dest))
		mir_funcs_by_id[spec.wrapper_fn_id] = builder.func

	with _timed("mir_validate"):
		def _run_mir_validator(name: str, action: Callable[[], None]) -> bool:
			try:
				action()
				return True
			except AssertionError as err:
				_append_boundary_contract_diag(
					checked,
					phase="mir_validate",
					prefix=f"MIR validation contract failure ({name})",
					err=err,
					fn_id=FunctionId(module=entry_module, name=entry_name, ordinal=0),
					signatures_by_id=signatures_by_id,
					origin_by_fn_id=origin_by_fn_id,
				)
				_assert_all_phased(checked.diagnostics, context="compile_stubbed_funcs")
				if return_checked:
					if return_ssa:
						return False
					return False
				return False

		if shared_type_table is not None:
			if not _run_mir_validator("canonicalize_signature_type_ids", lambda: _canonicalize_signature_type_ids(signatures_by_id, shared_type_table)):
				if return_checked:
					if return_ssa:
						return {}, checked, None
					return {}, checked
				return {}
			if not _run_mir_validator("canonicalize_mir_type_ids", lambda: _canonicalize_mir_type_ids(mir_funcs_by_id, shared_type_table)):
				if return_checked:
					if return_ssa:
						return {}, checked, None
					return {}, checked
				return {}
		validator_plan: list[tuple[str, Callable[[], None]]] = [
			("validate_mir_call_invariants", lambda: validate_mir_call_invariants(mir_funcs_by_id)),
			("validate_mir_basic_hygiene", lambda: validate_mir_basic_hygiene(mir_funcs_by_id)),
		]
		if shared_type_table is not None:
			validator_plan.extend(
				[
					("validate_mir_call_types", lambda: validate_mir_call_types(mir_funcs_by_id, signatures_by_id, shared_type_table)),
					("validate_mir_concrete_layout_types", lambda: validate_mir_concrete_layout_types(mir_funcs_by_id, shared_type_table)),
					("validate_mir_variant_field_invariants", lambda: validate_mir_variant_field_invariants(mir_funcs_by_id, shared_type_table)),
				]
			)
		validator_plan.extend(
			[
				("validate_mir_array_alloc_invariants", lambda: validate_mir_array_alloc_invariants(mir_funcs_by_id)),
				("validate_mir_wrapping_u64_invariants", lambda: validate_mir_wrapping_u64_invariants(mir_funcs_by_id, shared_type_table)),
			]
		)
		if shared_type_table is not None:
			validator_plan.extend(
				[
					("validate_mir_iface_init_invariants", lambda: validate_mir_iface_init_invariants(mir_funcs_by_id, signatures_by_id, shared_type_table)),
					("validate_mir_array_copy_invariants", lambda: validate_mir_array_copy_invariants(mir_funcs_by_id, shared_type_table)),
					("validate_mir_call_byvalue_moves", lambda: validate_mir_call_byvalue_moves(mir_funcs_by_id, signatures_by_id, shared_type_table)),
				]
			)
		for validator_name, validator_action in validator_plan:
			if not _run_mir_validator(validator_name, validator_action):
				if return_checked:
					if return_ssa:
						return {}, checked, None
					return {}, checked
				return {}
	if shared_type_table is not None:
		if drift_debug.enabled("ssa"):
			import sys
			for fn_id, func in mir_funcs_by_id.items():
				if getattr(fn_id, "module", None) != "main":
					continue
				for block in func.blocks.values():
					for instr in block.instructions:
						if isinstance(instr, M.Call):
							print(f"[drift:debug][mir-pre-arc] call fn={instr.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
		with _timed("string_arc"):
			for fn_id, func in mir_funcs_by_id.items():
				mir_funcs_by_id[fn_id] = insert_string_arc(
					func,
					type_table=shared_type_table,
					fn_infos=checked.fn_infos_by_id,
				)
		unknown_ty = shared_type_table.ensure_unknown()
		for func in mir_funcs_by_id.values():
			func.local_types = dict(getattr(func, "local_types", {}) or {})
			for local_name in func.locals:
				if local_name not in func.local_types:
					func.local_types[local_name] = unknown_ty
			for block in func.blocks.values():
				for instr in block.instructions:
					if isinstance(instr, M.StoreLocal):
						cur = func.local_types.get(instr.local)
						if cur is None or cur == unknown_ty:
							val_ty = func.local_types.get(instr.value)
							if val_ty is not None and val_ty != unknown_ty:
								func.local_types[instr.local] = val_ty
								if drift_debug.enabled("local_types_trace") and instr.local == "done":
									td = shared_type_table.get(val_ty)
									print(f"[drift:debug][local_types_trace] fn={func.fn_id} pass=post_arc store_local={instr.local} ty={val_ty}:{td.kind.name}:{td.name}", file=sys.stderr)
	if drift_debug.enabled("ssa"):
		import sys
		for fn_id, func in mir_funcs_by_id.items():
			if getattr(fn_id, "module", None) != "main":
				continue
			for block in func.blocks.values():
				for instr in block.instructions:
					if isinstance(instr, M.Call):
						print(f"[drift:debug][mir] call fn={instr.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
	_assert_signature_map_split(
		base_signatures_by_id=base_signatures_by_id,
		derived_signatures_by_id=derived_signatures_by_id,
		context="compile_stubbed_funcs post-synthesis",
	)
	# Stage3: summaries
	code_to_exc = {code: name for name, code in (exc_env or {}).items()}
	summaries = ThrowSummaryBuilder().build(mir_funcs_by_id, code_to_exc=code_to_exc)

	# Optional SSA/type-env for typed throw checks
	ssa_funcs: Dict[FunctionId, MirToSSA.SsaFunc] | None = None
	type_env = checked.type_env
	if build_ssa:
		with _timed("ssa"):
			ssa_funcs = {fn_id: MirToSSA().run(func) for fn_id, func in mir_funcs_by_id.items()}
		if type_env is None:
			# First preference: checker-owned SSA typing using TypeIds + signatures.
			type_env = Checker(
				signatures_by_id={},
				hir_blocks_by_id={},
				call_info_by_callsite_id={},
				type_table=shared_type_table,
			).build_type_env_from_ssa_by_id(
				ssa_funcs,
				signatures_by_id,
				can_throw_by_id=declared_by_id,
				diagnostics=checked.diagnostics,
			)
			checked.type_env = type_env
		if type_env is None and signatures_by_id:
			# Fallback: minimal checker TypeEnv that tags return SSA values with the
			# signature return TypeId. This keeps type-aware checks usable even when
			# the fuller SSA typing could not derive any facts.
			type_env = build_minimal_checker_type_env(checked, ssa_funcs, signatures_by_id, table=checked.type_table)
			checked.type_env = type_env

	# Stage4: throw checks
	with _timed("throw_checks"):
		run_throw_checks(
			funcs=mir_funcs_by_id,
			summaries=summaries,
			declared_can_throw=declared_by_id,
			type_env=type_env or checked.type_env,
			fn_infos=checked.fn_infos_by_id,
			ssa_funcs=ssa_funcs,
			diagnostics=checked.diagnostics,
		)
	_assert_all_phased(checked.diagnostics, context="compile_stubbed_funcs")

	if return_checked and return_ssa:
		return mir_funcs_by_id, checked, ssa_funcs
	if return_checked:
		return mir_funcs_by_id, checked
	return mir_funcs_by_id


def compile_to_llvm_ir_for_tests(
	func_hirs: Mapping[FunctionId | str, H.HBlock],
	signatures: Mapping[FunctionId | str, FnSignature],
	exc_env: Mapping[str, int] | None = None,
	entry: str = "main",
	type_table: "TypeTable | None" = None,
	module_exports: Mapping[str, dict[str, object]] | None = None,
	module_deps: Mapping[str, set[str]] | None = None,
	origin_by_fn_id: Mapping[FunctionId, Path] | None = None,
	prelude_enabled: bool = True,
	emit_instantiation_index: Path | None = None,
	enforce_entrypoint: bool = False,
	reserved_namespace_policy: ReservedNamespacePolicy = ReservedNamespacePolicy.ALLOW_DEV,
	debug_enabled: bool = True,
) -> tuple[str, CheckedProgramById]:
	"""
	End-to-end helper: HIR -> MIR -> throw checks -> SSA -> LLVM IR for tests.

	This mirrors the stub driver pipeline and finishes by lowering SSA to LLVM IR.
	It is intentionally narrow: assumes a single Drift entry `drift_main` (or
	`entry`) returning `Int`, `String`, or `FnResult<Int, Error>` and uses the
	v1 ABI.
	Returns IR text and the CheckedProgramById so callers can assert diagnostics.
	"""
	func_hirs_by_id, signatures_by_id, fn_ids_by_name = _normalize_func_maps(func_hirs, signatures)
	shared_type_table = type_table or TypeTable()

	reserved = _reserved_module_ids(func_hirs_by_id, signatures_by_id)
	if reserved and reserved_namespace_policy is ReservedNamespacePolicy.ENFORCE:
		return (
			"",
			CheckedProgramById(
				fn_infos_by_id={},
				type_table=shared_type_table,
				exception_catalog=exc_env,
				diagnostics=_reserved_namespace_diags(reserved),
			),
		)

	# Ensure prelude signatures are present for tests that bypass the CLI.
	prelude_injected = _should_inject_prelude(prelude_enabled, module_deps)
	if prelude_injected:
		_inject_prelude(signatures_by_id, fn_ids_by_name, shared_type_table)

	# First, run the normal pipeline to get MIR + FnInfos + SSA (and diagnostics).
	if "::" in entry:
		entry_module, entry_name = entry.split("::", 1)
	else:
		entry_module, entry_name = "main", entry
	mir_funcs, checked, ssa_funcs = compile_stubbed_funcs(
		func_hirs=func_hirs_by_id,
		signatures=signatures_by_id,
		exc_env=exc_env,
		module_exports=module_exports,
		module_deps=module_deps,
		origin_by_fn_id=origin_by_fn_id,
		return_checked=True,
		build_ssa=True,
		return_ssa=True,
		type_table=shared_type_table,
		prelude_enabled=prelude_enabled,
		emit_instantiation_index=emit_instantiation_index,
		enforce_entrypoint=enforce_entrypoint,
		entry_module=entry_module,
		entry_name=entry_name,
		run_borrow_check=True,
	)
	_assert_all_phased(checked.diagnostics, context="compile_to_llvm_ir_for_tests")
	if any(d.severity == "error" for d in checked.diagnostics):
		return "", checked
	# Drop generic templates from codegen; only concrete instantiations are emitted.
	generic_templates = {
		fn_id
		for fn_id, sig in signatures_by_id.items()
		if getattr(sig, "type_params", None) or getattr(sig, "impl_type_params", None)
	}
	if checked.type_table is not None:
		for fn_id, info in checked.fn_infos_by_id.items():
			sig = info.signature
			if sig is None:
				continue
			param_ids = list(sig.param_type_ids or [])
			if sig.return_type_id is not None:
				param_ids.append(sig.return_type_id)
			if any(checked.type_table.has_typevar(tid) for tid in param_ids):
				generic_templates.add(fn_id)
	if generic_templates:
		mir_funcs = {fn_id: fn for fn_id, fn in mir_funcs.items() if fn_id not in generic_templates}
		if ssa_funcs is not None:
			ssa_funcs = {fn_id: fn for fn_id, fn in ssa_funcs.items() if fn_id not in generic_templates}

	# Lower module to LLVM IR and append the OS entry wrapper when needed.
	rename_map: dict[FunctionId, str] = {}
	argv_wrapper: str | None = None
	entry_ids = fn_ids_by_name.get(entry_name, [])
	entry_id = None
	for fn_id in entry_ids:
		if fn_id.module == entry_module:
			entry_id = fn_id
			break
	if entry_id is None and len(entry_ids) == 1:
		entry_id = entry_ids[0]
		entry_module = entry_id.module
	if entry_id is None and not entry_ids:
		name_matches = [fn_id for fn_id in signatures_by_id.keys() if fn_id.name == entry_name]
		if len(name_matches) == 1:
			entry_id = name_matches[0]
			entry_module = entry_id.module
	if entry_id is None:
		for fn_id in signatures_by_id.keys():
			if function_symbol(fn_id) == f"{entry_module}::{entry_name}":
				entry_id = fn_id
				break
	entry_info = checked.fn_infos_by_id.get(entry_id) if entry_id is not None else None
	# Detect main(argv: Array<String>) and emit a C-ABI wrapper that builds argv.
	if entry_info and entry_info.signature and entry_info.signature.param_type_ids and checked.type_table is not None:
		param_ty = entry_info.signature.param_type_ids[0]
		td = checked.type_table.get(param_ty)
		if len(entry_info.signature.param_type_ids) == 1 and td.kind.name == "ARRAY" and td.param_types:
			elem_td = checked.type_table.get(td.param_types[0])
			if elem_td.name == "String":
				# Guard: require return Int and exactly one param of Array<String>.
				if entry_info.signature.return_type_id != checked.type_table.ensure_int():
					raise ValueError("main(argv: Array<String>) must return Int")
				if entry_id is None:
					raise ValueError("main(argv: Array<String>) requires a main entry function")
				rename_map[entry_id] = "drift_main"
				argv_wrapper = "drift_main"

	# For main::main without argv, only force drift_main + OS wrapper when
	# entrypoint enforcement is requested (CLI/real-build path). Driver/unit
	# helper callers rely on historical behavior where plain main() stays as
	# user function unless argv-wrapper shape is required.
	if enforce_entrypoint and argv_wrapper is None and entry_id is not None and entry_id.module == "main" and entry_id.name == "main":
		rename_map[entry_id] = "drift_main"

	fn_infos = dict(checked.fn_infos_by_id)
	try:
		_validate_codegen_contract(
			mir_funcs,
			ssa_funcs,
			fn_infos,
			checked.type_table,
			debug_enabled=debug_enabled,
		)
	except AssertionError as err:
		_append_boundary_contract_diag(
			checked,
			phase="codegen",
			prefix="LLVM lowering contract failure",
			err=err,
			fn_id=entry_id,
			signatures_by_id=signatures_by_id,
			origin_by_fn_id=origin_by_fn_id,
		)
		_assert_all_phased(checked.diagnostics, context="compile_to_llvm_ir_for_tests")
		return "", checked

	try:
		module = lower_module_to_llvm(
			mir_funcs,
			ssa_funcs,
			fn_infos,
			type_table=checked.type_table,
			module_exports=module_exports,
			rename_map=rename_map,
			argv_wrapper=argv_wrapper,
			word_bits=host_word_bits(),
			debug_enabled=debug_enabled,
			provenance_git_sha=_git_short_sha(),
		)
	except AssertionError as err:
		_append_boundary_contract_diag(
			checked,
			phase="codegen",
			prefix="LLVM lowering contract failure",
			err=err,
			fn_id=entry_id,
			signatures_by_id=signatures_by_id,
			origin_by_fn_id=origin_by_fn_id,
		)
		_assert_all_phased(checked.diagnostics, context="compile_to_llvm_ir_for_tests")
		return "", checked
	if enforce_entrypoint or argv_wrapper is not None:
		module.emit_abi_stamp()
	install_process_preamble_available = any(
		fn_id.module == "std.io" and fn_id.name == "install_process_preamble"
		for fn_id in fn_infos.keys()
	)
	# Emit OS wrapper for:
	# - explicit entrypoint-enforced path (CLI/real-build), or
	# - driver/unit-test paths that pre-name their entry "drift_main"
	#   (historical convention: they need a runnable binary).
	# Skip for helper codegen paths where entry is the raw "main" symbol
	# and enforce_entrypoint is not set.
	if argv_wrapper is None and entry_id is not None and (enforce_entrypoint or entry_name == "drift_main"):
		entry_sym = rename_map.get(entry_id, function_symbol(entry_id) if entry_id is not None else f"{entry_module}::{entry_name}")
		module.emit_entry_wrapper(entry_sym, install_process_preamble=install_process_preamble_available)
	return module.render(), checked


def _fake_decls_from_hirs(hirs: Mapping[FunctionId, H.HBlock]) -> list[object]:
	"""
	Shim: build decl-like objects from HIR blocks so the type resolver can
	construct FnSignatures when real decls are not available.

	This exists only for the stub pipeline; a real front end will provide
	declarations with parsed types and throws clauses.
	"""
	def _scan_returns(block: H.HBlock) -> tuple[bool, bool]:
		"""Return (saw_value_return, saw_void_return)."""
		saw_val = False
		saw_void = False
		for stmt in block.statements:
			if isinstance(stmt, H.HReturn):
				if getattr(stmt, "value", None) is None:
					saw_void = True
				else:
					saw_val = True
			elif isinstance(stmt, H.HIf):
				t_val, t_void = _scan_returns(stmt.then_block)
				s_val = False
				s_void = False
				if stmt.else_block:
					s_val, s_void = _scan_returns(stmt.else_block)
				saw_val = saw_val or t_val or s_val
				saw_void = saw_void or t_void or s_void
			elif isinstance(stmt, H.HLoop):
				b_val, b_void = _scan_returns(stmt.body)
				saw_val = saw_val or b_val
				saw_void = saw_void or b_void
			elif isinstance(stmt, H.HTry):
				b_val, b_void = _scan_returns(stmt.body)
				saw_val = saw_val or b_val
				saw_void = saw_void or b_void
				for arm in stmt.catches:
					a_val, a_void = _scan_returns(arm.block)
					saw_val = saw_val or a_val
					saw_void = saw_void or a_void
		return saw_val, saw_void

	decls: list[FakeDecl] = []
	for fn_id, block in hirs.items():
		ret_ty = "Int"
		if isinstance(block, H.HBlock):
			val_ret, void_ret = _scan_returns(block)
			if void_ret and not val_ret:
				ret_ty = "Void"
		decls.append(FakeDecl(fn_id=fn_id, name=fn_id.name, params=[], return_type=ret_ty))
	return decls



__all__ = ["compile_stubbed_funcs", "compile_to_llvm_ir_for_tests"]


def _diag_to_json(diag: Diagnostic, phase: str, source: Path) -> dict:
	"""Render a Diagnostic to a structured JSON-friendly dict."""
	line = getattr(diag.span, "line", None) if diag.span is not None else None
	column = getattr(diag.span, "column", None) if diag.span is not None else None
	file = None
	if diag.span is not None:
		file = getattr(diag.span, "file", None)
	if file is None:
		file = "<source>"
	phase = getattr(diag, "phase", None) or phase
	notes = list(getattr(diag, "notes", []) or [])
	return {
		"phase": phase,
		"message": diag.message,
		"code": getattr(diag, "code", None),
		"severity": diag.severity,
		"file": file,
		"line": line,
		"column": column,
		"notes": notes,
	}


def _source_label() -> str:
	return "<source>"


def _package_label() -> str:
	return "<package>"


def _abi_fingerprint(target: str, *, word_bits: int) -> dict[str, object]:
	inline_bytes = (word_bits // 8) * 4
	return {
		"drift_abi_version": 1,
		"target": target,
		"word_bits": int(word_bits),
		"iface_inline_bytes": int(inline_bytes),
		"iface_inline_align": int(word_bits // 8),
		"iface_layout": "iface-v1-sbo",
		"fnresult_layout": "fnresult-v1",
		"call_conv": "drift-v1",
	}


def _trust_label() -> str:
	return "<trust-store>"



def main(argv: list[str] | None = None) -> int:
	"""
	Minimal CLI: parses a Drift file, type checks, then borrow checks. If any stage
	emits errors, compilation fails.

	With --json, prints structured diagnostics (phase/message/severity/file/line/column)
	and an exit_code; otherwise prints human-readable messages to stderr.
	"""
	# Handle --version before argparse so it works without required positional args.
	raw_argv = argv if argv is not None else sys.argv[1:]
	if "--version" in raw_argv or "-V" in raw_argv:
		print(_version_string())
		return 0
	parser = argparse.ArgumentParser(description="lang driftc stub")
	parser.add_argument("source", type=Path, nargs="+", help="Path(s) to Drift source file(s)")
	parser.add_argument(
		"-M",
		"--module-path",
		dest="module_paths",
		action="append",
		type=Path,
		help="Module root directory (repeatable); used to discover source files (module id always comes from module declaration)",
	)
	parser.add_argument(
		"--package-root",
		dest="package_roots",
		action="append",
		type=Path,
		help="Package root directory (repeatable); used to satisfy imports from local package artifacts",
	)
	parser.add_argument(
		"--trust-store",
		type=Path,
		help="Path to project trust store JSON (default: ./drift/trust.json)",
	)
	parser.add_argument(
		"--no-user-trust-store",
		action="store_true",
		help="Disable user-level trust store fallback (~/.config/drift/trust.json)",
	)
	parser.add_argument(
		"--allow-unsigned-from",
		dest="allow_unsigned_from",
		action="append",
		type=Path,
		help="Allow unsigned packages from this directory (repeatable)",
	)
	parser.add_argument(
		"--allow-unsafe",
		action="store_true",
		help="Allow unsafe functions and unsafe blocks (required for unsafe code outside toolchain-trusted modules)",
	)
	parser.add_argument(
		"--dev",
		action="store_true",
		help="Enable dev-only switches (non-normative, for local testing)",
	)
	parser.add_argument(
		"--dev-core-trust-store",
		type=Path,
		help="Dev-only override for the core trust store JSON (requires --dev)",
	)
	parser.add_argument(
		"--require-signatures",
		action="store_true",
		help="Require signatures for all packages (including local build outputs)",
	)
	parser.add_argument("-o", "--output", type=Path, help="Path to output executable")
	parser.add_argument("--emit-ir", type=Path, help="Write LLVM IR to the given path")
	parser.add_argument(
		"--emit-instantiation-index",
		type=Path,
		help="Write instantiation index JSON to the given path",
	)
	parser.add_argument("--emit-package", type=Path, help="Write an unsigned package artifact (.dmp) to the given path")
	parser.add_argument("--package-id", type=str, help="Package identity (required with --emit-package)")
	parser.add_argument("--package-version", type=str, help="Package version (SemVer; required with --emit-package)")
	parser.add_argument("--package-target", type=str, help="Target triple (required with --emit-package)")
	parser.add_argument("-g", "--debug-info", action="store_true", help="Emit debug info in generated LLVM (DWARF)")
	parser.add_argument("--no-debug-info", action="store_true", help="Disable debug info emission")
	parser.add_argument("--optimized", action="store_true", help="Compile with -O2 optimization (selects optimized runtime archive)")
	parser.add_argument("--linker", choices=["ld", "gold"], default=None, help="Select linker (default: prefer gold if available)")
	parser.add_argument(
		"--target-word-bits",
		type=int,
		help="Target pointer width in bits (required for codegen; e.g. 32 or 64)",
	)
	parser.add_argument("--package-build-epoch", type=str, default=None, help="Optional build epoch label (non-semantic)")
	parser.add_argument(
		"--json",
		action="store_true",
		help="Emit diagnostics as JSON (phase/message/severity/file/line/column)",
	)
	parser.add_argument(
		"--prelude",
		dest="prelude",
		action="store_true",
		default=True,
		help="Legacy no-op flag (value prelude removed; kept for compatibility)",
	)
	parser.add_argument(
		"--no-prelude",
		dest="prelude",
		action="store_false",
		help="Legacy no-op flag (value prelude removed; kept for compatibility)",
	)
	parser.add_argument(
		"--stdlib-root",
		type=Path,
		help="Path to stdlib root (optional); when set, stdlib sources are loaded",
	)
	parser.add_argument(
		"--entry",
		type=str,
		help="Entry point symbol (module::fn or fn; default main::main)",
	)
	parser.add_argument(
		"--test-build-only",
		action="store_true",
		help="Enable @test_build_only declarations (tests only)",
	)
	args = parser.parse_args(argv)
	optimized = getattr(args, "optimized", False)
	debug_enabled = not optimized
	if args.no_debug_info:
		debug_enabled = False
	if args.debug_info:
		debug_enabled = True
	if args.stdlib_root is None:
		from lang.driftc.parser import stdlib_root as _stdlib_root
		args.stdlib_root = _stdlib_root()
	def _normalize_cli_path(path: Path | None) -> Path | None:
		if path is None:
			return None
		if path.is_absolute():
			return path
		return (Path.cwd() / path).resolve()
	args.output = _normalize_cli_path(args.output)
	args.emit_ir = _normalize_cli_path(args.emit_ir)
	args.emit_package = _normalize_cli_path(args.emit_package)
	args.emit_instantiation_index = _normalize_cli_path(args.emit_instantiation_index)
	def _parse_entry_spec(spec: str | None) -> tuple[str, str]:
		if not spec:
			return ("main", "main")
		if "::" in spec:
			mod, fn = spec.split("::", 1)
			if not mod or not fn:
				raise ValueError(f"invalid --entry '{spec}' (expected module::fn)")
			return (mod, fn)
		return ("main", spec)

	entry_module, entry_name = _parse_entry_spec(args.entry)
	if args.target_word_bits is None and _TEST_TARGET_WORD_BITS is not None:
		args.target_word_bits = _TEST_TARGET_WORD_BITS

	source_paths: list[Path] = list(args.source)
	source_path = source_paths[0]
	# Treat the input set as a workspace, even for a single file, so import
	# resolution behavior is consistent across the CLI and the e2e harness:
	# if user code imports a missing module, we fail early with a parser-phase
	# diagnostic instead of silently compiling a single file in isolation.
	module_paths = list(args.module_paths or []) or None
	loaded_pkgs = []
	external_exports = None
	if args.package_roots:
		# Load trust store(s) for package signature verification.
		#
		# Pinned policy:
		# - project-local trust store is primary: ./drift/trust.json (or --trust-store)
		# - user-level trust store is an optional convenience layer
		# - `driftc` is the final gatekeeper: verification happens at use time
		project_trust_path = args.trust_store or (Path.cwd() / "drift" / "trust.json")
		project_trust = TrustStore(keys_by_kid={}, allowed_kids_by_namespace={}, revoked_kids=set())
		if project_trust_path.exists():
			project_trust = load_trust_store_json(project_trust_path)
		elif args.trust_store is not None:
			# Explicit trust store path is required to exist.
			msg = f"trust store not found: {_trust_label()}"
			if args.json:
				print(
					json.dumps(
						{
							"exit_code": 1,
							"diagnostics": [
								{
									"phase": "package",
									"message": msg,
									"severity": "error",
									"file": "<trust-store>",
									"line": None,
									"column": None,
								}
							],
						}
					)
				)
			else:
				print(f"{_trust_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1

		if args.dev_core_trust_store is not None and not args.dev:
			msg = "--dev-core-trust-store requires --dev"
			if args.json:
				print(
					json.dumps(
						{
							"exit_code": 1,
							"diagnostics": [
								{
									"phase": "package",
									"message": msg,
									"severity": "error",
									"file": None,
									"line": None,
									"column": None,
								}
							],
						}
					)
				)
			else:
				print(f"error: {msg}", file=sys.stderr)
			return 1

		merged_trust = project_trust
		try:
			if args.dev_core_trust_store is not None:
				core_trust = load_trust_store_json(args.dev_core_trust_store)
			else:
				core_trust = load_core_trust_store()
		except ValueError as err:
			msg = str(err)
			if args.json:
				print(
					json.dumps(
						{
							"exit_code": 1,
							"diagnostics": [
								{
									"phase": "package",
									"message": msg,
									"severity": "error",
									"file": None,
									"line": None,
									"column": None,
								}
							],
						}
					)
				)
			else:
				print(f"error: {msg}", file=sys.stderr)
			return 1
		if not args.no_user_trust_store:
			user_path = Path.home() / ".config" / "drift" / "trust.json"
			if user_path.exists():
				user_trust = load_trust_store_json(user_path)
				merged_trust = merge_trust_stores(project_trust, user_trust)

		allow_unsigned_roots: list[Path] = []
		# Default local unsigned outputs directory (pinned).
		allow_unsigned_roots.append((Path.cwd() / "build" / "drift" / "localpkgs").resolve())
		for p in list(args.allow_unsigned_from or []):
			allow_unsigned_roots.append(p.resolve())

		policy = PackageTrustPolicy(
			trust_store=merged_trust,
			core_trust_store=core_trust,
			require_signatures=bool(args.require_signatures),
			allow_unsigned_roots=allow_unsigned_roots,
		)

		package_files = discover_package_files(list(args.package_roots))
		for pkg_path in package_files:
			# Integrity + trust verification happens here, before any package
			# metadata is used for import resolution.
			try:
				loaded_pkgs.append(load_package_v0_with_policy(pkg_path, policy=policy))
			except ValueError as err:
				msg = str(err)
				if args.json:
					print(
						json.dumps(
							{
								"exit_code": 1,
								"diagnostics": [
									{
										"phase": "package",
										"message": msg,
										"severity": "error",
										"file": "<package>",
										"line": None,
										"column": None,
									}
								],
							}
						)
					)
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1

		# Determinism: package discovery order (filenames, rglob ordering, CLI
		# `--package-root` ordering) must not affect compilation results. Sort loaded
		# packages by the module ids they provide, which is a content-derived key and
		# independent of filesystem paths.
		loaded_pkgs.sort(key=lambda p: tuple(sorted(p.modules_by_id.keys())))

		# Enforce "single version per package id per build".
		pkg_id_map: dict[str, tuple[str, str, str, Path]] = {}  # package_id -> (version, target, sha256, path)
		for pkg in loaded_pkgs:
			man = pkg.manifest
			pkg_id = man.get("package_id")
			pkg_ver = man.get("package_version")
			pkg_target = man.get("target")
			if not isinstance(pkg_id, str) or not isinstance(pkg_ver, str) or not isinstance(pkg_target, str):
				msg = f"package {_package_label()} missing package identity fields"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			pkg_sha = sha256_hex(pkg.path.read_bytes())
			prev = pkg_id_map.get(pkg_id)
			if prev is None:
				pkg_id_map[pkg_id] = (pkg_ver, pkg_target, pkg_sha, pkg.path)
				continue
			prev_ver, prev_target, prev_sha, prev_path = prev
			if pkg_ver != prev_ver or pkg_target != prev_target:
				msg = (
					f"multiple versions/targets for package id '{pkg_id}' in build: "
					f"'{prev_ver}' ({prev_target}) and '{pkg_ver}' ({pkg_target}) across distinct package artifacts"
				)
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			if pkg_sha != prev_sha and pkg.path != prev_path:
				msg = f"duplicate package id '{pkg_id}' in build from different artifacts"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1

		abi_expected: dict[str, object] | None = None
		for pkg in loaded_pkgs:
			abi = pkg.manifest.get("abi_fingerprint")
			if not isinstance(abi, dict):
				msg = f"package '{pkg.manifest.get('package_id')}' missing abi_fingerprint"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			if abi_expected is None:
				abi_expected = abi
				continue
			if abi != abi_expected:
				msg = "ABI fingerprint mismatch across packages in build"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
		if abi_expected is not None:
			target = args.package_target if args.package_target is not None else abi_expected.get("target")
			if not isinstance(target, str):
				msg = "ABI fingerprint missing target"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			local_abi = _abi_fingerprint(str(target), word_bits=host_word_bits())
			if local_abi != abi_expected:
				msg = "ABI fingerprint mismatch between toolchain target and loaded packages"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1

		# Reject duplicate module ids across package files early. Unioning exports
		# is unsafe because it can mask collisions and make resolution nondeterministic.
		mod_to_pkg: dict[str, Path] = {}
		for pkg in loaded_pkgs:
			for mid in pkg.modules_by_id.keys():
				prev = mod_to_pkg.get(mid)
				if prev is None:
					mod_to_pkg[mid] = pkg.path
				elif prev != pkg.path:
					msg = f"module '{mid}' provided by multiple packages"
					if args.json:
						print(
							json.dumps(
								{
									"exit_code": 1,
									"diagnostics": [
										{
											"phase": "package",
											"message": msg,
											"severity": "error",
											"file": "<source>",
											"line": None,
											"column": None,
										}
									],
								}
							)
						)
					else:
						print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1
		if args.output or args.emit_ir:
			dep_main = _find_dependency_main(loaded_pkgs)
			if dep_main is not None:
				pkg_id, pkg_path, _sym_name = dep_main
				msg = f"illegal entrypoint 'main' in dependency package {pkg_id}; entrypoints are only allowed in the root package"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
		external_exports = collect_external_exports(loaded_pkgs)

	if external_exports is None:
		external_exports = {}
	if "lang.core" not in external_exports:
		external_exports["lang.core"] = _prelude_exports()

	external_module_packages: dict[str, str] = {}
	if loaded_pkgs:
		for pkg in loaded_pkgs:
			pkg_id = getattr(pkg, "manifest", {}).get("package_id")
			if not isinstance(pkg_id, str) or not pkg_id:
				continue
			for mid in getattr(pkg, "modules_by_id", {}).keys():
				if isinstance(mid, str):
					external_module_packages.setdefault(mid, pkg_id)

	package_id = str(args.package_id) if args.package_id else None
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		source_paths,
		module_paths=module_paths,
		external_module_exports=external_exports,
		external_module_packages=external_module_packages,
		package_id=package_id,
		stdlib_root=args.stdlib_root,
		test_build_only=bool(getattr(args, "test_build_only", False)),
		word_bits=getattr(args, "target_word_bits", None),
	)
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	origin_by_fn_id: dict[FunctionId, Path] = {}
	for mod in modules.values():
		for fn_id, src_path in mod.origin_by_fn_id.items():
			origin_by_fn_id[fn_id] = src_path
	prelude_injected = _should_inject_prelude(bool(args.prelude), module_deps)
	if prelude_injected:
		_inject_prelude(signatures, fn_ids_by_name, type_table)
	func_hirs_by_id = func_hirs
	base_signatures_by_id = MappingProxyType(dict(signatures))
	derived_signatures_by_id: dict[FunctionId, FnSignature] = {}
	signatures_by_id: Mapping[FunctionId, FnSignature] = ChainMap(
		derived_signatures_by_id,
		base_signatures_by_id,
	)
	external_signatures_by_name: dict[str, FnSignature] = {}
	external_signatures_by_symbol: dict[str, FnSignature] = {}
	external_signatures_by_id: dict[FunctionId, FnSignature] = {}
	external_trait_defs: list[object] = []
	external_impl_metas: list[object] = []
	external_missing_traits: set[object] = set()
	external_missing_impl_modules: set[str] = set()
	external_template_hirs_by_key: dict[FunctionKey, H.HBlock] = {}
	external_template_requires_by_key: dict[FunctionKey, object] = {}
	external_template_keys_by_fn_id: dict[FunctionId, FunctionKey] = {}
	external_template_layout_by_key: dict[FunctionKey, list[dict[str, object]]] = {}
	id_registry = IdRegistry()

	if parse_diags:
		_assert_all_phased(parse_diags, context="parser")
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "parser", source_path) for d in parse_diags],
			}
			print(json.dumps(payload))
		else:
			for d in parse_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
		return 1

	_required_modules_main: set[str] = {m for m in modules.keys() if isinstance(m, str)}
	if module_deps:
		_required_modules_main.update({m for m in module_deps.keys() if isinstance(m, str)})
		for deps in module_deps.values():
			_required_modules_main.update({m for m in deps if isinstance(m, str)})
	_ensure_module_packages(
		type_table,
		modules=_required_modules_main,
		package_id=package_id,
		allow_fill=False,
		context="driftc",
	)

	def _is_toolchain_stdlib_module(mid: str) -> bool:
		if args.stdlib_root is None:
			return False
		mod = modules.get(mid)
		if mod is None:
			return False
		try:
			mod.source_path.resolve().relative_to(args.stdlib_root.resolve())
		except ValueError:
			return False
		return True

	if not args.dev:
		reserved = [
			mid
			for mid in modules.keys()
			if mid.startswith(("std.", "lang.", "drift.")) and not _is_toolchain_stdlib_module(mid)
		]
		if reserved:
			diags = _reserved_namespace_diags(reserved)
			_assert_all_phased(diags, context="package")
			if args.json:
				print(json.dumps({"exit_code": 1, "diagnostics": [_diag_to_json(d, "package", source_path) for d in diags]}))
			else:
				for d in diags:
					print(f"{_source_label()}:?:?: {d.severity}: {d.message}", file=sys.stderr)
			return 1

	method_wrapper_specs: list[MethodWrapperSpec] = []
	wrapper_errors: list[str] = []

	def _register_derived_signature_cli(fn_id: FunctionId, sig: FnSignature) -> None:
		existing = derived_signatures_by_id.get(fn_id) or base_signatures_by_id.get(fn_id)
		if existing is not None:
			if existing != sig:
				raise AssertionError(f"signature collision for '{function_symbol(fn_id)}'")
			return
		derived_signatures_by_id[fn_id] = sig

	method_wrapper_specs, wrapper_errors = _inject_method_boundary_wrappers(
		signatures_by_id=base_signatures_by_id,
		existing_ids=set(base_signatures_by_id.keys()) | set(derived_signatures_by_id.keys()),
		register_derived=_register_derived_signature_cli,
		type_table=type_table,
	)
	_assert_signature_map_split(
		base_signatures_by_id=base_signatures_by_id,
		derived_signatures_by_id=derived_signatures_by_id,
		context="driftc CLI pre-typecheck",
	)
	if wrapper_errors:
		for msg in wrapper_errors:
			if args.json:
				print(
					json.dumps(
						{
							"exit_code": 1,
							"diagnostics": [
								{"phase": "package", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}
							],
						}
					)
				)
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1

	# Prime builtins so TypeTable IDs are stable for package compatibility checks.
	# This must be done before comparing against package payload fingerprints.
	type_table.ensure_int()
	type_table.ensure_uint()
	type_table.ensure_bool()
	type_table.ensure_float()
	type_table.ensure_string()
	type_table.ensure_void()
	type_table.ensure_error()
	type_table.ensure_diagnostic_value()
	# Keep derived Optional<T> ids stable across builds (package embedding).
	opt_base = type_table.ensure_optional_base()
	type_table.ensure_instantiated(opt_base, [type_table.ensure_int()])
	type_table.ensure_instantiated(opt_base, [type_table.ensure_bool()])
	type_table.ensure_instantiated(opt_base, [type_table.ensure_string()])

	# Verify package TypeTable compatibility before importing signatures/IR.
	# Build link-time TypeId maps for packages and import their type definitions
	# into the host TypeTable. This allows package consumption without requiring
	# identical TypeId assignment across independently-produced artifacts.
	pkg_typeid_maps: dict[Path, dict[int, int]] = {}
	if loaded_pkgs:
		pkg_paths: list[Path] = []
		pkg_tt_objs: list[dict[str, Any]] = []
		for pkg in loaded_pkgs:
			# MVP rule: all modules in a package must share the same encoded type table.
			pkg_tt_obj: dict[str, Any] | None = None
			for mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				tt = payload.get("type_table")
				if not isinstance(tt, dict):
					msg = f"package {_package_label()} module '{mid}' is missing type_table"
					if args.json:
						print(
							json.dumps(
								{
									"exit_code": 1,
									"diagnostics": [
										{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}
									],
								}
							)
						)
					else:
						print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1
				if pkg_tt_obj is None:
					pkg_tt_obj = tt
				else:
					if type_table_fingerprint(tt) != type_table_fingerprint(pkg_tt_obj):
						msg = f"package {_package_label()} contains inconsistent type_table across modules"
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}
										],
									}
								)
							)
						else:
							print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
			if pkg_tt_obj is None:
				continue
			pkg_paths.append(pkg.path)
			pkg_tt_objs.append(pkg_tt_obj)

		try:
			maps = import_type_tables_and_build_typeid_maps(pkg_tt_objs, type_table)
		except ValueError as err:
			msg = f"failed to import package types: {err}"
			if args.json:
				print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1
		for path, tid_map in zip(pkg_paths, maps):
			pkg_typeid_maps[path] = tid_map
		for base_id, schema in getattr(type_table, "struct_bases", {}).items():
			mod = getattr(schema, "module_id", None)
			name = getattr(schema, "name", None)
			if isinstance(mod, str) and isinstance(name, str):
				pkg = getattr(type_table, "module_packages", {}).get(mod, getattr(type_table, "package_id", None))
				id_registry.intern_type(TypeKey(package_id=pkg, module=mod, name=name, args=()), preferred=base_id)
		for base_id, schema in getattr(type_table, "variant_schemas", {}).items():
			mod = getattr(schema, "module_id", None)
			name = getattr(schema, "name", None)
			if isinstance(mod, str) and isinstance(name, str):
				pkg = getattr(type_table, "module_packages", {}).get(mod, getattr(type_table, "package_id", None))
				id_registry.intern_type(TypeKey(package_id=pkg, module=mod, name=name, args=()), preferred=base_id)

	# If package roots were provided, merge package signatures into the signature
	# environment so type checking can validate calls to imported functions.
	if loaded_pkgs:
		local_display_names = set(fn_ids_by_name.keys())
		for pkg in loaded_pkgs:
			for _mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				sigs_obj = payload.get("signatures")
				if not isinstance(sigs_obj, dict):
					continue
				tid_map = pkg_typeid_maps.get(pkg.path, {})
				for sym, sd in sigs_obj.items():
					if not isinstance(sd, dict):
						continue
					name = str(sd.get("name") or sym)
					if "__impl" in name:
						msg = f"package signature references private symbol {name}; packages must expose only public entrypoints"
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{
												"phase": "package",
												"message": msg,
												"severity": "error",
												"file": "<source>",
												"line": None,
												"column": None,
											}
										],
									}
								)
							)
						else:
							print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
					if name in local_display_names or name in external_signatures_by_name:
						continue
					module_name = sd.get("module")
					if module_name is None:
						msg = f"package signature '{name}' missing module; signatures must include module"
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{
												"phase": "package",
												"message": msg,
												"severity": "error",
												"file": "<source>",
												"line": None,
												"column": None,
											}
										],
									}
								)
							)
						else:
							print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
					if module_name is not None and "::" not in name:
						name = f"{module_name}::{name}"
					if "is_pub" not in sd:
						msg = f"package signature '{name}' missing is_pub; signatures must include is_pub"
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{
												"phase": "package",
												"message": msg,
												"severity": "error",
												"file": "<source>",
												"line": None,
												"column": None,
											}
										],
									}
								)
							)
						else:
							print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
					param_type_ids = sd.get("param_type_ids")
					if isinstance(param_type_ids, list):
						param_type_ids = [tid_map.get(int(x), int(x)) for x in param_type_ids]
					ret_tid = sd.get("return_type_id")
					if isinstance(ret_tid, int):
						ret_tid = tid_map.get(ret_tid, ret_tid)
					impl_tid = sd.get("impl_target_type_id")
					if isinstance(impl_tid, int):
						impl_tid = tid_map.get(impl_tid, impl_tid)
					wraps_fn_id = function_id_from_obj(sd.get("wraps_target_fn_id"))
					if bool(sd.get("is_wrapper", False)) and wraps_fn_id is None:
						msg = f"package signature '{name}' is marked wrapper but missing wraps_target_fn_id"
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{
												"phase": "package",
												"message": msg,
												"severity": "error",
												"file": "<source>",
												"line": None,
												"column": None,
											}
										],
									}
								)
							)
						else:
							print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1

					symbol = str(sym)
					fn_id = function_id_from_obj(sd.get("fn_id"))
					if fn_id is None:
						msg = f"package signature '{name}' missing fn_id; signatures must include fn_id"
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{
												"phase": "package",
												"message": msg,
												"severity": "error",
												"file": "<source>",
												"line": None,
												"column": None,
											}
										],
									}
								)
							)
						else:
							print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
					if module_name is not None and fn_id.module != module_name:
						msg = f"package signature '{name}' fn_id module mismatch ({fn_id.module} vs {module_name})"
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{
												"phase": "package",
												"message": msg,
												"severity": "error",
												"file": "<source>",
												"line": None,
												"column": None,
											}
										],
									}
								)
							)
						else:
							print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
					type_param_names = sd.get("type_params")
					if not isinstance(type_param_names, list):
						type_param_names = []
					impl_type_param_names = sd.get("impl_type_params")
					if not isinstance(impl_type_param_names, list):
						impl_type_param_names = []
					type_params: list[TypeParam] = []
					for idx, tp_name in enumerate(type_param_names):
						if isinstance(tp_name, str):
							type_params.append(TypeParam(id=TypeParamId(fn_id, idx), name=tp_name))
					impl_owner = FunctionId(module="lang.__external", name=f"__impl_{symbol}", ordinal=0)
					impl_type_params: list[TypeParam] = []
					for idx, tp_name in enumerate(impl_type_param_names):
						if isinstance(tp_name, str):
							impl_type_params.append(TypeParam(id=TypeParamId(impl_owner, idx), name=tp_name))
					type_param_map = {p.name: p.id for p in (impl_type_params + type_params)}
					impl_target_type_args: list[TypeId] | None = None
					if impl_type_params:
						impl_target_type_args = [
							type_table.ensure_typevar(tp.id, name=tp.name) for tp in impl_type_params
						]
					param_mutable_raw = sd.get("param_mutable")
					param_mutable = None
					if isinstance(param_mutable_raw, list):
						param_mutable = [bool(x) for x in param_mutable_raw]

					param_types_raw = sd.get("param_types")
					param_types: list[object] | None = None
					if isinstance(param_types_raw, list):
						decoded: list[object] = []
						ok = True
						for entry in param_types_raw:
							te = decode_type_expr(entry)
							if te is None:
								ok = False
								break
							decoded.append(te)
						if ok:
							param_types = decoded
							param_type_ids = [
								resolve_opaque_type(t, type_table, module_id=module_name, type_params=type_param_map)
								for t in decoded
							]

					return_type = None
					return_raw = sd.get("return_type")
					if return_raw is not None:
						return_type = decode_type_expr(return_raw)
						if return_type is not None:
							ret_tid = resolve_opaque_type(return_type, type_table, module_id=module_name, type_params=type_param_map)
					intrinsic_kind = None
					intrinsic_kind_raw = sd.get("intrinsic_kind")
					if isinstance(intrinsic_kind_raw, str):
						try:
							intrinsic_kind = IntrinsicKind(intrinsic_kind_raw)
						except ValueError:
							intrinsic_kind = None
					is_intrinsic = bool(sd.get("is_intrinsic", False)) or intrinsic_kind is not None

					sig = FnSignature(
						name=name,
						module=module_name,
						method_name=sd.get("method_name"),
						param_names=sd.get("param_names"),
						param_mutable=param_mutable,
						param_type_ids=param_type_ids,
						return_type_id=ret_tid,
						declared_can_throw=sd.get("declared_can_throw"),
						is_intrinsic=is_intrinsic,
						intrinsic_kind=intrinsic_kind,
						is_method=bool(sd.get("is_method", False)),
						self_mode=sd.get("self_mode"),
						impl_target_type_id=impl_tid,
						is_pub=bool(sd.get("is_pub")),
						is_wrapper=bool(sd.get("is_wrapper", False)),
						wraps_target_fn_id=wraps_fn_id,
						is_exported_entrypoint=bool(sd.get("is_exported_entrypoint", False)),
						param_types=param_types,
						return_type=return_type,
						type_params=type_params,
						impl_type_params=impl_type_params,
						impl_target_type_args=impl_target_type_args,
					)
					if module_name is not None and module_name in modules:
						continue
					external_signatures_by_name[name] = sig
					if symbol not in external_signatures_by_symbol:
						external_signatures_by_symbol[symbol] = sig
					if fn_id not in external_signatures_by_id:
						external_signatures_by_id[fn_id] = sig

		(
			external_trait_defs,
			external_impl_metas,
			external_missing_traits,
			external_missing_impl_modules,
		) = _collect_external_trait_and_impl_metadata(
			loaded_pkgs=loaded_pkgs,
			type_table=type_table,
			external_signatures_by_id=external_signatures_by_id,
			id_registry=id_registry,
		)

		for pkg in loaded_pkgs:
			pkg_id = pkg.manifest.get("package_id")
			if not isinstance(pkg_id, str) or not pkg_id:
				pkg_id = "<unknown>"
			for mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				templates_obj = payload.get("generic_templates")
				templates = decode_generic_templates(templates_obj)
				# Template import failure classification:
				#
				# HARD ERRORS (return _template_import_error):
				#   Structural corruption — the package is malformed and cannot be
				#   trusted.  Any of these aborts compilation.
				#   - non-dict entry, v0 format, invalid template_id, pkg_id mismatch,
				#     missing fn_id, fn_id/template_id mismatch, invalid ordinal,
				#     missing signature dict, type_params not a list, layout mismatch,
				#     layout conflict, missing HIR body, fn_id None after validation,
				#     duplicate template mapping
				#
				# SOFT SKIPS (continue, with optional note):
				#   Recoverable mismatch — the template cannot be instantiated in this
				#   compilation but the package is not corrupt.
				#   - unknown ir_kind (note), filtered signature, incomplete TypeExprs,
				#     fingerprint mismatch (note), id intern conflict
				for entry in templates:
					if not isinstance(entry, dict):
						msg = f"generic_templates entry is not a dict in package {pkg_id}"
						if args.json:
							print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
						else:
							print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
					ir_kind = entry.get("ir_kind")
					if ir_kind not in ("TemplateHIR-v1", "TemplateHIR-v0"):
						print(
							f"{_package_label()}:?:?: note: unknown template ir_kind "
							f"'{ir_kind}' in package {pkg_id}; entry skipped",
							file=sys.stderr,
						)
						continue
					fn_id: FunctionId | None = None

					def _template_import_error(msg: str) -> int:
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{
												"phase": "package",
												"message": msg,
												"severity": "error",
												"file": "<package>",
												"line": None,
												"column": None,
											}
										],
									}
								)
							)
						else:
							print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1

					if ir_kind == "TemplateHIR-v0":
						return _template_import_error(
							f"TemplateHIR-v0 templates are not supported; rebuild package {pkg_id}"
						)
					template_id = entry.get("template_id")
					fn_key = function_key_from_obj(template_id)
					if fn_key is None:
						return _template_import_error(
							f"invalid TemplateHIR-v1 template_id in package {pkg_id}"
						)
					if fn_key.package_id != pkg_id:
						return _template_import_error(
							f"TemplateHIR entry package id mismatch ({fn_key.package_id} vs {pkg_id})"
						)
					req = entry.get("require")
					if req is not None:
						external_template_requires_by_key[fn_key] = req
					if ir_kind == "TemplateHIR-v1":
						fn_id = function_id_from_obj(entry.get("fn_id"))
						if fn_id is None:
							return _template_import_error(
								f"TemplateHIR-v1 entry missing fn_id in package {pkg_id}"
							)
						if fn_id.module != fn_key.module_path or fn_id.name != fn_key.name:
							return _template_import_error(
								f"TemplateHIR-v1 fn_id does not match template_id in package {pkg_id}"
							)
						if fn_id.ordinal < 0:
							return _template_import_error(
								f"TemplateHIR-v1 fn_id has invalid ordinal in package {pkg_id}"
							)
						sig_entry = entry.get("signature")
						if not isinstance(sig_entry, dict):
							ident = f"{fn_id.module}::{fn_id.name}"
							return _template_import_error(
								f"TemplateHIR-v1 entry missing signature dict for {ident} in package {pkg_id}"
							)
						impl_params = sig_entry.get("impl_type_params") or []
						fn_params = sig_entry.get("type_params") or []
						if not isinstance(impl_params, list) or not isinstance(fn_params, list):
							ident = f"{fn_id.module}::{fn_id.name}"
							return _template_import_error(
								f"TemplateHIR-v1 type_params not a list for {ident} in package {pkg_id}"
							)
						expected_layout = []
						for idx in range(len(impl_params)):
							expected_layout.append({"scope": "impl", "index": idx})
						for idx in range(len(fn_params)):
							expected_layout.append({"scope": "fn", "index": idx})
						layout = entry.get("generic_param_layout")
						if layout != expected_layout:
							ident = f"{fn_id.module}::{fn_id.name}"
							return _template_import_error(
								f"TemplateHIR-v1 generic_param_layout mismatch for {ident} in package {pkg_id}"
							)
						prev_layout = external_template_layout_by_key.get(fn_key)
						if prev_layout is not None and prev_layout != expected_layout:
							ident = f"{fn_id.module}::{fn_id.name}"
							return _template_import_error(
								f"TemplateHIR-v1 generic_param_layout conflict for {ident} in package {pkg_id}"
							)
						if prev_layout is None:
							external_template_layout_by_key[fn_key] = list(expected_layout)
					hir = entry.get("ir")
					if ir_kind == "TemplateHIR-v1" and not isinstance(hir, H.HBlock):
						if fn_id is not None:
							ident = f"{fn_id.module}::{fn_id.name}"
						else:
							ident = f"{fn_key.module_path}::{fn_key.name}"
						return _template_import_error(
							f"TemplateHIR-v1 entry missing HIR body for {ident} in package {pkg_id}"
						)
					if isinstance(hir, H.HBlock):
						external_template_hirs_by_key[fn_key] = normalize_hir(hir)
					if ir_kind == "TemplateHIR-v1":
						if fn_id is None:
							return _template_import_error(
								f"TemplateHIR-v1 entry missing fn_id after validation in package {pkg_id}"
							)
						sig = external_signatures_by_id.get(fn_id)
						if sig is None:
							# Signature may have been filtered (e.g., module in locally-compiled set).
							# This is recoverable — skip the template without error.
							continue
						if sig.param_types is None or sig.return_type is None:
							# Incomplete signature (missing TypeExprs) — skip template.
							continue
						decl_fp, _layout = compute_template_decl_fingerprint(
							sig,
							declared_name=fn_key.name,
							module_id=fn_key.module_path,
							require_expr=req if req is not None else None,
							default_package=pkg_id,
							module_packages=getattr(type_table, "module_packages", None),
						)
						if decl_fp != fn_key.decl_fingerprint:
							print(
								f"{_package_label()}:?:?: note: template '{fn_key.name}' "
								f"(pkg={pkg_id}, module={fn_key.module_path}): "
								f"declaration fingerprint mismatch; template skipped",
								file=sys.stderr,
							)
							continue
						try:
							fn_id = id_registry.intern_function(fn_key, preferred=fn_id)
						except ValueError:
							# Id conflict is recoverable — another template already
							# claimed this FunctionId.  Skip this duplicate silently.
							continue
						prev_key = external_template_keys_by_fn_id.get(fn_id)
						if prev_key is not None and prev_key != fn_key:
							return _template_import_error(
								f"duplicate template mapping for {fn_key.module_path}::{fn_key.name} in package {pkg_id}"
							)
						external_template_keys_by_fn_id[fn_id] = fn_key

		# Merge external trait metadata and function require clauses into trait worlds
		# so requirement enforcement can operate across package boundaries.
		if type_table is not None:
			from lang.driftc.traits.world import TraitWorld, ImplDef, type_key_from_typeid

			trait_worlds = getattr(type_table, "trait_worlds", None)
			if not isinstance(trait_worlds, dict):
				trait_worlds = {}
				type_table.trait_worlds = trait_worlds
			for trait_def in external_trait_defs:
				mod = getattr(trait_def.key, "module", None) or "main"
				world = trait_worlds.setdefault(mod, TraitWorld())
				if trait_def.key not in world.traits:
					world.traits[trait_def.key] = trait_def
			for impl in external_impl_metas:
				trait_key = getattr(impl, "trait_key", None)
				if trait_key is None:
					continue
				def_mod = getattr(impl, "def_module", None) or "main"
				world = trait_worlds.setdefault(def_mod, TraitWorld())
				target_key = type_key_from_typeid(type_table, impl.target_type_id)
				head_key = target_key.head()
				existing_ids = world.impls_by_trait_target.get((trait_key, head_key), [])
				impl_trait_args = tuple(
					type_key_from_typeid(type_table, tid)
					for tid in (getattr(impl, "trait_args", []) or [])
				)
				dup = False
				if existing_ids:
					for impl_id in existing_ids:
						existing = world.impls[impl_id]
						if existing.target == target_key and existing.trait_args == impl_trait_args and existing.require == getattr(impl, "require_expr", None):
							dup = True
							break
				if dup:
					continue
				impl_id = len(world.impls)
				world.impls.append(
					ImplDef(
						trait=trait_key,
						trait_args=impl_trait_args,
						target=target_key,
						target_head=head_key,
						methods=[],
						require=getattr(impl, "require_expr", None),
						loc=getattr(impl, "loc", None),
					)
				)
				world.impls_by_trait.setdefault(trait_key, []).append(impl_id)
				world.impls_by_target_head.setdefault(head_key, []).append(impl_id)
				world.impls_by_trait_target.setdefault((trait_key, head_key), []).append(impl_id)
			for fn_id, fn_key in external_template_keys_by_fn_id.items():
				req_expr = external_template_requires_by_key.get(fn_key)
				if req_expr is None:
					continue
				mod = getattr(fn_id, "module", None) or "main"
				world = trait_worlds.setdefault(mod, TraitWorld())
				world.requires_by_fn[fn_id] = req_expr

		# Import package constant tables into the host TypeTable so source code can
		# reference imported consts as typed literals.
		#
		# Const entries in package payloads use package-local TypeIds; remap them
		# through the link-time `tid_map` so the host TypeTable owns the canonical
		# ids used by the rest of the pipeline.
		for pkg in loaded_pkgs:
			tid_map = pkg_typeid_maps.get(pkg.path, {})
			for mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				consts_obj = payload.get("consts")
				if not isinstance(consts_obj, dict):
					continue
				for cname, entry in consts_obj.items():
					if not isinstance(cname, str) or not cname:
						continue
					if not isinstance(entry, dict):
						continue
					raw_tid = entry.get("type_id")
					val = entry.get("value")
					if not isinstance(raw_tid, int):
						continue
					remapped_tid = tid_map.get(raw_tid, raw_tid)
					sym = f"{mid}::{cname}"
					prev = getattr(type_table, "consts", {}).get(sym)
					if prev is not None:
						if prev != (remapped_tid, val):
							msg = f"const '{sym}' provided by multiple sources with different values"
							if args.json:
								print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
							else:
								print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
							return 1
						continue
					type_table.define_const(module_id=mid, name=cname, type_id=remapped_tid, value=val)

	# Materialize const re-exports into the exporting module’s const table.
	#
	# Consts are compile-time values embedded into IR at each use site and also
	# recorded in module interfaces/packages. When a module re-exports a const from
	# another module (e.g. `export { a.* }` where `a` exports `ANSWER`), downstream
	# consumers must be able to reference `b::ANSWER` *without* needing module `a`
	# present at compile time.
	#
	# Implementation strategy (MVP):
	# - export-resolution records `reexports.consts` mapping `{local: {module,name}}`
	#   for provenance.
	# - driftc copies the origin const's typed literal `(TypeId, value)` into the
	#   exporting module’s const table under `exporting_mid::local`.
	#
	# This step is performed after package const import because origin const values
	# may come from packages, and their TypeIds must be remapped into the host
	# TypeTable before we can copy them.
	for exporting_mid, exp in (module_exports or {}).items():
		if not isinstance(exp, dict):
			continue
		reexp = exp.get("reexports")
		if not isinstance(reexp, dict):
			continue
		consts_map = reexp.get("consts")
		if not isinstance(consts_map, dict):
			continue
		for local_name, target in consts_map.items():
			if not isinstance(local_name, str) or not local_name:
				continue
			if not isinstance(target, dict):
				continue
			origin_mid = target.get("module")
			origin_name = target.get("name")
			if not isinstance(origin_mid, str) or not origin_mid:
				continue
			if not isinstance(origin_name, str) or not origin_name:
				continue
			origin_sym = f"{origin_mid}::{origin_name}"
			dst_sym = f"{exporting_mid}::{local_name}"
			origin_entry = type_table.lookup_const(origin_sym)
			if origin_entry is None:
				msg = f"re-exported const '{dst_sym}' refers to missing const '{origin_sym}'"
				if args.json:
					print(
						json.dumps(
							{
								"exit_code": 1,
								"diagnostics": [
									{
										"phase": "typecheck",
										"message": msg,
										"severity": "error",
										"file": "<source>",
										"line": None,
										"column": None,
									}
								],
							}
						)
					)
				else:
					print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			origin_tid, origin_val = origin_entry
			prev = type_table.lookup_const(dst_sym)
			if prev is not None:
				if prev != (origin_tid, origin_val):
					msg = f"const '{dst_sym}' defined with a different value than re-export target '{origin_sym}'"
					if args.json:
						print(
							json.dumps(
								{
									"exit_code": 1,
									"diagnostics": [
										{
											"phase": "typecheck",
											"message": msg,
											"severity": "error",
											"file": "<source>",
											"line": None,
											"column": None,
										}
									],
								}
							)
						)
					else:
						print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1
				continue
			type_table.define_const(module_id=exporting_mid, name=local_name, type_id=origin_tid, value=origin_val)

	# Normalize HIR before any further analysis so:
	# - sugar does not leak into later stages, and
	# - borrow materialization runs before borrow checking.
	normalized_hirs_by_id = {fn_id: normalize_hir(block) for fn_id, block in func_hirs_by_id.items()}

	# Type check each function with the shared TypeTable/signatures.
	unsafe_trusted_modules = set()
	if type_table is not None:
		for mod_id, pkg_id in (getattr(type_table, "module_packages", {}) or {}).items():
			if pkg_id == "std":
				unsafe_trusted_modules.add(mod_id)
	if not unsafe_trusted_modules and module_exports is not None:
		for mod_id in module_exports.keys():
			if isinstance(mod_id, str) and mod_id.startswith("std."):
				unsafe_trusted_modules.add(mod_id)
	type_checker = TypeChecker(type_table=type_table, allow_unsafe=bool(getattr(args, "allow_unsafe", False)), unsafe_trusted_modules=unsafe_trusted_modules)
	callable_registry = CallableRegistry()
	next_callable_id = 1
	def _registry_impl_target_type_id(impl_tid: TypeId | None) -> TypeId | None:
		if impl_tid is None or type_table is None:
			return impl_tid
		td = type_table.get(impl_tid)
		if td.kind is TypeKind.REF and td.param_types:
			inner = td.param_types[0]
			inner_def = type_table.get(inner)
			if inner_def.kind is TypeKind.ARRAY:
				return type_table.array_base_id()
			return inner
		if td.kind is TypeKind.ARRAY:
			return type_table.array_base_id()
		return impl_tid
	def _template_sig_for(sig: FnSignature) -> CallableTemplateSignature | None:
		if not (sig.type_params or getattr(sig, "impl_type_params", [])):
			return None
		if sig.param_types is None or sig.return_type is None:
			return None
		return CallableTemplateSignature(param_types=tuple(sig.param_types), result_type=sig.return_type)
	type_diags: list[Diagnostic] = []
	if type_table is not None:
		type_checker.validate_interface_schemas(diagnostics=type_diags)
	module_ids: dict[object, int] = {None: 0}
	signatures_by_id_all: Mapping[FunctionId, FnSignature] = ChainMap(
		external_signatures_by_id,
		derived_signatures_by_id,
		base_signatures_by_id,
	)
	display_name_by_id = {fn_id: _display_name_for_fn_id(fn_id) for fn_id in signatures_by_id_all.keys()}

	for fn_id, sig in signatures_by_id.items():
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		if getattr(sig, "is_wrapper", False):
			continue
		param_types_tuple = tuple(sig.param_type_ids)
		module_name = getattr(fn_id, "module", None) or sig.module
		module_id = module_ids.setdefault(module_name, len(module_ids))
		if sig.is_method:
			if sig.impl_target_type_id is None:
				type_diags.append(
					Diagnostic(
						message=f"method '{display_name_by_id.get(fn_id, sig.name)}' missing receiver metadata (impl target/self_mode)",
						severity="error",
						phase="typecheck",
						span=getattr(sig, "loc", None),
					)
				)
				continue
			if sig.self_mode is None:
				continue
			self_mode = {
				"value": SelfMode.SELF_BY_VALUE,
				"ref": SelfMode.SELF_BY_REF,
				"ref_mut": SelfMode.SELF_BY_REF_MUT,
			}.get(sig.self_mode)
			if self_mode is None:
				type_diags.append(
					Diagnostic(
						message=f"method '{display_name_by_id.get(fn_id, sig.name)}' has unsupported self_mode '{sig.self_mode}'",
						severity="error",
						phase="typecheck",
						span=getattr(sig, "loc", None),
					)
				)
				continue
			callable_registry.register_inherent_method(
				callable_id=next_callable_id,
				name=sig.method_name or sig.name,
				module_id=module_id,
				visibility=Visibility.public() if sig.is_pub else Visibility.private(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				template_signature=_template_sig_for(sig),
				template_type_params=tuple(tp.name for tp in (sig.type_params or [])),
				template_impl_type_params=tuple(tp.name for tp in (getattr(sig, "impl_type_params", []) or [])),
				fn_id=fn_id,
				impl_id=next_callable_id,
				impl_target_type_id=_registry_impl_target_type_id(sig.impl_target_type_id),
				self_mode=self_mode,
				is_generic=bool(sig.type_params or getattr(sig, "impl_type_params", [])),
			)
			next_callable_id += 1
		else:
			callable_registry.register_free_function(
				callable_id=next_callable_id,
				name=fn_id.name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				template_signature=_template_sig_for(sig),
				template_type_params=tuple(tp.name for tp in (sig.type_params or [])),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
			next_callable_id += 1

	for fn_id, sig in external_signatures_by_id.items():
		if callable_registry.get_by_fn_id(fn_id) is not None:
			continue
		sig_name = display_name_by_id.get(fn_id, _display_name_for_fn_id(fn_id))
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		param_types_tuple = tuple(sig.param_type_ids)
		module_name = getattr(fn_id, "module", None) or sig.module
		if module_name is not None and module_name in modules:
			continue
		module_id = module_ids.setdefault(module_name, len(module_ids))
		if sig.is_method:
			if sig.impl_target_type_id is None:
				type_diags.append(
					Diagnostic(
						message=f"method '{sig_name}' missing receiver metadata (impl target/self_mode)",
						severity="error",
						phase="typecheck",
						span=getattr(sig, "loc", None),
					)
				)
				continue
			if sig.self_mode is None:
				continue
			self_mode = {
				"value": SelfMode.SELF_BY_VALUE,
				"ref": SelfMode.SELF_BY_REF,
				"ref_mut": SelfMode.SELF_BY_REF_MUT,
			}.get(sig.self_mode)
			if self_mode is None:
				type_diags.append(
					Diagnostic(
						message=f"method '{sig_name}' has unsupported self_mode '{sig.self_mode}'",
						severity="error",
						phase="typecheck",
						span=getattr(sig, "loc", None),
					)
				)
				continue
			callable_registry.register_inherent_method(
				callable_id=next_callable_id,
				name=sig.method_name or sig_name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				template_signature=_template_sig_for(sig),
				template_type_params=tuple(tp.name for tp in (sig.type_params or [])),
				template_impl_type_params=tuple(tp.name for tp in (getattr(sig, "impl_type_params", []) or [])),
				fn_id=fn_id,
				impl_id=next_callable_id,
				impl_target_type_id=_registry_impl_target_type_id(sig.impl_target_type_id),
				self_mode=self_mode,
				is_generic=bool(sig.type_params or getattr(sig, "impl_type_params", [])),
			)
			next_callable_id += 1
		else:
			callable_registry.register_free_function(
				callable_id=next_callable_id,
				name=fn_id.name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				template_signature=_template_sig_for(sig),
				template_type_params=tuple(tp.name for tp in (sig.type_params or [])),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
			next_callable_id += 1

	# candidate_signatures_for_diag removed; no name-keyed fallback map

	def _collect_reexport_targets(mod: str) -> set[str]:
		exp = module_exports.get(mod) if isinstance(module_exports, dict) else None
		if exp is None and isinstance(external_exports, dict):
			exp = external_exports.get(mod)
		if not isinstance(exp, dict):
			return set()
		reexp = exp.get("reexports") if isinstance(exp, dict) else None
		if not isinstance(reexp, dict):
			return set()
		targets: set[str] = set()
		type_reexp = reexp.get("types") if isinstance(reexp.get("types"), dict) else {}
		for kind in ("structs", "variants", "exceptions", "interfaces"):
			entries = type_reexp.get(kind) if isinstance(type_reexp, dict) else None
			if not isinstance(entries, dict):
				continue
			for info in entries.values():
				if isinstance(info, dict):
					tgt = info.get("module")
					if isinstance(tgt, str):
						targets.add(tgt)
		const_reexp = reexp.get("consts") if isinstance(reexp.get("consts"), dict) else {}
		if isinstance(const_reexp, dict):
			for info in const_reexp.values():
				if isinstance(info, dict):
					tgt = info.get("module")
					if isinstance(tgt, str):
						targets.add(tgt)
			trait_reexp = reexp.get("traits") if isinstance(reexp.get("traits"), dict) else {}
			if isinstance(trait_reexp, dict):
				for info in trait_reexp.values():
					if isinstance(info, dict):
						tgt = info.get("module")
						if isinstance(tgt, str):
							targets.add(tgt)
			value_reexp = reexp.get("values") if isinstance(reexp.get("values"), dict) else {}
			if isinstance(value_reexp, dict):
				for info in value_reexp.values():
					if isinstance(info, dict):
						tgt = info.get("module")
						if isinstance(tgt, str):
							targets.add(tgt)
			return targets

	# Ensure module ids exist for any module mentioned in the workspace graph.
	if isinstance(module_deps, dict):
		all_mods = set(module_deps.keys())
		if isinstance(module_exports, dict):
			all_mods |= set(module_exports.keys())
		for mid in sorted(all_mods):
			module_ids.setdefault(mid, len(module_ids))

	def _better_chain(new_chain: tuple[str, ...], existing: tuple[str, ...] | None) -> bool:
		if existing is None:
			return True
		if len(new_chain) != len(existing):
			return len(new_chain) < len(existing)
		return new_chain < existing

	visible_modules_by_name: dict[str, tuple[int, ...]] = {}
	visible_module_names_by_name: dict[str, set[str]] = {}
	visibility_provenance_by_name: dict[str, dict[str, tuple[str, ...]]] = {}
	if isinstance(module_deps, dict):
		prelude_modules: set[str] = set()
		if args.prelude:
			for fn_id in signatures_by_id.keys():
				if fn_id.module == "lang.core":
					prelude_modules.add("lang.core")
					break
		for mod_name in module_deps.keys():
			imports = set(module_deps.get(mod_name, set()))
			best: dict[str, tuple[str, ...]] = {mod_name: (mod_name,)}
			queue: list[tuple[int, tuple[str, ...], str]] = []
			heapq.heappush(queue, (1, (mod_name,), mod_name))
			while queue:
				_len, chain, cur = heapq.heappop(queue)
				if best.get(cur) != chain:
					continue
				neighbors = set(_collect_reexport_targets(cur))
				if cur == mod_name:
					neighbors |= imports
				for tgt in sorted(neighbors):
					new_chain = chain + (tgt,)
					if _better_chain(new_chain, best.get(tgt)):
						best[tgt] = new_chain
						heapq.heappush(queue, (len(new_chain), new_chain, tgt))
			visible = set(best.keys())
			if prelude_modules:
				for prelude in sorted(prelude_modules):
					best.setdefault(prelude, (mod_name, prelude))
				visible |= prelude_modules
			visible_module_names_by_name[mod_name] = visible
			visibility_provenance_by_name[mod_name] = best
			visible_ids_list = []
			for m in sorted(visible):
				visible_ids_list.append(module_ids.setdefault(m, len(module_ids)))
			visible_ids = tuple(visible_ids_list)
			visible_modules_by_name[mod_name] = visible_ids
	else:
		all_mods = set(merged_programs.keys())
		if args.prelude:
			for fn_id in signatures_by_id.keys():
				if fn_id.module == "lang.core":
					all_mods.add("lang.core")
					break
		for mod_name in sorted(all_mods):
			visible_module_names_by_name[mod_name] = set(all_mods)
			visibility_provenance_by_name[mod_name] = {m: (mod_name, m) for m in all_mods}
			visible_ids_list = []
			for m in sorted(all_mods):
				visible_ids_list.append(module_ids.setdefault(m, len(module_ids)))
			visible_modules_by_name[mod_name] = tuple(visible_ids_list)

	global_impl_index = GlobalImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	for impl in external_impl_metas:
		if getattr(impl, "trait_key", None) is None:
			global_impl_index.add_impl(impl=impl, type_table=type_table, module_ids=module_ids)
	global_trait_index = GlobalTraitIndex.from_trait_worlds(getattr(type_table, "trait_worlds", None))
	for trait_def in external_trait_defs:
		if hasattr(trait_def, "key"):
			global_trait_index.add_trait(trait_def.key, trait_def)
	for missing_trait in external_missing_traits:
		if hasattr(missing_trait, "module") and hasattr(missing_trait, "name"):
			global_trait_index.mark_missing(missing_trait)
	global_trait_impl_index = GlobalTraitImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	for impl in external_impl_metas:
		if getattr(impl, "trait_key", None) is not None:
			global_trait_impl_index.add_impl(impl=impl, type_table=type_table, module_ids=module_ids)
	for module_id in external_missing_impl_modules:
		global_trait_impl_index.mark_missing_module(module_ids.setdefault(module_id, len(module_ids)))
	global_trait_impl_index.module_names_by_id = {
		mod_id: name for name, mod_id in module_ids.items() if name is not None
	}
	trait_scope_by_module: dict[str, list] = {}
	if isinstance(module_exports, dict):
		for mod_name, exp in module_exports.items():
			if isinstance(exp, dict):
				scope = exp.get("trait_scope", [])
				if isinstance(scope, list):
					trait_scope_by_module[mod_name] = list(scope)
				else:
					trait_scope_by_module[mod_name] = []
	visible_modules_by_name_set = {
		mod: set(visible) for mod, visible in visible_module_names_by_name.items()
	}

	type_diags.extend(
		validate_trait_scopes(
			trait_index=global_trait_index,
			trait_impl_index=global_trait_impl_index,
			trait_scope_by_module=trait_scope_by_module,
			module_ids=module_ids,
		)
	)
	type_diags.extend(
		find_impl_method_conflicts(
			module_exports=module_exports,
			signatures_by_id=signatures_by_id,
			type_table=type_table,
			visible_modules_by_name=visible_modules_by_name_set,
		)
	)

	signatures_by_id_all = dict(signatures_by_id)
	signatures_by_id_all.update(external_signatures_by_id)
	linked_world, require_env = _build_linked_world(type_table)

	typed_fns: dict[FunctionId, object] = {}
	for fn_id, hir_block in normalized_hirs_by_id.items():
		# Build param type map from signatures when available.
		param_types: dict[str, "TypeId"] = {}
		param_mutable: dict[str, bool] | None = None
		sig = signatures_by_id.get(fn_id)
		if sig and sig.param_names and sig.param_type_ids:
			param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids) if pty is not None}
		if sig and sig.param_names and sig.param_mutable:
			if len(sig.param_names) == len(sig.param_mutable):
				param_mutable = {pname: bool(flag) for pname, flag in zip(sig.param_names, sig.param_mutable)}
		current_file = None
		if fn_id in origin_by_fn_id:
			current_file = str(origin_by_fn_id.get(fn_id))
		elif sig is not None:
			current_file = Span.from_loc(getattr(sig, "loc", None)).file
		fn_module_name = sig.module if sig is not None and sig.module is not None else "main"
		fn_module_id = module_ids.setdefault(fn_module_name, len(module_ids))
		visible_modules = visible_modules_by_name.get(fn_module_name, (fn_module_id,))
		visibility_by_id: dict[ModuleId, tuple[str, ...]] = {}
		provenance_by_name = visibility_provenance_by_name.get(fn_module_name, {})
		for mod_name, chain in provenance_by_name.items():
			visibility_by_id[module_ids.setdefault(mod_name, len(module_ids))] = chain
		direct_imports = set(module_deps.get(fn_module_name, set())) if isinstance(module_deps, dict) else None
		result = type_checker.check_function(
			fn_id,
			hir_block,
			param_types=param_types,
			param_mutable=param_mutable,
			return_type=sig.return_type_id if sig is not None else None,
			callable_registry=callable_registry,
			impl_index=global_impl_index,
			trait_index=global_trait_index,
			trait_impl_index=global_trait_impl_index,
			trait_scope_by_module=trait_scope_by_module,
			linked_world=linked_world,
			require_env=require_env,
			visible_modules=visible_modules,
			current_module=fn_module_id,
			visibility_provenance=visibility_by_id,
			visibility_imports=direct_imports,
			signatures_by_id=signatures_by_id_all,
		)
		type_diags.extend(result.diagnostics)
		typed_fns[fn_id] = result.typed_fn
	if type_checker.defaulted_phase_count() != 0:
		raise AssertionError(
			f"typecheck diagnostics missing phase (defaulted={type_checker.defaulted_phase_count()})"
		)

	if type_diags:
		_assert_all_phased(type_diags, context="typecheck")
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "typecheck", source_path) for d in type_diags],
			}
			print(json.dumps(payload))
			return 1
		else:
			for d in type_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
			return 1

	# Compute non-retaining metadata for callable parameters before lambda validation.
	signatures_by_id_all = analyze_non_retaining_params(
		typed_fns,
		signatures_by_id_all,
		type_table=type_table,
	)

	# Phase 3a/3b: annotate known stdlib callable params with escape levels.
	_apply_stdlib_escape_annotations(signatures_by_id_all)

	# Enforce non-escaping lambda rule after type resolution so method calls are visible.
	lambda_diags: list[Diagnostic] = []
	for _fn_id, typed_fn in typed_fns.items():
		res = validate_lambdas_non_retaining(
			typed_fn.body,
			signatures_by_id=signatures_by_id_all,
			call_resolutions=getattr(typed_fn, "call_resolutions", None),
		)
		lambda_diags.extend(res.diagnostics)
	if lambda_diags:
		_assert_all_phased(lambda_diags, context="typecheck")
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "typecheck", source_path) for d in lambda_diags],
			}
			print(json.dumps(payload))
		else:
			for d in lambda_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
		return 1

	# Checker (stub) enforces language-level rules (e.g., nothrow) after typecheck
	# so we can use CallInfo for method-call throw analysis.
	call_info_by_callsite_id: dict[FunctionId, dict[int, CallInfo]] = {}
	for fn_id, typed_fn in typed_fns.items():
		call_info = getattr(typed_fn, "call_info_by_callsite_id", None)
		if isinstance(call_info, dict):
			call_info_by_callsite_id[fn_id] = dict(call_info)
		else:
			call_info_by_callsite_id.setdefault(fn_id, {})
	checked = Checker.run_by_id(
		CheckerInputsById(
			hir_blocks_by_id=normalized_hirs_by_id,
			signatures_by_id=signatures_by_id_all,
			call_info_by_callsite_id=call_info_by_callsite_id,
		),
		exception_catalog=exception_catalog,
		type_table=type_table,
	)
	if checked.type_table is not None and not loaded_pkgs:
		type_table = checked.type_table
	if checked.diagnostics:
		_assert_all_phased(checked.diagnostics, context="typecheck")
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "typecheck", source_path) for d in checked.diagnostics],
			}
			print(json.dumps(payload))
		else:
			for d in checked.diagnostics:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
		return 1

	# Reconcile method call CallInfo with checker-inferred throw behavior.
	#
	# For method calls, CallInfo is authored during typecheck before the stub
	# checker infers can-throw. When signatures omit `nothrow`, the checker may
	# infer nothrow for methods that never throw; update the call-site metadata
	# accordingly so nothrow enforcement matches inference.
	for typed_fn in typed_fns.values():
		call_info = getattr(typed_fn, "call_info_by_callsite_id", None)
		if not isinstance(call_info, dict):
			continue
		for csid, info in list(call_info.items()):
			if info.target.kind is not CallTargetKind.DIRECT or info.target.symbol is None:
				continue
			target_id = info.target.symbol
			sig = signatures_by_id.get(target_id)
			if sig is None or not sig.is_method:
				continue
			if getattr(sig, "is_wrapper", False):
				continue
			if sig.declared_can_throw is not None:
				continue
			fn_info = checked.fn_infos_by_id.get(target_id)
			if fn_info is None:
				continue
			inferred = bool(fn_info.declared_can_throw)
			if inferred == info.sig.can_throw:
				continue
			new_sig = CallSig(
				param_types=info.sig.param_types,
				user_ret_type=info.sig.user_ret_type,
				can_throw=inferred,
			)
			call_info[csid] = CallInfo(target=info.target, sig=new_sig)

	# Enforce trait requirements (struct + function requires) before borrow checking.
	trait_diags: list[Diagnostic] = []
	linked_world, require_env = _build_linked_world(type_table)
	if linked_world is not None and require_env is not None:
		used_types = collect_used_type_keys(typed_fns, type_table, signatures_by_id)
		used_by_module: dict[str, set] = {}
		used_unknown: set = set()
		for ty in used_types:
			mod = getattr(ty, "module", None)
			if mod is None:
				used_unknown.add(ty)
				continue
			used_by_module.setdefault(mod, set()).add(ty)
		for module_name in linked_world.trait_worlds.keys():
			module_used = set(used_by_module.get(module_name, set()))
			module_used.update(used_unknown)
			visible_modules = visible_module_names_by_name.get(module_name, {module_name})
			res = enforce_struct_requires(
				linked_world,
				require_env,
				module_used,
				module_name=module_name,
				visible_modules=visible_modules,
			)
			trait_diags.extend(res.diagnostics)
		for fn_id, typed_fn in typed_fns.items():
			module_name = fn_id.module or "main"
			visible_modules = visible_module_names_by_name.get(module_name, {module_name})
			res = enforce_fn_requires(
				linked_world,
				require_env,
				typed_fn,
				type_table,
				module_name=module_name,
				signatures=signatures_by_id,
				visible_modules=visible_modules,
			)
			trait_diags.extend(res.diagnostics)
	if trait_diags:
		_assert_all_phased(trait_diags, context="typecheck")
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "typecheck", source_path) for d in trait_diags],
			}
			print(json.dumps(payload))
		else:
			for d in trait_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
		return 1

	intrinsic_diags: list[Diagnostic] = []
	for typed_fn in typed_fns.values():
		intrinsic_diags.extend(_validate_intrinsic_callinfo(typed_fn))
	if intrinsic_diags:
		_assert_all_phased(intrinsic_diags, context="typecheck")
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "typecheck", source_path) for d in intrinsic_diags],
			}
			print(json.dumps(payload))
			return 1
		else:
			for d in intrinsic_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
		return 1

	# Borrow check each typed function (mandatory stage).
	borrow_diags: list[Diagnostic] = []
	for _fn_id, typed_fn in typed_fns.items():
		bc = BorrowChecker.from_typed_fn(
			typed_fn,
			type_table=type_table,
			signatures_by_id=signatures_by_id_all,
			enable_auto_borrow=True,
		)
		borrow_diags.extend(bc.check_block(typed_fn.body))

	if borrow_diags:
		_assert_all_phased(borrow_diags, context="borrowcheck")
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "borrowcheck", source_path) for d in borrow_diags],
			}
			print(json.dumps(payload))
			return 1
		else:
			for d in borrow_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
		return 1

	# Package emission mode (Milestone 4): produce an unsigned package artifact
	# containing provisional DMIR payloads for all modules in the workspace.
	if args.emit_package is not None:
		if not args.package_id or not args.package_version or not args.package_target:
			msg = "--emit-package requires --package-id, --package-version, and --package-target"
			if args.json:
				print(
					json.dumps(
						{
							"exit_code": 1,
							"diagnostics": [
								{
									"phase": "package",
									"message": msg,
									"severity": "error",
									"file": "<source>",
									"line": None,
									"column": None,
								}
							],
						}
					)
				)
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1
		if package_id is None:
			msg = "--emit-package requires a non-empty package id"
			if args.json:
				print(
					json.dumps(
						{
							"exit_code": 1,
							"diagnostics": [
								{
									"phase": "package",
									"message": msg,
									"severity": "error",
									"file": "<source>",
									"line": None,
									"column": None,
								}
							],
						}
					)
				)
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1

		signatures_for_pkg = signatures_by_id_all if loaded_pkgs else signatures_by_id
		combined_exports: dict[str, dict[str, object]] | None = None
		if module_exports or external_exports:
			combined_exports = dict(external_exports or {})
			if isinstance(module_exports, dict):
				combined_exports.update(module_exports)
		mir_funcs, checked_pkg = compile_stubbed_funcs(
			func_hirs=func_hirs_by_id,
			signatures=signatures_for_pkg,
			exc_env=exception_catalog,
			module_exports=combined_exports,
			module_deps=module_deps,
			origin_by_fn_id=origin_by_fn_id,
			type_table=type_table,
			package_id=package_id,
			external_trait_defs=external_trait_defs,
			external_impl_metas=external_impl_metas,
			external_missing_traits=external_missing_traits,
			external_missing_impl_modules=external_missing_impl_modules,
			return_checked=True,
			prelude_enabled=bool(args.prelude),
			generic_templates_by_key=external_template_hirs_by_key,
			template_keys_by_fn_id=external_template_keys_by_fn_id,
			emit_instantiation_index=args.emit_instantiation_index,
			enforce_entrypoint=bool(args.output or args.emit_ir),
		)
		_assert_all_phased(checked_pkg.diagnostics, context="typecheck")
		if any(d.severity == "error" for d in checked_pkg.diagnostics):
			if args.json:
				payload = {
					"exit_code": 1,
					"diagnostics": [_diag_to_json(d, "stage4", source_path) for d in checked_pkg.diagnostics],
				}
				print(json.dumps(payload))
			else:
				for d in checked_pkg.diagnostics:
					loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
					print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
			return 1
	
		pkg_signatures_by_symbol: dict[str, FnSignature] = {
			function_symbol(fn_id): info.signature
			for fn_id, info in checked_pkg.fn_infos_by_id.items()
			if info.signature is not None
		}
		signatures_by_symbol = {
			function_symbol(fn_id): sig for fn_id, sig in signatures_by_id_all.items()
		}
	
		# Group functions/signatures by module id.
		source_module_ids = {getattr(fn_id, "module", None) or "main" for fn_id in normalized_hirs_by_id.keys()}
		per_module_sigs: dict[str, dict[str, FnSignature]] = {}
		inst_sigs: dict[str, FnSignature] = {}
		for name, sig in pkg_signatures_by_symbol.items():
			if getattr(sig, "is_instantiation", False):
				inst_sigs[name] = sig
				continue
			mid = getattr(sig, "module", None) or "main"
			if mid not in source_module_ids:
				continue
			per_module_sigs.setdefault(mid, {})[name] = sig
	
		per_module_mir: dict[str, dict[str, object]] = {}
		inst_mir: dict[str, object] = {}
		for fn_id, fn in mir_funcs.items():
			name = function_symbol(fn_id)
			sig = pkg_signatures_by_symbol.get(name)
			mid = getattr(sig, "module", None) if sig is not None else None
			mid = mid or "main"
			if sig is not None and getattr(sig, "is_instantiation", False):
				inst_mir[name] = fn
				continue
			if mid in source_module_ids:
				per_module_mir.setdefault(mid, {})[name] = fn
		per_module_hir: dict[str, dict[str, H.HBlock]] = {}
		for fn_id, block in normalized_hirs_by_id.items():
			mid = getattr(fn_id, "module", None) or "main"
			per_module_hir.setdefault(mid, {})[function_symbol(fn_id)] = block
	
		inst_module_id: str | None = None
		if inst_sigs or inst_mir:
			inst_module_id = f"{package_id}.__instantiations"
			if inst_sigs:
				per_module_sigs.setdefault(inst_module_id, {}).update(inst_sigs)
			if inst_mir:
				per_module_mir.setdefault(inst_module_id, {}).update(inst_mir)
	
		blobs_by_sha: dict[str, bytes] = {}
		blob_types: dict[str, int] = {}
		blob_names: dict[str, str] = {}
		manifest_modules: list[dict[str, object]] = []
		manifest_blobs: dict[str, dict[str, object]] = {}
		module_origin_by_id: dict[str, Path] = {}
		for fn_id, src_path in origin_by_fn_id.items():
			mid = getattr(fn_id, "module", None) or "main"
			if mid not in module_origin_by_id and isinstance(src_path, Path):
				module_origin_by_id[mid] = src_path
		module_source_by_id: dict[str, Path] = {}
		for mid, mod in modules.items():
			sp = getattr(mod, "source_path", None)
			if isinstance(sp, Path):
				module_source_by_id[mid] = sp
		stdlib_root_path = Path(args.stdlib_root).resolve() if args.stdlib_root else None

		all_module_ids: set[str] = set(per_module_sigs.keys()) | set(per_module_mir.keys())
		if isinstance(module_exports, dict):
			all_module_ids |= set(str(k) for k in module_exports.keys())
		pkg_next_impl_id = 0
		for mid in sorted(all_module_ids):
			# MVP packaging: do not bundle toolchain modules. They are supplied by
			# the toolchain and distributed separately under reserved namespaces.
			if mid.startswith(("std.", "lang.", "drift.")):
				if stdlib_root_path is not None:
					mod_origin = module_origin_by_id.get(mid)
					if mod_origin is None:
						mod_origin = module_source_by_id.get(mid)
					if mod_origin is not None:
						try:
							mod_origin.resolve().relative_to(stdlib_root_path)
						except Exception:
							pass
						else:
							continue
				else:
					continue
	
			# Export surface uses module-local names (unqualified). Global names
			# inside the compiler are qualified (`mid::name`).
			exported_values: list[str] = []
			exported_types_obj: object = {}
			exported_traits_obj: object = {}
			reexports_obj: object = {}
			mexp: dict[str, object] = {}
			if isinstance(module_exports, dict):
				mexp_obj = module_exports.get(mid, {})
				if isinstance(mexp_obj, dict):
					mexp = mexp_obj
					exported_types_obj = mexp.get("types", {})
					exported_traits_obj = mexp.get("traits", [])
					reexports_obj = mexp.get("reexports", {})
					vals_obj = mexp.get("values")
					if isinstance(vals_obj, list):
						exported_values = list(vals_obj)
			if not exported_values:
				for sym_name, sig in per_module_sigs.get(mid, {}).items():
					if not getattr(sig, "is_exported_entrypoint", False):
						continue
					if sig.is_method:
						continue
					prefix = f"{mid}::"
					exported_values.append(sym_name[len(prefix) :] if sym_name.startswith(prefix) else sym_name)
			exported_values.sort()
			if not isinstance(exported_types_obj, dict):
				exported_types_obj = {}
			if not isinstance(reexports_obj, dict):
				reexports_obj = {}
			exported_types: dict[str, list[str]] = {
				"structs": list(exported_types_obj.get("structs", [])) if isinstance(exported_types_obj.get("structs"), list) else [],
				"variants": list(exported_types_obj.get("variants", [])) if isinstance(exported_types_obj.get("variants"), list) else [],
				"exceptions": list(exported_types_obj.get("exceptions", [])) if isinstance(exported_types_obj.get("exceptions"), list) else [],
				"interfaces": list(exported_types_obj.get("interfaces", [])) if isinstance(exported_types_obj.get("interfaces"), list) else [],
			}
			exported_traits: list[str] = (
				list(exported_traits_obj) if isinstance(exported_traits_obj, (list, set, tuple)) else []
			)
			exported_consts: list[str] = (
				list(module_exports.get(mid, {}).get("consts", [])) if isinstance(module_exports, dict) else []
			)
	
			trait_worlds = getattr(type_table, "trait_worlds", {}) if type_table is not None else {}
			trait_world = trait_worlds.get(mid) if isinstance(trait_worlds, dict) else None
			requires_by_symbol: dict[str, object] = {}
			if trait_world is not None and hasattr(trait_world, "requires_by_fn"):
				for fn_id, req_expr in getattr(trait_world, "requires_by_fn", {}).items():
					requires_by_symbol[function_symbol(fn_id)] = req_expr
			if package_id is None:
				raise ValueError("package_id is required to emit module payloads")
			trait_metadata = _encode_trait_metadata_for_module(
				package_id=package_id,
				module_id=mid,
				exported_traits=exported_traits,
				trait_world=trait_world,
			)
			impl_headers = _encode_impl_headers_for_module(
				module_id=mid,
				impls=list(module_exports.get(mid, {}).get("impls", []))
				if isinstance(module_exports, dict)
				else [],
			)
			for hdr in impl_headers:
				hdr["impl_id"] = pkg_next_impl_id
				pkg_next_impl_id += 1

			# Synthesize signatures for value re-exports so package consumers can
			# reference them without requiring trampolines.
			sig_env: dict[str, FnSignature] = dict(per_module_sigs.get(mid, {}))
			if isinstance(reexports_obj, dict):
				reexp_vals = reexports_obj.get("values")
				if isinstance(reexp_vals, dict):
					for local_name, entry in reexp_vals.items():
						if not isinstance(entry, dict):
							continue
						origin_mod = entry.get("module")
						origin_name = entry.get("name")
						if not isinstance(origin_mod, str) or not isinstance(origin_name, str):
							continue
						local_sym = f"{mid}::{local_name}"
						if local_sym in sig_env:
							continue
						origin_sym = f"{origin_mod}::{origin_name}"
						origin_sig = signatures_by_symbol.get(origin_sym)
						if origin_sig is None:
							msg = f"internal: missing signature metadata for re-export target '{origin_sym}'"
							if args.json:
								print(
									json.dumps(
										{
											"exit_code": 1,
											"diagnostics": [
												{
													"phase": "package",
													"message": msg,
													"severity": "error",
													"file": "<source>",
													"line": None,
													"column": None,
												}
											],
										}
									)
								)
							else:
								print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
							return 1
						sig_env[local_sym] = replace(
							origin_sig,
							name=local_sym,
							module=mid,
							is_exported_entrypoint=True,
						)
			# Ensure exported values are marked as entrypoints in the package
			# signature table, including `main`, so provider validation matches
			# the exported surface.
			for local_name in exported_values:
				sym = f"{mid}::{local_name}"
				sig = sig_env.get(sym)
				if sig is None:
					continue
				if not getattr(sig, "is_exported_entrypoint", False):
					sig_env[sym] = replace(sig, is_exported_entrypoint=True)
	
			payload_obj = encode_module_payload_v0(
				package_id=package_id,
				module_id=mid,
				type_table=checked_pkg.type_table or type_table,
				signatures=sig_env,
				mir_funcs=per_module_mir.get(mid, {}),
				generic_templates=encode_generic_templates(
					package_id=package_id,
					module_id=mid,
					signatures=sig_env,
					hir_blocks=per_module_hir.get(mid, {}),
					requires_by_symbol=requires_by_symbol,
					module_packages=getattr(checked_pkg.type_table or type_table, "module_packages", None),
				),
				exported_values=exported_values,
				exported_types=exported_types,
				exported_traits=exported_traits,
				exported_consts=exported_consts,
				reexports=reexports_obj,
				trait_metadata=trait_metadata,
				impl_headers=impl_headers,
			)
	
			# Module interface (package interface table v0).
			#
			# This is the authoritative exported surface used by:
			# - the workspace loader for import validation, and
			# - driftc for ABI-boundary enforcement at call sites.
			#
			# Tightening rule: exported values must have corresponding signature
			# entries, and the interface must match the payload exports exactly.
			exported_syms = [f"{mid}::{v}" for v in exported_values]
			payload_sigs = payload_obj.get("signatures") if isinstance(payload_obj, dict) else None
			if not isinstance(payload_sigs, dict):
				payload_sigs = {}
			iface_sigs: dict[str, object] = {}
			for sym in exported_syms:
				sd = payload_sigs.get(sym)
				if not isinstance(sd, dict):
					msg = f"internal: missing signature metadata for exported value '{sym}' while emitting package"
					if args.json:
						print(
							json.dumps(
								{
									"exit_code": 1,
									"diagnostics": [
										{
											"phase": "package",
											"message": msg,
											"severity": "error",
											"file": "<source>",
											"line": None,
											"column": None,
										}
									],
								}
							)
						)
					else:
						print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1
				iface_sigs[sym] = sd
	
			# Exported schemas (exceptions/variants) for the type namespace.
			#
			# These are used as load-time guardrails: exported type schemas must match
			# payload schemas exactly. For MVP, we include schemas only for exported
			# exceptions and variants; structs are validated via TypeTable linking.
			payload_tt = payload_obj.get("type_table") if isinstance(payload_obj, dict) else None
			if not isinstance(payload_tt, dict):
				payload_tt = {}
	
			iface_exc: dict[str, object] = {}
			payload_exc = payload_tt.get("exception_schemas")
			if isinstance(payload_exc, dict):
				for t in exported_types.get("exceptions", []):
					fqn = f"{mid}:{t}"
					raw = payload_exc.get(fqn)
					if isinstance(raw, (list, tuple)) and len(raw) == 2 and isinstance(raw[1], (list, tuple)):
						iface_exc[fqn] = list(raw[1])
	
			iface_var: dict[str, object] = {}
			payload_var = payload_tt.get("variant_schemas")
			if isinstance(payload_var, dict):
				for raw in payload_var.values():
					if not isinstance(raw, dict):
						continue
					if raw.get("module_id") != mid:
						continue
					name = raw.get("name")
					if not isinstance(name, str) or name not in exported_types.get("variants", []):
						continue
					iface_var[name] = raw
	
			iface_obj = {
				"format": "drift-module-interface",
				"version": 0,
				"module_id": mid,
				"exports": payload_obj.get(
					"exports",
					{
						"values": [],
						"types": {"structs": [], "variants": [], "exceptions": [], "interfaces": []},
						"consts": [],
						"traits": [],
					},
				),
				"reexports": payload_obj.get("reexports", {}) if isinstance(payload_obj, dict) else {},
				"signatures": iface_sigs,
				"exception_schemas": iface_exc,
				"variant_schemas": iface_var,
				"consts": payload_obj.get("consts", {}) if isinstance(payload_obj, dict) else {},
				"trait_metadata": payload_obj.get("trait_metadata", []) if isinstance(payload_obj, dict) else [],
				"impl_headers": payload_obj.get("impl_headers", []) if isinstance(payload_obj, dict) else [],
			}
			iface_bytes = canonical_json_bytes(iface_obj)
			iface_sha = sha256_hex(iface_bytes)
			blobs_by_sha[iface_sha] = iface_bytes
			blob_types[iface_sha] = 2
			blob_names[iface_sha] = f"iface:{mid}"
			manifest_blobs[f"sha256:{iface_sha}"] = {"type": "exports", "length": len(iface_bytes)}
	
			payload_bytes = canonical_json_bytes(payload_obj)
			payload_sha = sha256_hex(payload_bytes)
			blobs_by_sha[payload_sha] = payload_bytes
			blob_types[payload_sha] = 1
			blob_names[payload_sha] = f"dmir:{mid}"
			manifest_blobs[f"sha256:{payload_sha}"] = {"type": "dmir", "length": len(payload_bytes)}
	
			manifest_modules.append(
				{
					"module_id": mid,
					"exports": {
						"values": exported_values,
						"types": exported_types,
						"consts": exported_consts,
						"traits": exported_traits,
					},
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			)
	
		manifest_obj: dict[str, object] = {
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": package_id,
			"package_version": str(args.package_version),
			"target": str(args.package_target),
			"abi_fingerprint": _abi_fingerprint(str(args.package_target), word_bits=host_word_bits()),
			"build_epoch": str(args.package_build_epoch) if args.package_build_epoch else None,
			"unsigned": True,
			"unstable_format": True,
			"payload_kind": "provisional-dmir",
			"payload_version": 0,
			"modules": manifest_modules,
			"blobs": manifest_blobs,
		}
	
		write_dmir_pkg_v0(
			args.emit_package,
			manifest_obj=manifest_obj,
			blobs=blobs_by_sha,
			blob_types=blob_types,
			blob_names=blob_names,
		)
	
		if args.json:
			print(json.dumps({"exit_code": 0, "diagnostics": []}))
		return 0
	
	# If no codegen requested, acknowledge success.
	if args.output is None and args.emit_ir is None:
		if args.json:
			print(json.dumps({"exit_code": 0, "diagnostics": []}))
		return 0

	if loaded_pkgs:
		# Compile source functions through the normal pipeline to get MIR+SSA.
		combined_exports: dict[str, dict[str, object]] | None = None
		if module_exports or external_exports:
			combined_exports = dict(external_exports or {})
			if isinstance(module_exports, dict):
				combined_exports.update(module_exports)
		src_mir, checked_src, ssa_src = compile_stubbed_funcs(
			func_hirs=func_hirs_by_id,
			signatures=signatures_by_id_all,
			exc_env=exception_catalog,
			module_exports=combined_exports,
			module_deps=module_deps,
			origin_by_fn_id=origin_by_fn_id,
			return_checked=True,
			build_ssa=True,
			return_ssa=True,
			type_table=type_table,
			prelude_enabled=bool(args.prelude),
			generic_templates_by_key=external_template_hirs_by_key,
			template_keys_by_fn_id=external_template_keys_by_fn_id,
			emit_instantiation_index=args.emit_instantiation_index,
			external_trait_defs=external_trait_defs,
			external_impl_metas=external_impl_metas,
			external_missing_traits=external_missing_traits,
			external_missing_impl_modules=external_missing_impl_modules,
			enforce_entrypoint=True,
		)
		_assert_all_phased(checked_src.diagnostics, context="typecheck")
		ssa_src = ssa_src or {}

		if any(d.severity == "error" for d in checked_src.diagnostics):
			if args.json:
				payload = {
					"exit_code": 1,
					"diagnostics": [_diag_to_json(d, "stage4", source_path) for d in checked_src.diagnostics],
				}
				print(json.dumps(payload))
			else:
				for d in checked_src.diagnostics:
					loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
					print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
			return 1

		# Decode package MIR payloads. We intentionally do not blindly embed all
		# loaded package modules; instead we include only the call-graph closure
		# reachable from the source module(s). This keeps builds predictable and
		# avoids unnecessary collisions/work.
		pkg_mir_all: dict[FunctionId, M.MirFunc] = {}
		pkg_sigs_by_id: dict[FunctionId, FnSignature] = {}
		for pkg in loaded_pkgs:
			tid_map = pkg_typeid_maps.get(pkg.path, {})
			for _mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				if payload.get("payload_kind") != "provisional-dmir" or payload.get("payload_version") != 0:
					msg = f"unsupported package payload kind/version in {_package_label()}"
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
					else:
						print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1
				sigs_obj = payload.get("signatures")
				name_to_fn_id: dict[str, FunctionId] = {}
				if isinstance(sigs_obj, dict):
					for name, sd in sigs_obj.items():
						if not isinstance(sd, dict):
							continue
						fn_id_obj = sd.get("fn_id")
						fn_id = function_id_from_obj(fn_id_obj)
						if fn_id is not None:
							name_to_fn_id[str(name)] = fn_id
				mir_obj = payload.get("mir_funcs")
				if isinstance(mir_obj, dict):
					for fn_id, fn in decode_mir_funcs(mir_obj, name_to_fn_id=name_to_fn_id).items():
						if isinstance(fn, M.MirFunc):
							_remap_mir_func_typeids(fn, tid_map)
							pkg_mir_all[fn_id] = fn
				if isinstance(sigs_obj, dict):
					for name, sd in sigs_obj.items():
						if not isinstance(sd, dict):
							continue
						fn_id_obj = sd.get("fn_id")
						fn_id = function_id_from_obj(fn_id_obj)
						if fn_id is None or fn_id in pkg_sigs_by_id:
							continue
						param_type_ids = sd.get("param_type_ids")
						if isinstance(param_type_ids, list):
							param_type_ids = [tid_map.get(int(x), int(x)) for x in param_type_ids]
						ret_tid = sd.get("return_type_id")
						if isinstance(ret_tid, int):
							ret_tid = tid_map.get(ret_tid, ret_tid)
						impl_tid = sd.get("impl_target_type_id")
						if isinstance(impl_tid, int):
							impl_tid = tid_map.get(impl_tid, impl_tid)
						pkg_sigs_by_id[fn_id] = FnSignature(
							name=str(sd.get("name") or name),
							module=sd.get("module"),
							method_name=sd.get("method_name"),
							param_names=sd.get("param_names"),
						param_type_ids=param_type_ids,
						return_type_id=ret_tid,
						declared_can_throw=sd.get("declared_can_throw"),
						is_method=bool(sd.get("is_method", False)),
						self_mode=sd.get("self_mode"),
						impl_target_type_id=impl_tid,
						is_pub=bool(sd.get("is_pub", False)),
						is_exported_entrypoint=bool(sd.get("is_exported_entrypoint", False)),
						)

		# SSA for package functions (required for LLVM lowering v1).
		def _called_funcs_in_mir(fn: M.MirFunc) -> set[FunctionId]:
			calls: set[FunctionId] = set()
			for block in fn.blocks.values():
				for instr in block.instructions:
					if isinstance(instr, M.Call):
						calls.add(instr.fn_id)
			return calls

		# Call-graph reachability: prune both src_mir and pkg_mir to only
		# functions transitively reachable from the entry point(s).  This
		# removes eagerly-instantiated templates (destructors for unused
		# types) and unreachable package functions.
		entry_name = getattr(args, "entry_fn", None) or "main"
		src_roots: set[FunctionId] = set()
		for fn_id, fn in src_mir.items():
			sig = checked_src.fn_infos_by_id.get(fn_id)
			if sig is not None and sig.signature is not None:
				sname = sig.signature.name or ""
				if sname.endswith("::" + entry_name) or sname == entry_name:
					src_roots.add(fn_id)

		# BFS through src_mir call graph from entry roots.
		src_needed: set[FunctionId] = set(src_roots)
		queue: list[FunctionId] = list(src_roots)
		while queue:
			cur = queue.pop()
			fn = src_mir.get(cur)
			if fn is None:
				continue
			for callee in _called_funcs_in_mir(fn):
				if callee in src_mir and callee not in src_needed:
					src_needed.add(callee)
					queue.append(callee)

		src_mir = {fn_id: fn for fn_id, fn in src_mir.items() if fn_id in src_needed}
		ssa_src = {fn_id: fn for fn_id, fn in ssa_src.items() if fn_id in src_needed}

		# Package MIR: include functions reachable from (pruned) source MIR.
		pkg_needed: set[FunctionId] = set()
		for fn in src_mir.values():
			for callee in _called_funcs_in_mir(fn):
				if callee in pkg_mir_all:
					pkg_needed.add(callee)

		# Expand through package-to-package calls.
		queue = list(pkg_needed)
		while queue:
			cur = queue.pop()
			fn = pkg_mir_all.get(cur)
			if fn is None:
				continue
			for callee in _called_funcs_in_mir(fn):
				if callee in pkg_mir_all and callee not in pkg_needed:
					pkg_needed.add(callee)
					queue.append(callee)

		pkg_mir: dict[FunctionId, M.MirFunc] = {}
		for fn_id in pkg_needed:
			fn = pkg_mir_all[fn_id]
			pkg_mir[fn_id] = fn

		if checked_src.type_table is not None:
			pkg_fn_infos: dict[FunctionId, FnInfo] = {}
			for fn_id, sig in pkg_sigs_by_id.items():
				if fn_id in pkg_fn_infos:
					continue
				pkg_fn_infos[fn_id] = make_fn_info(fn_id, sig, declared_can_throw=_sig_declared_can_throw(sig))
			for fn_id, fn in pkg_mir.items():
				pkg_mir[fn_id] = insert_string_arc(
					fn,
					type_table=checked_src.type_table,
					fn_infos=pkg_fn_infos,
				)

		pkg_ssa: dict[FunctionId, MirToSSA.SsaFunc] = {}
		for fn_id, fn in pkg_mir.items():
			pkg_ssa[fn_id] = MirToSSA().run(fn)

		# Merge (source wins on symbol conflicts).
		mir_all = dict(pkg_mir)
		mir_all.update(src_mir)
		ssa_all = dict(pkg_ssa)
		ssa_all.update(ssa_src)

		# FnInfos: include source + package signatures so codegen can type calls.
		fn_infos = dict(checked_src.fn_infos_by_id)
		pkg_sig_env: dict[FunctionId, FnSignature] = dict(pkg_sigs_by_id)
		all_sig_env = dict(pkg_sig_env)
		for fn_id, info in checked_src.fn_infos_by_id.items():
			if info.signature is None:
				continue
			all_sig_env[fn_id] = info.signature
		for fn_id, sig in all_sig_env.items():
			if fn_id in fn_infos:
				continue
			fn_infos[fn_id] = make_fn_info(fn_id, sig, declared_can_throw=_sig_declared_can_throw(sig))

		_build_profile = "asan" if _env_true("DRIFT_ASAN") else ("optimized" if optimized else ("debug" if debug_enabled else "default"))
		try:
			_validate_codegen_contract(
				mir_all,
				ssa_all,
				fn_infos,
				checked_src.type_table,
				debug_enabled=debug_enabled,
			)
			module = lower_module_to_llvm(
				mir_all,
				ssa_all,
				fn_infos,
				type_table=checked_src.type_table,
				module_exports=module_exports,
				rename_map={},
				argv_wrapper=None,
				word_bits=_target_word_bits(args.target_word_bits),
				debug_enabled=debug_enabled,
				provenance_git_sha=_git_short_sha(),
				provenance_build_profile=_build_profile,
			)
		except AssertionError as err:
			msg = f"internal: LLVM lowering contract failure ({err})"
			if args.json:
				print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1
		module.emit_abi_stamp()
		ir = module.render()
	else:
		ir, _checked = compile_to_llvm_ir_for_tests(
			func_hirs=func_hirs_by_id,
			signatures=signatures_by_id,
			exc_env=exception_catalog,
			entry=f"{entry_module}::{entry_name}",
			type_table=type_table,
			module_exports=module_exports,
			module_deps=module_deps,
			origin_by_fn_id=origin_by_fn_id,
			prelude_enabled=bool(args.prelude),
			enforce_entrypoint=True,
			emit_instantiation_index=args.emit_instantiation_index,
			debug_enabled=debug_enabled,
		)
		if _checked is not None and any(d.severity == "error" for d in _checked.diagnostics):
			if args.json:
				payload = {
					"exit_code": 1,
					"diagnostics": [_diag_to_json(d, "typecheck", source_path) for d in _checked.diagnostics],
				}
				print(json.dumps(payload))
			else:
				for d in _checked.diagnostics:
					loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
					print(f"{_source_label()}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
			return 1
		if args.emit_instantiation_index is not None and not args.emit_instantiation_index.exists():
			compile_stubbed_funcs(
				func_hirs=func_hirs_by_id,
				signatures=signatures_by_id,
				exc_env=exception_catalog,
				module_exports=module_exports,
				module_deps=module_deps,
				origin_by_fn_id=origin_by_fn_id,
				type_table=type_table,
				prelude_enabled=bool(args.prelude),
				emit_instantiation_index=args.emit_instantiation_index,
				enforce_entrypoint=True,
				entry_module=entry_module,
				entry_name=entry_name,
			)

	# Emit IR if requested.
	if args.emit_ir is not None:
		args.emit_ir.parent.mkdir(parents=True, exist_ok=True)
		args.emit_ir.write_text(ir)

	# If only IR emission requested, we are done.
	if args.output is None:
		if args.json:
			print(json.dumps({"exit_code": 0, "diagnostics": []}))
		return 0

	clang = shutil.which("clang-15") or shutil.which("clang")
	if clang is None:
		msg = "clang not available for code generation"
		if args.json:
			print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
		else:
			print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
		return 1

		if package_id is None:
			msg = "--emit-package requires a non-empty package id"
			if args.json:
				print(
					json.dumps(
						{
							"exit_code": 1,
							"diagnostics": [
								{
									"phase": "package",
									"message": msg,
									"severity": "error",
									"file": "<source>",
									"line": None,
									"column": None,
								}
							],
						}
					)
				)
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1

	args.output.parent.mkdir(parents=True, exist_ok=True)
	ir_path = args.output.with_suffix(".ll")
	ir_path.write_text(ir)

	runtime_sources = [str(p) for p in get_runtime_sources(ROOT)]
	runtime_root = (ROOT / "lang" / "language_runtime").resolve()
	compiler_infra_root = (ROOT / "lang" / "compiler_infra").resolve()
	search_dirs = [
		Path("/lib"),
		Path("/lib64"),
		Path("/usr/lib"),
		Path("/usr/lib64"),
		Path("/lib/x86_64-linux-gnu"),
		Path("/usr/lib/x86_64-linux-gnu"),
	]
	def _link_flags_for_lib(name: str) -> list[str]:
		if not find_library(name):
			return []
		for d in search_dirs:
			if (d / f"lib{name}.so").exists():
				return [f"-l{name}"]
		return []
	link_libs = _link_flags_for_lib("dw") + _link_flags_for_lib("unwind") + _link_flags_for_lib("unwind-x86_64") + _link_flags_for_lib("elf")
	def _select_linker() -> str:
		if args.linker == "ld":
			return "ld"
		if args.linker == "gold":
			return "gold"
		if shutil.which("ld.gold") is not None:
			return "gold"
		return "ld"

	def _linker_supports_gdb_index(use_linker: str) -> bool:
		if use_linker == "gold":
			ld = shutil.which("ld.gold")
			if ld is None:
				return False
			try:
				res = subprocess.run([ld, "--help"], capture_output=True, text=True, cwd=ROOT)
			except Exception:
				return False
			if res.returncode != 0:
				return False
			return "--gdb-index" in res.stdout
		return False

	use_linker = _select_linker()
	linker_flags = ["-fuse-ld=gold"] if use_linker == "gold" else []
	gdb_index_flag = ["-Wl,--gdb-index"] if debug_enabled and _linker_supports_gdb_index(use_linker) else []
	asan_enabled = _env_true("DRIFT_ASAN")
	asan_flags = ["-fsanitize=address", "-g"] if asan_enabled else []
	opt_flags = ["-O2"] if optimized else []
	runtime_archive: str | None = None
	rt_mode = runtime_archive_mode()
	if rt_mode == "archive":
		variant = runtime_archive_variant(
			debug_enabled=debug_enabled,
			asan_enabled=asan_enabled,
			alloc_track_enabled=False,
			optimized=optimized,
		)
		try:
			archive_path = build_runtime_archive(ROOT, clang=clang, variant=variant)
		except Exception as ex:
			msg = f"runtime archive build failed in archive mode: {ex}"
			if args.json:
				print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1
		runtime_archive = str(archive_path)

	if debug_enabled:
		ir_obj = args.output.with_suffix(".ir.o")
		ir_compile_cmd = [
			clang,
			*linker_flags,
			*asan_flags,
			*opt_flags,
			"-c",
			"-x",
			"ir",
			str(ir_path),
			"-g",
			"-o",
			str(ir_obj),
		]
		ir_compile = subprocess.run(ir_compile_cmd, capture_output=True, text=True, cwd=ROOT)
		if ir_compile.returncode != 0:
			msg = f"clang failed: {ir_compile.stderr.strip()}"
			if args.json:
				print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1
		rt_inputs: list[str]
		if rt_mode == "archive":
			rt_inputs = [runtime_archive]
		else:
			rt_dir = args.output.parent / "runtime_objs"
			rt_dir.mkdir(parents=True, exist_ok=True)
			rt_objs: list[str] = []
			for src in runtime_sources:
				src_path = Path(src)
				obj_path = rt_dir / (src_path.stem + ".o")
				rt_compile_cmd = [
					clang,
					*linker_flags,
					*asan_flags,
					*opt_flags,
					"-c",
					"-x",
					"c",
					str(src_path),
					"-g0",
					"-I",
					str(runtime_root),
					"-I",
					str(compiler_infra_root),
					"-o",
					str(obj_path),
				]
				rt_compile = subprocess.run(rt_compile_cmd, capture_output=True, text=True, cwd=ROOT)
				if rt_compile.returncode != 0:
					msg = f"clang failed: {rt_compile.stderr.strip()}"
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
					else:
						print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1
				rt_objs.append(str(obj_path))
			rt_inputs = rt_objs
		link_cmd = [
			clang,
			*linker_flags,
			*asan_flags,
			*opt_flags,
			str(ir_obj),
			*rt_inputs,
			*link_libs,
			*gdb_index_flag,
			"-Wl,--as-needed",
			"-o",
			str(args.output),
		]
	else:
		if rt_mode == "archive":
			link_cmd = [
				clang,
				*linker_flags,
				*asan_flags,
				*opt_flags,
				"-x",
				"ir",
				str(ir_path),
				"-x",
				"none",
				runtime_archive,
				*link_libs,
				"-Wl,--as-needed",
				"-o",
				str(args.output),
			]
		else:
			link_cmd = [
				clang,
				*linker_flags,
				*asan_flags,
				*opt_flags,
				"-x",
				"ir",
				str(ir_path),
				"-x",
				"c",
				"-I",
				str(runtime_root),
				"-I",
				str(compiler_infra_root),
				*runtime_sources,
				*link_libs,
				"-Wl,--as-needed",
				"-o",
				str(args.output),
			]
	print("[driftc] link:", " ".join(link_cmd), file=sys.stderr)
	link_res = subprocess.run(link_cmd, capture_output=True, text=True, cwd=ROOT)
	if link_res.returncode != 0:
		msg = f"clang failed: {link_res.stderr.strip()}"
		abi_hint = ""
		if "__drift_rt_abi_version_" in link_res.stderr:
			from lang.driftc.driftc_versions import DRIFT_RT_ABI_VERSION as _abi_ver
			abi_hint = f"\nhint: driftc targets runtime ABI v{_abi_ver}; linked runtime provides a different ABI. Rebuild runtime/std artifacts (just runtime-libs)."
		if args.json:
			print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg + abi_hint, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
		else:
			print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			if abi_hint:
				print(abi_hint, file=sys.stderr)
		return 1

	if args.json:
		print(json.dumps({"exit_code": 0, "diagnostics": []}))
	return 0


if __name__ == "__main__":
	sys.exit(main())
