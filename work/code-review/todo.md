# Drift Toolchain — Code Review Findings & Action Plan

Reviewer: Klaudia
Date: 2026-02-19
Scope: `hir_to_mir.py`, `llvm_codegen.py`, `checker/__init__.py`,
`borrow_checker_pass.py`, `borrow_checker.py`, `driftc.py`,
`call_contract.py`, `mir_validate.py`

---

## Finding Summary

| ID  | Severity | File(s)                           | Area                                         | Status |
|-----|----------|-----------------------------------|----------------------------------------------|--------|
| F1  | CRITICAL | `stage2/hir_to_mir.py` ~L884      | Scrutinee/payload drop ordering — current behavior is correct but undertested | open |
| F2  | HIGH     | `llvm_codegen.py` ~L7443          | DiagnosticValue drop emits alloca outside entry block | open |
| F3  | HIGH     | `borrow_checker_pass.py` ~L1392   | `Optional<&T>` loan tracking misses HInvoke/captures — **merged with F7** | open |
| F4  | HIGH     | `driftc.py` ~L753                 | Intrinsic validation uses `AssertionError`, not Diagnostic | open |
| F5  | HIGH     | `stage2/hir_to_mir.py` (scattered)| Inconsistent copy predicates (~12 sites)     | open |
| F6  | MEDIUM   | `mir_validate.py`                 | No validation of `VariantGetField` operand bounds | open |
| F7  | MEDIUM   | `borrow_checker_pass.py` ~L293    | Lambda `&mut` captures escaping into `spawn` not rejected — **merged into F3 task** | open |
| F8  | MEDIUM   | `mir_validate.py`                 | No SSA operand existence check (undefined names reach codegen) | open |
| F9  | MEDIUM   | `llvm_codegen.py` ~L7618          | Variant layout computed twice (drop helper vs `_variant_layout`) | open |
| F10 | LOW      | `stage2/hir_to_mir.py` ~L750      | Match constructors validated after block creation begins — **lower urgency; checker primary** | open |
| F11 | MEDIUM   | `call_contract.py` ~L12           | `CallContractIssue` has no `span` field      | open |
| F12 | LOW      | `llvm_codegen.py` ~L5826, L6020   | Forward nominal resolution implemented twice | open |
| F13 | LOW      | `checker/__init__.py` ~L497       | `"internal:"` strings reach user diagnostics | open |
| F14 | LOW      | `checker/__init__.py` ~L1094      | `span=None` on several `report_*` helpers    | open |
| F15 | LOW      | `llvm_codegen.py` (scattered)     | Bool i1↔i8 coercion at 5+ sites without helper | open |

---

## Detailed Findings

### F1 — Payload/Scrutinee Drop Ordering — Correct but Undertested (CRITICAL)

**Root cause:** `hir_to_mir.py` — match arm binder extraction (approx. lines 878–915)

**Finding update (owner feedback):** The current unconditional `arm_scrut_payload_moved = True`
behavior is *correct* — it is what fixed the most recent regressions. My original
proposed fix direction (restrict the flag to the Copy branch) was **inverted** and
would regress those fixes. **No code change planned.**

The risk remains real: the flag controls early-drop of the scrutinee vs. survival
until arm exit. The area is fragile enough that adding targeted regressions pinning
both the correct and incorrect shapes is the right action.

**Shapes that must be covered by regressions:**
- Non-Copy payload binder: `Result::Ok(conn: TcpStream)` / borrowed aggregate —
  scrutinee must survive arm body; no early drop.
- Copy-with-drop payload: `Result::Ok(s: CopyButDroppable)` — CopyValue must
  be emitted; original scrutinee must be dropped correctly.
- No-double-drop invariant: dropping the scrutinee exactly once regardless of
  arm count.
- No-early-drop invariant: payload reference must be valid throughout the arm body.

---

### F2 — DiagnosticValue Drop: `alloca` Outside Entry Block (HIGH)

**Root cause:** `llvm_codegen.py::_emit_drop_value()` — the `TypeKind.DIAGNOSTICVALUE` path

LLVM requires all `alloca` instructions to be in the function entry block so they
dominate all uses. The DV drop path emits a bare inline `alloca` at the current
insertion point. If a `DiagnosticValue` is dropped inside a loop, branch, or match
arm, the generated IR is structurally invalid and may fail LLVM verification (or
silently miscompile with optimisation passes).

