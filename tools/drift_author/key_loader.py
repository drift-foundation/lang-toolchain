# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Author private-key seed loading.

This module is the ONLY place author private key material enters
the Python process from disk or environment.  The orch / cert
emission code (`tools/drift_deploy/cert_emit.py`) is forbidden by
the static import-boundary test from importing this module or
anything under `tools/drift_author/` -- the cert pipeline must be
able to certify a release WITHOUT ever holding the author key.

Loading conventions mirror `lang/drift/sign.py`'s `_load_seed32` /
`_decode_seed32` shape so users who know `drift sign --key-file`
already know how `drift-author publish --key-file` works.

Format:
  - On disk: a file containing the base64-encoded 32-byte Ed25519
    private seed (with optional leading/trailing whitespace).
  - In env / CLI text: the same base64 string passed directly.

Returns a 32-byte `bytes` value suitable for
`lang.drift.crypto.ed25519_sign_from_seed`.
"""

from __future__ import annotations

import base64
from pathlib import Path


_ED25519_SEED_LEN = 32


def _decode_seed_text(text: str, *, source: str) -> bytes:
	"""Decode a base64-encoded seed string.

	`source` names where the text came from (path, env var, etc.)
	so a decode error points back at the offending source rather
	than surfacing a bare `binascii.Error`.
	"""
	stripped = text.strip()
	if not stripped:
		raise ValueError(
			f"author seed from {source} is empty (expected "
			f"base64-encoded 32-byte Ed25519 private seed)"
		)
	try:
		raw = base64.b64decode(stripped.encode("ascii"), validate=True)
	except (ValueError, base64.binascii.Error) as err:
		raise ValueError(
			f"author seed from {source}: invalid base64: {err}"
		) from err
	if len(raw) != _ED25519_SEED_LEN:
		raise ValueError(
			f"author seed from {source}: expected {_ED25519_SEED_LEN} bytes "
			f"after base64 decode, got {len(raw)}"
		)
	return raw


def decode_author_seed32(text: str) -> bytes:
	"""Decode an in-memory base64 seed string."""
	return _decode_seed_text(text, source="<inline text>")


def load_author_seed32(path: Path) -> bytes:
	"""Read and decode an author private-key seed file.

	The file must contain a single base64 line (with optional
	whitespace).  Returns 32 raw bytes.  Any error names the path
	so the user can locate the bad file.
	"""
	if not path.is_file():
		raise FileNotFoundError(f"author seed file not found: {path}")
	try:
		text = path.read_text(encoding="utf-8")
	except OSError as err:
		raise ValueError(f"author seed file {path}: {err}") from err
	return _decode_seed_text(text, source=str(path))
