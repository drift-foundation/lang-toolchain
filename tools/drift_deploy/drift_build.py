# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift build — manifest-driven local artifact builds.

Reads drift-manifest.json, resolves dependencies from the lockfile,
and invokes driftc with the correct flags.  Does NOT own resolution —
consumes existing drift-lock.json written by ``drift prepare``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tools.drift_deploy.build_cmd import build_app_cmd, build_package_cmd, resolve_driftc
from tools.drift_deploy.lockfile import read_lock, verify_lock_integrity
from tools.drift_deploy.manifest import (
	Artifact,
	ManifestError,
	load_manifest,
)
from tools.drift_deploy.resolver import ResolvedDep, build_package_index


# ── Errors ───────────────────────────────────────────────────────────


class BuildError(Exception):
	"""Fatal build error."""
	pass


# ── Subprocess environment ───────────────────────────────────────────

# Keys to scrub from child process environments.  PYTHONPATH leaks the
# build tool's import roots into PEX-based driftc, causing it to pick
# up unbundled lang/ modules and crash with ModuleNotFoundError.
_SCRUB_ENV_KEYS = frozenset({"PYTHONPATH", "PYTHONHOME"})


def _clean_env() -> dict[str, str]:
	"""Build a clean environment for driftc subprocess calls."""
	return {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV_KEYS}


# ── Version constraint helpers ───────────────────────────────────────


_RANGE_CHARS = frozenset("^~>=<*")


def _is_exact_version(version: str) -> bool:
	"""Return True if version looks like an exact pin (no range operators)."""
	return not any(c in _RANGE_CHARS for c in version)


# ── CLI ──────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift build",
		description="Build Drift artifacts from drift-manifest.json.",
	)
	p.add_argument("artifact_name", nargs="?", default=None,
		help="Artifact to build (optional if manifest has exactly one artifact)")
	p.add_argument("--manifest", "-m", type=Path, default=Path("drift-manifest.json"),
		help="Path to drift-manifest.json (default: ./drift-manifest.json)")
	p.add_argument("--package-root", type=Path, action="append", default=None,
		help="Library root for resolving package deps (repeatable)")
	p.add_argument("--native-lib-path", type=Path, action="append", default=None,
		help="Native library search path for linker (repeatable)")
	p.add_argument("--target", type=str, default="drift-dev",
		help="Target triple (default: drift-dev)")
	p.add_argument("-o", "--output", type=Path, default=None,
		help="Explicit output path override")
	p.add_argument("--driftc", type=Path, default=None,
		help="Path to driftc (default: driftc from PATH)")
	return p


def _resolve_driftc(driftc_arg: Path | None) -> Path:
	try:
		result = resolve_driftc(driftc_arg)
	except ValueError as e:
		raise BuildError(str(e))
	if result is None:
		raise BuildError("driftc not found (no sibling binary, not on PATH); pass --driftc explicitly")
	return result


def _load_deploy_config(manifest_dir: Path) -> dict:
	"""Load and validate drift-deploy-config.json from manifest directory."""
	config_path = manifest_dir / "drift-deploy-config.json"
	if not config_path.exists():
		return {}
	try:
		config = json.loads(config_path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError) as e:
		raise BuildError(f"failed to read {config_path}: {e}")
	if not isinstance(config, dict):
		raise BuildError(f"{config_path} must be a JSON object")
	return config


