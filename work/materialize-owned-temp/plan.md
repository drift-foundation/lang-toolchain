# Slice: `_materialize_owned_temp` compiler helper

**Goal:** make the 4-step "synthetic owning local" pattern in
`hir_to_mir.py` impossible to forget by encapsulating it in a single
helper, and add a hard-gate static lint that requires every
`ensure_local + StoreLocal` site to either use the helper or carry an
explicit audit allow marker.

**Scope:** compiler only (`lang/driftc/stage2/hir_to_mir.py`).  No
runtime change, no ABI change, no Drift-source surface change.

**Status:** queued.  Do NOT start until the full e2e suite is green
on this branch with the `DRIFT_OWNED_STRING` slice landed (0.31.92).

**Lessons baked in from slice 1 (`DRIFT_OWNED_STRING`):**
- Hard gate — no warning-only escape, no `audit-pending`, no `audit-deferred`.  If a site cannot be triaged in-slice, stop and report.
- Audit + parser self-tests in the same slice.
- Audit lint lives in `lang/tests/driver/` (same shape as
  `test_ledger_cache_safety_mutation_audit.py` and slice 1's
  `test_drift_owned_string_audit.py`).
- Mid-slice convention discovery is allowed — if a probe fails
  during a "known-correct" migration, revert that one site,
  mark it `non-owning` / `consumed-immediately` / etc., document
  the discovery in `docs/history.md`.

---

## Background

`HIRToMIR` synthesizes locals to materialize owned values across
control flow (rvalue borrow receivers, Arc intrinsic borrow targets,
synthesized struct views, match-binder bind sites).  The "correct"
pattern is four steps that must always go together:

```python
local = f"{prefix}{self.b.new_temp()}"
self.b.ensure_local(local)             # 1. alloca in the function
self._local_types[local] = ty          # 2. type table for SSA/codegen
self._register_drop_local(local, ty)   # 3. scope-exit cleanup
self.b.emit(M.StoreLocal(local=local, value=value))  # 4. initialize
```

Forgetting step 3 (`_register_drop_local`) leaks the slot's inner
refcounted fields at function-exit paths that reach scope-0 cleanup
from inside a loop or nested arm.  Documented as a recurring class:

- `__borrow_tmp` (fixed 2026-05-16, the `chunked_loop_conditional_break_path_drop` bug).
- `__exc_params_view_*` (Cluster 1 fix, 2026-05-06).
- `__cap_move_*` (lambda capture move-out, original site).

The Arc-fat / Arc-as-interface / match-binder-bind paths do call
`_register_drop_local`, but each site hand-rolls the 4 steps with
prose comments.  No structural enforcement — review memory has been
the only safeguard, and the safeguard has missed three times.

A previous audit (during the chunked-loop bug investigation) found
**31 `ensure_local + StoreLocal` sites** without nearby
`_register_drop_local` calls.  ~17 are non-owning, transient, or
covered by separate register sites.  **14 are NEEDS REVIEW** — owning
shapes that could expose the same leak class under specific control-
flow shapes but aren't currently exercised by the memcheck suite.

---

## Deliverables

1. `_materialize_owned_temp` + `_materialize_owned_temp_for_borrow`
   helpers on `HIRToMIR` in `lang/driftc/stage2/hir_to_mir.py`.
2. Convert the 4 known-correct sites (HBorrow rvalue fallback;
   `_lower_arc_fat_intrinsic_call`; `_lower_arc_as_interface_op`;
   `_emit_synthesized_error_params_view`) to use the helpers.
   Acceptance: IR diff is empty per-fixture.
3. Triage all 14 NEEDS REVIEW sites in-slice — each becomes
   either (a) a helper-using site, (b) an explicit
   `_register_drop_local` + `materialize-audit: allow registered`
   marker, or (c) a `materialize-audit: allow <reason>` marker
   for non-owning / consumed-immediately cases.
4. Static audit test `lang/tests/driver/test_owned_temp_materialization_audit.py`
   that scans `hir_to_mir.py` for `ensure_local + StoreLocal` sites
   and requires the helper, an explicit `_register_drop_local` call,
   or an allow marker.  Hard gate, no `audit-pending`, no `audit-deferred`.
