# Reviewed ownership-corpus baseline

The checked-in reference for `just ownership-corpus-check` — candidate driftc 0.33.94 / ABI 22: 942 compiled, 367 compile-failed, 49 rule-excluded.

## Provenance

| field | value |
|---|---|
| origin run | retained run dir `lang/tests/ownership_corpus/promotions/0.33.94-bare-temp-field-projection-uaf/candidate`; promoted from the RETAINED artifacts without a rerun |
| driftc / ABI | **0.33.94** / **ABI 22** |
| corpus tool | v1.7.1 |
| run started_unix | 1785620810.4314916 |
| universe | 942 compiled / 1309 discovered (367 compile-failed, 49 rule-excluded) |
| promotion | drift_corpus_promote.py under approval `approval.json` (full sha256 7701cadd7284622a1185b76ecabe8a87275c982178dd4fe85933687259d727fb); reviewer identity and date are recorded by Git history — the commit that renamed approval-DRAFT.json to approval.json and landed this promotion |

## Predecessor

The prior reviewed baseline (driftc 0.33.93 / ABI 22; origin run started_unix 1785528642.42006), preserved verbatim in this record's predecessor/ directory; earlier chain in the Git history of reviewed-baseline/BASELINE.md.

## Approved deltas and attribution

Counter deltas vs the predecessor (exact, per the approval):

* `c1_agree`: +8955
* `c1_path_dependent`: +288
* `c3_moveout_flag_guarded`: +36
* `c3_moveout_owned`: +18479
* `c3_moveout_unreachable_block`: +18
* `c3_moveout_zero_safe`: +27
* `events`: +28230
* `fns`: +11446
* `pre_post_verdict_drift`: +468
* `site_class:drop_before_overwrite_site4`: +45
* `site_class:materialized_lastuse_release`: +6340
* `site_class:moveout_expansion`: +18560
* `site_class:overwrite_release`: +2529
* `site_class:scope_exit_release`: +756

Machine attribution_facts in this approval, re-proven from the record's fixture-counters on every dry-run and apply: modal delta no shared-fixture drift (932 fixtures unchanged); 1 outlier fixture(s): borrow_chained_ref_projection_noncopy {'c3_moveout_owned': -8, 'events': -8, 'site_class:moveout_expansion': -8}; autoborrow_owned_rvalue_field_method_drops_once contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2053, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3136, 'fns': 1272, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 704, 'site_class:moveout_expansion': 2062, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_call_field contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2056, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3141, 'fns': 1272, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 706, 'site_class:moveout_expansion': 2065, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_ctor_field contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2054, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3137, 'fns': 1272, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 704, 'site_class:moveout_expansion': 2063, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_hoisted_control contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2056, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3141, 'fns': 1272, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 706, 'site_class:moveout_expansion': 2065, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_index_guard contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2055, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3138, 'fns': 1272, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 704, 'site_class:moveout_expansion': 2064, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_mixed contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2055, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3138, 'fns': 1272, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 704, 'site_class:moveout_expansion': 2064, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_nested contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2053, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3136, 'fns': 1272, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 704, 'site_class:moveout_expansion': 2062, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_static_literal_mask contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2053, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3134, 'fns': 1272, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 702, 'site_class:moveout_expansion': 2062, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_ternary_bitcopy_scalar_ok contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2052, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3133, 'fns': 1271, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 702, 'site_class:moveout_expansion': 2061, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; rvalue_field_proj_throwing_edge contributes {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2057, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3142, 'fns': 1274, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 706, 'site_class:moveout_expansion': 2066, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}; autoborrow_owned_rvalue_field_method_unchanged withdrew {'c1_agree': 995, 'c1_path_dependent': 32, 'c3_moveout_flag_guarded': 4, 'c3_moveout_owned': 2057, 'c3_moveout_unreachable_block': 2, 'c3_moveout_zero_safe': 3, 'events': 3138, 'fns': 1275, 'pre_post_verdict_drift': 52, 'site_class:drop_before_overwrite_site4': 5, 'site_class:materialized_lastuse_release': 702, 'site_class:moveout_expansion': 2066, 'site_class:overwrite_release': 281, 'site_class:scope_exit_release': 84}.  Residual zero on every counter; hard gates zero.

## Update policy

This baseline changes ONLY through `tools/drift_corpus_promote.py`
under a reviewed approval file — dry-run by default, `--apply`
required, artifact hashes pinned on both sides, exact universe and
counter-delta expectations enforced, hard gates zero, and a
post-write exact zero-delta comparison.  Certification NEVER
regenerates or re-blesses it; the promote tool is never invoked by
`just test`, `just certify`, or run-all-tests.sh.  Process
documentation: doc/ownership-corpus-gate.md.
