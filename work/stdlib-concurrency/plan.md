# std.concurrent — supervised-async primitives (channel + is_complete)

**Status:** plan, not implemented.
**Driver:** PushCoin bookkeeper team request, `pushcoin/work/stdlib-concurrency-request.md` (revised 2026-05-26).
**Companion bug track:** VirtualThread drop lifetime UAF (see §5 — separate slice).

## 1. Scope agreement

The app-team report (revised) is a valid consumer-pressure statement:
supervised async workers must retain handles, be reapable, support bounded
drain, and surface failure observably. `MpscQueue<Handle<T>>` is non-blocking
+ Handle-typed and does not fit; `Mutex`+`Condvar` works but is per-service
boilerplate. The K/toolchain side owns the API contract and implementation
plan — this file is that plan.

The revised request collapses the supervisor model onto **one authoritative
lifecycle path**: retained `VirtualThread<T>` handles plus a typed
completion channel. Shutdown drops the sender `Arc` and bounds reaper/drain
behavior on the consumer side. `WaitGroup` is **no longer requested** —
parallel completion accounting via WaitGroup could diverge from
retained-handle/channel state, so removing it improves correctness, not
just surface area.

Accept the gap statement as given. The two additions below are additive
and require no runtime/ABI work (everything lives in
`stdlib/std/concurrent/concurrent.drift` on top of the existing `thread.*`
substrate and the now-stable `Condvar`).

## 2. Slice contents

**Primary:** `channel<T>` — blocking typed channel with close semantics.
This alone is sufficient for the supervised-worker design: workers send
completion records to a reaper VT through `Arc<Sender<T>>`; shutdown drops
the last `Arc<Sender<T>>`; the reaper's `recv` returns `CLOSED` and the
process exits its drain loop.

**Secondary:** `is_complete` on `VirtualThread<T>` / `Future<T>` — for the
optional polling-style reaper variant. Not required if the consumer uses
the channel-driven shape; included because the substrate
(`thread.vt_is_completed`) already exists and surfacing it is ~30 LOC.

Both can land in one PR. If `is_complete` slips for any reason, the
channel piece is independently complete and ships on its own.

### 2.1 `channel<T>` — blocking typed channel with close

**Surface:**

```drift
pub fn channel<T>() nothrow -> (Sender<T>, Receiver<T>)

pub struct Sender<T>      // move-only; share via Arc<Sender<T>>
pub struct Receiver<T>    // move-only; single consumer

impl<T> Sender<T>:
  fn send(&self, var v: T) nothrow -> Result<Void, ConcurrencyError>

impl<T> Receiver<T>:
  fn recv(&mut self) nothrow -> Result<T, ConcurrencyError>
  fn recv_timeout(&mut self, d: Duration) nothrow -> Result<T, ConcurrencyError>
```

**No T bound in v1.** Earlier draft of this plan called for
`require T is Share`. That was wrong: `Share` permits shared-owner
cloning; channel payloads are ownership transfers (`var v: T` is moved
in by `send`, moved out by `recv`), not clones. A Share bound would
exclude ordinary move-only completion records — the exact shape the
bookkeeper team sends. The correct gate is a future `Send` trait that
asserts a value is safe to *transfer* across VTs. Until that contract
is enforced language-wide, `channel<T>()` publishes with no bound; once
`Send` lands the signature gains `require T is Send` as a follow-up.
Document the eventual-Send intent in the doc-comment so downstream code
isn't surprised by the later tightening.

**Internals:** one `Arc<ChannelInner<T>>` shared by Sender and Receiver.
**Closed state lives inside the predicate mutex,** not in separate
atomics — the prior draft's `AtomicBool` close flags allowed a sender
to pass the closed check, lose the race to receiver destruction, then
enqueue an undeliverable value:

```drift
struct ChannelInner<T> {
    state: Mutex<ChannelState<T>>,
    not_empty: Condvar,
}

struct ChannelState<T> {
    queue: Array<T>,
    sender_closed: Bool,
    receiver_closed: Bool,
}
```

`send`, `recv`, sender destruction, and receiver destruction all
linearize through `state.lock()`. Inside that lock the operation reads
both the closed flags and the queue and acts atomically.

Unbounded buffer for v1. Their volume is one send per completed worker;
bounded variant defers until a second consumer asks. No backpressure
semantics need to be committed to yet.

**Close semantics (under the predicate mutex):**

- `Destructible for Sender<T>` takes `state.lock()`, sets
  `sender_closed = true`, drops the lock, then
  `not_empty.signal_all()` to wake any receiver blocked in `recv`.
  With share-via-`Arc<Sender<T>>`, the destructor fires exactly when
  the last sender reference drops.
- `Destructible for Receiver<T>` takes `state.lock()`, sets
  `receiver_closed = true`, drains `queue` into a local under the lock
  (each popped `T` runs its own `Destructible` after release), drops
  the lock. **No signal is emitted on this path in v1:** there are no
  blocked senders to wake (unbounded buffer → `send` never parks), and
  the single consumer destroying itself cannot be waiting on its own
  Condvar. The earlier draft's "wake blocked senders on Receiver drop"
  language echoed the app team's request verbatim, but with an
  unbounded v1 there are no blocked senders. The actual required
  behavior is: future/concurrent `send` calls observe `receiver_closed`
  and return `CLOSED`; queued undelivered values are destroyed in the
  drain step above. A `not_full` Condvar + signal_all gets added if/
  when a bounded variant ships.
