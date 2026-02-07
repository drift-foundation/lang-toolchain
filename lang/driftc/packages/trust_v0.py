# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Trust store handling for package signature verification (v0).

This module defines the minimal trust-policy mechanism needed for Milestone 4:
- packages are verified offline using local trust stores,
- trust is pinned by namespace (module-id prefix) to allowed signer keys.

Pinned policy (MVP):
- trust stores are project-local by default (`./drift/trust.json`),
- a user-level trust store may be optionally loaded as a convenience layer,
- signature verification is performed by `driftc` at *use time* (gatekeeper),
  even if a downloader tool also verifies earlier.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
import os
from typing import Any, Mapping


def _b64_decode(text: str) -> bytes:
	# Standard base64 with padding; reject whitespace-only surprises.
	return base64.b64decode(text.encode("ascii"), validate=True)


@dataclass(frozen=True)
class TrustedKey:
	algo: str  # "ed25519" in v1
	kid: str
	pubkey_raw: bytes  # raw bytes (32 for Ed25519)


@dataclass(frozen=True)
class TrustStore:
	"""
	Resolved trust store.

	- `keys_by_kid` provides public keys for verification.
	- `allowed_kids_by_namespace` pins which keys may sign which namespaces.
	- `revoked_kids` is a local revocation set (MVP).
	"""

	keys_by_kid: dict[str, TrustedKey]
	allowed_kids_by_namespace: dict[str, set[str]]
	revoked_kids: set[str]

	def allowed_kids_for_module(self, module_id: str) -> set[str]:
		"""
		Return the allowed signer key ids for a module id.

		Namespace matching rules (MVP):
		- exact match: "acme.crypto" matches module "acme.crypto"
		- prefix match: "acme.*" matches module ids starting with "acme."
		- choose the most specific (longest) matching namespace key(s)
		"""
		best_len = -1
		out: set[str] = set()
		for ns, kids in self.allowed_kids_by_namespace.items():
			if ns.endswith(".*"):
				pfx = ns[:-2]
				if module_id == pfx or module_id.startswith(pfx + "."):
					l = len(pfx)
				else:
					continue
			else:
				if module_id != ns:
					continue
				l = len(ns)
			if l > best_len:
				best_len = l
				out = set(kids)
			elif l == best_len:
				out |= set(kids)
		return out


def load_trust_store_json(path: Path) -> TrustStore:
	"""
	Load a trust store file.

	Format (pinned for v0, JSON):
	{
	  "format": "drift-trust",
	  "version": 0,
	  "keys": {
	    "<kid>": { "algo": "ed25519", "pubkey": "<base64 raw bytes>" }
	  },
	  "namespaces": {
	    "acme.*": ["<kid>", "..."]
	  },
	  "revoked": ["<kid>", ...] // optional
	}
	"""
	obj = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(obj, dict):
		raise ValueError("trust store must be a JSON object")
	if obj.get("format") != "drift-trust" or obj.get("version") != 0:
		raise ValueError("unsupported trust store format/version")

	keys_by_kid: dict[str, TrustedKey] = {}
	keys_obj = obj.get("keys") or {}
	if not isinstance(keys_obj, dict):
		raise ValueError("trust store keys must be a JSON object")
	for kid, kobj in keys_obj.items():
		if not isinstance(kid, str) or not isinstance(kobj, dict):
			continue
		algo = str(kobj.get("algo") or "")
		pub_b64 = kobj.get("pubkey")
		if algo != "ed25519" or not isinstance(pub_b64, str):
			continue
		pub_raw = _b64_decode(pub_b64)
		if len(pub_raw) != 32:
			raise ValueError("ed25519 pubkey must be 32 bytes")
		keys_by_kid[kid] = TrustedKey(algo=algo, kid=kid, pubkey_raw=pub_raw)

	ns_obj = obj.get("namespaces") or {}
	if not isinstance(ns_obj, dict):
		raise ValueError("trust store namespaces must be a JSON object")
	allowed: dict[str, set[str]] = {}
	for ns, kids in ns_obj.items():
		if not isinstance(ns, str) or not isinstance(kids, list):
			continue
		allowed[ns] = {str(k) for k in kids if isinstance(k, str)}

	revoked_kids: set[str] = set()
	revoked_obj = obj.get("revoked") or []
	if isinstance(revoked_obj, list):
		# Legacy v0 shape: "revoked": ["<kid>", ...]
		revoked_kids = {str(k) for k in revoked_obj if isinstance(k, str)}
	elif isinstance(revoked_obj, dict):
		# Preferred v0 shape used by drift tooling: "revoked": { "<kid>": {...}, ... }
		revoked_kids = {str(k) for k in revoked_obj.keys() if isinstance(k, str)}

	return TrustStore(keys_by_kid=keys_by_kid, allowed_kids_by_namespace=allowed, revoked_kids=revoked_kids)


def load_core_trust_store() -> TrustStore:
	"""
	Load the toolchain-shipped core trust store.

	This store is authoritative for reserved namespaces (`lang.*`, `std.*`,
	`drift.*`) and is not influenced by user/project trust files.
	"""
	default_path = Path(__file__).with_name("core_trust.json")
	if default_path.exists():
		return load_trust_store_json(default_path)
	raise ValueError(f"core trust store not found: {default_path}")


def merge_trust_stores(primary: TrustStore, secondary: TrustStore) -> TrustStore:
	"""
	Merge two trust stores deterministically.

	Primary entries win on key conflicts; namespace allowlists union.
	"""
	keys = dict(secondary.keys_by_kid)
	keys.update(primary.keys_by_kid)
	allowed: dict[str, set[str]] = {}
	for ns, kids in secondary.allowed_kids_by_namespace.items():
		allowed.setdefault(ns, set()).update(kids)
	for ns, kids in primary.allowed_kids_by_namespace.items():
		allowed.setdefault(ns, set()).update(kids)
	revoked = set(secondary.revoked_kids) | set(primary.revoked_kids)
	return TrustStore(keys_by_kid=keys, allowed_kids_by_namespace=allowed, revoked_kids=revoked)
