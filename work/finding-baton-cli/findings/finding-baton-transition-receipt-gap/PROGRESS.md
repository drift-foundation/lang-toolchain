# PROGRESS: finding-baton-transition-receipt-gap

Last updated: 2026-08-04 (K, actor `k`, seed `46dae92ab18cc9f2a91412ac92fee356`)

STATUS: VALIDATED — the `adopt` transition closes the gap; recommending
closure.

## Independent validation (isolated mailbox via MAILBOX_REPO_ROOT; the
live work/ mailbox was NOT touched)

Positive path (test seed `aaaa...11`):
1. Manual v3 publish (detail → pending token) + manual v3 claim —
   reproducing the pre-adoption state exactly.
2. `implementer handoff` WITHOUT receipt: refused, exit 4,
   "claim receipt does not exist", no mutation — the original gap.
3. `implementer adopt <claim>`: exit 0, `status: adopted`, receipt
   minted at the same path the refusal named; claim NOT popped, no
   response published — exactly the announced contract.
4. `implementer handoff` after adopt: exit 0, `status: completed` —
   response detail + outgoing token published, claim popped.

Negative paths (all exit 4, no mailbox mutation):
- wrong seed (claim held by `bbbb...22`, adopt attempted with mine):
  "only the exact actor instance named by the claim may adopt it";
- wrong actor (same message);
- split state (forged claim COPY with the original pending token still
  present): "cannot adopt split mailbox state; original pending token
  still exists: REVIEW-PENDING-..." — the most important guard, since a
  copy-based forgery is precisely what a receipt would otherwise
  legitimize.

## Agreement

Validation agrees with the reviewer's 12/12: fail-closed preserved
(receiptless response actions still refuse; no implicit fallback), the
transition is explicit and exact-instance-bound, and split-state
adoption is impossible.  Resolution option 2 as implemented is
sufficient; no counterexample found.  The historical manually-completed
handoff stands as evidence and needs no reconstruction.  Recommend
CLOSING this child.
