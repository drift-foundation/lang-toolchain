# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Single authority for the source-rebuild run graph (consumer side).

Under `--source-rebuild` / `DRIFT_CERT_MODE=certify`, `drift prepare
--check`, `drift build`, and `drift deploy` all need to produce the
SAME dependency graph and the SAME verdict.  Historically the three
callers each had their own mixture of "consult the lock," "verify
against the index," and
"substitute versions in place" — which let contradictions creep in
(e.g. `--check` accepting a new transitive dep as evidence while
`drift build` still compiled against the stale lock).  This module
is the one place consumer source-rebuild semantics live.

Under the 0.31.3 run-snapshot model, source-rebuild consumers
pin source identity per CERTIFICATION RUN via an orch-produced
run snapshot (`tools.drift_deploy.run_snapshot`).  The consumer's
local `drift/trust.json` is NOT consulted for upstream author
verification — orch already verified author trust at staging time
against orch's own trust store, and attested the result through
the snapshot.

Pipeline (identical for all three callers):

    1. Load the orch-produced run snapshot (passed in by the
       caller via `run_snapshot=`).  Required; passing `None`
       raises.  Upstream callers (drift_build / drift_deploy /
       drift_prepare) get the path from `--run-snapshot <path>`
       or `DRIFT_RUN_SNAPSHOT=<path>`.
    2. Build the package index with `run_snapshot=` wired into
       `build_package_index`.  The index builder gates every
       discovered package against the snapshot's entry for
       `(pkg_id, version)`: missing entry or
       `(source_content_id, author_key, source_attestation_key)`
       mismatch is a HARD ERROR (`ResolutionError`).  The bad
       package is not silently pruned — pruning would let the
       resolver fall back to an older snapshot-authorised version
       and silently mask a same-version source swap orch DID NOT
       stage.
    3. Re-resolve the artifact's direct-dep graph against the
       snapshot-gated index using the ordinary `resolve_artifact`
       constraint solver.  The resolver enforces consumer
       manifest ranges (direct deps) and producer `required_deps`
       (transitive deps) — the same rules that apply in strict
       mode.
    4. Structural per-dep gates on the fresh-resolved graph:
       non-co-artifact deps must have `author_key != "unsigned"`
       AND non-empty `source_content_id` /
       `source_attestation_key` on disk.  These catch cases the
       snapshot gate cannot (unsigned packages have no v1 author
       claim and can't be snapshot-gated; a missing v1 cert
       claim yields empty identity at index time but technically
       could match an all-empty snapshot entry, so we reject
       empty identity structurally).
    5. Compare the fresh-resolved graph to the existing lock's
       per-artifact graph.  Any shape or per-field drift becomes
       EVIDENCE (`SourceRebuildEvidence`), not an error — the
       lock is evidence in source-rebuild mode, not a gate.
    6. Return `SourceRebuildResult(resolved_graph, evidence, errors)`.

The caller consumes:

    - `resolved_graph` — the authoritative graph.  Compile
      (drift build / drift deploy) and lock comparison (drift
      prepare --check) use this as the dependency graph.  The
      lock is NEVER consulted as the graph source under source-
      rebuild; it is input only for evidence comparison.
    - `evidence` — informational.  Printed to stdout for CI log
      capture; does not stall any step.
    - `errors` — non-empty means the caller must abort the run.
      These are "hard" failures (unsigned dep, missing
      attestation on disk, resolver failure).  Snapshot
      mismatches surface through `ResolutionError` raised by
      `build_package_index` and are caught by the caller.

