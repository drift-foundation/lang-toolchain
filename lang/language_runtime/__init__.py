# Helper to list/build runtime C artifacts for linking.
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


def _check_supported_target() -> None:
	"""Verify the build target is supported by the VT runtime."""
	if sys.platform != "linux" or platform.machine() != "x86_64":
		raise RuntimeError(
			"Drift VT runtime requires x86_64 Linux "
			f"(current: {sys.platform}/{platform.machine()})"
		)


def get_runtime_sources(root: Path) -> List[Path]:
	_check_supported_target()
	base = root / "lang" / "language_runtime"
	runtime = root / "lang" / "compiler_infra"
	return [
		# Deterministic float formatting (Ryu) for Drift `Float` once supported.
		base / "ryu_d2s.c",
		base / "alloc_track_runtime.c",
		base / "array_runtime.c",
		base / "string_runtime.c",
		base / "argv_runtime.c",
		base / "console_runtime.c",
		base / "posix" / "atomic_runtime.c",
		base / "posix" / "io_runtime.c",
		base / "posix" / "thread_runtime.c",
		base / "posix" / "drift_context.S",
		base / "posix" / "assert_runtime.c",
		base / "random_runtime.c",
		base / "env_runtime.c",
		# ABI version stamp for link-time compatibility guard.
		# Also carries the paired runtime identity sentinels (variant gated
		# by -DDRIFT_RT_MODE_DEBUG) so they ride into every linked binary
		# alongside the always-pulled ABI symbol.
		base / "abi_version_stamp.c",
		# Diagnostic/Error runtime lives alongside lang/ for now; include it so
		# e2e codegen links DV/exception helpers.
		runtime / "diagnostic_runtime.c",
		runtime / "error_dummy.c",
	]


def get_runtime_include_dirs(root: Path) -> List[Path]:
	return [
		root / "lang" / "language_runtime",
		root / "lang" / "compiler_infra",
	]


_VALID_VARIANTS = {
	"default", "debug", "alloc_track",
	"asan", "ubsan", "asan_ubsan",
}


def runtime_archive_variant(
	*,
	debug_style: bool,
	asan_enabled: bool,
	alloc_track_enabled: bool,
	ubsan_enabled: bool = False,
) -> str:
	"""Pick the on-disk runtime archive variant for a given lane.

	Vocabulary contract (do not deviate):
	  - ``debug_style=False`` → "default" (the production "normal" lane).
	  - ``debug_style=True``  → "debug"   (the explicit `_debug` opt-in lane,
	                                       selected by `drift build --debug`
	                                       or `DRIFT_DEBUG=1`).

	Sanitizer / alloc_track variants are internal test-mode lanes that
	take precedence over the normal/debug-style binary distinction; they
	ride on the `__drift_rt_mode_normal` sentinel.  alloc_track stays
	exclusive (instrumentation that wraps libc allocators).
	"""
	if alloc_track_enabled:
		return "alloc_track"
	if asan_enabled and ubsan_enabled:
		return "asan_ubsan"
	if asan_enabled:
		return "asan"
	if ubsan_enabled:
		return "ubsan"
	if debug_style:
		return "debug"
	return "default"


