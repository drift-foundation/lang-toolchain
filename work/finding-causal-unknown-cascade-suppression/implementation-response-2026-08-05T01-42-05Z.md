# Implementation handoff

Timestamp: 2026-08-05T01-42-05Z
Role: implementer
Actor: k

Responds to the exact incoming claim:

`CLAIMED--REVIEW-PENDING-2026-08-05T01-40-35Z--BY-k--SEED-46dae92ab18cc9f2a91412ac92fee356--AT-2026-08-05T01-40-48Z`
# Planning addendum response: existing-test edit enumeration (K)

Author: K (implementer), actor `k`, seed `46dae92ab18cc9f2a91412ac92fee356`

Responds to the claim recorded in the baton receipt for
`REVIEW-PENDING-2026-08-05T01-40-35Z` (target sha256
cee905daa73758dcc878a34fe17f0b80d4097a0ad03380181a2a3ab0797a5034).

## Existing-test edits planned: NONE

After auditing every guard the implementation touches, the current plan
requires ZERO existing-test edits:

- lang/tests/driver/test_implicit_callback_wrap.py
  (test_site5_typed_let_bare_lambda_to_callback1 and siblings): verified
  — these assert only `errors == []` at the CHECKER boundary (which is
  why they pass today while the driver ICEs at MIR).  The wrap
  restoration keeps checker diagnostics empty, so the assertions hold
  unchanged.  The real end-to-end pins land in NEW full compile/run
  regression files per the reviewer's own preference.
- test_stored_capturing_lambda_diagnostic.py /
  test_uninvoked_stored_lambda.py: the uninvoked/unreferenced flush
  contracts they pin are untouched (first-VALUE-USE finalization only
  fires on references; unreferenced pendings still flush identically).
  Referenced-alias shapes are NEW tests.
- test_type_checker_copy_unknown.py: the independent-Unknown tripwire
  stays a tripwire; unchanged.
- test_pending_lambda_probe_barrier.py /
  test_defer_probe_state_transaction.py: owner API and probe counters
  are extended, not changed, for the new HVar consumer; current shapes'
  counter expectations are not expected to shift.

## Contingencies (NOT requested now; will use a fresh approval gate if
they materialize)

1. If the finalizer changes probe-counter deltas in the two transaction
   modules' driver shapes, those exact counter assertions would need
   updating (semantics-neutral, coverage-preserving).
2. If the Site-5 restoration surfaces a diagnostic-count change in any
   test_implicit_callback_wrap.py site (none expected), that exact
   assertion would need a semantics-stating request.

## For clarity (not test edits, no approval required)

The two PRODUCTION source comments that overclaim causality
(type_checker.py `_require_copy_value` guard; call_resolver.py
binding-call guard) are corrected as part of the implementation, per the
plan's Phase 7 item 4.

## New test files planned (no approval needed)

- lang/tests/type_checker/test_causal_unknown_provenance.py
- lang/tests/driver/test_pending_lambda_value_finalization.py
- lang/tests/driver/test_typed_callback_slot_wrap_restoration.py
  (typed-let + argument-slot + return-slot compile/RUN pins, the
  bare-pending-arg ICE, the named-fn borrow AttributeError, and the
  pending-borrow finalize-and-accept contract)

Planning remains open for the reviewer's terminal acceptance; shared
implementation stays blocked on the full-suite gate.
