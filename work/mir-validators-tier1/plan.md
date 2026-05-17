# MIR validators — Tier 1 (parked plan)

**Status:** parked 2026-05-17.  Higher-priority app-team bug
report came in immediately after this plan was approved.
Resume when that's resolved.

**Origin:** architecture conversation 2026-05-17 between user and
assistant.  The recurring failure mode being addressed: "MIR
shape is locally plausible but only valid under a specific
context.  The rule lives in comments or pass-local memory
instead of being represented as an executable contract."

**Approved direction:** small, targeted validators consuming
facts the existing infrastructure (`mir_validate.py`,
`ownership_ledger.py`, `cleanup_authoring.py`) ALREADY produces.
Reject the unified node-effect-declaration framework.  Reject
duplicating ownership-ledger node-effects into per-node metadata
(two sources of truth).

The `ledger_cache.py` precedent is the template: discover the
invariant from real bugs, turn it into runtime assertion +
static audit + named exceptions.  Incremental, not predictive.

---

## Prioritized work order

### 1. `validate_mir_void_return_shape` — FIRST

- **Catches:** the 2026-05-17 Void-callback-lambda bug class
  (`Return(value=synth_void)` on a nothrow Void fn).  Already
  asserted by LLVM lowering today; this validator fires the
  diagnostic at the right place (immediately after MIR build)
  instead of deep in codegen.
- **Where:** add to `validate_mir_basic_hygiene` in
  `lang/driftc/mir_validate.py` (existing per-fn shape
  validator).
- **Size:** ~10 LOC.
- **Confidence:** high.  Tiny, well-bounded, mirrors an existing
  assertion.
- **Feasibility check (~30 min) before starting:** confirm
  `validate_mir_basic_hygiene` runs BEFORE the LLVM lowering
  assert in driver pass-ordering at
  `lang/driftc/driftc.py::compile_stubbed_funcs`.  If it
  doesn't, the diagnostic-delta is zero and the validator is
  pointless until pass order is adjusted.
- **Rule:** for each `M.Return(value)` in a fn whose declared
  return type is `Void` AND that is NOT `declared_can_throw`:
  `value` must be `None`.  Can-throw Void fns legitimately
  return `Ok(Void)` carrier values; the rule excludes them.

### 2. `validate_mir_match_arm_binder_uniqueness` — SECOND

- **Catches:** the 2026-05-17 shared-binder bug class
  (synthesizer bypasses `__match_binder_<N>_<orig>`
  localization, two arms produce same binder name → MIR has
  same local name written from two arms but read via
  binding-id-suffixed canonical names).
- **Where:** add to `mir_validate.py` as a new
  `validate_mir_match_arm_binder_uniqueness` validator.
- **Size:** ~30 LOC.
- **Confidence:** medium.  Tiny code but needs a layering
  decision (see feasibility check).
- **Feasibility check (~30 min) before starting:** confirm
  whether the type-checker or
  `stage1/ast_to_hir.py::lower_match_expr` is the canonical
  enforcer of binder uniqueness.  If yes, the MIR validator is
  a stage2 BACKSTOP catching synth-pass bypasses (its actual
  scope today: `const_share_synth`); the primary check stays
  in checker/synth output validation.  If no, the MIR
  validator IS the primary check and the bug class is broader
  than today's incident.
- **Rule:** for each function's MIR, walk all `M.StoreLocal`
  and `M.LoadLocal` references.  Locals named after a
  match-arm binder (heuristic: prefix `__match_binder_*` OR
  any local that appears as `arm.binders[i]` in source
  MatchArm metadata if available) must be pairwise distinct
  across arms.  Failure → user-facing diagnostic naming the
  colliding binder + the two arms.

### 3. `cleanup_coverage_validator` — THIRD

- **Catches:** the 2026-05-16 `__array_get/pop_res`
  over-register class — slot is BOTH cleanup-registered AND
  its value transferred out via `LoadLocal`-as-return.  Also
  covers the four cleanup-class bugs from earlier history
  (`__borrow_tmp`, `__exc_params_view_*`, `__cap_move_*`).
- **Where:** new module
  `lang/driftc/stage2/cleanup_coverage_validator.py`, runs
  after `cleanup_authoring`.  Consumes the same
  `LiveStateMap` that `cleanup_authoring` consumes — no new
  facts needed.
- **Size:** ~150 LOC.
- **Confidence:** medium-high on value, medium on
  implementation difficulty.  Most subtle of the three.
