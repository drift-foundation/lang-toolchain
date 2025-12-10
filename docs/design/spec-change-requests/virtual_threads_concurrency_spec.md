# Virtual-thread concurrency and blocking I/O spec (proposal)

> **Status:** Draft, hybrid proposal/spec  
> **Scope:** Loom-style virtual threads, blocking I/O integration, epoll-based reactor, and executor policies for Drift runtime.
> **Audience:** Drift runtime implementors, stdlib maintainers, and compiler/runtime architects.

---

## 1. Goals and non‑goals

### 1.1. Goals

1. **Loom-style programming model**
   - User code calls *blocking* functions (`read`, `write`, `accept`, `sleep`, `join`, etc.) with no `async`/`await`, futures, or callbacks in the public API.
   - Virtual threads (VTs) are parked/unparked by the runtime when these operations would block, so carrier OS threads remain free.

2. **Virtual threads over a small OS thread pool**
   - Many VTs (10k–100k) multiplexed over a bounded pool of carrier threads (e.g., sized by `ExecutorPolicy`).
   - VT scheduling is cooperative via well-defined blocking points (I/O, timers, `join`, etc.).

3. **epoll-based reactor**
   - On Linux, use `epoll` as the primary I/O multiplexer for sockets and file descriptors that support non-blocking I/O.
   - Event loop integrated with VT scheduler: readiness events wake parked VTs.

4. **Stable blocking stdlib API**
   - `std.io`, `std.net`, and `std.concurrent` expose *synchronous*, blocking interfaces.
   - Implementation is free to use VTs or raw OS threads, but semantics must remain compatible if fallback is needed.

5. **Executor policies**
   - Allow configuration of carrier thread counts, queue limits, saturation behavior, and scheduling hints via `ExecutorPolicy` and executors.
   - Virtual-thread operations (`vt_spawn`, `vt_park`, `vt_unpark`) must route through the active executor.

6. **Testability and incremental rollout**
   - Architecture supports early implementation using pure OS threads while keeping the public API and module boundaries identical to the final VT design.
   - The same tests should pass when the implementation flips from OS-threads-only to VT+epoll.

### 1.2. Non-goals

- No alternative explicit-async model (no `async` keywords, no core futures type in the public API).
- No cross-platform abstraction for all possible OS I/O APIs in this document; the focus is Linux/epoll, with hooks defined so other backends (kqueue, IOCP, etc.) can slot in later.
- No formal scheduling fairness guarantees beyond “a parked virtual thread must eventually be unparked once its event is ready.”
- No detailed memory model beyond standard Drift language rules.

---

## 2. High‑level architecture

### 2.1. Components

1. **Virtual threads (VTs)**
   - Logical units of execution with their own stacks, registers, and metadata.
   - Scheduled cooperatively on carrier OS threads.

2. **Carrier threads (OS threads)**
   - Threads in a pool managed by an executor.
   - Each carrier runs a scheduler loop: pop next runnable VT, run until it parks or yields.

3. **Executors**
   - Manage carrier threads and the runnable VT queues.
   - Bound by `ExecutorPolicy` describing thread counts, queue limits, and saturation behavior.
   - Provide `submit_vt`, `wake_vt`, and timer/epoll hooks.

4. **Reactor (epoll-based)**
   - Owns an `epoll` instance and registers file descriptors with readiness interests.
   - On events, maps fd → VT(s) waiting on that fd, and unparks them via the executor.

5. **Timer wheel / timer facility**
   - Manages time-based wake-ups (`sleep`, timeouts).
   - Integrates with the scheduler: earliest timer deadline drives blocking behavior (e.g., `epoll_wait` timeout).

6. **System-call wrapper / blocking boundary**
   - Internal helper (`block_on_io` & friends) that:
     - Performs non-blocking syscalls.
     - On `EAGAIN`/`EWOULDBLOCK`, parks the current VT and registers interest with reactor.
   - All stdlib blocking I/O must call through this boundary.

### 2.2. Layering

From top to bottom:

1. **User code**  
   Calls `std.io`, `std.net`, `std.concurrent` blocking APIs.

