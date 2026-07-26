# REGEX-ENGINE-ALLOCATION-CHECKPOINT — rev 2 (post-review; blockers resolved)

Slice: `work/regex-engine-allocation-removal/` — remove allocation
churn from the std.regex NFA executor.  Baseline tree: **committed
mainline `32d676bb`** ("pre-cert"; the string-view phase is commit
`1f934862`).  The 0.33.88 certification is **in flight**; this slice
targets **0.33.89** as one consolidated chunk with its own final
certification (the release boundary).  K owns the implementation
gates and broad suites.

Review outcome (2026-07-26): core design — epoch marks, direct
next-closure construction, iterative DFS-preserving worklist,
function-local scratch — **APPROVED**; nine blockers + a workload
correction resolved below; implementation proceeds without another
arm-selection review unless a stop condition fires.

---

## 1. Current allocation model (measured, call-exact, CORRECTED)

All matching funnels into `_try_match_at_range` (regex.drift:939).
Per **anchored attempt**: `_make_bitmap` (:944) + initial `clist`
(:945); per **consumed byte**: `seeds` (:958) + replacement `clist`
(:969), plus an O(prog) `_clear_bitmap` write sweep and the recursive
`_add_state` closure walk.  `_find_from_range` (:1084) and `is_match`
(:1054) repeat everything per candidate start.  Compile-time
allocations are separate and excluded by the twin-subtraction
windows.

**Corrected allocator-call calibration** (probe rev 2: classified
counters + exact live-pointer set; blocker 1): each
`Array::with_capacity` emits 2 `drift_alloc_array` **wrapper calls**
— **ONE real** `posix_memalign` (the reserve) and **ONE sentinel
no-op** (the zero-capacity empty-init returns the runtime sentinel).
Dropping an array performs exactly **ONE real free**; every other
`drift_free_array` call in the engine's shape is a no-op
(sentinel/NULL tombstone; a call+branch, no allocator work).
`live_end = 0` in every window — the live-set proves real frees
exactly and no leaks.

Per attempt consuming k bytes, per candidate start:

- arrays constructed: 2 + 2k → **real allocations: 2 + 2k**, real
  frees: 2 + 2k
- wrapper traffic: alloc calls 4 + 4k; free calls include no-op
  tombstones (+5/attempt + 3/byte in this code shape)

RETRACTED from rev 1: the "storage + array box, both real
posix_memalign" claim (real allocator work is HALF that) and the
"allocator-dominance" inference — the 2.5 ms DFA-proxy gap proves NFA
**bookkeeping** dominates (~98.6% of runtime); how much of that is
allocator time vs closure-walk vs clear-sweep is **not independently
proven** and will fall out of the rewrite measurement itself.

## 2. Harness and measured evidence

`bench/`: big-scan suite (`ops.drift`, `counts.drift` + `driver.c`),
calibration probe, **generated small-subject suite** (`gen_small.py`
→ 38 timing rows + 22 count windows; single source of truth shared
with the model), faithful Python replica (`model.py`), orchestrator
(`run_bench.py`: content-addressed build cache keyed on
source+stdlib+commit/dirty+versions+flags — stale reuse impossible;
provenance records; heartbeats; canonical quiet-baseline artifact
gated on `REGEX_BENCH_SET_BASELINE=1` AND max loadavg < 1.0; loaded
runs labeled; interleaved ABAB compare mode for the final gate), and
the dual-engine differential (`gen_diff.py` +
`legacy_regex.drift.snapshot` @ 32d676bb → 1000 seeded cases;
**self-validated: 0 mismatches** against the identical engine).

Counters pinned per window: retain, release (split **real** vs
null-tombstone), from_utf8, alloc calls / real allocs / sentinel
no-ops, free calls / real frees (live-set exact) / no-op frees.
**Reconciliation: residual ZERO on all 32 windows** (10 big + 22
small; 4+3 patterns, 5 scenarios, sizes 64 B–2 MiB).  Replica
fidelity: model scan-all checksum equals the real binary's CHECK line
exactly (93,208 matches / last end 2,097,159).

### Small-subject suite (PRIMARY workload; blocker: workload correction)

Real allocations **per single search** (count windows ÷ 100), today:

| scenario | 64 B | 128 B | 256 B | 512 B | 1 KiB | 4 KiB |
|---|---|---|---|---|---|---|
| late hit | 598 | 1,310 | **3,118** | 5,078 | 10,270 | 40,918 |
| no match | 650 | 1,426 | 2,550 | 5,130 | 10,386 | **40,970** |
| early hit | — | — | 12 | — | — | 12 |
| anchored validation | — | — | 514 | — | — | 8,194 |
| 6-branch alternation | — | — | 1,016 | — | — | 16,376 |

An ordinary 256-byte request-shaped search performs ~3,000 real
malloc/free pairs; even the 48 ns zero-width search does 4.  View
forms are engine-identical (+1 retain/+1 real release for the one
subject-view construction; the extra null-tombstone release call is
move machinery, classified and pinned).  String windows pin 0
retains, 0 real releases, 0 null releases, 0 from_utf8.

