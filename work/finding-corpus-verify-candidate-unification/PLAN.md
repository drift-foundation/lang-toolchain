# Research and proposed implementation plan

Timestamp: 2026-08-05T15:21:52Z

This plan is a reviewer hypothesis, not an authoritative implementation prescription. K should independently validate the race/staleness analysis and may propose a safer or simpler design that preserves the acceptance criteria.

## Current authorities

`tools/drift_corpus_check.py` already has nearly all required pieces:

- `run_verify` discovers the full universe, fingerprints the toolchain and universe, compiles every fixture in isolated scratch, validates stable fixture hashes and start==finish snapshot, computes per-fixture projections and aggregate counters, compares exactly to the reviewed baseline, checks hard gates, and retains a complete actual bundle on drift.
- `_export_handoff` serializes a promotion expectation with exact universe, compiled/failed buckets, per-fixture projections, aggregate counters, toolchain/run composites, and exhaustive observed/projected partitions.
- `_validate_handoff` and `_expectation_from_handoff` fail closed on malformed, stale, non-exhaustive, or wrong-fingerprint candidates.
- `run_promote` performs the independent full reproduction, zero-hard-gate check, staged install, and post-install reload/equality proof.

The missing bridge is policy only: `run_verify` calls `_retain_actual` but never `_export_handoff`.

## Recommended implementation shape

The hard workflow invariant is at most two full-universe compiles: discovery/verification plus promotion. A design that retains any normal or bootstrap path requiring `check --fresh` between them does not meet the finding.

1. Extract a small fresh-result publication authority rather than duplicating handoff construction in `run_verify`.

   Inputs should be the stable run snapshot, universe, fresh projections, failed bucket, counters, start time/jobs, and whether the result is promotable. For a full fresh observation:

   ```text
   observed = sorted(compiled_ok + failed)
   projected = []
   origin.work_dir = build/tmp/ownership-corpus-actual (or an explicit verify-origin label)
   ```

2. On every complete, stable run with zero hard gates:

   - retain/export the full observation evidence;
   - atomically export a handoff built from the exact same in-memory fresh result;
   - print the evidence/candidate paths and disposition guidance;
   - return 0 on an exact baseline match and 1 on valid drift so CI/cert remains red until a human promotes and commits the changed baseline.

3. On exact baseline match:

   - return 0 and leave tracked baseline bytes unchanged;
   - still publish reviewable candidate material for this exact observation; promotion is unnecessary but the run remains a uniformly disposable or reviewable artifact;
   - ensure an older canonical handoff cannot be mistaken for output from this verification. The fixed-path design invalidates the old handoff before the run and atomically replaces it with this run's result.

4. On infrastructure abort, unstable start/end snapshot, fixture hash mutation, malformed baseline, or nonzero hard gates:

   - do not emit a promotable handoff;
   - retain an actual for semantic/hard-gate drift where diagnostic evidence is complete, as today;
   - ensure no pre-existing canonical handoff survives in a way that falsely appears to belong to this failed run.

5. Preserve promotion's independent reproduction. Do not allow `promote` to install the retained actual directly and do not weaken its full compile, exact comparison, hard-gate, fingerprint, staged-write, or post-install checks.

6. Fold bootstrap into the same fresh authority:

   - absent reviewed baseline: run the complete fresh discovery, return 1, and emit a valid candidate;
   - present but unreadable/malformed/projection-less baseline: fail closed with exit 2 and no candidate;
   - do not retain `check --fresh` as a bootstrap-only third contract.

7. Update the public lifecycle:

   ```text
   incremental exploration: ownership-corpus-check [--select ...]
   fresh gate/candidate:     ownership-corpus-verify
   reviewed installation:   ownership-corpus-promote
   ```

   Delete `--fresh` from the developer lane, its help text, tests, and documentation rather than keeping two full-run contracts. Because Drift is pre-1.0, do not retain a compatibility alias.

## Stale-candidate and concurrency question

This deserves explicit due diligence. Today `HANDOFF_PATH` and `ACTUAL_DIR` are fixed global build paths, `_atomic_json` atomically replaces the handoff, and `_retain_actual` replaces the actual directory. If corpus commands can overlap, blindly unlinking or overwriting a fixed candidate can race another `check`/`verify`.

