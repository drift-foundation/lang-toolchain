# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lang.drift.crypto import b64_encode, b64_decode, compute_ed25519_kid, ed25519_sign_from_seed, sha256_hex


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
class SigEntry:
	"""Parsed signature entry with decoded raw bytes."""
	algo: str
	kid: str
	sig_raw: bytes
	pubkey_raw: bytes | None = None


@dataclass(frozen=True)
class SigFile:
	"""Parsed .sig sidecar with envelope metadata."""
	package_sha256_hex: str
	signatures: list[SigEntry]
	envelope_version: int = 0
	author_profile_sha256_hex: str | None = None


@dataclass(frozen=True)
class SignOptions:
	package_path: Path
	key_seed_path: Path | None
	key_seed_text: str | None
	out_path: Path
	add_signature: bool
	include_pubkey: bool
	author_profile_path: Path | None = None  # if set, sign envelope covering profile digest
	provenance_path: Path | None = None  # if set, include provenance digest in v2 envelope


def _decode_seed32(text: str) -> bytes:
	try:
		raw = b64_decode(text.strip())
	except Exception as err:
		raise ValueError("invalid base64 in key seed input") from err
	if len(raw) != 32:
		raise ValueError("ed25519 private key seed must decode to 32 bytes")
	return raw


def _load_seed32(path: Path) -> bytes:
	"""
	Load a private signing key seed from a file.

	MVP format (pinned):
	- file contains base64 of raw 32-byte Ed25519 private seed (whitespace allowed).
	"""
	text = path.read_text(encoding="utf-8")
	return _decode_seed32(text)


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
Load a `pkg.sig` sidecar file.

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


def load_sig_sidecar(path: Path) -> SigFile:
	"""Load a .sig sidecar with decoded raw bytes and envelope metadata."""
	obj = _load_sig_sidecar_obj(path)
	pkg_sha_raw = obj["package_sha256"]
	if not isinstance(pkg_sha_raw, str) or not pkg_sha_raw.startswith("sha256:"):
		raise ValueError("signature sidecar missing package_sha256")
	pkg_sha_hex = pkg_sha_raw.split("sha256:", 1)[1]

	entries: list[SigEntry] = []
	for s in obj.get("signatures") or []:
		if not isinstance(s, dict):
			continue
		algo = str(s.get("algo") or "")
		kid = str(s.get("kid") or "")
		sig_b64 = s.get("sig")
		pub_b64 = s.get("pubkey")
		if algo != "ed25519" or not isinstance(sig_b64, str) or not kid:
			continue
		sig_raw = b64_decode(sig_b64)
		if len(sig_raw) != 64:
			raise ValueError("ed25519 signature must be 64 bytes")
		pub_raw = None
		if isinstance(pub_b64, str):
			pub_raw = b64_decode(pub_b64)
			if len(pub_raw) != 32:
				raise ValueError("ed25519 pubkey must be 32 bytes")
		entries.append(SigEntry(algo=algo, kid=kid, sig_raw=sig_raw, pubkey_raw=pub_raw))
	if not entries:
		raise ValueError("signature sidecar contains no usable signatures")

	envelope_version = obj.get("envelope_version", 0)
	if not isinstance(envelope_version, int):
		raise ValueError("signature sidecar envelope_version must be an integer")
	ap_sha: str | None = None
	raw_ap = obj.get("author_profile_sha256")
	if raw_ap is not None:
		if not isinstance(raw_ap, str) or not raw_ap.startswith("sha256:"):
			raise ValueError("signature sidecar author_profile_sha256 must be 'sha256:<hex>'")
		ap_sha = raw_ap.split("sha256:", 1)[1]

	return SigFile(
		package_sha256_hex=pkg_sha_hex,
		signatures=entries,
		envelope_version=envelope_version,
		author_profile_sha256_hex=ap_sha,
	)


def sign_package_v0(opts: SignOptions) -> None:
	if not opts.package_path.exists():
		raise ValueError(f"package not found: {opts.package_path}")

	pkg_bytes = opts.package_path.read_bytes()
	pkg_sha = sha256_hex(pkg_bytes)
	if opts.key_seed_path is not None:
		seed32 = _load_seed32(opts.key_seed_path)
	elif opts.key_seed_text is not None:
		seed32 = _decode_seed32(opts.key_seed_text)
	else:
		raise ValueError("missing signing key seed input")

	# Determine what the signature covers.
	profile_sha: str | None = None
	provenance_sha: str | None = None
	has_profile = opts.author_profile_path and opts.author_profile_path.exists()
	has_provenance = opts.provenance_path and opts.provenance_path.exists()

	if has_provenance:
		# V2 envelope: includes provenance digest.
		if has_profile:
			profile_bytes = opts.author_profile_path.read_bytes()  # type: ignore[union-attr]
			profile_sha = sha256_hex(profile_bytes)
		provenance_bytes = opts.provenance_path.read_bytes()  # type: ignore[union-attr]
		provenance_sha = sha256_hex(provenance_bytes)
		from lang.drift.envelope import build_envelope_v2
		message = build_envelope_v2(
			package_sha256_hex=pkg_sha,
			author_profile_sha256_hex=profile_sha,
			provenance_sha256_hex=provenance_sha,
		)
		envelope_version = 2
	elif has_profile:
		profile_bytes = opts.author_profile_path.read_bytes()  # type: ignore[union-attr]
		profile_sha = sha256_hex(profile_bytes)
		from lang.drift.envelope import build_envelope
		message = build_envelope(
			package_sha256_hex=pkg_sha,
			author_profile_sha256_hex=profile_sha,
		)
		envelope_version = 1
	else:
		message = pkg_bytes
		envelope_version = 0

	sig_raw, pub_raw = ed25519_sign_from_seed(priv_seed32=seed32, message=message)
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
			obj = _load_sig_sidecar_obj(opts.out_path)
			if obj.get("package_sha256") != f"sha256:{pkg_sha}":
				raise ValueError("signature sidecar package_sha256 mismatch")
		obj = {
			"format": "dmir-pkg-sig",
			"version": 0,
			"package_sha256": f"sha256:{pkg_sha}",
			"signatures": [entry],
		}

	# Envelope metadata — lets verifiers reconstruct the signed payload.
	if envelope_version >= 1:
		obj["envelope_version"] = envelope_version
		if profile_sha:
			obj["author_profile_sha256"] = f"sha256:{profile_sha}"
		if provenance_sha:
			obj["provenance_sha256"] = f"sha256:{provenance_sha}"

	opts.out_path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
