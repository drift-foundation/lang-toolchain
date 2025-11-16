# Drift Language Specification
---

## 1. Overview

Drift is a modern systems language built on a simple premise: programming should be pleasant, expressive, and safe by default — without giving up the ability to write efficient, low-level code when you actually need it.

Most languages pick a side:

- High-level and comfortable, but slow when you push the limits.
- Low-level and risky, but fast if you fight the compiler hard enough.

Drift rejects that binary. You get a single language that works across the entire performance spectrum.

### 1.1 Safety first, without sacrificing power

Drift avoids the foot-guns that plague many systems languages:

- No raw pointers in userland.
- No pointer arithmetic.
- Clear ownership and deterministic destruction (RAII).
- Explicit moves instead of silent copies.

Yet it doesn’t enforce safety by making everything slow or hiding costs behind a garbage collector.

### 1.2 Escape hatches when you ask for them

High-level code stays high-level by default. Low-level control appears only when you deliberately reach for the tooling (`lang.abi`, `lang.internals`, `@unsafe`).

### 1.3 Move semantics everywhere

Passing a value by value moves it—no deep copies unless you opt in. Moves are cheap; cloning is explicit.

### 1.4 Zero-cost abstractions

Drift’s abstractions compile down to what you would hand-write. Ownership, traits, interfaces, and concurrency are “pay for what you use.”

### 1.5 Ready out of the box, no hidden machinery

The language ships meaningful tools (structured errors, virtual threads, collection literals) without magic or implicit globals. Everything is imported explicitly.

### 1.6 Predictable interop

Precise binary layouts, opaque ABI types, and sealed unsafe modules keep foreign calls predictable without exposing raw pointers.

### 1.7 Representation transparency only when requested

Everyday Drift code treats core types as opaque. When you need to see the layout, you opt in via `extern "C"` or `lang.abi` helpers.

### 1.8 Performance without fear

Write clear code first. When you profile a hotspot, the language gives you the tools to optimize surgically without rewriting everything in C.

### 1.9 A language for both humans and machines

Drift emphasizes predictability, clarity, and strong guarantees so humans can reason about programs—and so tooling can help without guesswork.

### 1.10 Signed modules and portable distribution

All modules compile down to a canonical Drift Module IR (DMIR) that can be cryptographically signed and shipped as a Drift Module Package (DMP). Imports are verified before execution, so every machine sees the same typed semantics and can reject tampered artifacts.

---

## 2. Variable and reference qualifiers

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


## 3. Ownership and move semantics (`x->`)

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

### 3.3 Default: move-only types

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

### 3.4 Opting into copying

Types that want implicit copies implement the `Copy` trait (see Section 13.3 for the trait definition). The trait is only available when **every field is copyable**. Primitives already implement it; your structs may do the same:

```drift
implement Copy for Int {}
implement Copy for Bool {}

struct Job { id: Int }
implement Copy for Job {}

var a = Job(id = 1)
var b = a      // ✅ copies `a` by calling `copy`
```

Copying still respects ownership rules: `ref self` indicates the value is borrowed for the duration of the copy, after which both the original and the newly returned value remain valid.

### 3.5 Explicit deep copies (`clone`-style)

If a move-only type wants to offer a deliberate, potentially expensive duplicate, it can expose an explicit method (e.g., `clone`). Assignment still will not copy—callers must opt in:

```drift
struct Buffer { data: Bytes }   // move-only

implement Buffer {
    fn clone(ref self) returns Buffer {
        return Buffer(data = self.data.copy())
    }
}

var b1 = Buffer(...)
var b2 = b1.clone()   // ✅ explicit deep copy
var b3 = b1           // ❌ still not allowed; Buffer is not `Copy`
```

This pattern distinguishes cheap, implicit copies (`Copy`) from explicit, potentially heavy duplication.

---

### 3.6 Example — copy vs move

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

### 3.7 Example — non-copyable type

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

### 3.8 Example — borrowing instead of moving

```drift
fn inspect(f: ref File) returns Void {
    print("just reading header")
}

var f = File()
inspect(ref f)    // borrow read-only
upload(f->)       // later move ownership away
```

---

### 3.9 Example — mut borrow vs move

