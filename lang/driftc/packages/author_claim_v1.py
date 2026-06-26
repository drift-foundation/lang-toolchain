# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Author claim — `drift-author-claim` v1.

The author claim binds the author's identity to a package
**release**: source identity (source_content_id), declared deps,
namespaces.  Per O6 (sign-off 2026-05-18) the author claim NEVER
binds artifact bytes — that role is exclusively the certifier/
distributor's via `.cert-claim`.  Per the v1 spec correction
(2026-05-20) the author claim ALSO does not bind target / build
class — that field belongs on the certifier's claim
(`cert_claim.body.target`), so one author claim can cover the same
source release across multiple build targets.

Role invariant:
    Author role answers "who authorized this source release?"
    Distributor / certifier role answers "who vouches for this
    concrete artifact?"  Same actor MAY hold both roles, but the
    claims stay separate.

Body schema (signed-over), v2:

    {
      "schema_version": 2,
      "package_id": "<str>",
      "version": "<str>",
      "artifact_kind": "package" | "app",
      "namespaces": ["<pattern>", ...],
      "source_content_id": "sha256:<hex>",
      "required_deps": [{"name": "<str>", "version_range": "<str>"}, ...],
      "release_utc": "<ISO 8601>"
    }

v2 (clean break): `artifact_kind` is REQUIRED and the loader rejects body
`schema_version` 1 outright — no legacy reader; existing artifacts re-issue
v2 claims.  The envelope `format`/`version` stays `drift-author-claim` v1.

Sidecar schema (envelope around the body):

    {
      "format": "drift-author-claim",
      "version": 1,
      "body": <body>,
      "signatures": [
        {"algo": "ed25519", "kid": "<kid>", "sig": "<base64 raw 64B>"}, ...
      ]
    }

Signing rule: the author signs `canonical_json_bytes(body)`.
Multiple signatures are allowed (multi-author releases).  At verify
time, at least one signature MUST verify against a kid in
`trust.allowed_authors_for_module(M)` for every module M the
package claims to own — "any one within the array" suffices per O5.

Per the v1 product-boundary directive: this module accepts EXACTLY
`format: "drift-author-claim"` with `version: 1`.  Any other shape
is rejected with a clear "unsupported format version" diagnostic.
There is no fallback to a pre-v1 source-attestation format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from lang.drift.crypto import (
	b64_decode,
	b64_encode,
	compute_ed25519_kid,
	ed25519_sign_from_seed,
	verify_ed25519,
)
from lang.driftc.packages.source_content_id import (
	canonical_json_bytes,
	validate_sci,
)
from lang.driftc.packages.trust_v1 import TrustStore


_FORMAT_TAG = "drift-author-claim"
_FORMAT_VERSION = 1
# v2 body: adds REQUIRED `artifact_kind` ("package"|"app").  Clean break —
# the loader rejects body schema_version 1 outright (no legacy reader);
# existing artifacts re-issue v2 claims (pool re-cert).
_BODY_SCHEMA_VERSION = 2
# Public alias — the ONE source of truth for the author-claim body schema
# version.  Emitters (drift-author CLI, deploy) MUST import this rather than
# define a local constant, so a stale local copy can never sign a wrong-version
# body.  See lang/tests/packages/test_no_v1_claim_constructors.py.
BODY_SCHEMA_VERSION = _BODY_SCHEMA_VERSION
_CANONICAL_ARTIFACT_KINDS = frozenset({"package", "app"})


# Exact key sets accepted at each nesting level.  The strict loader rejects
# unknown keys so an attacker cannot smuggle unsigned-but-visible data
# (e.g. `body.artifact_sha256`) past a signature that was computed
# from the same JSON.  Per O6, author claims MUST NOT bind artifact
# bytes; the loader rejects the field name outright to prevent a
# verifier from being confused into thinking the field is meaningful.
_ENVELOPE_KEYS = frozenset({"format", "version", "body", "signatures"})
_BODY_KEYS = frozenset({
	"schema_version",
	"package_id",
	"version",
	"artifact_kind",
	"namespaces",
	"source_content_id",
	"required_deps",
	"release_utc",
})
_REQUIRED_DEP_KEYS = frozenset({"name", "version_range"})
_SIGNATURE_KEYS = frozenset({"algo", "kid", "sig"})


