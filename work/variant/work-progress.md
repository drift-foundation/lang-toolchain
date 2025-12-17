# Variant + generics iteration prereqs

## Scope and pinned decisions

- Goal driver: enable iterator-based `for pattern in expr { ... }`
  (per `docs/design/spec-change-requests/drift-loops-and-iterators.md`).
- `match` is **expression-only** in MVP.
- Match arms are **blocks** in MVP:
  - `Ctor(pat) => { ... }`
  - `default => { ... }`
  - A block may contain statements; its value (when needed) is the last expression.
- Patterns are **positional-only** in MVP (`Some(x)`), and there is **no `_` wildcard pattern**.
  - No named-field patterns (`Some(value = x)` deferred).
- Variant ABI/layout is **compiler-private** for now
  (do not freeze in `docs/design/drift-lang-abi.md` yet).
- Clean path: implement **generics first**, but only what is required for:
  - `Optional<T>`
  - `Iterator<Item>.next() -> Optional<Item>`
- `match` exhaustiveness (MVP):
  - Non-exhaustive matches are allowed **only** if a `default` arm is present.
  - Without `default`, matches must be exhaustive.

## Constructor name resolution (pinned)

Variant constructors are **unqualified identifiers** in MVP (`Some`, `None`, `Ok`, `Err`).

Resolution rule:

- A constructor name may be used **only if it resolves uniquely** in the current scope.
- If multiple visible constructors share the same name, using the bare name is a
  **compile-time error**.
- Diagnostics must list all candidate constructors.
- Until qualified constructor syntax exists, collisions are resolved only by
  adjusting scope (imports / visibility / re-exports).

This guarantees deterministic behavior without committing to a long-term surface syntax.

## Constructor use in expressions (pinned, MVP)

- A constructor call in *expression position* (e.g. `Some(x)`) is only allowed when there is an **expected variant type** from context:
  - explicit type annotation (`val y: Optional<Int> = Some(1)`),
  - function parameter type (`takes_opt(Some(1))` where `takes_opt` expects `Optional<Int>`),
  - function return type (`return Some(1)` in a function returning `Optional<Int>`),
  - other contexts where the compiler already knows the expected type.
- If no expected variant type is available, the compiler emits a typecheck error:
  - “constructor call requires an expected variant type; add a type annotation”.
- `match` patterns resolve constructor names relative to the scrutinee type and do not require an expected type.

## Generics MVP constraints (hard limits)

To prevent scope creep, generics are deliberately restricted:

- Generics apply to **nominal types only**:
  - `variant Optional<T> { ... }`
  - (later) `trait Iterator<Item> { ... }`
- **No generic functions** in MVP.
- **No generic `implement<T> ...` blocks** in MVP.
- **No higher-kinded types**.
- **No trait bounds or constraints** beyond simple substitution.
- Codegen strategy is **monomorphization-only**:
  - Only concrete instantiations that appear after typechecking are generated.
- Type inference is minimal:
  - Constructor arguments may determine `T` when the surrounding type is known.
  - No global inference or Hindley–Milner-style unification.

## Instantiated type representation

Pinned internal model:

- Each instantiated generic type corresponds to a **distinct interned `TypeId`**,
  keyed by `(generic_base_id, type_args...)`.
- `Optional<Int>` and `Optional<Float>` are different `TypeId`s.
- `TypeTable` owns instantiation via:
  - `ensure_instantiated(base_id, [arg_ids]) -> TypeId`
- This guarantees:
  - cheap equality checks,
  - stable monomorphization caching,
  - clear error messages,
  - memoized LLVM emission.

## Variant lowering (compiler-private contract)

Internal invariant for MVP (not user-visible ABI):

