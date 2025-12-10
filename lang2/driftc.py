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
from pathlib import Path
from typing import Dict, Mapping, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from lang2 import stage1 as H
from lang2.stage1 import normalize_hir
from lang2.stage1.hir_utils import collect_catch_arms_from_block
from lang2.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2.stage3.throw_summary import ThrowSummaryBuilder
from lang2.stage4 import run_throw_checks
from lang2.stage4 import MirToSSA
from lang2.checker import Checker, CheckedProgram, FnSignature
from lang2.checker.catch_arms import CatchArmInfo
from lang2.borrow_checker_pass import BorrowChecker
from lang2.borrow_checker import PlaceBase, PlaceKind
from lang2.core.diagnostics import Diagnostic
from lang2.core.types_core import TypeTable
from lang2.codegen.llvm import lower_module_to_llvm
from lang2.parser import parse_drift_to_hir
from lang2.type_resolver import resolve_program_signatures
from lang2.type_checker import TypeChecker
from lang2.method_registry import CallableRegistry, CallableSignature, Visibility, SelfMode


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
	catch_arms_map: Dict[str, List[CatchArmInfo]] = {}
	normalized_hirs: Dict[str, H.HBlock] = {}
	for name, hir_block in func_hirs.items():
		hir_norm = normalize_hir(hir_block)
		normalized_hirs[name] = hir_norm
		arms = collect_catch_arms_from_block(hir_norm)
		if arms:
			catch_arms_map[name] = arms

	# If no signatures were supplied, resolve basic signatures from normalized HIR.
	shared_type_table = type_table
	if signatures is None:
		shared_type_table, signatures = resolve_program_signatures(_fake_decls_from_hirs(normalized_hirs))

	# Stage “checker”: obtain declared_can_throw from the checker stub so the
	# driver path mirrors the real compiler layering once a proper checker exists.
	checker = Checker(
		declared_can_throw=declared_can_throw,
		signatures=signatures,
		exception_catalog=exc_env,
		catch_arms=catch_arms_map,
		hir_blocks=func_hirs,
		type_table=shared_type_table,
	)
	checked = checker.check(func_hirs.keys())
	# Ensure declared_can_throw is a bool for downstream stages; guard against
	# accidental truthy objects sneaking in from legacy shims.
	for info in checked.fn_infos.values():
		if not isinstance(info.declared_can_throw, bool):
			info.declared_can_throw = bool(info.declared_can_throw)
	declared = {name: info.declared_can_throw for name, info in checked.fn_infos.items()}
	mir_funcs: Dict[str, M.MirFunc] = {}

	for name, hir_norm in normalized_hirs.items():
		builder = MirBuilder(name=name)
		sig = signatures.get(name)
		param_types: dict[str, "TypeId"] = {}
		if sig is not None and sig.param_names is not None:
			builder.func.params = list(sig.param_names)
		if sig is not None and sig.param_names is not None and sig.param_type_ids is not None:
			param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids)}
		HIRToMIR(builder, type_table=shared_type_table, exc_env=exc_env, param_types=param_types).lower_block(hir_norm)
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
			type_env = checker.build_type_env_from_ssa(ssa_funcs, signatures or {})
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
	# First, run the normal pipeline to get MIR + FnInfos + SSA (and diagnostics).
	mir_funcs, checked, ssa_funcs = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc_env,
		return_checked=True,
		build_ssa=True,
		return_ssa=True,
		type_table=type_table,
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

	module = lower_module_to_llvm(
		mir_funcs,
		ssa_funcs,
		checked.fn_infos,
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
	class _FakeParam:
		def __init__(self, name: str, typ: str = "Int") -> None:
			self.name = name
			self.type = typ

	class _FakeDecl:
		def __init__(self, name: str) -> None:
			self.name = name
			self.params: list[_FakeParam] = []
			self.return_type = "Int"
			self.throws = ()
			self.loc = None
			self.is_extern = False
			self.is_intrinsic = False

	return [_FakeDecl(name) for name in hirs.keys()]


__all__ = ["compile_stubbed_funcs", "compile_to_llvm_ir_for_tests"]


def _diag_to_json(diag: Diagnostic, phase: str, source: Path) -> dict:
	"""Render a Diagnostic to a structured JSON-friendly dict."""
	line = getattr(diag.span, "line", None) if diag.span is not None else None
	column = getattr(diag.span, "column", None) if diag.span is not None else None
	return {
		"phase": phase,
		"message": diag.message,
		"severity": diag.severity,
		"file": str(source),
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
	parser.add_argument("source", type=Path, help="Path to Drift source file")
	parser.add_argument(
		"--json",
		action="store_true",
		help="Emit diagnostics as JSON (phase/message/severity/file/line/column)",
	)
	args = parser.parse_args(argv)

	source_path: Path = args.source
	func_hirs, signatures, type_table, parse_diags = parse_drift_to_hir(source_path)

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

	# Type check each function with the shared TypeTable/signatures.
	type_checker = TypeChecker(type_table=type_table)
	callable_registry = CallableRegistry()
	next_callable_id = 1

	def _unwrap_ref(ty: "TypeId") -> "TypeId":
		td = type_table.get(ty)
		if td.kind is TypeKind.REF and td.param_types:
			return td.param_types[0]
		return ty

	if signatures:
		for sig_name, sig in signatures.items():
			if sig.param_type_ids is None or sig.return_type_id is None:
				continue
			param_types_tuple = tuple(sig.param_type_ids)
			if sig.is_method and sig.impl_target_type_id is not None and sig.self_mode is not None:
				self_mode = {
					"value": SelfMode.SELF_BY_VALUE,
					"ref": SelfMode.SELF_BY_REF,
					"ref_mut": SelfMode.SELF_BY_REF_MUT,
				}.get(sig.self_mode, SelfMode.SELF_BY_VALUE)
				callable_registry.register_inherent_method(
					callable_id=next_callable_id,
					name=sig_name,
					module_id=0,
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
					name=sig_name,
					module_id=0,
					visibility=Visibility.public(),
					signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
					is_generic=False,
				)
				next_callable_id += 1
	type_diags: list[Diagnostic] = []
	typed_fns: dict[str, object] = {}
	for fn_name, hir_block in func_hirs.items():
		# Build param type map from signatures when available.
		param_types: dict[str, "TypeId"] = {}
		sig = signatures.get(fn_name) if signatures else None
		if sig and sig.param_names and sig.param_type_ids:
			param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids) if pty is not None}
		result = type_checker.check_function(
			fn_name,
			hir_block,
			param_types=param_types,
			call_signatures=signatures,
			callable_registry=callable_registry,
			visible_modules=(0,),
			current_module=0,
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

	# No diagnostics produced. Borrow checker/codegen not yet wired; acknowledge success.
	if args.json:
		print(json.dumps({"exit_code": 0, "diagnostics": []}))
	return 0


if __name__ == "__main__":
	sys.exit(main())
