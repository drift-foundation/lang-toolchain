# Change request: Introduce `Fn(...) -> T` function types (type-only parameters)

**Status:** proposed  
**Audience:** parser/compiler, spec authors  
**Primary goal:** replace the current *function-type* syntax (`fn(...) -> T`) with a clearer, non-repetitive type form using `Fn` and `->`.

---

## 1. Summary

Today Drift uses `fn` both for **function declarations** and **function types**. Function types repeat `fn` and the return arrow, which becomes noisy when returning functions:

```drift
fn my_fun(x: Int) -> Fn(Int) -> Int { ... }  // noisy
```

This change introduces a dedicated *type constructor*:

```drift
Fn(Int) -> Int
Fn(Fn(Int) -> Int) -> Int
Fn(Int) -> Fn(Int) -> Int
```

Key rule: **function-type parameters are type-only** (no names), even if ordinary function declarations keep named parameters.

---

## 2. Motivation

### 2.1. Readability for nested function types
`->` repeats at each nesting level and quickly becomes hard to scan. `Fn(...) -> T` stays compact and conventional.

### 2.2. Cleaner grammar and diagnostics
Using a distinct type constructor avoids overloading `fn` in type position and improves parse anchors and error recovery.

### 2.3. Forward compatibility
`Fn` provides a natural place to hang future callable qualifiers/effects (e.g., `nothrow`, calling conventions), without complicating declaration syntax.

---

## 3. Non-goals

- This change **does not** alter function declaration return syntax (already `-> Ty`).
- This change **does not** change lambda syntax or callable traits/interfaces.
- This change **does not** introduce named parameters in function types (explicitly rejected).

---

## 4. Surface syntax changes

### 4.1. New function type form
A function type is written:

```drift
Fn(<ty0>, <ty1>, ...) -> <ret_ty>
Fn() -> <ret_ty>
```

Examples:

```drift
val f: Fn(Int) -> Int = abs;
val g: Fn(Int, Bool) -> String = pick;
val h: Fn(Fn(Int) -> Int) -> Int = higher_order;
```

Associativity note: `Fn(A) -> Fn(B) -> C` parses as `Fn(A) -> (Fn(B) -> C)` because `->` only appears inside a single `FnType`. The other grouping must be written explicitly as `Fn(Fn(A) -> Fn(B)) -> C`.

### 4.2. (Optional in this change) `nothrow` on function types
To preserve current expressiveness (since function types currently use `ReturnSig`), allow:

```drift
Fn(Int) nothrow -> Int
```

If included, the rule mirrors existing ordering constraints for `ReturnSig`:
- `nothrow` (if present) appears **before** the return arrow.
- `Fn(Int) -> Int nothrow` is invalid.

If you want to keep this change smaller, you can defer `nothrow` on function types; but then you must update places that currently rely on `Fn(...) nothrow -> T` in type position (notably `cast<...>` examples in the spec).

---

## 5. Grammar impact (normative)

This section specifies the minimal grammar deltas required.

### 5.1. Current grammar (excerpt)
From the authoritative grammar, types are currently:

```ebnf
Ty           ::= RefType | FnType | BaseType
FnType       ::= "fn" "(" (Ty ("," Ty)*)? ")" ReturnSig
ReturnSig    ::= "nothrow"? "->" Ty
```

and function declarations are:

```ebnf
FnDef        ::= "fn" Ident TypeParams? "(" Params? ")" ReturnSig RequireClause? Block
Params       ::= Param ("," Param)*
Param        ::= Ident ":" Ty
```

### 5.2. Proposed grammar (excerpt)

#### 5.2.1. Replace `FnType` with `FnType`
Introduce a new function type production keyed by `Fn` and `->`:

```ebnf
Ty           ::= RefType | FnType | BaseType

FnType       ::= "Fn" "(" (Ty ("," Ty)*)? ")" FnReturn
FnReturn     ::= "nothrow"? "->" Ty   // (optional nothrow, see §4.2)
```

#### 5.2.2. Deprecate or remove the old `fn(...) ReturnSig` type form
Option A (clean): **remove** the old function type form:

