# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Run-scope source-rebuild snapshot (v0, unsigned).

The source-rebuild trust model separates two concerns:

**Author trust** — "who is an authorized signer for package namespace
X" — lives in `TrustStore`s (core + project + optional user) and is
verified at ORCH STAGING TIME.  Orch runs `drift deploy` (producer
path) against its own trust store; each successful staging writes
the verified identity for that package into a run-scope snapshot.
Downstream repos do not need author-trust knowledge of upstreams.

**Source identity** — "for THIS run, package `net-tls@0.4.1` has
exactly this `source_content_id`, signed by exactly these kids" —
lives in the run snapshot this module defines.  Downstream source-
rebuild consumers (`drift build --source-rebuild` / `drift deploy
--source-rebuild` / `drift prepare --check --source-rebuild`)
verify each resolved dep against the snapshot.  The consuming
repo's local `drift/trust.json` is NOT consulted for upstream
packages under source-rebuild.

Pre-0.31.3 source-rebuild pulled both concerns into the consuming
repo's trust store — which broke the contract Lock-v2 was designed
for: every compatible upstream patch forced every downstream repo
to re-authorise the upstream signer in local trust, or re-prepare
to refresh the lock.  The run snapshot pins source identity per
certification run without asking downstream to carry upstream
trust.

v0 format is unsigned JSON — snapshots are run-local artifacts
produced and consumed inside the same orch workspace.  When
snapshots start leaving the run boundary we'll revisit signing
(the trust root for the snapshot itself would need a new key-
distribution story).

JSON shape:

    {
      "format": "drift-run-snapshot",
      "version": 0,
      "run_id": "<opaque-orch-run-id>",
      "packages": {
        "net-tls|0.4.1": {
          "source_content_id": "sha256:<hex>",
          "author_key":       "ed25519:<kid>",
          "source_attestation_key": "ed25519:<kid>",
          "sha256": "<hex, evidence-only>"
        },
        ...
      }
    }

Key is `"{pkg_id}|{version}"` — the `|` separator avoids collision
with package ids that may contain `@` (none today, but the format
is forward-compatible).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SNAPSHOT_FORMAT_TAG = "drift-run-snapshot"
SNAPSHOT_FORMAT_VERSION = 0


@dataclass(frozen=True)
class SnapshotEntry:
	"""One package pinned by the run snapshot.

	Fields:
	- `source_content_id`: canonical hash of declared source/build
	  inputs, mirrored from the v1 author claim / cert claim
	  `source_content_id` field.  EQUALITY GATE for source-rebuild
	  consumption.
	- `author_key`: kid carried in the package's `<pkg>.author-claim`
	  sidecar.  EQUALITY GATE.
	- `source_attestation_key`: kid carried in the package's
	  `<pkg>.cert-claim.<kid>.json` sidecar (the certifier).
	  EQUALITY GATE.
	- `sha256`: bytes digest of the `.dmp`, evidence only (rebuilt
	  artifacts may legitimately differ in bytes while source
	  identity is stable).  Carried in the snapshot so orch logs
	  can correlate, but NOT checked as a gate in v0.
	"""

	source_content_id: str
	author_key: str
	source_attestation_key: str
	sha256: str = ""


@dataclass(frozen=True)
class RunSnapshot:
	"""A loaded run snapshot.

	`packages` is keyed by `"{pkg_id}|{version}"` — use
	`lookup(pkg_id, version)` to avoid hard-coding the key format.
	`run_id` is informational (echoed back for diagnostics).
	"""

	run_id: str
	packages: dict[str, SnapshotEntry] = field(default_factory=dict)

	@staticmethod
	def _key(pkg_id: str, version: str) -> str:
		return f"{pkg_id}|{version}"

	def lookup(self, pkg_id: str, version: str) -> SnapshotEntry | None:
		return self.packages.get(self._key(pkg_id, version))

	def has(self, pkg_id: str, version: str) -> bool:
		return self._key(pkg_id, version) in self.packages


