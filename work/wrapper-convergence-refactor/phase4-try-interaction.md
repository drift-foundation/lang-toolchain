# Phase 4 Design Note: Try Expression / Boundary Wrapping Interaction

## Date: 2026-04-01

---

## 1. Where ConstructResultOk Is Created Today

| Site | File:Line | Context | Legal in nothrow fn? |
|------|-----------|---------|---------------------|
| `_visit_expr_HResultOk` | hir_to_mir.py:4829 | Explicit FnResult.Ok() in source | No (only can-throw) |
| `lower_function_body` (void) | hir_to_mir.py:5145 | Implicit return in can-throw void fn | No |
| `_visit_stmt_HReturn` (void expr) | hir_to_mir.py:5754 | `return;` with void expr in can-throw | No |
| `_visit_stmt_HReturn` (void bare) | hir_to_mir.py:5763 | Bare `return;` in can-throw | No |
| `_visit_stmt_HReturn` (value) | hir_to_mir.py:5791 | `return val;` in can-throw | No |
| **Wrapper MIR body** | driftc.py:7041 | `__wrap_method::` synthesis | Yes (wrapper IS can-throw) |

**Every existing ConstructResultOk is inside a can-throw function.** The
LLVM codegen guard at `llvm_codegen.py:3350` enforces this. There are no
`ConstructResultOk` instructions in nothrow functions today.

## 2. How Try Expressions Work With Boundary Calls

### Nothrow method in different package: `try b.wrap(7) catch { 0 }`

1. Call resolver redirects to `__wrap_method::wrap` (can-throw wrapper)
2. MIR emits `Call(__wrap_method::wrap, can_throw=True)` → FnResult
3. `_lower_can_throw_call_value` unwraps: `ResultIsErr` → `ResultOk`
4. Try dispatch routes errors to catch block
5. Ok value flows to join block

**No ConstructResultOk in the caller.** The wrapper body (can-throw) has
it. The caller just unwraps the FnResult.

### Nothrow free function in different package: `try foo() catch { 0 }`

