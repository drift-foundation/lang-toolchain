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
`DriftIoReg = { int fd; uint8_t dir; uint8_t closed; DriftIoReg *next; }`, allocated
per registered (fd,dir), freed at cleanup. `closed` is set by `forget_fd` (under
`r->mu`) when this VT's fd is closed underneath it (§3.1) — a durable terminal signal
on the VT's OWN node, so the waiter never mistakes a watch on a *reused* fd number for
its own readiness. Runtime-internal; the compiler never sees this layout.

### 1.2 Epoch-stamped watch slot (extend `ReactorWatch`)
```c
uint64 read_vt;  uint64 read_epoch;
uint64 write_vt; uint64 write_epoch;
uint8  pending_read, pending_write;   // existing
uint8  pending_hup, pending_err;      // NEW — set from EPOLLHUP/EPOLLERR (I1)
```
`*_epoch` = the registering VT's `wait_epoch` at registration time. The four
`pending_*` bytes are the durable readiness record read by `check_pending`/collection
(I1); they are epoch-independent and survive `reactor_wait_clear` (which clears the
`*_vt` slots + the wait-set timer, but NOT `pending_*`), so a same-fd edge that races a
clear is replayed to the next wait.

### 1.2bis The two invariants that make this correct

The whole refinement rests on separating **the data record** from **the wake**:

> **I1 — pending is the durable, epoch-INDEPENDENT readiness record.** Edge delivery
> ALWAYS sets the fired direction's `pending_*` bit (and `pending_hup`/`pending_err`
> — new per-watch bytes) under `r->mu`, *whether or not it wakes a waiter*. This is
> the one behavior change to edge delivery: today the waiter path zeroes `read_vt`
> and direct-resumes WITHOUT setting pending (the woken reader is expected to drain
> the fd). A readiness *reporter* does not drain, so pending must persist and be the
> thing collection reads. Pending is "fd F has a level/edge available" — true
> regardless of which episode registered interest, so it is never gated by epoch and
> is never lost.

> **I2 — the epoch gates only the WAKE (the claim), never the data.** A current-
> epoch registration may claim+resume its parked VT; a stale-epoch (prior-episode)
> registration sets pending but must NOT claim/resume (that is the multi-worker
> stale-wake). So a late edge for a torn-down registration can never spuriously wake
> a VT now parked on a different set — but its data still lands in `pending`, so the
> next wait that registers that fd sees it immediately (ET-replay). **No readiness is
> ever dropped; only spurious *wakes* are suppressed.**

### 1.3 Runtime intrinsics (the only new/changed boundary symbols — §5)
- `reactor_wait_register(fd, interest, vt, epoch) -> Int` — epoch-stamped variant of
  `reactor_register_io`; sets the slot `(vt, epoch)` AND pushes a `DriftIoReg{fd,dir}`
  node onto `vt->io_regs`. **Registers NO timer** (the deadline timer is owned solely
  by `reactor_wait_park`). **Returns a status (issue: registration failure):** `0` on
  success, else the `errno` from `epoll_ctl(ADD)` (e.g. `EBADF` for a closed/invalid
  fd, `EPERM` for a non-pollable fd). **Rollback on failure — no partial state:** if
  the `ADD` fails for a newly-created watch, unlink+free that watch; either way, do NOT
  set the failing direction's slot and do NOT push the `io_regs` node. So a failed
  register leaves the reactor exactly as before the call — nothing to park on, nothing
  to leak.
- `vt_wait_epoch_begin() -> Uint64` — snapshot of `wait_epoch` to stamp the *next*
  wait-set (does NOT bump).
- `reactor_wait_clear(vt) -> Void` — **the cleanup**: (1) bump `vt->wait_epoch`
  (atomic), (2) under `r->mu`: **remove `vt`'s wait-set timer** (if any), walk
  `io_regs` zeroing each watch slot still equal to `(vt, *)`, free the nodes. Does
  **not** touch `pending_*` (so a same-fd edge that raced the clear survives for the
  next wait). Empty `io_regs` after. Called after the wait loop ends, AND on a
  registration failure before any park (where no timer exists yet — it just clears the
  already-registered slots/nodes).
- `reactor_wait_collect_pending(fd, interest) -> Int` — **the collection intrinsic
   (medium issue).** Under `r->mu`, consumes and returns a **readiness mask** for the
   fd (bit layout in §1.4a). Returns the `CLOSED` bit if the fd has no watch
   (closed-underneath). Replaces the boolean `reactor_check_pending` for the wait-set
   path. Idempotent-after-consume (bits cleared).
- `reactor_wait_park(vt, deadline_ms) -> Int` — **the park protocol (issues 1 & timeout;
   no token).** It **owns the deadline timer** so timeout uses the same no-token wake as
   IO/cancel. One critical section under `r->mu` does, atomically: **peek**
   `pending_*`/`closed`; check `vt_is_cancelled()`; check `now>=deadline`; if none, **register
   an epoch-stamped wait-set timer** (when `deadline_ms>0`) **and** publish `state=PARKED`.
   Release `r->mu`; block until `state != PARKED`. On return, re-acquire `r->mu`,
   **remove this VT's wait-set timer** (idempotent — gone if it already fired), and
   **derive the reason**: `cancelled → CANCELLED`, else `pending/closed → WOKEN`, else
   `now>=deadline → TIMEOUT`, else `WOKEN` (spurious). Return `WOKEN | TIMEOUT | CANCELLED`.
   **No `park_token` is ever deposited by any waker** — see §1.6. The timer is removed on
   EVERY wake (IO/cancel/spurious/timeout) so a late timer cannot wake the next episode.
