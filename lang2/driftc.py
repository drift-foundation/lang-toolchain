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

from typing import Dict, Mapping

from lang2 import stage1 as H
from lang2.stage1 import normalize_hir
from lang2.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2.stage3.throw_summary import ThrowSummaryBuilder
from lang2.stage4 import run_throw_checks
from lang2.checker import Checker
from lang2.diagnostics import Diagnostic


def compile_stubbed_funcs(
	func_hirs: Mapping[str, H.HBlock],
	declared_can_throw: Mapping[str, bool] | None = None,
	exc_env: Mapping[str, int] | None = None,
) -> Dict[str, M.MirFunc]:
	"""
	Lower a set of HIR function bodies through the lang2 pipeline and run throw checks.

	Args:
	  func_hirs: mapping of function name -> HIR block (body).
	  declared_can_throw: optional mapping of fn name -> bool; **test/prototype-only**.
	    In a real compiler this will be computed by the checker from signatures.
	  exc_env: optional exception environment (event name -> code) passed to HIRToMIR.

	Returns:
	  dict of function name -> lowered MIR function.

	Raises:
	  RuntimeError if throw invariants are violated (until real diagnostics are wired).
	"""
	# Stage “checker”: obtain declared_can_throw from the checker stub so the
	# driver path mirrors the real compiler layering once a proper checker exists.
	checker = Checker(declared_can_throw or {})
	checked = checker.check(func_hirs.keys())
	declared = {name: info.declared_can_throw for name, info in checked.fn_infos.items()}
	mir_funcs: Dict[str, M.MirFunc] = {}

	for name, hir_block in func_hirs.items():
		# Normalize sugar (HTryResult) before lowering.
		hir_norm = normalize_hir(hir_block)
		builder = MirBuilder(name=name)
		HIRToMIR(builder, exc_env=exc_env).lower_block(hir_norm)
		mir_funcs[name] = builder.func

	# Stage3: summaries
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc=exc_env or {})

	# Stage4: throw checks
	run_throw_checks(
		funcs=mir_funcs,
		summaries=summaries,
		declared_can_throw=declared,
		type_env=checked.type_env,
		ssa_funcs=None,
		diagnostics=checked.diagnostics,
	)

	return mir_funcs


__all__ = ["compile_stubbed_funcs"]
