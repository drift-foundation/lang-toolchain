# Borrow Checker Escape Context Model — Design Document

Author: Klaudia
Date: 2026-02-20
Status: Approved as roadmap candidate — concerns addressed below, implementation not started

---

## 1. Background & Motivation

### 1.1 What A5 is

A5 is the architectural recommendation from the Feb-2026 code review:

> **Borrow checker: extend escape analysis to `spawn` / callback boundaries.**
> The fix for F3 and F7 will be easier once there is a defined notion of
> "escape context" (local scope vs. global/thread boundary) threaded through
> the borrow checker pass. This is a non-trivial design change and should be
> planned separately.

F3/F7 landed a v0 MVP (binary non-escaping check via `param_nonretaining`).
A5 replaces that MVP with a first-class escape-context model that can express
structured concurrent lifetimes, thread boundaries, and immediate-call scopes
within a unified, extensible framework.

### 1.2 Why it matters

Current limitation: a lambda with any borrowed capture is either
`param_nonretaining=True` (safe) or rejected with a generic message. There is
no gradation between "must be called synchronously" and "may be sent to a
spawned detached thread". This means:

- **False negatives**: a `&mut T` borrowed capture passed to `conc.spawn` is
  only caught if `spawn`'s parameter is annotated `param_nonretaining=False`
  (the default). If the annotation is absent or wrong, the error is silent.
- **False positives**: a borrowed-capture lambda passed to `conc.scope` (a
  structured scope that guarantees completion before return) is rejected even
  though it is provably safe if the borrowed place outlives the scope call.
- **Opaque diagnostics**: the error message "closures with borrowed captures
  are non-escaping in v0" does not tell the user whether the problem is thread
  escape, long-lived storage escape, or incorrect non-retaining annotation.
- **No spawn/reactor/scope detection**: stdlib boundary functions must currently
  be annotated by hand; there is no mechanism to express gradations.

### 1.3 Scope

This document covers:
- The `EscapeLevel` type taxonomy and its relationship to `Loan` and
  `FnSignature`
- Required changes to `borrow_checker.py`, `borrow_checker_pass.py`,
  `checker/__init__.py`, and stdlib signature annotations
- A phased implementation plan that preserves all existing regressions at
  every checkpoint
- Open questions about structured-scope lifetime reasoning (Phase 4)

This document does **not** cover:
- Full NLL (Non-Lexical Lifetimes) region inference — Drift's NLL-lite
  lexical regions are kept as-is
- Explicit lifetime parameters (`'a`) in the language surface — Drift has no
  lifetime annotation syntax and this design stays within that constraint
- A1–A4 architecture items — separate effort

### 1.4 A5 Guardrails (mandatory, applies to every phase)

These rules mirror the boundary discipline used in recent hardening (F1–F15)
and apply without exception throughout A5 implementation:

1. **Regression-first.** Every behavior change requires a failing test that
   demonstrates the problem *before* the fix lands. No fix without a regression.
2. **No stdlib workarounds.** Stdlib source must not be rewritten to work around
   borrow-checker gaps. If a valid stdlib pattern is rejected, the borrow checker
   must be fixed or the pattern must be flagged as a known limitation with a
   tracked issue, not silently rewritten.
3. **Stable diagnostic codes.** E_ESCAPE_* codes and message strings are part of
   the test contract from Phase 2 onward. Renames require explicit test updates
   and a note in the progress log — no silent renames.
4. **No silent default behavior changes.** Every change to what the unannotated
   default (`THREAD`) allows or rejects must be tested with a positive and a
   negative regression before landing.
5. **Cross-sensitivity validation.** The ref-field and `Result::Ok` binder paths
   are high-sensitivity areas that interact with borrow-checker contracts. The
   validation subset (§9) must be run at every phase checkpoint, not only the
   new A5 tests.
6. **Test assertions: stable prefix and note only.** No test expectation may
   assert the full text of a diagnostic message. Tests must assert only: the
   diagnostic code (e.g., `E_ESCAPE_THREAD`), a stable key fragment (e.g., the
   captured binding name), and the phase tag (`"borrow_check"`). Full-text
   assertions cause wording churn to break tests across minor message changes.
   The unannotated-param note (`"no escape-level annotation; treated as THREAD
   in MVP"`) is considered a stable key fragment and may be asserted directly.
7. **Stop gate.** If any test in the §9 high-sensitivity validation subset fails,
   the phase is blocked — no "continue with known fail" in A5. A failing
   high-sensitivity test is a regression, not a known issue. The phase may not
   merge until the subset is green.

---

## 2. Current State

### 2.1 Borrow checker data model (relevant subset)

```
borrow_checker.py
  Place (frozen dataclass)        — borrowable location (base + projections)
  Projection (FieldProj/IndexProj/DerefProj)
  places_overlap(a, b) -> bool    — single overlap source of truth

borrow_checker_pass.py
  LoanKind (Enum): SHARED | MUT
  Loan (frozen dataclass):
    place: Place
    kind: LoanKind
    temporary: bool               — dropped at expression scope exit
    live_blocks: Optional[frozenset[int]]  — NLL-lite region
    origin_span: Span
    ref_binding_id: Optional[int]
  _FlowState:
    place_states: Dict[Place, PlaceState]
    loans: Set[Loan]
  BorrowChecker:
    param_nonretaining aware via FnSignature.param_nonretaining
```

### 2.2 Current lambda escape logic

```python
# borrow_checker_pass.py
def _lambda_has_borrow_capture(lam) -> bool          # L323
def _report_lambda_escape_if_borrowed(lam, span)     # L330

# wired at arg positions (HCall/HMethodCall/HInvoke):
# L1865, L1883, L1911, L1926
```

Decision tree at each call argument position:

```
is arg a lambda?
  └─ yes → does it have any REF/REF_MUT capture?
       └─ yes → is param_nonretaining[i] == True?
            ├─ yes → add capture loans as temporary (safe)
            └─ no  → reject with generic message
```

