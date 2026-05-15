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

# Route this tool's scratch under $DRIFT_TMP_ROOT so it's janitor-safe
# even when run outside pytest (which would otherwise relocate via
# PYTEST_DEBUG_TEMPROOT).  See docs/conventions/tmp-root.md.
sys.path.insert(0, str(ROOT))
from lang.test_support.drift_tmp import session_root as _drift_session_root


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
	#
	# Dual-runtime contract: backtrace symbolization machinery (libdwfl +
	# libunwind walk) lives ONLY in the debug-style runtime variant.  The
	# normal lane's `drift_debug_print_stacktrace()` is a stub that prints
	# a single hint line.  This deps check exists to validate that the host
	# CAN produce working backtraces; that requires selecting the
	# debug-style lane explicitly via DRIFT_DEBUG=1, regardless of any
	# DRIFT_DEBUG state inherited from the caller.  --debug-info enables
	# DWARF emission in the user binary but is orthogonal to runtime lane
	# selection.
	#
	# Cache isolation: deps_check builds the runtime archive into its own
	# temporary cache root (DRIFT_RUNTIME_LIB_CACHE_DIR=<tmp>/runtime_cache)
	# so that concurrent test workers cannot race against the shared
	# `<repo>/build/runtime_libs/` cache during xdist parallel runs.  This
	# eliminates a flake observed under `just lang-driver-test` where two
	# workers concurrently rebuilt `build/runtime_libs/debug/...` and one
	# of them observed the other's in-progress ar(1) write.
	with tempfile.TemporaryDirectory(prefix="drift_deps_", dir=str(_drift_session_root())) as tmp:
		tmp_dir = Path(tmp)
		src = tmp_dir / "assert_deps.drift"
		out = tmp_dir / "assert_deps_bin"
		runtime_cache = tmp_dir / "runtime_cache"
		runtime_cache.mkdir(parents=True, exist_ok=True)
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
		# Maximally-isolated env: strip every DRIFT_* var inherited from
		# the parent (test runner, shell, etc.) so the variant selection
		# can ONLY see what we explicitly set here.  This eliminates a
		# class of flakes where an inherited DRIFT_ASAN / DRIFT_UBSAN /
		# etc. caused driftc to pick a runtime variant other than the
		# debug one this check requires.
		env = {k: v for k, v in os.environ.items() if not k.startswith("DRIFT_")}
		env["PYTHONPATH"] = str(ROOT)
		env["DRIFT_DEBUG"] = "1"
		env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(runtime_cache)
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
			# Self-diagnostic dump: when this check fails, we want to know
			# *why* — most likely the binary linked the normal runtime
			# variant despite DRIFT_DEBUG=1 being set in the env (cache
			# leak, env-propagation race, or runtime variant resolution
			# bug).  Inspect the binary's sentinel surface and dump the
			# observed stderr so the next failure pinpoints the cause
			# instead of repeating "stacktrace missing".
			diag_lines = ["stacktrace missing; no frame output detected"]
			nm = shutil.which("nm")
			if nm is not None:
				nm_res = subprocess.run(
					[nm, "--defined-only", str(out)],
					text=True, capture_output=True, timeout=10,
				)
				if nm_res.returncode == 0:
					has_normal = "__drift_rt_mode_normal" in nm_res.stdout
					has_debug = "__drift_rt_mode_debug" in nm_res.stdout
					diag_lines.append(
						f"  binary sentinels: normal={has_normal} debug={has_debug}"
					)
					if has_normal and not has_debug:
						diag_lines.append(
							"  → binary linked NORMAL runtime variant despite DRIFT_DEBUG=1"
						)
					elif has_debug and not has_normal:
						diag_lines.append(
							"  → binary linked DEBUG runtime variant correctly; "
							"the libdwfl/libunwind walk produced no frames"
						)
			diag_lines.append("  full DRIFT_* env passed to driftc subprocess:")
			drift_env = sorted((k, v) for k, v in env.items() if k.startswith("DRIFT_"))
			if drift_env:
				for k, v in drift_env:
					diag_lines.append(f"    {k}={v}")
			else:
				diag_lines.append("    (none)")
			diag_lines.append(f"  observed stderr (first 1KB):\n{stderr[:1024]}")
			return _fail("\n".join(diag_lines))

	# pex — required for deploy (PEX --scie eager packaging).
	venv_pex = ROOT / ".venv" / "bin" / "pex"
	if not venv_pex.exists():
		_warn("pex not installed in .venv (needed for deploy); install: ./.venv/bin/pip install pex")

	if not args.quiet:
		print("deps-check: ok")
	return 0


if __name__ == "__main__":
	sys.exit(main())
