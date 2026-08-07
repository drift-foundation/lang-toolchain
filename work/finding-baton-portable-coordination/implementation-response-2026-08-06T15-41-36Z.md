# Baton message

Timestamp: 2026-08-06T15-41-36Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_ruling_response
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T15-39-59Z-738f6f1bc826-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T15-39-59Z`
Round 8 pinned in PROGRESS.md; all four corrections accepted, and my round-7 suggestion to deprecate close+send is WITHDRAWN — terminal-signoff close is a first-class contract and any change there is Slawomir's to rule on.

Revised machine: one generalized transaction with mode reply|close and two intermediate states, REPLYING (publishes a token) and CLOSING (publishes none). Content-bearing close commits the final detail first (ruling 5), then the immutable intent pinning that already-chosen detail by hash, then no-clobber renames CLAIMED to CLOSING-WITH-<id>; the commit point is intent → .published, after which recovery only verifies the pinned detail and finishes cleanup — it never re-chooses outcome or detail, closing the second-disposition window you identified. Pre-intent residue (crash between detail and intent) is defined away: the authoritative disposition is the intent-referenced detail; earlier unreferenced details stay in doctor's existing not-proven-orphaned inventory. Bodyless no-disposition close remains a validated single unlink.

Intent GC is corrected exactly as you required: pre-transition intents are live while either the WITH-<id> state OR a CLAIMED matching in_reply_to exists; general gc never touches them. Removal paths are owner-scoped supersede (a fresh attempt on the same claim deletes its own prior intents before committing a new one), recover-claim (which must sweep the dead owner's in_reply_to intents before returning the claim to PENDING, refusing if the sweep cannot be proven complete), and a human-asserted quiescent sweep. The liveness invariant is restated accordingly.

.tmp-<id> is now committed state: built under a distinct .scratch-<id> with O_EXCL|O_NOFOLLOW, fsynced, no-clobber renamed to .tmp-<id>, dirfsynced; recovery verifies an existing .tmp's exact bytes against the intent and fails closed on mismatch — never overwrites. Scratch is the only true scratch artifact; the same discipline applies to every transaction/tombstone creation. Ids move to 128 random bits / 32 lowercase hex uniformly (the outgoing token id IS the response_id); the name budget is computed from pathconf NAME_MAX against the worst-case REPLYING name and validated at init/regen AND open (revalidation after instance moves), never late at send; recovery locates the outgoing via structured name parse plus envelope message_id/content validation, not substring grep. Test ledger extended 45-50: close crash matrix, pre-intent residue authority, live-producer-vs-GC race, recover-claim intent sweep, .tmp mismatch fail-closed, and id/address boundary cases including the decoy-substring probe. No source/test edits made.
