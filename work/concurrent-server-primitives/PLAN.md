# PLAN — runtime/stdlib primitives for concurrent network servers

**Driver:** web-drift team request (`/tmp/drift-announce/2026-06-20-runtime-primitives-for-concurrent-servers.md`).
A correct single-fiber event-loop `web.rest` server passes every correctness gate
but fails perf because the only yield available is `conc.sleep(≥1ms)` (944 req/s,
`drift_framework_ratio` 0.00 vs gate ≥0.45).

**Bundling:** may split across commits; **ships as ONE release**. Features are
listed in prioritized order (F1 highest). F1 unblocks the team immediately.

---

## Runtime ground truth (verified by reading the runtime)

- **`thread.vt_yield()` already exists** (`thread.drift:157` → codegen
  `@drift_thread_yield`). Impl `posix/thread_runtime.c:2112` `drift_thread_yield`
  **re-enqueues self on the executor ready queue, stays READY, `swapcontext`s to
  the scheduler** so another VT runs first, then resumes next scheduler turn. No
  timer, no alloc. Off-VT → `sched_yield()`. = the requested `yield_now()` semantics.
- **`conc.sleep(Duration(0))`** returns `Err(TIMEOUT)` immediately (`concurrent.drift`
  `sleep`, `d.millis==0` branch) — never yields → why the team's `sleep(0)` busy-spins.
- **Single-fd reactor wait** (`io.drift:750` `_block_on_io`): ET-replay
  `reactor_check_pending(fd,interest)` → `reactor_register_io(fd,interest,vt,deadline)`
  → `vt_park_until(deadline)`. Interest codes **1=read (EPOLLIN), 4=write (EPOLLOUT)**.
- **Reactor data model** (`posix/thread_runtime.c`): one `ReactorWatch` per fd with
  a single `read_vt` + single `write_vt`; a VT carries a **single `wait_id`=fd**
  (last register wins). Readiness wake clears the firing direction's `read_vt`
  (`:798/:818`) + frees the VT's timers; an edge with NO waiter sets
  `pending_read/_write` for later replay (`:812`). **`drift_reactor_forget_vt` (full
  per-VT cleanup) runs ONLY from `drift_vt_destroy` (`:259`)** — NOT on park-return.

**Consequence:** a VT on N fds that wakes on one leaves stale `read_vt=thisVT` on
the other N-1 watches (no per-wake cleanup) + the single `wait_id` can't represent
N fds → stale-wake bugs. **Multi-fd is a reactor refactor (F3), not a wrapper.**

---

## Prioritized features

### F1 — `conc.yield_now()` + single-fd `io.poll()`   [READY NOW · ABI 17 · highest]
Both over **existing intrinsics only → no new runtime symbols → ABI 17, version bump only.**
This alone unblocks the team's perf gate.

**F1a `std.concurrent.yield_now() nothrow -> Void`**
```drift
pub fn yield_now() nothrow -> Void { thread.vt_yield(); }
```
Off-VT: `vt_yield` already degrades to `sched_yield()`. Export from `std.concurrent`.

**F1b `std.io.poll(fd: Int, interest: IoInterest, timeout: conc.Duration) nothrow -> core.Result<IoInterest, IoError>`**
Single fd, single direction. Mirrors `_block_on_io`; distinguishes readiness vs timeout:
- Requires a VT (off-VT → `Err(kind=requires_vthread)`). `interest`→code (Read=1, Write=4).
  `deadline = millis>0 ? now+millis : 0` (0 = park until ready, no timer).
- ET replay: `reactor_check_pending(fd,code)!=0` → `Ok(interest)` immediately (before register).
- Else `reactor_register_io(fd,code,vt,deadline)`, **then re-check `check_pending`**: an edge that
  arrived in the register window set the pending flag with no waiter; on hit, clear the just-set
  waiter (`reactor_register_io(fd,code,0,0)`) and return `Ok`. This guard is required because the
  ET edge won't re-fire — without it a `vt_park(0)` (no-deadline) park would block forever.
- Park: **positive deadline → `vt_park_until(deadline)`; no deadline (`deadline==0`) → `vt_park(0)`.**
  (`vt_park_until(0)` returns immediately, so it CANNOT be used for "park until ready".)
- **After every wake, clear the waiter** (`reactor_register_io(fd,code,0,0)`) before deciding the
  result — a readiness wake already cleared `read_vt` in the reactor, but a timeout/spurious wake
  did NOT, and the leftover back-pointer can wrongly wake this VT during a later unrelated wait.
- Result: `check_pending!=0` → `Ok`; else `deadline!=0 && now>=deadline` → `Err(kind=timeout)`;
  else (readiness wake — waiter path clears `read_vt`, sets no pending) → `Ok(interest)`.
  **Conservative-ready is correct** (caller's non-blocking I/O confirms; false-ready → WOULD_BLOCK
  → re-poll). Must NOT re-park (ET edge already consumed → would hang).
