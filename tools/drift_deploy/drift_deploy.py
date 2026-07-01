# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift deploy — standardized package deploy tool.

Entry point for building, signing, smoking, and publishing Drift
package and app artifacts from a drift/manifest.json manifest.
"""

from __future__ import annotations

import argparse
import hashlib as _hl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lang.driftc import _events as _events
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
		help="Package root for resolving package_deps (repeatable; default: --dest)")
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
	p.add_argument("--timing", action="store_true",
		help=(
			"Collect per-phase compile timings from each underlying "
			"driftc invocation and print `[drift:timing][<artifact>]` "
			"summary lines to stderr after each artifact's build.  "
			"Forwards `--timing` to the driftc subprocess; intended for "
			"the toolchain-perf data-gathering release "
			"(see `doc/timing.md`).  No effect on deploy output."
		))
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

	# ── Cert-suite evidence (signed certifier metadata) ─────────────
	#
	# These three flags carry the cert-suite identity + evidence
	# digest that get signed into `cert_claim.body.cert_suite`.  They
	# are first-class CLI flags (not just env vars) because the values
	# are signed certifier metadata and belong in the deploy command /
	# release config, not in ambient shell state.  Env vars
	# (`DRIFT_DEPLOY_CERT_SUITE_*`) remain as a compatibility fallback
	# for orch hosts that still drive deploy from env; CLI flags win
	# when both are set.
	#
	# `--cert-suite-evidence-sha256` and `--cert-suite-no-evidence`
	# are mutually exclusive.  Missing both (and no env-var fallback)
	# is a hard error -- a signed cert claim must NOT carry a synthetic
	# evidence digest by default.
	p.add_argument("--cert-suite-id", type=str, default=None,
		help=(
			"Cert-suite identity recorded in cert_claim.body.cert_suite.id.  "
			"Defaults to `drift-deploy/v1` if neither this flag nor "
			"$DRIFT_DEPLOY_CERT_SUITE_ID is set."
		))
	p.add_argument("--cert-suite-version", type=str, default=None,
		help=(
			"Cert-suite version recorded in cert_claim.body.cert_suite.version.  "
			"Defaults to `1.0` if neither this flag nor "
			"$DRIFT_DEPLOY_CERT_SUITE_VERSION is set."
		))
	p.add_argument("--cert-suite-result", type=str, default=None,
		choices=("pass", "fail"),
		help=(
			"Cert-suite result (`pass` | `fail`) recorded in "
			"cert_claim.body.cert_suite.result.  Defaults to `pass` if "
			"neither this flag nor $DRIFT_DEPLOY_CERT_SUITE_RESULT is set.  "
			"A `fail` claim is well-formed but rejected by default at "
			"consumer verify time."
		))
	suite_evid = p.add_mutually_exclusive_group()
	suite_evid.add_argument("--cert-suite-evidence-sha256", type=str, default=None,
		dest="cert_suite_evidence_sha256",
		help=(
			"sha256:<hex> digest of the cert suite's own evidence artifact "
			"(test logs / coverage report / vendor cert PDF / ...).  Mutually "
			"exclusive with --cert-suite-no-evidence.  Wins over "
			"$DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256."
		))
	suite_evid.add_argument("--cert-suite-no-evidence", action="store_true",
		help=(
			"Opt-in: the cert suite legitimately produces no evidence "
			"artifact (records sha256(\"\") as result_evidence_sha256 and "
			"emits a stderr warning so the choice is visible in the build "
			"log).  Mutually exclusive with --cert-suite-evidence-sha256.  "
			"Use this only when the suite genuinely has no artifact; do not "
			"use to silence the missing-evidence error."
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
	"""Resolve the project trust store path, exists-before-injecting.

	Contract (matches the deploy wrapper / pex_entry rule so the
	whole toolchain behaves the same way):

	  - Explicit `--trust-store`: caller asserted intent.  The path
	    MUST exist; missing -> `DeployError` with a clear diagnostic.
	    Silently forwarding a path that the driftc subprocess will
	    reject is exactly the symptom the cert team flagged on the
	    net-tls staging host.

	  - `$DRIFT_TRUST_STORE` env var: env is an explicit intent too
	    (the operator chose to set it).  Same rule: file must exist
	    or we fail loud with the path that was set.

	  - Neither: return `None`.  Do NOT default to
	    `~/.config/drift/trust.json` or any other ambient location
	    -- driftc itself reads the user-trust layer at compile time
	    (gated on `Path.exists()` in `lang/driftc/driftc.py`).
	    Conflating the user layer into the `--trust-store` flag we
	    forward would force a non-existent path into the subprocess
	    cmd line on a clean host, which is exactly what we're fixing.
	"""
	if args.trust_store:
		path = Path(args.trust_store).expanduser()
		if not path.exists():
			raise DeployError(
				f"--trust-store path does not exist: {path}.  "
				f"Pass a path to an existing trust store JSON, or omit "
				f"the flag to let driftc fall through to its default "
				f"user-trust layer (~/.config/drift/trust.json, picked "
				f"up automatically when it exists)."
			)
		return path
	env_raw = os.environ.get("DRIFT_TRUST_STORE")
	if env_raw:
		path = Path(env_raw).expanduser()
		if not path.exists():
			raise DeployError(
				f"$DRIFT_TRUST_STORE points at a path that does not "
				f"exist: {path}.  The env var is treated as explicit "
				f"intent -- unset it or repair the path.  (We do not "
				f"silently fall through to the default; that masked the "
				f"cert-host net-tls failure.)"
			)
		return path
	return None


# Empty-bytes SHA — published as the "no evidence" sentinel for the
# cert-suite evidence digest.  Hoisted so both the CLI/env resolver
# and the cert-claim emitter reference the same canonical value.
_EMPTY_EVIDENCE_SHA = "sha256:" + _hl.sha256(b"").hexdigest()


@dataclass(frozen=True)
class CertSuiteOptions:
	"""Resolved cert-suite metadata that gets signed into
	`cert_claim.body.cert_suite`.

	`result_evidence_sha256` carries the digest of the cert suite's
	OWN evidence artifact (test logs, coverage report, vendor cert
	PDF, ...).  This is **separate** from `body.evidence_sha256`,
	which binds the run-level `.provenance.zst` bundle (trust-v1.md
	§3.6) -- one digest per concern.

	`no_evidence_sentinel` is True iff the operator opted into the
	empty-bytes sentinel (either via `--cert-suite-no-evidence` or
	the env-fallback `DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1` pair).
	When set, the emitter prints a visible stderr warning so the
	choice is auditable in the build log.
	"""
	id: str
	version: str
	result: str
	result_evidence_sha256: str
	no_evidence_sentinel: bool


def _resolve_cert_suite_options(args: argparse.Namespace) -> CertSuiteOptions:
	"""Resolve cert-suite identity + evidence from CLI flags, with
	env vars as compatibility fallback.

	Contract (matches the team-stated UX rules):

	  - `--cert-suite-evidence-sha256` and `--cert-suite-no-evidence`
	    are mutually exclusive (argparse-enforced at parse time).
	  - `--cert-suite-no-evidence` records `sha256("")` as
	    `result_evidence_sha256`; the emitter prints a visible
	    warning so the choice is auditable.
	  - Missing both (no CLI flag, no env fallback) is a hard
	    `DeployError` -- a signed cert claim must NOT carry a
	    synthetic evidence digest by default.
	  - CLI flags take precedence over the legacy env vars
	    (`DRIFT_DEPLOY_CERT_SUITE_{ID,VERSION,RESULT,EVIDENCE_SHA256,NO_EVIDENCE}`),
	    which remain as a compatibility fallback for orch hosts
	    that still drive deploy from env.
	"""
	# id / version / result: CLI > env > hardcoded default.
	suite_id = (
		args.cert_suite_id
		or os.environ.get("DRIFT_DEPLOY_CERT_SUITE_ID")
		or "drift-deploy/v1"
	)
	suite_version = (
		args.cert_suite_version
		or os.environ.get("DRIFT_DEPLOY_CERT_SUITE_VERSION")
		or "1.0"
	)
	suite_result = (
		args.cert_suite_result
		or os.environ.get("DRIFT_DEPLOY_CERT_SUITE_RESULT")
		or "pass"
	)
	# Argparse enforces `choices=("pass","fail")` on the CLI surface,
	# but the env-fallback path can still inject an arbitrary string.
	# Validate here so malformed values fail at deploy startup rather
	# than deep inside cert claim body assembly.
	if suite_result not in ("pass", "fail"):
		raise DeployError(
			f"cert suite result {suite_result!r} is not valid; must be "
			f"`pass` or `fail`.  Source was "
			f"{'--cert-suite-result' if args.cert_suite_result else '$DRIFT_DEPLOY_CERT_SUITE_RESULT'}."
		)

	# Local closure that mirrors `lang.driftc.packages.source_content_id.validate_sci`
	# but with cert-suite-specific error context.  The deploy-startup
	# guarantee is "malformed CLI/env values fail before any artifact
	# build" — generic SCI-shape errors deeper in the cert claim body
	# would erode that.
	from lang.driftc.packages.source_content_id import validate_sci as _validate_sci
	def _validate_evidence(value: str, *, source: str) -> str:
		try:
			return _validate_sci(value, field="cert suite evidence digest")
		except ValueError as e:
			raise DeployError(
				f"cert suite evidence digest from {source} is malformed: "
				f"{e}.  Expected shape `sha256:<64-lowercase-hex>` (the "
				f"sha256 of the cert suite's own evidence artifact)."
			) from e

	# Evidence resolution — three CLI inputs, two env-fallback inputs:
	#   1. `--cert-suite-evidence-sha256 <sha>` -> use sha directly.
	#   2. `--cert-suite-no-evidence`            -> use empty-bytes
	#      sentinel + flag warning (no env opt-in needed; the CLI
	#      flag IS the explicit opt-in).
	#   3. Neither CLI flag set -> fall back to the env-driven path:
	#        - `DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256` REQUIRED.
	#        - If env says empty-sha, require
	#          `DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1` (legacy
	#          opt-in pair).
	#        - Else: fail closed.
	if args.cert_suite_evidence_sha256:
		# Explicit real digest via CLI.
		validated = _validate_evidence(
			args.cert_suite_evidence_sha256,
			source="--cert-suite-evidence-sha256",
		)
		return CertSuiteOptions(
			id=suite_id,
			version=suite_version,
			result=suite_result,
			result_evidence_sha256=validated,
			no_evidence_sentinel=False,
		)
	if args.cert_suite_no_evidence:
		# Explicit opt-in via CLI — the flag itself IS the opt-in
		# (no separate `DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1`
		# pairing required; the env-fallback path keeps that
		# legacy shape).  No shape validation needed: the sentinel
		# is a module-level constant we construct ourselves.
		return CertSuiteOptions(
			id=suite_id,
			version=suite_version,
			result=suite_result,
			result_evidence_sha256=_EMPTY_EVIDENCE_SHA,
			no_evidence_sentinel=True,
		)
	# Env-fallback path.
	env_evidence = os.environ.get("DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256")
	if not env_evidence:
		raise DeployError(
			f"cert claim emission requires the cert suite's evidence "
			f"digest.  Pass one of:\n"
			f"  --cert-suite-evidence-sha256 sha256:<hex>   (the real "
			f"digest of the suite's evidence artifact)\n"
			f"  --cert-suite-no-evidence                    (opt-in "
			f"sentinel when the suite legitimately produces no artifact)\n"
			f"or (legacy env-driven shape) set DRIFT_DEPLOY_CERT_SUITE_"
			f"EVIDENCE_SHA256, paired with DRIFT_DEPLOY_CERT_SUITE_NO_"
			f"EVIDENCE=1 if using the empty-bytes sentinel ({_EMPTY_EVIDENCE_SHA}).  "
			f"v1 cert claims do not accept a synthetic default in a "
			f"signed body."
		)
	if env_evidence == _EMPTY_EVIDENCE_SHA:
		if os.environ.get("DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE") != "1":
			raise DeployError(
				f"$DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256 was set to the "
				f"empty-bytes sentinel ({_EMPTY_EVIDENCE_SHA}), but the "
				f"explicit opt-in DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1 "
				f"is not set.  v1 treats this as a misconfiguration: the "
				f"signed cert claim would carry a no-evidence sentinel "
				f"without the operator visibly asserting that the suite "
				f"genuinely produces no evidence.  Either supply the real "
				f"evidence digest, pass --cert-suite-no-evidence, or set "
				f"DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1 to opt into the "
				f"sentinel."
			)
		return CertSuiteOptions(
			id=suite_id,
			version=suite_version,
			result=suite_result,
			result_evidence_sha256=_EMPTY_EVIDENCE_SHA,
			no_evidence_sentinel=True,
		)
	# Env-supplied non-empty digest — validate shape before signing.
	validated_env = _validate_evidence(
		env_evidence,
		source="$DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256",
	)
	return CertSuiteOptions(
		id=suite_id,
		version=suite_version,
		result=suite_result,
		result_evidence_sha256=validated_env,
		no_evidence_sentinel=False,
	)


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

	`co_artifact_names` names the package artifacts declared in the
	current manifest.  Only those IDs may legitimately appear in the
	lock with `dep_type: "co-artifact"` — anything else claiming
	co-artifact status is treated as lock corruption and rejected.

	`snapshot_exempt_ids` is threaded into the run-snapshot-gated
	`build_package_index` call under source-rebuild.  Populated by
	the caller (`_run_impl`) with the manifest's package-artifact
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


def _merge_and_cleanup_child_timings(
	timing_out_path: "Path | None", prefix: str,
) -> None:
	"""Read the child driftc's `--timing-out` file, merge its
	structured timings into the currently-installed wrapper sink
	under `prefix`, then unlink the file.

	Best-effort -- I/O failures (corrupt file, child crashed before
	writing) are silently swallowed; the wrapper summary just won't
	include child phase data for the failed call.  ALWAYS called
	from a `finally` so failed child compiles still contribute their
	phase data to the wrapper summary (users adopting --timing will
	often share failure logs; without this they'd see only the
	wrapper's own `<prefix>` wall time, not the per-phase breakdown).
	"""
	if timing_out_path is None:
		return
	child_summary: "dict | None" = None
	try:
		child_summary = json.loads(
			timing_out_path.read_text(encoding="utf-8")
		)
	except (OSError, json.JSONDecodeError):
		child_summary = None
	finally:
		try:
			timing_out_path.unlink(missing_ok=True)
		except OSError:
			pass
	sink = _events.current_sink()
	if sink is not None and isinstance(child_summary, dict):
		sink.merge_subprocess_timings(prefix, child_summary)
		# Workload counters merge under the same prefix
		# (e.g. `build.compile.*` / `smoke.compile.*`).  Additive: a
		# retried child contributes both its phase time and its
		# workload denominators twice, keeping per-unit elapsed
		# comparable across retries.  Child's `workload_schema` is
		# forwarded explicitly so a mismatch with the parent sink's
		# schema is refused at the sink boundary and surfaced via a
		# `<prefix>.workload_schema_mismatch` marker -- preventing
		# mislabeled counters when the parent and child run different
		# toolchain versions.
		_child_workload = child_summary.get("workload")
		_child_schema = child_summary.get("workload_schema")
		if isinstance(_child_workload, dict):
			sink.merge_subprocess_workload(
				prefix, _child_workload, sub_schema=_child_schema,
			)


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
	timing: bool = False,
	timing_prefix: str = "build.compile",
) -> Path:
	"""Build a package artifact. Returns path to staged .dmp.

	`source_content_id`, if provided, is stamped verbatim into the
	emitted .dmp manifest.  Computed by the caller from stable source
	inputs (see
	`lang/driftc/packages/source_content_id.compute_artifact_source_content_id`)
	so the same value can later be reused when emitting the
	v1 author + cert claim sidecars without re-walking the source
	tree.

	`timing`: pass `--timing-out` to driftc, read the child's
	structured timings JSON, and merge into the currently-installed
	wrapper sink (via `events.current_sink()`) under `timing_prefix`
	(default `build.compile`).  `_run_baseline_smoke_package` passes
	a different prefix (`smoke.compile`) so the wrapper summary can
	distinguish the two.  No stderr re-emission -- the outer wrapper
	is the only thing that prints.
	"""
	out_dmp = staged_install / f"{art.name}.dmp"
	staged_install.mkdir(parents=True, exist_ok=True)

	extra_flags: list[str] = []
	timing_out_path: Path | None = None
	if timing:
		# mkstemp returns (fd, path); close the fd to avoid leaking
		# descriptors across artifacts.  driftc overwrites the file
		# via Path.write_text() once the child compile finishes.
		_fd, _path = tempfile.mkstemp(
			prefix=f"drift-deploy-{art.name}-{timing_prefix.replace('.', '-')}-",
			suffix=".timing.json",
		)
		os.close(_fd)
		timing_out_path = Path(_path)
		extra_flags.extend(["--timing-out", str(timing_out_path)])

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
		extra_flags=extra_flags or None,
	)

	with _events.timed(timing_prefix):
		result = subprocess.run(cmd, capture_output=True, text=True, env=_clean_env())
	try:
		if result.returncode != 0:
			raise DeployError(
				f"build failed for package '{art.name}':\n"
				f"command: {' '.join(cmd)}\n"
				f"stderr: {result.stderr.strip()}"
			)
	finally:
		# Merge BEFORE re-raising so failed compiles still contribute
		# their phase data to the wrapper's summary.
		_merge_and_cleanup_child_timings(timing_out_path, timing_prefix)

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
	timing: bool = False,
	timing_prefix: str = "build.compile",
) -> Path:
	"""Build an app artifact. Returns path to staged binary.

	`timing` / `timing_prefix`: see `_build_package`.  Same
	side-channel-via-`--timing-out` shape; merges child driftc
	timings into the currently-installed wrapper sink.
	"""
	out_bin = staged_install / art.name
	staged_install.mkdir(parents=True, exist_ok=True)

	extra_flags: list[str] = []
	timing_out_path: Path | None = None
	if timing:
		# mkstemp returns (fd, path); close the fd to avoid leaking
		# descriptors across artifacts.  driftc overwrites the file
		# via Path.write_text() once the child compile finishes.
		_fd, _path = tempfile.mkstemp(
			prefix=f"drift-deploy-{art.name}-{timing_prefix.replace('.', '-')}-",
			suffix=".timing.json",
		)
		os.close(_fd)
		timing_out_path = Path(_path)
		extra_flags.extend(["--timing-out", str(timing_out_path)])

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
		extra_flags=extra_flags or None,
	)

	with _events.timed(timing_prefix):
		result = subprocess.run(cmd, capture_output=True, text=True, env=_clean_env())
	try:
		if result.returncode != 0:
			raise DeployError(
				f"build failed for app '{art.name}':\n"
				f"command: {' '.join(cmd)}\n"
				f"stderr: {result.stderr.strip()}"
			)
	finally:
		# Merge BEFORE re-raising so failed compiles still contribute
		# their phase data to the wrapper's summary.
		_merge_and_cleanup_child_timings(timing_out_path, timing_prefix)

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
	publishes the claim from their workstation via `drift author`;
	deploy only locates that file, verifies it matches THIS release,
	and copies it into the staged install directory.

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

	Source location: `<manifest_dir>/<pkg>.author-claim` (the canonical
	name produced by `tools.drift_author.sign_and_write_author_claim`).
	`manifest_dir` is the directory holding the project's
	`manifest.json` -- by convention `<repo>/drift/` -- so the author
	claim sits as a sibling of the manifest, not under an additional
	`drift/` subdir.  An earlier version of this helper added a
	redundant `/drift` segment here, which made the lookup probe
	`<repo>/drift/drift/<pkg>.author-claim` and rejected every
	correctly-placed claim.
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.sidecar_naming import author_claim_filename

	canonical_name = author_claim_filename(package_id)
	src = manifest_dir / canonical_name
	if not src.is_file():
		raise DeployError(
			f"artifact '{package_id}': pre-signed author claim not "
			f"found at {src}.  v1 release flow requires the author "
			f"to publish the claim via `drift author` BEFORE "
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
			f"Re-run `drift author` to regenerate."
		) from e
	body = claim.body
	if body.package_id != package_id:
		raise DeployError(
			f"author claim at {src} binds package_id "
			f"{body.package_id!r}, but this build is for "
			f"{package_id!r}.  Re-run `drift author` for "
			f"{package_id!r} (or remove the stale claim from "
			f"{manifest_dir})."
		)
	if body.version != package_version:
		raise DeployError(
			f"author claim at {src} binds version {body.version!r}, "
			f"but this build is for {package_version!r}.  Stale "
			f"claims from a previous release must NOT be reused: "
			f"the certifier signs (artifact bytes + dep_graph + "
			f"cert_suite) but the AUTHOR's release intent is "
			f"version-specific.  Bump the manifest's artifact version "
			f"to {package_version!r} and re-run `drift author "
			f"--overwrite` and republish."
		)
	if body.source_content_id != source_content_id:
		raise DeployError(
			f"author claim at {src} binds source_content_id "
			f"{body.source_content_id!r}, but this build's source "
			f"hashed to {source_content_id!r}.  The source tree has "
			f"changed since the author signed; re-run `drift author "
			f"--overwrite` with the current source to refresh the claim."
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
	artifact_kind: str,
	target: str,
	compiler_info: "CompilerInfo",
	source_content_id: str,
	artifact_sha256: str,
	resolved_deps: dict[str, "ResolvedDep"],
	direct_dep_ids: set[str],
	staged_pkg_root: Path,
	provenance_path: Path | None,
	cert_suite_options: CertSuiteOptions,
) -> Path:
	"""Sign a v1 cert claim for `artifact_path` and write the sidecar.

	The cert claim binds artifact bytes + toolchain identity + the
	full resolved transitive dep_graph + the cert-suite result (per
	O3 / O4).  Returns the sidecar path.

	`cert_suite_options` is the resolved cert-suite metadata from
	`_resolve_cert_suite_options` -- CLI flags (`--cert-suite-id`,
	`--cert-suite-evidence-sha256`, `--cert-suite-no-evidence`, ...)
	win over the legacy `DRIFT_DEPLOY_CERT_SUITE_*` env vars.  The
	field values here are signed into `cert_claim.body.cert_suite`,
	so we keep the input plumbing explicit at the deploy-command
	level rather than ambient shell state.

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
		CertSuite,
		DepGraphEntry,
		Toolchain,
		make_cert_claim_body,
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
	#
	# Resolution happens upstream in `_resolve_cert_suite_options`:
	# CLI flags (`--cert-suite-id`, `--cert-suite-evidence-sha256`,
	# `--cert-suite-no-evidence`, ...) take precedence over the
	# legacy `DRIFT_DEPLOY_CERT_SUITE_*` env vars; missing both forms
	# is a hard error there (a signed cert claim must NOT carry a
	# synthetic default).  By the time we get here `cert_suite_options`
	# is fully resolved, including any no-evidence-sentinel opt-in.
	# The visible warning lives here, alongside the actual emission,
	# so it can't be silently elided by a future refactor.
	if cert_suite_options.no_evidence_sentinel:
		# Softer, operator-facing wording: name the artifact, say what the
		# consequence is, and DON'T surface the raw empty-hash sentinel on the
		# normal deploy path.  The actual `result_evidence_sha256` (the
		# empty-bytes sentinel) is still recorded in the signed claim below and
		# remains visible in structured/verbose output and at verify time — it
		# just doesn't clutter the normal stderr warning.
		print(
			f"warning: cert suite '{cert_suite_options.id}' did not attach a "
			f"test-report artifact for {artifact_kind} '{package_id}'.\n"
			f"The staged cert claim is still signed, but downstream "
			f"verification will show that no report was attached. For "
			f"release/cert-pool artifacts, attach the report or document the "
			f"exception.",
			file=sys.stderr,
		)
	cert_suite = CertSuite(
		id=cert_suite_options.id,
		version=cert_suite_options.version,
		result=cert_suite_options.result,
		result_evidence_sha256=cert_suite_options.result_evidence_sha256,
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

	# v2 signed locator: a deployed package is materialized as the
	# `<package_id>.zdmp` beside the sidecars; a deployed app is the
	# runnable binary itself (`artifact_path` here is that on-disk binary,
	# so its filename is the canonical locator `verify_deployed_app`
	# matches against).
	signed_locator = (
		artifact_path.name if artifact_kind == "app" else f"{package_id}.zdmp"
	)
	body = make_cert_claim_body(
		package_id=package_id,
		version=package_version,
		artifact_kind=artifact_kind,
		artifact_path=signed_locator,
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
	timing: bool = False,
) -> None:
	"""Built-in baseline smoke for package artifacts.

	`timing`: pass `--timing-out` to the smoke driftc invocation;
	merge child timings into the active wrapper sink under
	`smoke.compile.*`.  Time the wrapper-Python compile and run
	steps via `events.timed("smoke.compile")` and
	`events.timed("smoke.run")`, so the wrapper summary attributes
	wall-clock between subprocess overhead, compile work, and run
	work distinctly.
	"""
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
		f'pub fn main() nothrow -> Int {{\n'
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

	# Side-channel timings file for the smoke compile.  Close the
	# mkstemp fd immediately to avoid leaking descriptors across
	# repeated invocations; driftc overwrites the file via
	# Path.write_text() when the child compile finishes.
	timing_out_path: Path | None = None
	if timing:
		_fd, _path = tempfile.mkstemp(
			prefix=f"drift-deploy-{art.name}-smoke-compile-",
			suffix=".timing.json",
		)
		os.close(_fd)
		timing_out_path = Path(_path)
		cmd.extend(["--timing-out", str(timing_out_path)])

	has_native = bool(art.native_deps)

	clean = _clean_env()

	if has_native:
		# Compile + link + run.
		with _events.timed("smoke.compile"):
			result = subprocess.run(cmd, capture_output=True, text=True, env=clean)
		if result.returncode != 0:
			_merge_and_cleanup_child_timings(timing_out_path, "smoke.compile")
			raise DeployError(
				f"baseline smoke failed for '{art.name}' (compile+link):\n"
				f"{result.stderr.strip()}"
			)
		_merge_and_cleanup_child_timings(timing_out_path, "smoke.compile")
		# Run.
		with _events.timed("smoke.run"):
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
		with _events.timed("smoke.compile"):
			result = subprocess.run(cmd, capture_output=True, text=True, env=clean)
		if result.returncode != 0:
			# Retry without --test-build-only in case it's not supported.
			cmd.remove("--test-build-only")
			with _events.timed("smoke.compile"):
				result = subprocess.run(cmd, capture_output=True, text=True, env=clean)
			if result.returncode != 0:
				_merge_and_cleanup_child_timings(timing_out_path, "smoke.compile")
				raise DeployError(
					f"baseline smoke failed for '{art.name}' (compile):\n"
					f"{result.stderr.strip()}"
				)
		_merge_and_cleanup_child_timings(timing_out_path, "smoke.compile")


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
	compiler_info: "CompilerInfo",
	staged_pkg_root: Path,
	cert_suite_options: "CertSuiteOptions | None",
	native_lib_paths: list[Path] | None = None,
	dep_namespace_map: dict[str, str] | None = None,
	author_profile_path: Path | None = None,
	timing: bool = False,
) -> None:
	"""Per-artifact deploy entry.  Owns the wrapper-level timing
	session when `timing` is set: installs an EventSink for the
	artifact, lets the inner pipeline emit phases via
	`events.timed(...)` and `_build_package` / `_build_app` /
	`_run_baseline_smoke_package` merge child driftc timings under
	the right prefix, then prints the consolidated
	`[drift:timing][<art.name>] ...` block.  Without `timing`,
	straight passthrough to the impl -- no sink, no overhead.
	"""
	if not timing:
		return _deploy_artifact_impl(
			art,
			driftc=driftc,
			target=target,
			resolved=resolved,
			stage_dir=stage_dir,
			manifest_dir=manifest_dir,
			package_roots=package_roots,
			dest=dest,
			app_dest=app_dest,
			sign_key=sign_key,
			baseline_trust=baseline_trust,
			skip_smoke=skip_smoke,
			dry_run=dry_run,
			compiler_info=compiler_info,
			staged_pkg_root=staged_pkg_root,
			cert_suite_options=cert_suite_options,
			native_lib_paths=native_lib_paths,
			dep_namespace_map=dep_namespace_map,
			author_profile_path=author_profile_path,
			timing=False,
		)
	wrapper_sink = _events.EventSink()
	with _events.install_sink(wrapper_sink):
		wrapper_sink.begin_compile()
		try:
			_deploy_artifact_impl(
				art,
				driftc=driftc,
				target=target,
				resolved=resolved,
				stage_dir=stage_dir,
				manifest_dir=manifest_dir,
				package_roots=package_roots,
				dest=dest,
				app_dest=app_dest,
				sign_key=sign_key,
				baseline_trust=baseline_trust,
				skip_smoke=skip_smoke,
				dry_run=dry_run,
				compiler_info=compiler_info,
				staged_pkg_root=staged_pkg_root,
				cert_suite_options=cert_suite_options,
				native_lib_paths=native_lib_paths,
				dep_namespace_map=dep_namespace_map,
				author_profile_path=author_profile_path,
				timing=True,
			)
		finally:
			# Print the wrapper summary in the SAME finally that
			# closes the sink so failed deploys still report timing
			# (those are the runs users will most often send back
			# during adoption -- and child driftc summaries are
			# suppressed by --timing-out, so without this they'd see
			# no timing block at all).
			wrapper_sink.close_all_open_phases()
			wrapper_sink.end_compile()
			# Outermost emit -- only the wrapper prints.  Same shape
			# as drift_build's `_print_wrapper_timing_summary` (kept
			# independent so deploy doesn't import build's private
			# helper).
			_summary = wrapper_sink.timings_summary()
			_total = float(_summary.get("total_wall", 0.0))
			_phases = dict(_summary.get("phases", {}))
			_counts = dict(_summary.get("counts", {}))
			print(
				f"[drift:timing][{art.name}] total_wall={_total:.3f}s",
				file=sys.stderr,
			)
			for _k, _v in sorted(
				_phases.items(), key=lambda kv: (-float(kv[1]), kv[0]),
			):
				_c = _counts.get(_k, 0)
				_vs = float(_v)
				_pct = (_vs / _total * 100.0) if _total > 0 else 0.0
				print(
					f"[drift:timing][{art.name}]   {_k:<28s} = {_vs:7.3f}s  {_pct:5.1f}%  count={_c}",
					file=sys.stderr,
				)
			# Workload block (`[drift:workload][<art>] ...`) follows the
			# timing block when the merged sink carries any workload
			# counters.  Keys appear under `build.compile.*` /
			# `smoke.compile.*` prefixes courtesy of
			# `_merge_and_cleanup_child_timings` -- which now merges
			# the child's workload dict via the sibling
			# `merge_subprocess_workload`.
			_workload = dict(_summary.get("workload", {}))
			if _workload:
				_schema = int(_summary.get("workload_schema", 0))
				print(
					f"[drift:workload][{art.name}] workload_schema={_schema}",
					file=sys.stderr,
				)
				for _wk in sorted(_workload.keys()):
					print(
						f"[drift:workload][{art.name}]   {_wk}={int(_workload[_wk])}",
						file=sys.stderr,
					)


def _deploy_artifact_impl(
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
	# Required for package artifacts (cert claim emission); None
	# permitted for app-only deploys (no cert claim is signed).
	cert_suite_options: CertSuiteOptions | None,
	native_lib_paths: list[Path] | None = None,
	dep_namespace_map: dict[str, str] | None = None,
	author_profile_path: Path | None = None,
	timing: bool = False,
) -> None:
	"""Full pipeline for one artifact: build → sign → assets → smoke → publish.

	`timing`: forward `--timing` to the underlying driftc build, and
	re-emit `[drift:timing][<art.name>] ...` lines from driftc's
	stderr.  No effect on artifact bytes or sign/cert output.
	"""
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
	# v2: BOTH package and app artifacts carry SCI — it is the provenance
	# (and author/cert) leg of the three-way source-identity equality.
	# `compute_artifact_sci` passes the canonical `art.kind` through, so
	# the same Artifact + manifest_dir produces the same digest here and in
	# `drift_build`; a divergence would fail the consumer-side three-way
	# SCI check (trust-v1.md §3.5).
	if art.kind in ("package", "app"):
		from tools.drift_deploy.build_cmd import compute_artifact_sci
		try:
			source_content_id = compute_artifact_sci(art, manifest_dir=manifest_dir)
		except (FileNotFoundError, ValueError) as e:
			# v2/v4: source_content_id is the signed leg of provenance AND
			# the author/cert claims.  A certified package/app cannot be
			# emitted without it — fail HARD here, before any provenance is
			# built, rather than the v0 "warn and skip" path.
			raise DeployError(
				f"artifact '{art.name}': could not compute source_content_id "
				f"({e}); v2 certified package/app deploys require a resolvable "
				f"source tree to attest source identity."
			)
	if art.kind == "package":
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
			timing=timing,
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
			timing=timing,
		)

	# ── Step 2: Stage author profile + v1 trust sidecars (package only) ──
	#
	# `sig_path` is a v1 cert-claim sidecar in this flow (kept as the
	# variable name for back-compat with downstream smoke env wiring
	# that still spells it DRIFT_STAGED_SIG).  No staged_trust_path:
	# v1 has no overlay -- smoke uses the project's baseline trust.
	sig_path: Path | None = None
	staged_profile: Path | None = None

	if art.kind == "package":
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
			source_content_id=source_content_id,
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
		# author published it via `drift author` BEFORE running
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
		with _events.timed("attach_author_claim"):
			author_claim_path = _attach_author_claim_to_artifact(
				package_id=art.name,
				package_version=art.version,
				source_content_id=source_content_id,
				manifest_dir=manifest_dir,
				staged_install=staged_install,
			)
		# `_run_impl` always resolves cert_suite_options up front when
		# `has_packages` is True; reaching this branch with no options
		# would be an internal contract bug, not a user-facing error.
		assert cert_suite_options is not None, (
			"package artifact reached cert-claim emission without "
			"resolved cert_suite_options -- `_run_impl` must call "
			"`_resolve_cert_suite_options` whenever the manifest has "
			"a package artifact"
		)
		with _events.timed("cert_emit"):
			cert_claim_path = _emit_cert_claim_for_artifact(
				dmp_path,
				cert_key=sign_key,
				package_id=art.name,
				package_version=art.version,
				artifact_kind=art.kind,
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
				cert_suite_options=cert_suite_options,
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
	# Package artifacts now carry their declared assets INSIDE the verified
	# .dmp/.zdmp (content-addressed blobs covered by the cert claim's
	# artifact_sha256 + the SCI), materialized only through the verify-gated
	# `drift unpack`.  So a package no longer publishes a loose, unverified
	# `assets/` folder — that ambiguous trust path is removed deliberately.
	# Apps have no signed container to carry assets, so they keep the loose
	# staging (app asset trust is out of scope for the in-package design).
	if art.kind == "app":
		_stage_assets(art, manifest_dir=manifest_dir, staged_install=staged_install)

	# ── Step 4: Provenance + author/cert legs (app) ──
	# An app is a first-class certified artifact: it carries the SAME
	# three-leg agreement as a package (author claim + cert claim +
	# provenance), differing only in that the certified bytes are the
	# runnable BINARY (not a `.zdmp` container) and there is no smoke
	# package root.  The cert claim's signed `artifact_path` names the
	# binary, which is what `verify_deployed_app` matches against.
	if art.kind == "app":
		import hashlib as _hl
		if sign_key is None:
			raise DeployError(
				f"artifact '{art.name}': signing key required for app artifacts; "
				f"pass --sign-key-file or set $DRIFT_SIGN_KEY_FILE"
			)
		# Stage the author profile (informational sidecar; carried for
		# parity with package deploys and downstream inspection tooling).
		if author_profile_path:
			from lang.drift.author_profile import load_author_profile, write_author_profile
			from dataclasses import replace as _dc_replace
			src_profile = load_author_profile(author_profile_path)
			bound_profile = _dc_replace(src_profile, package=art.name)
			staged_profile = staged_install / f"{art.name}.author-profile"
			write_author_profile(bound_profile, staged_profile)
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
			source_content_id=source_content_id,
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

		# Author + cert legs (same pre-signed-author / deploy-signs-cert
		# split as packages).  The author published `<app>.author-claim`
		# via `drift author` before this run; deploy locates it, validates
		# it binds this exact release (id/version/SCI), and stages it next
		# to the binary, then emits a fresh cert claim binding the binary's
		# sha256 + SCI + dep_graph + cert_suite + provenance evidence.
		if source_content_id is None:
			raise DeployError(
				f"artifact '{art.name}': source_content_id was not computed "
				f"for this build; v1 cert claims require the SCI to attest "
				f"source identity.  Re-run with the source tree available."
			)
		with _events.timed("attach_author_claim"):
			author_claim_path = _attach_author_claim_to_artifact(
				package_id=art.name,
				package_version=art.version,
				source_content_id=source_content_id,
				manifest_dir=manifest_dir,
				staged_install=staged_install,
			)
		assert cert_suite_options is not None, (
			"app artifact reached cert-claim emission without resolved "
			"cert_suite_options -- `_run_impl` must call "
			"`_resolve_cert_suite_options` whenever the manifest has a "
			"package or app artifact"
		)
		with _events.timed("cert_emit"):
			cert_claim_path = _emit_cert_claim_for_artifact(
				app_bin_path,
				cert_key=sign_key,
				package_id=art.name,
				package_version=art.version,
				artifact_kind=art.kind,
				target=target,
				compiler_info=compiler_info,
				source_content_id=source_content_id,
				artifact_sha256=app_sha256,
				resolved_deps=resolved,
				direct_dep_ids={d.name for d in art.package_deps},
				staged_pkg_root=staged_pkg_root,
				provenance_path=provenance_path,
				cert_suite_options=cert_suite_options,
			)
		# Wire the cert claim into `sig_path` for the app smoke env
		# (DRIFT_STAGED_SIG), mirroring the package path.  Both sidecars
		# travel into the published app dir via `_publish_app`'s copytree.
		sig_path = cert_claim_path

	# ── Step 5: Smoke ──
	# Build a filtered smoke package root containing only the artifact
	# itself and its resolved deps. The compiler eagerly verifies all
	# packages under --package-root, so unrelated signed packages with
	# untrusted namespaces would block smoke compilation. This root is
	# used for both baseline and custom smoke (via DRIFT_STAGED_PKG_ROOT).
	smoke_pkg_root = stage_dir / f"_smoke_pkgroot_{art.name}"
	smoke_pkg_root.mkdir(parents=True, exist_ok=True)
	if art.kind == "package":
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
		if art.kind == "package":
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
				timing=timing,
			)
		else:
			with _events.timed("smoke.app"):
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
		if art.kind == "package":
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

		with _events.timed("smoke.custom"):
			_run_custom_smoke(art, env=smoke_env)

	# ── Step 6: Publish ──
	if dry_run:
		print(f"  dry-run: skipping publish for '{art.name}'")
		return

	with _events.timed("publish"):
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
	has_packages = any(a.kind == "package" for a in artifacts)
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

	# Resolve cert-suite identity + evidence (CLI > env > error).  We
	# resolve it once up front -- before any artifact build -- so a
	# missing/conflicting CLI+env config fails the run fast, not
	# halfway through the build.  BOTH package and app artifacts emit a
	# cert claim (the cert leg of the three-leg agreement), so the
	# evidence digest is needed whenever the manifest declares either.
	cert_suite_options: CertSuiteOptions | None = None
	if has_packages or has_apps:
		cert_suite_options = _resolve_cert_suite_options(args)

	# Package roots: default to --dest.
	package_roots = args.package_root or ([args.dest] if args.dest else [])

	# Native library search paths (env + config + CLI).
	native_lib_paths = _resolve_native_lib_paths(args, manifest_dir)

	# Signing key required for any certified artifact (package or app):
	# both emit a cert claim, which the certifier signs.
	if (has_packages or has_apps) and sign_key is None:
		raise DeployError(
			"signing key required for package/app artifacts; "
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
		a.name: a.module_namespace for a in artifacts if a.kind == "package"
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

	# Package artifacts in this manifest are the ONLY packages whose
	# lock entries may legitimately carry `dep_type: "co-artifact"`
	# (bypassing the sha/signer re-check because they are built in
	# this same deploy run).  Anything else claiming co-artifact
	# status in the lock is rejected at verify time.
	co_artifact_names = {a.name for a in artifacts if a.kind == "package"}

	# Source-rebuild lane selector + orch run snapshot.  Deploy
	# enters source-rebuild mode under `DRIFT_CERT_MODE=stage`,
	# `DRIFT_CERT_MODE=certify`, or explicit `--source-rebuild`.
	# Unset leaves deploy in strict-lock producer mode (local
	# publishing).  Under source-rebuild the snapshot is REQUIRED.
	#
	# The stage vs certify split is expressed as `snapshot_exempt_ids`:
	# under stage, the manifest's package-artifact names are exempt
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
				cert_suite_options=cert_suite_options,
				native_lib_paths=native_lib_paths,
				dep_namespace_map=dep_namespace_map,
				author_profile_path=author_profile_path,
				timing=getattr(args, "timing", False),
			)

	finally:
		# Clean up staging directory.
		shutil.rmtree(str(stage_dir), ignore_errors=True)

	print(f"\ndrift deploy: done ({len(artifacts)} artifact(s))")
	return 0


if __name__ == "__main__":
	sys.exit(run())
