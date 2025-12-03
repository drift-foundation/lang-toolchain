# phase 0 — align spec / grammar / ABI (prep)

* [ ] Update spec/grammar with the new exception syntax:
  `exception Foo { x: Int, y: String }`
* [ ] Lock down the formal `DiagnosticValue` enum used across:

  * DMIR
  * compiler lowering
  * runtime representation
  * ABI
* [ ] Remove all earlier mentions of:

  * `args`
  * stringifying locals
  * JSON encoding
* [ ] Add final definitions for:

  * `DiagnosticValue` memory layout
  * `ErrorFrame` layout (typed locals)
  * `Error.attrs` map rules
  * default `Diagnostic` derivation for structs

---

# phase 1 — core runtime data model

## 1.1 Implement `DiagnosticValue`

Variants per spec:

* `Missing`
* `Null`
* `Bool`
* `Int`
* `Float`
* `String`
* `Array(Array<DiagnosticValue>)`
* `Object(Map<String, DiagnosticValue>)`

Helpers:

* `get(field: String) -> DiagnosticValue`
* `index(idx: Int) -> DiagnosticValue`
* `.as_int() / .as_string() / .as_bool() / .as_float()`
  → return `Optional<T>`, never throw
* Scalar access on wrong type or missing → `none`

Tests:

* scalar access
* nested objects
* arrays
* handling of Missing / wrong-type access

## 1.2 Implement `Diagnostic` trait

As per spec:

```drift
trait Diagnostic {
    fn to_diag(self) returns DiagnosticValue
}
```

Runtime must provide:

* Implementations for primitive types
* Implementation for `Optional<T>` → `Null` or nested
* Auto-generated implementation for structs:
  turns every field into an object entry using `to_diag()`

Tests:

* struct → object tree
* custom implementation overrides

---

# phase 2 — Error representation

## 2.1 Replace `Error.args` with `Error.attrs`

Runtime struct must now contain:

* `attrs: Map<String, DiagnosticValue>`
* `ctx_frames: Array<CtxFrame>`
  where each `CtxFrame.locals: Map<String, DiagnosticValue>`

Remove:

* string args
* args_view
* any flattening behavior

Add:

* `fn attrs(&self) -> Map<String,DiagnosticValue>`
* `attrs["sql_code"].as_int()` must work as expected

## 2.2 Implement the new `^` capture model

* Captured locals must call `.to_diag()` at capture time
* Typed `DiagnosticValue` stored in each frame
* Must never throw
* Old string serialization removed entirely

Tests:

* capture scalar
* capture struct
* capture local shadowing rules
* ensuring capture does not allocate or mutate unexpectedly

---

# phase 3 — DMIR and ABI

## 3.1 DMIR extensions

Create proper DMIR nodes for `DiagnosticValue`:

* `DV_Missing`
* `DV_Null`
* `DV_Bool`
* `DV_Int`
* `DV_Float`
* `DV_String`
* `DV_Array`
* `DV_Object`

DMIR error node now has:

```
attrs: Map<String, DiagnosticValue>
ctx_frames: Array<CtxFrame>
```

Tests:

* DMIR lowering of exception construction
* DMIR round-trip stability for nested DiagnosticValue trees

## 3.2 Updated ABI (non-JSON)

The ABI **must not use JSON**.

Define ABI structs that carry typed diagnostics directly:

* ABI representation of `DiagnosticValue` (tag + union)
* ABI representation of `ErrorAttr { key; value: DiagnosticValue_abi }`
* ABI representation of `ErrorFrame { locals: array of { key; value } }`

Work items:

* Finalize C-accessible structs
* Implement conversion from runtime `DiagnosticValue` → ABI struct
* Ensure deep trees (objects/arrays) copy correctly
* Ensure lifetime/ownership rules are clean and documented

Tests (C side):

* nested object round-trip
* nested arrays
* missing/null handling
* frames with locals

---

# phase 4 — parser + AST

## 4.1 New exception syntax

Implement grammar:

```
exception Name {
    field1: Type,
    field2: Type,
}
```

Update AST:

* store ordered list of field declarations
* track trait constraints if any

Tests:

* single field
* multiple fields
* no fields
* trailing comma variants

---

# phase 5 — compiler lowering / SSA / codegen

Lowering must:

* Construct DMIR error nodes with typed `DiagnosticValue`

* Lower exception initializers:

  ```
  throw MyErr { x: n, y: foo }
  ```

* Capture frames via typed locals

* Preserve the new exception syntax

Codegen must:

* Allocate DiagnosticValue instances
* Build arrays/objects recursively
* Populate error struct with typed data
* Call C ABI layer with typed diagnostics

Tests:

* try/catch lowering with new error layout
* SSA correctness for throw paths
* multiple nested DiagnosticValue combinations

---

# phase 6 — runtime logging (optional, non-spec)

Logging is **not** part of the spec.
If logs choose to serialize values, that serialization is **implementation-defined** and must not affect ABI.

Tasks:

* Provide a simple serializer (likely JSON or debug-format), purely for logs
* Keep this outside the ABI surface
* Ensure logging is stable but not relied upon for interop

Tests:

* Logging produces readable structured output
* No effect on runtime behaviour

---

# phase 7 — migration & cleanup

* Remove all legacy `args` codepaths
* Remove old string-based `DiagnosticCtx`
* Convert error constructors in the codebase to `attrs` and structs
* Remove any flattening logic invented for string args
* Update test suite to match typed diagnostics only

---

# phase 8 — validation

* All unit tests for DiagnosticValue + Diagnostic trait pass
* DMIR/SSA tests updated
* ABI tests pass with C consumers
* End-to-end: throw, catch, log, inspect errors produces correct typed output
* Spec and implementation remain 1:1

