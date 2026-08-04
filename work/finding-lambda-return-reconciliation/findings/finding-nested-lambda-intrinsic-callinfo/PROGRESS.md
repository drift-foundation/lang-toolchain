# PROGRESS: finding-nested-lambda-intrinsic-callinfo

STATUS: FIX IN TREE — focused gates rolled into the parent battery.

## Log (2026-08-04, K)

- Revalidated the reviewer hypothesis on the implementation tree before
  changing anything: the repro fails exactly as documented
  (E_INTRINSIC_CALLINFO_MISSING_NODE); the parent detach at TypedFn
  construction takes the WHOLE `call_info_by_callsite_id` /
  `instantiations_by_callsite_id` maps after `_apply_fnptr_consts` removed
  the extracted subtree; the existing completeness walker deliberately
  skips lambda bodies and checks only source→CallInfo, so the surplus
  direction was unchecked.  No counter-evidence found; the recommended
  partition point (finalized TypedFn construction) fits.
- RED FIRST: lang/tests/driver/test_nested_lambda_callinfo_ownership.py
  installed before the fix — 3 red (stored-nested compile/run,
  parent+extracted coexistence, structural ownership pin) / 1 green
  (immediate-IIFE counter-boundary, green pre-fix as expected).
- FIX: `_owned_callsite_ids(body)` at TypedFn construction — a FULL
  post-rewrite `iter_hir_walk` with `default_should_descend` (descends
  into lambdas still present in the HIR) collecting
  HCall/HMethodCall/HInvoke callsite ids; the detached
  `call_info_by_callsite_id` AND `instantiations_by_callsite_id` are
  filtered to owned keys.  The LIVE maps stay unpruned (LambdaFnSpec
  snapshots alias them for pre-recheck terminal-call classification).
  `_validate_intrinsic_callinfo` untouched — still strict.
- POST-FIX: 4/4 in the new module (structural pin includes an
  extraction-actually-happened guard so it can never pass vacuously).
- Version: rides pending 0.35.0, no bump; ABI 22.  History: paragraph
  added to the pending 0.35.0 entry.
