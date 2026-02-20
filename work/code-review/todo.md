# Drift Toolchain — Code Review Findings & Action Plan

Reviewer: Klaudia
Date: 2026-02-19 (verified 2026-02-20, re-verified 2026-02-20)
Scope: `hir_to_mir.py`, `llvm_codegen.py`, `checker/__init__.py`,
`borrow_checker_pass.py`, `borrow_checker.py`, `driftc.py`,
`call_contract.py`, `mir_validate.py`

---

## Finding Summary

| ID  | Severity | File(s)                           | Area                                         | Status |
|-----|----------|-----------------------------------|----------------------------------------------|--------|
| F1  | CRITICAL | `stage2/hir_to_mir.py` ~L884      | Scrutinee/payload drop ordering — current behavior correct, regressions needed | **done** ✓ |
| F2  | HIGH     | `llvm_codegen.py` L7648           | DiagnosticValue drop emits alloca outside entry block | **done** ✓ |
| F3  | HIGH     | `borrow_checker_pass.py` L1460    | `Optional<&T>` loan tracking misses HInvoke/captures — merged with F7 | **done** ✓ |
| F4  | HIGH     | `driftc.py` L740–956              | Intrinsic validation uses `AssertionError`, not Diagnostic | **done** ✓ |
| F5  | HIGH     | `stage2/hir_to_mir.py` L396       | Inconsistent copy predicates (~12 sites)     | **done** ✓ |
| F6  | MEDIUM   | `mir_validate.py` L13             | No validation of `VariantGetField` operand bounds | **done** ✓ |
| F7  | MEDIUM   | `borrow_checker_pass.py` L323     | Lambda `&mut` captures escaping into `spawn` not rejected — merged into F3 | **done** ✓ |
| F8  | MEDIUM   | `mir_validate.py` L50             | No SSA operand existence check (undefined names reach codegen) | **done** ✓ |
| F9  | MEDIUM   | `llvm_codegen.py` L7544           | Variant layout computed twice (drop helper vs `_variant_layout`) | **done** ✓ |
| F10 | LOW      | `stage2/hir_to_mir.py` L796       | Match constructors validated after block creation begins | **done** ✓ |
| F11 | MEDIUM   | `call_contract.py` L17            | `CallContractIssue` has no `span` field      | **done** ✓ |
| F12 | LOW      | `llvm_codegen.py` L5892, L5931    | Forward nominal resolution implemented twice | **done** ✓ |
| F13 | LOW      | `checker/__init__.py` / `call_contract.py` | `"internal:"` strings reach user diagnostics | **done** ✓ |
| F14 | LOW      | `checker/__init__.py` L1094       | `span=None` on several `report_*` helpers    | **done** ✓ |
| F15 | LOW      | `llvm_codegen.py` L8067           | Bool i1↔i8 coercion at 5+ sites without helper | **done** ✓ |

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

**Verified (2026-02-20):** All five regression shapes confirmed covered:

- **Non-Copy binder / no-early-drop:**
  `lang/tests/codegen/e2e/result_ok_noncopy_binder_drop_ordering/main.drift` —
  defines `Conn { session: Session }` with a `Destructible` impl that instruments
  `alive_flag` and `drop_count`. Matches on `Result::Ok(v: Conn)`, asserts
  `alive == true` and `drops == 0` before arm exit (proving no early drop).
  `expected.json` confirms `exit_code: 0`.

- **Borrowed aggregate fields in Ok payload:**
  Also covered by `result_ok_noncopy_binder_drop_ordering` — `Session` carries
  `&mut Bool` and `&mut Int` ref fields; field accesses in arm body remain valid.

- **Copy+drop payload / CopyValue emission / drop-count-once:**
  `lang/tests/stage2/test_hir_to_mir_match_copy_payload_drop_once.py` —
  stage2 unit test. Constructs a `V<Int>` variant with `Some(value: Int, msg: String)`
  arm; asserts `CopyValue` instruction is emitted for the `Int` field extraction
  (line 70) and `total_variant_drops == 1` across the match CFG (line 79), with
  per-arm max of 1 and dispatch block having zero drops (lines 75–78).

- **Double-drop detection:**
  Additionally pinned by
  `lang/tests/codegen/e2e/result_ok_array_match_move_no_double_free` (Array payload
  variant) and the stage2 unit test above.

