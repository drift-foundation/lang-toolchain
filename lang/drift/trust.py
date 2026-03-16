# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lang.drift.crypto import b64_decode, b64_encode, compute_ed25519_kid
from lang.drift.dmir_pkg_v0 import read_identity_v0
from lang.drift.sign import load_sig_sidecar_v0


def _now_iso8601_utc() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_or_init_trust_store(path: Path) -> dict[str, Any]:
	"""
	Load a trust store, or return an initialized empty one.

	This is drift-tooling UX; driftc is the verifier and maintains its own strict
	parser/validator.
	"""
	if not path.exists():
		return {
			"format": "drift-trust",
			"version": 0,
			"namespaces": {},
			"keys": {},
			"revoked": {},
		}
	obj = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(obj, dict) or obj.get("format") != "drift-trust" or obj.get("version") != 0:
		raise ValueError("unsupported trust store format/version")
	obj.setdefault("namespaces", {})
	obj.setdefault("keys", {})
	obj.setdefault("revoked", {})
	return obj


def _write_trust_store(path: Path, obj: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _ensure_dict(obj: Any, msg: str) -> dict[str, Any]:
	if not isinstance(obj, dict):
		raise ValueError(msg)
	return obj


@dataclass(frozen=True)
class TrustListOptions:
	trust_store_path: Path


def list_trust_store(opts: TrustListOptions) -> dict[str, Any]:
	obj = _load_or_init_trust_store(opts.trust_store_path)
	return obj


@dataclass(frozen=True)
class TrustAddKeyOptions:
	trust_store_path: Path
	namespace: str
	pubkey_b64: str
	kid: str | None


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

	# Allow for namespace (idempotent).
	allowed = namespaces.get(opts.namespace)
	if allowed is None:
		namespaces[opts.namespace] = [kid]
	elif isinstance(allowed, list):
		if kid not in allowed:
			allowed.append(kid)
	else:
		raise ValueError("trust store namespaces entries must be arrays")

	_write_trust_store(opts.trust_store_path, obj)


@dataclass(frozen=True)
class TrustRevokeOptions:
	trust_store_path: Path
	kid: str
	reason: str | None


def revoke_kid_in_trust_store(opts: TrustRevokeOptions) -> None:
	obj = _load_or_init_trust_store(opts.trust_store_path)
	revoked = obj.get("revoked")
	# Support upgrading older trust stores where revoked was a list of kids.
	if revoked is None:
		obj["revoked"] = {}
		revoked = obj["revoked"]
	if isinstance(revoked, list):
		revoked_dict: dict[str, Any] = {str(k): {} for k in revoked if isinstance(k, str)}
		obj["revoked"] = revoked_dict
		revoked = revoked_dict
	revoked_obj = _ensure_dict(revoked, "trust store revoked must be a JSON object")

	revoked_obj.setdefault(opts.kid, {"revoked_at": _now_iso8601_utc()})
	if opts.reason is not None:
		entry = revoked_obj.get(opts.kid)
		if isinstance(entry, dict):
			entry.setdefault("reason", opts.reason)

	_write_trust_store(opts.trust_store_path, obj)


@dataclass(frozen=True)
class TrustImportOptions:
	trust_store_path: Path
	namespace: str | None
	source_path: Path


def plan_trust_import(opts: TrustImportOptions) -> tuple[Path, str, str | None]:
	source = opts.source_path
	sidecar_path = source
	package_id: str | None = None
	if source.suffix == ".sig":
		# Try to find sibling package (.zdmp or .dmp) for identity.
		for ext in (".zdmp", ".dmp"):
			base = source.with_suffix(ext)
			if base.exists():
				ident = read_identity_v0(base)
				package_id = ident.package_id
				break
	if source.suffix in (".dmp", ".zdmp"):
		ident = read_identity_v0(source)
		package_id = ident.package_id
		sidecar_path = source.with_suffix(".sig")
	if sidecar_path.suffix != ".sig":
		raise ValueError("trust import expects a .sig sidecar or a .dmp/.zdmp package path")
	if not sidecar_path.exists():
		raise ValueError(f"signature sidecar not found: {sidecar_path}")
	namespace = opts.namespace
	if namespace is None:
		if package_id is None:
			raise ValueError("namespace is required when importing from .sig without sibling .dmp")
		namespace = f"{package_id}.*"
	return (sidecar_path, namespace, package_id)


def import_sidecar_keys_to_trust_store(opts: TrustImportOptions) -> dict[str, Any]:
	sidecar_path, namespace, package_id = plan_trust_import(opts)
	sf = load_sig_sidecar_v0(sidecar_path)
	imported: list[str] = []
	missing_pubkeys: list[str] = []
	for sig in sf.signatures:
		pubkey_b64 = sig.pubkey_b64
		if pubkey_b64 is None or not pubkey_b64.strip():
			missing_pubkeys.append(sig.kid)
			continue
		add_key_to_trust_store(
			TrustAddKeyOptions(
				trust_store_path=opts.trust_store_path,
				namespace=namespace,
				pubkey_b64=pubkey_b64,
				kid=sig.kid,
			)
		)
		imported.append(sig.kid)
	return {
		"source": str(sidecar_path),
		"namespace": namespace,
		"package_id": package_id,
		"imported_kids": sorted(set(imported)),
		"missing_pubkeys": sorted(set(missing_pubkeys)),
	}
