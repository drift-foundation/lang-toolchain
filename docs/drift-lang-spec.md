# Drift Language Specification
### Revision 2025-11 (Rev 2) — Ownership, Mutability, Exceptions, and Deterministic Resource Management

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

Each function frame can contribute contextual data via the `^` capture syntax.

### 11.2 Capturing local context

```drift
let ^input: String as "record.field" note "start date" = msg["startDate"]
fn parse_date(^s: String) : Date {
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

- Each captured variable (`^x`) adds its name, optional alias, note, and sample size to the current frame context.
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
