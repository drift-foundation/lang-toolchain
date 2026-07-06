# Release-relevant candidate review and plan

**In reply to:** `2026-07-06T154400Z-release-relevant-candidate-review-request.md`
**Mode:** research/review only — no repo edits, no staging, full suite left undisturbed.
**Baseline facts (verified against the tree and certified pool, not from memory):**
certified pool = **0.33.69 / ABI 19 / dee458cc**. Main is three commits ahead, all uncertified:
`fc45b02b` (0.33.70, projected-capture lowering), `cfd53731` (0.33.71, throwing-Fn field-fat interim),
`30f1b48b` (0.33.72, **uniform-fat throwing Fn, ABI 19→20**, includes the post-review fixes: nothrow-return
fat arm + coercion, module-level mapper mirror, size-approx fix). Working tree is clean.

**Planning premise (per maintainer, 2026-07-06):** ABI preservation is irrelevant for this window — the
uniform-fat work already invalidates ABI. Consequences used throughout this plan: (a) the next release
pays full recert regardless, so "ABI-neutral" is no longer a sequencing advantage among candidates;
(b) none of the remaining candidates *needs* an ABI change, so nothing must be rushed into this window to
piggyback the bump; (c) if the team has any other boundary-layout change queued and desired, this window
is the one free ride — none of the four listed candidates is one, and I found no other queued layout
change in `work/` or `doc/refactor_triggers.md` references that wants it.

---

## 1. Which candidates are release-relevant

| # | Candidate | Release-relevant? | Why |
|---|---|---|---|
| 1 | Uniform-fat throwing `Fn` (0.33.72/ABI 20) | **YES — it IS the release** | Fixes a filed CORE_BUG family end-to-end; carries the ABI bump; bookkeeper is waiting on certified ≥0.33.72 to collapse to the clean `Array<OpBinding>` catalog |
| 2 | String Scope A (transfer-policy centralization) | **Next slice, not this release** | Fixes no shipping defect — the unsafe shape it enables (non-bitcopy Copy projected captures) is *safely rejected* today. It is the hardening/enabler that must precede candidate 3 |
| 3 | Projected Copy captures (non-bitcopy widening) | **Deferred behind #2** | The safe slice already shipped in 0.33.70 (`Copy && is_bitcopy`, transitive — includes all-bitcopy structs). What remains is exactly the String-bearing surface that produced a confirmed ASAN UAF when the gate was relaxed without Scope A |
| 4 | Ref-typed callback args (escape gap 4a) | **Remains research** | Mechanism is code-verified but NOT runtime-confirmed; the empirical repro is blocked by an unrelated SSA ICE. Not actionable as a fix yet — but the blocking ICE itself deserves promotion to a small triage item (see §5) |
| — | `stdlib-bootstrap-split` | Out of scope per request | Research-only, non-blocking (unchanged) |

**One item the candidate list missed, resolved on inspection:** the signalfd SIGTERM busy-spin
LANGUAGE_BUG filed by drift-mariadb-client (2026-07-03) was fixed in `95d5b19c` (2026-07-03), which
*predates* `dee458cc` — it is already inside certified 0.33.69. Not a candidate; no release action needed.

## 2. Recommended order after the full suite completes

**If green:**
1. **Release 0.33.72 / ABI 20** (candidate 1). Certify, then coordinate the recert wave — ABI 20 forces
   every certified bundle/app to rebuild. Release-notes content spans everything since 0.33.69: the
   0.33.70 projected-capture lowering + `--emit-package` projected-capture rejection, and the
   0.33.71+0.33.72 throwing-Fn family (uniform fat; the interim `E_THROWING_FN_FIELD_BORROW` rejection
   from 0.33.71 was introduced and removed entirely *within* this uncertified span, so no user ever sees
   it — worth one line in history.md, nothing in release notes).
   App-facing note for bookkeeper: single-struct catalog unblocked; also `&op.run`, `apply(op.run,…)`,
   `Some(op.run)`, `push(op.run)` all now work — their report's "collapse when fixed" plan can execute.
