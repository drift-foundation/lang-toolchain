# PROGRESS — concurrent-server primitives

Power-loss recovery point for this slice. Newest entries on top. Each entry
records the change + verification result + ABI status. See `PLAN.md` for the full
prioritized design (F1…F5).

## Status table

| Feature | Priority | State | ABI | Notes |
|---|---|---|---|---|
| F1a `conc.yield_now()` | 1 | **DONE / SHIPPING** (0.33.47) | 17 | wraps `vt_yield`; validated two-VT handoff + not-sleep. The only public Phase-1 API. |
| F1b single-fd `io.poll()` | — | **PULLED — not shipped** (direction change 2026-06-20) | n/a | built+tested, then removed before release (wrong server pattern). Design folded into F3. |
| F2 `Duration(0)` ops yield | 2 | **DEFERRED** (team asked not to change `Duration(0)` in first cut) | 17 | hold unless requested |
| F3 unified wait-set / `poll_many` | **NEXT** | **DESIGN for review** (`F3-multifd-plan.md`) | **18** | canonical wait-set primitive; single-fd routes through it as N=1; FIRST public readiness API |
| F4 fair / multi-worker scheduling | 4 | **NOT STARTED** | TBD | report §4.A starvation |
| F5 executor lifecycle + reactor | 5 | **NOT STARTED** | **bump** | `Executor.shutdown` + custom-executor reactor I/O |

## Log

### 2026-06-20 — DIRECTION CHANGE: pull public single-fd `io.poll`; F3 wait-set is the first public readiness API
- Rationale (from product): a public single-fd poll teaches the wrong server pattern
  (one fd at a time). Nothing is certified/released and users are internal, so we
  change course now. Phase 1's shipping surface is **`conc.yield_now()` only**.
- Done:
  - Kept `conc.yield_now()` (unchanged) + its tests.
  - **Removed** `std.io.poll` (export + function + `_interest_code`/`_interest_of`/
    `ERRNO_ETIMEDOUT` helpers) from `stdlib/std/io/io.drift`. Pre-existing error-kind
    constants left intact. No private stepping-stone kept — the design is preserved
    in `F3-multifd-plan.md` + git history and re-authored fresh as the wait-set
    primitive (avoids dead/divergent code).
  - Removed `test_concurrent_yield_poll.py`; added `test_concurrent_yield_now.py`
    (yield_now functional + valgrind only). **PASS** (io.drift recompiles clean).
  - **Kept `TcpListener.raw_fd()`** — the wait-set `PollEntry` is raw-fd based and
    listener accept-readiness is a core use case; updated its docstring to reference
    the forthcoming wait-set API instead of the removed `io.poll`.
- Next phase: **F3 unified wait-set / `poll_many`** as the first public readiness
  API (design `F3-multifd-plan.md`, ABI 18, review before any runtime code).
- ABI: still **17** (yield_now only). DRIFTC_VERSION stays 0.33.47 (yield_now is the
  shipped change).

### 2026-06-20 — review findings 1/2/3 addressed (still 0.33.47, ABI 17)
- **F1 (high): `poll(timeout <= 0)` returned immediately instead of parking.**
  `vt_park_until(0)` returns at once; switched the no-deadline branch to `vt_park(0)`
  (matching `conc.block_on_io`). Also added a re-check of `reactor_check_pending`
  AFTER `register_io` (clearing the just-set waiter on hit) so an edge arriving in
  the register window can't make a `vt_park(0)` block forever. Test
  `test_poll_no_deadline_parks_until_ready` (peer writes ~100ms in; asserts poll
  parked >= 50ms then woke Ok) **PASS** — discriminates the bug.
- **F2 (high): stale reactor waiter on timeout/spurious wake.** After every park,
  `poll` now calls `reactor_register_io(fd, code, 0, 0)` (vt=0 clears the direction,
  no new runtime surface) before deciding the result, so a timeout/spurious wake
  leaves no stale `read_vt`/`write_vt` back-pointer. Confirmed real at the C level:
  the W7 timer-expiry path only `drift_thread_unpark`s (never clears the watch), and
  `drift_vt_claim_for_resume` is a bare PARKED→READY CAS with no `wait_id` guard — so
  a stale back-pointer CAN claim a VT parked elsewhere. Test
  `test_poll_timeout_clears_stale_waiter` added as the cleanup harness + functional
  guard; the spurious-wake race is not deterministically forceable on the single
  cooperative worker (passes with/without the clear there), but the clear is the
  correct defense and future-proofs F4 multi-worker (documented in the test).
