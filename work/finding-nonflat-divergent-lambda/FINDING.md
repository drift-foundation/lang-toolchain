# Finding: non-flat divergent lambda bodies are checker-rejected to mask a lowering bug

Date: 2026-08-03 (filed from work/review-k-r4-p1/FINDINGS.md item P1-1)

Classification: LANGUAGE_BUG (no-semantic-masking violation + lowering defect)

Suspected subsystem: hidden-lambda CFG finalization (`lang/driftc/driftc.py`
around the "hidden lambda block must end with a value or return" assertion),
plus the value-less-body guard in `type_checker.py::_lambda_body_result`.

## The defect

`_lambda_body_result`'s guard accepts only a literal trailing `HThrow`/
`HRethrow` as a non-Void lambda body with no value.  A semantically terminal
body such as

```drift
|| -> Int => { if cond { throw A(kind=1); } else { throw B(kind=2); } }
```

is rejected "must return a value" solely because `_last` is `HIf`, and
`test_stored_capturing_lambda_diagnostic.py` pinned that rejection.  The
in-code comment admits the reason is that codegen mislowers non-flat divergent
bodies — i.e. a checker rejection is masking a known lowering bug, violating
AGENTS.md no-semantic-masking and the user's ruling that pre-existing bugs
encountered in this slice get fixed.

## Required outcome

1. Regression FIRST (compile/run positives, currently failing):
   - if/else both-throw body (both branches exercised at runtime);
   - at least one nested terminal block or statement-form match whose arms all
     throw.
2. Fix hidden-lambda CFG finalization so terminal bodies lower correctly.
3. Replace the guard's syntax-class test (`isinstance(_last, HThrow)`) with
   semantic fallthrough/terminal analysis.
4. Flip the pinned negative in test_stored_capturing_lambda_diagnostic.py into
   a positive.

## Trigger scan

doc/refactor_triggers.md scanned 2026-08-03 for the R4 round: no registered
trigger covers lambda-body CFG finalization or divergent-body lowering.
Root-cause fix, no refactor escalation.
