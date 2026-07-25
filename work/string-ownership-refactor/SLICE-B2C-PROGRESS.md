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
- [x] **S3 — site-3 Return emitter (ISOLATED/UNWIRED) — DONE 2026-07-21**:
  `site3_return_emitter.py::emit_site3_returns(func, plan)` uses the full
  `EmitterPhase` lifecycle (begin_phase→stage all site-3→rewrite→
  mark_rewritten→commit). APPENDS the canonical drop sequence
  (LoadLocal→ZeroValue→StoreLocal[synthetic_zero_back]→DropValue) per
  ordered `Site3Drop`, AFTER all original instructions and BEFORE the
  PRESERVED `M.Return` (identity+value+span kept; original instruction
  objects/order kept). UNWIRED (no driver caller; S5 wires the unified
  authority). Plus `CleanupPlan.assert_sites_consumed(sites)` for
  multi-consumer completeness (keeps final global `assert_all_consumed`).
  Teeth `test_site3_return_emitter.py` (empty/multi-drop/order/Return-
  identity/span-value/type-drift/replaced-Return) + completeness tooth.
- [x] **S4 — Overwrite authority (PRODUCTION migration) — DONE 2026-07-21,
  GATE PASS**: driver builds the frozen plan ONCE per fn at the pre-
  string_arc ledger-A slot (driver-local dict, no MIR attr, no ledger
  build) with boundary containment; passes it to `overwrite_cleanup`,
  which after its unchanged R2/R7 rewrite+`_validate` runs a SEPARATE
  EmitterPhase consuming nullsafe+site4: nullsafe emits one drop-before-
  store each; site4 MUST_DROP emits (14), MUST_NOT_DROP validated+consumed
  but emits nothing; `assert_sites_consumed({"nullsafe","site4"})`. site-4
  audit note (14) migrated via `record_counted_only(SITE4)` (added to the
  reporter allow-list). string_arc's nullsafe+site4 arms NEUTERED (stores
  pass through). mutation-audit: `mark_ledger_dirty("overwrite_cleanup.
  plan_overwrite")` per changed block. **924 GATE: every production
  counter +0** (drop_before_overwrite_site4=14, overwrite_release=233,519),
  universe identical (924, sym-diff 0). **Census (temp hook, reverted):
  nullsafe 133,998, site4 14+72,995=73,009, PATH_DEPENDENT 0, site3 1,088.**
  156 focused teeth green; self-alias/memcheck carriers pending.
  ORIGINAL: independent counters/bijections
  (nullsafe 133,998; site-4 14). Remove nullsafe + site-4 emission from
  string_arc atomically. Gate: focused + corpus +0; tripwire intact.
  NOTE (amendment 4): S5's Return rewrite MUST preserve the ORIGINAL
  Return object (insert cleanup before it; do not replace it with a new
  M.Return) so the plan's site-3 TERM anchor stays valid at postflight
  commit. Emitters use `begin_phase`→stage→`mark_rewritten`→`commit`
  (NOT `session.consume`; AST-pinned).
- [x] **S5 — unified Return authority (site-3 + R3/R4), PRODUCTION
  wiring — DONE 2026-07-22, GATE PASS + closure review applied**:
  `return_cleanup_emitter.py::emit_return_cleanups(func, plan)` (replaces
  the unwired S3 `site3_return_emitter.py`) consumes the plan's
  `string_release` (R3/R4, new `StringReleasePayload` +
  `string_return_releases` authority) AND `site3` decisions ATOMICALLY
  per PRESERVED `M.Return`: string-release band (LoadLocal→ZeroValue→
  StoreLocal[synthetic_zero_back]→StringRelease per local) THEN the
  site-3 drop tail, both before the terminator; every original
  non-Return instruction object + order untouched (S4 overwrite anchors
  still validate). Driver phase `return_cleanup` (after string_arc,
  before overwrite_cleanup), boundary-contained. AUDIT: differential
  finalize moved out of string_arc — driver-owned SINGLE deferred
  `StringArcAudit.finalize` per fn with ONE `l_post` built AFTER
  return_cleanup and BEFORE overwrite_cleanup (byte-identical former
  build point); plan slot freezes the C1 ledger-A half
  (`C1BoundaryFrozen`/`C1Contribution`); split finalize synthesizes
  `scope_exit_release` events exactly once (sole source; monolithic
  path retained for the byte-identity reference test). string_arc's
  R3/R4/site-3 emission + self-finalize removed atomically. **GATE
  PASS: 924 corpus all 14 counters +0 (scope_exit_release=68,562,
  pre_post_verdict_drift=48,178, materialized_lastuse_release=618,744),
  universe identical; memcheck 105 passed/1 skipped, 0 leaks; focused
  battery 218.**
- [x] **S6 — C: R8 recognition on the plan window — DONE 2026-07-22,
  same combined gate**: `compute_recognized_releases` in
  `string_ownership_analysis.py` is the SINGLE recognition entry point
  (seeds a COPY of local_types; invokes `build_fnwide_producers` +
  `compute_string_temp_liveness` + per-block
  `recognize_materialized_releases` ONLY there) returning frozen
  `R8Recognition`. Driver computes `_r8contrib[fn_id]` for EVERY fn at
  the plan slot; string_arc CONSUMES the frozen vessel (fallback via the
  same entry point for bare unit invocation only; source pin: string_arc
  names none of the three analyses). Covered by the S5 combined 924
  corpus +0 gate (recognition affects codegen; `materialized_
  lastuse_release` 618,744 +0) + consume==fallback identity teeth.
- [x] **S7 — no-residual-rebuild proof — DONE 2026-07-22, review-HOLD
  closure applied (test/proof-only)**: durable instrumented gate
  `lang/tests/driver/test_b2c_zero_added_ledger_builds.py` (6 tests).
  Instrumentation installed ONCE per test (stacking corrupted counts —
  found & fixed) and covers EVERY bound build path: `ownership_ledger.
  build_ledger` (source + driver l_post runtime import), `ledger_cache.
  build_ledger` (attach traffic), and the PRE-BOUND aliases
  `drop_flags.build_ledger` + `cleanup_authoring.build_and_attach_
  ledger`.  ONE test compiles the fixture audit-OFF then audit-ON and
  proves: (a) reason-set EQUALITY both directions vs the frozen set
  (4 driver builds + `cleanup_authoring.in_pass_rebuild`; the fixture
  exercises all 5 incl. `rebuild_after_drop_flags`); (b) FULL raw
  attribution — raw == attach + drop_flags-internal + expected l_post
  (0 off / exactly one per planned fn on), so any out-of-consumer
  direct build fails; (c) cross-mode: identical reason MULTISET,
  identical df count, raw_on == raw_off + planned_fns; (d) all four
  B2+C consumers zero-build-delta in both modes.  Negative teeth:
  missing frozen reason, extra reason, extra unattributed raw build,
  missing l_post all fail the helpers.  Source pin extended to the
  full plan-window/consumer surface incl. `string_arc.py` +
  `string_ownership_analysis.py` (8 modules; none may name a build
  entry point).
- [x] **S8 — B1/B2+C cleanup debts — DONE 2026-07-22 (combined S7+S8
  closure chunk)**:
  (1) `_validate` rewritten-site survival hardened — output occurrence
  count per inventoried store identity; vanished (0) or duplicated (>1)
  store is a contained AssertionError, never a raw `pos[id]` KeyError /
  silent position collapse; vanished+duplicated teeth added.
  (2) transient MIR attributes STRIPPED — `_strip_transient_attrs` at
  the end of `insert_overwrite_cleanup` (after every consumer: R2
  recognition skip, `_validate`, plan-emission validator, planner
  tripwire, Return-emitter checks, audit l_post) removes
  `ow_authored_for` + `synthetic_zero_back` from all instructions;
  pipeline tooth pins no-transient-attrs output; the plan-test
  `_canonical_drop_before` helper no longer requires the tag.
  (3) StoreRef authority prose retargeted in
  `test_mut_struct_string_field_self_concat.py` (5 present-tense
  `string_arc` claims + 2 stale `string_arc.py:NNNN` citations →
  `overwrite_cleanup` `_K_STORE_REF` arm).
  (4) unused `mutated` local removed from overwrite_cleanup.
  (5) overbroad `hir_to_mir` MOVE/SHARE capture comment retargeted:
  callback lambdas (heap env + cb-drop thunk owner) never reach the
  register line with MOVE/SHARE; immediate lambdas do, and SITE-3 (the
  unified Return authority) is their live release authority —
  authority-comment only, no lowering change.
  (6) the 5 pre-existing bare-`insert_string_arc` tests repaired
  through the PRODUCTION-FAITHFUL pipeline (plan → string_arc →
  return_cleanup → overwrite_cleanup): swap emit/skip/POD tests assert
  the overwrite plan phase's emission; missing-ledger tripwire now
  pins BOTH the production `require_fresh_ledger` refusal AND the
  authority-level `site4_verdict` RuntimeError; PATH_DEPENDENT
  tripwire raises from the plan build; observe-mode `[drift:
  ownership_ledger]` site-4 telemetry RE-HOMED into
  `destructible_planner`'s site-4 arm (debug-gated, same site tag/
  verdict/reason/point/needs_drop axis — it had been LOST in the S4
  migration); `test_llvm_codegen_string::
  test_string_literal_overwrite_emits_release` runs the full sequence
  before SSA→LLVM.
  Plus carried S5/S6-review polish: the driver completeness guard
  type-checks plan (`CleanupPlan`) / R8 (`R8Recognition`) / collector
  (`StringArcAudit`) values, and `R8Recognition` rejects non-string
  frozenset members; teeth for plan/R8 value injections.
