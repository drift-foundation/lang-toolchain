# PROGRESS — concurrent-server primitives

Power-loss recovery point for this slice. Newest entries on top. Each entry
records the change + verification result + ABI status. See `PLAN.md` for the full
prioritized design (F1…F5).

## Status table

| Feature | Priority | State | ABI | Notes |
|---|---|---|---|---|
| F1a `conc.yield_now()` | 1 | **DONE / SHIPPING** (0.33.47) | 17 | wraps `vt_yield`; validated two-VT handoff + not-sleep. The only public Phase-1 API. |
| F1b single-fd `io.poll()` | — | **PULLED — not shipped** (direction change 2026-06-20) | n/a | built+tested, then removed before release (wrong server pattern). Design folded into F3. |
| F2 `Duration(0)` ops yield | 2 | **DEFERRED** (team asked not to change `Duration(0)` in first cut) | 17 | hold unless requested |
| F3 unified wait-set / `poll_many` | **IMPLEMENTED** | runtime+stdlib+`_block_on_io` migration DONE; full Gate A e2e + Gate B valgrind pending | **18** | `io.poll_many` shipping; both `_block_on_io` copies migrated to the wait-set; Gate B **10/10** functional + net/io & concurrency regression green |
| F4 scheduler fairness | 4 | **DESIGN READY** (`F4-fairness-plan.md`) | **none** | direct-resume bypasses FIFO queue; gate it. ~30-50 LOC, no ABI bump |
| F5 executor lifecycle + reactor | 5 | **DESIGN READY** (`F5-executor-lifecycle-plan.md`) | **bump** | `Executor.shutdown` + reactor poller decoupling; 2 slices; after F4 |

## Log

### 2026-06-21 — Pre-cert poll_many API improvements from the web trial (stdlib-only, ABI 18 unchanged)
1. **Token-carrying readiness:** `PollEntry`/`PollReady` gain `token: Int` (opaque
   caller value, carried through). Coalesce: duplicate fd entries merge ONLY if token
   matches; same fd with different tokens → `invalid-argument`. Pure stdlib (token
   lives in the parallel arrays; runtime untouched). Tests: token round-trip
   (readiness) + token-conflict.
2. **Strengthened docs:** explicit edge-backed operational contract — drain reads to
   WOULD_BLOCK/EOF/error (or app fairness cap); write to WOULD_BLOCK/empty; a partial
   read/write may not get a second wake for already-buffered readiness; HUP/ERR sticky.
3. **Partial-drain regression** (`test_partial_drain_single_wake`): 16 KiB drained
   across repeated 4 KiB reads after ONE poll_many wake (also exercises write_bytes).
4. **`io.buffer_reset`** (= set_len 0, no boundary) + **`TcpStream.write_bytes(&mut
   Array<Byte>, off, len, timeout)`** — zero-copy range write over EXISTING intrinsics
   (`array_byte_as_mut_ptr` + `mem.ptr_offset`), **no new runtime/compiler boundary**.
   WART flagged: takes `&mut src` because the only Array<Byte>→ptr intrinsic is mutable
   (bytes not modified); a clean `&Array` version would need a new `array_byte_as_ptr`
   intrinsic = boundary, deferred. New `NET_ERROR_KIND_INVALID_ARGUMENT`.
- **Validated:** Gate B `test_poll_many.py` **12/12 functional** (incl. token ×2 +
  partial-drain); net/io regression **13 passed**. ABI stays 18 (no runtime change).
- Did NOT start io_uring / fused-read / accept_many (post-cert design, per instruction).

### 2026-06-21 — F4 + F5 design prep (design-only, no code) — actionable once F3 lands
Per request: produced two implementation-ready plans while F3 goes through cert.
- **`F4-fairness-plan.md`** — root-causes the §4.A starvation to the reactor's
  direct-resume (swapcontext) bypassing the already-FIFO ready queue. Recommends
  ready-age FIFO fairness (gate direct-resume on `ready_count==0`, else enqueue) over
  multi-worker. Spells out invariants, the 7 affected wake paths (only edge-delivery
  changes), deterministic tests incl. a fail-today/pass-after reproducer + probes,
  **no ABI bump**, smallest slice (~30-50 LOC, 2 edge-delivery sites + counter),
  env-flag rollback.
