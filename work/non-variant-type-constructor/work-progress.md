# Non-Variant Qualified Static Calls (`Type::func(...)`) – Work Plan

## Goal
Enable non-variant qualified static calls in MVP:
- `Type::func(...)` where `func` is an associated/static function (no `self` receiver).

Keep existing behavior:
- variant constructor calls (`Variant::Ctor(...)`) continue to work unchanged,
- value-form qualified member references remain rejected (`Type::member` not called).

## Scope (MVP-safe)
- Add support only for **called** qualified members on non-variant bases.
- Do not add general associated-value semantics.
- Do not add implicit receiver rewriting here.

## Current State (baseline)
- Parser already supports qualified-member syntax (`::`) and emits `HQualifiedMember`.
- Checker/call-resolver currently prioritizes variant constructor flow and reports:
  - `E-QMEM-NONVARIANT` for non-variant bases.
- Stage2 has variant-only logic in `_visit_expr_HQualifiedMember` and assert guards for malformed typed-mode states.

## Implementation Plan

### 1. Checker/call-resolver: add non-variant associated-static resolution
- In qualified-member call resolution, after variant-constructor path does not apply:
  - attempt resolving `Type::member(...)` as associated/static function for the resolved base type.
- Resolution result must produce proper `CallInfo`:
  - direct call target,
  - fully instantiated param/return types,
  - can-throw metadata.
- Enforce static-only in this feature path:
  - methods requiring `self` are rejected with a checker-facing diagnostic.

### 2. Diagnostics
- Keep diagnostics user-facing and deterministic.
- Add/standardize specific diagnostics for this path:
  - non-static member used as `Type::member(...)` (receiver required),
  - unknown member on non-variant base,
  - arity/type-arg mismatches in associated-static call path.
- Ensure no internal checker/stage2 assertion messages leak for these user errors.

### 3. Typed-mode/stage2 invariants
- Ensure typed-mode normalization of qualified-member calls yields call info that stage2 can lower through normal call path.
- Preserve variant-only `_visit_expr_HQualifiedMember` value semantics unless explicitly expanded later.
- No new ad-hoc lowering fallback; prefer checker-owned resolution and call metadata.

### 4. Boundary contract alignment (mandatory)
Per boundary guardrails:
1. Add positive end-to-end regressions for the new supported shape.
2. Add negative regressions for unsupported shapes.
3. Update stale comments/tests/messages that still imply non-variant `Type::...` is universally invalid.

## Regression Matrix

### Positive
- Driver: non-generic struct associated static call
  - `S::make(...)` where `make` has no receiver and returns `S`.
- Driver: generic associated static call
  - Type-generic base only: `Box<Int>::empty()`.
  - Additional function-generic path (if function declares its own type params):
    - e.g. `Box<Int>::from_other<type U>(...)`.
- e2e: executable smoke using this style in real code path.

### Negative
- Driver: `Type::member(...)` where `member` requires `self` receiver
  - must fail with checker diagnostic (not internal).
- Driver: unknown qualified member on non-variant base
  - must fail with clear checker diagnostic.
- Driver: value-form non-called `Type::member`
  - remains rejected per MVP.

### Boundary/contract
- Driver boundary assertion: successful non-variant qualified static call compiles without
  - `internal: MIR lowering contract failure`,
  - `internal: LLVM lowering contract failure`.

## Validation Checklist
- Targeted driver tests for new positive/negative/boundary cases.
- Existing qualified-member variant constructor tests remain green.
- Stage2 typed-mode validator tests remain green.
- Small e2e subset including new case passes under:
  - normal,
  - `DRIFT_ASAN=1`,
  - `DRIFT_ALLOC_TRACK=1`.

## Non-Goals (for this iteration)
- General associated values or namespace-like static members.
- New `Type::member` value semantics.
- Broader method dispatch redesign.

## Syntax/Type-Arg Pin
- Distinguish base-type generic args from function generic args in qualified static calls:
  - Base-type instantiation: `Box<Int>::empty()`
  - Function-level type args (only when function itself is generic): `Type<...>::fn<type ...>(...)`
- Add targeted tests to prevent ambiguity/regression between these two type-arg channels.

## Status (Current Branch)
- Implemented:
  - non-variant qualified static call resolution in checker/call-resolver (`Type::func(...)`),
  - diagnostics for:
    - `E-QMEM-RECEIVER-REQUIRED`,
    - `E-QMEM-NO-MEMBER`,
    - `E-QMEM-NO-OVERLOAD`,
  - parser/checker acceptance for associated static methods declared inside `implement` blocks (no `self` receiver),
  - callable-registry boundary relaxation for associated static methods (`self_mode is None`),
  - instantiation recording fallback for qualified static calls using base type args.
- Regression coverage added/updated (e2e):
  - `qualified_static_nonvariant_basic`,
  - `qualified_static_generic_base_and_fn_typeargs` (currently generic-base path),
  - `qualified_static_receiver_required_rejected`,
  - `qualified_static_unknown_member_rejected`,
  - updated expectations:
    - `qualified_ctor_nonvariant_base_rejected`,
    - `method_receiver_missing_self`,
    - `method_receiver_self_required`.

## Follow-Up Status
- Function-generic explicit type-arg channel is now covered for qualified static calls:
  - parser accepts `Type<...>::fn<type ...>(...)`,
  - static resolver records inferred/explicit function type args for instantiation keys,
  - regression pinned in:
    - `qualified_static_generic_base_and_fn_typeargs` (`Box<type Int>::id<type Int>(...)`).
- Duplicate type-arg channel for qualified constructors is still rejected (moved to checker):
  - `E-QMEM-DUP-TYPEARGS` from qualified ctor resolution when both base and call channels are provided,
  - regression pinned in `qualified_ctor_dup_type_args_rejected`.
