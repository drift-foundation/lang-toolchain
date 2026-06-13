# Reload Substrate — Slice 3 Implementation Plan

**Status:** IMPLEMENTED + TESTED — 2026-06-12 (5 review rounds folded in; see the
static-review section). Toolchain bumped to DRIFTC_VERSION 0.33.32 /
DRIFT_RT_ABI_VERSION 17. On branch `feature/reload-substrate`, uncommitted.
**Toolchain at plan time:** DRIFTC_VERSION 0.33.31, DRIFT_RT_ABI_VERSION 16.

**Progress (all parts done, validated):**
- Part 1 (SIGUSR1 + `await_signal` abort): `test_signal_await.py` 6/6.
- Part 2 (`std.fs.read_dir(path, timeout)`, offloaded, distinct error kinds):
  `test_std_fs_read_dir.py` 11/11 (incl. timeout/cancel/saturation lifecycle +
  carrier-liveness + memcheck), leak-clean.
- Part 3 (reload coordinator): `doc/design/reload-substrate.md` +
  `test_reload_coordinator.py` 1/1.
- Part 4 (ABI 16→17, DRIFTC 0.33.31→0.33.32): `test_abi_version_stamp.py` 21/21.
- Regression: DNS + net/concurrency e2e 110/110 (shared blocking-pool refcount).
- Docs: stdlib IO contract (`drift-stdlib-spec.md`) + two mechanisms
  (`drift-concurrency.md`) + `std.io` regular-file follow-up audit recorded.
- There is no separate progress file; this plan is the single work doc.

## Goal

Land the three substrate pieces a config/plugin **reload coordinator** needs, plus
the analysis to land them safely:

1. `ProcessSignal::User1` (SIGUSR1) delivered through the existing
   signalfd → reactor path, so a Drift program can `await_signal()` on SIGUSR1.
2. A deterministic `std.fs.read_dir()` with explicit symlink and error semantics
   (the reload trigger scans a directory to discover new state).
3. The reload-coordinator protocol, documented: the signal VT *only* sends a
   channel notification; a worker VT stages, verifies, and atomically swaps.
4. Regression, ABI/version, and platform-support analysis for the above.

This slice composes **existing** primitives wherever possible. The only genuinely
new runtime surface is the `std.fs` directory-walk C wrappers (§2). Signals reuse
the existing `drift_signal_await` boundary unchanged (§1). The coordinator (§3) is
pure Drift over channels + `Arc<Mutex<…>>` + RAII.

---

## Part 1 — `ProcessSignal::User1` via signalfd / reactor

### What already exists (ground truth)

- `pub variant ProcessSignal { Interrupt, Terminate }` —
  `stdlib/std/concurrent/concurrent.drift:245`.
- `pub fn await_signal() nothrow -> ProcessSignal` —
  `concurrent.drift:2286` — calls `thread.signal_await()` and maps the returned
  signo (`15 → Terminate`, else `Interrupt`).
- Intrinsic `@intrinsic pub fn signal_await() nothrow -> Int` —
  `stdlib/lang/thread.drift:211`. Lowered in `llvm_codegen.py` to
  `call i64 @drift_signal_await()`.
- `int64_t drift_signal_await(void)` —
  `lang/language_runtime/posix/thread_runtime.c:2298`. Registers the calling VT
  as the **single** signal waiter via CAS on `drift_signal_waiter_vt`, wakes the
  reactor, parks, and returns `drift_signal_delivered_signo` (the raw signo) on
  wake. Returns `-1` off-VT / no-signalfd / second-waiter / non-Linux.
- Process-wide mask + signalfd creation —
  `thread_runtime.c:2503-2506`: `sigaddset(SIGINT)`, `sigaddset(SIGTERM)`,
  `sigprocmask(SIG_BLOCK, …)`, `signalfd(-1, &mask, SFD_NONBLOCK)`.
- Reactor dispatch is **generic on `ssi_signo`** —
  `thread_runtime.c:707-714` and `1206-1218`: reads `signalfd_siginfo`, stores
  `si.ssi_signo`, unparks the waiter. No per-signal branching.
- **No conflict with SIGUSR2**: SIGUSR2 is the liveness interrogator, consumed by
  a *dedicated `sigwait` thread* (`liveness_runtime.c:380`), not signalfd. SIGUSR1
  is unused and free for the signalfd path.

### Changes required (small, additive)

1. **C runtime** (`thread_runtime.c`, the mask setup ~2503): add
   `sigaddset(&mask, SIGUSR1);` to the same `sigprocmask`/`signalfd` mask. This is
   the only C change — dispatch (`ssi_signo`-generic) and `drift_signal_await`
   need **no change**. Update the `signal_fd` comment at line 333 / 350 to read
   "SIGINT/SIGTERM/SIGUSR1".
2. **Stdlib** (`concurrent.drift`):
   - Add `User1` to `ProcessSignal` (variant at :245).
   - Extend `await_signal()` (:2286): `if signo == 10 { return ProcessSignal::User1(); }`
     before the `Interrupt` default. Use a named constant
     (`SIGNO_SIGUSR1: Int = 10`) rather than a bare literal, mirroring the existing
     `15` check; document the Linux signal numbers.
   - Update the doc comment block (:2282-2283) to list SIGUSR1 → User1.
3. **Fix `await_signal()` to honor its documented abort contract (Finding 2 —
   in scope, NOT deferred).** The doc (`concurrent.drift:2258-2275`) promises:
   "A second concurrent caller … is a hard program error and aborts the process
   with a diagnostic … Misuse aborts." The implementation instead maps the
   runtime's `-1` (off-VT / no-signalfd / second-waiter / non-Linux) to
   `ProcessSignal::Interrupt()` — a silent contract violation. Fix: when
   `thread.signal_await()` returns `-1`, **abort with a diagnostic** (the
   "infrastructure/second-waiter failure" message), not a fabricated `Interrupt`.
   Keep `nothrow` (abort is not a recoverable exception, per the doc). Add a
   second-waiter **driver regression** (two VTs both calling `await_signal()` → the
   second aborts with the diagnostic). _Note:_ this is a behavior change that makes
   the function match its own contract; call it out in `history.md`.

### Semantics carried over / corrected

- **Single waiter**: at most one VT may be parked in `await_signal()` at a time
  (CAS at `thread_runtime.c:2309`). The reload coordinator's signal VT is that one
  waiter — see §3. Second-waiter / infra failure now **aborts** (Finding 2 above),
  matching the doc.
- **Standard signals COALESCE (Finding 5 — corrects the earlier "not coalesced"
  claim).** signalfd merges multiple pending deliveries of the same standard
  (non-realtime) signal into a **single** `signalfd_siginfo`: N rapid SIGUSR1s may
  surface as **one** `await_signal()` return. Therefore the reload trigger is an
  **idempotent "rescan current state" signal, not a one-event-per-signal command**
  (see §3). The existing `await_signal` doc comment (`concurrent.drift:2275-2278`)
  asserting "not coalesced or queued" is **also wrong for standard signals** and
  should be corrected in the same edit (it is true only in the trivial
  one-signal-at-a-time case).
- **Edge-triggered, buffered in kernel**: a SIGUSR1 delivered before a waiter parks
  stays in the signalfd buffer and is delivered on the next park (but see
  coalescing — only one delivery survives per standard signal).

### Non-goals (Part 1)

- No real-time signals (SIGRTMIN+n), no `sigaction` user handlers, no per-signal
  multiplexing beyond the existing single-waiter model.

---

## Part 2 — Deterministic `std.fs.read_dir()`

### What already exists (ground truth)

- **No `std.fs` module** yet; filesystem ops live in `std.io`
  (`stdlib/std/io/io.drift`). No `opendir`/`readdir`/`closedir`/`lstat`/`scandir`
  wrappers exist in `io_runtime.c` or `thread.drift`.
