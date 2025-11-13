# Drift Language Specification
### Revision 2025-11 (Rev 4) — Ownership, Mutability, Exceptions, and Deterministic Resource Management

---

## 1. Overview

<img src="assets/drift.png" alt="Drift" width="240" align="right" />

**Drift** is a statically typed, compiled systems language designed for clarity, safety, and mechanical predictability.  
It merges **C++ ownership semantics** (RAII, deterministic destruction, const-correctness) with the **type safety and borrowing model of Rust**, within a concise, modern syntax.

This specification defines the **core semantics, syntax, and safety model** of the Drift programming language.  
It describes how values are owned, borrowed, and destroyed; how exceptions propagate structured diagnostic data; and how deterministic resource management (RAII) interacts with the type system and scoping rules.  
Drift provides predictable lifetimes, explicit control of mutability and ownership, and strong compile-time guarantees — all without a garbage collector.

**Focus areas:**
- **Deterministic ownership & move semantics (`x->`)**
- **Explicit mutability (`mut`, `ref`, `ref mut`)**
- **Structured exceptions with contextual capture (`^`)**
- **Memory-safe access primitives (`Volatile`, `Mutable`)**
- **Imports and system I/O (`import sys.console.out`)**
- **C-family block syntax with predictable scopes and lifetimes**
- **Generic arrays via `lang.array.Array<T>` with literal syntax `[a, b, ...]`**

**Struct design at a glance:**
- Drift exposes only `struct` for user-defined data; no record/class split.
- Tuple-struct header sugar: `struct Point(x: Int64, y: Int64)` — compact syntax with named fields.
- Anonymous tuple types: `(T1, T2, ...)` for ad-hoc, methodless bundles.
- Dot access always applies, even for tuple-struct sugar.

---

## 2. Variable and Reference Qualifiers

| Concept | Keyword / Syntax | Meaning |
|---|---|---|
| Immutable binding | `val` | Cannot be rebound or moved |
| Mutable binding | `var` | Can mutate or transfer ownership |
| Const reference | `ref T` | Shared, read-only access (C++: `T const&`) |
| Mutable reference | `ref mut T` | Exclusive, mutable access (C++: `T&`) |
| Ownership transfer | `x->` | Moves value, invalidating source |
| Interior mutability | `Mutable<T>` | Mutate specific fields inside const objects |
| Volatile access | `Volatile<T>` | Explicit MMIO load/store operations |
| **Blocks & scopes** | `{ ... }` | Define scope boundaries for RAII and deterministic lifetimes |

`val`/`var` bindings may omit the type annotation when the right-hand expression makes the type unambiguous. For example, `val greeting = "hello"` infers `String`, while `val nums = [1, 2, 3]` infers `Array<Int64>`. Add an explicit `: Type` when inference fails or when you want to document the intent.

#### Struct syntax variants

```drift
struct Point {
    x: Int64,
    y: Int64
}

struct Point(x: Int64, y: Int64)  // header form; identical type
```

The tuple-style header desugars to the block form. Field names remain available for dot access while the constructor supports positional and named invocation. The resulting type follows standard Drift value semantics: fields determine copy- vs move-behavior, and ownership transfer still uses `foo->` as usual.

---


## 3. Ownership and Move Semantics (`x->`)

`x->` transfers ownership of `x` without copying. After a move, `x` becomes invalid. Equivalent intent to `std::move(x)` in C++ but lighter and explicit.

### 3.1 Syntax

```drift
PostfixExpr ::= PrimaryExpr
              | PostfixExpr '->'
```

### 3.2 Core rules
| Aspect | Description |
|---------|-------------|
| **Move target** | Must be an owned (`var`) value. |
| **Copyable types** | `x` copies; `x->` moves. |
| **Non-copyable types** | Must use `x->`; plain `x` is a compile error. |
| **Immutable (`val`)** | Cannot move from immutable bindings. |
| **Borrowed (`ref`, `ref mut`)** | Cannot move from non-owning references. |

---

### 3.3 Example — Copy vs Move

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

### 3.4 Example — Non-copyable type

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

### 3.5 Example — Borrowing instead of moving

```drift
fn inspect(f: ref File) returns Void {
    print("just reading header")
}

var f = File()
inspect(ref f)    // borrow read-only
upload(f->)       // later move ownership away
```

---

### 3.6 Example — Mut borrow vs move

```drift
fn fill(f: ref mut File) returns Void { /* writes data */ }

var f = File()
fill(ref mut f)   // exclusive mutable borrow
upload(f->)       // move after borrow ends
```

