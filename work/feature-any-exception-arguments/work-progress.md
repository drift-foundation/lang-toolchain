## Overview

This workstream enhances Drift exceptions so that:

* Exceptions support **any number of arguments**, each of arbitrary type.
* All arguments undergo **diagnostic formatting**, not `Display`.
* Args and `^` locals are stored as **diagnostic strings** (no “first payload” special case).
* Catch-time access uses **typed keys** instead of stringly lookup.
* **Dot-shortcut** sugar improves ergonomic, type-safe access to exception argument keys.

This file tracks work across three steps:

* **Step 1 — Spec cleanup** *(complete)*
* **Step 2 — Diagnostic trait formalization** *(complete)*
* **Step 3 — Key-indexing & dot-shortcut sugar** *(spec/grammar done; compiler/runtime next)*

---

## Step 1 — Spec cleanup (complete)

**Scope:** Fix long-standing inconsistencies in exception argument semantics.

### Changes

* Chapter 14 rewritten to remove “first payload” special case.
* All exception args and `^` locals must implement **Diagnostic**; normalized to `String`.
* Catch semantics clarified: event dispatch by name; no payload typing.
* `Error.args` defined as `Map<String, String>`.
* Updated wording in try/catch sections to match diagnostic-only model.
* Step recorded as spec-only, no code/tests.

### Status: **DONE**

---

## Step 2 — Diagnostic trait (spec-only) (complete)

**Goal:** Introduce the dedicated trait that produces structured diagnostic strings.

### Changes

* §5.13.7 defines:

  * `Diagnostic` trait with `write_diagnostic(self, &mut DiagnosticCtx)`.
  * `DiagnosticCtx` (`emit_current`, `field`, `field_fmt`, `nested`).
* Exception args and `^` locals **must** implement Diagnostic.
* Blanket spec guidance: primitives use `emit_current(self.to_string())`; complex types implement Diagnostic explicitly.
* Mark `Debuggable` as **legacy for diagnostics**.
* Updated all references in Chapter 14 to use Diagnostic explicitly.
* No implementation yet: this step remains spec-only.

### Status: **DONE**

---

# Step 3 — Exception args-view & dot-shortcut integration

This step introduces:

1. **Args-view value types** (e.g., `DbErrorArgsView`).
2. **Key types** (`DbErrorArgKey`) used to type-safely select attributes.
3. **`operator[]`** on args-view types taking a key value and returning `Option<String>`.
4. **Dot-shortcut sugar** for clean key access:

   * `e.args[.sql_code]`
   * `e.args[.sql_code()]`

Dot-shortcut is a value-level sugar, not a type-level facility; everything resolves against the **receiver value** on which `operator[]` or `.func(…)` is invoked.

---

## Step 3A — Spec + grammar (complete in this pass)

**Scope:** Document args-views/keys and dot-shortcut behavior; extend grammar to parse leading-dot expressions.

### Changes

- **`drift-lang-spec.md`**

  - Added §14.5.4 “Args-view and typed key access”:

    - Every exception `E` has an args-view type:

      ```drift
      struct EArgsView { /* opaque view into Error.args */ }

      implement E {
          fn args(self: &E) returns EArgsView
      }
      ```

    - Every exception `E` has an arg-key type:

      ```drift
      struct EArgKey { name: String }
      ```

    - `EArgsView` exposes key-based lookup:

      ```drift
      implement EArgsView {
          fn operator[](self: &EArgsView, key: EArgKey) returns Option<String>

          // one generated key per declared field, e.g.:
          fn sql_code(self: &EArgsView) returns EArgKey
          fn payload(self: &EArgsView) returns EArgKey
      }
      ```

    - `Option<String>` holds the **diagnostic string** for the attribute.

    - Dot-shortcut sugar is defined as:

      ```drift
      e.args[.sql_code]    // desugars to e.args[ e.args.sql_code ]
      e.args[.sql_code()]  // desugars to e.args[ e.args.sql_code() ]
      view.filter(.is_admin) // desugars to view.filter(view.is_admin)
      ```

      Resolution is **value-based**: `.foo` is applied to the receiver value (`e.args`, `view`, etc.).

- **`drift-lang-grammar.md`**

  - Extended `PostfixTail` and argument lists to allow **leading-dot expressions** inside `[]` and `()`:

    ```ebnf
    PostfixTail  ::= "." Ident
                  | "[" ExprOrLeadingDot "]"
                  | "(" ArgsOrLeadingDot? ")"
                  | "->"

    ArgsOrLeadingDot ::= ExprOrLeadingDot ("," ExprOrLeadingDot)*
    ExprOrLeadingDot ::= Expr | LeadingDotExpr
    LeadingDotExpr  ::= "." Ident ("(" Args? ")")?
    ```

  - Added a note that `LeadingDotExpr` only appears inside indexing brackets or argument lists and is desugared semantically into member access on the receiver (see main spec for details).

