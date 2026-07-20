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

- 2026-07-15 — TLR-3 DESIGN CHECKPOINT (report-only, per instruction;
  work/string-ownership-refactor/TLR-3-DESIGN.md). STOPPED FOR REVIEW.
  MEASUREMENT: scratch TEMP-MEASURE run on the committed TLR-2b tree
  (build/tmp/tlr3-measure, exit 0, universe identical, events +0):
  temp_lastuse 332,320 splits LOSSLESSLY as concat 192,523 / call
  114,780 / copyvalue 11,095 / none 7,398 / from 6,479 / exc 45 /
  other+plain 0 — bucket-identical to the original measurement, and the
  shim's ConstString branch confirmed dead in production (0 notes).
  Instrumentation reverted; NOTE: restoration used `git checkout --` on
  the two scratch-instrumented files (working-tree git op — flagged;
  reverse-edits next time per the no-git-writes rule); tree verified at
  15a5122d with zero TM_ refs, battery 38/38.
  DESIGN: StringConcat extension is MECHANICALLY SAFE via ONE new
  module constant MATERIALIZED_RELEASE_FAMILY consumed by the analysis
  shape predicate AND the TLR-1 shim split (single source, no drift);
  only other code change: recognized-guard on the StringFrom*/Concat
  owned-registration arm (seed-half subtraction already set-driven).
  Qualified population = exactly the 192,523 measured (release-arm ⇔
  qualification equivalence, pinned in 2a). New pinned obligations:
  cross-FAMILY same-drain-group chains (Concat+ConstString releases
  interleave; gap validation is set-driven, drain-order rule
  family-agnostic). Expected delta: temp_lastuse −192,523 → 139,797;
  materialized +192,523 → 478,947; sum conserved; all else +0; gates
  zero; full memcheck. Recommends SINGLE implementation slice (no
  TLR-1-style shim step — decoupling purpose spent; deviation recorded).
  Flagged ahead: TLR-4 Call family needs its own gate (can_throw
  topology); CopyValue rides the stake-precision sub-investigation;
  cross-block tail needs lifetime analysis.

