# PROGRESS: finding-uninvoked-stored-lambda-lowering

STATUS: RESOLVED (parent revision 6, under the 2026-08-03 v1 ruling).

Final contract: CAPTURELESS stored lambdas (invoked or not) lower via
LambdaFnSpec/fnptr-const (unannotated params reject "cannot infer");
CAPTURING stored lambdas reject AT THE BINDING ("bare capturing lambdas
cannot be stored in v1...", borrowed variant for borrow captures) — no raw
HLambda reaches lowering (labeled contract assertion in HLet lowering).
Supported escape (core.callbackN) compiles AND runs with capture effects at
construction.  The compiler worklists reach a joint fixed point (hidden
specs, thunks, instantiations, late wrappers + late non-wrapper lowering).

Pins: lang/tests/driver/test_uninvoked_stored_lambda.py — 17 tests, all
green (see parent PROGRESS for the breakdown).

- [x] Red regressions (all shapes)
- [x] Trigger scan (no match)
- [x] Root-cause fixes
- [x] Pins green (17/17)
