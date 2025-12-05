# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 4 package: MIR → SSA skeleton.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis) → stage4 (SSA) → LLVM/obj

Public API:
  - MirToSSA: entry point to convert MIR to SSA form
  - SsaFunc: wrapper for SSA-ified MirFunc
"""

from .ssa import MirToSSA, SsaFunc

__all__ = ["MirToSSA", "SsaFunc"]
