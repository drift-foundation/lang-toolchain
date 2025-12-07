# Drift Compiler Architecture (lang2/)
The current compiler pipeline is intentionally split into clear stages so each layer stays small and testable. This document is a guided tour for senior engineers who want a high-level map plus pointers to the hairy bits: error propagation, type safety, and where the invariants live.

## Acronyms
- **AST**: Abstract Syntax Tree (stage0)
- **DF**: Dominance Frontier
- **CFG**: Control-Flow Graph
- **DV**: DiagnosticValue (error payload)
- **FnResult**: Result-like return type `<T, Error>` used for can-throw functions
- **HIR**: High-level IR (sugar-free, stage1)
- **IR**: Intermediate Representation (generic term for HIR/MIR/SSA)
- **MIR**: Mid-level IR (explicit ops/CFG, stage2)
- **SSA**: Static Single Assignment (stage4)

## Big-picture pipeline
At a high level the compiler is a conveyor belt. Each stage owns one concern and hands a simpler, more explicit representation to the next.

```
Surface AST (stage0) ──> HIR (stage1) ──> MIR (stage2) ──> SSA (stage4) ──> LLVM/obj
                           ^ pre-analysis (stage3) feeds invariants into stage4 ^
```

**Stage0 (AST)** is just parsed syntax. No sugar is removed and the tree mirrors the source closely.

**Stage1 (HIR)** removes all surface sugar but stays purely syntactic. Receiver placeholders and method sugar become explicit `HMethodCall`s, indexing/field sugar become `HIndex`/`HField`, and diagnostic/exception constructors become `HDVInit`. Control-flow sugar like `while`, `for`, ternary, and try/throw are expressed in terms of explicit `HLoop`, `HIf`, `HTry`, and `HThrow`. A try uses a list of `HCatchArm` so multiple catch arms and catch-alls can be represented without semantics baked in.

**Stage2 (MIR)** is the explicit, semantic-free IR of operations and blocks. It has ops for loads/stores, calls/method calls, `ConstructDV`, `ConstructError`, `ConstructResultOk/Err`, `ErrorEvent` (project event code from an Error), `AssignSSA` (SSA helper), `Phi`, etc., and only structured terminators (`Goto`, `IfTerminator`, `Return`). HIR→MIR is a visitor (no `isinstance` ladders) and rejects malformed catch arms (multiple catch-all, catch-all not last). Ternary lowers to a diamond CFG storing into a hidden local. `throw` builds an `Error` (event code from `exc_env` when known, else 0); inside a try it routes to the current try’s dispatch, otherwise wraps in `FnResult.Err` and returns. `try` lowers to explicit blocks: dispatch uses `ErrorEvent` + per-arm constants; catch blocks bind the error (binder optional) and emit another `ErrorEvent`. Unmatched errors unwind to the nearest outer try via the try stack and only return `FnResult.Err` when no outer try exists. Unknown/duplicate catch arms are rejected in lowering. Tests under `lang2/stage2/tests` cover straight-line, ternary, multi-arm try (unmatched, nested unwind, binder/no-binder, unknown events, catch-all), and legacy scenarios.

**Stage3 (pre-analysis + throw summaries)** records facts about MIR: address-taken locals; may-fail sites (calls/method calls/ConstructDV/ConstructError); throw sites and exception DV names (decoded from `ConstructError` event codes via `code_to_exc` + `ConstInt` tracking). `ThrowSummaryBuilder` aggregates per-function: `constructs_error`, `exception_types`, `may_fail_sites`.

**Stage4 (SSA/dom + throw checks)** provides CFG utilities and enforces invariants. Dominators + dominance frontiers support SSA rewrite (single-block and simple acyclic CFGs) with `AssignSSA`. Throw checks (`run_throw_checks`) consume `ThrowSummary` + checker intent (`declared_can_throw`) and enforce: (1) no `ConstructError` in non–can-throw fns, (2) can-throw fns must not have bare `return;`, (3) can-throw fns must return a value produced by `ConstructResultOk/Err` (structural stopgap; type-aware check is stubbed as `enforce_fnresult_returns_typeaware`). Tests under `lang2/stage4/tests` cover dom/DF, SSA skeleton, throw checks, and integration with stage2/3 summaries.

