# regex-engine-allocation-removal

Remove allocation churn from the std.regex NFA executor
(`stdlib/std/regex/regex.drift`).  Prior evidence (string-view phase,
STRING-VIEW-PERFORMANCE-CHECKPOINT.md §11): the real engine measured
**171,325 µs** on the 2 MB `[a-z]+[0-9]+` carrier versus **2,519 µs**
for a DFA-shaped raw scan; switching the engine's byte reads to bulk
windows was worth only ~1.9%.  The dominant cost is NFA bookkeeping —
above all the per-byte/per-start allocations in
`_try_match_at_range` — not byte access.

## Phase contract (maintainer directive, 2026-07-26)

**Report-only checkpoint first; STOP for review before ANY
implementation.**  The checkpoint must:

1. Establish the exact current allocation model, separating: bitmap
   per anchored attempt; initial clist per attempt; seeds per
   consumed byte; replacement clist per consumed byte; the repetition
   of all of it across every candidate start in
   `_find_from_range`/`is_match`; compile-time NFA allocations
   (separate, excluded from the matching count).
2. Count-exact benchmark harness on the REAL engine with wrapped
   `drift_alloc_array`/`drift_free_array` and marker windows that
   exclude regex compilation, input construction, and reporting.
   Measured: allocations, frees, bytes processed, candidate starts,
   NFA steps.
3. Architecture comparison: reusable current/next lists + reusable
   bitmaps; generation/epoch visitation marks (no O(prog) clear);
   direct next-closure construction (deleting seeds); iterative
   epsilon-closure worklist vs the recursive `_add_state`.
4. Preferred shape: function-local `_NfaScratch` allocated once per
   top-level search, reused across every byte and candidate start.
   NO mutable scratch in `Regex` — matching stays thread-safe,
   reentrant, callable through shared `&Regex`.
5. Steady-state target: ZERO allocations per consumed byte; ZERO per
   candidate start; bounded per top-level search (workspace
   construction only — independent of input length and start count);
   no subject String retain/materialization for String OR view
   entry points.
6. If epoch marking is selected: pin generation-counter
   overflow/reset behavior (fail-safe or deterministic reset; never
   "unreachable").
7. One matcher authority; ALL semantics preserved: leftmost match,
   longest-at-that-start, greediness, alternation order, empty
   matches, zero-width assertions, `^`/`$` relative to view
   boundaries, negated ranges, UTF-8 byte offsets, String/view
   parity, fabricated-view bounds protection.
8. Benchmarks: 2 MB `[a-z]+[0-9]+` carrier; long no-match input
   (many candidate starts); wide-alternation/high-epsilon pattern;
   empty & zero-width matches; short parser/token inputs; String AND
   StringByteView forms; compile time separately.
9. Performance is a release gate: ≥2× median on the prior real-engine
   carrier; zero allocator growth with input length; no material
   regression on short/adversarial cases; exact medians +
   distributions.  If allocations are gone but speedup < 1.5×: STOP
   and explain the newly exposed bottleneck before expanding scope.
10. Measure optimized binary size + representative stdlib compile
    wall/CPU.  Public API and runtime boundaries unchanged; any
    boundary change triggers explicit compiler-version/ABI review.
11. Implementation (POST-review) lands as ONE consolidated chunk:
    engine rewrite, allocation teeth, semantic suite, perf
    protocol/tooling, docs, history, corpus attribution,
    reviewed-baseline promotion, full memcheck/ASAN/broad suite, one
    final 0.33.89 certification.  No separately certified sub-slices.
12. Corpus deltas from std.regex restructuring: measured, attributed
    to residual zero, reviewed, approved, promoted BEFORE the final
    runner.  Zero-delta gate stays strict.

LANGUAGE_BUGs: regression-first + doc/refactor_triggers.md.  Stop
only for real semantic uncertainty, boundary/ABI implication, failed
invariant, or a performance result that invalidates the design.

## Layout

- `Progress.md` — step log (updated same-turn per step).
- `REGEX-ENGINE-ALLOCATION-CHECKPOINT.md` — the report-only
  checkpoint (ends at STOP).
- `bench/` — measurement harness (bench-local; `work/` is ephemeral
  and never referenced from source/tests):
  - `ops.drift` — big-scan timing binary (tier_bench-style RESULT
    lines; includes the scratch-shape construction twins).
  - `counts.drift` + `driver.c` — count-exact wrap binary: classified
    counters (wrapper calls / REAL allocations via live-pointer set /
    sentinel + tombstone no-ops; releases split real vs null), marker
    windows by compile-twin subtraction.
  - `probe.drift` + `probe_driver.c` — allocator-call calibration +
    packed-vs-four-arrays workspace comparison.
  - `gen_small.py` → `generated/{ops_small.drift, counts_small.drift,
    driver_small.c, small_meta.json}` — the SMALL-SUBJECT
    representative suite (PRIMARY perf gate): 5 scenarios x 6 sizes
    (64 B–4 KiB) + view forms + compile+match rows; ns/search and
    searches/s reporting.
  - `gen_diff.py` + `legacy_regex.drift.snapshot` (32d676bb) →
    `generated/diff_main.drift` — dual-engine shadow differential:
    verbatim Lg-renamed legacy engine vs current std.regex over 1000
    seeded pattern/input cases (compile result+error tag/offset,
    find_first spans, is_match, view parity).  Self-validated at
    ZERO mismatches pre-rewrite.
  - `model.py` — faithful Python replica: candidate starts, NFA
    steps, bytes, and CLASSIFIED call predictions for both suites;
    reconciled residual-zero against the wrap counters.
  - `run_bench.py` — orchestrator: content-addressed build cache
    (source+stdlib+commit+flags key), provenance records, compile
    heartbeats, multi-launch same-launch medians, canonical
    quiet-baseline artifact (REGEX_BENCH_SET_BASELINE=1, max load
    < 1.0), loaded-run labeling, interleaved ABAB compare mode
    (REGEX_BENCH_COMPARE=base:candidate) for the final gate.

## Standing constraints

Tree state: rides the uncommitted 0.33.88 string-view-performance
delta (candidate staged as drift-0.33.88+abi22; certification rerun
pending).  This slice touches NOTHING outside `work/` until the
checkpoint review approves implementation.  User owns all git
mutations.  Full suites are user-run.
