# Generics support — work progress

Goal: add function generics with explicit instantiation and a stable, ID-based substitution spine (TypeParamId/TypeVar) that later phases can build on.

## G2 invariants (locked)
- Type params are identified by ID after parsing; names exist only for diagnostics and pretty-printing.
- Every TypeVar carries its owner (function/signature) to prevent accidental capture across functions/modules.
- Substitution is explicit and total per instantiation in G2 (no partial substitution or inference in this phase).
- Type param names live in the *type namespace* (do not collide with value identifiers).

## Status
- G2 complete: TypeParamId/TypeVar + Subst spine wired, requirements lowered to TypeParamId.
- G3 complete: explicit `<type ...>` instantiation everywhere + minimal inference; ctor/qualified-member inference from expected type works.
- G3.S1 complete: struct generics (base schema + concrete instantiation) with `<type ...>` constructors and nested args.
- G3.S2 complete: generic impl matching (including nested patterns) + ambiguity errors, layered impl/method generics.
- Type-checker suite: `lang2-type-checker-test` green.

## Phase G2 — TypeParamId + TypeVar + substitution spine

### G2.1 Data model
- Add `TypeParamId` (owner + index) and `TypeVar(TypeParamId)` as a `TypeId`/`TypeKind` variant.
  - Recommended shape: `TypeParamId { owner: FnId (or SigId), index: u16 }`.
- Change `FnSignature.type_params` from `List[str]` into:
  - `TypeParam { id: TypeParamId, name: InternedStr, span: Span }`.
  - Preserve `name`/`span` strictly for diagnostics.

### G2.2 Lowering: parser → HIR → TypeIds
- When building a function signature, allocate TypeParamIds in order and build a signature-local `name -> TypeParamId` map.
- When resolving types inside the signature (param types / return type / requirement subjects), lower matching names to:
  - `TypeVar(TypeParamId)` (not a nominal type lookup).
- Scope rule: signature type params are in scope across the whole signature, including requirements/guards.

### G2.3 Substitution spine
- Define `Subst { owner: FnId, args: Vec<TypeId> }` and enforce owner equality on apply/instantiate.
  - (Fast path is vector indexed by `TypeParamId.index`; no HashMap needed in G2.)
- Implement `apply(type_id, subst) -> TypeId`:
  - `TypeVar(id)` → `subst.args[id.index]`
  - recurse through all composite types you already represent (arrays, tuples, optionals, refs/ptrs, fn types, etc.).
  - Keep pure (no mutation); optional memoization later if you intern types and want speed.
- Add `instantiate_fn_sig(sig, subst)` to substitute param + return types (and later requirements if needed).

### G2.4 Explicit instantiation
- In call checking, build `Subst` from `call.type_args`:
  - if arg count != `sig.type_params.len()` → diagnostic (new code; span should highlight the call type-arg list).
  - otherwise instantiate callee parameter/return types *before* checking arguments.
- Ensure qualified members follow the same instantiation rule:
  - `Type::Ctor<type T>(...)` uses the same substitution mechanism (no special casing beyond callee selection).

### G2.5 Trait requirement subjects (data correctness)
- Store `T is Trait` with subject lowered to `TypeParamId` (never stringly).
- Checking “satisfies trait” can be deferred, but the representation must be correct now.

## Tests (G2)
- Same name, different owners: `f<T>` vs `g<T>` remain isolated (no cross-capture).
- Substitution through nesting: `Array<T>` → `Array<Int>` and deeper nests.
- Requirements are ID-based: `where T is Hashable` stores `TypeParamId` subject.
- Length mismatch diagnostic for explicit `<type ...>` call args.
- Explicit `<type ...>` instantiation substitutes param/return types in the checker.

## Exit criteria
- Signatures carry `TypeParamId` and types can contain `TypeVar`.
- Explicit `<type ...>` instantiation substitutes parameter/return types via `Subst`.
- Trait requirements store `TypeParamId` subjects (not strings).

## Phase G3 — Explicit instantiation everywhere + minimal inference

### G3.0 Decisions
- Explicit instantiation works for all callable forms (free functions, methods, qualified members/ctors, function values).
- Minimal inference only; if any type params remain unsolved, emit an error with `<type ...>` guidance.

### G3.1 Explicit instantiation coverage
- Qualified members/ctors:
  - `Type<T>::Ctor(...)` and `Type::Ctor<type T>(...)` both build `Subst` and instantiate param/return types.
  - Type-arg count mismatch points at the `<type ...>` span.
- Methods:
  - Support `obj.method<type T>(...)` (and static/associated forms if present).
  - Instantiate method signatures before argument checks.
- Function values:
  - Allow `<type ...>` only when the function value is generic; otherwise error.

### G3.2 Minimal inference
- Per-candidate solve:
  - Bind `TypeVar` params from argument types.
  - Use expected type to bind return-only generics when available.
  - If any params remain unbound, candidate fails with "cannot infer" (no partial instantiation).
- If multiple candidates succeed, require a unique winner or emit ambiguity with `<type ...>` guidance.

### G3.3 Trait requirements
- After substitution (explicit or inferred), enforce `T is Trait` using the solver.
- If the solver cannot decide, emit a clear error (do not silently accept).

### Tests (G3)
- Inference from args: `id<T>(x)` infers `T` from arguments.
- Inference from expected return type: `make<T>()` with expected `Int`.
- Composite propagation: `Array<T>` or `Optional<T>` binds `T` via args.
- Qualified ctor inference: `Optional::None()` with expected type, plus explicit forms.
- Conflicting inference: `f<T>(a: T, b: T)` rejects mixed types.
- Overload interaction: generic vs non-generic is deterministic or errors with guidance.
- Trait requirement enforcement after substitution.

## Phase G3.S1 — Struct generics

### G3.S1.1 Struct schema + instantiation
- Add struct type params: `struct Box<T> { value: T }`.
- Store struct base schemas with template field types using `GenericTypeExpr`.
- Instantiate struct types with `Box<Int>` → concrete instance type.
- Instances are concrete only (no TypeVar in `ensure_struct_instantiated`).

### G3.S1.2 Type resolution + constructors
- Resolve `Box<Int>` in type positions via `ensure_struct_instantiated`.
- Struct constructors accept explicit call-site type args: `Box<type Int>(...)`.
- Missing type args for generic structs emit a clear error with `<type ...>` guidance.

### Tests (G3.S1)
- `struct Box<T> { value: T }` with `val b: Box<Int> = Box<type Int>(1); b.value`.
- Nested args: `Box<Array<String>>`.

## Phase G3.S2 — Generic impl matching

### G3.S2.1 Impl targets
- Parse impl type params: `implement<T> Box<T> { ... }`.
- Resolve impl target base id + template type args (TypeVars owned by impl).

### G3.S2.2 Method resolution
- Match receiver type args against impl target template.
- Apply impl substitution to method signature before method-level instantiation.
- Reject ambiguous impl matches explicitly.

### Tests (G3.S2)
- `implement<T> Box<T> { fn get(self: Box<T>) returns T }` with `Box<Int>.get()`.
- Mismatch/ambiguity cases produce diagnostics.
