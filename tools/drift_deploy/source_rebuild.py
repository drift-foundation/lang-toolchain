# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Single authority for the source-rebuild run graph.

Under `DRIFT_SOURCE_REBUILD=1`, `drift prepare --check`, `drift build`,
and `drift deploy` all need to produce the SAME dependency graph and
the SAME verdict.  Historically the three callers each had their own
mixture of "consult the lock," "verify against the index," and
"substitute versions in place" — which let contradictions creep in
(e.g. `--check` accepting a new transitive dep as evidence while
`drift build` still compiled against the stale lock).

This module is the one place source-rebuild semantics live.

Pipeline (identical for all three callers):

    1. Load merged trust store (core + project + optional user).
    2. Build the package index with `trust_store` wired in.
       `build_package_index` cryptographically verifies each `.sig`
       against the trust store's pubkeys AND enforces the per-module-
       namespace allowlist (keyed by each `module_id` in the package
       manifest, NOT the package id).  Any trust-verification failure
       is a HARD ERROR — the bad package is not silently pruned,
       because pruning would let the resolver fall back to an older
       trusted in-range version and silently mask the artifact orch
       intended to certify.
    3. Re-resolve the artifact's direct-dep graph against that
       trust-verified index using the ordinary `resolve_artifact`
       constraint solver.  The resolver enforces consumer manifest
       ranges (direct deps) and producer `required_deps` (transitive
       deps) — the same rules that apply in strict mode.
    4. Structural per-dep gates on the fresh-resolved graph:
       non-co-artifact deps must have `author_key != "unsigned"` AND
       non-empty `source_content_id` / `source_attestation_key` on
       disk.  These catch cases the index-time cryptographic check
       cannot (unsigned packages have no `.sig` to verify; a missing
       `.source-attestation` yields empty identity at index time).
    5. Compare the fresh-resolved graph to the existing lock's
       per-artifact graph.  Any shape or per-field drift becomes
       EVIDENCE (`SourceRebuildEvidence`), not an error.
    6. Return `SourceRebuildResult(resolved_graph, evidence, errors)`.

The caller consumes:

    - `resolved_graph` — the authoritative graph.  Compile (drift
      build / drift deploy) and lock comparison (drift prepare
      --check) use this as the dependency graph.  The lock is NEVER
      consulted as the graph source under source-rebuild; it is
      input only for evidence comparison.
    - `evidence` — informational.  Printed to stdout for CI log
      capture; does not stall any step.
    - `errors` — non-empty means the caller must abort the run.
      These are "hard" failures (unsigned dep, missing attestation,
      untrusted signer kid, revoked kid, resolver failure).

Strict mode is UNCHANGED: callers keep consulting the lock as the
authoritative graph and call `verify_lock_compatibility` directly.
This module is the source-rebuild-only authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.drift_deploy.manifest import Artifact
from tools.drift_deploy.resolver import (
	PackageEntry,
	ResolutionError,
	ResolvedDep,
	build_package_index,
	resolve_artifact,
)


@dataclass(frozen=True)
class SourceRebuildEvidence:
	"""Lock-vs-fresh graph/signer drift for ONE artifact.

	Purely informational — callers print it but do not treat any
	field as a failure.  Field semantics:

	- `added`: `(pkg_id, fresh_version)` — present in the resolved
	  graph, absent from the lock.  New transitive pulled in by a
	  compatible upstream patch.
	- `removed`: `(pkg_id, lock_version)` — present in the lock,
	  absent from the resolved graph.  Transitive that an upstream
	  patch dropped.
	- `version_changed`: `(pkg_id, lock_version, fresh_version)` —
	  a pkg_id present in both graphs but at different M.N.P.
	- `sha_drift`: `(pkg_id, lock_sha, fresh_sha)` — bytes differ
	  (rebuild produced different artifact bytes).
	- `signer_drift`: `(pkg_id, field_name, lock_value, fresh_value)`
	  for `field_name` in `{author_key, source_content_id,
	  source_attestation_key}`.  Recorded when non-empty on either
	  side AND values differ.
	"""

	added: list[tuple[str, str]] = field(default_factory=list)
	removed: list[tuple[str, str]] = field(default_factory=list)
	version_changed: list[tuple[str, str, str]] = field(default_factory=list)
	sha_drift: list[tuple[str, str, str]] = field(default_factory=list)
	signer_drift: list[tuple[str, str, str, str]] = field(default_factory=list)

	def is_empty(self) -> bool:
		return not (
			self.added or self.removed or self.version_changed
			or self.sha_drift or self.signer_drift
		)


