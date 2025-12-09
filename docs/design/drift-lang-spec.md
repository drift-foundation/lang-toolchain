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
- Explicit moves instead of silent copies.

Yet it doesn’t enforce safety by making everything slow or hiding costs behind a garbage collector.

### 1.2. Escape hatches when you ask for them

High-level code stays high-level by default. Low-level control appears only when you deliberately reach for the tooling (`lang.abi`, `lang.internals`, `@unsafe`).

### 1.3. Move semantics everywhere

Passing a value by value moves it—no deep copies unless you opt in. Moves are cheap; cloning is explicit.

### 1.4. Zero-cost abstractions

Drift’s abstractions compile down to what you would hand-write. Ownership, traits, interfaces, and concurrency are “pay for what you use.”

### 1.5. Ready out of the box, no hidden machinery

The language ships meaningful tools (structured errors, virtual threads, collection literals) without magic or implicit globals. Everything is imported explicitly.

## 2. Expressions (surface summary)

Drift expressions largely follow a C-style surface with explicit ownership rules:

- Function calls: `f(x, y)`
- Attribute access: `point.x`
- Indexing: `arr[0]`
- Unary operators: `-x`, `not x`, `!x`
- Binary operators: `+`, `-`, `*`, `/`, comparisons (`<`, `<=`, `>`, `>=`, `==`, `!=`), boolean (`and`, `or`)
- Bitwise operators: `&`, `|`, `^`, `~`, `<<`, `>>` — **require `Uint` operands**; using any other type (including `Int`, `Bool`, `String`, or arrays) is a type error.
- Ternary conditional: `cond ? then_expr : else_expr` (lower precedence than `or`; `cond` must be `Bool`, and both arms must have the same type)
- Pipeline: `lhs >> stage` (left-associative; lower precedence than ternary/`or`; stages are calls/idents)
- Move operator: `x->` moves ownership
- Array literals: `[1, 2, 3]`
- String concatenation uses `+`
- String byte length is exposed via `byte_length(s: String) -> Uint` (UTF‑8 code units, not characters); a future `char_length` may count user-visible characters.
- Empty strings may be written as `""` or `String.EMPTY`. A convenience helper `is_empty(s: String) -> Bool` checks `byte_length(s) == 0`.
- Program entry (v1): exactly one `main` function, returning `Int`, with one of two signatures:
  - `fn main() returns Int`
  - `fn main(argv: Array<String>) returns Int` (argv includes the program name at index 0). The runtime builds `argv` and calls this `main`; no drift_main indirection in user code.

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

  behaves like:

  ```drift
  val __recv = R
  __recv.method(__recv.a(), __recv.b)
  ```

Where leading-dot is valid:

- Inside method-call argument lists (`R.method(...)`), including in nested sub-expressions.
- Inside an index expression `R[expr]` **only when** `expr` is a leading-dot form; `R[.len]` means `R[R.len]`.

Scoping / nesting:

- Leading-dot binds to the immediately enclosing receiver. Nested calls each have their own receiver placeholder. For example:

  ```drift
  outer().a(.x, inner().b(.y))
  ```

  desugars to:

  ```drift
  val __outer = outer()
  val __inner = inner()
  __outer.a(__outer.x, __inner.b(__inner.y))
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
| Immutable binding | `val` | Cannot be rebound or moved |
| Mutable binding | `var` | Can mutate or transfer ownership |
| Const reference | `&T` | Shared, read-only access (C++: `T const&`) |
| Mutable reference | `&mut T` | Exclusive, mutable access (C++: `T&`) |
| Ownership transfer | `x->` | Moves value, invalidating source |
| Interior mutability | `Mutable<T>` | Mutate specific fields inside const objects |
| Volatile access | `Volatile<T>` | Explicit MMIO load/store operations |
| **Blocks & scopes** | `{ ... }` | Define scope boundaries for RAII and deterministic lifetimes |

`val`/`var` bindings may omit the type annotation when the right-hand expression makes the type unambiguous. For example, `val greeting = "hello"` infers `String`, while `val nums = [1, 2, 3]` infers `Array<Int64>`. Add an explicit `: Type` when inference fails or when you want to document the intent.

### 3.1. Primitive palette (updated)

| Type    | Description |
|---------|-------------|
| `Bool`  | Logical true/false. |
| `Int`   | Signed two’s-complement integer of the platform’s natural word size. Guaranteed to be at least 32 bits. |
| `Uint`  | Unsigned integer of the platform’s natural word size. Same bit-width as `Int`. |
| `Size`  | (Reserved for future revisions) Natural-width unsigned; not used for collection lengths/indices in v1. |
| `Float` | IEEE-754 binary floating type used as the default floating-point scalar. Guaranteed to be at least 32 bits. Implementations must document whether `Float` is `F32` or `F64` on a given target. |
| `Int8`, `Int16`, `Int32`, `Int64` | Fixed-width signed integers, exactly 8/16/32/64-bit two’s-complement. |
| `Uint8`, `Uint16`, `Uint32`, `Uint64` | Fixed-width unsigned integers, exactly 8/16/32/64-bit. |
| `F32`, `F64` | IEEE-754 binary32 and binary64 floating-point types. |
| `Byte` | Unsigned 8-bit value (`Uint8` under the hood); used for byte buffers and FFI. |
| `String` | UTF-8 immutable rope. |

`Byte` gives Drift APIs a canonical scalar for binary data. Use `Array<Byte>` (or the dedicated buffer types described in Chapters 6–7) when passing contiguous byte ranges.

#### 3.1.2. String semantics (v1)

- Storage is UTF-8; **bytewise** semantics:
  - `byte_length(s: String) -> Uint` returns the number of UTF-8 code units (bytes), not graphemes. A future `char_length` may count user-visible characters.
  - Equality (`==`) is bytewise; no normalization or case folding.
- Empty strings: `""` or `String.EMPTY`; `is_empty(s: String) -> Bool` checks `byte_length(s) == 0`.
- Concatenation uses `+` and produces a new `String`.
- `Array<String>` is supported; each element is a `%DriftString` header `{%drift.size, i8*}` at the ABI.

#### 3.1.1. Integer and float semantics

Drift distinguishes between **natural-width** numeric primitives and **fixed-width** primitives.

- Natural-width primitives (`Int`, `Uint`, `Size`, `Float`) map to the platform’s efficient scalars:
  - `Int` / `Uint` are at least 32 bits and typically match the native register size.
- `Size` is reserved for future revisions and **not** used for collection lengths/indices in v1. Collections use `Uint` for lengths/capacities and `Int` for indices (see chapter 12).
  - `Float` is either `F32` or `F64`; implementations document the choice per target.
- Fixed-width primitives (`Int8`…`Int64`, `Uint8`…`Uint64`, `F32`, `F64`) have exact widths and are used for binary/wire/FFI with explicit sizes.

Overflow:
- Fixed-width integers use modular two’s-complement wraparound.
- Natural-width integers: debug builds should trap on overflow; release builds may wrap unless the implementation guarantees trapping. Checked helpers (`checked_add`, etc.) may exist in stdlib.

Conversions:
- Widening non-overflowing conversions (e.g., `Int32`→`Int64`, `Uint32`→`Uint64`) may be implicit.
- Narrowing or sign-changing conversions must be explicit and may fail at runtime if out of range.
- `Size` ↔ other ints follow the same rules: `Size`→`Uint` is lossless; `Uint`→`Size` is lossless only if widths allow; `Int`→`Size` is explicit and requires non-negative values.
- Floating conversions follow IEEE-754 rules; narrowing (`F64`→`F32`) must be explicit.

### 3.2. Comments

Drift supports two comment forms:

```drift
// Single-line comment through the newline
val greeting = "hello"

/* Multi-line
   block comment */
fn main() returns Void { ... }
```

Block comments may span multiple lines but do not nest. Comments are ignored by the parser, so indentation/terminator rules treat them as whitespace.

### 3.3. Source location helper (`lang.core`)

Diagnostics frequently need to record where they were emitted. The `lang.core` module exposes a standard helper:

```drift
import lang.core.source_location

fn source_location() returns SourceLocation

struct SourceLocation {
    file: String,
    line: Int64
}
```

`source_location()` is a pure, zero-cost intrinsic that the compiler lowers to the current file/line at the callsite. Typical usage:

```drift
val ^log_site: SourceLocation as "log.site" = source_location()
logger.warn("slow write", site = log_site)

throw InvalidOrder(site = source_location(), order_id = order.id)
```

Because the helper returns a regular struct, you can store it in locals, pass it to `^` captures, or include it in exception arguments. Future logging APIs can accept `SourceLocation` explicitly, keeping site metadata opt-in instead of hard-wired into the runtime.

### 3.4. Struct syntax variants

```drift
struct Point {
    x: Int64,
    y: Int64
}