`param_nonretaining` is `Optional[List[Optional[bool]]]` on `FnSignature`.
`None` at a position means "not annotated" (treated as False = escaping).

### 2.3 What is missing

| Gap | Impact |
|-----|--------|
| No gradation between LOCAL, SCOPED, THREAD escape levels | Can't distinguish `scope` (safe) from `spawn` (unsafe) for borrowed captures |
| Binary `param_nonretaining` carries no escape-level semantics | Callee can claim non-retaining but send lambda to a thread internally |
| No place-lifetime reasoning at scope boundaries | Can't prove borrow outlives `conc.scope` call |
| `spawn`/`reactor`/`scope` not recognized as distinct boundaries | Error message always generic |
| `EscapeLevel` not in `Loan` | Can't propagate required escape level through loan chains |

---

## 3. Architecture: Escape Context Model

### 3.1 EscapeLevel taxonomy

```
EscapeLevel (IntEnum, ordered from most restrictive to most permissive):

  IMMEDIATE = 0
    Lambda must be called synchronously within the current expression
    evaluation. The callee cannot store the lambda or delay its call.
    Example: an immediately-invoked closure `(|x| => x + 1)(5)`.

  LOCAL = 1
    Lambda may be passed to a callee and called within that callee's
    stack frame, but must be dropped before the callee returns.
    Equivalent to current param_nonretaining=True semantics.
    Example: sort_in_place(arr, |a, b| => a.cmp(b)).

  SCOPED = 2
    Lambda may escape into a structured concurrent scope whose lifetime
    is bounded by the current stack frame. All threads spawned within
    the scope complete before the scope call returns. Borrows are safe
    if the borrowed place outlives the scope call site.
    Example: conc.scope(|s| => { s.spawn(|| => { uses borrow }) }).

  THREAD = 3
    Lambda may be sent to a detached virtual thread with an unbounded
    lifetime. Borrows of local places are never safe at this level.
    Example: conc.spawn(|| => { ... }).

  STATIC = 4
    Lambda (and all its captures) must be 'static — no borrows of
    anything shorter-lived than the program. Reserved for global
    registry/reactor callbacks that outlive any individual request.
    Example: reactor.register_handler(|| => { ... }).
```

The ordering is meaningful: a loan that is safe at level N is also safe at
any level ≤ N. A loan that is only safe at IMMEDIATE is not safe at LOCAL,
SCOPED, THREAD, or STATIC.

### 3.2 Loan escape level

Every `Loan` carries the maximum escape level it may safely reach. For a loan
of a local variable, the maximum is always `LOCAL` (the loan cannot outlive
the function). The field is `max_escape: EscapeLevel`.

```
Loan (updated):
  place: Place
  kind: LoanKind
  temporary: bool
  live_blocks: Optional[frozenset[int]]
  origin_span: Span
  ref_binding_id: Optional[int]
  max_escape: EscapeLevel = EscapeLevel.LOCAL   # NEW
```

The value of `max_escape` is determined at loan creation:
- Borrow of a local/param → `LOCAL`
- Borrow of a place proven to live longer (future: heap, global) → could be
  raised; not needed in the initial implementation
- Loan cloned from a ref parameter → inherits from caller signature (future)

### 3.3 Lambda escape level computation

A lambda with captured borrows has an effective `escape_level` equal to the
minimum `max_escape` across all its captured loans. This is the maximum level
at which the lambda can safely be passed.

```
_lambda_escape_level(lam, state) -> EscapeLevel:
    loans = {l for l in state.loans if l.ref_binding_id in lam_capture_binding_ids}
    if loans is empty:
        return EscapeLevel.STATIC   # no borrow captures — can go anywhere
    return min(l.max_escape for l in loans)
```

A lambda with no borrowed captures can be passed at any escape level
(including THREAD and STATIC).

### 3.4 FnSignature: param_escape_level

Replace `param_nonretaining: Optional[List[Optional[bool]]]` with:

```python
param_escape_level: Optional[List[Optional[EscapeLevel]]] = None
```

Semantics of each element:
- `None` → not annotated; treated as `EscapeLevel.THREAD` (worst case, most
  permissive requirement on the lambda, most restrictive on the borrow)
- `EscapeLevel.LOCAL` → equivalent to current `param_nonretaining=True`
- `EscapeLevel.SCOPED` → structured scope; borrowed captures allowed if
  provably outliving the scope (Phase 4)
- `EscapeLevel.THREAD` → detached thread; no borrowed captures allowed
- `EscapeLevel.STATIC` → must be 'static; no captured borrows at all

Backward compatibility bridge during migration:
```python
# checker/__init__.py or FnSignature property
@property
def effective_param_escape_level(self, i: int) -> EscapeLevel:
    if self.param_escape_level is not None and i < len(self.param_escape_level):
        lvl = self.param_escape_level[i]
        if lvl is not None:
            return lvl
    # fall back to param_nonretaining
    if self.param_nonretaining is not None and i < len(self.param_nonretaining):
        nr = self.param_nonretaining[i]
        if nr is True:
            return EscapeLevel.LOCAL
    return EscapeLevel.THREAD   # default: worst case
```

This bridge allows Phase 2 migration without breaking any existing stdlib
annotations.

### 3.5 Call site validation

At each call argument position that receives a lambda:

```
required_escape = sig.effective_param_escape_level(arg_index)
lambda_level    = _lambda_escape_level(lam, state)

if lambda_level < required_escape:
    _report_escape_violation(lam, required_escape, lambda_level, span)
```

`_report_escape_violation` emits a diagnostic with:
- Clear message naming the escape boundary (thread spawn, static registry, etc.)
- The span of the call argument
- A note naming the specific borrow that restricts the lambda's escape level
- The span of the original borrow expression
- **If the required level came from the unannotated default:** an additional note:
  `"parameter has no escape-level annotation; treated as THREAD in MVP"`
  This note fires only on the error path (not on accepted calls), so it is not
  noisy for code that doesn't capture borrows.