```drift
fn fill(f: ref mut File) returns Void { /* writes data */ }

var f = File()
fill(ref mut f)   // exclusive mutable borrow
upload(f->)       // move after borrow ends
```

Borrow lifetimes are scoped to braces; once the borrow ends, moving is allowed again.

---

### 3.10 Example — move return values

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

### 3.11 Example — composition of moves

```drift
fn take(a: Array<Job>) returns Void { /* consumes array */ }

var jobs = Array<Job>()
jobs.push(Job(id = 1))
jobs.push(Job(id = 2))

take(jobs->)    // move entire container
take(jobs)      // ❌ jobs invalid after move
```

---

### 3.12 Lifetime and destruction rules
- Locals are destroyed **in reverse declaration order** when a block closes.  
- Moving (`x->`) transfers destruction responsibility to the receiver.  
- Borrowed references are automatically invalidated at scope exit.  
- No garbage collection — **destruction is deterministic** (RAII).

---
## 4. Imports and standard I/O

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

## 5. Standard I/O design

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


## 6. `lang.array` and array literals

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


## 7. Collection literals (arrays and maps)

Drift includes literal syntax for homogeneous arrays (`[a, b, ...]`) and maps (`{ key: value, ... }`).
The syntax is part of the language grammar, but **literals never hard-wire a concrete container type**.
Instead, they are desugared through capability interfaces so projects can pick any backing collection.

### 7.1 Goals

1. **Ergonomics** — trivial programs should be able to write `val xs = [1, 2, 3]` without ceremony.
2. **Flexibility** — large systems must be free to route literals into custom containers, including
   arena-backed vectors, small-capacity stacks, or persistent maps.

### 7.2 Syntax

#### 7.2.1 Array literal

```
ArrayLiteral ::= "[" (Expr ("," Expr)*)? "]"
```

Example: `val xs = [1, 2, 3]`.

#### 7.2.2 Map literal

```
MapLiteral ::= "{" (MapEntry ("," MapEntry)*)? "}"
MapEntry   ::= Expr ":" Expr
```

Example: `val user = { "name": "Ada", "age": 38 }`.

Duplicate keys are allowed syntactically; the target type decides whether to keep the first value, last
value, or reject duplicates.

### 7.3 Type resolution

A literal `[exprs...]` or `{k: v, ...}` requires a *target* type `C`. Resolution happens in two phases:

1. Infer the element type(s) from the literal body. Array literals require all expressions to unify to a
   single element type `T`. Map literals infer key type `K` and value type `V` from their entries.
2. Determine the target container type `C` from context. If no context constrains the literal, the
   compiler falls back to the standard prelude types (`Array<T>` and `Map<K, V>`).

#### 7.3.1 `FromArrayLiteral`

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

#### 7.3.2 `FromMapLiteral`

Map literals use a similar interface:

```drift
interface FromMapLiteral<Key, Value> {
    static fn from_map_literal(entries: Array<(Key, Value)>) returns Self
}
```

The compiler converts `{k1: v1, ...}` into `C.from_map_literal(tmp_entries)` where `tmp_entries` is an
`Array<(K, V)>`.

### 7.4 Standard implementations

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

### 7.5 Strict mode and overrides

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

### 7.6 Diagnostics

- `[1, "two"]` → error: element types do not unify.
- `{}` without a target type → error when no default map is in scope.
- `val s: SortedSet<Int> = [1, 2, 3]` → error unless `SortedSet<Int>` implements `FromArrayLiteral<Int>`.

### 7.7 Summary

- Literal syntax is fixed in the language, but its meaning is delegated to interfaces.
- The prelude provides ready-to-use defaults (`Array`, `Map`).
- Strict mode and custom containers can override the target type.
- Errors are clear when element types disagree or no implementation is available.


## 8. Variant types (`variant`)

Drift’s `variant` keyword defines **tagged unions**: a value that is exactly one of several named alternatives (variants). Each alternative may carry its own fields, and the compiler enforces exhaustive handling when you `match` on the value.

### 8.1 Syntax

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

### 8.2 Semantics and representation

A `variant` value stores:

1. A hidden **tag** indicating which alternative is active.
2. The **payload** for that variant’s fields.

Only the active variant’s fields may be accessed. This is enforced statically by pattern matching.