**Residual note:** Coverage is per-shape, not exhaustive over all arm-count
combinations. Multi-arm matches with mixed Copy/non-Copy arms in the same variant
are not currently tested. If `arm_scrut_payload_moved` logic is revisited, add
a 2-arm mixed-payload regression at that time.

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

**Verified (2026-02-20):** `_ensure_dv_drop_helper()` confirmed at `llvm_codegen.py` L7648;
drop path in `_emit_drop_value` calls it at L7371. No inline alloca remains for the DV path.
Test file `lang/codegen/llvm/tests/test_llvm_codegen_dv_drop_helper.py` confirmed present.

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

**Verified (2026-02-20):** `_borrow_from_optional_ref_call()` now branches on `H.HInvoke`
at `borrow_checker_pass.py` L1460; chain-peeling for `HField`/`HIndex`/`HPlaceExpr`
at L1463–1471. `HInvoke` coverage also added in `_ref_binding_ids_in_expr()` (L1231)
and `_collect_ref_uses_in_expr()` (L1558). Lambda escape: `_lambda_has_borrow_capture()`
at L323; `_report_lambda_escape_if_borrowed()` at L330; wired at HCall/HInvoke arg and
kwarg sites (L1865, L1883, L1911, L1926). Test file
`lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py` confirmed present.

---

### F4 — Intrinsic Validation: `AssertionError` Instead of Diagnostic (HIGH)

**Root cause:** `driftc.py` intrinsic validation block ~L753–870

The ~120-line block that checks intrinsic call arity and argument shapes raises
bare `AssertionError`. User code with a wrong intrinsic invocation gets a Python
traceback instead of a compiler `Diagnostic` with source span, phase tag, and
error code. The `Diagnostic` infrastructure already supports all required fields.

**Verified (2026-02-20):** `_intrinsic_contract_diag()` helper confirmed at `driftc.py`
L740–754; full `_validate_intrinsic_callinfo()` at L757–956. No bare `AssertionError`
remains in the intrinsic validation block. Structured `Diagnostic` objects emitted
with `severity="error"` and `phase="typecheck"`. Test file
`lang/tests/driver/test_intrinsic_callinfo_diagnostics.py` confirmed present.

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

**Verified (2026-02-20):** `_should_copy_value(ty)` confirmed at `hir_to_mir.py` L396.
13 call sites verified, including the match binder path (L843, L848, L886, L931).
The `is_bitcopy()` check at L3487 is now inside the `_should_copy_value` consolidation
context; its role is addressed there.

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

**Verified (2026-02-20):** `validate_mir_variant_field_invariants(funcs, type_table)`
confirmed at `mir_validate.py` L13. Wired into the driver at `driftc.py` L5226.
Test file `lang/tests/stage2/test_mir_validate_variant_and_hygiene.py` confirmed present.

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

**Verified (2026-02-20):** `validate_mir_basic_hygiene(funcs)` confirmed at
`mir_validate.py` L50. Wired into the driver at `driftc.py` L5222.
Test file `lang/tests/stage2/test_mir_validate_variant_and_hygiene.py` confirmed present.

---

### F9 — Variant Layout Computed Twice (MEDIUM)

**Root cause:** `llvm_codegen.py` — `_variant_layout()` (~L5814) and the drop helper
emit closure (~L7618)

Field offsets and alignment are computed once in `_variant_layout()` for the LLVM
type declaration, then re-implemented inline inside the drop helper without calling
`_variant_layout()`. If variant layout semantics change (e.g. tombstone tag width,
alignment rules), both sites must be updated independently.

**Verified (2026-02-20):** Drop helper emit closure at `llvm_codegen.py` L7544 now
calls `_variant_layout()` rather than re-implementing alignment/offset arithmetic.
Single source of truth confirmed.

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

**Verified (2026-02-20):** Constructor pre-validation now runs before arm block
allocation in `hir_to_mir.py`; `arm_blocks` creation was moved after the pre-check.
Regression test added: `lang/tests/stage2/test_hir_to_mir_match_ctor_prevalidate.py`,
asserting unknown constructors raise early and no `match_arm_*` blocks are created.

