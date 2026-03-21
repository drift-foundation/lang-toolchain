# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Signed envelope for package + author-profile binding.

The envelope is a deterministic byte string that the Ed25519 signature
covers.  It includes digests of both the package bytes and (optionally)
the author-profile bytes, so modifying either invalidates the signature.

Envelope v0 (legacy): signature covers raw package bytes directly.
Envelope v1: signature covers the canonical envelope string.
"""

from __future__ import annotations

from lang.drift.crypto import sha256_hex


ENVELOPE_HEADER = "drift-sig-envelope-v1"
ENVELOPE_HEADER_V2 = "drift-sig-envelope-v2"


def build_envelope(
	*,
	package_sha256_hex: str,
	author_profile_sha256_hex: str | None = None,
) -> bytes:
	"""
	Build the canonical envelope bytes that the signer signs.

	The format is line-oriented and deterministic:
	  drift-sig-envelope-v1\\n
	  package-sha256:<hex>\\n
	  author-profile-sha256:<hex>\\n   (only if profile is present)
	"""
	lines = [
		ENVELOPE_HEADER,
		f"package-sha256:{package_sha256_hex}",
	]
	if author_profile_sha256_hex:
		lines.append(f"author-profile-sha256:{author_profile_sha256_hex}")
	return ("\n".join(lines) + "\n").encode("utf-8")


def build_envelope_v2(
	*,
	package_sha256_hex: str,
	author_profile_sha256_hex: str | None = None,
	provenance_sha256_hex: str | None = None,
) -> bytes:
	"""
	Build the canonical v2 envelope bytes that the signer signs.

	V2 extends v1 with an optional provenance digest line:
	  drift-sig-envelope-v2\\n
	  package-sha256:<hex>\\n
	  author-profile-sha256:<hex>\\n   (only if profile is present)
	  provenance-sha256:<hex>\\n       (only if provenance is present)
	"""
	lines = [
		ENVELOPE_HEADER_V2,
		f"package-sha256:{package_sha256_hex}",
	]
	if author_profile_sha256_hex:
		lines.append(f"author-profile-sha256:{author_profile_sha256_hex}")
	if provenance_sha256_hex:
		lines.append(f"provenance-sha256:{provenance_sha256_hex}")
	return ("\n".join(lines) + "\n").encode("utf-8")


def build_envelope_from_bytes(
	*,
	package_bytes: bytes,
	author_profile_bytes: bytes | None = None,
) -> bytes:
	"""Convenience: build envelope from raw artifact bytes."""
	pkg_sha = sha256_hex(package_bytes)
	profile_sha = sha256_hex(author_profile_bytes) if author_profile_bytes else None
	return build_envelope(
		package_sha256_hex=pkg_sha,
		author_profile_sha256_hex=profile_sha,
	)
