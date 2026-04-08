# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lang.driftc.env_flags import env_true
from lang.language_runtime import build_runtime_archive, runtime_archive_variant


@pytest.fixture(autouse=True)
def _opt_out_of_lane_audit(tmp_path: Path) -> None:
	"""Mark every tmp_path in this file as audit-skip.

	The wrapper env-mode tests intentionally exercise lane combinations in
	isolated subprocess envs — they are contract-pinning regressions for
	the dual-runtime selection plumbing, not consumers of the parent
	session's lane.  Their build artifacts are by design orthogonal to
	whatever DRIFT_DEBUG state the outer pytest session was launched in,
	so the conftest sentinel audit must skip them.
	"""
	(tmp_path / ".drift-lane-audit-skip").write_text("", encoding="utf-8")


def _repo_root() -> Path:
	return Path(__file__).resolve().parents[3]


_RUNNER_ENV_KEYS = {"DRIFT_MEMCHECK", "DRIFT_MASSIF", "DRIFT_ASAN", "DRIFT_DEBUG"}


def _clean_env() -> dict[str, str]:
	"""Return os.environ without suite-wide runner flags.

	Tests that need specific runner flags set them explicitly after calling
	this.  This prevents suite-wide DRIFT_MEMCHECK=1 (etc.) from leaking
	into wrapper tests that are not about those flags.
	"""
	return {k: v for k, v in os.environ.items() if k not in _RUNNER_ENV_KEYS}


