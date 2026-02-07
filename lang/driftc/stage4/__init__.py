# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 4 package: MIR → SSA skeleton.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis) → stage4 (SSA) → LLVM/obj

Public API:
  - MirToSSA: entry point to convert MIR to SSA form
  - SsaFunc: wrapper for SSA-ified MirFunc
  - DominatorAnalysis / DominanceFrontierAnalysis: CFG utilities for SSA
  - throw_checks: helpers to consume throw summaries and enforce invariants
"""

from .ssa import MirToSSA, SsaFunc
from .dom import (
	DominatorAnalysis,
	DominatorInfo,
	DominanceFrontierAnalysis,
	DominanceFrontierInfo,
)
from .throw_checks import (
	FuncThrowInfo,
	build_func_throw_info,
	enforce_can_throw_invariants,
	enforce_can_throw_signature_shape,
	enforce_return_shape_for_can_throw,
	enforce_fnresult_returns_for_can_throw,
	run_throw_checks,
	enforce_fnresult_returns_typeaware,
)

__all__ = [
	"MirToSSA",
	"SsaFunc",
	"DominatorAnalysis",
	"DominatorInfo",
	"DominanceFrontierAnalysis",
	"DominanceFrontierInfo",
	"FuncThrowInfo",
	"build_func_throw_info",
	"enforce_can_throw_invariants",
	"enforce_can_throw_signature_shape",
	"enforce_return_shape_for_can_throw",
	"enforce_fnresult_returns_for_can_throw",
	"run_throw_checks",
	"enforce_fnresult_returns_typeaware",
]
