# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Cert-claim emit (orch / certifier side).

Pipeline counterpart to `tools/drift_author/author_publish.py`.  A
cert claim binds:

  - the exact artifact bytes (`artifact_sha256`),
  - the source content id stamped at build time (compared, never
    recomputed from binary -- G1),
  - the toolchain identity that produced the artifact,
  - the FULL resolved transitive dep graph (every consumer dep must
    be attested -- O3),
  - the cert suite identity + result (per O4, consumers may pin
    `--require-cert-suite`).

Multiple INDEPENDENT certifiers attesting the same release each
write a SEPARATE sidecar (`<pkg>.cert-claim.<kid>.json`) per O1.
Multi-signature on a single sidecar is reserved for key rotation
under one certifier identity.

Author-key isolation: this module MUST NOT import anything under
`tools/drift_author/`.  The orch pipeline holds a CERTIFIER key,
never an author key.  A static import-boundary test
(`lang/tests/packages/test_author_key_boundary.py`) enforces this
contract by AST walk; see that file's docstring for the rationale.
The cert pipeline therefore physically cannot read author key
material via this module's call graph.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from lang.driftc.packages.cert_claim_v1 import (
	CertClaim,
	CertClaimBody,
	add_signature as _add_signature,
	dump_cert_claim_json,
	load_cert_claim_json,
	make_cert_claim,
)
from lang.driftc.packages.sidecar_naming import cert_claim_filename


_ED25519_SEED_LEN = 32


# ── Certifier key loading (separate code path from author keys) ────


def _decode_cert_seed_text(text: str, *, source: str) -> bytes:
	"""Decode a base64-encoded certifier private seed.

	A deliberate sibling of `tools.drift_author.key_loader._decode_seed_text`
	-- same shape, separate module.  Loading author seeds from this
	function (or vice versa) is impossible by import: this module
	cannot reach `tools/drift_author/` and is the only file allowed
	to read certifier key material in the deploy pipeline.
	"""
	stripped = text.strip()
	if not stripped:
		raise ValueError(
			f"certifier seed from {source} is empty (expected "
			f"base64-encoded 32-byte Ed25519 private seed)"
		)
	try:
		raw = base64.b64decode(stripped.encode("ascii"), validate=True)
	except (ValueError, base64.binascii.Error) as err:
		raise ValueError(
			f"certifier seed from {source}: invalid base64: {err}"
		) from err
	if len(raw) != _ED25519_SEED_LEN:
		raise ValueError(
			f"certifier seed from {source}: expected {_ED25519_SEED_LEN} "
			f"bytes after base64 decode, got {len(raw)}"
		)
	return raw


def decode_cert_seed32(text: str) -> bytes:
	"""Decode an in-memory base64 certifier seed string."""
	return _decode_cert_seed_text(text, source="<inline text>")


def load_cert_seed32(path: Path) -> bytes:
	"""Read and decode a certifier private-key seed file."""
	if not path.is_file():
		raise FileNotFoundError(f"certifier seed file not found: {path}")
	try:
		text = path.read_text(encoding="utf-8")
	except OSError as err:
		raise ValueError(f"certifier seed file {path}: {err}") from err
	return _decode_cert_seed_text(text, source=str(path))


# ── Cert-claim emit ────────────────────────────────────────────────


@dataclass(frozen=True)
class SignCertClaimOptions:
	"""Inputs to `sign_and_write_cert_claim`.

	`body` is pre-assembled by the caller (deploy pipeline or CLI)
	with the dep_graph already populated from the consumer's
	resolved closure -- the helper does NOT discover deps on its
	own, since the orch pipeline already has that view from its
	lockfile / resolver.

	`seed32` is the 32-byte certifier seed loaded via
	`load_cert_seed32`.  This module is the only sanctioned source
	of certifier key material in the deploy pipeline.

	`sidecar_dir` is the directory the file is written into.  The
	filename includes the certifier's kid (so two certifiers can
	coexist for the same release per O1).
	"""
	body: CertClaimBody
	seed32: bytes
	sidecar_dir: Path
	overwrite: bool = False


def _claim_path_for(seed32: bytes, body: CertClaimBody, sidecar_dir: Path) -> Path:
	"""Compute the canonical cert-claim filename for the kid that
	will sign this body."""
	# Re-derive the kid from the seed so the filename matches the
	# signature's `kid` field exactly.  Inlining the derivation
	# (rather than calling `make_cert_claim` first) keeps the path
	# computation independent of canonical-bytes side effects.
	from lang.drift.crypto import compute_ed25519_kid, ed25519_sign_from_seed
	# `ed25519_sign_from_seed` returns (sig, pubkey); we only need
	# the pubkey to compute the kid, so sign an empty message and
	# discard the sig.
	_sig, pubkey_raw = ed25519_sign_from_seed(priv_seed32=seed32, message=b"")
	kid = compute_ed25519_kid(pubkey_raw)
	return sidecar_dir / cert_claim_filename(body.package_id, kid)


def sign_and_write_cert_claim(opts: SignCertClaimOptions) -> Path:
	"""Build a single-signature cert claim and write it to the
	canonical sidecar path for this certifier's kid.  Returns the
	written path.

	Raises FileExistsError when this exact (package_id, kid) sidecar
	already exists and `overwrite=False`.  A different certifier
	(different kid) on the same release is NOT a conflict and
	writes a separate file.
	"""
	if not opts.sidecar_dir.is_dir():
		raise FileNotFoundError(
			f"cert-claim sidecar directory does not exist: {opts.sidecar_dir}"
		)
	out_path = _claim_path_for(opts.seed32, opts.body, opts.sidecar_dir)
	if out_path.exists() and not opts.overwrite:
		raise FileExistsError(
			f"cert claim sidecar already exists: {out_path}.  "
			f"Use add_cert_signature_to_claim_file() to add a "
			f"rotation-co-signature, or pass overwrite=True to replace."
		)
	claim = make_cert_claim(opts.body, opts.seed32)
	out_path.write_text(dump_cert_claim_json(claim), encoding="utf-8")
	return out_path


def add_cert_signature_to_claim_file(
	*,
	sidecar_dir: Path,
	package_id: str,
	current_certifier_kid: str,
	seed32: bytes,
) -> Path:
	"""Append a key-rotation signature to an existing cert-claim
	sidecar.  The appended signature signs the SAME body bytes as
	the current signature.

	`current_certifier_kid` names the existing sidecar.  An
	independent certifier (different identity) does NOT append --
	they write a new file per O1.
	"""
	in_path = sidecar_dir / cert_claim_filename(package_id, current_certifier_kid)
	if not in_path.is_file():
		raise FileNotFoundError(
			f"no existing cert claim at {in_path}; use "
			f"sign_and_write_cert_claim() for the first signature"
		)
	existing: CertClaim = load_cert_claim_json(
		in_path.read_text(encoding="utf-8")
	)
	updated: CertClaim = _add_signature(existing, seed32)
	in_path.write_text(dump_cert_claim_json(updated), encoding="utf-8")
	return in_path
