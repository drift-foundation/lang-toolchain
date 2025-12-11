# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
lang2 compiler package (`driftc`).

Exports the CLI entrypoints and key helpers so existing imports of
`lang2.driftc` continue to work after reorganizing compiler modules
under this package.
"""

from .driftc import compile_stubbed_funcs, compile_to_llvm_ir_for_tests, main

__all__ = ["compile_stubbed_funcs", "compile_to_llvm_ir_for_tests", "main"]