- **Edge-delivery change (no new symbol, boundary behavior — both loops):** for a
  fired `(fd, dir)` under `r->mu`: **set `pending_dir` (and `pending_hup/err`) — I1,
  ALWAYS**; then *iff* the slot is set and `slot.epoch == vt->wait_epoch`:
  **claim ONLY if `vt->state == PARKED`** (`claim_for_resume` CAS `PARKED→READY`,
  then enqueue/direct-resume, zero the slot, **and remove the VT's wait-set timer** so
  a late deadline can't wake the next episode). **If the VT is not PARKED, do NOTHING
  else** — no token. (Safe: a non-PARKED VT will peek `pending` under `r->mu` inside
  `reactor_wait_park` *before* it can commit to PARKED — §1.6.) A stale-epoch slot:
  set pending only, no claim; bump `reactor_stale_epoch_drops` (I2).
- (reuse existing) `now_ms`, `vt_current`, `vt_is_cancelled`. `reactor_check_pending`
  (boolean) stays for legacy callers until `_block_on_io` migrates.

### 1.4 Stdlib driver `_wait_set(regs, deadline_ms) -> ready[]` (the one wait loop)
```
vt = vt_current(); if 0 -> Err(requires_vthread)
if regs is empty -> Err(invalid_argument)        // §1.4a; never registers/parks
epoch = vt_wait_epoch_begin()
for each (fd,dir) in regs (coalesced):
    st = reactor_wait_register(fd, dir, vt, epoch)        // no timer here; rolls back its own fd on fail
    if st != 0:                                          // bad/closed/non-pollable fd (e.g. EBADF)
        reactor_wait_clear(vt)                           // tear down ALL already-registered entries
        return Err(IoError(kind=invalid_argument, code=st))   // TERMINAL — never parks (issue: no silent hang)
loop:
    // PRECEDENCE: cancellation > readiness > timeout. Cancel is tested FIRST every
    // iteration, so a cancelled VT never reports readiness (closes the reviewer's
    // "collect before cancel propagates" hole).
    if vt_is_cancelled():                break CANCELLED
    // COLLECT — io_regs still populated; consumes pending (mask) under r->mu.
    ready = []
    for each node(fd,interest) in io_regs (coalesced per fd):
        if node.closed: ready += PollReady(fd, hangup); continue   // §3.1: trust OUR node,
                                                                   //   not a watch on a reused fd#
        m = reactor_wait_collect_pending(fd, interest)     // mask; CLOSED if no watch
        if m != 0: ready += PollReady(fd, m)
    if ready not empty:                  break READY        // readiness wins over a reached deadline
    if deadline_ms>0 and now_ms()>=deadline_ms: break TIMEOUT
    rc = reactor_wait_park(vt, deadline_ms)                 // peek+timer+publish-PARKED, no token
    if rc == CANCELLED:                  break CANCELLED    // EXPLICIT: cancellation wins immediately,
                                                            //   no post-wake collect
    // rc == WOKEN or TIMEOUT: fall through to loop top.
    //   WOKEN  -> next collect reports the readiness.
    //   TIMEOUT-> reactor_wait_park returns TIMEOUT only when nothing was pending at
    //            re-derive; the loop top still does ONE final collect (readiness that
    //            raced the deadline wins), then `now>=deadline` breaks TIMEOUT.
reactor_wait_clear(vt)                   // AFTER collection: epoch bump + slot/timer clear + free nodes
return READY: Ok(ready) | TIMEOUT: Err(timeout) | CANCELLED: propagate cancel
```
**Collect-before-park** closes the wake/collect race; **`reactor_wait_park`'s
peek-under-`r->mu`** closes the collect→park gap (the old token's job, now done with
no token); **collect-before-clear** lets collection read `pending` while slots exist.

### 1.4a Readiness mask (the collection result shape — medium issue)
`reactor_wait_collect_pending(fd, interest) -> Int` returns an OR of:
```
bit 0  READABLE  // interest&read  && pending_read   (consumed)
bit 1  WRITABLE  // interest&write && pending_write  (consumed)
bit 2  HANGUP    // pending_hup  (EPOLLHUP)          (consumed)
bit 3  ERROR     // pending_err  (EPOLLERR)          (consumed)
bit 4  CLOSED    // no watch exists for fd (closed-underneath; §3.1) — terminal
```
The stdlib maps the mask → `PollReady{fd, readable, writable, hangup, error}`
(`CLOSED` ⇒ `hangup=true`). HANGUP/ERROR are reported regardless of `interest`
(always-surfaced). `interest` is the coalesced `want_read|want_write` for the fd.

### 1.5 Readiness ordering — proof no legitimate event is lost (area #1)

Let `V` be the waiting VT, episode epoch `E` (slots stamped `E`, `V.wait_epoch == E`
until `reactor_wait_clear` bumps it to `E+1`). Edge delivery and the waiter's
`reactor_wait_collect_pending`/`reactor_wait_park` peek both take `r->mu`; `pending`
writes/reads are therefore serialized.
For an edge on a registered `(F, D)` of episode `E`, walk the five windows:

| When the edge fires | What edge delivery does (under `r->mu`) | What `V` does | Lost? |
|---|---|---|---|
| **before park** (after register, V RUNNING in `_wait_set`) | set `pending_D` (I1); V not PARKED → **no claim, no token** | `collect` reads `pending_D` → **READY**; or if past F, `reactor_wait_park`'s peek (under `r->mu`) sees `pending_D` → returns WOKEN → re-collect → READY | **No** |
| **during park** (V PARKED, epoch E) | set `pending_D`; `slot.epoch==E==V.wait_epoch` & `state==PARKED` → claim (`PARKED→READY`) → zero slot, enqueue/resume | wakes → `collect` reads `pending_D` → READY | **No** |
| **after wake, before collect** (V RUNNING) | set `pending_D`; V not PARKED → no claim, no token | in-progress/next `collect` reads `pending_D`; else `reactor_wait_park` peek catches it before parking → READY | **No** |
| **during collect** (V iterating `io_regs`) | set `pending_D` under `r->mu` | if before `collect` reaches F → this round; if after → `pending_D` persists; `reactor_wait_park` peek catches it before the next park, else next wait replays | **No** |
| **during/after clear** (V.wait_epoch already bumped to E+1) | set `pending_D` (I1, still); `slot.epoch==E≠E+1` → **no claim** (I2) | terminal outcome already decided; `pending_D` persists on the watch → the *next* wait that registers F collects it (ET-replay) | **No** |

The invariant proven: **`pending_D` is set on every path** (I1), and the only thing
the epoch ever suppresses is a *wake* of a VT that has left episode E (I2) — which is
exactly the spurious wake we want gone. Because `reactor_wait_clear` bumps the epoch
*before* it clears slots, the slot-clear/edge race in the last row resolves to "drop
the wake, keep the data": the edge sees `slot.epoch (E) ≠ V.wait_epoch (E+1)` and
suppresses the claim, while `pending_D` survives for the next registration of F.
**A legitimate readiness can be deferred to the next wait (ET-replay) but never
dropped; a spurious wake is never delivered.**

The one runtime obligation this imposes: **edge delivery must set `pending` BEFORE it
attempts (or declines) the claim, all under `r->mu`,** and **`reactor_wait_clear`
must bump the epoch BEFORE taking `r->mu` to clear slots.** Both are single-writer-
under-one-lock orderings; no second lock, no cross-lock cycle.

### 1.6 Park protocol — no token, no lost wake, no stale token (issue 1)

The unsafe design was "if the claim fails because V is RUNNING, deposit a generic
`park_token`." That token can survive to terminal READY (collect finds pending and
returns without ever parking) and then short-circuit V's *next, unrelated* park —
exactly the class the runtime warns about for direct I/O wake. **We remove the token
entirely** and make `pending` itself the condition, rechecked under `r->mu` at the
moment V commits to PARKED. `r->mu` is the wait mutex; `pending` is the condition;
`vt->state` is the futex/condition word.

