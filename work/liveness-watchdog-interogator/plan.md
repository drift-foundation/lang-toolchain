# Runtime liveness watchdog + interrogator

Status: parked until the current toolchain certification is complete.

## Problem

Production can reach a state where the process looks healthy from the
outside — memory, CPU, disk, and container status are acceptable — but
some or all requests stop making progress.  This is not an acceptable
failure mode.  A Drift service must provide a way to answer:

- which virtual threads exist,
- what each VT is doing,
- what each blocked VT is waiting on,
- how long each VT has been in that state,
- whether the runtime as a whole is still making progress, and
- enough context to diagnose the hang after the process is gone.

The desired behavior is twofold:

1. An operator can interrogate a live process and get a liveness dump.
2. A configured watchdog can abort the process with a dump when the
   runtime reaches a no-progress state.

## Non-goals for the first slice

- Do not build a distributed health system.
- Do not infer application-level request semantics in the runtime.
- Do not depend on unsafe work in a POSIX signal handler.
- Do not require a full native stack unwinder before the feature is
  useful.
- Do not treat every long wait as fatal; some waits are legitimate.

## Required runtime state

The runtime needs a low-overhead state record for each VT.  At minimum:

- `vt_id`
- state:
  - `READY`
  - `RUNNING`
  - `PARKED_TIMER`
  - `PARKED_IO`
  - `PARKED_JOIN`
  - `PARKED_MUTEX`
  - `PARKED_CONDVAR`
  - `CANCELLED`
  - `COMPLETED`
- `state_since_ms`
- last progress counter / sequence number
- carrier thread id when running
- wait target:
  - timer deadline
  - fd + interest mask + deadline
  - joined VT id
  - mutex id
  - condvar id
- best available Drift source location:
  - function symbol
  - file/line/column or source span when available

Executor-level state:

- worker count
- ready queue length
- running count
- parked count
- completed count
- shutting-down flag

Reactor-level state:

- registered fd waiters
- registered timers
- next timer deadline
- count of ready events observed but not consumed, if available

## Logical stack requirement

Native C backtraces are not sufficient because Drift VTs are fibers.
The first useful version should expose a logical Drift stack or, at
minimum, the current Drift function/source span at every park boundary.

Acceptable first slice:

- compiler emits or threads through function/source metadata for park,
  join, mutex, condvar, and IO wait sites;
- runtime stores the last known logical frame for each VT;
- dumps show that frame even if full stack reconstruction is not ready.

Better later slice:

- per-VT logical call stack updated at function entry/exit or at
  compiler-selected safe points.

## Interrogation surface

First version should support a live-process dump without requiring the
process to be already failing.

Candidate surface:

```bash
kill -USR2 <pid>
```

Signal-handler rule: the signal handler must not format or write the
full dump.  It should only set an atomic flag or write to an eventfd.
A normal runtime context then emits the dump.

Dump destinations:

- stderr, for container logs;
- a JSON file such as `/tmp/drift-runtime.<pid>.liveness.json`.

Possible later surface:

```bash
drift inspect <pid>
```

That can wrap the signal/file mechanism or use a runtime control socket.

## Watchdog surface

The watchdog should be opt-in and configurable.

Configuration candidates:

- environment variables for first slice:
  - `DRIFT_LIVENESS_WATCHDOG=1`
  - `DRIFT_LIVENESS_TIMEOUT_MS=30000`
  - `DRIFT_LIVENESS_DUMP_DIR=/tmp`
- later: std.runtime configuration API.

On fatal detection:

1. write a liveness dump;
2. log a short fatal summary to stderr;
3. abort the process.

Example fatal line:

```text
[drift:liveness] fatal: no runtime progress for 30000ms; dump=/tmp/drift-runtime.1234.liveness.json
```

## Fatality criteria

The watchdog must not abort merely because one VT waited for a long time.
It should require strong evidence of no progress or a caller-declared
deadline breach.

Candidate hard-fail conditions:

- no VT progress counter changes for the configured timeout while work
  is expected to be live;
- executor has queued work but no worker makes progress for the
  configured timeout;
- reactor has ready events but no VT consumes them for the configured
  timeout;
- all non-completed VTs are parked on waits with no possible wake source;
- a VT exceeds an explicit max wait for join/mutex/condvar/IO, once such
  max-wait annotations exist.

The first slice can be conservative: dump aggressively, abort only on
clear no-progress.

## Dump schema

The dump should be machine-readable JSON, with stable keys.

Minimum shape:

```json
{
  "schema": "drift.liveness.v1",
  "pid": 1234,
  "uptime_ms": 123456,
  "reason": "operator_signal | watchdog_no_progress",
  "executor": {
    "workers": 4,
    "ready_queue_len": 0,
    "running": 0,
    "parked": 12,
    "completed": 1024,
    "shutting_down": false
  },
  "reactor": {
    "fd_waiters": 3,
    "timers": 8,
    "next_deadline_ms": 123456789
  },
  "vts": [
    {
      "vt_id": 42,
      "state": "PARKED_MUTEX",
      "state_since_ms": 123426,
      "wait": {
        "kind": "mutex",
        "id": 77
      },
      "carrier_thread": null,
      "last_progress": 9912,
      "logical_frame": {
        "function": "bookkeeper.server::handle_request",
        "file": "src/server.drift",
        "line": 118,
        "column": 9
      }
    }
  ]
}
```

## Implementation slices

### Slice 1: passive dump plumbing

- Add runtime bookkeeping for VT state transitions and wait targets.
- Add signal-triggered dump request.
- Emit JSON dump from a safe runtime context.
- No abort behavior yet.
- Add tests around dump schema and signal-triggered emission.

### Slice 2: logical frame metadata

- Thread compiler-emitted function/source metadata into park/wait sites.
- Store the latest logical frame on the VT record.
- Extend dump schema with logical frame data.

### Slice 3: watchdog warning mode

- Add a watchdog thread or runtime tick.
- Detect no-progress conditions.
- Emit warning dumps, but do not abort by default.
- Run under stress tests to tune false positives.

### Slice 4: abort mode

- Add opt-in fatal watchdog mode.
- On confirmed no-progress, dump and abort.
- Add integration tests that deliberately deadlock under controlled
  conditions and assert a dump is produced before abort.

## Testing requirements

- Unit tests for state transition bookkeeping.
- Runtime integration test for `SIGUSR2` dump.
- Deterministic deadlock fixture for watchdog warning mode.
- Deterministic abort fixture for watchdog fatal mode.
- JSON schema compatibility test.
- Tests must use deterministic handshakes, not arbitrary sleeps, except
  as outer deadlock guards.

## Open questions

- Should mutex/condvar ids be stable object addresses, monotonic runtime
  ids, or both?
- How much logical stack can be captured cheaply without bloating every
  call?
- Should watchdog abort be process-wide only, or can it request a
  supervised graceful shutdown first?
- How should app-level request ids be attached to VT records?
- Do we need a std.runtime API for applications to publish request
  heartbeat/progress counters?
