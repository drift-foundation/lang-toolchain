# Certified ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` (the
924-fixture ownership-audit corpus certification gate).

## Provenance

| field | value |
|---|---|
| origin run | the Phase D final acceptance run (`build/tmp/phase-d-final`), the corpus result certified with toolchain 0.33.87 |
| toolchain | driftc **0.33.87**, runtime **ABI 21** |
| source commit | `3d48b7f0` (tests/records: Phase D closure) |
| corpus tool | `tools/drift_corpus_audit.py` v1.6.0 |
| run date | 2026-07-22T20:12:28Z (started_unix 1784751148) |
| universe | 924 compiled / 1268 discovered (344 compile-failed partition, 49 rule-excluded), 14 counters, all hard gates 0 |

Checked-in artifacts: `aggregate.json` (counters), `manifest.json`
(universe identity: verbatim inclusion rule, per-fixture
whole-directory content hashes, compile partition), and
`metadata.json` (the origin run's volatile context — jobs, duration,
python — kept as provenance RECORD only).  Only aggregate + manifest
participate in comparison.

## Generation command

Produced on the certified 0.33.87/ABI-21 tree (commit `3d48b7f0`) —
NEVER from an uncertified candidate — via:

```
.venv/bin/python tools/drift_corpus_audit.py \
    --out build/tmp/phase-d-final -j16
```

then copying `aggregate.json`, `manifest.json`, `metadata.json` here.

## Update policy

This baseline changes ONLY through an explicit, REVIEWED commit that
intentionally accepts a new corpus state (universe change from fixture
edits, or a reviewed counter change).  Certification NEVER regenerates
or re-blesses it automatically — a broken candidate must not be able
to approve itself; baseline drift must be visible in the diff.
Refresh by copying `aggregate.json` + `manifest.json` +
`metadata.json` from the accepted run's `--out` directory and updating
the provenance table AND generation command above — version, ABI,
commit, tool version, date — in the same commit.  Never point the
recipe at an ephemeral `build/tmp/...` directory.

## Relationship to the ownership MATRIX

Two DISTINCT certification gates share the "ownership" name:

* **`just ownership-matrix-check`** — the 51 curated, GENERATED
  ownership-transfer matrix fixtures (`__ownership_matrix__/_gen.py`
  freshness guard).  Runs inside `just test` (so twice under
  run-all-tests.sh, once per sanitizer mode).
* **`just ownership-corpus-check`** — THIS gate: the full 924-fixture
  compile-audit corpus with exact-equality counter comparison against
  this baseline.  Runs EXACTLY ONCE per entrypoint — from the
  independent `just certify` workflow, and as the first step of the
  maintainer's private pre-handoff runner (run-all-tests.sh) — and NEVER
  from `just test` (which run-all-tests.sh executes twice).  `certify`
  never invokes run-all-tests.sh.
