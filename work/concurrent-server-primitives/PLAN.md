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

### F1 — `conc.yield_now()`   [SHIPPED · 0.33.47 · ABI 17 · the only public Phase-1 API]
Over the existing `thread.vt_yield` intrinsic → no new runtime symbols → ABI 17, version bump only.
This alone unblocks the team's perf gate (replaces the `sleep(1ms)` yield floor).
```drift
pub fn yield_now() nothrow -> Void { thread.vt_yield(); }
```
Off-VT: `vt_yield` already degrades to `sched_yield()`. Exported from `std.concurrent`.
Test: `test_concurrent_yield_now.py` — two-VT handoff + not-1ms-sleep assertion (functional +
valgrind `--fair-sched=yes`).

**Single-fd `io.poll` — PROTOTYPED, NOT SHIPPED (direction change 2026-06-20).** A public
single-fd `io.poll(fd, interest, timeout)` was built and fully tested on this branch, then **pulled
before release**: a single-fd readiness call teaches the wrong server pattern (one fd at a time). The
**first public readiness API will be the unified wait-set / multi-fd `poll_many`** (F3); single-fd
behavior, if ever exposed, will be a thin wrapper over that one primitive. The single-fd design work
(ET-replay, `vt_park(0)` for no-deadline, post-wake waiter clear, the epoch/stale-wake analysis) is
preserved in `F3-multifd-plan.md` and git history, and feeds directly into F3. `TcpListener.raw_fd()`
is **kept** (wait-set entries are raw-fd based; listener readiness is a core use case).

### F2 — `Duration(0)` accept/read/write yield on `WOULD_BLOCK`   [OPTIONAL · ABI 17 · low]
Non-blocking ops that find nothing ready call `vt_yield()` before returning `WOULD_BLOCK`.
Lower value once F1 lands; the team asked NOT to change `Duration(0)` in the first cut → HOLD
unless requested. Codegen/stdlib only, ABI 17.

### F3 — unified wait-set / multi-fd `poll_many`   [NEXT · the FIRST public readiness API · ABI 18]
**This is now the next phase and the first public readiness API** (single-fd poll was pulled — see F1).
Full design & proof pass: **`F3-multifd-plan.md`**. Summary:
- Make the **wait-set the canonical runtime wait primitive**; single-fd is N=1 routed through it
  (no duplicated semantics). Public `poll_many(entries, timeout) -> Result<Array<PollReady>, IoError>`
  in `std.io` (`PollEntry{fd,want_read,want_write}` / `PollReady{fd,readable,writable,hangup,error}`).
- Runtime: per-VT wait-set (`io_regs` list) + **per-registration epoch guard** on `DriftVt`;
  epoch-stamped watch slots; epoch check in BOTH edge-delivery loops drops stale old-episode edges.
- **Cleanup on EVERY resume path** (readiness / timeout / cancel / spurious via the shared post-park
  `reactor_wait_clear`; fd-close unparks slot waiters; VT-destroy frees `io_regs`).
- New intrinsics (`reactor_wait_register`, `vt_wait_epoch_begin`, `reactor_wait_clear`) → **ABI 18**.
- Gate A compatibility (Phase-1 + net/io e2e unchanged) before/with the refactor; Gate B multi-fd
  matrix with `@test_build_only` stale-edge-drop / slot-occupancy probes for deterministic race tests.
- Optionally migrate `_block_on_io` (both copies) onto the same primitive so every socket op shares
  one wait implementation (risk split in `F3-multifd-plan.md` §6).

### F4 — scheduler fairness   [scheduler work · NO ABI bump · NEXT after F3]
Stop freshly-spawned/woken VTs starving behind already-ready I/O fibers on the default single
worker (report §4.A; deterministic FAIL under valgrind). **Implementation-ready design:
`F4-fairness-plan.md`.** Root cause is the reactor's direct-resume (swapcontext) bypassing the
already-FIFO ready queue; fix = gate direct-resume on an empty ready queue (else enqueue at tail).
~30–50 LOC in two edge-delivery sites + a `ready_count`; **no ABI bump**; rollback = revert the
gate. Recommended over multi-worker (smaller, fixes the actual starvation).

### F5 — custom executor lifecycle + reactor integration   [runtime · ABI BUMP · separate branch]
`Executor.shutdown(mode, timeout)` (drain/cancel + join; today none → thread leaks) AND
custom-executor VTs must service async socket I/O via the global reactor (today the poller is
tied to the default worker). **Implementation-ready design: `F5-executor-lifecycle-plan.md`.**
Two slices: (1) shutdown+join (fixes the leak, no reactor change); (2) reactor poller decoupling
(fixes off-default I/O, ABI bump). Independent of F4; sequence after it.

---

## Release bundling & ABI

| Feature | Surface | New runtime symbols | ABI |
|---|---|---|---|
| F1 yield_now (SHIPPED) | stdlib over existing intrinsic | no | **17** (version bump only) |
| F2 Duration(0) yields | codegen/stdlib | no | 17 |
| F3 unified wait-set / `poll_many` | reactor refactor + intrinsics | **yes** | **18** |
| F4 fair/multi-worker sched | scheduler | maybe | TBD |
| F5 executor lifecycle + reactor | runtime | **yes** | **bump** |

F1 is ABI 17 (shipped). F3 and F5 each bump the ABI; if they land in the same release the
**release ABI bumps once** (artifacts rebuild through cert) — prefer bundling F3+F5 to amortize.

**Recommendation:** F1 (`yield_now`) is done at ABI 17 and unblocks the perf gate now. Next is F3 —
the unified wait-set as the **first public readiness API** (design in `F3-multifd-plan.md`, ABI 18,
reviewed before any runtime code). F2/F4/F5 as capacity allows; bundle the ABI-18 work to amortize
the rebuild.