5. Parser self-tests (3-5 cases) on the audit, same shape as slice 1's
   `test_drift_owned_string_audit.py`.
6. `docs/history.md` entry; `lang/versions.py` minor bump
   (`0.31.92 → 0.31.93`); ABI unchanged at 14.

---

## Helper API

Lives on `HIRToMIR` in `lang/driftc/stage2/hir_to_mir.py`, next to
`_register_drop_local`:

```python
def _materialize_owned_temp(
    self,
    *,
    name_prefix: str,
    ty: TypeId,
    value: M.ValueId,
) -> str:
    """The blessed way to allocate a synthetic local that stores an
    OWNED value across any control flow.  Encapsulates the four steps
    that must always go together:

      1. `ensure_local(local)`              -- alloca'd in the function
      2. `_local_types[local] = ty`         -- type table for SSA/codegen
      3. `_register_drop_local(local, ty)`  -- scope-exit cleanup
      4. `emit(StoreLocal(local, value))`   -- initialize the slot

    Returns the synthesized local name.  Caller emits `AddrOfLocal`
    or further reads/writes against the returned name as needed.

    Use this anywhere the synthesized local will hold a materialized
    owned value that must participate in scope cleanup (i.e. its
    inner refcounted fields must be released when the enclosing
    scope exits).  Do NOT use for ephemeral SSA-style temps that are
    consumed immediately within the same expression -- those have no
    scope-cleanup concern and use `b.new_temp()` directly.
    """
    local = f"{name_prefix}{self.b.new_temp()}"
    self.b.ensure_local(local)
    self._local_types[local] = ty
    self._register_drop_local(local, ty)
    self.b.emit(M.StoreLocal(local=local, value=value))
    return local


def _materialize_owned_temp_for_borrow(
    self,
    *,
    ty: TypeId,
    value: M.ValueId,
    is_mut: bool = False,
) -> M.ValueId:
    """`_materialize_owned_temp` + `AddrOfLocal`.  Returns the
    borrow pointer.  This is the HBorrow rvalue fallback's whole
    job -- the canonical use case the slice is named after."""
    local = self._materialize_owned_temp(
        name_prefix="__borrow_tmp", ty=ty, value=value,
    )
    ptr = self.b.new_temp()
    self.b.emit(M.AddrOfLocal(dest=ptr, local=local, is_mut=is_mut))
    return ptr
```

`_register_drop_local` is internally idempotent and gated by
`_needs_runtime_drop` / `_type_is_destructible`, so unconditional
call inside `_materialize_owned_temp` is safe for trivially-droppable
`ty` (Int, Bool, etc.) — the registration just no-ops.  Same
contract as the inline pattern.

---

## First migration sites (in priority order)

All four are known-correct sites that already implement the 4-step
pattern by hand.  Converting them PROVES the helper preserves
bit-for-bit IR shape before any new sites adopt it.

| # | Site | hir_to_mir.py | Helper |
|---|------|---------------|--------|
| 1 | `_visit_expr_HBorrow` rvalue fallback | ~2546–2575 | `_for_borrow` |
| 2 | `_lower_arc_fat_intrinsic_call` (chained-rvalue Arc<I>) | ~9295–9312 | `_for_borrow` |
| 3 | `_lower_arc_as_interface_op` (rvalue Arc<T>) | ~9492–9509 | `_for_borrow` |
| 4 | `_emit_synthesized_error_params_view` call site (inside `_lower_method_call_with_info`) | ~9687–9706 | base `_materialize_owned_temp` (the AddrOf at this site is a mutable borrow on the synthesized view struct) |

**Acceptance criterion per conversion:** generate IR for the
pinned regression fixture for each site, diff against pre-conversion
IR.  Diff MUST be empty.

