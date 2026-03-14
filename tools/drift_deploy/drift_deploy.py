# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift deploy — standardized package deploy tool.

Entry point for building, signing, smoking, and publishing Drift
package and app artifacts from a drift-package.json manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tools.drift_deploy.lockfile import (
	expand_to_dep_flags,
	read_lock,
	verify_lock_integrity,
	write_lock,
)
from tools.drift_deploy.manifest import (
	Artifact,
	Manifest,
	ManifestError,
	load_manifest,
)
from tools.drift_deploy.resolver import (
	ResolutionError,
	ResolvedDep,
	build_package_index,
	resolve_artifact,
)
from tools.drift_deploy.sidecar import write_app_sidecar


# ── Errors ───────────────────────────────────────────────────────────


class DeployError(Exception):
	"""Fatal deploy error."""
	pass


# ── CLI ──────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift deploy",
		description="Build, sign, smoke-test, and publish Drift artifacts.",
	)
	p.add_argument("--manifest", type=Path, default=Path("drift-package.json"),
		help="Path to drift-package.json (default: ./drift-package.json)")
	p.add_argument("--dest", type=Path, default=None,
		help="Publish destination root for package artifacts (required if manifest has packages)")
	p.add_argument("--app-dest", type=Path, default=None,
		help="Publish destination root for app artifacts (required if manifest has apps)")
	p.add_argument("--package-root", type=Path, action="append", default=None,
		help="Library root for resolving package_deps (repeatable; default: --dest)")
	p.add_argument("--artifact", action="append", default=None,
		help="Build only this artifact (repeatable; default: all)")
	p.add_argument("--driftc", type=Path, default=None,
		help="Path to driftc (default: driftc from PATH)")
	p.add_argument("--sign-key-file", type=Path, default=None,
		help="Ed25519 signing key file (default: $DRIFT_SIGN_KEY_FILE)")
	p.add_argument("--trust-store", type=Path, default=None,
		help="Baseline trust store for smoke overlay (default: $DRIFT_TRUST_STORE)")
	p.add_argument("--target", type=str, default=None,
		help="Target triple (default: host triple)")
	p.add_argument("--update-lock", action="store_true",
		help="Re-resolve all package_deps and rewrite drift-lock.json")
	p.add_argument("--skip-smoke", action="store_true",
		help="Skip all smoke tests (CI escape hatch)")
	p.add_argument("--dry-run", action="store_true",
		help="Build + sign + smoke but do not publish")
	return p


def _resolve_driftc(args: argparse.Namespace) -> Path:
	if args.driftc:
		p = args.driftc
		if not p.exists():
			raise DeployError(f"--driftc path does not exist: {p}")
		return p
	driftc = shutil.which("driftc")
	if driftc:
		return Path(driftc)
	raise DeployError("driftc not found on PATH; pass --driftc explicitly")


def _resolve_target(args: argparse.Namespace) -> str:
	if args.target:
		return args.target
	machine = platform.machine()
	arch_map = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
	arch = arch_map.get(machine, machine)
	return f"{arch}-linux-gnu"


def _resolve_sign_key(args: argparse.Namespace) -> Path | None:
	"""Resolve signing key path. Returns None if no key available."""
	if args.sign_key_file:
		if not args.sign_key_file.exists():
			raise DeployError(f"--sign-key-file does not exist: {args.sign_key_file}")
		return args.sign_key_file
	env_path = os.environ.get("DRIFT_SIGN_KEY_FILE")
	if env_path:
		p = Path(env_path)
		if not p.exists():
			raise DeployError(f"$DRIFT_SIGN_KEY_FILE does not exist: {p}")
		return p
	return None


def _resolve_trust_store(args: argparse.Namespace) -> Path | None:
	if args.trust_store:
		return args.trust_store
	env_path = os.environ.get("DRIFT_TRUST_STORE")
	if env_path:
		return Path(env_path)
	return None


