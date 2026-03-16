# Plan: Generic blocking-job offload for VT runtime (first consumer: hostname resolution)

## Context

`net.connect()` currently calls `getaddrinfo()` inline on the carrier thread (interim fix). This blocks the worker thread executing the VT, potentially starving other VTs and the scheduler. The right fix is a **generic blocking-call offload mechanism** in the runtime — a bounded worker pool that VTs can submit blocking host calls to, park, and resume with results. Hostname resolution via `getaddrinfo()` is the first consumer; the mechanism should be reusable for future blocking host APIs (file/process ops, blocking FFI).

## Scope

### This patch (phase 1)

- Implement generic blocking-job offload for VT execution
- Use hostname resolution in `net.connect` as the first consumer
- Make the VT path deadline-aware and non-blocking with respect to carrier/scheduler threads

### Accepted temporary limitation

- On the non-VT main-thread path, hostname resolution blocks directly in `getaddrinfo()` without deadline enforcement
- This is intentionally deferred — we are not adding complexity to perfect a path that is architecturally transitional

### Phase 2 direction (not this patch)

- Run user `main` inside a root VT from startup, eliminating the main-vs-VT semantic split
- Once that lands, all user code runs on VTs and the direct-blocking main-thread path becomes dead code
- This patch does not attempt to fold that runtime execution-model redesign into the blocking-offload work

## Design overview

### Public surface: `deadline_ms` added to `net_connect`

- `thread.net_connect(&String, Int, Int) -> Int` — third param is `deadline_ms` (absolute monotonic)
- `std.net.connect(addr, timeout)` passes its computed deadline through
- Hostname resolution is invisible to the user — it happens inside `drift_net_connect`
- No new stdlib intrinsic or API split for DNS

### Architecture

```
VT calls thread.net_connect(&"example.com", 80, deadline_ms)
  → C: drift_net_connect(ip, port, deadline_ms)
    → inet_pton fast path? → yes → socket+connect (existing path, deadline ignored)
    → no (hostname) → drift_blocking_submit(job) + register timer(deadline) + park VT
      → worker thread: getaddrinfo() → fill native sockaddr → unpark VT
      → VT resumes: read native sockaddr → socket+connect → return fd
    → timeout? → CAS expired → return -1/EAGAIN
```

## Generic blocking-job offload facility

### Job representation

```c
typedef struct DriftBlockingJob {
    void (*job_fn)(struct DriftBlockingJob *job);      // blocking work function
    void (*destroy_fn)(struct DriftBlockingJob *job);  // consumer cleanup (frees job + owned resources)
    uint64_t vt;                    // requesting VT handle (for unpark)
    atomic_int completed;           // 0 = pending, 1 = done
    atomic_int expired;             // 0 = live, 1 = VT timed out
    int error;                      // 0 = success, nonzero = job-specific error
    struct DriftBlockingJob *next;  // intrusive list link
} DriftBlockingJob;
```

The `destroy_fn` callback makes cleanup fully generic. Both the worker (on expired timeout) and the VT (after consuming the result) call `job->destroy_fn(job)` to free the job and all consumer-owned resources. The worker pool never needs consumer-specific knowledge.

Consumer-specific jobs embed this as the first field:

```c
typedef struct DriftResolveJob {
    DriftBlockingJob base;
    char *hostname;                 // owned copy, freed by worker
    int port;
    struct sockaddr_storage addr;   // native resolved address (no string round-trip)
    socklen_t addrlen;
} DriftResolveJob;
```

The worker pool is generic — it just calls `job->job_fn(job)`. Consumer-specific data and logic live in the embedded struct and job function.

### Worker pool

```c
typedef struct DriftBlockingPool {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    DriftBlockingJob *queue_head;
    DriftBlockingJob *queue_tail;
    int queue_len;
    int queue_limit;
    int stopping;
    int worker_count;
    pthread_t *workers;
} DriftBlockingPool;
```

- **Pool size**: 4 threads
- **Queue limit**: 64 pending jobs; beyond that, submit returns -1
- **Worker loop**: `lock → wait(cv) → dequeue → unlock → job->job_fn(job) → completed=1 → if !expired: drift_thread_unpark(vt) else: job->destroy_fn(job)`
- **Shutdown**: `stopping=1`, `broadcast(cv)`, `join` all workers
- **Init**: lazy via `pthread_once` on first submit

### Result delivery

