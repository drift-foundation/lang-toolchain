#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
#include <errno.h>
#include <stdatomic.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#ifdef __linux__
#include <ucontext.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/timerfd.h>
#include <sys/signalfd.h>
#include <sys/mman.h>
#include <unistd.h>
#include <signal.h>
#endif

#ifdef NVALGRIND
#define VALGRIND_STACK_REGISTER(start, end) (0)
#define VALGRIND_STACK_DEREGISTER(id) do {} while(0)
#define RUNNING_ON_VALGRIND 0
#elif __has_include(<valgrind/valgrind.h>)
#include <valgrind/valgrind.h>
#else
#define VALGRIND_STACK_REGISTER(start, end) (0)
#define VALGRIND_STACK_DEREGISTER(id) do {} while(0)
#define RUNNING_ON_VALGRIND 0
#endif

#include "string_runtime.h"
#include "posix/liveness_runtime.h"
#include "posix/blocking_pool.h"
#ifdef __linux__
#include "posix/drift_context.h"
#include <sys/syscall.h>
#endif

/* When running under Valgrind, fall back to glibc swapcontext/makecontext.
 * Valgrind's VEX JIT cannot follow raw %rsp manipulation in drift_swapcontext
 * and crashes with an internal SIGSEGV.  Detected once at executor creation. */
static int drift_valgrind_mode = 0;

/* Free a fiber stack.  When is_mmap is true the region was obtained via
 * mmap with a guard page one page below stack_ptr; otherwise it was a
 * plain malloc allocation. */
static void drift_fiber_stack_free(void *stack_ptr, size_t stack_size, int is_mmap) {
	if (!stack_ptr) return;
#ifdef __linux__
	if (is_mmap) {
		size_t page_sz = (size_t)sysconf(_SC_PAGESIZE);
		void *map_base = (char *)stack_ptr - page_sz;
		munmap(map_base, stack_size + page_sz);
		return;
	}
#else
	(void)is_mmap;
#endif
	free(stack_ptr);
	(void)stack_size;
}

// Drift interface value layout (ABI v1):
// { i8*, i8*, [4 x i64], i8 }
// Align to 8 and pad to full size (56 bytes on 64-bit).
typedef struct DriftIface {
	void *data;
	void *vtable;
	uint64_t inline_words[4];
	uint8_t is_inline;
	uint8_t _pad[7];
} DriftIface;

typedef struct DriftCallbackVTable {
	void *drop;
	void *call;
} DriftCallbackVTable;

struct DriftRuntimeRegistryEntry;

typedef struct DriftVt {
	DriftIface cb;
	pthread_t thread;
	// Written by the worker thread when it begins execution.
	// Other threads may read it; keep single-writer invariant unless protected.
	atomic_int started;
	atomic_int completed;
	atomic_int cancelled;
	atomic_int dropped;
	atomic_int state;
	// Stack is allocated/freed by the worker thread (single owner).
	void *stack;
	size_t stack_size;
	int stack_is_mmap;  // 1 = mmap+guard page, 0 = malloc
	unsigned valgrind_stack_id;
#ifdef __linux__
	DriftContext ctx;
	ucontext_t ctx_uc;  /* Valgrind fallback — used when drift_valgrind_mode. */
	// Context is initialized once by the worker thread (single-writer).
	int ctx_ready;
#endif
	struct DriftExec *exec;
	pthread_mutex_t mu;
	pthread_cond_t cv;
	/* Wake latch for the park/unpark handshake.  Binary: a resumer deposits 1,
	 * a park consumes by atomic_exchange(...,0).  Accessed with seq_cst so the
	 * park "set PARKED then re-check token" and unpark "set token then re-check
	 * state" double-handshake has no lost-wake window.  See drift_thread_park /
	 * drift_thread_unpark. */
	atomic_int park_token;
	struct DriftVt *reg_prev;
	struct DriftVt *reg_next;
	struct DriftRuntimeRegistryEntry *thread_registry_head;
	int64_t io_bytes_since_yield;   /* ET fairness: cumulative successful IO since last yield */
	uint64_t join_waiter;           /* VT handle to unpark on completion, or 0 */
	uint64_t vtid;                  /* Process-unique VT id, assigned at spawn (1-based). 0 = not on a VT. */
	/* Liveness interrogator bookkeeping (Slice 1).  All best-effort, read
	 * lock-free by the liveness thread; correctness of the program does not
	 * depend on them. */
	atomic_int wait_kind;               /* enum DriftWaitKind; refines DRIFT_VT_PARKED */
	atomic_uint_fast64_t wait_id;       /* opaque wait-object id (joined vtid / condvar / channel) */
	atomic_int_fast64_t state_since_ms; /* monotonic ms at last RUNNING/PARKED transition */
	atomic_uint_fast64_t carrier_tid;   /* OS tid currently running this VT, 0 if none */
	atomic_uint_fast64_t last_progress; /* progress counter snapshot at last resume */
} DriftVt;

typedef enum DriftVtState {
	DRIFT_VT_NEW = 0,
	DRIFT_VT_READY = 1,
	DRIFT_VT_RUNNING = 2,
	DRIFT_VT_PARKED = 3,
	DRIFT_VT_FINISHED = 4,
	DRIFT_VT_CANCELLED = 5,
} DriftVtState;

typedef void (*DriftCallback0)(void *data);

typedef void (*DriftCallbackDrop)(void *data);

static _Atomic uint64_t drift_default_executor = 0;
static _Atomic uint64_t drift_default_reactor = 0;
static int64_t drift_exec_submit_override = -1;
static pthread_once_t drift_reactor_once = PTHREAD_ONCE_INIT;
static pthread_once_t drift_reactor_shutdown_once = PTHREAD_ONCE_INIT;
static pthread_once_t drift_vt_tls_once = PTHREAD_ONCE_INIT;
static pthread_key_t drift_vt_tls_key;
static pthread_once_t drift_exec_once = PTHREAD_ONCE_INIT;
static pthread_once_t drift_exec_cleanup_once = PTHREAD_ONCE_INIT;
typedef struct DriftRuntimeRegistryEntry {
	uint64_t type_tag;
	void *ptr;
	DriftIface dropper;
	struct DriftRuntimeRegistryEntry *next;
} DriftRuntimeRegistryEntry;
typedef struct DriftRuntimeGlobalRegistry {
	int64_t _opaque;
} DriftRuntimeGlobalRegistry;
static pthread_mutex_t drift_runtime_registry_mu = PTHREAD_MUTEX_INITIALIZER;
static DriftRuntimeRegistryEntry *drift_runtime_registry_head = NULL;
static __thread DriftRuntimeRegistryEntry *drift_runtime_thread_registry_head = NULL;
static pthread_once_t drift_runtime_registry_cleanup_once = PTHREAD_ONCE_INIT;
static _Atomic int drift_runtime_registry_cleaned = 0;
static DriftRuntimeGlobalRegistry drift_runtime_global_registry = {0};
static DriftRuntimeGlobalRegistry drift_runtime_thread_registry = {0};
static void drift_drop_callback(DriftIface *cb);
static void drift_exec_shutdown_all_atexit(void);
static void drift_runtime_registry_cleanup_atexit(void);
static pthread_mutex_t drift_vt_registry_mu = PTHREAD_MUTEX_INITIALIZER;
static DriftVt *drift_vt_registry_head = NULL;
static void drift_reactor_forget_vt(DriftVt *vt);

static void drift_runtime_registry_register_cleanup_once(void) {
	(void)atexit(drift_runtime_registry_cleanup_atexit);
}

static void drift_runtime_registry_entry_list_cleanup(DriftRuntimeRegistryEntry **head) {
	DriftRuntimeRegistryEntry *cur = *head;
	*head = NULL;
	while (cur) {
		DriftRuntimeRegistryEntry *next = cur->next;
		void *drop_data = cur->dropper.data;
		if ((cur->dropper.is_inline & 1) != 0) {
			drop_data = (void *)cur->dropper.inline_words;
		}
		DriftCallbackVTable *vt = (DriftCallbackVTable *)cur->dropper.vtable;
		if (vt && vt->call) {
			((void (*)(void *, void *))vt->call)(drop_data, cur->ptr);
		}
		if (vt && vt->drop) {
			((DriftCallbackDrop)vt->drop)(drop_data);
		}
		free(cur);
		cur = next;
	}
}

static void drift_runtime_registry_cleanup_atexit(void) {
	if (atomic_exchange_explicit(&drift_runtime_registry_cleaned, 1, memory_order_acq_rel) != 0) {
		return;
	}
	pthread_mutex_lock(&drift_runtime_registry_mu);
	drift_runtime_registry_entry_list_cleanup(&drift_runtime_registry_head);
	pthread_mutex_unlock(&drift_runtime_registry_mu);
	drift_runtime_registry_entry_list_cleanup(&drift_runtime_thread_registry_head);
}

void drift_runtime_registry_cleanup_now(void) {
	drift_runtime_registry_cleanup_atexit();
}

static void drift_vt_registry_add(DriftVt *vt) {
	if (!vt) {
		return;
	}
	pthread_mutex_lock(&drift_vt_registry_mu);
	vt->reg_prev = NULL;
	vt->reg_next = drift_vt_registry_head;
	if (drift_vt_registry_head) {
		drift_vt_registry_head->reg_prev = vt;
	}
	drift_vt_registry_head = vt;
	pthread_mutex_unlock(&drift_vt_registry_mu);
}

static void drift_vt_registry_remove(DriftVt *vt) {
	if (!vt) {
		return;
	}
	pthread_mutex_lock(&drift_vt_registry_mu);
	if (vt->reg_prev) {
		vt->reg_prev->reg_next = vt->reg_next;
	} else if (drift_vt_registry_head == vt) {
		drift_vt_registry_head = vt->reg_next;
	}
	if (vt->reg_next) {
		vt->reg_next->reg_prev = vt->reg_prev;
	}
	vt->reg_prev = NULL;
	vt->reg_next = NULL;
	pthread_mutex_unlock(&drift_vt_registry_mu);
}

static void drift_vt_destroy(DriftVt *h) {
	if (!h) {
		return;
	}
	pthread_mutex_lock(&h->mu);
	drift_runtime_registry_entry_list_cleanup(&h->thread_registry_head);
	pthread_mutex_unlock(&h->mu);
	drift_reactor_forget_vt(h);
	drift_vt_registry_remove(h);
	pthread_mutex_destroy(&h->mu);
	pthread_cond_destroy(&h->cv);
	if (h->stack) {
		VALGRIND_STACK_DEREGISTER(h->valgrind_stack_id);
		drift_fiber_stack_free(h->stack, h->stack_size, h->stack_is_mmap);
		h->stack = NULL;
		h->stack_size = 0;
	}
	free(h);
}

static void drift_vt_registry_cleanup_atexit(void) {
	/* Ensure no executor workers are concurrently mutating VT lifetime/registry
	 * while process-exit VT cleanup walks and tears down the registry list. */
	drift_exec_shutdown_all_atexit();
	pthread_mutex_lock(&drift_vt_registry_mu);
	DriftVt *cur = drift_vt_registry_head;
	drift_vt_registry_head = NULL;
	pthread_mutex_unlock(&drift_vt_registry_mu);
	while (cur) {
		DriftVt *next = cur->reg_next;
		cur->reg_prev = NULL;
		cur->reg_next = NULL;
		pthread_mutex_lock(&cur->mu);
		drift_runtime_registry_entry_list_cleanup(&cur->thread_registry_head);
		pthread_mutex_unlock(&cur->mu);
		if (!atomic_load(&cur->completed)) {
			drift_drop_callback(&cur->cb);
			atomic_store(&cur->completed, 1);
		}
		pthread_mutex_destroy(&cur->mu);
		pthread_cond_destroy(&cur->cv);
		if (cur->stack) {
			VALGRIND_STACK_DEREGISTER(cur->valgrind_stack_id);
			drift_fiber_stack_free(cur->stack, cur->stack_size, cur->stack_is_mmap);
			cur->stack = NULL;
			cur->stack_size = 0;
		}
		free(cur);
		cur = next;
	}
}

__attribute__((constructor))
static void drift_vt_registry_register_cleanup_ctor(void) {
	(void)atexit(drift_vt_registry_cleanup_atexit);
}

typedef struct ReactorTimer {
	int64_t deadline_ms;
	uint64_t vt;
	struct ReactorTimer *next;
} ReactorTimer;

/* Fairness budget: max bytes a VT may drain per scheduler turn before
 * yielding.  Prevents a single hot fd from starving other VTs under
 * edge-triggered epoll.  Internal constant — not user-configurable. */
#define DRIFT_IO_BUDGET_BYTES 65536

typedef struct ReactorWatch {
	int fd;
	uint32_t events;          /* kernel interest mask (set once on EPOLL_CTL_ADD) */
	uint64_t read_vt;         /* VT parked for EPOLLIN, or 0 */
	uint64_t write_vt;        /* VT parked for EPOLLOUT, or 0 */
	uint8_t  pending_read;    /* 1 = fd readable, edge not yet consumed to EAGAIN */
	uint8_t  pending_write;   /* 1 = fd writable, edge not yet consumed to EAGAIN */
	struct ReactorWatch *next;
} ReactorWatch;

/* Poll ownership: exactly one thread calls epoll_wait at a time.
 * POLL_OWNER_REACTOR (default): reactor thread owns epoll_wait.
 * POLL_OWNER_WORKER: the single idle worker owns epoll_wait. */
#define POLL_OWNER_REACTOR 0
#define POLL_OWNER_WORKER  1

typedef struct Reactor {
	int epoll_fd;
	int wake_fd;
	int signal_fd;  /* signalfd for SIGINT/SIGTERM/SIGUSR1, -1 if not registered */
	pthread_mutex_t mu;
	pthread_cond_t cv;
	ReactorTimer *timers;
	ReactorWatch *watches;
	int stopping;
	pthread_t thread;
	int thread_started;
	atomic_int in_wait;
	atomic_int poll_owner;
} Reactor;

static Reactor *drift_default_reactor_ptr = NULL;
static void drift_reactor_shutdown_default_atexit(void);

/* Process-global signal handling state (Linux only).
 * signal_fd is created at runtime init (drift_run_main_on_vt) before any
 * worker threads are spawned.  SIGINT/SIGTERM/SIGUSR1 are blocked
 * process-wide so signalfd is the sole consumer. */
static int drift_signal_fd = -1;
static atomic_uintptr_t drift_signal_waiter_vt = 0;
static atomic_int drift_signal_delivered_signo = 0;

/* Process-global VT id counter.  Starts at 1; 0 is the sentinel
 * for "not running on a VT". */
static atomic_uint_fast64_t drift_vtid_counter = 1;

/* Liveness interrogator counters (Slice 1).  drift_progress_counter is bumped
 * on every observable scheduler advance (VT resume, VT completion); sampling
 * it twice tells an operator whether the runtime is making progress at all.
 * drift_completed_counter is the process-wide finished-VT total. */
static atomic_uint_fast64_t drift_progress_counter = 0;
static atomic_uint_fast64_t drift_completed_counter = 0;

/* Relaxed accessors for best-effort liveness bookkeeping. These fields and
 * counters carry no synchronization meaning — the liveness reader tolerates a
 * slightly-stale heartbeat — so they use memory_order_relaxed to keep the
 * seq_cst default off the scheduler hot path (drift_vt_tls_set / park). */
static inline void lv_set_i(atomic_int *p, int v) { atomic_store_explicit(p, v, memory_order_relaxed); }
static inline void lv_set_u(atomic_uint_fast64_t *p, uint64_t v) { atomic_store_explicit(p, v, memory_order_relaxed); }
static inline void lv_set_t(atomic_int_fast64_t *p, int64_t v) { atomic_store_explicit(p, v, memory_order_relaxed); }
static inline uint64_t lv_bump(atomic_uint_fast64_t *p) { return atomic_fetch_add_explicit(p, 1, memory_order_relaxed) + 1; }
static inline int lv_get_i(atomic_int *p) { return atomic_load_explicit(p, memory_order_relaxed); }
static inline uint64_t lv_get_u(atomic_uint_fast64_t *p) { return atomic_load_explicit(p, memory_order_relaxed); }
static inline int64_t lv_get_t(atomic_int_fast64_t *p) { return atomic_load_explicit(p, memory_order_relaxed); }

/* Per-thread cached kernel TID.  gettid() is a bare syscall on glibc, so cache
 * it to keep the context-switch hot path cheap. */
#ifdef __linux__
static __thread uint64_t drift_cached_tid = 0;
static uint64_t drift_self_tid(void) {
	if (drift_cached_tid == 0) {
		drift_cached_tid = (uint64_t)syscall(SYS_gettid);
	}
	return drift_cached_tid;
}
#else
static uint64_t drift_self_tid(void) { return 0; }
#endif

static void drift_reactor_forget_vt(DriftVt *vt) {
#ifdef __linux__
	Reactor *r = (Reactor *)atomic_load(&drift_default_reactor);
	if (!r || !vt) {
		return;
	}
	pthread_mutex_lock(&r->mu);
	ReactorTimer *tp = NULL;
	ReactorTimer *tc = r->timers;
	while (tc) {
		ReactorTimer *tn = tc->next;
		if (tc->vt == (uint64_t)vt) {
			if (tp) {
				tp->next = tn;
			} else {
				r->timers = tn;
			}
			free(tc);
		} else {
			tp = tc;
		}
		tc = tn;
	}
	ReactorWatch *wc = r->watches;
	while (wc) {
		if (wc->read_vt == (uint64_t)vt) {
			wc->read_vt = 0;
			wc->pending_read = 0;
		}
		if (wc->write_vt == (uint64_t)vt) {
			wc->write_vt = 0;
			wc->pending_write = 0;
		}
		wc = wc->next;
	}
	pthread_mutex_unlock(&r->mu);
#else
	(void)vt;
#endif
}