2. **Stdlib blocking API implementation**  
   `InputStream.read`, `Socket.write`, `ThreadHandle.join`, `sleep`, etc.  
   All I/O and sleep delegate to internal runtime helpers that know about VTs.

3. **Internal runtime layer (language intrinsics)**  
   `lang.thread` intrinsics and helper functions responsible for:
   - Creating VTs.
   - Parking/unparking VTs.
   - Accessing current executor and VT handle.

4. **Executor + reactor implementation (C/POSIX)**  
   epoll, timer wheel, run queues, carrier threads.

5. **OS kernel**  
   Threads, file descriptors, networking, time.

---

## 3. User‑facing API (Drift examples)

> Note: signatures use existing naming conventions from the language spec (`std.concurrent`, `ExecutorPolicy`, `lang.thread` intrinsic hooks, etc.). Names in this document are indicative and can be adjusted to match the evolving spec.

### 3.1. Virtual-thread spawning

```drift
module std.concurrent

struct ThreadHandle<T>
    // opaque
{
    fn join(self: &mut ThreadHandle<T>) returns T
}

fn spawn<F, R>(f: F) returns ThreadHandle<R>
    where F is Callable<(), R>, F is Send, R is Send
```

Usage:

```drift
import std.concurrent as conc

fn worker(id: Int) returns Int {
    // Simulate work
    std.concurrent.sleep(Duration::millis(10))
    return id * 2
}

fn main() returns Int {
    val handles = Array<conc.ThreadHandle<Int>>()

    var i = 0
    while i < 10 {
        let id = i
        handles.push(conc.spawn(fn () returns Int {
            return worker(id)
        }))
        i = i + 1
    }

    var sum = 0
    for h in handles {
        sum = sum + h.join()
    }
    return sum
}
```

Semantics:

- `spawn` always *logically* creates a virtual thread, even if runtime currently uses a 1:1 OS-thread fallback.
- `join` is a *blocking* call that parks the caller VT until the target completes.

### 3.2. Scoped/structured concurrency

```drift
module std.concurrent

struct Scope {
    // opaque
}

fn scope<F>(body: F) returns Void
    where F is Callable<(Scope), Void>, F is Send
```

Usage:

```drift
import std.concurrent as conc

fn main() returns Int {
    var total = 0

    conc.scope(fn (s: conc.Scope) returns Void {
        val h1 = s.spawn(fn () returns Int {
            std.concurrent.sleep(Duration::millis(100))
            return 1
        })
        val h2 = s.spawn(fn () returns Int {
            std.concurrent.sleep(Duration::millis(50))
            return 2
        })

        total = h1.join() + h2.join()
    })

    return total
}
```

Semantics:

- All tasks spawned through a `Scope` must be joined or canceled before `scope` returns.
- If any child throws, `scope` must:
  - Mark the scope as failed.
  - Cancel remaining children (cooperative cancellation; see executor behavior).
  - Rethrow or surface an aggregated error as per final spec decision.

### 3.3. Blocking I/O (sockets)

```drift
module std.net

struct TcpListener {
    // opaque
}

struct TcpStream {
    // opaque
}

fn listen(addr: String, port: Int) returns TcpListener
fn accept(self: &TcpListener) returns TcpStream
fn read(self: &TcpStream, buf: &mut [Byte]) returns Int
fn write(self: &TcpStream, buf: &[Byte]) returns Int
fn close(self: &mut TcpStream) returns Void
```

Usage:

```drift
import std.net
import std.concurrent as conc

fn handle_client(mut conn: std.net.TcpStream) returns Void {
    var buf = ByteArray::with_capacity(4096)
    loop {
        let n = conn.read(&mut buf)
        if n <= 0 {
            break
        }
        let wrote = conn.write(buf.slice(0, n))
        if wrote != n {
            // handle partial write or error
            break
        }
    }
    conn.close()
}

fn main() returns Int {
    let listener = std.net.listen("0.0.0.0", 8080)

    loop {
        let conn = listener.accept()
        conc.spawn(fn () returns Void {
            handle_client(conn)
        })
    }
}
```

