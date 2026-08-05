# Child finding: Callback slots record interface labels without constructing values

Date filed: 2026-08-05

Parent: `finding-causal-unknown-cascade-suppression`.

Status: confirmed `LANGUAGE_BUG`, planning-ready, shared implementation blocked
on the active full-suite gate. `PROGRESS.md` is implementer-owned and absent.

## Reproducer

```drift
val cb: core.Callback1<Int, Int> = |x: Int| nothrow => x;
return cb.call(7) - 7;
```

K's localized full-compile probe reaches MIR with a raw HLambda and reports:

```text
MIR lowering contract failure (raw HLambda reached HLet lowering (checker bug))
```

This is an internal boundary failure on accepted source.

A second producer was confirmed during parent planning round 2:

```drift
fn take_cb(cb: core.Callback0<Int>) nothrow -> Int { return cb.call(); }
val f = || => { 7 };
return take_cb(f) - 7;
```

The pending binding is stamped as Callback without a wrapper and MIR reports a
move from uninitialized interface local `f`. The two reproducers are one
boundary defect: a Callback TypeId is being used as a substitute for the
required callback-object construction.

## Existing contract settles wrap versus rejection

No new language ruling is required:

- `doc/history.md`, 2026-04-26 / release 0.31.17, explicitly added implicit
  Callback/CallbackThrow wrapping at typed-let initializer Site 5.
- `lang/tests/driver/test_implicit_callback_wrap.py`,
  `test_site5_typed_let_bare_lambda_to_callback1`, pins that exact source as a
  clean positive.
- the same history entry states the wrapper must be built before raw interface
  coercion and must preserve borrowed-capture rejection and no-double-wrap.

The current regression test did not protect full lowering strongly enough.
The fix restores the wrap; clean rejection would replace an existing language
contract and is out of scope without an explicit spec-change request.

## Likely mechanisms

Typed HLet passes the declared Callback interface as `expected_type` into
`type_expr(HLambda)`. The lambda path returns that expected interface type, so
the later initializer mismatch block sees `inferred_ty == declared_ty` and
never calls `_try_callback_wrap_for_iface_slot`. The raw HLambda survives on
the HLet even though its type side table claims Callback.

This violates the checker/lowering rule: a semantic conversion that affects
lowering must be represented in HIR or a consumed node-level mark.

For the argument form, pending-lambda resolution receives the Callback
expectation and appears to install that interface type on the original HVar.
That destroys the required representation distinction: a captureless stored
lambda is a thin function pointer; the argument slot must contain the
synthesized callback wrapper.

## Required fix properties

- detect direct HLambda + concrete Callback/CallbackThrow slots before
  equality can bypass wrapping;
- route through the existing callback-wrap authority; do not construct an
  ad-hoc wrapper in MIR;
- replace `stmt.value` with the synthesized HCall and type that node;
- distinguish WRAPPED / REJECTED / SKIP, preserving the existing rejection
  short-circuit;
- avoid double wrapping explicit `core.callbackN(...)` initializers;
- keep borrowed-capture/throw-mode/arity diagnostics on their established
  authorities;
- make the pending captureless HVar alias path converge on the same typed-let
  wrapper after it finalizes to a thin function type.
- make bare pending-HVar arguments finalize the binding to a thin function and
  then splice the same wrapper into the argument slot;
- audit direct/free/static/method argument routes for interface-label-only
  acceptance rather than patching only the observed route.

## Tests

Prefer a new regression file for executable/structural coverage:

1. full compile and run of the direct typed-let source (exit 0);
2. post-check HIR assertion that HLet.value is the synthesized callback HCall;
3. run the existing Site-5 positive, explicit no-double-wrap, capture-copy,
   CallbackThrow, and borrowed-capture guards unchanged;
4. add an alias companion in the pending-finalization child once that path is
   implemented.
5. add the pending-HVar Callback argument as a full compile/run positive and
   assert both binding type and rewritten argument structure.

The parent plan carries an exact existing-test edit ledger. It currently
proposes comment/docstring corrections in
`test_implicit_callback_wrap.py`, with assertions unchanged; that ledger must
receive Slawomir's approval before editing.

## Version/spec/ABI

- user-visible regression fix; stays on pending unreleased 0.35.0 if still
  applicable;
- no spec change;
- no runtime ABI change expected (ABI 22);
- history should state that the old checker-only Site-5 pin was insufficient
  and the restored path is now full compile/run protected.
