# Reviewed ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` — candidate driftc 0.33.91 / ABI 22: 933 compiled, 359 compile-failed, 49 rule-excluded.

## Provenance

| field | value |
|---|---|
| origin run | retained run dir `lang/tests/ownership_corpus/promotions/0.33.91-unswept-fixture-rescan/candidate`; promoted from the RETAINED artifacts without a rerun |
| driftc / ABI | **0.33.91** / **ABI 22** |
| corpus tool | v1.7.1 |
| run started_unix | 1785423523.237132 |
| universe | 933 compiled / 1292 discovered (359 compile-failed, 49 rule-excluded) |
| promotion | drift_corpus_promote.py under approval `approval.json` (full sha256 02805bbe50f36f28bfa5e7c2c12d4004b1651ff99e3496e22bcb8b6cdcf3d8f6); reviewer identity and date are recorded by Git history — the commit that renamed approval-DRAFT.json to approval.json and landed this promotion |

## Predecessor

The prior reviewed baseline (driftc 0.33.91 / ABI 22; origin run started_unix 1785398027.0169559), preserved verbatim in this record's predecessor/ directory; earlier chain in the Git history of reviewed-baseline/BASELINE.md.

## Approved deltas and attribution

Counter deltas vs the predecessor (exact, per the approval):

* (none — zero-delta promotion)

Machine attribution_facts in this approval, re-proven from the record's fixture-counters on every dry-run and apply: modal delta no shared-fixture drift (933 fixtures unchanged); zero outliers; no new fixtures.  Residual zero on every counter; hard gates zero.

## Update policy

This baseline changes ONLY through `tools/drift_corpus_promote.py`
under a reviewed approval file — dry-run by default, `--apply`
required, artifact hashes pinned on both sides, exact universe and
counter-delta expectations enforced, hard gates zero, and a
post-write exact zero-delta comparison.  Certification NEVER
regenerates or re-blesses it; the promote tool is never invoked by
`just test`, `just certify`, or run-all-tests.sh.  Process
documentation: doc/ownership-corpus-gate.md.
