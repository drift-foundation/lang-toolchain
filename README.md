# Drift Language Specification
### Revision 2025-11 (Rev 4) — Ownership, Mutability, Exceptions, and Deterministic Resource Management

---

## 1. Overview

**Drift** is a statically typed, compiled systems language designed for clarity, safety, and mechanical predictability.  
It merges **C++ ownership semantics** (RAII, deterministic destruction, const-correctness) with the **type safety and borrowing model of Rust**, within a concise, modern syntax.

<img src="assets/drift.png" alt="Drift" width="240" align="right" />

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

This model allows concise I/O while keeping imports explicit and predictable.  
The objects `out`, `err`, and `in` are references to standard I/O stream instances.


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

## 14. Traits, `where` Specifiers, and Compile-time Trait Logic

Traits describe **capabilities** a type may possess. They are compile-time contracts, not base classes or inheritance hierarchies. A type “has” a trait when it satisfies every function declared by that trait. Traits enable expressive, composable, type-safe polymorphism without runtime dispatch.

### 14.1 Defining traits

Traits declare required functions but no data:

```drift
trait Display {
    fn fmt(self, ref f: Formatter) returns Void
}

trait Debug {
    fn fmt(self, ref f: Formatter) returns Void
}
```



### 14.1.1 Trait bounds on traits

Traits may depend on other traits through `where` clauses, just like functions or impls. `Self` is the implicit type parameter that refers to the implementing type.

```drift
trait Printable
    where Self has Display and Debug
{
    // helper methods could be added here
}
```

Read this as “Printable is a trait where Self has Display and Debug.”

You can also extend behavior:

```drift
trait Summarizable
    where Self has Display
{
    fn summary(self, ref f: Formatter) returns Void
}
```

Using `Self` keeps trait bounds uniform with generic bounds elsewhere.

### 14.2 Implementing traits

A type implementing a dependent trait uses the same `where` syntax:

```drift
implement Printable for Point
    where Point has Display and Debug
{
    fn fmt(self, ref f: Formatter) returns Void {
        f.write("Printable(" + self.x.to_string() + ", " + self.y.to_string() + ")")
    }
}
```


Implementations attach behavior to concrete types—even those defined elsewhere:

```drift
struct Point { x: Int64, y: Int64 }

implement Display for Point {
    fn fmt(self, ref f: Formatter) returns Void {
        f.write("(" + self.x.to_string() + ", " + self.y.to_string() + ")")
    }
}

implement Debug for Point {
    fn fmt(self, ref f: Formatter) returns Void {
        f.write("Point{x=" + self.x.to_string() + ", y=" + self.y.to_string() + "}")
    }
}
```

Implementations can be conditional:

```drift
struct Box<T> { inner: T }

implement Display for Box<T>
    where T has Display
{
    fn fmt(self, ref f: Formatter) returns Void {
        self.inner.fmt(ref f)
    }
}
```



**Consistency overview**

| Context | Example |
|---------|---------|
| Function | `fn save<T>(x: T) returns Void where T has Serializable` |
| Struct   | `struct Box<T> where T has Display { inner: T }` |
| Trait    | `trait Printable where Self has Display and Debug { ... }` |
| Implement | `implement Display for Box<T> where T has Display { ... }` |

### 14.3 `where` specifiers

Generic declarations attach trait constraints with a trailing `where` clause:

```drift
fn save<T, U>(t: ref T, u: ref U) returns Void
    where T has Serializable and Hashable,
          U has Display
{
    t.serialize()
    val fmt = Formatter()
    u.fmt(ref fmt)
}
```

The clause reads like English: “T has Serializable and Hashable; U has Display.”

### 14.4 Trait expressions and boolean algebra

Constraints support boolean operators:

| Expression | Meaning |
|------------|---------|
| `T has Display and Debug` | T must satisfy both traits. |
| `T has Display or Debug`  | T must satisfy at least one. |
| `T has not Copy`          | T must not implement Copy. |
| `T has (Display and Debug) or Serializable` | Group with parentheses. |

Aliases capture common patterns:

```drift
traitexpr Printable = Display or Debug

fn show<T>(value: ref T) returns Void
    where T has Printable
{ ... }
```

### 14.5 Compile-time trait tests inside code

Within a `where`-constrained function you can branch on traits statically:

```drift
fn log<T>(value: ref T, verbose: Bool) returns Void
    where T has (Display or Debug)
{
    val fmt = Formatter()

    if verbose and trait T has Debug {
        Debug::fmt(value, ref fmt)
    }
    else if trait T has Display {
        Display::fmt(value, ref fmt)
    }
    else {
        fmt.write("<unprintable>")
    }

    out.writeln(fmt.to_string())
}
```

Only reachable branches remain after monomorphization; the compiler guarantees the calls inside a branch are valid for the trait being tested.

 Specialization via `where`

Overloads may differ solely by trait constraints:

```drift
fn serialize<T>(x: ref T) returns Bytes
    where T has Serializable
{
    return x.serialize()
}

fn serialize<T>(x: ref T) returns Bytes
    where T has not Serializable
{
    return reflect::dump(x)
}
```

Ambiguous matches cause a compile-time error; missing matches produce diagnostics like “Type Foo must have Serializable.”

### 14.7 Nested / negated trait tests

Trait expressions compose freely:

```drift
fn clone_if_possible<T>(x: ref T) returns T {
    if trait T has Copy {
        return x
    }
    else if trait T has Clone {
        return x.clone()
    }
    else {
        panic("Type not clonable")
    }
}
```

### 14.8 Lifetimes and references

Trait logic honors Drift’s borrowing rules. Returning a `ref` is only legal if it aliases data passed in by reference:

```drift
fn first_item<T>(list: ref List<T>) returns ref T
    where T has Display
{
    return ref list.items[0]
}
```

Returning a reference to a local is compile-time illegal.

### 14.9 Design philosophy

- Traits are capabilities, not inheritance hierarchies.
- `implement` attaches behavior retroactively without altering the original type.
- `where` clauses read as declarative capability statements.
- Trait tests in code blocks branch with zero runtime overhead.
- Boolean trait algebra gives precise control over specialization.

### 14.10 Complete example

```drift
trait Display { fn fmt(self, ref f: Formatter) returns Void }
trait Debug   { fn fmt(self, ref f: Formatter) returns Void }

struct Box<T> { inner: T }

implement Display for Box<T>
    where T has Display
{
    fn fmt(self, ref f: Formatter) returns Void { self.inner.fmt(ref f) }
}

implement Debug for Box<T>
    where T has Debug
{
    fn fmt(self, ref f: Formatter) returns Void { Debug::fmt(self.inner, ref f) }
}

fn log<T>(value: ref T, verbose: Bool) returns Void
    where T has (Display or Debug)
{
    val fmt = Formatter()
    if verbose and trait T has Debug {
        Debug::fmt(value, ref fmt)
    }
    else if trait T has Display {
        Display::fmt(value, ref fmt)
    }
    else {
        fmt.write("<unprintable>")
    }
    out.writeln(fmt.to_string())
}
```

Only reachable branches remain after monomorphization; the compiler guarantees the calls inside a branch are valid for the trait being tested.

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