Strict mode is UNCHANGED: callers keep consulting the lock as
the authoritative graph and call `verify_lock_compatibility`
directly.  Orch's producer/staging path (a separate flow)
continues to use `build_package_index(trust_store=...)` for
author-trust verification; that path is NOT routed through this
module.  This module is the consumer-only source-rebuild
authority.
"""

from __future__ import annotations

import sys
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
	  * Every entry in `resolved_graph` came from the snapshot-
	    gated package index.  Its `(source_content_id,
	    author_key, source_attestation_key)` triple exact-matches
	    the run snapshot's entry for `(pkg_id, version)`; missing
	    entries and mismatches raise `ResolutionError` at index
	    time (never reach this result).
	  * Author trust is attested by the snapshot, not re-verified
	    here: orch's producer/staging step validated v1 author +
	    cert claim signers against orch's own trust store before
	    writing each snapshot entry.  The consumer
	    DOES NOT re-run that verification, and the consumer's
	    local `drift/trust.json` is NOT consulted.
	  * Every non-co-artifact entry has non-empty
	    `source_content_id` / `source_attestation_key` OR appears
	    in `errors` (structural gate in
	    `apply_structural_trust_gates`).
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
	run_snapshot: Any,
	pkg_index: dict[str, list[PackageEntry]] | None = None,
	co_artifact_entries: dict[str, PackageEntry] | None = None,
	snapshot_exempt_ids: set[str] | None = None,
) -> SourceRebuildResult:
	"""Produce the source-rebuild run graph for one artifact.

	`package_roots` is the list of directories to walk for `.dmp`
	/ `.zdmp` discovery.  `manifest_dir` is used only for
	diagnostics.

	`run_snapshot` is REQUIRED — the v0.31.3 model pins source
	identity per certification run via an orch-produced snapshot
	(see `tools.drift_deploy.run_snapshot`).  Source-rebuild
	consumers MUST be invoked with a loaded `RunSnapshot`; passing
	`None` raises.  The snapshot is the authoritative "what this
	run is certifying" statement — every staged package's
	`(source_content_id, author_key, source_attestation_key)` must
	match its snapshot entry exactly.  The downstream's local
	`drift/trust.json` is NOT consulted for upstream author trust
	(orch already verified signers at staging time against orch's
	own trust store; the result is attested through the snapshot).

	`pkg_index` may be supplied to amortize work across multiple
	artifacts (e.g. `drift prepare --check` iterates the whole
	manifest).  If `None`, this function builds it with the same
	`run_snapshot` gate and the caller-supplied
	`snapshot_exempt_ids`.

	`co_artifact_entries` is an optional map of pkg_id →
	PackageEntry overlays (co-artifacts of the current manifest
	that haven't been built yet).  These take priority over any
	externally-discovered entry with the same pkg_id — the
	resolver will pin to the co-artifact's own version.  Matches
	the pattern used in `drift_prepare.py::_run_impl`.

	`snapshot_exempt_ids` (stage-mode producer-output exemption):
	pkg_ids whose disk packages skip the run-snapshot gate while
	still being indexed.  Only the caller that knows which
	artifacts it is PRODUCING in this deploy invocation should
	populate this (the `drift_deploy._run_impl` call site under
	`DRIFT_CERT_MODE=stage` uses the current manifest's library-
	artifact names).  `DRIFT_CERT_MODE=certify` (pure consumer)
	must pass `None` — certify's contract requires every consumed
	package to be in the snapshot, and allowing exemptions there
	would defeat the gate.

	`existing_lock_graph` is the lock's per-artifact map (or None
	if no lock exists yet).  Used for evidence only; the lock is
	never the graph authority under source-rebuild.
	"""
	if run_snapshot is None:
		raise ValueError(
			"resolve_source_rebuild: `run_snapshot` is required.  "
			"Source-rebuild consumers pin source identity per run "
			"via an orch-produced snapshot; callers must load it "
			"first (tools.drift_deploy.run_snapshot.load_run_snapshot) "
			"and pass the `RunSnapshot` here.  The snapshot replaces "
			"the pre-0.31.3 downstream-trust-store path entirely for "
			"upstream author verification."
		)
	if pkg_index is None:
		pkg_index = build_package_index(
			package_roots,
			run_snapshot=run_snapshot,
			snapshot_exempt_ids=snapshot_exempt_ids,
		)
		# Layer in the caller-supplied co-artifact overlays AFTER
		# the snapshot-gated index is built.  Co-artifacts come from
		# the current manifest's library artifacts; they haven't
		# been signed yet (their `.dmp` is built later in the same
		# deploy run), so they bypass the snapshot gate and must be
		# injected manually.  The co-artifact's own eventual
		# verification happens later in the same deploy run when
		# its `.dmp` is produced.
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
				f"against snapshot-gated package index failed: {e}"
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
	"""Per-dep source-rebuild gates the snapshot check can't catch.

	`build_package_index(run_snapshot=...)` exact-matches each disk
	package's `(source_content_id, author_key,
	source_attestation_key)` against the snapshot entry for
	`(pkg_id, version)`.  It cannot gate on:
	  * `author_key == "unsigned"` — the unsigned dev-opt-in; there's
	    no v1 author claim on disk, and if the snapshot had somehow
	    entered `"unsigned"` as its author_key it would nominally
	    match.  Rejected here because source-rebuild's contract
	    requires real signed artifacts.
	  * empty `source_content_id` / `source_attestation_key` — a
	    missing v1 cert claim sidecar yields empty identity at
	    index time.  A malformed snapshot with empty-string fields
	    is rejected at snapshot load via strict field validation
	    (`run_snapshot.load_run_snapshot`), but this structural
	    check is defence-in-depth against any caller that bypasses
	    the loader (e.g. tests that construct a snapshot
	    programmatically).
	Co-artifact entries (same-manifest libraries built later in the
	same run) are exempt.
	"""
	for pkg_id, dep in resolved.items():
		if dep.dep_type == "co-artifact" or pkg_id in co_artifact_names:
			continue
		# Reject BOTH the dev-opt-in `"unsigned"` sentinel AND an
		# empty `author_key` (no v1 author claim sidecar).  Defence-in-depth
		# for callers that bypass the snapshot-gated index (tests
		# injecting mocked `PackageEntry` objects, older producer
		# paths).  Under the 0.31.3 model the snapshot gate is the
		# primary check, but this secondary check keeps the
		# invariant "no unsigned non-co-artifact reaches the
		# resolved graph" intact even when the primary gate is
		# bypassed.
		if dep.author_key == "unsigned" or not dep.author_key:
			errors.append(
				f"artifact '{art_name}' dep '{pkg_id}': resolved "
				f"entry has no verifiable signer "
				f"(author_key={dep.author_key!r}); source-rebuild "
				f"requires every disk package to have a v1 author "
				f"claim sidecar AND a matching run-snapshot entry.  "
				f"Run `drift author` for the package under a "
				f"kid the snapshot authorises."
			)
			continue
		if not dep.source_content_id:
			errors.append(
				f"artifact '{art_name}' dep '{pkg_id}': resolved "
				f"entry has empty `source_content_id` on disk; "
				f"source-rebuild requires a non-empty v1 cert claim "
				f"(the trust gate has nothing to verify otherwise).  "
				f"Re-run cert-claim emission via `drift-deploy`."
			)
		if not dep.source_attestation_key:
			errors.append(
				f"artifact '{art_name}' dep '{pkg_id}': resolved "
				f"entry has empty `source_attestation_key` on disk; "
				f"source-rebuild requires a non-empty v1 cert-claim "
				f"certifier kid.  Re-run cert-claim emission via "
				f"`drift-deploy`."
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
	out: Any = None,
) -> None:
	"""Print evidence with `channel` prefix.

	`channel` is the caller's label (`"drift prepare --check"`,
	`"drift build"`, `"drift deploy"`) — appears verbatim in the
	leading line so CI log scrapers can attribute the evidence to
	the correct step.

	`out` selects the stream (default stdout, the historical
	behavior).  `drift lock emit --source-rebuild` passes stderr:
	its stdout is a machine contract (exactly the `--dep` flags)
	and evidence is diagnostics there.
	"""
	if evidence.is_empty():
		return
	if out is None:
		out = sys.stdout
	print(
		f"{channel} --source-rebuild: artifact '{art_name}' drift "
		f"vs. lock (compile uses fresh graph, not lock):",
		file=out,
	)
	for pkg_id, version in evidence.added:
		print(f"  + {pkg_id}@{version} (new in resolved graph)", file=out)
	for pkg_id, version in evidence.removed:
		print(f"  - {pkg_id}@{version} (no longer in resolved graph)", file=out)
	for pkg_id, lv, fv in evidence.version_changed:
		print(f"  ~ {pkg_id}: version {lv} -> {fv}", file=out)
	for pkg_id, lsha, fsha in evidence.sha_drift:
		print(f"  ~ {pkg_id}: sha256 {lsha!r} -> {fsha!r}", file=out)
	last_pkg: str | None = None
	for pkg_id, field_name, lv, fv in evidence.signer_drift:
		if pkg_id != last_pkg:
			print(f"  ~ {pkg_id}:", file=out)
			last_pkg = pkg_id
		pad = max(0, 22 - len(field_name))
		print(f"      {field_name}{' ' * pad}  locked={lv!r}  disk={fv!r}", file=out)
