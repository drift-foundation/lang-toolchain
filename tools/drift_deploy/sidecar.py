# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
App provenance sidecar (.meta.json) emission.

Records the resolved dependency graph and build metadata alongside
a compiled app binary for post-hoc audit and reproducibility.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.drift_deploy.resolver import ResolvedDep


def write_app_sidecar(
	path: Path,
	*,
	app_name: str,
	app_version: str,
	target: str,
	compiler_version: str,
	resolved_deps: dict[str, ResolvedDep],
) -> None:
	"""Write <app>.meta.json sidecar next to the app binary."""
	deps_obj: dict[str, Any] = {}
	for pkg_id in sorted(resolved_deps.keys()):
		dep = resolved_deps[pkg_id]
		deps_obj[pkg_id] = {
			"version": dep.version,
			"integrity": dep.integrity,
		}

	obj: dict[str, Any] = {
		"schema_version": 1,
		"app": app_name,
		"version": app_version,
		"target": target,
		"compiler_version": compiler_version,
		"built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
		"resolved_deps": deps_obj,
	}

	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
		encoding="utf-8",
	)
