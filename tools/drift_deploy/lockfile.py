# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift/lock.json read / write / verify (schema v3).

The lock records, per artifact, the **exact resolved artifact** for
every dependency in the transitive graph: `M.N.P` version, sha256 of
the `.dmp` file, signer `author_key`, and `dep_type`.  This answers
the auditor question "what exactly did this library compile against?"
without a range → disk-scan round trip.

The authored manifest (drift/manifest.json) carries the owner's
declared acceptable range; the lock is downstream of resolution.
`drift prepare` is the only sanctioned writer.  Build / deploy
strictly consume the exact pins and refuse any mismatch against the
on-disk package (version, sha, signer).

Schema history:
- v1 — exact version + `integrity: "sha256:<hex>"` (pre-0.27 era).
- v2 — major.minor range + author_key; sha was discarded.
- v3 — exact M.N.P + sha256 + author_key + dep_type (0.29.0+).

v1 and v2 locks are rejected at load; `drift prepare` regenerates as
v3.  No silent migration — a stale lock must not quietly reinterpret
a range entry as an exact pin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.drift_deploy.resolver import ResolvedDep


LOCK_SCHEMA_VERSION = 3

# Lock v3 pins every non-co-artifact entry to a fully-qualified
# `M.N.P` version (exact resolved release).  Any other shape —
# a range (`M.N`), a constraint operator (`^0.3.0`), empty string,
# or garbage — is a lock-corruption symptom.  Defensive shape check
# at the loader means build/deploy never have to re-parse.
_EXACT_MNP_RE = re.compile(r"^\d+\.\d+\.\d+$")


def write_lock(
	path: Path,
	artifacts: dict[str, dict[str, ResolvedDep]],
) -> None:
	"""Write drift/lock.json (schema v3).

	`artifacts` is {artifact_name → {package_id → ResolvedDep}}.
	Each entry is emitted exactly: version M.N.P + sha256 +
	author_key + dep_type.  No range field; no file-level
	integrity.  The map key IS the package id; no redundant
	`package_id` field inside the entry.
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
	"""Read drift/lock.json (schema v3 only).

	v1 and v2 locks are rejected with a pointer to `drift prepare`.
	Under v3 the lock is downstream of resolution, authored only by
	`drift prepare`, and load-bearing for build/deploy strict-exact
	re-verification — accepting older shapes here would silently
	reinterpret owner-declared ranges (v2) or pre-authorship pins
	(v1) as exact pins and bypass the trust re-check.

	Returns {artifact_name → {package_id → ResolvedDep}}.
	"""
	data = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(data, dict):
		raise ValueError("drift/lock.json must be a JSON object")
	sv = data.get("schema_version")
	if sv in (1, 2):
		raise ValueError(
			f"drift/lock.json uses schema v{sv}; v3 is required as of "
			"0.29.0.  Run `drift prepare` to regenerate the lock with "
			"exact resolved versions, sha256 digests, and signer keys. "
			"Older schemas carried ranges (v2) or byte-only pins (v1) "
			"and cannot be safely reinterpreted as v3 exact pins."
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
					f"missing 'version' (exact M.N.P required in v3)"
				)
			dep_type = dep_data.get("dep_type", "direct")
			# v3 pins an exact `M.N.P` for every entry, including
			# co-artifacts (their .dmp is built in the same deploy
			# run but pinned at the manifest's exact release version).
			# Reject any range shape or constraint operator here so
			# build/deploy never have to second-guess the lock.
			if not _EXACT_MNP_RE.match(version):
				raise ValueError(
					f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
					f"version '{version}' is not an exact M.N.P pin — v3 "
					f"only stores fully-resolved versions (a range or "
					f"constraint here means the lock was hand-edited or "
					f"left over from an older schema).  Run `drift "
					f"prepare` to regenerate."
				)
			dep_sha = dep_data.get("sha256", "")
			dep_author_key = dep_data.get("author_key", "")
			if dep_type != "co-artifact":
				if not isinstance(dep_sha, str) or not dep_sha:
					raise ValueError(
						f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
						f"missing 'sha256' — v3 requires exact artifact "
						f"digests for every non-co-artifact dep"
					)
				if not isinstance(dep_author_key, str) or not dep_author_key:
					raise ValueError(
						f"drift/lock.json artifact '{art_name}' dep '{pkg_id}' "
						f"missing 'author_key' — packages must be signed "
						f"before locking; run `drift prepare` after signing"
					)
				# "unsigned" is an explicit opt-in for development builds
				# where packages are consumed via --allow-unsigned-from.
			resolved[pkg_id] = ResolvedDep(
				version=version,
				sha256=dep_sha if isinstance(dep_sha, str) else "",
				dep_type=dep_type,
				package_id=pkg_id,
				author_key=dep_author_key if isinstance(dep_author_key, str) else "",
			)
		result[art_name] = resolved
	return result


def verify_lock_compatibility(
	lock_deps: dict[str, ResolvedDep],
	package_index: dict[str, list],
) -> list[str]:
	"""Verify a v3 lock against the current package index.

	Strict-exact contract: every non-co-artifact dep in the lock
	must have a single disk entry at the exact `M.N.P` version with
	matching sha256 and matching author_key.  Any deviation is a
	build/deploy-time error.  The authoritative mechanism for
	moving the lock forward is `drift prepare`; this function NEVER
	silently re-resolves.

	Returns a list of error messages (empty = all good).
	"""
	errors: list[str] = []
	for pkg_id, dep in lock_deps.items():
		if dep.dep_type == "co-artifact":
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
		# `build_package_index`; if we see >1 here, pick the first
		# and still require the sha to match.
		disk = exact_matches[0]
		# sha256 match — required for every non-co-artifact entry
		# on both sides.  The lock's sha is the exact fingerprint of
		# the `.dmp` the producer signed off on; a different sha
		# means the artifact was rebuilt or replaced, which
		# invalidates the lock.  Empty sha on EITHER side is a hard
		# fail: without both digests, the load-bearing reproducibility
		# check would silently pass and a programmatically-constructed
		# `ResolvedDep` with `sha256=""` could bypass verification.
		# (dep_type == "co-artifact" was already skipped above.)
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
		# Signer re-check.  "unsigned" is the explicit dev-mode
		# opt-out (requires --allow-unsigned-from downstream).
		if dep.author_key == "unsigned":
			continue
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
	return errors


def expand_to_dep_flags(resolved: dict[str, ResolvedDep]) -> list[str]:
	"""Expand a v3-locked resolved graph into exact --dep flags for driftc.

	Returns ["--dep", "net.tls@0.3.15", "--dep", "acme.crypto@0.9.0", ...].
	Order is deterministic (sorted by package id).  `driftc` stays a
	flat exact loader — it never sees ranges.
	"""
	flags: list[str] = []
	for pkg_id in sorted(resolved.keys()):
		dep = resolved[pkg_id]
		flags.extend(["--dep", f"{pkg_id}@{dep.version}"])
	return flags
