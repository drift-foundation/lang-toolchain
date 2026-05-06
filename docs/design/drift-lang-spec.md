# Drift Language Specification 1.0
<!-- CANONICAL-SPEC -->

## 1. Overview

Drift is a modern systems language built on a simple premise: programming should be pleasant, expressive, and safe by default — without giving up the ability to write efficient, low-level code when you actually need it.

Most languages pick a side:

- High-level and comfortable, but slow when you push the limits.
- Low-level and risky, but fast if you fight the compiler hard enough.

Drift rejects that binary. You get a single language that works across the entire performance spectrum.

### 1.1. Safety first, without sacrificing power

Drift avoids the foot-guns that plague many systems languages:

- No raw pointers in userland.
- No pointer arithmetic.
- Clear ownership and deterministic destruction (RAII).
- Explicit copies; moves are implicit in consuming positions.

Yet it doesn’t enforce safety by making everything slow or hiding costs behind a garbage collector.

### 1.2. Escape hatches when you ask for them

High-level code stays high-level by default. Low-level control appears only when you deliberately reach for the tooling (`lang.abi`, `lang.internals`, `@unsafe`).

### 1.3. Move semantics everywhere

Passing by value consumes unless the type is `Copy` (in which case the compiler may duplicate implicitly). Moves are implicit in consuming positions; duplication is explicit.

### 1.4. Zero-cost abstractions

Drift’s abstractions compile down to what you would hand-write. Ownership, traits, interfaces, and concurrency are "pay for what you use."

### 1.5. Ready out of the box, no hidden machinery

The language ships meaningful tools (structured errors, virtual threads, collection literals) without magic or implicit globals. Everything is imported explicitly.

## 2. Expressions (surface summary)

Drift expressions largely follow a C-style surface with explicit ownership rules:

- Function calls: `f(x, y)`
- Attribute access: `point.x` (owned value), `ptr->x` (through `&T` / `&mut T`)
- Indexing: `arr[0]`
- Unary operators: `-x`, `not x`, `!x`
- Binary operators: `+`, `-`, `*`, `/`, comparisons (`<`, `<=`, `>`, `>=`, `==`, `!=`), boolean (`and`, `or`)
- Bitwise operators: `&`, `|`, `^`, `~`, `<<`, `>>` — **require `Uint` operands**; using any other type (including `Int`, `Bool`, `String`, or arrays) is a type error.
- Ternary conditional: `cond ? then_expr : else_expr` (lower precedence than `or`; `cond` must be `Bool`, and both arms must have the same type)
- Pipeline: `lhs |> stage` (left-associative; lower precedence than `or` and higher than ternary; stages are calls/idents)
- Move expression: `move x` transfers ownership
- Explicit cast: `cast<T>(expr)` (strict; function-type casts only in v1)
- Array literals: `[1, 2, 3]`
- String concatenation uses `+`
- Public String byte length is exposed as method `s.byte_length() -> Int` (UTF‑8 code units, not characters).
- Empty strings may be written as `""` or `String.EMPTY`. A convenience helper `is_empty(s: String) -> Bool` checks `s.byte_length() == 0`.
- Program entry (v1): exactly one `main` function, returning `Int`, **declared `nothrow`**, with one of two signatures:
  - `fn main() nothrow -> Int`
  - `fn main(argv: Array<String>) nothrow -> Int` (argv includes the program name at index 0). The runtime builds `argv` and calls this `main`; no drift_main indirection in user code.
  - The executable entrypoint is `main::main`. For entry-bearing programs, the module must be `main`; if the module header is omitted, it defaults to `main`.
  - `main` is only allowed in the **root package**; dependency packages must not define a `main`.

### 2.x. Receiver placeholder (`.foo`, `.foo(...)`)

When calling a method on a receiver expression `R`, a leading-dot placeholder lets arguments reuse that same receiver without re-evaluating it:

```drift
R.method(.field, .other_method(), .index)
```

Semantics:

- `.name` desugars to `R.name`.
- `.name(a, b)` desugars to `R.name(a, b)`.
- The receiver `R` is evaluated **exactly once** and reused for the main call and all leading-dot arguments. Conceptually:

  ```drift
  R.method(.a(), .b)
  ```

### 2.y. Explicit cast expression

`cast<T>(expr)` is an explicit, compile-time checked conversion.

**Numeric casts** — `cast<T>(expr)` between integer/float types is narrowing-permissive: the compiler must not reject an explicit narrowing cast solely due to overflow risk. For integer-to-integer casts where the target width is N bits, the result is `expr mod 2^N` (low N bits retained); signedness is interpreted by the target type after truncation. This applies to runtime values; `const` initializers currently require literal-only expressions and do not support `cast<T>(...)` (see §3.9), but when const-expr evaluation gains cast support, these same truncation semantics will apply. Float-to-int and int-to-float edge-case behavior is implementation-defined per target; tests should pin current behavior for stability.

**Function casts** — `cast<T>(expr)` where T is a function type disambiguates overloads when taking a function reference:

```drift
  val f = cast<Fn(Int) nothrow -> Int>(abs);
```

No thunking or adapter insertion occurs; unsupported cast shapes are rejected.

### 2.z. Macro direction

Drift macro support is language-aware and **not** a C-style textual preprocessor.

- Macros parse source into AST first, then expand macro invocations into ordinary AST nodes **before type checking**.
- Expansion is deterministic and diagnostics are source-anchored at macro call sites and expanded nodes.
- Macro support is compiler-owned/built-in only (e.g., `log.info!`, `log.debug!`, `log.error!`).

Design intent:

- Avoid text-substitution pitfalls from preprocessor-style macros.
- Preserve clear tooling, diagnostics, and predictable compilation semantics.

  behaves like:

  ```drift
  val __recv = R;
  __recv.method(__recv.a(), __recv.b);
  ```

Where leading-dot is valid:

- Inside method-call argument lists (`R.method(...)`), including in nested sub-expressions.
- Inside an index expression `R[expr]` **only when** `expr` is a leading-dot form; `R[.len]` means `R[R.len]`.

Scoping / nesting:

- Leading-dot binds to the immediately enclosing receiver. Nested calls each have their own receiver placeholder. For example:

  ```drift
  outer().a(.x, inner().b(.y));
  ```

  desugars to:

  ```drift
  val __outer = outer();
  val __inner = inner();
  __outer.a(__outer.x, __inner.b(__inner.y));
  ```

All arguments still evaluate left-to-right; the only special rule is reuse of the already-evaluated receiver.

### 2.1. Predictable interop

Precise binary layouts, opaque ABI types, and sealed unsafe modules keep foreign calls predictable without exposing raw pointers.

### 2.2. Representation transparency only when requested

Everyday Drift code treats core types as opaque. When you need to see the layout, you opt in via `extern "C"` or `lang.abi` helpers.

### 2.3. Performance without fear

Write clear code first. When you profile a hotspot, the language gives you the tools to optimize surgically without rewriting everything in C.

### 2.4. A language for both humans and machines

Drift emphasizes predictability, clarity, and strong guarantees so humans can reason about programs—and so tooling can help without guesswork.

### 2.5. Signed modules and portable distribution

All modules compile down to a canonical Drift Module IR (DMIR) that can be cryptographically signed and shipped as a Drift Module Package (DMP). Imports are verified before execution, so every machine sees the same typed semantics and can reject tampered artifacts.

---

## 3. Variable and reference qualifiers

| Concept | Keyword / Syntax | Meaning |
|---|---|---|
| Immutable binding | `val` | Cannot be rebound; may be moved/consumed |
| Mutable binding | `var` | Can mutate; may be moved/consumed |
| Const reference | `&T` | Shared, read-only access (C++: `T const&`) |
| Mutable reference | `&mut T` | Exclusive, mutable access (C++: `T&`) |
| Ownership transfer | `move x` | Moves value, invalidating source |
| Interior mutability | `Mutable<T>` | Mutate specific fields inside const objects |
| Volatile access | `Volatile<T>` | Explicit MMIO load/store operations |
| **Blocks & scopes** | `{ ... }` | Define scope boundaries for RAII and deterministic lifetimes |

`val`/`var` bindings may omit the type annotation when the right-hand expression makes the type unambiguous. For example, `val greeting = "hello"` infers `String`, while `val nums = [1, 2, 3]` infers `Array<Int>`. Add an explicit `: Type` when inference fails or when you want to document the intent.

Function parameters are `val` by default (owned, immutable). Use `var` for mutable parameters: `fn id(var x: File) -> File { return move x; }`.

### 3.2. Borrow traits and argument coercion

Drift supports **argument-only** coercions via borrow traits. This is intended to improve ergonomics for wrapper types (e.g., `Arc<Mutex<T>>`) without enabling method-receiver auto-deref.

```drift
trait Borrow<T> { fn borrow(self: &Self) nothrow -> &T; }
trait BorrowMut<T> { fn borrow_mut(self: &mut Self) nothrow -> &mut T; }
```

Coercion rules (argument-only):

- If a parameter expects `&T` and the argument has type `X` (or `&X`), and `X: Borrow<T>`, the compiler may insert `borrow(&arg)`.
- If a parameter expects `&mut T` and the argument has type `X` (or `&mut X`), and `X: BorrowMut<T>`, the compiler may insert `borrow_mut(&mut arg)` (or `borrow_mut(arg)` if `arg` is already `&mut X`).
- When a generic function expects `&T`/`&mut T` and the argument is coerced via `Borrow`/`BorrowMut`, the compiler may use the trait argument (the `T` in `Borrow<T>`/`BorrowMut<T>`) to infer the function’s type arguments.
- No coercion for by-value parameters (`T`).
- No method-receiver auto-deref.

**Statement terminators:** Simple statements end with `;`. Compound statements that carry a block (`if`/`while`/`for`/`try`/`match`) and standalone block statements (`{ ... }`) are self-terminating. Newlines are whitespace only. In a normal block, an expression statement must be a **postfix expression** (call, member access, index, literal, or name) and end with `;` to avoid ambiguity with statement-form `try`. In a value-producing block (e.g., lambda bodies, match arms, try/catch expression arms), the final expression must **not** end with `;`.

### 3.1. Primitive palette

| Type    | Description |
|---------|-------------|
| `Bool`  | Logical true/false. |
| `Int`   | Signed two’s-complement integer of the platform’s natural word size. Guaranteed to be at least 32 bits. |
| `Uint`  | Unsigned integer of the platform’s natural word size. Same bit-width as `Int`. |
| `Size`  | (Reserved for future revisions) Natural-width unsigned; not used for collection lengths/indices in v1. |
| `Float` | Target-native floating-point scalar (IEEE-754 binary32 or binary64 on supported targets). |
| `Int32` | Fixed-width signed 32-bit integer. Available in all user code for C FFI interop (`int` in C). Supports casts to/from other numeric scalars. |
| `Uint32` | Fixed-width unsigned 32-bit integer. Available in all user code for C FFI interop (`unsigned int` in C). Supports casts to/from other numeric scalars. |
| `Int8`, `Int16`, `Int64` | Fixed-width signed integers, exactly 8/16/64-bit two’s-complement (**reserved in v1; allowed only in `lang.abi.*`**). |
| `Uint8`, `Uint16` | Fixed-width unsigned integers, exactly 8/16-bit (**reserved in v1; allowed only in `lang.abi.*`**). |
| `Uint64` | Fixed-width unsigned 64-bit integer. Available in all user code for portable 64-bit unsigned arithmetic (crypto, hashing, bit manipulation). Supports all bitwise operators (`&`, `\|`, `^`, `~`, `<<`, `>>`) and their augmented-assignment forms. |
| `F32`, `F64` | IEEE-754 binary32 and binary64 floating-point types (**reserved in v1; allowed only in `lang.abi.*`**). |
| `Byte` | Unsigned 8-bit value (v1 surface scalar; `Uint8` is reserved); used for byte buffers and FFI. |
| `String` | Immutable UTF-8 string (shared backing). |

`Byte` gives Drift APIs a canonical scalar for binary data. Use `Array<Byte>` (or the dedicated buffer types described in Chapters 6–7) when passing contiguous byte ranges.

`Byte` is a v1 surface type even though other fixed-width scalars are reserved; `Uint8` remains reserved outside `lang.abi.*`.

#### 3.1.2. String semantics (v1)

- Storage is UTF-8; **bytewise** semantics:
  - `s.byte_length() -> Int` returns the number of UTF-8 code units (bytes), not graphemes.
  - Global `byte_length(...)` is internal-only (`std.*` implementation surface), not part of user-facing API.
  - Equality (`==`) is bytewise; no normalization or case folding.
- Empty strings: `""` or `String.EMPTY`; `is_empty(s: String) -> Bool` checks `s.byte_length() == 0`.
- Concatenation uses `+` and produces a new `String`.
- `Array<String>` is supported; each element is a `%DriftString` header `{%drift.isize, i8*}` at the ABI.

#### 3.1.1. Integer and float semantics

**Literal forms:**

- Decimal integers: `42`, `0`, `1000000`
- Hexadecimal integers: `0xFF`, `0x00200000`, `0XAB` (case-insensitive prefix)
- Unsigned suffix: `42u`, `0xFFu` (Uint literal)
- Unsigned 64-bit suffix: `42u64`, `0x10u64` (Uint64 literal)
- Float: `3.14`, `1.0e10`, `2.5E-3` (decimal only; no hex float)

Drift distinguishes between **natural-width** numeric primitives and **fixed-width** primitives.

- **v1 uses pointer-sized carriers** for `Int`/`Uint` (isize/usize). This avoids wasting space on 32-bit targets and keeps arithmetic efficient.
- `Size` is not available in v1; collections use `Int` for lengths, capacities, and indices (see chapter 12).
- `Float` is the target’s native floating-point type (most commonly IEEE-754 binary64; on some targets it may be binary32). The surface name remains `Float` regardless of width. Its bit-width/layout are target-defined; ABI stability is guaranteed within a target, not across different targets.
- Fixed-width primitives (`Int8`, `Int16`, `Int64`, `Uint8`, `Uint16`, `F32`, `F64`) are **reserved in v1**. They are used only in ABI/FFI modules and internal compiler/runtime types; user code should use `Int`/`Uint`/`Float`. The following fixed-width types are **available in user code**: `Int32` and `Uint32` (for C FFI interop where `int`/`unsigned int` are 32-bit), and `Uint64` (for portable 64-bit unsigned arithmetic). All three support casts (`cast<Int32>(...)`, etc.) to/from other numeric scalars. `Uint64` additionally supports arithmetic, bitwise operators (`&`, `|`, `^`, `~`, `<<`, `>>`), augmented assignment (`&=`, `|=`, `^=`, `<<=`, `>>=`), and comparisons.

Overflow:
- Fixed-width integers use modular two’s-complement wraparound.
- Natural-width integers: debug builds should trap on overflow; release builds may wrap unless the implementation guarantees trapping. Checked helpers (`checked_add`, etc.) may exist in stdlib.

Conversions:
- **Explicit casts are narrowing-permissive.** `cast<T>(x)` is a forceful conversion that may truncate/wrap when `T` has smaller range/width. The compiler must not reject explicit narrowing casts solely due to overflow risk. For integer-to-integer casts where target width is N bits, the result is `x mod 2^N`. Signedness is interpreted by the target type after truncation.
- **Typed literals and `const` declarations are strict.** Typed literals (e.g. `123u`, `42u64`) and `const` declarations enforce exact range validation for the declared type. Out-of-range literals are rejected at compile time. This is separate from cast semantics: `const x: Uint = 184467...u` is rejected if out of range, but `cast<Uint>(large_value)` is allowed truncation per cast rule (once const-expr evaluation supports casts; currently v1 rejects non-literal const initializers).
- **Checked conversions (future).** `std.num` will provide checked conversion APIs (e.g. `to_uint_checked(x: Int) -> Result<Uint, ConversionError>`) that fail on out-of-range instead of truncating.
- **Diagnostics.** No overflow diagnostic for explicit `cast<T>(...)` narrowing. Diagnostics are emitted for: invalid cast shape/type category, invalid typed literal ranges, and invalid checked conversion calls (when provided).
- Fixed-width conversions are reserved until the fixed-width primitives are enabled.
- `Size` is not available in v1; use `Uint` in place of `Size`.
- Floating conversions follow IEEE-754 rules on supported targets; other targets use the platform’s native float behavior.

#### 3.1.3. Void semantics

`Void` is the unit type. Functions declared `-> Void` return no meaningful value.

- `Void` values may be bound (`val x = void_fn()`) and assigned (`x = void_fn()`). This is required for generic code where a type parameter may be instantiated with `Void` (e.g., `Result<Void, E>`, `Callback0<Void>`).
- `Void` is `Copy` and has no destructor. At the ABI level it is represented as an unused `i8` slot.
- Returning a non-`Void` value from a `Void` function is a compile-time error.
- Explicitly declaring a binding with type annotation `Void` (`val x: Void = ...`) is a compile-time error.
- `Void` values may not be used where a non-`Void` type is expected; normal type-mismatch rules apply.

#### 3.1.4. Comments

Drift supports two comment forms:

```drift
// Single-line comment through the newline
val greeting = "hello";

/* Multi-line
   block comment */;
fn example() -> Void { ... }
```

Block comments may span multiple lines but do not nest. Comments are ignored by the parser, so indentation/terminator rules treat them as whitespace.

### 3.3. Console helpers (`std.console`)

Console helpers are provided by the standard module `std.console` and must be
imported explicitly.

```drift
module std.console;

/// Writes UTF-8 text to the process standard output.
/// Does not append a newline.
fn print(text: String) -> Void

/// Writes UTF-8 text to the process standard output,
/// then appends a single '\n'.
fn println(text: String) -> Void

/// Writes UTF-8 text to the process standard error.
/// Does not append a newline.
fn eprint(text: String) -> Void

/// Writes UTF-8 text to the process standard error,
/// then appends a single '\n'.
fn eprintln(text: String) -> Void
```

Notes:

- Inputs to `print`/`println`/`eprint`/`eprintln` must be `String` (UTF-8).
- `print` writes to stdout without a trailing newline.
- `println` writes to stdout and appends exactly one `\n`.
- `eprint` writes to stderr without a trailing newline.
- `eprintln` writes to stderr and appends exactly one `\n`.
- In v1 these functions are `nothrow` and best-effort: if write fails or times
  out, they return without raising.
- Console writes are implemented on top of `std.io` configured output streams
  using nonblocking/reactor-backed write loops with bounded timeout.
- They perform no formatting beyond what you compose yourself.

### 3.4. Type prelude (always-on in v1)

Drift provides a small, always-on **type prelude** for core types that appear
pervasively in annotations. This is distinct from the value prelude above.

The type prelude includes:

- `Int`, `Uint`, `Byte`, `Bool`, `Float`, `String`, `Void`, `Error`
- `Array<T>`
- `Optional<T>`
- `FnResult<ok, err>`
- `&T`, `&mut T` (reference type constructors)

The diagnostic value model is `JsonNode` / `JsonHandle` from `std.json.value` (§5.13.8). These are not in the type prelude — diagnostic-context surfaces import them explicitly. The legacy `DiagnosticValue` type is no longer part of the public surface; see §5.13.8.

Memory utilities live in `std.mem` and are **not** auto-imported. In particular:

- `std.mem.swap(a, b)` exchanges two addressable places.
- `std.mem.replace(place, value)` writes a new value and returns the old one.

These names are not reserved; they must be referenced through an explicit
module path or `import std.mem as mem; mem.swap(...)`.

Typical usage:

```drift
println("hello, world");
```

### 3.5. Struct syntax variants

```drift
struct Point {
    x: Int,
    y: Int
}

struct Point(x: Int, y: Int);  // header form; identical type
```

The tuple-style header desugars to the block form. Field names remain available for dot access while the constructor supports positional and named invocation. The resulting type follows standard Drift value semantics: fields determine copy- vs move-behavior, and ownership transfer still uses `move foo` as usual.

---

### 3.5. `val` fields as type-level constants

```drift
struct Test {
    val GAME_CTRL_UP:   Int = 1;
    val GAME_CTRL_DOWN: Int = 2;
}
```

Rules:

- A `val` field in a `struct` is a **type-level constant**, not per-instance storage.
- `val` fields do **not** contribute to the struct’s runtime layout or `size_of<T>()`.
- A struct that contains only `val` fields is a **zero-sized type**; e.g. `size_of<Test>() == 0` above.
- Accessing a `val` field through an instance (`obj.GAME_CTRL_UP`) is equivalent to accessing it through the type (`Test.GAME_CTRL_UP`) and is compile-time constant-foldable.
- `val` fields must be initialized with **compile-time constant expressions**.
- Constant safety: a `val` field may only use a type that:
  - does not implement `Destructible`, and
  - can be fully constructed at compile time (primitives, static `String`, plain structs/variants with const-friendly fields).
  Types requiring runtime destruction or runtime data as initializers are disallowed for `val` fields.

---

### 3.5. Borrow expressions

- `&v` produces a shared reference `&T` from an lvalue `v: T`.
- `&mut v` produces an exclusive mutable reference `&mut T` from a mutable lvalue `v: T`.
- Borrowing from temporaries (rvalues) is a compile-time error; bind to a local first.
- The legacy `ref` / `ref mut` spelling is invalid.

### 3.6. Call-site auto-borrowing (global rule)

For parameters or receivers of type `&T` / `&mut T`, calling with an lvalue `v: T` auto-borrows:

- `g(v)` ≡ `g(&v)` if the parameter is `&T`.
- `h(v)` ≡ `h(&mut v)` if the parameter is `&mut T`.

Borrowing from rvalues (temporaries, moved values) is an error. The explicit forms `&v` / `&mut v` remain legal.

### 3.7. Method receivers and overloading

Receivers inside an `implement` block are written with an explicit mode: `T` (by value), `&T` (shared borrow), or `&mut T` (exclusive borrow):

- A method receiver must be spelled as the **first parameter** and must be named `self` for consistency. Valid forms are `self: T`, `self: &T`, and `self: &mut T`. Any other first-parameter name in an `implement` method is a compile-time error.
- A call on an lvalue prefers `self: &T`, then `self: &mut T`, then `self: T` (copies if `Copy`, otherwise consumes the receiver).
- A call on an rvalue (`move obj`, `make()`) can bind only to a `self` receiver; borrowed receivers are not allowed on rvalues.

These rules keep borrowing consistent across free functions, methods, and control-flow desugarings.

#### 3.8.1. When `self` is by value (consuming receiver)

Using `self: T` makes the method **consume** the receiver: the caller transfers ownership into the method and the original value becomes invalid after the call (unless it was explicitly moved from a temporary).

This is the right design when the API intent is ownership-taking, for example:

- **Conversions**: returning a new owned representation without cloning.
  - `fn into_bytes(self: String) -> Array<Uint>`
- **Resource finalization**: preventing accidental use-after-close.
  - `fn close(self: File) -> Void`