```diff
- FnType       ::= "fn" "(" (Ty ("," Ty)*)? ")" ReturnSig
+ FnType       ::= "Fn" "(" (Ty ("," Ty)*)? ")" FnReturn
```

Option B (transition): keep old `fn(...) ReturnSig` as a **deprecated alias** for one release:
- Parser accepts both.
- Formatter pretty-prints to `Fn(...) -> T`.
- Linter emits a warning when the old form is used.

---

## 6. Lexical / keyword considerations

### 6.1. Is `Fn` a keyword?
To make parsing and tooling predictable, treat `Fn` as a **reserved keyword** (like `Int`/`String`) and disallow it as an identifier in all positions. In type position, `Fn` must appear as `Fn(...) -> T` (bare `Fn` is invalid).

Implementation choices:

- **Preferred:** add `"Fn"` to the keyword set (token-level keyword).
  - Pros: no user-defined type named `Fn`; simplest parsing.
  - Cons: adds one more reserved word (but it aligns with your “types are capitalized” convention).

- **Alternative:** keep `Fn` as an identifier token, but in `Ty` parsing treat `NAME == "Fn"` followed by `(` as the `FnType` introducer.
  - Pros: fewer keywords.
  - Cons: `struct Fn { ... }` becomes context-dependent/ambiguous; more complexity, worse error messages.

### 6.2. Token list updates
No new punctuation is introduced (the grammar already lists `->` as a token). If `Fn` becomes a keyword, update the reserved keyword list accordingly.

---

## 7. Spec touch points (non-normative but should be updated)

These are examples and explanatory sections that should be updated to match the new syntax.

### 7.1. `cast<T>(expr)` examples for function types
The spec currently demonstrates function-type casts using the old `Fn(...) nothrow -> T` type literal. Update to:

```drift
val f = cast<Fn(Int) nothrow -> Int>(abs);
```

(If you choose to defer `nothrow` on function types, then either remove `nothrow` from this example or provide a different disambiguation story.)

### 7.2. Closure/callable chapters
Any text referring to “function pointers are written as `Fn(Args...) -> R`” should be revised to “`Fn(Args...) -> R`”, while keeping `fn` as the declaration keyword.

---

## 8. Migration plan

1. **Parser:** accept `Fn(...) -> T` immediately.
2. **Transition (recommended):** accept the old `Fn(...) -> T` type form with a deprecation warning for one cycle.
3. **Formatter:** normalize function types to `Fn(...) -> T`.
4. **After transition:** remove the old form and make it a hard parse error with a clear diagnostic and fix-it.

Suggested diagnostic text:
- `E-SYNTAX-FNTYPE-OLD: function types use 'Fn(...) -> T' (not 'Fn(...) -> T')`

---

## 9. Test cases to add

### 9.1. Parsing / pretty printing
- `val f: Fn(Int) -> Int = abs;`
- `val f: Fn() -> Int = get;`
- `val f: Fn(Fn(Int) -> Int) -> Int = hof;`
- `val f: Fn(Int) -> Fn(Int) -> Int = curry;`

### 9.2. Rejection (type-only params)
These should be rejected in *type* position:

- `Fn(x: Int) -> Int`  // names not allowed
- `Fn(self: &T) -> Int` // no named receivers in types

### 9.3. `nothrow` placement (if enabled)
- accept: `Fn(Int) nothrow -> Int`
- reject: `Fn(Int) -> Int nothrow`

---

## 10. Notes / open decisions (explicit)

- **Decision A:** Reserve `Fn` as a keyword vs context-sensitive identifier.
- **Decision B:** Allow `nothrow` in function types now (recommended) vs defer.
- **Decision C:** Transition period vs immediate break.

---

## Appendix: One-line before/after examples

Before:
```drift
fn my_fun(x: Int) -> Fn(Int) -> Int { ... }
val f = cast<Fn(Int) nothrow -> Int>(abs);
```

After:
```drift
fn my_fun(x: Int) -> Fn(Int) -> Int { ... }
val f = cast<Fn(Int) nothrow -> Int>(abs);
```
