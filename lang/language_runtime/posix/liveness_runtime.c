/* Runtime liveness interrogator (Slice 1): formatting + dedicated thread.
 *
 * Collection of scheduler state lives in thread_runtime.c (which owns the
 * VT/exec/reactor structs).  This file owns the parts that must stay
 * independent of any potentially-wedged subsystem: a dedicated sigwait thread
 * for SIGUSR2, and emit paths that use only raw open(2)/write(2)/dprintf —
 * never Drift IO, app loggers, the reactor, or callbacks. */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "posix/liveness_runtime.h"

#include <stdarg.h>

/* dprintf wrapper that records a write failure (ENOSPC/EIO/partial) into *err
 * so the JSON emitter can report success/failure honestly instead of assuming
 * the file was written. */
static void lv_dp(int fd, int *err, const char *fmt, ...) {
	va_list ap;
	va_start(ap, fmt);
	int n = vdprintf(fd, fmt, ap);
	va_end(ap);
	if (n < 0) {
		*err = -1;
	}
}

/* ---- small helpers ---------------------------------------------------- */

static const char *drift_liveness_reason_str(int reason) {
	switch (reason) {
		case DRIFT_LIVENESS_REASON_OPERATOR_SIGNAL:     return "operator_signal";
		case DRIFT_LIVENESS_REASON_WATCHDOG_NO_PROGRESS: return "watchdog_no_progress";
		default: return "unknown";
	}
}

/* Full state name, synthesizing PARKED_* from the wait kind. */
static const char *drift_liveness_state_str(int state, int wait_kind) {
	switch (state) {
		case DRIFT_LV_VT_NEW:       return "NEW";
		case DRIFT_LV_VT_READY:     return "READY";
		case DRIFT_LV_VT_RUNNING:   return "RUNNING";
		case DRIFT_LV_VT_FINISHED:  return "COMPLETED";
		case DRIFT_LV_VT_CANCELLED: return "CANCELLED";
		case DRIFT_LV_VT_PARKED:
			switch (wait_kind) {
				case DRIFT_WAIT_TIMER:   return "PARKED_TIMER";
				case DRIFT_WAIT_IO:      return "PARKED_IO";
				case DRIFT_WAIT_JOIN:    return "PARKED_JOIN";
				case DRIFT_WAIT_CONDVAR: return "PARKED_CONDVAR";
				case DRIFT_WAIT_CHANNEL: return "PARKED_CHANNEL";
				case DRIFT_WAIT_BLOCKING_ADMISSION: return "PARKED_BLOCKING_ADMISSION";
				default:                 return "PARKED";
			}
		default: return "UNKNOWN";
	}
}

static const char *drift_liveness_wait_str(int wait_kind) {
	switch (wait_kind) {
		case DRIFT_WAIT_TIMER:   return "timer";
		case DRIFT_WAIT_IO:      return "io";
		case DRIFT_WAIT_JOIN:    return "join";
		case DRIFT_WAIT_CONDVAR: return "condvar";
		case DRIFT_WAIT_CHANNEL: return "channel";
		case DRIFT_WAIT_BLOCKING_ADMISSION: return "blocking-admission";
		default:                 return "none";
	}
}

/* Resolve the JSON output path from env, substituting "%p" with the pid.
 * Precedence: DRIFT_LIVENESS_JSON_PATH > DRIFT_LIVENESS_DUMP_DIR > /tmp.
 * Returns 1 on success, 0 if the result would not fit. */
static int drift_liveness_resolve_json_path(char *buf, size_t cap, int pid) {
	const char *tmpl = getenv("DRIFT_LIVENESS_JSON_PATH");
	char built[768];
	if (!tmpl || tmpl[0] == '\0') {
		const char *dir = getenv("DRIFT_LIVENESS_DUMP_DIR");
		if (dir && dir[0] != '\0') {
			snprintf(built, sizeof(built), "%s/drift-runtime.%%p.liveness.json", dir);
		} else {
			snprintf(built, sizeof(built), "/tmp/drift-runtime.%%p.liveness.json");
		}
		tmpl = built;
	}

	size_t o = 0;
	for (size_t i = 0; tmpl[i] != '\0'; i++) {
		if (tmpl[i] == '%' && tmpl[i + 1] == 'p') {
			int n = snprintf(buf + o, (o < cap) ? cap - o : 0, "%d", pid);
			if (n < 0) return 0;
			o += (size_t)n;
			i++;  /* consume 'p' */
		} else {
			if (o + 1 >= cap) return 0;
			buf[o++] = tmpl[i];
		}
		if (o >= cap) return 0;
	}
	if (o >= cap) return 0;
	buf[o] = '\0';
	return 1;
}

