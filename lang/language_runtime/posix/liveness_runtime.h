#ifndef DRIFT_LIVENESS_RUNTIME_H
#define DRIFT_LIVENESS_RUNTIME_H

/* Runtime liveness interrogator (Slice 1: passive dump plumbing).
 *
 * Provides an operator-triggerable snapshot of the VT scheduler so a stuck
 * process can be diagnosed in prod without a debugger.  Triggered live via
 * `kill -USR2 <pid>`, handled by a dedicated runtime thread that is
 * independent of the reactor / executor / app loggers (any of which may be
 * the wedged component).  Emits a bounded human summary to stderr and a full
 * `drift.liveness.v1` JSON document to a file.
 *
 * No watchdog/abort yet (Slices 3-4). */

#include <stdint.h>

/* Reason a snapshot was produced. */
#define DRIFT_LIVENESS_REASON_OPERATOR_SIGNAL    0
#define DRIFT_LIVENESS_REASON_WATCHDOG_NO_PROGRESS 1

/* wait_kind values stored on each VT.  Mutex is intentionally absent: the
 * stdlib `Mutex<T>` is a spin-lock, so contention shows up as a long-RUNNING
 * VT, never a parked state.  These integer values are shared with the stdlib
 * `vt_set_wait(kind, id)` callers in std.concurrent — keep them in sync. */
enum DriftWaitKind {
	DRIFT_WAIT_NONE    = 0,
	DRIFT_WAIT_TIMER   = 1,
	DRIFT_WAIT_IO      = 2,
	DRIFT_WAIT_JOIN    = 3,
	DRIFT_WAIT_CONDVAR = 4,
	DRIFT_WAIT_CHANNEL = 5,
	/* Parked waiting for blocking-executor admission (Block(timeout)
	 * saturation).  wait_id carries the executor's stable id. */
	DRIFT_WAIT_BLOCKING_ADMISSION = 6,
};

/* Compiler-emitted FFI callsite record (blocking-FFI observability,
 * ABI 21).  One static instance per instrumented extern "C" callsite,
 * in rodata: the current VT holds a single atomic pointer to it while
 * the C call is in flight, so a snapshot's one acquire load always
 * observes a consistent {symbol, file, line} triple and the pointee
 * never dies.  Layout is an ABI contract with codegen ({ptr, ptr, i64}). */
typedef struct DriftFfiSite {
	const char *symbol;
	const char *file;
	int64_t line;
} DriftFfiSite;

#define DRIFT_LIVENESS_MAX_EXECS 64
#define DRIFT_LIVENESS_OP_LABEL_CAP 48
#define DRIFT_LIVENESS_EXEC_NAME_CAP 32

/* Per-executor snapshot entry (blocking-FFI observability).  `id` is
 * the registration ordinal assigned at exec creation — stable for the
 * process lifetime; VT records join on it via wait_id / exec_id. */
typedef struct DriftExecSnapshot {
	uint64_t id;
	char     name[DRIFT_LIVENESS_EXEC_NAME_CAP];
	int      name_len;
	int64_t  queue_len;
	int      running;
	int64_t  queue_limit;
	int64_t  waiters;
	int      workers;
	int      shutting_down;
} DriftExecSnapshot;

/* Mirror of DriftVtState in thread_runtime.c (distinct names to avoid a
 * redefinition clash where that file includes this header).  Keep in sync. */
enum DriftLvVtState {
	DRIFT_LV_VT_NEW       = 0,
	DRIFT_LV_VT_READY     = 1,
	DRIFT_LV_VT_RUNNING   = 2,
	DRIFT_LV_VT_PARKED    = 3,
	DRIFT_LV_VT_FINISHED  = 4,
	DRIFT_LV_VT_CANCELLED = 5,
};

/* Bounds on what a single snapshot will copy out of the live registries.
 * Exceeding a bound sets the matching `*_truncated` flag rather than growing
 * unboundedly while holding scheduler locks. */
