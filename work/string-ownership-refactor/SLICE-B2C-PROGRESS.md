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
  **COMMIT-CLEAR.  The maintainer runs the ACTUAL full suite
  (`run-all.sh`), certifies, and deploys 0.33.87/ABI-21; then
  B-repr(B5) opens as a separate design-first ABI-22 boundary.**
- **GATES PASS (2026-07-22)**: 924 corpus audit `build/tmp/s7s8` vs
  accepted baseline `build/tmp/s5s6` — exit 0, universe identical
  (924/1268, same partition), **all 14 counters +0** (incl.
  drop_before_overwrite_site4=14, overwrite_release=233,519,
  scope_exit_release=68,562, materialized_lastuse_release=618,744,
  pre_post_verdict_drift=48,178).  Full memcheck: **105 passed /
  1 skipped, 0 leaks**, lane audit PASS — identical to the S5+S6
  gate.  Chunk complete.  (The then-next steps — S9 review and Phase D
  — are COMPLETE; see the Phase D combined sweep section below.)