static int drift_liveness_text_enabled(void) {
	const char *v = getenv("DRIFT_LIVENESS_TEXT");
	if (v && v[0] == '0' && v[1] == '\0') {
		return 0;
	}
	return 1;  /* default on */
}

/* ---- JSON emission ---------------------------------------------------- */

/* Emit a JSON-escaped string body (no surrounding quotes).  Executor
 * names, op labels, and FFI file paths are arbitrary user bytes — raw
 * %.*s with an embedded quote/backslash/control byte would corrupt the
 * document (review finding).  Bounded input (labels <= 48, names <= 32,
 * paths bounded by the site constant), so per-byte dprintf is fine at
 * snapshot rate. */
static void lv_json_escape(int fd, int *werr, const char *p, int len) {
	for (int i = 0; i < len; i++) {
		unsigned char c = (unsigned char)p[i];
		switch (c) {
			case '"':  lv_dp(fd, werr, "\\\""); break;
			case '\\': lv_dp(fd, werr, "\\\\"); break;
			case '\n': lv_dp(fd, werr, "\\n"); break;
			case '\r': lv_dp(fd, werr, "\\r"); break;
			case '\t': lv_dp(fd, werr, "\\t"); break;
			default:
				if (c < 0x20) {
					lv_dp(fd, werr, "\\u%04x", c);
				} else {
					lv_dp(fd, werr, "%c", c);
				}
		}
	}
}

static void lv_json_escape_cstr(int fd, int *werr, const char *p) {
	lv_json_escape(fd, werr, p, (int)strlen(p));
}