Semantics:

- `accept`, `read`, and `write` are blocking for callers, but must not pin carrier threads:
  - When a call would block, the current VT is parked and another VT is scheduled.
  - When the fd is ready, the parked VT is unparked and the call resumes transparently.

### 3.4. Sleep and timers

```drift
module std.concurrent

struct Duration { /* ... */ }

fn sleep(d: Duration) returns Void
```

Usage:

```drift
fn delayed_message(ms: Int) returns Void {
    std.concurrent.sleep(Duration::millis(ms))
    std.io.println("done")
}
```

Semantics:

- `sleep` *parks* the current VT until the timer deadline, freeing the carrier thread to run other VTs.
- Implemented via a timer wheel integrated into the executor and/or reactor.

---

## 4. Internal runtime contracts (`lang.thread`)

These APIs are **not** public, but are treated as language/runtime intrinsics. They form the stable contract between the Drift compiler, stdlib, and the runtime written in C.

### 4.1. Virtual thread lifecycle

Conceptual functions (actual linkage: `extern "C"` or similar):

```drift
module lang.thread

// Opaque handle to a virtual thread.
struct VThreadHandle { /* ... */ }

// Opaque handle to an executor.
struct ExecutorHandle { /* ... */ }

// Spawn a new virtual thread on the current executor.
extern fn vt_spawn(entry: fn(), exec: ExecutorHandle) returns VThreadHandle

// Park the current virtual thread, yielding the carrier thread.
extern fn vt_park() returns Void

// Unpark a specific virtual thread.
extern fn vt_unpark(handle: VThreadHandle) returns Void

// Get handle to the current VT.
extern fn current_vthread() returns VThreadHandle

// Get handle to the current executor.
extern fn current_executor() returns ExecutorHandle
```

Normative requirements:

- `vt_spawn` must:
  - Allocate VT stack and metadata.
  - Place the VT into executor’s runnable queue.
- `vt_park` must:
  - Mark the current VT as parked/unrunnable.
  - Yield control back to the executor, which picks another runnable VT.
- `vt_unpark` must:
  - Mark the VT as runnable if it was parked.
  - Enqueue it into the executor’s runnable queue.

### 4.2. I/O and timers

```drift
module lang.thread

// Register interest in I/O readiness for fd and associate a VT to be unparked.
extern fn register_io(fd: Int, events: Int, vt: VThreadHandle) returns Int

// Register a timer; VT is unparked when deadline elapses.
extern fn register_timer(deadline_ms: Uint64, vt: VThreadHandle) returns Int
```

Here, `events` is a bitmask corresponding to epoll events (e.g., `EPOLLIN`, `EPOLLOUT`). `deadline_ms` is a monotonic time in milliseconds (or other chosen unit).

Normative requirement:

- `register_io` must:
  - Register or update the fd with the epoll instance used by the executor/reactor.
  - Associate the fd + event mask with the VT handle so that epoll readiness results in `vt_unpark(vt)`.
- `register_timer` must:
  - Insert a timer into the executor’s timer structure.
  - On expiry, unpark the associated VT.

---

## 5. Blocking I/O boundary (Drift side)

All stdlib blocking I/O must go through a small internal helper that knows whether it is running on a VT or a raw OS thread.

### 5.1. `block_on_io` helper

Internal shape (not public):

```drift
module runtime.io

fn block_on_io<F, R>(op: F) returns R
    where F is Callable<(), R>
```

Logic (Drift pseudocode over intrinsics):

```drift
fn block_on_io<F, R>(op: F) returns R
    where F is Callable<(), R>
{
    // Fast path: not on a virtual thread.
    if !runtime.is_virtual_thread() {
        return op()
    }

    loop {
        let res = op()

        if res is IoWouldBlock {
            let meta = res.io_meta()  // fd, interest mask
            let me = lang.thread.current_vthread()

            // Register I/O and park.
            lang.thread.register_io(meta.fd, meta.events, me)
            lang.thread.vt_park()

            // After wake, loop to retry the operation.
            continue
        }

        return res
    }
}
```

Notes:

- `IoWouldBlock` represents an internal error/condition: non-blocking syscall returned `EAGAIN`/`EWOULDBLOCK`.
- `op` must be written to perform non-blocking I/O and encode “would block” as `IoWouldBlock` rather than throwing or returning a normal error.

### 5.2. Stdlib integration example (`TcpStream.read`)

```drift
module std.net

struct TcpStream {
    fd: Int  // file descriptor
    // other metadata
}

fn read(self: &TcpStream, buf: &mut [Byte]) returns Int {
    return runtime.io.block_on_io(fn () returns Int {
        let n = runtime.syscall.read_nonblocking(self.fd, buf)
        if n.is_would_block {
            return IoWouldBlock::from_fd(self.fd, EPOLLIN)
        }
        return n.value
    })
}
```

### 5.3. Fallback behavior (no virtual threads)

If the runtime is configured without virtual threads or cannot create an executor, `runtime.is_virtual_thread()` must return `false` for all executions. In that case:

- `block_on_io` just calls `op()` and blocks the OS thread directly.
- All semantics remain correct, only scalability differs.

---

## 6. epoll-based reactor (C / POSIX)

The reactor is a C module that:

- Owns an `epoll` fd.
- Integrates with the executor’s scheduling loop.
- Manages fd → VT mappings.

### 6.1. Data structures (C)

```c
// Pseudocode: types and signatures are indicative.

typedef struct VThread VThread;
typedef struct Executor Executor;

typedef struct IoWaiter {
    int fd;
    uint32_t events;   // EPOLLIN | EPOLLOUT | ...
    VThread *vt;
} IoWaiter;

typedef struct Reactor {
    int epfd;          // epoll file descriptor
    // fd -> IoWaiter
    // This can be a hash map, array indexed by fd, or other structure.
    IoWaiter **waiters;
    size_t waiters_cap;
} Reactor;
```

Initialization:

```c
int reactor_init(Reactor *r) {
    r->epfd = epoll_create1(EPOLL_CLOEXEC);
    if (r->epfd < 0) return -1;

    r->waiters = NULL;
    r->waiters_cap = 0;
    return 0;
}
```

### 6.2. Registering I/O interest

```c
int reactor_register_io(Reactor *r, int fd, uint32_t events, VThread *vt) {
    // Ensure waiters[fd] exists.
    if ((size_t)fd >= r->waiters_cap) {
        size_t new_cap = r->waiters_cap ? r->waiters_cap * 2 : 64;
        while (new_cap <= (size_t)fd) new_cap *= 2;
        IoWaiter **nw = realloc(r->waiters, new_cap * sizeof(IoWaiter *));
        if (!nw) return -1;
        memset(nw + r->waiters_cap, 0, (new_cap - r->waiters_cap) * sizeof(IoWaiter *));
        r->waiters = nw;
        r->waiters_cap = new_cap;
    }

    IoWaiter *w = r->waiters[fd];
    if (!w) {
        w = calloc(1, sizeof(IoWaiter));
        if (!w) return -1;
        r->waiters[fd] = w;
    }

    w->fd = fd;
    w->events = events;
    w->vt = vt;

    struct epoll_event ev;
    ev.events = events | EPOLLONESHOT;
    ev.data.u32 = (uint32_t)fd;

    int op = EPOLL_CTL_ADD;
    // If already registered, switch to MOD.
    if (epoll_ctl(r->epfd, EPOLL_CTL_MOD, fd, &ev) < 0) {
        if (errno == ENOENT) {
            if (epoll_ctl(r->epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {
                return -1;
            }
        } else {
            return -1;
        }
    }

    return 0;
}
```

Notes:

- Use `EPOLLONESHOT` so that after an event fires, the fd is disabled until the VT re-arms it (by calling `register_io` again).
- `ev.data.u32` stores a small key (fd); alternatively, use `ev.data.ptr` for a direct pointer to `IoWaiter` or `VThread`.

### 6.3. Event loop integration

The executor’s main loop must:

