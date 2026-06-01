# String interpolation with f-strings

author: Sławomir Liszniański; created: 2025-12-13

## Goal

Add `f"..."` string interpolation (f-strings) to Drift as **syntax sugar** that stays compatible with a future pluggable formatting engine. The language standardizes only:

- The surface syntax inside `f"..."` (holes, escaping, spec string).
- A minimal, stable lowering contract (one stdlib hook).

Everything else (supported format specs, global engine configuration, performance strategy) can evolve in the standard library and runtime.

Non-goals (MVP):

- `printf`-style formatting.
- Implicit `print(any)` overloads.
- Locale-aware formatting.
- User-defined formatting specs beyond a small built-in subset.

---

## Spec changes

### New literal form: f-string

Introduce a new string literal kind:

- **f-string**: `f"..."` and (if supported) `f'...'` mirrors existing string literal quoting rules.

An f-string is composed of:

- Literal text segments
- **Holes**: `{ <expr> [ ":" <spec> ] }`

Where:

- `<expr>` is any Drift expression.
- `<spec>` is a compile-time substring (no nested `{}` in MVP).

### Escaping braces

Inside an f-string:

- `{{` produces a literal `{`
- `}}` produces a literal `}`

A single unescaped `{` or `}` is a compile-time error.

### Evaluation order

Holes are evaluated left-to-right in source order, exactly once each. Literal segments are not evaluated.

### Typing rules

For MVP, any expression inside a hole must be one of:

- `Bool`
- `Int` / `Uint` (and optionally fixed-width ints)
- `Float` (and optionally `F32`/`F64`)
- `String`

If the type is not supported, the compiler reports an error indicating that the value is not formattable in an f-string (future revisions may allow user-defined formatting via traits/interfaces).

### Format spec string (MVP)

`{expr:spec}` includes an optional specifier string. For MVP:

- The `spec` is treated as an opaque compile-time string that is validated by the formatter against the value type.
- If `spec` is present but unsupported for the type, it is a compile-time error.

Recommended MVP spec subset:

**Int/Uint**
- `""` (empty) → decimal
- `x` / `X` → hex lowercase/uppercase (no `0x` prefix)
- `<width>` or `0<width>` where width is digits:
  - `8` → right-aligned width 8, space padded (optional)
  - `08` → zero padded width 8
  - (If you want only one: implement `0<width>` only.)

**Float**
- `""` → default deterministic representation
- `.N` → fixed precision with N digits after decimal (e.g. `.3`)
- `e` / `E` → scientific notation (optional)

**Bool**
- only `""` → `true` / `false`

**String**
- only `""` → raw string contents

### Lowering contract (stable hook)

The compiler must lower any f-string to one standard-library function call (stable ABI at the Drift level):

```
std.fmt.interpolate(parts: Array<String>, holes: Array<std.fmt.Hole>) -> String
```

Where `parts.len == holes.len + 1`.

`std.fmt.Hole` is a stdlib-defined container holding:
- the value to format
- the spec string (possibly empty)

In MVP, value passing can be modeled as a closed variant in stdlib:

```
variant std.fmt.FmtValue {
    Bool(value: Bool)
    Int(value: Int)
    Uint(value: Uint)
    Float(value: Float)
    String(value: String)
}

struct std.fmt.Hole {
    value: std.fmt.FmtValue,
    spec: String
}
```

Later, `FmtValue` can be replaced with a trait/object-based approach without changing f-string syntax (only the lowering target changes).

### Diagnostics (names suggested)

- `E-FSTR-UNBALANCED-BRACE`: unescaped `{` or `}` in f-string.
- `E-FSTR-EMPTY-HOLE`: `{}` is invalid; hole must contain an expression.
- `E-FSTR-UNSUPPORTED-TYPE`: hole expression type not supported in MVP.
- `E-FSTR-BAD-SPEC`: invalid/unsupported `:spec` for the hole type.
- `E-FSTR-NESTED`: nested `{` inside a spec (MVP disallow).

---

## Grammar changes

### Lexer / tokens

Add recognition of f-string prefix:

- A token sequence `f` immediately followed by a string literal delimiter starts an f-string literal.
- Whitespace between `f` and the quote is not allowed.

### Parser productions (high-level)

Add:

- `FStringLiteral := 'f' StringQuote FStringBody StringQuote`

Within `FStringBody`:

- Text run: any characters except `{` or `}` (with existing string escape rules).
- Escaped brace: `{{` or `}}` (becomes literal `{` / `}`).
- Hole: `{ Expr ( ':' SpecText )? }`

For MVP, `SpecText` is a raw substring without `{` or `}`.

### AST additions

Add a new AST node, e.g.:

- `FString(parts: List[StringSegment], holes: List[FStringHole])`
- Or `FString(segments: List[Segment])` where Segment is either Text or Hole.

Where each hole stores:
- expression AST
- optional spec string
- source spans for good diagnostics.

---

## Compiler implementation plan

### 1) AST → HIR

Lower `FString` into a normal call expression:

- Create an `Array<String>` literal for `parts`.
- Create an `Array<std.fmt.Hole>` literal for `holes`.
- For each hole expression:
  - type-check later, but HIR should represent the expression as-is.

Suggested HIR form:

```
HCall(
    callee = HQualifiedName("std.fmt.interpolate"),
    args = [parts_expr, holes_expr]
)
```

This makes f-strings “disappear” early, keeping later stages simple.

### 2) Type checking (MVP)

When checking the lowered call:

- Ensure `std.fmt.interpolate` is resolvable and has the expected signature.
- For each hole, check:
  - the hole expression type is one of the MVP set.
  - the spec is valid for that type (compiler can validate by calling a small compile-time validator, or defer to stdlib and treat invalid spec as runtime error; MVP recommendation: compile-time error for determinism).

Alternative (cleaner long-term):
- Let stdlib define the acceptable specs (compile-time validator exported from stdlib metadata), but that’s more work. MVP can hardcode the validation rules in the compiler.

### 3) Desugaring to `FmtValue`

If using the `FmtValue` variant approach in MVP:

- Wrap each expression into a `FmtValue` constructor:
  - `Bool(v)` / `Int(v)` / `Float(v)` / `String(v)`
- Build each `Hole(value=..., spec=...)`.

### 4) MIR lowering

No special MIR support is required if f-strings lower to regular calls and container literals.
Only ensure:
- Array literal lowering is correct and efficient enough for tests.
- `std.fmt.interpolate` is linked.

---

## Standard library work (std.fmt)

### Module surface

Create `std.fmt`:

- `struct Hole { value: FmtValue, spec: String }`
- `variant FmtValue { Bool(...), Int(...), Uint(...), Float(...), String(...) }`
- `fn interpolate(parts: Array<String>, holes: Array<Hole>) -> String`

Implementation should:

- Use a `StringBuilder`-like internal helper to avoid quadratic concatenation.
- Append `parts[i]`, then formatted hole `i`, for i in 0..holes.len, then `parts[last]`.

For MVP, `interpolate` may allocate a new String and append in a loop.

---

## Runtime support (C) needed for core types

### Key decision: where formatting happens

MVP recommendation:

- Implement formatting logic in the runtime (C) for primitives:
  - integer to decimal/hex
  - float to string
- Keep Drift stdlib thin wrappers calling into C.

This keeps the compiler simple and gives predictable output across platforms.

### Required C runtime API

Add these functions (names are suggestions; pick a consistent prefix):

#### Integer formatting

```
DriftString drift_int_to_string(int64_t v);
DriftString drift_uint_to_string(uint64_t v);
DriftString drift_int_to_string_spec(int64_t v, const char* spec_utf8, size_t spec_len);
DriftString drift_uint_to_string_spec(uint64_t v, const char* spec_utf8, size_t spec_len);
```

MVP behavior:
- empty spec => decimal
- x/X => hex
- 0<width> => zero pad (decimal; optionally allow hex with width later)

If you want to avoid parsing spec in C initially, expose separate helpers:

```
DriftString drift_int_to_dec(int64_t v);
DriftString drift_int_to_hex(int64_t v, bool uppercase);
DriftString drift_int_to_dec_pad0(int64_t v, int width);
```

#### Float formatting

Use a deterministic conversion. Options:
- Ryu for shortest-roundtrip (recommended; small, fast, deterministic)
- `snprintf` with a fixed format (less deterministic across libc/targets)

API:

```
DriftString drift_float_to_string(double v);
DriftString drift_float_to_string_spec(double v, const char* spec_utf8, size_t spec_len);
```

MVP specs:
- empty => default (Ryu shortest)
- .N => fixed precision N (can use `snprintf` for fixed if acceptable, but document any platform differences; better: use a known algorithm/library)
- e/E => scientific (optional)

#### Bool / String

- Bool can be handled in Drift (`"true"`/`"false"`), no C needed.
- String passthrough needs no formatting.

### Memory / ownership

These runtime functions must return `DriftString` in the ABI you already use (rope header or `{len, ptr}`).
The returned strings must be allocated in a way compatible with Drift string lifetime rules (e.g., runtime-managed heap + destructor via `String` drop).

### Error handling

Formatting functions should be non-throwing and total for MVP:
- Invalid spec should be rejected at compile time.
- If runtime still receives invalid spec (defensive), return a safe placeholder like `"<fmt-error>"` or fall back to default.

---

## Testing plan

### Parser tests
- `f"hello"` parses as `FString` with one part, zero holes.
- Escapes: `f"{{}}"` → `"{}"`.

### Typechecker tests
- `f"{1}"` OK
- `f"{some_struct}"` → `E-FSTR-UNSUPPORTED-TYPE`
- `f"{1:wat}"` → `E-FSTR-BAD-SPEC`

### Codegen/e2e tests
- Basic: `println(f"i={10} b={true} s={"x"}")`
- Hex/pad: `println(f"{255:x} {7:03}")` => `ff 007`
- Float: `println(f"{1.5}")`, `println(f"{1.2345:.2}")` => `1.23` (define expected rounding)

---

## Compatibility and evolution notes

- Only `f"..."` syntax and the lowering hook name/signature should be treated as stable.
- The set of supported `:spec` features can expand without breaking existing code.
- In the future, `FmtValue` can be replaced with a trait-based `Formattable` without changing f-string syntax (only the lowering target or Hole representation changes).
- If you later add a global formatter engine, implement it behind `std.fmt.interpolate` (or a secondary internal hook) so the compiler remains unchanged.
