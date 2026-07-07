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
