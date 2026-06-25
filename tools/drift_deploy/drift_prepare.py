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
import os
import sys
from pathlib import Path

from tools.drift_deploy.build_cmd import UserPath
from tools.drift_deploy.lockfile import write_lock

# TYPE_CHECKING-style forward ref used in `_compare_locks_for_check`
# signature.  Imported lazily at call time in `_run_impl` to avoid a
# hard dependency at module load for CLI paths that never enter the
# source-rebuild branch.
from lang.driftc.packages.trust_v1 import TrustStore  # noqa: F401
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

# Uniform lane selector shared with `drift build` and `drift
# deploy`: `--source-rebuild` CLI flag OR `DRIFT_CERT_MODE=certify`.
# Orch's verification-phase signal is the env form; normal local
# `drift prepare --check` (unset `DRIFT_CERT_MODE`) stays in strict
# mode.
from tools.drift_deploy.build_cmd import CertModeError
from tools.drift_deploy.build_cmd import (
	producer_output_exemption_active as _producer_output_exemption_active,
)
from tools.drift_deploy.build_cmd import source_rebuild_enabled as _source_rebuild_enabled


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
		help="Package root (used as default --package-root)")
	p.add_argument("--package-root", type=UserPath, action="append", default=None,
		help="Package root for resolving package_deps (repeatable; default: --dest)")
	p.add_argument("--check", action="store_true",
		help="Verify-only mode: exit 0 iff re-resolution produces the "
		     "exact existing drift/lock.json; non-zero if the lock would "
		     "change or is absent.  Does not write the lock file.  Use "
		     "in CI to guard against stale locks.")
	p.add_argument("--source-rebuild", action="store_true",
		help=(
			"Source-rebuild equivalence mode for `--check`.  MUST be "
			"paired with `--check` — passing this flag without "
			"`--check` is rejected fail-fast, since the lock-writing "
			"path is authoritative/strict by design.  Re-resolves "
			"against the orch-produced run snapshot (see "
			"`--run-snapshot`), then compares the fresh graph to the "
			"on-disk lock.  Graph-shape / version / sha / signer "
			"drift vs. lock is logged as evidence (tolerated).  "
			"Requires `--run-snapshot` or `DRIFT_RUN_SNAPSHOT`; no "
			"snapshot is a hard fail.  Default `--check` remains "
			"strict/exact — this flag is the only way to relax it.  "
			"Manual synonym for the env-driven path "
			"`DRIFT_CERT_MODE=certify`; orch's certification-run "
			"verification phase uses the env form."
		))
	p.add_argument("--run-snapshot", type=UserPath, default=None,
		help=(
			"Path to the orch-produced run snapshot "
			"(`tools.drift_deploy.run_snapshot` JSON v0).  Required "
			"under `--source-rebuild` / `DRIFT_CERT_MODE=certify`.  "
			"Also honoured via `DRIFT_RUN_SNAPSHOT=<path>`; CLI "
			"wins on conflict.  Silently ignored on the lock-"
			"writing path (no `--check`), matching the "
			"discipline for `--source-rebuild` itself."
		))
	return p


def _topo_sort_artifacts(artifacts: list) -> list:
	"""Topological sort: packages before apps that depend on them."""
	from tools.drift_deploy.drift_deploy import _topo_sort_artifacts as _ts
	return _ts(artifacts)