### 3.6 Structured scope lifetime reasoning (Phase 4 scope)

For `SCOPED` parameters, `lambda_level < SCOPED` (i.e., the lambda has LOCAL
loans) is acceptable IF the borrowed places are provably alive across the
scope call:

```
_check_lambda_scope_escape(lam, scope_call_expr, state) -> bool:
    for loan in _captured_loans(lam, state):
        place = loan.place
        if _state_for(state, place) != VALID:
            return False   # place might be moved before scope ends
        # After scope returns: is place still in scope?
        # The scope call is a statement; places defined in enclosing block are
        # alive after the call — satisfied trivially if place is a param or
        # local defined before this statement.
        if not _place_is_defined_before_stmt(place, current_stmt):
            return False
    return True
```

**MVP conservative rule — not a full lifetime proof.**
`_place_is_defined_before_stmt` is a syntactic proxy: it checks that the
place was `let`-bound or assigned before the scope call statement in the
direct enclosing block. This is conservative — it will reject some patterns
that are actually safe (e.g., borrows through call-return values stored in
locals in an outer block). It does NOT constitute a soundness proof; it is a
first-cut heuristic that is sound for the common pattern:

```
var x = ...           // defined before scope
conc.scope(|s| => {
    s.spawn(|| => { use x })   // accepted: x is in enclosing block
})
```

Patterns rejected conservatively (false positives) will be tracked as known
limitations and relaxed in future iterations, not silently accepted.

**Explicit non-goal: nested-block false positive (must be tested).**
The following pattern looks safe but is intentionally rejected by the MVP rule
because `x` is not defined in the direct enclosing block of the `conc.scope`
call — it is defined in an inner block:

```drift
fn example() nothrow -> Void {
    {
        var x = "hello"
        conc.scope(|s| => {
            s.spawn(|| => { use x })   // REJECTED: x in nested block, not direct enclosing
        })
    }
}
```

A test must be written for this shape, marked as an **expected conservative
false positive**:
```
lang/tests/borrow_checker/test_escape_level_model.py:
  - test_scoped_spawn_nested_block_false_positive:
      → E_ESCAPE_SCOPE emitted
      docstring: "conservative MVP: x is defined in a nested block, not the
      direct enclosing block of conc.scope; rejected even though safe.
      Known false positive — tracked for future relaxation."
```

This test must not be deleted or converted to an accept case without a
corresponding design change to `_place_is_defined_before_stmt`.

This analysis does NOT require full lifetime inference — it only needs the
existing place-state map and the CFG position of the scope call statement.

### 3.7 Diagnostic taxonomy

| Situation | Code | Message |
|-----------|------|---------|
| Borrowed capture sent to detached thread | E_ESCAPE_THREAD | "closure captures borrowed value `{name}` which cannot be sent to a detached virtual thread" |
| Borrowed capture stored in escaping position | E_ESCAPE_STORE | "closure captures borrowed value `{name}` which cannot escape its original scope" |
| Borrowed capture sent to static/global registry | E_ESCAPE_STATIC | "closure captures borrowed value `{name}` which does not have 'static lifetime; reactor and global callbacks require owned or 'static captures" |
| Borrowed capture in scoped spawn, borrow may not outlive scope | E_ESCAPE_SCOPE | "closure captures borrowed value `{name}` which may not be valid for the full duration of the concurrent scope" |
| Generic / unannotated param (THREAD default) | E_ESCAPE_THREAD | same as thread message, plus note: "parameter has no escape-level annotation; treated as THREAD in MVP" |

All diagnostics include:
- `phase = "borrow_check"`
- `span` of the problematic lambda argument
- `notes` containing the span of the original borrow / capture expression
- `notes` naming the borrow that restricts the escape level
- For unannotated params: additional note about THREAD default (see §3.5)

---

## 4. Impact Analysis

### 4.1 Files changed

| File | Change type | Description |
|------|-------------|-------------|
| `lang/driftc/borrow_checker.py` | Addition | `EscapeLevel` enum (~15 lines) |
| `lang/driftc/borrow_checker.py` | Addition | `max_escape: EscapeLevel` field on `Loan` |
| `lang/driftc/borrow_checker_pass.py` | Replace | `_lambda_has_borrow_capture` + `_report_lambda_escape_if_borrowed` → `_lambda_escape_level` + `_check_lambda_escape_level` |
| `lang/driftc/borrow_checker_pass.py` | Extend | `_borrow_place`: set `loan.max_escape` at creation |
| `lang/driftc/borrow_checker_pass.py` | Extend | Call-arg lambda validation (L1865, L1883, L1911, L1926): use escape-level comparison |
| `lang/driftc/borrow_checker_pass.py` | Addition (Phase 4) | `_check_lambda_scope_escape` |
| `lang/driftc/checker/__init__.py` | Addition | `param_escape_level` on `FnSignature`; `effective_param_escape_level` bridge property |
| `lang/driftc/checker/__init__.py` | Extend | Stdlib signature builder: annotate `conc.spawn`, `conc.scope`, `lang.thread.vt_spawn`, reactor/registry callbacks |
| `stdlib/std/concurrent.drift` | No change (annotations in checker) | Signatures updated in checker only |
| `lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py` | Update | Add new test cases for EscapeLevel diagnostics |
| New: `lang/tests/borrow_checker/test_escape_level_model.py` | New | Unit tests for EscapeLevel taxonomy, lambda_escape_level computation, per-level diagnostics |
| New: `lang/tests/codegen/e2e/borrow_escape_spawn_rejected/` | New | E2e: borrowed capture to `conc.spawn` rejected (E_ESCAPE_THREAD) |
| New: `lang/tests/codegen/e2e/borrow_escape_scope_accepted/` | New | E2e (Phase 4): borrowed capture to `conc.scope` accepted when place outlives scope |
| New: `lang/tests/codegen/e2e/borrow_escape_static_rejected/` | New | E2e: borrowed capture to static callback rejected (E_ESCAPE_STATIC) |
| `lang/tests/driver/test_callinfo_param_layout_contract.py` | Review | Check if `param_nonretaining` appears in assertions; update if so |
| `lang/tests/borrow_checker/test_lambda_capture_borrow_overlap.py` | Preserve | Must pass at every phase checkpoint |
| `lang/tests/borrow_checker/test_lambda_capture_borrow_overlap_method.py` | Preserve | Must pass at every phase checkpoint |

