#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy-time PEX entry point for driftc.

This module is the console_script entry point baked into the PEX --scie eager
executable that ships as bin/driftc in a deployed Drift distribution.

It resolves the deploy tree layout relative to the executable's location,
configures the environment, then delegates to lang.driftc.driftc.main().

Resource layout assumed (relative to the executable at <dist>/bin/driftc):

  <dist>/lib/compiler/     — compiler Python sources (lang/ tree) + C/H/S files
  <dist>/lib/runtime/      — pre-built runtime archives by variant
  <dist>/lib/stdlib/       — signed stdlib package (std.dmp + std.sig)

The PEX itself bundles the Python interpreter (--scie eager) and third-party
dependencies (lark, llvmlite, cryptography, zstandard).  The compiler sources remain in
lib/compiler/ so that __file__-relative resource lookups (grammar.lark,
core_trust.json, C/H/S sources for runtime archive rebuilds) continue to
resolve correctly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _peek_stdlib_dep(stdlib_dir: Path) -> str | None:
	"""Return 'std@<version>' by peeking at the stdlib .dmp/.zdmp manifest."""
	try:
		from lang.driftc.packages.dmir_pkg_v0 import peek_package_id_and_version
		for p in sorted(stdlib_dir.iterdir()):
			if p.suffix in (".zdmp", ".dmp") and p.is_file():
				result = peek_package_id_and_version(p)
				if result is not None:
					return f"{result[0]}@{result[1]}"
	except Exception:
		pass
	return None


def main() -> None:
	# Resolve the deploy tree root from the real path of this executable.
	# For scie binaries, sys.argv[0] is the path to the scie executable.
	# Resolve symlinks (e.g. <dest>/current/bin/driftc -> ../drift-V/bin/driftc).
	exe = Path(os.path.realpath(sys.argv[0]))
	dist_root = exe.parent.parent

	# Prepend compiler sources to sys.path so lang.driftc is importable.
	# This directory also contains the .lark grammar, core_trust.json, and
	# C/H/S runtime source files — all resolved via __file__ relative paths.
	compiler_lib = str(dist_root / "lib" / "compiler")
	if compiler_lib not in sys.path:
		sys.path.insert(0, compiler_lib)

	# Remove CWD from sys.path to prevent ambient module shadowing
	# (mirrors PYTHONSAFEPATH=1 from the old wrapper).
	cwd = os.path.realpath(os.getcwd())
	sys.path = [p for p in sys.path if os.path.realpath(p) != cwd]
	# Re-insert compiler_lib in case it was the CWD.
	if compiler_lib not in sys.path:
		sys.path.insert(0, compiler_lib)

	# Runtime archive resolution: use a writable user-local cache so that
	# missing variants (e.g. asan) can be built on demand even when the
	# install tree is read-only.  Pre-built archives from the install tree
	# are copied into the cache on first run.
	#
	# Copies (not symlinks) are used because symlinks break when the install
	# tree is relocated or when tests use ephemeral temp directories.
	import shutil as _shutil
	from lang.language_runtime import runtime_archive_name as _rt_ar_name
	_ar_name = _rt_ar_name()
	_install_runtime = dist_root / "lib" / "runtime"
	if "DRIFT_RUNTIME_LIB_CACHE_DIR" not in os.environ:
		_cache_runtime = Path.home() / ".cache" / "drift" / "runtime"
		_cache_runtime.mkdir(parents=True, exist_ok=True)
		# Seed pre-built archives from the install tree.
		if _install_runtime.is_dir():
			for _variant_dir in sorted(_install_runtime.iterdir()):
				if not _variant_dir.is_dir():
					continue
				_archive = _variant_dir / _ar_name
				if not _archive.is_file():
					continue
				_cache_variant = _cache_runtime / _variant_dir.name
				_cache_variant.mkdir(parents=True, exist_ok=True)
				_cache_archive = _cache_variant / _ar_name
				if not _cache_archive.is_file():
					try:
						_shutil.copy2(str(_archive), str(_cache_archive))
					except OSError:
						pass  # Best-effort; build will recreate if needed.
		os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(_cache_runtime)

	# Build driftc argument list.
	args = list(sys.argv[1:])

	# Inject --package-root and --dep for the signed stdlib package.
	# The --dep flag satisfies the 0.27.72+ contract that --package-root
	# requires explicit --dep entries for every consumed package.
	stdlib_dir = dist_root / "lib" / "stdlib"
	if stdlib_dir.is_dir():
		stdlib_prefix = ["--package-root", str(stdlib_dir)]
		_stdlib_dep = _peek_stdlib_dep(stdlib_dir)
		if _stdlib_dep:
			stdlib_prefix.extend(["--dep", _stdlib_dep])
		args = stdlib_prefix + args

	# Forward optional user trust store.
	trust_store = os.environ.get("DRIFT_TRUST_STORE", "")
	if trust_store and Path(trust_store).is_file():
		args = ["--trust-store", trust_store] + args

	from lang.driftc.driftc import main as driftc_main

	sys.exit(driftc_main(args))


if __name__ == "__main__":
	main()
