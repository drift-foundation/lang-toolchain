# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _repo_root() -> Path:
	return Path(__file__).resolve().parents[3]


def _run_wrapper(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	wrapper = _repo_root() / "bin" / "driftc"
	return subprocess.run(["/bin/bash", str(wrapper), *args], text=True, capture_output=True, env=env)


def test_driftc_wrapper_rejects_memcheck_and_massif_in_direct_mode() -> None:
	env = dict(os.environ)
	env["DRIFT_MEMCHECK"] = "1"
	cp = _run_wrapper(["--help"], env=env)
	assert cp.returncode != 0
	assert "runner-only" in (cp.stderr or "")

	env = dict(os.environ)
	env["DRIFT_MASSIF"] = "1"
	cp = _run_wrapper(["--help"], env=env)
	assert cp.returncode != 0
	assert "runner-only" in (cp.stderr or "")


def test_driftc_wrapper_asan_adds_sanitize_flags(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main",
				"import std.core;",
				"fn main() nothrow -> Int {",
				"\treturn 0;",
				"}",
				"",
			]
		),
		encoding="utf-8",
	)
	out = tmp_path / "a.out"
	env = dict(os.environ)
	env["DRIFT_ASAN"] = "1"
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert "[driftc] link:" in stderr
	assert "-fsanitize=address" in stderr
	assert out.exists()
