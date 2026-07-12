# String Scope A — progress log

> 2026-07-08: Scope B PLAN written (`SCOPE-B-PLAN.md`, plan-only, no code). Headline: "Scope B"
> splits into B-arch (string_arc→ledger authority unification, ABI-NEUTRAL — the real debt) and
> B-repr (representation reshape, ABI bump + pool recert + downstream migration — no current
> correctness driver). v1 recommendation: defer B-repr; adopt B-arch sequenced after the
> ledger-cache-safety slice; enter via a differential reporter (B-arch-0).
> 2026-07-08 v2 (maintainer relaxed constraints: ABI breakage acceptable; C direct-field
> interop non-sacred): re-ranked in §10. Order UNCHANGED and forced by dependency structure —
> String stays implicit-copy, so no representation removes the stake-authoring problem
> (`_emit_copy_value` already unifies at codegen; Arc avoids a string_arc-equivalent only by
> being clone-EXPLICIT). What changes: B-repr graduates from deferred to a COMMITTED follow-on
> slice targeting **B5 "RcBytes"** ({len, ptr→{strong,flags}+bytes} — header at offset 0,
> inline len kept, accessor-based C API, static-empty singleton retires {0,NULL}); SSO stays
> rejected (re-forks Scope A's classification). Final sequence (v2.1 — ledger-cache-safety
> verified ALREADY IN-TREE, plan §11.1): B-arch-0 reporter → B-arch-1..n (string_arc deleted) →
> B-repr(B5) with ABI 20→21 + pool recert.
> 2026-07-08 v2.1: §11 added per maintainer review — ledger-cache prerequisite confirmed
> satisfied (stage2/ledger_cache.py + dirty-bit/mutation-audit tests in-tree); B-arch-0 reporter
> contract pinned (closed StringStakeEvent model tagged at emission sites, L_pre/L_post only,
> divergence classes C1-C4 + capped UNCLASSIFIED, DRIFT_STRING_ARC_AUDIT=1 gated,
> observational-only gate, no-fixes rule). Substance ACCEPTED by maintainer; cleared to start
> B-arch-0.

Branch: `refactor/string-transfer-policy-scope-a` (cut 2026-07-07 from post-0.33.74-cert main).
Plan: `NEXT-PHASE-PLAN.md` in this directory. Scope per maintainer: structural String/non-bitcopy Copy
classification → centralized alias-to-owned transfer handling → projected-capture widening only after
the ownership fix is proven. OUT: String runtime representation (Scope B), `string_arc.py` ledger merge
(unless strictly necessary), ref-typed callback args (0.33.74 handled), `--emit-package` re-enable.

## Log

- 2026-07-07: branch cut. Lock-in verified on cert base:
  `test_boxed_callback_projected_move_capture_rejected.py` 6/6 green (incl.
  `test_copy_typed_non_bitcopy_struct_field_still_rejected`).