1. Pop runnable VTs from its queue.
2. Run VTs until they park or exit.
3. When there is no runnable VT, or a configured “poll interval” elapses:
   - Call `epoll_wait` with a timeout equal to:
     - The time until the next timer expires, or
     - A default maximum poll timeout.

C-like pseudo-code:

```c
int executor_run(Executor *exec) {
    Reactor *r = exec->reactor;
    const int MAX_EVENTS = 64;
    struct epoll_event events[MAX_EVENTS];

    while (!exec->shutdown_requested) {
        VThread *vt = pop_runnable_vt(exec);
        if (vt) {
            run_vthread(exec, vt);  // runs until vt parks, yields, or exits
            continue;
        }

        // No runnable threads; wait for I/O or timers.
        int timeout_ms = executor_next_timeout_ms(exec);
        if (timeout_ms < 0) timeout_ms = -1;  // wait indefinitely

        int n = epoll_wait(r->epfd, events, MAX_EVENTS, timeout_ms);
        if (n < 0) {
            if (errno == EINTR) continue;
            // log / handle error
            continue;
        }

        // Handle timers first.
        executor_handle_timers(exec);

        for (int i = 0; i < n; ++i) {
            int fd = (int)events[i].data.u32;
            uint32_t ev = events[i].events;

            IoWaiter *w = NULL;
            if ((size_t)fd < r->waiters_cap) {
                w = r->waiters[fd];
            }
            if (!w || !w->vt) continue;

            // Wake the VT associated with this fd.
            vt_unpark_from_c(exec, w->vt);

            // EPOLLONESHOT ensures we must re-arm via reactor_register_io.
            // No need to modify here; VT's next I/O call will re-arm.
        }
    }

    return 0;
}
```

Normative requirement:

- An I/O readiness event must result in `vt_unpark` for all associated VTs; the unparked VT must be visible in the runnable queue as soon as possible.

### 6.4. Timer integration

Timer structure example:

```c
typedef struct Timer {
    uint64_t deadline_ms;
    VThread *vt;
} Timer;

typedef struct TimerWheel {
    // Implementation could be a min-heap, wheel, or other structure.
    // For now, assume a simple min-heap.
} TimerWheel;

uint64_t executor_next_timeout_ms(Executor *exec) {
    uint64_t now = monotonic_ms();
    Timer *next = timerwheel_peek(exec->timers);
    if (!next) return -1; // no timers
    if (next->deadline_ms <= now) return 0; // expired; process immediately
    return (int)(next->deadline_ms - now);
}

void executor_handle_timers(Executor *exec) {
    uint64_t now = monotonic_ms();
    while (1) {
        Timer *t = timerwheel_peek(exec->timers);
        if (!t || t->deadline_ms > now) break;
        (void) timerwheel_pop(exec->timers);

        // Wake associated VT
        vt_unpark_from_c(exec, t->vt);
    }
}
```

Normative requirement:

- A VT parked on a timer must be unparked no later than its timer deadline plus a small implementation-defined scheduling delay.

---

## 7. Virtual thread representation and scheduling (C / POSIX)

### 7.1. Virtual thread struct

```c
typedef enum {
    VT_RUNNABLE,
    VT_PARKED,
    VT_FINISHED,
    VT_CANCELED,
} VThreadState;

typedef struct VThread {
    VThreadState state;
    Executor *executor;
    // Platform-specific stack pointer, registers, etc.
    void *stack_ptr;
    size_t stack_size;
    // Entry function and argument closure
    void (*entry)(void *arg);
    void *arg;
    // Link fields for queues
    struct VThread *next;
} VThread;
```

### 7.2. Executor struct

```c
typedef struct Executor {
    pthread_t *threads;
    int thread_count;

    Reactor *reactor;
    TimerWheel *timers;

    // Runnable queue
    VThread *runq_head;
    VThread *runq_tail;

    pthread_mutex_t runq_lock;
    pthread_cond_t  runq_cv;

    int shutdown_requested;
} Executor;
```

### 7.3. Spawning a VT