@dataclass(frozen=True)
class SourceRebuildResult:
	"""Output of `resolve_source_rebuild` for ONE artifact.

	- `resolved_graph` — authoritative graph for compile/lock-compare.
	- `evidence` — drift vs. the existing lock (informational).
	- `errors` — hard failures; non-empty means caller must abort.

	Invariants (by construction — tests pin them):
	  * Every entry in `resolved_graph` came from the trust-verified
	    package index, so its `.sig` cryptographically verified and
	    its signer kid is in the trust store's namespace allowlist
	    for every module_id the package declares.
	  * Every non-co-artifact entry has non-empty
	    `source_content_id` / `source_attestation_key` OR appears
	    in `errors`.
	  * No unsigned non-co-artifact dep reaches `resolved_graph`
	    without also appearing in `errors`.
	"""

	resolved_graph: dict[str, ResolvedDep]
	evidence: SourceRebuildEvidence
	errors: list[str]


def resolve_source_rebuild(
	*,
	artifact: Artifact,
	package_roots: list[Path],
	manifest_dir: Path,
	existing_lock_graph: dict[str, ResolvedDep] | None,
	co_artifact_names: set[str],
	pkg_index: dict[str, list[PackageEntry]] | None = None,
	trust_store: Any | None = None,
	co_artifact_entries: dict[str, PackageEntry] | None = None,
) -> SourceRebuildResult:
	"""Produce the source-rebuild run graph for one artifact.

	`manifest_dir` is used to locate `drift/trust.json` if
	`trust_store` is not supplied.  `package_roots` is the list
	of directories to walk for `.dmp` / `.zdmp` discovery.

	`pkg_index` and `trust_store` may be supplied by the caller to
	amortize work across multiple artifacts in the same run (e.g.
	`drift prepare --check` iterates the whole manifest).  If
	either is None this function loads / builds it.

	`co_artifact_entries` is an optional map of pkg_id → PackageEntry
	overlays (co-artifacts of the current manifest that haven't been
	built yet).  These take priority over any externally-discovered
	entry with the same pkg_id — the resolver will pin to the
	co-artifact's own version.  Matches the pattern used in
	`drift_prepare.py::_run_impl`.

	`existing_lock_graph` is the lock's per-artifact map (or None if
	no lock exists yet).  Used for evidence only; the lock is never
	the graph authority under source-rebuild.
	"""
	if trust_store is None:
		from tools.drift_deploy.trust_loader import load_merged_trust_store
		trust_store = load_merged_trust_store(manifest_dir)
	if pkg_index is None:
		pkg_index = build_package_index(
			package_roots,
			trust_store=trust_store,
		)
		# Layer in the caller-supplied co-artifact overlays AFTER
		# the trust-verified index is built.  Co-artifacts come from
		# the current manifest's library artifacts; they haven't
		# been signed yet (their `.dmp` is built later in the same
		# deploy run), so they can't pass the trust gate and must
		# be injected manually.
		if co_artifact_entries:
			for pkg_id, entry in co_artifact_entries.items():
				pkg_index[pkg_id] = [entry]

	errors: list[str] = []

	direct_deps = [(d.name, d.version) for d in artifact.package_deps]
	try:
		resolved_graph = resolve_artifact(
			artifact.name, direct_deps, pkg_index,
			searched_roots=package_roots,
		)
	except ResolutionError as e:
		return SourceRebuildResult(
			resolved_graph={},
			evidence=SourceRebuildEvidence(),
			errors=[
				f"artifact '{artifact.name}': source-rebuild resolve "
				f"against trust-verified package index failed: {e}"
			],
		)

	apply_structural_trust_gates(
		artifact.name, resolved_graph, co_artifact_names, errors,
	)

	evidence = compare_lock_vs_fresh(
		existing_lock_graph or {}, resolved_graph,
	)

	return SourceRebuildResult(
		resolved_graph=resolved_graph,
		evidence=evidence,
		errors=errors,
	)