- 2026-07-07: STEP 1 (probe) — DONE. Throwaway relaxation of both gate sites
  (`borrow_checker_pass.py::_is_copy_projected_field` + `lambda_validate.py` resolver mirror; marked
  SCOPE-A PROBE THROWAWAY) + probe `scopea_tag_probe.drift` (session scratchpad): non-Copy root
  `Prepared { tag: Tag, items: Array<Int> }`, `Tag { label: String }` with `implement core.Copy`,
  boxed callback implicitly captures `p.tag` (MOVE→COPY downgrade), body passes it by value to
  `fn describe(t: Tag)`.
  **Repro:** plain runs nondeterministic (exit 0 / glibc `tcache` abort); ASAN deterministic
  `heap-use-after-free` in `drift_string_release` (string_runtime.c:256).

  **EXACT TRANSFER BOUNDARY THAT LOSES OWNERSHIP** (final MIR compared between `main` and the hidden
  lambda for the same source expression `describe(p.tag)`):
  - `main`: `StructGetField(field_ty=Tag)` → **`CopyValue(ty=Tag)`** → call. Ownership correct — the
    by-value arg gets an independently-retained deep copy (String field retained inside
    `_emit_copy_value`'s struct recursion).
  - lambda body: `LoadRef(env)` → `StructGetField(.t7, env_struct, field_ty=Tag)` →
    **`Call(describe, args=['.t7'])` with NO CopyValue.**
  The COPY-kind branch of `hir_to_mir.py::_load_capture_from_env` returns the shallow env-field extract
  WITHOUT marking it in `_ref_field_temps`, so the by-value call-argument boundary's
  `_copy_if_ref_alias` no-ops; `describe` treats the alias as owned and its exit releases `label`; the
  callback env's own drop releases the same `label` again → double release / UAF.
  Note: env CONSTRUCTION is NOT the leak — the outer side correctly emits `CopyValue(Tag)` before
  storing into the env (0.33.70's §4e.1 fix working as intended). This is the third sibling of the
  §4e family: 0.33.70 fixed the REF-kind slot-read marking (§4e.2); the COPY-kind non-bitcopy read was
  left unmarked because the bitcopy gate made it unreachable. Also confirms the audit's correction:
  classification alone would NOT have caught this — it is a missing CALL to the shared alias-marking
  helper on one more parallel read path.
- 2026-07-07: throwaway gate relaxation REVERTED (implementation proceeds against the narrow gate;
  widening is the final step per plan).
- 2026-07-07: STEP 2 (implementation) — code in, validation pending.
  a. **Structural classification** (`types_core.py::_is_copy_structural`): SCALAR-String now
     structurally Copy=True (was False), closing the isolated-vs-stdlib two-authority split; String's
     ownership facts (Copy=True, needs-drop=True, bitcopy=False) are now all mode-independent.
  b. **Centralized alias-to-owned** (`hir_to_mir.py::_mark_ref_alias_if_non_bitcopy`): single contract
     helper; converted the three existing bare `_ref_field_temps.add` sites (deref path, array-index
     field fast path, `_load_capture_from_env` REF branch) and ADDED the two missing paths:
     (1) `_load_capture_from_env` COPY-kind fall-through — THE probe's boundary; (2) the HVar
     visitor's inline whole-root REF/REF_MUT capture read (the audit's fourth parallel path).
  Validation plan: focused suites at narrow gate (capture tests, String ownership/leak memcheck rows,
  projected-capture driver tests) → widen gate (both sites) → Tag probe clean under ASAN/Valgrind →
  flip lock-in regression.
- 2026-07-07: STEP 2 validated at NARROW gate: capture suites 25/25; ownership matrix ASAN clean;
  `lang/tests/memcheck` 91 passed / 4 failed — **failures bisected to PRE-EXISTING** (same set with
  HEAD file contents restored; `test_unmatched_typed_catch_propagate_no_uaf.py`, compile failures;
  memcheck is outside the normal `just test` gate — flagged to maintainer for separate triage).
- 2026-07-07: STEP 3 (widening) — DONE. Both gate sites lifted to full Copy surface
  (`_is_copy_projected_field` + `lambda_validate` resolver); docstrings rewritten to describe the
  root-cause fix instead of the narrowing. Soundness edge verified: `implement core.Copy` on an
  interface-carrying struct is rejected at the impl site (`E_COPY_IMPL_NONCOPY_TARGET`), so the
  widened gate cannot admit interface-containing fields.
  **Ownership proof:** `scopea_tag_probe` (the STEP-1 UAF repro) now 5×exit-0 plain, ASAN clean,
  Valgrind clean. Lock-ins flipped: `test_copy_typed_non_bitcopy_string_field_compiles_and_runs` +
  `test_copy_typed_non_bitcopy_struct_field_runs_clean_asan` (the in-tree ASAN proof of the
  0.33.70-confirmed UAF shape). Batteries: projected-capture 13/13 (incl. package-emit rejection
  pins), high-risk memcheck matrix subset ok, `lang/tests/packages` 472/472.
- 2026-07-07: `DRIFTC_VERSION` 0.33.74 → 0.33.75; ABI stays 20. `doc/history.md` entry added.
  Slice complete pending maintainer's full serial gate. Report:
  `/tmp/drift-announce/` (see latest scope-a file).
- 2026-07-07: two gate cleanups folded in per maintainer (both maintainer-diagnosed, verified here):
  1. `lang/tests/memcheck/test_unmatched_typed_catch_propagate_no_uaf.py` — the 4 pre-existing compile
     failures were STALE FIXTURE SYNTAX (private `fn main()` entrypoints predating the 0.33.6x
     pub-entrypoint requirement), not the typed-catch UAF reopening. All four carriers (plus the module
     docstring example) now `pub fn main()`: **4 passed** incl. Valgrind checks.
  2. `test_drop_policy_contract.py::test_drop_policy_string_unshortcut_classification` updated for
     Scope A: isolated-table String policy is now `needs_drop=True, is_bitcopy=False,
     is_cheap_copy=True, has_structural_drop=True` (structurally Copy), and the docstring documents
     that the isolated and Copy-hook classifications must now AGREE. Drop-policy battery
     (contract + copy-short-circuit + pkg copy-status divergence + match-scrut CopyValue): **15 passed**.
- 2026-07-07: full-gate round 2 — 7 stage2 unit failures, all pinning the pre-Scope-A ISOLATED-mode
  String classification. Updated BY INTENT per maintainer:
  1. Non-Copy/MoveOut/partial-move machinery tests keep true non-Copy coverage via the canonical
     non-Copy droppable carrier `Array<Int>` (String can no longer drive the MOVE branch anywhere):
     `test_constructor_noncopy_arg_moves_out_local`,
     `test_match_by_value_noncopy_binder_moves_payload_and_zeros_source`, and both
     `test_match_cleanup_full_candidate_set.py` builders (Pair Array/Array, Pair2 Array/Int) — the
     Filter-A/Filter-B retirement pins are preserved unchanged.
  2. String-specific tests now assert Scope-A behavior: array-literal String lvalues emit CopyValue in
     isolated stage2 (1 for single, 2 for the two-element reuse case); new companions
     `test_constructor_string_arg_copies` (LoadLocal no-MoveOut + CleanupHook keeps `s` a live drop
     candidate; the balancing retain is authored by later ledger passes) and
     `test_match_by_value_string_binder_copies_payload` (binder CopyValue, no MoveOut).
  3. `test_match_copy_payload_emits_copyvalue_and_has_single_scrutinee_drop_across_cfg` REFRAMED (the
     old "exactly one DropValue(variant) across CFG" was an isolated-mode artifact — the String `msg`
     binder partial-moved, suppressing the Some-arm whole drop). Authored MIR verified tombstone-safe:
     arm MoveOut→scrut-tmp + TombstoneValue stored back to `x`; join drops `x` (live on the None path,
     tombstoned no-op on the Some path). New pins: no drops in match_dispatch, ≤1 variant drop per
     block, tombstone store on the consumed source path, both binders CopyValue, String binder cleaned
     exactly once across the CFG.
  Full `lang/tests/stage2` suite after the sweep: **311 passed, 0 failed** (302 prior + 7 fixed +
  2 new String-contract companions), lane audit clean. Handed to maintainer for the full serial gate.
- 2026-07-07: full-gate round 3 — 2 driver failures
  (`test_replace_consumes_noncopy_arg_and_rejects_later_borrow`,
  `test_string_kind_implicit_const_share_rewrite_into_generic_field`). **NOT stale carriers —
  CANARIES for an unintended production semantic change.** Decisive experiment: `Box { x: String }`
  (no declared Copy impl) + `mem.replace` + later `&b` — certified 0.33.74 full build REJECTS
  (`cannot borrow from moved or uninitialized 'b'`); working-tree 0.33.75 full build ACCEPTS.
  Mechanism: `_is_copy_structural`'s STRUCT/VARIANT recursion propagates String's new structural
  True upward → undeclared String-bearing composites auto-Copy wherever the structural answer is
  authoritative (no-hook contexts AND the hook-mode structural fallback, whose eligibility gate
  checks resolvability only, not declared impls). Finding + proposed surgical fix (stop String
  propagation in the two composite arms only; keep SCALAR String True):
  `/tmp/drift-announce/2026-07-07T182113Z-scope-a-composite-copy-widening-finding.md`.
  NO PATCH APPLIED — awaiting maintainer decision per instruction.
- 2026-07-07: maintainer agreed (blocking) — surgical composite-boundary fix APPLIED:
  `_field_propagates_structural_copy` helper in `types_core.py::copy_status`; the STRUCT/VARIANT
  structural recursion now evaluates SCALAR-String fields under the legacy poison rule (String does
  not propagate structural Copy into undeclared composites) while direct `copy_status(String)` stays
  structurally True. Scope-B escalation NOT needed — the narrow patch preserves all four required
  properties. Verification ladder ALL GREEN:
  1. Canaries UNCHANGED: `test_intrinsic_move_borrowcheck` + `test_constshare_generic_field_frontend`
     + projected file — 11 passed.
  2. Box production repro REJECTS again (`cannot borrow from moved or uninitialized 'b'`), matching
     certified 0.33.74.
  3. Declared-Copy Tag positives: projected ASAN row in-suite + standalone probe 3×exit-0,
     Valgrind clean, ASAN clean.
  4. stage2 suite: **311 passed**.
  5. Drop-policy battery: **15 passed** (direct String mode-independence intact).
  Handed to maintainer for the full serial gate.

## Scope B log

- 2026-07-09: **B-arch-0 (differential stake reporter) IMPLEMENTED** per plan §11.2.
  `DRIFT_STRING_ARC_AUDIT=1` gates `StringArcAudit` in `ownership_ledger_reporter.py`
  (extends the existing reporter, no new framework); `string_arc.py` emission sites tagged
  with the closed site_class enumeration (deliverable deltas: +temp_lastuse_release,
  +store_value_retain, +value_position_retain; destructor_self structurally unused).
  L_pre vs L_post diff classifies into C1-C4 (+pre_post_verdict_drift both-snapshot check);
  per-fn JSONL (divergent fns only unless VERBOSE) + atexit aggregate; details capped 50/class.
  NEUTRALITY: seeded off-vs-on IR byte-identical modulo build timestamp (unseeded off-vs-off
  differs 9.2k lines = pre-existing hash nondeterminism); stage2 suite 318 passed (incl. 5 new
  pins in test_string_arc_audit_reporter.py). CORPUS (e2e, both rounds: 543 compiles, 1.97M events):
  **UNTAGGED=0, UNCLASSIFIED=0** — gate criterion met, no classification-model revision needed.
  Zero leak candidates (c1_must_drop_without_release=0); c2_visible_stake=0 (every emitted
  retain is ledger-invisible — plan premise empirically confirmed). Inventory + ranked B-arch-1
  worklist: B-ARCH-0-INVENTORY.md (rank 1 = site-3 return stake). Review report:
  /tmp/drift-announce/2026-07-09T150000Z-barch0-string-stake-reporter.md. NO fixes made,
  NO full gate run (per direction). Awaiting B-arch-1 shape decision.
- 2026-07-09: B-arch-0 review fixes landed: L_post fail-closed (`post_ledger_build_failed`
  hard-count, force-emitted past the volume guard; pinned — audit pins 6/6), stale docstring +
  detail-cap comment corrected. **B-arch-1 STOPPED before coding per the explicit stop
  condition**: corpus has ZERO return_retain_site3 events (shape structurally extinct post
  Phase 4); C4 = 100% release-face = downstream shadow of C2 ctor/call-arg stakes; eliminating
  it requires the broad value-position CopyValue migration the scope excluded. Probe: `return
  p.name` generates zero string_arc events (copy materialized upstream). Inventory §4 ranking
  REVISED (rank 1 = C2 stake materialization as one mechanism, zeroes C4 as byproduct). Stop
  report: /tmp/drift-announce/2026-07-09T170000Z-barch1-stop-rescope-return-stake.md.
  Awaiting re-scope decision.
- 2026-07-09: **B-arch-1a (call-arg String stakes) IMPLEMENTED + audited.**
  `stage2/string_stakes.py` materializes by-value String call-arg copy stakes as pre-ledger
  CopyValue (Call/CallIndirect/CallIface; producer-chain-ends-at-LoadLocal criterion mirrors
  string_arc's move-vs-retain decision; semantic SCALAR/"String" param predicate per review
  finding, pkg-boundary pin added). Driver wiring in the cleanup_authoring loop before the
  per-fn ledger rebuild; mark_ledger_dirty + mutation-audit SCOPED_FILES. Verification:
  new pins 9/9 (direct+ASAN, move-still-moves, return-reaching+ASAN, indirect, iface,
  pkg-boundary, audit acceptance); stage2+memcheck 415 passed/1 skipped; om matrices 51/51
  normal + 51/51 ASAN; pkgb ok; mutation/dirty-bit audits 16/16. CORPUS before/after
  (FULL definitive run, 543 vs 543 compiles, 647,943 fns both sides): **call_arg_retain
  58,680 → 0, zero residuals**; c2 114,107 → 55,427 (= value_position 47,803 + store_value
  7,624 exactly); c4 unchanged (106,620 — call args caused none, consistent with the
  stop-report analysis); leak candidates / UNTAGGED / UNCLASSIFIED / post_ledger_build_failed
  all 0; every other counter byte-identical. Events −58,680 exactly. Report:
  /tmp/drift-announce/2026-07-09T200000Z-barch1a-call-arg-stakes.md. Awaiting review;
  B-arch-1b (value_position) not started per direction.

- 2026-07-09: **B-arch-1b (value-position String stakes) IMPLEMENTED + audited.**
  `string_stakes.py` generalized: shared LoadLocal-producer criterion applied via a
  value-position table (ConstructStruct/Variant args, ArrayLit elements,
  ArrayElemInit/InitUnchecked/Assign, ConstructIfaceValue, ConstructResultOk,
  ConstructError.event_fqn, ExcSetParamsJson, ExcAppendContextFrame). Pins 10/10
  (ctor+ASAN, variant, Result-Ok, exc-params+ASAN, array+ASAN, audit acceptance, pkg-boundary
  ctor); stage2+memcheck+1a-pins 424 passed/1 skipped; matrices 51/51+51/51; pkgb clean.
  CORPUS three-way (543/647,943 identical universe): value_position 47,803 → 20,103 (−27,700,
  events −27,700 exactly); **C4 106,620 → 82,728, the −23,892 converting EXACTLY into
  c1_agree** (CopyValue breaks Return-as-move; boundary releases now ledger-agreed);
  pre_post_verdict_drift +23,892 (same locals, post-ledger zero-store reinit — explained,
  not a gate); gates all 0; store_value + every untouched counter byte-identical. RESIDUALS
  characterized: field-extraction producers (throw_self envelope builders, JSON cursor field
  paths) — the documented conservative boundary; next-slice candidate. Report:
  /tmp/drift-announce/2026-07-09T230000Z-barch1b-value-position-stakes.md. Awaiting review.
- 2026-07-10: **B-arch-1c CHECKPOINT delivered (STOP before migration, per direction).**
  Baseline on merged 0.33.78+certified branch: byte-identical to after-1b on EVERY counter
  (value_position 20,103; C4 82,728; gates 0) — merge provably inert for B-arch. PREMISE
  CORRECTION: plain user field reads (self.field/obj.name/nested/ref-param/pkg) produce ZERO
  value_position residuals — probes p1-p5 all clean; copies materialize upstream of
  string_arc. Real residual producers (instrumented histogram, reverted after):
  StructGetField ~8.8k (synthesized Throw::throw_self envelope builders + StrictJsonCursor
  internals), cross-block/param-like ~5.8k, LoadRef/AddrOf* ~1.3k, trace Exc/ArrayDup.
  Safety argument: CopyValue at the exact point of today's late retain (view stays view;
  no new invalidation window vs 0.33.58 class; move decisions cannot flip — field-read dests
  are never movable today). Acceptance targets + regression plan (incl. Valgrind row for
  arr[i].name) in the checkpoint report:
  /tmp/drift-announce/2026-07-10T170000Z-barch1c-checkpoint-field-producers.md.
  Open impl question flagged: bare-storage "param-like" operands (one probe resolves it).
  Awaiting approval to implement.