**The operations, all serialized by `r->mu` (timer included — the timeout fix):**
```
reactor_wait_park(V, deadline):              ANY waker of V (IO edge / timer / cancel):
  lock r->mu                                   lock r->mu
  if peek_pending(V->io_regs)                  // IO edge: set pending_D (I1)  [timer/cancel: skip]
     or vt_is_cancelled(V)                     if (epoch-current) and V->state == PARKED:
     or now>=deadline:                            CAS PARKED->READY; enqueue;
       unlock; return <reason>                     remove V's wait-set timer; zero slot (IO)
  if deadline>0: register_wait_timer(          else:  // V RUNNING/READY
       V, deadline, epoch=V->wait_epoch)          (nothing — NO token, for every waker)
  V->state = PARKED            // publish      unlock r->mu
  unlock r->mu
  block until V->state != PARKED
  lock r->mu; remove_wait_timer(V)             // idempotent — gone if it already fired
  reason = cancelled?CANCELLED : pending/closed?WOKEN : now>=deadline?TIMEOUT : WOKEN
  unlock r->mu; return reason
```
The deadline **timer is registered inside the same `r->mu` critical section as the
peek + PARKED publish**, and **every wake path removes it** (IO/cancel claim removes
it; `reactor_wait_park` removes it post-block). So no separate `vt_park_until` registers
a competing timer, and **no late timer survives into the next episode** (belt-and-
braces: the timer is also epoch-stamped, so a timer that fires after `reactor_wait_clear`
sees `timer.epoch != V->wait_epoch` and is dropped, exactly like a stale IO edge).

**Claim:** no readiness/timeout/cancel is lost, and no token can outlive a park.
- **No stale token — from ANY waker.** There is no token. Every waker (IO edge, timer
  expiry, cancel) wakes *only* by a `state` CAS on an *already-PARKED* V; none deposits
  anything for a RUNNING V. **Timer expiry uses the same wait-set claim path** (under
  `r->mu`: `if state==PARKED` CAS+enqueue; else nothing) — it does NOT call the generic
  `drift_thread_unpark`, so the timeout path can no longer leak a `park_token`.