Worker calls `drift_thread_unpark(job->vt)` directly after setting `completed=1`. This enqueues the VT to its executor (`drift_exec_enqueue`) and wakes the reactor via the **existing** `wake_fd` eventfd (`drift_reactor_wake`). No new eventfd, no done-list, no reactor loop changes.

### Deadline handling

The caller's `deadline_ms` flows from stdlib through the intrinsic into the C runtime:

1. `drift_net_connect` receives `deadline_ms` directly
2. Registers reactor timer for `deadline_ms`, then parks via `drift_thread_park(0)`
3. **Happy path**: worker finishes first → `completed=1` → `drift_thread_unpark(vt)` → VT resumes → reads native result → socket+connect → returns fd. Timer fires later, unpark is idempotent (VT already RUNNING).
4. **Timeout path**: timer fires first → VT resumes → sees `completed==0` → CAS `expired` 0→1 → returns -1/EAGAIN. Worker finishes later → sees `expired==1` → frees job.
5. **Race**: `drift_thread_unpark` is idempotent. VT resumes once, reads `completed==1` → happy path.

### Job ownership protocol

Heap-allocated due to timeout lifetime race:
- **VT allocates** job, enqueues, parks
- **Happy path**: VT reads result, calls `job->destroy_fn(job)`
- **Timeout path**: VT sets `expired=1`, does NOT free. Worker calls `job->destroy_fn(job)` after `job_fn` returns.

### Saturation / backpressure

Queue limit 64. `drift_blocking_submit` returns -1 if full. `drift_net_connect` returns -1 with `errno=EAGAIN`.

**Stdlib EAGAIN behavior in connect loop** (net.drift:215-224):
1. If no deadline → return `WouldBlock` immediately
2. If deadline expired → return `WouldBlock`
3. Otherwise → `_park_main_thread_io(remaining)` (sleeps for a short quantum via `vt_park_until`) → retry

This is correct for pool saturation: transient saturation clears quickly, the sleep quantum prevents CPU spinning, and the deadline bounds total wait time. On a VT, `vt_park_until` context-switches properly. This is intentionally the right semantics — retry with backoff up to deadline, then WouldBlock.

## DNS consumer implementation

### IP-literal fast path

`drift_net_connect` tries `inet_pton(AF_INET, ...)` first. If it parses, socket+connect immediately — zero pool involvement, deadline_ms ignored (connect is non-blocking, returns EINPROGRESS).

### Resolve job destroy + work functions

```c
static void drift_resolve_job_destroy(DriftBlockingJob *base) {
    DriftResolveJob *job = (DriftResolveJob *)base;
    free(job->hostname);  // NULL-safe; already freed by job_fn on happy path
    free(job);
}

static void drift_resolve_job_fn(DriftBlockingJob *base) {
    DriftResolveJob *job = (DriftResolveJob *)base;
    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char port_str[8];
    snprintf(port_str, sizeof(port_str), "%d", job->port);
    int rc = getaddrinfo(job->hostname, port_str, &hints, &res);
    free(job->hostname);
    job->hostname = NULL;
    if (rc != 0 || !res) {
        base->error = (rc != 0) ? rc : -1;
        return;
    }
    memcpy(&job->addr, res->ai_addr, res->ai_addrlen);
    job->addrlen = res->ai_addrlen;
    freeaddrinfo(res);
    base->error = 0;
}
```

### drift_net_connect implementation