def load_run_snapshot(path: Path) -> RunSnapshot:
	"""Read and parse a v0 run snapshot from disk.

	Raises `ValueError` on malformed JSON, wrong format tag /
	version, or missing required fields.  Raises `OSError` (or
	subclass) on read failure — the caller turns that into a
	user-facing error.  Per-entry field validation is strict:
	every entry must carry non-empty `source_content_id`,
	`author_key`, `source_attestation_key` (all three are equality
	gates for source-rebuild consumers).  `sha256` is optional.
	"""
	text = path.read_text(encoding="utf-8")
	try:
		obj = json.loads(text)
	except json.JSONDecodeError as e:
		raise ValueError(f"run snapshot {path}: invalid JSON: {e}") from e
	if not isinstance(obj, dict):
		raise ValueError(f"run snapshot {path}: top-level value must be a JSON object")
	if obj.get("format") != SNAPSHOT_FORMAT_TAG:
		raise ValueError(
			f"run snapshot {path}: unexpected `format`; expected "
			f"{SNAPSHOT_FORMAT_TAG!r}, got {obj.get('format')!r}"
		)
	if obj.get("version") != SNAPSHOT_FORMAT_VERSION:
		raise ValueError(
			f"run snapshot {path}: unexpected `version`; expected "
			f"{SNAPSHOT_FORMAT_VERSION}, got {obj.get('version')!r}"
		)
	run_id = obj.get("run_id", "")
	if not isinstance(run_id, str):
		raise ValueError(f"run snapshot {path}: `run_id` must be a string")
	pkgs_obj = obj.get("packages")
	if not isinstance(pkgs_obj, dict):
		raise ValueError(f"run snapshot {path}: `packages` must be a JSON object")
	packages: dict[str, SnapshotEntry] = {}
	# Strict field validators — malformed snapshot fails at load,
	# not later as an opaque mismatch.  Matches the discipline on
	# every other signed / canonical surface that records sha ids
	# and signer kids.
	from lang.driftc.packages.source_content_id import validate_sci as validate_sha256_hex_id
	for key, entry_obj in pkgs_obj.items():
		if not isinstance(key, str) or "|" not in key:
			raise ValueError(
				f"run snapshot {path}: package key {key!r} must be "
				f"'{{pkg_id}}|{{version}}'"
			)
		if not isinstance(entry_obj, dict):
			raise ValueError(
				f"run snapshot {path}: entry for {key!r} must be a JSON object"
			)
		scid = entry_obj.get("source_content_id")
		ak = entry_obj.get("author_key")
		sak = entry_obj.get("source_attestation_key")
		sha = entry_obj.get("sha256", "")
		# `source_content_id`: strict `sha256:<64 lowercase hex>`.
		# Uses the shared validator so the snapshot loader and the
		# v1 author/cert claim loaders enforce identical shape.
		try:
			validate_sha256_hex_id(
				scid,
				field=f"run snapshot {path}: entry {key!r} 'source_content_id'",
			)
		except ValueError:
			raise  # propagate with the validator's own message
		# `author_key` + `source_attestation_key`: require the
		# `ed25519:<opaque>` shape.  We don't re-validate the kid
		# body here — that's enforced on signing surfaces — but the
		# prefix discipline prevents loading a snapshot whose kids
		# are wrong shape (e.g. bare b64 without algorithm prefix,
		# or garbage).
		for field_name, value in (
			("author_key", ak),
			("source_attestation_key", sak),
		):
			if not isinstance(value, str) or not value:
				raise ValueError(
					f"run snapshot {path}: entry for {key!r} has "
					f"missing or empty `{field_name}` (required for "
					f"snapshot-gated source-rebuild verification)"
				)
			if not value.startswith("ed25519:") or len(value) <= len("ed25519:"):
				raise ValueError(
					f"run snapshot {path}: entry for {key!r} "
					f"`{field_name}` must be 'ed25519:<kid>'; got {value!r}"
				)
		if not isinstance(sha, str):
			raise ValueError(
				f"run snapshot {path}: entry for {key!r} `sha256` "
				f"must be a string if present"
			)
		packages[key] = SnapshotEntry(
			source_content_id=scid,
			author_key=ak,
			source_attestation_key=sak,
			sha256=sha,
		)
	return RunSnapshot(run_id=run_id, packages=packages)