- **No lost wake (the collect→park gap):** suppose `collect` released `r->mu` having
  consumed nothing, and an edge fires before V parks. Two interleavings, both under
  `r->mu`:
  1. *edge's `r->mu` section precedes `reactor_wait_park`'s:* edge sets `pending_D`,
     reads `state != PARKED` (V hasn't published yet) → does nothing. Then
     `reactor_wait_park` takes `r->mu`, **peeks `pending_D` → returns WOKEN** (never
     parks). V re-collects → READY. ✓
  2. *`reactor_wait_park`'s section precedes the edge's:* peek finds nothing → V
     publishes `state = PARKED` **under `r->mu`** → unlocks → (begins to block). Then
     edge takes `r->mu`, sets `pending_D`, reads `state == PARKED` → CAS
     `PARKED→READY` + wake. V's block is keyed on `state == PARKED`; since the CAS
     already moved it to READY, the block returns immediately (or is woken). ✓
  Because `state = PARKED` is published **inside the same `r->mu` hold as the peek**,
  there is no window where V is "about to park" but invisible to a concurrent edge:
  the edge either sees `PARKED` (and wakes) or V's peek sees `pending` (and doesn't
  park). This is the standard mutex+condition+futex handshake; the only specialization
  is that the condition is `pending` over `io_regs` and the mutex is `r->mu`.
- **Timeout** wakes via the wait-set timer's expiry, which takes the SAME claim path
  (state CAS on a PARKED V, no token) and removes itself; `reactor_wait_park` re-derives
  `TIMEOUT` (no pending). A timer racing a `reactor_wait_clear` (next episode) is dropped
  by its epoch stamp. **Cancellation** sets `cancelled=1` and wakes a wait-set-parked V
  by the reactor claim path (§3); `reactor_wait_park` re-derives `CANCELLED` (checked
  first). The loop then breaks and `reactor_wait_clear` runs.

(If the runtime's generic `vt_park` cannot publish PARKED under an external lock,
`reactor_wait_park` is a new intrinsic that does — that is the boundary cost, already
counted in §5. It does NOT reuse the generic `park_token`.)

---

## 2. Single-fd compatibility path

**`poll_many(entries, timeout)` is the FIRST and ONLY public readiness API** (the
public single-fd `io.poll` was pulled — §F1 in `PLAN.md`). `PollEntry`/`PollReady`,
coalesced duplicates, empty→`invalid_argument`, hup/err always surfaced; = `_wait_set`
over the coalesced N entries, assembling `PollReady[]`.

**Registration failure is TERMINAL `Err`, never a hang or a per-fd result (policy).**
`poll_many` takes raw fds, so an entry may be closed/invalid/non-pollable. Because
`reactor_wait_register` now returns a status (§1.3), `_wait_set` **fails the whole call
on the first bad fd**: `reactor_wait_clear` tears down every already-registered entry,
and it returns `Err(IoError(kind=invalid_argument, code=errno))` **before any park** —
so `poll_many([bad_fd], no_deadline)` returns immediately instead of parking forever.
(Rejected: reporting the bad fd as a per-fd `hangup`/`error` — that conflates "I closed
this fd mid-loop" (a real `hangup` on a once-valid registration, §3.1) with "you passed
me a fd that was never registrable"; the latter is a caller bug and should surface
loudly.) An fd that is valid at registration but closed *while waiting* still yields
`hangup` via §3.1 — the two paths are kept distinct.

**Single-fd is an INTERNAL detail, not a public function.** If a single-fd
convenience is ever wanted it is a thin **private** wrapper — a one-entry wait-set
`[(fd, interest)]` through `_wait_set`, mapping the single result. The observable
single-fd contract (ET-replay; `Duration(0)`=park-until-ready, no-deadline branch of
`reactor_wait_park`; distinct `timeout` kind) is now provided by `_wait_set` itself. Two prototype-era
notes are SUPERSEDED by the refined model: the bespoke per-wake slot clear becomes
the shared `reactor_wait_clear`; and "conservative-ready / must-not-re-park" is
replaced by **exact pending-based collection with a safe re-park loop** (§1.4/§1.5) —
safe precisely because pending is now always set (I1), so re-parking can never miss a
consumed ET edge. No public single-fd surface is reintroduced without a separate
decision.

### 2.0 Ready-result handoff — decision (area #2)
**Chosen: pending flags are the ready record (option b), with the one edge-delivery
change that pending is set even on the wake path (I1).** Rejected:
- *(a) per-VT ready buffer populated by edge delivery* — a second structure to
  allocate, lock, and drain, duplicating what `pending_*` already is (per-fd, per-
  direction, under `r->mu`); it would also need its own stale-epoch handling.
- *(c) stdlib conservative-ready over all registered fds* — fine for N=1 but **wrong
  for N>1**: it cannot say *which* fds are ready, defeating `poll_many`.