Representative timing (ns/search; quiet-machine canonical artifact
`bench/results/baseline-quiet.json` governs — figures below from the
reconciled run at load ≲1; late/nomatch dominated by per-attempt
allocation overhead ~150–190 ns/attempt):

- early_256 ≈ 170 ns; zw ≈ 49 ns; anchored_256 ≈ 7.9 µs;
  late_256 ≈ 47 µs; nomatch_4096 ≈ 590 µs; alt_256 ≈ 25 µs;
  compile p1 ≈ 132 ns, 16-branch alt ≈ 1.1 µs.

### Big-scan suite (scaling gates; unchanged from rev 1 modulo the
classification)

2 MiB carrier scan-all: 179.4 ms quiet median; **11.74M real
allocations + 11.74M real frees** (23.5M wrapper alloc calls).  2 MiB
no-match: 268.8 ms; **18.04M real allocs/frees**.  Growth exactly
affine in input length (×31.988 = chunk ratio).  String/view timing
parity ≤0.5%.

## 3–4. Design (approved) + PACKED workspace decision (blocker 3)

Epoch marks (O(1) clear) + direct next-closure (seeds deleted) +
iterative worklist (recursion deleted) in ONE function-local scratch.

**Packed workspace chosen, probe-measured** (blocker 3): ONE
`Array<Int>` of length 4·prog_len, regions `[marks | list A | list B
| worklist]` at bases 0/P/2P/3P, region lengths as scalars, list
regions swapping ROLES via a base toggle.  Probe: packed = **1 real
allocation + 1 real free per scratch** vs **4** for four separate
Arrays (and 200 vs 800 wrapper calls per 100 constructs).  Four
heap allocations per ordinary 250-byte request is rejected; the
four-array form is retained only as the fallback if packing hits an
implementation wall (none expected — plain `Array<Int>` indexing,
no unsafe).

Steady-state contract (the release gate): **0 allocations per
consumed byte; 0 per candidate start; exactly 1 real allocation + 1
real free per top-level search** (the packed workspace), independent
of input length and start count.  No scratch in `Regex` — matching
stays thread-safe, reentrant, callable through shared `&Regex`.