- **Error convention**: `pub error IoError { kind: String, code: Int }`
  (`io.drift:96`), `kind = "errno"`, `code = errno`; surfaced as
  `core.Result<T, IoError>` and unwrapped with `.or_throw()`.
- **Iterator traits**: `iter.Iterable<Src, Item, Iter>` /
  `iter.SinglePassIterator<T>` (`stdlib/std/iter/iter.drift:22,33`). `Array<T>` is
  already Iterable (borrow-iteration) — exercised by the for-in matrix
  (`for_in_*`, `std_json_entries_for_in`) landed this cycle.
- **String boundary**: extern receivers take a by-value `DriftString` whose
  refcount stake the callee must release exactly once (`DRIFT_OWNED_STRING` macro,
  `string_runtime.h:40`); `drift_io_open` (`io_runtime.c:16`) is the worked
  example (`drift_string_to_cstr` → `free`).
- `File` is **not** `Destructible` (explicit `.close()`); `Buffer` **is**
  (`io.drift:568`).

### Design decisions

**Module.** Create `stdlib/std/fs/fs.drift` (the user asked for `std.fs.read_dir`).
Reuse the established errno convention by importing `IoError` from `std.io` for the
error type (one errno convention; `read_dir` is an IO op). _Alt considered:_ a
parallel `FsError` to keep `std.fs` free of a `std.io` dependency — rejected for
v1 to avoid duplicating the errno mapping; revisit if `std.io`↔`std.fs` coupling
becomes a problem. **Open item (P2-A):** confirm the module-dependency direction
is acceptable (`std.fs` → `std.io`).

**VT-SAFETY RULE (Finding 7, the load-bearing constraint).** All public IO must be
**VT-safe**: it must **never block a carrier thread**. The whole directory walk —
`opendir`, the `readdir` enumeration, per-entry UTF-8 validation,
`fstatat(…, AT_SYMLINK_NOFOLLOW)`, and `closedir` — runs on **one blocking-pool job**
while the calling VT is **parked**. We do **not** expose synchronous per-entry
`opendir`/`readdir`/`fstatat`/`closedir` intrinsics to Drift: NFS, FUSE, and autofs
can block unpredictably inside any of those syscalls, and directory descriptors are
**not** made reliably nonblocking by epoll, so an inline enumeration would stall the
carrier and every other VT it hosts. The earlier per-entry-intrinsic design is
**withdrawn**.

**Snapshot built off-carrier, decoded on-VT (this is what makes it both VT-safe and
"deterministic").** `read_dir(path)` submits **one** job to the existing runtime
blocking-job pool (§ "Reuse the existing blocking pool" below), parks the calling
VT, and the pool worker performs the entire `open → enumerate → validate → fstatat →
close` sequence, building a **C-owned snapshot** (a flat array of `(name, kind)`) or
producing **one** error (with the close-error precedence already resolved C-side).
The VT resumes with either the complete snapshot handle or that one error. Only
**after wakeup** does Drift do non-blocking, in-memory work: copy each entry name out
as a `DriftString`, build `Array<DirEntry>`, and **sort** by name. Consequences:
- The sort uses Drift's built-in `String` ordering operators (`<`/`>`), which lower
  to `StringCmp` → `drift_string_cmp` — deterministic, locale-independent
  **unsigned-byte** lexicographic comparison (`hir_to_mir.py:3043`,
  `string_runtime.c`). No `string_cmp` intrinsic / `Comparable for String` needed
  (Finding 3). NOT `alphasort(3)` (locale-dependent). `.` and `..` excluded C-side.
- The snapshot decode/sort touches only the materialized C array — **no syscalls**,
  so the per-entry accessor intrinsics are carrier-safe (they read memory, never the
  filesystem). This is the key distinction from the withdrawn design.
- No OS handle is held across the Drift-side decode → no `Destructible`
  directory-iterator, no TOCTOU during iteration.
- Return type: `core.Result<Array<DirEntry>, IoError>`. The caller writes
  `for entry in entries { … }` reusing `Array`'s existing Iterable — no new iterator.
- Trade-off: a large directory is materialized twice transiently (C snapshot, then
  `Array<DirEntry>`); the C snapshot is freed as soon as the decode completes.
  Acceptable for v1 (config/plugin dirs). _Future:_ streaming is deferred.

**Types** (in `std.fs`):
```drift
pub variant FileKind { File, Dir, Symlink, Other, Unknown }
pub struct DirEntry { pub name: String, pub kind: FileKind }   // owns `name`; String self-releases, no explicit Destructible
// timeout is REQUIRED (round 5): explicit deadline, no implicit unbounded wait.
pub fn read_dir(path: String, timeout: conc.Duration) nothrow -> core.Result<Array<DirEntry>, IoError>
```
> Note: the round 2/3 single-arg `read_dir(path)` (deadline 0) and the
> errno-collapsed error mapping below are **superseded by round 5** — the public
> API takes an explicit `timeout`, and saturation/timeout/cancellation are distinct
> `IoError.kind`s (`"saturated"`/`"timeout"`/`"cancelled"`), not `"errno"`. See the
> round 5 note and the final `read_dir` body in `stdlib/std/fs/fs.drift`.

**Explicit symlink semantics via `fstatat` (Finding 4 — no `d_type`, no path concat).**
- `DirEntry.kind` reflects the **entry itself**, never the symlink target.
- The **worker** classifies **every** entry with
  `fstatat(dirfd(DIR*), d_name, &st, AT_SYMLINK_NOFOLLOW)` and maps `st.st_mode`
  via `S_ISLNK/S_ISDIR/S_ISREG/…` → `FileKind`. This is relative to the open
  directory fd (immune to the directory being renamed mid-scan), does not depend on
  non-portable `dirent.d_type`, and reports a symlink as `Symlink` (never resolved).
- A single-entry `fstatat` failure → that entry's `kind = FileKind::Unknown`; the
  listing continues. "Unknown" = "type could not be determined", not "absent".
- Target resolution stays a separate future op (`metadata(path)` via `stat`).

**Filename UTF-8 validation (Finding 1 — POSIX names are arbitrary bytes).**
- POSIX entry names are arbitrary non-NUL byte sequences; a Drift `String` must be
  valid UTF-8, and `drift_string_from_utf8_bytes` (`string_runtime.c:105`) does
  **NOT** validate. The **worker** validates each `d_name`'s bytes *before* storing
  it in the snapshot. The **first** invalid name fails the **whole** call:
  `Result::Err(IoError{kind:"invalid-utf8", code:EILSEQ})`, no snapshot. The result
  object carries a `status` enum (see boundary below); `read_dir` maps the
  invalid-utf8 status to this error. Add `IO_ERROR_KIND_INVALID_UTF8 = "invalid-utf8"`.

**Frozen error semantics + close-error precedence (Finding 6) — resolved C-side.**
The worker computes the **single** final outcome and stores it on the result object,
so Drift never re-derives precedence:
- `opendir` failure → `status = errno`, `code = errno` (ENOENT 2, ENOTDIR 20, …).
- `readdir` failure mid-scan or an invalid-UTF-8 name → that error wins; the partial
  C snapshot is discarded by the worker.
- A per-entry `fstatat` failure is **not** a whole-call failure (→ `Unknown`).
- **Close-error precedence (frozen):** the worker always `closedir`s. Read/validate
  error wins over a close error; a close-only failure returns the close errno; a
  snapshot is **never** handed back after a close failure. Whole-call failures are
  exactly: `opendir`, `readdir`, invalid UTF-8, `closedir`.

### Reuse the existing blocking pool (do NOT build a second one)