def write_run_snapshot(
	path: Path,
	*,
	run_id: str,
	entries: dict[tuple[str, str], SnapshotEntry],
) -> None:
	"""Emit a v0 run snapshot to disk.

	`entries` is keyed by `(pkg_id, version)` tuples for type-safety
	at call sites; this function converts to the `"pkg_id|version"`
	JSON key form.  Orch calls this after `drift deploy` (producer
	mode) completes staging — one snapshot per certification run,
	shared by every downstream source-rebuild call in the same run.
	"""
	pkgs_obj: dict[str, Any] = {}
	for (pkg_id, version), entry in entries.items():
		key = RunSnapshot._key(pkg_id, version)
		pkgs_obj[key] = {
			"source_content_id": entry.source_content_id,
			"author_key": entry.author_key,
			"source_attestation_key": entry.source_attestation_key,
		}
		if entry.sha256:
			pkgs_obj[key]["sha256"] = entry.sha256
	obj = {
		"format": SNAPSHOT_FORMAT_TAG,
		"version": SNAPSHOT_FORMAT_VERSION,
		"run_id": run_id,
		"packages": pkgs_obj,
	}
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
		encoding="utf-8",
	)


def verify_disk_entry_against_snapshot(
	snapshot: RunSnapshot,
	*,
	pkg_id: str,
	version: str,
	disk_source_content_id: str,
	disk_author_key: str,
	disk_source_attestation_key: str,
) -> str | None:
	"""Gate check for one disk package against the snapshot.

	Returns `None` on match, an error message string on mismatch.
	Missing snapshot entry for `(pkg_id, version)` is an error —
	the snapshot is the authoritative "what this run is certifying"
	statement, so a package not in it was never authorised by
	orch.  Source-rebuild consumers treat the return string as a
	hard failure.
	"""
	entry = snapshot.lookup(pkg_id, version)
	if entry is None:
		return (
			f"package '{pkg_id}@{version}' not present in run snapshot — "
			f"orch did not stage this package/version combination.  "
			f"Source-rebuild consumers can only accept packages the "
			f"snapshot authorises; the disk package was not part of the "
			f"certified source graph for this run."
		)
	if entry.source_content_id != disk_source_content_id:
		return (
			f"package '{pkg_id}@{version}' `source_content_id` "
			f"mismatch vs. run snapshot:\n"
			f"  snapshot: {entry.source_content_id}\n"
			f"  on-disk:  {disk_source_content_id}\n"
			f"  same-version source swap rejected — the disk package "
			f"was rebuilt from different source than orch staged."
		)
	if entry.author_key != disk_author_key:
		return (
			f"package '{pkg_id}@{version}' `author_key` mismatch vs. "
			f"run snapshot:\n"
			f"  snapshot: {entry.author_key}\n"
			f"  on-disk:  {disk_author_key}\n"
			f"  artifact signer differs from the one orch recorded at "
			f"staging time."
		)
	if entry.source_attestation_key != disk_source_attestation_key:
		return (
			f"package '{pkg_id}@{version}' `source_attestation_key` "
			f"mismatch vs. run snapshot:\n"
			f"  snapshot: {entry.source_attestation_key}\n"
			f"  on-disk:  {disk_source_attestation_key}\n"
			f"  cert-claim signer differs from the one orch "
			f"recorded at staging time."
		)
	return None
