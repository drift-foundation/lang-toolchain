# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
lang2 driftc stub (checker/driver scaffolding).

This is **not** a full compiler. It exists to document how the lang2 pipeline
should be orchestrated once a real parser/type checker lands:

AST -> HIR (stage0/1)
   -> normalize_hir (stage1) to desugar result-driven try sugar
   -> HIR->MIR (stage2)
   -> MIR pre-analysis + throw summaries (stage3)
   -> throw checks (stage4) using `declared_can_throw` from the checker

When the real parser/checker is available, this file should grow proper CLI
handling and diagnostics. For now it exposes a single helper
`compile_stubbed_funcs` to drive the existing stages in tests or prototypes.
"""

from __future__ import annotations

import argparse
import json
import sys
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, List, Tuple

# Repository root (lang2 lives under this).
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from lang2.driftc import stage1 as H
from lang2.driftc.stage1 import normalize_hir
from lang2.driftc.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2.driftc.stage3.throw_summary import ThrowSummaryBuilder
from lang2.driftc.stage4 import run_throw_checks
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.checker import Checker, CheckedProgram, FnSignature, FnInfo
from lang2.driftc.borrow_checker_pass import BorrowChecker
from lang2.driftc.borrow_checker import PlaceBase, PlaceKind
from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.types_core import TypeTable
from lang2.codegen.llvm import lower_module_to_llvm
from lang2.drift_core.runtime import get_runtime_sources
from lang2.driftc.parser import parse_drift_to_hir, parse_drift_files_to_hir, parse_drift_workspace_to_hir
from lang2.driftc.type_resolver import resolve_program_signatures
from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.type_checker import TypeChecker
from lang2.driftc.method_registry import CallableRegistry, CallableSignature, Visibility, SelfMode
from lang2.driftc.packages.dmir_pkg_v0 import canonical_json_bytes, sha256_hex, write_dmir_pkg_v0
from lang2.driftc.packages.provisional_dmir_v0 import encode_module_payload_v0, decode_mir_funcs, type_table_fingerprint
from lang2.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps
from lang2.driftc.packages.provider_v0 import (
	PackageTrustPolicy,
	collect_external_exports,
	discover_package_files,
	load_package_v0,
	load_package_v0_with_policy,
)
from lang2.driftc.packages.trust_v0 import TrustStore, load_trust_store_json, merge_trust_stores


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


def _remap_mir_func_typeids(fn: M.MirFunc, tid_map: dict[int, int]) -> None:
	"""
	Remap TypeId fields in a MirFunc in-place.

	Package payloads are produced independently, so their TypeIds must be mapped
	into the host link-time TypeTable before SSA/LLVM lowering.
	"""
	for block in fn.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.ZeroValue):
				instr.ty = int(_remap_tid(tid_map, instr.ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.AddrOfArrayElem, M.LoadRef, M.StoreRef)):
				instr.inner_ty = int(_remap_tid(tid_map, instr.inner_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.ConstructStruct):
				instr.struct_ty = int(_remap_tid(tid_map, instr.struct_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.ConstructVariant):
				instr.variant_ty = int(_remap_tid(tid_map, instr.variant_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.VariantTag):
				instr.variant_ty = int(_remap_tid(tid_map, instr.variant_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.VariantGetField):
				instr.variant_ty = int(_remap_tid(tid_map, instr.variant_ty))  # type: ignore[assignment]
				instr.field_ty = int(_remap_tid(tid_map, instr.field_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.StructGetField):
				instr.struct_ty = int(_remap_tid(tid_map, instr.struct_ty))  # type: ignore[assignment]
				instr.field_ty = int(_remap_tid(tid_map, instr.field_ty))  # type: ignore[assignment]
			elif isinstance(instr, M.AddrOfField):
				instr.struct_ty = int(_remap_tid(tid_map, instr.struct_ty))  # type: ignore[assignment]
				instr.field_ty = int(_remap_tid(tid_map, instr.field_ty))  # type: ignore[assignment]
			elif isinstance(instr, (M.ArrayLit, M.ArrayIndexLoad, M.ArrayIndexStore)):
				instr.elem_ty = int(_remap_tid(tid_map, instr.elem_ty))  # type: ignore[assignment]


def _inject_prelude(signatures: dict[str, FnSignature], type_table: TypeTable) -> None:
	"""
	Ensure the lang.core prelude trio is present in the signatures map.

	These are pure functions (not macros) that write UTF-8 text to stdout/stderr.
	They return Void (v2 wires a real Void type through the pipeline).
	"""
	string_id = type_table.ensure_string()
	void_id = type_table.ensure_void()
	for name in ("print", "println", "eprintln"):
		sym_name = name
		# Keyed by short name; module carries qualification.
		if sym_name in signatures:
			continue
		signatures[sym_name] = FnSignature(
			name=name,
			method_name=name,
			param_names=["text"],
			param_type_ids=[string_id],
			return_type_id=void_id,
			is_method=False,
			module="lang.core",
		)


def compile_stubbed_funcs(
	func_hirs: Mapping[str, H.HBlock],
	declared_can_throw: Mapping[str, bool] | None = None,
	signatures: Mapping[str, FnSignature] | None = None,
	exc_env: Mapping[str, int] | None = None,
	return_checked: bool = False,
	build_ssa: bool = False,
	return_ssa: bool = False,
	type_table: "TypeTable | None" = None,
	run_borrow_check: bool = False,
) -> (
	Dict[str, M.MirFunc]
	| tuple[Dict[str, M.MirFunc], CheckedProgram]
	| tuple[Dict[str, M.MirFunc], CheckedProgram, Dict[str, "MirToSSA.SsaFunc"] | None]
):
	"""
	Lower a set of HIR function bodies through the lang2 pipeline and run throw checks.

	Args:
	  func_hirs: mapping of function name -> HIR block (body).
	  declared_can_throw: optional mapping of fn name -> bool; **legacy test shim**.
	    Prefer `signatures` for new tests and treat this as deprecated.
	  signatures: optional mapping of fn name -> FnSignature. The real checker will
	    use parsed/type-checked signatures to derive throw intent; this parameter
	    lets tests mimic that shape without a full parser/type checker.
	  exc_env: optional exception environment (event name -> code) passed to HIRToMIR.
	  return_checked: when True, also return the CheckedProgram produced by the
	    checker so diagnostics/fn_infos can be asserted in integration tests.
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
	  # TODO: drop declared_can_throw once all callers provide signatures/parsing.

	Returns:
	  dict of function name -> lowered MIR function. When `return_checked` is
	  True, returns a `(mir_funcs, checked_program)` tuple.

	Notes:
	  In the driver path, throw-check violations are appended to
	  `checked.diagnostics`; direct calls to `run_throw_checks` without a
	  diagnostics sink still raise RuntimeError in tests. This helper exists
	  for tests/prototypes; a real CLI will build signatures and diagnostics
	  from parsed sources instead of the shims here.
	"""
	# Guard: signatures with TypeIds must come with a shared TypeTable so TypeKind
	# queries stay coherent end-to-end.
	if signatures is not None and type_table is None:
		for sig in signatures.values():
			if sig.return_type_id is not None or sig.param_type_ids is not None:
				raise ValueError("signatures with TypeIds require a shared type_table")

	# Normalize upfront so catch-arm collection and lowering share the same HIR.
	normalized_hirs: Dict[str, H.HBlock] = {name: normalize_hir(hir_block) for name, hir_block in func_hirs.items()}

	# If no signatures were supplied, resolve basic signatures from normalized HIR.
	shared_type_table = type_table
	if signatures is None:
		shared_type_table, signatures = resolve_program_signatures(_fake_decls_from_hirs(normalized_hirs))
	else:
		# Ensure TypeIds are resolved on supplied signatures using a shared table.
		if shared_type_table is None:
			shared_type_table = TypeTable()
		for sig in signatures.values():
			if sig.return_type_id is None and sig.return_type is not None:
				sig.return_type_id = resolve_opaque_type(sig.return_type, shared_type_table, module_id=getattr(sig, "module", None))
			if sig.param_type_ids is None and sig.param_types is not None:
				sig.param_type_ids = [resolve_opaque_type(p, shared_type_table, module_id=getattr(sig, "module", None)) for p in sig.param_types]

	# Stage “checker”: obtain declared_can_throw from the checker stub so the
	# driver path mirrors the real compiler layering once a proper checker exists.
	checker = Checker(
		declared_can_throw=declared_can_throw,
		signatures=signatures,
		exception_catalog=exc_env,
		hir_blocks=func_hirs,
		type_table=shared_type_table,
	)
	# Important: the checker needs metadata for both:
	# - functions we are compiling (have HIR bodies), and
	# - functions we only know by signature (callees, intrinsics, externs).
	#
	# Several downstream phases (HIR→MIR lowering and SSA typing) consult the
	# checker's `FnInfo` map to decide whether a callee is can-throw.
	decl_names: set[str] = set(func_hirs.keys())
	if signatures is not None:
		decl_names.update(signatures.keys())
	checked = checker.check(sorted(decl_names))
	# Ensure declared_can_throw is a bool for downstream stages; guard against
	# accidental truthy objects sneaking in from legacy shims.
	for info in checked.fn_infos.values():
		if not isinstance(info.declared_can_throw, bool):
			info.declared_can_throw = bool(info.declared_can_throw)
	declared = {name: info.declared_can_throw for name, info in checked.fn_infos.items()}
	# Prefer the checker's table when the caller did not supply one so TypeIds
	# stay coherent across lowering/codegen.
	if shared_type_table is None and checked.type_table is not None:
		shared_type_table = checked.type_table
	mir_funcs: Dict[str, M.MirFunc] = {}

	for name, hir_norm in normalized_hirs.items():
		builder = MirBuilder(name=name)
		sig = signatures.get(name)
		param_types: dict[str, "TypeId"] = {}
		if sig is not None and sig.param_names is not None:
			builder.func.params = list(sig.param_names)
		if sig is not None and sig.param_names is not None and sig.param_type_ids is not None:
			param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids)}
		HIRToMIR(
			builder,
			type_table=shared_type_table,
			exc_env=exc_env,
			param_types=param_types,
			signatures=signatures,
			can_throw_by_name=declared,
			return_type=sig.return_type_id if sig is not None else None,
		).lower_function_body(hir_norm)
		if sig is not None and sig.param_names is not None:
			builder.func.params = list(sig.param_names)
		mir_funcs[name] = builder.func

	# Stage3: summaries
	code_to_exc = {code: name for name, code in (exc_env or {}).items()}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc=code_to_exc)

	# Optional SSA/type-env for typed throw checks
	ssa_funcs = None
	type_env = checked.type_env
	if build_ssa:
		ssa_funcs = {name: MirToSSA().run(func) for name, func in mir_funcs.items()}
		if type_env is None:
			# First preference: checker-owned SSA typing using TypeIds + signatures.
			type_env = checker.build_type_env_from_ssa(ssa_funcs, signatures or {}, can_throw_by_name=declared)
			checked.type_env = type_env
		if type_env is None and signatures is not None:
			# Fallback: minimal checker TypeEnv that tags return SSA values with the
			# signature return TypeId. This keeps type-aware checks usable even when
			# the fuller SSA typing could not derive any facts.
			type_env = build_minimal_checker_type_env(checked, ssa_funcs, signatures, table=checked.type_table)
			checked.type_env = type_env

	# Stage4: throw checks
	run_throw_checks(
		funcs=mir_funcs,
		summaries=summaries,
		declared_can_throw=declared,
		type_env=type_env or checked.type_env,
		fn_infos=checked.fn_infos,
		ssa_funcs=ssa_funcs,
		diagnostics=checked.diagnostics,
	)

	if return_checked and return_ssa:
		return mir_funcs, checked, ssa_funcs
	if return_checked:
		return mir_funcs, checked
	return mir_funcs


def compile_to_llvm_ir_for_tests(
	func_hirs: Mapping[str, H.HBlock],
	signatures: Mapping[str, FnSignature],
	exc_env: Mapping[str, int] | None = None,
	entry: str = "main",
	type_table: "TypeTable | None" = None,
) -> tuple[str, CheckedProgram]:
	"""
	End-to-end helper: HIR -> MIR -> throw checks -> SSA -> LLVM IR for tests.

	This mirrors the stub driver pipeline and finishes by lowering SSA to LLVM IR.
	It is intentionally narrow: assumes a single Drift entry `drift_main` (or
	`entry`) returning `Int`, `String`, or `FnResult<Int, Error>` and uses the
	v1 ABI.
	Returns IR text and the CheckedProgram so callers can assert diagnostics.
	"""
	# Ensure prelude signatures are present for tests that bypass the CLI.
	shared_type_table = type_table or TypeTable()
	_inject_prelude(signatures, shared_type_table)

	# In the real compiler, any error diagnostics stop the pipeline before
	# lowering/codegen. Mirror that in tests so negative cases can assert on
	# diagnostics without tripping MIR/LLVM invariants (assertions).
	precheck = Checker(
		declared_can_throw=None,
		signatures=signatures,
		exception_catalog=exc_env,
		hir_blocks=dict(func_hirs),
		type_table=shared_type_table,
	)
	prechecked = precheck.check(func_hirs.keys())
	if any(d.severity == "error" for d in prechecked.diagnostics):
		return "", prechecked

	# First, run the normal pipeline to get MIR + FnInfos + SSA (and diagnostics).
	mir_funcs, checked, ssa_funcs = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc_env,
		return_checked=True,
		build_ssa=True,
		return_ssa=True,
		type_table=shared_type_table,
	)

	# Lower module to LLVM IR and append the OS entry wrapper when needed.
	rename_map: dict[str, str] = {}
	argv_wrapper: str | None = None
	entry_info = checked.fn_infos.get(entry)
	# Detect main(argv: Array<String>) and emit a C-ABI wrapper that builds argv.
	if (
		entry == "main"
		and entry_info
		and entry_info.signature
		and entry_info.signature.param_type_ids
		and len(entry_info.signature.param_type_ids) == 1
		and checked.type_table is not None
	):
		param_ty = entry_info.signature.param_type_ids[0]
		td = checked.type_table.get(param_ty)
		if td.kind.name == "ARRAY" and td.param_types:
			elem_td = checked.type_table.get(td.param_types[0])
			if elem_td.name == "String":
				# Guard: require return Int and exactly one param of Array<String>.
				if entry_info.signature.return_type_id != checked.type_table.ensure_int():
					raise ValueError("main(argv: Array<String>) must return Int")
				rename_map["main"] = "drift_main"
				argv_wrapper = "drift_main"

	# Add prelude FnInfos so codegen can recognize console intrinsics by module/name.
	fn_infos = dict(checked.fn_infos)
	for name in ("print", "println", "eprintln"):
		if name in signatures and name not in fn_infos:
			fn_infos[name] = FnInfo(name=name, declared_can_throw=False, signature=signatures[name])

	module = lower_module_to_llvm(
		mir_funcs,
		ssa_funcs,
		fn_infos,
		type_table=checked.type_table,
		rename_map=rename_map,
		argv_wrapper=argv_wrapper,
	)
	# If the entry is already called "main" and has no argv wrapper, do not emit
	# a wrapper that would call itself; otherwise emit a thin OS wrapper that
	# calls the entry.
	if argv_wrapper is None and entry != "main":
		module.emit_entry_wrapper(entry)
	return module.render(), checked


def _fake_decls_from_hirs(hirs: Mapping[str, H.HBlock]) -> list[object]:
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
	for name, block in hirs.items():
		ret_ty = "Int"
		if isinstance(block, H.HBlock):
			val_ret, void_ret = _scan_returns(block)
			if void_ret and not val_ret:
				ret_ty = "Void"
		decls.append(FakeDecl(name=name, params=[], return_type=ret_ty))
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
		file = str(source)
	phase = getattr(diag, "phase", None) or phase
	return {
		"phase": phase,
		"message": diag.message,
		"severity": diag.severity,
		"file": file,
		"line": line,
		"column": column,
	}


def main(argv: list[str] | None = None) -> int:
	"""
	Minimal CLI: parses a Drift file, type checks, then borrow checks. If any stage
	emits errors, compilation fails.

	With --json, prints structured diagnostics (phase/message/severity/file/line/column)
	and an exit_code; otherwise prints human-readable messages to stderr.
	"""
	parser = argparse.ArgumentParser(description="lang2 driftc stub")
	parser.add_argument("source", type=Path, nargs="+", help="Path(s) to Drift source file(s)")
	parser.add_argument(
		"-M",
		"--module-path",
		dest="module_paths",
		action="append",
		type=Path,
		help="Module root directory (repeatable); when provided, module ids are inferred from file paths under these roots",
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
		"--require-signatures",
		action="store_true",
		help="Require signatures for all packages (including local build outputs)",
	)
	parser.add_argument("-o", "--output", type=Path, help="Path to output executable")
	parser.add_argument("--emit-ir", type=Path, help="Write LLVM IR to the given path")
	parser.add_argument("--emit-package", type=Path, help="Write an unsigned package artifact (.dmp) to the given path")
	parser.add_argument("--package-id", type=str, help="Package identity (required with --emit-package)")
	parser.add_argument("--package-version", type=str, help="Package version (SemVer; required with --emit-package)")
	parser.add_argument("--package-target", type=str, help="Target triple (required with --emit-package)")
	parser.add_argument("--package-build-epoch", type=str, default=None, help="Optional build epoch label (non-semantic)")
	parser.add_argument(
		"--json",
		action="store_true",
		help="Emit diagnostics as JSON (phase/message/severity/file/line/column)",
	)
	args = parser.parse_args(argv)

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
			msg = f"trust store not found: {project_trust_path}"
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
									"file": str(project_trust_path),
									"line": None,
									"column": None,
								}
							],
						}
					)
				)
			else:
				print(f"{project_trust_path}:?:?: error: {msg}", file=sys.stderr)
			return 1

		merged_trust = project_trust
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
										"file": str(pkg_path),
										"line": None,
										"column": None,
									}
								],
							}
						)
					)
				else:
					print(f"{pkg_path}:?:?: error: {msg}", file=sys.stderr)
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
				msg = f"package '{pkg.path}' missing package identity fields"
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": str(pkg.path), "line": None, "column": None}]}))
				else:
					print(f"{pkg.path}:?:?: error: {msg}", file=sys.stderr)
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
					f"'{prev_ver}' ({prev_target}) from '{prev_path}' and '{pkg_ver}' ({pkg_target}) from '{pkg.path}'"
				)
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": str(pkg.path), "line": None, "column": None}]}))
				else:
					print(f"{pkg.path}:?:?: error: {msg}", file=sys.stderr)
				return 1
			if pkg_sha != prev_sha and pkg.path != prev_path:
				msg = (
					f"duplicate package id '{pkg_id}' in build from different artifacts: "
					f"'{prev_path}' and '{pkg.path}'"
				)
				if args.json:
					print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": str(pkg.path), "line": None, "column": None}]}))
				else:
					print(f"{pkg.path}:?:?: error: {msg}", file=sys.stderr)
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
					msg = f"module '{mid}' provided by multiple packages: '{prev}' and '{pkg.path}'"
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
											"file": str(source_path),
											"line": None,
											"column": None,
										}
									],
								}
							)
						)
					else:
						print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
					return 1
		external_exports = collect_external_exports(loaded_pkgs)

	func_hirs, signatures, type_table, exception_catalog, module_exports, parse_diags = parse_drift_workspace_to_hir(
		source_paths,
		module_paths=module_paths,
		external_module_exports=external_exports,
	)
	_inject_prelude(signatures, type_table)

	if parse_diags:
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "parser", source_path) for d in parse_diags],
			}
			print(json.dumps(payload))
		else:
			for d in parse_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{source_path}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
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
	type_table.new_optional(type_table.ensure_int())
	type_table.new_optional(type_table.ensure_bool())
	type_table.new_optional(type_table.ensure_string())

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
					msg = f"package '{pkg.path}' module '{mid}' is missing type_table"
					if args.json:
						print(
							json.dumps(
								{
									"exit_code": 1,
									"diagnostics": [
										{"phase": "package", "message": msg, "severity": "error", "file": str(pkg.path), "line": None, "column": None}
									],
								}
							)
						)
					else:
						print(f"{pkg.path}:?:?: error: {msg}", file=sys.stderr)
					return 1
				if pkg_tt_obj is None:
					pkg_tt_obj = tt
				else:
					if type_table_fingerprint(tt) != type_table_fingerprint(pkg_tt_obj):
						msg = f"package '{pkg.path}' contains inconsistent type_table across modules"
						if args.json:
							print(
								json.dumps(
									{
										"exit_code": 1,
										"diagnostics": [
											{"phase": "package", "message": msg, "severity": "error", "file": str(pkg.path), "line": None, "column": None}
										],
									}
								)
							)
						else:
							print(f"{pkg.path}:?:?: error: {msg}", file=sys.stderr)
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
				print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": str(source_path), "line": None, "column": None}]}))
			else:
				print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
			return 1
		for path, tid_map in zip(pkg_paths, maps):
			pkg_typeid_maps[path] = tid_map

	# If package roots were provided, merge package signatures into the signature
	# environment so type checking can validate calls to imported functions.
	if loaded_pkgs:
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
												"file": str(source_path),
												"line": None,
												"column": None,
											}
										],
									}
								)
							)
						else:
							print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
						return 1
					if name in signatures:
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
					signatures[name] = FnSignature(
						name=name,
						module=sd.get("module"),
						method_name=sd.get("method_name"),
						param_names=sd.get("param_names"),
						param_type_ids=param_type_ids,
						return_type_id=ret_tid,
						is_method=bool(sd.get("is_method", False)),
						self_mode=sd.get("self_mode"),
						impl_target_type_id=impl_tid,
						is_exported_entrypoint=bool(sd.get("is_exported_entrypoint", False)),
					)

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
								print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": str(pkg.path), "line": None, "column": None}]}))
							else:
								print(f"{pkg.path}:?:?: error: {msg}", file=sys.stderr)
							return 1
						continue
					type_table.define_const(module_id=mid, name=cname, type_id=remapped_tid, value=val)

	# Checker (stub) enforces language-level rules (e.g., Void returns) before the
	# lower-level TypeChecker/BorrowChecker run.
	# Normalize HIR before any further analysis so:
	# - sugar does not leak into later stages, and
	# - borrow materialization runs before borrow checking.
	func_hirs = {name: normalize_hir(block) for name, block in func_hirs.items()}
	checker = Checker(
		declared_can_throw=None,
		signatures=signatures,
		exception_catalog=exception_catalog,
		hir_blocks=func_hirs,
		type_table=type_table,
	)
	checked = checker.check(func_hirs.keys())
	if checked.type_table is not None:
		type_table = checked.type_table
	if checked.diagnostics:
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "typecheck", source_path) for d in checked.diagnostics],
			}
			print(json.dumps(payload))
		else:
			for d in checked.diagnostics:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{source_path}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
		return 1

	# Type check each function with the shared TypeTable/signatures.
	type_checker = TypeChecker(type_table=type_table)
	callable_registry = CallableRegistry()
	next_callable_id = 1
	type_diags: list[Diagnostic] = []
	module_ids: dict[object, int] = {None: 0}

	if signatures:
		for sig_symbol, sig in signatures.items():
			if sig.param_type_ids is None or sig.return_type_id is None:
				continue
			param_types_tuple = tuple(sig.param_type_ids)
			module_id = module_ids.setdefault(sig.module, len(module_ids))
			if sig.is_method:
				if sig.impl_target_type_id is None or sig.self_mode is None:
					type_diags.append(
						Diagnostic(
							message=f"method '{sig_symbol}' missing receiver metadata (impl target/self_mode)",
							severity="error",
							span=getattr(sig, "loc", None),
						)
					)
					continue
				self_mode = {
					"value": SelfMode.SELF_BY_VALUE,
					"ref": SelfMode.SELF_BY_REF,
					"ref_mut": SelfMode.SELF_BY_REF_MUT,
				}.get(sig.self_mode)
				if self_mode is None:
					type_diags.append(
						Diagnostic(
							message=f"method '{sig_symbol}' has unsupported self_mode '{sig.self_mode}'",
							severity="error",
							span=getattr(sig, "loc", None),
						)
					)
					continue
				callable_registry.register_inherent_method(
					callable_id=next_callable_id,
					name=sig.method_name or sig_symbol,
					module_id=module_id,
					visibility=Visibility.public(),
					signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
					impl_id=next_callable_id,
					impl_target_type_id=sig.impl_target_type_id,
					self_mode=self_mode,
					is_generic=False,
				)
				next_callable_id += 1
			else:
				callable_registry.register_free_function(
					callable_id=next_callable_id,
					# Workspace builds qualify call sites (`mod::fn`). Keep the callable
					# registry aligned with that identity to avoid string-rewrite
					# mismatches during resolution.
					name=sig_symbol,
					module_id=module_id,
					visibility=Visibility.public(),
					signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
					is_generic=False,
				)
				next_callable_id += 1
	# Build a name-keyed map for free-function signatures (fallback path only).
	call_sigs_by_name: dict[str, FnSignature] = {}
	if signatures:
		for sig in signatures.values():
			if not sig.is_method:
				call_sigs_by_name[sig.name] = sig

	typed_fns: dict[str, object] = {}
	for fn_name, hir_block in func_hirs.items():
		# Build param type map from signatures when available.
		param_types: dict[str, "TypeId"] = {}
		sig = signatures.get(fn_name) if signatures else None
		if sig and sig.param_names and sig.param_type_ids:
			param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids) if pty is not None}
		fn_module_id = module_ids.get(sig.module, 0) if sig is not None else 0
		result = type_checker.check_function(
			fn_name,
			hir_block,
			param_types=param_types,
			return_type=sig.return_type_id if sig is not None else None,
			call_signatures=call_sigs_by_name,
			callable_registry=callable_registry,
			visible_modules=tuple(module_ids.values()),
			current_module=fn_module_id,
		)
		type_diags.extend(result.diagnostics)
		typed_fns[fn_name] = result.typed_fn

	if type_diags:
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "typecheck", source_path) for d in type_diags],
			}
			print(json.dumps(payload))
		else:
			for d in type_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{source_path}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
		return 1

	# Borrow check each typed function (mandatory stage).
	borrow_diags: list[Diagnostic] = []
	for fn_name, typed_fn in typed_fns.items():
		bc = BorrowChecker.from_typed_fn(typed_fn, type_table=type_table, signatures=signatures, enable_auto_borrow=True)
		borrow_diags.extend(bc.check_block(typed_fn.body))

	if borrow_diags:
		if args.json:
			payload = {
				"exit_code": 1,
				"diagnostics": [_diag_to_json(d, "borrowcheck", source_path) for d in borrow_diags],
			}
			print(json.dumps(payload))
		else:
			for d in borrow_diags:
				loc = f"{getattr(d.span, 'line', '?')}:{getattr(d.span, 'column', '?')}" if d.span else "?:?"
				print(f"{source_path}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
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
									"file": str(source_path),
									"line": None,
									"column": None,
								}
							],
						}
					)
				)
			else:
				print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
			return 1

		mir_funcs, checked_pkg = compile_stubbed_funcs(
			func_hirs=func_hirs,
			signatures=signatures,
			exc_env=exception_catalog,
			type_table=type_table,
			return_checked=True,
		)
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
					print(f"{source_path}:{loc}: {d.severity}: {d.message}", file=sys.stderr)
			return 1

		# Group functions/signatures by module id.
		per_module_sigs: dict[str, dict[str, FnSignature]] = {}
		for name, sig in signatures.items():
			mid = getattr(sig, "module", None) or "main"
			per_module_sigs.setdefault(mid, {})[name] = sig

		per_module_mir: dict[str, dict[str, object]] = {}
		for name, fn in mir_funcs.items():
			sig = signatures.get(name)
			mid = getattr(sig, "module", None) if sig is not None else None
			mid = mid or "main"
			per_module_mir.setdefault(mid, {})[name] = fn

		blobs_by_sha: dict[str, bytes] = {}
		blob_types: dict[str, int] = {}
		blob_names: dict[str, str] = {}
		manifest_modules: list[dict[str, object]] = []
		manifest_blobs: dict[str, dict[str, object]] = {}

		all_module_ids: set[str] = set(per_module_sigs.keys()) | set(per_module_mir.keys())
		if isinstance(module_exports, dict):
			all_module_ids |= set(str(k) for k in module_exports.keys())
		for mid in sorted(all_module_ids):
			# MVP packaging: do not bundle the built-in prelude module. It is
			# supplied by the toolchain and will later be distributed as its own
			# package under the `std.*` namespace.
			if mid == "lang.core":
				continue

			# Export surface uses module-local names (unqualified). Global names
			# inside the compiler are qualified (`mid::name`).
			exported_values: list[str] = []
			for sym_name, sig in per_module_sigs.get(mid, {}).items():
				if not getattr(sig, "is_exported_entrypoint", False):
					continue
				if sig.is_method:
					continue
				prefix = f"{mid}::"
				exported_values.append(sym_name[len(prefix) :] if sym_name.startswith(prefix) else sym_name)
			exported_values.sort()

			exported_types_obj: object = {}
			reexports_obj: object = {}
			if isinstance(module_exports, dict):
				mexp = module_exports.get(mid, {})
				if isinstance(mexp, dict):
					exported_types_obj = mexp.get("types", {})
					reexports_obj = mexp.get("reexports", {})
			if not isinstance(exported_types_obj, dict):
				exported_types_obj = {}
			if not isinstance(reexports_obj, dict):
				reexports_obj = {}
			exported_types: dict[str, list[str]] = {
				"structs": list(exported_types_obj.get("structs", [])) if isinstance(exported_types_obj.get("structs"), list) else [],
				"variants": list(exported_types_obj.get("variants", [])) if isinstance(exported_types_obj.get("variants"), list) else [],
				"exceptions": list(exported_types_obj.get("exceptions", [])) if isinstance(exported_types_obj.get("exceptions"), list) else [],
			}
			exported_consts: list[str] = (
				list(module_exports.get(mid, {}).get("consts", [])) if isinstance(module_exports, dict) else []
			)

			payload_obj = encode_module_payload_v0(
				module_id=mid,
				type_table=checked_pkg.type_table or type_table,
				signatures=per_module_sigs.get(mid, {}),
				mir_funcs=per_module_mir.get(mid, {}),
				exported_values=exported_values,
				exported_types=exported_types,
				exported_consts=exported_consts,
				reexports=reexports_obj,
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
											"file": str(source_path),
											"line": None,
											"column": None,
										}
									],
								}
							)
						)
					else:
						print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
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
					if isinstance(raw, list) and len(raw) == 2 and isinstance(raw[1], list):
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
						"types": {"structs": [], "variants": [], "exceptions": []},
						"consts": [],
					},
				),
				"reexports": payload_obj.get("reexports", {}) if isinstance(payload_obj, dict) else {},
				"signatures": iface_sigs,
				"exception_schemas": iface_exc,
				"variant_schemas": iface_var,
				"consts": payload_obj.get("consts", {}) if isinstance(payload_obj, dict) else {},
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
					"exports": {"values": exported_values, "types": exported_types, "consts": exported_consts},
					"interface_blob": f"sha256:{iface_sha}",
					"payload_blob": f"sha256:{payload_sha}",
				}
			)

		manifest_obj: dict[str, object] = {
			"format": "dmir-pkg",
			"format_version": 0,
			"package_id": str(args.package_id),
			"package_version": str(args.package_version),
			"target": str(args.package_target),
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

	# Require entry point main for codegen.
	if "main" not in func_hirs:
		msg = "missing entry point 'main' for code generation"
		if args.json:
			print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": str(source_path), "line": None, "column": None}]}))
		else:
			print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
		return 1

	if loaded_pkgs:
		# Compile source functions through the normal pipeline to get MIR+SSA.
		src_mir, checked_src, ssa_src = compile_stubbed_funcs(
			func_hirs=func_hirs,
			signatures=signatures,
			exc_env=exception_catalog,
			return_checked=True,
			build_ssa=True,
			return_ssa=True,
			type_table=type_table,
		)
		ssa_src = ssa_src or {}

		# Decode package MIR payloads. We intentionally do not blindly embed all
		# loaded package modules; instead we include only the call-graph closure
		# reachable from the source module(s). This keeps builds predictable and
		# avoids unnecessary collisions/work.
		pkg_mir_all: dict[str, M.MirFunc] = {}
		pkg_sigs: dict[str, FnSignature] = {}
		for pkg in loaded_pkgs:
			tid_map = pkg_typeid_maps.get(pkg.path, {})
			for _mid, mod in pkg.modules_by_id.items():
				payload = mod.payload
				if not isinstance(payload, dict):
					continue
				if payload.get("payload_kind") != "provisional-dmir" or payload.get("payload_version") != 0:
					msg = f"unsupported package payload kind/version in {pkg.path}"
					if args.json:
						print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "package", "message": msg, "severity": "error", "file": str(source_path), "line": None, "column": None}]}))
					else:
						print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
					return 1
				mir_obj = payload.get("mir_funcs")
				if isinstance(mir_obj, dict):
					for name, fn in decode_mir_funcs(mir_obj).items():
						if isinstance(fn, M.MirFunc):
							_remap_mir_func_typeids(fn, tid_map)
							pkg_mir_all[name] = fn
				sigs_obj = payload.get("signatures")
				if isinstance(sigs_obj, dict):
					for name, sd in sigs_obj.items():
						if name in pkg_sigs:
							continue
						if not isinstance(sd, dict):
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
						pkg_sigs[name] = FnSignature(
							name=str(sd.get("name") or name),
							module=sd.get("module"),
							method_name=sd.get("method_name"),
							param_names=sd.get("param_names"),
							param_type_ids=param_type_ids,
							return_type_id=ret_tid,
							is_method=bool(sd.get("is_method", False)),
							self_mode=sd.get("self_mode"),
							impl_target_type_id=impl_tid,
							is_exported_entrypoint=bool(sd.get("is_exported_entrypoint", False)),
						)

		# SSA for package functions (required for LLVM lowering v1).
		def _called_funcs_in_mir(fn: M.MirFunc) -> set[str]:
			calls: set[str] = set()
			for block in fn.blocks.values():
				for instr in block.instructions:
					if isinstance(instr, M.Call):
						calls.add(instr.fn)
			return calls

		# Roots: any call target from source MIR that is defined by a package.
		needed: set[str] = set()
		for fn in src_mir.values():
			for callee in _called_funcs_in_mir(fn):
				if callee in pkg_mir_all:
					needed.add(callee)

		# Expand to call-graph closure through package functions.
		queue = list(sorted(needed))
		while queue:
			cur = queue.pop()
			fn = pkg_mir_all.get(cur)
			if fn is None:
				continue
			for callee in _called_funcs_in_mir(fn):
				if callee in pkg_mir_all and callee not in needed:
					needed.add(callee)
					queue.append(callee)

		pkg_mir: dict[str, M.MirFunc] = {name: pkg_mir_all[name] for name in sorted(needed)}

		pkg_ssa: dict[str, MirToSSA.SsaFunc] = {}
		for name, fn in pkg_mir.items():
			pkg_ssa[name] = MirToSSA().run(fn)

		# Merge (source wins on symbol conflicts).
		mir_all = dict(pkg_mir)
		mir_all.update(src_mir)
		ssa_all = dict(pkg_ssa)
		ssa_all.update(ssa_src)

		# FnInfos: include source + package signatures so codegen can type calls.
		fn_infos = dict(checked_src.fn_infos)
		all_sig_env = dict(pkg_sigs)
		all_sig_env.update(signatures)
		for name, sig in all_sig_env.items():
			if name in fn_infos:
				continue
			fn_infos[name] = FnInfo(name=name, declared_can_throw=bool(getattr(sig, "declared_can_throw", False)), signature=sig)

		module = lower_module_to_llvm(
			mir_all,
			ssa_all,
			fn_infos,
			type_table=checked_src.type_table,
			rename_map={},
			argv_wrapper=None,
		)
		ir = module.render()
	else:
		ir, _checked = compile_to_llvm_ir_for_tests(
			func_hirs=func_hirs,
			signatures=signatures,
			exc_env=exception_catalog,
			entry="main",
			type_table=type_table,
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
			print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": str(source_path), "line": None, "column": None}]}))
		else:
			print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
		return 1

	args.output.parent.mkdir(parents=True, exist_ok=True)
	ir_path = args.output.with_suffix(".ll")
	ir_path.write_text(ir)

	runtime_sources = [str(p) for p in get_runtime_sources(ROOT)]
	link_cmd = [
		clang,
		"-x",
		"ir",
		str(ir_path),
		"-x",
		"c",
		*runtime_sources,
		"-o",
		str(args.output),
	]
	link_res = subprocess.run(link_cmd, capture_output=True, text=True, cwd=ROOT)
	if link_res.returncode != 0:
		msg = f"clang failed: {link_res.stderr.strip()}"
		if args.json:
			print(json.dumps({"exit_code": 1, "diagnostics": [{"phase": "codegen", "message": msg, "severity": "error", "file": str(source_path), "line": None, "column": None}]}))
		else:
			print(f"{source_path}:?:?: error: {msg}", file=sys.stderr)
		return 1

	if args.json:
		print(json.dumps({"exit_code": 0, "diagnostics": []}))
	return 0


if __name__ == "__main__":
	sys.exit(main())
