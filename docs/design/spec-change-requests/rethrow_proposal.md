# Drift proposal: `rethrow;` statement

This proposal introduces a dedicated `rethrow;` statement for propagating the *currently caught* `Error` value out of a `catch` block, without requiring an expression payload.

## Goals

- Make “propagate the same error upward” explicit and unambiguous.
- Avoid `throw e` (where `e: Error`) as the idiom for rethrowing.
- Improve static reasoning and diagnostics: `rethrow;` is only valid in a catch context.
- Preserve the existing runtime model:
  - error identity is `event_code = hash_v1(utf8(event_fqn))`
  - `Error` carries `event_fqn` string label
  - attrs are declared named fields only; no special keys

Non-goals (v1):
- No exception “wrapping” construct baked into `rethrow`.
- No implicit mutation of the error object.
- No cross-function implicit “current exception” state (no TLS magic).

---

## Language specification changes

### Syntax

Add a statement form:

```
rethrow ;
```

### Static semantics

- `rethrow;` is only permitted **lexically inside a `catch` arm body**.
- Using `rethrow;` outside any catch arm is a compile-time error.

### Dynamic semantics

Within a catch arm, `rethrow;` propagates the *exact same* caught `Error` value:
- same `event_code`
- same `event_fqn`
- same attrs
- same context frames/stack

It is equivalent to “throw the error bound to this catch arm,” but expressed without an identifier.

### Interaction with `try/catch`

- `rethrow;` behaves like a normal throw in terms of control flow:
  - If there is an outer enclosing try/catch in the same function, it routes to the nearest outer try dispatch.
  - Otherwise, it exits the function as `FnResult.Err(error)` (or the language’s can-throw propagation mechanism).

### Diagnostics

- Error message for invalid placement:
  - `rethrow is only valid inside a catch block`

---

## Compiler implementation plan (lang2)

### Stage0 AST

Add:

- `RethrowStmt(loc: Span)`

Parser: recognize `rethrow;` as a statement.

Adapter: map to stage0 `RethrowStmt`.

### Stage1 HIR

Add:

- `HRethrow(loc: Span = Span())` (statement node)

Lowering (AstToHIR):
- `RethrowStmt` -> `HRethrow`

Normalization passes:
- If you have HIR normalization, ensure it preserves `HRethrow` and threads spans.

### Stage2 HIR→MIR lowering

#### Key idea

When lowering an `HTry`, you already allocate a hidden local:

- `error_local = "__try_err..."`

This local holds the thrown `Error` that is being dispatched.

For `rethrow;`, you want access to the error value *in the current catch arm*.

#### Minimal mechanism

When entering a catch arm block, you already emit:

- `err_again = LoadLocal(error_local)`

Do one additional store for rethrow purposes:

- introduce a per-try hidden “current catch error” local:
  - `catch_error_local = "__catch_err..."` (unique per try lowering)
- on catch block entry, store:
  - `StoreLocal(catch_error_local, err_again)`

Then implement `HRethrow` lowering as:

1. `err = LoadLocal(catch_error_local)`
2. if inside an outer try context:
   - store into outer try’s `error_local`
   - `Goto(outer_dispatch)`
3. else:
   - `ConstructResultErr(err)`
   - `Return`

This is identical to your existing “throw existing Error value” propagation path, just with a dedicated syntax and no expression.

#### Data structure changes

Extend the try-stack context to carry the catch-local used for rethrow:

- `_TryCtx` gains:
  - `catch_error_local: str`

Push it when entering an `HTry` lowering, so nested tries behave correctly:
- A `rethrow;` inside an inner catch rethrows to the *outer* try context after the inner context has been popped (you already pop before lowering catch blocks).

#### Lowering rules

- `HRethrow` is only legal when a `catch_error_local` is active.
- Stage2 should still defend and raise if it appears outside catch.

### Type checker changes

Add a catch-context boolean (or stack depth counter) while walking HIR:

- On `HTry`, when checking each `HCatchArm.block`, set `in_catch = True` for the body walk.
- For `HRethrow`:
  - if not `in_catch`: diagnostic error with span
  - else: mark function as “may throw” (because it propagates an error out of the catch unless caught by an outer try)

---

## Testing plan

### Parser tests
- parse `rethrow;` into `RethrowStmt`
- span coverage

### Stage1 tests
- AST→HIR: `RethrowStmt` -> `HRethrow`

### Checker tests
- `rethrow;` outside catch => diagnostic includes correct span
- `rethrow;` inside catch => allowed

### Stage2 MIR tests

1) `try { throw m:A } catch m:A { rethrow; }`
- should propagate out (no outer try), not loop back into same try

2) nested:
```
try {
  try { throw m:A } catch m:A { rethrow; }
} catch m:A { ... }
```
- inner rethrow should route to outer try dispatch

### E2E test
- Program that throws, catches, logs attr, and `rethrow;` to an outer catch, verifying outer handler runs.

---

## Spec text to drop into Chapter 14

Add `rethrow;`:

- allowed only inside catch blocks
- rethrows the current caught Error unchanged
- matching remains by `event_code`; `event_fqn` is for labeling/logging only

---

## Compatibility and migration

- Existing code that did `throw e` where `e: Error` can migrate to `rethrow;` inside catch blocks.
- Keep `throw <Error>` permitted (for now) for:
  - try-result sugar lowering (`unwrap_err`)
  - advanced forwarding utilities
- Recommend `rethrow;` as the common “same error” case.