def runtime_archive_cache_root(root: Path) -> Path:
	cache_dir = (os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR") or "").strip()
	if cache_dir:
		return Path(cache_dir)
	build_root = (os.environ.get("DRIFT_RUNTIME_BUILD_ROOT") or "").strip()
	if build_root:
		return Path(build_root) / "runtime_libs"
	return root / "build" / "runtime_libs"


def runtime_archive_name(variant: str = "default") -> str:
	"""Return the ABI-versioned runtime archive filename for ``variant``.

	Embedding the ABI version in the filename ensures stale cached archives
	(from prior ABI versions) are never linked — the compiler asks for
	``libdrift_rt_abi7.a`` and the linker will not find ``libdrift_rt_abi6.a``.

	The dual-runtime contract requires the explicit `_debug` infix on the
	debug-style variant filename so production releases can spot it by
	inspection.  All other variants share the unsuffixed name; sanitizer
	and alloc_track variants are internal test modes that ride on the
	normal filename.
	"""
	from lang.versions import DRIFT_RT_ABI_VERSION
	if variant == "debug":
		return f"libdrift_rt_debug_abi{DRIFT_RT_ABI_VERSION}.a"
	return f"libdrift_rt_abi{DRIFT_RT_ABI_VERSION}.a"


def runtime_archive_path(root: Path, *, variant: str) -> Path:
	if variant not in _VALID_VARIANTS:
		raise ValueError(f"unknown runtime archive variant '{variant}'")
	return runtime_archive_cache_root(root) / variant / runtime_archive_name(variant)


def _runtime_deps(root: Path) -> List[Path]:
	base = root / "lang" / "language_runtime"
	infra = root / "lang" / "compiler_infra"
	deps = get_runtime_sources(root)
	deps.extend(base.rglob("*.h"))
	deps.extend(base.rglob("*.S"))
	deps.extend(infra.glob("*.h"))
	return deps


def _needs_rebuild(archive_path: Path, deps: List[Path]) -> bool:
	if not archive_path.exists():
		return True
	try:
		archive_mtime = archive_path.stat().st_mtime
	except OSError:
		return True
	for dep in deps:
		try:
			if dep.stat().st_mtime > archive_mtime:
				return True
		except OSError:
			return True
	return False


def build_runtime_archive(root: Path, *, clang: str, variant: str) -> Path:
	if variant not in _VALID_VARIANTS:
		raise ValueError(f"unknown runtime archive variant '{variant}'")
	ar_bin = shutil.which("llvm-ar") or shutil.which("ar")
	if ar_bin is None:
		raise RuntimeError("ar/llvm-ar not available")
	cache_root = runtime_archive_cache_root(root)
	build_root = cache_root / variant
	obj_dir = build_root / "objs"
	archive_path = build_root / runtime_archive_name(variant)
	lock_path = build_root / ".build.lock"
	deps = _runtime_deps(root)
	# ABI version constant also drives rebuild (change → force recompile).
	abi_ver_file = root / "lang" / "driftc" / "driftc_versions.py"
	if abi_ver_file.exists():
		deps.append(abi_ver_file)
	if not _needs_rebuild(archive_path, deps):
		return archive_path
	build_root.mkdir(parents=True, exist_ok=True)
	obj_dir.mkdir(parents=True, exist_ok=True)

	# Serialize archive creation so parallel e2e workers do not race.
	with lock_path.open("a+", encoding="utf-8") as lock_file:
		try:
			import fcntl
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
		except Exception:
			pass
		if not _needs_rebuild(archive_path, deps):
			return archive_path
		# Read ABI version from the single source of truth.
		from lang.driftc.driftc_versions import DRIFT_RT_ABI_VERSION
		include_dirs = get_runtime_include_dirs(root)
		# Dual-runtime workstream (step 4 default flip): the unsuffixed
		# `default` variant is the production "normal" runtime — built with
		# -O2 and no debug info.  The explicit `debug` variant is the
		# `_debug` opt-in (-g) and carries the paired identity sentinel via
		# -DDRIFT_RT_MODE_DEBUG.  Sanitizer/alloc_track variants are
		# internal test modes that ride on the `__drift_rt_mode_normal`
		# sentinel; their cflags are unchanged.
		cflags: list[str] = []
		cdefs: list[str] = [f"-DDRIFT_RT_ABI_VERSION={DRIFT_RT_ABI_VERSION}"]
		if variant == "default":
			cflags.extend(["-O2"])
		elif variant == "debug":
			cflags.extend(["-g"])
			cdefs.append("-DDRIFT_RT_MODE_DEBUG=1")
		elif variant == "asan":
			cflags.extend(["-fsanitize=address", "-g"])
		elif variant == "ubsan":
			cflags.extend(["-fsanitize=undefined", "-fno-sanitize-recover=undefined", "-g"])
		elif variant == "asan_ubsan":
			cflags.extend(["-fsanitize=address", "-fsanitize=undefined", "-fno-sanitize-recover=undefined", "-g"])
		elif variant == "alloc_track":
			cdefs.extend(["-DDRIFT_ALLOC_WRAP_ENABLED=1"])

		obj_paths: list[Path] = []
		for src in get_runtime_sources(root):
			rel = src.relative_to(root)
			src_str = str(rel)
			if src_str.endswith(".S"):
				obj_name = src_str.replace("/", "__").replace(".S", ".o")
				lang_flag = ["-x", "assembler-with-cpp"]
			else:
				obj_name = src_str.replace("/", "__").replace(".c", ".o")
				lang_flag = ["-x", "c"]
			obj_path = obj_dir / obj_name
			cmd = [clang, "-c", *lang_flag, *cflags, *cdefs]
			for inc in include_dirs:
				cmd.extend(["-I", str(inc)])
			cmd.extend(["-o", str(obj_path), str(src)])
			res = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
			if res.returncode != 0:
				msg = res.stderr.strip() or res.stdout.strip() or "clang failed"
				raise RuntimeError(f"runtime archive compile failed ({src}): {msg}")
			obj_paths.append(obj_path)

		ar_cmd = [ar_bin, "rcs", str(archive_path), *(str(p) for p in obj_paths)]
		ar_res = subprocess.run(ar_cmd, capture_output=True, text=True, cwd=root)
		if ar_res.returncode != 0:
			msg = ar_res.stderr.strip() or ar_res.stdout.strip() or "ar failed"
			raise RuntimeError(f"runtime archive build failed: {msg}")
	return archive_path


def runtime_archive_mode() -> str:
	mode = (os.environ.get("DRIFT_RUNTIME_LINK_MODE") or "archive").strip().lower()
	if mode not in {"archive", "source"}:
		return "archive"
	return mode


__all__ = [
	"build_runtime_archive",
	"get_runtime_include_dirs",
	"get_runtime_sources",
	"runtime_archive_cache_root",
	"runtime_archive_path",
	"runtime_archive_mode",
	"runtime_archive_variant",
]
