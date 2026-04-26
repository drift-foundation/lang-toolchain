# Plan: close the implicit `Callback*` / `CallbackThrow*` wrap UX gap

## Status
Plan only. No code edits.

## Problem

When a parameter / field / binding / return slot is statically typed as one
of the concrete callback interfaces — `Callback0/1/2`,
`CallbackThrow0/1/2` — the type system already knows exactly which wrapper
the value must be coerced into. In some call shapes the checker performs
that wrap implicitly today; in other shapes it does not, and the user is
forced to write `core.callbackN(...)` / `core.callback_throwN(...)` by hand.

This is a consistency gap, not a new language feature: there is already a
single coercion semantics; we just need to apply it everywhere a static
callback target type is visible.

### Failing shapes (hand-boxing required today)
1. Associated / static call: `Type::method(|x| => ...)` where the param is
   `Callback1<...>`.
2. Struct ctor field init: `S(cb = |x| => ...)` where field is
   `Callback1<...>` (positional or named).
3. Variant ctor field init: `Some(|x| => ...)` where the arm's field is a
   callback type.
4. Typed `let` initializer: `val cb: core.Callback1<Int, Bool> = |x| => ...`.
5. Return-position with declared callback return type:
   `fn f() -> core.Callback1<Int, Bool> { return |x| => ...; }`.
6. UFCS / qualified-trait method dispatch (`Trait::method(recv, |x| => ...)`)
   for a callback-typed param that isn't `Fn*`-trait-bounded.

### Working shapes today (the contract we must preserve)
A. Free function call, `name(|x| => ...)` where the resolved param is
   `Callback*`. Implemented by post-resolution wrap loop in
   `lang/driftc/checker/call_resolver.py:5883-5922` and the failure-fallback
   `_wrap_explicit_capture_callbacks` at `:5597-5627` / `:5649`.
B. Inherent method call, `recv.m(|x| => ...)`. Two-pass implicit wrap at
   `lang/driftc/checker/call_resolver.py:3123-3168` (primary) and
   `:3176-3211` (alt).
C. `Fn{0,1,2}`-trait-bounded generic param of a free fn: auto-wrap at
   `lang/driftc/checker/call_resolver.py:5862-5882`. Includes borrowed
   captures because escape is enforced later by the borrow checker.
D. Pre-scan that propagates the expected `Callback*` type into
   user-written `core.callbackN(lambda)` so the inner lambda's param types
   are concrete (`call_resolver.py:5513-5557`, free calls only).

## Existing machinery (the only mechanism — do not duplicate)

### Wrap construction (every site does it the same way)
```python
cb_var  = H.HVar(name="callbackN" / "callback_throwN", module_id="std.core")
cb_call = H.HCall(fn=cb_var, args=[arg_lambda], kwargs=[])
cb_call._is_implicit_wrap = True            # marker — see borrow checker
cb_call.callsite_id = ctx.alloc_callsite_id()
ctx.alloc_node_id(cb_call)
cb_call.expected_type_hint = param_ty       # so inner lambda gets concrete params
expr.args[idx] = cb_call
arg_types[idx] = type_expr(cb_call, expected_type=param_ty, used_as_value=False)
```

This block is currently inlined in three places (5883-5922, 3123-3168,
3176-3211) plus a `callback0..2`-only variant in
`_wrap_explicit_capture_callbacks` at 5597-5627. They diverge slightly —
the post-resolution version handles `CallbackThrow*` and fn-typed args;
the failure-fallback version does not. That divergence is part of what
makes the coverage uneven.

### Borrow / escape contract
- User-written `core.callbackN(lambda)` rejects borrowed captures up front
  (`call_resolver.py:4654-4665`) unless the call carries
  `_is_implicit_wrap`.
- Implicit wraps deliberately skip that check and let the borrow checker
  do escape analysis (`borrow_checker_pass.py:_is_callback_wrapper_call` /
  `_unwrap_callback_lambda` at ~`:54-75`, enforcement at ~`:2043-2090`).
- The borrow checker recognises a wrap by name + module, not by
  `_is_implicit_wrap`. So **every new implicit wrap site must use the
  same `module_id="std.core"` + `name="callbackN"/"callback_throwN"`
  shape** or borrow-time escape enforcement silently disappears. This is
  the load-bearing invariant for the patch.

## Proposed approach

### Step 1 — Extract a shared helper

