# Runtime liveness watchdog + interrogator — Slice 1 (passive dump plumbing)

## Implementation status — LANDED (2026-05-29, compiler 0.33.10 / ABI 15)

Slice 1 implemented and green. Post-review fixes applied: compiler version
bumped 0.33.9→**0.33.10** (ABI shape change requires both); fast-path/cancelled/
invalid-deadline park returns now clear stale wait metadata (`drift_vt_resume_clear`)
so a RUNNING VT never misreports a wait; JSON writer returns success/failure
(checked dprintf via `lv_dp` + `close()` status); JSON write failure emits a
stderr line even when `DRIFT_LIVENESS_TEXT=0`; dump file opened `0600 | O_NOFOLLOW`;
liveness bookkeeping atomics use `memory_order_relaxed`. Deviations from the
plan below, all intentional:

- **Condvar/channel `wait_id` is `0`** (opaque placeholder), not a per-object
  monotonic id. Drift has no easy module-global mutable counter; assigning ids
  needed more machinery than the wait-*kind* value justified for v1. Wait kind
  (CONDVAR) is reported; the specific object id is deferred. JOIN still carries
  the target vtid (clean), IO carries the fd.
- **Channel waits report as `condvar`**: channels block on an internal condvar,
  so they surface under PARKED_CONDVAR. No separate CHANNEL annotation site
  exists (the lock-free `MpscQueue` never parks).
- **`vt_set_wait` required a new `@intrinsic`** (not a plain extern): `lang.thread`
  vt_* calls are hand-lowered by name in `llvm_codegen.py`. Added the intrinsic
  decl + lowering at both `_lower_call` sites + the `declare`. ABI bumped 14→15.
- Files landed: `posix/liveness_runtime.{c,h}` (new), `thread_runtime.c` (VT
  fields/set-points/`drift_thread_set_wait`/SIGUSR2 thread/`drift_liveness_collect`),
  `__init__.py`, `llvm_codegen.py`, `thread.drift`, `concurrent.drift`,
  `versions.py` (ABI 15). Docs: `docs/liveness.md` + cross-links in
  `docs/design/drift-concurrency.md` and `README.md`.
- Tests: `lang/tests/driver/test_liveness_interrogator.py` (3 pass). Regression:
  ABI stamp suite (17 pass, incl. new v15 mismatch), 99 concurrency/condvar e2e
  cases, VT/signal driver tests — all green.

## Context

Production Drift services can reach a state where the process looks healthy
from the outside (memory/CPU/disk/container all fine) but stops making useful
progress. Today there is **no way to look inside the VT scheduler in prod**
and answer: what are the carrier threads doing, what VTs exist, and what is
each waiting on. This must cover **both** failure modes:

- **Hot stuck** (≈100% CPU): a carrier is pegged running a VT that never yields
  (e.g. a contended `Mutex<T>`, which is a spin-lock — `concurrent.drift:444`).
  There is no parked state here; it shows up as a VT `RUNNING` too long.
- **Cold stuck** (≈0% CPU): VTs parked forever on a timer/IO/join/condvar/channel
  wake that never arrives.

"Stuck" = **no useful progress**, independent of CPU level. This slice delivers
operational visibility: a runtime-owned snapshot, triggerable live via
`kill -USR2 <pid>`, emitting a bounded human summary to stderr and full JSON to
a file. No watchdog/abort yet (Slices 3–4). The full staged design lives at
`work/liveness-watchdog-interogator/plan.md`; this file plans Slice 1 only.

## Design principles (from operator requirements)

- **Runtime-owned and independent.** The emit path must not depend on the
  reactor, executor workers, app loggers, Drift IO, or callbacks — any of those
  may be the wedged component. → a **dedicated liveness thread** + raw
  `write(2)`/`open(2)`, never Drift IO.
- **Resilient, not blocking.** Walking scheduler registries uses
  `pthread_mutex_trylock` with a bounded spin/timeout; a lock held by a wedged
  carrier yields a *degraded* section in the dump (noted in the footer), never a
  hang.
- **Bounded stderr.** Counts + carrier summary + a top-N of the most suspicious
  runners/waiters. Full per-VT detail goes only to JSON.
- **Not user-redirectable in v1.** Path/fd are fixed at process start via env;
  app code cannot mutate them. A deliberate `runtime.liveness_snapshot()` admin
  API is deferred.