- `recv` takes the lock; while `queue.empty() && !sender_closed`, calls
  `not_empty.wait(&mut g)`; if `queue.len > 0` pops and returns
  `Ok(v)`; otherwise (queue empty && sender_closed) returns
  `Err(CLOSED)`.
- `send` takes the lock; if `receiver_closed`, returns `Err(CLOSED)`
  without enqueueing (the moved-in `var v: T` is dropped via
  `Destructible`); otherwise `queue.push(move v)` and
  `not_empty.signal_one()` after release.

**Open questions deferred (intentionally):**
- No `try_send` / `try_recv` until a consumer asks. Current shape covers
  the bookkeeper pattern (blocking-recv reaper + send-from-worker).
- No `len()` accessor — opens races if exposed; not needed by the team.

**Estimated cost:** ~250 LOC Drift.

### 2.2 `is_complete` (secondary)

**Surface:**

```drift
impl<T> VirtualThread<T>: pub fn is_complete(&self) nothrow -> Bool
impl<T> Future<T>:        pub fn is_complete(&self) nothrow -> Bool
```

**Semantics — terminal-state predicate, not "live-and-pollable":**

```drift
pub fn is_complete(self: &VirtualThread<T>) nothrow -> Bool {
    // joined → terminal (result already extracted; no further work)
    if self.joined { return true; }
    // submit-error → terminal (task never ran; deferred error will surface on join)
    if self.submit_error != 0 { return true; }
    // live handle: delegate to runtime
    return thread.vt_is_completed(self.handle) != 0;
}
```

Semantics: terminal-state predicate. "Is there any more work to wait
for on this handle?" Joined handles and submit-error handles are
terminal; returning `false` from them would produce a polling
false-negative that loops forever in a reaper sweep that polls
`is_complete` and only joins on `true`.

Cancellation *request* alone (`self.cancelled == true` while
`vt_is_completed` still returns 0) is **not** completion — the
runtime hasn't finished unwinding yet. Falls through to
`vt_is_completed`, which is the correct gate.

Note: the `Future<T>` shim must delegate to the corrected
`VirtualThread<T>.is_complete`, not to `is_done` (which is
specifically the joined-flag accessor). Keep `is_done` as-is for
backwards-compat with `FutureGroup.join_any` callers.

Substrate (`thread.vt_is_completed`) already used by `join_timeout` and
`FutureGroup.join_any`. Pure surface area.

**Note on the drop-lifetime bug interaction:** `is_complete` is safe in
the documented use (handle retained until `join` runs to completion).
The drop UAF (§5) only fires when the handle is dropped while the task
is still running — i.e., the misuse case the team is explicitly
avoiding. `is_complete` is therefore not blocked by §5.

**Estimated cost:** ~30 LOC.

## 3. Sequencing (historical — captured at classification time)

> **Closed.** The result-ownership LANGUAGE_BUG referenced below was
> resolved at 0.33.1 (see §5).  The sequencing rules in this section
> describe the policy that gated the supervisor-primitives slice at
> classification time; they are retained for traceability.  The
> `is_complete` / `channel<T>` slice is now unblocked.

**Hard ordering, per repo regression-first policy. No deferral was
requested.** The VT-drop UAF was an active LANGUAGE_BUG when this
sequencing was written (see §5).  Because the channel slice was
specifically designed to *supervise* `VirtualThread<T>` handles, its
implementation and tests had to be built on a sound handle-lifetime
contract; shipping supervisor primitives while
drop-of-an-unjoined-running-task was known-unsafe would have given
the app team an attractive API built over an unresolved
memory-safety defect.

**Steps in order:**

1. **Classify §5 as LANGUAGE_BUG now.** Not "when the repro lands";
   the repro pins the defect, it does not classify it. Diagnosis at
   §5.1 traces a verifiable UAF between `concurrent.drift:1442` and
   `thread_runtime.c:2087`. Add a project memory entry tagged
   LANGUAGE_BUG so `[[]]` cross-refs from future work land here.
