# Research and implementation plan

Timestamp: 2026-08-05

This is a reviewer design hypothesis. K should independently verify format, staleness, and install details and may propose a simpler or safer implementation that preserves the fast-or-fail contract.

## Current seam

`tools/drift_corpus_check.py` currently uses one schema-1 handoff for two semantically different producers:

- `run_check` may export a mixture of observed and projected cached results;
- `run_verify` exports a complete fresh observation (`observed` exhaustive, `projected=[]`).

`run_promote` validates the handoff's composites/universe, then calls `_compile_set` over every fixture and compares the second run to the handoff before `_staged_install`.

Fast promotion cannot safely install a projected check handoff. The producer distinction must become explicit and fail closed.

## Proposed shape

1. Make the canonical promotion candidate a fresh-verify-only schema (bump `HANDOFF_SCHEMA_VERSION` or rename it to a candidate schema).

   The atomically published JSON should contain, at minimum:

   - an exact producer/kind marker such as `verified_fresh_observation`;
   - the complete run snapshot object needed for `fingerprint.json`, not only its composites;
   - exact verify-run metadata (`started_unix`, measured `duration_s`, `jobs`, Python/repo metadata or a normalized metadata object accepted by baseline emission);
   - full universe/source hashes and compiled/failed buckets;
   - per-fixture projections and merged counters;
   - exhaustive `observed` and empty `projected` partitions;
   - a canonical payload digest if useful for accidental-corruption detection.

   Avoid duplicating the large projections map merely to represent baseline files. Prefer one normalized semantic payload plus deterministic baseline rendering.

2. Publish the candidate from `run_verify` only after fixture-hash stability, end-snapshot equality, and zero hard gates. Exact matches and valid drift both publish. Invalid/aborted/hard-gate runs publish nothing, as in the just-landed contract.

3. Stop `run_check` from writing `HANDOFF_PATH`. It should continue to write its work-dir records/report and print projected deltas for fast iteration. If a developer-facing export remains useful, give it a different non-promotable filename/schema so it cannot overwrite the verified candidate; the cleaner pre-1.0 contract is report-only check.

4. Rewrite `run_promote` as validation/install only:

   - discover the current universe and compute the current toolchain/run snapshot under `_corpus_lock`;
   - load the candidate and validate exact key sets/types/schema/digests;
   - require the fresh-verify producer marker, exhaustive observed universe, empty projected set, zero hard gates, internally consistent buckets/projections/counters, and complete install metadata;
   - require candidate snapshot and universe/source hashes to equal the current recomputed values;
   - on any mismatch, return controlled failure before `_staged_install`, with no compiler call, scratch directory, retained actual, or baseline mutation;
   - on agreement, render/stage the baseline from the candidate and retain the current post-install reload/equality/fingerprint proof;
   - preserve the candidate's verify-run metadata instead of calculating duration from the later promotion timestamp.

5. Delete the old promotion compile/reproduction branch. Do not leave a compatibility flag or fallback. Remove `-j` from the promote recipe/help surface and reject an explicitly supplied `-j/--jobs` with `--promote`; worker count remains valid for check/verify only.

6. If independent reproduction remains valuable, expose it as a separately reviewed command/recipe (for example `ownership-corpus-reproduce`) or defer it to a follow-up. It must not be silently invoked by normal promotion. Do not add the optional command merely to preserve old code.

## Validation matrix

1. Verify drift -> promote:
   - verify compiles every fixture exactly once;
   - promote compiles zero fixtures;
   - total workflow compiler count is one per fixture;
   - installed baseline exactly equals the verified candidate observation.
2. Verify exact match -> promote:
   - candidate validates;
   - promotion is either a byte-identical no-op or an identity/metadata refresh derived from that candidate;
   - zero compiles.
3. Missing/malformed/wrong-schema/wrong-producer candidate:
   - fast controlled failure;
   - no compile/scratch/baseline mutation.
4. Projected/non-exhaustive candidate and legacy check handoff:
   - rejected as non-promotable;
   - check cannot overwrite the canonical verified candidate.
5. Tree/toolchain/universe/source change after verify:
   - snapshot mismatch rejects before install and before any fixture compile;
   - diagnostic instructs a new verify.
6. Candidate with nonzero hard-gate counters, bucket overlap, projection/counter mismatch, bad metadata, or fingerprint inconsistency:
   - rejected fail closed.
7. Install failure/post-install mismatch:
   - controlled infrastructure failure under the existing staged-install guarantees; no compile fallback.
8. Metadata provenance:
   - delayed promotion preserves verify's duration/start/jobs rather than measuring the delay.
9. CLI/docs/history:
   - promotion help and recipe contain no worker count or recompilation claim;
   - lifecycle documents one full verify run plus fast promotion;
   - incremental check remains documented as non-promotable exploratory feedback.
10. Real production smoke:
   - the first post-change verify candidate promotes on an unchanged tree in seconds and yields only the reviewed baseline delta.
11. Candidate payload corruption:
   - a modified byte/digest mismatch fails before snapshot work, compilation, scratch creation, or baseline mutation.
12. Metadata provenance:
   - delayed promotion installs the verify run's measured duration/start/jobs rather than incorporating the review delay.
13. Obsolete worker option:
   - `--promote -j N` and `--promote --jobs N` fail clearly; check/verify worker options remain supported.

## Likely files

- `tools/drift_corpus_check.py`
- `lang/tests/tools/test_corpus_verify_candidate.py`
- `lang/tests/tools/test_drift_corpus_check.py`
- possibly `lang/tests/tools/test_ownership_corpus_check.py` for recipe/CLI assertions
- `justfile`
- `doc/ownership-corpus-gate.md`
- pending 0.35.0 entry in `doc/history.md`

Do not edit existing tests until Slawomir approves the final exact ledger.