### 8.3 Construction

Each variant behaves like a constructor:

```drift
val success: Result<Int64, String> = Ok(value = 42)
val failure = Err(error = "oops")            // type inference fills in `<Int64, String>`
```

Named arguments are required when a variant has multiple fields; single-field variants may support positional construction, though the explicit form is always accepted.

### 8.4 Pattern matching and exhaustiveness

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

### 8.5 Recursive data

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

### 8.6 Generics

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

### 8.7 Value semantics and equality

Variants follow Drift’s value semantics: they are copied/moved by value, and their equality/ordering derive from their payloads. Two `Result` values are equal only if they hold the same variant *and* the corresponding fields are equal.

### 8.8 Evolution considerations

- Adding a new variant is a **breaking change** because every `match` must handle it explicitly.
- Library authors should document variant additions clearly or provide fallback variants when forward compatibility matters.

Variants underpin key library types such as `Result<T, E>` and `Option<T>`, enabling safe, expressive modeling of operations with multiple outcomes.


## 9. Exceptions and error context

Drift provides structured exception handling through a single `Error` type, **exception events**, and the `^` capture modifier.  
Exceptions are **not** UI messages: they carry machine-friendly context (event name, arguments, captured locals, stack) that can be logged, inspected, or transmitted without embedding human prose.

### 9.1 Goals
Drift’s exception system is designed to:

- Use **one concrete error type** (`Error`) for all thrown failures.
- Represent failures as **event names plus arguments**, not free-form text.
- Capture **call-site context** (locals per frame + backtrace) automatically.
- Preserve a **precise, frozen ABI layout** so exceptions can propagate across Drift modules and plugins.
- Fit cleanly over a conceptual `Result<T, Error>` model for internal lowering and ABI design.
- Respect **move semantics**: `Error` is move-only and is always transferred with `e->`.

---

### 9.2 Error type and layout

```drift
struct Error {
    event: String,
    args: Map<String, String>,
    ctx_frames: Array<CtxFrame>,
    stack: BacktraceHandle
}
```

#### 9.2.1 event
Event name of the exception (`"BadArgument"`).

#### 9.2.2 args
Only event arguments, stringified via `Display.to_string()`.

#### 9.2.3 ctx_frames
Per-frame captured locals:

```drift
struct CtxFrame {
    fn_name: String,
    locals: Map<String, String>
}
```

Event args never appear here.

#### 9.2.4 stack
Opaque captured backtrace.

---

### 9.3 Exception events

#### 9.3.1 Declaring events
```drift
exception InvalidOrder(order_id: Int64, code: String)
exception Timeout(operation: String, millis: Int64)
```

Each parameter type must implement `Display`.

#### 9.3.2 Throwing
```drift
throw InvalidOrder(order_id = order.id, code = "order.invalid")
```

Runtime builds an `Error` with:
- event name
- args (stringified)
- empty ctx_frames (filled during unwind)
- backtrace

#### 9.3.3 Display requirement
Each exception argument type must implement:

```drift
trait Display {
    fn to_string(self) returns String
}
```

---

### 9.4 Capturing local context with ^

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
- Values must implement `Display`.
- Capture happens once per frame.

---

### 9.5 Throwing, catching, rethrowing

`Error` is move-only.

#### 9.5.1 Catch by event
```drift
try {
    ship(order)
} catch InvalidOrder(e) {
    log(ref e)
}
```

Matches by `error.event`.

#### 9.5.2 Catch-all + rethrow
```drift
catch e {
    log(ref e)
    throw e->
}
```

Ownership moves back to unwinder.

---

### 9.6 Internal Result<T, Error> semantics

Conceptual form:

```drift
variant Result<T, E> {
    Ok(value: T)
    Err(error: E)
}
```

Every function behaves as if returning `Result<T, Error>`; ABI lowers accordingly.

---

### 9.7 Drift–Drift propagation (plugins)

Unwinding is allowed across Drift modules/plugins as long as:
- The `Error` layout is identical.
- Same runtime/unwinder is used.

Event name + args + ctx_frames + stack fully capture portable state.

---

### 9.8 Logging and serialization
JSON example:

