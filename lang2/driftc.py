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

from typing import Dict, Mapping, List, Tuple

from lang2 import stage1 as H
from lang2.stage1 import normalize_hir
from lang2.stage1.hir_utils import collect_catch_arms_from_block
from lang2.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2.stage3.throw_summary import ThrowSummaryBuilder
from lang2.stage4 import run_throw_checks
from lang2.stage4 import MirToSSA
from lang2.checker import Checker, CheckedProgram, FnSignature
from lang2.checker.catch_arms import CatchArmInfo
from lang2.core.diagnostics import Diagnostic
from lang2.codegen.llvm import lower_module_to_llvm


def compile_stubbed_funcs(
	func_hirs: Mapping[str, H.HBlock],
	declared_can_throw: Mapping[str, bool] | None = None,
	signatures: Mapping[str, FnSignature] | None = None,
	exc_env: Mapping[str, int] | None = None,
	return_checked: bool = False,
	build_ssa: bool = False,
	return_ssa: bool = False,
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
	# Normalize upfront so catch-arm collection and lowering share the same HIR.
	catch_arms_map: Dict[str, List[CatchArmInfo]] = {}
	normalized_hirs: Dict[str, H.HBlock] = {}
	for name, hir_block in func_hirs.items():
		hir_norm = normalize_hir(hir_block)
		normalized_hirs[name] = hir_norm
		arms = collect_catch_arms_from_block(hir_norm)
		if arms:
			catch_arms_map[name] = arms

	# Stage “checker”: obtain declared_can_throw from the checker stub so the
	# driver path mirrors the real compiler layering once a proper checker exists.
	checker = Checker(
		declared_can_throw=declared_can_throw,
		signatures=signatures,
		exception_catalog=exc_env,
		catch_arms=catch_arms_map,
		hir_blocks=func_hirs,
	)
	checked = checker.check(func_hirs.keys())
	declared = {name: info.declared_can_throw for name, info in checked.fn_infos.items()}
	mir_funcs: Dict[str, M.MirFunc] = {}

	for name, hir_norm in normalized_hirs.items():
		builder = MirBuilder(name=name)
		HIRToMIR(builder, exc_env=exc_env).lower_block(hir_norm)
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
	entry: str = "drift_main",
) -> tuple[str, CheckedProgram]:
	"""
	End-to-end helper: HIR -> MIR -> throw checks -> SSA -> LLVM IR for tests.

	This mirrors the stub driver pipeline and finishes by lowering SSA to LLVM IR.
	It is intentionally narrow: assumes a single Drift entry `drift_main` (or
	`entry`) returning `Int` or `FnResult<Int, Error>` and uses the v1 ABI.
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
	)

	# Lower module to LLVM IR and append the OS entry wrapper.
	module = lower_module_to_llvm(mir_funcs, ssa_funcs, checked.fn_infos)
	module.emit_entry_wrapper(entry)
	return module.render(), checked


__all__ = ["compile_stubbed_funcs", "compile_to_llvm_ir_for_tests"]
