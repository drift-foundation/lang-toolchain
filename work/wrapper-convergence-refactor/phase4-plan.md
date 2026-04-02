# Phase 4: Wrapper MIR Cleanup

## Status: design complete, split into 4A-prereq/4A/4B
## Updated: 2026-04-02

## Split

**4A-prereq**: Extract `_llvm_type_for_typeid` (or minimal subset) to
module level in LLVM codegen. Mechanical refactor, no semantic change.
Enables standalone wrapper LLVM emission without a `_FuncBuilder` context.

**4A**: Remove wrapper MIR bodies from the semantic pipeline. Wrapper
fn_ids still exist as MIR call targets. Codegen emits wrapper LLVM inline
using the module-level type mapping. Call resolver and type checker
unchanged.

**4B** (future): Remove wrapper MIR call targets. Requires resolving the
try / ConstructResultOk interaction (see phase4-try-interaction.md).

---

## 4A-prereq: Module-Level Type Mapping

### Problem

`_llvm_type_for_typeid` is on `_FuncBuilder` (per-function). It requires
a MIR function and SSA context to initialize. Emitting wrapper LLVM
without a MIR body requires type mapping at the module level.

### Scope

Extract the TypeId → LLVM type mapping logic to `LlvmModule` or a shared
utility that `_FuncBuilder` delegates to. The function-level builder
continues to work as before (delegates to the shared mapping).

### What changes

- `LlvmModule` or a new `TypeMapper` gets the type mapping methods:
  - `_llvm_type_for_typeid(ty_id) -> str`
  - `_llty(raw) -> str` (the emit-prefix helper)
  - `_is_void_typeid(ty_id) -> bool`
  - `_llvm_ok_type_for_sig(sig) -> (str, str)`
- `_FuncBuilder._llvm_type_for_typeid` delegates to the shared mapper
- The shared mapper is initialized once per module, not per function

### What must NOT change

- No semantic behavior change
- No LLVM IR output change for any existing test
- No new features or wrapper logic
- Wrapper MIR bodies still exist and are lowered normally

### Regressions

- All codegen tests pass with identical IR output
- `_FuncBuilder._llvm_type_for_typeid` still works for all existing call
  sites
- No new type mapping failures for any TypeId that was previously handled

### Size

~50 lines. Mechanical extraction. May require moving some cached state
from `_FuncBuilder` to `LlvmModule`.

---

## 4A: Remove Wrapper MIR Bodies

### Prerequisite

4A-prereq must be landed and regression-clean.

### Scope

- Delete the wrapper MIR synthesis loop (driftc.py:7015-7046)
- Keep wrapper signatures in `derived_signatures_by_id`
- Keep call resolver redirect to `__wrap_method::` (unchanged)
- Keep type checker redirect (unchanged)
- LLVM codegen emits wrapper LLVM inline using module-level type mapping
  when encountering a wrapper fn_id with no MIR/SSA body

### What this does NOT change

- Wrapper fn_ids still exist as MIR call targets
- Call resolver still redirects to `__wrap_method::`
- Type checker still redirects to wrapper
- `ConstructResultOk` stays in can-throw functions only
- Try expression semantics unchanged
- `is_wrapper` / `method_wrapper_by_target` / `MethodWrapperSpec` remain

### What this eliminates

- Wrapper MIR bodies going through string_arc, SSA, throw checks
- `forwarded_to_callee` in `param_drop_status`
- ~30 lines of wrapper MIR synthesis code

### Implementation

When codegen encounters a Call to a fn_id that:
- Has `is_wrapper=True` in its fn_info signature
- Has `wraps_target_fn_id` pointing to the real function
- Has NO entry in `funcs` / `ssa_funcs`

It emits inline LLVM using the module-level type mapper:
```llvm
%raw = call <ret_ty> @"target_fn"(<args>)
%ok0 = insertvalue <fnresult_ty> zeroinitializer, i8 0, 0
%ok1 = insertvalue <fnresult_ty> %ok0, <ret_ty> %raw, 1
%ok2 = insertvalue <fnresult_ty> %ok1, ptr null, 2
ret <fnresult_ty> %ok2
```

### Regressions

1. No `__wrap_method::` entries in `mir_funcs_by_id` after lowering
2. Cross-package nothrow method call via wrapper still produces FnResult
3. Generic instantiation wrappers work via inline codegen
4. Try expression semantics unchanged
5. All ABI boundary tests pass
6. Package consumer tests pass

### Additional requirement discovered during implementation

The package consumer BFS (`_build_package_consumer_unit`, driftc.py:2126)
discovers target functions by walking wrapper MIR instruction references.
Without wrapper MIR bodies, the BFS can't find the targets. The BFS must
be updated to seed wrapper targets from `wraps_target_fn_id` on wrapper
signatures, not from MIR Call instructions.

Also: FnResult type key sanitization — `type_key_string` can produce keys
with `<`, `>`, `::` that are invalid in LLVM type names. The inline
wrapper emission must sanitize these.

### Size

~100 lines across 3-4 files (MIR deletion + inline LLVM emission + BFS
update + key sanitization).

---

## 4B (future): Remove Wrapper MIR Call Targets

### What it would change

- Call resolver stops redirecting to `__wrap_method::`
- Type checker stops redirecting
- `method_wrapper_by_target` removed
- `MethodWrapperSpec` removed from `Pass1State`
- MIR call targets are always the real function

### Why it's separate

Requires resolving the `ConstructResultOk` in nothrow functions problem
(see phase4-try-interaction.md). The wrapper fn_id exists as a MIR call
target precisely because it provides a can-throw context for FnResult
construction. Removing it requires either:
- Making ConstructResultOk legal in nothrow functions
- Or a new MIR representation for boundary adaptation

### Disposition of remaining wrapper identity

| Item | 4A-prereq | 4A | 4B |
|------|-----------|----|----|
| Type mapping per-function | **Extracted** | N/A | N/A |
| Wrapper MIR bodies | Unchanged | **Removed** | N/A |
| Wrapper fn_ids in MIR calls | Unchanged | Kept | **Removed** |
| `is_wrapper` on signatures | Unchanged | Kept | Removed |
| `method_wrapper_by_target` | Unchanged | Kept | **Removed** |
| `MethodWrapperSpec` | Unchanged | Kept | **Removed** |
| Call resolver redirect | Unchanged | Kept | **Removed** |
| Type checker redirect | Unchanged | Kept | **Removed** |