```json
{
  "event": "InvalidOrder",
  "args": { "order_id": "42", "code": "order.invalid" },
  "ctx_frames": [
    { "fn_name": "ship", "locals": { "record.id": "42" }},
    { "fn_name": "ingest_order", "locals": { "batch": "B1" }}
  ],
  "stack": "opaque"
}
```

---

### 9.9 Summary

- Single `Error` type.
- Event-based exceptions.
- Arguments + captured locals normalized to strings.
- Move-only errors with deterministic ownership.
- Precisely defined layout for plugin-safe unwinding.
- Semantically equivalent to `Result<T, Error>` internally.

## 10. Mutators, transformers, and finalizers

In Drift, a function’s **parameter ownership mode** communicates its **lifecycle role** in a data flow.  
This distinction becomes especially clear in pipelines (`>>`), where each stage expresses how it interacts with its input.

### 10.1 Function roles

| Role | Parameter type | Return type | Ownership semantics | Typical usage |
|------|----------------|--------------|---------------------|----------------|
| **Mutator** | `ref mut T` | `Void` or `T` | Borrows an existing `T` mutably and optionally returns it. Ownership stays with the caller. | In-place modification, e.g. `fill`, `tune`. |
| **Transformer** | `T` | `U` (often `T`) | Consumes its input and returns a new owned value. Ownership transfers into the call and out again. | `compress`, `clone`, `serialize`. |
| **Finalizer / Sink** | `T` | `Void` | Consumes the value completely. Ownership ends here; the resource is destroyed or released at function return. | `finalize`, `close`, `free`, `commit`. |

### 10.2 Pipeline behavior

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

### 10.3 Rationale

This mirrors real-world resource lifecycles:
1. Creation — ownership established.  
2. Mutation — zero or more `ref mut` edits.  
3. Transformation — optional `T → U`.  
4. Finalization — release or destruction.

Explicit parameter types make these transitions visible and verifiable at compile time.

### 10.4 RAII interaction

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

## 12. Null safety & optional values

Drift is **null-free**. There is no `null` literal. A value is either present (`T`) or explicitly optional (`Optional<T>`). The compiler never promotes `Optional<T>` to `T` implicitly.

### 12.1 Types

| Type | Meaning |
|------|---------|
| `T` | Non-optional; always initialized. |
| `Optional<T>` | Possibly empty; either a value or nothing. |

### 12.2 Construction

```drift
val present: Optional<Int64> = Optional.of(42)
val empty: Optional<Int64> = Optional.none()
```

### 12.3 Interface

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

### 12.4 Control flow

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

### 12.5 Parameters & returns

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

### 12.6 Ownership

`if_present` borrows (`ref T`) by default. No move occurs unless you explicitly consume `T` inside the block.

### 12.7 Diagnostics (illustrative)

- **E2400**: cannot assign `Optional.none()` to non-optional type `T`.
- **E2401**: attempted member/method use on `Optional<T>` without `map`/`unwrap`/`if_present`.
- **E2402**: `unwrap()` on empty optional.
- **E2403**: attempted implicit conversion `Optional<T>` → `T`.

### 12.8 End-to-end example