### 4.2 No changes to

- `lang/driftc/stage2/hir_to_mir.py` — escape context is borrow-checker only
- `lang/codegen/llvm/llvm_codegen.py` — no codegen impact
- `lang/driftc/mir_validate.py` — no MIR impact
- Language surface — no new syntax; escape levels are inferred from signatures

### 4.3 Stdlib annotation targets

These are checker-side signature annotations, not changes to stdlib source:

| Function / parameter | Current annotation | New annotation |
|----------------------|--------------------|----------------|
| `std.concurrent.spawn` callback param | `param_nonretaining=False` (default) | `param_escape_level=THREAD` |
| `std.concurrent.scope` outer closure | `param_nonretaining=False` (default) | `param_escape_level=SCOPED` |
| `std.concurrent.scope` inner spawn callback | `param_nonretaining=False` | `param_escape_level=THREAD` |
| `lang.thread.vt_spawn` | `param_nonretaining=False` | `param_escape_level=THREAD` |
| reactor/registry global callbacks | not annotated | `param_escape_level=STATIC` |
| `std.algo.sort_in_place` comparator | `param_nonretaining=True` | `param_escape_level=LOCAL` |
| `std.containers.HashMap` iteration callbacks | `param_nonretaining=True` | `param_escape_level=LOCAL` |
| User-defined `Fn*` / `Callback*` trait impls | (none) | Determined by trait definition (design TBD) |

### 4.4 Backward compatibility

The bridge property `effective_param_escape_level` means all existing
`param_nonretaining` annotations continue to work without any change during
migration. The migration plan removes `param_nonretaining` only at Phase 5
after all callsites are converted.

---

## 5. Implementation Plan

Each phase is independently reviewable and passes all existing regressions
before the next phase begins. The phases map to a review checkpoint.

---

### Phase 0 — Foundation types (no behavior change)

**Goal:** Add `EscapeLevel` and update `Loan` without changing any validation
logic. All existing tests pass unchanged.

**Changes:**

1. `lang/driftc/borrow_checker.py`:
   - Add `EscapeLevel(IntEnum)` with 5 values (`IMMEDIATE=0` … `STATIC=4`)
   - Add `max_escape: EscapeLevel = EscapeLevel.LOCAL` to `Loan` dataclass

2. `lang/driftc/borrow_checker_pass.py`:
   - `_borrow_place(...)`: pass `max_escape=EscapeLevel.LOCAL` when constructing
     new `Loan` objects (default matches current implicit behavior)
   - `_clone_loans_from_ref(...)`: propagate `max_escape` when cloning

3. `lang/driftc/checker/__init__.py`:
   - Add `param_escape_level: Optional[List[Optional[EscapeLevel]]] = None`
     to `FnSignature`
   - Add `effective_param_escape_level(i) -> EscapeLevel` bridge property

**Tests to add:**
```
lang/tests/borrow_checker/test_escape_level_model.py
  - test_escape_level_ordering: IMMEDIATE < LOCAL < SCOPED < THREAD < STATIC
  - test_loan_default_max_escape: new Loan defaults to LOCAL
  - test_loan_max_escape_propagation: cloned loan preserves max_escape
```

**Checkpoint commands:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/test_escape_level_model.py -v
```

**Regression checkpoint:** All existing borrow checker tests pass.

---

### Phase 1 — Lambda escape level computation

**Goal:** Introduce `_lambda_escape_level` as a replacement for
`_lambda_has_borrow_capture`. No diagnostic changes yet.

**Changes:**

1. `lang/driftc/borrow_checker_pass.py`:
   - Add `_captured_loan_binding_ids(lam) -> Set[int]`: returns binding ids of
     REF/REF_MUT captures in the lambda
   - Add `_lambda_escape_level(lam, state) -> EscapeLevel`:
     - If no REF/REF_MUT captures: return `EscapeLevel.STATIC`
     - Otherwise: find all loans whose `ref_binding_id` is in the capture set;
       return `min(l.max_escape for l in matching_loans)` or `LOCAL` if no
       matching loans found in state (conservative)
   - Add `_check_lambda_escape_level(lam, required: EscapeLevel, span)`:
     - Compute `lambda_level = _lambda_escape_level(lam, state)`
     - If `lambda_level < required`: emit diagnostic (use existing generic
       message for now — message upgrade is Phase 2)
   - Keep `_lambda_has_borrow_capture` and `_report_lambda_escape_if_borrowed`
     as stubs delegating to new helpers (no external behavior change)

**Tests to add:**
```
lang/tests/borrow_checker/test_escape_level_model.py (extend):
  - test_lambda_no_borrow_capture_is_static: lambda with only COPY/MOVE
    captures returns STATIC level
  - test_lambda_ref_capture_is_local: lambda with &T capture returns LOCAL
  - test_lambda_mut_ref_capture_is_local: lambda with &mut T capture returns LOCAL
```

**Checkpoint commands:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/test_escape_level_model.py -v
```

**Regression checkpoint:** All existing borrow checker tests pass unchanged.

---

### Phase 2 — Wire escape levels at call sites