- **Builders that "finish"**: ensuring the partially-built value is not reused.
  - `fn build(self: Builder) -> Product`
- **Draining/extracting owned internals** from move-only types.
  - `fn into_iter(self: Vec<T>) -> VecIter<T>`

If a method does not need ownership, prefer borrowed receivers (`&T` / `&mut T`) so callers can keep using the original value.

---

### 3.8. Unqualified names inside method bodies (no implicit receiver)

Inside a method body, unqualified names resolve exactly the same way they do in free functions:

1. **Local scope**: locals, parameters (including `self`), and any bindings introduced by control flow.
2. **Module scope**: functions, structs, traits, exceptions, and other items visible in the current module scope.

There is **no implicit receiver lookup**. Unqualified identifiers never resolve to fields or methods of `self`. To access members, you must write them explicitly (`self.field`, `self.method(...)`, or `self->field` for borrowed receivers).

Unqualified calls never resolve to methods. `name(args...)` is always a free-function call (or an error if no such function exists). Method calls require an explicit receiver (`self.name(args...)`) or explicit UFCS (`Type::name(self, ...)`, `Trait::name(self, ...)`). Function-style method calls are not inferred.

---

### 3.9. Constants (`const`)

Drift supports constants via `const` at both module scope and block scope.
Constants are immutable bindings whose value is known at compile time; each
use site re-materializes the literal value (no runtime storage is allocated).

#### Module-scope constants

```drift
const ANSWER: Int = 42;
const OK: Bool = true;
const GREETING: String = "hello";
const MASK: Uint = 4294967295u;
const TABLE: Array<Uint> = [1116352408u, 1899447441u, 3049323471u];
```

#### Block-scope constants

```drift
fn example() nothrow -> Int {
    const LIMIT: Int = 100;
    const TAG: String = "ok";
    const WEIGHTS: Array<Int> = [10, 20, 30];
    return WEIGHTS[1];
}
```

Block-scope constants follow the same initializer rules as module-scope
constants but are scoped to the enclosing block (function body, `if`/`while`
body, bare block). They are not exportable and do not participate in module
interface resolution.

Because each use re-materializes the literal, non-Copy types like `String`
may be used at multiple sites without triggering move semantics:

```drift
const S: String = "data";
process(S);   // fresh ConstString materialized
process(S);   // another fresh ConstString — no use-after-move
```

Rules:

- A `const` must have an explicit type annotation.
- The initializer must be a **compile-time literal**:
  - `Int`, `Uint`, `Uint64`, `Bool`, `String`, `Float`, `Byte` literals
  - `Uint` literals with `u` suffix: `42u`, `0u`, `4294967295u`
  - `Uint64` literals with `u64` suffix: `42u64`, `0u64`, `18446744073709551615u64`
  - unary `+` / `-` applied to an integer/float literal
  - `Array<T>` literals where `T` is a scalar type (`Int`/`Uint`/`Uint64`/`Byte`/`Float`/`Bool`) and all elements are compile-time literals; empty arrays are rejected
- Non-literal expressions (`1 + 2`, calls, indexing, interpolation, etc.) are
  rejected in v1.
- **Literal range validation is strict.** Typed literals and `const` initializers
  must fit within the declared type's range. Out-of-range values are rejected at
  compile time:
  - `Byte`: `[0, 255]`
  - `Uint`: `[0, 2^W-1]` where W is the target word size (e.g. 64 or 32)
  - `Uint64`: `[0, 2^64-1]`
  - A `u`-suffix literal (e.g. `42u`) is a `Uint`-typed expression and may appear anywhere an expression is valid. Assigning or binding a `u`-suffix literal to an incompatible declared type (e.g. `const x: Int = 42u`) is a type error.
  - A `u64`-suffix literal (e.g. `42u64`) is a `Uint64`-typed expression. It follows the same rules as `u`-suffix literals but targets the 64-bit unsigned type. `u64` literals may not be used where `Uint` is expected, and vice versa — no implicit coercion between `Uint` and `Uint64`. Unary negation on `u64` literals (e.g. `-1u64`) is a compile-time error (`E-NEG-UNSIGNED`).
- Const arrays are backed by read-only LLVM globals. Each use site materializes
  a `DriftArrayHeader` pointing at the shared global. `.len` works; mutation is
  not allowed.
- Block-scope constants participate in the same shadowing rules as `val`: a
  local `const` may shadow an outer `const`, `val`, or module-level `const`.
- `pub const` is only valid at module scope; `pub` is a parse error inside a
  block.
- Module-scope `const` names participate in import/export and conflict rules
  like other exported values.

Tooling/packaging note:

- When a module exports a constant, its type and literal value become part of
  the module interface recorded in DMIR/DMIR-PKG (§20), so importing modules do
  not need access to source to type-check and inline the constant.


## 4. Ownership and move semantics (`move x`)

`move x` transfers ownership of `x` without copying. After a move, `x` becomes invalid. Equivalent intent to `std::move(x)` in C++ but lighter and explicit.

Restriction: `move` operands must be addressable local/parameter bindings. Moving out of projections (`move x.y`, `move a[i]`) is not supported in v1.

### 4.1. Core rules
| Aspect | Description |
|---------|-------------|
| **Move target** | Must be an owned binding (`val` or `var`). |
| **Copy types** | `x` copies; `move x` moves. |
| **Non-copyable types** | Consuming positions move; `copy x` is a compile error. |
| **Immutable (`val`)** | Cannot rebind; may be moved/consumed. |
| **Borrowed (`&`, `&mut`)** | Cannot move from non-owning references. |

---

### 4.2. Default: move-only types

Every type is **move-only by default**. If you define a struct and do nothing else, the compiler will refuse to copy it; passing or assigning it by value consumes it. `move x` is the explicit form.

```drift
// Move-only by default
struct File {
    fd: Int
}

var f = open("log.txt");

var g = f; // ✅ moves ownership
use_file(f); // ❌ error: use after move
var h = move g; // ✅ explicit move ownership

fn use_file(x: File) -> Void { ... }

use_file(f); // ✅ consumes f (implicit move)
use_file(move h); // ✅ explicit move into the call
```

This design keeps ownership explicit: you opt *out* of move-only semantics only when cheap copies are well-defined.

### 4.3. Opting into copying

`Copy` is a marker trait defined in `std.core` (not auto-preluded). Use a qualified trait name (`std.core.Copy`) or import the module when writing trait requirements. A type is `Copy` when the compiler may duplicate it implicitly at duplication points; this must be **O(1)**. `Copy` does **not** imply "no drop": some `Copy` types (e.g., `String`) require semantic copying with retain/release under the hood. The internal `BitCopy` predicate still controls memcpy fast paths.

```drift
struct Job { id: Int }

implement std.core.Copy for Job {}

var a = Job(id = 1);
var b = a; // ✅ copies `a` because Job is Copy
```

### 4.4. Explicit copy expression

Use the `copy <expr>` expression to force a duplicate of a `Copy` value. It fails at compile time if the operand is not `Copy`. The operand must be an **lvalue/place** (local/param/field/index). This works anywhere an expression is allowed (call arguments, closure captures, `val`/`var` bindings) and leaves the original binding usable. By-value passing **does** implicitly move non-`Copy` values in consuming positions; `copy` is how you make the intent to duplicate explicit.

Copying still respects ownership rules: `self: &T` indicates the value is borrowed for the duration of the copy, after which both the original and the newly returned value remain valid.

### 4.4a. Explicit share expression (0.31.20)

Use the `share <expr>` expression to construct a second owner of a `Share`-implementing value (e.g. `Arc<T>`) without consuming the outer binding. Symmetric with `captures(share x)` in lambda capture lists; same Share-trait constraint, same warning-bearing aliasing contract, same lowering to `Share::share(&x)`.

```drift
val app: Arc<AppHandle> = arc(make_handle());
serve(share app, port);   // adds an owner; `app` remains usable
val r = app.get();         // `app` is still LIVE
```

Restrictions in v1:

- **Subject must be a NAME** (local binding). Bind first if you need a more complex expression: `val a = compute(); share a;`. Diagnostic: `E-SHARE-EXPR-SUBJECT-NOT-LOCAL`.
- **Subject type must implement `std.core.shareable.Share`.** For `Copy` types use `copy x`; for non-Share non-Copy types use `move x`. Diagnostic: `E-SHARE-EXPR-NOT-SHARE` (parallel to `E-CAPTURE-SHARE-NOT-SHARE`).
- **Argument evaluation order is unchanged.** `share x` lowers in place; no pre-hoisting before earlier args.
- **Borrow-survives-call invariant.** `share x` is a refcount bump on the owner, NOT a mutation of the binding. Outstanding borrows into `*x` (e.g. `val r = x.get();`) remain valid through the call AND its unwind path. A `try { f(share x); } catch ... { ...use(r)... }` does not need a re-`.get()` in the catch arm.

ABI-neutral; source-only.

### 4.5. Explicit deep copies (`dup`-style)

If a move-only type wants to offer a deliberate, potentially expensive duplicate, it can expose an explicit method (e.g., `dup`). Assignment still will not copy—callers must opt in:

```drift
struct Buffer { data: ByteBuffer }   // move-only

implement Buffer {
    fn dup(self: &Buffer) -> Buffer {
        return Buffer(data = self.data.copy());
    }
}

var b1 = Buffer(...);
var b2 = b1.dup(); // ✅ explicit deep copy
var b3 = b1; // ❌ still not allowed; Buffer is not `Copy`
```

This pattern distinguishes cheap, implicit copies (`Copy`) from explicit, potentially heavy duplication.

---

### 4.6. Example — copy vs move

```drift
struct Job { id: Int }

fn process(job: Job) -> Void {
    print("processing job ", job.id);
}

var j = Job(id = 1);

process(j); // ✅ copy (Job is copyable)
process(move j); // ✅ move; j now invalid
process(j); // ❌ error: j was moved
```

---

### 4.7. Example — non-copyable type

```drift
struct File { /* non-copyable handle */ }

fn upload(f: File) -> Void {
    print("sending file");
}

var f = File();
upload(move f); // ✅ move ownership
upload(f); // ❌ cannot copy non-copyable type
```

---

### 4.8. Example — borrowing instead of moving

```drift
fn inspect(f: &File) -> Void {
    print("just reading header");
}

var f = File();
inspect(f); // auto-borrows &f
upload(move f); // later move ownership away
```

---

### 4.9. Example — mut borrow vs move

```drift
fn fill(f: &mut File) -> Void { /* writes data */ }

var f = File();
fill(f); // auto-borrows &mut f
upload(move f); // move after borrow ends
```

Borrow lifetimes are scoped to braces; once the borrow ends, moving is allowed again.

---

### 4.10. Example — move return values

```drift
fn open(name: String) -> File {
    val f = File();
    return move f; // move to caller
}

fn example() -> Void {
    var f = open("log.txt");
}
```

Ownership flows *out* of the function; RAII ensures destruction if not returned.

---

### 4.11. Example — composition of moves

```drift
fn take(a: Array<Job>) -> Void { /* consumes array */ }

var jobs = Array<Job>();
jobs.push(Job(id = 1));
jobs.push(Job(id = 2));

take(move jobs); // move entire container
take(jobs); // ❌ jobs invalid after move
```

---

### 4.12. Lifetime and destruction rules
- Locals are destroyed **in reverse declaration order** when a block closes.  
- Moving (`move x`) transfers destruction responsibility to the receiver.  
- Borrowed references are automatically invalidated at scope exit.  
- No garbage collection — **destruction is deterministic** (RAII).

---

### 4.13. Moved-out state and partial-field moves

The "moved-out" state of a binding is **static ownership bookkeeping**, not a
runtime value. The compiler tracks, per binding and per place, whether
ownership has been transferred away; touching a moved-out place after the
move is a compile-time error. The runtime representation of the storage
behind a moved place is implementation-defined and depends on the type — see
§4.13.4.

#### 4.13.1. Move targets in v1

In v1, `move x` only accepts an addressable local or parameter binding.
Moving out of a projection — `move s.field`, `move arr[i]` — is **rejected at
compile time**:

```text
move of a projected place is not supported in v1;
move a local/param or use swap/replace
```

The canonical "move one field out" pattern is `std.mem.replace`, which
swaps a fresh value into the slot and returns the old one as an owned value:

```drift
val old = mem.replace(&mut s.field, fresh_value);
```

This works for every aggregate, has zero runtime overhead beyond the swap,
and keeps the field slot fully valid for any subsequent destructor.

#### 4.13.2. Partial field moves on plain aggregates (post-v1, optional)

For plain aggregates — structs that do **not** implement `Destructible`
(§5.11) and whose field types do not require custom drop coordination — the
language reserves the option to allow partial field moves in a future
revision. The model would be per-field liveness tracking inside the
aggregate's lifetime: each field independently transitions from *live* to
*moved-out*, and the aggregate's normal end-of-scope drop releases only the
fields that are still live.

This is **reserved, not promised**. v1 programs must use `mem.replace` for
the move-one-field-out pattern, and code reviews should not treat
"partial field moves" as a near-term plan.

#### 4.13.3. Aggregates with custom destructors reject partial moves

A struct that implements `Destructible` (or contains a field that does)
will **not** support partial field moves, in v1 or later. The reason is
contractual, not implementation-driven:

- `destroy(self)` runs as a single, indivisible action over the whole
  aggregate. It expects every field to be in a fully-formed state.
- A partial move would force `destroy` to inspect per-field liveness and
  branch on it. That defeats the point of the trait — destructors must
  remain simple and total.
- It would also entangle ownership state with destructor source code:
  adding a new "look at field X" line in `destroy` could retroactively
  invalidate a partial move that compiled before.

The corresponding diagnostic is shaped to point users at the supported
patterns (see §11.5 and `effective-drift.md`):

```text
cannot move field 'token' out of 'Session': Session has a custom destructor
hint: store the field as Optional<Token> and use take()
hint: or swap a replacement value in with std.mem.replace
```

#### 4.13.4. Runtime neutralization is type-specific

There is **no universal "tombstone" value** in Drift. After a move, the
*static* state of the source place is "moved-out" (and any further use is
rejected at compile time), but what the *runtime storage* contains is
type-specific:

- **`String`** — moved-out storage is left holding a null/empty header so
  that the source's destructor (if it still runs by some path) is a no-op.
- **`variant`** — moved-out variant slots are overwritten with an internal
  tombstone tag (`__drift_internal_tombstone`) that the variant's drop
  machinery recognizes and ignores. This is a compiler-internal arm and is
  not user-visible in `match`.
- **`Optional<T>`** — the user-visible "absence" form is `None`; the
  compiler does not introduce a separate tombstone for `Optional`.
- **`Array<T>`** — moved-out arrays are left in a zero-length state.
- **Plain aggregates with no resources** — moved-out storage may simply be
  left as garbage; the static ownership state guarantees it will not be
  read.

These neutralizations exist so that destructors which run on borderline
paths (e.g., partial unwinding) stay sound. They are **implementation
mechanics**, not part of the type's value space:

- `Void` is **not** a tombstone. `Void` is the unit return type with one
  inhabitant; it does not denote "moved-out" or "absence". Documentation,
  diagnostics, and design notes must not describe `Void` as a tombstone.
- User code must not test for tombstones, and must not rely on
  moved-out storage producing any particular observable value. The static
  use-after-move check is the contract; the runtime byte pattern is not.

---
## 5. Traits and compile-time capabilities

### 5.1. Traits vs. interfaces

- **Traits** are compile-time contracts with static/monomorphic dispatch. Implementations are specialized per concrete type (monomorphized) and incur no runtime vtable. Use traits for zero-cost abstractions like iterators, ops, or helpers that should inline/bake per type.
- **Interfaces** are runtime contracts with dynamic dispatch via a vtable (fat pointers `{data, vtable}`). Use interfaces when you need late binding across modules/plugins or heterogeneous collections. Owned interfaces include a drop slot; borrowed interfaces omit it.
- Choosing between them: prefer traits by default for performance and simplicity; reach for interfaces only when you truly need runtime polymorphism/late binding. The ABI and signing model keep interface layouts stable, while traits remain a compile-time-only construct.

(*Conforms to Drift Spec Rev. 2025-11 (Rev 4)*)  
(*Fully consistent with the `require` + `is` syntax finalized in design discussions.*)

---

### 5.2. Overview

Traits in Drift describe **capabilities** a type *is capable of*.  
They are compile-time contracts, not inheritance hierarchies and not runtime polymorphism.

Traits provide:
- **Adjective-like descriptions** of capabilities ("Dup", "Destructible", "Debug").
- **Static dispatch** — no vtables, zero runtime cost.
- **Package-scoped implementations** — an impl is legal only if the trait or the receiver type head is defined in the current package.
- **Type completeness checks** — types may *require* certain traits to exist.
- **Trait-guarded code paths** — functions may adapt their behavior based on whether a type implements a trait.

Traits unify:
- RAII/destruction
- formatting and debugging
- serialization
- copying, hashing, comparison
- algorithmic constraints
- type-safe generic specialization

---

### 5.3. Defining traits

A trait defines a set of functions that a type must provide to be considered capable of that trait.

```drift
trait Dup {
    fn dup(self: &Self) -> Self
}

trait Debug {
    fn fmt(self) -> String
}

trait Destructible {
    fn destroy(self) -> Void
}
```

Rules:

- Traits declare **behavior only** (no fields).
- `Self` refers to the implementing type.
- Inside a trait, an untyped receiver `self` is implicitly `self: Self`.
- Traits can depend on other traits (via `require Self is TraitX`).
- Trait names should be **adjectives** describing the capability.

---

### 5.4. Implementing traits

An implementation attaches the capability to a type.

```drift
struct Point { x: Int, y: Int }

implement Debug for Point {
    fn fmt(self) -> String {
        return "(" + self.x.to_string() + ", " + self.y.to_string() + ")";
    }
}
```

Rules:

- **Orphan rule:** an impl is legal only if the trait is defined in the current package
  or the receiver type head is defined in the current package. Otherwise:
  `E-IMPL-ORPHAN: implementation is not allowed outside the trait or type package.`

#### 5.4.1. Generic trait implementations

```drift
struct Box<T> { value: T }

implement Debug for Box<T>
    require T is Debug
{
    fn fmt(self) -> String {
        return self.value.fmt();
    }
}
```

- The `require` clause limits this implementation to types where `T is Debug`.
- If the requirement does not hold, the implementation is ignored for that specialization.

---

### 5.5. Type-level trait requirements (`require`)

A type may declare that it cannot exist unless certain traits are implemented.

```drift
struct File
    require Self is Destructible, Self is Debug
{
    fd: Int
}
```

Meaning:

- The program is **ill-formed** unless implementations exist:

  ```drift
  implement Destructible for File { ... }
  implement Debug  for File { ... }
  ```

Compiler errors if missing:

```
E-REQUIRE-SELF: Type File requires trait Destructible but no implementation was found.
E-REQUIRE-SELF: Type File requires trait Debug but no implementation was found.
```

When a type is well-formed, its `require` obligations are assumed as implied facts
when proving trait requirements for values of that type.

#### 5.5.1. Requiring traits of parameters

```drift
struct Box<T>
    require T is Dup,
            Self is Destructible
{
    value: T
}
```

Constraints:

- `Box<T>` can only be instantiated when `T is Dup`.
- `Box<T>` is considered incomplete unless a `Destructible` implementation for `Box<T>` exists.

---

### 5.6. Function-level trait requirements

Functions may restrict their usage to specific capabilities:

```drift
fn dup_twice<T>
    require T is Dup
(value: T) -> (T, T) {
    val a = value.dup();
    val b = value.dup();
    return (a, b);
}
```

More than one requirement may be listed:

```drift
fn print_both<T, U>
    require T is Debug,
            U is Debug
(t: T, u: U) -> Void {
    println(t.fmt());
    println(u.fmt());
}
```

Using a function with unmet trait requirements triggers a compile-time error.

**Subject restriction:** in this revision, trait requirements refer to type
parameters (e.g., `T is Trait`) or `Self`. Value identifiers (`x is Trait`)
are not part of the surface language.

---

### 5.6.1. Trait method lookup and scope

Trait dot-call lookup is explicit and module-scoped, with guard-scoped additions:

- Traits participate in dot-call lookup if listed via `use trait` or assumed by a
  trait guard in the current branch.
- `use trait <QualifiedTraitName>` is a module-level directive (module-scoped).
- Multiple `use trait` directives are allowed; all listed traits are in scope.
- Trait guards add a temporary, branch-local scope; there is no general block-scoped
  trait import.
- Dot-call lookup does **not** consider traits outside the module-scoped list
  and the current guard assumptions.
- Ambiguity between traits requires explicit disambiguation (UFCS).
- UFCS syntax: `TraitName::method(receiver, args...)`. Module-qualified trait names
  are allowed. UFCS bypasses `use trait` scope but still respects visibility.

Trait bounds and `use trait` are orthogonal:

- `use trait` controls *which* traits are considered for dot-call lookup.
- `require T is Trait` controls *which instantiations are allowed*.

Example:

```drift
import m_traits as t
use trait t.Show

fn f<T>(x: T) -> Int require T is t.Show {
    return x.show();
}
```

---

### 5.7. Trait guards (`if T is TraitName`)

Trait guards allow functions to adapt behavior based on whether a type implements a trait.

```drift
fn log_value<T>(value: T) -> Void {
    if T is Debug {
        println("[dbg] " + value.fmt());
    } else {
        println("<value>");
    }
}
```

Semantics:

- `if T is Debug` is a **compile-time condition**.
- Only the active branch must type-check for the given `T`.
- Inside the guarded block, methods from the trait become valid (`value.fmt()` here).
- The guard condition is assumed as a proof fact in that branch for trait requirements.
- Guard subjects must be type parameters (or `Self`); value identifiers are not
  allowed in this revision.

### 5.8. Multiple trait conditions

```drift
fn log_value<T>(value: T) -> Void {
    if T is Debug and T is Serializable {
        ...;
    } else if T is Debug {
        ...;
    } else if T is Serializable {
        ...;
    } else {
        ...;
    }
}
```

Trait guards prevent combinatorial explosion of overloaded functions.

---

### 5.9. Trait expressions (boolean logic)

Trait requirements and guards allow boolean trait expressions:

- `T is A and T is B` — must implement both traits
- `T is A or T is B` — must implement at least one
- `not (T is std.core.Copy)` — must *not* implement the trait
- Parentheses allowed for grouping

Example:

```drift
fn dup_if_possible<T>(value: T) -> T {
    if T is std.core.Copy {
        return value; // implicit copy
    } else if T is Dup {
        return value.dup();
    } else {
        panic("Type cannot be duplicated");
    }
}
```

Traits become composable *properties* of types.