struct Point(x: Int64, y: Int64)  // header form; identical type
```

The tuple-style header desugars to the block form. Field names remain available for dot access while the constructor supports positional and named invocation. The resulting type follows standard Drift value semantics: fields determine copy- vs move-behavior, and ownership transfer still uses `foo->` as usual.

---

### 3.5. `val` fields as type-level constants

```drift
struct Test {
    val GAME_CTRL_UP:   Int = 1
    val GAME_CTRL_DOWN: Int = 2
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

### 3.5. Tuple types and tuple expressions

Drift supports **tuple types** as simple product types with unnamed fields. They are a single type written with parentheses:

```drift
(T1, T2, ..., Tn)    // n >= 2; (T) is just T
```

- Elements may have different types.
- A tuple is sized if all elements are sized.
- Ownership is per element: moving a tuple moves each element; copying a tuple is allowed only if **all** elements implement `Copy`.
- Tuples participate in traits/requirements componentwise; e.g., `(A, B)` is `Copy` iff both `A` and `B` are.

Tuple **expressions** use the same shape:

```drift
val pair = (left, right)
```

Each element’s ownership flows into the tuple according to the expression used.

Tuples can be **destructured** in bindings:

```drift
val (x, y) = bounds()   // moves the returned tuple; x and y bind its elements
```

Functions may return tuples or accept them as parameters, and tuple types appear in generics (e.g., `Callable<(Int, String), Bool>`). There is no implicit tuple splat/spread; tuple members are accessed via destructuring or pattern matching once supported.

---

### 3.6. Borrow expressions

- `&v` produces a shared reference `&T` from an lvalue `v: T`.
- `&mut v` produces an exclusive mutable reference `&mut T` from a mutable lvalue `v: T`.
- Borrowing from temporaries (rvalues) is a compile-time error; bind to a local first.
- The legacy `ref` / `ref mut` spelling is invalid.

### 3.7. Call-site auto-borrowing (global rule)

For parameters or receivers of type `&T` / `&mut T`, calling with an lvalue `v: T` auto-borrows:

- `g(v)` ≡ `g(&v)` if the parameter is `&T`.
- `h(v)` ≡ `h(&mut v)` if the parameter is `&mut T`.

Borrowing from rvalues (temporaries, moved values) is an error. The explicit forms `&v` / `&mut v` remain legal.

### 3.8. Method receivers and overloading

Receivers inside an `implement` block are written as `self: T` (by value), `self: &T` (shared borrow), or `self: &mut T` (exclusive borrow):

- A call on an lvalue prefers `self: &T`, then `self: &mut T`, then `self: T` (which copies if `Copy`, otherwise requires `obj->`).
- A call on an rvalue (`obj->`, `make()`) can bind only to a `self` receiver; borrowed receivers are not allowed on rvalues.

These rules keep borrowing consistent across free functions, methods, and control-flow desugarings.

---


## 4. Ownership and move semantics (`x->`)

`x->` transfers ownership of `x` without copying. After a move, `x` becomes invalid. Equivalent intent to `std::move(x)` in C++ but lighter and explicit.

### 4.1. Core rules
| Aspect | Description |
|---------|-------------|
| **Move target** | Must be an owned (`var`) value. |
| **Copyable types** | `x` copies; `x->` moves. |
| **Non-copyable types** | Must use `x->`; plain `x` is a compile error. |
| **Immutable (`val`)** | Cannot move from immutable bindings. |
| **Borrowed (`&`, `&mut`)** | Cannot move from non-owning references. |

---

### 4.2. Default: move-only types

Every type is **move-only by default**. If you define a struct and do nothing else, the compiler will refuse to copy it; the only way to pass or assign it by value is to move with `x->`.

```drift
// Move-only by default
struct File {
    fd: Int
}

var f = open("log.txt")

var g = f        // ❌ cannot copy move-only type; use a move
var h = f->      // ✅ move ownership

fn use_file(x: File) returns Void { ... }

use_file(f)      // ❌ copy required
use_file(f->)    // ✅ move into the call
```

This design keeps ownership explicit: you opt *out* of move-only semantics only when cheap copies are well-defined.

### 4.3. Opting into copying

Types that want implicit copies implement the `Copy` trait (see Chapter 5 for the trait definition). The trait is only available when **every field is copyable**. Primitives already implement it; your structs may do the same:

```drift
implement Copy for Int {}
implement Copy for Bool {}

struct Job { id: Int }
implement Copy for Job {}

var a = Job(id = 1)
var b = a      // ✅ copies `a` by calling `copy`

### 4.4. Explicit copy expression

Use the `copy <expr>` expression to force a duplicate of a `Copy` value. It fails at compile time if the operand is not `Copy`. This works anywhere an expression is allowed (call arguments, closure captures, `val`/`var` bindings) and leaves the original binding usable. Default by-value passing still **moves** non-`Copy` values; `copy` is how you make the intent to duplicate explicit.
```

Copying still respects ownership rules: `self: &T` indicates the value is borrowed for the duration of the copy, after which both the original and the newly returned value remain valid.

### 4.5. Explicit deep copies (`clone`-style)

If a move-only type wants to offer a deliberate, potentially expensive duplicate, it can expose an explicit method (e.g., `clone`). Assignment still will not copy—callers must opt in:

```drift
struct Buffer { data: ByteBuffer }   // move-only

implement Buffer {
    fn clone(self: &Buffer) returns Buffer {
        return Buffer(data = self.data.copy())
    }
}

var b1 = Buffer(...)
var b2 = b1.clone()   // ✅ explicit deep copy
var b3 = b1           // ❌ still not allowed; Buffer is not `Copy`
```

This pattern distinguishes cheap, implicit copies (`Copy`) from explicit, potentially heavy duplication.

---

### 4.6. Example — copy vs move

```drift
struct Job { id: Int }

fn process(job: Job) returns Void {
    print("processing job ", job.id)
}

var j = Job(id = 1)

process(j)    // ✅ copy (Job is copyable)
process(j->)  // ✅ move; j now invalid
process(j)    // ❌ error: j was moved
```

---

### 4.7. Example — non-copyable type

```drift
struct File { /* non-copyable handle */ }

fn upload(f: File) returns Void {
    print("sending file")
}

var f = File()
upload(f->)   // ✅ move ownership
upload(f)     // ❌ cannot copy non-copyable type
```

---

### 4.8. Example — borrowing instead of moving

```drift
fn inspect(f: &File) returns Void {
    print("just reading header")
}

var f = File()
inspect(f)     // auto-borrows &f
upload(f->)       // later move ownership away
```

---

### 4.9. Example — mut borrow vs move

```drift
fn fill(f: &mut File) returns Void { /* writes data */ }

var f = File()
fill(f)        // auto-borrows &mut f
upload(f->)       // move after borrow ends
```

Borrow lifetimes are scoped to braces; once the borrow ends, moving is allowed again.

---

### 4.10. Example — move return values

```drift
fn open(name: String) returns File {
    val f = File()
    return f->        // move to caller
}

fn main() returns Void {
    var f = open("log.txt")
}
```

Ownership flows *out* of the function; RAII ensures destruction if not returned.

---

### 4.11. Example — composition of moves

```drift
fn take(a: Array<Job>) returns Void { /* consumes array */ }

var jobs = Array<Job>()
jobs.push(Job(id = 1))
jobs.push(Job(id = 2))

take(jobs->)    // move entire container
take(jobs)      // ❌ jobs invalid after move
```

---

### 4.12. Lifetime and destruction rules
- Locals are destroyed **in reverse declaration order** when a block closes.  
- Moving (`x->`) transfers destruction responsibility to the receiver.  
- Borrowed references are automatically invalidated at scope exit.  
- No garbage collection — **destruction is deterministic** (RAII).

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
- **Adjective-like descriptions** of capabilities (“Clonable”, “Destructible”, “Debuggable”).
- **Static dispatch** — no vtables, zero runtime cost.
- **Injectable implementations** — implementations can be attached from any module.
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
trait Clonable {
    fn clone(self) returns Self
}

trait Debuggable {
    fn fmt(self) returns String
}

trait Destructible {
    fn destroy(self) returns Void
}
```

Rules:

- Traits declare **behavior only** (no fields).
- `Self` refers to the implementing type.
- Traits can depend on other traits (via `require Self is TraitX`).
- Trait names should be **adjectives** describing the capability.

---

### 5.4. Implementing traits

An implementation attaches the capability to a type.

```drift
struct Point { x: Int64, y: Int64 }

implement Debuggable for Point {
    fn fmt(self) returns String {
        return "(" + self.x.to_string() + ", " + self.y.to_string() + ")"
    }
}
```

#### 5.4.1. Generic trait implementations

```drift
struct Box<T> { value: T }

implement Debuggable for Box<T>
    require T is Debuggable
{
    fn fmt(self) returns String {
        return self.value.fmt()
    }
}
```

- The `require` clause limits this implementation to types where `T is Debuggable`.
- If the requirement does not hold, the implementation is ignored for that specialization.

---

### 5.5. Type-level trait requirements (`require`)

A type may declare that it cannot exist unless certain traits are implemented.

```drift
struct File
    require Self is Destructible, Self is Debuggable
{
    fd: Int64
}
```

Meaning:

- The program is **ill-formed** unless implementations exist:

  ```drift
  implement Destructible for File { ... }
  implement Debuggable  for File { ... }
  ```

Compiler errors if missing:

```
E-REQUIRE-SELF: Type File requires trait Destructible but no implementation was found.
E-REQUIRE-SELF: Type File requires trait Debuggable but no implementation was found.
```

#### 5.5.1. Requiring traits of parameters

```drift
struct Box<T>
    require T is Clonable,
            Self is Destructible
{
    value: T
}
```

Constraints:

- `Box<T>` can only be instantiated when `T is Clonable`.
- `Box<T>` is considered incomplete unless a `Destructible` implementation for `Box<T>` exists.

---

### 5.6. Function-level trait requirements

Functions may restrict their usage to specific capabilities:

```drift
fn clone_twice<T>
    require T is Clonable
(value: T) returns (T, T) {
    val a = value.clone()
    val b = value.clone()
    return (a, b)
}
```

More than one requirement may be listed:

```drift
fn print_both<T, U>
    require T is Debuggable,
            U is Debuggable
(t: T, u: U) returns Void {
    out.writeln(t.fmt())
    out.writeln(u.fmt())
}
```

Using a function with unmet trait requirements triggers a compile-time error.

---

### 5.7. Trait guards (`if T is TraitName`)

Trait guards allow functions to adapt behavior based on whether a type implements a trait.

```drift
fn log_value<T>(value: T) returns Void {
    if T is Debuggable {
        out.writeln("[dbg] " + value.fmt())
    } else {
        out.writeln("<value>")
    }
}
```

Semantics:

- `if T is Debuggable` is a **compile-time condition**.
- Only the active branch must type-check for the given `T`.
- Inside the guarded block, methods from the trait become valid (`value.fmt()` here).

### 5.8. Multiple trait conditions

```drift
fn log_value<T>(value: T) returns Void {
    if T is Debuggable and T is Serializable {
        ...
    } else if T is Debuggable {
        ...
    } else if T is Serializable {
        ...
    } else {
        ...
    }
}
```

Trait guards prevent combinatorial explosion of overloaded functions.

---

### 5.9. Trait expressions (boolean logic)

Trait requirements and guards allow boolean trait expressions:

- `T is A and T is B` — must implement both traits
- `T is A or T is B` — must implement at least one
- `not (T is Copyable)` — must *not* implement the trait
- Parentheses allowed for grouping

Example:

```drift
fn clone_if_possible<T>(value: T) returns T {
    if T is Copyable {
        return value            // implicit copy
    } else if T is Clonable {
        return value.clone()
    } else {
        panic("Type cannot be cloned")
    }
}
```

Traits become composable *properties* of types.

---

### 5.10. Trait dependencies (traits requiring traits)

Traits themselves may declare capabilities they depend upon:

```drift
trait Printable
    require Self is Debuggable, Self is Displayable
{
    fn print(self) returns String {
        return self.fmt()
    }
}
```

Any type that implements `Printable` must also implement `Debuggable` and `Displayable`.

---

### 5.11. RAII and the `Destructible` trait

Destruction is expressed as a trait:

```drift
trait Destructible {
    fn destroy(self) returns Void
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
    fn destroy(self) returns Void {
        if !self.handle.is_null() {
            mysql_close(self.handle)
        }
    }
}
```

RAII semantics:

- Automatic cleanup at scope exit calls `destroy(self)` exactly once.
- Manual early destruction is allowed via `value.destroy()`, which consumes `self`.

This integrates seamlessly with move semantics and deterministic lifetimes.

---

### 5.12. Overloading and specialization by trait

Functions may overload based on trait requirements:

```drift
fn save<T>
    require T is Serializable
(value: T) returns ByteBuffer {
    return value.serialize()
}

fn save<T>(value: T) returns ByteBuffer {
    return reflect::dump(value)
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
trait Debuggable {
    fn fmt(self) returns String
}
```

**Legacy note:** `Debuggable` was historically used for diagnostics. For exceptions and captured locals, use the `Diagnostic` trait defined in §5.13.7.

#### 5.13.2. Implementing a trait

```drift
implement Debuggable for File {
    fn fmt(self) returns String { ... }
}
```

#### 5.13.3. Requiring traits in a type (type completeness)

```drift
struct Cache<K, V>
    require K is Hashable,
            Self is Destructible
{
    ...
}
```

#### 5.13.4. Requiring traits in a function

```drift
fn print<T>
    require T is Debuggable
(v: T) returns Void { ... }
```

#### 5.13.5. Trait-guarded logic

```drift
if T is Debuggable { ... }
if not (T is Serializable) { ... }
```

#### 5.13.6. Boolean trait expressions

```drift
require T is (Debuggable or Displayable)
require T is Clonable and not Destructible
```

#### 5.13.7. Diagnostic trait

Exceptions and `^`-captured locals rely on a dedicated diagnostic trait:

```drift
trait Diagnostic {
    fn to_diag(self) returns DiagnosticValue
}
```

Rules:

- Primitive types implement `to_diag` as scalars.
- `Optional<T>` implements `to_diag` as `Null` (None) or `T.to_diag()`.
- Structs without a custom implementation default to an `Object` mapping each field name to `field_value.to_diag()`.
- `to_diag` must never throw.

#### 5.13.8. DiagnosticValue: structured diagnostics

```drift
variant DiagnosticValue {
    Missing
    Null
    Bool(value: Bool)
    Int(value: Int64)
    Float(value: Float64)
    String(value: String)
    Array(items: Array<DiagnosticValue>)
    Object(fields: Map<String, DiagnosticValue>)
}
```

Library helpers (non-throwing):

```drift
fn kind(self: &DiagnosticValue) returns String        // optional helper
fn get(self: &DiagnosticValue, field: String) returns DiagnosticValue
fn index(self: &DiagnosticValue, idx: Int) returns DiagnosticValue
fn as_string(self: &DiagnosticValue) returns Optional<String>
fn as_int(self: &DiagnosticValue) returns Optional<Int64>
fn as_bool(self: &DiagnosticValue) returns Optional<Bool>
fn as_float(self: &DiagnosticValue) returns Optional<Float64>
```

Rules:

- Wrong type / missing field / out-of-bounds → `DiagnosticValue::Missing`.
- `.as_*()` on `Missing` returns `Optional.none`.

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

---


### 5.15. Design Rationale

Traits are designed to:

- Express **capabilities**, not inheritance.
- Enable rich, generic programming without runtime cost.
- Allow types to declare their **necessary capabilities** via `require`.
- Allow algorithms to adapt to available capabilities via **trait guards**.
- Provide a unified abstraction for:
  - RAII (`Destructible`)
  - formatting (`Debuggable`, `Displayable`)
  - serialization, hashing, comparison
  - “marker” traits for POD or special behaviors

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
    fn write(self: &OutputStream, bytes: ByteSlice) returns Void
    fn writeln(self: &OutputStream, text: String) returns Void
    fn flush(self: &OutputStream) returns Void
}
```

### 6.2. Rules

- Interfaces may not define fields — pure behavior only.
- Interfaces are **first‑class types** (unlike traits).
- A function that receives an `OutputStream` may be passed any object that implements that interface.
- The method signatures inside an interface show the receiver type explicitly (`self: &OutputStream`).

### 6.3. Receiver rules (`self`)

Drift differentiates between **methods** (eligible for dot-call syntax) and **free functions**.

- **Only functions defined inside an `implement Type { ... }` block become methods.**
- Inside such a block, the first parameter **must** be spelled `self`, with an explicit mode and type:
  - `self: T` → pass by value
  - `self: &T` → shared borrow
  - `self: &mut T` → exclusive/mutable borrow
  - (future) `self: move T` → consuming receiver
- The receiver’s type is implied by the `implement` header, so you write `self: &File`, not `File self`.
- Outside an `implement` block every function is a free function. A free function may take any parameters (including an explicit `&File`), but it is invoked with ordinary call syntax (`translate(point, 1, 2)`), not `point.translate(...)`.

Example:

```drift
struct Point { x: Int64, y: Int64 }

implement Point {
    fn move_by(self: &mut Point, dx: Int64, dy: Int64) returns Void {
        self.x += dx
        self.y += dy
    }
}

fn translate(p: &mut Point, dx: Int64, dy: Int64) returns Void {
    p.x += dx
    p.y += dy
}

point.move_by(1, 2)     // method call (auto-borrows &mut point)
translate(point, 3, 4)  // free function call (auto-borrows &mut point)
```

This rule set makes the receiver’s ownership mode explicit and prevents implicit, C++-style magic receivers.

---

### 6.4. Implementing interfaces

A concrete type implements an interface through an `implement` block:

```drift
struct File {
    fd: Int64
}

implement OutputStream for File {
    fn write(self: &File, bytes: ByteSlice) returns Void {
        sys_write(self.fd, bytes)
    }

    fn writeln(self: &File, text: String) returns Void {
        self.write((text + "\n").to_bytes())
    }

    fn flush(self: &File) returns Void {
        sys_flush(self.fd)
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
fn write_header(out: OutputStream) returns Void {
    out.writeln("=== header ===")
}
```

#### 6.5.2. Return values

```drift
fn open_log(path: String) returns OutputStream {
    var f = File.open(path)
    return f      // implicit upcast: File → OutputStream
}
```

#### 6.5.3. Locals

```drift
var out: OutputStream = std.console.out
out.writeln("ready")
```

#### 6.5.4. Heterogeneous arrays

```drift
var sinks: Array<OutputStream> = []
sinks.push(open_log("app.log"))
sinks.push(std.console.out)
```

Each element may be a different type implementing the same interface.

---

### 6.6. Dynamic dispatch semantics

A value of interface type is represented as a **fat pointer**, containing:

1. A pointer to the concrete object.
2. A pointer to the interface’s vtable for that concrete type.

When calling:

```drift
out.write(buf)
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
    fn area(self: &Shape) returns Float64
}
```

#### 6.8.2. Implementations

```drift
struct Circle { radius: Float64 }
struct Rect   { w: Float64, h: Float64 }

implement Shape for Circle {
    fn area(self: &Circle) returns Float64 {
        return 3.14159265 * self.radius * self.radius
    }
}

implement Shape for Rect {
    fn area(self: &Rect) returns Float64 {
        return self.w * self.h
    }
}
```

#### 6.8.3. Usage

```drift
fn total_area(shapes: Array<Shape>) returns Float64 {
    var acc: Float64 = 0.0
    var i = 0
    while i < shapes.len() {
        acc = acc + shapes[i].area()
        i = i + 1
    }
    return acc
}
```

Heterogeneous containers work naturally:

```drift
var all: Array<Shape> = []
all.push(Circle(radius = 4.0))
all.push(Rect(w = 3.0, h = 5.0))
```

---

### 6.9. Ownership & RAII

Interface values follow Drift ownership and move semantics.

### 6.10. Moving

```drift
fn consume(out: OutputStream) returns Void {
    out.writeln("consumed")
}
```

Passing `out` moves the *interface wrapper* and transfers ownership of the underlying concrete value.

### 6.11. Destruction

At scope exit:

- If underlying type implements `Destructible`, its `destroy(self)` runs. Owned interface types should **require** `Destructible` so their vtables always carry a drop slot; borrowed interface views omit this and perform no destruction.
- Otherwise, nothing is done.

```drift
{
    var log = open_log("a.log")    // OutputStream
    log.writeln("start")
}   // log.destroy() runs if File is Destructible
```

No double‑destroy is possible because `destroy(self)` consumes the value.

---

### 6.12. Multiple interfaces

A type may implement several interfaces:

```drift
interface Readable  { fn read(self: &Readable) returns ByteBuffer }
interface Writable  { fn write(self: &Writable, b: ByteSlice) returns Void }
interface Duplex    { fn close(self: &Duplex) returns Void }

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
trait Debuggable { fn fmt(self) returns String }

interface DebugSink {
    fn write_debug(self: &DebugSink, msg: String) returns Void
}

fn log_value<T>
    require T is Debuggable
(val: T, sink: DebugSink) returns Void {
    sink.write_debug(val.fmt())
}
```

- `T is Debuggable`: compile‑time capability  
- `sink: DebugSink`: runtime dynamic behavior  

This pattern is central to building logging, serialization, and plugin systems.

---

### 6.14. Error handling across interfaces

Interface method calls participate in normal exception propagation:

```drift
fn dump(src: InputStream, dst: OutputStream) returns Void {
    var buf = ByteBuffer.with_capacity(4096)
    loop {
        buf.clear()
        val n = src.read(buf.as_mut_slice())
        if n == 0 { break }
        dst.write(buf.slice(0, n))
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

## 7. Imports and standard I/O

Drift uses explicit imports — no global or magic identifiers.  
For console I/O details, see Chapter 18. This chapter focuses on import mechanics.

### 7.1. Import syntax (modules and symbols)

```drift
import std.console.out        // bind the exported `out` stream
import std.console.err        // bind the exported `err` stream
import std.io                 // bind the module
import std.console.out as print // optional alias
```

**Name‑resolution semantics**

- `QualifiedName` is resolved left‑to‑right.  
- If it resolves to a **module**, the import binds that module under its last segment (or the `as` alias).  
- If it resolves to an **exported symbol** inside a module (e.g., `std.console.out`), the import binds that symbol directly into the local scope under its own name (or the `as` alias).  
- Ambiguities between module and symbol names must be disambiguated with `as` or avoided.
- Aliases affect only the local binding; frames and module metadata always record the original module ID, not the alias.
- For console streams and other standard I/O primitives, refer to Chapter 18.
- Only **exported** symbols may be resolved by `import`. Attempting to `import M.f` when `f` is not exported by module `M` is a compile-time error.

**Module identifiers**

- Declared with `module <id>` once per file; multiple files may share the same `<id>`, but a single-module build fails if any file is missing or mismatches the ID. A standalone file with no declaration defaults to `main`.
- `<id>` must be lowercase alnum segments separated by dots, with optional underscores inside segments; no leading/trailing/consecutive dots/underscores; length ≤ 254 UTF-8 bytes.
- Reserved prefixes are rejected: `lang.`, `abi.`, `std.`, `core.`, `lib.`.
- Frames/backtraces record the declared module ID (not filenames), so cross-module stacks are unambiguous.

### 7.2. Module interface and exports

A **static module** (one compiled into the host image, either directly from source or via DMP/DMIR) may define many top-level items (functions, structs, traits, interfaces), but only a **selected subset** forms its *module interface*. The module interface consists of symbols that are **exported** and therefore visible to other modules.

Drift treats functions in the module interface as **can-throw entry points**:

- Every exported function is allowed to fail and therefore participates in the standard `Result<T, Error>` model.
- At the ABI level, exported Drift functions are always compiled using the **error-aware calling convention**:
  - `fn f(...) returns T` → ABI returns `Result<T, Error>` encoded as `{T, Error*}`.
  - `fn f(...) returns Void` → ABI returns `Result<Void, Error>` encoded as `Error*`.
- Internal helpers (non-exported functions) may use more aggressive internal optimizations for error handling, but their exact calling convention is not visible across module boundaries.

Import resolution (Section 7.1) only considers **exported** symbols:

- `import my.module.foo` may only bind `foo` if `foo` appears in `my.module`’s export list.
- Non-exported functions and types are private to the defining module and cannot be named from other modules.

The export set is recorded in the module’s DMIR/DMP metadata (Chapter 20). Tools use this metadata to enforce that only exported, can-throw entry points participate in cross-module linking.

## 8. Control flow

Drift uses structured control flow; all loops and conditionals are block-based.

### 8.1. If/else

```drift
if cond {
    do_true()
} else {
    do_false()
}
```

- `if <cond> { ... } else { ... }` selects a branch based on a `Bool` condition.
- The condition must type-check as `Bool`; the two branches need not return the same type unless used as an expression (e.g., in a ternary).
- Each branch has its own scope for locals; names inside a branch shadow outer names.

### 8.2. While loops

```drift
var i: Int64 = 0
while i < 3 {
    i = i + 1
}
```

- `while <cond> { <stmts> }` evaluates `<cond>` each iteration and runs the body while it is `true`.
- `<cond>` must be `Bool`; type errors are reported at compile time.
- The body forms its own scope for local bindings; fresh bindings inside the loop shadow outer names and are re-created per iteration.
- `break` exits the nearest enclosing loop; `continue` jumps to the next iteration (re-evaluating the condition).

### 8.3. Ternary (`? :`) operator

```drift
val label = is_error ? "error" : "ok"
```

- `cond ? then_expr : else_expr` is an expression-form conditional; `cond` must be `Bool`.
- `then_expr` and `else_expr` must have the same type (checked at compile time).
- Useful for concise branching without introducing additional block nesting; when control flow is complex, prefer a full `if/else`.

### 8.4. Try/catch (expression and statement)

**Expression form (`try expr catch …`):**

```drift
val result = try parse_int(input) catch { 0 }
val logged = try parse_int(input) catch err { log(err); 0 }
val parsed = try parse_amount(input) catch BadFormat(e) { 0 }
val routed = try parse(input) catch BadFormat(e) { 0 } catch { 1 }
```

- Evaluates the attempt expression; on success, yields its value.
- On error, evaluates the `catch` arm; the arm’s block must produce a value of the same type as the attempt.
- Catch forms:
  - `catch { block }` — catch-all, no binder.
  - `catch e { block }` — catch-all, binder `e: Error`.
  - `catch EventName(e) { block }` — match specific event, binder `e: Error`.
- Multiple catch arms are allowed; event arms are tested in source order, then catch-all; if no arm matches and there is no catch-all, the error is rethrown.
- Event identity is by event name; the implementation assigns each exception a deterministic integer `event_code` for dispatch, but that encoding is an implementation detail.
- The **attempt must be a function call** (`Name` or `Name.Attr`); non-call attempts are a compile-time error in the current revision.
- This is sugar for a block-wrapped statement `try/catch` that returns the block’s value.

**Statement form (`try/catch`):**

```drift
try {
    risky()
} catch MyError(err) {
    handle(err)
}
```

- Executes the body; on error, transfers control to the first matching catch (event match or catch-all).
- Catch binder (if present) has type `Error`.
- Matching is by exception/event name only; omitting the name makes the clause a catch-all. Domains/attributes are not matched (yet).
- Multiple catches are allowed; event-specific arms are evaluated in source order, then catch-all. If no arm matches and there is no catch-all, the error is rethrown to the caller.
- Control falls through after the try/catch unless all branches return/raise.

## 9. Reserved keywords and operators

Keywords and literals are reserved and cannot be used as identifiers (functions, variables, modules, structs, exceptions, etc.):  
`fn`, `val`, `var`, `returns`, `if`, `else`, `while`, `break`, `continue`, `try`, `catch`, `throw`, `raise`, `return`, `exception`, `import`, `module`, `true`, `false`, `not`, `and`, `or`, plus language/FFI/legacy keywords (`auto`, `pragma`, `bool`, `int`, `float`, `string`, `void`, `abstract`, `assert`, `boolean`, `byte`, `case`, `char`, `class`, `const`, `default`, `do`, `double`, `enum`, `extends`, `final`, `finally`, `for`, `goto`, `implements`, `instanceof`, `interface`, `long`, `native`, `new`, `package`, `private`, `protected`, `public`, `short`, `static`, `strictfp`, `super`, `switch`, `synchronized`, `this`, `throws`, `transient`, `volatile`).

**Operator tokens (reserved):** `+`, `-`, `*`, `/`, `%`, `==`, `!=`, `<`, `<=`, `>`, `>=`, `and`, `or`, `not`, `? :`, `>>` (pipeline), `<<`, `|>`, `<|`, indexing brackets `[]`, and member access `.`. These participate in precedence/associativity rules; identifiers cannot reuse them.

## 10. Variant types (`variant`)

Drift’s `variant` keyword defines **tagged unions**: a value that is exactly one of several named alternatives (variants). Each alternative may carry its own fields, and the compiler enforces exhaustive handling when you `match` on the value.

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

### 10.2. Semantics and representation

A `variant` value stores:

1. A hidden **tag** indicating which alternative is active.
2. The **payload** for that variant’s fields.

Only the active variant’s fields may be accessed. This is enforced statically by pattern matching.

### 10.3. Construction

Each variant behaves like a constructor:

```drift
val success: Result<Int64, String> = Ok(value = 42)
val failure = Err(error = "oops")            // type inference fills in `<Int64, String>`
```

Named arguments are required when a variant has multiple fields; single-field variants may support positional construction, though the explicit form is always accepted.

### 10.4. Pattern matching and exhaustiveness

`match` is used to consume a variant. All variants must be covered (or you must use future catch-all syntax once it exists).

```drift
fn describe(result: Result<Int64, String>) returns String {
    match result {
        Ok(value) => {
            return "ok: " + value.to_string()
        }
        Err(error) => {
            return "error: " + error
        }
    }
}
```

Matches can be nested or composed with other `variant` types:

```drift
variant Optional<T> {
    Some(value: T)
    None
}

variant DbError {
    ConnectionLost
    QueryFailed(message: String)
}

variant LookupResult<T> {
    Found(value: T)
    Missing
    Error(err: DbError)
}

fn describe_lookup(id: Int64, r: LookupResult<String>) returns String {
    match r {
        Found(value) => "Record " + id.to_string() + ": " + value
        Missing      => "No record for id " + id.to_string()
        Error(err)   => match err {
            ConnectionLost       => "Database connection lost"
            QueryFailed(message) => "Query failed: " + message
        }
    }
}
```

### 10.5. Recursive data

Variants are ideal for ASTs and other recursive shapes:

```drift
variant Expr {
    Literal(value: Int64)
    Add(lhs: &Expr, rhs: &Expr)
    Neg(inner: &Expr)
}

fn eval(expr: &Expr) returns Int64 {
    match expr {
        Literal(value) => value
        Add(lhs, rhs) => eval(lhs) + eval(rhs)
        Neg(inner) => -eval(inner)
    }
}
```

### 10.6. Generics

Variants support type parameters exactly like `struct` or `fn` declarations:

```drift
variant PairOrError<T, E> {
    Pair(first: T, second: T)
    Error(error: E)
}

fn make_pair<T>(x: T, y: T) returns PairOrError<T, String> {
    if x == y {
        return Error(error = "values must differ")
    }
    return Pair(first = x, second = y)
}
```

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
val present: Optional<Int64> = Some(value = 42)
val empty: Optional<Int64> = None
```

### 11.3. Control flow

```drift
match qty {
    Some(q) => out.writeln("qty=" + q.to_string()),
    None => out.writeln("no qty"),
}
```

There is no safe-navigation operator (`?.`). Access requires explicit pattern matching or helper combinators built atop `Optional<T>`.

### 11.4. Parameters & returns

- A parameter of type `T` cannot receive `None`.
- Use `Optional<T>` for “maybe” values.
- Returning `None` from a function declared `: T` is a compile error.

```drift
fn find_sku(id: Int64) returns Optional<String> { /* ... */ }

val sku = find_sku(42)
match sku {
    Some(s) => out.writeln("sku=" + s),
    None => out.writeln("missing"),
}
```

### 11.5. Ownership

Pattern matching moves the bound value by default. If you need to borrow instead, destructure a reference to the `Optional` and match on that (planned once borrow-patterns are added).

### 11.6. Diagnostics (illustrative)

- **E2400**: cannot assign `None` to non-optional type `T`.
- **E2401**: attempted member/method use on `Optional<T>` without pattern matching / combinators.
- **E2402**: attempted unwrap of `None` (discouraged pattern).
- **E2403**: attempted implicit conversion `Optional<T>` → `T`.

### 11.7. End-to-end example

```drift
import sys.console.out

struct Order {
    id: Int64,
    sku: String,
    quantity: Int64
}

fn find_order(id: Int64) returns Optional<Order> {
    if id == 42 { return Some(value = Order(id = 42, sku = "DRIFT-1", quantity = 1)) }
    return None
}

fn ship(o: Order) returns Void {
    out.writeln("shipping " + o.sku + " id=" + o.id)
}

fn main() returns Void {
    val maybe_order = find_order(42)

    match maybe_order {
        Some(o) => ship(o),
        None => out.writeln("order not found"),
    }
}

### 11.8. Optional API (minimal)

The standard library exposes a minimal API on `Optional<T>`:

```drift
struct Optional<T> {
    fn is_some(self) returns Bool
    fn is_none(self) returns Bool
    fn unwrap_or(self, default: T) returns T
}
```

Semantics:
- `is_some` tests the tag.
- `is_none` is `!is_some`.
- `unwrap_or` returns the inner value if present, otherwise `default`.

This API is sufficient to inspect `Optional<T>` without pattern matching; richer combinators can be added later.
```

### 11.8. Optional API (minimal)

The standard library exposes a minimal API on `Optional<T>`:

```drift
struct Optional<T> {
    fn is_some(self) returns Bool
    fn is_none(self) returns Bool
    fn unwrap_or(self, default: T) returns T
}
```

Semantics:
- `is_some` tests the tag.
- `is_none` is `!is_some`.
- `unwrap_or` returns the inner value if present, otherwise `default`.

This API is sufficient to inspect `Optional<T>` without pattern matching; richer combinators can be added later.

---
## 12. `lang.array`, `ByteBuffer`, and array literals

`lang.array` is the standard module for homogeneous sequences. It exposes the generic type `Array<T>` plus builder helpers and the binary-centric `ByteBuffer`. `Array` is always in scope for type annotations, so you can write:

```drift
import sys.console.out

fn main() returns Void {
    val names: Array<String> = ["Bob", "Alice", "Ada"]
    out.writeln("names ready")
}
```

Array literals follow the same ownership and typing rules as other expressions:

```drift
val nums = [1, 2, 3]            // infers Array<Int64>
val names = ["Bob", "Alice"]     // infers Array<String>

val explicit: Array<Int64> = [1, 2, 3]  // annotation still allowed when desired
```

- `[expr1, expr2, ...]` constructs an `Array<T>` where every element shares the same type `T`. The compiler infers `T` from the elements.
- Mixed-type literals (`[1, "two"]`) are rejected at parse-time.
- Empty literals are reserved for a future constructor; for now, call the stdlib helper once it lands.

`Array<T>` integrates with the broader language design — it moves with `->`, can be captured with `^`, and will participate in trait implementations like `Display` once the stdlib grows. The literal syntax keeps sample programs succinct while we flesh out higher-level APIs.

**Indexing and lengths.** In v1, container lengths/capacities use `Uint`; indices are `Int`:

- `Array<T>.len: Uint`
- `Array<T>.capacity: Uint`
- `ByteBuffer.len: Uint`
- `ByteSlice.len: Uint`

Any function that indexes into a container or string must accept an `Int` index; bounds checks reject negative indices and indices ≥ `len`. Lengths/capacities are unsigned counts (`Uint`). Examples elsewhere that show `Size`/`Int64` are illustrative; the canonical types for arrays/strings in v1 are `Uint` for lengths/capacities and `Int` for indices.

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
var buf = ByteBuffer.with_capacity(4096)
val literal = ByteBuffer.from_array([0x48, 0x69])
val from_utf8 = ByteBuffer.from_string("drift")
```

Core operations:

- `fn len(self: &ByteBuffer) returns Uint` — number of initialized bytes.
- `fn capacity(self: &ByteBuffer) returns Uint` — reserved storage.
- `fn clear(self: &mut ByteBuffer) returns Void` — resets `len` to zero without freeing.
- `fn push(self: &mut ByteBuffer, b: Byte) returns Void`
- `fn extend(self: &mut ByteBuffer, slice: ByteSlice) returns Void`
- `fn as_slice(self: &ByteBuffer) returns ByteSlice`
- `fn slice(self: &ByteBuffer, start: Uint, len: Uint) returns ByteSlice`
- `fn as_mut_slice(self: &mut ByteBuffer) returns MutByteSlice`
- `fn reserve(self: &mut ByteBuffer, additional: Uint) returns Void`

`ByteSlice`/`MutByteSlice` are lightweight descriptors (`{ ptr, len }`). They do not own memory; borrow rules ensure the referenced storage stays alive for the duration of the borrow. `MutByteSlice` provides exclusive access, so you cannot obtain a second mutable slice while one is active.

Typical I/O pattern:

```drift
fn copy_stream(src: InputStream, dst: OutputStream) returns Void {
    var scratch = ByteBuffer.with_capacity(4096)

    loop {
        scratch.clear()
        val filled = src.read(scratch.as_mut_slice())
        if filled == 0 { break }

        val chunk = scratch.slice(0, filled)
        dst.write(chunk)
    }
}
```

`read` writes into the provided mutable slice and returns the number of bytes initialized; `slice` then produces a read-only view of that prefix without copying. FFI helpers in `lang.abi` can also manufacture `ByteSlice`/`MutByteSlice` wrappers around raw pointers for zero-copy interop.


### 12.2. Indexing, mutation, and borrowing
#### 12.2.1. Borrowed element references

To avoid copying and allow other APIs to operate on a specific slot, `Array<T>` exposes helper methods:

```drift
fn ref_at(self: &Array<T>, index: Int) returns &T
fn ref_mut_at(self: &mut Array<T>, index: Int) returns &mut T
```

- `ref_at` borrows the array immutably and returns an immutable `&T` to element `index`. Multiple `ref_at` calls may coexist, and the array remains usable for other reads while the borrow lives.
- `ref_mut_at` requires an exclusive `&mut Array<T>` borrow and yields an exclusive `&mut T`. While the returned reference lives, no other borrows of the same element (or the array) are allowed; this enforces the usual aliasing rules.

Bounds checks mirror simple indexing: out-of-range indices raise `IndexError(container = "Array", index = i)`. These APIs make it easy to hand a callee a view of part of the array—e.g., pass `ref_mut_at` into a mutator function that expects `&mut T`—without copying the element or exposing the entire container.


Use square brackets to read an element:

```drift
val nums = [1, 2, 3]
val first = nums[0]
```

Assignments through an index require the binding to be mutable:

```drift
var mutable_values: Array<Int64> = [5, 10, 15]
mutable_values[1] = 42      // ok

val frozen = [7, 8, 9]
frozen[0] = 1               // compile error: cannot assign through immutable binding
```

Nested indexing works as expected (e.g., `matrix[row][col]`) as long as the root binding is declared with `var`.


## 13. Collection literals (arrays and maps)

Drift includes literal syntax for homogeneous arrays (`[a, b, ...]`) and maps (`{ key: value, ... }`).
The syntax is part of the language grammar, but **literals never hard-wire a concrete container type**.
Instead, they are desugared through capability interfaces so projects can pick any backing collection.

### 13.1. Goals

1. **Ergonomics** — trivial programs should be able to write `val xs = [1, 2, 3]` without ceremony.
2. **Flexibility** — large systems must be free to route literals into custom containers, including
   arena-backed vectors, small-capacity stacks, or persistent maps.

### 13.2. Syntax

#### 13.2.1. Array literal

`[expr1, expr2, ...]` constructs a homogeneous array literal. Example: `val xs = [1, 2, 3]`.

#### 13.2.2. Map literal

`{ key: value, ... }` constructs a map literal. Example: `val user = { "name": "Ada", "age": 38 }`.

Duplicate keys are allowed in the literal; the target type decides whether to keep the first value, last
value, or reject duplicates.

### 13.3. Type resolution

A literal `[exprs...]` or `{k: v, ...}` requires a *target* type `C`. Resolution happens in two phases:

1. Infer the element type(s) from the literal body. Array literals require all expressions to unify to a
   single element type `T`. Map literals infer key type `K` and value type `V` from their entries.
2. Determine the target container type `C` from context. If no context constrains the literal, the
   compiler falls back to the standard prelude types (`Array<T>` and `Map<K, V>`).

#### 13.3.1. `FromArrayLiteral`

A type `C` may accept array literals by implementing:

```drift
interface FromArrayLiteral<Element> {
    static fn from_array_literal(items: Array<Element>) returns Self
}
```

Desugaring of `[e1, e2, ...]` becomes:

```drift
C.from_array_literal(tmp_array)
```

Where `tmp_array` is an ephemeral `Array<T>` built by the compiler. If `C` does not implement the
interface, the literal fails to type-check.

#### 13.3.2. `FromMapLiteral`

Map literals use a similar interface:

```drift
interface FromMapLiteral<Key, Value> {
    static fn from_map_literal(entries: Array<(Key, Value)>) returns Self
}
```

The compiler converts `{k1: v1, ...}` into `C.from_map_literal(tmp_entries)` where `tmp_entries` is an
`Array<(K, V)>`.

### 13.4. Standard implementations

The prelude wires literals to the default collections:

```drift
implement<T> FromArrayLiteral<T> for Array<T> {
    static fn from_array_literal(items: Array<T>) returns Array<T> {
        return items
    }
}

implement<K, V> FromMapLiteral<K, V> for Map<K, V> {
    static fn from_map_literal(entries: Array<(K, V)>) returns Map<K, V> {
        val m = Map<K, V>()
        for (k, v) in entries {
            m.insert(k, v)
        }
        return m
    }
}
```

This keeps “hello world” code terse:

```drift
fn main() returns Void {
    val xs = [1, 2, 3]
    val cfg = { "mode": "debug" }
}
```

### 13.5. Strict mode and overrides

Projects may opt into a strict mode that disables implicit prelude imports. In that configuration:

- Literal syntax still parses.
- You must import the concrete collection types you want to accept literals.
- If no suitable `FromArrayLiteral`/`FromMapLiteral` implementation is in scope, the literal fails.

Custom containers can opt in by providing their own implementations:

```drift
struct SmallVec<T> { /* ... */ }

implement<T> FromArrayLiteral<T> for SmallVec<T> {
    static fn from_array_literal(items: Array<T>) returns SmallVec<T> {
        var sv = SmallVec<T>()
        for v in items { sv.push(v) }
        return sv
    }
}

val fast: SmallVec<Int> = [1, 2, 3]
```

The same pattern applies to alternative map implementations.

### 13.6. Diagnostics

- `[1, "two"]` → error: element types do not unify.
- `{}` without a target type → error when no default map is in scope.
- `val s: SortedSet<Int> = [1, 2, 3]` → error unless `SortedSet<Int>` implements `FromArrayLiteral<Int>`.

### 13.7. Summary

- Literal syntax is fixed in the language, but its meaning is delegated to interfaces.
- The prelude provides ready-to-use defaults (`Array`, `Map`).
- Strict mode and custom containers can override the target type.
- Errors are clear when element types disagree or no implementation is available.


## 14. Exceptions and error context

Drift provides structured exception handling through a single `Error` type, **exception events**, and the `^` capture modifier.  
Exception declarations create constructor names in the value namespace. `throw ExcName { field: expr, ... }` is valid syntax: fields must match the declared names/types, produce an `Error` value with the exception’s deterministic `event_code`, and integrate with the existing `try/catch` event dispatch. Every exception attribute is recorded in `Error.attrs` as a typed `DiagnosticValue`, and any `^`-captured locals are recorded in `ctx_frames` the same way; both are diagnostics, not user-facing payloads.
Exceptions are **not** UI messages: they carry machine-friendly context (event name, arguments, captured locals, stack) that can be logged, inspected, or transmitted without embedding human prose.
`Error` itself is a catch-all handler type: user functions do not return `Error` or throw `Error` directly; they throw concrete exception events, and catch blocks may bind either a specific exception type or `Error` as a generic binder.

### 14.1. Goals
Drift’s exception system is designed to:

- Use **one concrete error type** (`Error`) for all thrown failures.
- Represent failures as **event names plus arguments**, not free-form text.
- Capture **call-site context** (locals per frame + backtrace) automatically.
- Preserve a **precise, frozen ABI layout** so exceptions can propagate across Drift modules and plugins.
- Fit cleanly over a conceptual `Result<T, Error>` model for internal lowering and ABI design.
- Respect **move semantics**: `Error` is move-only and is always transferred with `e->`.

---

### 14.2. Error type and layout

```drift
struct Error {
    event: String,
    attrs: Map<String, DiagnosticValue>,
    ctx_frames: Array<CtxFrame>,
    stack: BacktraceHandle
}

exception IndexError {
    container: String,
    index: Int64,
}
```

#### 14.2.1. event
Event name of the exception (`"BadArgument"`).

#### 14.2.2. attrs
All exception attributes as typed `DiagnosticValue` entries (see §5.13.8). Values are produced via `Diagnostic.to_diag()`; no stringification is implied.

#### 14.2.3. ctx_frames
Per-frame captured locals:

```drift
struct CtxFrame {
    fn_name: String,
    locals: Map<String, DiagnosticValue>
}
```

Event attrs never appear here.

#### 14.2.4. stack
Opaque captured backtrace.

---

### 14.3. Exception events

#### 14.3.1. Declaring events
```drift
exception InvalidOrder {
    order_id: Int64,
    code: String,
}
exception Timeout {
    operation: String,
    millis: Int64,
}
```

Each field type must implement `Diagnostic` (see §5.13.7).

#### 14.3.2. Throwing
```drift
throw InvalidOrder { order_id: order.id, code: "order.invalid" }
```

Runtime builds an `Error` with:
- event name
- attrs (each declared field converted via `Diagnostic.to_diag()` into `Map<String, DiagnosticValue>`)
- empty ctx_frames (filled during unwind)
- backtrace

#### 14.3.3. Diagnostic requirement
Each exception field type must implement `Diagnostic` (see §5.13.7) so the runtime can capture a typed `DiagnosticValue`.

---

### 14.4. Capturing local context with ^

Locals can be captured:

```drift
val ^input: String as "record.field" = s
```

A frame is added when unwinding past the function:

```json
{
  "fn_name": "parse_date",
  "locals": { "record.field": "2025-13-40" }
}
```

Rules:
- Only `^`-annotated locals captured.
- Values must implement `Diagnostic` (see §5.13.7).
- Capture happens once per frame.

---

### 14.5. Throwing, catching, rethrowing

`Error` is move-only.

#### 14.5.1. Catch by event
```drift
try {
    ship(order)
} catch InvalidOrder(e) {
    log(&e)
}
```

Matches by `error.event`.

#### 14.5.2. Catch-all + rethrow
```drift
catch e {
    log(&e)
    throw e->
}
```

Ownership moves back to unwinder.

#### 14.5.3. Inline catch-all shorthand

For a single call where you just want a fallback value, use the one-liner form:

```drift
val date = try parse_date(input) catch { default_date }
```

This is sugar for a catch-all handler:

```drift
val date = {
    try { parse_date(input) }
    catch _ { default_date }
}
```

The `else` expression must produce the same type as the `try` expression. Exception context (`event`, attrs, captured locals, stack) is still recorded before control flows into the `else` arm.

---

#### 14.5.4. Accessing attributes

Attributes are accessed via `Error.attrs`:

```drift
val code = e.attrs["sql_code"].as_int()
val cust = e.attrs["order"]["customer"]["id"].as_string()
```

Lookups and `.as_*()` are non-throwing; missing or wrong-typed fields yield `DiagnosticValue::Missing` and `Optional.none`.

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
- Internal, non-exported functions may be lowered more aggressively (for example, eliding the error channel when analysis proves “no throw”), but such optimizations must not change the behavior of exported entry points as seen through the module interface.

---

### 14.7. Drift–Drift propagation across static modules

Unwinding is allowed across **static Drift modules** as long as:
- The `Error` layout used by those modules is identical.
- They share the same runtime and unwinder.

This applies to modules that are compiled together into a single image (either directly from source or via DMIR/DMP). **Unwinding must not cross FFI or OS-level shared library boundaries**; any exported Drift APIs used via C/FFI must convert failures into value errors at the boundary (see Chapter 17).

Event name + attrs + ctx_frames + stack fully capture portable state.

---

### 14.8. Logging and serialization
Serialization/logging is implementation-defined. A possible JSON shape:

```json
{
  "event": "InvalidOrder",
  "attrs": { "order_id": 42, "code": "order.invalid" },
  "ctx_frames": [
    { "fn_name": "ship", "locals": { "record.id": "42" }},
    { "fn_name": "ingest_order", "locals": { "batch": "B1" }}
  ],
  "stack": "opaque"
}
```

---

### 14.9. Summary

- Single `Error` type.
- Event-based exceptions.
- Attributes + captured locals stored as typed `DiagnosticValue`.
- Move-only errors with deterministic ownership.
- Precisely defined layout for cross-module-safe unwinding.
- Semantically equivalent to `Result<T, Error>` internally.

## 15. Mutators, transformers, and finalizers

In Drift, a function’s **parameter ownership mode** communicates its **lifecycle role** in a data flow.  
This distinction becomes especially clear in pipelines (`>>`), where each stage expresses how it interacts with its input.

### 15.1. Function roles

| Role | Parameter type | Return type | Ownership semantics | Typical usage |
|------|----------------|--------------|---------------------|----------------|
| **Mutator** | `&mut T` | `Void` or `T` | Borrows an existing `T` mutably and optionally returns it. Ownership stays with the caller. | In-place modification, e.g. `fill`, `tune`. |
| **Transformer** | `T` | `U` (often `T`) | Consumes its input and returns a new owned value. Ownership transfers into the call and out again. | `compress`, `clone`, `serialize`. |
| **Finalizer / Sink** | `T` | `Void` | Consumes the value completely. Ownership ends here; the resource is destroyed or released at function return. | `finalize`, `close`, `free`, `commit`. |

### 15.2. Pipeline behavior

The pipeline operator `>>` is **ownership-aware**.  
It is left-associative and automatically determines how each stage interacts based on the callee’s parameter type:

```drift
fn fill(f: &mut File) returns Void { /* mutate */ }
fn tune(f: &mut File) returns Void { /* mutate */ }
fn finalize(f: File) returns Void { /* consume */ }

open("x")
  >> fill      // borrows mutably; File stays alive
  >> tune      // borrows mutably again
  >> finalize; // consumes; File is now invalid
```

- **Mutator stages** borrow temporarily and return the same owner.
- **Transformer stages** consume and return new ownership.
- **Finalizer stages** consume and end the pipeline.
- **Desugaring intuition:** `x >> f` behaves like `f(x)`, and `x >> g(a, b)` behaves like `g(x, a, b)`. Pipelines are left-associative, so `a >> f >> g` becomes `g(f(a))`. Ownership follows the parameter types of each stage (borrow vs move).

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
      >> fill
      >> tune;      // RAII closes automatically here
}

{
    open("x")
      >> fill
      >> tune
      >> finalize;  // explicit end-of-life
}
```

In both cases, the file handle is safely released exactly once.

### 15.5. Destructors and moves

- Deterministic RAII: owned values run their destructor at end of liveness—scope exit, early return, or after being consumed by a finalizer. No deferred GC-style cleanup.
- Move-only by default: moving a value consumes it; the source binding becomes invalid and is not dropped there. Drop runs exactly once on the final owner.
- Copyable types opt in: only `Copy` types may be implicitly copied; they either have trivial/no destructor or a well-defined copy+drop story.

## 16. Memory model

This chapter defines Drift's rules for value storage, initialization, destruction, and dynamic allocation. The goal is predictable semantics for user code while relegating low-level memory manipulation to the standard library and `lang.abi`.

Drift deliberately hides raw pointers, pointer arithmetic, and untyped memory. Those operations exist only inside sealed, `@unsafe` library internals. User-visible code works with typed values, references, and safe containers like `Array<T>`.

### 16.1. Value storage

Every sized type `T` occupies `size_of<T>()` bytes. Sized types include primitives, structs whose fields are all sized, and generic instantiations where each argument is sized. These values may live in locals, struct fields, containers, or temporaries. The compiler chooses the actual storage (registers vs stack) and that choice is unobservable.

#### 16.1.1. Initialization & destruction

- A value must be initialized exactly once before use.
- A value must be destroyed exactly once when it leaves scope or is overwritten.
- Types with destructors run them during destruction; other types are dropped with no action.

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
module lang.abi

struct RawBuffer { /* opaque */ }
struct Layout { size: Int, align: Int }

@intrinsic fn size_of<T>() returns Int
@intrinsic fn align_of<T>() returns Int

@unsafe fn alloc(layout: Layout) returns RawBuffer
@unsafe fn realloc(buf: RawBuffer, old: Layout, new: Layout) returns RawBuffer
@unsafe fn dealloc(buf: RawBuffer, layout: Layout) returns Void
```

- `alloc` returns uninitialized storage for a layout.
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
    len: Int      // initialized elements
    cap: Int      // reserved slots
    buf: RawBuffer
}
```

Invariant: indices `0 .. len` are initialized; `len .. cap` are uninitialized slots ready for construction. Growth occurs before inserting when `len == cap`.

#### 16.5.3. Growth algorithm

```
fn grow<T>(&mut self: Array<T>) @unsafe {
    old_cap = self.cap
    new_cap = max(1, old_cap * 2)

    old_layout = layout_for<T>(old_cap)
    new_layout = layout_for<T>(new_cap)

    new_buf = if old_cap == 0 {
        alloc(new_layout)
    } else {
        realloc(self.buf, old_layout, new_layout)
    }

    self.buf = new_buf
    self.cap = new_cap
}
```

If `realloc` moves the allocation, the old buffer is later released with `dealloc`.

#### 16.5.4. Moving elements

Initialized elements move slot-by-slot:

```
for i in 0 .. self.len {
    src = slot_at<T>(old_buf, i)
    dst = slot_at<T>(new_buf, i)
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

Drift deliberately keeps raw pointer syntax out of the language surface. Low-level memory manipulation and FFI plumbing are funneled through sealed standard-library modules so typical programs interact with typed handles rather than `*mut T` tokens.

### 17.1. Policy: no raw pointer tokens

- No `*mut T` / `*const T` syntax exists in Drift.
- User-visible pointer arithmetic and casts are forbidden.
- Untyped byte operations live behind `@unsafe` internals such as `lang.abi` and `lang.internals`.

### 17.2. Slots and uninitialized handles

Chapter 16 defines the canonical typed-storage helpers `Slot<T>` and `Uninit<T>` used by container internals. The pointer-free surface relies on those opaque handles instead of raw addresses; user code never sees pointer syntax or untyped memory.

### 17.3. Guarded builders for container growth

Growable containers expose builder objects instead of raw capacity math. Example:

```drift
var xs = Array<Line>()
xs.reserve(100)

var builder = xs.begin_uninit(3)
builder.emplace(/* args for element 0 */)
builder.emplace(/* args for element 1 */)
builder.emplace(/* args for element 2 */)
builder.finish()                 // commits len += 3; rollback if dropped early
```

- `UninitBuilder<T>` only exposes `emplace`, `write`, `len_built`, and `finish`.
- Dropping the builder without `finish()` destroys partially built elements and leaves `len` unchanged.
- No pointer arithmetic leaks outside.

### 17.4. `RawBuffer` internals

Containers rely on `lang.abi::RawBuffer` for contiguous storage, but the public surface offers only safe operations:

```drift
struct RawBuffer<T> { /* opaque */ }

fn capacity(self: &RawBuffer<T>) returns Size
fn slot_at(self: &RawBuffer<T>, i: Size) returns Slot<T> @unsafe
fn reallocate(self: &mut RawBuffer<T>, new_cap: Size) @unsafe
```

`Array<T>` and similar types use these hooks internally; ordinary programs never touch the raw bytes.

### 17.5. Numeric types in FFI

Drift distinguishes **natural-width** and **fixed-width** numeric primitives. FFI bindings must respect how C expresses numeric widths:

1. **C uses implementation-defined integer types** (e.g., `int`, `unsigned`, `size_t`, `ptrdiff_t`, `uintptr_t`):
   - Drift bindings may use the corresponding natural-width primitives:
     - `size_t` → `Size`
     - `ptrdiff_t` → `Int`
     - `uintptr_t` → `Uint`
     - `int` → `Int` (or `Int32` if the ABI explicitly freezes C `int` to 32-bit)
   - This pattern is appropriate when the C API itself is intentionally abstract over width.
2. **C uses explicit fixed widths (`<stdint.h>` / `<inttypes.h>`)** (e.g., `int16_t`, `uint32_t`, `uint8_t`):
   - Drift bindings **must** use the matching fixed-width primitives:
     - `int8_t` → `Int8`, …, `int64_t` → `Int64`
     - `uint8_t` → `Uint8`, …, `uint64_t` → `Uint64`
   - Natural-width primitives (`Int`, `Uint`, `Size`, `Float`) **must not** appear in such signatures.
3. **`Size` ABI representation:** `Size` lowers to an unsigned integer type that is at least as wide as a pointer and at least 16 bits. C shims typically map `Size` to `uintptr_t`:

```c
typedef uintptr_t DriftSize;
```

Use `DriftSize` in C structs and function signatures.
4. **Public APIs vs. FFI surface:** FFI modules under `lang.abi.*` mirror C headers exactly and therefore use fixed-width Drift primitives whenever the C API does. Public Drift APIs in `std.*` and user code should prefer the natural-width types (`Int`, `Uint`, `Size`, `Float`, domain types) and hide fixed widths behind wrappers. Narrowing conversions (e.g., `Size` → `Uint32` for a `uint32_t len` parameter) must be explicit and checked or documented.

#### 17.5.1. FFI wrapper pattern

For C APIs that use explicit fixed widths (e.g., `uint32_t len`), a recommended pattern:

- Low-level binding in `lang.abi.*` using fixed-width types:

```drift
// lang.abi.zlib
extern "C"
fn crc32(seed: Uint32, data: &Uint8, len: Uint32) returns Uint32
```

- High-level wrapper in `std.*` using `Size`/containers:

```drift
fn narrow_size_to_u32(len: Size) returns Uint32 {
    if len > Uint32::MAX {
        throw Error("len-too-large", code = "zlib.len.out_of_range")
    }
    return cast(len)
}

fn crc32(seed: Uint32, buf: ByteBuffer) returns Uint32 {
    val len32: Uint32 = narrow_size_to_u32(buf.len())
    return lang.abi.zlib.crc32(seed, buf.as_slice().data_ptr(), len32)
}
```

Public code imports the wrapper; fixed widths stay localized to the FFI layer.

### 17.6. FFI via `lang.abi`

Interop lives in `lang.abi`, which exposes opaque pointer/slice types instead of raw addresses:

- `abi.CPtr<T>` / `abi.MutCPtr<T>` — handles that represent foreign pointers; they can be passed around but not dereferenced directly.
- `abi.Slice<T>` / `abi.MutSlice<T>` — safe views that lower to `(ptr, len)` at the ABI boundary.
- `extern "C" struct` / `extern "C" fn` map to C layouts and calls.

Only `lang.abi` knows how to construct these handles from actual addresses. Example:

```drift
import lang.abi as abi

extern "C" struct Point { x: Int32, y: Int32 }
extern "C" fn draw(points: abi.Slice<Point>) returns Int32

fn render(points: Array<Point>) returns Int32 {
    return draw(points.as_slice())     // no raw pointers in user code
}
```

#### 17.5.1. Callbacks (C ABI)

- Only **non-capturing** functions may cross the C ABI as callbacks; they are exported/imported as thin `extern "C"` function pointers. This matches C’s model and keeps the ABI predictable.
- Capturing closures are **not** auto-wrapped for C. If state is needed, authors must build it explicitly (e.g., a struct of state plus a manual trampoline taking `void*`), and manage allocation/freeing on the C side; the language runtime does not box captures for C callbacks.
- Drift-side code calling into C APIs that accept only a bare function pointer must provide a non-capturing function; APIs that also accept a user-data pointer can be targeted later with an explicit `ctx`+trampoline pattern, but that is a deliberate, manual choice.
- Callbacks returned **from** C are treated as opaque `extern "C"` function pointers (cdecl). If the C API also returns a `ctx`/userdata pointer, it is modeled as a pair `{fn_ptr, ctx_ptr}` but remains **borrowed**: Drift does not free or drop it unless the API explicitly transfers ownership. Wrappers must:
  - enforce the C calling convention,
  - reject null pointers (or fail fast if invoked),
  - prevent Drift exceptions from crossing into C (catch and convert to a Drift error),
  - assume no thread-affinity guarantees unless the API states otherwise.

### 17.6. Unsafe modules (`lang.internals`)

Truly low-level helpers (`Slot<T>`, unchecked length changes, raw buffer manipulation) live in sealed modules such as `lang.internals`. Importing them requires explicit opt-in (feature flag + `@unsafe` annotations). Most applications never import these modules; the standard library and advanced crates do so when implementing containers or FFI shims.

### 17.7. Examples

**Placement without pointers**

```drift
var arr = Array<UserType>.with_capacity(10)

var value = UserType(...)
arr.push(value)                      // standard path

var builder = arr.begin_uninit(1)
builder.write(value)
builder.finish()
```

**FFI call**

```drift
import lang.abi as abi

extern "C" struct Buf { data: abi.CPtr<U8>, len: Int32 }
extern "C" fn send(buf: abi.Slice<U8>) returns Int32

fn transmit(bytes: Array<U8>) returns Int32 {
    return send(bytes.as_slice())
}
```

**Plugin note.** OS-level plugins (shared libraries) are just C-ABI FFI surfaces from Drift’s point of view. Authors should design plugin APIs as small C-style interfaces (opaque handles + error codes) and wrap them in static Drift modules as described in Chapter 21.

### 17.8. Summary

- The surface language never exposes raw pointer syntax or arithmetic.
- Constructors, builders, and slices provide placement-new semantics without revealing addresses.
- FFI always flows through `lang.abi` with opaque handles.
- Unsafe helpers live behind `lang.internals` and require explicit opt-in.
- Programmers still achieve zero-cost interop and efficient container implementations while keeping the foot-guns sealed away.

---

---

## 18. Standard I/O design

### 18.1. `std.io` module

```drift
module std.io

interface OutputStream {
    fn write(self: &OutputStream, bytes: ByteSlice) returns Void
    fn writeln(self: &OutputStream, text: String) returns Void
    fn flush(self: &OutputStream) returns Void
}

interface InputStream {
    fn read(self: &InputStream, buffer: MutByteSlice) returns Int64
}
```

### 18.2. `std.console` module

```
// initialized before main is called
val out : OutputStream = ... 
val err : OutputStream = ...
val in  : InputStream = ...
```

These built-in instances represent the system console streams. Now you can write simple console programs without additional setup:

```drift
import std.console.out as out

fn main() returns Void {
    val name: String = "Drift"
    out.writeln("Hello, " + name)
}
```

`out.writeln` accepts any value whose type implements the `Display` trait. All builtin primitives (`Int64`, `Float64`, `Bool`, `String`, `Error`) implement `Display`, so diagnostics like `out.writeln(verify(order))` type-check without manual string conversions.

This model allows concise I/O while keeping imports explicit and predictable.  
The objects `out`, `err`, and `in` are references to standard I/O stream instances.


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

val t = conc.spawn(fn() returns Int {
    return compute_answer()
})

val ans = t.join()
```

Spawn operations return a handle whose `join()` parks the caller until completion. Joining a failed thread returns a `JoinError` encapsulating the thrown `Error`.

#### 19.2.1. Custom executors

Developers may target a specific executor policy:

```drift
val policy = ExecutorPolicy.builder()
    .min_threads(4)
    .max_threads(32)
    .queue_limit(5000)
    .timeout(2.seconds)
    .on_saturation(Policy.RETURN_BUSY)
    .build()

val exec = conc.make_executor(policy)

val t = conc.spawn_on(exec, fn() returns Void {
    handle_connection()
})
```

#### 19.2.2. Structured concurrency

`conc.scope` groups spawned threads so they finish before the scope exits:

```drift
conc.scope(fn(scope: conc.Scope) returns Void {
    val u = scope.spawn(fn() returns User { load_user(42) })
    val d = scope.spawn(fn() returns Data { fetch_data() })

    val user = u.join()
    val data = d.join()

    render(user, data)
})
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
    .build()

val exec = conc.make_executor(policy)
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
- `join()` returns either the thread’s result or a `JoinError` capturing the propagated `Error`.
- Parking/unparking is transparent to user code.
- `Send`/`Sync` trait bounds govern which values may move across threads or be shared by reference.

### 19.7. Intrinsics: `lang.thread`

At the bottom layer the runtime exposes a minimal intrinsic surface to the standard library:

```drift
module lang.thread

@intrinsic fn vt_spawn(entry: fn() returns Void, exec: ExecutorHandle)
@intrinsic fn vt_park() returns Void
@intrinsic fn vt_unpark(thread: VirtualThreadHandle) returns Void
@intrinsic fn current_executor() returns ExecutorHandle

@intrinsic fn register_io(fd: Int, interest: IOEvent, thread: VirtualThreadHandle)
@intrinsic fn register_timer(when: Timestamp, thread: VirtualThreadHandle)
```

Library code such as `std.concurrent` is responsible for presenting straightforward APIs; user programs never touch these intrinsics directly.

### 19.8. Scoped virtual threads

Structured scopes ensure children finish (or are cancelled) before scope exit:

```drift
conc.scope(fn(scope: conc.Scope) returns Void {
    val a = scope.spawn(fn() returns Int { slow_calc() })
    val b = scope.spawn(fn() returns Int { slow_calc() })
    val c = scope.spawn(fn() returns Int { slow_calc() })

    val ra = a.join()
    val rb = b.join()
    val rc = c.join()

    out.writeln(ra + rb + rc)
})
```

This pattern mirrors `try/finally`: if any child throws, the scope cancels the rest and rethrows after all joins complete.

### 19.9. Interaction with ownership & memory

- Moves between threads require `Send`; shared borrows require `Sync`.
- Destructors run deterministically when each virtual thread ends, preserving RAII guarantees.
- Containers backed by `RawBuffer` (`Array`, `Map`, etc.) behave identically on all threads.

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

### 20.3. Module package layout

```
+------------------+
| HEADER           |
+------------------+
| METADATA         |  ← signed
+------------------+
| DMIR             |  ← signed
+------------------+
| SIGNATURE_BLOCK  |  ← not signed
+------------------+
| OPTIONAL_SOURCE  |  ← not signed
+------------------+
```

- **Header**: magic (`"DRIFTDMP"`), format version, offsets/sizes for each section.
- **Metadata**: deterministic map (name, version, dependency digests, minimum compiler). Encoded with canonical key ordering.
- **DMIR**: list of canonical items `{kind, fully-qualified name, canonical body}`.
- **Signature block**: one or more signature entries (e.g., Ed25519). The signature covers `HEADER .. DMIR`.
- **Optional source**: raw UTF-8 source for auditing; not part of verification.

The **METADATA** section includes an `exports` table describing the module interface:

- For each exported function: fully-qualified name, type, and a flag indicating that it uses the standard Drift `Result<T, Error>` calling convention.
- Non-exported functions and types are omitted from this table and cannot be imported by other modules.
- The `exports` table is the canonical source of truth for which symbols may be referenced across module boundaries.

### 20.4. Signatures and verification

1. Compute `payload = bytes[header_start .. dmir_end]`.
2. Hash with SHA-256.
3. Verify at least one signature in the signature block against the trusted key store.
4. If a dependency manifest pins a digest or signer, those must match.
5. Reject if `dmir_version` is unsupported.

Keys live in a simple TOML trust store:

```toml
[[trusted_keys]]
id   = "drift-stdlib"
algo = "ed25519"
pub  = "base64..."
```

Projects may additionally pin dependency digests or require specific signers.

**Verification point.** DMP signatures are verified only at module import / compilation time by the Drift toolchain. No runtime signature verification is performed by the generated program, and DMP is not a runtime tamper-resistance mechanism.

### 20.5. Security properties

- Repository compromises cannot forge modules without the private key.
- Canonicalization ensures reproducible builds and stable signatures.
- DMIR versioning decouples language evolution from compiler releases.
- Optional source does not influence verification, so audits cannot poison signatures.

### 20.6. Future extensions

Potential enhancements include transparency logs, certificate-based hierarchies, revocation lists, and dual-signature modes.

Signed DMIR gives Drift a portable, semantically precise unit of distribution while keeping authenticity verifiable on every machine.

*Note:* The exact signing/verification scheme (PGP vs Ed25519, cert hierarchies, revocation policies) is still under design and will be finalized before the DMP format is stabilized. The structure here captures intent; cryptographic options may evolve.

**Design note — module interface and errors.** Drift deliberately restricts the module interface to a small, explicit set of exported functions that can throw. This keeps cross-module ABIs uniform (every exported function uses `Result<T, Error>`), simplifies plugin design, and prevents accidental exposure of internal helper functions. Internal code is free to optimize error handling aggressively, but anything that crosses a module boundary must treat errors as first-class values using the standard `Error` type and `Result<T, Error>` encoding.

---



## 21. Plugin-style extension via FFI

Drift’s core module system is **static**: modules are compiled into a single image (either directly from source or via DMIR/DMP), and their interfaces are described by the export list in DMIR (Chapter 20). All exported functions are conceptually `Result<T, Error>` and may unwind across static module boundaries (Chapter 14.7).

Dynamic, OS-level plugins (shared libraries such as `.so`, `.dll`, `.dylib`) are treated as **FFI**, not as first-class Drift modules:

- They use a C-style ABI.
- They are loaded with the host platform’s dynamic loader (`dlopen`/`dlsym`, `LoadLibrary`, etc.).
- Their public surface is a small, explicit C API (opaque handles, error codes), described in C headers rather than Drift `module` declarations.

Drift code interacts with such plugins by:

1. Defining an FFI surface in a dedicated `lang.abi.*` or application-specific FFI module:

   ```drift
   // Example: plugin FFI surface
   extern "C" struct PluginApi {
       version: Uint32,
       init: extern "C" fn() returns Int32,
       shutdown: extern "C" fn() returns Int32,
       do_work: extern "C" fn(handle: PluginHandle, req: &RequestC, resp: &mut ResponseC) returns Int32
   }

   extern "C"
   fn plugin_get_api(expected_version: Uint32) returns &PluginApi
   ```

2. Writing a **static Drift module** that wraps this C API in normal Drift functions and types:

   ```drift
   module host.plugins.example

   export {
       fn do_work(req: Request) returns Result<Response, Error>
   }

   fn do_work(req: Request) returns Result<Response, Error> {
       // call into the .so via FFI, map Int32 error codes to Drift Error, etc.
   }
   ```

3. Treating the FFI boundary like any other C interop:

   - No unwinding crosses the `.so` boundary.
   - Failures are communicated as **values** (e.g., integer error codes, small tagged enums).
   - Opaque handles are used for plugin-owned state; the host never relies on plugin internal layout.

### 21.1. Error handling at the FFI plugin boundary

At the OS-level plugin boundary:

- Drift’s `Error` type and unwinding **must not** cross into or out of a `.so`.
- Plugin APIs must return errors as ABI-stable primitives (e.g., `Int32` error codes, or a small `enum` marked as FFI-safe).
- Hosts are responsible for mapping these codes into Drift’s `Error` values at the wrapper layer (static modules).

Example:

```drift
extern "C"
fn plugin_do_work(api: &PluginApi, req: &RequestC, resp: &mut ResponseC) returns Int32

fn do_work(req: Request) returns Result<Response, Error> {
    var req_c = to_c_request(req)
    var resp_c = ResponseC.zero()

    val code = plugin_do_work(api, &req_c, &resp_c)
    if code == 0 {
        return Ok(from_c_response(resp_c))
    }
    // convert error code to Drift Error
    return Err(make_plugin_error(code))
}
```

This pattern keeps the OS plugin ABI small and stable while preserving Drift’s richer error model inside the static world.

### 21.2. Summary

- **Static modules** (Chapter 7, Chapter 20) are the core Drift unit of composition. They are compiled into a single image or via DMIR/DMP and may use the full error model and unwinding semantics.
- **Plugins in the OS sense** are handled via **FFI**: a C-style ABI with opaque handles and explicit error codes, wrapped by static Drift modules.
- The language does **not** define a separate “plugin module” kind or a first-class Drift-plugin ABI in this revision. Future revisions may introduce a higher-level Drift-to-Drift plugin profile if real-world experience justifies the added complexity.


## 22. Closures and callable traits

Drift treats callables as **traits first**, with an optional dynamic wrapper when you explicitly want type erasure. Capture modes are ownership-based; borrow captures (`&`, `&mut`) are intentionally deferred until the borrow/lifetime rules are specified.

### 22.1. Surface syntax

- Expression-bodied closures: `|params| => expr` (the expression value is returned).
- Block-bodied closures may be added later; if present, they follow normal function rules with explicit `return`.

### 22.2. Capture modes (current revision)

- `x` — **move** capture. Consumes the binding when the closure is created and stores it in the closure environment. Mutating the captured value mutates only the environment copy.
- `copy x` — **copy** capture. Requires `x` to implement `Copy`; duplicates the value into the environment and leaves the original usable.
- `&x`, `&mut x` — **not yet supported**; rejected until borrow/lifetime checking is specified.

Each captured name must be spelled explicitly; there is no implicit capture list.

### 22.3. Lowering model

- **Non-capturing** closures/functions lower to **thin function pointers** and are `Copy`.
- **Capturing** closures lower to a **fat object** `{ env_ptr, call_ptr }`, where `env_ptr` points to a heap box holding the captured values under their capture modes. The environment has a single destructor; dropping the closure drops the env exactly once.

### 22.4. Callable traits (static dispatch)

Closures automatically implement one or more callable traits based on how they use their environment:

```drift
trait Callable<Args, R> {
    fn call(self: &Self, args: Args) returns R
}

trait CallableMut<Args, R> {
    fn call(self: &mut Self, args: Args) returns R
}

trait CallableOnce<Args, R> {
    fn call(self: Self, args: Args) returns R
}
```

- Pure/non-mutating closures implement `Callable` and `CallableOnce`.
- Mutating closures implement `CallableMut` and `CallableOnce`.
- Closures that move out of their captures implement **only** `CallableOnce`.
- Non-capturing functions implement all three traits.

Generics use these traits for zero-cost, monomorphized dispatch:

```drift
fn apply_twice<F>(f: F, x: Int) returns Int
    require F is Callable<(Int), Int> {
    return f.call(x) + f.call(x)
}

fn accumulate<F>(f: &mut F, xs: Array<Int>) returns Void
    require F is CallableMut<(Int), Void> {
    var i = 0
    while i < xs.len() { f.call(xs[i]); i = i + 1 }
}

fn run_once<F>(f: F) returns Int
    require F is CallableOnce<Void, Int> {
    return f.call()
}
```

For multi-argument callables, `Args` is typically a tuple (e.g., `(Int, String)`); for zero-argument callables, use `Void` as the parameter type and call with `f.call()`.

### 22.5. Dynamic callable interface (opt-in erasure)

When you need runtime dispatch, use an explicit interface:

```drift
interface CallableDyn<Args, R> {
    fn call(self: &CallableDyn<Args, R>, args: Args) returns R
}

fn erase<F, Args, R>(f: F) returns CallableDyn<Args, R>
    require F is Callable<Args, R> {
    // implementation-defined boxing/adaptation
}
```

Erasure is explicit; the default callable path remains trait-based static dispatch.

### 22.6. ABI and interop notes

- Closures are ordinary Drift values and can cross Drift module/plugin boundaries like any other value.
- Capturing closures are **not** automatically wrapped for C ABIs. To interoperate with C callbacks, use a thin (non-capturing) function pointer or build an explicit `{ void* ctx, fn(ctx, …) }` trampoline; see `lang.abi` for guidance.
- Borrow captures will be added once the borrow/lifetime model is specified; until then they are rejected.


## Appendix A — Ownership Examples

```drift
struct Job { id: Int }

fn process(job: Job) returns Void {
    import std.console.out
    out.writeln("processing job " + job.id.to_string())
}

var j = Job(id = 1)

process(j)    // copy
process(j->)  // move
process(j)    // error: use of moved value
```

---

## Appendix B — Formal grammar (external)

This specification focuses on semantics: ownership, types, errors, concurrency, and runtime behavior. The complete formal grammar (tokens, precedence, productions) lives in `docs/drift-lang-grammar.md` and is authoritative for syntax. In case of conflict: semantics in this spec win for meaning; syntax in the grammar file wins for how code is parsed.
