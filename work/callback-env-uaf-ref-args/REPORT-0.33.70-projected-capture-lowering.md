# Projected lambda-capture lowering — 0.33.70 (branch `fix/projected-capture-lowering`)

Status: implementation complete and reviewed (three review rounds). Final
full unfiltered `lang/tests` run: 3385 passed, 5 skipped, 3 pre-existing
failures unrelated to this branch (see §14). See §9, §10/§11, and §13 for
each round's findings and what changed; §14 for the final verification.

## 1. Scope

Follow-up to the 0.33.69 hotfix (`fix/callback-env-uaf-ref-args`, now certified).
That fix closed a real UAF (a struct field moved into a boxed callback with no
`captures(...)` clause) but did so with an intentionally blanket rule: **every**
MOVE-kind capture of a projected place (`p.field`, not a whole local) was
rejected, including the safe case where the field's type is Copy (e.g.
`p.count: Int`) — because the lowering itself couldn't actually handle a
projected capture correctly, independent of capture kind.

This branch was scoped to:
1. Pin the pre-existing REF-kind projected-capture ICE (unrelated to the
   0.33.69 UAF — a plain immediate-invoked lambda reading `p.count` with no
   `captures(...)` at all crashed LLVM codegen).
2. Fix the metadata/prologue lowering so projected REF/COPY captures lower
   correctly.
3. Close two aliasing-mark gaps at the same two sites every other
   ownership-transfer boundary already covers, as consistency/hardening
   coverage (see §9 — mutation testing found these are not currently
   provably load-bearing for `String` specifically).
4. Re-enable Copy-typed projected captures in the boxed-callback path, now
   that the lowering is provably correct.
5. Keep non-Copy MOVE projected captures rejected — no change there.

Explicitly NOT bundled (per instructions): ref-typed callback argument escape
diagnostics, the String ownership/classification refactor, runtime/String
representation work. Those are tracked separately in `doc/refactor_triggers.md`.

## 2. Bug 1 — REF-projected capture crashed LLVM codegen (metadata/prologue)

Minimal repro (no boxed callback involved at all):

```drift
struct Prepared { count: Int }
fn use_it(p: Prepared) -> Int {
    return (| | => { return p.count + 1; })();   // p.count, no captures(...) clause
}
```

```
NotImplementedError: LLVM codegen v1: integer binop requires matching
Int/Uint operands (have %Struct_main_Prepared_..., drift.int)
```

**Root cause.** `driftc.py`'s hidden-lambda worklist re-derives a captured
slot's type from the CAPTURE ROOT's whole-struct type at several sites, all
keyed only by root binding id — every one of them ignored `key.proj`:
- the `env_field_types[slot]` overwrite when the captured expr's own type was
  a typevar/Unknown,
- the root-named `_local_types` preseed,
- the candidate-slot-type preseed fallback (used when the root's own type was
  unresolved),
- the name-to-slot fallback table (this one also **collides** when a root has
  more than one projected capture — two different projections both keyed by
  the root's name).

Then `hir_to_mir.py::_emit_lambda_capture_prologue` materialized a
body-visible local **named after the root** for every captured slot,
including projected ones, and stored the (now wrongly-typed) slot value into
it. For `p.count`, that produced a local literally named `p`, typed as the
whole `Prepared` struct, holding just the `Int` — the body's own `p.count`
expression then tried to project `.count` off a value LLVM had already typed
as a bare `Int`. Crash.

**Fix.** Guard all five `driftc.py` metadata sites with `if cap.key.proj:
continue` (skip root-type re-derivation for a projected slot — its env slot
is already correctly typed by outer lowering), and add the same guard to the
prologue's per-slot loop in `hir_to_mir.py` (a projected capture gets **no**
body-visible local at all; the body's own `p.count` expression resolves
directly against the env slot elsewhere, keyed by the exact capture key, not
by root name).

Verified: two additional cases confirm the fix generalizes — two projections
of the same root in one lambda body (`p.count` + `p.flag`, proves no name
collision), and a bare-root REF capture (`&p`) alongside a projection of the
same root in one body (proves the root still gets a real local when it's
independently captured, while the projection doesn't).

## 3. Bug 2 — two aliasing-mark gaps found while fixing bug 1 (see §9 for a
   correction to how load-bearing these are proven to be)