def _resolve_package_roots(
	cli_roots: list[Path] | None,
	manifest_dir: Path,
) -> list[Path]:
	"""
	Merge package roots from CLI, config file, and environment.

	Precedence (lowest to highest):
	  1. $DRIFT_PACKAGE_ROOT (colon-separated)
	  2. drift-deploy-config.json "package_roots"
	  3. --package-root CLI flags

	All paths must be absolute — relative paths are ambiguous because
	driftc may run from a different working directory.
	"""
	result: list[Path] = []

	# 1. Environment variable (lowest priority).
	env_val = os.environ.get("DRIFT_PACKAGE_ROOT", "")
	if env_val:
		for p in env_val.split(":"):
			p = p.strip()
			if p:
				pp = Path(p)
				if not pp.is_absolute():
					raise BuildError(
						f"$DRIFT_PACKAGE_ROOT: relative path '{p}' not allowed; "
						f"absolute paths are required for package roots"
					)
				result.append(pp)

	# 2. Config file.
	config = _load_deploy_config(manifest_dir)
	config_path = manifest_dir / "drift-deploy-config.json"
	if config:
		raw_roots = config.get("package_roots", [])
		if not isinstance(raw_roots, list):
			raise BuildError(f"{config_path}: 'package_roots' must be an array")
		for entry in raw_roots:
			if not isinstance(entry, str) or not entry:
				raise BuildError(f"{config_path}: 'package_roots' entries must be non-empty strings")
			ep = Path(entry)
			if not ep.is_absolute():
				raise BuildError(
					f"{config_path}: relative path '{entry}' not allowed in 'package_roots'; "
					f"absolute paths are required for package roots"
				)
			result.append(ep)

	# 3. CLI flags (highest priority).
	if cli_roots:
		for cr in cli_roots:
			if not cr.is_absolute():
				raise BuildError(
					f"--package-root: relative path '{cr}' not allowed; "
					f"absolute paths are required for package roots"
				)
		result.extend(cli_roots)

	return result


def _resolve_native_lib_paths(
	cli_paths: list[Path] | None,
	manifest_dir: Path,
) -> list[Path]:
	"""
	Merge native library search paths from CLI, config file, and environment.

	Same three-source merge and absolute-path requirement as drift deploy.
	"""
	result: list[Path] = []

	# 1. Environment variable (lowest priority).
	env_val = os.environ.get("DRIFT_NATIVE_LIB_PATH", "")
	if env_val:
		for p in env_val.split(":"):
			p = p.strip()
			if p:
				pp = Path(p)
				if not pp.is_absolute():
					raise BuildError(
						f"$DRIFT_NATIVE_LIB_PATH: relative path '{p}' not allowed; "
						f"absolute paths are required for native library search hints"
					)
				result.append(pp)

	# 2. Config file.
	config = _load_deploy_config(manifest_dir)
	config_path = manifest_dir / "drift-deploy-config.json"
	if config:
		raw_paths = config.get("native_lib_paths", [])
		if not isinstance(raw_paths, list):
			raise BuildError(f"{config_path}: 'native_lib_paths' must be an array")
		for entry in raw_paths:
			if not isinstance(entry, str) or not entry:
				raise BuildError(f"{config_path}: 'native_lib_paths' entries must be non-empty strings")
			ep = Path(entry)
			if not ep.is_absolute():
				raise BuildError(
					f"{config_path}: relative path '{entry}' not allowed in 'native_lib_paths'; "
					f"absolute paths are required for native library search hints"
				)
			result.append(ep)

	# 3. CLI flags (highest priority).
	if cli_paths:
		for nlp in cli_paths:
			if not nlp.is_absolute():
				raise BuildError(
					f"--native-lib-path: relative path '{nlp}' not allowed; "
					f"absolute paths are required for native library search hints"
				)
		result.extend(cli_paths)

	return result


def _resolve_artifact(manifest, name: str | None) -> Artifact:
	"""Select the artifact to build."""
	if name is None:
		if len(manifest.artifacts) == 1:
			return manifest.artifacts[0]
		names = ", ".join(a.name for a in manifest.artifacts)
		raise BuildError(
			f"manifest has multiple artifacts ({names}); "
			f"specify which one to build"
		)
	for art in manifest.artifacts:
		if art.name == name:
			return art
	raise BuildError(f"artifact '{name}' not found in manifest")


