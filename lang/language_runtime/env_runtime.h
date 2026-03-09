#ifndef DRIFT_ENV_RUNTIME_H
#define DRIFT_ENV_RUNTIME_H

#include "string_runtime.h"

/* Read-only process environment access.
 *
 * drift_env_get: returns the value of the named environment variable.
 *   If the variable is unset, returns a valid empty DriftString {0, NULL}.
 *   If set to an empty string, returns {0, NULL}.
 *   Otherwise returns a freshly-allocated DriftString copy of the value.
 *   The caller owns the returned string and must release it.
 *   Use drift_env_has to distinguish unset from empty.
 *
 * drift_env_has: returns 1 if the named variable is set, 0 otherwise.
 */
DriftString drift_env_get(DriftString name);
int drift_env_has(DriftString name);

#endif  /* DRIFT_ENV_RUNTIME_H */
