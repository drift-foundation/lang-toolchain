# Perf analysis — iterative `std.json` parser (non-recursive re-land)

Durable profiling record for the iterative `_parse_value_iter` hot path and
the object-member hand-off. Companion to the comparative gate
`lang/tests/driver/test_std_json_parse_perf_gate.py` and the history entry
dated 2026-07-27. Instruction counts are from Callgrind; wall-clock is from
the interleaved idle A/B in §1.1 — the binding comparison. None of the
conclusions are prose-only.

## Provenance

| Item | Value |
|------|-------|
| Candidate source | `stdlib/std/json/json.drift` sha256 `4c24f438d11313495cf010fa02cbcb6e96b9c638c94c9fcd13b6029dc740fa10` — the **take** object hand-off (`fields.insert(mem.replace(&mut *pkey, ""), move node)`). This is the bound form in the tree. |
| Repo HEAD at capture | `a40400f67469bb14557db5a00e7b601bcf1bd04a` |
| Toolchain | 0.33.90 / ABI 22 (`lang/versions.py`) |
| Host | perf_event blocked (`perf_event_paranoid=4`, no sudo) → Callgrind for instruction counts; `time.now_monotonic` in-process for wall-clock |
| Label `d4` / "take" | the **bound** hand-off — takes the key out of the frame with `mem.replace` and moves it into `fields`; on the located path clones once into `vspans`. Matches the current source above. |
| Label `d3` / "clone" | the **rejected** alternative — `fields.insert((*pkey).clone(), move node)` (clones into `fields`, and again into `vspans` when locating). Built only for the A/B; NOT in the tree. |

### Exact commands

```
# instruction counts (Ir), delta method (t@8000 − t@4000)/4000 cancels startup
valgrind --tool=callgrind --branch-sim=yes --cache-sim=no \
         --callgrind-out-file=<out> <binary>
# line-level attribution (from repo root so the recorded relative path resolves)
valgrind --tool=callgrind --branch-sim=yes --cache-sim=no --dump-line=yes \
         --callgrind-out-file=<out> g_request_8000
callgrind_annotate --auto=yes <out> stdlib/std/json/json.drift

# wall-clock A/B (the reproducible runner does all of this fail-closed):
PYTHONPATH=. .venv/bin/python lang/tests/driver/perf_json_ab_runner.py \
        --out lang/tests/driver/perf_json_ab_samples.json
#   builds one oracle-stdlib per hand-off form (take from the hash-pinned tree
#   source; clone by exact-one replacement), compiles the same _TIMING_SRC
#   program against each, runs UNPINNED and SERIAL on an idle host, shuffles
#   clone/take order per round (recorded seed), 50 rounds, per-parse ns =
#   micros / n, MEDIAN + min/max of all samples, NO minima selection.

# the perf gate itself (serial, unpinned; never the xdist lane):
PYTHONPATH=. .venv/bin/python -m pytest -p no:xdist -m perf -s \
        lang/tests/driver/test_std_json_parse_perf_gate.py   # (or: just perf-protocols)
```

Instruction cost is `Ir`, delta method (events `Ir Bc Bcm Bi Bim`,
`--branch-sim=yes`; `summary:` field 1 = `Ir`). Wall-clock is the interleaved
idle A/B of §1.1. The perf gate `test_std_json_parse_perf_gate.py` runs
SERIALLY on an idle host (marker `perf`, excluded from the parallel
`lang-driver-test` xdist lane, executed by `just perf-protocols`) and encodes
the per-shape bands below (`_BANDS`), calibrated UNPINNED/SERIAL/idle
2026-07-27 (worst-observed across two runs + margin):

| shape | ratio med/worst band | absΔ med/worst band (ns) | worst observed (take) |
|-------|:--:|:--:|:--:|
| scalar | 1.18 / 1.24 | 9 / 16 | 1.080 / 1.120, 4.3 / 6.6 |
| tiny_arr | 1.42 / 1.52 | 75 / 95 | 1.313 / 1.344, 54.0 / 59.8 |
| tiny_obj | 1.62 / 1.74 | 105 / 135 | 1.533 / 1.556, 80.1 / 83.4 |
| malformed | 1.50 / 1.58 | 85 / 105 | 1.375 / 1.404, 60.6 / 66.0 |
| request | 1.34 / 1.42 | 360 / 430 | 1.215 / 1.233, 254.9 / 270 |

