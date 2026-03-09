/* Read-only process environment access (lang, v1). */
#include "env_runtime.h"

#include <stdlib.h>
#include <string.h>

DriftString drift_env_get(DriftString name) {
	char *cname = drift_string_to_cstr(name);
	const char *val = getenv(cname);
	free(cname);
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
	return val != NULL ? 1 : 0;
}
