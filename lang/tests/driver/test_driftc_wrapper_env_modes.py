# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from lang.driftc.env_flags import env_true
from lang.language_runtime import build_runtime_archive, runtime_archive_variant


def _repo_root() -> Path:
	return Path(__file__).resolve().parents[3]


def _run_wrapper(args: list[str], *, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
	wrapper = _repo_root() / "bin" / "driftc"
	return subprocess.run(["/bin/bash", str(wrapper), *args], text=True, capture_output=True, env=env, cwd=cwd)


def test_driftc_wrapper_rejects_memcheck_and_massif_in_direct_mode() -> None:
	env = dict(os.environ)
	env.pop("DRIFT_ASAN", None)
	env["DRIFT_MEMCHECK"] = "1"
	cp = _run_wrapper(["--help"], env=env)
	assert cp.returncode != 0
	assert "runner-only" in (cp.stderr or "")

	env = dict(os.environ)
	env.pop("DRIFT_ASAN", None)
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
	variant = "asan" if env_true("DRIFT_ASAN", env=os.environ) else "debug"
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	prev_asan = os.environ.get("DRIFT_ASAN")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		if variant == "debug":
			os.environ.pop("DRIFT_ASAN", None)
		else:
			os.environ["DRIFT_ASAN"] = "1"
		clang = subprocess.run(["/bin/bash", "-lc", "command -v clang-15 || command -v clang"], text=True, capture_output=True).stdout.strip()
		assert clang
		build_runtime_archive(_repo_root(), clang=clang, variant=variant)
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
		if prev_asan is None:
			os.environ.pop("DRIFT_ASAN", None)
		else:
			os.environ["DRIFT_ASAN"] = prev_asan
	env = dict(os.environ)
	env["DRIFT_RUNTIME_LINK_MODE"] = "archive"
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert str(cache_dir) in stderr
	assert "libdrift_rt.a" in stderr
	assert (cache_dir / variant / "libdrift_rt.a").exists()
	assert out.exists()


def test_driftc_wrapper_relative_output_from_non_repo_cwd(tmp_path: Path) -> None:
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
	rel_out = Path("out_rel.bin")
	env = dict(os.environ)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(rel_out)], env=env, cwd=tmp_path)
	assert cp.returncode == 0, cp.stderr
	assert (tmp_path / rel_out).exists()


def test_optimized_flag_adds_o2_to_clang(tmp_path: Path) -> None:
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
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang-15 || command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		build_runtime_archive(_repo_root(), clang=clang, variant="optimized")
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
	env = dict(os.environ)
	env.pop("DRIFT_ASAN", None)
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--optimized", "--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert "[driftc] link:" in stderr
	assert "-O2" in stderr
	# Policy: --optimized keeps debug info by default (debug_enabled=True).
	assert "-g" in stderr
	assert out.exists()


def test_optimized_no_debug_info(tmp_path: Path) -> None:
	"""--optimized --no-debug-info takes the release code path (no -g)."""
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
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang-15 || command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		build_runtime_archive(_repo_root(), clang=clang, variant="optimized")
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
	env = dict(os.environ)
	env.pop("DRIFT_ASAN", None)
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--optimized", "--no-debug-info", "--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	link_line = [l for l in stderr.splitlines() if "[driftc] link:" in l]
	assert link_line, "expected [driftc] link: line in stderr"
	assert "-O2" in link_line[0]
	# No -g in the link command when --no-debug-info is passed.
	assert " -g " not in link_line[0] and " -g\n" not in link_line[0]
	assert out.exists()


def test_optimized_runtime_archive_variant() -> None:
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, alloc_track_enabled=False, optimized=True) == "optimized"
	assert runtime_archive_variant(debug_enabled=True, asan_enabled=False, alloc_track_enabled=False, optimized=True) == "optimized"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=True, alloc_track_enabled=False, optimized=True) == "asan"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, alloc_track_enabled=True, optimized=True) == "alloc_track"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, alloc_track_enabled=False, optimized=False) == "default"


def test_build_runtime_archive_optimized(tmp_path: Path) -> None:
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang-15 || command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	cache_dir = tmp_path / "runtime_cache"
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		archive = build_runtime_archive(_repo_root(), clang=clang, variant="optimized")
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
	assert archive.exists()
	assert str(archive).endswith("optimized/libdrift_rt.a")