- **`F5-executor-lifecycle-plan.md`** — `Executor.shutdown(Drain/Cancel, timeout)` +
  reactor poller decoupling (root cause: poll ownership gated to the default single
  worker via `threads_count != 1`). Public API, lifecycle states × VT states, who
  polls/owns wakeups, shutdown semantics (drain vs cancel, timeout, parked-I/O,
  blocking-pool, teardown ordering), ABI bump (new exec intrinsics), 7 leak/UAF/
  valgrind gates, 2 slices (shutdown+join; then reactor decoupling). Separate branch,
  after F4.
- Both kept design/read-only; F4/F5 NOT started on the F3 branch.
- **Review refinements applied (same day):** F4 — reuse existing `DriftExec.queue_len`
  (confirmed = ready-queue length under `exec->mu`; no duplicate `ready_count`); made
  the direct-resume gate's locking explicit (§2.1a: decide under target `exec->mu`,
  nested in `r->mu`); fixed the core repro so the hot fiber actually PARKS each
  iteration (eventfd drain→register→peer-rearm) to exercise direct-resume. F5 — DECIDED
  v1 Destructible = explicit-mandatory shutdown, no blocking destructor (best-effort
  non-blocking cancel only); reinforced firm sequencing F3 cert→F4→F5.1→F5.2 and "not
  one broad patch"; clarified the ABI bump lands at F5 Slice 1 (`exec_shutdown`
  intrinsic).

### 2026-06-21 — F3 review round 3: cancel self-reclaim stale park_token (fixed)
- **High (stale token):** `reactor_wait_park`'s cancel self-reclaim (PARKED→RUNNING →
  return CANCELLED) didn't clear `park_token` that the cancelling unpark may have
  latched in the RUNNING window. **Fixed:** `atomic_store(&h->park_token, 0)` in the
  self-reclaim branch (same discipline as `drift_thread_unpark` when it enqueues).