```c
VThread *vt_create(Executor *exec, void (*entry)(void *), void *arg) {
    VThread *vt = calloc(1, sizeof(VThread));
    if (!vt) return NULL;

    vt->state = VT_RUNNABLE;
    vt->executor = exec;
    vt->entry = entry;
    vt->arg = arg;

    // Allocate stack and initialize registers here (platform-specific).
    // ...

    return vt;
}

void executor_submit_vt(Executor *exec, VThread *vt) {
    pthread_mutex_lock(&exec->runq_lock);
    if (exec->runq_tail) {
        exec->runq_tail->next = vt;
    } else {
        exec->runq_head = vt;
    }
    exec->runq_tail = vt;
    vt->next = NULL;
    pthread_cond_signal(&exec->runq_cv);
    pthread_mutex_unlock(&exec->runq_lock);
}
```

The `lang.thread.vt_spawn` intrinsic can be implemented as:

```c
// Called from Drift via FFI
VThreadHandle lang_thread_vt_spawn(void (*entry)(void *), ExecutorHandle exec_handle) {
    Executor *exec = (Executor *)exec_handle.ptr;
    VThread *vt = vt_create(exec, entry, /*arg=*/NULL);
    if (!vt) {
        // handle allocation failure
    }
    executor_submit_vt(exec, vt);
    VThreadHandle h = { .ptr = vt };
    return h;
}
```

### 7.4. Parking and unparking

Parking (`vt_park`) from Drift invokes a small C stub:

```c
// Called from Drift when current VT decides to park.
void lang_thread_vt_park(void) {
    VThread *vt = current_vthread();  // TLS or other mechanism
    Executor *exec = vt->executor;

    vt->state = VT_PARKED;
    // Switch back to scheduler context (stack-switch / setjmp/longjmp / ucontext/etc.).
    switch_to_scheduler(exec, vt);
}
```

Unparking is called by reactor/timers:

```c
void lang_thread_vt_unpark(VThreadHandle h) {
    VThread *vt = (VThread *)h.ptr;
    Executor *exec = vt->executor;

    if (vt->state == VT_PARKED) {
        vt->state = VT_RUNNABLE;
        executor_submit_vt(exec, vt);
    }
}
```

Normative requirement:

- `vt_park` must not return to the Drift code until a subsequent `vt_unpark` call schedules the VT again.
- Carrier threads must always run the scheduler loop when not executing VT code; they must never busy-wait on parked VTs.

---

## 8. Executor policy

`ExecutorPolicy` governs how executors are created and configured.

### 8.1. Drift-side API

```drift
module std.concurrent

struct ExecutorPolicy {
    // opaque builder
}

struct Executor {
    // opaque handle to runtime executor
}

impl ExecutorPolicy {
    fn new() returns ExecutorPolicy
    fn min_threads(self: &mut ExecutorPolicy, n: Int) returns &mut ExecutorPolicy
    fn max_threads(self: &mut ExecutorPolicy, n: Int) returns &mut ExecutorPolicy
    fn queue_limit(self: &mut ExecutorPolicy, n: Int) returns &mut ExecutorPolicy
    fn on_saturation_return_busy(self: &mut ExecutorPolicy) returns &mut ExecutorPolicy
    fn build(self: ExecutorPolicy) returns Executor
}
```

Normative requirements:

- `min_threads` and `max_threads` must bound the size of the carrier thread pool for the executor.
- `queue_limit` must define an upper bound on the number of runnable VTs queued at any time; attempts to exceed this may:
  - Return a “busy” error on `spawn`, or
  - Block the caller until space is available (depending on `on_saturation` policy).
- `on_saturation_return_busy` selects a non-blocking policy: VT creation returns an error when the queue is full.

### 8.2. Integration with `std.concurrent.spawn`

High-level behavior:

- `std.concurrent.spawn` uses a **default global executor** created with some policy (e.g., `min_threads = 1`, `max_threads = CPU cores`, `queue_limit` = large).
- `spawn_on(exec, f)` (if exposed) uses the specified executor instead.

Drift example:

```drift
fn main() returns Int {
    let policy = std.concurrent.ExecutorPolicy::new()
        .min_threads(1)
        .max_threads(std.sys.num_cpus())
        .queue_limit(100_000)
        .on_saturation_return_busy()
        .build()

    // Optionally: use spawn_on(policy, ...)
    return 0
}
```

