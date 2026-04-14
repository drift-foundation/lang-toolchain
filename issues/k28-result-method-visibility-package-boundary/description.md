# K28: Result method visibility through package boundary

## Status (2026-04-13)
**FIXED in 0.27.190.**  Prelude visibility for `std.core.Result`
methods through package consumers.  Consumer-side checker change only;
no ABI bump.  Regression: `test_ext_cross_package_or_throw` (now
passing) plus `test_ext_std_core_non_prelude_still_hidden` (negative
guard for the narrow exemption).

`Optional` was considered but is already seeded under
`module_id = "lang.core"` by the parser, so it already passes the
existing prelude branch — no std.core allow-list entry needed.

## Pre-fix history (2026-04-13, after delta reduction)
**Re-classified as a method-visibility issue, not a TypeId duplication.**

The original "two Result base TypeIds (package-linked vs source-compiled)"
hypothesis is **disproven**.  Compiler-internal dump shows a single
`Result` base TypeId (tid=111, module_id=`std.core`, kind=VARIANT) plus
N parameterized instantiations.  No duplication.

Actual root cause: the consumer's `visible_modules_set` does not contain
`std.core`.  `or_throw` is defined in `std.core`, so `_candidate_visible`
returns False.  The prelude exemption (`_is_prelude_type_method`) does
not save it because that gate requires `Result.module_id` to be in
`{None, "lang.core"}`, but it is `"std.core"`.

Adding `import std.core as core;` to the consumer makes the diagnostic
disappear deterministically (rc=0).

Full evidence: `work/k28-delta-reduction/results.md` and
`work/k28-delta-reduction/repro.py`.

## Symptom
Consumer imports a package that returns `Result<Int, ProducerError>` and
calls `(move r).or_throw()` (or chained `pkgfn(...).or_throw()`) without
also importing `std.core`.  Diagnostic:

```
method 'or_throw' exists but is not visible here
```

## Confirmed root cause
At the `.or_throw()` call site (`lang/driftc/checker/call_resolver.py`):

- Receiver, base, and impl-target all converge on the same `Result` base
  (no duplication).
- `or_throw` candidate is registered with `module_id` = std.core's
  module integer (10 in the repro).
- `visible_modules_set` for the consumer (which only does
  `import acme.thrower`) is `{3 (main), 9, 20, 33, 34}` — std.core
  absent.
- `_candidate_visible` returns False (std.core not in visible_modules_set).
- `_is_prelude_type_method` returns False because `Result.module_id ==
  "std.core"`, not `None` / `"lang.core"`.
- Diagnostic fires.

## Suspected subsystem
- `lang/driftc/checker/call_resolver.py` —
  `_is_prelude_type_method` allow-list is too narrow; or
- `lang/driftc/parser/__init__.py` —
  `Result`/`Optional` should arguably be seeded with `module_id = None`
  to match their conceptually-builtin status.

## Repro
```
lang/tests/driver/test_external_consumer.py::test_ext_cross_package_or_throw
```
Currently marked `xfail(strict=True)`.  Reproduces the issue correctly;
should be re-purposed as a guard once the chosen fix lands.

## Fix landed (0.27.190)
Implemented as a narrow-exemption variant of option 1.

In `_is_prelude_type_method` (`lang/driftc/checker/call_resolver.py`):

```python
if td.module_id in _PRELUDE_TYPE_MODULES:
    return True
if td.module_id == "std.core" and td.name in _PRELUDE_STD_CORE_TYPE_NAMES:
    return True
return False
```

`_PRELUDE_STD_CORE_TYPE_NAMES = frozenset({"Result"})`.

`Optional` is intentionally NOT in this set: the parser seeds the
canonical `Optional` variant under `module_id = "lang.core"` (see
`ensure_optional_base` in `lang/driftc/parser/__init__.py`), so it
passes the first branch above.  Adding it to the std.core allow-list
would be dead code today.  Add it only if a real `std.core.Optional`
receiver path emerges.

Deliberately NOT chosen:
- "Add std.core to `_PRELUDE_TYPE_MODULES`" — would expose every
  std.core type's methods (`Cell.get`, `DiagnosticEntry`, `DefaultHasher`,
  …) globally, far broader than intended.
- Promote `Result`/`Optional` to `module_id = None` true builtins —
  deeper identity-model change; too broad for a point release.
- Diagnostic-note improvement ("did you mean `import std.core`?") —
  still worth doing as an independent UX improvement, but not required
  by the fix itself.

## Regression tests
Both in `lang/tests/driver/test_external_consumer.py`:
- `test_ext_cross_package_or_throw` — now passing (was strict-xfail).
  Fixture intentionally omits `import std.core` (inline assertion
  guards against re-adding it).  Exercises both `(move r).or_throw()`
  on a bound local and chained `pkgfn(...).or_throw()` on an rvalue.
- `test_ext_std_core_non_prelude_still_hidden` — guard test that
  asserts `std.core.Cell.get` on a package-returned `Cell<Int>`
  STILL fails the visibility check without `import std.core`.
  If it ever passes, the prelude exemption has been broadened beyond
  `Result` and the visibility surface needs review.

## Workaround (no longer needed on 0.27.190+)
Pre-fix, downstream consumers could add `import std.core as core;` in
any file that called inherent `Result` methods on package-returned
values.  No longer required on 0.27.190+; the narrow prelude exemption
makes Result/Optional methods visible without an explicit import.

## Why drift-web's tests pass
Their consumer files almost certainly already import std.core directly
(or through a chain that pulls it into visible scope).  This is *not*
the K28 fixture's situation; the fixture's `main.drift` only imports
`acme.thrower`.  Confirm with web team if curious — but no further
package-side or producer-side regression is needed.

## Impact
Limited to consumers that:
- call inherent `Result` methods (`or_throw`, `on_error`, `is_ok`, …) on
  values whose type is unified through `std.core.Result`, AND
- do not import `std.core` directly or via any module that brings it
  into visible-modules scope.

## Scope
Consumer-side checker visibility gate.  No runtime ABI change.
A fix requires a compiler version bump only.
