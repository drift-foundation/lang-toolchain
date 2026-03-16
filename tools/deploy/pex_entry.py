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
dependencies (lark, llvmlite, cryptography).  The compiler sources remain in
lib/compiler/ so that __file__-relative resource lookups (grammar.lark,
core_trust.json, C/H/S sources for runtime archive rebuilds) continue to
resolve correctly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


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

	# Point runtime archive cache at deployed pre-built archives.
	os.environ.setdefault(
		"DRIFT_RUNTIME_LIB_CACHE_DIR",
		str(dist_root / "lib" / "runtime"),
	)

	# Build driftc argument list.
	args = list(sys.argv[1:])

	# Inject --package-root for the signed stdlib package.
	stdlib_dir = dist_root / "lib" / "stdlib"
	if stdlib_dir.is_dir():
		args = ["--package-root", str(stdlib_dir)] + args

	# Forward optional user trust store.
	trust_store = os.environ.get("DRIFT_TRUST_STORE", "")
	if trust_store and Path(trust_store).is_file():
		args = ["--trust-store", trust_store] + args

	from lang.driftc.driftc import main as driftc_main

	sys.exit(driftc_main(args))


if __name__ == "__main__":
	main()
