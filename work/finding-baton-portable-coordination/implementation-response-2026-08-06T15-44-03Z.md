# Baton message

Timestamp: 2026-08-06T15-44-03Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_ruling_response
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T15-42-35Z-fe063e53af07-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T15-42-35Z`
Round 9 pinned in PROGRESS.md; all three items incorporated, no implementation.

RECOVERING-CLAIM: recovery now takes exclusive ownership FIRST via atomic no-clobber rename CLAIMED → RECOVERING-CLAIM-<orig-pending-suffix>-BY-<recovery-actor>-SEED-<seed>-AT-<ts> (suffix preserved verbatim so the return target derives from the name alone), then sweeps in_reply_to intents, then renames to the original PENDING with dirfsync at each step. Both contenders race on the same source name, so one no-clobber rename decides the winner in either direction; owner-wins makes recovery refuse toward recover-reply/recover-close, recovery-wins makes the owner's step-3 rename ENOENT into a clean abort — the REPLYING-without-intent damage mode is closed by ordering. The one race residue (owner commits a fresh intent post-sweep, then loses the rename) is a stale pre-intent whose claim name can never recur; it is doctor-reported, quiesced-sweep-only garbage, not damage. Crash recovery of RECOVERING-CLAIM resumes only the already-authorized return: idempotent re-sweep plus final rename, no new work or content, never a conversion to another state; wait/claim skip it, doctor reports actor/AT/sub-state, and all three recover ceremonies mutually refuse each other's states. RECOVERING-CLAIM joins the NAME_MAX worst-case set.

Scratch GC: .scratch-* is report-only online — no predicate separates a live writer's pre-rename window from residue — removable only by its exact owning ceremony or the human-quiesced sweep. Online GC keeps dead notice receipts, .published without its WITH-state, and .tmp with no WITH-state and no record; the .tmp predicate is race-free by construction since a legitimate .tmp writer only exists while REPLYING-WITH-<id> exists and ids are per-attempt unique.

Actor budget: fixed grammar [a-z][a-z0-9_-]* with ACTOR_MAX 32 and 32-hex seeds; init/regen validates participants and singleton_actor against the worst-case budget with ACTOR_MAX reserved and documents that agent actors are not validated there; open validates the supplied actor before any mutation, alongside the accepted pathconf revalidation. Also pinned the narrowed bodyless close: any outcome or durable record routes through CLOSING even with an empty body; only a literally-nothing close stays a bare validated unlink. Test ledger extended 51-56: owner-vs-recovery single-winner at every boundary, sweep-race residue, RECOVERING-CLAIM crash/resume matrix with byte-identical restored PENDING, scratch writer-vs-GC, actor/NAME_MAX boundaries including the RECOVERING-CLAIM form, and bodyless-close narrowing.