def _reject_unknown_keys(obj: dict, allowed: frozenset[str], *, context: str) -> None:
	"""Raise ValueError naming any key in `obj` not in `allowed`.

	Centralized so every nesting level in the author-claim loader
	rejects unknown keys with the same diagnostic shape.  Critical
	for security: an injected `body.artifact_sha256` (or any other
	field not in the v2 body schema) MUST NOT be silently dropped — the
	loader would then recompute signing bytes from only the known
	fields, the existing signature would still verify, and a
	downstream consumer who naively reads the JSON dict could be
	misled into thinking the unknown field was authoritative.
	"""
	unknown = set(obj.keys()) - allowed
	if unknown:
		raise ValueError(
			f"{context}: unknown field(s) {sorted(unknown)!r}; "
			f"this claim schema accepts exactly {sorted(allowed)!r}.  The strict "
			f"loader rejects unknown keys to prevent unsigned data from "
			f"riding inside a signed-looking claim."
		)


# ── Public dataclasses ─────────────────────────────────────────────


@dataclass(frozen=True)
class RequiredDep:
	"""A declared dependency in the author claim body.

	`version_range` is the AUTHOR'S declared range (e.g. `"^0.5.0"`),
	not the consumer's resolved version.  The resolver's choices are
	NOT part of source identity — only the author's declaration is
	signed here.
	"""
	name: str
	version_range: str


@dataclass(frozen=True)
class AuthorClaimBody:
	"""Signed payload of an author claim.

	No `target_class`: target / build environment is certifier
	metadata (`cert_claim.body.target`), not source identity.  One
	author claim can therefore cover the same source release across
	multiple build targets; each target gets its own cert claim.

	`artifact_kind` ("package"|"app", v2): the author attests source
	identity for a runnable app or an importable package; the kind is
	explicit so policy/diagnostics don't infer it.  Must equal the cert
	claim's kind (verify cross-checks).
	"""
	schema_version: int  # always 2 (v2)
	package_id: str
	version: str
	artifact_kind: str  # "package" | "app"
	namespaces: tuple[str, ...]
	source_content_id: str  # "sha256:<hex>"
	required_deps: tuple[RequiredDep, ...]
	release_utc: str  # ISO 8601


@dataclass(frozen=True)
class AuthorSignature:
	"""One ed25519 signature on an author claim body."""
	algo: str   # "ed25519"
	kid: str
	sig_raw: bytes  # 64-byte raw signature


@dataclass(frozen=True)
class AuthorClaim:
	"""Full author claim — body plus one or more signatures."""
	body: AuthorClaimBody
	signatures: tuple[AuthorSignature, ...]


# ── Body factory (the ONLY supported emit path) ────────────────────


def make_author_claim_body(
	*,
	package_id: str,
	version: str,
	artifact_kind: str,
	namespaces: tuple[str, ...],
	source_content_id: str,
	release_utc: str,
	required_deps: tuple[RequiredDep, ...] = (),
) -> AuthorClaimBody:
	"""Construct a CURRENT-schema author claim body.

	`schema_version` is set internally (`_BODY_SCHEMA_VERSION`), so callers
	never pass it.  ALL production emitters MUST use this factory instead of
	`AuthorClaimBody(schema_version=..., ...)` — that eliminates the
	stale-local-constant drift class entirely (an emitter cannot accidentally
	sign a v1 body).
	"""
	return AuthorClaimBody(
		schema_version=_BODY_SCHEMA_VERSION,
		package_id=package_id,
		version=version,
		artifact_kind=artifact_kind,
		namespaces=namespaces,
		source_content_id=source_content_id,
		required_deps=required_deps,
		release_utc=release_utc,
	)


