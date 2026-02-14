# Compiler Hardening Work Progress

## Status

Planned only. No implementation started in this track.

This track is explicitly a compiler/runtime stabilization effort, not a stdlib feature effort.

---

## Why This Track Exists

Recent work repeatedly surfaced LANGUAGE_BUGs from ordinary stdlib usage:
- ownership/copy enforcement gaps (accepted by checker, broken by semantics)
- ref-return/escape-rule friction in practical patterns
- lowering/codegen mismatches after checker acceptance
- brittle behavior from large monolithic decision paths (`type_checker.py`, MIR lowering, LLVM emission)

The goal is to stop “fix one symptom, hit another” by enforcing clear phase contracts and invariant-driven testing.

---

## Scope (In)

1. Ownership and value-category correctness
- non-Copy extraction from borrowed base
- move/copy/read rules across field/index/projection chains
- by-value call argument legality and required move forms

2. Reference semantics correctness
- return-by-reference derivation and escape policy
- reference propagation through helper APIs and common wrappers
- compatibility between checker rules and real stdlib usage patterns

3. Checker ↔ MIR ↔ LLVM phase contract enforcement
- any checker-accepted program in supported subset must lower and codegen
- unsupported semantics must be rejected early in checker with deterministic diagnostics

4. Diagnostic quality for enforcement errors
- span attribution where currently `<source>:None`
- stable diagnostic categories for invariant violations

5. Regression hardening for known weak seams
- field projection and place classification
- method-call receiver ownership/autoborrow interactions
- match/try paths with ownership-sensitive payloads

---

## Scope (Out)

- New language features
- New stdlib APIs (except minimal test scaffolding)
- Broad refactors unrelated to invariant enforcement
- Performance tuning unless required to keep tests reliable

---

## Hard Freeze Policy During This Track

- No new feature surface in stdlib or language.
- Only bugfixes + enforcement + diagnostics + tests.
- If a new capability seems needed, pin it and defer.

---

## Guiding Principles

1. Regression-first always
- Add minimal failing test before fix.
- Confirm fail -> fix root cause -> confirm pass.

2. No semantic masking
- Do not patch stdlib/app code to avoid compiler defects.
- If workaround is requested, mark as temporary and keep bug open.

3. Phase-contract discipline
- Checker acceptance is a guarantee to MIR/codegen in supported subset.
- If MIR/LLVM cannot support a shape, checker must reject it explicitly.

4. Deterministic diagnostics
- Prefer explicit invariant error over downstream crashes/asserts.

5. Stage hand-off contracts are first-class deliverables
- Every phase boundary must have explicit accepted/rejected shape contracts.
- Handoff validation must run before entering the next phase.
- No “best effort” continuation when contract checks fail.

---

## Invariant Matrix (Source of Truth)

### A. Value Category and Ownership

A1. Non-Copy from borrowed base
- `&S` + `s.field` where field non-Copy and used as value: reject.

A2. Non-Copy from owned base
- owned local `s.field` by-value: allowed only via move semantics according to place rules.

A3. Copy from borrowed base
- `&S` + `s.field` where field Copy: allowed.

A4. Place projections
- field/index chains preserve correct value category and move requirements.

A5. By-value call arg rules
- non-Copy local by-value call requires legal move source and no borrowed overlap.

### B. Reference Semantics

B1. Ref return derivation
- returned reference must derive from permitted source according to rule.

B2. Escape diagnostics
- invalid ref-return should fail in checker with stable message + span.

B3. Helper indirection
- wrapper/helper patterns should not silently bypass or over-trigger escape rules.

### C. Phase Contracts

C1. Checker accepted => MIR lowerable (supported subset)
C2. MIR generated => LLVM emission succeeds (supported subset)
C3. Unsupported shape rejected in checker with deterministic diagnostics

### D. Diagnostics

D1. Ownership violations include clear action text (copy vs move).
D2. Diagnostics should carry file/line/column whenever possible.
D3. Remove `<source>:None` in these invariant paths.

---

## Work Plan

## Phase 1: Stage Hand-Off Contract Isolation (Top Priority)

Objectives
- Make checker->MIR and MIR->LLVM boundaries explicit, validated, and enforceable.
- Remove implicit assumptions currently scattered across downstream code.

Tasks
- Define contract schemas for:
  - checker output consumed by MIR lowering,
  - MIR output consumed by LLVM emission.