2. **Start Scope A** (candidate 2) on a fresh branch per `NEXT-PHASE-PLAN.md`
   (`refactor/string-transfer-policy-scope-a`) — it is the widest-leverage next compiler slice and the
   prerequisite for candidate 3.
3. **Candidate 3 rides on Scope A's completion** — it is Scope A's step 4, not an independent line item.
4. **In parallel, small and independent: reduce/root-cause the SSA `load before store for local '__b8'`
   ICE** (see §5). It is a valid-source ICE (policy: ICE on valid source → fix compiler) and the sole
   unblocker for candidate 4's empirical confirmation.

**If the suite fails:** triage before any release motion. All three unreleased commits are candidates, but
the overwhelming prior is 30f1b48b (largest surface, representation change). Focused plan:
- Bucket failures by domain first: fn-ptr/callback/closure tests, `Array<Fn>` layout/stride shapes,
  debug-info tests, package tests (`lang/tests/packages` — 472-test suite was green on 0.33.70), memcheck.
- Anything touching `Fn` values/params/returns/variant-payloads → 30f1b48b; run the 15-case
  `test_throwing_fn_struct_field_storage.py` + the 63-test targeted battery first (they were green
  pre-suite; a discrepancy means environment/build-cache, not code).
- Projected-capture/lambda failures → fc45b02b (its own full-suite baseline was 3385 passed / 3
  pre-existing unrelated failures — compare against that list before attributing).
- Note: 3 known pre-existing failures existed at the 0.33.70 gate (entrypoint-`pub` policy, `/tmp`-root
  hygiene debt). Expect them; do not attribute them to this span.

## 3. Per-candidate detail

### Candidate 1 — uniform-fat throwing `Fn` (0.33.72, ABI 20)

- **Defect/product reason:** bookkeeper CORE_BUG family (invalid IR on Fn-in-struct-field, heap
  corruption from short array stride, SIGSEGV through field refs); removes the fat/thin representation
  seam entirely. Product: unblocks the app team's catalog design and removes a whole class of
  "rejected/ICE at the seam" surprises.
- **Regression-first entry:** already done — `lang/tests/driver/test_throwing_fn_struct_field_storage.py`
  (15 cases: 4 layout, 9 seam shapes, 2 nothrow-return) + module-mapper unit pins.
- **Files touched:** `lang/codegen/llvm/llvm_codegen.py`, `lang/driftc/type_checker.py` (rejection
  removal), tests, `lang/versions.py`. Committed as 30f1b48b.
- **ABI/version:** DRIFTC 0.33.72, ABI 19→20 (array-of-Fn stride 8→16, Fn param/return convention). This
  is THE bump of the window.
- **Targeted verification before full gate:** done — 63 passed (regression file + module-type-mapping +
  function-reference wrappers + implicit callback wrap + cross-module callback + stage1 function types),
  all 12 seam probes correct end-to-end.
- **Blockers:** none. Full serial suite (running) is the last gate. Two parked non-blockers for a later
  minor: checker's asymmetric nothrow→throwing `Fn` conversion policy (accepted at return position,
  rejected at call args/match arms — should be made uniform one way); extern-C `Fn`-param isize fallback
  should become an explicit rejection.

### Candidate 2 — String Scope A (structural classification + transfer centralization)

- **Defect/product reason:** no shipping defect. Two structural problems with a shared root: (a) the
  isolated-vs-stdlib-loaded `copy_status(String)` two-authority split flips `is_cheap_copy` /
  `_should_copy_value` between modes; (b) the recurring bug class where parallel lowering paths each
  independently forget `_ref_field_temps.add()` / `_copy_if_ref_alias()` (three fired historical
  instances + the fourth known path: whole-root HVar REF/REF_MUT capture reads bypassing
  `_load_capture_from_env`). The audit's key correction stands: classification alone does NOT close the
  bug class — the helper centralization is the part that does; classification is its prerequisite.
