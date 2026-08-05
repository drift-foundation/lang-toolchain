# Evidence: causal Unknown provenance and pending-lambda value finalization

Refreshed: 2026-08-05.

This file separates executed observations from current static facts and open
probes. Detailed ordered streams from K's preflight remain in implementer-owned
`PROGRESS.md`; this reviewer evidence does not rewrite that channel.

## Executed observations

### Original causal tripwires (2026-08-03; rerun by K 2026-08-04)

`probe_causal_unknown_suppression.py` has two deliberately red cases. Each
creates an exact preseeded binding whose type is canonical Unknown and whose
producer emitted no diagnostic. A separate invalid copy first creates an
unrelated error.

Observed in both cases: only the unrelated copy diagnostic remained.

Missing independent diagnostics:

- `E-COPY-UNKNOWN` for a later read of the separate Unknown binding;
- `call target is not a function value` for a later call through it.

This proves function-global suppression at the public `check_function`
boundary. It does not prove the proposed representation.

### Same-source and HInvoke parity

K's `probe_preflight_hypotheses.py` established:

- ordinary HCall through a causally poisoned binding: primary only;
- equivalent synthetic HInvoke: primary plus call-target cascade;
- pending captureless lambda resolving by direct call: concrete recovery is
  clean on later calls.

The desired HCall result currently occurs for the wrong reason (any prior
error); HInvoke proves the policy has drifted across consumers.

### Pending-value and alias matrix

K's executed `probe_pending_alias_matrix.py` established:

| Shape | Current result | Contract implication |
|---|---|---|
| annotated/inferable captureless `f`, then `val g = f` | sole `E-COPY-UNKNOWN` | valid non-capturing fnptr alias is rejected |
| pending `f`, then contextual `Callback1` alias | sole `E-COPY-UNKNOWN` | expected callback shape never reaches pending lambda |
| unannotated `|x| => x`, then alias | copy cascade, then cannot-infer primary | presentation order is inverted; invalid ABI must remain rejected |
| alias first, direct resolving call later | earlier copy error remains | behavior is source-order dependent |
| `bad = missing_name; bad()` | one unknown-name primary | desired same-source suppression, presently global |
| explicit capturing lambda then alias | copy cascade, then approved bare-storage primary | same one-primary requirement as implicit capture |

The earlier implicit-borrow alias probe produced the same cascade-first shape.

### Additional value positions and transaction surface

K's `probe_txn_and_value_positions.py` established:

- `return f` on pending captureless binding sees Unknown and emits
  `E-COPY-UNKNOWN`;
- `sink(f)` with an incompatible concrete parameter reports overload failure
  with `[Unknown]`, demonstrating argument positions also consume the
  placeholder (this source is not itself a positive contract);
- HCall through a pending lambda can occur under deferred-call probes;
- before the barrier fix, pending resolution mutated unowned external state.

The later `test_pending_lambda_probe_barrier.py` red-first audit proved the
actual rollback leak with a forcing nested generic call. The committed fix now
bars all pending-owner mutation before external state changes, preserves exact
state identity, propagates nested barriers, and converts only at the outermost
probe. The focused module passed 8/8 independently at terminal review.

## Current static facts

### Function-global copy suppression

At `lang/driftc/type_checker.py:4124`:

```python
if ty_id == self._unknown and any(getattr(d, "severity", None) == "error" for d in diagnostics):
    return
```

`_require_copy_value` receives the expression. For HVar callers, lexical
`binding_id` has already been established.

### Function-global HCall suppression

At `lang/driftc/checker/call_resolver.py:6786`, the local-binding HCall route
has the same function-global predicate. Its exact `binding_id` remains in
scope. `CallResolverContext` needs a read-only causal query if the authority
stays in the type checker.

### HInvoke opposite policy

At `lang/driftc/type_checker.py:10204`, HInvoke appends the call-target
diagnostic unconditionally after a non-function result. It is a real internal
boundary even though ordinary stored source parses as HCall(fn=HVar).

### Pending lifecycle

- owner: `PendingLambdaOwner`, `type_checker.py:606+`;
- per-function instance: `:2787`;
- HCall resolution: `:10080`;
- HInvoke resolution: `:10113`;
- registration: `:12418`;
- final drain: `:13854`.

The HCall and HInvoke blocks currently duplicate expected-function assembly,
lambda typing, binding update, and retirement. The final drain separately
duplicates capture discovery/rejection, unconstrained-parameter rejection,
binding update, and spec validation.

### HVar consumption

The HVar branch around `type_checker.py:7350+` reads `binding_types` and calls
`_require_copy_value`; it does not consult the pending owner. Consequently
every non-call reference sees the declaration's Unknown placeholder.

`used_as_value` and `defer_value_use` control Copy diagnostics, but they do not
provide a pending-lambda type. A total finalizer must not mistake “do not copy
here” for “do not resolve the binding.”

### Transaction owner

`FnCheckState.OWNED_TABLES` at `type_checker.py:462+` drives both `_TxnDict`
undo logging and `state_fingerprint()`. Mutable cause state written during
expression typing belongs there. Returned `TypedFn` detaches current owned
tables into plain dicts; a private cause table need not be returned at all.

