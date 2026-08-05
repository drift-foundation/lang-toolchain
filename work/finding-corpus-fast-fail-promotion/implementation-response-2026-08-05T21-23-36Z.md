# Baton message

Timestamp: 2026-08-05T21-23-36Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: corpus_fast_fail_promotion
Fast-or-fail promotion is implemented end to end under the accepted plan
and pre-approved ledger — tools suite 185 passed (twice), diff-check
clean.  PROGRESS.md carries the full record; the shape in brief:

- Schema-2 candidate: kind marker, FULL run-snapshot object, the verify
  run's measured metadata (duration measured at publication), and a
  payload_sha256 seal verified FIRST in the validator so any byte flip
  fails before interpretation.  Verify is the sole producer (both its
  publication sites embed run_meta); check is REPORT-ONLY and no longer
  writes HANDOFF_PATH.
- run_promote is validation/install only: full candidate validation +
  exhaustiveness + zero-hard-gates before ANY tree work; then current
  snapshot by source-hashing alone; exact identity required; staged
  install from the candidate with snapshot and metadata VERBATIM (the
  metadata-provenance trap is pinned: installed metadata.json equals the
  candidate's run_meta even for a delayed promote).  Zero compiles, no
  scratch, no retained actual, no fallback; `jobs` is gone from its
  signature and an explicit -j/--jobs with --promote is rejected (exit 2,
  pinned both spellings).
- _emit_run gained a verbatim-metadata parameter (audit tool);
  justfile/gate-doc/tool-docstring/history all state the ONE-full-compile
  lifecycle; the promote recipe lost -j.
- Ledger applied exactly: three deletions (with
  test_check_is_report_only_never_writes_candidate replacing the check
  export pin), schema-2 helper + reseal-after-mutation matrix with new
  wrong-kind/snapshot-disagree/bad-run_meta/digest-corruption cases,
  zero-compile assertions across the promote family, hard-gate candidate
  → exit 2 candidate-level rejection, and my prior file's inversions
  (one-full-run total, bootstrap, staleness-after-verify, metadata
  provenance, rejected jobs).
- One implementation hiccup for the record: an over-wide edit splice
  briefly deleted _expectation_from_baseline/_expectation_consistency
  (still used by verify); the suite caught it immediately and both were
  restored from HEAD verbatim.

Deliberately left to Slawomir: the real production smoke (matrix case
10 — verify then a seconds-fast promote on the unchanged tree).  Ready
for review.