- 2026-07-10: **B-arch-1c (MIR field/view producer stakes) IMPLEMENTED + audited.**
  Fn-wide producer resolution (SSA-sound) + `_is_string_value_view` rule per review
  guardrails (StructGetField String field_ty, LoadRef String inner_ty, LoadField String
  dest, bare storage operands; AddrOf* never staked; VariantGetField EXCLUDED per review —
  dest already owned, removal corpus-neutral; HResultOk/fresh terminal; store_value
  untouched). CORPUS (543/647,943 identical universe):
  **value_position_retain 20,103 → 0** (events −20,103 exactly; residual itemization
  vacuous); c2 = store_value 7,624 exactly; ALL hard gates 0; every other counter
  byte-identical. C4 UNCHANGED — conjecture corrected: remaining 82,728 are real-move
  shadows (no-op releases of zeroed slots at multi-path joins), release-elision territory.
  Pins 11/11 (throw_self+ASAN, cursor-loop+ASAN, ref-path+ASAN, arr[i].name +ASAN+VALGRIND
  definitely-lost-0, audit acceptance); batteries 445 passed/1 skipped; matrices 51/51+51/51.
  Report: /tmp/drift-announce/2026-07-10T210000Z-barch1c-field-view-stakes.md. Awaiting
  review; store_value (1d) and release-elision slices not started.