Two viable designs:

### A. Bounded fixed-path change (selected direction)

- Invalidate `HANDOFF_PATH` at the beginning of a fresh verify.
- Publish it atomically only after a stable, complete, zero-hard-gate drift.
- Continue using current strict fingerprint/universe validation in promote.
- Add one coarse advisory lock covering check, verify, and promote for their full duration. The lock turns mutual exclusion into an enforced contract and protects both the candidate and retained-actual fixed paths.

This is the selected working direction, subject to implementation due diligence finding a concrete incompatibility.

### B. Immutable run candidates (not selected)

- Publish a complete candidate under a unique run-ID directory, using staging plus atomic directory/pointer publication.
- Make `promote` consume an explicit candidate path or ID.
- A failed run publishes no candidate; older candidates remain clearly distinct rather than masquerading as current output.
- Validate the candidate's exact snapshot/universe against the current tree before compiling.

This is more robust for concurrency and auditability but expands CLI/schema/tests. Avoid it if fixed-path mutual exclusion is an intentional and adequately pinned contract. Do not introduce a half-measure such as “latest” selection that restores ambiguity.

## Test matrix

The implementation should at least pin these synthetic, fast cases:

1. `verify` exact match:
   - full universe compiled once;
   - exit 0;
   - baseline byte-identical;
   - candidate exists, validates, equals the baseline observation, and is attributable to this run rather than a stale predecessor.
2. `verify` valid zero-gate drift:
   - exit 1;
   - baseline byte-identical;
   - retained actual exists;
   - candidate exists and passes `_validate_handoff`;
   - all included fixtures are `observed`, none `projected`;
   - candidate projections/counters/universe equal retained actual/in-memory result.
3. `verify` drift then `promote`, without `check`:
   - verify compiles full universe once;
   - promote compiles it independently once;
   - exact candidate reproduction installs;
   - installed baseline reloads equal to the fresh result.
4. Hard-gate drift:
   - exit 1 and actual retained;
   - no promotable candidate.
5. Start/finish fingerprint change, fixture mutation, compile/projection infrastructure failure, and malformed baseline:
   - exit 2;
   - no new/promotable candidate;
   - baseline untouched.
6. Pre-existing candidate behavior:
   - verify never consumes it as its comparison authority;
   - successful verify atomically replaces it with the new observation;
   - failed verify cannot leave it ambiguously attributed to the failed run.
7. Stale/malformed candidate promotion remains rejected before compiling.
8. Promotion mismatch still retains actual and leaves baseline untouched.
9. CLI/recipe pins reflect the one fresh authority; if `--fresh` is deleted, it is rejected rather than silently accepted.
10. `certify` still invokes the fresh verification authority exactly once and never promotes.
11. Absent-baseline bootstrap:
   - full universe compiled once;
   - exit 1 with a valid candidate;
   - promotion performs the only second full compile and installs it;
   - a malformed-baseline control exits 2 candidate-free.
12. Whole workflow run-count pin:
   - discovery plus promotion invokes the fixture compiler exactly twice per fixture in total;
   - no recipe or documented path inserts a third full compile.

Existing tests likely needing migration include `test_verify_matches_baseline_no_handoff`, `test_verify_ignores_handoff`, `test_verify_drift_fails_zero_mutation`, the `--fresh` developer-lane pin, CLI help/argument pins, and recipe/documentation assertions. Do not edit them until Slawomir approves test changes.

## Review risks

- Publishing a handoff before checking hard gates would turn a known-invalid run into a promotion candidate.
- Leaving a previous handoff after an aborted or exact-match verify creates misleading authority even if `promote` eventually fails safely.
- Reusing `ACTUAL_DIR` directly as authority without the handoff validator would blur evidence and approval roles.
- Removing promotion's second full compile would collapse observation and authorization into one run and is out of scope.
- Keeping both `verify`-candidate and `check --fresh` as equivalent full-run contracts would violate the intended simplification unless they retain genuinely different documented semantics.
