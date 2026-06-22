# PROGRESS — poll_many spurious-readable via stale epoll event on a reused fd

Power-loss recovery point. Newest on top. See `PLAN.md`.

## Status table

| Item | State |
|---|---|
| Classification | **CORE_BUG / runtime wait-set defect**, confirmed |
| Root cause | epoll dispatch matches events by bare `data.fd` → stale event reattached to reused-fd watch |
| Fix (generation guard) | **DONE** (both dispatch paths via one shared resolver) |
| Deterministic pin | **DONE** — C whitebox resolver test; discrimination verified; NO Drift test surface |
| `register_io` ADD-under-lock (review #2) | **DONE** |
| Test intrinsics removed from production (review #3) | **DONE** — no `reactor_test_*` in stdlib/codegen/runtime |
| Stress companion | **DONE** — valgrind churn, clean + 0 spurious |
| Existing poll_many/net/io/conc regression | **DONE** — poll_many 13 + C pin pass after intrinsic removal; full net/io 63/1-skip green earlier |
| Version bump | **DONE** — DRIFTC_VERSION 0.33.49 → **0.33.50** (patch); ABI stays **18** |
| Release notes | **DONE** — `/tmp/drift-announce/2026-06-21-drift-lang-0.33.50-poll_many-stale-fd-event-release-notes.md` |
| MariaDB tight-loop rerun | pending (handed off) |

## Log

### 2026-06-22 (cont.) — review fixes on the peek-confirm RC
1. **Surface honesty — DECIDED (reviewer):** keep `net_peek_readable` as a `lang.thread`
   internal intrinsic; do NOT move the peek into `reactor_wait_collect_pending` (that
   would put a socket syscall under the reactor mutex and broaden it to internal wait
   paths — wrong trade for a non-app-facing surface). `lang.thread` is already
   toolchain plumbing (`io_read`/`io_write`/`reactor_wait_*`). Documented honestly as
   NEW toolchain-plumbing surface — NOT "no new public API." (No app-facing `std.*` API
   shape change; `poll_many` stays one API.)
2. **Exact-length gate now actually exact:** `test_poll_many_exact_length_read_no_stale
   _readable` uses `io.buffer(8)` and asserts `n == 8` (was buffer(64)/any n>0), so the
   socket is provably drained before the not-readable assertion.
3. **Docs reworded** to the peek-confirm behavior: `readable` is CONFIRMED at return
   (kernel peek) → a read will make progress / EOF / error, not WOULD_BLOCK (absent a
   race). Sharpened the apparent contradiction: a hint is CONSUMED when surfaced (drain
   after a surfaced readable; partial-read + re-poll won't re-surface the remainder),
   but buffered data behind an UN-surfaced latched hint (framed exact-length read) IS
   reported (peek confirms) — which is why more_buffered_than_read holds. Removed the
   reverted reconcile-guarantee wording.
- All 3 gate/control tests green after the fixes.

### 2026-06-22 (cont.) — source-side fix REJECTED; surface peek-confirm is the fix (0.33.52)
- **MariaDB team's catch (correct):** exact-length framed reads are the DOMINANT
  pattern (read header, read exactly payload_len, STOP — never EAGAIN). User-space
  byte accounting CANNOT tell "drained to empty" from "more still buffered" — only the
  kernel can. So the source-side `io_charge` clear:
  - PASSES the exact-length-drained gate, but
  - **FAILS the dual** (read N, M still buffered → poll_many must stay readable) →
    silent hang, STRICTLY WORSE than the loud recycle. **Empirically confirmed:** with
    the io_charge clear, test 1 passed, `test_poll_many_more_buffered_than_read_still
    _readable` FAILED rc=3.
- **REVERTED** the `io_charge` reconcile (back to set-on-overflow only) and removed the
  `io_reconcile_pending` helper + C-test Part B.
- **FIX = surface-time kernel re-confirmation.** `poll_many`, before surfacing a
  `readable` hint, calls the new `drift_net_peek_readable` (recv `MSG_PEEK |
  MSG_DONTWAIT`): `-1` EAGAIN → suppress (stale/replayed); `>=0` data/EOF → keep;
  `-2` not-a-peekable-socket (listener/pipe) → trust the hint (preserves accept()).
  Durable pending stays; the kernel is the source of truth. New intrinsic
  `net_peek_readable` (runtime + thread.drift + codegen via `_f3_int_intrinsics`).
- **Both RC gates now pass:** `test_poll_many_exact_length_read_no_stale_readable`
  (drained → not readable) AND `test_poll_many_more_buffered_than_read_still_readable`
  (remainder → readable + recoverable). Positive controls added
  (`test_poll_many_peek_confirm_positive_controls`: listener accept via the -2
  carve-out; peer-close → readable + EOF).
- Contract reworded earlier (no-stale-latch + confirm-with-op) stands; `_block_on_io`
  needs no peek (its own `io_read` is the confirmation). DRIFTC_VERSION → **0.33.52**.
- **Mishap + recovery:** a stray `git checkout llvm_codegen.py` (in a debug one-liner)
  reverted that file to HEAD (8cece373), discarding the uncommitted net_peek codegen
  only (all other files intact). Re-added net_peek via the proven `_f3_int_intrinsics`
  table; verified end-to-end (minimal compile + both gates green).

### 2026-06-22 — 0.33.50 RC FAILED at MariaDB; corrected root cause + real fix (0.33.51)
- **0.33.50 RC did NOT fix the incident.** MariaDB rerun: same ~7–10% phase-2
  spurious recycle, byte-identical signature (`[KA] ready tok=4 r=1 h=0 e=0` then
  3× single-fd reprobe TIMEOUT). **The generation guard was necessary-but-insufficient
  — NOT the fix for this incident.** 0.33.50 is a failed RC; do not certify.
- **Corrected root cause (the team's lead, confirmed in code):** a **latched
  `pending_read` bit**. During a conn's lease the response edge sets `pending_read=1`
  (I1, durable); the protocol read drains the socket via direct `io_read` but, because
  it reads an exact length and never observes EAGAIN, never routes through
  `_block_on_io`'s `check_pending`/`collect` — so the cached bit is never cleared.
  The conn returns to `available` idle with `pending_read=1` stuck on its watch; the
  keepalive's aggregate `poll_many` `collect` surfaces it → false-positive readable →
  recycle. (The single-fd reprobe TIMEOUTs because the aggregate `collect` already
  consumed the stale bit.) Generation/fd-reuse was a red herring for THIS incident.
- **Missing invariant (per direction #4):** readiness consumed by direct I/O must not
  later be reported as fresh wait-set readiness.
- **Fix:** `drift_reactor_io_charge` (called after every successful `io_read`/
  `io_write`) now reconciles the cached pending bit via
  `drift_reactor_io_reconcile_pending`: normal/under-budget → **CLEAR** the
  direction's bit (reader caught up; a genuine later edge re-sets it; the active
  reader never relies on the bit — it reads the socket buffer directly); over-budget
  fairness path → still SET (the yielding reader must re-poll to finish draining).
  Runtime-only, ABI 18, DRIFTC_VERSION **0.33.50→0.33.51**.
- **Regression (direction #3):** C whitebox Part B in `reactor_stale_fd_event_test.c`
  — latch `pending_read`, run the reconcile (drain), assert `collect_pending` reports
  NOT-ready; over-budget re-arms; write direction independent. **Discrimination
  verified** (force never-clear → assertion aborts).
- **Generation guard kept** (independent latent-bug fix; Part A C test; no regressions)
  but explicitly no longer billed as the incident fix.
- Full net/io/conc regression after the io_charge fix: **62 passed, 1 skipped**.
- **Contract clarified (intentional).** The current `poll_many` doc already PERMITS a
  transient spurious `readable` (`io.drift`: "WOULD_BLOCK after a readiness report is
  normal — just re-poll"). So two distinct issues:
  (1) **Consumer:** the pool recycling on a bare `r=1 h=0 e=0` without a confirming
      non-blocking read is too strict under that contract — a MariaDB-client fix.
  (2) **Runtime quality:** an *indefinite* stale-`readable` latch on a drained idle fd
      is beyond "conservative edge replay" — the defect the io_charge fix removes.
  Tightened the contract with an explicit **no-stale-latch reconciliation guarantee**
  (no *persistent* re-report of already-consumed readiness; transient edge-replay
  still permitted, confirm-with-the-op still required). This makes the runtime-quality
  invariant intentional and gateable.
- **Gate:** the C whitebox Part B (`reactor_stale_fd_event_test.c`) IS the deterministic
  gate for the no-stale-latch guarantee (drain → reconcile → collect reports NOT-ready;
  discrimination verified). The forthcoming DB-free repro's hard-fail criterion should
  be "a fully-read idle fd reports NOT readable" (valid under the tightened contract),
  NOT "aggregate-readable + reprobe-timeout" alone (which the base contract permits).
- PENDING: wire the MariaDB toolchain-parametric repro into the gate when delivered;
  MariaDB-client confirming-read fix (issue 1) tracked on their side.

### 2026-06-21 — review round 3 follow-ups (hygiene)
- Fixed stale comment in `test_poll_many.py` (stress companion now points to the C
  whitebox pin `reactor_stale_fd_event_test.c`, not the removed Drift test).
- Made the gen-0 convention explicit: `wake_fd`/`signal_fd` now register with
  `ev.data.u64 = (uint32_t)fd` (was `ev.data.fd`) — bit-identical, but states the
  "generation 0 = internal fd" contract at the registration sites now that dispatch
  always reads `data.u64`.
- Reactor-thread-path coverage accepted as documented residual (per direction): the
  shared resolver is C-whitebox pinned; the worker path gets real-loop/black-box
  coverage; net/io/concurrency + MariaDB loop are the integration checks.

### 2026-06-21 — review round 3 (remove test intrinsics from production surface)
- **Decision (reviewer):** do NOT ship `reactor_test_*` as public `lang.thread`
  intrinsics in a certified toolchain — they are sharp runtime-mutation hooks that
  expose internal epoll/watch state as apparent stdlib API and can create states real
  code never could.
- **Removed all Drift test surface:** `reactor_test_watch_gen`,
  `reactor_test_inject_event`, `reactor_test_bump_generation`, and the read-only
  `reactor_stale_fd_event_drops` intrinsic — gone from `thread.drift` (decls +
  exports), `llvm_codegen.py` (table + declares + the inject void handler), and the
  runtime (the C shim functions + `_get`). The production runtime surface is now
  `poll_many` behaviour only; the internal `drift_reactor_stale_fd_event_drops`
  counter remains (incremented on the stale-drop path; read only by the C test).
- **Pin moved to a C whitebox unit test:** `lang/tests/runtime/
  reactor_stale_fd_event_test.c` (+ driver `lang/tests/driver/
  test_reactor_stale_fd_event.py`) `#include`s the runtime TU (test-only
  compilation, never packaged) and calls the static `drift_reactor_watch_for_event`
  resolver directly: current-gen resolves, stale/any-mismatch gen drops (+counter),
  gen-0 (wake/signal) bypass, fd decoded from low 32, vanished-fd no-op. **Discrimination
  verified:** neutralizing the guard → the C assertion aborts. Runs in ~0.3s (no
  Drift build).
- The two Drift deterministic tests (inject + bump-generation) were **removed** with
  their intrinsics; the **black-box stress companion** (`test_poll_many_fd_reuse_churn
  _stress_memcheck`, public API only) stays as the real-fd-reuse / real-loop coverage.
- Residual (documented): the reactor-thread dispatch path is not independently
  driven; both loops share the single `drift_reactor_watch_for_event` resolver, which
  the C test pins directly. ABI stays 18; version 0.33.50.

### 2026-06-21 — review round 2 (ABI/versioning, register_io window, test depth)
- **ABI stays 18** per direction: pre-cert churn within the unreleased ABI-18 line;
  the new internal `reactor_test_*` intrinsics + LLVM declares + runtime symbols +
  packaged archive all ship from this one build, so the certified artifact is
  internally consistent. DRIFTC_VERSION set to **0.33.50** (patch); announce + docs
  realigned from the earlier 0.34.0.
- **#2 fixed:** `reactor_register_io` now does its one-time `EPOLL_CTL_ADD` UNDER
  `r->mu` (was after unlock). The post-unlock window could install the kernel
  registration with the prior generation for a concurrently closed/reused fd number,
  which would drop *legitimate* future events for the new watch as stale. Matches
  `reactor_wait_register`'s lifecycle.
- **#3 addressed:** the inject pin is white-box (bypasses real `epoll_wait`). Added a
  deterministic pin that drives the **REAL worker dispatch loop**:
  `test_poll_many_real_loop_drops_stale_event` registers a watch (ADD stamps
  `gen<<32|fd`), bumps the watch generation in place via `reactor_test_bump_generation`
  (simulates close+fd-reuse without re-registering), then a real byte arrives and the
  actual reactor loop decodes the GA-stamped event against the now-GB watch and DROPS
  it — asserted via `reactor_stale_fd_event_drops`. **Discrimination verified**:
  neutralizing the guard → FAILS (real loop applies the stale event). This proves the
  loop reads `events[i].data.u64` (not bare `.fd`) AND the resolver gen-check, on a
  genuinely-pending event.
  - NOTE: a `capture_one`/`apply_captured` epoll-interception approach was prototyped
    and rejected — a blocking `epoll_wait` from a fiber starves the cooperative
    single worker (the byte-sender can't run), and a cooperative poll consumes the ET
    edge before capture. The bump-generation approach drives the real loop without
    either hazard.
  - Both dispatch loops route through the single shared `drift_reactor_watch_for_event`
    resolver; the worker-owned path is exercised end-to-end here, the reactor-thread
    path shares the identical resolver.

### 2026-06-21 — root-caused + fixed + deterministic pin
- **Root cause:** both epoll dispatch loops did `int fd = events[i].data.fd; w =
  find_watch(r, fd)`. A stale event held from a prior `epoll_wait` batch for fd X,
  delivered after X was closed (`forget_fd`) and its number reused by a new watch,
  was **reattached to the new watch** → spurious `pending_read`/`hangup` on a healthy
  conn. memcheck+contention widens the cross-thread window (reactor thread processing
  a batch vs a worker close+reopen). Pre-existing reactor flaw; `poll_many`'s
  watch→close→reopen pattern newly triggers it. Matches the mariadb evidence exactly
  (aggregate readable `r=1 h=0 e=0`; immediate single-fd re-polls all TIMEOUT).
- **Fix (event identity, not re-probing):** `ReactorWatch.generation` (monotonic,
  set at creation); `epoll_ctl(ADD)` stamps `ev.data.u64 = (gen<<32)|fd`; a single
  shared resolver `drift_reactor_watch_for_event` decodes (fd,gen), finds the watch,
  and **drops the event (returns NULL, bumps `reactor_stale_fd_event_drops`) on gen
  mismatch**. Both dispatch loops route through it; `wake_fd`/`signal_fd` keep gen 0
  and are matched by fd before the resolver. `forget_fd` still DEL-before-close and
  frees the watch (a reused fd gets a fresh watch + fresh gen + zero pending).
- **Deterministic regression** `test_poll_many_stale_fd_event_dropped`: creates a
  healthy idle loopback fd, reads its watch generation, injects a stale-gen EPOLLIN
  via the `reactor_test_inject_event` hook (same shared resolver both loops use),
  then asserts aggregate `poll_many` does NOT report readable (and the drop counter
  fired). Contrast pinned: if aggregate says readable, a single-fd re-poll must also
  be readable, else it's a fabricated event. **Discrimination verified:** neutralizing
  the guard → test FAILS rc=3 (aggregate false-positive + re-poll timeout); guard
  present → PASS.
- New `@test_build_only`-style probes/hooks: `reactor_stale_fd_event_drops`,
  `reactor_test_watch_gen`, `reactor_test_inject_event` (+ codegen + thread.drift).
- **Avoided** (per policy): library reprobe-before-recycle as the real fix; clearing
  pending on registration; loosening the poll_many contract.
- **ABI:** `generation` is a runtime-internal field; `ev.data.u64` packing is internal.
  No exported runtime signature/layout change → **DRIFT_RT_ABI_VERSION stays 18**.
  DRIFTC_VERSION minor bump 0.33.49 → 0.34.0.
- **Pending:** stress companion (loopback churn under valgrind); full regression run
  (in progress); release notes; ask mariadb to rerun the tight memcheck+contention
  loop (target 0 phase-2 false positives).
