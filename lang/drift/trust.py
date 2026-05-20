# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`drift trust` CLI — manage a v1 trust store JSON file.

The on-disk shape produced and consumed by this module matches
`lang.driftc.packages.trust_v1`:

  {
    "format": "drift-trust",
    "version": 1,
    "keys":  { "<kid>": {"algo": "ed25519", "pubkey": "<b64>"} },
    "namespaces": {
        "<pattern>": {"authors": ["<kid>", ...], "certifiers": ["<kid>", ...]}
    },
    "revoked": ["<kid>", ...]
  }

This CLI is the drift-tooling UX layer; driftc and drift_deploy
re-parse the file through their own strict validators
(`trust_v1.load_trust_store_json`).  Both surfaces must agree on
the shape or a CLI-written file fails to load at compile time.

The pre-v1 trust shape (`version: 0`, flat-list namespaces, dict
`revoked`) is no longer accepted on load.  Pre-v1 stores are not
migrated automatically: the user re-runs `drift trust add` to
rebuild the file in v1 shape.  This matches the audit's
"pre-v1 acceptance is a hard product boundary" rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lang.drift.crypto import b64_decode, b64_encode, compute_ed25519_kid


def _now_iso8601_utc() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _empty_v1_store() -> dict[str, Any]:
	return {
		"format": "drift-trust",
		"version": 1,
		"keys": {},
		"namespaces": {},
		"revoked": [],
	}


def _load_or_init_trust_store(path: Path) -> dict[str, Any]:
	"""Load a v1 trust store, or return an initialized empty one.

	This is drift-tooling UX; driftc is the verifier and maintains
	its own strict parser/validator
	(`lang.driftc.packages.trust_v1.load_trust_store_json`).
	"""
	if not path.exists():
		return _empty_v1_store()
	obj = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(obj, dict) or obj.get("format") != "drift-trust" or obj.get("version") != 1:
		raise ValueError(
			"unsupported trust store format/version (expected "
			"`format: drift-trust, version: 1`).  Pre-v1 stores are "
			"not accepted; re-run `drift trust add` for each kid to "
			"rebuild the file in v1 shape."
		)
	obj.setdefault("keys", {})
	obj.setdefault("namespaces", {})
	obj.setdefault("revoked", [])
	return obj


