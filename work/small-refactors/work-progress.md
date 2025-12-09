  ## Rough edges / potential bugs

  - Checker inference/validation does not seed locals with function parameter types. If you index a parameter xs: Array<String> in a
    condition, _validate_array_exprs/_validate_bool_conditions will treat it as unknown and may skip diagnostics or type checks.
  - Type inference logic is duplicated across _infer_hir_expr_type, _validate_array_exprs, and _validate_bool_conditions; they each
    maintain their own locals maps. This is brittle.
  - _validate_bool_conditions only learns about locals introduced by let inside the block; it ignores parameters and any outer scope, so
    boolean-condition checks can silently skip type errors on parameters.
  - Type table access still pokes at _uint_type/_string_type directly; a public API (ensure_uint, ensure_string) would be cleaner and
    less error-prone.
  - HIR→MIR and checker both implement .len/.cap typing rules separately; they’re in sync today, but there’s no shared helper, so drift
    risk is high.
  - Error reporting: duplicate-function rejection uses a thrown ValueError instead of a diagnostic; also span info is generally None in
    checker diags (known limitation, but worth noting).

  ## Proposed cleanup plan

  1. Seed locals from parameters in checker passes
      - Before _validate_array_exprs and _validate_bool_conditions, initialize locals with parameter TypeIds from the current function’s
        signature. This lets conditions and indexing on params be checked properly.
  2. Centralize expression typing helper
      - Factor out a single _infer_hir_expr_type-style helper that both array validation and bool-condition validation can use, seeded
        with the same locals. Avoid maintaining separate locals maps per pass.
  3. Add a small TypeTable API and use it
      - Replace direct _uint_type/_string_type access with ensure_uint()/ensure_string() (or add them if missing) in checker/type
        resolution. This removes the remaining “poke private attrs” hacks.
  4. Unify .len/.cap typing rule
      - Move the “Array/String len → Uint” rule into one shared helper (or document clearly) so HIR→MIR and checker can’t drift.
  5. Optional: change duplicate-function handling to a diagnostic
      - Instead of raising ValueError in the parser adapter, surface a structured diagnostic (with a span when available). Not a blocker,
        but improves UX.
  6. Add/adjust tests after cleanup
      - Add a checker test where a function param xs: Array<String> is indexed; expect correct type and diagnostics on misuse.
      - Add a checker test for an if condition using a param of the wrong type; ensure the Bool check fires.

### Additional spec alignment tasks
- DONE: Spec now says mixed-type array literals are rejected at compile/type-check time (not parse-time).
- DONE: Removed duplicate “Optional API (minimal)” subsection from the spec.
- TODO (tests): Parser adapter tests still use `drift_main`; update them to use `fn main()` for the canonical entry (optionally keep a
  legacy drift_main parsing test).