def _get_compiler_version(driftc: Path) -> str:
	try:
		result = subprocess.run(
			[str(driftc), "--version"],
			capture_output=True, text=True, timeout=10,
		)
		# driftc --version outputs "driftc X.Y.Z-dev" or similar.
		for line in result.stdout.strip().splitlines():
			parts = line.strip().split()
			if len(parts) >= 2:
				return parts[-1]
		return result.stdout.strip()
	except Exception:
		return "unknown"


# ── Artifact ordering ────────────────────────────────────────────────


def _topo_sort_artifacts(artifacts: list[Artifact]) -> list[Artifact]:
	"""
	Topological sort: packages before apps that depend on them.

	Intra-manifest dependencies: if app A depends on package P (both
	in manifest), P must be built first.
	"""
	by_name = {a.name: a for a in artifacts}
	# Build adjacency: a depends on b if a.package_deps references b.name.
	intra_deps: dict[str, set[str]] = {a.name: set() for a in artifacts}
	for a in artifacts:
		for dep in a.package_deps:
			if dep.name in by_name:
				intra_deps[a.name].add(dep.name)

	# Kahn's algorithm.
	in_degree = {name: 0 for name in by_name}
	for name, deps in intra_deps.items():
		for d in deps:
			in_degree[name] += 1  # name depends on d

	queue = sorted(n for n, deg in in_degree.items() if deg == 0)
	order: list[str] = []
	while queue:
		n = queue.pop(0)
		order.append(n)
		for name, deps in intra_deps.items():
			if n in deps:
				deps.discard(n)
				in_degree[name] -= 1
				if in_degree[name] == 0:
					queue.append(name)
					queue.sort()  # deterministic

	if len(order) != len(by_name):
		raise DeployError("circular intra-manifest dependency detected")

	return [by_name[n] for n in order]


# ── Resolution / lock ────────────────────────────────────────────────


def _resolve_or_load_lock(
	manifest: Manifest,
	artifacts: list[Artifact],
	manifest_path: Path,
	package_roots: list[Path],
	update_lock: bool,
) -> dict[str, dict[str, ResolvedDep]]:
	"""
	Resolve dependencies or load from lock file.

	Returns {artifact_name → {package_id → ResolvedDep}}.
	Artifacts with no package_deps get an empty dict.
	"""
	lock_path = manifest_path.parent / "drift-lock.json"

	# Determine which artifacts need resolution.
	need_resolution = [a for a in artifacts if a.package_deps]

	if not need_resolution:
		return {a.name: {} for a in artifacts}

	# Build package index for resolution.
	pkg_index = build_package_index(package_roots)

	# Try loading existing lock.
	existing_lock: dict[str, dict[str, ResolvedDep]] | None = None
	if lock_path.exists() and not update_lock:
		try:
			existing_lock = read_lock(lock_path)
		except ValueError as e:
			raise DeployError(f"failed to read {lock_path}: {e}")

	result: dict[str, dict[str, ResolvedDep]] = {}

	for art in artifacts:
		if not art.package_deps:
			result[art.name] = {}
			continue

		direct_deps = [(dep.name, dep.version) for dep in art.package_deps]

		if existing_lock is not None and not update_lock:
			# Use existing lock if available for this artifact.
			if art.name not in existing_lock:
				raise DeployError(
					f"artifact '{art.name}' not found in {lock_path}; "
					f"run with --update-lock to re-resolve"
				)
			locked = existing_lock[art.name]

			# Verify all direct deps are covered.
			for dep_name, _dep_ver in direct_deps:
				if dep_name not in locked:
					raise DeployError(
						f"artifact '{art.name}': package_dep '{dep_name}' not in lock file; "
						f"run with --update-lock to re-resolve"
					)

			# Verify integrity.
			errors = verify_lock_integrity(locked, pkg_index)
			if errors:
				raise DeployError(
					f"artifact '{art.name}': lock integrity check failed:\n"
					+ "\n".join(f"  {e}" for e in errors)
				)

			result[art.name] = locked
		else:
			# Resolve from scratch.
			try:
				resolved = resolve_artifact(
					art.name, direct_deps, pkg_index,
					searched_roots=package_roots,
				)
			except ResolutionError as e:
				raise DeployError(str(e))
			result[art.name] = resolved

	# Write lock file (all artifacts, including no-dep ones as empty).
	lock_artifacts = {name: deps for name, deps in result.items() if deps}
	if lock_artifacts:
		write_lock(lock_path, lock_artifacts)

	# Fill in no-dep artifacts.
	for art in artifacts:
		if art.name not in result:
			result[art.name] = {}

	return result