---

## 9. Error handling and cancellation

### 9.1. Thread completion and `join`

- When a VT finishes its entry function:
  - Its result (return value or thrown error) is stored in a completion slot attached to the thread handle.
  - Any VTs blocked on `join` are unparked.

### 9.2. Cancellation (structured concurrency)

For `Scope` and similar constructs:

- When the scope is canceled or an error is propagated:
  - Children must be marked as “canceled” and receive a cooperative cancellation signal.
  - Cancelled VTs must still run destructors for their stack-allocated objects when unwinding (using language-level unwinding semantics).
  - Implementation detail (e.g., injecting a cancellation exception) is beyond this document, but the scheduler must not forcibly drop a VT without unwinding.

---

## 10. Incremental rollout plan

1. **Phase 0: OS-thread-only implementation**
   - `std.concurrent.spawn` uses `pthread_create` directly.
   - `join` uses `pthread_join`.
   - `std.io` & `std.net` use blocking syscalls directly.
   - `block_on_io` exists but simply calls `op()`.
   - All tests are written against blocking APIs.

2. **Phase 1: Introduce `lang.thread` intrinsics and VT data structures**
   - Implement `vt_spawn`, `vt_park`, `vt_unpark`, `current_vthread`, `current_executor`.
   - Replace `spawn`/`join` implementation with VTs + executor, but keep I/O blocking the carrier threads.

3. **Phase 2: Implement epoll-based reactor and timers**
   - Implement `register_io` and `register_timer` in terms of epoll and a timer wheel.
   - Wire `conc.sleep` to use `register_timer` + `vt_park`.
   - Scheduler driven by epoll events and timer deadlines.

4. **Phase 3: Turn on VT-aware I/O in `block_on_io`**
   - Switch all `std.net` and relevant `std.io` code to use non-blocking syscalls returning `IoWouldBlock`.
   - Fully implement the loop in `block_on_io` that registers fd, parks VT, and retries on wake.

5. **Phase 4: Harden and extend**
   - Avoid hidden carrier-blocking operations (DNS, certain syscalls) by routing them through a dedicated blocking thread pool.
   - Tighten error handling, cancellation, and structured concurrency semantics.
   - Optimize scheduler (work stealing, affinity, etc.) as needed.

---

## 11. Testing strategy

1. **Functional tests**
   - 1,000+ concurrent connections to a VT-based echo server; verify responses and memory usage.
   - Multi-threaded `join` tests: join before and after completion, error propagation.
   - Timer accuracy tests: `sleep` and timeout behavior.

2. **Stress tests**
   - 50k VTs doing small I/O and sleeping intermittently.
   - Ensure OS thread count stays within configured `max_threads` and that throughput scales reasonably.

3. **Fallback tests**
   - Run tests with virtual threads disabled (forcing OS-thread blocking). Semantics must remain correct.

4. **Correctness under error conditions**
   - epoll failure paths (e.g. closed fd) should not crash executor; they must fail the waiting VT gracefully.

---

## 12. Open questions / TBD

- Exact error types and propagation model for I/O errors and cancellation.
- Whether executors are exposed in the public API (`spawn_on`) or remain mostly internal with a single global default.
- Fine-grained fairness guarantees between VTs and starvation prevention.
- How to integrate with Drift’s eventual `Send`/`Sync` traits for cross-thread safety.
- Cross-platform abstractions (kqueue/IOCP) and how to factor them cleanly under `lang.thread`.

---

## 13. Summary

This document specifies a Loom-style virtual-thread model for Drift with:

- A stable blocking stdlib API (`std.concurrent`, `std.io`, `std.net`).
- Internal `lang.thread` intrinsics for VT lifecycle, I/O, and timers.
- A concrete epoll-based reactor design and executor loop.
- A clear incremental path from OS-thread-only to scalable VT-based concurrency.

Implementation can start with OS threads but must keep all layering and contracts defined here so that the switch to virtual-thread scheduling is a backend change, not an API redesign.
