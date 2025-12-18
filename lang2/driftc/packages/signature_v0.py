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
To avoid adding heavyweight Python crypto dependencies in MVP, verification
delegates to the system OpenSSL binary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

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
	obj = json.loads(path.read_text(encoding="utf-8"))
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
		sig_raw = _b64_decode(sig_b64)
		pub_raw = _b64_decode(pub_b64) if isinstance(pub_b64, str) else None
		entries.append(SigEntry(algo=algo, kid=kid, sig_raw=sig_raw, pubkey_raw=pub_raw))
	if not entries:
		raise ValueError("signature sidecar contains no usable signatures")
	return SigFile(package_sha256_hex=pkg_sha_hex, signatures=entries)


def _ed25519_pubkey_pem_from_raw(pubkey_raw: bytes) -> bytes:
	"""
	Build a minimal Ed25519 SubjectPublicKeyInfo PEM from raw public key bytes.

	DER prefix for Ed25519 SPKI (RFC 8410):
	  SEQUENCE {
	    SEQUENCE { OID 1.3.101.112 }
	    BIT STRING (0 unused bits) <32-byte key>
	  }

	We construct DER manually to keep MVP dependency-free.
	"""
	if len(pubkey_raw) != 32:
		raise ValueError("ed25519 pubkey must be 32 bytes")
	# DER: 30 2a 30 05 06 03 2b 65 70 03 21 00 <32 bytes>
	der = bytes.fromhex("302a300506032b6570032100") + pubkey_raw
	b64 = base64.b64encode(der)
	lines = [b"-----BEGIN PUBLIC KEY-----"]
	for i in range(0, len(b64), 64):
		lines.append(b64[i : i + 64])
	lines.append(b"-----END PUBLIC KEY-----")
	return b"\n".join(lines) + b"\n"


def verify_ed25519_openssl(*, pubkey_raw: bytes, message: bytes, signature_raw: bytes) -> bool:
	"""
	Verify an Ed25519 signature using the system OpenSSL binary.

	Returns True on success, False on verification failure.
	Raises on tool errors / missing OpenSSL.
	"""
	openssl = shutil.which("openssl")
	if openssl is None:
		raise ValueError("openssl not available for ed25519 verification")
	pub_pem = _ed25519_pubkey_pem_from_raw(pubkey_raw)
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		pub_path = tdp / "pub.pem"
		msg_path = tdp / "msg.bin"
		sig_path = tdp / "sig.bin"
		pub_path.write_bytes(pub_pem)
		msg_path.write_bytes(message)
		sig_path.write_bytes(signature_raw)
		res = subprocess.run(
			[
				openssl,
				"pkeyutl",
				"-verify",
				"-pubin",
				"-inkey",
				str(pub_path),
				"-sigfile",
				str(sig_path),
				"-in",
				str(msg_path),
			],
			capture_output=True,
			text=True,
		)
		return res.returncode == 0


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
	for entry in sf.signatures:
		if entry.algo != "ed25519":
			continue
		if entry.kid in trust.revoked_kids:
			continue
		pub_raw = None
		pub_source = "none"
		tk = trust.keys_by_kid.get(entry.kid)
		if tk is not None:
			pub_raw = tk.pubkey_raw
			pub_source = "trust"
		elif entry.pubkey_raw is not None:
			pub_raw = entry.pubkey_raw
			pub_source = "sidecar"
			# Ensure provided pubkey matches kid deterministically.
			if compute_ed25519_kid(pub_raw) != entry.kid:
				continue
		if pub_raw is None:
			continue
		try:
			ok = verify_ed25519_openssl(pubkey_raw=pub_raw, message=pkg_bytes, signature_raw=entry.sig_raw)
		except Exception as err:
			raise ValueError(f"signature verification failed: {err}") from err
		if ok:
			verified_kids[entry.kid] = pub_source

	if not verified_kids:
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
