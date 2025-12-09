## Rough edges / potential bugs

- DONE: Type inference now lives in a shared _TypingContext/_walk_hir; array/bool validators share a single locals map.
- Error reporting: duplicate-function rejection uses a thrown ValueError instead of a diagnostic; also span info is generally None in
  checker diags (known limitation, but worth noting).

  ## Proposed cleanup plan

1. Centralize expression typing helper (DONE)
    - _TypingContext wraps infer logic and feeds a shared _walk_hir for array/bool validation with a single locals map.
2. Change duplicate-function handling to a diagnostic (DONE)
    - Parser adapter now emits a diagnostic (with span when available) instead of raising on duplicate functions.
3. Add/adjust tests after cleanup (DONE)
    - Added checker tests for param-indexed arrays and param-based if conditions to ensure shared locals seeding is exercised.

### Additional spec alignment tasks
- DONE: Spec now says mixed-type array literals are rejected at compile/type-check time (not parse-time).
- DONE: Removed duplicate “Optional API (minimal)” subsection from the spec.
- DONE: Parser adapter tests now use `fn main()` (canonical entry); drift_main is treated as a normal fn only.