```drift

### 12.9 Tuple structs & tuple returns

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
## 13 Traits and compile-time capabilities

(*Conforms to Drift Spec Rev. 2025-11 (Rev 4)*)  
(*Fully consistent with the `require` + `is` syntax finalized in design discussions.*)

---

### 13.1 Overview

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

### 13.2 Defining traits

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

### 13.3 Implementing traits

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

### 13.4 Type-level trait requirements (`require`)

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

### 13.5 Function-level trait requirements

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

### 13.6 Trait guards (`if T is TraitName`)

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

### 13.7 Trait expressions (boolean logic)

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

### 13.8 Trait dependencies (traits requiring traits)

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

### 13.9 RAII and the `Destructible` trait

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

### 13.10 Overloading and specialization by trait

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

### 13.11 Complete syntax summary

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

### 13.13 Thread-safety marker traits (`Send`, `Sync`)

Certain libraries (notably `std.concurrent`) rely on two marker traits that express thread-safety:

- **`Send`** — values of a type implementing `Send` may be moved from one thread to another.
- **`Sync`** — shared references (`ref T`) to a type implementing `Sync` may be shared across threads simultaneously.

All primitives and standard library containers implement these traits when safe.

```drift
trait Send { }
trait Sync { }
```

Implementing `Send` means a value may be moved to another thread. Implementing `Sync` means shared references may be used concurrently. A struct may opt into `Send` if all of its fields are `Send`; similarly for `Sync`. Types that manage thread-affine resources (e.g., OS handles that must stay on one thread) simply omit these traits and remain single-threaded.

The concurrency chapter (Section 16.6) references these bounds when describing virtual-thread movement and sharing.

---


### 13.14. Design Rationale

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
## 14. Interfaces & dynamic dispatch

Drift supports **runtime polymorphism** through *interfaces*.  
Interfaces allow multiple **different concrete types** to be treated as one unified abstract type at runtime.  
This is the dynamic counterpart to compile‑time polymorphism provided by *traits*.

**In short:**

- **Traits** describe *capabilities* and enable *compile‑time specialization*.
- **Interfaces** describe *runtime object shapes* and enable *dynamic dispatch*.

Traits are *not* types; interfaces *are* types.  
Both systems integrate cleanly with Drift’s ownership, RAII, and borrowing rules.

---

### 14.1 Interface definitions

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
- The method signatures inside an interface show the receiver type explicitly (`self: ref OutputStream`).

### 14.2 Receiver rules (`self`)

Drift differentiates between **methods** (eligible for dot-call syntax) and **free functions**.

- **Only functions defined inside an `implement Type { ... }` block become methods.**
- Inside such a block, the first parameter **must** be spelled `self`, with an explicit mode:
  - `self` → pass by value
  - `ref self` → shared borrow
  - `ref mut self` → exclusive/mutable borrow
  - (future) `move self` → consuming receiver
- The receiver’s type is implied by the `implement` header, so you never annotate it (`ref self`, not `self: ref File`).
- Outside an `implement` block every function is a free function. A free function may take any parameters (including an explicit `ref File`), but it is invoked with ordinary call syntax (`translate(ref point, 1, 2)`), not `point.translate(...)`.

Example:

```drift
struct Point { x: Int64, y: Int64 }

implement Point {
    fn move_by(ref mut self, dx: Int64, dy: Int64) returns Void {
        self.x += dx
        self.y += dy
    }
}

fn translate(ref p: Point, dx: Int64, dy: Int64) returns Void {
    p.x += dx
    p.y += dy
}

point.move_by(1, 2)       // method call (inside `implement`)
translate(ref point, 3, 4) // free function call
```

This rule set makes the receiver’s ownership mode explicit and prevents implicit, C++-style magic receivers.

---

### 14.3 Implementing interfaces

A concrete type implements an interface through an `implement` block:

```drift
struct File {
    fd: Int64
}

