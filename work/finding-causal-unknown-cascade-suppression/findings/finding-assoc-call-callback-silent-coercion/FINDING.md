# Finding: assoc-call (`Type::fn`) Callback params bypass the canonical wrapper

Child of `finding-causal-unknown-cascade-suppression`; mandated by
review-2026-08-05T04-27-45Z ("the confirmed Site-1 Callback defect cannot
remain a follow-up").

## Defect

`resolve_nonvariant_qualified_static_call` admits a bare lambda or fn-typed
value at a concrete `Callback*` param through `coerce_args_for_params`'
silent INTERFACE retyping (the arg type is simply REPLACED by the param
type when the param is INTERFACE and the arg is not).  No
`core.callbackN(...)` wrapper is ever constructed, so lowering receives a
raw non-interface value under an interface-typed slot:

- bare lambda arg (`S::take_cb(|x: Int| nothrow => x + 1)`): checker-clean,
  invalid LLVM IR e2e — clang: "global variable reference must have pointer
  type" (reproduced identically on clean `git archive HEAD`, i.e.
  PRE-EXISTING);
- named-fn arg (`S::take_cb(add1)`): checker-clean, internal traceback —
  `NotImplementedError: interface impl not found for interface value` in
  `_ensure_interface_vtable` (ConstructIfaceValue over the raw fn value);
- arity-mismatched lambda (`Callback2` param, arity-1 lambda): ALSO
  checker-silent on the same path (fails only at clang).

Control: the FREE-function path (`take_cb(|x: Int| => ...)`) wraps
correctly and runs — the defect is specific to the associated/static-call
family (Site 1 of the implicit-callback-wrap matrix).

## refactor_triggers.md scan (2026-08-04)

No registered trigger covers callback-wrap site unification; the deliverable
stays a minimal root-cause repair routing Site 1 through the SAME canonical
wrapper authority as Sites 2/5/6 (`_try_wrap_arg_for_callback_field` →
`_implicit_callback_wrap`, the sole constructor).

## Fix shape

At the assoc-call success path in `resolve_call_expr` (before
`record_call_info`), for each concrete `Callback*` param with a bare-lambda
or fn-typed arg: run `_try_wrap_arg_for_callback_field`; WRAPPED → splice
into `expr.args`; REJECTED → poison (diagnostic already emitted); SKIP for
such an arg is an ARITY mismatch → a real diagnostic replaces the silent
acceptance (fails closed instead of emitting invalid IR).

## Pins

`lang/tests/driver/test_assoc_call_callback_wrap.py` — red-first: bare
lambda compile/RUN, named-fn compile/RUN, arity-negative checker
diagnostic (no clang error, no Traceback), free-fn control stays green.