# ── Build ────────────────────────────────────────────────────────────


def _build_package(
	art: Artifact,
	*,
	driftc: Path,
	target: str,
	resolved: dict[str, ResolvedDep],
	staged_install: Path,
	manifest_dir: Path,
	package_roots: list[Path],
) -> Path:
	"""Build a package artifact. Returns path to staged .dmp."""
	out_dmp = staged_install / f"{art.name}.dmp"
	staged_install.mkdir(parents=True, exist_ok=True)

	cmd = [
		str(driftc),
		"--emit-package", str(out_dmp),
		"--package-id", art.name,
		"--package-version", art.version,
		"--package-target", target,
	]

	# Native deps.
	for nd in art.native_deps:
		cmd.extend(["--native-link-lib", nd.lib])

	# Package deps as declarations.
	for pd in art.package_deps:
		cmd.extend(["--package-dep", f"{pd.name}={pd.version}"])

	# Exact resolved versions via --dep.
	cmd.extend(expand_to_dep_flags(resolved))

	# Package roots.
	for pr in package_roots:
		cmd.extend(["--package-root", str(pr)])

	# Source inputs: entry module + declared module paths.
	cmd.append(str(manifest_dir / art.entry_module))
	for mod_path in art.modules:
		resolved_mod = manifest_dir / mod_path
		if str(resolved_mod) != str(manifest_dir / art.entry_module):
			cmd.append(str(resolved_mod))

	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise DeployError(
			f"build failed for package '{art.name}':\n"
			f"command: {' '.join(cmd)}\n"
			f"stderr: {result.stderr.strip()}"
		)

	return out_dmp


def _build_app(
	art: Artifact,
	*,
	driftc: Path,
	target: str,
	resolved: dict[str, ResolvedDep],
	staged_install: Path,
	manifest_dir: Path,
	package_roots: list[Path],
) -> Path:
	"""Build an app artifact. Returns path to staged binary."""
	out_bin = staged_install / art.name
	staged_install.mkdir(parents=True, exist_ok=True)

	cmd = [str(driftc), "-o", str(out_bin), "--target", target]

	# Exact resolved versions via --dep.
	cmd.extend(expand_to_dep_flags(resolved))

	# Package roots.
	for pr in package_roots:
		cmd.extend(["--package-root", str(pr)])

	# Native deps for link-time.
	for nd in art.native_deps:
		cmd.extend(["--link-lib", nd.lib])

	# Source inputs: entry module + declared module paths.
	cmd.append(str(manifest_dir / art.entry_module))
	for mod_path in art.modules:
		resolved_mod = manifest_dir / mod_path
		if str(resolved_mod) != str(manifest_dir / art.entry_module):
			cmd.append(str(resolved_mod))

	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise DeployError(
			f"build failed for app '{art.name}':\n"
			f"command: {' '.join(cmd)}\n"
			f"stderr: {result.stderr.strip()}"
		)

	return out_bin


# ── Sign ─────────────────────────────────────────────────────────────


def _sign_package(
	dmp_path: Path,
	*,
	sign_key: Path,
) -> Path:
	"""Sign a .dmp and produce .dmp.sig. Returns path to sidecar."""
	from lang.drift.sign import SignOptions, sign_package_v0

	sig_path = dmp_path.parent / f"{dmp_path.name}.sig"
	sign_package_v0(SignOptions(
		package_path=dmp_path,
		key_seed_path=sign_key,
		key_seed_text=None,
		out_path=sig_path,
		add_signature=False,
		include_pubkey=True,
	))
	return sig_path


