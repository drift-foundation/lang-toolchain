# driftc profile — picking the first speedup target

A local cProfile pass to identify which subsystem deserves the first
real optimization PR.  No code changes; pure measurement.

We can't run the bookkeeper workload locally, so the next-best
substitute is a full-pipeline compile of a substantive e2e fixture
that exercises most of the focus buckets
(`typecheck_funcs`, `post_check_analysis`, `hir_to_mir`,
`generic_instantiation`, `borrow_check` family, the inner
`compile_stubbed_funcs` phases).  Caveat: this workload does **not**
exercise `trust_pre_pass` or `trust_verify_loop` — those need a
multi-package consumer compile and should get a follow-up profile
pass.

## Workload

```bash
.venv/bin/python -m lang.driftc.driftc \
    lang/tests/codegen/e2e/std_regex_parser_corners/main.drift \
    --stdlib-root stdlib \
    --target-word-bits 64 \
    --entry main::main \
    -o /tmp/perf_profile/regex.bin \
    --timing
```

**Why this workload**:

- Largest single-file e2e fixture in the tree (`std_regex_parser_corners/main.drift`, 428 LOC) — non-trivial type-checking and HIR/MIR work without dragging in package machinery.
- Imports `std.regex` which pulls in real generic instantiation work + stdlib trait/impl resolution → exercises `generic_instantiation`, `checker`, `borrow_check`, full HIR→MIR + codegen + link.
- Reproducibly local; no setup beyond a clean checkout.
- Runs in **31.6s wall** without cProfile, **68.9s with cProfile** — long enough to profile meaningfully without burning a coffee break per iteration.

State: **cold** (fresh process per run; no warm JIT/cache effects).

## Total wall + timing bucket breakdown

Two runs: bare `--timing` (no profiler overhead) and the same command under cProfile.  cProfile roughly doubles wall (every Python call is intercepted); read it for relative shape, not absolute throughput.

### Bare `--timing` run — wall 31.641s

| Bucket | Time | % |
|---|---:|---:|
| typecheck (inner CSF) | 11.448s | 36.2% |
| link | 2.021s | 6.4% |
| typecheck_funcs (CLI-side) | 1.816s | 5.7% |
| parse | 1.656s | 5.2% |
| post_check_analysis | 1.292s | 4.1% |
| borrow_check (inner) | 0.987s | 3.1% |
| checker (inner) | 0.983s | 3.1% |
| hir_to_mir | 0.766s | 2.4% |
| borrow_check_cli | 0.540s | 1.7% |
| codegen.lower | 0.488s | 1.5% |
| generic_instantiation | 0.425s | 1.3% |
| drop_flags | 0.342s | 1.1% |
| string_arc | 0.298s | 0.9% |
| normalize_hir (inner) | 0.249s | 0.8% |
| normalize_hirs_cli | 0.236s | 0.7% |
| ledger_rebuild_post_drop_flags | 0.162s | 0.5% |
| mir_validate | 0.156s | 0.5% |
| ssa | 0.128s | 0.4% |
| _smaller buckets_ | ~0.04s | 0.1% |

**Named coverage: ~24s of 31.6s (~76%).  Unattributed: ~7.6s (~24%).**

`trust_pre_pass` / `trust_verify_loop` don't appear because there are no `--package-root` args.

### Under cProfile — wall 68.895s

Per-bucket times scale unevenly (loop-heavy buckets balloon more than I/O-heavy ones).  Pertinent shifts: `typecheck` 36.2% → 11.8%, `typecheck_funcs` 5.7% → 8.5%, `post_check_analysis` 4.1% → 6.0%, `hir_to_mir` 2.4% → 2.5%.  Read the absolute Python attribution from cProfile, not from these scaled bucket numbers.

## Top Python functions by cumulative time (cProfile)

```
   ncalls   tottime  cumtime  filename:lineno(function)
        1     0.101   47.314  driftc.py:2847(compile_stubbed_funcs)
     5253     1.988   23.241  ownership_ledger.py:285(build_ledger)
     4209     0.006   21.979  ledger_cache.py:63(build_and_attach_ledger)
     1044     0.033   17.318  cleanup_authoring.py:201(author_cleanup)
     1061     0.250   17.233  cleanup_authoring.py:324(_rebuild_ledger)
     2576     1.195   14.392  type_checker.py:1727(check_function)
   375921     8.395   13.973  ownership_ledger.py:409(_join_dicts)
     1237     0.045    8.095  driftc.py:3814(_typecheck_fn)
   411020     2.976    6.609  ownership_ledger.py:449(_walk_block)
  2272233     2.724    6.423  node_ids.py:41(iter_hir_walk)
        1     0.034    5.004  parser:2370(parse_drift_workspace_to_hir)
        2     0.000    4.388  checker:417(run_by_id)
```

