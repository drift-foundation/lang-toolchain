# Code review: `conc.sleep` double-registered its wake timer (Issue 3, blocking)

**Branch / commit:** working tree against `main` (0.31.82 → 0.31.83 in
`lang/versions.py`). ABI 14 unchanged.
**Severity:** RUNTIME_BUG. Blocks v1 keepalive certification for
`mariadb-rpc`. Customer report at
`~/src/pushcoin/work/managed-connection-spike/v1_implementation_blockers.md`
(Issue #3 in that file).
**Workaround:** none in user code — the bug is in the stdlib `sleep`
implementation itself.

## TL;DR

`std.concurrent.sleep` registered its wake timer twice — once
explicitly via `thread.reactor_register_timer(deadline, vt)` at the
Drift level, and again *internally* inside
`thread.vt_park_until(deadline)` (the C function
`drift_thread_park_until` at
`lang/language_runtime/posix/thread_runtime.c:1863` calls
`drift_reactor_register_timer` itself).

Both timers fired. The first unpark hit the VT in
`DRIFT_VT_PARKED` (enqueue path, harmless). The second hit it in
`DRIFT_VT_READY` (state already advanced) and took the fallback
branch that bumps `park_token`. The next `sleep` call entered with
the stale token, decremented it, and **early-returned without
parking**. Each cycle leaked one extra token; over multiple sleeps
the customer saw alternating-instant sleeps on main, and on spawned
VTs got either spin-loops (every sleep token-bypassed) or no
execution (depending on jitter).

Fix: one line removed from `stdlib/std/concurrent/concurrent.drift`
(and an explanatory multi-line comment so the next maintainer
doesn't add the register back). No C-side changes.

## Customer-visible repro (verbatim from the ask)

```drift
val vt = conc.spawn<type Int>(core.callback0(| | captures(share counter) => {
    while i < 100 {
        match conc.sleep(conc.Duration(millis = 100)) { ... }
        counter.fetch_add(1);
        i = i + 1;
    }
}));
conc.sleep(conc.Duration(millis = 550));
// Customer: counter is either 0 (VT didn't run) or ~5000 (spin loop).
// Expected: counter is ~5.
```

Across 5 customer runs: 3/5 spin-loop, 2/5 no-run — both manifestations
of the same underlying bug.

## Narrowing

The first thing I confirmed was that the bug is **not specific to
spawned VTs**. Four sequential `conc.sleep(100ms)` calls on main
exhibit:

```
sleep#1=100  ✓
sleep#2=0    ✗  (returns instantly)
sleep#3=100  ✓
sleep#4=0    ✗
```

Tracing `drift_thread_park_until` and `drift_thread_unpark` with a
gated `DRIFT_SLEEP_TRACE` printf revealed:

```
sleep#1: park_until token_in=0 → swap, park.
  unpark state=PARKED → enqueue (no token++)
  unpark state=READY  → FALLTHROUGH, token++=1
sleep#2: park_until token_in=1 → consume, return.
sleep#3: park_until token_in=0 → swap, park.
  unpark state=PARKED → enqueue (no token++)
  unpark state=READY  → FALLTHROUGH, token++=1
  unpark state=READY  → FALLTHROUGH, token++=2
sleep#4: park_until token_in=2 → consume (token=1).
```

Two key observations:

1. Each park-and-wake produces **two `unpark` calls** for the same
   VT. The first wins the state race (`PARKED` → enqueue, no
   token++); the second sees the already-advanced state (`READY`)
   and falls through to the `park_token++` branch.
2. The duplicates *accumulate* across cycles (sleep#3 fires
   `unpark` THREE times). Stale duplicates from prior cycles
   compound — the customer's spawned-VT loop saw the spin-loop
   manifestation precisely because, after a few cycles, *every*
   sleep had a pending token to consume.

## Root cause

`std.concurrent.sleep`:

```drift
var deadline = thread.now_ms() + d.millis;
var vt = thread.vt_current();
if vt == 0 {
    thread.vt_park_until(d.millis);
    return ...
}
thread.reactor_register_timer(deadline, vt);   // ← REDUNDANT
thread.vt_park_until(deadline);                // ← ALSO REGISTERS INTERNALLY
return ...
```

The C function `drift_thread_park_until` (line ~1863) calls
`drift_reactor_register_timer((uint64_t)deadline_ms, (uint64_t)vt)`
*before* it sets the VT to `PARKED` and swaps to the scheduler.
The Drift-level `thread.reactor_register_timer(deadline, vt)` call
duplicates that registration.

Confirmed by grepping all callers of `reactor_register_timer` in
stdlib: only this one Drift caller exists (other usages are
`reactor_register_io`, which is a separate intrinsic for I/O
events). The double-registration is a localized bug in `sleep`.

## Fix

One line removed from `stdlib/std/concurrent/concurrent.drift`:

```drift
-    thread.reactor_register_timer(deadline, vt);
     thread.vt_park_until(deadline);
```

Plus a multi-line comment at the call site explaining the
double-registration trap, so the next maintainer who looks at
`sleep` and thinks "shouldn't this call `reactor_register_timer`?"
finds the answer in the code rather than rediscovering the bug.

No C-side changes. The C runtime was correct — only the stdlib was
duplicating the registration.

### Why not change the C side instead?

Two options were possible:

- **(A)** Strip the internal registration from
  `drift_thread_park_until`, and require all callers to register
  beforehand.
- **(B)** Strip the redundant Drift-level call in `sleep`.

I chose (B) because:

1. The C function is named `park_until` — a "park until deadline"
   primitive that registers the wake itself is a coherent API
   shape. Splitting register-and-park across two calls is a
   strictly-more-error-prone interface.
2. `sleep` is the only caller of `reactor_register_timer` from
   Drift. Changing the C function would mean changing zero
   callers — i.e. just inverting where the bug was, not really
   fixing the interface.
3. (B) is a one-line stdlib change with no ABI implications. (A)
   would change the runtime contract.

## Regression tests

Both checked in:

- **`lang/tests/codegen/e2e/conc_sleep_sequential_main/`** — four
  sequential `sleep(100ms)` calls on main, each asserted to take
  80–500ms. Pre-fix: sleeps #2 and #4 measure ~0ms. Post-fix: all
  four measure ~100ms. This is the *root-cause* regression — pins
  the alternating-instant-sleep symptom.
- **`lang/tests/codegen/e2e/conc_sleep_in_spawned_vt/`** — the
  customer's exact shape. Spawn VT loops `sleep(100ms) +
  counter.fetch_add`, main sleeps 550ms, assert counter is in
  4..7. Pre-fix: counter is `0` (VT never ran) or thousands (spin
  loop). Post-fix: counter is `5`, deterministically across 5
  consecutive runs.

Pre-fix, both new tests fail with exactly the customer-reported
shape. Post-fix, both pass. Regression-first per project policy.

## Risk surface

- **Only the stdlib `sleep` function changes** — one removed line
  plus comments. No C runtime change. No MIR / codegen change. No
  ABI implication.
- The C function `drift_thread_park_until`'s contract is
  unchanged: it still registers the wake timer internally. Any
  other Drift code that calls `vt_park_until` directly without
  also calling `reactor_register_timer` was already correct;
  `sleep` was the only over-registering caller.
- The non-VT branch of `sleep` (`if vt == 0`) was unchanged — it
  was already correct, calling `vt_park_until(d.millis)` which
  routes to the bare-thread `nanosleep` path that takes a
  duration.

## Files touched

| File | Change |
|---|---|
| `stdlib/std/concurrent/concurrent.drift` | -1 line (the redundant `reactor_register_timer` call); +8 lines of explanatory comment |
| `lang/versions.py` | 0.31.82 → 0.31.83 |
| `docs/history.md` | full release entry, including root-cause narrative + alternative-fix rationale |
| `lang/tests/codegen/e2e/conc_sleep_sequential_main/` | NEW — root-cause regression (sequential main sleeps each ~100ms) |
| `lang/tests/codegen/e2e/conc_sleep_in_spawned_vt/` | NEW — customer's exact shape (spawn + counter, main sleeps 550ms, assert ticks ∈ 4..7) |

## Issues 1 and 2 from the same customer report

The mariadb team's report at
`~/src/pushcoin/work/managed-connection-spike/v1_implementation_blockers.md`
contains three issues. Issue 3 is the blocker and is fixed by this
slice. Status of the other two:

### Issue 1 (CORE_BUG): `arc.get().field` on non-Copy fields fails

**REPRODUCED** in minimal form, independent of `Arc`. The repro at
`/tmp/issue1_repro/main.drift`:

```drift
fn _peek_handle(h: &Handle) nothrow -> Int { return h.raw; }

fn main() nothrow -> Int {
    val w = Wrapper(inner = Inner(handle = Handle(raw = 42)));
    val n = _peek_handle(&w.get().handle);   // ← rejected
    //          ^^^^^^^^^^^^^^^^^^^
    // error: cannot copy value of type 'Handle' (use move <expr>)
    return n;
}
```

`Wrapper::get(self: &Wrapper) -> &Inner` returns a borrowed
reference. Chaining `.handle` on the borrowed result then trying
to take `&` of it is rejected as if it were a copy of the non-Copy
`Handle` field. The customer's workaround (bind `get()` to a named
local first, then project + borrow) compiles and runs:

```drift
val inner_ref: &Inner = w.get();
val n = _peek_handle(&inner_ref.handle);    // OK
```

This is the same family as the earlier returned-ref chain bug, but
for field projection rather than method chaining. The compiler's
checker / lowering for field projection on an rvalue ref-returning
receiver doesn't recognize that the projection should yield a
nested reference. Diagnosis-and-fix is a separate slice.

### Issue 2 (CORE_BUG): `captures(share x)` + later `move x` SSA crash

**REPORTED BY CUSTOMER; NOT REPRODUCED LOCALLY** in any minimal
shape I tried. Variants tested:

| Variant | Result |
|---|---|
| `Arc<Payload>`, `share x` in `var cb`, `move x` into local `val w = Wrapper(...)` | compiles |
| Same but constructing the wrapper as the return value (no `val w` intermediate) | compiles |
| Same but with closure body that reads `x.get().v` | compiles |
| Same but using `conc.spawn<T>` instead of a bare `Callback0` | compiles |

None of these reproduce the customer's reported "SSA: load before
store" ICE. The bug must depend on some structural condition my
repros don't hit — perhaps a particular sequence of moves in the
closure body, or a specific captured-state shape. Need a minimal
repro from the customer (their actual failing file from
`packages/mariadb-rpc/tests/spike/managed_connection_spike.drift`
isn't in the snapshot of `~/src/pushcoin/` I can see — only the
bookkeeper code is there, which uses `captures(share app)` but
doesn't trigger the bug).

**Recommended next step**: ask the customer to extract the
minimum-failing function from the mariadb spike into a standalone
`.drift` file and post it. Without that, I can't pin the bug at
the SSA pass.

The customer noted a separate doc/syntax point — closure syntax
`| |` (with a space) vs `||` (no space). That's a parser polish
issue, not a blocker; tracked but not in this slice.

### Audit follow-up (raised in review of this fix)

The same double-timer-registration pattern is also present for
**timed I/O waits** — `stdlib/std/net/net.drift:166-167`,
`stdlib/std/io/io.drift:732-733`, and
`stdlib/std/concurrent/concurrent.drift:1255-1257` all do:

```drift
thread.reactor_register_io(fd, interest, vt, deadline_ms);
thread.vt_park_until(deadline_ms);
```

C's `drift_reactor_register_io` registers a timer when
`deadline_ms > 0`
(`lang/language_runtime/posix/thread_runtime.c:2293-2295`), and
`vt_park_until` then registers another. Less
customer-visible than the `sleep` bug — manifests as "I/O
timeout returned earlier than expected" rather than 0ms sleeps,
which is easy to misattribute to network/disk jitter. Not folded
into this slice to keep the patch narrow around the reported
blocker; queued as the next runtime audit item before assuming
the timer subsystem is clean.

## Ship readiness

Issue 3 (blocker) is fixed, regression-pinned both ways, no
external risk. I'd land this. The timed-I/O audit finding is
tracked separately and is not in scope here.
