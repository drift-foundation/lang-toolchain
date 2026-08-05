# Evidence: causal Unknown cascade suppression

Evidence split:

- **Executed baseline:** 2026-08-03 work-only probe result.
- **Static refresh:** 2026-08-04 committed pending-`0.35.0` source.
- **Not yet established:** the pending-value-read, alias propagation, and
  `HInvoke` parity hypotheses identified during refresh.

The 2026-08-04 refresh ran no probe or compiler command because the repository
full suite was already consuming the tree and CPU. This file does not present
static deductions as executed results.

## Executed red baseline (2026-08-03)

Command used then:

```sh
./.venv/bin/python3 -m pytest -q work/finding-causal-unknown-cascade-suppression/probe_causal_unknown_suppression.py
```

Observed result: `2 failed`. In both cases the diagnostic stream contained only
the deliberately unrelated first error:

```text
copy operand must be an addressable place in v1 (local/param/field/index)
```

Missing diagnostics:

- copy boundary: no `E-COPY-UNKNOWN` for the separate preseeded Unknown binding;
- call boundary: no `call target is not a function value` for that binding.

The probe uses public `check_function` preseed maps to isolate a last-line
checker invariant. It proves the suppression is function-global at this
boundary. It does not prove that the proposed binding-cause table is the only
or best implementation.

## Static source facts (2026-08-04)

### Function-global copy suppression remains

At `lang/driftc/type_checker.py:4012-4036`, `_require_copy_value` still has:

```python
if ty_id == self._unknown and any(getattr(d, "severity", None) == "error" for d in diagnostics):
	return
```

The adjacent comment says the Unknown value's failure is already diagnosed,
but the predicate proves only that some earlier function diagnostic exists.
The helper receives `expr`; its main `HVar` use sites pass the bound node, whose
`binding_id` has already been resolved.

### Function-global `HCall(fn=HVar)` suppression remains

At `lang/driftc/checker/call_resolver.py:6623-6719`, the local binding branch:

1. obtains/stamps `binding_id`;
2. calls `type_expr(expr.fn, used_as_value=True)`;
3. handles a real function type;
4. otherwise suppresses the call-target error when the result is Unknown and
   any earlier error exists.

The exact binding id is still in scope at the suppression point, so a causal
predicate can cross the checker/resolver boundary without name re-resolution.

### `HInvoke` is a distinct consumer

At `lang/driftc/type_checker.py:10010-10115`, `HInvoke` resolves a pending
`HVar` lambda, re-types its callee, and unconditionally emits the same
call-target message when the callee is not a function. This is not evidence of
the reported over-suppression, but it is evidence that fixing only
`call_resolver.py` can leave inconsistent same-binding cascade behavior.

### Current context boundary

`CallResolverContext` is a frozen dataclass at
`lang/driftc/checker/call_resolver.py:810+`. The only direct constructor is
`make_call_ctx(**kwargs)`; current call sites are in `type_checker.py` near
lines 8518, 10003, and 10310. A new non-default context predicate requires all
three sites; a defaulted predicate must still fail safely rather than silently
reintroduce the global heuristic.

### Transaction owner

At `lang/driftc/type_checker.py:438-506`, `FnCheckState.OWNED_TABLES` lists the
transaction-aware mutable tables. Each map is `_TxnDict`, diagnostics is
`_TxnList`, and `state_fingerprint()` enumerates the owned names. The deferred
call resolver opens `CheckerStateTxn` for allowlisted expression shapes and can
commit or roll back diagnostics, HIR changes, tables, and allocator cells.

Therefore any mutable cause table written during expression typing must be
owned here. Whether current lambda-producer paths are admitted by the shape
gate is not a reason to create an untracked state channel.

### Binding producer inventory

The current `binding_types[...]` writes show these relevant paths:

| Path | Current behavior | Diagnostic timing |
|---|---|---|
| Pending stored lambda declaration (`12318-12331`) | writes `Unknown`, records pending lambda | none yet |
| Pending `HCall` resolution (`9986-10001`) | writes typed function or `Unknown`, pops pending | lambda typing may diagnose before write |
| Pending `HInvoke` resolution (`10019-10033`) | same | lambda typing may diagnose before write |
| Ordinary `HLet` (`12331-12464`) | stores initializer/declared type | initializer may diagnose before write |
| Final pending flush (`13760-13823`) | rejects capture/unconstrained cases as `Unknown`, or stores inferred function type | direct primary diagnostic at rejection |