The "worst observed" column is the max across the calibration runs, the
durable A/B runner, and a full gate run. The ratio band is the tighter gate (a
same-process paired reading, so it cancels machine speed and is less
host-sensitive); the absolute-ns band is a coarser host-relative backstop
(scales with machine speed) that catches an overhead blow-up a ratio alone
would miss on a tiny-baseline shape. Both are measured with the gate's own
unpinned/serial protocol, so calibration and gate agree. Margins are sized to
the measured run-to-run variance (the small-baseline shapes swing ~7–8%
between idle runs). Running the shapes CONCURRENTLY (e.g. under xdist) inflates
the ratios via CPU contention — the malformed shape read ~1.48 that way vs
~1.27 serial — which is why the gate is barred from the parallel lane and runs
serially under `just perf-protocols`.

Shapes (all parsed with `json.permissive()`, i.e. **non-located** — `ctx.locate`
is false; this is the gated hot path):
- `scalar` = `12345`
- `tiny_arr` = `[1,2,3]`
- `tiny_obj` = `{"a":1}`
- `request` = `{"id":1234567,"name":"widget-42","active":true,"ratio":314,"tags":["a","b","c"],"meta":{"x":1,"y":2}}`

## 1. Object member hand-off — inline take BOUND (faster on the binding A/B)

The review hypothesised that the object hand-off's key CLONE
(`fields.insert((*pkey).clone(), move node)`) — which on a non-located parse
also leaves the key retained in the frame slot until the frame is popped —
was why the `tiny_obj` shape did not improve, and directed an inline TAKE
(`fields.insert(mem.replace(&mut *pkey, ""), move node)`) that MOVES the key
storage into the map instead of cloning it. Both forms were built and
benchmarked head-to-head.

### Instruction count (Callgrind)

Per-parse `Ir`, delta method `(Ir@8000 − Ir@4000)/4000`. The take removes one
atomic retain of the key per member (`drift_string_retain`, tiny_obj, total
Ir over 8000 parses):

| shape | clone | take | Δ Ir |    | function | clone | take | Δ |
|-------|------:|------:|------:|--|----------|------:|------:|------:|
| tiny_obj | 6040 | 6029 | −11 | | `drift_string_retain` | 1,320,000 | 1,000,000 | −24% |
| request | 38050 | 38035 | −15 | | `drift_string_release` | 3,264,036 | 3,328,036 | +64,000 |

### Wall-clock — the binding interleaved idle A/B (§1.1)

An earlier non-interleaved best-of-12 delta measurement wrongly reported the
take 7–10% SLOWER. That was a **methodology artifact** (separate binaries
measured in separate loops → different machine states; minima selection). The
binding comparison is the interleaved idle A/B below — same process times both
parsers, forms shuffled per round, UNPINNED and SERIAL on an idle host,
medians of all samples, no minima. **It shows the take FASTER:**

Durable unpinned/serial run (`perf_json_ab_samples.json`, 50 samples/form):

| shape | clone iter (ratio) | take iter (ratio) | take vs clone |
|-------|------:|------:|------:|
| tiny_obj | 228.6 ns (1.542) | 217.6 ns (1.460) | **take −11.0 ns** |
| request | 1397.7 ns (1.183) | 1370.7 ns (1.170) | take −27 ns (**wash**) |

The take is a clear, robust win on the tiny-object shape (−11 to −14 ns, ~5%,
same direction across every run) — the −24% retain saving beats the cost of
the short-key `String` clone plus its retain (Drift has no small-string
optimization; every `String` clone is a heap retain). On the request shape the
two are within run-to-run noise: the take-minus-clone difference **flips sign**
across runs (−34 ns pinned, +31 ns one unpinned run, −27 ns another) — i.e. **a
wash**, not a reproducible win either way. (The committed samples above happen
to show the take ahead on request; that is not depended on.)

**Decision — the take is BOUND** (`stdlib/std/json/json.drift`, the current
tree), on: (a) the robust tiny-object wall-clock win, (b) −24% key retains
(Callgrind, machine-independent), and (c) corpus-clean
(`c3_moveout_not_owned`=0). It is never measurably *worse* than the clone —
the request shape is a wash — and the clone's only edge was a marginally
simpler ownership path. The earlier non-interleaved best-of-12 result (which
claimed the take *slower*) is preserved above as a documented
measurement-method artifact.

### 1.1 Interleaved A/B methodology (the binding wall-clock method)

The reproducible runner is `lang/tests/driver/perf_json_ab_runner.py`; its raw
output (provenance + every launch + medians) is committed at
`lang/tests/driver/perf_json_ab_samples.json`.

- **From-scratch binaries** built on the idle machine (load < 0.1): a `take`
  oracle-stdlib (`build_oracle_stdlib` over the hash-pinned tree source) and a
  `clone` oracle-stdlib (the take one with each of the two object-insert lines
  turned back to `(*pkey).clone()` by an EXACT-ONE replacement, fail-closed on
  drift), each compiled with the SAME timing program (warm-up then `n=200000`
  parses of the iterative and the recursive oracle parser, both timed
  in-process with `time.now_monotonic` in A then B order).
