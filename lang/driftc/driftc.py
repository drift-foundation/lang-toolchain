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
import functools
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


def _toolchain_git_sha() -> str:
	"""Return the toolchain source commit: build-time stamp if available, else runtime git."""
	from lang.versions import DRIFTC_GIT_SHA
	return DRIFTC_GIT_SHA or _git_short_sha()


def _version_string() -> str:
	"""Build the driftc --version output."""
	from lang.driftc.driftc_versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
	git_sha = _toolchain_git_sha()
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
from lang.driftc.stage2.mir_lowering_error import MirLoweringError
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
	STAGE3_FAT_ARC_ACTIVE,
)
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.function_id import (
	FunctionId,
	function_id_from_obj,
	function_id_to_obj,
	function_symbol,
	method_wrapper_id,
	parse_function_symbol,
)
from lang.driftc.traits.enforce import collect_used_type_keys, enforce_struct_requires, enforce_fn_requires
from lang.driftc.traits.linked_world import build_require_env, link_trait_worlds, LinkedWorld, RequireEnv
from lang.driftc.traits.world import TypeKey, TraitKey, type_key_from_typeid
from lang.driftc.traits.solver import Env as TraitEnv, ProofStatus, prove_is
from lang.codegen.llvm import lower_module_to_llvm, ENTRY_WRAPPER_IMPLICIT_DEPS
from lang.codegen.llvm.test_utils import host_word_bits
from lang.language_runtime import (
	build_runtime_archive,
	get_runtime_sources,
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
	decode_hir_funcs,
	decode_generic_templates,
	decode_trait_expr,
	decode_type_expr,
	compute_template_decl_fingerprint,
	compute_template_decl_fingerprint_debug,
	encode_generic_templates,
	encode_hir_funcs,
	encode_module_payload_v0,
	encode_span,
	encode_trait_expr,
	encode_type_expr,
	type_table_fingerprint,
)
from lang.driftc.packages.type_table_link_v0 import decode_type_table_obj, import_type_tables_and_build_typeid_maps
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
	if resolved_nom is not None and td.param_types:
		canon_args = [_canonicalize_forward_nominal_type_id(type_table, a, _seen=seen) for a in td.param_types]
		try:
			if resolved_nom in type_table.variant_schemas:
				return type_table.ensure_variant_instantiated(resolved_nom, canon_args)
			if resolved_nom in type_table.struct_bases:
				return type_table.ensure_struct_instantiated(resolved_nom, canon_args)
			if resolved_nom in type_table.interface_bases:
				return type_table.ensure_interface_instantiated(resolved_nom, canon_args)
		except ValueError:
			return ty_id
		return ty_id
	return resolved_nom if resolved_nom is not None else ty_id


# ---------------------------------------------------------------------------
# Stage 8.2: canonical TypeKey for host TypeTable TypeIds.
# ---------------------------------------------------------------------------

# Enabled by DRIFT_DEBUG_TYPEXPR_RESOLVE=1.  When active, consumer paths
# resolve signatures via BOTH TypeExpr and tid_map and compare results using
# canonical semantic identity rather than raw integer equality.
_TYPEXPR_DEBUG = os.environ.get("DRIFT_DEBUG_TYPEXPR_RESOLVE") == "1"

# Counters for assertion-mode diagnostics.
_typexpr_checks = 0
_typexpr_integer_divergences = 0
_typexpr_canonical_mismatches = 0

if _TYPEXPR_DEBUG:
	import atexit

	def _typexpr_summary() -> None:
		if _typexpr_checks == 0:
			return
		print(
			f"\nTYPEXPR_RESOLVE summary: {_typexpr_checks} checks, "
			f"{_typexpr_integer_divergences} integer-divergences, "
			f"{_typexpr_canonical_mismatches} canonical-mismatches",
			file=sys.stderr,
		)
		if _typexpr_canonical_mismatches > 0:
			print(
				"TYPEXPR_RESOLVE: CANONICAL MISMATCHES DETECTED — these are bugs.",
				file=sys.stderr,
			)

	atexit.register(_typexpr_summary)


def _host_type_key(tid: TypeId, tt: TypeTable, _memo: dict[TypeId, object] | None = None) -> object:
	"""Compute a canonical structural key for a host-TypeTable TypeId.

	Mirrors the linker's key_for_tid() but operates on the host TypeTable.
	Returns a nested tuple that two TypeIds can be compared against to determine
	semantic identity independent of interning order.
	"""
	if _memo is None:
		_memo = {}
	if tid in _memo:
		return _memo[tid]
	# Guard against infinite recursion.
	_memo[tid] = ("cycle", tid)
	try:
		td = tt.get(tid)
	except (KeyError, IndexError):
		return ("unknown",)
	k = td.kind
	name = td.name
	mid = td.module_id or ""

	# Resolve package identity for module-scoped types.
	pkg_id = tt._package_for_module(td.module_id) if mid else ""

	if k is TypeKind.VOID:
		r = ("builtin", "VOID", "Void")
	elif k is TypeKind.ERROR:
		r = ("builtin", "ERROR", "Error")
	elif k is TypeKind.DIAGNOSTICVALUE:
		r = ("builtin", "DIAGNOSTICVALUE", "DiagnosticValue")
	elif k is TypeKind.UNKNOWN:
		r = ("unknown",)
	elif k is TypeKind.SCALAR:
		r = ("builtin", "SCALAR", name) if mid == "" else ("nominal", "SCALAR", pkg_id, mid, name)
	elif k is TypeKind.TYPEVAR:
		pid = td.type_param_id
		if pid is not None:
			# TYPEVAR defs don't carry module_id; derive package from the
			# owner function's module (mirrors linker's key_for_tid).
			owner_pkg = tt._package_for_module(pid.owner.module) if pid.owner.module else ""
			r = ("typevar", owner_pkg, pid.owner.module, pid.owner.name, pid.owner.ordinal, pid.index)
		else:
			r = ("typevar_unnamed", name)
	elif k is TypeKind.ARRAY:
		r = ("array", _host_type_key(td.param_types[0], tt, _memo)) if td.param_types else ("array",)
	elif k is TypeKind.REF:
		r = ("ref", bool(td.ref_mut), _host_type_key(td.param_types[0], tt, _memo)) if td.param_types else ("ref",)
	elif k is TypeKind.RAW_PTR:
		r = ("rawptr", _host_type_key(td.param_types[0], tt, _memo)) if td.param_types else ("rawptr",)
	elif k is TypeKind.FNRESULT:
		subs = tuple(_host_type_key(p, tt, _memo) for p in td.param_types)
		r = ("fnresult", subs)
	elif k is TypeKind.FUNCTION:
		subs = tuple(_host_type_key(p, tt, _memo) for p in td.param_types)
		r = ("function", bool(td.fn_throws), subs)
	elif k in (TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.INTERFACE):
		# Check for generic instantiation.
		inst = None
		if k is TypeKind.STRUCT:
			inst = tt.get_struct_instance(tid)
		elif k is TypeKind.VARIANT:
			inst = tt.variant_instances.get(tid)
		elif k is TypeKind.INTERFACE:
			inst = tt.interface_instances.get(tid)
		if inst is not None and inst.type_args:
			base_td = tt.get(inst.base_id)
			base_mid = base_td.module_id or ""
			base_pkg = tt._package_for_module(base_td.module_id) if base_mid else ""
			base_key = ("nominal", k.name, base_pkg, base_mid, base_td.name)
			arg_keys = tuple(_host_type_key(a, tt, _memo) for a in inst.type_args)
			r = ("inst", base_key, arg_keys)
		else:
			r = ("nominal", k.name, pkg_id, mid, name)
	elif k is TypeKind.FORWARD_NOMINAL:
		subs = tuple(_host_type_key(p, tt, _memo) for p in td.param_types)
		r = ("forward_nominal", pkg_id, mid, name, subs)
	else:
		subs = tuple(_host_type_key(p, tt, _memo) for p in td.param_types)
		r = ("other", k.name, name, subs)
	_memo[tid] = r
	return r


def _assert_typexpr_tid_match(
	label: str,
	typexpr_tid: TypeId | None,
	tidmap_tid: TypeId | None,
	tt: TypeTable,
) -> None:
	"""Compare TypeExpr-resolved and tid_map-resolved TypeIds.

	Called in assertion mode (DRIFT_DEBUG_TYPEXPR_RESOLVE=1).
	- Identical integers: pass (fast path).
	- Different integers, same canonical key: log, don't fail.
	- Different canonical keys: fail hard.
	"""
	global _typexpr_checks, _typexpr_integer_divergences, _typexpr_canonical_mismatches
	_typexpr_checks += 1
	if typexpr_tid == tidmap_tid:
		return
	if typexpr_tid is None or tidmap_tid is None:
		_typexpr_canonical_mismatches += 1
		msg = (
			f"TYPEXPR_RESOLVE MISMATCH [{label}]: "
			f"typexpr={typexpr_tid} tidmap={tidmap_tid} (one is None)"
		)
		print(msg, file=sys.stderr)
		raise AssertionError(msg)
	key_a = _host_type_key(typexpr_tid, tt)
	key_b = _host_type_key(tidmap_tid, tt)
	if key_a == key_b:
		_typexpr_integer_divergences += 1
		print(
			f"TYPEXPR_RESOLVE integer-divergence [{label}]: "
			f"typexpr={typexpr_tid} tidmap={tidmap_tid} "
			f"canonical={key_a}",
			file=sys.stderr,
		)
	else:
		_typexpr_canonical_mismatches += 1
		msg = (
			f"TYPEXPR_RESOLVE CANONICAL MISMATCH [{label}]: "
			f"typexpr={typexpr_tid} tidmap={tidmap_tid} "
			f"typexpr_key={key_a} tidmap_key={key_b}"
		)
		print(msg, file=sys.stderr)
		raise AssertionError(msg)


def _canonicalize_mir_type_ids(mir_funcs_by_id: Mapping[FunctionId, M.MirFunc], type_table: TypeTable) -> None:
	"""Resolve surviving FORWARD_NOMINAL TypeIds in MIR local_types and instructions.

	Signature and struct-field sweeps were removed (proven no-ops with early
	linking).  MIR local_types still contain FORWARD_NOMINALs for certain
	alias/variant patterns where the type-checker infers a TypeId before the
	concrete declaration is visible.
	"""
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
	# `Copy` canonically lives in `std.core.copy` (re-exported by
	# `std.core`).  Production stdlib registers it at the submodule;
	# minimal test stubs that inline `pub trait Copy` inside
	# `module std.core` register it at `std.core` directly — accept
	# either.
	copy_key = _find_trait_key(linked_world.global_world, module="std.core.copy", name="Copy")
	if copy_key is None:
		copy_key = _find_trait_key(linked_world.global_world, module="std.core", name="Copy")
	if copy_key is None:
		std_modules = {"std.core", "std.core.copy", "std.iter", "std.containers", "std.algo"}
		if any(mod in std_modules for mod in linked_world.trait_worlds.keys()):
			raise ValueError("stdlib missing std.core.copy.Copy trait metadata")
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
		# Raw pointers are inherently Copy — they are plain machine-word
		# addresses with no ownership semantics.
		if td.kind is TypeKind.RAW_PTR:
			return True
		# Primitive scalar types are inherently Copy (except String which is
		# reference-counted). This covers built-in numeric types like Int32,
		# Uint32, etc. that may not have explicit Copy trait implementations.
		if td.kind is TypeKind.SCALAR and td.name != "String":
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


def _install_share_query(type_table: TypeTable, linked_world: LinkedWorld) -> None:
	"""Install a Share-trait query hook on `type_table`.

	Mirror of `_install_destructible_query`.  Used by the type
	checker's focused `E-CAPTURE-SHARE-NOT-SHARE` diagnostic on
	`captures(share x)` — the diagnostic needs a fast Share-impl
	predicate to fire BEFORE the synthesized `Share::share(&x)`
	HCall is dispatched to the call_resolver, so the user sees the
	capture-specific message rather than a generic trait-resolution
	error.  Routes through the trait prover.
	"""
	share_key = _find_trait_key(linked_world.global_world, module="std.core.shareable", name="Share")
	if share_key is None:
		# Stdlib trait metadata not present (e.g. test stubs).  Fall
		# back to "unknown" — the checker treats unknown as "no
		# diagnostic" so we don't false-fire.
		def _fallback_share(_tid: int) -> bool | None:
			return None
		type_table.set_share_query(_fallback_share, allow_fallback=True)
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

	def _query_share(tid: int) -> bool | None:
		try:
			subject = type_key_from_typeid(type_table, tid)
		except Exception:
			return None
		res = prove_is(world, env, {}, subject, share_key)
		if res.status is ProofStatus.PROVED:
			return True
		if res.status is ProofStatus.REFUTED:
			return False
		return None

	type_table.set_share_query(_query_share, allow_fallback=False)


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


def _scan_destructible_impls_by_name(
	module_exports: Mapping[str, dict[str, object]] | None,
	external_impl_metas: list | None = None,
) -> dict[TypeId, FunctionId]:
	"""Scan module_exports and external_impl_metas for Destructible impls
	using trait_key name+module matching.  Does not need linked_world.

	Returns a TypeId→FunctionId map of destroy functions.
	"""
	result: dict[TypeId, FunctionId] = {}
	sources: list = []
	if module_exports is not None:
		for exp in module_exports.values():
			if isinstance(exp, dict):
				for impl in (exp.get("impls") or []):
					if isinstance(impl, ImplMeta):
						sources.append(impl)
	if external_impl_metas:
		for impl in external_impl_metas:
			if isinstance(impl, ImplMeta):
				sources.append(impl)
	for impl in sources:
		tk = getattr(impl, "trait_key", None)
		if tk is None or getattr(tk, "name", "") != "Destructible":
			continue
		if getattr(tk, "module", "") != "std.core":
			continue
		tid = getattr(impl, "target_type_id", None)
		if not isinstance(tid, int):
			continue
		for method in impl.methods:
			if method.name == "destroy":
				result[tid] = method.fn_id
				break
	return result


def _dump_type_table_queries_if_enabled(type_table: TypeTable | None) -> None:
	"""Diagnostic: when `DRIFT_DUMP_TYPE_QUERIES=1`, after the linked
	world + destructor_fns are installed, dump
	`(copy_status, has_drop, is_destructible, is_bitcopy)` for every
	nominal type the type-table knows about (plus any generic
	instantiation's resolved type-arg names).  One JSON record per
	type, prefixed with `[drift:type-query]`.

	Off by default; useful when comparing source-loaded vs
	package-loaded stdlib for type-link / trait-prover divergence.
	Originated as Vector-3/4 instrumentation for the whole-scrutinee
	migration boundary-bug investigation (2026-04-24); kept as a
	standing diagnostic since the surface (type-link canonicalization
	across the .dmp boundary) is the kind of thing that needs ongoing
	visibility.
	"""
	import os
	if not os.environ.get("DRIFT_DUMP_TYPE_QUERIES") or type_table is None:
		return
	import json
	import sys
	from lang.driftc.core.types_core import TypeKind
	dump_kinds = (
		TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.SCALAR, TypeKind.INTERFACE,
		TypeKind.ERROR, TypeKind.ARRAY, TypeKind.RAW_PTR, TypeKind.FNRESULT,
		TypeKind.DIAGNOSTICVALUE,
	)
	def _safe(fn):
		try:
			return fn()
		except Exception as e:
			return f"<err:{type(e).__name__}>"
	def _arg_name(tid):
		try:
			ad = type_table.get(tid)
			return getattr(ad, "name", None) or ad.kind.name
		except Exception:
			return f"<tid:{tid}>"
	for tid, td in sorted(type_table._defs.items()):
		kind = getattr(td, "kind", None)
		if kind not in dump_kinds:
			continue
		type_args: list[int] = []
		try:
			inst = type_table.get_variant_instance(tid)
		except Exception:
			inst = None
		if inst is None:
			try:
				inst = type_table.get_struct_instance(tid)
			except Exception:
				inst = None
		if inst is not None:
			type_args = [int(a) for a in getattr(inst, "type_args", []) or []]
		# Compute the post-link DropPolicy too — that's the
		# authoritative drop-decision surface for cleanup_authoring
		# and any other consumer that asks "should this type be
		# dropped".  Bug 2 (compute_drop_policy short-circuit on
		# Copy && has_drop, e.g. String / Optional<String>) is pinned
		# via these fields.
		from lang.driftc.stage2.drop_policy_compute import compute_drop_policy as _cdp
		policy = _safe(lambda: _cdp(type_table, tid))
		payload = {
			"channel": "type-query",
			"tid": int(tid),
			"kind": kind.name,
			"name": getattr(td, "name", None),
			"module": getattr(td, "module_id", None),
			"type_args": type_args,
			"type_arg_names": [_arg_name(a) for a in type_args],
			"copy_status": _safe(lambda: type_table.copy_status(tid)),
			"has_drop": _safe(lambda: bool(type_table.has_drop(tid))),
			"is_destructible": _safe(lambda: bool(type_table.is_destructible(tid))),
			"is_bitcopy": _safe(lambda: bool(type_table.is_bitcopy(tid))),
			"policy_needs_drop": (bool(getattr(policy, "needs_drop", None)) if hasattr(policy, "needs_drop") else None),
			"policy_is_cheap_copy": (bool(getattr(policy, "is_cheap_copy", None)) if hasattr(policy, "is_cheap_copy") else None),
			"policy_has_structural_drop": (bool(getattr(policy, "has_structural_drop", None)) if hasattr(policy, "has_structural_drop") else None),
		}
		sys.stderr.write("[drift:type-query] " + json.dumps(payload, sort_keys=True) + "\n")


def _install_destructor_fns(
	type_table: TypeTable | None,
	linked_world: LinkedWorld | None,
	module_exports: Mapping[str, dict[str, object]] | None,
	external_impl_metas: list | None = None,
) -> None:
	if type_table is None or linked_world is None:
		return
	destructible_key = _find_trait_key(linked_world.global_world, module="std.core", name="Destructible")
	if destructible_key is None:
		return
	destructor_fns: dict[TypeId, FunctionId] = {}
	# Scan module_exports for Destructible impls (source-compiled path).
	if module_exports is not None:
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
				# Do NOT skip typevar types — we need generic destructors like
				# Arc<T>::destroy registered so the name+module fallback in
				# has_drop can match cross-package instantiations like Arc<AtomicBool>.
				for method in impl.methods:
					if method.name != "destroy":
						continue
					destructor_fns[target_type_id] = method.fn_id
	# Also scan external_impl_metas (package-consumed path).
	# When stdlib is loaded as a package, Destructible impls are in
	# external_impl_metas, not module_exports.
	if external_impl_metas:
		for ei in external_impl_metas:
			if not isinstance(ei, ImplMeta):
				continue
			if ei.trait_key != destructible_key:
				continue
			ei_tid = getattr(ei, "target_type_id", None)
			if not isinstance(ei_tid, int):
				continue
			# Do NOT skip typevar types — same reasoning as above.
			for method in ei.methods:
				if method.name != "destroy":
					continue
				destructor_fns[ei_tid] = method.fn_id
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
		_install_share_query(type_table, linked_world)
	default_package = getattr(type_table, "package_id", None)
	module_packages = getattr(type_table, "module_packages", None)
	return linked_world, build_require_env(
		linked_world,
		default_package=default_package,
		module_packages=module_packages,
	)


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
		"types": {"structs": [], "variants": [], "exceptions": [], "interfaces": [], "aliases": []},
		"consts": [],
		"traits": [],
		"reexports": {
			"types": {"structs": {}, "variants": {}, "exceptions": {}, "interfaces": {}, "aliases": {}},
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
	# Uses the shared iterative HIR walker from `stage1/node_ids.py` —
	# the dedup consolidated this and three other local copies of the
	# same pattern.  The walker preserves declaration-order pre-order
	# visitation and `id(obj)` dedup.
	from lang.driftc.stage1.node_ids import iter_hir_walk
	found: dict[int, H.HExpr] = {}
	for obj in iter_hir_walk(root):
		if isinstance(obj, (H.HCall, H.HMethodCall, H.HInvoke)):
			found[getattr(obj, "node_id", -1)] = obj
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


# Arc runtime-boundary intrinsic kinds whose CallInfo may legitimately
# carry a template-level signature (typevar params / return type) after
# typecheck.  Each of these kinds routes the runtime call through a
# `_arc_*_impl<T>` helper instantiated per call site; the helper's OWN
# monomorphization satisfies the generic-survived invariant, not the
# intrinsic call site itself.
#
# Narrowly scoped on purpose: every OTHER intrinsic kind is still
# subject to the generic-survived check, so an accidental template-sig
# leak on a non-Arc intrinsic (e.g. a `RAW_PTR_AT_REF` callsite that
# forgot to substitute) still surfaces `E_INTERNAL_GENERIC_CALLINFO`.
# Do NOT generalize this exemption to `CallTargetKind.INTRINSIC is X`
# — the invariant is "Arc bridge intrinsics may carry template sigs,"
# not "all intrinsics may carry template sigs."
_ARC_BRIDGE_INTRINSIC_KINDS: frozenset["IntrinsicKind"] = frozenset({
	IntrinsicKind.ARC_CLONE,
	IntrinsicKind.ARC_GET,
	IntrinsicKind.ARC_DESTROY,
	IntrinsicKind.ARC_AS_INTERFACE,
})


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
		if (
			info.target.kind is CallTargetKind.INTRINSIC
			and info.target.intrinsic in _ARC_BRIDGE_INTRINSIC_KINDS
		):
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


@dataclass
class CompilationUnit:
	"""Input bundle for the shared codegen entry point (_emit_codegen)."""
	mir_funcs: dict[FunctionId, M.MirFunc]
	ssa_funcs: dict[FunctionId, MirToSSA.SsaFunc]
	fn_infos: dict[FunctionId, FnInfo]
	type_table: TypeTable
	rename_map: dict[FunctionId, str]
	entry_id: FunctionId | None
	wrapper_dep_flags: dict[str, bool]


def _resolve_destroy_fn_for_type(
	drop_ty: int,
	fn_infos: Mapping[FunctionId, FnInfo],
	pkg_sigs: Mapping[FunctionId, FnSignature] | None = None,
) -> FunctionId | None:
	"""Find the Destructible::destroy impl fn_id for a given TypeId."""
	for fn_id, info in fn_infos.items():
		sig = info.signature
		if sig is None:
			continue
		if sig.method_name != "destroy":
			continue
		if sig.impl_target_type_id == drop_ty:
			return fn_id
	if pkg_sigs is not None:
		for fn_id, sig in pkg_sigs.items():
			if sig.method_name != "destroy":
				continue
			if sig.impl_target_type_id == drop_ty:
				return fn_id
	return None


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
				if isinstance(instr, M.ConstructIface) and instr.fn_ref.fn_id not in fn_infos:
					raise AssertionError(
						f"codegen contract: unknown ConstructIface target {function_symbol(instr.fn_ref.fn_id)} in {function_symbol(fn.fn_id)}"
					)
	# Placeholder for future debug-info contract hardening.
	_ = debug_enabled


def _emit_codegen(
	unit: CompilationUnit,
	*,
	module_exports: Mapping[str, dict[str, object]] | None = None,
	word_bits: int,
	debug_enabled: bool = True,
	provenance_git_sha: str = "",
	provenance_build_profile: str = "default",
) -> str:
	"""Shared codegen entry: validate contract, lower to LLVM IR, emit wrappers, render."""
	assert unit.type_table is not None, "_emit_codegen: CompilationUnit.type_table is None"
	_validate_codegen_contract(unit.mir_funcs, unit.ssa_funcs, unit.fn_infos, unit.type_table, debug_enabled=debug_enabled)
	# K31: detect main(argv: Array<String>) and use argv_entry_wrapper.
	argv_wrapper: str | None = None
	if unit.entry_id is not None:
		entry_info = unit.fn_infos.get(unit.entry_id)
		if entry_info and entry_info.signature and entry_info.signature.param_type_ids and unit.type_table is not None:
			if len(entry_info.signature.param_type_ids) == 1:
				param_ty = entry_info.signature.param_type_ids[0]
				td = unit.type_table.get(param_ty)
				if td.kind.name == "ARRAY" and td.param_types:
					elem_td = unit.type_table.get(td.param_types[0])
					if elem_td.name == "String":
						argv_wrapper = unit.rename_map.get(unit.entry_id, function_symbol(unit.entry_id))
	module = lower_module_to_llvm(
		unit.mir_funcs,
		unit.ssa_funcs,
		unit.fn_infos,
		type_table=unit.type_table,
		module_exports=module_exports,
		rename_map=unit.rename_map,
		argv_wrapper=argv_wrapper,
		word_bits=word_bits,
		debug_enabled=debug_enabled,
		provenance_git_sha=provenance_git_sha,
		provenance_build_profile=provenance_build_profile,
	)
	module.emit_abi_stamp()
	if unit.entry_id is not None and argv_wrapper is None:
		entry_sym = unit.rename_map.get(unit.entry_id, function_symbol(unit.entry_id))
		module.emit_entry_wrapper(entry_sym, **unit.wrapper_dep_flags)
	return module.render()


def _called_funcs_in_mir(fn: M.MirFunc) -> set[FunctionId]:
	"""Return the set of function IDs directly referenced by MIR instructions."""
	calls: set[FunctionId] = set()
	for block in fn.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.Call):
				calls.add(instr.fn_id)
			elif isinstance(instr, M.ConstructIface):
				calls.add(instr.fn_ref.fn_id)
			elif isinstance(instr, M.FnPtrConst):
				calls.add(instr.fn_ref.fn_id)
	return calls