## Trigger mechanism: dedicated liveness thread

Replace the "route through reactor" idea. At runtime init in
`drift_run_main_on_vt` (`thread_runtime.c:~2195`, where SIGINT/SIGTERM are
already blocked + signalfd'd):

- Block `SIGUSR2` (added to the existing SIGINT/SIGTERM `sigprocmask(SIG_BLOCK)`)
  in the **main thread, before any carrier/reactor/app thread is created**.
  pthreads inherit the signal mask at creation, so blocking it first guarantees
  no thread ever has the default SIGUSR2 disposition (which would *terminate the
  process*). This ordering is a correctness requirement, not an optimization —
  the mask must be set in `drift_run_main_on_vt` prior to executor/reactor
  startup. **(Finding #2.)**
- Spawn `drift_liveness_thread` (one pthread, started once). Its loop:
  `sigwait`/`sigtimedwait` on `{SIGUSR2}`. On `SIGUSR2` → call
  `drift_liveness_emit(LIVENESS_REASON_OPERATOR_SIGNAL)`.
- **Fail-safe if the thread can't start.** If `pthread_create` for the liveness
  thread fails *after* SIGUSR2 is blocked: leave SIGUSR2 blocked (do **not**
  unblock — unblocking re-arms default-terminate), emit one
  `[drift:liveness] warning: liveness thread unavailable` line to stderr, and
  continue. Net effect: the feature is silently disabled and any SIGUSR2 is
  harmlessly queued/ignored; the process is never killed by it.
- In Slices 3–4 this same thread also wakes on a timeout to run the watchdog
  progress check; structure the wait loop now to accommodate that.

This thread does no app work, so it cannot be starved by a hot carrier or a
wedged reactor.

## New files

- `lang/language_runtime/posix/liveness_runtime.c` — snapshot collection, JSON +
  text emitters, the liveness thread, env parsing.
- `lang/language_runtime/posix/liveness_runtime.h` — public surface:
  `void drift_liveness_thread_start(void);` (called from `drift_run_main_on_vt`),
  `void drift_liveness_emit(int reason);` (reused by Slice 4 watchdog).
- Register `liveness_runtime.c` in
  `lang/language_runtime/__init__.py:get_runtime_sources()` (alongside
  `posix/thread_runtime.c`).

The collector needs read access to `thread_runtime.c` internals (VT/exec/reactor
registries + their mutexes). Add **internal accessor declarations** in a shared
private header (or extend an existing one) rather than exposing globals broadly;
the registries (`drift_vt_registry_head`/`_mu`, `drift_exec_registry_head`/`_mu`,
`drift_default_reactor_ptr`) currently live `static` in `thread_runtime.c`. Add
`drift_liveness_collect_*` helper functions *inside* `thread_runtime.c` (which
already holds the locks/structs) and have `liveness_runtime.c` call them — keeps
the locking discipline in one translation unit.

## Runtime state additions (`thread_runtime.c`)

Add to `struct DriftVt` (`thread_runtime.c:87`):

- `atomic_int wait_kind;` — `DRIFT_WAIT_NONE/TIMER/IO/JOIN/CONDVAR/CHANNEL`
  (mutex omitted — it spins, never parks).
- `atomic_uint_fast64_t wait_id;` — **opaque, monotonically-assigned** wait-object
  id (joined vtid; condvar/channel id from a runtime counter — see below); `0` if
  n/a. Never a raw pointer address. **(Finding #3.)**
- `atomic_int_fast64_t state_since_ms;` — set on every state transition; gives
  run-duration (hot) and park-duration (cold).
- `atomic_uint_fast64_t carrier_tid;` — OS tid of the carrier currently running
  this VT; `0` when not running.

State enum (`DriftVtState`, `:120`) stays as-is (single `DRIFT_VT_PARKED`); the
dump synthesizes `PARKED_TIMER` etc. from `(state, wait_kind)`. Minimal blast
radius — no existing state comparison changes.

Set points (single-writer where possible):

- `drift_vt_tls_set(vt)` brackets every resume (`:743/926/...`): on set,
  `carrier_tid = gettid()`, `state_since_ms = now`; on `set(NULL)`, clear
  `carrier_tid`. (Confirm both raw-asm and Valgrind/ucontext resume paths.)
- `drift_thread_park`/`park_until` (`:1828/~1901`): stamp `state_since_ms` when
  entering PARKED; clear `wait_kind`/`wait_id` on resume.
- `drift_thread_join` (`:1650`): set caller `wait_kind=JOIN`, `wait_id=target
  vtid` before parking.
- `drift_reactor_register_io` (`:2324`): set `wait_kind=IO` (fd/interest read
  back from the reactor watch at dump time — reactor is source of truth).

Global progress signal:

- `static atomic_uint_fast64_t drift_progress_counter;` bumped on VT
  completion (`drift_worker_vt_finish`), VT pickup/resume, and reactor unpark.
  Exposed in the dump as `progress_counter` + a `progress_counter_at_ms`. Lets
  an operator sample twice to see motion; Slice 3 automates this.

## Wait-reason annotation for condvar/channel (stdlib hook)

Timer/IO/join annotate for free in C. Condvar/channel park via the *generic*
`thread.vt_park(0)` / `vt_park_until` (`concurrent.drift:867/870/872`) — the
runtime can't distinguish them from a sleep. Add a tiny hook:

- Runtime: `void drift_thread_set_wait(uint64_t kind, uint64_t id);` in
  `thread_runtime.c` (sets current VT's `wait_kind`/`wait_id`). This is a
  **runtime-exported helper consumed by stdlib → an ABI boundary change**, so it
  triggers an ABI bump (see ABI section). **(Finding #1.)**
- Extern decl in `stdlib/lang/thread.drift` (mirror existing `vt_park`):
  `vt_set_wait(kind, id)`.
- Wire in `stdlib/std/concurrent/concurrent.drift`:
  - `sleep` → `vt_set_wait(TIMER, 0)` before its `vt_park_until`.
  - `Condvar.wait`/`wait_timeout`/`wait_until` → `vt_set_wait(CONDVAR,
    <condvar id>)` before park. The id is an **opaque monotonic id** lazily
    assigned to the `CondvarState` (`:659`) on first wait from a global atomic
    counter — *not* the object's raw address (Finding #3). Add a small id field
    to `CondvarState` (stdlib churn is acceptable since this slice already bumps
    ABI + recompiles stdlib).
  - Channel recv/send blocking path (built on Condvar) → `CHANNEL` + channel id
    (same opaque-monotonic scheme).
- `mutex` needs nothing — it spins; contention surfaces as a long-`RUNNING` VT
  (documented in the dump legend).

*(This is the one part that touches stdlib and is the reason for the ABI bump.)*

## JSON schema (`drift.liveness.v1`)

Written with raw `open/dprintf/write` to the resolved path. Shape per
`work/.../plan.md`: top-level `schema/pid/uptime_ms/reason`, `executor`
(`workers/ready_queue_len/running/parked/completed/shutting_down`), `reactor`
(`fd_waiters/timers/next_deadline_ms`), and `vts[]` with
`vt_id/state/wait{kind,id,...}/carrier_thread/state_since_ms/last_progress`.
`logical_frame` is emitted as `null` in Slice 1 (populated in Slice 2). Footer
carries `degraded` flags for any section read under a failed trylock.

## Bounded stderr summary

Every line prefixed `[drift:liveness]`. Contents:

- one header line: pid, uptime, reason, json path (or write-fail note);
- counts: workers, running, ready, parked (broken out by wait_kind), completed;
- carrier summary: each carrier tid → vt_id it runs + run-duration;
- top-N suspicious: longest-`RUNNING` VTs (hot) and longest-`PARKED` VTs (cold),
  N small (e.g. 5 each).

No unbounded per-VT loop on stderr. If JSON write fails, stderr still emits and
the footer line states the failure.

## Env controls (read once at process start)

- `DRIFT_LIVENESS_TEXT=0|1` — stderr summary on/off (default on).
- `DRIFT_LIVENESS_JSON_PATH=<template>` — JSON path; `%p` → pid. Default
  `/tmp/drift-runtime.%p.liveness.json`. (`DRIFT_LIVENESS_DUMP_DIR` from the doc
  honored as the dir when `JSON_PATH` unset.)
- Watchdog env (`DRIFT_LIVENESS_WATCHDOG`, `_TIMEOUT_MS`) parsed but inert this
  slice.

## Files to modify (summary)

- `lang/language_runtime/posix/thread_runtime.c` — VT fields, set points,
  progress counter, `drift_thread_set_wait`, SIGUSR2 mask + thread start in
  `drift_run_main_on_vt`, internal `drift_liveness_collect_*` helpers.
- `lang/language_runtime/posix/liveness_runtime.{c,h}` — new.
- `lang/language_runtime/__init__.py` — add source to `get_runtime_sources()`.
- `stdlib/lang/thread.drift` — `vt_set_wait` extern.
- `stdlib/std/concurrent/concurrent.drift` — annotate sleep/condvar/channel;
  add opaque-monotonic id field to `CondvarState`.
- `lang/versions.py` — bump `DRIFT_RT_ABI_VERSION` 14 → 15.
- `lang/tests/driver/test_abi_version_stamp.py` — verify/refresh for v15.

## Testing

Deterministic handshakes, no arbitrary sleeps except outer deadlock guards
(per project memory on concurrency tests).

- **Driver test** (`lang/tests/driver/`): build a Drift program that parks VTs
  in known states — one in `conc.sleep` (TIMER), one in `join` (JOIN), one on a
  channel/condvar recv (CHANNEL/CONDVAR), one spinning (hot RUNNING) — uses an
  atomic handshake to confirm all are in position, then `kill -USR2` the child;
  assert the JSON file appears, parses, has `schema=="drift.liveness.v1"`, and
  the expected per-VT states/wait_kinds; assert stderr has `[drift:liveness]`
  lines. Outer timeout guard only.
- **Schema test**: validate emitted JSON keys against the documented v1 shape
  (catches accidental key drift).
- **State-bookkeeping check**: a focused fixture asserting `state_since_ms`
  advances on transitions and `carrier_tid` is set only while RUNNING.
- **Valgrind/memcheck**: run the new fixtures through the existing
  `valgrind_cmd` lane (`--fair-sched=yes` per memory) — the spinning-worker
  fixture needs the bounded-spin + `timed_out` flag pattern from
  `feedback_concurrency_test_noncoop_worker_spin`.

## ABI — bump 14 → 15 (Finding #1)

Per repo policy (`AGENTS.md` / `project_abi_policy`), a **runtime-exported helper
consumed by stdlib is a compiler/runtime ABI boundary change even when additive**.
`drift_thread_set_wait` is exactly that. So this slice bumps the ABI in the same
patch:

- Edit `lang/versions.py:15` `DRIFT_RT_ABI_VERSION: int = 14` → `15`. This is the
  single source; `lang/driftc/driftc_versions.py` re-exports it and
  `lang/language_runtime/abi_version_stamp.c` stamps `__drift_rt_abi_version_<N>`
  from it via a compile-time `-D` define, so the new sentinel symbol
  (`__drift_rt_abi_version_15`) is produced automatically.
- Refresh `lang/tests/driver/test_abi_version_stamp.py` in the same patch. It
  reads `DRIFT_RT_ABI_VERSION` dynamically (sentinel-symbol assertions,
  match/mismatch link tests, provenance `abi <N>` checks), so it should track the
  bump automatically — verify it passes and update any baseline that doesn't.
- This is a genuine boundary change, so artifacts rebuild through cert as usual
  (the bump-and-rebuild path, not the same-ABI candidate path).

New `DriftVt` fields also change `sizeof(DriftVt)`, but `DriftVt` is
heap-allocated by the runtime and never embedded in compiled artifacts; the bump
covers the boundary regardless. Confirm at implementation time that no compiled
artifact hard-codes `sizeof(DriftVt)`.

## Out of scope (later slices)

- Slice 2: logical Drift frame (function/file/line at park sites) — `logical_frame`
  is `null` until then; also enables a real "where is the hot VT" answer.
- Slice 3: watchdog warning mode (progress-counter no-progress detection on the
  liveness thread tick).
- Slice 4: opt-in fatal watchdog (dump-then-abort; stderr summary always
  attempted before abort). **Note:** the abort path must attempt the stderr
  summary even when `DRIFT_LIVENESS_TEXT=0` (a disabled routine dump must not
  silence the fatal summary), unless a separate `DRIFT_LIVENESS_ABORT_TEXT=0`
  escape hatch is added. Slice 1's `DRIFT_LIVENESS_TEXT` default-on is fine.
