# F3 — unify the reactor on a wait-set wait primitive — design & proof pass (NO CODE)

Status: **design for review. Implements nothing.** Per direction update: the
multi-fd wait-set becomes the **canonical runtime wait primitive**, and existing
single-fd waits route through it as a one-entry wait-set. Companion to `PLAN.md`
(F3) and `PROGRESS.md`.

**Hard constraints (from the direction update):**
- `poll_many` is the **first public readiness API.** There is NO public single-fd
  `io.poll` — it was prototyped and pulled (§F1 in `PLAN.md`); it is not shipping and
  is not treated as a source-compatible public API.
- Public **source-compatibility** that must hold is for the EXISTING net/io
  surface: all `TcpStream`/`TcpListener`/`UdpSocket` `read`/`write`/`accept`/
  `connect` (and file io) signatures and behavior are unchanged by this refactor.
- Any **one-fd** readiness behavior is **private/internal** (a thin wrapper over the
  wait-set primitive) unless a public single-fd API is separately approved later.
- Internally, `_block_on_io` (and any future private one-fd helper) may channel
  through the new wait-set path **once compatibility tests are in place**.
- Runtime refactor → **ABI bump expected** unless proven otherwise (it isn't — §5).
- **No runtime code is written until the cleanup/epoch story is reviewed.**

Everything is grounded in the current reactor
(`lang/language_runtime/posix/thread_runtime.c`) and the now-removed prototype
single-fd poll (preserved in git history) plus `_block_on_io`
(`stdlib/std/io/io.drift`).

---

## 0. Runtime ground truth (what we are unifying)

- **`ReactorWatch`** is per-fd: `{ fd; events; uint64 read_vt; uint64 write_vt;
  uint8 pending_read, pending_write; *next }`. **One waiter VT per direction per
  fd.** `r->watches` list under `r->mu`; `EPOLL_CTL_ADD` once (`EPOLLET|IN|OUT`).
- **`DriftVt`** (opaque `VtHandle=uint64` to Drift) has `state`, `park_token`,
  `mu`, `vtid`, `exec`, and a **single** `wait_kind`/`wait_id` (liveness only).
  No per-VT reactor wait-set today.
- **Edge delivery** (two sites: reactor-thread loop ~L756, worker-owned loop
  ~L1300): under `r->mu`, per fd, read `read_vt`/`write_vt`, zero it,
  `drift_vt_claim_for_resume` (bare `PARKED→READY` CAS — **no wait_id/epoch
  guard**), resume/enqueue; no-waiter edge sets `pending_*`.
- **`reactor_check_pending(fd,dir)`** consumes the pending flag (one-shot).
- **Today's wait shape (single-fd users):**
  `_block_on_io(fd,interest,deadline)` and the now-removed prototype single-fd poll
  did: check_pending → `reactor_register_io(fd,interest,vt,deadline)` → `vt_park*`.
  The prototype additionally re-checked pending after register and **cleared its slot
  after every wake** (the bespoke stale-waiter fix) — that logic moves into the
  shared wait-set primitive here.
- **Cleanup today:** `drift_reactor_forget_vt(vt)` clears ALL of a VT's slots —
  but only at `drift_vt_destroy`. `drift_reactor_forget_fd(fd)` (`EPOLL_CTL_DEL` +
  `free(watch)`) runs on fd close (`io_runtime.c:45`) and does **not** unpark a VT
  parked on that fd. Timer expiry / cancel just unpark; they don't clear slots.

**The unification thesis:** there should be exactly one "register interest, park,
wake, clean up" pattern in the runtime. Today the wait is open-coded for single-fd —
`_block_on_io` has *no* cleanup on its timeout path, and the prototype poll needed a
bespoke per-wake clear to be correct. Make it **one primitive** that takes 1..N
entries; single-fd is N=1. The epoch + cleanup story (§3/§4) then lives in exactly
one place and every waiter — `poll_many` (public), `_block_on_io`, and through it
every net/io read/write/accept — inherits the same correctness.

---

## 1. The canonical runtime wait operation

A wait-set of N `(fd, interest)` entries; park until any ready / timeout / cancel;
record the ready entries; **clear all registrations on every resume path**.

It is split into a thin **stdlib driver** (`_wait_set`, in `std.io`) over a small
set of **runtime intrinsics** — so `PollEntry`/`PollReady` and result assembly stay
pure Drift (out of the ABI surface; §5), while the race-critical bits (epoch-stamped
slots, the guard in edge delivery, atomic cleanup) live in C.

### 1.1 Per-VT wait-set state (new, on `DriftVt`)
```c
atomic_uint_fast64_t wait_epoch;   // bumped at the END of every park episode
DriftIoReg          *io_regs;      // head of THIS episode's (fd,dir) registrations
```
`DriftIoReg = { int fd; uint8_t dir; DriftIoReg *next; }`, allocated per registered
(fd,dir), freed at cleanup. Runtime-internal; the compiler never sees this layout.

### 1.2 Epoch-stamped watch slot (extend `ReactorWatch`)
```c
uint64 read_vt;  uint64 read_epoch;
uint64 write_vt; uint64 write_epoch;
```
`*_epoch` = the registering VT's `wait_epoch` at registration time.

### 1.3 Runtime intrinsics (the only new/changed boundary symbols — §5)
- `reactor_wait_register(fd, interest, vt, deadline_ms, epoch) -> Void` —
  epoch-stamped variant of `reactor_register_io`; sets the slot `(vt, epoch)` AND
  pushes a `DriftIoReg` node onto `vt->io_regs`.
- `vt_wait_epoch_begin() -> Uint64` — returns the epoch to register the *next*
  wait-set under (snapshot of `wait_epoch`; does not bump).
- `reactor_wait_clear(vt) -> Void` — **the cleanup**: bump `vt->wait_epoch`, then
  under `r->mu` walk `io_regs`, zero each slot still pointing at `vt`, free nodes.
- (reuse existing) `reactor_check_pending(fd,dir)`, `vt_park`, `vt_park_until`,
  `now_ms`, `vt_current`.

### 1.4 Stdlib driver `_wait_set(regs, deadline_ms) -> ready-mask` (one place)
```
vt = vt_current(); if 0 -> requires_vthread
epoch = vt_wait_epoch_begin()
for each (fd,dir) in regs: reactor_wait_register(fd, dir, vt, deadline_ms, epoch)
// edge-in-register-window guard:
if any check_pending(fd,dir): reactor_wait_clear(vt); return <those ready>
park: deadline_ms>0 ? vt_park_until(deadline_ms) : vt_park(0)
// EVERY resume path (io / timeout / cancel / spurious) lands here:
reactor_wait_clear(vt)                       // bump epoch + clear all slots + free nodes
collect: for each (fd,dir): if check_pending(fd,dir) mark ready (+ hup/err bits)
if none ready and deadline elapsed -> Err(timeout)
return ready set                              // conservative-ready (caller confirms with the op)
```
Note `reactor_wait_clear` runs **before** collection, and it bumps the epoch first
so an edge racing the clear is dropped by the guard (§4).

---

## 2. Single-fd compatibility path

**`poll_many(entries, timeout)` is the FIRST and ONLY public readiness API** (the
public single-fd `io.poll` was pulled — §F1 in `PLAN.md`). `PollEntry`/`PollReady`,
coalesced duplicates, empty→`invalid_argument`, hup/err always surfaced; = `_wait_set`
over the coalesced N entries, assembling `PollReady[]`.

**Single-fd is an INTERNAL detail, not a public function.** If a single-fd
convenience is ever wanted it is a thin **private** wrapper — a one-entry wait-set
`[(fd, interest)]` through `_wait_set`, mapping the single result. The Phase-1
observable contract (ET-replay, `Duration(0)`=park-until-ready via `vt_park(0)`,
distinct `timeout` kind, conservative-ready, no re-park, post-wake cleanup) is
preserved by `_wait_set` itself — the bespoke per-wake clear that lived in the
prototype `io.poll` becomes the shared `reactor_wait_clear`. No public single-fd
surface is reintroduced without a separate decision.

### `_block_on_io` and the net/io read/write/accept/connect paths — migrate NOW
`_block_on_io(fd, interest, deadline)` exists **twice** (`io.drift:750`,
`net.drift:153`) and is the single choke point every blocking socket op funnels
through. Re-author **both** as a one-entry `_wait_set` wait (park-until-ready-or-
timeout; result discarded — the caller re-attempts the actual op). Then **no caller
changes**: the read/write/accept/connect loops keep calling `_block_on_io`.

**Call sites that inherit the change (no edits needed, listed for the equivalence
review):**
- `io.drift`: file read/write loops at ~L1271 (`_block_on_io(fd,1,deadline)`),
  ~L1320 (`,4,`), ~L1478 (`,1,`), ~L1592 (`,4,`).
- `net.drift`: `_block_on_io` + its callers — `TcpStream.read`/`write`,
  `accept`, `connect` retry loops; `UdpSocket` recv/send.

**Old/new behavior equivalence (the migration's burden of proof):**
| Aspect | Old `_block_on_io` | New (1-entry `_wait_set`) | Equivalent? |
|---|---|---|---|
| ET-replay before park | yes (return early) | yes (same check_pending) | ✔ identical |
| register interest | `reactor_register_io` | `reactor_wait_register` (+epoch+node) | ✔ same slot effect |
| park (finite) | `vt_park_until(deadline)` | same | ✔ |
| park (deadline≤0) | returns immediately (net copy) / n/a | **`vt_park(0)`** | ⚠ behavior CHANGE for deadline≤0 — but callers always pass a positive per-op deadline, so unreached; pin with a test |
| post-wake slot state | **left set** (stale until forget_vt/next call) | **cleared** (`reactor_wait_clear`) | ⚠ strictly safer; callers re-register on retry so no functional change |
| caller retry loop | re-call `_block_on_io` | re-call `_block_on_io` | ✔ unchanged |

The two ⚠ rows are the whole equivalence risk; both are covered by the
compatibility matrix (§4) before the refactor merges. Because every socket op
already loops `op → WOULD_BLOCK → _block_on_io → retry`, the extra per-wake clear
cannot change results — the next iteration re-registers regardless.

**Recommendation: migrate `_block_on_io` in the same slice** (it is the proof that
the unified primitive is byte-equivalent across all socket ops). If review prefers
lower blast radius, see the risk split (§6) for deferring it one slice.

---

## 3. Cleanup and stale-wake safety

**Invariant: a VT's wait-set is torn down and its epoch bumped on EVERY resume path,
before it can park on a new set.** Single shared site for the common paths:
`_wait_set`'s `reactor_wait_clear(vt)` immediately after `vt_park*` returns — because
IO-wake, timer-wake, cancel-wake, and spurious-wake all return control to the VT at
that one park site.

| Path | Who cleans | Mechanism |
|---|---|---|
| readiness (IO) wake | the VT, post-park | `reactor_wait_clear` (bump epoch, clear slots, free nodes) |
| timeout wake | same | same — uniform post-park cleanup (fixes today's `_block_on_io` timeout leak) |
| cancellation wake | same | same — cancelled VT returns to park site, cleans, then unwinds |
| spurious wake | same | same |
| **VT destroy** (never resumes) | `drift_vt_destroy` → `drift_reactor_forget_vt` | extend to also free `io_regs` (already clears slots under `r->mu`) |
| **fd close/forget while registered** | `drift_reactor_forget_fd` | before `free(watch)`: capture `read_vt`/`write_vt` and `drift_thread_unpark` them so a waiter on the closed fd wakes, cleans, and observes the fd gone; the waiter's later `reactor_wait_clear` finds no watch for that fd → no-op (safe) |

### Per-registration epoch / generation guard
`vt->wait_epoch` is bumped once per episode (at `reactor_wait_clear` start). Each
slot carries the `reg_epoch` it was registered under. **Edge delivery (both loops)
gains:** under `r->mu`, if `w->read_vt == V` then claim **only if**
`V->wait_epoch == w->read_epoch`; else drop (zero the slot, no claim, no pending).
Symmetric for write; `EPOLLERR|EPOLLHUP` fold into the set direction(s).

### Locking order & UAF/deadlock argument
- `r->mu` is the sole lock over watch slots, the watch list, and timers.
  `wait_epoch` is atomic. `io_regs` is mutated only by its owning VT, except
  `forget_vt`/`forget_fd` which take `r->mu`.
- Order is always **`wait_epoch` (atomic, lock-free) → then `r->mu`.** Edge delivery
  holds `r->mu` and reads `wait_epoch` atomically: a consistent old-or-new value —
  old ⇒ legitimate claim of a still-parked VT; new ⇒ drop. Both correct.
- **No UAF:** slots are freed only under `r->mu`; the dispatch loops already hold
  `r->mu` across claim+enqueue so `forget_vt` (from `drift_vt_destroy`, possibly
  another thread) cannot free concurrently (existing comments L847/L895/L1348/
  L1370). `drift_vt_destroy → forget_vt` clears every slot referencing the VT under
  `r->mu` before the struct is freed, so no slot dangles.
- **No deadlock:** the hot path takes a single lock; cleanup never holds `r->mu`
  across `swapcontext`/a VT lock; edge delivery never takes a VT lock then `r->mu`.
  No lock cycle.
- **Double-resume:** prevented by `claim_for_resume`'s CAS (only one resumer wins
  `PARKED→READY`) **plus** the epoch guard (a stale-epoch edge never even attempts
  the claim). Today only the CAS exists; the epoch closes the
  claim-the-wrong-episode hole that the CAS alone cannot see.

---

## 4. Compatibility & correctness tests — before/with the refactor

**Gate A — nothing regresses (run before the refactor lands):**
- All Phase-1 `test_concurrent_yield_poll.py` (9) still pass — now exercising the
  N=1 wait-set path: yield_now, poll readiness/timeout/replay/no-deadline,
  stale-waiter, listener accept-readiness, 2× valgrind.
- Existing net/io e2e + driver suites (`TcpStream`/`TcpListener`/`UdpSocket`
  read/write/accept/connect, file io) still pass — proving the `_block_on_io`
  migration is behavior-preserving across every socket call site.
- Single-fd no-deadline, timeout, pending-edge replay, stale-waiter cleanup remain
  pinned (they now validate the shared primitive).

**Gate B — multi-fd correctness (new):**
| # | Test | Asserts |
|---|---|---|
| 1 | wake one of N | `PollReady[]` = only that fd/direction |
| 2 | wake on A, then disjoint wait on B, fire A again | B times out **and** `reactor_stale_edge_drops()` (test-only counter) +=1 |
| 3 | timeout | `Err(timeout)`; `io_regs` empty + all slots cleared (test-only occupancy probe) |
| 4 | cancel mid-wait | unwind; all registrations cleared |
| 5 | fd close/forget while registered | waiter unparked; **valgrind clean** (no UAF on freed watch) |
| 6 | duplicate fd/direction | coalesced; one `PollReady`/fd; one slot |
| 7 | hangup (peer close) / error | `.hangup`/`.error` set even for read-only waiter |
| 8 | empty entry list | `Err(invalid_argument)`, no park |
| 9 | stress under valgrind `--fair-sched=yes` | no stale wake, no double-resume, no leak; stale-drop counter stable |

**Determinism aid:** because the negative race tests (2,5,9) are timing-sensitive,
add **`@test_build_only`** runtime probes — `reactor_stale_edge_drops()` (epoch
guard fired) and a slot-occupancy probe — so the assertions are on observable
counters, not scheduler luck. Plus a **C-level unit** for the epoch guard
(register under epoch e, bump to e+1, deliver an edge for e → assert dropped).

---

## 5. ABI / version

**ABI 18 — proven, not guessed.** The unification adds runtime-exported boundary
symbols and changes edge-delivery semantics that cannot live in stdlib:
- new intrinsics `reactor_wait_register`, `vt_wait_epoch_begin`,
  `reactor_wait_clear` (boundary signatures);
- epoch check in both edge-delivery loops; `forget_fd` unparks slot waiters;
  `forget_vt` frees `io_regs` (boundary behavior).

`DriftVt`/`ReactorWatch` field growth is runtime-internal (compiler never emits
their layout — `VtHandle` is opaque), so the struct change alone would not bump;
the **new exported intrinsics do**. `PollEntry`/`PollReady` stay pure Drift stdlib
types (no runtime layout knowledge) because `_wait_set`/`poll_many` are authored in
Drift over per-fd intrinsics — keep them out of the ABI surface.

→ **Bump `DRIFT_RT_ABI_VERSION` 17 → 18**; artifacts rebuild through cert (ABI
policy: new runtime symbols ⇒ bump + rebuild). Bundle with F5 (executor lifecycle,
also ABI-bump) in one release to amortize the rebuild if scheduling allows.

---

## 6. Risk split — if full unification is too invasive for one slice

The goal is **no duplicated semantics**: exactly one implementation of
register+park+epoch+cleanup. Two ordered, non-duplicating steps:

- **Step 1 (smallest, still unifying):** land the runtime primitive + intrinsics
  (epoch-stamped slots, guard, `reactor_wait_clear`, `forget_*` updates) and the
  stdlib `_wait_set`. Route **`poll_many` (the new public API)** through it (plus any
  private one-fd helper as N=1; no public single-fd API is introduced).
  Leave `_block_on_io` on its current direct `register_io`+park path **temporarily
  and explicitly** (a tracked TODO), NOT reimplemented — so there is still one
  *canonical* primitive; `_block_on_io` is a known-legacy shim awaiting migration,
  not a second semantics. Blast radius excludes the hot socket read/write/accept
  loops until the primitive is proven by Gate A+B.
- **Step 2:** migrate both `_block_on_io` copies to one-entry `_wait_set` (no caller
  edits), re-run the full net/io e2e as the equivalence gate, and delete the legacy
  path. After Step 2 there is a single wait implementation end-to-end.

Both steps are ABI-18 (Step 1 introduces the intrinsics). Prefer doing Step 1 and
Step 2 in one slice if the net/io e2e gate is green; split only if review wants the
hot-path migration isolated.

---

## 7. Open questions for the reviewer
1. Migrate `_block_on_io` now (one slice) vs the Step-1/Step-2 split (§6)?
2. `poll_many` name vs unifying under `poll` (blocked by v1 overload rules) — accept
   the two-function pair sharing one `_wait_set`?
3. Empty list → `Err(invalid_argument)`; hup/err always-surfaced — confirm.
4. Bundle ABI-18 with F5 in one release?
5. Accept `@test_build_only` runtime counters (`reactor_stale_edge_drops`, slot
   occupancy) as the determinism mechanism for the negative race tests?