static void drift_reactor_register_shutdown_once(void) {
	(void)atexit(drift_reactor_shutdown_default_atexit);
}

static void drift_run_callback(DriftIface *cb, int do_free);
static void drift_drop_callback(DriftIface *cb);
#ifdef __linux__
static void drift_vt_fiber_entry(uintptr_t arg);
#endif
void drift_thread_unpark(uint64_t vt);
void drift_thread_park(uint64_t reason);
void drift_blocking_pool_quiesce(void);  /* defined below; called in teardown (Finding 1) */
void drift_reactor_register_timer(uint64_t deadline_ms, uint64_t vt);
/* drift_reactor_cancel_vt_timers is declared in posix/blocking_pool.h (shared
 * with fs_runtime.c, the second blocking-pool consumer). */
uint64_t drift_exec_default_get(void);
uint64_t drift_reactor_default_get(void);
uint64_t drift_exec_submit(uint64_t exec, uint64_t vt);
uint64_t drift_thread_spawn(DriftIface *cb_ptr, uint64_t exec);

typedef struct ExecNode {
	DriftVt *vt;
	struct ExecNode *next;
} ExecNode;

#define EXEC_NODE_FREELIST_CAP 16

typedef struct DriftExec {
	pthread_mutex_t mu;
	pthread_cond_t cv;
	ExecNode *head;
	ExecNode *tail;
	int shutting_down;
	int64_t queue_len;
	int64_t queue_limit;
	atomic_int running;
	int threads_count;
	pthread_t *threads;
	size_t stack_bytes;
	int destroyed;
	struct DriftExec *reg_prev;
	struct DriftExec *reg_next;
	ExecNode *node_freelist;
	int node_freelist_len;
} DriftExec;

/* ExecNode freelist helpers.  Caller must hold exec->mu. */
static ExecNode *exec_node_alloc(DriftExec *exec) {
	if (exec->node_freelist) {
		ExecNode *n = exec->node_freelist;
		exec->node_freelist = n->next;
		exec->node_freelist_len--;
		return n;
	}
	return (ExecNode *)malloc(sizeof(ExecNode));
}

static void exec_node_release(DriftExec *exec, ExecNode *node) {
	if (exec->node_freelist_len < EXEC_NODE_FREELIST_CAP) {
		node->next = exec->node_freelist;
		exec->node_freelist = node;
		exec->node_freelist_len++;
	} else {
		free(node);
	}
}

static __thread DriftExec *drift_exec_tls = NULL;
static pthread_mutex_t drift_exec_registry_mu = PTHREAD_MUTEX_INITIALIZER;
static DriftExec *drift_exec_registry_head = NULL;

static void drift_exec_registry_add(DriftExec *exec) {
	if (!exec) {
		return;
	}
	pthread_mutex_lock(&drift_exec_registry_mu);
	exec->reg_prev = NULL;
	exec->reg_next = drift_exec_registry_head;
	if (drift_exec_registry_head) {
		drift_exec_registry_head->reg_prev = exec;
	}
	drift_exec_registry_head = exec;
	pthread_mutex_unlock(&drift_exec_registry_mu);
}

static void drift_exec_registry_remove(DriftExec *exec) {
	if (!exec) {
		return;
	}
	pthread_mutex_lock(&drift_exec_registry_mu);
	if (exec->reg_prev) {
		exec->reg_prev->reg_next = exec->reg_next;
	} else if (drift_exec_registry_head == exec) {
		drift_exec_registry_head = exec->reg_next;
	}
	if (exec->reg_next) {
		exec->reg_next->reg_prev = exec->reg_prev;
	}
	exec->reg_prev = NULL;
	exec->reg_next = NULL;
	pthread_mutex_unlock(&drift_exec_registry_mu);
}
#ifdef __linux__
static __thread DriftContext *drift_sched_ctx = NULL;
static __thread ucontext_t *drift_sched_ctx_uc = NULL;
#endif

static int64_t drift_now_ms(void) {
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
		return 0;
	}
	return (int64_t)(ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL);
}

/* Transition a VT to READY, re-stamping liveness state_since_ms so the dump
 * reports a fresh ready-queue age (not the stale age of its prior PARKED/
 * RUNNING state).  Keeps the diagnostic for "ready queue stuck / scheduler not
 * draining" honest.  `state` stays seq_cst (it has scheduler-visible meaning);
 * the timestamp is relaxed best-effort. */
static inline void drift_vt_set_ready(DriftVt *vt) {
	lv_set_t(&vt->state_since_ms, drift_now_ms());
	atomic_store(&vt->state, DRIFT_VT_READY);
}

/* Single-claim resume primitive (Findings 2 & the round-10 deadlock fix): EVERY
 * path that resumes a parked VT — reactor IO event, deadline timer, cancellation,
 * worker completion — claims it by CAS PARKED→READY, so exactly one wins.  A
 * plain load-PARKED-then-store let two resumers both fire (a duplicate resume).
 *
 * The claim target is always READY, never RUNNING — even for the reactor's
 * direct (in-place) resume.  While the VT is READY, other unparks recognize the
 * wake is already claimed and DO NOT deposit a park_token.  If a direct resumer
 * claimed RUNNING instead, a concurrent unpark would see RUNNING, deposit a
 * token, the parking VT would consume it and return WITHOUT suspending, and the
 * direct resumer's carrier_tid==0 guard would wait forever (deadlock).  The
 * direct-resume path flips READY→RUNNING itself, only after the VT has suspended
 * (carrier_tid==0), immediately before the swapcontext.
 * Returns 1 if this caller won the claim (and must perform the resume). */
static inline int drift_vt_claim_for_resume(DriftVt *vt) {
	int expected = DRIFT_VT_PARKED;
	if (atomic_compare_exchange_strong(&vt->state, &expected, DRIFT_VT_READY)) {
		lv_set_t(&vt->state_since_ms, drift_now_ms());
		return 1;
	}
	return 0;
}

/* Test-only (Finding 1, round 11): counts the reactor's successful DIRECT-resume
 * claims — i.e. the poller (not a competing unpark/cancel) won PARKED→READY and
 * is about to resume the VT in place.  A test waits until this advances to be
 * SURE the reactor won the claim, then races a cancellation against the
 * READY→RUNNING window below.  Without this, on a single-worker executor the
 * cancel always claims first and the direct-resume CAS never executes, so the
 * race the test means to cover never runs. */
static atomic_long drift_test_direct_resume_claims = 0;
int64_t drift_vt_test_direct_resume_claims(void) {
	return (int64_t)atomic_load(&drift_test_direct_resume_claims);
}

/* Test-only (Finding 1, round 11): pause the reactor in the direct-resume path
 * AFTER it has claimed the VT (READY) and the VT has suspended (carrier_tid==0),
 * but BEFORE the READY→RUNNING flip + swapcontext.  Widens the window in which a
 * racing cancellation must be a no-op because the reactor already owns the
 * resume. */
static int drift_test_direct_resume_pause_ms = -1;
static pthread_once_t drift_test_direct_resume_pause_once = PTHREAD_ONCE_INIT;
static void drift_test_direct_resume_pause_init(void) {
	const char *e = getenv("DRIFT_TEST_DIRECT_RESUME_PAUSE_MS");
	drift_test_direct_resume_pause_ms = (e && *e) ? atoi(e) : 0;
}
static void drift_test_direct_resume_pause(void) {
	pthread_once(&drift_test_direct_resume_pause_once, drift_test_direct_resume_pause_init);
	int ms = drift_test_direct_resume_pause_ms;
	if (ms > 0) {
		struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
		nanosleep(&ts, NULL);
	}
}

static void drift_vt_tls_init_once(void) {
	pthread_key_create(&drift_vt_tls_key, NULL);
}

static void drift_vt_tls_set(DriftVt *vt) {
	pthread_once(&drift_vt_tls_once, drift_vt_tls_init_once);
	/* Liveness bookkeeping: this is the single chokepoint every VT
	 * resume/suspend passes through, so attribute the carrier here.
	 * Non-NULL => the calling carrier is about to run `vt`; NULL => the
	 * carrier just suspended whatever VT it was running. */
	if (vt) {
		uint64_t prog = lv_bump(&drift_progress_counter);
		lv_set_u(&vt->carrier_tid, drift_self_tid());
		lv_set_u(&vt->last_progress, prog);
		lv_set_t(&vt->state_since_ms, drift_now_ms());
		lv_set_i(&vt->wait_kind, DRIFT_WAIT_NONE);
		lv_set_u(&vt->wait_id, 0);
	} else {
		DriftVt *cur = (DriftVt *)pthread_getspecific(drift_vt_tls_key);
		if (cur) {
			lv_set_u(&cur->carrier_tid, 0);
		}
	}
	pthread_setspecific(drift_vt_tls_key, vt);
}

static DriftVt *drift_vt_tls_get(void) {
	pthread_once(&drift_vt_tls_once, drift_vt_tls_init_once);
	return (DriftVt *)pthread_getspecific(drift_vt_tls_key);
}

#ifdef __linux__
static ReactorWatch *drift_reactor_find_watch(Reactor *r, int fd);
static void drift_reactor_collect_timers(Reactor *r, int64_t now_ms, ReactorTimer **out);
static int64_t drift_now_ms(void);
#endif

static void drift_worker_vt_finish(DriftVt *vt) {
	if (vt->stack) {
		VALGRIND_STACK_DEREGISTER(vt->valgrind_stack_id);
		drift_fiber_stack_free(vt->stack, vt->stack_size, vt->stack_is_mmap);
		vt->stack = NULL;
		vt->stack_size = 0;
	}
	pthread_mutex_lock(&vt->mu);
	atomic_store(&vt->completed, 1);
	lv_set_t(&vt->state_since_ms, drift_now_ms());
	lv_bump(&drift_completed_counter);
	lv_bump(&drift_progress_counter);
	int dropped_after_finish = atomic_load(&vt->dropped);
	atomic_store(&vt->park_token, 1);
	uint64_t waiter = vt->join_waiter;
	vt->join_waiter = 0;
	pthread_cond_broadcast(&vt->cv);
	pthread_mutex_unlock(&vt->mu);
	if (waiter != 0) {
		drift_thread_unpark(waiter);
	}
	if (dropped_after_finish) {
		drift_vt_destroy(vt);
	}
}

/* Phase A: worker-side polling.  When the single worker's run queue is empty,
 * the worker claims poll ownership and calls epoll_wait directly, resuming
 * I/O-ready VTs without the reactor → executor cross-thread handoff.
 *
 * Returns 1 if the worker handled at least one event (caller should re-check
 * the run queue), 0 if it should fall through to condvar_wait. */
static void drift_exec_enqueue(DriftExec *exec, DriftVt *vt);

#ifdef __linux__
static int drift_worker_poll(DriftExec *exec, DriftContext *sched_ctx) {
	Reactor *r = drift_default_reactor_ptr;
	if (!r || r->epoll_fd < 0 || exec->threads_count != 1) return 0;

	/* W2: CAS poll_owner from REACTOR to WORKER. */
	int expected = POLL_OWNER_REACTOR;
	if (!atomic_compare_exchange_strong(&r->poll_owner, &expected, POLL_OWNER_WORKER)) {
		return 0;
	}

	/* W3: publish in_wait before releasing exec->mu so any concurrent
	 * drift_reactor_wake sees us and writes to wake_fd. */
	atomic_store_explicit(&r->in_wait, 1, memory_order_release);
	pthread_mutex_unlock(&exec->mu);

	int did_work = 0;
	struct epoll_event events[16];

	for (;;) {
		/* W4: compute epoll timeout from timer list. */
		int timeout_ms = -1;
		pthread_mutex_lock(&r->mu);
		if (r->timers) {
			int64_t now_ms = drift_now_ms();
			int64_t min_deadline = r->timers->deadline_ms;
			for (ReactorTimer *t = r->timers; t; t = t->next) {
				if (t->deadline_ms < min_deadline)
					min_deadline = t->deadline_ms;
			}
			int64_t delta = min_deadline - now_ms;
			if (delta <= 0) timeout_ms = 0;
			else if (delta > INT32_MAX) timeout_ms = INT32_MAX;
			else timeout_ms = (int)delta;
		}
		pthread_mutex_unlock(&r->mu);

		/* W5: Publish in_wait before re-checking the run queue.  Any
		 * concurrent drift_reactor_wake after our check will see
		 * in_wait=1 and write to wake_fd, so epoll_wait returns.
		 * Without this, a timer collected by the reactor between the
		 * previous W8 check and here could enqueue work while in_wait
		 * was 0 — the wake would be lost and epoll_wait(-1) blocks
		 * forever. */
		atomic_store_explicit(&r->in_wait, 1, memory_order_release);
		pthread_mutex_lock(&exec->mu);
		if (exec->head || exec->shutting_down) {
			atomic_store_explicit(&r->in_wait, 0, memory_order_relaxed);
			goto release_poll;
		}
		pthread_mutex_unlock(&exec->mu);
		int n = epoll_wait(r->epoll_fd, events, 16, timeout_ms);
		/* W6 */
		atomic_store_explicit(&r->in_wait, 0, memory_order_relaxed);

		if (n < 0 && errno == EINTR) {
			/* Check if we should exit poll mode. */
			pthread_mutex_lock(&exec->mu);
			if (exec->head || exec->shutting_down) {
				goto release_poll;
			}
			pthread_mutex_unlock(&exec->mu);
			continue;
		}

		/* W7: process events. */
		if (n > 0) {
			for (int i = 0; i < n; i++) {
				int fd = events[i].data.fd;
				if (fd == r->wake_fd) {
					uint64_t buf;
					while (read(r->wake_fd, &buf, sizeof(buf)) > 0) { }
					continue;
				}
				if (fd == r->signal_fd && r->signal_fd >= 0) {
					DriftVt *waiter = (DriftVt *)atomic_load(&drift_signal_waiter_vt);
					if (!waiter) {
						continue;  /* no waiter — leave signal in kernel buffer */
					}
					struct signalfd_siginfo si;
					ssize_t sn = read(r->signal_fd, &si, sizeof(si));
					if (sn == (ssize_t)sizeof(si)) {
						atomic_store(&drift_signal_delivered_signo, (int)si.ssi_signo);
						atomic_store(&drift_signal_waiter_vt, 0);
						drift_thread_unpark((uint64_t)waiter);
					}
					continue;
				}
				/* T4b: ET per-direction resolution under r->mu. */
				uint32_t ev = events[i].events;
				pthread_mutex_lock(&r->mu);
				ReactorWatch *w = drift_reactor_find_watch(r, fd);
				DriftVt *direct_vt = NULL;
				DriftVt *enqueue_vt = NULL;
				if (w) {
					/* Resolve read direction.  drift_vt_claim_for_* CAS the
					 * PARKED transition so a concurrent timer/cancellation cannot
					 * also claim this VT (Finding 2): if the CAS loses, that other
					 * resumer already owns the wake and we do nothing here.
					 *
					 * FAST-I/O direct-resume: the swapcontext below IS the wake —
					 * no park_token is bumped (an unconsumed token would
					 * short-circuit the VT's next park; see the maria-team
					 * "sleep(550ms) elapsed=0" reduction, doc/history.md
					 * 2026-05-16). */
					if ((ev & (EPOLLIN | EPOLLERR | EPOLLHUP)) && w->read_vt) {
						DriftVt *rv = (DriftVt *)w->read_vt;
						w->read_vt = 0;
						if (drift_vt_claim_for_resume(rv)) {
							ReactorTimer *tp = NULL;
							ReactorTimer *tc = r->timers;
							while (tc) {
								ReactorTimer *tn = tc->next;
								if (tc->vt == (uint64_t)rv) {
									if (tp) tp->next = tn; else r->timers = tn;
									free(tc);
								} else { tp = tc; }
								tc = tn;
							}
							direct_vt = rv;
						}
					} else if (ev & (EPOLLIN | EPOLLERR | EPOLLHUP)) {
						w->pending_read = 1;
					}
					/* Resolve write direction. */
					if ((ev & (EPOLLOUT | EPOLLERR | EPOLLHUP)) && w->write_vt) {
						DriftVt *wv = (DriftVt *)w->write_vt;
						w->write_vt = 0;
						int claimed = 0;
						if (!direct_vt) {
							if (drift_vt_claim_for_resume(wv)) {
								direct_vt = wv;
								claimed = 1;
							}
						} else if (wv != direct_vt) {
							if (drift_vt_claim_for_resume(wv)) {
								enqueue_vt = wv;
								claimed = 1;
							}
						}
						if (claimed) {
							ReactorTimer *tp = NULL;
							ReactorTimer *tc = r->timers;
							while (tc) {
								ReactorTimer *tn = tc->next;
								if (tc->vt == (uint64_t)wv) {
									if (tp) tp->next = tn; else r->timers = tn;
									free(tc);
								} else { tp = tc; }
								tc = tn;
							}
						}
					} else if (ev & (EPOLLOUT | EPOLLERR | EPOLLHUP)) {
						w->pending_write = 1;
					}
				}
				/* Enqueue under r->mu so drift_reactor_forget_vt cannot
				 * free the VT between unlock and enqueue. */
				if (enqueue_vt && enqueue_vt->exec) {
					pthread_mutex_lock(&enqueue_vt->exec->mu);
					drift_exec_enqueue(enqueue_vt->exec, enqueue_vt);
					pthread_mutex_unlock(&enqueue_vt->exec->mu);
				}
				pthread_mutex_unlock(&r->mu);
				if (!direct_vt) continue;
				/* Test-only (round 11): the reactor has now WON the PARKED→READY
				 * claim for direct resume.  Publish that so a racing test can wait
				 * for it before cancelling into the READY→RUNNING window. */
				atomic_fetch_add(&drift_test_direct_resume_claims, 1);
				/* Re-entrancy guard (Finding 1): the same premature-suspension
				 * window as the carrier loop — do not swap into direct_vt until
				 * the carrier that parked it has finished saving its context
				 * (carrier_tid == 0).  No-op when the poller is the very carrier
				 * that parked it. */
				while (atomic_load(&direct_vt->carrier_tid) != 0) {
					sched_yield();
				}
				/* Test-only (round 11): hold the VT in READY here so a concurrent
				 * cancellation must observe READY ("already claimed") and be a
				 * no-op — proving the direct-resume claim wins and the cancel does
				 * not double-enqueue or strand a token. */
				drift_test_direct_resume_pause();
				/* The VT (claimed as READY) has now suspended; flip READY->RUNNING
				 * immediately before resuming it in place.  Round-10 deadlock fix:
				 * claiming RUNNING at claim time would let a racing unpark see RUNNING,
				 * deposit a token the VT consumes instead of suspending, hanging the
				 * carrier_tid guard above. */
				atomic_store(&direct_vt->state, DRIFT_VT_RUNNING);
				/* Resume VT directly. */
				drift_vt_tls_set(direct_vt);
				if (drift_valgrind_mode)
					swapcontext(drift_sched_ctx_uc, &direct_vt->ctx_uc);
				else
					drift_swapcontext(sched_ctx, &direct_vt->ctx);
				drift_vt_tls_set(NULL);
				did_work = 1;
				int st = atomic_load(&direct_vt->state);
				if (st == DRIFT_VT_FINISHED || st == DRIFT_VT_CANCELLED) {
					drift_worker_vt_finish(direct_vt);
				}
			}
		}

		/* W7 timeout path: collect expired timers.
		 * Hold r->mu across dispatch so drift_reactor_forget_vt (called
		 * from drift_vt_destroy on another thread) cannot complete until
		 * we finish — preventing use-after-free on the VT pointer. */
		pthread_mutex_lock(&r->mu);
		ReactorTimer *ready = NULL;
		drift_reactor_collect_timers(r, drift_now_ms(), &ready);
		while (ready) {
			ReactorTimer *next = ready->next;
			if (ready->vt != 0) {
				drift_thread_unpark(ready->vt);
			}
			free(ready);
			ready = next;
		}
		pthread_mutex_unlock(&r->mu);

		/* W8: re-check run queue and shutdown before going back to poll. */
		pthread_mutex_lock(&exec->mu);
		if (exec->head || exec->shutting_down) {
			goto release_poll;
		}
		pthread_mutex_unlock(&exec->mu);
	}

release_poll:
	/* W9: release poll ownership.  Caller holds exec->mu. */
	atomic_store(&r->poll_owner, POLL_OWNER_REACTOR);
	/* Wake reactor so it resumes epoll_wait. */
	pthread_cond_signal(&r->cv);
	return did_work;
}
#endif

