# Reviewed ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` — candidate driftc 0.33.93 / ABI 22: 933 compiled, 359 compile-failed, 49 rule-excluded.

## Provenance

| field | value |
|---|---|
| origin run | retained run dir `lang/tests/ownership_corpus/promotions/0.33.93-std-meta-cli-redesign/candidate`; promoted from the RETAINED artifacts without a rerun |
| driftc / ABI | **0.33.93** / **ABI 22** |
| corpus tool | v1.7.1 |
| run started_unix | 1785528642.42006 |
| universe | 933 compiled / 1292 discovered (359 compile-failed, 49 rule-excluded) |
| promotion | drift_corpus_promote.py under approval `approval.json` (full sha256 772c2193c89efad5aa2caf38d1b4fe4426b5df761a7de8f665ddb5b139442865); reviewer identity and date are recorded by Git history — the commit that renamed approval-DRAFT.json to approval.json and landed this promotion |

## Predecessor

The prior reviewed baseline (driftc 0.33.93 / ABI 22; origin run started_unix 1785516863.5298185), preserved verbatim in this record's predecessor/ directory; earlier chain in the Git history of reviewed-baseline/BASELINE.md.

## Approved deltas and attribution

Counter deltas vs the predecessor (exact, per the approval):

* `c1_agree`: -939
* `c3_moveout_owned`: -9331
* `events`: -10264
* `fns`: -2799
* `site_class:materialized_lastuse_release`: -933
* `site_class:moveout_expansion`: -9331

Machine attribution_facts in this approval, re-proven from the record's fixture-counters on every dry-run and apply: modal delta c1_agree -1, c3_moveout_owned -10, events -11, fns -3, site_class:materialized_lastuse_release -1, site_class:moveout_expansion -10 on all 932 shared fixtures; 1 outlier fixture(s): std_meta_build_info_unstamped {'c1_agree': -7, 'c3_moveout_owned': -11, 'events': -12, 'fns': -3, 'site_class:materialized_lastuse_release': -1, 'site_class:moveout_expansion': -11}; no new fixtures; no removed fixtures.  Residual zero on every counter; hard gates zero.

## Update policy

This baseline changes ONLY through `tools/drift_corpus_promote.py`
under a reviewed approval file — dry-run by default, `--apply`
required, artifact hashes pinned on both sides, exact universe and
counter-delta expectations enforced, hard gates zero, and a
post-write exact zero-delta comparison.  Certification NEVER
regenerates or re-blesses it; the promote tool is never invoked by
`just test`, `just certify`, or run-all-tests.sh.  Process
documentation: doc/ownership-corpus-gate.md.
