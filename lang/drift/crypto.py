# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def sha256_hex(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def b64_encode(data: bytes) -> str:
	return base64.b64encode(data).decode("ascii")


def b64_decode(text: str) -> bytes:
	return base64.b64decode(text.encode("ascii"), validate=True)


def compute_ed25519_kid(pubkey_raw: bytes) -> str:
	"""
	Compute key id (kid) for an Ed25519 public key.

	Pinned scheme (must match driftc verifier):
	  kid = "ed25519:" + base64(sha256(pubkey_raw))
	"""
	return "ed25519:" + b64_encode(hashlib.sha256(pubkey_raw).digest())


def ed25519_public_bytes_raw(pubkey) -> bytes:
	try:
		return pubkey.public_bytes_raw()
	except AttributeError:
		return pubkey.public_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PublicFormat.Raw,
		)


def verify_ed25519(*, pubkey_raw: bytes, message: bytes, signature_raw: bytes) -> bool:
	"""
	Verify an Ed25519 signature.

	Returns True on success, False on verification failure.
	Raises on internal decoding/usage errors.
	"""
	from cryptography.exceptions import InvalidSignature
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
	try:
		key = Ed25519PublicKey.from_public_bytes(pubkey_raw)
	except Exception as err:
		raise ValueError("invalid ed25519 public key bytes") from err
	try:
		key.verify(signature_raw, message)
		return True
	except InvalidSignature:
		return False


def ed25519_sign_from_seed(*, priv_seed32: bytes, message: bytes) -> tuple[bytes, bytes]:
	"""
	Sign `message` with an Ed25519 private key seed.

	Args:
	  priv_seed32: raw 32-byte Ed25519 private seed.
	  message: bytes to sign (package bytes).

	Returns:
	  (sig_raw_64, pubkey_raw_32)
	"""
	if len(priv_seed32) != 32:
		raise ValueError("ed25519 private key seed must be 32 bytes")
	priv = Ed25519PrivateKey.from_private_bytes(priv_seed32)
	sig = priv.sign(message)
	pub = ed25519_public_bytes_raw(priv.public_key())
	return sig, pub
