# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy metadata — single source of truth for version, ABI, git, and build info.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class DeployMetadata:
	driftc_version: str
	abi_version: int
	git_commit: str
	git_commit_full: str
	build_utc: str
	host_platform: str
	host_arch: str

	@property
	def version_tag(self) -> str:
		"""Human-readable version tag (for logs, not directory names)."""
		return f"drift-{self.driftc_version}+abi{self.abi_version}"


def load_deploy_metadata(repo_root: Path) -> DeployMetadata:
	"""Load deploy metadata from the repo."""
	import sys
	sys.path.insert(0, str(repo_root))
	try:
		from lang.driftc.driftc_versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
	finally:
		sys.path.pop(0)

	git_commit_full = _git_rev(repo_root, short=False)
	git_commit = _git_rev(repo_root, short=True)

	return DeployMetadata(
		driftc_version=DRIFTC_VERSION,
		abi_version=DRIFT_RT_ABI_VERSION,
		git_commit=git_commit,
		git_commit_full=git_commit_full,
		build_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
		host_platform=platform.system().lower(),
		host_arch=platform.machine(),
	)


def _git_rev(repo_root: Path, *, short: bool) -> str:
	cmd = ["git", "-C", str(repo_root), "rev-parse"]
	if short:
		cmd.append("--short")
	cmd.append("HEAD")
	try:
		result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
		if result.returncode == 0:
			return result.stdout.strip()
	except Exception:
		pass
	return "unknown"
