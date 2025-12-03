# diagnostic error refactor – work progress 

This doc tracks the implementation work for the new diagnostic model (`DiagnosticValue`, `Error.attrs`, new exception syntax, DMIR/ABI changes).
Spec work is tracked separately; this file is about *code* and tests.

## phase 1 – core types and runtime data model

### 1.1 `DiagnosticValue` type

* [ ] Add the `DiagnosticValue` variant to the core runtime / std (matching spec exactly):

  * `Missing`
  * `Null`
  * `Bool(value: Bool)`
  * `Int(value: Int64)`
  * `Float(value: Float64)`
  * `String(value: String)`
  * `Array(items: Array<DiagnosticValue>)`
  * `Object(fields: Map<String, DiagnosticValue>)`

* [ ] Implement helper methods:

  * [ ] `kind(&DiagnosticValue) -> String` (optional but useful for debugging).
  * [ ] `get(&DiagnosticValue, field: String) -> DiagnosticValue`
  * [ ] `index(&DiagnosticValue, idx: Int) -> DiagnosticValue`
  * [ ] `as_string`, `as_int`, `as_bool`, `as_float` → all return `Optional<T>`, never throw.
  * [ ] Ensure:
    - Out-of-range / wrong-type / missing field → `DiagnosticValue::Missing`.
    - `.as_*()` on `Missing` returns `Optional.none`.

* [ ] Unit tests for `DiagnosticValue`:

  * [ ] Accessing scalar values via `.as_*()`.
  * [ ] Nested `Object` and `Array` navigation.
  * [ ] Missing keys / wrong types / OOB index.

### 1.2 `Diagnostic` trait

* [ ] Replace old diagnostic trait / `DiagnosticCtx` usage with:

  ```drift
  trait Diagnostic {
      fn to_diag(self) returns DiagnosticValue
  }
  ```

* [ ] Implement `Diagnostic` for:

  * [ ] Primitive scalars (`Bool`, `Int`, `Float`, `String`).
  * [ ] Optionally for `Optional<T>` → `Null` or `T.to_diag()`.

* [ ] Add a default-derive-style path for structs:

  * [ ] For plain structs without a custom impl, synthesize `to_diag()` that returns `Object({ field_name: field_value.to_diag(), ... })`.

* [ ] Tests:

  * [ ] Struct with nested struct → correct nested `Object` tree.
  * [ ] Custom `Diagnostic` impl overrides the default behaviour.

---

## phase 2 – runtime `Error` representation

### 2.1 Replace `Error.args` with `Error.attrs`

* [ ] Change the runtime `Error` struct:

  * [ ] `args: Map<String, String>` → `attrs: Map<String, DiagnosticValue>`.
  * [ ] `locals` in context frames: `Map<String, DiagnosticValue>` instead of string map.

* [ ] Update all runtime constructors / helpers that build errors to use `attrs`.

* [ ] Remove any args-specific helpers / sugar (e.g. `args_view`, typed arg keys).

* [ ] Add new helper API:

  ```drift
  fn attrs(self: &Error) returns Map<String, DiagnosticValue>
  ```

* [ ] Ensure existing convenience access patterns are viable:

  ```drift
  val code = e.attrs["sql_code"].as_int()
  val cust = e.attrs["order"]["customer"]["id"].as_string()
  ```

### 2.2 `^` capture machinery

* [ ] Change capture-sites to call `to_diag()` on captured locals instead of stringifying.

* [ ] Store captured locals as `Map<String, DiagnosticValue>` in frames.

* [ ] Make sure capture logic does *not* throw:

  * If `to_diag()` fails, it should be a bug in the type, not in the capture path.

* [ ] Tests:

  * [ ] `^foo` where `foo` is a scalar.
  * [ ] `^foo` where `foo` is a struct with nested fields.
  * [ ] JSON logging for frames shows structured attrs, not strings.

---

## phase 3 – ABI + C runtime boundary

### 3.1 ABI struct changes / semantics

* [ ] Keep `struct DriftErrorAttr { const char* key; const char* value_json; };` layout.

* [ ] Update semantics:

  * [ ] `value_json` is canonical JSON encoding of the *full* `DiagnosticValue` tree.

* [ ] Implement `DiagnosticValue → JSON` encoding with:

  * [ ] Correct handling of `Missing` (either omitted or encoded as explicit marker; decide and document).
  * [ ] Canonical object key ordering (lexicographic) for stability.

* [ ] Implement `Error.attrs → DriftErrorAttr[]` conversion:

  * [ ] Keys iterated in canonical order.
  * [ ] Each `DiagnosticValue` serialized to JSON.

* [ ] Tests (C side):

  * [ ] Round-trip simple scalar attrs.
  * [ ] Nested objects and arrays.
  * [ ] Stable JSON ordering across runs.

### 3.2 C API / helper updates

* [ ] Update any C helpers that previously treated `value` as plain string to treat it as JSON.
* [ ] Adjust example snippets and comments to mention “JSON-encoded DiagnosticValue tree”.

