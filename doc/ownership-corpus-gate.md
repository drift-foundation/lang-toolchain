# The ownership-corpus process

The ownership corpus compiles a large fixture universe and audits the
ownership-authoring decisions the compiler makes on each one, summed into a
small set of counters. A checked-in **reviewed (golden) baseline** records the
accepted universe and counters. The lifecycle is:

```
committed golden baseline
  → fresh verified observation + promotion candidate  (ownership-corpus-verify)
  → fast validation + install, ZERO compiles          (ownership-corpus-promote)
  → committed golden baseline
```

`ownership-corpus-check` remains the quick incremental/projected iteration
lane (report-only: it never mints the candidate).  The complete
discovery-to-install workflow performs **exactly one full-universe
compile** — verify's — and promote is fast-or-fail: it validates and
installs the verified candidate or fails immediately and requires a new
verify.

CI reads the committed golden state only — it never approves or installs a
candidate (the verify gate does publish the reviewable candidate under
build/tmp as a side effect of every valid observation; installing it is
always a separate human-invoked promote).  There are three public recipes,
all driven by `tools/drift_corpus_check.py`.  The compiling lanes (check,
verify) build into a fresh temporary directory per invocation and require
the toolchain+universe fingerprint captured at the start to equal the one
re-captured at the finish; promote compiles nothing and probes the current
identity passively.

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
- REPORT-ONLY: after stable start/end tree identity it writes its work-dir
  `report.json` and prints the projected deltas.  **It never mints the
  canonical promotion candidate and never changes the committed baseline** —
  fresh promotable evidence comes from `ownership-corpus-verify` alone.

A full fresh run is `ownership-corpus-verify`'s job — the single fresh
authority.  (The old `--fresh` developer flag is retired.)

## `just ownership-corpus-verify` — CI / cert gate AND candidate producer

The single corpus command used by `just certify`, and the single fresh
authority for promotion candidates.

- Ignores the developer cache and never CONSUMES the handoff — a pre-existing
  candidate (valid, malformed, stale, or delta-proposing) is invalidated at
  the start of the run, so no earlier candidate can masquerade as this run's
  output.
- One fresh full-universe compile; stable start/end fingerprint.
- Compares **exactly** to the committed reviewed baseline: inclusion rule,
  fixture names + source hashes, exclusions + reasons, compiled/failed buckets,
  every per-fixture projection, aggregate counters, and zero hard gates.
- Every COMPLETE, stable, zero-hard-gate observation atomically republishes
  the promotion candidate `build/tmp/ownership-corpus-projection.json` —
  exact matches included (exit 0; promotion simply unnecessary).  Hard-gate
  and aborted/invalid runs (exit 2) publish **nothing**.
- On drift, **fails loudly** (exit 1) and retains the fresh actual under
  `build/tmp/ownership-corpus-actual/` for diagnosis; the exported candidate
  is the SAME observation packaged for `promote`, which validates and
  installs it with zero compiles after review.  Verify **never** calls
  installation code or modifies any tracked baseline file — committed data
  stays byte-identical. A golden clean clone passes `just certify` with no
  `check` or `promote` first and with zero tracked diffs.
- With NO reviewed baseline at all (bootstrap), verify performs the same
  discovery run and emits the initial candidate (exit 1); a present but
  unreadable/malformed baseline still fails closed (exit 2, no candidate).
- `check`, `verify`, and `promote` serialize on one coarse advisory lock
  (`build/tmp/ownership-corpus.lock`).

## `just ownership-corpus-promote` — fast-or-fail install (zero compiles)

A deliberate maintainer action, **never** wired into CI / `just test` /
`just certify`.  It performs **no corpus compilation at all** and accepts
no worker count.

- Requires the canonical fresh-verify candidate (missing, malformed,
  corrupted-digest, wrong-kind, projected/non-exhaustive, hard-gate-bearing,
  or stale is an immediate error — never a baseline fallback, never a
  compile).  Never reads developer cache files.
- Validates the candidate's schema, digest seal, producer kind, exhaustive
  observation, and internal consistency, then recomputes the CURRENT
  toolchain/universe snapshot (hashing every fixture source — the staleness
  protection) and requires exact identity with the candidate.
- On agreement, stages/installs the candidate's exact observation with the
  existing post-install reload/equality/fingerprint proofs; the verify
  run's snapshot and measured metadata install VERBATIM (a delayed
  promotion never absorbs the review gap into `duration_s`).  A
  byte-identical baseline is a validated no-op.  Invoking promote is the
  explicit authorization to install the reviewed observation; the
  resulting Git diff is reviewed and committed, and that becomes the new
  golden baseline.

## Re-baselining

```
just ownership-corpus-verify        # THE full compile: gate + promotion candidate
just ownership-corpus-promote       # fast validation + install; zero compiles
git add lang/tests/ownership_corpus/reviewed-baseline && git commit
```

(`just ownership-corpus-check` remains available for quick incremental
iteration before the fresh verify; it is never required for re-baselining.)

## Bootstrap / migration

`projections.json` was landed once, mechanically, from the last approved
candidate's per-fixture counters (`tools/corpus_migrate_projections.py`) — a
format migration of already-approved evidence, proven against the live
`manifest.json`/`aggregate.json`, with no recompile. `fingerprint.json` is not
fabricated; the first genuine `ownership-corpus-promote` generates it. Runtime
tooling never depends on Git history.
