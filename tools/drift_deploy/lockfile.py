# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift/lock.json read / write / verify (schema v4 with v1-trust
semantics in v4 field slots; v5 rename deferred).

The lock records, per artifact, the **exact resolved artifact** for
every dependency in the transitive graph.  Under the v1 trust
model each entry carries:

  - artifact identity: `M.N.P` version + `sha256` of the `.dmp`
    file + `author_key` (NOW SEMANTICALLY: the cert-claim signer
    kid -- the certifier who attested the artifact bytes).  Load-
    bearing for default byte-exact consumption.
  - source identity: `source_content_id` (canonical hash of
    declared source/build inputs; computed by
    `lang.driftc.packages.source_content_id.compute_source_content_id`)
    + `source_attestation_key` (NOW SEMANTICALLY: the author-claim
    signer kid -- the author who attested release intent).  Load-
    bearing for source-rebuild certification.

The v4 field names persist while the SEMANTICS shift: `author_key`
holds a cert kid and `source_attestation_key` holds an author
kid.  A future lockfile-v5 bump renames the slots to `cert_kid`
and `author_kid` respectively.  Diagnostics emitted to users have
already been migrated to v1 terminology.

The authored manifest (drift/manifest.json) carries the owner's
declared acceptable range; the lock is downstream of resolution.
`drift prepare` is the only sanctioned writer.

Verifier modes (`verify_lock_compatibility`):
  - **strict** (default): re-checks `(version, sha256, cert_kid,
    source_content_id, author_kid)` against the on-disk package +
    its v1 sidecars (`<pkg>.author-claim` +
    `<pkg>.cert-claim.<kid>.json`).  Both halves of the identity
    are enforced.
  - **source_rebuild** (Phase D opt-in): re-checks `(version,
    source_content_id, author_kid)` only; tolerates `sha256` and
    `cert_kid` drift because the rebuilt artifact is expected to
    differ in bytes and may have been certified by a different
    certifier.  Per-package sha drift is recorded as run evidence
    (caller-supplied `sha_drift_log` list).  Missing v1 author
    claim on disk is a hard fail with republish-required guidance.

`drift prepare` enforces source identity at write time as well:
non-co-artifact resolved deps without a valid author claim cause
a fail-fast `PrepareError` with republish-required guidance.

Schema history:
- v1 — exact version + `integrity: "sha256:<hex>"` (pre-0.27 era).
- v2 — major.minor range + author_key; sha was discarded.
- v3 — exact M.N.P + sha256 + author_key + dep_type (0.29.0).
- v4 — v3 + source_content_id + source_attestation_key (0.30.0+).
  Field SEMANTICS migrated to v1 trust roles; field names retained.

v1, v2, and v3 locks are rejected at load; `drift prepare`
regenerates as v4.  No silent migration -- a stale lock must not
quietly reinterpret a range entry as an exact pin or pretend a
byte-only-pinned entry has a verified source identity.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.drift_deploy.resolver import ResolvedDep

# Imported for the VERIFY_MODE_SOURCE_REBUILD disk-kid trust gate.
# Callers provide a merged project+core trust store so the verifier
# can assert that the disk's certifier kid (artifact signer) and
# author kid (release-intent signer) are both in the package's
# namespace allowlist under their respective roles, and neither is
# revoked -- this replaces lock-kid equality as the source-rebuild
# trust anchor.
from lang.driftc.packages.trust_v1 import TrustStore


LOCK_SCHEMA_VERSION = 4

# Lock v4 pins every non-co-artifact entry to a fully-qualified
# `M.N.P` version (exact resolved release) — same exact-pin shape
# that landed in v3 and is preserved by v4.  Any other shape —
# a range (`M.N`), a constraint operator (`^0.3.0`), empty string,
# or garbage — is a lock-corruption symptom.  Defensive shape check
# at the loader means build/deploy never have to re-parse.
_EXACT_MNP_RE = re.compile(r"^\d+\.\d+\.\d+$")

# v4 recognises exactly three `dep_type` values (unchanged from v3).
# Anything else in a
# lock file is either a typo, a hand-edit, or a forward-compat value
# from a future schema we haven't cut yet — reject at load so the
# downstream verifier never has to fall back to "unknown-type =
# default direct" behaviour.  In particular, `co-artifact` carries
# a strict structural meaning (same-manifest sibling, built in this
# deploy run, sha/author_key not yet known) — if a lock marks some
# OTHER dep kind as co-artifact we MUST surface that as corruption.
_VALID_DEP_TYPES = frozenset(("direct", "transitive", "co-artifact"))


