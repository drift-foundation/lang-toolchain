# Reviewed ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` (the 925-fixture ownership-audit corpus certification gate) — the COMBINED 0.33.89/ABI-22 candidate: std.regex packed-workspace executor + String hot-path recovery.

## Provenance

| field | value |
|---|---|
| origin run | retained run dir `lang/tests/ownership_corpus/promotions/0.33.89-combined/candidate`; promoted from the RETAINED artifacts without a rerun |
| driftc / ABI | **0.33.89** / **ABI 22** |
| corpus tool | v1.7.1 |
| run started_unix | 1785083395.9722407 |
| universe | 925 compiled / 1269 discovered (344 compile-failed, 49 rule-excluded) |
| promotion | drift_corpus_promote.py under approval `approval.json` (sha256 d251720d2ff530685620cb53bf8f941f4fb3be691bd7026816b70f7aa54d0b4c), approved by sl@pushcoin.com on 2026-07-26 |

## Predecessor

The 2026-07-25 string-view-performance promotion (driftc 0.33.88 / ABI 22; run ownership-corpus-20260725-070420-2045579), itself over the 2026-07-24 promotion and the certified 0.33.87 baseline at 3d48b7f0.

## Approved deltas and attribution

Counter deltas vs the predecessor (exact, per the approval):

* `c1_agree`: +1100
* `c1_path_dependent`: +22
* `c3_moveout_flag_guarded`: +5
* `c3_moveout_owned`: -654
* `c3_moveout_unreachable_block`: +2
* `c3_moveout_zero_safe`: +15
* `events`: +372
* `fns`: +1254
* `pre_post_verdict_drift`: +52
* `site_class:materialized_lastuse_release`: +669
* `site_class:moveout_expansion`: -632
* `site_class:overwrite_release`: +261
* `site_class:scope_exit_release`: +74

Machine attribution_facts in this approval, re-proven from the record's fixture-counters on every run: ALL 924 shared fixtures carry the IDENTICAL modal delta {events -3, c3_moveout_owned -3, site_class:moveout_expansion -3} — the uniform stdlib contribution of the regex packed-workspace rewrite — with ZERO outliers; the remaining deltas are the new pin fixture std_regex_view_offsets_alternation's own contribution.  Residual zero on every counter; hard gates zero.  The String hot-path runtime recovery (launch-time trace cache, C-runtime-only) contributes ZERO ownership delta.

## Update policy

This baseline changes ONLY through `tools/drift_corpus_promote.py`
under a reviewed approval file — dry-run by default, `--apply`
required, artifact hashes pinned on both sides, exact universe and
counter-delta expectations enforced, hard gates zero, and a
post-write exact zero-delta comparison.  Certification NEVER
regenerates or re-blesses it; the promote tool is never invoked by
`just test`, `just certify`, or run-all-tests.sh.  Process
documentation: doc/ownership-corpus-gate.md.
