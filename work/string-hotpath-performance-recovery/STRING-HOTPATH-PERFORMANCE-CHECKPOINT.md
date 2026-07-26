# STRING-HOTPATH-PERFORMANCE-CHECKPOINT — rev 3 (IMPLEMENTED / CLOSED)

Classification: **PERFORMANCE_REGRESSION** (was blocking
certification).  STATUS: the accepted design — launch-time-cached
DRIFT_STR_TRACE with validation unchanged — is **IMPLEMENTED** in
lang/language_runtime/string_runtime.c, toothed
(test_string_trace_cache.py: output contract + concurrency hammer
with atomic exact-refcount assertion, normal+debug archives), and
acceptance-measured (reproducible matrix, implemented runtime as the
cur side: parse 0.533 / route 0.483 / clone+drop 0.411 vs certified
0.33.87; results/matrix-20260726T170248Z.json).  This checkpoint is
CLOSED; remaining steps are maintainer-owned (promotion, web gate,
run-all-tests.sh, one combined 0.33.89/ABI-22 certification).

**eq_unequal_len adjudication (reviewer, recorded):** non-blocking.
Independent disassembly comparison of the two binaries showed
`drift_string_eq` is instruction-for-instruction identical (same
0xf5-byte body); only linked address/cache-line placement differs.
The measured candidate penalty is ~0.46 ns/op — inside the original
+1 ns retained-validation envelope — while the real route carrier
improves ~2×.  Consequence: the brittle 1.45 RATIO band is RETIRED
for sub-nanosecond rows; the +1 ns absolute envelope and the carrier
gates remain the acceptance criteria.  No production alignment tweak
from this one micro-row.

Release-sequencing premise (review correction 10): current main is
**75a7d53a**, which already contains the parked regex WIP and stamps
0.33.89.  The target is therefore the **combined 0.33.89/ABI-22
candidate** — String recovery + regex closure — with ONE final
corpus-baseline promotion and ONE certification, not a separate
0.33.88 recovery.

Evidence is checked in and reproducible (correction 7):
`bench/gen_ablations.py` (regenerates every ablated runtime from the
PRESERVED hash-pinned pre-fix source
`bench/ablations/evidence_base_string_runtime.c`; `--check` always
regenerates and compares byte-for-byte), `bench/ablations/*.c` (the
generated sources used), `bench/run_matrix.py` (reproducible
interleaved runner: builds every side from checked-in sources,
records identities/hashes/commands, fail-closed, env-scrubbed,
seeded shuffle), the DECISION matrix
`bench/results/matrix-20260726T155503Z.json` and the FINAL acceptance
matrix `bench/results/matrix-20260726T170248Z.json` (implemented
runtime as the cur side; full per-launch data +
commit/toolchain/host/loadavg),
`bench/results/counts-20260726T155623Z.txt` (classified counts),
`bench/prim_bench.drift`, `bench/hist_ops.drift`,
`bench/hist_driver.c`, `bench/repr_proto.c`.  Certified side:
`driftc 0.33.87 | abi 21 | git 3d48b7f0` (`~/opt/drift/certified/
current`); current side: this tree's driftc + stdlib + runtime.

---

## 1. Exact trace-call accounting (correction 1)

Classified wrap counts (marker windows; `--wrap=getenv` keyed to the
`DRIFT_STR_TRACE` name; retain/release classified by handle kind at
entry using the same flag bits the runtime reads):

Raw window output (each op runs its carrier ×1000; the odd +1s and
+4/+5s belong to one-time SETUP inside the op — subject construction
and scaffolding — not to the steady-state request):

| window (×1000 + setup) | retains h/s/i/t | releases h/s/i/t | **getenv(DRIFT_STR_TRACE)** | materializations |
|---|---|---|---|---|
| parse ×1000 | 9000/0/0/0 | 18001/4/0/18005 | **27,001 = 27×1000 + 1** | 9001 |
| route ×1000 | 9000/0/1000/0 | 12001/1004/2000/4005 | **21,001 = 21×1000 + 1** | 3001 |

Normalized STEADY-STATE figures per request/call (setup excluded):

| per request/call | retains h/s/i/t | releases h/s/i/t | **getenv(DRIFT_STR_TRACE)** | materializations | real allocs |
|---|---|---|---|---|---|
| parse | 9 / 0 / 0 / 0 | 18 / 0 / 0 / 18 | **27** | 9 | 9 |
| route | 9 / 0 / 1 / 0 | 12 / 1 / 2 / 4 | **21** | 3 | 3 |

