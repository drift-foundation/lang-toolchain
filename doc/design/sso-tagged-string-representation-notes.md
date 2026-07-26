# SSO / tagged String representation — design notes for a future optional project

Status: **DEFERRED, OPTIONAL.**  Recorded during the 0.33.89 String
hot-path recovery (see the 2026-07-26 entries in `doc/history.md`),
whose review decided: the launch-time trace-cache fix recovers the
regression with ~2× margin on the web carriers; a representation
change is NOT needed for any current gate.  These notes preserve the
measured design inputs so the project can restart without
re-deriving them.  Measurements were taken on the reduced drift-web
request-parse / route-match carriers (x86-64, clang -O2); re-measure
before acting.

## Measured String-length histogram (the SSO-capacity input)

Materialized-String lengths per parsed request / route call, counted
via a wrap harness on the carrier workloads:

| workload | lengths (bytes) |
|---|---|
| request parse (9 tokens/req) | 1, 3, 4, 6, 7, 7, 9, 10, **16** |
| route match (3 segments/call) | 2, 3, 6 |

**≤ 15 bytes covers 8/9 parse tokens (88.9%) and 100% of route
segments.**  The single 16-byte token is `application/json`.

## C-floor prototype ranking (primitive-level, carrier token mix)

Four representations implemented as standalone C prototypes over
identical data (rankings, not absolute promises — Drift codegen
effects like by-value ABI passing of 16-byte handles and
observation-guard interplay are NOT captured):

| design | token materialize+drop | clone+drop 7 B | eq 7 B |
|---|---|---|---|
| R0 branch-lean ABI-22 shape (control) | 5.2 ns | 7.0 ns | sub-ns |
| R1 tagged pointer bits | 5.0 ns (~3% better) | 6.9 ns | sub-ns + identity fast path |
| R2 SSO-15 (≤15 B inline) | **3.7 ns (~29% better)** | **~0 (inline)** | ~0 (inline) |
| R3 SSO-15 + tagged heap | **3.6 ns** | ~0 | ~0 |

* R1 (tagged low pointer bits: heap/static/immortal) alone is
  marginal on heap-dominated mixes; its value is removing the header
  touch for static/immortal handles and giving malformed-handle
  detection a pointer-local encoding.
* R2/R3 remove malloc/free/refcount entirely for ≤15-byte strings.
* Prototype layout that worked: 16 raw bytes; byte 15 (the heap
  pointer's most-significant byte, 0x00 for canonical x86-64 user
  pointers) is the tag — `0x80|len` marks inline (bytes in raw[0..14]),
  0x00 means heap `{int64 len, ptr}`.

## In-system expectation

The runtime primitive is roughly ¼ of in-system token cost (the rest
is substring/Result/bounds machinery), so the ~29% floor win dilutes
to an estimated **10–15% additional carrier improvement** over the
current (post-recovery) runtime.  That was judged not worth the
closure list below while the gates are green.

## Capacity recommendation

**15 inline bytes (16-byte handle).**  Growing to a 24-byte handle to
capture the lone 16-byte token (`application/json`) would enlarge
every String-bearing struct by 8 bytes — a worse trade; consider
interning common header values instead if that token class matters.

## Closure checklist (from the recovery review — every item must be
designed before implementation)

* tombstone vs live empty: inline len-0 becomes the natural empty; the
  drop-only all-zero tombstone needs a distinct encoding;
* heap/static/immortal discrimination via pointer low bits — no header
  flags load on retain/release for non-heap;
* fail-closed malformed-handle behavior with pointer-local tag decode;
* interior-NUL cache: header for heap strings; inline strings scan on
  demand (≤15 B, trivial);
* StringByteView CANNOT retain an inline backing — views over short
  strings must copy-to-heap at construction or carry bytes by value
  (decision required; retain-based zero-copy only for heap backings);
* `string_bytes_base` / callback pointer validity: by-value handles
  have unstable interior pointers ⇒ borrowed pointer-taking C
  accessors across the FFI boundary, plus a deprecation sweep of
  by-value data-pointer users;
* literal/codegen/runtime layout authorities all change (codegen
  literal emission, observation guards, the B5 lean-guard IR);
* downstream C consumers re-pinned; ABI bump (23) with mismatch pins;
* allocator + refcount overflow behavior unchanged for heap; inline
  has no refcount;
* NO "non-atomic when refcount == 1" shortcut without a real
  thread-confinement proof (standing decision).

## Acceptance shape when the project runs

Same-host interleaved paired ratios on the allocation-heavy String
carriers (`tools/perf/string_hotpath_bench.drift`) plus the byte-scan
and StringByteView controls; no regression on long strings, concat,
FFI bridge ops, or multithreaded sharing; the trace-contract and
malformed-handle teeth must pass unchanged.
