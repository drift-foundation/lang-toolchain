# Drift compiler robustness matrix

**Status:** triage walk only — no guardrails or tests added yet. This document is the artifact requested as the precondition for any robustness implementation work.

**Date of walk:** 2026-04-07
**Compiler at walk time:** 0.27.153
**Probe harness:** `work/robustness/probe.py` (throwaway scaffolding, not a test)
**Methodology:** Each row was generated as a parameterized minimal repro, fed to `driftc --dev`, classified by exit code + stderr inspection. Where the failure was a Python crash, the deepest stack frame was inspected to attribute a phase. Failure thresholds were narrowed by binary-ish search across depth values until the cliff was bracketed.

---

## Headline observations

1. **The dominant single failure mode is Python `RecursionError`.** Six of the twelve probed categories crash this way. This is exactly the prediction from the planning conversation and validates the hypothesis that **most categories collapse to "deep recursive descent in driftc"**.

2. **Recursion limits are hit across at least four phases**: parser (Lark + builders), stage1 (HIR builder), stage4 (SSA cycle detection), and at least one path through stage2 / codegen that I haven't fully attributed yet. This means a **single central depth check at the parser/HIR boundary is not sufficient**. At minimum we need:
   - A parser-level depth limit, *and*
   - An iterative replacement (or explicit stack) for the SSA `_has_backedge.dfs` walker, *and*
   - A pass over each AST/HIR/MIR walker that recurses on user-controlled depth.

3. **Two categories are pathological-scaling rather than recursion-limit.** `huge_struct` (5000 fields) and `long_function_body` (50000 statements) hang past 30s without crashing. These are not crashes, they are throughput problems — the right target is "scales acceptably or rejects with a deliberate limit" and the right guardrail (if any) is a soft cap with a clean diagnostic, not a recursion fix.

4. **One latent semantic-correctness issue surfaced as a side-effect of robustness probing**: `struct Node(child: Node, value: Int)` **compiles with no diagnostic** as long as nothing constructs the type. This is a self-referential value type with no indirection — the type has infinite size. Drift currently accepts the declaration. This is not a robustness issue per se but it is a real finding worth tracking separately. Same shape with mutually recursive structs.

5. **No segfaults, no SIGABRT, no memory corruption observed** in any of the 12 categories at any depth probed. The failure modes are uniformly Python exceptions or timeouts. This is good — it means we are not currently in "data corruption" territory; we're in "ungraceful crash" territory, which is much cheaper to fix.

6. **No hangs longer than 30s observed in true crash categories**, only in the two pathological-scaling categories where the compiler is making progress but slowly.

---

## Matrix

Columns:

- **category** — short name
- **phase** — phase that exhibits the failure (parser / stage1 / checker / stage2 / stage4 / codegen / runtime)
- **failure shape** — one of: clean diagnostic / python exception / timeout/hang / crash/segfault / pathological scaling / **ok**
- **current behavior** — concrete observation from the probe walk
- **target behavior** — what the desired outcome is (compile ok / compile error with stable diagnostic / runtime trap)
- **guardrail needed** — what implementation work is required to reach target
- **priority** — H/M/L based on (severity of current behavior) × (cost of fix); see priority key at end