Add `_implicit_callback_wrap(ctx, *, arg, param_ty, arg_ty) -> tuple[H.HCall|None, TypeId]`
to `lang/driftc/checker/call_resolver.py`, factoring the four divergent
copies (3123-3168 primary, 3176-3211 alt, 5597-5627 fallback, 5883-5922
post-resolution) into one. The helper:

- accepts an argument expression (`HLambda` or `HCall` returning a fn
  value) and the statically-resolved expected param type;
- returns `None` when the param is not a `Callback*` / `CallbackThrow*`
  interface, or the arg is neither a lambda nor a fn-typed value of the
  right arity;
- otherwise returns the freshly built `HCall` wrapper plus its TypeId,
  pre-typed against `expected_type=param_ty` so the inner lambda is
  concretised through the same code path as today;
- always sets `_is_implicit_wrap = True`, `expected_type_hint = param_ty`,
  `callsite_id`, `node_id`, and emits `module_id="std.core"`.

The four existing call sites become one-line uses of this helper. No
behavior change at A/B/C/D from Step 1 alone — that is the regression
guard for the refactor.

### Step 2 — Apply the helper at the gap sites

Each site below performs the same pattern: walk arg/field positions,
look up the statically expected callback type, call the helper, splice
the wrap back in, and let downstream lowering treat it identically to
the explicit case.

#### 2.1 Associated / static call — `resolve_nonvariant_qualified_static_call`
`lang/driftc/checker/call_resolver.py:517-603`

The candidate-loop `args_match_params` check at `:594` rejects
`fn → Callback*` because no wrap has been inserted yet. After the
candidate has at least proven receiver / arity compatibility, run the
helper across each `(arg, inst_params[idx])` pair and re-run
`args_match_params` once. On success, replace the corresponding
`expr.args[idx]` and the `arg_types[idx]` so the rest of the loop
(`coerce_args_for_params`, `record_call_info`) continues unchanged.

Mirror Path B's "alt pass" pattern: only attempt wrap if the first
match fails, so we don't perturb already-passing candidates.

#### 2.2 Struct ctor field init — `resolve_struct_ctor`
`lang/driftc/checker/call_resolver.py:1068-1286`

Two branches need the helper:

- positional, `:1228-1245`: when `_same_type(have, want)` fails and
  `want_def.kind is TypeKind.INTERFACE`, try the helper on
  `arg_exprs[idx]` against `want` **before** falling into
  `record_iface_coercion(...)`. If the helper produces a wrap, splice
  `arg_exprs[idx] = cb_call`, update `arg_types[idx]`, and `continue`
  (the wrap result is already typed against the field).
- named, `:1267-1284`: same shape, on `ctor_args[idx]` against
  `field_types[field_idx]`.

Why this matters: `record_iface_coercion` lowers via
`M.ConstructIfaceValue` (`stage2/hir_to_mir.py:2029-2056`), which
expects a struct value implementing the iface. Lambdas do not implement
`Callback*` — only the explicit wrap thunk does — so today a lambda
into a callback field either bottoms out at MIR-time as a malformed
upcast or produces broken codegen. The wrap must happen **before** the
iface_coercion fallback fires.

#### 2.3 Variant ctor field init — `resolve_unqualified_variant_ctor` and friends
`lang/driftc/checker/call_resolver.py:1289-1353` (and any companion
qualified-variant ctor resolver).

The function as it stands does not field-type-check; that happens at the
type_checker call site. Add the helper either here (so the rewrite is
visible to downstream lowering immediately) or at the type_checker
caller — whichever sees the per-field expected type. Pick one and pin it
in the patch comment to avoid both sides trying to wrap.

#### 2.4 Typed `let` initializer
`lang/driftc/type_checker.py:9125-9168`

When `declared_ty.kind is TypeKind.INTERFACE` and the inferred RHS is a
non-INTERFACE function-typed expression (lambda or fn-ref), run the
helper. On a wrap result, replace `stmt.value` and re-bind
`val_ty = declared_ty`. On no-wrap (declared type is not a `Callback*`),
fall through to today's `record_iface_coercion` path unchanged.

#### 2.5 Return-position
`lang/driftc/type_checker.py:9510-9540` (the `record_iface_coercion(stmt.value, return_type)` sites).

Same shape as 2.4: try the helper on the return expression against
`return_type` before the iface_coercion fallback.

#### 2.6 UFCS / qualified-trait method dispatch
`resolve_qualified_member_ufcs` (`lang/driftc/checker/call_resolver.py:3284+`)
and `resolve_method_call`'s trait-dispatch branches.