# ── Assets ───────────────────────────────────────────────────────────


def _stage_assets(
	art: Artifact,
	*,
	manifest_dir: Path,
	staged_install: Path,
) -> None:
	"""Copy declared assets into staged install directory."""
	if not art.assets:
		return

	assets_dir = staged_install / "assets"
	assets_dir.mkdir(parents=True, exist_ok=True)

	for asset_path_str in art.assets:
		src = manifest_dir / asset_path_str
		if not src.exists():
			raise DeployError(
				f"artifact '{art.name}': declared asset not found: {asset_path_str}"
			)
		dst = assets_dir / asset_path_str
		if src.is_dir():
			shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
		else:
			dst.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(str(src), str(dst))


# ── Smoke ────────────────────────────────────────────────────────────


def _run_baseline_smoke_package(
	art: Artifact,
	*,
	driftc: Path,
	staged_install: Path,
	staged_pkg_root: Path,
	staged_trust: Path | None,
) -> None:
	"""Built-in baseline smoke for package artifacts."""
	# Generate a trivial consumer that imports the staged package.
	smoke_dir = staged_install.parent / f"_smoke_{art.name}"
	smoke_dir.mkdir(parents=True, exist_ok=True)

	consumer_src = smoke_dir / "smoke_consumer.drift"
	# Minimal consumer: import the package module.
	consumer_src.write_text(
		f'import {art.name}\nfn main() {{}}\n',
		encoding="utf-8",
	)

	consumer_bin = smoke_dir / "smoke_consumer"
	cmd = [
		str(driftc),
		"-o", str(consumer_bin),
		"--package-root", str(staged_pkg_root),
		f"--dep", f"{art.name}@{art.version}",
	]
	if staged_trust:
		cmd.extend(["--trust-store", str(staged_trust)])
	cmd.append(str(consumer_src))

	has_native = bool(art.native_deps)

	if has_native:
		# Compile + link + run.
		result = subprocess.run(cmd, capture_output=True, text=True)
		if result.returncode != 0:
			raise DeployError(
				f"baseline smoke failed for '{art.name}' (compile+link):\n"
				f"{result.stderr.strip()}"
			)
		# Run.
		run_result = subprocess.run(
			[str(consumer_bin)], capture_output=True, text=True, timeout=30,
		)
		if run_result.returncode != 0:
			raise DeployError(
				f"baseline smoke failed for '{art.name}' (run):\n"
				f"{run_result.stderr.strip()}"
			)
	else:
		# Compile only (--test-build-only if available, else just compile).
		cmd.append("--test-build-only")
		result = subprocess.run(cmd, capture_output=True, text=True)
		if result.returncode != 0:
			# Retry without --test-build-only in case it's not supported.
			cmd.remove("--test-build-only")
			result = subprocess.run(cmd, capture_output=True, text=True)
			if result.returncode != 0:
				raise DeployError(
					f"baseline smoke failed for '{art.name}' (compile):\n"
					f"{result.stderr.strip()}"
				)


def _run_baseline_smoke_app(
	art: Artifact,
	*,
	staged_bin: Path,
) -> None:
	"""Built-in baseline smoke for app artifacts: run the binary."""
	if not staged_bin.exists():
		raise DeployError(f"baseline smoke: staged binary not found: {staged_bin}")

	result = subprocess.run(
		[str(staged_bin), "--help"],
		capture_output=True, text=True, timeout=30,
	)
	# Accept exit 0 or exit 1 (--help may not be implemented).
	# But crash (signal) is a failure.
	if result.returncode < 0:
		raise DeployError(
			f"baseline smoke failed for app '{art.name}' (crashed with signal {-result.returncode}):\n"
			f"{result.stderr.strip()}"
		)


