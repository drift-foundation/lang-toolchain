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

# Re-export from the shared module so `drift prepare`, `drift build`,
# and `drift deploy` share ONE source of truth for the source-rebuild
# lane selector — matching CLI flag OR `DRIFT_SOURCE_REBUILD=1` env
# var.  The env-var path is what orch sets for source-from-commit
# certification runs so repo-owned `just test` / lock-check
# invocations pick up the lane without each justfile threading
# `--source-rebuild` explicitly.
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
		help="Package library root (used as default --package-root)")
	p.add_argument("--package-root", type=UserPath, action="append", default=None,
		help="Library root for resolving package_deps (repeatable; default: --dest)")
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
			"path is authoritative/strict by design.  Compares the on-"
			"disk lock to the freshly-resolved graph on {dep set, "
			"version, dep_type, source_content_id, "
			"source_attestation_key} only; tolerates `sha256` and "
			"`author_key` drift because the on-disk packages may be "
			"rebuilt artifacts whose bytes and signer key legitimately "
			"differ from the original author's.  Per-package byte/"
			"signer drift is printed to stdout as run evidence so "
			"humans (and CI logs) can see the divergence.  Trust-root "
			"enforcement is preserved: non-co-artifact deps with empty "
			"source identity or `author_key == \"unsigned\"` on either "
			"side of the comparison are hard-failed — no silent "
			"fallback to byte-only verification.  Default `--check` "
			"remains strict/exact — this flag is the only way to relax "
			"it.  Also enabled by `DRIFT_SOURCE_REBUILD=1` so orch-"
			"driven `just test` / lock-check invocations can select "
			"the lane without the downstream justfile threading "
			"`--source-rebuild` through every `drift prepare --check` "
			"call; in that path the env var is silently ignored on the "
			"lock-writing path (orch sets it globally for the "
			"certification environment).  Mirrors the `drift build` / "
			"`drift deploy` pattern."
		))
	return p


def _topo_sort_artifacts(artifacts: list) -> list:
	"""Topological sort: packages before apps that depend on them."""
	from tools.drift_deploy.drift_deploy import _topo_sort_artifacts as _ts
	return _ts(artifacts)


# Fields the source-rebuild `--check` lane compares on.  sha256 and
# author_key are intentionally absent — they're the artifact-byte /
# artifact-signer half of the v4 identity and legitimately differ
# when packages are rebuilt by a different hand.  Source identity
# (`source_content_id` + `source_attestation_key`) IS compared because
# that half is what the package owner attested, and the whole point
# of source-rebuild mode is that the rebuild was made from the same
# attested source.  `dep_type` is included so a co-artifact→direct
# flip (or vice-versa) still surfaces as drift in either mode.
_SOURCE_REBUILD_EQ_FIELDS = (
	"version", "dep_type", "source_content_id", "source_attestation_key",
)