# ── Body canonicalization (signed bytes) ───────────────────────────


def validate_body_shape(body: AuthorClaimBody) -> None:
	"""Enforce v1 value-shape rules on a dataclass-constructed body.

	The JSON loader (`_parse_body`) runs equivalent checks when
	parsing an untrusted document.  Without this function a
	hand-built dataclass with malformed values (empty strings,
	bad SCI shape, schema_version != 2) would sign canonical
	bytes that the loader would later refuse.  Called from
	`body_signing_bytes` and `_body_to_canonical_dict` so emit and
	load enforce the same contract.
	"""
	if body.schema_version != _BODY_SCHEMA_VERSION:
		raise ValueError(
			f"author claim body.schema_version must be {_BODY_SCHEMA_VERSION}; "
			f"got {body.schema_version!r}"
		)
	if not isinstance(body.package_id, str) or not body.package_id:
		raise ValueError("author claim body.package_id must be a non-empty string")
	if not isinstance(body.version, str) or not body.version:
		raise ValueError("author claim body.version must be a non-empty string")
	if body.artifact_kind not in _CANONICAL_ARTIFACT_KINDS:
		raise ValueError(
			f"author claim body.artifact_kind must be one of "
			f"{sorted(_CANONICAL_ARTIFACT_KINDS)!r} (v2); got {body.artifact_kind!r} "
			f"(legacy 'library' is not a valid signed kind)"
		)
	if not body.namespaces:
		raise ValueError("author claim body.namespaces must be a non-empty list")
	for idx, ns in enumerate(body.namespaces):
		if not isinstance(ns, str) or not ns:
			raise ValueError(
				f"author claim body.namespaces[{idx}] must be a non-empty string; got {ns!r}"
			)
	from lang.driftc.packages.source_content_id import validate_sci as _validate_sci
	_validate_sci(body.source_content_id, field="author claim body.source_content_id")
	if not isinstance(body.release_utc, str) or not body.release_utc:
		raise ValueError("author claim body.release_utc must be a non-empty string")
	for idx, d in enumerate(body.required_deps):
		if not isinstance(d.name, str) or not d.name:
			raise ValueError(
				f"author claim body.required_deps[{idx}].name must be a non-empty string"
			)
		if not isinstance(d.version_range, str) or not d.version_range:
			raise ValueError(
				f"author claim body.required_deps[{idx}].version_range must be a non-empty string"
			)


def _body_to_canonical_dict(body: AuthorClaimBody) -> dict[str, Any]:
	"""Convert the body dataclass to a canonical-shaped dict.

	Runs `validate_body_shape` first so emit and load enforce the
	same value contract.

	`required_deps` is converted to a list of
	`{"name": ..., "version_range": ...}` objects sorted by name.
	Duplicate dep names are REJECTED here too (in addition to the
	load-time check in `_parse_body`); the canonical sort would
	otherwise tie-break on input order.
	"""
	validate_body_shape(body)
	seen_names: set[str] = set()
	for d in body.required_deps:
		if d.name in seen_names:
			raise ValueError(
				f"author claim body.required_deps: duplicate dep name "
				f"{d.name!r} — a release claim must declare each dep "
				f"exactly once"
			)
		seen_names.add(d.name)
	deps_dicts = sorted(
		[{"name": d.name, "version_range": d.version_range} for d in body.required_deps],
		key=lambda d: d["name"],
	)
	return {
		"schema_version": int(body.schema_version),
		"package_id": body.package_id,
		"version": body.version,
		"artifact_kind": body.artifact_kind,
		"namespaces": sorted(body.namespaces),
		"source_content_id": body.source_content_id,
		"required_deps": deps_dicts,
		"release_utc": body.release_utc,
	}


