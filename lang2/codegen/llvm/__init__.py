# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
LLVM codegen entrypoint for lang2.

This package will lower MIR/SSA to LLVM IR following the v1 ABI defined in
`docs/design/drift-lang-abi.md`. The initial scope is intentionally small:
scalars (`Int`, `Bool`), `Error`, and `FnResult<Int, Error>` for can-throw
functions. Additional types and calling conventions will be added
incrementally as the backend grows.
"""

from .llvm_codegen import LlvmModuleBuilder, lower_ssa_func_to_llvm

__all__ = ["LlvmModuleBuilder", "lower_ssa_func_to_llvm"]
