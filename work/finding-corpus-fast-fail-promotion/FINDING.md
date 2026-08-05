# Ownership-corpus promotion must be fast-or-fail

Status: design/research while the legacy recompiling promotion is still running. Do not edit fingerprinted corpus-tool sources until that run exits.

## Human ruling

Normal `ownership-corpus-promote` performs **no corpus compilation at all**. It either validates and installs the exact complete observation emitted by `ownership-corpus-verify`, or fails immediately and requires a new verify run.

The accepted lifecycle is therefore one full corpus compile per baseline update:

```text
ownership-corpus-verify   # complete fresh observation + reviewable candidate
human review
ownership-corpus-promote  # fast validation/install only; zero compiles
```

`ownership-corpus-check` remains the quick incremental/projected development lane. It is not fresh evidence and must not be promotable by a no-recompile promotion path.

## Why the current promotion is unnecessarily slow

The current candidate records the run/toolchain composites, full fixture universe and source hashes, compiled/failed partitions, exhaustive fresh projections, counters, and observed/projected partitions. `promote` recomputes the current snapshot and rejects a stale candidate before compiling, but then recompiles all 1,338 fixtures solely to reproduce the already-complete verified observation.

When verify and promote see the same repository/toolchain/universe snapshot, that second compile adds only a mandatory nondeterminism/ambient-environment check. It adds no source-staleness protection. Reproducibility is useful as an explicit audit, but should not be hidden inside normal promotion or double the cost of every baseline update.

## Required contract

- Verify is the only producer of a promotable candidate.
- The candidate is a complete, atomically published, self-validating fresh observation. It carries everything required to render and validate the reviewed baseline, including the full run snapshot and the verify-run metadata—not merely snapshot composite strings.
- A promotable candidate proves every included fixture was freshly observed: `observed` is the exhaustive universe and `projected` is empty.
- Verify publishes no candidate on an invalid/aborted run or nonzero hard gate.
- Promote takes the shared corpus lock, loads and strictly validates the candidate, recomputes the current toolchain/universe snapshot, and requires exact identity with the candidate.
- Promote performs zero fixture compiler invocations, creates no compile scratch, and has no fallback. Missing, malformed, projected, hard-gate, wrong-kind, or stale candidates fail before baseline mutation and instruct the user to run verify.
- On a valid current candidate, promote stages/installs the candidate's exact observation with the existing post-install semantic and fingerprint validation. Verify-run metadata must not be recomputed using promotion wall time.
- An already-identical baseline is a validated no-op.
- Incremental check remains useful for iteration but cannot overwrite or impersonate the canonical verified candidate. Prefer removing its canonical handoff publication entirely and retaining its report/delta artifacts under its work directory.
- Independent full reproduction, if retained, is a separately named optional audit command. It is not invoked by promote, verify, test, certify, or the documented normal lifecycle.
- The tracked reviewed baseline remains the sole committed authority; candidate production and promotion never imply approval without the human-controlled promote/commit step.

## Compatibility/version boundary

This is a user-visible tooling-contract change folded into the pending `DRIFTC_VERSION=0.35.0` release. ABI remains 22. Under the pre-1.0 one-contract rule, remove the recompiling promotion path and projected-check handoff compatibility rather than retaining hidden fallbacks or dual promotion modes.

Existing test edits require Slawomir's explicit approval after the final migration ledger is reviewed. No language-spec change is involved.

## Completion evidence

- A real verify run produces a candidate; promote from the unchanged tree completes without invoking the fixture compiler and installs/refreshes the expected baseline identity.
- Any repository/toolchain/universe change after verify makes promote fail fast with the baseline byte-identical.
- Synthetic tests prove the total verify-to-promote fixture compile count is exactly one per fixture, not two.
- Documentation and diagnostics never claim promotion recompiles or suggest incremental check as a producer of promotable evidence.