### Status: **DONE (spec/grammar)**

Compiler/runtime work remains.

---

## Step 3B — Compiler/runtime plan (current focus)

**Goal:** Make a user-defined exception with multiple args (e.g., `sql_code`) fully functional, including:

- Diagnostic-only storage in `Error.args` (already spec’d in Steps 1–2).
- Generated args-view type and key type per exception.
- `operator[]` on args-view returning `Option<String>`.
- Dot-shortcut lowering for `e.args[.sql_code]` / `e.args[.sql_code()]`.

**Runtime progress:** `DriftError` now carries an args array (`(key,value)*` + count), `drift_error_new_dummy(code, key, payload)` seeds one entry when `key` is non-empty, and `drift_error_get_arg` provides lookup. Compiler wiring to populate multiple fields and synthesize args-views/keys remains.

**Compiler progress:** checker metadata now carries `arg_order` plus synthesized arg-key/args-view type names for each exception; synthetic structs for key/view are registered; `ExceptionCtor` nodes retain `arg_order`; `MyError.args` resolves to the per-exception args-view type; indexing that view with a declared string key type-checks as `Option<String>`.

**SSA/runtime plumbing:** args-view indexing lowers to `__exc_args_get` returning an `Option<String>` wrapper over `drift_error_get_arg`; `Option<String>` has a concrete LLVM/C shape (`{i8 is_some, DriftString}`). `ExceptionCtor` now seeds all declared args: first field via `drift_error_new_dummy(code, key, payload)`, remaining fields via `drift_error_add_arg(err, key, value)`. Dot-shortcut rewrite is still pending.

**Compiler alignment:** args-view indexing now expects the per-exception ArgKey type (ArgKey structs + key helper methods are synthesized); lowering extracts the key name and calls `__exc_args_get`, returning `Option<String>`.

**Tests:** Added SSA-level cases for args lookup (now using ArgKey helpers) and dot-shortcut; added a C runtime test (`runtime_error_args_none.c`) to cover the `None` branch of `__exc_args_get`. Justfile `test-runtime-c` compiles/execs both runtime tests.

### 3B.1 Front-end: exception metadata & synthetic types

**Tasks:**

1. **Extend exception metadata**

   - When processing an `exception E(field1: T1, field2: T2, ...)` declaration, record:

     - Exception name `E`.
     - Ordered list of fields `(name, type)`.

   - This metadata already feeds `Error.args` construction; reuse it to drive key generation.

2. **Synthesize arg-key type per exception**

   - For each exception `E`, synthesize a type in the current module:

     ```drift
     struct EArgKey {
         name: String
     }
     ```

   - Implementation detail: name-mangling (e.g., `_E_ArgKey`) is up to the compiler; spec only cares about the conceptual type.

3. **Synthesize args-view type per exception**

   - For each exception `E`, synthesize:

     ```drift
     struct EArgsView {
         // likely holds a reference to Error plus the event name/code
         // e.g., error: &Error
     }
     ```

   - The internal field(s) are implementation detail, but it must be sufficient to:

     - know which exception schema applies (E’s field list),
     - read from the underlying `Error.args` map at runtime.

4. **Generate args-view accessor on exception**

   - For each exception `E`, synthesize a method:

     ```drift
     implement E {
         fn args(self: &E) returns EArgsView { ... }
     }
     ```

   - In lowering, `E::args` will be compiled to:

     - wrap the `Error` value for this event into an `EArgsView` (or, in catch contexts, wrap the caught `Error`).

   - Exact binding between `E` in the spec and `Error` in implementation can be handled in your existing “exception event” machinery; the plan here assumes that in a typed catch (`catch E(e)`), the compiler has both the `Error` and the event name `E`.

---

### 3B.2 Front-end: key values on args-view

**Tasks:**

5. **Generate key vals or methods per field**

   For each declared field `field_name` in `exception E`, on `EArgsView` generate **one** of:

   - **Val key:**

     ```drift
     implement EArgsView {
         val field_name: EArgKey = EArgKey(name = "field_name");
     }
     ```

   - **Fn key:**

     ```drift
     implement EArgsView {
         fn field_name(self: &EArgsView) returns EArgKey {
             return EArgKey(name = "field_name");
         }
     }
     ```

   Either form is compatible with dot-shortcut; the val form is slightly simpler to implement and use.

6. **Generate `operator[]` for args-view**

   - On `EArgsView`, synthesize:

     ```drift
     implement EArgsView {
         fn operator[](self: &EArgsView, key: EArgKey) returns Option<String> {
             // lookup key.name in underlying Error.args
         }
     }
     ```

   - Implementation strategy:

     - When `EArgsView` is constructed, it holds a `&Error` (and possibly verifies `Error.event` == `E` in debug builds).
     - `operator[]`:

       - looks up `self.error.args.get(key.name)`,
       - returns `Some(string)` if present, else `None`.

