# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lang2.drift.crypto import b64_encode, b64_decode, compute_ed25519_kid, ed25519_sign_from_seed, sha256_hex


@dataclass(frozen=True)
class SigEntryV0:
	algo: str
	kid: str
	sig_b64: str
	pubkey_b64: str | None = None


@dataclass(frozen=True)
class SigSidecarV0:
	package_sha256: str
	signatures: list[SigEntryV0]


@dataclass(frozen=True)
class SignOptions:
	package_path: Path
	key_seed_path: Path
	out_path: Path
	add_signature: bool
	include_pubkey: bool


def _load_seed32(path: Path) -> bytes:
	"""
	Load a private signing key seed from a file.

	MVP format (pinned):
	- file contains base64 of raw 32-byte Ed25519 private seed (whitespace allowed).
	"""
	text = path.read_text(encoding="utf-8").strip()
	try:
		raw = b64_decode(text)
	except Exception as err:
		raise ValueError("invalid base64 in key seed file") from err
	if len(raw) != 32:
		raise ValueError("ed25519 private key seed must decode to 32 bytes")
	return raw


def _load_sig_sidecar_obj(path: Path) -> dict[str, Any]:
	obj = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(obj, dict):
		raise ValueError("signature sidecar must be a JSON object")
	if obj.get("format") != "dmir-pkg-sig" or obj.get("version") != 0:
		raise ValueError("unsupported signature sidecar format/version")
	if "package_sha256" not in obj:
		raise ValueError("signature sidecar missing package_sha256")
	sigs = obj.get("signatures")
	if not isinstance(sigs, list):
		raise ValueError("signature sidecar signatures must be an array")
	return obj


def load_sig_sidecar_v0(path: Path) -> SigSidecarV0:
	"""
Load a `pkg.dmp.sig` sidecar file.

This is pure parsing/validation; it does not consult trust policy.
	"""
	obj = _load_sig_sidecar_obj(path)
	pkg_sha = obj["package_sha256"]
	if not isinstance(pkg_sha, str):
		raise ValueError("signature sidecar package_sha256 must be a string")

	out: list[SigEntryV0] = []
	for raw in obj.get("signatures") or []:
		if not isinstance(raw, dict):
			raise ValueError("signature entry must be an object")
		algo = raw.get("algo")
		kid = raw.get("kid")
		sig = raw.get("sig")
		pub = raw.get("pubkey")
		if algo != "ed25519":
			raise ValueError("unsupported signature algorithm")
		if not isinstance(kid, str) or not kid:
			raise ValueError("signature entry missing kid")
		if not isinstance(sig, str) or not sig:
			raise ValueError("signature entry missing sig")
		if pub is not None and (not isinstance(pub, str) or not pub):
			raise ValueError("signature entry pubkey must be a non-empty string")
		out.append(SigEntryV0(algo=algo, kid=kid, sig_b64=sig, pubkey_b64=pub))

	return SigSidecarV0(package_sha256=pkg_sha, signatures=out)


def sign_package_v0(opts: SignOptions) -> None:
	if not opts.package_path.exists():
		raise ValueError(f"package not found: {opts.package_path}")

	pkg_bytes = opts.package_path.read_bytes()
	pkg_sha = sha256_hex(pkg_bytes)
	seed32 = _load_seed32(opts.key_seed_path)
	sig_raw, pub_raw = ed25519_sign_from_seed(priv_seed32=seed32, message=pkg_bytes)
	kid = compute_ed25519_kid(pub_raw)

	entry: dict[str, object] = {
		"algo": "ed25519",
		"kid": kid,
		"sig": b64_encode(sig_raw),
	}
	if opts.include_pubkey:
		entry["pubkey"] = b64_encode(pub_raw)

	if opts.add_signature:
		if not opts.out_path.exists():
			raise ValueError(f"signature sidecar not found: {opts.out_path}")
		obj = _load_sig_sidecar_obj(opts.out_path)
		if obj.get("package_sha256") != f"sha256:{pkg_sha}":
			raise ValueError("signature sidecar package_sha256 mismatch")
		obj["signatures"].append(entry)
	else:
		if opts.out_path.exists():
			# If the sidecar already exists, ensure it matches the package and then
			# overwrite it (deterministic "replace" semantics).
			obj = _load_sig_sidecar_obj(opts.out_path)
			if obj.get("package_sha256") != f"sha256:{pkg_sha}":
				raise ValueError("signature sidecar package_sha256 mismatch")
		obj = {
			"format": "dmir-pkg-sig",
			"version": 0,
			"package_sha256": f"sha256:{pkg_sha}",
			"signatures": [entry],
		}

	opts.out_path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