def _run_custom_smoke(
	art: Artifact,
	*,
	env: dict[str, str],
) -> None:
	"""Run artifact's custom smoke_command if configured."""
	if not art.smoke_command:
		return

	result = subprocess.run(
		art.smoke_command,
		env=env,
		capture_output=True, text=True, timeout=300,
	)
	if result.returncode != 0:
		raise DeployError(
			f"custom smoke failed for '{art.name}' (exit {result.returncode}):\n"
			f"command: {art.smoke_command}\n"
			f"stderr: {result.stderr.strip()}"
		)


# ── Publish ──────────────────────────────────────────────────────────


def _publish_package(
	art: Artifact,
	*,
	staged_install: Path,
	dest: Path,
) -> Path:
	"""Atomically publish a package artifact. Returns publish directory."""
	pub_dir = dest / art.name / art.version
	if pub_dir.exists():
		# Back up for rollback.
		backup = pub_dir.parent / f"{art.version}.bak"
		if backup.exists():
			shutil.rmtree(str(backup))
		pub_dir.rename(backup)
		try:
			shutil.copytree(str(staged_install), str(pub_dir))
		except Exception:
			# Rollback.
			if backup.exists():
				if pub_dir.exists():
					shutil.rmtree(str(pub_dir))
				backup.rename(pub_dir)
			raise
		# Success — remove backup.
		if backup.exists():
			shutil.rmtree(str(backup))
	else:
		pub_dir.parent.mkdir(parents=True, exist_ok=True)
		shutil.copytree(str(staged_install), str(pub_dir))

	return pub_dir


def _publish_app(
	art: Artifact,
	*,
	staged_install: Path,
	app_dest: Path,
) -> Path:
	"""Publish an app artifact. Returns publish directory."""
	pub_dir = app_dest / art.name / art.version
	if pub_dir.exists():
		backup = pub_dir.parent / f"{art.version}.bak"
		if backup.exists():
			shutil.rmtree(str(backup))
		pub_dir.rename(backup)
		try:
			shutil.copytree(str(staged_install), str(pub_dir))
		except Exception:
			if backup.exists():
				if pub_dir.exists():
					shutil.rmtree(str(pub_dir))
				backup.rename(pub_dir)
			raise
		if backup.exists():
			shutil.rmtree(str(backup))
	else:
		pub_dir.parent.mkdir(parents=True, exist_ok=True)
		shutil.copytree(str(staged_install), str(pub_dir))

	return pub_dir


# ── Per-artifact pipeline ────────────────────────────────────────────


