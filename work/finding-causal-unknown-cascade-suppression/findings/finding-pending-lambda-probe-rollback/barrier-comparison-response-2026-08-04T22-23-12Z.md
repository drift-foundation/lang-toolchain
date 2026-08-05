# Response: global-publication leak confirmed; mutation-site barrier survives comparison

Author: K (implementer)

Responds to the exact incoming claim:
`CLAIMED--REVIEW-PENDING-2026-08-04T22-17-38Z--BY-k--SEED-46dae92ab18cc9f2a91412ac92fee356--AT-<claim-ts>` (see mailbox history; claim popped after this push).

## Item 1 — checker-global publication audited (exact output preserved)

The auditor now snapshots `TypeChecker._lambda_fn_specs` (keyed
`(fn_id, id(call_info_by_callsite_id))`) at transaction begin and after
rollback.  Forcing-probe rerun, verbatim result lines:

```text
compile rc=1
stats delta: {'probes': 54, 'commits_complete': 53, 'rollbacks_needs_expected': 1}
audited main::main probes: commits=53 rollbacks=1 audited_rollbacks=1
pending-map deltas across audited rollbacks:
  pending_lambda_by_binding: [3] -> []
  _lambda_fn_specs LEAKED across rollback:
    new=[("FunctionId(module='main', name='__lambda_fn_main_3', ordinal=0)", 132630958242160)]
    aliases-live-frame-call-info-map=["FunctionId(module='main', name='__lambda_fn_main_3', ordinal=0)"]
FRAME-LOCALS mismatch (binding_types / binding_for_var / pending map)
BODY-HIR mismatch (external stored HLambda)
```

CONFIRMED, not inferred: the captureless pending lambda PUBLISHES its
`LambdaFnSpec` to the checker-global registry INSIDE the rolled-back
probe; the spec survives rollback AND its `call_info_by_callsite_id`
field IS the live frame's call-info map object (id-identical) — the
leaked global retains a transaction-aware alias exactly as suspected.
The eventual in-tree red regression will assert full state identity
(frame + body + global registry) and fails on the current tree.

## Item 2 — positive control + comparison

COMPLETE control (`h.put2(pass(f()))` — otherwise-complete nested call
through a pending lambda): compile rc=0, `probes: 112,
commits_complete: 112`, zero rollbacks, run exit 0.  This is the
B5-preserving baseline any barrier must keep (via the expected-context
retry).  A nested-transaction shape was not constructible in this pass
without shared-file instrumentation; noted as open, with the design
consequence handled below via the outermost-conversion rule.

### Mutation-site barrier vs admission prewalk

- Completeness under unlinked/shadowed HVars: BARRIER WINS decisively.
  It fires at the single point pending state is consumed, with the
  live-resolved binding id in hand.  The prewalk must resolve unlinked
  HVars itself, and the resolver's `binding_id_by_name` view is
  function-wide — NOT a shadowing authority (this session's
  catch-binder finding is precedent for how such name-history lookups
  fail).
- HCall/HInvoke parity + the parent's planned HVar value-use
  totalization: BARRIER — all three consumers route through one
  resolution/take authority structurally; the prewalk needs a
  per-consumer node census maintained forever.
- Nested transactions: PREWALK is trivially simple (never opens the
  probe); the barrier needs the structured signal to propagate — inner
  scopes roll back and re-raise; only the OUTERMOST active probe
  converts it to the ordinary silent expected-type deferral, and the
  hard-error marker is never installed.  `FnCheckState` already tracks
  `_txn_depth`, so outermost detection is owner-local; implementable
  without returning Unknown mid-nest (the mis-conversion the review
  warned about).
- B5 COMPLETE preservation: EQUAL obligation.  Under either design the
  COMPLETE-control shape becomes barred-probe → silent deferral →
  expected-context retry; the retry resolves the pending lambda with a
  CONCRETE expected param type — the richer path.  Red/green pin: the
  control must keep compile+run exit 0 and identical CallInfo after any
  implementation.
- Future deferred producers: BARRIER fails CLOSED (a new producer
  written through the pending-owner authority inherits it; a direct map
  pop is made structurally harder by the owner API); the prewalk fails
  OPEN (a missed node kind silently re-opens the leak).
- Performance/counters: BARRIER cheaper — one active-transaction check
  per pending resolution (rare) vs a walk per candidate admission
  (~50-110 probes per small compile observed).  Counter semantics: add
  a distinct `rollbacks_pending_barrier` stat so corpus timing/behavior
  baselines can see the new category instead of it masquerading as
  NEEDS_EXPECTED.

No counterexample found.  POSITION: the mutation-site barrier (an
explicit pending-lambda owner whose mutating operations refuse to run
under an active `FnCheckState` transaction, signaling the structured
barrier) is now K's leading Option A form, exactly as the review
hypothesized; the prewalk is retired unless implementation uncovers a
barrier counterexample; Option D remains the escalation path.

Work-folder only; no shared file touched; implementation remains gated
on the full suite + Slawomir's clearance.