- 2026-07-10: 1c review round — BLOCKING finding fixed: VariantGetField removed from the
  view set (dest already owned per codegen/string_arc/ledger contracts, now cited at the
  site). Post-removal: 36/36 stake pins; corpus BYTE-IDENTICAL to pre-removal AFTER
  (vp_retain 0, c2=store 7,624, C4 82,728, gates 0) — no VariantGetField operand was ever
  staked; removal is contract hygiene, zero residuals to itemize.
- 2026-07-10: **B-arch-1d CHECKPOINT delivered (STOP before implementation, per direction).**
  Baseline: store_value_retain 7,624 = entire remaining C2; ~14 stdlib fns (json encode/log
  emit family). Probe ranking (throwaway, reverted): StoreLocal×LoadLocal ~3.3k,
  StoreLocal×ArrayIndexLoadUnchecked ~2.8k (NEW view kind), StoreLocal×ResultOk ~1.8k
  (Ok-payload PROJECTION, not ctor — 0.33.46-adjacent, extra pins planned),
  StoreRef×LoadLocal ~250, ArrayIndexStore 0. ORDERING PROOF: today's store expansion
  releases old-dest BEFORE retaining the new value — copy-before-release is strictly safer
  (closes the latent self-aliased-store window); site-4 drop_before_overwrite disjoint
  (destination side, untouched). Expected: store_value → ~0, C2 → 0 TOTAL, gates 0,
  C4/drift byte-identical predicted. Report:
  /tmp/drift-announce/2026-07-10T230000Z-barch1d-checkpoint-store-stakes.md. One scope
  decision flagged (include ArrayIndexStore despite 0 hits — recommended). Awaiting approval.