Top-line: **`compile_stubbed_funcs` accounts for 47.3s of the 68.9s cProfile run (~69%)**.  Inside CSF, the heaviest *cumulative* path is `build_ledger` (23.2s) and its caller chain `build_and_attach_ledger` (22.0s) / `cleanup_authoring.author_cleanup` (17.3s) / `_rebuild_ledger` (17.2s).

## Top Python functions by self time (cProfile)

```
   ncalls   tottime  filename:lineno(function)
   375921    8.395   ownership_ledger.py:409(_join_dicts)
 87215954    5.473   {method 'get' of 'dict' objects}
 92369966    4.482   {built-in method builtins.isinstance}
   411020    2.976   ownership_ledger.py:449(_walk_block)
  2272233    2.724   node_ids.py:41(iter_hir_walk)
 21849841    2.212   <string>:23(__hash__)                  [dataclass-generated]
     5253    1.988   ownership_ledger.py:285(build_ledger)
 33016676    1.707   {built-in method builtins.hash}
 35111033    1.696   ownership_ledger.py:106(join)
   101998    1.576   type_checker.py:1725(_infer_expr_type)
 24440770    1.556   {built-in method builtins.getattr}
  4870383    1.373   types_core.py:1013(TypeTable.__getitem__)
     2576    1.195   type_checker.py:1727(check_function)
  2353177    1.007   ownership_ledger.py:713(_apply_field_state)
 14474794    0.966   function_id.py:29(function_symbol)
 13592245    0.867   {method 'append' of 'list' objects}
     3867    0.837   linked_world.py:269(merge_trait_worlds)
   403040    0.742   parse_tree_builder.py:33(__call__)
  2353177    0.719   ownership_ledger.py:686(_apply)
```

## Mapping hot functions to timing buckets

| Hot function | Self time | Calls | Bucket(s) it falls under |
|---|---:|---:|---|
| `ownership_ledger._join_dicts` | 8.4s | 376k | Spans `drop_flags`, `ledger_rebuild_post_drop_flags`, `string_arc`, `borrow_check`, **and the unattributed CSF gap** (cleanup_authoring loop, see below) |
| `ownership_ledger._walk_block` | 3.0s | 411k | Same as above |
| `ownership_ledger.join` | 1.7s | 35M | Same as above |
| `ownership_ledger.build_ledger` | 2.0s (23s cum) | 5,253 | Same as above |
| `ownership_ledger._apply_field_state` | 1.0s | 2.4M | Same as above |
| `cleanup_authoring.author_cleanup` | 0.03s (17.3s cum) | 1,044 | **UNATTRIBUTED** — sits between `_timed("ledger_rebuild_post_drop_flags")` and `_timed("string_arc")` in driftc.py:7384, no `events.timed` wrap |
| `cleanup_authoring._rebuild_ledger` | 0.25s (17.2s cum) | 1,061 | Same — UNATTRIBUTED |
| `type_checker.check_function` | 1.2s (14.4s cum) | 2,576 | `typecheck` (inner CSF) + `typecheck_funcs` (CLI) |
| `type_checker._infer_expr_type` | 1.6s | 102k | `typecheck` / `typecheck_funcs` |
| `parser.parse_drift_workspace_to_hir` | 0.03s (5.0s cum) | 1 | `parse` |
| `checker.run_by_id` | 0.00s (4.4s cum) | 2 | `checker` (inner CSF) |
| `linked_world.merge_trait_worlds` | 0.84s | 3,867 | Likely `post_check_analysis` or `csf_typecheck_setup` |
| `dict.get` / `isinstance` / `getattr` / `hash` | ~13s combined | 100M+ combined | Cross-cutting; concentrated in ledger + type_checker |

### Calibration finding worth flagging back to the operator

**~17s of `cleanup_authoring` work currently appears as unattributed wall time**.  driftc.py:7384's per-function `author_cleanup` + `_rebuild_ledger` loop is not wrapped in any `events.timed` block — it sits in the CSF orchestration gap between `ledger_rebuild_post_drop_flags` and `string_arc`.  On the operator's bookkeeper run, this would also be invisible in the named buckets.

