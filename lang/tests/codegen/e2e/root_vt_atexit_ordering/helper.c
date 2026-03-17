/*
 * Atexit ordering validator for root VT execution.
 *
 * Registers a pthread_key with a TLS destructor on the worker thread
 * (reachable from root VT context) and an atexit handler.  Correct
 * shutdown sequence:
 *
 *   1. drift_run_main_on_vt shuts down executor → worker thread exits
 *      → pthread_key destructor fires (atexit_fired == 0 → OK)
 *   2. @main returns → atexit handlers fire → atexit_fired = 1
 *
 * Broken sequence (pre-fix ABI 6):
 *
 *   1. @main returns → atexit fires in LIFO:
 *      a. atexit_handler: atexit_fired = 1
 *      b. executor atexit: joins worker → TLS destructor fires
 *         with atexit_fired == 1 → BUG detected
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

static volatile int atexit_fired = 0;
static pthread_key_t tls_key;

static void tls_destructor(void *val) {
	if (val == NULL) return;
	free(val);
	if (atexit_fired) {
		fprintf(stderr, "ATEXIT_ORDERING_BUG: worker TLS destructor "
			"fired after atexit handler\n");
		fflush(stderr);
	}
}

static void atexit_handler(void) {
	atexit_fired = 1;
}

intptr_t atexit_test_init(void) {
	if (pthread_key_create(&tls_key, tls_destructor) != 0) return 1;
	atexit(atexit_handler);
	return 0;
}

intptr_t atexit_test_touch(void) {
	int *val = (int *)malloc(sizeof(int));
	if (val == NULL) return 1;
	*val = 42;
	pthread_setspecific(tls_key, val);
	return 0;
}
