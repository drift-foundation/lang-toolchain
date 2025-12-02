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
* **Step 3 — Key-indexing & dot-shortcut sugar** *(next)*

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

# Step 3 — Exception args-view & dot-shortcut integration (**NEXT**)

This step introduces:

1. **Args-view value types** (e.g., `DbErrorArgsView`).
2. **Key types** (`DbErrorArgKey`) used to type-safely select attributes.
3. **operator[]** on args-view types taking a key value.
4. **Dot-shortcut sugar** for clean key access:

   * `e.args[.sql_code]`
   * `e.args[.sql_code()]`

Dot-shortcut is a value-level sugar, not a type-level facility; everything resolves against the **receiver value** on which `operator[]` or `.func(…)` is invoked.

**Status:** Spec/grammar updated in this pass (Option-based args-view, dot-shortcut syntax). Compiler/runtime changes still pending.

---

## 3.1 Args-view type

Each exception type `E` gains a compiler-generated args-view:

```drift
struct EArgsView {
    // opaque reference to underlying Error
}
```

Produced by:

```drift
implement E {
    fn args(self: &E) returns EArgsView
}
```

**Responsibilities of ArgsView:**

* Provide `operator[](self, key: EArgKey) -> Option<String>`.
* Provide named key-constructors (`sql_code`, `reason`, …) as:

  * `val` fields returning `EArgKey`

The compiler generates these from the exception field names.

---

## 3.2 Key type

For each exception:

```drift
struct EArgKey { name: String }
```

Each attribute in the exception declaration maps to one `EArgKey` value on the args-view:

```drift
val sql_code: EArgKey = EArgKey(name = "sql_code")
```

These are **plain Drift values**, not static members, not type-level constructs.

---

## 3.3 operator[] for args-view

Args-view exposes key-based indexing:

```drift
implement EArgsView {
    fn operator[](self: &EArgsView, key: EArgKey) returns Option<String>
}
```

The returned `Option<String>` reflects the **diagnostic string** stored in `Error.args`.

---

## 3.4 Dot-shortcut sugar (new language feature)

**Name:** *dot-shortcut*
**Applies to:**

* inside indexing brackets (`base[ .expr ]`)
* inside argument lists (`base.fn( .expr , ...)`)

**Rule:**

> Inside `base[ .expr ]` or `base.fn( .expr )`, a leading `.expr` is sugar for `base.expr`.

### Examples

#### Indexing

```drift
e.args[.sql_code]
// desugars to:
e.args[ e.args.sql_code ]
```

```drift
e.args[.sql_code()]
// desugars to:
e.args[ e.args.sql_code() ]
```

#### Calls

```drift
view.filter(.is_admin)
// desugars to:
view.filter(view.is_admin)
```

```drift
obj.method(.build_key(a, b))
// desugars to:
obj.method(obj.build_key(a, b))
```

**Notes:**

* The resolution is **value-based**: `.foo` means “call `foo` on the base value.”
* No type-level magic, no symbol literals, no keyword lists.
* `base` is evaluated **once** and used for both sides of the desugared call.
* If `.foo` is not a valid member or method of the base type → compile error (normal name resolution).
* Dot-shortcut applies **only** when inside `[]` or argument lists. Normal method calls (`x.y()`) behave unchanged.

---

## 3.5 Grammar integration (surface only)

Update `drift-lang-grammar.md` :

* Extend `Expr` inside `IndexTail ::= "[" Expr "]"` and argument lists so that `Expr` may begin with a **LeadingDotExpr**:

```
LeadingDotExpr ::= "." Ident ( "(" Args? ")" )?
```

* Desugaring is semantic: parser records `.foo(...)` as a `LeadingDotExpr` with no receiver; lowering rewrites it to `base.foo(...)`.

No additional productions for paths, selectors, or symbolic literals.

---

## 3.6 Spec updates (Step 3 requires edits in the spec)

In `drift-lang-spec.md` :

* Chapter 14 (Exceptions):

  * Add subsection **14.5.4 ArgsView and Key Access** describing:

    * the args-view type,
    * key value construction,
    * Diagnostic string return values,
    * `Option<String>` usage.
  * Explicitly describe **dot-shortcut** and its desugaring in exception context.

* Chapter 5 (traits):

  * Optionally note that `EArgKey` is a simple value type, not trait-governed.

* Grammar appendix:

  * Add LeadingDotExpr explanation (semantic rewrite, not a separate operator).

---

## 3.7 Deliverables for Step 3

**Spec-level:**

* Update `drift-lang-spec.md` sections 5 and 14.
* Update grammar file to accept leading-dot expressions.
* Add a short section describing dot-shortcut as value-based sugar.

**Compiler work:**

* Generate `EArgsView` and `EArgKey` types.
* Generate key methods/vals for each declared exception arg.
* Implement dot-shortcut rewrite in lowering.
* Implement `operator[]` on args-views.
* Update error tests.

**Tests:**

* `e.args[.sql_code]` and `e.args[.sql_code()]` produce same lowering.
* Invalid `.foo` reports “no member named `foo` on EArgsView”.
* Generic `Error.args()` continues working with stringly lookup.
* Multiple args supported; defaults preserved.
* Diagnostic trait runs for all arg types.

---
