# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy step: publish staged distribution via rename with rollback.

Publishes the staged tree as a flat layout directly under DEST
(bin/, lib/, doc/, examples/).  No partial tree is ever exposed.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from tools.deploy.steps.metadata import DeployMetadata


def generate_manifest(dist: Path, meta: DeployMetadata) -> None:
	"""Write lib/manifest.json into the staged distribution.

	Emits the dual-runtime ``runtimes`` map alongside the legacy
	``runtime_variants`` list.  The orchestrator capability check keys on
	``runtimes.normal.lib`` and ``runtimes.debug.lib`` to verify the staged
	toolchain supports both lanes; both entries must reference files that
	actually exist on disk relative to the distribution root.

	Vocabulary contract (do not deviate):
	  - external lane name "normal"      → on-disk variant subdir "default"
	    + unsuffixed archive ``libdrift_rt_abi<N>.a``
	  - external lane name "debug"       → on-disk variant subdir "debug"
	    + ``_debug``-infix archive ``libdrift_rt_debug_abi<N>.a``
	"""
	from tools.deploy.steps.bundle import RUNTIME_VARIANTS

	from lang.language_runtime import runtime_archive_name
	bundled_variants = [
		v for v in RUNTIME_VARIANTS
		if (dist / "lib" / "runtime" / v / runtime_archive_name(v)).exists()
	]

	# Build the dual-runtime map.  Each lane's ``lib`` is a path relative to
	# the distribution root so the manifest stays valid under arbitrary
	# install prefixes; the orchestrator capability check resolves it
	# against the staged toolchain root.
	runtimes: dict[str, dict[str, str]] = {}
	# external "normal" lane → on-disk "default" variant subdir.
	normal_rel = Path("lib") / "runtime" / "default" / runtime_archive_name("default")
	if (dist / normal_rel).exists():
		runtimes["normal"] = {"lib": str(normal_rel)}
	# external "debug" lane → on-disk "debug" variant subdir, _debug-infix archive.
	debug_rel = Path("lib") / "runtime" / "debug" / runtime_archive_name("debug")
	if (dist / debug_rel).exists():
		runtimes["debug"] = {"lib": str(debug_rel)}

	manifest = {
		"driftc_version": meta.driftc_version,
		"runtime_abi_version": meta.abi_version,
		"git_commit": meta.git_commit_full,
		"build_utc": meta.build_utc,
		"host_platform": meta.host_platform,
		"host_arch": meta.host_arch,
		"entrypoint": "pex-scie-eager",
		"runtime_variants": bundled_variants,
		"runtimes": runtimes,
	}

	out_path = dist / "lib" / "manifest.json"
	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(
		json.dumps(manifest, indent=2) + "\n",
		encoding="utf-8",
	)


def publish_flat(dist: Path, dest: Path) -> None:
	"""Publish staged distribution flat into dest via rename with rollback.

	The staged tree in *dist* is renamed to *dest* so that bin/, lib/,
	doc/ etc. live immediately under *dest* with no inner versioned
	subdirectory or ``current`` symlink.

	No partial tree is ever exposed — each step is a single rename(2).
	If *dest* already exists, it is first renamed to a backup, then the
	staged tree is renamed into place.  During replacement *dest* may be
	briefly absent between the two renames.  On failure the backup is
	restored.
	"""
	dest_parent = dest.parent
	dest_parent.mkdir(parents=True, exist_ok=True)

	backup: Path | None = None
	if dest.exists():
		backup = dest_parent / f".{dest.name}.old.{os.getpid()}"
		print(f"[deploy] replacing existing {dest.name}", flush=True)
		dest.rename(backup)

	try:
		dist.rename(dest)
	except Exception:
		# Rollback: restore backup.
		if backup is not None and backup.exists():
			if dest.exists():
				shutil.rmtree(str(dest), ignore_errors=True)
			backup.rename(dest)
		raise

	# Success — remove backup.
	if backup is not None and backup.exists():
		shutil.rmtree(str(backup))

	print(f"[deploy] published: {dest}", flush=True)