---

### 3B.3 Dot-shortcut lowering

**Tasks:**

7. **AST/IR representation of LeadingDotExpr**

   - Parser already produces `LeadingDotExpr ::= "." Ident ("(" Args? ")")?`.
   - In the AST/IR, tag this node so the lowering phase can distinguish it from normal expressions.

8. **Lowering rule for indexing**

   - For a `PostfixExpr` `base` with `PostfixTail` `[ LeadingDotExpr(expr) ]`, rewrite during lowering:

     ```drift
     let tmp = base;                // ensure base is evaluated once
     tmp[ tmp.expr ]                // if LeadingDotExpr is `.expr`
     tmp[ tmp.expr(args...) ]       // if LeadingDotExpr is `.expr(args...)`
     ```

   - Apply normal type-checking to the rewritten form:

     - `tmp.expr` must be a valid member or method of `tmp`’s type.
     - The return type of `tmp.expr` must match the parameter type of `operator[]`.

9. **Lowering rule for argument lists**

   - For a call `base.fn( LeadingDotExpr(expr), other_args... )`, rewrite:

     ```drift
     let tmp = base;
     tmp.fn( tmp.expr, other_args... )           // `.expr`
     tmp.fn( tmp.expr(args...), other_args... )  // `.expr(args...)`
     ```

   - Again, normal method and type resolution apply.

10. **Error reporting**

    - If lowering finds that `tmp.expr` does not resolve, emit a normal:

      - “no member named `expr` on type X” error.

    - Dot-shortcut should never introduce new, special error kinds; it uses standard member lookup failures.

---

### 3B.4 MIR → SSA → LLVM impact

**Tasks:**

11. **New types in IR**

    - Ensure the front-end’s type system and MIR can represent:

      - `EArgsView` as a struct type,
      - `EArgKey` as a struct type with a `String` field.

    - Ensure these appear in DMIR where appropriate (e.g., if they are visible outside the module).

12. **Method lowering**

    - `E::args(self: &E) -> EArgsView` must lower to:

      - whichever runtime routine/IR pattern you use to wrap an `Error` (or event-specific Error) into an `EArgsView`.

    - `EArgsView.operator[](key: EArgKey) -> Option<String>` is a simple helper:

      - call into your existing `Error.args` map, using `key.name` as the key,
      - wrap the result in `Option<String>`.

13. **Catch lowering**

    - In typed catches like:

      ```drift
      try { ... } catch InvalidOrder(e) { ... }
      ```

      confirm that:

      - `e` remains of type `Error` (as spec),
      - but you can construct `InvalidOrderArgsView` from `e` transparently when encountering `e.args` in that scope.

    - This may already align with your existing event metadata; you’re effectively tagging an `Error` with an `E` event, and args-view uses that event association.

---

### 3B.5 Tests / validation

**Tasks:**

14. **Positive tests**

    - Define a user exception:

      ```drift
      exception DbError(sql_code: Int, reason: String)
      ```

    - Throw and catch:

      ```drift
      fn test() returns Void {
          try {
              throw DbError(sql_code = 23505, reason = "duplicate key")
          } catch DbError(e) {
              val code =
                  e.args[.sql_code]
                      .unwrap_or("0")  // once combinators exist; or hand-written match

              val reason =
                  e.args[.reason]
                      .unwrap_or("<none>");
          }
      }
      ```

    - Confirm in IR that:

      - all args are present in `Error.args` as strings (`"23505"`, `"duplicate key"`),
      - `e.args[.sql_code]` is lowered to `e.args[ e.args.sql_code ]` and then to appropriate calls.

15. **Negative tests**

    - `e.args[.sql_cod]` (typo in method/val name) should produce:

      - “no member `sql_cod` on type EArgsView”.

    - `e.args[.nonexistent()]` similar.

16. **Generic Error behavior**

    - Ensure `Error` itself still exposes the generic, stringly API:

      ```drift
      fn log_sql_state(err: Error) returns Void {
          val code =
              err.args()
                 .key("sql_state")
                 .first()
                 .unwrap_or("<none>");
      }
      ```

    - No args-view or dot-shortcut expectations for generic `Error`.

---

### Step 3B Status

- **Spec/grammar:** done.
- **Compiler tasks (3B.1–3B.5):** pending; to be implemented next.

Once Step 3B is complete, we’ll have:

- arbitrary-argument exceptions captured via Diagnostic,
- typed args-views and key types,
- value-based dot-shortcut for concise, type-safe attribute access in typed catches.
