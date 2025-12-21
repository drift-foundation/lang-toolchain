# Qualified Type Member Access (`Type::member`) + Variant Constructors — work tracker

Goal: add a **general type-level qualified member reference** syntax (`Type::member`) and
use it (in MVP) to support **qualified variant constructor calls**, eliminating constructor
name ambiguity and improving ergonomics when multiple variants expose the same constructor
names (`Some`, `None`, `Ok`, `Err`, …).

Pinned choice (per discussion): use `::` (not `.`) for type-level qualification.

---

## Scope (MVP)

We implement:

1) A general **qualified type member reference** expression:

```drift
TypeRef::member
```

2) A qualified constructor call as ordinary call syntax on that member reference:

```drift
val x: Optional<Int> = Optional::Some(1)     // OK: expected type provides T
val y = Optional::Some(1)                    // OK: infers Optional<Int>
val z = Optional<Int>::None()                // OK: explicit type args on the base
val w = Optional::None<Int>()                // OK: explicit type args after the member (fallback)
```

Pinned MVP parsing constraint:
- `TypeRef` in `TypeRef::member` is a nominal type reference with optional type arguments, but `<...>` is only treated as type arguments in expression position when it is unambiguous:
  - `TypeName<T>::Ctor(...)` commits only when the matching `>` is followed by `::`
  - `TypeName::Ctor<T>(...)` commits only when the matching `>` is followed by `(`
- This commit rule keeps comparisons like `while i < 3 { ... }` unambiguous while still allowing nested generic bases like `Optional<Array<String>>::None()`.

Non-goals for this slice:

- Qualified *methods* (`Type::method`) or associated constants.
- Qualified access to module members via `::` (module qualification remains `import m as x; x.foo`).
- Named-field construction / named-field patterns.
- Allowing `::` on non-`TypeRef` type expressions (keep LHS syntactically narrow for now).

---

## Semantics (pinned)

### 0) What is `TypeRef` (pinned, MVP)

`TypeRef` is intentionally narrow in MVP to avoid grammar ambiguity and future churn.

Allowed forms:

- A nominal type name (`Ident`) with optional type arguments, e.g. `Optional`, `Optional<Int>`, `Optional<Array<String>>`.

If module-qualified type names exist in a given compiler stage, a `TypeRef` may also include
an explicit module qualification in that stage’s type grammar. (MVP for this feature does
not expand `TypeRef` beyond “named type + optional type args”.)

Explicitly excluded (even if they exist elsewhere in the language later):

- Parenthesized / composite type expressions
- Function types
- Union/intersection types
- Any “computed” type operators

### 1) Where `::` applies

- `TypeRef::member` is a type-level qualified member reference.
- In MVP, the only legal `member` kind is a **variant constructor name**.
  - Using `TypeRef::member` where `TypeRef` is not a `variant` type is a typecheck error.
  - Using a non-constructor member name is a typecheck error.

### 2) Expected-type rule interaction

Unqualified constructor calls still require an expected type:

```drift
val a: Optional<Int> = Some(1)   // OK
val b = Some(1)                 // error (no expected type)
```

Qualified constructor calls provide their own type and therefore do **not** require a
separate expected type as long as the variant instantiation can be determined from:

- explicit type arguments on `TypeRef`, or
- inference from constructor arguments (see below).

### 3) Deterministic type-argument inference (pinned algorithm)

For a qualified constructor call `TypeRef::Ctor(args...)`:

1. Resolve `TypeRef` to a variant type constructor `V`.
2. Infer type arguments by unifying constructor field types with argument types.
3. If inference succeeds and is fully determined: instantiate and typecheck.
4. If inference is underconstrained (e.g. `Optional::None()`): error `E-QMEM-CANNOT-INFER` unless an expected type provides the missing type arguments.
5. If inference conflicts: error `E-QMEM-INFER-CONFLICT`.

MVP consequence:
- `Optional::Some(1)` succeeds (infers `T = Int`).
- `Optional::None()` fails (underconstrained).

#### 3.1) Typechecking order during inference (pinned)

- Argument expressions are typechecked first to obtain their argument types.
- Unification uses the argument types *prior to any implicit coercions* (MVP: exact match).
- Coercions (if/when they exist later) are applied only after the variant instantiation is
  fully determined.

### 4) Collision behavior

- `Type::Ctor(...)` is never ambiguous (constructor lookup is performed within that variant).
- It is the primary escape hatch for the existing “unqualified constructor collisions” UX gap.

### 5) Constructor overloads (pinned)

- There are **no overloaded constructor names** within a single `variant`.
  A constructor name is unique per variant; arity mismatch is a direct error.

### 6) `TypeRef::member` used as a value (pinned MVP behavior)

The syntax introduces a general member reference expression, so we pin its MVP behavior:

- `TypeRef::member` is allowed syntactically.
- In MVP, it is only *typecheck-valid* when used as the callee of a call:

  ```drift
  Optional::Some(1)         // OK: infers Optional<Int>
  val f = Optional::Some    // error in MVP
  ```