**Scratch lifetimes, exact** (blocker 4): one scratch per top-level
`is_match` / `find_first` / `find_first_view` call; ONE scratch across
the ENTIRE `replace_first`/`replace_all` operation (every internal
search shares it); exported-internal `_try_match_at` and `_find_from`
remain as compatibility wrappers constructing one scratch per
invocation.  Gate semantics: the 64 KiB-vs-2 MiB "identical counts"
gate applies to ONE top-level `find_first` no-match; the manual
scan-all benchmark calls `_find_from` per match, so its pinned
formula is **real allocs = (#matches + 1) × 1** per full scan.

## 5. Iterative algorithm, pinned (blocker 5)

- mark a PC when **pushed** (never on pop) → worklist bounded by
  prog_len;
- ONE fresh epoch per closure, shared by all matching seeds
  contributing to that next closure (initial closure likewise);
- current states processed in existing list order;
- `Split` pushes **b before a** so a expands first (recursive-DFS
  parity; closure contents are epoch-guarded SETS, so match results
  are order-independent regardless — parity kept for like-for-like
  state-list contents);
- region/capacity bounds asserted: `assert(sc.prog_len ==
  nfa.ops.len)` at the authority entry; region cursors are bounded
  structurally by the epoch guard (each pc enters list/worklist at
  most once per epoch — the same invariant that bounds the legacy
  clist), with the packed array's own bounds check as the fail-closed
  backstop; a differential + adversarial tooth exercises deep split
  chains;
- `gen` initialized to 0, incremented BEFORE first use (all-zero
  marks are never a valid "visited" state);
- deterministic overflow reset (§6 of rev 1, unchanged): at ceiling
  2^63−2, zero all marks, gen := 0, continue; tooth via an
  exported-internal `_find_from_gen_saturated` hook that runs a real
  search from a saturated generation and must equal normal results.

## 6. Dual-engine shadow differential (blocker 6)

`generated/diff_main.drift`: the legacy engine snapshot (verbatim,
`Lg`-renamed, from 32d676bb) and the current std.regex compiled into
ONE binary; 1000 seeded cases (240 valid patterns × 4 subjects + 40
invalid patterns) comparing compile success AND error tag/offset,
find_first presence AND exact spans, is_match, and view-vs-String
parity.  Pre-rewrite self-check: **0 mismatches** (harness proven).
The rewrite must hold 0; the legacy executor is deleted from stdlib
in the same chunk — the snapshot IS the shadow.  Permanent pins
retained afterward: the 15 existing `std_regex_*` e2e fixtures
(anchors, class edges/escapes, greediness, quantifiers, zero-length
progress, replace, adversarial stress, utf8 offsets, parser corners,
compile errors) plus new pins for alternation priority and
view-relative offsets where the sweep finds gaps.

## 7. Exported-internal surface (blocker 7 — audited, recorded)

Sweep of EVERY local workspace under `~/src` (drift-workflows,
drift-web, drift-net-tls, drift-mariadb-client, drift-query,
mariachi, pushcoin, build-orchestrator, plus older drift-lang
checkouts): **zero consumers** of the regex exported-internals
outside drift-lang stdlib copies themselves.  In-tree: no users of
`_make_bitmap` / `_clear_bitmap` / `_add_state` outside regex.drift —
not even tests.  Plan: DELETE those three from the export block and
the module (an **exported-internal source-surface change**, described
honestly as such — not "no API change") with a history compatibility
note; KEEP `_try_match_at` and `_find_from` as compat wrappers.
Public API (`compile`/`is_match`/`find_first`/`*_view`/`replace_*`/
`match_view`/`match_subview`) unchanged.

## 8. Hard performance bands (blocker 8 + workload correction)

Primary — representative small-subject suite, interleaved
baseline-vs-candidate on the same quiet machine (ABAB, same-launch
medians, no minima):

- **256 B and 4 KiB late-hit AND no-match: ≥ 2× faster** (median);
- **no representative case slower by more than 5% median**, and no
  individual launch worse than +10% (early/late/nomatch/anchored/alt
  × 6 sizes, String + view forms, compile-once AND compile+match
  rows);
- a meaningful aggregate improvement across the representative suite
  (geomean reported);
- compile rows and optimized binary size: material growth triggers
  review (§10 of rev 1 stands: sizes + stdlib compile wall/CPU
  before/after; ABI 22 expected unchanged — any deviation triggers
  explicit review).

Scaling — big 2 MiB cases remain gates:

- carrier scan-all and 2 MiB no-match: ≥ 2× faster (vs 179.4 /
  268.8 ms quiet baselines);
- wide alternation 512 KiB: ≥ 1.5× faster;
- allocator size-independence: identical matching-only real-alloc
  counts at 64 KiB vs 2 MiB for one top-level find; scan-all obeys
  the (#matches+1) formula exactly.

STOP rule (unchanged): allocations at contract but carrier median
< 1.5× → stop and attribute the exposed bottleneck (first suspect:
the closure-walk volume — 10.6M `_add_state`-equivalent expansions
per carrier scan survive the design) before expanding scope.

## 9–12. Records, plan, corpus (rev 1 §10–§12 stand, corrected)

Baseline commit **32d676bb** (mainline, committed; NOT an uncommitted
delta — rev 1's framing corrected).  `bench/__pycache__` removed.
One consolidated implementation chunk targeting **0.33.89**: engine
rewrite per §3–§5, allocation teeth (wrap-harness: 1-real-alloc
bounded per search, size-independence, scan-all formula, retain/
release-real pins, overflow tooth), semantic suite (existing 15
fixtures + new pins + the 1000-case differential at zero), perf
protocol per §8 with interleaved compare, export-surface change +
history note, docs, corpus measure→attribute(residual zero)→review→
approve→promote BEFORE the final runner, full memcheck/ASAN/broad
suites (user-run), one final 0.33.89 certification.

---

Implementation begins now per the review directive ("proceed directly
to implementation without another arm-selection review unless a stop
condition fires").

---

# ADDENDUM 1 — implementation results (2026-07-26)

Implementation landed per §3–§5; every gate met.

* **Differential**: 1000-case dual-engine shadow — 0 mismatches
  (first compile of the rewrite); valgrind-clean.
* **Semantics**: 16/16 std_regex e2e fixtures (incl. the NEW
  std_regex_view_offsets_alternation pin fixture: view-relative
  offsets, view-boundary anchors, alternation arm-order independence,
  empty match at view end, match_subview composition).  String-view
  phase pins re-verified (9/9: zero-retain matching, +1 view,
  conversions delta 201).
* **Allocation contract** (test_regex_scratch_counts.py, classified
  wrap shim): 1 real alloc+free per top-level search; identical
  windows at 4 KiB vs 256 KiB; scan-all = matches+1 exact;
  replace_all = 1 workspace by from_utf8 subtraction; view +1
  retain/+1 real release; gen-saturated reset equals normal;
  live_end 0.
* **Perf (interleaved ABAB, §8 bands)**: small suite — all 38 rows
  0.41–0.50 ratio (2.0–2.45×), nothing slower; gate cases
  late/nomatch 256 B & 4 KiB ≈ 0.42 (~2.35×).  Big suite — carrier
  0.429 (2.33×; 177.4→76.1 ms), 2 MiB no-match 0.407 (2.46×; view
  0.397), alt 512 KiB 0.623 (1.61×, band 1.5×), compile 0.98–1.00.
  STOP rule not triggered.
* **Size/compile**: ops.bin −32 B; driftc wall 22.4 s legacy vs
  22.3 s new (parity).  ABI 22 unchanged; version bumped to 0.33.89
  with history entry (incl. the exported-internal surface note).
* **Corpus**: measurement run + residual-zero attribution in
  progress; promotion awaits maintainer review (§12 discipline).

Incident note: during the first compile-cost measurement a stray
`git stash` fragment stashed the regex.drift rewrite; the maintainer
restored it via `git stash pop`.  No content lost (stash held exactly
that one file; untracked files unaffected).  All measurements above
were taken on the restored tree.
