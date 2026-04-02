# Single source of truth for toolchain identity and ABI contract version.
#
# This module is the neutral shared metadata point for both lang.drift and
# lang.driftc.  It is packaged as part of the PEX tool artifact.
#
# DRIFTC_VERSION: compiler/toolchain release version (SemVer).
#
# DRIFT_RT_ABI_VERSION: bump when changing any compiler/runtime boundary
# contract: runtime-exported helper signatures, data layouts crossing the
# boundary (struct/variant/frame payload ABI), calling conventions, or
# ownership/drop contract changes.
# Do not bump for pure internal refactors with no boundary change.

DRIFTC_VERSION: str = "0.27.142"
DRIFT_RT_ABI_VERSION: int = 7

# Build-time source commit stamp.  Empty in the source tree; populated by
# the deploy bundle step so that deployed toolchains report the exact commit
# they were built from, rather than probing git at runtime.
DRIFTC_GIT_SHA: str = ""