```c
int64_t drift_net_connect(DriftString *ip, int64_t port, int64_t deadline_ms) {
    char *host = drift_string_to_cstr(*ip);

    // Fast path: IPv4 literal
    struct sockaddr_in a4 = {0};
    a4.sin_family = AF_INET;
    a4.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &a4.sin_addr) == 1) {
        free(host);
        struct sockaddr_storage sa;
        memcpy(&sa, &a4, sizeof(a4));
        return drift_connect_to_addr(&sa, sizeof(a4));
    }

    // Hostname path
    DriftVt *vt = drift_vt_tls_get();
    if (!vt) {
        // Main thread: block directly
        struct sockaddr_storage addr;
        socklen_t addrlen;
        int rc = drift_resolve_blocking(host, (int)port, &addr, &addrlen);
        free(host);
        if (rc != 0) { errno = EINVAL; return -1; }
        return drift_connect_to_addr(&addr, addrlen);
    }

    // VT path: submit to blocking pool
    DriftResolveJob *rj = calloc(1, sizeof(DriftResolveJob));
    if (!rj) { free(host); errno = ENOMEM; return -1; }
    rj->base.job_fn = drift_resolve_job_fn;
    rj->base.destroy_fn = drift_resolve_job_destroy;
    rj->base.vt = (uint64_t)vt;
    rj->hostname = host;  // ownership transferred
    rj->port = (int)port;

    int64_t submit_rc = drift_blocking_submit(&rj->base);
    if (submit_rc != 0) {
        free(rj->hostname);
        free(rj);
        errno = EAGAIN;
        return -1;
    }

    // Register caller's deadline as reactor timer, then park
    if (deadline_ms > 0) {
        drift_reactor_register_timer((uint64_t)deadline_ms, (uint64_t)vt);
    }
    drift_thread_park(0);

    // Resumed — check result
    if (atomic_load(&rj->base.completed)) {
        if (rj->base.error != 0) {
            rj->base.destroy_fn(&rj->base);
            errno = EINVAL;
            return -1;
        }
        struct sockaddr_storage addr;
        memcpy(&addr, &rj->addr, rj->addrlen);
        socklen_t addrlen = rj->addrlen;
        rj->base.destroy_fn(&rj->base);
        return drift_connect_to_addr(&addr, addrlen);
    }
    // Timed out — transfer ownership to worker
    atomic_store(&rj->base.expired, 1);
    errno = EAGAIN;
    return -1;
}
```

## File changes

### 1. `lang/language_runtime/posix/thread_runtime.c`

- Add `DriftBlockingJob` and `DriftBlockingPool` structs (generic, after Reactor)
- Add `drift_blocking_pool_init()`, `drift_blocking_worker()`, `drift_blocking_submit()`, `drift_blocking_pool_shutdown()` (generic)
- Add `DriftResolveJob`, `drift_resolve_job_fn()`, `drift_connect_to_addr()`, `drift_resolve_blocking()` (DNS consumer)
- Add `drift_net_connect()` — replaces io_runtime.c's version
- Add includes: `#include <netdb.h>`, `#include <netinet/in.h>`, `#include <arpa/inet.h>`, `#include <sys/socket.h>`

### 2. `lang/language_runtime/posix/io_runtime.c`

- Remove `drift_net_connect` implementation (moved to thread_runtime.c)
- Remove `#include <netdb.h>` (no longer needed here — check other users first)
- Keep all other functions

### 3. `lang/language_runtime/posix/io_runtime.h`

- Update declaration: `int64_t drift_net_connect(DriftString *ip, int64_t port, int64_t deadline_ms);`

### 4. `stdlib/lang/thread.drift`

- Update intrinsic: `@intrinsic pub fn net_connect(ip: &String, port: Int, deadline_ms: Int) nothrow -> Int;`

### 5. `stdlib/std/net/net.drift`

- In `connect()`: pass deadline to `net_connect`
  ```drift
  val fd = thread.net_connect(&addr.ip, addr.port, deadline);
  ```

### 6. `lang/codegen/llvm/llvm_codegen.py`

- Update declare: add third `i64` parameter to `@drift_net_connect`
- Update call sites (2 locations): pass 3 args, update arg count checks from 2 to 3

### 7. Existing test

- `std_net_tcp_connect_hostname` — validates hostname connect works via blocking pool

## Pool parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Worker threads | 4 | DNS usually fast; handles concurrent bursts |
| Queue limit | 64 | Backpressure; EAGAIN on overflow |
| Init | lazy (pthread_once) | Zero cost if never used |

## Future consumers (aspirational, not in this patch)

The `DriftBlockingPool` + `DriftBlockingJob` abstraction is consumer-agnostic by design. Future internal consumers could include file/process ops or selected blocking FFI integrations — each would provide its own embedded job struct, `job_fn`, and `destroy_fn`. No public user-facing blocking-job submission API is exposed in this patch.

## Verification

1. `PYTHONPATH=. .venv/bin/python lang/tests/codegen/e2e/runner.py --debug std_net_tcp_connect_hostname` — hostname connect via blocking pool
2. `PYTHONPATH=. .venv/bin/python lang/tests/codegen/e2e/runner.py --debug std_net_tcp_listen_accept_connect std_net_tcp_connect_timeout` — IP literal fast path
3. Full e2e suite — no regressions