**Re-verified (2026-02-20) — concrete evidence:**
- Pre-validation loop: `hir_to_mir.py` L774–778 iterates all arms, calls
  `inst.arms_by_name.get(arm.ctor)`, raises `AssertionError` on first unknown name.
- `arm_blocks` list created at L779 — **after** the validation loop.
- The test (`test_match_unknown_ctor_prevalidated_before_arm_blocks`) passes "Bogus"
  as the constructor name, catches `AssertionError`, then asserts
  `not any(name.startswith("match_arm_") for name in builder.func.blocks.keys())`.
  This is a direct proof that no arm blocks are created before the error fires.
- **Residual scope note:** This validation guards against unknown constructor names.
  It does NOT catch a valid constructor name with a wrong field count or field type
  mismatch — those remain the checker's responsibility (and are caught downstream
  by the F6 `validate_mir_variant_field_invariants` validator if they survive to MIR).

---

### F11 — `CallContractIssue` Has No `span` Field (MEDIUM)

**Root cause:** `call_contract.py` `CallContractIssue` dataclass ~L12–15

All 5 contract checks produce `CallContractIssue` objects with `code`, `message`,
and `notes` but no source span. Callers must reconstruct location from surrounding
context; if that context is lost the diagnostic is unactionable for users.

**Verified (2026-02-20):** `CallContractIssue` dataclass now has
`span: Span = field(default_factory=Span)` at `call_contract.py` L17.
All 5 contract checks confirmed to use `span=getattr(expr, "loc", Span())`.
Test file `lang/tests/driver/test_intrinsic_callinfo_diagnostics.py` provides
span-bearing diagnostic coverage.

---

### F12 — Forward Nominal Resolution Duplicated in Codegen (LOW)

**Root cause:** `llvm_codegen.py` ~L5826–5861 and ~L6020–6063

The same loop-with-cycle-detection for resolving `ForwardNominal` type aliases is
implemented twice in two different layout helpers. Any future fix (e.g. increasing
alias depth limit) must be applied to both.

**Verified (2026-02-20):** `_resolve_forward_nominal_typeid()` confirmed at
`llvm_codegen.py` L5892; `_canonical_codegen_typeid()` at L5931.
Both `_variant_layout()` (L5824) and `_llvm_type_for_typeid()` (L6024) now call
`_canonical_codegen_typeid()`. The duplicate resolution loops have been replaced.

---

### F13 — `"internal:"` Strings in User-Facing Diagnostics (LOW)

**Root cause:** `checker/__init__.py` ~L497–522

Several diagnostic messages contain `"internal: signature for … (checker bug)"`.
If these paths are reachable via malformed input, users see confusing internal-error
strings. They should use opaque error codes or be guarded to non-user output.

**Verified (2026-02-20):** `checker/__init__.py` and `call_contract.py` no longer
emit `"internal:"` prefixes in user diagnostics. `call_contract.py` messages were
renamed (L70, L82, L91, L101, L116), and driver contract tests were updated in
`lang/tests/driver/test_callinfo_param_layout_contract.py`.

**Re-verified (2026-02-20) — concrete evidence:**
- Grep for `"internal:"` in `call_contract.py` returns zero matches.
- Current messages at L70, L82, L91, L101, L116 are respectively:
  - `"invoke CallInfo target must be INDIRECT (checker bug)"`
  - `"method call CallInfo target must not be CONSTRUCTOR (checker bug)"`
  - `"invoke CallInfo must not set includes_callee (checker bug)"`
  - `"CallInfo includes_callee set on {call_kind} (checker bug)"`
  - `"CallInfo param layout mismatch for {call_kind} (checker bug)"`
- `test_callinfo_param_layout_contract.py` assertions match these exact strings
  (e.g. L45: `"CallInfo param layout mismatch for method call (checker bug)"`).
  No `"internal:"` in any assertion.
- **Design note:** Messages retain `"(checker bug)"` suffix, which is intentional —
  these contract violations indicate a compiler-internal invariant failure, not a
  user code error. The suffix is appropriate and more informative than a bare
  opaque code. Message strings are now part of the test contract; renames require
  test updates.

---

### F14 — Missing Source Spans on Checker Diagnostic Helpers (LOW)

**Root cause:** `checker/__init__.py` ~L1094–1138

Several `report_*` helpers (including `report_index_not_int`) emit with
`span=None`. Users cannot navigate to the offending line in IDEs.

