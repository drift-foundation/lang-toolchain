# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Author profile — create and load .author-profile files.

An author profile is a JSON file containing a public key, publisher
metadata, and intended namespace claims.  Publishers create profiles
via ``drift init``; consumers inspect them and choose to trust them
via ``drift trust``.

When published alongside a signed package, the profile is
cryptographically bound to the package signature via a signed
envelope that covers both the package digest and the profile digest.
The ``package`` field in the profile names the artifact it is bound to,
enabling deterministic sidecar resolution without directory heuristics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lang.drift.crypto import b64_decode, b64_encode, compute_ed25519_kid


AUTHOR_PROFILE_FORMAT = "author-profile"
AUTHOR_PROFILE_VERSION = 0


@dataclass(frozen=True)
class AuthorProfile:
	"""Parsed author profile."""
	algo: str
	kid: str
	pubkey_b64: str
	name: str
	org: str
	email: str
	url: str
	namespaces: list[str]
	package: str = ""  # artifact name; set by deploy when binding to a signed package


def create_author_profile(
	*,
	pubkey_raw: bytes,
	name: str,
	org: str = "",
	email: str = "",
	url: str = "",
	namespaces: list[str],
) -> AuthorProfile:
	"""Create an AuthorProfile from a raw Ed25519 public key."""
	if len(pubkey_raw) != 32:
		raise ValueError("ed25519 public key must be 32 bytes")
	if not name and not org:
		raise ValueError("at least one of name or org is required")
	if not namespaces:
		raise ValueError("at least one namespace is required")
	kid = compute_ed25519_kid(pubkey_raw)
	pubkey_b64 = b64_encode(pubkey_raw)
	return AuthorProfile(
		algo="ed25519",
		kid=kid,
		pubkey_b64=pubkey_b64,
		name=name,
		org=org,
		email=email,
		url=url,
		namespaces=list(namespaces),
	)


def write_author_profile(profile: AuthorProfile, path: Path) -> None:
	"""Write an author profile to a .author-profile file."""
	obj: dict[str, Any] = {
		"format": AUTHOR_PROFILE_FORMAT,
		"version": AUTHOR_PROFILE_VERSION,
		"key": {
			"algo": profile.algo,
			"kid": profile.kid,
			"pubkey": profile.pubkey_b64,
		},
		"publisher": {
			"name": profile.name,
			"org": profile.org,
			"email": profile.email,
			"url": profile.url,
		},
		"namespaces": profile.namespaces,
	}
	if profile.package:
		obj["package"] = profile.package
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
		encoding="utf-8",
	)


def load_author_profile(path: Path) -> AuthorProfile:
	"""Load and validate an author profile from a .author-profile file."""
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError) as e:
		raise ValueError(f"failed to read author profile: {e}")

	if not isinstance(data, dict):
		raise ValueError("author profile must be a JSON object")
	fmt = data.get("format")
	if fmt != AUTHOR_PROFILE_FORMAT:
		raise ValueError(
			f"not an author profile: expected format '{AUTHOR_PROFILE_FORMAT}', "
			f"got '{fmt}'"
		)
	if data.get("version") != AUTHOR_PROFILE_VERSION:
		raise ValueError(f"unsupported author profile version: {data.get('version')}")

	key = data.get("key")
	if not isinstance(key, dict):
		raise ValueError("author profile missing 'key' object")
	algo = key.get("algo")
	if algo != "ed25519":
		raise ValueError(f"unsupported key algorithm: {algo}")
	pubkey_b64 = key.get("pubkey")
	if not isinstance(pubkey_b64, str) or not pubkey_b64:
		raise ValueError("author profile missing 'key.pubkey'")
	kid = key.get("kid")
	if not isinstance(kid, str) or not kid:
		raise ValueError("author profile missing 'key.kid'")

	try:
		pub_raw = b64_decode(pubkey_b64)
	except Exception:
		raise ValueError("author profile key.pubkey is not valid base64")
	if len(pub_raw) != 32:
		raise ValueError(
			f"author profile key.pubkey must decode to 32 bytes, got {len(pub_raw)}"
		)

	expected_kid = compute_ed25519_kid(pub_raw)
	if kid != expected_kid:
		raise ValueError(
			f"author profile key.kid does not match pubkey "
			f"(expected {expected_kid}, got {kid})"
		)

	publisher = data.get("publisher") or data.get("signer")
	if not isinstance(publisher, dict):
		raise ValueError("author profile missing 'publisher' object")
	name = publisher.get("name", "")
	if not isinstance(name, str):
		raise ValueError("author profile 'publisher.name' must be a string")
	p_org = publisher.get("org", "")
	if not isinstance(p_org, str):
		raise ValueError("author profile 'publisher.org' must be a string")
	if not name and not p_org:
		raise ValueError("author profile requires at least one of 'publisher.name' or 'publisher.org'")

	namespaces = data.get("namespaces")
	if not isinstance(namespaces, list) or not namespaces:
		raise ValueError("author profile requires non-empty 'namespaces' array")
	for i, ns in enumerate(namespaces):
		if not isinstance(ns, str) or not ns:
			raise ValueError(f"author profile namespace[{i}] must be a non-empty string")

	package = data.get("package", "")
	if package is not None and not isinstance(package, str):
		raise ValueError("author profile 'package' must be a string")

	return AuthorProfile(
		algo=algo,
		kid=kid,
		pubkey_b64=pubkey_b64,
		name=name,
		org=p_org,
		email=publisher.get("email", ""),
		url=publisher.get("url", ""),
		namespaces=namespaces,
		package=package or "",
	)


def apply_author_profile_to_trust_store(
	profile: AuthorProfile,
	trust_store_path: Path,
) -> dict[str, Any]:
	"""
	Add profile's key and namespace authorizations to a trust store.

	Returns a report: {kid, namespaces_added, already_trusted}.
	"""
	from lang.drift.trust import (
		TrustAddKeyOptions,
		add_key_to_trust_store,
		_load_or_init_trust_store,
	)

	store = _load_or_init_trust_store(trust_store_path)
	ns_obj = store.get("namespaces", {})

	# v1 trust store entries are role-tagged dicts:
	#   `{"authors": [...], "certifiers": [...]}`.
	# A profile already counts as trusted when the kid is present in
	# BOTH roles (the profile flow adds with role="both").
	already: list[str] = []
	added: list[str] = []
	for ns in profile.namespaces:
		entry = ns_obj.get(ns)
		if isinstance(entry, dict):
			authors = entry.get("authors") or []
			certifiers = entry.get("certifiers") or []
			if profile.kid in authors and profile.kid in certifiers:
				already.append(ns)
				continue
		added.append(ns)

	for ns in profile.namespaces:
		add_key_to_trust_store(TrustAddKeyOptions(
			trust_store_path=trust_store_path,
			namespace=ns,
			pubkey_b64=profile.pubkey_b64,
			kid=profile.kid,
		))

	return {
		"kid": profile.kid,
		"namespaces_added": added,
		"already_trusted": already,
	}
