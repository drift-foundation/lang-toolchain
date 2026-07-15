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
- 2026-07-12: **Observability slice IMPLEMENTED (ABI 20→21, version 0.33.80).**
  Runtime: DriftVt gains op_label(48, publish-length-last)/submitter_vtid/
  exec_id_word(stamped at every enqueue)/atomic ffi_site ptr; DriftExec gains
  exec_id(registration ordinal)/name(32, length-last)/wait_count. New externs
  drift_exec_set_name, drift_vt_set_op (records caller vtid as separate numeric
  submitter), drift_ffi_enter(site)/exit (single atomic ptr to compiler-emitted
  rodata DriftFfiSite {sym,file,line}). New wait kind blocking-admission (wait_id =
  exec id; deadline via extended timer correlation). Liveness walker: bounded
  execs[64] snapshot + per-VT op/submitter/exec/ffi; JSON + stderr top-running
  extended. Codegen: user-module extern "C" calls bracketed with enter/exit via
  cached per-callsite site constants; std.*/lang.*-declared externs + @intrinsic
  excluded (std.codec's drift_codec_* showed up in EVERY binary on the first cut —
  the intrinsic-only IR pin caught it). Stdlib: REQUIRED names/labels —
  build_blocking_executor(policy, name), spawn_blocking_on/run_blocking_on(exec,
  label, cb), new spawn_on_labeled. vt_set_op/exec_set_name intrinsics wired at BOTH
  codegen dispatch sites. Example examples/blocking_ffi (named exec + labeled op +
  named wrapper + --stuck; README carries expected USR2 output, verified live
  byte-for-shape incl. main.drift:33); justfile passes --allow-unsafe for it. Docs:
  drift-concurrency "Blocking FFI from virtual threads" + "Making blocking FFI
  diagnosable"; effective-drift entry. Findings during impl: deadline correlation
  had to include the new wait kind; completed VTs leave the registry (label-persists
  assert removed). Pins: test_blocking_ffi_observability.py (3: stuck-FFI USR2 names
  subsystem/op/submitter/extern-symbol/file:line in JSON+stderr with waiters>=1;
  marker clears after return; instrumentation scope IR pin). Targeted gate 60/60
  (admission 18 relabeled + obs 3 + cb-flags 12 + channel + ABI stamp + liveness
  interrogator suite). Full suite = user-run combined-facility gate.
- 2026-07-12: **Observability review round (3 findings, fixed+pinned).** (1) BLOCKING:
  DriftVt is malloc'd and the spawn init block never zeroed op_len/submitter_vtid/
  exec_id_word/ffi_site → walker could read garbage labels or a WILD ffi_site
  pointer. Zero-init added at spawn; pin: unlabeled VTs report no op/submitter/ffi.
  (2) MEDIUM: JSON printed user strings raw (%.*s) — quote/backslash/newline in a
  label/name corrupted the document. lv_json_escape helper (", \, \n, \r, \t,
  control → \u00xx) applied to exec names, op labels, AND ffi symbol/file; pin:
  hostile name 'sto"rage\demo\n' + label 'op"quote\back\nnl\tt' → json.loads
  succeeds and both round-trip exactly. (3) LOW/MED: stderr top-parked line now
  carries op= and wait=blocking-admission exec_id= (was wait_id-only — the queued
  blocking op was JSON-visible but not stderr-actionable); also added
  PARKED_BLOCKING_ADMISSION state name. Obs pins 4/4; targeted gate re-run green.
- 2026-07-12: **Owned-string audit round — challenged and RESOLVED in favor of the
  shadows (user accepted).** Full-suite audit flagged the two new String-taking
  externs. Initial review suggestion (Convention-B allow markers) was challenged:
  both stdlib call sites pass `move` (caller cannot release after — B's defining
  shape doesn't apply); decisive heap-string valgrind probe (heap exec name + heap
  label ×5, shadows in place) = 0 errors 0 leaks — B-misclassification would have
  double-freed, A-without-shadow would have leaked. Kept DRIFT_OWNED_STRING shadows;
  string_runtime.h convention section reworded (intrinsic-ness does NOT decide the
  convention, the Drift-level call site does; stale site list replaced by
  audit-authority + grep); NEW both-directions pin test_heap_labels_balanced_valgrind.
  Audit + obs file 11/11; earlier gate (audit+obs+admission) green.
- 2026-07-12: **Two full-suite harness fixes (neither a design concern, per review
  agreement).** (1) reactor stale-fd whitebox TU (#includes thread_runtime.c
  standalone) gained an undefined drift_string_release from the DRIFT_OWNED_STRING
  cleanup sites — added the sibling test-only stub (resolver-only whitebox never
  exercises the externs). (2) test_heap_labels_balanced_valgrind lacked the standard
  asan_active skipif the file's other valgrind rows carry — failed under DRIFT_ASAN=1
  via the known ASAN/valgrind collision, not an ownership defect (0 leaks/errors in
  its own log). Verified: both green in normal lane; heap pin SKIPS in ASAN lane.
  Full suite restart is the user's.
- 2026-07-12: **Stale e2e fixture: concurrent_queue_limit_enforced (full-suite catch,
  agreed not environmental).** Raw exec_create(..., saturation=0) fixture encoded the
  pre-admission behavior where saturation was (void)-ignored and full always returned
  busy; with Block real (0 = Block per shipped encoding), the second submit is now
  legitimately admitted after the occupant finishes → fixture returned 1. Fixed to
  saturation=1 with the positional encoding documented at the call — kept RAW
  (challenged the builder suggestion: this is the only e2e coverage of the intrinsic
  numeric ABI; builder path already has the 18 admission pins). expected.json
  description updated. Fixture green. Sweep: no other raw exec_create callers.
- 2026-07-13: **Cleanup Slice 1 IMPLEMENTED (branch refactor/string-authority-cleanup;
  plan tightened 5/5 per review before coding).** 1a: owned-at-extraction contract —
  marker discipline at all 8 _emit_copy_value sites in llvm_codegen (3× owned-at-
  extraction: ArrayIndexLoad/ArrayIndexLoadUnchecked/VariantGetField — three nodes,
  two families; 5× copy-construction) + matching markers at string_stakes terminal
  sites; NEW test_extraction_retain_contract.py (3 pins, Python ast, fail-closed:
  unmarked site fails; larger-than-expected extraction set fails with explicit
  STOP/REPORT message; extraction∩view-isinstance must be empty, tie checked both
  directions). 1b: NEW tools/drift_corpus_audit.py v1.0.0 (aggregate.json comparable/
  sorted/volatile-free; manifest.json universe identity incl. success partition;
  metadata.json volatile-only; --baseline: universe equality→exit 2 on mismatch,
  exact-delta table, hard gates nonzero on NEW side→exit 1) + 4 pins in
  lang/tests/tools/ (stability/determinism-byte-identical/universe-mismatch/
  gate-failure; one test fix: naive volatile substring check false-positived on
  c1_path_dependent → structural key check). Slice battery 7/7. No compiler-semantics
  changes (comments + new files only). Full-corpus baseline running; reference record
  (manifest sha256 + aggregate table + command + tool version) appended on
  completion. Report: /tmp/drift-announce/2026-07-13T010000Z-cleanup-slice1-
  guardrails-tooling.md.
