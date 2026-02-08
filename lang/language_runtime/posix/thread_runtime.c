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
#include <ctype.h>
#ifdef __linux__
#include <ucontext.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/timerfd.h>
#include <unistd.h>
#endif

#include "string_runtime.h"
#include "../../compiler_infra/diagnostic_runtime.h"

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

typedef struct DriftVt {
	DriftIface cb;
	pthread_t thread;
	// Written by the worker thread when it begins execution.
	// Other threads may read it; keep single-writer invariant unless protected.
	atomic_int started;
	atomic_int completed;
	atomic_int cancelled;
	atomic_int state;
	// Stack is allocated/freed by the worker thread (single owner).
	void *stack;
	size_t stack_size;
#ifdef __linux__
	ucontext_t ctx;
	// Context is initialized once by the worker thread (single-writer).
	int ctx_ready;
#endif
	struct DriftExec *exec;
	pthread_mutex_t mu;
	pthread_cond_t cv;
	int park_token;
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
static pthread_once_t drift_vt_tls_once = PTHREAD_ONCE_INIT;
static pthread_key_t drift_vt_tls_key;
static pthread_once_t drift_exec_once = PTHREAD_ONCE_INIT;
static pthread_once_t drift_log_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t drift_log_mu;
static pthread_mutex_t drift_log_cv_mu;
static pthread_cond_t drift_log_cv;
static pthread_t drift_log_worker;
static _Atomic int64_t drift_log_inited = 0;
static _Atomic int64_t drift_log_worker_started = 0;
static _Atomic int64_t drift_log_worker_shutdown = 0;
static _Atomic int64_t drift_log_min_level = 1;
static _Atomic int64_t drift_log_queue_capacity = 1024;
static _Atomic int64_t drift_log_backpressure = 0;
static _Atomic int64_t drift_log_write_timeout_ms = 1000;
static _Atomic int64_t drift_log_enqueue_timeout_ms = 1000;
static _Atomic int64_t drift_log_queue_depth = 0;
static _Atomic int64_t drift_log_inflight = 0;
static _Atomic int64_t drift_log_dropped_oldest = 0;
static _Atomic int64_t drift_log_dropped_newest = 0;
typedef struct DriftLogRecordSlot {
	_Atomic int state; // 0 empty, 2 ready
	int64_t level;
	DriftString payload_json;
} DriftLogRecordSlot;
static DriftLogRecordSlot *drift_log_slots = NULL;
static _Atomic uint64_t drift_log_write_seq = 0;
static _Atomic uint64_t drift_log_read_seq = 0;

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
} Reactor;

static Reactor *drift_default_reactor_ptr = NULL;

static void drift_run_callback(DriftIface *cb, int do_free);
static void drift_drop_callback(DriftIface *cb);
#ifdef __linux__
static void drift_vt_fiber_entry(uintptr_t arg);
#endif
uint64_t drift_log_runtime_init(int64_t min_level, int64_t queue_capacity, int64_t backpressure_policy, int64_t write_timeout_ms, int64_t enqueue_timeout_ms);
void drift_thread_unpark(uint64_t vt);
void drift_reactor_register_timer(uint64_t deadline_ms, uint64_t vt);

typedef struct ExecNode {
	DriftVt *vt;
	struct ExecNode *next;
} ExecNode;

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
} DriftExec;

static __thread DriftExec *drift_exec_tls = NULL;
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

static DriftString drift_log_string_from_cbuf(const char *buf, size_t len) {
	DriftString out = drift_string_from_utf8_bytes(buf, (drift_isize)len);
	return out;
}

