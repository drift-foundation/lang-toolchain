# Reviewed ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` (the
924-fixture ownership-audit corpus certification gate).

## Provenance

| field | value |
|---|---|
| origin run | corpus measurement run, retained run dir `build/tmp/ownership-corpus-20260725-070420-2045579`; promoted from the RETAINED artifacts without a rerun |
| toolchain | driftc **0.33.88**, runtime **ABI 22** (string-view-performance candidate — the commit accompanying this promotion) |
| corpus tool | `tools/drift_corpus_audit.py` v1.7.1 |
| run date | 2026-07-25T13:04:20Z (started_unix 1784984660) |
| universe | 924 compiled / 1268 discovered (344 compile-failed partition, 49 rule-excluded), 14 counters, all hard gates 0 |

Checked-in artifacts: `aggregate.json` (counters), `manifest.json`
(universe identity: verbatim inclusion rule, per-fixture
whole-directory content hashes, compile partition), and
`metadata.json` (the origin run's volatile context — jobs, duration,
python — kept as provenance RECORD only).  Only aggregate + manifest
participate in comparison.

## Promotion record (2026-07-25)

Predecessor baseline: the 2026-07-24 promotion (driftc 0.33.88 /
ABI 22 at commit `b2caeb44`, itself promoted over the certified
**0.33.87** / **ABI 21** baseline at commit `3d48b7f0`).  The
maintainer explicitly reviewed and approved promotion of EXACTLY this
run — verified byte-exact against the attribution report before
copying:

* UNIVERSE: partition IDENTICAL (924/344/49); hash delta = exactly
  the 4 fixtures intentionally migrated to the Result byte-access
  API (`iterator_op_id_mapping`,
  `pub_error_manual_diagnostic_redaction`,
  `pub_error_manual_diagnostic_string_field`,
  `string_byte_at_method`); no fixtures added or removed.
* COUNTERS (vs the predecessor): fns +32,340; events +20,356;
  c1_agree +18,485; c3_moveout_owned +19,431;
  site_class:moveout_expansion +19,431;
  site_class:overwrite_release +924;
  site_class:materialized_lastuse_release +1; every other counter +0;
  all hard gates zero.
* PER-FIXTURE ATTRIBUTION (residual zero): 923/924 fixtures carry
  the IDENTICAL modal delta {fns +35, events +22, c1_agree +20,
  c3_moveout_owned +21, moveout_expansion +21, overwrite_release +1}
  — the uniform stdlib contribution of the string-view-performance
  phase (std.text +24 fns, std.regex +6, std.json +3, std.parse +2,
  std.source +1, std.core −1 = exactly +35).  The single outlier is
  `string_byte_at_method` (its body became four Result matches):
  beyond-modal {events +28, moveout +27, c1_agree +5,
  materialized_lastuse_release +1}.  Per-fixture sums reconcile to
  the totals on EVERY counter.

## Generation command

The underlying run was produced by the standard recipe on the
reviewed string-view-performance tree — promotion copied the retained
artifacts; the corpus was NOT rerun for the promotion:

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
