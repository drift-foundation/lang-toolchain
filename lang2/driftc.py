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
from lang2.checker import Checker, CheckedProgram, FnSignature
from lang2.checker.catch_arms import CatchArmInfo
from lang2.diagnostics import Diagnostic
from lang2.types_env_impl import build_type_env_from_ssa


def compile_stubbed_funcs(
	func_hirs: Mapping[str, H.HBlock],
	declared_can_throw: Mapping[str, bool] | None = None,
	signatures: Mapping[str, FnSignature] | None = None,
	exc_env: Mapping[str, int] | None = None,
	return_checked: bool = False,
	build_ssa: bool = False,
) -> Dict[str, M.MirFunc] | tuple[Dict[str, M.MirFunc], CheckedProgram]:
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
	    to supply `checked.type_env`; the SSA-based bridge is a temporary fallback.

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
	)
	checked = checker.check(func_hirs.keys())
	declared = {name: info.declared_can_throw for name, info in checked.fn_infos.items()}
	mir_funcs: Dict[str, M.MirFunc] = {}

	for name, hir_norm in normalized_hirs.items():
		builder = MirBuilder(name=name)
		HIRToMIR(builder, exc_env=exc_env).lower_block(hir_norm)
		mir_funcs[name] = builder.func

	# Stage3: summaries
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc=exc_env or {})

	# Optional SSA/type-env for typed throw checks
	ssa_funcs = None
	type_env = checked.type_env
	if build_ssa:
		from lang2.stage4 import MirToSSA  # local import to avoid unused deps
		from lang2.types_env_impl import build_type_env_from_ssa
		from lang2.checker.type_env_bridge import build_checker_type_env_from_inferred
		from lang2.checker.type_env_builder import build_minimal_checker_type_env

		ssa_funcs = {name: MirToSSA().run(func) for name, func in mir_funcs.items()}
		# Prefer a checker-owned TypeEnv (even minimal) when possible.
		if type_env is None and signatures is not None:
			type_env = build_minimal_checker_type_env(checked, ssa_funcs, signatures, table=checked.type_table)
			checked.type_env = type_env
		if type_env is None:
			# Bridge: infer types from SSA/signatures and wrap into a checker-owned TypeEnv
			inferred = build_type_env_from_ssa(ssa_funcs, signatures=signatures)
			type_env = build_checker_type_env_from_inferred(inferred, ssa_funcs, signatures or {})
			checked.type_env = type_env

	# Stage4: throw checks
	run_throw_checks(
		funcs=mir_funcs,
		summaries=summaries,
		declared_can_throw=declared,
		type_env=type_env or checked.type_env,
		ssa_funcs=ssa_funcs,
		diagnostics=checked.diagnostics,
	)

	if return_checked:
		return mir_funcs, checked
	return mir_funcs


__all__ = ["compile_stubbed_funcs"]
