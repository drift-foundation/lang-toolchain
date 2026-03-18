# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy step: atomically publish staged distribution.

Publishes the staged tree as DEST/VERSION_DIR and atomically switches
the DEST/current symlink. Safe same-version replacement with rollback.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from tools.deploy.steps.metadata import DeployMetadata


def generate_manifest(dist: Path, meta: DeployMetadata) -> None:
	"""Write lib/manifest.json into the staged distribution."""
	from tools.deploy.steps.bundle import RUNTIME_VARIANTS

	bundled_variants = [
		v for v in RUNTIME_VARIANTS
		if (dist / "lib" / "runtime" / v / "libdrift_rt.a").exists()
	]

	manifest = {
		"driftc_version": meta.driftc_version,
		"runtime_abi_version": meta.abi_version,
		"git_commit": meta.git_commit_full,
		"build_utc": meta.build_utc,
		"host_platform": meta.host_platform,
		"host_arch": meta.host_arch,
		"entrypoint": "pex-scie-eager",
		"runtime_variants": bundled_variants,
	}

	out_path = dist / "lib" / "manifest.json"
	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(
		json.dumps(manifest, indent=2) + "\n",
		encoding="utf-8",
	)


def publish_atomic(dist: Path, dest: Path, version_dir: str) -> None:
	"""Atomically publish staged distribution to dest."""
	dest.mkdir(parents=True, exist_ok=True)
	final = dest / version_dir

	if final.exists():
		backup = dest / f".{version_dir}.old.{os.getpid()}"
		print(f"[deploy] replacing existing {version_dir}", flush=True)
		final.rename(backup)
		try:
			dist.rename(final)
		except Exception:
			# Rollback.
			if backup.exists():
				backup.rename(final)
			raise
		# Success — remove backup.
		if backup.exists():
			shutil.rmtree(str(backup))
	else:
		dist.rename(final)

	print(f"[deploy] published: {final}", flush=True)


def switch_current_symlink(dest: Path, version_dir: str) -> None:
	"""Atomically switch the current symlink."""
	tmplink = dest / f".current.tmp.{os.getpid()}"
	tmplink.symlink_to(version_dir)
	try:
		tmplink.rename(dest / "current")
	except OSError:
		# Fallback for systems that don't support atomic rename over symlinks.
		tmplink.unlink()
		current = dest / "current"
		if current.exists() or current.is_symlink():
			current.unlink()
		current.symlink_to(version_dir)

	print(f"[deploy] current -> {version_dir}", flush=True)
