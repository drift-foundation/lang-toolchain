#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Drift distribution deploy — Python orchestrator.

Builds a versioned, self-contained Drift distribution:
  DEST/drift-<VERSION>+abi<ABI>/  (bin, lib, doc, examples)
  DEST/current -> drift-<VERSION>+abi<ABI>  (atomic symlink)

Steps:
  1. Build PEX --scie eager executables (bin/driftc, bin/drift)
  2. Bundle compiler sources, runtime archives, docs
  3. Build + sign stdlib package, core trust store
  4. Compile + run smoke test using deployed paths
  5. Atomic publish + symlink switch

Requires DRIFT_SIGN_KEY_FILE or DRIFT_SIGN_KEY_CMD for stdlib signing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from tools.deploy.steps.bundle import (
	RUNTIME_VARIANTS,
	bundle_compiler,
	bundle_docs_and_examples,
	bundle_runtime_archives,
)
from tools.deploy.steps.metadata import load_deploy_metadata
from tools.deploy.steps.pex import build_drift_pex, build_driftc_pex
from tools.deploy.steps.publish import (
	generate_manifest,
	publish_atomic,
	switch_current_symlink,
)
from tools.deploy.steps.smoke import run_smoke_test
from tools.deploy.steps.stdlib import build_and_install_stdlib


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(
		prog="deploy",
		description="Build and publish a versioned Drift distribution.",
	)
	parser.add_argument("--dest", type=Path, required=True,
		help="Deploy destination root")
	parser.add_argument("--python", type=Path, default=None,
		help="Python interpreter (optional; for smoke/prereq checks)")
	args = parser.parse_args(argv)

	repo_root = Path(__file__).resolve().parent.parent.parent
	dest = args.dest.expanduser().resolve()

	# ── Signing key ──────────────────────────────────────────────────
	if not os.environ.get("DRIFT_SIGN_KEY_FILE") and not os.environ.get("DRIFT_SIGN_KEY_CMD"):
		print("error: package signing key required.", file=sys.stderr)
		print("  set DRIFT_SIGN_KEY_FILE=/path/to/seed.key", file=sys.stderr)
		print("  or  DRIFT_SIGN_KEY_CMD=\"command\"", file=sys.stderr)
		return 1

	# ── Python override ──────────────────────────────────────────────
	if args.python:
		os.environ["DRIFT_PYTHON"] = str(args.python.resolve())

	# ── Metadata ─────────────────────────────────────────────────────
	meta = load_deploy_metadata(repo_root)

	print(f"[deploy] version:  {meta.driftc_version}", flush=True)
	print(f"[deploy] abi:      {meta.abi_version}", flush=True)
	print(f"[deploy] commit:   {meta.git_commit}", flush=True)
	print(f"[deploy] dest:     {dest / meta.version_dir}", flush=True)

	# ── Prerequisites ────────────────────────────────────────────────
	clang = shutil.which("clang")
	if not clang:
		print("error: clang not found in PATH", file=sys.stderr)
		return 1

	# ── Build runtime archives ───────────────────────────────────────
	print("[deploy] building runtime archives...", flush=True)
	_build_runtime_archives(repo_root, clang)

	# ── Staging ──────────────────────────────────────────────────────
	dest.mkdir(parents=True, exist_ok=True)
	stage = Path(tempfile.mkdtemp(prefix=".deploy-staging.", dir=str(dest)))

	try:
		dist = stage / meta.version_dir

		# ── Step 1: PEX executables ──────────────────────────────────
		build_driftc_pex(repo_root, dist)
		build_drift_pex(repo_root, dist)

		# ── Step 2: Bundle ───────────────────────────────────────────
		bundle_compiler(repo_root, dist)
		bundle_runtime_archives(repo_root, dist)
		bundle_docs_and_examples(dist)

		# ── Step 3: Stdlib ───────────────────────────────────────────
		build_and_install_stdlib(repo_root, stage, dist, meta.driftc_version)

		# ── Step 4: Smoke ────────────────────────────────────────────
		run_smoke_test(dist, repo_root, stage)

		# ── Step 5: Publish ──────────────────────────────────────────
		generate_manifest(dist, meta)
		publish_atomic(dist, dest, meta.version_dir)
		switch_current_symlink(dest, meta.version_dir)
	except Exception as e:
		shutil.rmtree(str(stage), ignore_errors=True)
		if isinstance(e, SystemExit):
			raise
		print(f"error: {e}", file=sys.stderr)
		return 1

	# Clean staging remnants.
	shutil.rmtree(str(stage), ignore_errors=True)

	print()
	print(f'  export PATH="{dest}/current/bin:$PATH"')
	print()
	return 0


def _build_runtime_archives(repo_root: Path, clang: str) -> None:
	"""Build runtime archives for all variants."""
	sys.path.insert(0, str(repo_root))
	try:
		from lang.language_runtime import build_runtime_archive
		for variant in RUNTIME_VARIANTS:
			build_runtime_archive(repo_root, clang=clang, variant=variant)
	finally:
		sys.path.pop(0)


if __name__ == "__main__":
	sys.exit(main())
