# 0.33.73 — nested boxed-callback captures (LANGUAGE_BUG family)

**Follow-up to:** triage `2026-07-06T161920Z-ref-typed-callback-args-03373-triage.md` (green-lit by the
release thread). **Regression-first; ABI stays 20; unsafe ref-capture shapes were uncompilable at every
intermediate step** (rejection landed before the ICE fix unshielded them).
**Status:** implemented and verified with the targeted battery (66 passed / 0 failed) plus
ASAN/Valgrind rows; patch in the working tree, no staging/commits. The 0.33.72 full-suite clone was not
touched.

## What the slice fixes

A boxed callback built inside another boxed callback's body could not safely capture anything from its
enclosing lambda. Three coupled defects, all verified pre-existing in certified 0.33.69:

1. **SSA ICE for any nested capture of the enclosing lambda's parameter**
   (`SSA: load before store for local '__b{id}'`). Root cause turned out simpler and better-localized
   than the triage's worklist-seeding hypothesis: both hidden-lambda worklists constructed their
   `HIRToMIR` instance **without the typed fn's `binding_names`** (the regular-fn path passes it —
   driftc.py:6311), so the instance's name map was empty and nested env construction resolved capture
   roots through the `__b{binding_id}` fallback — a load of a local nothing ever stores. Two-line fix
   class: pass `binding_names=` in both worklist constructions.