---

## phase 4 – front-end: parsing, AST, and DMIR

### 4.1 Exception declaration syntax

* [ ] Update grammar:

  * From: `exception Foo(a: Int, b: String)`
  * To:   `exception Foo { a: Int, b: String, }`

* [ ] Update parser rules and AST nodes for `ExceptionDef`.

* [ ] Update pretty-printer / formatter.

* [ ] Add parser tests:

  * [ ] Simple exception.
  * [ ] With trait params and trait requirements.
  * [ ] Empty `{}`
  * [ ] Mixed spacing / trailing commas.

### 4.2 DMIR representation of exception attrs

* [ ] Update DMIR error node to:

  ```text
  attrs: Map<String, DiagnosticValue>
  ctx_frames: Array<CtxFrame>  // where CtxFrame.locals: Map<String, DiagnosticValue>
  ```

* [ ] Extend DMIR literal/expr system to represent `DiagnosticValue` variants:

  * [ ] `DV_Missing`
  * [ ] `DV_Null`
  * [ ] `DV_Bool`
  * [ ] `DV_Int`
  * [ ] `DV_Float`
  * [ ] `DV_String`
  * [ ] `DV_Array([…])`
  * [ ] `DV_Object([(key, dv), …])`

* [ ] Make DMIR lowering for exception construction build `DiagnosticValue` literals instead of strings:

  * [ ] Exception fields → `DiagnosticValue` via `to_diag()` (or statically known where possible).
  * [ ] Preserve declaration order in DMIR; JSON exporter sorts keys later.

* [ ] DMIR tests:

  * [ ] Lowering simple `InvalidOrder { order_id: 42, code: "bad" }`.
  * [ ] Nested diagnostics via fields containing structs.

---

## phase 5 – compiler lowering / SSA / codegen

* [ ] Update lowering from AST exception constructor to DMIR error node with `attrs`.

* [ ] Ensure error construction now:

  * [ ] Captures event name.
  * [ ] Populates `attrs` map with `DiagnosticValue` literals.
  * [ ] Attaches `ctx_frames` with `DiagnosticValue` locals.

* [ ] Adjust SSA/codegen paths to:

  * [ ] Allocate and initialize `DiagnosticValue` instances.
  * [ ] Call the runtime helpers for JSON encoding where needed (not on hot paths unless requested).

* [ ] SSA / MIR tests:

  * [ ] Error creation with multiple attrs, nested objects.
  * [ ] Try/catch lowering still works; `Error` struct layout changes don’t break the control-flow.

---

## phase 6 – logging, tools, and ecosystem

* [ ] Update JSON logging format:

  * [ ] `"args"` → `"attrs"` everywhere.
  * [ ] Values emit as JSON scalars/arrays/objects, not strings.
* [ ] Update any log parsers, CLI tools, or dashboards that expect string args.
* [ ] Provide a small “before/after” doc for ops/devs that shows:

  * Old log shape vs new log shape.
  * Examples of nested diagnostics and how to query them.

---

## phase 7 – migration & cleanup

### 7.1 Codebase migration from `args` to `attrs`

* [ ] Mechanical change: `e.args[...]` → `e.attrs[...]` plus `.as_*()` where needed.
* [ ] Replace any dot-notation flattening assumptions with proper tree access.
* [ ] Delete args-view APIs and internal flattening logic (no longer part of the internal representation; keep only optional serialization helpers if needed).

### 7.2 Backward compatibility / transitional story

* [ ] Decide whether to support reading old plain-string `value` JSON during a transition period:

  * [ ] If yes:
    - Detect “legacy” simple JSON values and treat them as scalars.
    - Mark this behaviour as deprecated.
  * [ ] If no:
    - Bump ABI version / error payload version and treat mixed environments as unsupported.

* [ ] Add migration note for application authors:

  * [ ] “Replace `args` with `attrs` and update code to navigate `DiagnosticValue` trees.”
  * [ ] “Attributes are now strongly typed and nestable.”

### 7.3 Cleanup

* [ ] Remove `DiagnosticCtx` and any dead code paths.
* [ ] Remove tests that rely on string-based args and replace with structured-diag tests.
* [ ] Update internal docs / comments that mention “args” or string-only diagnostics.

---

## phase 8 – validation checklist

* [ ] All unit tests for `DiagnosticValue`, `Diagnostic`, `Error.attrs`, and JSON encoding pass.
* [ ] All compiler / DMIR / SSA tests pass (including new structured-error tests).
* [ ] End-to-end “error logging” / “SQL error” scenarios produce the new JSON format.
* [ ] Log consumers and tools are updated or confirmed compatible.
* [ ] Spec and implementation are in sync for:

  * `DiagnosticValue` definition and helpers.
  * `Diagnostic` trait semantics.
  * Exception syntax.
  * `Error.attrs` and `locals` as `DiagnosticValue`.
  * ABI JSON encoding rules and canonicalization.

