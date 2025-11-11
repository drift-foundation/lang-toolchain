# Drift Language Specification
### Revision 2025-11 (Rev 4) — Ownership, Mutability, Exceptions, and Deterministic Resource Management

---

## 1. Overview

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

fn process(job: Job) : Void {
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

fn upload(f: File) : Void {
    print("sending file")
}

var f = File()
upload(f->)   // ✅ move ownership
upload(f)     // ❌ cannot copy non-copyable type
```

---

### 3.5 Example — Borrowing instead of moving

```drift
fn inspect(f: ref File) : Void {
    print("just reading header")
}

var f = File()
inspect(ref f)    // borrow read-only
upload(f->)       // later move ownership away
```

---

### 3.6 Example — Mut borrow vs move

```drift
fn fill(f: ref mut File) : Void { /* writes data */ }

var f = File()
fill(ref mut f)   // exclusive mutable borrow
upload(f->)       // move after borrow ends
```

Borrow lifetimes are scoped to braces; once the borrow ends, moving is allowed again.

---

### 3.7 Example — Move return values

```drift
fn open(name: String) : File {
    let f = File()
    return f->        // move to caller
}

fn main() : Void {
    var f = open("log.txt")
}
```

Ownership flows *out* of the function; RAII ensures destruction if not returned.

---

### 3.8 Example — Composition of moves

```drift
fn take(a: Array<Job>) : Void { /* consumes array */ }

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
    fn write(self: ref OutputStream, bytes: Bytes) : Void
    fn writeln(self: ref OutputStream, text: String) : Void
    fn flush(self: ref OutputStream) : Void
}

interface InputStream {
    fn read(self: ref InputStream, buffer: ref mut Bytes) : Int
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

fn main() : Void {
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
fn parse_date(s: String) : Date {
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
fn fill(f: ref mut File) : Void { /* mutate */ }
fn tune(f: ref mut File) : Void { /* mutate */ }
fn finalize(f: File) : Void { /* consume */ }

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

fn process(job: Job) : Void {
    import std.console.out
    out.writeln("processing job " + job.id.to_string())
}

var j = Job(id = 1)

process(j)    // copy
process(j->)  // move
process(j)    // error: use of moved value
```

---

### End of Drift Language Specification


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
    fn present(self) : Bool
    fn none(self) : Bool
    fn unwrap(self) : T
    fn unwrap_or(self, default: T) : T
    fn map<U>(self, f: fn(T) : U) : Optional<U>
    fn if_present(self, f: fn(ref T) : Void) : Void
    fn if_none(self, f: fn() : Void) : Void
}

module Optional {
    fn of<T>(value: T) : Optional<T>
    fn none<T>() : Optional<T>
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
fn find_sku(id: Int64) : Optional<String> { /* ... */ }

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
import sys.console.out

struct Order {
    id: Int64,
    sku: String,
    quantity: Int64
}

fn find_order(id: Int64) : Optional<Order> {
    if id == 42 { return Optional.of(Order(id = 42, sku = "DRIFT-1", quantity = 1)) }
    return Optional.none()
}

fn ship(o: Order) : Void {
    out.writeln("shipping " + o.sku + " id=" + o.id)
}

fn main() : Void {
    val maybe_order = find_order(42)

    maybe_order.if_present(ref o: {
        ship(o)
    })

    if maybe_order.none() {
        out.writeln("order not found")
    }
}
```