(h/s/i/t = heap/static/immortal/tombstone.)  The rev-1 claim of "36
releases ⇒ 36 getenv" was wrong exactly as the review said: release
returns before getenv for tombstone/static/immortal handles.  The
true multiplier is **retains(heap) + releases(heap)**: 27 for parse,
21 for route.  Implied per-getenv cost from the matrix below:
475 ns ÷ 27 ≈ 17.6 ns (parse) and 414 ns ÷ 21 ≈ 19.7 ns (route) —
consistent (~18–20 ns per call on this host).

## 2. Corrected attribution: absolute terms that close (correction 2)

The factorial matrix (7 sides × 5 interleaved launches, medians of
same-launch medians — full table §3) yields these per-request terms
for **parse** (ns; route in parentheses, per call):

| term | how measured | value |
|---|---|---|
| net regression | cur − 0.33.87 | **+283** (+243) |
| per-call trace tax | cur − cached_current (only trace policy differs) | **+475** (+414) |
| cached-trace branch residual | cached_current − none_current | +4 (+7) |
| retained-validation cost vs ABI-21-shaped floor | none_current − lean_ref | +2 (+4) |
| concealed 0.33.88 improvements — AGGREGATE RESIDUAL across all non-trace compiler/stdlib/runtime differences between 0.33.87 and the lean floor (bulk substring copying is a plausible major contributor but is NOT independently attributed by this matrix) | 0.33.87 − lean_ref | **−199** (−183) |

Identity check: +475 +4 +2 −199 = **+282 ≈ +283** measured net
(route: +414 +7 +4 −183 = +242 ≈ +243).  The decomposition closes.

So, as the review stated: **the tracing overhead alone exceeds 100%
of the net regression**; other 0.33.88 improvements partially conceal
it.  The rev-1 "~80% / 15–20%" split is retracted.  Validation's
total retained cost on the carriers is single-digit ns per request —
real on eq/cmp microrows (~+0.5–1 ns/op) but immaterial at carrier
level.

## 3. Factorial ablation matrix (correction 3)

2×2 over trace policy {cached at launch, none} × validation {current,
branch-lean}, plus stock ends and the ABI-21-shaped floor reference.
All ablation sides run byte-identical Drift IR (compiled once by
current driftc); only the linked string_runtime.c differs.

| row (µs, medians) | 0.33.87 | cur ABI-22 | cached+current | cached+branchlean | none+current | none+branchlean | lean_ref |
|---|---|---|---|---|---|---|---|
| construct_drop_7 (2M) | 84,733 | 155,402 | 49,510 | 49,695 | 47,832 | 47,679 | 46,883 |
| construct_drop_16 | 113,184 | 168,150 | 63,825 | 64,280 | 62,546 | 62,954 | 61,807 |
| construct_drop_48 (1M) | 123,563 | 120,928 | 64,147 | 64,317 | 76,748* | 63,672 | 76,314* |
| clone_drop_heap (2M pairs) | 38,397 | 73,758 | 16,168 | 16,002 | 15,270 | 15,087 | 14,430 |
| clone_drop_static | 4,649 | 5,024 | 5,344 | 5,694 | 5,314 | 5,317 | 5,271 |
| clone_drop_empty | 4,616 | 5,021 | 5,353 | 5,699 | 4,980 | 5,321 | 5,280 |
| eq_same_handle | 3,199 | 4,273 | 4,264 | 4,280 | 4,266 | 4,620 | 3,890 |
| eq_equal_independent | 3,212 | 4,270 | 4,266 | 4,281 | 4,267 | 4,624 | 3,900 |
| eq_unequal_len | 1,413 | 2,309 | 1,783 | 2,536 | 1,891 | 2,531 | 1,415 |
| eq_unequal_prefix | 3,906 | 4,639 | 4,624 | 4,292 | 4,275 | 4,635 | 3,560 |
| eq_long_last_byte | 3,943 | 5,035 | 5,372 | 5,086 | 5,063 | 5,036 | 3,956 |
| eq_same_handle_long (71 B) | 4,291 | 5,368 | 5,356 | 5,385 | 5,476 | 5,728 | 4,403 |
| cmp_shared_prefix | 3,216 | 5,339 | 4,301 | 4,666 | 4,313 | 4,657 | 4,262 |
| concat_7_7 (1M) | 51,627 | 102,169 | 22,981 | 23,046 | 21,797 | 21,748 | 20,948 |
| **carrier_parse (300k)** | 125,005 | 209,920 | **67,266** | 68,128 | 65,956 | 65,787 | 65,441 |
| **carrier_route (1M)** | 335,248 | 578,636 | **164,088** | 165,435 | 156,876 | 157,247 | 152,745 |
| control_sba_scan | 18,512 | 5,834 | 6,904* | 5,814 | 5,800 | 6,907* | 5,808 |
| control_byte_scan | 1,758 | 1,759 | 1,766 | 1,762 | 1,762 | 1,768 | 1,761 |

