# Reviewed ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` — candidate driftc 0.33.91 / ABI 22: 933 compiled, 359 compile-failed, 49 rule-excluded.

## Provenance

| field | value |
|---|---|
| origin run | retained run dir `lang/tests/ownership_corpus/promotions/0.33.91-reject-redundant-call-borrows/candidate`; promoted from the RETAINED artifacts without a rerun |
| driftc / ABI | **0.33.91** / **ABI 22** |
| corpus tool | v1.7.1 |
| run started_unix | 1785398027.0169559 |
| universe | 933 compiled / 1292 discovered (359 compile-failed, 49 rule-excluded) |
| promotion | drift_corpus_promote.py under approval `approval.json` (full sha256 8fb336ae2b57e2466847bc9503167e88033993a6ead29c63bf180b641a9623b0); reviewer identity and date are recorded by Git history — the commit that renamed approval-DRAFT.json to approval.json and landed this promotion |

## Predecessor

The prior reviewed baseline (driftc 0.33.89 / ABI 22; origin run started_unix 1785213134.265577), preserved verbatim in this record's predecessor/ directory; earlier chain in the Git history of reviewed-baseline/BASELINE.md.

## Approved deltas and attribution

Counter deltas vs the predecessor (exact, per the approval):

* `c1_agree`: +7751
* `c1_path_dependent`: +256
* `c3_moveout_flag_guarded`: +32
* `c3_moveout_owned`: +15272
* `c3_moveout_unreachable_block`: +16
* `c3_moveout_zero_safe`: +24
* `events`: +23473
* `fns`: +9154
* `pre_post_verdict_drift`: +416
* `site_class:drop_before_overwrite_site4`: +40
* `site_class:materialized_lastuse_release`: +5344
* `site_class:moveout_expansion`: +15344
* `site_class:overwrite_release`: +2073
* `site_class:scope_exit_release`: +672

Machine attribution_facts in this approval, re-proven from the record's fixture-counters on every dry-run and apply: modal delta c3_moveout_owned -1, events -1, fns -1, site_class:moveout_expansion -1 on all 919 shared fixtures; 6 outlier fixture(s): autoborrow_method_receiver_through_ref_rvalue_chain {'c3_moveout_owned': 13, 'events': 13, 'fns': 1, 'site_class:moveout_expansion': 13}; borrow_chained_ref_projection_noncopy {'c3_moveout_owned': 45, 'events': 45, 'fns': 1, 'site_class:moveout_expansion': 45}; closures_share_capture_eval_order {'c3_moveout_owned': -1, 'events': -1, 'site_class:moveout_expansion': -1}; method_overload_param_type_concrete_beats_generic {'c1_agree': -6, 'c3_moveout_owned': -6, 'events': -7, 'fns': -1, 'site_class:moveout_expansion': -6, 'site_class:overwrite_release': -1}; method_overload_param_type_three_way {'c1_agree': -9, 'c3_moveout_owned': -10, 'events': -11, 'fns': -1, 'site_class:moveout_expansion': -10, 'site_class:overwrite_release': -1}; method_overload_param_type_two_way {'c1_agree': -7, 'c3_moveout_owned': -8, 'events': -9, 'fns': -1, 'site_class:moveout_expansion': -8, 'site_class:overwrite_release': -1}; autoborrow_bare_alias_param contributes {'c1_agree': 972, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2019, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3045, 'fns': 1259, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 668, 'site_class:moveout_expansion': 2028, 'site_class:overwrite_release': 260, 'site_class:scope_exit_release': 84}; autoborrow_bare_assoc_fn contributes {'c1_agree': 972, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2019, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3045, 'fns': 1259, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 668, 'site_class:moveout_expansion': 2028, 'site_class:overwrite_release': 260, 'site_class:scope_exit_release': 84}; autoborrow_bare_builtin_extend contributes {'c1_agree': 971, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2023, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3048, 'fns': 1258, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 668, 'site_class:moveout_expansion': 2032, 'site_class:overwrite_release': 259, 'site_class:scope_exit_release': 84}; autoborrow_bare_interface_arg contributes {'c1_agree': 972, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2019, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3045, 'fns': 1260, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 668, 'site_class:moveout_expansion': 2028, 'site_class:overwrite_release': 260, 'site_class:scope_exit_release': 84}; autoborrow_bare_lambda_iife contributes {'c1_agree': 971, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2018, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3043, 'fns': 1259, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 668, 'site_class:moveout_expansion': 2027, 'site_class:overwrite_release': 259, 'site_class:scope_exit_release': 84}; autoborrow_bare_mem_intrinsics contributes {'c1_agree': 971, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2019, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3044, 'fns': 1258, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 668, 'site_class:moveout_expansion': 2028, 'site_class:overwrite_release': 259, 'site_class:scope_exit_release': 84}; rvalue_arg_temp_drop_bare contributes {'c1_agree': 971, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2021, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3046, 'fns': 1262, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 668, 'site_class:moveout_expansion': 2030, 'site_class:overwrite_release': 259, 'site_class:scope_exit_release': 84}; trait_qualified_ref_type_arg_impl_lookup contributes {'c1_agree': 973, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2020, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3046, 'fns': 1259, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 668, 'site_class:moveout_expansion': 2029, 'site_class:overwrite_release': 260, 'site_class:scope_exit_release': 84}.  Residual zero on every counter; hard gates zero.

## Update policy

This baseline changes ONLY through `tools/drift_corpus_promote.py`
under a reviewed approval file — dry-run by default, `--apply`
required, artifact hashes pinned on both sides, exact universe and
counter-delta expectations enforced, hard gates zero, and a
post-write exact zero-delta comparison.  Certification NEVER
regenerates or re-blesses it; the promote tool is never invoked by
`just test`, `just certify`, or run-all-tests.sh.  Process
documentation: doc/ownership-corpus-gate.md.
