# B2+C combined implementation — PROGRESS (recovery checkpoint)

Branch/commit base: B1 recovery commit `7108b2bd` ("refactor(stage2):
move overwrite cleanup out of string_arc"). One 0.33.87 / ABI-21
endgame candidate; NO intermediate cert. Approved architecture:
`SLICE-B2-R6-ARCHITECTURAL-CHECKPOINT.md` §4 (frozen ledger-A decision
plan; Return authority = site-3 + R3/R4; Overwrite authority = nullsafe
+ site-4 as plan consumers in overwrite_cleanup; one coordinated
late-cleanup phase; ZERO transient rebuilds).

Update this file (log line + status flip) in the SAME turn as each
step — power-loss recovery point.

## Migration baselines (frozen, must reproduce +0)
- site-3 Return destructible drops: **1,088** (1,087 ERROR + 1 STRUCT
  `closures_share_capture_arc_generic::__lambda_main_0_0::app`)
- site-4 drop-before-overwrite: **14**
- null-safe destructible overwrite: **133,998** (0 marked-synthetic;
  disjoint from site-4/R7)
- R3 scope-exit String release: **68,562**
- materialized last-use release: **618,744**
- overwrite_release (R2, B1): **233,519**
- Every production aggregate counter **+0**; hard gates zero; ZERO
  additional ledger builds vs pre-B2+C.

## Ordered steps (each with a dev gate)

- [x] **S0 — pipeline/ledger map** (DONE 2026-07-21). Driver order
  (per-fn loop in compile_stubbed_funcs):
  1. match_cleanup_authoring + rebuild (rebuild_after_match_cleanup_authoring)
  2. drop_flags planning (driftc.py:8027)
  3. ledger_rebuild_post_drop_flags (8054, reason rebuild_after_drop_flags)
  4. cleanup_authoring (8086) — authors CleanupHook drops off that ledger
  5. **rebuild_after_cleanup_authoring (8126) = LEDGER A** — the exact
     ledger string_arc consumes; keyed to ORIGINAL block indices.
  6. observe gate (debug only, 8131)
  7. string_arc (8201) — consumes ledger A via maybe_fresh_ledger;
     site-3 (Return sweep) + site-4 (verdict_at@orig-idx + tripwire) +
     nullsafe + R3/R4/R5/R1 String; marks ledger dirty after.
  8. overwrite_cleanup (8242) — B1 pass, AFTER string_arc, NO ledger.
  9. ssa (8316), throw_checks (8340)...
  **Plan computation site**: immediately at/before the string_arc slot,
  reading LEDGER A (already built at 8126) + original MIR. **Zero-added-
  rebuild CONFIRMED**: the plan reuses the existing ledger A (no new
  build); emitters consume the frozen plan (no fresh verdict_at on
  shifted ledgers); string_arc still marks dirty; overwrite_cleanup
  already runs ledger-free after. No new build_and_attach_ledger is
  introduced by B2+C. Gate: map written, no code change. PASS.
- [x] **S1 — decision-plan module** (DONE 2026-07-21):
  `lang/driftc/stage2/cleanup_plan.py` — `CleanupPlan` + `Decision` +
  `AnchorCoord` (INSTR/TERM) + `PlanContractError` (subclasses
  AssertionError → driver boundary-containment surfaces it as clean
  `internal:` diag). Keyed on `id(obj)` in a plain object (NO dynamic
  MIR attributes). Consumption = object-identity + exactly-once-in-func
  + same-block + kind + semantic-field checks; `locate()` returns
  CURRENT index (may differ from orig); `check_relative_order()`;
  `assert_all_consumed()`. Teeth `lang/tests/stage2/test_cleanup_plan.py`
  **43/43** (grown across S1 closure + B2+C amendments; the initial
  container landed at 15/15 — see log): insertion-before-anchor stays valid + changed-index-OK
  (both sides), replace/duplicate/cross-block-move/disappear/field-drift/
  block-vanish/terminator-replace/reorder/dup-registration/unconsumed/
  double-consume/add-after-freeze all fail closed; two-sites-share-Return
  allowed (for S5 unified authority). Gate: PASS.
  Per-function plan object of **original-anchor records** — each carries (a) original block +
  original numerical index, (b) original object identity/reference,
  (c) expected kind + local + type/operand relationship, (d) consumed
  state. Keyed by original object identity + (block, original_index).
  NO dynamic MIR attributes.
  **Anchor-lifecycle contract (maintainer 2026-07-21T001349Z):**
  `(block, original_index)` is the immutable PROOF COORDINATE for the
  ledger-A query, validated at plan-BUILD. At CONSUMPTION do NOT require
  numerical-index equality (Return + earlier overwrite emissions shift
  current indices). Consumption validates: exact object present ONCE in
  the same block; semantic fields (kind/local/type/operand) unchanged;
  original-anchor relative order preserved. Changed current index =
  OK. FAIL CLOSED on: disappearance, duplication, cross-block movement,
  replacement, reordered anchors, wrong local/type/operand, unconsumed/
  orphan. Teeth BOTH sides: insertion-before-anchor does NOT invalidate;
  move/replace/duplicate DOES. Gate: unit tests + both-side teeth green.
  STOP if the lifecycle needs a rebuild or dynamic MIR metadata.
- [~] **S2 — plan computation (site-3 + site-4 + nullsafe)** from
  original MIR + ledger A. NON-EMITTING; mutates neither MIR nor ledger.
  DONE so far: frozen immutable payloads `cleanup_payloads.py`
  (Site3ReturnPayload w/ ordered Site3Drop tuple + local_count; Site4Payload
  w/ verdict + emit + frozen ty; NullsafePayload w/ frozen ty).
  EXTRACTION STRATEGY (avoid a "second approximation" — reuse string_arc's
  exact logic): pull the three decision computations out of
  `insert_string_arc` into a shared module `destructible_authority.py` as
  pure functions parameterized by (func, type_table, ledger, classification):
    (a) `classify_destructibles(func, type_table)` → (destructible_locals,
        nullsafe_destructible_locals) — extract lines ~241-316 predicates
        (`_is_destructible_tid`/`_is_error_tid`/`_is_nullsafe_drop`) +
        the two set comprehensions; reuse `classify_string_array_locals`
        for string_locals/array_locals inputs.
    (b) `plan_site4(func, ledger, type_table, destructible, nullsafe)` →
        per (block, orig_index) StoreLocal verdict via `verdict_at`
        (missing-ledger RuntimeError; PATH_DEPENDENT→RuntimeError at
        PLANNING); records MUST_DROP + MUST_NOT_DROP; emit=MUST_DROP.
    (c) `plan_nullsafe(func, destructible∩nullsafe)` → per StoreLocal;
        `synthetic_zero_back` present at this pre-string_arc surface = STOP.
    (d) `plan_site3(func, ledger, type_table, destructible, ...)` →
        per-Return: `initialized_at_return` definite-assignment fixpoint
        (assigned_in/store_defs), `skip_cleanup_locals` (moved/dropped +
        ledger MUST_NOT_DROP fold + `_is_flag_managed`), PATH_DEPENDENT
        zero-storage widening, `sorted(destructible_locals)` order.
    Then `insert_string_arc` CALLS (a)-(d) for its emission (compute-then-
    emit) so output is byte-identical (+0), and a new `destructible_planner.py`
    builds a `CleanupPlan` from the same functions. Each extraction step is
    corpus-verified +0 before the next.
  Gate: **924-fixture** shadow census — site-3 emitted locals 1,088
  (1,087 ERROR + exactly 1 Arc STRUCT); site-4 MUST_DROP 14 + full
  candidate/verdict split w/ PATH_DEPENDENT=0; nullsafe 133,998
  synthetic=0; every production counter +0; zero MIR/ledger-dirty/rebuild
  deltas. Sample/unit ladder first, but acceptance is the full 924.
  EXECUTION (maintainer 2026-07-21T053232Z): TWO milestones, ≤2 full corpus.
  - **Milestone A — DONE 2026-07-21**: `destructible_authority.py` extracted
    (DropClassifier; classify_destructible_locals; site4_verdict w/
    missing-ledger + PATH_DEPENDENT tripwires; compute_store_defs/
    assigned_in; site3_return_drops — ordered, reproduces skip fold +
    zero-storage widening BEFORE flag-managed fold). string_arc DELEGATES,
    emission/order/audit/identities unchanged (net −222/+74). 12 differential
    tests + existing string_arc suites green (121 in touched battery).
    GATE PASS: instrumented 924 corpus — every production counter +0 (14/14
    identical to flagret), universe identical (924, sym-diff 0), site-3
    census 1,088 = 1,087 ERROR + 1 STRUCT (the Arc event
    closures_share_capture_arc_generic::__lambda_main_0_0::app). Site-3
    probe reverted byte-identically (0 traces). One transient redundancy:
    string_arc keeps building skip/init inline for its String sweep +
    observe reporter + boundary audit (dies with string_arc in D);
    site3_return_drops is the single authority for the EMITTED drop set.
  - **Amendments (review 2026-07-21T120409Z) — DONE**: (1) CLOSED authority
    (canonical DropVerdict/compute_drop_policy/zero_storage_drop_safe/
    is_flag_managed imports; site4_verdict returns typed DropVerdict; dropped
    unused clf/string_ty/injected params). (2) Bookkeeping extracted ONCE —
    `compute_return_move_state` (moved-out fixpoint + intra-block explicit-drop
    replay) → immutable `ReturnMoveState`; string_arc + planner each invoke the
    SAME authority function INDEPENDENTLY (no shared instance today; no other
    read-sites; inline build removed). (3) PATH_DEPENDENT teeth
    (site-4 tripwire; site-3 zero-safe widened / zero-unsafe not). (4) EmitterPhase
    -in-production: AST pin forbids production `.consume`/`._mark_consumed`
    (must use begin_phase→stage→mark_rewritten→commit) + S5 Return-identity note.
    (5) prose: site3_return_drops = behavior-preserving recomposition; string_arc
    inline skip/init marked TRANSITIONAL.
  - **Milestone B — DONE, S2 CLOSED 2026-07-21**: standalone non-emitting
    `destructible_planner.py` on the closed authority at the pre-mutation
    ledger-A slot (immutable payloads, validate_and_freeze vs original snapshot,
    mutates nothing). 4 planner unit teeth; env-gated driver shadow census
    REVERTED after gate (driftc.py clean vs HEAD). FINAL 924 census PASS:
    site-3 **1,088** (1,087 ERROR + 1 STRUCT = the Arc
    `closures_share_capture_arc_generic::__lambda_main_0_0::app`); site-4
    MUST_DROP **14** + MUST_NOT_DROP 72,995, PATH_DEPENDENT **0**; nullsafe
    **133,998** synthetic **0**; planner_ok 1,107,693 / planner_fail 0; every
    production counter **+0**, universe identical (924), no MIR/ledger-dirty/
    build delta. Touched battery 153 green.
  - **S2 static closure (review 2026-07-21T195839Z) — DONE**, no corpus rerun:
    (1) planner uses `require_fresh_ledger` (ledger A REQUIRED regardless of
    population; teeth: no-site4 missing-ledger + dirty-ledger fail). (2) AST
    consume-bypass pin REPAIRED (was scanning nonexistent lang/lang/driftc):
    correct root lang/driftc, asserts root exists + >50 files visited, excludes
    only cleanup_plan.py impl, plus a synthetic positive/negative detector probe.
    (3) Type relationships ENFORCED not carried: `Decision.type_bindings`
    (local→TypeId) checked vs func.local_types at BOTH validate_and_freeze AND
    locate; StoreLocal.value in fields; teeth: wrong-type-at-freeze, absent-local,
    post-freeze type drift, value-operand drift. (4) Authority fully closed:
    `site4_verdict` computes canonical needs_drop internally + returns
    (DropVerdict, needs_drop); `compute_return_move_state` takes `string_ty`
    datum (not a predicate); `Site4Payload.verdict` is typed `DropVerdict` with
    __post_init__ rejecting PATH_DEPENDENT (unconstructible as a payload).
    Doc: planner has NO production consumer (census wiring removed); string_arc
    + planner invoke the same authority INDEPENDENTLY (no shared instance).
    Behavior-preserving (Arc fixture compiles +0-equivalent). 161 teeth green.
- [ ] **S3 — site-3 Return emitter (BUILD + TEST IN ISOLATION, do NOT
  wire to production yet)**: narrow module; emits at Return anchors in
  `sorted(destructible_locals)` order; validates each decision vs object
  identity + semantic fields + relative order. Production wiring is
  DEFERRED to S5 (S3 and S5 must not be two independent Return rewriters
  over the same original Return anchor). Gate: unit/focused tests on the
  isolated emitter green (no production corpus wiring in this step).
- [ ] **S4 — Overwrite authority** (nullsafe + site-4 into
  overwrite_cleanup, consuming plan): independent counters/bijections
  (nullsafe 133,998; site-4 14). Remove nullsafe + site-4 emission from
  string_arc atomically. Gate: focused + corpus +0; tripwire intact.
  NOTE (amendment 4): S5's Return rewrite MUST preserve the ORIGINAL
  Return object (insert cleanup before it; do not replace it with a new
  M.Return) so the plan's site-3 TERM anchor stays valid at postflight
  commit. Emitters use `begin_phase`→stage→`mark_rewritten`→`commit`
  (NOT `session.consume`; AST-pinned).
- [ ] **S5 — unified Return authority (site-3 + R3/R4), PRODUCTION
  wiring**: ONE coordinated Return-authority traversal consumes site-3
  (from S3) + C's R3/R4 String Return/scope decisions ATOMICALLY in a
  single Return rewrite (0.27.145 re-proof vs current upstream-stake/
  ledger model). The rewrite MUST preserve every original non-Return
  instruction object AND original-anchor relative order so the overwrite
  plan (S4) stays valid. Remove site-3 + R3/R4 emission from string_arc
  atomically with wiring the unified authority. Gate: corpus site-3
  +0 / R3=68,562, return-alias safety, memcheck; overwrite-plan anchors
  still validate.
- [ ] **S6 — C: R8** on the same original-MIR planning window. Gate:
  corpus +0.
- [ ] **S7 — no-residual-rebuild proof**: after B2+C, no ledger
  consumer forces an intermediate rebuild; ledger-build count gate
  (zero additional vs pre-B2+C). Gate: instrumented build-count.
- [ ] **S8 — B1/B2+C cleanup debts**: (1) _validate occurrence
  hardening + teeth; (2) strip transient MIR attributes
  (ow_authored_for, synthetic_zero_back once consumer ran); (3)
  StoreRef prose retarget in test_mut_struct_string_field_self_concat;
  (4) unused `mutated` in overwrite_cleanup; (5) retarget overbroad
  hir_to_mir.py:5770-5776 comment (authority-comment only). Gate:
  battery + grep-clean.
- [ ] **S9 — end static delta review** for the B2+C chunk (NOT a
  cert). Then D (R5/R1 + delete string_arc.py + driver phase) → the
  single 0.33.87 full serial suite + certification.

## Ordering note
S3 builds the site-3 emitter in isolation; S5 wires the ONE unified
Return authority (site-3 + R3/R4). S4 (overwrite plan: site-4 +
nullsafe StoreLocal anchors) depends on the Return rewrite preserving
every original non-Return instruction object + relative order — so the
overwrite-plan anchors must still validate after S5's Return rewrite.
The complete plan is computed ONCE (S2) from ledger A + original MIR
before any mutation; all emitters consume it.

## Log
- 2026-07-21: B1 committed (7108b2bd) as recovery point; final doc
  amendment landed (refactor-trigger all-classes recast + immediate/
  callback asymmetry + overbroad-comment inventory). B2+C authorized,
  no further architecture review. S0 done (pipeline map; zero-added-
  rebuild confirmed; ledger A = rebuild_after_cleanup_authoring @8126).
- 2026-07-21: anchor-lifecycle contract recorded (maintainer
  2026-07-21T001349Z): plan-time proof coordinate `(block,
  original_index)` ≠ consumption-time numerical index; consumption
  validates object-identity + same-block-once + semantic fields +
  relative order; changed index OK, move/replace/dup/orphan fail closed.
  Return-authority choreography: S3 isolated, S5 unified. S1 is GO.
- 2026-07-21: S1 v1 — cleanup_plan.py container + 15/15 teeth.
- 2026-07-21: S1 review (2026-07-21T044707Z) HOLD → CLOSED. Three
  foundational fixes:
  (1) **build-time occupancy validation** `validate_and_freeze(func)` —
      proves each anchor is exactly at its recorded coord (INSTR at
      instructions[orig_index] & isinstance MInstr; TERM is
      block.terminator & orig_index==len(instructions)); belongs-to-func;
      coord-collision + cross-site coord/field consistency (at add);
      consumption forbidden pre-finalize.
  (2) **immutability + plan-private consumption state** — `Decision` is a
      frozen dataclass with tuple `fields`; NO public `consumed`; state
      lives in plan `_consumed` keyed by plan-owned `token`; foreign
      decisions rejected via `_require_owned`; introspection returns
      tuples.
  (3) **batch O(1) consumption** — `ConsumptionSession` scans the func
      ONCE (`scan_count==1` pin), builds identity→location + occurrence
      index, validates relative order once; `locate()` O(1). No
      O(decisions×MIR).
  Follow-through: unused imports (field/Callable) removed; end-to-end
  PlanContractError boundary-containment pin DEFERRED to driver-wiring
  time (S3/S4/S5) as required. Teeth 29/29.
- 2026-07-21: S1 second review — two bypasses CLOSED:
  (A) consumption is now session-only: no public `mark_consumed`;
      `session.consume(dec)` validates the anchor via `locate` first,
      then marks consumed (plan-private `_mark_consumed`). Can't mark
      consumed without a session validating the anchor.
  (B) sessions are STALE-SAFE + phase-scoped: `locate`/`consume` do an
      O(1) `is`-recheck that the anchor is STILL at its scanned location
      in the CURRENT MIR (a mutation-since-scan → fail closed); session
      is a context manager (`with`), `close()` invalidates it. Use one
      session per emitter phase (PREFLIGHT before rewrite; FRESH session
      POSTFLIGHT to re-validate survivors).
  Plus: declared semantic fields validated at `validate_and_freeze`
  (not only at consumption). Teeth **32/32** (added stale-session,
  closed-session, no-public-mark-consumed, field-mismatch-at-finalize).
  **S1 commit-clear.** Committed as recovery point 7df534dc.
- 2026-07-21: S1 closure follow-up (post-commit, review 2026-07-21T051015Z
  — the three items were already in 7df534dc; added the explicit phase
  protocol): `EmitterPhase` enforces preflight `stage()` → `mark_rewritten()`
  → `commit()` (fresh postflight validate, THEN consume — a decision is
  never marked consumed on a stale preflight view; commit fails closed if
  the rewrite broke an anchor, marking nothing). Full stale-matrix pins:
  open-session then insert-before / remove / move / duplicate / field-drift
  → rejected via O(1) is-recheck (no per-decision whole-function rescan).
  Missing-attribute field validation pinned. Teeth **42/42**.
