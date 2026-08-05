# Design alternatives: pending-lambda work inside deferred-call probes

Date: 2026-08-04

Status: reviewer research, deliberately non-authoritative. The implementer
must independently validate the mutation inventory and may reject the ranking,
the proposed alternatives, or the finding itself when code/probe evidence says
otherwise. Reviewer and implementer have equal responsibility for due
diligence; neither role's claim is authoritative. No shared-code implementation
should begin until the forcing rollback probe exists.

## The invariant we actually need

`NEEDS_EXPECTED` and exception rollback must restore the checker to the exact
state immediately before the speculative attempt. "Exact" includes:

- diagnostics and all lowering-visible side tables;
- callsite/node/binding allocators;
- every mutated HIR node, including nodes reached semantically through a
  binding rather than structurally through the candidate expression;
- binding/scope metadata;
- checker-global registries populated during expression typing;
- pending-work queues and their payloads.

The current transaction proves this only for a syntactically allowlisted
subtree. A direct `HVar` can cross that syntactic boundary: if its binding is a
pending lambda, typing a call through it recursively types an HLambda stored in
an earlier HLet, outside the subtree snapshot.

## Mutation closure to verify

Static inspection of `type_checker.py` indicates that resolving such a lambda
can touch at least:

- `pending_lambda_by_binding` and `binding_types` for the stored binding;
- external HLambda/HParam/HCapture attributes (`expected_fn_inferred`, binding
  ids, capture ids/lists, body rewrites, effective throw state);
- `_next_binding_id`, node/callsite allocators;
- `binding_names`, `binding_mutable`, `binding_place_kind`, and potentially
  other binding/capture bookkeeping;
- ordinary `FnCheckState` recorder tables and diagnostics;
- `fnptr_consts_by_node_id`;
- checker-global `self._lambda_fn_specs` publication;
- TypeTable interning (currently considered content-addressed/idempotent, but
  this assumption must be rechecked for any proposed isolated transaction).

The forcing audit—not this static list—decides the actual closure. The audit
must fingerprint the entire function HIR and relevant checker-global registries
as well as frame locals and the current owner.

## Option A — centralized semantic transaction-barrier authority

Before opening a deferred-call transaction, walk the candidate and query an
exact-binding predicate/effect authority. If any reachable operation can
resolve pending semantic state outside the transaction owner, do not probe;
take the existing expected-type deferral path.

This is the smallest viable design, but it is long-term acceptable only if it
is expressed as a general invariant—"candidate is transaction-pure"—rather
than a one-off check for one dictionary. The authority must account for:

- direct HCall and HInvoke callees;
- unlinked HVars using lexical binding identity or conservative rejection;
- aliases if aliases can retain deferred callable identity;
- any future binding-backed deferred producer;
- nested candidates and future safe-node additions.

Pros: preserves the proven transaction owner; low runtime and code risk; avoids
rolling back global lambda publication. Cons: may defer calls that could have
completed; correctness relies on keeping the semantic-effect classifier total.

Required evidence: demonstrate that all current paths which can type a pending
lambda cross the centralized predicate, and add a fail-closed test for an
unknown/new deferred producer category. Measure `gated_shape` delta on focused
compiles so the gate is not silently disabling the original B5 success cases.

## Option B — complete transaction owner and dependency-closed HIR snapshot

Move every mutable per-function channel reachable during a probe into the
explicit owner, and snapshot not just the candidate subtree but its semantic
dependency closure (at minimum the referenced pending HLambda and descendants).
Make checker-global publications transactional too, or stage them until commit.

Pros: preserves speculative power and makes the transaction contract semantic
rather than syntactic. Cons: much larger than adding two dicts; easy to miss a
channel; nested transaction ordering and object identity become harder; whole-
function HIR snapshots may turn the current ~100 probes per stdlib compile into
an avoidable performance cost.

This option is viable only if `check_function` is refactored so mutable state is
owned by construction, not discovered by a growing list. A partial expansion
that owns the pending map and binding types while leaving `_lambda_fn_specs` or
external capture mutation outside is worse than the current explicit gate
because it advertises a false guarantee.

Required evidence: the independent frame/whole-body/global-registry auditor is
green on rollback; nested commit + outer rollback is green; exception rollback
is green; measured probe overhead stays acceptable.

## Option C — stabilize pending dependencies before the transaction

Discover pending callable dependencies first and resolve them in the ordinary,
non-speculative checker state. The later call probe then sees only stable
function types and can use the existing owner.

Pros: conceptually separates deferred-lambda scheduling from call speculation;
no rollback of lambda internals. Cons: pending-lambda typing often needs call
argument and expected-return context. Eager resolution may permanently publish
an Unknown/wrong signature or diagnostics that a later expected-type retry
would have avoided. Recursive dependencies can also open probes during the
stabilization itself.

Do not choose this unless a matrix proves contextual equivalence for annotated
and unannotated params, inferred returns, generic outer calls, callback slots,
throwing lambdas, and failure diagnostics. "Move the existing code before
`begin_txn`" is not sufficient.

## Option D — staged/pure call-resolution plan

Refactor speculative resolution to compute a `ResolutionPlan`/structured
outcome without mutating live HIR or checker tables. COMPLETE/HARD_ERROR applies
the plan once; NEEDS_EXPECTED discards it. Diagnostics are data in the plan,
not writes later erased by an undo log.

Pros: strongest local architecture for speculative call resolution; mutation
completeness becomes explicit at plan application; avoids snapshotting mutable
graphs. Cons: recursive `type_expr` currently performs many rewrites and
registrations, so making the boundary pure is a substantial resolver/checker
refactor. A nominal plan that still calls mutating `type_expr` is not pure.

This is the preferred long-term direction if more than one semantic escape is
found or the effect gate requires continuing special cases. It may be too large
for the present slice, but the forcing probe should tell us whether that trigger
has arrived.

## Option E — persistent lambda inference variables; delete pending mutation

At lambda declaration, create a function type containing inference variables.
Uses add constraints; finalization solves or diagnoses them. The binding never
temporarily means an untracked `Unknown`, and call probes do not trigger an
out-of-tree lambda typecheck.

Pros: addresses the root scheduling model, alias/value-position inconsistencies,
and transaction escape together. Cons: largest scope; requires durable inference
variables across statements, ownership of constraints, occurs/conflict rules,
diagnostic provenance, and lowering finalization. It is a compiler inference
project, not a local patch.

This becomes justified if the parent finding demonstrates that deferred lambda
identity must flow through aliases/returns/arguments and cannot be represented
by one totalization point, or if Options A-D cannot preserve behavior cleanly.

## Provisional ranking and stop conditions

1. First force the rollback and measure the real mutation closure.
2. Prefer Option A only if it can be a centralized, future-fail-closed semantic
   effect rule with no known bypass—not merely `if bid in pending_map`.
3. Prefer Option D over a knowingly incomplete Option B.
4. Choose Option B only after owner-by-construction and global-publication
   staging are designed and measured.
5. Escalate to Option E if aliases and multiple value positions prove pending
   inference is itself the shared root defect.

Stop and return for review if:

- the forcing probe cannot produce rollback after pending resolution;
- a proposed gate needs name-based matching or a growing list of call-site
  exceptions;
- transaction expansion lacks a complete owner/global-registry inventory;
- stabilization changes accepted types or diagnostic timing;
- the fix would silently narrow the original B5 COMPLETE-probe behavior.
