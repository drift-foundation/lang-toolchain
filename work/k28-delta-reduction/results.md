# K28 delta-reduction results

Date: 2026-04-13
Compiler: 0.27.189 | ABI 9 | git 02dbf4ee

## Headline

**K28 is a method-visibility issue, not a TypeId duplication issue.**

The trigger is: the consumer never `import std.core` (directly or
transitively-with-import), so `std.core` is not in the consumer's
`visible_modules_set`.  `or_throw` is defined in `std.core`, so its
visibility check fails.  The prelude exemption (`_is_prelude_type_method`)
does **not** save it because that exemption requires the receiver type's
`module_id` to be in `{None, "lang.core"}`, but `Result` is in `std.core`.

The previously-suspected "two Result base TypeIds (package-linked vs.
source-compiled)" is **not** what's happening.  The type table contains
exactly one `Result` base (tid=111, module_id=std.core, kind=VARIANT) plus
N instantiations.  No duplication.

## Variant matrix

Standalone harness: `work/k28-delta-reduction/repro.py`.  All variants
start from the in-tree xfail fixture and change one variable at a time.

| Variant | Change                                                   | Result | Notes |
|---------|----------------------------------------------------------|--------|-------|
| baseline | exact xfail fixture                                      | FAIL   | "method 'or_throw' exists but is not visible here" |
| v1      | drop consumer `--stdlib-root`                            | FAIL   | default resolves to same repo `stdlib/` — no behavioural change |
| v2      | + `--target-word-bits 64`                                | FAIL   | irrelevant axis |
| v2b-nondev | drop `--dev` from consumer                            | FAIL   | `--dev` only gates trust-store override + reserved-namespace check |
| v2c-producer-nondev | drop `--dev` from producer                   | FAIL   | same |
| v2d-both-nondev | drop `--dev` from both                           | FAIL   | same |
| v2e-chained | consumer source uses chained `pkgfn().or_throw()`    | FAIL   | call form is NOT the trigger |
| v2f-probeshape | producer rewritten to web-probe shape (own exception, var self, no `std.err:ResultError`, no `DiagnosticValue`) | FAIL | source shape is NOT the trigger |
| v3a     | producer built without `-M` (deploy-style)               | FAIL   | producer flag-set not the trigger |
| v3b     | + consumer drops `-M`                                    | FAIL   | consumer `-M` not the trigger |
| v3      | full deploy-style consumer (`--target-word-bits 64`, `-o`, no `--dev`, no `--dev-core-trust-store`) | FAIL | none of these matter |
| v4      | full rename to web-probe naming (`or-throw-probe` / `module probe`, own `ProbeException`, `pub` + `export {…}`) | FAIL | naming/layout is NOT the trigger |
| **v2g-importcore** | **consumer adds `import std.core as core;`** | **PASS** | **trigger isolated** |
| **v4-importcore**  | **v4 + consumer `import std.core`**          | **PASS** | trigger isolated, holds across naming |

The matrix collapses to a single binary axis: **does the consumer have
`std.core` in its `visible_modules_set` at the call site of `.or_throw()`?**

## Compiler-internal evidence

Debug dump from `lang/driftc/checker/call_resolver.py` at the
"exists but is not visible" diagnostic site (env-gated; reverted after
data collection — see git for the diff if you want to re-enable):

```
[K28] method_name='or_throw'
[K28] current_module=3  current_module_name='main'
[K28] receiver_nominal_for_lookup: tid=111 kind=VARIANT module_id=std.core name=Result
[K28] receiver_base:                tid=111 kind=VARIANT module_id=std.core name=Result
[K28] recv_ty:                      tid=888 kind=VARIANT module_id=std.core name=Result
[K28] visible_modules_set (5): [3, 9, 20, 33, 34]
[K28] hidden[0]: kind=METHOD_INHERENT module_id=10 pub=True fn_id.module=std.core
[K28]            impl_target: tid=111 kind=VARIANT module_id=std.core name=Result
[K28]            visible_check=False prelude_check=False
```

Reading:
- Receiver, base, and impl-target all converge on the *same* Result base
  (tid=111).  No TypeId duplication.
- The candidate is `or_throw` defined in `std.core` (module_id=10).
- `visible_modules_set = {3, 9, 20, 33, 34}` — std.core (module 10) is
  absent, so `_candidate_visible` returns False.
- `_is_prelude_type_method` returns False because `Result.module_id ==
  "std.core"`, but the prelude allow-list is `{None, "lang.core"}`.
- Diagnostic fires.

When `import std.core as core;` is added to the consumer, std.core's
module_id joins `visible_modules_set` and the candidate becomes visible.

## Why drift-web's tests passed

The drift-web consumer files almost certainly `import std.core` directly
(or import a module that adds std.core to visible scope through whatever
the language's transitive-visibility rule actually is).  This is *not*
the K28-fixture's situation — the fixture's `main.drift` imports only
`acme.thrower`.

To confirm: ask web team whether their `tests/consumer/rest_or_throw_test.drift`
and `tests/consumer/or_throw_probe_test.drift` contain `import std.core`
(or any other import that pulls in std.core).  If yes, this fully closes
the loop.

## What this means for K28

The bug (or "bug") splits into two questions:

1. **UX / language ergonomics:** should `or_throw` (and other Result
   methods) be callable on a `Result` value without an explicit
   `import std.core`?  Currently no, because `Result.module_id` is
   `"std.core"` rather than `None`/`"lang.core"`, so the prelude
   exemption in `_is_prelude_type_method` doesn't fire.  Two possible
   fixes:
   - extend `_PRELUDE_TYPE_MODULES` to include `"std.core"` for variants
     that are conceptually builtins (`Result`, `Optional`); or
   - mark `Result`/`Optional` as having `module_id = None` in the
     type table, treating them as true builtins.
   Either way this is a deliberate language-design call, not an
   accidental compiler regression.

2. **Diagnostic clarity:** the current diagnostic "method 'or_throw'
   exists but is not visible here" is misleading when the issue is a
   missing transitive import.  A note like "did you mean to
   `import std.core`?" would resolve nearly all real-world incidents
   without a language change.

## Recommended next actions

- **Web team:** no compiler bug blocking them; the workaround is
  `import std.core` in any consumer that calls Result methods directly.
  Likely zero code changes needed if their test files already import
  std.core.
- **Compiler team:** decide between the two fixes above (prelude
  extension vs. true-builtin promotion) plus the diagnostic-note
  improvement.  Both are pure consumer-side; no ABI bump.
- **Test fixture:** the in-tree
  `test_ext_cross_package_or_throw` xfail should be re-purposed as a
  guard for the chosen fix, *not* deleted.  It correctly reproduces
  the visibility gap.

## Standalone repro

```bash
.venv/bin/python work/k28-delta-reduction/repro.py baseline       # FAIL
.venv/bin/python work/k28-delta-reduction/repro.py v2g-importcore # PASS
.venv/bin/python work/k28-delta-reduction/repro.py v4             # FAIL
.venv/bin/python work/k28-delta-reduction/repro.py v4-importcore  # PASS
```

Set `DRIFT_K28_DEBUG=1` after restoring the debug block in
`call_resolver.py` (see git history for the dropped diff) to re-print
the dump.