1. Call resolver does NOT redirect (free functions don't get wrappers)
2. MIR emits `Call(foo, can_throw=False)` → surface type directly
3. No `_lower_can_throw_call_value` (not can-throw)
4. Result stored as-is. Try dispatch never fires for this call.

**No ConstructResultOk anywhere.** The nothrow free function returns
directly. The try/catch is a no-op for this call (only catches errors
from nested can-throw calls in the try body).

### Can-throw function: `try bar() catch { 0 }`

1. `Call(bar, can_throw=True)` → FnResult
2. `_lower_can_throw_call_value` unwraps as above
3. Try dispatch routes errors normally

**No ConstructResultOk in the caller.** The callee produces FnResult in
its own return statements.

## 3. Why Phase 4 Inline Wrapping Breaks

The proposed Phase 4 adds `ConstructResultOk` at the call site in the
**caller** for nothrow method calls with `boundary_ret_type_id`. But:

1. The caller may be nothrow (e.g., `main() nothrow -> Int`)
2. `ConstructResultOk` in a nothrow function is rejected by codegen
3. The codegen guard exists because FnResult lowering requires the
   function to have FnResult return type support

The existing architecture avoids this by putting `ConstructResultOk` in
the **wrapper** (which IS can-throw), not in the caller. The caller only
sees the wrapper's FnResult return value and unwraps it.

## 4. The Semantic Contract

### Current contract (correct, to be preserved)

- **ConstructResultOk**: only legal in can-throw functions
- **Boundary adaptation for nothrow methods**: happens in a can-throw
  wrapper function, not in the caller
- **Try expression**: unwraps FnResult from can-throw calls; no-op for
  nothrow calls
- **Nothrow free functions**: no boundary wrapping at all (no wrappers)

### Why the caller can't do inline wrapping

The caller doesn't "know" it needs FnResult until the call resolver tells
it. By that point, the function's throwness is already determined. A
nothrow function can't suddenly start producing FnResult values — the
codegen's function-level ABI setup doesn't support it.

The wrapper exists precisely because it IS a separate function with
`declared_can_throw=True`. It provides the can-throw context that
`ConstructResultOk` requires.

## 5. Design Options for Phase 4

### Option A: Make ConstructResultOk legal in nothrow functions (narrow)

Add `fnresult_ty` to `ConstructResultOk` (already prototyped). When set,
codegen uses the explicit type instead of the current function's return
type. This allows nothrow callers to construct FnResult for boundary
adaptation.

**Risk**: FnResult values in nothrow functions interact with string_arc,
SSA, and throw checks in untested ways. The try expression lowering
assumes FnResult only appears in can-throw contexts. Relaxing this
invariant could have hidden consequences.

### Option B: Emit can-throw wrapper inline as a nested block (medium)

Instead of a separate `__wrap_method::` function, emit the wrapper's
3-instruction body as inline blocks within the caller:

```
entry:
    Call(wrap, can_throw=False) → %raw
    br boundary_wrap
boundary_wrap:  [declared_can_throw context = True for this block]
    ConstructResultOk(%raw) → %fnres
    br try_dispatch
try_dispatch:
    ResultIsErr %fnres → ...
```

**Risk**: MIR doesn't have per-block throwness. Functions are either
can-throw or nothrow. Inline can-throw blocks in a nothrow function
would require a new MIR concept.

### Option C: Keep wrapper as a separate function, make it codegen-only (preferred)

Don't emit wrapper MIR. Instead:
1. Keep the wrapper **signature** (metadata only, no MIR body)
2. The call resolver still redirects to `__wrap_method::foo`
3. The caller sees a can-throw call, emits FnResult unwrapping normally
4. LLVM codegen, when it encounters a call to a wrapper that has no MIR
   body, generates the wrapper LLVM IR inline

This is the cleanest option because:
- No change to MIR semantics (`ConstructResultOk` stays in can-throw only)
- No change to try expression lowering
- No change to call resolver or type checker
- The only change is: wrapper MIR body is not synthesized; codegen
  generates the equivalent LLVM inline

**Implementation**: In `emit_module_ir`, when lowering a function call to a
wrapper that has no SSA/MIR, codegen generates the Call + FnResult wrap
inline in LLVM IR. The wrapper function definition is also generated at
the LLVM level (for the public symbol) but never exists in MIR.

## 6. Revised Phase 4 Plan (Option C)

### What changes

1. **Delete wrapper MIR synthesis** (driftc.py:7015-7046)
2. **Keep wrapper signatures** in `derived_signatures_by_id`
3. **Keep call resolver redirect** to `__wrap_method::` (unchanged)
4. **Keep type checker redirect** (unchanged)
5. **LLVM codegen**: when lowering a Call to a wrapper fn_id that has no
   MIR/SSA body, generate the wrapper LLVM IR inline:
   - Call the target nothrow function
   - Construct FnResult.Ok from the result
   - Continue with the FnResult value

### What stays true

- `can_throw` is never falsified
- `ConstructResultOk` only exists in can-throw functions
- Try expression semantics are unchanged
- Call resolver and type checker are unchanged
- MIR-level semantics are unchanged
- The wrapper is a codegen artifact, not a MIR entity

### What disappears

- Wrapper MIR bodies in `mir_funcs_by_id`
- `MethodWrapperSpec` in `Pass1State`
- Wrapper MIR going through string_arc, SSA, throw checks
- `forwarded_to_callee` in `param_drop_status`

### What's needed in codegen

When `_FuncBuilder` encounters a Call to a function that:
- Has `is_wrapper=True` in its signature
- Has `wraps_target_fn_id` pointing to the real function
- Has NO entry in `ssa_funcs` (no MIR body)

It generates:
```llvm
%raw = call <target_ret_ty> @"target_fn"(<args>)
%ok0 = insertvalue <fnresult_ty> zeroinitializer, i8 0, 0
%ok1 = insertvalue <fnresult_ty> %ok0, <target_ret_ty> %raw, 1
%ok2 = insertvalue <fnresult_ty> %ok1, ptr null, 2
; continue with %ok2 as the FnResult value
```

This is the same code the current LLVM wrapper bodies produce.

### Generic instantiation

K39 still creates wrapper signatures for generic instantiations. The
wrapper signature exists in `derived_signatures_by_id`. When codegen
encounters a call to this wrapper, it doesn't find SSA/MIR and generates
the inline wrapper LLVM. No change to K39 needed.

### Package MIR

Existing packages contain wrapper MIR. With "recompile the world" policy:
- New packages stop emitting wrapper MIR
- Consumer stops expecting wrapper MIR
- Consumer generates inline wrapper LLVM from wrapper signatures

## 7. Double-Wrapping Prevention

Double-wrapping (FnResult inside FnResult) cannot occur because:
1. The call resolver only redirects nothrow methods to wrappers
2. Can-throw methods are never redirected (they already return FnResult)
3. The wrapper IS can-throw, so its callers see FnResult directly
4. The wrapper calls the nothrow target — no FnResult nesting

## 8. Regressions

1. No `__wrap_method::` entries in `mir_funcs_by_id`
2. Cross-package nothrow method call still produces FnResult at caller
3. Generic instantiation wrappers work via inline codegen
4. Try expression semantics unchanged
5. Match-arm scrutinee type unchanged
6. All ABI boundary tests pass
7. Package consumer tests pass