For trait methods with a non-`Fn*`-bounded `Callback*` param, the same
post-resolution wrap pass already used by inherent methods at 3123-3211
should be invoked once `param_type_ids` is known. Reuse the helper.

### Step 3 — Borrow / escape parity check

For each gap site, add a memcheck-aware regression that passes a
borrowed-capture lambda (e.g. `|x| captures(ref r) => ...`) where the
expected target is a `Callback*` field / static-fn arg / let init / etc.
The diagnostic must be the same as today's explicit
`core.callbackN(lambda_with_borrow)` rejection — i.e. enforcement happens
in the borrow checker via `_is_callback_wrapper_call`, not in
call_resolver. Do **not** copy the user-written check (`:4654-4665`)
into call_resolver; rely on the existing
`_is_implicit_wrap`-aware deferral.

### Step 4 — Doc / comment cleanup

- The four divergent inline-wrap blocks should be replaced with calls to
  the helper; the inline `# B2/B4: auto-wrap...` comment moves to the
  helper.
- Search for any `///` doc comment, AGENTS.md/CLAUDE.md note, or stdlib
  comment claiming "you must wrap with `core.callbackN`" for
  callback-typed params and remove the "must" framing. Keep
  `core.callbackN` documented as the explicit form for fn-references and
  ambiguous contexts.

## Out of scope (named, not promised)

- Function-pointer-typed locals (`let f: fn(Int) -> Bool = ...; f(42)`).
  These are not `Callback*` — different runtime shape, different
  coercion. No change.
- Generic `Callback*<T>` where `T` is unresolved. Path D (the pre-scan)
  already declines to propagate, and the post-resolution wrap only fires
  once the schema is concrete. Same constraint applies in the new sites:
  helper returns `None` if `expected_param` has unresolved type vars.
- Adding implicit wrap from a fn-reference `add1` in a context where it
  isn't already supported. The current free-fn / inherent-method paths
  handle the fn-typed-arg case; new sites should match that, but we are
  not extending the rule to cases that wouldn't work for a lambda either.

## Concrete patch sites (file:line, ordered)

Refactor first:
1. `lang/driftc/checker/call_resolver.py` new helper near the existing
   `_wrap_explicit_capture_callbacks` (above `:5597`).
2. `:3123-3211` — replace inherent-method primary + alt blocks.
3. `:5597-5627` — replace fallback `_wrap_explicit_capture_callbacks`
   body (preserve its outer "retry resolution" controller at `:5649`).
4. `:5883-5922` — replace post-resolution wrap loop.

Gap sites:
5. `:517-603` (`resolve_nonvariant_qualified_static_call`) — add
   wrap-and-retry pass before final `args_match_params` failure path.
6. `:1228-1245`, `:1267-1284` (`resolve_struct_ctor`) — wrap before
   iface_coercion fallback for callback-typed fields.
7. `:1289-1353` (`resolve_unqualified_variant_ctor`) — pick the
   right side (here or its caller) and apply helper.
8. `:3284+` (`resolve_qualified_member_ufcs`) and trait-dispatch in
   `resolve_method_call` — apply helper for non-`Fn*`-bounded callback
   params.
9. `lang/driftc/type_checker.py:9125-9168` and `:9510-9540` — try wrap
   before `record_iface_coercion` for callback-typed declared / return
   types.

## Regression-first test plan

All new tests live under `lang/tests/checker/` and `lang/tests/driver/`
beside the existing `test_callback_*` files. Run order: write tests,
land in the failing state, run the gate, then apply the fix.

### Positive (each must compile + run end-to-end)
- T1 `test_implicit_cb_static_fn`: `Type::take_cb(|x| => x + 1)` where
  `take_cb` is associated and param is `Callback1<Int, Int>`.
- T2 `test_implicit_cb_struct_field_pos` and `_kw`: `Holder(|x| => ...)`
  and `Holder(cb = |x| => ...)` for field `cb: Callback1<Int, Int>`.
- T3 `test_implicit_cb_variant_field`: `Some(|x| => ...)` where arm
  field is `Callback1<...>`.
- T4 `test_implicit_cb_let_typed`: `val cb: core.Callback1<Int, Bool> =
  |x| => x > 0;` followed by `cb.call(1)`.
- T5 `test_implicit_cb_return_position`: `fn make_cb() -> core.Callback1<Int, Int> { return |x| => x + 1; }`.
- T6 `test_implicit_cb_trait_method`: trait method whose param is
  `Callback1<...>` (non-`Fn*`-bounded), called with a bare lambda.
- T7 `test_implicit_cb_throw`: each of T1–T6 repeated with
  `CallbackThrow1` and a throwing lambda body.