Pending already exists per `(fd,dir)`, is written/read under `r->mu`, and carries
exactly "this fd has an edge available". The only gap was that the wake path didn't
set it; I1 closes that. `hangup`/`error` get two new per-watch bytes
(`pending_hup`/`pending_err`) set from `EPOLLHUP`/`EPOLLERR` the same way, plus the
"registered fd with no watch ⇒ hangup" rule (§4) for the close-underneath case. The
woken VT reads these via `reactor_wait_collect_pending` (mask, §1.4a) over its own
`io_regs` — no reliance on conservative over-reporting and no second buffer.

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
| ET-replay before park | yes (return early) | yes (`reactor_wait_park` peek / first collect) | ✔ same effect |
| register interest | `reactor_register_io` | `reactor_wait_register` (+epoch+node) | ✔ same slot effect |
| park (finite) | `vt_park_until(deadline)` | `reactor_wait_park` (finite deadline) | ✔ same blocking semantics; adds the under-`r->mu` peek |
| park (deadline≤0) | returns immediately (net copy) / n/a | `reactor_wait_park` (no deadline → park until ready) | ⚠ behavior CHANGE for deadline≤0 — callers always pass a positive per-op deadline, so unreached; pin with a test |
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
`_wait_set`'s `reactor_wait_clear(vt)` after the loop ends (post-`reactor_wait_park`) — because
IO-wake, timer-wake, cancel-wake, and spurious-wake all return control to the VT at
that one park site.

| Path | Who cleans | Mechanism |
|---|---|---|
| readiness (IO) wake | the VT, post-loop | `reactor_wait_clear` (bump epoch, clear slots, free nodes) after `collect` decides READY |
| timeout wake | same | wait-set timer (owned by `reactor_wait_park`, §1.6) expires → SAME no-token claim path (state CAS, no `drift_thread_unpark`); loop re-derives TIMEOUT → `reactor_wait_clear` → `Err(timeout)`. Fixes today's `_block_on_io` timeout leak AND the timeout-token gap |
| spurious wake | same | loop `collect` finds nothing, not done → re-park; eventual real wake/timeout/cancel cleans up once (no per-spurious churn) |
| cancellation wake | same (cooperative) | `reactor_wait_park`'s under-`r->mu` precheck tests `vt_is_cancelled()`; the block also wakes on the `PARKED→READY` claim cancel performs → loop breaks CANCELLED → `reactor_wait_clear`. See backstop below for kill-without-resume |
| **VT destroy** (never resumes) | `drift_vt_destroy` → `drift_reactor_forget_vt` | extend to ALSO free `io_regs` (it already clears slots under `r->mu`). **This is the cancellation backstop** (next paragraph) |
| **fd close/forget while registered** | `drift_reactor_forget_fd` | §3.1 — **claim+enqueue the waiter UNDER `r->mu`** (lifetime barrier), set its `io_regs` node `closed`, free watch; waiter's `collect` then emits `hangup` |

