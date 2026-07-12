# Blocking FFI from virtual threads

Demonstrates BOTH halves of the blocking-FFI contract:

- **Scheduler**: blocking C calls run on a dedicated, bounded, NAMED
  `BlockingExecutor`; cooperative carriers are never pinned. Admission is
  bounded (`Block(timeout)` by default) — saturation surfaces as ordinary
  `Result` errors, never an unbounded pile-up.
- **Diagnostics**: named executor + labeled operations + named extern
  wrappers mean a wedged process explains itself.

## The rule

> A blocked worker is acceptable only if operators can identify **which
> subsystem, which operation, which extern call, and where in Drift
> source** — from `kill -USR2` alone.

## Try it

```
kill -USR2 <pid>   # after ./example_blocking_ffi --stuck &
```

Expected stderr summary (abridged):

```
[drift:liveness] top running (hot-stuck candidates):
[drift:liveness]   vtid=2 carrier_tid=... running_for=...ms op=demo.stuck_op exec_id=2 ffi=usleep@.../main.drift:33
```

Expected JSON excerpts (`drift.liveness.v1`):

```json
"execs": [{"id": 2, "name": "storage-demo", "queue_len": 0, "running": 1,
           "queue_limit": 1, "waiters": 1, "workers": 1, "shutting_down": false}]

{"vtid": 2, "state": "RUNNING", "carrier_tid": 12345,
 "op": "demo.stuck_op", "submitter": 1, "exec_id": 2,
 "ffi": {"symbol": "usleep", "file": ".../main.drift", "line": 33}}

{"vtid": 3, "state": "PARKED",
 "wait": {"kind": "blocking-admission", "exec_id": 2, "deadline_ms": ...},
 "op": "demo.waiting_op"}
```

Reading it: the `storage-demo` executor's one worker has been RUNNING
`demo.stuck_op` inside `usleep` (called from `main.drift:33`) for
`running_for` ms, and one labeled submission is parked awaiting
admission behind it.

## Limitations

Drift identifies the Drift-visible operation and extern symbol/callsite.
It cannot see inside the C library — if `mdb_txn_commit` is internally
stuck in `fdatasync`, attach native tooling (gdb/perf/strace) to the
reported `carrier_tid` to go deeper.
