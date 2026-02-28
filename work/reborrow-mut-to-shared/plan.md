# Plan: Implicit Shared Reborrow from Mutable Reference

## 0) Goal
Close the language ergonomics gap where `&mut T` cannot currently be used at immediate call sites that expect `&T`, while keeping escaping/return-position coercions explicitly out of scope until borrow semantics are specified more fully.

## 1) Classification
- Primary: `LANG_GAP`
- Secondary: `SPEC_CLARIFICATION`

This is not treated as a compiler defect unless the current spec already promises the coercion.

## 2) Target behavior (v1)
### 2.1 Supported
Allow implicit reborrow/coercion from `&mut T` to `&T` only in non-escaping call argument position.

Examples that should compile:
```drift
struct Foo {}

fn takes_ref(x: &Foo) nothrow -> Int { return 0; }

fn demo() nothrow -> Int {
    var f = Foo();
    return takes_ref(&mut f);
}
```

### 2.2 Explicitly not supported in this work
Do not add implicit return-position coercion yet.

Example that should continue to fail:
```drift
struct Foo {}

fn as_ref(x: &mut Foo) nothrow -> &Foo {
    return x;
}
```

Rationale: return-position reborrow creates an escaping borrow result and needs explicit lifetime/borrow semantics.

## 3) Deliverables
1. Positive regression: call-site `&mut T` accepted where `&T` is expected.
2. Negative regression: return-position `&mut T` -> `&T` remains rejected with a clear diagnostic.
3. Checker/lowering implementation for call-boundary-only reborrow.
4. Spec or design-note clarification recording the supported boundary and the deferred return-position case.

## 4) Regression-first plan
### 4.1 Positive regression
Add a minimal driver/e2e test proving:
- `takes_ref(&mut f)` compiles and runs.
- Same should work for callback invocation if callback parameter type is `&T` and caller holds `&mut T`.

### 4.2 Negative regression
Add a minimal test proving:
- `return x;` where `x: &mut Foo` and return type is `&Foo` still fails.
- Diagnostic should say coercion/reborrow is only supported in immediate call argument position (or equivalent wording).

## 5) Likely implementation area
Expected touch points:
- Checker argument compatibility / call-site type matching
- Potentially lowering if borrow flavor is encoded distinctly and needs a shared-borrow temporary or explicit reborrow node

Non-goal:
- No global implicit subtype relation between `&mut T` and `&T`
- No broad expression-level coercion outside immediate call argument position

## 6) Semantic contract
### 6.1 Allowed
- Direct call argument passing: `fn f(x: &T)` may accept an argument expression of type `&mut T`.
- The coercion is temporary and non-escaping.

### 6.2 Not allowed
- Assignment/coercion to locals by default:
  - `val r: &Foo = &mut f;` remains out of scope unless explicitly designed later.
- Return-position coercion remains rejected.
- Storing `&mut T` where `&T` is expected in aggregates remains out of scope.

## 7) Callback coverage
Since the report mentions REST callbacks, include one regression covering callback parameter passing:
- function/callback expects `&Request`
- caller has `&mut Request`
- immediate call should compile

Do not broaden this into closure capture or escaping borrow design.

## 8) Risks
1. Over-broad coercion leaking into assignments/returns/aggregate stores.
2. Borrow checker inconsistencies if lowering treats the reborrow as the same value rather than a restricted shared view.
3. Silent expansion of reference compatibility without documentation.

Controls:
- Keep implementation narrowly scoped to call argument matching.
- Add negative regressions for return/assignment contexts.
- Update design note/spec text in the same patch.

## 9) Completion criteria
- Positive call-site regression passes.
- Negative return-position regression passes.
- Callback case passes.
- No unrelated borrow/reference regressions introduced.
- Spec/design note updated to state: call-site reborrow supported, escaping coercions deferred.