Borrow lifetimes are scoped to braces; once the borrow ends, moving is allowed again.

---

### 3.7 Example — Move return values

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

### 3.8 Example — Composition of moves

```drift
fn take(a: Array<Job>) returns Void { /* consumes array */ }

var jobs = Array<Job>()
jobs.push(Job(id = 1))
jobs.push(Job(id = 2))

take(jobs->)    // move entire container
take(jobs)      // ❌ jobs invalid after move
```

---

### 3.9 Lifetime and destruction rules
- Locals are destroyed **in reverse declaration order** when a block closes.  
- Moving (`x->`) transfers destruction responsibility to the receiver.  
- Borrowed references are automatically invalidated at scope exit.  
- No garbage collection — **destruction is deterministic** (RAII).

---
## 4. Imports and Standard I/O

Drift uses explicit imports — no global or magic identifiers.  
Console output is available through the `std.console` module.

### 4.1 Import syntax (modules and symbols)

```drift
import std.console.out        // bind the exported `out` stream
import std.console.err        // bind the exported `err` stream
import std.io                 // bind the module
import std.console.out as print // optional alias
```

**Grammar**

```ebnf
ImportDecl    ::= 'import' ImportItem (',' ImportItem)* NEWLINE
ImportItem    ::= QualifiedName (' ' 'as' ' ' Ident)?
QualifiedName ::= Ident ('.' Ident)*
```

**Name‑resolution semantics**

- `QualifiedName` is resolved left‑to‑right.  
- If it resolves to a **module**, the import binds that module under its last segment (or the `as` alias).  
- If it resolves to an **exported symbol** inside a module (e.g., `std.console.out`), the import binds that symbol directly into the local scope under its own name (or the `as` alias).  
- Ambiguities between module and symbol names must be disambiguated with `as` or avoided.

## 5 Standard I/O Design

### `std.io` module

```drift
module std.io

interface OutputStream {
    fn write(self: ref OutputStream, bytes: Bytes) returns Void
    fn writeln(self: ref OutputStream, text: String) returns Void
    fn flush(self: ref OutputStream) returns Void
}

interface InputStream {
    fn read(self: ref InputStream, buffer: ref mut Bytes) returns Int
}
```

### `std.console` module

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


## 6. `lang.array` and Array Literals

`lang.array` is the standard module for homogeneous sequences. It exposes the generic type `Array<T>` plus builder helpers. `Array` is always in scope for type annotations, so you can write:

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

### 6.1 Indexing and mutation

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


## 7. Variant Types (`variant`)

Drift’s `variant` keyword defines **tagged unions**: a value that is exactly one of several named alternatives (variants). Each alternative may carry its own fields, and the compiler enforces exhaustive handling when you `match` on the value.

### 7.1 Syntax

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

### 7.2 Semantics and representation

A `variant` value stores:

1. A hidden **tag** indicating which alternative is active.
2. The **payload** for that variant’s fields.

Only the active variant’s fields may be accessed. This is enforced statically by pattern matching.

### 7.3 Construction

Each variant behaves like a constructor:

```drift
val success: Result<Int64, String> = Ok(value = 42)
val failure = Err(error = "oops")            // type inference fills in `<Int64, String>`
```

Named arguments are required when a variant has multiple fields; single-field variants may support positional construction, though the explicit form is always accepted.