- **F3 (medium): `TcpListener.raw_fd()` added** (`net.drift`), mirroring
  `TcpStream.raw_fd()`; the `poll` doc's listener-readiness guidance is now valid.
  No runtime symbol; ABI 17. Direct test `test_poll_listener_accept_readiness`
  (poll the LISTENER fd for Read → connecting peer wakes it → `accept()` succeeds)
  pins the documented server use case. **PASS.**
- **Verification:** full `test_concurrent_yield_poll.py` — **9 passed** (7 functional
  incl. no-deadline + stale-waiter + listener-accept-readiness; 2 valgrind), leaks=0.

### 2026-06-20 — F1 implemented (yield_now + single-fd poll), 0.33.47, ABI 17
- `std.concurrent.yield_now() nothrow -> Void` — wraps `thread.vt_yield()`; exported.
  Docs off-VT behavior (= `vt_yield` → `sched_yield`).
- `std.io.poll(fd, interest, timeout) nothrow -> core.Result<IoInterest, IoError>`
  — single fd/direction; requires VT (off-VT → `requires_vthread`); Read→1, Write→4;
  ET-replay (`reactor_check_pending`) → register → `vt_park_until`; readiness vs
  `timeout` distinguished; conservative-ready (no re-park; caller confirms). Exported.
  Documented: NOT multi-fd; NOT a readiness guarantee (confirm with the I/O); ONE
  waiter per fd/direction — concurrent poll of the same fd/direction is unsupported
  (last waiter wins; cannot reject cleanly without new runtime state — review note).
- **Review note resolved:** duplicate-waiter rejection needs new runtime state →
  out of scope; documented "last waiter wins / unsupported" in the `poll` doc.
- **Verification:** `lang/tests/driver/test_concurrent_yield_poll.py` — yield_now
  two-VT handoff + not-1ms-sleep (4 functional tests **PASS**); poll readiness,
  timeout (distinct kind), pending-edge replay all **PASS**; valgrind variants for
  yield_now + poll-readiness (`--fair-sched=yes`) **PASS** (leaks=0).
  Off-VT `requires_vthread` path is by-inspection (user code can't run off-VT).
- **ABI: 17** (both over existing intrinsics — `vt_yield`, `reactor_*`,
  `vt_park_until`; no new runtime exported symbols). `DRIFTC_VERSION` → 0.33.47.
- F3 multi-fd remains **BLOCKED** on the reactor refactor (see table + PLAN F3).

### 2026-06-20 — plan pinned; runtime ground-truth established
- Read the request and the runtime. KEY findings (see PLAN.md "Runtime ground truth"):
  - `thread.vt_yield()` already implements the requested `yield_now()` semantics
    (`drift_thread_yield`: re-enqueue self on ready queue + swapcontext to scheduler,
    no timer). F1a is a ~1-line stdlib wrapper. **ABI 17.**
  - `conc.sleep(0)` returns `Err(TIMEOUT)` immediately (never yields) — explains the
    team's busy-spin.
  - Single-fd reactor wait template is `io.drift:750 _block_on_io`
    (`reactor_check_pending` ET replay → `reactor_register_io` → `vt_park_until`).
    F1b (`io.poll`) is buildable over these existing intrinsics. **ABI 17.**
  - **Multi-fd (F3) determined UNSAFE on the current reactor:** `drift_reactor_forget_vt`
    (full per-VT registration cleanup) runs ONLY from `drift_vt_destroy`, not on
    park-return; a VT on N fds that wakes on one leaves stale `read_vt` back-pointers
    on the others + carries a single `wait_id`. Multi-fd requires a reactor refactor
    (per-VT wait-set + cleanup-on-every-resume + epoch guard + new intrinsics → ABI
    bump). Matches the user's "only if safe per-VT multi-wait cleanup exists" gate.
- Verification: design only (no code yet). Next step = implement F1a + F1b + tests.
- ABI: F1 = 17 (no new runtime symbols). F3/F5 = bump when implemented.
