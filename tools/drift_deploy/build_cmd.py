# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Shared artifact-to-driftc command builders.

Used by both ``drift build`` (local builds) and ``drift deploy``
(full publish pipeline).  Functions return ``list[str]`` command
vectors — callers handle execution, environment, and error reporting.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from tools.drift_deploy.lockfile import expand_to_dep_flags
from tools.drift_deploy.manifest import Artifact
from tools.drift_deploy.resolver import ResolvedDep


def UserPath(s: str) -> Path:
	"""Argparse type that expands ``~`` in path arguments."""
	return Path(s).expanduser()


def resolve_driftc(explicit: Path | None = None) -> Path | None:
	"""
	Resolve the driftc binary path.

	Resolution order:
	  1. Explicit path (--driftc flag) — must exist.
	  2. Sibling ``driftc`` next to the running ``drift`` executable.
	     Follows symlinks so deployed layouts resolve correctly
	     (e.g. ``~/opt/drift/toolchain/bin/driftc``).
	  3. ``driftc`` from ``$PATH``.

	Returns the resolved Path, or None if not found.
	Raises ValueError if an explicit path was given but does not exist.
	"""
	# 1. Explicit.
	if explicit is not None:
		if not explicit.exists():
			raise ValueError(f"--driftc path does not exist: {explicit}")
		return explicit

	# 2. Sibling of the running executable.
	drift_exe = Path(os.path.realpath(sys.argv[0]))
	sibling = drift_exe.parent / "driftc"
	if sibling.is_file() and os.access(str(sibling), os.X_OK):
		return sibling

	# 3. PATH lookup.
	on_path = shutil.which("driftc")
	if on_path:
		return Path(on_path)

	return None


def project_root_for(manifest_dir: Path) -> Path:
	"""Return the project root for a manifest at ``manifest_dir / manifest.json``.

	Under the canonical post-rename layout, the manifest lives at
	``<project_root>/drift/manifest.json``, so the project root is the
	parent of the manifest directory.  Source paths, asset paths, and the
	build output directory are all resolved relative to the project root,
	not the manifest dir — users write ``entry_module: "src/lib.drift"``
	expecting it to point at ``<project_root>/src/lib.drift``, not
	``<project_root>/drift/src/lib.drift``.

	If the manifest's containing dir is NOT named ``drift`` (e.g. a
	non-standard manifest location passed via ``--manifest /tmp/foo.json``),
	the project root collapses to the manifest dir itself, so the legacy
	"sources next to manifest" interpretation still works for one-off use.
	"""
	if manifest_dir.name == "drift":
		return manifest_dir.parent
	return manifest_dir


def build_source_args(art: Artifact, manifest_dir: Path) -> list[str]:
	"""
	Build the source file arguments: entry_module first, then remaining
	modules, deduplicated.

	Paths in the manifest are project-root-relative.  See ``project_root_for``
	for the resolution rule.
	"""
	root = project_root_for(manifest_dir)
	args: list[str] = []
	entry = str(root / art.entry_module)
	args.append(entry)
	for mod_path in art.modules:
		resolved_mod = str(root / mod_path)
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
	trust_store: Path | None = None,
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

	# Trust store for verifying co-artifact dependencies.
	if trust_store is not None:
		cmd.extend(["--trust-store", str(trust_store)])

	# Native deps.
	for nd in art.native_deps:
		cmd.extend(["--native-link-lib", nd.lib])

	# Package dep declarations: only DIRECT deps (from manifest), and
	# they carry the **manifest's owner-declared range**, NOT the
	# lock's exact version.  This is the published constraint a
	# downstream consumer sees — shipping a producer's exact pin
	# here would leak the producer's local lock into consumer
	# resolution, forcing every intermediate library to republish on
	# every upstream patch bump.  Transitive deps do NOT appear here;
	# they flow to the compiler only via --dep (exact, from lock).
	for pd in art.package_deps:
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
	trust_store: Path | None = None,
	extra_flags: list[str] | None = None,
) -> list[str]:
	"""Build the driftc command for an app artifact."""
	cmd = [str(driftc), "-o", str(output_path)]

	# Native target: emit --target-word-bits for host.
	if target == "native":
		import struct
		word_bits = struct.calcsize("P") * 8
		cmd.extend(["--target-word-bits", str(word_bits)])

	# Custom entry point (e.g. "pushcoin.bookkeeper::main").
	if art.entry_point:
		cmd.extend(["--entry", art.entry_point])

	# Trust store for verifying package dependencies.
	if trust_store is not None:
		cmd.extend(["--trust-store", str(trust_store)])

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
