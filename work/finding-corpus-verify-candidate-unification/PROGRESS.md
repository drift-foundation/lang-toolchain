# PROGRESS: corpus verify/candidate unification (implementer-owned)

Actor: K.  Implementation released 2026-08-05T16-14-09Z (post-0.35.0
corpus promotion).  Ruled design: two-full-runs ceiling; EVERY complete
stable zero-hard-gate fresh verify publishes the candidate (exact matches
included — Slawomir's correction); bootstrap folded into verify (absent →
candidate; malformed → exit 2); Design A + coarse advisory lock;
`check --fresh` retires.

## Phase 1 — LANDED (additive; every existing pin stays green)

tools/drift_corpus_check.py:
- `LOCK_PATH` + `_corpus_lock()` (flock; blocks with a note); `main()`
  wraps all three lanes.  Direct `run_*` calls (the test harness) bypass
  it by design.
- `run_verify`: begin-invalidate of HANDOFF_PATH (printed); bootstrap
  detection `_baseline_absent` (NEITHER manifest.json nor aggregate.json
  exists — any present core file still routes through the fail-closed
  reader); publication via new `_publish_fresh_candidate` on BOTH valid
  outcomes (match → exit 0 + candidate labeled "verify:exact-match";
  zero-gate drift/bootstrap → exit 1 + candidate labeled with ACTUAL_DIR);
  hard gates → retain actual, NO candidate; all exit-2 paths publish
  nothing (begin-invalidate already removed any stale one).
- Module docstring updated to the two-run lifecycle.

Docs: doc/ownership-corpus-gate.md (lifecycle diagram, verify section,
re-baseline recipe now verify→promote), justfile comment block (verify =
gate AND candidate producer; dropped the `--fresh` example).  History:
tooling paragraph folded into the pending 0.35.0 entry.

New pins: lang/tests/tools/test_corpus_verify_candidate.py (9 tests):
exact-match candidate attributed to this run; drift candidate == retained
actual; drift→promote two-full-runs ceiling (compile counts pinned);
hard-gate no-candidate; begin-invalidate on abort (valid-different +
malformed stale candidates); match replaces stale candidate;
absent-baseline bootstrap → candidate → promote installs;
malformed-baseline fail-closed control; lock exclusivity.

RED evidence: structural — HEAD's `run_verify` contains ZERO occurrences
of `_export_handoff`/invalidate/bootstrap/lock (verified via
`git show HEAD:tools/drift_corpus_check.py`); the finding itself
documents the withheld-handoff behavior.  (A full mirror-tree red run of
the new file was attempted but blocked on the conftest import chain;
structural evidence + the reviewer's own baseline description stand in.)

Verification: `pytest lang/tests/tools/` — **172 passed** (twice: after
source+tests, and again after doc/docstring edits).  All pre-existing
pins green untouched — phase 1 is fully additive.

## Phase 2 — APPROVED (test_edit_approval 2026-08-05T16-28-10Z) & DONE

- `--fresh` deleted end to end: argparse flag + help; the
  `--select/--fresh` guard (now `--select` only); `run_check`'s `fresh`
  parameter and force path (comment now points at verify as the single
  fresh authority); doc/ownership-corpus-gate.md paragraph replaced;
  history wording updated from "scheduled for removal" to REMOVED.
- Approved test edits applied:
  `test_fresh_forces_full_recompile_ignoring_cache` DELETED; file header
  lane description updated; rider rename
  `test_verify_matches_baseline_no_handoff` →
  `test_verify_match_is_exit_0_baseline_byte_identical` (+ contract
  comment); `test_verify_ignores_handoff` comment corrected to
  invalidate-and-republish semantics (assertions untouched).
- New pin: `test_retired_fresh_flag_is_rejected` (argparse SystemExit 2
  on all three lanes) — matrix case 9.
- Residual `--fresh` sweep: only the intentional narrative comment in
  the new test file and the doc's "retired" note remain.
- FINAL verification: `pytest lang/tests/tools/` = **172 passed**
  (171 after the deletion + the new rejection pin; suite run clean
  four times total across the phases).

## Historical ledger (as approved)

The `--fresh` deletion.  Exact ledger (deliberately tiny):
- DELETE test `test_fresh_forces_full_recompile_ignoring_cache`
  (lang/tests/tools/test_drift_corpus_check.py:183) — pins the removed
  lane via `run_check(..., fresh=True)`.
- Update that file's HEADER docstring lane description (comment-only).
- Source deletions riding approval: argparse `--fresh` flag + help, the
  `--select/--fresh` guard message wording, `run_check`'s `fresh`
  parameter and its code path, the `--fresh` paragraph in
  doc/ownership-corpus-gate.md (line ~52).
No other existing test requires changes: the three verify pins
(`test_verify_matches_baseline_no_handoff` — stale NAME only, assertions
green; `test_verify_ignores_handoff`; `test_verify_drift_fails_zero_mutation`)
all pass unmodified under the new contract.  Optional rename of the
first's misleading name can ride the same approval.

## Round 2 (review-2026-08-05T16-34-40Z) — both P1s fixed, checklist swept

- P1 begin-invalidation: the HANDOFF_PATH unlink is now the FIRST
  run_verify action — before fixture discovery, the empty-universe
  check, and the toolchain probe.  Unlink OSError intentionally falls
  through to the top-level infrastructure boundary (controlled exit 2;
  no dedicated diagnostic needed — evaluated per the review's question).
  PIN: test_early_failure_still_invalidates_stale_candidate (synthetic
  toolchain InfraError with a pre-seeded candidate → exit 2, candidate
  gone).
- P1 partial-baseline: absence is now defined against the COMPLETE
  recognized bundle (_BASELINE_ARTIFACTS: manifest, aggregate,
  projections, fingerprint, metadata, BASELINE.md, audit/) — any present
  artifact routes through the fail-closed reader.  No legitimate
  bootstrap flow pre-creates any of these (checked: bootstrap docs
  describe empty/no directory).  PIN:
  test_partial_baseline_is_damage_not_bootstrap parameterized over the
  four non-core artifacts → exit 2, no candidate.
- Checklist: rejection pin + doc paragraph + history wording were
  already done in phase 2; NEW this round: --verify argparse help no
  longer claims "read-only" without qualification (gate AND candidate
  producer; tracked baseline never written); stale-wording sweep over
  justfile (corpus block header + certify comment) and the tool's verify
  section banner.
- FINAL: pytest lang/tests/tools/ = **177 passed** (172 + 5 new P1
  pins).

## Round 3 (review 17-06-09Z) — file-as-baseline edge + whitespace

- `_baseline_absent` now fails closed on an EXISTING non-directory
  baseline path (it can never hold artifacts; the no-children probe
  would otherwise misread it as absence) — the ordinary reader then
  yields the unreadable/malformed exit 2.  PIN:
  test_baseline_path_that_is_a_file_fails_closed (regular file → exit
  2, no candidate).
- justfile:202 trailing whitespace removed; `git diff --check` clean.
- FINAL: pytest lang/tests/tools/ = **178 passed**.

## Round 4 (review 17-15-34Z) — lexical existence for dangling symlinks

- `_baseline_absent` uses os.path.lexists at BOTH levels (baseline path
  and per-artifact): a dangling baseline symlink or dangling recognized
  artifact symlink is damage, not absence.  A valid symlink TO a
  directory still works (is_dir follows).
- PINS: test_dangling_baseline_symlink_fails_closed +
  test_dangling_artifact_symlink_fails_closed (both exit 2, no
  candidate).
- FINAL: pytest lang/tests/tools/ = **180 passed**; git diff --check
  clean.
