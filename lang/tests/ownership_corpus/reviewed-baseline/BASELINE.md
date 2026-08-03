# Reviewed ownership-corpus baseline

The authoritative golden state that `just ownership-corpus-verify` (CI /
`just certify`) checks against, and the seed for a clean clone's
`just ownership-corpus-check`.

The exact universe and counters live in the machine files beside this note —
`manifest.json` (inclusion rule, per-fixture source hashes, exclusions, and the
compiled / failed partition), `aggregate.json` (the summed ownership counters),
`projections.json` (the per-fixture ownership projections — for fast clean-clone
seeding and exact per-fixture verification), `metadata.json`, and — after the
first genuine promotion under this workflow — `fingerprint.json` (the run
snapshot). Current shape: **942 compiled / 367 compile-failed / 49
rule-excluded**, 14 counter keys.

## Lifecycle

```
committed golden baseline
  → local projected candidate   (just ownership-corpus-check)
  → fresh validated candidate   (just ownership-corpus-promote)
  → committed golden baseline
```

- **`just ownership-corpus-verify`** — the only corpus command in CI / `certify`.
  Read-only: a fresh full compile compared exactly to this baseline; fails on any
  drift; never writes a baseline file. A golden clean clone passes with zero
  tracked diffs.
- **`just ownership-corpus-check`** — fast developer lane. Seeds an empty cache
  from `projections.json` (no compile for unchanged fixtures), recompiles only
  new / edited / `--select`ed fixtures, and exports a candidate handoff. Never
  changes this baseline.
- **`just ownership-corpus-promote`** — manual maintainer re-baseline. Requires
  the candidate handoff, recompiles the full universe from scratch, must
  reproduce the candidate exactly, then installs. Never wired into CI.

`projections.json` was mechanically migrated from the last approved candidate's
per-fixture counters (a format migration of already-approved evidence, proven
against `manifest.json`/`aggregate.json` — not a recompile).
`fingerprint.json` is not fabricated; the first real promotion generates it.

The Git commit that lands these files **is** the approval; reviewer identity and
date come from Git history.

## Distinct from the ownership matrix

The 51-fixture ownership **matrix** (`just ownership-matrix-check`, inside
`just test`) and this full-corpus **audit** are separate gates. Earlier
version-by-version provenance is in `doc/history.md`; the process is documented
in `doc/ownership-corpus-gate.md`.
