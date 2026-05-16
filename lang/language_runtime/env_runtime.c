/* Read-only process environment access (lang, v1). */
#include "env_runtime.h"

#include <stdlib.h>
#include <string.h>

/* `name` arrives with a transferred refcount per the Drift→C ABI
 * convention: the Drift caller (`std.env::get__impl`) retains via
 * `drift_string_retain(name)` before calling, so the callee owns that
 * stake and MUST release it before returning.  Matches the
 * `console_runtime.c` and `posix/io_runtime.c::drift_io_open` patterns.
 * Without the release, every call from a heap-allocated String arg
 * leaks one refcount; string literals are static-flagged and would
 * mask the leak in fixtures that only call with `"HOME"`-style
 * constants. */

DriftString drift_env_get(DriftString name) {
	char *cname = drift_string_to_cstr(name);
	const char *val = getenv(cname);
	free(cname);
	drift_string_release(name);
	if (val == NULL) {
		DriftString s = {0, NULL};
		return s;
	}
	return drift_string_from_cstr(val);
}

int drift_env_has(DriftString name) {
	char *cname = drift_string_to_cstr(name);
	const char *val = getenv(cname);
	free(cname);
	drift_string_release(name);
	return val != NULL ? 1 : 0;
}