static DriftString drift_log_json_escape_impl(DriftString s) {
	if (s.data == NULL || s.len <= 0) {
		return drift_string_from_cstr("");
	}
	size_t n = (size_t)s.len;
	size_t max_out = n * 6 + 1;
	char *buf = (char *)malloc(max_out);
	if (buf == NULL) {
		return drift_string_from_cstr("");
	}
	size_t out = 0;
	for (size_t i = 0; i < n; i++) {
		unsigned char c = (unsigned char)s.data[i];
		if (c == '\"' || c == '\\') {
			buf[out++] = '\\';
			buf[out++] = (char)c;
			continue;
		}
		if (c == '\n') {
			buf[out++] = '\\';
			buf[out++] = 'n';
			continue;
		}
		if (c == '\r') {
			buf[out++] = '\\';
			buf[out++] = 'r';
			continue;
		}
		if (c == '\t') {
			buf[out++] = '\\';
			buf[out++] = 't';
			continue;
		}
		if (c < 0x20) {
			(void)snprintf(buf + out, max_out - out, "\\u%04x", (unsigned int)c);
			out += 6;
			continue;
		}
		buf[out++] = (char)c;
	}
	DriftString escaped = drift_log_string_from_cbuf(buf, out);
	free(buf);
	return escaped;
}

static DriftString drift_log_dv_to_json_impl(struct DriftDiagnosticValue dv) {
	switch ((int)dv.tag) {
		case DV_BOOL: {
			uint8_t b = 0;
			if (drift_dv_as_bool(&dv, &b)) {
				return drift_string_from_cstr(b ? "true" : "false");
			}
			return drift_string_from_cstr("false");
		}
		case DV_INT: {
			drift_isize iv = 0;
			if (drift_dv_as_int(&dv, &iv)) {
				return drift_string_from_int64((int64_t)iv);
			}
			return drift_string_from_cstr("0");
		}
		case DV_FLOAT: {
			double fv = 0.0;
			if (drift_dv_as_float(&dv, &fv)) {
				return drift_string_from_f64(fv);
			}
			return drift_string_from_cstr("0.0");
		}
		case DV_STRING: {
			struct DriftString sv = {0, NULL};
			if (!drift_dv_as_string(&dv, &sv)) {
				return drift_string_from_cstr("\"\"");
			}
			DriftString escaped = drift_log_json_escape_impl((DriftString){sv.len, sv.data});
			DriftString out = drift_string_concat(drift_string_from_cstr("\""), escaped);
			out = drift_string_concat(out, drift_string_from_cstr("\""));
			return out;
		}
		case DV_NULL:
		case DV_MISSING:
		default:
			return drift_string_from_cstr("null");
	}
}

DriftString drift_log_runtime_json_escape(DriftString s) {
	return drift_log_json_escape_impl(s);
}

DriftString drift_log_runtime_dv_to_json(const struct DriftDiagnosticValue *dv) {
	if (dv == NULL) {
		return drift_string_from_cstr("null");
	}
	return drift_log_dv_to_json_impl(*dv);
}

static const char *drift_log_level_text(int64_t level) {
	if (level <= 0) {
		return "debug";
	}
	if (level == 1) {
		return "info";
	}
	return "error";
}

static void drift_log_emit_record_json(int64_t level, DriftString payload_json) {
	struct timespec ts;
	(void)clock_gettime(CLOCK_REALTIME, &ts);
	time_t sec = ts.tv_sec;
	struct tm tm_utc;
	(void)gmtime_r(&sec, &tm_utc);
	char tm_buf[40];
	size_t tm_len = strftime(tm_buf, sizeof(tm_buf), "%Y-%m-%dT%H:%M:%S", &tm_utc);
	int millis = (int)(ts.tv_nsec / 1000000L);
	unsigned long tid = (unsigned long)pthread_self();
	fprintf(stderr, "{\"tm\":\"%.*s.%03dZ\",\"level\":\"%s\",", (int)tm_len, tm_buf, millis, drift_log_level_text(level));
	if (payload_json.data != NULL && payload_json.len > 0) {
		fwrite(payload_json.data, 1, (size_t)payload_json.len, stderr);
	}
	fprintf(stderr, ",\"tid\":%lu}\n", tid);
	fflush(stderr);
}

