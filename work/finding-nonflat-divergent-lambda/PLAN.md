# PLAN: finding-nonflat-divergent-lambda

Regression-first (AGENTS.md LANGUAGE_BUG order):

1. Red probe with all divergent shapes (if/else both-throw stored + capturing
   IIFE, statement-form match all-throw, nested bare-block throw) — expect
   rejection/abort on current tree.  → repro_nonflat_divergent_positive.drift
2. Lift the guard: semantic no-fallthrough analysis (`_block_diverges`) in
   `_lambda_body_result`, replacing the last-node syntax-class test.
3. Bisect what still fails at runtime → root-cause the lowering side.
4. Fix the root cause (turned out to be the `_lambda_can_throw` walker, NOT
   CFG finalization — see PROGRESS).
5. Parity-fix stage2's fallback copy of the walker.
6. Flip the in-tree pinned negative into positives covering every shape, plus
   a still-rejected fallthrough control.
7. Targeted suites green → hand to review.
