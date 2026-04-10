# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift prepare — resolve dependencies and write drift/lock.json.

Explicit mutating step for repo-managed release metadata.
Run before commit; drift deploy then consumes the prepared state.

Expected workflow:
  1. Edit drift/manifest.json
  2. drift prepare --dest /deploy
  3. Review drift/lock.json changes
  4. Commit
  5. drift deploy --dest /deploy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.drift_deploy.build_cmd import UserPath
from tools.drift_deploy.lockfile import write_lock
from tools.drift_deploy.manifest import (
	Manifest,
	ManifestError,
	load_manifest,
)
from tools.drift_deploy.resolver import (
	PackageEntry,
	ResolutionError,
	ResolvedDep,
	build_package_index,
	resolve_artifact,
)
from tools.drift_deploy.semver import parse_version


class PrepareError(Exception):
	"""Fatal prepare error."""
	pass


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift prepare",
		description=(
			"Resolve package dependencies and write drift/lock.json. "
			"Run before commit; drift deploy consumes the prepared state."
		),
	)
	p.add_argument("--manifest", type=UserPath, default=Path("drift") / "manifest.json",
		help="Path to drift/manifest.json (default: ./drift/manifest.json)")
	p.add_argument("--dest", type=UserPath, default=None,
		help="Package library root (used as default --package-root)")
	p.add_argument("--package-root", type=UserPath, action="append", default=None,
		help="Library root for resolving package_deps (repeatable; default: --dest)")
	return p


def _topo_sort_artifacts(artifacts: list) -> list:
	"""Topological sort: packages before apps that depend on them."""
	from tools.drift_deploy.drift_deploy import _topo_sort_artifacts as _ts
	return _ts(artifacts)


def run(argv: list[str] | None = None) -> int:
	"""Main entry point. Returns exit code."""
	parser = build_arg_parser()
	args = parser.parse_args(argv)

	try:
		return _run_impl(args)
	except PrepareError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except ManifestError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print("\ninterrupted", file=sys.stderr)
		return 130


def _run_impl(args: argparse.Namespace) -> int:
	manifest = load_manifest(args.manifest)
	manifest_dir = args.manifest.resolve().parent

	artifacts = list(manifest.artifacts)

	# Topological sort.
	artifacts = _topo_sort_artifacts(artifacts)

	# Package roots: default to --dest.
	package_roots = args.package_root or ([args.dest] if args.dest else [])

	# Determine which artifacts need resolution.
	need_resolution = [a for a in artifacts if a.package_deps]

	if not need_resolution:
		print("drift prepare: no artifacts have package_deps; nothing to resolve")
		return 0

	# Build package index.
	pkg_index = build_package_index(package_roots)

	# Inject co-artifact entries: package-kind artifacts in the same manifest
	# can satisfy each other's package_deps without being published.
	co_artifact_names: set[str] = set()
	for art in artifacts:
		if art.kind == "library":
			co_artifact_names.add(art.name)
			pkg_deps = [(dep.name, dep.version) for dep in art.package_deps]
			entry = PackageEntry(
				package_id=art.name,
				version=parse_version(art.version),
				path=Path("/dev/null"),  # no .dmp yet
				sha256="co-artifact",
				package_deps=pkg_deps,
			)
			# Co-artifact takes priority over any externally-discovered
			# entry with the same package_id.
			pkg_index[art.name] = [entry]

	# Resolve each artifact.
	resolved_map: dict[str, dict[str, ResolvedDep]] = {}
	for art in artifacts:
		if not art.package_deps:
			continue
		direct_deps = [(dep.name, dep.version) for dep in art.package_deps]
		try:
			resolved = resolve_artifact(
				art.name, direct_deps, pkg_index,
				searched_roots=package_roots,
			)
		except ResolutionError as e:
			raise PrepareError(str(e))

		# Mark co-artifact deps: integrity is unknown at prepare time
		# (the .dmp hasn't been built yet); deploy verifies at build time.
		for pkg_id in list(resolved):
			if pkg_id in co_artifact_names:
				old = resolved[pkg_id]
				resolved[pkg_id] = ResolvedDep(
					version=old.version,
					integrity="sha256:co-artifact",
					dep_type="co-artifact",
				)

		resolved_map[art.name] = resolved

	# Write lock file — full rewrite for the entire manifest.
	lock_path = manifest_dir / "lock.json"
	write_lock(lock_path, resolved_map)

	# Summary.
	print(f"drift prepare: resolved {len(resolved_map)} artifact(s)")
	for art_name, deps in sorted(resolved_map.items()):
		dep_summary = ", ".join(f"{k}@{v.version}" for k, v in sorted(deps.items()))
		print(f"  {art_name}: {dep_summary}")
	print(f"  wrote {lock_path}")

	return 0


if __name__ == "__main__":
	sys.exit(run())
