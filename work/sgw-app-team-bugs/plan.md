# sgw app-team compiler-findings tracker

**Origin:** singular-gateway implementation report
(`/home/sl/src/pushcoin/work/singular-gateway/compiler-findings.md`,
authored 2026-05-17 against drift-0.31.99+abi14).

**Why this lives here, not in the app-team report:** the report
is the app team's record of what bit them.  This tracker is the
toolchain side's classification + action plan -- not allowed to
edit the app-team doc directly.

---

## Classification

| # | Bug | Severity (app) | Classification | Action |
|---|---|---|---|---|
| 1 | `ConnectionPool.close()` trait metadata crash | medium | LANGUAGE_BUG | Pin + fix method resolver / cross-package interface metadata |
| 2 | Cross-package `pub error` catch binder field projection | medium | LANGUAGE_BUG | Pin + fix catch binder / package error projection |
| 3 | `match &Result { Err(e) => ... }` binder unbound | low-medium | LANGUAGE_BUG | Pin + fix by-ref variant match binder lowering |
| 4 | `&"literal"` temporary borrow sometimes rejected | low | LANGUAGE_BUG (after reduction) | Reduce to deterministic repro first; then pin + fix |
| 5 | No `panic` / `unreachable!` / `Never` type | low (usability) | Language feature gap | Separate design note, NOT bug-fix queue |

---

## Priority order (from user 2026-05-17)

1. **#2 first** -- bookkeeper will consume `singular:ConfigError`
   cross-package soon; this is the next real integration friction
   point.
2. **#3** -- straightforward shape (by-ref variant match binder);
   reduces ergonomic tax on Result-returning APIs.
3. **#1** -- requires deeper investigation into method-resolver
   cross-package trait-metadata lookup; medium severity but app
   has a working (if imperfect) fallback via field-destruction
   order.
4. **#4** -- needs reduction first.  Report says "sometimes" --
   that's a heisenbug shape we cannot fix from compiler side
   without isolating the trigger.  Defer until reproducible.
5. **#5** -- explicitly OUT of the bug-fix queue.  File separately
   as a feature/design note: likely `core.unreachable<T>() -> T`
   built-in or proper `!`/Never type design.  Don't mix into
   bug-fix slices.

---

## Per-bug status

### #1 -- ConnectionPool.close() trait metadata crash

**Status:** not started.

**Repro:** `/tmp/sgw-stub/` per app-team report; reduce in-repo.

**Crash shape:**
```
lang.driftc.method_resolver.ResolutionError: missing trait metadata
  for 'mariadb-rpc::mariadb.rpc.managed.ConnectionSource'
  at lang/driftc/checker/call_resolver.py:2694 (resolve_method_call)
```

**Suspect area:** `call_resolver.resolve_method_call` looking up
trait-metadata for a cross-package interface (`mariadb.rpc.managed.ConnectionSource`)
when dispatching against a concrete impl type from a different
package (`mariadb.rpc.pool.ConnectionPool`).  Likely a missed
seed of the trait scope into the consumer's index OR a stale
lookup key.

**Open questions before starting:**
 - Does this reproduce with a minimal two-package setup (consumer
   imports package A's interface, calls a method on package B's
   impl)?  Or only with the full mariadb-rpc/pool layering?
 - Is the missed metadata the interface's schema, the impl's
   method-map, or the linearization?  Crash message says "trait
   metadata" but that could mean any of the three.

---

### #2 -- Cross-package pub error catch binder field projection (NEXT)

**Status:** in-progress / starting.

**Crash shape:**
```
error: field access requires a struct value [E-AUTO-69eb9f81]
error: no matching overload for function 'X' with args [<type id>]
```
inside `catch otherpkg:SomeError(e) { ... val t = e.tag; }`.
Same-package equivalents work; only cross-package binders fail.

**Suspect area:** catch-binder lowering treats the cross-package
binder's TypeId as opaque/forward-nominal rather than projecting
through the imported pub-error schema.  Likely in the
type-checker's catch-arm pass or the cross-module pub-error
package-export shape.