## Error propagation & exceptions
* Exceptions carry an event code (from exception metadata) + DV payload.
* `ErrorEvent` is the MIR op that projects an event code from an Error; dispatch blocks compare this against per-arm constants.
* Try/throw semantics:
  * Throw inside innermost try → error stored in that try’s hidden local → jump to its dispatch.
  * Dispatch walks arms in order, compares codes, jumps to catch or catch-all.
  * Unmatched errors: unwind to the nearest outer try (same function) via the try stack; only return `FnResult.Err` when no outer try exists.
* Catch-arm validation:
  * Lowering rejects malformed shapes (multiple catch-all, catch-all not last).
  * Checker-side helpers (`lang2/checker/catch_arms.py`) currently enforce duplicate/unknown event arms; the driver collects HIR catch arms after normalization and feeds them into the checker along with an exception catalog. Future work is to attach real spans and a richer catalog from the front-end.
* Unknown event names fall back to code `0`; throw/catch of the same unknown name still match, but the checker should eventually forbid unknown events.

## Invariants & type safety (current vs planned)
* Enforced now:
  * Non–can-throw fns must not construct errors.
  * Can-throw fns must not have bare `return;`.
  * FnResult returns are enforced in two modes:
    * **Structural (untyped/unit tests):** return value must be produced by `ConstructResultOk/Err` in the same function; aliasing/forwarding is rejected.
* **Type-aware (typed paths):** when SSA + `TypeEnv` are provided, only the returned SSA value’s type matters (`is_fnresult`/`fnresult_parts`); structural guard is skipped. See `lang2/core/types_protocol.py`, `lang2/core/types_env_impl.py`, and `lang2/stage4/tests/test_throw_checks_typeaware*.py`.
* Planned/remaining work:
  * Make typed paths the default once a real `TypeEnv` is available everywhere (structural guard becomes a fallback/debug check).
  * Checker-provided `declared_can_throw` map (derived from signatures / throws clauses) fed into `run_throw_checks` in the real driver. Tests use `FnSignature` + checker helpers (see `lang2/test_support`) rather than hard-coded bool maps.
  * Checker-side catch-arm validation is present in the stub; future work is to attach real spans and a front-end exception catalog.

## Known challenges / design choices
* **Layering over monoliths:** Each stage has a clear public API; policy (throw checks) sits in stage4, not tangled with lowering.
* **Exception event codes:** Forward-mapped via `exc_env` during throw/catch lowering; reverse-mapped in pre-analysis via `ConstInt` + `code_to_exc` to recover DV names.
* **Unwinding vs return Err:** Current semantics keep unwinding within a function using the try stack; only top-level returns Err. Cross-function unwinding is a future design decision.
* **Unknown events:** Mapped to code 0; acceptable for now but slated for checker rejection.
* **Result-driven try sugar:** To be introduced as a desugaring pattern (`let tmp = fallible(); if tmp.is_err() { throw tmp.unwrap_err(); } let x = tmp.unwrap();`) in a checker/HIR rewrite when `?`/try-expression sugar arrives.
* **Testing guidance:** Integration tests that need checker output/diagnostics should prefer the driver harness (`compile_stubbed_funcs(..., return_checked=True)`) and assert on the `CheckedProgram` (fn_infos, diagnostics, exception catalog). Direct `run_throw_checks(...)` calls are reserved for stage4 unit tests.
* **TypeEnv shims:** `SimpleTypeEnv` (manual tagging) and `InferredTypeEnv/build_type_env_from_ssa` (infer FnResult types from SSA + signatures) exist for tests; a real checker/type system will eventually provide the canonical `TypeEnv`.

## How to run the staged tests
* Default Just target: `just lang2-test` (runs `pytest` on stage2 + stage4 suites).
* Stage-specific: `pytest lang2/stage2/tests` or `pytest lang2/stage4/tests`.

## Where to look next
* `lang2/stage1/ast_to_hir.py` — sugar removal, HIR nodes.
* `lang2/stage2/hir_to_mir.py` — MIR lowering, try/throw dispatch.
* `lang2/stage3/pre_analysis.py` + `throw_summary.py` — MIR facts/aggregation.
* `lang2/stage4/throw_checks.py` — can-throw invariants.
* `work/split-mir-ssa-visitor/work-progress.md` — running progress doc + future tasks.