- **Minimum regression-first entry (per NEXT-PHASE-PLAN):** start from the 0.33.70 lock-in
  `test_copy_typed_non_bitcopy_struct_field_still_rejected`; build the scratch probe that temporarily
  relaxes the gate to reproduce the `Tag(label: String)` ASAN UAF and trace the retain loss; only convert
  rejection tests to positives after the fix is demonstrated. Do not skip the probe step — it is the only
  failing-first evidence this refactor can have.
- **Expected files:** `types_core.py` (structural retain-copy+needs-drop lanes), `driftc.py`
  (`_install_copy_query` simplification), `type_checker.py` (`_COMPILER_KNOWN_COPY_SCALARS` retirement),
  `hir_to_mir.py` + `drop_policy_compute.py` (`_drop_policy`, `_should_copy_value`, `_copy_if_ref_alias`,
  capture env construction), possibly `string_arc.py` read-side only. **Explicitly keep out:** merging
  `string_arc.py` into the ledger pipeline (the authority-timing problem is the hardest variant and is
  not required for Scope A's deliverable; also respects the standing ownership-lattice change bar), any
  Scope B representation work, ref-typed callback args, `--emit-package` projected-capture re-enable.
- **ABI/version:** ABI-neutral by design (and per the planning premise, that's now merely a fact, not a
  constraint). DRIFTC minor bump when the projected-capture gate widens (behavior change); the
  classification/centralization step alone warrants a bump only if any observable behavior shifts.
- **Targeted verification before full gate:** the plan's matrix — bitcopy scalar + bitcopy-struct
  projected captures still pass; `Tag(label: String)` and plain-String projected captures under ASAN (if
  widening); non-Copy MOVE projected and `--emit-package` projected stay rejected; whole-root HVar
  REF/REF_MUT alias regression if that path is touched. Plus: string ownership/leak memcheck suite from
  the start (standing rule: ownership/cleanup-path patches carry memcheck in the gate), projected-capture
  driver tests, `lang/tests/packages`.
- **Blockers/clarifications:** none hard. One scoping decision to confirm at kickoff: the lane taxonomy
  (bitcopy / retain-copy / structural-copy / move-only) and that `string_arc.py` remains its own
  authority reading the central classification (option (b) in the audit), not a ledger merge.

### Candidate 3 — projected Copy captures, non-bitcopy widening

- **Defect/product reason:** ergonomics/completeness, not a defect: `p.label` (String field) captured in
  a boxed callback is currently rejected with a clear diagnostic and a documented workaround
  (`std.mem.replace` + `captures(move local)`). 0.33.70 already shipped the entire safe surface
  (`Copy && is_bitcopy`, transitive).
- **Minimum entry:** this IS Scope A step 4 — there is no narrower correct slice left (see §4).
- **Files:** `borrow_checker_pass.py::_is_copy_projected_field` gate relaxation + the Scope A helpers it
  depends on; test flips in `test_boxed_callback_projected_move_capture_rejected.py`.
- **ABI/version:** ABI-neutral; DRIFTC minor bump (behavior change).
- **Verification:** ASAN + Valgrind rows are mandatory for every newly-accepted shape (this is exactly
  the class where compile-success proves nothing); keep `--emit-package` rejection tests green.
- **Blockers:** Scope A completion, demonstrated by the probe-UAF turning green.

### Candidate 4 — ref-typed callback args (escape gap 4a)

- **Defect reason:** code-verified silent-unsoundness *mechanism*: an escaping nested boxed closure that
  implicitly captures the outer callback's `&T` param copies the raw pointer into a heap env (MOVE-kind
  capture of a reference value → invisible to REF/REF_MUT escape machinery → STATIC escape level →
  dangling pointer). Also a second reachable path: explicit `captures(copy ref_param)` passes the
  existing COPY check because `&T` is Copy.
