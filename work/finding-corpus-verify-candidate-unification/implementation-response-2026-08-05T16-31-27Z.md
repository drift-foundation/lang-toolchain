# Baton message

Timestamp: 2026-08-05T16-31-27Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: corpus_verify_candidate_unification
Phase 1 of the unification is LANDED and fully green — and it is fully
ADDITIVE: every pre-existing pin passes unmodified, so nothing awaits test
approval except the `--fresh` deletion itself.  Full detail in PROGRESS.md;
highlights:

- run_verify now begin-invalidates the candidate, publishes on BOTH valid
  outcomes per Slawomir's correction (exact match → exit 0 + candidate
  labeled verify:exact-match; zero-gate drift/bootstrap → exit 1 +
  candidate labeled with the retained ACTUAL_DIR), publishes NOTHING on
  hard gates or any exit-2 path, and folds bootstrap in (absent baseline —
  neither manifest.json nor aggregate.json — is the discovery run;
  any present core file still fails closed through the ordinary reader).
- One coarse advisory flock (build/tmp/ownership-corpus.lock) now
  serializes check/verify/promote at the main() choke point; the direct
  run_* entry points used by the test harness stay lock-free by design.
- Promote is untouched — independent reproduction/install exactly as
  before; the two-full-runs ceiling is pinned by compile-count assertions.
- New pins: lang/tests/tools/test_corpus_verify_candidate.py, 9 tests
  covering the matrix (candidate attribution on match, candidate==actual
  on drift, drift→promote two-run pin, hard-gate no-candidate,
  begin-invalidate on abort with valid-different AND malformed stale
  candidates, stale-candidate replacement on match, bootstrap→promote,
  malformed-baseline control, lock exclusivity).
- Docs/justfile/tool-docstring moved to the two-run lifecycle; history
  folded into the pending 0.35.0 entry (tooling paragraph; ABI untouched).
- RED evidence is structural: HEAD's run_verify has zero occurrences of
  _export_handoff/invalidate/bootstrap/lock (git show), matching the
  finding's own description; a mirror-tree red run was blocked by the
  conftest import chain, noted honestly in PROGRESS.
- Verification: pytest lang/tests/tools/ = 172 passed, run twice (after
  source+tests, and after doc edits).

PHASE-2 LEDGER for Slawomir's approval (deliberately tiny): delete
test_fresh_forces_full_recompile_ignoring_cache (the only test pinning the
retired lane, via run_check(fresh=True)); comment-only header update in
the same file; the source deletions ride it (argparse flag/help, guard
wording, run_check's fresh parameter+path, the doc's --fresh paragraph).
Optional same-approval rider: rename test_verify_matches_baseline_no_handoff
(its NAME is now stale; its assertions are green and untouched).  No other
existing test needs changes — the three verify pins pass unmodified under
the new contract.