Array and variant drop paths avoid this by using `_ensure_array_drop_helper()`,
which emits the helper once at module scope. DiagnosticValue needs the same
treatment: a dedicated `_ensure_dv_drop_helper()` or entry-block pre-allocation
via the existing `_ensure_local_storage()` pattern.

**Agreed plan:** implement `_ensure_dv_drop_helper()` matching the array/variant
pattern; add loop/branch regression to catch dominance violations.

---

### F3 — `Optional<&T>` Loan Tracking Incomplete + Lambda Escape (HIGH) ← merged with F7

**Root cause:** `borrow_checker_pass.py::_borrow_from_optional_ref_call()` ~L1392–1439
and `_add_lambda_capture_loans()` ~L293–308

Two related gaps in escape/borrow tracking, addressed as one task:

**Optional<&T> coverage (F3):** `_borrow_from_optional_ref_call` handles `HCall`
and `HMethodCall` but not:
- `HInvoke` (function pointer / callback calls returning `Optional<&T>`)
- Nested access chains: `iter.next().some_field`
- Lambda captures holding `Optional<&mut T>` that escape into `spawn()`

Consequence: iterator-based patterns (`HashMap::iter`, array range iterators)
can produce unchecked borrow conflicts that compile silently.

**Lambda capture escape (F7):** Loans from `_add_lambda_capture_loans` are
registered with `temporary=True` (dies after enclosing expression), but a lambda
passed to `conc.spawn()` or stored in a struct escapes that scope. No
cross-check of capture escape context against loan lifetime → a `&mut T`
borrowed into a spawned virtual thread is not caught.

**Agreed plan:** extend `_borrow_from_optional_ref_call` to cover `HInvoke`;
add escape-context awareness to lambda capture loan registration; add regression
tests for iterator and callback borrow patterns.

---

### F4 — Intrinsic Validation: `AssertionError` Instead of Diagnostic (HIGH)

**Root cause:** `driftc.py` intrinsic validation block ~L753–870

The ~120-line block that checks intrinsic call arity and argument shapes raises
bare `AssertionError`. User code with a wrong intrinsic invocation gets a Python
traceback instead of a compiler `Diagnostic` with source span, phase tag, and
error code. The `Diagnostic` infrastructure already supports all required fields.

---

### F5 — Inconsistent Copy Predicates Across Stage2 (HIGH)

**Root cause:** `hir_to_mir.py` — ~12 scattered call sites

Two independent systems coexist:
- `_type_table.is_copy(ty)` → `bool`
- `_type_table.copy_status(ty)` → ternary (`True` / `False` / `None`)

The compound used at line ~878 (`is_copy(bty) and not _needs_runtime_drop(bty)`)
is unique to that site; elsewhere `copy_status(ty) is True` is used. A type could
satisfy one predicate but not the other, causing inconsistent copy/move decisions.

Additionally, `is_bitcopy()` appears only at line ~3427 with no documentation; it
is unclear whether this is load-bearing or a stale artifact.

---

### F6 — No `VariantGetField` Bounds Validation in `mir_validate.py` (MEDIUM)

**Root cause:** `mir_validate.py` — missing validator function

The 8 existing validators do not check `VariantGetField` / `VariantGetFieldAddr`
for:
- Valid arm name (constructor exists in the variant definition)
- Valid field index (index < arm.field_count)
- Consistent `variant_ty` and `field_ty` with the schema

Stage2 can emit an out-of-bounds field index if `binder_field_indices` is wrong.
Without this check the error surfaces as a codegen assertion or silent LLVM type
mismatch, far from the actual source.

---

### F7 — Lambda `&mut` Capture Escape into `spawn` Not Rejected (MEDIUM)

**Merged into F3.** See F3 for full description and agreed plan.
Root cause: `borrow_checker_pass.py::_add_lambda_capture_loans()` ~L293–308.

---

### F8 — No SSA Operand Existence Check in `mir_validate.py` (MEDIUM)

**Root cause:** `mir_validate.py` — missing hygiene pass

None of the 8 validators verifies that `DropValue(value=v)` or
`MoveOut(dest=d, local=l)` reference SSA names / locals actually defined in the
function. A stage2 bug that emits a drop of a non-existent temp silently passes
validation and generates corrupt LLVM IR.

