# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
lang2-only e2e runner.

Default layout: each case lives under `tests/lang2-e2e/<case>/` with:
  - main.drift
  - expected.json

This runner drives `lang2.driftc` for both compile-error (JSON diagnostics)
and run-mode cases. For compile errors, it uses `--json` and matches structured
diagnostics. For run-mode, it builds an executable with `-o` and executes it,
matching exit/stdout/stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DRIFTC = REPO / ".venv" / "bin" / "python3"
DRIFTC_MODULE = "lang2.driftc"
BUILD_BASE = REPO / "build" / "tests" / "lang2" / "e2e"


def _run_case(case_dir: Path, build_root: Path) -> str:
	expected_path = case_dir / "expected.json"
	source_path = case_dir / "main.drift"
	if not expected_path.exists() or not source_path.exists():
		return "skipped (missing expected.json or main.drift)"

	expected = json.loads(expected_path.read_text())
	mode = expected.get("mode", "compile")
	use_json = expected.get("use_json", False) or "diagnostics" in expected
	build_dir = build_root / case_dir.name
	if build_dir.exists():
		shutil.rmtree(build_dir)
	build_dir.mkdir(parents=True, exist_ok=True)

	env = dict(os.environ)
	env["PYTHONPATH"] = str(ROOT)

	if use_json:
		cmd = [
			str(DRIFTC),
			"-m",
			DRIFTC_MODULE,
			str(source_path),
			"--json",
		]
		res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
		try:
			payload = json.loads(res.stdout)
		except json.JSONDecodeError as err:
			return f"FAIL (expected JSON diagnostics, got parse error: {err})"
		exit_expected = expected.get("exit_code", 1)
		if payload.get("exit_code") != exit_expected:
			return f"FAIL (exit {payload.get('exit_code')} != expected {exit_expected})"
		diag_expect = expected.get("diagnostics", [])
		diags = payload.get("diagnostics", [])
		for exp in diag_expect:
			msg_sub = exp.get("message_contains")
			phase = exp.get("phase")
			match_found = False
			for d in diags:
				if phase is not None and d.get("phase") != phase:
					continue
				if msg_sub is not None and msg_sub not in d.get("message", ""):
					continue
				match_found = True
				break
			if not match_found:
				return "FAIL (missing expected diagnostic)"
		return "ok"

	# Build executable.
	output_path = build_dir / "a.out"
	cmd = [
		str(DRIFTC),
		"-m",
		DRIFTC_MODULE,
		str(source_path),
		"-o",
		str(output_path),
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
	if res.returncode != 0:
		return f"FAIL (compile/link failed: {res.stderr})"

	if mode == "compile":
		return "ok"

	run_res = subprocess.run([str(output_path), *expected.get("args", [])], capture_output=True, text=True, env=env)
	exit_expected = expected.get("exit_code", 0)
	if run_res.returncode != exit_expected:
		return f"FAIL (exit {run_res.returncode}, expected {exit_expected})"
	if run_res.stdout != expected.get("stdout", ""):
		return "FAIL (stdout mismatch)"
	if run_res.stderr != expected.get("stderr", ""):
		return "FAIL (stderr mismatch)"
	return "ok"


def main(argv: Iterable[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description="Run lang2 e2e tests via lang2.driftc")
	ap.add_argument("cases", nargs="*", help="Specific test case names (subdirs under --root)")
	ap.add_argument(
		"--root",
		default="tests/lang2-e2e",
		help="Root directory containing per-case subdirs (default: tests/lang2-e2e)",
	)
	args = ap.parse_args(argv)

	root_dir = Path(args.root)
	if not root_dir.is_absolute():
		root_dir = ROOT.parent / root_dir

	if not root_dir.exists():
		print(f"{root_dir}: no test cases (directory missing)", file=sys.stderr)
		return 0

	build_root = BUILD_BASE / root_dir.name

	case_dirs = sorted(d for d in root_dir.iterdir() if d.is_dir())
	if args.cases:
		names = set(args.cases)
		case_dirs = [d for d in case_dirs if d.name in names]

	failures: list[tuple[Path, str]] = []
	for case_dir in case_dirs:
		if not (case_dir / "main.drift").exists():
			continue
		status = _run_case(case_dir, build_root)
		print(f"{case_dir.name}: {status}")
		if not status.startswith("ok") and not status.startswith("skipped"):
			failures.append((case_dir, status))

	if failures:
		for case, status in failures:
			print(f"[e2e] {case.name}: {status}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
