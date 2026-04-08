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

/*
 * Internal runtime identity sentinel — NOT user-facing API.
 *
 * The Drift toolchain ships two runtime variants from a single staged
 * distribution: a normal/optimized variant and an explicit `_debug`
 * opt-in variant.  The dual-runtime selection regression uses these
 * paired sentinels to prove which variant the linker actually pulled in.
 *
 * Co-located with the ABI version stamp because this object file is
 * always extracted from the static archive (the compiler emits a required
 * reference to __drift_rt_abi_version_<N>), guaranteeing the sentinel is
 * present in every linked binary regardless of static-archive .o selection.
 *
 * Selection is gated at runtime-build time by the `-DDRIFT_RT_MODE_DEBUG=1`
 * cflag passed only to the debug-style variant.  __attribute__((used))
 * prevents compiler dead-code elimination at any optimization level.
 *
 * Contract: each runtime archive exports exactly ONE of the two sentinels.
 */
#ifdef DRIFT_RT_MODE_DEBUG
__attribute__((used)) const int __drift_rt_mode_debug = 1;
#else
__attribute__((used)) const int __drift_rt_mode_normal = 1;
#endif