#define DRIFT_LIVENESS_MAX_VTS     4096
#define DRIFT_LIVENESS_MAX_TIMERS  4096
#define DRIFT_LIVENESS_MAX_WATCHES 4096

typedef struct DriftVtSnapshot {
	uint64_t vtid;             /* same value as std.log's vtid (thread.vt_id()) */
	int      state;             /* DriftVtState */
	int      wait_kind;         /* enum DriftWaitKind */
	uint64_t wait_id;           /* opaque: joined vt_id / condvar id / channel id */
	int64_t  state_since_ms;    /* monotonic ms at last state transition */
	uint64_t carrier_tid;       /* OS tid running this VT, 0 if not running */
	uint64_t last_progress;     /* progress counter snapshot at last run */
	/* Resolved wait targets (filled from the reactor when applicable). */
	int64_t  timer_deadline_ms; /* TIMER: absolute monotonic deadline, else -1 */
	int      io_fd;             /* IO: fd, else -1 */
	uint32_t io_events;         /* IO: interest mask (EPOLLIN|EPOLLOUT) */
	/* Blocking-FFI observability (ABI 21). */
	char     op_label[DRIFT_LIVENESS_OP_LABEL_CAP];
	int      op_len;            /* 0 = no label */
	uint64_t submitter_vtid;    /* 0 = none recorded */
	uint64_t exec_id;           /* executor the VT last enqueued on; 0 = none */
	const char *ffi_symbol;     /* rodata, NULL when not inside an extern call */
	const char *ffi_file;
	int64_t  ffi_line;
} DriftVtSnapshot;

typedef struct DriftLivenessSnapshot {
	int      pid;
	int64_t  uptime_ms;
	int      reason;
	uint64_t progress_counter;
	int64_t  now_ms;

	/* Executor (default executor for v1). */
	int      exec_present;
	int      exec_workers;
	int64_t  exec_ready_queue_len;
	int      exec_running;       /* exec->running atomic */
	int      exec_shutting_down;
	uint64_t exec_completed;     /* process-wide completed VT count */

	/* Reactor (default reactor for v1). */
	int      reactor_present;
	int      reactor_fd_waiters;
	int      reactor_timers;
	int64_t  reactor_next_deadline_ms; /* min pending deadline, else -1 */

	/* Derived VT-state tallies (from the VT registry walk). */
	int      vt_count;
	int      vt_truncated;
	int      tally_running;
	int      tally_ready;
	int      tally_parked;
	int      tally_finished;
	int      tally_cancelled;
	int      tally_wait[7];      /* indexed by enum DriftWaitKind */

	/* Degraded-section markers: a failed trylock leaves the section empty
	 * and flags it here so the dump never lies about completeness. */
	int      degraded_vt_registry;
	int      degraded_exec_registry;
	int      degraded_reactor;

	/* Per-executor snapshots (blocking-FFI observability). */
	int      exec_count;
	int      execs_truncated;
	DriftExecSnapshot execs[DRIFT_LIVENESS_MAX_EXECS];

	DriftVtSnapshot vts[DRIFT_LIVENESS_MAX_VTS];
} DriftLivenessSnapshot;

/* Implemented in thread_runtime.c (which owns the VT/exec/reactor structs and
 * their mutexes).  Fills *out using bounded trylock; never blocks on a lock a
 * wedged carrier may hold. */
void drift_liveness_collect(DriftLivenessSnapshot *out, int reason);

/* Process start time (monotonic ms), published by drift_run_main_on_vt so the
 * collector can compute uptime.  0 until set. */
void drift_liveness_set_start_ms(int64_t start_ms);

/* Implemented in liveness_runtime.c. */
void drift_liveness_emit(int reason);     /* collect, then write text + JSON */
void drift_liveness_thread_start(void);   /* spawn the dedicated sigwait thread (idempotent) */
void drift_liveness_thread_shutdown(void); /* stop + join the sigwait thread (idempotent) */

#endif /* DRIFT_LIVENESS_RUNTIME_H */
