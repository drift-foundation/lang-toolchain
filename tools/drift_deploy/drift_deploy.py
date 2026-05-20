# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift deploy — standardized package deploy tool.

Entry point for building, signing, smoking, and publishing Drift
package and app artifacts from a drift/manifest.json manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tools.drift_deploy.build_cmd import UserPath, build_app_cmd, build_package_cmd, resolve_driftc
from tools.drift_deploy.lockfile import (
	expand_to_dep_flags,
	read_lock,
	verify_lock_compatibility,
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
)
from tools.drift_deploy.provenance import (
	CompilerInfo,
	build_provenance,
	build_provenance_bundle,
	compress_provenance_bundle,
	parse_compiler_info,
	provenance_sha256,
	write_provenance,
	write_provenance_bundle,
)


# ── Errors ───────────────────────────────────────────────────────────


class DeployError(Exception):
	"""Fatal deploy error."""
	pass


# ── Subprocess environment ───────────────────────────────────────────

# Keys to scrub from child process environments. PYTHONPATH leaks the
# deploy tool's import roots into PEX-based driftc, causing it to pick
# up unbundled lang/ modules and crash with ModuleNotFoundError.
_SCRUB_ENV_KEYS = frozenset({"PYTHONPATH", "PYTHONHOME"})


def _clean_env() -> dict[str, str]:
	"""Build a clean environment for driftc subprocess calls."""
	return {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV_KEYS}


# ── CLI ──────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift deploy",
		description="Build, sign, smoke-test, and publish Drift artifacts.",
	)
	p.add_argument("--manifest", type=UserPath, default=Path("drift") / "manifest.json",
		help="Path to drift/manifest.json (default: ./drift/manifest.json)")
	p.add_argument("--dest", type=UserPath, default=None,
		help="Publish destination root for package artifacts (required if manifest has packages)")
	p.add_argument("--app-dest", type=UserPath, default=None,
		help="Publish destination root for app artifacts (required if manifest has apps)")
	p.add_argument("--package-root", type=UserPath, action="append", default=None,
		help="Library root for resolving package_deps (repeatable; default: --dest)")
	p.add_argument("--artifact", action="append", default=None,
		help="Build only this artifact (repeatable; default: all)")
	p.add_argument("--driftc", type=UserPath, default=None,
		help="Path to driftc (default: driftc from PATH)")
	p.add_argument("--sign-key-file", type=UserPath, default=None,
		help="Ed25519 signing key file (default: $DRIFT_SIGN_KEY_FILE)")
	p.add_argument("--trust-store", type=UserPath, default=None,
		help="Baseline trust store for smoke overlay (default: $DRIFT_TRUST_STORE)")
	p.add_argument("--target", type=str, default=None,
		help="Target triple (default: host triple)")
	p.add_argument("--native-lib-path", type=UserPath, action="append", default=None,
		help="Native library search path for linker (repeatable; also: $DRIFT_NATIVE_LIB_PATH, drift/deploy-config.json)")
	p.add_argument("--skip-smoke", action="store_true",
		help="Skip all smoke tests (CI escape hatch)")
	p.add_argument("--dry-run", action="store_true",
		help="Build + sign + smoke but do not publish")
	p.add_argument("--source-rebuild", action="store_true",
		help=(
			"Source-rebuild certification mode (downstream consumer "
			"role): verify each staged dep against orch's run "
			"snapshot (see `--run-snapshot`), not against lock "
			"equality or local trust-store authorisation.  Requires "
			"`--run-snapshot` or `DRIFT_RUN_SNAPSHOT`; no snapshot "
			"is a hard fail.  Manual synonym for the env-driven "
			"path `DRIFT_CERT_MODE=certify`; orch certification runs "
			"use the env form.  Normal local `drift deploy` (TLS or "
			"any team publishing their own artifact) does not set "
			"this flag and does not set `DRIFT_CERT_MODE`."
		))
	p.add_argument("--run-snapshot", type=UserPath, default=None,
		help=(
			"Path to the orch-produced run snapshot "
			"(`tools.drift_deploy.run_snapshot` JSON v0).  Required "
			"under `--source-rebuild` / `DRIFT_CERT_MODE=certify`.  "
			"Also honoured via `DRIFT_RUN_SNAPSHOT=<path>`; CLI "
			"wins on conflict.  Pins source identity (scid + signer "
			"kids) per package so downstream consumers verify they "
			"are consuming the exact source graph orch certified "
			"without carrying upstream author trust in local "
			"`drift/trust.json`."
		))
	return p


# Uniform lane selector: `--source-rebuild` CLI flag OR any
# `DRIFT_CERT_MODE` value.  Both stage and certify consume deps
# under source-rebuild semantics (fresh-resolve against snapshot-
# gated index; lock = evidence).  The difference is whether
# intra-manifest co-artifacts (producer outputs of THIS deploy)
# are exempt from the snapshot gate —
# `producer_output_exemption_active` returns True iff stage.
from tools.drift_deploy.build_cmd import CertModeError
from tools.drift_deploy.build_cmd import env_true as _env_true
from tools.drift_deploy.build_cmd import (
	producer_output_exemption_active as _producer_output_exemption_active,
)
from tools.drift_deploy.build_cmd import source_rebuild_enabled as _source_rebuild_enabled


def _resolve_driftc(args: argparse.Namespace) -> Path:
	try:
		result = resolve_driftc(args.driftc)
	except ValueError as e:
		raise DeployError(str(e))
	if result is None:
		raise DeployError("driftc not found (no sibling binary, not on PATH); pass --driftc explicitly")
	return result


def _resolve_target(args: argparse.Namespace) -> str:
	"""
	Resolve target triple.

	Default is 'drift-dev' — the standard target for all current Drift
	packages and the stdlib. This matches the ABI fingerprint target
	used by the compiler's deploy pipeline (tools/deploy/steps/stdlib.py).
	Pass --target explicitly for cross-compilation or non-standard targets.
	"""
	if args.target:
		return args.target
	return "drift-dev"


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