| # | Pinned fixture | Why this fixture |
|---|----------------|------------------|
| 1 | `lang/tests/codegen/e2e/borrow_tmp_match_loop_outer_scope_drop/` | Pinned by slice 1's family work; exercises HBorrow rvalue fallback exactly. |
| 2 | TBD — find/write an e2e that hits chained-rvalue fat-Arc method receiver | If none exists, add a tiny one to `lang/tests/codegen/e2e/`. |
| 3 | TBD — same as #2 for `Arc::as_interface()` on rvalue. | |
| 4 | An e2e or driver test that round-trips `e.params.encode_compact()` through a catch arm | Should exist already; locate during the slice. |

If diff is non-empty for any conversion → helper has semantic drift;
do not land.  Add an inline `# materialize-audit: allow <reason>`
explaining why this site stays hand-rolled, and triage the helper
gap as a follow-up.

---

## NEEDS REVIEW site triage (in-slice; hard gate forces this)

Captured in
`project_chunked_loop_conditional_break_path_drop.md` from the
chunked-loop slice.  Reproduced here for slice planning:

| Line | Function | Var | Initial classification |
|------|----------|-----|------------------------|
| 1453 | `_ensure_arm_scrut_ptr` | `arm_scrut_local` | match scrutinee — possibly registered via match_cleanup_authoring |
| 1458 | `_ensure_arm_scrut_ptr` | `source_local` | same family |
| 1701 | `_ensure_arm_scrut_ptr` | `tmp_local` | likely covered by binder-side register at line 1742 |
| 2827 | `_move_from_callback_capture_slot` | `tmp_local` | owning move-out staging; followed by `M.MoveOut`; consumed-in-place |
| 3162 | `_visit_expr_HIndex` | `tmp_local` | array element temp |
| 3467 | `_visit_expr_HMapLiteral` | `map_local` | owning constructed map |
| 4325 | `_lower_lambda_immediate_call` | `env_local` | lambda env |
| 4998 | `_lower_error_encode_compact` | `fqn_local` | error JSON envelope temp |
| 5148 | `_project_capture_to_json_text` | `str_local` | capture JSON projection |
| 5275 | `_lower_typed_catch_field_proj` | `json_local` | typed-catch JSON |
| 5285 | `_lower_typed_catch_field_proj` | `key_local` | typed-catch key |
| 6847 | `_emit_diagnostic_owning_throw` | `struct_local` | diagnostic owning struct (consumed by HThrow on this path) |
| 6937 | `_visit_expr_HTernary` | `temp_local` | ternary result |
| 7128 | `_visit_expr_HTryExpr` | `binder_local` | try-expr binder |

**Per-site decision rule:**

1. **Owning + flows to scope-exit cleanup → CONVERT** to `_materialize_owned_temp`.
   The helper's `_register_drop_local` will register it correctly.
2. **Owning + consumed inline (immediately followed by `MoveOut` /
   `DropValue` / `Call` that takes the value) → keep inline pattern**
   + add `# materialize-audit: allow consumed <one-line reason>` marker.
   Adding the helper here could change semantics (registering an
   already-consumed value risks double-drop at scope exit).
3. **Non-owning (Bool short-circuit, ephemeral SSA value) → keep
   inline pattern** + add `# materialize-audit: allow non-owning <reason>`.
4. **Owning but registered via a different path (binder bind, etc.)
   → keep inline pattern** + add `# materialize-audit: allow registered <reason>`.