def _deploy_artifact(
	art: Artifact,
	*,
	driftc: Path,
	target: str,
	resolved: dict[str, ResolvedDep],
	stage_dir: Path,
	manifest_dir: Path,
	package_roots: list[Path],
	dest: Path | None,
	app_dest: Path | None,
	sign_key: Path | None,
	baseline_trust: Path | None,
	skip_smoke: bool,
	dry_run: bool,
	compiler_version: str,
	staged_pkg_root: Path,
) -> None:
	"""Full pipeline for one artifact: build → sign → assets → smoke → publish."""
	staged_install = stage_dir / art.name / art.version

	# ── Step 1: Build ──
	if art.kind == "package":
		dmp_path = _build_package(
			art,
			driftc=driftc,
			target=target,
			resolved=resolved,
			staged_install=staged_install,
			manifest_dir=manifest_dir,
			package_roots=package_roots,
		)
	else:
		bin_path = _build_app(
			art,
			driftc=driftc,
			target=target,
			resolved=resolved,
			staged_install=staged_install,
			manifest_dir=manifest_dir,
			package_roots=package_roots,
		)

	# ── Step 2: Sign (package only) ──
	sig_path: Path | None = None
	staged_trust_path: Path | None = None

	if art.kind == "package":
		if sign_key is None:
			raise DeployError(
				f"artifact '{art.name}': signing key required for package artifacts; "
				f"pass --sign-key-file or set $DRIFT_SIGN_KEY_FILE"
			)
		sig_path = _sign_package(dmp_path, sign_key=sign_key)

		# Set up staged package root layout for smoke.
		# Layout: staged_pkg_root/<name>/<version>/<name>.dmp (+.sig)
		smoke_pkg_dir = staged_pkg_root / art.name / art.version
		smoke_pkg_dir.mkdir(parents=True, exist_ok=True)
		shutil.copy2(str(dmp_path), str(smoke_pkg_dir / dmp_path.name))
		if sig_path:
			shutil.copy2(str(sig_path), str(smoke_pkg_dir / sig_path.name))

		# Build staged trust overlay.
		from tools.drift_deploy.staged_trust import (
			build_staged_trust,
			extract_pubkey_from_seed,
		)
		try:
			pubkey = extract_pubkey_from_seed(sign_key)
			staged_trust_path = stage_dir / "drift" / "trust.json"
			build_staged_trust(
				baseline_trust_path=baseline_trust,
				signer_pubkey_raw=pubkey,
				artifact_namespace=art.name,
				out_path=staged_trust_path,
			)
		except Exception as e:
			raise DeployError(f"staged trust generation failed: {e}")

	# ── Step 3: Assets ──
	_stage_assets(art, manifest_dir=manifest_dir, staged_install=staged_install)

	# ── Step 4: App sidecar ──
	if art.kind == "app" and resolved:
		sidecar_path = staged_install / f"{art.name}.meta.json"
		write_app_sidecar(
			sidecar_path,
			app_name=art.name,
			app_version=art.version,
			target=target,
			compiler_version=compiler_version,
			resolved_deps=resolved,
		)

	# ── Step 5: Smoke ──
	if skip_smoke:
		print(f"  warning: --skip-smoke: smoke skipped for '{art.name}'", file=sys.stderr)
	else:
		if art.kind == "package":
			_run_baseline_smoke_package(
				art,
				driftc=driftc,
				staged_install=staged_install,
				staged_pkg_root=staged_pkg_root,
				staged_trust=staged_trust_path,
			)
		else:
			_run_baseline_smoke_app(art, staged_bin=staged_install / art.name)

		# Smoke env for custom command.
		smoke_env = dict(os.environ)
		smoke_env.update({
			"DRIFT_STAGE_DIR": str(stage_dir),
			"DRIFT_STAGED_PKG_ROOT": str(staged_pkg_root),
			"DRIFT_STAGED_INSTALL": str(staged_install),
			"DRIFT_STAGED_DRIFTC": str(driftc),
			"DRIFT_ARTIFACT_NAME": art.name,
			"DRIFT_ARTIFACT_VERSION": art.version,
			"DRIFT_ARTIFACT_KIND": art.kind,
		})
		if art.kind == "package":
			smoke_env["DRIFT_STAGED_PKG"] = str(dmp_path)
			if sig_path:
				smoke_env["DRIFT_STAGED_SIG"] = str(sig_path)
		else:
			smoke_env["DRIFT_STAGED_BIN"] = str(staged_install / art.name)
		if staged_trust_path:
			smoke_env["DRIFT_STAGED_TRUST"] = str(staged_trust_path)

		_run_custom_smoke(art, env=smoke_env)

	# ── Step 6: Publish ──
	if dry_run:
		print(f"  dry-run: skipping publish for '{art.name}'")
		return

	if art.kind == "package":
		if dest is None:
			raise DeployError("--dest required for package artifacts")
		pub = _publish_package(art, staged_install=staged_install, dest=dest)
		print(f"  published: {pub}")
	else:
		if app_dest is None:
			raise DeployError("--app-dest required for app artifacts")
		pub = _publish_app(art, staged_install=staged_install, app_dest=app_dest)
		print(f"  published: {pub}")


# ── Main ─────────────────────────────────────────────────────────────


def run(argv: list[str] | None = None) -> int:
	"""Main entry point. Returns exit code."""
	parser = build_arg_parser()
	args = parser.parse_args(argv)

	try:
		return _run_impl(args)
	except DeployError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except ManifestError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print("\ninterrupted", file=sys.stderr)
		return 130


