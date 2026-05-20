#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Drift distribution deploy — Python orchestrator.

Builds a self-contained Drift distribution published flat under DEST:
  DEST/bin/    — PEX --scie eager executables (driftc, drift)
  DEST/lib/    — compiler sources, runtime archives, stdlib
  DEST/doc/    — documentation
  DEST/examples/

Toolchain identity comes from ``lib/manifest.json``, ``driftc --version``,
provenance metadata, and certification records — not from directory names.

Steps:
  1. Build PEX --scie eager executables (bin/driftc, bin/drift)
  2. Bundle compiler sources, runtime archives, docs
  3. Build + sign stdlib package, core trust store
  4. Compile + run smoke test using deployed paths
  5. Atomic publish (flat)

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
	publish_flat,
)
from tools.deploy.steps.smoke import run_smoke_test
from tools.deploy.steps.stdlib import build_and_install_stdlib


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(
		prog="deploy",
		description="Build and publish a Drift distribution.",
	)
	parser.add_argument("--dest", type=Path, required=True,
		help="Deploy destination root (flat: bin/, lib/, etc. placed directly here)")
	parser.add_argument("--python", type=Path, default=None,
		help="Python interpreter (optional; for smoke/prereq checks)")
	parser.add_argument("--stdlib-author-claim", type=Path, required=True,
		help="Path to the externally-produced std.author-claim sidecar "
		"(emitted out-of-band by Foundation's offline author-signing "
		"flow).  This deploy step VALIDATES this artifact against the "
		"build but does NOT generate one.  Author private keys never "
		"enter the deploy host.")
	parser.add_argument("--stdlib-author-pubkey-b64", type=str, required=True,
		help="Base64-encoded 32-byte Ed25519 public key of the "
		"Foundation author kid that signed --stdlib-author-claim.  "
		"Recorded in core_trust_v1.json under the `authors` role.")
	parser.add_argument("--certifier-key-file", type=Path, default=None,
		help="Path to the certifier private-key seed file (base64-"
		"encoded 32-byte Ed25519 seed) the deploy host uses to sign "
		"`std.cert-claim`.  Falls back to $DRIFT_SIGN_KEY_FILE when "
		"omitted.  It is policy-allowed for this to be the same "
		"physical file used by the Foundation `drift-author publish` "
		"step earlier in the pipeline -- the role separation is about "
		"which claim body is signed at which step, not about forcing "
		"two distinct on-disk keys.")
	args = parser.parse_args(argv)

	repo_root = Path(__file__).resolve().parent.parent.parent
	dest = args.dest.expanduser().resolve()

	# ── Certifier key resolution ─────────────────────────────────────
	# Explicit --certifier-key-file wins; fall back to DRIFT_SIGN_KEY_FILE.
	# Fail closed with a clear pointer if neither is set: the deploy
	# step does not mint cert seeds.
	if args.certifier_key_file is not None:
		certifier_key_path = args.certifier_key_file.expanduser().resolve()
	else:
		env_path = os.environ.get("DRIFT_SIGN_KEY_FILE")
		if not env_path:
			print(
				"error: no certifier key available for `std.cert-claim`. "
				"Either pass --certifier-key-file <path> or set "
				"DRIFT_SIGN_KEY_FILE=<path>.  The deploy step does not "
				"generate certifier seeds -- it consumes one already "
				"provisioned for this host.",
				file=sys.stderr,
			)
			return 1
		certifier_key_path = Path(env_path).expanduser().resolve()
	if not certifier_key_path.is_file():
		print(
			f"error: certifier key path does not exist: {certifier_key_path}",
			file=sys.stderr,
		)
		return 1

	# ── Python override ──────────────────────────────────────────────
	if args.python:
		os.environ["DRIFT_PYTHON"] = str(args.python.resolve())

	# ── Metadata ─────────────────────────────────────────────────────
	meta = load_deploy_metadata(repo_root)

	print(f"[deploy] version:  {meta.driftc_version}", flush=True)
	print(f"[deploy] abi:      {meta.abi_version}", flush=True)
	print(f"[deploy] commit:   {meta.git_commit}", flush=True)
	print(f"[deploy] dest:     {dest}", flush=True)

	# ── Prerequisites ────────────────────────────────────────────────
	clang = shutil.which("clang")
	if not clang:
		print("error: clang not found in PATH", file=sys.stderr)
		return 1

	# ── Build runtime archives ───────────────────────────────────────
	print("[deploy] building runtime archives...", flush=True)
	_build_runtime_archives(repo_root, clang)

	# ── Staging ──────────────────────────────────────────────────────
	# Stage under dest's PARENT (not dest itself) so that rename-based
	# atomic publish works: if dest already exists, it is renamed to a
	# backup, then the staged dist is renamed into place.  Staging under
	# dest would break because the staged tree would move with the backup.
	dest.parent.mkdir(parents=True, exist_ok=True)
	stage = Path(tempfile.mkdtemp(prefix=".deploy-staging.", dir=str(dest.parent)))

	try:
		dist = stage / "dist"

		# ── Step 1: PEX executables ──────────────────────────────────
		build_driftc_pex(repo_root, dist)
		build_drift_pex(repo_root, dist)

		# ── Step 2: Bundle ───────────────────────────────────────────
		bundle_compiler(repo_root, dist)
		bundle_runtime_archives(repo_root, dist)
		bundle_docs_and_examples(dist)

		# ── Step 3: Stdlib ───────────────────────────────────────────
		# Author claim is an INPUT to this step (see
		# `tools/deploy/steps/stdlib.py` module docstring +
		# `docs/design/trust-v1.md` §7.5).  Deploy never holds the
		# Foundation author private key.
		build_and_install_stdlib(
			repo_root, stage, dist, meta.driftc_version,
			stdlib_author_claim_path=args.stdlib_author_claim.expanduser().resolve(),
			stdlib_author_pubkey_b64=args.stdlib_author_pubkey_b64,
			certifier_key_path=certifier_key_path,
			driftc_commit=meta.git_commit,
		)

		# ── Step 4: Smoke ────────────────────────────────────────────
		run_smoke_test(dist, repo_root, stage)

		# ── Step 5: Publish flat ─────────────────────────────────────
		generate_manifest(dist, meta)
		publish_flat(dist, dest)
	except Exception as e:
		shutil.rmtree(str(stage), ignore_errors=True)
		if isinstance(e, SystemExit):
			raise
		print(f"error: {e}", file=sys.stderr)
		return 1

	# Clean staging remnants.
	shutil.rmtree(str(stage), ignore_errors=True)

	print()
	print(f'  export PATH="{dest}/bin:$PATH"')
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