static int drift_liveness_write_json(int fd, const DriftLivenessSnapshot *s) {
	int werr = 0;
	lv_dp(fd, &werr,
		"{\n"
		"  \"schema\": \"drift.liveness.v1\",\n"
		"  \"pid\": %d,\n"
		"  \"uptime_ms\": %lld,\n"
		"  \"reason\": \"%s\",\n"
		"  \"progress_counter\": %llu,\n"
		"  \"now_ms\": %lld,\n",
		s->pid,
		(long long)s->uptime_ms,
		drift_liveness_reason_str(s->reason),
		(unsigned long long)s->progress_counter,
		(long long)s->now_ms);

	lv_dp(fd, &werr,
		"  \"executor\": {\"present\": %s, \"workers\": %d, \"ready_queue_len\": %lld, "
		"\"running\": %d, \"parked\": %d, \"completed\": %llu, \"shutting_down\": %s},\n",
		s->exec_present ? "true" : "false",
		s->exec_workers,
		(long long)s->exec_ready_queue_len,
		s->exec_running,
		s->tally_parked,
		(unsigned long long)s->exec_completed,
		s->exec_shutting_down ? "true" : "false");

	/* Per-executor snapshots (blocking-FFI observability). */
	lv_dp(fd, &werr, "  \"execs\": [");
	for (int i = 0; i < s->exec_count; i++) {
		const DriftExecSnapshot *e = &s->execs[i];
		lv_dp(fd, &werr, "%s\n    {\"id\": %llu, \"name\": ",
			(i == 0) ? "" : ",", (unsigned long long)e->id);
		if (e->name_len > 0) {
			lv_dp(fd, &werr, "\"");
			lv_json_escape(fd, &werr, e->name, e->name_len);
			lv_dp(fd, &werr, "\"");
		} else {
			lv_dp(fd, &werr, "null");
		}
		lv_dp(fd, &werr, ", \"queue_len\": %lld, \"running\": %d, "
			"\"queue_limit\": %lld, \"waiters\": %lld, \"workers\": %d, "
			"\"shutting_down\": %s}",
			(long long)e->queue_len, e->running,
			(long long)e->queue_limit, (long long)e->waiters,
			e->workers, e->shutting_down ? "true" : "false");
	}
	lv_dp(fd, &werr, "\n  ],\n");

	if (s->reactor_present) {
		lv_dp(fd, &werr,
			"  \"reactor\": {\"present\": true, \"fd_waiters\": %d, \"timers\": %d, \"next_deadline_ms\": ",
			s->reactor_fd_waiters, s->reactor_timers);
		if (s->reactor_next_deadline_ms >= 0) {
			lv_dp(fd, &werr, "%lld},\n", (long long)s->reactor_next_deadline_ms);
		} else {
			lv_dp(fd, &werr, "null},\n");
		}
	} else {
		lv_dp(fd, &werr, "  \"reactor\": {\"present\": false},\n");
	}

	lv_dp(fd, &werr,
		"  \"tallies\": {\"running\": %d, \"ready\": %d, \"parked\": %d, "
		"\"finished\": %d, \"cancelled\": %d, "
		"\"wait\": {\"timer\": %d, \"io\": %d, \"join\": %d, \"condvar\": %d, \"channel\": %d, \"blocking_admission\": %d}},\n",
		s->tally_running, s->tally_ready, s->tally_parked,
		s->tally_finished, s->tally_cancelled,
		s->tally_wait[DRIFT_WAIT_TIMER], s->tally_wait[DRIFT_WAIT_IO],
		s->tally_wait[DRIFT_WAIT_JOIN], s->tally_wait[DRIFT_WAIT_CONDVAR],
		s->tally_wait[DRIFT_WAIT_CHANNEL],
		s->tally_wait[DRIFT_WAIT_BLOCKING_ADMISSION]);

	lv_dp(fd, &werr, "  \"vts\": [");
	for (int i = 0; i < s->vt_count; i++) {
		const DriftVtSnapshot *v = &s->vts[i];
		int64_t age = (s->now_ms >= v->state_since_ms) ? (s->now_ms - v->state_since_ms) : 0;
		lv_dp(fd, &werr, "%s\n    {\"vtid\": %llu, \"state\": \"%s\", \"state_since_ms\": %lld, "
			"\"age_ms\": %lld, ",
			(i == 0) ? "" : ",",
			(unsigned long long)v->vtid,
			drift_liveness_state_str(v->state, v->wait_kind),
			(long long)v->state_since_ms,
			(long long)age);

		if (v->carrier_tid != 0) {
			lv_dp(fd, &werr, "\"carrier_tid\": %llu, ", (unsigned long long)v->carrier_tid);
		} else {
			lv_dp(fd, &werr, "\"carrier_tid\": null, ");
		}
		lv_dp(fd, &werr, "\"last_progress\": %llu, ", (unsigned long long)v->last_progress);

		/* wait object */
		lv_dp(fd, &werr, "\"wait\": {\"kind\": \"%s\"", drift_liveness_wait_str(v->wait_kind));
		if (v->wait_kind == DRIFT_WAIT_TIMER) {
			if (v->timer_deadline_ms >= 0) {
				lv_dp(fd, &werr, ", \"deadline_ms\": %lld", (long long)v->timer_deadline_ms);
			} else {
				lv_dp(fd, &werr, ", \"deadline_ms\": null");
			}
		} else if (v->wait_kind == DRIFT_WAIT_IO) {
			lv_dp(fd, &werr, ", \"fd\": %d, \"events\": %u", v->io_fd, v->io_events);
		} else if (v->wait_kind == DRIFT_WAIT_JOIN ||
		           v->wait_kind == DRIFT_WAIT_CONDVAR ||
		           v->wait_kind == DRIFT_WAIT_CHANNEL) {
			lv_dp(fd, &werr, ", \"id\": %llu", (unsigned long long)v->wait_id);
		} else if (v->wait_kind == DRIFT_WAIT_BLOCKING_ADMISSION) {
			/* wait_id carries the target executor's stable id; the
			 * TIMER correlation above already resolved the admission
			 * deadline when one is armed. */
			lv_dp(fd, &werr, ", \"exec_id\": %llu", (unsigned long long)v->wait_id);
			if (v->timer_deadline_ms >= 0) {
				lv_dp(fd, &werr, ", \"deadline_ms\": %lld", (long long)v->timer_deadline_ms);
			}
		}
		lv_dp(fd, &werr, "}");
		/* Blocking-FFI observability fields (absent when unset). */
		if (v->op_len > 0) {
			lv_dp(fd, &werr, ", \"op\": \"");
			lv_json_escape(fd, &werr, v->op_label, v->op_len);
			lv_dp(fd, &werr, "\"");
		}
		if (v->submitter_vtid != 0) {
			lv_dp(fd, &werr, ", \"submitter\": %llu", (unsigned long long)v->submitter_vtid);
		}
		if (v->exec_id != 0) {
			lv_dp(fd, &werr, ", \"exec_id\": %llu", (unsigned long long)v->exec_id);
		}
		if (v->ffi_symbol != NULL) {
			lv_dp(fd, &werr, ", \"ffi\": {\"symbol\": \"");
			lv_json_escape_cstr(fd, &werr, v->ffi_symbol);
			lv_dp(fd, &werr, "\", \"file\": \"");
			lv_json_escape_cstr(fd, &werr, v->ffi_file ? v->ffi_file : "<unknown>");
			lv_dp(fd, &werr, "\", \"line\": %lld}", (long long)v->ffi_line);
		}
		lv_dp(fd, &werr, ", \"logical_frame\": null}");
	}
	lv_dp(fd, &werr, "\n  ],\n");

	lv_dp(fd, &werr,
		"  \"truncated\": %s,\n"
		"  \"degraded\": {\"vt_registry\": %s, \"exec_registry\": %s, \"reactor\": %s}\n"
		"}\n",
		s->vt_truncated ? "true" : "false",
		s->degraded_vt_registry ? "true" : "false",
		s->degraded_exec_registry ? "true" : "false",
		s->degraded_reactor ? "true" : "false");
	return werr;
}

