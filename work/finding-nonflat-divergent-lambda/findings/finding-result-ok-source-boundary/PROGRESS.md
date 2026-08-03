# PROGRESS: finding-result-ok-source-boundary

STATUS: RESOLVED (parent revision 9, per the review-2026-08-03T21-17-18Z
ruling — hypothesis 1, spec-aligned source separation).

- [x] Independent validation: local `val r = Ok(a)` ICE reproduced; ALSO
      found the spec-blessed annotated form broken (initializer FnResult vs
      Result) — strengthening the producer/consumer-disagreement diagnosis
- [x] Trigger re-scan at work start: no matching doc/refactor_triggers.md
      entry
- [x] Root cause fixed: `Ok(...) -> HResultOk` rewrite deleted; HResultOk
      deleted exhaustively; ctor-context diagnostic extended (non-variant
      expected types + std.core arms)
- [x] Acceptance criteria: repro rejects cleanly (E-CTOR-EXPECTED-TYPE, no
      traceback); public construction positive compiles AND runs (annotated
      local + throws->Result return with caller try/match); no double-wrap;
      ConstShare payload contract pinned structurally + compile/run
- [x] Pins: test_local_unannotated_ok_rejected_cleanly,
      test_return_ok_into_public_result_runs,
      test_named_fn_return_ok_wrapped_rejected (re-pinned),
      test_parse_unqualified_ok_lowers_to_plain_hcall,
      Phase-5 structural + run pair
- [x] Full lang-driver-test green (2340 passed / 10 skipped)
