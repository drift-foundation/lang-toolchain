# Stage 1 tests (AST → HIR)

Place stage-1 (HIR) specific test sources/fixtures here. Any runtime artifacts
should be written under `build/tests/stage1/` to mirror the stage layout.

Test coverage overview:

* `ast_to_hir_*` — basic expressions/statements, calls, DV ctor, ternary, loops,
  and try/throw lowering to `HTry`/`HCatchArm`/`HThrow`.