**Verified (2026-02-20):** `report_index_not_int` now accepts `span: object | None = None`
and passes `Span.from_loc(span)` to the diagnostic. Call sites confirmed to forward
`getattr(expr, "loc", None)` or `getattr(expr.index, "loc", None)`. Test file
`lang/tests/driver/test_index_diagnostics_spans.py` confirmed present.

---

### F15 — Bool i1↔i8 Coercion Scattered Across Codegen (LOW)

**Root cause:** `llvm_codegen.py` — multiple sites (~L2791, L5910, L6003, L7291, etc.)

Bool storage coercion between `i1` (SSA representation) and `i8`
(struct/array storage) appears at 5+ independent sites. No central helper exists,
so a future change to Bool storage representation requires touching all of them.

**Verified (2026-02-20):** `_is_bool_storage_pair()` confirmed at `llvm_codegen.py`
L8067. 15 call sites found using it (verified via grep). The scattered inline i1↔i8
patterns have been replaced by calls to this helper.

---

## Itemized Action Plan

All bug fixes follow the LANGUAGE_BUG protocol:
**regression test first → fix → confirm positive + negative pass.**

---

### Item 1 — Regressions for F1: Payload/Scrutinee Drop Ordering
**Priority: CRITICAL | Files: tests only — no code change** — **DONE ✓**

The current behavior is correct. Action is regressions that pin it:

- [x] Positive: `Result::Ok(conn: TcpStream)` match binder — pinned by
      `lang/tests/codegen/e2e/result_ok_noncopy_binder_drop_ordering` (ASAN +
      memcheck run clean).
- [x] Positive: borrowed aggregate in `Ok` payload — pinned by
      `lang/tests/codegen/e2e/struct_ref_field_result_ok_move_drop_once`.
- [x] Positive: Copy+drop payload shape pinned in stage2:
      `lang/tests/stage2/test_hir_to_mir_match_copy_payload_drop_once.py`
      (`CopyValue` emitted for copy field extraction and variant scrutinee drop
      count is exactly one across the match CFG).
- [x] Negative: double-drop detection — pinned by
      `lang/tests/codegen/e2e/result_ok_array_match_move_no_double_free`.
- [x] No-early-drop assertion: payload reference must be valid at arm exit point.
      (`lang/tests/codegen/e2e/result_ok_noncopy_binder_drop_ordering`)
- [x] Existing `*result*` and `*optional*` e2e suite passes; new cases coexist
      cleanly (covered by standard pre-commit validation run).

---

### Item 2 — Fix F2: DiagnosticValue Drop Alloca
**Priority: HIGH | Files: `llvm_codegen.py`** — **DONE ✓**

- [x] Add failing test: function that drops a `DiagnosticValue` inside a `while`
      loop or `if` branch.
      (`lang/codegen/llvm/tests/test_llvm_codegen_dv_drop_helper.py` — confirmed)
- [x] Fix: emit `_ensure_dv_drop_helper()` (module-scope helper), mirroring
      the array/variant pattern.
      (`llvm_codegen.py` L7648, called from `_emit_drop_value` at L7371 — confirmed)
- [ ] Optionally run `opt --verify` on the generated IR to confirm no alloca
      dominance violations. (verification step — not confirmed yet)

---

### Item 3 — Fix F3+F7: Borrow Checker Escape Analysis (merged)
**Priority: HIGH | Files: `borrow_checker_pass.py`** — **DONE ✓**

- [x] Extend `_borrow_from_optional_ref_call` to handle `HInvoke` in addition to
      `HCall`/`HMethodCall`.
      (`borrow_checker_pass.py` L1460 — confirmed; chain-peeling at L1463–1471)
- [x] Add escape-context awareness to `_add_lambda_capture_loans`: `_lambda_has_borrow_capture()`
      at L323; `_report_lambda_escape_if_borrowed()` at L330; wired at L1865, L1883, L1911, L1926.
- [x] Add regressions:
  (`lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py` — confirmed)
  - Iterator borrow via `HInvoke` path does not silently escape.
  - Callback/closure capturing `&mut T` passed to `spawn` is rejected.
  - Callback capturing `&T` (read-only) passed to `spawn` is accepted.
  - Nested `iter.next().field` access chain is tracked correctly.
