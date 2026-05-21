# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Author-claim emit (write + multi-author append).

Thin wrapper over `lang.driftc.packages.author_claim_v1`'s sign +
canonicalize primitives.  Provides the two operations callers need
to publish an author claim:

  - `sign_and_write_author_claim(opts)` — produce a fresh sidecar
    with a single signature.  Refuses to overwrite an existing
    sidecar unless `overwrite=True`.

  - `add_signature_to_claim_file(...)` — read the existing sidecar,
    append a signature from another author's key, write back.
    Multi-author releases per O8.  Reads + writes the SAME body
    bytes; the appended signature signs the original body.

Filename placement uses `sidecar_naming.author_claim_filename`
(percent-escapes the package_id when needed) so the discovery
side (`verify_v1.discover_author_claim_path`) finds the file at
the canonical name.

This module never touches the cert claim.  The cert claim has a
separate emit path under `tools/drift_deploy/cert_emit.py` that
runs with a DIFFERENT key role.  The static import-boundary check
forbids `tools.drift_deploy.*` from reaching anything here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lang.driftc.packages.author_claim_v1 import (
	AuthorClaim,
	AuthorClaimBody,
	add_signature as _add_signature,
	dump_author_claim_json,
	load_author_claim_json,
	make_author_claim,
)
from lang.driftc.packages.sidecar_naming import (
	author_claim_filename,
	author_pubkey_filename,
)


@dataclass(frozen=True)
class SignAuthorClaimOptions:
	"""Inputs to `sign_and_write_author_claim`.

	`body` is pre-assembled by the caller (CLI or pipeline) since
	`AuthorClaimBody` carries author-level identity fields that
	belong to the publishing workflow, not this helper.

	`seed32` is the 32-byte raw Ed25519 private seed.  The caller
	is expected to have loaded it via
	`tools.drift_author.key_loader` -- the only sanctioned source
	of author key material.

	`sidecar_dir` is the directory the file is written into; the
	filename is computed from `body.package_id` via
	`author_claim_filename`.

	`overwrite=False` (default) refuses to clobber an existing
	sidecar.  Multi-author releases append via
	`add_signature_to_claim_file` instead.  Setting
	`overwrite=True` is only correct for republish-from-scratch
	workflows; it discards any existing signatures.
	"""
	body: AuthorClaimBody
	seed32: bytes
	sidecar_dir: Path
	overwrite: bool = False


def _claim_path(sidecar_dir: Path, package_id: str) -> Path:
	return sidecar_dir / author_claim_filename(package_id)


def sign_and_write_author_claim(opts: SignAuthorClaimOptions) -> Path:
	"""Build a single-signature author claim and write it to the
	canonical sidecar path.  Returns the written claim path.

	Also writes a sibling `<pkg>.author-pubkey.b64` companion file
	carrying the base64-encoded 32-byte Ed25519 pubkey of the signer.
	The author claim itself does NOT carry pubkey bytes inline (kids
	resolve through the trust store at verify time), so this
	companion is what makes `drift trust bootstrap` able to derive
	the trust store from on-disk sidecars without an extra manual
	step.  Pubkeys are public; the file is safe to commit alongside
	the claim.

	Raises FileExistsError when a sidecar already exists and
	`overwrite=False`.  Validates the body shape before signing so
	a bad value-shape can never produce a sidecar the v1 loader
	would later reject (the loader runs the same validator).
	"""
	if not opts.sidecar_dir.is_dir():
		raise FileNotFoundError(
			f"author-claim sidecar directory does not exist: "
			f"{opts.sidecar_dir}"
		)
	out_path = _claim_path(opts.sidecar_dir, opts.body.package_id)
	if out_path.exists() and not opts.overwrite:
		raise FileExistsError(
			f"author claim sidecar already exists: {out_path}.  "
			f"Use add_signature_to_claim_file() for multi-author "
			f"co-signing, or pass overwrite=True to replace."
		)
	claim = make_author_claim(opts.body, opts.seed32)
	out_path.write_text(dump_author_claim_json(claim), encoding="utf-8")

	# Derive + write the pubkey companion next to the claim.  The kid
	# is computable from the pubkey via `compute_ed25519_kid`, so we
	# don't need to write it separately.  Use a fresh sign with empty
	# message just to recover pub_raw (the canonical pubkey-from-seed
	# helper in `lang.drift.crypto`).
	import base64
	from lang.drift.crypto import ed25519_sign_from_seed
	_sig, pub_raw = ed25519_sign_from_seed(priv_seed32=opts.seed32, message=b"")
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pubkey_path = opts.sidecar_dir / author_pubkey_filename(opts.body.package_id)
	pubkey_path.write_text(pub_b64 + "\n", encoding="utf-8")

	return out_path


def add_signature_to_claim_file(
	*,
	sidecar_dir: Path,
	package_id: str,
	seed32: bytes,
) -> Path:
	"""Read an existing author-claim sidecar for `package_id`, add
	a signature from a second author's key, write back.

	The appended signature signs the SAME body bytes as the
	existing signatures -- if a co-author wants to change the body
	they must agree with the lead author and republish from
	scratch.  This enforces "every signer co-signs the same source
	identity" without coordinating dataclass construction across
	processes.

	Raises FileNotFoundError if no sidecar exists yet (call
	`sign_and_write_author_claim` first).
	"""
	in_path = _claim_path(sidecar_dir, package_id)
	if not in_path.is_file():
		raise FileNotFoundError(
			f"no existing author claim at {in_path}; use "
			f"sign_and_write_author_claim() for the first signature"
		)
	existing: AuthorClaim = load_author_claim_json(
		in_path.read_text(encoding="utf-8")
	)
	updated: AuthorClaim = _add_signature(existing, seed32)
	in_path.write_text(dump_author_claim_json(updated), encoding="utf-8")
	return in_path


def find_existing_author_claim(
	sidecar_dir: Path, *, package_id: str,
) -> Optional[Path]:
	"""Return the path of an existing author-claim sidecar for
	`package_id`, or None.  Convenience for tooling that wants to
	check before deciding new-vs-append.
	"""
	p = _claim_path(sidecar_dir, package_id)
	return p if p.is_file() else None