implement OutputStream for File {
    fn write(ref self, bytes: Bytes) returns Void {
        sys_write(self.fd, bytes)
    }

    fn writeln(ref self, text: String) returns Void {
        self.write((text + "\n").to_bytes())
    }

    fn flush(ref self) returns Void {
        sys_flush(self.fd)
    }
}
```

Rules:

1. All interface functions must be provided.
2. Method signatures begin with an explicit receiver (`ref self` here); the type (`File`) is implied by the `implement` header.
3. A type may implement multiple interfaces.
4. Implementations may appear in any module.

---

### 14.4 Using interface values

Interfaces may be used anywhere that types may appear.

#### 14.4.1 Parameters

```drift
fn write_header(out: OutputStream) returns Void {
    out.writeln("=== header ===")
}
```

#### 14.4.2 Return values

```drift
fn open_log(path: String) returns OutputStream {
    var f = File.open(path)
    return f      // implicit upcast: File → OutputStream
}
```

#### 14.4.3 Locals

```drift
var out: OutputStream = std.console.out
out.writeln("ready")
```

#### 14.4.4 Heterogeneous arrays

```drift
var sinks: Array<OutputStream> = []
sinks.push(open_log("app.log"))
sinks.push(std.console.out)
```

Each element may be a different type implementing the same interface.

---

### 14.5 Dynamic dispatch semantics

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

### 14.6 Interfaces vs traits

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

### 14.7 Shape example

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
    fn area(ref self) returns Float64 {
        return 3.14159265 * self.radius * self.radius
    }
}

implement Shape for Rect {
    fn area(ref self) returns Float64 {
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

### 14.8 Ownership & RAII for interface values

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

### 14.9 Multiple interfaces

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

### 14.10 Interfaces + traits together

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

### 14.11 Error handling across interfaces

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

### 14.12 Summary

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

## 15. Memory model

This chapter defines Drift's rules for value storage, initialization, destruction, and dynamic allocation. The goal is predictable semantics for user code while relegating low-level memory manipulation to the standard library and `lang.abi`.

Drift deliberately hides raw pointers, pointer arithmetic, and untyped memory. Those operations exist only inside sealed, `@unsafe` library internals. User-visible code works with typed values, references, and safe containers like `Array<T>`.

### 15.1 Value storage

Every sized type `T` occupies `size_of<T>()` bytes. Sized types include primitives, structs whose fields are all sized, and generic instantiations where each argument is sized. These values may live in locals, struct fields, containers, or temporaries. The compiler chooses the actual storage (registers vs stack) and that choice is unobservable.

#### 15.1.1 Initialization & destruction

- A value must be initialized exactly once before use.
- A value must be destroyed exactly once when it leaves scope or is overwritten.
- Types with destructors run them during destruction; other types are dropped with no action.

#### 15.1.2 Uninitialized memory

User code never manipulates uninitialized memory. Library internals rely on two sealed helpers:

- `Slot<T>` — typed storage for one `T`.
- `Uninit<T>` — marker used to construct a `T` inside a slot.

Only standard library `@unsafe` code touches these helpers.

### 15.2 Raw storage

`lang.abi` defines an opaque `RawBuffer` representing raw bytes that are not yet interpreted as typed values. Only allocator intrinsics can produce or consume a `RawBuffer`; user code cannot observe its address or layout. Growable containers use `RawBuffer` to reserve contiguous storage for multiple elements of the same type.

### 15.3 Allocation & deallocation

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

### 15.4 Layout of contiguous elements

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

### 15.5 Growth of containers

#### 15.5.1 Overview

Growable containers track both `len` (initialized elements) and `cap` (reserved slots). When `len == cap`, they obtain a larger `RawBuffer` and move existing elements—this is capacity growth.

#### 15.5.2 Array layout

```drift
struct Array<T> {
    len: Int      // initialized elements
    cap: Int      // reserved slots
    buf: RawBuffer
}
```

Invariant: indices `0 .. len` are initialized; `len .. cap` are uninitialized slots ready for construction. Growth occurs before inserting when `len == cap`.

#### 15.5.3 Growth algorithm

```
fn grow<T>(ref mut self: Array<T>) @unsafe {
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

#### 15.5.4 Moving elements

Initialized elements move slot-by-slot:

```
for i in 0 .. self.len {
    src = slot_at<T>(old_buf, i)
    dst = slot_at<T>(new_buf, i)
    move_slot_to_slot(src, dst)
}
```

`slot_at` and `move_slot_to_slot` are sealed helpers that perform placement moves without exposing raw pointers to user code.

#### 15.5.5 Initializing new slots

After growth, indices `len .. cap` become `Uninit<T>` slots. Public methods (e.g., `push`, `spare_capacity_mut`) safely initialize them.

### 15.6 Stability & relocation

Because `realloc` may relocate a `RawBuffer`, any references, slices, or views derived from a container become invalid after growth. Users must treat such views as ephemeral. Only the container itself may assume addresses remain stable between growth events.

### 15.7 Stack vs dynamic storage

Drift does not expose stack vs heap distinctions. Local variables and temporaries are compiler-managed; growable containers always use the allocator APIs above. This abstraction lets the backend optimize placement without affecting semantics.

### 15.8 Summary

The memory model rests on:

1. No raw pointers in user code.
2. Typed storage abstractions (`Slot<T>`, `Uninit<T>`).
3. Strict init/destroy rules.
4. All dynamic allocation routed through `lang.abi`.
5. Predictable contiguous container semantics with explicit growth.
6. Backend freedom for placing locals/temporaries.

These rules scale to arrays, strings, maps, trait objects, and future higher-level abstractions using the same mechanisms.

---

## 16. Concurrency & virtual threads

Drift offers structured, scalable concurrency via **virtual threads**: lightweight, stackful execution contexts scheduled on a pool of operating-system carrier threads. Programmers write synchronous-looking code without explicit `async`/`await`, yet the runtime multiplexes potentially millions of virtual threads.

### 16.1 Virtual threads vs carrier threads

| Layer | Meaning | Created by | Cost | Intended users |
|-------|---------|------------|------|----------------|
| Virtual thread | Drift-level lightweight thread | `std.concurrent.spawn` | Very cheap | User code |
| Carrier thread | OS thread executing many virtual threads | Executors | Expensive | Runtime |

Virtual threads borrow a carrier thread while running, but yield it whenever they perform a blocking operation (I/O, timer wait, join, etc.).

### 16.2 `std.concurrent` API surface

Drift’s standard concurrency module exposes straightforward helpers:

```drift
import std.concurrent as conc

val t = conc.spawn(fn() returns Int {
    return compute_answer()
})

val ans = t.join()
```

Spawn operations return a handle whose `join()` parks the caller until completion. Joining a failed thread returns a `JoinError` encapsulating the thrown `Error`.

#### 16.2.1 Custom executors

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

#### 16.2.2 Structured concurrency

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

### 16.3 Executors and policies

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

### 16.4 Blocking semantics

Virtual threads behave as though they block, but the runtime parks them and frees the carrier thread:

- I/O operations register interest with the reactor and park the virtual thread.
- Timers park until their deadline elapses.
- `join()` parks the caller until the child completes.
- When the event loop signals readiness, the reactor unparks the waiting virtual thread onto a carrier.

### 16.5 Reactors

Drift ships with a shared default reactor (epoll/kqueue/IOCP depending on platform). Advanced users may supply custom reactors or inject them into executors for specialized workloads.

### 16.6 Virtual thread lifecycle

- Each virtual thread owns an independent call stack; RAII semantics run normally when the thread exits.
- `join()` returns either the thread’s result or a `JoinError` capturing the propagated `Error`.
- Parking/unparking is transparent to user code.
- `Send`/`Sync` trait bounds govern which values may move across threads or be shared by reference.

### 16.7 Intrinsics: `lang.thread`

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

### 16.8 Scoped virtual threads

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

### 16.9 Interaction with ownership & memory

- Moves between threads require `Send`; shared borrows require `Sync`.
- Destructors run deterministically when each virtual thread ends, preserving RAII guarantees.
- Containers backed by `RawBuffer` (`Array`, `Map`, etc.) behave identically on all threads.

### 16.10 Summary

- Virtual threads deliver the ergonomics of synchronous code with the scalability of event-driven runtimes.
- Executors configure carrier thread pools, queues, and timeout policies.
- Blocking APIs park virtual threads instead of OS threads.
- Reactors wake parked threads when I/O or timers fire.
- Structured concurrency scopes offer deterministic cancellation and cleanup.
- Only a handful of `lang.thread` intrinsics underpin the model; user-facing code resides in `std.concurrent`.

## 17. Pointer-free surface and ABI boundaries

Drift deliberately keeps raw pointer syntax out of the language surface. Low-level memory manipulation and FFI plumbing are funneled through sealed standard-library modules so typical programs interact with typed handles rather than `*mut T` tokens.

### 17.1 Policy: no raw pointer tokens

- No `*mut T` / `*const T` syntax exists in Drift.
- User-visible pointer arithmetic and casts are forbidden.
- Untyped byte operations live behind `@unsafe` internals such as `lang.abi` and `lang.internals`.

### 17.2 Slots and uninitialized handles

To enable placement construction without exposing addresses, the runtime uses opaque helpers (see Section 15.1.2):

- `Slot<T>` — a typed storage location capable of holding one `T`.
- `Uninit<T>` — a marker denoting “not constructed yet.”

Internal APIs operate on these handles:

```drift
slot.write(value: T)         // move/copy into the slot
slot.emplace(args…)         // construct in place from arguments
slot.assume_init() @unsafe  // produce a normal reference once initialized
```

Typical code never manipulates the underlying addresses—only these safe handles.

### 17.3 Guarded builders for container growth

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

### 17.4 `RawBuffer` internals

Containers rely on `lang.abi::RawBuffer` for contiguous storage, but the public surface offers only safe operations:

```drift
struct RawBuffer<T> { /* opaque */ }

fn capacity(ref self) returns Int
fn slot_at(ref self, i: Int) returns Slot<T> @unsafe
fn reallocate(ref mut self, new_cap: Int) @unsafe
```

`Array<T>` and similar types use these hooks internally; ordinary programs never touch the raw bytes.

### 17.5 FFI via `lang.abi`

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

### 17.6 Unsafe modules (`lang.internals`)

Truly low-level helpers (`Slot<T>`, unchecked length changes, raw buffer manipulation) live in sealed modules such as `lang.internals`. Importing them requires explicit opt-in (feature flag + `@unsafe` annotations). Most applications never import these modules; the standard library and advanced crates do so when implementing containers or FFI shims.

### 17.7 Examples

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

### 17.8 Summary

- The surface language never exposes raw pointer syntax or arithmetic.
- Constructors, builders, and slices provide placement-new semantics without revealing addresses.
- FFI always flows through `lang.abi` with opaque handles.
- Unsafe helpers live behind `lang.internals` and require explicit opt-in.
- Programmers still achieve zero-cost interop and efficient container implementations while keeping the foot-guns sealed away.

---

---

## 18. Signed modules and DMIR

Drift distributes code as **digitally signed module packages (DMPs)** built around a canonical, target-independent representation called **DMIR** (Drift Module Intermediate Representation). Signing DMIR rather than backend objects guarantees that every user receives the same typed semantics, regardless of platform or compiler optimizations. This matters because:

- modules often travel through untrusted mirrors, caches, or registries; signatures ensure they weren’t tampered with en route.
- reproducible canonical IR decouples semantic identity from backend artifacts, so verification survives compiler/platform differences.
- dependency manifests can pin digests/signers to prevent supply-chain attacks.

### 18.1 Position in the pipeline

```
source → AST → HIR → DMIR (canonical) → [sign] → MIR/backend → object/JIT
```

DMIR is the authoritative checkpoint. Later transformations (optimizations, codegen) do not affect the signature.

### 18.2 Canonical DMIR contents

DMIR stores the typed, desugared module with all names resolved:

- Top-level declarations (functions, structs, interfaces, traits, constants).
- Canonical function bodies (control flow normalized, metadata stripped).
- Canonical literal encodings (UTF-8 strings, LEB128 integers, IEEE-754 floats).
- Deterministic ordering by fully-qualified name.
- No timestamps, file paths, environment data, or formatting trivia.

Each DMIR block carries an independent version number (`dmir_version`).

### 18.3 Module package layout

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

### 18.4 Signatures and verification

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

### 18.5 Security properties

- Repository compromises cannot forge modules without the private key.
- Canonicalization ensures reproducible builds and stable signatures.
- DMIR versioning decouples language evolution from compiler releases.
- Optional source does not influence verification, so audits cannot poison signatures.

### 18.6 Future extensions

Potential enhancements include transparency logs, certificate-based hierarchies, revocation lists, and dual-signature modes.

Signed DMIR gives Drift a portable, semantically precise unit of distribution while keeping authenticity verifiable on every machine.

*Note:* The exact signing/verification scheme (PGP vs Ed25519, cert hierarchies, revocation policies) is still under design and will be finalized before the DMP format is stabilized. The structure here captures intent; cryptographic options may evolve.

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


## Appendix B — Grammar (EBNF excerpt)

*(Trait/`implement`/`where` grammar is summarized in Appendix C.)*


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


## Appendix C — Trait Grammar Notes

Traits and implementations use the same `where` syntax as functions. Grammar sketch:

```ebnf
TraitDef   ::= "trait" Ident TraitParams? TraitWhere? TraitBody
TraitWhere ::= "where" TraitClause ("," TraitClause)*
TraitClause ::= "Self" "has" TraitExpr

Implement  ::= "implement" Ty "for" Ty TraitWhere? TraitBody

TraitExpr  ::= TraitTerm ( ("and" | "or") TraitTerm )*
TraitTerm  ::= "not"? Ident | "(" TraitExpr ")"
```

These forms defer to the existing `where` machinery described in Section 13.

### End of Drift Language Specification
