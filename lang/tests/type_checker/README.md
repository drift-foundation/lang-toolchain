# Type checker unit tests

These tests exercise the `TypeChecker` unit in isolation. They are **not**
Stage‑1 pipeline validation tests and do not assert on typed‑HIR artifacts
such as `NodeId`/`CallSig`/`CallTarget`.

When the Stage‑1 typed‑HIR handoff is implemented, add pipeline tests under
`lang/tests/stage1/` and keep these focused on local type‑checker behavior.
