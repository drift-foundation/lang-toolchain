# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lang2.drift.crypto import b64_encode, b64_decode, compute_ed25519_kid, ed25519_sign_from_seed, sha256_hex


@dataclass(frozen=True)
class SignOptions:
	package_path: Path
	key_seed_path: Path
	out_path: Path
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


def sign_package_v0(opts: SignOptions) -> None:
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

	obj = {
		"format": "dmir-pkg-sig",
		"version": 0,
		"package_sha256": f"sha256:{pkg_sha}",
		"signatures": [entry],
	}
	opts.out_path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

