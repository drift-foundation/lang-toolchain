# Evidence: causal Unknown cascade suppression

Snapshot date: 2026-08-03. Line numbers are navigational and may drift.

## Reproduced baseline

Command:

```sh
./.venv/bin/python3 -m pytest -q work/finding-causal-unknown-cascade-suppression/probe_causal_unknown_suppression.py
```

**Observed on 2026-08-03:** `2 failed`. In each case the diagnostic list
contained only the unrelated first error:

```text
copy operand must be an addressable place in v1 (local/param/field/index)
```

The independent `E-COPY-UNKNOWN` and independent call-target error were both
absent. This establishes the function-global suppression behavior at the
checker-unit boundary; it does not by itself prove that the proposed poison
table is the best fix.

## Confirmed source facts

### Copy-use suppression

At `lang/driftc/type_checker.py:3991-4015`, `_require_copy_value` returns early
for `Unknown` whenever the per-function `diagnostics` list contains any error:

```python
if ty_id == self._unknown and any(
    getattr(d, "severity", None) == "error" for d in diagnostics
):
    return
```

The function receives the current `expr`, and `HVar` nodes carry a resolved
`binding_id`, so binding-specific lookup is available at this consumer.

### Call-target suppression

At `lang/driftc/checker/call_resolver.py:6749-6755`, the local-binding call path
returns `Unknown` without a diagnostic under the same global condition. The
branch already has the resolved local `binding_id` and can therefore consult a
binding-specific predicate without re-resolving the name.

### State transactions

At `lang/driftc/type_checker.py:462-493`, `FnCheckState.OWNED_TABLES` enumerates
the transaction-aware per-function tables. `diagnostics` is `_TxnList`; the
recording tables are `_TxnDict`. `state_fingerprint()` includes exactly the
owned table names. Any new mutable poison side table that can be touched during
a deferred probe should participate in this same contract.

### Stored-lambda producer routes

- `type_checker.py:9941-9956`: first `HCall` through a pending lambda types the
  lambda, assigns either the resulting function type or `Unknown`, and removes
  the pending entry.
- `type_checker.py:9974-9988`: analogous `HInvoke` path.
- `type_checker.py:12286-12299`: unannotated stored lambdas begin pending with
  an `Unknown` binding type.
- `type_checker.py:13727+`: end-of-function flush diagnoses unresolved stored
  lambdas and leaves rejected bindings `Unknown`.

These locations are an audit list, not proof that every assignment should set
the same poison category.

## Existing coverage and its limit

`lang/tests/driver/test_stored_capturing_lambda_diagnostic.py:64-87` asserts:

- the primary stored-capturing-lambda rejection is visible and spanned;
- `E-COPY-UNKNOWN` is absent;
- `call target is not a function value` is absent.

That is a useful positive contract for same-binding cascade suppression. It has
only one primary error and cannot expose suppression caused by a different
earlier expression.

`lang/tests/type_checker/test_type_checker_copy_unknown.py` pins ordinary
`E-COPY-UNKNOWN` production for an unresolved generic explicit copy and
`Array<T>.dup()`. Neither test first inserts an unrelated diagnostic.

## Why the synthetic boundary probe is appropriate

The current comment promises a last-line tripwire for an *undiagnosed* Unknown.
In normal parsed source, most Unknown values already originate in an upstream
diagnostic, making an uncaused Unknown difficult or impossible to express. The
`TypeChecker.check_function` API deliberately accepts preseeded binding and
scope maps. A preseeded `Unknown` therefore isolates the exact invariant under
test without inventing a user-language construct.

The eventual in-tree test may instead use a more realistic internal producer if
the implementer finds one. Preserve the logical distinction: the first error
must be unrelated to the second binding.

## Suspected affected-file boundary

**Proposed:**

- `lang/driftc/type_checker.py` — poison authority, producers, copy consumer,
  state ownership, and call-context wiring.
- `lang/driftc/checker/call_resolver.py` — context field/predicate and the local
  binding call consumer.
- focused checker and driver tests.

No lowering, runtime, stdlib, or language-spec edit is currently justified.
