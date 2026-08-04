# Proposed plan: `Uint` bitwise fixture at the `main -> Int` boundary

This plan is reviewer guidance, not an implementation specification.

1. Re-read this child finding and independently verify the spec clauses.
2. Confirm the current focused failure:
   `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py --summarize bitwise_uint_ops`.
3. Probe `return cast<Int>(x)` out of tree or in a temporary work repro and
   confirm it compiles/runs with exit 254.
4. Record the proposed one-line existing-fixture edit in implementer-owned
   `PROGRESS.md` and create an `APPROVAL-PENDING-*` token.
5. After explicit approval, make only that contract-preserving fixture edit.
6. Keep or add a focused negative boundary pin proving a non-literal `Uint`
   cannot be returned from an ordinary `-> Int` function without a cast, if
   existing return-mismatch coverage does not already make this unambiguous.
7. Run the focused e2e case and the return-boundary driver tests.

