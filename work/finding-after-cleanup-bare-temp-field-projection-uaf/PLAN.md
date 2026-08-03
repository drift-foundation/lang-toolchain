# Plan: remove dangling bare-temp field-projection work references

This is a bounded spare-time cleanup for K.  Do not alter lowering behavior or
test expectations.

## 1. Replace the three MIR references

In `lang/driftc/stage2/hir_to_mir.py`, retain the useful local explanation of
single-owner materialization and address projection, but replace each removed
`work/...` pointer with a concise durable reference such as:

```text
see doc/history.md, 2026-07-31 field-projection UAF entry
```

Update “0.34.0 fix” to the accepted **0.34.1** contract.  Do not rewrite or
reflow the surrounding technical comments unnecessarily.

## 2. Replace the driver-test docstring reference

In `lang/tests/driver/test_rvalue_arg_temp_drop_ab.py`, replace the removed work
path with the same `doc/history.md` entry or simply identify the test as the
0.34.1 field-projection UAF ownership-parity coverage.

Keep the A/B route explanation and all assertions unchanged.

## 3. Replace the e2e fixture reference

In
`lang/tests/codegen/e2e/autoborrow_owned_rvalue_field_method_drops_once/main.drift`,
replace the removed work pointer with the 0.34.1 history reference.  Preserve
the explanation that the old implementation-shape fixture was superseded by a
semantic single-drop pin.

## 4. Verify reference hygiene

The required static gate is:

```bash
rg -n "work/bare-temp-field-projection-uaf|bare-temp-field-projection-uaf" . \
  --glob '!work/**' --glob '!.git/**'
```

Expected result: no matches.

Also run `git diff --check` and inspect the five comment-only edits.  Because no
executable code or test expectation changes, a broad compiler gate is not
required.  If desired, run only the directly named driver file and e2e fixture
as a smoke check.

## Completion criteria

- Zero durable references to the removed work path.
- All historical pointers resolve to `doc/history.md` or are self-contained.
- Withdrawn 0.34.0 is not described as the current accepted fix.
- No source semantics, tests, version, ABI, or release announcement changes.
