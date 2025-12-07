"""
lang2.core: shared core types/diagnostics/type-env shims used across stages.

Modules:
  - diagnostics: minimal Diagnostic placeholder
  - types_protocol: TypeEnv protocol (type-centric API)
  - types_core: TypeId/TypeTable primitives
  - types_env_impl: Simple/Inferred TypeEnv helpers
"""

__all__ = [
    "diagnostics",
    "types_protocol",
    "types_core",
    "types_env_impl",
]