- **Stale timeout registration is NOT self-healing — it is explicitly cleared** (the post-wake
  `reactor_register_io(fd,code,0,0)` above). `drift_vt_claim_for_resume` is a bare PARKED→READY CAS
  with no `wait_id` guard, so a stale `read_vt` can claim a VT parked elsewhere; the explicit clear
  closes that gap and future-proofs F4 multi-worker. Multi-fd needs per-VT wait-set cleanup → F3.

**F1 tests** (`test_concurrent_yield_poll.py`, TCP loopback; 9 passing): yield_now two-VT
progress + not-1ms-sleep assertion; poll readiness, timeout (distinct kind), pending-edge replay,
**no-deadline `Duration(0)` parks-until-ready**, **stale-waiter cleanup harness**, **listener
accept-readiness** (`TcpListener.raw_fd()` → `poll` → `accept`); memcheck-clean (`--fair-sched=yes`)
yield_now + poll-readiness. off-VT `requires_vthread` is by-inspection (user code can't run off-VT).

### F2 — `Duration(0)` accept/read/write yield on `WOULD_BLOCK`   [OPTIONAL · ABI 17 · low]
Non-blocking ops that find nothing ready call `vt_yield()` before returning `WOULD_BLOCK`.
Lower value once F1 lands; the team asked NOT to change `Duration(0)` in the first cut → HOLD
unless requested. Codegen/stdlib only, ABI 17.

### F3 — multi-fd `poll(entries, timeout)`   [reactor refactor · ABI BUMP · the durable answer]
`poll(&Array<PollEntry{fd,want_read,want_write}>, timeout) -> Result<Array<PollReady{fd,readable,writable,hangup}>, NetError>`.
Required runtime work (this is the stale-wake-prone part):
1. **Per-VT registration set** (intrusive list on `DriftVt`, or per-park epoch token).
2. **Cleanup on EVERY resume** (any-fd / timeout / cancel), not just destroy — generalize
   `drift_reactor_forget_vt` to "forget this VT's current wait-set", called from the resume
   path under `r->mu`.
3. **Generation/epoch guard**: a delivered unpark checks the VT's current epoch == registration
   epoch; drop late edges from a prior park episode. Robust defense even if (2) races.
4. New intrinsics + runtime exports (wait-set register / `reactor_forget_wait_set`) → **ABI bump**.
5. `PollEntry`/`PollReady` types in `std.io`/`std.net`; `hangup` from `EPOLLERR|EPOLLHUP`.

**F3 tests (must prove cleanup):** N-fd wait → wake on one → immediately wait on a DISJOINT fd →
old set's later readiness must NOT spuriously wake; timeout with partial readiness; cancel
mid-multi-wait leaves no stale back-pointer (valgrind + stale-wake assertion). Gate before merge.

### F4 — multi-worker reactor / fair scheduling   [scheduler work · ABI TBD]
Stop freshly-spawned/woken VTs starving behind already-ready I/O fibers on the default single
worker (report §4.A; deterministic FAIL under valgrind). Either FIFO-fair ready-queue dispatch
by ready-age (runtime already stamps `state_since_ms`) or >1 reactor-integrated worker. Reopens
one-fiber-per-connection. Design separately.

### F5 — custom executor lifecycle + reactor integration   [runtime · ABI BUMP · largest]
`Executor.shutdown(self, timeout) -> Result<Void, ConcurrencyError>` (drain+join; today none →
thread leaks) AND custom-executor VTs must service async socket I/O via the same reactor as the
default (today unreliable). New runtime exports → ABI bump. Sequence last.

---

## Release bundling & ABI

| Feature | Surface | New runtime symbols | ABI |
|---|---|---|---|
| F1 yield_now + single-fd poll | stdlib over existing intrinsics | no | **17** (version bump only) |
| F2 Duration(0) yields | codegen/stdlib | no | 17 |
| F3 multi-fd poll | reactor refactor + intrinsics | **yes** | **bump** |
| F4 fair/multi-worker sched | scheduler | maybe | TBD |
| F5 executor lifecycle + reactor | runtime | **yes** | **bump** |

If F3/F5 land in the same release as F1, the **release ABI bumps once** (artifacts rebuild
through cert); F1 alone is ABI 17. Decide the cut line before tagging.

**Recommendation:** ship F1 now (unblocks perf gate, ABI 17, low risk); schedule F3 next as a
focused reactor slice with the stale-wake matrix as its gate; F2/F4/F5 as capacity allows,
folded into the same release if their ABI bump is acceptable.