2. **Ref-valued MOVE/COPY captures (the triage's 4(a) hazard), previously shielded by the ICE.**
   Rejected at the wrap site with **`E_ESCAPE_REF_CAPTURE`** (`stage1/lambda_validate.py`), mirroring
   the existing v0 rule for explicit `captures(ref …)`. Delivery had to move from the plan's
   borrow-checker-only location: the borrow checker never descends into lambda bodies, and the user-fn
   validation walk runs before `capture_as_move` is set on *nested* wraps — so the rule lives in
   `validate_lambdas_non_retaining` (types available, walks nested bodies) and is **re-run from the
   hidden-lambda worklist** for bodies that build nested lambdas. The planned
   `_lambda_escape_level` LOCAL-bound is ALSO in (borrow_checker_pass.py) so loan-tracked positions
   reject through the existing `E_ESCAPE_*` machinery; `_report_escape_violation` gained a note naming
   the ref-valued captured binding.

3. **Implicit BORROW captures in boxed callbacks — a new confirmed silent use-after-scope, found during
   the required ASAN/Valgrind verification.** `flag_note.clone()` on a captured outer local classifies
   the capture REF (borrow beats the boxed MOVE default), so the env stored a raw pointer to the
   enclosing lambda's **stack slot**. The compiled program returned wrong values nondeterministically
   (dead-frame reads; Valgrind: invalid reads in the callback thunk; my earlier triage attributed the
   V2 crash to this shape's sibling — see "corrections" below). The v0 "borrowed captures are
   non-escaping" rule only covered explicit `captures(ref …)`; the implicit path is now rejected with
   **`E_CALLBACK_BORROWED_CAPTURE`** and an actionable `captures(move …)`/`captures(copy …)`
   suggestion.

## Corrections to the triage record (important for reviewers)

- The triage's "owned-local capture double-frees" (V2) was a **composite** of two other bugs, not an env
  ownership bug: (a) the implicit-borrow stack-pointer capture above, and (b) the **separate,
  pre-existing interface-field-copy bug**: `val cb = h.cb` (reading an interface-typed struct field by
  value) shallow-copies the boxed callback without retaining its env → double-free/UAF on drop. (b)
  reproduces **without any lambda nesting** and **on certified 0.33.69** (probe: plain fn returning
  `Holder`, explicit `captures(move …)`, `val cb = h.cb` → segfault on both HEAD and certified).
  **(b) is NOT fixed in this slice** — it needs its own regression-first fix (likely `_copy_if_ref_alias`
  classification for INTERFACE-typed field reads, or a checker rejection if interface values are not
  Copy). Until then: use direct calls (`h.cb.call()`) or move the holder.
- My initial ASAN row on the implicit-borrow shape passed by luck (dead stack still intact); the pytest
  environment's different stack layout exposed it (wrong value, exit 1), and Valgrind confirmed invalid
  reads. Lesson recorded: for this bug family, run both sanitizers AND repeat runs.

## Required behaviors — status

| Requirement | Status |
|---|---|
| Non-ref outer param captured by nested boxed callback compiles/runs | ✅ (`Bool` param; exit 0; ASAN clean) |
| Ref outer param capture rejects via escape diagnostic path | ✅ `E_ESCAPE_REF_CAPTURE` — pinned for: nested implicit `&String`, nested explicit `captures(copy …)`, nested `&mut Int`, top-level implicit `&String`, top-level explicit `captures(copy …)`, top-level `Optional<&String>` |
| Valid synchronous web/rest patterns stay accepted | ✅ `test_product_shape_consumer_patterns.py` + `test_implicit_callback_wrap.py` green; in-file control with ref params + `captures(copy …)` |
| Nested owned String local capture compiles/runs clean under ASAN/Valgrind | ✅ via explicit `captures(move …)` (5× exit 0; ASAN clean; Valgrind 0 errors). The IMPLICIT borrow form is **rejected** — it was never sound (stack-pointer env) |
| Projected-capture tests stay green | ✅ 6/6 |

## Files changed

- `lang/driftc/driftc.py` — `binding_names=` passed into both hidden-lambda worklist `HIRToMIR`
  constructions (the ICE fix); nested-lambda re-validation hook (gated to bodies that build nested
  lambdas) with fatal diagnostics through the existing `type_diags` channel.
- `lang/driftc/stage1/lambda_validate.py` — wrap-site rules for boxed (`capture_as_move`) lambdas:
  ref-valued MOVE/COPY captures → `E_ESCAPE_REF_CAPTURE`; REF/REF_MUT captures →
  `E_CALLBACK_BORROWED_CAPTURE`. Both need `binding_types`+`type_table` (post-typecheck callers) and
  no-op otherwise, so the pre-typecheck package-emit caller is unaffected.
- `lang/driftc/borrow_checker_pass.py` — `_has_ref_valued_move_or_copy_capture()` +
  `_lambda_escape_level` LOCAL bound + diagnostic note.
- `lang/tests/driver/test_nested_boxed_callback_captures.py` — new, **11 cases** (correction: an earlier
  revision of this report claimed 9 while the file had 8 — review finding. Three regressions were added
  to close the coverage gaps the review identified: top-level `captures(copy s)` of a `&String` param,
  nested capture of a `&mut Int` outer-lambda param, and capture of an `Optional<&String>` param — all
  rejecting with `E_ESCAPE_REF_CAPTURE`, matching the shapes `_is_ref_valued_type` /
  `_is_ref_binding_id` explicitly handle). NOTE FOR REVIEWERS: this file is currently UNTRACKED, so
  `git diff --stat` omits it — the eventual commit needs `git add -A` (or explicit add) to include it.
- `lang/versions.py` — 0.33.72 → **0.33.73**; ABI stays 20. `doc/history.md` — 0.33.73 entry.
  (Noting for the maintainer: history.md has no 0.33.71/0.33.72 entries yet — release-notes gap from the
  uniform-fat branch.)

## Verification (serial; the 0.33.72 suite clone untouched)

- New regression file: **11/11** (8 within the 66-test battery below + 3 review-requested additions,
  re-run standalone: 11 passed, 125s).
- Targeted battery: `test_nested_boxed_callback_captures.py` +
  `test_boxed_callback_projected_move_capture_rejected.py` (6) + `test_implicit_callback_wrap.py` +
  `test_product_shape_consumer_patterns.py` + `test_cross_module_callback_named_fn.py` +
  `test_lambda_capture_discovery.py` — **66 passed, 0 failed** (16m45s).
- Escape-level model + non-retaining params suites — **29 passed**.
- Manual rows: nested param capture and explicit-move owned-local capture — ASAN clean, Valgrind clean
  (0 errors), 5× repeat runs stable.
- Full serial suite: NOT run by me (the box's clone is running the 0.33.72 gate). The slice should get
  its own full gate after 0.33.72's completes; the touched surfaces' suites are all green above.

## Reasons this might not be 0.33.73 (disclosure)

1. **New rejections can break previously-compiling (unsound) user code**: any boxed callback that
   implicitly borrow-captures an outer binding (e.g. calls `.clone()` on a captured local without a
   `captures(…)` clause) now needs `captures(move|copy …)`. Every such program read through a possibly
   dead pointer — the rejection is correct — but it IS a source-compat change; release notes must call
   it out with the migration hint (the diagnostic includes it).
2. The interface-field-copy bug (`val cb = h.cb`) remains open and is arguably worse (silent, certified,
   no nesting needed). If the team wants one "callback safety" release, it could be argued 0.33.73
   should wait for that fix too; my recommendation is ship this slice now (it's coherent and verified)
   and make the iface-field-copy fix the next slice — it has an independent mechanism and test surface.
3. The full serial gate for this slice is still owed once the box frees up.

**Probe artifacts:** session scratchpad `refcb_*.drift` (12 variants), Valgrind/ASAN logs alongside.
