# Named-field variant construction + patterns (MVP)

## Goal

Add ergonomic, unambiguous named-field support for variant constructors in both:

- **Construction** (calls): `Optional::Some(value = 1)`
- **Patterns** (match arms): `Some(value = v)`

…while keeping lowering deterministic and preventing long-term ambiguity between:

- `Ctor` (bare)
- `Ctor()` (explicit parens)
- `Ctor(a, b)` (positional)
- `Ctor(x = a, y = b)` (named)

## Pinned MVP semantics

### Construction (variant ctor calls)

- Named args are allowed for variant constructor calls.
- **No mixing** positional and named arguments in the same ctor call.
- Named args are order-independent.
- **All fields must be provided** (no defaults).
- Unknown / duplicate / missing field is an error.
- Zero-field ctors allow `Ctor()` only; `Ctor(x=...)` is an error.

### Patterns (match arms)

- `Ctor()` means: **match ctor tag only; ignore payload** (works for any arity).
- Named binders are allowed and may be a **subset** of fields.
- **Bare `Ctor` is allowed only for zero-field ctors**:
  - `None` ok, `None()` ok
  - `Some` is an error if `Some` has fields
  - `Some()` ok (tag-only)
- **Positional patterns require exact arity** (pinned):
  - `Ctor(a,b)` must bind exactly all fields for that ctor.
  - For “bind none”, use `Ctor()`.
- No mixing positional and named binders in the same pattern.

## Compiler shape pins (architecture)

### Single source of truth for mapping/ordering

- The typed checker is the authoritative place that:
  - validates ctor args and patterns,
  - maps named fields to declared field indices,
  - normalizes construction args into declared field order,
  - normalizes pattern binders into a stable `(binder_name, field_index)` mapping.
- Stage2 lowering must be **assert-only** for these invariants (no re-derivation).

### Explicit pattern argument form in HIR

To prevent ambiguity and future churn, the HIR arm pattern carries:

- `pattern_arg_form: "bare" | "paren" | "positional" | "named"`

This distinguishes `Ctor` vs `Ctor()` even when binders are empty.

## Work plan

### 1) Parser + stage0 AST

- Extend match-arm pattern parsing to support:
  - `Ctor()` (paren form)
  - `Ctor(field = binder, ...)` (named binders)
- Preserve the pattern argument form through AST.
- Ensure “mix positional + named” is rejected at parse/AST build time (or yields a clear parse diagnostic).

### 2) Stage1 HIR

- Extend `HMatchArm` to carry:
  - `pattern_arg_form`
  - `binder_fields` (only for named)
- Thread these through `AstToHIR` (no semantic decisions here).
- Ensure binder alpha-renaming continues to work for named binders (uses `binders` list).

### 3) Typed checker

#### 3.1 Constructor calls (named args)

- Remove the “variant constructors do not support keyword arguments” MVP restriction.
- Add a shared helper to map named args to field order:
  - errors: unknown/dup/missing/mix positional+named
- Normalize ctor calls so stage2 sees positional args in field order (no kwargs).

#### 3.2 Match patterns (named binders + Ctor())

- Validate pattern forms against scrutinee variant instance:
  - `bare` only if zero-field ctor
  - `paren` always ok (tag-only)
  - `positional` requires exact arity
  - `named` can be subset; validate unknown/dup fields
- Normalize binders to a stable field-index list:
  - `binder_field_indices: list[int]` parallel to `binders`

### 4) Stage2 lowering

- Constructor calls:
  - assert variant ctor calls have no kwargs and args are in declared order
- Match lowering:
  - `paren`: no field extraction
  - `positional/named`: emit `VariantGetField` per binder using normalized field indices

### 5) Tests (e2e first, then unit)

Must-have e2e:

- Positive:
  - named ctor construction: `Optional::Some(value = 1)` + named pattern bind
  - tag-only pattern: `Some()` matches and ignores payload
  - zero-field: `None` and `None()` both accepted
- Negative:
  - `Some` (bare) rejected for non-zero-field ctor
  - named binder on zero-field ctor rejected (`None(value = v)`)
  - ctor call missing field rejected
  - unknown/duplicate field rejected (construction + pattern)
  - mixing positional+named rejected (construction + pattern)

## Status

- Completed

Implemented and covered by e2e:

- Named-field variant construction: `Optional::Some(value = 1)` (no mixing with positional; all fields required; unknown/dup/missing rejected).
- Named-field variant patterns: `Some(value = v)` (subset allowed; unknown/dup rejected).
- Tag-only pattern: `Ctor()` matches constructor tag and ignores payload (any arity).
- Bare pattern restricted to zero-field ctors only (`None` ok; `Some` rejected when `Some` has fields).

Maintenance / invariants (post-implementation hardening):

- Stage2 `match` lowering is now mechanically safe for value-producing matches: an arm that falls through to the join must store the match result; otherwise it must terminate (asserts as “checker bug” / “lowering bug”).
- Stage2 no longer silently re-derives `binder_field_indices` in strict mode; missing indices are treated as a checker bug. The stub/test pipeline uses an explicit opt-in fallback to keep untyped fixtures runnable without weakening the production contract.

This tracker can be retired; deferred follow-ups (if any) belong in `TODO.md` under `[Iteration]`.
