# Helper to list runtime C sources for linking.
from pathlib import Path
from typing import List

def get_runtime_sources(root: Path) -> List[Path]:
	base = root / "lang2" / "drift_core" / "runtime"
	runtime = root / "lang2" / "runtime"
	return [
		# Deterministic float formatting (Ryu) for Drift `Float` once supported.
		base / "ryu_d2s.c",
		base / "array_runtime.c",
		base / "string_runtime.c",
		base / "argv_runtime.c",
		base / "console_runtime.c",
		# Diagnostic/Error runtime lives alongside lang2/ for now; include it so
		# e2e codegen links DV/exception helpers.
		runtime / "diagnostic_runtime.c",
		runtime / "error_dummy.c",
	]

__all__ = ["get_runtime_sources"]
