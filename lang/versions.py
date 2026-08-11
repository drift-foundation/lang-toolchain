# Single source of truth for toolchain identity and ABI contract version.
#
# This module is the neutral shared metadata point for both lang.drift and
# lang.driftc.  It is packaged as part of the PEX tool artifact.
#
# DRIFTC_VERSION: compiler/toolchain release version (SemVer).  Before 1.0,
# actual or suspected user-visible impact bumps MINOR and resets PATCH
# (0.Y.Z -> 0.(Y+1).0); PATCH is reserved for changes proven user-neutral.
#
# DRIFT_RT_ABI_VERSION: bump when changing any compiler/runtime boundary
# contract: runtime-exported helper signatures, data layouts crossing the
# boundary (struct/variant/frame payload ABI), calling conventions, or
# ownership/drop contract changes.
# Do not bump for pure internal refactors with no boundary change.

DRIFTC_VERSION: str = "0.36.0"
DRIFT_RT_ABI_VERSION: int = 22

# Build-time source commit stamp.  Empty in the source tree; populated by
# the deploy bundle step so that deployed toolchains report the exact commit
# they were built from, rather than probing git at runtime.
DRIFTC_GIT_SHA: str = ""

# App-visible toolchain identity stamped into the drift-build-info/v1
# document (`meta.build_info()` / the `.drift_build_info` section).
# `vendor` is the org that produced this toolchain build; `license` is the
# SPDX identifier of the toolchain's source license.  Both fields are
# constants of THIS toolchain build (a downstream rebuild from a fork
# would change them).  They surface to app code in the document's
# `toolchain` section beside `driftc` / `abi` / `git`.
DRIFTC_VENDOR: str = "The Drift Language Foundation"
DRIFTC_LICENSE: str = "GPL-3.0"