- 2026-07-13: **Slice 1 review round (5 static findings, addressed; tool v1.1.0).**
  (1) scratch rooted via session_root(base=build/tmp) + explicit dir= (never bare
  /tmp); (2) shlex.split for --driftc-args; (3) whole-directory fixture hashes
  (companion modules/C helpers/expected.json covered); (4) explicit compile-only
  single-unit inclusion rule embedded verbatim in every manifest + rule-excluded
  fixtures recorded with reasons (module_paths/c_sources shapes) + new exclusion
  pin; (5) issues/blocking-executor-missing-from-concurrent-exports kept OUT of
  this slice — flagged to user (pub-but-not-exported: type unnameable cross-module,
  blocks drift-query Slice 12; functions resolve, type doesn't). Battery 8/8.
  Baseline restarted on v1.1.0 (v1.0.0 run discarded, superseded manifest schema).
  Incident note: a pkill pattern self-matched the invoking shell again (exit 144) —
  the edits were re-applied and verified; reinforces the standing rule.
- 2026-07-13: **Slice 1 review round 2 (blocking authority inversion + 2 fixed; tool
  v1.2.0).** (1) BLOCKING: the contract pin trusted markers as classification
  authority — a future retaining extraction mislabeled `# copy-construction` passed
  silently. Rewritten: the authority is the AST-INFERRED instruction context per
  call site (innermost isinstance(instr, X) branch body → else `instr: <Node>`
  param annotation → else helper identity), classified against an explicit
  reviewed table (EXTRACTION_CONTEXTS / CONSTRUCTION_CONTEXTS); markers only
  DOCUMENT and must agree; unclassified contexts fail with the STOP/REPORT text;
  multi-node isinstance branches deliberately surface as unclassified. NEW teeth
  test: doctored copy with the ArrayIndexLoad site relabeled copy-construction →
  context authority still classifies extraction AND reports the marker
  disagreement. (2) MEDIUM: non-empty --out now refused (exit 2) — stale
  audit/<fixture>.jsonl from a prior run could be aggregated as current results;
  new reuse pin. (3) unused fn_count removed. Battery 10/10 (contract 4 + tool 6).
  Baseline restarted on v1.2.0 (v1.1.0 run superseded).
- 2026-07-13: **Slice 1 review round 3 (medium; tool v1.3.0).** Standalone baseline
  acquisition could return 0 with nonzero hard gates (gates were only checked
  inside _compare) — a reference baseline could silently bless a regression for
  the phase. Shared _hard_gate_failures(counters) helper now applied BOTH after
  writing a standalone run (exit 1; with --baseline the delta table still prints
  first) and on the new side of comparisons. Gate pin extended: clean counters →
  no failures; tampered counters → comparison exits 1 AND the same helper flags
  standalone acquisition. Battery 10/10. Baseline restarted on v1.3.0.
- 2026-07-13: **Slice 1 review round 4 (medium; tool v1.4.0).** In --baseline mode
  the standalone gate check ran BEFORE _compare, so universe-mismatch + gate-failure
  returned 1 with _compare's exit 2 ignored — breaking the mismatch-dominates
  contract. Fixed to the reviewer's shape: baseline mode delegates entirely to
  _compare (which owns the ordering: universe equality → delta table → new-side
  gates); the standalone helper path applies only without --baseline. NEW pin:
  mismatched universes + tampered gate → exit 2. Battery 11 pins green (contract
  4 + tool 7). NOTE: the running reference baseline stays on v1.3.0 — v1.4.0
  changes ONLY the --baseline branch; standalone acquisition semantics are
  byte-identical, recorded here to keep the reference citation honest.
- 2026-07-12: **0.33.81 merge verified + Slice 1 REFERENCE BASELINE recorded.**
  Merge verification accepted (payload = certified hotfix only: std.concurrent
  exports + e2e timeout scaling + versions/history/tests; Slice 1 files intact;
  battery 11/11 on merged tree). Deleted
  issues/blocking-executor-missing-from-concurrent-exports/ from this branch —
  resolved in 0.33.81, repros promoted to
  lang/tests/driver/test_blocking_executor_exports.py on main.
  **Reference baseline (slices 2-4 compare against this via --baseline):**
  - command: `PYTHONPATH=. .venv/bin/python tools/drift_corpus_audit.py --out build/tmp/cleanup-baseline -j 16`
  - tree: refactor/string-authority-cleanup with 0.33.81/ABI 21 merged (post issue-dir deletion; deletion does not affect the corpus universe)
  - tool version: 1.4.0
  - manifest sha256: 8dceddd56d770b9bac1a14949670a097961df0087a56fcfeb220c98915ae72ce
  - universe: 924/1268 fixtures compiled (344 compile-failed, 49 excluded by rule)
  - hard gates: ZERO (unclassified / untagged / c1_must_drop_without_release /
    post_ledger_build_failed all absent; standalone gate check passed, exit 0)
  - aggregate counters (fixtures_compiled: 924):
    | counter | value |
    |---|---|
    | c1_agree | 882371 |
    | c1_path_dependent | 20384 |
    | c2_invisible_stake | 1 |
    | c3_moveout_not_owned | 19504 |
    | c3_moveout_owned | 1835400 |
    | events | 2775744 |
    | fns | 1107693 |
    | pre_post_verdict_drift | 48178 |
    | site_class:drop_before_overwrite_site4 | 14 |
    | site_class:moveout_expansion | 1854904 |
    | site_class:overwrite_release | 233519 |
    | site_class:scope_exit_release | 68562 |
    | site_class:store_value_retain | 1 |
    | site_class:temp_lastuse_release | 618744 |
  This supersedes the v1.3.0 in-flight citation from review round 4. Next:
  Slice 2 Part 1 (C3 decision checkpoint, report-only) awaiting clearance.
- 2026-07-12: **Slice 2 Part 1 — C3 decision checkpoint COMPLETE (report-only);
  STOPPED for arm selection.** Report: C3-DECISION-REPORT.md. Headline: the
  plan's premise was wrong — C3 (19,504, detail records uncapped = complete
  census, 91 distinct sites, 18 stdlib sites = 99.5% of volume) is FIVE
  populations, and model-vs-allowlist as framed only decides population A
  (8,316 flag-guarded `*_cleanup_drop_*` moves). B = 8,384 UNGUARDED inline
  cleanup drops of zero-tag-drop-safe variants at PATH_DEPENDENT points (all
  `__cleanup_t*` compiler-authored; child_sp/cr); C = 1,852 cleanup moves in
  statically-DEAD catch blocks (`try <nothrow fields.get> catch` in
  std.json::JsonNode::get / JsonObject::get — dispatch has no CFG preds);
  D = 945 zero-init-as-empty-value immediately moved (tombstoned;
  std.log::log_context attrs + __maplit temps); E = 7 re-moves of moved-out
  locals/binders across 5 fixture sites — NOT root-caused, must stay
  divergent pending individual triage. Conditional-ownership event model IS
  representable (edge-refined dataflow on drop-flag branches, ~100-150
  lines) but the shared-authority feedback means refined states change
  release-elision/site-4 emission — "everything else byte-identical" is NOT
  achievable under the model arm with a shared ledger. Recommendation:
  A → structural recognition (retired-C4 discipline), flag-refinement
  recorded as a future emission slice with its own acceptance; B+D →
  `c3_moveout_zero_safe` reporter comparison fix; C → unreachable-block
  filter (+ stdlib dead-try/catch hygiene flag, separate thread); E →
  triage first. Expected movement: 19,504 → 7 residual, exact balance per
  population. Side-results (valgrind probes, scratchpad only): throwing
  calls DO get explicit call_err→dispatch MIR edges — reachable catch
  blocks have correct ledger state, heap-string-in-catch probes clean (no
  release-elision leak concern); the plan's 11,441 was a stale smaller-
  corpus generation of the same populations. No in-tree changes; nothing
  staged.