static void *drift_exec_worker(void *arg) {
	DriftExec *exec = (DriftExec *)arg;
	drift_exec_tls = exec;
#ifdef __linux__
	DriftContext sched_ctx;
	drift_sched_ctx = &sched_ctx;
	ucontext_t sched_ctx_uc;
	drift_sched_ctx_uc = &sched_ctx_uc;
#endif
	while (1) {
		pthread_mutex_lock(&exec->mu);
#ifdef __linux__
		/* Phase A: if queue is empty and single-worker, claim poll ownership
		 * and call epoll_wait directly.  Re-check head under exec->mu (W1). */
		if (!exec->head && !exec->shutting_down && exec->threads_count == 1) {
			int polled = drift_worker_poll(exec, &sched_ctx);
			/* Returns with exec->mu held. */
			if (polled) {
				/* Handled work; re-check queue at loop top. */
				pthread_mutex_unlock(&exec->mu);
				continue;
			}
			/* drift_worker_poll returned 0 with exec->mu held —
			 * either CAS failed or no events.  Fall through to condvar. */
		}
#endif
		while (!exec->head && !exec->shutting_down) {
			pthread_cond_wait(&exec->cv, &exec->mu);
		}
		if (exec->shutting_down) {
			pthread_mutex_unlock(&exec->mu);
			break;
		}
		ExecNode *node = exec->head;
		DriftVt *vt = NULL;
		if (node) {
			exec->head = node->next;
			if (!exec->head) {
				exec->tail = NULL;
			}
			exec->queue_len--;
			atomic_fetch_add(&exec->running, 1);
			vt = node->vt;
			exec_node_release(exec, node);
		}
		pthread_mutex_unlock(&exec->mu);
		if (!vt) {
			if (node) {
				atomic_fetch_sub(&exec->running, 1);
			}
			continue;
		}
		if (atomic_load(&vt->cancelled) && !atomic_load(&vt->started)) {
			atomic_store(&vt->state, DRIFT_VT_CANCELLED);
			pthread_mutex_lock(&vt->mu);
			if (!atomic_exchange(&vt->completed, 1)) {
				drift_drop_callback(&vt->cb);
			}
			atomic_store(&vt->park_token, 1);
			uint64_t w1 = vt->join_waiter;
			vt->join_waiter = 0;
			pthread_cond_broadcast(&vt->cv);
			pthread_mutex_unlock(&vt->mu);
			if (w1 != 0) drift_thread_unpark(w1);
			atomic_fetch_sub(&exec->running, 1);
			continue;
		}
		/* Use atomic_exchange to distinguish "first pickup" (was_started=0)
		 * from "re-pickup after park" (was_started=1).  On first pickup the
		 * fiber stack is virgin; if cancelled, dropping the cb is safe.  On
		 * re-pickup the fiber has executed user code, may hold owning values
		 * on its stack (e.g., Arc clones moved into a function parameter
		 * from the closure env, see std.concurrent::_keepalive_loop), and
		 * MUST be resumed so its cooperative-cancellation safe points can
		 * unwind those locals.  Dropping the cb here would free the closure
		 * env but the moved-out captures live on the fiber's stack — when
		 * the stack is later torn down by drift_worker_vt_finish those
		 * captures leak.  Symptom (filed 2026-05-22 from bookkeeper
		 * shutdown-hang repro post-fix): registered `Arc<Pooled>` whose
		 * destroy joins a parked VT leaks the `Arc<PoolInner>` clone the
		 * keepalive closure captured. */
		int was_started = atomic_exchange(&vt->started, 1);
		if (atomic_load(&vt->cancelled) && !was_started) {
			atomic_store(&vt->state, DRIFT_VT_CANCELLED);
			pthread_mutex_lock(&vt->mu);
			if (!atomic_exchange(&vt->completed, 1)) {
				drift_drop_callback(&vt->cb);
			}
			atomic_store(&vt->park_token, 1);
			uint64_t w2 = vt->join_waiter;
			vt->join_waiter = 0;
			pthread_cond_broadcast(&vt->cv);
			pthread_mutex_unlock(&vt->mu);
			if (w2 != 0) drift_thread_unpark(w2);
			atomic_fetch_sub(&exec->running, 1);
			continue;
		}
		/* Re-entrancy guard (multi-carrier safety, Finding 1): a parked VT can be
		 * unpark-CAS'd to READY and enqueued while its previous carrier is still
		 * between publishing PARKED and the swapcontext that SAVES its fiber
		 * context.  Running it now — on this carrier — would swap into an unsaved
		 * context while the other carrier still executes the same fiber.  The
		 * previous carrier clears carrier_tid (via drift_vt_tls_set(NULL)) only
		 * AFTER that swapcontext returns, so wait until it is 0 before running.
		 * No-op in the common case (single carrier, or a cleanly-suspended VT). */
		while (atomic_load(&vt->carrier_tid) != 0) {
			sched_yield();
		}
		atomic_store(&vt->state, DRIFT_VT_RUNNING);
#ifdef __linux__
		if (!vt->ctx_ready) {
			if (vt->stack_size == 0)
				vt->stack_size = exec->stack_bytes;
			/* Use mmap with a guard page at the bottom to detect stack overflow.
			 * The guard page (PROT_NONE) will cause SIGSEGV if the fiber stack
			 * overflows, instead of silently corrupting adjacent heap metadata. */
			size_t page_sz = (size_t)sysconf(_SC_PAGESIZE);
			size_t map_sz = vt->stack_size + page_sz;
			void *map = mmap(NULL, map_sz, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
			if (map == MAP_FAILED) {
				vt->stack = malloc(vt->stack_size);
				vt->stack_is_mmap = 0;
			} else if (mprotect(map, page_sz, PROT_NONE) != 0) {
				/* Guard page failed; release the mapping and fall back to malloc. */
				munmap(map, map_sz);
				vt->stack = malloc(vt->stack_size);
				vt->stack_is_mmap = 0;
			} else {
				vt->stack = (char *)map + page_sz;
				vt->stack_is_mmap = 1;
			}
			if (vt->stack) {
				vt->valgrind_stack_id = VALGRIND_STACK_REGISTER(
					vt->stack, (char *)vt->stack + vt->stack_size);
			}
			vt->ctx_ready = 1;
			if (drift_valgrind_mode) {
				getcontext(&vt->ctx_uc);
				vt->ctx_uc.uc_link = NULL;  /* fiber_entry swaps back via TLS */
				vt->ctx_uc.uc_stack.ss_sp = vt->stack;
				vt->ctx_uc.uc_stack.ss_size = vt->stack_size;
				makecontext(&vt->ctx_uc, (void (*)())drift_vt_fiber_entry, 1, (uintptr_t)vt);
			} else {
				drift_makecontext(&vt->ctx, (char *)vt->stack + vt->stack_size, drift_vt_fiber_entry, (uintptr_t)vt);
			}
		}
		drift_vt_tls_set(vt);
		if (drift_valgrind_mode)
			swapcontext(&sched_ctx_uc, &vt->ctx_uc);
		else
			drift_swapcontext(&sched_ctx, &vt->ctx);
		drift_vt_tls_set(NULL);
		int state = atomic_load(&vt->state);
		if (state == DRIFT_VT_FINISHED || state == DRIFT_VT_CANCELLED) {
			drift_worker_vt_finish(vt);
		}
#else
		drift_vt_tls_set(vt);
		drift_run_callback(&vt->cb, 0);
		atomic_store(&vt->state, DRIFT_VT_FINISHED);
			pthread_mutex_lock(&vt->mu);
			atomic_store(&vt->completed, 1);
			int dropped_after_finish = atomic_load(&vt->dropped);
			atomic_store(&vt->park_token, 1);
			pthread_cond_broadcast(&vt->cv);
			pthread_mutex_unlock(&vt->mu);
			if (dropped_after_finish) {
				drift_vt_destroy(vt);
			}
			drift_vt_tls_set(NULL);
#endif
		atomic_fetch_sub(&exec->running, 1);
	}
#ifdef __linux__
	drift_sched_ctx = NULL;
#endif
	return NULL;
}

static void drift_reactor_wake(Reactor *r) {
#ifdef __linux__
	if (!r || r->wake_fd < 0) {
		return;
	}
	if (!atomic_exchange_explicit(&r->in_wait, 0, memory_order_relaxed)) {
		return;
	}
	uint64_t one = 1;
	(void)write(r->wake_fd, &one, sizeof(one));
#else
	(void)r;
#endif
}

static ReactorWatch *drift_reactor_find_watch(Reactor *r, int fd) {
	ReactorWatch *cur = r->watches;
	while (cur) {
		if (cur->fd == fd) {
			return cur;
		}
		cur = cur->next;
	}
	return NULL;
}

static ReactorWatch *drift_reactor_take_watch(Reactor *r, int fd) {
	ReactorWatch *prev = NULL;
	ReactorWatch *cur = r->watches;
	while (cur) {
		if (cur->fd == fd) {
			if (prev) {
				prev->next = cur->next;
			} else {
				r->watches = cur->next;
			}
			cur->next = NULL;
			return cur;
		}
		prev = cur;
		cur = cur->next;
	}
	return NULL;
}

static void drift_reactor_add_timer(Reactor *r, int64_t deadline_ms, uint64_t vt) {
	ReactorTimer *t = (ReactorTimer *)malloc(sizeof(ReactorTimer));
	if (!t) {
		return;
	}
	t->deadline_ms = deadline_ms;
	t->vt = vt;
	t->next = r->timers;
	r->timers = t;
}

static void drift_reactor_collect_timers(Reactor *r, int64_t now_ms, ReactorTimer **out) {
	ReactorTimer *prev = NULL;
	ReactorTimer *cur = r->timers;
	ReactorTimer *ready = NULL;
	while (cur) {
		if (cur->deadline_ms <= now_ms) {
			ReactorTimer *next = cur->next;
			if (prev) {
				prev->next = next;
			} else {
				r->timers = next;
			}
			cur->next = ready;
			ready = cur;
			cur = next;
			continue;
		}
		prev = cur;
		cur = cur->next;
	}
	*out = ready;
}

/* Forward declaration: drift_exec_enqueue is defined after drift_vt_fiber_entry
 * but used in the reactor I/O path (T4a). */
static void drift_exec_enqueue(DriftExec *exec, DriftVt *vt);

#ifdef __linux__
static void *drift_reactor_thread_entry(void *arg) {
	Reactor *r = (Reactor *)arg;
	struct epoll_event events[16];
	while (1) {
		/* R1: compute timeout from timer list. */
		int timeout_ms = -1;
		pthread_mutex_lock(&r->mu);
		if (r->stopping) {
			pthread_mutex_unlock(&r->mu);
			break;
		}
		if (r->timers) {
			int64_t now_ms = drift_now_ms();
			int64_t min_deadline = r->timers->deadline_ms;
			for (ReactorTimer *t = r->timers; t; t = t->next) {
				if (t->deadline_ms < min_deadline) {
					min_deadline = t->deadline_ms;
				}
			}
			int64_t delta = min_deadline - now_ms;
			if (delta <= 0) {
				timeout_ms = 0;
			} else if (delta > INT32_MAX) {
				timeout_ms = INT32_MAX;
			} else {
				timeout_ms = (int)delta;
			}
		}

		/* R2: check poll_owner.  If worker owns epoll, yield to condvar. */
		int n = 0;
		if (atomic_load(&r->poll_owner) == POLL_OWNER_WORKER) {
			/* Worker owns epoll_wait.  Wait on condvar for either:
			 * - timer deadline (timedwait)
			 * - worker releases poll (signals r->cv)
			 * - stopping (checked at loop top) */
			if (timeout_ms < 0) {
				/* No timers: sleep up to 1s, re-check poll_owner. */
				struct timespec ts;
				clock_gettime(CLOCK_REALTIME, &ts);
				ts.tv_sec += 1;
				pthread_cond_timedwait(&r->cv, &r->mu, &ts);
			} else if (timeout_ms > 0) {
				struct timespec ts;
				clock_gettime(CLOCK_REALTIME, &ts);
				ts.tv_sec += timeout_ms / 1000;
				ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
				if (ts.tv_nsec >= 1000000000L) {
					ts.tv_sec += 1;
					ts.tv_nsec -= 1000000000L;
				}
				pthread_cond_timedwait(&r->cv, &r->mu, &ts);
			}
			/* timeout_ms == 0: don't wait, just process timers immediately. */
			pthread_mutex_unlock(&r->mu);
		} else {
			/* R3: reactor owns epoll_wait — existing behavior.
			 * Set in_wait BEFORE releasing mu so any concurrent
			 * drift_reactor_wake (e.g. from timer registration)
			 * sees in_wait=1 and writes to wake_fd. */
			atomic_store_explicit(&r->in_wait, 1, memory_order_release);
			pthread_mutex_unlock(&r->mu);
			n = epoll_wait(r->epoll_fd, events, 16, timeout_ms);
			/* Only clear in_wait if the worker hasn't claimed poll
			 * ownership while we were in epoll_wait.  If poll_owner
			 * is now WORKER, in_wait belongs to the worker (set at
			 * W3/W5) — clearing it would lose wake_fd wakes. */
			if (atomic_load(&r->poll_owner) == POLL_OWNER_REACTOR)
				atomic_store_explicit(&r->in_wait, 0, memory_order_relaxed);
		}
		if (n < 0 && errno != EINTR) {
			continue;
		}
		if (n > 0) {
			for (int i = 0; i < n; i++) {
				int fd = events[i].data.fd;
				if (fd == r->wake_fd) {
					uint64_t buf;
					while (read(r->wake_fd, &buf, sizeof(buf)) > 0) { }
					/* If the worker owns poll, this wake was likely
					 * intended for the worker — re-signal so the
					 * worker's epoll_wait sees it.  The reactor will
					 * exit to R2 condvar on the next loop iteration,
					 * ending the brief overlap period. */
					if (atomic_load(&r->poll_owner) == POLL_OWNER_WORKER) {
						uint64_t one = 1;
						(void)write(r->wake_fd, &one, sizeof(one));
					}
					continue;
				}
				if (fd == r->signal_fd && r->signal_fd >= 0) {
					DriftVt *waiter = (DriftVt *)atomic_load(&drift_signal_waiter_vt);
					if (!waiter) {
						continue;
					}
					struct signalfd_siginfo si;
					ssize_t sn = read(r->signal_fd, &si, sizeof(si));
					if (sn == (ssize_t)sizeof(si)) {
						atomic_store(&drift_signal_delivered_signo, (int)si.ssi_signo);
						atomic_store(&drift_signal_waiter_vt, 0);
						drift_thread_unpark((uint64_t)waiter);
					}
					continue;
				}
				/* T4a: ET per-direction resolution under r->mu. */
				uint32_t ev = events[i].events;
				pthread_mutex_lock(&r->mu);
				ReactorWatch *w = drift_reactor_find_watch(r, fd);
				DriftVt *read_io_vt = NULL;
				DriftVt *write_io_vt = NULL;
				if (w) {
					/* Resolve read direction. */
					if ((ev & (EPOLLIN | EPOLLERR | EPOLLHUP)) && w->read_vt) {
						DriftVt *rv = (DriftVt *)w->read_vt;
						w->read_vt = 0;
						uint64_t rv_id = (uint64_t)rv;
						ReactorTimer *tp = NULL;
						ReactorTimer *tc = r->timers;
						while (tc) {
							ReactorTimer *tn = tc->next;
							if (tc->vt == rv_id) {
								if (tp) tp->next = tn; else r->timers = tn;
								free(tc);
							} else { tp = tc; }
							tc = tn;
						}
						/* CAS-claim PARKED->READY (Finding 2): if a timer/cancel
						 * already claimed it, the CAS loses and we skip — no
						 * duplicate enqueue. */
						if (drift_vt_claim_for_resume(rv)) {
							read_io_vt = rv;
						}
					} else if (ev & (EPOLLIN | EPOLLERR | EPOLLHUP)) {
						w->pending_read = 1;
					}
					/* Resolve write direction. */
					if ((ev & (EPOLLOUT | EPOLLERR | EPOLLHUP)) && w->write_vt) {
						DriftVt *wv = (DriftVt *)w->write_vt;
						w->write_vt = 0;
						uint64_t wv_id = (uint64_t)wv;
						ReactorTimer *tp = NULL;
						ReactorTimer *tc = r->timers;
						while (tc) {
							ReactorTimer *tn = tc->next;
							if (tc->vt == wv_id) {
								if (tp) tp->next = tn; else r->timers = tn;
								free(tc);
							} else { tp = tc; }
							tc = tn;
						}
						if (drift_vt_claim_for_resume(wv)) {
							write_io_vt = wv;
						}
					} else if (ev & (EPOLLOUT | EPOLLERR | EPOLLHUP)) {
						w->pending_write = 1;
					}
				}
				/* Enqueue resolved VTs under r->mu so drift_reactor_forget_vt
				 * (called from drift_vt_destroy on another thread) cannot
				 * complete until we finish — preventing use-after-free on
				 * the VT pointer between unlock and enqueue. */
				if (read_io_vt && read_io_vt->exec) {
					pthread_mutex_lock(&read_io_vt->exec->mu);
					drift_exec_enqueue(read_io_vt->exec, read_io_vt);
					pthread_mutex_unlock(&read_io_vt->exec->mu);
				}
				if (write_io_vt && write_io_vt->exec && write_io_vt != read_io_vt) {
					pthread_mutex_lock(&write_io_vt->exec->mu);
					drift_exec_enqueue(write_io_vt->exec, write_io_vt);
					pthread_mutex_unlock(&write_io_vt->exec->mu);
				}
				pthread_mutex_unlock(&r->mu);
				/* Wake after releasing r->mu to avoid holding both locks. */
				if (read_io_vt || write_io_vt) {
					drift_reactor_wake(r);
				}
			}
		}

		/* Hold r->mu across dispatch so drift_reactor_forget_vt (called
		 * from drift_vt_destroy on the worker thread) cannot complete
		 * until we finish — preventing use-after-free on the VT. */
		pthread_mutex_lock(&r->mu);
		ReactorTimer *ready = NULL;
		drift_reactor_collect_timers(r, drift_now_ms(), &ready);
		while (ready) {
			ReactorTimer *next = ready->next;
			if (ready->vt != 0) {
				drift_thread_unpark(ready->vt);
			}
			free(ready);
			ready = next;
		}
		pthread_mutex_unlock(&r->mu);
	}
	return NULL;
}
#endif

static Reactor *drift_reactor_create(void) {
	Reactor *r = (Reactor *)calloc(1, sizeof(Reactor));
	if (!r) {
		return NULL;
	}
	r->epoll_fd = -1;
	r->wake_fd = -1;
	r->signal_fd = -1;
	pthread_mutex_init(&r->mu, NULL);
	pthread_cond_init(&r->cv, NULL);
#ifdef __linux__
	r->epoll_fd = epoll_create1(0);
	r->wake_fd = eventfd(0, EFD_NONBLOCK);
	if (r->epoll_fd >= 0 && r->wake_fd >= 0) {
		struct epoll_event ev;
		ev.events = EPOLLIN;
		ev.data.fd = r->wake_fd;
		epoll_ctl(r->epoll_fd, EPOLL_CTL_ADD, r->wake_fd, &ev);
	}
	/* Register the process-global signalfd on this reactor's epoll set.
	 * The reactor only reads from signalfd when a waiter is registered;
	 * otherwise the signal stays queued in the kernel buffer. */
	if (r->epoll_fd >= 0 && drift_signal_fd >= 0) {
		r->signal_fd = drift_signal_fd;
		struct epoll_event sev;
		sev.events = EPOLLIN;
		sev.data.fd = drift_signal_fd;
		epoll_ctl(r->epoll_fd, EPOLL_CTL_ADD, drift_signal_fd, &sev);
	}
	/* Test-only (round 11): suppress the dedicated reactor thread so a
	 * single-worker executor's worker is forced to be the sole reactor poller.
	 * This makes the worker-inline direct-resume claim path (drift_worker_poll)
	 * deterministically reachable for an fd event — otherwise the always-on
	 * reactor thread races and usually services the event via the QUEUED claim
	 * path instead, leaving the direct-resume window untested.  Only safe for
	 * programs whose IO runs on single-worker executors (the worker polls). */
	{
		const char *no_rt = getenv("DRIFT_TEST_NO_REACTOR_THREAD");
		if (!(no_rt && no_rt[0] == '1')) {
			if (pthread_create(&r->thread, NULL, drift_reactor_thread_entry, r) == 0) {
				r->thread_started = 1;
			}
		}
	}
#endif
	return r;
}

static void drift_reactor_destroy(Reactor *r) {
	if (!r) {
		return;
	}
	pthread_mutex_lock(&r->mu);
	r->stopping = 1;
	pthread_mutex_unlock(&r->mu);
	/* Force-write to wake_fd unconditionally.  drift_reactor_wake is
	 * guarded by in_wait which may be 0 if the reactor is between its
	 * stopping check and epoll_wait entry.  The eventfd counter is
	 * persistent, so even if the reactor hasn't entered epoll_wait yet,
	 * the write ensures the subsequent epoll_wait returns immediately. */
#ifdef __linux__
	if (r->wake_fd >= 0) {
		uint64_t one = 1;
		(void)write(r->wake_fd, &one, sizeof(one));
	}
#endif
	/* Also signal condvar in case the reactor is in R2 (poll_owner=WORKER). */
	pthread_cond_signal(&r->cv);
#ifdef __linux__
	if (r->thread_started) {
		pthread_join(r->thread, NULL);
	}
	if (r->wake_fd >= 0) {
		close(r->wake_fd);
		r->wake_fd = -1;
	}
	if (r->epoll_fd >= 0) {
		close(r->epoll_fd);
		r->epoll_fd = -1;
	}
	/* Close signalfd at shutdown.  Signal mask stays permanently blocked —
	 * no attempt to restore, the process is exiting. */
	if (drift_signal_fd >= 0) {
		close(drift_signal_fd);
		drift_signal_fd = -1;
		r->signal_fd = -1;
	}
#endif
	ReactorTimer *t = r->timers;
	while (t) {
		ReactorTimer *next = t->next;
		free(t);
		t = next;
	}
	ReactorWatch *w = r->watches;
	while (w) {
		ReactorWatch *next = w->next;
		free(w);
		w = next;
	}
	pthread_cond_destroy(&r->cv);
	pthread_mutex_destroy(&r->mu);
	free(r);
}

static void drift_reactor_shutdown_default_atexit(void) {
	/* Reactor users run on executor workers; ensure workers are stopped before
	 * reactor mutex/fds are torn down at process exit. */
	drift_exec_shutdown_all_atexit();
	uint64_t raw = atomic_exchange(&drift_default_reactor, 0);
	Reactor *r = (Reactor *)raw;
	drift_default_reactor_ptr = NULL;
	if (r) {
		drift_reactor_destroy(r);
	}
}

static void drift_reactor_init_once(void) {
	drift_default_reactor_ptr = drift_reactor_create();
	if (drift_default_reactor_ptr) {
		atomic_store(&drift_default_reactor, (uint64_t)drift_default_reactor_ptr);
		pthread_once(&drift_reactor_shutdown_once, drift_reactor_register_shutdown_once);
	}
}

static DriftExec *drift_exec_create_internal(int64_t min_threads, int64_t max_threads, int64_t queue_limit, int64_t stack_bytes) {
	(void)min_threads;
	/* Detect Valgrind once before spawning worker threads. */
	static int drift_valgrind_checked = 0;
	if (!drift_valgrind_checked) {
		drift_valgrind_mode = RUNNING_ON_VALGRIND ? 1 : 0;
		drift_valgrind_checked = 1;
	}
	if (max_threads <= 0) {
		max_threads = 1;
	}
	DriftExec *exec = (DriftExec *)calloc(1, sizeof(DriftExec));
	if (!exec) {
		return NULL;
	}
	pthread_mutex_init(&exec->mu, NULL);
	pthread_cond_init(&exec->cv, NULL);
	exec->queue_limit = queue_limit;
	atomic_store(&exec->running, 0);
	if (stack_bytes <= 0) {
		stack_bytes = 262144;
	}
	exec->stack_bytes = (size_t)stack_bytes;
	exec->destroyed = 0;
	exec->reg_prev = NULL;
	exec->reg_next = NULL;
	exec->threads_count = (int)max_threads;
	exec->threads = (pthread_t *)calloc((size_t)exec->threads_count, sizeof(pthread_t));
	if (!exec->threads) {
		free(exec);
		return NULL;
	}
	for (int i = 0; i < exec->threads_count; i++) {
		pthread_create(&exec->threads[i], NULL, drift_exec_worker, exec);
	}
	drift_exec_registry_add(exec);
	return exec;
}

static void drift_exec_destroy_internal(DriftExec *exec) {
	if (!exec) {
		return;
	}
	pthread_mutex_lock(&drift_exec_registry_mu);
	if (exec->destroyed) {
		pthread_mutex_unlock(&drift_exec_registry_mu);
		return;
	}
	exec->destroyed = 1;
	if (exec->reg_prev) {
		exec->reg_prev->reg_next = exec->reg_next;
	} else if (drift_exec_registry_head == exec) {
		drift_exec_registry_head = exec->reg_next;
	}
	if (exec->reg_next) {
		exec->reg_next->reg_prev = exec->reg_prev;
	}
	exec->reg_prev = NULL;
	exec->reg_next = NULL;
	pthread_mutex_unlock(&drift_exec_registry_mu);
	pthread_mutex_lock(&exec->mu);
	exec->shutting_down = 1;
	pthread_cond_broadcast(&exec->cv);
	pthread_mutex_unlock(&exec->mu);
	/* Wake worker if it is in poll mode (epoll_wait). */
	Reactor *r = drift_default_reactor_ptr;
	if (r) drift_reactor_wake(r);
	for (int i = 0; i < exec->threads_count; i++) {
		pthread_join(exec->threads[i], NULL);
	}
	pthread_mutex_lock(&exec->mu);
	ExecNode *node = exec->head;
	exec->head = NULL;
	exec->tail = NULL;
	exec->queue_len = 0;
	pthread_mutex_unlock(&exec->mu);
	while (node) {
		ExecNode *next = node->next;
		DriftVt *vt = node->vt;
		if (vt) {
			if (!atomic_load(&vt->started)) {
				atomic_store(&vt->state, DRIFT_VT_CANCELLED);
				pthread_mutex_lock(&vt->mu);
				if (!atomic_exchange(&vt->completed, 1)) {
					drift_drop_callback(&vt->cb);
				}
				atomic_store(&vt->park_token, 1);
				pthread_cond_broadcast(&vt->cv);
				pthread_mutex_unlock(&vt->mu);
			}
			/* Ownership of VT allocations is centralized in the global VT registry.
			 * Executor queue drain only finalizes queued work state/callback drop;
			 * final memory reclamation is handled by join/drop paths or registry
			 * cleanup to avoid duplicate free on stale/duplicate queue references. */
			vt->exec = NULL;
		}
		free(node);
		node = next;
	}
	/* Drain the ExecNode freelist. */
	ExecNode *fl = exec->node_freelist;
	exec->node_freelist = NULL;
	exec->node_freelist_len = 0;
	while (fl) {
		ExecNode *fn = fl->next;
		free(fl);
		fl = fn;
	}
	pthread_cond_destroy(&exec->cv);
	pthread_mutex_destroy(&exec->mu);
	free(exec->threads);
	free(exec);
}

static void drift_exec_shutdown_all_atexit(void) {
	/* Prevent the default-executor atexit hook from dereferencing stale
	 * pointers after global executor teardown runs first. */
	atomic_store(&drift_default_executor, 0);
	while (1) {
		pthread_mutex_lock(&drift_exec_registry_mu);
		DriftExec *exec = drift_exec_registry_head;
		pthread_mutex_unlock(&drift_exec_registry_mu);
		if (!exec) {
			break;
		}
		drift_exec_destroy_internal(exec);
	}
}

static void drift_exec_register_shutdown_once(void) {
	(void)atexit(drift_exec_shutdown_all_atexit);
}

static void drift_exec_shutdown_default_atexit(void) {
	uint64_t raw = atomic_exchange(&drift_default_executor, 0);
	DriftExec *exec = (DriftExec *)raw;
	if (exec) {
		drift_exec_destroy_internal(exec);
	}
}

static void drift_exec_init_once(void) {
	pthread_once(&drift_exec_cleanup_once, drift_exec_register_shutdown_once);
	DriftExec *exec = drift_exec_create_internal(1, 1, 0, 262144);
	if (exec) {
		atomic_store(&drift_default_executor, (uint64_t)exec);
		(void)atexit(drift_exec_shutdown_default_atexit);
	}
}

static void drift_run_callback(DriftIface *cb, int do_free) {
	void *data = cb->data;
	if ((cb->is_inline & 1) != 0) {
		data = (void *)cb->inline_words;
	}
	DriftCallbackVTable *vt = (DriftCallbackVTable *)cb->vtable;
	if (vt && vt->call) {
		((DriftCallback0)vt->call)(data);
	}
	if (vt && vt->drop) {
		((DriftCallbackDrop)vt->drop)(data);
	}
	if (do_free) {
		free(cb);
	}
}

static void drift_drop_callback(DriftIface *cb) {
	void *data = cb->data;
	if ((cb->is_inline & 1) != 0) {
		data = (void *)cb->inline_words;
	}
	DriftCallbackVTable *vt = (DriftCallbackVTable *)cb->vtable;
	if (vt && vt->drop) {
		((DriftCallbackDrop)vt->drop)(data);
	}
}

#ifdef __linux__
static void drift_vt_fiber_entry(uintptr_t arg) {
	DriftVt *vt = (DriftVt *)arg;
	if (!vt) {
		return;
	}
	drift_vt_tls_set(vt);
	atomic_store(&vt->state, DRIFT_VT_RUNNING);
	drift_run_callback(&vt->cb, 0);
	atomic_store(&vt->state, DRIFT_VT_FINISHED);
	drift_vt_tls_set(NULL);
	/* Swap back to the *current* worker's scheduler context via TLS.
	 * Must be explicit on both paths — uc_link targets the initializing
	 * worker, which may differ from the worker executing this VT now. */
	if (drift_valgrind_mode) {
		if (drift_sched_ctx_uc)
			swapcontext(&vt->ctx_uc, drift_sched_ctx_uc);
	} else {
		if (drift_sched_ctx)
			drift_swapcontext(&vt->ctx, drift_sched_ctx);
	}
}
#endif

static void drift_exec_enqueue(DriftExec *exec, DriftVt *vt) {
	ExecNode *node = exec_node_alloc(exec);
	if (!node) {
		return;
	}
	node->vt = vt;
	node->next = NULL;
	if (exec->tail) {
		exec->tail->next = node;
	} else {
		exec->head = node;
	}
	exec->tail = node;
	exec->queue_len++;
	pthread_cond_signal(&exec->cv);
}

static int drift_exec_remove_vt_locked(DriftExec *exec, DriftVt *vt) {
	if (!exec || !vt) {
		return 0;
	}
	ExecNode *prev = NULL;
	ExecNode *cur = exec->head;
	while (cur) {
		if (cur->vt == vt) {
			ExecNode *next = cur->next;
			if (prev) {
				prev->next = next;
			} else {
				exec->head = next;
			}
			if (exec->tail == cur) {
				exec->tail = prev;
			}
			if (exec->queue_len > 0) {
				exec->queue_len--;
			}
			exec_node_release(exec, cur);
			return 1;
		}
		prev = cur;
		cur = cur->next;
	}
	return 0;
}

static void *drift_thread_entry(void *arg) {
	DriftVt *vt = (DriftVt *)arg;
	drift_vt_tls_set(vt);
	atomic_store(&vt->state, DRIFT_VT_RUNNING);
	drift_run_callback(&vt->cb, 0);
	atomic_store(&vt->state, DRIFT_VT_FINISHED);
	atomic_store(&vt->completed, 1);
	return NULL;
}

uint64_t drift_thread_spawn(DriftIface *cb_ptr, uint64_t exec) {
	(void)exec;
	if (!cb_ptr) {
		return 0;
	}
	DriftIface cb = *cb_ptr;
	DriftVt *vt = (DriftVt *)malloc(sizeof(DriftVt));
	if (!vt) {
		drift_run_callback(&cb, 0);
		return 0;
	}
	vt->cb = cb;
	atomic_store(&vt->started, 0);
	atomic_store(&vt->completed, 0);
	atomic_store(&vt->cancelled, 0);
	atomic_store(&vt->dropped, 0);
	atomic_store(&vt->state, DRIFT_VT_NEW);
	vt->stack = NULL;
	vt->stack_size = 0;
	vt->stack_is_mmap = 0;
#ifdef __linux__
	vt->ctx_ready = 0;
#endif
	vt->exec = NULL;
	vt->thread = (pthread_t)0;
	pthread_mutex_init(&vt->mu, NULL);
	pthread_cond_init(&vt->cv, NULL);
	vt->park_token = 0;
	vt->reg_prev = NULL;
	vt->reg_next = NULL;
	vt->thread_registry_head = NULL;
	vt->io_bytes_since_yield = 0;
	vt->join_waiter = 0;
	vt->vtid = atomic_fetch_add(&drift_vtid_counter, 1);
	lv_set_i(&vt->wait_kind, DRIFT_WAIT_NONE);
	lv_set_u(&vt->wait_id, 0);
	lv_set_t(&vt->state_since_ms, drift_now_ms());
	lv_set_u(&vt->carrier_tid, 0);
	lv_set_u(&vt->last_progress, 0);
	drift_vt_registry_add(vt);
	return (uint64_t)vt;
}

void drift_thread_join(uint64_t vt) {
	DriftVt *h = (DriftVt *)vt;
	if (h == NULL) {
		return;
	}

	/* Fast path: already completed. */
	if (atomic_load(&h->completed)) {
		drift_vt_destroy(h);
		return;
	}

	DriftVt *caller = drift_vt_tls_get();
	if (caller && drift_sched_ctx) {
		/* VT-aware cooperative join: park this VT, let worker run child.
		 * Loop because the waiter may be resumed for reasons other than
		 * target completion (e.g., spurious unpark).  But if the *caller*
		 * is cancelled, drift_thread_park returns immediately without
		 * blocking (the cancelled check at its top), so we must escape
		 * to avoid a livelock. */
		for (;;) {
			pthread_mutex_lock(&h->mu);
			if (atomic_load(&h->completed)) {
				pthread_mutex_unlock(&h->mu);
				drift_vt_destroy(h);
				return;
			}
			h->join_waiter = (uint64_t)caller;
			pthread_mutex_unlock(&h->mu);

			/* Liveness: label this park as a join on the target's id. */
			lv_set_i(&caller->wait_kind, DRIFT_WAIT_JOIN);
			lv_set_u(&caller->wait_id, h->vtid);
			drift_thread_park(0);  /* context-swap to scheduler */

			if (atomic_load(&h->completed)) {
				drift_vt_destroy(h);
				return;
			}
			/* Caller cancelled — abandon the join.  Clear waiter so the
			 * child's finish path doesn't unpark a stale handle.  The
			 * child VT is not destroyed; it will be reclaimed by the
			 * VT registry cleanup at shutdown. */
			if (atomic_load(&caller->cancelled)) {
				pthread_mutex_lock(&h->mu);
				if (h->join_waiter == (uint64_t)caller)
					h->join_waiter = 0;
				pthread_mutex_unlock(&h->mu);
				return;
			}
			/* Spurious wake — re-register and park again. */
		}
	}

	/* Non-VT path: hard condvar wait (OS main thread, blocking pool, etc). */
	pthread_mutex_lock(&h->mu);
	while (!atomic_load(&h->completed)) {
		pthread_cond_wait(&h->cv, &h->mu);
	}
	pthread_mutex_unlock(&h->mu);
	drift_vt_destroy(h);
}

uint64_t drift_thread_is_completed(uint64_t vt) {
	DriftVt *h = (DriftVt *)vt;
	if (h == NULL) {
		return 0;
	}
	return atomic_load(&h->completed) ? 1 : 0;
}

uint64_t drift_thread_join_timeout(uint64_t vt, int64_t timeout_ms) {
	DriftVt *h = (DriftVt *)vt;
	if (h == NULL) {
		return 0;
	}
	if (timeout_ms <= 0) {
		return 1;
	}
	if (atomic_load(&h->completed)) {
		drift_vt_destroy(h);
		return 0;
	}
	if (atomic_load(&h->cancelled)) {
		return 1;
	}

	DriftVt *caller = drift_vt_tls_get();
	if (caller && drift_sched_ctx) {
		/* VT-aware cooperative join with timeout. */
		pthread_mutex_lock(&h->mu);
		if (atomic_load(&h->completed)) {
			pthread_mutex_unlock(&h->mu);
			drift_vt_destroy(h);
			return 0;
		}
		h->join_waiter = (uint64_t)caller;
		pthread_mutex_unlock(&h->mu);

		int64_t deadline = drift_now_ms() + timeout_ms;
		drift_reactor_register_timer((uint64_t)deadline, (uint64_t)caller);
		drift_thread_park(0);

		/* Resumed: check outcome. */
		if (atomic_load(&h->completed)) {
			drift_reactor_cancel_vt_timers((uint64_t)caller);
			drift_vt_destroy(h);
			return 0;  /* joined successfully */
		}
		/* Timeout: clear waiter, don't destroy. */
		pthread_mutex_lock(&h->mu);
		h->join_waiter = 0;
		pthread_mutex_unlock(&h->mu);
		return 1;  /* timeout */
	}

	/* Non-VT path: existing condvar timedwait. */
	struct timespec ts;
	if (clock_gettime(CLOCK_REALTIME, &ts) != 0) {
		return 1;
	}
	time_t add_sec = (time_t)(timeout_ms / 1000);
	long add_nsec = (long)((timeout_ms % 1000) * 1000000L);
	ts.tv_sec += add_sec;
	ts.tv_nsec += add_nsec;
	if (ts.tv_nsec >= 1000000000L) {
		ts.tv_sec += 1;
		ts.tv_nsec -= 1000000000L;
	}
	pthread_mutex_lock(&h->mu);
	while (!atomic_load(&h->completed)) {
		int rc = pthread_cond_timedwait(&h->cv, &h->mu, &ts);
		if (rc == ETIMEDOUT) {
			pthread_mutex_unlock(&h->mu);
			return 1;
		}
	}
	pthread_mutex_unlock(&h->mu);
	drift_vt_destroy(h);
	return 0;
}

uint64_t drift_thread_current(void) {
	DriftVt *vt = drift_vt_tls_get();
	if (vt) {
		return (uint64_t)vt;
	}
	return 0;
}

/* Return 1 if the current VT has been cancelled, 0 otherwise (including
 * the case where there is no current VT — off-VT callers see 0).
 *
 * Used by std.concurrent.Condvar (and other stdlib parking primitives)
 * to surface CANCELLED as a typed error after vt_park returns: the
 * runtime's cancel path sets vt->cancelled before bumping park_token,
 * so a parked VT that wakes via cancellation can read this flag after
 * its park returns and route the appropriate error. */
int64_t drift_thread_is_cancelled(void) {
	DriftVt *vt = drift_vt_tls_get();
	if (!vt) return 0;
	return atomic_load(&vt->cancelled) ? 1 : 0;
}

/* Return the stable VT id for the current virtual thread, or 0 if
 * not running on a VT. */
uint64_t drift_thread_vtid(void) {
	DriftVt *vt = drift_vt_tls_get();
	if (vt) {
		return vt->vtid;
	}
	return 0;
}

/* Current VT *handle* (the pointer-as-uint64 that park/unpark/timer use),
 * 0 if off-VT.  Distinct from drift_thread_vtid (the logical id); the
 * blocking-pool consumers need this handle for drift_thread_unpark and the
 * reactor timer registration.  Exposed via posix/blocking_pool.h. */
uint64_t drift_thread_current_vt_handle(void) {
	return (uint64_t)drift_vt_tls_get();
}

/* Return the OS kernel thread id (gettid()) of the calling carrier thread —
 * the same value liveness reports as carrier_tid, and the id that
 * top/ps/proc/perf/strace use.  This is what std.log emits as `tid`. */
int64_t drift_thread_tid(void) {
	return (int64_t)drift_self_tid();
}

/* Liveness annotation hook, called by stdlib sync primitives immediately
 * before they park the current VT (condvar, channel, sleep).  Records the
 * wait kind + an opaque wait-object id so the liveness dump can report what a
 * parked VT is waiting on.  No-op off a VT.  Cleared automatically on resume
 * (drift_vt_tls_set / non-VT wake path).  Additive runtime/stdlib ABI symbol. */
void drift_thread_set_wait(uint64_t kind, uint64_t id) {
	DriftVt *vt = drift_vt_tls_get();
	if (!vt) {
		return;
	}
	lv_set_i(&vt->wait_kind, (int)kind);
	lv_set_u(&vt->wait_id, id);
}

/* Clear best-effort wait metadata and re-stamp running-since.  Call on every
 * resume / fast-return / early-return path that leaves a VT RUNNING without
 * going through drift_vt_tls_set (park_token fast paths, cancelled returns,
 * invalid-deadline returns).  Without this, wait_kind/wait_id can stay JOIN /
 * TIMER / CONDVAR while the VT is actually running, so the liveness dump would
 * misreport what a running VT is waiting on. */
static inline void drift_vt_resume_clear(DriftVt *vt) {
	lv_set_i(&vt->wait_kind, DRIFT_WAIT_NONE);
	lv_set_u(&vt->wait_id, 0);
	lv_set_t(&vt->state_since_ms, drift_now_ms());
}

/* Test-only: busy-spin (DRIFT_TEST_PARK_PAUSE_US microseconds) immediately after
 * a fiber publishes DRIFT_VT_PARKED but BEFORE the swapcontext that saves its
 * context — deterministically widening the premature-suspension window so a
 * concurrent unpark+other-carrier reliably exercises the re-entrancy guard
 * (Finding 1).  The cache is initialized once via pthread_once (no data race
 * across carriers) so the common (env-unset) path is a plain read. */
static int drift_test_park_pause_us = 0;
static pthread_once_t drift_test_park_pause_once = PTHREAD_ONCE_INIT;
static void drift_test_park_pause_init(void) {
	const char *e = getenv("DRIFT_TEST_PARK_PAUSE_US");
	drift_test_park_pause_us = (e && *e) ? atoi(e) : 0;
}
static void drift_test_park_pause(void) {
	pthread_once(&drift_test_park_pause_once, drift_test_park_pause_init);
	int us = drift_test_park_pause_us;
	if (us > 0) {
		struct timespec t0, now;
		clock_gettime(CLOCK_MONOTONIC, &t0);
		for (;;) {
			clock_gettime(CLOCK_MONOTONIC, &now);
			long elapsed = (now.tv_sec - t0.tv_sec) * 1000000L +
			               (now.tv_nsec - t0.tv_nsec) / 1000L;
			if (elapsed >= us) break;
		}
	}
}

void drift_thread_park(uint64_t reason) {
	(void)reason;
	DriftVt *vt = drift_vt_tls_get();
	if (!vt) {
		sched_yield();
		return;
	}
	if (atomic_load(&vt->cancelled)) {
		drift_vt_resume_clear(vt);
		return;
	}
#ifdef __linux__
	if (drift_sched_ctx) {
		/* Fast path: a wake is already latched. */
		if (atomic_exchange(&vt->park_token, 0)) {
			drift_vt_resume_clear(vt);
			atomic_store(&vt->state, DRIFT_VT_RUNNING);
			return;
		}
		lv_set_t(&vt->state_since_ms, drift_now_ms());
		atomic_store(&vt->state, DRIFT_VT_PARKED);
		drift_test_park_pause();  /* test-only window widener (Finding 1) */
		/* Handshake re-check: if a resumer latched a token while we were still
		 * RUNNING (before we published PARKED), claim it now and do not park.
		 * Paired with unpark's "set token then re-check state == PARKED". */
		if (atomic_exchange(&vt->park_token, 0)) {
			drift_vt_resume_clear(vt);
			atomic_store(&vt->state, DRIFT_VT_RUNNING);
			return;
		}
		if (drift_valgrind_mode)
			swapcontext(&vt->ctx_uc, drift_sched_ctx_uc);
		else
			drift_swapcontext(&vt->ctx, drift_sched_ctx);
		vt->io_bytes_since_yield = 0;
		atomic_store(&vt->state, DRIFT_VT_RUNNING);
		return;
	}
#endif
	pthread_mutex_lock(&vt->mu);
	lv_set_t(&vt->state_since_ms, drift_now_ms());
	atomic_store(&vt->state, DRIFT_VT_PARKED);
	while (atomic_load(&vt->park_token) == 0 && !atomic_load(&vt->cancelled)) {
		pthread_cond_wait(&vt->cv, &vt->mu);
	}
	atomic_store(&vt->park_token, 0);
	vt->io_bytes_since_yield = 0;
	lv_set_i(&vt->wait_kind, DRIFT_WAIT_NONE);
	lv_set_u(&vt->wait_id, 0);
	lv_set_t(&vt->state_since_ms, drift_now_ms());
	atomic_store(&vt->state, DRIFT_VT_RUNNING);
	pthread_mutex_unlock(&vt->mu);
}

void drift_thread_yield(void) {
	DriftVt *vt = drift_vt_tls_get();
	if (!vt) {
		sched_yield();
		return;
	}
#ifdef __linux__
	if (drift_sched_ctx) {
		/* Re-enqueue self so the scheduler can pick another VT first.
		 * Stay in READY state (not PARKED) so we don't need an unpark. */
		if (vt->exec) {
			pthread_mutex_lock(&vt->exec->mu);
			drift_exec_enqueue(vt->exec, vt);
			pthread_mutex_unlock(&vt->exec->mu);
		}
		drift_vt_set_ready(vt);
		if (drift_valgrind_mode)
			swapcontext(&vt->ctx_uc, drift_sched_ctx_uc);
		else
			drift_swapcontext(&vt->ctx, drift_sched_ctx);
		vt->io_bytes_since_yield = 0;
		atomic_store(&vt->state, DRIFT_VT_RUNNING);
		return;
	}
#endif
	sched_yield();
}

void drift_thread_park_until(int64_t deadline_ms) {
	DriftVt *vt = drift_vt_tls_get();
	if (deadline_ms <= 0) {
		/* Invalid/elapsed deadline: caller already tagged the wait (e.g.
		 * TIMER/CONDVAR) — clear it so a running VT isn't shown as waiting. */
		if (vt) drift_vt_resume_clear(vt);
		return;
	}
	if (!vt) {
		struct timespec ts;
		ts.tv_sec = (time_t)(deadline_ms / 1000);
		ts.tv_nsec = (long)((deadline_ms % 1000) * 1000000L);
		nanosleep(&ts, NULL);
		return;
	}
	if (atomic_load(&vt->cancelled)) {
		drift_vt_resume_clear(vt);
		return;
	}
#ifdef __linux__
	if (drift_sched_ctx) {
		/* Fast path: a wake is already latched. */
		if (atomic_exchange(&vt->park_token, 0)) {
			drift_vt_resume_clear(vt);
			atomic_store(&vt->state, DRIFT_VT_RUNNING);
			return;
		}
		lv_set_t(&vt->state_since_ms, drift_now_ms());
		atomic_store(&vt->state, DRIFT_VT_PARKED);
		drift_reactor_register_timer((uint64_t)deadline_ms, (uint64_t)vt);
		drift_test_park_pause();  /* test-only window widener (Finding 1) */
		/* Handshake re-check (see drift_thread_park): claim a token latched
		 * during the RUNNING->PARKED transition or the timer registration. */
		if (atomic_exchange(&vt->park_token, 0)) {
			drift_vt_resume_clear(vt);
			atomic_store(&vt->state, DRIFT_VT_RUNNING);
			return;
		}
		if (drift_valgrind_mode)
			swapcontext(&vt->ctx_uc, drift_sched_ctx_uc);
		else
			drift_swapcontext(&vt->ctx, drift_sched_ctx);
		vt->io_bytes_since_yield = 0;
		atomic_store(&vt->state, DRIFT_VT_RUNNING);
		return;
	}
#endif
	struct timespec ts;
	if (clock_gettime(CLOCK_REALTIME, &ts) != 0) {
		return;
	}
	time_t add_sec = (time_t)(deadline_ms / 1000);
	long add_nsec = (long)((deadline_ms % 1000) * 1000000L);
	ts.tv_sec += add_sec;
	ts.tv_nsec += add_nsec;
	if (ts.tv_nsec >= 1000000000L) {
		ts.tv_sec += 1;
		ts.tv_nsec -= 1000000000L;
	}
	pthread_mutex_lock(&vt->mu);
	lv_set_t(&vt->state_since_ms, drift_now_ms());
	atomic_store(&vt->state, DRIFT_VT_PARKED);
	while (atomic_load(&vt->park_token) == 0 && !atomic_load(&vt->cancelled)) {
		int rc = pthread_cond_timedwait(&vt->cv, &vt->mu, &ts);
		if (rc == ETIMEDOUT) {
			break;
		}
	}
	atomic_store(&vt->park_token, 0);
	vt->io_bytes_since_yield = 0;
	lv_set_i(&vt->wait_kind, DRIFT_WAIT_NONE);
	lv_set_u(&vt->wait_id, 0);
	lv_set_t(&vt->state_since_ms, drift_now_ms());
	atomic_store(&vt->state, DRIFT_VT_RUNNING);
	pthread_mutex_unlock(&vt->mu);
}

void drift_thread_unpark(uint64_t vt) {
	DriftVt *h = (DriftVt *)vt;
	if (!h) {
		return;
	}
	if (atomic_load(&h->completed)) {
		return;
	}
	if (h->exec) {
		/* Coherent park/unpark handshake (no lost wake, no stale token):
		 *
		 *  (1) Try to claim a PARKED fiber for the run queue.  The CAS makes the
		 *      PARKED->READY transition atomic so concurrent resumers (worker
		 *      completion, deadline timer, cancellation) enqueue the VT EXACTLY
		 *      ONCE — drift_exec_enqueue does not dedup. */
		int expected = DRIFT_VT_PARKED;
		if (atomic_compare_exchange_strong(&h->state, &expected, DRIFT_VT_READY)) {
			lv_set_t(&h->state_since_ms, drift_now_ms());
			pthread_mutex_lock(&h->exec->mu);
			drift_exec_enqueue(h->exec, h);
			pthread_mutex_unlock(&h->exec->mu);
			/* Wake worker if it is in poll mode (epoll_wait instead of condvar). */
			Reactor *r = drift_default_reactor_ptr;
			if (r) drift_reactor_wake(r);
			return;
		}
		/*  (2) READY: the VT is already being resumed (another resumer claimed the
		 *      wake and enqueued it).  Do NOT deposit a token — a stale token would
		 *      make the VT's next, unrelated park return immediately. */
		if (expected == DRIFT_VT_READY) {
			return;
		}
		/*  (3) RUNNING / NEW (about to park): latch a single wake token, then
		 *      RE-CHECK state.  Paired with park's "set PARKED then re-check
		 *      token", this closes the RUNNING->PARKED lost-wake race: if the VT
		 *      published PARKED after our CAS but before our token store, our
		 *      re-check claims it for the run queue here. */
		atomic_store(&h->park_token, 1);
		expected = DRIFT_VT_PARKED;
		if (atomic_compare_exchange_strong(&h->state, &expected, DRIFT_VT_READY)) {
			/* The VT parked after we latched the token; it may have missed it.
			 * Resume it via the run queue and drop the token we just set (the
			 * enqueue is the wake now). */
			atomic_store(&h->park_token, 0);
			lv_set_t(&h->state_since_ms, drift_now_ms());
			pthread_mutex_lock(&h->exec->mu);
			drift_exec_enqueue(h->exec, h);
			pthread_mutex_unlock(&h->exec->mu);
			Reactor *r = drift_default_reactor_ptr;
			if (r) drift_reactor_wake(r);
			return;
		}
		/* Still RUNNING (the VT will consume the token at its park re-check) or
		 * already READY (claimed elsewhere): nothing more to do. */
		return;
	}
	/* OS-thread fallback VT (no executor): latch the token and signal its cv. */
	pthread_mutex_lock(&h->mu);
	atomic_store(&h->park_token, 1);
	pthread_cond_signal(&h->cv);
	pthread_mutex_unlock(&h->mu);
}

uint64_t drift_exec_default_get(void) {
	uint64_t cur = atomic_load(&drift_default_executor);
	if (cur != 0) {
		return cur;
	}
	pthread_once(&drift_exec_once, drift_exec_init_once);
	return atomic_load(&drift_default_executor);
}

void drift_exec_default_set(uint64_t exec) {
	atomic_store(&drift_default_executor, exec);
}

uint64_t drift_exec_create(int64_t min_threads, int64_t max_threads, int64_t queue_limit, int64_t timeout_ms, int64_t saturation, int64_t stack_bytes) {
	(void)timeout_ms;
	(void)saturation;
	pthread_once(&drift_exec_cleanup_once, drift_exec_register_shutdown_once);
	DriftExec *exec = drift_exec_create_internal(min_threads, max_threads, queue_limit, stack_bytes);
	if (!exec) {
		return 0;
	}
	return (uint64_t)exec;
}

uint64_t drift_exec_submit(uint64_t exec, uint64_t vt) {
	if (drift_exec_submit_override >= 0) {
		return (uint64_t)drift_exec_submit_override;
	}
	if (vt == 0) {
		return 0;
	}
	DriftExec *ex = (DriftExec *)exec;
	if (!ex) {
		return 0;
	}
	DriftVt *h = (DriftVt *)vt;
	if (atomic_load(&h->started)) {
		return 0;
	}
	if (atomic_load(&h->cancelled)) {
		return 0;
	}
	drift_vt_set_ready(h);
	pthread_mutex_lock(&ex->mu);
	if (ex->queue_limit > 0) {
		int running = atomic_load(&ex->running);
		int64_t total = ex->queue_len + (int64_t)running;
		if (total >= ex->queue_limit) {
			pthread_mutex_unlock(&ex->mu);
			return 1;
		}
	}
	h->exec = ex;
	drift_exec_enqueue(ex, h);
	pthread_mutex_unlock(&ex->mu);
	/* Wake worker if it is in poll mode (epoll_wait instead of condvar).
	 * Without this, the condvar signal from drift_exec_enqueue is lost
	 * when the worker owns epoll — identical to drift_thread_unpark. */
	Reactor *r = drift_default_reactor_ptr;
	if (r) drift_reactor_wake(r);
	return 0;
}

uint64_t drift_time_now_ms(void) {
	int64_t now = drift_now_ms();
	if (now < 0) {
		return 0;
	}
	return (uint64_t)now;
}

int64_t drift_time_now_us(void) {
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
		return 0;
	}
	return (int64_t)ts.tv_sec * 1000000LL + (int64_t)ts.tv_nsec / 1000LL;
}

