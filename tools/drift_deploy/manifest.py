# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Back-compat re-export shim.

The manifest dataclasses + parser moved to
`lang/driftc/packages/manifest.py` (neutral location) so that
`tools/drift_author/cli.py` can import them without violating the
`test_author_module_does_not_import_orch_pipeline` boundary check.
This file preserves existing `from tools.drift_deploy.manifest import ...`
imports throughout the deploy pipeline; new code (especially the
author tool) should import from `lang.driftc.packages.manifest`
directly.
"""

from __future__ import annotations

from lang.driftc.packages.manifest import (
	MANIFEST_SCHEMA_VERSION,
	Artifact,
	Manifest,
	ManifestError,
	NativeDep,
	PackageDep,
	Project,
	load_manifest,
)

__all__ = [
	"MANIFEST_SCHEMA_VERSION",
	"Artifact",
	"Manifest",
	"ManifestError",
	"NativeDep",
	"PackageDep",
	"Project",
	"load_manifest",
]
