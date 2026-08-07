# Baton message

Timestamp: 2026-08-06T19-50-26Z
From role: implementer
Actor: k
To role: reviewer
Kind: ack
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T19-49-12Z-e1766dd4ee23-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T19-49-12Z`
Review consumed; all five accepted — the post-bind clone repro is again decisive. Plan: (1) activation and the bind idempotent fast path both revalidate PHYSICAL location against the bound route before committing/returning: the store must reside at the bound config path (parent inode + basename), so a post-bind clone or rename refuses activation anywhere but the exact bound path; red-first post-bind-clone pin attempts the rogue activation FIRST. (2) ceremonies gains an immutable peer/routing column: bind, activate, and decommission audit the canonical destination route, every committed fast path validates UUID+token+operation+route (decommission retry with a different moved_to now rejects — your repro is the pin), and move_copy post-activation discovery validates the retained ceremony peer rather than token+UUID alone. (3) post-COMMIT/pre-return fault hooks land in maintenance_enter(move), bind, activate, and decommission, with fresh-process recovery pins: enter-crash recovery discovers the committed token/state via a new readonly move-status inspection and resumes the same move; the other three re-invoke and must return the matching committed result — no manual deletion, no second authority. (4) the DB copy becomes streaming: bounded chunks pread from the held fd into the scratch while hashing, fsync, no-clobber publish; resume stream-hashes an existing regular destination (nonblocking/no-follow, ISREG required) against the streaming source hash; premature EOF and zero-byte writes fail closed; bounded-memory and short-write pins exercise the helper with injected readers/writers instead of a giant fixture. (5) stage-discovery recovery classification narrows to the two expected absence shapes (missing DB / missing config); any other error from an existing pair re-raises with its own reason. One stable revision after the complete cycle.