Ground truth (`thread_runtime.c`): there is already a bounded blocking-job pool —
`DriftBlockingPool` (`:3067`), `DriftBlockingJob` (`:3057`), **4 fixed workers**
(`DRIFT_BLOCKING_POOL_WORKERS`, `:3079`), **bounded FIFO queue of 64**
(`DRIFT_BLOCKING_POOL_QUEUE_LIMIT`, `:3080`), `drift_blocking_submit` (`:3156`,
returns `-1` when the queue is full or the pool is stopping), worker loop `:3085`,
and the park/unpark primitives `drift_thread_park` (`:1935`) / `drift_thread_unpark`
(`:2093`). Its first consumer, **DNS resolve** in `drift_net_connect` (`:3258`), is
the exact end-to-end template: alloc a `*Job` subtype → `drift_blocking_submit` →
`drift_reactor_register_timer(deadline)` → `drift_thread_park(0)` → on wake, branch
on `completed` (success: cancel timer, consume result) vs. timed-out (set
`expired=1`, abandon to the worker, which frees via `destroy_fn`). `read_dir` becomes
the **second consumer** of this same pool. We **factor/reuse** it; we do **not**
create an ad-hoc second pool (the existing one is already bounded and shutdown-safe).

### C runtime additions (the new boundary surface)

New file `lang/language_runtime/posix/fs_runtime.c` (+ `.h`). It defines a
`DriftReadDirJob` (embedding `DriftBlockingJob`), the worker `job_fn` that performs
the whole walk into a C-owned snapshot, and a small **mutex-protected,
generation-tagged result-handle table** so the parked VT can hand the surviving
snapshot back to Drift across several non-blocking accessor calls.

| Intrinsic (`thread.*`) | C symbol | Signature | Role |
|---|---|---|---|
| `fs_read_dir(path: String, deadline_ms: Int) -> Int` | `drift_fs_read_dir` | `int64_t(DriftString, int64_t)` | **Blocking-offload submit + park.** Returns a result handle `≥ 1` (snapshot OR resolved error, query via accessors), or a frozen negative sentinel: `-1` ENOMEM, `-2` saturation (queue full → backpressure), `-4` timed-out/abandoned. On a VT: alloc job, `drift_blocking_submit`, register `deadline_ms` timer, park. Off-VT (main thread, not a carrier): run inline. |
| `fs_result_status(h: Int) -> Int` | `drift_fs_result_status` | `int64_t(int64_t)` | `0` ok (snapshot present) / `1` errno error / `2` invalid-utf8. (Pure memory read — carrier-safe.) |
| `fs_result_errno(h: Int) -> Int` | `drift_fs_result_errno` | `int64_t(int64_t)` | errno (status `1`) or `EILSEQ` (status `2`). |
| `fs_result_count(h: Int) -> Int` | `drift_fs_result_count` | `int64_t(int64_t)` | entry count (status `0`). |
| `fs_result_name(h: Int, idx: Int) -> String` | `drift_fs_result_name` | `DriftString(int64_t, int64_t)` | UTF-8-validated name of entry `idx` (fresh `DriftString`, owned by Drift). |
| `fs_result_kind(h: Int, idx: Int) -> Int` | `drift_fs_result_kind` | `int64_t(int64_t, int64_t)` | `FileKind` code of entry `idx`. |
| `fs_result_free(h: Int) -> Int` | `drift_fs_result_free` | `int64_t(int64_t)` | Releases the VT's stake on the result/snapshot (see ownership). |

Only `fs_read_dir` ever touches the filesystem, and it does so **on a pool worker**
while the VT is parked. Every `fs_result_*` accessor is a pure in-memory read of the
already-materialized snapshot, so all of them are safe to call on a carrier.

**`DriftReadDirJob` + snapshot (C):**
```c
typedef struct { char *name; size_t name_len; int kind; } DriftDirEntC;
typedef struct DriftReadDirJob {
    DriftBlockingJob base;        // job_fn / destroy_fn / vt / completed / expired
    atomic_int refcount;          // 2 on submit (VT + worker); last release frees
    char *path;                   // owned copy; freed in destroy_fn
    int status;                   // 0 ok / 1 errno / 2 invalid-utf8 (close-precedence resolved)
    int err;                      // errno or EILSEQ
    DriftDirEntC *entries;        // C-owned snapshot (status 0); freed in destroy_fn
    size_t count;
} DriftReadDirJob;
```

**Lifecycle & ownership (UAF-safe by construction — hardens the DNS template).**
The DNS consumer copies its (tiny, fixed-size) result out of the job *before*
`destroy_fn` and frees synchronously on the success path. `read_dir` cannot do that —
the snapshot is large and the VT must read it across multiple accessor calls — so the
job must outlive the `fs_read_dir` return. It also must not be freed twice in the
narrow window where the worker sets `completed` at the same instant the deadline timer
fires. We therefore replace DNS's flag-only handoff with an **atomic refcount = 2**:
- **Submit:** `refcount = 2` (one stake for the VT, one for the worker). `vt`,
  `job_fn`, `destroy_fn`, owned `path` set; `drift_blocking_submit`.
- **Worker:** runs the full walk into `entries`/`status`/`err`, `atomic_store(completed,1)`,
  then `release(job)` (decrement; if it hit 0, free). If it did **not** unpark yet and
  the VT stake is still held, it calls `drift_thread_unpark(vt)` **before** its
  release so it never dereferences `job->vt` after a potential free.
- **VT wake — success (`completed==1`):** cancel the deadline timer; register the job
  in the result-handle table; return the handle. The VT keeps its stake until
  `fs_result_free`, which does `release(job)`.
- **VT wake — timeout/cancel (`completed==0`):** `atomic_store(expired,1)`,
  `release(job)` (drops the VT stake), return `-4`. The worker still holds its stake
  and frees when its syscall finally returns. The abandoned snapshot never reaches
  Drift, and the VT never touches the job again → no UAF.
- **Off-VT inline path:** `refcount = 1`, freed by `fs_result_free` (or immediately if
  it produced an error with no handle). No worker involved.
- The job owns its `path` and `entries` **independently** of any Drift value, so a
  cancelled/dropped `read_dir` frame cannot free memory the worker still uses, and
  vice versa. `destroy_fn` frees `path`, every `entries[i].name`, the `entries` array,
  and the struct exactly once (whoever drops the last stake).

**Cancellation / drop.** VT cancellation (`drift_thread_cancel`, `:2245`) bumps the
park token and unparks; the VT then wakes with `completed==0` and takes the
timeout/abandon path above — identical to a deadline expiry. **A timeout or cancel
abandons the result but cannot portably interrupt an in-kernel filesystem syscall:**
the worker stays blocked in `open`/`readdir`/`close` until the kernel returns, then
releases its stake. This is documented as an explicit limitation.

**Saturation / backpressure.** `drift_blocking_submit` returns `-1` when the bounded
queue (64) is full or the pool is stopping; `fs_read_dir` maps that to the **`-2`**
sentinel and `read_dir` surfaces `IoError{kind:"errno", code:EAGAIN}`. Because the
queue is bounded and there are only 4 workers, a storm of stalled NFS `read_dir`s
cannot grow an unbounded queue or fork unbounded threads — it degrades to `EAGAIN`
backpressure once the 4 workers are occupied and 64 jobs are queued. **Known
limitation (documented, deferred):** this pool is **shared** with DNS resolve, so
sustained directory stalls can also starve DNS; per-category fairness / a larger or
dedicated pool is out of scope for v1. To make the saturation/stall path testable
**portably** (no real NFS in CI), `fs_read_dir`'s worker honors a test-only,
env-gated artificial pre-`opendir` delay (`DRIFT_FS_TEST_STALL_MS`) so a regression
can occupy all workers and assert (a) carrier liveness and (b) `EAGAIN` backpressure.

**String ownership:** `drift_fs_read_dir` takes the by-value `DriftString path` and
releases the stake (`DRIFT_OWNED_STRING` → `drift_string_to_cstr` → the worker frees
the copy), mirroring `drift_io_open`. `drift_fs_result_name` returns a freshly
allocated, already-validated `DriftString` (ownership to Drift).

