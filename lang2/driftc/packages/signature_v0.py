# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Package signature sidecar verification (v0).

Pinned policy:
- Signature scheme (v1): Ed25519.
- Public keys are encoded as base64 of raw 32-byte keys.
- Signatures are encoded as base64 of raw 64-byte signatures.
- The sidecar file is JSON, named `<pkg>.dmp.sig` (e.g. `pkg.dmp.sig`).
- Signatures cover the *exact bytes* of the uncompressed DMIR-PKG container.
  If the package is transported compressed (e.g. `.zst`), it must be
  decompressed before verification. Compression is not part of the signed
  payload.

This module implements verification using local tooling only (offline).
Verification uses `cryptography` (Ed25519) and does not shell out to external
tools. This keeps signature verification self-contained and avoids relying on
system binaries for security-critical checks.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from lang2.driftc.packages.trust_v0 import TrustStore


def sha256_hex(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _b64_encode(data: bytes) -> str:
	return base64.b64encode(data).decode("ascii")


def _b64_decode(text: str) -> bytes:
	return base64.b64decode(text.encode("ascii"), validate=True)


def compute_ed25519_kid(pubkey_raw: bytes) -> str:
	"""
	Compute key id (kid) for an Ed25519 public key.

	Pinned scheme:
	  kid = "ed25519:" + base64(sha256(pubkey_raw))
	"""
	d = hashlib.sha256(pubkey_raw).digest()
	return "ed25519:" + _b64_encode(d)


@dataclass(frozen=True)
class SigEntry:
	algo: str
	kid: str
	sig_raw: bytes
	pubkey_raw: Optional[bytes] = None
	pubkey_source: str = "none"  # "trust" | "sidecar"


@dataclass(frozen=True)
class SigFile:
	package_sha256_hex: str
	signatures: list[SigEntry]


def load_sig_sidecar(path: Path) -> SigFile:
	"""
	Load and validate a `.dmp.sig` sidecar.

	Minimal schema (pinned):
	{
	  "format": "dmir-pkg-sig",
	  "version": 0,
	  "package_sha256": "sha256:<hex>",
	  "signatures": [
	    { "algo": "ed25519", "kid": "...", "pubkey": "<b64>", "sig": "<b64>" }
	  ]
	}
	"""
	try:
		obj = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as err:
		raise ValueError("signature sidecar invalid JSON") from err
	if not isinstance(obj, dict):
		raise ValueError("signature sidecar must be a JSON object")
	if obj.get("format") != "dmir-pkg-sig" or obj.get("version") != 0:
		raise ValueError("unsupported signature sidecar format/version")
	pkg_sha = obj.get("package_sha256")
	if not isinstance(pkg_sha, str) or not pkg_sha.startswith("sha256:"):
		raise ValueError("signature sidecar missing package_sha256")
	pkg_sha_hex = pkg_sha.split("sha256:", 1)[1]
	sigs_obj = obj.get("signatures")
	if not isinstance(sigs_obj, list):
		raise ValueError("signature sidecar missing signatures list")
	entries: list[SigEntry] = []
	for s in sigs_obj:
		if not isinstance(s, dict):
			continue
		algo = str(s.get("algo") or "")
		kid = str(s.get("kid") or "")
		sig_b64 = s.get("sig")
		pub_b64 = s.get("pubkey")
		if algo != "ed25519" or not isinstance(sig_b64, str) or not kid:
			continue
		try:
			sig_raw = _b64_decode(sig_b64)
		except Exception as err:
			raise ValueError("signature sidecar contains invalid base64 in 'sig'") from err
		if len(sig_raw) != 64:
			raise ValueError("ed25519 signature must be 64 bytes")
		pub_raw = None
		if isinstance(pub_b64, str):
			try:
				pub_raw = _b64_decode(pub_b64)
			except Exception as err:
				raise ValueError("signature sidecar contains invalid base64 in 'pubkey'") from err
			if len(pub_raw) != 32:
				raise ValueError("ed25519 pubkey must be 32 bytes")
		entries.append(SigEntry(algo=algo, kid=kid, sig_raw=sig_raw, pubkey_raw=pub_raw))
	if not entries:
		raise ValueError("signature sidecar contains no usable signatures")
	return SigFile(package_sha256_hex=pkg_sha_hex, signatures=entries)


def verify_ed25519(*, pubkey_raw: bytes, message: bytes, signature_raw: bytes) -> bool:
	"""
	Verify an Ed25519 signature.

	Returns True on success, False on verification failure.
	Raises on internal decoding/usage errors.
	"""
	try:
		key = Ed25519PublicKey.from_public_bytes(pubkey_raw)
	except Exception as err:
		raise ValueError("invalid ed25519 public key bytes") from err
	try:
		key.verify(signature_raw, message)
		return True
	except InvalidSignature:
		return False


def verify_package_signatures(
	*,
	pkg_path: Path,
	pkg_bytes: bytes,
	pkg_manifest: dict[str, Any],
	trust: TrustStore,
	require_signatures: bool,
	allow_unsigned_roots: list[Path],
) -> None:
	"""
	Verify signature/trust policy for a loaded package.

	Raises ValueError on policy failure.
	"""
	pkg_sha = sha256_hex(pkg_bytes)

	allow_unsigned = False
	if not require_signatures and bool(pkg_manifest.get("unsigned")):
		abs_pkg = pkg_path.resolve()
		for r in allow_unsigned_roots:
			try:
				abs_pkg.relative_to(r.resolve())
			except Exception:
				continue
			allow_unsigned = True
			break

	sig_path = Path(str(pkg_path) + ".sig")
	if not sig_path.exists():
		if allow_unsigned:
			return
		raise ValueError(f"missing signature sidecar for package '{pkg_path}'")

	sf = load_sig_sidecar(sig_path)
	if sf.package_sha256_hex != pkg_sha:
		raise ValueError("signature sidecar package_sha256 mismatch")

	# Verify signatures and collect the set of verified kids.
	verified_kids: dict[str, str] = {}  # kid -> pubkey source ("trust"|"sidecar")
	seen_ed25519 = 0
	seen_revoked_kids: set[str] = set()
	for entry in sf.signatures:
		if entry.algo != "ed25519":
			continue
		seen_ed25519 += 1
		if entry.kid in trust.revoked_kids:
			seen_revoked_kids.add(entry.kid)
			continue
		pub_raw = None
		pub_source = "none"
		tk = trust.keys_by_kid.get(entry.kid)
		if tk is not None:
			pub_raw = tk.pubkey_raw
			pub_source = "trust"
		# MVP policy: driftc does not perform TOFU by accepting unknown public keys
		# from signature sidecars. The trust store is authoritative for which keys
		# are trusted and what their public key bytes are. Sidecars may still
		# include `pubkey` as a convenience for tooling, but driftc verifies only
		# against trust-store keys.

		if pub_raw is None:
			continue

		try:
			ok = verify_ed25519(pubkey_raw=pub_raw, message=pkg_bytes, signature_raw=entry.sig_raw)
		except Exception as err:
			raise ValueError(f"signature verification failed: {err}") from err
		if ok:
			verified_kids[entry.kid] = pub_source

	if not verified_kids:
		# Provide a clearer error when the sidecar is present but all candidate
		# signatures are revoked by the local trust store.
		if seen_ed25519 > 0 and len(seen_revoked_kids) == seen_ed25519:
			k = sorted(seen_revoked_kids)[0]
			raise ValueError(f"signature key '{k}' is revoked")
		raise ValueError("no valid signatures for package")

	# Enforce per-module namespace trust policy.
	modules = pkg_manifest.get("modules")
	if not isinstance(modules, list):
		raise ValueError("package manifest missing modules list")
	for m in modules:
		if not isinstance(m, dict):
			continue
		mid = m.get("module_id")
		if not isinstance(mid, str):
			continue
		allowed = trust.allowed_kids_for_module(mid)
		if not allowed:
			raise ValueError(f"no trusted keys configured for module namespace of '{mid}'")
		# Core namespaces must not be TOFU'd via sidecar pubkeys.
		is_core = mid.startswith(("std.", "lang.", "drift."))
		ok = False
		for kid, source in verified_kids.items():
			if kid not in allowed:
				continue
			if is_core and source != "trust":
				continue
			ok = True
			break
		if not ok:
			raise ValueError(f"package signatures are not trusted for module '{mid}'")