def _run_impl(args: argparse.Namespace) -> int:
	# Load manifest.
	manifest = load_manifest(args.manifest)
	manifest_dir = args.manifest.resolve().parent

	# Select artifacts.
	if args.artifact:
		art_names = set(args.artifact)
		all_names = {a.name for a in manifest.artifacts}
		unknown = art_names - all_names
		if unknown:
			raise DeployError(f"unknown artifact(s): {', '.join(sorted(unknown))}")
		artifacts = [a for a in manifest.artifacts if a.name in art_names]
	else:
		artifacts = list(manifest.artifacts)

	# Validate dest requirements.
	has_packages = any(a.kind == "package" for a in artifacts)
	has_apps = any(a.kind == "app" for a in artifacts)

	if has_packages and not args.dest:
		raise DeployError("--dest required when manifest contains package artifacts")
	if has_apps and not args.app_dest:
		raise DeployError("--app-dest required when manifest contains app artifacts")

	# Resolve toolchain.
	driftc = _resolve_driftc(args)
	target = _resolve_target(args)
	sign_key = _resolve_sign_key(args)
	baseline_trust = _resolve_trust_store(args)
	compiler_version = _get_compiler_version(driftc)

	# Package roots: default to --dest.
	package_roots = args.package_root or ([args.dest] if args.dest else [])

	# Signing key required for package artifacts.
	if has_packages and sign_key is None:
		raise DeployError(
			"signing key required for package artifacts; "
			"pass --sign-key-file or set $DRIFT_SIGN_KEY_FILE"
		)

	# Topological sort.
	artifacts = _topo_sort_artifacts(artifacts)

	print(f"drift deploy: {len(artifacts)} artifact(s), target={target}")
	print(f"  driftc: {driftc}")
	if args.dest:
		print(f"  dest: {args.dest}")
	if args.app_dest:
		print(f"  app-dest: {args.app_dest}")

	# ── Resolution / lock ──
	resolved_map = _resolve_or_load_lock(
		manifest, artifacts, args.manifest.resolve(),
		package_roots, args.update_lock,
	)

	# ── Per-artifact pipeline ──
	stage_dir = Path(tempfile.mkdtemp(
		prefix=".drift-deploy-staging.",
		dir=args.dest.parent if args.dest else manifest_dir,
	))

	staged_pkg_root = stage_dir / "_pkg_root"
	staged_pkg_root.mkdir(parents=True, exist_ok=True)

	# If we have external package roots, make the staged root include them
	# by symlinking existing packages (for smoke resolution).
	for pr in package_roots:
		if pr.exists() and pr.is_dir():
			for pkg_dir in sorted(pr.iterdir()):
				if pkg_dir.is_dir():
					target_link = staged_pkg_root / pkg_dir.name
					if not target_link.exists():
						target_link.symlink_to(pkg_dir.resolve())

	try:
		for art in artifacts:
			print(f"\n{'='*60}")
			print(f"artifact: {art.name} ({art.kind}) v{art.version}")
			print(f"{'='*60}")

			resolved = resolved_map.get(art.name, {})
			if resolved:
				print(f"  resolved deps: {', '.join(f'{k}@{v.version}' for k, v in sorted(resolved.items()))}")

			_deploy_artifact(
				art,
				driftc=driftc,
				target=target,
				resolved=resolved,
				stage_dir=stage_dir,
				manifest_dir=manifest_dir,
				package_roots=[staged_pkg_root] + package_roots,
				dest=args.dest,
				app_dest=args.app_dest,
				sign_key=sign_key,
				baseline_trust=baseline_trust,
				skip_smoke=args.skip_smoke,
				dry_run=args.dry_run,
				compiler_version=compiler_version,
				staged_pkg_root=staged_pkg_root,
			)
	finally:
		# Clean up staging directory.
		shutil.rmtree(str(stage_dir), ignore_errors=True)

	print(f"\ndrift deploy: done ({len(artifacts)} artifact(s))")
	return 0


if __name__ == "__main__":
	sys.exit(run())
