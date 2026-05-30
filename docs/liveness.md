# Runtime liveness interrogator: looking inside a stuck process

When a Drift service looks healthy from the outside — memory, CPU, disk, and
container status all fine — but stops making progress, you need to see what the
virtual-thread (VT) scheduler is actually doing. The liveness interrogator
answers, live and without a debugger:

- what carrier threads exist and which VT each is running,
- which VTs are runnable, running, parked, completed, or cancelled,
- what each parked VT is waiting on (timer, IO, join, condvar/channel),
- how long each VT has been in that state, and
- whether the runtime as a whole is still making progress.

It is designed for **both** failure modes:

- **Hot stuck** (≈100% CPU): a carrier is pegged running a VT that never
  yields — e.g. a contended `Mutex<T>` (which is a spin-lock) or a tight loop.
  Shows up as a VT in `RUNNING` for a long time, pinned to a carrier thread.
- **Cold stuck** (≈0% CPU): VTs parked forever on a wake that never arrives.
  Shows up as `PARKED_*` VTs with large wait durations.

"Stuck" means **no useful progress**, independent of CPU level.

> This is Slice 1 (passive dump plumbing): interrogation only. The fail-fast
> watchdog that aborts on no-progress is not active yet — see
> [Limitations](#limitations-slice-1).

## Quick use

Send `SIGUSR2` to the process:

```bash
kill -USR2 <pid>
```

The runtime owns a dedicated thread that consumes `SIGUSR2` and writes a
snapshot. It does **not** run in the signal handler, and it does not depend on
the reactor, the executor workers, application loggers, or Drift IO — any of
which may be the wedged component. It walks the scheduler registries with
bounded `trylock`, so a thread that is wedged *while holding a lock* degrades
the affected section of the report instead of hanging the dump.

You get two outputs from one snapshot:

1. a **bounded human summary** on stderr (for container logs), and
2. a **full JSON document** written to a file (for tooling).

## Outputs

### stderr summary

Every line is prefixed `[drift:liveness]`. It is intentionally bounded —
counts, a carrier summary, and the top few most-suspicious runners/waiters —
so it is safe to emit into logs. Full per-VT detail goes only to the JSON file.

```
[drift:liveness] === drift.liveness.v1 pid=1234 uptime=83210ms reason=operator_signal ===
[drift:liveness] json=/tmp/drift-runtime.1234.liveness.json
[drift:liveness] progress_counter=99120 completed=1024
[drift:liveness] executor: workers=4 running=1 ready=0 shutting_down=0
[drift:liveness] reactor: fd_waiters=3 timers=8 next_deadline_ms=...
[drift:liveness] vts: total=42 running=1 ready=0 parked=40 (timer=10 io=20 join=5 condvar=5 channel=0) finished=1 cancelled=0
[drift:liveness] top running (hot-stuck candidates):
[drift:liveness]   vtid=12 carrier_tid=5678 running_for=45000ms
[drift:liveness] top parked (cold-stuck candidates):
[drift:liveness]   vtid=30 PARKED_JOIN wait_id=29 parked_for=60000ms
```

If the JSON file cannot be written, the stderr summary is still emitted and the
`json=` line reports the failure (`json=WRITE_FAILED errno=…`).

### JSON file

Default path: `/tmp/drift-runtime.<pid>.liveness.json`. See
[Environment controls](#environment-controls) to change it. The schema is
described under [JSON schema](#json-schema-driftlivenessv1). The file is created
mode `0600` (owner-only — dumps carry runtime state) with `O_NOFOLLOW` (a
symlink at the target path is refused). If the write fails (e.g. `ENOSPC`), the
stderr summary still emits and its `json=` line reports the failure — and a
one-line error is printed even when `DRIFT_LIVENESS_TEXT=0`.

## Environment controls

Read once at process start; application code cannot redirect the diagnostic
path at runtime (a deliberate `runtime.liveness_snapshot()` admin API is a
later slice).

| Variable | Default | Effect |
|---|---|---|
| `DRIFT_LIVENESS_TEXT` | `1` | `0` suppresses the stderr summary (JSON still written). |
| `DRIFT_LIVENESS_JSON_PATH` | *(unset)* | Output path template; `%p` is replaced with the pid. |
| `DRIFT_LIVENESS_DUMP_DIR` | `/tmp` | Directory for the default filename when `DRIFT_LIVENESS_JSON_PATH` is unset. |

When `DRIFT_LIVENESS_JSON_PATH` is unset the file is
`<DRIFT_LIVENESS_DUMP_DIR>/drift-runtime.<pid>.liveness.json`.

## JSON schema: `drift.liveness.v1`

```json
{
  "schema": "drift.liveness.v1",
  "pid": 1234,
  "uptime_ms": 83210,
  "reason": "operator_signal",
  "progress_counter": 99120,
  "now_ms": 123456789,
  "executor": {
    "present": true, "workers": 4, "ready_queue_len": 0,
    "running": 1, "parked": 40, "completed": 1024, "shutting_down": false
  },
  "reactor": {
    "present": true, "fd_waiters": 3, "timers": 8, "next_deadline_ms": 123456999
  },
  "tallies": {
    "running": 1, "ready": 0, "parked": 40, "finished": 1, "cancelled": 0,
    "wait": { "timer": 10, "io": 20, "join": 5, "condvar": 5, "channel": 0 }
  },
  "vts": [
    {
      "vtid": 30,
      "state": "PARKED_JOIN",
      "state_since_ms": 63456,
      "age_ms": 60000,
      "carrier_tid": null,
      "last_progress": 9912,
      "wait": { "kind": "join", "id": 29 },
      "logical_frame": null
    }
  ],
  "truncated": false,
  "degraded": { "vt_registry": false, "exec_registry": false, "reactor": false }
}
```

Key fields:

- **`vtid`** — the virtual-thread id. This is the **same value `std.log` emits
  as `vtid`** (both come from the runtime's `thread.vt_id()`), so liveness output
  correlates directly with application log lines.
- **`progress_counter`** — bumped on every scheduler advance (VT resumed, VT
  completed). Sample it twice a few seconds apart: if it does not move while
  work is expected, the runtime is not making progress.
- **`state`** — one of `NEW`, `READY`, `RUNNING`, `COMPLETED`, `CANCELLED`, or
  a parked refinement `PARKED_TIMER` / `PARKED_IO` / `PARKED_JOIN` /
  `PARKED_CONDVAR` / `PARKED` (generic).
- **`age_ms`** — how long the VT has been in its current state. For `RUNNING`
  this is the uninterrupted run duration (hot-stuck signal); for `PARKED_*` it
  is the wait duration (cold-stuck signal).
- **`carrier_tid`** — the OS kernel thread id (TID) running the VT, or `null`
  when not running. This is the **same kernel TID that `std.log` emits as `tid`**
  and that `top`/`ps`/`/proc`/`perf`/`strace` use — attach `gdb`/`perf` to it to
  inspect a specific hot carrier.
- **`wait`** — what a parked VT is blocked on. `timer` carries `deadline_ms`;
  `io` carries `fd` + `events`; `join` carries `id` (the joined VT's id);
  `condvar`/`channel` carry an opaque `id` (0 in Slice 1). There is no
  parked-mutex state: `Mutex<T>` spins, so mutex contention appears as a
  long-`RUNNING` VT, not a parked one.
- **`degraded`** — if any section is `true`, a lock was held (likely by a
  wedged thread) and that section of the report is incomplete. This is itself
  a strong diagnostic signal — note *which* lock could not be taken.
- **`truncated`** — `true` if there were more VTs than the per-snapshot cap.

## How to read it

**Hot stuck (high CPU):**
1. Look at `tallies.running` and the "top running" list / `vts[]` entries in
   `RUNNING`.
2. A VT with a large `age_ms` while `RUNNING` is pinning its `carrier_tid`.
   That carrier TID is your target — attach `perf top -t <tid>` or
   `gdb -p <pid>` and inspect that thread to find the loop / spin.
3. A `degraded.*` flag plus a hot carrier often means the hot VT is spinning
   *while holding* that lock.

**Cold stuck (low CPU):**
1. `tallies.running` is 0 (or near it) and `parked` is high.
2. Group the parked VTs by `wait.kind`. The dominant kind tells you the
   subsystem: `io` (fd never became ready / peer gone), `join` (waiting on a VT
   that never completes — follow `wait.id`), `timer` (check `deadline_ms` vs
   `now_ms`), `condvar`/`channel` (a signal/send that never came).
2. Cross-reference `reactor.fd_waiters` / `reactor.timers` /
   `reactor.next_deadline_ms` to confirm the reactor still holds the wakeups.

## Limitations (Slice 1)

- **`logical_frame` is always `null`.** Per-VT Drift source location
  (function/file/line at the park site) lands in a later slice. For now, use
  `carrier_tid` + a native debugger for the hot-stuck case.
- **No watchdog.** The opt-in fail-fast watchdog that dumps and aborts on
  confirmed no-progress is not active yet. Slice 1 is interrogation only.
- **`condvar`/`channel` wait ids are `0`** (opaque placeholder); channel waits
  report as `condvar` because channels block on an internal condvar.
- **Linux only.**

## What to include in a bug report / incident

- The full JSON dump file (`/tmp/drift-runtime.<pid>.liveness.json`).
- The `[drift:liveness]` stderr lines from the container log.
- **Two** dumps a few seconds apart, so the toolchain team can see whether
  `progress_counter` and any `RUNNING` VT's `age_ms` are moving.
- For a hot carrier, a `gdb -p <pid>` thread backtrace of the `carrier_tid`
  TID named in the dump.
- Any non-empty `degraded` flags (they point at a wedged lock holder).
