# Boundary Wrapper Convergence Refactor

## Status: approved direction, ready for first slice
## Date: 2026-04-01 (updated post-0.27.137 certification)

## Policy: no backward compatibility required

We can rebuild the world. Small ecosystem, no external consumers that need
legacy wrapper routing preserved. The goal is the cleanest converged
architecture, not minimal churn. Existing divergent paths are deleted, not
preserved.

---

## 1. Problem Statement

The compiler has two semantic paths for calling the same pub function:

- **Direct** (same-module or source-compiled stdlib): caller gets the
  surface return type, no FnResult wrapping
- **Wrapper** (cross-module or package-consumed stdlib): caller gets
  FnResult<T, Error>, boundary ABI wrapping/unwrapping

Which path is taken depends on `source_modules` — a set populated based on
whether stdlib is source or package. This is the root cause of every
mode-divergence bug in the 0.27.132–0.27.137 cycle:

| Version | Bug | Wrapper-path cause |
|---------|-----|--------------------|
| 0.27.132–0.27.135 | Double-drop / missing-drop | `has_drop` differs between paths due to different TypeIds |
| 0.27.137 | Use-after-move in match arm | `copy_status` differs because wrapper path creates different scrutinee type |

The fix is not more point-patches. It's eliminating the two-path divergence.

---

## 2. Target Architecture

### Principle

**Pub boundary contract is a property of the declaration, not a per-call-site
routing decision.** After artifact ingress, the compiler sees one semantic
path for calling any pub function. Mode affects inputs and packaging, not
compiler meaning.

### Concrete design

1. **Every pub function has a `boundary_abi` recorded at declaration time.**
   For can-throw: the return type is `FnResult<T, Error>`. For nothrow
   methods with pub wrappers: same. For non-pub: no boundary (internal only).

2. **MIR always uses the boundary ABI for cross-module pub calls.** The
   match scrutinee type is always the same regardless of mode. No
   `source_modules` check.

3. **LLVM codegen applies a transparent optimization**: for same-module
   calls to known functions, codegen may call `__impl` directly and
   construct the FnResult locally. This optimization is invisible to MIR.

4. **`__wrap_method::` exists only in LLVM IR.** No wrapper MIR. No wrapper
   function IDs in the MIR pipeline. No `param_drop_status` for wrappers.

5. **`source_modules` is removed from call routing entirely.** Routing
   depends only on: is the callee pub? Is the caller in the same package?

### What gets deleted

- `source_modules` set on TypeTable (routing use only; diagnostic use can stay)
- `explicitly_packaged_modules` in routing logic
- `_resolve_call_target_symbol` mode-conditional branches
- `__wrap_method::` MIR synthesis (driftc.py:6961+)
- `_inject_method_boundary_wrappers` as a MIR-pipeline concern (keep for
  signature metadata only)
- `MethodWrapperSpec` as a MIR concept
- `forwarded_to_callee` in `param_drop_status` (no wrapper MIR = no need)
- The `is_cross_module` late decision based on `source_modules`

### What stays

- `__impl` rename in LLVM codegen (performance optimization)
- Public wrapper emission in LLVM codegen (ABI boundary)
- `FnResult<T, Error>` internal type (can-throw return convention)
- `boundary_abi` on FnSignature (the new source of truth)
- Package signature serialization of wrapper metadata

---

## 3. Phased Plan

Under the "no backward compat" policy, the original 5 phases collapse to 3.
Phases A+B merge (no need for a metadata-only phase if we're immediately
using it). Phases C+D merge (no need to keep wrapper MIR around while
removing `source_modules`).

### Phase 1: Boundary ABI on signature + MIR normalization

**Goal**: Every pub function carries `boundary_abi` on its FnSignature. MIR
call lowering uses this for all cross-module pub calls. `source_modules` is
no longer consulted for return type selection.

**Changes**:
- `checker/__init__.py`: add `boundary_ret_type_id: TypeId | None` to
  FnSignature. Populated for all pub functions that have (or would have)
  wrappers.
- `hir_to_mir.py` / call info: when lowering a call to a pub function from
  a different module, use `boundary_ret_type_id` as the call's return type.
  The call result is always FnResult-shaped in MIR.
- `driftc.py`: populate `boundary_ret_type_id` during signature
  construction, using the same logic as `_inject_method_boundary_wrappers`
  but without creating wrapper FunctionIds.

**What's deleted**:
- The return-type divergence between source and PEX paths
- The match-arm scrutinee type divergence that caused 0.27.137

**Regressions**:
- Source and PEX MIR for `match rest.start(...)` produces the same
  scrutinee type
- `copy_status` and `has_drop` return the same answers in both modes
- All existing e2e tests pass (they already handle FnResult returns)

**Key risk**: same-module calls to pub functions now see FnResult in MIR.
The codegen optimization must correctly unwrap for same-module calls.
Existing tests will catch this.

**Size**: Medium. This is the core behavioral change.

---

### Phase 2: Remove `source_modules` routing + delete wrapper MIR