int64_t drift_time_now_utc_us(void) {
	struct timespec ts;
	if (clock_gettime(CLOCK_REALTIME, &ts) != 0) {
		return 0;
	}
	return (int64_t)ts.tv_sec * 1000000LL + (int64_t)ts.tv_nsec / 1000LL;
}

void drift_exec_submit_test_override(int64_t code) {
	drift_exec_submit_override = code;
}

int64_t drift_exec_get_running(uint64_t exec) {
	DriftExec *ex = (DriftExec *)exec;
	if (!ex) {
		return 0;
	}
	return (int64_t)atomic_load(&ex->running);
}

void drift_thread_drop(uint64_t vt) {
	DriftVt *h = (DriftVt *)vt;
	if (!h) {
		return;
	}
	pthread_mutex_lock(&h->mu);
	atomic_store(&h->dropped, 1);
	int is_completed = atomic_load(&h->completed);
	int is_started = atomic_load(&h->started);
	if (!is_completed) {
		atomic_store(&h->cancelled, 1);
		atomic_store(&h->park_token, 1);
		pthread_cond_broadcast(&h->cv);
	}
	pthread_mutex_unlock(&h->mu);
	if (is_completed) {
		drift_vt_destroy(h);
		return;
	}
	if (!is_started && h->exec == NULL) {
		if (!atomic_exchange(&h->completed, 1)) {
			drift_drop_callback(&h->cb);
		}
		drift_vt_destroy(h);
		return;
	}
}

