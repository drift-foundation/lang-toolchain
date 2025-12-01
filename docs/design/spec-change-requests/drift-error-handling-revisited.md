## Error Handling Revisited

This document finalizes the new error-handling model for Drift. It removes `try … else`, introduces expression-form `try … catch`, and clarifies the statement-form `try { … } catch { … }` semantics.

The goal is to keep Drift strongly event-based, eliminate redundant sugar, unify mental models, and allow multi-statement fallbacks in expression position.

---

## 1. Removed Feature: `try expr else expr`

The `try … else …` expression form is **removed entirely**.

**Rationale:**

* It duplicates the role of a catch-all fallback.
* It prevents multi-statement fallback logic.
* It cannot discriminate on events.
* The new expression-form `try … catch` subsumes all valid use cases.

All examples, grammar rules, and lowering descriptions referring to `try … else` are deleted.
Code using that form must migrate to the new `try expr catch { … }` form.

---

## 2. Statement-Form `try { … } catch …` (unchanged)

The existing statement form remains exactly as today.

### 2.1 Allowed forms

```drift
try {
    ...
} catch EventName(e) {
    ...          // e: Error
}
```

```drift
try {
    ...
} catch e {
    ...          // catch-all, e: Error
}
```

```drift
try {
    ...
} catch {
    ...          // catch-all, no binder
}
```

### 2.2 Semantics

* Matching is **by event name**, never by type.
* Binder type is always `Error`.
* Missing binder = `_` (ignored).
* Other events propagate upward.
* No new sugar; **no** `catch (e: Error)` form.

---

## 3. New Feature: Expression-Form `try expr catch`

A new expression form replaces `try … else`.

It allows:

* Multi-statement fallback logic
* Access to the caught `Error`
* Matching specific events
* Type-checked result unification
* Clean desugaring into a block containing statement `try`/`catch`

### 3.1 Syntax

#### **3.1.1 Catch-all without binder**

```drift
val x = try some_call() catch {
    // fallback statements
    fallback_expr
}
```

#### **3.1.2 Catch-all with binder**

```drift
val x = try some_call() catch e {
    // use e: Error
    fallback_expr
}
```

#### **3.1.3 Specific event**

```drift
val x = try some_call() catch InvalidOrder(e) {
    // handle only InvalidOrder
    fallback_expr
}
```

If the thrown error does **not** match `InvalidOrder`, the error propagates normally.

### 3.2 Type rules

* Attempt expression must have type `T`.
* Fallback block must have type `T`.
* A block returns the value of its **last expression** unless explicit `return` appears.

### 3.3 Desugaring

#### Catch-all:

```drift
try expr catch { block }
```

desugars to:

```drift
{
    try { expr }
    catch e {
        block
    }
}
```

#### Specific event:

```drift
try expr catch EventName(e) { block }
```

desugars to:

```drift
{
    try { expr }
    catch EventName(e) {
        block
    }
}
```

### 3.4 Effects on control flow & context capture

* The attempt expression runs first.
* If it throws, context capture (`ctx_frames`) and backtrace recording occur as usual.
* Control then enters the catch block.
* The block returns a value for the overall expression.
* Drift does not support `finally`.

---

## 4. Changes Required in the Language Spec

### 4.1 Chapter 8.4 (Try/else → removed)

Remove all mentions of `try expr else expr`.

Add a subsection **Expression-Form Try/Catch** containing:

* Syntax (three forms defined above)
* Type rules
* Desugaring
* Notes on context capture

### 4.2 Chapter 14.5 (Inline catch-all shorthand)

Remove the description of `try expr else expr`.

Replace with:

> Expression-form `try expr catch { … }` and
> `try expr catch Event(e) { … }` behave as if wrapped in a block containing a statement `try` and the corresponding catch arm.

### 4.3 Grammar adjustments (summary)

Add productions:

```
Expr ::= 'try' Expr 'catch' CatchExprArm

CatchExprArm ::=
      EventName '(' IDENT ')' Block
    | IDENT Block
    | Block
```

Remove:

```
TryElseExpr
```

### 4.4 Spec examples

All examples referencing `try … else` must be rewritten to use `try … catch`.

---

## 5. Migration Guidance

### Replace old try/else:

**Old:**

```drift
val timeout = try parse_ms(s) else 1000
```

**New:**

```drift
val timeout = try parse_ms(s) catch { 1000 }
```

### Handling an explicit event:

```drift
val amount = try read_amount(raw) catch BadFormat(e) {
    0
}
```

### Multi-statement fallback:

```drift
val user = try fetch_user(id) catch e {
    log_error(e)
    default_user()
}
```

---

## 6. Summary of Decisions

* **`try … else` removed.**
* **Statement try/catch unchanged** (event matching, binder always `Error`).
* **New expression-form try/catch introduced**:

  * catch-all without binder,
  * catch-all with binder,
  * specific event.
* **No new sugar.** No `catch (e: Error)`.
* Desugaring remains consistent: expression forms wrap a statement `try` inside a block.
* Fallback blocks may contain multiple statements and must produce a value.

This unifies Drift’s error model into a clean, consistent structure while increasing expressive power.

