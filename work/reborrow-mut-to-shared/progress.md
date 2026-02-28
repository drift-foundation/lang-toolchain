# Reborrow &mut T → &T: Progress Log

## Phase 0: Research — understanding current implementation

### Type representation
- `&T` and `&mut T` both use `TypeKind.REF`, distinguished by `TypeDef.ref_mut` (bool).
- `TypeTable.ensure_ref(inner)` → `&T`, `ensure_ref_mut(inner)` → `&mut T`. Different TypeIds.

### Current checking layers (call-site argument matching)

| Layer | File:line | `&mut T` → `&T`? | Notes |
|---|---|---|---|
| Shallow checker `check_call_signature` | checker/__init__.py:2344 | No | Hard TypeId equality at line 2406 |
| Deep `_args_match_params` | type_checker.py:1595 | No | No mutability downgrade |
| Deep `_apply_autoborrow_args` | type_checker.py:1619 | Only via Borrow trait | `_try_borrow_coerce` path |
| Method receiver `_receiver_compat` | type_checker.py:3804 | **Yes** | Explicitly handles it |
| Unification | type_checker.py:3421 | No | `ref_mut != ref_mut` → conflict |

### Key insight
**Receiver coercion already exists** at type_checker.py:3804-3807 for `&mut T` → `&T`. The gap is only in regular function argument position. This means the type system conceptually allows it — just not wired up for non-receiver args.

### Touch points identified
1. `check_call_signature` (checker/__init__.py:~2406) — shallow checker, TypeId equality. Must allow RefMut<T> where Ref<T> expected.
2. `_args_match_params` (type_checker.py:~1607) — deep match predicate. Must accept the coercion.
3. `_apply_autoborrow_args` (type_checker.py:~1619) — coercion engine. Must handle the case (no HIR rewrite needed — just accept the type).
4. Possibly `_can_borrow_coerce` in call_resolver.py:~3559.

### Lowering question
Does MIR/codegen need a reborrow node, or can it accept `&mut T` as-is where `&T` is expected? Since both are pointer-typed in LLVM IR (`T*`), the codegen likely doesn't care — both are just pointer values. The distinction is semantic (checker-level) not representational.

## Phase 1: Regression tests — discovery

### Finding: Both cases already work

Created regression tests and ran them. Both pass on the current compiler without any changes:

1. **Positive case** (`reborrow_mut_to_shared_call_site`): `takes_shared_ref(&mut f)` compiles and runs. The `_try_borrow_coerce` path in `_apply_autoborrow_args` (type_checker.py:1780) synthesizes an `arg.borrow()` call, which resolves because receiver coercion (type_checker.py:3804) allows `&mut T` receivers on methods expecting `&T` receivers. **Already works.**

2. **Return-position case** (`reborrow_mut_to_shared_return_rejected`): `fn as_shared_ref(f: &mut Foo) -> &Foo { return f; }` also compiles and runs. **Also already works** — the plan says it should be rejected, but the current compiler accepts it.

### Tested scenarios that all pass:
- Basic `&mut T` → `&T` at call site
- Two args, both `&mut` where `&` expected
- Mixed `&` and `&mut` args
- `&mut String` where `&String` expected
- Return-position `&mut T` → `&T`

### Extended testing — all scenarios pass

Tested additional scenarios that also all compile and run:
- `process(ctx)` where `ctx: &mut Ctx` and `process` expects `&Ctx` (bare variable, not `&mut expr`)
- `handler.call(&mut req)` where callback expects `&Request`
- `val shared: &Foo = &mut f;` (local assignment)
- `fn identity_ref(f: &Foo) -> &Foo { return f; }` called with `&mut f`
- Chained/nested reborrow patterns

### Mechanism
The `_try_borrow_coerce` path in `_apply_autoborrow_args` (type_checker.py:1780) synthesizes `arg.borrow()` method calls. The receiver coercion at type_checker.py:3804 allows `&mut T` on `&T` self-param methods. This makes the synthesized `.borrow()` resolve successfully, so the coercion works implicitly via the existing Borrow trait mechanism + receiver coercion.

For return-position and assignment-position, the type checker appears to accept `&mut T` where `&T` is expected because they share the same LLVM representation (both are `T*` pointers) and the unification path may have a permissive fallback.

### Correction: farm compiler rejects all cases

The Phase 1 testing was done against the local dev branch, which may have had partial support already. User confirmed with 4 isolated repros that on the installed/farm compiler, **all** of these are rejected:

1. `cb.call(&mut f, ...)` where `cb` is `Callback2<Int, &Foo, Int>` → rejected
2. Wrapper closure calling `h.call(f, ...)` inside → rejected
3. Indirect helper function forwarding to `.call()` → rejected
4. Direct `takes_ref(&mut f)` with no Callback2 → also rejected

Root cause: the shallow checker and deep type checker both do hard TypeId equality between `&mut T` and `&T`, so neither recognizes the coercion. The `_try_borrow_coerce` path only works when the existing Borrow trait mechanism + receiver coercion can kick in, which depends on the checker not rejecting the call first.

## Phase 2: Implementation

### Changes made

**Touch point 1: Shallow checker** (`checker/__init__.py:2413-2421`)

Added reborrow coercion in `check_call_signature`, right after the TypeId inequality check and before the interface coercion check:

```python
# Implicit reborrow: allow &mut T where &T is expected.
if (
    arg_def is not None and param_def is not None
    and arg_def.kind is TypeKind.REF and param_def.kind is TypeKind.REF
    and arg_def.ref_mut is True and param_def.ref_mut is False
    and arg_def.param_types and param_def.param_types
    and arg_def.param_types[0] == param_def.param_types[0]
):
    continue
```

This prevents the shallow checker from emitting a false type-mismatch diagnostic when `&mut T` is passed where `&T` is expected.

**Touch point 2: Deep type checker** (`type_checker.py:1616-1623`)

Added matching logic in `_args_match_params` (overload resolution predicate), using the same condition:

```python
# Implicit reborrow: &mut T matches where &T is expected.
if (
    arg_def.kind is TypeKind.REF and param_def.kind is TypeKind.REF
    and arg_def.ref_mut is True and param_def.ref_mut is False
    and arg_def.param_types and param_def.param_types
    and arg_def.param_types[0] == param_def.param_types[0]
):
    continue
```

This allows `_args_match_params` to accept `&mut T` arguments where `&T` parameters are expected, so overload resolution doesn't reject the match. The actual coercion at the LLVM level is a no-op since both `&T` and `&mut T` are `T*` pointers.

**Touch point 3: Call resolver — interface method dispatch** (`call_resolver.py:1536-1545`)

The primary type-checking for Callback/interface `.call()` happens in `resolve_method_call`, not in `check_call_signature`. This inline check runs during the typecheck phase, before `_validate_calls`. Added the same reborrow coercion:

```python
# Implicit reborrow: allow &mut T where &T is expected.
arg_def = ctx.type_table.get(arg_ty)
param_def = ctx.type_table.get(param_ty)
if (arg_def.kind is TypeKind.REF and param_def.kind is TypeKind.REF
        and arg_def.ref_mut is True and param_def.ref_mut is False
        and arg_def.param_types and param_def.param_types
        and arg_def.param_types[0] == param_def.param_types[0]):
    continue
```

**Touch point 4: Call resolver — function-value `.call()` dispatch** (`call_resolver.py:1492-1501`)

Same inline type-equality check for `recv_nominal_def.kind is TypeKind.FUNCTION` path. Added the same coercion.

### Why these four touch points are needed

1. **Shallow checker `check_call_signature`** — secondary validation pass on all call nodes. Without the coercion, emits a diagnostic even if earlier phases accepted the call.
2. **`_args_match_params`** — overload resolution predicate. Must return `True` or the call candidate is rejected.
3. **`resolve_method_call` interface dispatch** — primary type-check for `cb.call(...)` on Callback/interface types. Runs during the typecheck phase, before `check_call_signature`. This was the root cause of the farm failure.
4. **`resolve_method_call` function-value dispatch** — same for bare function-value `.call()`.
5. **`_apply_autoborrow_args`** does not need changes — once the above gates allow the match, it handles coercion via the existing `_try_borrow_coerce` path.

### Testing — all pass

**Positive regression tests:**
- `reborrow_mut_to_shared_call_site` — direct fn calls with `&mut T` where `&T` expected (6 sections)
- `reborrow_mut_to_shared_callback` — Callback1/Callback2 dispatch with `&mut T` where `&T` expected (4 sections)
- User's `/tmp/drift-cb-reborrow` and `/tmp/drift-cb-reborrow2` repros — both pass

