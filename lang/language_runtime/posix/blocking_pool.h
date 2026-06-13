#ifndef DRIFT_BLOCKING_POOL_H
#define DRIFT_BLOCKING_POOL_H

#include <stdatomic.h>
#include <stdint.h>

/* Shared bounded blocking-syscall worker pool (defined in thread_runtime.c).
 *
 * Runs syscalls that may block (DNS getaddrinfo, std.fs directory walks, …)
 * on a small fixed set of worker threads so a virtual-thread carrier is never
 * blocked.  4 workers, bounded FIFO queue of 64; submission past the bound
 * returns -1 (backpressure).  Consumers embed DriftBlockingJob as the first
 * member of their own job struct, park the calling VT after submit, and resume
 * when the worker unparks them.  See drift_net_connect (DNS) and fs_runtime.c
 * (read_dir) for the two consumers. */

typedef struct DriftBlockingJob {
	void (*job_fn)(struct DriftBlockingJob *job);     /* runs on a worker thread */
	void (*destroy_fn)(struct DriftBlockingJob *job); /* frees the job */
	uint64_t vt;                                      /* VT handle to unpark */
	atomic_int completed;                             /* 1 once job_fn returned */
	atomic_int expired;                               /* 1 if the VT abandoned it */
	atomic_int refcount;                              /* see ownership note below */
	atomic_int vt_resumed;                            /* claims the single VT wakeup */
	int error;                                        /* consumer-defined result code */
	struct DriftBlockingJob *next;                    /* queue linkage */
} DriftBlockingJob;

/* Ownership protocol (UAF-safe for the timeout/cancel/abandon race).
 *
 * A consumer initializes refcount = 2 before drift_blocking_submit: one stake
 * for the VT, one for the worker.  Each side releases its stake EXACTLY once
 * via drift_blocking_job_release; whichever release brings the count to 0 calls
 * destroy_fn.  Because neither side dereferences the job after its own release,
 * and the job outlives both reads, the worker's post-completion access to
 * job->vt / job->expired can never touch freed memory — closing the narrow
 * "worker completes exactly as the deadline fires" double-free window.
 *
 * vt_resumed is claimed (atomic_exchange to 1) by whichever side resumes the VT
 * first — the worker before its unpark, or the VT itself right after park()
 * returns (e.g. woken by the deadline timer).  The worker skips its unpark if
 * the VT already claimed it, so a late worker cannot deliver a spurious wakeup
 * to a VT that has already moved on.
 *
 * Off-VT (main-thread) callers run the job inline with refcount = 1 and no
 * worker stake. */
int64_t drift_blocking_submit(DriftBlockingJob *job);

/* Drop one stake on a job; frees it (destroy_fn) when the last stake goes. */
void drift_blocking_job_release(DriftBlockingJob *job);

/* Current VT handle (the pointer-as-uint64 used by park/unpark/timer), or 0
 * if not running on a VT (e.g. the main thread). */
uint64_t drift_thread_current_vt_handle(void);

/* Park / unpark the calling / a target VT; register or cancel a reactor
 * deadline timer.  (All defined in thread_runtime.c.) */
void drift_thread_park(uint64_t reason);
void drift_thread_unpark(uint64_t vt);
void drift_reactor_register_timer(uint64_t deadline_ms, uint64_t vt);
void drift_reactor_cancel_vt_timers(uint64_t vt);

/* 1 if the current VT has been cancelled.  A VT parked on a pool job that wakes
 * (e.g. via its deadline timer) reads this to distinguish cancellation from a
 * plain timeout. */
int64_t drift_thread_is_cancelled(void);

#endif  // DRIFT_BLOCKING_POOL_H
