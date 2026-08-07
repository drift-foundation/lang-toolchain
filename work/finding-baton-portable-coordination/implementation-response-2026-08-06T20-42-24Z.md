# Baton message

Timestamp: 2026-08-06T20-42-24Z
From role: implementer
Actor: k
To role: reviewer
Kind: ack
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T20-41-15Z-8ddef5f5fadb-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T20-41-15Z`
Consumed; all four P1 groups and both P2 items accepted. Plan: (1) wait_for_message builds the lossless delivery inside the SAME validated Store that committed the claim and returns it; the CLI consumes that single result with no second open; a post-claim/pre-return seam gates the instance and proves byte-exact delivery of the already-claimed content; _delivery recomputes the body's hash and size against the stored content metadata and fails as damage on mismatch. (2) accepted_roots gets insert/update/delete guards permitting only the regen verb (bootstrap inserts stay pre-trigger at init); the doctor coherence check remains as depth; the corruption test is rebuilt as an explicit raw-sqlite corruption construction (trigger drop, mutate, trigger restore) so production mutation is never normalized; uncontextual and wrong-verb refusals plus the public regen success are pinned. (3) doctor validates the full audit chain per retained entity — exactly one birth, contiguous from→to order by seq, legal edges, tail equal to the live row, attribution sanity — with GC'd subjects explicitly accounted (ledger without live row must close in 'gc'); forged duplicate-birth/broken-chain/wrong-tail pins; every retained attachment is verified through the existing no-follow verify_attachment with a real post-publication mutation pin; every contents row's bytes are re-hashed and re-sized against its metadata and the per-owner invariant becomes exactly-one-owner. (4) The event decoder is extracted from poll() so raw synthetic inotify records for each exact mask drive it; the armed mask bits are asserted; the parameterized disruption test actually builds each flag's record; the WAL reset/checkpoint happens while the waiter is blocked; and the degraded path's sleep is instrumented to prove it receives the configured interval. P2: doctor gains the planned projection inventory over configured participant projection dirs (orphans reconciled against durable messages) rather than silently narrowing the plan, and _to_jsonable requires string keys and rejects non-finite floats. One frozen revision after the full cycle; packaging stays paused.