def _resolve_native_lib_paths(args: argparse.Namespace, manifest_dir: Path) -> list[Path]:
	"""
	Merge native library search paths from three sources.

	Precedence (lowest to highest):
	  1. $DRIFT_NATIVE_LIB_PATH (colon-separated)
	  2. drift/deploy-config.json "native_lib_paths"
	  3. --native-lib-path CLI flags

	All sources are concatenated in order. The linker processes -L flags
	left-to-right, so highest-priority paths appear last.

	All paths must be absolute. Relative paths are rejected because build
	and smoke steps run from staging/temp directories, making relative
	paths ambiguous and fragile.
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
					raise DeployError(
						f"$DRIFT_NATIVE_LIB_PATH: relative path '{p}' not allowed; "
						f"absolute paths are required for native library search hints"
					)
				result.append(pp)

	# 2. Config file.
	config_path = manifest_dir / "deploy-config.json"
	if config_path.exists():
		try:
			config = json.loads(config_path.read_text(encoding="utf-8"))
		except (json.JSONDecodeError, OSError) as e:
			raise DeployError(f"failed to read {config_path}: {e}")
		if not isinstance(config, dict):
			raise DeployError(f"{config_path} must be a JSON object")
		raw_paths = config.get("native_lib_paths", [])
		if not isinstance(raw_paths, list):
			raise DeployError(f"{config_path}: 'native_lib_paths' must be an array")
		for entry in raw_paths:
			if not isinstance(entry, str) or not entry:
				raise DeployError(f"{config_path}: 'native_lib_paths' entries must be non-empty strings")
			ep = Path(entry)
			if not ep.is_absolute():
				raise DeployError(
					f"{config_path}: relative path '{entry}' not allowed in 'native_lib_paths'; "
					f"absolute paths are required for native library search hints"
				)
			result.append(ep)

	# 3. CLI flags (highest priority).
	if args.native_lib_path:
		for nlp in args.native_lib_path:
			if not nlp.is_absolute():
				raise DeployError(
					f"--native-lib-path: relative path '{nlp}' not allowed; "
					f"absolute paths are required for native library search hints"
				)
		result.extend(args.native_lib_path)

	return result


def _get_compiler_info(driftc: Path) -> CompilerInfo:
	try:
		result = subprocess.run(
			[str(driftc), "--version"],
			capture_output=True, text=True, timeout=10, env=_clean_env(),
		)
		return parse_compiler_info(result.stdout)
	except Exception:
		return CompilerInfo(version="unknown", abi=0, commit="unknown")


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


def _resolve_artifact_deps(
	art: Artifact,
	*,
	package_roots: list[Path],
	lock_path: Path,
	existing_lock: dict[str, dict[str, ResolvedDep]] | None,
	co_artifact_names: set[str] | None = None,
	source_rebuild: bool = False,
	run_snapshot: Any = None,
	snapshot_exempt_ids: set[str] | None = None,
) -> dict[str, ResolvedDep]:
	"""
	Load locked dependencies for a single artifact.

	Deploy is read-only with respect to drift/lock.json.  If the lock
	is missing or stale, the user must run ``drift prepare`` first.

	`co_artifact_names` names the library artifacts declared in the
	current manifest.  Only those IDs may legitimately appear in the
	lock with `dep_type: "co-artifact"` — anything else claiming
	co-artifact status is treated as lock corruption and rejected.

	`snapshot_exempt_ids` is threaded into the run-snapshot-gated
	`build_package_index` call under source-rebuild.  Populated by
	the caller (`_run_impl`) with the manifest's library-artifact
	names when `DRIFT_CERT_MODE=stage` — intra-manifest co-artifacts
	published earlier in this same deploy invocation skip the
	snapshot gate because they are producer outputs of THIS run, not
	consumed deps.  Under `DRIFT_CERT_MODE=certify` or manual
	`--source-rebuild`, this is `None` and the gate fires on every
	discovered package.
	"""
	if not art.package_deps:
		return {}

	direct_deps = [(dep.name, dep.version) for dep in art.package_deps]

	if existing_lock is None:
		raise DeployError(
			f"artifact '{art.name}' has package_deps but no drift/lock.json; "
			f"run 'drift prepare' first"
		)

	# Strict: lock is authoritative.  Source-rebuild: lock is
	# evidence; we re-resolve against the trust-verified index so
	# build and --check consume the same graph.  See the symmetric
	# block in `drift_build.py::_resolve_deps` for the full rationale.
	if not source_rebuild:
		if art.name not in existing_lock:
			raise DeployError(
				f"artifact '{art.name}' not found in {lock_path}; "
				f"run 'drift prepare' to re-resolve"
			)
		for dep_name, _dep_ver in direct_deps:
			if dep_name not in existing_lock[art.name]:
				raise DeployError(
					f"artifact '{art.name}': package_dep '{dep_name}' not in lock file; "
					f"run 'drift prepare' to re-resolve"
				)
	from tools.drift_deploy.lockfile import (
		VERIFY_MODE_SOURCE_REBUILD,
		VERIFY_MODE_STRICT,
	)
	mode = VERIFY_MODE_SOURCE_REBUILD if source_rebuild else VERIFY_MODE_STRICT
	# Source-rebuild: the orch run snapshot (caller-supplied) is
	# the trust authority at index time.  Strict mode inherits
	# trust through lock equality.
	try:
		pkg_index = build_package_index(
			package_roots,
			run_snapshot=run_snapshot if source_rebuild else None,
			snapshot_exempt_ids=snapshot_exempt_ids if source_rebuild else None,
		)
	except ResolutionError as e:
		raise DeployError(str(e))
	if source_rebuild:
		# Fresh-resolve is authoritative.  Delegate to the single
		# source-rebuild authority (symmetric with drift_build).
		from tools.drift_deploy.source_rebuild import (
			print_evidence,
			resolve_source_rebuild,
		)
		rebuild = resolve_source_rebuild(
			artifact=art,
			package_roots=package_roots,
			manifest_dir=lock_path.parent,
			existing_lock_graph=existing_lock.get(art.name, {}),
			co_artifact_names=co_artifact_names or set(),
			pkg_index=pkg_index,
			run_snapshot=run_snapshot,
			snapshot_exempt_ids=snapshot_exempt_ids,
		)
		if rebuild.errors:
			raise DeployError(
				f"artifact '{art.name}': source-rebuild resolve "
				f"failed:\n" + "\n".join(f"  {e}" for e in rebuild.errors)
			)
		locked = rebuild.resolved_graph
		print_evidence(
			art_name=art.name,
			channel="drift deploy",
			evidence=rebuild.evidence,
		)
	else:
		locked = existing_lock[art.name]
		# Strict-mode verification — byte-exact lock vs. package index.
		errors = verify_lock_compatibility(
			locked, pkg_index,
			allowed_co_artifacts=co_artifact_names or set(),
			mode=mode,
		)
		if errors:
			raise DeployError(
				f"artifact '{art.name}': lock compatibility check failed:\n"
				+ "\n".join(f"  {e}" for e in errors)
			)
	return dict(locked)


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
	native_lib_paths: list[Path] | None = None,
	trust_store: Path | None = None,
	source_content_id: str | None = None,
) -> Path:
	"""Build a package artifact. Returns path to staged .dmp.

	`source_content_id`, if provided, is stamped verbatim into the
	emitted .dmp manifest.  Computed by the caller from stable source
	inputs (see
	`lang/driftc/packages/source_content_id.compute_artifact_source_content_id`)
	so the same value can later be reused when emitting the
	v1 author + cert claim sidecars without re-walking the source
	tree.
	"""
	out_dmp = staged_install / f"{art.name}.dmp"
	staged_install.mkdir(parents=True, exist_ok=True)

	cmd = build_package_cmd(
		art,
		driftc=driftc,
		target=target,
		resolved_deps=resolved,
		output_path=out_dmp,
		manifest_dir=manifest_dir,
		package_roots=package_roots,
		native_lib_paths=native_lib_paths,
		trust_store=trust_store,
		source_content_id=source_content_id,
	)

	result = subprocess.run(cmd, capture_output=True, text=True, env=_clean_env())
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
	native_lib_paths: list[Path] | None = None,
	trust_store: Path | None = None,
) -> Path:
	"""Build an app artifact. Returns path to staged binary."""
	out_bin = staged_install / art.name
	staged_install.mkdir(parents=True, exist_ok=True)

	cmd = build_app_cmd(
		art,
		driftc=driftc,
		target=target,
		resolved_deps=resolved,
		output_path=out_bin,
		manifest_dir=manifest_dir,
		package_roots=package_roots,
		native_lib_paths=native_lib_paths,
		trust_store=trust_store,
	)

	result = subprocess.run(cmd, capture_output=True, text=True, env=_clean_env())
	if result.returncode != 0:
		raise DeployError(
			f"build failed for app '{art.name}':\n"
			f"command: {' '.join(cmd)}\n"
			f"stderr: {result.stderr.strip()}"
		)

	return out_bin


# ── Provenance bundle collection ─────────────────────────────────────


def _collect_dep_provenance(
	resolved: dict[str, ResolvedDep],
	staged_pkg_root: Path,
) -> dict[str, dict]:
	"""Collect provenance documents from resolved dependencies.

	Scans staged_pkg_root/<dep_name>/<version>/ for provenance files
	(.provenance.zst or .provenance.json). Returns a dict mapping
	dep name → parsed provenance dict.
	"""
	result: dict[str, dict] = {}
	for dep_name, dep in sorted(resolved.items()):
		dep_dir = staged_pkg_root / dep_name / dep.version
		if not dep_dir.is_dir():
			continue
		# Prefer .provenance.zst (new format), fall back to .provenance.json (legacy).
		zst_path = dep_dir / f"{dep_name}.provenance.zst"
		json_path = dep_dir / f"{dep_name}.provenance.json"
		if zst_path.exists():
			try:
				from tools.drift_deploy.provenance import load_provenance_bundle
				bundle = load_provenance_bundle(zst_path)
				prov = bundle.get("provenance")
				if isinstance(prov, dict):
					result[dep_name] = prov
			except Exception:
				continue
		elif json_path.exists():
			try:
				prov = json.loads(json_path.read_text(encoding="utf-8"))
				if isinstance(prov, dict):
					result[dep_name] = prov
			except Exception:
				continue
	return result


def _collect_dep_keys(
	resolved: dict[str, ResolvedDep],
	staged_pkg_root: Path,
) -> dict[str, dict[str, str]]:
	"""Collect dependency-signer kids from v1 sidecars for the
	provenance bundle.

	v1 pubkeys live in the trust store, NOT in sidecars: author /
	cert claims carry kids + signatures but reference pubkeys
	indirectly.  This helper returns the kids alone.  The provenance bundle is informational (the
	cert claim's `dep_graph` is the load-bearing record of who
	attested each dep), so it's enough to record the kids and
	roles here -- consumers that need pubkey bytes resolve them
	through their own trust store.

	Returns a dict mapping kid -> {algo, kid, role} where role is
	`"author"` for author-claim signers and `"certifier"` for
	cert-claim signers.  When a kid plays both roles (common in
	the Foundation bootstrap), the dict carries `"role": "author+certifier"`.
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.cert_claim_v1 import load_cert_claim_json
	from lang.driftc.packages.sidecar_naming import (
		author_claim_filename, cert_claim_filename_prefix,
	)

	result: dict[str, dict[str, str]] = {}

	def _add(kid: str, role: str) -> None:
		entry = result.get(kid)
		if entry is None:
			result[kid] = {"algo": "ed25519", "kid": kid, "role": role}
			return
		# Merge roles when the same kid signed both author and cert.
		existing = entry.get("role", "")
		if role not in existing.split("+"):
			merged = "+".join(sorted(set(existing.split("+") + [role])))
			entry["role"] = merged

	for dep_name, dep in sorted(resolved.items()):
		dep_dir = staged_pkg_root / dep_name / dep.version
		# Author claim is per-release singleton at the canonical name.
		author_path = dep_dir / author_claim_filename(dep_name)
		if author_path.is_file():
			try:
				claim = load_author_claim_json(author_path.read_text(encoding="utf-8"))
				for sig in claim.signatures:
					_add(sig.kid, "author")
			except Exception:
				pass
		# Cert claims: per-certifier; scan by prefix.
		cert_prefix = cert_claim_filename_prefix(dep_name)
		if dep_dir.is_dir():
			for entry in sorted(dep_dir.iterdir()):
				if not entry.is_file():
					continue
				if not (entry.name.startswith(cert_prefix) and entry.name.endswith(".json")):
					continue
				try:
					cc = load_cert_claim_json(entry.read_text(encoding="utf-8"))
					for sig in cc.signatures:
						_add(sig.kid, "certifier")
				except Exception:
					continue
	return result


