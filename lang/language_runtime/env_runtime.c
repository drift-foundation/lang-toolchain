/* Read-only process environment access (lang, v1). */
#include "env_runtime.h"

#include <stdlib.h>
#include <string.h>

/* `name` arrives with a transferred refcount per the Drift→C ABI
 * convention: the Drift caller (`std.env::get__impl`) retains via
 * `drift_string_retain(name)` before calling, so the callee owns that
 * stake and MUST release it before returning.  DRIFT_OWNED_STRING
 * makes the release automatic at scope exit; see
 * `lang/language_runtime/string_runtime.h` for the contract and
 * `lang/tests/driver/test_drift_owned_string_audit.py` for the
 * lint that pins adoption across the runtime. */

DriftString drift_env_get(DriftString name_in) {
	DRIFT_OWNED_STRING DriftString name = name_in;
	char *cname = drift_string_to_cstr(name);
	const char *val = getenv(cname);
	free(cname);
	if (val == NULL) {
		/* absent (guarded by env_has on the stdlib side; only an unset
		 * race reaches here) -> canonical empty, never the tombstone */
		return drift_string_empty();
	}
	return drift_string_from_cstr(val);
}

int drift_env_has(DriftString name_in) {
	DRIFT_OWNED_STRING DriftString name = name_in;
	char *cname = drift_string_to_cstr(name);
	const char *val = getenv(cname);
	free(cname);
	return val != NULL ? 1 : 0;
}