- [x] Confirm existing borrow checker tests still pass.

---

### Item 4 — Fix F4: Intrinsic Validation → Structured Diagnostics
**Priority: HIGH | Files: `driftc.py`** — **DONE ✓**

- [x] Add driver-level regression: wrong-arity `swap` call produces a
      `Diagnostic` with `severity="error"`, `phase="typecheck"`, and a non-None
      `span`. (`lang/tests/driver/test_intrinsic_callinfo_diagnostics.py` — confirmed)
- [x] Convert the intrinsic validation block to `Diagnostic` emission via
      `_intrinsic_contract_diag()` helper (L740–754); full validator at L757–956.
- [x] Stable error codes assigned per intrinsic arity/shape error.

---

### Item 5 — Fix F5: Centralize Copy Predicate in Stage2
**Priority: HIGH | Files: `hir_to_mir.py`** — **DONE ✓**

- [x] Extract `_should_copy_value(ty: TypeId) -> bool` in `hir_to_mir.py`.
      (L396 — confirmed; normalises `is_copy()` vs `copy_status()`)
- [x] Replace all ~12 scattered sites. (13 sites confirmed — L843, L848, L886,
      L931, and others; `is_bitcopy()` at L3487 addressed within this consolidation)
- [x] Investigate `is_bitcopy()`: now addressed inside `_should_copy_value` scope.

---

### Item 6 — Add F6: `VariantGetField` Validator in `mir_validate.py`
**Priority: MEDIUM | Files: `mir_validate.py`** — **DONE ✓**

- [x] Implement `validate_mir_variant_field_invariants(funcs, type_table)`:
      (`mir_validate.py` L13 — confirmed; wired in driver at `driftc.py` L5226)
  - arm name must exist in the variant's arm schema
  - field index must be < arm.field_count
  - `field_ty` must match the arm schema field type
- [x] Add positive test: well-formed `VariantGetField` passes validation.
      (`lang/tests/stage2/test_mir_validate_variant_and_hygiene.py` — confirmed)
- [x] Add negative test: out-of-bounds field index fails validation with a clear
      non-internal diagnostic.

---

### Item 7 — Add F8: SSA Operand Hygiene Pass in `mir_validate.py`
**Priority: MEDIUM | Files: `mir_validate.py`** — **DONE ✓**

- [x] Implement `validate_mir_basic_hygiene(funcs)`:
      (`mir_validate.py` L50 — confirmed; wired in driver at `driftc.py` L5222)
  - build def-set per function (all SSA names emitted as destinations)
  - verify every instruction operand (value, local) is in that set
  - report which instruction and which operand is undefined
- [x] Add negative test: `DropValue` with undefined value name fails hygiene check.
      (`lang/tests/stage2/test_mir_validate_variant_and_hygiene.py` — confirmed)

---

### Item 8 — Fix F11: Add `span` to `CallContractIssue`
**Priority: MEDIUM | Files: `call_contract.py`, callers** — **DONE ✓**

- [x] Add `span` field to `CallContractIssue` dataclass.
      (`call_contract.py` L17: `span: Span = field(default_factory=Span)` — confirmed)
- [x] Update all 5 contract checks to forward the originating expression's span.
      (All 5 use `span=getattr(expr, "loc", Span())` — confirmed)
- [x] Update callers to attach the span to emitted diagnostics.
- [x] Regression: call contract violation diagnostic includes non-None source span.
      (`lang/tests/driver/test_intrinsic_callinfo_diagnostics.py` — confirmed)

---

### Item 9 — Fix F9: Single Source of Truth for Variant Layout
**Priority: MEDIUM | Files: `llvm_codegen.py`** — **DONE ✓**

- [x] Refactor the drop helper emit closure to call `_variant_layout()` rather
      than re-implementing alignment/offset arithmetic.
      (Drop helper at `llvm_codegen.py` L7544 calls `_variant_layout()` — confirmed)
- [ ] Add test: variant with multi-field arm; drop inside a branch; verify no
      runtime crash. (test not explicitly confirmed)

---

### Item 10 — Fix F10: Pre-Validate Match Constructors Before Block Creation
**Priority: LOW (after critical items) | Files: `hir_to_mir.py`** — **DONE ✓**

Checker is the primary owner of unknown-constructor detection; this is a
secondary safety net for cases the checker misses.