static void *drift_log_worker_main(void *arg) {
	(void)arg;
	while (atomic_load(&drift_log_worker_shutdown) == 0) {
		int64_t depth = atomic_load(&drift_log_queue_depth);
		if (depth <= 0) {
			pthread_mutex_lock(&drift_log_cv_mu);
			if (atomic_load(&drift_log_queue_depth) == 0 && atomic_load(&drift_log_worker_shutdown) == 0) {
				pthread_cond_wait(&drift_log_cv, &drift_log_cv_mu);
			}
			pthread_mutex_unlock(&drift_log_cv_mu);
			continue;
		}
		if (atomic_fetch_sub(&drift_log_queue_depth, 1) <= 0) {
			atomic_fetch_add(&drift_log_queue_depth, 1);
			sched_yield();
			continue;
		}
		uint64_t seq = atomic_fetch_add(&drift_log_read_seq, 1);
		DriftLogRecordSlot *slot = &drift_log_slots[seq % (uint64_t)atomic_load(&drift_log_queue_capacity)];
		while (atomic_load(&slot->state) != 2) {
			sched_yield();
		}
		int64_t level = slot->level;
		DriftString payload_json = slot->payload_json;
		slot->level = 0;
		atomic_store(&slot->state, 0);
		atomic_fetch_add(&drift_log_inflight, 1);
		drift_log_emit_record_json(level, payload_json);
		drift_string_release(payload_json);
		atomic_fetch_sub(&drift_log_inflight, 1);
		pthread_mutex_lock(&drift_log_cv_mu);
		pthread_cond_broadcast(&drift_log_cv);
		pthread_mutex_unlock(&drift_log_cv_mu);
	}
	return NULL;
}

static void drift_log_init_once(void) {
	pthread_mutex_init(&drift_log_mu, NULL);
	pthread_mutex_init(&drift_log_cv_mu, NULL);
	pthread_cond_init(&drift_log_cv, NULL);
}

static void drift_log_ensure_defaults(void) {
	pthread_once(&drift_log_once, drift_log_init_once);
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
		if (node) {
			exec->head = node->next;
			if (!exec->head) {
				exec->tail = NULL;
			}
			exec->queue_len--;
		}
		pthread_mutex_unlock(&exec->mu);
		if (!node) {
			continue;
		}
		DriftVt *vt = node->vt;
		free(node);
		if (!vt) {
			continue;
		}
		if (atomic_load(&vt->cancelled) && !atomic_load(&vt->started)) {
			atomic_store(&vt->state, DRIFT_VT_CANCELLED);
			drift_drop_callback(&vt->cb);
			atomic_store(&vt->completed, 1);
			pthread_mutex_lock(&vt->mu);
			vt->park_token++;
			pthread_cond_broadcast(&vt->cv);
			pthread_mutex_unlock(&vt->mu);
			continue;
		}
		atomic_store(&vt->started, 1);
		if (atomic_load(&vt->cancelled)) {
			atomic_store(&vt->state, DRIFT_VT_CANCELLED);
			if (!atomic_exchange(&vt->completed, 1)) {
				drift_drop_callback(&vt->cb);
				pthread_mutex_lock(&vt->mu);
				vt->park_token++;
				pthread_cond_broadcast(&vt->cv);
				pthread_mutex_unlock(&vt->mu);
			}
			continue;
		}
		atomic_store(&vt->state, DRIFT_VT_RUNNING);
		atomic_fetch_add(&exec->running, 1);
#ifdef __linux__
		if (!vt->ctx_ready) {
			getcontext(&vt->ctx);
			vt->ctx.uc_link = &sched_ctx;
			vt->stack_size = exec->stack_bytes;
			vt->stack = malloc(vt->stack_size);
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
				free(vt->stack);
				vt->stack = NULL;
				vt->stack_size = 0;
			}
		}
#else
		drift_vt_tls_set(vt);
		drift_run_callback(&vt->cb, 0);
		atomic_store(&vt->state, DRIFT_VT_FINISHED);
		atomic_store(&vt->completed, 1);
		pthread_mutex_lock(&vt->mu);
		vt->park_token++;
		pthread_cond_broadcast(&vt->cv);
		pthread_mutex_unlock(&vt->mu);
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
		pthread_mutex_unlock(&r->mu);

		int n = epoll_wait(r->epoll_fd, events, 16, timeout_ms);
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
				ReactorWatch *w = drift_reactor_take_watch(r, fd);
				uint64_t vt = w ? w->vt : 0;
				pthread_mutex_unlock(&r->mu);
				if (w) {
					free(w);
				}
				if (r->epoll_fd >= 0) {
					epoll_ctl(r->epoll_fd, EPOLL_CTL_DEL, fd, NULL);
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
	pthread_create(&r->thread, NULL, drift_reactor_thread_entry, r);
#endif
	return r;
}

