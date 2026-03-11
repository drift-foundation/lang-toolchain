#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
	return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)


def _check_cmd(name: str) -> str | None:
	return shutil.which(name)


def _fail(msg: str) -> int:
	print(f"deps-check: {msg}", file=sys.stderr)
	return 1


def _warn(msg: str) -> None:
	print(f"deps-check: warning: {msg}", file=sys.stderr)


def _check_pkg_lib(lib: str) -> bool:
	res = _run(["pkg-config", "--exists", lib])
	return res.returncode == 0


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--quiet", action="store_true", help="suppress success output")
	args = ap.parse_args()

	clang = _check_cmd("clang")
	if clang is None:
		return _fail("clang not found in PATH")

	if _check_cmd("pkg-config") is None:
		return _fail("pkg-config not found in PATH")

	if _check_cmd("ld.gold") is None:
		return _fail("ld.gold not found; --gdb-index will be unavailable")

	missing_libs = [lib for lib in ("libdw", "libunwind", "libelf") if not _check_pkg_lib(lib)]
	if missing_libs:
		return _fail(f"missing pkg-config libs: {', '.join(missing_libs)}")

	# Validate LLVM/codegen + runtime stacktrace by compiling and running a tiny assert.
	with tempfile.TemporaryDirectory(prefix="drift_deps_") as tmp:
		tmp_dir = Path(tmp)
		src = tmp_dir / "assert_deps.drift"
		out = tmp_dir / "assert_deps_bin"
		src.write_text(
			"fn main() nothrow -> Int {\n\tassert(1 == 2, \"deps\");\n\treturn 0;\n}\n"
		)
		cmd = [
			sys.executable,
			"-m",
			"lang.driftc",
			"--debug-info",
			"--stdlib-root",
			"stdlib",
			str(src),
			"-o",
			str(out),
		]
		env = os.environ.copy()
		env["PYTHONPATH"] = str(ROOT)
		res = _run(cmd, cwd=ROOT, env=env, timeout=120)
		if res.returncode != 0:
			return _fail(f"lang.driftc debug build failed: {res.stderr.strip()}")

		run = _run([str(out)], cwd=tmp_dir, env=env, timeout=30)
		stderr = run.stderr or ""
		if "assertion failed" not in stderr:
			return _fail("assert runtime did not emit expected failure output")
		if "<stacktrace unavailable>" in stderr:
			return _fail("stacktrace unavailable; check libdw/libunwind installation")
		if "#0" not in stderr and "drift_assert_loc" not in stderr:
			return _fail("stacktrace missing; no frame output detected")

	# pex — required for deploy (PEX --scie eager packaging).
	venv_pex = ROOT / ".venv" / "bin" / "pex"
	if not venv_pex.exists():
		_warn("pex not installed in .venv (needed for deploy); install: ./.venv/bin/pip install pex")

	if not args.quiet:
		print("deps-check: ok")
	return 0


if __name__ == "__main__":
	sys.exit(main())