- 2026-07-10: **B-arch-1d (store-value stakes) IMPLEMENTED + audited — C2 INVENTORY CLOSED.**
  Store positions (StoreLocal/StoreRef/ArrayIndexStore sources) + view kinds
  ArrayIndexLoad[Unchecked] (element views) and ResultOk (Ok-payload PROJECTION — ctor
  stays terminal). Ordering: copy-before-release (strictly safer; self-aliased pin). OOB
  ArrayIndexStore: noreturn abort contract quoted + live pin (silent abort in nothrow main).
  CORPUS: store_value 7,624 → 0 (events −7,624 exactly); **c2_invisible_stake = 0 TOTAL
  (114,107 → 0 across 1a/1b/1c/1d)**; C4 82,728 + drift 28,265 byte-identical as predicted;
  gates 0. Pins 14/14 (overwrite +ASAN+VALGRIND, array stores +ASAN+VALGRIND, mut-ref,
  ResultOk both paths, self-aliased, OOB, audit); 0.33.46 matrix explicit 10/10; batteries
  459 passed/1 skipped; matrices 51/51+51/51. Report:
  /tmp/drift-announce/2026-07-11T010000Z-barch1d-store-stakes.md. Awaiting review.
  Remaining B-arch: release elision (~231k), C3 modeling, drift → B-repr(B5).
- 2026-07-10: 1d review round — audit acceptance made PER-SHAPE (shared `_audit_pin`):
  main (StoreLocal/ArrayIndexStore), set (StoreRef), probe (ResultOk projection). Rationale
  pinned in the helper: runtime rows cannot catch a rewrite regression (string_arc's
  late-retain fallback preserves behavior; only the audit sees it). 16/16. 1d ACCEPTED.
- 2026-07-11: **B-arch-1 CLOSED by maintainer** (C2 fully migrated). **Release-elision
  CHECKPOINT delivered as its own slice (report only).** Core thesis: the 0.27.145 blocker
  ("consulting the ledger for strings leaks") is structurally gone — B-arch-1 removed the
  wrong-MOVED_OUT class; every disagreeing scope-exit release now sits over a zeroed slot
  (UNINIT 136,407 elide; C4 moved-out 82,728 elide; PATH_DEPENDENT 11,951 KEEP unconditional
  — no string drop-flags). ~219k/259k emissions (84%) elidable. C3 (11,441) kept SEPARATE:
  different instruction/authority/fix-shape, byte-identical through all four stake slices
  (no visibility coupling), site 3 already skips flag-managed locals. Post-acceptance
  hardening proposed: retire the C4 allowlist into a failure class. Report:
  /tmp/drift-announce/2026-07-11T030000Z-release-elision-checkpoint.md. Decisions requested:
  Strings-now/Arrays-follow-up scope; PATH_DEPENDENT disposition; allowlist retirement.