### Negative (each must produce the same diagnostic as the explicit form does today)
- N1 borrowed capture into a `Callback*` field of an *escaping* sink
  (e.g. struct stored in a static or returned). Expected: borrow-checker
  rejection via existing `_is_callback_wrapper_call` path. No new
  diagnostic codes.
- N2 borrowed capture into a `Callback*` static-fn argument that escapes.
  Same.
- N3 lambda arity mismatch: `Callback2<...>` parameter, lambda with one
  param. Expected: arity diagnostic from the helper / inner wrap (same
  text as the explicit path produces today).
- N4 unresolved generic `Callback0<T>` in a static-fn signature with no
  expected-type pressure: helper declines, today's "cannot infer type
  arguments" diagnostic remains.

### Ledger / contract hygiene
- Ensure `_is_implicit_wrap` is set on every helper-produced `HCall`;
  add a single assertion-style test that spawns the helper directly and
  inspects the flag, so a future refactor can't drop the marker without
  noticing.

### Verification gate
- `PYTHONPATH=. pytest -n16 lang/tests/checker lang/tests/driver`
- `PYTHONPATH=. pytest -n16 lang/tests/borrow_checker` (regressions on N*)
- `PYTHONPATH=. pytest -n16 lang/tests/codegen/e2e/pex_e2e_runner.py`
  (full CLI parity — 1023+5 today)
- `PYTHONPATH=. pytest -n16 lang/tests/memcheck` — required because the
  positive tests exercise lambda-capture lifetimes through new wrap
  sites; matches the standing rule that authority work touching
  ownership-adjacent paths must run memcheck in the gate from the start.

## Risk register

- **R1 — duplicate wrap.** If both the call_resolver gap fix and a
  type_checker caller apply the helper to the same node, we get
  `core.callback1(core.callback1(lambda))`. Mitigation: helper checks
  `isinstance(arg, H.HCall) and arg.fn.name in {"callback0",...}` and
  returns `None` early. Add a test with an explicit
  `core.callback1(lambda)` already present in each new site.
- **R2 — `expected_type_hint` not honoured downstream.** The free-fn
  path retypes the wrap with `expected_type=param_ty` so the inner
  lambda gets concrete params. New sites must do the same; the helper
  enforces it by always passing `expected_type=param_ty` to its
  internal `type_expr` call.
- **R3 — borrow-checker escape rule silently bypassed.** If a new site
  wraps with a different `module_id` / `name`, `_is_callback_wrapper_call`
  doesn't match and borrowed-capture escapes go unchecked. Mitigation:
  helper is the only place those literals appear; grep guard
  `module_id="std.core"` + `name="callbackN"` after the refactor.
- **R4 — generic `Callback<T>` over-eager wrap.** Wrap before the
  schema is concrete corrupts inference. Helper must short-circuit
  when `expected_param`'s interface instance has any unresolved
  typevar (mirrors the existing `:5550-5557` predicate).
- **R5 — variant ctor field check duplicated.** Adding wrap inside
  `resolve_unqualified_variant_ctor` and at its caller would do the
  work twice. Pick exactly one site and pin it in a comment.

## Open questions

- Q1 — should the helper also accept `H.HField` / `H.HVar` referring to
  a free fn (the `add1` case)? The free-fn post-resolution loop already
  does this; mirroring it in new sites adds value but slightly enlarges
  the surface. Default: yes for static-fn args and struct fields (where
  `Holder(cb = add1)` is a natural ergonomics request). Defer for variant
  ctor and return-position until a real demand surfaces.
- Q2 — should the helper return diagnostics on shape mismatch or just
  return `None`? Default: return `None`; let the existing
  "no-overload" / "type mismatch" diagnostic fire so the message is
  identical to today.
- Q3 — Is there an `iface_coercion`-vs-wrap precedence rule we should
  surface to a user-readable doc note? Probably no; the rule "wrap if
  source is a function value and target is `Callback*`" is internal.

## Effort estimate

- Step 1 refactor: ~1 day (4 inline blocks → one helper, behavior-preserving).
- Step 2 gap sites: ~2-3 days (6 sites, one shape, but each lives in a
  slightly different resolver and needs its own retry-on-failure plumbing).
- Step 3 borrow parity: ~0.5 day (mostly tests).
- Step 4 doc/comment cleanup: ~0.5 day.
- Verification gate (full pex_e2e + memcheck + borrow_checker): ~1
  cycle.

Total: small-to-medium; aligns with the user's read.