- [x] Pre-validation loop runs before match arm block allocation.
- [x] `arm_blocks` allocation moved to after constructor validation.
- [x] Add regression test: unknown constructor raises before arm-block creation.
      (`lang/tests/stage2/test_hir_to_mir_match_ctor_prevalidate.py`)

---

### Item 11 — Fix F12: Deduplicate Forward Nominal Resolution in Codegen
**Priority: LOW | Files: `llvm_codegen.py`** — **DONE ✓**

- [x] Extract canonical resolution helpers.
      (`_resolve_forward_nominal_typeid()` at L5892; `_canonical_codegen_typeid()` at L5931 — confirmed)
- [x] Replace both duplicated implementations with calls to the new methods.
      (`_variant_layout()` at L5824 and `_llvm_type_for_typeid()` at L6024 both
      call `_canonical_codegen_typeid()` — confirmed)

---

### Item 12 — Fix F13/F14: Diagnostic String and Span Quality in Checker
**Priority: LOW | Files: `checker/__init__.py`, `call_contract.py`** — **DONE ✓**

- [x] F14 — Audit all `report_*` helpers emitting `span=None`; thread expression
      `loc` and attach it. (`report_index_not_int` and others now accept and forward
      span — confirmed; `lang/tests/driver/test_index_diagnostics_spans.py` confirmed)
- [x] F13 — `checker/__init__.py` itself: no `"internal:"` strings remain (grep confirms).
- [x] F13 — `call_contract.py` message fields were renamed to remove `"internal:"`
      prefixes, and contract tests were updated accordingly.

---

### Item 13 — Fix F15: Centralise Bool i1↔i8 Coercion in Codegen
**Priority: LOW | Files: `llvm_codegen.py`** — **DONE ✓**

- [x] Add central bool coercion helper.
      (`_is_bool_storage_pair()` at `llvm_codegen.py` L8067 — confirmed)
- [x] Replace all 5+ scattered coercion sites with calls to the new helper.
      (15 call sites confirmed via grep — all inline patterns replaced)
- [x] Invariant documented: `i1` = SSA register, `i8` = struct/array storage.

---

## Residual Risks & Observations (re-verification pass, 2026-02-20)

These are not new action items — they record observations from the re-verification
pass that should inform future changes in these areas.

**R1 — F1 coverage gap: mixed-payload arms.**
All five F1 regression shapes are now covered, but each test covers a single arm
type (all-Copy or all-non-Copy). A match with two arms where one binder is Copy
and the other is non-Copy is not tested. If `arm_scrut_payload_moved` logic is
revisited, add a 2-arm mixed-payload regression at that time.

**R2 — F10 guards name existence only.**
The stage2 pre-validation (`hir_to_mir.py` L774–778) catches unknown constructor
names before arm blocks are created. It does not catch a valid constructor name
with a wrong field count or type mismatch — those remain the checker's
responsibility. Downstream, `validate_mir_variant_field_invariants` (F6) will
catch field-bounds violations if they survive to MIR. The two validators are
complementary; neither alone is sufficient.

**R3 — F13 messages are now part of the test contract.**
`test_callinfo_param_layout_contract.py` asserts exact message strings (e.g.
`"CallInfo param layout mismatch for method call (checker bug)"`). Any rename of
`call_contract.py` message strings requires updating those assertions. The
`"(checker bug)"` suffix is intentional — it signals a compiler-internal
invariant violation, not a user code error, which is semantically correct for
these paths. Do not remove it without discussion.

**R4 — Item 2 / F2: `opt --verify` step still not confirmed.**
The fix for DiagnosticValue drop alloca dominance is in place, but the optional
`opt --verify` / LLVM verifier pass on generated IR has not been confirmed as
part of CI. If any e2e test infrastructure runs `opt --verify`, this is covered
implicitly; otherwise it remains a manual check to do when validating a LLVM
upgrade or opt-level change.

**R5 — Item 9 / F9: variant multi-field drop-in-branch test not confirmed.**
The `_variant_layout()` single-source fix is in place. The action plan item for
a dedicated test (variant with multi-field arm; drop inside a branch; no crash)
was not explicitly confirmed as added. If not present, add one to
`lang/tests/codegen/e2e/` before making any variant layout changes.

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
