# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift build — manifest-driven local artifact builds.

Reads drift/manifest.json, resolves dependencies from the lockfile,
and invokes driftc with the correct flags.  Does NOT own resolution —
consumes existing drift/lock.json written by ``drift prepare``.

On-disk layout (post-rename, hard cut — no legacy fallback):

  drift/manifest.json       (the project manifest; was drift-manifest.json)
  drift/lock.json           (resolved dep lock; was drift-lock.json)
  drift/trust.json          (trust store; unchanged location)
  drift/deploy-config.json  (per-machine resolver hints; was drift-deploy-config.json)

All four drift-owned metadata files now live under the `drift/` namespace.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tools.drift_deploy.build_cmd import UserPath, build_app_cmd, build_package_cmd, resolve_driftc
from tools.drift_deploy.lockfile import read_lock, verify_lock_compatibility
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
_SCRUB_ENV_KEYS = frozenset({
	"PYTHONPATH", "PYTHONHOME",
	# Runtime instrumentation flags — meaningless for compilation/package
	# emission and cause bin/driftc wrapper to reject the invocation.
	"DRIFT_MEMCHECK", "DRIFT_MASSIF",
})


def _clean_env() -> dict[str, str]:
	"""Build a clean environment for driftc subprocess calls."""
	return {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV_KEYS}


def _env_true(name: str) -> bool:
	"""Truthy check matching the rest of the toolchain's env-flag idiom."""
	return os.environ.get(name, "") in ("1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON")


# ── CLI ──────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift build",
		description="Build Drift artifacts from drift/manifest.json.",
	)
	p.add_argument("artifact_name", nargs="?", default=None,
		help="Artifact to build (optional if manifest has exactly one artifact)")
	p.add_argument("--manifest", "-m", type=UserPath, default=Path("drift") / "manifest.json",
		help="Path to drift/manifest.json (default: ./drift/manifest.json)")
	p.add_argument("--package-root", type=UserPath, action="append", default=None,
		help="Library root for resolving package deps (repeatable)")
	p.add_argument("--native-lib-path", type=UserPath, action="append", default=None,
		help="Native library search path for linker (repeatable)")
	p.add_argument("--target", type=str, default=None,
		help="Build target (default: native for apps, drift-dev for libraries)")
	p.add_argument("-o", "--output", type=UserPath, default=None,
		help="Explicit output path override")
	p.add_argument("--driftc", type=UserPath, default=None,
		help="Path to driftc (default: driftc from PATH)")
	p.add_argument("--debug", action="store_true",
		help="Produce a debug-style build (links the `_debug` runtime variant; "
		     "equivalent to setting DRIFT_DEBUG=1)")
	p.add_argument("--source-rebuild", action="store_true",
		help=(
			"Source-rebuild certification mode: tolerate `.dmp` byte "
			"sha256 drift between the lock and the on-disk package "
			"as long as the lock's recorded source identity "
			"(`source_content_id` + `source_attestation_key`) re-"
			"verifies against the package's `.source-attestation` "
			"sidecar.  Per-package sha drift is reported to stdout "
			"as run evidence.  Use when an upstream `.dmp` was "
			"rebuilt from the same source the author attested (e.g. "
			"orchestrator source-from-commit certification).  "
			"Missing source attestations and unsigned packages are "
			"hard-failed in this mode — there is no silent fallback "
			"to byte-only verification."
		))
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
	"""Load and validate drift/deploy-config.json from manifest directory."""
	config_path = manifest_dir / "deploy-config.json"
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
	  2. drift/deploy-config.json "package_roots"
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
	config_path = manifest_dir / "deploy-config.json"
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
	config_path = manifest_dir / "deploy-config.json"
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
	*,
	co_artifact_names: set[str] | None = None,
	source_rebuild: bool = False,
) -> dict[str, ResolvedDep]:
	"""
	Resolve dependencies for a build.

	Strict-exact contract (0.29+):

	- Any artifact that declares `package_deps` MUST have a v3
	  `drift/lock.json` containing the full resolved graph (direct +
	  transitive).  `drift build` never resolves ranges at build time;
	  that is `drift prepare`'s sole responsibility.
	- On-disk packages are verified by exact version, sha256, AND
	  author_key via `verify_lock_compatibility`.  Any mismatch is a
	  hard build error pointing back at `drift prepare`.
	- The full locked graph is returned; driftc receives every
	  transitive as an exact `--dep PKG@M.N.P` pin.
	"""
	if not art.package_deps:
		return {}

	lock_path = manifest_dir / "lock.json"

	if not lock_path.exists():
		# Any declared dep requires a lock — no "exact pin in manifest"
		# escape hatch.  v2 manifests only accept owner-declared ranges
		# (`M` / `M.N`); even a hypothetical `M.N.P` would still need a
		# lock to carry sha256 + author_key for the strict-exact
		# re-verification below.
		dep_list = ", ".join(f"{d.name}={d.version}" for d in art.package_deps)
		raise BuildError(
			f"artifact '{art.name}' declares package_deps [{dep_list}] "
			f"but no {lock_path} exists.  Run `drift prepare` to resolve "
			f"the full transitive graph and write the lock before building."
		)

	try:
		lock_data = read_lock(lock_path)
	except ValueError as e:
		# `read_lock` already emits the "run drift prepare" guidance
		# for v1/v2/malformed locks; surface it verbatim.
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

	# Verify lock compatibility against package roots.  Every
	# non-co-artifact dep must have a single disk entry at the exact
	# M.N.P, matching sha256, matching author_key.  Any deviation is a
	# `drift prepare` problem.
	#
	# `co_artifact_names` names the manifest's library artifacts —
	# only those IDs may legitimately carry `dep_type: "co-artifact"`
	# in the lock (bypassing sha/signer re-check because they are
	# built in this same run).  Anything else claiming co-artifact
	# status is rejected as corruption.
	if package_roots:
		pkg_index = build_package_index(package_roots)
		from tools.drift_deploy.lockfile import (
			VERIFY_MODE_SOURCE_REBUILD,
			VERIFY_MODE_STRICT,
		)
		mode = VERIFY_MODE_SOURCE_REBUILD if source_rebuild else VERIFY_MODE_STRICT
		# Capture per-package sha drift for run-evidence reporting
		# in source-rebuild mode.  Strict mode never produces
		# entries here (sha mismatch is a hard error).
		sha_drift_log: list[tuple[str, str, str]] = []
		errors = verify_lock_compatibility(
			locked, pkg_index,
			allowed_co_artifacts=co_artifact_names or set(),
			mode=mode,
			sha_drift_log=sha_drift_log,
		)
		if errors:
			raise BuildError(
				f"artifact '{art.name}': lock compatibility check failed:\n"
				+ "\n".join(f"  {e}" for e in errors)
			)
		if source_rebuild and sha_drift_log:
			# Run evidence: surface every byte-divergent package so
			# the human running the certification sees exactly which
			# rebuilds aren't byte-stable.  This is informational —
			# the verification already accepted them based on
			# matching source identity.
			print(
				f"drift build --source-rebuild: artifact '{art.name}' "
				f"accepted with byte-drift in {len(sha_drift_log)} "
				f"dep(s) (source identity verified):"
			)
			for pkg_id, locked_sha, disk_sha in sha_drift_log:
				print(f"  {pkg_id}:  locked {locked_sha}  ->  rebuilt {disk_sha}")

	# v4 lock: entries already carry exact version + sha256 +
	# source identity.  Pass the full transitive graph through
	# unchanged — patch movement happens only in `drift prepare`.
	return locked


