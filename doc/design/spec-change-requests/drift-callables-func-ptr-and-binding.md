# FnNs, function pointers, and binding in Drift

This document captures the decisions around **function values**, **static vs dynamic callables**, **binding**, and **borrowed vs owned erasure** in Drift, plus implementation guidance for a production compiler.

---

## 1. Goals and principles

### Goals

* Provide **ergonomic callbacks** (functions, methods, bound methods, partial application).
* Preserve the **two worlds rule**:

  * `Fn(...) [nothrow] -> ...` = **function pointers** (no captures, always safe to retain).
  * `FnN` / `CallbackN` = **may capture** (subject to retaining/non-retaining analysis).
* Keep **ABI correctness** for can-throw calls (FnResult-style ABI).
* Avoid dual truths: the compiler must keep a **single authoritative source** for:

  * call ABI (nothrow vs can-throw),
  * function value identity (wrapper vs impl),
  * call target identity (canonical ids, not raw strings).

### Principles

* Static dispatch is the default; dynamic dispatch is explicit.
* Binding is a library feature built on primitives the compiler provides (function values + callables).
* Any “escape hatch” (string symbols, name guessing) is treated as a bug.

---

## 2. Core value kinds

### 2.1 Function pointers: `Fn(P...) [nothrow] -> R`

A function pointer is a **thin pointer** to code, with a fully known signature.

Properties:

* **No captures, ever.**
* Always safe to store/retain (no borrow/capture lifetime complexity).
* Callable via normal call syntax once it is a value (`HInvoke` → `CallIndirect`).

Throw-mode:

* Function pointer types include throw-mode (`nothrow` vs can-throw).
* Throw-mode determines ABI:

  * NOTHROW: returns `R`
  * CAN_THROW: returns `FnResult<R, Error>` (canonical `Error` TypeId)

### 2.2 Static callables: `FnN<P..., R>`

A static callable is a **concrete type** (usually a struct) that implements a trait/interface for a specific signature.

Properties:

* May capture values (receiver, bound args, remap tables).
* Calls are statically dispatched when monomorphized; can inline.

### 2.3 Dynamic callables: `CallbackN<P..., R>`

A dynamic callable is a **type-erased callable** used for:

* heterogeneous storage (`Array<CallbackN<sig>>`)
* plugin boundaries
* long-lived registries where static generic explosion is undesirable

Dynamic representation (conceptual):

* `data_ptr` (environment/object)
* `call_ptr` (thunk that knows how to call with `sig`)
* optional `drop_ptr` (if owned)

Dynamic dispatch is explicit; users decide when to erase.

---

## 3. Binding and method references

### 3.1 Unbound method references

Unbound method references do not capture a receiver; they are compatible with `fn` pointers by making the receiver an explicit first parameter.

Example conceptual shape:

* method `S.inc(self: &S, x: Int) nothrow -> Int`
* unbound reference type: `Fn(&S, Int) nothrow -> Int`

Ergonomics:

* Canonical call form: `p(&s, 3)`
* Optional sugar (later): allow `s.p(3)` to desugar to `p(&s, 3)` if `p` expects `&S` as the first param.

### 3.2 Bound method values

Bound method values *capture* a receiver (and possibly other bound args). They are therefore **not function pointers**.

Decision:

* Bound method values produce a **static FnN**, not a `fn` pointer.

This keeps the two-worlds rule clean:

* function pointers never carry environments
* captured receiver implies a callable/closure object

### 3.3 `bind(...)` is a stdlib feature

Decision:

* `bind` is a **standard library** feature, not compiler magic.
* It returns a concrete type implementing `FnN<P..., R>`.

Basic form:

* `bind(receiver, unbound_method, args...)` returns a callable object.

Advanced form:

* support placeholders (e.g. `_`) to remap/skip/permute parameters (Boost-style).

Example intent:

* `bind(s, S.add, _, 5)` produces a callable where `call(x) = s.add(x, 5)`.

---

## 4. Erasure to dynamic callables

### 4.1 Owned vs borrowed conversion API

Decision (pinned):

* `std.callback(c)` → **owned** dynamic callback (may box/allocate)
* `std.callback_ref(&c)` → **borrowed** dynamic callback (no box)

These are explicit operations that convert any `T: FnN<...>` (and also `fn` pointers) into the dynamic form.