def apply_structural_trust_gates(
	art_name: str,
	resolved: dict[str, ResolvedDep],
	co_artifact_names: set[str],
	errors: list[str],
) -> None:
	"""Per-dep source-rebuild gates the index-time crypto check can't catch.

	`build_package_index(trust_store=...)` enforces the cryptographic
	`.sig` check + namespace allowlist for SIGNED packages.  It
	cannot gate on:
	  * `author_key == "unsigned"` — the unsigned dev-opt-in; there's
	    no `.sig` for the crypto check to run against.  Rejected here
	    for source-rebuild because the owner-continuity trust anchor
	    has nothing to verify.
	  * empty `source_content_id` / `source_attestation_key` — a
	    missing `.source-attestation` sidecar yields empty identity
	    at index time; the crypto check for the sidecar is triggered
	    only when the sidecar exists.  Rejected here because source-
	    rebuild's trust anchor requires a signed attestation.
	Co-artifact entries (same-manifest libraries built later in the
	same run) are exempt.
	"""
	for pkg_id, dep in resolved.items():
		if dep.dep_type == "co-artifact" or pkg_id in co_artifact_names:
			continue
		# Reject BOTH the dev-opt-in `"unsigned"` sentinel AND an
		# empty `author_key` (no `.sig` sidecar).  An empty
		# author_key on a non-co-artifact means the package has no
		# `.sig` at all — the primary index-time gate in
		# `build_package_index(trust_store=...)` hard-fails on this
		# path too; this helper keeps a defence-in-depth check so
		# callers that build the index without the trust store (or
		# that inject mocked PackageEntry objects in tests) still
		# trip the boundary.
		if dep.author_key == "unsigned" or not dep.author_key:
			errors.append(
				f"artifact '{art_name}' dep '{pkg_id}': resolved "
				f"entry has no verifiable signer "
				f"(author_key={dep.author_key!r}); source-rebuild "
				f"requires every disk package to cryptographically "
				f"verify against the trust store's namespace "
				f"allowlist.  Sign and republish (toolchain >= "
				f"0.30.0) under a trusted kid before using source-"
				f"rebuild on this dep."
			)
			continue
		if not dep.source_content_id:
			errors.append(
				f"artifact '{art_name}' dep '{pkg_id}': resolved "
				f"entry has empty `source_content_id` on disk; "
				f"source-rebuild requires a non-empty source "
				f"attestation (the trust gate has nothing to verify "
				f"otherwise).  Republish with toolchain >= 0.30.0 "
				f"so the `.source-attestation` sidecar is emitted."
			)
		if not dep.source_attestation_key:
			errors.append(
				f"artifact '{art_name}' dep '{pkg_id}': resolved "
				f"entry has empty `source_attestation_key` on disk; "
				f"source-rebuild requires a non-empty source-"
				f"attestation signer.  Republish with toolchain "
				f">= 0.30.0."
			)


def compare_lock_vs_fresh(
	lock: dict[str, ResolvedDep],
	fresh: dict[str, ResolvedDep],
) -> SourceRebuildEvidence:
	added: list[tuple[str, str]] = [
		(pkg_id, fresh[pkg_id].version)
		for pkg_id in sorted(fresh.keys() - lock.keys())
	]
	removed: list[tuple[str, str]] = [
		(pkg_id, lock[pkg_id].version)
		for pkg_id in sorted(lock.keys() - fresh.keys())
	]
	version_changed: list[tuple[str, str, str]] = []
	sha_drift: list[tuple[str, str, str]] = []
	signer_drift: list[tuple[str, str, str, str]] = []
	for pkg_id in sorted(lock.keys() & fresh.keys()):
		l_dep = lock[pkg_id]
		f_dep = fresh[pkg_id]
		if l_dep.version != f_dep.version:
			version_changed.append((pkg_id, l_dep.version, f_dep.version))
		if l_dep.sha256 != f_dep.sha256 and (l_dep.sha256 or f_dep.sha256):
			sha_drift.append((pkg_id, l_dep.sha256, f_dep.sha256))
		for field_name in ("author_key", "source_content_id", "source_attestation_key"):
			lv = getattr(l_dep, field_name)
			fv = getattr(f_dep, field_name)
			if lv != fv and (lv or fv):
				signer_drift.append((pkg_id, field_name, lv, fv))
	return SourceRebuildEvidence(
		added=added,
		removed=removed,
		version_changed=version_changed,
		sha_drift=sha_drift,
		signer_drift=signer_drift,
	)


def print_evidence(
	*,
	art_name: str,
	channel: str,
	evidence: SourceRebuildEvidence,
) -> None:
	"""Print evidence to stdout with `channel` prefix.

	`channel` is the caller's label (`"drift prepare --check"`,
	`"drift build"`, `"drift deploy"`) — appears verbatim in the
	leading line so CI log scrapers can attribute the evidence to
	the correct step.
	"""
	if evidence.is_empty():
		return
	print(
		f"{channel} --source-rebuild: artifact '{art_name}' drift "
		f"vs. lock (compile uses fresh graph, not lock):"
	)
	for pkg_id, version in evidence.added:
		print(f"  + {pkg_id}@{version} (new in resolved graph)")
	for pkg_id, version in evidence.removed:
		print(f"  - {pkg_id}@{version} (no longer in resolved graph)")
	for pkg_id, lv, fv in evidence.version_changed:
		print(f"  ~ {pkg_id}: version {lv} -> {fv}")
	for pkg_id, lsha, fsha in evidence.sha_drift:
		print(f"  ~ {pkg_id}: sha256 {lsha!r} -> {fsha!r}")
	last_pkg: str | None = None
	for pkg_id, field_name, lv, fv in evidence.signer_drift:
		if pkg_id != last_pkg:
			print(f"  ~ {pkg_id}:")
			last_pkg = pkg_id
		pad = max(0, 22 - len(field_name))
		print(f"      {field_name}{' ' * pad}  locked={lv!r}  disk={fv!r}")