- 2026-07-13: **Slice 2 Part 2 IMPLEMENTED — hybrid C3 classification; acceptance
  EXACT.** Per the approved hybrid arm selection on C3-DECISION-REPORT.md:
  reporter-side only (`ownership_ledger_reporter.py` C3 ladder + `string_arc.py`
  wiring), NO ledger/lattice change, NO emission change.
  - A → `c3_moveout_flag_guarded`: STRUCTURAL verification in the reporter
    (retired-C4 discipline — MIR shape, never a name/count): MoveOut at idx 0
    feeding an immediately-following DropValue (snapshotted at note() time from
    the SOURCE stream, since finalize runs post-rewrite), single predecessor
    entering via IfTerminator whose cond loads the subject's OWN
    `_drop_flag_for_local` flag.
  - B+D → `c3_moveout_zero_safe`: raw TOMBSTONED (lattice's own drop-safe-bytes
    guarantee) OR raw MAYBE_UNINIT + authored MoveOut→DropValue pairing +
    `variant_zero_tag_drop_safe` (the same predicate cleanup_authoring used to
    choose the unguarded arm). Both legs required.
  - C → `c3_moveout_unreachable_block` (observational; renamed from the report's
    placeholder `unreachable_block_event`): event block absent from
    l_pre.block_in ⇔ never reached by the CFG walk.
  - E: raw MOVED_OUT intentionally NOT normalized — stays divergent.
  Fail-closed: finalize's new `func`/`zero_safe_ty` inputs are optional; without
  them every non-LIVE MoveOut classifies as before (divergent), never as agree.
  Pins: +5 in test_string_arc_audit_reporter.py (A agree; A-teeth wrong-flag
  divergent; D tombstoned agree; C unreachable observational; B ladder — both
  legs required, either missing → divergent, AND an E-shaped MOVED_OUT with both
  legs true stays divergent). Batteries: reporter 12/12; stage2+guardrails 336/336.
  **Pre-change reference (0.33.82 merge corpus-neutral):** run
  `build/tmp/cleanup-prepart2` vs recorded Slice-1 baseline → universe accepted,
  ALL 14 counters +0 (merge changed nothing observable). manifest sha256
  fed2559af4798246a149d2f33cc1f7d1c148b5bdc1518ef6b8121ddaedf5ff64.
  **Acceptance run** (`--out build/tmp/cleanup-part2 --baseline
  build/tmp/cleanup-prepart2 -j 16`, tool v1.4.0, exit 0): movement EXACTLY as
  predicted — c3_moveout_not_owned 19,504 → 7 (−19,497); +8,316 flag_guarded;
  +9,329 zero_safe (B 8,384 + D 945); +1,852 unreachable; every other counter
  +0 (events/fns/site classes byte-identical — reporter-only by construction);
  hard gates zero. Residual 7 verified = EXACTLY the population-E events from
  the decision report (same fixtures/subjects/points). No new population found;
  no STOP trigger fired.
  **Future-slice record (per instruction):** edge-refined flag-aware ledger
  modeling (population A's 2A arm) is EXPLICITLY out of this bookkeeping slice —
  it changes release-elision/site-4 emission and needs its own predicted-delta
  acceptance table + memcheck gate. Logged in CLEANUP-EXECUTION-PLAN.md addendum.
  Next: STOP for review; then Slice 3 (Array measurement, report-only) and the
  E-population triage (7 events, 5 sites) as separate work.
- 2026-07-13: **FOLLOW-UP (open, tracked; not introduced by Slice 2): single
  invisible stake contradicts the "C2 fully closed" narrative.** The corpus
  carries `c2_invisible_stake = 1` / `site_class:store_value_retain = 1` — one
  event, located: fixture `rest_health_spawn_cb_iface`, fn
  `m::__lambda_cb_start_server_0_0` (hidden callback lambda), point
  `entry[12]`, subject `.t10` (an SSA TEMP, not a named local — note the C2
  visibility test can never classify an SSA-temp subject as visible, since
  `tracked_locals` holds named locals only; so this is either a genuinely
  un-migrated store_value stake shape in hidden-lambda entry blocks OR a C2
  operationalization artifact for temp subjects). Already present in the
  recorded Slice-1 reference baseline (2026-07-12, pre-0.33.82), so it entered
  between the B-arch C2=0 acceptance (0.33.79) and Slice 1 — possibly via a
  fixture/lambda-lowering change in between. Does NOT affect the Slice 2 C3
  acceptance (all C2/site-class counters +0 across the slice) and is NOT a
  hard gate. ACTION before any future "invisible-stake program remains closed"
  claim: root-cause this one event (B-arch triage method: per-site MIR dump +
  heap-string probe if it turns out to be a real orphaned stake).
- 2026-07-13: **C2 singleton RECONCILED (report-only triage).** Classification:
  a REAL store-value stake emission — but a runtime NO-OP — surfaced by
  UNIVERSE GROWTH, not by merge/mainline and not a tooling artifact.
  - Exact site: `rest_health_spawn_cb_iface` (fixture since 2026-03-24),
    `m::__lambda_cb_start_server_0_0`, entry[12]. Shape:
    `captures(move <String>)` in a spawn_cb lambda → prologue materializes the
    capture (stake-CopyValue → StoreLocal → MoveOut) then ZERO-BACKS the env
    slot: `StoreRef(env_field, ZeroValue .t10)`. string_arc's StoreRef arm
    `_ensure_owned`s the stored value; an input-stream ZeroValue dest is never
    in `owned_values` → it emits `StringRetain(.t10)` — a retain of
    compile-time-zeroed String bytes, i.e. a no-op. Surrounding semantics
    verified CORRECT in the MIR (body's copy staked +1; overwrite-release
    takes the env slot's ref at zero-back; cb_drop sees a zeroed slot).
  - Why the "C2 = 0 TOTAL / CLOSED" claim didn't see it: B-ARCH-0-INVENTORY's
    own corpus caveat — B-arch coverage was 543/1,317 e2e cases
    (session-local scripts); the slice-1b tool's universe is 924/1,268. The
    closure claim was true FOR ITS CORPUS; the widened universe surfaced one
    uncovered shape. Narrative correction: "C2 closed" must be read as
    closed-over-543; the widened-universe residual is exactly this one no-op.
  - LOAD-BEARING for Slice 4a: the plan tripwires string_arc's store_value
    fallback as unreachable — with this event live, that tripwire WOULD FIRE
    on this fixture. Reconciliation is therefore a 4a PREREQUISITE.
  - Recommended next action (NOT implemented — emission change, out of
    report-only scope): tiny mechanical string_arc classification fix — treat
    input-stream ZeroValue dests as owned/no-stake-needed in the store paths
    (zeroed bytes transfer nothing; the retain is a no-op). Expected corpus
    delta: store_value_retain 1→0, c2_invisible_stake 1→0, one dead retain
    instruction removed; acceptance via the tool (exact delta) + memcheck on
    the capture-fixture family. Fits the ownership-change bar as mechanical +
    restores the C2=0 invariant 4a depends on. Alternative (doc-only caveat +
    tripwire carve-out) rejected as worse: keeps dead emission and complicates
    4a's unreachability claim.
- 2026-07-13: **Slice 3 COMPLETE (Array measurement, report-only) — STOPPED per
  plan.** Report: SLICE3-ARRAY-MEASUREMENT.md. Instrumentation: reporter-side
  `note_array_drop` inventory (separate from string events — `events` counter
  untouched by construction) + string_arc note site at the Return-boundary
  `_drop_all_arrays` sweep; counters `site_class:scope_exit_arraydrop` /
  `arraydrop_state:*` / `arraydrop_verdict:*` (counted-only, never gates).
  Pin +1 (mix + inertness); reporter 13/13; stage2+guardrails 337/337.
  Corpus (`build/tmp/cleanup-slice3` vs cleanup-part2, exit 0): EVERY
  pre-existing counter +0 (inertness proven) + the new mix: 156,308 swept
  drops = 141,391 uninit (90.5%) + 10,297 moved_out (6.6%) + 4,620
  maybe_uninit (3.0%) + **0 live / 0 tombstoned**; verdicts 151,688
  must_not_drop (97%) + 4,620 path_dependent + **0 must_drop**. Probe-verified
  structural explanation for live=0: live arrays never reach the sweep —
  cleanup_authoring owns their drops (inline MoveOut+DropValue → string_arc's
  `moved_out_locals` fold skips them) and return sources are alias-walk
  skipped; the sweep is a legacy backstop firing only where the block-path
  `moved_in` tracking can't prove death, and the lattice proves ALL of those
  emissions dead or path-dependent. Recommendation: GO — Array
  release-elision as its own future emission slice (predicted delta
  scope_exit_arraydrop 156,308 → 4,620, strings byte-identical, memcheck
  in-gate; must re-verify no array analog of the 0.27.145 return-retain
  hazard). NO implementation done. Slice 4a note: its store_value tripwire
  remains gated on the C2-singleton fix (see reconciliation entry above).
- 2026-07-13: **Slice 3 review round 1: note-site coverage pin added.** The
  direct-API pin didn't exercise the string_arc NOTE SITE; new
  `test_arraydrop_note_site_covers_return_sweep` runs insert_string_arc
  (audit env on) over a func with three real Array<String> locals: uninit
  (swept, uninit/must_not_drop), zero-init-stored (swept,
  tombstoned/must_not_drop), moved-in sink (swept, live/must_drop) — and a
  moved-out local proven NOT recorded (string_arc's moved_out_locals fold
  skips it; scope_exit_arraydrop == 3 exactly). Reporter 14/14;
  stage2+guardrails 338/338. No non-test changes in this round.
- 2026-07-13: **C2-singleton FIX landed (Slice 4a prerequisite) — invisible-stake
  program closed over the FULL tool universe.** One-classification change in
  `string_arc.py`: String-typed input-stream ZeroValue dests join
  `owned_values` (zeroed String bytes are a valid owned empty value; retain and
  release are both runtime no-ops), so the store paths'
  `_can_move_owned_once` takes the direct no-retain route for the
  `captures(move <String>)` env-slot zero-back. Nothing else touched.
  Corpus acceptance (`build/tmp/cleanup-c2fix` vs cleanup-slice3, tool v1.4.0,
  exit 0): c2_invisible_stake 1→0, site_class:store_value_retain 1→0, and
  events 2,775,744→2,775,743 (−1 — ARITHMETICALLY ENTAILED: the removed retain
  WAS one event; a delta where the site-class dropped but events stayed flat
  would be impossible). EVERY other counter +0, hard gates zero. C2 is now 0
  over the 924-fixture universe — the narrative caveat from the reconciliation
  entry is retired.
  Pins: `test_zerovalue_store_needs_no_stake` (zero-back StoreRef → no
  store_value_retain / no C2; overwrite releases untouched at 2) + NEW
  memcheck carrier `test_spawn_cb_move_capture_zero_back.py` (HEAP-string
  move-capture across 8 spawn/join cycles; over-retain → definitely-lost,
  over-release → Invalid read/free; deliberately NOT an e2e fixture so the
  corpus universe stays fixed mid-phase). Batteries: reporter 15/15;
  stage2+guardrails 339/339; FULL memcheck suite 97 passed / 1 skipped
  (emission-change gate). Slice 4a's store_value tripwire is now UNBLOCKED.
- 2026-07-13: **C2-fix review round 1: vacuous-pin finding fixed.** The
  zero-back unit pin's ZeroValue temp `%z` was not in func.local_types, so
  `_ensure_owned` early-returned on `_is_string_value` and the pin passed
  WITHOUT the fix (no teeth). Fixed: `%z: String` added to the synthetic
  func's local_types with a comment explaining the metadata is intentional
  (production HIR lowering records ZeroValue dest types; the regression
  depends on it). Teeth PROVEN: with the ZeroValue-owned classification
  temporarily disabled the pin now FAILS; restored, battery 15/15. No
  non-test changes in this round (string_arc diff unchanged at +13).
- 2026-07-13: **Slice 4a LANDED — store_value fallback fail-closed tripwires +
  a MAJOR mid-slice finding.** Deliverables:
  (1) ENUMERATION (report req.): stake fallback classes corpus-zero in the
  924-fixture universe post-C2-fix — store_value_retain (tripwired THIS
  slice), call_arg_retain, value_position_retain (candidates for later 4a
  rounds), return_retain_site3 (already loud via retired-C4 UNCLASSIFIED),
  destructor_self (structurally unused).
  (2) TRIPWIRE: `_dead_stake_tripwire` in string_arc — structured AssertionError
  (site-class, fn, block[idx], value, store target, best-effort producer,
  report path issues/string-arc-dead-stake-tripwire/) at the three
  store_value _ensure_owned sites, converted at a NEW driver boundary wrap
  (string_arc loop in compile_stubbed_funcs) to a clean
  `internal: string ownership stake contract failure (…)` diagnostic with
  best-effort span — never a Python traceback.
  (3) FINDING (initial fail-closed conversion fired CLI-wide): the fallback
  was NEVER uniformly dead — `_ensure_owned` has TWO historical arms:
  PROVEN-String → retain (the corpus-zero dead part) and NO-type-metadata →
  silent pass-through (LIVE, exercised constantly — e.g. can-throw call
  Ok-payload holders `__call_ok` stores). "store_value_retain = 0" only ever
  measured the RETAIN arm. Tripwires now guard exactly the retain arm;
  untyped values keep the historical pass-through. Deletion note for 4a′:
  only the RETAIN emission is deletable; the pass-through is load-bearing.
  (4) ADJACENT CONTRACT GAP FIXED (surfaced by the over-broad first cut):
  string_arc's producer chain classified ArrayIndexLoad dests as VIEWS
  (owned_values.discard) and did not handle ArrayIndexLoadUnchecked AT ALL —
  contradicting the owned-at-extraction contract (codegen retains; the
  slice-1 pin enforces it for codegen+string_stakes but string_arc's chain
  predated it). Both now classify owned-at-extraction (mirroring the
  VariantGetField arm, NOT move_only). Latent 1d-leak shape; corpus-neutral
  on the CLI path (proven by acceptance below). Plus pre-scan local_types
  registration for String ZeroValue/AIL[U] dests (instruction-carried types)
  so metadata gaps cannot false-fire the tripwire (use_counts skips untyped
  values → _can_move_owned_once could never approve).
  (5) PINS: test_dead_store_value_stake_tripwire_fires (message stability:
  all structured fields), test_tripwire_surfaces_as_clean_internal_diagnostic
  (in-process compile with injected failure → clean phased diagnostic, no
  IR, no traceback), test_c2_invisible_stake_classifier_still_covered
  (C2 coverage moved off the now-fail-closed fallback);
  _string_shuffle_func reworked (per-store owned producers — the old
  double-store shape is the tripwire trigger now).
  ACCEPTANCE (build/tmp/cleanup-4a vs cleanup-c2fix, tool v1.4.0, exit 0):
  EVERY counter +0, identical universe partition, store_value_retain and
  c2_invisible_stake remain 0, hard gates zero. NO real corpus fixture trips
  (the early CLI firings were the over-broad first cut, resolved by the
  retain-arm refinement + AIL contract fix — not by weakening the guard on
  actual retains). Batteries: stage2 FULL 331/331; memcheck 97+1skip;
  guardrails+ICE pins 16/16. Next per user: E-population triage (7 events)
  BEFORE Array elision.
- 2026-07-13: **Slice 4a review round 1 (blocking + 2 addressed).**
  (1) BLOCKING — the AIL/AILU owned-at-extraction string_arc fix was
  unpinned (the slice-1 static pin covers codegen+string_stakes, not
  string_arc's producer chain). Two new pins, dests DELIBERATELY not
  pre-seeded in local_types so the instruction-typed pre-scan is exercised:
  checked ArrayIndexLoad single-use store → no tripwire, no
  store_value_retain, no C2 (pre-fix VIEW classification sent this into the
  tripwire); ArrayIndexLoadUnchecked ditto + asserts the pre-scan registered
  the dest's String type (pre-fix the instruction was wholly unhandled and
  the dest invisible to ownership — silent pass-through either way, so the
  metadata assertion is the teeth for that half). Teeth PROVEN empirically:
  with the classification+pre-scan temporarily reverted both pins fail;
  restored, string_arc byte-identical to the committed state.
  (2) MEDIUM — the tripwire's report path now exists:
  issues/string-arc-dead-stake-tripwire/description.md (meaning, required
  repro info, triage starting points; intentionally repro-free intake).
  (3) Boundary wrap phase renamed mir_validate → string_arc (phase strings
  are free-form; only non-None is enforced).
  Battery: reporter 20/20; stage2 FULL 333/333.
- 2026-07-13: **E-population triage COMPLETE (report-only) — STOPPED for
  scheduling.** Report: E-POPULATION-TRIAGE.md. The 7 residual
  c3_moveout_not_owned events = THREE shapes; TWO are confirmed
  LANGUAGE_BUGs from valid source with SILENT VALUE CORRUPTION (zeroed
  reads), explicitly called out per instruction:
  (1) re-match of a consumed match scrutinee (3 events,
  match_stmt_nested_match_last_stmt) — outer match MoveOut+zero-back, inner
  match reads zeroed storage; PROBE: Ok(5) rematch binder reads 0; non-Copy
  String payload also accepted and reads empty. Checker/lowering mismatch.
  (2) use-after-move of a non-Copy error binder passed BY VALUE (3 events,
  three std_io fixtures) — `pub error` IoError by-value param moves; checker
  fails to reject the second call; zeroed error → the fixtures' would-block
  path RETURNS 4 TODAY (masked by EOF-first test env). Control probe: plain
  bitcopy struct lowers as LoadLocal (no move) — the move is correct, the
  missing rejection is the bug. Plus stdlib API smell: is_eof_error /
  is_would_block_error should take &IoError.
  (3) authored cleanup drop of an explicitly-moved catch binder (1 event,
  catch_binder_visible_in_arm) — runtime-safe dead drop of zeroed error;
  recommend reporter-side drop-paired MOVED_OUT zero-safe extension
  (cannot mask shapes 1-2: their consumers are not drop-paired), emission
  fix optional later.
  RECOMMENDATION: fix shapes 1-2 as a dedicated regression-first slice
  BEFORE Array elision (issue bundles + failing pins from the scratchpad
  probes); expect the checker fix to REJECT the four carrier fixtures
  (latent invalid source) → fixture rewrites + corpus reference re-record
  in the same slice. No permanent allowlist for any of the 7; end-state:
  c3_moveout_not_owned → true 0, then promote to hard gate.
- 2026-07-13: **E-fix slice IN PROGRESS — STOPPED on stdlib fallout scope
  decision (tree RED, nothing committed).** Trigger scan recorded: implicit-
  move registry entry = considered, NOT fired (fixes restore rejection, never
  accept implicit moves). Root causes: shape 2 = `_walk_expr_for_borrowed_
  boundaries` never walked match `arm.block` (the explicit-move gate + all
  borrowed-arg boundary checks skipped every call inside statement-form match
  arms; `_lower_call_arg`'s MoveOut backstop silently consumed); shape 1 =
  borrow checker HMatchExpr never tracked scrutinee consumption (lowering
  moves + zero-backs non-copy scrutinees). Decided semantics: by-value match
  of a non-Copy PLACE scrutinee consumes; later uses reject E_USE_AFTER_MOVE;
  borrow to preserve; Copy scrutinees keep the lowering copy branch;
  projected scrutinees excluded (partial-move rule) + flagged for audit.
  Implemented: both fixes + io predicates → &IoError ×4 + fixture-1 rewrite +
  5 regression-first pins (4 verified failing pre-fix); borrow suite 90/90.
  ALSO surfaced (probe-verified, follow-up candidate): ConstShare synthesis
  qualifies fields against the DECLARING module's import-visible world — an
  import-less module silently cannot derive ConstShare for its error types.
  BLOCKER: the restored gate exposes 49 stdlib sites / 9 modules
  (JsonErrorData ×20, RegexError ×11, RegexNode ×6, ConcurrencyError ×6,
  LoggerRuntimeState ×2, Utf8Error/SourceError/Token<K>/CliError ×1) — every
  driftc run fails until swept; each site needs eyeball verification (the
  double-use ones are MORE latent zero-read bugs). Decision menu in
  E-POPULATION-TRIAGE.md fix-slice section: (a) sweep all 49 in-slice
  (recommended), (b) split, (c) ConstShare grandfathering (rejected).
- 2026-07-13: **E-fix slice COMPLETE (option (a) executed) — three
  LANGUAGE_BUGs fixed; ruling recorded; stopped for review.**
  RULING (blocking clarification resolved; recorded in
  doc/refactor_triggers.md under the implicit-move entry): considered — NOT
  fired — with the match SCRUTINEE recorded as the language's ONE deliberate
  implicit-consume position (pre-existing, now TRACKED via borrow-check flow
  state + pinned; "pattern-match consume" bullet addressed: it targets NEWLY
  ADDED positions; sharpened fire condition: a second implicit-consume
  position or another capture-slot mis-route fires it).
  THIRD BUG (found by the ruling probe, live on certified 0.33.82): match on
  a MOVE-CAPTURED non-Copy scrutinee read ZEROED payload (arm consume
  targeted the never-materialized local; dispatch was capture-aware) — fixed
  by routing `_ensure_arm_scrut_ptr`'s consume through
  `_move_from_callback_capture_slot` (tombstone write-back skipped on that
  path); valgrind-clean; pinned.
  SWEEP: 49/49 stdlib sites = single terminal transfers → `move` spelled
  (table in E-POPULATION-TRIAGE.md); 0 predicates (io family → &IoError
  earlier), 0 stdlib double-uses. FIXTURES: 8 rewritten total — the 4 known
  carriers + cleanup_err/json_like_key/loop_err_return/result_err_convert
  (Err(e) re-wraps → move), std_time_iso ×2 (Ok(ts) → move), and
  match_yield_qualified_ctor = ANOTHER shape-1 latent bug (inner re-match of
  consumed zero-payload scrutinee, passed only because Block() is tag 0) →
  fresh inner scrutinee.
  PINS 7/7 (test_match_consume_and_arm_call_gate.py): arm-body gate
  restoration; re-match rejection ×2; use-after-consuming-match; by-ref IO
  intent; bare-match exception; move-captured scrutinee payload.
  Batteries: stage2+borrow+guardrails 434/434; memcheck FULL 97+1skip.
  CORPUS: new phase reference `build/tmp/cleanup-efix` (manifest sha256
  bb5bd4bb406538344f32850782a63814c247c88e67d5671377138bd5f13ff434;
  partition identical 924/344/49; mismatch = the 8 rewritten sources only).
  Delta vs 4a-ref fully explained: c3_moveout_not_owned 7 → **1** (−6 =
  shapes 1-2 eliminated; residual = EXACTLY shape 3's
  catch_binder_visible_in_arm event, deferred per instruction);
  moveout_expansion/events −3,691 (= −3,685 owned + −6 not-owned: &IoError
  auto-borrows replaced predicate-arg moves corpus-wide); ALL other counters
  +0; hard gates zero.
  REMAINING to true-zero: shape 3's drop-paired MOVED_OUT reporter rule,
  then promote c3_moveout_not_owned to the hard-gate set. Follow-ups
  recorded: projected-place scrutinee audit; ConstShare synthesis
  visibility; spec wording for the match exception.
- 2026-07-13: **E-fix review round 1 (2 blocking, addressed).** (1) Stale
  interim ruling text in E-POPULATION-TRIAGE.md ("neither accepts implicit
  moves") rewritten as an explicit REVIEW CORRECTION superseded by the final
  ruling (the match-scrutinee exception); the pin-file docstring carried the
  same phrase and was fixed to match. (2) Version/history: 0.33.83 (ABI
  stays 21) with a doc/history.md entry covering the three fixes AND the
  SOURCE-COMPAT BREAK migration guidance (move for terminal transfer, &e for
  classification, &IoError predicate signatures + auto-borrow note,
  E_USE_AFTER_MOVE restructuring guidance, bare-match stays legal/consuming).
  Pins re-run 7/7.
- 2026-07-13: **Shape 3 CLOSED — C3 divergence at TRUE ZERO and promoted to
  HARD GATE.** Reporter-only ladder extension per instruction: raw MOVED_OUT
  classifies `c3_moveout_zero_safe` ONLY with the immediate-DropValue pairing
  (the snapshotted `moveout_feeds_drop` fact) — the compiler-authored dead
  drop of zero-backed storage (catch-binder cleanup after a user move).
  UNPAIRED MOVED_OUT re-moves (the shapes-1/2 store/call/scrutinee
  value-corruption class, source-fixed in 0.33.83) remain divergent — pinned
  both ways (paired → zero_safe; unpaired + zero-safe-predicate-true →
  DIVERGENT: the pairing is load-bearing and the predicate cannot substitute).
  Pins: ladder pin reworked + NEW end-to-end catch-binder pin
  (materialize → user move → authored MoveOut(e)+DropValue → reclassifies;
  fn-level c3_moveout_not_owned == 0). Reporter battery 21/21; stage2 FULL
  334/334; tool pins 7/7.
  GATE PROMOTION: `c3_moveout_not_owned` added to HARD_GATES
  (tools/drift_corpus_audit.py, tool v1.5.0 — gate-set change only;
  acquisition semantics unchanged).
  ACCEPTANCE (build/tmp/cleanup-shape3 vs cleanup-efix, exit 0, gate ACTIVE
  on the new side): universe identical 924/344/49; c3_moveout_not_owned
  1 → 0 (−1); c3_moveout_zero_safe +1 (the exact event); EVERY other counter
  +0. New phase reference: cleanup-shape3, manifest sha256
  3537978414a59214dc37de058fc8c8d7d9025ecb57c068d836f4e4b7346c5ae6.
  The E-population program is COMPLETE: 7 events → 2 LANGUAGE_BUG families
  source-fixed (+1 found en route), 1 dead-drop shape structurally
  recognized, 0 allowlists, divergence class now fail-closed corpus-wide.
  STOPPED for review before Array release-elision.
- 2026-07-14: **Array release-elision LANDED (emission slice) — acceptance
  EXACT; STOPPED for review before the next string_arc deletion step.**
  Implementation: one ledger fold in string_arc's Return-boundary section,
  exactly mirroring the String elision — Array locals whose boundary verdict
  is MUST_NOT_DROP join skip_cleanup_locals; PATH_DEPENDENT keeps the
  unconditional null-safe drop (first-slice discipline); unknown DropPolicy →
  conservative keep; `_ledger is None` → legacy. Strings untouched (separate
  fold). 0.27.145-hazard note recorded in-code: arrays have no late
  retain-wrap at return, so MOVED_OUT verdicts stay valid post-rewrite.
  PINS: note-site pin reworked into the elision pin (uninit/tombstoned
  elided; LIVE `sink` kept = the live-direction guard; OUTPUT-MIR ArrayDrop
  counts asserted per local — sink 2 [overwrite+sweep], a_live 1 [overwrite
  only; sweep gone], a_uninit 0, a_moved 1) + NEW PATH_DEPENDENT-kept pin
  (diamond → maybe_uninit → drop retained). NEW memcheck carrier
  test_array_release_elision.py — heap-backed Array<String> rows ×3
  (live-at-exit / moved-to-caller / conditionally-moved), over-elision reads
  as definitely-lost, kept-drop-after-move as Invalid free; valgrind clean.
  Batteries: reporter 22/22; memcheck FULL 98 passed + 1 skip;
  stage2+borrow+guardrails 436/436.
  ACCEPTANCE (build/tmp/cleanup-arrelide vs cleanup-shape3, tool v1.5.0,
  exit 0): universe identical 924/344/49 (manifest identical to shape3 —
  no source/env change); scope_exit_arraydrop 156,308 → 4,620 (−151,688 =
  EXACTLY the must_not_drop population; residual = EXACTLY the 4,620
  path-dependent drops, itemized by arraydrop_state:maybe_uninit);
  arraydrop uninit −141,391 → 0, moved_out −10,297 → 0; EVERY String
  counter byte-identical; hard gates zero incl. c3_moveout_not_owned.
  New phase reference: cleanup-arrelide.
  NOTES for review: (1) version bump not included — emission change likely
  rides the next release (0.33.84?) with a history entry at your call;
  (2) deletion-campaign consequence: the return-boundary array sweep is now
  PATH_DEPENDENT-only — a future slice can either flag-model those 4,620 or
  fold the sweep into cleanup_authoring, then delete `_drop_all_arrays`.
- 2026-07-14: **Deletion-campaign checkpoint (report-only) — SLICE4B-INVENTORY.md
  written; STOPPED for approval.** Inventory from the committed
  cleanup-arrelide reference: corpus-zero retain fallbacks remaining =
  call_arg_retain (3 sites), value_position_retain (9 sites: 2 explicit +
  7 default-class), return_retain_site3 (1 site, audit already loud), all
  funneling through _ensure_owned's SINGLE terminal StringRetain;
  destructor_self has NO emission site (enumeration residue). Live classes:
  temp_lastuse_release 618,744 / overwrite_release 233,519 /
  scope_exit_release 68,562 (String, post-elision) / scope_exit_arraydrop
  4,620 (all path-dependent) / site4 14; moveout_expansion 1,851,213
  structural (B-repr). PROPOSAL (smallest slice, "4b"): central retain-arm
  tripwire in _ensure_owned (converts all 3 corpus-zero classes at once,
  preserving move pre-checks, untyped pass-through, and last-use-release
  bookkeeping — the 4a two-arm lesson) + retire destructor_self from the
  closed enumeration (future use → UNTAGGED, already a hard gate). Expected
  acceptance: EVERY counter +0 vs cleanup-arrelide, gates zero; trigger
  pins per class + UNTAGGED pin; memcheck in gate; optional site-class gate
  promotion. NOT in 4b: 4a′ deletion of tripwired store_value branches
  (awaits cert cycle); all live-class migrations (sequenced in the report).
- 2026-07-14: **Slice 4b LANDED (per approved SLICE4B-INVENTORY.md + 3 review
  amendments) — every remaining late-retain class fail-closed; STOPPED for
  review.** Implementation: `_ensure_owned`'s terminal StringRetain replaced
  by the shared `_dead_stake_tripwire` — fail-closing call_arg_retain
  (3 sites), value_position_retain (9 sites), return_retain_site3 (1 site)
  at their single funnel; move/owned pre-checks, the untyped pass-through,
  and the LIVE last-use-release bookkeeping untouched (4a two-arm lesson).
  AMENDMENTS APPLIED: (1) hard-gate promotion MANDATORY — the four
  site_class:* retain counters joined HARD_GATES (tool v1.6.0; protects
  against tripwire bypasses and direct audit notes); (2) tripwire
  docstring/message + intake doc generalized to the shared 4a/4b wording;
  (3) destructor_self RETIRED from STRING_ARC_SITE_CLASSES with the
  "retained for completeness" comment rewritten (constant kept for
  historical parsing; any future use → UNTAGGED, already gated).
  PINS: +4 — synthetic trigger per family (call_arg via CallIndirect
  param_types; value_position via ArrayLit view element; return_site3 via a
  LoadRef view the alias walk cannot approve) + destructor_self-is-UNTAGGED.
  Batteries: reporter 26/26; stage2+guardrails+gate-pins 357/357; FULL
  memcheck 98 + 1 skip.
  ACCEPTANCE (build/tmp/cleanup-4b vs cleanup-arrelide, tool v1.6.0,
  exit 0): universe identical 924/344/49; EVERY counter +0; all NINE hard
  gates zero on the new side (four new site-class gates active). New phase
  reference: cleanup-4b.
  DELETION LADDER STATE: all five late-retain classes now
  tripwired-or-siteless; 4a′/4b′ branch deletion awaits one clean cert
  cycle with zero firings; next campaign steps per SLICE4B-INVENTORY §4
  (temp_lastuse migration measurement first).
- 2026-07-14: **temp_lastuse_release measurement checkpoint COMPLETE
  (report-only) — TLR-MEASUREMENT.md; STOPPED for TLR-1 approval.** Method:
  one scratch corpus run with temporary producer tagging; tree restored
  byte-identical (git diff empty; reporter 26/26); scratch run itself
  universe-identical 924/344/49, exit 0, all nine gates zero, buckets sum
  losslessly to 618,744. FINDINGS: (1) split settled BY CONSTRUCTION —
  100% _note_use, 0% _ensure_owned (post-4b any proven-String _ensure_owned
  call trips; green 4b acceptance = zero such releases; corollary:
  _ensure_owned's release arm is dead-in-effect → joins 4b′ deletion).
  (2) only 2 of 37 _note_use sites can release (consume=False: the generic
  instruction fallthrough + non-Return terminator operands) — the class is
  precisely "owned creator temps whose LAST use is non-consuming" (concat
  chains, comparisons). (3) producer histogram: ConstString 286,424 /
  StringConcat 192,523 / Call 114,780 / CopyValue 11,095 (string_stakes
  over-staking churn signal, sub-finding) / cross-block 'none' 7,398 /
  StringFrom* 6,479 / Exc* 45. (4) ledger does NOT model SSA temp
  lifetimes (named locals only) — migration = B-arch play: a pre-ledger
  release-materialization pass, then tripwire, then delete.
  PROPOSAL TLR-1 (smallest): materialize block-local ConstString last-use
  releases under a NEW site class; sum-conservation acceptance
  (temp_lastuse −N, materialized +N, all else +0), output-MIR identity pin,
  memcheck in gate; stop triggers incl. non-conservation and any
  cross-scope touch. 4a′/4b′ deletion still awaits a clean cert cycle.
- 2026-07-14: **TLR-1 design REVISED per review (stop/report — no
  implementation).** The review blocker acknowledged: "pre-ledger pass" and
  "byte-identical" were contradictory (a pre-ledger pass changes the ledger
  snapshot and downstream inputs). Revised design appended to
  TLR-MEASUREMENT.md: **option B chosen** — an in-string_arc classification
  SHIM at the single `_note_use` release point. Answers on record: (1) pass
  order/ledger UNCHANGED (no new pass, no rebuild — the snapshot is
  bit-for-bit today's); (2) double-release vacuous (one author; the
  question becomes real only in TLR-2's extraction and lives in ITS design
  gate); (3) output MIR BYTE-IDENTICAL by identity (only the env-gated
  audit tag differs) + output-MIR pin; (4) new
  SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE joins the closed enumeration +
  finalize's _counted_only (RELEASE-kind: C1/C2/C3 untouched; events +0);
  (5) smallest non-vacuous pin: one fn, both split directions (ConstString
  temps at a StringEq → materialized; a Concat-result temp and a
  cross-block ConstString → stay temp_lastuse) + release-position identity.
  Qualification predicate needs NO new analysis: the per-block producers
  map + the existing live_out guard already imply block-locality, so the
  expected transfer is EXACTLY the measured 286,424 (deviation = finding,
  not tolerance). TLR-2 (extraction, placement A: late pass immediately
  before string_arc, ledger rebuilt after, recognition handshake,
  behavior-equivalent not byte-identical) gets its own design gate.
  Awaiting TLR-1 approval.
- 2026-07-14: **TLR-1 LANDED (option-B shim, per approved revised design + 2
  review tightenings) — acceptance EXACT; STOPPED for review.**
  Implementation: classification split at `_note_use`'s single release point
  — `isinstance(producers.get(val), M.ConstString)` selects the NEW
  `materialized_lastuse_release` tag; the SAME StringRelease is emitted on
  the SAME path at the SAME position, unconditionally. No new pass, no
  ledger change, no pipeline-order change; output MIR byte-identical by
  identity (env-gated audit tag is the only difference).
  TIGHTENINGS APPLIED: (1) TLR-MEASUREMENT.md §5's pre-ledger-pass sketch
  struck through and marked SUPERSEDED in place (single authoritative plan);
  (2) NEW reporter pin `test_materialized_lastuse_is_closed_counted_only`
  (member of the closed enumeration, counted-only: not UNTAGGED, not
  UNCLASSIFIED, no C1/C2/C3 entry).
  Handshake pin `test_tlr1_shim_splits_and_emission_is_identical`: both
  split directions (block-local ConstStrings at a StringEq → materialized
  ×4; a Concat-result temp AND a cross-block ConstString → temp_lastuse ×2)
  + release-position identity in the OUTPUT MIR.
  Batteries: reporter 28/28; stage2+guardrails 352/352; FULL memcheck
  98 + 1 skip.
  ACCEPTANCE (build/tmp/cleanup-tlr1 vs cleanup-4b, tool v1.6.0, exit 0):
  temp_lastuse_release 618,744 → 332,320 (−286,424);
  materialized_lastuse_release +286,424 — EXACTLY the measured ConstString
  bucket, zero deviation; events +0; EVERY other counter +0; universe
  identical 924/344/49; all nine hard gates zero. New phase reference:
  cleanup-tlr1. The shim's tag boundary is now the corpus-proven ownership
  boundary for the TLR-2 extraction pass (separate design gate; NOT
  started, per instruction).
- 2026-07-14: **TLR-2 design checkpoint WRITTEN (TLR-2-DESIGN.md) — STOPPED
  before code.** Headline design decision: the honest hard part is that
  "consuming-ness" is DEFINED by string_arc's consumer arms, so the slice
  splits — TLR-2a extracts the classification into ONE shared predicate
  (`consumes_string_operand`), pure refactor with an all-+0 corpus
  signature BEFORE any new author exists; TLR-2b adds the pass
  (`string_releases.py::materialize_lastuse_releases`) using the SAME
  predicate + a conformance pin. Placement: late per-fn pass immediately
  before string_arc and BEFORE the existing per-fn ledger build (no extra
  rebuild; the one ledger is built on post-materialization MIR; index
  shifts safe because StringRelease has no transfer-function arm — states
  identical, keys shift consistently, counters position-independent).
  Handshake: pre-scan recognition of in-contract pre-existing
  StringRelease → temp excluded from owned_values (double-release
  impossible BY CONSTRUCTION — _note_use requires ownership); recognition
  arm notes the materialized_lastuse_release event (counter keeps its
  author-independent meaning; events constant); OUT-of-contract input
  release → dead-stake tripwire (verified contract, not trust).
  Expected delta both sub-slices: EVERYTHING +0 vs cleanup-tlr1
  (materialized stays 286,424; temp_lastuse stays 332,320). Output MIR:
  behavior-equivalent committed, byte-identical as a stretch A/B check
  (set-differences are stoppers, reorderings reportable). Regression plan
  covers both failure directions (double-release: construction + tripwire
  + memcheck + surplus gate; missing: conformance pin + memcheck + deficit
  gate). Stop triggers per sub-slice enumerated. Out of scope: family
  extensions (TLR-3+), _note_use release-arm tripwire, 4a′/4b′ deletion
  (parked until a clean cert cycle, reaffirmed).
- 2026-07-14: **TLR-2 design REVISION 2 (blocking review, 3 findings — all
  incorporated; still design-only).** (1) Recognition now specified to run
  BEFORE use counting: StringRelease IS a use per _iter_used_values, so an
  unrecognized materialized release would inflate the prescan count and
  MOVE the last-use point — in-contract releases are excluded from
  counting entirely, and the rewrite loop symmetrically skips _note_use for
  them (the original "harmless fallthrough decrement" claim retracted —
  the counts never included the occurrence, so no decrement may happen).
  NEW prescan-exclusion pin specified. (2) Explicit multiplicity rule §3a:
  repeated operands in one instruction (StringEq(%c,%c)) drain to ONE
  release AFTER the draining instruction — never per-occurrence, never
  before; repeated-operand case added to the conformance pin. (3) Second
  shared contract named: `compute_lastuse_release_points` (pure,
  occurrence-level release-point calculator built on
  `consumes_string_operand`) — TLR-2a extracts BOTH contracts + a
  calculator-vs-string_arc conformance pin; the pass consumes contract 2,
  never a reimplementation; the two-implementation window is explicit and
  closes when the last family migrates. Ledger-placement argument accepted
  as-is by review. Awaiting re-approval.
- 2026-07-14: **TLR-2a LANDED (pure refactor, corpus all-+0) — STOPPED for
  review before TLR-2b.** Extracted to string_arc module level:
  (1) `iter_used_values` — the occurrence iterator, moved verbatim from the
  closure (which now aliases it: single source for what counts as an
  occurrence); (2) contract 1 `consumes_string_operand`; (3) contract 2
  `compute_lastuse_release_points` (multiplicity rule §3a + the TLR-2b
  prescan-exclusion recognition rule implemented and pinned NOW).
  string_arc behavior UNTOUCHED (arms not rewritten — the closure→alias
  swap is the only pass-side change; conformance is empirical per design).
  IMPLEMENTATION FINDING (reported per the design's stop-trigger rule; no
  stop needed because the model subsumes both contracts): the two approved
  contracts under-modeled a THIRD operand axis — some handled arms neither
  consume nor note an operand (ref-position/non-String call params,
  info-less calls, ctor/Exc arms' non-selected operands, ErrorRaise).
  Implemented as a three-way DISPOSITION (CONSUME/USE/IGNORE) per String
  operand; IGNORE = counted-but-never-drained = never released — the
  calculator must reproduce it or it would invent releases string_arc
  never emits. `consumes_string_operand` is defined on top of dispositions,
  honoring contract 1's name.
  PINS: +2 — `test_tlr2a_calculator_conforms_to_string_arc` (calculator vs
  live pass over one block with ALL reviewed shapes: qualified temp;
  repeated-operand StringEq(%r,%r) → ONE point/release after the draining
  instruction; consumed single-use ConstString → none; Concat temp → not in
  the family, stays temp_lastuse; info-less-call IGNORE temp → none; plus
  emitted-release positions located by draining-instruction shape) and
  `test_tlr2a_prescan_exclusion_contract` (in-contract pre-materialized
  release contributes NO occurrence; other temps' points unchanged;
  released temp excluded).
  Batteries: reporter 30/30; stage2+guardrails 354/354; FULL memcheck
  98 + 1 skip. ACCEPTANCE (build/tmp/cleanup-tlr2a vs cleanup-tlr1,
  exit 0): EVERY counter +0 (materialized stays 286,424; temp_lastuse
  stays 332,320; events unchanged); universe identical; all nine hard
  gates zero. New phase reference: cleanup-tlr2a. TLR-2b (the extraction
  pass + string_arc recognition) awaits go-ahead.

- 2026-07-14 — TLR-2a REVIEW ROUND (4 findings: 3 from the first review
  + 1 blocking follow-up), all addressed. IN REVIEW.
  (1) SHARED SEEDING: `seed_string_dest_types(blocks_in_order,
  local_types, *, fn_infos, type_table)` extracted to module level
  (verbatim mirror of the private rules: ConstString/Concat/Retain/
  StringFrom* → String; AssignSSA/Phi propagation; Call via fn_infos or
  drift_string_* symbols; CallIndirect/CallIface `user_ret_type`);
  `_seed_dest_types` now delegates. PIN
  `test_tlr2a_seeder_closes_missing_metadata_gap`: production-like func
  with NO manual temp seeding — bare calculator sees nothing (the gap is
  real), seeder-on-a-copy + calculator computes the right points, live
  pass agrees (2 materialized releases).
  (2) IGNORE-AXIS PINS: `test_tlr2a_ignore_axis_conformance` — via
  CallIndirect instruction-carried param_types: ref-position (&String)
  arg → IGNORE; non-String by-value param arg → IGNORE; and the MIXED
  case (one IGNORE occurrence + a later USE occurrence) → NO release
  from calculator OR live pass (prescan counts 2, rewrite drains 1,
  count never reaches zero); pure-USE control temp still released
  (calculator point + live materialized release + output-MIR
  StringRelease list == ["%u"]). Docstring records that ctor/Exc
  non-selected-slot and ErrorRaise IGNOREs are unreachable for String
  operands in well-typed MIR (totality rows, not constructible).
  (3) DESIGN REV 3 (TLR-2-DESIGN.md): new §0a records the ACTUAL
  contract — three-way CONSUME/USE/IGNORE disposition
  (`string_operand_dispositions`), `consumes_string_operand` as its
  CONSUME projection, the calculator built on the full table (any
  non-USE occurrence disqualifies), the phantom-release failure mode,
  the shared seeder as the third contract piece 2b MUST call, and the
  reachability note. §0 contract-1 wording fixed: arms deliberately
  UNCHANGED in 2a (conformance by pins), not "consulted by the arms".
  (4) BLOCKING FOLLOW-UP — RECOGNITION MUST VALIDATE PLACEMENT: shape
  recognition alone (block-local ConstString producer) was too broad —
  a StringRelease placed BEFORE the temp's real draining instruction
  would still be recognized, excluded from counting, and suppress
  string_arc's own release: a TLR-2b emission bug silently becomes a
  use-after-release (or missing release). `compute_lastuse_release_points`
  now runs recognition in two phases: SHAPE (before occurrence counting,
  enabling the exclusion) then PLACEMENT validation — each recognized
  release must be the UNIQUE StringRelease of its temp, the temp's
  remaining occurrences all USE, temp not live-out/terminator-read, and
  the release index == draining-instruction index + 1; anything else
  raises the structured `unexpected input release` AssertionError (same
  fail-closed AssertionError → driver-boundary-diagnostic path as the
  dead-stake tripwires, live once 2b's prescan calls the calculator).
  PIN `test_tlr2a_misplaced_input_release_is_rejected`: release BEFORE
  a later StringEq(%t, …) → raises; duplicate releases (first one at
  the correct point) → raises. Design §3 recognition bullet updated to
  the two-half (shape AND placement) definition.
  Batteries: reporter 33/33; stage2 346/346; ledger-cache guardrails
  24/24; FULL memcheck 98 + 1 skip (run after the seeder delegation —
  the only production-path change; the calculator edits are dead code
  for the live pass until 2b). CORPUS: cleanup-tlr2a-r2 IN FLIGHT
  (first run killed + restarted: mid-run string_arc.py edit would have
  mixed tree states — tainted by the standing rule even though the
  edited function is not on the production path). Expected: every
  counter +0 vs cleanup-tlr2a, universe identical, all nine gates zero.

- 2026-07-14 — TLR-2a REVIEW ROUND, FINDING 5 (medium): call-param
  String classification in `string_operand_dispositions` used raw
  TypeId equality (`ty_id == string_ty`) where the live rewrite arms
  use the SEMANTIC `_param_is_string` (TypeKind.SCALAR && name ==
  "String") — and String param TypeIds are not canonical across the
  package/type-table boundary (the string_stakes lesson). A
  semantically-String by-value arg with a non-canonical TypeId would
  classify IGNORE in the table while the live arm CONSUMES it. (IGNORE
  still disqualifies the temp from release-point output, so no phantom
  release today — the real risk is CONTRACT DRIFT: the extracted
  `consumes_string_operand` would lie relative to the live arm, and
  future users of the predicate — the 2b pass, family migrations —
  would decide wrongly.) FIX: `_param_is_str_semantic` helper in the
  dispositions table; both call branches (Call via fn_infos signature;
  CallIndirect/CallIface via instruction-carried param_types) now use
  it; REF check order unchanged (mirrors the live arms). Non-call arms
  deliberately stay raw-equality — the live pass's own `_is_string_tid`
  is raw equality there, and the table mirrors the arms, not an ideal.
  PIN `test_tlr2a_semantic_string_param_conformance`: carrier
  `new_scalar("String")` (≠ ensure_string()); asserts (a) the
  disposition table classifies the arg CONSUME on all three call arms,
  (b) calculator points contain ONLY the control temp (no phantom
  releases after consumed args), (c) live-pass agreement (output MIR
  releases == control only; 1 materialized, 0 temp_lastuse). TEETH
  proven: temporary revert to raw equality → pin fails; restored exact
  (grep-verified). Design §0a updated with the semantic-predicate rule.
  Batteries: reporter 34/34; stage2 347/347; guardrails 24/24. CORPUS:
  restarted AGAIN as cleanup-tlr2a-r2 (second kill — this fix was
  another mid-run string_arc.py edit; table/calculator are still off
  the production path, but the no-mid-run-edit rule is unconditional).

- 2026-07-14 — TLR-2a REVIEW ROUND ACCEPTANCE: build/tmp/cleanup-tlr2a-r2
  (settled tree: all five review findings + wording fix), exit 0. EVERY
  counter +0 vs cleanup-tlr2a; universe identical 924/344/49; all nine
  hard gates zero; materialized_lastuse_release 286,424 /
  temp_lastuse_release 332,320 exact. cleanup-tlr2a-r2 is the phase
  reference for TLR-2b. Commit message delivered; TLR-2b (extraction
  pass + recognition handshake) starting per the user go-ahead.

- 2026-07-14 — TLR-2b IMPLEMENTED (extraction slice per TLR-2-DESIGN.md
  rev 3; go-ahead after cleanup-tlr2a-r2 accepted +0). VERIFICATION IN
  FLIGHT (stage2/guardrails/memcheck/corpus). Components:
  (1) NEW PASS `lang/driftc/stage2/string_releases.py` ::
  `materialize_lastuse_releases(func, *, type_table, fn_infos)` — emits
  StringRelease(%t) immediately after the draining instruction of every
  qualified block-local ConstString temp. Consumes ONLY the shared
  contracts (no re-implementation): `seed_string_dest_types` on a
  private local_types COPY; NEW shared `compute_string_temp_liveness`
  (the live-out fixpoint EXTRACTED from insert_string_arc — single
  liveness author; identical result pre/post-materialization since
  in-contract releases never reach block_use); contract-2
  `compute_lastuse_release_points` per block. Same-drain-group temps
  release consecutively in DRAIN order (last-occurrence position in the
  draining instruction's iter_used_values walk — mirrors _note_use's
  decrement sequence). Bottom-up insertion; terminator-drained group
  lands at end-of-instructions. mark_ledger_dirty on change; idempotent
  (second run recognizes its own output → no points → False).
  (2) DRIVER: wired in the cleanup_authoring per-fn loop AFTER
  materialize_call_arg_stakes, BEFORE _ol_build_and_attach — the one
  ledger string_arc consumes is built on post-materialization MIR
  (StringRelease has no ledger transfer-function arm; index shifts
  only). No extra rebuild.
  (3) STRING_ARC RECOGNITION: shared `recognize_materialized_releases`
  (new public projection; `_analyze_lastuse_block` is now the single
  core both contracts delegate to) called per block BEFORE use counting
  (fast-path: skipped when the block has no input StringRelease). Four
  suppression/recognition hooks: prescan-exclusion (recognized release
  contributes no occurrence); owned_values -= recognized after the
  per-block `set(owned_defs)` seed (the fn-wide prepass registers every
  ConstString dest — found via the A/B pin: without this, double
  release); ConstString rewrite-arm re-add skip (symmetric half);
  recognition arm in the rewrite loop (copy verbatim, NO _note_use — an
  uncounted decrement would skew _can_move_owned_once — and the audit
  note for materialized_lastuse_release moves here: same event, new
  author, `events` constant).
  (4) CONTRACT HARDENING FOUND BY THE A/B PIN: the placement rule
  "release == drain+1" was too strict — same-drain-group temps release
  CONSECUTIVELY, so the k-th release sits at drain+1+k. Refined: the
  gap between drain and release may contain ONLY in-contract releases
  (any non-release instruction in the gap — a later use, a later drain
  point — still rejects). Shape strictness added: ANY input
  StringRelease whose operand is not a block-local ConstString String
  temp raises `unexpected input release` (only the TLR-2b pass may
  author pre-string_arc releases; sole other producer grep-verified:
  none).
  PINS: +4 — `test_tlr2b_pass_plus_arc_equals_arc_only` (A/B
  byte-identity: pass+arc == arc-only instruction stream; audit counters
  equal per key; 5 materialized incl. repeated-operand + two same-group
  pairs, 1 temp_lastuse Concat control, consumed temp none);
  `test_tlr2b_pass_is_idempotent`;
  `test_tlr2b_out_of_contract_input_release_trips_string_arc`
  (shape-mismatch Concat release raises through insert_string_arc);
  `test_tlr2b_cross_block_temp_untouched` (pass no-op; configs equal;
  0 materialized). Reporter battery 38/38.
  EXPECTED ACCEPTANCE: every counter +0 vs cleanup-tlr2a-r2
  (materialized stays 286,424 — same events, new author;
  temp_lastuse stays 332,320; events unchanged); universe identical;
  all nine hard gates zero; memcheck 98+1skip.

- 2026-07-15 — TLR-2b ACCEPTED: build/tmp/cleanup-tlr2b, exit 0. EVERY
  counter +0 vs cleanup-tlr2a-r2 (19 aggregate keys identical);
  universe identical 924/344/49; all nine hard gates zero;
  materialized_lastuse_release 286,424 EXACT (same events, new author —
  the pass + string_arc recognition arm); temp_lastuse_release 332,320;
  events 2,772,052 unchanged. Batteries: reporter 38/38; stage2
  351/351; ledger-cache guardrails 24/24; FULL memcheck 98 + 1 skip
  (with the pass live in the pipeline — heap-string rows are the
  double-release/missing-release detector). cleanup-tlr2b is the new
  phase reference. STOPPED for review. Next per the measured ladder:
  TLR-3 family extensions (StringConcat → Call results →
  StringFrom*/Exc* → cross-block tail → tripwire _note_use's release
  arm); 4a'/4b' deletion still parked on a clean cert cycle.

- 2026-07-15 — **B5/String representation + C interop decisions PINNED** while
  TLR work continued. `SCOPE-B-PLAN.md` §10.2.1 is now the authoritative
  follow-up record: Drift-native model is immutable UTF-8 `String` as
  specialized `{len, RcBytes}` over exact storage with a hidden trailing
  NUL; no SSO; `RcBytesFlags` limited to static/immortal plus
  interior-NUL cache state; C never observes the native layout. Checked
  C-string helpers are Rust-like and fallible (`with_cstr` through
  `with_cstr4`, returning `Result<T, CStringError>` with
  `InteriorNul(index)`); unsafe no-scan variants are explicit
  (`with_cstr_unsafe` through `with_cstr4_unsafe`) and document prefix
  semantics if an interior NUL exists; `with_bytes` handles
  length-aware APIs; `CStringScope` is opaque internal pins/temps, not
  user-visible indexing; owned C handoff remains a separate type.
  `CLEANUP-EXECUTION-PLAN.md` now cross-references the pinned B5
  decisions from the B-repr handoff section.