| # | category | phase | failure shape | current behavior | target behavior | guardrail needed | priority |
|---|---|---|---|---|---|---|---|
| 1 | nested blocks `{ { { … } } }` | parser | ~~python exc: `RecursionError`~~ → **fixed (0.27.155, off-by-one corrected in 0.27.156)** | d≤256 compiles, d=257 yields `<source>:N:1: error: block nesting depth exceeds 256` | clean diagnostic with file:line span | **DONE.** `PARSER_MAX_NESTING_DEPTH = 256`, `_NESTING_DEPTH` counter in `_build_block`, `ParserNestingLimitError(ValueError)`, diagnostic dispatch hooked at 3 sites in `parser/__init__.py`. `parse_program` temporarily raises `sys.setrecursionlimit` so the in-builder counter has stack headroom (~4 frames per source level). Threshold check is `> PARSER_MAX_NESTING_DEPTH + 1` to account for the enclosing function-body block (corrected post-review in 0.27.156). Regression: `lang/tests/parser/test_parser_nesting_limit.py` (5 tests pinning d=100, d=255, d=256, d=257, d=500). | **DONE** |
| 2 | nested if/else | parser → stage1 → checker → driftc | ~~python exc: `RecursionError`~~ → **fixed (0.27.157, regression broadened in 0.27.158)** | d≤256 compiles cleanly, d=257 emits the row #1 diagnostic | clean diagnostic via row #1 block-counter | **DONE.** Original matrix entry said the failure was "parser only" — false. After row #1's block counter mitigated the parser path, end-to-end revalidation surfaced **six** sequential mutually-recursive `walk`/`walk_value` walker pairs across stage1, type_checker, and driftc.py, each shadowing the next. All six converted to iterative form (declaration-order preserved via reverse-push) — three in `lang/driftc/stage1/node_ids.py` (factored through new module-level helper `_iter_hir_walk`), two in `lang/driftc/driftc.py`, one in `lang/driftc/type_checker.py`. **Follow-up: factor the four duplicate copies in driftc.py / type_checker.py into a shared utility** (deferred — see row #15). Regression coverage: `lang/tests/stage1/test_node_ids_deep_recursion.py` (3 unit tests at d=3000 covering the three `node_ids` walkers under `sys.setrecursionlimit(1000)`) **plus** `lang/tests/driver/test_nested_if_deep_pipeline.py` (2 driver tests at d=256/d=257 exercising the full compiler pipeline so the type_checker and driftc.py iterative walkers are also pinned end-to-end). | **DONE** |
| 3 | nested parenthesized expressions `(((1)))` | parser | ~~python exc: `RecursionError`~~ → **fixed (0.27.159)** | d≤256 compiles, d=257 emits `<source>:N:M: error: expression nesting depth exceeds 256` | clean diagnostic with file:line span | **DONE.** Added `PARSER_MAX_EXPR_NESTING_DEPTH = 256` constant and `_EXPR_NESTING_DEPTH` global counter. `_build_postfix` (canonical entry per recursive expression descent level — 1:1 with source paren depth) increments/decrements the counter inside `try`/`finally` and raises `ParserNestingLimitError` with `> PARSER_MAX_EXPR_NESTING_DEPTH + 1` (the `+1` accounts for the leaf-level postfix call). Reuses the existing `ParserNestingLimitError` class and the diagnostic dispatch wired in row #1, so no new dispatch sites. Note: rows #1 and #2 already raised the headroom (rows #1 + #2 fixes mean nested parens now compile up to ~d=1000 before hitting the cliff at d=1500; the new counter intercepts cleanly at d=257 well before the cliff). Regression: `lang/tests/parser/test_parser_expr_nesting_limit.py` (5 boundary tests at d=100, d=255, d=256, d=257, d=1500). | **DONE** |
| 4 | long binary-add chain `1+1+1+…` | stage1 → stage2 | ~~python exc: `RecursionError`~~ → **fixed (0.27.160, helper-path coverage broadened in 0.27.161)** | d=2000 compiles cleanly through full pipeline (~44s wall-clock; scaling concern logged separately) | iterative lowering in stage1; stage2 left recursive but given more stack headroom | **DONE.** Two-part fix: (1) `stage1/ast_to_hir.py::_visit_expr_Binary` rewritten to iteratively unroll the left spine — collect `(op, right_ast, loc)` tuples, lower the leftmost leaf once, then rebuild the HIR chain from the inside out. The first iteration is unconditional (the entry expr is known to be a Binary by dispatch) to avoid an infinite-loop pitfall when the leftmost descent stops immediately. (2) `lang/driftc/driftc.py` defines `_with_compile_recursion_headroom` decorator (bump to 8192, restore on exit) applied to **all three** public compile entry points (`main`, `compile_stubbed_funcs`, `compile_to_llvm_ir_for_tests`) so library/helper consumers get the same headroom as the CLI path and the bump does not leak globally. Original 0.27.160 only patched `main`; 0.27.161 broadened it after review caught the gap. Right operands stay recursive (typically leaves; right-leaning chains would need a separate fix). Regression coverage (3 files): `lang/tests/stage1/test_long_binary_chain.py` (synthetic stage0 AST chain of 5000 under `setrecursionlimit(1000)`), `lang/tests/driver/test_long_add_chain_pipeline.py` (CLI path d=500 / d=2000), and `lang/tests/stage2/test_compile_stubbed_funcs_recursion_headroom.py` (library path: calls `compile_stubbed_funcs` directly with d=700 chain under `setrecursionlimit(1000)` and asserts the limit is restored on return). | **DONE** |
| 5 | else-if ladder (100s of arms) | parser converter → stage1 → HIR walker | ~~python exc: `RecursionError`~~ → **fixed (0.27.162 + 0.27.163 ordering/coverage; d=5000 contract strengthened to clean compile in 0.27.164 after the LLVM column overflow was resolved)** | d=1000 and d=5000 compile cleanly through full pipeline; d=8000 fails on other downstream concerns (clang-side scaling) but with no Python crash and no column overflow | iterative chain flattening at 2 sites with **forward-order lowering preserved**; recursion-limit bump for the third | **DONE.** Three-part fix: (1) `parser/__init__.py::_convert_if` iterative chain flattener. (2) `stage1/ast_to_hir.py::_visit_stmt_IfStmt` iterative chain flattener — **lowers each chain level in forward (outer-first) order** then constructs `H.HIf` nodes innermost-out as a pure post-step. (3) `_COMPILE_RECURSION_HEADROOM` raised from 8192 to 32768 for the still-recursive HIR rewrite walker. Regression coverage (3 files, 6 tests): parser-converter unit test, stage1 unit tests (depth + binding-id allocation order), driver tests at d=1000 (compiles), d=5000 (now compiles after the column-overflow fix in 0.27.164), d=8000 (no Python crash + no column overflow). | **DONE** |
| 6 | huge match (1000s of arms) | stage4 (SSA) | ~~python exc: `RecursionError`~~ → **fixed (0.27.154)** | ~~last ok d=850; first fail d=1000~~ → compiles d=2000 in 14.3s, runs correctly | iterative DFS at all four stage4 recursive walkers | **DONE.** Four sequential recursive walkers in stage4 needed conversion (each shadowed the next): `ssa.py::_has_backedge.dfs` (cycle detection), `dom.py::DominanceFrontierAnalysis._dfs` (post-order propagation), `ssa.py::rename_block` (dominator-tree pre/post-order with state restoration), `ssa.py::_compute_block_order.dfs` (RPO computation). All converted to iterative form with explicit work stacks. Regression: `lang/tests/stage4/test_has_backedge_deep_chain.py` (3 tests). | **DONE** |
| 7 | huge struct (1000s of fields) | unknown (compile completes <2000) | timeout/hang | last ok d=2000; first fail d=5000 (>30s) | scales `O(n)`, no hang; soft cap optional | profile to find the `O(n²)` step (likely type checker or codegen field iteration); decide whether to optimize or impose a soft cap | M |
| 8 | long function body (10000s of stmts) | unknown | timeout/hang | last ok d=20000; first fail d=50000 (>30s) | scales `O(n)`, no hang | same profiling exercise; this is normal-shape code (machine-generated handlers, big lookup tables) and should not need a limit | M |
| 9 | huge tuple/positional-arg call (1000 args) | n/a | **ok** | compiles cleanly through d=1000 | unchanged | none | — |
| 10 | many generic params on one fn | parser | **probe artifact — confirmed clean** | re-probed with the correct `id<type T1, T2, ...>(args)` syntax (the original probe omitted the `type` marker that distinguishes type args from comparison); compiles cleanly through d=500 | n/a | none needed | **NOT A BUG** — original probe used wrong syntax. Matrix entry was wrong about the failure, not about the limit. |
| 11 | nested generic args `Array<Array<…>>` | parser → trait world → TypeKey | ~~probe artifact~~ → **fixed (0.27.166, regression broadened in 0.27.167)**; pathological scaling at d≥2000 remains (Tier 3) | d=500 compiles cleanly through full pipeline; d=2000 reaches Tier 3 scaling (~600s wall-clock) but no Python crash | three iterative conversions in `parser/__init__.py::_type_expr_key`, `traits/world.py::type_key_from_typeid`, and TypeKey hash/eq | **DONE.** Three sequential recursion sites: (1) `parser/__init__.py::_type_expr_key` iterative post-order with `id(node)` cache. (2) `traits/world.py::type_key_from_typeid` iterative post-order with `tid` cache and new `_type_id_children` helper. (3) `TypeKey` frozen dataclass: `eq=False`, cached hash via `__post_init__`, iterative pair-stack `__eq__` with hash short-circuit. Regression coverage (4 files, 12 tests total — broadened in 0.27.167 after review caught two coverage gaps): `lang/tests/parser/test_type_expr_key_deep_nesting.py` (2 tests pinning site 1), `lang/tests/traits/test_type_key_deep_nesting.py` (8 tests pinning site 3 hash/eq **plus** site 2 `type_key_from_typeid` against a real `TypeTable` at d=5000 under `setrecursionlimit(1000)`), `lang/tests/driver/test_deep_nested_generic_pipeline.py` (2 driver tests: d=500 compiles cleanly, d=2000 reaches Tier 3 scaling with no Python crash — making the deep-depth contract committed regression coverage rather than narrative). Wall-clock at d=2000 remains pathological (~600s); that is Tier 3 scaling, not row #11 robustness. | **DONE** |
| 12 | very long identifier (10k+ chars) | parser | ~~clang failure~~ → **fixed (0.27.165)** | d≤256 compiles cleanly, d=257 emits `<source>:N:M: error: identifier length exceeds 256 (got N)` with file:line:col span | clean Drift-side diagnostic before clang sees it | **DONE.** Added `PARSER_MAX_IDENTIFIER_LENGTH = 256` constant and `ParserIdentifierLengthError(ValueError)` class. New `_validate_identifier_lengths(tree)` helper does an iterative one-pass walk over the parse tree (no recursion) and raises with the offending NAME token's span when its text exceeds the cap. `parse_program` calls the validator after Lark parsing returns and before AST building, so all NAME-derived identifiers are checked once. Diagnostic dispatch wired at all 3 sites in `parser/__init__.py`. Cap value: matrix originally suggested 1024 but the actual downstream cliff is at ~1023 source chars (clang's `multiple definition of local value named '__dbg_keepalive_xxxx...'` collision — the codegen wraps user identifiers with ~22 chars of prefix/suffix overhead). 256 is well below the cliff with 4× headroom and well above any realistic identifier length. Regression: `lang/tests/parser/test_parser_identifier_length_limit.py` (6 boundary tests pinning d=100, d=255, d=256, d=257, d=500, d=1000, plus a span-presence test). | **DONE** |
| 13 | direct self-referential value struct `struct Node(child: Node, …)` | type checker | ~~latent bug~~ → **fixed (0.27.168)** | clean diagnostic: `error: recursive value type: 'main::Node' is infinitely recursive through field 'child'; the field must contain at least one indirection (Arc, &, Array, RawPtr); suggestion: wrap the offending field in Arc<Node>` | kind-based cycle detector with Arc<...> primary suggestion | **DONE.** New `TypeChecker.validate_no_recursive_value_types` runs after monomorphization, builds a by-value edge graph from struct/variant instances, runs iterative Tarjan SCC, emits one diagnostic per offending type in each cycle. Indirection set is purely kind-based (REF, RAW_PTR, ARRAY, FUNCTION, INTERFACE) — Arc<T> is naturally accepted because it transparently contains a RawPtr<T> via its `buf` field. Suggestion preserves `Optional<...>` wrapper when present (`Optional<Node>` → `Optional<Arc<Node>>`). Also fixed an unrelated `has_drop` recursion crash on uninstantiable recursive variants by adding an in-progress cycle guard. Issue dir `issues/recursive-value-struct-accepted/` deleted. Regression: `lang/tests/driver/test_recursive_value_struct_diagnostic.py` (9 driver tests covering direct self-ref, mutual 2-cycle, 3-cycle, self-recursive variant, Optional<Self>, Arc-wrapped, Array-wrapped, plain non-recursive struct, mixed recursive/non-recursive in same module). |
| 14 | mutually recursive value structs `struct A(b: B); struct B(a: A)` | type checker | ~~latent bug~~ → **fixed (0.27.168)** | clean diagnostic naming both A and B in the cycle | same cycle detector covers both | **DONE.** Same fix as #13. |
| 15 | duplicated `walk`/`walk_value` HIR walker pattern | stage1, checker, driftc | ~~code-quality / DRY~~ → **fixed (0.27.170)** | shared `iter_hir_walk(root, should_descend=...)` helper in `stage1/node_ids.py`; all three local copies in `driftc.py` and `type_checker.py` replaced with calls to it | shared utility with parameterized `should_descend` predicate | **DONE.** Promoted `_iter_hir_walk` from a stage1-private helper to the public `iter_hir_walk(root, *, should_descend=default_should_descend)`. The default `default_should_descend` predicate matches the original stage1 behavior (HIR node or HIR-module dataclass). Replaced the three local copies with calls: (1) `lang/driftc/driftc.py::_collect_call_nodes_by_id` — uses default predicate. (2) `lang/driftc/driftc.py::_collect_hcast_node_ids` — uses default predicate. (3) `lang/driftc/type_checker.py::_collect_callsite_ids` — passes a custom predicate that wraps `default_should_descend` and skips `H.HLambda` so the call collector does not cross closure boundaries. Net deletion: ~120 lines of duplicated walker boilerplate replaced with ~30 lines of shared helper + 3 small call sites. Backwards-compatibility alias `_iter_hir_walk = iter_hir_walk` retained. Existing 6 stage1 unit tests + the row #2/#5 driver pipeline tests cover the refactor. |

