Issues identified (ordering of points can be ignored, I removed clean items).

## 3. SSA lowering (strict SSA path)

This is where the interesting edges are.

### 3.1 Expression form: `_lower_try_catch_expr`

The new `_lower_try_catch_expr` builds:

* A snapshot of live user vars, threaded via block params (φ).
* An error block that *only* carries threaded locals, no `Error` value.
* A join block with result + locals as params.
* A call terminator with:

  * `normal` edge → join(res, locals)
  * `error` edge → err_block(locals only) 

Issues / gaps:

1. **Error value is dropped on the floor.**

   * The error edge does **not** pass an `Error` SSA value; `err_block` params are only the live locals. There is also no catch binder param. 
   * This directly contradicts your DMIR/SSA spec text, which says “error edges carry the Error value”. 
   * If you’re intentionally punting on error values in SSA for now, that’s fine, but then the spec needs a “temporary deviation” call-out. Otherwise, you’re going to forget and have a silent semantics drift.

2. **Only simple calls actually get try/catch semantics in SSA.**

   * The call-shaped case (`try foo(...) catch`) gets a proper call-with-edges; types are checked against the fallback type (`ret_ty == fallback_ty`), all good. 

   * The non-call case:

     ```python
     val_ssa, val_ty, current, env = lower_expr_to_ssa(expr.attempt, ...)
     ...
     current.terminator = Br(join(...))
     ```

     simply lowers the attempt as a pure expression and wires a straight branch to the join. If that expression can throw in the eventual SSA world, the catch arm is **never** used. 

   * Interpreter **does** catch exceptions thrown by arbitrary attempt expressions, not just calls, so SSA semantics diverge here.

   **Suggestion:**
   Either:

   * temporarily *reject* non-call attempts in `_lower_try_catch_expr` (raise `LoweringError`) so tests stay honest; or
   * generalize the lowering to build a try-body block that contains arbitrary lowering of the attempt to a call with error edges (more work, but future-proof).

3. **Catch block must end with an expression or “return expr”.**

   * You explicitly reject empty catch blocks and “last statement not expression/return”. 
   * That matches the checker’s `_check_block_result` expectations, so that’s fine; just be aware this is stricter than a generic block.

Net: the shape is structurally sound and plays nice with SSAVerifier, but you’re currently *lying* about semantics for non-call attempts and you’re discarding the error value completely.

### 3.2 Statement form: `lower_try_stmt`

This new lowering is deliberately narrow:

* Requires at least one catch and non-empty try body.
* Requires the final statement of the try body to be an `ExprStmt` whose value is a `Call(Name)`; anything else is rejected. 
* Uses **only the first** `CatchClause` (`first_catch = stmt.catches[0]`); additional catches and event patterns are completely ignored. 
* Catch block params again only carry threaded locals, no `Error`. Binder is not modeled at all.
* Call terminator is `Call(dest=..., normal→join(locals), error→catch(locals))`, again with no error arg in the edge. 

So:

* **Intentional limitation?** For now this is enough to support your SSA tests for simple `try { foo(x) } catch { ... }` shapes. 
* **But** it has the same two semantic gaps as the expression form:

  * No error value, no binder.
  * No support for multiple catches or event matching.

I’d call those out in a comment on `lower_try_stmt` as “early SSA lowering only supports catch-all with first clause, no error binder”.

### 3.3 Throw in SSA

* In `lower_block_in_env`, `ThrowStmt` and `RaiseStmt` both become a MIR `Throw` terminator with an SSA error value: `Throw(error=err_ssa, loc=stmt.loc)`. 
* You also added a verifier test for throw shape (`bb0(param err) { throw err }`) and the can-error invariants tests ensure throw only appears in `can_error` functions. 

That wiring looks good and matches the “throw only in can-error fns” decision.

---

## 4. Can-error invariants

The three tests at the end of `mir_ssa_tests.py` are doing exactly what you described:

1. `Throw` in a non-can-error function → `_annotate_can_error` must reject. 
2. `call-with-edges` where callee.can_error is `False` → reject. 
3. Plain call (no edges) to a `can_error` callee → reject. 

Given that both `_lower_try_catch_expr` and `lower_try_stmt` emit calls with `normal` and `error` edges, they fit these rules: `_annotate_can_error` will infer the callee as `can_error`, and any attempt to call it plainly somewhere else will be caught.

That part is solid: the invariants line up with the lowering.

---

## 5. Straightline MIR (`lower_to_mir.py`)

This file is still a bit of a jungle, but relevant bits:

* There are **two** implementations of ternary and try-expr lowering (`lower_ternary_expr` / `lower_try_expr_expr` nested inside `lower_expr`, and duplicated `_lower_ternary_expr` / `_lower_try_expr_expr` at the bottom). 
* The inner `lower_try_expr_expr` and the bottom `_lower_try_expr_expr` both use `TryCatchExpr` now (wording says “try/catch expression lowering currently supports call attempts only”), so you’ve already renamed it away from “try_else”. 

Not a correctness bug per se, but:

* Having two versions of the same helper is asking for divergence later. I’d consolidate them or delete the unused one once you’re sure which path is actually used.
* The shape still uses an explicit `err_dest` + `Call` returning `(value, error)` with both `normal` and `error` edges, which is your pair-return design and matches the DMIR spec better than the SSA scaffold does. 

Given this is the path that actually drives codegen today, you’re OK at the semantic level; the SSA path is the one lagging behind.

---

## 6. Docs / spec drift

Two small doc mismatches with your stated “try/catch only” direction:

1. `drift-lang-grammar.md` still documents `TryStmt` as:

   ```ebnf
   TryStmt      ::= "try" Block (TryElse | TryCatch)?
   TryElse      ::= "else" Block
   TryCatch     ::= "catch" (Ident)? Block
   ```

   and also separately has `TryCatchExpr ::= "try" Expr "catch" CatchExprArm`. 

   That doesn’t match the *implemented* parser (no try/else anymore). Either delete `TryElse` entirely or mark it as removed.

2. DMIR spec examples still mention “try_else” as a construct and show surface `try … catch` as lowering to a `try_else` operator. 

   With the new syntax, that example should just talk in terms of `try expr catch { … }` → structured try/catch, no “try_else” operator.

---

## 7.Fix next

1. Commit to “error edges always carry an `Error` SSA value”:

     * add an `Error` param to the error blocks in `_lower_try_catch_expr` and `lower_try_stmt`;
     * wire the catch binder to that param where present.

2. **Resolve `_lower_try_catch_expr` support.**

   * generalize to a dedicated try-body block with call-with-edges inside.

3. **Fix the limitations in `lower_try_stmt`.**

   * Extend it to multiple catch blocks with basic event discrimination.

4. **Clean up `lower_to_mir.py`.**

   * Remove the duplicate `_lower_try_expr_expr` or the inline one, and make sure the remaining version matches your new can-error shape and error-frame capture story.

5. **Update the docs.**

   * Strip `TryElse` from `drift-lang-grammar.md`.
   * Update DMIR spec examples to use `try … catch` terminology only.

