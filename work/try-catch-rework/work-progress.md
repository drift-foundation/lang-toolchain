# Try/Catch Rework — Feature Progress

This document tracks **all changes** related to removing `try/else`, introducing the new `try/catch` expression form, and aligning SSA + MIR + backend semantics with the updated error model.
It will also define the **next phases** so this feature lands without semantic or backend drift.

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
* Expression-form lowering now rejects non-call attempts (honest semantics).
* Statement try/catch supports multiple catch-all clauses; event-specific/multi-catch still TODO.
* SSA verifier now supports `err_dest` on call terminators.
* SSA codegen trusts MIR’s `can_error` flags and asserts invariants; no backend rescanning.
* Throw lowering uses an explicit placeholder pair (no backend undef issues).

### Tests

* New e2e negative tests:

  * `call_plain_can_error`
  * `call_with_edges_non_can_error`
* All previous try/else usage migrated to try/catch.
* All SSA + e2e tests passing.

### Spec & docs

* Grammar dropped `try/else`.
* DMIR grammar updated.
* Try/catch expression added to language spec.
* Work logged here for traceability.

---

# 2. Current state of SSA lowering

## Expression form

Current `_lower_try_catch_expr` now:

* Snapshots live user vars.
* Builds:

  * try block
  * error block (receives the `Error` value + locals)
  * join block (result + locals)
* Emits a `Call` terminator with:

  * `dest` (value)
  * `err_dest` (Error)
  * `normal` / `error` edges
* Wires catch binder to error value.
* Only call attempts allowed (others → `LoweringError`).

**Limitations**

* No event-specific catch arms (catch-all only).
* No multi-catch.

## Statement form

`lower_try_stmt`:

* Requires final statement to be a `Call(Name)` expression.
* Emits call-with-edges with real error threading.
* Only the first catch is supported.
* No event discrimination or multi-catch.

---

# 3. Backend & Verifier

## SSAVerifierV2

* Recognizes `err_dest`.
* Enforces terminator semantics.
* Ensures def-before-use and dominance for both value+error results.
* Validates can-error invariants.

## SSA Codegen

* No longer rescans MIR.
* Uses MIR’s `can_error` to assert:

  * No throws in non-can-error fns.
  * No call-with-edges to non-can-error callees.
  * No plain calls to can-error funcs.
* Splits `{T, Error*}` pairs properly.
* Branches based on `err != null`.

All aligned.

---

# 4. Next Phases (New)

These phases describe the **remaining work** to fully finish the try/catch redesign.

---

## Phase 1 — Full try/catch semantics in SSA

*(multi-catch + event matching + binder correctness)*

### Goals

* Support **multiple catch clauses** in SSA.
* Support **event-specific catches**:

  * `catch (MyEvent e) { ... }`
  * `catch(MyEvent)` with no binder
* Support catch-all fallback.

### Required steps

1. Extend statement-form lowering:

   * Create one SSA block per catch clause **or**
   * Create a single error-dispatch block with a decision tree.
2. Add event discrimination logic:

   * Compare event string/pointer/value against catch arm.
3. Wire binder correctly for each arm.
4. Update SSA verifier tests.
5. Extend interpreter tests to match the new SSA semantics.

### DONE (implemented)

- SSA stmt try/catch now lowers all catch clauses, threads `Error` value and binder, and allows multiple catch-all blocks.
- Expression try/catch threads `Error` and binder.
- Error edges carry `err_dest`; verifier updated.

### Remaining

- Event-specific catches still not supported in SSA lowering; currently raise `LoweringError`.
- Error dispatch always routes to the first catch-all (no event discrimination yet).

### Tests added

- Positive: multi catch-all lowering (`try_multi_catch` e2e), expr try/catch with binder (`try_expr_binder` e2e).
- Negative: event-specific catches rejected in stmt/expr forms (`try_event_catch_stmt`, `try_event_catch_expr` e2e).

---

## Phase 2 — Broaden “attempt” surface for expr try/catch

*(beyond direct calls)*

### Goals

* Allow richer forms of attempts, not only direct `Call(Name)`:

  * Method calls
  * Possibly arbitrary expressions (depending on scope)
* Ensure semantics exactly match interpreter behavior.

### Required steps

1. Pick a strategy:

   * **Option A (safe & narrow):** Allow only call-like constructs (`foo()`, `obj.method()`)
   * **Option B (full):** Introduce a dedicated SSA block to run the attempt and detect errors anywhere within it.
2. Update lowering rules for `_lower_try_catch_expr`.
3. Add negative tests for illegal attempt forms.
4. Update language spec to reflect supported attempts.

---

## Phase 3 — SSA becomes the primary e2e backend

*(proof that error-edge semantics hold under real execution)*

### Goals

* Begin running selected e2e programs through SSA-only backend.
* Validate end-to-end correctness for:

  * try/catch
  * throw
  * call-with-edges
  * error propagation

### Required steps

1. Identify a small set of test programs (e.g., `throw_try`, `try_catch`, `array_string`).
2. Add a build target: `just test-e2e-ssa`.
3. Use SSA backend to emit actual binaries; execute them.
4. Fix any backend/SSA discrepancies found.
5. Gradually expand coverage.

---

# 5. Final Cleanup Targets

These are reserved for after the 3 phases are complete.

* Remove duplicate try-lowering logic in `lower_to_mir.py`.
* Update all spec examples to remove `try_else` and align with new SSA shape.
* Consider a general “error dispatch” MIR op if we want multi-catch to be first-class.
* Audit lifetime and scope rules for catch binders.
* Ensure DMIR → SSA lowering path is unified, not forked.

---

# 6. Status Summary

| Area                               | Status                  |
| ---------------------------------- | ----------------------- |
| Parser/AST                         | ✓ done                  |
| Interpreter                        | ✓ done                  |
| Checker                            | ✓ done                  |
| SSA expression T/C                 | ✓ done (call-only)      |
| SSA stmt T/C                       | ✓ single catch-all only |
| Error-edge threading               | ✓ done                  |
| SSA verifier                       | ✓ updated               |
| Backend                            | ✓ invariant-driven      |
| Multi-catch                        | ✗ Phase 1               |
| Event-matching                     | ✗ Phase 1               |
| Full attempt generalization        | ✗ Phase 2               |
| SSA-driven e2e                     | ✗ Phase 3               |
| Cleanup (docs, duplicate lowering) | pending                 |

---

If you want this exported as a ZIP or want to start with **Phase 1 implementation**, tell me and we’ll begin.