static void drift_reactor_init_once(void) {
	drift_default_reactor_ptr = drift_reactor_create();
	if (drift_default_reactor_ptr) {
		atomic_store(&drift_default_reactor, (uint64_t)drift_default_reactor_ptr);
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
	exec->threads_count = (int)max_threads;
	exec->threads = (pthread_t *)calloc((size_t)exec->threads_count, sizeof(pthread_t));
	if (!exec->threads) {
		free(exec);
		return NULL;
	}
	for (int i = 0; i < exec->threads_count; i++) {
		pthread_create(&exec->threads[i], NULL, drift_exec_worker, exec);
	}
	return exec;
}

static void drift_exec_init_once(void) {
	DriftExec *exec = drift_exec_create_internal(1, 1, 0, 262144);
	if (exec) {
		atomic_store(&drift_default_executor, (uint64_t)exec);
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
	atomic_store(&vt->completed, 1);
	pthread_mutex_lock(&vt->mu);
	vt->park_token++;
	pthread_cond_broadcast(&vt->cv);
	pthread_mutex_unlock(&vt->mu);
	drift_vt_tls_set(NULL);
	if (drift_sched_ctx) {
		swapcontext(&vt->ctx, drift_sched_ctx);
	}
}
#endif

static void drift_exec_enqueue(DriftExec *exec, DriftVt *vt) {
	ExecNode *node = (ExecNode *)malloc(sizeof(ExecNode));
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
	atomic_store(&vt->state, DRIFT_VT_NEW);
	vt->stack = NULL;
	vt->stack_size = 0;
#ifdef __linux__
	vt->ctx_ready = 0;
#endif
	vt->exec = NULL;
	vt->thread = (pthread_t)0;
	pthread_mutex_init(&vt->mu, NULL);
	pthread_cond_init(&vt->cv, NULL);
	vt->park_token = 0;
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
	pthread_mutex_destroy(&h->mu);
	pthread_cond_destroy(&h->cv);
	free(h);
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
		pthread_mutex_destroy(&h->mu);
		pthread_cond_destroy(&h->cv);
		if (h->stack) {
			free(h->stack);
		}
		free(h);
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
	pthread_mutex_destroy(&h->mu);
	pthread_cond_destroy(&h->cv);
	free(h);
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
	h->exec = ex;
	pthread_mutex_lock(&ex->mu);
	if (ex->queue_limit > 0) {
		int running = atomic_load(&ex->running);
		int64_t total = ex->queue_len + (int64_t)running;
		if (total >= ex->queue_limit) {
			pthread_mutex_unlock(&ex->mu);
			return 1;
		}
	}
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

uint64_t drift_log_runtime_init(int64_t min_level, int64_t queue_capacity, int64_t backpressure_policy, int64_t write_timeout_ms, int64_t enqueue_timeout_ms) {
	drift_log_ensure_defaults();
	if (queue_capacity <= 0) {
		queue_capacity = 1;
	}
	pthread_mutex_lock(&drift_log_mu);
	if (atomic_load(&drift_log_inited) == 0) {
		drift_log_slots = (DriftLogRecordSlot *)calloc((size_t)queue_capacity, sizeof(DriftLogRecordSlot));
		if (drift_log_slots == NULL) {
			pthread_mutex_unlock(&drift_log_mu);
			return 0;
		}
		for (int64_t i = 0; i < queue_capacity; i++) {
			atomic_store(&drift_log_slots[i].state, 0);
		}
		atomic_store(&drift_log_min_level, min_level);
		atomic_store(&drift_log_queue_capacity, queue_capacity);
		atomic_store(&drift_log_backpressure, backpressure_policy);
		atomic_store(&drift_log_write_timeout_ms, write_timeout_ms);
		atomic_store(&drift_log_enqueue_timeout_ms, enqueue_timeout_ms);
		atomic_store(&drift_log_queue_depth, 0);
		atomic_store(&drift_log_dropped_oldest, 0);
		atomic_store(&drift_log_dropped_newest, 0);
		atomic_store(&drift_log_write_seq, 0);
		atomic_store(&drift_log_read_seq, 0);
		atomic_store(&drift_log_inited, 1);
		if (atomic_load(&drift_log_worker_started) == 0) {
			if (pthread_create(&drift_log_worker, NULL, drift_log_worker_main, NULL) == 0) {
				atomic_store(&drift_log_worker_started, 1);
			}
		}
		pthread_mutex_unlock(&drift_log_mu);
		return 1;
	}
	if (
		atomic_load(&drift_log_min_level) == min_level
		&& atomic_load(&drift_log_queue_capacity) == queue_capacity
		&& atomic_load(&drift_log_backpressure) == backpressure_policy
		&& atomic_load(&drift_log_write_timeout_ms) == write_timeout_ms
		&& atomic_load(&drift_log_enqueue_timeout_ms) == enqueue_timeout_ms
	) {
		pthread_mutex_unlock(&drift_log_mu);
		return 1;
	}
	pthread_mutex_unlock(&drift_log_mu);
	return 0;
}

uint64_t drift_log_runtime_min_level(void) {
	drift_log_ensure_defaults();
	int64_t out = atomic_load(&drift_log_min_level);
	return (uint64_t)out;
}

uint64_t drift_log_runtime_enqueue(int64_t level, int64_t logger_min_level, int64_t enqueue_timeout_ms, DriftString payload_json) {
	drift_log_ensure_defaults();
	if (atomic_load(&drift_log_inited) == 0) {
		if (drift_log_runtime_init(1, 1024, 0, 1000, 1000) == 0) {
			drift_string_release(payload_json);
			return 0;
		}
	}
	if (level < logger_min_level) {
		drift_string_release(payload_json);
		return 0;
	}
	int64_t cap = atomic_load(&drift_log_queue_capacity);
	if (cap <= 0) {
		cap = 1;
	}
	while (1) {
		int64_t depth = atomic_load(&drift_log_queue_depth);
		if (depth < cap) {
			if (atomic_compare_exchange_weak(&drift_log_queue_depth, &depth, depth + 1)) {
				uint64_t seq = atomic_fetch_add(&drift_log_write_seq, 1);
				DriftLogRecordSlot *slot = &drift_log_slots[seq % (uint64_t)cap];
				while (atomic_load(&slot->state) != 0) {
					sched_yield();
				}
				slot->level = level;
				slot->payload_json = payload_json;
				atomic_store(&slot->state, 2);
				pthread_mutex_lock(&drift_log_cv_mu);
				pthread_cond_signal(&drift_log_cv);
				pthread_mutex_unlock(&drift_log_cv_mu);
				return 1;
			}
			continue;
		}
		int64_t policy = atomic_load(&drift_log_backpressure);
		if (policy == 1) {
			atomic_fetch_add(&drift_log_dropped_oldest, 1);
			drift_string_release(payload_json);
			return 1;
		}
		if (policy == 2) {
			atomic_fetch_add(&drift_log_dropped_newest, 1);
			drift_string_release(payload_json);
			return 0;
		}
		int64_t timeout_ms = enqueue_timeout_ms;
		if (timeout_ms <= 0) {
			timeout_ms = atomic_load(&drift_log_enqueue_timeout_ms);
		}
		if (timeout_ms <= 0) {
			return 0;
		}
		int64_t deadline_ms = drift_now_ms() + timeout_ms;
		pthread_mutex_lock(&drift_log_cv_mu);
		while (atomic_load(&drift_log_queue_depth) >= cap) {
			int64_t now_ms = drift_now_ms();
			if (now_ms >= deadline_ms) {
				pthread_mutex_unlock(&drift_log_cv_mu);
				drift_string_release(payload_json);
				return 0;
			}
			int64_t remain_ms = deadline_ms - now_ms;
			struct timespec ts;
			if (clock_gettime(CLOCK_REALTIME, &ts) != 0) {
				pthread_mutex_unlock(&drift_log_cv_mu);
				drift_string_release(payload_json);
				return 0;
			}
			ts.tv_sec += (time_t)(remain_ms / 1000);
			ts.tv_nsec += (long)((remain_ms % 1000) * 1000000L);
			if (ts.tv_nsec >= 1000000000L) {
				ts.tv_sec += 1;
				ts.tv_nsec -= 1000000000L;
			}
			(void)pthread_cond_timedwait(&drift_log_cv, &drift_log_cv_mu, &ts);
		}
		pthread_mutex_unlock(&drift_log_cv_mu);
	}
}

uint64_t drift_log_runtime_flush(int64_t timeout_ms) {
	drift_log_ensure_defaults();
	if (timeout_ms < 0) {
		return 0;
	}
	int64_t deadline_ms = drift_now_ms() + timeout_ms;
	pthread_mutex_lock(&drift_log_cv_mu);
	while (1) {
		int64_t depth = atomic_load(&drift_log_queue_depth);
		int64_t inflight = atomic_load(&drift_log_inflight);
		if (depth == 0 && inflight == 0) {
			pthread_mutex_unlock(&drift_log_cv_mu);
			return 1;
		}
		if (timeout_ms == 0) {
			pthread_mutex_unlock(&drift_log_cv_mu);
			return 0;
		}
		int64_t now_ms = drift_now_ms();
		if (now_ms >= deadline_ms) {
			pthread_mutex_unlock(&drift_log_cv_mu);
			return 0;
		}
		int64_t remain_ms = deadline_ms - now_ms;
		struct timespec ts;
		if (clock_gettime(CLOCK_REALTIME, &ts) != 0) {
			pthread_mutex_unlock(&drift_log_cv_mu);
			return 0;
		}
		ts.tv_sec += (time_t)(remain_ms / 1000);
		ts.tv_nsec += (long)((remain_ms % 1000) * 1000000L);
		if (ts.tv_nsec >= 1000000000L) {
			ts.tv_sec += 1;
			ts.tv_nsec -= 1000000000L;
		}
		(void)pthread_cond_timedwait(&drift_log_cv, &drift_log_cv_mu, &ts);
	}
}

void drift_exec_submit_test_override(int64_t code) {
	drift_exec_submit_override = code;
}

void drift_thread_drop(uint64_t vt) {
	DriftVt *h = (DriftVt *)vt;
	if (!h) {
		return;
	}
	if (!atomic_load(&h->started)) {
		atomic_store(&h->state, DRIFT_VT_CANCELLED);
		drift_drop_callback(&h->cb);
	}
	pthread_mutex_destroy(&h->mu);
	pthread_cond_destroy(&h->cv);
	free(h);
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
		drift_drop_callback(&h->cb);
		atomic_store(&h->completed, 1);
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
	drift_default_reactor = reactor;
	if (reactor != 0) {
		drift_default_reactor_ptr = (Reactor *)reactor;
	}
}

void drift_reactor_register_io(uint64_t fd, uint64_t interest, uint64_t vt, uint64_t deadline_ms) {
#ifdef __linux__
	Reactor *r = drift_default_reactor_ptr;
	if (vt != 0) {
		DriftVt *h = (DriftVt *)vt;
		int st = atomic_load(&h->state);
		if (st == DRIFT_VT_FINISHED || st == DRIFT_VT_CANCELLED) {
			return;
		}
	}
	if (!r) {
		r = drift_reactor_create();
		drift_default_reactor_ptr = r;
		atomic_store(&drift_default_reactor, (uint64_t)r);
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
		int op = existed ? EPOLL_CTL_MOD : EPOLL_CTL_ADD;
		epoll_ctl(r->epoll_fd, op, (int)fd, &ev);
	}
	if (deadline_ms > 0) {
		drift_reactor_register_timer(deadline_ms, vt);
	}
	drift_reactor_wake(r);
#else
	(void)fd;
	(void)interest;
	(void)vt;
	(void)deadline_ms;
#endif
}

void drift_reactor_register_timer(uint64_t deadline_ms, uint64_t vt) {
	Reactor *r = drift_default_reactor_ptr;
	if (vt != 0) {
		DriftVt *h = (DriftVt *)vt;
		int st = atomic_load(&h->state);
		if (st == DRIFT_VT_FINISHED || st == DRIFT_VT_CANCELLED) {
			return;
		}
	}
	if (!r) {
		r = drift_reactor_create();
		drift_default_reactor_ptr = r;
		atomic_store(&drift_default_reactor, (uint64_t)r);
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