Once projected captures could reach codegen at all, two pre-existing gaps
(verified directly in the code, not just inferred) looked like they would
reopen a UAF for non-bitcopy fields specifically (e.g. `&String`):

- **`_load_capture_from_env`'s REF/REF_MUT branch** did a `LoadRef` into a
  fresh temp — structurally identical to the general deref path
  (`_visit_expr_HUnary` DEREF) and the array-index field-projection fast
  path — but never marked the result in `_ref_field_temps`, unlike both of
  those. Without the mark, `_copy_if_ref_alias` (which every OTHER
  ownership-transfer boundary calls) would never upgrade a borrowed
  `&String` read into an owned copy before it escapes.
- **The COPY-kind capture env-construction branch**, in both
  `_lower_lambda_immediate_call` and `_lower_lambda_callback`, stored the
  captured value straight into the closure's env struct without calling
  `_copy_if_ref_alias` first — unlike every other ownership-transfer
  boundary (struct/variant construction, return, assignment, call args).

Both fixed by applying the existing `_copy_if_ref_alias` helper at both
sites, matching the pattern already used everywhere else in the file.
**Update from review (§9): mutation testing found a later, independent pass
(`string_arc.py`) already covers the `String` case regardless of these two
fixes** — see §9 for the full finding. The fixes are kept (correct and
consistent with the rest of the file's contract) but are no longer claimed
as the proven fix for an observable UAF in this report; that claim didn't
survive verification.

**Proof case (not just a compile-success check):** a REF-projected capture
of a `String` field, returned from an immediate-invoked lambda, with the
source struct also dropped after — compiled and run under
`--sanitize=address,undefined`, and (per §9) also checked under Valgrind.
It runs clean, both with and without these two specific fixes present —
see §9 for why that is, and what it does and doesn't tell us.

## 4. Enabling Copy-AND-BITCOPY-typed projected captures in the boxed-callback
   path (scope narrowed in review — see §10)

With the lowering now provably correct, the 0.33.69 blanket rejection can be
narrowed: `capture_discovery.py::discover_captures` gained an optional
`is_copy_projected_field` resolver. When a MOVE-kind capture of a projected
place would otherwise be rejected, a typed caller can supply this resolver;
if it says the field is Copy **and bitcopy**, the capture downgrades to a
plain COPY read instead of erroring. Every other field — non-Copy, or
Copy-but-non-bitcopy (`String`, or a Copy struct/variant containing one) —
is unconditionally still rejected. **The bitcopy requirement was added
during review (§10), after a Copy struct containing a `String` was found to
produce a confirmed heap-use-after-free when downgraded; the original
landing only required Copy.** Callers with no type context (this stage runs
before type-checking) pass nothing and keep the old behavior.

This needed wiring at **two** call sites — both are typed, and both actually
decide `HLambda.captures` for a real (non-`--emit-package`) compile:

- `borrow_checker_pass.py::_check_lambda_captures` — via the borrow
  checker's existing `Place`/`_type_of_place` model. Also fixed a latent
  correctness bug found while doing this: the existing COPY-kind capture
  validation checked the **root's** type (`binding_types.get(root_local)`)
  even for a projected capture, which is wrong (the root, `Prepared`, can be
  non-Copy while the projected field, `Int`, is) — now checks the field's
  type via `_type_of_place` when `cap.key.proj` is set.
- `driftc.py`'s post-typecheck `validate_lambdas_non_retaining` call — this
  turned out to be the actual authoritative gate for a normal compile (it
  runs before borrow-checking and its diagnostics abort compilation
  directly), so it needed the same capability. It has `binding_types` +
  `type_table` but not the borrow checker's `Place` machinery, so it uses a
  new small helper, `resolve_projected_capture_type`, that walks the
  capture's field-projection chain directly via
  `type_table.struct_field_info`.

`--emit-package` gets its own, broader, dedicated rejection — see §11 — it
does not merely "keep the old behavior" as originally landed; see that
section for why a plain unchanged pre-typecheck call was not sufficient.

## 5. Regressions added

- **`lang/tests/driver/test_projected_lambda_capture_lowering.py`** (new,
  4 tests) — covers the lowering mechanism directly via immediate-invoked
  lambdas, independent of boxed-callback machinery:
  1. the exact REF-projected ICE repro now compiles and runs,
  2. two projections of the same root don't collide,
  3. a bare-root capture plus a projection of the same root both resolve,
  4. the non-bitcopy alias/CopyValue proof case runs clean under ASAN.
- **`test_boxed_callback_projected_move_capture_rejected.py`** — the former
  lock-in test (`test_copy_typed_projected_field_also_currently_rejected`,
  asserting Copy-typed fields were ALSO rejected) is now a positive test
  (`test_copy_typed_projected_field_now_compiles_and_runs`, an `Int` field —
  bitcopy), consistent with §4. The non-Copy rejection test and the
  `mem.replace`-control test are unchanged and still pass. **Two more tests
  added in review, both LOCK-IN REJECTIONS (§10 — not positive tests):**
  `test_copy_typed_non_bitcopy_string_field_still_rejected` (`String`) and
  `test_copy_typed_non_bitcopy_struct_field_still_rejected` (a Copy struct
  containing a `String` — the shape that produced the confirmed UAF in §10).
- **`lang/tests/packages/test_package_emit_projected_capture_rejected.py`**
  (new file, added in review, §11) — 3 tests: an implicit Copy-typed
  projected capture is rejected under `--emit-package`, a pre-existing
  REF-kind projected capture is rejected too (same boundary problem, not
  specific to the new capability), and a whole-local (non-projected)
  implicit capture still compiles and packages successfully (control).
- Targeted run: `test_projected_lambda_capture_lowering.py` +
  `test_boxed_callback_projected_move_capture_rejected.py` +
  `test_lambda_capture_discovery.py` + `test_lambda_validation.py` — 23/23
  passed (post-review, post-narrowing).
- `lang/tests/packages/` full run — 472/472 passed (469 pre-existing + 3
  new package-emit tests), no regressions from the new `--emit-package`
  checks.
- Broader adjacent-risk run (`-k "capture or lambda or callback or
  closure"` across `lang/tests/driver/` + `lang/tests/stage1/`) — 231/231
  passed (pre-narrowing baseline; re-run recommended before merge — see §12).
- Full unfiltered `lang/tests` run (pre-narrowing baseline): **3380 passed,
  5 skipped, 3 failed.** The 3 failures are pre-existing and unrelated to
  this branch — none touch a file this branch changed:
  - `test_variant_borrowed_match_construct_int_payload.py::test_borrowed_match_int_payload_reconstruct_same_variant_links`
    fails on the recently-landed "entrypoint must be pub" policy (its fixture
    source predates that requirement).
  - `test_tmp_root_compliance.py::test_no_hardcoded_writable_tmp_paths` and
    `::test_no_bare_tempfile_dir_required_calls` fail on pre-existing
    `/tmp`-root hygiene debt in unrelated files (`driftc.py` comments,
    `tools/drift_deploy/`, various test docstrings).
  A second full run after the §10/§11 changes is recommended before merge
  (see §12) — not yet re-run as of this writing.

## 6. Outstanding

- No git add/commit/staging performed — patch is in the working tree on
  `fix/projected-capture-lowering`, ready for review.

## 7. Versioning

`DRIFTC_VERSION` 0.33.69 → 0.33.70 (behavior-changing compiler fix — a
program that previously failed to compile now compiles). `DRIFT_RT_ABI_VERSION`
stays 19: no compiler/runtime boundary layout, signature, or
calling-convention change. `doc/history.md` entry added.

## 8. Files changed

- `lang/driftc/driftc.py` — five metadata-guard sites in the hidden-lambda
  worklist (`if cap.key.proj: continue`); `validate_lambdas_non_retaining`
  call now passes `binding_types`/`type_table`.
- `lang/driftc/stage2/hir_to_mir.py` — prologue skip for projected capture
  slots; `_ref_field_temps` mark on `_load_capture_from_env`'s REF/REF_MUT
  branch; `_copy_if_ref_alias` on the COPY-kind env-construction branch in
  both `_lower_lambda_immediate_call` and `_lower_lambda_callback`.
- `lang/driftc/stage1/capture_discovery.py` — `is_copy_projected_field`
  resolver parameter on `discover_captures`; new
  `resolve_projected_capture_type` helper.
- `lang/driftc/stage1/lambda_validate.py` — threads `binding_types`/
  `type_table` through to build the resolver.
- `lang/driftc/borrow_checker_pass.py` — `_check_lambda_captures` builds and
  passes the resolver; projected COPY-capture validation now checks the
  field's type instead of the root's; resolver narrowed to Copy-AND-bitcopy
  in review (§10).
- `lang/tests/driver/test_projected_lambda_capture_lowering.py` — new.
- `lang/tests/driver/test_boxed_callback_projected_move_capture_rejected.py`
  — lock-in test flipped to a positive test; docstring updated; two more
  lock-in REJECTION tests added in review for the non-bitcopy Copy cases
  (`String`, and a Copy struct containing `String` — the confirmed-UAF
  shape, §10).
- `lang/tests/packages/test_package_emit_projected_capture_rejected.py` —
  new file, added in review (§11).
- `lang/versions.py` — `DRIFTC_VERSION` 0.33.69 → 0.33.70.
- `doc/history.md`, `doc/refactor_triggers.md` — entries for this branch's
  work, the deferred/out-of-scope trigger notes, and the review-round
  corrections in §9–§11.

## 9. Review round — findings and what changed

A review pass on this branch raised four findings. Summary and disposition:

**High — the two Bug-2 aliasing fixes were added but nothing proved they
were load-bearing; the ASAN test only covered `Int`, not the non-bitcopy
(`String`) case the fixes actually target.** Valid gap. Added
`test_copy_typed_projected_string_field_boxed_callback_no_double_free`
(the boxed-callback COPY-branch String ASAN proof requested). But going
further and mutation-testing it (reverting each of the three
`_ref_field_temps`/`_copy_if_ref_alias` call sites one at a time and
re-running under both ASAN and Valgrind `--leak-check=full`, plus a
`DRIFT_STR_TRACE`-instrumented run and a direct `--emit-ir` diff) turned up
something more specific than "untested": **none of the three sites
currently change observable behavior when reverted, for `String`.**
IR diffing showed the exact same `drift_string_retain`/`drift_string_release`
call sequence with or without the fixes — a separate, later MIR pass,
`stage2/string_arc.py` (ledger-based ARC insertion; despite the filename,
its `_type_needs_drop` walk is generic over STRUCT/VARIANT/ARRAY, not
String-specific), independently inserts the same retain regardless. Valgrind
confirmed 18 allocs/18 frees, zero leaks, zero errors in both the
fix-present and fix-reverted builds.

Disposition: kept all three fixes (they're correct, and match the file's
own documented contract — "`_copy_if_ref_alias` must be called at every
ownership-transfer boundary" — the same as the general deref path and the
array-index fast path already do). Corrected the report and
`doc/history.md`/`doc/refactor_triggers.md` to stop claiming these fixed an
observable UAF, and to record instead that they're consistency fixes not
provably load-bearing for `String` today, with STRUCT/VARIANT-typed
projected captures (outside `string_arc.py`'s String-specific safety net,
though not its generic needs-drop walk) flagged as the untested case that
would actually distinguish this.

**Medium — `doc/history.md` claimed the full unfiltered suite had run
clean before the run had actually finished.** Valid — the run was still in
flight when that line was written. Fixed: the full run has since completed
(3380 passed / 5 skipped / 3 pre-existing unrelated failures, §5/§6) and
the doc now states the actual result instead of a status projection.

**Medium — `doc/refactor_triggers.md`'s "found during research, not yet
fixed, deferred pending that feature landing" note was stale**, since this
branch IS that feature landing. Fixed: reworded to "resolved in 0.33.70,"
with the mutation-testing caveat folded in so the trigger doc doesn't
overclaim either.

**Low / follow-up, not fixed — a fourth parallel lowering path.** The
`HVar` visitor has its own inline REF/REF_MUT capture-read branch (for a
bare-root capture, not a projection) that duplicates
`_load_capture_from_env`'s `LoadRef` logic without the `_ref_field_temps`
mark. This is whole-root capture behavior, orthogonal to the projected-field
(`p.field`) work this branch targets, and per the review's own
recommendation was not folded in here. Recorded as a trigger note in
`doc/refactor_triggers.md` for whoever next audits the alias-helper's
end-to-end completeness.

## 10. Second review round — a confirmed UAF, and narrowing the feature

A second review pass raised three findings against the state left by §9.

**High — package emission (`--emit-package`) was not safe for the new
Copy-projected-capture capability.** Confirmed. `--emit-package` serializes
`_pre_typecheck_hirs`, a deep-copy of the HIR taken BEFORE type-checking, so
the only capture-discovery pass that had run by that point was the
pre-existing, necessarily-untyped early call at the top of the package-emit
branch — it cannot supply the typed resolver, AND (traced empirically,
using a debug probe and a direct repro under `-M`/`--emit-package`) at that
point `capture_as_move` isn't set yet either (`call_resolver.py` sets it
during type-checking), so for a would-be boxed callback this pass doesn't
even see a MOVE-kind capture to reject — it defaults to REF and moves on
silently. A first fix attempt (making that early pass's own discarded
diagnostics fatal) turned out to be structurally unable to catch the
boxed-callback shape for exactly this reason — verified against a live
repro, not just reasoned about. The actual fix: a new, dedicated,
**post-typecheck** check (right after the existing typed
`validate_lambdas_non_retaining` call) walks every typed function's HIR for
any `HLambda` capture with `cap.key.proj` non-empty and rejects `--emit-package`
outright if found. This is deliberately broader than just the new
Copy-downgrade case — a pre-existing REF-kind projected capture has the
exact same "the pre-typecheck snapshot doesn't carry the same decision"
problem, untested/unverified across this boundary before now, so it's
rejected too. Verified against three cases: the Copy-projected boxed
callback (rejects, no `.dmp` written), a REF-projected immediate lambda
(rejects, no `.dmp` written), and a whole-local implicit capture (control —
still compiles and packages, exit 0). Full `lang/tests/packages` suite:
472/472 passed (469 pre-existing + 3 new), no regressions from either the
early-pass fix or the new post-typecheck check.