### 4.2 What “erasure” means

Erasure hides the concrete callable type (e.g., `Bind<S,...>`) and retains only:

* the signature (`Sig`)
* runtime thunks needed to invoke it dynamically

This enables heterogeneous storage:

* arrays of mixed callable implementations
* registries of callbacks of a uniform signature

### 4.3 Ownership model

* Owned `CallbackN` must be safe to store long-term.

  * It must own its environment (commonly via boxing), and carry a drop hook if needed.
* Borrowed `CallbackNRef`-style value is safe only within the referenced object’s lifetime.

---

## 5. Function reference values and cast selection

### 5.1 Function references as values

Function references produce a first-class function value constant in HIR/MIR (conceptually `FnPtrConst`).

Identity:

* Function values are identified canonically (e.g., `FunctionRefId`), not by raw symbol strings.
* Wrapper vs impl selection is explicit in the identity kind.

Selection rules:

* Typed context drives overload selection.
* Module-qualified names (`A.id`) are supported for value resolution.
* Exported/extern references bind to wrapper identity and force CAN_THROW (per policy).

### 5.2 Explicit cast syntax

Decision (pinned):

* Use **function-form explicit cast**: `cast<T>(expr)`

Initial semantics:

* strict and type-directing
* used for overload disambiguation and selecting a function reference in expression position
* no thunk insertion in cast (thunking is a separate step)

---

## 6. Throw-mode and ABI adaptation

### 6.1 Why adaptation is required

A function pointer’s throw-mode changes its ABI:

* NOTHROW: returns plain `R`
* CAN_THROW: returns `FnResult<R, Error>`

You cannot safely treat a nothrow function pointer as a can-throw function pointer without an adapter.

### 6.2 Nothrow → can-throw thunking

Decision:

* Typed assignment may insert an Ok-wrapping thunk to adapt NOTHROW to CAN_THROW.
* `cast<T>(expr)` remains strict at this stage (no thunk insertion).

Thunk shape (conceptual):

* `thunk(args...) -> FnResult<R, Error> { return Ok(target(args...)) }`

Implementation hint:

* Materialize thunks pre-MIR (in the driver/HIR layer) with stable canonical ids.
* Cache thunks by `(target_ref, signature)` to avoid duplicates.

---

## 7. Implementation hints and staged rollout

### 7.1 Compiler primitives that must exist

* Stable NodeId throughout HIR
* CallInfo / CallSig for every call site
* MIR carries `can_throw` per call; LLVM lowers from MIR (no guessing)
* Canonical `Error` TypeId used for FnResult ABI

### 7.2 Recommended implementation steps

1. **Static/dynamic callable type definitions**

   * Define `FnN` traits + `CallbackN` interface family.
2. **Unbound method references**

   * Resolve `S.method` as a function value of type `Fn(&S, ...) -> ...`.
3. **`bind` library**

   * Implement bind objects as concrete structs implementing `FnN<...>`.
4. **Dynamic erasure**

   * Implement `std.callback(c)` and `std.callback_ref(&c)` conversions.
5. **Thunking**

   * Add NOTHROW → CAN_THROW thunk generation for typed assignment.
6. **Lambda coercion**

   * Captureless lambda → synth function + fn pointer; capturing lambda → error.

### 7.3 Testing focus (what to lock)

* Function values preserve canonical identity (wrapper vs impl) across modules.
* Typed call pipeline uses CallInfo/CallSig everywhere (no name guessing).
* Thunking creates stable wrapper identities and produces correct FnResult ABI.
* `bind` returns static FnN; dynamic erasure is explicit and produces correct call behavior.
* Borrowed vs owned dynamic callables enforce lifetime/ownership expectations.

---

## 8. Summary of pinned choices

* `bind` returns **static**: `FnN<...>`
* Dynamic callable creation is explicit:

  * `std.callback(c)` → owned (may box)
  * `std.callback_ref(&c)` → borrowed (no box)
* Function pointer casts use `cast<T>(expr)` (strict, single type arg)
* Bound methods produce `FnN`, not `fn` pointers
* Unbound methods are compatible with `fn` pointers by explicit receiver-first parameter
* Throw-mode is part of function pointer types and determines ABI; adaptation uses explicit thunks (Ok-wrap)

---