---

## Categories not yet probed

These were on the original list but were not exercised in this first walk. Listed for completeness; should be added to a follow-up walk before the matrix is treated as exhaustive.

| category | reason deferred |
|---|---|
| pathological monomorphization (`Box<Box<Box<…>>>` actually instantiated, recursive generic functions) | needs syntax research per #11 above |
| recursive type aliases | unclear whether Drift has type aliases at this layer |
| huge import graphs / many modules | requires multi-file fixture setup |
| unicode pathology in identifiers / strings | lexer-edge work, separate scope |
| compile-time integer overflow in const evaluation | needs `const` evaluation surface review |
| massive enum/variant with payload | partial coverage via #6 (huge_match), but no payload-bearing variant probe |
| runtime stack overflow in user-code recursion | runtime concern, deliberately separate per scope decision |

---

## Phase × failure-shape summary

| phase | rows | dominant failure shape |
|---|---|---|
| parser | #1, #2, #3, #11, #12 | RecursionError × 3, clean diagnostic × 2 |
| stage1 | #4, #5 | RecursionError × 2 |
| checker | (none observed) | — |
| stage2 | (none observed) | — |
| stage4 (SSA) | #6 | RecursionError × 1 |
| codegen | (none directly) | — |
| runtime | n/a | — |
| unknown / cross-cutting | #7, #8 | timeout × 2 |
| n/a (compiled successfully) | #9, #10 (probe-artifact), #13, #14 | — |

