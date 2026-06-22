/* Whitebox runtime unit test for the poll_many stale-fd-event generation guard.
 *
 * This is a TEST-ONLY translation unit: it #includes the runtime source so it can
 * call the static `drift_reactor_watch_for_event` resolver directly. None of this
 * is compiled into the packaged/certified toolchain, and the runtime exposes NO
 * Drift-visible test hooks — the production surface is `poll_many` behaviour only.
 *
 * Pins the generation guard: an epoll event payload `data.u64 = (gen<<32)|fd`
 * resolves to the current watch ONLY when the generation matches; a stale event for
 * a closed-then-reused fd number is dropped (no pending set, no VT woken).
 *
 * (The MariaDB keepalive incident — a latched read hint surviving an exact-length
 * read — is gated separately, end-to-end, by the std.net driver tests
 * test_poll_many_exact_length_read_no_stale_readable and
 * test_poll_many_more_buffered_than_read_still_readable.)
 *
 * Built and run by lang/tests/driver/test_reactor_stale_fd_event.py.
 */
#include "posix/thread_runtime.c"
#include <assert.h>
#include <stdio.h>

/* Stubs for the few externs the runtime TU references (asm context switch + sibling
 * TUs) that this resolver-only test never calls. */
void drift_swapcontext(DriftContext *from, DriftContext *to) { (void)from; (void)to; }
void drift_makecontext(DriftContext *ctx, void *stack_top,
                       void (*entry)(uintptr_t), uintptr_t arg) {
	(void)ctx; (void)stack_top; (void)entry; (void)arg;
}
void drift_liveness_thread_start(void) {}
void drift_liveness_thread_shutdown(void) {}
char *drift_string_to_cstr(DriftString s) { (void)s; return 0; }

static uint64_t pack(uint32_t gen, int fd) {
	return ((uint64_t)gen << 32) | (uint32_t)fd;
}

int main(void) {
	Reactor r;
	memset(&r, 0, sizeof(r));
	pthread_mutex_init(&r.mu, NULL);
	r.epoll_fd = -1;
	r.wake_fd = -1;

	/* Two watches on distinct fds with distinct generations. */
	ReactorWatch wa; memset(&wa, 0, sizeof(wa)); wa.fd = 7;  wa.generation = 100;
	ReactorWatch wb; memset(&wb, 0, sizeof(wb)); wb.fd = 11; wb.generation = 250;
	wa.next = &wb; wb.next = NULL; r.watches = &wa;

	/* 1. Current-generation event resolves to its watch. */
	assert(drift_reactor_watch_for_event(&r, pack(100, 7), NULL) == &wa);
	assert(drift_reactor_watch_for_event(&r, pack(250, 11), NULL) == &wb);

	/* 2. Stale (older) generation for a reused fd number is dropped + counted. */
	long d0 = (long)atomic_load(&drift_reactor_stale_fd_event_drops);
	assert(drift_reactor_watch_for_event(&r, pack(99, 7), NULL) == NULL);
	assert((long)atomic_load(&drift_reactor_stale_fd_event_drops) == d0 + 1);

	/* 3. Any non-matching generation (incl. a higher one) is also dropped. */
	assert(drift_reactor_watch_for_event(&r, pack(101, 7), NULL) == NULL);
	assert((long)atomic_load(&drift_reactor_stale_fd_event_drops) == d0 + 2);

	/* 4. Generation 0 bypasses the check (wake_fd/signal_fd are registered via
	 *    data.fd, i.e. generation field 0, and matched by fd). */
	assert(drift_reactor_watch_for_event(&r, (uint64_t)7u, NULL) == &wa);

	/* 5. The fd is decoded from the low 32 bits regardless of the generation. */
	int fd_out = -1;
	(void)drift_reactor_watch_for_event(&r, pack(100, 7), &fd_out);
	assert(fd_out == 7);
	fd_out = -1;
	(void)drift_reactor_watch_for_event(&r, pack(99, 7), &fd_out);   /* stale: still decodes fd */
	assert(fd_out == 7);

	/* 6. No watch for the fd → NULL, and NOT counted as a stale-generation drop
	 *    (a vanished fd is a distinct, also-correct, no-op). */
	long d1 = (long)atomic_load(&drift_reactor_stale_fd_event_drops);
	assert(drift_reactor_watch_for_event(&r, pack(5, 999), NULL) == NULL);
	assert((long)atomic_load(&drift_reactor_stale_fd_event_drops) == d1);

	printf("reactor-stale-fd-event: ALL-OK\n");
	return 0;
}
