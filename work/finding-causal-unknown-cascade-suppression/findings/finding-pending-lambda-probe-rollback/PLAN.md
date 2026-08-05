# Plan: close pending-lambda deferred-probe rollback leak

This child is research-first and remains gated on the active full suite for
shared edits. Work-folder compiler probes are authorized.

1. Re-read the parent finding, this child, current mailbox protocol, and
   `doc/refactor_triggers.md`.
2. Extend a work-only probe with an audited NEEDS_EXPECTED rollback whose
   candidate subtree resolves a pending lambda before encountering the
   expected-dependent failure.
3. Record exact before/after values for `binding_types`,
   `pending_lambda_by_binding`, the pending HLambda structure, owner
   fingerprint, and allocator cells. Attribute the rollback to the exact
   candidate node/binding, not aggregate counters.
4. If no mutation survives, reject/revise this finding with evidence. If it
   does, classify the repro as the mandatory failing regression.
5. Complete the alternative matrix in `DESIGN.md` with evidence from the
   forcing probe. In particular, inventory `_lambda_fn_specs`, HLambda/capture
   mutation, binding metadata, allocator movement, and TypeTable effects.
6. Prefer a new in-tree regression file. Request Slawomir's approval before
   editing `test_defer_probe_state_transaction.py` or any other existing test.
7. Select an architecture only after the proof:
   - choose a semantic-effect gate only if pending-lambda resolution can be
     defined and recognized as one complete centralized transaction barrier;
   - choose transaction expansion only if the whole reachable mutation closure
     has an explicit owner and dependency-closed HIR rollback;
   - otherwise prototype staged resolution or persistent inference rather than
     accumulating exceptions around the current probe.
8. Add red-first boundary tests for the selected invariant, then implement it.
   Do not encode the expected implementation (`gated_shape`, for example) in
   user-semantic tests; transaction unit tests may pin the mechanism once the
   architecture is chosen.
9. Keep the original `id(f())` successful control and ordinary non-pending
   NEEDS_EXPECTED rollback control. Prove diagnostics and resolution metadata
   match the non-speculative path.
10. Run focused resolver/transaction/pending-lambda tests. Defer broad gates
    until review converges.
11. Record evidence, rejected alternatives, performance measurements, and
    disagreements in implementer-owned child `PROGRESS.md`, then hand off
    atomically under protocol v3.

No implementation is pre-approved by this plan. In particular, do not add only
`binding_types` and `pending_lambda_by_binding` to `FnCheckState`, and do not
land a one-off pending-map gate without the centralized-barrier proof.
