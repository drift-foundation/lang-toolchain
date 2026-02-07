# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
lang package: staged compiler refactor playground.

Stages:
  stage0: AST definitions (local copy for the refactor)
  stage1: HIR and AST→HIR lowering
  stage2: MIR and HIR→MIR lowering
"""

__all__ = ["stage0", "stage1", "stage2"]