5. **Genuinely unclear → write a heap-arg memcheck probe**
   (analog of slice 1's `"x" + "y"` probe for the runtime ABI), find
   the failure mode, then decide.

For each NEEDS REVIEW site, the per-site analysis goes in the commit
message of slice 2 (or in `project_owned_temp_audit.md` if the slice
splits into multiple commits — see Sequencing below).

**Risk acknowledgment:** triaging all 14 sites in one slice is the
high-cost part.  K's prior framing suggested deferring; the hard-gate
audit removes that option.  Acceptable because the per-site work is
small (each is a 5-10 line judgment call) and the alternative —
leaving the audit out — re-creates the recurring class.

**No deferral escape.** If a particular site requires investigation
that exceeds the slice's timebox, STOP the slice and report that
site (e.g. open a tracking note, kick a probe slice for that single
site).  Do NOT land the audit with a `audit-deferred` / `audit-pending`
marker that silently lets the site slide — that recreates the
warning-only convention this slice is meant to replace.

---

## Audit lint

New test `lang/tests/driver/test_owned_temp_materialization_audit.py`.

**Scope:** `lang/driftc/stage2/hir_to_mir.py` only.  Other stage2
files don't synthesize new locals at scale.

**Algorithm (regex-based, same shape as the slice 1 audit and
`test_ledger_cache_safety_mutation_audit.py`):**

1. For each `self.b.ensure_local(NAME)` call site in the scoped file,
   open a 30-line forward window.
2. If the window contains BOTH `self._local_types[NAME] =` AND
   `self.b.emit(M.StoreLocal(local=NAME, ...))`, this is a candidate
   site.
3. The site must satisfy ONE of:
   - **(a) Helper body:** the `ensure_local(NAME)` is inside the
     body of the function `_materialize_owned_temp` (matched by
     enclosing-function name).  This is the ONE canonical inline
     site; every other production caller goes through this helper,
     which means the dangerous pattern only physically exists here.
     Detected by walking up from the `ensure_local` line to the
     nearest `def ` and checking the function name.
   - **(b) Explicit register:** the 30-line forward window contains
     `self._register_drop_local(NAME, ...)`.  Used by sites that
     hand-roll the pattern for reasons documented in the allow
     marker.
   - **(c) Allow marker:** a comment line within 10 lines of the
     `ensure_local` call matches the marker grammar (below).  Used
     by sites that are non-owning / consumed-in-place / registered
     elsewhere / synthesized-and-thrown.

Sites that USE `_materialize_owned_temp(...)` at the call site
(the post-conversion shape for the 4 known-correct sites and most
of the NEEDS REVIEW sites) are NOT candidate sites at all — the
scanner sees `local = self._materialize_owned_temp(...)`, not
`ensure_local + StoreLocal`.  Migration to the helper makes the
audit's surface area shrink, not grow.

**Allow marker format:**

```python
# materialize-audit: allow <reason-keyword> <free-form one-line>
```

Allowed reason keywords (typos fail; lint validates against this set):

| Keyword | When to use |
|---|---|
| `non-owning` | The synthesized local holds a non-droppable value (Bool short-circuit, Int ephemeral, etc.). |
| `consumed` | Owning value consumed inline by a following `MoveOut` / `DropValue` / Call that takes the value; never reaches scope-exit cleanup. |
| `registered` | Owning value registered via a different mechanism (match_cleanup_authoring, drop_flags, binder bind path).  Marker must name the registration site. |
| `synthesized` | Owning struct/variant assembled inline and consumed by the same expression (e.g. diagnostic owning throw). |
| `intentional` | Reviewed and deliberately hand-rolled for a specific reason (rare; marker must explain). |

**Marker proximity:** within 10 lines of the `ensure_local` call,
EITHER on the same line OR immediately preceding (matches the
`ledger-cache-safety-audit` rule).

**Hard gate; no warning-only escape.** No `audit-pending`, no
`audit-deferred`.  If a site genuinely cannot be triaged within
the slice, STOP and report — do not land the audit until every
site has a final classification.  A warning-only deferral
mechanism is still a warning-only deferral mechanism; renaming it
doesn't make the invariant honest.

---

## Parser self-tests

Same shape as slice 1's `test_drift_owned_string_audit.py`.
Synthetic-source unit cases that exercise the matcher independently
of whichever real `hir_to_mir.py` content exists at the time.

**What the audit actually scans for.**  The candidate set is sites
that contain `ensure_local(NAME)` + `_local_types[NAME] = …` +
`StoreLocal(local=NAME, …)` in a 30-line window.  Production code
that **uses** `_materialize_owned_temp(...)` does NOT contain those
patterns in the caller — the patterns are inside the helper body.
So "helper-using" sites simply disappear from the candidate set;
they're not a positive case the parser needs to validate.  The
parser's positive cases are:

1. The helper BODY itself — the lone canonical `ensure_local + StoreLocal`
   triple that lives inside `_materialize_owned_temp`.  The audit
   permits this one specific function (by name match) without
   requiring a marker.
2. Inline pattern with explicit `_register_drop_local` call in the
   same window.
3. Inline pattern with a recognized allow marker within 10 lines.

The negative cases are bare inline patterns missing both
`_register_drop_local` AND a marker, OR markers with unrecognized
reason keywords.

**Five self-tests** (mapped to the above):

1. `test_audit_self_helper_body_excluded` — a synthetic function
   named `_materialize_owned_temp` whose body contains the 4-step
   inline pattern passes the audit without needing a marker.  Pins
   the "helper body is the canonical site" allowance.
2. `test_audit_self_explicit_register_passes` — synthetic non-helper
   function with `ensure_local + _local_types[…] = … +
   _register_drop_local + StoreLocal` triple passes.
3. `test_audit_self_marker_with_recognized_reason_passes` — synthetic
   non-helper function with the `non-owning` (or any recognized)
   marker within 10 lines of `ensure_local` passes.
4. `test_audit_self_unmarked_unregistered_fails` — bare `ensure_local
   + _local_types[…] = … + StoreLocal` with neither
   `_register_drop_local` nor a marker fails.
5. `test_audit_self_bad_reason_fails` — marker with unknown reason
   keyword (`audit-pending`, `audit-deferred`, typos) fails.

All five must pass in CI alongside the real-tree gate.

**Explicitly NOT a test:** "site that calls `_materialize_owned_temp(...)`
passes" — that's not testing the dangerous pattern; the scanner
never sees the dangerous pattern at a helper call site (it's
abstracted behind the helper).  Including such a test would create
false confidence in coverage.

---

## Sequencing inside the slice

Order matters because the audit will fail until every site is
converted, register-explicit, or marker-annotated:

1. **Add the helpers** (`_materialize_owned_temp` and
   `_for_borrow`) to `hir_to_mir.py`.  No behavior change; audit
   wouldn't fail anything new.
2. **Convert the 4 known-correct sites** one at a time, verifying
   IR diff is empty per fixture after each conversion.
3. **Triage the 14 NEEDS REVIEW sites** one at a time.  For each:
   either convert + verify (option a), add `_register_drop_local`
   + marker (b), or add a `non-owning` / `consumed` / `synthesized`
   / `registered` / `intentional` marker (c).  Per-site IR diff
   check NOT required for marker-only changes (no MIR shape
   change); IS required for option (a) conversions.  If any site
   resists classification within the timebox, STOP the slice and
   report — do not paper over with a deferral marker.
4. **Add the audit lint and parser self-tests.**  Acceptance:
   green on the full `hir_to_mir.py` after #2 and #3.  All 5
   self-tests pass.
5. **`docs/history.md` + `lang/versions.py` bump.**

Land as a single slice (one PR / one commit family) — splitting
risks landing the audit without the conversions or vice versa, both
of which leave the tree in a transitionally inconsistent state.

---

## Files touched

| File | Change |
|---|---|
| `lang/driftc/stage2/hir_to_mir.py` | Add `_materialize_owned_temp` + `_for_borrow`; convert 4 known-correct sites; triage 14 NEEDS REVIEW sites (convert / register+marker / marker only) |
| `lang/tests/driver/test_owned_temp_materialization_audit.py` | New; ~160 LOC including 5 parser self-tests |
| `docs/history.md` | New top entry |
| `lang/versions.py` | `0.31.92 → 0.31.93`; ABI unchanged at 14 |
| `work/materialize-owned-temp/plan.md` | This file (already exists) |

**Patch size estimate:**
- Helpers: ~40 LOC.
- 4 conversions: net ~–30 LOC (each replaces ~10 lines with one helper call).
- 14 NEEDS REVIEW: split roughly 5 conversions (saving ~5 LOC each
  = ~–25), 5 register+marker (no net change), 4 marker-only (+~8 LOC).
- Audit lint + parser self-tests: ~160 LOC.
- **Total: ~150 LOC net new, of which ~160 is the test infrastructure
  and ~–10 is production code delta.**

---

## Verification

- **Per-conversion IR-diff check:** each of the 4 known-correct
  conversions produces byte-identical IR for its pinned fixture
  before and after.
- **Per-NEEDS-REVIEW triage:** if option (a) (convert), IR-diff
  check passes for any e2e that exercises the site.  If option (b)
  or (c) (markers), only the audit lint result matters.
- **Memcheck regression:** the 11-fixture cluster used by slice 1
  (`std_io_*`, `env_*`, `mutex_guard_*`, `borrow_tmp_*`, `array_*`)
  MUST remain memcheck-clean.
- **Full e2e:** must remain green.
- **Driver tests:** must remain green, including the new audit
  test (1 real-tree gate + 5 parser self-tests).
- **`borrow_tmp_match_loop_outer_scope_drop`** (the regression
  fixture added by the chunked-loop fix this same family) MUST
  stay memcheck-clean — it's the load-bearing pin for site #1's
  conversion.

---

## Risk assessment

**Low risk for the 4 known-correct conversions** (IR-identical
acceptance criterion is the safety net).

**Low-to-medium risk for the 14 NEEDS REVIEW triage.**  The
no-escape rule forces per-site reasoning that K previously wanted
to defer.  Acceptable because:
- The audit catches the recurring leak class structurally.
- Each per-site call is small (5-10 lines of judgment + a marker).
- If a site genuinely cannot be classified within the slice, the
  contract is to STOP and report — open a tracking note, kick a
  probe slice for that one site, and land the audit ONCE all sites
  are decided.  Deferral via the audit itself is not an option.

**Risk of premature triage:** if a NEEDS REVIEW site gets
mis-classified as `non-owning` when it's actually owning, the
audit would silently pass while the leak persists.  Mitigation:
the per-site triage decisions go in the commit message / project
memory; reviewers can audit the audit.  Memcheck regression is
the runtime backstop.

**Risk of breaking the IR-identical contract:** the helper's
`new_temp` counter increments in a specific order; if the
caller previously called `new_temp` in a different sequence
(e.g. for the AddrOf temp first, then `ensure_local`), the
generated MIR will have different temp numbers and the IR diff
will be non-empty even if behavior is identical.  Mitigation:
keep `_materialize_owned_temp_for_borrow`'s sequence — `new_temp`
for the local FIRST (inside the helper), then `new_temp` for the
AddrOf ptr — matching the pre-conversion sequence at the 4
sites.  If a site's pre-conversion order differs, leave it
hand-rolled with an `intentional` marker.

---

## Out of scope

- Other stage2 files (cleanup_authoring, drop_flags, etc.) — those
  consume but don't synthesize new owning locals.
- Stage1 (ast_to_hir) — different ownership semantics; covered by
  the borrow-materialize pass.
- LLVM codegen — works against MIR, no synthesis at the local
  level.
- Generalizing the helper to non-MIR contexts.

---

## Open questions to resolve at slice kickoff

1. **Fixture coverage for sites #2-4.**  Do existing e2e tests
   exercise the chained-rvalue Arc paths and the
   `error_params_view` path?  If not, add minimal fixtures during
   the slice (each ~30 lines of Drift + an `expected.json`).
2. **Stop-and-report policy.**  How many unresolved NEEDS REVIEW
   sites are acceptable at slice landing?  **Answer: zero.**  Every
   site decided before merge.  If a site requires investigation
   that exceeds the slice's timebox, stop the slice, open a
   tracking note for that site, and land the audit ONLY when all
   sites are decided.  No deferral marker exists in the audit
   vocabulary — by design.
3. **Counter ordering for `_for_borrow`.**  Confirm the helper's
   `new_temp` sequence (local-then-ptr) matches the existing 4
   sites' counter ordering.  If any site's counter ordering is
   ptr-then-local, the IR diff will fail; either re-do that site
   inline with a marker or adjust the helper to expose ordering
   control.