**Goal:** Replace binary `param_nonretaining` check with escape-level
comparison at all 4 call-arg wiring sites. Introduce per-level diagnostic
messages.

**Changes:**

1. `lang/driftc/borrow_checker_pass.py`:
   - At L1865, L1883 (HCall args), L1911, L1926 (HInvoke args):
     - Replace `_report_lambda_escape_if_borrowed(lam, span)` with:
       ```python
       required = checker.effective_param_escape_level(arg_index)
       _check_lambda_escape_level(lam, required, span)
       ```
   - `_check_lambda_escape_level`: emit level-specific diagnostic per §3.7
   - Remove `_lambda_has_borrow_capture` and `_report_lambda_escape_if_borrowed`
     (their behavior is fully replaced)

2. `lang/driftc/checker/__init__.py`:
   - Annotate `conc.spawn` and `lang.thread.vt_spawn` with
     `param_escape_level=[EscapeLevel.THREAD]` in stdlib signature builders

**Tests to add:**
```
lang/tests/borrow_checker/test_escape_level_model.py (extend):
  - test_borrowed_capture_to_thread_param_rejected: E_ESCAPE_THREAD emitted
  - test_borrowed_capture_to_local_param_accepted: no error (LOCAL ≥ LOCAL)
  - test_no_capture_to_any_level_accepted: STATIC lambda passes THREAD param

lang/tests/codegen/e2e/borrow_escape_spawn_rejected/:
  main.drift: fn that tries to pass &mut T capture to conc.spawn
  expected.json: compile error with E_ESCAPE_THREAD

lang/tests/codegen/e2e/borrow_escape_static_rejected/:
  main.drift: fn that tries to pass &T capture to a STATIC-annotated param
  expected.json: compile error with E_ESCAPE_STATIC
```

**Checkpoint commands:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/test_escape_level_model.py -v
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/driver/test_boundary_matrix_result_variant_contract.py -q
PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j4 \
    borrow_escape_spawn_rejected \
    borrow_escape_static_rejected \
    result_ok_move_conn_source_drop_regression \
    struct_ref_field_result_ok_move_drop_once
```

**Regression checkpoint:**
- `test_invoke_optional_ref_and_lambda_escape.py` passes (existing cases still
  emit errors; message text may differ — update expected strings if needed)
- `test_lambda_capture_borrow_overlap.py` passes
- `test_lambda_capture_borrow_overlap_method.py` passes

---

### Phase 3a — Minimal annotated set (new checkpoint)

**Goal:** Annotate only the highest-impact boundary functions (`spawn`, `scope`,
one LOCAL callback) before sweeping all of stdlib. This checkpoint isolates
diagnostic stability early and reduces blast radius if annotation semantics need
adjustment.

**SCOPED behavior in this phase:** `SCOPED` is annotated on `scope`'s outer
closure but mapped conservatively to `LOCAL` behavior in
`effective_param_escape_level` until Phase 4 lands. Diagnostics for SCOPED
rejections carry an additional note: "SCOPED escape currently conservative (MVP):
place-lifetime validation not yet implemented; treated as LOCAL restriction."

**Changes:**

1. `lang/driftc/checker/__init__.py` (stdlib signature builders):
   - `std.concurrent.spawn` callback: `param_escape_level=THREAD` (already done
     in Phase 2; confirm present)
   - `std.concurrent.scope` outer closure: `param_escape_level=SCOPED`
     (behavior: conservative LOCAL until Phase 4)
   - `std.concurrent.scope` inner `s.spawn` callback: `param_escape_level=THREAD`
   - `std.algo.sort_in_place` comparator: migrate `param_nonretaining=True`
     → `param_escape_level=LOCAL`

2. `lang/driftc/checker/__init__.py`: in `effective_param_escape_level`, map
   `SCOPED` → `LOCAL` (conservative bridge; removed in Phase 4)

**Tests to add:**
```
lang/tests/borrow_checker/test_escape_level_model.py (extend):
  - test_scope_outer_closure_annotated_scoped: verify effective level is LOCAL
    (conservative bridge) until Phase 4
  - test_sort_in_place_comparator_local_accepted: &T capture in comparator → no error
  - test_static_level_dry_run: construct a synthetic FnSignature with
    param_escape_level=[STATIC] in the test helper (not a real stdlib call);
    assert a borrowed-capture lambda passed to it emits E_ESCAPE_STATIC with
    code "E_ESCAPE_STATIC" and phase "borrow_check". Purpose: exercise STATIC
    semantics in isolation before Phase 3b introduces them at full stdlib scope.
    Mark test as "dry-run / synthetic param" in its docstring.
```

**Checkpoint commands:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/test_escape_level_model.py -v
PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j4 \
    borrow_escape_spawn_rejected \
    result_ok_move_conn_source_drop_regression \
    struct_ref_field_result_ok_move_drop_once
```

---

### Phase 3b — Full stdlib annotation

**Goal:** Sweep all remaining stdlib boundary functions with correct escape
levels. First phase where STATIC boundary errors appear against real stdlib
signatures. STATIC semantics must have been exercised by the Phase 3a dry-run
before this phase begins.

**Pre-3b STATIC audit (mandatory before writing any annotation):**

Run the following to identify all reactor/registry callsites and existing tests
that may be affected:
```
grep -n "GlobalRegistry\|reactor\|register_handler\|vt_spawn" \
    lang/driftc/checker/__init__.py
grep -rn "GlobalRegistry\|reactor" lang/tests/codegen/e2e/
grep -rn "GlobalRegistry\|reactor" lang/tests/driver/
```

For each STATIC annotation target, record:
- The function/param name
- Whether any existing test currently passes a lambda with a borrowed capture
  to this param (these will become new rejections — list them explicitly in the
  progress log before annotating)

