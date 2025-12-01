## Exception Constructors — Work Progress

This document tracks the full implementation of **user-defined exception construction** in Drift.
The goal is:

> `throw InvalidPayload(payload = line)`
> must be treated as a **first-class exception constructor**, not as an unknown function call.

This work touches checker, lowering, SSA, codegen, and tests — so progress is staged carefully.

---

# 1. Motivation

Currently, a program like:

```drift
exception InvalidPayload(payload: String)

fn ingest(line: String) {
  if line == "" {
    throw InvalidPayload(payload = line)
  }
}
```

produces the diagnostic:

```
unknown function 'InvalidPayload'
```

This is incorrect. `InvalidPayload` is not a function — it is a declared exception type.

The language already has:

* deterministic `event_code` assignment per exception,
* Error/Exception model for try/catch dispatch,
* SSA `ErrorEvent` projection,
* throw propagation implemented.

But it **lacks the front-end and lowering logic** for constructing user-defined exceptions.

This project corrects that.

---

# 2. Plan / Phases

We break the work into tight, sequential phases.

---

## Phase 1 — Checker: register and type-check exception constructors

### Goals

* Add `ExceptionDecl` to the checker symbol table.
* Resolve `InvalidPayload(...)` inside `throw` as an **exception constructor call**, not a function call.
* Type-check named fields against the declared exception fields.
* Produce a semantic `ThrowException` IR node that carries:

  * event name,
  * event_code,
  * field/value mapping.

### Deliverables

* Update `checker.py` exception table population.
* Modify call resolution inside `throw` branch:

  * Look up exception names.
  * Reject if fields mismatch.
  * Construct a typed exception constructor node.
* Unit tests:

  * Positive: well-typed constructor.
  * Negative: unknown field, missing field, extra field.
  * Negative: using an exception as a function (should remain an error).

---

## Phase 2 — Lowering: lower exception constructor to SSA "Error" creation

### Goals

Lower a `ThrowException(exc, field_args...)` to an SSA form that:

* Constructs an `Error` value carrying:

  * `event_code`,
  * any field data (payload),
  * location/metadata where possible.

At this stage, the runtime may still store only `event_code` + dummy payload pointer — but the lowering must be correct enough to evolve once error structure expands.

### Deliverables

* Extend `lower_to_mir_ssa`:

  * For `ThrowException`, produce a new MIR instruction:

    * Option A (minimal now): treat constructor as `Call` to an internal runtime builder (e.g. `drift_error_new(code, payload_str)`).
    * Option B (future): build a struct-like Error representation directly.
* Pass `event_code` from `checker.exception_infos`.
* SSA unit test to prove MIR shape is accepted by verifier.

---

## Phase 3 — Runtime support: add a constructor helper

### Goals

Implement a real `Error* drift_error_new(event_code, payload)` runtime helper (or upgrade `error_dummy.c`).

### Minimal viable runtime

* Accept `event_code: i64` (already in dummy runtime).
* Accept one payload pointer (String) OR a vector/struct; for now, String-only is fine.
* Populate the struct:

  * `.code = event_code`
  * `.payload = pointer`
  * later: frames, domain, etc.

### Deliverables

* Update `lang/runtime/error_dummy.[ch]` (or introduce `error_runtime.[ch]`).
* Provide LLVM declaration in `ssa_codegen`.
* Add an SSA e2e test:

  * `throw InvalidPayload(...)`
  * Catches via stmt-form or expression-form try/catch
  * Asserts event dispatch sees the right event_code.

---

## Phase 4 — SSA catch dispatch integration

### Goals

Ensure existing event-dispatch logic seamlessly matches exceptions created via constructors.

Because lowering will produce real event_codes and Error payload, the existing try/catch SSA dispatch (via `ErrorEvent`) should “just work”.

### Deliverables

* Add a new e2e:

  ```drift
  exception Bad(payload: String)

  fn main() {
    try {
      throw Bad(payload="x")
    } catch Bad e {
      out.writeln("caught")
    }
  }
  ```
* Expected: prints `"caught"`.

---

## Phase 5 — Polish, ergonomics, spec update

### Work

* Update language spec:

  * “Exception declarations create constructor names in the value namespace.”
  * `throw ExcName(arg=...)` syntax described.
  * Exception fields documented.
* Add negative tests:

  * Field mismatch.
  * Missing required field.
  * Exception shadowing rules.
* Clean up old “unknown function” expectations.

---

# 3. Status Summary

| Area                             | Status                |
| -------------------------------- | --------------------- |
| Exception decl in checker        | ✓ registered with event codes |
| Exception constructor resolution | ✓ checker builds ExceptionCtor nodes |
| Exception constructor lowering   | ✓ SSA calls `drift_error_new_dummy`; event_code + first payload threaded |
| Runtime support for constructors | ✓ `drift_error_new_dummy` stores event_code and payload |
| SSA dispatch compatibility       | ✓ verified via ctor_catch/expr/multi e2e |
| e2e coverage                     | ✓ stmt/expr/multi throw/catch for user exceptions |
| Spec updates                     | ✓ constructor semantics & payload note documented |

---

# 4. Immediate Next Step

Per your preference (Option B):

**Implement Phase 1: checker support for exception constructors.**

Specifically:

* Add exceptions to the symbol table.
* When seeing `throw Name(args...)`, resolve `Name` in the exception namespace.
* Create an `ExceptionConstructor` IR node instead of function call.
* Reject mis-typed fields with clear checker errors.
* Add 2–3 checker tests + 1 compile-time negative test.

Once Phase 1 compiles and tests pass, we move on to Phase 2.

---

# 5. Notes

* This work is **isolated** from try/catch semantics; all event dispatch logic remains correct.
* Constructors are pure front-end sugar; SSA lowering remains strict and explicit.
* Runtime work can be incremental — no need to perfect the Error struct now.
* This approach matches the incremental structure you used for try/catch, SSA ref semantics, and event dispatch.
