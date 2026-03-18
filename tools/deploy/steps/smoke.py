# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy step: compile and run smoke test using staged distribution.

Exercises the full signed-package path: PEX entry → compiler → package
load → signature verification → compile → link → run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run_smoke_test(dist: Path, repo_root: Path, stage: Path) -> None:
	"""Compile and run smoke test using deployed PEX executable."""
	print("[deploy] running smoke test with deployed PEX executable...", flush=True)

	smoke_src = repo_root / "tools" / "deploy" / "smoke_test.drift"
	smoke_bin = stage / "smoke_test_bin"
	driftc = dist / "bin" / "driftc"

	# Scrub PYTHONPATH — PEX must be self-contained.
	env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}

	# Compile.
	result = subprocess.run(
		[str(driftc), str(smoke_src), "-o", str(smoke_bin)],
		env=env,
		capture_output=True, text=True,
	)
	# Show last few lines of compiler output (link command, etc.)
	if result.stderr:
		for line in result.stderr.strip().splitlines()[-5:]:
			print(line, flush=True)
	if result.returncode != 0:
		raise RuntimeError(
			f"smoke compilation failed (exit {result.returncode}):\n{result.stderr}"
		)

	# Run.
	run_result = subprocess.run(
		[str(smoke_bin)],
		env=env,
		capture_output=True, text=True,
	)
	smoke_stdout = run_result.stdout.strip()
	smoke_exit = run_result.returncode

	if smoke_exit != 42:
		raise RuntimeError(
			f"smoke test failed (expected exit 42, got {smoke_exit})\n"
			f"stdout: {smoke_stdout}"
		)
	if smoke_stdout != "drift deploy smoke ok":
		raise RuntimeError(
			f"smoke test stdout mismatch\n"
			f"expected: drift deploy smoke ok\n"
			f"got:      {smoke_stdout}"
		)

	print(f"[deploy] smoke test passed (exit=42, stdout ok)", flush=True)

	# Clean build artifacts.
	for lock in dist.rglob(".build.lock"):
		lock.unlink()