def body_signing_bytes(body: AuthorClaimBody) -> bytes:
	"""Return the exact bytes the author signs over."""
	return canonical_json_bytes(_body_to_canonical_dict(body))


# ── Sign / sign-with / add-signature ───────────────────────────────


def sign_body(body: AuthorClaimBody, priv_seed32: bytes) -> AuthorSignature:
	"""Sign a body with an Ed25519 seed.  Returns the signature record
	(`AuthorSignature`); does not embed the signature into a claim
	envelope — use `make_author_claim` for that.
	"""
	msg = body_signing_bytes(body)
	sig_raw, pubkey_raw = ed25519_sign_from_seed(priv_seed32=priv_seed32, message=msg)
	kid = compute_ed25519_kid(pubkey_raw)
	return AuthorSignature(algo="ed25519", kid=kid, sig_raw=sig_raw)


def make_author_claim(body: AuthorClaimBody, priv_seed32: bytes) -> AuthorClaim:
	"""Construct an author claim with ONE signature from the given seed."""
	sig = sign_body(body, priv_seed32)
	return AuthorClaim(body=body, signatures=(sig,))


def add_signature(claim: AuthorClaim, priv_seed32: bytes) -> AuthorClaim:
	"""Return a new author claim with an additional signature
	co-signing the same body.  Useful for dual-author releases.
	The body is the same bytes; only the `signatures` tuple grows.
	"""
	new_sig = sign_body(claim.body, priv_seed32)
	return AuthorClaim(
		body=claim.body,
		signatures=claim.signatures + (new_sig,),
	)


# ── JSON load / dump ───────────────────────────────────────────────


def dump_author_claim_json(claim: AuthorClaim) -> str:
	"""Serialize an author claim to canonical sidecar JSON.

	Output is `json.dumps(..., sort_keys=True, separators=(",", ":"))`
	for determinism plus a trailing newline.  The body inside the
	envelope is the SAME canonical-dict shape used by
	`body_signing_bytes` — so a reader who recomputes
	`canonical_json_bytes(envelope["body"])` recovers the exact
	bytes the author signed.

	Signatures are sorted by kid for deterministic output ordering.
	"""
	envelope = {
		"format": _FORMAT_TAG,
		"version": _FORMAT_VERSION,
		"body": _body_to_canonical_dict(claim.body),
		"signatures": sorted(
			[
				{
					"algo": s.algo,
					"kid": s.kid,
					"sig": b64_encode(s.sig_raw),
				}
				for s in claim.signatures
			],
			key=lambda s: s["kid"],
		),
	}
	return json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n"


def load_author_claim_json(text: str) -> AuthorClaim:
	"""Parse and shape-validate an author-claim sidecar.

	Strict loader (format v1, body schema v2).  Any deviation (wrong format tag, wrong version,
	missing fields, wrong types) raises `ValueError` with a
	descriptive message.

	This function does NOT verify signatures — call
	`verify_author_claim_signatures` (low-level) or
	`verify_author_claim_for_module` (composition) for that.
	"""
	obj = json.loads(text)
	if not isinstance(obj, dict):
		raise ValueError("author claim must be a JSON object")
	_reject_unknown_keys(obj, _ENVELOPE_KEYS, context="author claim envelope")

	fmt = obj.get("format")
	ver = obj.get("version")
	if fmt != _FORMAT_TAG:
		raise ValueError(
			f"unsupported author claim format: expected {_FORMAT_TAG!r}, got {fmt!r}"
		)
	if ver != _FORMAT_VERSION:
		raise ValueError(
			f"unsupported author claim version: expected v{_FORMAT_VERSION}, "
			f"got v{ver!r}"
		)

	body_raw = obj.get("body")
	if not isinstance(body_raw, dict):
		raise ValueError("author claim 'body' must be a JSON object")
	body = _parse_body(body_raw)

	sigs_raw = obj.get("signatures")
	if not isinstance(sigs_raw, list):
		raise ValueError("author claim 'signatures' must be a list")
	if not sigs_raw:
		raise ValueError("author claim 'signatures' must contain at least one signature")
	signatures = tuple(_parse_signature(s, idx) for idx, s in enumerate(sigs_raw))

	return AuthorClaim(body=body, signatures=signatures)