This is also a partial explanation of bookkeeper's "still ~10s unattributed" report — a chunk of it is the same cleanup-authoring loop.

(Attribution fix is a separate small change; not a speedup.  Worth landing alongside any real perf work that touches this region.)

## Candidate optimizations (CANDIDATES ONLY, not chosen yet)

Listed in rough order of expected ROI based on the data.  Each is a starting hypothesis — needs validation before any code touches.

### Candidate 1 — Avoid rebuilding ledgers per function per pass

`build_ledger` fires **5,253 times** for this single-source-file compile (~1,237 source functions × ~4-5 rebuilds per function across drop_flags, post-drop-flags rebuild, cleanup_authoring's inner rebuild loop, string_arc, etc.).  The ledger has an attached-dirty-bit infrastructure (`ledger_cache.py`) but `cleanup_authoring._rebuild_ledger` (1,061 calls) appears to unconditionally rebuild after every mutating hook step.

- **Hypothesis**: many `_rebuild_ledger` calls inside `author_cleanup` could be skipped or batched if the mutating step didn't actually invalidate the locals/blocks the next step reads.
- **Expected impact**: if half the rebuilds are unnecessary, ~11s out of the 17s cleanup_authoring cumulative drops → roughly **10-15% total wall**.
- **Validation**: instrument `_rebuild_ledger` call sites with reason tracking; measure how many produce the same ledger they replaced (cache-hit rate); count edges where a real invalidation actually happened.
- **Risk**: ledger staleness is a real correctness concern — the existing aggressive rebuild posture exists because past bugs (commented in `cleanup_authoring.py:78`) cited stale-ledger-poisoning issues.  Any cache contract change needs careful bug-class review.

### Candidate 2 — Reduce `_join_dicts` per-call cost

`_join_dicts` is **the single biggest self-time hotspot** at 8.4s self / 376k calls (~22µs per call).  Hot inner loop:

```python
def _join_dicts(a, b, tracked):
    merged = {}
    for name in tracked:
        sa = a.get(name, LiveState.UNINIT)
        sb = b.get(name, LiveState.UNINIT)
        merged[name] = join(sa, sb)
    return merged
```

- **Hypothesis**: per-call cost is dominated by `tracked` iteration + 3 dict lookups + `join()` Python call per name.  Options: (a) pre-sort `tracked` once at ledger-build start and stash on the func; (b) replace `LiveState` Enum with small-int constants to make `join()` a direct table lookup; (c) Cython/mypyc the file (small surface — see [perf-analysis-mypyc.md](./perf-analysis-mypyc.md) for the broader mypyc cost-benefit).
- **Expected impact**: 2-3x on `_join_dicts` itself → ~5-6s cumulative savings → **~3-5% total wall**.
- **Validation**: microbench the function on a synthetic worst-case dict shape; verify Python-level changes don't regress correctness against the ownership-ledger test suite.

### Candidate 3 — `__slots__` on hot dataclasses (`MirOp`/`HExpr`/`LiveState` ecosystem)

**21.8M `__hash__` calls** + **24.4M `getattr` calls** + **92.4M `isinstance` calls** together account for ~12s self time.  Most of these target dataclass instances whose attribute access goes through `__dict__` lookups.  Adding `__slots__` to the 135 MIR-node classes (`lang/driftc/stage2/mir_nodes.py`) cuts attribute-access cost ~2x and reduces memory traffic.

- **Hypothesis**: `__slots__` is a one-line-per-class addition; gain is broad-but-shallow (~20-30% on attribute-heavy paths).
- **Expected impact**: ~3-5% total wall, fully reversible, no semantic change.
- **Validation**: add `__slots__` to one hot class (e.g., `MirInsn` or `HExpr`), microbench attribute access, then expand if it pays.
- **Risk**: `__slots__` breaks `__dict__`-based hacks (mostly `setattr(obj, "_extra_field", ...)` patterns).  Audit for those before rolling out.

### Candidate 4 — Cache or prune HIR walker (`node_ids.iter_hir_walk`)

`iter_hir_walk` is called **2.27M times** for 2.7s self time.  Each call descends a (potentially large) HIR subtree.  Many call sites are independent — they walk the same tree repeatedly to extract different facts.

- **Hypothesis**: at least some call sites can share a single walk (extracting multiple facts per traversal) or memoize per-node results when the HIR is immutable post-typecheck.
- **Expected impact**: 2-3% total wall, depending on how many independent walks can collapse.
- **Validation**: add per-callsite call-count instrumentation to `iter_hir_walk`; find the heaviest callers and check whether they could share a walk.

### Candidate 5 — `function_id.function_symbol` micro-optimization

14.5M calls / 0.97s self time.  Looking at the function (`function_id.py:29`), it likely formats a `(module, name)` tuple into a "module::name" string.  Cheap individually but called extremely often.

- **Hypothesis**: cache the result on the `FunctionId` instance (1 extra slot, 1-time cost per id); or intern the result via `sys.intern()` for hash-locality wins.
- **Expected impact**: ~1-2% total wall.
- **Validation**: microbench the call; check cache-hit rate against unique-FunctionId-count.

## Decision framework

Sorted by **expected wall reduction × risk-adjusted confidence**:

| Candidate | Expected wall | Risk | Recommended order |
|---|---:|---|---|
| #1 Reduce ledger rebuild count | 10-15% | Medium-High (correctness sensitive) | **First IF a non-correctness-risky pattern is found** — needs design pass before code |
| #2 `_join_dicts` micro-opt | 3-5% | Low | **Good first PR** — small, isolated, no semantic change |
| #3 `__slots__` on MIR nodes | 3-5% | Low-Medium (audit `__dict__` users) | Second PR; can land alongside #2 |
| #4 HIR walker sharing/memo | 2-3% | Medium (callsite-by-callsite work) | Defer until #1-3 land |
| #5 function_symbol cache | 1-2% | Low | Trivial; bundle with #2 |

**Plus** (not a speedup, but a separate quick win): wrap the `cleanup_authoring` loop at driftc.py:7384 in `events.timed("cleanup_authoring")` so the 17s currently hiding in the unattributed CSF gap shows up as a proper bucket.  Bookkeeper's residual unattributed time will drop noticeably once this attribution lands.

## My recommendation

**First speedup PR: Candidate 2 (`_join_dicts` micro-opt) + Candidate 3 (`__slots__` on MIR nodes) bundled together.**

Rationale:
- Combined expected wall: ~6-10%.
- Low risk on both — purely structural / data-shape changes; no algorithmic invariant moves.
- Provides a measurable baseline before tackling the higher-risk Candidate 1.
- Validates the bench/timing harness end-to-end on a real before/after.

**Hold Candidate 1 (rebuild count) for a separate dedicated PR**, after a design pass to identify which rebuilds are safely skippable.  The 10-15% upside is real but the correctness surface is wide — the kind of work that benefits from being its own focused review cycle.

## Limitations of this profile

- **No `trust_pre_pass` / `trust_verify_loop` data** — this workload has no `--package-root`.  Those buckets are 4-7s on bookkeeper but invisible here.  Worth a separate profile pass against a multi-package consumer compile fixture (e.g., the `test_pkg_v1_duplicate_roots_resolved_closure.py` setup) before committing to optimization choices that ignore trust.
- **cProfile overhead skews bucket proportions.**  The "% of cProfile wall" numbers in the bare run are reliable for relative ranking; cProfile-attributed numbers are reliable for "where Python is actually spending CPU" but not for absolute throughput claims.
- **Single workload.**  Regex-heavy stdlib compile.  A more numerics-heavy or trait-heavy workload may shift the hot list — particularly `linked_world.merge_trait_worlds` and `type_checker._infer_expr_type` could climb.

## Raw artifacts (not committed)

- `/tmp/perf_profile/regex_full.prof` — 706KB cProfile output, viewable with `python -m pstats`.
- `/tmp/perf_profile/regex_full.stderr` — full `[drift:timing]` block.
- `/tmp/perf_profile/regex.bin` — compiled binary output.

Reproduce with:

```bash
mkdir -p /tmp/perf_profile && .venv/bin/python -m cProfile \
    -o /tmp/perf_profile/regex_full.prof \
    -m lang.driftc.driftc \
    lang/tests/codegen/e2e/std_regex_parser_corners/main.drift \
    --stdlib-root stdlib --target-word-bits 64 --entry main::main \
    -o /tmp/perf_profile/regex.bin --timing 2> /tmp/perf_profile/regex_full.stderr

# Top 25 by self time:
.venv/bin/python -c "import pstats; p=pstats.Stats('/tmp/perf_profile/regex_full.prof'); p.strip_dirs().sort_stats('time').print_stats(25)"

# Top 25 by cumulative:
.venv/bin/python -c "import pstats; p=pstats.Stats('/tmp/perf_profile/regex_full.prof'); p.strip_dirs().sort_stats('cumulative').print_stats(25)"
```