Other writes found by the static inventory seed params/binders or use the
dedicated `Error` type. They should not be folded into one “Unknown after any
error” rule without producer-specific evidence.

## Existing in-tree guards

`lang/tests/driver/test_stored_capturing_lambda_diagnostic.py` currently pins
the invoked implicit-borrow capture case:

- one primary borrowed-capture rejection;
- real source span;
- no `E-COPY-UNKNOWN`;
- no repeated call-target message.

This is the essential same-binding positive suppression guard. Its module
comment uses broad wording (“a prior error already explains”), but no edit is
needed to run it. Editing that existing comment or test requires explicit
human approval.

`lang/tests/driver/test_uninvoked_stored_lambda.py` pins final-flush rejection
for implicit borrow, explicit borrow, and value capture. It also has a
`move f` case, which intentionally bypasses copy-value checking and therefore
does not cover a bare value read of the pending Unknown binding.

`lang/tests/type_checker/test_type_checker_copy_unknown.py` proves
`E-COPY-UNKNOWN` is a live tripwire for unresolved generic copy cases, but it
does not place an unrelated diagnostic first.

`lang/tests/checker/test_defer_probe_state_transaction.py` pins rollback,
fingerprint coverage, nested transaction behavior, and wrapper detachment for
the existing owner. If a new regression file can exercise the same public
state contract, no existing-test edit is needed; otherwise request approval
before modifying this file.

## Work-only probe construction

`probe_causal_unknown_suppression.py` creates binding id 41 with the table's
canonical Unknown type using:

- `preseed_binding_types`;
- `preseed_binding_names`;
- `preseed_scope_env`;
- `preseed_scope_bindings`.

It then emits an unrelated `HCopy(HLiteralInt(1))` error followed by either an
`HVar` value read or `HCall(fn=HVar)`. The binding has no causal marker by
construction. A correct causal patch should make both assertions green without
treating every preseeded Unknown as diagnosed poison.

## Red-first probes still required

These are proposed probes, not observed failures:

1. **`HInvoke` same-binding parity.** Construct a diagnosed Unknown binding,
   then `HInvoke(callee=HVar(...))`; determine whether only the primary should
   remain.
2. **Ordinary diagnosed producer.** Compile `val bad = missing_name; bad();`
   and determine whether the unknown-name primary remains the only diagnostic.
   This separates the general causal contract from stored-lambda scheduling.
3. **Pending lambda bare value read.** Store a capturing lambda and read `f` as
   a value before final flush. Count/order diagnostics and rule whether the
   early `E-COPY-UNKNOWN` is a cascade or an independent contract error.
4. **One-hop alias.** Initialize a new binding from a diagnosed Unknown binding,
   then use/call the alias. Determine whether cause propagation is required.
5. **Concrete recovery.** A pending captureless lambda resolves successfully;
   later uses must not see a stale cause.
6. **Shadowing.** Two bindings share a source name but have different ids; a
   cause on one must not suppress the other.
7. **Rollback.** Mutate the chosen cause table inside a checker transaction,
   then verify rollback and commit independently through `state_fingerprint()`.

If probes 2 or 3 reveal a larger provenance problem, K may create a nested
child finding at any point. The implementation slice should either fix it while
the causal authority is open or state precisely why it is independent.

## Expected affected-file boundary

Likely, subject to K's revalidation:

- `lang/driftc/type_checker.py`: causal state authority, producer updates,
  copy and `HInvoke` consumers, transaction ownership, resolver wiring;
- `lang/driftc/checker/call_resolver.py`: context predicate and local
  `HCall(fn=HVar)` consumer;
- new focused checker/driver tests;
- `doc/history.md`: concise entry folded into pending `0.35.0` if still
  unreleased.

No lowering, runtime, stdlib, language-spec, or ABI edit is presently
supported by evidence.