def _parse_body(obj: dict) -> AuthorClaimBody:
	"""Strict body parse.  Every field type-checked.  Unknown keys
	are REJECTED — see `_reject_unknown_keys` for the rationale.
	Specifically: an injected `artifact_sha256` (or any other field
	outside the v2 body schema) MUST cause load to fail, otherwise an
	attacker could smuggle a fake artifact binding past a valid
	author signature.  Per O6, author claims never bind artifact
	bytes; the loader enforces this at the field-name level."""
	_reject_unknown_keys(obj, _BODY_KEYS, context="author claim body")
	schema_ver = obj.get("schema_version")
	if schema_ver != _BODY_SCHEMA_VERSION:
		raise ValueError(
			f"author claim body schema_version: expected {_BODY_SCHEMA_VERSION}, "
			f"got {schema_ver!r}"
		)
	package_id = obj.get("package_id")
	if not isinstance(package_id, str) or not package_id:
		raise ValueError("author claim body.package_id must be a non-empty string")
	version_str = obj.get("version")
	if not isinstance(version_str, str) or not version_str:
		raise ValueError("author claim body.version must be a non-empty string")
	artifact_kind = obj.get("artifact_kind")
	if artifact_kind not in _CANONICAL_ARTIFACT_KINDS:
		raise ValueError(
			f"author claim body.artifact_kind must be one of "
			f"{sorted(_CANONICAL_ARTIFACT_KINDS)!r} (v2); got {artifact_kind!r}"
		)
	namespaces_raw = obj.get("namespaces")
	if not isinstance(namespaces_raw, list) or not namespaces_raw:
		raise ValueError("author claim body.namespaces must be a non-empty list of strings")
	namespaces: list[str] = []
	for n in namespaces_raw:
		if not isinstance(n, str) or not n:
			raise ValueError(f"author claim body.namespaces: entries must be non-empty strings; got {n!r}")
		namespaces.append(n)
	sci = obj.get("source_content_id")
	validate_sci(sci, field="author claim body.source_content_id")

	deps_raw = obj.get("required_deps", [])
	if not isinstance(deps_raw, list):
		raise ValueError("author claim body.required_deps must be a list")
	required_deps: list[RequiredDep] = []
	seen_dep_names: set[str] = set()
	for idx, d in enumerate(deps_raw):
		if not isinstance(d, dict):
			raise ValueError(f"author claim body.required_deps[{idx}] must be a JSON object")
		_reject_unknown_keys(
			d, _REQUIRED_DEP_KEYS,
			context=f"author claim body.required_deps[{idx}]",
		)
		name = d.get("name")
		rng = d.get("version_range")
		if not isinstance(name, str) or not name:
			raise ValueError(
				f"author claim body.required_deps[{idx}].name must be a non-empty string"
			)
		if not isinstance(rng, str) or not rng:
			raise ValueError(
				f"author claim body.required_deps[{idx}].version_range must be a non-empty string"
			)
		# Reject duplicate dep names.  Two entries with the same name
		# but different ranges are ambiguous as a release claim
		# (which range does the author actually authorize?), and the
		# canonical signing-bytes ordering -- sorted only by name --
		# would otherwise depend on input order for equal keys.
		if name in seen_dep_names:
			raise ValueError(
				f"author claim body.required_deps[{idx}]: duplicate dep "
				f"name {name!r} — a release claim must declare each dep "
				f"exactly once"
			)
		seen_dep_names.add(name)
		required_deps.append(RequiredDep(name=name, version_range=rng))

	release_utc = obj.get("release_utc")
	if not isinstance(release_utc, str) or not release_utc:
		raise ValueError("author claim body.release_utc must be a non-empty string")

	return AuthorClaimBody(
		schema_version=schema_ver,
		package_id=package_id,
		version=version_str,
		artifact_kind=artifact_kind,
		namespaces=tuple(namespaces),
		source_content_id=sci,
		required_deps=tuple(required_deps),
		release_utc=release_utc,
	)