**Trait-level `require` restriction:** when defining a trait, the `require`
clause is limited to **conjunctions** of `Self is Trait` (supertraits). `or` and
`not` are not allowed in trait requirements until their semantics are specified.

Examples:

```drift
trait Printable
    require Self is Debug, Self is Display
{
    ...
}

trait SyncSend
    require (Self is Send and Self is Sync)
{
    ...
}
```

Not allowed (trait-level):

```drift
trait Printable
    require (Self is Debug or Self is Display)
{
    ...
}

trait NonCopy
    require not (Self is std.core.Copy)
{
    ...
}
```

---

### 5.10. Trait dependencies (traits requiring traits)

Traits themselves may declare capabilities they depend upon:

```drift
trait Printable
    require Self is Debug, Self is Display
{
    fn print(self) -> String {
        return self.fmt();
    }
}
```

Any type that implements `Printable` must also implement `Debug` and `Display`.

---

### 5.10.1. The `Share` trait (`std.core.shareable.Share`)

Drift distinguishes **value-like duplication** (`Copy`) from
**shared-owner duplication** (`Share`). They are deliberately separate
capabilities:

```drift
// in stdlib/std/core/shareable.drift:
pub trait Share {
    fn share(self: &Self) nothrow -> Self;
}
```

- **`Copy`** types may be passed by value without consuming.
  Duplication produces a **value-like duplicate**: representation
  aliasing (e.g., `String`'s shared ARC backing) is not observable
  as **mutation aliasing** — at the language semantic layer the
  duplicate behaves as an independent value. Default impls cover
  scalars, references, `Optional<T>` over Copy, and `String`
  (whose refcounted backing is a hidden optimization).
- **`Share`** types produce **another OWNER** of the same underlying
  resource. Aliasing is part of the meaning: subsequent mutations
  through any owner are observable through the other. `share` is
  intentionally a **warning-bearing** operation: choosing it is
  choosing to accept aliasing.

A type implements one, the other, both (rare and meaningful), or
neither. Drift never auto-derives `Share`; it is always an explicit
trait impl.

#### Synchronization is the programmer's responsibility

`Share` says nothing about thread-safety of mutations performed
through aliased owners. If the underlying resource is mutated across
threads, tasks, or callbacks, synchronization (e.g., `Mutex`,
atomics) is the caller's contract. The `Share` impl's only
guarantee is that the additional owner is well-formed (refcount
bumped atomically for refcounted types like `Arc<T>`; the per-type
contract is decided by the implementor).

#### `Share` is not `Clone`

Drift does not have a `Clone` trait. The single-trait "Clone" idiom
in some other ecosystems conflates value-like duplication with
shared-owner duplication, leading to recurring user surprise around
whether `clone()` produces an independent copy or another handle to
shared state. Drift keeps the two capabilities orthogonal to
preserve the warning value of `share` at code-review time.

#### `Share` method must be `nothrow`

Aliasing-duplication does not introduce error paths. Implementors
that need fallible setup should expose a separate factory function,
not abuse `share`.

#### Canonical adopters

- **`Arc<T>` (`std.concurrent`)** is `Share`. The implementation
  delegates to `Arc.clone()` (the existing `_arc_clone_impl`
  intrinsic), atomically incrementing the strong count and returning
  a new `Arc<T>` pointing to the same `ArcBox<T>`. `Arc.clone()`
  remains available as a method; `share` is the language-level
  spelling preferred for capture syntax (see §22.2.4).
- **`String` is intentionally NOT `Share`.** Even though its
  representation uses shared backing under the hood, the user-facing
  semantic is value-like duplication. Forcing `String` into `Share`
  would defeat the warning value of the `share` keyword. Use
  `copy s` for `String`.
- **`Array<T>` is NOT `Share`,** and a shared-backing variant of
  `Array<T>` is **not planned**. Drift's `Array<T>` is owned-vector-
  shaped (move on assignment, deep-copy via explicit clone); making
  array assignment alias the backing store would change a load-
  bearing semantic and is out of scope for the language. To share
  collection state across owners, wrap the collection in an `Arc`
  (e.g., `Arc<Mutex<Array<T>>>` for cross-thread mutation) and
  share the `Arc`.

### 5.11. RAII and the `Destructible` trait

Destruction is expressed as a trait:

```drift
trait Destructible {
    fn destroy(self) -> Void
}
```

Types with owned resources demand this trait:

```drift
struct OwnedMySql
    require Self is Destructible
{
    handle: MySqlPtr
}

implement Destructible for OwnedMySql {
    fn destroy(self) -> Void {
        if !self.handle.is_null() {
            mysql_close(self.handle);
        }
    }
}
```

RAII semantics:

- Automatic cleanup at scope exit calls `destroy(self)` exactly once.
- Manual early destruction is allowed via `value.destroy()`, which consumes `self`.
- `destroy(self)` is **non-throwing**: it must not unwind or return `FnResult`. Any cleanup failure must be handled internally (log, best-effort) so resource release cannot itself trigger further unwinding.

This integrates seamlessly with move semantics and deterministic lifetimes.

---

### 5.12. Overloading and specialization by trait

Functions may overload based on trait requirements:

```drift
fn save<T>
    require T is Serializable
(value: T) -> ByteBuffer {
    return value.serialize();
}

fn save<T>(value: T) -> ByteBuffer {
    return reflect::dump(value);
}
```

Rules:

- The compiler picks the most specific applicable overload.
- Ambiguity is a compile‑time error.
- If no overload applies, the compiler reports a missing capability.

---

### 5.13. Complete syntax summary

#### 5.13.1. Defining a trait

```drift
trait Debug {
    fn fmt(self) -> String
}
```

**Legacy note:** `Debug` was historically used for diagnostics. For exceptions and captured locals, use the `Diagnostic` trait defined in §5.13.7.

#### 5.13.2. Implementing a trait

```drift
implement Debug for File {
    fn fmt(self) -> String { ... }
}
```

#### 5.13.3. Requiring traits in a type (type completeness)

```drift
struct Cache<K, V>
    require K is Hashable,
            Self is Destructible
{
    ...;
}
```

#### 5.13.4. Requiring traits in a function

```drift
fn print<T>
    require T is Debug
(v: T) -> Void { ... }
```

#### 5.13.5. Trait-guarded logic

```drift
if T is Debug { ... }
if not (T is Serializable) { ... }
```

#### 5.13.6. Boolean trait expressions

```drift
require (T is Debug or T is Display)
require (T is Dup and not (T is Destructible))
```

#### 5.13.7. Diagnostic trait

Exceptions and `^`-captured locals rely on a dedicated diagnostic trait. The diagnostic representation is **JSON** — a `JsonNode` value owned by the projecting type:

```drift
trait Diagnostic {
    fn to_json(self: &Self) nothrow -> JsonNode
}
```

`JsonNode` is the recursive value variant exposed by `std.json.value` (low-level JSON layer; see Chapter 14 / §14.2 for the exception envelope and Chapter 18 for the JSON module split).

Rules:

- Primitive types implement `to_json` as scalars (`Bool`, `Int`, `Uint`, `Float`, `String` map to the corresponding `JsonNode` arms; `Number` carries the textual representation).
- `Optional<T>` implements `to_json` as `JsonNode::Null` (`None()`) or `T.to_json()`.
- Structs without a custom implementation default to a `JsonNode::Object` mapping each field name to `field_value.to_json()`.
- `to_json` must never throw.
- The implementation lives next to `JsonNode` in the low-level `std.json.value` layer; user-side `import std.json.value as jv;` (or `import std.json as json;` which re-exports) brings it into scope.

#### 5.13.8. JsonNode: structured diagnostics

The diagnostic value model is `JsonNode` (see Chapter 18 for the full JSON surface):

```drift
variant JsonNode {
    Null
    Bool(value: Bool)
    Number(raw: String)        // textual numeric — parse via as_int / as_float / as_uint
    String(value: String)
    Array(values: Array<JsonNode>)
    Object(fields: HashMap<String, JsonNode>)
}
```

Public access surface (non-throwing, all return `Optional<T>` for the leaf type):

```drift
fn as_int(self: &JsonNode) -> Optional<Int>
fn as_uint(self: &JsonNode) -> Optional<Uint>
fn as_float(self: &JsonNode) -> Optional<Float>
fn as_bool(self: &JsonNode) -> Optional<Bool>
fn as_string(self: &JsonNode) -> Optional<String>
fn as_array(self: &JsonNode) -> Optional<&Array<JsonNode>>
fn as_object(self: &JsonNode) -> Optional<&HashMap<String, JsonNode>>
fn is_null(self: &JsonNode) -> Bool
```

A `JsonHandle` (`std.json.value`) wraps a refcounted owner of a `JsonNode` tree, implements `Frozen` + `ConstShare`, and provides two distinct surfaces — a cheap dump path and a fluent cursor lookup path:

```drift
// --- Dump path (primary log/save surface) ---
fn encode_compact(self: &JsonHandle) nothrow -> String        // canonical JSON text

// --- Fluent lookup (cursor-based — distinguishes missing from explicit JSON null) ---
fn get(self: &JsonHandle, key: String) nothrow -> JsonCursor  // cursor over the named field

// --- Structured traversal ---
fn root(self: &JsonHandle) nothrow -> &JsonNode               // borrow the underlying tree
```

Rules:

- **Dump-first surface.** `encode_compact` is the primary public path; it emits canonical JSON text directly from the stored tree (or, for diagnostic carriers, directly from the stored canonical JSON string — see §14) and avoids parse/materialize/re-encode round-trips.
- **Missing ≠ JSON null.** `JsonHandle::get` (and `JsonCursor::get`, `JsonCursor::index`) return a `JsonCursor` that *separately* represents "absent" and "present-and-explicit-null". Callers that need to distinguish them inspect the cursor; callers that don't simply chain `.as_int() / .as_string() / …`, both of which return `Optional<T>` and yield `Optional::None()` for either absent or wrong-typed values.
- `JsonCursor` is the canonical fluent-lookup surface. The standard public chain is `e.params.get("k").as_int()` → `Optional<Int>` with no `&` ceremony at the call site.
- `JsonHandle` is `ConstShare` — `var b = e.params; var c = b;` produces independent owners via implicit duplication (Phase 5 ConstShare substrate).

> **Legacy note (Slice 7c-3, ABI 14, 2026-05-06):** The previous `DiagnosticValue` variant and `to_diag()` method are fully retired. Slice 7a removed the public surface, Slice 7c-1 deleted the runtime exports, Slice 7c-2 deleted the compiler-internal HIR / MIR / codegen substrate, and Slice 7c-3 deleted the residual `TypeKind.DIAGNOSTICVALUE` type identity (enum value, `ensure_diagnostic_value()` helper, `dv_ty` plumbing, package serialization arms, LLVM type emission, C struct types). New public APIs, docs, and tests must use `JsonNode` and `to_json` (or, for diagnostic projection, `core.Diagnostic.to_json_text`).

### 5.14. Thread-safety marker traits (`Send`, `Sync`)

Certain libraries (notably `std.concurrent`) rely on two marker traits that express thread-safety:

- **`Send`** — values of a type implementing `Send` may be moved from one thread to another.
- **`Sync`** — shared references (`&T`) to a type implementing `Sync` may be shared across threads simultaneously.

All primitives and standard library containers implement these traits when safe.

```drift
trait Send { }
trait Sync { }
```

Implementing `Send` means a value may be moved to another thread. Implementing `Sync` means shared references may be used concurrently. A struct may opt into `Send` if all of its fields are `Send`; similarly for `Sync`. Types that manage thread-affine resources (e.g., OS handles that must stay on one thread) simply omit these traits and remain single-threaded.

The concurrency chapter (Chapter 19) references these bounds when describing virtual-thread movement and sharing.

> **v1 status:** `Send` and `Sync` are spec-defined but not compiler-enforced in v1. No trait-bound checking occurs at spawn or thread-sharing boundaries.

---

### 5.14.1. Unborrowed marker trait

Some APIs must store values indefinitely (globals, registries, caches). For these
cases Drift uses a marker trait that guarantees the value does not depend on any
borrowed views:

- **`Unborrowed`** — values contain no borrowed references or borrowed views
  and therefore do not rely on shorter lifetimes.

```drift
trait Unborrowed { }
```

Meaning: "borrowed" includes any type whose validity depends on an owner’s
lifetime (for example `&T`, `&mut T`, and borrowed view types such as slices).

Guarantee: if `T is Unborrowed`, then values of `T` may be stored indefinitely
without capturing a borrow to shorter-lived data.

Non-guarantees: `Unborrowed` is orthogonal to thread safety; it does not imply
`Send` or `Sync`.
`Unborrowed` is about lifetime independence; owning indirections/containers are
allowed so long as they do not embed borrows.

Structural rules (compiler-known, auto-impl):
- `&T` and `&mut T` are never `Unborrowed`.
- Borrowed view types (for example `ByteSlice`, `MutByteSlice`) are never `Unborrowed`.
- Structs/variants/tuples/arrays are `Unborrowed` iff all field/element types
  are `Unborrowed`.
- Generic containers are `Unborrowed` iff the container does not embed borrows
  and all type arguments are `Unborrowed`.
- Function pointers are `Unborrowed` when non-capturing.
- Closures are `Unborrowed` iff they capture no borrows and all by-value
  captures are `Unborrowed`.

> **v1 status:** `Unborrowed` is spec-defined but not compiler-enforced in v1. No structural auto-impl checking is performed.

---


### 5.15. Design Rationale

Traits are designed to:

- Express **capabilities**, not inheritance.
- Enable rich, generic programming without runtime cost.
- Allow types to declare their **necessary capabilities** via `require`.
- Allow algorithms to adapt to available capabilities via **trait guards**.
- Provide a unified abstraction for:
  - RAII (`Destructible`)
  - formatting (`Debug`, `Display`)
  - serialization, hashing, comparison
  - "marker" traits for POD or special behaviors

The trio of:

1. **Traits**  
2. **`require` clauses**  
3. **Trait guards (`if T is Trait`)**

forms a coherent, expressive, zero‑overhead system.

---
## 6. Interfaces & dynamic dispatch

Drift supports **runtime polymorphism** through *interfaces*.  
Interfaces allow multiple **different concrete types** to be treated as one unified abstract type at runtime.  
This is the dynamic counterpart to compile‑time polymorphism provided by *traits*.

**No class/struct inheritance:** Drift has no concrete type inheritance. Data and behavior compose via structs + traits (static) and interfaces (dynamic). This avoids fragile base classes, hidden layout coupling, and diamond/virtual-base complexity while keeping ABI/layout predictable; interfaces supply dynamic dispatch without inheriting state.

Closures and callable traits are specified separately (see Chapter 22). Interfaces focus purely on dynamic dispatch for traditional object shapes.

---

### 6.1. Interface definitions

Interfaces define a set of functions callable on any implementing type.

```drift
interface OutputStream {
    fn write(self: &OutputStream, bytes: ByteSlice) -> Void
    fn writeln(self: &OutputStream, text: String) -> Void
    fn flush(self: &OutputStream) -> Void
}
```

### 6.2. Rules

- Interfaces may not define fields — pure behavior only.
- Interfaces are **first‑class types** (unlike traits).
- A function that receives an `OutputStream` may be passed any object that implements that interface.
- The method signatures inside an interface show the receiver type explicitly (`self: &OutputStream`).

### 6.3. Receiver rules (`self`)

Drift differentiates between **methods** (eligible for dot-call syntax) and **free functions**.

- Methods are declared inside an `implement Type { ... }` block; they do **not** implicitly create free-function aliases.
- The **first parameter** in a method declaration is the receiver; its mode is determined by its type:
  - `T`: pass by value
  - `&T`: shared borrow
  - `&mut T`: exclusive/mutable borrow
  `self` is the idiomatic name for the receiver, but the role comes from position/mode, not spelling.
- The receiver’s nominal type is implied by the `implement` header; there is no magic receiver outside an `implement` block.
- `implement` headers must use the nominal type (`Point`, `Vec<Int>`, etc.), not a reference-qualified type (`&Point`, `&mut Point`); reference headers are rejected.
- Outside an `implement` block every function is a free function. A free function may take any parameters (including an explicit `&File`), but it is invoked with ordinary call syntax (`translate(point, 1, 2)`), not `point.translate(...)`.

Example:

```drift
struct Point { x: Int, y: Int }

implement Point {
    fn move_by(self: &mut Point, dx: Int, dy: Int) -> Void {
        self.x += dx;
        self.y += dy;
    }
}

fn translate(p: &mut Point, dx: Int, dy: Int) -> Void {
    p.x += dx;
    p.y += dy;
}

point.move_by(1, 2); // method call (auto-borrows &mut point)
translate(point, 3, 4); // free function call (auto-borrows &mut point)

Within a module, a given name may be used for both a free function and a method. Unqualified calls always resolve to free functions (locals win over free functions); methods are only reachable via an explicit receiver or explicit UFCS. Method names may be reused across different types as long as each (type, method name) pair is unique.
```

This rule set makes the receiver’s ownership mode explicit and prevents implicit, C++-style magic receivers.

---

### 6.4. Implementing interfaces

A concrete type implements an interface through an `implement` block:

```drift
struct File {
    fd: Int
}

implement OutputStream for File {
    fn write(self: &File, bytes: ByteSlice) -> Void {
        sys_write(self.fd, bytes);
    }

    fn writeln(self: &File, text: String) -> Void {
        self.write((text + "\n").to_bytes());
    }

    fn flush(self: &File) -> Void {
        sys_flush(self.fd);
    }
}
```

Rules:

1. All interface functions must be provided.
2. Method signatures begin with an explicit receiver (`self: &File` here); the type (`File`) is implied by the `implement` header.
3. A type may implement multiple interfaces.
4. Implementations may appear in any module.

---

### 6.5. Using interface values

Interfaces may be used anywhere that types may appear.

#### 6.5.1. Parameters

```drift
fn write_header(out: OutputStream) -> Void {
    println("=== header ===");
}
```

#### 6.5.2. Return values

```drift
fn open_log(path: String) -> OutputStream {
    var f = File.open(path);
    return f; // implicit upcast: File → OutputStream
}
```

#### 6.5.3. Locals

```drift
var out: OutputStream = open_log("app.log");
println("ready");
```

#### 6.5.4. Heterogeneous arrays

```drift
var sinks: Array<OutputStream> = [];
sinks.push(open_log("app.log"));
sinks.push(open_log("audit.log"));
```

Each element may be a different type implementing the same interface.

---

### 6.6. Dynamic dispatch semantics

A value of interface type is represented as a **fat pointer**, containing:

1. A pointer to the concrete object.
2. A pointer to the interface’s vtable for that concrete type.

When calling:

```drift
out.write(buf);
```

the compiler emits:

- load vtable for OutputStream
- resolve the `write` slot
- indirect call to the concrete implementation

This ensures fully dynamic runtime dispatch with minimal overhead.

---

### 6.7. Interfaces vs traits

Characteristic | **Trait** | **Interface**
---------------|-----------|-------------
Purpose | static capability | dynamic behavior
Type? | **No** | **Yes**
Dispatch | static (zero cost) | dynamic (vtable)
Heterogeneous containers | impossible | supported
Retroactive extension | always | always
Requires `Self`? | yes | no
Use in generics | required (`T is Trait`) | invalid (`T is Interface`)

Traits = static logic  
Interfaces = runtime logic  
The two systems are orthogonal by design.

---

### 6.8. Shape example

#### 6.8.1. Define the interface

```drift
interface Shape {
    fn area(self: &Shape) -> Float
}
```

#### 6.8.2. Implementations

```drift
struct Circle { radius: Float }
struct Rect   { w: Float, h: Float }

implement Shape for Circle {
    fn area(self: &Circle) -> Float {
        return 3.14159265 * self.radius * self.radius;
    }
}

implement Shape for Rect {
    fn area(self: &Rect) -> Float {
        return self.w * self.h;
    }
}
```

#### 6.8.3. Usage

```drift
fn total_area(shapes: Array<Shape>) -> Float {
    var acc: Float = 0.0;
    var i = 0;
    while i < shapes.len() {
        acc = acc + shapes[i].area();
        i = i + 1;
    }
    return acc;
}
```

Heterogeneous containers work naturally:

```drift
var all: Array<Shape> = [];
all.push(Circle(radius = 4.0));
all.push(Rect(w = 3.0, h = 5.0));
```

---

### 6.9. Ownership & RAII

Interface values follow Drift ownership and move semantics.

### 6.10. Moving

```drift
fn consume(out: OutputStream) -> Void {
    println("consumed");
}
```

Passing `out` moves the *interface wrapper* and transfers ownership of the underlying concrete value.

### 6.11. Destruction

At scope exit:

- If underlying type implements `Destructible`, its `destroy(self)` runs. Owned interface types should **require** `Destructible` so their vtables always carry a drop slot; borrowed interface views omit this and perform no destruction.
- Otherwise, nothing is done.

```drift
{
    var log = open_log("a.log"); // OutputStream
    log.writeln("start");
}; // log.destroy() runs if File is Destructible
```

No double‑destroy is possible because `destroy(self)` consumes the value.

---

### 6.12. Multiple interfaces

A type may implement several interfaces:

```drift
interface Readable  { fn read(self: &Readable) -> ByteBuffer }
interface Writable  { fn write(self: &Writable, b: ByteSlice) -> Void }
interface Duplex    { fn close(self: &Duplex) -> Void }

struct Stream { ... }

implement Readable for Stream { ... }
implement Writable for Stream { ... }
implement Duplex   for Stream { ... }
```

Each interface gets its own vtable.  
There is no conflict unless the implementing type violates signature constraints.
Layout stability: if interface inheritance is used, parent entries (including the drop slot for owned interfaces) stay at fixed offsets. Separate interfaces never share a vtable; each interface value carries the vtable for that interface only.

---

### 6.13. Interfaces + traits together

These systems complement each other:

```drift
trait Debug { fn fmt(self) -> String }

interface DebugSink {
    fn write_debug(self: &DebugSink, msg: String) -> Void
}

fn log_value<T>
    require T is Debug
(val: T, sink: DebugSink) -> Void {
    sink.write_debug(val.fmt());
}
```

- `T is Debug`: compile‑time capability  
- `sink: DebugSink`: runtime dynamic behavior  

This pattern is central to building logging, serialization, and plugin systems.

---

### 6.14. Error handling across interfaces

Interface method calls participate in normal exception propagation:

```drift
fn dump(src: InputStream, dst: OutputStream) -> Void {
    var buf = ByteBuffer.with_capacity(4096);
    loop {
        buf.clear();
        val n = src.read(buf.as_mut_slice());
        if n == 0 { break }
        dst.write(buf.slice(0, n));
    }
}
```

Thrown errors travel unchanged across interface boundaries, preserving `^`-captured context.

---

### 6.15. Summary

Interfaces provide:

- true dynamic dispatch
- heterogeneous collections
- seamless integration with RAII and ownership
- retroactive modeling
- uniform, predictable runtime behavior

Traits provide:

- static capabilities
- compile‑time specialization
- no runtime overhead
- fine-grained constraints and guards

Drift separates these two forms of polymorphism to preserve clarity, predictability, and performance.

Together they form a flexible dual system:

- **Traits for compile-time adaptability**
- **Interfaces for runtime flexibility**

---

### 6.16. Arc interface views (shared interface ownership)

An `Arc<T>` (reference-counted shared ownership; see `std.concurrent`) over a
concrete `T` that implements one or more interfaces may be **viewed** as an
`Arc<Interface>` for any interface `T` implements. This is the language-level
shape of shared, multiply-viewed services: one concrete object implements
several interfaces, one `Arc` owns it, and different subsystems each receive
an `Arc<Interface>` face over the same allocation.

The conversion is an explicit coercion expressed as a method on
`Arc<T>`. The user names only the target interface face (`I`); the
source concrete type (`T`) is inferred from the `Arc<T>` receiver:

```drift
val svc     = conc.arc(AppService(...));
val logs    = svc.as_interface<type log.ContextResolver>();
val metrics = svc.as_interface<type metrics.Emitter>();
```

All three handles (`svc`, `logs`, `metrics`) denote the **same allocation**
and share the **same control block / strong count**. `Arc<Interface>` is an
interface view over that allocation — **not** a boxed copy of an interface
value and not a second owned control block.

#### 6.16.1. Normative contract

1. **Multi-interface implementability.** A single concrete type `T` may
   implement any number of interfaces `I1, I2, …, Ik` through independent
   `implement Ij for T` blocks. Interface implementations do not conflict
   unless method signatures do.

2. **Conversion is permitted iff `T` implements `I`.** The expression
   `a.as_interface<type I>()` (where `a: Arc<T>`) is well-formed if and
   only if `T` implements `I`. The compiler enforces this through the
   `require T is I` clause on the method; a call where `T` does not
   implement `I` is rejected at compile time with a `require`-unsatisfied
   diagnostic. There is no runtime fallback.

3. **No payload copy or move.** The concrete payload is not copied, moved,
   or reconstructed by the conversion. The `Arc<Interface>` view reads and
   dispatches against the same bytes as the originating `Arc<T>`.

4. **No second control block.** The conversion does not allocate. It
   operates purely on the existing control block: it observes the current
   strong count, atomically increments it, and constructs a handle that
   points at the same allocation with a different interface view.

5. **Shared strong count.** All `Arc` handles over the same underlying
   allocation — whether `Arc<T>` handles, `Arc<Ij>` handles for any `Ij`
   that `T` implements, or clones of any of those — reference a single
   strong count in the shared control block. Calling `.clone()` on any
   handle atomically increments that count; dropping any handle atomically
   decrements it.

6. **Destructor runs exactly once.** When the final handle over the shared
   allocation drops (strong count reaches zero), the concrete `T`'s
   destructor runs exactly once. This is true regardless of which view
   kind is the last to drop — the final view may be any `Arc<Ij>` or an
   `Arc<T>`; destruction always runs `T`'s `Destructible::destroy`, not
   any `Ij`'s drop slot. Implementations must not attempt to recover `T`
   from the interface view at drop time; a conformant implementation
   captures the `T`-typed destroy mechanism at `arc<T>(value)`
   construction time.

7. **Dispatch through `T`-as-`I` vtable.** Method calls on an
   `Arc<I>` view dispatch through the `T`-as-`I` vtable — i.e., through
   the `implement I for T` block's method bodies, with the concrete `T`
   instance as the receiver. Each `Arc<Ij>` view carries the vtable for
   its own `Ij`; views over the same allocation for different interfaces
   use different vtables.

8. **Object identity preserved across views.** Any two `Arc<Ij>` views
   derived from the same underlying allocation address the same concrete
   object. State mutated through one view is observable through another
   view.

9. **Mutation requires interior mutability.** `Arc` (including its
   interface views) provides shared ownership, not unique mutable access.
   Mutation of state reachable through `Arc<I>` method calls requires
   standard interior-mutability patterns inside `T` — typically
   `conc.Mutex<...>` fields, atomic fields, or lock-free message queues.
   An `Arc<I>` view does not grant a `&mut I` or `&mut T`; if multiple
   subsystems hold different interface views over the same allocation and
   all may mutate, `T` must coordinate that mutation internally.

10. **Arc<Interface> is a view, not a boxed interface value.** An
    `Arc<Interface>` is a shared view over the allocation that holds a
    concrete `T`. It is not an interface value (`{data_ptr, vtable}` fat
    pointer on the stack) that happens to be inside an `Arc`. The
    distinction matters: two `Arc<I>` views that trace back to the same
    `arc<T>(value)` call share the control block; two independent
    `arc<T1>(v1)` and `arc<T2>(v2)` calls — even if `T1 = T2` — do not.

#### 6.16.2. Explicit construction

Arc interface views are constructed only through an explicit conversion.
The conversion is not implicit: assigning or passing an `Arc<T>` where
an `Arc<I>` is expected does not silently coerce, even when `T`
implements `I`. `a.as_interface<type I>()` (where `a: Arc<T>`) is the
canonical form — the method's `<I>` type parameter names the target
face; `T` is inferred from the receiver's `Arc<T>`. Implementations
may offer ergonomic aliases; the explicit method form remains the
language-level primitive.

#### 6.16.3. Interaction with borrowed interface dispatch

`Arc<Interface>.get()` returns an `&Interface` reference — a borrowed
interface view whose lifetime is tied to the `Arc<Interface>` handle.
Method calls on this `&Interface` reference use the same vtable the
containing `Arc<Interface>` carries; dispatch cost is one indirect call
through the vtable's method slot, with no refcount traffic on the per-call
path. Cloning and dropping the `Arc<Interface>` handle itself remains the
only operations that touch the refcount.

---

## 7. Imports

Drift uses explicit imports — no global or magic identifiers beyond the implicit
`lang.core` prelude (Section 3.3). This chapter focuses on import mechanics.

**Provider-agnostic imports.** From the programmer’s perspective, `import`
always imports a module interface by **module id**. A build may supply modules
either as Drift source files or as signed module packages (DMIR/DMP, Chapter
20). This does not change the surface syntax or the name-resolution rules; it
only changes where the toolchain obtains the imported module’s interface and
typed semantics.

**Build targets vs compilation units.** Drift code is organized into **modules**
(the language-level unit named by a module id and referenced by `import`).
End users generally think in terms of producing an executable or a package
artifact, not "building a module" directly.

- **Executable build**: resolve imports from source roots and package roots,
  compile all required modules, then link into a single executable with an entry
  point.
- **Package build**: compile one or more modules from source roots and bundle
  them into a distributable package artifact (DMIR/DMP) together with module
  interfaces and metadata.

A **package** is a distribution artifact, not a namespace or import unit;
imports always resolve against **modules**, whether they originate from source
roots or package contents.

**Packages are distribution containers.** A single package artifact (e.g., a
signed DMIR/DMP) may contain **many modules**. Each contained module retains its
own canonical **module id** (e.g., `std.io`, `std.net`, `std.fmt`) and exports an
interface independently. Imports always target modules by module id; packages
only determine **where** module contents and interfaces are sourced from (source
roots vs package roots).

**Module id uniqueness.** A build must reject any configuration in which the
same module id is provided by multiple packages (or by both source and package).
Module ids are globally unique within a build; this prevents ambiguous imports
and ensures deterministic linking.

Example: the Drift standard library is distributed as one package containing
multiple modules under the `std.*` module-id prefix.

The `std.runtime` module is reserved for OS/process interface surfaces such as
argv/env access, registry-style runtime storage, and standard I/O handles.

**Tooling note — module roots vs package roots.** The compiler distinguishes:
- **Source module roots**: directories searched for `.drift` source modules (CLI: `-M/--module-path`, repeatable).
- **Package roots**: locations searched for signed module packages (DMP/DMIR). A package root may be either a directory containing many packages (a local repository/cache) or an individual package archive file (CLI: `--package-root`, repeatable).

This separation avoids ambiguity and makes builds reproducible: source roots are never treated as package roots and vice versa.

### 7.0. Exports (module interface)

Modules have an explicit **export set** and an explicit **visibility marker**
(`pub`). All other top-level items are private to the defining module.

Syntax:

```drift
export { foo, Point, Boom }
export { other.module.* }
```

Rules:

- Items are **private by default**. A name is eligible for export only if it is
  declared `pub`.
- The export list names top-level items declared in the current module:
  - functions
  - constants (`const`)
  - types such as `struct`, `variant`, `exception`, `trait`, `interface`, `type`
- `export` may appear multiple times within the module; the effective export set
  is their union.
- Exporting a name that is not declared in the module is a compile-time error.
- Exporting a **non-`pub`** local symbol is a compile-time error.
- `export { other.module.* }` re-exports exactly that module’s export set.
- Exporting a non-`pub` symbol **must not** elevate visibility; `export` only
  exposes items already marked `pub`.
- Name collisions introduced by re-exports are a compile-time error (the toolchain
  must report both sources).

Exported functions are module interface entry points and therefore use the
cross-module can-throw ABI calling convention (Section 7.2).

### 7.0.1. Member visibility (struct fields and methods)

Member visibility is **module-scoped** and defaults to private.

Rules:

- Struct fields and `implement` methods are **private by default**.
- A field or method marked `pub` is accessible outside the defining module,
  subject to the visibility of its enclosing type.
- `export { TypeName }` does **not** elevate member visibility; it only exports
  the type name.
- Field access and method calls are checked at the access site:
  - A private field/method may be used only within the defining module.
  - Access outside the defining module is a compile-time error.

### 7.0.2. Test-build-only declarations

Declarations annotated with `@test_build_only` are included only in test builds.
In normal builds they are ignored (not visible, not exported). The compiler
enables them only when invoked with `--test-build-only`.

### 7.1. Import syntax (modules only)

Drift has a single import form:

**Module import** — binds a module name (or alias) into the local scope:

```drift
import lang.array          // bind the module
import std.concurrent as conc  // bind with alias
```

**Name‑resolution semantics**

- `import <ModuleId>` always resolves to a **module** and binds that module under
  its last segment (or the `as` alias).
- Aliases affect only the local binding; frames and module metadata always record the original module ID, not the alias.
- For the implicit `lang.core` prelude, no import is needed; everything else
  must be imported explicitly.
- Only **exported** symbols may be referenced through an imported module. Attempting
  to access a non-exported symbol is a compile-time error.

**Module identifiers**

- Declared with `module <id>` once per file. Exactly one source file defines one
  module. A build must fail if multiple files declare the same module id.
  **Single‑file script mode** may default a missing declaration to `main`;
  workspace/target builds require an explicit `module <id>`.
- `<id>` must be lowercase segments separated by dots. Each segment must start with a lowercase letter and may contain lowercase letters, digits, and underscores; underscores may not be leading/trailing or consecutive. Dots may not be leading/trailing or consecutive. Total length ≤ 254 UTF-8 bytes.
- Reserved namespaces: `lang.*`, `std.*`, and `drift.*` (and any future toolchain‑reserved
  prefixes) are allowed syntactically but may only be provided by **trusted
  toolchain keys** under the package trust policy. This restriction is enforced
  at package load time (not in the parser) and **cannot** be bypassed with
  `--allow-unsigned-from`. User trust-store entries do not grant access to
  reserved namespaces; only toolchain‑shipped keys are accepted via the
  toolchain’s core trust store. Toolchains may provide non‑normative dev/test
  switches that replace the core trust store; those switches are not part of the
  language/build specification.
- Frames/backtraces record the declared module ID (not filenames), so cross-module stacks are unambiguous.

### 7.1.1. Merge modules and prelude modules (convention)

Drift modules are single-file and explicit. Larger surfaces are composed by
**merge modules** and optional **prelude modules**:

- A **merge module** is a normal module whose primary job is re-exports.
- A **prelude module** is a normal module (often `<pkg>.prelude`) that re-exports
  frequently used traits/types for one-line imports.

Example:

```
com.acme.http.client    // real code
com.acme.http.server    // real code
com.acme.http           // merge module: reexports client/server
com.acme.http.prelude   // prelude module: reexports traits/types for ergonomics
```

Merge and prelude modules use the standard `export { other.module.* }` mechanism
and follow the same visibility rules as any other module.

### 7.2. Module interface and exports

A **static module** (one compiled into the host image, either directly from source or via DMP/DMIR) may define many top-level items (functions, structs, traits, interfaces), but only a **selected subset** forms its *module interface*. The module interface consists of symbols that are **exported** and therefore visible to other modules.

Drift treats functions in the module interface as **can-throw entry points**:

- Every exported function is allowed to fail and therefore participates in the standard `Result<T, Error>` model.
- At the ABI level, exported Drift functions are always compiled using the **error-aware calling convention**:
  - `fn f(...) -> T` → ABI -> `Result<T, Error>` encoded as `{T, Error*}`.
  - `fn f(...) -> Void` → ABI -> `Result<Void, Error>` encoded as `Error*`.
- Internal helpers (non-exported functions) may use more aggressive internal optimizations for error handling, but their exact calling convention is not visible across module boundaries.

Import resolution (Section 7.1) only considers **exported** symbols:

- A module-qualified access `my.module.foo` is valid only if `foo` appears in
  `my.module`’s export list.
- Non-exported functions and types are private to the defining module and cannot be named from other modules.

The export set is recorded in the module’s DMIR/DMP metadata (Chapter 20). Tools use this metadata to enforce that only exported, can-throw entry points participate in cross-module linking.

### 7.3. Export surfaces and entry modules (package boundary)

`export` and `pub` define a module’s interface **within a build**. At the
package boundary, tooling may further restrict which modules are importable from
other packages via **surfaces**.

Rules:

- A package may declare one or more **surfaces** (e.g., `api`, `internal`).
- Each surface lists **entry modules** (module ids).
- Cross-package imports may only target entry modules that are listed in a
  surface of the dependency package.
- Modules not listed as entry modules are not importable cross-package, even if
  they contain `pub` items.
- Surfaces may carry an `import_notice` that is emitted once per build when an
  entry module is imported.

## 8. Control flow

Drift uses structured control flow; all loops and conditionals are block-based.

### 8.1. If/else

```drift
if cond {
    do_true();
} else {
    do_false();
}
```

- `if <cond> { ... } else { ... }` selects a branch based on a `Bool` condition.
- The condition must type-check as `Bool`; the two branches need not return the same type unless used as an expression (e.g., in a ternary).
- Each branch has its own scope for locals; names inside a branch shadow outer names.

### 8.2. While loops

```drift
var i: Int = 0;
while i < 3 {
    i = i + 1;
}
```

- `while <cond> { <stmts> }` evaluates `<cond>` each iteration and runs the body while it is `true`.
- `<cond>` must be `Bool`; type errors are reported at compile time.
- The body forms its own scope for local bindings; fresh bindings inside the loop shadow outer names and are re-created per iteration.
- `break` exits the nearest enclosing loop; `continue` jumps to the next iteration (re-evaluating the condition).

### 8.3. For loops and iterators

```drift
for item in collection {
    process(item);
}
```

- `for <name> in <expr> { <stmts> }` iterates over a value that implements the `Iterable` trait.
- The desugaring creates an iterator via `.iter()` and calls `.next()` on each iteration until `None` is returned.
- `break` and `continue` work as in `while` loops.

**Iterator throw contract (normative):**

- `SinglePassIterator.next()` and related advancement methods (`prev()` for bidirectional iterators) are `nothrow`. A `for` loop body may appear inside a `nothrow` function without triggering throw-contract diagnostics from the iteration machinery itself.
- **Iterator invalidation** (e.g., modifying a container's structure during iteration) is treated as an invariant violation. Invalidated iterators abort the process via `assert`-style failure with a diagnostic message, not via a typed `throw` from `next()`. This is deterministic and non-recoverable.

### 8.3a. Counted `for` loops

Drift provides two counted-loop forms in addition to the iterator form (§8.3). Both are pure syntax sugar over `while` + `break`/`continue`; neither introduces new control-flow primitives.

**Legacy unparenthesized counted form:**

```drift
for var i = 0; i < n; i = i + 1 {
    use(i);
}
```

- Shape: `for <init> ; <cond> ; <step> { <body> }`.
- All three clauses are **required** in this form. Omitting any of them is a parse error.
- Semantics match the parenthesized form below.

**C-style parenthesized form:**

```drift
for (var i = 0; i < n; i = i + 1) {
    use(i);
}
```

- Shape: `for ( <init>? ; <cond>? ; <step>? ) { <body> }`.
- Each of the three clauses is **independently optional**. Clause optionality applies **only** to this parenthesized form, never to the legacy unparenthesized form above.

**Init scope:**

- Bindings introduced by `<init>` are visible in `<cond>`, `<step>`, and `<body>`.
- They are **not** visible after the loop. Referencing them after the loop is a name-resolution error.

**Omitted clauses:**

- Missing `<init>`: no binding is introduced; pre-existing variables remain in scope as usual.
- Missing `<cond>`: equivalent to `true` — the loop runs until exited via `break` (or `return`/`throw`).
- Missing `<step>`: no step action runs between iterations.

**Control flow:**

- `continue` jumps to the loop header in such a way that `<step>` (if present) executes **before** `<cond>` is re-checked. This is the standard C semantics and matches what users expect from the syntactic form.
- `break` exits the loop immediately and does **not** execute `<step>`.
- `break` and `continue` always target the nearest enclosing loop, including across nested counted/iterator/`while` forms.

**Examples:**

```drift
// Full form.
for (var i = 0; i < 5; i = i + 1) {
    sum = sum + i;
}

// Missing init — uses an outer binding.
var i = 0;
for (; i < 5; i = i + 1) {
    sum = sum + i;
}

// Missing cond — infinite loop, exited via break.
for (var i = 0; ; i = i + 1) {
    if i >= 5 { break; }
    sum = sum + i;
}

// Missing step — manual increment in body.
for (var i = 0; i < 5; ) {
    sum = sum + i;
    i = i + 1;
}
```

**Non-goals:**

- This form does not change foreach/iterator loops (§8.3).
- This form does not introduce range-style loops (e.g. `for i in 0..n`).
- This form does not alter the semantics of the legacy unparenthesized counted form; the only difference between the two forms is parenthesization and clause optionality.

### 8.4. Ternary (`? :`) operator

```drift
val label = is_error ? "error" : "ok";
```

- `cond ? then_expr : else_expr` is an expression-form conditional; `cond` must be `Bool`.
- `then_expr` and `else_expr` must have the same type (checked at compile time).
- Useful for concise branching without introducing additional block nesting; when control flow is complex, prefer a full `if/else`.

### 8.5. Try/catch (expression and statement)

**Expression form (`try expr catch …`):**

```drift
val result = try parse_int(input) catch { 0 };
val logged = try parse_int(input) catch err { log(err); 0 };
val parsed = try parse_amount(input) catch BadFormat(e) { 0 };
val routed = try parse(input) catch BadFormat(e) { 0 } catch { 1 };
```

- Evaluates the attempt expression; on success, yields its value.
- On error, evaluates the `catch` arm; the arm’s block must produce a value of the same type as the attempt.
- Catch forms:
  - `catch { block }` — catch-all, no binder.
  - `catch e { block }` — catch-all, binder `e: Error`.
  - `catch EventName(e) { block }` — match a specific event in the **current module**, binder `e: Error`.
  - `catch mod:EventName(e) { block }` — match a specific event from module `mod`, binder `e: Error`.
  - Unqualified event names resolve **only** to the current module (imports are not searched).
- Multiple catch arms are allowed; event arms are tested in source order, then catch-all; if no arm matches and there is no catch-all, the error is rethrown.
- Catch blocks in expression form may **not** contain `return`, `break`, `continue`, or `rethrow`; they must evaluate to a value whose type matches the attempt. Violation diagnostic: **E-TRYEXPR-CONTROLFLOW** ("control-flow statement not allowed in try-expression catch block; use statement try { ... } catch { ... } instead").
- Event identity is by event name in source; lowering compares deterministic `event_code` constants derived from the fully-qualified event name (§14.1.1). The runtime never matches on strings.
- The attempt is any value-producing expression. Applying `try` to a statically `nothrow` expression is a compile-time error.
- This is sugar for a block-wrapped statement `try/catch` that returns the block’s value.

**Statement form (`try/catch`):**

```drift
try {
    risky();
} catch MyError(err) {
    handle(err);
}
```

- Executes the body; on error, transfers control to the first matching catch (event match or catch-all).
- Catch binder (if present) has type `Error`.
- Matching is by exception/event name only; omitting the name makes the clause a catch-all. Domains/attributes are not matched (yet).
- Event names may be unqualified (`EventName`) for the current module or module-qualified (`mod:EventName`). Unqualified names do not search imports.
- Multiple catches are allowed; event-specific arms are evaluated in source order, then catch-all. If no arm matches and there is no catch-all, the error is rethrown to the caller.
- Control falls through after the try/catch unless all branches return/raise.

### 8.6. Compound assignment

Drift supports statement-form compound assignment as syntactic sugar over the underlying binary operator and assignment paths.

**Operators:**

- arithmetic: `+=`, `-=`, `*=`, `/=`, `%=`
- bitwise:   `&=`, `|=`, `^=`, `<<=`, `>>=`

The set is closed; no other compound forms exist. The bitwise forms inherit the same operand-type constraints as their plain binary counterparts (e.g., bitwise ops in v1 require `Uint`/`Uint64`).

**Statement-only form.** Compound assignment is a *statement*, never an expression. There is no expression-valued compound assignment, and the result of `x += y` cannot be used as a value, embedded in another expression, or returned. This matches plain `x = y`.

**Valid left-hand sides.** Any place expression that is a valid LHS for plain `=` is also a valid LHS for compound assignment:

- locals (`x += y`)
- struct fields (`obj.f += y`)
- indexed places (`arr[i] += y`)
- chained projections (`obj.children[i].count += y`)
- dereferenced places (`*p += y`)

The mutability requirements are identical to plain assignment: the place must be writable (mutable local, mutable field of a writable container, element of a mutable indexable, etc.). Borrow-check rules apply unchanged.

**Single-evaluation rule (normative).** The LHS place expression is evaluated **exactly once**, regardless of how many times the equivalent expansion would textually mention it. Concretely:

- For `obj.f += y`, the receiver `obj` is evaluated once.
- For `arr[i()] += y`, the index expression `i()` is evaluated once. Side effects in `i()` are observed exactly once.
- For `arrs[i()][j()] += y`, both `i()` and `j()` are evaluated once each.
- For `*p += y`, the pointer expression `p` is evaluated once.

This rule is what distinguishes compound assignment from a naive textual `x = x op y` rewrite, and is the primary reason compound assignment is a first-class form rather than a parser macro. The compiler lowers compound assignment to a single load-modify-store cycle against the place's address; it does **not** desugar to `x = x <op> y` at the AST level.

**Type rule.** Type-checking matches `x = x <op> y`:

- The RHS must have a type that the corresponding binary operator accepts together with the LHS type, and
- the resulting binary-op type must be assignable back to the LHS place.

There are no implicit coercions beyond those that already apply to the equivalent binary operator and assignment. If `x + y` would be rejected, so is `x += y`. If `x = (x + y)` would be rejected (e.g., narrowing without an explicit cast), so is `x += y`.

**Examples:**

```drift
i += 1;
total += amount;
remaining -= used;
mask &= flag;
shifted <<= 1u;

obj.value += 5;
arr[idx] *= 2;
arrs[outer][inner] -= 1;
```

**Non-goals.**

- No `++` or `--` operators (pre- or post-increment/decrement).
- No expression-valued compound assignment (`(x += 1)` is not a value).
- No new coercion or promotion rules; behavior is exactly that of the corresponding binary op composed with assignment, modulo single-evaluation of the place.
- No compound forms beyond the ten listed above.

## 9. Reserved keywords and operators

### 9.1. Language keywords

The following identifiers are unconditionally reserved by the parser and cannot be used as user-defined names:

`fn`, `val`, `var`, `const`, `pub`, `type`, `use`, `mut`, `struct`, `variant`, `trait`, `interface`, `exception`, `implement`, `if`, `else`, `while`, `for`, `break`, `continue`, `return`, `match`, `try`, `catch`, `throw`, `rethrow`, `raise`, `yield`, `cast`, `is`, `require`, `module`, `import`, `export`, `unsafe`, `nothrow`, `throws`, `move`, `copy`, `true`, `false`.

> `move` and `copy` are also accepted as identifiers in binding positions (e.g., parameter names) via the `ident` grammar rule.

### 9.2. Operator keywords

These words function as operators in expression context:

`and` (logical AND), `or` (logical OR), `not` (logical negation).

### 9.3. Contextual keywords

Reserved only in specific grammar positions; may appear as identifiers elsewhere:

`captures` (closure capture list), `default` (match arm), `domain` (exception domain clause), `in` (for-loop binding), `as` (import/binding alias), `Fn` (function type annotation).

Decorator markers: `@tombstone`, `@test_build_only`, `@intrinsic`.

### 9.4. Reserved type names

The following type names cannot be used for user-defined struct/variant/exception/trait names:

`Int`, `Uint`, `Byte`, `Bool`, `Float`, `String`, `Void`, `Error`, `Array`, `Optional`, `FnResult`.

`DiagnosticValue` is no longer reserved (Slice 7c-3, ABI 14, 2026-05-06). The legacy DV substrate is fully retired across the public surface, runtime archive, and compiler internals (see §5.13.8). The name is now an ordinary identifier and may be used freely.

### 9.5. Operator tokens

`+`, `-`, `*`, `/`, `%`, `==`, `!=`, `<`, `<=`, `>`, `>=`, `? :`, `|>` (pipeline), `<<`, `>>`, indexing brackets `[]`, and member access `.`. These participate in precedence/associativity rules; identifiers cannot reuse them.

## 10. Variant types (`variant`)

Drift’s `variant` keyword defines **tagged unions**: a value that is exactly one of several named alternatives (constructors). Each constructor may carry its own fields.

In v1, variants are primarily consumed via the `match` **expression** (see §10.4).

### 10.1. Syntax

```drift
variant Result<T, E> {
    Ok(value: T)
    Err(error: E)
}
```

- `variant` introduces a top-level type definition.
- The type name uses UpperCamel case and may declare generic parameters (`<T, E>`).
- Each variant uses UpperCamel case and may include a field list `(field: Type, ...)`.
- At least one variant must be declared, and names must be unique within the type.
- Constructor field names are part of the public API; renaming a field is a breaking change.

#### 10.1.1. Tombstone constructors (`@tombstone`)

Array mutation operations (e.g., `pop/remove/insert/swap_remove`) move values out of array slots. For **droppable** variant payloads, the compiler must be able to synthesize a safe, non-owning value of the same type. Variants provide this via a **tombstone constructor**.

```drift
variant Maybe<T> {
    @tombstone None
    Some(value: T)
}
```

Rules:

- `@tombstone` is contextual: it is only recognized on variant arms.
- At most one arm per variant may be marked `@tombstone`.
- The tombstone arm must have **no payload** (zero fields).
- A variant is **droppable** if any arm payload type is droppable (by schema analysis, including transitive droppable types).
- Droppable variants **must** declare a tombstone arm.
- The compiler records the tombstone arm name as `tombstone_ctor` in the variant schema.

### 10.2. Semantics and representation

A `variant` value stores:

1. A hidden **tag** indicating which alternative is active.
2. The **payload** for that variant’s fields.

Only the active variant’s fields may be accessed. This is enforced statically by pattern matching.

### 10.3. Construction

Each constructor behaves like a value constructor call:

```drift
val success: Result<Int, String> = Ok(42);
val failure: Result<Int, String> = Err("oops");
```

Rules:

- Constructors are **unqualified identifiers** (`Ok`, `Err`, `Some`, `None`).
- Constructor arguments may be **positional** or **named**.
- **Do not mix** positional and named arguments in the same call.
- Named arguments bind to the **constructor field names**.
- **Unknown** field names, **duplicate** field names, **missing** required fields, or **extra** positional arguments are errors (no defaults).
- An **unqualified** constructor call in expression position requires an **expected variant type** from context (e.g., an annotation, a parameter type, or a function return type). If there is no expected type, the compiler rejects the call rather than guessing.
- **Evaluation order:** constructor arguments are evaluated in **source order** (left-to-right) even when named; assignment into the canonical field order happens after evaluation.

Examples:

```drift
val x: Optional<Int> = Some(1); // OK: expected type is Optional<Int>
val y = Some(1); // error (no expected variant type)
```

#### 10.3.1. Qualified constructor calls (`TypeRef::Ctor(...)`)

To disambiguate constructors (and to allow type argument inference without an expected type), Drift supports *qualified* constructor calls:

```drift
val x = Optional::Some(1); // OK: infers Optional<Int>
val y = Optional<Int>::None(); // OK: explicit type arguments on the variant type
val z = Optional::None<type Int>(); // OK: explicit type arguments after the constructor name
```

Rules:

- Syntax: `TypeRef::Ctor(args...)`.
- `TypeRef` is a nominal type reference (a variant type name; module-qualified type names may be supported where type references are supported).
- Only **variant constructors** may be referenced through `TypeRef::Ctor(...)`.
- The constructor name must exist on the referenced variant type and the argument count must match the constructor’s fields.
- Argument rules are the same as unqualified calls (positional or named, no mixing, named binds to field names).
- Generic type arguments may be inferred from constructor arguments:
  - `Optional::Some(1)` infers `T = Int`.
  - If type arguments are underconstrained (e.g., `Optional::None()`), the compiler rejects the call unless an expected type provides them (e.g., via a return type or an explicit annotation).

Type argument spelling:

- Either `TypeRef<T>::Ctor(...)` or `TypeRef::Ctor<type T>(...)` may be used to provide explicit type arguments for a generic variant.
- At most one explicit type-argument list may appear in a qualified constructor call.
- The explicit type-argument list always applies to the **variant type instantiation** (not to the constructor name itself). The two spellings are equivalent in v1.

Restriction:

- `TypeRef::Ctor` is not a first-class value. For example, `val f = Optional::Some` is rejected; use a direct call `Optional::Some(...)`.

### 10.4. Pattern matching and exhaustiveness

`match` is used to consume a variant. In v1, `match` is an **expression** and is also allowed as a **compound statement** (no trailing `;` needed). At least one arm is required. Arms are **comma-separated** with an optional trailing comma.

```drift
val s = match result {
    Ok(v) => { "ok: " + v.to_string() },
    Err(e) => { "error: " + e },
};
```

**Arm bodies are blocks.**

- **Expression form:** arm bodies are value blocks (a block with a final expression) and the match result is that final value.
- **Statement form:** arm bodies are blocks only (no value); statement-form matches must not yield a value.

**Default arm**:

- Non-exhaustive matches are allowed **only** if a `default` arm is present.
- `default` is a keyword (not a pattern) and introduces no bindings.
- `default` may appear **at most once** and must be **last**.

Exhaustiveness rules (v1):

- Without `default`, the match must cover every constructor of the scrutinee variant.
- With `default`, any subset of constructors may be listed.

```drift
fn describe(result: Result<Int, String>) -> String {
    return match result {
        Ok(value) => { "ok: " + value.to_string() },
        Err(error) => { "error: " + error },
    };
}
```

Matches can be nested or composed with other `variant` types:

```drift
variant Optional<T> {
    @tombstone None
    Some(value: T)
}

For ABI stability across modules, the canonical `Optional<T>` layout uses a
fixed constructor tag order: `None` is tag 0 and `Some` is tag 1, regardless of
how user code lists the arms.

variant DbError {
    ConnectionLost
    QueryFailed(message: String)
}

variant LookupResult<T> {
    Found(value: T)
    Missing
    Error(err: DbError)
}

fn describe_lookup(id: Int, r: LookupResult<String>) -> String {
    return match r {
        Found(value) => { "Record " + id.to_string() + ": " + value },
        Missing      => { "No record for id " + id.to_string() },
        Error(err)   => { match err {
            ConnectionLost       => { "Database connection lost" },
            QueryFailed(message) => { "Query failed: " + message },
            default              => { "Unknown db error" },
        } },
        default      => { "Unknown lookup result" },
    };
}
```

### 10.5. Recursive data

Recursive variants are not supported in v1. They require an explicit indirection strategy (e.g. references or an owned `Box<T>` type) to break infinite size.

### 10.6. Generics

Variants and functions may declare type parameters. Codegen monomorphizes only
the concrete instantiations used by the program.

```drift
variant PairOrError<T, E> {
    Pair(first: T, second: T)
    Error(error: E)
}

fn make_pair(x: Int, y: Int) -> PairOrError<Int, String> {
    if x == y {
        return Error("values must differ");
    }
    return Pair(x, y);
}
```

**Generic functions.** Functions may declare type
parameters and be instantiated explicitly. Call-site type arguments require
the `type` marker to avoid ambiguity with comparisons:

```drift
fn id<T>(value: T) -> T { return value }
val x = id<type Int>(1);
```

Type arguments may be inferred from arguments in obvious cases, but if any type
parameter remains underconstrained the call is rejected unless explicit type
arguments are provided.

### 10.7. Value semantics and equality

Variants follow Drift’s value semantics: they are copied/moved by value, and their equality/ordering derive from their payloads. Two `Result` values are equal only if they hold the same variant *and* the corresponding fields are equal.

### 10.8. Evolution considerations

- Adding a new variant is a **breaking change** because every `match` must handle it explicitly.
- Library authors should document variant additions clearly or provide fallback variants when forward compatibility matters.

Variants underpin key library types such as `Result<T, E>` and `Optional<T>`, enabling safe, expressive modeling of operations with multiple outcomes.


## 11. Null safety & optional values

Drift is **null-free**. There is no `null` literal. A value is either present (`T`) or explicitly optional (`Optional<T>`). The compiler never promotes `Optional<T>` to `T` implicitly.

### 11.1. Types

| Type | Meaning |
|------|---------|
| `T` | Non-optional; always initialized. |
| `Optional<T>` | Possibly empty; either a value or nothing. |

### 11.2. Construction

```drift
val present: Optional<Int> = Some(42);
val empty: Optional<Int> = None();
```

### 11.3. Control flow

```drift
match qty {
    Some(q) => { println("qty=" + q.to_string()); },
    None => { println("no qty"); },
}
```

There is no safe-navigation operator (`?.`). Access requires explicit pattern matching or helper combinators built atop `Optional<T>`.

### 11.4. Parameters & return types

- A parameter of type `T` cannot receive `None()`.
- Use `Optional<T>` for "maybe" values.
- Returning `None()` from a function declared `-> T` is a compile error.

```drift
fn find_sku(id: Int) -> Optional<String> { /* ... */ }

val sku = find_sku(42);
match sku {
    Some(s) => { println("sku=" + s); },
    None => { println("missing"); },
}
```

### 11.5. Ownership

Pattern matching moves the bound value by default. If you need to borrow instead, destructure a reference to the `Optional` and match on that (planned once borrow-patterns are added).

### 11.6. Diagnostics (illustrative)

- **E2400**: cannot assign `None()` to non-optional type `T`.
- **E2401**: attempted member/method use on `Optional<T>` without pattern matching / combinators.
- **E2402**: attempted unwrap of `None` (discouraged pattern).
- **E2403**: attempted implicit conversion `Optional<T>` → `T`.

### 11.7. End-to-end example

```drift
struct Order {
    id: Int,
    sku: String,
    quantity: Int
}

fn find_order(id: Int) -> Optional<Order> {
    if id == 42 { return Some(Order(id = 42, sku = "DRIFT-1", quantity = 1)); }
    return None();
}

fn ship(o: Order) -> Void {
    println("shipping " + o.sku + " id=" + o.id);
}

fn example() -> Void {
    val maybe_order = find_order(42);

    match maybe_order {
        Some(o) => { ship(o); },
        None => { println("order not found"); },
    }
}

---
## 12. `lang.array`, `ByteBuffer`, and array literals

`lang.array` is the standard module for homogeneous sequences. It exposes the generic type `Array<T>` plus builder helpers and the binary-centric `ByteBuffer`. `Array` is available via the type prelude, so you can write:

```drift
fn example() -> Void {
    val names: Array<String> = ["Bob", "Alice", "Ada"];
    println("names ready");
}
```

Array literals follow the same ownership and typing rules as other expressions:

```drift
val nums = [1, 2, 3]; // infers Array<Int>
val names = ["Bob", "Alice"]; // infers Array<String>

val explicit: Array<Int> = [1, 2, 3]; // annotation still allowed when desired
```

- `[expr1, expr2, ...]` constructs an `Array<T>` where every element shares the same type `T`. The compiler infers `T` from the elements.
- Mixed-type literals (`[1, "two"]`) are rejected during type checking (compile-time error).
- **Restriction:** non-empty array literals require `T` to be `Copy`. Empty literals (`[]`) are allowed only with an explicit `Array<T>` type annotation.
- Empty literals are reserved for a future constructor; for now, call the stdlib helper once it lands.

**Builtin Array (stepping stone).** `Array<T>` is a builtin container in v1 primarily to support literal syntax and a fixed ABI layout, but its semantics are aligned with the stdlib RawBuffer recipe: uninitialized capacity, initialized prefix `[0..len)`, move-out on pop/remove, and `gen` invalidation only on structural change. Indexing is specified as trait-based (operator lowering) so user containers can implement `[]` without being builtin. The long-term direction is: keep literal syntax as a compiler primitive that lowers to a typed literal payload, and move `Array<T>` into stdlib once the unsafe/Ptr story is fully stabilized; until then, the builtin must stay behaviorally equivalent to the RawBuffer-backed implementation to avoid drift.

`Array<T>` integrates with the broader language design — it moves with `move x`, can be captured with `^`, and will participate in trait implementations like `Display` once the stdlib grows. The literal syntax keeps sample programs succinct while we flesh out higher-level APIs.

**Indexing and lengths.** In v1, container lengths/capacities and indices use `Int`:

- `Array<T>.len: Int`
- `Array<T>.capacity: Int`
- `ByteBuffer.len: Int`
- `ByteSlice.len: Int`

Any function that indexes into a container or string must accept an `Int` index; bounds checks reject negative indices and indices ≥ `len`. Lengths/capacities are signed counts (`Int`) with the invariant `len >= 0` / `capacity >= 0`. Examples elsewhere that show `Size` are illustrative; the canonical type for array/string lengths and indices in v1 is `Int`.

### 12.1. ByteBuffer, ByteSlice, and MutByteSlice

#### 12.1.1. Borrowing rules and zero-copy interop

`ByteSlice`/`MutByteSlice` behave like other Drift borrows:

- A `ByteSlice` (`&ByteSlice`) is a shared view: multiple readers may coexist, but none may mutate.
- A `MutByteSlice` (`&MutByteSlice`) is an exclusive view: while it exists, no other references (mutable or shared) to the same range are allowed.
- Views never own memory. They rely on the original owner (often a `ByteBuffer` or foreign allocation) to outlive the slice’s scope. Moving the owner invalidates outstanding slices, just like any other borrow.

These rules integrate with `Send`/`Sync` (see Chapter 5, thread-safety marker traits): a `ByteSlice` is `Send`/`Sync` because it is immutable metadata; a `MutByteSlice` is neither, so you cannot share a mutable view across threads without additional synchronization.

This design yields zero-copy interop: host code can wrap foreign `(ptr, len)` pairs in `ByteSlice`, pass them through Drift APIs, and guarantee the callee sees the original bytes without copying. Likewise, `ByteBuffer.as_mut_slice()` hands a shared library a raw view to fill without reallocations. Lifetimes stay explicit and deterministic, avoiding GC-style surprises.


Binary APIs use three closely related stdlib types:

| Type | Role |
|------|------|
| `ByteBuffer` | Owning, growable buffer of contiguous `Byte` values (move-only). |
| `ByteSlice` | Immutable borrowed view into existing bytes (`len`, `data_ptr`). |
| `MutByteSlice` | Exclusive borrowed view for writing bytes in place. |

`ByteBuffer` lives in `lang.array.byte` and follows the same ownership rules as other containers. Constructors include:

```drift
var buf = ByteBuffer.with_capacity(4096);
val literal = ByteBuffer.from_array([0x48, 0x69]);
val from_utf8 = ByteBuffer.from_string("drift");
```

Core operations:

- `fn len(self: &ByteBuffer) -> Int` — number of initialized bytes.
- `fn capacity(self: &ByteBuffer) -> Int` — reserved storage.
- `fn clear(self: &mut ByteBuffer) -> Void` — resets `len` to zero without freeing.
- `fn push(self: &mut ByteBuffer, b: Byte) -> Void`
- `fn extend(self: &mut ByteBuffer, slice: ByteSlice) -> Void`
- `fn as_slice(self: &ByteBuffer) -> ByteSlice`
- `fn slice(self: &ByteBuffer, start: Int, len: Int) -> ByteSlice`
- `fn as_mut_slice(self: &mut ByteBuffer) -> MutByteSlice`
- `fn reserve(self: &mut ByteBuffer, additional: Int) -> Void`

`ByteSlice`/`MutByteSlice` are lightweight descriptors (`{ ptr, len }`). They do not own memory; borrow rules ensure the referenced storage stays alive for the duration of the borrow. `MutByteSlice` provides exclusive access, so you cannot obtain a second mutable slice while one is active.

Typical I/O pattern:

```drift
fn copy_stream(src: InputStream, dst: OutputStream) -> Void {
    var scratch = ByteBuffer.with_capacity(4096);

    loop {
        scratch.clear();
        val filled = src.read(scratch.as_mut_slice());
        if filled == 0 { break }

        val chunk = scratch.slice(0, filled);
        dst.write(chunk);
    }
}
```

`read` writes into the provided mutable slice and -> the number of bytes initialized; `slice` then produces a read-only view of that prefix without copying. FFI helpers in `lang.abi` can also manufacture `ByteSlice`/`MutByteSlice` wrappers around raw pointers for zero-copy interop.


### 12.2. Indexing, mutation, and borrowing
#### 12.2.1. Borrowed element access

`Array<T>` exposes **borrowed** element access via:

```drift
fn get(self: &Array<T>, index: Int) -> Optional<&T>
```

and via direct borrows of indexed places:

```drift
val p = &arr[i];
val q = &mut arr[i];
```

- `get` returns `None` on out-of-range indices (no throw).
- Direct indexing (`arr[i]`) is **Copy-only**; for non-`Copy` elements, use `get` or borrow the indexed place.


Use square brackets to read an element:

```drift
val nums = [1, 2, 3];
val first = nums[0];
```

Assignments through an index require the binding to be mutable:

```drift
var mutable_values: Array<Int> = [5, 10, 15];
mutable_values[1] = 42; // ok

val frozen = [7, 8, 9];
frozen[0] = 1; // compile error: cannot assign through immutable binding
```

Nested indexing works as expected (e.g., `matrix[row][col]`) as long as the root binding is declared with `var`.


## 13. Collection literals (arrays and maps)

### 13.1. Array literals

`[expr1, expr2, ...]` constructs an `Array<T>` directly (see §12). The compiler infers `T` from the elements; all elements must unify to a single type.

```drift
val xs = [1, 2, 3];               // Array<Int>
val names = ["Bob", "Alice"];      // Array<String>
val explicit: Array<Int> = [1, 2]; // annotation allowed
```

### 13.2. Map literals

`{ key: value, ... }` constructs a map. Map literals are **target-directed**: the compiler uses the expected type from context to determine the concrete map type. When no expected type constrains a non-empty literal, the default target is `HashMap<K, V>` (backed by `std.containers.HashMapCore` with `DefaultBuildHasher`). Other map-like types (e.g., `TreeMap<K, V>`) may be targeted via type annotation. Empty map literals (`{}`) always require an expected type or explicit annotation; the compiler cannot infer a target type from zero entries.

```drift
val scores = { "alice": 10, "bob": 20 };                // HashMap<String, Int>
val tree: TreeMap<String, Int> = { "x": 1, "y": 2 };    // target-directed
val empty: HashMap<String, Int> = {};                    // annotation required
```

Duplicate keys are allowed in the literal; the target type decides whether to keep the first value, last value, or reject duplicates.

### 13.3. Brace forms are disjoint

Drift uses braces for two distinct constructs with disjoint syntax:

- **Map literal:** `{ expr_key: expr_value, ... }` — keys and values are arbitrary expressions. Map literals **only** use `:`.
- **Struct initializer:** `TypeName { field = expr, ... }` — `TypeName` resolves to a declared struct; fields are identifiers checked against the struct declaration; uses `=`.

There is no ambiguous brace form: `{k: v}` is always a map literal; `Type { f = v }` is always a struct initializer (a "named field initializer").

Exceptions use **constructor syntax** (`ExcName(...)`) rather than braces (§14.3.2).

### 13.4. Diagnostics

- `[1, "two"]` → error: element types do not unify.
- `{}` without a target type → error: cannot infer target type for empty map literal; add a type annotation.
- Mixed-type literals are rejected during type checking (compile-time error).


## 14. Exceptions and error context

Drift provides structured exception handling through a single `Error` type, **exception events**, and the `^` capture modifier.  
Exception declarations create constructor names in the value namespace. `throw ExcName(...)` is valid syntax: arguments must match the declared names/types, produce an `Error` value with the exception’s deterministic `event_code`, and integrate with the existing `try/catch` event dispatch. Every exception attribute is recorded in `Error.params` as a JSON value, and any `^`-captured locals are recorded in `Error.context` as a JSON array of frame objects; both are diagnostics, not user-facing payloads.
Exceptions are **not** UI messages: they carry machine-friendly context (event name, arguments, captured locals, stack) that can be logged, inspected, or transmitted without embedding human prose.
`Error` itself is a catch-all handler type: user functions do not return `Error` or throw `Error` directly; they throw concrete exception events, and catch blocks may bind either a specific exception type or `Error` as a generic binder.

**Exceptions and `Result<T, Error>` coexist, with distinct roles (single semantic model).**
- **Exceptions** are the surface mechanism for propagating failures: semantically, every can-throw computation produces a `Result<T, Error>`, and `throw` / `try` / `catch` are the structured syntax for constructing, inspecting, and propagating that result while capturing context. Implementations may realize propagation via unwinding within static modules, but the semantic model remains result-based.
- **`Result<T, Error>`** is the value-level encoding of a potentially failing computation. It is explicit in signatures and ideal for "expected" failures where the caller stays in-band (`match`/pattern matching, pipelines, etc.).
- Drift has one semantic error model: a can-throw computation produces a `Result<T, Error>` (conceptually), and `throw`/`try`/`catch` are surface sugar that construct, inspect, and propagate that `Result` while capturing context for debugging/logging.

### 14.1. Goals
Drift’s exception system is designed to:

- Use **one concrete error type** (`Error`) for all thrown failures.
- Represent failures as **event names plus arguments**, not free-form text.
- Capture **call-site context** (locals per frame + backtrace) automatically.
- Preserve a **precise, frozen ABI layout** so exceptions can propagate across Drift modules and plugins.
- Fit cleanly over a conceptual `Result<T, Error>` model for internal lowering and ABI design.
- Respect **move semantics**: `Error` is move-only and is always transferred with `move e`.

---

### 14.1.1. Source identity vs. runtime identity

- **Source identity** of an exception event is its fully-qualified name `"<module_name>.<submodule...>:<event>"` (canonical, no aliases). Catch clauses resolve to this FQN:
  - `catch EventName(...)` resolves to the current module’s event `"<current_module>:EventName"`.
  - `catch mod:EventName(...)` resolves to the named module’s event `"<mod>:EventName"`.
  Non-canonical/ambiguous names are rejected.
- **Runtime identity** is a deterministic 64-bit `event_code = hash_v1(fqn_utf8_bytes)` (UTF-8 of the canonical FQN); users never type or see codes.
- `catch m:Evt` lowers to `if err.event_code == hash_v1(fqn)`; matching is by code, derived from the resolved FQN with the `:` delimiter.
- `event_code == 0` is reserved for **unknown/unmapped** events (e.g., absent catalog entry); user-defined events must never deliberately use code 0.
- Collisions detected during compilation are fatal within the build; if/when multi-module linking is introduced, collision handling must remain deterministic.
- `event_fqn()` -> the stored canonical FQN string label for logging/telemetry; it is never used for control flow or matching.

---

### 14.2. Error type and layout

```drift
type ErrorCode = Uint64

struct Error {
    event_code: Uint64,        // stable runtime routing key (see §14.1.1)
    event_fqn: String,         // canonical FQN label (for logging/telemetry only)
    params: JsonHandle,        // declared exception fields, JSON object (see §14.2.2)
    context: JsonHandle,       // ^-captured frames, JSON array of frame objects (see §14.2.3)
    stack: BacktraceHandle     // opaque backtrace
}

exception IndexError {
    container_id: String,
    index: Int,
}
```

The `Error` value is the user-visible catch binder. Internally the runtime stores `params` and `context` as a single JSON document (see ABI spec §2 for the on-the-wire shape); accessor methods return `JsonHandle` views without forcing the user to know the storage form.

#### 14.2.1. event_code
Deterministic 64-bit code derived from the exception’s fully-qualified name (`"<module>.<submodules>:<event>"`) using the frozen hash (§14.1.1). This is the **only** runtime routing key. `0` is reserved for unknown/unmapped events and must not be produced by user-declared exceptions.

#### 14.2.2. params
JSON object whose entries are the exception’s declared fields, keyed by field name. Each declared field value is produced via `Diagnostic.to_json()` (§5.13.7) and stored as a `JsonNode`. Every declared field is stored under its own name — there is no special "primary payload" field. Any field type used in an exception declaration must implement `Diagnostic`; attempting to declare a non-Diagnostic field type is a compile-time error.

`params` is exposed to user code as a `JsonHandle`. The primary public surface is the dump form `e.params.encode_compact() -> String`; fluent lookup goes through a `JsonCursor` (`e.params.get("k").as_int()`) that distinguishes "absent key" from "present-and-explicit JSON null" — see §5.13.8 and §14.5.4.

#### 14.2.3. context
JSON array of frame objects in unwind order, one entry per frame that captured locals via `^` (see §14.4). Each frame object has the shape:

```json
{ "fn_name": "<function name>", "locals": { "<name>": <JsonNode>, ... } }
```

Frame `locals` values are produced via `Diagnostic.to_json()`. Event params never appear here. Frames are appended in unwind order (innermost first); a function that unwinds without capturing contributes no frame.

#### 14.2.4. stack
Opaque captured backtrace.

#### 14.2.5. event_fqn
Canonical FQN string label (`"<module>.<submodules>:<event>"`) stored with the error. It is **not** used for matching; matching is by `event_code`. Exposed via `event_fqn()` for logging/telemetry.

---

### 14.3. Exception events

#### 14.3.1. Declaring events
```drift
exception InvalidOrder {
    order_id: Int,
    code: String,
}
exception Timeout {
    operation: String,
    millis: Int,
}
```

Each field type must implement the `Diagnostic` trait (see §5.13.7).

#### 14.3.2. Throwing
```drift
throw InvalidOrder(order_id = order.id, code = "order.invalid");
```

Runtime builds an `Error` with:
- `event_code = hash(fqn)` (see §14.1.1)
- `params`: each declared field converted via `Diagnostic.to_json()` into a `JsonNode` and stored under its name in a JSON object; every declared field is stored — there is no special "primary payload" field; every field type must implement the `Diagnostic` trait (§5.13.7)
- empty `context` (frame objects appended during unwind, see §14.4)
- backtrace

Exception throws use constructor syntax only:
- `throw E(...)` (parentheses are required even for zero-field exceptions: `throw Timeout()`).
- The argument list must supply **exactly** the declared fields.
- Positional arguments map to declared fields in declaration order; positional arguments must precede keyword arguments.
- Unknown fields, missing fields, or duplicate fields are compile-time errors.

#### 14.3.3. Diagnostic requirement
Each exception field type must implement `Diagnostic` (see §5.13.7) so the runtime can project the field value to a `JsonNode` via `to_json(&Self) -> JsonNode`.

#### 14.3.4. Event code derivation and collision policy
- Canonical FQN string: `"<module_name>.<submodule...>:<event>"` with UTF-8 encoding, no aliases or whitespace.
- Hash algorithm: `hash_v1(fqn_utf8_bytes)` (frozen; currently xxhash64, truncated/encoded as unsigned 64-bit).
- Runtime routing key: `event_code = hash_v1(fqn_utf8_bytes)`.
- Collision policy: any collision detected within a build is a **compile-time error**. If cross-module linking is introduced, collision handling must remain deterministic; with FQN input the practical risk is negligible.
- Tooling/debug mapping: see §14.6.5 for the code→name table carried in DMIR/metadata.

---

### 14.4. Capturing local context with ^

Locals can be captured:

```drift
val ^input: String as "record.field" = s;
```

A frame object is appended to `Error.context` when unwinding past the function:

```json
{
  "fn_name": "parse_date",
  "locals": { "record.field": "2025-13-40" }
}
```

Rules:
- Only `^`-annotated locals are captured.
- Local values must implement `Diagnostic` (§5.13.7); each value is projected via `to_json` to a `JsonNode`.
- Capture happens once per frame; a function that unwinds without any `^` capture contributes no frame.

---

### 14.5. Throwing, catching, rethrowing

`Error` is move-only.

#### 14.5.1. Catch by event
```drift
try {
    ship(order);
} catch shop.orders:InvalidOrder(e) {
    log(&e);
}
```

Matches by `error.event_code` derived from the resolved fully-qualified event name (§14.1.1); the source uses the event name, runtime compares the deterministic code.
Unqualified `catch EventName(...)` resolves to the current module’s event; `catch mod:EventName(...)` targets another module. No implicit prefixing is performed beyond the current module.

#### 14.5.2. Catch-all + rethrow
```drift
catch e {
    log(&e);
    rethrow;
}
```

Ownership moves back to unwinder.

#### 14.5.3. Inline catch-all shorthand

For a single call where you just want a fallback value, use the one-liner form:

```drift
val date = try parse_date(input) catch { default_date };
```

This is sugar for a catch-all handler:

```drift
val date = {
    try { parse_date(input) }
    catch _ { default_date }
};
```

The `else` expression must produce the same type as the `try` expression. Exception context (`event`, attrs, captured locals, stack) is still recorded before control flows into the `else` arm.

---

#### 14.5.4. Accessing params and context

Most handlers do not parse individual fields; they record the full envelope for logs/audits. The public surface is therefore led by the **dump path**, with structured traversal and typed lookup as secondary.

**Dump path (primary).**

```drift
val whole   = e.encode_compact();             // canonical envelope JSON (event_code,
                                              // event_fqn, params, context, stack)
val params  = e.params.encode_compact();      // declared exception fields, JSON object
val frames  = e.context.encode_compact();     // ^-captured frames, JSON array
log(&whole);
```

`e.encode_compact()` is the canonical full-envelope log/save form. It emits directly from the stored canonical JSON strings (`params_json`, `context_json` — see ABI spec §2) without an intermediate parse/re-encode step.

**Structured traversal.**

```drift
val tree: JsonHandle = e.to_json();   // materialize whole envelope as JsonHandle
val params_handle    = e.params;      // JsonHandle over the params subtree
```

`e.to_json()` materializes the full envelope as a `JsonHandle` tree for callers that need traversal; `e.params` and `e.context` expose the named subtrees directly without re-materializing the envelope.

**Typed lookup (secondary).**

```drift
val code = e.params.get("sql_code").as_int();             // Optional<Int>
val cust = e.params.get("order").get("customer").get("id").as_string();

for frame in e.context.iter() {
    val name  = frame.get("fn_name").as_string();
    val value = frame.get("locals").get("record.field").as_string();
}
```

`JsonHandle::get` and `JsonCursor::get` return a `JsonCursor` (§5.13.8). `JsonCursor` distinguishes **absent** keys/indices from **present-and-explicit JSON null** — both yield `Optional.none` from the typed `.as_*()` accessors, but the cursor itself records which case occurred for callers that need to discriminate. `get`, `iter`, and the typed `.as_*()` accessors are all non-throwing; out-of-range indices and wrong-typed values likewise produce a cursor that returns `Optional.none` from typed reads.

---

### 14.6. Internal Result<T, Error> semantics

(See Chapter 10 for the `variant` definition and `Result<T, E>` basics.)

Conceptual form:

```drift
variant Result<T, E> {
    Ok(value: T)
    Err(error: E)
}
```

Every function behaves as if returning `Result<T, Error>`; ABI lowers accordingly.

When a function is part of a module’s exported interface (Chapter 7.2), the `Result<T, Error>` model is **visible at the ABI**:

- Exported functions always use the `Result<T, Error>` calling convention on the wire, encoded as `{T, Error*}` or `Error*` at the ABI.
- Callers in other modules must treat every exported function as potentially failing, even if its implementation never actually throws.
- Internal, non-exported functions may be lowered more aggressively (for example, eliding the error channel when analysis proves "no throw"), but such optimizations must not change the behavior of exported entry points as seen through the module interface.

**RAII and destruction semantics.**

Destructors and deterministic resource cleanup (`Destructible`, scope exit, and move semantics) behave **exactly as if error propagation were implemented purely via explicit `Result<T, Error>` values**, regardless of whether a particular implementation realizes propagation using unwinding internally.

In particular:
- All destructors that would run on normal control-flow return also run during error propagation.
- No destructor is skipped or reordered due to unwinding.
- Error propagation must not introduce observable differences in lifetime or drop behavior.

Unwinding, when used, is an **implementation strategy only** and must preserve the same destruction semantics as value-based propagation.

---

#### 14.6.1. Can-throw vs non-throw functions (nothrow rules)

Drift distinguishes **can-throw** functions from **non-throwing** ones and enforces the contract statically:

- **Can-throw is an effect, not a type.** Surface signatures remain `fn f(...) -> T`. A function is considered can-throw when its body may throw (via `throw`/`rethrow` or uncaught calls to other can-throw functions). A function may be declared **nothrow** with `fn f(...) nothrow -> T { ... }`; this is a compile-time constraint that forbids escaping throws.
- **Non-throwing invariants.** A non-throwing function must not use `throw`/`raise`/`rethrow`, must not construct an `Error`, and must not allow an exception to escape. It may call can-throw functions only if it handles failures locally (e.g., via `try/catch`) and still -> a plain `T`. Violations are compile-time errors tied to the source span of the offending statement/expression.
- **Can-throw invariants.** A can-throw function may throw and may call other can-throw functions without local handling; exceptions propagate to the nearest enclosing `try/catch` or to the caller.
- **ABI clarity.** The compiler lowers can-throw functions to the internal `Result<T, Error>` calling convention for codegen/ABI purposes (not a surface-level type). Non-throwing functions use plain -> internally; exported functions always use the `Result<T, Error>` ABI at module boundaries. Mixing conventions within a single call boundary is rejected rather than silently coerced.

These rules keep the error model explicit, prevent accidental unwinding from non-throwing code, and make cross-module ABIs predictable.

---

#### 14.6.2. Event-code metadata for tooling
- DMIR carries a table `{ event_code -> fully-qualified event name }` (and may include declared field schemas) for diagnostics, logging, and tooling.
- Runtime/host APIs may expose a resolver to turn `event_code` into a name for display. This mapping is **not** required for correctness or routing; matching always uses codes (§14.1.1).

---

### 14.7. Drift–Drift propagation across static modules

Unwinding is allowed across **static Drift modules** as long as:
- The `Error` layout used by those modules is identical.
- They share the same runtime and unwinder.

This applies to modules that are compiled together into a single image (either directly from source or via DMIR/DMP). **Unwinding must not cross FFI or OS-level shared library boundaries**; any exported Drift APIs used via C/FFI must convert failures into value errors at the boundary (see Chapter 17).

Event code + event_fqn + params + context + stack fully capture portable state.

---

### 14.8. Logging and serialization
The canonical full-envelope projection is `e.encode_compact()` (§14.5.4) — a `String` shaped as:

```json
{
  "event_fqn": "orders:InvalidOrder",
  "event_code": "0x1…",
  "params": { "order_id": 42, "code": "order.invalid" },
  "context": [
    { "fn_name": "ship",          "locals": { "record.id": "42" } },
    { "fn_name": "ingest_order",  "locals": { "batch": "B1" } }
  ],
  "stack": "opaque"
}
```

Because the runtime already stores `params` and `context` as canonical JSON strings (ABI spec §2), `e.encode_compact()` emits this envelope by string-splicing the stored buffers — no parse/materialize/re-encode step. The legacy DV→JSON conversion at logging time no longer exists. Whitespace and key ordering inside the inner segments are not stable (see ABI §2.2); structural callers must parse rather than byte-compare.

---

### 14.9. Summary

- Single `Error` type.
- Event-based exceptions.
- Declared exception fields stored as JSON `params`; `^`-captured locals stored as a JSON array of frame objects in `context`.
- Public surface is dump-first: `e.encode_compact()` emits the full envelope directly from stored canonical JSON strings; `e.to_json()` materializes a `JsonHandle` tree for traversal; `e.params.get("k").as_*()` is the cursor-based fluent lookup. Cursors distinguish absent keys from explicit JSON null.
- Move-only errors with deterministic ownership.
- Precisely defined layout for cross-module-safe unwinding.
- Semantically equivalent to `Result<T, Error>` internally.

## 15. Mutators, transformers, and finalizers

In Drift, a function’s **parameter ownership mode** communicates its **lifecycle role** in a data flow.  
This distinction becomes especially clear in pipelines (`|>`), where each stage expresses how it interacts with its input.

### 15.1. Function roles

| Role | Parameter type | Return type | Ownership semantics | Typical usage |
|------|----------------|--------------|---------------------|----------------|
| **Mutator** | `&mut T` | `Void` or `T` | Borrows an existing `T` mutably and optionally -> it. Ownership stays with the caller. | In-place modification, e.g. `fill`, `tune`. |
| **Transformer** | `T` | `U` (often `T`) | Consumes its input and -> a new owned value. Ownership transfers into the call and out again. | `compress`, `dup`, `serialize`. |
| **Finalizer / Sink** | `T` | `Void` | Consumes the value completely. Ownership ends here; the resource is destroyed or released at function return. | `finalize`, `close`, `free`, `commit`. |

### 15.2. Pipeline behavior

The pipeline operator `|>` is **ownership-aware**.  
It is left-associative and automatically determines how each stage interacts based on the callee’s parameter type:

```drift
fn fill(f: &mut File) -> Void { /* mutate */ }
fn tune(f: &mut File) -> Void { /* mutate */ }
fn finalize(f: File) -> Void { /* consume */ }

open("x")
  |> fill      // borrows mutably; File stays alive
  |> tune      // borrows mutably again
  |> finalize; // consumes; File is now invalid
```

- **Mutator stages** borrow temporarily and return the same owner.
- **Transformer stages** consume and return new ownership.
- **Finalizer stages** consume and end the pipeline.
- **Desugaring intuition:** `x |> f` behaves like `f(x)`, and `x |> g(a, b)` behaves like `g(x, a, b)`. Pipelines are left-associative, so `a |> f |> g` becomes `g(f(a))`. Ownership follows the parameter types of each stage (borrow vs move).

At the end of scope, if the value is still owned (not consumed by a finalizer), RAII automatically calls its destructor.

### 15.3. Rationale

This mirrors real-world resource lifecycles:
1. Creation — ownership established.  
2. Mutation — zero or more `&mut` edits.  
3. Transformation — optional `T → U`.  
4. Finalization — release or destruction.

Explicit parameter types make these transitions visible and verifiable at compile time.

### 15.4. RAII interaction

All owned resources obey RAII: their destructors run automatically at scope end.  
Finalizers are **optional** unless early release, explicit error handling, or shared-handle semantics require them.

```drift
{
    open("x")
      |> fill
      |> tune; // RAII closes automatically here
}

{
    open("x")
      >> fill
      >> tune
      >> finalize; // explicit end-of-life
}
```

In both cases, the file handle is safely released exactly once.

### 15.5. Destructors and moves

- Deterministic RAII: owned values run their destructor at end of liveness—scope exit, early return, or after being consumed by a finalizer. No deferred GC-style cleanup.
- Move-only by default: moving a value consumes it; the source binding becomes invalid and is not dropped there. Drop runs exactly once on the final owner.
- Copy types opt in: only `Copy` types may be implicitly duplicated; they must provide an O(1), non-allocating duplication path. `Copy` does not imply "no drop" (e.g., `String` retains/releases on copy). The internal `BitCopy` predicate enables memcpy fast paths but is not user-visible.

### 15.6. Unsafe code and raw pointers

Drift supports a minimal unsafe surface for C-API wrappers and FFI:

- `unsafe fn` declares an unsafe function; it may perform unsafe operations.
- `unsafe { ... }` blocks permit calling unsafe functions inside safe ones.
- Unsafe syntax is accepted only when the compiler is invoked with `--allow-unsafe`; otherwise it is a compile-time error.
- Calls to `extern "C"` functions require an `unsafe` block at the call site (see §21).

Raw pointers are represented by a distinct type `Ptr<T>`, with a user-facing alias `RawPtr<T>`:

- `RawPtr<T>` is the user-facing name; it resolves to the internal `Ptr<T>`.
- `Ptr<T>` / `RawPtr<T>` requires `T` to be sized; fat pointers are not supported in v1.
- `Ptr<T>` / `RawPtr<T>` is `Copy` and does not participate in borrow checking.
- There are no implicit conversions between `Ptr<T>` and `&T` / `&mut T`.
- Pointer read/write/offset operations are only permitted in unsafe contexts and are provided by `std.mem`.
- `RawPtr<T>` is FFI-safe and maps to an opaque pointer (`void *` / `i8*`) at the C ABI boundary (see §17.5, §21).

## 16. Memory model

This chapter defines Drift's rules for value storage, initialization, destruction, and dynamic allocation. The goal is predictable semantics for user code while relegating low-level memory manipulation to the standard library and `lang.abi`.

Drift keeps raw pointer manipulation out of safe code. Raw pointer operations exist only in unsafe contexts, and user-visible safe code works with typed values, references, and safe containers like `Array<T>`.

### 16.1. Value storage

Every sized type `T` occupies `size_of<T>()` bytes. Sized types include primitives, structs whose fields are all sized, and generic instantiations where each argument is sized. These values may live in locals, struct fields, containers, or temporaries. The compiler chooses the actual storage (registers vs stack) and that choice is unobservable.

#### 16.1.1. Initialization & destruction

- A value must be initialized exactly once before use.
- A value must be destroyed exactly once when it leaves scope or is overwritten.
- Types with destructors run them during destruction; other types are dropped with no action.

**Error propagation and destruction.**

Resource destruction is deterministic and independent of the mechanism used to propagate errors. Whether a failure is propagated via explicit value inspection or via internal unwinding, the set and order of destructor invocations is identical.

#### 16.1.2. Uninitialized memory

User code never manipulates uninitialized memory. Library internals rely on two sealed helpers:

- `Slot<T>` — typed storage for one `T`.
- `Uninit<T>` — marker used to construct a `T` inside a slot.

Only standard library `@unsafe` code touches these helpers.

### 16.2. Raw storage

`lang.abi` defines an opaque `RawBuffer` representing raw bytes that are not yet interpreted as typed values. Only allocator intrinsics can produce or consume a `RawBuffer`; user code cannot observe its address or layout. Growable containers use `RawBuffer` to reserve contiguous storage for multiple elements of the same type.

### 16.3. Allocation & deallocation

The runtime exposes three allocation primitives to the standard library:

```drift
module lang.abi;

struct RawBuffer { /* opaque */ }
struct Layout { size: Int, align: Int }

@intrinsic fn size_of<T>() -> Int;
@intrinsic fn align_of<T>() -> Int;

@unsafe fn alloc(layout: Layout) -> RawBuffer;
@unsafe fn realloc(buf: RawBuffer, old: Layout, new: Layout) -> RawBuffer;
@unsafe fn dealloc(buf: RawBuffer, layout: Layout) -> Void;

Note (v1 implementation): container allocation currently lowers to the runtime
helpers `drift_alloc_array` / `drift_free_array` (plus pointer arithmetic) rather
than `lang.abi` calls. The `lang.abi` surface is the long-term abstraction and
should be used as the canonical spec entry point; the current runtime hooks are
an implementation detail of v1 codegen.
```

- `alloc` -> uninitialized storage for a layout.
- `realloc` resizes an existing allocation, preserving contents when possible.
- `dealloc` releases storage.

Only containers and other stdlib internals call these functions; user code cannot.

### 16.4. Layout of contiguous elements

Containers such as `Array<T>` store `cap` elements of type `T` in a contiguous region computed as:

```
layout_for<T>(cap):
    size = size_of<T>() * cap
    align = align_of<T>()
```

Guarantees:

- If `cap == 0`, a distinguished empty buffer may be used.
- If `cap > 0`, the container holds a `RawBuffer` allocated with `layout_for<T>(cap)`.
- That buffer may only be resized or freed via `realloc`/`dealloc`.

### 16.5. Growth of containers

#### 16.5.1. Overview

Growable containers track both `len` (initialized elements) and `cap` (reserved slots). When `len == cap`, they obtain a larger `RawBuffer` and move existing elements—this is capacity growth.

#### 16.5.2. Array layout

```drift
struct Array<T> {
    len: Int     // initialized elements
    cap: Int     // reserved slots
    gen: Int     // invalidation counter (increments on structural change)
    ptr: Ptr<Byte>
}
```

Invariant: indices `0 .. len` are initialized; `len .. cap` are uninitialized slots ready for construction. The `ptr` is the RawBuffer handle for storage sized to `cap` elements (ABI-flattened as a raw pointer), and `gen` increments only when structural state actually changes (len/cap change or allocation moves). Growth occurs before inserting when `len == cap`.

#### 16.5.3. Growth algorithm

```
fn grow<T>(&mut self: Array<T>) @unsafe {
    old_cap = self.cap
    old_ptr = self.ptr
    new_cap = max(1, old_cap * 2)

    old_layout = layout_for<T>(old_cap)
    new_layout = layout_for<T>(new_cap)

    new_ptr = if old_cap == 0 {
        alloc(new_layout)
    } else {
        realloc(self.ptr, old_layout, new_layout)
    }

    self.ptr = new_ptr
    self.cap = new_cap
}
```

If `realloc` moves the allocation, the old buffer is later released with `dealloc`.

#### 16.5.4. Moving elements

Initialized elements move slot-by-slot:

```
for i in 0 .. self.len {
    src = slot_at<T>(old_ptr, i)
    dst = slot_at<T>(new_ptr, i)
    move_slot_to_slot(src, dst)
}
```

`slot_at` and `move_slot_to_slot` are sealed helpers that perform placement moves without exposing raw pointers to user code.

#### 16.5.5. Initializing new slots

After growth, indices `len .. cap` become `Uninit<T>` slots. Public methods (e.g., `push`, `spare_capacity_mut`) safely initialize them.

### 16.6. Stability & relocation

Because `realloc` may relocate a `RawBuffer`, any references, slices, or views derived from a container become invalid after growth. Users must treat such views as ephemeral. Only the container itself may assume addresses remain stable between growth events.

### 16.7. Stack vs dynamic storage

Drift does not expose stack vs heap distinctions. Local variables and temporaries are compiler-managed; growable containers always use the allocator APIs above. This abstraction lets the backend optimize placement without affecting semantics.

### 16.8. Summary

The memory model rests on:

1. No raw pointers in user code.
2. Typed storage abstractions (`Slot<T>`, `Uninit<T>`).
3. Strict init/destroy rules.
4. All dynamic allocation routed through `lang.abi`.
5. Predictable contiguous container semantics with explicit growth.
6. Backend freedom for placing locals/temporaries.

These rules scale to arrays, strings, maps, trait objects, and future higher-level abstractions using the same mechanisms.

---

## 17. Pointer-free surface and ABI boundaries

Drift deliberately keeps raw pointer *syntax* out of the safe language surface. Low-level memory manipulation and FFI plumbing are funneled through `Ptr<T>` and `std.mem` in unsafe contexts so typical programs interact with typed handles rather than `*mut T` tokens.

### 17.1. Policy: no raw pointer tokens

- No `*mut T` / `*const T` syntax exists in Drift.
- Raw pointers are represented by `Ptr<T>` and are only usable inside `unsafe` contexts.
- User-visible pointer arithmetic and casts are forbidden outside unsafe code.
- Untyped byte operations live behind `std.mem` and `lang.abi` internals.

### 17.2. Slots and uninitialized handles

Chapter 16 defines the canonical typed-storage helpers `Slot<T>` and `Uninit<T>` used by container internals. The pointer-free surface relies on those opaque handles instead of raw addresses; user code never sees pointer syntax or untyped memory.

### 17.3. Guarded builders for container growth

Growable containers expose builder objects instead of raw capacity math. Example:

```drift
var xs = Array<Line>();
xs.reserve(100);

var builder = xs.begin_uninit(3);
builder.emplace(/* args for element 0 */);
builder.emplace(/* args for element 1 */);
builder.emplace(/* args for element 2 */);
builder.finish(); // commits len += 3; rollback if dropped early
```

- `UninitBuilder<T>` only exposes `emplace`, `write`, `len_built`, and `finish`.
- Dropping the builder without `finish()` destroys partially built elements and leaves `len` unchanged.
- No pointer arithmetic leaks outside.

### 17.4. `RawBuffer` internals

Containers rely on `lang.abi::RawBuffer` for contiguous storage, but the public surface offers only safe operations:

```drift
struct RawBuffer<T> { /* opaque */ }

fn capacity(self: &RawBuffer<T>) -> Int
fn slot_at(self: &RawBuffer<T>, i: Int) -> Slot<T> @unsafe
fn reallocate(self: &mut RawBuffer<T>, new_cap: Int) @unsafe
```

`Array<T>` and similar types use these hooks internally; ordinary programs never touch the raw bytes.

### 17.5. Numeric types in FFI

Drift distinguishes **natural-width** and **fixed-width** numeric primitives. The C FFI MVP (§21) supports a specific subset of types at the ABI boundary.

#### 17.5.1. FFI-safe type mapping (MVP)

The following types are permitted in `extern "C"` function signatures:

| Drift type    | C equivalent                  | LLVM IR type | Notes                        |
|---------------|-------------------------------|-------------|------------------------------|
| `Int`         | `ptrdiff_t` / `intptr_t`      | `iN` (word) | Natural-width signed integer |
| `Uint`        | `size_t` / `uintptr_t`        | `iN` (word) | Natural-width unsigned       |
| `Int32`       | `int` / `int32_t`             | `i32`       | Fixed-width 32-bit signed    |
| `Uint32`      | `unsigned int` / `uint32_t`   | `i32`       | Fixed-width 32-bit unsigned  |
| `Uint64`      | `uint64_t`                    | `i64`       | Explicit 64-bit unsigned     |
| `Byte`        | `uint8_t` / `char`            | `i8`        | Single byte                  |
| `Bool`        | `_Bool` / `uint8_t`           | `i1`        | Boolean                      |
| `Float`       | `double`                      | `double`    | IEEE 754 double              |
| `RawPtr<T>`   | `void *`                      | `ptr`       | Opaque pointer               |
| `Void`        | `void`                        | `void`      | Return type only             |

`Void` is valid only as a return type; it is rejected as a parameter type.

#### 17.5.2. Types not FFI-safe (MVP)

The following types are **not** permitted in `extern "C"` signatures. The compiler rejects them with a diagnostic:

- `String` — managed, reference-counted; no stable C layout
- `Array<T>` — managed container with internal header
- `Fn(…) -> R` — Drift callable; no C function-pointer equivalent in the MVP
- `FnResult<T>` — internal error-handling wrapper
- `Optional<T>` — tagged union with Drift-specific layout
- Drift `struct` and `variant` types — no stable C layout guarantee
- Any type not in the FFI-safe table above

#### 17.5.3. Numeric width guidance

- Use `Int` / `Uint` for C APIs that use implementation-defined widths (`size_t`, `ptrdiff_t`, `uintptr_t`, etc.).
- Use `Int32` / `Uint32` for C APIs that use `int` / `unsigned int` (32-bit on all major platforms). This is the correct choice for most POSIX and OpenSSL APIs.
- Use `Uint64` for C APIs that use explicit `uint64_t`.
- `Int32`, `Uint32`, and `Uint64` are available in user code. Other fixed-width primitives (e.g., `Int8`, `Int16`, `Uint16`) are internal to `lang.abi` and are not yet exposed.

---

## 18. Standard I/O design (v1)

`std.io` is part of the v1 surface and provides configured stream/file I/O with
nonblocking + reactor-backed timeout behavior.

### 18.1. Handles and builders

Console handles:

```drift
fn stdin() -> InputStream
fn stdout() -> OutputStream
fn stderr() -> OutputStream

fn stdin_builder() -> InputStreamBuilder
fn stdout_builder() -> OutputStreamBuilder
fn stderr_builder() -> OutputStreamBuilder
```

File entry:

```drift
fn file_builder(path: String) -> FileBuilder
```

Configured values are produced via `build()`:

```drift
InputStreamBuilder.build()  -> ConfiguredInputStream
OutputStreamBuilder.build() -> ConfiguredOutputStream
FileBuilder.build()         -> Result<ConfiguredFile, IoError>
```

### 18.2. Operations

Core operations:

```drift
ConfiguredInputStream.read(buf: &mut Buffer) -> Result<Int, IoError>
ConfiguredInputStream.read_line()            -> Result<String, IoError>
ConfiguredOutputStream.write(buf: &Buffer)   -> Result<Int, IoError>

ConfiguredFile.read(buf: &mut Buffer)        -> Result<Int, IoError>
ConfiguredFile.write(buf: &Buffer)           -> Result<Int, IoError>
ConfiguredFile.close()                       -> Result<Void, IoError>
```

`FileBuilder` configuration methods are fluent:
`read/write/create/truncate/append/mode/timeout`.

### 18.3. Error model

`IoError` is flat:

```drift
variant IoError { Errno(code: Int) }
```

Sentinel codes:

- `IO_ERR_WOULD_BLOCK`
- `IO_ERR_EOF`
- `IO_ERR_LINE_TOO_LONG`

Helpers:

```drift
fn io_error_code(e: IoError) -> Int
fn io_is_would_block(code: Int) -> Bool
fn io_is_eof(code: Int) -> Bool
fn io_is_line_too_long(code: Int) -> Bool
fn is_would_block_error(e: IoError) -> Bool
fn is_eof_error(e: IoError) -> Bool
fn is_line_too_long_error(e: IoError) -> Bool
```

### 18.4. `read_line` semantics

- Returns `Ok(line)` without trailing `\n` (newline is consumed).
- Consecutive newlines produce consecutive empty strings (`Ok("")`).
- EOF before any byte returns `Err(Errno(IO_ERR_EOF))`.
- If bytes exceed `max_line_bytes`, returns `Err(Errno(IO_ERR_LINE_TOO_LONG))`.

### 18.5. Console wrappers

`std.console` remains a thin text API (`print`, `println`, `eprint`,
`eprintln`) built on configured `stdout`/`stderr` streams. It is `nothrow`,
best-effort, and performs no formatting beyond explicit string composition.

### 18.6. Legacy surface

Legacy file-open APIs such as `OpenOptions` + `io.open(...)` are not part of
the current v1 surface; use `file_builder(...)` and configured handles.


## 19. Concurrency & virtual threads

Drift offers structured, scalable concurrency via **virtual threads**: lightweight, stackful execution contexts scheduled on a pool of operating-system carrier threads. Programmers write synchronous-looking code without explicit `async`/`await`, yet the runtime multiplexes potentially millions of virtual threads.

### 19.1. Virtual threads vs carrier threads

| Layer | Meaning | Created by | Cost | Intended users |
|-------|---------|------------|------|----------------|
| Virtual thread | Drift-level lightweight thread | `std.concurrent.spawn` | Very cheap | User code |
| Carrier thread | OS thread executing many virtual threads | Executors | Expensive | Runtime |

Virtual threads borrow a carrier thread while running, but yield it whenever they perform a blocking operation (I/O, timer wait, join, etc.).

### 19.2. `std.concurrent` API surface

Drift’s standard concurrency module exposes straightforward helpers:

```drift
import std.concurrent as conc

val t = conc.spawn(| | => compute_answer()); // lambdas are wrapped as core.callback0(...)

val ans = t.join();
```

Spawn operations return a handle whose `join()` parks the caller until completion. Joining a failed thread -> a `JoinError` encapsulating the thrown `Error`.

#### 19.2.1. Custom executors

Developers may target a specific executor policy:

```drift
val policy = ExecutorPolicy.builder()
    .min_threads(4)
    .max_threads(32)
    .queue_limit(5000)
    .timeout(2.seconds)
    .on_saturation(Policy.RETURN_BUSY)
    .build();

val exec = conc.make_executor(policy);

val t = conc.spawn_on(exec, | | => handle_connection()); // wrapped as core.callback0(...)
```

#### 19.2.2. Structured concurrency

`conc.scope` groups spawned threads so they finish before the scope exits:

```drift
conc.scope(|scope: conc.Scope| => {
    val u = scope.spawn(| | => load_user(42)); // wrapped as core.callback0(...)
    val d = scope.spawn(| | => fetch_data());  // wrapped as core.callback0(...)

    val user = u.join();
    val data = d.join();

    render(user, data);
});
```

If any child fails, the scope cancels the remaining children and propagates the error, ensuring deterministic cleanup.

### 19.3. Executors and policies

Carrier threads are managed by executors configured via a fluent `ExecutorPolicy` builder:

```drift
val policy = ExecutorPolicy.builder()
    .min_threads(2)
    .max_threads(64)
    .queue_limit(10000)
    .timeout(250.millis)
    .on_saturation(Policy.BLOCK)
    .build();

val exec = conc.make_executor(policy);
```

Policy fields:

| Field | Meaning |
|-------|---------|
| `min_threads(N)` | Minimum carrier threads kept alive |
| `max_threads(N)` | Maximum carrier threads allowed |
| `queue_limit(N)` | Cap on runnable virtual threads awaiting carriers |
| `timeout(Duration)` | Upper bound for blocking waits |
| `on_saturation(action)` | Behavior when the queue is full (`BLOCK`, `RETURN_BUSY`, or `THROW`) |

Timeouts apply uniformly to blocking ops backed by the executor.

### 19.4. Blocking semantics

Virtual threads behave as though they block, but the runtime parks them and frees the carrier thread:

- I/O operations register interest with the reactor and park the virtual thread.
- Timers park until their deadline elapses.
- `join()` parks the caller until the child completes.
- When the event loop signals readiness, the reactor unparks the waiting virtual thread onto a carrier.

### 19.5. Reactors

Drift ships with a shared default reactor (epoll/kqueue/IOCP depending on platform). Advanced users may supply custom reactors or inject them into executors for specialized workloads.

### 19.6. Virtual thread lifecycle

- Each virtual thread owns an independent call stack; RAII semantics run normally when the thread exits.
- `join()` -> either the thread’s result or a `JoinError` capturing the propagated `Error`.
- Parking/unparking is transparent to user code.
- `Send`/`Sync` trait bounds govern which values may move across threads or be shared by reference (spec-defined; not compiler-enforced in v1).

### 19.7. Intrinsics: `lang.thread`

At the bottom layer the runtime exposes a minimal intrinsic surface to the standard library:

```drift
module lang.thread;

@intrinsic fn vt_spawn(entry: core.Callback0<Void>, exec: ExecutorHandle) nothrow -> VtHandle;
@intrinsic fn vt_park() -> Void;
@intrinsic fn vt_unpark(thread: VirtualThreadHandle) -> Void;
@intrinsic fn current_executor() -> ExecutorHandle;

@intrinsic fn register_io(fd: Int, interest: IOEvent, thread: VirtualThreadHandle);
@intrinsic fn register_timer(when: Timestamp, thread: VirtualThreadHandle);
```

Library code such as `std.concurrent` is responsible for presenting straightforward APIs; user programs never touch these intrinsics directly.

### 19.8. Scoped virtual threads

Structured scopes ensure children finish (or are cancelled) before scope exit:

```drift
conc.scope(|scope: conc.Scope| => {
val a = scope.spawn(| | => slow_calc()); // wrapped as core.callback0(...)
val b = scope.spawn(| | => slow_calc()); // wrapped as core.callback0(...)
val c = scope.spawn(| | => slow_calc()); // wrapped as core.callback0(...)

    val ra = a.join();
    val rb = b.join();
    val rc = c.join();

    println(ra + rb + rc);
});
```

This pattern mirrors `try/finally`: if any child throws, the scope cancels the rest and rethrows after all joins complete.

### 19.9. Interaction with ownership & memory

- Moves between threads require `Send`; shared borrows require `Sync` (spec-defined; not compiler-enforced in v1).
- Destructors run deterministically when each virtual thread ends, preserving RAII guarantees.
- Containers backed by `RawBuffer` (`Array`, `Map`, etc.) behave identically on all threads.

#### 19.9.1. `Arc` / `Mutex` memory-order contract (`std.concurrent`)

For shared-state primitives in `std.concurrent`, memory orders are pinned as:

- `Arc` refcount increment (`clone`): `Relaxed`.
- `Arc` refcount decrement (`drop`): `Release`.
- `Arc` final-drop path (`prev == 1`): perform an `Acquire` barrier/load before destroying payload.
- `Mutex` lock acquisition (CAS success): `Acquire` (failure path may use `Relaxed`).
- `Mutex` unlock: `Release`.

This is the normative contract for stdlib behavior and performance tuning; stronger orderings are allowed internally only if they preserve these semantics.

### 19.10. Summary

- Virtual threads deliver the ergonomics of synchronous code with the scalability of event-driven runtimes.
- Executors configure carrier thread pools, queues, and timeout policies.
- Blocking APIs park virtual threads instead of OS threads.
- Reactors wake parked threads when I/O or timers fire.
- Structured concurrency scopes offer deterministic cancellation and cleanup.
- Only a handful of `lang.thread` intrinsics underpin the model; user-facing code resides in `std.concurrent`.

## 20. Signed modules and DMIR

Drift distributes code as **digitally signed module packages (DMPs)** built around a canonical, target-independent representation called **DMIR** (Drift Module Intermediate Representation). Signing DMIR rather than backend objects guarantees that every user receives the same typed semantics, regardless of platform or compiler optimizations. This matters because:

- modules often travel through untrusted mirrors, caches, or registries; signatures ensure they weren’t tampered with en route.
- reproducible canonical IR decouples semantic identity from backend artifacts, so verification survives compiler/platform differences.
- dependency manifests can pin digests/signers to prevent supply-chain attacks.
- Threat model: DMP protects against supply-chain and dependency tampering (swapped module artifacts). It does not protect against attackers who can already modify the compiler, linker, or the running process itself.

### 20.1. Position in the pipeline

```
source → AST → HIR → DMIR (canonical) → [sign] → MIR/backend → object/JIT
```

DMIR is the authoritative checkpoint. Later transformations (optimizations, codegen) do not affect the signature.

### 20.2. Canonical DMIR contents

DMIR stores the typed, desugared module with all names resolved:

- Top-level declarations (functions, structs, interfaces, traits, constants).
- Canonical function bodies (control flow normalized, metadata stripped).
- Canonical literal encodings (UTF-8 strings, LEB128 integers, IEEE-754 floats).
- Deterministic ordering by fully-qualified name.
- A canonical **export list**: the subset of top-level symbols that form the module interface. For functions, each export entry records the fully-qualified name, type signature, and that it is an exported Drift entry point using the error-aware calling convention `Result<T, Error>`. This export list describes the interface of a **static module** as seen by other Drift code compiled against the same DMIR; it is not a promise of OS-level binary compatibility.
- No timestamps, file paths, environment data, or formatting trivia.

Each DMIR block carries an independent version number (`dmir_version`).

### 20.3. Module package container (DMIR-PKG v0)

`driftc` emits and consumes a deterministic, hashing-friendly container format for module packages called **DMIR-PKG v0**.
The container is designed to be:

- deterministic (same inputs → identical bytes)
- streaming-friendly for verification
- suitable for signing (signatures are stored as a sidecar; see §20.4)

**Compression.** DMIR-PKG v0 containers are stored uncompressed. Compression, if used, is an outer transport layer (Zstandard recommended) and is not part of the container format nor the signed payload.

#### 20.3.1. Binary layout

All integers are little-endian.

Header (fixed size):

- `magic`: 8 bytes: ASCII `DMIRPKG` followed by a single NUL byte (`b"DMIRPKG\0"`)
- `version`: u16, value `0`
- `flags`: u16, value `0` (reserved)
- `header_size`: u32, fixed header size in bytes (for forward extension)
- `manifest_len`: u64, length of manifest JSON bytes
- `manifest_sha256`: 32 bytes, SHA-256 of the manifest JSON bytes
- `toc_len`: u64, number of TOC entries
- `toc_entry_size`: u32, fixed size `80` in v0
- `toc_sha256`: 32 bytes, SHA-256 of the TOC bytes
- `reserved`: 64 bytes, all zero in v0

Immediately after the header:

1. `manifest_bytes`: UTF-8 JSON (canonical encoding; see §20.3.2)
2. `toc_bytes`: `toc_len * toc_entry_size` bytes
3. `blob_region`: raw concatenation of blobs at offsets recorded in the TOC

TOC entry (fixed 80 bytes):

- `blob_sha256`: 32 bytes
- `offset`: u64 (absolute file offset)
- `length`: u64
- `type`: u16 (payload type tag; toolchain-defined)
- `flags`: u16 (reserved)
- `name_len`: u32 (0..24)
- `name_prefix`: 24 bytes (UTF-8 prefix, zero-padded)

#### 20.3.2. Manifest JSON

The manifest is canonical JSON:

- UTF-8
- stable object key ordering (lexicographic)
- no insignificant whitespace

It records (minimum required set):

- `modules`: a list of module entries. Each entry is an object with:
  - `module_id`: string
  - `interface_blob`: string reference `sha256:<hex>`
  - `payload_blob`: string reference `sha256:<hex>`
- `blobs`: a mapping from blob references (`"sha256:<hex>"`) to an object `{type, length}`.
 - `surfaces`: a map of surface name → object:
   - `entry_modules`: list of module ids that are allowed as cross-package import entry points
   - `import_notice`: optional string emitted once per build when an entry module is imported

The manifest bytes are part of `pkg.dmp`; therefore any signature over `pkg.dmp`
authenticates the manifest contents (including `surfaces`, `entry_modules`, and
`import_notice`).

It may additionally record tooling-level metadata such as:

- `unsigned`: boolean (true when the package has no sidecar signature; local build outputs may be unsigned)
- `unstable_format`: boolean (true for provisional/unstable payload kinds)
- `payload_kind`: string (e.g. `"provisional-dmir"`)
- `payload_version`: integer (e.g. `0`)
- package identity fields (name/version; tooling-level)

#### 20.3.3. Integrity verification (hashes)

When consuming a DMIR-PKG v0 container, the toolchain verifies:

1. header magic/version/entry sizes
2. SHA-256(manifest_bytes) matches `manifest_sha256`
3. SHA-256(toc_bytes) matches `toc_sha256`
4. each blob’s SHA-256 matches its TOC entry
5. blob offsets are in-range and non-overlapping
6. manifest blob references match the TOC (strict: no missing blobs and no unreferenced blobs)

Local build outputs (e.g. `build/drift/localpkgs/*.dmp`) may be unsigned; integrity verification (hashes) is still required.

### 20.4. Signatures and verification

Signature generation is performed by tooling (`drift` / `drift sign`), not by `driftc`.

Signatures are stored as a sidecar file next to the package container:

- package: `pkg.dmp`
- signature sidecar: `pkg.dmp.sig`

The sidecar is JSON and may contain multiple signatures (multiple keys/algorithms/rotations).

Signatures cover the canonical **uncompressed** `pkg.dmp` bytes (the DMIR-PKG container bytes). Transport compression (e.g. `pkg.dmp.zst`) is outside the signed payload.
Because signatures are computed over the raw `pkg.dmp` bytes, all container
contents are signed: header + manifest + TOC + blobs.

**Verification point.** Package signatures (when required by policy) are verified at module import / compilation time by the Drift toolchain. No runtime signature verification is performed by the generated program.

**Policy rule.** When signatures are required by policy, consumers must reject
any package whose manifest is not covered by a valid signature.

### 20.5. Security properties

- Repository compromises cannot forge modules without the private key.
- Canonicalization ensures reproducible builds and stable signatures.
- DMIR versioning decouples language evolution from compiler releases.
- Optional source does not influence verification, so audits cannot poison signatures.

### 20.6. Future extensions

Potential enhancements include transparency logs, certificate-based hierarchies, revocation lists, and dual-signature modes.

Signed DMIR gives Drift a portable, semantically precise unit of distribution while keeping authenticity verifiable on every machine.

*Note:* The exact signing/verification scheme (PGP vs Ed25519, cert hierarchies, revocation policies) is still under design and will be finalized before the DMP format is stabilized. The structure here captures intent; cryptographic options may evolve.

**Relationship to source builds.** A module may be built directly from source or
loaded from a verified DMP/DMIR package. In both cases the module identity is
its declared `module <id>` and it participates in imports via that id (Chapter
7). Packages change the distribution and verification story; they do not change
the language-level meaning of imports.

**Design note — module interface and errors.** Drift deliberately restricts the module interface to a small, explicit set of exported functions that can throw. This keeps cross-module ABIs uniform (every exported function uses `Result<T, Error>`), simplifies plugin design, and prevents accidental exposure of internal helper functions. Internal code is free to optimize error handling aggressively, but anything that crosses a module boundary must treat errors as first-class values using the standard `Error` type and `Result<T, Error>` encoding.

---



## 21. C foreign function interface (FFI)

Drift provides a minimal C FFI surface for calling external C functions from Drift code. This is the **MVP** implementation; it covers declaration-only interop with scalar-typed C functions. Higher-level features (callbacks, struct layout sharing, dynamic plugin loading) are not yet implemented.

### 21.1. Extern declarations

An `extern "C"` declaration introduces a C-linkage function that the compiler emits as a bare LLVM `declare` (no Drift name mangling). The function has no body in Drift; it is resolved by the linker.

**Single declaration form:**

```drift
extern "C" fn abs(x: Int) nothrow -> Int;
```

**Block form** for multiple declarations sharing the same ABI:

```drift
extern "C" {
    fn abs(x: Int) nothrow -> Int;
    fn sqrt(x: Float) nothrow -> Float;
}
```

**Syntax rules:**

- The ABI string must be `"C"`. No other ABI strings are accepted; the compiler rejects unknown ABI identifiers.
- All extern declarations must include `nothrow`. Drift’s exception model (`throws`) is not permitted on extern functions — unwinding must never cross the C boundary.
- Extern declarations have no function body.
- Type parameters are not supported on extern functions.
- The `unsafe` modifier is not valid on the declaration itself (it is required at the *call site*).
- Both forms accept the `pub` modifier for cross-module visibility (see §21.6).

### 21.2. FFI-safe types

Only the type subset listed in §17.5.1 is permitted in extern signatures. The compiler validates each parameter and return type and rejects non-FFI-safe types with a diagnostic. See §17.5 for the full mapping table and restrictions.

### 21.3. Safety contract

Calls to `extern "C"` functions are **unsafe** operations:

```drift
extern "C" fn abs(x: Int) nothrow -> Int;

fn example() nothrow -> Int {
    // Requires unsafe block:
    val result: Int = unsafe { abs(-42) };
    return result;
}
```

**Rules:**

- Every call site must be wrapped in an `unsafe { ... }` block.
- The source file must be compiled with `--allow-unsafe`; without it, `unsafe` blocks are rejected.
- The programmer is responsible for ensuring that arguments satisfy the C function’s preconditions (valid pointers, correct sizes, etc.). The compiler performs no runtime checks beyond type matching.

### 21.4. No unwinding across the C boundary

- Drift’s `Error` type and stack unwinding **must not** cross into or out of C code.
- `extern "C"` functions are `nothrow` by contract. If the C function aborts or invokes undefined behavior, Drift provides no recovery mechanism.
- Errors from C code should be communicated as return values (error codes, sentinel values) and mapped to Drift’s `Error` type in a wrapper function.

**Error wrapping pattern:**

```drift
extern "C" fn c_operation(handle: RawPtr<Byte>, buf: RawPtr<Byte>, len: Int) nothrow -> Int;

fn operation(handle: RawPtr<Byte>, data: RawPtr<Byte>, len: Int) -> Int {
    val code: Int = unsafe { c_operation(handle, data, len) };
    if code < 0 {
        throw Error("c_operation failed", code);
    }
    return code;
}
```

### 21.5. Linker integration

External C functions must be resolved at link time. The compiler provides three flags for specifying link inputs:

| Flag             | Effect                               | Example                              |
|------------------|--------------------------------------|--------------------------------------|
| `--link-lib`     | Link against a system library (`-l`) | `--link-lib m` (links `-lm`)        |
| `--link-search`  | Add a library search path (`-L`)     | `--link-search /opt/lib`            |
| `--link-obj`     | Link an additional object file       | `--link-obj helper.o`               |

All three flags may be specified multiple times. They are passed directly to the linker without modification.

**Example — calling a custom C helper:**

```bash
# Compile the C helper
clang -c helper.c -o helper.o

# Compile Drift source with the object linked in
driftc --link-obj helper.o --allow-unsafe main.drift
```

### 21.6. Cross-module visibility

Extern `"C"` declarations participate in the standard Drift visibility and import system. The `pub` modifier makes an extern declaration visible to importing modules:

```drift
// internal/ffi.drift
module internal.ffi;

pub extern "C" fn SSL_new(ctx: RawPtr<Byte>) nothrow -> RawPtr<Byte>;
pub extern "C" fn SSL_free(ssl: RawPtr<Byte>) nothrow -> Void;

export { SSL_new, SSL_free };
```

```drift
// internal/ssl.drift
module internal.ssl;

import internal.ffi;

fn create_session(ctx: RawPtr<Byte>) nothrow -> RawPtr<Byte> {
    return unsafe { internal.ffi.SSL_new(ctx) };
}
```

**Visibility model:**

- `pub extern "C" fn …` makes the declaration importable by sibling modules.
- `extern "C" fn …` (without `pub`) is module-private — only callable within the declaring module.
- `pub extern "C" { … }` applies `pub` to all declarations in the block.
- `export { … }` lists work with pub extern C names like any other public symbol.
- Codegen always preserves bare C symbol identity — no module-qualified names, no entrypoint boundary wrappers.
- The intended architecture is: centralize raw FFI declarations in an internal module, build Drift-native wrappers in sibling modules, expose only safe Drift API to downstream consumers.

### 21.7. Scope and future directions

This section documents the **shipped MVP** as of 0.27.29-dev. The following are explicitly **not supported** in this revision:

- `extern "C" struct` — no C-layout struct declarations
- `extern "C" Fn(…)` — no C function pointer types
- Variadic functions (`...`)
- Callbacks from C into Drift
- Dynamic library loading (`dlopen`/`dlsym`) as a language feature
- Fixed-width integer types beyond `Int32`, `Uint32`, `Uint64`, and `Byte` in user-facing FFI signatures
- `&T` / `&mut T` parameters in extern signatures (references are not FFI-safe)

**Plugin-style extension** (OS-level shared libraries) is a future goal built on top of this foundation. The language does not define a separate "plugin module" kind or a first-class plugin ABI in this revision. Future revisions may add:

- C-layout struct declarations for sharing data structures across the boundary
- C function pointer types for callbacks
- A higher-level Drift-to-Drift plugin profile if real-world experience justifies it

### 21.7. Static modules vs FFI

- **Static modules** (Chapter 7, Chapter 20) are the core Drift unit of composition. They are compiled into a single image or via DMIR/DMP and may use the full error model and unwinding semantics.
- **C FFI** is for calling into external C code with a restricted type surface and explicit safety boundaries, wrapped by static Drift modules that present a safe interface to the rest of the program.


## 22. Closures and callable traits

Drift treats callables as **traits first**, with an optional dynamic wrapper when you explicitly want type erasure. Capture modes are ownership-based and include borrows (`&`, `&mut`) in this revision (non-escaping closures only).

### 22.0.1 Callable kinds

Drift distinguishes two callable worlds:

- **Non-capturing callables**: function pointers (`Fn(P1, ... , Pn) -> R`) and callback interfaces (dynamic dispatch). These do not capture environment and may be stored/returned freely.
- **Capturing closures**: lambda literals produce compiler-synthesized closure values (code + environment). Closures may capture by `copy`/`move` (escaping) or by borrow (non-escaping).

Parameter choice follows this split:

- Use `Fn(P1, ... , Pn) -> R` when only non-capturing callables are allowed.
- Use a generic type with `require F is FnN<...>` to accept capturing or non-capturing callables.

### 22.1. Surface syntax

- Expression-bodied closures: `|params| => expr` (the expression value is returned).
- Block-bodied closures: `|params| => { ... }` follow normal function rules with explicit `return`.
- Pipe-style lambdas use `=>` by design; Rust-style `|params| expr` (without `=>`) is **not** a Drift form (avoids conflicts with the pipeline operator `|>`).
- Optional explicit captures: `|params| captures (capture_list) => ...` selects explicit capture mode (no implicit captures).
- Passing a borrowed-capture closure across a call boundary is allowed only when the callee is proven non-retaining for that parameter; otherwise it is rejected. Immediate call is always allowed.

#### 22.1.1. Optional lambda return types

- Default: lambda return type is inferred from the body.
- Optional: `|params| -> Ty => ...` pins the return type; the checker enforces the body is assignable to `Ty`.
- Block-bodied lambdas return a value from a trailing expression (`{ expr }`) when allowed, otherwise `return` statements govern the result. If `-> Ty` is present, the body must produce a value.

### 22.2. Capture modes (current revision)

Closures support two capture modes: implicit capture (default) and explicit capture
via `captures(...)`.

#### 22.2.1. Implicit capture mode

If a closure has no `captures(...)` clause, free roots used in the body are
captured implicitly. A free root is a value identifier that resolves to an
outer binding (not a parameter or local).

Capture kind is inferred from usage:

- Read-only use captures a shared borrow (`&x`).
- Writes capture a mutable borrow (`&mut x`).
- Move/consume use captures by value (move into the environment).

The compiler does not switch to by-value captures just because a type is `Copy`;
read-only uses still borrow.

#### 22.2.2. Explicit capture mode (`captures(...)`)

If a closure has a `captures(...)` clause, the capture list is exhaustive: every
free root used in the body must be listed, and no implicit captures are allowed.
Using an explicit capture list disables implicit capture entirely.

Capture items:

- `&x` — borrowed read-only; non-escaping (see §22.2.3).
- `&mut x` — borrowed mutable; non-escaping.
- `copy x` — value-like duplicate; requires `T: Copy`.
- `move x` — ownership transfer; outer binding consumed.
- `share x` — second-owner alias; requires `T: Share`. See §22.2.4.

**Bareword `captures(x)` (no mode keyword) is rejected at parse time as of 0.31.22.** Pre-0.31.22 it silently lowered to `&x`, which produced silent runtime miscompiles for escaping closures (e.g. loop-built closure chains assigned into `core.callback*` — every closure observed the loop-final value of the captured cell). Use one of the explicit forms above; the compiler diagnostic lists the available choices.

Rules:

- Each capture item names a root identifier in the enclosing value scope.
- Root identifiers may appear at most once in the capture list.
- Projections (`x.field`, `x[i]`, `*p`) are not allowed in the capture list.
- The captured name is bound in the closure body and shadows the outer binding.
- If a parameter or local reuses a captured name, it is a hard error.

Captured name types:

- `&x` binds `x: &T`
- `&mut x` binds `x: &mut T`
- `copy x`, `move x`, or `share x` binds `x: T`

There is no implicit auto-deref rewrite of captured names. If `x` is captured as
`&T`, any body operation that requires mutation through `x` is an error; capture
`&mut x` instead.

Capture items are evaluated once at closure creation time, left-to-right. The
items are type-checked and borrow-checked as if they are a sequence of
statements executed at closure creation; earlier moves/borrows affect later
items.

The body may use projections (`x.field`, `x[i]`, `*x`) as long as the root `x` is
captured, subject to existing place model limits.

`copy x` requires the type to implement `Copy`; otherwise it is an error.

#### 22.2.3. Escaping closures and borrowed captures

Borrowed captures (`&x`, `&mut x`) are non-escaping. A closure with any borrowed
captures:

1. **Immediate call is allowed.**
2. **Argument passing is checked against parameter escape level metadata**:
   - `IMMEDIATE` / `LOCAL`: borrowed captures are allowed.
   - `SCOPED`: borrowed captures are allowed only when the captured place is proven
     defined before the scope call in the enclosing block (conservative rule).
   - `THREAD` / `STATIC`: borrowed captures are rejected.
3. **Return/store is rejected** for borrowed-capture closures.
4. **Unannotated call boundaries default to `THREAD` in v1**, so borrowed-capture
   arguments are rejected unless a stricter non-escaping level is proven.
   **Exception:** generic parameters bounded by `Fn1`/`FnN` traits infer `SCOPED`
   escape level, so borrowed-capture closures passed to Fn-bounded params are
   accepted without an explicit escape annotation.

Escape-level metadata is inferred from available bodies for same-unit calls and
may be supplied by compiler metadata for separately compiled units.

Current limitations:
- SCOPED lifetime proof is intentionally conservative and checks only the direct
  enclosing block statement order; predecessor/nested-block safe cases may be
  rejected.
- The Fn-bounded SCOPED inference applies only to `Fn`/`FnMut`/`FnOnce` trait
  bounds; non-Fn trait bounds (e.g., marker traits) do not trigger the relaxation.

Closures with only `copy`/`move`/`share` captures may escape.

#### 22.2.4. `share x` capture (`std.core.shareable.Share`)

`share x` produces a **second owner** of `x`'s underlying resource at
closure-creation time and move-captures that owner into the
environment. The original `x` binding remains usable in the enclosing
scope.

**Type requirement.** `T: Share` (see §5.10.1). The type must
implement `std.core.shareable.Share`. If `T` is `Copy` (and not also
`Share`), the diagnostic suggests `captures(copy x)`. If `T` is
neither, the diagnostic suggests `captures(move x)` or implementing
`Share` for `T`.

**Trait resolution is by trait, not by method name.** The compiler
resolves `share x` through the explicit `std.core.shareable.Share`
trait (semantically equivalent to a fully-qualified
`Share::share(&x)` call). An inherent `.share()` method on a non-
`Share` type **does NOT satisfy** `captures(share x)`. This is
intentional: code-review of `share x` carries the contract of the
`Share` trait, not whatever a user-defined `.share()` method
happens to do.

**The trait does not need to be in scope** at the capture site.
`use trait shareable.Share;` is not required for `captures(share x)`
syntax (the compiler resolves the trait by fully-qualified
identity). It IS required if user code wants to call `Share::share(...)`
or `x.share()` explicitly via trait dispatch.

**Evaluation timing and order.** The `Share::share(&x)` call is
evaluated **inline at the lambda's expression position** in the
enclosing call (not pre-hoisted into the surrounding block). In a
shape like

```drift
foo(side_effect(),
    | | captures(share app) => { ... })
```

the runtime order is:

1. `side_effect()` runs first (arg 0).
2. `Share::share(&app)` runs next, at the lambda's env-construction
   site (arg 1).
3. `foo`'s body runs after both args have evaluated.

Earlier captures in the same `captures(...)` list also evaluate
left-to-right; `share x` follows the same rule as `copy` / `move`.

**Ownership semantics.** `share x` does **NOT** consume `x`. The
synthesized `Share::share(&x)` borrows `&x` (immutable), then
returns a fresh +1 owner of the same underlying resource. The fresh
owner is move-captured into the closure environment. The outer `x`
binding remains LIVE and is dropped at its own scope exit. The
captured copy is dropped with the closure environment.

In refcount terms (e.g., `Arc<T>`), exactly one bump and exactly
two drops happen: bump from `Share::share(&x)`, drop of the env
field at closure destruction, drop of the original `x` at outer
scope exit. No double-drop, no leak.

**Aliasing warning.** `share x` is a warning-bearing operation. The
two owners point at the same underlying resource. Mutations through
either owner are observable through the other. If the closure
escapes to a thread/task/callback that mutates through the captured
owner, **synchronization is the programmer's responsibility** (see
§5.10.1).

**Examples:**

```drift
val app: conc.Arc<App> = conc.arc(App(...));

// Immediate call.
val direct = (| | captures(share app) => {
    return app.get().answer();
})();

// Callback-boxed (captures-share is the preferred spelling for
// callback shared-ownership capture).
val cb: core.Callback0<Int> = core.callback0(
    | | captures(share app) => app.get().answer()
);

// Outer `app` is still LIVE here.
val outer = app.get().answer();
```

### 22.3. Lowering model

- **Non-capturing** closures/functions lower to **thin function pointers** and are `Copy`.
- **Capturing** closures lower to an **environment + code pair** `{ env_ptr, call_ptr }`. The environment holds captured values/borrows under their capture modes; dropping the closure drops the environment exactly once. This ABI is internal-only in the current revision.
- When a closure literal is evaluated, capture items (if any) are evaluated
  left-to-right and stored into the environment. Borrow captures create
  closure-owned loans. `move` captures invalidate the source binding at closure
  creation time.

### 22.4. FnN traits (static dispatch)

FnN traits are compiler-provided and not user-implementable. The compiler
synthesizes closure types for lambdas and provides appropriate callable impls.
Every `Fn(P1, ... , Pn) -> R` is implicitly `FnN<P1, ... , Pn, R>` (and may also
implement `FnMutN`/`FnOnceN`). The reverse coercion is not automatic;
capturing closures do not coerce to `Fn(...)` unless proven non-capturing.

Closures automatically implement one or more FnN traits based on how they use their environment:

```drift
trait Fn0<R> {
    fn call(self: &Self) -> R
}

trait Fn1<A, R> {
    fn call(self: &Self, a: A) -> R
}

trait Fn2<A, B, R> {
    fn call(self: &Self, a: A, b: B) -> R
}
```

The `FnN` family covers arities **0 through 6** (`Fn0`..`Fn6`); a parallel `FnThrow0`..`FnThrow6` family covers throwing callables. **Arity 6 is the v1 cap** — for 7+ params, pack arguments into a struct rather than asking for `Fn7`. This bound is intentional: every additional arity adds a row to the compiler's central callback table, and past 6 params the call site reads as a struct anyway.

`FnMutN` and `FnOnceN` follow the same arity pattern but use `&mut Self` and
`Self` receivers, respectively.

- Pure/non-mutating closures implement `FnN` and `FnOnceN`.
- Mutating closures implement `FnMutN` and `FnOnceN`.
- Closures that move out of their captures implement **only** `FnOnceN`.
- Non-capturing `Fn(...)` functions may implement all three traits.

Generics use these traits for zero-cost, monomorphized dispatch:

```drift
fn apply_twice<F>(f: F, x: Int) -> Int
    require F is Fn1<Int, Int> {
    return f.call(x) + f.call(x);
}

fn accumulate<F>(f: &mut F, xs: Array<Int>) -> Void
    require F is FnMut1<Int, Void> {
    var i = 0;
    while i < xs.len() { f.call(xs[i]); i = i + 1 }
}

fn run_once<F>(f: F) -> Int
    require F is FnOnce0<Int> {
    return f.call();
}
```

### 22.5. CallbackN interfaces (opt-in erasure)

When you need runtime dispatch, use explicit callback interfaces:

```drift
interface Callback0<R> {
    fn call(self: &Callback0<R>) -> R
}

interface Callback1<A, R> {
    fn call(self: &Callback1<A, R>, a: A) -> R
}

fn erase1<F, A, R>(f: F) -> Callback1<A, R>
    require F is Fn1<A, R> {
    // implementation-defined boxing/adaptation
}

**Throwing callbacks** (`CallbackThrowN`) use the same interface/value layout as `CallbackN`.
The only difference is the call signature: the `call` slot follows the throwing ABI
(returns a `FnResult`-shaped value instead of a plain `R`). No representation changes
are required beyond that calling convention.
```

Erasure is explicit; the default callable path remains trait-based static dispatch.

### 22.6. ABI and interop notes

- Closures are ordinary Drift values and can cross Drift module/plugin boundaries like any other value.
- Capturing closures are **not** automatically wrapped for C ABIs. To interoperate with C callbacks, use a thin (non-capturing) function pointer or build an explicit `{ void* ctx, Fn(ctx, …) }` trampoline; see `lang.abi` for guidance.
Borrow captures were rejected in earlier revisions; in this revision they are supported under the non-escaping closure rules above and participate in the borrow checker.


## Appendix A — Ownership Examples

```drift
struct Job { id: Int }

fn process(job: Job) -> Void {
    println("processing job " + job.id.to_string());
}

var j = Job(id = 1);

process(j); // copy
process(move j); // move
process(j); // error: use of moved value
```

---

## Appendix B — Formal grammar (external)

This specification focuses on semantics: ownership, types, errors, concurrency, and runtime behavior. The complete formal grammar (tokens, precedence, productions) lives in `docs/design/drift-lang-grammar.md` and is authoritative for syntax. In case of conflict: semantics in this spec win for meaning; syntax in the grammar file wins for how code is parsed.