**This validates the planning hypothesis: most failures are recursion-driven, distributed across at least 3 phases, but concentrated in the parser and stage1.** A single central depth check at the parser/HIR boundary would cover #1–#5 (5 of 6 RecursionError categories). The SSA case (#6) needs a separate fix. The pathological-scaling cases (#7, #8) need profiling, not a recursion fix.

---

## Recommendations (prioritized)

### Tier 1 — high-value, low-cost (do first)

1. **Convert `lang/driftc/stage4/ssa.py:253` `_has_backedge.dfs` to iterative DFS.** Single function, ~10 lines of code, removes the entire #6 category (and any future deeply-CFG'd input). This is the cheapest fix with the largest blast radius reduction. **Cost: ~30 min including a regression test.**

2. **Add a parser-level nesting-depth counter** with a configurable limit (default 256) and a clean diagnostic shape. Apply to: `block`, `if_stmt`, `paren_expr`/`primary`. Covers categories #1, #2, #3 in one change. **Cost: ~2–4 hours including diagnostic shape, default tuning, and three regression tests.**

3. **Convert stage1's binary-op and else-chain walkers to iterative form.** Specifically the `Binary.left/right` recursion (covers #4) and the `else_block: [if]` recursion (covers #5). These are stage1 lowering paths and are well-isolated. **Cost: ~2–3 hours including regression tests for #4 and #5.**