**Approach:**
 1. Build minimal two-package repro: package A exports
    `pub error E { tag: String }`, package B consumer catches
    `A:E(e)` and projects `e.tag`.
 2. Pin as failing regression in driver tests (subprocess
    pattern, same as recent sgw-stub regressions).
 3. Investigate read-only: trace catch-binder TypeId through
    type-checker, find where same-package vs cross-package
    diverge.
 4. Report findings; await direction on fix.
 5. Implement fix per direction; re-run regression + broader
    pub-error suite.

**Why first:** bookkeeper consumer-side is imminent.  Hitting
this in real integration would mean the gateway can't expose
structured `ConfigError`s -- defeats the typed-error design
the gateway shape relies on.

---

### #3 -- match &Result by-ref variant binder unbound

**Status:** not started.

**Crash shape:**
```
error: unknown name 'e' [E-AUTO-9e92e226]
error: field access requires a struct value [E-AUTO-69eb9f81]
```
inside `match r { core.Result::Err(e) => { ... } }` where
`r: &core.Result<...>`.  Same pattern with `match move r` (by
value) works.

**Suspect area:** by-ref variant-match lowering.  Per
`docs/match_by_ref_variant.md` history, this path has been
worked on multiple times (most recently the
`match_by_ref_variant.py` doc rules) -- likely a missed shape
in the by-ref binder lowering for cross-package variants OR
result-specific shape.

**Approach:**
 - Build minimal repro (single-package first, then check if
   cross-package amplifies).
 - If single-package reproduces: in-checker / stage2 fix.
 - If only cross-package: shares root with #2 (cross-package
   nominal projection); possibly fix-once-fixes-both.

---

### #4 -- &"literal" temp borrow sometimes rejected

**Status:** blocked on reduction.

**Crash shape:**
```
error: cannot borrow from moved or uninitialized '__tmp_borrowN'
  [E-AUTO-e57d22a5]
```
on some `&"literal"` borrows, not others -- in the same file,
same call shape.

**Action:** DO NOT touch until reproducible.  The app team's
report calls it "sometimes" -- a non-deterministic
trigger from the compiler perspective.  Possible triggers
to investigate when reducing:
 - Number of preceding `&"literal"` borrows in the same fn
   (counter overflow / id collision)
 - Specific call shape (chained method vs direct, by-ref-arg
   vs let-binding)
 - Interaction with prior `match` / `try` blocks at the same
   scope
 - String literal interning vs fresh-temp emission

Send back to app team for a 100%-reproducible reduction
before opening as a fix slice.

---

### #5 -- No panic / unreachable / Never

**OUT OF BUG-FIX QUEUE.**  Separate language design note;
filing under a different work directory.  Not tracked here
beyond this reference.

Likely future slice: `core.unreachable<T>() -> T` built-in
that diverges (compiler intrinsic, no body).  The
`!`/`Never` type approach is bigger surgery and probably not
worth it for v1.

---

## Cross-package theme

The app team's report flags a recurring pattern: each bug
degrades AT the package boundary.  Within-package shapes work;
crossing the boundary breaks.  Bugs #1 (trait metadata), #2
(catch binder field projection), and possibly #3 (if
cross-package amplifies it) share this shape.

**Hypothesis:** the package compiler's `is_exported_entrypoint`
/ trait-scope / nominal-instance export does not propagate the
secondary metadata (linearization, field schemas, etc.) that
the consumer-side resolver / projection needs.  Same root,
multiple surface symptoms.

If #2's investigation reveals this, the fix may close #1 and
#3 as well -- in which case the action order collapses to
"investigate #2 thoroughly" rather than three independent
fixes.  Worth keeping in mind.

---

## Repo rules to follow

Per `AGENTS.md` LANGUAGE_BUG process:
 - Minimal failing regression FIRST.  Confirm it fails on
   current main before writing any fix.
 - Root-cause compiler / toolchain fix.  No app or stdlib
   workaround as the final resolution.
 - Refactor-triggers scan (`docs/refactor_triggers.md`):
   note "no trigger matched" or apply one.
 - Version bump + history entry per fix slice.
 - K-review before landing.

Test pattern: subprocess.run-based driver test (matches the
recent pattern from `test_arc_interface_get_dispatch_segfault.py`,
`test_const_share_synth_shared_binder_name.py`,
`test_lambda_void_callback_throw_check.py`).  All cross-package
bugs will need the `stdlib_package` fixture pattern from
`test_pkg_consumer_e2e.py` + a custom secondary package
build.
