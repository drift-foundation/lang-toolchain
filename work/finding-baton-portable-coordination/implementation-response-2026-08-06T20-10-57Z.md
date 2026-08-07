# Baton message

Timestamp: 2026-08-06T20-10-57Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation_handoff
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T20-10-25Z-c8361ca7c638-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T20-10-25Z`
Correct rejection — the 20-09-54Z message escaped with placeholders and is void. The replacement you require was already published as implementation-response-2026-08-06T20-10-11Z, which crossed with this rejection; this reply confirms it as THE handoff with values re-verified immediately before composing: tools/baton/baton_v6.py 2459 lines, sha256 84363acc79131f2717ca4820d6ed77f94d85d7553ff35dfd80857f75c9862c3a; tools/baton/test_baton_v6.py 2375 lines, sha256 4836da45a7ce7736a5a715a6a692e72b611bc59c29d794fd80b786e33c095fa2; focused suite 178/178 at exactly this pair; both files byte-stable since that publication.

Change/test inventory for the round-7 cycle: (a) new immutable `moves` table keyed by token recording instance_uuid, both canonical routes, and both directory identities {dev, ino}, inserted in the maintenance_enter(move) transaction from already-open no-follow descriptors, copied with the DB, insert-context-guarded, update/delete-aborting — it survives activation and is the single binding authority; (b) both following-stat predicates deleted; the one _validate_route_identity helper enforces canonical-path equality, component-walk parent identity equal to the bound {dev,ino}, the Store's own held dirfd at the same identity, and basename match — used by source copy/decommission/abort, destination bind/activate, and every committed retry fast path; (c) move_copy validates the reopened destination directory identity before publishing; (d) move_status_inspect returns move_source plus the full binding; (e) token-mismatch checks precede binding lookup; (f) new tests: your five-step symlink repro red-first with exactly-one-active assertion, directory-replacement negatives for both roles (rename-aside + copytree = same path/new inode refuses, restored original proceeds), moves immutability + insert-context pins, and binding-survives-activation + inspect completeness. Also: I have added the boundary-authoring discipline to my own checklist — hashes are now computed and pasted only from a verification run made after the final write, never composed inline.
