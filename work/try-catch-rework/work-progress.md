# Try/Catch Rework — Feature Progress

This document tracks **all changes** related to removing `try/else`, introducing the new `try/catch` expression form, and aligning SSA + MIR + backend semantics with the updated error model.
It also defines the **next phases** so this feature lands without semantic or backend drift.
:contentReference[oaicite:0]{index=0}

---

# 1. Completed in the current pass

### Surface syntax & AST

* Removed `try/else` entirely from grammar and AST.
* Added `TryCatchExpr` with catch-arm variants.
* Parser updated to support expression-form try/catch.
* Legacy `_lower_try_expr` code removed.

### Checker / interpreter / linter

* Statement and expression try/catch checking unified.
* Catch blocks must yield a value of the same type as the attempt.
* Binder is typed as `Error`.
* Interpreter updated to support expression-form try/catch.
* Linter updated accordingly.

### SSA lowering

* SSA lowering now **threads the actual `Error` value** on error edges.
* Both expression and statement forms carry the error into the catch blocks.
* Catch binders are wired to the SSA error parameter.
* Expression-form lowering:
  * Only supports **call attempts** (honest semantics) with simple `Name` / `Name.Attr` callees.
  * Supports multiple catch arms (event-specific + catch-all) with event dispatch via `ErrorEvent`; still call-only.
* Statement try/catch:
  * Requires the final statement of the try body to be a `Call(...)` expression (name or name.attr).
  * Supports **multiple catch clauses** (event-specific + catch-all); each lowered to its own SSA block with `Error` + locals threaded.
  * Error dispatch goes through a dedicated dispatch block that reads the error event/code (`ErrorEvent`) and picks the matching event catch in source order, then falls back to the first catch-all (or rethrows if none).
* SSA verifier understands `err_dest` on call terminators.
* SSA codegen trusts MIR’s `can_error` flags and asserts invariants; no backend rescanning.
* Throw lowering uses an explicit placeholder pair (no backend undef issues).
* `ErrorEvent` projection is available for dispatch and is lowered via the dummy runtime helper `drift_error_get_code`.

### Tests

New/updated e2e coverage:

* Invariants / misuse:
  * `call_plain_can_error`
  * `call_with_edges_non_can_error`
* Positive try/catch SSA semantics:
  * `try_call`
  * `try_call_error`
  * `try_catch`
  * `try_multi_catch`  (multi catch-all statement form)
  * `try_event_catch_stmt` (event dispatch in stmt form)
  * `try_expr_binder`  (expr try/catch with binder)
  * `try_event_catch_expr` (expr event dispatch)
* Projection primitive:
  * `mir_ssa_error_event_test` (ErrorEvent → drift_error_get_code end-to-end)
* Negative (intentional limitations):
  * `try_event_catch_expr_noncall` (expr attempt must be a call)

All SSA tests (`mir_ssa_tests`, `ssa_check_smoke`, `ssa_programs_test`) and e2e runner cases are passing.

### Spec & docs

* Grammar dropped `try/else`.
* DMIR grammar updated.
* Try/catch expression added to language spec.
* This work log tracks phase-by-phase changes.
* Docs now explicitly state:
  * Statement and expression try/catch support event-specific dispatch via the projected event/code.
  * Expression-form try/catch is still call-only; non-call attempts are rejected.

---

# 2. Current state of SSA lowering

## Expression form

Current `_lower_try_catch_expr`:

* Snapshots live user vars.
* Builds:
  * catch blocks (one per arm) with params `(Error, live locals...)`
  * dispatch block that reads `ErrorEvent` and selects matching event arms in order (fallback to catch-all or rethrow)
  * join block (result + locals)
* Emits a `Call` terminator with `normal` / `error` edges to the join/dispatch blocks.
* Wires catch binders to the `Error` parameter in each catch block.
* Only supports attempts that are calls with:
  * `Name` callee, e.g., `foo(x)`
  * `Name.Attr` callee, e.g., `obj.method(x)`
* Non-call attempts still yield `LoweringError` by design.

**Limitations**

* Attempts must remain call-shaped (Name / Name.Attr); broader attempts are deferred to Phase 2.
* At most one catch-all is allowed in the expression form.
* Runtime/SSA exposes an `ErrorEvent` projection (via `drift_error_get_code`) that returns the error’s event/code as an integer.

## Statement form

`lower_try_stmt`:

* Requires non-empty try body.
* Requires the final statement to be an `ExprStmt` wrapping a `Call(...)`:
  * Callee must be `Name` or `Name.Attr`.
* Lowers **all** catch clauses present in the source:
  * Each clause becomes a separate SSA block with params:
    * `Error` value
    * threaded live locals
  * Binder (if present) is bound to the `Error` param.
* Join block:
  * Threads live locals after the try/catch.
  * All fallthrough paths from catch blocks branch to this join with updated locals.
* Error dispatch:
  * Error edge now targets a **dispatch block**.
  * The dispatch block reads the event/code via `ErrorEvent`, compares against event-specific catch clauses in source order, and routes to the matching catch.
  * Falls back to the first catch-all if nothing matches; rethrows if there is no catch-all.
  * Event identifiers are currently encoded as deterministic integer codes assigned per exception definition and exposed via the dummy runtime’s `drift_error_get_code`.

---

# 3. Backend & Verifier

## SSAVerifierV2

* Recognizes `err_dest` on call terminators.
* Enforces terminator structure:
  * Only calls-with-edges may define both `dest` and `err_dest`.
* Ensures def-before-use and dominance for both value and error results.
* Validates `can_error` invariants based on MIR annotations.

## SSA Codegen