**Cancellation — who clears `io_regs`, and how the wake reaches a no-token park
(area #3).** Cancellation is *cooperative*: `drift_thread_cancel` sets `cancelled=1`
and wakes the VT, which resumes **at its `_wait_set` park site** (NOT user code) and
runs `reactor_wait_clear` before the cancel unwinds — self-cleaning, like timeout.
**Interop obligation (because `reactor_wait_park` ignores the generic `park_token`):**
cancellation of a VT currently in a wait-set episode (`vt->io_regs != NULL`) must wake
it by the **reactor claim path** — `state PARKED→READY` (CAS) — exactly as edge
delivery and `forget_fd` do, NOT by bumping the generic token. Combined with
`reactor_wait_park` testing `vt_is_cancelled()` in its under-`r->mu` precheck (so a
cancel observed before the VT commits to PARKED returns `CANCELLED` without parking),
this closes the cancel/park race with no reliance on a token. The one path where a
parked VT does **not** run post-park code is the scheduler's *kill-at-dispatch* of an
already-cancelled VT (`thread.drift` "scheduler kills parked VTs at worker-dispatch
time"): there the VT goes straight to `drift_vt_destroy`, and the **extended
`forget_vt` is the backstop** — clears every slot referencing the VT and frees
`io_regs` under `r->mu` before the struct is freed. So `io_regs` is cleared either by
the VT (cooperative resume) or by `forget_vt` (kill/destroy) — never leaked, never
dangling.

### Per-registration epoch / generation guard (refined — I1/I2)
`vt->wait_epoch` is bumped once per episode (first action of `reactor_wait_clear`,
*before* it takes `r->mu`). Each slot carries the `reg_epoch` it registered under.
**Edge delivery (both loops), under `r->mu`, for a fired `(F,D)`:**
1. **set `pending_D` (and `pending_hup/err`) ALWAYS** — the durable, epoch-independent
   data record (I1). Never gated by epoch, so readiness is never dropped.
2. if `w->{read,write}_vt == V` **and** `V->wait_epoch == slot.epoch`:
   - if `V->state == PARKED`: `claim_for_resume(V)` (`PARKED→READY`) → zero the slot,
     remove `V`'s wait-set timer, enqueue/direct-resume (the wake).
   - else (`V` RUNNING/READY): **nothing — no token** (§1.6). `pending_D` is already
     set; `V` will observe it via `collect` or `reactor_wait_park`'s peek before it
     can park, so the wake is never lost without a token.
   - else (epoch mismatch — stale prior-episode reg): **no claim**; bump
     `reactor_stale_epoch_drops`. (I2 — suppress the spurious wake; the data already
     lives in `pending_D` for the next registration of F.)

`EPOLLERR|EPOLLHUP` set `pending_err`/`pending_hup` and fold into whichever
direction(s) the slot has a waiter for.

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

### 3.1 fd close / forget semantics (area #4)

`drift_reactor_forget_fd(fd)` runs on `close` (`io_runtime.c:45`). **The waiter is
woken WHILE STILL HOLDING `r->mu`, by claim+enqueue — the same lifetime barrier the
existing dispatch loops use** (their comments at L847/L895/L1348/L1370: "Enqueue
under r->mu so `drift_reactor_forget_vt` cannot free the VT between unlock and
enqueue"). The prior draft's "capture under lock, free watch, unlock, then unpark"
was wrong precisely because freeing the slot removes the barrier `forget_vt` relied
on. Corrected contract — nothing touches a VT after `r->mu` is released:
```
forget_fd(fd):
    lock r->mu
    epoll_ctl(EPOLL_CTL_DEL, fd)               // existing — no new edges for this fd
    w = find_watch(fd)
    if w:
        for each waiter V in {w->read_vt, w->write_vt}, V != 0, distinct:
            mark_closed(V)                       // see "result" below (durable, not on the freed watch)
            if V->state == PARKED: CAS PARKED->READY; drift_exec_enqueue(V)   // UNDER r->mu
            // (if not PARKED, V will observe close via its mark + io_regs peek)
            reactor_close_unparks++
        unlink w from r->watches; free(w)        // slots + pending gone with the watch
    unlock r->mu
    reactor_wake(r)                              // wake the poller thread; touches NO VT memory
```

- **`mark_closed(V)` — where the terminal result lives (NOT on the freed watch).**
  Because the watch is about to be freed, `pending_hup` cannot carry the result. We
  record "a registered fd of V was closed" durably **on V's own `io_regs` node for
  this fd** (set `node.closed = 1`, under `r->mu`) — `io_regs` is owned by V and is
  not freed by `forget_fd`. The waiter's `collect` reads `node.closed` (or, for the
  not-yet-registered-look-up case, `reactor_wait_collect_pending` returns `CLOSED`
  when no watch exists) and emits **`PollReady{fd, hangup=true}`** — a *terminal*
  result, so a no-deadline `poll_many` cannot hang on a fd that will never be ready.
  (`hangup`, not `error`; `error` is reserved for `EPOLLERR` on a live fd.)
- **Lifetime — no UAF (issue 2).** Every VT touch (`state` CAS, `enqueue`,
  `mark_closed`) happens **under `r->mu`**. `drift_vt_destroy → forget_vt` must also
  take `r->mu` to clear that VT's slots before the struct is freed, so it is blocked
  for the entire `forget_fd` critical section — the VT cannot be freed while we claim
  or enqueue it. Once enqueued READY under `r->mu`, the VT is owned by its executor
  run-queue (not eligible for destroy). After `r->mu` is released, `forget_fd`
  dereferences **no VT** — `reactor_wake(r)` touches only the reactor. This is exactly
  the proven discipline of the existing dispatch loops, not a new prose assertion.
- **No swapcontext under lock / no deadlock.** `forget_fd` only *enqueues* (marks
  READY + pushes to the exec queue); it never `swapcontext`s into a VT. Single lock
  (`r->mu`) on this path; no VT lock taken under it; no cycle.
- **Why fd reuse cannot wake the wrong VT.** `EPOLL_CTL_DEL` + `free(watch)` complete
  under `r->mu` before the fd number can be reused. A later socket reusing the number
  registers a **fresh watch** with the new VT's slot; the old waiter already (a) was
  enqueued, (b) will collect its `closed`/`hangup`, and (c) runs `reactor_wait_clear`
  freeing its `io_regs` node for F — so it references neither the freed nor the new
  watch. Epoch covers same-fd/same-VT across episodes; watch-free+DEL covers fd reuse.
- **No double free vs concurrent `reactor_wait_clear`** (both under `r->mu`,
  serialized): `forget_fd` first ⇒ `wait_clear` re-looks-up F by number, finds no
  watch, skips it (frees only its own `io_regs` nodes). `wait_clear` first ⇒ slot
  zeroed, `io_regs` freed; `forget_fd` finds the watch with `read_vt==0`/`write_vt==0`
  ⇒ no waiter to enqueue ⇒ frees the watch. Watch freed once (forget_fd); nodes freed
  once (wait_clear).

---

## 4. Compatibility & correctness tests — before/with the refactor

**Gate A — nothing regresses (run before the refactor lands):**
- `test_concurrent_yield_now.py` (yield_now functional + valgrind) still passes —
  unaffected (yield_now does not touch the reactor), but run as a smoke.
- **Existing net/io e2e + driver suites are the primary Gate A** (`TcpStream`/
  `TcpListener`/`UdpSocket` read/write/accept/connect, file io) — they must pass
  unchanged, proving the `_block_on_io` → 1-entry-`_wait_set` migration is
  behavior-preserving across every socket call site. This replaces the pulled
  single-fd `io.poll` tests as the N=1 coverage.
- New single-fd-equivalence pins (authored against the private one-entry wrapper or
  directly via a 1-entry `poll_many`): no-deadline park-until-ready, timeout kind,
  pending-edge replay, post-wake slot cleared — the prototype `io.poll` behaviors,
  re-pinned on the shared primitive.

**Gate B — multi-fd correctness (new):**
| # | Test | Asserts |
|---|---|---|
| 1 | wake one of N | `PollReady[]` = only that fd/direction; `vt_io_reg_count()==0` and `reactor_active_slots()==0` after return |
| 2 | wake on A, then disjoint wait on B, fire A again | B `Err(timeout)` **and** `reactor_stale_epoch_drops()` += exactly 1 (epoch suppressed A's late edge) |
| 3 | timeout | `Err(timeout)`; `vt_io_reg_count()==0` + `reactor_active_slots()==0`; **a 2nd unrelated park right after must NOT return early** (proves the timeout path left no token) |
| 3b | readiness racing the deadline | a write that lands at ~the deadline → `Ok(readable)`, not `Err(timeout)` (readiness > timeout precedence) |
| 4 | cancel mid-wait | unwind; `vt_io_reg_count()==0` (cooperative path) AND a variant that forces kill-at-dispatch → still 0 via `forget_vt` |
| 4b | cancel while a fd is also ready | returns CANCELLED, **not** readiness (cancellation > readiness precedence) |
| 5 | fd close/forget while registered | waiter gets `PollReady{fd,hangup}`; `reactor_close_unparks()` += 1; **valgrind clean** (no UAF on freed watch) |
| 6 | duplicate fd/direction | coalesced; one `PollReady`/fd; `reactor_active_slots()` counts one slot/dir |
| 7 | hangup (peer close) / error | `.hangup`/`.error` set even for a read-only waiter |
| 8 | empty entry list | `Err(invalid_argument)`, no park (`vt_io_reg_count` never rises) |
| 9 | **invalid/closed fd, finite timeout** | returns `Err(invalid_argument)` **immediately**, NOT `Err(timeout)` (distinguish by elapsed ≪ timeout + kind) |
| 10 | **invalid/closed fd, NO deadline** | returns `Err(invalid_argument)` and **does not hang** (the core gap — bounded by a test watchdog) |
| 11 | **mixed wait-set: one invalid fd among valid ones** | `Err(invalid_argument)`; all prior registrations rolled back: `vt_io_reg_count()==0` AND `reactor_active_slots()==0` (no orphan watch) |
| 12 | stress under valgrind `--fair-sched=yes` | no stale wake, no double-resume, no leak; counters internally consistent |

**Determinism aid — exact `@test_build_only` runtime probes (area #6).** All are
process-global atomics or per-VT reads, zero-cost in release builds:
- **`reactor_stale_epoch_drops() -> Int`** — incremented in edge delivery on the I2
  branch (slot set, `slot.epoch != V->wait_epoch`, claim suppressed). Pins test #2:
  assert it rises by exactly 1 when A's late edge is suppressed — turns the race into
  a counter assertion, no scheduler luck.
- **`reactor_active_slots() -> Int`** — count of watch slots with a non-zero
  `read_vt`/`write_vt` across `r->watches` (under `r->mu`). And **`vt_io_reg_count(vt)
  -> Int`** — length of `vt->io_regs`. Both must read 0 after any `poll_many` returns
  (tests #1,#3,#8,#11 — incl. the registration-failure rollback) — proves
  `reactor_wait_clear` (or `forget_vt`) ran.
- **`reactor_close_unparks() -> Int`** — incremented per waiter claimed+enqueued under `r->mu` in
  `forget_fd`. Pins test #5: assert += 1 and the waiter observed `hangup`.
- **C-level unit** for the epoch guard, independent of the scheduler: register a slot
  under epoch `e`, bump `wait_epoch` to `e+1`, deliver an edge → assert `pending` set
  (I1) AND no claim attempted AND `reactor_stale_epoch_drops` += 1 (I2).

---

## 5. ABI / version

**ABI 18 — proven, not guessed.** The unification adds runtime-exported boundary
symbols and changes edge-delivery semantics that cannot live in stdlib:
- new intrinsics: `reactor_wait_register` (**returns a status** — registration failure,
  §1.3), `vt_wait_epoch_begin`, `reactor_wait_clear`, **`reactor_wait_park`** (no-token
  peek-park + timer ownership, §1.6), **`reactor_wait_collect_pending`** (readiness mask,
  §1.4a) (boundary signatures);
- boundary behavior changes: epoch check + pending-always-set in both edge-delivery
  loops; `forget_fd` claims+enqueues waiters under `r->mu` and sets `io_regs.closed`;
  `forget_vt` frees `io_regs`; cancel of a wait-set-parked VT routes through the
  reactor claim path.

`DriftVt`/`ReactorWatch` field growth is runtime-internal (compiler never emits
their layout — `VtHandle` is opaque), so the struct change alone would not bump;
the **new exported intrinsics do**. `PollEntry`/`PollReady` stay pure Drift stdlib
types (no runtime layout knowledge) because `_wait_set`/`poll_many` are authored in
Drift over per-fd intrinsics — keep them out of the ABI surface.

→ **Bump `DRIFT_RT_ABI_VERSION` 17 → 18**; artifacts rebuild through cert (ABI
policy: new runtime symbols ⇒ bump + rebuild). Bundle with F5 (executor lifecycle,
also ABI-bump) in one release to amortize the rebuild if scheduling allows.

---

## 6. Migration decision (area #5) + risk-split fallback

**DECISION: one-slice full migration.** The reviewer's gate was "if the ready handoff
is fully solved, migrate `_block_on_io` in the same slice." §1.5 + §2.0 + §3 now prove
the ready handoff (pending-as-record I1, epoch-gates-wake-only I2, collect-before-
clear, the no-token peek-park protocol §1.6, no-watch⇒hangup) — so **both `_block_on_io` copies are
re-authored as one-entry `_wait_set` in the same slice**, giving exactly one wait
implementation end-to-end. Gate A (full net/io e2e) is the equivalence proof; it runs
green before merge. No second/legacy wait path remains.

**Fallback (only if Gate A surfaces an equivalence gap):** the goal is **no duplicated
semantics** — exactly one implementation of register+park+epoch+cleanup. If forced to
split, two ordered, non-duplicating steps:

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

## 7. Decisions recorded + remaining questions for the reviewer

**Decided in this pass (areas 1–6 + 3 follow-up issues + the 2 park-protocol gaps):**
- Ready handoff = **pending flags** (I1), epoch gates the wake only (I2); no second
  buffer, no conservative over-reporting (§2.0).
- **Park protocol = no token, for ALL wakers incl. timeout.** `reactor_wait_park`
  peeks `pending`/`closed`/`cancelled`, **registers the epoch-stamped deadline timer,**
  and publishes `PARKED` — all in one `r->mu` critical section. IO edge, **timer
  expiry,** and cancel each wake ONLY an already-PARKED VT via a `state` CAS (no
  `park_token`, no `drift_thread_unpark`); each removes the wait-set timer so no late
  timer wakes the next episode. `reactor_wait_park` re-derives the reason post-block
  (§1.6, issues 1 + timeout).
- **Explicit loop control + precedence (issue 2): cancellation > readiness > timeout.**
  Cancel is tested first each iteration and `rc==CANCELLED` breaks immediately (never
  collects); on `rc==TIMEOUT` the loop does one final collect so readiness racing the
  deadline still wins; `rc==WOKEN` loops to collect (§1.4).
- **`forget_fd` wakes the waiter UNDER `r->mu` via claim+enqueue** (the existing
  dispatch lifetime barrier) — never a post-unlock deref; sets the VT's `io_regs`
  node `closed` so a reused fd# can't be mistaken for its own readiness (issue 2,
  §3.1).
- **Collection intrinsic = `reactor_wait_collect_pending(fd, interest) -> Int` mask**
  (READABLE/WRITABLE/HANGUP/ERROR/CLOSED), consuming, under `r->mu` (medium issue,
  §1.4a). Replaces boolean `reactor_check_pending` on the wait-set path.
- **Registration failure is TERMINAL (issue: invalid fd).** `reactor_wait_register`
  returns a status + rolls back its own fd's partial watch/node on `epoll_ctl(ADD)`
  failure; `_wait_set` clears all already-registered entries and returns
  `Err(invalid_argument)` **before any park** — never a silent hang. An fd closed
  *while waiting* is the separate `hangup` path (§3.1), kept distinct (§2).
- Cleanup on every resume path via post-loop `reactor_wait_clear`; `forget_vt` is the
  kill-without-resume cancellation backstop; cancel of a wait-set-parked VT must wake
  via the reactor claim path, not the generic token (§3).
- Migration = **one slice** (`_block_on_io` included), gated by full net/io e2e (§6).
- Determinism probes = `reactor_stale_epoch_drops`, `reactor_active_slots`,
  `vt_io_reg_count`, `reactor_close_unparks` (all `@test_build_only`) + a C-level
  epoch unit (§4).

**Still for the reviewer:**
1. `poll_many` name vs unifying under `poll` (blocked by v1 overload rules) — accept
   the two-function pair sharing one `_wait_set`?
2. Empty list → `Err(invalid_argument)`; `hangup`/`error` always-surfaced; `close`
   underneath a waiter surfaces as `hangup` (not `error`) — confirm.
3. Bundle the ABI-18 bump with F5 (executor lifecycle) in one release to amortize the
   rebuild?
4. OK to add the one edge-delivery behavior change (set `pending` even on the wake
   path, I1) — it slightly changes a hot path shared by every socket read/write?
5. `reactor_wait_park` as a NEW intrinsic that publishes `PARKED` under `r->mu`
   (the generic `vt_park` can't recheck an external condition under an external lock).
   Accept the new boundary symbol, or is there an existing park you'd prefer adapted?
6. Cancellation interop obligation: route cancel of a wait-set-parked VT through the
   reactor claim path (state CAS), not the generic token — confirm acceptable.
