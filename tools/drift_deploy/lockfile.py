# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift/lock.json read / write / verify (schema v4).

The lock records, per artifact, the **exact resolved artifact** for
every dependency in the transitive graph.  Under the v4
two-identity model each entry carries:

  - artifact identity: `M.N.P` version + `sha256` of the `.dmp`
    file + `author_key` (signer kid).  Load-bearing for default
    byte-exact consumption.
  - source identity: `source_content_id` (canonical hash of
    declared source/build inputs, see
    `tools.drift_deploy.source_attestation.compute_source_content_id`)
    + `source_attestation_key` (kid that signed the
    `.source-attestation` body).  Load-bearing for source-rebuild
    certification, where the rebuilt `.dmp` bytes legitimately
    differ from the author's original artifact but the source the
    rebuild was made from must match what the owner attested.

The authored manifest (drift/manifest.json) carries the owner's
declared acceptable range; the lock is downstream of resolution.
`drift prepare` is the only sanctioned writer.

Verifier modes (`verify_lock_compatibility`):
  - **strict** (default): re-checks `(version, sha256, author_key,
    source_content_id, source_attestation_key)` against the
    on-disk package + its `.source-attestation` sidecar.  Both
    halves of the v4 identity are enforced.
  - **source_rebuild** (Phase D opt-in): re-checks `(version,
    source_content_id, source_attestation_key)` only; tolerates
    `sha256` and `author_key` drift because the rebuilt artifact
    is expected to differ in bytes and may have been signed by
    a different key.  Per-package sha drift is recorded as run
    evidence (caller-supplied `sha_drift_log` list).  Missing
    source attestation on disk is a hard fail with republish-
    required guidance — no silent fallback to strict.

`drift prepare` enforces source identity at write time as well:
non-co-artifact resolved deps without a valid attestation cause
a fail-fast `PrepareError` with republish-required guidance, so
v4 locks on disk are guaranteed to carry signed, cross-bound
source identity.

Schema history:
- v1 — exact version + `integrity: "sha256:<hex>"` (pre-0.27 era).
- v2 — major.minor range + author_key; sha was discarded.
- v3 — exact M.N.P + sha256 + author_key + dep_type (0.29.0).
- v4 — v3 + source_content_id + source_attestation_key (0.30.0+).