/* ---- bounded stderr summary ------------------------------------------- */

#define DRIFT_LIVENESS_TOPN 5

/* Pick up to k indices of VTs matching `state` with the largest age, writing
 * them (most-stuck first) into out[]; returns the count written. */
static int drift_liveness_top_by_age(const DriftLivenessSnapshot *s, int state,
                                     int *out, int k) {
	int count = 0;
	for (int i = 0; i < s->vt_count; i++) {
		if (s->vts[i].state != state) continue;
		int64_t age_i = s->now_ms - s->vts[i].state_since_ms;
		/* insertion into a descending-age top-k list */
		int pos = count;
		while (pos > 0) {
			int64_t age_prev = s->now_ms - s->vts[out[pos - 1]].state_since_ms;
			if (age_prev >= age_i) break;
			if (pos < k) out[pos] = out[pos - 1];
			pos--;
		}
		if (pos < k) {
			out[pos] = i;
			if (count < k) count++;
		}
	}
	return count;
}

static void drift_liveness_write_text(int fd, const DriftLivenessSnapshot *s,
                                      const char *json_path, int json_errno) {
	dprintf(fd, "[drift:liveness] === drift.liveness.v1 pid=%d uptime=%lldms reason=%s ===\n",
		s->pid, (long long)s->uptime_ms, drift_liveness_reason_str(s->reason));

	if (json_path) {
		dprintf(fd, "[drift:liveness] json=%s\n", json_path);
	} else {
		dprintf(fd, "[drift:liveness] json=WRITE_FAILED errno=%d (%s)\n",
			json_errno, strerror(json_errno));
	}

	dprintf(fd, "[drift:liveness] progress_counter=%llu completed=%llu\n",
		(unsigned long long)s->progress_counter, (unsigned long long)s->exec_completed);

	dprintf(fd, "[drift:liveness] executor: workers=%d running=%d ready=%lld shutting_down=%d\n",
		s->exec_workers, s->exec_running, (long long)s->exec_ready_queue_len,
		s->exec_shutting_down);

	if (s->reactor_present) {
		dprintf(fd, "[drift:liveness] reactor: fd_waiters=%d timers=%d next_deadline_ms=%lld\n",
			s->reactor_fd_waiters, s->reactor_timers, (long long)s->reactor_next_deadline_ms);
	} else {
		dprintf(fd, "[drift:liveness] reactor: not initialized\n");
	}

	dprintf(fd, "[drift:liveness] vts: total=%d running=%d ready=%d parked=%d "
		"(timer=%d io=%d join=%d condvar=%d channel=%d) finished=%d cancelled=%d%s\n",
		s->vt_count, s->tally_running, s->tally_ready, s->tally_parked,
		s->tally_wait[DRIFT_WAIT_TIMER], s->tally_wait[DRIFT_WAIT_IO],
		s->tally_wait[DRIFT_WAIT_JOIN], s->tally_wait[DRIFT_WAIT_CONDVAR],
		s->tally_wait[DRIFT_WAIT_CHANNEL],
		s->tally_finished, s->tally_cancelled,
		s->vt_truncated ? " (TRUNCATED)" : "");

	/* Carriers: every RUNNING VT is on a carrier.  running count is bounded by
	 * worker count, so this list is naturally small. */
	int top[DRIFT_LIVENESS_TOPN];
	int n = drift_liveness_top_by_age(s, DRIFT_LV_VT_RUNNING, top, DRIFT_LIVENESS_TOPN);
	if (n > 0) {
		dprintf(fd, "[drift:liveness] top running (hot-stuck candidates):\n");
		for (int i = 0; i < n; i++) {
			const DriftVtSnapshot *v = &s->vts[top[i]];
			int64_t age = s->now_ms - v->state_since_ms;
			dprintf(fd, "[drift:liveness]   vtid=%llu carrier_tid=%llu running_for=%lldms",
				(unsigned long long)v->vtid, (unsigned long long)v->carrier_tid,
				(long long)age);
			if (v->op_len > 0) {
				dprintf(fd, " op=%.*s", v->op_len, v->op_label);
			}
			if (v->exec_id != 0) {
				dprintf(fd, " exec_id=%llu", (unsigned long long)v->exec_id);
			}
			if (v->ffi_symbol != NULL) {
				dprintf(fd, " ffi=%s@%s:%lld", v->ffi_symbol,
					v->ffi_file ? v->ffi_file : "<unknown>", (long long)v->ffi_line);
			}
			dprintf(fd, "\n");
		}
	}

	n = drift_liveness_top_by_age(s, DRIFT_LV_VT_PARKED, top, DRIFT_LIVENESS_TOPN);
	if (n > 0) {
		dprintf(fd, "[drift:liveness] top parked (cold-stuck candidates):\n");
		for (int i = 0; i < n; i++) {
			const DriftVtSnapshot *v = &s->vts[top[i]];
			int64_t age = s->now_ms - v->state_since_ms;
			dprintf(fd, "[drift:liveness]   vtid=%llu %s wait_id=%llu parked_for=%lldms",
				(unsigned long long)v->vtid,
				drift_liveness_state_str(v->state, v->wait_kind),
				(unsigned long long)v->wait_id, (long long)age);
			if (v->op_len > 0) {
				dprintf(fd, " op=%.*s", v->op_len, v->op_label);
			}
			if (v->wait_kind == DRIFT_WAIT_BLOCKING_ADMISSION) {
				dprintf(fd, " wait=blocking-admission exec_id=%llu",
					(unsigned long long)v->wait_id);
			} else if (v->exec_id != 0) {
				dprintf(fd, " exec_id=%llu", (unsigned long long)v->exec_id);
			}
			dprintf(fd, "\n");
		}
	}

	if (s->degraded_vt_registry || s->degraded_exec_registry || s->degraded_reactor) {
		dprintf(fd, "[drift:liveness] DEGRADED (lock unavailable): vt_registry=%d exec_registry=%d reactor=%d\n",
			s->degraded_vt_registry, s->degraded_exec_registry, s->degraded_reactor);
	}
}