def _write_trust_store(path: Path, obj: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _ensure_dict(obj: Any, msg: str) -> dict[str, Any]:
	if not isinstance(obj, dict):
		raise ValueError(msg)
	return obj


def _ensure_list(obj: Any, msg: str) -> list[Any]:
	if not isinstance(obj, list):
		raise ValueError(msg)
	return obj


def _ns_entry(namespaces: dict[str, Any], ns: str) -> dict[str, list[str]]:
	"""Return the role-tagged entry for `ns`, creating it on demand."""
	entry = namespaces.get(ns)
	if entry is None:
		entry = {"authors": [], "certifiers": []}
		namespaces[ns] = entry
		return entry
	if not isinstance(entry, dict):
		raise ValueError(
			f"trust store namespace {ns!r} entry must be a JSON "
			f"object with 'authors' and 'certifiers' lists"
		)
	entry.setdefault("authors", [])
	entry.setdefault("certifiers", [])
	return entry


@dataclass(frozen=True)
class TrustListOptions:
	trust_store_path: Path


def list_trust_store(opts: TrustListOptions) -> dict[str, Any]:
	return _load_or_init_trust_store(opts.trust_store_path)


@dataclass(frozen=True)
class TrustAddKeyOptions:
	"""Add a public key to the trust store.

	`role` is `"author"`, `"certifier"`, or `"both"`.  Default is
	`"both"` (Foundation-bootstrap pattern: a single kid plays
	both roles for a namespace).  Production setups that separate
	the two should pass `"author"` or `"certifier"` explicitly.
	"""
	trust_store_path: Path
	namespace: str
	pubkey_b64: str
	kid: str | None
	role: str = "both"


def add_key_to_trust_store(opts: TrustAddKeyOptions) -> None:
	obj = _load_or_init_trust_store(opts.trust_store_path)
	keys = _ensure_dict(obj.get("keys"), "trust store keys must be a JSON object")
	namespaces = _ensure_dict(obj.get("namespaces"), "trust store namespaces must be a JSON object")

	pub_raw = b64_decode(opts.pubkey_b64.strip())
	if len(pub_raw) != 32:
		raise ValueError("ed25519 public key must decode to 32 bytes")
	kid = opts.kid or compute_ed25519_kid(pub_raw)
	if opts.kid is not None and kid != opts.kid:
		raise ValueError("provided --kid does not match derived kid from pubkey")

	# Record key material (idempotent).
	keys.setdefault(kid, {"algo": "ed25519", "pubkey": b64_encode(pub_raw)})

	# Grant for the namespace (idempotent across calls).  Role lists
	# stay sorted-unique so a re-run produces the same file.
	entry = _ns_entry(namespaces, opts.namespace)
	roles_to_grant: list[str]
	if opts.role == "both":
		roles_to_grant = ["authors", "certifiers"]
	elif opts.role == "author":
		roles_to_grant = ["authors"]
	elif opts.role == "certifier":
		roles_to_grant = ["certifiers"]
	else:
		raise ValueError(
			f"trust add: --role must be 'author', 'certifier', or "
			f"'both'; got {opts.role!r}"
		)
	for role_key in roles_to_grant:
		role_list = entry[role_key]
		if not isinstance(role_list, list):
			raise ValueError(
				f"trust store namespace {opts.namespace!r} {role_key!r} "
				f"must be a list"
			)
		if kid not in role_list:
			role_list.append(kid)
			role_list.sort()

	_write_trust_store(opts.trust_store_path, obj)


@dataclass(frozen=True)
class TrustRevokeOptions:
	trust_store_path: Path
	kid: str
	reason: str | None


def revoke_kid_in_trust_store(opts: TrustRevokeOptions) -> None:
	"""Mark a kid as revoked in the trust store.

	v1 stores `revoked` as a flat list of kids (per the v1 trust
	loader).  The `reason` parameter is accepted for CLI
	compatibility but not persisted into the v1 trust JSON --
	revocation in v1 is binary at the trust gate, and revocation
	provenance belongs in a separate audit log if needed.
	"""
	obj = _load_or_init_trust_store(opts.trust_store_path)
	revoked = obj.get("revoked")
	if revoked is None:
		obj["revoked"] = []
		revoked = obj["revoked"]
	# Upgrade older pre-v1 shapes (list-of-kids or dict-of-kid->meta)
	# defensively.  Pre-v1 STORES are rejected at load time above;
	# this branch covers a partially-edited file that still has the
	# old `revoked` shape after a manual edit.
	if isinstance(revoked, dict):
		revoked = sorted(revoked.keys())
		obj["revoked"] = revoked
	if not isinstance(revoked, list):
		raise ValueError("trust store revoked must be a flat list of kid strings")

	if opts.kid not in revoked:
		revoked.append(opts.kid)
		revoked.sort()

	_write_trust_store(opts.trust_store_path, obj)


@dataclass(frozen=True)
class TrustImportOptions:
	"""Import a v1 author-claim sidecar's signer kids into the trust
	store.  Pre-v1 `.sig` imports are no longer supported; use
	`drift-author publish` to produce a v1 author claim, then
	import it here.
	"""
	trust_store_path: Path
	namespace: str | None
	source_path: Path
	role: str = "author"  # author-claims grant author role by default


def plan_trust_import(opts: TrustImportOptions) -> tuple[Path, str]:
	"""Resolve the import source and decide the target namespace.

	Accepts a v1 `<pkg>.author-claim` sidecar OR a package
	(`.dmp`/`.zdmp`) whose canonical author-claim sidecar sits
	next to it.  Returns `(claim_path, namespace)`.

	The namespace defaults to `<package_id>.*` read from the
	claim body when not provided.  Pre-v1 `.sig` imports are
	rejected -- explicit error pointing the user at `drift-author
	publish`.
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.sidecar_naming import author_claim_filename
	from lang.drift.dmir_pkg_v0 import read_identity_v0

	source = opts.source_path
	if source.suffix == ".sig":
		raise ValueError(
			"pre-v1 `.sig` sidecars are no longer supported by "
			"`drift trust import`.  Re-run `drift-author publish` "
			"to produce a v1 author claim, then import that file."
		)
	claim_path: Path
	if source.suffix in (".dmp", ".zdmp"):
		ident = read_identity_v0(source)
		canon = author_claim_filename(ident.package_id)
		claim_path = source.parent / canon
		if not claim_path.is_file():
			raise ValueError(
				f"v1 author claim sidecar not found next to package: "
				f"{claim_path}.  Run `drift-author publish` for "
				f"{ident.package_id}@{ident.version} first."
			)
	else:
		claim_path = source
	if not claim_path.exists():
		raise ValueError(f"author claim sidecar not found: {claim_path}")
	# Read claim to derive default namespace.
	claim = load_author_claim_json(claim_path.read_text(encoding="utf-8"))
	namespace = opts.namespace or (
		claim.body.namespaces[0] if claim.body.namespaces else f"{claim.body.package_id}.*"
	)
	return claim_path, namespace


def import_sidecar_keys_to_trust_store(opts: TrustImportOptions) -> dict[str, Any]:
	"""Import every signer kid from a v1 author-claim sidecar
	into the trust store under the appropriate role.

	Returns a summary dict for CLI display.  Author claims grant
	the author role by default; pass `role="both"` (caller-controlled)
	to also grant certifier in the same call.
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json

	claim_path, namespace = plan_trust_import(opts)
	claim = load_author_claim_json(claim_path.read_text(encoding="utf-8"))
	imported: list[str] = []
	# v1 author-claim signatures don't carry pubkey bytes inline
	# (pubkeys live in the trust store).  The user is expected to
	# have the pubkey via `drift key` already, or to have added it
	# manually with `drift trust add`.  Here we record the kid +
	# namespace + role, surfacing "missing pubkey" as a per-kid
	# diagnostic the caller can act on.
	missing_pubkeys: list[str] = []
	obj = _load_or_init_trust_store(opts.trust_store_path)
	known_keys = _ensure_dict(obj.get("keys"), "trust store keys must be a JSON object")
	for sig in claim.signatures:
		if sig.kid not in known_keys:
			missing_pubkeys.append(sig.kid)
			continue
		# Grant the role for the namespace.  Reuse the same
		# add-key code path so namespace storage stays consistent.
		key_entry = known_keys[sig.kid]
		pubkey_b64 = key_entry.get("pubkey", "") if isinstance(key_entry, dict) else ""
		add_key_to_trust_store(TrustAddKeyOptions(
			trust_store_path=opts.trust_store_path,
			namespace=namespace,
			pubkey_b64=pubkey_b64,
			kid=sig.kid,
			role=opts.role,
		))
		imported.append(sig.kid)
	return {
		"source": str(claim_path),
		"namespace": namespace,
		"package_id": claim.body.package_id,
		"imported_kids": sorted(set(imported)),
		"missing_pubkeys": sorted(set(missing_pubkeys)),
	}
