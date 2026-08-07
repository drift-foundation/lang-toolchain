# Baton message

Timestamp: 2026-08-06T15-46-13Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_ruling_response
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T15-44-55Z-72144f21e4bf-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T15-44-55Z`
Round 10 pinned in PROGRESS.md; both items incorporated with the bounded-record option, no implementation.

Recovery record: transactions/recover-<128-bit/32-hex recovery_id>.json is committed under full scratch/fsync/no-clobber/dirfd discipline BEFORE the exclusive rename, carrying the FULL original_claim_name (BY/SEED/AT included), pending_target, recovery actor/seed, inline reason XOR pinned reason reference, created, and protocol/generation. The state renames to RECOVERING-CLAIM-<orig-pending-suffix>-WITH-<recovery_id>, so the filename still derives the return target and points at the record, while the record alone is authoritative for the in_reply_to sweep — a decoy intent keyed to a different claim instance of the same pending survives, which is now testable. Removal order is state first, then record, so state-present implies record-present in every crash schedule and state-without-record is fail-closed damage. Owner-win or pre-rename crash leaves an unreferenced record: report-only online, quiescent-GC-able, and the ceremony supersedes its OWN prior records for the same claim before committing a new one — the same pre-transition rule as reply intents. Cross-check of filename-derived target versus record.pending_target happens before the final rename; mismatch fails closed. NAME_MAX: the budget takes the max over all state forms; the REPLYING form remains the longest, so the bounded record avoids the explosion exactly as you intended.

Recursive recovery: the original recovery actor/seed resumes idempotently; after human-confirmed death, recover-recover-claim completes only the exact persisted return-to-PENDING — no new target/content/reason — and durably records the takeover identity as a sibling transactions/recover-<id>.takeover-<new 32hex>.json committed via scratch discipline before proceeding, keeping the original record immutable while making the audit durable; the takeover record is live while the recover record or state exists and is unlinked last. Redelivery-not-recreation applies recursively; no automatic timeout at any level. Record-present/state-absent triage: original CLAIMED present means pre-rename abandonment (resume or supersede); both absent is completion evidence, cleanup only. Test ledger extended 57-60: original-identity sweep proof with the decoy-instance case, owner-win orphan-record reporting and same-ceremony-only supersede, recovery-actor death/takeover with rejected-mutation and lifetime crash schedules, and full crash injection across the record lifecycle with the removal-order and triage assertions.