/* ---- public emit ------------------------------------------------------ */

void drift_liveness_emit(int reason) {
	DriftLivenessSnapshot *snap = (DriftLivenessSnapshot *)malloc(sizeof(*snap));
	if (!snap) {
		static const char msg[] = "[drift:liveness] error: snapshot allocation failed\n";
		(void)write(2, msg, sizeof(msg) - 1);
		return;
	}

	drift_liveness_collect(snap, reason);

	char path[1024];
	path[0] = '\0';
	int json_ok = 0;
	int json_err = 0;
	int path_ok = drift_liveness_resolve_json_path(path, sizeof(path), snap->pid);
	if (path_ok) {
		/* 0600: dumps carry runtime state (fds, tids, and future logical
		 * frames) — not world-readable.  O_NOFOLLOW: refuse to follow a
		 * symlink planted at the target path (the default lives in /tmp). */
		int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
		if (fd >= 0) {
			int werr = drift_liveness_write_json(fd, snap);
			if (werr != 0) {
				json_err = errno ? errno : EIO;
			}
			if (close(fd) != 0) {
				werr = -1;
				json_err = errno;
			}
			json_ok = (werr == 0);
		} else {
			json_err = errno;
		}
	} else {
		json_err = ENAMETOOLONG;
	}

	if (drift_liveness_text_enabled()) {
		drift_liveness_write_text(2, snap, json_ok ? path : NULL, json_err);
	} else if (!json_ok) {
		/* Text summary suppressed, but a failed JSON write must not be
		 * silent — emit one error line regardless of DRIFT_LIVENESS_TEXT. */
		dprintf(2, "[drift:liveness] error: JSON dump write failed (errno=%d %s) path=%s\n",
			json_err, strerror(json_err), path_ok ? path : "(unresolved)");
	}

	free(snap);
}

