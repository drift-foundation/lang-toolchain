# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 3 package: MIR pre-analyses (address-taken, may-fail, etc.).

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis) → SSA → LLVM/obj

Public API:
  - MirPreAnalysis: run analyses over a MirFunc
  - MirAnalysisResult: holds computed flags/sets
"""

from .pre_analysis import MirPreAnalysis, MirAnalysisResult

__all__ = ["MirPreAnalysis", "MirAnalysisResult"]