---

### F9 — Variant Layout Computed Twice (MEDIUM)

**Root cause:** `llvm_codegen.py` — `_variant_layout()` (~L5814) and the drop helper
emit closure (~L7618)

Field offsets and alignment are computed once in `_variant_layout()` for the LLVM
type declaration, then re-implemented inline inside the drop helper without calling
`_variant_layout()`. If variant layout semantics change (e.g. tombstone tag width,
alignment rules), both sites must be updated independently.

---

### F10 — Match Constructor Validated After Block Creation (LOW — lower urgency)

**Root cause:** `hir_to_mir.py` ~L750–753

The assertion `arm_def = inst.arms_by_name.get(arm.ctor)` fires during dispatch
block generation, after blocks for preceding arms may already have been emitted.
A bad constructor name (from a recent variant rename that the checker didn't catch)
leaves a partially-constructed CFG before the error is raised.

**Agreed note:** The checker should be the primary catch point for unknown
constructors; stage2 pre-validation is a secondary safety net. Cleaner to fix
but not urgent — schedule after critical items (F1–F6, F8).

---

### F11 — `CallContractIssue` Has No `span` Field (MEDIUM)

**Root cause:** `call_contract.py` `CallContractIssue` dataclass ~L12–15

All 5 contract checks produce `CallContractIssue` objects with `code`, `message`,
and `notes` but no source span. Callers must reconstruct location from surrounding
context; if that context is lost the diagnostic is unactionable for users.

---

### F12 — Forward Nominal Resolution Duplicated in Codegen (LOW)

**Root cause:** `llvm_codegen.py` ~L5826–5861 and ~L6020–6063

The same loop-with-cycle-detection for resolving `ForwardNominal` type aliases is
implemented twice in two different layout helpers. Any future fix (e.g. increasing
alias depth limit) must be applied to both.

---

### F13 — `"internal:"` Strings in User-Facing Diagnostics (LOW)

**Root cause:** `checker/__init__.py` ~L497–522

Several diagnostic messages contain `"internal: signature for … (checker bug)"`.
If these paths are reachable via malformed input, users see confusing internal-error
strings. They should use opaque error codes or be guarded to non-user output.

---

### F14 — Missing Source Spans on Checker Diagnostic Helpers (LOW)

**Root cause:** `checker/__init__.py` ~L1094–1138

Several `report_*` helpers (including `report_index_not_int`) emit with
`span=None`. Users cannot navigate to the offending line in IDEs.

---

### F15 — Bool i1↔i8 Coercion Scattered Across Codegen (LOW)

**Root cause:** `llvm_codegen.py` — multiple sites (~L2791, L5910, L6003, L7291, etc.)

Bool storage coercion between `i1` (SSA representation) and `i8`
(struct/array storage) appears at 5+ independent sites. No central helper exists,
so a future change to Bool storage representation requires touching all of them.

---

## Itemized Action Plan

All bug fixes follow the LANGUAGE_BUG protocol:
**regression test first → fix → confirm positive + negative pass.**

---

### Item 1 — Regressions for F1: Payload/Scrutinee Drop Ordering
**Priority: CRITICAL | Files: tests only — no code change**

The current behavior is correct. Action is regressions that pin it:

- [ ] Positive: `Result::Ok(conn: TcpStream)` match binder — use `conn` throughout
      arm body, assert no early-free (valgrind / ASAN clean).
- [ ] Positive: borrowed aggregate in `Ok` payload — field reference valid in arm.
- [ ] Positive: Copy+drop payload shape (`Result::Ok(s: CopyDroppable)`) —
      `CopyValue` emitted, original scrutinee dropped exactly once.
- [ ] Negative: double-drop detection — scrutinee drop count == 1 across all arms.
- [ ] No-early-drop assertion: payload reference must be valid at arm exit point.
- [ ] Confirm existing `*result*` and `*optional*` e2e suite still passes after
      adding new cases.

---

### Item 2 — Fix F2: DiagnosticValue Drop Alloca
**Priority: HIGH | Files: `llvm_codegen.py`**

- [ ] Add failing test: function that drops a `DiagnosticValue` inside a `while`
      loop or `if` branch.
- [ ] Fix: emit a `_ensure_dv_drop_helper()` (module-scope helper), mirroring
      the array/variant pattern, OR pre-allocate an entry-block slot for the DV
      argument via `_ensure_local_storage()`.
- [ ] Optionally run `opt --verify` on the generated IR to confirm no alloca
      dominance violations.

---

### Item 3 — Fix F3+F7: Borrow Checker Escape Analysis (merged)
**Priority: HIGH | Files: `borrow_checker_pass.py`**

- [ ] Extend `_borrow_from_optional_ref_call` to handle `HInvoke` in addition to
      `HCall`/`HMethodCall`.
- [ ] Add escape-context awareness to `_add_lambda_capture_loans`: if the lambda
      is passed to `conc.spawn()` or a known escaping position, mark the capture
      loan as non-temporary (lifetime extends beyond the expression scope).
- [ ] Add regressions:
  - Iterator borrow via `HInvoke` path does not silently escape.
  - Callback/closure capturing `&mut T` passed to `spawn` is rejected.
  - Callback capturing `&T` (read-only) passed to `spawn` is accepted.
  - Nested `iter.next().field` access chain is tracked correctly.
- [ ] Confirm existing borrow checker tests still pass.

---

### Item 4 — Fix F4: Intrinsic Validation → Structured Diagnostics
**Priority: HIGH | Files: `driftc.py`**

- [ ] Add driver-level regression: wrong-arity `swap` call should produce a
      `Diagnostic` with `severity="error"`, `phase="typecheck"`, and a non-None
      `span`.
- [ ] Convert the `~L753–870` intrinsic validation block from `AssertionError`
      to `Diagnostic` emission.
- [ ] Assign stable error codes to each intrinsic arity / argument shape error.

---

### Item 5 — Fix F5: Centralize Copy Predicate in Stage2
**Priority: HIGH | Files: `hir_to_mir.py`**

- [ ] Extract `_should_copy_value(ty: TypeId) -> bool` in `hir_to_mir.py` that
      normalises `is_copy()` vs `copy_status()` into a single canonical check.
- [ ] Replace all ~12 scattered sites one at a time with tests passing at each step.
- [ ] Investigate and document (or remove) the `is_bitcopy()` check at ~L3427.

---

### Item 6 — Add F6: `VariantGetField` Validator in `mir_validate.py`
**Priority: MEDIUM | Files: `mir_validate.py`**

- [ ] Implement `validate_mir_variant_field_invariants(funcs, type_table)`:
  - arm name must exist in the variant's arm schema
  - field index must be < arm.field_count
  - `field_ty` must match the arm schema field type
- [ ] Add positive test: well-formed `VariantGetField` passes validation.
- [ ] Add negative test: out-of-bounds field index fails validation with a clear
      non-internal diagnostic.

---

### Item 7 — Add F8: SSA Operand Hygiene Pass in `mir_validate.py`
**Priority: MEDIUM | Files: `mir_validate.py`**

- [ ] Implement `validate_mir_basic_hygiene(funcs)`:
  - build def-set per function (all SSA names emitted as destinations)
  - verify every instruction operand (value, local) is in that set
  - report which instruction and which operand is undefined
- [ ] Add negative test: `DropValue` with undefined value name fails hygiene check.

---

### Item 8 — Fix F11: Add `span` to `CallContractIssue`
**Priority: MEDIUM | Files: `call_contract.py`, callers**

- [ ] Add `span: Optional[Any] = None` field to the `CallContractIssue` dataclass.
- [ ] Update all 5 contract checks to forward the originating expression's span.
- [ ] Update callers (checker, stage2, driver) to attach the span to emitted
      diagnostics.
- [ ] Add regression: a call contract violation diagnostic must include a
      non-None source span.

---

### Item 9 — Fix F9: Single Source of Truth for Variant Layout
**Priority: MEDIUM | Files: `llvm_codegen.py`**

- [ ] Refactor the drop helper emit closure to call `_variant_layout()` rather
      than re-implementing alignment/offset arithmetic.
- [ ] Add test: variant with multi-field arm; drop inside a branch; verify no
      runtime crash.

---

### Item 10 — Fix F10: Pre-Validate Match Constructors Before Block Creation
**Priority: LOW (after critical items) | Files: `hir_to_mir.py`**

Checker is the primary owner of unknown-constructor detection; this is a
secondary safety net for cases the checker misses.

- [ ] Move `arm_def = inst.arms_by_name.get(arm.ctor)` lookup to a pre-pass
      loop over all arms before any block is created.
- [ ] On first bad constructor raise immediately, before any CFG mutation.
- [ ] Add driver-level negative test: unknown constructor in match → clear
      checker diagnostic, no partial CFG.

---

### Item 11 — Fix F12: Deduplicate Forward Nominal Resolution in Codegen
**Priority: LOW | Files: `llvm_codegen.py`**

- [ ] Extract `_resolve_forward_nominal_chain(ty_id)` as a single method.
- [ ] Replace both duplicated implementations (~L5826 and ~L6020) with calls
      to the new method.

---

### Item 12 — Fix F13/F14: Diagnostic String and Span Quality in Checker
**Priority: LOW | Files: `checker/__init__.py`**

- [ ] Audit all `"internal:"` diagnostic message strings; replace with opaque
      codes (e.g. `"E_INTERNAL_MISSING_TYPEID"`) or suppress to logging only.
- [ ] Audit all `report_*` helpers emitting `span=None`; thread the expression
      `loc` through the typing context and attach it.

---

### Item 13 — Fix F15: Centralise Bool i1↔i8 Coercion in Codegen
**Priority: LOW | Files: `llvm_codegen.py`**

- [ ] Add `_coerce_bool_storage(value: str, from_ty: str, to_ty: str) -> str`.
- [ ] Replace all 5+ scattered coercion sites with calls to the new helper.
- [ ] Add a comment documenting the invariant: `i1` = SSA register representation,
      `i8` = struct/array storage representation.

---

## Architectural Recommendations (Separate Effort)

These are larger refactors that should be discussed before scheduling:

**A1 — Expand `call_contract.py` to be the true single validation seam.**
Intrinsic arity checks (driftc.py), CallInfo repair (hir_to_mir.py), and
param-type completeness checks (stage2) should move here as parameterised helpers.
Callers across driver, stage2, and borrow checker call one module, not their own
re-implementations.

**A2 — Centralise `has_drop(ty)` in `TypeTable`.**
`_type_needs_drop()` is implemented independently in codegen, stage2, and checker.
One canonical `TypeTable.has_drop(ty) -> bool` should replace all three.

**A3 — Add stage attribution to `mir_validate.py` failures.**
All 8 validators raise `AssertionError` with no stage tag. Adding a `phase: str`
parameter to each and prefixing messages with `[{phase}] MIR invariant:` makes
it immediately clear which compilation stage produced the bad MIR.

**A4 — Extract `_ensure_interface_drop_helper()` in codegen.**
The 60-line inline interface drop in `_emit_drop_value` should be a separate
module-scope helper matching the array/variant pattern, making it independently
testable.

**A5 — Borrow checker: extend escape analysis to `spawn` / callback boundaries.**
The fix for F3 and F7 will be easier once there is a defined notion of "escape
context" (local scope vs. global/thread boundary) threaded through the borrow
checker pass. This is a non-trivial design change and should be planned separately.

---

## Test Files Referenced

Existing coverage to preserve:
- `lang/tests/codegen/e2e/*result*`
- `lang/tests/codegen/e2e/*struct_ref_field*`
- `lang/tests/driver/test_boundary_matrix_result_variant_contract.py`
- `lang/tests/driver/test_codegen_boundary_diagnostics.py`
- `lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py`
- `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py`

New tests to add (one per item above):
- `test_result_ok_noncopy_binder_drop_ordering` — Item 1 / F1 (regressions only, no code change)
- `test_diagnostic_value_drop_in_loop` — Item 2 / F2
- `test_borrow_checker_optional_ref_hinvoke` — Item 3 / F3
- `test_borrow_checker_lambda_mut_capture_spawn_rejected` — Item 3 / F7
- `test_intrinsic_wrong_arity_diagnostic_has_span` — Item 4 / F4
- `test_mir_validate_variant_field_bounds` — Item 6 / F6
- `test_mir_validate_ssa_operand_existence` — Item 7 / F8
- `test_call_contract_issue_has_span` — Item 8 / F11
- `test_variant_drop_helper_single_source_layout` — Item 9 / F9
