# Child finding: deferred probe can mutate a pending lambda outside its transaction boundary

Date filed: 2026-08-04

Parent: `finding-causal-unknown-cascade-suppression`

Status: provisional LANGUAGE_BUG, extracted from K's preflight response to
`review-2026-08-04T21-48-05Z.md`. Shared implementation remains gated on the
running full suite; work-folder probes are authorized.

Every statement here remains subject to implementer revalidation. The current
evidence proves a structural contract breach and live probe reachability, but
has not yet forced the affected transaction to roll back in one source repro.

## Finding

`CheckerStateTxn` is documented as safe only for expression shapes whose typing
writes through:

1. `FnCheckState.OWNED_TABLES` and allocator cells; or
2. attributes of the probed HIR subtree, which the transaction snapshots.

The resolver's safe-node allowlist admits `HVar` and `HCall`. A syntactically
safe probed subtree can therefore contain a call through a stored pending
lambda, such as the `f()` inside an outer deferred call argument.

Typing that nested call executes `type_checker.py`'s pending-callee
pre-resolution before the nested resolver call:

- `type_expr(pending_lambda, expected_type=...)` mutates the HLambda and can
  register lambda/call metadata;
- `binding_types[binding_id]` is overwritten;
- `pending_lambda_by_binding.pop(binding_id)` removes the pending entry.

`binding_types` and `pending_lambda_by_binding` are plain checker-frame dicts,
not `FnCheckState` tables. More importantly, the pending HLambda initializer is
stored elsewhere in the function body, outside the probed argument subtree, so
the transaction's per-subtree HIR snapshot does not cover its mutations.

If the outer deferred probe returns NEEDS_EXPECTED or throws, rollback restores
owned tables and the probed subtree but can leave these external mutations in
place. A later expected-type retry then observes state produced by a probe that
was supposedly erased.

## Current empirical boundary

K's work-only `id(f())` probe confirms deferred probes open during a compile
containing a nested pending-lambda call: 57 probes, all 57 complete commits,
zero rollback and zero diagnostics. This establishes reachability but does not
manifest the leak because the transaction containing the pending resolution
did not roll back.

The next proof must place two expressions in the same deferred safe subtree:

1. an earlier nested `f()` that resolves and pops a pending lambda;
2. a later expected-dependent call (modeled on the existing `dflt<T>()` tooth)
   that forces the enclosing probe to NEEDS_EXPECTED and roll back.

The existing independent frame auditor in
`lang/tests/checker/test_defer_probe_state_transaction.py` already snapshots
plain dict/list/set locals in `check_function`; it should expose changes to
`binding_types` and `pending_lambda_by_binding`. Extend the work-only probe
first—do not edit that existing test without Slawomir's approval.

## Design status: open; do not implement the narrow gate yet

The first review draft preferred a dynamic `pending_lambda_by_binding` gate.
That is now demoted to one candidate, not the implementation recommendation.
The user explicitly requires a deeper alternative analysis when the minimal
fix would leave a brittle long-term invariant. See `DESIGN.md`.

A one-off predicate of the form "candidate mentions a currently pending
binding" is acceptable only if evidence establishes a durable semantic rule:

> Resolving a pending lambda is an explicit transaction barrier, and every path
> that can trigger it is recognized by one centralized, exact-binding
> dependency/effect authority before a speculative transaction begins.

If that statement cannot be made total across direct calls, invokes, aliases,
future binding-backed callable forms, and nested resolution, the narrow gate is
not the fix. Compare at least these alternatives before implementation:

1. a centralized semantic-effect gate (the strengthened form of the narrow
   idea, not an ad-hoc pending-map lookup);
2. a complete transaction owner plus dependency-closed HIR rollback;
3. non-speculative stabilization of pending dependencies before probing;
4. staged/pure call-resolution results that publish mutations only on commit;
5. replacing pending-lambda mutation with persistent inference variables.

The current static inventory already makes a casually widened transaction
unsafe: pending-lambda typing writes more than `binding_types` and the pending
map. It can stamp the external HLambda/capture nodes, allocate lambda parameter
bindings, populate binding metadata, add `fnptr_consts_by_node_id`, and publish
to the checker-global `_lambda_fn_specs`. Any transaction-expansion proposal
must prove ownership of that whole reachable mutation closure, not merely add
the two first-observed dictionaries.

## Acceptance criteria

- A work/in-tree repro forces the relevant enclosing probe to rollback after a
  nested pending-lambda resolution; the pre-fix independent state audit fails
  for the expected unowned channels.
- Post-fix, the selected architecture prevents any speculative transaction
  from leaking pending-lambda resolution. A gate is only one possible means;
  the acceptance test must name and prove the chosen invariant.
- The expected-type retry produces the same clean, single user diagnostic or
  successful result as the non-speculative path; no duplicate/lost diagnostics.
- Ordinary safe HCall/HVar probes with no pending-lambda reference remain
  enabled and keep their existing commit/rollback coverage.
- Binding identity and shadowing are pinned; name-only matching is forbidden.
- No external pending HLambda, binding type, pending-map entry, allocator, or
  recorder mutation survives a rollback.

## Version/spec/ABI

- No spec change is proposed.
- No compiler/runtime ABI change is expected; ABI remains 22.
- This is internal transaction correctness with possible diagnostic/acceptance
  impact. If it lands on the still-unreleased `0.35.0` train, keep 0.35.0 and
  fold the history note there; re-evaluate if release state changes.

## Refactor-trigger note

The parent scan found no registered `doc/refactor_triggers.md` entry matching
this shape. K must rescan at actual implementation start. The explicit-owner
contract is already local code policy even without a registry trigger.
