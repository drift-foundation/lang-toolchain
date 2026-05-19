# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Canonical sidecar filename naming for trust-v1 claims.

Single home for filename helpers used by BOTH the emit side
(`author_claim_v1`, `cert_claim_v1`, `drift author-publish`,
`drift cert-publish`) AND the discovery side (`verify_v1`).
Without a single home the two halves can disagree on escaping
rules: an emitter that percent-encodes a package id and a
discoverer that searches with the raw id will silently miss
each other's files.

Escaping rule: any character outside `[A-Za-z0-9._-]` is
percent-encoded (`%HH`).  Letters, digits, dot, underscore, and
hyphen pass through.  This keeps the common case readable while
preserving filesystem safety against `/`, `:`, spaces, and any
other character that could shape the filename into a path or
break on hosts with restrictive filename grammars.

Both `package_id` and `certifier_kid` are escaped this way.
Readers MUST parse the canonical package_id and kid from the
file body's signed fields, not from the filename — the filename
is a disambiguator on disk, not a trust input.

Canonical sidecar names (v1):
  - author claim: `<safe_pkg>.author-claim`
  - cert claim:   `<safe_pkg>.cert-claim.<safe_kid>.json`

The author claim is per-release singleton (no kid disambiguator
because exactly one author claim per release; multi-author
releases use the `signatures: [...]` array INSIDE that one file).
"""

from __future__ import annotations

import re


_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

_AUTHOR_CLAIM_SUFFIX = ".author-claim"
_CERT_CLAIM_INFIX = ".cert-claim."


def filename_escape_segment(s: str) -> str:
	"""Percent-encode any character outside `[A-Za-z0-9._-]`.

	Pure-ASCII alphanumerics + `._-` pass through unchanged for
	readability; everything else becomes `%HH`.  Used by both the
	emit side (filename generation) and the discovery side
	(prefix matching) so the two stay aligned.
	"""
	def _enc(c: str) -> str:
		return f"%{ord(c):02X}"
	return "".join(_enc(c) if _FILENAME_UNSAFE.match(c) else c for c in s)


def _require_non_empty(name: str, value: str) -> None:
	if not isinstance(value, str) or not value:
		raise ValueError(f"{name} must be a non-empty string; got {value!r}")


def author_claim_filename(package_id: str) -> str:
	"""Canonical author-claim sidecar filename: `<safe_pkg>.author-claim`.

	Single file per package release (per O8 — author claims are
	standalone per-release).  Package id is escaped per
	`filename_escape_segment`.
	"""
	_require_non_empty("author_claim_filename: package_id", package_id)
	return f"{filename_escape_segment(package_id)}{_AUTHOR_CLAIM_SUFFIX}"


def cert_claim_filename(package_id: str, certifier_kid: str) -> str:
	"""Canonical cert-claim sidecar filename:
	`<safe_pkg>.cert-claim.<safe_kid>.json`.

	BOTH `package_id` and `certifier_kid` are escaped per
	`filename_escape_segment` so characters like `/`, `:`, `=`, or
	spaces cannot turn the filename into a path or break naming on
	hosts that reject them.  Per O1, the kid is the FULL kid (no
	short-prefix collision risk).
	"""
	_require_non_empty("cert_claim_filename: package_id", package_id)
	_require_non_empty("cert_claim_filename: certifier_kid", certifier_kid)
	return (
		f"{filename_escape_segment(package_id)}{_CERT_CLAIM_INFIX}"
		f"{filename_escape_segment(certifier_kid)}.json"
	)


def cert_claim_filename_prefix(package_id: str) -> str:
	"""Return the on-disk filename prefix shared by every cert claim
	for a given package release.  Discovery iterates a directory
	and filters by this prefix to find all per-certifier claims.

	The prefix uses the SAME escaping as `cert_claim_filename` so
	an emitter and a discoverer always agree.
	"""
	_require_non_empty("cert_claim_filename_prefix: package_id", package_id)
	return f"{filename_escape_segment(package_id)}{_CERT_CLAIM_INFIX}"