def _compare_locks_for_check(
	existing: dict[str, dict[str, ResolvedDep]],
	resolved: dict[str, dict[str, ResolvedDep]],
	*,
	source_rebuild: bool,
	byte_drift_log: list[tuple[str, str, str, str, str, str]],
) -> list[str]:
	"""Compare an on-disk lock to a freshly-resolved graph for `--check`.

	Returns a list of drift descriptions; empty = locks are equivalent
	under the selected mode.  `byte_drift_log` is appended with
	`(artifact, pkg_id, locked_sha, resolved_sha, locked_author_key,
	resolved_author_key)` tuples for every per-dep sha256 OR author_key
	disagreement encountered in source-rebuild mode — printed by the
	caller as run evidence.  In strict mode `byte_drift_log` is
	untouched; mismatches on those fields surface through the returned
	drift list instead.

	Strict mode (default) is byte-for-byte: any disagreement on any
	`ResolvedDep` field (via `ResolvedDep` equality) is drift.

	Source-rebuild mode enforces artifact set + per-artifact dep
	set + `(version, dep_type, source_content_id,
	source_attestation_key)` AND a signed trust root on every
	non-co-artifact dep: empty `source_content_id`, empty
	`source_attestation_key`, or `author_key == "unsigned"` on EITHER
	side is rejected — source-rebuild mode only certifies packages
	with a signed source attestation, so an empty-identity entry
	matching another empty-identity entry by dict equality is a trust
	bypass.  Co-artifact entries are legitimately empty on both sides
	(their `.dmp` + sidecars are built later in the same deploy run)
	and are exempted from the trust-root check.
	"""
	errors: list[str] = []
	if existing.keys() != resolved.keys():
		added = sorted(resolved.keys() - existing.keys())
		removed = sorted(existing.keys() - resolved.keys())
		if added:
			errors.append(f"artifacts added since prepare: {', '.join(added)}")
		if removed:
			errors.append(f"artifacts removed since prepare: {', '.join(removed)}")
		# Dep-level diffs on shared artifacts are still useful signal;
		# fall through to report them too.
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
			if source_rebuild:
				for field in _SOURCE_REBUILD_EQ_FIELDS:
					ev = getattr(e, field)
					rv = getattr(r, field)
					if ev != rv:
						errors.append(
							f"artifact '{art_name}' dep '{pkg_id}': "
							f"{field} differs (locked {ev!r}, resolved {rv!r})"
						)
				# Trust-root gate: non-co-artifact deps MUST carry a
				# signed source identity.  Empty `source_content_id`,
				# empty `source_attestation_key`, or `author_key ==
				# "unsigned"` on either side means there is nothing to
				# verify the rebuild against — accepting would collapse
				# trust to "trust the rebuilder," which is the exact
				# bypass source-rebuild mode exists to prevent.  This
				# mirrors `verify_lock_compatibility`'s unsigned-reject
				# and missing-attestation-reject paths in the build /
				# deploy lanes; without it, two empty-identity entries
				# would pass the dict-equality check above and reach a
				# `--check` pass under the source-rebuild banner.  The
				# co-artifact dep_type is exempted because its empty
				# identity is structural (built later, same deploy
				# run), not a missing trust root.
				if e.dep_type != "co-artifact" and r.dep_type != "co-artifact":
					for side, dep in (("locked", e), ("resolved", r)):
						if not dep.source_content_id or not dep.source_attestation_key:
							errors.append(
								f"artifact '{art_name}' dep '{pkg_id}': "
								f"{side} entry has no signed source identity "
								f"(source_content_id={dep.source_content_id!r}, "
								f"source_attestation_key={dep.source_attestation_key!r}); "
								f"source-rebuild mode requires a signed source "
								f"attestation as the trust root.  Republish "
								f"with toolchain >= 0.30.0 and re-run `drift "
								f"prepare`, or drop `--source-rebuild` / "
								f"`DRIFT_SOURCE_REBUILD` to use strict mode."
							)
						if dep.author_key == "unsigned":
							errors.append(
								f"artifact '{art_name}' dep '{pkg_id}': "
								f"{side} entry is `author_key: \"unsigned\"`; "
								f"unsigned packages have no `.source-"
								f"attestation` sidecar so source-rebuild mode "
								f"cannot certify them.  Sign and republish "
								f"(toolchain >= 0.30.0) before using "
								f"`--source-rebuild` / `DRIFT_SOURCE_REBUILD` "
								f"on this dep."
							)
				# sha256 / author_key: record drift, not error.
				# Only log when EITHER side has a non-empty value —
				# co-artifact entries carry "" on both sides and are
				# not drift.
				if (e.sha256 or r.sha256) and e.sha256 != r.sha256:
					byte_drift_log.append((
						art_name, pkg_id,
						e.sha256, r.sha256,
						e.author_key, r.author_key,
					))
				elif (e.author_key or r.author_key) and e.author_key != r.author_key:
					# author_key drifted even though sha matched (rare
					# but possible: same bytes, different signer —
					# e.g. cross-signed republish).  Still evidence.
					byte_drift_log.append((
						art_name, pkg_id,
						e.sha256, r.sha256,
						e.author_key, r.author_key,
					))
			else:
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
	# env-var path (`DRIFT_SOURCE_REBUILD=1`) is NOT treated as an
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
			"var form `DRIFT_SOURCE_REBUILD=1` is silently ignored on "
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

		# Mark co-artifact deps: sha256 is unknown at prepare time
		# (the .dmp hasn't been built yet).  Use an empty sha — the
		# v4 reader accepts empty sha256 iff dep_type="co-artifact",
		# and `verify_lock_compatibility` skips co-artifacts entirely.
		# Deploy verifies the real sha at build time after the
		# co-artifact is staged.  Same skip applies to
		# `source_content_id` and `source_attestation_key` — both are
		# left "" until the co-artifact's `.source-attestation`
		# sidecar is emitted later in the same deploy run.
		for pkg_id in list(resolved):
			if pkg_id in co_artifact_names:
				old = resolved[pkg_id]
				# Match the shape `read_lock` reconstructs from the
				# on-disk JSON (see lockfile.read_lock): `package_id`
				# is the map key, `author_key` / `source_content_id` /
				# `source_attestation_key` are "" for co-artifacts
				# (signing and attestation have not happened yet —
				# the .dmp + sidecars are built later in the same
				# deploy run).  Without these the in-memory map and
				# the freshly-read lock compare unequal even when
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
	# Empty fields here mean the package's `.source-attestation`
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
			# (`author_key == "unsigned"`), its `.source-attestation`
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
			"drift prepare: cannot write v4 lock — these resolved "
			"dependencies have no valid source attestation:",
		]
		for art_name, pkg_id, ver in missing_attestation:
			lines.append(f"  {art_name} -> {pkg_id}@{ver}")
		lines.append(
			"Republish each listed package with toolchain >= 0.30.0 "
			"so its `.source-attestation` sidecar is emitted, then "
			"re-run `drift prepare`.  Per-package stderr warnings "
			"above (if any) name the specific failure mode for "
			"sidecars that were present-but-rejected (mismatched "
			"package_id/version/target_class/required_deps/"
			"source_content_id, or signature verification failure); "
			"a missing sidecar produces no warning — it just shows "
			"up here.  The v4 lock format requires source identity "
			"for every non-co-artifact dep so source-rebuild "
			"certification has a signed source identity to verify "
			"against — silently allowing empty fields would defeat "
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
		byte_drift_log: list[tuple[str, str, str, str, str, str]] = []
		errors = _compare_locks_for_check(
			existing, resolved_map,
			source_rebuild=source_rebuild,
			byte_drift_log=byte_drift_log,
		)
		# Always surface byte/signer drift as evidence when we saw any,
		# even if the overall check passes.  In strict mode this list
		# is always empty (differences become errors instead), so the
		# branch is a no-op there.  Prints before the pass/fail verdict
		# so the evidence sits next to the mode label in CI logs.
		if byte_drift_log:
			print(
				f"drift prepare --check ({mode_label}): byte/signer drift "
				f"vs. lock (tolerated under source-rebuild mode):"
			)
			for art_name, pkg_id, lsha, rsha, lak, rak in byte_drift_log:
				print(f"  {art_name} -> {pkg_id}:")
				if lsha != rsha:
					print(f"    sha256      locked={lsha!r} resolved={rsha!r}")
				if lak != rak:
					print(f"    author_key  locked={lak!r} resolved={rak!r}")
		if errors:
			print(
				f"drift prepare --check ({mode_label}): {lock_path} is "
				f"stale; run `drift prepare` to refresh",
				file=sys.stderr,
			)
			for err in errors:
				print(f"  {err}", file=sys.stderr)
			return 1
		print(f"drift prepare --check ({mode_label}): {lock_path} is up-to-date")
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