* No longer rescans MIR to infer can-error; it relies on MIR’s annotations and asserts invariants:
  * No `Throw` in non-`can_error` functions.
  * No call-with-edges to non-`can_error` callees.
  * No plain call to `can_error` callees.
* Splits `{T, Error*}` return pairs correctly.
* Branches based on `err != null` and threads locals through the CFG as set up by SSA lowering.

All aligned with the current SSA lowering shape.

---

# 4. Next Phases

These phases describe the remaining work to fully finish the try/catch redesign.

---

## Phase 1 — Full try/catch semantics in SSA

*(multi-catch + event matching + binder correctness)*

Status:

* **Phase 1a — Error event projection:** ✔ done. Added `ErrorEvent` MIR instruction, `_ssa_read_error_event` helper, SSA codegen lowering via `drift_error_get_code`, dummy runtime support, and SSA test coverage.
* **Phase 1b — Event dispatch:**
  * Statement form: ✔ done. Error edge now flows into a dispatch block that compares the projected event/code against event-specific catch clauses in order, with catch-all fallback (or rethrow if none). Event identifiers are deterministic integer codes assigned per exception definition.
  * Expression form: ✔ done. Call-only attempts dispatch through an expr-level dispatch block using `ErrorEvent`, support multiple catch arms (event-specific + catch-all), and rethrow if no arm matches.
  * Tests flipped to positive: `try_event_catch_stmt` and `try_event_catch_expr` now prove event dispatch and catch-all fallback; non-call attempts remain a compile error (`try_event_catch_expr_noncall`).

Remaining work:

* Broaden attempt surface for expressions beyond call shapes (Phase 2).
* Keep documenting that expressions remain call-only and allow a single catch-all.

---

## Phase 2 — Broaden “attempt” surface for expr try/catch

*(beyond direct calls)*

### Goals

* Potentially allow richer forms of attempts, not only direct call shapes, while keeping semantics aligned with the interpreter.

### Strategy (to be decided after Phase 1)

1. **Option A (safe & narrow)**  
   * Continue to support only call-like constructs (`foo()`, `obj.method()`) as attempts.  
   * Document this explicitly in the language spec as a current limitation.

2. **Option B (full)**  
   * Introduce a dedicated SSA “try body” block that runs the entire attempt expression and surfaces errors via a single call-with-edges or throw.
   * More work, more power, but higher complexity.

### Required steps

* Decide between Option A vs B.
* If broadening beyond calls:
  * Update `_lower_try_catch_expr` to build a dedicated try-body region.
  * Ensure that any throw in that region correctly routes to the catch arm.
* Add negative tests for illegal attempt forms.
* Update the language spec to reflect supported attempts.

---

## Phase 3 — SSA becomes the primary e2e backend

*(proof that error-edge semantics hold under real execution)*

### Goals

* Run a subset of e2e programs through the SSA backend only.
* Validate end-to-end behavior for:
  * try/catch
  * throw
  * call-with-edges
  * error propagation

### Required steps

1. Identify a seed set of test programs (e.g., `throw_try`, `try_catch`, `try_multi_catch`, `array_string`).
2. Use `tests/e2e_runner.py` with `--backend=ssa-llvm` and a Just target like `test-e2e-ssa-subset` to:
   * Lower via strict SSA.
   * Emit an object/binary via `ssa_codegen`.
   * Run the binary and compare output/exit code with `expected.json`.
3. Fix any backend/SSA discrepancies found.
4. Gradually expand coverage until SSA is trusted as the main codegen path.

### Progress

* `tests/e2e_runner.py` accepts `--backend` and case filters. :contentReference[oaicite:1]{index=1}  
* Justfile includes `test-e2e-ssa-subset` that runs `hello`, `throw_try`, `try_catch`, `try_call_error`, `try_event_catch_stmt`, `try_event_catch_expr` via SSA backend.
* Added `ErrorEvent` projection wired to `drift_error_get_code` and a dedicated SSA test to ensure it codegens end-to-end.

---

# 5. Final Cleanup Targets

These are reserved for after the 3 phases are complete.

* Remove duplicate try-lowering logic in `lower_to_mir.py`.
* Update all spec examples to remove `try_else` remnants and align with the new SSA shape.
* Consider a general “error dispatch” MIR op if we want multi-catch dispatch to be first-class.
* Audit lifetime and scope rules for catch binders (both stmt and expr forms).
* Ensure DMIR → SSA lowering path is unified, not forked.

---

# 6. Status Summary

| Area                               | Status                                              |
| ---------------------------------- | --------------------------------------------------- |
| Parser/AST                         | ✓ done                                              |
| Interpreter                        | ✓ done                                              |
| Checker                            | ✓ done                                              |
| SSA expression T/C                 | ✓ call-only (name/attr), multi-catch with event dispatch |
| SSA stmt T/C                       | ✓ multi-catch with event dispatch + catch-all fallback |
| Error-edge threading               | ✓ done                                              |
| SSA verifier                       | ✓ updated                                           |
| Backend                            | ✓ invariant-driven                                  |
| Phase 1a: Error event projection   | ✓ done (ErrorEvent + runtime + SSA test)           |
| Phase 1b: Event-based dispatch     | ✓ stmt+expr done (expr call-only)                   |
| Multi-catch                        | ✓ stmt/expr multi-catch with event dispatch         |
| Event-matching                     | ✓ stmt/expr via event codes                         |
| Full attempt generalization        | ✗ Phase 2                                           |
| SSA-driven e2e                     | ◔ subset wired (`hello`, `throw_try`)               |
| Cleanup (docs, duplicate lowering) | pending                                             |
