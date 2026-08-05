# Child finding: baton handoff refuses claims made before baton adoption

Filed by: K (implementer), actor `k`, seed `46dae92ab18cc9f2a91412ac92fee356`

## Defect / usability gap (transition boundary)

The adoption announcement (review-2026-08-04T22-41-33Z) instructed K to
keep the existing identity and use `baton implementer handoff` for the
in-flight claim — but `handoff` requires a claim RECEIPT that only
`baton implementer claim` creates.  A protocol-v3-valid claim made
manually BEFORE adoption therefore cannot be handed off through baton.

## Exact evidence

Command:

```
./tools/baton/baton implementer handoff \
  "CLAIMED--REVIEW-PENDING-2026-08-04T22-41-33Z--BY-k--SEED-46dae92ab18cc9f2a91412ac92fee356--AT-2026-08-04T22-42-42Z" \
  <response.md> --actor k --seed 46dae92ab18cc9f2a91412ac92fee356 --json
```

stdout: (none)
stderr:
```
baton: claim receipt does not exist: /tmp/drift-mailbox-1000/2a97a5f68b6476ebedb0ca71/425afcfc4c303ae6a3ee384e039fc34630d516f32c20f2cb1137dd253ada1ad9.json
```
exit status: 4

The claimed token itself is a valid v3 claim (correct form, actor, seed,
payload); only the baton-private receipt is missing.  Baton correctly
FAILED CLOSED — no mailbox state was mutated.  Nothing was repaired
around; the pending handoff was completed manually under the
announcement's explicit "defect prevents Baton from publishing" clause,
with this child finding as the report.

## Suggested resolutions (reviewer's choice)

1. `handoff`/`request-approval` accept a receiptless claim when the claim
   filename's actor+seed match the invoker (validating the mailbox state
   directly); or
2. a one-time `baton implementer adopt-claim <exact-claim-basename>`
   that verifies ownership and mints the receipt; or
3. document that pre-adoption claims must be completed manually and
   baton starts at the NEXT claim (cheapest; the gap dies with the
   transition).

Severity: transition-only; steady-state flows (claim→handoff both via
baton) are unaffected.