uint64_t drift_thread_cancel(uint64_t vt) {
	DriftVt *h = (DriftVt *)vt;
	if (!h) {
		return 1;
	}
	if (atomic_load(&h->completed)) {
		return 1;
	}
	atomic_store(&h->cancelled, 1);
	if (atomic_load(&h->started)) {
		/* A STARTED VT must be resumed PROMPTLY so it observes the cancellation
		 * at its park site and unwinds, instead of waiting for a deadline timer
		 * or IO event to fire.  drift_thread_unpark picks the right resume path
		 * for the VT's current state: it CAS-claims the PARKED->READY transition
		 * and enqueues a fiber-parked VT exactly once (race-safe against a
		 * concurrent worker/timer unpark), or bumps the park token for a VT
		 * about to park / on the OS-thread fallback. */
		drift_thread_unpark(vt);
		return 0;
	}
	/* Unstarted: mark cancelled; if it has no executor, complete it now and wake
	 * any joiner (the executor-backed case is finished when the worker picks it
	 * up and sees the cancelled flag). */
	atomic_store(&h->state, DRIFT_VT_CANCELLED);
	pthread_mutex_lock(&h->mu);
	atomic_store(&h->park_token, 1);
	pthread_cond_broadcast(&h->cv);
	if (h->exec == NULL) {
		if (!atomic_exchange(&h->completed, 1)) {
			drift_drop_callback(&h->cb);
		}
		atomic_store(&h->park_token, 1);
		uint64_t waiter = h->join_waiter;
		h->join_waiter = 0;
		pthread_cond_broadcast(&h->cv);
		pthread_mutex_unlock(&h->mu);
		if (waiter != 0) {
			drift_thread_unpark(waiter);
		}
		return 0;
	}
	pthread_mutex_unlock(&h->mu);
	return 0;
}