- 2026-07-15 — TLR-3 IMPLEMENTED (single slice per approved design;
  required shape followed exactly). VERIFICATION IN FLIGHT
  (stage2/guardrails/memcheck/corpus). Code:
  (1) NEW module constant `MATERIALIZED_RELEASE_FAMILY = (M.ConstString,
  M.StringConcat)` in string_arc.py — single source consumed by the
  analysis shape predicate (`_is_conststring_temp` renamed
  `_is_family_temp`), the recognition/rejection contract (shape half of
  `unexpected input release`), and the TLR-1 shim classification in
  `_note_use` (isinstance against the constant). Call / CopyValue /
  StringFrom* / cross-block tails remain OUT (constant comment records
  the scope).
  (2) Recognized-guard added to the StringFrom*/StringConcat
  owned-registration arm (Concat is now suppressible; StringFrom*
  members can't be in the recognized set yet — guard is a no-op there).
  Seed-half (`owned_values -= recognized_released`) already set-driven.
  (3) string_releases.py: no logic change (consumes the calculator);
  docstrings updated to family wording. Family docstrings in string_arc
  contracts updated.
  PINS (battery 41/41): four flipped pins updated —
  tlr1_shim (5 materialized / 1 temp_lastuse: %cc joins, %x1 cross-block
  stays), tlr2a conformance (points now include %cc at the shared drain
  idx 7 — cross-family same-drain group asserted in output; 5/0),
  tlr2b A/B (6/0), out-of-contract pin rewritten: SHAPE carrier moved to
  StringFromInt (Concat became in-contract), plus misplaced-Concat and
  duplicated-Concat placement carriers (all three still trip).
  NEW pins: `test_tlr3_concat_chain_ab_byte_identity` (a+b+c chain; pass
  output layout asserted instruction-by-instruction incl. cross-family
  drain order %c1 then %d; A/B byte-identity; 5 materialized/0),
  `test_tlr3_multiuse_and_consumed_concat` (multi-use releases EXACTLY
  once after the LAST use — position asserted; consumed concat emits
  nothing from either author; A/B equal; 5/0),
  `test_tlr3_cross_block_concat_untouched` (pass materializes the
  in-block ConstString operands but NOT the cross-block %cc; string_arc
  still releases it as temp_lastuse; configs byte-identical; 2/1).
  Idempotence pin extended with a Concat temp (release layout asserted).
  EXPECTED ACCEPTANCE (vs cleanup-tlr2b): temp_lastuse 332,320 →
  139,797 (−192,523); materialized 286,424 → 478,947 (+192,523); sum
  conserved; events 2,772,052 unchanged; all other counters +0;
  universe identical; nine gates zero; full memcheck 98+1.

- 2026-07-15 — TLR-3 ACCEPTED: build/tmp/cleanup-tlr3, exit 0.
  EXACTLY two counters moved vs cleanup-tlr2b: temp_lastuse_release
  332,320 → 139,797 (−192,523); materialized_lastuse_release 286,424 →
  478,947 (+192,523) — sum conserved; every other counter +0; events
  2,772,052 unchanged; universe identical 924/344/49; all nine hard
  gates zero. Batteries: reporter 41/41; stage2 354/354; guardrails
  24/24; FULL memcheck 98+1 (an initial 19-failure run was load-induced
  flake — raced the corpus audit's 16 compile jobs; standalone rerun on
  the identical tree clean). cleanup-tlr3 is the new phase reference.
  Commit msg delivered. Next: TLR-4 checkpoint (in progress, report
  only).

- 2026-07-15 — TLR-4 DESIGN CHECKPOINT (report-only;
  work/string-ownership-refactor/TLR-4-DESIGN.md). STOPPED FOR REVIEW.
  MEASUREMENT (scratch TM run on the TLR-3 tree, build/tmp/tlr4-measure,
  exit 0, universe identical, events +0; restoration via STORED REVERSE
  EDITS, zero TM_ refs, battery 41/41): the 139,797 residual splits
  LOSSLESSLY — the ENTIRE call bucket is Call·nothrow·infosem 114,780
  (direct Call, non-throw, signature-proven semantic-String return);
  CallIndirect/CallIface/can-throw/helper/info-less ALL ZERO; remaining
  copyvalue 11,095 / none 7,398 / from 6,479 / exc 45.
  STRUCTURAL: can-throw String results are IMPOSSIBLE as family temps —
  _lower_can_throw_call_value gives the call an ENVELOPE dest, ends the
  block at ResultIsErr/IfTerminator, and the payload reaches later code
  as a MoveOut dest from the hidden ok-local; error edges cannot skip
  block-local releases (throw topology IS block topology); can-throw
  args are CONSUME or IGNORE → out of family either way.
  DESIGN (per review direction): replace the MATERIALIZED_RELEASE_FAMILY
  tuple with ONE shared is_materialized_release_family_producer(prod, *,
  local_types, fn_infos, type_table) predicate — ConstString/Concat
  unconditional; calls admitted only nothrow + semantically-String-
  proven (fn_infos signature semantic test or drift_string_* symbols);
  info-less conservatively OUT (population 0 today, pinned so metadata
  regressions can't widen the family); consumed by analysis/recognition
  AND shim (one source); owned Call-arm gains the recognized guard.
  Open sub-decision flagged: admit CallIndirect/CallIface via
  instruction-carried user_ret_type (recommended, population 0) or gate
  to M.Call only. ONE family, one slice (no throw split — dissolved
  structurally). Expected delta: temp_lastuse 139,797 → 25,017;
  materialized 478,947 → 593,727; sum conserved; all else +0; gates
  zero; memcheck STANDALONE (TLR-3 flake lesson). Regression plan incl.
  REQUIRED throwing-call topology pin (driver-level, both edges
  exercised, memcheck row) + envelope-dest-never-family unit pin +
  info-less-stays-out pin + semantic-String carrier.

- 2026-07-15 — TLR-4 IMPLEMENTED (single slice per approved design +
  predicate directive). VERIFICATION IN FLIGHT (corpus then memcheck
  STRICTLY SEQUENTIAL — the TLR-3 flake lesson; stage2 358/358 and
  guardrails 24/24 already green). Code:
  (1) `MATERIALIZED_RELEASE_FAMILY` tuple REPLACED by the shared
  predicate `is_materialized_release_family_producer(prod, *, fn_infos,
  type_table)` (dest String-typed-ness stays the caller's condition in
  `_is_family_temp`): ConstString/StringConcat unconditional; direct
  Call — NOT can_throw AND (fn_infos signature return semantically
  String [finding-5 test] OR drift_string_* helper symbol);
  CallIndirect/CallIface — NOT can_throw AND semantic-String
  instruction-carried user_ret_type; info-less/unproven conservatively
  OUT. Consumed by `_is_family_temp` (analysis+recognition) AND the
  TLR shim — one source preserved.
  (2) `DRIFT_STRING_HELPER_SYMBOLS` extracted to module level;
  `_is_string_creator` now consumes it (same proof list, one source).
  `_is_semantic_string_tid` module helper; the dispositions table's
  `_param_is_str_semantic` delegates to it.
  (3) Owned-registration: call dests are registered ONLY in the fn-wide
  `owned_defs` prepass (no rewrite-loop re-add arm — verified) — the
  per-block `owned_values -= recognized_released` subtraction fully
  covers family suppression; documented at the prepass branch and
  proven empirically by the A/B pins.
  PINS (battery 45/45): conformance pin gains the qualified-Call column
  (%qc via fn_infos, points + release-position assertions updated);
  NEW `test_tlr4_call_family_ab_semantic_and_idempotence` (semantic
  non-canonical return TypeId carrier + helper-symbol carrier +
  multi-use ONE-release + consumed none; A/B byte-identity; pass
  idempotent with Call temps); `test_tlr4_nonfamily_calls_stay_out`
  (can-throw String-dest fail-closed guard, info-less stays out,
  cross-block, CallIndirect throw-guard; pass no-op; A/B identical;
  4 temp_lastuse / 0 materialized);
  `test_tlr4_indirect_iface_user_ret_type_family` (CallIndirect via
  non-canonical user_ret_type + CallIface via canonical; both
  materialized); `test_tlr4_out_of_contract_call_release_trips`
  (misplaced + duplicated family-Call releases trip).
  NEW MEMCHECK ROW `lang/tests/memcheck/test_call_result_lastuse_release.py`
  (heap-string carriers; row 1 = family comparison operands; row 2 =
  REQUIRED throwing-call topology: throws callee, error edge exercised
  every third i through the try/catch-expression fallback, family
  release on the join; missing-release reads definitely-lost,
  double-release reads Invalid free) — passed standalone.
  EXPECTED ACCEPTANCE (vs cleanup-tlr3): temp_lastuse 139,797 → 25,017
  (−114,780); materialized 478,947 → 593,727 (+114,780); sum conserved;
  events 2,772,052 unchanged; all other counters +0; universe
  identical; nine gates zero; memcheck 99+1 (new row included).

- 2026-07-15 — TLR-4 ACCEPTED (final, supersedes the IN-FLIGHT status
  above): build/tmp/cleanup-tlr4, exit 0. EXACTLY two counters moved vs
  cleanup-tlr3: temp_lastuse_release 139,797 → 25,017 (−114,780);
  materialized_lastuse_release 478,947 → 593,727 (+114,780) — sum
  conserved (618,744 lifetime total); every other counter +0; events
  2,772,052 unchanged; universe identical 924/344/49; all nine hard
  gates zero. STANDALONE memcheck (run sequentially AFTER the corpus
  job exited, per the TLR-3 flake lesson): 99 passed + 1 skipped —
  includes the new throwing-topology row
  (test_call_result_lastuse_release.py). Batteries: reporter 45/45;
  stage2 358/358; guardrails 24/24. cleanup-tlr4 is the new phase
  reference. Remaining temp_lastuse population 25,017 = copyvalue
  11,095 (stake-precision investigation) + cross-block none 7,398
  (lifetime analysis) + StringFrom* 6,479 + Exc* 45 (mechanical later
  family). Commit msg delivered.

- 2026-07-15 — TLR-5 DESIGN CHECKPOINT (report-only;
  work/string-ownership-refactor/TLR-5-DESIGN.md). STOPPED FOR REVIEW.
  MEASUREMENT (fine-grained scratch TM run on the TLR-4 tree,
  build/tmp/tlr5-measure, exit 0, universe identical, events +0;
  reverse-edit restoration, zero TM_ refs, battery 45/45): residual
  25,017 splits LOSSLESSLY — copyvalue 11,095 / none 7,398 /
  StringFromUint 1,853 / StringFromFloat 1,850 / StringFromInt 1,850 /
  StringFromBool 926 (from-total 6,479 exact) / ExcGetParamsJson 40 /
  ExcGetContextJson 5 (exc-total 45 exact).
  STRUCTURAL: both families confirmed UNCONDITIONAL owned producers —
  plain single-dest instructions, scalar/error operands (never String),
  no can_throw, no control flow; StringFrom* = f-string hole lowering +
  throw-envelope event-code formatting, codegen drift_string_from_* +1;
  Exc* = error .params/.context field access, ABI §2.3 retained
  returns. Suppression already covered: StringFrom* owned arm carries
  the TLR-3 recognized guard (goes live, comment update only); Exc* is
  prepass-only (verified) → subtraction covers.
  DESIGN: ONE slice, no 5a/5b split (identical mechanics, exc
  population 45, split = ceremony); both families join the
  UNCONDITIONAL isinstance tuple in the shared predicate. Expected
  delta: temp_lastuse 25,017 → 18,493 (−6,524); materialized 593,727 →
  600,251 (+6,524); per-family sub-checks 6,479 + 45; all else +0;
  gates zero; memcheck STANDALONE. Pin note: the standing
  shape-rejection carrier (StringFromInt) joins the family — moves to
  a CopyValue-produced carrier, StringFromInt converts to placement
  cases (the TLR-3 Concat-carrier flip repeating). Two new memcheck
  rows planned: f-string interpolation (StringFrom* live) and
  error-inspection catch arm (.params/.context, Exc* live, error edge
  every iteration). After TLR-5 the ONLY residuals are copyvalue +
  cross-block (18,493) — the _note_use release-arm tripwire stays
  parked until those resolve.

- 2026-07-15 — TLR-5 IMPLEMENTED (single slice per approved design +
  required shape). VERIFICATION IN FLIGHT (corpus → memcheck strictly
  sequential; fast batteries green: reporter 47/47, stage2 360/360,
  guardrails 24/24, new memcheck rows passed standalone). Code:
  (1) `is_materialized_release_family_producer`: unconditional tuple
  extended with StringFromInt/Bool/Uint/Float + ExcGetParamsJson/
  ExcGetContextJson; docstring records the TLR-5 members and narrows
  the out-of-scope note to CopyValue + cross-block.
  (2) StringFrom*/Concat owned-arm comment updated — the recognized
  guard is LIVE for every member of the arm now (no logic change).
  (3) Exc* suppression documented at both prepass branches:
  prepass-only registration (no rewrite-loop re-add arm), covered by
  `owned_values -= recognized_released`.
  (4) Out-of-contract SHAPE carrier moved StringFromInt → CopyValue
  (second carrier migration — Concat in TLR-3, StringFromInt now);
  StringFromInt converts to NEW misplaced + duplicated placement cases.
  PINS (battery 47/47): NEW `test_tlr5_stringfrom_and_exc_family`
  (all four StringFrom* kinds + both Exc* kinds qualified; multi-use
  releases ONCE after LAST use; consumed emits none; A/B byte-identity;
  pass idempotent; 7 materialized / 0 temp_lastuse);
  `test_tlr5_cross_block_stringfrom_untouched` (pass no-op; stays
  temp_lastuse; configs identical). NEW MEMCHECK ROWS
  `test_stringfrom_exc_lastuse_release.py` (f-string row: all four
  hole kinds drain into interpolation chains + compared results;
  error-inspection row: catch arm reads e.params/e.context — Exc* on
  the LIVE error path every other iteration) — passed standalone.
  EXPECTED ACCEPTANCE (vs cleanup-tlr4): temp_lastuse 25,017 → 18,493
  (−6,524 = 6,479 StringFrom* + 45 Exc*); materialized 593,727 →
  600,251; sum conserved; events/all else +0; universe identical;
  gates zero; memcheck standalone.

- 2026-07-15 — TLR-5 ACCEPTED (final): build/tmp/cleanup-tlr5, exit 0.
  EXACTLY two counters moved vs cleanup-tlr4: temp_lastuse_release
  25,017 → 18,493 (−6,524); materialized_lastuse_release 593,727 →
  600,251 (+6,524) — per-family transfer matches the measurement
  (6,479 StringFrom* + 45 Exc*); sum conserved; every other counter
  +0; events 2,772,052 unchanged; universe identical 924/344/49; all
  nine hard gates zero. STANDALONE memcheck 100 passed + 1 skipped
  (incl. both new TLR-5 rows). Batteries: reporter 47/47; stage2
  360/360; guardrails 24/24. cleanup-tlr5 is the new phase reference.
  Remaining temp_lastuse 18,493 = CopyValue 11,095 + cross-block none
  7,398 — the LAST two populations before the _note_use release-arm
  tripwire. Commit msg delivered. Next: CopyValue residual checkpoint
  (report-only, in progress).

- 2026-07-16 — COPYVALUE RESIDUAL CHECKPOINT (report-only;
  work/string-ownership-refactor/COPYVALUE-RESIDUAL-REPORT.md).
  STOPPED FOR REVIEW.
  MEASUREMENT (origin-tagged scratch run, build/tmp/cv-measure, exit 0,
  universe identical, events +0, no untagged bucket; 14 reverse edits
  restored, battery 47/47): the 11,095 CopyValue residual is EXACTLY
  TWO hir_to_mir sites — array_elem_copy (bounds-checked arr[i] value
  read) 9,246 + array_elem_field_copy (arr[i].field read) 1,849;
  string_stakes and ALL other emit sites ZERO.
  KEY FINDING: the original "CopyValue (string_stakes stakes!)"
  annotation is DISPROVEN — every .stake is consumed at its anchor;
  the B-arch stake passes produce zero released-unused stakes.
  CLASSIFICATION: field-copy 1,849 = real ownership (borrowed
  view → owned copy, semantically necessary) → migrate; elem-copy
  9,246 = release is real ownership NOW (migrate) + the copy itself is
  ELIMINABLE CHURN later (hidden __tmp local could MoveOut instead of
  LoadLocal+CopyValue — deletes a retain/release pair per element read;
  separate lowering slice, different corpus signature, events DOWN).
  No boundary-forced-churn bucket.
  PROPOSED: Path A = TLR-6 (M.CopyValue joins the unconditional family;
  temp_lastuse 18,493 → 7,398 exact −11,095 with 9,246+1,849
  sub-checks; materialized → 611,346; shape carrier migrates a third
  time → StringRetain becomes the non-member carrier); Path B =
  element-read churn elimination AFTER Path A (own design gate);
  cross-block 7,398 stays out (lifetime analysis) — after Path A it is
  the ONLY residual before the _note_use release-arm tripwire.

- 2026-07-16 — COPYVALUE CHECKPOINT REVIEW AMENDMENT (blocking gap in
  Path A as written, caught in review): CopyValue — unlike Call/Exc*
  (prepass-only) — has a LIVE rewrite-loop owned re-add arm
  (string_arc.py ~1898), and the per-block subtraction runs BEFORE the
  rewrite loop; family membership without the arm guard → recognized
  release copied through AND _note_use second release at the drain.
  Report Path A amended (COPYVALUE-RESIDUAL-REPORT.md §4): TLR-6 MUST
  (a) add the same `if instr.dest not in recognized_released:` guard
  the ConstString and StringFrom*/Concat arms carry, and (b) pin the
  teeth: CopyValue temp + pre-materialized release after last
  non-consuming use → EXACTLY ONE release in output MIR, zero
  temp_lastuse — fails if the predicate extends without the guard.
  Checkpoint otherwise accepted (report-only state verified clean).
  TLR-6 is the next implementation slice, pending go-ahead.

- 2026-07-16 — TLR-6 IMPLEMENTED (as amended). VERIFICATION IN FLIGHT
  (corpus → memcheck strictly sequential; fast batteries green:
  reporter 50/50, stage2 363/363, guardrails 24/24, new memcheck row
  standalone-passed). Code:
  (1) `M.CopyValue` joins the UNCONDITIONAL family in
  `is_materialized_release_family_producer`; docstring records TLR-6
  membership (+ .stake-never-qualifies rationale) and narrows the
  out-of-scope note to the cross-block tail — the LAST population
  before the _note_use release-arm tripwire.
  (2) REVIEW-AMENDMENT GUARD: the CopyValue rewrite-loop owned re-add
  arm (string_arc.py ~1898) now carries
  `instr.dest not in recognized_released` — the live-arm suppression
  the amendment required (prepass subtraction alone is insufficient:
  the arm runs AFTER it).
  (3) Shape carrier migrated a THIRD time: CopyValue → StringRetain
  (owned but not a materialization boundary); CopyValue converts to
  new misplaced + duplicated placement cases.
  PINS (battery 50/50): `test_tlr6_copyvalue_guard_teeth` — the
  dedicated amendment pin (CopyValue temp + pre-materialized release
  after last non-consuming use → EXACTLY ONE release, zero
  temp_lastuse); TEETH PROVEN by temporary guard removal (pin FAILED on
  the double release, guard restored, pin passes; tree grep-verified).
  `test_tlr6_copyvalue_family` (qualified/multi-use-once/consumed-none
  view-source copies; A/B byte-identity; idempotence);
  `test_tlr6_cross_block_copyvalue_untouched`. NEW MEMCHECK ROW
  `test_copyvalue_lastuse_release.py` (heap Array<String>: arr[i]
  value-read comparisons + arr[i].name field-read comparisons — BOTH
  measured sites live) — passed standalone.
  EXPECTED ACCEPTANCE (vs cleanup-tlr5): temp_lastuse 18,493 → 7,398
  (−11,095 = 9,246 elem + 1,849 field); materialized 600,251 →
  611,346; sum conserved; events/all else +0; universe identical;
  gates zero; memcheck standalone.

- 2026-07-16 — TLR-6 ACCEPTED (final): build/tmp/cleanup-tlr6, exit 0.
  EXACTLY two counters moved vs cleanup-tlr5: temp_lastuse_release
  18,493 → 7,398 (−11,095, matching the measured 9,246 elem + 1,849
  field sites); materialized_lastuse_release 600,251 → 611,346
  (+11,095) — sum conserved (618,744 lifetime total, of which 611,346
  now under the dedicated materialization authority); every other
  counter +0; events 2,772,052 unchanged; universe identical
  924/344/49; all nine hard gates zero. STANDALONE memcheck 101 passed
  + 1 skipped (incl. the new CopyValue row). Batteries: reporter 50/50;
  stage2 363/363; guardrails 24/24. cleanup-tlr6 is the new phase
  reference. The ONLY remaining temp_lastuse population is the
  cross-block tail (7,398) — its design gate is the last step before
  the _note_use release-arm tripwire. Commit msg delivered.

- 2026-07-16 — TLR-7 DESIGN CHECKPOINT (report-only;
  work/string-ownership-refactor/TLR-7-DESIGN.md). STOPPED FOR REVIEW.
  The LAST population before the _note_use release-arm tripwire.
  MEASUREMENT (fn-wide producer + CFG-shape scratch run,
  build/tmp/xb-measure, exit 0, universe identical, events +0; 3
  reverse edits restored, zero TM_/_tm_ refs, battery 50/50): ALL 7,398
  resolve to FAMILY producers — StringConcat×loop 7,392 (per-iteration
  intra-loop block crossings in multi-block string-building loops) +
  StringConcat×linear 6; ZERO joins, ZERO non-family/no-producer.
  → acceptance target IS temp_lastuse → 0 exactly (no residuals; the
  review's itemization branch is moot).
  DESIGN (five review requirements folded in): §0 precise framing —
  reuses the EXISTING fn-wide liveness authority, adds fn-wide producer
  resolution (no new lifetime analysis BEYOND it); §3c bypass-path
  caveat — behavior EQUIVALENCE claim, not a leak proof (TLR-7 mirrors
  today's drain points; zero join-shaped drains measured, but the
  record claim is equivalence); §4 fn-wide producer contract
  ("block-local family temp" → "fn-wide unique producer, release in
  the drain block"; duplicate-dest fails closed; shared lookup
  authority pass+recognition; recognition message/tests drop
  "block-local"; four legacy cross-block-untouched pins FLIP to
  materialized expectations); §5 pin ladder incl. the REQUIRED
  loop/backedge pins (per-iteration positive + loop-carried NEGATIVE
  control), path-exclusive dual drains, branch join, straight-line,
  consumed-before-exit, live-out-to-terminator, cross-block
  misplaced/duplicated trips, multi-block loop memcheck row.
  Proofs: §3a path-exclusivity via the liveness fixpoint (backedges
  covered); §3b cross-block suppression already structural (per-block
  owned re-seed + drain-block subtraction; arm guards unchanged).
  Expected: temp_lastuse 7,398 → 0; materialized 611,346 → 618,744 —
  the ENTIRE lifetime population under the dedicated authority; all
  else +0. After TLR-7: release-arm tripwire → cert cycle → delete
  with 4a'/4b'.

- 2026-07-16 — TLR-7 DESIGN REVIEW AMENDMENTS (2 items): (1) BLOCKING —
  §5 gains the BYPASS-PATH A/B pin (producer before a diamond, one arm
  drains, other arm bypasses, dead at join → release ONLY in the use
  arm, none in bypass/join, byte-identical to arc-only) — the §3c
  behavior-equivalence contract now has teeth; (2) MEDIUM — §4
  contract-update list expanded with three more stale block-local
  surfaces: compute_string_temp_liveness docstring (~847; the
  liveness-invariance ARGUMENT must be rewritten — refined form:
  every in-contract release site is dominated by a use of the same
  temp within the drain block, incl. the terminator-drained case, so
  block_use already contains the temp; the invariance CONCLUSION
  survives), is_materialized_release_family_producer docstring, and
  materialize_lastuse_releases FUNCTION doc. Awaiting implementation
  go-ahead.

- 2026-07-16 — TLR-7 IMPLEMENTED (as amended). VERIFICATION IN FLIGHT
  (corpus → memcheck strictly sequential; fast batteries green:
  reporter 54/54, stage2 367/367, guardrails 24/24, new memcheck row
  standalone-passed). Code:
  (1) NEW shared `build_fnwide_producers(blocks_in_order)` — the ONE
  producer-lookup authority (duplicate SSA dest fails closed);
  `_analyze_lastuse_block` + both public contracts gain
  `producers_fnwide=` (None → single-block fallback for unit callers,
  documented); insert_string_arc builds the map once post-seeding and
  threads it to per-block recognition AND the shim classification
  (fn-wide — A/B counter equality depends on it; the per-block
  `producers` map keeps serving the move-approval helpers unchanged);
  the pass builds + threads the same map.
  (2) FULL WORDING SWEEP (six surfaces per the amended design):
  predicate docstring, analysis qualification/shape docstrings,
  phase-1 comment, fail-closed message ("fn-wide producer resolution"),
  compute_string_temp_liveness invariance argument REWRITTEN to the
  dominated-by-use form, string_releases module + function docs
  (TLR-2b..7; cross-block-excluded sentences retired; coverage note →
  ALL 618,744), predicate out-of-scope note → ladder closed.
  (3) CONTRACT GAP CAUGHT BY THE TERMINATOR PIN: terminator-drained
  temps (point = len(instructions)) were rejected by the placement
  validation (`temp not in term_used`). Arc-only probe CONFIRMED the
  in-pass emission releases at end-of-instructions before the
  terminator read — the pass placement was faithful; validation gained
  a terminator-drained arm (release sits IN the trailing release run,
  after every instruction occurrence; Return-consumed still rejects;
  first attempt anchored the run at the last occurrence — wrong for
  terminator-ONLY-used temps — corrected to anchor at the release).
  PINS (battery 54/54): SIX legacy pins FLIPPED to materialized
  expectations, carriers preserved (tlr1 shim %x1 → 6/0; tlr2b/3/5/6
  cross-block pins → drain-block release asserted + A/B counter
  equality; tlr4 %xb → family, stay-out set now can-throw/info-less/
  throw-indirect only, 1/3). NEW: `test_tlr7_cfg_shapes_ab` (branch
  join → single release at join; path-exclusive dual drains → one per
  arm; BYPASS PATH → release ONLY in the use arm, none in bypass/join
  — the §3c contract's teeth); `test_tlr7_loop_backedge_ab` (positive:
  per-iteration Concat produced in head, drained in body2 across the
  intra-loop boundary; NEGATIVE control: %seed live through the
  backedge → no release inside the loop, drains after exit);
  `test_tlr7_consumed_and_terminator_cases` (cross-block consumed →
  none; non-Return terminator-drained → trailing release, position
  asserted); `test_tlr7_cross_block_out_of_contract_and_dup_producer`
  (cross-block misplaced/duplicated trip; duplicate-SSA-dest builder
  tripwire). NEW MEMCHECK ROW `test_crossblock_lastuse_release.py`
  (the measured 7,392 shape: concat chains interleaving bounds-checked
  array reads inside multi-block loop bodies + a conditional variant)
  — passed standalone.
  EXPECTED ACCEPTANCE (vs cleanup-tlr6): temp_lastuse 7,398 → 0
  (sub-check 7,392 loop + 6 linear); materialized 611,346 → 618,744 —
  the ENTIRE lifetime population; all else +0; universe identical;
  gates zero; memcheck standalone.

- 2026-07-16 — TLR-7 ACCEPTED (final): build/tmp/cleanup-tlr7, exit 0.
  EXACTLY two counters moved vs cleanup-tlr6: temp_lastuse_release
  7,398 → 0; materialized_lastuse_release 611,346 → 618,744 — THE
  ENTIRE LIFETIME POPULATION under the dedicated materialization
  authority (the aggregate no longer contains a temp_lastuse_release
  key: counters 18 → 17); every other counter +0; events 2,772,052
  unchanged; universe identical 924/344/49; all nine hard gates zero.
  STANDALONE memcheck 102 passed + 1 skipped (incl. the new cross-block
  loop row). Batteries: reporter 54/54; stage2 367/367; guardrails
  24/24. cleanup-tlr7 is the new phase reference. THE TLR LADDER IS
  CLOSED: string_arc's _note_use release arm is corpus-zero — the
  release-arm tripwire (fail-closed, then delete with 4a'/4b' after a
  clean cert cycle) is the next slice. Commit msg delivered.

- 2026-07-16 — RELEASE-ARM TRIPWIRE DESIGN CHECKPOINT (report-only;
  work/string-ownership-refactor/RELEASE-ARM-TRIPWIRE-DESIGN.md).
  STOPPED FOR REVIEW. The cert-cycle guard before deleting the dead
  arm; no deletion in the slice.
  CONFIRMED: committed TLR-7 reference (a1fa8f59, cleanup-tlr7) has
  temp_lastuse_release ABSENT from the aggregate (17 counters = exact
  corpus 0). BRANCH: string_arc.py:1698-1733 — the whole
  `use_counts==0 and owned and not live_out` body (shim + audit note +
  append + discards). DECISION: tripwire the WHOLE body (the condition
  IS the defect signal post-TLR-7; releasing would mask the accounting
  hole; the TLR-1 shim retires with it). PAYLOAD: fn, block[idx],
  value, fn-wide producer + family flag, use_count/consume/live_out,
  intake path issues/string-arc-release-arm-tripwire/; AssertionError →
  existing driver boundary wrap → clean internal diagnostic.
  KEY COST SURFACED: arc-only insert_string_arc stops being a valid
  configuration for family-temp MIR — nearly every reporter pin's
  config-A leg would fire the tripwire; the slice must migrate the
  battery to a shared _run_pipeline helper, collapse A/B pins to
  single-config assertions (their prove-the-pass job is complete and
  untestable once the in-pass author dies), and retire the TLR-1 shim
  pin (subject deleted).
  NEW PINS: stale-unmigrated-family-temp (arc-only ConstString drain →
  trips) + truly-non-family owned temp (StringRetain dest → trips,
  family=False in payload) + optional driver-level internal-diagnostic
  shape pin. ACCEPTANCE: every counter +0 vs cleanup-tlr7 (materialized
  stays 618,744; events unchanged — the shim was corpus-dead);
  tripwire firing on ANY corpus fixture = stop trigger; memcheck
  STANDALONE in gate. After a clean cert cycle: delete with 4a'/4b' +
  retire SITE_CLASS_TEMP_LASTUSE_RELEASE from the enumeration.

- 2026-07-16 — TRIPWIRE DESIGN REVIEW AMENDMENTS (3 items): (1)
  BLOCKING — the driver-level internal-diagnostic pin is now MANDATORY
  (§6): the boundary-wrap containment is a user-facing contract;
  unit AssertionError coverage doesn't test it. (2) MEDIUM — §6a added:
  the slice must document the pipeline precondition in string_arc.py
  (module doc + comment at insert_string_arc: materialize first in
  production; bare use only for tests that intentionally avoid/reach
  the arm). (3) MEDIUM — §6b added: ALL five direct-caller test files
  scanned and PRE-CLASSIFIED — reporter battery migrates (§5);
  test_move_from_ref (t_str consumed by ConstructVariant → never
  reaches the arm) and the three no-family-producer files
  (return_swap/drop_before_overwrite_swap/recursive_type_guard) are
  SAFE AS-IS; slice must re-verify all four under the armed tripwire
  and add per-file exemption comments. Awaiting implementation
  go-ahead.

- 2026-07-16 — RELEASE-ARM TRIPWIRE IMPLEMENTED (per amended design;
  cert-cycle guard, NO deletion). VERIFICATION IN FLIGHT (corpus →
  memcheck strictly sequential; fast batteries green: reporter 56/56,
  stage2 369/369, guardrails 24/24). Code:
  (1) `_release_arm_tripwire(val, *, block_name, idx)` — fn-level
  closure sibling of `_dead_stake_tripwire`; structured payload: fn,
  block[idx], value, FN-WIDE producer + family flag (distinguishing
  stale-unmigrated-family vs non-family-producer firing classes),
  use_count/consume/live_out, intake path. The WHOLE `use_counts==0 &&
  owned && !live_out` branch body replaced (shim + audit note + append
  + discards all retired — the TLR-1 shim dies here); the branch
  comment records the accounting-hole rationale and the deletion
  schedule (with 4a'/4b' after a clean cert cycle).
  (2) PIPELINE PRECONDITION documented at both surfaces: module doc +
  comment at insert_string_arc (materialize first in production; bare
  use only for tests that intentionally avoid/reach the arm).
  (3) INTAKE DOC issues/string-arc-release-arm-tripwire/description.md
  (mirrors the dead-stake intake; both firing classes + triage rule).
  (4) BATTERY MIGRATION (the bulk): shared `_run_pipeline` helper;
  TLR-1 shim pin RETIRED (subject deleted); four conformance pins'
  live halves run the pipeline; twelve A/B pins collapsed to
  single-config (config-A legs + agg_a comparisons deleted with
  retirement notes; pass-output layout + recognition counters are the
  surviving contract); tlr4_nonfamily REWORKED into the call-shape
  non-family tripwire carrier (pass materializes only %xb, then arc
  TRIPS family=False); tlr6 teeth pin adapted (guard-missing failure
  mode is now the tripwire; pass-materialized both releases, must sail
  through with exactly one release per temp).
  (5) EXEMPTION COMMENTS added to all four external caller files
  (return_swap / drop_before_overwrite_swap / recursive_type_guard:
  no family producers; move_from_ref: t_str consumed) — all 23 tests
  re-verified green under the armed tripwire.
  (6) NEW PINS: stale-family (bare arc, ConstString drain →
  family=True + producer=ConstString in payload); non-family
  (StringRetain carrier, production-faithful pipeline → family=False);
  MANDATORY driver-level diagnostic pin — materialization pass no-op'd
  via monkeypatch so REAL source ("a"+"b" comparison) reaches the REAL
  arm through the REAL boundary wrap → clean `internal:` diagnostic
  with lastuse_release_arm payload asserted end-to-end.
  EXPECTED ACCEPTANCE (vs cleanup-tlr7): EVERY counter +0
  (materialized stays 618,744; temp_lastuse stays absent; events
  unchanged); universe identical; gates zero; NO corpus fixture trips
  the arm (any firing = stop trigger falsifying TLR-7 coverage);
  memcheck standalone. After acceptance: tripwire held through a clean
  FULL SUITE (user-run) before any deletion.

- 2026-07-16 — TRIPWIRE SLICE REVIEW ROUND (1 blocking + 3 medium):
  (1) BLOCKING — `_ensure_owned`'s temp_lastuse release half REMOVED:
  since 4b it could only execute en route to the unconditional
  dead-stake raise one statement later ("dead-in-effect", the TLR
  measurement corollary), and its audit note polluted doomed-compile
  records; removal is behavior-neutral and completes the fail-closed
  claim — NO live emission site tags SITE_CLASS_TEMP_LASTUSE_RELEASE
  (design §6c added; 4b comment at the site records the removal).
  (2) predicate docstring: consumer list corrected (analysis/
  recognition + tripwire family flag; shim recorded as retired third).
  (3) reporter constants: materialized_lastuse_release rewritten to
  the post-TLR-7 pass/recognition meaning (TLR-1 origin kept as
  history); temp_lastuse_release gains the historical fail-closed
  note. (4) six stale A/B-equivalence docstrings rewritten to the
  single-config framing. Batteries green on the settled tree
  (reporter 56/56, stage2 369/369); the mid-run-tainted corpus gate
  was killed and RELAUNCHED on the settled tree (cleanup-tripwire).

- 2026-07-17 — RELEASE-ARM TRIPWIRE SLICE ACCEPTED (final):
  build/tmp/cleanup-tripwire, exit 0. EVERY counter +0 vs cleanup-tlr7;
  temp_lastuse_release still ABSENT (17 counters);
  materialized_lastuse_release unchanged at 618,744; events 2,772,052
  unchanged; universe identical 924/344/49; all nine hard gates zero;
  NO fixture tripped either tripwire (a firing would have failed the
  compile and broken universe identity). STANDALONE memcheck 102
  passed + 1 skipped. Batteries: reporter 56/56; stage2 369/369;
  guardrails 24/24. Commit msg delivered. NEXT: the tripwire holds
  through a clean FULL SUITE (user-run); deletion (this arm + 4a'/4b'
  + retiring SITE_CLASS_TEMP_LASTUSE_RELEASE) only after that cert
  cycle.

- 2026-07-17 — TLR-8: MoveOut JOINS THE FAMILY (first production
  tripwire catch, same day as slice acceptance).  drift-workflows'
  staged-0.33.83 alignment hit the release-arm tripwire on 15 sites,
  all one class: `"lit" + move s` — a moved String operand draining at
  a non-consuming concat (family=False, producer=MoveOut, use_count=0;
  issues/string-arc-release-arm-tripwire/, pinned minimal repro +
  three production firings).  The toolchain corpus has ZERO `+ move`
  concat sites, so the TLR measurement never saw the class — the
  tripwire surfaced it as a clean ICE exactly as designed.  Fix is the
  TLR-6 shape: (1) `M.MoveOut` added to
  `is_materialized_release_family_producer` (dest inherits the storage
  local's +1 verbatim — unconditional owner); (2) `seed_string_dest_types`
  MoveOut arm (instruction-carried ty); (3) the expansion arm's
  owned/move-only re-add guarded on `recognized_released` (live
  rewrite-loop re-add after the per-block subtraction — the TLR-6
  teeth lesson).  Consumed move dests disqualify at the calculator as
  before.  Pins: tlr8 family / guard-teeth / cross-block / driver-level
  end-to-end (real source) + memcheck row
  test_move_operand_concat_release.py covering all three production
  shapes (plain, match-binder arm, chained) — throw-path shape
  verified manually (compile+run+valgrind clean).  Repro compiles,
  runs (len 8), valgrind 13/13 clean.  Batteries: stage2 373/373
  (incl. reporter 60/60); standalone memcheck run.  RESOLUTION.md
  written in the issue folder.  NEXT: user-run full suite → stage a
  fixed 0.33.83 for drift-workflows (their alignment is blocked on
  this; they will NOT work around in app code per intake triage).

- 2026-07-17 — TLR-8 REVIEW ROUND (2 findings, both addressed):
  (1) version/history: DRIFTC_VERSION bumped 0.33.83 → 0.33.84
  (behavior-changing ownership fix; ABI stays 21 — no boundary shape
  changed), doc/history.md 2026-07-17 entry written; (2) throw-path
  shape (production firing 3) promoted from manual verification to a
  PINNED memcheck row (row 4 `reject`: moved concat into an
  error-constructor field, error edge exercised every third i through
  the try/catch fallback) — fixture green under valgrind.

- 2026-07-18 — **0.33.84 / ABI 21 CERTIFIED** (maintainer). The
  tripwire slice's deletion condition ("one clean cert cycle with
  zero firings", RELEASE-ARM-TRIPWIRE-DESIGN.md §8) is MET: full
  suite + cert ran with all three tripwire families armed, zero
  firings (TLR-8's production catch was fixed in-tree pre-cert).

- 2026-07-18 — **TRIPWIRE-DELETION PLAN CHECKPOINT (report-only) —
  STOPPED for approval.** work/string-ownership-refactor/
  TRIPWIRE-DELETION-PLAN.md (copy:
  /tmp/drift-announce/2026-07-19T011159Z-tripwire-deletion-plan.md).
  Maintainer's approved skeleton folded in verbatim (delete release
  arm + `_release_arm_tripwire`; delete the three 4a store branches
  + 4b `_ensure_owned` funnel + `_dead_stake_tripwire` once unused,
  preserving move paths and untyped pass-through; retire
  SITE_CLASS_TEMP_LASTUSE_RELEASE per the destructor_self/retired-C4
  discipline — constant kept, out of the closed set AND out of
  finalize's _counted_only; retire only tripwire-specific tests,
  keep TLR-8 + all memcheck rows; update both intake folders).
  Code-forced tweaks flagged for confirmation (§3): T1 store-arm
  if/else COLLAPSE (move and fallback arms are identical
  post-deletion — the tripwire was the only difference); T2 driver
  boundary wrap KEPT + tripwire-diagnostic pin GENERALIZED into the
  wrap-containment pin (the 2026-07-13 review made wrap containment
  a mandatory user-facing contract — it outlives its in-tree
  assertion sources); T3 tlr4_nonfamily REWORK not retire (stay-out
  half is live pass contract); T4 tlr6/tlr8 guard-teeth keep teeth
  via the exactly-one-release output-MIR assertion; T5 four
  bare-caller exemption comments reworded not removed; T6 historical
  "config-A retired" docstrings untouched. NO corpus-tool change
  (v1.6.0 stays; resurrected tag → UNTAGGED, already hard-gated —
  destructor_self precedent). Failure-mode conversion recorded
  honestly (§4): out-of-corpus defects go from clean ICE to leak
  (release side) / over-release (stake side); surviving guards are
  the four retain site-class hard gates + UNTAGGED + memcheck.
  Acceptance (§7): every counter +0 vs build/tmp/cleanup-tripwire
  (doubles as the missing post-TLR-8 corpus reference — TLR-8 is
  corpus-invisible, zero `+ move` sites), standalone memcheck
  102+1skip, reporter/stage2/guardrails, identical universe, gates
  zero. OPEN ITEMS (§8): drift-workflows clean rebuild on certified
  0.33.84 on record; version recommendation = NO bump (no observable
  behavior change on valid source, ABI stays 21); confirm T1-T6.
  No code changed; nothing staged.

- 2026-07-18 — **DELETION SLICE APPROVED (maintainer) with one
  BLOCKING correction; implementation started.** T4 rev-1 reasoning
  was WRONG and is corrected in the plan (rev 2): with the release
  arm deleted, the TLR-6 CopyValue and TLR-8 MoveOut
  `recognized_released` re-add guards are THEMSELVES dead — removing
  them cannot produce a second release (recognition copies the
  materialized release through without consulting `owned_values`; the
  re-owned state MAY propagate — AssignSSA copies owned membership —
  and affect branch selection via `_can_move_owned_once`, but every
  affected branch is output-equivalent: `_ensure_owned` is identity,
  the store paths are unconditional, `_note_use` only changes
  bookkeeping — so no branch can author another instruction or
  release.  Proof correction applied 2026-07-18 in two rounds,
  superseding both the "only consumer"/all-USE phrasing AND the
  "never acted on" phrasing here, in the plan, and at the string_arc
  MoveOut arm).
  → both guards DELETE this slice; `test_tlr6_copyvalue_guard_teeth`
  + `test_tlr8_moveout_guard_teeth` RETIRE; TLR-6/8 family,
  cross-block, end-to-end, and memcheck regressions all preserved.
  FLAGGED not acted on: the same-argument ConstString +
  StringFrom*/Concat re-add guards stay per the correction's letter.
  Decisions on record: external precondition MET (certified run
  20260719-001008-drift-lang-99a68ee exercised drift-workflows
  0251b24; staging + normal/debug test + stress + perf, ZERO tripwire
  log matches); NO version bump, NO ABI bump (ABI 21); T1/T2/T3/T5/T6
  approved — T2's generalized containment pin must explicitly assert
  phase/`internal:` diagnostic, EMPTY IR, and no traceback; count
  corrected: 7+2 retires, reporter collection 60 → 51.

- 2026-07-18 — **TRIPWIRE-DELETION SLICE IMPLEMENTED (per approved
  plan rev 2). VERIFICATION IN FLIGHT** (corpus vs cleanup-tripwire
  running; memcheck strictly sequential after it; fast batteries
  GREEN: reporter 51/51 — exactly the corrected count, stage2 364/364
  (= 373 − 9 retirements), guardrails 27 (corpus-tool 7 +
  mutation-audit 6 + dirty-bit 10 + extraction contract 4)). Code:
  (1) string_arc.py — `_note_use` release-arm branch DELETED (replaced
  by a six-line no-arm comment; owned temps draining to zero are inert
  bookkeeping); `_release_arm_tripwire` + `_dead_stake_tripwire` defs
  DELETED; the three 4a store fallbacks COLLAPSED to the unconditional
  retain-free consume (T1 — arms were identical modulo the tripwire);
  `_ensure_owned` now an identity pass-through with the retirement
  rationale (13 call sites untouched — funnel shape kept greppable per
  plan; count corrected 2026-07-18, an earlier "14" was a miscount);
  T4-corrected: CopyValue + MoveOut `recognized_released` re-add
  guards DELETED (recognition copies the materialized release through
  without consulting `owned_values`; the re-owned state may propagate
  and steer branches, but every affected branch is output-equivalent
  — no branch can author another instruction or release);
  ConstString/StringFrom*/Concat guards KEPT per the correction's
  letter, comments now say consistency-only; module doc +
  insert_string_arc precondition reworded (bare use = silent
  under-release, not a trip); stale surfaces fixed (predicate
  docstring consumers, subtraction comment, metadata pre-scan
  rationale, two retain-for-additional-consumers phrases →
  string_stakes, recognition fail-closed comparison referent).
  (2) reporter — SITE_CLASS_TEMP_LASTUSE_RELEASE retired from
  STRING_ARC_SITE_CLASSES AND finalize's `_counted_only` (constant
  kept, destructor_self-style comment: future notes → UNTAGGED, hard
  gate); materialized comment records the arm deletion.
  (3) driftc.py — boundary wrap KEPT (generic phase containment),
  comment updated. (4) corpus tool — NO change, v1.6.0, four retain
  hard gates stay. (5) tests — 9 RETIRED (4 dead-stake fires +
  `_expect_tripwire`, 3 release-arm pins, 2 T4 teeth) each with a
  retirement note; containment pin GENERALIZED to
  `test_string_arc_boundary_wrap_contains_assertions` (injected
  generic AssertionError → asserts `internal:` prefix + payload,
  diagnostic phase == "string_arc", EMPTY IR, compile-returned =
  no-traceback — per maintainer spec); destructor_self pin extended to
  `test_retired_site_classes_are_untagged` (both retired tags, count
  stays 51); tlr4_nonfamily REWORKED (T3): clean run, arc adds NOTHING
  — exactly one %xb release, none for %th/%ni/%ti, materialized == 1;
  `_run_pipeline`/`_string_shuffle_func`/c2-pin docstrings updated;
  four bare-caller exemption comments → BARE-USE SAFETY wording (T5);
  historical Config-A notes untouched (T6). (6) intakes — release-arm
  RESOLUTION.md closure entry (cert run + drift-workflows evidence +
  deletion outcome + TLR-8 pins preserved); dead-stake folder gains
  RESOLUTION.md (never fired, branches deleted, hard gates survive).
  Leftover-reference sweep clean (one intentional retirement-note
  mention). No version bump, no ABI bump (ABI 21) per decision.
  EXPECTED ACCEPTANCE: every counter +0 vs cleanup-tripwire (17 keys;
  doubles as the post-TLR-8 corpus reference), universe 924/344/49,
  nine hard gates zero, exit 0; then standalone memcheck 102+1skip.

- 2026-07-18 — **TRIPWIRE-DELETION SLICE ACCEPTED (final):**
  build/tmp/cleanup-tripdel vs cleanup-tripwire, tool v1.6.0, exit 0.
  EVERY counter +0 (all 17 aggregate keys byte-identical:
  materialized_lastuse_release 618,744; events 2,772,052;
  temp_lastuse_release ABSENT; c3 ladder, arraydrop mix, drift —
  everything); universe identical 924/344/49; all hard gates zero.
  This run is also the post-TLR-8 corpus reference (TLR-8 confirmed
  corpus-invisible: +0 across the board). STANDALONE memcheck
  **103 passed + 1 skipped** (102+1 prior + the TLR-8 review's
  throw-path row — count reconciles exactly), exit 0. Batteries:
  reporter 51/51; stage2 364/364; guardrails 27. cleanup-tripdel is
  the new phase reference. THE TRIPWIRE ERA IS CLOSED: string_arc
  authors no last-use releases and carries no fail-closed stake arms;
  surviving guards are the four retain site-class hard gates +
  UNTAGGED + the memcheck rows. Commit msg delivered. NEXT (roadmap
  corrected 2026-07-18 — the earlier line here listed Array
  release-elision, which LANDED 2026-07-14/cleanup-arrelide and
  shipped in certified 0.33.84):
  (1) Branch `string-arc-endgame-array-sweep` (approved bundling of
  the sweep retirement + the guard cleanup; report-only checkpoint
  first — must compare flag-modeling vs migration into
  cleanup_authoring, account BIJECTIVELY for all 4,620 residual
  drops, and prove `_drop_all_arrays` deletable safely):
    - Sub-slice A: delete the same-argument
      ConstString/StringFrom*/Concat re-add guards (consistency-only
      since the release arm's deletion); acceptance = every counter
      +0.
    - Sub-slice B: migrate the 4,620 PATH_DEPENDENT Array drops,
      delete `_drop_all_arrays`; then compiler 0.33.85 / ABI 21
      certification and release.
  (2) Recorded small follow-ups: projected-place scrutinee audit,
  ConstShare synthesis visibility, spec wording for the
  match-consume exception.
  (3) string_arc endgame inventory → B-repr(B5) entry criteria.

- 2026-07-18 — **Documentation-closure round (4 items, maintainer
  list; no code-behavior change).** (1) T4 proof correction applied
  at the string_arc MoveOut arm, in TRIPWIRE-DELETION-PLAN.md §3, and
  in both PROGRESS proof passages: the "release arm was the ONLY
  consumer"/all-USE phrasing replaced.  [Round 2, same day: the
  first replacement's "never acted on" was ALSO inaccurate — AssignSSA
  propagates owned membership and `_can_move_owned_once` reads it.
  Final proof form everywhere: the re-owned state may propagate and
  affect branch selection, but every affected branch is
  output-equivalent (`_ensure_owned` identity, unconditional store
  paths, `_note_use` bookkeeping-only), so no branch can author
  another instruction or release.  Same correction extended to the
  two adjacent "inert" comments (prepass-subtraction + ConstString
  arm → "output-neutral", both pointing at the MoveOut arm proof).]
  (2) `_ensure_owned` call-site count corrected 14 → 13 (miscount;
  actual sites verified by grep). (3) The stale NEXT roadmap in the
  acceptance entry (listed already-landed Array release-elision)
  replaced with the corrected ladder — array-sweep retirement
  checkpoint first (flag-model vs cleanup_authoring migration,
  bijective 4,620 accounting, `_drop_all_arrays` deletion proof;
  implementation = 0.33.85/ABI 21 + cert). (4) doc/history.md
  0.33.84 entry now records the Array release-elision it shipped
  (landed 2026-07-14, bump deferred into 0.33.84): heading extended +
  dedicated paragraph (ledger-consulting sweep, 156,308 → 4,620,
  memcheck carrier). Announce copy of the plan refreshed. AWAITING
  final static closure; array-sweep-retirement checkpoint opens only
  after it, per direction.

- 2026-07-18 — **string-arc-endgame-array-sweep CHECKPOINT COMPLETE
  (report-only, cleared by maintainer) — STOPPED for review; no
  implementation GO.** ARRAY-SWEEP-RETIREMENT-CHECKPOINT.md (copy:
  /tmp/drift-announce/2026-07-19T060000Z-array-sweep-retirement-
  checkpoint.md). HEADLINE — the bijection measurement (scratch
  instrument, build/tmp/bij-measure, exit 0, universe identical
  924/344/49; instrumentation REVERTED byte-identically:
  cmp-verified against pristine copies, zero SCRATCH refs in tree,
  reporter battery 51/51 on the restored tree) shows the 4,620
  residual Array drops are THREE STDLIB FNS × 924 fixtures, in TWO
  classes, and Arm M's original "flip the skip" covers NEITHER
  as-framed:
  - 3,696 UNMATCHED (std.json::_parse_array `items` ×2 +
    _parse_object_throwing `occurrences` ×2 per fixture):
    cleanup_authoring ALREADY authored complete flag-guarded cleanup
    (_KIND_GUARDED — co-located c3_moveout_flag_guarded counts, so
    no _KIND_SKIP record exists to flip); the sweeps sit in the
    guarded emission's own {blk}_cleanup_post_{local} continuation
    blocks (cleanup_authoring.py:563). Under the flag≡owns
    invariant + entry zero-init + zero-drop-no-op, the residual
    drop is a PROVEN NO-OP on every path → retire by deletion
    (B-U): no new emission, no observable order change.
  - 924 FN/LOCAL-FALLBACK (std.fs::read_to_bytes
    `__match_binder_4_bytes`): genuine _KIND_SKIP at the match_join2
    hook, sweep at match_join. RESOLVED STATICALLY by maintainer
    review (fs.drift:285): close-success moves bytes into Ok;
    close-FAILURE returns Err(ce) WITHOUT consuming bytes — the
    array is genuinely LIVE on the close-error arm, today freed
    only by the sweep. B-M = author exactly 924 unguarded drops at
    the EXISTING match_join2 CleanupHook (NOT the swept exit); the
    authored MoveOut zero-backs all paths and the sweep note dies.
    A REAL drop migration and a REAL RAII-order change (sweep
    sorted-order at match_join → reverse-decl hook order at
    match_join2; fs instance has Byte elements so no observable
    destructor reordering there; general contract pinned by the
    ordering carrier).
  - matched_exact 0, skiprec_orphan 0 — all 4,620 accounted; both
    non-exact classes explained per the review rule.
  Zero-safety proof on the ACTUAL chain: string_arc's entry-block
  array zero-init (~1578 loop; array allocas NOT auto-zeroed) +
  MoveOut zero-backs + len-0 element helper /
  drift_free_array(NULL) no-op (array_runtime.c:75); entry-init
  recorded as a SURVIVING string_arc responsibility → endgame
  inventory. Predicate extraction carries the maintainer's
  migration rule verbatim: zero_storage_drop_safe in
  drop_policy_compute.py replaces EVERY production consumer —
  cleanup_authoring.py:121, drop_flags.py:276/308 (which today
  imports the variant-only name FROM string_arc), the reporter's
  zero_safe_ty wiring (string_arc.py:2832); match_cleanup_authoring
  is NOT a consumer (line 40 = docstring mention only, per
  maintainer correction); wrapper compat/tests-only. Arm F reframed
  honestly per maintainer: it would flag ONLY the 924 remaining
  sites (the 3,696 are already flag-managed — nothing left for F to
  guard there); rejected for runtime flag state + MIR growth
  against zero benefit vs the unconditional authored drop
  (success-arm storage already zero-backed by the move into Ok).
  Destruction order: B-U removes only no-ops (nothing observable);
  B-M is a REAL RAII-order change for the 924 live drops (recorded
  for the 0.33.85 history entry); ordering carrier pin specced per
  maintainer (both condition outcomes, PD array among live arrays +
  interleaved destructible). Pins: maintainer's five verbatim +
  B-U retirement pin + B-M authored-drop pin (hook-position drop,
  no sweep drop, both close arms valgrind — error arm is the
  leak-direction guard) + json-parser and read_to_bytes memcheck
  rows; test_array_release_elision.py rows stay. PREDICTED
  ACCEPTANCE (DEFINITIVE per maintainer — optional all-no-op
  outcome removed): events +924, moveout_expansion +924,
  c3_moveout_zero_safe +924 (predicate leg required — without it
  +924 c3_moveout_not_owned = hard-gate failure); the three
  arraydrop keys vanish (4,620 → 0 each); every other counter +0.
  Sub-slice A1 APPROVED (both guards + the asymmetric subtraction)
  under the T4 output-equivalence proof; INDEPENDENT
  every-counter-+0 acceptance, no bump. Sequencing: A then B; B =
  compiler 0.33.85 / ABI 21 → certification + release. Stop
  conditions §9 (incl. authored-drop-fails-to-kill-sweep-note).
  Scratch restoration + report-copy identity verified statically by
  maintainer. NO code changed in this checkpoint; tree state = the
  reviewed tripwire-deletion slice, untouched. Awaiting
  implementation GO.

- 2026-07-19 — **GO granted (maintainer) after two doc-only
  corrections (both applied):** (1) string_arc's direct site-3
  predicate call (~2691, the initialized_at_return variant widening
  — arrays can't reach it via destructible_locals, but the
  no-variant-only-name rule stands) added to the consumer inventory;
  (2) §8's "(null) ordering impact" replaced with the real 924-site
  RAII-order change. Implementation order pinned: A1 → B-M → step-3
  corpus verification (+924 triple; arraydrop 4,620 → 3,696) → B-U →
  final 0.33.85/ABI 21 acceptance + certification.

- 2026-07-19 — **Sub-slice A1 ACCEPTED (independent gate):**
  ConstString + StringFrom*/Concat re-add guards AND the prepass
  `owned_values -= recognized_released` subtraction deleted under
  the T4 output-equivalence proof (recognition machinery — prescan
  exclusion + copy-through arm — untouched; stale prepass comments
  at the Exc*/Call arms rewritten). Gate: stage2 364/364; standalone
  memcheck 103+1skip; corpus build/tmp/sweep-a1 vs cleanup-tripdel
  EXIT 0, EVERY counter +0 (17 keys), universe identical 924/344/49,
  gates zero.

- 2026-07-19 — **B-M IMPLEMENTED (verification corpus in flight).**
  Code: (1) `zero_storage_drop_safe(ty, tt)` NEW in
  drop_policy_compute.py (VARIANT tag-0 + ARRAY zeroed-header; else
  fail closed; docstring carries the entry-init dependency note).
  (2) MIGRATION RULE executed — every production consumer switched:
  cleanup_authoring PD ladder; drop_flags 2b criterion (~276/308 —
  arrays now never admitted via 2b, matching Arm M; 2a untouched);
  string_arc site-3 widening call + the reporter zero_safe_ty lambda
  (2832). `variant_zero_tag_drop_safe` reduced to a VARIANT-ONLY
  compat shim (tests only; widened-wrapper form rejected — a variant
  name admitting arrays would lie). Stale docs fixed in
  match_cleanup_authoring/mir_nodes/reporter/cleanup_authoring
  module docs. (3) LADDER SHAPE (counter-neutrality decision,
  flagged in checkpoint §2.4/§2.3): PD arrays take UNGUARDED
  authoring ONLY when not flag-managed; flag-managed arrays (the
  2a-admitted json accumulators) keep the guarded family so the
  slice stays exactly ±924; variants keep today's predicate-first
  decision verbatim. PINS ALL GREEN: predicate contract 3/3 (incl.
  fail-closed SOURCE-SCAN pin enforcing the migration rule — any new
  production call of the variant-only name fails with a STOP
  message, and a compat-shim-stays-variant-only pin);
  cleanup_authoring 14/14 (+ unguarded-PD-array pin: emitted, drop
  chain, no block split, not flag-managed; + flag-managed-array
  keeps-guarded-family pin: flag-clear present, no unconditional
  hook drop); reporter c3 array ladder pin (paired MAYBE_UNINIT
  array MoveOut → zero_safe via the REAL predicate; unpaired →
  divergent/hard-gated); RAII ordering carrier
  test_array_sweep_raii_order.py GREEN (real source, both condition
  outcomes, exact destroy order pd→mid→live1 — PD array normalized
  onto reverse-decl RAII); NEW memcheck rows
  test_array_sweep_retirement.py (read_to_bytes shape both arms +
  json-parser shape both exits, heap strings, valgrind 0/0) — passed
  standalone. stage2 FULL 370/370 (364 + 6 new). Step-3 corpus
  (build/tmp/sweep-bm vs sweep-a1) RUNNING; K's predicted deltas:
  events/moveout_expansion/c3_moveout_zero_safe each +924,
  scope_exit_arraydrop 4,620 → 3,696.

- 2026-07-19 — **Step-3 verification EXACT (build/tmp/sweep-bm vs
  sweep-a1, exit 0):** events +924; site_class:moveout_expansion
  +924; c3_moveout_zero_safe +924; scope_exit_arraydrop 4,620 →
  3,696 with arraydrop_state:maybe_uninit / verdict:path_dependent
  tracking −924 exactly; c3_moveout_flag_guarded +0 (the json
  accumulators provably untouched — the counter-neutral ladder
  held); every other counter +0; universe identical; gates zero.
  The read_to_bytes sweep note died via the authored MoveOut — the
  hook→Return dataflow stop condition did NOT fire. B-M VERIFIED;
  cleared to B-U per the implementation order.

- 2026-07-19 — **B-U IMPLEMENTED — the Return-boundary array sweep
  is GONE.** Deleted: `_drop_all_arrays` (def + sole call), the
  MUST_NOT_DROP array elision fold, and the Slice-3 note_array_drop
  loop — replaced by a single no-sweep comment recording the
  bijection provenance and the two surviving dependencies
  (`_drop_array_local` for the overwrite path; the entry-block
  array zero-init, endgame-inventory item). Reporter's array_drops
  API kept for direct contract tests (comment marks the production
  note site historical); four stale `_drop_all_arrays` doc mentions
  rewritten. PIN REWORKS: the two sweep-pinning tests flipped into
  B-U retirement pins — test_arraydrop_note_site_covers_return_
  sweep now asserts ZERO arraydrop keys and ZERO Return-boundary
  ArrayDrops (LIVE sink included: cleanup_authoring hooks are the
  sole scope-exit authority; bare carriers deliberately hook-less)
  with overwrite drops intact; test_array_elision_keeps_path_
  dependent_drop asserts the PD local gets NOTHING from arc (the
  authoring covered by the cleanup_authoring unguarded-array pin);
  the direct-API reporter pin stays. Stale row-3 wording in
  test_array_release_elision.py updated (sweep → authored cleanup).
  Batteries: stage2 FULL 370/370; array runtime carriers 3/3
  (elision rows + retirement rows + RAII ordering carrier) — all
  green post-deletion. Version bumped 0.33.85 (ABI stays 21);
  doc/history.md entry written (sweep retirement, predicate
  extraction + migration rule, RAII-order note, flag-managed-array
  follow-up, A1 rider). FINAL acceptance corpus
  (build/tmp/sweep-final vs cleanup-tripdel) RUNNING; standalone
  memcheck follows strictly after.

- 2026-07-19 — **string-arc-endgame-array-sweep ACCEPTED (final):**
  build/tmp/sweep-final vs cleanup-tripdel, tool v1.6.0, exit 0 —
  EXACTLY the checkpoint §7 definitive table: events +924,
  site_class:moveout_expansion +924, c3_moveout_zero_safe +924
  (9,330 → 10,254); scope_exit_arraydrop / arraydrop_state:
  maybe_uninit / arraydrop_verdict:path_dependent all 4,620 → 0
  (aggregate 17 → 14 keys); EVERY other counter +0 incl.
  c3_moveout_flag_guarded 8,316 (json accumulators byte-identical);
  universe identical 924/344/49; all hard gates zero. STANDALONE
  memcheck **104 passed + 1 skipped** (103+1 prior + the new
  test_array_sweep_retirement row — reconciles exactly). Batteries:
  stage2 370/370 (364 + zero_storage 3 + cleanup_authoring 2 +
  c3-array ladder 1); RAII ordering carrier + array memcheck
  carriers green. Sub-slice gates on record: A1 +0
  (build/tmp/sweep-a1); step-3 ±924 (build/tmp/sweep-bm).
  sweep-final is the new phase reference. Version 0.33.85, ABI 21;
  history.md entry written. THE RETURN-BOUNDARY ARRAY SWEEP IS
  RETIRED: cleanup_authoring is the sole scope-exit array authority;
  string_arc's remaining array responsibilities = entry-block
  zero-init (endgame inventory) + the overwrite-path
  `_drop_array_local`. Commit msg delivered. NEXT: user-run full
  suite → 0.33.85 certification + release; then the recorded
  follow-ups (flag-managed-array unguarded unification with its own
  predicted-delta acceptance; small follow-ups; string_arc endgame
  inventory → B-repr(B5) entry criteria).

- 2026-07-19 — **Static review round (CHANGES REQUIRED → all four
  code items CLOSED; item 5 git materialization is the
  maintainer's, per direction).** Review at /tmp/drift-announce/
  2026-07-19T123424Z-drift-lang-release-notes.md.
  (1) Reporter arraydrop note surface DELETED —
  SITE_CLASS_SCOPE_EXIT_ARRAYDROP constant, StringArcAudit.
  array_drops, note_array_drop, and the finalize aggregation loop
  (no compatibility consumer identified; banner comment records
  that a resurrected tag counts UNTAGGED = hard gate); the
  direct-only test_arraydrop_measurement_mix_and_inertness RETIRED
  with a note; unused `classify` import dropped.
  (2) Dead ARRAY half of the Return alias-walk skip REMOVED
  (String-only now; no downstream consumer existed post-B-U —
  arrays excluded from destructible_locals, _release_all_locals and
  the boundary audit intersect strings); comment rewritten;
  test_array_return_source.py audit narrative updated to record the
  removal. string_arc's remaining array responsibilities are now
  EXACTLY the claimed two: entry-block zero-init +
  overwrite-path _drop_array_local.
  (3) Stale-authority wording swept: test_array_release_elision
  header + row comments; both memcheck failure-message touch-point
  lists (cleanup_authoring named as the scope-exit authority);
  drop_flags module doc + 2b criteria and cleanup_authoring's four
  docstring surfaces rewritten to the zero-storage-unsafe vs
  zero-storage-safe rule (historical `path_dependent_non_variant_
  skip` tag NAME kept for triage-tooling stability, noted at each
  surface); reporter C3 comments "zero-tag" → the generic
  predicate.
  (4) Migration pin HARDENED to an AST scan (ImportFrom incl.
  aliases, Attribute refs, bare Names, shadow defs — anywhere in
  lang/driftc outside the shim's own def; docstrings/comments
  invisible by construction). TEETH PROVEN live: a function-level
  aliased import probe was caught at its exact line
  (drop_flags.py:454 "import as _p"); probe reverted, cmp-verified,
  battery green. (The first probe attempt — module-level import —
  failed via circular-import collection error instead: loud but not
  the pin; the function-level probe is the honest demonstration.)
  GATES: stage2 369/369 (370 − 1 retired test, exactly the review's
  predicted decrease); closure corpus build/tmp/sweep-closure vs
  sweep-final EXIT 0, EVERY counter +0 (14 keys both sides —
  production deltas unchanged per the review requirement);
  standalone memcheck 104 passed + 1 skipped (unchanged — the
  memcheck-file edits were docstrings and failure-message text
  only). Item 5 (materialize the
  candidate on branch string-arc-endgame-array-sweep; mixed index +
  three untracked test files) EXPLICITLY left to the maintainer
  ("you can ignore staged/unstaged... I take care of it").
  Awaiting the static delta review.

- 2026-07-19 — **Delta-review quick edits (4 items, maintainer list;
  parse-checked only, NO test runs per direction).** (1) The two
  remaining string_arc passages (~2475 scope note + authority
  boundary) no longer claim arrays share alias-walk/legacy
  authority — strings-only, with the array half's retirement and
  cleanup_authoring's authority stated in both. (2)
  test_site3_return_source_alias_walk.py docstring rewritten
  String-only end to end (title, shape, location note, gate wording,
  and the Array-note paragraph now records the branch's REMOVAL and
  points at test_array_return_source.py instead of deferring future
  work). (3) drop_flags private helper RENAMED
  _has_non_variant_path_dependent_at_cleanup_hook →
  _has_zero_storage_unsafe_path_dependent_at_cleanup_hook (def +
  sole call; the historical path_dependent_non_variant_skip TAG
  string is unchanged). (4) AST pin hardened: the exception is now
  EXACTLY ONE sync-def shim in string_arc.py (duplicate defs there
  are offenders; shim_defs == 1 asserted), and AsyncFunctionDef /
  ClassDef shadows are offenders everywhere including string_arc.
  Commit msg delivered.

- 2026-07-19 — **FLAG-RETIREMENT CHECKPOINT COMPLETE (report-only,
  per direction) — STOPPED for review.**
  FLAG-RETIREMENT-CHECKPOINT.md (copy: /tmp/drift-announce/
  2026-07-19T203317Z-flag-retirement-checkpoint.md). Corpus
  inventory (build/tmp/flaginv-measure, exit 0, universe identical;
  scratch instrument across drop_flags/cleanup_authoring/string_arc/
  reporter REVERTED byte-identically — 4× cmp vs pristine, zero
  scratch refs, battery 51/51):
  - Flag-managed ZERO-STORAGE-SAFE locals: ARRAY 15,711 (17/fixture,
    49,906 bookkeeping stores) + VARIANT 4,683 (~5/fixture, 18,757
    stores) = 20,394 locals / ≈137k bookkeeping instructions;
    zero-UNSAFE keepers: SCALAR-String 7,406 + STRUCT 4,691.
  - Identity: std.cli parse ×4, std.codec decode ×4, std.fs
    _read_all_capped, std.io poll_many, std.json values/items/
    occurrences, std.random buf, std.regex ×3 (arrays); fs cr +
    json child_sp/node/sp (variants — the C3-population carriers).
  - KEY FINDINGS: only 3,696 array flags still gate emission (the
    json guarded cleanups; c3fg event split ARRAY 3,696 / SCALAR
    2,772 / STRUCT 1,848); flag-managed VARIANTS (8,384 unguarded:
    VARIANT:flag decisions) ALREADY take unguarded authoring — their
    flags gate NOTHING (pure 2a-admission overhead + dead
    flag-clears); most flagged arrays (15,711 vs 3,696) never reach
    a PD hook at all.
  - ARMS: A = array-only unification (−3,696 flag_guarded →
    +3,696 zero_safe; 15,711 flags retired; leaves the variant
    anomaly + a ladder special case). B = GENERIC zs retirement
    (identical counter table — variants add ZERO movement; 20,394
    flags/137k instructions retired; admission becomes ONE rule:
    flag iff needs_drop AND NOT zero_storage_drop_safe; the B-M
    counter-neutrality exception dies naturally). RECOMMENDED: B.
  - Ordering: arrays move flag-branch → inline hook at the SAME
    boundary (no observable reordering; carrier stays); variants:
    zero emission change.
  - Safety: B-M zero-storage chain for arrays; tag-0 doctrine for
    variants; flag≡owns kept where needed (zero-unsafe); consumer
    audit 5 entries — the ONE live-decision consumer is string_arc
    site-3's _flag_managed_at_return (variants), subsumed by its
    ledger consultation (authored cleanup → MOVED_OUT →
    MUST_NOT_DROP) — REQUIRED PIN.
  - Acceptance (Arm B): c3_moveout_flag_guarded 8,316 → 4,620,
    c3_moveout_zero_safe 10,254 → 13,950, ALL else +0 (multiplicity
    note + stop conditions §8); implementation targets 0.33.86 /
    ABI 21 → cert → release.
  - Recorded observation (OUT of scope): String zeroed-release is
    also a runtime no-op — a String-predicate slice could retire
    7,406 more flags, but only AFTER the string_arc re-homing
    inventory.
  NO code changed; tree = the reviewed 0.33.85 candidate. NEXT
  after approval+slice: small recorded follow-ups → string_arc
  endgame/re-homing inventory → B-repr(B5) entry audit.

- 2026-07-20 — **Flag-retirement checkpoint REV 2 — all four review
  amendments closed (report-only; Arm B reviewer-preferred; GO
  attaches on closure).** Review: /tmp/drift-announce/
  2026-07-20T020349Z-drift-lang-release-notes.md; rev 2 copy:
  2026-07-20T021501Z-flag-retirement-checkpoint-rev2.md.
  (1) FULL-POPULATION IDENTITY CLOSED (§1.2a): per-fixture
  aggregate mining + instrumented re-probe of every deviant class
  (instrument re-applied then REVERTED byte-identically — 4× cmp,
  zero scratch refs, battery 51/51). ARRAY +3 = FutureGroup<T>::
  join_all `out` generic inst ×2 fixtures (+3+3 stores) +
  struct_ctor fixture-local `names` (+4 stores) — sums close
  exactly (15,708+3; 49,896+10). VARIANT +63 across 53 fixtures =
  fixture-local user Optional/Result locals (pop results, match
  scrutinees/results, FutureGroup join results; 18,480+277 stores);
  +68 unguarded decisions verified per-fn (m ×5 hooks, popped* ×6,
  r ×5, any/all ×2; 8,316+68). All extras = the SAME anomaly class
  (2a-admitted, flags gating nothing).
  (2) EMISSION-SHAPE PROOF (§6): fallback=3,696 / edge-elaborated=0
  DERIVED — pddec:guarded:ARRAY (decisions, pre-demotion) equals
  c3fg_kind:ARRAY (A-rule structural fallback-shape events); an
  edge demotion cannot match the A-rule (no flag-load guard block;
  classifies c3_moveout_owned) → demotions=0, exactly one fallback
  MoveOut per decision; ±3,696 table now DEFINITIVE, deviation
  stays STOP.
  (3) ADMISSION FORMULA CORRECTED (§2): zero-storage-safety is an
  ADDITIONAL EXCLUSION — needs_drop AND NOT zs AND user-moveout AND
  (2a OR 2b) — full pseudocode in-report; pins now cover 2a/2b
  positive AND negative controls + user-moveout precondition.
  (4) SITE-3 TIGHTENED (§5.3/§7): causality corrected — the ledger's
  MOVED_OUT transition (from cleanup_authoring's authored MoveOut,
  rebuilt ledger) drives the generic consultation's skip BEFORE
  _flag_managed_at_return is formed; zero-backing proves drop
  SAFETY only. Pin lands BEFORE the admission change and covers
  parse_located sp + a §1.2a fixture-specific variant; stale site-3
  flag-authority comments update in-slice.
  ACCEPTANCE ADDITIONS folded (§6): structural deltas — zs flag
  locals 20,394 → 0, zs stores 68,663 → 0 (+ConstBool pairs),
  3,696 guarded block pairs removed, zero-unsafe String/Struct
  populations byte-identical (7,406/27,762 + 4,691/16,842) — via
  the same scratch instrument re-run at acceptance. Tree unchanged
  (0.33.85 candidate); Arm B implementation → 0.33.86/ABI 21 →
  cert → release on GO.

- 2026-07-20 — **Arm B IMPLEMENTED + ACCEPTED (0.33.86 candidate,
  ABI 21) — review HOLD amendments closed same day.** Order per
  direction: (1) site-3 variant pins FIRST
  (lang/tests/memcheck/test_variant_flag_retirement.py: parse_located
  sp shape + Array.pop Optional shape, exactly-once destroy by EXACT
  stdout both outcomes + valgrind; green PRE-change then POST-change);
  (2) drop_flags zs ADDITIONAL exclusion (criteria preserved);
  cleanup_authoring PD ladder → one uniform rule (zs → UNGUARDED
  predicate-first/fail-closed; TypeKind import dropped); site-3
  flag-skip comment rewritten to the ledger-causality wording;
  (3) pins: admission 2a trio + no-move control + 2b criterion trio;
  B-M exception pin reworked to stale-metadata fail-closed;
  (4) 0.33.86 + history entry.
  ACCEPTANCE — ALL EXACT: counters (build/tmp/flagret vs
  sweep-closure, exit 0): c3_moveout_flag_guarded 8,316 → 4,620,
  c3_moveout_zero_safe 10,254 → 13,950, EVERY other counter +0
  (events/moveout_expansion +0 = the fallback-only proof held).
  STRUCTURAL (build/tmp/flagret-struct, instrumented, exit 0):
  flagmgd/flagstores zs keys ABSENT (20,394 locals / 68,663 stores →
  0); SCALAR-String 7,406/27,762 + STRUCT 4,691/16,842
  BYTE-IDENTICAL; guarded block-pair removal evidenced by the exact
  event migration + the no-split unit pin. Instrument REVERTED
  byte-identically (3× cmp, zero markers, battery green).
  STANDALONE memcheck **105 passed + 1 skipped** (104 + the new
  variant row — reconciles exactly). stage2 372/372.
  REVIEW HOLD (2026-07-20T051815Z) — both static amendments CLOSED:
  (a) NEW test_2b_admission_through_insert_drop_flags — 2a-false/
  2b-true carrier (Unreachable-terminated; 2a counts Return blocks
  only) through the PRODUCTION admission fn: struct admitted via 2b
  alone, array/variant NOT on the identical carrier; helper pin kept
  as supplemental. (b) Five stale authority surfaces genericized
  (cleanup_authoring skip-tripwire comment ~136, author_cleanup
  docstring ~229, per-arm gate ~400, telemetry mapping ~812;
  mir_nodes CleanupHook contract ~470 — all now "ANY
  zero-storage-safe candidate, uniform since Arm B") and the
  reworked pin RENAMED
  test_authoring_ignores_stale_flag_metadata_for_zero_safe_array
  (old name proved the opposite behavior; retirement note kept).
  Comment-only amendment edits postdate the corpus runs —
  emission-inert by construction. Sweeps: zero scratch markers,
  zero stale wording. Commit msg delivered. NEXT: reviewer static
  delta → user-run full suite → 0.33.86 certification + release;
  then small recorded follow-ups → string_arc endgame/re-homing
  inventory → B-repr(B5) entry audit.

- 2026-07-20 — **Final wording hold CLEARED (review
  2026-07-20T053236Z: "implementation and acceptance approved";
  commit-cleared without another review loop).** Two exact
  comment-only corrections applied: (1) cleanup_authoring per-arm
  paragraph replaced with the actual classification contract
  (activates only when EVERY candidate is zero-storage-unsafe PD +
  flag-managed; UNGUARDED or SKIP anywhere disables per-arm;
  remaining hook emissions keep reverse-decl order; SKIP candidates
  emit NOTHING; mixed hooks keep the guarded fallback) — the old
  text wrongly said SKIP candidates "must emit"/"everything emits";
  (2) the 2b pin docstring no longer claims Unreachable is the only
  terminator — corrected to "no Return block anywhere; terminal
  block ends in Unreachable" (the diamond's IfTerminator/Goto
  acknowledged). Verified: parse OK, git diff --check clean, zero
  scratch markers (all families), touched batteries 26/26. No
  corpus/runtime rerun (comment/docstring only, per reviewer). The
  0.33.86/ABI 21 candidate is COMMIT-CLEARED; maintainer's full
  serial suite + certification follow.

- 2026-07-20 — **STRING-ARC-ENDGAME-RESUME-CHECKPOINT delivered
  (report-only, per direction; full suite running) — STOPPED.**
  work/string-ownership-refactor/STRING-ARC-ENDGAME-RESUME-
  CHECKPOINT.md (copy: /tmp/drift-announce/2026-07-20T060043Z-...).
  Explicitly NOT built on NEXT-PHASE-PLAN.md (completed Scope-A
  plan; historical only). Contents: (1) the three small follow-ups
  DISPOSED individually — 2a projected-place scrutinee audit (own
  report-only checkpoint, checker surface, not endgame-coupled),
  2b ConstShare synthesis visibility (independent repro-first
  checker slice, maintainer priority), 2c match-consume spec prose
  (doc-only, no version stamps per convention, RECOMMENDED FIRST);
  (2) string_arc responsibility+consumer AUDIT R1-R10, quantified
  from build/tmp/flagret (R1 entry zero-init cornerstone; R2
  overwrite releases 233,519; R3 scope-exit strings 68,562 + elision
  fold; R4 return retain-wrap/alias walk — the 0.27.145 re-proof
  surface; R5 MoveOut expansion 1,852,137; R6 site-3/4 destructibles
  — recommended FIRST fold into cleanup_authoring; R7 array
  overwrite drop; R8 recognition 618,744; R9 dead-but-greppable
  surfaces die with the file; R10 shared-analysis library extraction
  — mechanical, corpus +0, can go first) + consumer graph (driftc,
  string_releases, 27 test files; reporter has no import);
  pre_post_verdict_drift 48,178 + c1_path_dependent 20,384 carried
  as B-repr inputs; (3) B5 entry criteria SCOREBOARD — criterion 2
  (C3 hard gate) DISCHARGED 2026-07-13, criterion 3 (Array elision)
  DISCHARGED AND SUPERSEDED (landed 0.33.84 → sweep retired 0.33.85
  → flags 0.33.86), criterion 1 in progress via the audit, criterion
  4 standing green; (4) compiler/ABI/cert assumptions refreshed
  (certified 0.33.84; 0.33.86 candidate in full suite; ABI 21
  through the endgame; first ABI movement is B-repr 21→22 + recert;
  flocker 0.2.1 infra fix in-tree); (5) proposed resume order —
  every step its own checkpoint→review→implement cycle, NOTHING
  authorized by the document. Ends at STOP.