**High (found while adding the test the second-round review requested,
not one of its own three items — a real bug, not a process finding) — a
Copy struct containing a `String`, captured implicitly via a projected
field in a boxed callback, is a CONFIRMED heap-use-after-free.** The
review's third finding asked for exactly this test case
("a boxed-callback projected field whose type is a Copy struct containing
String, or narrow the feature if that shape is not intended"). Built it:

```drift
struct Tag(label: String);
implement core.Copy for Tag {}
struct Prepared { tag: Tag }
// ... p.tag captured implicitly inside core.callback0(...), returned, source struct also dropped
```

Compiled clean under `--sanitize=address,undefined` — and then crashed at
runtime:

```
==ERROR: AddressSanitizer: heap-use-after-free ... READ of size 11
    #2 drift_string_eq string_runtime.c:309
    #3 drift_main ...
freed by thread T2 here: ... free ...
```

This is a real, unfixed defect in the boxed-callback COPY-kind
env-construction path for non-bitcopy Copy types beyond plain `String` —
unlike the plain-`String` case (§9), this one is NOT saved by
`string_arc.py`'s incidental coverage. Root-causing exactly which step
loses the retain (deep-copy of a struct's nested non-bitcopy field via
`CopyValue`, vs. how the value is later read back out through
`_load_capture_from_env` and returned) is real, additional lowering work,
not attempted here given the time already spent on this branch.

**Disposition: narrowed the feature instead of shipping the bug.** Per the
review's own offered alternative, the Copy-projected-capture downgrade
(`is_copy_projected_field`, in both `borrow_checker_pass.py` and
`lambda_validate.py`) now requires the field to be **Copy AND bitcopy**
(`type_table.is_bitcopy(ty)`), not just Copy. Bitcopy types (Int, Uint,
Bool, Float, Byte, etc.) have no refcount to double-own, so they sidestep
the entire retain/alias question this bug lives in. `String` and any Copy
struct/variant remain rejected with the same "not supported yet"
diagnostic as before this branch. This deliberately does NOT special-case
"String is fine, only structs aren't" — per standing project policy
(no String-special-casing in generic lowering), the narrowing is by
bitcopy-ness, a structural property, not by type identity. Two new lock-in
tests confirm both non-bitcopy shapes stay rejected:
`test_copy_typed_non_bitcopy_string_field_still_rejected` and
`test_copy_typed_non_bitcopy_struct_field_still_rejected` (the latter is
the exact confirmed-UAF repro above, now locked in as a compile-time
rejection instead of a runtime crash).

Net effect on this branch's actual capability: Copy-downgrade of a
projected capture in a boxed callback now only fires for scalar bitcopy
fields (`p.count: Int`, `p.flag: Bool`, etc.) — narrower than what §4
originally described and shipped, but verified safe rather than
verified-broken.

## 11. Files touched in this round (in addition to §8)

- `lang/driftc/driftc.py` — reworded the early pre-typecheck package-emit
  check's comment to accurately describe its (narrow) scope; added the new
  post-typecheck `--emit-package` projected-capture rejection.
- `lang/driftc/borrow_checker_pass.py`, `lang/driftc/stage1/lambda_validate.py`,
  `lang/driftc/stage1/capture_discovery.py` — narrowed the
  `is_copy_projected_field` contract/implementations to require bitcopy.
- `lang/tests/packages/test_package_emit_projected_capture_rejected.py` —
  new (3 tests).
- `lang/tests/driver/test_boxed_callback_projected_move_capture_rejected.py`
  — module docstring rewritten for the narrowed scope; the String test
  flipped from a positive ASAN test to a rejection lock-in; a new struct-
  containing-String rejection lock-in test added (the confirmed-UAF repro).

## 12. Outstanding after this round

- The full unfiltered `lang/tests` run in §5/§6 predates the §10/§11
  changes. Targeted re-runs are green (23/23 capture-focused tests,
  472/472 package tests, plus 882/882 across a broader adjacent-risk sweep
  after §10/§11 landed — see §13). A full-suite re-run is in progress as
  of this writing (§13).
- The root cause of the confirmed UAF (§10) — why the boxed-callback
  COPY-kind path loses a nested non-bitcopy field's retain specifically,
  when the immediate-lambda REF-kind path (§3) does not — is not
  understood, only worked around via narrowing. Worth a dedicated
  follow-up investigation before anyone considers widening the
  Copy-downgrade past bitcopy types.
- No git add/commit/staging performed — patch is in the working tree on
  `fix/projected-capture-lowering`, ready for review.

## 13. Third review round — scope-tightening findings, no new bugs

A third review pass raised two findings against the §10/§11 state. Both
were narrow, correct catches with no behavioral surprise — the bitcopy
narrowing itself was confirmed coherent.

**Medium/scope risk — the `--emit-package` projected-capture guard (§10/§11)
scanned every `typed_fns` entry, including dependency HIR merged in for
cross-module type-checking, not just the functions actually re-serialized
into the emitted payload.** `typed_fns` includes functions loaded via
`_pkg_hir_loaded` (a dependency package's HIR, pulled in for cross-package
generic instantiation) alongside the current build's own source functions;
the payload-assembly code further down (`source_module_ids`) explicitly
excludes `_pkg_hir_loaded` fn_ids for exactly this reason — only the
current source build's own modules end up in the `.dmp`. My new guard
didn't apply the same exclusion, so a projected capture anywhere in a
loaded dependency's HIR could reject the CURRENT build even though that
dependency function is never part of what's being emitted. Fixed: the
guard's loop now skips any `_fn_id in _pkg_hir_loaded` the same way
`source_module_ids` does. Practical reachability note: today, no
dependency `.dmp` can actually contain a projected capture in the first
place, because building THAT dependency via `--emit-package` would hit
this same guard at ITS OWN build time — so this exact false-positive isn't
constructible via a live round-trip through this toolchain right now. The
fix is still correct and worth keeping (matches the established filtering
invariant used two screens down in the same function, and guards against a
future scenario — an older pre-toolchain-restriction dependency package,
or a later relaxation of the package-serialization limitation itself).
`lang/tests/packages/` re-run: 472/472 passed (469 pre-existing + 3 new),
no regression from the added filter.

**Low — a stale comment in `capture_discovery.py` (around the MOVE+proj
downgrade site) still said "A Copy-typed projected field ... is safe" and
described routing non-bitcopy values through `_copy_if_ref_alias`,
contradicting the corrected contract stated in the function's own
docstring a few dozen lines above (Copy AND bitcopy, `String`/struct
routing through `_copy_if_ref_alias` is exactly what caused the confirmed
UAF in §10).** Fixed: reworded to match the docstring's contract exactly,
with a pointer to `borrow_checker_pass.py::_is_copy_projected_field`'s
fuller explanation, so a future reader hits the correct story regardless
of which of the two comments they read first.

No test changes were needed for the Low finding (comment-only). No behavior
changed for either finding except closing the scope-risk's theoretical gap.

## 14. Full unfiltered suite — final result (post all three review rounds)

`lang/tests` full run: **3385 passed, 5 skipped, 3 failed** (34m30s).
+5 passed vs. the §5 baseline (3380), matching the tests added across both
review rounds (2 lock-in rejections added to
`test_boxed_callback_projected_move_capture_rejected.py` + 3 new tests in
`test_package_emit_projected_capture_rejected.py`). The same 3 failures as
the §5 baseline, unchanged and still pre-existing/unrelated to this branch
(entrypoint-`pub` policy, `/tmp`-root hygiene debt) — no new failures from
any change in this branch across all three review rounds.

## 15. Fourth review round — a doc/test scope mismatch (implementation was correct, docs were too narrow)

A fourth review pass raised two findings.

**Medium — the narrowing (§10) was documented and tested as "scalar
bitcopy types (Int, Bool, Float, Byte, Uint, etc.)", but the actual
implementation checks `type_table.is_bitcopy(ty)`, and `is_bitcopy` is
TRANSITIVE for structs — a Copy struct is bitcopy iff every field is
(recursively) bitcopy, per `types_core.py::TypeTable.is_bitcopy`'s STRUCT
case.** So the real, shipped behavior already accepted a Copy struct
composed entirely of bitcopy fields (e.g. `Point { x: Int, y: Int }`
marked `implement core.Copy for Point {}`), not just bare scalars — while
every doc and test said "scalar" and "Copy struct/variant fields remain
rejected." Verified directly: built the `Point` case (a Copy struct field
captured implicitly in a boxed callback, immediate-invoked and returned)
and confirmed it compiles clean under ASAN and runs correctly (exit 0,
correct values round-tripped through the boxed callback and
`conc.spawn`/`.join()`). This is actually SAFE and intentional-once-you-
think-it-through — a fully-bitcopy struct has no refcounted content
anywhere in its closure, so there is nothing for the confirmed UAF (§10)
to apply to. The implementation was correct; the docs/tests undersold it.
Fixed by rewording every "bitcopy types (Int, Bool, ...)" mention
(`borrow_checker_pass.py::_is_copy_projected_field`, `lambda_validate.py`,
`capture_discovery.py`'s resolver contract, and this test file's module
docstring) to state the real, transitive contract — Copy AND bitcopy,
which includes all-bitcopy structs and excludes variants unconditionally
(variants are never bitcopy regardless of field types) — and added
`test_copy_typed_projected_bitcopy_struct_field_compiles_and_runs` to lock
in the struct case as a real positive test rather than leaving it
implementation-only. `test_boxed_callback_projected_move_capture_rejected.py`
re-run: 6/6 passed (up from 5).

**Low — stale package-test counts across the report/history made the
release notes look self-contradictory**: earlier sections said "480/480"
for the `lang/tests/packages/` suite while the final section said
"472/472". Root cause: "480" was actually the total from a COMBINED run
across `lang/tests/packages/` plus two specific driver test files (472 +
4 + 4), mislabeled in the surrounding prose as if it were the packages
suite alone. Fixed: normalized every package-suite-count mention (here and
in `doc/history.md`) to the correct, consistent 472/472 (469 pre-existing
+ 3 new), and updated the report's opening Status line and the
`doc/history.md` full-suite figure to the FINAL numbers (3385 passed,
three review rounds) instead of a stale intermediate snapshot, so a
reader going front-to-back doesn't hit numbers that look like they
disagree with each other.

This is the final verification; ready for review as-is.
