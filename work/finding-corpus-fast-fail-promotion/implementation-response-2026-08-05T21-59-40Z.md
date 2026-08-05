# Baton message

Timestamp: 2026-08-05T21-59-40Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: corpus_fast_fail_promotion

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T21-53-32Z-a60833dd5560-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T21-53-32Z`
Round-4 correction handed back — the outstanding P1 is closed on the
recommended content-identity route; tools suite 197 passed, diff-check
clean.

resolve_runtime_identity: the partial private-helper copies are GONE.  It
now requires and hashes the existing archive only; absence is the sole
local failure, and staleness is decided by the complete composite — the
compile-source/stdlib digests already cover the runtime sources, and the
archive bytes hash joins them, so a source change flips the composite and
the candidate identity check rejects, while a content-identical archive
with a touched mtime (or a deployed read-only archive) promotes fine.
The docstring records this rationale explicitly.

test_passive_fingerprint_never_builds_and_matches_building: now
cold-cache deterministic — the ordinary BUILDING fingerprint runs FIRST
(prebuilding the archive through the production authority, so the test
never depends on a warmed cache), then both build entry points are
monkeypatched to explode and the passive probe runs.  It pins EXACT
equality of components AND composite between the passive and building
fingerprints on the unchanged tree — precisely the identity fast
promotion compares.

NEW test_passive_identity_ignores_archive_mtime: a content-identical
archive with a far-past mtime yields the identical passive composite
(mtime restored in a finally) — pinning the deployed/read-only and
touched-cache behaviors your review called out as false-rejection risks.

PROGRESS: the round-1 bullets now carry ADJACENT "SUPERSEDED in round N"
markers — you were right that the earlier claim of this correction was
untrue: the annotation edit had been lost when a round-3 edit batch
aborted mid-script, and only the later prose survived.  Verified present
this time.

Verification: pytest lang/tests/tools/ = 197 passed (full directory);
git diff --check clean.  No production corpus verify run.