**Existing regression tests (no breakage):**
- `borrow_coerce_combo_ok` — existing borrow coercion patterns
- `borrow_coerce_combo_rejected` — rejection cases still rejected
- `borrow_reborrow_mut` — mut reborrow semantics
- `borrow_reborrow_mut_requires_mut_ref_rejected` — rejection still works
- `borrow_struct_field_param_mut_reborrow_rejected` — rejection still works
- `callable_borrowed_capture_callback_boxing_rejected` — rejection still works
- `callable_callback_arity_mismatch` — arity checks unaffected
- `callable_callback_in_array` — callback in array semantics
- `callable_composed_callbacks` — composed callbacks
- `callback_arc_mutex_full_mutation` — arc/mutex callback patterns
- `autoborrow_field_receiver` — field autoborrow
- `callback0_void_arity` — void arity callbacks
- `match_qualified_binder_local` — match binder (recent fix)
- `callback_move_capture_nested_callback` — nested move capture
- `callable_callback_returned_from_generic` — generic callback return

## Phase 3: Negative regression — return-position finding

### Plan expectation vs reality

The plan (§2.2, §4.2) specifies that `fn as_ref(x: &mut Foo) -> &Foo { return x; }` should remain rejected, and a negative regression should pin that rejection.

**Finding:** The return-position coercion is **already accepted** by the compiler — both before and after this patch. Verified by reverting the patch (`git stash`) and testing the exact plan example:

```drift
fn as_ref(x: &mut Foo) nothrow -> &Foo {
    return x;
}
```

This compiles and runs on the unmodified compiler. The acceptance comes from the pre-existing `_try_borrow_coerce` / receiver coercion path — not from this patch's changes.

Similarly, assignment-position `val r: &Foo = &mut f;` also compiles on the unmodified compiler.

### Consequence

A negative regression test pinning return-position rejection cannot be written because the rejection doesn't exist. The plan's assumption (§2.2) that return-position is currently rejected was incorrect.

**This patch does NOT widen the coercion surface for return-position or assignment-position.** Those paths were already accepted before the patch. The patch only makes the call-argument path work through the Callback interface dispatch, where `_try_borrow_coerce` couldn't reach because the shallow checker and `_args_match_params` were rejecting the match first.

### Decision needed (for plan owner)

Options:
1. Accept that return-position coercion is already supported and document it as-is (no negative regression needed).
2. File a separate task to implement return-position rejection if the language spec requires it. That would be a restriction (breaking change) and is independent of this call-site patch.
3. Write a negative regression for a different case that IS rejected (e.g., `&T` → `&mut T` direction, which correctly remains rejected).

### What IS correctly rejected (verified)

Taking `&mut` of a field through a shared reference is correctly rejected. For example, `val p = &mut (*f).x;` where `f: &Foo` produces: "cannot take &mut through *p unless p is a mutable reference". This is the fundamental soundness invariant — shared references don't allow mutable sub-borrows.

**Negative regression test:** `reborrow_mut_through_shared_ref_rejected` pins this rejection.

## Phase 4: Design note

### Semantic contract: `&mut T` → `&T` implicit reborrow

**Supported (as of this patch):**
- **Call-site argument position**: `fn f(x: &T)` accepts `&mut T` arguments. Works for direct function calls and through Callback1/Callback2/CallbackThrow interface dispatch.
- **Return position**: `fn f(x: &mut T) -> &T { return x; }` compiles. (Pre-existing, not introduced by this patch.)
- **Assignment position**: `val r: &Foo = &mut f;` compiles. (Pre-existing, not introduced by this patch.)

**Not supported (correctly rejected):**
- **Mutable sub-borrow through shared ref**: `&mut (*f).x` where `f: &Foo` is rejected with "cannot take &mut through *p unless p is a mutable reference". Shared references cannot be used to create mutable sub-borrows of their fields.

**Mechanism:**
- At the checker level, the coercion is recognized by comparing `TypeKind.REF` + `ref_mut` flags + inner type equality.
- At the lowering level, no transformation is needed — both `&T` and `&mut T` are `T*` pointers in LLVM IR.
- The coercion is non-escaping in spirit: it temporarily views a mutable reference as shared. The mutable reference remains valid and the caller retains ownership.

**Scope of this patch:**
- Four checker gates were opened: `check_call_signature` (shallow), `_args_match_params` (deep overload resolution), `resolve_method_call` interface dispatch, and `resolve_method_call` function-value dispatch.
- No changes to `_apply_autoborrow_args`, `_can_borrow_coerce`, return-type checking, assignment checking, or unification.
- The call-site coercion now works uniformly: direct calls, method calls, and interface dispatch (Callback/CallbackThrow).