Produce a before/after diff of rejections before merging: run the full borrow
checker test suite before annotating, capture which tests pass, then run again
after annotating, and record every new failure. Each new failure must be either:
(a) an expected rejection with a new negative regression test added, or
(b) a false positive that blocks the annotation (borrow checker must be fixed first).

**Changes:**

1. `lang/driftc/checker/__init__.py` (stdlib signature builders):
   - `lang.thread.vt_spawn`: `param_escape_level=THREAD`
   - `std.runtime.GlobalRegistry` callbacks: `param_escape_level=STATIC`
   - `lang.thread` reactor callbacks: `param_escape_level=STATIC`
   - `std.containers.HashMap` / TreeMap / Deque / HashSet iteration callbacks:
     `param_escape_level=LOCAL`
   - All remaining `param_nonretaining=True` usages: migrate to
     `param_escape_level=LOCAL`
   - **Audit first:** run grep before annotating:
     ```
     grep -n "param_nonretaining" lang/driftc/checker/__init__.py
     ```
     Confirm each site before migrating.

**Tests to add:**

For each STATIC-annotated function, add a paired test — both the reject and the
accept case are required. No STATIC annotation may land without its accept
counterpart:
```
lang/tests/borrow_checker/test_escape_level_model.py (extend):
  - test_static_reactor_callback_rejected:
      &T capture to real STATIC-annotated reactor param → E_ESCAPE_STATIC
      Assert: code "E_ESCAPE_STATIC", phase "borrow_check", capture name fragment.
      Do NOT assert full message text.
  - test_static_reactor_callback_owned_accepted:
      owned (MOVE) capture to same STATIC-annotated param → no error.
      This is the "existing pattern still accepted" test — verifies that
      the annotation does not break code that correctly uses owned captures.
  - test_hashmap_iter_callback_local_accepted:
      &T capture in HashMap iteration callback (LOCAL) → no error
```

```
lang/tests/codegen/e2e/ — existing concurrent suite must still pass:
  - std_net_tcp_stress_connections (verify no regression)
  - concurrent_queue_limit_enforced (verify no regression)
```

**Checkpoint commands:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/driver/ -q
just test-e2e
# Verify no param_nonretaining remaining (should be zero after Phase 5,
# but track progress here):
grep -rn "param_nonretaining" lang/driftc/ | grep -v "effective_param_escape_level"
```

**Regression checkpoint:**
- Pre-3b audit complete; before/after diff documented in progress log
- All existing e2e tests that pass today still pass
- Every STATIC-annotated param has a paired accept + reject test
- All new E_ESCAPE_* assertions use code + key fragment only (no full text)
- Reactor/registry STATIC annotations do not break any existing stdlib usage

---

### Phase 4 — Structured scope lifetime reasoning

**Goal:** Lift the conservative LOCAL bridge on `SCOPED` parameters. After this
phase, a borrowed-capture lambda passed to a `SCOPED` param is accepted if and
only if the borrowed places pass the conservative MVP place-definition check
(§3.6). Patterns that fail the check emit `E_ESCAPE_SCOPE`; patterns that pass
compile successfully.

**Precondition:** Phase 3a and 3b are merged and stable. The existing Q3
(mutable scope-local captures across two spawns) must be verified as caught by
the existing MUT conflict detection before this phase merges.

This is the highest-complexity phase and the only one that requires new
control-flow analysis. See §3.6 for the algorithm and its explicit limitations.

**Changes:**

1. `lang/driftc/borrow_checker_pass.py`:
   - Add `_place_is_defined_before_stmt(place, stmt_index, block) -> bool`:
     - Returns True if `place` is a param, or was `let`-bound/assigned before
       `stmt_index` in `block` (conservative: only checks the direct enclosing
       block, not nested blocks)
   - Add `_check_lambda_scope_escape(lam, state, scope_call_span) -> bool`:
     - For each captured loan:
       - Check place is VALID in current state
       - Check `_place_is_defined_before_stmt(...)` — ensures it will still
         exist after the scope returns
     - If all pass: return True (safe to send at SCOPED level)
     - Otherwise: return False (emit E_ESCAPE_SCOPE)
   - In `_check_lambda_escape_level`: if required is `SCOPED` and
     `lambda_level == LOCAL`, call `_check_lambda_scope_escape` before
     emitting an error — only emit if scope check also fails

2. `lang/driftc/checker/__init__.py`:
   - Remove the conservative "treat SCOPED as LOCAL" fallback from Phase 3

**Tests to add:**
```
lang/tests/borrow_checker/test_escape_level_model.py (extend):
  - test_scoped_spawn_with_outliving_borrow_accepted:
      var x = 42
      conc.scope(|s| => { s.spawn(|| => { read x }) })
      → no error (x is LOCAL, outlives scope)
  - test_scoped_spawn_with_non_outliving_borrow_rejected:
      conc.scope(|s| => {
          var x = 42
          s.spawn(|| => { read x })   # x defined inside scope — may not outlive
      })
      → E_ESCAPE_SCOPE
  - test_scoped_spawn_nested_block_false_positive:   ← PINNED EXPECTED FALSE POSITIVE
      {
          var x = "hello"
          conc.scope(|s| => { s.spawn(|| => { use x }) })
      }
      → E_ESCAPE_SCOPE
      docstring: "conservative MVP: x is defined in a nested block, not the
      direct enclosing block of conc.scope. Rejected even though provably safe.
      Known false positive — do not convert to accept without a design change
      to _place_is_defined_before_stmt."
      This test exists to document intentional conservative behavior. It must
      remain a negative case until the algorithm is explicitly relaxed.

lang/tests/codegen/e2e/borrow_escape_scope_accepted/:
  main.drift: valid pattern — &T passed to conc.scope inner spawn
  expected.json: compiles and runs correctly
```

**Checkpoint commands:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/test_escape_level_model.py -v
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/driver/ -q
PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j4 \
    borrow_escape_scope_accepted \
    borrow_escape_spawn_rejected \
    result_ok_move_conn_source_drop_regression \
    struct_ref_field_result_ok_move_drop_once
```