2. **Audit `doc/refactor_triggers.md`** for any applicable trigger
   (per the "scan refactor_triggers when starting any LANGUAGE_BUG
   fix" rule in `AGENTS.md`). If the VT lifetime / Destructible-for-
   VT shape matches a registered trigger, the deliverable escalates
   to that refactor rather than a minimal patch.
3. **Add the runtime-instrumented regression (§5.2).** Minimal:
   drop of an unjoined running `VirtualThread<String>` must not
   access freed result storage. Lands under the existing
   memcheck/driver harness. Confirm it fails on `HEAD` before any
   fix — that's the contract pin.
4. **Implement option (d) — Drift-side `Arc<Mutex<ResultState<T>>>`.**
   This was selected by the ownership-matrix review on 2026-05-26 as
   the implementation path. Options (a)/(b) are retained in §5.3
   only as rejected analysis / fallback; option (c) was removed as
   unsound. The fix lives entirely in
   `stdlib/std/concurrent/concurrent.drift`; no runtime ABI change.
5. **Implement the runtime/toolchain fix; confirm the regression
   flips to passing.** Carries its own `DRIFTC_VERSION` bump and
   `doc/history.md` entry (see §6); if it changes the
   compiler/runtime boundary, also `DRIFT_RT_ABI_VERSION` bump and
   ABI-mismatch regression updates per `doc/design/drift-lang-abi.md`.
6. **Then proceed with the §2 stdlib slice** (channel<T> primary,
   `is_complete` secondary). Send back to the app team at this
   point for contract confirmation against the now-settled surface.

**Secondary ordering vs. other in-flight work:**
- §2 lands **after** the inline-match cache-isolation work currently
  in flight (`project_inline_match_env_get_string_leak`). No
  dependency, but the bookkeeper team is the smoke surface for
  `std.concurrent`; stacking new primitives on top of an unsettled
  cache contract would muddy attribution if anything regresses.
- Independent of the planned ledger-cache-safety slice
  (`project_ledger_cache_safety_slice`) — different layer.

## 4. Test plan

Functional API tests live under `lang/tests/codegen/e2e/`, matching
the existing `concurrent_*` convention (e.g.
`concurrent_cancel_after_completion_no_effect`,
`concurrent_builder_chain`, `conc_sleep_in_spawned_vt` already in
that directory). One e2e directory per test, each containing the
`.drift` program + the expected-output / expected-leaks fixtures the
harness consumes.

The VT-drop UAF regression (§5.2) lives in the runtime-instrumented
memcheck/driver path, not in the codegen e2e suite — ASAN/Valgrind
instrumentation is the load-bearing assertion.

**Channel (under `lang/tests/codegen/e2e/`):**
- `concurrent_channel_basic` — single producer, single consumer,
  send-then-recv; recv blocks until send.
- `concurrent_channel_close_sender` — drop last `Arc<Sender>`; recv
  returns CLOSED after queue drains.
- `concurrent_channel_close_receiver_destructs_queued` — drop Receiver
  with values in queue; count `Destructible::destroy` calls
  (one per queued value).
- `concurrent_channel_recv_timeout` — empty channel +
  `recv_timeout(50ms)` → TIMEOUT; populated channel + recv_timeout →
  value.
- `concurrent_channel_multi_producer` — N workers behind
  `Arc<Sender>`, single reaper; closes cleanly when all senders drop.
- `concurrent_channel_move_only_T` — T is a custom non-Copy,
  non-Share struct; verify send/recv works and `Destructible` runs
  exactly once per value. **Pins the no-`T: Share`-bound decision.**
- `concurrent_channel_close_recv_race` — pin the linearization fix
  from §2.1. Many senders racing receiver destruction. Each producer
  builds `N` owned payloads up front, each tagged with a unique id;
  every payload runs a custom `Destructible` that records `(id,
  reason)` into a shared atomic ledger where `reason ∈
  {received, drained_on_close, rejected_send_closed}`. Total
  ledger entries == total payloads created, with each id appearing
  exactly once. The invariant under test:
    * payloads whose `send` returned `Ok` end up either `received`
      (consumer pulled them off before Receiver drop) or
      `drained_on_close` (Receiver's destructor drained them);
    * payloads whose `send` returned `Err(CLOSED)` end up
      `rejected_send_closed` (the rejected-send path destroys the
      moved-in `var v: T`, per §2.1);
    * no payload is stranded in an unowned queue slot or
      double-counted.
  Replaces the earlier draft's "destructor count == sends_ok" oracle,
  which was wrong because the rejected-send path also runs
  `Destructible` per §2.1. N-iteration stress variant for race
  coverage.

**VirtualThread.is_complete (under `lang/tests/codegen/e2e/`):**
- `concurrent_vt_is_complete_live` — spawn / before completion
  `is_complete` is false / poll until true / `join` returns Ok.
- `concurrent_vt_is_complete_after_join` — spawn / join Ok /
  `is_complete` returns **true** (terminal). Pins §2.3.
- `concurrent_vt_is_complete_submit_error` — spawn against a saturated
  ReturnBusy executor / `is_complete` returns **true** before any
  join attempt. Pins §2.3.
- `concurrent_vt_is_complete_cancel_pending` — pin the rule
  "cancellation request alone is not completion." Determinism
  requires a started-task handshake plus a controllable blocker, not
  just `cancel()` followed by an immediate check (the task may
  complete cleanup before the poll, in which case `true` would be
  correct).
  Test shape:
    1. The spawned task signals a "started" Condvar/`AtomicBool`
       and then parks on a second condition the test controls
       (a release Condvar, or a `recv` on a separate
       `channel<Unit>` the test holds the sender for).
    2. The test waits for the "started" signal so the worker is
       guaranteed past `started = 1` and inside the blocker.
    3. The test calls `cancel()`. The blocker does not yield to
       cancellation on its own (it's a `Condvar.wait` or `recv` —
       neither is a cooperative safe point that the runtime auto-
       wakes on cancel).
    4. While still blocked: assert `is_complete() == false`.
    5. The test releases the blocker (signal Condvar / send on the
       channel). The task body checks the cancel flag at its post-
       blocker safe point and returns.
    6. Poll `is_complete()` until it returns **true**; then `join()`
       returns `Err(CANCELLED)`.
  This pins both halves of the contract: false while the runtime
  hasn't marked the task complete, and true once it has — neither
  half is observed by accident.

**VT-drop UAF regression (runtime-instrumented, not codegen e2e):**
- Lives under the existing memcheck/driver harness (sibling to the
  Valgrind-instrumented fixtures that exercise `std_io_*` and
  `mutex_guard_condvar_*`). Spawn task with a 50ms `conc.sleep` body
  returning a `String`; drop the `VirtualThread<String>` immediately;
  main sleeps 200ms to let the worker write its result. ASAN/Valgrind
  catches the heap-use-after-free on `buf_for_cb`. Lands as part of
  step 1 of §3's sequencing (regression-first; the §2 e2e tests
  cannot land until this regression has a green fix).

(No WaitGroup tests in this slice — `WaitGroup` was removed from the
requested surface per the revised consumer request; see §1.)

## 5. VirtualThread result-ownership LANGUAGE_BUG — RESOLVED 0.33.1

**Status: closed 2026-05-26 at compiler version 0.33.1.** All eight
regressions (R1-R8) flipped from failing to passing under both the
normal lane and the DRIFT_MEMCHECK=1 valgrind lane (R8 is a pure
liveness defect verified uninstrumented with a tight timeout).
Option (d) — Drift-side `Arc<Mutex<ResultState<T>>>` shared between
handle and cb thunk — landed as specified in §5.3, plus three
adjacent `join_any`-only fixes:
- R6: replace raw `mem.read` peek with `mem.ptr_at_ref` + deref-Copy
  so retain/release-bearing `T: Copy` types — `String` foremost —
  are read with proper Copy semantics.
- R7: gate the peek on `ResultState.initialized`; delegate
  uninitialised slots (cancel-before-start) to `.join()` for proper
  cancellation cleanup.
- R8: detect `submit_error != 0` at the top of each per-future probe
  and delegate to `.join()`, eliminating the `vt_is_completed(0)`
  infinite-poll hang.
ABI unchanged at 14; no runtime modifications.  See
`doc/history.md` 2026-05-26 for the release entry and
`doc/design/drift-concurrency.md` for the public protocol
documentation.

Sections 5.1-5.3 below are the diagnosis/design that fed into the
fix — retained for historical reference.  When this material was
first written, the LANGUAGE_BUG was active and the classification
language below reflects that moment in time; the bug has since been
closed by the fix summarized above.

**Status at classification (2026-05-26):** active LANGUAGE_BUG (not
"to be classified after repro"). The diagnosis below is sufficient
to classify; the regression test pins the contract but does not gate
the classification. Per the sequencing rule in §3, this LANGUAGE_BUG
fix lands **before** the §2 stdlib slice.

**First investigation step before writing the regression:** audit
`doc/refactor_triggers.md` for any registered trigger that matches
the VT-lifetime / `Destructible-for-VirtualThread<T>` shape. If a
matching trigger exists, the deliverable escalates to that refactor
per the `AGENTS.md` rule; if not, file the audit result ("scanned;
no match") in the eventual `history.md` entry alongside the fix.
This is the same discipline applied to every LANGUAGE_BUG fix in
recent history (see the "Refactor-triggers scan" lines in
`doc/history.md`).

### 5.1 Repro path

1. `spawn<T>(cb)` allocates `buf = mem.alloc_uninit<T>(1)`, captures
   `buf_for_cb` into the callback, returns a `VirtualThread<T>` holding
   `result = buf_for_join` (same ptr/cap as `buf_for_cb` — aliased).
2. Executor worker dequeues the task; `h->started = 1`; cb begins.
3. User code drops the `VirtualThread<T>`. `Destructible::destroy`
   (`concurrent.drift:1442`–`1457`) calls `thread.vt_drop(handle)` then
   unconditionally `mem.dealloc<T>(buf)`.
4. `drift_thread_drop` (`thread_runtime.c:2087`–`2113`) only joins/
   destroys when `is_completed` OR (`!is_started && exec == NULL`). For
   started-and-running, it sets `cancelled = 1`, broadcasts cv,
   **returns immediately without joining**.
5. Worker continues executing cb; `cb.call()` eventually runs
   `mem.write(&mut buf_for_cb, 0, move v)` → write to freed memory.

### 5.2 Regression suite — result-ownership matrix

The bug class is broader than just R1 (drop-while-started UAF).
Design review on 2026-05-26 grew the matrix in stages: the first
expansion (R2-R4) uncovered three more cases in the same
result-ownership protocol family; R5 was added as the
`join_timeout` analogue of R4; R6/R7/R8 surfaced during
implementation review as an adjacent `FutureGroup.join_any` cluster
that share the same touched file but not the same root.  The
landed fix closes all eight; none can be
treated as out of scope. All eight reproduced deterministically on
the pre-fix tree (R1-R7 under valgrind memcheck; R8 as an
uninstrumented liveness hang against a tight timeout).

**R1 — drop-while-started UAF (the originally reported bug).**
Pinned at `lang/tests/driver/test_vt_drop_started_running_uaf.py`.
Spawn task that signals `started`, parks in `conc.sleep`, then
returns its owned `String`. Supervising VT spin-waits for `started`,
drops the unjoined handle. vt_drop broadcasts cv; cb wakes; thunk
writes `String` into the buffer the Drift destructor already freed.
Observed on HEAD: 2 `Invalid write of size 8` reports into a 16-byte
freed block (`sizeof(DriftString)` confirmed).

**R2 — submit-error drop double-free.**
Pinned at `lang/tests/driver/test_vt_result_ownership_matrix.py`
(`test_r2_submit_error_drop_double_free`).
`thread.exec_submit_test_override(1)` forces every submit to fail.
`spawn` takes the submit-error branch at
`stdlib/std/concurrent/concurrent.drift:1136-1144`, deallocates the
buffer, drops the runtime handle, returns a VT with
`submit_error = 1` / `handle = 0`. Dropping that VT without
`.join()`: `Destructible::destroy` (concurrent.drift:1442) skips
vt_drop (handle == 0) but unconditionally
`mem.dealloc<T>(buf)` — the same 16-byte block freed twice.
Observed on HEAD: valgrind `Invalid free()`; 17 allocs / 18 frees
(one extra free).

**R3 — completed-unjoined drop leaks unobserved owned result.**
Pinned at `test_r3_completed_unjoined_drop_no_leak`.
Spawn task that returns `"a" + "b"` (heap-allocated via
`drift_string_concat`; literal `.clone()` is a refcount no-op against
static memory and does not exhibit the bug — the test must use
concat or another heap-producing form). Wait 200ms for completion,
then drop without `.join()`. `Destructible::destroy` calls
`mem.dealloc<T>(buf)` — which lowers to `drift_free_array(ptr)` at
`lang/codegen/llvm/llvm_codegen.py:9956-9963` (pure `free`,
layout-only, **no T destructor run on the buffer contents**).
The published String value's heap body leaks. Observed on HEAD:
67 bytes definitely lost from `drift_string_concat`.

**R4 — cancel-then-publish join-CANCELLED leaks the discarded result.**
Pinned at `test_r4_cancel_publish_join_cancelled_no_leak`.
Spawn task that signals `started`, parks in `conc.sleep(50ms)`, then
unconditionally constructs and returns `"a" + "b"`. Supervisor waits
for `started`, calls `vt.cancel()` (succeeds because cb is parked
and `completed == 0`; sets `self.cancelled = true`). The cb wakes
from sleep, has no post-park cooperative cancel check, builds the
String, publishes it. `vt.join()` then takes the cancellation branch
at `stdlib/std/concurrent/concurrent.drift:1314-1325` which calls
`vt_join` + `mem.dealloc(buf)` **without `mem.read(v)`**. The
published String body is leaked. Observed on HEAD: 67 bytes
definitely lost from `drift_string_concat` (post-cancel publish).

**R5 — same as R4 but via `join_timeout()` instead of `join()`.**
Pinned at `test_r5_cancel_publish_join_timeout_cancelled_no_leak`.
Cancellation branch of `join_timeout` duplicates the same
dealloc-without-read shape; observed on HEAD: 62-67 bytes definitely
lost.

**R6 — `FutureGroup<T>::join_any()` double-release for `T: Copy`
with retain/release semantics.**
Pinned at `test_r6_future_group_join_any_string_double_release`.
`FutureGroup<T>` requires `T: core.Copy`.  In Drift, **`Copy` does
not imply "no destructor":** `String` is `Copy` with retain/release
refcount semantics.  The legacy `join_any` used a raw `mem.read` as
a non-consuming peek and left `initialized = true`, intending the
future to be re-observable by `join_all`.  For `T = String` this
moves the single ownership stake out of the slot while the buffer
still claims to own it; the subsequent `join_all` (or future
destruction) double-releases the same DriftString backing storage
and the process aborts in `drift_string_release`'s refcount-underflow
guard.  Observed on HEAD: SIGABRT after 27 valgrind errors.

**R7 — `FutureGroup<T>::join_any()` reads uninitialised result
storage for a future cancelled before its callback started.**
Pinned at
`test_r7_future_group_join_any_cancelled_before_start_no_uninit_read`.
Worker drops the cb in the `cancelled && !started` pickup branch
and marks `completed = 1`, but the result slot was never written
(`ResultState.initialized = false`).  Legacy `join_any` treats
`vt_is_completed != 0` as proof of publication and `*ref`s
uninitialised slot bytes; for `T = String` memcheck catches this
as "Conditional jump or move depends on uninitialised value(s)"
inside `drift_string_release` on the returned garbage value.

**R8 — `FutureGroup<T>::join_any()` hangs on submit-error future.**
Pinned at
`test_r8_future_group_join_any_submit_error_no_hang`.
A future whose submission failed carries `submit_error != 0` and
`handle == 0`.  `thread.vt_is_completed(0)` returns 0 forever; the
polling loop has no terminal check for this case and parks 1 ms
between sweeps indefinitely.  No memory-safety shape — pure
liveness defect.  Pinned via uninstrumented run with 10 s timeout.

**Matrix table:**

|     | scenario                                              | mechanism                                       | observed signature                                  |
| --- | ----------------------------------------------------- | ----------------------------------------------- | --------------------------------------------------- |
| R1  | drop while cb still running                           | dealloc-before-write race                       | UAF: 16 bytes freed then written by cb thunk        |
| R2  | drop with submit_error != 0 (handle == 0)             | spawn already dealloc'd buf                     | double-free of 16-byte block                        |
| R3  | drop after cb completed, never joined                 | layout-only dealloc skips T drop                | 67-byte heap-String body leaked                     |
| R4  | cancel after start; cb publishes; join → CANCELLED    | join cancel branch skips read                   | 67-byte heap-String body leaked                     |
| R5  | same as R4 but via `join_timeout` cancelled branch    | same shape duplicated in join_timeout           | 62-67-byte heap-String body leaked                  |
| R6  | `FutureGroup<String>::join_any` then `join_all`       | raw `mem.read` peek skips Copy retain on String | SIGABRT in `drift_string_release` (double-release)  |
| R7  | `FutureGroup<T>::join_any` on cancel-before-start     | `vt_is_completed` ≠ "published"; `*ref` uninit  | uninit-read in `drift_string_release` on garbage T  |
| R8  | `FutureGroup<T>::join_any` with submit-error future   | `vt_is_completed(0)==0` forever; no terminal    | infinite poll (10s timeout fires)                   |

R1-R5 share the central root: the result-buffer ownership protocol
between `spawn`, `Destructible::destroy`, `join` (Ok and Cancelled
branches), `join_timeout` (Ok and Cancelled branches), and the cb
thunk was not coherent. R6/R7/R8 are an adjacent cluster localized
to `FutureGroup.join_any` (Copy ≠ no-destructor gotcha; "completed"
≠ "published" gotcha; no terminal handling for handle==0). The
runtime side (`drift_thread_drop`) is not in fact the root cause
for any of them — it's the Drift-side protocol in
`stdlib/std/concurrent/concurrent.drift`.

### 5.3 Fix shape — option (d) preferred; investigate before any ABI expansion

The previous fix-shape framing assumed the fix must occur in
`drift_thread_drop` (i.e., on the runtime side). Design review on
2026-05-26 reframed this: **the result-buffer ownership protocol is
entirely Drift-side state in `std.concurrent`**. The runtime never
touched the buffer in the first place — `spawn` allocates it,
`Destructible::destroy` frees it, `join` reads-then-frees it.
Fixing the protocol on the Drift side avoids scheduler deadlock risk
and adds no runtime ABI surface.

Two of the original four matrix cases (R2, R3) involve no runtime
cooperation at all — they're pure Drift-side ownership bugs. Even
R1 and R4 (which do involve cb / cancellation race) can be resolved
purely in Drift by sharing the buffer state between the VT handle
and the cb thunk through a synchronized container.  (R5 mirrors
R4 in `join_timeout`; R6/R7/R8 are the adjacent
`FutureGroup.join_any` cluster, all Drift-side as well.)

**Option (d) — Drift-side `Arc<Mutex<ResultState<T>>>` (preferred,
investigate first):**

Replace `VirtualThread<T>.result: RawBuffer<T>` with a synchronized
state record co-owned by the handle and the cb thunk:

```drift
struct ResultState<T> {
    buf: RawBuffer<T>,
    initialized: Bool,    // cb wrote a T into buf
    released: Bool,       // buf has been deallocated
    abandoned: Bool,      // VT handle was dropped / cancelled
}

implement<T> core.Destructible for ResultState<T> {
    pub fn destroy(self) nothrow -> Void {
        if !self.released {
            if self.initialized {
                // mem.read consumes the T; its Destructible runs
                // at the end of the v's scope.
                unsafe { var v = mem.read<type T>(&mut self.buf, 0); }
            }
            unsafe { mem.dealloc<type T>(self.buf); }
        }
    }
}

pub struct VirtualThread<T> {
    joined: Bool,
    handle: Int,
    state: core.Arc<conc.Mutex<ResultState<T>>>,
    cancelled: Bool,
    submit_error: Int,
}
```

The cb thunk captures a clone of the same `Arc<Mutex<ResultState<T>>>`.
Operations linearize through `state.lock()`:

- **Thunk (after `cb.call()` returns v):**
  - lock; if `abandoned`: leave v to be dropped at scope end
    (T's `Destructible` runs locally), do not write to buf, do not
    publish; else: `mem.write(&mut buf, 0, move v); initialized = true`;
    unlock.
- **`Destructible for VirtualThread<T>`:**
  - if `joined`: return.
  - lock; `abandoned = true`; if `initialized`:
    `var v = mem.read(&mut buf, 0); initialized = false;` (v's
    Destructible runs at end of inner scope); unlock.
  - if `handle != 0`: `thread.vt_drop(handle)` — runtime signals cv
    so any parked cb wakes and the thunk path runs.
  - Our `Arc` clone drops here. ResultState destructor fires when the
    last Arc dies; if `!released`, deallocs buf (and runs T destructor
    on any still-initialized contents, which there won't be at this
    point unless the thunk re-published between our check and now —
    but we set abandoned before unlocking, so the thunk cannot
    publish after our check).
- **`join` (Ok path):**
  - `thread.vt_join(handle)` — wait for cb to complete (the thunk
    will have either published or detected abandoned; here we're
    on the publish path).
  - lock; assert `initialized`; `var v = mem.read(&mut buf, 0);
    initialized = false;` unlock.
  - `joined = true`; Arc drops; ResultState dealloc's buf at last-Arc.
  - return `Ok(v)`.
- **`join` (Cancelled path — `self.cancelled == true`):**
  - `thread.vt_join(handle)`.
  - lock; if `initialized`: `var v = mem.read(&mut buf, 0);
    initialized = false;` (drop v); unlock.
  - `joined = true`; Arc drops; ResultState dealloc's buf.
  - return `Err(CANCELLED)`.
- **`spawn` / `spawn_on` submit-error:**
  - Don't manually dealloc the buf. Drop the thunk (its Arc clone
    decrements). Return the VT with `submit_error != 0` (still
    holding its Arc). When the caller drops the VT, ResultState
    sees its Arc count drop to zero and deallocates the buf in its
    Destructible. **No double-free, no special-case.**

**How option (d) closes each matrix case:**

|     | option (d) outcome |
| --- | --- |
| R1  | Destructor sets abandoned before vt_drop; cb wakes, thunk sees abandoned and drops v locally without writing to buf. No UAF. |
| R2  | spawn no longer manually dealloc's on submit-error; ResultState destructor handles buf cleanup when both Arcs are gone. No double-free. |
| R3  | Destructor sees initialized=true under the lock, reads v out, drops it; ResultState destructor deallocs the empty buf. No leak. |
| R4  | Join's cancellation branch reads v out under the lock and drops it. No leak. |
| R5  | join_timeout's cancellation branch mirrors R4; same fix. No leak. |
| R6  | Adjacent `join_any`-only fix: replace raw `mem.read` peek with `mem.ptr_at_ref` + `*deref`, which routes through `T`'s real Copy lowering (`drift_string_retain` for `String`).  Legacy peek-without-consume contract preserved; no double-release. |
| R7  | Adjacent `join_any`-only fix: gate the peek on `ResultState.initialized` under the lock.  If `initialized == false`, release the lock and delegate to `f.vt.join()` for proper cancellation cleanup.  No uninit read. |
| R8  | Adjacent `join_any`-only fix: at the top of each per-future probe, if `f.vt.submit_error != 0` delegate to `f.vt.join()` (which surfaces the deferred submit error as Err(FAILED)).  Polling loop never spins on `handle == 0`. |

**Cost estimate:** ~120-180 LOC delta in
`stdlib/std/concurrent/concurrent.drift`. New `ResultState<T>` struct
and Destructible impl; updates to `spawn`, `spawn_on`,
`Destructible for VirtualThread<T>`, `join`, `join_timeout`. **Zero
runtime/ABI changes.** Mutex<T> + Arc<T> are already stable. No
`thread_runtime.c` modifications.

**Concerns to validate during investigation:**

1. **Mutex<T> contention cost.** The Mutex is spin-CAS
   (`stdlib/std/concurrent/concurrent.drift:382-388`). Critical
   sections are short (a few stores + maybe a `mem.read`). Acceptable
   for the contention pattern (single thunk writer, single
   destroy/join reader, rarely racing).
2. **`mem.read` from a `RawBuffer<T>` inside a `Mutex<T>` guard
   `&mut`.** Verify the borrow-checker accepts this shape; if not,
   surface as a separate finding.
3. **Submit-error in spawn-with-Arc-construction.** The spawn path
   now allocates Arc + Mutex + ResultState BEFORE attempting submit.
   On submit failure, those structures must drop cleanly. Standard
   destructor chain should handle it, but verify.
4. **Arc cycle risk.** None: ResultState holds no back-reference to
   the Arc; the cb thunk's captures form a tree, not a cycle.
5. **`join_timeout` partial completion.** On timeout, `joined` stays
   false; subsequent retry must observe state correctly. The Mutex
   ensures lock/unlock around each access; no special handling
   needed.

**Subordinate options (only if (d) investigation finds a blocker):**

- **(a) Synchronous join in `drift_thread_drop`.** Closes R1 but
  not R2/R3/R4. Risks deadlock against the
  `thread_runtime.c:858` invariant; requires a cb-safe-point survey.
  Not preferred even alone, and does not close the matrix.
- **(b) Per-T drop thunk on the runtime side.** Closes R1/R3 but
  requires ABI plumbing (`DRIFT_RT_ABI_VERSION` bump). Does not
  obviously close R2 (submit-error is pre-thunk-registration) or
  R4 (post-publish cancel cleanup is a Drift-side flow).
  Strictly worse than (d) on cost and coverage.
- **~~(c) Untyped runtime handoff.~~** Removed earlier as unsound;
  layout-only `mem.dealloc<T>` would leak destructible T.

### 5.3.1 Implementation rule

Implement the simplest design that satisfies the whole matrix.
Investigate option (d) before any runtime ABI expansion. If (d) is
found unworkable for a specific reason during investigation, document
the blocker here and fall back to a layered design — but do **not**
fall back to a partial fix that closes some matrix cases and leaves
others open.  (Outcome: option (d) landed at 0.33.1 and closed all
of R1-R8; see §5 status header.)

### 5.4 Provisional consumer-side guidance (historical — pre-fix)

> Superseded by the option-(d) fix at 0.33.1.  After the fix,
> dropping an unjoined running `VirtualThread<T>` is sound:
> `Destructible::destroy` sets `abandoned = true` under the state
> lock; the cb thunk's `_publish_or_drop` then drops the produced
> `T` locally instead of writing to the buffer; the buffer is
> deallocated exactly once by `ResultState::destroy` when the last
> `Arc` clone dies.  The guidance below was the temporary contract
> for the window between classification and fix landing.

Consumers should retain `VirtualThread<T>` handles and drive them
to `.join` / `.join_timeout` before drop, exactly as the bookkeeper
team's supervised pattern already does. The Destructible-for-
`VirtualThread<T>` contract is "supervised drain is preferred —
the destructor handles unjoined drops safely (post-0.33.1) but
retaining handles is still the supported supervisory pattern."

## 6. Release bookkeeping

Public stdlib surface addition. The slice requires:

- **`doc/history.md`** entry under the date the slice lands, in the
  established format (`## YYYY-MM-DD (stdlib addition: ...)` followed
  by the `lang/versions.py` bump line). Lists `channel<T>` /
  `Sender<T>` / `Receiver<T>` / `is_complete` as additive (no
  removed / changed contracts).
- **`doc/design/drift-concurrency.md`** updated with the new
  primitives' design rationale (closed-state-under-mutex,
  no-`T:Share` v1, terminal `is_complete` semantics). This is the
  canonical cross-reference for the public API surface.
- **`DRIFTC_VERSION` bump in `lang/versions.py`** for the slice's
  landing commit, matching the per-change bump cadence the rest of
  history.md uses. Pure stdlib addition — patch-level bump.
- **ABI: unchanged** by §2 in isolation. Channel is pure Drift-side;
  `is_complete` is a method on existing types using already-exposed
  `thread.vt_is_completed`. No new runtime symbols, no new boundary
  contracts. `DRIFT_RT_ABI_VERSION` (per `lang/versions.py`) stays
  put; `drift-lang-abi.md` does not need an entry for this slice.

**§5 version obligations (separate from §2 — applies regardless of
fix shape chosen):**

- The runtime fix is itself a behavior-changing landed commit. It
  requires its **own `DRIFTC_VERSION` bump** in `lang/versions.py`
  and its own `doc/history.md` entry under the standard
  `## YYYY-MM-DD (LANGUAGE_BUG fix: …)` heading.
- If the selected fix changes the compiler/runtime boundary contract
  — option (b)'s per-T drop-thunk registry is the prototypical case
  — then it **also** requires:
  - a `DRIFT_RT_ABI_VERSION` bump in `lang/versions.py` (currently
    14; the runtime stamp at `lang/language_runtime/abi_version_stamp.c`
    auto-tracks the macro);
  - an ABI-mismatch regression update under the existing
    abi-version-stamp test path so the new version-mismatch surface
    is exercised;
  - a `doc/design/drift-lang-abi.md` entry documenting the new
    boundary contract (per the rules at the top of that file).
- Option (a) — synchronous join in drop — does not change the
  symbol/signature surface. `DRIFT_RT_ABI_VERSION` stays put for
  that path; only `DRIFTC_VERSION` + `history.md` apply.
- Fix-shape selection in §5.3 is the decision point for which of the
  two version obligations applies. Either way, §5's bookkeeping is
  separate from §2's — the slice ordering in §3 means §5's bump
  lands first, then §2's lands as a follow-up patch bump.

## 7. Out of scope

- **`WaitGroup`** — removed from the bundled slice per the revised
  consumer request. The supervisor model now relies on retained VT
  handles + the typed completion channel as the single authoritative
  lifecycle path; a parallel completion counter could diverge from
  retained-handle/channel state. If a future consumer needs a
  WaitGroup-shaped primitive, it ships as its own additive request —
  not on this slice's coattails.
- `detach()` — explicitly rejected by the requester; would footgun the
  lease contract.
- New cancellation/abort semantics — cooperative `cancel()` is enough
  for the team's bounded-drain policy.
- Bounded channel variant / select / try_send / try_recv — wait for a
  second consumer to ask before committing to backpressure semantics.
- `Scope`-based scoped-spawn rework — existing `Scope` stub stays
  as-is. The channel-driven reaper plus retained handles covers the
  supervised-drain use case without scoped-spawn plumbing.
- `Send` trait definition / enforcement — `channel<T>()` ships
  unbounded in `T` for v1; tightening to `require T is Send` is the
  follow-up once the language gate exists.
