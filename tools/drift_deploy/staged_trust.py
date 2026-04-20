# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Staged trust store overlay for smoke validation.

During smoke, the just-built package is not yet published and may not
be trusted by the baseline trust store.  The staged trust overlay adds
the build-time signer as a trusted key for the artifact's namespace,
enabling smoke consumers to verify the staged package.

Composition: staged_trust = baseline_trust ∪ staged_signer
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any


def build_staged_trust(
	*,
	baseline_trust_path: Path | None,
	signer_pubkey_raw: bytes,
	artifact_namespace: str,
	out_path: Path,
	dep_namespaces: list[str] | None = None,
) -> None:
	"""
	Build a staged trust store for smoke validation.

	Merges the baseline trust store (if any) with the staged signer's
	public key, authorized for `artifact_namespace.*` and any
	dependency namespaces needed for smoke compilation.

	Args:
		baseline_trust_path: Path to existing trust.json (or None).
		signer_pubkey_raw: 32-byte Ed25519 public key of the signer.
		artifact_namespace: Package namespace to authorize (e.g., "net.tls").
		out_path: Where to write the staged trust.json.
		dep_namespaces: Additional namespaces to authorize (resolved deps).
	"""
	# Load baseline or start empty.
	if baseline_trust_path and baseline_trust_path.exists():
		data = json.loads(baseline_trust_path.read_text(encoding="utf-8"))
	else:
		data = {
			"format": "drift-trust",
			"version": 0,
			"keys": {},
			"namespaces": {},
			"revoked": {},
		}

	# Compute kid for the signer.
	kid = "ed25519:" + base64.b64encode(
		hashlib.sha256(signer_pubkey_raw).digest()
	).decode("ascii")

	pubkey_b64 = base64.b64encode(signer_pubkey_raw).decode("ascii")

	# Add the signer key if not already present.
	keys = data.setdefault("keys", {})
	if kid not in keys:
		keys[kid] = {
			"algo": "ed25519",
			"pubkey": pubkey_b64,
		}

	# Authorize this kid for the artifact namespace.
	namespaces = data.setdefault("namespaces", {})
	ns_pattern = f"{artifact_namespace}.*"
	ns_kids = namespaces.get(ns_pattern, [])
	if kid not in ns_kids:
		ns_kids.append(kid)
	namespaces[ns_pattern] = ns_kids

	# Also authorize exact match for the package itself.
	exact_kids = namespaces.get(artifact_namespace, [])
	if kid not in exact_kids:
		exact_kids.append(kid)
	namespaces[artifact_namespace] = exact_kids

	# Authorize dependency namespaces (co-deployed packages signed with
	# the same key that the smoke compile needs to verify).
	for dep_ns in (dep_namespaces or []):
		dep_pattern = f"{dep_ns}.*"
		dep_kids = namespaces.get(dep_pattern, [])
		if kid not in dep_kids:
			dep_kids.append(kid)
		namespaces[dep_pattern] = dep_kids
		dep_exact = namespaces.get(dep_ns, [])
		if kid not in dep_exact:
			dep_exact.append(kid)
		namespaces[dep_ns] = dep_exact

	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(
		json.dumps(data, indent=2, ensure_ascii=False) + "\n",
		encoding="utf-8",
	)


def load_signing_key_seed(key_seed_path: Path) -> bytes:
	"""Load a 32-byte Ed25519 private seed from the canonical Drift
	key-file format (base64 text, possibly trailing newline).

	Decode goes through `lang.drift.crypto.b64_decode` so the strict
	`validate=True` semantics are shared with every other signing
	surface (artifact `.sig`, `.source-attestation`, smoke trust
	extraction).  Without `validate=True`, Python's base64 decoder
	silently ignores non-base64 characters — that would let a
	corrupted key file decode "successfully" to a different seed
	than the producer intended.

	Raises `ValueError` on bad base64, embedded non-base64 chars,
	wrong decoded length, or read failure so a single failure
	surface covers all signers.
	"""
	from lang.drift.crypto import b64_decode
	try:
		seed_text = key_seed_path.read_text(encoding="utf-8").strip()
	except OSError as err:
		raise ValueError(f"signing key file unreadable: {err}") from err
	try:
		seed_bytes = b64_decode(seed_text)
	except Exception as err:
		raise ValueError(f"signing key file does not contain valid base64: {err}") from err
	if len(seed_bytes) != 32:
		raise ValueError(
			f"signing key seed must decode to 32 bytes, got {len(seed_bytes)}"
		)
	return seed_bytes


def extract_pubkey_from_seed(key_seed_path: Path) -> bytes:
	"""
	Extract the Ed25519 public key from a seed file.

	The seed file contains a base64-encoded 32-byte Ed25519 seed.
	"""
	from lang.drift.crypto import ed25519_sign_from_seed

	seed_bytes = load_signing_key_seed(key_seed_path)
	# Sign a dummy message to extract the public key.
	_sig, pubkey = ed25519_sign_from_seed(priv_seed32=seed_bytes, message=b"")
	return pubkey