- **No pinning**: every launch runs UNPINNED — the OS schedules it — as a
  SERIAL run on an otherwise idle host. Pinning is deliberately absent; the
  ratio is a same-process paired reading, so machine-speed drift cancels within
  each launch. (Concurrency, not lack of pinning, is what corrupts the numbers:
  the gate is barred from the parallel xdist lane for that reason.)
- **Interleaved + shuffled**: 50 rounds; within each round the `clone` and
  `take` binaries for a shape run in a per-round-shuffled order (recorded seed),
  so both forms see the same drifting machine state.
- **Absolute both sides every launch**: each launch prints iter and recursive
  microseconds; per-parse ns = µs/`n`, iter = mean of the A/B orders.
- **Medians, all samples, NO minima**: the two A/B orders are averaged WITHIN
  each launch, so there are **50 paired samples per form per shape** (one per
  round); reported as median (min/max).

## 2. Complete non-located span-handling cost (bounds a node-only specialization)

Question: how much would a node-only specialization (Design 2 — strip all
span machinery from the `!locate` path) save? This bounds the **complete**
span cost — not just `memcpy` — using Callgrind attribution.

Separately-attributable span callees in the non-located `request` profile,
plus the inlined `if ctx.locate` residual from `-g` line attribution:

| span-handling residual | Ir (8000 parses) | per-parse | share of request |
|------------------------|------:|------:|------:|
| `_set_leaf` (body skipped when `!locate`, call still made) | 1,224,000 | 153 | 0.40% |
| `__drift_array_drop_…_SpanTree` (drop of always-allocated empty span vectors) | 136,000 | 17 | 0.04% |
| inlined `if ctx.locate` branches + `move span` drops in `_parse_value_iter` | ~144,000 | ~18 | ~0.05% |
| **total** | **~1,504,000** | **~188** | **~0.49%** |

For the `scalar` shape the entire span residual is `_set_leaf` = 136,000 Ir =
**17/parse = 0.79%**.

Line attribution confirms the `_Completed::Value(node, span)` moves, the
`*sp = move span` root store, and the `val _ds = move span` / `_drop_span`
drops fold to ~0 Ir (`Optional<_SpanTree>::None` is a tag-only value; no heap
work when `!locate`). No `_SpanTree` constructors and no `_set_leaf` *body*
work appear in the non-located profile — the path builds zero span structure.

**Conclusion:** a node-only specialization would save **at most ~0.5%** on the
request shape (≤0.8% on bare scalars). The residual is dominated by the
`_set_leaf` call overhead, not span construction. That is well below the bar
for forking a second parser body (double the surface to keep differentially
correct against the located path), so Design 2 is **not** implemented. The
finding is recorded so the decision is evidence-based, not assumed.

## 3. Designs evaluated

| design | what | result | disposition |
|--------|------|--------|-------------|
| D1 scalar dispatch | route root-scalar inputs to `_parse_scalar_opt` without pushing a frame | landed earlier this slice; removes the frame push/pop for bare-scalar roots | **landed** |
| D3 in-place hand-off | mutate top frame through `&mut stack[top]` instead of pop/mutate/push | landed earlier this slice; removes the whole-frame shallow copy per member | **landed** |
| D3′ inline key take | `mem.replace(&mut *pkey,"")` moved into `fields`, no `val` (avoids E-AUTO-e2bbe721 store-to-outer) | §1.1: interleaved idle A/B — **faster** than clone by 14 ns (tiny_obj) / 34 ns (request); tiny_obj ratio 1.421 vs clone 1.469 | **BOUND — faster + corpus-clean** |
| D2 node-only span strip | remove all span machinery from `!locate` path | §2: bounded ≤0.5% request / ≤0.8% scalar | **rejected — not worth the second parser body** |

(D1 and D3 were profiled and landed in earlier iterations of this slice. D3′
was implemented and benchmarked here; the binding interleaved A/B (§1.1) put
it ahead of the clone on wall-clock — reversing an earlier non-interleaved
best-of-12 result — and it is now BOUND. D2 is rejected on the §2 span bound.)

## 4. Structural iter-vs-rec gap

The residual shallow slowdown vs the recursive oracle is dominated by **heap
frame-stack traffic** — `drift_alloc_array` (~7% of tiny_obj), `malloc`/`free`
(~9%), and the `HashMapCore` frame containers — not by key handling or span
transport, both bounded above. This is the intended trade: the iterative
parser holds container state on the heap so deeply-nested input cannot
overflow the fiber stack (the DoS this re-land closes). The gate
`test_std_json_parse_perf_gate.py` reports this ratio as a material,
honestly-gated number rather than hiding it.