### Context construction

Three `make_call_ctx(...)` sites currently occur near `type_checker.py:8612`,
`:10097`, and `:10404`. A required causal predicate must reach all three; a
defaulted predicate must fail toward emitting the tripwire.

## Specification evidence

The approved current specification is already sufficient; no spec edit is
needed:

- `doc/design/drift-lang-spec.md` §22.0.1: non-capturing function pointers may
  be stored/returned freely; capturing literals need a supported representation.
- §22.2.3: bare stored capturing lambdas are invalid even if never used.
- §22.3: non-capturing closures lower to thin function pointers and are Copy.

Thus “captureless alias compiles” and “capturing bare storage rejects cleanly”
are two sides of one existing contract, not a new language decision.

## Refactor-trigger and announcement evidence

`doc/refactor_triggers.md` was scanned on 2026-08-05. No registered trigger
matches causal diagnostic provenance or pending-lambda finalization.

No files were reported under `/tmp/drift-announce/` during the refresh.

Version at refresh: `DRIFTC_VERSION = 0.35.0`, ABI 22.

## Open evidence required before design selection

1. Does cause need to cross a poisoned call result before HLet assigns it?
2. Which wrapper expressions are causally transparent, if any?
3. Does a single HVar pending hook cover explicit move/borrow/discarded reads
   without ownership or lowering regressions?
4. Does contextual Callback aliasing retain a thin original binding and insert
   a lowering-visible callback wrapper?
5. Can capture discovery/rejection in the shared finalizer avoid applying
   capture effects for a bare representation that is already illegal?
6. Can no-context unconstrained finalization reject before publishing any
   `LambdaFnSpec` with Unknown ABI types?
7. Does finalizing during a deferred argument probe take exactly the new
   barrier/rollback/retry route, with no second mutation channel?

These are work for red-first probes, not assumptions to bury in implementation.

## Planning-review evidence (2026-08-05)

K added and ran the localized `probe_planning_review_matrix.py` during the
full-suite gate. Observed:

- `bad = missing_name; x = bad(); x();` emits only `E-UNKNOWN-NAME` today.
  A causal replacement must mark the suppressed `bad()` call node and carry
  that cause into `x`, or it regresses presentation.
- `val g = move f; g()` over pending `f` reaches the same Unknown/cascade
  family. A pending HVar hook must run even when Copy consumption is disabled.
- `val r = &f` over pending `f` is silently accepted. This is not proof that
  borrowing a function pointer is invalid; a concrete/named-fn control is
  needed to establish the intended post-finalization contract.
- discarded `f;` is later finalized by drain and stays clean.
- direct typed-let `val cb: core.Callback1<Int, Int> = |x: Int| => x` reaches
  MIR as raw HLambda and emits an internal lowering-contract failure.
- the unconstrained uninvoked case emits one cannot-infer primary with an empty
  LambdaFnSpec registry.

The direct Callback behavior is already governed by permanent evidence:

- `doc/history.md` 2026-04-26 / release 0.31.17 explicitly added implicit
  wrapping at typed-let initializer Site 5;
- `lang/tests/driver/test_implicit_callback_wrap.py::test_site5_typed_let_bare_lambda_to_callback1`
  pins clean acceptance, but only through its current compile helper and did
  not prevent the raw-HLambda MIR regression.

Therefore the direct-form child restores implicit wrap and adds a new full
compile/run boundary pin. It does not request a new wrap-versus-reject language
ruling.

## Planning round-2 evidence (2026-08-05)

K added and ran `probe_planning_round2.py` under the finding tree while the
shared full suite continued. Observed:

- `bad = missing_name; m = move bad; m()` emits only the unknown-name primary;
- `bad = missing_name; t = (true ? bad : bad); t()` also emits only that
  primary;
- passing a pending captureless `f` directly to `Callback0<Int>` reaches MIR
  as a move from an uninitialized interface local;
- after first finalizing stored `f` by call, `&f` compiles and runs;
- `&seven` for a named function raises raw `AttributeError` because an
  `HFnPtrConst` is consumed by a path expecting `.name`;
- direct typed Callback HLet has Callback binding type, raw HLambda initializer,
  and no checker diagnostic.

The first two observations select expression-node provenance in addition to
exact binding causes. The third and sixth are two manifestations of a
lowering-invisible Callback conversion. The borrow pair selects
finalize-and-accept for pending `&f` and exposes a separate function-reference
materialization boundary defect.

Static review confirms the typed-HLet equality bypass: HLambda typing may
return the expected Callback interface itself; the later HLet callback-wrap
branch runs only for `inferred_ty != declared_ty`. Any fix must construct the
wrapper before the inner lambda is typed through the callback intrinsic,
otherwise capturing Callback literals risk being rejected by the captureless
function-pointer branch.

Static review also finds a plausible named-function-borrow chain: stage1
classifies a bare HVar syntactically as a place, while `_apply_fnptr_consts`
recursively replaces marked HNodes without protecting an `HPlaceExpr.base`.
Since the canonical-place contract requires that base to remain HVar, a
structural regression must confirm the exact transition before the repair is
selected.