- [x] **S9 — end static delta review — DONE 2026-07-22, CLEAR** (NOT a
  cert): `2026-07-22T152813Z-drift-lang-release-notes.md` — S7 HOLD
  closed and the complete S7+S8 chunk commit-ready; no production,
  test-contract, version, ABI, or architecture blocker remains.
  (The then-next step — Phase D — is COMPLETE; see the Phase D combined
  sweep section below.)

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
- 2026-07-21: post-S4 CONTRACT-HARDENING closure (fail-closed, guard-only —
  NO production emission or decision-population change; item-1 `.ll` diff of
  `closures_share_capture_arc_generic` byte-identical under `PYTHONHASHSEED=0`
  modulo the build-timestamp banner):
  * **Remaining-anchor postflight (item 1).** `string_arc` now PRESERVES the
    original `M.Return` object at each Return-boundary rewrite (updates
    `term.value` in place, keeps `block.terminator = term`) so the frozen
    plan's site-3 TERM anchors survive.  New NON-consuming
    `CleanupPlan.validate_unconsumed(func, sites=None)` re-proves every
    still-unconsumed decision's anchor via `session.locate` without
    consuming.  Driver calls it right after the string_arc loop (site-3
    survived string_arc, contained at the string_arc boundary) and
    `insert_overwrite_cleanup` calls it at the END of the plan phase (site-3
    survived null-safe/site-4 insertion).  Teeth: legit-insertion validates;
    replaced / disappeared / field-drifted Return fail; consumed decisions
    skipped; before-freeze refused.
  * **Mandatory plan + total planner containment (item 2).**
    `insert_overwrite_cleanup(..., plan)` is now REQUIRED (no `=None`; a
    `None`/non-CleanupPlan plan is a fail-closed `PlanContractError`); the
    no-op path is gone.  Driver uses exact-key `_dplans[fn_id]` and asserts
    `set(_dplans) == set(mir_funcs_by_id)` before the overwrite loop (a
    missing plan can never mean skipped cleanup).  The plan-build loop now
    catches `(AssertionError, RuntimeError)` — `PlannerStop` and the
    site-4/site-3 authority tripwires join `PlanContractError` under one
    `destructible_plan` boundary diag.  Legacy R2/R7 unit tests pass an
    explicit validated EMPTY plan; the old `plan=None` no-op test is now a
    REFUSAL test; new `test_destructible_plan_boundary.py` pins both the
    PlanContractError and the PlannerStop/RuntimeError containment.
  * **Emission↔consumption bijection in overwrite_cleanup (item 3).**
    `emit_anchors` is built fail-closed (duplicate identity key and
    wrong-payload/site rejected).  BEFORE `commit()`, `_validate_plan_emission`
    proves, separately for null-safe and site-4 MUST_DROP, that each emitting
    decision produced EXACTLY ONE canonical `LoadLocal→ZeroValue→StoreLocal
    [synthetic_zero_back]→DropValue` (full operand/type links; drop↔store
    identity tracked in an emitter-local side table, NOT a MIR attribute — see
    closure delta 2), and that MUST_NOT_DROP authored nothing.  Teeth:
    suppressed + duplicated + MUST_NOT_DROP-authored all fail before commit.
  * **Hardened unwired S3 emitter → S5 base (item 4).**
    `site3_return_emitter` seeds collision-proof temps from `local_types` +
    every SSA dest/operand (collision pin against a pre-existing `.s3d*`
    name), marks the ledger dirty IFF ≥1 drop emitted (added to the
    mutation-audit `SCOPED_FILES`), self-calls
    `assert_sites_consumed({"site3"})`, validates payloads to
    `PlanContractError` (never a raw `AttributeError`), and proves the
    site-3 expected/emitted bijection before commit (Return preserved).
    Removed the unused `typing.Any` import.
  * **Closure items (item 5).** Reporter exact-delta teeth for
    `record_counted_only(SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4, n)`;
    corrected `overwrite_cleanup.py` stale module prose (null-safe + site-4
    now emit HERE from the plan; site-3 stays string_arc's until S5); this
    entry.

## S4 closure delta 2 (review 2026-07-22T043031Z) — DONE, contract-only
- (1) `_dplans` set-mismatch now BOUNDARY-CONTAINED as phase `destructible_plan`
  (clean internal diag, empty IR, no traceback) — was a bare `assert` that could
  escape; driver tooth injects a ghost plan key to force the (otherwise
  structurally-unreachable) guard.
- (2) Removed dynamic MIR tags `plan_authored_for` (S4) + `s3_authored_for` (S3);
  authoring identity now in emitter-local side tables (overwrite: list of
  (store_id, drop); site3: {return_id: [drops]}), passed to the validators. Teeth
  exercise the side tables, not tagged MIR.
- (3) S3 validation is ORDER-SENSITIVE (emitted (local,ty) sequence in block order
  == payload's sorted destruction order, no sorting) + requires `isinstance
  Site3Drop` (was `hasattr`). Teeth: reordered-sequence fails; non-Site3Drop →
  PlanContractError.
- (4) `_validate_plan_emission` missing Store-anchor → `PlanContractError` (was raw
  `KeyError`); removed-anchor tooth.
- Emission-neutral (overwrite_release=250, events=2955 on the Arc fixture; no
  setattr of the removed tags). Affected battery 99 green. No corpus rerun.

## S5+S6 static review closure (review 2026-07-22T130128Z) — DONE,
contract-only (no recognition/decision/emitted-MIR change; NO corpus
rerun per reviewer; the accepted 924 +0 + memcheck gates remain valid)
- Review verdict: architecture + measured production gate ACCEPTED
  (unified Return ordering, l_post placement, R8 window, 924 +0,
  memcheck); HOLD on five contract-closure groups, all now closed:
- **(1) Driver contribution tables — exact completeness before first
  consumption.** ONE table-completeness guard after the planning loop
  and BEFORE string_arc (subsumes the former pre-overwrite `_dplans`
  check): `set(_dplans) == set(_r8contrib) == set(mir_funcs_by_id)`;
  audit ON additionally requires collector + C1 sets == fn set AND every
  C1 value a `C1Contribution`; audit OFF requires both audit maps EMPTY.
  Boundary-contained as `destructible_plan` ("completeness failure"
  prefix, empty IR). Every production consumer now indexes EXACTLY
  (`[fn_id]`, never `.get`) — a missing R8 entry can no longer select
  string_arc's fallback, a missing collector/C1 entry can no longer
  select the monolithic finalize. Driver teeth: ghost plan/R8/collector/
  C1 entries + audit-off residue, all contained.
- **(2) Exactly-once, boundary-contained finalize lifecycle.**
  `StringArcAudit._finalized`: `finalize()` refuses a second call BEFORE
  any event/aggregate mutation (flag set even on a failing finalize);
  `note()`/`note_return_boundary()` after finalization fail closed. The
  driver finalize loop (collector lookup, l_post, contribution lookup,
  merge) is contained as phase `string_arc_audit_finalize` (clean
  `internal:` diag, empty IR); the observational l_post
  build-failure counter behavior preserved. Source pin STRENGTHENED:
  whole-`lang/driftc` sweep — only the driver coordinator (driftc.py)
  may contain a `.finalize(` invocation. End-to-end injected-finalize
  containment driver tooth added.
- **(3) Genuinely immutable, complete R8Recognition.** Mapping validated
  (str -> frozenset) and COPIED into `MappingProxyType` at construction;
  `for_block` fail-closed on a missing key (never default-empty).
  string_arc validates at consumption BEFORE rewriting: recognition
  block-key set == function block set, every value frozenset. Teeth:
  immutability (direct + alias mutation), malformed value, missing
  block, extra block, wrong function.
- **(4) Structurally validated C1 tied bijectively to the plan.**
  `validate_c1_contribution` (reporter): unique well-formed boundary
  points; unique+sorted string_locals; released/skipped duplicate-free,
  disjoint, PARTITION string_locals; verdict/raw key sets exactly ==
  string_locals; enum-typed values — all AssertionError, never KeyError;
  called in the split finalize BEFORE merge. `crosscheck_c1_against_plan`
  (planner, at the plan slot while original coordinates are valid):
  C1 boundaries ↔ plan `string_release` decisions in bijection by Return
  coordinate with EXACT ORDERED equality of `released` vs payload locals.
  Non-None C1 mandatory when audit enabled (group-1 guard); merge
  exactly once (group-2 flag). Teeth: dup boundary/local, missing
  verdict/raw entry, bad partition, wrong enum type, malformed-at-merge,
  release drift/dropped/foreign boundary, double merge, late notes.
- **(5) Records.** Status report toolchain line corrected (tree = the
  consolidated **0.33.87 / ABI 21 candidate**, not 0.33.80; cert still
  lands only after Phase D); S5/S6 flipped DONE above with the
  single-finalize placement + exact counter gate recorded; the 5
  pre-existing bare-`insert_string_arc` failures explicitly assigned to
  S8 item (6).
- Verification: focused affected batteries only (reporter / R8 /
  driver-boundary / planner / mutation-audit 97 green; adjacent
  emitter/plan/authority/string_arc suites 141 green; 238 total).
  Awaiting static delta review.
- 2026-07-22 (later): S5+S6 static review CLEAR
  (2026-07-22T131935Z-drift-lang-release-notes.md); chunk committed as
  `a40776a1`.  Non-blocking record correction applied: closure-delta
  report's certified baseline corrected to **0.33.85 / ABI 21** (0.33.86
  cert in flight per RESUME-CHECKPOINT), not 0.33.80.

## S7+S8 combined closure chunk (authorized 2026-07-22, post-a40776a1)
- Scope per clearance: prove zero added ledger builds (S7); discharge
  all recorded cleanup debt + repair the 5 production-pipeline test
  debts (S8, incl. the carried S5/S6 value-type/member polish); ONE
  static review at the end; NO certification.
- S7 + S8 item details recorded in the checkboxes above (both flipped
  DONE this chunk).
- Corpus policy: SLICE-B §9 standing rule (validator/authoring changes
  MUST re-run the corpus) applies — `_validate` hardening + transient-
  attr strip touch the validator/authoring surface, so the 924 audit
  (`tools/drift_corpus_audit.py --out build/tmp/s7s8 --baseline
  build/tmp/s5s6`) + full memcheck are part of this chunk's gate.
  Observe re-home is debug-gated (off in corpus); strip runs after the
  audit l_post build → expected +0.
- Focused battery: 260 green across the touched set (overwrite family,
  planner, authority, swap, plan-boundary incl. new value-type teeth,
  r8 incl. member teeth, cleanup_plan, site3/return emitter, audit +
  ownership-ledger reporters, mutation-audit, ledger-build gate,
  codegen string, StoreRef lowering pin).  One test-helper update:
  `_canonical_drop_before` no longer requires `synthetic_zero_back`
  (the pass strips it — that IS debt item 2's contract).
- 2026-07-22 (later): S7 gate review-HOLD closed (equality + attribution
  + pre-bound aliases + string_arc source pin + negative teeth); S9
  static delta review CLEAR (2026-07-22T152813Z); chunk committed by
  maintainer.  **Phase D deletion checkpoint OPENED (report-only):
  `PHASE-D-DELETION-CHECKPOINT.md`** — full string_arc re-inventory
  (R1/R5/R8-note + seeding side-effects + output-neutral proof),
  permanent homes (new `moveout_zeroing.py` pass at the string_arc slot;
  observe site-3 records → planner), pinned driver sequence with zero
  added ledger builds, counter disposition (all 14 preserved +0),
  complete deletion inventory, fail-closed pins, acceptance sequence
  ending at the single full serial suite + 0.33.87/ABI-21 cert.
  STOPPED for review; no implementation.

## Phase D combined sweep (GO 2026-07-22T185411Z) — COMPLETE
- Binding decisions applied: pass = `ownership_normalization.py::
  normalize_ownership_mir` (driver phase `ownership_normalization`);
  R8 freeze stays UNCONDITIONAL (production fail-closed release
  validator); site-3 observation shares ONE structured authority result
  (`Site3Decision` from `site3_return_decision` — drops + flag_managed +
  generic_skips + initialized; planner consumes it for BOTH plan payload
  and debug records, never inferred from the drop tuple); table-driven
  seeding coverage; 924 shadow differential before deletion.
- DONE so far: Site3Decision refactor + planner consumption + observe
  re-home (authority tests updated); ownership_normalization.py written
  (R1 + R5 + R8 copy-through by IDENTITY + verbatim seeding sweep +
  dirty-iff-changed, no ledger); driver rewired (phase, containment,
  validate_unconsumed relocated); S7 gate + mutation-audit retargeted;
  20-test unit file incl. 12-family table-driven seeding pins; TEMP
  shadow instrumentation in driver (declared-fields exact, dynamic
  metadata one-directional — legacy reconstruction LOST span/debug_name
  on rebuilt instructions; identity pass-through preserves them =
  the sanctioned improvement); test migration: swap/codegen-string/
  debug-return-span/site3-return-swap (renamed from string_arc_return_
  swap)/move_from_ref_ownership_contract (renamed)/drop_classifier_
  recursive_type_guard (renamed)/r8-plan-window (production-wide
  single-owner pin, string_releases carve-out for the two shared input
  analyses)/audit-reporter (boundary pin → ownership_normalization
  phase; finalize pin anchored off the reporter)/extraction (neutral-
  library pin generalized to all consuming passes)/zero_storage
  (shim-retirement pin — green only post-deletion); production prose
  sweep (191 → historical/retargeted).  FIXED in-flight: shadow-helper
  insertion had detached `@_with_compile_recursion_headroom` from
  `compile_stubbed_funcs` (caught by the headroom pin; CLI path was
  unaffected — corpus valid).  Battery: 574 passed / 1 expected-red
  (shim pin, green post-deletion).
- **SHADOW DIFFERENTIAL GATE PASS** (build/tmp/phase-d-shadow vs s7s8):
  exit 0, universe identical (all 924 compiled — zero old-vs-new
  divergences across ~1.1M fns; comparator = declared dataclass fields
  EXACT + dynamic metadata one-directional), all 14 counters +0 with
  the NEW pass authoring the notes.
- **string_arc.py DELETED**; shadow instrumentation removed from the
  driver (grep-zero); file-absence + no-production-import pins added
  and green; shim-retirement pin green; residual sweep: zero production
  imports, flagged test prose retargeted (boundary/plan tests,
  overwrite pins, memcheck carriers, alias-walk/typebox docstrings).
- Post-deletion battery: **2,772 passed / 1 skipped** (one PRE-EXISTING
  stale fixture repaired in-sweep: `test_variant_borrowed_match_
  construct_int_payload` predates the 2026-06-30 "Require public app
  entrypoints" enforcement — `fn main` → `pub fn main`; front-end only,
  unrelated to Phase D).
- **FINAL CORPUS GATE PASS** (build/tmp/phase-d-final vs s7s8, clean
  run, string_arc deleted): exit 0, universe identical, **all 14
  counters +0**, hard gates zero.
- **Memcheck: 105 passed / 1 skipped, 0 leaks, lane audit PASS.**
- **Ownership matrices: om 51/51 successful, 0 failed.**
- **BROAD PYTEST BATTERY PASS**: `pytest -n16 --dist=worksteal lang/`
  (superset of the 12 uniform emit_test_plan lanes + driver/codegen/
  modules/checker/runtime/e2e) — **3,946 passed / 5 skipped**, exit 0,
  lane audit PASS, 0 leaks (33m41s).  NOTE (final-review item 3): this
  is NOT the repository certification gate — the full-suite gate is
  `run-all.sh` (`just test` under BOTH `DRIFT_MEMCHECK=1` and
  `DRIFT_ASAN=1`, incl. standalone LLVM/IR/e2e, deploy tooling, and
  package-consumer boundary recipes) and remains PENDING as the single
  release boundary (maintainer-run after commit-clear).
- **PHASE D SWEEP COMPLETE — returned for the single final static
  review.**  Final report:
  `/tmp/drift-announce/2026-07-22T230000Z-phase-d-final-report.md`.
- **Final-review closure (HOLD 2026-07-23T013128Z, four items — all
  documentation/test-contract only, no corpus/memcheck rerun):**
  (1) records closed onto the completed architecture — doc/history.md
  0.33.87 entry now describes B1 + B2+C (S1–S9) + Phase D and the
  final `ownership_normalization` topology; the Phase D checkpoint is
  CLOSED with an implementation banner (moveout_zeroing name and the
  R8 audit-gating remark marked superseded/retracted); this file's
  stale "Phase D is next" tails annotated.
  (2) live-prose sweep finished — planner "unwired/no consumer"
  docstring replaced with the production plan-authority contract;
  overwrite_cleanup placement/provenance/S4-tail corrected;
  string_stakes old-destination release re-attributed to
  overwrite_cleanup; drop_policy_compute zero-init/site-3/shim prose
  retargeted (R1 in ownership_normalization; widening in
  site3_return_decision; shim RETIRED); driver comments fixed; plus a
  sweep of ledger_cache/ownership_ledger/reporter/string_releases/
  string_stakes/mir_nodes/match_cleanup_authoring/cleanup_authoring/
  destructible_authority residuals — every live authority statement
  now true, genuine history preserved.
  (3) the pytest lang/ result RELABELED as the broad battery (above);
  the run-all.sh certification gate stays pending.
  (4) seeding pin strengthened: table pin re-scoped to the
  absent-destination axis; NEW teeth pin the two real axes —
  instruction-carried families OVERWRITE a stale pre-existing dest
  binding (StructGetField/LoadRef/LoadLocal probes) and the prescan
  String-ZeroValue family PRESERVES an existing binding
  (only-if-missing), each with an unrelated-binding noninterference
  control.  Affected pins rerun green.
  Stale-fixture `fn main` → `pub fn main` correction accepted by the
  review.
- **Round-2 mechanical closure (narrow HOLD 2026-07-23T015208Z) —
  DONE**: this heading flipped to COMPLETE; report test-count 24→26;
  ALL remaining current-tense references to the deleted pass finished
  across production + migrated tests (final `rg` inventory: only stable
  tags, pin/scanner internals, and explicitly historical prose remain);
  two runtime strings kept matcher-stable (finalize diag prefix;
  "must not emit releases" guard — both teeth verified); `git diff
  --check` clean.  Per review: no pytest/corpus/memcheck rerun.
- **run-all.sh false start fixed (2026-07-23)**: `ownership-matrix-check`
  failed because one round-2 prose retarget in `__ownership_matrix__/
  _gen.py` was inside an EMITTED fixture template (the `//` comment in
  `om_match_bind_token/main.drift`), desyncing generator vs checked-in
  fixture.  REVERTED that one emitted line to its original historical
  wording — the fixture text must stay byte-identical (it is corpus-
  universe content; regenerating would shift the universe hash and
  invalidate the +0 baseline chain).  The two Python-side generator
  comments keep their retargets (not emitted).  `--check`: 51 fixtures
  up to date.  Rule recorded: generated-fixture TEXT is frozen test
  data — historical references inside it are provenance, not live prose.
- **CERTIFICATION GATE PASS (2026-07-23, run by K per one-time
  maintainer authorization)**: `./run-all.sh` — the FULL `just test`
  under BOTH `DRIFT_MEMCHECK=1` and `DRIFT_ASAN=1` — **exit 0**, both
  modes "lang tests: Success." + "ASAN suite OK"; zero FAILED, zero
  recipe failures, zero sanitizer/leak reports (log:
  `build/tmp/run-all-phase-d.log`; memcheck-mode driver lane 2,135
  passed / 1 skipped; ASAN-mode driver lane 2,095 passed / 41
  mode-selected skips; matrix-check + all shards green).
  **COMMIT-CLEAR with the certification gate GREEN.**
- **0.33.87 / ABI 21 CERTIFIED AND DEPLOYED (2026-07-23**, pool run
  `20260723-120948-drift-lang-3d48b7f`; 8 pkgs + 2 apps at abi 21**).
  The string_arc endgame is CLOSED end to end.**
- **B-repr(B5) OPENED (design-first, ABI 21→22)**: report-only
  checkpoint at `B5-ABI22-DESIGN-CHECKPOINT.md` — exact
  native/LLVM/runtime-C/static-literal/empty layouts; RcBytes header/
  flags/byte-tail/hidden-NUL/retain-release/allocation/overflow rules;
  borrowed/owned C interop + checked/unsafe C-string APIs; full
  consumer inventory (incl. the StringByteAt inline byte-GEP and the
  zero-storage tombstone contract); atomic 21→22 migration sequence w/
  versioning + mismatch regression; pool/DriftQuery/external-FFI plan;
  acceptance matrix; STOP conditions.  ONE coordinated branch, ONE
  ABI-22 certification.  STOPPED for design review; no implementation.
- B5 design review rounds (2026-07-23): ten binding decisions folded in
  (0.33.88/ABI-22; two-word handle; atomic flags + pinned state
  machine; corrected DROP-ONLY tombstone w/ fail-closed observation +
  armed-trap retain/reachability gates; singleton `__drift_rt_string_
  empty` (IMMORTAL, mutually exclusive w/ STATIC); accessors + layout
  audit; to_cstr preserved owned; probe-proven std.ffi APIs; no-UTF-8-
  validation + ctor edge table; scope-closure table).  External FFI
  audit PERFORMED pre-GO: zero downstream C sees DriftString; zero
  by-value String externs downstream; recompile-only impact; DriftQuery
  sign-off BLOCKS cert.  Call boundary CHOSEN: private
  `string_bytes_base` intrinsic + pointer-taking C helpers (&String);
  owned/scope types completed (ReleasedCBytes, nullable-field release —
  no forget intrinsic needed; CArgv = Copy non-owning view).  ALL §3
  signatures COMPILE-PROVEN by the permanent regression
  `lang/tests/driver/test_b5_ffi_signature_probes.py` (green), which
  also recorded a checker finding: helper-returned `mem.Ptr<T>` fails
  require-bound unification (inline works) — [SUPERSEDED by round 4:
  reclassified LANGUAGE_BUG and fixed regression-first on this branch
  per repository policy; no separate slice].  Exact-tombstone validation,
  `>=` refcount threshold, unconditional flag/malformed contract path
  pinned.  Representation arm selection CLOSED per reviewer.
- B5 review round 4 (2026-07-23, four blockers) — CLOSED:
  (1) **LANGUAGE_BUG fixed regression-first** (probe discovery was a
  checker defect, not API debt): `call_resolver.py` argument-position
  `defer_infer_diag` deferral returned Unknown BEFORE attempting
  resolution, silently un-typing any nested call in method-arg
  position.  Protocol: minimal failing regression
  `test_nested_call_arg_defer_infer_regression.py` (confirmed FAIL
  pre-fix), subsystem recorded (checker call-resolution),
  refactor_triggers.md scanned (no trigger), root-cause fix (deferral
  now sandboxes a real resolution attempt; commits clean successes;
  genuine inference failures still defer), regression PASSES.  Rides
  the 0.33.88/ABI-22 branch, shares its single cert.  Post-fix
  batteries: type_checker/checker/traits/method_registry/borrow/
  stage1/parser 585 green; stage2/core/stage3/stage4/modules/packages
  1,107 green; driver+codegen battery running.
  (2) with_cstring_scope corrected: helper RETAINS ownership, body
  gets `&mut CStringScope` (move-out impossible → callback-bound
  cleanup guaranteed); both forms compile-proven.
  (3) probe v2: EVERY promised family probed (throwing 1..4, unsafe
  1..4, both scope forms, ReleasedCBytes data()/size() + CArgv
  vector()/count() getters) — compiles/links/runs exit 0; baked into
  the permanent probe regression.
  (4) residuals fixed: banner decision 6 names the three-lowering
  layout authority; flag illegality unconditional contract-fail (not
  debug assert); StringByteAt = one of three layout lowerings;
  validate() returns (state, flags) — no stale local.
  Awaiting implementation GO.
- B5 review round 5 (2026-07-23, HOLD + maintainer override) — CLOSED:
  (1) **Deferred call resolution is now a TRANSACTION** (maintainer
  override: no non-generic-only shortcut, no narrow eager gate).  The
  `defer_infer_diag` attempt in `resolve_call_expr` snapshots the HIR
  node states (call, callee, args, kwarg values), journals every
  record channel (expression types, CallInfo, invoke-CallInfo, iface/
  ptr-to-ref coercions, instantiations, call resolutions) and buffers
  diagnostics; a successful COMPLETE resolution (non-Unknown, zero
  error diags) COMMITS by replaying all journals; an incomplete or
  failed attempt ROLLS BACK completely — no diagnostics, HIR rewrites,
  callsite metadata, expression types, coercions, or instantiations
  left behind — and the enclosing call retries with the parameter type
  exactly as pre-fix.  Journal wrappers forward *args/**kwargs
  verbatim (recorders are invoked with keyword args).  All four
  mandated pins in `test_nested_call_arg_defer_infer_regression.py`:
  non-generic success (FAILS pre-fix), argument-inferable generic
  success (`ident<T>` nested in interface-call args — FAILS pre-fix),
  expected-return-dependent generic retry (`take_opt(Optional::
  Some(3))`, qualified generic ctor — worked pre-fix, preserved),
  invalid-call diagnostic preservation on BOTH the free-call path
  (real `make_ptr` overload diag named, identical pre/post) and the
  interface-argument path (arg-mismatch diag preserved, compile always
  fails).  Pre/post-fix behavior verified empirically against a HEAD
  copy of call_resolver.py.  Note: the interface-method arg path has
  NO expected-type retry (pre-existing v1 gap, unchanged); unqualified
  `Some(3)` in free-call args is a deliberate v1 E-CTOR-EXPECTED-TYPE
  rejection (defer flag is HQualifiedMember-only there, pre-existing).
  (2) checkpoint §3.3.1 second moved-scope occurrence fixed: both
  with_cstring_scope signatures now `core.Fn(Throw)1<&mut
  CStringScope, T>`, helper-retains-ownership comment.
  (3) probe regression completed: header rewritten (transactional fix,
  &mut scope forms, no stale workaround note), `OwnedCBytesProbe.get()`
  added + exercised, checked-nothrow arities 2..4 instantiated in
  main; checkpoint §3.2 arity claim now "ALL arities 1..4" for both
  checked families; §3.2 fix paragraph rewritten to the transactional
  design.  Probe test green (compile+link+run exit 0).
  (4) round-3 PROGRESS statement annotated SUPERSEDED (checker finding
  reclassified LANGUAGE_BUG, fixed on-branch; no separate slice).
  [Record correction (round 6): "5/5" = the regression file's FOUR
  pytest tests plus the separate B5 probe regression, run together.]
  [SUPERSEDED (round 6): the snapshot/journal implementation described
  in item (1) above was rejected as not a real transaction — replaced
  by the owner-level design below.]
- B5 review round 6 (2026-07-23, HOLD on checker fix only) — CLOSED:
  the manual snapshot/journal transaction was replaced per the binding
  correction ("put the transaction boundary where the complete checker
  state is owned … model three outcomes explicitly").
  **Design:** `CheckerStateTransaction` (type_checker.py) — the
  transaction boundary lives in `check_function`, which OWNS all
  per-function checker state as frame locals.  It captures the owner
  frame GENERICALLY (no manual channel list in the resolver): every
  dict/list/set local snapshotted by value (scope-stack List[Dict]
  copied one level deeper), every scalar local — including the
  `next_node_id`/`next_callsite_id` allocators (closure cells) —
  restored through the live PEP-667 `frame.f_locals` proxy (Python
  3.13), rebound container locals re-bound to the original object, and
  the probed HIR subtree deep-copied at begin with rollback swapping
  the pristine twin's state into the live root (same node_ids, so
  side-table keys keep matching).  Excluded by design: TypeTable
  interning (canonical/idempotent/content-addressed — inert if
  unused).  `check_function` hands the factory to the resolver via
  `CallResolverContext.begin_state_txn` (all three make_call_ctx
  sites; None → legacy silent bail).
  **Three outcomes, modeled explicitly** in the `defer_infer_diag`
  block of `resolve_call_expr`: COMPLETE (non-Unknown result, zero new
  error diags → live resolution stands, commit); NEEDS_EXPECTED (every
  new error matches the needs-expected class — prefix table
  `_DEFER_NEEDS_EXPECTED_PREFIXES` ("cannot infer ",
  "E-CTOR-EXPECTED-TYPE") — or silent Unknown, or an exception →
  ROLLBACK, defer, enclosing retry with the parameter type exactly as
  pre-fix); HARD_ERROR (errors regardless of expected type → live
  resolution + REAL diagnostics COMMITTED, node marked
  `_defer_probe_hard_error` so both `_propagate_arg_expected_types`
  retry sites skip it — diagnostic appears exactly once).
  Misclassification is safe one-way by construction: hard-as-needs
  merely rolls back and the retry re-emits with expected known.
  Diagnostic IMPROVEMENT: invalid nested calls in interface-method
  argument position now surface their real diagnostic (pre-fix: only
  the outer arg-mismatch); driver pin 4b strengthened to assert the
  real diag exactly once plus the mismatch.
  **Invariant tooth** `lang/tests/checker/test_defer_probe_state_
  transaction.py` (4 tests, in-process compiles with the transaction
  class monkeypatch-audited): (a) NEEDS_EXPECTED rollback restores
  EXACT frame-state fingerprint + whole-body HIR structural identity,
  and the retry emits the single real diagnostic (probe copy rolled
  back — no duplicate, no swallow); (b) fingerprint scope guard —
  must contain next_node_id/next_callsite_id/expr_types/diagnostics/
  iface_coercions (catches state hoisting out of the owner frame);
  (c) forced RuntimeError inside the probe recursion → exception
  rollback with identity + compile continues via retry, no ICE;
  (d) HARD_ERROR commits, diag exactly once.  Probe shape verified
  against pre-fix HEAD: `h.put(dflt())` (struct-method arg, T
  uninferable in v1) produces the IDENTICAL single diagnostic pre/post
  — and `h.take(Some(3))` unqualified-in-method-arg is a pre-existing
  v1 rejection on both, unchanged.
  **Verification:** regression 4/4 + probe 1/1; invariant tooth 4/4;
  checker-adjacent battery 589 green; stage2/core/stage3/stage4/
  modules/packages 1,107 green (3 skipped); driver+codegen battery
  RUNNING at return time (stale narrow-gate run stopped per
  maintainer).  Checkpoint §3.2 rewritten to the owner-level design.
  [SUPERSEDED (round 7): the frame-introspection transaction above was
  rejected — replaced by the explicit-owner design below.]
- B5 review round 7 (2026-07-23, HOLD on checker transaction) — CLOSED
  per the binding direction (explicit owner, overlay/undo log, staged
  HIR mutation log, structured outcomes, re-raise, corpus measurement):
  (1) **Explicit per-function checker-state owner**: `FnCheckState`
  (type_checker.py) owns the ELEVEN recorder side tables (expr_types,
  iface/borrowed-iface/ptr-to-ref coercions, call_resolutions,
  call_info_by_callsite_id, callsite_owner_node_id, instantiations_by_
  callsite_id/_node_id, fnptr_consts_by_node_id, diagnostics) as
  transaction-aware `_TxnDict`/`_TxnList` undo-log overlays ALIASED
  into check_function's locals, plus the three allocator cells
  (next_node_id, next_callsite_id — converted from closure cells —
  and the checker's _next_binding_id).  NO frame introspection in
  production.  `CheckerStateTxn` = watermark-scoped undo log (nested
  probes safe: inner commit retains entries for outer rollback) +
  saved allocator cells + an EXPLICIT per-node HIR mutation log for
  the probed subtree (attribute snapshots restored IN PLACE —
  descendant node identities preserved; no deep-copy root swap).
  (2) **Fail-closed shape gate** `_defer_probe_shape_safe`: explicit
  HIR-node allowlist; lambdas, match/block exprs, statements, and ANY
  unlisted (incl. future) node kind take the legacy silent deferral —
  this is what makes the owner's enumeration complete for probed
  shapes by construction (binding/scope tables deliberately not
  owned).
  (3) **Structured outcome classification** — no message parsing:
  diagnostic CODES set at emission (`E-CTOR-EXPECTED-TYPE`;
  `E-INFER-UNDERDETERMINED` from InferErrorKind.CANNOT_INFER at the
  free-call/struct/variant/assoc-fn sites; `E-INFER-EXPECTED-LITERAL`
  on the array/map-literal diags); inference CONFLICTS carry
  `E-INFER-CONFLICT` and are HARD.  Mixed needs-expected+hard rolls
  back (retry re-emits hard diags with expected known — no noise).
  (4) **Unexpected exceptions ROLL BACK AND RE-RAISE** (ICE
  containment); the tooth's exception test INVERTED to pin
  propagation after an identity-verified rollback.
  (5) **Invariant tooth rewritten** (5 tests): owner-fingerprint
  identity (full-VALUE dump — independent of the undo-log mechanism)
  + an INDEPENDENT raw frame-locals auditor + whole-body HIR dump
  across every rollback; fingerprint-scope guard (allocator cells +
  core tables must appear); exception rollback-then-reraise;
  hard-error commit with diag exactly once; direct fail-closed gate
  unit test (lambda / match-expr subtrees rejected).
  (6) **Corpus measurement (mandated)**: identical universe (924/1268)
  and ALL 14 counters +0 vs certified phase-d-final on BOTH branch
  runs (1,107,693 fns / 2,772,976 events).  Frequency: 102 probes per
  full stdlib compile, all COMPLETE (0 gated, 0 rollbacks in stdlib).
  Compile time, clean isolated runs: pre-fix HEAD 1503.9s wall /
  23,230.1s user vs owner-txn 1518.1s wall / 23,437.3s user =
  **+0.9% wall / +0.9% CPU** (an earlier +58% reading was concurrent-
  battery contention, not overhead; re-measured cleanly).
  **Process note:** the first driver+codegen battery of this round
  (2 failures) was INVALIDATED by the timing script swapping checker
  files to HEAD mid-run; the script was rewritten to run HEAD timing
  in an ISOLATED tree copy with a trap-based restore (live tree never
  swapped), the owner delta was restored and re-verified, and the
  clean battery passed 2,159 (1 skipped).
  **Verification (final, all against the committed-candidate tree):**
  defer regression 4/4 + B5 probe 1/1 + tooth 5/5; checker-adjacent
  590; stage2→packages 1,107 (3 skipped); driver+codegen 2,159
  (1 skipped, uncontaminated rerun).  Checkpoint §3.2 and both test
  docstrings rewritten to the explicit-owner design.
- B5 review round 8 (2026-07-23, four corrections before timing) —
  CLOSED:
  (1) **_TxnList transaction-complete**: every list mutator covered —
  append/extend keep cheap pop-based undo entries (hot path);
  slice/index deletes (`del diagnostics[start:]` at call_resolver
  for-in retargeting + two checker sites), item/slice assignment,
  insert/pop/remove/clear/sort/reverse record full-list snapshot
  restore entries.  Tooth: production slice-delete pattern interleaved
  with appends/inserts/pops rolls back to exact contents; nested inner
  commit + outer rollback reverts inner mutations.
  (2) **Shape gate on the canonical HIR predicate**: gate now uses
  `node_ids.default_should_descend` (H.HNode + every recognized HIR
  module incl. stage1.closures) instead of an hir_nodes-only module
  check — recognized-but-unapproved shapes are REJECTED, never
  skipped.  Tooth: HCapture closure metadata in a probed subtree is
  rejected.
  (3) **No wrapper escape into results**: TypedFn detaches all seven
  remaining owned _TxnDict tables via dict(...) (expr_types was
  already detached) and TypeCheckResult detaches diagnostics via
  list(...) — result objects no longer retain FnCheckState (which
  retains every table and the checker).  Tooth: constructor spies pin
  exact plain dict/list types during a real compile.
  (4) **Mixed policy = the STATED rule**: NEEDS_EXPECTED only when
  EVERY new error carries an expected-dependent code; a mixed
  expected-dependent + hard failure is HARD_ERROR and commits all its
  diagnostics (classifier flipped any→all; contract comment updated).
  Tooth: synthesized mixed probe (real E-INFER-UNDERDETERMINED +
  injected hard companion) takes the HARD commit, both diagnostics
  survive exactly once, no retry duplication.
  Cleanups: unused `copy` import removed (deepcopy died with the
  frame-based design); CallResolverContext.begin_state_txn comment
  rewritten from "frame locals" to the explicit-owner wording.
  **Verification (post-corrections, final):** tooth 9/9; defer
  regression 4/4 + B5 probe 1/1 + discard drop-timing 1/1; combined
  checker+stage batteries 1,701 (3 skipped); driver+codegen 2,159
  (1 skipped).  **Final corpus (corrected code, clean/isolated,
  sequential):** identical universe (924/1268), ALL 14 counters +0 vs
  certified phase-d-final; timing 1510.7s wall / 23,314.2s user vs
  pre-fix HEAD 1503.9s / 23,230.1s = **+0.45% wall / +0.36% CPU**.
- B5 review round 9 (2026-07-23, guardrail HOLD, contract-only) —
  CLOSED: (1) `_defer_probe_shape_safe` dropped its `is_dataclass`
  prefilter — `default_should_descend` is now the ONLY filter, so a
  NON-dataclass H.HNode subclass (canonically recognized) is rejected
  instead of silently accepted; tooth constructs a synthetic
  non-dataclass HExpr (with honest is_dataclass/predicate
  preconditions) and pins rejection.  (2) Inherited-mutator gaps
  closed: `_TxnList.__imul__` (snapshot-logged), `_TxnDict.__ior__`
  (routes through logged update) and `_TxnDict.popitem`
  (snapshot-logged); tooth pins exact rollback for `*=`, `|=`, and
  popitem.  Per maintainer: focused teeth only — no corpus/timing/
  broad-battery rerun; work stays on the combined 0.33.88/ABI-22
  branch, no standalone certification.  Tooth 11/11; defer regression
  4/4 re-verified.
- Examples/style sweep (2026-07-23, maintainer directive, folded in):
  `examples/tcp_echo/main.drift` rewritten idiomatic one-shot
  (echo_once helper, bare-statement discards, no or_throw in throws
  code, SIGIL-FREE call sites for &T and &mut T per signatures,
  nothrow main boundary, one-client naming/comments, `return
  server.join()` propagating server status) — compiles+links+runs
  exit 0, every idiomatic form works, NO LANGUAGE_BUG.  16 clear
  `val _ =` call-discard sites → bare call statements across
  examples/ (logging×3, blocking_ffi×2, tcp_client_server write,
  runtime_registry, udp_ping×2, cli×5, file_io×2); all touched
  examples recompiled, runnable ones re-run clean.  Rule-4 keeps:
  `val _ = conc.spawn_cb(...)` (ownership-bearing VT handle),
  `val _ = self`, `val _ = move guard`.  doc/effective-drift.md:
  discard sites updated, UDP snippet synced, duplicate TCP example
  replaced with the new source, NEW "Discarding call results" section
  (bare-call idiom + immediate-drop vs scope-exit lifetime caveat),
  and the stale "&mut needs an explicit sigil" paragraph corrected
  (contradicted by the directive and the compiling example);
  doc/design/drift-concurrency.md sleep-loop snippet updated;
  doc/history.md untouched (frozen).  NEW focused regression
  `lang/tests/driver/test_bare_statement_discard_drop_timing.py`
  (immediate-drop contract had no coverage): pins destroy:bare →
  after-bare → after-bound → destroy:bound — green.
- **GATES PASS (2026-07-22)**: 924 corpus audit `build/tmp/s7s8` vs
  accepted baseline `build/tmp/s5s6` — exit 0, universe identical
  (924/1268, same partition), **all 14 counters +0** (incl.
  drop_before_overwrite_site4=14, overwrite_release=233,519,
  scope_exit_release=68,562, materialized_lastuse_release=618,744,
  pre_post_verdict_drift=48,178).  Full memcheck: **105 passed /
  1 skipped, 0 leaks**, lane audit PASS — identical to the S5+S6
  gate.  Chunk complete.  (The then-next steps — S9 review and Phase D
  — are COMPLETE; see the Phase D combined sweep section below.)
- B5 IMPLEMENTATION (2026-07-23, GO on string-brepr-b5-abi22; recovery
  base 58d5a105+f9d653cb) — step log:
  S1 RUNTIME REWRITE COMPLETE: string_runtime.{h,c} → DriftRcBytes
  header-at-offset-0 (16B, _Static_assert battery), two-word handle
  {len, storage}, flags STATIC/IMMORTAL/NUL_SCANNED/HAS_INTERIOR_NUL
  (+reserved-mask), drift_contract_fail (unconditional both builds),
  __drift_rt_string_empty hidden singleton (IMMORTAL|NUL_SCANNED),
  validate() tombstone/malformed prologue in EVERY helper (observation
  fails closed; release-family no-op on exact {0,NULL}), retain
  fail-closed + >= DRIFT_RC_MAX_LIVE overflow guard, §2.5 constructor
  edge table (NULL/negative/overflow abort; len==0 → singleton; concat
  subtraction-form guard), accessors drift_string_len/data, §3.3 C
  bridge (interior_nul_index with monotonic relaxed fetch_or cache /
  to_owned_cstr / to_owned_cbytes / frees).  from_bool constants →
  IMMORTAL.  Standalone smoke green in normal AND NDEBUG builds.
  S2 CONSUMER MIGRATION COMPLETE: console/array/assert/thread member
  reads → accessors (compiler-proof: full runtime C compiles clean);
  env_get/fs_result_name absent-returns → empty singleton (stdlib
  guards via has/count; {0,NULL} never returned to Drift);
  array_runtime event-fqn handle fabrication → allocated.
  S3 CODEGEN COMPLETE: literal emitters → {strong 1, flags 5|13,
  bytes} with GEP-to-header field 0 + compile-time flag computation;
  "" → {0, @__drift_rt_string_empty} (external hidden decl);
  StringByteAt +16 bytes base; NEW StringBytesBase MIR op + lowering
  (borrow, no retain) = third layout-authority lowering; codegen unit
  goldens regenerated (flags 1→5), 69/69.
  S4 VERSIONS STAMPED: 0.33.88 / ABI 22 (coherent tree; link stamp
  auto-flips; first e2e binary linked against libdrift_rt_abi22.a).
  S5 std.ffi COMPLETE (§10 SHIP set, nothing narrowed): CStringError,
  with_bytes(+throw), with_cstr1-4(+throw, +unsafe 1-4) with
  LEFT-TO-RIGHT (ordinal,index) reporting via zero-copy hidden-NUL
  borrow, OwnedCStr/OwnedCBytes/ReleasedCBytes (+get/release/Drop),
  CStringScope (cstr/cstr_unsafe/argv→CArgv) with helper-retained
  &mut scope; string_bytes_base checker intercept (std.ffi-gated) +
  IntrinsicKind + call_contract row + hir_to_mir + ownership-uses row;
  4 bridge intrinsics via std.ffi-module codegen arms + declares.
  END-TO-END SMOKE GREEN (ffi-ok): byte-scan sum, zero-copy cstr,
  InteriorNul(1,1) on "a\x00b" literal (compile-time flags 13),
  owned handoffs, scope argv.  Findings: multi-line export block with
  trailing comma parses as expr-block (worked around, single-line);
  expr-form match arms can't return (nested-match form used);
  inline callback2 under explicit-generic call infers R=Void
  (pre-existing inference shape — typed-binding style used, as the
  probes always did).
  S6 TEETH COMPLETE (all green):
  - test_b5_string_representation.py: C-level battery compiled in BOTH
    normal and NDEBUG runtimes — positives (singleton identity across
    ""/empty-concat, hidden NUL for every ctor, bool-immortals,
    NUL-cache monotonic publish + cached re-query, owned copies,
    tombstone drop-only no-op) + 21 abort teeth (all tombstone
    observations incl. retain, malformed {len!=0,NULL}/negative even
    in release, STATIC+IMMORTAL / orphan HAS_INTERIOR_NUL / reserved
    bit, NULL/negative/overflow ctor edges, concat overflow, refcount
    overflow, release underflow) with [drift:contract] message pins.
  - test_string_layout_audit.py (4): layout knowledge confined to
    string_runtime.{h,c} + exactly three codegen lowerings (+16 GEP
    count == 2, singleton refs == 3 in _emit_empty_singleton_handle,
    centralized _string_literal_flags, retired field-2 GEP banned);
    member-read audit (caught + fixed two stale comments);
    lang/driftc representation-blindness pin.
  - test_b5_ffi_api_teeth.py: left-to-right (arg,index) = 1101/1210/
    1401 pins, zero-copy canonical-empty cstr, escape-compiles pin,
    OwnedCStr release-then-drop, OwnedCBytes drop-only + interior-NUL
    view, scope argv element-ordinal 1202 + pin counting.
  - test_string_bytes_base_intrinsic.py (3): IR pin base-once +16 GEP
    + NO drift_string_retain in with_bytes body; rejected outside
    std.ffi; arity/type misuse rejected via stdlib-overlay mutation.
  - test_abi_version_stamp.py::test_abi_mismatch_bidirectional_21_22:
    ABI-21 object × ABI-22 runtime AND ABI-22 object × ABI-21 runtime
    (stamp-swapped archive facsimile) both fail at link naming the
    respective symbol; driver-hint predicate fires.
  - IR-only e2e harness (test_driftc_codegen_e2e.py) now links the
    string runtime — every module with an empty literal references
    the singleton symbol (new hard runtime dependency, by design);
    8/8.
  - doc/history.md: 0.33.88/ABI-22 entry — representation-only, **no
    valid-source semantic change**, with the approved invalid-state
    hardening named in exactly those terms.
  S7 UNACCOUNTED IN-TREE CONSUMER FOUND + MIGRATED (driver battery
  16 failures): lang/compiler_infra/{error_dummy,diagnostic_runtime}
  — OUTSIDE the checkpoint's §4 "15 runtime files" census —
  diagnostic_runtime.h carried a GUARDED DUPLICATE old-shape
  `DriftString {len, char* data}`; error_dummy.c (the real DriftError
  runtime) compiled against it and read ABI-22 header bytes as string
  data (exception event_fqn envelope corruption).  Fixed: the
  duplicate definition RETIRED (header now includes the real
  string_runtime.h — single layout authority), error_dummy.c migrated
  to accessors + empty-singleton returns ({0,NULL} absent-returns
  eliminated; post-release field zero-backs remain legal drop-only
  tombstones), phase-1 C ownership test migrated (storage->strong
  whitebox peek + accessor eq).  reactor whitebox stubs
  drift_contract_fail.  IR-only harnesses (codegen_e2e, void_e2e)
  link the string runtime; external-consumer harnesses link a
  MINIMAL singleton shim built from the real header (their IR carries
  own string stubs); IR symbol audit counts external GLOBAL
  declarations.  string_runtime validate() got its owned-string-audit
  read-only-borrow marker.
  S8 GATES (in-tree candidate): e2e fixture corpus rc=0; driver+
  codegen 2,163 passed/10 skipped (0 failed after S7); compiler-side
  batteries 1,711 passed/3 skipped; om 51/51 + asan lane + pkgb lane
  ok; phase-1 valgrind wrapper green.  PERF GATE (§2.8/§8.6): parser-
  shaped byte-scan carrier (256KiB, 1500 passes, interleaved 7×) —
  0.33.87 median 1.230s vs B5 median 1.170s = **-4.9% (faster);
  min -9.8%**; outputs identical.  NO regression.
  S9 RELEASE-BLOCKING HOLD (4 findings) — CORRECTED:
  (1) COMPILER observation contract: new `__drift_string_observe_guard`
  (internal alwaysinline IR fn, emitted once per module) wired into
  ALL THREE layout-authority observation lowerings (StringLen /
  StringByteAt / StringBytesBase) — tombstone, {len!=0,NULL}, and
  negative-len handles fail closed BEFORE any length/storage use;
  byte_length can no longer read a tombstone as 0 and with_bytes can
  no longer receive NULL+16.  Teeth: link-driven observer battery
  (Drift-compiled observers × C-fabricated handles × both runtime
  builds) — 3 lowerings × {tombstone, malformed, negative} abort with
  pinned messages; valid handles pass through — 2/2.
  (2) Allocator pairing: new `drift_string_to_owned_cstr_unchecked`
  (cstr-paired) for scope.cstr_unsafe; OwnedCBytes destroy/free now
  via `ffi_cbytes_free(ptr, len)` -> `drift_cbytes_free` (the PAIRED
  deallocator, struct rebuilt at the call); pub `cstr_free` /
  `cbytes_free` added (SHIP-table "Drop/free/release" rows) so the
  pairing holds end-to-end from Drift; all pairing comments corrected.
  (3) Layout audit extended: production roots now language_runtime +
  posix + compiler_infra (tests/ + lang-obsolete excluded explicitly);
  duplicate `struct DriftString`/`DriftRcBytes` definitions forbidden
  (exactly one, in string_runtime.h); member-read scan covers pointer
  form (s->len/s->data); caught + fixed one stale comment — 5/5.
  (4) API teeth de-overclaimed and strengthened: OwnedCStr release ->
  REAL `cstr_free`; OwnedCBytes release path exercised + `cbytes_free`;
  owned-copy isolation ACTUALLY MUTATES the released copy and proves
  the source unchanged; NEW memcheck-lane
  test_b5_owned_types_lifecycle.py (drop-only + release-then-free +
  release-then-drop for both owned types + full scope pin/argv
  lifecycle) — valgrind clean, 0 leaks/invalid accesses.
  Informational corpus run during the fixes showed a 16-fixture
  std_text universe mismatch — verified TRANSIENT (mid-edit
  contamination; fixtures compile+run clean on the corrected tree);
  the authoritative corpus rerun is gated on the static delta review.
  Affected-gate rerun on the corrected tree: 89/89 (codegen units,
  representation battery both builds, API teeth, layout audit 5/5,
  bytes-base boundary, defer pins, probes, observation guards).
  PERF GATE RERUN (guards active): 0.33.87 median 1.230s vs B5 median
  1.240s = +0.81% median / -4.88% min — WITHIN NOISE (B5's own spread
  ±6%); §8.6 PASS.  Corpus + run-all.sh + certification held for the
  static delta review per maintainer instruction.
  S10 HOLD (2 contract blockers + 2 guardrails) — CORRECTED:
  (1) ILLEGAL FLAGS now fail closed at OBSERVATION in both paths:
  C accessors share a `drift_string_observe_validate` prologue
  (tombstone/malformed + reserved-bit + STATIC+IMMORTAL + orphan
  HAS_INTERIOR_NUL, relaxed flags load); the IR observe guard gained
  the same three flag checks (flags word loaded atomically at storage
  +8 — recorded as part of the codegen layout authority and pinned by
  the audit's exactly-one (+8) count).  Teeth: representation battery
  +3 accessor flag cases; observation-guard battery +3 flag handles ×
  3 lowerings × both builds — 6/6.
  (2) Free APIs re-marked `pub unsafe fn` with strict exactly-once
  transferred-allocation provenance docs (never borrowed pointers,
  never live scope pins — the scope frees its own; ReleasedCBytes is
  a Copy VIEW, freeing through >1 copy double-frees); the misleading
  scope-pin mention removed.
  (3) Layout audit hardened: EXACT-path allowlist (basename shadowing
  caught by a negative tooth), TYPE-level `DriftRcBytes` ban (catches
  aliased ->flags), member scan covers len/data/STORAGE in value AND
  pointer form, duplicate-definition pin is authoritative-PATH-based;
  4 negative self-teeth prove the scanners bite — 9/9.
  (4) `drift_string_to_owned_cstr_unchecked` recorded as an
  implementation-time bridge amendment in checkpoint §3.3 and the
  history entry (with the pairing rationale).
  PERF RERUN (flag-validating guards active): 0.33.87 median 1.230s
  vs B5 median 1.240s = +0.81% median / +1.72% min, distributions
  overlapping (0.33.87 spans 1.16-1.29, B5 1.18-1.25) — WITHIN NOISE,
  §8.6 PASS.  Affected-gate sweep on corrected tree: 117/117 (codegen
  units, representation battery, API teeth, hardened audit, bytes-base
  boundary, defer pins, probes, observation guards, lifecycle+phase1
  memcheck, ABI-stamp suite incl. bidirectional mismatch).
  Corpus + run-all.sh + certification awaiting static delta clearance.
- Ownership-corpus certification infrastructure (2026-07-24, maintainer
  directive; TEST INFRA ONLY — no compiler-version/ABI change):
  (1) `tools/drift_corpus_audit.py` gains `--require-zero-delta`
  (needs --baseline): certification mode failing closed on missing
  counter keys, unexpected new keys, and ANY nonzero delta, on top of
  the existing identical-universe (exit 2) and hard-gate (exit 1)
  checks; missing/corrupt baseline or run data now exits 2 with a
  BASELINE/RUN DATA ERROR instead of a traceback.
  (2) Checked-in certified baseline
  `lang/tests/ownership_corpus/certified-baseline/` — aggregate.json +
  manifest.json + metadata.json copied from the phase-d-final run
  produced ON CERTIFIED 0.33.87/ABI-21 (commit 3d48b7f0, tool v1.6.0,
  run 2026-07-22T20:12:28Z; NEVER from the B5 candidate), with BASELINE.md pinning
  provenance, the generation command, the reviewed-update-only policy
  (certification never regenerates/blesses automatically), and the
  matrix-vs-corpus distinction.
  (3) justfile: `ownership-corpus-check` (fresh timestamped
  build/tmp run dir, retained on failure; compares against the
  checked-in baseline in zero-delta mode) — kept OUT of `just test`
  (run-all.sh runs test twice; corpus runs exactly once) — and the NEW
  `certify` entrypoint = ownership-corpus-check once.  [AMENDED per
  maintainer wiring correction: certify is an INDEPENDENT workflow
  that never invokes run-all.sh; run-all.sh (the private pre-handoff
  runner) itself runs the corpus once before its memcheck/ASAN `just
  test` passes; wiring teeth prove one-corpus-in-run-all,
  one-corpus-in-certify, no-run-all-in-certify, no-corpus-in-test.]
  (4) Negative teeth `lang/tests/tools/test_ownership_corpus_check.py`
  (15, incl. the S-review malformed-universe/non-integer-counter/
  baseline-side-schema/inclusion-rule+excluded-record additions): synthetic-run comparisons proving universe drift (2), nonzero
  delta (1, and that plain --baseline alone does NOT fail it — the
  documented policy gap), missing/unexpected counter keys (1), hard
  gates (1), corrupt/missing baseline (2), flag-requires-baseline; plus
  checked-in-baseline sanity (gates zero, 924 universe, provenance
  strings) and justfile wiring pins (corpus once in certify, never in
  test; matrix stays in test).  Existing tool tests unaffected (tools
  battery 22/22).  Awaiting static review; per directive the expensive
  corpus was NOT rerun for synthetic cases.
- Docs-only release closure (2026-07-24, maintainer review of
  user-facing documentation; NO code/ABI change):
  (1) README String-runtime link fixed (was pointing at a nonexistent
  doc/design/drift-string-impl.md; now the real
  spec-change-requests/ path, retitled to the RcBytes representation).
  (2) doc/design/spec-change-requests/drift-string-impl.md REPLACED:
  the retired unique-owned {len, char* data}/direct-free()/per-literal
  -data-pointer page is now the ABI-22 narrative companion to
  string_runtime.h (layout, flags, ownership protocol, canonical
  empty, access rules, PAIRED C-string bridge, literals) — tombstone
  material confined to the C-integrator section, explicitly marked
  unreachable from Drift source.
  (3) doc/design/drift-lang-abi.md: new "String ABI (current)" section
  (two-word handle + pointer to the design page/header + link-stamp
  gating) and a currency note marking the ABI-14 DV-migration block as
  historical.
  (4) doc/effective-drift.md: NEW "C interop for String (std.ffi)"
  section — worked examples for with_bytes (borrowed window +
  escape-is-invalid), checked with_cstr + InteriorNul(arg,index)
  handling, owned handoff with release + exactly-once PAIRED unsafe
  frees, CStringScope argv, and the no-view/spans/offsets performance
  posture.  No tombstone mention (not a user-visible state).  Example
  shapes COMPILE-VERIFIED end-to-end (scratch adaptation runs exit 0).
  (5) Allocator-pairing language corrected everywhere "free-
  compatible" appeared (ffi.drift module doc + OwnedCStr doc,
  string_runtime.h §3.3 comment): pairing is CONTRACTUAL —
  drift_cstr_free/drift_cbytes_free only, never raw free().
  (6) std.text.substring: ALLOCATES disclosure + spans/offsets/
  with_bytes guidance for performance-sensitive parsers.
  FINDING escalated to LANGUAGE_BUG and FIXED in the follow-on slice
  below (the doc keeps the direct trailing-match form; no workaround
  ships): match/try expressions as lambda trailing bodies ICE'd in
  MIR lowering.

## 2026-07-24 — LANGUAGE_BUG slice: match/try as lambda trailing expression (regression-first, rides 0.33.88/ABI-22 candidate)

- CLASSIFICATION: LANGUAGE_BUG (maintainer directive). Regression-first
  contained slice on the stamped 0.33.88 candidate; NO separate
  certification; ABI stays 22 (pure compiler-internal AST→HIR fix).
- SYMPTOM: a value-producing `match` (or `try..catch`) used DIRECTLY as
  a lambda's trailing expression ICE'd in HIR→MIR for EVERY lambda:
  "MIR lowering contract failure (value-producing [Bool] match arm must
  yield a value or terminate (checker bug))" — variant E-AUTO-a16b07f1,
  Bool E-AUTO-23cd2496.  Two prior sightings: Bool match in the B5
  observation-guard teeth, variant match in the effective-drift
  CStringScope example (temporarily worked around with bind-then-yield;
  workaround now REMOVED per directive).
- ROOT CAUSE (subsystem: stage1 AST→HIR, `ast_to_hir.py::
  _visit_expr_Lambda`): the parser classifies a lambda-tail match/try
  via the EXPRESSION-form productions (arms must end with a value), but
  the lambda body conversion routed every body statement through the
  generic statement visitor, whose ExprStmt arm lowers match with
  `value_context=False` → `HMatchArm.result=None` → HIR→MIR value path
  asserts.  Named-fn tails never hit it (return/val forms take the
  value-context path).  doc/refactor_triggers.md scanned: NO registered
  trigger matches this failure family.
- FIX (one authority): ExprStmt lowering unified into
  `_lower_expr_stmt(stmt, *, value_context)` — the ordinary statement
  visitor calls it with False, the lambda-tail conversion calls it with
  True for EVERY trailing ExprStmt (no per-shape branches at call
  sites); the authority routes match/try payloads to their expression
  lowerings with the position's value_context.  Void lambdas
  unaffected — HIR→MIR's lambda tail statement path
  evaluates-and-discards arm results.
- REGRESSION: lang/tests/driver/test_lambda_trailing_match_value.py
  (3 tests — the positive program is full compile-AND-run; the two
  negatives are compile-and-REJECT): 6 positive pins (Bool sole-tail,
  Bool after statements, variant call-result scrutinee, variant param
  scrutinee, try/catch tail, named-fn behavior parity) + negative
  companions with EXACT-diagnostic pins: `return` in an
  expression-form arm rejects with EXACTLY E_EXPECTED_SEMICOLON (the
  match-as-value message; never the ICE), and the PRE-EXISTING
  unannotated-lambda Void inference is unchanged, surfacing as EXACTLY
  the use-site arithmetic mismatch E-AUTO-5a90687a (Void vs Int) —
  separate, deliberately untouched limitation.
- DOCS: doc/effective-drift.md CStringScope example restored to the
  direct trailing-match form; that EXACT example compile-proven and run
  (scratch adaptation exit 0).
- STATUS: fix + regression + doc restoration DONE; focused gates next,
  then full pre-mainline `just test` (production lowering changed).
- REVIEW ROUND 1 (3 blocking corrections, all applied): (1) ExprStmt
  lowering refactored into the single `_lower_expr_stmt` authority
  (the earlier duplicated MatchExpr/TryCatchExpr branches in the
  lambda path removed); (2) both negative tests now pin their EXACT
  diagnostics (E_EXPECTED_SEMICOLON + "no implicit return" guidance;
  E-AUTO-5a90687a "Void vs Int"), not either-of-two codes / bare
  "Void" containment; (3) compile-and-run overclaim corrected here and
  in the test header — only the positive program runs; negatives are
  compile-and-reject.  In-flight full suite STOPPED (invalidated by
  the authority refactor); focused regression rerun, then the full
  suite ONCE.

## 2026-07-24 — Corpus gate on candidate: EXPECTED failure, delta fully attributed (NOT a regression)

- run-all-tests.sh corpus stage (run dir ownership-corpus-20260724-083631-1146539,
  retained) failed --require-zero-delta vs the certified-0.33.87 baseline:
  fns +24024, events/c3_moveout_owned/site_class:moveout_expansion +18480.
  Universe IDENTICAL (924/344/49 partition + hashes), all hard gates zero,
  every other counter +0.
- ATTRIBUTION (verified via DRIFT_STRING_ARC_AUDIT_VERBOSE on a minimal
  main): the NEW std.ffi module — absent from the certified 0.33.87
  baseline tree by construction — contributes exactly 26 fns and
  20 events (all c3_moveout_owned/moveout_expansion) per compile;
  26×924=24024, 20×924=18480.  Residual after attribution: ZERO on
  every counter.  The lambda-tail LANGUAGE_BUG fix contributes nothing.
- PROCESS: baseline is NEVER auto-blessed.  Pre-mainline verification
  proceeds with the two-mode full suite directly (corpus already ran
  once, outcome attributed).  Baseline re-bless happens ONLY at
  certification of the combined String phase: regenerate from the
  newly certified tree, update BASELINE.md provenance, deliberate
  check-in.  Until then candidate corpus runs are expected to fail
  with EXACTLY this delta; any different delta needs fresh attribution.

## 2026-07-24 — Full-suite catch: statement-form lambda-tail match regression, fixed at the parser classification

- test_reload_coordinator (full suite, maintainer run) FAILED post-fix:
  E-MATCH-NO-VALUE on a lambda whose TRAILING statement is a
  STATEMENT-form match (plain block arms, all exiting via `return`,
  with a nested statement-form match).  The lambda-tail authority was
  forcing value_context=True onto it, misreading statement arms as
  valueless results.
- ROOT CAUSE (one level deeper than the original bug): the grammar has
  TWO productions — match_expr (value_block arms) vs match_stmt (block
  arms) — but parser.py's match_stmt builder wrapped BOTH into the
  same ExprStmt(MatchExpr), ERASING the classification the original
  fix claimed to align with.
- FIX: `MatchExpr.statement_form` recorded by the parser
  (parser/ast.py + _build_match_expr + match_stmt builder), carried
  through the stage0 conversion (parser/__init__.py, stage0/ast.py),
  and honored by `_lower_expr_stmt`: a statement-form match NEVER
  takes value context, even at a lambda tail.  Statement-form `try`
  is a distinct TryStmt node — no flag needed, verified.
- REGRESSION: positive pin 7 added to
  test_lambda_trailing_match_value.py — statement-form tail match
  (arms return) incl. nested statement-form inner, full
  compile-and-run on three inputs; test_reload_coordinator green.
- Focused gates rerun next; the full suite remains the maintainer's.
- REVIEW ROUND 2 (3 corrections, all applied): (1) the grammar
  production is now the SOLE authority — `_build_match_expr` derives
  statement_form (and arm node names) from `_name(tree)` and raises on
  any unexpected production; the caller-supplied boolean with a
  silently-misclassifying default is gone.  (2) compatibility
  fallbacks removed: parser/__init__.py conversion and
  ast_to_hir._lower_expr_stmt read `.statement_form` directly — a
  missing classification now fails loudly (AttributeError), never
  silently becomes expression-form.  (3) NEW boundary pin
  lang/tests/parser/test_parser_match_statement_form.py: match_expr →
  False, match_stmt → True, stage0 conversion preserves both;
  coordinator-shaped compile/run pin retained as the e2e companion.
  Focused gates: parser+stage1+lambda/match battery 296/296.

## 2026-07-24 — Baseline PROMOTION (maintainer-approved) + runner order reverted

- run-all-tests.sh REVERTED to the original fail-fast order (corpus →
  memcheck → ASAN) per maintainer directive; the earlier reorder was
  not authorized.  No failure-tolerance anywhere: a matching tree now
  yields genuine exit 0 at every stage.
- BASELINE PROMOTED per explicit maintainer approval of EXACTLY the
  2026-07-24 14:45 run's output: retained artifacts from
  build/tmp/ownership-corpus-20260724-144528-1377082 copied into
  lang/tests/ownership_corpus/certified-baseline/ WITHOUT a corpus
  rerun, after byte-exact validation against the approved figures
  (fns +24024; events/c3_moveout_owned/moveout_expansion +18480; all
  other counters +0; universe identical 924/344/49; hard gates zero).
- BASELINE.md rewritten: new provenance (0.33.88/ABI 22, commit
  b2caeb44, tool 1.7.1, 2026-07-24T20:45:28Z) + a Promotion record
  documenting the predecessor (certified 0.33.87/ABI 21/3d48b7f0) and
  the approved delta with its std.ffi attribution.  Update policy and
  matrix-vs-corpus sections unchanged; strict zero-delta gate
  UNCHANGED against the new baseline.
- VERIFIED: corpus teeth 16/16; _compare(promoted baseline, retained
  run, require_zero_delta) exits 0 — the next run-all-tests.sh corpus
  stage passes genuinely on a matching tree.
- RENAME (pre-commit cleanup, maintainer directive): the baseline dir
  is an uncertified reviewed mainline candidate now, so
  lang/tests/ownership_corpus/certified-baseline/ →
  lang/tests/ownership_corpus/reviewed-baseline/.  Updated: justfile
  (recipe path + comments), tools/drift_corpus_audit.py docstring,
  test_ownership_corpus_check.py (BASELINE path, docstrings,
  test_certified_baseline_is_complete_and_clean →
  test_reviewed_baseline_is_complete_and_clean, needle list now pins
  BOTH the promoted 0.33.88/ABI 22/b2caeb44 provenance AND the
  historical 0.33.87/ABI 21/3d48b7f0 predecessor), BASELINE.md
  generation-command example.  Repo sweep: "certified baseline"
  survives only in the BASELINE.md predecessor/promotion record and
  this ledger's historical entries.  Teeth 16/16 post-rename.

## 2026-07-24 — string-view-performance phase OPENED: design checkpoint written, at STOP

- Branch string-view-performance from mainlined B5 (base 1d92e0b6);
  extends the 0.33.88/ABI-22 candidate; ONE combined certification at
  phase end.
- REPORT-ONLY checkpoint written:
  work/string-ownership-refactor/STRING-VIEW-PERFORMANCE-CHECKPOINT.md
  (ends at STOP).  Inputs: full surface/consumer re-audit
  (string_byte_at, with_bytes, SourceCursor, std.json spans +
  allocation hot spots, parse_*_bytes, StringBytesIter, substring's 3
  production callers, regex/codec) + a MEASURED 5-variant
  parser-shaped benchmark (2MB / 279,653 tokens, medians):
  byte_at scan 2199us; with_bytes base-once 419us (5.2x); substring
  materialization 28987us (~96ns/token); per-token view prototype
  13947us (~42ns/token construct+destroy, 2.1x better than substring,
  zero alloc); reused-view reads 2350us (byte_at+7% — reads don't
  retain).  Copy probe: String-bearing struct is NOT Copy
  (E-AUTO-e8f17b8b) → views are move-only, dup() explicit.
- Headline design: std.text StringByteView {backing String, start,
  len} (honest byte-range name; StringView reserved for future
  UTF-8-validated views); reads via composed string_byte_at (no new
  intrinsic); bulk ffi.with_view_bytes for the 5.2x path; to_string()
  the only allocator; C interop copies (no zero-copy C-string
  promise); ABI stays 22 — pure stdlib value, no compiler boundary
  change.  Adoption in-chunk: json _parse_string/_parse_number,
  SourceCursor.slice_view, split_views, parse_int/uint_view,
  json-pointer segmentation.
- NEXT: maintainer review of the checkpoint; implementation only
  after clearance.
- CHECKPOINT REV 2 (8 blocking review corrections folded in, still at
  STOP): (1) bulk window moved to std.text composing through
  ffi.with_bytes — PROBED: generic signature compiles+runs
  (captures(move body, copy start, copy vlen)), and measured w6 =
  405us vs w2 419us — base-once tier survives composition; (2) three
  performance tiers stated honestly (safe byte_at ≈ guarded path;
  bulk window = base-once but unsafe+callback-scoped; view solves
  storage/lifetime, not safe max speed); (3) public API CLOSED:
  TextError reused, methods-on-view style, both view/view and
  view/String search forms with view-relative indexes + existing
  empty-needle semantics, iteration CONSUMES the view (dup() for
  both), subtraction-form bounds, offset=start on construction
  failures, container id std.text:StringByteView; (4) allocation
  claims corrected (split_views allocates its array + one retain per
  element; to_string qualified); (5) adoption list corrected:
  _parse_string reclassified as adjacent non-view work (owned String
  required), _parse_number = validation + one REQUIRED
  materialization, JSON-pointer migration DROPPED (HashMap
  heterogeneous lookup out of scope), JsonDoc.span_view added as the
  flagship genuine adoption; (6) parse_int_bytes offset discrepancy
  VERIFIED in code (doc says relative-to-start, impl returns
  absolute) — pinned: documented contract wins, regression-first fix
  before parse_*_view; (7) acceptance now requires EXACT
  retain/alloc-count proofs via -Wl,--wrap counting shim (B5
  custom-link technique) + IR call accounting; (8) corpus wording
  aligned with the promotion policy (measure→attribute→review→
  approve→promote BEFORE final runner/cert).
- CHECKPOINT REV 3 (second review round, 5 corrections, still at
  STOP): (1) window-size sweep MEASURED (bench2: safe reads vs
  composed-bulk-per-window at 8B..2MB): per-window fixed cost ~54ns,
  crossover ~64B — per-token bulk is 7.1x WORSE at 8B, 2-10x better
  >=128B; pinned API guidance: per-token bulk windows are an
  antipattern, safe byte_at for token-sized work; (2) counting shim
  corrected to REAL wrappable symbols — drift_string_new_copy is
  static/unwrappable (string_runtime.c:150); wrap exported
  drift_string_retain/release + drift_string_from_utf8_bytes
  (to_string path) + drift_alloc_array (boxed-callback env; paired
  with drift_cb_env_free; marker-window discipline since arrays share
  it); forced-throw with_view_bytes_throw obligation added (no leaked
  retain/env across unwind); (3) search names reuse text vocabulary:
  index_of/index_of_view (text has index_of at :534, no `find`);
  shipping signatures for split_views / SourceCursor.slice_view /
  LocatedCursor.raw_view / JsonDoc.byte_range_view / parse_*_view
  pinned in §5 incl. split_views parity table (empty delimiter →
  per-byte views; absent → [whole]; empty input → [empty view];
  empty fields preserved); (4) JSON flagship = provenance-safe
  LocatedCursor.raw_view() (JsonByteSpan has no source identity —
  span_view rejected); byte_range_view explicitly numeric, delegates
  to text.byte_view with TextError (not JsonErrorData); (5) exact
  §6a offset table pinned (invalid-range → offset 0 positionless;
  syntax/sign/digit/overflow/underflow/invalid-datatype rows all
  relative) + REQUIRED 0.33.88 history record since callers may
  observe today's absolute offsets.
- CHECKPOINT §11 ADDED (regex integration, maintainer review
  addition; still at STOP): RegexMatch stays a cheap Copy span
  ({pub start, pub end}, no source identity) — every conversion is
  bounds-CHECKED, no provenance claim (fabricable by construction).
  Pinned: match_view(m, &String) and match_subview(m, &StringByteView)
  -> Result<StringByteView, text.TextError> (out-of-bounds
  offset=m.start; malformed end<start rejected); DECISION SHIP
  is_match_view/find_first_view — value is avoiding SUBJECT-substring
  materialization, offsets VIEW-RELATIVE, anchors bind to view
  boundaries; regex-shaped benchmark MEASURED (bench3): DFA proxy
  indexed 2519us vs one-bulk-window 744us (3.4x = read-bound ceiling)
  vs real engine 172703us (69x slower — per-byte NFA bookkeeping incl.
  a per-byte seeds Array alloc in _try_match_at; read path ~1.5% of
  engine time) → engine bulk conversion explicitly OUT OF SCOPE,
  engine per-byte alloc noted as future-phase opportunity; counting
  obligations extended (existing matching zero-retain regression;
  *_view matching zero-retain; match_view/match_subview exactly one
  retain); pins: lifetime beyond haystack binding, fabricated-match
  negative matrix (never panic/UB/ICE), empty match -> valid empty
  view/singleton, nested-view offset composition + byte-equality.
  std.regex already imports std.text — no new dependency.
- §11 CORRECTION ROUND CLOSED (rev 4) → IMPLEMENTATION GO: (1) real-
  engine bulk-read variant BUILT+MEASURED (bench4: verbatim NFA
  executor replica from exported Regex.root; stock 171.3ms, replica-
  byteat 169.0ms fidelity ±1.4%, replica-bulk 165.8ms → bulk reads
  worth 1.9%; engine conversion out of scope ON EVIDENCE; per-byte
  seeds alloc = separate unmeasured future opportunity); (2) ONE
  matcher authority pinned + compile-proven: range triple (s,&base,
  len) engine core; view entries via NEW std.text read-only accessors
  incl. borrow-returning backing_ref(&self)->&String — probe runs, IR
  shows ZERO retain/release/alloc in core/entry/accessor; STOP-not-
  clone contingency recorded; (3) signatures compile-real:
  text.StringByteView alias forms probe-verified (Result<text.T,
  text.E> runs), all four fns added to regex export block; (4) §1/§9
  contradictions fixed (zero-copy span results but NO retained
  backing / view input; regex IS integrated via §11); (5) lifetime/
  retain-count fixtures pinned HEAP-BACKED non-static (literals are
  STATIC/immortal — vacuous-pass hazard) — applies to all §10 count
  fixtures.  Maintainer disposition: conversions/offsets/anchors/
  fabricated-checks/RegexMatch-stays-Copy APPROVED; after these
  closures proceed to consolidated implementation, no further
  arm-selection review, one final certification.

## 2026-07-24 — IMPLEMENTATION step 1: std.text StringByteView foundation LANDED (tree, untested-by-suite)

- stdlib/std/text.drift: StringByteView {backing,start,len} private
  fields + STRING_BYTE_VIEW_CONTAINER_ID + byte_view/byte_view_all +
  implement block (byte_length/is_empty/byte_at[IndexError,
  view-relative]/subview/dup/eq_view/eq_string/starts_with(_view)/
  ends_with(_view)/index_of(_view)/to_string/backing_ref/start_offset/
  bytes) + ViewBytesIter + with_view_bytes(_throw) (composed through
  ffi.with_bytes, probed shape) + split_views (split field-structure
  parity, reuses _index_of_from).  Imports += std.err, std.ffi;
  exports += 8 entries.  Doc comments carry the measured tier guidance
  (per-token bulk antipattern, ~54ns/window, ~100B threshold).
- 31-check scratch smoke compile+run PASSES (bounds, throwing
  byte_at, subview/dup/eq/search, to_string+empty singleton,
  consuming iterator, authority accessors, bulk window, split_views
  parity x4, lifetime-beyond-binding) on heap-backed strings.
- Note: smoke initially hit E_EXPR_BLOCK_MISSING_VALUE on mixed
  value/return match arms — the EXISTING clear diagnostic working as
  designed (statement-form rewrite applied in the test; no compiler
  issue).
- NEXT: §6a parse offset contract fix + parse_*_view.

## 2026-07-24 — IMPLEMENTATION steps 2-5: §6a fix + parse/source/json/regex adoption LANDED (tree)

- §6a: parse_int_bytes/parse_uint_bytes offsets now RELATIVE per the
  pinned table (invalid-range→0 positionless; misleading `val len =
  end` renamed `limit`); contract note in doc comments records the
  pre-0.33.88 absolute-offset behavior correction.  parse_int_view/
  parse_uint_view added (view-relative offsets; empty view =
  invalid-syntax@0); parse imports std.text; exports updated.
- std.source: SourceCursor.slice_view(start, end) ->
  Result<text.StringByteView, SourceError> — checks and error codes
  IDENTICAL to slice (signature is Int offsets, mirroring the real
  slice, not the checkpoint's SourcePos sketch — recorded deviation).
- std.json: import std.text as txt (alias `text` collides with the
  ubiquitous `text` param name — first compile caught E-AUTO-a6cf269d
  x30); JsonDoc.byte_range_view (explicit numeric, delegates verbatim
  to txt.byte_view) + LocatedCursor.raw_view (provenance-safe,
  parser-span invariant, fail-safe empty view on the unreachable
  arm); _parse_string ESCAPE-FREE FAST PATH: scan-ahead + one
  _slice_string range copy for clean strings, identical limit
  trip-point (scan-count stands in for decoded length), legacy
  control-byte semantics preserved, escape falls back to the general
  loop seeded with the clean prefix.  _parse_number: NO change needed
  — already validates over the span and materializes exactly once
  (checkpoint's honest description matches existing code).
- std.regex: matcher authority = range triple — _try_match_at_range/
  _find_from_range private internals (range-relative positions,
  anchors at range boundaries, reads at base+pos); exported
  _try_match_at/_find_from now one-line delegators (String behavior
  bit-identical); is_match_view/find_first_view (view-relative,
  zero-retain via backing_ref/start_offset) + checked match_view/
  match_subview (end<start pre-check, then byte_view/subview);
  4 exports added.
- 51-check adoption smoke compile+run PASSES: §6a offsets both
  families, slice_view ok+error, raw_view/byte_range_view,
  fast-path + escaped-string equivalence via json.parse, regex
  view-relative offsets, match_subview/match_view round-trips,
  fabricated-match matrix (inverted/negative/oob → checked errors),
  empty-match → empty view, anchors bind to view boundaries (^$
  matches the view, fails the whole string).
- NEXT: existing-suite regression runs, committed test files,
  counting harness, benchmark gate, docs, history.

## 2026-07-24 — IMPLEMENTATION steps 6-9: evidence gates + docs + history COMPLETE; focused gates 45/45

- lang/tests/driver/test_string_byte_view.py (4 fixtures, all
  compile+run): SEMANTICS (31 checks), ADOPTION (51 checks), OFFSET
  TABLE (every §6a row, both families; overflow trips at rel 18 for
  20 nines — guard fires BEFORE the 19th digit consumes — table
  wording "where the scan stopped" holds), FORCED THROW (100 unwinds,
  view+backing usable after).
- lang/tests/driver/test_string_byte_view_counts.py — the §10
  count-exact wrap harness (retain/release/from_utf8_bytes/
  alloc_array/free_array; B5 custom-link + @main rename).  PINNED
  from observation: construct 100 retains/100 ops; dup+subview 201;
  reads (2100 guarded byte_at + 200 searches) retain=1 alloc=0
  release=7 CONSTANT (needles hoisted — literal temps add counted
  no-op releases); to_string nonempty from_utf8=100 (+100 io-buffer
  alloc/free); empty from_utf8=0/alloc=0 (singleton); bulk alloc=1
  (capture-less body does NOT box; only the capturing inner does);
  throw x100 retains stay 1 (no leaked retain; envs freed via
  same-TU drift_cb_env_free — invisible to wrap, delegated to
  memcheck); regex String matching retain DELTA vs compile-only = 0,
  view matching delta = +1 (SUBTRACTION method — compile noise
  cancels).  KEY wrap caveat documented: --wrap sees cross-TU
  (program-level IR) calls only.
- lang/tests/memcheck/test_string_byte_view_lifetime.py — valgrind-
  clean: views outlive bindings (Array + dropped scopes), dup/subview
  single-retain balance, 50 forced-throw window unwinds (THE env
  alloc/free balance proof), split_views + match_view survivors; all
  heap-backed.
- lang/tests/driver/test_string_view_perf_tiers.py — guard-band tier
  gate (NOT a benchmark; contention-safe ratios): bulk(direct+
  composed) >=2x faster than indexed; view reads within 3x of
  byte_at; substring >1.3x the view shape; checksums equal.
- Docs: text.drift substring guidance now points at StringByteView as
  the standard answer; Effective Drift "String views for parsers"
  section added (three tiers, per-token-window antipattern, adopter
  list, C-interop honesty) with its example COMPILE-PROVEN
  (docex2 run exit 0).
- doc/history.md: new 0.33.88 string-view entry incl. the §6a
  BEHAVIOR CORRECTION record (documented relative-offset contract now
  holds; callers may have observed absolute offsets before).
- FOCUSED GATES: 45/45 — all four new files + json/source/text/parse
  existing suites + string layout audit + B5 representation/guards/
  ffi/bytes-base/memcheck batteries.
- REMAINING before mainline of the combined phase: full pre-mainline
  suite (maintainer-run), corpus run with EXPECTED attributable
  stdlib delta (measure→attribute→review→approve→promote), combined
  certification at phase end per the standing plan.

## 2026-07-24 — Static-delta review round: 4 release blockers + amendments CLOSED

- (1) match_view/match_subview now FULLY validate (start>=0, end>=start,
  end<=len) BEFORE the length subtraction — INT_MIN/INT_MAX fabricated
  spans can no longer overflow; EXTREMES fixture pins all four extreme
  combinations for both conversions.
- (2) LocatedCursor.raw_view() FAILS CLOSED: corruption of the parser-
  span invariant is a contract ABORT (assert), never substituted
  content; enforcement lives in exported-internal
  json._span_view_or_abort (the invariant is unreachable from safe
  Drift through raw_view itself) with a tooth proving the abort +
  message.
- (3) perf tier gate rewritten onto the SHIPPED API: w4 = production
  byte_view per token, w5 = StringByteSource.read whole-scan (the
  range-guarded engine/parser read path), w6 = text.with_view_bytes;
  prototype SView/wvb removed from the gate.  Green.
- (4) backing_ref()/start_offset() REMOVED (capability leak: a narrow
  view could expose its entire backing).  Replaced with the reviewer-
  preferred zero-overhead byte-source abstraction:
  text.StringByteSource {bytes: &String, base, len} — PRIVATE borrow
  fields (LocatedCursor precedent, probe-verified zero
  retain/release/alloc in IR), range-guarded fail-closed read();
  byte_source(v) bounds the window to the VIEW, byte_source_all(s)
  keeps String-entry matching zero-retain.  regex authority + parse
  views rewired; checkpoint §11 amended with the revision record.
- Amendments: EXTREMES fixture also pins negative byte_at's EXACT
  IndexError (container id std.text:StringByteView + index) for both
  underflow and overflow indexes; move-only pin (use-twice →
  E-AUTO-e8f17b8b); private-field forgery rejection pin; counting
  harness extended — match conversions retain delta EXACTLY 201
  (100+100+1 whole-view) vs compile+find baseline with no
  materialization, split_views EXACTLY one retain per element (3);
  is_match doc comment restored to is_match; Effective Drift example
  now idiomatic (no & at call sites; auto-borrow) and re-proven
  (docex2 exit 0); "one direct range copy" replaced with the accurate
  range-copy-helper (temp buffer + final storage) wording in
  json.drift + history.
- NEW PRE-EXISTING COMPILER DEFECT FOUND (not view-related, needs
  LANGUAGE_BUG classification): two sequential try/catch statements
  whose TYPED catch binders share a name (`catch std.err:IndexError(e)`
  twice) fail with spanless "use of uninitialized 'e'"
  [E-AUTO-77978427]; each alone compiles; renaming the second binder
  compiles+runs.  Reproduces WITHOUT any view code (plain
  string_byte_at) — minimal repros preserved in scratch
  (svperf/mre3.drift byte_at-free variant mre5).  Binder-scope/alpha-
  renaming family (cf. 0.33.36 nested-try binder, hidden-lambda binder
  collisions).  Worked around in the EXTREMES fixture with a marked
  comment (binder e2); NOT silently absorbed.

## 2026-07-24 — LANGUAGE_BUG slice: sibling catch-binder identity (STOP gate work) + static items

- CLASSIFICATION (maintainer): LANGUAGE_BUG in checker catch-binder
  scope/binding identity — lexical identity/alpha-renaming family.
  TRIGGER SCAN RECORDED: the creation-site lifetime-registration
  trigger in doc/refactor_triggers.md does NOT fire (this is not
  drop, unwind cleanup, or lifetime authority).
- ROOT CAUSE (instrumented pre-fix): HCatchArm carried only the
  binder NAME; the borrow checker's catch-entry initialization used
  the name-keyed _binding_id_by_name fallback (EARLIEST binding per
  name), so with sibling arms both named `e`, BOTH entries marked
  arm 1's bid (bc-debug: entries marked [5]; the failing use carried
  bid 6) → spanless "use of uninitialized 'e'" in arm 2.
- FIX AT THE IDENTITY OWNER (mirrors HMatchArm.binder_ids /
  0.33.4+0.33.36 family; NO local rename workaround):
  hir_nodes.HCatchArm gains binder_id; type_checker records the
  arm-scoped bid on the arm at BOTH allocation sites (statement HTry
  + expression-form arms); borrow_checker catch-entry marking now
  keys (name, binder_id) and marks THE ARM'S OWN binding first
  (name-based collection retained as fallback only).
- REGRESSION: lang/tests/driver/test_catch_binder_sibling_name_reuse.py
  — pre-fix failure recorded in the docstring (exact diagnostic +
  bc-debug bids); two sequential typed catches BOTH named `e` with
  DISTINCT payloads (AlphaErr{code,tag} vs BetaErr{level}),
  compile-AND-run proving each use reads ITS OWN arm's payload;
  three-arm chains reusing `e` twice; NEGATIVE scope pin (binder not
  visible after its arm).  2/2 green.  e2 workaround in the view
  EXTREMES fixture REPLACED with `e` (no temporary workaround
  remains).  ABI unchanged (compiler-internal; rides 0.33.88).
- STATIC ITEMS: (1) perf gate safe-read tier now PRIMARY
  StringByteView.byte_at (within 4x band; landing pads never fire
  in-bounds) with the _StringByteSource path as SECONDARY (3x);
  (2) byte source renamed to repository internal convention
  (_StringByteSource/_byte_source/_byte_source_all, cf. _NfaProg) —
  exported-internal plumbing, NOT promised user API; range guard +
  zero-retain proof retained; (3) Effective Drift prose
  with_view_bytes(&v, cb) → (v, cb).
- FINDING #2 (separate, pre-existing, runtime — NOT the binder bug,
  NOT introduced by this phase): runtime-thrown IndexError from the
  string_byte_at INTRINSIC bounds path aborts (silent SIGABRT) for
  (a) a single POSITIVE-OOB index (mre8: 9999 → abort) and (b) any
  SECOND runtime-thrown IndexError in one fn regardless of binder
  names (mre7/mre9), while a single NEGATIVE index is catchable
  (mre6 clean) and Drift-CONSTRUCTED IndexErrors (view byte_at)
  catch cleanly repeatedly (EXTREMES fixture).  Repro matrix in
  scratch svperf/mre6-9.drift.  Awaiting routing.

## 2026-07-25 — Second LANGUAGE_BUG round: binder corrections + string OOB abort fix

FIRST BUG (sibling catch binders) — three review corrections closed:
- (1) Expression-form: PROBED — sibling-name expr-form arms compile+
  run correctly WITHOUT identity plumbing (no statement-style
  catch-entry marking path exists for HTryExpr); the ineffective
  hasattr assignment REMOVED from the checker with the claim
  documented in place; a real expr-form compile/run sibling-name pin
  added to the regression (guards the property).
- (2) Marking now uses the arm's binder_id EXCLUSIVELY when present
  (name lookup only when absent); mechanism tooth added:
  lang/tests/borrow_checker/test_catch_entry_marks_own_binding.py —
  burst-grouped VALID marks (fixpoint-revisit safe): every entry
  visit marks exactly ONE binding, union == both sibling ids, and
  the name-keyed lookup is never consulted for the binder name.
- (3) Scope negative pins the EXACT diagnostic: E-UNKNOWN-NAME
  "unknown name 'e'" and explicitly NOT use-of-uninitialized.

SECOND BUG — CLASSIFIED per maintainer: LANGUAGE_BUG in the
intrinsic/runtime exception-construction path; no matching
refactor-trigger uplift (catch-lifetime trigger is cleanup/drop
authority, unrelated) — scan recorded.
- CORRECTED PRE-FIX MATRIX: the earlier "single negative catches" was
  a PIPE-SWALLOWED exit code (`| head` ate $?); re-measurement showed
  EVERY intrinsic OOB aborted (mre5-mre9 all SIGABRT), while
  Drift-constructed IndexErrors caught fine.
- ROOT CAUSE: StringByteAt lowering delegated bounds to C-side
  drift_bounds_check whose failure path is drift_error_raise — the
  error_dummy abort() stub (no C→Drift throw channel exists).  Array
  indexing was never affected: hir_to_mir expands xs[i] into an
  IR-level bounds compare + the unified _emit_index_error_throw
  (synthesized IndexError + params JSON + normal dispatch edges).
- FIX AT THE COMMON AUTHORITY: STRING_BYTE_AT MIR lowering now emits
  the SAME guarded-index expansion, and _emit_index_error_throw is
  parameterized by container id (STRING_CONTAINER_ID
  "std.core:String" added to core/container_ids.py, matching
  std.core).  Codegen's C-side check retained as unreachable defense
  in depth.  Lowering-internal only → 0.33.88/ABI 22 unchanged (no
  exported helper signature/layout/convention moved).
- REGRESSION: lang/tests/driver/test_string_byte_at_oob_catchable.py
  — full compile/run pins with EXACT caught indexes (9999 w/
  container id std.core:String; -4; sequential distinct binders;
  sequential SAME binder composing with the identity fix; explicit-
  construction + in-bounds controls) and the nothrow-uncaught abort
  contract preserved.  2/2 green; whole mre matrix now exits 0.

## 2026-07-25 — string_byte_at HOLD corrections closed (3/3)

- (1) PUBLIC METHOD surface: String.byte_at was `nothrow` while
  documenting IndexError — the nothrow frame was a throw WALL, so
  `try { s.byte_at(oob) } catch` aborted INSIDE the wrapper (probe
  confirmed 134 pre-change).  `nothrow` REMOVED (core.drift wrapper;
  doc comment records why); zero stdlib callers of `.byte_at(`
  affected; compile/run pin added (s.byte_at(9999) caught with exact
  index + container id).  Drift-level signature change only — no
  C-boundary change, ABI 22 stands.
- (2) Uncaught control now EXACT: returncode == -6 (SIGABRT, matching
  nothrow_oob_aborts) and stderr PINNED EXACTLY EMPTY (the current
  uncaught channel is the silent raise-stub abort; any future
  uncaught-error diagnostic must be a deliberate reviewed change).
- (3) Valid-read regression MEASURED pre/post using the preserved
  pre-fix binary (same source/flags/machine): checked-everywhere
  first cut was +18% (2135→2518us, 2MB scan).  Introduced
  StringByteAt(unchecked=True) — emitted ONLY by the guarded
  expansion (StringLen already ran the observation guard on the same
  handle; bounds proven by the branch); codegen skips the second
  guard + the C bounds call for it; the fully CHECKED form remains
  the default for any other MIR producer.  RESULT: 2MB scan 2138 →
  846us — ~2.5x FASTER than pre-fix (the per-byte C call dominated);
  view reads 2334→1120us.  Emitted-IR growth +8.4% pre-opt lines
  (113,487→123,026), binary +4.6% (677,192→708,560 B) on the bench
  unit — recorded.
- Perf tier gate REBANDED on fresh evidence (bulk-vs-indexed gap
  narrowed ~5.2x→~2.0x because indexed got faster): bulk>=1.4x,
  read tiers within 4x, substring>1.3x view — all measured with
  headroom.  Live doc claims retimed (text.drift ~2x;
  effective-drift "roughly halves"); checkpoint gains a
  post-checkpoint retiming ADDENDUM (§2 tables stay as the honest
  pre-fix record; tier order and all decisions unchanged; per-token
  bulk antipattern stands — crossover only moves upward).
- history.md updated (String.byte_at can-throw + retiming noted in
  the LANGUAGE_BUG entry).

## 2026-07-25 — Byte-access HOLD rework: binding Result API + validator + perf/records

- BINDING API landed: String.byte_at + StringByteView.byte_at nothrow
  -> Result<Byte, std.err:IndexError>.  PLACEMENT DEVIATION (recorded):
  the String method lives in std.text via foreign `implement String`
  (probe-verified) because std.core cannot import std.err (cycle);
  callers import std.text.  core's old wrapper REMOVED.
- PRIMITIVE realigned: core.string_byte_at declared NOTHROW,
  documented-internal, FAIL-CLOSED — MIR expansion now assert-shaped
  (AssertLoc diagnostic "string byte access out of range..." +
  Unreachable on the fail edge; no throw machinery), unchecked load
  on the proven edge.  Three authorities agree (decl/CallInfo/
  lowering).  GATING the primitive from user code judged INFEASIBLE:
  71 e2e fixtures call it (frozen corpus) — documented-internal
  instead (deviation recorded).
- MIR VALIDATOR: stage2/unchecked_load_validator.py wired at
  lower_module_to_llvm entry (FINAL post-mutation boundary): proves
  single-pred THEN-edge reachability, AND(GE(i,0-const),
  LT(i,StringLen(SAME value))) over the load's OWN value+index,
  AssertLoc-on-same-cond + Unreachable fail edge.  9 stage2 teeth:
  canonical passes; unguarded / wrong string / wrong index / reversed
  branch / missing observation / mutated cond / assert-less fail edge
  all rejected; checked loads unconstrained.
- CONSUMERS: 4 e2e fixtures updated (3 digit-readers -> primitive;
  string_byte_at_method -> Result API incl. Err pin) — fixture-hash
  universe delta EXPECTED, for corpus attribution/promotion review.
  Embedded fixtures (semantics/extremes/counts/perf-gate) reworked to
  Result; counts op_reads (2100 Result reads) still retain=1 alloc=0
  → "Result reads do not retain/allocate" PROVEN count-exactly.
- REGRESSION REWRITTEN: test_string_byte_access_result.py (replaces
  the catchability file): exact Ok/Err both surfaces both OOB signs
  in a NOTHROW caller; no-exception pin (deliberate try/catch never
  fires — Err-as-data inside); primitive valid reads + FAIL-CLOSED
  OOB pinned EXACTLY (SIGABRT -6 + "string byte access out of range"
  + "assertion failed" on stderr — no more silent-abort pin);
  internal source path nothrow+correct.
- PERF (co-equal gate): CURRENT-TREE ONE-TABLE (512KiB, medians):
  raw 212us | bulk 101/113us (0.5x) | source reads 448us (2.1x) |
  PUBLIC Result byte_at 2211us (10.4x) | view/token 3361 | substring
  6224.  TARGET MISS FLAGGED: <=2x for the public Result path NOT
  met — w5-vs-w5b delta (identical reads) = Result ENUM machinery
  (~3.4ns/byte: construction+match+outlined enum-drop of the
  String-bearing Err temp), NOT duplicated range checks; optimizing
  enum/match cleanup lowering collides with the ownership-lattice
  change bar → maintainer decision required.  Gate: separate view
  (14x TRIPWIRE, explicitly not an endorsement) and source (3x)
  assertions + the table in-file.
- COMPILE IMPACT (representative full-stdlib-closure build, legacy
  lowering flip x3 runs): wall 22.73->22.92s (+0.8%), pre-opt IR
  +3.7%, optimized binary IDENTICAL (716,128 B) — NOT material.
- CROSSOVER remeasured (final impl): break-even ~216 B; ONE
  recommendation everywhere = bulk from ~256 B up (text.drift,
  effective-drift, checkpoint addendum 2, history all aligned).

## 2026-07-25 — Enum/match cleanup optimization round: <=2x target MET; validator hardened; docs reconciled

- OPTIMIZATION (general, authority-level, per directive — no byte_at
  special case): (1) by-value alwaysinline __drift_variant_drop_
  single-value helpers (arrays keep the loop helper); (2) observe
  guard: branch-lean hot path + cold noinline __drift_string_observe_fail
  dispatch (six messages EXACT; the old inlined fail arms cost
  byte_length 260 vs threshold 225 → NOTHING in stdlib ever inlined);
  (3) emit_func hoists static allocas to entry blocks (non-entry
  alloca = LLVM "never inline: dynamic alloca" — this alone took the
  Result tier 2211→874us); (4) size-based inlinehint (MIR<=64).
  Chain of measured causes: PLT/cost→guard bulk; never-inline→
  mid-block allocas; final 10 points of cost→inlinehint.
- RESULT: public accessors 2211→250us (view, 1.16x) / 358us (String,
  1.66x) — BOTH under the HARD <=2x band; iterator MEASURED 1.59x
  (docs updated from "raw byte speed"); source 1.15x; bulk 0.5x.
  Full 9-tier table in the gate + checkpoint addendum 3.
- PROOF SET (directive item 1): test_variant_drop_inline.py (Ok-heavy
  loop, Err loop w/ retained String payloads, EARLY RETURN with live
  Err Result, loop-carried+break, non-Result control; IR-shape teeth:
  by-value alwaysinline helper, call-by-value, NO len=1 array-helper
  Result drops) + valgrind twin (exactly-once under memcheck);
  counters: CODEGEN-only change, MIR untouched → ownership counters
  structurally identical (corpus enforces end-to-end).
- VALIDATOR hardened: unchecked load must be FIRST instruction of its
  block; negative tooth inserts DropValue before the load (10/10).
- DOCS: effective-drift no longer recommends core.string_byte_at to
  users (documented-internal note instead); std.text import
  requirement explicit; iterator claim = measured 1.6x; history 64B
  crossover remnant reconciled to 216B/256B.
- WHOLE-WORKLOAD REPEAT: wall 22.91s (+0.8% vs legacy, unchanged);
  binary 716,128→766,232 (+7.0%) — the inlining trade, REPORTED for
  review (pre-opt IR +3.3%).

## 2026-07-25 — Four soundness/footprint closures (review round) COMPLETE

- (1) ALLOCA PLACEMENT: textual global hoist REMOVED; replaced by
  owning-site registration (_scratch_alloca in _FuncBuilder + the
  drop-helper closure; 9 builder sites + drop-helper arm allocas +
  iface helper converted — each a transient slot fully re-stored
  before use, address never escaping its lowering → entry placement
  is semantics-preserving by construction).  Teeth: module-wide
  no-non-entry-static-alloca scan + escaping-loop-local address
  CONTROL (ptr_from_ref per iteration, values per-iteration correct).
- (2) INLINEHINT narrowed STRUCTURALLY: hot-path MIR (outside
  Unreachable-terminated arms) <= 48, threshold-swept (0/28/40/48/56;
  56 was bimodal-at-2.0x on String.byte_at; 48 stable, same size);
  positive/negative eligibility teeth (small accessor w/ cold assert
  arm hinted; 70-instr hot function not).  Blanket +7% REJECTED and
  replaced: final binary 758,320 (+2.2% over no-hint; -1.0% vs
  blanket) with BOTH accessors under the hard 2x band.
- (3) GUARD ORDER: negative len rejected BEFORE the storage+8 flags
  deref (fault risk on {neg len, garbage non-NULL ptr});
  negative_badptr subprocess tooth (storage=0x8) added to the
  observation-guard battery, both builds — message pinned, no
  SIGSEGV.
- (4) VARIANT CONTROLS added: user variant (Tag(String)/Plain) +
  Optional<String> with RUNTIME-UNKNOWN parity-driven tags, both
  arms, exactly-once (valgrind twin); Array<String> control pins the
  loop-shaped array helper WITH RUNTIME LEN in IR; Shape drops pinned
  to the by-value helper.
- ABLATION TABLE recorded (checkpoint addendum 4): each optimization
  dimension's removal costs 2-6x on an accessor tier for <=1.1% size;
  final whole-workload wall +0.2%, binary +5.9% total / hint +2.2%.
- Tier gate now asserts on per-run MINIMA (String.byte_at is bimodal
  ~352/~440us across launches — ASLR/alignment; minima are the
  demonstrated capability; interference only inflates).  3/3 stable
  gate runs.  FINAL: view 1.9x, String 1.6x, iter 1.4x, source
  1.03x, bulk 0.47x.

## 2026-07-25 — Honest-gate + shape-hint round (4 corrections) CLOSED

- (1) TIER GATE: minima selection REMOVED — compile once, 5 fresh
  launches, SAME-LAUNCH median/median ratios, EVERY launch must meet
  the hard bands.  The String.byte_at bimodality (352/440us)
  DISAPPEARED under the final shape-narrowed hint (8/8 probe launches
  1.66-1.70x) — no tuning or band relaxation needed.
- (2) INLINEHINT now structurally SMALL + SHAPE: _inline_hint_eligible
  (extracted, unit-testable) requires hot<=48 AND (variant return OR
  Unreachable-cold failure block).  Ordinary small hot fns NOT hinted
  (byte_length correctly unhinted, inlines on cost).  Teeth: exact
  48/49 boundary x both shapes, ordinary-small negative, cold-
  discount positive (unit, stage2) + IR-level category
  positives/negatives (small_accessor cold-arm, small_variant Result;
  small_plain + big_hot negatives).
- (3) SIZE both baselines pinned everywhere: FINAL binary 746,040 B =
  +0.5% vs no-hint (742,136) and +4.2% vs legacy (716,128); wall at
  PARITY (22.63-22.74 vs 22.73).  The <=1.1% ablation claim corrected
  (no-hint saves ~2.1% from the interim point).  The final
  small+shape predicate reclaimed most of the interim +5.9%.
- (4) Loop-local control RENAMED address-taken (claim narrowed: Drift
  borrow rules forbid a true iteration-escaping address from source);
  STATIC SOURCE INVENTORY tooth added — every alloca-emitting
  lowering family must route through the scratch registries (found
  and converted 5 more sites: clone-helper idx/vptr/vtmp/optr +
  array-drop loop idx_ptr; allowlist holds only entry-position
  prologue emissions).
- Gate green 2/2 multi-launch; policy teeth 4/4; predicate teeth 7/7.

## 2026-07-25 — AST inventory + prose closures (test/prose-only round)

- INVENTORY replaced with an AST-BASED exhaustive scan
  (_scan_alloca_emissions): every string literal or f-string
  containing "= alloca" anywhere in llvm_codegen.py — independent of
  append/insert/extend/list-literal emission form — classified
  against _ALLOCA_AUTHORITIES (each with its entry-placement
  justification: the two scratch registries; four
  entry-insertion-index authorities — local storage, shared iface
  slot, fresh iface slot, dbg keepalive; two entry-prologue literal
  authorities — argv thunk, iface drop helper; docstrings excluded).
  Unclassified occurrence = failure.  NEGATIVE TEETH: synthetic
  append-form AND list-literal/extend-form sources both detected
  (the replaced grep missed non-append forms by construction).
- PROSE: history — "small functions get inlinehint" → small ELIGIBLE
  accessor-shaped functions; alloca sentence rewritten to owning-site
  registration of known-nonescaping scratch temporaries (no global
  static-alloca hoist implication).  Checkpoint addendum 4 — the
  size-only hot<=48 description and the 758,320-byte ablation table
  marked SUPERSEDED INTERIM EVIDENCE (final small+shape config =
  746,040 B, closing section); "escaping-loop-local" wording renamed
  to the accurate ADDRESS-TAKEN control with the narrowed claim.
- Per directive: no corpus, performance, or broad-suite rerun in this
  round (test/prose only); the policy test file rerun locally.

## 2026-07-25 — Corpus measurement + attribution COMPLETE (residual zero); splice regression caught & fixed; perf-protocols recipe added

- WHITESPACE blocker cleared (llvm_codegen.py:10748); git diff --check
  clean.
- CORPUS RUN 1 caught a REAL REGRESSION the focused suites missed
  (they excluded the e2e lane): the clone-helper entry-alloca splice
  was LABEL-BLIND — that generator emits an explicit __bb_entry:
  (unlike the drop helpers), so recursive-variant/struct clone
  helpers got allocas BEFORE the label → invalid IR → exactly
  variant_recursive_borrow_copy + array_recursive_struct_borrow_copy
  flipped compiled→failed.  All three splice sites made label-aware;
  both fixtures compile and exit with their expected codes; targeted
  gates 20/20.
- CORPUS RUN 2 (fixed tree, run dir ownership-corpus-20260725-070420-
  2045579, retained): partition IDENTICAL 924/344/49; universe delta
  = EXACTLY the 4 intentionally API-migrated fixtures (hash-only).
  PER-FIXTURE ATTRIBUTION vs the retained promoted-baseline audit
  data: 923/924 fixtures carry the IDENTICAL modal delta {fns +35,
  events +22, c1_agree +20, c3_moveout_owned +21, moveout_expansion
  +21, overwrite_release +1} — the uniform stdlib contribution,
  matching the single-compile probe module-by-module (text +24 fns,
  regex +6, json +3, parse +2, source +1, core -1 = +35 exactly).
  ONE outlier: string_byte_at_method (its body became 4 Result
  matches): beyond-modal {+28 events, +27 moveout_owned/expansion,
  +5 c1_agree, +1 materialized_lastuse_release} — fully explained.
  TOTALS: fns +32,340; events +20,356; c1_agree +18,485;
  moveout_owned/expansion +19,431; overwrite_release +924;
  materialized_lastuse_release +1.  RECONCILIATION: per-fixture sums
  == totals on EVERY counter — RESIDUAL ZERO.  Hard gates all zero.
- PERF-PROTOCOLS RECIPE (maintainer-approved): `just perf-protocols`
  — read-only: tier bench (compile once, 3 fresh launches),
  crossover sweep, whole-workload compile timing + binary size;
  sources committed under tools/perf/ with provenance headers;
  explicitly NOT part of test/certify; ablations remain a documented
  manual procedure.  Smoke: parses + runs (workload 746,032 B, wall
  22.6-22.8s — matches the recorded finals).
- AWAITING: maintainer review of this attribution → reviewed-baseline
  promotion from the retained run dir → the one full suite →
  combined B5 + string-view certification.
- perf-protocols recipe HARDENED (maintainer-directed): refuses to run
  with DRIFT_MEMCHECK/DRIFT_ASAN set (exit 2 + rationale to stderr —
  sanitized figures are invalid as references; sanitizer coverage of
  the feature surface lives in the test suites).  Both refusals
  proven; native run unchanged.
- perf-protocols LIVENESS: 20s compile heartbeat added (compiling.../
  still compiling (Ns)/compiled banners + per-run step lines) — no
  silent stretch exceeds the watchdog window on any host speed.
- crossover_bench.drift MIGRATED to the SHIPPED APIs (maintainer
  blocker): safe tier = StringByteView.byte_at Result match-unwrap;
  bulk tier = per-window SUBVIEW (one retain+drop per window) +
  text.with_view_bytes with a per-window boxed callback — per-window
  construction/drop behavior preserved.  Prototype SView/wvb and the
  raw core.string_byte_at removed from the protocol.  Header notes
  the reference-figure shift.  SMOKE (recipe-only, per directive):
  production curve — safe ~1.8-1.9ms flat; bulk 12.9ms@8B / 3.2ms@32B
  / 0.92ms@128B / 372us@512B / 186us@64K → production break-even
  ≈ 64-96B (lower than the raw-shape 216B, as the safe tier now
  prices the Result accessor).  The docs' "~256B and up" guidance
  remains CONSERVATIVE-SAFE (bulk strictly wins above it); tightening
  it is a maintainer call, not taken unilaterally.
- Maintainer will run run-all-tests.sh directly.

## 2026-07-25 — Baseline promotion 2 + shipped-API guidance (maintainer-approved; artifact/doc-only)

- PROMOTED build/tmp/ownership-corpus-20260725-070420-2045579 into
  reviewed-baseline EXACTLY (no rerun, semantics untouched):
  validated byte-exact vs the attribution report first (partition
  924/344/49, totals, gates zero, env 0.33.88/ABI22/tool 1.7.1);
  BASELINE.md rewritten with the full promotion chain (3d48b7f0
  certified → b2caeb44 promotion → this) + the residual-zero
  attribution record.  Zero-delta proven against the promoted
  artifacts; corpus teeth 16/16.  run-all-tests.sh stage 1 now passes
  genuinely on this tree.
- GUIDANCE updated to shipped-API evidence (maintainer-approved):
  break-even ~64-96 B, recommend bulk from ~128 B — text.drift (both
  comments), effective-drift, history, crossover bench header;
  checkpoint addendum 5 records the supersession (the 216 B/256 B
  raw-shape guidance was conservative-safe, not wrong).
- Maintainer runs run-all-tests.sh next (corpus stage → genuine exit
  0 → memcheck just test → ASAN just test).
