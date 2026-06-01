# Error handling in Drift vs Rust — a guide for Rustaceans

Rust developers coming to Drift will notice something fundamental right away: **Drift exposes exceptions at the surface**, but its *semantics* are value‑based and disciplined in a way that avoids most traditional exception pitfalls.

This document explains how Drift’s error model works, how it compares to Rust’s `Result<T, E>`‑centric design, and why the differences are intentional.

---

## 1. One semantic model: value‑based errors

At the semantic level, Drift has exactly **one** error model:

> **Every can‑throw computation produces a `Result<T, Error>`.**

This is *not* surface syntax, but it *is* the meaning of the language.

- `throw`, `try`, and `catch` are **surface constructs**.
- Internally and at module boundaries, failures are values.
- The compiler/runtime may **implement propagation via unwinding** *inside the static Drift world*.

Unwinding is an **implementation strategy**, not a second semantic model.

This mirrors Rust’s core idea (errors are values), while allowing more ergonomic syntax.

---

## 2. Surface syntax: exceptions vs explicit `Result`

### Drift (surface)

```drift
exception InvalidOrder(order_id: Int64)

fn ship(order: Order) -> Void {
    if !verify(order) {
        throw InvalidOrder(order_id = order.id)
    }
}
```

- No `Result<T, E>` in the signature.
- Failure is implicit in the control flow.
- The function is *can‑throw* by effect, not by type.

### Rust (surface)

```rust
fn ship(order: Order) -> Result<(), InvalidOrder> {
    if !verify(order) {
        return Err(InvalidOrder { order_id: order.id });
    }
    Ok(())
}
```

Rust makes failure **type‑visible** everywhere.

### Key difference

- **Rust**: error flow is explicit and type‑checked at every call site.
- **Drift**: error flow is implicit, but semantically equivalent to `Result<T, Error>`.

---

## 3. A single, canonical `Error` type (Drift)

Unlike Rust (where `E` is arbitrary), Drift has exactly **one runtime error representation**:

```drift
struct Error {
    event_code: Uint64
    event_fqn: String
    params_json: String      // canonical JSON object: declared field values
    context_json: String     // canonical JSON array: `^`-captured frames
    stack: BacktraceHandle
}
```

> **Migration note (Slice 7c-3, ABI 14, 2026-05-06).** Earlier drafts of this article showed `attrs: Map<String, DiagnosticValue>` and a separate `ctx_frames` array. Both were retired by the DV→JSON migration (Slices 7a–7c-3). The shape above is the live ABI 14 surface; see `doc/design/drift-lang-abi.md` §2 for the helper contract.

Key properties:

- **Single ABI‑stable layout**
- **Move‑only ownership**
- **Event identity decoupled from type layout**
- **Structured, machine‑friendly diagnostics**

Exception *events* define structure (fields), not runtime object hierarchies.

This avoids the classloader / inheritance / ABI issues common in Java and C++.

---

## 4. Structured diagnostics, not messages

Drift exceptions are **not UI messages**.

They carry:
- an event identity
- typed attributes
- captured locals
- a backtrace

Human‑readable messages are produced *later* by logging, formatting, or UI layers.

Rust often mixes diagnostics and presentation by convention (`Display`, `Error::source()`), but the language does not enforce a separation.

Drift does.

---

## 5. Automatic context capture vs manual context

### Drift

```drift
val ^record_id: String as "record.id" = record["id"]
```

If an exception crosses this frame, the runtime records:

```json
{
  "fn_name": "ingest_record",
  "locals": { "record.id": "42" }
}
```

- Capture is **opt‑in but automatic**.
- Captured values are typed diagnostics.
- Forgetfulness is impossible once annotated.

### Rust

Context is added manually, typically via libraries:

```rust
parse(input).context("while parsing record")?
```

Both approaches are valid; Drift optimizes for **completeness and determinism**.

---

## 6. Propagation and unwinding boundaries

### Drift

- Unwinding **may** be used internally.
- It is allowed **only across static Drift modules** that:
  - share the same runtime
  - share the same `Error` layout
  - agree on the can‑throw ABI

**Unwinding must never cross FFI or OS‑level shared library boundaries.**

At those boundaries, errors are converted to values (`Result`, error codes, etc.).

### Rust

- `panic` is explicitly *not* for recoverable errors.
- Unwinding across FFI is restricted or forbidden.
- Recoverable errors always flow as values.

Drift’s rules are stricter than C++ and closer to Rust—just with more surface sugar.

---

## 7. Module interfaces and ABI discipline (Drift)

Drift pins a crucial rule:

> **Every exported function is a can‑throw ABI boundary.**

Consequences:

- Exported functions always use `{T, Error*}` / `Error*` at the ABI.
- Callers must assume failure is possible.
- Internal helpers may optimize aggressively, but **never leak that choice**.

This is what keeps cross‑module propagation sound.

Rust does not need this rule because `Result<T, E>` is already explicit at every boundary.

---

## 8. Ergonomics trade‑off

### Rust

```rust
let data = load()?;
let conf = parse(data)?;
Ok(conf)
```

### Drift

```drift
val data = load()
val conf = parse(data)
return conf
```

- Rust optimizes for **type‑path clarity**.
- Drift optimizes for **success‑path readability**.

Both preserve correctness; they choose different defaults.

---

## 9. Comparison summary

| Aspect | Drift | Rust |
|------|------|------|
| Semantic model | Value‑based `Result<T, Error>` | Value‑based `Result<T, E>` |
| Surface syntax | Exceptions | Explicit results |
| Error type | Single canonical `Error` | Arbitrary `E` |
| Context capture | Automatic (opt‑in) | Manual |
| Backtrace | Always | Optional |
| Cross‑module propagation | Allowed (static world only) | Via values only |
| FFI safety | No unwinding across FFI | Same rule |
| Boilerplate | Minimal | Explicit |

---

## 10. Mental model for Rust developers

The correct mental model is:

> **Drift exceptions are structured, automatically enriched `Result<T, Error>` values with ergonomic syntax — not unchecked control‑flow escapes.**

If you keep that model in mind, Drift’s approach remains predictable, analyzable, and safe — even though it *looks* like a traditional exception system on the surface.

---

## 11. Why Drift chose this design

1. **Large modular systems** need ABI‑stable diagnostics.
2. **Plugin and package distribution** require predictable error propagation.
3. **Automatic context** produces better telemetry with less effort.
4. **Ergonomics matter** for non‑toy systems.
5. **Value‑based semantics** keep the model analyzable and optimizable.

The result is a hybrid that keeps Rust’s semantic rigor while reclaiming some of the ergonomics that traditional exception systems aimed for—but rarely delivered cleanly.

