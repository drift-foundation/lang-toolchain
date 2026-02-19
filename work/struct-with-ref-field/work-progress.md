# Struct With Ref Field (restricted MVP) – work progress

## Goal
- Support user syntax like `struct S { x: &T }` / `struct S { x: &mut T }` without extra surface annotations.
- Keep MVP sound by enforcing strict non-escape rules, while still allowing the target API shape:
  - `query(&mut Session) -> Result<Statement, E>` where `Statement` embeds `&mut Session`.

## Non-goals (this phase)
- Full lifetime/region system.
- Allowing ref-field structs in owning containers/globals.
- New language keyword (no `@borrowed`/similar).

## Model
- Compiler infers an internal class: **borrowed aggregate** (any struct with at least one ref field).
- Restrictions apply automatically based on type class.

## Required behavior

### Allowed
- Local construction and local use.
- Return of borrowed aggregate **only when borrow origin is tied to input ref param provenance**.
- Wrapper returns preserving provenance:
  - `Result<borrowed_aggregate, E>`
  - `Optional<borrowed_aggregate>`

### Rejected
- Storing borrowed aggregate in owning containers (`Array`, `HashMap`, `TreeMap`, etc.).
- Storing in globals/registry/other long-lived process state.
- Escaping closure/callback capture.
- Generic pass-through unless explicitly proven non-retaining.
- Returning borrowed aggregates with mixed/multiple ref origins.

## Provenance rule (pinned)
- Returned borrowed aggregates must have **exactly one** ref-origin parameter.
- Multi-origin returned borrowed aggregates are rejected in MVP.
- Rationale: keeps provenance, alias checks, and diagnostics tractable while matching immediate use cases (e.g. `Statement` tied to one `&mut Session`).

## Pinned caution gates (must be implemented + tested)

1. Origin proof through wrappers
- Origin tracking must survive `Result::Ok/Err`, `Optional::Some/None`, and match binders.
- If origin is lost/unknown, boundary operations must reject.

2. Method receiver interactions
- `&mut self` methods on borrowed aggregates must preserve exclusivity of embedded `&mut` fields.
- Conflicting borrow paths must be rejected deterministically.

3. No hidden retain via generics
- Generic calls are retaining by default.
- Borrowed-aggregate pass-through only when explicit non-retaining metadata proves safety.

4. Destructor path safety
- Return paths must preserve deterministic drop ordering for locals/temps.
- Returning borrowed aggregates must not weaken existing drop guarantees.

## Implementation plan

1. Parser/type declaration gate
- Remove hard MVP reject for ref-typed struct fields in `lang/driftc/parser/__init__.py`.

2. Type checker classification + boundaries
- Mark structs with ref fields as borrowed aggregates (internal flag/type query).
- Enforce non-escape rules at:
  - assignment/storage boundaries
  - call boundaries
  - return boundaries
- Extend current ref-origin logic to aggregate/wrapper paths.

3. Borrow checker flow updates
- Track borrowed-aggregate flow as carrying underlying borrow constraints.
- Reject escapes and aliasing violations through method/call/control-flow paths.

4. Stage boundary checks (strict)
- Ensure checker -> stage2 -> MIR validate -> LLVM lowering expectations are aligned.
- Add positive + negative boundary regressions.
- Update stale boundary comments/tests/messages if support surface changes.

5. Diagnostics
- User-facing, non-internal diagnostics for each forbidden boundary:
  - container/global store
  - escaping capture
  - retaining generic boundary
  - invalid return provenance

## Regression matrix (mandatory)

### Positive
- Local ref-field struct construction/use.
- `query(&mut Session) -> Result<Statement, E>` shape compiles and runs.
- `Optional<Statement>` return path with valid provenance.

### Negative
- Return borrowed aggregate with no param-tied provenance.
- Store borrowed aggregate in `Array` / map / global / registry.
- Capture borrowed aggregate in escaping closure/callback/spawn.
- Generic boundary without explicit non-retaining metadata.
- Alias violation via `&mut self` receiver + embedded `&mut` field.

### Safety runs
- Normal suite for targeted new tests.
- `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1` on targeted subset.
- Valgrind run for focused borrowed-aggregate subset.

## Current status
- Planning pinned.
- Implemented slice 1 + part of slice 2:
  - Removed parser hard reject for struct ref fields.
  - Existing e2e `struct_ref_field_rejected` flipped to acceptance (`exit_code: 0`).
  - Added driver regression suite:
    - `test_borrowed_aggregate_return_single_origin_allowed`
    - `test_borrowed_aggregate_return_from_local_rejected`
    - `test_borrowed_aggregate_return_multi_origin_rejected`
    - `test_borrowed_aggregate_pass_through_generic_default_retaining_rejected`
    - `test_borrowed_aggregate_store_in_array_push_rejected`
    - `test_borrowed_aggregate_pass_by_ref_param_allowed`
  - Added checker enforcement for borrowed-aggregate return provenance:
    - require reference-param-derived origin
    - single-origin only
    - mutable ref fields require `&mut` origin param
    - supports direct wrapper-return constructors (`Result::Ok`, `Optional::Some`) when carrying borrowed aggregates
  - Added checker call-boundary enforcement:
    - borrowed aggregate by-value args are rejected on retaining boundaries
    - explicit non-retaining params and by-ref param shapes are allowed
    - escaping callback/lambda captures that include borrowed aggregates are rejected on retaining boundaries
    - registry/global store via retaining APIs (e.g. `GlobalRegistry::set`) is rejected through the same boundary rule
  - Added checker container boundary enforcement for arrays:
    - owning `Array<borrowed_aggregate>` declarations are rejected

## Rollout note
- Temporary `std.*` exemption was removed.
- Current return enforcement distinguishes:
  - known-invalid local origins (rejected), and
  - unresolved/intermediate ref temporaries without proven origin (deferred, not hard-failed at this stage).
- This keeps strict rejection for pinned invalid paths while avoiding broad stdlib false positives during incremental rollout.

## Remaining implementation work
- Enforce non-escape boundaries beyond returns:
  - reject container/global/registry stores for borrowed aggregates beyond current Array guard
  - reject escaping closure/callback captures
- Strengthen provenance through local variable flow (not only direct constructor returns).
- Stage2/MIR/LLVM boundary alignment for borrowed-aggregate shapes:
  - add positive/negative boundary regressions per guardrail policy
  - verify diagnostics are checker-facing and non-internal for unsupported boundaries
- Add dedicated e2e coverage for boundary rejections.
  - Added:
    - `struct_ref_field_result_return_ok` (positive)
    - `struct_ref_field_array_store_rejected` (negative)
    - `struct_ref_field_callback_capture_rejected` (negative)
    - `struct_ref_field_registry_store_rejected` (negative)
- Added dedicated driver boundary contract suite:
  - `lang/tests/driver/test_struct_ref_field_boundary_contract.py`
  - Asserts:
    - positive case reaches LLVM IR/main wrapper without internal contract failures
    - negative cases fail at `typecheck` phase with non-internal diagnostics
    - regression: concrete by-value receiver methods on borrowed-aggregate wrappers (TreeMap entry APIs) are not falsely rejected when non-retaining metadata is still unknown at typecheck time
