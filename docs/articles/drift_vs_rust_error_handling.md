# Error Handling in Drift vs Rust — A Guide for Rustaceans

Rust developers coming to Drift will immediately notice something fundamental: **Drift is an exception‑driven language**, but not in the Java/C++ sense and not in conflict with strong typing. Instead, Drift implements **structured exception events** with automatic context capture, a frozen ABI layout, and deterministic ownership. Rust, by contrast, builds nearly all recoverable error handling on the explicit `Result<T, E>` type and treats unwinding as exceptional.

This guide explains how the two philosophies diverge and what Rust programmers should expect when reading or writing Drift.

---

## 1. Structured Exception Events (Drift) vs Typed Results (Rust)

### Drift
Drift uses *exception events* for recoverable errors:

```drift
exception InvalidOrder(order_id: Int64)

fn ship(order: Order) returns Void {
    if !verify(order) {
        throw InvalidOrder(order_id = order.id)
    }
}
```

Programmers do **not** write functions returning `Result<T, E>`. The throwing behavior is implicit, and callers do not see an error type in the signature.

Under the hood, Drift models function outcomes as:

```
Result<T, Error>  // implicit, compiler‑generated
```

…but this representation is **never exposed in the surface language**.

### Rust
In Rust, recoverable errors use explicit types:

```rust
fn ship(order: Order) -> Result<(), InvalidOrder> { ... }
```

The signature forces the caller to handle or propagate errors with `?`.

### Rationale
- **Drift** optimizes for readability and reduces boilerplate in success‑path code.
- **Rust** optimizes for explicitness and type‑driven error flow clarity.

---

## 2. Drift Errors Are Structured Diagnostics, Not Messages

Rust errors usually implement `Display` and often include human‑oriented messages.

Drift exceptions are not UI messages. They are structured diagnostics:

```drift
struct Error {
    event: String,
    args: Map<String, String>,
    ctx_frames: Array<CtxFrame>,
    stack: BacktraceHandle
}
```

- `event` is the exception name.
- `args` are the exception parameters.
- `ctx_frames` contain per‑frame captured locals.
- `stack` is an opaque backtrace handle.

No English text is included unless explicitly provided as an argument.

### Rationale
Drift draws a strict line between:
- **diagnostics** (machine‑friendly)
- **user‑visible messages** (handled elsewhere)

Rust leaves this distinction to developers and libraries.

---

## 3. Automatic Context Capture (Drift) vs Manual Context (Rust)

Rust requires manual context via `.context()` or crates like `anyhow`, `eyre`, or `tracing`.

Drift captures `^`-annotated locals automatically:

```drift
val ^record_id: String as "record.id" = record["id"]
```

If an exception crosses this frame, Drift records:

```json
{
  "fn_name": "ingest_record",
  "locals": { "record.id": "42" }
}
```

Combined with arguments and stack trace, every Drift exception is a rich diagnostic packet.

### Rationale
- Drift wants deterministic, complete, structured diagnostics with no developer forgetfulness.
- Rust lets developers choose their own diagnostic strategy.

---

## 4. Cross‑Module Propagation in Drift vs Avoidance in Rust

Rust’s unwinding mechanism is intentionally *not* designed to cross FFI/plugin boundaries. Panics should not be used for recoverable errors.

Drift explicitly supports unwinding across Drift‑compiled modules and dynamic plugins because the `Error` ABI is frozen:

- All thrown errors share the same layout.
- Errors are move‑only and never copied.
- The unwinder transfers a stable error handle across modules.

### Rationale
Drift’s module/plugin system requires:
- ABI‑stable error format
- shared unwinder semantics
- predictable cross‑boundary diagnostics

Rust avoids this because explicit `Result<T, E>` already solves cross‑crate error flow safely.

---

## 5. Error Philosophy: Unified Diagnostics (Drift) vs Explicit Types (Rust)

### Drift
- One canonical `Error` type for all exceptions.
- Exception events define structure, not runtime types.
- Context capture and backtrace built in.
- Cross‑module behavior guaranteed.

### Rust
- Many error types, each crate defines its own.
- Developer chooses how much structure to include.
- Backtrace optional, context optional.
- No stable cross‑module error ABI.

### Rationale
Drift centers predictable diagnostics for large distributed/plugin environments.  
Rust centers developer explicitness and strong typing.

---

## 6. Ergonomics: Success‑Path Clarity vs Type‑Path Clarity

Rust:

```rust
let data = load()?;
let conf = parse(data)?;
Ok(conf)
```

Drift:

```drift
val data = load()    // may throw
val conf = parse(data)
return conf
```

Rust emphasizes type‑visible error flow.  
Drift emphasizes readability of the success path.

Both approaches are valid—and optimized for different domains.

---

## 7. Summary Table

| Feature | Drift | Rust |
|--------|-------|------|
| Recoverable errors | **Exceptions** | **Result<T, E>** |
| Error type | Single structured `Error` | Arbitrary `E` |
| Context | Built‑in automatic | Manual / library-based |
| Backtrace | Always captured | Optional |
| Cross‑module behavior | Supported | Discouraged |
| Diagnostic focus | Machine‑friendly | Human or mixed |
| UI messages | Not part of exceptions | Often included |
| Boilerplate | Minimal | Higher, but explicit |
| Ownership | Move‑only exceptions | Move‑only error values |
| Borrowing syntax | `&T` / `&mut T`, lvalue calls auto‑borrow, no borrowing from rvalues | `&T` / `&mut T`, explicit at call sites; borrows can be taken from temps |

---

## 8. Why Drift Chose This Path

1. **Plugin and modular runtime architecture** require ABI‑stable diagnostics.
2. **Readable code** without boilerplate is a priority.
3. **Automatic context** ensures high‑fidelity error reports.
4. **Structured diagnostics** enable superior log/telemetry tooling.
5. **Clear separation** between exception diagnostics and UI messages.

---

## 9. Closing Thoughts

Rust and Drift reflect different priorities:

- Rust: *explicitness, strong typing, and deliberate error flow*
- Drift: *structured diagnostics, ergonomics, and cross‑module consistency*

For a Rust developer entering Drift, the best mental model is:

> “Drift exceptions are structured, automatically enriched, fail‑fast `Result<T, Error>`—but without the syntactic noise and without UI text.”

Once you adopt this mindset, Drift’s approach becomes predictable and clean.
