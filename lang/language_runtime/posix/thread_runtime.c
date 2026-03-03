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
#ifdef __linux__
#include <ucontext.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/timerfd.h>
#include <sys/mman.h>
#include <unistd.h>
#endif

#ifdef NVALGRIND
#define VALGRIND_STACK_REGISTER(start, end) (0)
#define VALGRIND_STACK_DEREGISTER(id) do {} while(0)
#elif __has_include(<valgrind/valgrind.h>)
#include <valgrind/valgrind.h>
#else
#define VALGRIND_STACK_REGISTER(start, end) (0)
#define VALGRIND_STACK_DEREGISTER(id) do {} while(0)
#endif

#include "string_runtime.h"

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
	ucontext_t ctx;
	// Context is initialized once by the worker thread (single-writer).
	int ctx_ready;
#endif
	struct DriftExec *exec;
	pthread_mutex_t mu;
	pthread_cond_t cv;
	int park_token;
	struct DriftVt *reg_prev;
	struct DriftVt *reg_next;
	struct DriftRuntimeRegistryEntry *thread_registry_head;
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

typedef struct ReactorWatch {
	int fd;
	uint32_t events;
	uint64_t vt;
	struct ReactorWatch *next;
} ReactorWatch;

typedef struct Reactor {
	int epoll_fd;
	int wake_fd;
	pthread_mutex_t mu;
	pthread_cond_t cv;
	ReactorTimer *timers;
	ReactorWatch *watches;
	int stopping;
	pthread_t thread;
	int thread_started;
	atomic_int in_wait;
} Reactor;

static Reactor *drift_default_reactor_ptr = NULL;
static void drift_reactor_shutdown_default_atexit(void);

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
	ReactorWatch *wp = NULL;
	ReactorWatch *wc = r->watches;
	while (wc) {
		ReactorWatch *wn = wc->next;
		if (wc->vt == (uint64_t)vt) {
			if (wp) {
				wp->next = wn;
			} else {
				r->watches = wn;
			}
			free(wc);
		} else {
			wp = wc;
		}
		wc = wn;
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
void drift_reactor_register_timer(uint64_t deadline_ms, uint64_t vt);

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
static __thread ucontext_t *drift_sched_ctx = NULL;
#endif

static int64_t drift_now_ms(void) {
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
		return 0;
	}
	return (int64_t)(ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL);
}

static void drift_vt_tls_init_once(void) {
	pthread_key_create(&drift_vt_tls_key, NULL);
}

static void drift_vt_tls_set(DriftVt *vt) {
	pthread_once(&drift_vt_tls_once, drift_vt_tls_init_once);
	pthread_setspecific(drift_vt_tls_key, vt);
}

static DriftVt *drift_vt_tls_get(void) {
	pthread_once(&drift_vt_tls_once, drift_vt_tls_init_once);
	return (DriftVt *)pthread_getspecific(drift_vt_tls_key);
}