def _compare_locks_for_check(
	existing: dict[str, dict[str, ResolvedDep]],
	resolved: dict[str, dict[str, ResolvedDep]],
) -> list[str]:
	"""Strict-mode byte-for-byte compare of the on-disk lock vs.
	a freshly-resolved graph.

	Returns a list of drift descriptions; empty means the two graphs
	are `ResolvedDep`-equal.  Used only by the strict `drift prepare
	--check` path.  Source-rebuild `--check` goes through the
	shared authority in `tools/drift_deploy/source_rebuild.py` —
	`apply_structural_trust_gates` + `compare_lock_vs_fresh` + the
	`print_evidence` helper — so there is exactly one executable
	policy path for source-rebuild across prepare / build / deploy.
	"""
	errors: list[str] = []
	if existing.keys() != resolved.keys():
		added = sorted(resolved.keys() - existing.keys())
		removed = sorted(existing.keys() - resolved.keys())
		if added:
			errors.append(f"artifacts added since prepare: {', '.join(added)}")
		if removed:
			errors.append(f"artifacts removed since prepare: {', '.join(removed)}")
	for art_name in sorted(existing.keys() & resolved.keys()):
		ed = existing[art_name]
		rd = resolved[art_name]
		if ed.keys() != rd.keys():
			added = sorted(rd.keys() - ed.keys())
			removed = sorted(ed.keys() - rd.keys())
			if added:
				errors.append(f"artifact '{art_name}': deps added: {', '.join(added)}")
			if removed:
				errors.append(f"artifact '{art_name}': deps removed: {', '.join(removed)}")
		for pkg_id in sorted(ed.keys() & rd.keys()):
			e = ed[pkg_id]
			r = rd[pkg_id]
			if e != r:
				errors.append(
					f"artifact '{art_name}' dep '{pkg_id}': "
					f"locked {e!r} != resolved {r!r}"
				)
	return errors