# ── v1 cert / author claim emit ──────────────────────────────────────


def _read_co_artifact_identity(
	staged_pkg_root: Path,
	dep_pkg_id: str,
	dep_version: str,
) -> tuple[str, str, str, str] | None:
	"""Return `(artifact_sha256, source_content_id, author_kid,
	cert_kid)` for a co-artifact dep by reading its just-emitted v1
	sidecars.  Returns None if any required piece is missing or if
	the sidecar bodies do not bind to `(dep_pkg_id, dep_version)`;
	caller fails closed.

	Binding validation (load-bearing):
	  - author_claim.body.package_id == dep_pkg_id
	  - author_claim.body.version == dep_version
	  - cert_claim.body.package_id   == dep_pkg_id
	  - cert_claim.body.version      == dep_version
	  - author_claim.body.source_content_id == cert_claim.body.source_content_id

	Without these checks a stale/corrupt sibling sidecar whose body
	bound to a different (package_id, version) would silently leak
	its `artifact_sha256` + `source_content_id` into the dependent's
	`dep_graph`.  Downstream consumers might catch the mismatch at
	verify time, but the deploy must refuse to sign a dependent
	cert claim with a misbound sibling identity in the first place.

	Failures are noted on stderr (with the specific mismatch) so the
	operator has a breadcrumb to which sibling sidecar is broken;
	the caller turns the None return into a DeployError listing all
	affected co-artifacts.

	This is the only place in C.2 where the deploy pipeline reads
	identity from "next-to-the-artifact" files.  Safe because the
	files were emitted by the SAME deploy run a few iterations
	earlier and live inside our own staged tree -- not a network-
	provided value.
	"""
	import sys
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.cert_claim_v1 import load_cert_claim_json
	from lang.driftc.packages.sidecar_naming import (
		author_claim_filename,
		cert_claim_filename_prefix,
	)

	def _warn(reason: str) -> None:
		print(
			f"warning: co-artifact identity rejected for "
			f"{dep_pkg_id}@{dep_version}: {reason}",
			file=sys.stderr,
		)

	dep_dir = staged_pkg_root / dep_pkg_id / dep_version
	if not dep_dir.is_dir():
		return None

	# Author claim: per-release singleton, canonical filename.
	author_path = dep_dir / author_claim_filename(dep_pkg_id)
	if not author_path.is_file():
		return None
	try:
		author_claim = load_author_claim_json(author_path.read_text(encoding="utf-8"))
	except Exception as e:
		_warn(f"author claim at {author_path} will not load ({e})")
		return None
	if not author_claim.signatures:
		_warn(f"author claim at {author_path} has no signatures")
		return None
	if author_claim.body.package_id != dep_pkg_id:
		_warn(
			f"author claim at {author_path} body.package_id "
			f"{author_claim.body.package_id!r} != expected "
			f"{dep_pkg_id!r}"
		)
		return None
	if author_claim.body.version != dep_version:
		_warn(
			f"author claim at {author_path} body.version "
			f"{author_claim.body.version!r} != expected "
			f"{dep_version!r}"
		)
		return None
	author_kid = author_claim.signatures[0].kid

	# Cert claim: per-certifier; the deploy run produced exactly one
	# (the deploy's own certifier kid).  If somehow several are
	# present, pick the first by sorted filename so the dep_graph
	# entry is deterministic.
	cert_prefix = cert_claim_filename_prefix(dep_pkg_id)
	cert_path: Path | None = None
	for entry in sorted(dep_dir.iterdir()):
		if entry.is_file() and entry.name.startswith(cert_prefix) and entry.name.endswith(".json"):
			cert_path = entry
			break
	if cert_path is None:
		return None
	try:
		cert_claim = load_cert_claim_json(cert_path.read_text(encoding="utf-8"))
	except Exception as e:
		_warn(f"cert claim at {cert_path} will not load ({e})")
		return None
	if not cert_claim.signatures:
		_warn(f"cert claim at {cert_path} has no signatures")
		return None
	cert_kid = cert_claim.signatures[0].kid

	body = cert_claim.body
	if body.package_id != dep_pkg_id:
		_warn(
			f"cert claim at {cert_path} body.package_id "
			f"{body.package_id!r} != expected {dep_pkg_id!r}"
		)
		return None
	if body.version != dep_version:
		_warn(
			f"cert claim at {cert_path} body.version "
			f"{body.version!r} != expected {dep_version!r}"
		)
		return None
	# SCI agreement between the two sidecars.  v1 invariant: the
	# author and cert claim attest the SAME source identity for the
	# same release.  A disagreement here means one of the two
	# sidecars is stale -- propagating either value into the
	# dependent's dep_graph would tie its cert claim to a
	# self-contradictory upstream attestation set.
	if author_claim.body.source_content_id != body.source_content_id:
		_warn(
			f"co-artifact SCI mismatch between author claim and "
			f"cert claim: author {author_claim.body.source_content_id!r} "
			f"vs cert {body.source_content_id!r}"
		)
		return None
	# Both stamps must be present and well-shaped.
	if not isinstance(body.artifact_sha256, str) or not body.artifact_sha256.startswith("sha256:"):
		_warn(
			f"cert claim at {cert_path} body.artifact_sha256 is "
			f"malformed: {body.artifact_sha256!r}"
		)
		return None
	if not isinstance(body.source_content_id, str) or not body.source_content_id.startswith("sha256:"):
		_warn(
			f"cert claim at {cert_path} body.source_content_id is "
			f"malformed: {body.source_content_id!r}"
		)
		return None
	return (body.artifact_sha256, body.source_content_id, author_kid, cert_kid)


