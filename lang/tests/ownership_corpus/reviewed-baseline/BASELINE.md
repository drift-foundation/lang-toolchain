# Reviewed ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` (the
924-fixture ownership-audit corpus certification gate).

## Provenance

| field | value |
|---|---|
| origin run | maintainer run-all-tests.sh corpus stage, retained run dir `build/tmp/ownership-corpus-20260724-144528-1377082`; promoted from the RETAINED artifacts without a rerun |
| toolchain | driftc **0.33.88**, runtime **ABI 22** (reviewed mainline candidate) |
| source commit | `b2caeb44` (parser/stage1 statement-form match classification) |
| corpus tool | `tools/drift_corpus_audit.py` v1.7.1 |
| run date | 2026-07-24T20:45:28Z (started_unix 1784925928) |
| universe | 924 compiled / 1268 discovered (344 compile-failed partition, 49 rule-excluded), 14 counters, all hard gates 0 |

Checked-in artifacts: `aggregate.json` (counters), `manifest.json`
(universe identity: verbatim inclusion rule, per-fixture
whole-directory content hashes, compile partition), and
`metadata.json` (the origin run's volatile context — jobs, duration,
python — kept as provenance RECORD only).  Only aggregate + manifest
participate in comparison.

## Promotion record (2026-07-24)

Predecessor baseline: certified **0.33.87** / **ABI 21**, commit
`3d48b7f0`, tool v1.6.0, run 2026-07-22T20:12:28Z.  The maintainer
explicitly reviewed and approved promotion of EXACTLY this delta from
the predecessor — verified byte-exact against the retained run before
copying:

| counter | approved delta |
|---|---|
| `fns` | +24024 |
| `events` | +18480 |
| `c3_moveout_owned` | +18480 |
| `site_class:moveout_expansion` | +18480 |
| every other counter | +0 |

with the universe IDENTICAL to the predecessor (same 924/344/49
partition, same fixture hashes, same inclusion rule) and all hard
gates zero.  Attribution: the `std.ffi` module (new in the 0.33.88
candidate, absent from the 0.33.87 tree) contributes exactly 26 fns
and 20 `c3_moveout_owned`/`moveout_expansion` events per fixture
compile (26/20 x 924); residual after attribution is zero on every
counter.

## Generation command

The underlying run was produced by the standard recipe on the reviewed
0.33.88/ABI-22 tree (commit `b2caeb44`) — promotion copied the
retained artifacts; the corpus was NOT rerun for the promotion:

```
just ownership-corpus-check
# → tools/drift_corpus_audit.py --out build/tmp/ownership-corpus-<ts> -j16 \
#       --baseline lang/tests/ownership_corpus/reviewed-baseline \
#       --require-zero-delta
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