- **Test note:** the fix is correct hygiene but NOT behaviorally observable via Drift
  APIs — a cancelled VT's `cancelled` flag already short-circuits every subsequent
  park (its `conc.sleep` aborts via cancellation; `join()` returns Err so the
  duration isn't retrievable). Empirically confirmed (join→Err). The requested
  "cancel-then-sleep, assert not early" test conflicts with cancellation semantics
  (a cancelled sleep SHOULD abort), so no discriminating regression is constructible;
  fix verified by inspection. Cancel correctness (no hang) stays covered by
  `test_poll_many_cancel_no_hang`. Gate B **10/10**.

### 2026-06-21 — F3 review round 2: cancel lost-wake + forget_vt pending + doc (fixed + validated)
- **High (cancel lost-wake):** `drift_thread_cancel` sets cancelled=1 then
  `drift_thread_unpark`, which can only set a park_token (ignored by the no-token
  wait-set park) if the VT is RUNNING in the cancelled-check→PARKED-publish window →
  VT could sleep to timeout. **Fixed:** `reactor_wait_park` re-checks `cancelled`
  AFTER publishing PARKED and reclaims its own PARKED→RUNNING (returns CANCELLED) or,
  if a resumer already claimed it, falls through to an immediate resume. Test
  `test_poll_many_cancel_no_hang` — 20× spawn/cancel/join on a no-deadline poll
  (half racing the park), asserts no hang. **PASS.**
- **Medium (forget_vt pending):** `forget_vt` no longer clears `pending_read/_write`
  when removing a VT's slots — pending is durable (I1) and must survive VT/episode
  death for ET replay, matching `reactor_wait_clear`.
- **Low (doc):** `reactor_wait_collect_pending` intrinsic doc now states read/write
  are consumed but HUP/ERR are sticky/non-consuming (don't reintroduce consuming).
- **Validated:** `test_poll_many.py` **10/10 functional** (incl. cancel); cancel/
  vt-destroy/registry/conc/net regression **48 passed, 1 skipped**.

### 2026-06-21 — F3 `_block_on_io` migration + 2 review corrections (done + validated)
Migrated BOTH `_block_on_io` copies (io.drift, net.drift) to the shared wait-set;
fixed 2 review blockers; added EPOLLRDHUP.
- **Migration:** `_block_on_io` now uses the poll_many loop shape (N=1, result
  discarded): register → loop{ collect_pending (CONSUMING) → break if ready/timeout/
  cancel → reactor_wait_park } → clear. Closes the register→park lost-wake window the
  no-token edge path introduced.
- **Correction 1 (busy-spin):** the first migration only peeked (reactor_wait_park
  doesn't consume) → a drained-to-EAGAIN retry could spin on stale pending. Fixed by
  consuming via `reactor_wait_collect_pending` in the loop. New probe
  `reactor_park_blocks` + test `test_block_on_io_no_stale_pending_spin` (asserts the
  2nd read actually PARKED, not spun) — deterministic.
- **Correction 2 (HUP/ERR consumption):** made `pending_hup`/`pending_err`
  **non-consuming/sticky** in collect (terminal fd conditions; a 2nd same-fd waiter
  can't miss them). Added **EPOLLRDHUP** to watch masks + edge delivery so peer
  half-close surfaces as hangup. Test `test_poll_many_hup_non_consuming` (sticky
  across two polls).
- **Zero-interest rejection:** poll_many rejects an entry with neither read nor write
  → `Err(invalid-argument)` before registering (no hang). Test (finite + no-deadline).
- **Validated:** `test_poll_many.py` **9/9 functional pass**; full net/io +
  concurrency/VT/reactor regression through the migrated `_block_on_io` **57 passed,
  1 skipped**.
- **Remaining:** Gate B valgrind; full Gate A e2e suite (~1h); optional cancel +
  stale-epoch-probe tests. Perf note: migrated `_block_on_io` does 1 malloc (io_regs
  node) per IO wait — flagged for the hot path.

### 2026-06-20 — F3 IMPLEMENTATION (code) — runtime + stdlib + Gate B working; migration/Gate-A pending
Design cleared; implemented per guardrails. ABI 17→18, DRIFTC_VERSION 0.33.47→0.33.48.
- **Runtime C** (`posix/thread_runtime.c`): DriftIoReg + DriftVt.{wait_epoch,io_regs};
  ReactorWatch.{read_epoch,write_epoch,pending_hup,pending_err}; ReactorTimer.{epoch,
  wait_set}. Both edge-delivery loops: always-set-pending (I1) + epoch-gated claim (I2)
  + timer removal on wake + stale-epoch-drop probe. W7 timer-expiry uses no-token claim
  path for wait-set timers (`fire_timer`). `forget_fd`: claim+enqueue waiters UNDER
  r->mu + set io_regs.closed (lifetime-safe, reuse-safe). `forget_vt`: free io_regs.
  5 intrinsics: vt_wait_epoch_begin, reactor_wait_register (status+rollback),
  reactor_wait_clear, reactor_wait_collect_pending (mask), reactor_wait_park
  (peek+timer+publish-PARKED, no token, re-derive reason). Probes:
  reactor_stale_epoch_drops, reactor_close_unparks. Compiles clean.
- **Codegen** (`llvm_codegen.py`): declares + lowering for all 7. **thread.drift**:
  @intrinsic decls + exports.
- **stdlib** (`std.io`): PollEntry/PollReady (pub fields), `poll_many` driver loop
  (coalesce, register+rollback→Err, collect mask, precedence cancel>ready>timeout,
  reactor_wait_clear on every exit). Exported. (No bitwise on Int — arithmetic helpers.)
- **Verified end-to-end:** intrinsic smoke (rc=0); `poll_many` TCP readiness (rc=0);
  Gate B `test_poll_many.py` — **6/6 functional PASS**: invalid-fd finite-timeout (fast
  Err, not timeout), invalid-fd no-deadline (no hang), empty-list, multi-fd readiness
  (only ready fd), **timeout-leaves-no-token** (sleep after timeout takes full time),
  peer-close→hangup.
- **Regression validated:** Gate A net/io driver subset **16 passed**; concurrency/VT/
  reactor/timer/cancel/executor/futures core **41 passed, 1 skipped** — the
  edge-delivery (always-set-pending) + W7-timer + park-core changes did NOT break
  existing IO or the scheduler. `PollReady.err` named `err` (`error` is reserved).
- **PENDING (final phase):** `_block_on_io` migration to 1-entry `_wait_set` (guardrail
  step 4 — primitive tests now pass); full Gate A e2e suite (~1h); Gate B valgrind +
  cancel + stale-epoch-probe tests.

### 2026-06-20 — F3 design refinement #4 (no code) — registration-failure path + minor, awaiting re-review
Re-review accepted the park protocol / timer ownership / cancel precedence / forget_fd
redesign; flagged one High registration-path gap + 2 minor. Fixed in `F3-multifd-plan.md`:
- **High (invalid-fd silent hang) — FIXED.** `reactor_wait_register` now **returns a
  status** (0 / `epoll_ctl(ADD)` errno) and **rolls back its own partial watch/node on
  failure** — no orphan watch with no epoll registration (§1.3). `_wait_set` checks the
  status per entry: on failure it **`reactor_wait_clear`s all already-registered entries
  and returns `Err(invalid_argument)` BEFORE any park** (§1.4), so
  `poll_many([bad_fd], no_deadline)` returns immediately instead of hanging. Policy
  (§2): registration failure = terminal Err for the whole call (rejected per-fd hangup —
  kept distinct from the "closed-while-waiting ⇒ hangup" path of §3.1). Gate-B tests
  added: #9 invalid-fd+finite-timeout → Err immediately (not timeout), #10 invalid-fd+
  no-deadline → no hang, #11 mixed one-bad-fd → all rolled back (`vt_io_reg_count==0`,
  `reactor_active_slots==0`); old stress test renumbered #12.
- **Minor — FIXED.** §1.3 `reactor_wait_clear` bullet now states it removes the wait-set
  timer (matches the §1.4 loop comment); removed a redundant duplicate bullet.
  `reactor_close_unparks` wording already "claim/enqueue under r->mu" (kept).
- ABI 18 list updated (`reactor_wait_register` returns status). Still no runtime code.

### 2026-06-20 — F3 design refinement #3 (no code) — 2 park-protocol gaps + medium, awaiting re-review
Re-review confirmed the prior 3 fixes; flagged 2 High park-protocol gaps + 1 medium.
All addressed in `F3-multifd-plan.md` (design-only):
- **Gap 1 (timeout token) — FIXED.** `reactor_wait_park` now **owns the deadline
  timer**: peek pending/closed/cancelled + register epoch-stamped timer + publish
  `state=PARKED` all in ONE `r->mu` critical section. **Timer expiry uses the same
  wait-set claim path** (state CAS on a PARKED VT, no `drift_thread_unpark`, no token),
  and every wake (IO/cancel/spurious/timeout) removes the timer so no late timer wakes
  the next episode (timer also epoch-stamped as belt-and-braces). `reactor_wait_register`
  no longer takes/registers a deadline — single timer authority (§1.3/§1.6).
- **Gap 2 (ignored park reason) — FIXED.** `_wait_set` loop now handles
  `reactor_wait_park`'s `rc` explicitly with documented **precedence
  cancellation > readiness > timeout**: cancel tested first each iteration +
  `rc==CANCELLED` breaks immediately (no post-collect); `rc==TIMEOUT` does one final
  collect (readiness racing the deadline wins); `rc==WOKEN` loops (§1.4). Added Gate-B
  tests 3 (2nd park after timeout must not return early — proves no token), 3b
  (readiness-at-deadline wins), 4b (cancel beats readiness).
- **Medium — FIXED.** `reactor_close_unparks` reworded to "claimed+enqueued under
  `r->mu`" (not token); removed the duplicate Gate-B test #1.
- Still no runtime code; ABI projected 18.

### 2026-06-20 — F3 design refinement #2 (no code) — 3 review blockers fixed, awaiting re-review
Reviewer found 2 blocking + 1 medium issue in the refined plan; all addressed in
`F3-multifd-plan.md` (still design-only):
- **Issue 1 (stale wake-token) — REDESIGNED, no token.** The "deposit a generic
  `park_token` on claim-fail" was unsafe (a terminal-READY collect can leave the
  token to short-circuit the next unrelated park — the exact class the runtime warns
  about). Replaced with a **no-token peek-park protocol** (`reactor_wait_park`, §1.6):
  under `r->mu` it peeks `pending`/`cancelled`, else publishes `state=PARKED` *inside
  the same `r->mu` hold*; edge delivery claims ONLY an already-PARKED VT. Full
  no-lost-wake / no-stale-token proof (the mutex=`r->mu`, condition=`pending`,
  futureword=`state` handshake).
- **Issue 2 (forget_fd VT lifetime) — REDESIGNED.** Old draft captured the waiter
  under `r->mu`, freed the watch, unlocked, then unparked — losing the lifetime
  barrier (`forget_vt` could free the VT after the slot is gone). Now `forget_fd`
  **claims+enqueues the waiter UNDER `r->mu`** (the existing dispatch barrier;
  `forget_vt` is blocked on `r->mu`, can't free), sets the VT's `io_regs` node
  `closed` (durable terminal signal that also defeats fd-number reuse), then frees the
  watch. Nothing dereferences a VT after unlock (§3.1).
- **Medium (collection shape) — PINNED.** New intrinsic
  `reactor_wait_collect_pending(fd, interest) -> Int` mask
  (READABLE/WRITABLE/HANGUP/ERROR/CLOSED), consuming, under `r->mu` (§1.4a); replaces
  boolean `reactor_check_pending` on the wait-set path.
- Side effects: added `io_regs.closed` field (§1.1); cancellation must wake a
  wait-set-parked VT via the reactor claim path (not the generic token) to interop
  with `reactor_wait_park` (§3); ABI-18 intrinsic list updated (+`reactor_wait_park`,
  +`reactor_wait_collect_pending`); 2 new reviewer questions (new park intrinsic;
  cancel-via-claim obligation). Swept the doc for stale token/`vt_park*` references.
- Still no runtime code; ABI projected 18.

### 2026-06-20 — F3 design refinement pass (no code) — areas 1–6 tightened, awaiting review
Refined `F3-multifd-plan.md` per reviewer's 6 asks; runtime code still NOT started.
- **Ready handoff (area 2) — decided: pending flags (I1).** The crux: edge delivery
  today zeroes `read_vt` and direct-resumes WITHOUT setting pending, so a readiness
  *reporter* (which doesn't drain the fd) can't tell which fd fired. Fix: **two
  invariants** — I1 pending is the durable, epoch-INDEPENDENT readiness record (edge
  delivery ALWAYS sets it, even on the wake path); I2 the epoch gates only the WAKE
  (claim), never the data. Rejected per-VT ready buffer (dup of pending) and
  conservative-ready (can't say which fd for N>1).
- **Ordering proof (area 1):** 5-window state machine (before/during park, after-wake-
  before-collect, during collect, during/after clear) — proven no legitimate event is
  lost (deferred to next wait via ET-replay at worst), no spurious wake delivered.
  Key fixes vs the prior draft: **collect BEFORE clear** (so `io_regs` still populated)
  and **collect-before-park loop** with **token-on-claim-fail** to close the
  collect/park lost-wake gap. Replaces the now-wrong "clear then check_pending".
- **Cleanup (area 3):** timeout/spurious/cancel all clean via the post-loop
  `reactor_wait_clear`; **cancellation backstop** = extended `forget_vt` frees
  `io_regs` for the scheduler's kill-at-dispatch (no post-park code) case.
- **fd close (area 4):** `forget_fd` token-unparks slot waiters (deferred, never
  direct-resume); waiter's collect sees "registered fd with no watch ⇒ `hangup`";
  reuse-safe via DEL+free; UAF-free via `r->mu` serialization (full argument in §3.1).
- **Migration (area 5) — decided: one slice.** Ready handoff is solved, so both
  `_block_on_io` copies migrate to 1-entry `_wait_set` in the same slice; full net/io
  e2e is the equivalence gate. Step-1/Step-2 split kept only as a fallback.
- **Determinism probes (area 6):** `reactor_stale_epoch_drops`, `reactor_active_slots`,
  `vt_io_reg_count`, `reactor_close_unparks` (`@test_build_only`) + a C-level epoch unit.
- ABI still projected **18**; no runtime code until this is reviewed.

### 2026-06-20 — DIRECTION CHANGE: pull public single-fd `io.poll`; F3 wait-set is the first public readiness API
- Rationale (from product): a public single-fd poll teaches the wrong server pattern
  (one fd at a time). Nothing is certified/released and users are internal, so we
  change course now. Phase 1's shipping surface is **`conc.yield_now()` only**.
- Done:
  - Kept `conc.yield_now()` (unchanged) + its tests.
  - **Removed** `std.io.poll` (export + function + `_interest_code`/`_interest_of`/
    `ERRNO_ETIMEDOUT` helpers) from `stdlib/std/io/io.drift`. Pre-existing error-kind
    constants left intact. No private stepping-stone kept — the design is preserved
    in `F3-multifd-plan.md` + git history and re-authored fresh as the wait-set
    primitive (avoids dead/divergent code).
  - Removed `test_concurrent_yield_poll.py`; added `test_concurrent_yield_now.py`
    (yield_now functional + valgrind only). **PASS** (io.drift recompiles clean).
  - **Kept `TcpListener.raw_fd()`** — the wait-set `PollEntry` is raw-fd based and
    listener accept-readiness is a core use case; updated its docstring to reference
    the forthcoming wait-set API instead of the removed `io.poll`.
- Next phase: **F3 unified wait-set / `poll_many`** as the first public readiness
  API (design `F3-multifd-plan.md`, ABI 18, review before any runtime code).
- ABI: still **17** (yield_now only). DRIFTC_VERSION stays 0.33.47 (yield_now is the
  shipped change).

### 2026-06-20 — review findings 1/2/3 addressed (still 0.33.47, ABI 17)
- **F1 (high): `poll(timeout <= 0)` returned immediately instead of parking.**
  `vt_park_until(0)` returns at once; switched the no-deadline branch to `vt_park(0)`
  (matching `conc.block_on_io`). Also added a re-check of `reactor_check_pending`
  AFTER `register_io` (clearing the just-set waiter on hit) so an edge arriving in
  the register window can't make a `vt_park(0)` block forever. Test
  `test_poll_no_deadline_parks_until_ready` (peer writes ~100ms in; asserts poll
  parked >= 50ms then woke Ok) **PASS** — discriminates the bug.
- **F2 (high): stale reactor waiter on timeout/spurious wake.** After every park,
  `poll` now calls `reactor_register_io(fd, code, 0, 0)` (vt=0 clears the direction,
  no new runtime surface) before deciding the result, so a timeout/spurious wake
  leaves no stale `read_vt`/`write_vt` back-pointer. Confirmed real at the C level:
  the W7 timer-expiry path only `drift_thread_unpark`s (never clears the watch), and
  `drift_vt_claim_for_resume` is a bare PARKED→READY CAS with no `wait_id` guard — so
  a stale back-pointer CAN claim a VT parked elsewhere. Test
  `test_poll_timeout_clears_stale_waiter` added as the cleanup harness + functional
  guard; the spurious-wake race is not deterministically forceable on the single
  cooperative worker (passes with/without the clear there), but the clear is the
  correct defense and future-proofs F4 multi-worker (documented in the test).
- **F3 (medium): `TcpListener.raw_fd()` added** (`net.drift`), mirroring
  `TcpStream.raw_fd()`; the `poll` doc's listener-readiness guidance is now valid.
  No runtime symbol; ABI 17. Direct test `test_poll_listener_accept_readiness`
  (poll the LISTENER fd for Read → connecting peer wakes it → `accept()` succeeds)
  pins the documented server use case. **PASS.**
- **Verification:** full `test_concurrent_yield_poll.py` — **9 passed** (7 functional
  incl. no-deadline + stale-waiter + listener-accept-readiness; 2 valgrind), leaks=0.

### 2026-06-20 — F1 implemented (yield_now + single-fd poll), 0.33.47, ABI 17
- `std.concurrent.yield_now() nothrow -> Void` — wraps `thread.vt_yield()`; exported.
  Docs off-VT behavior (= `vt_yield` → `sched_yield`).
- `std.io.poll(fd, interest, timeout) nothrow -> core.Result<IoInterest, IoError>`
  — single fd/direction; requires VT (off-VT → `requires_vthread`); Read→1, Write→4;
  ET-replay (`reactor_check_pending`) → register → `vt_park_until`; readiness vs
  `timeout` distinguished; conservative-ready (no re-park; caller confirms). Exported.
  Documented: NOT multi-fd; NOT a readiness guarantee (confirm with the I/O); ONE
  waiter per fd/direction — concurrent poll of the same fd/direction is unsupported
  (last waiter wins; cannot reject cleanly without new runtime state — review note).
- **Review note resolved:** duplicate-waiter rejection needs new runtime state →
  out of scope; documented "last waiter wins / unsupported" in the `poll` doc.
- **Verification:** `lang/tests/driver/test_concurrent_yield_poll.py` — yield_now
  two-VT handoff + not-1ms-sleep (4 functional tests **PASS**); poll readiness,
  timeout (distinct kind), pending-edge replay all **PASS**; valgrind variants for
  yield_now + poll-readiness (`--fair-sched=yes`) **PASS** (leaks=0).
  Off-VT `requires_vthread` path is by-inspection (user code can't run off-VT).
- **ABI: 17** (both over existing intrinsics — `vt_yield`, `reactor_*`,
  `vt_park_until`; no new runtime exported symbols). `DRIFTC_VERSION` → 0.33.47.
- F3 multi-fd remains **BLOCKED** on the reactor refactor (see table + PLAN F3).

### 2026-06-20 — plan pinned; runtime ground-truth established
- Read the request and the runtime. KEY findings (see PLAN.md "Runtime ground truth"):
  - `thread.vt_yield()` already implements the requested `yield_now()` semantics
    (`drift_thread_yield`: re-enqueue self on ready queue + swapcontext to scheduler,
    no timer). F1a is a ~1-line stdlib wrapper. **ABI 17.**
  - `conc.sleep(0)` returns `Err(TIMEOUT)` immediately (never yields) — explains the
    team's busy-spin.
  - Single-fd reactor wait template is `io.drift:750 _block_on_io`
    (`reactor_check_pending` ET replay → `reactor_register_io` → `vt_park_until`).
    F1b (`io.poll`) is buildable over these existing intrinsics. **ABI 17.**
  - **Multi-fd (F3) determined UNSAFE on the current reactor:** `drift_reactor_forget_vt`
    (full per-VT registration cleanup) runs ONLY from `drift_vt_destroy`, not on
    park-return; a VT on N fds that wakes on one leaves stale `read_vt` back-pointers
    on the others + carries a single `wait_id`. Multi-fd requires a reactor refactor
    (per-VT wait-set + cleanup-on-every-resume + epoch guard + new intrinsics → ABI
    bump). Matches the user's "only if safe per-VT multi-wait cleanup exists" gate.
- Verification: design only (no code yet). Next step = implement F1a + F1b + tests.
- ABI: F1 = 17 (no new runtime symbols). F3/F5 = bump when implemented.