def _discover_and_synthesize_wrappers(
	*,
	reachable: set[FunctionId],
	mir_pool: dict[FunctionId, M.MirFunc],
	ssa_pool: dict[FunctionId, MirToSSA.SsaFunc],
	wrapper_target_by_id: dict[FunctionId, FunctionId],
	wrapper_sigs: dict[FunctionId, FnSignature],
	fn_infos: Mapping[FunctionId, FnInfo],
	signatures_by_id: Mapping[FunctionId, FnSignature],
	type_table: TypeTable,
) -> None:
	"""Discover wrapper references in reachable MIR and synthesize missing bodies.

	Shared between _build_package_consumer_unit and the Option B direct
	CompilationUnit path.  Iterates to fixpoint: synthesized wrapper bodies
	may themselves reference further functions that need to be included.
	"""
	wrappers_needed: set[FunctionId] = set()
	changed = True
	while changed:
		changed = False
		for fid in list(reachable):
			fn = mir_pool.get(fid)
			if fn is None:
				continue
			for callee in _called_funcs_in_mir(fn):
				if callee in mir_pool and callee not in reachable:
					reachable.add(callee)
					changed = True
				elif callee not in mir_pool and callee in wrapper_target_by_id and callee not in wrappers_needed:
					wrappers_needed.add(callee)
					changed = True
	# Synthesize MIR bodies for discovered wrappers.
	for wrap_id in wrappers_needed:
		if wrap_id in mir_pool:
			continue
		wrap_sig = wrapper_sigs.get(wrap_id)
		if wrap_sig is None or wrap_sig.param_type_ids is None:
			continue
		param_names = list(wrap_sig.param_names or [])
		if len(param_names) != len(wrap_sig.param_type_ids):
			param_names = [f"p{i}" for i in range(len(wrap_sig.param_type_ids))]
		target_id = wrapper_target_by_id[wrap_id]
		target_sig = signatures_by_id.get(target_id)
		if target_sig is None:
			_ti = fn_infos.get(target_id)
			if _ti is not None:
				target_sig = _ti.signature
		target_ret = target_sig.return_type_id if target_sig is not None else wrap_sig.return_type_id
		builder = make_builder(wrap_id)
		builder.func.params = list(param_names)
		call_dest: M.ValueId | None
		if type_table.is_void(target_ret):
			call_dest = None
		else:
			call_dest = builder.new_temp()
		builder.emit(M.Call(dest=call_dest, fn_id=target_id, args=param_names, can_throw=False))
		ok_dest = builder.new_temp()
		builder.emit(M.ConstructResultOk(dest=ok_dest, value=call_dest))
		builder.set_terminator(M.Return(value=ok_dest))
		mir_pool[wrap_id] = builder.func
		ssa_pool[wrap_id] = MirToSSA().run(builder.func)
		reachable.add(wrap_id)
		# Also ensure the target is reachable.
		if target_id in mir_pool and target_id not in reachable:
			reachable.add(target_id)


def _synthesize_fat_arc_destructor_wrappers(
	*,
	type_table: TypeTable,
	mir_pool: dict[FunctionId, M.MirFunc],
	ssa_pool: dict[FunctionId, MirToSSA.SsaFunc],
	fn_infos: dict[FunctionId, FnInfo],
	signatures_by_id: dict[FunctionId, FnSignature],
	external_signatures_by_id: Mapping[FunctionId, FnSignature],
	reachable: set[FunctionId],
) -> int:
	"""Stage 3 fat-Arc destructor synthesis (ABI 10).

	For each `Arc<I>` instance that appears as a `DropValue.ty` in
	reachable MIR and has the fat `{ctrl, data, vtable}` layout, mint
	a compiler-owned per-I wrapper:

	    fn _arc_fat_destroy_wrapper__<inst>(var self: Arc<I>) nothrow -> Void {
	        ctrl = self.ctrl         // StructGetField(field_index=0)
	        _arc_fat_drop_via_ctrl(ctrl)
	        return
	    }

	The wrapper's fn_id is written into `type_table.destructor_fns[ty_id]`,
	superseding any entry from the initial Destructible scan (which skips
	fat Arc<I> — see `is_arc_fat_layout_instance` guards in the scan +
	K39 rescan).  The thin `_arc_destroy_impl<I>` template is never
	queued, monomorphized, or referenced for fat receivers.

	Returns the number of wrappers registered.  When the activation flag
	is off, no instance carries the fat layout and this pass is a
	no-op.
	"""
	# Resolve the Slice 1 non-generic drop primitive.  It lives under
	# `std.concurrent` and is nothrow/free-function.  Check the main
	# sig table first; fall back to external (package-consumer shape).
	fat_drop_fn_id: FunctionId | None = None
	for fid, sig in signatures_by_id.items():
		if fid.module == "std.concurrent" and fid.name == "_arc_fat_drop_via_ctrl" and not bool(getattr(sig, "is_method", False)):
			fat_drop_fn_id = fid
			break
	if fat_drop_fn_id is None:
		for fid, sig in external_signatures_by_id.items():
			if fid.module == "std.concurrent" and fid.name == "_arc_fat_drop_via_ctrl" and not bool(getattr(sig, "is_method", False)):
				fat_drop_fn_id = fid
				break

	ptr_byte_ty = type_table.new_ptr(type_table.ensure_byte())
	void_ty = type_table.ensure_void()
	destructor_fns: dict[int, FunctionId] = dict(getattr(type_table, "destructor_fns", None) or {})
	# Scope the synthesis to fat Arc<I> instances that actually need a
	# destructor — the types reachable (directly or transitively via
	# struct fields, variant arm payloads, or container element types)
	# from any `DropValue.ty` in reachable MIR.  Iterating all of
	# `struct_instances` is too broad: the type-table picks up fat
	# Arc<I> shapes from imported-but-unused stdlib surfaces (e.g.
	# `LoggerConfig.resolver` when a consumer loads std.log signatures
	# without touching the logger).  Synthesizing wrappers for those
	# pulls `_arc_fat_drop_via_ctrl` and its callees into the
	# consumer's reachable set, breaking minimal package-consumer
	# builds that neither import `lang.atomic` nor do any Arc drop.
	#
	# BUT: filtering strictly on direct `DropValue.ty` is too narrow
	# — fat `Arc<I>` typically lives inside an aggregate that's
	# dropped as a whole (LoggerConfig.resolver, LoggerConfigBuilder.
	# resolver).  LLVM codegen walks struct fields recursively on
	# drop, looking up `destructor_fns[field_ty]` per field; if the
	# fat Arc<I> has no wrapper, the field drop is a no-op and the
	# ArcBox leaks.  Expand the set via the same type-graph walk
	# `_seed_destroy_type_graph` uses (struct fields + variant arm
	# payloads + container element types).
	#
	# Known imprecision: the `param_types` leg of the walk follows
	# every type parameter of every visited type, including those of
	# non-owning containers (e.g. `Ref<T>`, `&T`).  That means we may
	# synthesize a wrapper for a fat `Arc<I>` that appears only as a
	# borrowed / referenced mention, not as an owned field.  This
	# mirrors `_seed_destroy_type_graph`'s behaviour, so we match the
	# existing precedent rather than introduce a stricter
	# owning-only walk here; tightening is a follow-up that would
	# have to coordinate with the other pass.
	_fat_drop_targets: set[int] = set()
	_tg_queue: list[int] = []
	for _fn_id in list(reachable):
		_fn = mir_pool.get(_fn_id)
		if _fn is None:
			continue
		for _blk in _fn.blocks.values():
			for _instr in _blk.instructions:
				if isinstance(_instr, M.DropValue):
					if _instr.ty not in _fat_drop_targets:
						_fat_drop_targets.add(_instr.ty)
						_tg_queue.append(_instr.ty)
	while _tg_queue:
		_ty = _tg_queue.pop()
		_inst = type_table.get_struct_instance(_ty)
		if _inst is not None:
			for _field_ty in _inst.field_types:
				if _field_ty not in _fat_drop_targets:
					_fat_drop_targets.add(_field_ty)
					_tg_queue.append(_field_ty)
		_vinst = type_table.get_variant_instance(_ty)
		if _vinst is not None:
			for _arm in _vinst.arms:
				for _field_ty in _arm.field_types:
					if _field_ty not in _fat_drop_targets:
						_fat_drop_targets.add(_field_ty)
						_tg_queue.append(_field_ty)
		_td = type_table.get(_ty)
		if _td.param_types:
			for _child_ty in _td.param_types:
				if _child_ty not in _fat_drop_targets:
					_fat_drop_targets.add(_child_ty)
					_tg_queue.append(_child_ty)
	added = 0
	for arc_inst_id in list(type_table.struct_instances.keys()):
		if not type_table.is_arc_fat_layout_instance(arc_inst_id):
			continue
		if arc_inst_id not in _fat_drop_targets:
			continue
		if fat_drop_fn_id is None:
			raise AssertionError(
				"fat Arc<I> synthesis requires `std.concurrent._arc_fat_drop_via_ctrl` "
				"(non-generic Slice 1 helper) but it was not found in the signature "
				"tables — STAGE3_FAT_ARC_ACTIVE is on yet the stdlib primitive is "
				"missing or mis-declared"
			)
		wrap_name = f"_arc_fat_destroy_wrapper__{arc_inst_id}"
		wrap_fn_id = FunctionId(module="std.concurrent", name=wrap_name, ordinal=0)
		# Invariant enforced by the scan skips: no thin
		# `_arc_destroy_impl<I>` instantiation should have landed for
		# this Arc<I> inst.  Detect a stray one early — the
		# instantiated name carries the `_arc_destroy_impl__inst__<hash>`
		# shape.
		_prior = destructor_fns.get(arc_inst_id)
		if _prior is not None and _prior.name.startswith("_arc_destroy_impl"):
			raise AssertionError(
				f"fat Arc<I> inst TypeId={arc_inst_id} has a thin "
				f"`_arc_destroy_impl` entry in destructor_fns "
				f"({function_symbol(_prior)}); the scan skip failed "
				f"and an invalid thin helper was queued"
			)
		if wrap_fn_id not in mir_pool:
			wrap_sig = FnSignature(
				name=function_symbol(wrap_fn_id),
				param_type_ids=[arc_inst_id],
				return_type_id=void_ty,
				declared_can_throw=False,
				param_names=["self"],
				param_mutable=[True],
				module="std.concurrent",
				is_mir_bound=True,
			)
			signatures_by_id[wrap_fn_id] = wrap_sig
			fn_infos[wrap_fn_id] = make_fn_info(wrap_fn_id, wrap_sig, declared_can_throw=False)
			builder = make_builder(wrap_fn_id)
			builder.func.params = ["self"]
			ctrl_tmp = builder.new_temp()
			builder.emit(M.StructGetField(
				dest=ctrl_tmp,
				subject="self",
				struct_ty=arc_inst_id,
				field_index=0,
				field_ty=ptr_byte_ty,
			))
			builder.emit(M.Call(
				dest=None,
				fn_id=fat_drop_fn_id,
				args=[ctrl_tmp],
				can_throw=False,
			))
			builder.set_terminator(M.Return(value=None))
			builder.func.local_types = {"self": arc_inst_id, ctrl_tmp: ptr_byte_ty}
			mir_pool[wrap_fn_id] = builder.func
			ssa_pool[wrap_fn_id] = MirToSSA().run(builder.func)
		reachable.add(wrap_fn_id)
		# Unconditionally mark the Slice 1 ctrl helper reachable.  It
		# may live in the source MIR pool (single-module build where
		# stdlib is compiled from source) OR only in
		# `external_signatures_by_id` (package-consumer build where
		# std.concurrent is a pre-linked dep).  Either way, the
		# wrapper body calls it — downstream reachability/seeding must
		# see it, and the initial guard above already verified it
		# exists in one of those tables.
		reachable.add(fat_drop_fn_id)
		destructor_fns[arc_inst_id] = wrap_fn_id
		added += 1
	if added:
		type_table.destructor_fns = destructor_fns
	return added