- 2026-07-11: **Release-elision IMPLEMENTED + audited.** Prove-first items resolved BEFORE
  code: DropPolicy(String).needs_drop=True (no Copy-shortcut hazard — cheap-copy but
  drop-needing) and String tombstone ≡ _emit_zero_value (TOMBSTONED in elide set, proven).
  One guarded loop in string_arc's Return branch (strings analog of the destructible
  consultation; no-ledger → legacy; PATH_DEPENDENT/arrays/site-4/C3 untouched). CORPUS:
  scope_exit_release 259,351 → 40,216 (−219,135 = 136,407+82,728 EXACTLY, 84.5%);
  **c1_release_without_must_drop 0; C4 0 (retirement condition met — held for acceptance
  per decision 3)**; c1_agree +219,135 exactly; path_dependent 11,951 + C3 11,441 +
  drift 28,265 byte-identical (drift characterized: modeling artifact independent of
  emission — the clean B-repr(B5) input); gates 0. HISTORICAL BREAKERS 4/4 under valgrind
  (test_pkg_map_literal_string_leak — the 0.27.145 killer — now green with the consultation
  live: the thesis is empirical). Pins 10/10 (incl. live-at-exit leak-direction valgrind
  pin + mem.replace tombstone valgrind pin); batteries 461/1 skipped; matrices 51/51+51/51.
  Report: /tmp/drift-announce/2026-07-11T060000Z-release-elision-implemented.md. Awaiting
  acceptance; C4-allowlist retirement staged as immediate follow-up.
- 2026-07-11: **C4 allowlist RETIRED** (post-acceptance, per decision 3). Narrow reporter
  patch: both faces converted to UNCLASSIFIED (hard gate) with triage kinds —
  moved_out_release_regression_retired_c4 (release face) and
  return_retain_site3_regression_retired_c4 (extinct retain face); constant kept for
  historical-aggregate parsing; comments rewritten (no more counted-never-failed).
  Retirement pin added (synthetic MOVED_OUT-boundary release → UNCLASSIFIED, per-fn record
  force-emitted): reporter pins 7/7; elision+audit pins 12/12; 15-compile spot corpus:
  unclassified 0, no c4 key, gates 0 — output format unchanged beyond the retired bucket,
  full corpus not required. REMAINING B-arch: C3 flag-guarded modeling (11,441, allowlisted,
  byte-identical through six slices) or leave; Arrays release-elision follow-up;
  pre_post_verdict_drift 28,265 → B-repr(B5) planning input.
- 2026-07-10: **Full-suite BLOCKER fixed: 1d ArrayIndexLoad view-staking leak.** Two
  codegen-e2e memcheck failures (main_argv_content, array_extend_borrowed_source_string_
  no_uaf; exit 97, definitely-lost = one block per element load). ROOT CAUSE — 1d
  misclassified ArrayIndexLoad[Unchecked] as a borrowed element view; the codegen lowering
  RETAINS the extracted element (_lower_array_index_load[_unchecked] → _emit_copy_value →
  drift_string_retain), so the MIR dest is OWNED at extraction — VariantGetField's exact
  sibling (the shape the 1c review removed). The stake copied from the dest and orphaned
  the codegen +1. NOT the release-elision (bisected empirically: fails with elision off +
  AIL stake on; passes with elision on + AIL stake off) and NOT an array-authority gap —
  both hypothesized shapes ruled out. Exhaustive _emit_copy_value sweep: AIL and
  VariantGetField are the ONLY extraction nodes that retain at codegen; ResultOk is a bare
  extractvalue (true borrow — its staking stays). WHY 1d PINS MISSED IT: array pins used
  static literals; DRIFT_STRING_FLAG_STATIC no-ops retain/release and masks the imbalance
  (the e2e fixtures use heap strings by design). FIX: AIL[U] back to TERMINAL in
  _is_string_value_view with the owned-at-extraction contract comment; module docstring
  reverted; array store pins upgraded to heap strings. VERIFIED: both e2e fixtures green
  under memcheck; all stake+elision+reporter pins 63/63 (-n16); 15-compile spot corpus —
  no stake-retain site classes (C2 stays 0: AIL dest moves into its single consumer with
  no string_arc retain, matching pre-1d MIR), no c4 key, unclassified/untagged 0, gates 0,
  elision live. Report: /tmp/drift-announce/2026-07-10T000000Z-ail-stake-leak-fix.md.