/* ---------- Root VT: run user main on a VT fiber ---------- */

static int64_t drift_root_vt_result = 0;
static int64_t (*drift_root_vt_fn)(void) = NULL;

static void drift_root_vt_call(void *data) {
	(void)data;
	drift_root_vt_result = drift_root_vt_fn();
}

static DriftCallbackVTable drift_root_vt_vtable = {
	.drop = NULL,
	.call = (void *)drift_root_vt_call,
};

/* Process signal await — blocks the calling VT until SIGINT, SIGTERM, or
 * SIGUSR1 is delivered.  Returns the signal number (SIGINT=2, SIGTERM=15,
 * SIGUSR1=10) or -1 if a second waiter attempts to register (hard contract
 * violation).  Linux only.  The signalfd and signal mask are set up in
 * drift_run_main_on_vt before any worker threads are created. */
int64_t drift_signal_await(void) {
#ifdef __linux__
	if (drift_signal_fd < 0) {
		return -1;  /* signal infrastructure not initialized */
	}
	/* Enforce single waiter: CAS NULL → current VT. */
	DriftVt *self = (DriftVt *)pthread_getspecific(drift_vt_tls_key);
	if (!self) {
		return -1;  /* not running on a VT */
	}
	uintptr_t expected = 0;
	if (!atomic_compare_exchange_strong(&drift_signal_waiter_vt, &expected, (uintptr_t)self)) {
		return -1;  /* another waiter already registered */
	}
	/* Ensure the reactor is initialized so it can deliver the signal. */
	drift_reactor_default_get();
	/* Wake the reactor so it re-enters epoll_wait and can dispatch a
	 * pending signal that arrived before this call. */
	drift_reactor_wake(drift_default_reactor_ptr);
	/* Park this VT until the reactor delivers the signal. */
	drift_thread_park(0);
	return (int64_t)atomic_load(&drift_signal_delivered_signo);
#else
	return -1;
#endif
}

/* ---- Liveness interrogator collection (Slice 1) ----------------------- *
 * Implemented here because it reads the VT/exec/reactor structs and their
 * mutexes, all private to this translation unit.  Formatting (text/JSON) and
 * the dedicated signal thread live in liveness_runtime.c. */

static atomic_int_fast64_t drift_runtime_start_ms = 0;

void drift_liveness_set_start_ms(int64_t start_ms) {
	atomic_store(&drift_runtime_start_ms, start_ms);
}

/* Bounded trylock: retry briefly so a momentary lock holder doesn't force a
 * degraded section, but a wedged holder never blocks the dump.  Returns 0 with
 * the lock held, or -1 after giving up (~100ms). */
static int drift_liveness_trylock(pthread_mutex_t *mu) {
	for (int i = 0; i < 500; i++) {
		if (pthread_mutex_trylock(mu) == 0) {
			return 0;
		}
		struct timespec ts;
		ts.tv_sec = 0;
		ts.tv_nsec = 200000L;  /* 200us */
		nanosleep(&ts, NULL);
	}
	return -1;
}

void drift_liveness_collect(DriftLivenessSnapshot *out, int reason) {
	memset(out, 0, sizeof(*out));
	out->reason = reason;
	out->pid = (int)getpid();
	int64_t now = drift_now_ms();
	out->now_ms = now;
	int64_t start = atomic_load(&drift_runtime_start_ms);
	out->uptime_ms = (start > 0 && now >= start) ? (now - start) : 0;
	out->progress_counter = lv_get_u(&drift_progress_counter);
	out->exec_completed = lv_get_u(&drift_completed_counter);
	out->reactor_next_deadline_ms = -1;

	/* Phase 1: reactor — copy timer/watch -> VT mappings into temp arrays so
	 * we can resolve per-VT wait targets without holding the reactor lock
	 * across the VT walk (different lock, avoids ordering hazards). */
	struct { uint64_t vt; int64_t deadline; } timers[DRIFT_LIVENESS_MAX_TIMERS];
	int n_timers = 0;
	struct { uint64_t vt; int fd; uint32_t events; } watches[DRIFT_LIVENESS_MAX_WATCHES];
	int n_watches = 0;
#ifdef __linux__
	Reactor *r = drift_default_reactor_ptr;
	if (r) {
		out->reactor_present = 1;
		if (drift_liveness_trylock(&r->mu) == 0) {
			int64_t next = -1;
			for (ReactorTimer *t = r->timers; t; t = t->next) {
				out->reactor_timers++;
				if (next < 0 || t->deadline_ms < next) {
					next = t->deadline_ms;
				}
				if (n_timers < DRIFT_LIVENESS_MAX_TIMERS) {
					timers[n_timers].vt = t->vt;
					timers[n_timers].deadline = t->deadline_ms;
					n_timers++;
				}
			}
			out->reactor_next_deadline_ms = next;
			for (ReactorWatch *w = r->watches; w; w = w->next) {
				if (w->read_vt || w->write_vt) {
					out->reactor_fd_waiters++;
				}
				if (w->read_vt && n_watches < DRIFT_LIVENESS_MAX_WATCHES) {
					watches[n_watches].vt = w->read_vt;
					watches[n_watches].fd = w->fd;
					watches[n_watches].events = w->events;
					n_watches++;
				}
				if (w->write_vt && n_watches < DRIFT_LIVENESS_MAX_WATCHES) {
					watches[n_watches].vt = w->write_vt;
					watches[n_watches].fd = w->fd;
					watches[n_watches].events = w->events;
					n_watches++;
				}
			}
			pthread_mutex_unlock(&r->mu);
		} else {
			out->degraded_reactor = 1;
		}
	}
#endif

	/* Phase 2: executor (default = first registry entry).  exec fields read
	 * lock-free while holding the registry lock (which pins `ex` against
	 * concurrent removal/free); racy ints are acceptable for a snapshot. */
	if (drift_liveness_trylock(&drift_exec_registry_mu) == 0) {
		DriftExec *ex = drift_exec_registry_head;
		if (ex) {
			out->exec_present = 1;
			out->exec_workers = ex->threads_count;
			out->exec_running = atomic_load(&ex->running);
			out->exec_ready_queue_len = ex->queue_len;
			out->exec_shutting_down = ex->shutting_down;
		}
		pthread_mutex_unlock(&drift_exec_registry_mu);
	} else {
		out->degraded_exec_registry = 1;
	}

	/* Phase 3: VT registry walk. */
	if (drift_liveness_trylock(&drift_vt_registry_mu) == 0) {
		for (DriftVt *vt = drift_vt_registry_head; vt; vt = vt->reg_next) {
			if (out->vt_count >= DRIFT_LIVENESS_MAX_VTS) {
				out->vt_truncated = 1;
				break;
			}
			DriftVtSnapshot *s = &out->vts[out->vt_count++];
			s->vtid = vt->vtid;
			s->state = atomic_load(&vt->state);
			s->wait_kind = lv_get_i(&vt->wait_kind);
			s->wait_id = lv_get_u(&vt->wait_id);
			s->state_since_ms = lv_get_t(&vt->state_since_ms);
			s->carrier_tid = lv_get_u(&vt->carrier_tid);
			s->last_progress = lv_get_u(&vt->last_progress);
			s->timer_deadline_ms = -1;
			s->io_fd = -1;
			s->io_events = 0;
			switch (s->state) {
				case DRIFT_VT_RUNNING:   out->tally_running++; break;
				case DRIFT_VT_READY:     out->tally_ready++; break;
				case DRIFT_VT_PARKED:    out->tally_parked++; break;
				case DRIFT_VT_FINISHED:  out->tally_finished++; break;
				case DRIFT_VT_CANCELLED: out->tally_cancelled++; break;
				default: break;
			}
			if (s->wait_kind >= 0 && s->wait_kind < 6) {
				out->tally_wait[s->wait_kind]++;
			}
			if (s->wait_kind == DRIFT_WAIT_TIMER) {
				for (int i = 0; i < n_timers; i++) {
					if (timers[i].vt == (uint64_t)vt) {
						s->timer_deadline_ms = timers[i].deadline;
						break;
					}
				}
			} else if (s->wait_kind == DRIFT_WAIT_IO) {
				s->io_fd = (int)s->wait_id;
				for (int i = 0; i < n_watches; i++) {
					if (watches[i].vt == (uint64_t)vt) {
						s->io_events = watches[i].events;
						break;
					}
				}
			}
		}
		pthread_mutex_unlock(&drift_vt_registry_mu);
	} else {
		out->degraded_vt_registry = 1;
	}
}