**Regression checkpoint:**
- All Phase 0–3b regressions pass
- Q3 (mutable scope-local capture data race) verified as caught by existing MUT
  conflict detection before this phase merges
- The two new scope tests (`test_scoped_spawn_with_outliving_borrow_accepted` and
  `test_scoped_spawn_with_non_outliving_borrow_rejected`) pass
- `borrow_escape_scope_accepted` e2e compiles and runs correctly

**Known limitations (conservative MVP — not bugs, tracked for future relaxation):**
- Borrows through call-return values stored in outer locals may be rejected
  even when provably safe; exact false-positive patterns must be documented at
  merge time in this file
- Multi-level nested blocks are not analyzed; only the direct enclosing block
  is checked for place definition

---

### Phase 5 — Cleanup

**Goal:** Remove the `param_nonretaining` backward-compat bridge and all
transitional helpers. The model is fully migrated to `param_escape_level`.

**Pre-condition — must verify before any code removal:**
```
# Must return zero matches before removal begins:
grep -rn "param_nonretaining" lang/driftc/checker/__init__.py | grep -v "effective_param_escape_level"
grep -rn "param_nonretaining" lang/driftc/borrow_checker_pass.py
grep -rn "param_nonretaining" lang/tests/
```
If any match is found, it must be migrated or documented as intentional before
proceeding. Do not remove the field with live usages remaining.

**Changes:**

1. `lang/driftc/checker/__init__.py` (`FnSignature`):
   - Remove `param_nonretaining` field
   - Remove `effective_param_escape_level` bridge property (replace all callers
     with direct `param_escape_level` access with explicit `or EscapeLevel.THREAD`
     default)

2. `lang/driftc/borrow_checker_pass.py`:
   - Remove any remaining `param_nonretaining` references

3. Update any tests that assert on `param_nonretaining` field presence or
   value (should be zero after Phase 3b annotation sweep)

**Post-removal verification:**
```
# All must return zero:
grep -rn "param_nonretaining" lang/driftc/
grep -rn "param_nonretaining" lang/tests/
```

**Checkpoint commands:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/driver/ -q
just test-e2e
just lang-codegen-test
```

**Regression checkpoint:** Full suite passes. Zero remaining `param_nonretaining`
references in compiler and test sources.

---

## 6. Phase Dependency Graph

```
Phase 0 (types)
    │
    ▼
Phase 1 (lambda level computation)
    │
    ▼
Phase 2 (wire at call sites + THREAD diagnostics)
    │
    ▼
Phase 3a (minimal annotated set: spawn/scope/one LOCAL)
    │
    ▼
Phase 3b (full stdlib annotation sweep)
    │
    ├──► Phase 4 (scope reasoning — SCOPED lifted from LOCAL) ─┐
    │                                                           │
    └───────────────────────────────────────────────────────────┼──► Phase 5 (cleanup)
                                                                │
                                                                ▼
                                                          fully migrated
```

Phases 0–3b are sequential. Phase 4 can be developed in parallel with
validation after Phase 3b is stable (separate PR targeting the feature branch).
Phase 5 requires both Phase 3b and Phase 4 to be merged and stable.

---

## 7. Open Questions

### Q1: EscapeLevel on Fn* / Callback* trait objects — RESOLVED (MVP policy)

When a trait object of type `FnOnce` or `Callback<T>` is passed to a function,
the concrete `FnSignature` is not available at the call site, so
`param_escape_level` cannot be derived from it.

**MVP policy (mandatory, implemented in Phase 3b):**
- All trait-object / generic callback params with no explicit `param_escape_level`
  annotation are treated as `THREAD` (the conservative default).
- This is the same behavior as any other unannotated param.
- The diagnostic note "parameter has no escape-level annotation; treated as
  THREAD in MVP" applies here as well.

**Dedicated regression required (Phase 3b):**
```
lang/tests/borrow_checker/test_escape_level_model.py:
  - test_trait_object_callback_unannotated_thread_default:
      val cb: FnOnce<[], Void> = |...| => { uses_borrow }
      pass_to_generic_fn(cb)   # param unannotated → THREAD default
      → E_ESCAPE_THREAD
```
The test must assert **both**:
1. Diagnostic code is `"E_ESCAPE_THREAD"` and phase is `"borrow_check"`.
2. The notes list contains the fragment `"no escape-level annotation; treated as
   THREAD in MVP"` — this is the disambiguation signal that distinguishes an
   unannotated-default rejection from an explicitly THREAD-annotated rejection.
Both assertions are required; neither alone is sufficient.

**Future path (not in A5):** Option C from the original analysis — require the
callee's interface/trait definition to carry a `param_escape_level` annotation
that is validated at implementation sites. This is a language-surface change and
needs a separate proposal. Option A (FnThread/FnLocal trait variants) is higher
complexity and deferred further.

**Known limitation:** Trait-object callbacks cannot be used with borrowed
captures in MVP, even if the underlying implementation is provably non-escaping.
This must be documented as a known limitation in the PR description.

### Q2: Captures through heap / Arc

A lambda that captures `arc_ref: &Arc<T>` has a loan of the local Arc value,
not of the heap allocation. The heap value itself may be THREAD-safe. Should
the loan's `max_escape` reflect the heap value's lifetime?

**Current stance:** No. The loan is of the Arc pointer (local place), not the
heap. The loan's max_escape is LOCAL. The programmer should capture `arc_ref`
by MOVE (moving the Arc clone into the lambda), not by REF. The borrow checker
should guide toward this pattern with a clear error note.

### Q3: Mutable scope-local captures across spawned tasks

```drift
conc.scope(|s| => {
    var counter = 0
    s.spawn(|| => { counter = counter + 1 })
    s.spawn(|| => { counter = counter + 1 })
})
```