def write_lock(
	path: Path,
	artifacts: dict[str, dict[str, ResolvedDep]],
) -> None:
	"""Write drift/lock.json (schema v4).

	`artifacts` is {artifact_name → {package_id → ResolvedDep}}.
	Each entry is emitted exactly: version M.N.P + sha256 +
	author_key + source_content_id + source_attestation_key +
	dep_type.  No range field; no file-level integrity.  The map
	key IS the package id; no redundant `package_id` field inside
	the entry.

	Co-artifact entries leave the on-disk-derived fields (`sha256`,
	`author_key`, `source_content_id`, `source_attestation_key`)
	empty; they're filled at deploy time when the co-artifact is
	built.  `verify_lock_compatibility` skips co-artifact entries
	with a fail-closed allowlist on `package_id`.
	"""
	obj: dict[str, Any] = {
		"schema_version": LOCK_SCHEMA_VERSION,
		"artifacts": {},
	}
	for art_name in sorted(artifacts.keys()):
		resolved = artifacts[art_name]
		resolved_obj: dict[str, Any] = {}
		for pkg_id in sorted(resolved.keys()):
			dep = resolved[pkg_id]
			entry: dict[str, Any] = {
				"version": dep.version,
				"sha256": dep.sha256,
				"author_key": dep.author_key,
				"source_content_id": dep.source_content_id,
				"source_attestation_key": dep.source_attestation_key,
				"dep_type": dep.dep_type,
			}
			resolved_obj[pkg_id] = entry
		obj["artifacts"][art_name] = {"resolved": resolved_obj}
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
		encoding="utf-8",
	)