def run(argv: list[str] | None = None) -> int:
	"""Main entry point. Returns exit code."""
	parser = build_arg_parser()
	args = parser.parse_args(argv)

	try:
		return _run_impl(args)
	except PrepareError as e:
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
	# `--source-rebuild` is strictly a verification-lane selector for
	# `--check`; the lock-writing path is always authoritative/strict
	# by design.  Accepting the CLI flag on the write path would let
	# orch (or a human) believe they'd regenerated a "source-rebuild-
	# aware" lock when in fact the flag was silently ignored — the
	# exact ambiguity this flag was added to avoid.  Fail fast.  The
	# env-var path (`DRIFT_CERT_MODE=certify`) is NOT treated as an
	# opt-in here because orch legitimately sets it for the whole
	# certification environment and `drift prepare` (write) may still
	# be invoked in that env as a no-op on the selector; the explicit
	# CLI flag is the conscious-intent signal.
	if getattr(args, "source_rebuild", False) and not getattr(args, "check", False):
		raise PrepareError(
			"--source-rebuild is a verification-lane selector for "
			"`drift prepare --check`, not a lock-writing mode.  The "
			"lock emitted by `drift prepare` is the authoritative, "
			"byte-strict trust root downstream strict-mode consumers "
			"verify against; a 'source-rebuild-aware' lock is a "
			"category error.  Either pass `--check` (to verify an "
			"existing lock under the relaxed source-rebuild "
			"equivalence rule) or drop `--source-rebuild`.  The env-"
			"var form `DRIFT_CERT_MODE=certify` is silently ignored on "
			"the write path so orch can set the lane once for the "
			"whole certification environment; the explicit CLI flag "
			"must be paired with `--check`."
		)

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

	# Build package index.  When the run is a `--check --source-
	# rebuild` verification, load the trust store once and thread it
	# into `build_package_index` so the v1 author + cert claim
	# cryptographic verification + per-module-namespace role-tagged
	# allowlist enforcement runs before any resolved dep reaches
	# `_compare_locks_for_check`.  The write path (no `--check`)
	# does not take the flag and keeps the parse-only index -- the
	# lock-author's identity is the trust anchor there.
	# Source-rebuild `--check`: the orch-produced run snapshot is
	# the trust authority.  Load it here so the same snapshot is
	# shared across artifacts in a single --check invocation.  No
	# snapshot is a hard fail (matches drift_build / drift_deploy).
	_prepare_run_snap = None
	if getattr(args, "check", False) and _source_rebuild_enabled(args):
		from tools.drift_deploy.run_snapshot import load_run_snapshot
		_snap_path = getattr(args, "run_snapshot", None)
		if _snap_path is None:
			_env_path = os.environ.get("DRIFT_RUN_SNAPSHOT", "")
			if _env_path:
				_snap_path = Path(_env_path)
		if _snap_path is None:
			raise PrepareError(
				"source-rebuild --check requires a run snapshot.  "
				"Pass `--run-snapshot <path>` or set "
				"`DRIFT_RUN_SNAPSHOT=<path>`.  The snapshot pins "
				"source identity per certification run; source-"
				"rebuild verification cannot proceed without it."
			)
		try:
			_prepare_run_snap = load_run_snapshot(Path(_snap_path))
		except (ValueError, OSError) as e:
			raise PrepareError(f"run snapshot load failed: {e}")
	# Compute co-artifact names up front so they can be passed as
	# `snapshot_exempt_ids` under stage-mode source-rebuild (the
	# intra-manifest package-artifact peers are producer outputs of
	# the stage deploy; they must not be gated against the snapshot
	# here either, for consistency with `drift_deploy._run_impl`).
	_early_co_artifact_names = {a.name for a in artifacts if a.kind == "package"}
	_prepare_exempt_ids: set[str] | None = (
		set(_early_co_artifact_names)
		if _prepare_run_snap is not None and _producer_output_exemption_active()
		else None
	)
	try:
		pkg_index = build_package_index(
			package_roots,
			run_snapshot=_prepare_run_snap,
			snapshot_exempt_ids=_prepare_exempt_ids,
		)
	except ResolutionError as e:
		# Surface index-time failures (snapshot mismatch, missing
		# entry) as a normal prepare error, not a traceback.
		raise PrepareError(str(e))

	# Inject co-artifact entries: package-kind artifacts in the same manifest
	# can satisfy each other's package_deps without being published.
	co_artifact_names: set[str] = set()
	for art in artifacts:
		if art.kind == "package":
			co_artifact_names.add(art.name)
			pkg_deps = [(dep.name, dep.version) for dep in art.package_deps]
			entry = PackageEntry(
				package_id=art.name,
				version=parse_version(art.version),
				path=Path("/dev/null"),  # no .dmp yet
				# Co-artifact entries have no published sha256 —
				# the .dmp is built later in the same deploy run.
				# Use empty string; `verify_lock_compatibility`
				# skips co-artifacts entirely and `read_lock`
				# explicitly allows empty sha256 for
				# dep_type="co-artifact".
				sha256="",
				required_deps=pkg_deps,
			)
			# Co-artifact takes priority over any externally-discovered
			# entry with the same package_id.
			pkg_index[art.name] = [entry]

	# Resolve each artifact.
	#
	# For `--check --source-rebuild`, the authoritative resolve is
	# done below in the `--check` branch via
	# `source_rebuild.resolve_source_rebuild` — the single source-
	# rebuild authority that `drift build` / `drift deploy` also
	# consume.  Doing it here too would double-resolve and split the
	# trust-gate policy across two call sites.  Skip the main-loop
	# resolve in that case; the authority will run its own resolve +
	# per-dep structural trust gates + evidence collection.
	_is_src_rebuild_check = (
		getattr(args, "check", False) and _source_rebuild_enabled(args)
	)
	resolved_map: dict[str, dict[str, ResolvedDep]] = {}
	if not _is_src_rebuild_check:
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

			# Mark co-artifact deps: sha256 is unknown at prepare time
			# (the .dmp hasn't been built yet).  Use an empty sha —
			# the v4 reader accepts empty sha256 iff dep_type="co-
			# artifact", and `verify_lock_compatibility` skips co-
			# artifacts entirely.  Deploy verifies the real sha at
			# build time after the co-artifact is staged.  Same skip
			# applies to `source_content_id` and
			# `source_attestation_key` — both are left "" until the
			# co-artifact's v1 cert claim sidecar is emitted later
			# in the same deploy run.
			for pkg_id in list(resolved):
				if pkg_id in co_artifact_names:
					old = resolved[pkg_id]
					# Match the shape `read_lock` reconstructs from
					# the on-disk JSON (see lockfile.read_lock):
					# `package_id` is the map key, `author_key` /
					# `source_content_id` / `source_attestation_key`
					# are "" for co-artifacts (signing and
					# attestation have not happened yet — the .dmp +
					# sidecars are built later in the same deploy
					# run).  Without these the in-memory map and the
					# freshly-read lock compare unequal even when
					# they serialise identically, which broke `drift
					# prepare --check` for any manifest with a co-
					# artifact dep before the Phase 0 fix.
					resolved[pkg_id] = ResolvedDep(
						version=old.version,
						sha256="",
						dep_type="co-artifact",
						package_id=pkg_id,
						author_key="",
						source_content_id="",
						source_attestation_key="",
					)

			resolved_map[art.name] = resolved

	# Phase B trust gate: every non-co-artifact resolved dep must
	# carry a valid source identity (`source_content_id` +
	# `source_attestation_key`) before the v4 lock is written.
	# Skipped under `--check --source-rebuild` — `resolved_map` is
	# empty there (the authoritative resolve lives in the `--check`
	# branch via `resolve_source_rebuild`, whose
	# `apply_structural_trust_gates` pass enforces the same gate).
	# Empty fields here mean the package's v1 author claim
	# sidecar was missing or failed cross-binding / signature
	# verification at index time (see
	# `resolver._read_source_attestation_meta` for the per-package
	# warnings).  Writing such an entry would produce a v4 lock
	# that `read_lock` rejects on the next consume; surfacing the
	# error here gives the user a single, actionable diagnostic
	# instead of "the lock you just wrote is corrupt."
	missing_attestation: list[tuple[str, str, str]] = []  # [(artifact, pkg, version)]
	for art_name, resolved in resolved_map.items():
		for pkg_id, dep in resolved.items():
			if dep.dep_type == "co-artifact":
				continue
			# Unsigned dev opt-in: if the package itself is unsigned
			# (`author_key == "unsigned"`), its v1 cert claim
			# sidecar cannot exist either (signing infra governs both),
			# so source identity is implicitly empty too.  Skip the
			# gate for these — the unsigned escape hatch is preserved
			# end-to-end across both halves of the v4 identity.
			if dep.author_key == "unsigned":
				continue
			if not dep.source_content_id or not dep.source_attestation_key:
				missing_attestation.append((art_name, pkg_id, dep.version))
	if missing_attestation:
		lines = [
			"drift prepare: cannot write v4 lock -- these resolved "
			"dependencies have no valid v1 author claim:",
		]
		for art_name, pkg_id, ver in missing_attestation:
			lines.append(f"  {art_name} -> {pkg_id}@{ver}")
		lines.append(
			"Re-run `drift author` for each listed package "
			"so its `<pkg>.author-claim` sidecar is emitted, then "
			"re-run `drift prepare`.  Per-package stderr warnings "
			"above (if any) name the specific failure mode for "
			"sidecars that were present-but-rejected (mismatched "
			"package_id / version / source_content_id, or signature "
			"verification failure); a missing sidecar produces no "
			"warning -- it just shows up here.  The v4 lock format "
			"requires source identity for every non-co-artifact dep "
			"so source-rebuild certification has a signed source "
			"identity to verify against -- silently allowing empty "
			"fields would defeat "
			"the trust boundary."
		)
		raise PrepareError("\n".join(lines))

	lock_path = manifest_dir / "lock.json"

	if getattr(args, "check", False):
		# Verify-only mode: compare the freshly-resolved graph against
		# the on-disk lock.  Used by CI to detect stale locks without
		# mutating the working tree.  The lock MUST exist; absence is
		# treated as drift (prepare-before-commit was skipped).
		from tools.drift_deploy.lockfile import read_lock
		source_rebuild = _source_rebuild_enabled(args)
		mode_label = "source-rebuild" if source_rebuild else "strict"
		if not lock_path.exists():
			print(
				f"drift prepare --check: {lock_path} does not exist; "
				f"run `drift prepare` to generate it",
				file=sys.stderr,
			)
			return 1
		# A malformed or pre-v3 lock on disk is drift, not a crash.
		# `read_lock` raises `ValueError` for v1/v2/shape errors and
		# `json.JSONDecodeError` (a `ValueError` subclass) for
		# broken JSON; `OSError` covers late read failures after the
		# `exists()` check (races, permissions).  Treat every one of
		# these as "regenerate the lock" and exit non-zero with a
		# friendly diagnostic instead of a Python traceback.
		try:
			existing = read_lock(lock_path)
		except (ValueError, OSError) as e:
			print(
				f"drift prepare --check: {lock_path} is unreadable "
				f"({e}); run `drift prepare` to regenerate it",
				file=sys.stderr,
			)
			return 1
		if source_rebuild:
			# Single source-rebuild authority: `resolve_source_
			# rebuild` per artifact.  This is the SAME call path
			# `drift build --source-rebuild` and `drift deploy
			# --source-rebuild` take — one executable policy across
			# prepare / build / deploy.  Returns a typed
			# `SourceRebuildResult(resolved_graph, evidence, errors)`
			# that prepare here aggregates across artifacts for the
			# run-wide verdict.
			from tools.drift_deploy.source_rebuild import (
				print_evidence,
				resolve_source_rebuild,
			)
			errors: list[str] = []
			# Resolve every artifact with package_deps.  The
			# snapshot-gated `pkg_index` (built above with
			# `run_snapshot=_prepare_run_snap` and co-artifact
			# overlays) is passed in so the authority reuses it
			# instead of re-discovering packages.
			fresh_resolved: dict[str, dict[str, ResolvedDep]] = {}
			for art in artifacts:
				if not art.package_deps:
					continue
				result = resolve_source_rebuild(
					artifact=art,
					package_roots=package_roots,
					manifest_dir=manifest_dir,
					existing_lock_graph=existing.get(art.name, {}),
					co_artifact_names=co_artifact_names,
					pkg_index=pkg_index,
					run_snapshot=_prepare_run_snap,
					snapshot_exempt_ids=_prepare_exempt_ids,
				)
				errors.extend(result.errors)
				fresh_resolved[art.name] = result.resolved_graph
				print_evidence(
					art_name=art.name,
					channel="drift prepare --check",
					evidence=result.evidence,
				)
			# Artifact-set drift is structural (the consumer's own
			# manifest changed), not upstream movement — fail in
			# source-rebuild mode too.
			art_added = sorted(fresh_resolved.keys() - existing.keys())
			art_removed = sorted(existing.keys() - fresh_resolved.keys())
			if art_added:
				errors.append(f"artifacts added since prepare: {', '.join(art_added)}")
			if art_removed:
				errors.append(f"artifacts removed since prepare: {', '.join(art_removed)}")
			if errors:
				print(
					f"drift prepare --check (source-rebuild): {lock_path} is "
					f"stale or trust-gated; run `drift prepare` to refresh",
					file=sys.stderr,
				)
				for err in errors:
					print(f"  {err}", file=sys.stderr)
				return 1
			print(f"drift prepare --check (source-rebuild): {lock_path} is up-to-date")
			return 0
		# Strict `--check`: byte-exact lock vs. fresh-resolve.
		errors = _compare_locks_for_check(existing, resolved_map)
		if errors:
			print(
				f"drift prepare --check (strict): {lock_path} is "
				f"stale; run `drift prepare` to refresh",
				file=sys.stderr,
			)
			for err in errors:
				print(f"  {err}", file=sys.stderr)
			return 1
		print(f"drift prepare --check (strict): {lock_path} is up-to-date")
		return 0

	# Write lock file — full rewrite for the entire manifest.
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