This is a data race regardless of escape levels. The borrow checker's existing
MUT conflict detection should reject this (two MUT loans of `counter` across
two spawn calls). Verify this is caught before Phase 4 merges.

### Q4: `param_escape_level` in user-defined functions

Should user-defined Drift functions be able to express `param_escape_level` in
their source? There is currently no syntax for this.

**Current stance:** Out of scope. Only stdlib and intrinsic signatures carry
escape-level annotations (set in checker/signature builders). User-defined
callback params are unannotated (default: THREAD — conservative).

This is a language-surface design question that requires its own proposal.

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Phase 4 scope-check is too conservative (false positives) | Medium | User frustration on valid scoped-spawn patterns | MVP rule explicitly documented (§3.6); false positives tracked as known limitations, not bugs |
| stdlib annotation mistakes (wrong escape level) | Low | Silent over-permitting or false positive rejections | All stdlib annotations require e2e test pairs (positive + negative); Phase 3a checkpoint isolates blast radius |
| Bridge property introduces divergence during migration | Low | Inconsistent behavior for mixed annotation state | Bridge is read-only; callers use `effective_param_escape_level` exclusively during migration |
| Q1 trait-object gap produces silent thread-escape | Low (MVP policy resolves this) | Resolved by THREAD-default for all unannotated params; known limitation documented in PR | Dedicated regression in Phase 3b pins the policy |
| EscapeLevel.STATIC requirements clash with current reactor patterns | Medium | Existing stdlib usage rejected at Phase 3b | Audit reactor/registry usage in stdlib + test suite before Phase 3b lands; track all rejections |
| ref-field / Result::Ok binder paths destabilized by borrow-checker changes | Medium | Silent regression in high-sensitivity areas (recent F1/F2 fixes) | Explicitly included in every phase checkpoint's validation subset (see §11) |

---

## 9. Validation Subset (run at every phase checkpoint)

The following tests must pass at every phase checkpoint regardless of whether
the phase directly touches their areas. They cover high-sensitivity paths that
interact with borrow-checker contracts.

**Borrow checker unit tests:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q
```

Specifically:
- `lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py`
- `lang/tests/borrow_checker/test_lambda_capture_borrow_overlap.py`
- `lang/tests/borrow_checker/test_lambda_capture_borrow_overlap_method.py`

**Driver / boundary contract tests:**
```
PYTHONPATH=. ./.venv/bin/python3 -m pytest \
    lang/tests/driver/test_boundary_matrix_result_variant_contract.py \
    lang/tests/driver/test_struct_ref_field_boundary_contract.py \
    -q
```

**ref-field and Result::Ok binder e2e (high-sensitivity):**
```
PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j4 \
    result_ok_move_conn_source_drop_regression \
    struct_ref_field_result_ok_move_drop_once
```

**New A5 thread/scope/static boundary cases (added as they land):**
```
PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j4 \
    borrow_escape_spawn_rejected \
    borrow_escape_static_rejected \
    borrow_escape_scope_accepted
```

**Full suite (Phase 5 only):**
```
just test-e2e
just lang-codegen-test
```

---

## 10. Review Checklist (for branch PR)

Before the branch is merged to main, the following must be true:

- [ ] All `just test-e2e` cases pass
- [ ] All `just lang-codegen-test` cases pass
- [ ] All borrow checker unit tests pass
- [ ] `test_invoke_optional_ref_and_lambda_escape.py` passes (may need message
      string updates; not removal)
- [ ] `test_lambda_capture_borrow_overlap.py` passes
- [ ] `test_lambda_capture_borrow_overlap_method.py` passes
- [ ] `test_boundary_matrix_result_variant_contract.py` passes (ref-field sensitivity)
- [ ] `test_struct_ref_field_boundary_contract.py` passes (ref-field sensitivity)
- [ ] `result_ok_move_conn_source_drop_regression` e2e passes
- [ ] `struct_ref_field_result_ok_move_drop_once` e2e passes
- [ ] New E_ESCAPE_* diagnostic codes present in `test_escape_level_model.py`
      with positive and negative cases for each level
- [ ] E2e pair (accept + reject) exists for THREAD boundary
- [ ] E2e pair (accept + reject) exists for SCOPE boundary (Phase 4)
- [ ] E2e for STATIC rejection
- [ ] Q1 trait-object THREAD-default regression present in `test_escape_level_model.py`
- [ ] `param_nonretaining` removed from `FnSignature` (Phase 5 complete)
- [ ] Post-removal grep confirms zero `param_nonretaining` references in compiler and tests
- [ ] Q1–Q4 disposition recorded in this document (Q1 resolved; Q2–Q4 stances confirmed)
- [ ] Phase 4 known limitations (conservative MVP false positives) documented
- [ ] No `"internal:"` strings in new diagnostics
- [ ] All new diagnostic spans are non-None
- [ ] Unannotated-param THREAD-default note present in E_ESCAPE_THREAD diagnostic

---

## 11. Suggested Branch & Review Process

**Branch name:** `feat/escape-context-model`
**Base:** `main` at the commit that merges A1–A4

**Review gates:**
- Phase 0–1: self-reviewed by implementer, no external review required
- Phase 2: Klaudia review — focus on call-site wiring and diagnostic messages
- Phase 3a: Klaudia review — first real stdlib annotations, diagnostic stability checkpoint
- Phase 3b: Klaudia + owner review — full stdlib sweep, load-bearing contracts; STATIC audit required before merge
- Phase 4: Klaudia review — scope lifetime reasoning (conservative MVP algorithm and its known limitations)
- Phase 5: owner sign-off — removes a public field from FnSignature; grep verification required

**Merge strategy:** squash-per-phase into one PR per phase, all targeting the
feature branch. Feature branch merges to main via a single reviewed PR after
Phase 5 passes the review checklist above.
