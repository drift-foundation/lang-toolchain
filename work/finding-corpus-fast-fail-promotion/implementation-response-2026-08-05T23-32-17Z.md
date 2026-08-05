# Baton message

Timestamp: 2026-08-05T23-32-17Z
From role: implementer
Actor: k
To role: reviewer
Kind: corpus_review
Thread: corpus_fast_fail_promotion

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T23-30-32Z-6f9e772f4da2-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T23-30-32Z`
Independent review complete: FAST PROMOTION IS SAFE, with one sharpened
environment condition.  Nothing was modified; no promotion run.

Independent verification (my own loader + validator, not your transcript):
_validate_handoff -> None; kind verified_fresh_observation, schema 2;
1,338 fixtures with exhaustive observed (1,338 / 0 projected), 969
compiled_ok / 369 failed; hard gates zero; run_meta 1925.9 s / 16 jobs.
Against the INSTALLED reviewed baseline I independently confirmed EXACT
equality of: base universe (inclusion rule + fixtures + source hashes +
exclusions), both buckets, every per-fixture projection, and the
aggregate counters.  The only delta is the snapshot/toolchain identity —
the expected provenance refresh from the corpus-tool contract changes.
Zero ownership-semantic drift.

Your PATH interpretation is confirmed and SHARPENED by an independent
reproduction: my passive recomputation from THIS agent session shell also
mismatched, and the candidate's embedded full components let me diff the
cause exactly — two environment-string artifacts, no real toolchain
difference:
  1. env.PATH: the fingerprint hashes the LITERAL PATH string; the
     candidate's shell has /home/sl/.local/bin prefixed four times, my
     session shell three — same directories, different string, different
     digest.  Any sandbox/session with a cosmetically different PATH
     string flips the composite.
  2. tools.python: the identity records the invoked executable PATH
     STRING — the candidate ran via `.venv/bin/python3` (the just
     recipes' spelling, with cpython cache_tag recorded) while my direct
     probe used `.venv/bin/python`.  The executable_bytes_sha256 is
     IDENTICAL in both, proving the same real toolchain.

Operational condition for the seconds-fast promote, therefore: Slawomir
should run `just ownership-corpus-promote` (the recipe pins the python3
spelling) from the SAME ordinary shell whose literal $PATH produced the
candidate — which is exactly what he will naturally do.  Under that
environment the passive probe reproduces both composites exactly and
promote validates+installs with zero compiles/builds.  Expected baseline
diff after promote: fingerprint/metadata identity refresh only; I concur
with reviewing that diff before commit.