- A variant value lowers to:
```

{ tag: i8, payload: <opaque blob> }

````
- `tag` values are assigned by declaration order (0..N-1).
- `payload` is sized and aligned to fit the largest arm payload.
- Arms with no fields use only `tag`; payload bytes are ignored.
- Field layout within payload follows declaration order and natural alignment.

This contract is internal but treated as stable across frontend → MIR → LLVM
during MVP.

## Status

- Implemented:
  - Parser grammar + AST for `variant` declarations and `match` expressions.
  - Parser adapter + `TypeTable` plumbing for variant schemas (generic-aware) and
    instantiation (`ensure_instantiated`).
  - Stage1: `MatchExpr` lowering to `HMatchExpr`/`HMatchArm` with explicit arm
    `result` expression.
  - Typed checker:
    - `match` expression typing (default rules, duplicate ctor arms, value-vs-stmt arm rules).
    - Constructor call rule: requires an expected variant type (diagnostic `E-CTOR-EXPECTED-TYPE`).
  - Stage2 MIR:
    - `ConstructVariant`, `VariantTag`, `VariantGetField` MIR ops.
    - `HMatchExpr` lowering as explicit tag-dispatch CFG (value and statement position).
    - Variant constructor call lowering using expected-type threading from `let` annotations and `return`.
  - LLVM codegen:
    - Internal compiler-private variant representation: `%Variant_<id> = { i8 tag, [7 x i8], [N x i64] payload }`.
    - Lowering for `ConstructVariant` / `VariantTag` / `VariantGetField`.
  - E2E coverage:
    - `Optional<T>` + `match` roundtrip.
    - Rejection: constructor without expected type.
    - Rejection: non-exhaustive match without `default`.
  - Hardening / correctness fixes:
    - Parser `match` scrutinee/pattern extraction fixed (no ValueError crashes on valid `match`).
    - Stage1 normalization (`TryResultRewriter`) now traverses `HMatchExpr` blocks/results.
    - `stage1/__init__.py` exports `HMatchExpr`/`HMatchArm` so typed checking sees match nodes.
    - Typed checker prefers `E-CTOR-EXPECTED-TYPE` over a generic overload error when a ctor name has no callable candidates.
    - Stub SSA typing (`Checker.build_type_env_from_ssa`) recognizes `ConstructVariant`/`VariantTag`/`VariantGetField` so stage4 return-shape checks accept variant returns.
    - LLVM backend allows returning `%Variant_<id>` values from non-can-throw functions.

- Still pending / deferred:
  - Constructor name collision UX improvements (list candidates).
  - Named-field patterns and named-field construction (explicitly deferred).

## Plan

### Milestone 1: Variants + match end-to-end

Goal: ship `variant`, constructor calls, and `match` expressions,
sufficient to support `Optional<T>`.

#### 1) Spec alignment

Update `docs/design/drift-lang-spec.md` to pin:

- `variant` declaration semantics.
- `match` expression semantics.
- Pattern forms supported in MVP:
  - `default` (keyword; not a pattern)
  - constructor pattern `Ctor(p1, p2, ...)`
- Default arm semantics:
- `default` is a **keyword**, not a pattern.
- Syntax: `default => { ... }`
- Introduces **no bindings**.
- May appear **at most once**.
- Must be **last**.
- Exhaustiveness rules:
- Without `default`, matches must be exhaustive.
- With `default`, non-exhaustive matches are allowed.
- Diagnostics (MVP):
- Duplicate constructor arms are errors.
- `default` appearing more than once is an error.
- Any arm after `default` is an error (unreachable).
- Typing rule:
- All match arms must produce the **exact same type** (no coercions, no common-supertype inference).
- Block-valued typing rule (MVP):
  - If the match result is used as a value, every non-diverging arm block must end with a value expression of that same type.
  - If the match result is unused (statement position), arm blocks may omit a final expression.

#### 2) Front-end generics plumbing

- Parse and carry type parameters through AST → stage0 → HIR.
- Support generic variant declarations.

#### 3) Type system core

- Add `TypeKind.VARIANT`.
- Implement generic instantiation via `TypeTable.ensure_instantiated`.
- Enforce monomorphization-only strategy.

#### 4) Variant declarations and constructors

- Store variant schemas:
- name
- type parameters
- arms and field types
- Typecheck constructor calls:
- unique resolution
- correct arity
- argument type correctness

#### 5) Match expressions

- Typecheck:
- scrutinee must be a variant type.
- patterns must belong to that variant.
- arm result types must be identical.
- Lowering:
- generate tag tests
- extract payload fields
- join control flow with a single result value.

### Milestone 2: `for` loops via iterator protocol (MVP intrinsics)

Goal: enable `for val x in expr { ... }` by desugaring to:

```
let it = expr.iter();
loop {
  match it.next() {
    Some(x) => { body }
    default => { break }
  }
}
```

Implementations are compiler intrinsics until traits/modules exist:

- `Array<T>.iter() -> __ArrayIter_<T>`
- `__ArrayIter_<T>.next() -> Optional<T>`

Status:

- Implemented:
  - Stage1 `for` desugaring to `loop` + `match` using the iterator protocol.
  - Internal iterator struct type (`__ArrayIter_<T>`) via `TypeTable` (compiler-private).
  - Stage2 MIR lowering for `.iter()` / `.next()` intrinsics on arrays.
  - SSA+LLVM support for cyclic CFGs required by `loop` lowering.
  - E2E coverage: `lang2/codegen/tests/e2e/for_loop_sum_int`.

### Milestone 2: `for` loops via structural iteration

Goal: enable `for pattern in expr { ... }` using `Optional<T>`.

#### 6) Optional and iteration

- Provide:
```drift
variant Optional<T> {
    Some(value: T),
    None
}
````

* Desugar:

  ```drift
  for pat in expr { body }
  ```

  into:

  ```drift
  let it = expr.iter();
  loop {
      match it.next() {
          Some(pat) => { body }
          default => { break }
      }
  }
  ```

#### Dispatch strategy (pinned)

* `iter()` and `next()` are **methods**.
* Dispatch is **static only** in MVP:

  * no vtables
  * no trait objects
* Structural rule (temporary, until traits land):

  * `expr.iter()` must resolve uniquely.
  * iterator type must have `next(&mut self) -> Optional<Item>`.
  * failure to resolve is a compile-time error.

## Explicitly deferred

Not MVP:

* Named-field patterns.
* Qualified constructor syntax (`Optional.Some`).
* Stable external ABI for variants.
* Generic functions.
* Generic impl blocks.
* Trait bounds and dynamic dispatch.