def _parse_signature(obj: Any, idx: int) -> AuthorSignature:
	"""Strict per-signature parse.  Unknown keys are rejected so
	auxiliary unsigned data (e.g. fake `role`, `expires_at`, or
	policy hints) cannot ride inside a signed-looking signature
	record."""
	if not isinstance(obj, dict):
		raise ValueError(f"author claim signatures[{idx}] must be a JSON object")
	_reject_unknown_keys(
		obj, _SIGNATURE_KEYS,
		context=f"author claim signatures[{idx}]",
	)
	algo = obj.get("algo")
	if algo != "ed25519":
		raise ValueError(
			f"author claim signatures[{idx}]: algo must be 'ed25519'; got {algo!r}"
		)
	kid = obj.get("kid")
	if not isinstance(kid, str) or not kid:
		raise ValueError(f"author claim signatures[{idx}]: kid must be a non-empty string")
	sig_b64 = obj.get("sig")
	if not isinstance(sig_b64, str):
		raise ValueError(f"author claim signatures[{idx}]: 'sig' must be a base64 string")
	try:
		sig_raw = b64_decode(sig_b64)
	except Exception as err:
		raise ValueError(
			f"author claim signatures[{idx}]: 'sig' is not valid base64: {err}"
		) from err
	if len(sig_raw) != 64:
		raise ValueError(
			f"author claim signatures[{idx}]: ed25519 signature must be 64 bytes; "
			f"got {len(sig_raw)}"
		)
	return AuthorSignature(algo=algo, kid=kid, sig_raw=sig_raw)


# ── Verification ───────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthorClaimVerifyResult:
	"""Outcome of `verify_author_claim_for_module`.

	`ok=True` iff at least one signature verifies against a kid in
	`trust.allowed_authors_for_module(module_id)` AND the body's
	namespaces cover `module_id`.

	`accepted_kid` is the first kid that satisfied trust (deterministic
	by sort order over signatures).  Useful for diagnostics.

	`reason` carries a one-line human-readable explanation when
	`ok=False`.
	"""
	ok: bool
	accepted_kid: Optional[str]
	reason: str


def _namespace_covers(pattern: str, module_id: str) -> bool:
	"""True iff `pattern` (exact or `<ns>.*`) covers `module_id`.

	Matches the rule used by `TrustStore` for namespace lookup so
	authors describing their namespaces use the same shape as
	consumers' trust files.
	"""
	if pattern.endswith(".*"):
		pfx = pattern[:-2]
		return module_id == pfx or module_id.startswith(pfx + ".")
	return module_id == pattern


def verify_author_claim_signatures(
	claim: AuthorClaim,
	trust: TrustStore,
) -> set[str]:
	"""Return the set of trusted-author kids whose signatures verify.

	Verifies the cryptographic signatures and intersects against
	`trust.keys_by_kid`.  This does NOT consult the namespace
	allow-lists — use `verify_author_claim_for_module` for the full
	role+namespace composition check.

	A signature whose kid has no entry in `trust.keys_by_kid` is
	silently skipped (per the consumer's view it's an unknown
	signer; intersecting against keys_by_kid is the only way to
	even attempt verification).  A kid present in keys but whose
	signature bytes fail Ed25519 verification is also skipped.
	"""
	signing_bytes = body_signing_bytes(claim.body)
	verified: set[str] = set()
	for sig in claim.signatures:
		key = trust.keys_by_kid.get(sig.kid)
		if key is None:
			continue
		if key.algo != "ed25519":
			continue
		if verify_ed25519(
			pubkey_raw=key.pubkey_raw,
			message=signing_bytes,
			signature_raw=sig.sig_raw,
		):
			verified.add(sig.kid)
	return verified


