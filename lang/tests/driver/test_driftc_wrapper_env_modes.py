# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from lang.driftc.env_flags import env_true
from lang.language_runtime import build_runtime_archive, runtime_archive_variant


def _repo_root() -> Path:
	return Path(__file__).resolve().parents[3]


_RUNNER_ENV_KEYS = {"DRIFT_MEMCHECK", "DRIFT_MASSIF", "DRIFT_ASAN", "DRIFT_OPTIMIZED"}


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
	assert "libdrift_rt_abi" in stderr
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
	assert "libdrift_rt_abi" in stderr
	from lang.language_runtime import runtime_archive_name
	assert (cache_dir / variant / runtime_archive_name()).exists()
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


def test_optimized_flag_adds_o2_to_clang(tmp_path: Path) -> None:
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
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
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
	env = _clean_env()
	env.pop("DRIFT_ASAN", None)
	env.pop("DRIFT_UBSAN", None)
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--optimized", "--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert "[driftc] link:" in stderr
	assert "-O2" in stderr
	# Policy: --optimized suppresses debug info by default.
	link_line = [l for l in stderr.splitlines() if "[driftc] link:" in l]
	assert link_line, "expected [driftc] link: line in stderr"
	assert " -g " not in link_line[0] and " -g\n" not in link_line[0]
	assert out.exists()


def test_driftc_wrapper_optimized_env_adds_o2_and_strips_debug(tmp_path: Path) -> None:
	"""DRIFT_OPTIMIZED=1 must thread `--optimized --no-debug-info` semantics into driftc.

	Mirrors test_optimized_flag_adds_o2_to_clang but env-driven, so the same
	test populations that honor sanitizer knobs (driver, codegen e2e, package
	consumer) get an orthogonal optimized lane without per-runner CLI plumbing.
	"""
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
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
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
	env = _clean_env()
	env.pop("DRIFT_ASAN", None)
	env.pop("DRIFT_UBSAN", None)
	env["DRIFT_OPTIMIZED"] = "1"
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert "[driftc] link:" in stderr
	assert "-O2" in stderr
	link_line = [l for l in stderr.splitlines() if "[driftc] link:" in l]
	assert link_line, "expected [driftc] link: line in stderr"
	assert " -g " not in link_line[0] and " -g\n" not in link_line[0]
	assert out.exists()


def test_driftc_wrapper_optimized_composes_with_asan(tmp_path: Path) -> None:
	"""DRIFT_ASAN=1 + DRIFT_OPTIMIZED=1 must yield BOTH -fsanitize=address AND -O2.

	The two knobs are orthogonal: sanitizers are not special-cased away when
	optimization is requested, and optimization is not special-cased away when
	a sanitizer is active.
	"""
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
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
	assert clang
	prev_cache = os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR")
	prev_asan = os.environ.get("DRIFT_ASAN")
	try:
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
		os.environ["DRIFT_ASAN"] = "1"
		build_runtime_archive(_repo_root(), clang=clang, variant="asan_optimized")
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
	env["DRIFT_OPTIMIZED"] = "1"
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	link_line = [l for l in stderr.splitlines() if "[driftc] link:" in l]
	assert link_line, "expected [driftc] link: line in stderr"
	# Both knobs must be present in the link command — neither replaces the other.
	assert "-fsanitize=address" in link_line[0]
	assert "-O2" in link_line[0]
	# Asan-optimized variant of the runtime archive must be selected.
	assert "asan_optimized" in stderr
	assert out.exists()


def test_runtime_archive_variant_composability() -> None:
	"""Pure-function regression: optimized must compose with each sanitizer variant."""
	from lang.language_runtime import runtime_archive_variant
	# Defaults
	assert runtime_archive_variant(debug_enabled=True, asan_enabled=False, alloc_track_enabled=False) == "debug"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, alloc_track_enabled=False, optimized=True) == "optimized"
	# Sanitizers alone
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=True, alloc_track_enabled=False) == "asan"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, ubsan_enabled=True, alloc_track_enabled=False) == "ubsan"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=True, ubsan_enabled=True, alloc_track_enabled=False) == "asan_ubsan"
	# Sanitizers composed with optimized
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=True, alloc_track_enabled=False, optimized=True) == "asan_optimized"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, ubsan_enabled=True, alloc_track_enabled=False, optimized=True) == "ubsan_optimized"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=True, ubsan_enabled=True, alloc_track_enabled=False, optimized=True) == "asan_ubsan_optimized"
	# alloc_track stays exclusive (current contract).
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, alloc_track_enabled=True, optimized=True) == "alloc_track"


def test_optimized_debug_info_override(tmp_path: Path) -> None:
	"""--optimized --debug-info re-enables debug info (explicit override)."""
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
	cache_dir = tmp_path / "runtime_cache"
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
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
	env = _clean_env()
	env.pop("DRIFT_ASAN", None)
	env.pop("DRIFT_UBSAN", None)
	env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(cache_dir)
	cp = _run_wrapper(["--optimized", "--debug-info", "--target-word-bits", "64", "-M", str(tmp_path), str(src), "-o", str(out)], env=env)
	assert cp.returncode == 0, cp.stderr
	stderr = cp.stderr or ""
	assert "[driftc] link:" in stderr
	assert "-O2" in stderr
	# Explicit --debug-info restores -g even with --optimized.
	assert "-g" in stderr
	assert out.exists()


def test_optimized_runtime_archive_variant() -> None:
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, alloc_track_enabled=False, optimized=True) == "optimized"
	assert runtime_archive_variant(debug_enabled=True, asan_enabled=False, alloc_track_enabled=False, optimized=True) == "optimized"
	# DRIFT_OPTIMIZED is orthogonal: it composes with sanitizer variants
	# rather than being suppressed by them.
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=True, alloc_track_enabled=False, optimized=True) == "asan_optimized"
	# alloc_track stays exclusive (instrumentation wraps libc allocators).
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, alloc_track_enabled=True, optimized=True) == "alloc_track"
	assert runtime_archive_variant(debug_enabled=False, asan_enabled=False, alloc_track_enabled=False, optimized=False) == "default"


def test_build_runtime_archive_optimized(tmp_path: Path) -> None:
	clang = subprocess.run(["/bin/bash", "-lc", "command -v clang"], text=True, capture_output=True).stdout.strip()
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
	assert "optimized/libdrift_rt_abi" in str(archive)