### Tier 2 — semantic correctness, surfaced by walk

4. **Detect infinitely recursive value structs** (#13, #14). This is the latent bug the walk turned up. The fix is a type-checker post-pass that builds a directed graph of struct → field-type-cycles and rejects cycles whose edges are all by-value (no `Box`/`&`/`Arc`/`Array` indirection). This is a semantic correctness fix, not a robustness one, but the walk found it so it goes here. **Cost: 1–2 days including diagnostic, error message, and several test cases (direct self-ref, mutual, three-cycle, ok-with-Arc, ok-with-Array).** Requires a separate spec decision: do we allow `Box<Self>` indirection, and if so what is the syntax?

### Tier 3 — pathological scaling (defer until profiled)

5. **Profile the `huge_struct` and `long_function_body` cliffs (#7, #8)** to find the `O(n²)` (or worse) step. Do not add a hard limit here yet — these are normal shapes for machine-generated code (big config records, generated dispatch tables) and capping them would push downstream users into workarounds. The fix is *probably* in the type checker's per-field walks or a stage1/2 list-vs-set choice; needs profiling to confirm. **Cost: 1 day to profile, unknown to fix.**

### Tier 4 — small / pre-emptive

6. **Identifier-length cap in the lexer** (#12) — preempt clang-side downstream failures with a clean diagnostic. Default 1024 chars. **Cost: ~30 min.**

7. **Re-probe categories #10, #11** with correct syntax for explicit type args / nested generics — current matrix entries are probe artifacts, not real findings. **Cost: 30 min.**

### Out of scope for this matrix (deliberate)

- Runtime stack overflow in user-code recursion. Per scope decision, runtime stack management is a separate concern from compile-time robustness.
- Sanitizer-mode robustness re-runs. After Tier 1 lands, the *new* Tier 1 tests should also run under `DRIFT_UBSAN=1` and `DRIFT_ASAN=1` to make sure the guardrails themselves don't introduce new UB or memory issues.

---

## Robustness lane recommendation

When tests are eventually added (Tier 1 deliverables), they should live in their own pytest directory **and** be excluded from the standard `just test` lane:

- proposed location: `lang/tests/robustness/`
- proposed just recipe: `just test-robustness`
- CI: nightly run, with explicit timeout per test (most tests should complete in under 10s; the matrix's pathological-scaling probes can have a higher per-test timeout)
- the lane should run *both* in default mode and under `DRIFT_UBSAN=1` to validate that guardrail diagnostics are themselves UB-clean

The robustness tests must **not** be added to the default `just test` lane. They are deliberately slow by construction and would inflate every-PR wall clock by 10–20% for marginal day-to-day value. Their job is to catch regression in the guardrails, not to gate routine development.

---

## Priority key

- **H** = Tier 1 (do first; high blast-radius reduction, low implementation cost) or Tier 2 (latent correctness bug surfaced by walk)
- **M** = needs profiling or has unclear target
- **L** = pre-emptive or probe-artifact; reasonable to defer

---

## What this document is and is not

**Is:** the precondition artifact for any robustness implementation work. A snapshot of the compiler's current behavior at the limits, with a prioritized fix list.

**Is not:**
- a test plan (tests come after Tier 1 fixes land)
- a complete catalogue (see "Categories not yet probed")
- a commitment to fix every row (some rows will be deliberately deferred)
- a runtime-safety document (see scope decision)

**Next concrete step:** discuss prioritization, then begin Tier 1 item (1) — the SSA iterative-DFS conversion — as the smallest standalone unit of work with the largest single benefit.
