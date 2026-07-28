# Reviewed ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` — candidate driftc 0.33.89 / ABI 22: 925 compiled, 344 compile-failed, 49 rule-excluded.

## Provenance

| field | value |
|---|---|
| origin run | retained run dir `lang/tests/ownership_corpus/promotions/0.33.89-json-iterative-parser/candidate`; promoted from the RETAINED artifacts without a rerun |
| driftc / ABI | **0.33.89** / **ABI 22** |
| corpus tool | v1.7.1 |
| run started_unix | 1785213134.265577 |
| universe | 925 compiled / 1269 discovered (344 compile-failed, 49 rule-excluded) |
| promotion | drift_corpus_promote.py under approval `approval.json` (full sha256 2574212008016234367611ad210f907eccce89c5e8e3f957e6b5d79e5b782742); reviewer identity and date are recorded by Git history — the commit that renamed approval-DRAFT.json to approval.json and landed this promotion |

## Predecessor

The prior reviewed baseline (driftc 0.33.89 / ABI 22; origin run started_unix 1785200914.823527), preserved verbatim in this record's predecessor/ directory; earlier chain in the Git history of reviewed-baseline/BASELINE.md.

## Approved deltas and attribution

Counter deltas vs the predecessor (exact, per the approval):

* `c1_agree`: +38850
* `c1_path_dependent`: +9250
* `c3_moveout_flag_guarded`: -925
* `c3_moveout_owned`: +45325
* `c3_moveout_zero_safe`: -11100
* `events`: +54575
* `fns`: +5550
* `site_class:drop_before_overwrite_site4`: +4625
* `site_class:moveout_expansion`: +33300
* `site_class:overwrite_release`: +7400
* `site_class:scope_exit_release`: +9250

Machine attribution_facts in this approval, re-proven from the record's fixture-counters on every dry-run and apply: modal delta c1_agree +42, c1_path_dependent +10, c3_moveout_flag_guarded -1, c3_moveout_owned +49, c3_moveout_zero_safe -12, events +59, fns +6, site_class:drop_before_overwrite_site4 +5, site_class:moveout_expansion +36, site_class:overwrite_release +8, site_class:scope_exit_release +10 on all 925 shared fixtures; zero outliers; no new fixtures.  Residual zero on every counter; hard gates zero.

## Update policy

This baseline changes ONLY through `tools/drift_corpus_promote.py`
under a reviewed approval file — dry-run by default, `--apply`
required, artifact hashes pinned on both sides, exact universe and
counter-delta expectations enforced, hard gates zero, and a
post-write exact zero-delta comparison.  Certification NEVER
regenerates or re-blesses it; the promote tool is never invoked by
`just test`, `just certify`, or run-all-tests.sh.  Process
documentation: doc/ownership-corpus-gate.md.