def _default_output_path(art: Artifact, build_dir: Path) -> Path:
	if art.kind == "library":
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
	# Project root: parent of manifest_dir when the manifest lives at
	# `<root>/drift/manifest.json` (the canonical post-rename layout); else
	# the manifest dir itself (legacy or non-standard).  See
	# build_cmd.project_root_for() for the resolution rule.  Source paths,
	# build output, and asset paths are all project-root-relative; lock
	# and deploy-config are sibling-of-manifest (manifest_dir-relative).
	from tools.drift_deploy.build_cmd import project_root_for
	project_root = project_root_for(manifest_dir)

	# Select artifact.
	art = _resolve_artifact(manifest, args.artifact_name)

	# Resolve driftc.
	driftc = _resolve_driftc(args.driftc)

	# Package roots (resolved before deps — needed for lock compatibility check).
	package_roots = _resolve_package_roots(args.package_root, manifest_dir)

	# Collect the set of library artifact names declared in THIS
	# manifest — those are the IDs that may legitimately appear in
	# the lock with `dep_type: "co-artifact"`.  Every other pkg_id
	# claiming co-artifact status is rejected at lock-verify time.
	co_artifact_names = {a.name for a in manifest.artifacts if a.kind == "library"}

	# Resolve deps.
	resolved = _resolve_deps(art, manifest_dir, package_roots,
		co_artifact_names=co_artifact_names,
		source_rebuild=getattr(args, "source_rebuild", False))

	# Native lib paths.
	native_lib_paths = _resolve_native_lib_paths(args.native_lib_path, manifest_dir)

	# Dual-runtime workstream: `--debug` and `DRIFT_DEBUG=1` are co-equal
	# selectors for the debug-style runtime variant.  The selection is
	# threaded into driftc via the env var so the underlying compiler can
	# pick the matching runtime archive without a special CLI flag of its
	# own.  No alias for the retired DRIFT_OPTIMIZED / --optimized.
	debug_style_build = args.debug or _env_true("DRIFT_DEBUG")

	# Output path.  Build artifacts go under <project_root>/build, not
	# inside the drift/ subdirectory.
	build_dir = project_root / "build"
	if args.output:
		output_path = args.output
	else:
		output_path = _default_output_path(art, build_dir)

	# Ensure output directory exists.
	output_path.parent.mkdir(parents=True, exist_ok=True)

	# Resolve target per artifact kind.
	if art.kind == "app":
		if args.target is None or args.target == "native":
			args.target = "native"
		else:
			print(
				f"error: unsupported app target '{args.target}'; "
				f"app builds produce host-native executables. "
				f"Use --target native or omit --target.",
				file=sys.stderr,
			)
			return 1
	elif art.kind == "library":
		if args.target is None:
			args.target = "drift-dev"

	# Build command.
	if art.kind == "library":
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
	subprocess_env = _clean_env()
	if debug_style_build:
		# `--debug` and `DRIFT_DEBUG=1` are co-equal driver selectors; the
		# CLI flag normalizes to the env so the underlying compiler sees a
		# single canonical source of truth.
		subprocess_env["DRIFT_DEBUG"] = "1"
	result = subprocess.run(cmd, capture_output=True, text=True, env=subprocess_env)
	if result.returncode != 0:
		print(f"build failed for {art.kind} '{art.name}':", file=sys.stderr)
		if result.stderr:
			print(result.stderr.strip(), file=sys.stderr)
		return 1

	print(f"  output: {output_path}")
	return 0


if __name__ == "__main__":
	sys.exit(run())
