# String Scope A — progress log

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
