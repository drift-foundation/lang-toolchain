# The ownership-corpus process

The ownership corpus compiles a large fixture universe and audits the
ownership-authoring decisions the compiler makes on each one, summed into a
small set of counters. A checked-in **reviewed (golden) baseline** records the
accepted universe and counters. The lifecycle is:

```
committed golden baseline
  → local projected candidate   (ownership-corpus-check)
  → fresh validated candidate   (ownership-corpus-promote)
  → committed golden baseline
```

CI reads the committed golden state only — it never creates, approves, or
installs a candidate. There are three public recipes, all driven by
`tools/drift_corpus_check.py`. Every one compiles into a fresh temporary
directory per invocation and requires the toolchain+universe fingerprint
captured at the start to equal the one re-captured at the finish.

## The one authority for what a compile is

`tools/corpus_compile_contract.py` is the single source of truth for how the
corpus invokes `driftc`: argv, a minimal constructed child environment (explicit
inherit-allowlist + pinned hash/locale + a controlled per-run TMPDIR — nothing
ambient leaks in unfingerprinted, nothing writes under a shared /tmp),
`--sanitize`-aware runtime variant, and tool/linker/native-library selection.
Native-lib/linker/sanitizer selection come from `lang/driftc/link_selection.py`,
which **driftc itself also imports**, so what the corpus models can never drift
from what the compiler links. `tools/drift_corpus_fingerprint.py` hashes every
compile-result-relevant input into a **toolchain fingerprint**; with the static
universe digest it forms a **run snapshot**.

## `just ownership-corpus-check [<dir>]` — developer lane

Fast, resumable, always full-universe (default work dir
`build/tmp/ownership-corpus-work`).

- An empty cache seeds each fixture from the committed baseline's
  `projections.json` and failed bucket — **no compile for unchanged fixtures**.
- Only new, source-edited (hash changed), and `--select`ed fixtures recompile
  and become **current** observations; a compiler-fingerprint move keeps old
  observations as **projected** (stale) rather than forcing a full rebuild.
  Projected values are visibly marked and never described as freshly verified.
- Reused successes *and* failures are accounted (the `observed` / `projected`
  partitions cover both). It reports exact projected deltas against the golden
  baseline.
- After stable start/end tree identity, it atomically exports the local
  candidate to `build/tmp/ownership-corpus-projection.json`. **The handoff is not
  authority and never changes the committed baseline.**

`--fresh` forces a full recompile (ignore cache + seeds) for a broad change.

## `just ownership-corpus-verify` — CI / cert gate (the only one)

The single corpus command used by `run-all-tests.sh` and `just certify`.

- Ignores the developer cache **and** the handoff completely — even a valid,
  malformed, stale, or delta-proposing handoff.
- One fresh full-universe compile; stable start/end fingerprint.
- Compares **exactly** to the committed reviewed baseline: inclusion rule,
  fixture names + source hashes, exclusions + reasons, compiled/failed buckets,
  every per-fixture projection, aggregate counters, and zero hard gates.
- On any drift, **fails loudly** and retains the fresh actual under
  `build/tmp/ownership-corpus-actual/` for diagnosis. It **never** calls
  installation code or modifies any tracked baseline file — committed data stays
  byte-identical. A golden clean clone passes `run-all-tests.sh` with no `check`
  or `promote` first and with zero tracked diffs.

## `just ownership-corpus-promote` — manual re-baseline

A deliberate maintainer action, **never** wired into CI / `run-all-tests.sh` /
`just test` / `just certify`.

- Requires the canonical projection handoff (missing, malformed, or stale is an
  error — never a baseline fallback); its toolchain and run-snapshot composites
  must match the current tree. Never reads developer cache files.
- One fresh full-universe compile in isolated scratch; stable start/end
  fingerprint; must **exactly reproduce** the reviewed candidate (universe +
  hashes, exclusions, buckets, per-fixture projections, aggregate, zero hard
  gates).
- Unexpected fresh results fail and are retained separately; they never
  overwrite the handoff or become implicitly approved. Only exact agreement
  reaches staged baseline installation (a byte-preserving no-op when already
  identical), and the installed bundle is reloaded and proven equal to the fresh
  result. Invoking promote is the explicit authorization to install after fresh
  reproduction; the resulting Git diff is reviewed and committed, and that
  becomes the new golden baseline.

## Re-baselining

```
just ownership-corpus-check         # iterate; review report.json + projected deltas
just ownership-corpus-promote       # fresh full compile reproduces the candidate, installs
git add lang/tests/ownership_corpus/reviewed-baseline && git commit
```

## Bootstrap / migration

`projections.json` was landed once, mechanically, from the last approved
candidate's per-fixture counters (`tools/corpus_migrate_projections.py`) — a
format migration of already-approved evidence, proven against the live
`manifest.json`/`aggregate.json`, with no recompile. `fingerprint.json` is not
fabricated; the first genuine `ownership-corpus-promote` generates it. Runtime
tooling never depends on Git history.
