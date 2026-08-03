# BUG: value-block lambda return-type inference (checker/lowering disagreement)

**Severity:** certification-blocking LANGUAGE_BUG (surfaced in 0.34.1).
**Fix version:** 0.34.2, ABI 22 unchanged (do NOT reuse staged 0.34.1 identity `e211863c`).
**Branch:** reject-redundant-call-borrows.

## Symptom

A pre-existing, valid idiom regressed:

```drift
val x = try (|| => { val a = risky(1); a + 1 })() catch { 0 };
```

fails to compile with:

```
error: E-TRY-ARM-TYPE: try/catch arms must produce the same type
(the attempt produces Void, this catch arm produces Int)
```

The immediately-invoked value-block lambda genuinely returns `Int` (it compiled
and ran before 0.34.1). The `E-TRY-ARM-TYPE` check added in 0.34.1 (`46b67954`)
*exposed* an older disagreement — it did not create the defect.

## Root cause

For an **unannotated value-block lambda** whose body ends in a trailing
expression (an `HExprStmt`, no explicit `return`):

- **Stage1** marks the trailing `HExprStmt` as the lambda's value.
- **MIR lowering** (`stage2/hir_to_mir.py:5731`) correctly infers the return
  type from the block's LAST statement (`HExprStmt` → its expr; `HReturn` → its
  value).
- **The checker** (`type_checker.py`, lambda handling) searched only for
  explicit `HReturn` nodes and **defaulted the block lambda to `Void`**,
  ignoring the trailing value.

Result: the try-expression checker saw `attempt: Void` vs `catch: Int` and fired
a false `E-TRY-ARM-TYPE`. The direct-call CallInfo boundary was similarly
`Unknown`.

## Non-goals / decisions

- Do **not** weaken `E-TRY-ARM-TYPE`; fix the checker to agree with lowering.
- `statement_form` (statement-form match: arms diverge via `return`, no value)
  must be honored — such tails are NOT value-typed (would raise a spurious
  `E-MATCH-NO-VALUE`); their return type comes from the internal returns.
- No refactor trigger fires (the immediate-lambda registry entry concerns
  ownership/drop defects, not return-type inference — confirmed).
- Repurposing the stale `test_lambda_trailing_match_value.py` Void-inference
  negative is REQUIRED (it encodes a contradictory contract), not masking.
