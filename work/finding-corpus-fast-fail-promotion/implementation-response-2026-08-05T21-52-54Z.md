# Baton message

Timestamp: 2026-08-05T21-52-54Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: corpus_fast_fail_promotion

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T21-47-01Z-cc8a6aa1b294-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T21-47-01Z`
Round-3 corrections handed back — all seven contradiction spots fixed and
the P2 boundary pin added; tools suite 196 passed, diff-check clean.

The three active-guidance fixes: the gate doc's check bullet now states
report-only (work-dir report + deltas; candidate comes from verify alone);
run_check's lane comment no longer names promote as the reviewed gate over
an exported handoff (it directs at verify as the single fresh authority);
and the hard-gate NOTE now says a fresh verify would emit no promotable
candidate for this state, instead of implying check produced one.

The four record/test fixes: both test module docstrings state the
report-only/fast-fail contract; the bootstrap comment's "only second full
run" is now "fast promote installs with zero compiles"; the digest test's
comment carries the canonical-parsed-payload semantics; and PROGRESS.md's
round-1 section is annotated in place with SUPERSEDED markers (byte-flip
wording, and "source hashing only" corrected to include the passive
runtime-artifact resolution) so the implementer record is internally
consistent while preserving round history.

P2: test_promote_rejects_invalid_metadata_candidate_at_boundary — a
deliberately RESEALED candidate with jobs=0 goes through run_promote
itself: controlled exit 2, the passive identity probe monkeypatched to
explode (proving candidate validation rejects BEFORE any current-tree
work), and baseline bytes unchanged — malformed metadata can never reach
_emit_run/installation.

Verification: pytest lang/tests/tools/ = 196 passed (full directory);
git diff --check clean.  Production verify/promote smoke remains
Slawomir-owned.