(*) individual rows show ±10–20% code-layout sensitivity across
relinks (construct_drop_48, sba_scan wobbles, some eq rows); the
carriers and lifecycle rows are the stable signal.  Full per-launch
distributions in the results JSON.

**Factorial reading, per the review's rule ("simplest measured
winner; do not restructure validation merely because attractive"):**

* Trace policy is the entire carrier-level effect.
* Validation restructuring (branch-lean vs current, at fixed trace
  policy) yields **no measured carrier benefit** (parse 67.3→68.1,
  route 164.1→165.4 — slightly WORSE, within noise) and mixed
  microrow effects.  **Rejected for this recovery.**
* `none` vs `cached` differs by ~1–4% on carriers; the review
  requires preserving the trace feature.

**WINNER: `cached_current` — launch-time-cached DRIFT_STR_TRACE +
validation left exactly as it is.**  A ~3-line runtime change.
Outcome vs certified 0.33.87: parse **1.86× faster**, route **2.04×
faster**, clone+drop pair 8.1 vs 19.2 ns, concat 23.0 vs 25.8 ns/op,
byte-scan improvement preserved (3.2×), statics/empties flat.

## 4. Trace feature design (correction 4)

* `DRIFT_STR_TRACE` read ONCE during process initialization
  (`__attribute__((constructor))`), published as immutable state
  before user threads start.  No lazy unsynchronized init; no
  pthread_once on the hot path.
* Documented: the variable must be set before process launch;
  changing it mid-run has no effect.
* Enabled tracing preserved in normal AND debug runtimes;
  `DRIFT_STR_TRACE_FILTER` remains a per-event lookup on the
  already-enabled slow path only.
* Teeth: trace-disabled run (counts: zero getenv(DRIFT_STR_TRACE)
  after init — wrap-counter pin) and trace-enabled run
  (DRIFT_STR_TRACE=1 still emits retain/release events — output
  pin), in both runtimes.

## 5. Equality/compare contract (corrections 5–6)

* The length-first eq change is **withdrawn**.  ABI 22's guarantee —
  malformed handles fail closed at every entry point — stands; eq
  and cmp validate BOTH operands first, exactly as today.  (The
  rev-1 statement that cmp could "length-first" was wrong anyway:
  lexicographic comparison must compare the shared prefix regardless
  of length inequality.)
* Pointer-identity eq fast path: measured on long same-handle
  Strings (eq_same_handle_long, 71 B): current 2.68 ns/op vs the
  floor 2.20 — a ~0.5 ns/op ceiling on an uncommon shape, and the
  7-byte rows show no gain.  **Not added** in this recovery.
* All legality checks — reserved bits, STATIC+IMMORTAL exclusion,
  **HAS_INTERIOR_NUL-without-NUL_SCANNED coherence**, negative len,
  tombstone observation — remain exactly where they are.  (The
  rev-1 §4/§5 contradiction is resolved by the factorial outcome:
  validation is not restructured at all.  Moving NUL-cache authority
  belongs only to a future representation design.)

## 6. Representation designs: measured and DEFERRED (correction 9)

Retained for the record: C-floor prototypes (`bench/repr_proto.c`)
rank SSO-15 (+ tagged heap) ~29% below the current-layout floor on
the carrier token mix (3.6–3.7 vs 5.0–5.2 ns/token), with short-
string clone/eq becoming ~free; tagged-alone is ~3%.  The measured
length histogram (§1 counts file): parse tokens 1–16 B (16 exactly
once — `application/json`), route segments ≤6 B; ≤15 B covers 8/9
parse tokens.  In-system the SSO gain dilutes to an estimated
10–15% on carriers, against the heaviest closure list
(StringByteView cannot retain an inline backing; FFI/data-pointer
stability ⇒ borrowed pointer-taking accessors; every layout
authority).

**Decision per review: SSO-15 + tagged heap is a FUTURE OPTIONAL
representation project** — not a stage of this recovery, no implied
extra certification.  The current-layout recovery already exceeds
the release target by ~2×.

