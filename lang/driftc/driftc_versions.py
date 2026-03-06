# Single source of truth for compiler identity and ABI contract version.
#
# DRIFTC_VERSION: compiler release version (SemVer).
#
# DRIFT_RT_ABI_VERSION: bump when changing any compiler/runtime boundary
# contract: runtime-exported helper signatures, data layouts crossing the
# boundary (struct/variant/frame payload ABI), calling conventions, or
# ownership/drop contract changes.
# Do not bump for pure internal refactors with no boundary change.

DRIFTC_VERSION: str = "0.27.10-dev"
DRIFT_RT_ABI_VERSION: int = 4