- 2026-07-10: **Interlude — two DriftQuery LANGUAGE_BUGs (kept separate from the elision
  fix per direction).** (1) Arc<T>.get RecursionError on self-referential T: FIXED —
  visited-set guards on three call_resolver walks (_has_owner_typevar crasher +
  _has_unknown same-shape + _contains_foreign_typevar defensive); has_typevar left
  (structural early return). Pins test_arc_get_recursive_struct.py 2/2 (compile+run);
  checker/type_checker/traits/method_registry unfiltered 236/236. Report:
  /tmp/drift-announce/2026-07-10T120000Z-arc-get-recursive-typevar-walk-fix.md.
  (2) Receiver destroy atexit abort: CHECKPOINT ONLY per direction — root cause is NOT
  registry ordering; __drift_cb_drop_* runs user Destructible::destroy on zero-backed
  MOVED-OUT capture slots (Token probe: destroy-live then destroy-zeroed on the normal
  path, no channel). Receiver aborts because destroy dereferences the moved-from Arc
  sentinel (_arc_get_impl arc.drift:244 cap-0 bounds check). Options A (env drop flags,
  recommended) / B (zero gate, unsound) / C (stdlib moved-from guard, tactical) in
  /tmp/drift-announce/2026-07-10T130000Z-receiver-destroy-atexit-checkpoint.md. Awaiting
  decision.
- 2026-07-10: **Fix A implemented — CB-DROP liveness flags (0.33.79 candidate; cert held
  for this).** User agreed root cause + spec ruling; scope: compiler drop-glue only, no
  stdlib guard, no byte-sniffing. Mechanism: MOVE captures whose drop can reach a user
  destructor get trailing `__live{slot}` Int env fields (init 1; move-out stores 0 with
  the zero-back); _emit_callback_drop_thunk guards flagged slots; flag-free envs
  byte-identical. Matrix 11/11 (Token exactly-once, Receiver repro +ASAN+Valgrind,
  blocked-recv, join control, non-moved, conditional move both outcomes +Valgrind,
  pkg-mode signed-std). ABI assessed: stays 20 (bump was authorized, not needed — env
  blob interpreted only by own vtable thunks). history.md folded into 0.33.79 entry.
  Wide batteries (full e2e memcheck, stage2+memcheck, matrices) running at log time.
  Report: /tmp/drift-announce/2026-07-10T160000Z-cb-env-destructible-capture-flags.md.
- 2026-07-10: **Review round on fix A (blocking): predicate authority widened.**
  _drop_can_invoke_user_destroy now mirrors has_drop/codegen destructor authority:
  exact destructor_fns[ty] → is_destructible → (name, module_id) generic-nominal
  fallback (trait prover can miss cross-package generic instantiations). Explicit
  proof per review: instrumented pkg-mode consumer compile — Receiver resolves
  exact=True ∧ trait=True (existing pkg pin does NOT isolate destructor_fns-only; no
  deterministic in-tree shape does) → added pin 12: generic Wrap<T> Destructible moved
  out inside a boxed callback (core.callback0+spawn), destroy-exactly-once. Matrix
  12/12. Callback0 side-finding: both authorities False = correct (no user destructor,
  structural zero-safe drop). Targeted runs only per new rule.
- 2026-07-10: **Full-suite blocker: invalid LLVM IR (`multiple definition of 'raw116'`)
  — latent _fresh prefix-ambiguity, NOT channel semantics.** _fresh(hint) has no
  hint/counter separator; _fresh("raw1")@16 == _fresh("raw")@116. Env flag fields
  shifted temp counts into the collision. Exhaustive scan: 11 ambiguous pairs / 7
  families; all digit-suffixed hints renamed letter-terminated (22 sites); channel
  test/source untouched. NEW static pin test_fresh_hint_ambiguity.py (source scan,
  zero ambiguous pairs allowed). Verified in review-ordered sequence: failing test
  green → channel file + 12-pin callback matrix 14/14. Targeted only; full suite is
  the user's gate.
- 2026-07-11: **Block(timeout) admission IMPLEMENTED (runtime + stdlib), 10/10 pins.**
  Runtime: DriftExec gains saturation/submit_timeout_ms/FIFO ExecWaiter list +
  dedicated admit_cv (enqueue's single cv signal must reach a WORKER, never an
  admission waiter — lost-task-wakeup hazard found during implementation);
  try_admit_waiter_locked conditional transfer (admits only while queue+running <
  limit, wakes collected under mu and unparked after release); Block path in
  drift_exec_submit (park_until loop, done-flag arbitration, codes 0/1/2/3);
  shutdown drain (done=-1) before worker join. Stdlib: spawn_on code-3 →
  Err(CANCELLED) arm; BlockingExecutor + blocking_executor_builder (Block 5s/queue
  64/4 workers) + build_blocking_executor + spawn_blocking_on/run_blocking_on.
  ONE ownership finding vs design (fixed, not a stop-condition breach):
  drift_thread_drop's exactly-once cleanup branch requires !started && !exec, so
  h->exec must be assigned AT ADMISSION (in the transfer), not before the wait —
  premature assignment leaked the timed-out submission's callback (caught by the
  Destructible exactly-once pin). Pins (test_exec_block_admission.py, ALL real
  saturation): FIFO success-after-wait, timeout+Destructible exactly-once
  (+ASAN+Valgrind), ReturnBusy immediate, no-carrier-pinning, cancel→CANCELLED,
  yield/requeue no-over-admission, exit-with-waiter (+Valgrind). ABI 20 held.
  NEW SCOPE from review before facility completion: observability (liveness must
  identify stuck blocking work) — design proposal at
  /tmp/drift-announce/2026-07-12T010000Z-design-blocking-ffi-observability.md:
  wait-kind blocking-admission (extern-free), exec names + op labels + ffi
  enter/exit markers (3 new externs → ABI 21 + recert), compiler instrumentation
  of user-module extern "C" calls (recommended over manual markers), required
  label params, diagnosable-FFI docs/example contract. AWAITING approval; version
  bump + history deferred until the facility is complete per review stance.