def _resolve_deps(
	art: Artifact,
	manifest_dir: Path,
	package_roots: list[Path],
) -> dict[str, ResolvedDep]:
	"""
	Resolve dependencies for a build.

	When a lockfile exists, returns the full locked graph (direct +
	transitive) — not just direct deps.  Transitive pins are required
	so driftc can resolve the exact version of every package in the
	dependency tree, avoiding ambiguity errors when multiple versions
	of a transitive dep exist under package roots.

	Lock contract mirrors deploy: if a lockfile exists, it must contain
	the artifact and all its declared deps.  A stale or partial lock is
	an error — run ``drift prepare`` to re-resolve.

	Without a lockfile, only exact version pins are accepted.
	"""
	if not art.package_deps:
		return {}

	lock_path = manifest_dir / "drift-lock.json"

	if lock_path.exists():
		try:
			lock_data = read_lock(lock_path)
		except ValueError as e:
			raise BuildError(f"failed to read {lock_path}: {e}")

		# Lock present → enforce the full contract (same as deploy).
		if art.name not in lock_data:
			raise BuildError(
				f"artifact '{art.name}' not found in {lock_path}; "
				f"run 'drift prepare' to re-resolve"
			)
		locked = lock_data[art.name]
		for dep in art.package_deps:
			if dep.name not in locked:
				raise BuildError(
					f"artifact '{art.name}': package_dep '{dep.name}' not in lock file; "
					f"run 'drift prepare' to re-resolve"
				)

		# Verify lock integrity against package roots.
		if package_roots:
			pkg_index = build_package_index(package_roots)
			errors = verify_lock_integrity(locked, pkg_index)
			if errors:
				raise BuildError(
					f"artifact '{art.name}': lock integrity check failed:\n"
					+ "\n".join(f"  {e}" for e in errors)
				)

		return locked

	# No lockfile — only exact version pins accepted.
	resolved: dict[str, ResolvedDep] = {}
	for dep in art.package_deps:
		if _is_exact_version(dep.version):
			resolved[dep.name] = ResolvedDep(
				version=dep.version,
				integrity="",
				dep_type="direct",
			)
		else:
			raise BuildError(
				f"dependency '{dep.name}' requires version range '{dep.version}' "
				f"but no lockfile found; run 'drift prepare' first"
			)

	return resolved


def _default_output_path(art: Artifact, build_dir: Path) -> Path:
	if art.kind == "package":
		return build_dir / f"{art.name}.dmp"
	return build_dir / art.name


# ── Main ─────────────────────────────────────────────────────────────


def run(argv: list[str] | None = None) -> int:
	"""Main entry point. Returns exit code."""
	# Split on -- for passthrough flags.
	if argv is None:
		argv = []

	extra_flags: list[str] = []
	if "--" in argv:
		sep_idx = argv.index("--")
		extra_flags = argv[sep_idx + 1:]
		argv = argv[:sep_idx]

	parser = build_arg_parser()
	args = parser.parse_args(argv)

	try:
		return _run_impl(args, extra_flags)
	except BuildError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except ManifestError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print("\ninterrupted", file=sys.stderr)
		return 130


def _run_impl(args: argparse.Namespace, extra_flags: list[str]) -> int:
	# Load manifest.
	manifest = load_manifest(args.manifest)
	manifest_dir = args.manifest.resolve().parent

	# Select artifact.
	art = _resolve_artifact(manifest, args.artifact_name)

	# Resolve driftc.
	driftc = _resolve_driftc(args.driftc)

	# Package roots (resolved before deps — needed for lock integrity check).
	package_roots = _resolve_package_roots(args.package_root, manifest_dir)

	# Resolve deps.
	resolved = _resolve_deps(art, manifest_dir, package_roots)

	# Native lib paths.
	native_lib_paths = _resolve_native_lib_paths(args.native_lib_path, manifest_dir)

	# Output path.
	build_dir = manifest_dir / "build"
	if args.output:
		output_path = args.output
	else:
		output_path = _default_output_path(art, build_dir)

	# Ensure output directory exists.
	output_path.parent.mkdir(parents=True, exist_ok=True)

	# Build command.
	if art.kind == "package":
		cmd = build_package_cmd(
			art,
			driftc=driftc,
			target=args.target,
			resolved_deps=resolved,
			output_path=output_path,
			manifest_dir=manifest_dir,
			package_roots=package_roots,
			native_lib_paths=native_lib_paths,
			extra_flags=extra_flags or None,
		)
	else:
		cmd = build_app_cmd(
			art,
			driftc=driftc,
			target=args.target,
			resolved_deps=resolved,
			output_path=output_path,
			manifest_dir=manifest_dir,
			package_roots=package_roots,
			native_lib_paths=native_lib_paths,
			extra_flags=extra_flags or None,
		)

	# Execute.
	print(f"drift build: {art.name} ({art.kind}) v{art.version}")
	result = subprocess.run(cmd, capture_output=True, text=True, env=_clean_env())
	if result.returncode != 0:
		print(f"build failed for {art.kind} '{art.name}':", file=sys.stderr)
		if result.stderr:
			print(result.stderr.strip(), file=sys.stderr)
		return 1

	print(f"  output: {output_path}")
	return 0


if __name__ == "__main__":
	sys.exit(run())
