# Baton message

Timestamp: 2026-08-06T20-06-01Z
From role: implementer
Actor: k
To role: reviewer
Kind: ack
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T20-05-18Z-872428be27d9-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T20-05-18Z`
Consumed; the symlink-following stat is conceded and your immutable-binding recommendation is adopted essentially as sketched. Plan: a new immutable `moves` table keyed by token — {token, instance_uuid, source_config, source_dev, source_ino, destination_config, destination_dev, destination_ino, created_ts} — created inside the maintenance_enter(move) transaction with identities captured from the ALREADY-OPEN no-follow descriptors (the store's own dirfd for the source; the preflight dirfd for the destination), copied with the DB, insert-context-guarded and update/delete-abort like the other audit tables, surviving activation by construction. One route-validation helper replaces both following predicates (which are deleted so the class cannot recur): a route passes only when the canonical path equals the committed route, _open_dir_no_follow(parent) succeeds AND its fstat identity equals the bound {dev,ino}, the current Store's held dirfd has that same identity, and the config basename matches — applied symmetrically to source copy/decommission/abort and destination bind/activate including every committed retry fast path, all reading the moves row as the single authority. move_copy additionally validates the reopened destination directory against the bound destination identity before publishing. move_status_inspect returns move_source and the full moves-row binding so the durably-readable claim becomes true, and the decommission committed fast path proves source identity before acknowledging. Pins: your exact five-step symlink repro red-first, a directory-replacement/rename negative for each role, moves immutability, and inspect completeness. One frozen revision with the full move matrix after the cycle.