## 7. Carrier fidelity statement (correction 8)

The reduced carriers reproduce the web team's **relative**
regressions closely (parse +57% vs their +56%; route +69–73% vs
their +73%); absolute times differ from their in-framework blocks
(different iteration mixes and framework overhead).  Final
validation of the recovery must include drift-web's own pin bench
and health-rps gate on the rebuilt toolchain.

## 8. Acceptance gates for implementation GO

* Runtime change: the measured winner only — launch-time trace cache
  (§4).  Validation, eq/cmp, layouts, ABI 22: **unchanged**.
* Gates are SAME-HOST INTERLEAVED PAIRED RATIOS, never portable
  absolute-nanosecond thresholds (absolute timings stay as recorded
  evidence only): fixed-runtime vs certified-0.33.87 same-launch
  ratio bands — carriers parse/route ≤ 0.60 (measured 0.533/0.483);
  clone+drop ≤ 0.60 (measured 0.411); construct+drop 7 B/16 B ≤ 0.70
  (measured 0.559/0.556); concat ≤ 0.60 (measured 0.434); byte-scan
  ≤ 0.40 (improvement preserved; measured 0.373); statics/empties ≤
  1.25 (measured ≤ 1.16); long-String, FFI bridge, StringByteView,
  multithreaded sharing (atomics untouched): no regression on their
  rows.  eq/cmp: the RATIO band for sub-nanosecond rows is RETIRED
  per the reviewer's adjudication (ratios at that scale are dominated
  by code-layout placement — drift_string_eq proved
  instruction-for-instruction identical across the compared
  binaries); the acceptance criterion for eq/cmp is the ABSOLUTE
  +1 ns retained-validation envelope (measured worst case ~0.46
  ns/op) plus the carrier gates.
* Trace teeth (§4), contract teeth (malformed-handle matrix
  unchanged — all diagnostics identical), memcheck/ASAN clean.
* **Durable tools/perf protocol**: the prim_bench carriers move into
  tools/perf and perf-protocols, so allocation-heavy String carriers
  can never be missed again (the B5-era byte-scan-only gate is
  retired as insufficient).
* drift-web validation: their pin_bench blocks and baseline-health
  rps vs certified 0.33.87, interleaved.
* Size/compile: runtime .o only; record deltas (expected ≈0).

## 9. Combined-candidate closure (correction 10)

* Target: **0.33.89 / ABI 22** — String recovery + the parked regex
  work, certified TOGETHER.
* Corpus: the checked-in reviewed baseline predates regex, so the
  final corpus is NOT globally zero-delta.  Expected final state =
  the already-measured, already-attributed regex delta (modal
  {events −3, c3_moveout_owned −3, moveout_expansion −3} × 924 +
  the new pin fixture) and **zero ADDITIONAL delta from the String
  runtime change** (C-runtime-only; no stdlib/compiler change) —
  verified by re-measuring against current main, not assumed.
* The regex review HOLD is CLOSED (RESUME-STATUS §5: both hot-loop
  ablations measured and rejected on evidence — twice, the second
  time under the hardened shuffled-order runner with the preserved
  artifact bench/results/hotloop-20260726T182737Z.json; durable
  protocol landed; attribution fail-closed; wording corrected).
  Remaining: promote ONE final combined reviewed baseline
  (ownership-corpus-combined-20260726-102955-3365314), run ONE
  certification.

---

## DISPOSITION: IMPLEMENTED / CLOSED

All five design decisions were accepted and are DONE: (1) the
launch-time trace cache is implemented in string_runtime.c with
validation unchanged; (2) the §4 trace design is toothed
(test_string_trace_cache.py — exactly-one-lookup, immutability in
both directions with a post-change-only marker, presence semantics,
filter-on-enabled-path-only, atomic exact-refcount concurrency
hammer; normal + debug archives); (3) eq/cmp and all validation are
byte-unchanged (identity fast path not added; the reviewer's
disassembly adjudication is recorded above); (4) SSO/tagged storage
is deferred as an optional future project; (5) the combined 0.33.89
sequencing is executed — regex HOLD closed (all four §5 tasks;
hot-loop ablations rejected on evidence twice, fixed- and
shuffled-order), durable tools/perf carriers landed, corpus measured
and attributed fail-closed.  Remaining steps are maintainer-owned:
promote ownership-corpus-combined-20260726-102955-3365314, run the
drift-web gate and run-all-tests.sh, one 0.33.89/ABI-22
certification.