**`read_dir` body (Drift):** `val h = thread.fs_read_dir(path, deadline_ms);` →
- `h == -2` → `Err(IoError errno=EAGAIN)`; `h == -1` → `Err(IoError errno=ENOMEM)`;
  `h == -4` → `Err(IoError errno=ETIMEDOUT)`; (all negative sentinels map to an
  `IoError`, no handle to free).
- `h ≥ 1` → branch on `thread.fs_result_status(h)`: `1` → build
  `IoError{kind:"errno", code:fs_result_errno(h)}`; `2` → `IoError{kind:"invalid-utf8",
  code:fs_result_errno(h)}`; `0` → loop `idx` in `0..fs_result_count(h)` building
  `DirEntry{name: fs_result_name(h,idx), kind: kind_of(fs_result_kind(h,idx))}` into
  `Array<DirEntry>`. **Always** `thread.fs_result_free(h)` before returning (success
  or error). On success, sort by name (`<` over `String`) and return the snapshot;
  the partially-built `Array<DirEntry>` drops via RAII on any error path.

### Non-goals (Part 2)

- No `stat`/`metadata`/symlink-target resolution, no recursive walk, no streaming
  iterator, no Windows backend, no path-type abstraction (paths stay `String`).
- No second/ad-hoc blocking pool, no per-category pool fairness, no configurable
  worker/queue sizing (reuse the existing bounded pool as-is).

---

## Part 3 — Reload coordinator (documentation + reference)

The deliverable here is **documentation** of the protocol (`doc/design/reload-substrate.md`)
plus a small reference example, both composing existing primitives — no new runtime.

### Existing primitives (ground truth)

- **Channels** (`concurrent.drift`): `channel<T>() -> ChannelHalves<T>` (:1163);
  `Sender<T>.send(var v) -> Result<Void, ConcurrencyError>` (:1192);
  `Receiver<T>.recv() -> Result<T, ConcurrencyError>` (:1237) /
  `recv_timeout(d)` (:1270). Last-`Sender`-drop closes; `recv` then drains and
  returns `Err(CLOSED)`. Move-only halves; share a sender via `Arc<Sender<T>>`.
- **VirtualThread** (`concurrent.drift`): `spawn<T>(var cb) -> VirtualThread<T>`
  (:1602); `join`/`join_timeout`/`cancel`/`is_complete`.
- **Shared mutable state**: `Arc<Mutex<T>>` (Arc `arc.drift:139`, Mutex
  `concurrent.drift:495`); atomic swap via `mem.replace(&mut *guard, new) -> old`
  (`std/mem/mem.drift:155`) — the exact pattern the channel `Receiver` destructor
  uses to detach under-lock and drop outside the lock (:1346).
- **RAII**: a staged-but-not-published value is a normal local; on verify-failure
  `return Err(…)` drops it via its `Destructible` — no manual free.

### The protocol (what the doc must state)

1. **Signal VT (notify-only).** A dedicated VT loops:
   `await_signal()` → on `User1`, `sender.send(ReloadRequest{…})` → repeat.
   It does **nothing else** — no FS, no staging, no swap. This mirrors the liveness
   discipline ("minimal work in the signal-triggered path"; `doc/liveness.md:39`)
   and respects the **single-waiter** rule (it is the one `await_signal()` waiter).
   Note: this VT uses the signalfd/reactor path (it *parks*), which is normal VT
   code — not an async-signal-handler context — so calling `send()` is safe.
   **Idempotent-trigger discipline (Finding 5):** because standard signals coalesce
   (N rapid SIGUSR1 → possibly **one** `await_signal()` return), `ReloadRequest` is
   a content-free "rescan current state" trigger, NOT a per-event command. The
   worker re-reads the *current* directory state on each request; dropping a
   request because two signals merged is harmless (the surviving one rescans the
   latest state). **This slice uses the existing unbounded channel as-is** — it has
   no `try_send` or bounded capacity — and simply documents that **duplicate reload
   requests are harmless** (each one rescans the latest state, so redundant requests
   cost only a re-read, never incorrectness). Deduplication / a 1-slot pending-bit
   optimization is **explicitly deferred** (would require new bounded-channel or
   pending-flag surface this slice does not build).
2. **Worker VT (stage → verify → swap).** Receives `ReloadRequest` via `recv()`,
   then:
   - **Stage**: `read_dir(config_dir)` (§2, deterministic) → build the new state
     into a *local* `Staged` value.
   - **Verify**: validate the staged state; on failure `return`/continue — the
     local `Staged` drops (RAII), the live state is untouched.
   - **Atomic swap**: take `Arc<Mutex<State>>.lock()`, `mem.replace(&mut *guard,
     new_state)` to publish and get the `old_state`, release the lock, **then drop
     `old_state` outside the lock** (Receiver-destructor pattern, avoids running
     user destructors under the state mutex).
   Readers elsewhere hold the same `Arc<Mutex<State>>` and see either the whole old
   or the whole new state (single `mem.replace` under the lock = atomic publish).
3. **Failure isolation.** Verify-fail or `read_dir` error never mutates live state;
   the coordinator logs and waits for the next signal. Channel-close (sender Arc
   dropped) ends the worker loop cleanly via `Err(CLOSED)`.

### Reference deliverable

- `doc/design/reload-substrate.md`: protocol, the three rules above, a sequence
  description, and the single-waiter / signal-coalescing / atomic-publish caveats,
  plus the note that the channel is unbounded and duplicate requests are harmless.
- A runnable e2e example under `lang/tests/codegen/e2e/reload_coordinator_*` (also
  doubles as regression — see §4) demonstrating signal → notify → swap with a
  deterministic `read_dir` snapshot.

### Non-goals (Part 3)

- No actual code hot-swap / dynamic linking; "state" is Drift data (config/plugin
  descriptors), not executable code. No multi-directory or debounced reload in v1.

---

## Part 4 — Regression, ABI/version, and platform-support analysis

### ABI / version analysis

- **Signals (Part 1):** reuses `drift_signal_await` with an **unchanged**
  signature; the only C change is adding `SIGUSR1` to the existing signalfd mask
  (runtime-internal). No new/changed boundary symbol from signals alone.