/* ---- dedicated sigwait thread ----------------------------------------- */

#ifdef __linux__
static pthread_t drift_liveness_tid;
static pthread_once_t drift_liveness_once = PTHREAD_ONCE_INIT;
static atomic_int drift_liveness_started = 0;   /* 1 once the thread is running */
static atomic_int drift_liveness_stopping = 0;  /* set by shutdown to make the thread exit */

static void *drift_liveness_thread_main(void *arg) {
	(void)arg;
	/* SIGUSR2 was blocked process-wide by drift_run_main_on_vt before any
	 * thread existed, so this thread is the sole consumer.  sigwait keeps all
	 * formatting in a normal (non-async-signal) context. */
	sigset_t set;
	sigemptyset(&set);
	sigaddset(&set, SIGUSR2);
	for (;;) {
		int signo = 0;
		if (sigwait(&set, &signo) != 0) {
			struct timespec ts = {0, 50 * 1000000L};  /* 50ms backoff */
			nanosleep(&ts, NULL);
			continue;
		}
		/* Shutdown wakes us with pthread_kill(SIGUSR2); exit without dumping
		 * so no runtime thread survives into drift_run_main_on_vt teardown. */
		if (atomic_load_explicit(&drift_liveness_stopping, memory_order_acquire)) {
			break;
		}
		if (signo == SIGUSR2) {
			drift_liveness_emit(DRIFT_LIVENESS_REASON_OPERATOR_SIGNAL);
		}
	}
	return NULL;
}

static void drift_liveness_thread_start_once(void) {
	if (pthread_create(&drift_liveness_tid, NULL, drift_liveness_thread_main, NULL) == 0) {
		atomic_store_explicit(&drift_liveness_started, 1, memory_order_release);
	} else {
		/* Fail-safe: SIGUSR2 stays blocked (never re-armed to default-
		 * terminate); the feature is simply unavailable. */
		static const char msg[] =
			"[drift:liveness] warning: liveness thread unavailable; SIGUSR2 dumps disabled\n";
		(void)write(2, msg, sizeof(msg) - 1);
	}
}

void drift_liveness_thread_start(void) {
	pthread_once(&drift_liveness_once, drift_liveness_thread_start_once);
}

/* Stop and join the liveness thread.  Called from drift_run_main_on_vt before
 * registry/reactor/executor teardown so no runtime-owned thread is alive during
 * process/atexit cleanup (the runtime's standing shutdown invariant).
 * Idempotent. */
void drift_liveness_thread_shutdown(void) {
	if (atomic_exchange_explicit(&drift_liveness_started, 0, memory_order_acq_rel) != 1) {
		return;  /* never started, or already shut down */
	}
	atomic_store_explicit(&drift_liveness_stopping, 1, memory_order_release);
	/* Wake the blocked sigwait; SIGUSR2 is blocked so it pends on the thread
	 * and is delivered to sigwait even if a dump is in progress.  Only join if
	 * the wake succeeded: if pthread_kill failed the thread is still parked in
	 * sigwait, so joining would hang shutdown forever — warn and skip it
	 * instead (the process is exiting anyway). */
	int rc = pthread_kill(drift_liveness_tid, SIGUSR2);
	if (rc == 0) {
		(void)pthread_join(drift_liveness_tid, NULL);
	} else {
		static const char msg[] =
			"[drift:liveness] warning: could not wake liveness thread to stop; skipping join\n";
		(void)write(2, msg, sizeof(msg) - 1);
	}
}
#else
void drift_liveness_thread_start(void) {}
void drift_liveness_thread_shutdown(void) {}
#endif