- Add boundary validators that run at phase entry:
  - reject unsupported/ambiguous shapes with deterministic diagnostics.
- Move acceptance policy upstream:
  - if MIR/LLVM cannot support a shape, checker must reject it by policy.
- Add negative tests that intentionally violate boundary contracts and verify clean rejection at the correct phase.
- Add positive “contract conformance” tests for representative real programs (including stdlib-heavy patterns).

Deliverables
- Documented boundary contract checklist in this file.
- Validator hooks enabled by default in test builds.
- Regression suite proving contract enforcement points.

Gate
- No checker-accepted program in hardening subset fails later due to unsupported shape mismatch.
- Boundary failures are diagnosable at the hand-off phase, not via downstream assertions/crashes.

## Phase 0: Baseline and Test Inventory

Objectives
- Catalog current failures/flakes tied to ownership/ref semantics.
- Build a focused test list for fast iteration.

Tasks
- Create compiler-hardening subset list (driver + e2e + targeted codegen unit).
- Classify each test by invariant bucket (A/B/C/D).
- Record known intermittent cases separately from deterministic failures.

Deliverables
- Test manifest in this file (or adjacent note).
- Stable “quick-hardening” command set.

Gate
- We can run all hardening subset tests repeatedly without harness ambiguity.

---

## Phase 2: Ownership Enforcement Correctness (Field/Projection First)

Objectives
- Centralize and stabilize non-Copy extraction checks for projection paths.

Tasks
- Audit type-checker field/projection paths for duplicate early-return logic.
- Ensure borrowed-subject + non-Copy by-value is consistently rejected.
- Ensure owned-subject paths still allow legal move semantics.
- Add paired positive/negative tests for nested projection chains.

Tests (minimum)
- Driver: non-Copy field from borrowed base rejected (new pinned regression).
- Driver/e2e: Copy field from borrowed base allowed.
- Driver/e2e: owned base extraction semantics remain correct.
- Existing regressions touching `HField`/place canonicalization.

Gate
- No `<source>:None` on these failures.
- All field/projection ownership tests pass in normal, ASAN, alloc-track modes.

---

## Phase 3: Ref-Return/Escape Rule Rationalization

Objectives
- Make ref-return behavior predictable and usable for real patterns.

Tasks
- Pin exact MVP escape-rule policy in docs/spec note.
- Ensure checker implementation matches pinned policy exactly.
- Add tests for helper/wrapper patterns (registry-derived refs, parameter-derived refs, invalid locals).
- Explicitly track logger helper ergonomics case:
  - desired shape: helper APIs like `app.logging.security(reg: &GlobalRegistry) -> &Logger`.
  - current status: blocked by MVP escape-rule enforcement shape; e2e uses inline registry fetch + `&state.security` workaround form.
  - hardening target: either allow this derivation form safely, or reject it with intentional, stable diagnostics and documented rule rationale.
- If policy is intentionally strict, ensure diagnostics guide users to valid pattern.

Tests (minimum)
- Valid ref return from parameter-derived chains.
- Invalid ref return from temporaries/locals.
- Registry/wrapper usage cases pinned to policy (accepted or rejected by design).
- Include `macro_log_app_logging_context` helper-return variant as a tracked policy test (or explicit rejection test if intentionally unsupported).

Gate
- No surprise accept/reject drift across equivalent forms.
- Diagnostics deterministic and actionable.

---

## Phase 4: Checker→MIR Contract Guards

Objectives
- Fail early in checker instead of MIR assertion gaps.

Tasks
- Add contract assertions/tests for shapes known to cause MIR invariant failures.
- Normalize by-value arg legality checks in one checker seam.
- Remove duplicated ad hoc checks in lowering where checker can own policy.

Tests (minimum)
- Existing MIR invariant regressions for non-Copy by-value call args.
- Callback/lambda capture by-value ownership tests.
- Match/try ownership-sensitive payload paths.

Gate
- No MIR invariant violation for checker-accepted programs in targeted subset.

---

## Phase 5: MIR→LLVM Contract Guards

Objectives
- Ensure LLVM emitter sees only canonical, supported shapes.

Tasks
- Add/strengthen pre-emission canonical checks where needed.
- Convert current crash-prone paths to explicit, tagged diagnostics where checker contract cannot yet guarantee shape.
- Add unit/e2e coverage for historically fragile lowering/canonicalization interactions.

Tests (minimum)
- Existing llvm_codegen e2e regressions related to ownership/projection/callback paths.
- Repeat-run flaky-hunter subset on critical tests.