def read_lock(path: Path) -> dict[str, dict[str, ResolvedDep]]:
	"""Read drift/lock.json (schema v4 only).

	v1, v2, and v3 locks are rejected with a pointer to `drift
	prepare`.  Under v4 the lock is downstream of resolution,
	authored only by `drift prepare`, and load-bearing for
	build/deploy strict-exact re-verification across BOTH artifact
	identity (sha256 + author_key) AND source identity
	(source_content_id + source_attestation_key) — accepting older
	shapes here would silently reinterpret owner-declared ranges
	(v2), pre-authorship pins (v1), or byte-only pins (v3) as
	having a verified source identity and bypass the source-rebuild
	trust check.

	Returns {artifact_name → {package_id → ResolvedDep}}.
	"""
	data = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(data, dict):
		raise ValueError("drift/lock.json must be a JSON object")
	sv = data.get("schema_version")
	if sv in (1, 2):
		raise ValueError(
			f"drift/lock.json uses schema v{sv}; v4 is required as of "
			"0.30.0.  Run `drift prepare` to regenerate the lock with "
			"exact resolved versions, sha256 digests, signer keys, and "
			"source-attestation identity.  Older schemas carried ranges "
			"(v2) or byte-only pins (v1) and cannot be safely "
			"reinterpreted as v4 entries."
		)
	if sv == 3:
		raise ValueError(
			"drift/lock.json uses schema v3 (0.29.x); v4 is required as "
			"of 0.30.0.  Run `drift prepare` to regenerate the lock — "
			"v4 adds `source_content_id` and `source_attestation_key` "
			"per dep so source-rebuild certification has a signed source "
			"identity to verify against, independent of artifact-byte "
			"sha256.  Silently treating a v3 lock as v4 would let a "
			"rebuilt artifact pass the source-identity check despite "
			"the lock having no recorded source identity to compare."
		)
	if sv != LOCK_SCHEMA_VERSION:
		raise ValueError(
			f"unsupported drift/lock.json schema_version: {sv} "
			f"(expected {LOCK_SCHEMA_VERSION}; run `drift prepare` to regenerate)"
		)
	artifacts_obj = data.get("artifacts")
	if not isinstance(artifacts_obj, dict):
		raise ValueError("drift/lock.json missing 'artifacts' object")

	result: dict[str, dict[str, ResolvedDep]] = {}
	for art_name, art_data in artifacts_obj.items():
		if not isinstance(art_data, dict):
			raise ValueError(f"drift/lock.json artifact '{art_name}' must be an object")
		resolved_obj = art_data.get("resolved")
		if not isinstance(resolved_obj, dict):
			raise ValueError(f"drift/lock.json artifact '{art_name}' missing 'resolved' object")
		resolved: dict[str, ResolvedDep] = {}
		for pkg_id, dep_data in resolved_obj.items():
			if not isinstance(dep_data, dict):
				raise ValueError(f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' must be an object")
			version = dep_data.get("version")
			if not isinstance(version, str) or not version:
				raise ValueError(
					f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
					f"missing 'version' (exact M.N.P required in v4)"
				)
			dep_type = dep_data.get("dep_type", "direct")
			if dep_type not in _VALID_DEP_TYPES:
				raise ValueError(
					f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
					f"has invalid dep_type '{dep_type}' — v4 recognises "
					f"only {sorted(_VALID_DEP_TYPES)}; run `drift prepare` "
					f"to regenerate"
				)
			# v4 pins an exact `M.N.P` for every entry, including
			# co-artifacts (their .dmp is built in the same deploy
			# run but pinned at the manifest's exact release version).
			# Reject any range shape or constraint operator here so
			# build/deploy never have to second-guess the lock.
			if not _EXACT_MNP_RE.match(version):
				raise ValueError(
					f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
					f"version '{version}' is not an exact M.N.P pin — v4 "
					f"only stores fully-resolved versions (a range or "
					f"constraint here means the lock was hand-edited or "
					f"left over from an older schema).  Run `drift "
					f"prepare` to regenerate."
				)
			dep_sha = dep_data.get("sha256", "")
			dep_author_key = dep_data.get("author_key", "")
			dep_scid = dep_data.get("source_content_id", "")
			dep_sak = dep_data.get("source_attestation_key", "")
			if dep_type != "co-artifact":
				if not isinstance(dep_sha, str) or not dep_sha:
					raise ValueError(
						f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
						f"missing 'sha256' — v4 requires exact artifact "
						f"digests for every non-co-artifact dep"
					)
				if not isinstance(dep_author_key, str) or not dep_author_key:
					raise ValueError(
						f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
						f"missing 'author_key' — packages must be signed "
						f"before locking; run `drift prepare` after signing"
					)
				# v4: source identity fields are required for SIGNED
				# non-co-artifact entries.  The unsigned dev opt-in
				# (`author_key == "unsigned"`) propagates: an unsigned
				# package has no v1 author/cert claim sidecars (signing
				# infra governs both halves of the v4 identity), so
				# both `source_content_id` and `source_attestation_key`
				# are also allowed to be empty for those entries.  The
				# verifier honors the same rule end-to-end.
				#
				# For SIGNED entries, empty source identity would let
				# source-rebuild mode silently accept any rebuilt
				# artifact at this `(name, version)` because there'd
				# be nothing to verify against — the trust root would
				# collapse to "trust the rebuilder," which is exactly
				# what source-mode exists to prevent.  Re-`drift
				# prepare` is the path to refresh a v3 lock or one
				# whose attestation sidecars are missing on disk.
				if dep_author_key != "unsigned":
					from lang.driftc.packages.source_content_id import (
						validate_sci as validate_sha256_hex_id,
					)
					try:
						validate_sha256_hex_id(
							dep_scid,
							field=f"drift/lock.json artifact '{art_name}' dep "
							f"'{pkg_id}' source_content_id",
						)
					except ValueError as e:
						raise ValueError(
							f"{e}.  v4 lock requires a strict-shape source "
							f"identity for every signed non-co-artifact dep; "
							f"the package must be republished with a v1 author "
							f"claim (`drift-author publish`), then `drift "
							f"prepare` re-run."
						) from None
					if not isinstance(dep_sak, str) or not dep_sak.startswith("ed25519:"):
						raise ValueError(
							f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
							f"missing 'source_attestation_key' (expected "
							f"'ed25519:<kid>') -- v4 records the v1 author-claim "
							f"signer kid here as the source-identity trust root.  "
							f"Republish the package with `drift-author publish` "
							f"and re-run `drift prepare`."
						)
			resolved[pkg_id] = ResolvedDep(
				version=version,
				sha256=dep_sha if isinstance(dep_sha, str) else "",
				dep_type=dep_type,
				package_id=pkg_id,
				author_key=dep_author_key if isinstance(dep_author_key, str) else "",
				source_content_id=dep_scid if isinstance(dep_scid, str) else "",
				source_attestation_key=dep_sak if isinstance(dep_sak, str) else "",
			)
		result[art_name] = resolved
	return result


# ──────────────────────────────────────────────────────────────────
# Two verify modes, one trust root.
#
#   Trust root (both modes):
#     namespace owner-continuity via trust store — the installed
#     package's author-claim signer kid AND cert-claim signer kid
#     must each be in the trust store's namespace allowlist (in the
#     right role: authors / certifiers) for every `module_id` the
#     package declares, AND neither may be revoked, AND each claim
#     must cryptographically verify against the trust store's
#     pubkey.  Primary gate lives at index time in
#     `tools.drift_deploy.resolver.build_package_index(trust_store=...)`
#     — which hard-fails (raises `ResolutionError`) on any trust-
#     verification failure, rather than silently pruning.  Source-
#     rebuild `drift build` / `drift deploy` / `drift prepare
#     --check` all go through that boundary via
#     `tools.drift_deploy.source_rebuild.resolve_source_rebuild`,
#     which is the single authority for source-rebuild runs.
#     Strict mode inherits the same trust root through lock
#     equality: a strict lock was written by a `drift prepare` that
#     loaded its trust store and rejected untrusted kids at write
#     time, so disk-kid equality with a known-good lock kid is
#     sufficient — `verify_lock_compatibility` then only runs in
#     strict mode for byte-exact lock verification.
#
#   Strict mode (used by `drift build` / `drift deploy` default, and
#   `drift prepare --check` default):
#     equality on every recorded lock field — the lock is the
#     authoritative "what this repo committed to consume"
#     statement, and any drift is a verification failure.
#     `verify_lock_compatibility` is the verifier for this mode.
#
#   Source-rebuild mode (used by `drift build --source-rebuild` /
#   `drift deploy --source-rebuild` / `drift prepare --check
#   --source-rebuild`):
#     * the lock is EVIDENCE, not an authority.  The compile / lock-
#       compare graph is the fresh-resolve output produced by
#       `resolve_source_rebuild` against a trust-verified package
#       index — `verify_lock_compatibility` is NOT invoked in this
#       mode.
#     * lock-side `sha256`, `author_key`, `source_content_id`,
#       `source_attestation_key` are reported as evidence when they
#       differ from disk, NOT treated as failure — the lock records
#       the downstream repo's last-prepared state, not the currently
#       selected rebuild inputs.
#     * the disk-side trust gate (cryptographic v1 author + cert
#       claim verify + per-module-namespace allowlist + non-
#       revocation for both signer kids) runs at index time via
#       `build_package_index(trust_store=...)` → fail-fast
#       `ResolutionError` on any violation.
#     * structural per-dep gates (unsigned reject, empty source
#       identity reject) run via `apply_structural_trust_gates`
#       in the authority.
#     * consumer manifest range satisfaction for selected versions
#       is enforced by `resolve_artifact` against `.dmp`-declared
#       producer `required_deps`.  Version / dep-set drift between
#       the lock and the fresh graph is evidence.
#
# DO NOT reintroduce `source_content_id` equality as a hard gate
# in source-rebuild mode.  See `docs/history.md` 2026-04-21 for
# the bug this prevents (orch-selected source graph stale-locked
# by downstream repos whose `drift prepare` had not yet caught up
# to a compatible upstream patch).  The operational consequence of
# that mistake was every upstream patch staling every downstream
# repo's lock, forcing every consumer to re-prepare before orch
# could certify — exactly the churn lock v2 was designed to
# eliminate.  The disk-side trust-store gate added in 0.31.1
# replaces lock equality with something stronger: owner-namespace
# allowlist verification of whoever signed the package that's
# actually on disk right now.
# ──────────────────────────────────────────────────────────────────

VERIFY_MODE_STRICT = "strict"
"""Default consumption mode.

Pins every axis of the lock as exact equality — the lock is the
authoritative statement of what this repo committed to consume, and
any drift from it is a verification failure.

**Hard gates** (all lock fields equality-checked, plus the inherent
trust/signature requirements that also gate
`VERIFY_MODE_SOURCE_REBUILD`):

- exact `M.N.P` version match with lock
- exact `sha256` match with lock
- exact `author_key` kid match with lock
- exact `source_content_id` match with lock
- exact `source_attestation_key` kid match with lock
- installed `.dmp` is covered by a v1 author claim that verifies
- installed `.dmp` is covered by a v1 cert claim that verifies
- installed author-claim signer kid is trusted (authors role) for
  the package namespace
- installed cert-claim signer kid is trusted (certifiers role) for
  the package namespace
- neither installed signer kid is revoked
- package is not unsigned

No evidence bucket — every relaxation is a deliberate choice made
by the source-rebuild mode.  The two modes share a trust root
(namespace-allowlist owner continuity) and diverge only on which
lock fields participate as equality pins vs evidence."""

VERIFY_MODE_SOURCE_REBUILD = "source_rebuild"
"""Certification / source-from-commit mode.

HISTORICAL: this constant was the mode selector for
`verify_lock_compatibility`'s source-rebuild branch.  Under the
0.31.1 resolve-driven model, `verify_lock_compatibility` is ONLY
called in strict mode.  Source-rebuild `drift build` / `drift
deploy` / `drift prepare --check` all go through
`tools.drift_deploy.source_rebuild.resolve_source_rebuild`, which
uses `tools.drift_deploy.resolver.build_package_index(trust_store=
...)` as the fail-fast trust boundary and exposes a typed
`SourceRebuildResult(resolved_graph, evidence, errors)`.  The
hard-gate list below describes the source-rebuild CONTRACT (what
must be true for the run to pass), not the `verify_lock_
compatibility` call path.

**Hard gates** (enforced in `build_package_index(trust_store=...)`
+ `source_rebuild.apply_structural_trust_gates`):

- selected package version satisfies the consumer manifest range
  (enforced at resolver time — if the graph resolved, ranges are
  satisfied structurally)
- resolved graph satisfies producer `required_deps` during
  resolution (same resolver-time constraint)
- every on-disk package has a `<pkg>.author-claim` sidecar (empty
  `author_key` is a hard error — source-rebuild's trust root has
  nothing to verify against an unsigned disk package)
- every author claim cryptographically verifies against the trust
  store's pubkeys AND every module_id the package declares maps
  to an allowlisted author kid for that namespace
- every `<pkg>.cert-claim.<kid>.json` sidecar is present,
  structurally valid, cross-bound to the `.dmp` artifact, and
  self-verifies; its recorded certifier kid also passes the
  namespace-allowlist (certifiers role) + non-revocation check at
  index time
- neither signer kid (author or certifier) is in the trust store's
  revoked_kids set (core trust store OR project trust store)
- no co-artifact status on externally-discovered packages (co-
  artifact is only valid for same-manifest library siblings)

**Evidence only** (reported via `SourceRebuildEvidence`, does not
fail the verification):

- `sha256` drift vs. lock
- `author_key` kid drift vs. lock
- `source_content_id` drift vs. lock
- `source_attestation_key` kid drift vs. lock
- `version` drift vs. lock (resolver picked a different in-range
  version than the lock recorded)
- dep-set drift vs. lock (transitive added / removed)

The trust anchor is namespace owner-continuity through the trust
store, not literal equality with the lock's recorded signer kids.
The lock's recorded kids are stale evidence; the disk's kids are
verified live.  If an exact-source rebuild is needed, pin the
desired source commits in the source selection file (`run-all-
latest.json` or equivalent) or use strict mode.

**Bug this mode was created to prevent** (see `docs/history.md`
2026-04-21): the 0.30.0 source-rebuild implementation required
`source_content_id` equality with the downstream lock as a hard
gate.  That check presumed "the downstream lock is the source
graph orch wants to rebuild," which is wrong — orch selects the
source graph via its own pinning mechanism, and the downstream
lock is just the last state that downstream repo prepared against.
The equality requirement meant every upstream compatible patch
(even tooling-only commits) staled every downstream's lock and
required a re-`drift prepare` + commit + PR + pin bump before orch
could certify — the exact churn lock v2 was designed to avoid.
This mode now correctly trusts orch's source selection and
verifies trust through the owner-namespace allowlist, not through
downstream lock equality."""

_VERIFY_MODES = frozenset((VERIFY_MODE_STRICT, VERIFY_MODE_SOURCE_REBUILD))


def verify_lock_compatibility(
	lock_deps: dict[str, ResolvedDep],
	package_index: dict[str, list],
	*,
	allowed_co_artifacts: set[str] | None = None,
	mode: str = VERIFY_MODE_STRICT,
	sha_drift_log: list[tuple[str, str, str]] | None = None,
	signer_drift_log: list[tuple[str, str, str, str]] | None = None,
	trust_store: TrustStore | None = None,
) -> list[str]:
	"""Verify a v4 lock against the current package index.

	See the `VERIFY_MODE_STRICT` and `VERIFY_MODE_SOURCE_REBUILD`
	module-level docstrings for the full contract.

	`allowed_co_artifacts` names the library artifacts declared in
	the caller's current manifest that MAY be marked `dep_type
	"co-artifact"` in the lock (they are built in this same deploy
	run, so their sha256 / author_key / source identity is
	intentionally not yet known).  A lock entry whose `dep_type ==
	"co-artifact"` but whose `pkg_id` is NOT in this set is treated
	as a verification bypass attempt (hand-edited or malformed lock)
	and rejected — an external dependency cannot legitimately claim
	co-artifact status to skip the re-checks.  Passing `None`
	preserves the historical "trust the lock" behaviour; new call
	sites pass the actual allowlist.

	`sha_drift_log`, when provided, is appended with
	`(pkg_id, locked_sha, disk_sha)` for every per-package sha256
	disagreement in source-rebuild mode.  `signer_drift_log`, when
	provided, is appended with `(pkg_id, field_name, locked_kid,
	disk_kid)` for every per-package signer / source-identity
	disagreement in source-rebuild mode (field_name is one of
	`"author_key"`, `"source_content_id"`,
	`"source_attestation_key"`).  Callers surface these as run
	evidence; they are NOT verification failures.  Strict mode
	never appends to either log (a mismatch on any of those axes
	is a hard error there).

	`trust_store` is REQUIRED when `mode == VERIFY_MODE_SOURCE_
	REBUILD` — source-rebuild replaces lock-kid equality with a
	live namespace-allowlist + non-revoked check against the disk's
	author-claim signer and cert-claim signer.  `build_package_
	index` does not consult the trust store, so the verifier cannot
	assume an earlier pass has already rejected untrusted kids.
	Passing `None` in source-rebuild raises `ValueError` to fail
	fast rather than silently skip the gate.  Strict mode ignores
	`trust_store` (equality with a lock written by a trust-store-
	aware `drift prepare` is the gate there).

	Returns a list of error messages (empty = all good).
	"""
	if mode not in _VERIFY_MODES:
		raise ValueError(
			f"unknown verify_lock_compatibility mode {mode!r}; "
			f"expected one of {sorted(_VERIFY_MODES)}"
		)
	if mode == VERIFY_MODE_SOURCE_REBUILD and trust_store is None:
		# Fail fast: the disk-kid gate is the trust anchor for this
		# mode; a caller that didn't load a trust store is indicating
		# (probably by accident) that they want to run source-rebuild
		# verification without verifying trust.  Silently accepting
		# any kid would defeat the whole point.
		raise ValueError(
			"verify_lock_compatibility mode=VERIFY_MODE_SOURCE_REBUILD "
			"requires a `trust_store` — the disk-side author-claim and "
			"cert-claim signer kids must each be verified "
			"against the trust store's namespace allowlist (not revoked), "
			"and this verifier cannot delegate that check to any earlier "
			"pass.  Callers should load the merged project+core trust "
			"store (see `tools.drift_deploy.trust_loader.load_merged_"
			"trust_store`) and pass it as `trust_store=`."
		)
	errors: list[str] = []
	for pkg_id, dep in lock_deps.items():
		if dep.dep_type == "co-artifact":
			# Fail-closed: the bypass only applies when the caller
			# has named this package as a legitimate co-artifact.
			# Anything else masquerading as a co-artifact is either
			# a hand-edited lock or a `drift prepare` bug — treat as
			# corruption, not "just skip".
			if allowed_co_artifacts is not None and pkg_id not in allowed_co_artifacts:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' is "
					f"marked `dep_type: \"co-artifact\"` but '{pkg_id}' "
					f"is not a co-artifact in the current manifest.  "
					f"Only same-manifest library artifacts may use the "
					f"co-artifact dep_type (which skips sha/signer "
					f"re-check because those are not yet known at "
					f"prepare time).  Run `drift prepare` to "
					f"regenerate the lock."
				)
			continue
		entries = package_index.get(pkg_id, [])
		if not entries:
			errors.append(
				f"locked dependency '{pkg_id}@{dep.version}' not found "
				f"under package roots; install the pinned version or "
				f"run `drift prepare` to refresh the lock against "
				f"what is currently available"
			)
			continue
		# Exact version match required.  In strict mode the lock is
		# the pin; in source-rebuild mode the caller already
		# produced `lock_deps` via a fresh `resolve_artifact` call
		# against the same `package_index`, so every version here
		# is necessarily on disk by construction.  We do NOT do an
		# in-range fallback inside the verifier — the resolve-
		# driven source-rebuild model means either:
		#   (a) the resolver found a satisfying version and it's in
		#       the index (nothing to fall back to), or
		#   (b) the resolver failed and the caller raised before
		#       reaching this verifier.
		# A verifier-level fallback would duplicate resolver logic
		# and leave the "which version gets compiled" decision
		# split across two layers — the exact ambiguity the 0.31.1
		# alignment is meant to eliminate.
		exact_matches = [e for e in entries if str(e.version) == dep.version]
		if not exact_matches:
			available = sorted({str(e.version) for e in entries})
			errors.append(
				f"locked dependency '{pkg_id}' pins version '{dep.version}' "
				f"but available versions under package roots are: "
				f"{', '.join(available)}; run `drift prepare` to refresh "
				f"the lock or reinstall the pinned version."
			)
			continue
		# Multiple disk entries at the exact version would be a
		# package-root duplicate issue resolved upstream by
		# `build_package_index`; if we see >1 here, pick the first.
		disk = exact_matches[0]

		# ── Unsigned dev opt-in incompatible with source-rebuild ──
		# Source-rebuild mode REQUIRES v1 author + cert claims as its
		# trust root.  Unsigned packages have neither sidecar, so
		# there is nothing to verify against — hard fail.  This is
		# the only place the unsigned opt-in is rejected outright.
		if dep.author_key == "unsigned" and mode == VERIFY_MODE_SOURCE_REBUILD:
			errors.append(
				f"locked dependency '{pkg_id}@{dep.version}' is marked "
				f"`author_key: \"unsigned\"`, but source-rebuild mode "
				f"requires v1 author + cert claims as the trust "
				f"root.  Unsigned packages have no v1 claim sidecars "
				f"to verify against; run `drift-author publish` and "
				f"`drift-deploy` cert-claim emit before using "
				f"source-rebuild certification on this dep."
			)
			continue

		# ── Artifact-byte half ──
		# Strict mode: sha256 is ENFORCED for every non-co-artifact
		# entry, including unsigned.  The unsigned opt-in skips the
		# artifact SIGNATURE / source ATTESTATION (no signing key,
		# no sidecars), but it does NOT bypass byte identity — a
		# stale or replaced unsigned package in a package root must
		# still be caught.  Otherwise the "unsigned" escape hatch
		# would become a general integrity bypass for byte identity.
		# Source-rebuild mode: record the sha drift as run evidence
		# and skip the comparison; the rebuilt artifact is expected
		# to differ in bytes, and the trust root is the source-
		# identity half checked below.
		if mode == VERIFY_MODE_STRICT:
			if not dep.sha256:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' has empty "
					f"sha256 in the lock; run `drift prepare` to regenerate "
					f"(non-co-artifact entries require a digest)"
				)
				continue
			if not disk.sha256:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' on-disk "
					f"package has empty sha256 (package index did not "
					f"compute a digest — check `build_package_index`); "
					f"cannot verify artifact identity"
				)
				continue
			if dep.sha256 != disk.sha256:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' sha256 "
					f"mismatch:\n"
					f"  locked:   {dep.sha256}\n"
					f"  on-disk:  {disk.sha256}\n"
					f"  the artifact was rebuilt or replaced; run "
					f"`drift prepare` to refresh the lock"
				)
				continue
		else:
			if dep.sha256 and disk.sha256 and dep.sha256 != disk.sha256:
				if sha_drift_log is not None:
					sha_drift_log.append((pkg_id, dep.sha256, disk.sha256))

		# ── Artifact-signer + source-identity halves ──
		# The unsigned dev opt-in skips both: unsigned packages have
		# no v1 author claim (so author_key on disk is empty) and no
		# v1 cert claim (so source identity is empty).  Byte identity
		# (above) is still enforced; this branch governs the
		# SIGNATURE-anchored checks only.
		if dep.author_key == "unsigned":
			continue

		# Strict mode: artifact-signer must match the lock.
		# Source-rebuild mode: kid drift is evidence — the trust
		# anchor is namespace owner-continuity enforced at package-
		# index time via the v1 author + cert claim verifier in
		# `provider_v1` / `verify_v1.compose_verify`, not lock
		# equality.  The only hard gate here for source-rebuild is
		# "package must be signed on disk" (unsigned is already
		# rejected for source-rebuild earlier in this loop).
		if mode == VERIFY_MODE_STRICT:
			if not disk.author_key:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' is "
					f"unsigned in the current package root (lock expects "
					f"signer {dep.author_key})"
				)
				continue
			if dep.author_key != disk.author_key:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' signing "
					f"key changed\n"
					f"  locked:   {dep.author_key}\n"
					f"  on-disk:  {disk.author_key}\n"
					f"  run `drift prepare` to accept the new key"
				)
				continue
		else:
			if not disk.author_key:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' is "
					f"unsigned in the current package root; source-"
					f"rebuild mode requires signed packages — its "
					f"trust root is the namespace allowlist, which "
					f"has nothing to verify against an unsigned "
					f"artifact.  Sign and republish (toolchain >= "
					f"0.30.0) before using source-rebuild on this "
					f"dep."
				)
				continue
			# Disk-kid trust gate (source-rebuild): defence-in-depth
			# against a caller that passed `build_package_index(trust_
			# store=None)` before reaching here.  The PRIMARY gate is
			# `build_package_index` + `provider_v1` /
			# `verify_v1.compose_verify`, which cryptographically
			# verifies each v1 author + cert claim AND enforces the
			# per-module-namespace allowlist using `module_id` from
			# the manifest (NOT the package id — hyphenated ids like
			# `net-tls` are never valid module namespaces).  Call
			# sites in source-rebuild mode
			# (`drift build --source-rebuild` / `drift deploy` /
			# `drift prepare --check --source-rebuild`) all wire
			# `trust_store=` into `build_package_index`.
			#
			# This check uses `disk.module_ids` (populated when the
			# trust-aware index path ran).  When `disk.module_ids` is
			# empty, the defence-in-depth gate is a no-op — the
			# primary gate either didn't run (parse-only index) or
			# the call site is a test fixture that mocked
			# `build_package_index`.  Skipping the gate in that case
			# is intentional: a loud false-positive would block every
			# legitimate test, and the primary gate is the load-
			# bearing one in production.
			assert trust_store is not None  # enforced above
			disk_module_ids = getattr(disk, "module_ids", ()) or ()
			disk_kid_rejected = False
			# `disk.author_key` now sources from the v1 cert-claim
			# sidecar (per resolver._read_author_key); the trust
			# check therefore routes to the CERTIFIER role.
			if disk_module_ids:
				for mid in disk_module_ids:
					allowed = trust_store.allowed_certifiers_for_module(mid)
					if disk.author_key not in allowed:
						errors.append(
							f"locked dependency '{pkg_id}@{dep.version}' "
							f"disk certifier kid {disk.author_key!r} is not "
							f"in the trust store's certifier allowlist "
							f"for module '{mid}'.  Update trust store to "
							f"authorise the kid as a certifier for "
							f"'{mid}.*', or republish under an already-"
							f"trusted certifier kid."
						)
						disk_kid_rejected = True
						break
				if not disk_kid_rejected and disk.author_key in trust_store.revoked_kids:
					errors.append(
						f"locked dependency '{pkg_id}@{dep.version}' "
						f"disk certifier kid {disk.author_key!r} is "
						f"REVOKED in the current trust store.  "
						f"Republish under a non-revoked certifier kid."
					)
					disk_kid_rejected = True
			elif trust_store.allowed_certifiers_for_module(pkg_id):
				# No module_ids on disk (parse-only index path) BUT
				# the caller's trust store has an explicit allowlist
				# entry keyed by pkg_id.  This is the fallback for
				# test fixtures that configure trust via pkg_id --
				# verify there, but do not fail for "no namespace
				# match" since pkg_id may not be a valid module
				# namespace.
				allowed = trust_store.allowed_certifiers_for_module(pkg_id)
				if disk.author_key not in allowed:
					errors.append(
						f"locked dependency '{pkg_id}@{dep.version}' "
						f"disk certifier kid {disk.author_key!r} is not "
						f"in the trust store's certifier allowlist "
						f"for '{pkg_id}'.  Update trust store or "
						f"republish under an already-trusted certifier kid."
					)
					disk_kid_rejected = True
				elif disk.author_key in trust_store.revoked_kids:
					errors.append(
						f"locked dependency '{pkg_id}@{dep.version}' "
						f"disk certifier kid {disk.author_key!r} is "
						f"REVOKED in the current trust store."
					)
					disk_kid_rejected = True
			if disk_kid_rejected:
				continue
			if dep.author_key and dep.author_key != disk.author_key:
				if signer_drift_log is not None:
					signer_drift_log.append((pkg_id, "author_key", dep.author_key, disk.author_key))

		# Source-identity half.
		#
		# Strict mode: both `source_content_id` and
		# `source_attestation_key` must equality-match the lock, and
		# the disk sidecar must be present / valid.  The lock is
		# the trust anchor; equality is the gate.
		#
		# Source-rebuild mode: the disk sidecar must be present /
		# valid (hard gate — "installed v1 author claim verifies"
		# per the `VERIFY_MODE_SOURCE_REBUILD` docstring), but
		# equality with the lock's recorded `source_content_id` and
		# `source_attestation_key` is NOT a gate.  Drift is
		# evidence.  Trust comes from index-time namespace allowlist
		# verification (same mechanism as the artifact-signer half
		# above).
		if not disk.source_content_id or not disk.source_attestation_key:
			if mode == VERIFY_MODE_SOURCE_REBUILD:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' has no "
					f"valid v1 author claim on disk -- source-rebuild "
					f"mode requires the `<pkg>.author-claim` sidecar to "
					f"be present, well-formed, and cross-bound to the "
					f"package's manifest stamps (the installed package "
					f"must be verifiable as authored by a trusted owner).  "
					f"Re-run `drift-author publish` for the package "
					f"(or reinstall if the sidecar was lost), then retry.  "
					f"Source-rebuild does NOT silently fall back to "
					f"byte-only verification."
				)
			else:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' has no "
					f"valid v1 author claim on disk (sidecar missing, "
					f"unbound, or fails cross-binding to the package "
					f"manifest); run `drift prepare` to refresh against "
					f"current packages or republish via `drift-author "
					f"publish`."
				)
			continue
		if mode == VERIFY_MODE_STRICT:
			if dep.source_content_id != disk.source_content_id:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' source_content_id "
					f"mismatch:\n"
					f"  locked:   {dep.source_content_id}\n"
					f"  on-disk:  {disk.source_content_id}\n"
					f"  the package was rebuilt from different source; run "
					f"`drift prepare` to refresh the lock"
				)
				continue
			if dep.source_attestation_key != disk.source_attestation_key:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' "
					f"author kid changed\n"
					f"  locked:   {dep.source_attestation_key}\n"
					f"  on-disk:  {disk.source_attestation_key}\n"
					f"  the v1 author claim was re-signed by a different "
					f"key; run `drift prepare` to accept the new author "
					f"kid"
				)
				continue
		else:
			# Disk author-claim signer trust gate -- same defence-
			# in-depth pattern as the artifact-signer gate above.
			# In v1 the primary cryptographic verification happens
			# at the consumer load path via
			# `provider_v1.load_package_v1_with_policy` (which
			# invokes the full `verify_v1.compose_verify` per
			# module).  `_read_source_attestation_meta` here only
			# LOADS the v1 author claim and cross-binds shape
			# (package_id / version / SCI agreement with the
			# manifest); it does NOT verify the signature.  This
			# layer adds the by-namespace allowlist check (per
			# module_id when available, else by pkg_id if the trust
			# store was configured that way), so a kid that loads
			# fine but isn't authorised for the namespace is
			# rejected here.
			assert trust_store is not None  # enforced at function top
			disk_module_ids = getattr(disk, "module_ids", ()) or ()
			sak_rejected = False
			# `disk.source_attestation_key` now sources from the v1
			# author-claim sidecar (per resolver._read_source_attestation_meta);
			# the trust check therefore routes to the AUTHOR role.
			if disk_module_ids:
				for mid in disk_module_ids:
					allowed = trust_store.allowed_authors_for_module(mid)
					if disk.source_attestation_key not in allowed:
						errors.append(
							f"locked dependency '{pkg_id}@{dep.version}' "
							f"disk author kid "
							f"{disk.source_attestation_key!r} is not in "
							f"the trust store's author allowlist for "
							f"module '{mid}'.  The author claim was "
							f"signed by a kid the trust store does not "
							f"authorise for this namespace; accepting it "
							f"would defeat the owner-continuity trust "
							f"root.  Update trust store or republish "
							f"under an authorised author kid."
						)
						sak_rejected = True
						break
				if not sak_rejected and disk.source_attestation_key in trust_store.revoked_kids:
					errors.append(
						f"locked dependency '{pkg_id}@{dep.version}' "
						f"disk author kid "
						f"{disk.source_attestation_key!r} is REVOKED.  "
						f"Re-run `drift-author publish` under a "
						f"non-revoked kid."
					)
					sak_rejected = True
			elif trust_store.allowed_authors_for_module(pkg_id):
				allowed = trust_store.allowed_authors_for_module(pkg_id)
				if disk.source_attestation_key not in allowed:
					errors.append(
						f"locked dependency '{pkg_id}@{dep.version}' "
						f"disk author kid "
						f"{disk.source_attestation_key!r} is not in "
						f"the trust store's author allowlist for "
						f"'{pkg_id}'."
					)
					sak_rejected = True
				elif disk.source_attestation_key in trust_store.revoked_kids:
					errors.append(
						f"locked dependency '{pkg_id}@{dep.version}' "
						f"disk author kid "
						f"{disk.source_attestation_key!r} is REVOKED."
					)
					sak_rejected = True
			if sak_rejected:
				continue
			if dep.source_content_id and dep.source_content_id != disk.source_content_id:
				if signer_drift_log is not None:
					signer_drift_log.append((pkg_id, "source_content_id", dep.source_content_id, disk.source_content_id))
			if dep.source_attestation_key and dep.source_attestation_key != disk.source_attestation_key:
				if signer_drift_log is not None:
					signer_drift_log.append((pkg_id, "source_attestation_key", dep.source_attestation_key, disk.source_attestation_key))
	return errors


def expand_to_dep_flags(resolved: dict[str, ResolvedDep]) -> list[str]:
	"""Expand a v4-locked resolved graph into exact --dep flags for driftc.

	Returns ["--dep", "net.tls@0.3.15", "--dep", "acme.crypto@0.9.0", ...].
	Order is deterministic (sorted by package id).  `driftc` stays a
	flat exact loader — it never sees ranges.
	"""
	flags: list[str] = []
	for pkg_id in sorted(resolved.keys()):
		dep = resolved[pkg_id]
		flags.extend(["--dep", f"{pkg_id}@{dep.version}"])
	return flags
