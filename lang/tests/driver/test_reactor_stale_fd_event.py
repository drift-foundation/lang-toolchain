"""Driver for the whitebox runtime unit test of the poll_many stale-fd-event
generation guard (lang/tests/runtime/reactor_stale_fd_event_test.c).

The deterministic pin for the fix lives in C, NOT as Drift intrinsics: the runtime
exposes no test hooks to stdlib, so the production surface stays `poll_many`
behaviour only. This compiles the whitebox TU (which #includes the runtime source
to reach the static resolver) and runs it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# lang/tests/driver/ -> repo root is parents[3] (.../lang/tests/driver/<file>)
_REPO = Path(__file__).resolve().parents[3]
_RUNTIME_INC = _REPO / "lang" / "language_runtime"
_COMPILER_INC = _REPO / "lang" / "compiler_infra"
_TEST_C = _REPO / "lang" / "tests" / "runtime" / "reactor_stale_fd_event_test.c"


@pytest.mark.skipif(sys.platform != "linux", reason="epoll reactor is Linux-only")
def test_reactor_stale_fd_event_generation_guard(tmp_path: Path) -> None:
	cc = shutil.which("clang") or shutil.which("cc") or shutil.which("gcc")
	if cc is None:
		pytest.skip("no C compiler available")
	assert _TEST_C.is_file(), f"missing whitebox test: {_TEST_C}"
	binary = tmp_path / "reactor_stale_fd_event_test"
	compile_cmd = [
		cc, "-O1", "-x", "c", "-DDRIFT_RT_ABI_VERSION=18",
		f"-I{_RUNTIME_INC}", f"-I{_COMPILER_INC}",
		str(_TEST_C), "-lpthread", "-o", str(binary),
	]
	cres = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=180)
	assert cres.returncode == 0, f"whitebox test failed to compile:\n{cres.stderr[:2000]}"
	rres = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
	assert rres.returncode == 0, (
		f"stale-fd-event generation guard FAILED (rc={rres.returncode}):\n"
		f"{rres.stdout}\n{rres.stderr}"
	)
	assert "ALL-OK" in rres.stdout, f"unexpected output: {rres.stdout!r}"