static void *drift_exec_worker(void *arg) {
	DriftExec *exec = (DriftExec *)arg;
	drift_exec_tls = exec;
#ifdef __linux__
	ucontext_t sched_ctx;
	drift_sched_ctx = &sched_ctx;
#endif
	while (1) {
		pthread_mutex_lock(&exec->mu);
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
			vt->park_token++;
			pthread_cond_broadcast(&vt->cv);
			pthread_mutex_unlock(&vt->mu);
			atomic_fetch_sub(&exec->running, 1);
			continue;
		}
		atomic_store(&vt->started, 1);
		if (atomic_load(&vt->cancelled)) {
			atomic_store(&vt->state, DRIFT_VT_CANCELLED);
			pthread_mutex_lock(&vt->mu);
			if (!atomic_exchange(&vt->completed, 1)) {
				drift_drop_callback(&vt->cb);
			}
			vt->park_token++;
			pthread_cond_broadcast(&vt->cv);
			pthread_mutex_unlock(&vt->mu);
			atomic_fetch_sub(&exec->running, 1);
			continue;
		}
		atomic_store(&vt->state, DRIFT_VT_RUNNING);
#ifdef __linux__
		if (!vt->ctx_ready) {
			getcontext(&vt->ctx);
			vt->ctx.uc_link = &sched_ctx;
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
			vt->ctx.uc_stack.ss_sp = vt->stack;
			vt->ctx.uc_stack.ss_size = vt->stack_size;
			vt->ctx_ready = 1;
			makecontext(&vt->ctx, (void (*)())drift_vt_fiber_entry, 1, (uintptr_t)vt);
		}
		drift_vt_tls_set(vt);
		swapcontext(&sched_ctx, &vt->ctx);
		drift_vt_tls_set(NULL);
		int state = atomic_load(&vt->state);
			if ((state == DRIFT_VT_FINISHED) || (state == DRIFT_VT_CANCELLED)) {
				if (vt->stack) {
					VALGRIND_STACK_DEREGISTER(vt->valgrind_stack_id);
					drift_fiber_stack_free(vt->stack, vt->stack_size, vt->stack_is_mmap);
					vt->stack = NULL;
					vt->stack_size = 0;
				}
				pthread_mutex_lock(&vt->mu);
				atomic_store(&vt->completed, 1);
				int dropped_after_finish = atomic_load(&vt->dropped);
				vt->park_token++;
				pthread_cond_broadcast(&vt->cv);
				pthread_mutex_unlock(&vt->mu);
				if (dropped_after_finish) {
					drift_vt_destroy(vt);
				}
			}
#else
		drift_vt_tls_set(vt);
		drift_run_callback(&vt->cb, 0);
		atomic_store(&vt->state, DRIFT_VT_FINISHED);
			pthread_mutex_lock(&vt->mu);
			atomic_store(&vt->completed, 1);
			int dropped_after_finish = atomic_load(&vt->dropped);
			vt->park_token++;
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

#ifdef __linux__
static void *drift_reactor_thread_entry(void *arg) {
	Reactor *r = (Reactor *)arg;
	struct epoll_event events[16];
	while (1) {
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
		atomic_store_explicit(&r->in_wait, 1, memory_order_relaxed);
		pthread_mutex_unlock(&r->mu);

		int n = epoll_wait(r->epoll_fd, events, 16, timeout_ms);
		atomic_store_explicit(&r->in_wait, 0, memory_order_relaxed);
		if (n < 0 && errno != EINTR) {
			continue;
		}
		if (n > 0) {
			for (int i = 0; i < n; i++) {
				int fd = events[i].data.fd;
				if (fd == r->wake_fd) {
					uint64_t buf;
					while (read(r->wake_fd, &buf, sizeof(buf)) > 0) { }
					continue;
				}
				pthread_mutex_lock(&r->mu);
				ReactorWatch *w = drift_reactor_find_watch(r, fd);
				uint64_t vt = w ? w->vt : 0;
				if (w) { w->vt = 0; w->events = 0; }
				/* I/O completed — cancel the timeout timer(s) for this VT
				 * to prevent unbounded timer list growth.  Without this,
				 * stale timers accumulate O(n) with I/O ops and the
				 * reactor's O(n) scans become O(n²) total. */
				if (vt != 0) {
					ReactorTimer *tp = NULL;
					ReactorTimer *tc = r->timers;
					while (tc) {
						ReactorTimer *tn = tc->next;
						if (tc->vt == vt) {
							if (tp) tp->next = tn; else r->timers = tn;
							free(tc);
						} else {
							tp = tc;
						}
						tc = tn;
					}
				}
				pthread_mutex_unlock(&r->mu);
				if (w && r->epoll_fd >= 0) {
					struct epoll_event ev; ev.events = 0; ev.data.fd = fd;
					epoll_ctl(r->epoll_fd, EPOLL_CTL_MOD, fd, &ev);
				}
				if (vt != 0) {
					drift_thread_unpark(vt);
				}
			}
		}

		pthread_mutex_lock(&r->mu);
		ReactorTimer *ready = NULL;
		drift_reactor_collect_timers(r, drift_now_ms(), &ready);
		pthread_mutex_unlock(&r->mu);
		while (ready) {
			ReactorTimer *next = ready->next;
			if (ready->vt != 0) {
				drift_thread_unpark(ready->vt);
			}
			free(ready);
			ready = next;
		}
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
	if (pthread_create(&r->thread, NULL, drift_reactor_thread_entry, r) == 0) {
		r->thread_started = 1;
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
	drift_reactor_wake(r);
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
				vt->park_token++;
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
	if (drift_sched_ctx) {
		swapcontext(&vt->ctx, drift_sched_ctx);
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
	drift_vt_registry_add(vt);
	return (uint64_t)vt;
}

void drift_thread_join(uint64_t vt) {
	DriftVt *h = (DriftVt *)vt;
	if (h == NULL) {
		return;
	}
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
		pthread_mutex_lock(&h->mu);
		pthread_mutex_unlock(&h->mu);
		drift_vt_destroy(h);
		return 0;
	}
	if (atomic_load(&h->cancelled)) {
		return 1;
	}
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

void drift_thread_park(uint64_t reason) {
	(void)reason;
	DriftVt *vt = drift_vt_tls_get();
	if (!vt) {
		sched_yield();
		return;
	}
	if (atomic_load(&vt->cancelled)) {
		return;
	}
#ifdef __linux__
	if (drift_sched_ctx) {
		if (vt->park_token > 0) {
			vt->park_token--;
			atomic_store(&vt->state, DRIFT_VT_RUNNING);
			return;
		}
		atomic_store(&vt->state, DRIFT_VT_PARKED);
		if (vt->park_token > 0) {
			vt->park_token--;
			atomic_store(&vt->state, DRIFT_VT_RUNNING);
			return;
		}
		swapcontext(&vt->ctx, drift_sched_ctx);
		atomic_store(&vt->state, DRIFT_VT_RUNNING);
		return;
	}
#endif
	pthread_mutex_lock(&vt->mu);
	atomic_store(&vt->state, DRIFT_VT_PARKED);
	while (vt->park_token == 0 && !atomic_load(&vt->cancelled)) {
		pthread_cond_wait(&vt->cv, &vt->mu);
	}
	if (vt->park_token > 0) {
		vt->park_token--;
	}
	atomic_store(&vt->state, DRIFT_VT_RUNNING);
	pthread_mutex_unlock(&vt->mu);
}

void drift_thread_park_until(int64_t deadline_ms) {
	DriftVt *vt = drift_vt_tls_get();
	if (deadline_ms <= 0) {
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
		return;
	}
#ifdef __linux__
	if (drift_sched_ctx) {
		if (vt->park_token > 0) {
			vt->park_token--;
			atomic_store(&vt->state, DRIFT_VT_RUNNING);
			return;
		}
		atomic_store(&vt->state, DRIFT_VT_PARKED);
		drift_reactor_register_timer((uint64_t)deadline_ms, (uint64_t)vt);
		if (vt->park_token > 0) {
			vt->park_token--;
			atomic_store(&vt->state, DRIFT_VT_RUNNING);
			return;
		}
		swapcontext(&vt->ctx, drift_sched_ctx);
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
	atomic_store(&vt->state, DRIFT_VT_PARKED);
	while (vt->park_token == 0 && !atomic_load(&vt->cancelled)) {
		int rc = pthread_cond_timedwait(&vt->cv, &vt->mu, &ts);
		if (rc == ETIMEDOUT) {
			break;
		}
	}
	if (vt->park_token > 0) {
		vt->park_token--;
	}
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
	if (atomic_load(&h->state) == DRIFT_VT_PARKED && h->exec) {
		atomic_store(&h->state, DRIFT_VT_READY);
		pthread_mutex_lock(&h->exec->mu);
		drift_exec_enqueue(h->exec, h);
		pthread_mutex_unlock(&h->exec->mu);
		return;
	}
	atomic_store(&h->state, DRIFT_VT_READY);
	pthread_mutex_lock(&h->mu);
	h->park_token++;
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
	atomic_store(&h->state, DRIFT_VT_READY);
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
	return 0;
}

uint64_t drift_time_now_ms(void) {
	int64_t now = drift_now_ms();
	if (now < 0) {
		return 0;
	}
	return (uint64_t)now;
}

uint64_t drift_time_now_utc_ms(void) {
	struct timespec ts;
	if (clock_gettime(CLOCK_REALTIME, &ts) != 0) {
		return 0;
	}
	int64_t out = (int64_t)(ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL);
	if (out < 0) {
		return 0;
	}
	return (uint64_t)out;
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
		h->park_token++;
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
	pthread_mutex_lock(&h->mu);
	h->park_token++;
	pthread_cond_broadcast(&h->cv);
	pthread_mutex_unlock(&h->mu);
	if (!atomic_load(&h->started)) {
		atomic_store(&h->state, DRIFT_VT_CANCELLED);
		if (h->exec == NULL) {
			pthread_mutex_lock(&h->mu);
			if (!atomic_exchange(&h->completed, 1)) {
				drift_drop_callback(&h->cb);
			}
			h->park_token++;
			pthread_cond_broadcast(&h->cv);
			pthread_mutex_unlock(&h->mu);
		}
	}
	return 0;
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
	ReactorWatch *prev = NULL;
	ReactorWatch *cur = r->watches;
	while (cur) {
		if (cur->fd == fd) {
			if (prev) prev->next = cur->next;
			else r->watches = cur->next;
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
		w->events = (uint32_t)interest;
		w->vt = vt;
		w->next = r->watches;
		r->watches = w;
	} else {
		w->events = (uint32_t)interest;
		w->vt = vt;
	}
	pthread_mutex_unlock(&r->mu);
	if (r->epoll_fd >= 0) {
		struct epoll_event ev;
		ev.events = (uint32_t)interest;
		ev.data.fd = (int)fd;
		if (existed) {
			if (epoll_ctl(r->epoll_fd, EPOLL_CTL_MOD, (int)fd, &ev) != 0 && errno == ENOENT) {
				epoll_ctl(r->epoll_fd, EPOLL_CTL_ADD, (int)fd, &ev);
			}
		} else {
			epoll_ctl(r->epoll_fd, EPOLL_CTL_ADD, (int)fd, &ev);
		}
	}
	if (deadline_ms > 0) {
		drift_reactor_register_timer(deadline_ms, vt);
	}
#else
	(void)fd;
	(void)interest;
	(void)vt;
	(void)deadline_ms;
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
