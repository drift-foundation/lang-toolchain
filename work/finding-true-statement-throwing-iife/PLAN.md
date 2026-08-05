# Plan: true statement-position throwing IIFE regression

Refresh date: 2026-08-05. Planning/research only until the active
`run_all_tests.sh` gate for the preceding finding completes cleanly.

## Expected scope

The current evidence predicts a test-only change in
`lang/tests/driver/test_stmt_position_iife.py`. Do not edit compiler, spec,
stdlib, history, or version files if the work-only repro remains green.

## Implementation sequence

1. After the active full suite completes, re-read the whole finding and compile,
   link, and run `repro_true_statement_throwing_iife.drift`. Record the exact
   command and exit status in implementer-owned `PROGRESS.md`.
2. If the repro exits 0, retain the test-debt classification and make only the
   approved existing-test edits below.
3. Rename
   `test_throwing_iife_statement_in_try_runs` to
   `test_throwing_iife_value_in_try_runs` (or an equivalently accurate name),
   and rewrite its comment to say that the IIFE is the value operand of `try`.
   Keep its source and runtime oracle unchanged.
4. Add a neighboring full compile/run test using the work-only repro shape:
   `fire()` contains `(|| -> Int => { throw MyExc(kind = 1); })();` as a true
   discarded statement, followed by `return 99`; `main()` catches `fire()` and
   returns `x - 7`. Assert compile success, assert `"No MIR lowering"` is absent
   from compiler stderr, and assert binary exit 0.
5. Do not add a structural HIR test or all-terminal variant unless the source
   shape proves ambiguous on the refreshed tree. They would mix parser or
   terminal-flow contracts into a focused lowering-route pin.
6. If the repro is red, stop the test-only path. Classify the result as a
   `LANGUAGE_BUG`, preserve the failing regression, report the exact failure,
   and diagnose the current producer/consumer path before changing compiler
   code. The 2026-08-05 refactor-trigger scan found no matching larger refactor.
7. Run the focused gates below and hand results back through Baton. Do not run
   `run_all_tests.sh`; Slawomir owns the final full-suite and certification
   gates unless he explicitly delegates them.

## Existing-test edit approval ledger (approved)

One existing file would change:

- `lang/tests/driver/test_stmt_position_iife.py`
  - rename the misleading value-position test;
  - correct only its route comment/name, retaining its source/assertions;
  - add the new true statement-position compile/run test in the same file.

No existing test is deleted or weakened. No other existing test file is
planned for modification. Any additional existing-test edit requires a fresh
approval update before it is made.

Slawomir explicitly approved this exact one-file ledger on 2026-08-05 after
the planning response confirmed no additional existing-test edits are needed.

## Focused gates

```sh
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_stmt_position_iife.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_try_expr_immediate_lambda.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_throws_terminal_body_flow_phase2.py
./.venv/bin/python3 -m pytest -q lang/tests/stage2/test_callinfo_cutover.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
```

The new positive is lowering-visible, so its full compile-and-run assertion is
mandatory; checker-only success is insufficient. If focused review is clear,
Slawomir runs the final `run_all_tests.sh` before certification.