int64_t drift_run_main_on_vt(int64_t (*user_main)(void)) {
	/* Ignore SIGPIPE globally.  Socket/TLS write failures on closed peers
	 * must return EPIPE through normal error handling, not terminate the
	 * process.  This is standard practice for network programs (Go, Rust,
	 * Python, Node.js all do this at startup). */
	signal(SIGPIPE, SIG_IGN);

	/* Block SIGINT/SIGTERM/SIGUSR1 in the main thread.  All subsequently
	 * created threads (executor workers, reactor, blocking pool) inherit this
	 * mask.  signalfd becomes the sole consumer of these signals.  SIGUSR1 is
	 * an application-controlled signal (e.g. reload trigger) surfaced through
	 * the same generic ssi_signo dispatch as SIGINT/SIGTERM; it does NOT
	 * collide with SIGUSR2 (the liveness interrogator, consumed by a separate
	 * sigwait thread, never via signalfd).
	 *
	 * SIGUSR2 is also blocked here, BEFORE any carrier/reactor/app thread is
	 * created, so no thread ever carries the default SIGUSR2 disposition
	 * (which would terminate the process).  The dedicated liveness thread is
	 * the sole consumer of SIGUSR2 via sigwait.  Ordering is a correctness
	 * requirement: the block must precede executor/reactor startup. */
	drift_liveness_set_start_ms(drift_now_ms());
#ifdef __linux__
	{
		sigset_t mask;
		sigemptyset(&mask);
		sigaddset(&mask, SIGINT);
		sigaddset(&mask, SIGTERM);
		sigaddset(&mask, SIGUSR1);
		sigprocmask(SIG_BLOCK, &mask, NULL);
		drift_signal_fd = signalfd(-1, &mask, SFD_NONBLOCK);

		sigset_t usr2;
		sigemptyset(&usr2);
		sigaddset(&usr2, SIGUSR2);
		sigprocmask(SIG_BLOCK, &usr2, NULL);
	}
	/* Start the liveness interrogator thread.  If it cannot start it leaves
	 * SIGUSR2 blocked (never re-armed to default-terminate) and warns once;
	 * the feature is simply unavailable.  See drift_liveness_thread_start. */
	drift_liveness_thread_start();
#endif

	drift_root_vt_fn = user_main;
	drift_root_vt_result = 0;

	/* Eagerly init default executor.  Reactor is lazy — initialized on
	 * first I/O or timer use.  Eager reactor init would start the reactor
	 * thread unconditionally, which is wasteful for non-I/O programs and
	 * can cause symbol-collision issues (e.g., user-defined `read` fn
	 * overriding libc read in the reactor epoll loop). */
	uint64_t exec = drift_exec_default_get();

	/* Construct minimal DriftIface for root VT callback. */
	DriftIface cb;
	memset(&cb, 0, sizeof(cb));
	cb.vtable = (void *)&drift_root_vt_vtable;

	/* Create and submit root VT.  Pre-set a large stack (8 MiB) to match
	 * OS thread defaults — user main may use deep recursion or large
	 * stack-allocated structs.  Spawned child VTs keep the executor's
	 * default (256 KiB) unless overridden. */
	uint64_t root_vt = drift_thread_spawn(&cb, exec);
	((DriftVt *)root_vt)->stack_size = 8388608;  /* 8 MiB */
	drift_exec_submit(exec, root_vt);

	/* Block OS main thread until root VT completes.
	 * This uses the non-VT condvar path in drift_thread_join
	 * since the OS main thread has no VT TLS set. */
	drift_thread_join(root_vt);

	/* Stop + join the SIGUSR2 liveness thread FIRST, so no runtime-owned
	 * thread survives into the registry/reactor/executor teardown and the
	 * subsequent atexit/TLS cleanup (the runtime's standing shutdown
	 * invariant).  Must precede drift_runtime_registry_cleanup_now(). */
	drift_liveness_thread_shutdown();

	/* Tear down runtime threads before returning to @main.
	 *
	 * Under ABI 5, user main returned on the OS main thread with no
	 * runtime worker threads alive.  Under ABI 6, the executor worker
	 * thread is still alive (idle on its condvar) after the root VT
	 * completes.  If we leave it alive, the atexit firing order becomes:
	 *
	 *   1. Third-party atexit (e.g., OPENSSL_cleanup) — frees globals
	 *   2. Executor atexit — joins worker — worker TLS destructors fire
	 *      but library globals are already freed → leaked per-thread state
	 *
	 * By shutting down the executor here, worker TLS destructors fire
	 * while third-party libraries are still initialized.  The executor
	 * and reactor atexit handlers become no-ops. */
	/* Drain the global runtime registry while the executor + reactor are
	 * still alive.  Registry entries may be Drift `Arc<T>` values whose
	 * `T` implements `core.Destructible`; firing those droppers can
	 * legitimately call stdlib primitives that need a live runtime
	 * (e.g. joining a keepalive VT inside `pool.close()`).  If we deferred
	 * this to libc atexit -- AFTER reactor + executor are torn down --
	 * any such dropper hangs because the worker that would run its
	 * waiter no longer exists.  Symptom (filed 2026-05-21 from bookkeeper
	 * shutdown-hang repro): single-threaded `futex_do_wait` on the main
	 * thread, last log line user code emitted is the post-`main` marker
	 * but the process never exits.
	 *
	 * `drift_runtime_registry_cleanup_now` is idempotent (atomic
	 * exchange flag); the registry-cleanup atexit handler runs second
	 * and no-ops. */
	drift_runtime_registry_cleanup_now();
	/* Quiesce the blocking pool BEFORE the reactor/executor are torn down, so any
	 * authorized worker unpark lands on a still-live executor/VT (Finding 1).  The
	 * atexit handler — which runs AFTER this teardown — would otherwise be too late.
	 * Idempotent: the atexit fallback no-ops. */
	drift_blocking_pool_quiesce();
	drift_reactor_shutdown_default_atexit();
	drift_exec_shutdown_default_atexit();

	return drift_root_vt_result;
}

uint64_t drift_reactor_default_get(void) {
	uint64_t cur = atomic_load(&drift_default_reactor);
	if (cur != 0) {
		return cur;
	}
	pthread_once(&drift_reactor_once, drift_reactor_init_once);
	return atomic_load(&drift_default_reactor);
}

void drift_reactor_default_set(uint64_t reactor) {
	atomic_store(&drift_default_reactor, reactor);
	drift_default_reactor_ptr = (Reactor *)reactor;
}

void drift_reactor_forget_fd(int fd) {
	Reactor *r = drift_default_reactor_ptr;
	if (!r) return;
	pthread_mutex_lock(&r->mu);
	/* Explicit EPOLL_CTL_DEL before unlink.  Under persistent ET registration
	 * the fd stays in the epoll set for its entire lifetime; we must remove
	 * it before close() to avoid stale-fd races if the fd number is reused. */
	if (r->epoll_fd >= 0) {
		epoll_ctl(r->epoll_fd, EPOLL_CTL_DEL, fd, NULL);
	}
	ReactorWatch *prev = NULL;
	ReactorWatch *cur = r->watches;
	while (cur) {
		if (cur->fd == fd) {
			if (prev) prev->next = cur->next;
			else r->watches = cur->next;
			/* Clear all per-direction state before free. */
			cur->read_vt = 0;
			cur->write_vt = 0;
			cur->pending_read = 0;
			cur->pending_write = 0;
			free(cur);
			break;
		}
		prev = cur;
		cur = cur->next;
	}
	pthread_mutex_unlock(&r->mu);
}

void drift_reactor_register_io(uint64_t fd, uint64_t interest, uint64_t vt, uint64_t deadline_ms) {
#ifdef __linux__
	Reactor *r = (Reactor *)drift_reactor_default_get();
	if (vt != 0) {
		DriftVt *h = (DriftVt *)vt;
		int st = atomic_load(&h->state);
		if (st == DRIFT_VT_FINISHED || st == DRIFT_VT_CANCELLED) {
			return;
		}
		/* Liveness: label the imminent park (by stdlib _block_on_io) as an
		 * IO wait on this fd.  Cleared on resume. */
		lv_set_i(&h->wait_kind, DRIFT_WAIT_IO);
		lv_set_u(&h->wait_id, fd);
	}
	if (!r) {
		return;
	}
	pthread_mutex_lock(&r->mu);
	ReactorWatch *w = drift_reactor_find_watch(r, (int)fd);
	int existed = (w != NULL);
	if (!w) {
		w = (ReactorWatch *)malloc(sizeof(ReactorWatch));
		if (!w) {
			pthread_mutex_unlock(&r->mu);
			return;
		}
		w->fd = (int)fd;
		w->events = EPOLLET | EPOLLIN | EPOLLOUT;
		w->read_vt = 0;
		w->write_vt = 0;
		w->pending_read = 0;
		w->pending_write = 0;
		w->next = r->watches;
		r->watches = w;
	}
	/* Set the waiter for the requested direction. */
	if ((uint32_t)interest & EPOLLIN) {
		w->read_vt = vt;
	} else if ((uint32_t)interest & EPOLLOUT) {
		w->write_vt = vt;
	}
	pthread_mutex_unlock(&r->mu);
	/* Persistent ET: EPOLL_CTL_ADD once on first registration.
	 * No hot-path epoll_ctl on subsequent calls. */
	if (!existed && r->epoll_fd >= 0) {
		struct epoll_event ev;
		ev.events = EPOLLET | EPOLLIN | EPOLLOUT;
		ev.data.fd = (int)fd;
		epoll_ctl(r->epoll_fd, EPOLL_CTL_ADD, (int)fd, &ev);
	}
	/* `deadline_ms` is intentionally NOT used here to register a wake
	 * timer: that responsibility belongs to the caller's
	 * `vt_park_until(deadline_ms)` call which is the single timer-
	 * registration authority.  Registering the timer twice (once
	 * here, once inside `vt_park_until`) produces two wakes per
	 * deadline.  In the FAST-I/O case the reactor's I/O dispatch
	 * path proactively removes every timer for the woken VT from the
	 * timer list before unparking, so the duplicate is silently
	 * cleaned up.  In the TIMEOUT case the duplicates both survive
	 * to the timer collect, both fire, and the second unpark hits a
	 * non-PARKED VT (the first one advanced it to READY) and takes
	 * the fallback `park_token++` branch.  The stale token then
	 * short-circuits the VT's next park, producing the customer-
	 * visible "main's sleep(550) returns in ~1ms after rpc.connect()"
	 * symptom (their handshake internally hit a timed-I/O timeout
	 * path).  Symmetric to the 0.31.83 sleep fix in
	 * std.concurrent.sleep; see `doc/history.md`. */
	(void)deadline_ms;
#else
	(void)fd;
	(void)interest;
	(void)vt;
	(void)deadline_ms;
#endif
}

/* ET fairness: check and clear pending-ready for a direction.
 * Returns 1 if pending was set (caller should retry IO, not park).
 * Called from stdlib _block_on_io on EAGAIN path. */
int64_t drift_reactor_check_pending(int64_t fd, int64_t direction) {
#ifdef __linux__
	Reactor *r = drift_default_reactor_ptr;
	if (!r) return 0;
	int result = 0;
	pthread_mutex_lock(&r->mu);
	ReactorWatch *w = drift_reactor_find_watch(r, (int)fd);
	if (w) {
		if (((uint32_t)direction & EPOLLIN) && w->pending_read) {
			w->pending_read = 0;
			result = 1;
		} else if (((uint32_t)direction & EPOLLOUT) && w->pending_write) {
			w->pending_write = 0;
			result = 1;
		}
	}
	pthread_mutex_unlock(&r->mu);
	return (int64_t)result;
#else
	(void)fd; (void)direction;
	return 0;
#endif
}

/* ET fairness: charge bytes to the current VT's drain budget.
 * If the budget is exceeded, sets pending-ready on the watch,
 * re-enqueues the VT, and yields to the scheduler (swapcontext).
 * Returns 1 if a yield occurred, 0 otherwise.
 * Called from stdlib after each successful io_read/io_write. */
int64_t drift_reactor_io_charge(int64_t fd, int64_t direction, int64_t bytes) {
#ifdef __linux__
	DriftVt *vt = drift_vt_tls_get();
	if (!vt || bytes <= 0) return 0;
	vt->io_bytes_since_yield += bytes;
	if (vt->io_bytes_since_yield < DRIFT_IO_BUDGET_BYTES) return 0;

	/* Budget exceeded — set pending, reset counter, yield. */
	Reactor *r = drift_default_reactor_ptr;
	if (r) {
		pthread_mutex_lock(&r->mu);
		ReactorWatch *w = drift_reactor_find_watch(r, (int)fd);
		if (w) {
			if ((uint32_t)direction & EPOLLIN)  w->pending_read  = 1;
			if ((uint32_t)direction & EPOLLOUT) w->pending_write = 1;
		}
		pthread_mutex_unlock(&r->mu);
	}
	vt->io_bytes_since_yield = 0;

	/* Re-enqueue self and yield to scheduler. */
	DriftExec *exec = vt->exec;
	if (!exec || !drift_sched_ctx) return 0;
	drift_vt_set_ready(vt);
	pthread_mutex_lock(&exec->mu);
	drift_exec_enqueue(exec, vt);
	pthread_mutex_unlock(&exec->mu);
	if (r) drift_reactor_wake(r);
	if (drift_valgrind_mode)
		swapcontext(&vt->ctx_uc, drift_sched_ctx_uc);
	else
		drift_swapcontext(&vt->ctx, drift_sched_ctx);
	atomic_store(&vt->state, DRIFT_VT_RUNNING);
	return 1;
#else
	(void)fd; (void)direction; (void)bytes;
	return 0;
#endif
}

void drift_reactor_register_timer(uint64_t deadline_ms, uint64_t vt) {
	Reactor *r = (Reactor *)drift_reactor_default_get();
	if (vt != 0) {
		DriftVt *h = (DriftVt *)vt;
		int st = atomic_load(&h->state);
		if (st == DRIFT_VT_FINISHED || st == DRIFT_VT_CANCELLED) {
			return;
		}
	}
	if (!r) {
		return;
	}
	pthread_mutex_lock(&r->mu);
	drift_reactor_add_timer(r, (int64_t)deadline_ms, vt);
	pthread_mutex_unlock(&r->mu);
	drift_reactor_wake(r);
}

uint64_t drift_test_eventfd_create(void) {
#ifdef __linux__
	int fd = eventfd(0, EFD_NONBLOCK);
	if (fd < 0) {
		return 0;
	}
	return (uint64_t)fd;
#else
	return 0;
#endif
}

void drift_test_eventfd_write(uint64_t fd, uint64_t value) {
#ifdef __linux__
	uint64_t v = value;
	(void)write((int)fd, &v, sizeof(v));
#else
	(void)fd;
	(void)value;
#endif
}

uint64_t drift_test_timerfd_create(void) {
#ifdef __linux__
	int fd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK);
	if (fd < 0) {
		return 0;
	}
	return (uint64_t)fd;
#else
	return 0;
#endif
}

void drift_test_timerfd_set(uint64_t fd, uint64_t delay_ms) {
#ifdef __linux__
	struct itimerspec spec;
	spec.it_interval.tv_sec = 0;
	spec.it_interval.tv_nsec = 0;
	spec.it_value.tv_sec = (time_t)(delay_ms / 1000);
	spec.it_value.tv_nsec = (long)((delay_ms % 1000) * 1000000L);
	timerfd_settime((int)fd, 0, &spec, NULL);
#else
	(void)fd;
	(void)delay_ms;
#endif
}

void *drift_runtime_global_registry_ptr(void) {
	pthread_once(&drift_runtime_registry_cleanup_once, drift_runtime_registry_register_cleanup_once);
	return (void *)&drift_runtime_global_registry;
}

void *drift_runtime_thread_registry_ptr(void) {
	pthread_once(&drift_runtime_registry_cleanup_once, drift_runtime_registry_register_cleanup_once);
	return (void *)&drift_runtime_thread_registry;
}

uint64_t drift_runtime_registry_set(uint64_t type_tag, void *ptr, DriftIface dropper) {
	if (ptr == NULL) {
		drift_drop_callback(&dropper);
		return 0;
	}
	pthread_once(&drift_runtime_registry_cleanup_once, drift_runtime_registry_register_cleanup_once);
	pthread_mutex_lock(&drift_runtime_registry_mu);
	DriftRuntimeRegistryEntry *cur = drift_runtime_registry_head;
	while (cur) {
		if (cur->type_tag == type_tag) {
			pthread_mutex_unlock(&drift_runtime_registry_mu);
			drift_drop_callback(&dropper);
			return 0;
		}
		cur = cur->next;
	}
	DriftRuntimeRegistryEntry *entry = (DriftRuntimeRegistryEntry *)malloc(sizeof(DriftRuntimeRegistryEntry));
	if (!entry) {
		pthread_mutex_unlock(&drift_runtime_registry_mu);
		drift_drop_callback(&dropper);
		return 0;
	}
	entry->type_tag = type_tag;
	entry->ptr = ptr;
	entry->dropper = dropper;
	entry->next = drift_runtime_registry_head;
	drift_runtime_registry_head = entry;
	pthread_mutex_unlock(&drift_runtime_registry_mu);
	return 1;
}

uint64_t drift_runtime_registry_contains(uint64_t type_tag) {
	pthread_mutex_lock(&drift_runtime_registry_mu);
	DriftRuntimeRegistryEntry *cur = drift_runtime_registry_head;
	while (cur) {
		if (cur->type_tag == type_tag) {
			pthread_mutex_unlock(&drift_runtime_registry_mu);
			return 1;
		}
		cur = cur->next;
	}
	pthread_mutex_unlock(&drift_runtime_registry_mu);
	return 0;
}

void *drift_runtime_registry_get(uint64_t type_tag) {
	pthread_mutex_lock(&drift_runtime_registry_mu);
	DriftRuntimeRegistryEntry *cur = drift_runtime_registry_head;
	while (cur) {
		if (cur->type_tag == type_tag) {
			void *ptr = cur->ptr;
			pthread_mutex_unlock(&drift_runtime_registry_mu);
			return ptr;
		}
		cur = cur->next;
	}
	pthread_mutex_unlock(&drift_runtime_registry_mu);
	return NULL;
}

uint64_t drift_runtime_thread_registry_set(uint64_t type_tag, void *ptr, DriftIface dropper) {
	if (ptr == NULL) {
		drift_drop_callback(&dropper);
		return 0;
	}
	pthread_once(&drift_runtime_registry_cleanup_once, drift_runtime_registry_register_cleanup_once);
	DriftVt *vt = drift_vt_tls_get();
	if (vt) {
		pthread_mutex_lock(&vt->mu);
		DriftRuntimeRegistryEntry *cur = vt->thread_registry_head;
		while (cur) {
			if (cur->type_tag == type_tag) {
				pthread_mutex_unlock(&vt->mu);
				drift_drop_callback(&dropper);
				return 0;
			}
			cur = cur->next;
		}
		DriftRuntimeRegistryEntry *entry = (DriftRuntimeRegistryEntry *)malloc(sizeof(DriftRuntimeRegistryEntry));
		if (!entry) {
			pthread_mutex_unlock(&vt->mu);
			drift_drop_callback(&dropper);
			return 0;
		}
		entry->type_tag = type_tag;
		entry->ptr = ptr;
		entry->dropper = dropper;
		entry->next = vt->thread_registry_head;
		vt->thread_registry_head = entry;
		pthread_mutex_unlock(&vt->mu);
		return 1;
	}
	DriftRuntimeRegistryEntry *cur = drift_runtime_thread_registry_head;
	while (cur) {
		if (cur->type_tag == type_tag) {
			drift_drop_callback(&dropper);
			return 0;
		}
		cur = cur->next;
	}
	DriftRuntimeRegistryEntry *entry = (DriftRuntimeRegistryEntry *)malloc(sizeof(DriftRuntimeRegistryEntry));
	if (!entry) {
		drift_drop_callback(&dropper);
		return 0;
	}
	entry->type_tag = type_tag;
	entry->ptr = ptr;
	entry->dropper = dropper;
	entry->next = drift_runtime_thread_registry_head;
	drift_runtime_thread_registry_head = entry;
	return 1;
}