- 2026-07-11: **Review round on admission (3 findings, all fixed/folded).** (1)
  BLOCKING — FIFO bypass: direct submit ignored wait_head when capacity was free
  (release→admit window), letting new submitters starve older waiters. Fix:
  submissions enter the wait path when waiters exist even with free capacity
  (`total >= limit || wait_head != NULL`), + belt-and-braces self-admission pass in
  both waiter legs (safe: done=1 observed via latched park token / while-condition
  before any wait). New starvation pin (waiter with 4s budget vs 12 sequential
  competing submissions) — matrix now 11/11. (2) MEDIUM — reactor wake keyed on
  admitted_any, not n_wakes (cond-admitted non-VT waiter still enqueues a task a
  poll-mode worker must see). (3) MEDIUM — observability design corrected: NO
  per-exec JSON exists today (walker reports first entry only, ~3328); design now
  specifies bounded execs[] (cap 64 + truncated flag) with stable registration-
  ordinal ids joined from VT wait/op records. ABI 20 still held for admission;
  observability slice remains the ABI-21 boundary.
- 2026-07-12: **Review round 2 on admission (4 findings).** (1) BLOCKING lost-wake in
  admit batch: wake-capacity now checked BEFORE pop/admit (peek-first; batch-full →
  wake this round, loop for the rest) — pre-fix an admitted waiter beyond wakes[8]
  slept to deadline/forever. Pin: 12-simultaneous-release many-waiter shape with
  timeout=0, all 10 waiters woken. (2) BLOCKING shutdown drain same cap: now batched
  rounds of ≤64 (pop under mu, unpark after unlock, loop until drained); registry
  prepend order verified (newest-first destroy → waiters unpark while their home
  executor is alive, noted at the site). Pins: 70-waiter exit (+valgrind). Matrix
  14/14. (3) MEDIUM observability data-race scheme added to design (§1.4): exec name
  immutable-before-registry-publish; op label publish-length-last atomic;
  FFI marker redesigned to ONE atomic pointer to a compiler-emitted rodata
  DriftFfiSite record (no torn triples, no lifetime coordination). (4) LOW/MED:
  submitter recorded as numeric DriftVt.submitter_vtid inside drift_vt_set_op — not
  string-encoded into the 47-byte label.
- 2026-07-12: **Review round 3: shutdown topology blocker + two lows — fixed, matrix
  16/16 + adjacent batteries 126/1skip.** (1) BLOCKING topology: cross-executor
  waiters (home exec A created AFTER blocking exec B → A destroyed first → B's drain
  unparks through freed A->mu). Fix has TWO parts, and the first attempt taught the
  second: a naive global prepass that set shutting_down everywhere DRAINED waiters but
  froze their home executors' workers, stranding resumed-waiter fibers parked → the
  fiber-stack-held submission state leaked (caught by the pre-existing exit valgrind
  rows). Final design: (a) new admission_closed flag — prepass closes Block admission
  on ALL executors (no new waiters anywhere) while workers KEEP RUNNING, then drains
  and unparks every waiter while all homes are alive; (b) worker shutdown-drain —
  workers exit only when the queue is EMPTY; during shutdown they drop never-started
  work (existing destroy semantics, cancelled-unstarted pattern) but RESUME started
  fibers so an unparked waiter deterministically runs its submit-failure unwind
  (vt_drop) regardless of destroy timing. destroy_internal's own drain stays as
  backstop. Pins: cross-exec topology exit (+valgrind), all prior exit rows green
  again. (2) phantom READY: drift_vt_set_ready moved from submit entry to the two
  actual enqueue points (direct + admission transfer) — a Block-waiting submission
  now snapshots as created/awaiting-admission, not READY. (3) doc example fixed to
  "op" + separate "submitter": 3. Worker-loop semantics change flagged for the
  report: queued never-started work at shutdown is now dropped by the DRAINING worker
  rather than destroy's post-join walk (same outcome, earlier point); started fibers
  now resume at shutdown (new, load-bearing for graceful teardown).
- 2026-07-12: **Review round 4: admission_closed made a FULL freeze — matrix 18/18.**
  Submit rejects shutting_down||admission_closed at ENTRY (before the free-capacity
  direct path); admit_waiters stops admitting once closed; stale prepass comment
  fixed (the shutting_down/admission_closed distinction IS the fix). New pin (12):
  shutdown-resumed waiter re-submits into a DIFFERENT not-full blocking exec →
  Err(BUSY), with negative markers proving no enqueue and no task ran (+valgrind).
  Challenge noted per user instruction: kept ONE deliberate scope-widening — the
  entry check also fail-fasts `shutting_down` on the direct path (previously
  enqueue-into-dying-queue, silently dropped by destroy's walk). Argued FOR: Err(BUSY)
  to the caller beats silent loss, kills a third behavior class, and the silent-drop
  shape is the 0.32.x shutdown-bug breeding ground. Flagged as the one behavior
  change beyond the finding's letter.
