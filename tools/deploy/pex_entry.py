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
  <dist>/lib/stdlib/       — stdlib package + v1 trust sidecars
                            (std.dmp + std.author-claim + std.cert-claim.<kid>.json)

The PEX itself bundles the Python interpreter (--scie eager) and third-party
dependencies (lark, llvmlite, cryptography, zstandard).  The compiler sources remain in
lib/compiler/ so that __file__-relative resource lookups (grammar.lark,
core_trust_v1.json, C/H/S sources for runtime archive rebuilds) continue to
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
	exe = Path(os.path.realpath(sys.argv[0]))
	dist_root = exe.parent.parent

	# Prepend compiler sources to sys.path so lang.driftc is importable.
	# This directory also contains the .lark grammar, core_trust_v1.json
	# (the v1 role-tagged core trust store), and C/H/S runtime source
	# files — all resolved via __file__ relative paths.
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

	# Runtime archive resolution: point driftc directly at the deployment
	# tree's `lib/runtime/` directory.  The .a files there are pre-built,
	# signed alongside the rest of the toolchain, and serve as the
	# single source of truth for this deployment.  `ld.gold` opens the
	# archive O_RDONLY (verified via strace), so a 0444 read-only install
	# tree is fine — no copy, no chmod, no user-local cache.
	#
	# Why no `~/.cache/drift/runtime/`: a process-wide writable cache that
	# survives toolchain upgrades is a silent-Frankenstein hazard.  Each
	# deployment must be self-contained; two installed toolchains coexist
	# only if neither writes into shared user state.  Filed 2026-05-22.
	#
	# An operator-provided DRIFT_RUNTIME_LIB_CACHE_DIR (typically a CI
	# scratch dir under /tmp) still wins — that's an explicit override,
	# not a default.  The fall-through to `lib/runtime/` only fires when
	# the env var is unset.
	if not os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR"):
		_install_runtime = dist_root / "lib" / "runtime"
		if _install_runtime.is_dir():
			os.environ["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(_install_runtime)

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

	# Forward optional user trust store.  Exists-before-injecting:
	#   - DRIFT_TRUST_STORE set + file exists -> forward.
	#   - DRIFT_TRUST_STORE set + file missing -> fail loud (env was
	#     an explicit intent; silently dropping it masked the cert-
	#     host net-tls bug).
	#   - DRIFT_TRUST_STORE unset -> do nothing.  driftc has its own
	#     `~/.config/drift/trust.json` user-trust merge (gated on
	#     exists in `lang/driftc/driftc.py`); conflating that into
	#     a `--trust-store` flag here would forward a non-existent
	#     path to driftc on a clean host.
	trust_store = os.environ.get("DRIFT_TRUST_STORE", "")
	if trust_store:
		trust_path = Path(trust_store).expanduser()
		if not trust_path.is_file():
			print(
				f"error: $DRIFT_TRUST_STORE points at a path that does "
				f"not exist: {trust_path}",
				file=sys.stderr,
			)
			print(
				"hint: unset DRIFT_TRUST_STORE to let driftc fall through "
				"to its default user-trust layer, or repair the path.",
				file=sys.stderr,
			)
			sys.exit(1)
		args = ["--trust-store", str(trust_path)] + args

	from lang.driftc.driftc import main as driftc_main

	sys.exit(driftc_main(args))


if __name__ == "__main__":
	main()
