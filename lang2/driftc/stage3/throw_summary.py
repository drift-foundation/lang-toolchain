# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 3 helper: aggregate per-function throw summaries from MIR pre-analysis.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis) → SSA → LLVM/obj

We keep pre-analysis facts (address_taken, may_fail, ConstructError sites,
exception types) separate from the MIR itself. This helper consumes the
MirAnalysisResult for each function and produces a tiny summary that later
stages (checker/SSA/codegen) can consult for can-throw invariants without
re-running analyses or poking into MIR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set

from .pre_analysis import MirAnalysisResult, MirPreAnalysis
from lang2.driftc.stage2 import MirFunc


@dataclass
class ThrowSummary:
	"""Aggregated throw-related facts for a single function."""

	constructs_error: bool  # does this function contain any ConstructError at all?
	exception_types: Set[str]  # DV names inferred from event codes via code_to_exc
	may_fail_sites: Set[tuple[str, int]]  # raw copy of MirAnalysisResult.may_fail


class ThrowSummaryBuilder:
	"""
	Aggregate throw summaries from MIR pre-analysis.

	Entry point:
	  build(funcs: Dict[str, MirFunc],
	        code_to_exc: Dict[int, str] | None = None) -> Dict[str, ThrowSummary]

	This is a thin wrapper so later stages can consume throw facts without
	re-running pre-analysis on every pass.
	"""

	def build(self, funcs: Dict[str, MirFunc], code_to_exc: Dict[int, str] | None = None) -> Dict[str, ThrowSummary]:
		summaries: Dict[str, ThrowSummary] = {}
		analyzer = MirPreAnalysis(code_to_exc=code_to_exc)

		for fname, func in funcs.items():
			res: MirAnalysisResult = analyzer.analyze(func)
			summaries[fname] = ThrowSummary(
				constructs_error=bool(res.construct_error_sites),
				exception_types=set(res.exception_types),
				may_fail_sites=set(res.may_fail),
			)

		return summaries