def _seed_destroy_type_graph(
	*,
	initial_dropped_types: set[int],
	destructor_fns: dict[int, FunctionId],
	mir_pool: dict[FunctionId, M.MirFunc],
	needed: set[FunctionId],
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
	pkg_sigs: Mapping[FunctionId, FnSignature] | None = None,
	pre_seeded_destroyers: set[FunctionId] | None = None,
) -> None:
	"""Walk the type graph (struct fields + variant arm payloads) and destroy
	function bodies in a fixpoint loop to seed all transitively-needed
	destroyer functions into *needed*.

	Used by both source-side and package-side BFS in the package-consumer
	path so the logic stays in one place (K39).
	"""
	destroy_queue: list[FunctionId] = list(pre_seeded_destroyers or ())
	type_queue: list[int] = list(initial_dropped_types)
	visited_types: set[int] = set(initial_dropped_types)
	for ty_id, fn_id in destructor_fns.items():
		if fn_id in needed and ty_id not in visited_types:
			visited_types.add(ty_id)
			type_queue.append(ty_id)
	while type_queue or destroy_queue:
		while type_queue:
			ty = type_queue.pop()
			ty_destroy = destructor_fns.get(ty)
			if ty_destroy is not None and ty_destroy in mir_pool and ty_destroy not in needed:
				needed.add(ty_destroy)
				destroy_queue.append(ty_destroy)
			inst = type_table.get_struct_instance(ty)
			if inst is not None:
				for field_ty in inst.field_types:
					if field_ty not in visited_types:
						visited_types.add(field_ty)
						type_queue.append(field_ty)
			vinst = type_table.get_variant_instance(ty)
			if vinst is not None:
				for arm in vinst.arms:
					for field_ty in arm.field_types:
						if field_ty not in visited_types:
							visited_types.add(field_ty)
							type_queue.append(field_ty)
			# Walk ARRAY/OPTIONAL/RESULT element types for transitive destroyers.
			td = type_table.get(ty)
			if td.param_types:
				for child_ty in td.param_types:
					if child_ty not in visited_types:
						visited_types.add(child_ty)
						type_queue.append(child_ty)
		while destroy_queue:
			cur = destroy_queue.pop()
			fn = mir_pool.get(cur)
			if fn is None:
				continue
			for callee in _called_funcs_in_mir(fn):
				if callee in mir_pool and callee not in needed:
					needed.add(callee)
					destroy_queue.append(callee)
			for block in fn.blocks.values():
				for instr in block.instructions:
					if isinstance(instr, M.DropValue):
						destroy_id = destructor_fns.get(instr.ty) or _resolve_destroy_fn_for_type(instr.ty, fn_infos, pkg_sigs)
						if destroy_id is not None and destroy_id in mir_pool and destroy_id not in needed:
							needed.add(destroy_id)
							destroy_queue.append(destroy_id)
						if instr.ty not in visited_types:
							visited_types.add(instr.ty)
							type_queue.append(instr.ty)


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
					# Phase 3 of terminal-`throws`: round-trip the throws
					# flags so cross-package trait/impl matching at
					# `type_checker.py:1349` sees the producer's intent.
					# `declared_nothrow` was also previously missing from
					# this encoder; closing both gaps in the same patch
					# since the existing matching logic reads both fields.
					"declared_nothrow": bool(getattr(method, "declared_nothrow", False)),
					"declared_throws": bool(getattr(method, "declared_throws", False)),
					"declared_terminal_throws": bool(getattr(method, "declared_terminal_throws", False)),
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
	package_id: str | None = None,
	module_packages: dict[str, str] | None = None,
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
		# K26/K27: For same-module trait impls, trait_key is None because
		# the interface is already in the type table.  Fall back to
		# trait_expr (the raw TypeExpr from the source) to preserve trait
		# identity in the DMIR so consumers can build vtable indices.
		# K27: resolve the trait's owning package via module_packages
		# instead of defaulting to the producer's package_id — a cross-
		# package trait (e.g. core.Throw implemented in a user package)
		# must retain the trait owner's package identity.
		if trait_obj is None:
			trait_expr_raw = getattr(impl, "trait_expr", None)
			if trait_expr_raw is not None:
				te_name = getattr(trait_expr_raw, "name", None)
				te_mod = getattr(trait_expr_raw, "module_id", None) or module_id
				if isinstance(te_name, str) and te_name:
					te_pkg = (module_packages or {}).get(te_mod, package_id)
					trait_obj = {
						"package_id": te_pkg,
						"module": te_mod,
						"name": te_name,
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
								# Phase 3 of terminal-`throws`: read the
								# throws flags from the encoded payload.
								# Old packages (pre-Phase-3) lack these
								# fields; default to False for forward
								# compatibility.
								declared_nothrow=bool(method.get("declared_nothrow", False)),
								declared_throws=bool(method.get("declared_throws", False)),
								declared_terminal_throws=bool(method.get("declared_terminal_throws", False)),
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
				# Canonicalize: replace ad hoc impl TypeParamIds with the
				# target nominal type's canonical TypeParamIds so all packages
				# produce the same TypeVarId for the same type parameter.
				if type_params and target_expr is not None:
					_te_name = getattr(target_expr, "name", None)
					_te_mod = getattr(target_expr, "module_id", None) or def_module
					_te_args = list(getattr(target_expr, "args", []) or [])
					if _te_name:
						impl_type_param_map = type_table.canonicalize_impl_type_params(
							impl_type_param_map,
							target_module=_te_mod,
							target_name=_te_name,
							target_args=_te_args,
						)
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
						# Canonicalize method impl params against target struct.
						if impl_param_map and target_expr is not None:
							_te_name_m = getattr(target_expr, "name", None)
							_te_mod_m = getattr(target_expr, "module_id", None) or def_module
							_te_args_m = list(getattr(target_expr, "args", []) or [])
							if _te_name_m:
								impl_param_map = type_table.canonicalize_impl_type_params(
									impl_param_map,
									target_module=_te_mod_m,
									target_name=_te_name_m,
									target_args=_te_args_m,
								)
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
					# Package-local impl_ids are sequential-from-zero within each
					# package and are NOT globally unique.  Let the registry assign
					# a fresh global id instead of forcing the package-local value.
					impl_id = id_registry.intern_impl(impl_key)
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


def _apply_stdlib_escape_annotations(signatures_by_id: Mapping, *, semantic_world: object | None = None) -> None:
	"""Annotate known stdlib callable params with escape levels (idempotent).

	This must run before the borrow checker so that escape-level enforcement
	(SCOPED, THREAD, STATIC) applies to stdlib functions like conc.scope,
	conc.spawn, etc.  Called from both compile_stubbed_funcs (e2e/test path)
	and main() (CLI path).

	Production (world-backed): writes to SemanticWorld overlay.
	Test-only (no world): mutates FnSignature.param_escape_level in place.
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
		if _levels is None:
			continue
		if semantic_world is not None:
			if semantic_world.get_signature_annotation(_fn_id, "param_escape_level") is not None:
				continue
			semantic_world.annotate_signature(_fn_id, "param_escape_level", list(_levels))
		else:
			# Test-only: mutate sig field directly.
			if _sig.param_escape_level is not None:
				continue
			_sig.param_escape_level = list(_levels)


@dataclass
class Pass1State:
	"""Resolution infrastructure from the driver's Pass 1 type-check.

	When provided to compile_stubbed_funcs, the function uses these objects
	instead of rebuilding its own callable registry, trait indices, module
	visibility, etc.  This eliminates K42-class divergences where two
	independently-constructed registries produce different method resolution.

	Source function type-check is also skipped (typed_fns are reused).
	Generic instantiation and lambda type-check still run, using the shared state.
	"""
	typed_fns: dict  # FunctionId -> TypedFn
	callable_registry: object  # CallableRegistry
	impl_index: object  # GlobalImplIndex
	trait_index: object  # GlobalTraitIndex
	trait_impl_index: object  # GlobalTraitImplIndex
	trait_scope_by_module: dict  # str -> list[trait_key]
	linked_world: object  # LinkedWorld
	require_env: object  # RequireEnv
	visible_module_names_by_name: dict  # str -> set[str]
	module_ids: dict  # object -> int
	method_wrapper_specs: list  # list[MethodWrapperSpec]
	unsafe_trusted_modules: set  # set[str]
	function_keys_by_fn_id: dict  # FunctionId -> FunctionKey
	visibility_provenance_by_id: dict  # int -> tuple[str, ...]
	lambda_fn_specs: dict | None = None  # FunctionId -> LambdaFnSpec from Pass 1 type checker
	pkg_unsafe_modules: set | None = None  # set[str] — package modules needing unsafe permission only


def _format_param_drop_diagnostic(
	func: "M.MirFunc",
	param_name: str,
	param_ty: "TypeId",
	type_table: "TypeTable",
	lowering_status: str,
	postpass_has_drop: bool,
) -> str:
	"""Build a detailed diagnostic for param drop disagreement."""
	import sys
	td = type_table.get(param_ty)
	inst = type_table.get_struct_instance(param_ty)
	dfns = getattr(type_table, "destructor_fns", None) or {}
	in_dfns = param_ty in dfns
	pkg = getattr(type_table, "module_packages", {}).get(td.module_id, "<source>") if td.module_id else "<unknown>"

	lines = [
		f"param drop disagreement in {func.name}:",
		f"  param: {param_name}",
		f"  type: {td.name} (ty={param_ty}, kind={td.kind.name}, module={td.module_id})",
		f"  input: {pkg}",
		f"  lowering_status: {lowering_status}",
		f"  postpass has_drop: {postpass_has_drop}",
		f"  in destructor_fns: {in_dfns}",
	]
	if inst is not None:
		field_info = []
		for i, ft in enumerate(inst.field_types):
			ftd = type_table.get(ft)
			fhd = type_table.has_drop(ft)
			fin = ft in dfns
			field_info.append(f"    [{i}] {ftd.name} ty={ft} kind={ftd.kind.name} has_drop={fhd} in_dfns={fin}")
		lines.append(f"  struct instance: {len(inst.field_types)} fields")
		lines.extend(field_info)
	else:
		lines.append("  struct instance: NOT AVAILABLE")
	# Show generic name-match info for destructor_fns
	if td.module_id and not in_dfns:
		name_matches = [
			(dtid, type_table.get(dtid).module_id)
			for dtid in dfns
			if type_table.get(dtid).name == td.name
		]
		if name_matches:
			lines.append(f"  destructor name-matches (wrong TypeId): {name_matches}")
	return "\n".join(lines)


def _postdrop_check_param_drops(
	func: "M.MirFunc",
	type_table: "TypeTable",
	diagnostics: list["Diagnostic"] | None = None,
) -> None:
	"""Check for param drop disagreements between lowering and post-pass.

	For each param where has_drop is True at post-pass time but
	param_drop_status says "no_drop", emit a detailed diagnostic instead of
	silently injecting __postdrop_* drops.

	Params with status "scope_exit_drop", "forwarded_to_callee", or "moved"
	are already handled — no injection needed.

	Params with no recorded status (empty param_drop_status, e.g. from older
	MIR or non-lowered functions) fall back to a warning.
	"""
	for param_name in func.params:
		param_ty = func.local_types.get(param_name)
		if param_ty is None:
			continue
		postpass_has_drop = type_table.has_drop(param_ty)
		if not postpass_has_drop:
			continue
		status = func.param_drop_status.get(param_name)
		# If lowering recorded that this param needs a scope-exit drop,
		# was forwarded/moved, or is managed by string_arc — no action needed.
		if status in ("scope_exit_drop", "forwarded_to_callee", "moved", "string_arc_managed"):
			continue
		# Disagreement: has_drop=True at post-pass but lowering said no_drop
		# (or status was never recorded).
		if status == "no_drop" or status is None:
			diag_msg = _format_param_drop_diagnostic(
				func, param_name, param_ty, type_table,
				lowering_status=status or "<not recorded>",
				postpass_has_drop=True,
			)
			import sys
			print(f"[driftc] error: {diag_msg}", file=sys.stderr)
			if diagnostics is not None:
				diagnostics.append(Diagnostic(
					message=(
						f"param '{param_name}' in {func.name}: has_drop() is True at post-pass "
						f"but was '{status or '<not recorded>'}' at lowering time. "
						f"This indicates has_drop() instability across pipeline stages. "
						f"The param is missing a scope-exit drop."
					),
					severity="error",
					span=None,
					phase="postdrop",
				))


# Robustness matrix rows #4 and #5: Python recursion-limit headroom for
# the compile pipeline. Several lowering passes and HIR rewrite walks
# descend trees recursively. User-controlled-depth shapes can hit the
# default 1000 limit before any in-pass guard fires. Affected sites that
# remain recursive (deliberately, to avoid invasive refactors of complex
# visitors with many special cases):
#   - stage2/hir_to_mir.py::_visit_expr_HBinary
#     short-circuit AND/OR via new blocks, type-coercion, string-aware
#     MIR — too many special cases for an iterative spine flattener
#   - parser/__init__.py::walk_stmt/walk_block/walk_expr
#     HIR rewrite pass for module-qualified access; in-place mutation,
#     lexical-bound-set discipline, specialized handling for match/try
#     arms with binders — same complexity story
#
# Stage1's `_visit_expr_Binary` (row #4) and `_visit_stmt_IfStmt` (row #5)
# ARE iterative; only the downstream walkers above need stack headroom.
#
# The decorator is applied to every public compile entry point in this
# module so library consumers (compile_stubbed_funcs,
# compile_to_llvm_ir_for_tests) get the same headroom as the CLI path.
# The previous limit is restored on exit so callers that import driftc
# as a library do not get a permanent global recursion-limit change.
#
# 32768 supports ~8000 levels of else-if chain at the most expensive
# walker (`walk_stmt`/`walk_block`, ~4 frames per source level). The
# 0.27.160 value of 8192 only supported ~2000 levels and was too tight
# for row #5 at depths >2000.
_COMPILE_RECURSION_HEADROOM = 32768


def _with_compile_recursion_headroom(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		prev = sys.getrecursionlimit()
		bumped = prev < _COMPILE_RECURSION_HEADROOM
		if bumped:
			sys.setrecursionlimit(_COMPILE_RECURSION_HEADROOM)
		try:
			return fn(*args, **kwargs)
		finally:
			if bumped:
				sys.setrecursionlimit(prev)
	return wrapper


@_with_compile_recursion_headroom
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
	# Phase 1 (converge-one-pipeline): accept resolution state from the
	# driver to eliminate duplicate registry construction and type-check.
	pass1_state: "Pass1State | None" = None,
	semantic_world: "Any | None" = None,
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

	# Adapter: when semantic_world is provided, use it as the primary source
	# for stores that it owns.  Explicit parameters are kept for backward
	# compatibility (test paths that don't use a world).
	if semantic_world is not None:
		semantic_world.assert_ready()
		# Consistency: explicitly passed stores must match the world's references.
		if type_table is not None and semantic_world.type_table is not None and type_table is not semantic_world.type_table:
			raise RuntimeError("conflicting type_table: explicit argument differs from semantic_world.type_table")
		if module_deps is not None and semantic_world.module_deps is not None and module_deps is not semantic_world.module_deps:
			raise RuntimeError("conflicting module_deps: explicit argument differs from semantic_world.module_deps")
		if external_trait_defs is not None and semantic_world.external_trait_defs is not None and external_trait_defs is not semantic_world.external_trait_defs:
			raise RuntimeError("conflicting external_trait_defs: explicit argument differs from semantic_world.external_trait_defs")
		if external_impl_metas is not None and semantic_world.external_impl_metas is not None and external_impl_metas is not semantic_world.external_impl_metas:
			raise RuntimeError("conflicting external_impl_metas: explicit argument differs from semantic_world.external_impl_metas")
		if external_missing_traits is not None and semantic_world.external_missing_traits is not None and external_missing_traits is not semantic_world.external_missing_traits:
			raise RuntimeError("conflicting external_missing_traits: explicit argument differs from semantic_world.external_missing_traits")
		# Unpack world-owned stores, falling back to explicit args.
		if type_table is None and semantic_world.type_table is not None:
			type_table = semantic_world.type_table
		if module_deps is None and semantic_world.module_deps is not None:
			module_deps = semantic_world.module_deps
		if external_trait_defs is None and semantic_world.external_trait_defs is not None:
			external_trait_defs = semantic_world.external_trait_defs
		if external_impl_metas is None and semantic_world.external_impl_metas is not None:
			external_impl_metas = semantic_world.external_impl_metas
		if external_missing_traits is None and semantic_world.external_missing_traits is not None:
			external_missing_traits = semantic_world.external_missing_traits

	# Guard: signatures with TypeIds must come with a shared TypeTable so TypeKind
	# queries stay coherent end-to-end.  This runs after the adapter so that
	# type_table unpacked from semantic_world satisfies the requirement.
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
	if pass1_state is not None and signatures_by_id:
		# Phase 4 (converge-one-pipeline): signatures from the driver are already
		# resolved with TypeIds and can_throw.  Skip the expensive resolution loop
		# but fill missing error_type_id for can-throw sigs (package signatures
		# don't serialize error_type_id).
		_p4_resolved: dict[FunctionId, FnSignature] = {}
		for fn_id, sig in signatures_by_id.items():
			err_id = sig.error_type_id
			if sig.declared_can_throw is not False and err_id is None:
				err_id = shared_type_table.ensure_error()
				_p4_resolved[fn_id] = replace(sig, error_type_id=err_id)
		if _p4_resolved:
			base_signatures_by_id = dict(signatures_by_id)
			base_signatures_by_id.update(_p4_resolved)
		else:
			base_signatures_by_id = dict(signatures_by_id)
	elif not signatures_by_id:
		shared_type_table, base_signatures_by_id, _ffi_diags = resolve_program_signatures(
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
				if fn_id in base_signatures_by_id:
					return
				raise AssertionError(f"signature collision for '{function_symbol(fn_id)}'")
			return
		_record_signature_provenance(fn_id, sig)
		derived_signatures_by_id[fn_id] = sig

	if pass1_state is not None:
		# Phase 4 (converge-one-pipeline): wrapper signatures are already in
		# base_signatures_by_id (flattened from the driver's ChainMap), so
		# injection would return empty specs.  Reuse the driver's specs for
		# downstream wrapper MIR synthesis.
		method_wrapper_specs = pass1_state.method_wrapper_specs
	else:
		# Option B: no boundary wrapper injection.
		method_wrapper_specs = []

	if pass1_state is not None:
		# Phase 4 (converge-one-pipeline): the driver already normalized HIR
		# at Pass 1 (line 8012).  The caller passes normalized HIR directly,
		# so skip the redundant O(n) normalize_hir traversal.
		normalized_hirs_by_id = dict(func_hirs_by_id)
	else:
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
	_csf_pkg_unsafe: set[str] = set()
	if pass1_state is not None:
		# Phase 4 (converge-one-pipeline): reuse the driver's unsafe_trusted_modules
		# set, which was built from the same type_table and module_exports.
		unsafe_trusted_modules = pass1_state.unsafe_trusted_modules
		_csf_pkg_unsafe = pass1_state.pkg_unsafe_modules or set()
	else:
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
	if allow_unsafe and module_deps:
		for mod_id in module_deps.keys():
			if isinstance(mod_id, str):
				unsafe_trusted_modules.add(mod_id)
	_source_mods = {getattr(fid, "module", None) for fid in func_hirs_by_id.keys()} - {None}
	if module_deps is not None:
		_source_mods.update(m for m in module_deps.keys() if isinstance(m, str))
	if shared_type_table is not None:
		shared_type_table.source_modules = _source_mods
	type_checker = TypeChecker(type_table=shared_type_table, allow_unsafe=bool(allow_unsafe), unsafe_trusted_modules=unsafe_trusted_modules, pkg_unsafe_modules=_csf_pkg_unsafe, allow_unsafe_without_block=True, semantic_world=semantic_world, source_modules=_source_mods)
	# K42: seed Pass 1 lambda_fn_specs into the new TypeChecker so that
	# captureless lambda function bodies are generated during MIR lowering.
	if pass1_state is not None and pass1_state.lambda_fn_specs:
		type_checker._lambda_fn_specs.update(pass1_state.lambda_fn_specs)
	# Phase 6: only allocate callable_registry / module_ids / visibility_provenance
	# when pass1_state is absent — under pass1_state these are immediately
	# overwritten from the driver's state (lines 3087-3104).
	if pass1_state is not None:
		callable_registry = pass1_state.callable_registry
		module_ids = pass1_state.module_ids
		visibility_provenance_by_id = pass1_state.visibility_provenance_by_id
	else:
		callable_registry = CallableRegistry()
		module_ids: dict[object, int] = {None: 0}
		visibility_provenance_by_id: dict[int, tuple[str, ...]] = {}
	if pass1_state is None:
		if module_deps:
			all_mods = set(module_deps.keys())
			for deps in module_deps.values():
				all_mods |= set(deps)
			for mid in sorted(all_mods):
				module_ids.setdefault(mid, len(module_ids))
		else:
			for fn_id in signatures_by_id.keys():
				module_ids.setdefault(getattr(fn_id, "module", None), len(module_ids))
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
	# Phase 3: when pass1_state is provided, shared_type_table.trait_worlds
	# was already populated by Pass 1 (main:7817-7868).  Skip the merge
	# mutations but still run the orphan-impl diagnostic check so that
	# diagnostics parity is unchanged.
	_skip_trait_merge = pass1_state is not None
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

		if not _skip_trait_merge and external_trait_defs:
			for trait_def in external_trait_defs:
				key = getattr(trait_def, "key", None)
				if key is None:
					continue
				world = _ensure_world(getattr(key, "module", None))
				world.traits.setdefault(key, trait_def)

		# Collect all consumed package IDs so that their trait impls are
		# trusted (validated at package build time). The orphan rule only
		# applies to impls the *consumer* defines, not to upstream impls.
		_consumed_pkgs: set[str | None] = {default_package}
		if module_packages:
			_consumed_pkgs.update(module_packages.values())
		if external_impl_metas:
			for impl in external_impl_metas:
				if getattr(impl, "trait_key", None) is None:
					continue
				target_expr = getattr(impl, "target_expr", None)
				if target_expr is None:
					continue
				# Derive `target_key` from the already-resolved
				# `impl.target_type_id` so that free impl type-params
				# render as canonical TypeVars (`(None, None, "T")`)
				# rather than nominal types in `def_module`
				# (`("std", "std.concurrent", "T")` for an `Arc<T>`
				# impl in std.concurrent).  Pass 1 main's matching
				# merge already uses this canonical encoding;
				# aligning here makes the dup check below see
				# Pass 1's existing entry as identical and skip
				# re-appending.  Without alignment, two entries
				# land under `impls_by_trait_target[(Share,
				# Arc-head)]`, the solver's name-based type-param
				# binding makes BOTH applicable, status becomes
				# AMBIGUOUS, `is_share` returns False, and the
				# type checker fires `E-CAPTURE-SHARE-NOT-SHARE`
				# on `captures(share x)` in `--emit-package` mode.
				target_key = type_key_from_typeid(shared_type_table, impl.target_type_id)
				head_key = target_key.head()
				local_pkg = default_package
				trait_pkg = getattr(impl.trait_key, "package_id", None) or local_pkg
				target_pkg = getattr(head_key, "package_id", None) or local_pkg
				impl_pkg = _module_package(getattr(impl, "def_module", None))
				def _is_local_or_consumed(pkg: str | None) -> bool:
					return pkg is None or pkg == local_pkg or pkg in _consumed_pkgs
				if not _is_local_or_consumed(trait_pkg) and not _is_local_or_consumed(target_pkg):
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
				if _skip_trait_merge:
					continue
				world = _ensure_world(getattr(impl, "def_module", None))
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
					type_params=list(getattr(impl, "impl_type_params", []) or []),
					loc=getattr(impl, "loc", None),
				)
				impl_id = len(world.impls)
				world.impls.append(impl_def)
				world.impls_by_trait.setdefault(impl_def.trait, []).append(impl_id)
				world.impls_by_target_head.setdefault(impl_def.target_head, []).append(impl_id)
				world.impls_by_trait_target.setdefault((impl_def.trait, impl_def.target_head), []).append(impl_id)

		if not _skip_trait_merge:
			shared_type_table.trait_worlds = trait_worlds
			if hasattr(shared_type_table, "_global_trait_world"):
				delattr(shared_type_table, "_global_trait_world")
	# Phase 5: requires_by_fn_id is only consumed by the function_keys extension
	# loop (line 3065).  When pass1_state provides function_keys_by_fn_id,
	# that loop is skipped, so this population is also unnecessary.
	if pass1_state is None and isinstance(trait_worlds, dict):
		for world in trait_worlds.values():
			for fn_id, req in getattr(world, "requires_by_fn", {}).items():
				requires_by_fn_id[fn_id] = req
	# Pre-install destructor_fns before _build_linked_world so query
	# callbacks installed there cannot poison has_drop with False.
	# Uses trait_key name+module matching — no linked_world needed.
	if shared_type_table is not None and pass1_state is None:
		_pre_dfns = _scan_destructible_impls_by_name(module_exports, external_impl_metas)
		if _pre_dfns:
			shared_type_table.destructor_fns = _pre_dfns
			# Re-finalize non-generic variants now that destructor_fns is
			# authoritative.  The parser-phase `finalize_variants()` call
			# computes each variant instance's `internal_tombstone_ctor`
			# based on `has_drop(field_ty)`, which returns False for user
			# structs whose `core.Destructible` impl has not yet been
			# registered at parse time.  Without this second finalize,
			# variants like `UserMsg { Payload(Token), Other(Int) }` miss
			# their auto-injected internal tombstone — which later breaks
			# any codegen site that needs to materialize a drop-safe
			# tombstone (e.g. the match-scrutinee tombstone store in
			# `_ensure_arm_scrut_ptr`).
			shared_type_table.finalize_variants()

	if pass1_state is not None:
		linked_world = pass1_state.linked_world
		require_env = pass1_state.require_env
	else:
		linked_world, require_env = _build_linked_world(shared_type_table)
	# Phase 6: when pass1_state is provided, the driver already called
	# _install_destructor_fns + K39 on the shared type_table before
	# constructing Pass1State.  Skip to avoid _install_destructor_fns
	# replacing type_table.destructor_fns (which would clobber K39 entries).
	# destructor_fns is already pre-installed above (before _build_linked_world)
	# from both module_exports and external_impl_metas using trait_key name
	# matching.  Do NOT re-assign here — reassignment triggers __setattr__
	# cache clear, and any has_drop calls between the clear and the next
	# scope-drop CleanupHook authoring could re-poison the cache.
	#
	# The pre-install block handles all sources:
	#   - module_exports: local Destructible impls (source-compiled)
	#   - external_impl_metas: package Destructible impls (consumed packages)
	# The _install_destructor_fns + K39 block that was here is now folded
	# into the pre-install above.

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

	if pass1_state is not None:
		# Phase 5 (converge-one-pipeline): reuse the driver's function_keys_by_fn_id
		# which covers ALL generic signatures (wrappers + non-wrappers), skipping
		# the O(n) compute_template_decl_fingerprint loop below.
		function_keys_by_fn_id.update(pass1_state.function_keys_by_fn_id)
	else:
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
	# Phase 2 (converge-one-pipeline): skip callable_registry population,
	# trait/impl index construction, and module visibility setup when
	# pass1_state provides them.
	# Phase 6: callable_registry, module_ids, visibility_provenance_by_id
	# are already assigned from pass1_state at the top (line 2845).
	if pass1_state is not None:
		impl_index = pass1_state.impl_index
		trait_index = pass1_state.trait_index
		trait_impl_index = pass1_state.trait_impl_index
		trait_scope_by_module = pass1_state.trait_scope_by_module
		visible_module_names_by_name = pass1_state.visible_module_names_by_name
	else:
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
			if td.kind is TypeKind.STRUCT:
				inst = shared_type_table.get_struct_instance(impl_tid)
				if inst is not None:
					return inst.base_id
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
		# K20: pre-compute generic method keys to suppress __inst__ monomorphizations.
		_local_generic_method_keys: set[tuple[int | None, str | None]] = set()
		for _fid, _sig in signatures_by_id.items():
			if _sig.is_method and (_sig.type_params or getattr(_sig, "impl_type_params", [])):
				_local_generic_method_keys.add((_registry_impl_target_type_id(_sig.impl_target_type_id), _sig.method_name))
		for fn_id, sig in signatures_by_id.items():
			if sig.return_type_id is None:
				continue
			if getattr(sig, "is_wrapper", False):
				continue
			# K20: skip __inst__ monomorphized sigs when generic template exists.
			if "__inst__" in (fn_id.name or "") and sig.is_method and not (sig.type_params or getattr(sig, "impl_type_params", [])):
				norm_recv = _registry_impl_target_type_id(sig.impl_target_type_id)
				if (norm_recv, sig.method_name) in _local_generic_method_keys:
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
			all_trait_keys: list[object] | None = None
			for mod, exp in module_exports.items():
				if isinstance(exp, dict):
					scope = exp.get("trait_scope", None)
					if isinstance(scope, list):
						trait_scope_by_module[mod] = scope
					elif scope is None:
						# K25 backward-compat fallback for old packages without trait_scope.
						if all_trait_keys is None:
							all_trait_keys = list(trait_index.traits_by_id.keys()) if trait_index is not None else []
						trait_scope_by_module[mod] = all_trait_keys
	typed_fns_by_id: dict[FunctionId, object] = {}
	type_diags: list[Diagnostic] = []
	if trait_world_diags:
		type_diags.extend(trait_world_diags)
	if shared_type_table is not None:
		# Phase 3: Pass 1 already called validate_interface_schemas (main:8063).
		if pass1_state is None:
			type_checker.validate_interface_schemas(diagnostics=type_diags)
			# Recursive value-type cycle detector. Closes
			# `issues/recursive-value-struct-accepted/`. Runs after struct
			# and variant instances are committed to the type table so it
			# sees monomorphized types; emits one diagnostic per offending
			# type with a primary `Arc<...>` (or `Optional<Arc<...>>`)
			# suggestion.
			type_checker.validate_no_recursive_value_types(diagnostics=type_diags)
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
	if pass1_state is None:
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
	if pass1_state is None and module_deps is not None:
		def _collect_reexport_targets(mod: str) -> set[str]:
			exp = module_exports.get(mod) if isinstance(module_exports, dict) else None
			if not isinstance(exp, dict):
				return set()
			reexp = exp.get("reexports")
			if not isinstance(reexp, dict):
				return set()
			targets: set[str] = set()
			type_reexp = reexp.get("types") if isinstance(reexp.get("types"), dict) else {}
			for kind in ("structs", "variants", "exceptions", "interfaces", "aliases"):
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

		all_module_names: set[str] | None = None
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
		# K25 TEMPORARY FALLBACK — remove when DMIR serializes module import graph.
		# External package modules are absent from module_deps (their
		# import graph is not available in DMIR).  Give them visibility to
		# all modules — their dependencies were validated at package build
		# time.
		# Removal target: DMIR v1 freeze (serialize per-module import graph
		# in package metadata; reconstruct exact visible_module_names on load).
		if isinstance(module_exports, dict):
			for mod_name in module_exports:
				if mod_name not in visible_module_names_by_name:
					if all_module_names is None:
						all_module_names = set(module_exports.keys()) | set(module_deps.keys())
					visible_module_names_by_name[mod_name] = set(all_module_names)
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

	if pass1_state is not None:
		# Phase 1: reuse pre-typed results from the driver's Pass 1.
		typed_fns_by_id.update(pass1_state.typed_fns)
		for fn_id in pass1_state.typed_fns:
			typecheck_ok_by_fn[fn_id] = True
		# Convergence parity assertions: verify shared state matches what
		# independent construction would produce.  Gated behind debug flag.
		if drift_debug.enabled("convergence_parity"):
			import sys as _cp_sys
			_cp_errors: list[str] = []
			# 1. Function keys parity: recompute from signatures, compare.
			_cp_fkeys: dict[FunctionId, FunctionKey] = {}
			if isinstance(template_keys_by_fn_id, dict):
				_cp_fkeys.update(template_keys_by_fn_id)
			_cp_requires: dict[FunctionId, object] = {}
			_cp_tw = getattr(shared_type_table, "trait_worlds", {}) if shared_type_table is not None else {}
			if isinstance(_cp_tw, dict):
				for _cp_w in _cp_tw.values():
					for _cp_fid, _cp_req in getattr(_cp_w, "requires_by_fn", {}).items():
						_cp_requires[_cp_fid] = _cp_req
			_cp_default_pkg = getattr(shared_type_table, "package_id", None) or package_id
			_cp_mod_pkgs = getattr(shared_type_table, "module_packages", None)
			for _cp_fid, _cp_sig in signatures_by_id.items():
				if not (getattr(_cp_sig, "type_params", []) or getattr(_cp_sig, "impl_type_params", [])):
					continue
				if _cp_fid in _cp_fkeys:
					continue
				if _cp_sig.param_types is None or _cp_sig.return_type is None:
					continue
				_cp_mid = getattr(_cp_sig, "module", None) or getattr(_cp_fid, "module", None) or "main"
				_cp_name = _declared_name_from_fn_id(_cp_fid, _cp_mid)
				_cp_req = _cp_requires.get(_cp_fid)
				_cp_fp, _ = compute_template_decl_fingerprint(
					_cp_sig, declared_name=_cp_name, module_id=_cp_mid,
					require_expr=_cp_req if _cp_req is not None else None,
					default_package=_cp_default_pkg, module_packages=_cp_mod_pkgs,
				)
				_cp_fkeys[_cp_fid] = FunctionKey(
					package_id=package_id, module_path=_cp_mid,
					name=_cp_name, decl_fingerprint=_cp_fp,
				)
			for _cp_fid, _cp_key in _cp_fkeys.items():
				_cp_shared = function_keys_by_fn_id.get(_cp_fid)
				if _cp_shared is None:
					_cp_errors.append(f"function_key missing for {function_symbol(_cp_fid)}")
				else:
					if _cp_shared.decl_fingerprint != _cp_key.decl_fingerprint:
						_cp_errors.append(f"function_key fingerprint mismatch for {function_symbol(_cp_fid)}: shared={_cp_shared.decl_fingerprint} recomputed={_cp_key.decl_fingerprint}")
					if _cp_shared.package_id != _cp_key.package_id:
						_cp_errors.append(f"function_key package_id mismatch for {function_symbol(_cp_fid)}: shared={_cp_shared.package_id!r} recomputed={_cp_key.package_id!r}")
					if _cp_shared.module_path != _cp_key.module_path:
						_cp_errors.append(f"function_key module_path mismatch for {function_symbol(_cp_fid)}: shared={_cp_shared.module_path!r} recomputed={_cp_key.module_path!r}")
					if _cp_shared.name != _cp_key.name:
						_cp_errors.append(f"function_key name mismatch for {function_symbol(_cp_fid)}: shared={_cp_shared.name!r} recomputed={_cp_key.name!r}")
			# (Wrapper injection parity check removed — Option B has no wrappers.)
			# 3. Signature resolution parity: verify pass1 sigs match what
			#    resolution would produce (TypeIds, error_type_id, can_throw).
			for _cp_fid, _cp_sig in base_signatures_by_id.items():
				if _cp_sig.declared_can_throw is not False and _cp_sig.error_type_id is None:
					_cp_errors.append(f"signature {_cp_sig.name} still missing error_type_id after fixup")
				if _cp_sig.param_type_ids is None and _cp_sig.param_types is not None:
					_cp_errors.append(f"signature {_cp_sig.name} has param_types but no param_type_ids")
			# 4. Visibility provenance parity: spot-check module_ids coverage.
			for _cp_mod, _cp_mid in module_ids.items():
				if _cp_mod is not None and int(_cp_mid) not in visibility_provenance_by_id:
					_cp_errors.append(f"visibility_provenance_by_id missing mod_id={_cp_mid} for module '{_cp_mod}'")
			# 5. Destructor registration parity: verify destructor_fns populated.
			_cp_dfns = getattr(shared_type_table, "destructor_fns", None) or {}
			if module_exports is not None and linked_world is not None:
				_cp_dk = _find_trait_key(linked_world.global_world, module="std.core", name="Destructible")
				if _cp_dk is not None:
					for _cp_exp in module_exports.values():
						if not isinstance(_cp_exp, dict):
							continue
						for _cp_impl in (_cp_exp.get("impls") or []):
							if not isinstance(_cp_impl, ImplMeta):
								continue
							if _cp_impl.trait_key != _cp_dk:
								continue
							_cp_ttid = getattr(_cp_impl, "target_type_id", None)
							if isinstance(_cp_ttid, int) and not shared_type_table.has_typevar(_cp_ttid):
								if _cp_ttid not in _cp_dfns:
									_cp_errors.append(f"destructor_fns missing target_type_id={_cp_ttid}")
			if _cp_errors:
				print(f"[drift:debug][convergence_parity] FAIL: {len(_cp_errors)} parity errors", file=_cp_sys.stderr)
				for _cp_e in _cp_errors:
					print(f"  - {_cp_e}", file=_cp_sys.stderr)
				raise AssertionError(f"convergence parity check failed: {_cp_errors[0]} (and {len(_cp_errors)-1} more)" if len(_cp_errors) > 1 else f"convergence parity check failed: {_cp_errors[0]}")
			else:
				print(f"[drift:debug][convergence_parity] OK: all 5 checks passed", file=_cp_sys.stderr)
	else:
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
		call_expr.origin = "wrapper_call"
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
		new_sig = CallSig(param_types=new_params, user_ret_type=new_ret, can_throw=sig.can_throw, includes_callee=sig.includes_callee, declared_terminal_throws=sig.declared_terminal_throws)
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

	# Seed from pre-installed destructor_fns so we don't lose entries
	# registered during the early pre-install phase (before _build_linked_world).
	destructor_fns: dict[TypeId, FunctionId] = dict(getattr(shared_type_table, "destructor_fns", None) or {}) if shared_type_table is not None else {}
	# K39: Track generic Destructible impls for post-instantiation rescan.
	# At this point struct_instances may not yet contain all concrete types
	# (e.g. ScopeGuard<Int>) — those are created during _drain_instantiations.
	_generic_destructible_impls: list[tuple[TypeId, FunctionId]] = []  # (base_id, template_destroy_fn_id)
	if shared_type_table is not None and linked_world is not None:
		destructible_key = _find_trait_key(linked_world.global_world, module="std.core", name="Destructible")
		if destructible_key is not None:
			# Collect all ImplMeta objects: from module_exports + external_impl_metas.
			_all_impls: list[ImplMeta] = []
			if module_exports is not None:
				for exp in module_exports.values():
					if not isinstance(exp, dict):
						continue
					impls = exp.get("impls")
					if not isinstance(impls, list):
						continue
					for impl in impls:
						if isinstance(impl, ImplMeta):
							_all_impls.append(impl)
			# K39: Also scan external_impl_metas — package Destructible impls
			# (e.g. ScopeGuard<T>::destroy) live here, not in module_exports.
			if external_impl_metas:
				for impl in external_impl_metas:
					if isinstance(impl, ImplMeta):
						_all_impls.append(impl)
			for impl in _all_impls:
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
				# Arc runtime boundary: when the destroy method is
				# `@intrinsic` (e.g. Arc<T>::destroy with
				# intrinsic_kind = ARC_DESTROY), redirect the
				# monomorphization target to the matching
				# `_arc_destroy_impl<T>` helper.  Without this the
				# Destructible scan would queue the bodyless template
				# and surface an undefined symbol at link time.
				# Concrete-T destruction behavior is carried by the
				# helper body; Stage 3 replaces this redirect with a
				# direct compiler-emitted ARC_DESTROY lowering.
				#
				# Stage 3 fat-layout split: fat `Arc<I>` instances
				# (where `I` is an interface, layout specialized to
				# `{ctrl, data, vtable}`) do NOT go through the thin
				# `_arc_destroy_impl<T>` template.  The scan below
				# **skips** each such instance via
				# `is_arc_fat_layout_instance(inst_id)`, so no thin
				# `destructor_fns[inst_id]` entry is ever written
				# for a fat `Arc<I>`.  The per-I fat destructor
				# wrapper is instead synthesized late by
				# `_synthesize_fat_arc_destructor_wrappers` and is
				# the sole `destructor_fns` entry for the fat
				# instance.
				method_sig = signatures_by_id.get(method_fn_id) if signatures_by_id is not None else None
				if method_sig is not None and bool(getattr(method_sig, "is_intrinsic", False)):
					_helper_name = None
					_intrinsic_kind = getattr(method_sig, "intrinsic_kind", None)
					if _intrinsic_kind is not None and getattr(_intrinsic_kind, "value", None) == "arc_destroy":
						_helper_name = "_arc_destroy_impl"
					if _helper_name is None:
						continue
					helper_fn_id: FunctionId | None = None
					for fid, sig in signatures_by_id.items():
						if fid.module == "std.concurrent" and fid.name == _helper_name and not bool(getattr(sig, "is_method", False)):
							helper_fn_id = fid
							break
					if helper_fn_id is None:
						continue
					method_fn_id = helper_fn_id
				if shared_type_table.has_typevar(target_type_id):
					base_id: TypeId | None = None
					inst = shared_type_table.get_struct_instance(target_type_id)
					if inst is not None:
						base_id = inst.base_id
					else:
						base_id = target_type_id
					_generic_destructible_impls.append((base_id, method_fn_id))
					for inst_id, inst in shared_type_table.struct_instances.items():
						if inst.base_id != base_id:
							continue
						if shared_type_table.has_typevar(inst_id):
							continue
						# Fat-layout skip (ABI 10): fat `Arc<I>` instances
						# get a compiler-synthesized per-I destructor
						# wrapper installed by the late fat-Arc
						# synthesis pass.  Queuing `_arc_destroy_impl<I>`
						# here would instantiate a template whose body
						# (`self.buf`) is structurally invalid against
						# the live `{ctrl, data, vtable}` layout.
						if shared_type_table.is_arc_fat_layout_instance(inst_id):
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

	# Arc runtime boundary — shared intrinsic-template predicate.
	# Used in `_queue_instantiations` and `_rewrite_call_targets`
	# below to skip templates whose sig is `@intrinsic` (no body
	# to monomorphize; call sites route through
	# `_lower_method_call_with_info`'s helper-redirect for
	# concrete T).
	def _template_fn_id_for_key(template_key: "FunctionKey") -> "FunctionId | None":
		if signatures_by_id is None:
			return None
		for _fid, _sig in signatures_by_id.items():
			if _fid.module != template_key.module_path:
				continue
			if _fid.name != template_key.name:
				continue
			return _fid
		return None

	def _template_is_intrinsic_generic(template_key: "FunctionKey") -> bool:
		_fid = _template_fn_id_for_key(template_key)
		if _fid is None or signatures_by_id is None:
			return False
		_sig = signatures_by_id.get(_fid)
		return _sig is not None and bool(getattr(_sig, "is_intrinsic", False))

	# Arc runtime boundary (Stage 2) — helper-template mapping.
	# Each `@intrinsic` Arc method (ARC_CLONE / ARC_GET / ARC_DESTROY)
	# has a private generic helper carrying the concrete-T body.  When
	# an intrinsic-template call site is instantiated with a concrete
	# T, we redirect the instantiation to the helper template; the
	# bodyless intrinsic template itself is never monomorphized.
	_ARC_HELPER_NAME_BY_KIND_VALUE: dict[str, str] = {
		"arc_clone": "_arc_clone_impl",
		"arc_get": "_arc_get_impl",
		"arc_destroy": "_arc_destroy_impl",
	}

	def _arc_helper_template_key_for_intrinsic(template_key: "FunctionKey") -> "FunctionKey | None":
		_fid = _template_fn_id_for_key(template_key)
		if _fid is None or signatures_by_id is None:
			return None
		_sig = signatures_by_id.get(_fid)
		if _sig is None or not bool(getattr(_sig, "is_intrinsic", False)):
			return None
		_kind = getattr(_sig, "intrinsic_kind", None)
		_kind_val = getattr(_kind, "value", None) if _kind is not None else None
		_helper_name = _ARC_HELPER_NAME_BY_KIND_VALUE.get(_kind_val) if _kind_val is not None else None
		if _helper_name is None:
			return None
		for _helper_fid, _helper_key in function_keys_by_fn_id.items():
			if _helper_fid.module != "std.concurrent":
				continue
			if _helper_fid.name != _helper_name:
				continue
			return _helper_key
		return None

	# Arc runtime boundary — call-site → helper-instantiation map.
	# Populated below during `_queue_instantiations` and consumed by
	# `hir_to_mir._lower_method_call_with_info`'s INTRINSIC path so
	# the lowering knows which concrete `_arc_*_impl__inst__<T>` to
	# call without having to re-derive T itself.
	arc_helper_inst_fn_by_callsite: dict[tuple[FunctionId, int], FunctionId] = {}

	def _is_fat_arc_intrinsic_type_args(type_args: tuple[TypeId, ...]) -> bool:
		"""Stage 3 fat-layout predicate for Arc intrinsic call sites.

		Arc intrinsic templates (`arc_clone`/`arc_get`/`arc_destroy`)
		have a single type parameter which at a concrete call site is
		the `T` in `Arc<T>`.  Returns True when that T is an interface
		AND the Stage 3 activation flag is on — which matches
		hir_to_mir's `is_arc_fat_layout_instance` gate: on the fat
		path, `_lower_arc_fat_intrinsic_call` emits direct MIR
		(bump/drop via the Slice 1 non-generic helpers plus
		`ArcFatGet`) with no generic `_arc_*_impl<I>` helper needed.
		"""
		if not STAGE3_FAT_ARC_ACTIVE:
			return False
		if shared_type_table is None or len(type_args) != 1:
			return False
		t_def = shared_type_table.get(type_args[0])
		return t_def.kind is TypeKind.INTERFACE

	def _queue_instantiations(caller_fn_id: "FunctionId", typed_fn: object) -> None:
		inst_map = getattr(typed_fn, "instantiations_by_callsite_id", None)
		if not isinstance(inst_map, dict):
			inst_map = {}
		inst_map_by_node = getattr(typed_fn, "instantiations_by_node_id", None)
		if not isinstance(inst_map_by_node, dict):
			inst_map_by_node = {}
		# Callsite-keyed items route to the Arc helper map; node-keyed
		# items (which include non-method sites without callsite_id)
		# are only relevant to normal monomorphization.
		for csid, inst in list(inst_map.items()):
			type_args = tuple(getattr(inst, "type_args", ()) or ())
			if not type_args:
				continue
			template_key = getattr(inst, "target_key", None)
			if not isinstance(template_key, FunctionKey):
				continue
			if _template_is_intrinsic_generic(template_key):
				# Arc runtime boundary: redirect to helper template.
				# The bodyless intrinsic template itself is never
				# monomorphized; the helper carries the concrete-T
				# implementation.  Record the helper-inst fn_id so
				# hir_to_mir can emit a direct call to it.
				_helper_key = _arc_helper_template_key_for_intrinsic(template_key)
				if _helper_key is not None:
					# Stage 3 fat-layout split — when receiver T is an
					# interface and the activation flag is on, the
					# fat lowering emits direct MIR and no thin
					# `_arc_*_impl<I>` helper is needed or wanted.
					# Skip the queue entirely — queuing it would
					# attempt to monomorphize the thin helper body
					# against a fat-layout Arc<I>, which would
					# type-check fail on the `buf` field access.
					if _is_fat_arc_intrinsic_type_args(type_args):
						continue
					_helper_handle = _request_instantiation(_helper_key, type_args)
					if isinstance(csid, int):
						arc_helper_inst_fn_by_callsite[(caller_fn_id, csid)] = _helper_handle.fn_id
				continue
			_request_instantiation(template_key, type_args)
		for inst in list(inst_map_by_node.values()):
			type_args = tuple(getattr(inst, "type_args", ()) or ())
			if not type_args:
				continue
			template_key = getattr(inst, "target_key", None)
			if not isinstance(template_key, FunctionKey):
				continue
			if _template_is_intrinsic_generic(template_key):
				# Node-id path: also redirect (no callsite mapping to
				# record — node-id records belong to non-method shapes
				# which currently do not reach Arc intrinsics, but we
				# still want the helper queued if one ever does).
				_helper_key = _arc_helper_template_key_for_intrinsic(template_key)
				if _helper_key is not None:
					# Mirror the callsite-keyed fat-Arc skip.
					if _is_fat_arc_intrinsic_type_args(type_args):
						continue
					_request_instantiation(_helper_key, type_args)
				continue
			_request_instantiation(template_key, type_args)

	for _fn_id, typed_fn in sorted(typed_fns_by_id.items(), key=lambda kv: function_symbol(kv[0])):
		_queue_instantiations(_fn_id, typed_fn)

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
			_inst_wraps_target = getattr(sig, "wraps_target_fn_id", None)
			# For wrapper instantiations, resolve wraps_target_fn_id to
			# the instantiated target.  The target was instantiated by
			# a previous drain with the same type_args but a different
			# FunctionKey (different name → different hash).  Look up
			# the target instantiation in inst_cache by computing the
			# target's _inst_key with the same type_args.
			if _inst_wraps_target is not None and getattr(sig, "is_wrapper", False):
				_target_template_key = function_keys_by_fn_id.get(_inst_wraps_target)
				if _target_template_key is not None:
					_target_inst_key = _inst_key(_target_template_key, handle.type_args)
					_target_handle = inst_cache.get(_target_inst_key)
					if _target_handle is not None:
						_inst_wraps_target = _target_handle.fn_id
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
				wraps_target_fn_id=_inst_wraps_target,
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
			_queue_instantiations(inst_fn_id, inst_result.typed_fn)
			handle.status = "emitted"

	_drain_instantiations()
	# Arc runtime boundary — publish callsite → helper-instantiation map.
	# `hir_to_mir._lower_method_call_with_info` reads
	# `type_table.arc_helper_inst_fn_by_callsite` to lower
	# `INTRINSIC(ARC_*)` call sites as direct calls to the appropriate
	# monomorphized `_arc_*_impl__inst__<T>` helper.  The map is keyed
	# by `(containing_fn_id, callsite_id)` so it survives template
	# instantiation (each Arc<T> usage in a generic caller gets its
	# own entry once the caller itself is monomorphized).
	if shared_type_table is not None:
		setattr(shared_type_table, "arc_helper_inst_fn_by_callsite", arc_helper_inst_fn_by_callsite)
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
			# Arc runtime boundary: skip intrinsic templates — their
			# call sites keep the `CallTarget.intrinsic(...)` target
			# from method resolution, and `_lower_method_call_with_info`
			# redirects to the `_arc_*_impl<T>` helper.  Forcing a
			# Direct-target rewrite here would replace the intrinsic
			# dispatch with a reference to a bodyless template.
			if _template_is_intrinsic_generic(template_key):
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
						declared_terminal_throws=bool(getattr(inst_sig, "declared_terminal_throws", False)),
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

	# K39: Post-instantiation rescan for generic Destructible impls.
	# After all _drain_instantiations() rounds, struct_instances may contain
	# new concrete types (e.g. ScopeGuard<Int>) that weren't present during
	# the initial destructor_fns population.  Re-scan and register them.
	if _generic_destructible_impls and shared_type_table is not None:
		_k39_added = 0
		for base_id, method_fn_id in _generic_destructible_impls:
			for inst_id, inst in shared_type_table.struct_instances.items():
				if inst.base_id != base_id:
					continue
				if shared_type_table.has_typevar(inst_id):
					continue
				if inst_id in destructor_fns:
					continue
				# Fat-layout skip — same rationale as the initial
				# destructible scan: the thin `_arc_destroy_impl<I>`
				# template is structurally invalid against the fat
				# layout, and the late fat-Arc synthesizer installs
				# these entries instead.
				if shared_type_table.is_arc_fat_layout_instance(inst_id):
					continue
				key = function_keys_by_fn_id.get(method_fn_id)
				if key is None:
					continue
				handle = _request_instantiation(key, tuple(inst.type_args))
				destructor_fns[inst_id] = handle.fn_id
				_k39_added += 1
		if _k39_added > 0:
			_drain_instantiations()
			shared_type_table.destructor_fns = destructor_fns

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
							declared_terminal_throws=bool(getattr(sig_for_throw, "declared_terminal_throws", False)),
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
			# Arc runtime boundary — post-pass normalization.
			#
			# Any DIRECT target pointing at an `@intrinsic` generic
			# template (Arc.clone, Arc.get, Arc::Destructible::destroy,
			# Arc.as_interface) gets rewritten to INTRINSIC here,
			# regardless of which upstream writer produced it.
			# Centralizes the invariant "intrinsic templates never
			# appear as Direct call targets after typecheck" in a
			# single pass so we don't have to patch every call-info
			# writer.
			for csid, info in list(call_info.items()):
				if info.target.kind is not CallTargetKind.DIRECT or info.target.symbol is None:
					continue
				_target_sig = signatures_by_id.get(info.target.symbol) if signatures_by_id is not None else None
				if _target_sig is None or not bool(getattr(_target_sig, "is_intrinsic", False)):
					continue
				_intrinsic_kind = getattr(_target_sig, "intrinsic_kind", None)
				if _intrinsic_kind is None:
					continue
				from lang.driftc.stage1.call_info import CallTarget as _CT, CallSig as _CS
				# The DIRECT CallInfo came from a generic-template
				# lookup; some writers default `can_throw=True` when
				# `sig.declared_can_throw` wasn't threaded in.  For
				# @intrinsic runtime-boundary methods
				# (Arc.clone/get/destroy/as_interface), the target
				# sig's `declared_can_throw` IS authoritative — all
				# four are `nothrow`.  Re-derive here so the nothrow
				# checker sees the correct `can_throw` when it walks
				# the rewritten INTRINSIC call.
				_declared = getattr(_target_sig, "declared_can_throw", None)
				_can_throw = bool(info.sig.can_throw) if _declared is None else bool(_declared)
				_sig = info.sig
				if _can_throw != bool(info.sig.can_throw):
					_sig = _CS(
						param_types=info.sig.param_types,
						user_ret_type=info.sig.user_ret_type,
						can_throw=_can_throw,
						includes_callee=info.sig.includes_callee,
						declared_terminal_throws=info.sig.declared_terminal_throws,
					)
				call_info[csid] = CallInfo(
					target=_CT.intrinsic(_intrinsic_kind),
					sig=_sig,
				)
			for info in call_info.values():
				# Arc bridge intrinsics intentionally carry the
				# template-level sig (with typevar params/return);
				# they lower via hir_to_mir's helper-redirect, not
				# via monomorphization.  Every OTHER intrinsic kind
				# must still pass the generic-survived check —
				# narrowly scoped exemption, see
				# `_typevar_callinfo_diags` at line ~1023.
				if (
					info.target.kind is CallTargetKind.INTRINSIC
					and info.target.intrinsic in _ARC_BRIDGE_INTRINSIC_KINDS
				):
					continue
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
		_apply_stdlib_escape_annotations(signatures_by_id, semantic_world=semantic_world)
		borrow_diags: list[Diagnostic] = []
		with _timed("borrow_check"):
			for _fn_id, typed_fn in typed_fns_by_id.items():
				bc = BorrowChecker.from_typed_fn(
					typed_fn,
					type_table=shared_type_table,
					signatures_by_id=signatures_by_id,
					enable_auto_borrow=True,
					semantic_world=semantic_world,
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
	#
	# When a call node has a `callsite_id` but no entry in
	# `call_info_by_callsite_id`, the call's resolution path bailed out
	# silently — either the type-checker emitted a non-error diagnostic
	# and returned without recording, or a synthesized HCall (e.g. the
	# `Share::share(&x)` for `captures(share x)`) was not visited by the
	# resolution pipeline.  The bare csid number is unactionable in the
	# field; surface enough HCall context (origin, loc, fn target, arg
	# shapes) on each orphan to point reviewers at the responsible site.
	for fn_id, typed_fn in typed_fns_by_id.items():
		block = getattr(typed_fn, "body", None)
		if not isinstance(block, H.HBlock):
			continue
		call_info_by_callsite = getattr(typed_fn, "call_info_by_callsite_id", None)
		if not isinstance(call_info_by_callsite, dict):
			continue
		missing_callsite: list[int] = []
		# Map orphan csid -> HCall expr for detail emission below.
		_orphan_exprs_by_csid: dict[int, H.HExpr] = {}
		for expr in _collect_call_nodes_by_id(block).values():
			csid = getattr(expr, "callsite_id", None)
			if not isinstance(csid, int):
				missing_callsite.append(-1)
				continue
			if csid not in call_info_by_callsite:
				missing_callsite.append(csid)
				_orphan_exprs_by_csid[csid] = expr
		if missing_callsite:
			missing_desc = ", ".join(str(c) for c in sorted(set(missing_callsite))[:6])
			# Build a per-orphan detail block — capped at the same 6
			# csids the summary message lists.  For each orphan: the
			# HCall's source location, `origin` annotation (e.g.
			# "share_capture"), its `fn` target repr (truncated),
			# and the per-arg type/repr.  This is the minimum info
			# needed to identify which compiler path created the HCall
			# without recording CallInfo.
			_detail_lines: list[str] = []
			_first_orphan_loc = None
			for _ocsid in sorted(set(missing_callsite))[:6]:
				_oexpr = _orphan_exprs_by_csid.get(_ocsid)
				if _oexpr is None:
					_detail_lines.append(
						f"  csid {_ocsid}: <not reachable from body walk>"
					)
					continue
				_oloc = getattr(_oexpr, "loc", None)
				if _first_orphan_loc is None:
					_first_orphan_loc = _oloc
				_oorigin = getattr(_oexpr, "origin", None)
				_ofn = getattr(_oexpr, "fn", None)
				_ofn_repr = repr(_ofn)[:240] if _ofn is not None else "<no fn>"
				_detail_lines.append(
					f"  csid {_ocsid}: type={type(_oexpr).__name__} "
					f"origin={_oorigin!r} loc={_oloc}"
				)
				_detail_lines.append(f"    fn={_ofn_repr}")
				_oargs = getattr(_oexpr, "args", None) or []
				for _ai, _arg in enumerate(_oargs):
					_detail_lines.append(
						f"    arg[{_ai}]: {type(_arg).__name__} {repr(_arg)[:160]}"
					)
			_detail_text = "\n".join(_detail_lines) if _detail_lines else "<no detail>"
			# Anchor the diagnostic span at the first orphan's loc when
			# available — gives the reviewer a source line to start from
			# instead of `<source>:None:None`.
			_anchor_span = None
			if _first_orphan_loc is not None:
				try:
					_anchor_span = Span.from_loc(_first_orphan_loc)
				except Exception:
					_anchor_span = None
			checked.diagnostics.append(
				Diagnostic(
					message=(
						f"internal: missing CallInfo for callsite ids in "
						f"'{function_symbol(fn_id)}': {missing_desc}\n"
						f"orphan call-node detail:\n{_detail_text}"
					),
					code="E_INTERNAL_MISSING_CALLSITE_CALLINFO",
					severity="error",
					phase="typecheck",
					span=_anchor_span,
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
	# K30: For re-instantiated generic templates (caller and target in same
	# package module), use callee's declared_can_throw directly.  The checker
	# may set can_throw=True on calls to nothrow package functions (boundary
	# wrapper assumption), but intra-module calls go to the raw impl, not the
	# wrapper.  Cross-module calls to exported entrypoints keep the checker's
	# can_throw because a boundary wrapper will intercept them.
	for fn_id_iter, typed_fn in typed_fns_by_id.items():
		callsite_map = getattr(typed_fn, "call_info_by_callsite_id", None)
		if not isinstance(callsite_map, dict):
			continue
		caller_module = getattr(fn_id_iter, "module", None)
		updated_callsite: dict[int, CallInfo] = {}
		for csid, info in callsite_map.items():
			call_can_throw = info.sig.can_throw
			if info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
				target_info = checked.fn_infos_by_id.get(info.target.symbol)
				if target_info is not None:
					target_module = getattr(info.target.symbol, "module", None)
					same_module = caller_module is not None and caller_module == target_module
					target_sig = target_info.signature
					is_boundary_target = target_sig is not None and target_sig.is_exported_entrypoint
					if same_module or not is_boundary_target:
						call_can_throw = bool(target_info.declared_can_throw)
					else:
						call_can_throw = call_can_throw or bool(target_info.declared_can_throw)
			if call_can_throw != info.sig.can_throw:
				info = CallInfo(
					target=info.target,
					sig=CallSig(
						param_types=info.sig.param_types,
						user_ret_type=info.sig.user_ret_type,
						can_throw=bool(call_can_throw),
						declared_terminal_throws=info.sig.declared_terminal_throws,
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
		# Uses the shared iterative HIR walker. Row #15 dedup.
		from lang.driftc.stage1.node_ids import iter_hir_walk
		ids: set[int] = set()
		for obj in iter_hir_walk(body):
			if isinstance(obj, H.HCast):
				node_id = getattr(obj, "node_id", 0)
				if node_id:
					ids.add(node_id)
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

	# Clear the has_drop cache immediately before MIR lowering so that
	# _param_drop_locals in HIRToMIR sees the same has_drop() answers as
	# the post-pass.  Without this, stale False entries from pre-K39 queries
	# persist and the cleanup-authoring path skips drops for types whose
	# destructor was registered by K39 generic instantiation.
	if shared_type_table is not None:
		shared_type_table._needs_drop_cache.clear()
		# Also clear the structural copy cache.  Stale True entries for
		# structs whose fields include Destructible types (VirtualThread,
		# Arc) can cause match-arm codegen to treat non-Copy payloads as
		# Copy, skipping the payload-moved flag and emitting a spurious
		# scrutinee drop that destroys the already-extracted payload.
		if hasattr(shared_type_table, "_copy_cache_structural"):
			shared_type_table._copy_cache_structural.clear()
	# Option B: boundary_ret_type_id propagation removed.  The consumer
	# compiles all package functions from HIR — no boundary ABI needed.
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
		if sig is not None and (getattr(sig, "is_intrinsic", False) or getattr(sig, "is_extern_c", False)):
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
		except MirLoweringError as err:
			_append_boundary_contract_diag(
				checked,
				phase="mir_lower",
				prefix=str(err),
				err=err,
				fn_id=fn_id,
				signatures_by_id=signatures_by_id,
				hir_block=hir_norm,
				origin_by_fn_id=origin_by_fn_id,
			)
			_assert_all_phased(checked.diagnostics, context="compile_stubbed_funcs")
			continue
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
			# Capture-mode keywords map 1-to-1 onto HCaptureKind.  No
			# `"auto"` row — the bareword `captures(x)` form was
			# removed at the parser level in 0.31.22 (silent-miscompile
			# class for escaping closures; see
			# `project_bareword_captures_removed.md`).  If a hidden /
			# regenerated lambda spec ever surfaces with `kind="auto"`
			# here, it's a stage1 capture-discovery bug that should be
			# fixed at the source, not silently lowered to REF.
			kind_map = {
				"ref": C.HCaptureKind.REF,
				"ref_mut": C.HCaptureKind.REF_MUT,
				"copy": C.HCaptureKind.COPY,
				"move": C.HCaptureKind.MOVE,
				"share": C.HCaptureKind.SHARE,
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
		# Hidden lambda typed_fns are registered AFTER the initial
		# `_queue_instantiations` loop at the top of this function.
		# Their own `instantiations_by_callsite_id` (populated by the
		# lambda-body type-check pass above) would otherwise never
		# reach `_queue_instantiations` — leaving
		# `arc_helper_inst_fn_by_callsite` without the (lambda_fn_id,
		# csid) entry the Stage 2 MIR lowering looks up for Arc
		# intrinsic callsites inside the lambda body.  Queue the
		# hidden lambda's instantiations here so the downstream
		# `_drain_instantiations()` call at the end of the hidden-
		# lambda loop processes any newly-queued items (Arc helper
		# templates, generic methods, etc).
		_queue_instantiations(spec.fn_id, hidden_typed_fn)
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
				new_sig = CallSig(param_types=tuple(param_ids), user_ret_type=ret_id, can_throw=sig.can_throw, includes_callee=sig.includes_callee, declared_terminal_throws=sig.declared_terminal_throws)
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

	# Drain generic instantiation requests triggered by hidden lambda
	# type-checking.  Lambda bodies may call generic methods through
	# boundary wrappers, creating instantiation requests that weren't
	# processed by the earlier drain rounds.
	_pre_lambda_drain_fids = set(mir_funcs_by_id.keys())
	_drain_instantiations()
	# Synthesize MIR bodies for newly-drained wrapper functions.
	# These are __wrap_method wrappers whose generic instantiation was
	# triggered by lambda bodies. Use direct MIR synthesis (same pattern
	# as line ~7096) instead of HIR-to-MIR lowering.
	for _ld_fn_id in list(normalized_hirs_by_id.keys()):
		if _ld_fn_id in _pre_lambda_drain_fids:
			continue
		if _ld_fn_id in mir_funcs_by_id:
			continue
		_ld_sig = signatures_by_id.get(_ld_fn_id)
		if _ld_sig is None or _ld_sig.param_type_ids is None:
			continue
		if not getattr(_ld_sig, "is_wrapper", False):
			continue
		_ld_target = getattr(_ld_sig, "wraps_target_fn_id", None)
		if _ld_target is None:
			continue
		_ld_pnames = list(_ld_sig.param_names or [])
		if len(_ld_pnames) != len(_ld_sig.param_type_ids):
			_ld_pnames = [f"p{i}" for i in range(len(_ld_sig.param_type_ids))]
		_ld_builder = make_builder(_ld_fn_id)
		_ld_builder.func.params = list(_ld_pnames)
		_ld_target_ret = _ld_sig.return_type_id
		_ld_target_sig = signatures_by_id.get(_ld_target)
		if _ld_target_sig is not None:
			_ld_target_ret = _ld_target_sig.return_type_id
		_ld_call_dest: M.ValueId | None
		if shared_type_table is not None and shared_type_table.is_void(_ld_target_ret):
			_ld_call_dest = None
		else:
			_ld_call_dest = _ld_builder.new_temp()
		_ld_builder.emit(M.Call(dest=_ld_call_dest, fn_id=_ld_target, args=_ld_pnames, can_throw=False))
		_ld_ok = _ld_builder.new_temp()
		_ld_builder.emit(M.ConstructResultOk(dest=_ld_ok, value=_ld_call_dest))
		_ld_builder.set_terminator(M.Return(value=_ld_ok))
		for _ld_p in _ld_pnames:
			_ld_builder.func.param_drop_status[_ld_p] = "forwarded_to_callee"
		mir_funcs_by_id[_ld_fn_id] = _ld_builder.func
		_register_synth_signature(_ld_fn_id, _ld_sig)
		# Register in checked.fn_infos_by_id so MIR validation passes.
		if _ld_fn_id not in checked.fn_infos_by_id:
			checked.fn_infos_by_id[_ld_fn_id] = make_fn_info(
				_ld_fn_id, _ld_sig,
				declared_can_throw=True if _ld_sig.declared_can_throw is None else bool(_ld_sig.declared_can_throw),
			)
	# Also register fn_infos for non-wrapper functions from the late drain.
	for _ld2_fn_id in list(normalized_hirs_by_id.keys()):
		if _ld2_fn_id in _pre_lambda_drain_fids:
			continue
		if _ld2_fn_id in checked.fn_infos_by_id:
			continue
		_ld2_sig = signatures_by_id.get(_ld2_fn_id)
		if _ld2_sig is not None:
			checked.fn_infos_by_id[_ld2_fn_id] = make_fn_info(
				_ld2_fn_id, _ld2_sig,
				declared_can_throw=True if _ld2_sig.declared_can_throw is None else bool(_ld2_sig.declared_can_throw),
			)

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
	_all_lambda_specs = list(type_checker.lambda_fn_specs())
	for spec in _all_lambda_specs:
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
		# Wrappers forward all params to callee — ownership transferred.
		for pname in param_names:
			builder.func.param_drop_status[pname] = "forwarded_to_callee"
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
			if not _run_mir_validator("canonicalize_mir_type_ids", lambda: _canonicalize_mir_type_ids(mir_funcs_by_id, shared_type_table)):
				if return_checked:
					if return_ssa:
						return {}, checked, None
					return {}, checked
				return {}
		# Assert FnInfo ↔ signature convergence after resync (debug mode).
		if os.environ.get("DRIFT_DEBUG_TYPEID_DIVERGENCE") == "1":
			_da2_divergences: list[str] = []
			for _da2_fn_id, _da2_info in checked.fn_infos_by_id.items():
				_da2_sig = _da2_info.signature
				if _da2_sig is not None:
					if _da2_info.return_type_id != _da2_sig.return_type_id:
						_da2_divergences.append(f"fn={_da2_fn_id} info.ret={_da2_info.return_type_id} sig.ret={_da2_sig.return_type_id}")
					if _da2_info.error_type_id != _da2_sig.error_type_id:
						_da2_divergences.append(f"fn={_da2_fn_id} info.err={_da2_info.error_type_id} sig.err={_da2_sig.error_type_id}")
			if _da2_divergences:
				raise AssertionError(f"[typeid-divergence] post-resync: {len(_da2_divergences)} divergence(s):\n" + "\n".join(_da2_divergences))

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
		# Phase 3B kickoff: build the ownership ledger on every lowered
		# function unconditionally, attach to func.  Site consumers
		# being swapped over (3B step 1: `drop_before_overwrite` in
		# string_arc) read it as the authoritative drop verdict.  Build
		# is cheap (worklist dataflow over MIR); cost is amortised
		# across the consumers that read it.  Observe-mode telemetry
		# (disagreement records to stderr) is still gated separately on
		# `DRIFT_COMPILER_DEBUG='{"ownership_ledger":true}'`.
		from lang.driftc.stage2.ownership_ledger import build_ledger as _ol_build
		for fn_id, func in mir_funcs_by_id.items():
			ledger = _ol_build(func, drop_policy=lambda _t: None)
			setattr(func, "_ownership_ledger", ledger)
		# Phase 4 site-2 patch 5 — per-field match-cleanup authoring.
		# HIR→MIR emits `M.MatchCleanupHook` at each arm's partial-move
		# cleanup point with pre-allocated `__match_partial_drop_N`
		# locals (registered so later site-1 `CleanupHook`s see them
		# as candidates).  This pass queries `field_verdict_at` per
		# candidate and, for `MUST_DROP`, authors the canonical
		# `VariantGetFieldAddr + LoadRef + StoreLocal + arm-end
		# MoveOut + DropValue` chain.  For non-`MUST_DROP` candidates
		# no chain is authored and the `drop_tmp` stays `UNINIT`, so
		# site-1's subsequent `verdict_at` sees `classify(UNINIT,
		# needs_drop=True) = MUST_NOT_DROP` and skips cleanly.  Runs
		# BEFORE site-1 cleanup_authoring with a ledger rebuild in
		# between so site 1 sees the authored per-field transitions.
		# Supersedes the Phase 3c `ownership_ledger_trim` veto pass
		# (retired at patch-5 step 7).
		# See `lang/driftc/stage2/match_cleanup_authoring.py`.
		if shared_type_table is not None:
			from lang.driftc.stage2.match_cleanup_authoring import (
				author_match_cleanup as _author_match_cleanup,
			)
			for fn_id, func in mir_funcs_by_id.items():
				_author_match_cleanup(func, type_table=shared_type_table)
				ledger = _ol_build(func, drop_policy=lambda _t: None)
				setattr(func, "_ownership_ledger", ledger)
		# Phase 4 site-1 — cleanup re-authoring pass.  All HIR→MIR
		# scope-drop sites (function-exit, `lower_function_body` /
		# `lower_block` fall-through, lambda-block exits, `HBreak` /
		# `HContinue`) emit `M.CleanupHook` markers.  This pass walks
		# each block, queries `verdict_at` for every candidate, and
		# emits the canonical `MoveOut + DropValue` sequences.  Site 1
		# drop authority is now `verdict_at`; HIR-side `_moved_locals`
		# / `_mark_moved` / `_scope_drop_verdict` / `_emit_scope_drops`
		# all retired in patch 6c (2026-04-24).  See
		# `lang/driftc/stage2/cleanup_authoring.py`.
		if shared_type_table is not None:
			from lang.driftc.stage2.cleanup_authoring import (
				author_cleanup as _author_cleanup,
			)
			for fn_id, func in mir_funcs_by_id.items():
				_author_cleanup(func, type_table=shared_type_table)
				# Patch 3 re-enables nested-scope `lower_block`
				# fall-through migration to `M.CleanupHook`, which
				# expands the set of CleanupHooks `_author_cleanup`
				# rewrites.  Rebuild the ledger so downstream consumers
				# (string_arc, drop_flags, site-2 trim) see the
				# post-authoring per-instruction state instead of the
				# stale pre-authoring snapshot.  Stale state caused
				# site-4 tripwire fires under patch 3 before the
				# rebuild was added.
				ledger = _ol_build(func, drop_policy=lambda _t: None)
				setattr(func, "_ownership_ledger", ledger)
		if drift_debug.enabled("ownership_ledger"):
			# Phase 3A observational: drain the decision events
			# recorded by sites 1/2 during HIR→MIR and emit
			# disagreement records to stderr.  Runs after ledger build,
			# before string_arc.
			from lang.driftc.stage2.ownership_ledger_reporter import (
				compare_events as _ol_compare_events,
				stderr_emit as _ol_stderr_emit,
			)
			for fn_id, func in mir_funcs_by_id.items():
				log = getattr(func, "_drop_decision_log", None)
				if log is None:
					continue
				events = log.drain()
				if not events:
					continue
				ledger = getattr(func, "_ownership_ledger")
				# QUARANTINED 3A APPROXIMATION — DO NOT REUSE IN 3B.
				# This callable answers the ledger reporter's
				# `needs_drop(local)` question with the raw
				# `TypeTable.has_drop` query rather than the canonical
				# `DropPolicy.needs_drop` axis.  The two diverge on
				# exactly two shapes that DropPolicy short-circuits:
				#
				#   1. `copy_status(ty) is True` — DropPolicy returns
				#      False (the pre-Phase-1 Copy-trait shortcut, the
				#      bug shape Phase 2a fixed); has_drop returns
				#      whatever the structural walk says.
				#   2. `_contains_dv_transitive(ty)` — DropPolicy
				#      returns True (DV destructors short-circuit
				#      Copy); has_drop may return False if the carrier
				#      type is otherwise drop-free.
				#
				# These divergences will surface in 3A telemetry as
				# `site_stricter` (case 1) and `ledger_stricter` (case
				# 2) records.  Task #5 triage owns a dedicated
				# "DropPolicy approximation noise" bucket for them; the
				# pin in `test_ownership_ledger_three_quadrant_pin.py`
				# uses the real DropPolicy callable so the gate is not
				# vulnerable to this approximation.
				#
				# 3B MUST replace this with a per-function
				# DropPolicy.needs_drop accessor (e.g. attached by
				# HIRToMIR alongside `_drop_decision_log`) before any
				# consumer is swapped onto the ledger.  Reusing
				# `has_drop` in a non-observational pass would re-open
				# the original Phase 2a UAF surface.
				def _needs_drop(local: str, f: M.MirFunc = func) -> bool:
					ty = f.local_types.get(local)
					if ty is None:
						return False
					try:
						return bool(shared_type_table.has_drop(ty))
					except Exception:
						return False
				_ol_compare_events(
					events,
					ledger,
					needs_drop=_needs_drop,
					emit=_ol_stderr_emit,
				)
		# Phase 3C — runtime drop-flag insertion for path-dependent
		# destructible locals.  Runs between HIR→MIR and string_arc.
		_drop_flags_mutated_fns: list[FunctionId] = []
		with _timed("drop_flags"):
			from lang.driftc.stage2.drop_flags import insert_drop_flags as _insert_drop_flags
			from lang.driftc.stage2.drop_policy_compute import compute_drop_policy as _compute_drop_policy
			_drop_policy_callable = lambda ty: _compute_drop_policy(shared_type_table, ty)
			for fn_id, func in mir_funcs_by_id.items():
				_new_func, _mutated = _insert_drop_flags(
					func,
					type_table=shared_type_table,
					drop_policy=_drop_policy_callable,
				)
				mir_funcs_by_id[fn_id] = _new_func
				if _mutated:
					_drop_flags_mutated_fns.append(fn_id)
		# Rebuild the ownership ledger AFTER drop_flags FOR THE
		# FUNCTIONS IT MUTATED.  The ledger attached by
		# `cleanup_authoring` is keyed by `(block, idx)` pairs from the
		# pre-drop_flags MIR.  When drop_flags inserts drop-flag init
		# instructions (`ConstBool(__df*, False)` +
		# `StoreLocal(__drop_flag_*, __df*)`) at block heads, every
		# subsequent index shifts.  string_arc then consults the
		# ledger using POST-drop_flags indices via
		# `_ledger.verdict_at((block, idx), local, ...)`, so without a
		# rebuild it reads stale state.  In particular, the verdict at
		# the first `StoreLocal(L, …)` for a freshly-declared
		# destructible local can come from a different instruction's
		# post-state and return `MUST_DROP` over an UNINIT slot —
		# string_arc then emits drop-before-overwrite reading uninit
		# memory and SSA crashes.  Pinned by
		# `lang/tests/driver/test_if_join_drop_destructor_uniform_move.py`.
		# Originally introduced by 0.31.9 Phase 3B step-1
		# (commit 94a9c44d "step 3 done"); the comment in
		# `string_arc.py:911-919` claiming "drop-before-overwrite
		# decisions only depend on per-local state at StoreLocal points
		# within the function body — none of those points are mutated
		# by drop_flags" was wrong: drop_flags shifts the indices.
		#
		# Narrowing: `insert_drop_flags` returns `(func, mutated)`.
		# Only mutated functions need the rebuild; un-mutated ones
		# returned early at the no-flag-locals branch and their
		# pre-existing ledger is still index-aligned.  This is the
		# common case (most functions have no path-dependent
		# destructible locals); the unconditional rebuild was 60-90 %
		# overhead vs the changed-only rebuild on representative
		# compiles.
		with _timed("ledger_rebuild_post_drop_flags"):
			for fn_id in _drop_flags_mutated_fns:
				func = mir_funcs_by_id[fn_id]
				ledger = _ol_build(func, drop_policy=lambda _t: None)
				setattr(func, "_ownership_ledger", ledger)
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
	# Post-pass: check for param drop disagreements between lowering time
	# and post-pass time.  With param_drop_status populated by the lowerer,
	# we no longer silently inject __postdrop_* drops.  Instead, any
	# disagreement (has_drop=True now but lowering said no_drop) produces
	# an explicit diagnostic.
	if shared_type_table is not None:
		shared_type_table._needs_drop_cache.clear()
		for func in mir_funcs_by_id.values():
			_postdrop_check_param_drops(func, shared_type_table, diagnostics=checked.diagnostics)
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


@_with_compile_recursion_headroom
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
	root_vt: bool = True,
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
	# Stage 3: synthesize per-I fat Arc destructor wrappers for the
	# test/driver codegen path.  Mirrors the package-build call at
	# `main()` — both paths must install destructor wrappers for fat
	# `Arc<I>` instances before LLVM sees the MIR.  Self-gated on
	# `STAGE3_FAT_ARC_ACTIVE` via `is_arc_fat_layout_instance`.
	if checked.type_table is not None:
		if ssa_funcs is None:
			ssa_funcs = {}
		_synthesize_fat_arc_destructor_wrappers(
			type_table=checked.type_table,
			mir_pool=mir_funcs,
			ssa_pool=ssa_funcs,
			fn_infos=fn_infos,
			signatures_by_id=signatures_by_id,
			external_signatures_by_id={},
			reachable=set(mir_funcs.keys()),
		)
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
			provenance_git_sha=_toolchain_git_sha(),
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
		module.emit_entry_wrapper(entry_sym, install_process_preamble=install_process_preamble_available, root_vt=root_vt)
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


def _pkg_exact_satisfies_range(exact_ver: str, range_ver: str) -> bool:
	"""True iff an exact `M.N.P` version satisfies an owner-declared range.

	**Input contract** — under v3, `range_ver` reaches this helper
	only after the `.dmp` loader has validated it against
	`dmir_pkg_v0::is_owner_declared_range`, so the only shapes that
	can legitimately occur at runtime are:

	- `range_ver == "M"` (single integer) → matches any `M.x.x`.
	- `range_ver == "M.N"` (two-part) → matches any `M.N.x`.

	`exact_ver` is similarly expected to be a well-formed `M.N.P`
	from a consumer-supplied `--dep` pin.

	**Fail-closed contract** — any malformed `range_ver` or
	`exact_ver` returns `False` (the sanity check fails, the caller
	emits a diagnostic, and the build aborts).  The helper
	intentionally does NOT fall back to literal string equality: on
	a clean-break format boundary, accepting a malformed shape by
	string-compare would mask the upstream validation bug that let
	it through.  Any `False` return due to malformed input is a bug
	elsewhere (loader, in-memory manipulation, or a new caller that
	skipped validation) and should be investigated rather than
	silently permitted.

	Kept inline in the compiler (rather than importing from
	`tools/drift_deploy`) because the compiler's package-loading
	layer must not depend on deploy tooling.  `driftc` still does
	NOT resolve versions — it only cross-checks that a
	consumer-supplied exact `--dep` pin is consistent with a
	transitively-loaded package's declared range.
	"""
	ex_parts = exact_ver.split(".")
	rg_parts = range_ver.split(".")
	# Fail-closed on malformed shapes — see contract above.
	if not all(p.isdigit() for p in ex_parts) or not all(p.isdigit() for p in rg_parts):
		return False
	if len(ex_parts) != 3:
		return False  # exact_ver must be M.N.P
	if len(rg_parts) == 1:
		return ex_parts[0] == rg_parts[0]
	if len(rg_parts) == 2:
		return ex_parts[0] == rg_parts[0] and ex_parts[1] == rg_parts[1]
	return False  # range_ver must be "M" or "M.N" — 3+ parts is malformed


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



@_with_compile_recursion_headroom
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
	parser.add_argument(
		"--source-content-id",
		type=str,
		default=None,
		help=(
			"Canonical source-content id for the artifact, computed by "
			"drift_deploy from stable source inputs (see "
			"tools.drift_deploy.source_attestation.compute_artifact_source_content_id). "
			"Stamped verbatim into the .dmp manifest as 'source_content_id'. "
			"Required for source-rebuild certification; optional for byte-only "
			"consumption."
		),
	)
	parser.add_argument("-g", "--debug-info", action="store_true", help="Emit debug info in generated LLVM (DWARF)")
	parser.add_argument("--no-debug-info", action="store_true", help="Disable debug info emission")
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
	parser.add_argument("--link-lib", action="append", default=[], metavar="LIB",
		help="Link against library (passes -l<LIB> to linker)")
	parser.add_argument("--link-search", action="append", default=[], metavar="DIR",
		help="Add library search path (passes -L<DIR> to linker)")
	parser.add_argument("--link-obj", action="append", default=[], metavar="FILE",
		help="Link additional object file")
	parser.add_argument("--native-link-lib", action="append", default=[], metavar="LIB",
		help="Declare native library dependency in emitted package (repeatable; --emit-package only)")
	parser.add_argument("--package-dep", action="append", default=[], metavar="NAME=VERSION",
		help="Declare Drift package dependency in emitted package (repeatable; --emit-package only)")
	parser.add_argument("--no-package-native-deps", action="store_true",
		help="Suppress auto-linking of native deps declared by consumed packages")
	parser.add_argument("--dep", action="append", default=[], metavar="PKG@VERSION",
		help="Select exact dependency version for consumed package (repeatable; e.g., --dep net.tls@0.3.0)")
	args = parser.parse_args(argv)
	# Dual-runtime workstream (step 4): the production default lane is
	# "normal" (optimized, no debug info, links the unsuffixed runtime
	# archive).  `DRIFT_DEBUG=1` flips into the explicit "debug-style" lane
	# (no -O2, links the `_debug`-infix runtime archive).  This env var is
	# the canonical mechanism — `drift build --debug` translates the flag
	# into the same env on the driftc subprocess.
	#
	# DWARF emission (`-g` / `--debug-info` / `--no-debug-info`) is an
	# orthogonal control: a normal-lane build can still opt into debug info
	# on the command line, and a debug-style build can suppress it.
	debug_style_runtime = _env_true("DRIFT_DEBUG")
	debug_enabled = debug_style_runtime
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

		# ── Parse --dep before discovery ──────────────────────────────
		# --dep is the exclusive allowlist: only listed packages are
		# discovered, loaded, and trust-verified from --package-root.
		# --package-root without --dep is an error.
		_version_pins: dict[str, str] = {}
		for _pin_spec in getattr(args, "dep", []):
			if "@" not in _pin_spec:
				msg = f"--dep requires PKG@VERSION format, got: {_pin_spec}"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": None, "line": None, "column": None}]}))
				else:
					print(f"error: {msg}", file=sys.stderr)
				return 1
			_pin_name, _pin_ver = _pin_spec.split("@", 1)
			if not _pin_name or not _pin_ver:
				msg = f"--dep requires non-empty PKG and VERSION, got: {_pin_spec}"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": None, "line": None, "column": None}]}))
				else:
					print(f"error: {msg}", file=sys.stderr)
				return 1
			if _pin_name in _version_pins:
				msg = f"--dep specified twice for '{_pin_name}'"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": None, "line": None, "column": None}]}))
				else:
					print(f"error: {msg}", file=sys.stderr)
				return 1
			_version_pins[_pin_name] = _pin_ver

		if not _version_pins:
			msg = "--package-root requires at least one --dep PKG@VERSION"
			if args.json:
				print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": None, "line": None, "column": None}]}))
			else:
				print(f"error: {msg}", file=sys.stderr)
			return 1

		_dep_allowlist: set[str] = set(_version_pins.keys())
		_self_pkg_id = str(args.package_id) if args.package_id else None

		# ── Discover + pre-filter before load ─────────────────────────
		# Only discover and load packages whose package_id is in the
		# --dep allowlist AND whose filesystem version directory matches
		# the pinned version.  The directory name is the trusted version
		# signal (not the embedded manifest, which could be tampered).
		# Unrelated packages and non-matching versions are never loaded,
		# never trust-verified, and cannot fail or collide with the build.
		from lang.driftc.packages.dmir_pkg_v0 import peek_package_id
		package_files = discover_package_files(list(args.package_roots))
		_candidate_files: list[Path] = []
		for _pf in package_files:
			_peeked_id = peek_package_id(_pf)
			# `.zdmp` is the authoritative published artifact.  A
			# `.zdmp` that exists but cannot be peeked is a bad
			# published package — corrupt compression, truncated
			# header, pre-0.29 metadata, wrong magic, whatever the
			# cause.  Fail loudly, naming the file, so the user
			# reinstalls or republishes.  No fallback to a same-stem
			# `.dmp` sibling (that masks bad deploys); no silent
			# skip regardless of whether the `.zdmp` happens to sit
			# inside an allowlisted dep directory (unrelated bad
			# artifacts under a package root still indicate a broken
			# publish and should not be quietly ignored).
			if _peeked_id is None and _pf.suffix == ".zdmp":
				msg = (
					f"failed to load published package {_pf}: unreadable "
					f"or invalid metadata.  The .zdmp is the authoritative "
					f"published artifact and MUST load cleanly — no "
					f"fallback to a same-stem .dmp sibling, no silent "
					f"skip.  Reinstall or republish this package."
				)
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": str(_pf), "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			# Plain `.dmp` under a package root can still be an
			# unrelated/stray file; skip quietly.  When the user
			# explicitly asked for a specific package via `--dep`,
			# the later version-selection step surfaces a "not
			# found under package roots" diagnostic.
			if _peeked_id is None:
				continue
			# Standard-layout identity enforcement: if the file sits in a
			# directory named after a requested dep (<root>/<dep_id>/<ver>/),
			# the manifest package_id MUST match.  A mismatch indicates
			# tampering or misconfigured package root.
			_fs_grandparent = _pf.parent.parent.name
			if _fs_grandparent in _dep_allowlist and _peeked_id != _fs_grandparent:
				msg = (
					f"package identity mismatch: artifact at "
					f".../{_fs_grandparent}/{_pf.parent.name}/ claims package_id "
					f"'{_peeked_id}' in manifest (expected '{_fs_grandparent}')"
				)
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
				else:
					print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			if _peeked_id not in _dep_allowlist:
				continue  # not a requested dependency
			# Version prefilter: use the filesystem directory structure as
			# the trusted version signal when the standard layout is present.
			# Standard layout: <root>/<pkg_id>/<version>/<pkg>.dmp
			# When the grandparent dir name matches the package_id, the
			# parent dir is the version — skip non-matching versions.
			# For flat layouts (e.g., test fixtures), skip the version
			# check and let the load/verify step handle selection.
			_pinned_ver = _version_pins.get(_peeked_id)
			if _pinned_ver:
				_fs_grandparent = _pf.parent.parent.name
				if _fs_grandparent == _peeked_id:
					_fs_version = _pf.parent.name
					if _fs_version != _pinned_ver:
						continue  # wrong version directory — skip
			# Pre-load version identity check: for standard-layout roots,
			# the manifest version must agree with the directory name.
			# Package_id mismatch is already caught above.
			if _fs_grandparent == _peeked_id:
				from lang.driftc.packages.dmir_pkg_v0 import peek_package_id_and_version as _peek_id_ver
				_peeked_full = _peek_id_ver(_pf)
				if _peeked_full is not None:
					_, _manifest_ver = _peeked_full
					_fs_ver = _pf.parent.name
					if _manifest_ver != _fs_ver:
						msg = (
							f"package identity mismatch: '{_peeked_id}' at path "
							f".../{_fs_grandparent}/{_fs_ver}/ claims version "
							f"'{_manifest_ver}' in manifest (expected '{_fs_ver}')"
						)
						if args.json:
							print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
						else:
							print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
			if _self_pkg_id and _peeked_id == _self_pkg_id:
				continue  # self-exclusion (source build of this package)
			_candidate_files.append(_pf)

		for pkg_path in _candidate_files:
			# Integrity + trust verification happens here, only for
			# packages that matched the --dep allowlist.
			try:
				_loaded = load_package_v0_with_policy(pkg_path, policy=policy)
			except (ValueError, OSError) as err:
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
			except Exception as err:
				msg = f"failed to load package '{pkg_path}': {err}"
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

			loaded_pkgs.append(_loaded)

		# Determinism: package discovery order (filenames, rglob ordering, CLI
		# `--package-root` ordering) must not affect compilation results. Sort loaded
		# packages by the module ids they provide, which is a content-derived key and
		# independent of filesystem paths.
		loaded_pkgs.sort(key=lambda p: tuple(sorted(p.modules_by_id.keys())))

		# ── Version selection on loaded (allowlisted) packages ────────
		# Runs BEFORE the transitive sanity check: flat package roots
		# can supply multiple versions for an allowlisted pkg id, and
		# only the `--dep`-selected exact version can validly drive
		# downstream semantic checks.  Sanity-checking `required_deps`
		# on an unselected duplicate would falsely fail the compile
		# when that unselected version happens to list an extra
		# transitive — even though the selected version is perfectly
		# consistent with the consumer's lock.
		if loaded_pkgs:
			_pkgs_by_id: dict[str, list] = {}
			for _pkg in loaded_pkgs:
				_pid = _pkg.manifest.get("package_id", "")
				_pkgs_by_id.setdefault(_pid, []).append(_pkg)
			_filtered: list = []
			_used_pins: set[str] = set()
			for _pid, _pkg_list in _pkgs_by_id.items():
				if _pid in _version_pins:
					_used_pins.add(_pid)
					_want_ver = _version_pins[_pid]
					_matched = [p for p in _pkg_list if p.manifest.get("package_version") == _want_ver]
					if not _matched:
						_available = sorted({p.manifest.get("package_version", "?") for p in _pkg_list})
						msg = f"package '{_pid}' version '{_want_ver}' not found under package roots (available: {', '.join(_available)})"
						if args.json:
							print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
						else:
							print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
					_filtered.extend(_matched)
				# else: package_id matched allowlist but wasn't in _version_pins
				# — should not happen since _dep_allowlist == _version_pins.keys()
			# Check for pins that didn't match any discovered package.
			# Exclude self-package — it was intentionally filtered by self-exclusion.
			_unmatched_pins = set(_version_pins.keys()) - _used_pins
			if _self_pkg_id:
				_unmatched_pins.discard(_self_pkg_id)
			if _unmatched_pins:
				for _upin in sorted(_unmatched_pins):
					msg = f"package '{_upin}' version '{_version_pins[_upin]}' not found under package roots"
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
					else:
						print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			loaded_pkgs = _filtered
		else:
			# No packages loaded — check for unmatched non-self pins.
			_all_unmatched = set(_version_pins.keys())
			if _self_pkg_id:
				_all_unmatched.discard(_self_pkg_id)
			if _all_unmatched:
				for _upin in sorted(_all_unmatched):
					msg = f"package '{_upin}' version '{_version_pins[_upin]}' not found under package roots"
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
					else:
						print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1

		# ── Transitive dependency sanity check ────────────────────────
		# driftc is an exact loader, not a resolver.  The consumer's
		# `drift prepare` produced the full transitive graph as exact
		# `--dep PKG@VERSION` pins; every `required_deps` entry a
		# loaded package declares MUST already be in the allowlist.
		# If it isn't, `drift prepare` / `drift build` skipped it,
		# and driftc cannot invent an exact pin from the range
		# metadata.
		#
		# For each `required_deps` entry on each loaded package:
		# - If the name is in `_dep_allowlist`, verify the existing
		#   exact pin satisfies the declared range.  Mismatch =
		#   hard error.
		# - If the name is NOT in `_dep_allowlist`, hard-fail with a
		#   "missing exact --dep" diagnostic pointing at
		#   `drift prepare`.  No auto-expansion from filesystem — that
		#   would make driftc a second resolver.
		# - Skip the self-pkg id (self-exclusion rule).
		#
		# Runs AFTER version selection — the sanity check only looks
		# at the pin-selected exact version per pkg id, so unselected
		# duplicate versions in flat package roots cannot falsely
		# fail the compile (K Finding 3).
		#
		# Pre-cut `.dmp`s still carry the legacy `package_deps` key —
		# the loader (`dmir_pkg_v0._parse_required_deps`) rejects
		# those upstream.  Consume the typed
		# `LoadedPackage.required_deps` list directly here rather than
		# re-parsing the raw manifest JSON, so this pass never becomes
		# a second parser with slightly different behaviour.
		for _loaded_pkg in loaded_pkgs:
			_loaded_pkg_id = _loaded_pkg.manifest.get("package_id", "")
			for _pd in _loaded_pkg.required_deps:
				_pd_name = _pd.name
				_pd_ver = _pd.version
				if _pd_name == _self_pkg_id:
					continue  # self-exclusion
				if _pd_name not in _dep_allowlist:
					msg = (
						f"package '{_loaded_pkg_id}' declares required_deps "
						f"entry '{_pd_name}' ({_pd_ver}) but no --dep is "
						f"pinned for it; driftc is an exact loader and "
						f"cannot invent a version from a range.  Run "
						f"`drift prepare` / `drift build` so the complete "
						f"transitive graph reaches driftc as exact --dep pins."
					)
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
					else:
						print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1
				# In allowlist — verify the exact pin satisfies the
				# declared range.  `--dep` carries the exact `M.N.P`
				# from the consumer's v3 lock; `required_deps` carries
				# the producer's manifest-level range (`M` or `M.N`).
				# driftc only COMPARES the two here — it does not try
				# to pick a different version from package roots.
				# Resolution is `drift prepare`'s job.
				_pin = _version_pins.get(_pd_name, "")
				if _pin and not _pkg_exact_satisfies_range(_pin, _pd_ver):
					msg = (
						f"transitive dependency version conflict for "
						f"'{_pd_name}': the --dep pin '{_pin}' does not "
						f"satisfy the required_deps range '{_pd_ver}' "
						f"declared by package '{_loaded_pkg_id}'.  "
						f"driftc is an exact loader and will not pick a "
						f"different version from the package roots — "
						f"re-run `drift prepare` / `drift build` to "
						f"regenerate a consistent lock."
					)
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
					else:
						print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1

		# Enforce "single version per package id per build" and deduplicate
		# same-version packages discovered from multiple --package-root dirs.
		# When the same package_id@version@target appears in more than one
		# root, one copy is kept and the rest are dropped.  Selection order
		# is determined by discover_package_files (globally sorted by path),
		# not by --package-root CLI order.
		pkg_id_map: dict[str, tuple[str, str]] = {}  # package_id -> (version, target)
		_deduped_pkgs: list = []
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
			prev = pkg_id_map.get(pkg_id)
			if prev is None:
				pkg_id_map[pkg_id] = (pkg_ver, pkg_target)
				_deduped_pkgs.append(pkg)
				continue
			prev_ver, prev_target = prev
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
			# Same package_id, version, and target from a different path
			# (e.g. multiple --package-root dirs containing the same artifact,
			# or a freshly-staged copy alongside a certified release layout).
			# Keep the first copy, skip the duplicate.
		loaded_pkgs = _deduped_pkgs

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

	# Extract exception schemas from loaded packages so that exception types
	# referenced in function signatures are resolved as Error during parsing.
	external_exception_schemas: dict[str, tuple[str, list[str]]] = {}
	if loaded_pkgs:
		for pkg in loaded_pkgs:
			for _mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				payload_tt = payload.get("type_table")
				if not isinstance(payload_tt, dict):
					continue
				pkg_exc = payload_tt.get("exception_schemas")
				if isinstance(pkg_exc, dict):
					for fqn, schema in pkg_exc.items():
						if isinstance(schema, (list, tuple)) and len(schema) == 2:
							external_exception_schemas.setdefault(fqn, (str(schema[0]), [str(f) for f in schema[1]]))

	# ── Early type-table linking ────────────────────────────────────
	# Import package type definitions into a fresh TypeTable BEFORE
	# the parser runs.  This eliminates the temporal split where the
	# parser resolved types against an incomplete TypeTable and then a
	# post-parse linking step patched up the gaps.  With early linking
	# the parser sees the full type universe from the start.
	pkg_typeid_maps: dict[Path, dict[int, int]] = {}
	pkg_tid_universes: dict[Path, frozenset[int]] = {}
	pre_linked_type_table: TypeTable | None = None
	if loaded_pkgs:
		from lang.driftc.packages.type_table_link_v0 import (
			decode_type_table_obj,
			import_type_tables_and_build_typeid_maps,
		)
		_tt_kwargs: dict = {}
		_wb = getattr(args, "target_word_bits", None)
		if _wb is not None:
			_tt_kwargs["word_bits"] = _wb
		pre_linked_type_table = TypeTable(**_tt_kwargs)
		_pre_pkg_id = str(args.package_id) if args.package_id else None
		if _pre_pkg_id is not None:
			pre_linked_type_table.package_id = _pre_pkg_id
		# Seed module→package ownership so NominalKeys are correct.
		if isinstance(external_module_packages, dict):
			for _mod, _pkg in external_module_packages.items():
				pre_linked_type_table.module_packages.setdefault(_mod, _pkg)
		# Seed canonical stdlib/lang module ownership so the linker assigns
		# the same package_id the parser will use for source-compiled stdlib.
		pre_linked_type_table.module_packages.setdefault("lang.core", "lang.core")
		pre_linked_type_table.module_packages.setdefault("lang.thread", "lang.core")
		pre_linked_type_table.module_packages.setdefault("lang.atomic", "lang.core")
		# lang.__internal is compiler-internal scaffolding — intentionally NOT
		# seeded here.  It stays local (populated by the parser as local_pkg).
		# Scan package type tables for any std.*/lang.* modules and seed them.
		for _pkg in loaded_pkgs:
			for _mid in _pkg.modules_by_id:
				if isinstance(_mid, str):
					if _mid.startswith("std."):
						pre_linked_type_table.module_packages.setdefault(_mid, "std")
					elif _mid.startswith("lang.") and _mid != "lang.__internal":
						pre_linked_type_table.module_packages.setdefault(_mid, "lang.core")
		# Also seed stdlib modules referenced in package type tables but not
		# directly provided by any loaded package (e.g. std.core types appear
		# in every package's serialized type table).
		for _pkg in loaded_pkgs:
			for _mid, _mod in _pkg.modules_by_id.items():
				_payload = _mod.payload
				if not isinstance(_payload, dict):
					continue
				_tt = _payload.get("type_table")
				if not isinstance(_tt, dict):
					continue
				for _td in (_tt.get("defs") or {}).values():
					if isinstance(_td, dict):
						_dmid = _td.get("module_id")
						if isinstance(_dmid, str):
							if _dmid.startswith("std."):
								pre_linked_type_table.module_packages.setdefault(_dmid, "std")
							elif _dmid.startswith("lang.") and _dmid != "lang.__internal":
								pre_linked_type_table.module_packages.setdefault(_dmid, "lang.core")
				break  # all modules share same type_table
		# Extract per-package type-table objects and run linking.
		_pkg_paths: list[Path] = []
		_pkg_tt_objs: list[dict[str, Any]] = []
		for pkg in loaded_pkgs:
			_pkg_tt_obj: dict[str, Any] | None = None
			for mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				tt = payload.get("type_table")
				if not isinstance(tt, dict):
					msg = f"package {_package_label()} module '{mid}' is missing type_table"
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
					else:
						print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
					return 1
				if _pkg_tt_obj is None:
					_pkg_tt_obj = tt
				else:
					if type_table_fingerprint(tt) != type_table_fingerprint(_pkg_tt_obj):
						msg = f"package {_package_label()} contains inconsistent type_table across modules"
						if args.json:
							print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<package>", "line": None, "column": None}]}))
						else:
							print(f"{_package_label()}:?:?: error: {msg}", file=sys.stderr)
						return 1
			if _pkg_tt_obj is None:
				continue
			_pkg_paths.append(pkg.path)
			_pkg_tt_objs.append(_pkg_tt_obj)
		if _pkg_tt_objs:
			try:
				_maps = import_type_tables_and_build_typeid_maps(_pkg_tt_objs, pre_linked_type_table)
			except ValueError as err:
				msg = f"failed to import package types: {err}"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
				else:
					print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
				return 1
			for _path, _tid_map, _tt_obj in zip(_pkg_paths, _maps, _pkg_tt_objs):
				pkg_typeid_maps[_path] = _tid_map
				pkg_tid_universes[_path] = frozenset(decode_type_table_obj(_tt_obj).defs.keys())
		# K27: Sync builtin TypeId caches on the pre-linked type table so
		# that ensure_void() (and other ensure_* calls) in the parser and
		# consumer signature normalization return the same TypeId that the
		# linked packages use, avoiding duplicate Void/Error/etc entries.
		if pre_linked_type_table is not None:
			for _tid, _td in pre_linked_type_table._defs.items():
				if _td.kind is TypeKind.VOID and getattr(pre_linked_type_table, "_void_type", None) is None:
					pre_linked_type_table._void_type = _tid

	# ── Create SemanticWorld ─────────────────────────────────────────
	# The world carries all semantic stores through the pipeline.
	# Initially populated with the pre-linked TypeTable (if packages
	# were loaded) and package metadata.  The parser and later phases
	# enrich it.
	from lang.driftc.core.semantic_world import SemanticWorld, WorldPhase
	# When packages were loaded, the pre-linked TypeTable has the correct
	# word_bits and package types.  For source-only builds (no packages),
	# leave type_table as None — the parser will create one with the
	# correct word_bits from its own arguments.
	semantic_world = SemanticWorld(
		type_table=pre_linked_type_table,
		pkg_typeid_maps=pkg_typeid_maps,
		pkg_tid_universes=pkg_tid_universes,
	)
	if loaded_pkgs:
		semantic_world.advance_to(WorldPhase.PACKAGES_READY)

	# ── Source parsing ───────────────────────────────────────────────
	semantic_world.advance_to(WorldPhase.SOURCE_INGRESS)
	package_id = str(args.package_id) if args.package_id else None
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		source_paths,
		module_paths=module_paths,
		external_module_exports=external_exports,
		external_module_packages=external_module_packages,
		external_exception_schemas=external_exception_schemas or None,
		package_id=package_id,
		stdlib_root=args.stdlib_root,
		test_build_only=bool(getattr(args, "test_build_only", False)),
		word_bits=getattr(args, "target_word_bits", None),
		semantic_world=semantic_world,
	)
	# Update world with parser outputs.
	semantic_world.type_table = type_table
	semantic_world.module_exports = module_exports
	semantic_world.module_deps = module_deps

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
				_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
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

	# Option B: no boundary wrapper injection.  The consumer compiles
	# all package functions from HIR — no pre-built wrappers needed.
	method_wrapper_specs: list[MethodWrapperSpec] = []
	wrapper_errors: list[str] = []
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
	# Post-parse fixups for package-loaded types.  Type table linking ran
	# before the parser (early linking above), so pkg_typeid_maps and
	# pkg_tid_universes are already populated.  The remaining work is
	# id_registry interning, signature/field canonicalization (safety net),
	# and exception catalog population.
	if loaded_pkgs:
		for base_id, schema in getattr(type_table, "struct_bases", {}).items():
			mod = getattr(schema, "module_id", None)
			name = getattr(schema, "name", None)
			if isinstance(mod, str) and isinstance(name, str):
				pkg = getattr(type_table, "module_packages", {}).get(mod, getattr(type_table, "package_id", None))
				_tk = TypeKey(package_id=pkg, module=mod, name=name, args=())
				if id_registry._type_key_to_id.get(_tk) is None:
					id_registry.intern_type(_tk, preferred=base_id)
		for base_id, schema in getattr(type_table, "variant_schemas", {}).items():
			mod = getattr(schema, "module_id", None)
			name = getattr(schema, "name", None)
			if isinstance(mod, str) and isinstance(name, str):
				pkg = getattr(type_table, "module_packages", {}).get(mod, getattr(type_table, "package_id", None))
				_tk = TypeKey(package_id=pkg, module=mod, name=name, args=())
				if id_registry._type_key_to_id.get(_tk) is None:
					id_registry.intern_type(_tk, preferred=base_id)

		# Stage 3 invariant: with early linking, no FORWARD_NOMINALs should
		# survive to this point.  All package types were declared before
		# the parser ran, and source-internal forward references were
		# resolved during the parser's pre-declaration sweep.
		# FORWARD_NOMINALs for generic type params, trait base types, and
		# abstract bounds are expected — they represent uninstantiated
		# type variables, not missing concrete declarations.  Only warn
		# about FORWARD_NOMINALs that look like concrete types (have a
		# module_id in the current package or stdlib).
		_local_or_std = {package_id, "std", "lang.core", "__local__"} if package_id else {"__local__", "std", "lang.core"}
		_fwd_survivors = [
			(tid, td.name, td.module_id)
			for tid, td in type_table._defs.items()
			if td.kind is TypeKind.FORWARD_NOMINAL
			and td.module_id is not None
			and type_table.module_packages.get(td.module_id, "") in _local_or_std
		]
		# Note: _fwd_survivors may be non-empty for generic type params
		# and trait bounds from packages — these are expected abstract
		# placeholders, not missing concrete declarations.
		# Populate exception_catalog with event codes from loaded packages so that
		# catch handlers in consumer code emit correct event codes for package exceptions.
		from lang.driftc.core.event_codes import event_code as _event_code
		for fqn in external_exception_schemas:
			if fqn not in exception_catalog:
				exception_catalog[fqn] = _event_code(fqn)

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
					if module_name is not None and "::" not in name and not bool(sd.get("is_extern_c", False)):
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
					# Canonicalize impl type params against the target struct.
					if impl_type_params:
						_sig_impl_target = sd.get("impl_target_type")
						if _sig_impl_target is not None:
							_sig_it_expr = decode_type_expr(_sig_impl_target)
							if _sig_it_expr is not None:
								_sig_te_name = getattr(_sig_it_expr, "name", None)
								_sig_te_mod = getattr(_sig_it_expr, "module_id", None) or module_name
								if _sig_te_name:
									_itp_map = {tp.name: tp.id for tp in impl_type_params}
									_sig_te_args = list(getattr(_sig_it_expr, "args", []) or [])
									_canon_map = type_table.canonicalize_impl_type_params(
										_itp_map, target_module=_sig_te_mod, target_name=_sig_te_name,
										target_args=_sig_te_args,
									)
									if _canon_map is not _itp_map:
										impl_type_params = [
											TypeParam(id=_canon_map.get(tp.name, tp.id), name=tp.name)
											for tp in impl_type_params
										]
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
					# Generic templates use resolve_opaque_type with type variable
					# bindings. Concrete sigs use tid_map-remapped raw ids (retained
					# in payload for generic/__inst__); override with TypeExpr only
					# when raw ids are absent or resolve to UNKNOWN/FORWARD_NOMINAL.
					_has_type_params = bool(type_params) or bool(impl_type_params)
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
							if _has_type_params or not isinstance(param_type_ids, list) or len(param_type_ids) != len(decoded):
								param_type_ids = [
									resolve_opaque_type(t, type_table, module_id=module_name, type_params=type_param_map)
									for t in decoded
								]
							else:
								for _pi, _pt in enumerate(decoded):
									_existing = param_type_ids[_pi]
									_td = type_table.get(_existing)
									if _td is None or _td.kind in (TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL):
										param_type_ids[_pi] = resolve_opaque_type(_pt, type_table, module_id=module_name, type_params=type_param_map)

					return_type = None
					return_raw = sd.get("return_type")
					if return_raw is not None:
						return_type = decode_type_expr(return_raw)
						if return_type is not None:
							if _has_type_params:
								ret_tid = resolve_opaque_type(return_type, type_table, module_id=module_name, type_params=type_param_map)
							else:
								_ret_td = type_table.get(ret_tid) if isinstance(ret_tid, int) else None
								if not isinstance(ret_tid, int) or _ret_td is None or _ret_td.kind in (TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL):
									ret_tid = resolve_opaque_type(return_type, type_table, module_id=module_name, type_params=type_param_map)

					# Resolve impl_target_type from TypeExpr; only override tid_map
					# when missing or unresolved (UNKNOWN/FORWARD_NOMINAL).
					impl_target_type_raw = sd.get("impl_target_type")
					if impl_target_type_raw is not None:
						it_expr = decode_type_expr(impl_target_type_raw)
						if it_expr is not None:
							_it_td = type_table.get(impl_tid) if isinstance(impl_tid, int) else None
							if not isinstance(impl_tid, int) or _it_td is None or _it_td.kind in (TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL):
								impl_tid = resolve_opaque_type(it_expr, type_table, module_id=module_name, type_params=type_param_map)

					# Resolve error_type from TypeExpr when present in payload.
					error_type_raw = sd.get("error_type")
					err_tid: TypeId | None = None
					if error_type_raw is not None:
						et_expr = decode_type_expr(error_type_raw)
						if et_expr is not None:
							_resolved_err_ext = resolve_opaque_type(et_expr, type_table, module_id=module_name, type_params=type_param_map)
							if type_table.get(_resolved_err_ext).kind not in (TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL):
								err_tid = _resolved_err_ext

					# K27: Terminal-throws signatures encode return_type=null in the
					# package format (declared_terminal_throws is the source of truth).
					# Normalize return_type_id to Void on the consumer side so call
					# registration, method resolution, and FnResult plumbing have a
					# valid TypeId.  The declared_terminal_throws flag is preserved.
					if ret_tid is None and bool(sd.get("declared_terminal_throws", False)):
						ret_tid = type_table.ensure_void()

					intrinsic_kind = None
					intrinsic_kind_raw = sd.get("intrinsic_kind")
					if isinstance(intrinsic_kind_raw, str):
						try:
							intrinsic_kind = IntrinsicKind(intrinsic_kind_raw)
						except ValueError:
							intrinsic_kind = None
					is_intrinsic = bool(sd.get("is_intrinsic", False)) or intrinsic_kind is not None

					_is_inst = "__inst__" in name and not type_params and not impl_type_params
					sig = FnSignature(
						name=name,
						module=module_name,
						method_name=sd.get("method_name"),
						param_names=sd.get("param_names"),
						param_mutable=param_mutable,
						param_type_ids=param_type_ids,
						return_type_id=ret_tid,
						error_type_id=err_tid,
						declared_can_throw=sd.get("declared_can_throw"),
						# Phase 3 of terminal-`throws`: round-trip both
						# auto-try and bare-terminal flags. Old packages
						# (pre-Phase-3) lack these fields; default to False
						# for forward compatibility.
						declared_throws=bool(sd.get("declared_throws", False)),
						declared_terminal_throws=bool(sd.get("declared_terminal_throws", False)),
						declared_unsafe=bool(sd.get("declared_unsafe", False)) or None,
						is_intrinsic=is_intrinsic,
						intrinsic_kind=intrinsic_kind,
						is_method=bool(sd.get("is_method", False)),
						self_mode=sd.get("self_mode"),
						impl_target_type_id=impl_tid,
						is_pub=bool(sd.get("is_pub")),
						is_wrapper=bool(sd.get("is_wrapper", False)),
						wraps_target_fn_id=wraps_fn_id,
						is_exported_entrypoint=bool(sd.get("is_exported_entrypoint", False)),
						is_extern_c=bool(sd.get("is_extern_c", False)),
						param_types=param_types,
						return_type=return_type,
						type_params=type_params,
						impl_type_params=impl_type_params,
						impl_target_type_args=impl_target_type_args,
						is_instantiation=_is_inst,
					)
					# Stage 8.2: dual-path assertion for external signatures.
					# Only runs when raw TypeId fields are present (v0 payloads or
					# generic/__inst__ sigs in v1).  For v1 concrete sigs, raw fields
					# are intentionally absent.
					_has_raw_baseline_82 = sd.get("param_type_ids") is not None or sd.get("return_type_id") is not None
					if _TYPEXPR_DEBUG and not _has_type_params and not _is_inst and _has_raw_baseline_82:
						_sig_label = f"ext_sig:{name}"
						_raw_ptids_82 = sd.get("param_type_ids")
						_tm_ptids_82 = [tid_map.get(int(x), int(x)) for x in _raw_ptids_82] if isinstance(_raw_ptids_82, list) else None
						_raw_ret_82 = sd.get("return_type_id")
						_tm_ret_82 = tid_map.get(int(_raw_ret_82), int(_raw_ret_82)) if isinstance(_raw_ret_82, int) else None
						_raw_impl_82 = sd.get("impl_target_type_id")
						_tm_impl_82 = tid_map.get(int(_raw_impl_82), int(_raw_impl_82)) if isinstance(_raw_impl_82, int) else None
						if param_type_ids is not None and _tm_ptids_82 is not None and len(param_type_ids) == len(_tm_ptids_82):
							for _pi, (_te_tid, _tm_tid) in enumerate(zip(param_type_ids, _tm_ptids_82)):
								_assert_typexpr_tid_match(f"{_sig_label}:param[{_pi}]", _te_tid, _tm_tid, type_table)
						if _tm_ret_82 is not None:
							_assert_typexpr_tid_match(f"{_sig_label}:return", ret_tid, _tm_ret_82, type_table)
						if impl_tid is not None or _tm_impl_82 is not None:
							_assert_typexpr_tid_match(f"{_sig_label}:impl_target", impl_tid, _tm_impl_82, type_table)
						# Validate error_type structural correctness.
						if err_tid is not None:
							try:
								_err_td_82 = type_table.get(err_tid)
								if _err_td_82.kind is not TypeKind.ERROR:
									raise AssertionError(
										f"TYPEXPR_RESOLVE error_type resolved to {_err_td_82.kind.name} "
										f"(expected ERROR) in {_sig_label}"
									)
							except (KeyError, IndexError):
								raise AssertionError(
									f"TYPEXPR_RESOLVE error_type resolved to invalid TypeId {err_tid} "
									f"in {_sig_label}"
								)

					if module_name is not None and module_name in modules:
						continue
						# Skip hidden lambda callbacks — they will be re-derived
						# by the hidden lambda processing loop when the parent
						# function's HIR is type-checked by the consumer.  Pre-loading
						# them causes signature collisions with the re-derived version.
					_fn_name_check = getattr(fn_id, "name", "") or name
					if "__lambda_cb_" in _fn_name_check or "__lambda_" in _fn_name_check:
						continue
					external_signatures_by_name[name] = sig
					if symbol not in external_signatures_by_symbol:
						external_signatures_by_symbol[symbol] = sig
					if fn_id not in external_signatures_by_id:
						external_signatures_by_id[fn_id] = sig

		# Option B: no boundary wrapper injection for external methods.
		# All package functions are compiled from HIR in the consumer's
		# context.  No __wrap_method stubs or FnResult ABI wrappers needed.
		# Option B: boundary_ret_type_id is no longer set on external
		# signatures.  All package functions are compiled from HIR in
		# the consumer's context — no boundary ABI wrappers needed.

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

		# K26: Interface types (e.g., Sink) don't appear in trait_metadata
		# because they're registered in the type table, not the trait world.
		# Mark their trait_keys as "missing" so trait index validation doesn't
		# reject impl references to interface-based traits.
		_trait_def_keys = {getattr(td, "key", None) for td in external_trait_defs}
		for impl in external_impl_metas:
			tk = getattr(impl, "trait_key", None)
			if tk is not None and tk not in _trait_def_keys:
				external_missing_traits.add(tk)

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
							if os.environ.get("DRIFTC_DEBUG_FINGERPRINT") == "1":
								import json as _json
								_dbg = compute_template_decl_fingerprint_debug(
									sig,
									declared_name=fn_key.name,
									module_id=fn_key.module_path,
									require_expr=req if req is not None else None,
									default_package=pkg_id,
									module_packages=getattr(type_table, "module_packages", None),
								)
								print(
									f"  [consume-time] stored_fp={fn_key.decl_fingerprint}\n"
									f"  [consume-time] computed_fp={_dbg['decl_fingerprint']}\n"
									f"  [consume-time] fingerprint_obj={_json.dumps(_dbg['fingerprint_obj'], indent=2, default=str)}\n"
									f"  [consume-time] require_canonical={_json.dumps(_dbg['require_canonical'], indent=2, default=str)}",
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
						type_params=list(getattr(impl, "impl_type_params", []) or []),
						loc=getattr(impl, "loc", None),
					)
				)
				world.impls_by_trait.setdefault(trait_key, []).append(impl_id)
				world.impls_by_target_head.setdefault(head_key, []).append(impl_id)
				world.impls_by_trait_target.setdefault((trait_key, head_key), []).append(impl_id)
				# If `trait_key` names a real INTERFACE type (verified
				# against the consumer's type table, not just inferred
				# from absence of trait metadata), ALSO register the
				# impl in the interface-impl index so `require T is I`
				# queries in generic code (e.g.
				# `Arc<T>.as_interface<I>()`'s clause) can prove it
				# across the package boundary.  Without this,
				# `merge_trait_worlds`'s post-merge reclassification
				# misses external interface impls because
				# `world.interfaces` is empty for packages.
				#
				# Gate: confirm via `type_table.get_nominal(INTERFACE,
				# module, name)` — the type table registers interface
				# nominals as `TypeKind.INTERFACE`.  A bare
				# `trait_key in external_missing_traits` check would
				# also fire for *actually-missing* traits (e.g. a
				# genuine package-metadata gap), silently re-routing
				# them through the interface solver and masking the
				# underlying metadata bug.
				_is_iface = type_table.get_nominal(
					kind=TypeKind.INTERFACE,
					module_id=trait_key.module,
					name=trait_key.name,
				) is not None
				if _is_iface:
					from lang.driftc.traits.world import InterfaceDef as _InterfaceDef, InterfaceImplRef as _InterfaceImplRef
					if trait_key not in world.interfaces:
						world.interfaces[trait_key] = _InterfaceDef(
							key=trait_key,
							name=trait_key.name,
							loc=getattr(impl, "loc", None),
						)
					world.interface_impls_by_iface_target.setdefault(
						(trait_key, head_key), []
					).append(
						_InterfaceImplRef(
							iface=trait_key,
							target=target_key,
							target_head=head_key,
							type_params=tuple(getattr(impl, "impl_type_params", []) or ()),
							require_expr=getattr(impl, "require_expr", None),
							loc=getattr(impl, "loc", None),
						)
					)
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
					consts_obj = {}
				# Also import internal (non-exported) constants so generic template
				# re-instantiation can resolve module-scoped constant references.
				internal_consts_obj = payload.get("internal_consts")
				if isinstance(internal_consts_obj, dict):
					consts_obj = {**consts_obj, **internal_consts_obj}
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

	# Run capture discovery on normalized HIR so HLambda.captures is populated
	# BEFORE the pre-typecheck snapshot.  Without this, package consumers
	# receive HLambda nodes with explicit_captures but empty captures, causing
	# closure environment struct field mismatches during MIR lowering.
	if getattr(args, "emit_package", None):
		for _fn_id, _block in normalized_hirs_by_id.items():
			validate_lambdas_non_retaining(_block)

	# Snapshot normalized HIR before type-checking for package emission.
	# The type checker mutates HIR in place (e.g., rewrites HQualifiedMember
	# to HVar during variant constructor resolution).  Package consumers
	# need the PRE-mutation HIR so their type checker can resolve variant
	# constructors through the qualified-member path.
	import copy as _copy_mod
	_pre_typecheck_hirs: dict[FunctionId, H.HBlock] = {
		fn_id: _copy_mod.deepcopy(block) for fn_id, block in normalized_hirs_by_id.items()
	} if getattr(args, "emit_package", None) else {}

	# Option B Phase 2: load package HIR functions into the normalized pool.
	# With Phase 2a visibility entries above, the type checker can resolve
	# module-internal names inside package HIR function bodies.
	_pkg_hir_loaded: dict[FunctionId, H.HBlock] = {}
	# Signatures for ALL package HIR functions (including private ones)
	# so the type checker can resolve parameter types and call targets.
	_pkg_hir_sigs: dict[FunctionId, FnSignature] = {}
	if loaded_pkgs:
		for pkg in loaded_pkgs:
			tid_map = pkg_typeid_maps.get(pkg.path, {})
			for mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				_pv = payload.get("payload_version")
				if payload.get("payload_kind") != "provisional-dmir" or _pv != 2:
					raise AssertionError(
						f"unsupported package payload kind/version "
						f"(kind={payload.get('payload_kind')!r}, version={_pv!r}); "
						f"this compiler requires payload_version 2 (HIR-based)"
					)
				hir_obj = payload.get("hir_funcs")
				if not isinstance(hir_obj, dict) or not hir_obj:
					continue
				# Load signatures for ALL functions (including private) so
				# the type checker has param types when checking HIR bodies.
				sigs_obj = payload.get("signatures")
				if isinstance(sigs_obj, dict):
					for sname, sd in sigs_obj.items():
						if not isinstance(sd, dict):
							continue
						fn_id_obj = sd.get("fn_id")
						s_fn_id = function_id_from_obj(fn_id_obj)
						if s_fn_id is None:
							continue
						if s_fn_id in external_signatures_by_id:
							continue  # already resolved
						if sname not in hir_obj:
							continue  # only load sigs for functions we have HIR for
						# Resolve TypeExpr-based params
						param_types_raw = sd.get("param_types")
						resolved_ptids = None
						if param_types_raw is not None:
							resolved = []
							_ok = True
							for pt_obj in param_types_raw:
								pt_expr = decode_type_expr(pt_obj)
								if pt_expr is None:
									_ok = False
									break
								_r = resolve_opaque_type(pt_expr, type_table, module_id=sd.get("module"))
							resolved.append(_r)
							if _ok:
								resolved_ptids = resolved
						if resolved_ptids is None:
							raw_ptids = sd.get("param_type_ids")
							if isinstance(raw_ptids, list):
								resolved_ptids = [tid_map.get(int(x), int(x)) for x in raw_ptids]
						ret_tid = None
						return_type_raw = sd.get("return_type")
						if return_type_raw is not None:
							rt_expr = decode_type_expr(return_type_raw)
							if rt_expr is not None:
								ret_tid = resolve_opaque_type(rt_expr, type_table, module_id=sd.get("module"))
						if ret_tid is None:
							raw_ret = sd.get("return_type_id")
							if isinstance(raw_ret, int):
								ret_tid = tid_map.get(raw_ret, raw_ret)
						sig = FnSignature(
							name=str(sd.get("name") or sname),
							module=sd.get("module"),
							method_name=sd.get("method_name"),
							param_names=sd.get("param_names"),
							param_type_ids=resolved_ptids,
							return_type_id=ret_tid,
							declared_can_throw=sd.get("declared_can_throw"),
							is_method=bool(sd.get("is_method", False)),
							self_mode=sd.get("self_mode"),
							is_pub=bool(sd.get("is_pub", False)),
							is_exported_entrypoint=bool(sd.get("is_exported_entrypoint", False)),
						)
						_pkg_hir_sigs[s_fn_id] = sig
						external_signatures_by_id[s_fn_id] = sig
				decoded_hir = decode_hir_funcs(hir_obj)
				for sym, hir_block in decoded_hir.items():
					fn_id = parse_function_symbol(sym)
					if fn_id is None:
						continue
					if fn_id in normalized_hirs_by_id:
						continue
					normalized_hirs_by_id[fn_id] = normalize_hir(hir_block)
					_pkg_hir_loaded[fn_id] = hir_block

	# Option B: package HIR was serialized from a pre-typecheck snapshot,
	# so HLambda.captures is empty.  Run capture discovery on loaded HIR
	# so MIR lowering can build closure environment structs.
	if _pkg_hir_loaded:
		from lang.driftc.stage1.capture_discovery import discover_captures as _disc_caps
		def _discover_pkg_captures(node: object) -> None:
			if isinstance(node, H.HLambda):
				if not node.captures and node.explicit_captures:
					_disc_caps(node)
			for _fname in getattr(node, "__dataclass_fields__", {}) or {}:
				_val = getattr(node, _fname, None)
				if isinstance(_val, (H.HExpr, H.HNode)):
					_discover_pkg_captures(_val)
				elif isinstance(_val, list):
					for _item in _val:
						if isinstance(_item, (H.HExpr, H.HNode, H.HStmt)):
							_discover_pkg_captures(_item)
		for _pkg_fn_id in _pkg_hir_loaded:
			_norm = normalized_hirs_by_id.get(_pkg_fn_id)
			if _norm is not None:
				_discover_pkg_captures(_norm)

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
	if getattr(args, "allow_unsafe", False) and module_exports is not None:
		for mod_id in module_exports.keys():
			if isinstance(mod_id, str):
				unsafe_trusted_modules.add(mod_id)
	# Option B: package modules need unsafe-block permission (the producer
	# already validated unsafe at build time) but NOT full toolchain trust
	# (rawbuffer intrinsics, typed_validator privileges).  Track them in a
	# separate set and pass to the type checker as allow_unsafe_modules.
	_pkg_unsafe_modules: set[str] = set()
	if external_module_packages:
		for mod_id in external_module_packages:
			_pkg_unsafe_modules.add(mod_id)
	_source_mods_main = set(modules.keys()) if isinstance(modules, dict) else set()
	if semantic_world.type_table is not None:
		semantic_world.type_table.source_modules = _source_mods_main
	type_checker = TypeChecker(type_table=semantic_world.type_table, allow_unsafe=bool(getattr(args, "allow_unsafe", False)), unsafe_trusted_modules=unsafe_trusted_modules, pkg_unsafe_modules=_pkg_unsafe_modules, semantic_world=semantic_world, source_modules=_source_mods_main)
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
		if td.kind is TypeKind.STRUCT:
			inst = type_table.get_struct_instance(impl_tid)
			if inst is not None:
				return inst.base_id
		if td.kind is TypeKind.VARIANT:
			inst = type_table.get_variant_instance(impl_tid)
			if inst is not None:
				return inst.base_id
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

	# Pre-compute generic method keys from ALL signatures (local + external)
	# so K20 __inst__ dedup works regardless of which registration call sees the sig.
	_all_generic_method_keys: set[tuple[int | None, str | None]] = set()
	for _fid, _sig in signatures_by_id.items():
		if _sig.is_method and (_sig.type_params or getattr(_sig, "impl_type_params", [])):
			_all_generic_method_keys.add((_registry_impl_target_type_id(_sig.impl_target_type_id), _sig.method_name))
	for _fid, _sig in external_signatures_by_id.items():
		if _sig.is_method and (_sig.type_params or getattr(_sig, "impl_type_params", [])):
			_all_generic_method_keys.add((_registry_impl_target_type_id(_sig.impl_target_type_id), _sig.method_name))

	def _register_signatures_in_callable_registry(
		sigs: Mapping[FunctionId, FnSignature],
		*,
		is_external: bool = False,
		skip_modules: set[str] | None = None,
	) -> None:
		"""Register signatures into callable_registry with deterministic precedence.

		Priority: exact concrete match > generic template > wrapper (never).
		Skips wrappers (is_wrapper=True) and __inst__ monomorphizations when a
		generic template exists for the same (impl_target_type_id, method_name).
		"""
		nonlocal next_callable_id
		generic_method_keys = _all_generic_method_keys
		for fn_id, sig in sigs.items():
			if is_external and callable_registry.get_by_fn_id(fn_id) is not None:
				continue
			if getattr(sig, "is_wrapper", False):
				continue
			# K27: Terminal-throws signatures from packages have return_type=null
			# (declared_terminal_throws is the source of truth).  Normalize
			# return_type_id to Void for registry/call-result purposes while
			# preserving declared_terminal_throws=True on the signature.
			_effective_return_type_id = sig.return_type_id
			if _effective_return_type_id is None and bool(getattr(sig, "declared_terminal_throws", False)):
				_effective_return_type_id = type_table.ensure_void()
			if sig.param_type_ids is None or _effective_return_type_id is None:
				continue
			# K20: skip __inst__ monomorphized sigs when generic template exists.
			# Only skip sigs that are themselves non-generic (true monomorphizations,
			# not generic templates that happen to have __inst__ in the name).
			if "__inst__" in (fn_id.name or "") and sig.is_method and not (sig.type_params or getattr(sig, "impl_type_params", [])):
				norm_recv = _registry_impl_target_type_id(sig.impl_target_type_id)
				key = (norm_recv, sig.method_name)
				if key in generic_method_keys:
					continue
			module_name = getattr(fn_id, "module", None) or sig.module
			if is_external and skip_modules and module_name is not None and module_name in skip_modules:
				continue
			param_types_tuple = tuple(sig.param_type_ids)
			module_id = module_ids.setdefault(module_name, len(module_ids))
			sig_name = display_name_by_id.get(fn_id, _display_name_for_fn_id(fn_id))
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
				visibility = Visibility.public() if sig.is_pub else Visibility.private()
				callable_registry.register_inherent_method(
					callable_id=next_callable_id,
					name=sig.method_name or sig.name,
					module_id=module_id,
					visibility=visibility,
					signature=CallableSignature(param_types=param_types_tuple, result_type=_effective_return_type_id),
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
					signature=CallableSignature(param_types=param_types_tuple, result_type=_effective_return_type_id),
					template_signature=_template_sig_for(sig),
					template_type_params=tuple(tp.name for tp in (sig.type_params or [])),
					fn_id=fn_id,
					is_generic=bool(sig.type_params),
				)
				next_callable_id += 1

	_register_signatures_in_callable_registry(signatures_by_id)
	_register_signatures_in_callable_registry(external_signatures_by_id, is_external=True, skip_modules=modules)

	# ── World is ready ───────────────────────────────────────────────
	semantic_world.callable_registry = callable_registry
	semantic_world.base_signatures = base_signatures_by_id
	semantic_world.derived_signatures = derived_signatures_by_id
	semantic_world.external_signatures = external_signatures_by_id
	semantic_world.external_trait_defs = external_trait_defs
	semantic_world.external_impl_metas = external_impl_metas
	semantic_world.external_missing_traits = external_missing_traits
	semantic_world.advance_to(WorldPhase.READY)

	# candidate_signatures_for_diag removed; no name-keyed fallback map

	# Contract: no wrapper sigs should be in the registry.
	for _entry in callable_registry._entries.values() if hasattr(callable_registry, "_entries") else []:
		_efid = getattr(_entry, "fn_id", None)
		if _efid is not None:
			_esig = signatures_by_id_all.get(_efid)
			if _esig is not None and getattr(_esig, "is_wrapper", False):
				raise AssertionError(f"registry contract: wrapper sig '{function_symbol(_efid)}' leaked into callable_registry")

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
		for kind in ("structs", "variants", "exceptions", "interfaces", "aliases"):
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
			if isinstance(module_exports, dict):
				for std_mod in ("std.iter", "std.containers"):
					if std_mod in module_exports:
						prelude_modules.add(std_mod)
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
			# Package modules are always visible to consumer modules because all
			# public methods are available through the package interface.
			if external_module_packages:
				for pkg_mod in external_module_packages:
					best.setdefault(pkg_mod, (mod_name, pkg_mod))
				visible |= set(external_module_packages.keys())
			visible_module_names_by_name[mod_name] = visible
			visibility_provenance_by_name[mod_name] = best
			visible_ids_list = []
			for m in sorted(visible):
				visible_ids_list.append(module_ids.setdefault(m, len(module_ids)))
			visible_ids = tuple(visible_ids_list)
			visible_modules_by_name[mod_name] = visible_ids
		# Option B Phase 2a: add visibility entries for package modules so
		# the type checker can resolve module-internal names when compiling
		# package HIR.  Each package module sees only modules in its own
		# package (they were compiled as a unit) plus prelude modules.
		# Consumer source modules are NOT visible to package HIR — the
		# package was compiled without knowledge of the consumer.
		if external_module_packages:
			# Group modules by package_id for correct per-package scoping.
			_mods_by_pkg: dict[str, set[str]] = {}
			for _pm, _ppkg in external_module_packages.items():
				_mods_by_pkg.setdefault(_ppkg, set()).add(_pm)
			for pkg_mod in sorted(external_module_packages.keys()):
				if pkg_mod in visible_module_names_by_name:
					continue
				_own_pkg = external_module_packages[pkg_mod]
				_pkg_siblings = _mods_by_pkg.get(_own_pkg, set())
				# Package module sees its own siblings, prelude, all other
				# loaded package modules, and stdlib/dependency source modules.
				# Consumer application modules are NOT visible — the package
				# was compiled without knowledge of them.
				_all_pkg_mods = set(external_module_packages.keys())
				# Source modules that are part of stdlib (have exports and
				# are not the consumer's own modules).  Consumer modules
				# are identified by not being in any package AND not having
				# a known stdlib/lang prefix.
				_stdlib_src_mods: set[str] = set()
				if isinstance(module_deps, dict) and isinstance(module_exports, dict):
					for _sm in module_deps.keys():
						if _sm in module_exports and _sm not in _all_pkg_mods:
							_stdlib_src_mods.add(_sm)
				_pkg_visible = _pkg_siblings | prelude_modules | _all_pkg_mods | _stdlib_src_mods
				visible_module_names_by_name[pkg_mod] = set(_pkg_visible)
				visibility_provenance_by_name[pkg_mod] = {m: (pkg_mod, m) for m in _pkg_visible}
				pkg_visible_ids = []
				for m in sorted(_pkg_visible):
					pkg_visible_ids.append(module_ids.setdefault(m, len(module_ids)))
				visible_modules_by_name[pkg_mod] = tuple(pkg_visible_ids)
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
		type_table=semantic_world.type_table,
		module_ids=module_ids,
	)
	for impl in semantic_world.external_impl_metas:
		if getattr(impl, "trait_key", None) is None:
			global_impl_index.add_impl(impl=impl, type_table=semantic_world.type_table, module_ids=module_ids)
	global_trait_index = GlobalTraitIndex.from_trait_worlds(getattr(semantic_world.type_table, "trait_worlds", None))
	for trait_def in semantic_world.external_trait_defs:
		if hasattr(trait_def, "key"):
			global_trait_index.add_trait(trait_def.key, trait_def)
	for missing_trait in semantic_world.external_missing_traits:
		if hasattr(missing_trait, "module") and hasattr(missing_trait, "name"):
			# Do not mark a trait as missing if it is already defined in the
			# current world (e.g. source-compiled stdlib traits satisfy package
			# references that were "missing" during the package's own build).
			if missing_trait not in global_trait_index.traits_by_id:
				global_trait_index.mark_missing(missing_trait)
	global_trait_impl_index = GlobalTraitImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=semantic_world.type_table,
		module_ids=module_ids,
	)
	for impl in semantic_world.external_impl_metas:
		if getattr(impl, "trait_key", None) is not None:
			global_trait_impl_index.add_impl(impl=impl, type_table=semantic_world.type_table, module_ids=module_ids)
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
	# K25: External package modules — use deserialized trait_scope from DMIR
	# when available.  Backward-compat fallback for pre-trait_scope packages:
	# populate with all known traits (scope was validated at build time).
	if isinstance(external_exports, dict):
		_all_trait_keys: list[object] | None = None
		for mod_name, exp in external_exports.items():
			if mod_name in trait_scope_by_module:
				continue
			if isinstance(exp, dict):
				scope = exp.get("trait_scope", None)
				if isinstance(scope, list):
					trait_scope_by_module[mod_name] = list(scope)
				else:
					# K25 backward-compat fallback for old packages without trait_scope.
					if _all_trait_keys is None:
						_all_trait_keys = list(global_trait_index.traits_by_id.keys()) if global_trait_index is not None else []
					trait_scope_by_module[mod_name] = _all_trait_keys
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
	# Phase 3.5: validate trait impl terminal-throws compatibility in the
	# CLI path. compile_stubbed_funcs has its own call; the CLI main()
	# pipeline must do the same.
	if isinstance(module_exports, dict) and global_trait_index is not None:
		_cli_trait_impls: list[ImplMeta] = []
		for _exp in module_exports.values():
			if isinstance(_exp, dict):
				for _impl in _exp.get("impls", []) or []:
					if isinstance(_impl, ImplMeta):
						_cli_trait_impls.append(_impl)
		if _cli_trait_impls:
			type_checker.validate_trait_impls(
				_cli_trait_impls,
				signatures_by_id=signatures_by_id,
				trait_index=global_trait_index,
				diagnostics=type_diags,
			)

	signatures_by_id_all = dict(signatures_by_id)
	signatures_by_id_all.update(external_signatures_by_id)
	linked_world, require_env = _build_linked_world(semantic_world.type_table)

	# K42 + Phase 5: build function_keys_by_fn_id for Pass 1 covering ALL
	# generic signatures (wrappers + non-wrappers).  This serves two purposes:
	# 1. record_instantiation needs keys to store CallInstantiation records
	# 2. Phase 5 shares function_keys via Pass1State so compile_stubbed_funcs
	#    can skip its own O(n) compute_template_decl_fingerprint loop.
	#
	# INVARIANT: external_template_keys_by_fn_id already contains correct
	# keys for all external non-wrapper generic templates (populated from
	# DMIR TemplateHIR-v1 entries during package loading).  The fallback
	# loop below only synthesizes keys for fn_ids NOT already present,
	# which at this point are local consumer generics and wrapper methods.
	# For these, package_id is the local consumer package — correct because
	# they are defined in the current compilation unit.  If this assumption
	# changes (e.g. external non-wrapper generics missing from DMIR), the
	# fallback must derive per-signature package_id from module_packages.
	pass1_function_keys: dict[FunctionId, FunctionKey] = dict(external_template_keys_by_fn_id)
	_p1_default_package = getattr(semantic_world.type_table, "package_id", None) or package_id
	_p1_module_packages = getattr(semantic_world.type_table, "module_packages", None)
	# Build requires_by_fn_id from trait_worlds for fingerprint computation.
	_p1_requires_by_fn_id: dict[FunctionId, object] = {}
	_p1_trait_worlds = getattr(semantic_world.type_table, "trait_worlds", {}) if semantic_world.type_table is not None else {}
	if isinstance(_p1_trait_worlds, dict):
		for _p1_world in _p1_trait_worlds.values():
			for _p1_fid, _p1_req in getattr(_p1_world, "requires_by_fn", {}).items():
				_p1_requires_by_fn_id[_p1_fid] = _p1_req
	for _p1_fn_id, _p1_sig in signatures_by_id_all.items():
		if not (getattr(_p1_sig, "type_params", []) or getattr(_p1_sig, "impl_type_params", [])):
			continue
		if _p1_fn_id in pass1_function_keys:
			continue
		if _p1_sig.param_types is None or _p1_sig.return_type is None:
			continue
		_p1_module_id = getattr(_p1_sig, "module", None) or getattr(_p1_fn_id, "module", None) or "main"
		_p1_sym = function_symbol(_p1_fn_id)
		_p1_prefix = f"{_p1_module_id}::"
		_p1_name = _p1_sym[len(_p1_prefix):] if _p1_sym.startswith(_p1_prefix) else _p1_sym
		if "#" in _p1_name:
			_p1_base, _p1_ord = _p1_name.rsplit("#", 1)
			if _p1_ord.isdigit():
				_p1_name = _p1_base
		_p1_req_expr = _p1_requires_by_fn_id.get(_p1_fn_id)
		_p1_fp, _ = compute_template_decl_fingerprint(
			_p1_sig,
			declared_name=_p1_name,
			module_id=_p1_module_id,
			require_expr=_p1_req_expr if _p1_req_expr is not None else None,
			default_package=_p1_default_package,
			module_packages=_p1_module_packages,
		)
		pass1_function_keys[_p1_fn_id] = FunctionKey(
			package_id=package_id,
			module_path=_p1_module_id,
			name=_p1_name,
			decl_fingerprint=_p1_fp,
		)

	# Option B: clear is_exported_entrypoint on HIR-compiled package
	# functions so the codegen doesn't emit __impl renames or FnResult
	# wrappers.  Entry wrapper deps (install_process_preamble) are kept.
	if _pkg_hir_loaded:
		_entry_wrapper_keep = {
			FunctionId(module=mod, name=name, ordinal=0)
			for _flag, (mod, name) in ENTRY_WRAPPER_IMPLICIT_DEPS.items()
		}
		for _ext_fn_id, _ext_sig in external_signatures_by_id.items():
			if _ext_fn_id in _entry_wrapper_keep:
				continue
			if getattr(_ext_sig, "is_exported_entrypoint", False):
				_ext_sig.is_exported_entrypoint = False

	typed_fns: dict[FunctionId, object] = {}
	# Use signatures_by_id_all (includes external/package sigs) so package
	# HIR functions find their param types and return types.
	_typecheck_sigs = signatures_by_id_all if signatures_by_id_all else signatures_by_id
	for fn_id, hir_block in list(normalized_hirs_by_id.items()):
		# Build param type map from signatures when available.
		param_types: dict[str, "TypeId"] = {}
		param_mutable: dict[str, bool] | None = None
		sig = _typecheck_sigs.get(fn_id)
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
			function_keys_by_fn_id=pass1_function_keys,
		)
		type_diags.extend(result.diagnostics)
		typed_fns[fn_id] = result.typed_fn
	# (boundary markers already cleared pre-type-check above)
	if _pkg_hir_loaded and drift_debug.enabled("pkg_hir"):
		print(f"[pkg-hir] {len(_pkg_hir_loaded)} compiled from HIR, 0 fell back to MIR", file=sys.stderr)
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
				_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
			return 1

	# Compute non-retaining metadata for callable parameters before lambda validation.
	signatures_by_id_all = analyze_non_retaining_params(
		typed_fns,
		signatures_by_id_all,
		type_table=type_table,
		semantic_world=semantic_world,
	)

	# Phase 3a/3b: annotate known stdlib callable params with escape levels.
	_apply_stdlib_escape_annotations(signatures_by_id_all, semantic_world=semantic_world)

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
				_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
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
				_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
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
				declared_terminal_throws=info.sig.declared_terminal_throws,
			)
			call_info[csid] = CallInfo(target=info.target, sig=new_sig)

	# Enforce trait requirements (struct + function requires) before borrow checking.
	trait_diags: list[Diagnostic] = []
	linked_world, require_env = _build_linked_world(semantic_world.type_table)
	# Install destructor_fns early so any code that queries has_drop()
	# (e.g. copy_status callbacks, borrow checker, or trait enforcement)
	# before compile_stubbed_funcs runs gets the correct answer for
	# Destructible types.  Without this, has_drop(Arc) can return False
	# and the result gets cached, poisoning the later cleanup-authoring
	# verdict decisions even though compile_stubbed_funcs would install
	# destructor_fns internally.
	if linked_world is not None:
		_install_destructor_fns(semantic_world.type_table, linked_world, module_exports, external_impl_metas=semantic_world.external_impl_metas)
		_dump_type_table_queries_if_enabled(semantic_world.type_table)
		# Re-finalize non-generic variants after destructor_fns are
		# installed.  The parser-phase `finalize_variants()` call
		# computes each variant instance's `internal_tombstone_ctor`
		# based on `has_drop(field_ty)`, which returns False for user
		# structs whose `core.Destructible` impl has not yet been
		# registered.  That causes variants like
		# `UserMsg { Payload(tok: Token), Other(n: Int) }` (Token
		# implements Destructible) to be instantiated WITHOUT an
		# internal tombstone, which later breaks any codegen site
		# that needs to materialize a drop-safe tombstone for the
		# variant (e.g. the match-scrutinee tombstone store in
		# `_ensure_arm_scrut_ptr`).  Re-finalizing here clears the
		# needs-drop cache and rebuilds tombstone metadata with the
		# authoritative view of destructor_fns.
		semantic_world.type_table.finalize_variants()
	if linked_world is not None and require_env is not None:
		used_types = collect_used_type_keys(typed_fns, semantic_world.type_table, signatures_by_id)
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
				signatures=signatures_by_id_all,
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
				_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
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
				_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
		return 1

	# Borrow check each typed function (mandatory stage).
	borrow_diags: list[Diagnostic] = []
	for _fn_id, typed_fn in typed_fns.items():
		bc = BorrowChecker.from_typed_fn(
			typed_fn,
			type_table=semantic_world.type_table,
			signatures_by_id=signatures_by_id_all,
			enable_auto_borrow=True,
			semantic_world=semantic_world,
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
				_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
				print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
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
			origin_by_fn_id=origin_by_fn_id,
			package_id=package_id,
			external_missing_impl_modules=external_missing_impl_modules,
			return_checked=True,
			prelude_enabled=bool(args.prelude),
			generic_templates_by_key=external_template_hirs_by_key,
			template_keys_by_fn_id=external_template_keys_by_fn_id,
			emit_instantiation_index=args.emit_instantiation_index,
			enforce_entrypoint=bool(args.output or args.emit_ir),
			allow_unsafe=bool(getattr(args, "allow_unsafe", False)),
			semantic_world=semantic_world,
		)
		# All semantic declarations are now complete: source types, package
		# types, callable registration, closure environment structs from
		# MIR lowering.  Freeze the world before codegen.
		semantic_world.freeze()
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
					_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
					print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
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
		# Exclude package-loaded HIR functions — only the source modules
		# being built belong in this package's payload.
		source_module_ids = {
			getattr(fn_id, "module", None) or "main"
			for fn_id in normalized_hirs_by_id.keys()
			if fn_id not in _pkg_hir_loaded
		}
		per_module_sigs: dict[str, dict[str, FnSignature]] = {}
		inst_sigs: dict[str, FnSignature] = {}
		for name, sig in pkg_signatures_by_symbol.items():
			if getattr(sig, "is_instantiation", False):
				inst_sigs[name] = sig
				continue
			# Hidden lambda callbacks from instantiations go into the
			# instantiations section, even if their module is a source module.
			if ("__lambda_cb_" in name or "__lambda_" in name) and "__inst__" in name:
				inst_sigs[name] = sig
				continue
			mid = getattr(sig, "module", None) or "main"
			if mid not in source_module_ids:
				continue
			per_module_sigs.setdefault(mid, {})[name] = sig
		# Include signatures for hidden lambda callbacks from stdlib/dependency
		# generic instantiations that are not in pkg_signatures_by_symbol.
		for fn_id in mir_funcs:
			sym = function_symbol(fn_id)
			if sym in pkg_signatures_by_symbol or sym in inst_sigs:
				continue
			fn_name = getattr(fn_id, "name", "") or ""
			if ("__lambda_cb_" in fn_name or "__lambda_" in fn_name) and "__inst__" in fn_name:
				info = checked_pkg.fn_infos_by_id.get(fn_id)
				if info is not None and info.signature is not None:
					inst_sigs[sym] = info.signature
	
		per_module_mir: dict[str, dict[str, object]] = {}
		inst_mir: dict[str, object] = {}
		for fn_id, fn in mir_funcs.items():
			name = function_symbol(fn_id)
			sig = pkg_signatures_by_symbol.get(name)
			mid = getattr(sig, "module", None) if sig is not None else getattr(fn_id, "module", None)
			mid = mid or "main"
			if sig is not None and getattr(sig, "is_instantiation", False):
				inst_mir[name] = fn
				continue
			# Hidden lambda callbacks generated by stdlib/dependency generic
			# instantiations (e.g. __lambda_cb_spawn_cb__inst__...) have a
			# module ID outside the package's source modules.  Include them
			# in the instantiations section so consumers can resolve
			# ConstructIface targets.
			fn_name = getattr(fn_id, "name", "") or ""
			if ("__lambda_cb_" in fn_name or "__lambda_" in fn_name) and "__inst__" in fn_name:
				inst_mir[name] = fn
				continue
			if mid in source_module_ids:
				per_module_mir.setdefault(mid, {})[name] = fn
		# Use pre-typecheck HIR snapshot for serialization.  The type checker
		# mutates HIR in place (HQualifiedMember → HVar), so the post-mutation
		# normalized_hirs_by_id is not suitable for package consumers.
		_hir_source = _pre_typecheck_hirs if _pre_typecheck_hirs else normalized_hirs_by_id
		per_module_hir: dict[str, dict[str, H.HBlock]] = {}
		for fn_id, block in _hir_source.items():
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
		# Phase 9: pre-compute canonical keys for all TypeIds in the package
		# type table. The consumer uses these for O(1) key lookup instead of
		# walking the TypeDef graph.
		_pkg_emit_tt = checked_pkg.type_table or type_table
		from lang.driftc.packages.type_table_link_v0 import compute_canonical_keys
		_pkg_canonical_keys = compute_canonical_keys(_pkg_emit_tt, package_id)
		# Stage 8.4: compute MIR-reachable TypeId set as the UNION across all
		# modules. Only these defs are emitted; other types are covered by
		# canonical_keys. Phase B schema validation uses canonical_keys for
		# keyed packages (Phase 10).
		from lang.driftc.packages.provisional_dmir_v0 import _collect_mir_type_ids, _transitive_type_closure
		_pkg_mir_seeds: set[int] = set()
		for _rm_mid in all_module_ids:
			if _rm_mid.startswith(("std.", "lang.", "drift.")):
				continue
			_pkg_mir_seeds |= _collect_mir_type_ids(per_module_mir.get(_rm_mid, {}))
		# Generic template + signature seeds (all modules).
		for _rm_mid, _rm_sigs in per_module_sigs.items():
			if _rm_mid.startswith(("std.", "lang.", "drift.")):
				continue
			for _sym, _sig in _rm_sigs.items():
				_is_inst = "__inst__" in _sym
				_has_tp = bool(getattr(_sig, "type_params", None)) or bool(getattr(_sig, "impl_type_params", None))
				if _is_inst or _has_tp:
					if _sig.param_type_ids is not None:
						_pkg_mir_seeds.update(_sig.param_type_ids)
					if _sig.return_type_id is not None:
						_pkg_mir_seeds.add(_sig.return_type_id)
					_itid = getattr(_sig, "impl_target_type_id", None)
					if _itid is not None:
						_pkg_mir_seeds.add(_itid)
		# Const TypeIds.
		for _csym, (_ctid, _) in _pkg_emit_tt.consts.items():
			_pkg_mir_seeds.add(_ctid)
		# Synthetic struct defs (lambda capture envs) — the linker's Phase B
		# sweep reads these from pkg.defs to populate host struct fields.
		for _st_tid, _st_td in _pkg_emit_tt._defs.items():
			if _st_td.kind is TypeKind.STRUCT and _st_td.field_names is not None and _st_td.param_types:
				_pkg_mir_seeds.add(_st_tid)
		_pkg_reachable_tids = _transitive_type_closure(_pkg_mir_seeds, _pkg_emit_tt)

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
				"aliases": list(exported_types_obj.get("aliases", [])) if isinstance(exported_types_obj.get("aliases"), list) else [],
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
				package_id=package_id,
				module_packages=getattr(type_table, "module_packages", None),
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
	
			_raw_trait_scope = mexp.get("trait_scope", [])
			_trait_scope_dicts: list[dict[str, Any]] = []
			if isinstance(_raw_trait_scope, list):
				for _tk in _raw_trait_scope:
					_trait_scope_dicts.append({"package_id": getattr(_tk, "package_id", None), "module": getattr(_tk, "module", None), "name": getattr(_tk, "name", str(_tk))})
			_module_hir_blocks = per_module_hir.get(mid, {})
			payload_obj = encode_module_payload_v0(
				package_id=package_id,
				module_id=mid,
				type_table=_pkg_emit_tt,
				canonical_keys=_pkg_canonical_keys,
				reachable_tids=_pkg_reachable_tids,
				signatures=sig_env,
				generic_templates=encode_generic_templates(
					package_id=package_id,
					module_id=mid,
					signatures=sig_env,
					hir_blocks=_module_hir_blocks,
					requires_by_symbol=requires_by_symbol,
					module_packages=getattr(checked_pkg.type_table or type_table, "module_packages", None),
				),
				hir_funcs=encode_hir_funcs(
					module_id=mid,
					signatures=sig_env,
					hir_blocks=_module_hir_blocks,
				),
				exported_values=exported_values,
				exported_types=exported_types,
				exported_traits=exported_traits,
				exported_consts=exported_consts,
				reexports=reexports_obj,
				trait_metadata=trait_metadata,
				impl_headers=impl_headers,
				trait_scope=_trait_scope_dicts,
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
						"types": {"structs": [], "variants": [], "exceptions": [], "interfaces": [], "aliases": []},
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
				"trait_scope": payload_obj.get("trait_scope", []) if isinstance(payload_obj, dict) else [],
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
	
		# Build native_deps manifest section from --native-link-lib flags.
		_native_deps_section: dict[str, object] | None = None
		if getattr(args, "native_link_lib", []):
			_native_deps_section = {
				"schema_version": 1,
				"link_libs": [{"lib": lib} for lib in args.native_link_lib],
			}

		# Build required_deps manifest section from --package-dep flags.
		#
		# Name boundary (v3):
		# - `--package-dep` is the CLI flag carrying the producer's
		#   authored manifest range per dep (no lock pin leaks).
		# - Serialized into the .dmp manifest as `required_deps` —
		#   the published consumer-facing requirement.  Pre-0.29
		#   packages used the key `package_deps` for the same role;
		#   the rename reflects the semantic distinction between
		#   manifest.json's `package_deps` (owner-authored source)
		#   and the .dmp's `required_deps` (published requirement).
		#   Consumers reject pre-cut `.dmp`s that still use the old
		#   key — see `dmir_pkg_v0.py::_parse_required_deps`.
		_required_deps_list: list[dict[str, str]] | None = None
		if getattr(args, "package_dep", []):
			# `required_deps[].version` is strictly the owner-declared
			# acceptable range: `"M"` or `"M.N"`.  Validate at emit
			# time so hand-driven `--package-dep` usage cannot write
			# a `.dmp` that the new consumer-side loader would later
			# reject.  Normal `drift build` already feeds validated
			# manifest ranges through this path; this guard catches
			# direct CLI misuse.  The shape validator is shared with
			# the authored-manifest parser and the consumer-side
			# resolver — see `dmir_pkg_v0::is_owner_declared_range`.
			from lang.driftc.packages.dmir_pkg_v0 import is_owner_declared_range as _is_required_range
			_required_deps_list = []
			for dep_str in args.package_dep:
				if "=" not in dep_str:
					msg = f"--package-dep requires NAME=VERSION format, got: {dep_str}"
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "emit-package", "message": msg, "severity": "error", "file": "<cli>", "line": None, "column": None}]}))
					else:
						print(f"error: {msg}", file=sys.stderr)
					return 1
				dep_name, dep_ver = dep_str.split("=", 1)
				if not _is_required_range(dep_ver):
					msg = (
						f"--package-dep '{dep_name}={dep_ver}': version must be "
						f"the owner-declared acceptable range — `\"M\"` (any "
						f"M.x.x) or `\"M.N\"` (any M.N.x).  Exact pins, `^`/`~` "
						f"ranges, and other shapes are rejected at the .dmp "
						f"metadata boundary (writing one would produce a "
						f"package the consumer-side loader rejects)."
					)
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "emit-package", "message": msg, "severity": "error", "file": "<cli>", "line": None, "column": None}]}))
					else:
						print(f"error: {msg}", file=sys.stderr)
					return 1
				_required_deps_list.append({"name": dep_name, "version": dep_ver})

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
			"payload_version": 2,
			"modules": manifest_modules,
			"blobs": manifest_blobs,
		}
		if _native_deps_section is not None:
			manifest_obj["native_deps"] = _native_deps_section
		if _required_deps_list is not None:
			manifest_obj["required_deps"] = _required_deps_list
		if args.source_content_id is not None:
			scid = str(args.source_content_id)
			# Strict shape validation: literal `sha256:` + exactly
			# 64 lowercase hex chars.  Anything else (uppercase,
			# non-hex, wrong length, trailing whitespace) is
			# rejected at the trust boundary so a malformed id
			# cannot be stamped into the .dmp manifest and later
			# signed by a downstream attestation.
			import re as _re
			if not _re.fullmatch(r"sha256:[0-9a-f]{64}", scid):
				msg = (
					f"--source-content-id must match 'sha256:<64 lowercase hex>'; "
					f"got {scid!r}"
				)
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "emit-package", "message": msg, "severity": "error", "file": "<cli>", "line": None, "column": None}]}))
				else:
					print(f"error: {msg}", file=sys.stderr)
				return 1
			manifest_obj["source_content_id"] = scid

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
		# Phase 6: install destructor_fns on the shared type_table before
		# Pass1State so compile_stubbed_funcs can skip this under pass1_state.
		# INVARIANT: _install_destructor_fns replaces type_table.destructor_fns
		# entirely, then K39 extends it.  Both must run before Pass1State and
		# both must be skipped in CSF to avoid the replace clobbering K39 entries.
		_install_destructor_fns(type_table, linked_world, combined_exports, external_impl_metas=external_impl_metas)
		# Re-finalize non-generic variants now that destructor_fns is
		# authoritative.  See the parallel invocation above (after the
		# semantic_world install) for the full rationale — without this,
		# variants whose droppability is derived from user Destructible
		# impls (not from built-in types like String/Array) miss their
		# auto-injected internal tombstone metadata.
		type_table.finalize_variants()
		# K42: construct Pass1State to eliminate duplicate resolution in
		# compile_stubbed_funcs — reuse typed_fns + resolution infrastructure.
		# Phase 6: precompute visibility_provenance_by_id so CSF doesn't need
		# to reconstruct it from visibility_provenance_by_name + module_ids.
		_p1_vis_prov_by_id: dict[int, tuple[str, ...]] = {}
		for _p1vp_name, _p1vp_id in module_ids.items():
			if _p1vp_name is not None:
				_p1_vis_prov_by_id[int(_p1vp_id)] = (str(_p1vp_name),)
		for _p1vp_name, _p1vp_prov in visibility_provenance_by_name.items():
			_p1vp_id = module_ids.get(_p1vp_name)
			if _p1vp_id is not None:
				for _p1vp_other, _p1vp_chain in _p1vp_prov.items():
					_p1vp_other_id = module_ids.get(_p1vp_other)
					if _p1vp_other_id is not None:
						_p1_vis_prov_by_id[int(_p1vp_other_id)] = _p1vp_chain
		_p1_state = Pass1State(
			typed_fns=typed_fns,
			callable_registry=callable_registry,
			impl_index=global_impl_index,
			trait_index=global_trait_index,
			trait_impl_index=global_trait_impl_index,
			trait_scope_by_module=trait_scope_by_module,
			linked_world=linked_world,
			require_env=require_env,
			visible_module_names_by_name=visible_module_names_by_name,
			module_ids=module_ids,
			method_wrapper_specs=method_wrapper_specs,
			unsafe_trusted_modules=unsafe_trusted_modules,
			pkg_unsafe_modules=_pkg_unsafe_modules,
			function_keys_by_fn_id=pass1_function_keys,
			visibility_provenance_by_id=_p1_vis_prov_by_id,
			lambda_fn_specs=dict(type_checker._lambda_fn_specs) if type_checker._lambda_fn_specs else None,
		)
		src_mir, checked_src, ssa_src = compile_stubbed_funcs(
			func_hirs=normalized_hirs_by_id,
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
			entry_module=entry_module,
			entry_name=entry_name,
			allow_unsafe=bool(getattr(args, "allow_unsafe", False)),
			pass1_state=_p1_state,
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
					_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
					print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
			return 1

		# Option B: all package functions compiled from HIR through
		# compile_stubbed_funcs.  Build CompilationUnit directly from
		# the unified MIR — no _build_package_consumer_unit needed.
		#
		# BFS reachability: only include functions reachable from entry.
		# This avoids codegen failures for unreachable generic wrappers.
		_entry_id: FunctionId | None = None
		for fn_id in src_mir:
			if fn_id.module == entry_module and fn_id.name == entry_name:
				_entry_id = fn_id
				break
		_reachable: set[FunctionId] = set()
		if _entry_id is not None:
			_bfs_queue: list[FunctionId] = [_entry_id]
			_reachable.add(_entry_id)
			# Seed implicit entry-wrapper deps (e.g., install_process_preamble).
			# These are called by the OS entry wrapper (hardcoded in codegen)
			# and won't appear in MIR call instructions.
			# Only seed if the dep module is transitively imported by the entry
			# module — under Option B, all package HIR is in src_mir but only
			# actually-imported modules should contribute entry wrapper deps.
			# Use module_deps (import graph), not visibility (which includes
			# all package modules for method resolution).
			_entry_transitive_imports: set[str] = set()
			if isinstance(module_deps, dict):
				_eti_queue = [entry_module]
				_entry_transitive_imports.add(entry_module)
				while _eti_queue:
					_eti_cur = _eti_queue.pop()
					for _eti_dep in (module_deps.get(_eti_cur) or set()):
						if _eti_dep not in _entry_transitive_imports:
							_entry_transitive_imports.add(_eti_dep)
							_eti_queue.append(_eti_dep)
			for _dep_mod, _dep_name in ENTRY_WRAPPER_IMPLICIT_DEPS.values():
				if _dep_mod not in _entry_transitive_imports:
					continue
				_dep_fid = FunctionId(module=_dep_mod, name=_dep_name, ordinal=0)
				if _dep_fid in src_mir and _dep_fid not in _reachable:
					_reachable.add(_dep_fid)
					_bfs_queue.append(_dep_fid)
			while _bfs_queue:
				_cur = _bfs_queue.pop()
				_cur_fn = src_mir.get(_cur)
				if _cur_fn is None:
					continue
				for _callee in _called_funcs_in_mir(_cur_fn):
					if _callee in src_mir and _callee not in _reachable:
						_reachable.add(_callee)
						_bfs_queue.append(_callee)
			# Seed destroyers via type-graph walk (K39): codegen emits
			# destroy calls that aren't in MIR instructions.  Use the
			# shared _seed_destroy_type_graph to handle the full
			# transitive closure (struct fields, variant arms, etc.).
			_dropped_types: set[int] = set()
			_phase1_destroyers: set[FunctionId] = set()
			destructor_fns = getattr(type_table, "destructor_fns", None) or {}
			for _fid in list(_reachable):
				_fn = src_mir.get(_fid)
				if _fn is None:
					continue
				for _blk in _fn.blocks.values():
					for _instr in _blk.instructions:
						if isinstance(_instr, M.DropValue):
							_dropped_types.add(_instr.ty)
							_dfn = destructor_fns.get(_instr.ty)
							if _dfn is not None and _dfn in src_mir and _dfn not in _reachable:
								_reachable.add(_dfn)
								_phase1_destroyers.add(_dfn)
			_seed_destroy_type_graph(
				initial_dropped_types=_dropped_types,
				destructor_fns=destructor_fns,
				mir_pool=src_mir,
				needed=_reachable,
				type_table=type_table,
				fn_infos=checked_src.fn_infos_by_id,
				pre_seeded_destroyers=_phase1_destroyers,
			)
		# Seed interface impl methods for ConstructIfaceValue boxing (K29)
		# and ArcAsInterface fat-handle construction (Stage 3).  Both ops
		# carry a concrete T and emit a T-as-I vtable at codegen time
		# that references `impl I for T` methods — the impl fns have no
		# MIR call instruction, so BFS from the caller never reaches
		# them.  Seed them here so the vtable symbols resolve at link.
		_iface_value_tys: set[int] = set()
		for _fid in list(_reachable):
			_fn = src_mir.get(_fid)
			if _fn is None:
				continue
			for _blk in _fn.blocks.values():
				for _instr in _blk.instructions:
					if isinstance(_instr, M.ConstructIfaceValue):
						_iface_value_tys.add(_instr.value_ty)
					elif isinstance(_instr, M.ArcAsInterface):
						_iface_value_tys.add(_instr.concrete_ty)
		if _iface_value_tys and checked_src:
			for _impl_fn_id, _impl_info in checked_src.fn_infos_by_id.items():
				_impl_sig = _impl_info.signature
				if _impl_sig is None or not _impl_sig.is_method:
					continue
				if _impl_sig.impl_target_type_id in _iface_value_tys and _impl_fn_id in src_mir and _impl_fn_id not in _reachable:
					_reachable.add(_impl_fn_id)
					_bfs_queue = [_impl_fn_id]
					while _bfs_queue:
						_cur = _bfs_queue.pop()
						_cur_fn = src_mir.get(_cur)
						if _cur_fn is None:
							continue
						for _callee in _called_funcs_in_mir(_cur_fn):
							if _callee in src_mir and _callee not in _reachable:
								_reachable.add(_callee)
								_bfs_queue.append(_callee)
		# Discover and synthesize missing wrappers using the shared helper
		# (same logic as _build_package_consumer_unit).
		_wrapper_target_map: dict[FunctionId, FunctionId] = {}
		_wrapper_sigs_map: dict[FunctionId, FnSignature] = {}
		for _wfid, _winfo in checked_src.fn_infos_by_id.items():
			_wsig = _winfo.signature
			if _wsig and getattr(_wsig, "is_wrapper", False) and getattr(_wsig, "wraps_target_fn_id", None):
				_wrapper_target_map[_wfid] = _wsig.wraps_target_fn_id
				_wrapper_sigs_map[_wfid] = _wsig
		for _wfid, _wsig in signatures_by_id_all.items():
			if getattr(_wsig, "is_wrapper", False) and getattr(_wsig, "wraps_target_fn_id", None) and _wfid not in _wrapper_target_map:
				_wrapper_target_map[_wfid] = _wsig.wraps_target_fn_id
				_wrapper_sigs_map[_wfid] = _wsig
		_discover_and_synthesize_wrappers(
			reachable=_reachable,
			mir_pool=src_mir,
			ssa_pool=ssa_src,
			wrapper_target_by_id=_wrapper_target_map,
			wrapper_sigs=_wrapper_sigs_map,
			fn_infos=checked_src.fn_infos_by_id,
			signatures_by_id=signatures_by_id_all,
			type_table=type_table,
		)
		# Synthesize per-I fat Arc destructor wrappers (ABI 10).
		# Scoped to fat `Arc<I>` instances that actually appear as
		# `DropValue.ty` in reachable MIR — see the filter inside
		# the helper.
		_synthesize_fat_arc_destructor_wrappers(
			type_table=type_table,
			mir_pool=src_mir,
			ssa_pool=ssa_src,
			fn_infos=checked_src.fn_infos_by_id,
			signatures_by_id=signatures_by_id_all,
			external_signatures_by_id=external_signatures_by_id,
			reachable=_reachable,
		)
		_reachable_mir = {fid: fn for fid, fn in src_mir.items() if fid in _reachable}
		_reachable_ssa = {fid: fn for fid, fn in ssa_src.items() if fid in _reachable}
		_all_fn_infos: dict[FunctionId, FnInfo] = {}
		for fn_id in _reachable:
			info = checked_src.fn_infos_by_id.get(fn_id)
			if info is not None:
				_all_fn_infos[fn_id] = info
		for fn_id, sig in external_signatures_by_id.items():
			if fn_id in _reachable and fn_id not in _all_fn_infos:
				_dct = True if sig.declared_can_throw is None else bool(sig.declared_can_throw)
				_all_fn_infos[fn_id] = make_fn_info(fn_id, sig, declared_can_throw=_dct)
		_rename_map: dict[FunctionId, str] = {}
		if _entry_id is not None:
			_rename_map[_entry_id] = "drift_main"
		unit = CompilationUnit(
			mir_funcs=_reachable_mir,
			ssa_funcs=_reachable_ssa,
			fn_infos=_all_fn_infos,
			type_table=type_table,
			rename_map=_rename_map,
			entry_id=_entry_id,
			wrapper_dep_flags={
				flag: any(fid.module == dep_mod and fid.name == dep_name for fid in _reachable_mir)
				for flag, (dep_mod, dep_name) in ENTRY_WRAPPER_IMPLICIT_DEPS.items()
			},
		)
		# Build-profile provenance label.  Sanitizer test modes take
		# precedence over the dual-runtime normal/debug-style binary
		# distinction; the unsanitized lane records "debug" when
		# DRIFT_DEBUG=1 is set and "default" otherwise.
		if _env_true("DRIFT_ASAN") and _env_true("DRIFT_UBSAN"):
			_build_profile = "asan_ubsan"
		elif _env_true("DRIFT_ASAN"):
			_build_profile = "asan"
		elif _env_true("DRIFT_UBSAN"):
			_build_profile = "ubsan"
		else:
			_build_profile = "debug" if debug_style_runtime else "default"
		# K26: Inject external_impl_metas into combined_exports so that
		# _build_interface_impl_index can find trait impls for vtable
		# emission during codegen.  This must happen AFTER type-checking
		# (compile_stubbed_funcs) to avoid false type-mismatch errors
		# from un-remapped TypeIds.
		if combined_exports is not None and external_impl_metas:
			for impl in external_impl_metas:
				mod = getattr(impl, "def_module", None)
				if mod is not None and mod in combined_exports:
					exp = combined_exports[mod]
					if isinstance(exp, dict):
						existing = exp.get("impls")
						if isinstance(existing, list):
							existing.append(impl)
						else:
							exp["impls"] = [impl]
		try:
			ir = _emit_codegen(
				unit,
				module_exports=combined_exports,
				word_bits=_target_word_bits(args.target_word_bits),
				debug_enabled=debug_enabled,
				provenance_git_sha=_toolchain_git_sha(),
				provenance_build_profile=_build_profile,
			)
		except AssertionError as err:
			msg = f"internal: LLVM lowering contract failure ({err})"
			if args.json:
				print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": "<source>", "line": None, "column": None}]}))
			else:
				print(f"{_source_label()}:?:?: error: {msg}", file=sys.stderr)
			return 1
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
					_code_suffix = f" [{d.code}]" if getattr(d, "code", None) else ""
					print(f"{_source_label()}:{loc}: {d.severity}: {d.message}{_code_suffix}", file=sys.stderr)
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

	clang = shutil.which("clang")
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
	# Backtrace symbolization libraries (libdw, libunwind, libunwind-x86_64,
	# libelf) are gated on the dual-runtime debug-style variant.  The normal
	# lane is the production-equivalent path; production hosts must be able
	# to run normal-lane binaries without these libraries installed.  The
	# matching source-side gating lives in
	# lang/language_runtime/posix/assert_runtime.c, which omits the libdwfl
	# + libunwind walk under -DDRIFT_RT_MODE_DEBUG=0 (the normal variant)
	# so the runtime archive's .o files have zero references to libdw /
	# libunwind / libelf symbols, and `--as-needed` would also drop them
	# even if they were left on the cmdline.  This branch is the
	# defense-in-depth that keeps them off the cmdline entirely under the
	# normal lane.
	if debug_style_runtime:
		link_libs = _link_flags_for_lib("dw") + _link_flags_for_lib("unwind") + _link_flags_for_lib("unwind-x86_64") + _link_flags_for_lib("elf")
	else:
		link_libs = []
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
	ubsan_enabled = _env_true("DRIFT_UBSAN")
	asan_flags = ["-fsanitize=address", "-g"] if asan_enabled else []
	ubsan_flags = ["-fsanitize=undefined", "-fno-sanitize-recover=undefined", "-g"] if ubsan_enabled else []
	# Default flip: production "normal" lane gets -O2.  DRIFT_DEBUG=1
	# (the debug-style lane) suppresses -O2.  Sanitizer/alloc_track test
	# modes ride on the normal lane and also get -O2 unless they are
	# layered with DRIFT_DEBUG=1.
	opt_flags = [] if debug_style_runtime else ["-O2"]
	# Runtime is always linked from the pre-built variant archive.  The
	# legacy `DRIFT_RUNTIME_LINK_MODE=source` inline-compile escape hatch
	# was removed in 0.27.179: it never applied the dual-runtime variant
	# cflags (silently bypassing the `-DDRIFT_RT_MODE_DEBUG=1` gate),
	# its asm step (`drift_context.S`) was broken under `-x c`, and no
	# test population exercised it.
	variant = runtime_archive_variant(
		debug_style=debug_style_runtime,
		asan_enabled=asan_enabled,
		ubsan_enabled=ubsan_enabled,
		alloc_track_enabled=False,
	)
	try:
		archive_path = build_runtime_archive(ROOT, clang=clang, variant=variant)
	except Exception as ex:
		msg = f"runtime archive build failed: {ex}"
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
			*ubsan_flags,
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
		link_cmd = [
			clang,
			*linker_flags,
			*asan_flags,
			*ubsan_flags,
			*opt_flags,
			str(ir_obj),
			runtime_archive,
			*link_libs,
			*gdb_index_flag,
			"-Wl,--as-needed",
			"-o",
			str(args.output),
		]
	else:
		link_cmd = [
			clang,
			*linker_flags,
			*asan_flags,
			*ubsan_flags,
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
	for obj in getattr(args, 'link_obj', []):
		link_cmd.append(obj)
	for search_path in getattr(args, 'link_search', []):
		link_cmd.extend(["-L", search_path])
	for lib in getattr(args, 'link_lib', []):
		link_cmd.extend([f"-l{lib}"])
	# Consumer auto-link: append native deps from loaded packages (after user --link-lib).
	_pkg_native_libs: list[str] = []
	_pkg_native_lib_sources: dict[str, tuple[str, str]] = {}  # lib -> (pkg_id, pkg_ver)
	if loaded_pkgs and not getattr(args, 'no_package_native_deps', False):
		_seen_libs: set[str] = set()
		for _lpkg in loaded_pkgs:
			_pkg_id = _lpkg.manifest.get("package_id", "?")
			_pkg_ver = _lpkg.manifest.get("package_version", "?")
			for _ndep in _lpkg.native_deps:
				if _ndep.lib not in _seen_libs:
					_seen_libs.add(_ndep.lib)
					_pkg_native_libs.append(_ndep.lib)
					_pkg_native_lib_sources[_ndep.lib] = (_pkg_id, _pkg_ver)
		for _plib in _pkg_native_libs:
			link_cmd.extend([f"-l{_plib}"])
	print("[driftc] link:", " ".join(link_cmd), file=sys.stderr)
	link_res = subprocess.run(link_cmd, capture_output=True, text=True, cwd=ROOT)
	if link_res.returncode != 0:
		msg = f"clang failed: {link_res.stderr.strip()}"
		abi_hint = ""
		if "__drift_rt_abi_version_" in link_res.stderr:
			from lang.driftc.driftc_versions import DRIFT_RT_ABI_VERSION as _abi_ver
			abi_hint = f"\nhint: driftc targets runtime ABI v{_abi_ver}; linked runtime provides a different ABI. Rebuild runtime/std artifacts (just runtime-libs)."
		# Diagnostic enrichment: hint about package-declared native libs.
		_native_dep_hints: list[str] = []
		for _plib, (_src_id, _src_ver) in _pkg_native_lib_sources.items():
			if f"-l{_plib}" in link_res.stderr:
				_native_dep_hints.append(f"hint: package '{_src_id}' (v{_src_ver}) requires native library '{_plib}' (-l{_plib}).\n      Install the development package for your distribution, or pass --link-search <dir> to specify the library location.")
		if _native_dep_hints:
			abi_hint += "\n" + "\n".join(_native_dep_hints)
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