Diagnostic (pinned):
- `E-QMEM-NOT-CALLABLE`: qualified member is not a first-class value in MVP; call it directly.

---

## Work plan

### Step 1 — Grammar + parser AST

- Add a new postfix selector form for a **type reference**:
  - `QualifiedMemberExpr ::= TypeRef "::" Ident`
- Do not make “ctor-ness” a syntax decision; it is determined in typecheck.
- Ensure precedence behaves like a postfix selector (same tier as call/index/member),
  so `TypeRef::Ctor(args)` parses as `Call(QualifiedMemberExpr(...), args)`.
- AST nodes (pinned shape):
  - `QualifiedMemberExpr(base_type_ref, member_name, loc)`
  - `Call(func=<expr>, args=[...], loc=...)` reuses existing call node.

Acceptance:
- Parser round-trips:
  - `Optional::Some(1)`
  - `Optional<Int>::None()`
  - `Optional::None<Int>()`
  - `Optional<Array<String>>::None()`
  and captures locations.

### Step 2 — Stage0 adapter + stage1 HIR

- Thread the new AST node through:
  - parser adapter → stage0
  - stage0 → stage1 lowering
- Add a new HIR node mirroring the AST intent:
  - `HQualifiedMember(base_type_ref, member_name, loc)`
  - Keep calls as `HCall(fn=<expr>, args=[...], kwargs=[...])`.

Acceptance:
- `normalize_hir()` and rewrites traverse the new node (arg expressions, blocks, etc.).

### Step 3 — Type checking

Implement type rules in two stages to keep the “general member reference” contract real:

1) Resolve `TypeRef::member` to a **ResolvedQualifiedMember** (internal):
   - kind enum: `ctor` (MVP), later `assoc_fn`, `assoc_const`.
   - MVP: only `ctor` is implemented; other kinds are rejected.
2) Typecheck callability:
   - MVP: only calling a ctor is allowed; bare member references error (`E-QMEM-NOT-CALLABLE`).

- Resolve `HQualifiedMember.base_type_ref` → `TypeId`.
- Reject if not a `variant` type (`E-QMEM-NONVARIANT`).
- Lookup constructor by name within that variant schema (`E-QMEM-NO-CTOR`).
- Type argument handling uses the pinned inference algorithm above:
  - explicit args: instantiate and typecheck
  - inferred args: infer/unify or error
- Result type is the instantiated variant `TypeId`.

Diagnostics (pinned):
- `E-QMEM-NONVARIANT`: `Type::member` used with a non-variant type
- `E-QMEM-NO-CTOR`: constructor not found in variant
- `E-QMEM-ARITY`: wrong number of args
- `E-QMEM-CANNOT-INFER`: cannot infer type args (underconstrained, e.g. `Optional::None()`)
- `E-QMEM-INFER-CONFLICT`: inferred type args conflict with argument types
- `E-QMEM-NOT-CALLABLE`: qualified member reference is not a value in MVP (must be called)

### Step 4 — Stage2 lowering

- Lower qualified member ctor calls to the existing MIR `ConstructVariant` path:
  - resolve variant schema + arm index
  - evaluate arguments left-to-right
  - pack payload fields in declaration order

Pinned MIR invariant:
- MIR preserves argument evaluation order as written for `ConstructVariant`; optimizations must not reorder
  or duplicate argument evaluation.

Acceptance:
- No new MIR ops needed; this is pure front-end sugar to the existing variant construction.

### Step 5 — Tests (must-have)

Driver/unit tests:
- Parse/typecheck positive:
  - `Optional::Some(1)` works without an expected type annotation.
- Inference positive:
  - `Optional::Some(1)` works (infers `T = Int`) without an expected type annotation.
- Generic base negative:
  - `Optional::None()` rejected with `E-QMEM-CANNOT-INFER` (underconstrained).
- Non-variant negative:
  - `Point::Some(1)` rejected with `E-QMEM-NONVARIANT`.
- Unknown constructor negative:
  - `Optional::Bogus(1)` rejected with `E-QMEM-NO-CTOR`.

E2E tests:
- Disambiguation proof:
  - Two variants in scope both define `Some`; use qualified syntax to construct the intended one.
- Keep one test that uses unqualified `Some` to ensure old rule still holds (expected type required).

### Step 6 — Spec update (small, but required)

- Update `docs/design/drift-lang-spec.md` Chapter 10 (`variant`) to document:
  - the new qualified member + ctor form (`Type::Ctor(...)`)
  - how it interacts with the expected-type rule
  - MVP inference behavior (`Optional::Some(1)` works; `Optional::None()` errors)

---

## Status

- Completed (MVP)
  - Parser + HIR support for `TypeRef::Ctor(...)` with bounded `<...>` speculation (`>::` / `>(` commit tokens) and nested generic handling.
  - Typechecker resolves qualified members to variant ctors, supports deterministic inference (`Optional::Some(1)`), and emits improved ctor-related diagnostics.
  - E2E + driver coverage includes nested generics, both explicit spellings, and a pinned parser diagnostic for duplicate type-arg lists (`E-PARSE-QMEM-DUP-TYPEARGS`).