Gate
- No clang/LLVM crashes from accepted hardening-subset programs.

---

## Phase 6: Diagnostic Hygiene

Objectives
- Remove anonymous/unattributed diagnostics in touched paths.

Tasks
- Thread span data through ownership/ref-return diagnostics.
- Standardize message wording for invariant classes.
- Add driver checks for key message fragments + phase attribution.

Gate
- No `<source>:None` for hardening-subset ownership/ref diagnostics.

---

## Phase 7: Stability Qualification

Objectives
- Prove hardening improvements are durable under stress/sanitizers.

Tasks
- Run hardening subset:
  - normal mode
  - `DRIFT_ASAN=1`
  - `DRIFT_ALLOC_TRACK=1`
  - combined mode where supported
- Repeat-run flaky subset N times (recommend 20+) on hottest cases.

Gate
- Stable pass rate in repeat runs.
- No new memory leaks from hardening changes.

---

## Phase 8: Compile-Path Efficiency and IR Bloat Reduction (Later Step)

Objectives
- Reduce avoidable compile/link wall-time without changing language semantics.
- Identify and trim IR/codegen expansion hotspots in generic/container-heavy paths.

Tasks
- Add compile-cost profiling for selected heavy e2e cases (IR size, IR line count, clang wall-time).
- Track “budget” regressions for heavy cases (e.g., `treemap_remove_cases`) separately from functional regressions.
- Audit redundant monomorphization/emission patterns that inflate IR.
- Reduce duplicate helper emission and repeated glue where safe.
- Keep runtime behavior/tests unchanged; this phase is efficiency-only.

Validation
- Heavy-case compile wall-time trends improve or remain bounded.
- No semantic regressions in affected cases.
- ASAN/alloc-track subsets remain clean after efficiency changes.

Notes
- This is intentionally sequenced after correctness hardening phases.
- Timeout overrides (e.g., for very large IR cases) are operational guardrails, not the final optimization strategy.

---

## Test Strategy

1. Driver tests
- Pin checker policy and diagnostics precisely.

2. E2E tests
- Validate end-to-end behavior with real lowering/codegen/runtime.

3. LLVM/codegen unit tests
- Protect canonical lowering assumptions.

4. Sanitizer/memory modes
- Catch ownership/lifetime regressions not visible in normal runs.

---

## Proposed Initial Test Buckets

Bucket A: Ownership/Copy from Borrowed Paths
- new: `test_noncopy_field_projection_from_borrow.py`
- existing move/borrow regressions (borrow-after-move, move-while-borrowed, copy-noncopy)

Bucket B: Macro/Wrapper Ref Usage
- `macro_log_app_logging_context`
- related macro logger diagnostics

Bucket C: MIR Invariant Sensitivity
- callback move-capture regressions
- by-value non-Copy call arg regressions

Bucket D: LLVM Fragility
- targeted llvm_codegen_e2e ownership paths

---

## Risks

1. Hidden coupling in monolithic checker code
- Mitigation: narrow-scope edits + immediate regression coverage.

2. Over-correction causing legitimate patterns to fail
- Mitigation: paired positive/negative tests for each rule.

3. Diagnostic churn breaking tests
- Mitigation: assert invariant meaning; keep message fragments stable.

4. Flaky concurrency/network e2e noise
- Mitigation: isolate hardening subset; classify unrelated flakes separately.

---

## Exit Criteria

This track is complete when:
- invariant matrix A/B/C/D is covered by deterministic tests,
- no known checker-accepted ownership/ref cases fail later in MIR/LLVM in the subset,
- diagnostics for these rules are attributable and stable,
- sanitizer/memory runs for subset are clean,
- no workaround-only open items remain for these invariant classes.

---

## Deferred Follow-Ups (Post-Hardening)

- Larger structural decomposition of `type_checker.py` into policy modules.
- Optional stronger ref-return model evolution (if MVP escape rule remains too restrictive).
- Broader compiler architecture cleanup beyond invariant-critical paths.

---

## Commands (Planned Quick Iteration)

Note: run commands are intentionally short and repeatable.

- Driver single file:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q <driver_test_file>`

- E2E single case:
  - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 --debug <case>`

- E2E subset:
  - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 <case1> <case2> ...`

- Sanitizer/memory variant:
  - `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 <subset>`

No implementation work starts on this track until explicit go-ahead.