def verify_author_claim_for_module(
	claim: AuthorClaim,
	trust: TrustStore,
	module_id: str,
	*,
	expected_package_id: str,
	expected_version: str,
) -> AuthorClaimVerifyResult:
	"""Full author-claim verification for one module_id.

	Composes (in order):
	  1. `claim.body.package_id == expected_package_id` AND
	     `claim.body.version == expected_version` — pins this claim
	     to THIS package release.  Without this gate an attacker
	     could substitute an author claim for an unrelated package
	     and have it pass verification for a module whose namespace
	     happens to match.
	  2. namespace coverage: at least one entry in
	     `claim.body.namespaces` must cover `module_id`.
	  3. signature: at least one signature must verify AND the signer's
	     kid must be in `trust.allowed_authors_for_module(module_id)`.

	Revocation is handled inside `allowed_authors_for_module` (revoked
	kids are excluded there).

	Returns an `AuthorClaimVerifyResult` with a structured outcome.
	"""
	# Gate 1: package identity pinning.
	if claim.body.package_id != expected_package_id:
		return AuthorClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"author claim body.package_id ({claim.body.package_id!r}) "
				f"does not match expected package_id ({expected_package_id!r}); "
				f"this claim is for a different package"
			),
		)
	if claim.body.version != expected_version:
		return AuthorClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"author claim body.version ({claim.body.version!r}) does not "
				f"match expected version ({expected_version!r}); this claim is "
				f"for a different release of {expected_package_id!r}"
			),
		)

	# Namespace coverage.
	covering = [n for n in claim.body.namespaces if _namespace_covers(n, module_id)]
	if not covering:
		return AuthorClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"author claim does not cover module {module_id!r}; "
				f"claimed namespaces: {list(claim.body.namespaces)!r}"
			),
		)

	# Cryptographic signatures that intersect any trusted key.
	cryptographically_verified = verify_author_claim_signatures(claim, trust)
	if not cryptographically_verified:
		return AuthorClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				"no signature on author claim verifies against any key in the "
				"trust store"
			),
		)

	# Author-role-trusted kids for this module.  Surface revocation
	# explicitly: if a signer kid would have been allowed but for the
	# trust store's `revoked` list, name the revoked kid so the user
	# can correlate it with the `drift trust revoke` call.
	allowed_authors = trust.allowed_authors_for_module(module_id)
	revoked_signers = cryptographically_verified & trust.revoked_kids
	if not allowed_authors:
		if revoked_signers:
			return AuthorClaimVerifyResult(
				ok=False,
				accepted_kid=None,
				reason=(
					f"author claim was signed by revoked kid(s) "
					f"{sorted(revoked_signers)!r}; the trust store has these "
					f"listed in `revoked` and module {module_id!r} has no other "
					f"author-role kids"
				),
			)
		return AuthorClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"trust store authorizes no author-role kids for module "
				f"{module_id!r}; consult `drift trust apply` to add the "
				f"author's profile"
			),
		)

	accepted = sorted(cryptographically_verified & allowed_authors)
	if not accepted:
		if revoked_signers:
			return AuthorClaimVerifyResult(
				ok=False,
				accepted_kid=None,
				reason=(
					f"author claim was signed by revoked kid(s) "
					f"{sorted(revoked_signers)!r}; module {module_id!r} has "
					f"other author-role kids but the claim is not co-signed "
					f"by any of them"
				),
			)
		return AuthorClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"author claim signatures verified but none of the signer kids "
				f"are trusted in the 'authors' role for module {module_id!r}.  "
				f"Verified kids: {sorted(cryptographically_verified)!r}.  "
				f"Trusted authors for namespace: {sorted(allowed_authors)!r}"
			),
		)

	return AuthorClaimVerifyResult(
		ok=True,
		accepted_kid=accepted[0],
		reason="",
	)
