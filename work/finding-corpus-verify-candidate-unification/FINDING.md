# Unify fresh corpus verification with promotion-candidate production

Status: plan under review; implementation handoff must wait until the currently running `ownership-corpus-promote` exits.

## Problem

The current re-baseline path permits an unacceptable third full-universe compile:

```text
ownership-corpus-verify          # full discovery compile; currently withholds handoff
ownership-corpus-check --fresh   # redundant full compile; emits handoff
ownership-corpus-promote         # third full compile; reproduces and installs
```

In active compiler development, running `verify` first is especially wasteful: a compiler-version/fingerprint change makes baseline identity drift expected, yet `verify` performs the complete fresh run, retains almost all candidate data under `build/tmp/ownership-corpus-actual`, and deliberately refuses to export the small promotion handoff. The developer must then repeat a full `check --fresh` solely to repackage substantially the same observations before `promote` repeats the fresh run again.

This happened on the pending 0.35.0 train: the first fresh verification supplied complete, independently reviewed evidence, but no `ownership-corpus-projection.json`; a second full `check --fresh` had to be started before promotion, making promotion the third full run.

The supported lifecycle must never require more than two full corpus runs. This is an acceptance constraint, not merely an optimization target:

1. discovery/verification compiles the universe once and, on valid drift, produces the reviewed promotion candidate;
2. explicit promotion independently compiles the universe once, reproduces the candidate, and installs it.

No intervening repackaging, fresh-check, or post-promotion full compile may be required.

## Desired contract

One cache-independent fresh command should serve both CI verification and developer candidate production. Every complete, stable, zero-hard-gate fresh observation emits reviewable promotion material as a side effect; the human may accept it for independent promotion or reject it:

```text
ownership-corpus-verify
  exact baseline match       -> exit 0, baseline untouched, reviewable candidate emitted (promotion unnecessary)
  valid zero-hard-gate drift -> exit 1, baseline untouched, atomically export a promotion-ready candidate
  invalid/incomplete run     -> exit 2, no promotable candidate
  nonzero hard gate          -> exit 1, retain diagnostic actual, no promotable candidate
```

`ownership-corpus-promote` remains an explicit human-authorized operation. It must independently perform another fresh full-universe compile, reproduce the reviewed candidate exactly, and only then install and post-validate the baseline. Verification producing a candidate must never auto-promote or mutate tracked baseline files.

The incremental `ownership-corpus-check` lane remains useful for quick projected/selective development feedback and is not removed. Only its redundant full-recompile surface `check --fresh` is removed under the pre-1.0 one-contract rule once `verify` is the single fresh-run authority. Bootstrap is folded into `verify`: an absent reviewed baseline is treated as maximal valid drift and produces a candidate; a present but unreadable or malformed baseline remains an exit-2 failure with no candidate.

## Acceptance criteria

- A single `ownership-corpus-verify` fresh run that finds valid drift emits everything required by `ownership-corpus-promote`; no intervening `check --fresh` is needed.
- Every complete, stable, zero-hard-gate verify run emits a reviewable candidate derived from that exact observation, including an exact-baseline match.
- The complete discovery-to-install workflow performs exactly two full-universe compiles, never three.
- CI still fails on drift and never installs a baseline.
- Only a complete, stable start==finish snapshot with zero hard gates is promotable.
- A failed/aborted/hard-gate run cannot leave a stale canonical candidate that appears to describe that run.
- Promotion rejects candidates from another toolchain, universe, source hash set, or run snapshot exactly as today.
- Promotion still performs its own full fresh compile and exact reproduction before staged install.
- Tracked baseline files remain byte-identical after every verify outcome.
- Documentation has one unambiguous lifecycle; it must not continue instructing compiler developers to run a redundant fresh check.
- Existing incremental `ownership-corpus-check` cache/select behavior remains intact; it is the quick iterative development lane.
- With no reviewed baseline, `verify` performs the discovery run and emits the initial candidate; malformed/corrupt baseline state fails closed.

## Scope and authority

Likely files:

- `tools/drift_corpus_check.py`
- `lang/tests/tools/test_drift_corpus_check.py`
- `lang/tests/tools/test_ownership_corpus_check.py` if its recipe/contract pins need migration
- `justfile`
- `doc/ownership-corpus-gate.md`
- module/help comments and the pending 0.35.0 history entry

This is a user-visible tooling workflow change, so it belongs in the already-pending `DRIFTC_VERSION=0.35.0` history. It does not change a compiler/runtime ABI boundary; ABI remains 22.

Editing existing tests still requires Slawomir's explicit approval under repository rules. The implementation handoff should surface that gate before modifying test files.

No language-spec change is involved.
