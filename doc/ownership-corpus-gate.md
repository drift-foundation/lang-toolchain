# The ownership-corpus process

The ownership corpus compiles a large fixture universe and audits the
ownership-authoring decisions the compiler makes on each one, summed into a
small set of counters. A checked-in **reviewed baseline** records the accepted
universe and counters; promotion/certification fails if a fresh compile diverges
from the reviewed expectation.

The design deliberately separates **fast projections during development** from
**exhaustive fresh evidence at promotion**. There are two public recipes, both
driven by `tools/drift_corpus_check.py`.

## The one authority for what a compile is

`tools/corpus_compile_contract.py` is the single source of truth for how the
corpus invokes `driftc`: the argv, the minimal child environment (constructed
from an explicit inherit-allowlist plus pinned hash/locale and a **controlled
per-run TMPDIR** — no ambient compile-affecting variable leaks in
unfingerprinted, and no child writes under a shared /tmp), the runtime archive
variant (honoring `--sanitize`), and tool/linker/native-library selection.
Native-library, linker, and sanitizer selection come from
`lang/driftc/link_selection.py`, which **driftc itself also imports** — so what
the corpus models can never drift from what the compiler links.

`tools/drift_corpus_fingerprint.py` hashes every compile-result-relevant input
(compiler source, stdlib, the corpus tools + their schema versions, the prebuilt
runtime archive, native libraries, tool identities including executable bytes,
the argv template, and the exact child environment) into a **toolchain
fingerprint**; combined with the static universe digest it forms a **run
snapshot**.

## `just ownership-corpus-check [<dir>]` — developer lane

Fast, resumable, always full-universe. Default work dir
`build/tmp/ownership-corpus-work`, or an optional override.

Each fixture's **projection** (its ownership counters) is cached in a record
keyed on the fixture **content hash**. On each run:

- new fixtures, source-edited fixtures (hash changed), and `--select`ed fixtures
  are freshly compiled and become **current** observations;
- everything else is reused. A reused record observed under the *current*
  toolchain is a current observation; one observed under an *older* toolchain is
  carried forward as a **projected** (stale/inherited) value — a compiler-source
  change therefore does **not** trigger a full rebuild. Projected values are
  clearly distinguished and never described as freshly verified;
- when the cache is empty, per-fixture projections are seeded from the reviewed
  baseline's `projections.json` (fast clean clone); a one-time full run is only
  needed on the initial transition, before the baseline carries projections.

Reused successes *and* failures are accounted (the report's `observed` /
`projected` partitions both cover successes and failures). The completed
expectation — universe, buckets, per-fixture projections, aggregate, and the
observed/projected marks — is exported **atomically** to the cache-independent
handoff `build/tmp/ownership-corpus-projection.json`.

## `just ownership-corpus-promote` — fresh verification + install

Takes no directory and **never reads developer cache records**. It:

1. selects the **expectation** — the handoff if it exists (validated for basic
   internal consistency and that it describes the *current* tree/universe; a
   malformed or stale handoff is an **error**, never a silent baseline
   fallback), otherwise the checked-in reviewed baseline (clean clone / CI);
2. performs **one** fresh full-universe compile in isolated scratch;
3. requires a stable start==end fingerprint, **exact** agreement with the
   expectation (universe + source hashes, compiled/failed buckets, per-fixture
   projections, aggregate counters, exclusions + reasons), and zero hard gates;
4. on agreement, installs the reviewed baseline via staged writes (build +
   validate a sibling staging bundle, swap it in, then reload + validate) — a
   **byte-preserving no-op** when the fresh result already equals the baseline;
5. on disagreement, does **not** mutate the baseline: it retains the fresh
   *actual* report at `build/tmp/ownership-corpus-actual/` for diagnosis and
   reports the unexpected differences.

Invoking promote is approval of the *projected expectation*, verified
exhaustively — not approval of whatever the fresh compiler happens to produce.
Paying for the developer projection and then one independent fresh promotion
rebuild after a language change is intentional.

`just certify` and the maintainer's `run-all-tests.sh` both invoke
`just ownership-corpus-promote`. On a clean tree with no proposed changes the
fresh run compares directly against the checked-in baseline and performs no
source mutation.

## Worked example

- Baseline: fixture `x` fails compilation.
- A language fix intentionally makes `x` compile.
- `ownership-corpus-check` projects `x: failed → compiled_ok`; all untested
  fixtures are projected unchanged, and it exports the handoff.
- You review that result and run `ownership-corpus-promote`.
- The fresh full run must reproduce that exact flip. If an unrelated fixture `y`
  also flips, promotion stops and reports the unexpected difference; the baseline
  is untouched and the fresh actual is retained for diagnosis.

## Re-baselining

```
just ownership-corpus-check         # iterate (default work dir); review report.json
just ownership-corpus-promote       # fresh full compile reproduces the handoff, installs
git add lang/tests/ownership_corpus/reviewed-baseline && git commit
```
