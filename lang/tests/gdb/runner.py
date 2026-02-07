#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = ROOT / "build" / "tests" / "lang" / "tests" / "gdb"


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
	return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)


def _load_expected(path: Path) -> dict:
	if not path.exists():
		return {}
	return json.loads(path.read_text())


def _should_skip(expected: dict) -> str | None:
	if expected.get("sandbox_blocks") and os.environ.get("DRIFT_SANDBOX"):
		return "SKIP (sandbox)"
	if shutil.which("gdb") is None:
		return "SKIP (gdb not found)"
	return None


def _compile_case(case_dir: Path, out_bin: Path) -> tuple[int, str]:
	cmd = [
		sys.executable,
		"-m",
		"lang.driftc",
		"--debug-info",
		"--stdlib-root",
		"stdlib",
		str(case_dir / "main.drift"),
		"-o",
		str(out_bin),
	]
	env = os.environ.copy()
	env["PYTHONPATH"] = str(ROOT)
	res = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True)
	if res.returncode != 0:
		return res.returncode, res.stderr.strip()
	return 0, ""


def _run_gdb(case_dir: Path, bin_path: Path) -> tuple[int, str, str]:
	cmds = case_dir / "gdb.txt"
	cmd = ["gdb", "-q", "-batch", "-x", str(cmds), str(bin_path)]
	res = _run(cmd, cwd=ROOT, timeout=60)
	return res.returncode, res.stdout, res.stderr


def _check_contains(actual: str, required: list[str]) -> str | None:
	missing = [s for s in required if s not in actual]
	if missing:
		return "missing: " + ", ".join(missing)
	return None


def _run_case(case_dir: Path, debug: bool) -> str:
	expected = _load_expected(case_dir / "expected.json")
	skip = _should_skip(expected)
	if skip:
		return skip
	build_dir = BUILD_ROOT / case_dir.name
	build_dir.mkdir(parents=True, exist_ok=True)
	out_bin = build_dir / "prog"
	code, msg = _compile_case(case_dir, out_bin)
	if code != 0:
		return f"FAIL (compile): {msg}"
	rc, out, err = _run_gdb(case_dir, out_bin)
	if rc != 0:
		return f"FAIL (gdb exit {rc})"
	stdout_contains = expected.get("stdout_contains", [])
	stderr_contains = expected.get("stderr_contains", [])
	if stdout_contains:
		missing = _check_contains(out, stdout_contains)
		if missing:
			if debug:
				return f"FAIL (stdout {missing})\nstdout:\n{out}\nstderr:\n{err}"
			return f"FAIL (stdout {missing})"
	if stderr_contains:
		missing = _check_contains(err, stderr_contains)
		if missing:
			if debug:
				return f"FAIL (stderr {missing})\nstdout:\n{out}\nstderr:\n{err}"
			return f"FAIL (stderr {missing})"
	return "ok"


def main(argv: list[str] | None = None) -> int:
	argv = argv or sys.argv[1:]
	debug = "--debug" in argv
	args = [a for a in argv if a != "--debug"]
	case_root = ROOT / "lang" / "tests" / "gdb"
	case_dirs = sorted(d for d in case_root.iterdir() if d.is_dir()) if case_root.exists() else []
	if args:
		names = set(args)
		case_dirs = [d for d in case_dirs if d.name in names]
	failures: list[tuple[Path, str]] = []
	for case_dir in case_dirs:
		status = _run_case(case_dir, debug=debug)
		if status != "ok" and not status.startswith("SKIP"):
			failures.append((case_dir, status))
		print(f"[gdb] {case_dir.name}: {status}", file=sys.stderr)
	if failures:
		print("============[ SUMMARY ]=============", file=sys.stderr)
		print(f"Failures: {len(failures)}", file=sys.stderr)
		for case_dir, status in failures:
			print(f"{case_dir.name}: {status}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