def _run_wrapper(args: list[str], *, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
	wrapper = _repo_root() / "bin" / "driftc"
	return subprocess.run(["/bin/bash", str(wrapper), *args], text=True, capture_output=True, env=env, cwd=cwd)


def test_driftc_wrapper_rejects_memcheck_and_massif_in_direct_mode() -> None:
	env = _clean_env()
	env.pop("DRIFT_ASAN", None)
	env.pop("DRIFT_UBSAN", None)
	env["DRIFT_MEMCHECK"] = "1"
	cp = _run_wrapper(["--help"], env=env)
	assert cp.returncode != 0
	assert "runner-only" in (cp.stderr or "")

	env = _clean_env()
	env.pop("DRIFT_ASAN", None)
	env.pop("DRIFT_UBSAN", None)
	env["DRIFT_MASSIF"] = "1"
	cp = _run_wrapper(["--help"], env=env)
	assert cp.returncode != 0
	assert "runner-only" in (cp.stderr or "")


def test_driftc_wrapper_asan_adds_sanitize_flags(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
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
	env = _clean_env()
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
				"module main;",
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
	env = _clean_env()
	env["DRIFT_RUNTIME_LINK_MODE"] = "archive"
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	# Variant-aware archive filename: the debug-style variant carries an
	# explicit `_debug` infix (libdrift_rt_debug_abi<N>.a), so the assertion
	# checks the common `libdrift_rt` prefix shared by all variants.
	assert "libdrift_rt" in stderr
	assert out.exists()


def test_driftc_wrapper_runtime_archive_mode_respects_custom_cache_dir(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
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
		clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
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
	env = _clean_env()
	env["DRIFT_RUNTIME_LINK_MODE"] = "archive"
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert str(cache_dir) in stderr
	# Variant-aware archive filename: the debug-style variant carries an
	# explicit `_debug` infix (libdrift_rt_debug_abi<N>.a), so the assertion
	# checks the common `libdrift_rt` prefix shared by all variants.
	assert "libdrift_rt" in stderr
	from lang.language_runtime import runtime_archive_name
	assert (cache_dir / variant / runtime_archive_name(variant)).exists()
	assert out.exists()


def test_driftc_wrapper_relative_output_from_non_repo_cwd(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
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
	env = _clean_env()
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(rel_out)], env=env, cwd=tmp_path)
	assert cp.returncode == 0, cp.stderr
	assert (tmp_path / rel_out).exists()


_TINY_MAIN = "\n".join(
	[
		"module main;",
		"import std.core;",
		"fn main() nothrow -> Int {",
		"\treturn 0;",
		"}",
		"",
	]
)


def _stage_default_runtime_cache(tmp_path: Path) -> Path:
	"""Build the default runtime archive into a per-test cache and return the cache root."""
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		build_runtime_archive(_repo_root(), clang=clang, variant="default")
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
	return cache_dir


def test_default_lane_links_normal_runtime_with_o2(tmp_path: Path) -> None:
	"""Default driftc invocation (no DRIFT_DEBUG, no flag) → normal lane: -O2, no -g."""
	src = tmp_path / "main.drift"
	src.write_text(_TINY_MAIN, encoding="utf-8")
	out = tmp_path / "a.out"
	cache_dir = _stage_default_runtime_cache(tmp_path)
	env = _clean_env()
	env.pop("DRIFT_ASAN", None)
	env.pop("DRIFT_UBSAN", None)
	env.pop("DRIFT_DEBUG", None)
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert "[driftc] link:" in stderr
	link_line = [l for l in stderr.splitlines() if "[driftc] link:" in l][0]
	assert "-O2" in link_line
	assert " -g " not in link_line and " -g\n" not in link_line
	# Default lane links the unsuffixed runtime archive (no `_debug` infix).
	assert "libdrift_rt_abi" in link_line
	assert "libdrift_rt_debug_abi" not in link_line
	assert out.exists()


def test_drift_debug_env_selects_debug_runtime(tmp_path: Path) -> None:
	"""DRIFT_DEBUG=1 → debug-style lane: no -O2, links the `_debug` runtime archive."""
	src = tmp_path / "main.drift"
	src.write_text(_TINY_MAIN, encoding="utf-8")
	out = tmp_path / "a.out"
	# Stage BOTH variants in the cache so the runtime resolution has a real
	# choice to make — that is the contract being pinned.
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		build_runtime_archive(_repo_root(), clang=clang, variant="default")
		build_runtime_archive(_repo_root(), clang=clang, variant="debug")
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
	env = _clean_env()
	env.pop("DRIFT_ASAN", None)
	env.pop("DRIFT_UBSAN", None)
	env["DRIFT_DEBUG"] = "1"
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	link_line = [l for l in stderr.splitlines() if "[driftc] link:" in l][0]
	# Debug-style lane: no -O2, links the `_debug`-infix runtime archive.
	assert "-O2" not in link_line
	assert "libdrift_rt_debug_abi" in link_line
	assert out.exists()


def test_drift_debug_composes_with_asan(tmp_path: Path) -> None:
	"""DRIFT_ASAN=1 + DRIFT_DEBUG=1: sanitizers take precedence at variant selection.

	Sanitizer test modes are internal: they ride on the normal sentinel and
	override variant selection regardless of DRIFT_DEBUG.  -O2 is suppressed
	because DRIFT_DEBUG=1 is in effect; -fsanitize=address still applies.
	"""
	src = tmp_path / "main.drift"
	src.write_text(_TINY_MAIN, encoding="utf-8")
	out = tmp_path / "a.out"
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	prev_asan = os.environ.get("DRIFT_ASAN")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		os.environ["DRIFT_ASAN"] = "1"
		build_runtime_archive(_repo_root(), clang=clang, variant="asan")
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
		if prev_asan is None:
			os.environ.pop("DRIFT_ASAN", None)
		else:
			os.environ["DRIFT_ASAN"] = prev_asan
	env = _clean_env()
	env["DRIFT_ASAN"] = "1"
	env["DRIFT_DEBUG"] = "1"
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	link_line = [l for l in stderr.splitlines() if "[driftc] link:" in l][0]
	# Sanitizer flag still present; sanitizer variant selected (not debug).
	assert "-fsanitize=address" in link_line
	assert "/asan/libdrift_rt_abi" in link_line
	# DRIFT_DEBUG=1 suppresses -O2 even when stacked with a sanitizer.
	assert "-O2" not in link_line
	assert out.exists()


def test_runtime_archive_variant_polarity() -> None:
	"""Pure-function regression: dual-runtime polarity + sanitizer precedence."""
	from lang.language_runtime import runtime_archive_variant
	# Default: production "normal" lane.
	assert runtime_archive_variant(debug_style=False, asan_enabled=False, alloc_track_enabled=False) == "default"
	# Debug-style: explicit `_debug` opt-in.
	assert runtime_archive_variant(debug_style=True, asan_enabled=False, alloc_track_enabled=False) == "debug"
	# Sanitizers take precedence over the normal/debug-style distinction.
	assert runtime_archive_variant(debug_style=False, asan_enabled=True, alloc_track_enabled=False) == "asan"
	assert runtime_archive_variant(debug_style=True, asan_enabled=True, alloc_track_enabled=False) == "asan"
	assert runtime_archive_variant(debug_style=False, asan_enabled=False, ubsan_enabled=True, alloc_track_enabled=False) == "ubsan"
	assert runtime_archive_variant(debug_style=True, asan_enabled=False, ubsan_enabled=True, alloc_track_enabled=False) == "ubsan"
	assert runtime_archive_variant(debug_style=False, asan_enabled=True, ubsan_enabled=True, alloc_track_enabled=False) == "asan_ubsan"
	# alloc_track stays exclusive (instrumentation wraps libc allocators).
	assert runtime_archive_variant(debug_style=False, asan_enabled=False, alloc_track_enabled=True) == "alloc_track"
	assert runtime_archive_variant(debug_style=True, asan_enabled=False, alloc_track_enabled=True) == "alloc_track"


def test_drift_debug_with_explicit_debug_info(tmp_path: Path) -> None:
	"""DRIFT_DEBUG=1 + --debug-info: debug-style runtime AND DWARF emission.

	`-g` / `--debug-info` is orthogonal to runtime variant selection — it
	controls DWARF emission for the user's binary, independent of the
	dual-runtime lane choice.
	"""
	src = tmp_path / "main.drift"
	src.write_text(_TINY_MAIN, encoding="utf-8")
	out = tmp_path / "a.out"
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		build_runtime_archive(_repo_root(), clang=clang, variant="debug")
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
	env = _clean_env()
	env.pop("DRIFT_ASAN", None)
	env.pop("DRIFT_UBSAN", None)
	env["DRIFT_DEBUG"] = "1"
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--debug-info", "--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	link_line = [l for l in stderr.splitlines() if "[driftc] link:" in l][0]
	# Debug-style lane: -O2 suppressed, debug runtime selected, -g present.
	assert "-O2" not in link_line
	assert "libdrift_rt_debug_abi" in link_line
	assert "-g" in link_line
	assert out.exists()


def test_build_runtime_archive_default_carries_o2_and_normal_sentinel(tmp_path: Path) -> None:
	"""The default runtime archive is built with -O2 and exports __drift_rt_mode_normal."""
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	cache_dir = tmp_path / "runtime_cache"
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		archive = build_runtime_archive(_repo_root(), clang=clang, variant="default")
	finally:
		if prev_cache is None:
			os.environ.pop("DRIFT_RUNTIME_LIB_CACHE_DIR", None)
		else:
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = prev_cache
	assert archive.exists()
	assert "default/libdrift_rt_abi" in str(archive)
	# Sentinel symbol surface: normal sentinel present, debug sentinel absent.
	nm = subprocess.run(["nm", str(archive)], text=True, capture_output=True)
	assert nm.returncode == 0
	assert "__drift_rt_mode_normal" in nm.stdout
	assert "__drift_rt_mode_debug" not in nm.stdout