### 7.4 Pattern matching and exhaustiveness

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
variant Option<T> {
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

### 7.5 Recursive data

Variants are ideal for ASTs and other recursive shapes:

```drift
variant Expr {
    Literal(value: Int64)
    Add(lhs: ref Expr, rhs: ref Expr)
    Neg(inner: ref Expr)
}

fn eval(expr: ref Expr) returns Int64 {
    match expr {
        Literal(value) => value
        Add(lhs, rhs) => eval(lhs) + eval(rhs)
        Neg(inner) => -eval(inner)
    }
}
```

### 7.6 Generics

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

### 7.7 Value semantics and equality

Variants follow Drift’s value semantics: they are copied/moved by value, and their equality/ordering derive from their payloads. Two `Result` values are equal only if they hold the same variant *and* the corresponding fields are equal.

### 7.8 Evolution considerations

- Adding a new variant is a **breaking change** because every `match` must handle it explicitly.
- Library authors should document variant additions clearly or provide fallback variants when forward compatibility matters.

Variants underpin key library types such as `Result<T, E>` and `Option<T>`, enabling safe, expressive modeling of operations with multiple outcomes.


## 11. Exceptions and Context Capture

Drift provides structured exception handling through a unified `Error` type and the `^` capture modifier.  
This enables precise contextual diagnostics without boilerplate logging or manual tracing.

### 11.1 Exception model

All exceptions share a single type:

```drift
struct Error {
    message: String,
    code: String,
    cause: Option<Error>,
    ctx_frames: Array<CtxFrame>,
    stack: Backtrace
}
```

Each function frame can contribute contextual data via the `^` capture syntax. If you omit the `as "alias"` clause, the compiler derives a key from the enclosing scope (e.g., `parse_date.input`). Duplicate keys inside the same lexical scope are rejected at compile time (`E3510`).

### 11.2 Capturing local context

```drift
val ^input: String as "record.field" = msg["startDate"]
fn parse_date(s: String) returns Date {
    if !is_iso_date(s) {
        throw Error("invalid date", code="parse.date.invalid")
    }
    return decode_iso(s)
}
```

Captured context frames appear in order from the throw site outward, e.g.:

```json
{
  "error": "invalid date",
  "ctx_frames": [
    { "fn": "parse_date", "data": { "record.field": "2025-13-40" } },
    { "fn": "ingest_record", "data": { "record.id": "abc-123" } }
  ]
}
```

### 11.3 Runtime behavior

- Each captured variable (`^x`) adds its name and optional alias to the current frame context.
- Context maps are stacked per function frame.
- The runtime merges and serializes this information into the `Error` object when unwinding.

### 11.4 Design goals

- **Automatic context:** No need for explicit `try/catch` scaffolding.
- **Deterministic structure:** The captured state is reproducible and bounded.
- **Safe preview:** Large or sensitive values can be truncated or redacted.
- **Human-readable JSON form:** Ideal for logs, telemetry, or debugging.

---

## 12. Mutators, Transformers, and Finalizers

In Drift, a function’s **parameter ownership mode** communicates its **lifecycle role** in a data flow.  
This distinction becomes especially clear in pipelines (`>>`), where each stage expresses how it interacts with its input.

### 12.1 Function roles

| Role | Parameter type | Return type | Ownership semantics | Typical usage |
|------|----------------|--------------|---------------------|----------------|
| **Mutator** | `ref mut T` | `Void` or `T` | Borrows an existing `T` mutably and optionally returns it. Ownership stays with the caller. | In-place modification, e.g. `fill`, `tune`. |
| **Transformer** | `T` | `U` (often `T`) | Consumes its input and returns a new owned value. Ownership transfers into the call and out again. | `compress`, `clone`, `serialize`. |
| **Finalizer / Sink** | `T` | `Void` | Consumes the value completely. Ownership ends here; the resource is destroyed or released at function return. | `finalize`, `close`, `free`, `commit`. |

### 12.2 Pipeline behavior

The pipeline operator `>>` is **ownership-aware**.  
It automatically determines how each stage interacts based on the callee’s parameter type:

```drift
fn fill(f: ref mut File) returns Void { /* mutate */ }
fn tune(f: ref mut File) returns Void { /* mutate */ }
fn finalize(f: File) returns Void { /* consume */ }

open("x")
  >> fill      // borrows mutably; File stays alive
  >> tune      // borrows mutably again
  >> finalize; // consumes; File is now invalid
```

- **Mutator stages** borrow temporarily and return the same owner.
- **Transformer stages** consume and return new ownership.
- **Finalizer stages** consume and end the pipeline.

At the end of scope, if the value is still owned (not consumed by a finalizer), RAII automatically calls its destructor.

### 12.3 Rationale

This mirrors real-world resource lifecycles:
1. Creation — ownership established.  
2. Mutation — zero or more `ref mut` edits.  
3. Transformation — optional `T → U`.  
4. Finalization — release or destruction.

Explicit parameter types make these transitions visible and verifiable at compile time.

### 12.4 RAII interaction

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

## 13. Grammar (EBNF excerpt)

*(Trait/`implement`/`where` grammar is summarized in Appendix B.)*


```ebnf
Program     ::= ImportDecl* TopDecl*
ImportDecl  ::= "import" ImportItem ("," ImportItem)* NEWLINE
ImportItem  ::= ModulePath ("as" Ident)?
ModulePath  ::= Ident ("." Ident)*

TopDecl     ::= FnDef | TypeDef | StructDef | EnumDef

FnDef       ::= "fn" Ident "(" Params? ")" (":" Type)? Block
Params      ::= Param ("," Param)*
Param       ::= Ident ":" Ty | "^" Ident ":" Ty

Block       ::= "{" Stmt* "}"
Stmt        ::= ValDecl | VarDecl | ExprStmt | IfStmt | WhileStmt | ForStmt
              | ReturnStmt | BreakStmt | ContinueStmt | TryStmt | ThrowStmt

ValDecl     ::= "val" Ident ":" Ty "=" Expr NEWLINE
VarDecl     ::= "var" Ident ":" Ty "=" Expr NEWLINE
ExprStmt    ::= Expr NEWLINE
```

**Terminators and newlines.** The lexer emits a `TERMINATOR` whenever it encounters a newline (`\n`) and *all* of the following hold:

1. The current parenthesis/brace/bracket depth is zero (i.e., we are not inside `()`, `[]`, or `{}`).
2. The previous token is “terminable” — identifiers, literals, `)`, `]`, `}`, `return`, `break`, etc.
3. The previous token is **not** a binary operator (`+`, `*`, `>>`, ...), dot, comma, or colon that requires a follower.

Parsers may treat `TERMINATOR` exactly like a semicolon. Conversely, an explicit `;` is legal anywhere a `TERMINATOR` could appear, which allows compact one-liners or multi-statement lines when desired. This rule keeps Drift source tidy without forcing mandatory semicolons.

---

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

## 13. Null Safety & Optional Values

Drift is **null-free**. There is no `null` literal. A value is either present (`T`) or explicitly optional (`Optional<T>`). The compiler never promotes `Optional<T>` to `T` implicitly.

### 13.1 Types

| Type | Meaning |
|------|---------|
| `T` | Non-optional; always initialized. |
| `Optional<T>` | Possibly empty; either a value or nothing. |

### 13.2 Construction

```drift
val present: Optional<Int64> = Optional.of(42)
val empty: Optional<Int64> = Optional.none()
```

### 13.3 Interface

```drift
interface Optional<T> {
    fn present(self) returns Bool
    fn none(self) returns Bool
    fn unwrap(self) returns T
    fn unwrap_or(self, default: T) returns T
    fn map<U>(self, f: fn(T) returns U) returns Optional<U>
    fn if_present(self, f: fn(ref T) returns Void) returns Void
    fn if_none(self, f: fn() returns Void) returns Void
}

module Optional {
    fn of<T>(value: T) returns Optional<T>
    fn none<T>() returns Optional<T>
}
```

- `present()` is true when a value exists.
- `none()` is true when empty.
- `unwrap()` throws `Error("option.none_unwrapped")` if empty (discouraged in production).
- `map` transforms when present; otherwise stays empty.
- `if_present` calls the block with a borrow (`ref T`) to avoid moving.
- `if_none` runs a block when empty.

### 13.4 Control flow

```drift
if qty.present() {
    out.writeln("qty=" + qty.unwrap().toString())
}

if qty.none() {
    out.writeln("no qty")
}

qty.if_present(ref q: {
    out.writeln("qty=" + q.toString())
})
```

There is no safe-navigation operator (`?.`). Access requires explicit helpers.

### 13.5 Parameters & returns

- A parameter of type `T` cannot receive `Optional.none()`.
- Use `Optional<T>` for “maybe” values.
- Returning `none()` from a function declared `: T` is a compile error.

```drift
fn find_sku(id: Int64) returns Optional<String> { /* ... */ }

val sku = find_sku(42)
sku.if_present(ref s: { out.writeln("sku=" + s) })
if sku.none() {
    out.writeln("missing")
}
```

### 13.6 Ownership

`if_present` borrows (`ref T`) by default. No move occurs unless you explicitly consume `T` inside the block.

### 13.7 Diagnostics (illustrative)

- **E2400**: cannot assign `Optional.none()` to non-optional type `T`.
- **E2401**: attempted member/method use on `Optional<T>` without `map`/`unwrap`/`if_present`.
- **E2402**: `unwrap()` on empty optional.
- **E2403**: attempted implicit conversion `Optional<T>` → `T`.

### 13.8 End-to-end example

```drift

### 13.9 Tuple structs & tuple returns

- **Tuple structs:** `struct Point(x: Int64, y: Int64)` is a compact header that desugars to the standard block struct. Construction may be positional (`Point(10, 20)`) or named (`Point(x = 10, y = 20)`), dot access remains (`point.x`).

- **Anonymous tuple types:** use parentheses for ad-hoc results, e.g. `fn bounds() returns (Int64, Int64, Int64, Int64)` and destructure with `(x1, y1, x2, y2) = bounds()`.

import sys.console.out

struct Order {
    id: Int64,
    sku: String,
    quantity: Int64
}

fn find_order(id: Int64) returns Optional<Order> {
    if id == 42 { return Optional.of(Order(id = 42, sku = "DRIFT-1", quantity = 1)) }
    return Optional.none()
}

fn ship(o: Order) returns Void {
    out.writeln("shipping " + o.sku + " id=" + o.id)
}

fn main() returns Void {
    val maybe_order = find_order(42)

    maybe_order.if_present(ref o: {
        ship(o)
    })

    if maybe_order.none() {
        out.writeln("order not found")
    }
}
```

---
## 14 Traits and Compile-Time Capabilities

(*Conforms to Drift Spec Rev. 2025-11 (Rev 4)*)  
(*Fully consistent with the `require` + `is` syntax finalized in design discussions.*)

---

### 14.1. Overview

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

### 14.2. Defining Traits

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

### 14.3. Implementing Traits

An implementation attaches the capability to a type.

```drift
struct Point { x: Int64, y: Int64 }

implement Debuggable for Point {
    fn fmt(self) returns String {
        return "(" + self.x.to_string() + ", " + self.y.to_string() + ")"
    }
}
```

#### Generic trait implementations

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

### 14.4. Type-Level Trait Requirements (`require`)

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

#### Requiring traits of parameters

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

### 14.5. Function-Level Trait Requirements

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

### 14.6. Trait Guards (`if T is TraitName`)

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

### Multiple trait conditions

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

### 14.7. Trait Expressions (Boolean Logic)

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

### 14.8. Trait Dependencies (Traits requiring Traits)

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

### 14.9. RAII and the `Destructible` Trait

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

### 14.10. Overloading and Specialization by Trait

Functions may overload based on trait requirements:

```drift
fn save<T>
    require T is Serializable
(value: T) returns Bytes {
    return value.serialize()
}

fn save<T>(value: T) returns Bytes {
    return reflect::dump(value)
}
```

Rules:

- The compiler picks the most specific applicable overload.
- Ambiguity is a compile‑time error.
- If no overload applies, the compiler reports a missing capability.

---

### 14.11. Complete Syntax Summary

#### Defining a trait

```drift
trait Debuggable {
    fn fmt(self) returns String
}
```

#### Implementing a trait

```drift
implement Debuggable for File {
    fn fmt(self) returns String { ... }
}
```

#### Requiring traits in a type (type completeness)

```drift
struct Cache<K, V>
    require K is Hashable,
            Self is Destructible
{
    ...
}
```

#### Requiring traits in a function

```drift
fn print<T>
    require T is Debuggable
(v: T) returns Void { ... }
```

#### Trait-guarded logic

```drift
if T is Debuggable { ... }
if not (T is Serializable) { ... }
```

#### Boolean trait expressions

```drift
require T is (Debuggable or Displayable)
require T is Clonable and not Destructible
```

---

### 14.12. Design Rationale

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
# 15. Interfaces & Dynamic Dispatch

Drift supports **runtime polymorphism** through *interfaces*.  
Interfaces allow multiple **different concrete types** to be treated as one unified abstract type at runtime.  
This is the dynamic counterpart to compile‑time polymorphism provided by *traits*.

**In short:**

- **Traits** describe *capabilities* and enable *compile‑time specialization*.
- **Interfaces** describe *runtime object shapes* and enable *dynamic dispatch*.

Traits are *not* types; interfaces *are* types.  
Both systems integrate cleanly with Drift’s ownership, RAII, and borrowing rules.

---

# 15.1 Interface Definitions

Interfaces define a set of functions callable on any implementing type.

```drift
interface OutputStream {
    fn write(self: ref OutputStream, bytes: Bytes) returns Void
    fn writeln(self: ref OutputStream, text: String) returns Void
    fn flush(self: ref OutputStream) returns Void
}
```

### Rules

- Interfaces may not define fields — pure behavior only.
- Interfaces are **first‑class types** (unlike traits).
- A function that receives an `OutputStream` may be passed any object that implements that interface.

---

# 15.2 Implementing Interfaces

A concrete type implements an interface through an `implement` block:

```drift
struct File {
    fd: Int64
}

implement OutputStream for File {
    fn write(self: ref File, bytes: Bytes) returns Void {
        sys_write(self.fd, bytes)
    }

    fn writeln(self: ref File, text: String) returns Void {
        self.write((text + "\n").to_bytes())
    }

    fn flush(self: ref File) returns Void {
        sys_flush(self.fd)
    }
}
```

Rules:

1. All interface functions must be provided.
2. Methods receive a reference to the **concrete type** (`ref File`), not the interface.
3. A type may implement multiple interfaces.
4. Implementations may appear in any module.

---

# 15.3 Using Interface Values

Interfaces may be used anywhere that types may appear.

### As parameters

```drift
fn write_header(out: OutputStream) returns Void {
    out.writeln("=== header ===")
}
```

### As return values

```drift
fn open_log(path: String) returns OutputStream {
    var f = File.open(path)
    return f      // implicit upcast: File → OutputStream
}
```

### As locals

```drift
var out: OutputStream = std.console.out
out.writeln("ready")
```

### In heterogeneous arrays

```drift
var sinks: Array<OutputStream> = []
sinks.push(open_log("app.log"))
sinks.push(std.console.out)
```

Each element may be a different type implementing the same interface.

---

# 15.4 Dynamic Dispatch Semantics

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

# 15.5 Interfaces vs Traits

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

# 15.6 Shape Example

### Define the interface

```drift
interface Shape {
    fn area(self: ref Shape) returns Float64
}
```

### Implementations

```drift
struct Circle { radius: Float64 }
struct Rect   { w: Float64, h: Float64 }

implement Shape for Circle {
    fn area(self: ref Circle) returns Float64 {
        return 3.14159265 * self.radius * self.radius
    }
}

implement Shape for Rect {
    fn area(self: ref Rect) returns Float64 {
        return self.w * self.h
    }
}
```

### Usage

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

# 15.7 Ownership & RAII for Interface Values

Interface values follow Drift ownership and move semantics.

### Moving

```drift
fn consume(out: OutputStream) returns Void {
    out.writeln("consumed")
}
```

Passing `out` moves the *interface wrapper* and transfers ownership of the underlying concrete value.

### Destruction

At scope exit:

- If underlying type implements `Destructible`, its `destroy(self)` runs.
- Otherwise, nothing is done.

```drift
{
    var log = open_log("a.log")    // OutputStream
    log.writeln("start")
}   // log.destroy() runs if File is Destructible
```

No double‑destroy is possible because `destroy(self)` consumes the value.

---

# 15.8 Multiple Interfaces

A type may implement several interfaces:

```drift
interface Readable  { fn read(self: ref Readable) returns Bytes }
interface Writable  { fn write(self: ref Writable, b: Bytes) returns Void }
interface Duplex    { fn close(self: ref Duplex) returns Void }

struct Stream { ... }

implement Readable for Stream { ... }
implement Writable for Stream { ... }
implement Duplex   for Stream { ... }
```

Each interface gets its own vtable.  
There is no conflict unless the implementing type violates signature constraints.

---

# 15.9 Interfaces + Traits Together

These systems complement each other:

```drift
trait Debuggable { fn fmt(self) returns String }

interface DebugSink {
    fn write_debug(self: ref DebugSink, msg: String) returns Void
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

# 15.10 Error Handling Across Interfaces

Interface method calls participate in normal exception propagation:

```drift
fn dump(src: InputStream, dst: OutputStream) returns Void {
    var buf = Bytes(4096)
    loop {
        val n = src.read(ref buf)
        if n == 0 { break }
        dst.write(buf.slice(0, n))
    }
}
```

Thrown errors travel unchanged across interface boundaries, preserving `^`-captured context.

---

# 15.11 Summary

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

- **Traits for compile‑time adaptability**
- **Interfaces for runtime flexibility**

---

## Appendix B — Trait Grammar Notes

Traits and implementations use the same `where` syntax as functions. Grammar sketch:

```ebnf
TraitDef   ::= "trait" Ident TraitParams? TraitWhere? TraitBody
TraitWhere ::= "where" TraitClause ("," TraitClause)*
TraitClause ::= "Self" "has" TraitExpr

Implement  ::= "implement" Ty "for" Ty TraitWhere? TraitBody

TraitExpr  ::= TraitTerm ( ("and" | "or") TraitTerm )*
TraitTerm  ::= "not"? Ident | "(" TraitExpr ")"
```

These forms defer to the existing `where` machinery described in Section 14.

### End of Drift Language Specification
