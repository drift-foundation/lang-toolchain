# string-hotpath-performance-recovery

**STATUS: IMPLEMENTED / CLOSED (2026-07-26).**  The regression that
blocked certification is RECOVERED: the launch-time trace cache is
implemented in `lang/language_runtime/string_runtime.c` (validation,
eq/cmp, layout, ABI 22 byte-unchanged), toothed, and
acceptance-measured at parse 0.533 / route 0.483 vs certified
0.33.87.  This slice ships inside the COMBINED 0.33.89/ABI-22
candidate together with the closed regex work; remaining steps are
maintainer-owned (baseline promotion, web gate, run-all-tests.sh,
one certification).  Historical classification when opened:
PERFORMANCE_REGRESSION, then-blocking 0.33.88.

## Trigger

drift-web certification failure: baseline-health dropped 161–166k →
142–147k rps on 0.33.88 (three orchestrator runs + 16/16 local
interleaved A/B), attributed by the web team to String
materialization + drop in parse/routing (pin_bench: parse +56%,
route +73%, byte-scan 6× FASTER, Array<Byte> flat).  Compiler-team
correction: ABI 21 already had atomic refcounting; allocation
count/header/payload size materially unchanged; the suspect tax is
**per-operation validation + lifecycle work**, to be isolated by
ablation — not inferred from correlated blocks.

> **SUPERSEDED SECTIONS NOTE (2026-07-26 review):** the original
> phase contract below predates the design review.  The ACCEPTED
> design is: launch-time-cached DRIFT_STR_TRACE with validation
> UNCHANGED; no branch-lean validation, no length-first equality, no
> pointer-identity shortcuts; SSO/tagged storage DEFERRED as an
> optional future project; and the release target is the COMBINED
> 0.33.89/ABI-22 regex + String candidate — one corpus-baseline
> promotion, one certification (no separate 0.33.88 recovery
> certification, no separate regex certification).  See
> STRING-HOTPATH-PERFORMANCE-CHECKPOINT.md rev 2 + the acceptance
> message in Progress.md.

## Phase contract (maintainer directive, 2026-07-26 — historical)

Report-only checkpoint first
(`STRING-HOTPATH-PERFORMANCE-CHECKPOINT.md`), ending at STOP for
design review.  NO implementation, NO version stamping before review.

1. Import a reduced, durable version of drift-web's pin_bench
   evidence (request parse/materialization + path split/route match
   carriers).
2. Correct and pin the attribution (ablations, not inference):
   validation, equality, retain/release, empty handling,
   construction, copying, final free — isolated per term.
3. Measure 0.33.87 (certified) vs current mainline INTERLEAVED:
   construction+final release; non-final retain/release; heap /
   static / immortal / empty; same-handle eq; equal-independent eq;
   unequal-length and unequal-prefix eq; comparison; concat;
   substring materialization across representative lengths; the full
   web parse and route carriers; String-length histogram from the
   web workload.  Counts and allocations as well as time.
4. Compare head-to-head: (1) tagged storage (kind in aligned pointer
   bits; NUL cache stays in header but OUT of unrelated
   retain/release/eq validation); (2) 16-byte SSO (capacity from the
   measured histogram); (3) SSO + tagged heap if they compose;
   (4) a branch-lean ABI-22-shaped control (recovery without
   representation change).
5. NO "non-atomic when rc==1" shortcut without a real
   thread-confinement proof (atomicity was not the regression).
6. Design must close: tombstone vs live empty; heap/static/immortal
   discrimination; fail-closed malformed handles; interior-NUL cache
   authority + concurrency; pointer-identity/equality fast paths;
   StringByteView backing/lifetime; string_bytes_base + callback
   pointer validity; C accessors/FFI ownership (pointer-taking
   borrowed accessors if SSO destabilizes by-value data pointers);
   literal/codegen/runtime layout authorities; downstream C
   consumers + ABI mismatch pins; allocator + refcount overflow.
7. Performance acceptance co-equal with correctness: recover web
   parse/routing to within noise of 0.33.87 or better; recover
   primitive alloc/drop/eq costs (exact before/after tables);
   PRESERVE 0.33.88's byte-read improvement; no regression in long
   Strings, concat, FFI, multithreaded sharing, StringByteView;
   record optimized size + compile wall/CPU; add durable
   allocation-heavy String protocols to tools/perf.
8. StringByteView migration in drift-web may proceed but does NOT
   mask or resolve this regression.
9. Consolidated: design → runtime impl → performance → corpus
   attribution → COMBINED 0.33.89 certification TOGETHER WITH the
   parked regex work (regex HOLD closure per
   work/regex-engine-allocation-removal/RESUME-STATUS.md §5).

## Layout

- `STRING-HOTPATH-PERFORMANCE-CHECKPOINT.md` — the phase record,
  rev 3: IMPLEMENTED/CLOSED (attribution, factorial matrix, winner,
  adjudications, acceptance results).
- `Progress.md` — step log.
- `bench/prim_bench.drift` — dual-toolchain primitive + carrier
  timing protocol (compiles unchanged on 0.33.87 and mainline; the
  tools/perf copy `string_hotpath_bench.drift` is the durable
  perf-protocols surface).
- `bench/gen_ablations.py` + `bench/ablations/` — the factorial
  ablation sources (cached/none × current/branchlean + lean_ref),
  regenerated from the PRESERVED pre-fix runtime
  (`ablations/evidence_base_string_runtime.c`, hash-pinned);
  `--check` always regenerates and compares byte-for-byte.
- `bench/run_matrix.py` — reproducible interleaved runner: builds
  every side from checked-in sources into a fresh temp dir, records
  driftc/cc identities + sha256 + exact commands, fail-closed on
  missing sides/rows/hash drift, trace env scrubbed, seeded shuffled
  side order.
- `bench/hist_ops.drift` + `bench/hist_driver.c` — classified counts
  (heap/static/immortal/tombstone retain/release, --wrap=getenv trace
  multiplier) + String-length histogram.
- `bench/repr_proto.c` — deferred-SSO/tagged representation
  prototypes (preserved for the future optional project).
- `bench/results/` — matrix + counts artifacts with provenance.

## Tree state note

Branch `string-hotpath-performance-recovery` carries the COMPLETED
combined-candidate state: the regex packed-workspace executor
(reviewed clear; HOLD closed — see
work/regex-engine-allocation-removal/) at its base commit 75a7d53a
(the 0.33.89 stamp), plus this slice's String runtime recovery and
the durable perf protocols.  The combined corpus run
ownership-corpus-combined-20260726-102955-3365314 is the promotion
candidate.