def _attach_author_claim_to_artifact(
	*,
	package_id: str,
	package_version: str,
	source_content_id: str,
	manifest_dir: Path,
	staged_install: Path,
) -> Path:
	"""Discover the pre-signed author claim for this release, validate
	it binds to the artifact being certified, and stage it next to
	the artifact.

	Per the trust-v1 role split (and the author-key-out-of-orch hard
	gate enforced by `lang/tests/packages/test_author_key_boundary.py`),
	the deploy pipeline NEVER signs author claims itself.  The author
	publishes the claim from their workstation via `drift-author
	publish`; deploy only locates that file, verifies it matches THIS
	release, and copies it into the staged install directory.

	The binding check is load-bearing: without it, a stale
	`<pkg>.author-claim` for `demo@1.0.0` left in `drift/` would
	silently get attached to a `demo@1.0.1` build, and the certifier
	would sign a cert claim referencing an author intent that
	predates (and may not cover) the actual source bytes being
	released.  Hard-fail when:

	  - the file is missing;
	  - the body's package_id / version / source_content_id do not
	    match the artifact arguments;
	  - the file fails to parse as a v1 author claim.

	Source location: `<manifest_dir>/drift/<pkg>.author-claim` (the
	canonical name produced by `tools.drift_author.sign_and_write_author_claim`).
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.sidecar_naming import author_claim_filename

	src_dir = manifest_dir / "drift"
	canonical_name = author_claim_filename(package_id)
	src = src_dir / canonical_name
	if not src.is_file():
		raise DeployError(
			f"artifact '{package_id}': pre-signed author claim not "
			f"found at {src}.  v1 release flow requires the author "
			f"to publish the claim via `drift-author publish` BEFORE "
			f"drift_deploy runs; the deploy pipeline never holds the "
			f"author key (see tools/drift_author/ for the publish "
			f"command).  Without the author claim the deploy cannot "
			f"emit a meaningful cert claim."
		)
	try:
		claim = load_author_claim_json(src.read_text(encoding="utf-8"))
	except (ValueError, OSError) as e:
		raise DeployError(
			f"artifact '{package_id}': author claim at {src} "
			f"failed to parse as a v1 author claim ({e}).  "
			f"Re-run `drift-author publish` to regenerate."
		) from e
	body = claim.body
	if body.package_id != package_id:
		raise DeployError(
			f"author claim at {src} binds package_id "
			f"{body.package_id!r}, but this build is for "
			f"{package_id!r}.  Re-run `drift-author publish` for "
			f"{package_id!r} (or remove the stale claim from "
			f"{src_dir})."
		)
	if body.version != package_version:
		raise DeployError(
			f"author claim at {src} binds version {body.version!r}, "
			f"but this build is for {package_version!r}.  Stale "
			f"claims from a previous release must NOT be reused: "
			f"the certifier signs (artifact bytes + dep_graph + "
			f"cert_suite) but the AUTHOR's release intent is "
			f"version-specific.  Re-run `drift-author publish "
			f"--version {package_version}` and republish."
		)
	if body.source_content_id != source_content_id:
		raise DeployError(
			f"author claim at {src} binds source_content_id "
			f"{body.source_content_id!r}, but this build's source "
			f"hashed to {source_content_id!r}.  The source tree has "
			f"changed since the author signed; re-run `drift-author "
			f"publish` with the current source to refresh the claim."
		)
	dst = staged_install / canonical_name
	staged_install.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(src), str(dst))
	return dst


def _emit_cert_claim_for_artifact(
	artifact_path: Path,
	*,
	cert_key: Path,
	package_id: str,
	package_version: str,
	target: str,
	compiler_info: "CompilerInfo",
	source_content_id: str,
	artifact_sha256: str,
	resolved_deps: dict[str, "ResolvedDep"],
	direct_dep_ids: set[str],
	staged_pkg_root: Path,
	provenance_path: Path | None,
) -> Path:
	"""Sign a v1 cert claim for `artifact_path` and write the sidecar.

	The cert claim binds artifact bytes + toolchain identity + the
	full resolved transitive dep_graph + the cert-suite result (per
	O3 / O4).  Returns the sidecar path.

	Cert-suite identity defaults to `drift-deploy/v1` with result
	`pass`; production deployments override via the env vars
	`DRIFT_DEPLOY_CERT_SUITE_ID`, `DRIFT_DEPLOY_CERT_SUITE_VERSION`,
	`DRIFT_DEPLOY_CERT_SUITE_RESULT`, `DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256`
	so the same deploy CLI surfaces multiple gate identities (smoke,
	full-suite, etc.) without code changes.

	Each `resolved_deps` entry contributes a `DepGraphEntry` -- the
	consumer's closure at load time must be exactly this set for
	the cert claim to cover O3 at verify time.  For ORDINARY deps
	(external packages already resolved into the lock), all four
	identity fields (artifact_sha256, source_content_id, author_kid,
	cert_kid) come directly from the `ResolvedDep`.  For CO-ARTIFACT
	deps (sibling packages built earlier in this same deploy run
	via `_topo_sort_artifacts`'s deps-before-dependents ordering),
	the lock has empty fields by construction, so we read the
	identity back from the sibling's just-emitted sidecars in
	`staged_pkg_root/<pkg>/<version>/` -- this is NOT the silent-
	null peek path K flagged: missing sibling sidecars at this
	point indicate a deploy-order bug or a failed sibling build,
	both of which must fail closed.

	`direct_dep_ids` is the set of package ids the artifact's own
	manifest declares as direct deps (from `art.package_deps`).
	Entries in `resolved_deps` matching this set get
	`dep_kind="direct"`; the rest are `dep_kind="transitive"`.
	Without this split the cert claim would mislabel every dep as
	direct and lose the (informational but contract-visible)
	classification the v1 schema carries.

	Every transitive dep MUST contribute a non-empty
	`source_content_id`.  Falling back to a synthetic sentinel
	(e.g. `sha256:000...`) would let the certifier sign a guessed
	value the consumer can never reproduce from real source -- a
	silent O3 escape.  Hard-fail before signing.
	"""
	import hashlib as _hl
	import os
	import uuid as _uuid

	from lang.driftc.packages.cert_claim_v1 import (
		CertClaimBody,
		CertSuite,
		DepGraphEntry,
		Toolchain,
	)
	from tools.drift_deploy.cert_emit import (
		SignCertClaimOptions,
		load_cert_seed32,
		sign_and_write_cert_claim,
	)

	seed = load_cert_seed32(cert_key)

	# Build the dep_graph from the consumer's resolved closure.
	#
	# Each `DepGraphEntry` must carry the EXACT upstream identity
	# set the certifier vouches for: artifact_sha256 + SCI + the
	# kids that signed the dep's own author/cert claims.  Sourcing
	# the kids from the resolved lock (NOT best-effort sidecar
	# scanning) ties the cert claim to the same identity set the
	# resolver locked, so the cover check at consumer verify time
	# can compare apples to apples.  Fail closed for any non-co-
	# artifact dep whose identity is incomplete: silent fallback
	# to `author_kid=None` / `cert_kid=None` (the old peek path)
	# would let the certifier sign an entry whose attestation
	# chain isn't actually known to the deploy at cert time,
	# defeating O3 even when SCI is present.
	#
	# Field-name swap reminder: under the v4 lock schema the slot
	# called `author_key` carries the CERT kid (the cert-claim
	# signer in v1) and `source_attestation_key` carries the AUTHOR
	# kid (the author-claim signer in v1).  The lockfile-v5 rename
	# (deferred) will flip the spelling; the semantics are already
	# v1.
	missing_sci: list[str] = []
	missing_kids: list[str] = []
	co_artifact_unbuilt: list[str] = []
	dep_graph: list[DepGraphEntry] = []
	for dep_pkg_id in sorted(resolved_deps.keys()):
		dep = resolved_deps[dep_pkg_id]
		dep_type = getattr(dep, "dep_type", "") or "transitive"
		if dep_type == "co-artifact":
			# Sibling in this same deploy run.  Per
			# `_topo_sort_artifacts`'s deps-before-dependents
			# ordering, the sibling was deployed in an earlier
			# iteration and its v1 sidecars now sit at
			# `staged_pkg_root/<pkg>/<version>/`.  Read identity
			# back from the just-emitted cert-claim sidecar (the
			# sibling we just signed): it's the only authoritative
			# source -- the lock leaves co-artifact fields empty
			# by construction.  Missing sidecars here mean either
			# a topo-sort regression or a failed sibling build;
			# both are hard errors.
			co_identity = _read_co_artifact_identity(
				staged_pkg_root, dep_pkg_id, dep.version,
			)
			if co_identity is None:
				co_artifact_unbuilt.append(f"{dep_pkg_id}@{dep.version}")
				continue
			co_sha, co_sci, co_author_kid, co_cert_kid = co_identity
			dep_graph.append(DepGraphEntry(
				package_id=dep_pkg_id,
				version=dep.version,
				artifact_sha256=co_sha,
				source_content_id=co_sci,
				author_kid=co_author_kid,
				cert_kid=co_cert_kid,
				# A co-artifact is by definition a sibling the
				# manifest declares as a dep, so it is necessarily
				# direct (manifest.package_deps is the source).
				dep_kind="direct" if dep_pkg_id in direct_dep_ids else "transitive",
			))
			continue
		dep_sci = getattr(dep, "source_content_id", "") or ""
		if not (isinstance(dep_sci, str) and dep_sci.startswith("sha256:") and len(dep_sci) == 7 + 64):
			missing_sci.append(f"{dep_pkg_id}@{dep.version}")
			continue
		dep_author_kid = getattr(dep, "source_attestation_key", "") or ""
		dep_cert_kid = getattr(dep, "author_key", "") or ""
		if not dep_author_kid or not dep_cert_kid:
			missing_kids.append(
				f"{dep_pkg_id}@{dep.version} "
				f"(author_kid={dep_author_kid!r}, cert_kid={dep_cert_kid!r})"
			)
			continue
		dep_graph.append(DepGraphEntry(
			package_id=dep_pkg_id,
			version=dep.version,
			artifact_sha256=dep.sha256 if dep.sha256.startswith("sha256:") else f"sha256:{dep.sha256}",
			source_content_id=dep_sci,
			author_kid=dep_author_kid,
			cert_kid=dep_cert_kid,
			# Honor the lock's classification first; fall back to
			# the manifest's direct-dep set for entries that
			# pre-date the dep_type field (older locks).
			dep_kind=dep_type if dep_type in ("direct", "transitive") else (
				"direct" if dep_pkg_id in direct_dep_ids else "transitive"
			),
		))
	if co_artifact_unbuilt:
		raise DeployError(
			f"cert claim for {package_id!r}@{package_version!r} "
			f"cannot be signed: the following co-artifact deps are "
			f"missing their just-emitted v1 sidecars in the staged "
			f"package root (expected at "
			f"`{staged_pkg_root}/<pkg>/<version>/`):\n  - "
			+ "\n  - ".join(co_artifact_unbuilt)
			+ f"\nThis indicates the sibling artifact failed to "
			f"build or sign, or the topological deploy order is "
			f"broken.  A cert claim that omits an actual sibling "
			f"dep cannot pass the consumer-side closure cover check "
			f"(O3), so we refuse to sign rather than ship a "
			f"verifiable-only-by-luck bundle."
		)
	if missing_sci:
		raise DeployError(
			f"cert claim for {package_id!r}@{package_version!r} "
			f"cannot be signed: the following resolved deps lack a "
			f"source_content_id stamp:\n  - "
			+ "\n  - ".join(missing_sci)
			+ f"\nThe certifier must not sign a guessed or sentinel "
			f"SCI for a dependency (O3).  Re-run `drift prepare` "
			f"against republished v1 deps so every resolved dep "
			f"contributes a real identity to the dep_graph."
		)
	if missing_kids:
		raise DeployError(
			f"cert claim for {package_id!r}@{package_version!r} "
			f"cannot be signed: the following resolved deps lack a "
			f"known author kid and/or cert kid in the lock:\n  - "
			+ "\n  - ".join(missing_kids)
			+ f"\nThe certifier must record the exact upstream "
			f"identities it is attesting (O3); silent null kids "
			f"would defeat the dep_graph cover check at consumer "
			f"verify time.  Re-run `drift prepare` against deps "
			f"whose v1 author + cert sidecars resolve cleanly."
		)

	# `cert_suite.result_evidence_sha256` is the digest of the *suite*'s
	# own evidence artifact (test logs, coverage report, vendor cert
	# PDF, ...).  It is separate from `body.evidence_sha256`, which
	# binds the run-level `.provenance.zst` bundle (§3.6 of trust-v1).
	# Fail closed: a signed cert claim must NOT carry a synthetic
	# constant in either evidence field.  Callers (or the operator
	# environment) must supply
	# `DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256`; otherwise the cert
	# claim would attest a suite ran with evidence that doesn't
	# exist.
	#
	# The empty-bytes sentinel `sha256(b"")` is permitted, but ONLY
	# when the operator explicitly opts in via
	# `DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1`.  Naked-env supply of
	# the sentinel is rejected so an operator who genuinely forgot to
	# wire suite evidence can't silently ship "no evidence" by
	# typing the zero hash.  When the opt-in is active, the deploy
	# emits a clearly-labeled WARNING line to stderr so the choice
	# is visible in the build log -- the policy is "suite chose no
	# suite evidence," not "default no evidence."  `body.evidence_
	# sha256` (the provenance-bundle digest) is unaffected and still
	# fail-closed -- the sentinel here is suite-specific.
	_EMPTY_SHA = "sha256:" + _hl.sha256(b"").hexdigest()
	suite_evidence_env = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	if not suite_evidence_env:
		raise DeployError(
			f"cert claim emission for '{package_id}@{package_version}': "
			f"DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256 is required.  This "
			f"is the sha256:<hex> digest of the cert suite's own "
			f"evidence artifact (test logs / coverage report / vendor "
			f"cert PDF / ...).  v1 cert claims do not accept a "
			f"synthetic default in a signed body.  Either set the env "
			f"var to the real digest of the evidence this suite "
			f"produced, OR -- when the suite legitimately produces no "
			f"artifact -- set `DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1` "
			f"AND `DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256={_EMPTY_SHA}` "
			f"together (the explicit opt-in)."
		)
	if suite_evidence_env == _EMPTY_SHA:
		# The empty-bytes sentinel is permitted, but only with the
		# explicit opt-in.  An operator who set the env var to the
		# zero hash without the opt-in is treated as a misconfiguration
		# rather than a legitimate "no evidence" assertion.
		if os.environ.get("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE") != "1":
			raise DeployError(
				f"cert claim emission for '{package_id}@{package_version}': "
				f"DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256 was set to the "
				f"empty-bytes sentinel ({_EMPTY_SHA}), but the explicit "
				f"opt-in `DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1` is not "
				f"set.  v1 treats this as a misconfiguration: the "
				f"signed cert claim would carry a no-evidence sentinel "
				f"without the operator visibly asserting that the "
				f"suite genuinely produces no evidence.  Either supply "
				f"the real evidence digest, or set "
				f"DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1 to opt into the "
				f"sentinel."
			)
		print(
			f"warning: cert suite '{os.environ.get('DRIFT_DEPLOY_CERT_SUITE_ID', 'drift-deploy/v1')}' "
			f"is being signed with the empty-evidence sentinel "
			f"({_EMPTY_SHA}).  The cert claim will record 'suite "
			f"chose no suite evidence' -- inspectors will see the "
			f"zero hash in `cert_suite.result_evidence_sha256`.  "
			f"Document this choice in the release runbook.",
			file=sys.stderr,
		)
	cert_suite = CertSuite(
		id=os.environ.get("DRIFT_DEPLOY_CERT_SUITE_ID", "drift-deploy/v1"),
		version=os.environ.get("DRIFT_DEPLOY_CERT_SUITE_VERSION", "1.0"),
		result=os.environ.get("DRIFT_DEPLOY_CERT_SUITE_RESULT", "pass"),
		result_evidence_sha256=suite_evidence_env,
	)

	# `evidence_sha256` cryptographically binds the on-disk provenance
	# bundle bytes (`.provenance.zst` as written, i.e. the compressed
	# bytes the consumer receives) into the cert claim's signed body.
	# The bundle stays UNSIGNED on disk; its bytes are pinned through
	# the cert claim's signature.  Without this binding a hostile
	# mirror could substitute the provenance bundle while leaving the
	# cert claim intact, and an inspector would read attacker-chosen
	# evidence under a trusted certifier's name (see Scenario E /
	# attack-coverage matrix in trust-v1.md).
	#
	# Fail closed: if the deploy did not produce a provenance bundle,
	# we refuse to emit the cert claim.  The cert suite asserts the
	# certifier ran a suite WITH evidence; pinning an empty / sentinel
	# digest would let the cert claim attest evidence that doesn't
	# actually exist on disk.
	if provenance_path is None or not provenance_path.is_file():
		raise DeployError(
			f"cert claim emission for '{package_id}@{package_version}': "
			f"required provenance bundle is missing "
			f"(expected at {provenance_path!r}).  v1 cert claims bind "
			f"`evidence_sha256` to the on-disk `.provenance.zst` bytes; "
			f"there is no acceptable empty-evidence sentinel.  Re-run "
			f"the deploy with the provenance build step intact."
		)
	evidence_sha = "sha256:" + _hl.sha256(provenance_path.read_bytes()).hexdigest()

	body = CertClaimBody(
		schema_version=1,
		package_id=package_id,
		version=package_version,
		artifact_sha256=artifact_sha256,
		source_content_id=source_content_id,
		target=target,
		toolchain=Toolchain(
			driftc_version=compiler_info.version,
			drift_rt_abi=int(compiler_info.abi),
			driftc_commit=compiler_info.commit,
		),
		dep_graph=tuple(dep_graph),
		cert_suite=cert_suite,
		run_id=os.environ.get("DRIFT_DEPLOY_RUN_ID", str(_uuid.uuid4())),
		run_started_utc=os.environ.get(
			"DRIFT_DEPLOY_RUN_STARTED_UTC",
			"1970-01-01T00:00:00Z",
		),
		evidence_sha256=evidence_sha,
	)
	return sign_and_write_cert_claim(SignCertClaimOptions(
		body=body,
		seed32=seed,
		sidecar_dir=artifact_path.parent,
	))


# ── Assets ───────────────────────────────────────────────────────────


def _stage_assets(
	art: Artifact,
	*,
	manifest_dir: Path,
	staged_install: Path,
) -> None:
	"""Copy declared assets into staged install directory.

	Asset paths in the manifest are project-root-relative, not
	manifest_dir-relative.  Under the canonical layout the manifest lives
	at `<project_root>/drift/manifest.json`, so assets resolve against
	`<project_root>`, not `<project_root>/drift/`.
	"""
	if not art.assets:
		return

	from tools.drift_deploy.build_cmd import project_root_for
	project_root = project_root_for(manifest_dir)

	assets_dir = staged_install / "assets"
	assets_dir.mkdir(parents=True, exist_ok=True)

	for asset_path_str in art.assets:
		src = project_root / asset_path_str
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
	resolved: dict[str, ResolvedDep] | None = None,
	native_lib_paths: list[Path] | None = None,
) -> None:
	"""Built-in baseline smoke for package artifacts."""
	# Generate a trivial consumer that imports the staged package.
	smoke_dir = staged_install.parent / f"_smoke_{art.name}"
	smoke_dir.mkdir(parents=True, exist_ok=True)

	consumer_src = smoke_dir / "smoke_consumer.drift"
	# Generate a valid minimal Drift program that imports the staged package.
	# Uses module_namespace (not package name) — hyphens are not valid
	# Drift identifiers (net-tls → net_tls).
	consumer_src.write_text(
		f'module main;\n'
		f'\n'
		f'import {art.module_namespace};\n'
		f'\n'
		f'fn main() nothrow -> Int {{\n'
		f'\treturn 0;\n'
		f'}}\n',
		encoding="utf-8",
	)

	consumer_bin = smoke_dir / "smoke_consumer"
	cmd = [
		str(driftc),
		"-o", str(consumer_bin),
		"--package-root", str(staged_pkg_root),
		f"--dep", f"{art.name}@{art.version}",
	]
	# Pin resolved dependency versions — smoke must use the same exact
	# version selection as build.  Without these pins, the compiler may
	# see multiple versions of a transitive dependency in the smoke
	# package root and fail with an ambiguity error.
	for dep_id, dep in sorted((resolved or {}).items()):
		cmd.extend(["--dep", f"{dep_id}@{dep.version}"])
	if staged_trust:
		cmd.extend(["--trust-store", str(staged_trust)])
	# Native library search paths for smoke link step.
	for nlp in (native_lib_paths or []):
		cmd.extend(["--link-search", str(nlp)])
	cmd.append(str(consumer_src))

	has_native = bool(art.native_deps)

	clean = _clean_env()

	if has_native:
		# Compile + link + run.
		result = subprocess.run(cmd, capture_output=True, text=True, env=clean)
		if result.returncode != 0:
			raise DeployError(
				f"baseline smoke failed for '{art.name}' (compile+link):\n"
				f"{result.stderr.strip()}"
			)
		# Run.
		run_result = subprocess.run(
			[str(consumer_bin)], capture_output=True, text=True,
			timeout=30, env=clean,
		)
		if run_result.returncode != 0:
			raise DeployError(
				f"baseline smoke failed for '{art.name}' (run):\n"
				f"{run_result.stderr.strip()}"
			)
	else:
		# Compile only (--test-build-only if available, else just compile).
		cmd.append("--test-build-only")
		result = subprocess.run(cmd, capture_output=True, text=True, env=clean)
		if result.returncode != 0:
			# Retry without --test-build-only in case it's not supported.
			cmd.remove("--test-build-only")
			result = subprocess.run(cmd, capture_output=True, text=True, env=clean)
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
	"""
	Built-in baseline smoke for app artifacts.

	MVP contract: verify the binary exists and is runnable (not crashed).
	The app was already compiled and linked during the build step, so the
	baseline smoke checks that the produced binary can execute without
	crashing (signal death). Exit codes 0 and non-zero are both accepted
	since the binary may not support --help.

	This is intentionally weaker than "compile + link + run" — compile
	and link already happened in _build_app. The smoke confirms the
	artifact is a valid executable.
	"""
	if not staged_bin.exists():
		raise DeployError(f"baseline smoke: staged binary not found: {staged_bin}")

	result = subprocess.run(
		[str(staged_bin), "--help"],
		capture_output=True, text=True, timeout=30, env=_clean_env(),
	)
	# Accept any exit code >= 0. Signal death (returncode < 0) = crash.
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
	compiler_info: CompilerInfo,
	staged_pkg_root: Path,
	native_lib_paths: list[Path] | None = None,
	dep_namespace_map: dict[str, str] | None = None,
	author_profile_path: Path | None = None,
) -> None:
	"""Full pipeline for one artifact: build → sign → assets → smoke → publish."""
	staged_install = stage_dir / art.name / art.version

	# v1 has no build-time trust overlay step.  Co-artifact verification
	# during the build flows through the same path as production
	# consumer verification: the project's trust store + author/cert
	# sidecars discovered next to each .zdmp.  When a deploy invocation
	# builds multiple artifacts in a single run, each one's sidecars
	# land in `staged_pkg_root/<name>/<version>/` and the NEXT
	# artifact's build picks them up from that location automatically.
	build_trust_path: Path | None = baseline_trust

	# ── Step 1: Build ──
	# Build a per-artifact package root containing ONLY resolved
	# dependencies. Two filters:
	#  1. Exclude the artifact being built (self-consumption prevention).
	#  2. Exclude unrelated packages (trust-failure prevention: the
	#     compiler verifies ALL packages under --package-root, not just
	#     consumed ones — unrelated signed packages whose namespaces
	#     aren't in the trust store would block the build).
	build_pkg_root = stage_dir / f"_build_pkgroot_{art.name}"
	build_pkg_root.mkdir(parents=True, exist_ok=True)
	resolved_pkg_ids: set[str] = set(resolved.keys())
	for entry in staged_pkg_root.iterdir():
		if entry.name == art.name:
			continue
		if entry.name not in resolved_pkg_ids:
			continue
		link = build_pkg_root / entry.name
		if not link.exists():
			link.symlink_to(entry.resolve() if entry.is_symlink() else entry)

	source_content_id: str | None = None
	if art.kind == "library":
		from tools.drift_deploy.build_cmd import project_root_for
		from lang.driftc.packages.source_content_id import (
			compute_artifact_source_content_id,
		)
		# Phase A is additive: compute source_content_id from on-disk
		# source/asset bytes when they all resolve, otherwise log and
		# proceed without source-mode metadata (skipping the .source-
		# attestation emission below).  Phase C will tighten this:
		# once source-rebuild mode is enforced, missing source files
		# become a hard error instead of a graceful skip.
		try:
			source_content_id = compute_artifact_source_content_id(
				kind=art.kind,
				package_id=art.name,
				version=art.version,
				module_namespace=art.module_namespace,
				entry_module=art.entry_module,
				module_paths=list(art.modules),
				package_deps=[(d.name, d.version) for d in art.package_deps],
				native_deps=[d.lib for d in art.native_deps],
				unsafe=art.unsafe,
				asset_paths=list(art.assets),
				target_class=target,
				source_root=project_root_for(manifest_dir),
			)
		except (FileNotFoundError, ValueError) as e:
			print(
				f"warning: source attestation skipped for '{art.name}': {e}",
				file=sys.stderr,
			)
			source_content_id = None
		dmp_path = _build_package(
			art,
			driftc=driftc,
			target=target,
			resolved=resolved,
			staged_install=staged_install,
			manifest_dir=manifest_dir,
			package_roots=[build_pkg_root],
			native_lib_paths=native_lib_paths,
			trust_store=build_trust_path,
			source_content_id=source_content_id,
		)
	else:
		bin_path = _build_app(
			art,
			driftc=driftc,
			target=target,
			resolved=resolved,
			staged_install=staged_install,
			manifest_dir=manifest_dir,
			package_roots=[build_pkg_root],
			native_lib_paths=native_lib_paths,
			trust_store=build_trust_path,
		)

	# ── Step 2: Stage author profile + v1 trust sidecars (package only) ──
	#
	# `sig_path` is a v1 cert-claim sidecar in this flow (kept as the
	# variable name for back-compat with downstream smoke env wiring
	# that still spells it DRIFT_STAGED_SIG).  No staged_trust_path:
	# v1 has no overlay -- smoke uses the project's baseline trust.
	sig_path: Path | None = None
	staged_profile: Path | None = None

	if art.kind == "library":
		if sign_key is None:
			raise DeployError(
				f"artifact '{art.name}': signing key required for package artifacts; "
				f"pass --sign-key-file or set $DRIFT_SIGN_KEY_FILE"
			)
		# Stage the author profile BEFORE signing so the envelope
		# can include the profile digest in the signed payload.
		if author_profile_path:
			from lang.drift.author_profile import load_author_profile, write_author_profile
			from dataclasses import replace as _dc_replace
			src_profile = load_author_profile(author_profile_path)
			# Bind the profile to this specific package artifact.
			bound_profile = _dc_replace(src_profile, package=art.name)
			staged_profile = staged_install / f"{art.name}.author-profile"
			write_author_profile(bound_profile, staged_profile)

		# Emit provenance bundle BEFORE signing so the envelope
		# can include the provenance digest in the signed payload.
		# The signed digest covers the compressed .zst bytes on disk.
		import hashlib as _hl
		dmp_bytes_for_hash = dmp_path.read_bytes()
		dmp_sha256 = f"sha256:{_hl.sha256(dmp_bytes_for_hash).hexdigest()}"
		resolved_deps_for_provenance: dict[str, dict[str, str]] = {}
		for pkg_id in sorted(resolved.keys()):
			dep = resolved[pkg_id]
			resolved_deps_for_provenance[pkg_id] = {
				"version": dep.version,
				"sha256": dep.sha256,
			}
		from tools.drift_deploy.provenance import detect_source_identity
		source_id = detect_source_identity(manifest_dir)
		provenance_bytes = build_provenance(
			artifact_name=art.name,
			artifact_version=art.version,
			artifact_kind=art.kind,
			artifact_sha256=dmp_sha256,
			target=target,
			compiler=compiler_info,
			resolved_deps=resolved_deps_for_provenance,
			source=source_id,
		)
		provenance_obj = json.loads(provenance_bytes)

		# Collect dependency provenance and public keys for the bundle.
		dep_prov = _collect_dep_provenance(resolved, staged_pkg_root)
		dep_keys = _collect_dep_keys(resolved, staged_pkg_root)

		# Build and compress the provenance bundle.
		bundle_raw = build_provenance_bundle(provenance_obj, dep_prov, dep_keys)
		bundle_compressed = compress_provenance_bundle(bundle_raw)
		provenance_path = staged_install / f"{art.name}.provenance.zst"
		write_provenance_bundle(provenance_path, bundle_compressed)

		# v1 trust flow: discover the pre-signed author claim (the
		# author published it via `drift-author publish` BEFORE running
		# drift_deploy; the orch never holds the author key) and emit a
		# fresh cert claim bound to (artifact_sha256, source_content_id,
		# dep_graph, cert_suite).
		#
		# `source_content_id` is REQUIRED here -- v1 cert claims attest
		# source identity by comparing stamps with the author claim, so
		# a missing SCI means the deploy cannot produce a meaningful
		# cert claim.  Hard error rather than the v0 "skip silently"
		# graceful path.
		if source_content_id is None:
			raise DeployError(
				f"artifact '{art.name}': source_content_id was not "
				f"computed for this build; v1 cert claims require the "
				f"SCI to attest source identity.  Re-run with the "
				f"source-tree available so `compute_artifact_source_content_id` "
				f"can stamp the manifest."
			)
		author_claim_path = _attach_author_claim_to_artifact(
			package_id=art.name,
			package_version=art.version,
			source_content_id=source_content_id,
			manifest_dir=manifest_dir,
			staged_install=staged_install,
		)
		cert_claim_path = _emit_cert_claim_for_artifact(
			dmp_path,
			cert_key=sign_key,
			package_id=art.name,
			package_version=art.version,
			target=target,
			compiler_info=compiler_info,
			source_content_id=source_content_id,
			artifact_sha256=dmp_sha256,
			resolved_deps=resolved,
			# direct deps come from the artifact's own manifest;
			# anything in `resolved` not in this set is a transitive
			# pull added by the resolver.
			direct_dep_ids={d.name for d in art.package_deps},
			# `staged_pkg_root` is needed so co-artifact deps can
			# read their identities back from sibling sidecars
			# (the lock leaves co-artifact fields empty by
			# construction); topo-sort guarantees siblings are
			# already signed by the time we reach this artifact.
			staged_pkg_root=staged_pkg_root,
			provenance_path=provenance_path,
		)
		# Back-compat alias for code further down that still reads
		# `sig_path` to wire authenticated siblings into smoke / dest.
		# In v1 this points at the cert claim; the author claim
		# travels under `author_claim_path`.
		sig_path = cert_claim_path
		attestation_path: Path | None = None  # source-attestation is gone in v1

		# Compress .dmp → .zdmp for distribution.  The cert claim binds
		# the decompressed bytes' sha256, which equals the .dmp bytes
		# we already hashed above (sha256 is over the canonical
		# uncompressed payload, same as v0).
		from lang.driftc.packages.zdmp import compress_to_zdmp
		raw_bytes = dmp_path.read_bytes()
		zdmp_bytes = compress_to_zdmp(raw_bytes)
		zdmp_path = dmp_path.with_suffix(".zdmp")
		zdmp_path.write_bytes(zdmp_bytes)
		# Remove raw .dmp from staged install — only .zdmp is published.
		dmp_path.unlink()

		# Set up staged package root layout for smoke.
		# Layout: staged_pkg_root/<name>/<version>/<name>.zdmp (+siblings)
		#
		# If staged_pkg_root/<name> is a symlink (pointing to the old dest
		# from the pre-loop mirror), replace it with a fresh directory
		# containing ONLY the version being built. Old self versions from
		# dest are intentionally excluded — they are not dependencies and
		# may have incompatible artifact layouts that poison the smoke root.
		art_pkg_dir = staged_pkg_root / art.name
		if art_pkg_dir.is_symlink():
			# Remove symlink to dest — replace with a fresh directory
			# containing ONLY the version being built. Old self versions
			# from dest must not be visible in the smoke root; they are
			# not dependencies and can have incompatible artifact layouts.
			art_pkg_dir.unlink()
			art_pkg_dir.mkdir(parents=True, exist_ok=True)
		smoke_pkg_dir = art_pkg_dir / art.version
		# Remove any stale entry (symlink or empty dir) for this version.
		if smoke_pkg_dir.is_symlink() or smoke_pkg_dir.exists():
			if smoke_pkg_dir.is_symlink():
				smoke_pkg_dir.unlink()
			elif smoke_pkg_dir.is_dir():
				shutil.rmtree(str(smoke_pkg_dir))
		smoke_pkg_dir.mkdir(parents=True, exist_ok=True)
		shutil.copy2(str(zdmp_path), str(smoke_pkg_dir / zdmp_path.name))
		# v1 sidecars travel with the artifact: cert claim + author claim
		# must both reach the smoke pkg root so the smoke verifier can
		# clear the trust gate the same way a real consumer will.
		if sig_path:
			shutil.copy2(str(sig_path), str(smoke_pkg_dir / sig_path.name))
		if 'author_claim_path' in locals() and author_claim_path is not None:
			shutil.copy2(str(author_claim_path), str(smoke_pkg_dir / author_claim_path.name))
		# Provenance + author-profile remain as informational artifacts
		# (the v1 verifier reads neither; they're carried through for
		# downstream tooling such as `drift inspect` / provenance audit).
		if provenance_path and provenance_path.exists():
			shutil.copy2(str(provenance_path), str(smoke_pkg_dir / provenance_path.name))
		if staged_profile and staged_profile.exists():
			shutil.copy2(str(staged_profile), str(smoke_pkg_dir / staged_profile.name))

		# v1 has no "staged trust overlay" step.  The smoke compile
		# uses the project's normal trust store (passed in via
		# `--trust-store`), which the user must populate with both
		# author and cert role kids for the namespaces being built.
		# Without that, the smoke verify fails -- exactly what would
		# happen at consumer-load time, so the smoke is honest.

	# ── Step 3: Assets ──
	_stage_assets(art, manifest_dir=manifest_dir, staged_install=staged_install)
	# Author profile was staged in step 2 (before signing) for envelope binding.

	# ── Step 4: Provenance + sign (app) ──
	# App provenance records the build environment and dependency graph.
	# When a signing key is available, the app binary and provenance
	# bundle are authenticated with the same v2 envelope as packages.
	if art.kind == "app":
		import hashlib as _hl
		app_bin_path = staged_install / art.name
		app_bytes_for_hash = app_bin_path.read_bytes()
		app_sha256 = f"sha256:{_hl.sha256(app_bytes_for_hash).hexdigest()}"
		resolved_deps_for_provenance: dict[str, dict[str, str]] = {}
		for pkg_id in sorted(resolved.keys()):
			dep = resolved[pkg_id]
			resolved_deps_for_provenance[pkg_id] = {
				"version": dep.version,
				"sha256": dep.sha256,
			}
		from tools.drift_deploy.provenance import detect_source_identity
		source_id = detect_source_identity(manifest_dir)
		provenance_bytes = build_provenance(
			artifact_name=art.name,
			artifact_version=art.version,
			artifact_kind=art.kind,
			artifact_sha256=app_sha256,
			target=target,
			compiler=compiler_info,
			resolved_deps=resolved_deps_for_provenance,
			source=source_id,
		)
		provenance_obj = json.loads(provenance_bytes)

		# Collect dependency provenance and public keys for the bundle.
		dep_prov = _collect_dep_provenance(resolved, staged_pkg_root)
		dep_keys = _collect_dep_keys(resolved, staged_pkg_root)

		# Build and compress the provenance bundle.
		bundle_raw = build_provenance_bundle(provenance_obj, dep_prov, dep_keys)
		bundle_compressed = compress_provenance_bundle(bundle_raw)
		provenance_path = staged_install / f"{art.name}.provenance.zst"
		write_provenance_bundle(provenance_path, bundle_compressed)

		# v1: app artifacts do not currently produce a cert claim --
		# app verification flows through binary signing in a separate
		# subsystem (`drift sign` for distributable binaries).  The
		# v1 cutover affects PACKAGE artifacts; app signing remains a
		# follow-up.  Provenance bundle still emits unsigned so app
		# `drift inspect` remains useful.
		pass

	# ── Step 5: Smoke ──
	# Build a filtered smoke package root containing only the artifact
	# itself and its resolved deps. The compiler eagerly verifies all
	# packages under --package-root, so unrelated signed packages with
	# untrusted namespaces would block smoke compilation. This root is
	# used for both baseline and custom smoke (via DRIFT_STAGED_PKG_ROOT).
	smoke_pkg_root = stage_dir / f"_smoke_pkgroot_{art.name}"
	smoke_pkg_root.mkdir(parents=True, exist_ok=True)
	if art.kind == "library":
		art_in_staged = staged_pkg_root / art.name
		if art_in_staged.exists():
			smoke_art_link = smoke_pkg_root / art.name
			if not smoke_art_link.exists():
				smoke_art_link.symlink_to(
					art_in_staged.resolve() if art_in_staged.is_symlink() else art_in_staged
				)
	for dep_pkg_id in (resolved or {}):
		dep_in_staged = staged_pkg_root / dep_pkg_id
		if dep_in_staged.exists():
			dep_link = smoke_pkg_root / dep_pkg_id
			if not dep_link.exists():
				dep_link.symlink_to(
					dep_in_staged.resolve() if dep_in_staged.is_symlink() else dep_in_staged
				)

	if skip_smoke:
		print(f"  warning: --skip-smoke: smoke skipped for '{art.name}'", file=sys.stderr)
	else:
		if art.kind == "library":
			# v1: smoke uses the project's normal trust store
			# (`baseline_trust`).  No overlay -- the cert + author
			# sidecars travel with the artifact and are discovered
			# next to each .zdmp.
			_run_baseline_smoke_package(
				art,
				driftc=driftc,
				staged_install=staged_install,
				staged_pkg_root=smoke_pkg_root,
				staged_trust=baseline_trust,
				resolved=resolved,
				native_lib_paths=native_lib_paths,
			)
		else:
			_run_baseline_smoke_app(art, staged_bin=staged_install / art.name)

		# Smoke env for custom command.
		smoke_env = dict(os.environ)
		smoke_env.update({
			"DRIFT_STAGE_DIR": str(stage_dir),
			"DRIFT_STAGED_PKG_ROOT": str(smoke_pkg_root),
			"DRIFT_STAGED_INSTALL": str(staged_install),
			"DRIFT_STAGED_DRIFTC": str(driftc),
			"DRIFT_ARTIFACT_NAME": art.name,
			"DRIFT_ARTIFACT_VERSION": art.version,
			"DRIFT_ARTIFACT_KIND": art.kind,
		})
		if art.kind == "library":
			smoke_env["DRIFT_STAGED_PKG"] = str(zdmp_path)
			# `DRIFT_STAGED_SIG` is preserved for backward-compat
			# with smoke scripts that look for an artifact-signature
			# sidecar.  In v1 it points at the cert claim.
			if sig_path:
				smoke_env["DRIFT_STAGED_SIG"] = str(sig_path)
		else:
			smoke_env["DRIFT_STAGED_BIN"] = str(staged_install / art.name)
			if sig_path:
				smoke_env["DRIFT_STAGED_SIG"] = str(sig_path)

		_run_custom_smoke(art, env=smoke_env)

	# ── Step 6: Publish ──
	if dry_run:
		print(f"  dry-run: skipping publish for '{art.name}'")
		return

	if art.kind == "library":
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
	except CertModeError as e:
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
	has_packages = any(a.kind == "library" for a in artifacts)
	has_apps = any(a.kind == "app" for a in artifacts)

	if has_packages and not args.dest:
		raise DeployError("--dest required when manifest contains package artifacts")
	if has_apps and not args.app_dest:
		raise DeployError("--app-dest required when manifest contains app artifacts")

	# Publisher identity required for all deployable projects.
	# project.author_profile is optional in the manifest schema (other tools
	# may load manifests without needing it), but drift deploy enforces it:
	# every published project must have an explicit author identity.
	if not manifest.project.author_profile:
		raise DeployError(
			"publisher identity missing: set 'project.author_profile' in "
			"drift/manifest.json (run 'drift init' to create an author profile)"
		)
	author_profile_path = manifest_dir / manifest.project.author_profile
	if not author_profile_path.exists():
		raise DeployError(
			f"project.author_profile declared as '{manifest.project.author_profile}' "
			f"but file not found: {author_profile_path}"
		)

	# Resolve toolchain.
	driftc = _resolve_driftc(args)
	target = _resolve_target(args)
	sign_key = _resolve_sign_key(args)
	baseline_trust = _resolve_trust_store(args)
	compiler_info = _get_compiler_info(driftc)

	# Package roots: default to --dest.
	package_roots = args.package_root or ([args.dest] if args.dest else [])

	# Native library search paths (env + config + CLI).
	native_lib_paths = _resolve_native_lib_paths(args, manifest_dir)

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

	# Build mapping from package name → module_namespace for co-deployed deps.
	dep_namespace_map: dict[str, str] = {
		a.name: a.module_namespace for a in artifacts if a.kind == "library"
	}

	# ── Resolution / lock (per-artifact, read-only) ──
	# Deploy is read-only w.r.t. drift/lock.json. If deps need resolution,
	# the lock must already exist (written by 'drift prepare').
	lock_path = args.manifest.resolve().parent / "lock.json"
	existing_lock: dict[str, dict[str, ResolvedDep]] | None = None
	need_resolution = any(a.package_deps for a in artifacts)
	if need_resolution:
		if not lock_path.exists():
			raise DeployError(
				"drift/lock.json not found but artifacts have package_deps; "
				"run 'drift prepare' first"
			)
		try:
			existing_lock = read_lock(lock_path)
		except ValueError as e:
			raise DeployError(f"failed to read {lock_path}: {e}")

	# Library artifacts in this manifest are the ONLY packages whose
	# lock entries may legitimately carry `dep_type: "co-artifact"`
	# (bypassing the sha/signer re-check because they are built in
	# this same deploy run).  Anything else claiming co-artifact
	# status in the lock is rejected at verify time.
	co_artifact_names = {a.name for a in artifacts if a.kind == "library"}

	# Source-rebuild lane selector + orch run snapshot.  Deploy
	# enters source-rebuild mode under `DRIFT_CERT_MODE=stage`,
	# `DRIFT_CERT_MODE=certify`, or explicit `--source-rebuild`.
	# Unset leaves deploy in strict-lock producer mode (local
	# publishing).  Under source-rebuild the snapshot is REQUIRED.
	#
	# The stage vs certify split is expressed as `snapshot_exempt_ids`:
	# under stage, the manifest's library-artifact names are exempt
	# from the snapshot gate (they are PRODUCER OUTPUTS of this
	# deploy invocation, not consumed deps — and orch hasn't
	# refreshed the snapshot mid-deploy to include them).  Under
	# certify or manual `--source-rebuild`, no exemption: every
	# package the index discovers must be in the snapshot.
	_src_rebuild = _source_rebuild_enabled(args)
	_exempt_ids: set[str] | None = (
		set(co_artifact_names) if _src_rebuild and _producer_output_exemption_active() else None
	)
	_run_snap = None
	if _src_rebuild:
		from tools.drift_deploy.run_snapshot import load_run_snapshot
		_snap_path = getattr(args, "run_snapshot", None)
		if _snap_path is None:
			_env_path = os.environ.get("DRIFT_RUN_SNAPSHOT", "")
			if _env_path:
				_snap_path = Path(_env_path)
		if _snap_path is None:
			raise DeployError(
				"source-rebuild mode requires a run snapshot.  Pass "
				"`--run-snapshot <path>` or set `DRIFT_RUN_SNAPSHOT="
				"<path>`.  The snapshot pins source identity per "
				"certification run; downstream source-rebuild "
				"consumers cannot verify without it."
			)
		try:
			_run_snap = load_run_snapshot(Path(_snap_path))
		except (ValueError, OSError) as e:
			raise DeployError(f"run snapshot load failed: {e}")

	resolved_map: dict[str, dict[str, ResolvedDep]] = {}

	try:
		for art in artifacts:
			print(f"\n{'='*60}")
			print(f"artifact: {art.name} ({art.kind}) v{art.version}")
			print(f"{'='*60}")

			# Resolve this artifact's deps now — staged_pkg_root contains
			# .dmp files from earlier topo-sorted artifacts.  Under
			# source-rebuild, orch's run snapshot (loaded above) pins
			# source identity for every upstream dep.
			resolved = _resolve_artifact_deps(
				art,
				package_roots=[staged_pkg_root] + package_roots,
				lock_path=lock_path,
				existing_lock=existing_lock,
				co_artifact_names=co_artifact_names,
				source_rebuild=_src_rebuild,
				run_snapshot=_run_snap,
				snapshot_exempt_ids=_exempt_ids,
			)
			resolved_map[art.name] = resolved
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
				compiler_info=compiler_info,
				staged_pkg_root=staged_pkg_root,
				native_lib_paths=native_lib_paths,
				dep_namespace_map=dep_namespace_map,
				author_profile_path=author_profile_path,
			)

	finally:
		# Clean up staging directory.
		shutil.rmtree(str(stage_dir), ignore_errors=True)

	print(f"\ndrift deploy: done ({len(artifacts)} artifact(s))")
	return 0


if __name__ == "__main__":
	sys.exit(run())
