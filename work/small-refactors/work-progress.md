## Rough edges / potential bugs

- DONE: Type inference now lives in a shared _TypingContext + _walk_hir; array/bool validators share the same locals/diagnostics.
- Error reporting: duplicate-function rejection uses a thrown ValueError instead of a diagnostic; also span info is generally None in
  checker diags (known limitation, but worth noting).

  ## Proposed cleanup plan

1. Centralize expression typing helper (DONE)
    - _TypingContext wraps infer logic and feeds a shared _walk_hir for array/bool validation with a single locals map.
2. Change duplicate-function handling to a diagnostic
    - Instead of raising ValueError in the parser adapter, surface a structured diagnostic (with a span when available).
3. Add/adjust tests after cleanup
    - Add a checker test where a function param xs: Array<String> is indexed; expect correct type and diagnostics on misuse.
    - Add a checker test for an if condition using a param of the wrong type; ensure the Bool check fires.

### Additional spec alignment tasks
- DONE: Spec now says mixed-type array literals are rejected at compile/type-check time (not parse-time).
- DONE: Removed duplicate “Optional API (minimal)” subsection from the spec.
- DONE: Parser adapter tests now use `fn main()` (canonical entry); drift_main is treated as a normal fn only.