- **Feasibility check (~30 min) before starting — load-bearing:**
  read `ownership_ledger._apply` and confirm that
  `LoadLocal`-as-return-source is distinguishable from
  `LoadLocal`-into-another-local.  If the ledger doesn't track
  use-context (the SSA value's downstream destination), the
  validator needs additional state and the LOC estimate roughly
  doubles.  DO NOT start until this check passes.
- **Rule (sketch):** for each `M.Return(value)` with non-None
  value, trace back to the producing instruction (likely
  `LoadLocal` or `MoveOut`).  If the source slot is a
  `CleanupHook` candidate active at this program point per
  `verdict_at`: the slot must NOT have its content double-
  released.  Allowed shapes:
    - source instruction is `MoveOut` (lattice marks
      MOVED_OUT, drop suppressed at cleanup point — safe)
    - source instruction is `LoadLocal` AND the slot has a
      following `MoveOut`/`DropValue`/`StoreLocal` that
      explicitly transfers ownership (covered)
    - source instruction is `LoadLocal` AND the slot is NOT
      a cleanup candidate at this point — safe (the bug
      class's correct shape)
  Failure: cleanup AND `LoadLocal`-to-Return on the same
  slot with no intervening MoveOut → diagnostic.

---

## Deferred (not Tier 1, not now)

- **Codegen-call-resolution audit** (~100 LOC, post-LLVM-IR
  pass).  Would have caught the Arc<I> link error.  Different
  layer, more expected-external edge cases (extern C symbols,
  runtime archive, package consumer, etc.).  Defer until all
  current app-team blockers are done.
- **Per-node effect declarations.**  REJECTED per user
  decision: "The ownership ledger already centralizes node
  effects; duplicating that into node metadata risks two
  sources of truth."  Don't pursue.
- **Borrow-chain place-vs-value validator.**  The 2026-05-15
  bug cluster (`&place` on HField copied leaf, chained borrow
  rejected as copy, method-receiver autoborrow soundness)
  lives in the checker, well before MIR.  Out of scope for
  MIR validators.
- **Lifted audit markers (typed enums vs string literals).**
  Worth considering, but only the next time we add a new
  audit suite.  Standalone refactor with no behavior change
  has low ROI right now.

---

## Acceptance criteria (when work resumes)

For each Tier 1 validator:

1. Feasibility check passes (see per-validator section above).
2. Validator added with no regressions in
   `lang/tests/driver/` or full e2e cluster.
3. A regression test demonstrates the validator catches the
   originating bug class.  Mirrors the
   `test_lambda_void_callback_throw_check.py` /
   `test_const_share_synth_shared_binder_name.py` pattern:
   pre-fix shape verified to trip the validator's diagnostic;
   post-fix is clean.
4. If the validator finds existing pre-existing violations in
   stdlib or compiler self-build (`-M stdlib`), those become
   their own bug reports and the validator is gated behind a
   `DRIFT_STRICT_MIR_VALIDATE=1` env var until cleaned up.
   Don't ship a validator that fails on the current main.

Ship validators incrementally — one per commit, with its own
version bump and history entry.  This is hygiene work; don't
let it stack into one risky landing.

---

## Open questions for the resume

- Should the three validators share telemetry / diagnostic
  shape (e.g., common `[drift:mir_validate]` stderr line
  prefix the way `cleanup_authoring` uses
  `[drift:ownership_ledger]`)?  Probably yes — but defer the
  decision until the first one ships and we see what shape
  feels natural.
- Should `validate_mir_match_arm_binder_uniqueness` report
  the binder source spans (which `match` statement, which
  arm)?  If yes, walking back to source spans through MIR
  is non-trivial; might justify either staying in checker
  (where spans are cheap) or accepting "module:fn:binder
  name" diagnostic shape only.

---

## References

- `lang/driftc/stage2/ledger_cache.py` — the template for
  "discovered invariant → runtime+static enforcement +
  named exceptions" pattern this work follows.
- `lang/driftc/mir_validate.py` — host for tier 1 validators
  #1 and #2.
- `lang/driftc/stage2/cleanup_authoring.py` — runs immediately
  before the proposed `cleanup_coverage_validator`.
- `lang/driftc/stage2/ownership_ledger.py` — `LiveStateMap`,
  `verdict_at`, the fact layer all three validators consume.
- `docs/history.md` 2026-05-17 entries (Void lambda, shared
  binder, Arc<I> link) — the recent bugs the framework
  addresses.
- `docs/refactor_triggers.md` — the registry; consider
  whether to file a "MIR cross-instruction coverage
  validators" trigger if the pattern repeats further before
  we get to Tier 1.