uint64_t drift_runtime_thread_registry_contains(uint64_t type_tag) {
	DriftVt *vt = drift_vt_tls_get();
	if (vt) {
		pthread_mutex_lock(&vt->mu);
		DriftRuntimeRegistryEntry *cur = vt->thread_registry_head;
		while (cur) {
			if (cur->type_tag == type_tag) {
				pthread_mutex_unlock(&vt->mu);
				return 1;
			}
			cur = cur->next;
		}
		pthread_mutex_unlock(&vt->mu);
		return 0;
	}
	DriftRuntimeRegistryEntry *cur = drift_runtime_thread_registry_head;
	while (cur) {
		if (cur->type_tag == type_tag) {
			return 1;
		}
		cur = cur->next;
	}
	return 0;
}

void *drift_runtime_thread_registry_get(uint64_t type_tag) {
	DriftVt *vt = drift_vt_tls_get();
	if (vt) {
		pthread_mutex_lock(&vt->mu);
		DriftRuntimeRegistryEntry *cur = vt->thread_registry_head;
		while (cur) {
			if (cur->type_tag == type_tag) {
				void *ptr = cur->ptr;
				pthread_mutex_unlock(&vt->mu);
				return ptr;
			}
			cur = cur->next;
		}
		pthread_mutex_unlock(&vt->mu);
		return NULL;
	}
	DriftRuntimeRegistryEntry *cur = drift_runtime_thread_registry_head;
	while (cur) {
		if (cur->type_tag == type_tag) {
			void *ptr = cur->ptr;
			return ptr;
		}
		cur = cur->next;
	}
	return NULL;
}

/* Remove all reactor timers for a given VT.  Called on the happy path
 * of blocking-job completion so stale deadline timers do not fire
 * spurious unparks later. */
void drift_reactor_cancel_vt_timers(uint64_t vt_id) {
#ifdef __linux__
	Reactor *r = (Reactor *)atomic_load(&drift_default_reactor);
	if (!r) return;
	pthread_mutex_lock(&r->mu);
	ReactorTimer *tp = NULL;
	ReactorTimer *tc = r->timers;
	while (tc) {
		ReactorTimer *tn = tc->next;
		if (tc->vt == vt_id) {
			if (tp) tp->next = tn; else r->timers = tn;
			free(tc);
		} else {
			tp = tc;
		}
		tc = tn;
	}
	pthread_mutex_unlock(&r->mu);
#else
	(void)vt_id;
#endif
}

/* ================================================================
 * Generic blocking-job offload pool
 * ================================================================ */

/* DriftBlockingJob is defined in posix/blocking_pool.h (shared with the
 * fs_runtime.c read_dir consumer). */

typedef struct DriftBlockingPool {
	pthread_mutex_t mu;
	pthread_cond_t cv;
	DriftBlockingJob *queue_head;
	DriftBlockingJob *queue_tail;
	int queue_len;
	int queue_limit;
	atomic_int stopping;  /* set at shutdown; gates post-shutdown worker unparks */
	/* Number of worker unparks currently authorized/in-flight.  A worker takes a
	 * stake (under mu, only if !stopping) before calling drift_thread_unpark and
	 * drops it after; shutdown waits (on drain_cv) until this reaches 0 before
	 * tearing anything down, so an authorized notification can never race VT
	 * teardown.  A second atomic stopping check alone is insufficient. */
	int inflight_unparks;
	pthread_cond_t drain_cv;
	int worker_count;
	pthread_t *workers;
} DriftBlockingPool;

#define DRIFT_BLOCKING_POOL_WORKERS 4
#define DRIFT_BLOCKING_POOL_QUEUE_LIMIT 64

static DriftBlockingPool *drift_blocking_pool_ptr = NULL;
static pthread_once_t drift_blocking_pool_once = PTHREAD_ONCE_INIT;

static void *drift_blocking_worker(void *arg) {
	DriftBlockingPool *pool = (DriftBlockingPool *)arg;
	while (1) {
		pthread_mutex_lock(&pool->mu);
		while (!atomic_load(&pool->stopping) && pool->queue_head == NULL) {
			pthread_cond_wait(&pool->cv, &pool->mu);
		}
		if (atomic_load(&pool->stopping) && pool->queue_head == NULL) {
			pthread_mutex_unlock(&pool->mu);
			return NULL;
		}
		DriftBlockingJob *job = pool->queue_head;
		pool->queue_head = job->next;
		if (pool->queue_head == NULL) {
			pool->queue_tail = NULL;
		}
		pool->queue_len--;
		job->next = NULL;
		pthread_mutex_unlock(&pool->mu);

		job->job_fn(job);
		atomic_store(&job->completed, 1);

		/* Refcount protocol (see posix/blocking_pool.h): unpark the VT unless
		 * it has abandoned the job or already resumed itself, then drop the
		 * worker's stake.  job->vt / job->expired are read BEFORE the release,
		 * so the job memory is still alive here even if the VT's release races
		 * us — the last release frees.
		 *
		 * The unpark is authorized under pool->mu and gated on !stopping, and an
		 * in-flight stake is held across it: shutdown sets stopping and then
		 * WAITS for inflight_unparks to drain before any teardown, so an
		 * authorized unpark can never race VT/executor destruction.  (A bare
		 * atomic stopping check would still let an unpark that already passed the
		 * check fire after teardown began.) */
		if (!atomic_load(&job->expired)) {
			int authorized = 0;
			pthread_mutex_lock(&pool->mu);
			if (!atomic_load(&pool->stopping)) {
				pool->inflight_unparks++;
				authorized = 1;
			}
			pthread_mutex_unlock(&pool->mu);
			if (authorized) {
				/* Test-only: pause AFTER taking the in-flight stake but BEFORE the
				 * unpark, so a regression can initiate shutdown during this window
				 * and prove shutdown drains the stake before teardown (Finding 2). */
				const char *wp = getenv("DRIFT_TEST_WORKER_UNPARK_PAUSE_MS");
				if (wp && *wp) {
					long ms = atol(wp);
					if (ms > 0) {
						struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
						nanosleep(&ts, NULL);
					}
				}
				if (atomic_exchange(&job->vt_resumed, 1) == 0) {
					drift_thread_unpark(job->vt);
				}
				pthread_mutex_lock(&pool->mu);
				pool->inflight_unparks--;
				if (pool->inflight_unparks == 0) {
					pthread_cond_broadcast(&pool->drain_cv);
				}
				pthread_mutex_unlock(&pool->mu);
			}
		}
		drift_blocking_job_release(job);
	}
	return NULL;
}

void drift_blocking_job_release(DriftBlockingJob *job) {
	if (atomic_fetch_sub(&job->refcount, 1) == 1) {
		job->destroy_fn(job);
	}
}

/* Total wall-clock budget for joining blocking workers at shutdown.  Workers
 * that finish their current job within this window are joined cleanly (so an
 * abandoned job's snapshot is freed by the worker); a worker still stuck in an
 * uninterruptible filesystem syscall (e.g. a hung NFS mount) is NOT waited on —
 * the process exits promptly and the OS reaps the thread.  An abandoned NFS
 * operation must never block process exit indefinitely. */
#define DRIFT_BLOCKING_SHUTDOWN_BUDGET_MS 2000

static atomic_int drift_blocking_pool_quiesced;

/* Stop + drain the blocking pool: set stopping, WAIT for any authorized in-flight
 * unpark to finish, then bounded-join the workers.  Idempotent (runs once).
 *
 * This MUST be called synchronously during runtime teardown — after the registry
 * cleanup but BEFORE the reactor/executor are shut down (drift_run_main_on_vt) —
 * so that a worker's drift_thread_unpark(job->vt) lands on a STILL-LIVE executor.
 * The libc atexit handler is only a fallback; by the time it runs, the executor
 * and reactor are already gone. */
void drift_blocking_pool_quiesce(void) {
	if (atomic_exchange(&drift_blocking_pool_quiesced, 1)) {
		return;  /* already quiesced (e.g. explicit teardown ran; atexit no-ops) */
	}
	DriftBlockingPool *pool = drift_blocking_pool_ptr;
	if (!pool) return;
	/* No new submissions can race shutdown: any concurrent drift_blocking_submit
	 * sees `stopping` and is rejected. */
	pthread_mutex_lock(&pool->mu);
	atomic_store(&pool->stopping, 1);
	pthread_cond_broadcast(&pool->cv);
	/* Wait for any unpark authorized before `stopping` was set to finish, so no
	 * worker is mid-drift_thread_unpark when later atexit handlers destroy VTs. */
	while (pool->inflight_unparks > 0) {
		pthread_cond_wait(&pool->drain_cv, &pool->mu);
	}
	pthread_mutex_unlock(&pool->mu);

	/* Bounded join: absolute CLOCK_REALTIME deadline shared across all workers,
	 * so total shutdown wait is <= the budget, not budget * worker_count. */
	struct timespec deadline;
	clock_gettime(CLOCK_REALTIME, &deadline);
	deadline.tv_sec += DRIFT_BLOCKING_SHUTDOWN_BUDGET_MS / 1000;
	deadline.tv_nsec += (long)(DRIFT_BLOCKING_SHUTDOWN_BUDGET_MS % 1000) * 1000000L;
	if (deadline.tv_nsec >= 1000000000L) {
		deadline.tv_sec += 1;
		deadline.tv_nsec -= 1000000000L;
	}
	int all_joined = 1;
	for (int i = 0; i < pool->worker_count; i++) {
		if (pthread_timedjoin_np(pool->workers[i], NULL, &deadline) != 0) {
			/* This worker (and, conservatively, any after it) is stuck in a
			 * syscall past the budget — stop waiting and let the process exit. */
			all_joined = 0;
			break;
		}
	}

	if (!all_joined) {
		/* A worker is still running and may still touch pool->mu / its job, so it
		 * is NOT safe to free the pool here.  Leak the pool struct + workers
		 * (the process is exiting; the OS reclaims everything).  A late-waking
		 * worker safely finishes, frees its job, and exits via the stopping
		 * flag against the still-valid pool. */
		return;
	}

	/* All workers joined: drain any remaining queued jobs and free the pool. */
	DriftBlockingJob *j = pool->queue_head;
	while (j) {
		DriftBlockingJob *next = j->next;
		j->destroy_fn(j);
		j = next;
	}
	free(pool->workers);
	pthread_mutex_destroy(&pool->mu);
	pthread_cond_destroy(&pool->cv);
	pthread_cond_destroy(&pool->drain_cv);
	free(pool);
	drift_blocking_pool_ptr = NULL;
}

/* atexit fallback: idempotent via drift_blocking_pool_quiesce's once-flag.  In a
 * normal shutdown the explicit drift_blocking_pool_quiesce() in
 * drift_run_main_on_vt has already drained the pool while the executor/reactor
 * were still alive, so this no-ops. */
static void drift_blocking_pool_shutdown(void) {
	drift_blocking_pool_quiesce();
}

static void drift_blocking_pool_init(void) {
	DriftBlockingPool *pool = calloc(1, sizeof(DriftBlockingPool));
	if (!pool) return;
	pthread_mutex_init(&pool->mu, NULL);
	pthread_cond_init(&pool->cv, NULL);
	pthread_cond_init(&pool->drain_cv, NULL);
	pool->queue_limit = DRIFT_BLOCKING_POOL_QUEUE_LIMIT;
	pool->worker_count = DRIFT_BLOCKING_POOL_WORKERS;
	pool->workers = calloc((size_t)pool->worker_count, sizeof(pthread_t));
	for (int i = 0; i < pool->worker_count; i++) {
		pthread_create(&pool->workers[i], NULL, drift_blocking_worker, pool);
	}
	drift_blocking_pool_ptr = pool;
	atexit(drift_blocking_pool_shutdown);
}

int64_t drift_blocking_submit(DriftBlockingJob *job) {
	pthread_once(&drift_blocking_pool_once, drift_blocking_pool_init);
	DriftBlockingPool *pool = drift_blocking_pool_ptr;
	if (!pool) return -1;
	pthread_mutex_lock(&pool->mu);
	if (pool->queue_len >= pool->queue_limit || atomic_load(&pool->stopping)) {
		pthread_mutex_unlock(&pool->mu);
		return -1;
	}
	job->next = NULL;
	if (pool->queue_tail) {
		pool->queue_tail->next = job;
	} else {
		pool->queue_head = job;
	}
	pool->queue_tail = job;
	pool->queue_len++;
	pthread_cond_signal(&pool->cv);
	pthread_mutex_unlock(&pool->mu);
	return 0;
}

/* ================================================================
 * DNS resolve consumer (first blocking-job consumer)
 * ================================================================ */

typedef struct DriftResolveJob {
	DriftBlockingJob base;
	char *hostname;
	int port;
	struct sockaddr_storage addr;
	socklen_t addrlen;
} DriftResolveJob;

static void drift_resolve_job_destroy(DriftBlockingJob *base) {
	DriftResolveJob *job = (DriftResolveJob *)base;
	free(job->hostname);
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
		if (res) freeaddrinfo(res);
		return;
	}
	memcpy(&job->addr, res->ai_addr, res->ai_addrlen);
	job->addrlen = res->ai_addrlen;
	freeaddrinfo(res);
	base->error = 0;
}

/* Blocking resolve for main-thread path. */
static int drift_resolve_blocking(const char *hostname, int port,
                                  struct sockaddr_storage *addr,
                                  socklen_t *addrlen) {
	struct addrinfo hints = {0}, *res = NULL;
	hints.ai_family = AF_INET;
	hints.ai_socktype = SOCK_STREAM;
	char port_str[8];
	snprintf(port_str, sizeof(port_str), "%d", port);
	int rc = getaddrinfo(hostname, port_str, &hints, &res);
	if (rc != 0 || !res) {
		if (res) freeaddrinfo(res);
		return -1;
	}
	memcpy(addr, res->ai_addr, res->ai_addrlen);
	*addrlen = res->ai_addrlen;
	freeaddrinfo(res);
	return 0;
}

/* Shared helper: create non-blocking socket and connect to resolved address. */
static int64_t drift_connect_to_addr(struct sockaddr_storage *sa, socklen_t addrlen) {
	int fd = socket(sa->ss_family, SOCK_STREAM, 0);
	if (fd < 0) return -1;
	int flags = fcntl(fd, F_GETFL, 0);
	if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	int rc = connect(fd, (struct sockaddr *)sa, addrlen);
	if (rc < 0 && errno != EINPROGRESS) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	return fd;
}

int64_t drift_net_connect(DriftString *ip, int64_t port, int64_t deadline_ms) {
	char *host = drift_string_to_cstr(*ip);

	/* Fast path: IPv4 literal — no DNS, no pool. */
	struct sockaddr_in a4;
	memset(&a4, 0, sizeof(a4));
	a4.sin_family = AF_INET;
	a4.sin_port = htons((uint16_t)port);
	if (inet_pton(AF_INET, host, &a4.sin_addr) == 1) {
		free(host);
		struct sockaddr_storage sa;
		memcpy(&sa, &a4, sizeof(a4));
		return drift_connect_to_addr(&sa, sizeof(a4));
	}

	/* Hostname path. */
	DriftVt *vt = drift_vt_tls_get();
	if (!vt) {
		/* Main thread: block directly in getaddrinfo. */
		struct sockaddr_storage addr;
		socklen_t addrlen;
		int rc = drift_resolve_blocking(host, (int)port, &addr, &addrlen);
		free(host);
		if (rc != 0) { errno = EINVAL; return -1; }
		return drift_connect_to_addr(&addr, addrlen);
	}

	/* VT path: submit to blocking pool. */
	DriftResolveJob *rj = calloc(1, sizeof(DriftResolveJob));
	if (!rj) { free(host); errno = ENOMEM; return -1; }
	rj->base.job_fn = drift_resolve_job_fn;
	rj->base.destroy_fn = drift_resolve_job_destroy;
	rj->base.vt = (uint64_t)vt;
	atomic_store(&rj->base.refcount, 2);  /* VT stake + worker stake */
	rj->hostname = host;  /* ownership transferred */
	rj->port = (int)port;

	int64_t submit_rc = drift_blocking_submit(&rj->base);
	if (submit_rc != 0) {
		/* Not submitted: no worker stake exists, free directly. */
		free(rj->hostname);
		free(rj);
		errno = EAGAIN;
		return -1;
	}

	/* Register caller's deadline as reactor timer, then park. */
	if (deadline_ms > 0) {
		drift_reactor_register_timer((uint64_t)deadline_ms, (uint64_t)vt);
	}
	drift_thread_park(0);
	/* Claim this VT's single wakeup so a late worker does not unpark us. */
	atomic_exchange(&rj->base.vt_resumed, 1);

	/* Resumed — check result. */
	if (atomic_load(&rj->base.completed)) {
		/* Cancel the deadline timer so it does not fire a spurious
		 * unpark later while this VT is parked for something else. */
		if (deadline_ms > 0) {
			drift_reactor_cancel_vt_timers((uint64_t)vt);
		}
		if (rj->base.error != 0) {
			drift_blocking_job_release(&rj->base);
			errno = EINVAL;
			return -1;
		}
		struct sockaddr_storage addr;
		memcpy(&addr, &rj->addr, rj->addrlen);
		socklen_t addrlen = rj->addrlen;
		drift_blocking_job_release(&rj->base);
		return drift_connect_to_addr(&addr, addrlen);
	}
	/* Timed out / abandoned — drop the VT stake; the worker frees the job
	 * when its in-flight getaddrinfo finally returns. */
	atomic_store(&rj->base.expired, 1);
	drift_blocking_job_release(&rj->base);
	errno = EAGAIN;
	return -1;
}
