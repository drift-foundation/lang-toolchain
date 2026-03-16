#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy-time PEX entry point for drift-deploy.

This module is the console_script entry point baked into the PEX --scie eager
executable that ships as bin/drift-deploy in a deployed Drift distribution.

It resolves the deploy tree layout relative to the executable's location,
configures the environment, then delegates to tools.drift_deploy.drift_deploy.run().

Resource layout assumed (relative to the executable at <dist>/bin/drift-deploy):

  <dist>/lib/compiler/     — compiler Python sources (lang/ tree)

The PEX itself bundles the Python interpreter (--scie eager), third-party
dependencies (cryptography, zstandard), and the tools.drift_deploy package.
The compiler sources in lib/compiler/ provide deferred imports used by
drift-deploy at runtime (lang.drift.sign, lang.driftc.packages.zdmp, etc.).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
	# Resolve the deploy tree root from the real path of this executable.
	exe = Path(os.path.realpath(sys.argv[0]))
	dist_root = exe.parent.parent

	# Prepend compiler sources to sys.path so lang.* is importable.
	# drift-deploy uses deferred imports from lang.drift.sign,
	# lang.drift.crypto, lang.driftc.packages.zdmp, and
	# lang.driftc.packages.dmir_pkg_v0.
	compiler_lib = str(dist_root / "lib" / "compiler")
	if compiler_lib not in sys.path:
		sys.path.insert(0, compiler_lib)

	# Remove CWD from sys.path to prevent ambient module shadowing.
	cwd = os.path.realpath(os.getcwd())
	sys.path = [p for p in sys.path if os.path.realpath(p) != cwd]
	# Re-insert compiler_lib in case it was the CWD.
	if compiler_lib not in sys.path:
		sys.path.insert(0, compiler_lib)

	from tools.drift_deploy.drift_deploy import run

	sys.exit(run())


if __name__ == "__main__":
	main()
