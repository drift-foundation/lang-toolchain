# Code review: timed-I/O double-timer-registration fix (0.31.86)

**Branch / commit:** working tree against `main` (0.31.85 → 0.31.86 in
`lang/versions.py`). ABI 14 unchanged.
**Severity:** RUNTIME_BUG. Hit by mariadb team in production —
`main.sleep(550)` returned in ~1ms after `rpc.connect()`.  Closes the
audit follow-up flagged at 0.31.83 cert.
**Status:** original fix + tightened regression coverage (post code
review by K).  Ready to land.

## TL;DR

Same shape as the 0.31.83 `conc.sleep` fix, just for I/O waits.  C's
`drift_reactor_register_io` registered a wake timer when
`deadline_ms > 0`; `drift_thread_park_until` then registered the same
timer again.  In the **timeout case**, both stale timers fire in the
same reactor iteration, the second `drift_thread_unpark` hits the
already-advanced `READY` state, falls through to `park_token++`, and
the stale token short-circuits the VT's next `conc.sleep`.

Fix is a two-line C change: remove the internal
`drift_reactor_register_timer` call from `drift_reactor_register_io`.
`drift_thread_park_until` is now the single timer-registration
authority.  Three stdlib call sites (`net.drift`, `io.drift`,
`concurrent.drift`) already do the right thing.

## Why this needed a second round

The first round of regressions used `net.connect` followed by sleep,
then was tightened to `accept` with a fast connector.  **Neither
exercised the buggy path** — caught by K in code review:

- `net.connect` (stdlib) takes the C fast path through
  `drift_connect_to_addr` and **skips `_block_on_io` entirely**.
- A fast I/O wake (event arrives before deadline) goes through the
  reactor's I/O dispatch path, which **proactively removes every
  timer for the woken VT** from the timer list before unparking — the
  duplicate gets silently cleaned up.

Only the **timeout case** (deadline expires before any I/O event)
leaves both duplicates in the list at collect-timers time, producing
the duplicate unpark.

## Trigger condition (after the second round of investigation)

```
t=0   : main calls accept(timeout=100ms).
        _block_on_io registers I/O watch + timer T1@100 (C side) +
        timer T2@100 (vt_park_until).  Main parks.
t=100 : Accept times out.  Both T1 and T2 are in the timer list.
        drift_reactor_collect_timers returns both ready.  Dispatch
        loop calls drift_thread_unpark for each:
          - 1st unpark: state PARKED → READY + enqueue.  No token++.
          - 2nd unpark: state READY (advanced).  FALLTHROUGH branch.
            park_token++.  token = 1.
        Main resumes via worker.  Accept returns Err (timeout).
t=100+: main calls conc.sleep(100ms).
        Pre-fix: park_token > 0 → decrement + early-return.
                 Sleep returns in ~0ms.
        Post-fix: park_token == 0 → real park for ~100ms.
```

Confirmed empirically via instrumented runtime trace (added/removed
`getenv("DRIFT_IO_TRACE")` probes in `drift_thread_park_until` and
`drift_thread_unpark`).  The TIMEOUT case is the precise trigger;
the fast-I/O case is silently clean.

## Customer's actual failure mode

mariadb team's `rpc.connect()` internally did a timed I/O read.  The
deadline was computed against the overall `rpc.connect` timeout, not
the individual read.  When the read hit that deadline (or hit a
race with a real I/O event firing near the deadline), the stale
token was left.  Main's subsequent `sleep(550)` then instant-returned.

## Fix

Two-line C change at `lang/language_runtime/posix/thread_runtime.c`,
inside `drift_reactor_register_io`:

```c
// BEFORE:
if (deadline_ms > 0) {
    drift_reactor_register_timer(deadline_ms, vt);
}

// AFTER:
(void)deadline_ms;
```

Plus an explanatory comment naming the trigger condition and pointing
at the symmetric 0.31.83 fix.

`deadline_ms` is now a vestigial parameter on `reactor_register_io`
(consumed only by the caller's subsequent `vt_park_until(deadline_ms)`
call).  Worth dropping the argument in a future cleanup; out of scope
here to keep the patch narrow.

No Drift-side changes.  The three stdlib call sites
(`stdlib/std/net/net.drift:166`, `stdlib/std/io/io.drift:732`,
`stdlib/std/concurrent/concurrent.drift:1255`) already do the right
thing.

## Regression test

`lang/tests/codegen/e2e/conc_sleep_after_timed_io_wait/` — exercises
the **timeout** trigger directly:

```drift
match net.accept(&listener, conc.Duration(millis = 100)) {
    Ok(_) => { return 2; }      // unexpected
    Err(_) => { }               // expected: timeout
}
val t0 = time.now_monotonic();
conc.sleep(conc.Duration(millis = 100));
val elapsed = time.elapsed_ms(&t0);
// assert 80 <= elapsed <= 500
```

**Regression-first proven**: reverted only the C-side fix locally,
ran the test → exits 10 ("sleep too short").  Restored the fix → exits
0 ("ok").  The flip between the two states is the proof of coverage.

The earlier (deleted) `conc_keepalive_vt_after_net_connect` test had
the same coverage gap and was redundant after the precise trigger
was identified.  Deleted in this slice.

## Risk surface

- **Only the C-side `drift_reactor_register_io` changes.** No Drift
  code, no MIR/codegen, no ABI implication.
- The fix is strictly a *removal* of duplicate work — every I/O wait
  still has the timer registered (by `drift_thread_park_until`).
  Just once instead of twice.
- The vestigial `deadline_ms` parameter on `reactor_register_io` is
  intentionally left in place to keep the patch narrow.  Future
  cleanup can drop it; cost of leaving it is zero.

## Files touched

| File | Change |
|---|---|
| `lang/language_runtime/posix/thread_runtime.c` | 2-line fix + explanatory comment in `drift_reactor_register_io` |
| `lang/tests/codegen/e2e/conc_sleep_after_timed_io_wait/` | NEW — timeout-case regression (replaces the earlier insufficient-coverage tests) |
| `lang/versions.py` | 0.31.85 → 0.31.86 |
| `docs/history.md` | full entry incl. trigger-condition narrative, customer failure mode, both code-review catches (insufficient-coverage + process learning) |

## Process learnings called out in history.md

1. **Audit items don't age well.**  At 0.31.83 cert I flagged the I/O
   wait's parallel pattern as the next audit item; the mariadb team
   hit it in production three days later.  When an audit item names a
   specific known-broken pattern in customer-facing code, the cost of
   letting customers hit it exceeds the cost of widening the original
   patch.

2. **For runtime bugs, prove the regression by reverting.**  A test
   that passes both pre-fix and post-fix proves nothing.  The first
   two rounds of regression tests for this fix passed against the
   unfixed toolchain because they didn't exercise the buggy path.
   Caught by K in code review.  Filed as the second learning: revert
   the fix locally, run the test, confirm it FAILS pre-fix and
   PASSES post-fix.  Only then has coverage been established.

## Out of scope / follow-ups

- Drop the vestigial `deadline_ms` parameter on
  `thread.reactor_register_io` (Drift side) and
  `drift_reactor_register_io` (C side) in a future cleanup slice.
  Three Drift call sites + one C function signature.  Worth a
  separate slice; not blocking.
- The customer's other open issues (`arc.get().field` on non-Copy
  fields) are not part of this fix — separate compiler slice.
