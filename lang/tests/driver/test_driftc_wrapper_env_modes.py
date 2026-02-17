# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from lang.language_runtime import build_runtime_archive


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


def test_driftc_wrapper_runtime_archive_mode_links_static_runtime(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main",
				"import std.core;",
				"fn main() nothrow -> Int {",
				"	return 0;",
				"}",
				"",
			]
		),
		encoding="utf-8",
	)
	out = tmp_path / "a.out"
	env = dict(os.environ)
	env["DRIFT_RUNTIME_LINK_MODE"] = "archive"
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert "libdrift_rt.a" in stderr
	assert out.exists()


def test_driftc_wrapper_runtime_archive_mode_respects_custom_cache_dir(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main",
				"import std.core;",
				"fn main() nothrow -> Int {",
				"	return 0;",
				"}",
				"",
			]
		),
		encoding="utf-8",
	)
	out = tmp_path / "a.out"
	cache_dir = tmp_path / "runtime_cache"
	prev = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		clang = subprocess.run(["/bin/bash", "-lc", "command -v clang-15 || command -v clang"], text=True, capture_output=True).stdout.strip()
		assert clang
		build_runtime_archive(_repo_root(), clang=clang, variant="debug")
	finally:
		if prev is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev
	env = dict(os.environ)
	env["DRIFT_RUNTIME_LINK_MODE"] = "archive"
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert str(cache_dir) in stderr
	assert "libdrift_rt.a" in stderr
	assert (cache_dir / "debug" / "libdrift_rt.a").exists()
	assert out.exists()