v1, v2, and v3 locks are rejected at load; `drift prepare`
regenerates as v4.  No silent migration — a stale lock must not
quietly reinterpret a range entry as an exact pin or pretend a
byte-only-pinned entry has a verified source identity.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.drift_deploy.resolver import ResolvedDep


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
				# package has no `.source-attestation` sidecar (signing
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
					from tools.drift_deploy.source_attestation import (
						validate_sha256_hex_id,
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
							f"the package must be republished with toolchain "
							f">= 0.30.0 (so its `.source-attestation` sidecar "
							f"exists), then `drift prepare` re-run."
						) from None
					if not isinstance(dep_sak, str) or not dep_sak.startswith("ed25519:"):
						raise ValueError(
							f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
							f"missing 'source_attestation_key' (expected "
							f"'ed25519:<kid>') — v4 records the signer of the "
							f"source attestation as the trust root for source-"
							f"rebuild verification.  Republish the package "
							f"with toolchain >= 0.30.0 and re-run `drift prepare`."
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


VERIFY_MODE_STRICT = "strict"
VERIFY_MODE_SOURCE_REBUILD = "source_rebuild"
_VERIFY_MODES = frozenset((VERIFY_MODE_STRICT, VERIFY_MODE_SOURCE_REBUILD))


def verify_lock_compatibility(
	lock_deps: dict[str, ResolvedDep],
	package_index: dict[str, list],
	*,
	allowed_co_artifacts: set[str] | None = None,
	mode: str = VERIFY_MODE_STRICT,
	sha_drift_log: list[tuple[str, str, str]] | None = None,
) -> list[str]:
	"""Verify a v4 lock against the current package index.

	Two modes:

	- **strict** (default; default-consumption contract): every
	  non-co-artifact dep in the lock must have a single disk entry
	  at the exact `M.N.P` version with matching `sha256`,
	  `author_key`, `source_content_id`, AND `source_attestation_key`.
	  Both halves of the v4 identity model are enforced: bytes
	  AND source.

	- **source_rebuild** (Phase D opt-in): the rebuilt artifact is
	  expected to differ in `sha256` and may have been signed by
	  a different key (the rebuilder's, not the original author's),
	  so those two fields are NOT enforced — only `version` +
	  `source_content_id` + `source_attestation_key` are required to
	  match.  The trust root is the source-attestation key recorded
	  in the lock; the rebuilt `.dmp`'s own signature is irrelevant
	  to source-mode certification (the rebuilder cannot sign as the
	  package owner).  Missing source identity (empty
	  `source_content_id` or `source_attestation_key` on disk) is a
	  hard fail with a republish-required diagnostic — silently
	  falling back to strict mode would let an un-attested package
	  pass under the source-mode banner, defeating the trust
	  boundary.

	  When `sha_drift_log` is provided, every per-package
	  (locked_sha, disk_sha) pair where the values differ is
	  appended as `(pkg_id, locked_sha, disk_sha)` for the caller
	  to surface as run evidence.  This makes byte-divergence
	  visible to humans (and to future reproducible-build work)
	  without elevating it to a verification failure.

	`allowed_co_artifacts` names the library artifacts declared in
	the caller's current manifest that MAY be marked `dep_type
	"co-artifact"` in the lock (they are built in this same deploy
	run, so their sha256 / author_key / source identity is
	intentionally not yet known).  A lock entry whose `dep_type ==
	"co-artifact"` but whose `pkg_id` is NOT in this set is treated
	as a verification bypass attempt (hand-edited or malformed lock)
	and rejected — an external dependency cannot legitimately claim
	co-artifact status to skip the re-checks.

	Passing `None` preserves the historical "trust the lock"
	behaviour.  New call sites in build / deploy pass the actual
	allowlist.

	Returns a list of error messages (empty = all good).
	"""
	if mode not in _VERIFY_MODES:
		raise ValueError(
			f"unknown verify_lock_compatibility mode {mode!r}; "
			f"expected one of {sorted(_VERIFY_MODES)}"
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
		# Exact version match — no range float, no "highest in
		# minor" fallback.  If the lock pins 0.3.15 and only 0.3.14
		# is on disk, the build fails and the user runs
		# `drift prepare` (which will either pick up 0.3.14 or fail
		# with a clearer "no satisfying candidate" error).
		exact_matches = [e for e in entries if str(e.version) == dep.version]
		if not exact_matches:
			available = sorted({str(e.version) for e in entries})
			errors.append(
				f"locked dependency '{pkg_id}' pins version '{dep.version}' "
				f"but available versions under package roots are: "
				f"{', '.join(available)}; run `drift prepare` to refresh "
				f"the lock or reinstall the pinned version"
			)
			continue
		# Multiple disk entries at the exact version would be a
		# package-root duplicate issue resolved upstream by
		# `build_package_index`; if we see >1 here, pick the first.
		disk = exact_matches[0]

		# ── Unsigned dev opt-in incompatible with source-rebuild ──
		# Source-rebuild mode REQUIRES a signed source attestation
		# as its trust root.  Unsigned packages have neither a
		# `.sig` NOR a `.source-attestation`, so there is nothing
		# to verify against — hard fail.  This is the only place
		# the unsigned opt-in is rejected outright.
		if dep.author_key == "unsigned" and mode == VERIFY_MODE_SOURCE_REBUILD:
			errors.append(
				f"locked dependency '{pkg_id}@{dep.version}' is marked "
				f"`author_key: \"unsigned\"`, but source-rebuild mode "
				f"requires a signed source attestation as the trust "
				f"root.  Unsigned packages have no `.source-"
				f"attestation` sidecar to verify against; sign and "
				f"republish (toolchain >= 0.30.0) before using "
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
		# no `.sig` (so author_key on disk is empty) and no
		# `.source-attestation` (so source identity is empty).  Byte
		# identity (above) is still enforced; this branch governs
		# the SIGNATURE-anchored checks only.
		if dep.author_key == "unsigned":
			continue

		# Strict mode: artifact-signer must match the lock.  Source-
		# rebuild mode: skip — the rebuilt `.dmp` is expected to be
		# signed by the rebuilder's key, not the original author's;
		# trust comes from the source-attestation half, not the
		# `.sig` signer.
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

		# Source-identity half — enforced in BOTH modes for signed
		# packages.  Disk values come from
		# `_read_source_attestation_meta`, which already signature-
		# verified and cross-bound the body to the .dmp manifest
		# at index time.
		if not disk.source_content_id or not disk.source_attestation_key:
			if mode == VERIFY_MODE_SOURCE_REBUILD:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' has no "
					f"valid source attestation on disk — source-rebuild "
					f"mode requires the `.source-attestation` sidecar to "
					f"verify against the lock's recorded source identity. "
					f"Republish the package with toolchain >= 0.30.0 (or "
					f"reinstall, if the sidecar was lost), then re-run "
					f"`drift prepare`.  Source-mode does NOT silently "
					f"fall back to byte-only verification — that would "
					f"defeat the trust boundary."
				)
			else:
				errors.append(
					f"locked dependency '{pkg_id}@{dep.version}' has no "
					f"valid source attestation on disk (sidecar missing, "
					f"unbound, or signature failed); run `drift prepare` "
					f"to refresh against current packages or republish "
					f"with toolchain >= 0.30.0"
				)
			continue
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
				f"source_attestation_key changed\n"
				f"  locked:   {dep.source_attestation_key}\n"
				f"  on-disk:  {disk.source_attestation_key}\n"
				f"  the source attestation was re-signed by a different key; "
				f"run `drift prepare` to accept the new key"
			)
			continue
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