- **`std.fs` (Part 2):** adds **new** extern runtime symbols (`drift_fs_read_dir`,
  `drift_fs_result_status`, `drift_fs_result_errno`, `drift_fs_result_count`,
  `drift_fs_result_name`, `drift_fs_result_kind`, `drift_fs_result_free`) that the
  compiler will emit calls to. A binary compiled against this toolchain requires a
  runtime archive that provides them — the same situation as the `now_us`/`now_utc_us`
  precedent that bumped **ABI 15 → 16** ("new `lang.thread` intrinsics cross the
  compiler/runtime boundary → bump"). New boundary functions ⇒ **bump
  `DRIFT_RT_ABI_VERSION` 16 → 17**. (No `string_cmp` intrinsic is added — Finding 3.
  The per-entry `drift_fs_opendir`/`readdir_next`/`dirent_*`/`closedir` set from the
  withdrawn synchronous design is **not** added — replaced by the one blocking-offload
  submit `drift_fs_read_dir` plus the six carrier-safe `drift_fs_result_*` accessors,
  Finding 7.)
- **Net:** this slice bumps **ABI 16 → 17** and **`DRIFTC_VERSION` 0.33.31 →
  0.33.32** (`lang/versions.py:14-15`). The SIGUSR1 mask change and the
  `ProcessSignal::User1` variant ride the same bump (consumers rebuild through cert
  anyway). Per the project ABI policy: this is an artifact-boundary surface
  addition, so the same-ABI-candidate path does **not** apply — bump both the ABI
  int and DRIFTC_VERSION, and rebuild deps through cert. Add the `history.md` entry
  describing the boundary additions.

### Platform support

- **SIGUSR1 / signalfd / epoll:** Linux-only — identical to the existing
  `await_signal()` (`drift_signal_await` is `#ifdef __linux__`, returns `-1`
  elsewhere). `User1` inherits this; document "Linux only" on the variant, matching
  the existing ProcessSignal doc (`concurrent.drift:239`).
- **`read_dir` (POSIX `opendir`/`readdir`/`fstatat`/`closedir` on a blocking-pool
  worker):** portable across POSIX (Linux/macOS/BSD). The runtime tree is `posix/`,
  so the wrappers live there; the calls are standard POSIX, no Linux-specific gating
  needed. The blocking pool itself (`thread_runtime.c`) is already POSIX. Classification
  never reads `dirent.d_type` — every entry is classified with
  `fstatat(dirfd, name, AT_SYMLINK_NOFOLLOW)` (§2), portable and immune to
  `DT_UNKNOWN`; a per-entry `fstatat` failure degrades that entry to
  `FileKind::Unknown` without failing the call.
- **Windows:** out of scope (the runtime is POSIX-only today).

### Regression plan

- **e2e (full compile/run, + `DRIFT_MEMCHECK=1` on the resource-owning cases):**
  - `signal_await_user1`: extend the SIGUSR1 path of `test_signal_await.py`'s
    fixture (`lang/tests/driver/test_signal_await.py:26`) — `kill -USR1` → match
    `ProcessSignal::User1` → distinct exit code; assert SIGUSR2 still drives
    liveness (no regression / no cross-wiring).
  - `signal_await_second_waiter_aborts` (Finding 2): two VTs both call
    `await_signal()` → the second **aborts with the diagnostic** (process abort,
    non-zero status, expected stderr), proving the contract. Driver-level.
  - `std_fs_read_dir_invalid_utf8` (Finding 1): a directory containing an entry
    with a raw invalid-UTF-8 byte name (e.g. `\xff`) → `read_dir` returns
    `Err(invalid-utf8 / EILSEQ)`, no partial listing. (Fixture creates the name via
    a raw syscall/helper, since the bad name can't be a Drift string literal.)
  - `std_fs_read_dir_basic`: a fixture dir with files + subdir + a symlink; assert
    **deterministic sorted order**, `FileKind` per entry (symlink reported as
    `Symlink`, not its target), `.`/`..` excluded.
  - `std_fs_read_dir_errors`: ENOENT (missing path) and ENOTDIR (path is a file)
    → `Err(IoError errno)`; assert no partial listing.
  - `std_fs_read_dir_memcheck`: leak-clean across success and error paths — the C
    snapshot (`path`, every `entries[i].name`, the `entries` array, the job struct)
    freed exactly once via the refcount; each Drift `DirEntry.name` released; the
    result handle always `fs_result_free`d. Run under memcheck. **Also** exercise it
    **from a spawned VT** (the offload path), not just main, so the park/unpark +
    worker free is covered.
  - `reload_coordinator_smoke`: signal VT → channel → worker stages a `read_dir`
    snapshot, verifies, `mem.replace`-swaps; assert old state dropped exactly once
    (memcheck) and readers observe atomic old-or-new.
  - **`std_fs_read_dir_vt_offload` (Finding 7 — VT-safety):** call `read_dir` from a
    spawned VT while a *second* compute VT increments a shared atomic in a tight loop;
    assert the compute VT keeps ticking across the `read_dir` (its carrier was not
    blocked) and the snapshot is correct. This pins that the walk is offloaded, not
    inline on the carrier.
  - **`std_fs_read_dir_cancel_abandon` (lifecycle/cancellation):** spawn a `read_dir`
    VT against a stalled directory (`DRIFT_FS_TEST_STALL_MS` set high), `cancel()`/drop
    it before the worker finishes; assert (a) no UAF / no double-free (memcheck), (b)
    the worker's later completion frees the abandoned snapshot exactly once, (c) the
    process shuts down cleanly (the abandoned worker's stake is released at join).
  - **`std_fs_read_dir_saturation` (saturation/backpressure + carrier liveness — the
    NFS-stall proxy):** set `DRIFT_FS_TEST_STALL_MS` to stall every worker, fire enough
    concurrent `read_dir` VTs to fill the 4 workers + 64-deep queue, and assert (a) the
    overflow submissions get `Err(errno=EAGAIN)` (bounded queue, no unbounded growth),
    (b) a separate compute VT keeps progressing throughout (carrier liveness under
    stall), (c) once the stall clears, the queued reads complete. This is the portable
    stand-in for a real NFS/FUSE stall.
  - **`std_fs_read_dir_timeout` (lifecycle/deadline):** `read_dir` with a short
    `deadline_ms` against a stalled directory → `Err(errno=ETIMEDOUT)` with no partial
    listing; worker later frees the abandoned snapshot (memcheck clean).
- **driver / structural:**
  - A `std.fs` boundary test asserting the new intrinsics (`fs_read_dir` +
    `fs_result_*`) are declared + lowered (mirror an existing `io_*` lowering test).
  - ABI stamp: extend `test_abi_version_stamp.py` expectations to ABI 17.
- **Broad gates:** `just test-shard-2` (codegen e2e), the io/concurrency driver
  suites, and the new fixtures under memcheck. Confirm `await_signal` SIGINT/SIGTERM
  fixtures and the liveness SIGUSR2 test remain green (no signal cross-talk), and that
  the existing DNS-resolve path (the other blocking-pool consumer) stays green — the
  pool is now shared by two consumers.

### Follow-up audit (recorded, not done in this slice)

**Audit all existing regular-file APIs against the no-carrier-blocking rule
(Finding 7).** `drift_io_open`/`drift_io_read`/`drift_io_write` (`io_runtime.c`) call
`open`/`read`/`write` **inline on the calling carrier**, not through the blocking
pool. On a stalling filesystem (NFS/FUSE/autofs) these can block a carrier and every
VT it hosts — the same violation that motivated the `read_dir` redesign. Regular-file
read/write partially mitigate via reactor/epoll readiness for *sockets*, but a regular
file fd is **not** made nonblocking by epoll, so `read`/`write` on a slow FS still
block. This is a **pre-existing** condition, out of scope here, but must be tracked:
a later slice should route blocking regular-file syscalls (at least `open`, and
`read`/`write` on regular files) through the same blocking pool, or document an
explicit exception. Capture as a project memory + backlog item when this slice lands.

### Sequencing

1. Part 1 (SIGUSR1) — smallest, self-contained; land + test first.
2. Part 2 (`std.fs.read_dir`) — new runtime surface; drives the ABI bump.
3. Part 3 (coordinator doc + reference example) — composes 1 + 2.
4. Part 4 artifacts (version bump, `history.md`, ABI stamp, cert rebuild) — last,
   once the boundary surface is final.

---

## Open items to resolve during static review / implementation

- **P1-A (RESOLVED — Finding 2, now in scope):** `await_signal()` must **abort with
  a diagnostic** on the runtime `-1` (off-VT / no-signalfd / second-waiter), to
  honor its documented "misuse aborts" contract — not silently return `Interrupt`.
  Add a second-waiter driver regression. Not deferred.
- **P2-A:** `std.fs` → `std.io` dependency for `IoError` (vs. a parallel `FsError`).
  *Recommend: reuse `IoError`.*
- **P2-B (RESOLVED — FROZEN):** dir-handle representation is a **mutex-protected,
  generation-tagged runtime table** (not `DIR*`-as-int64). Concurrent `read_dir()`
  calls share the table under one process-wide mutex; handles pack
  `(slot_index, generation)` so a stale handle cannot address a reused slot. See
  "Handle representation (FROZEN)" in §2.
- **P2-C (RESOLVED by static review):** the dir handle is a plain `Int` (Copy),
  so `fs_closedir(handle)` carries **no ownership/borrow constraint** — this is
  ordinary "close on every return path" control-flow discipline, not the
  take-first owned-handle problem. The eagerly-built `Array<DirEntry>` is owned and
  drops via RAII on the error path automatically. Lower risk than first stated.
- **P2-D (RESOLVED — simpler than feared, per review Finding 3):** `String`
  ordering already works — `<`/`>`/`<=`/`>=` lower to `StringCmp` →
  `drift_string_cmp` (deterministic unsigned-byte; `hir_to_mir.py:3030-3047`).
  **No `string_cmp` intrinsic and no `Comparable for String` are needed.** The only
  remaining sort work is the sortable view: `algo.sort_in_place<R,T>`
  (`algo.drift:38`) requires `R is iter.RandomAccessPermutable<T>` with a
  `compare_at(i,j)` (`iter.drift:64`), so provide a small `DirEntryArray` view
  whose `compare_at` returns the sign of `name_i < name_j` (using the built-in
  operators) and permutes the backing `Array<DirEntry>`. Heapsort is **non-stable**
  (`algo.drift:32`) — **fine**, directory names are unique. **Open item:** confirm
  whether a stdlib `Array`-backed `RandomAccessPermutable` view already exists to
  reuse (e.g. an array slice view) before writing `DirEntryArray`.
- **P3-A:** `ReloadRequest`/`State`/`Staged` concrete shapes are example-specific;
  the doc specifies the *protocol*, the e2e fixture picks concrete types.

## Static review (performed before implementation)

Verified the plan's load-bearing claims against the tree. Confirmed accurate:
ProcessSignal enum + signalfd mask + `ssi_signo`-generic dispatch + single-waiter
CAS (§1); SIGUSR2 reserved by a separate `sigwait` thread, no signalfd conflict;
`IoError` errno convention + `DRIFT_OWNED_STRING` boundary + `Array` Iterable +
no `std.fs`/`readdir` wrappers (§2); `channel`/`Sender.send`/`Receiver.recv`/
`spawn`/`Arc<Mutex>`/`mem.replace` all exist as cited (§3).

**Review round 2 (reviewer findings 1–6) — all folded in:**

1. **Invalid-UTF-8 filenames (Finding 1, High).** Confirmed
   `drift_string_from_utf8_bytes` (`string_runtime.c:105`) does not validate. Part 2
   now fails the whole call with `IoError(kind="invalid-utf8", code=EILSEQ)`,
   validating bytes in C before any `DriftString` is built; raw-byte regression
   added.
2. **`await_signal()` contract (Finding 2, High).** Confirmed the doc
   (`concurrent.drift:2258-2275`) promises abort-on-misuse while the impl silently
   returns `Interrupt` on `-1`. **Now fixed in this slice** (abort + diagnostic),
   with a second-waiter driver regression. Not deferred.
3. **No `string_cmp` intrinsic needed (Finding 3, Medium).** Confirmed String
   `<`/`>` already lower via `StringCmp` → `drift_string_cmp` (unsigned-byte;
   `hir_to_mir.py:3030-3047`). The intrinsic + `Comparable for String` work is
   **removed**; only the sort-view remains. ABI 17 still required for `drift_fs_*`.
4. **`fstatat` classification (Finding 4, Medium).** Switched from `d_type` +
   `path+"/"+name` `lstat` to `fstatat(dirfd, name, AT_SYMLINK_NOFOLLOW)` per entry
   — race-free, portable, and it **deletes the `fs_lstat_kind` boundary function**.
5. **Standard signals coalesce (Finding 5, Medium).** Corrected the earlier "not
   coalesced" claim (and the pre-existing wrong doc comment at
   `concurrent.drift:2275-2278`). The coordinator now treats reloads as idempotent
   "rescan current state" triggers; a 1-slot pending flag suffices.
6. **Close-error precedence (Finding 6, Low).** Frozen: read/validate error wins
   over a close error; a close-only failure returns the close error; the snapshot is
   never returned after a close failure.

Also retained from review round 1: handle is `Int`/Copy (no borrow hazard, just
close-on-all-paths); `signo == 10` matches the existing `== 15` house style
(portability nit filed, not blocking).

**Review round 3 (reviewer corrections, all folded in):**

1. **fstatat failure semantics (Medium).** The earlier draft contradicted itself
   (per-entry `fstatat` → `Unknown`, yet listed `fstatat` among whole-call
   failures). Frozen: a per-entry `fstatat` failure degrades only that entry to
   `FileKind::Unknown` and the listing continues. Whole-call failures are **only**
   `opendir`, `readdir`, invalid filename UTF-8, and `closedir`. `fstatat` removed
   from the whole-call list (§2).
2. **`-EILSEQ` ambiguity (Medium).** `fs_readdir_next` could not distinguish an
   invalid-UTF-8 name from a real `readdir` failure whose errno was `EILSEQ`. Frozen
   a disjoint status encoding: `1` entry / `0` end / `-1` syscall failure (errno via
   `fs_dir_error(h)`) / `-2` invalid UTF-8. Negative errno values are no longer
   overloaded with semantic meaning; added the `drift_fs_dir_error` boundary symbol
   (six `drift_fs_*` total, ABI 17).
3. **No one-slot pending channel (Low).** Dropped the "1-slot pending flag" promise.
   This slice uses the existing unbounded channel and documents that duplicate
   reload requests are harmless; dedup / pending-bit deferred.
4. **Handle table frozen (added).** The dir-handle table is **mutex-protected and
   generation-tagged** so concurrent `read_dir()` calls cannot race and stale
   handles cannot address reused slots (P2-B resolved/frozen).

**Review round 4 (architecture — Finding 7, VT-safety; supersedes the round 2/3
per-entry boundary design for Part 2):**

7. **All public IO must be VT-safe — never block a carrier (High, architectural).**
   The round 2/3 design exposed synchronous per-entry
   `fs_opendir`/`fs_readdir_next`/`fs_dirent_*`/`fs_closedir` intrinsics, so the
   `readdir`/`fstatat` enumeration would have run **inline on the calling VT's carrier
   thread**. NFS/FUSE/autofs can block unpredictably inside those syscalls and a
   directory fd is not made nonblocking by epoll, so that would stall the carrier and
   every VT it hosts. **Withdrawn.** Replaced by a single **blocking-offload** design:
   - `read_dir` submits **one** job to the **existing** bounded blocking-job pool
     (`DriftBlockingPool`, 4 workers, 64-deep queue, `thread_runtime.c:3067+`), parks
     the VT, and the worker does the whole `open→enumerate→validate→fstatat→close`
     into a C-owned snapshot (DNS-resolve in `drift_net_connect` is the template). We
     reuse that pool; we do **not** build a second one.
   - The boundary becomes `drift_fs_read_dir` (submit+park; returns a result handle or
     a frozen `-1/-2/-4` sentinel) plus six **carrier-safe** `drift_fs_result_*`
     accessors that read the already-materialized snapshot (no syscalls). The
     round-3 "disjoint status encoding / `fs_dir_error`" concern is subsumed: the
     worker resolves the single outcome (incl. close-error precedence) into
     `status`/`err` on the result object, so there is no `-EILSEQ` ambiguity to begin
     with. The mutex-protected, generation-tagged handle table from round 3 is
     retained, now keyed on the **result** object.
   - Lifecycle/ownership hardened past the DNS template with an **atomic refcount = 2**
     (VT + worker; last release frees) to close the narrow `completed`/`expired`
     double-free window and guarantee no UAF on cancel/drop/timeout.
   - Added regressions: VT-offload (carrier liveness), cancel/abandon, saturation +
     carrier-liveness (portable NFS-stall proxy via `DRIFT_FS_TEST_STALL_MS`), and
     deadline timeout. Recorded a follow-up audit of `drift_io_*` regular-file APIs
     against the same rule.

**Net:** Part 1 (SIGUSR1 + `await_signal` abort) is unchanged and already landed +
green. Part 2 is re-architected around the no-carrier-blocking rule: one offloaded
job on the existing bounded pool, refcounted result, carrier-safe decode. Larger than
round 3 but correct; the existing pool means no new scheduler/threading surface. No
blocker — proceed: re-implement Part 2 against the blocking-pool boundary, then §4.

**Review round 5 (post-implementation review — IO-contract compliance gaps, all
folded in):**

1. **`read_dir` was not timeout-capable (High).** It exposed only
   `read_dir(path)` and passed deadline `0` (implicit unbounded wait), violating
   the contract's explicit-deadline clause. Fixed: the public API is now
   `read_dir(path: String, timeout: conc.Duration)`; it computes an absolute
   monotonic-ms deadline (`now_ms() + timeout.millis`, `0` = explicit no-deadline)
   and passes it to the offload. The coordinator and all tests pass a `Duration`.
2. **Failure modes were collapsed (High).** Saturation and timeout both mapped to
   `kind="errno"`, so callers couldn't tell backpressure from a filesystem error.
   Fixed: distinct `IoError.kind`s — `"timeout"` (ETIMEDOUT), `"cancelled"`
   (ECANCELED), `"saturated"` (EAGAIN), plus `"errno"`/`"invalid-utf8"`. New
   `IO_ERROR_KIND_TIMEOUT`/`_CANCELLED`/`_SATURATED` in `std.io`. The C boundary
   uses disjoint sentinels `-1/-2/-4/-5` and distinguishes cancel from timeout via
   `drift_thread_is_cancelled()` at the abandon point (cancel doesn't preempt a
   pool-parked VT; the deadline timer resumes it, then the flag routes the kind).
3. **Lifecycle regressions added (High).** `test_std_fs_read_dir.py` now pins:
   `timeout` (distinct kind, prompt abandon), `cancel_abandon` (distinct
   `cancelled` kind via a shared atomic, since cancelling the VT makes `join`
   itself return the cancellation), `saturation` (80 concurrent reads → ≥1
   `saturated`; observed exactly 12 = 80 − (4 workers + 64 queue)), and the
   timeout/cancel abandon paths under memcheck (worker frees the abandoned snapshot
   once).
4. **Carrier-liveness test strengthened (Medium).** Replaced the "both eventually
   finish" check with a **single-carrier** executor + `compute.join_timeout(500ms)`
   against a **2s** read_dir stall: the compute VT must complete *during* the stall,
   which is only possible if read_dir freed the carrier (offloaded). A
   carrier-blocking regression would make the 500ms join time out.

**Net (final):** Part 2 conforms to the stdlib IO contract — explicit deadline,
prompt abandon, distinct timeout/cancel/saturation/errno modes, bounded
backpressure, refcounted UAF-safe abandon — all pinned by regressions.

**Review round 6 (two central-runtime defects + test rigor, all fixed):**

1. **LANGUAGE_BUG — cancellation didn't promptly resume a parked VT (High).**
   `drift_thread_cancel` set `cancelled` + bumped the park token but did not
   re-enqueue a *started* fiber-parked VT, so a `read_dir` parked on a blocking job
   only reported cancellation when its deadline fired (the 400ms test masked it).
   Fixed centrally in `drift_thread_cancel` (not std.fs): re-enqueue a started,
   parked+exec VT (mirrors `drift_thread_unpark`). New runtime regression:
   `test_cancel_resumes_blocking_parked_vt_promptly` — 30s deadline + 30s stall,
   cancel after park, require resume < 500ms (observed ~0ms; a broken cancel ≈30s).
   Validated against the full 87-case vt/concurrency e2e sweep (no regressions).
2. **Abandoned jobs could block process exit indefinitely (High).**
   `drift_blocking_pool_shutdown` unconditionally `pthread_join`ed every worker at
   atexit, so a stuck NFS syscall could hang exit forever. Fixed: bounded
   `pthread_timedjoin_np` against a shared 2s deadline; stuck workers are not waited
   on (process exits, OS reaps; pool struct intentionally leaked so a late-waking
   worker stays safe). New regression: `test_timeout_permits_prompt_process_exit` —
   30s stall must let the process exit in seconds (observed ~2.2s).
3. **Single-carrier proof ordering hole (Medium).** Added a shared "reader entered
   read_dir" handshake atomic; the compute VT is introduced only after the reader
   has reached (and parked in) read_dir.
4. **Saturation capacity now pinned exactly (Medium).** Stall (5s) >> admission so
   no worker frees a queue slot mid-admission; assert exactly `80 - (4 + 64) == 12`
   rejected (admitted readers use a 500ms deadline to abandon promptly, bounding the
   test). Observed exactly 12.
5. **Stale docs fixed (Low).** `reload-substrate.md` and `thread.drift` now show
   `read_dir(path, timeout)` and the `-5` cancellation sentinel.

**Review round 7 (central scheduler wake-protocol races — all fixed):**

1. **Concurrent unparks: stale token + lost wake (High).** The round-6 CAS-only
   unpark still fell through to an unconditional `park_token++`, leaving (a) a stale
   token when a duplicate resumer hit a `READY` VT, and (b) a lost wake on
   `RUNNING→PARKED`. Fixed with a coherent lock-free **double-handshake**:
   `park_token` is an atomic latch; park publishes `PARKED` then re-checks the token;
   unpark deposits the token then re-checks `state == PARKED` (claim+enqueue);
   `READY` returns with no token. `drift_thread_unpark` CAS-claims `PARKED→READY` so
   cancel+timer+worker enqueue exactly once. Durable regression
   `test_park_unpark_no_stale_token_no_lost_wake` races timer+worker then asserts a
   second timed park is full-duration (200 iters; stress: 11 timing variants all
   clean). The deeper cross-park stale wake (a late deadline timer of park-1 waking
   park-2 — the bookkeeper "alternating-instant-sleep") is fixed by making
   `conc.sleep` register its timer once and **loop on a plain park** (spurious-wake
   tolerant), plus `read_dir` cancelling its timer on every abandon path.
2. **Timed-out shutdown workers could unpark destroyed VTs (High).** The worker loop
   now checks the (atomic) `pool->stopping` before unparking, so a worker finishing
   an *active* job during atexit teardown cannot touch a being-destroyed VT.
   Regression `test_process_exit_with_active_blocking_job` (active, non-timed-out
   parked job → prompt exit, no crash).
3. **Saturation count made deterministic (Medium).** Added a test-only walk-entry
   counter (`thread.fs_test_walk_entries`); the saturation test now occupies all 4
   workers and **barriers** on the counter == 4 before submitting 64 fillers + 12
   overflow, asserting exactly 12 rejections (no longer scheduling-dependent).
4. **Stress now in the test tree (Medium).** `test_cancel_timer_worker_storm_no_uaf`
   (cancel+timer+worker resume storm, 250 iters, under valgrind) replaces the manual
   300×3 run the reviewer noted was absent.

Validated: durable + storm + active-job + saturation pass; full vt/concurrency+net
e2e sweep green on the rewritten park/unpark/sleep protocol.

**Review round 8 (multi-carrier + shutdown races in the round-7 fix — addressed):**

1. **VT enqueued before it suspended (High, multi-carrier re-entrancy).** A park
   publishes PARKED before the swapcontext that SAVES its context; an unpark could
   CAS PARKED→READY and enqueue it, letting a second carrier swap into the unsaved
   context while the first still executes the fiber. Fixed with a **central
   re-entrancy guard**: before running a dequeued VT (and before the reactor's
   direct-resume swapcontext), the scheduler waits until the parking carrier has
   finished suspending it (`carrier_tid == 0`, cleared only after swapcontext
   returns). No-op for single-carrier / cleanly-suspended VTs. _(The reviewer's
   suggested explicit PARKING state is the more complete model but entangles with
   the reactor's direct-resume fast path — where a naive PARKING state causes lost
   wakes; the carrier_tid guard prevents the concurrent/re-entrant execution
   centrally at both run sites without that entanglement.)_
2. **Shutdown could race a late unpark (High).** A bare `stopping` check let a
   worker pass it, pause, then unpark after teardown began. Fixed with an
   **in-flight-unpark count + drain**: a worker takes a stake under `pool->mu`
   (only if `!stopping`) across its unpark; shutdown sets `stopping` and then WAITS
   on `drain_cv` until the count is 0 before any teardown.
3. **Deterministic hooks replace timing luck (Medium).** Two test-only runtime
   hooks pin the exact transitions: `DRIFT_TEST_PARK_PAUSE_US` widens the
   premature-suspension window (multi-carrier re-entrancy test, plain + valgrind),
   and `DRIFT_TEST_WORKER_UNPARK_PAUSE_MS` pauses a worker mid-unpark so a test can
   initiate shutdown in that window and prove the drain (process exit delayed by
   the pause; valgrind-clean). Both committed to the test tree.
4. **Test-instrumentation ABI (Low).** `fs_test_walk_entries` follows the existing
   `test_eventfd_create`/`test_timerfd_create` plain-`test_*`-intrinsic convention;
   explicitly documented as part of the ABI 17 boundary in `history.md`.

**Review round 9 (teardown ordering + reactor single-claim + a test data race):**

1. **Blocking-pool drain happened AFTER reactor/executor teardown (High).** The
   pool drained only via its libc atexit handler, which runs *after*
   `drift_run_main_on_vt` already destroyed the reactor + executor — so the
   inflight-drain couldn't protect them. Fixed: `drift_blocking_pool_quiesce()` is
   now called **synchronously** in the teardown (after registry cleanup, before
   reactor/executor shutdown), so any authorized worker unpark lands on a live
   executor; the atexit handler is an idempotent fallback (once-flag). Regression
   `test_worker_unpark_resumes_parked_vt` confirms the *worker* (not local-handle
   destruction) is the resumer: main JOINS a long-deadline reader (so it is never
   dropped/cancelled), the worker pauses mid-unpark, and the join only returns
   after that pause — proving `drift_thread_unpark` ran on the worker.
2. **Reactor used load-then-store, racing `drift_thread_unpark`'s CAS (High).** A
   reactor IO event could read PARKED while a timer/cancel CAS'd PARKED→READY and
   enqueued — then the reactor also resumed/enqueued (duplicate claim). Fixed with
   single-claim helpers `drift_vt_claim_for_direct_resume` (CAS PARKED→RUNNING) and
   `drift_vt_claim_for_enqueue` (CAS PARKED→READY), used at all four reactor sites
   and matching `drift_thread_unpark`'s existing CAS — so exactly one resumer wins.
   `test_timer_cancel_single_claim_race` pins the timer-vs-cancel claim race
   (multi-carrier, widened window, valgrind); the reactor fd-event claim sites are
   exercised by the std_net deadline e2e sweep.
3. **Park test-hook data race (Medium).** `drift_test_park_pause_us` was a
   non-atomic static read/written by multiple carriers; now initialized once via
   `pthread_once`.

**Review round 10 (direct-resume state transition) + test-suite hardening:**

1. **Direct reactor claim used RUNNING too early (High).** Claiming PARKED→RUNNING
   let a concurrent unpark see RUNNING and deposit a `park_token` the parking VT
   then consumed instead of suspending — so it never suspended and the reactor's
   `carrier_tid==0` guard could hang. Fixed: the single `drift_vt_claim_for_resume`
   claims **PARKED→READY** for both direct and queued resume (while READY, other
   unparks recognize "already claimed" and deposit no token); the direct-resume
   path flips **READY→RUNNING itself, only after the VT has suspended**
   (carrier_tid==0), immediately before the swapcontext. _Reachability note: the
   direct-resume lives in single-worker poll mode, where the resumed VT was parked
   by the same worker (carrier_tid already 0), so the specific cross-carrier hang
   is not triggerable in the current architecture; the PARKED→READY claim is the
   correct defensive semantics regardless. `test_reactor_fd_event_vs_cancel_direct_resume`
   exercises the direct-resume claim path under concurrent cancellation (via the
   `--test-build-only` `file_from_fd` path, not `--dev`)._

**Test-suite hardening (user request, beyond the findings):**
- **FS error semantics** pinned deterministically via test-only fault injection in
  `drift_fs_do_walk` (`DRIFT_FS_TEST_FSTATAT_FAIL_NAME` /
  `_READDIR_FAIL_ERRNO` / `_CLOSE_FAIL_ERRNO`): per-entry fstatat failure →
  `FileKind::Unknown` (snapshot still succeeds); read/validate error wins over a
  close error; a close-only failure rejects the snapshot.
- **Unsigned-byte ordering** proven with multibyte UTF-8 filenames (lead byte
  `0xC3`) that sort AFTER ascii `z` — which a character/locale collation would not.
- **Coordinator contract** upgraded: two sequential SIGUSR1 reloads with the
  directory changed between them; a third reload whose read fails leaves the
  published state untouched; and a Destructible old state dropped OUTSIDE the lock,
  proven by a `destroy()` that re-locks the live mutex (drop-inside-lock would
  self-deadlock). Fixes the old reference's "declares `old` while the guard is in
  scope" (now guard in an inner block, `old` held in an outer binding).

**Review round 11 (test-quality findings — runtime confirmed correct):**

1. **The direct-resume race test did not prove the reactor WINS the claim (High).**
   On a single-worker executor, main wrote the eventfd then immediately cancelled,
   so cancellation normally claimed PARKED→READY first and the reactor lost its CAS
   — the test passed without ever executing a successful direct-resume claim. Worse,
   the always-on dedicated **reactor thread** services real fd events via the QUEUED
   claim+enqueue path, so the worker-inline direct-resume (the path with the
   READY→RUNNING window) was never exercised at all. Fixed deterministically:
   - `DRIFT_TEST_NO_REACTOR_THREAD=1` suppresses the reactor thread so the reader's
     **dedicated single-worker executor** (`spawn_on`) is the sole poller → the fd
     event takes the worker-inline direct-resume path.
   - `vt_test_direct_resume_claims()` intrinsic (new atomic counter) advances when
     the reactor WINS PARKED→READY; main waits on it to PROVE the win before cancelling.
   - `DRIFT_TEST_DIRECT_RESUME_PAUSE_MS` holds the VT in READY (after suspend, before
     READY→RUNNING) so the cancel reliably lands in the window and must be a no-op.
   - main BUSY-SPINS (never parks) during the wait so its own single-worker carrier
     never enters the poll branch and steals poll ownership; the counter-wait is also
     the correctness guard (a too-early read that never parked → no claim → the test
     times out and fails, never a false pass). Added a `valgrind --fair-sched=yes`
     memcheck variant (the hot spin would otherwise starve the reader's worker on
     valgrind's serial scheduler) to catch double-enqueue/stale-token UAF.

2. **Sequential reload ordering still depended on sleeps (Medium).** The coordinator
   test slept 500 ms before mutating the directory but never observed that reload 1
   actually read two entries first — a delayed reload reading three would still pass.
   Fixed: the worker emits a per-reload `ack:<count>` on stderr (unbuffered), and the
   test reads each ack (via `select`, no sleep) to confirm reload 1 saw exactly 2
   BEFORE adding the third file, reload 2 saw 3, reload 3's read failed (state
   untouched). main emits a `ready` marker (the SIGUSR1 mask + signalfd are installed
   before any carrier runs, so this is a sufficient gate) — all timing sleeps removed.

## Explicitly rejected / deferred

- Streaming `DirIterator` holding a live `DIR*` (`Destructible`) — deferred; the
  snapshot design is what delivers determinism for v1.
- `sigaction`/real-time signals/user signal handlers — out of scope.
- `stat`-based symlink-target resolution, recursive walk, Windows backend.
