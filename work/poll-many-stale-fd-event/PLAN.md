# PLAN — poll_many spurious-readable via stale epoll event on a reused fd number

**Classification:** LANGUAGE_BUG / runtime wait-set defect in `std.io.poll_many`,
backed by the epoll dispatch in `lang/language_runtime/posix/thread_runtime.c`.
**Suspected subsystem:** epoll event attribution / fd-lifecycle bookkeeping in the
wait-set runtime.

## Symptom (cert)
`mariadb-rpc-e2e-pool_idle_close_recycle_test#run-memcheck` exit=4 (Phase-2:
"healthy churn caused N spurious recycle(s)"). Passes base/asan and normal+memcheck;
fails ~8–10% only under cert load + memcheck (the mariadb team reproduced it). The
pool's keepalive `poll_many` reports a **false-positive readable** on a healthy,
empty, idle socket; the pool then faithfully recycles a good connection.

Decisive evidence (keep attached to the bug):
```
aggregate poll_many: tok=4 r=1 h=0 e=0
immediate single-fd re-poll #0: TIMEOUT
immediate single-fd re-poll #1: TIMEOUT
immediate single-fd re-poll #2: TIMEOUT
```
Re-poll of the same fd µs later times out → the socket is genuinely empty → the
aggregate `poll_many` fabricated the event. Not stale-token (monotonic `_mint_token`
verified), not a server close (`r=1 h=0 e=0`; phase-1 real close is `h=1`;
`wait_timeout=28800s`), not leftover protocol bytes.

## Root cause (confirmed by inspection)
Both epoll dispatch loops attribute events by **bare fd number**:
`int fd = events[i].data.fd; ReactorWatch *w = drift_reactor_find_watch(r, fd);`
(worker-owned path ~L810/L833; reactor-thread path ~L1351/L1383).

When the reactor thread holds an event for fd X from a prior `epoll_wait` batch, and
a worker concurrently `forget_fd(X)` (close) then a new conn reuses fd number X and
re-registers (new watch), the **stale event is reattached to the new watch** for X →
spurious `pending_read`/`hangup` set on a healthy conn. memcheck + contention widens
the cross-thread window. Pre-existing reactor flaw; `poll_many`'s watch→close→reopen
pattern newly triggers it.

## Fix — event identity (generation), NOT re-probing
1. Add a per-watch monotonic `generation` (`uint32`/`uint64`) to `ReactorWatch`, set
   at creation.
2. On `epoll_ctl(ADD)`, stamp `ev.data.u64 = (generation << 32) | (uint32)fd`.
3. On dispatch (BOTH paths): decode `fd` + `gen`; `w = find_watch(fd)`; **drop the
   event if `w == NULL` or `w->generation != gen`** — no claim, no pending set.
4. Preserve the special `wake_fd` / `signal_fd` handling (they don't use the packed
   identity; match by fd as today).
5. Apply identical decode/drop logic in the worker-owned and reactor-thread loops.

### Explicitly avoid
- Library-level reprobe-before-recycle as the *real* fix (masks the CORE_BUG).
- Clearing all pending bits on registration (breaks edge-triggered replay).
- Changing the `poll_many` contract to permit false-positive readable on idle fds.

## Wait-set cleanup invariants to verify during the fix
- `reactor_wait_clear` invalidates VT slot registrations without leaving fd-level
  pending bits that could belong to a later fd incarnation.
- `forget_fd` still does `EPOLL_CTL_DEL` before close, and frees the watch (so a
  reused fd cannot inherit old `pending_read/write/hup/err` — a fresh watch + fresh
  generation).
- Stale-generation events are dropped without waking a VT or setting pending bits.

## Tests
1. **New standalone regression** `lang/tests/driver/test_poll_many.py`
   (no MariaDB): aggregate `poll_many` over loopback conns; give one a real
   readiness, close it, force fd-number reuse with a fresh healthy conn, aggregate-
   poll; if a new idle fd reports readable, immediately re-poll it 2–3× (short
   timeout); FAIL if aggregate said readable but all confirming polls timed out.
   Exit codes: 1 setup, 2 reuse-not-reached, 3 aggregate false-positive, 4
   confirming-poll-also-ready (not this bug), 5 hang. Run under memcheck/contention;
   must fail pre-fix.
2. Existing poll_many coverage must stay green: invalid fd, timeout-no-token,
   peer-close hangup, hup non-consuming, cancel-no-hang, readiness memcheck.

## Verification matrix
- `pytest lang/tests/driver/test_poll_many.py` (+ new regression) — pre-fix fails,
  post-fix passes.
- Net/io + concurrency regression unaffected.
- Hand the standalone repro to the toolchain artifact; ask MariaDB to rerun the
  tight memcheck+contention loop → target 0 phase-2 false positives.

## Versioning / ABI
- Runtime behavior change only. `ReactorWatch.generation` is a runtime-internal
  struct field; `ev.data.u64` encoding is internal to the reactor. **No exported
  runtime signature/layout change → DRIFT_RT_ABI_VERSION stays 18.**
- Bump `DRIFTC_VERSION` (minor).
- Add a `/tmp/drift-announce/...drift-lang...release-notes.md` entry: confirmed bug,
  fix summary, MariaDB validation status.

## Temporary workaround policy
If cert must go green before this lands, MariaDB may add a defensive confirmation
re-poll before recycling, **explicitly labelled temporary** and tied to the pinned
poll_many regression — not a resolution.
