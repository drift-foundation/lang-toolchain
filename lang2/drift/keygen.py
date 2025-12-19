# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lang2.drift.crypto import b64_encode, compute_ed25519_kid


@dataclass(frozen=True)
class KeygenOptions:
	out_path: Path
	print_pubkey: bool
	print_kid: bool


def keygen_ed25519_seed(opts: KeygenOptions) -> None:
	"""
	Generate a new Ed25519 private key seed file.

	MVP format (pinned):
	- the file contains base64 of raw 32-byte Ed25519 private seed, followed by a newline.
	"""
	seed32 = os.urandom(32)
	opts.out_path.parent.mkdir(parents=True, exist_ok=True)
	opts.out_path.write_text(b64_encode(seed32) + "\n", encoding="utf-8")

	if opts.print_pubkey or opts.print_kid:
		priv = Ed25519PrivateKey.from_private_bytes(seed32)
		pub_raw = priv.public_key().public_bytes_raw()
		if opts.print_pubkey:
			print(b64_encode(pub_raw))
		if opts.print_kid:
			print(compute_ed25519_kid(pub_raw))