**Goal**: LLVM codegen routing depends only on `boundary_ret_type_id`
presence and package membership. `__wrap_method::` wrapper MIR is deleted.
Wrapper symbols exist only in LLVM IR.

**Changes**:
- `llvm_codegen.py`: simplify `_resolve_call_target_symbol` — if callee has
  `boundary_ret_type_id` and caller is in a different package, use boundary
  ABI. Same-package calls use `__impl` optimization.
- `driftc.py`: remove wrapper MIR synthesis loop (line 6961+). Remove
  `MethodWrapperSpec` from MIR pipeline. Remove `_wrapper_fn_ids`.
- `driftc.py`: remove `source_modules` from routing-relevant code paths.
  Keep for diagnostics only.
- `mir_nodes.py`: remove `forwarded_to_callee` from `param_drop_status`
  (no wrapper MIR means no wrapper params).

**What's deleted**:
- ~50 lines of wrapper MIR synthesis
- ~30 lines of `_resolve_call_target_symbol` mode-conditional logic
- `MethodWrapperSpec` as a concept in the MIR pipeline
- `source_modules` / `explicitly_packaged_modules` routing checks

**Regressions**:
- All e2e tests pass
- Cross-package calls always use boundary ABI
- Same-package calls always use `__impl`
- No `__wrap_method::` FunctionIds in MIR

**Size**: Medium. Mostly deletion.

---

### Phase 3: Strict invariants + assertion hardening

**Goal**: Make the converged architecture enforceable. Any violation is a
compiler error.

**Changes**:
- Assert: every pub function with `declared_can_throw=False` and
  `is_method=True` has `boundary_ret_type_id` set
- Assert: MIR return type for a cross-module pub call matches
  `boundary_ret_type_id`
- Assert: `copy_status` for any type appearing in a pub function signature
  returns the same answer regardless of compilation mode (tested via
  cross-reference against package-consumed signatures)
- Remove the `param_drop_status` disagreement diagnostic (0.27.136) — it
  becomes unnecessary because the MIR path is unified

**What's deleted**:
- `_postdrop_check_param_drops` (the post-pass diagnostic) — the
  divergence that motivated it can no longer occur
- The `_copy_cache_structural` pre-MIR-lowering clear — no longer needed
  because TypeId universes are unified

**Size**: Small. Cleanup.

---

## 4. What Assertions Should Hold

After Phase 2:

1. **No `source_modules`-dependent routing.** The routing rule is: pub +
   different package → boundary ABI. Period.
2. **MIR return type for pub cross-module calls is always
   `boundary_ret_type_id`.** Never the surface type.
3. **`copy_status(T)` is mode-independent** for any type T that appears in
   a pub function signature.
4. **`has_drop(T)` is mode-independent** for any type T that appears in a
   pub function signature.
5. **No `__wrap_method::` FunctionIds in MIR.** Wrapper symbols are
   codegen-only.
6. **Match-arm scrutinee type for a pub function result is the same in
   source and PEX builds.**

---

## 5. Non-Goals

- No legacy routing preservation
- No compatibility shims for old wrapper behavior
- No `source_modules`-conditional semantics in any form
- No keeping both direct and wrapper MIR paths
- No wrappers whose MIR existence depends on compilation mode

---

## 6. Recommended First Slice

### Investigation finding: MIR is already the same

During implementation I discovered that the MIR for `rest.start()` calls
is already identical in source and PEX builds. The call lowering in
`hir_to_mir.py` produces the same return type (surface Result) in both
modes. The `call_abi_ret_type` function operates on `CallSig.can_throw`
which is determined by the callee's signature, not by the compilation mode.

The divergence that caused 0.27.137 was not in the MIR but in:
1. **Type table state**: different TypeIds from package linking →
   `copy_status` structural fallback giving different answers
2. **LLVM codegen routing**: `_resolve_call_target_symbol` choosing
   wrapper vs `__impl` based on `source_modules`

The codegen routing difference produces different LLVM IR (FnResult
wrapping/unwrapping overhead) but doesn't change the MIR-level semantics.
The type-table divergence was fixed in 0.27.137 (`copy_status` checking
`destructor_fns`).

### Revised first slice: boundary_ret_type metadata + codegen routing normalization

Since the MIR is already normalized, the first slice should target the
LLVM codegen routing:

1. **Add `boundary_ret_type_id` to FnSignature** (~10 lines, metadata)
2. **Populate it in `_inject_method_boundary_wrappers`** (~10 lines)
3. **Simplify `_resolve_call_target_symbol`**: replace
   `callee_mod not in source_modules` with
   `callee_sig.boundary_ret_type_id is not None` for routing decisions
   (~20 lines)

This removes `source_modules` from the routing path, making codegen
mode-independent. The `__impl` optimization still applies for same-module
calls but the decision no longer depends on which modules are source vs
package.

The regression test: compile the same code with `--stdlib-root` and
without (PEX stdlib), compare the LLVM IR structure — both should produce
the same wrapper routing for cross-package calls.
