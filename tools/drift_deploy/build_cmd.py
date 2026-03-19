# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Shared artifact-to-driftc command builders.

Used by both ``drift build`` (local builds) and ``drift deploy``
(full publish pipeline).  Functions return ``list[str]`` command
vectors — callers handle execution, environment, and error reporting.
"""

from __future__ import annotations

from pathlib import Path

from tools.drift_deploy.lockfile import expand_to_dep_flags
from tools.drift_deploy.manifest import Artifact
from tools.drift_deploy.resolver import ResolvedDep


def build_source_args(art: Artifact, manifest_dir: Path) -> list[str]:
	"""
	Build the source file arguments: entry_module first, then remaining
	modules, deduplicated.
	"""
	args: list[str] = []
	entry = str(manifest_dir / art.entry_module)
	args.append(entry)
	for mod_path in art.modules:
		resolved_mod = str(manifest_dir / mod_path)
		if resolved_mod != entry:
			args.append(resolved_mod)
	return args


def build_package_cmd(
	art: Artifact,
	*,
	driftc: Path,
	target: str,
	resolved_deps: dict[str, ResolvedDep],
	output_path: Path,
	manifest_dir: Path,
	package_roots: list[Path],
	native_lib_paths: list[Path] | None = None,
	extra_flags: list[str] | None = None,
) -> list[str]:
	"""Build the driftc command for a package artifact."""
	cmd = [
		str(driftc),
		"--emit-package", str(output_path),
		"--package-id", art.name,
		"--package-version", art.version,
		"--package-target", target,
	]

	# Native deps.
	for nd in art.native_deps:
		cmd.extend(["--native-link-lib", nd.lib])

	# Package dep declarations: only DIRECT deps (from manifest), but
	# using the exact resolved version — not the manifest's author-intent
	# range.  Transitive deps are NOT declared as package deps; they only
	# appear via --dep for compiler version selection.
	for pd in art.package_deps:
		if pd.name in resolved_deps:
			cmd.extend(["--package-dep", f"{pd.name}={resolved_deps[pd.name].version}"])
		else:
			cmd.extend(["--package-dep", f"{pd.name}={pd.version}"])

	# Exact resolved versions via --dep (compiler version selection).
	cmd.extend(expand_to_dep_flags(resolved_deps))

	# Package roots.
	for pr in package_roots:
		cmd.extend(["--package-root", str(pr)])

	# Unsafe support for FFI packages.
	if art.unsafe:
		cmd.append("--allow-unsafe")

	# Native library search paths (resolver input, not package metadata).
	for nlp in (native_lib_paths or []):
		cmd.extend(["--link-search", str(nlp)])

	# Extra passthrough flags.
	if extra_flags:
		cmd.extend(extra_flags)

	# Source inputs.
	cmd.extend(build_source_args(art, manifest_dir))

	return cmd


def build_app_cmd(
	art: Artifact,
	*,
	driftc: Path,
	target: str,
	resolved_deps: dict[str, ResolvedDep],
	output_path: Path,
	manifest_dir: Path,
	package_roots: list[Path],
	native_lib_paths: list[Path] | None = None,
	extra_flags: list[str] | None = None,
) -> list[str]:
	"""Build the driftc command for an app artifact."""
	cmd = [str(driftc), "-o", str(output_path)]

	# Exact resolved versions via --dep.
	cmd.extend(expand_to_dep_flags(resolved_deps))

	# Package roots.
	for pr in package_roots:
		cmd.extend(["--package-root", str(pr)])

	# Native deps for link-time.
	for nd in art.native_deps:
		cmd.extend(["--link-lib", nd.lib])

	# Unsafe support for FFI apps.
	if art.unsafe:
		cmd.append("--allow-unsafe")

	# Native library search paths (resolver input, not package metadata).
	for nlp in (native_lib_paths or []):
		cmd.extend(["--link-search", str(nlp)])

	# Extra passthrough flags.
	if extra_flags:
		cmd.extend(extra_flags)

	# Source inputs.
	cmd.extend(build_source_args(art, manifest_dir))

	return cmd