- **Actionable now? NO — keep as research.** The research doc's own gate is right: end-to-end runtime
  behavior is unconfirmed because the repro hits `RuntimeError: SSA: load before store for local '__b8'`
  (stage4/ssa.py:163) before demonstrating anything. Implementing the fix (bounding
  `_lambda_escape_level` to LOCAL when a MOVE/COPY capture root is reference-typed — the twice-corrected
  fix location) without a confirmed repro risks shipping an over- or under-rejection with no failing test
  to anchor it.
- **What IS actionable now (recommend scheduling):** the SSA ICE itself, as an independent small triage.
  It is an ICE on plausibly-valid source (nested boxed callback + `conc.spawn` returning
  `VirtualThread<String>`), which standing policy treats as fix-the-compiler; and it doubles as the 4(a)
  unblocker. Entry point: reduce the §7 repro from the research doc; first fork in the road is whether it
  shares a root with the driftc.py per-slot preseed gaps that 0.33.70 fixed (the doc flags this
  explicitly) — check that before assuming a fresh SSA bug.
- **Files (when eventually implemented):** `borrow_checker_pass.py` (`_lambda_escape_level` bound +
  `_check_lambda_captures` COPY case), driver regressions incl. the web/rest synchronous-dispatch
  control tests that must stay green (`test_product_shape_consumer_patterns.py`,
  `test_implicit_callback_wrap.py`).
- **ABI/version:** ABI-neutral; DRIFTC minor bump (new rejection).

## 4. Must Scope A precede projected Copy captures?

**Yes, and there is no narrower safe slice remaining.** The narrower slice already shipped: 0.33.70's
gate is `Copy && is_bitcopy` (transitive — all-bitcopy structs included, variants excluded), which is
precisely the subset with no refcounted content and therefore no ownership hazard. Everything still
rejected is Copy-but-non-bitcopy, i.e. String-bearing — and relaxing that gate without the Scope A alias
centralization produced a *confirmed* ASAN heap-use-after-free (`Tag(label: String)`) during 0.33.70
review. The 0.33.70 mutation-testing result reinforces this: the two aliasing-mark fixes landed there are
not currently load-bearing for String only because `string_arc.py`'s late pass happens to paper over
them; the widening would remove that accident of coverage. Additionally, 0.33.70's fourth review round
already banked the "Copy struct of bitcopy fields" case, so there is genuinely nothing left between the
current gate and the String-bearing surface.

## 5. Ref-typed callback args — actionable now?

No (details in §3/candidate 4). Keep research status. Schedule the SSA `load before store` ICE
root-cause as its own small item — it is independently justified by the ICE-on-valid-source policy and is
the only path to a clean 4(a) repro. Do not fold the eventual escape-level fix into Scope A ("keep the
three tracks separate even though they share root cause" — the audit's own directive, and the tracks
have different failure modes and test surfaces).

## 6. Final recommendation

Release train: **0.33.72/ABI 20 is the release, pending the running full serial suite.** If green:
certify, run the recert wave (all certified bundles rebuild — same coordination shape as prior ABI
bumps), notify bookkeeper that the catalog collapse is unblocked. If red: triage per §2's bucketing
before any other work.

Engineering queue after that, in order: **Scope A** (regression-first, probe-UAF before any gate change,
memcheck in gate from day one, `string_arc` ledger-merge explicitly out) → **projected-capture widening**
as Scope A's final step (ASAN/Valgrind rows mandatory) → meanwhile, as a small independent item, **the
SSA load-before-store ICE**, which both honors the ICE policy and unblocks the ref-typed callback-arg
question. Candidate 4's fix itself stays parked until that repro exists. Nothing else in `work/` is
release-relevant; the signalfd item some may remember as open is already fixed inside certified 0.33.69.
