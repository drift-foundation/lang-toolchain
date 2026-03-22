/*
 * ABI version stamp for link-time compatibility guard.
 *
 * The compiler emits a required reference to the same versioned symbol.
 * If compiler and runtime disagree on ABI version, the link step fails
 * with an unresolved symbol — deterministic, no runtime crash.
 *
 * The version number is injected via -DDRIFT_RT_ABI_VERSION=N at compile
 * time from the authoritative constant in lang/versions.py.
 */

#ifndef DRIFT_RT_ABI_VERSION
#error "DRIFT_RT_ABI_VERSION must be defined at compile time"
#endif

#define _DRIFT_ABI_PASTE2(prefix, ver) prefix ## ver
#define _DRIFT_ABI_PASTE(prefix, ver) _DRIFT_ABI_PASTE2(prefix, ver)
#define _DRIFT_ABI_SYMBOL _DRIFT_ABI_PASTE(__drift_rt_abi_version_, DRIFT_RT_ABI_VERSION)

void _DRIFT_ABI_SYMBOL(void) {
    /* Intentionally empty — existence is the contract. */
}
