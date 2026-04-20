# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Source attestation — author-signed binding between a package's
source identity and its published `(package_id, version)`.

The `.source-attestation` sidecar is the trust anchor for source-rebuild
certification.  It says, signed by the package owner: "release X.Y.Z of
package P was built from this exact source, against these declared
required-dep ranges, for this target class."  Any party that has the
source can recompute `source_content_id`, fetch the attestation,
verify the signature against the owner's key, and prove the source they
are about to rebuild matches what the owner attested.

This is deliberately separate from the artifact `.sig` envelope.  The
artifact signature attests "I built these specific bytes."  The source
attestation attests "I, the package owner, certify this source under
this name@version."  In source-rebuild workflows, the rebuilt `.dmp`
will have a different artifact signature (the rebuilder's, possibly
none), but the source attestation persists across rebuilds because
source identity is stable — bytes are not.

Trust model (matches the existing `.sig` envelope conventions):
- Ed25519 signing.
- kid = "ed25519:" + base64(sha256(pubkey_raw)).
- Signature covers the envelope text, not the body JSON directly.
- Envelope text references body content by sha256 so verification is a
  small, fixed-shape string regardless of body size.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
	Ed25519PrivateKey,
	Ed25519PublicKey,
)


# ── Canonical source_content_id ─────────────────────────────────────

SOURCE_ATTESTATION_BODY_SCHEMA_VERSION = 1
SOURCE_ATTESTATION_SIDECAR_FORMAT = "drift-source-attestation"
SOURCE_ATTESTATION_SIDECAR_VERSION = 0
_ENVELOPE_HEADER = "drift-src-attestation-v1"

# Strict shape for any sha256-prefixed identifier crossing a trust
# boundary: literal `sha256:` + exactly 64 lowercase hex chars.
# `re.fullmatch` is required so trailing whitespace, prefix-only, or
# wrapped strings (e.g. `"sha256:..\n"`) are rejected.
_SHA256_HEX_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256_BARE_HEX_RE = re.compile(r"[0-9a-f]{64}")


def validate_sha256_hex_id(s: Any, *, field: str) -> str:
	"""Strict validator for `sha256:<64 lowercase hex>` ids.

	Every signed/canonical surface that records a sha must funnel
	through this so a malformed id (uppercase, short, wrapped,
	non-hex) cannot become signed input.  Rejects programmatically:

	- `sha256:zzz...` (non-hex)
	- `sha256:ABC...` (upper-case)
	- `sha256:abc` (too short)
	- `sha256:` + 65 hex chars (too long)
	- prefix-only or whitespace-padded forms
	- non-string values

	Returns the validated string verbatim so callers can both
	validate and re-bind in a single call.  `field` names the
	calling context for the diagnostic.
	"""
	if not isinstance(s, str):
		raise ValueError(f"{field}: must be a string, got {type(s).__name__}")
	if not _SHA256_HEX_ID_RE.fullmatch(s):
		raise ValueError(
			f"{field}: must match 'sha256:<64 lowercase hex>'; got {s!r}"
		)
	return s


def _validate_sha256_bare_hex(s: Any, *, field: str) -> str:
	"""Bare 64-lowercase-hex form (no `sha256:` prefix) — used for
	per-module / per-asset content shas inside the canonical body
	since the prefix would just repeat 64 times."""
	if not isinstance(s, str):
		raise ValueError(f"{field}: must be a string, got {type(s).__name__}")
	if not _SHA256_BARE_HEX_RE.fullmatch(s):
		raise ValueError(
			f"{field}: must be 64 lowercase hex chars; got {s!r}"
		)
	return s


def _canonical_json(obj: Any) -> bytes:
	"""Canonical JSON serialization — sorted keys, compact separators,
	UTF-8.  Matches the pattern used by `provenance.py::build_provenance`
	and `lockfile.py::write_lock` so determinism is consistent across
	all signed/hashed artifacts."""
	return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _b64_encode(data: bytes) -> str:
	return base64.b64encode(data).decode("ascii")


def _b64_decode(text: str) -> bytes:
	return base64.b64decode(text.encode("ascii"), validate=True)


def _ed25519_kid(pubkey_raw: bytes) -> str:
	"""kid format pinned to match `signature_v0.compute_ed25519_kid`."""
	return "ed25519:" + _b64_encode(hashlib.sha256(pubkey_raw).digest())


def _normalize_canonical_path(p: str) -> str:
	"""Normalize a project-relative path for canonical hashing.

	Pinned policy:
	- backslashes → forward slashes
	- no absolute paths (must not start with `/`)
	- no `.` or `..` segments
	- no leading `./`
	- no trailing slash
	- no empty path

	Any path that fails these rules is rejected at canonicalisation
	time so an absolute or escape-style path can't sneak into a
	signed source identity (which would let two different source
	trees collide on `source_content_id` by aliasing absolute paths).
	"""
	if not isinstance(p, str) or not p:
		raise ValueError("canonical path must be a non-empty string")
	q = p.replace("\\", "/")
	while q.startswith("./"):
		q = q[2:]
	q = q.rstrip("/")
	if not q:
		raise ValueError(f"canonical path is empty after normalisation: {p!r}")
	if q.startswith("/"):
		raise ValueError(f"canonical path must be project-relative, not absolute: {p!r}")
	for seg in q.split("/"):
		if seg in ("", ".", ".."):
			raise ValueError(
				f"canonical path may not contain '.', '..', or empty segments: {p!r}"
			)
	return q


@dataclass(frozen=True)
class SourceContentInputs:
	"""Stable build inputs that determine source identity.

	Field names mirror the manifest exactly so the canonical schema
	is auditable against `tools/drift_deploy/manifest.py::Artifact`:
	`kind`, `name` (→ `package_id` for the .dmp manifest), `version`,
	`module_namespace`, `entry_module`, `modules`, `package_deps`,
	`native_deps`, `assets`, `unsafe`.  `target_class` comes from
	the build invocation (compiler `--package-target`).

	What's IN: identity, kind, declared dep ranges, source files
	(paths + content sha), native dep names, unsafe flag, asset paths
	+ content sha, target class.

	What's OUT (non-canonical): build epoch, compiler version, ABI
	fingerprint, signatures, absolute paths, file mtimes, payload
	bytes produced by the compiler.  Anything that varies across
	legitimate rebuilds of the same source is excluded by
	construction.
	"""
	kind: str  # "library" or "app" — matches manifest Artifact.kind
	package_id: str
	version: str
	module_namespace: str
	entry_module: str
	modules: list[tuple[str, str]]  # [(relative_path, sha256_hex), ...]
	package_deps: list[tuple[str, str]]  # [(name, version_range), ...]
	native_deps: list[str]
	unsafe: bool
	assets: list[tuple[str, str]]  # [(relative_path, sha256_hex), ...]
	target_class: str


def compute_source_content_id(inputs: SourceContentInputs) -> str:
	"""Compute the canonical, deterministic `source_content_id`.

	Returns `"sha256:<hex>"`.  The same inputs always yield the same
	id; different inputs (different module bytes, reordered deps with
	different content, etc.) yield different ids.  Order of inputs in
	`modules`, `package_deps`, `native_deps`, `assets` is normalized
	(sorted) inside this function so callers don't have to.

	Path safety: every path in `modules`, `entry_module`, and
	`assets` is run through `_normalize_canonical_path`, which
	rejects absolute paths, `..` segments, and empty entries.  This
	guarantees the signed identity references project-local source
	only.
	"""
	canonical = {
		"schema_version": 1,
		"kind": inputs.kind,
		"package_id": inputs.package_id,
		"version": inputs.version,
		"module_namespace": inputs.module_namespace,
		"entry_module": _normalize_canonical_path(inputs.entry_module),
		"modules": sorted(
			[
				{
					"path": _normalize_canonical_path(p),
					"sha256": _validate_sha256_bare_hex(s, field=f"modules[{p!r}].sha256"),
				}
				for (p, s) in inputs.modules
			],
			key=lambda e: e["path"],
		),
		"package_deps": sorted(
			[{"name": n, "version": v} for (n, v) in inputs.package_deps],
			key=lambda e: e["name"],
		),
		"native_deps": sorted(inputs.native_deps),
		"unsafe": bool(inputs.unsafe),
		"assets": sorted(
			[
				{
					"path": _normalize_canonical_path(p),
					"sha256": _validate_sha256_bare_hex(s, field=f"assets[{p!r}].sha256"),
				}
				for (p, s) in inputs.assets
			],
			key=lambda e: e["path"],
		),
		"target_class": inputs.target_class,
	}
	return "sha256:" + _sha256_hex(_canonical_json(canonical))


def hash_file(path: Path) -> str:
	"""sha256 hex of a file's exact bytes.  For module/asset content."""
	h = hashlib.sha256()
	with path.open("rb") as f:
		for chunk in iter(lambda: f.read(65536), b""):
			h.update(chunk)
	return h.hexdigest()


def compute_artifact_source_content_id(
	*,
	kind: str,
	package_id: str,
	version: str,
	module_namespace: str,
	entry_module: str,
	module_paths: list[str],
	package_deps: list[tuple[str, str]],
	native_deps: list[str],
	unsafe: bool,
	asset_paths: list[str],
	target_class: str,
	source_root: Path,
) -> str:
	"""Compute `source_content_id` for an artifact by hashing its
	on-disk source/asset files.

	`source_root` is the project root that `module_paths` and
	`asset_paths` are relative to.  Each path is hashed with
	`hash_file` and fed into `compute_source_content_id`.

	Missing files raise `FileNotFoundError` — silent dropping would
	let a deleted module produce a different id than the same source
	at consumption time, breaking the rebuild equivalence claim.
	"""
	module_entries: list[tuple[str, str]] = []
	for rel in module_paths:
		full = source_root / rel
		if not full.is_file():
			raise FileNotFoundError(
				f"source module '{rel}' not found at {full}; cannot compute "
				f"source_content_id"
			)
		module_entries.append((rel, hash_file(full)))
	asset_entries: list[tuple[str, str]] = []
	for rel in asset_paths:
		full = source_root / rel
		if not full.is_file():
			raise FileNotFoundError(
				f"asset '{rel}' not found at {full}; cannot compute "
				f"source_content_id"
			)
		asset_entries.append((rel, hash_file(full)))
	return compute_source_content_id(SourceContentInputs(
		kind=kind,
		package_id=package_id,
		version=version,
		module_namespace=module_namespace,
		entry_module=entry_module,
		modules=module_entries,
		package_deps=list(package_deps),
		native_deps=list(native_deps),
		unsafe=unsafe,
		assets=asset_entries,
		target_class=target_class,
	))


# ── SourceAttestation body + sidecar ──────────────────────────────


@dataclass(frozen=True)
class RequiredDepEntry:
	"""Owner-declared dep range as recorded in the attestation body.
	Same shape as `.dmp::required_deps[]` so consumers can cross-check
	the attestation's declared ranges against the package's published
	required_deps without parsing two different schemas."""
	name: str
	version: str  # owner-declared range ("M" or "M.N")


@dataclass(frozen=True)
class SourceAttestationBody:
	"""The signed payload of a `.source-attestation` sidecar.

	Pinned schema (v1).  All fields are required.  The body is the
	sole place the package owner attests `source_content_id`; the
	signature elsewhere in the sidecar binds this body to the owner's
	key.
	"""
	schema_version: int
	package_id: str
	version: str
	source_content_id: str  # "sha256:<hex>" — the canonical id from compute_source_content_id
	required_deps: list[RequiredDepEntry]
	target_class: str


def _body_to_json(body: SourceAttestationBody) -> dict[str, Any]:
	return {
		"schema_version": body.schema_version,
		"package_id": body.package_id,
		"version": body.version,
		"source_content_id": body.source_content_id,
		"required_deps": [
			{"name": d.name, "version": d.version}
			for d in sorted(body.required_deps, key=lambda x: x.name)
		],
		"target_class": body.target_class,
	}


def _body_from_json(obj: dict[str, Any]) -> SourceAttestationBody:
	if not isinstance(obj, dict):
		raise ValueError("source attestation body must be a JSON object")
	sv = obj.get("schema_version")
	if sv != SOURCE_ATTESTATION_BODY_SCHEMA_VERSION:
		raise ValueError(
			f"unsupported source attestation body schema_version: {sv} "
			f"(expected {SOURCE_ATTESTATION_BODY_SCHEMA_VERSION})"
		)
	pkg = obj.get("package_id")
	ver = obj.get("version")
	scid = obj.get("source_content_id")
	tcls = obj.get("target_class")
	rd = obj.get("required_deps")
	if not isinstance(pkg, str) or not pkg:
		raise ValueError("source attestation body missing 'package_id'")
	if not isinstance(ver, str) or not ver:
		raise ValueError("source attestation body missing 'version'")
	validate_sha256_hex_id(scid, field="source attestation body 'source_content_id'")
	if not isinstance(tcls, str) or not tcls:
		raise ValueError("source attestation body missing 'target_class'")
	if not isinstance(rd, list):
		raise ValueError("source attestation body missing 'required_deps' list")
	deps: list[RequiredDepEntry] = []
	for entry in rd:
		if not isinstance(entry, dict):
			raise ValueError("required_deps entries must be objects")
		n = entry.get("name")
		v = entry.get("version")
		if not isinstance(n, str) or not n:
			raise ValueError("required_deps entry missing 'name'")
		if not isinstance(v, str) or not v:
			raise ValueError("required_deps entry missing 'version'")
		deps.append(RequiredDepEntry(name=n, version=v))
	return SourceAttestationBody(
		schema_version=sv,
		package_id=pkg,
		version=ver,
		source_content_id=scid,
		required_deps=deps,
		target_class=tcls,
	)


def canonical_body_bytes(body: SourceAttestationBody) -> bytes:
	"""Canonical bytes the body_sha256 hashes.  Stable across runs."""
	return _canonical_json(_body_to_json(body))


def _build_envelope(body_sha256_hex: str) -> bytes:
	"""Envelope text the signer signs.  Shape mirrors
	`signature_v0._build_envelope_v2` so reviewers see the same trust
	pattern across both signed surfaces."""
	return f"{_ENVELOPE_HEADER}\nbody-sha256:{body_sha256_hex}\n".encode("utf-8")


@dataclass(frozen=True)
class AttestationSignature:
	"""One signature on the attestation envelope.  An attestation may
	carry multiple signatures (e.g. owner key + co-signing org key);
	verification needs at least one to validate against the expected
	signer."""
	algo: str  # always "ed25519" today
	kid: str
	pubkey_raw: bytes  # 32 bytes; embedded so verifiers don't need a registry
	sig_raw: bytes  # 64 bytes


@dataclass(frozen=True)
class SourceAttestationSidecar:
	"""Parsed `.source-attestation` sidecar."""
	body: SourceAttestationBody
	body_sha256_hex: str  # sha256 of canonical_body_bytes(body); included for self-check
	signatures: list[AttestationSignature]


def sign_attestation(
	body: SourceAttestationBody,
	*,
	signing_key_seed: bytes,
) -> SourceAttestationSidecar:
	"""Sign an attestation body with an Ed25519 seed (32 bytes).

	The seed is the same format used by `_sign_artifact` in
	`drift_deploy.py` (raw 32-byte private key) so a single
	`--sign-key-file` covers both artifact signing and source
	attestation signing.
	"""
	if len(signing_key_seed) != 32:
		raise ValueError(f"ed25519 seed must be 32 bytes, got {len(signing_key_seed)}")
	# Strict re-validate the body's source_content_id at the trust
	# boundary so a programmatic caller can't sign a malformed id.
	validate_sha256_hex_id(
		body.source_content_id,
		field="source attestation body 'source_content_id'",
	)
	priv = Ed25519PrivateKey.from_private_bytes(signing_key_seed)
	pub = priv.public_key()
	from cryptography.hazmat.primitives import serialization as _ser
	pub_raw = pub.public_bytes(
		encoding=_ser.Encoding.Raw,
		format=_ser.PublicFormat.Raw,
	)
	body_sha = _sha256_hex(canonical_body_bytes(body))
	envelope = _build_envelope(body_sha)
	sig_raw = priv.sign(envelope)
	kid = _ed25519_kid(pub_raw)
	return SourceAttestationSidecar(
		body=body,
		body_sha256_hex=body_sha,
		signatures=[
			AttestationSignature(
				algo="ed25519",
				kid=kid,
				pubkey_raw=pub_raw,
				sig_raw=sig_raw,
			),
		],
	)


def write_attestation_sidecar(path: Path, sidecar: SourceAttestationSidecar) -> None:
	"""Write the sidecar to disk as canonical JSON.  The on-disk shape
	is also canonical so any party can recompute body_sha256 from the
	`body` field on disk."""
	obj = {
		"format": SOURCE_ATTESTATION_SIDECAR_FORMAT,
		"version": SOURCE_ATTESTATION_SIDECAR_VERSION,
		"envelope": _ENVELOPE_HEADER,
		"body": _body_to_json(sidecar.body),
		"body_sha256": "sha256:" + sidecar.body_sha256_hex,
		"signatures": [
			{
				"algo": s.algo,
				"kid": s.kid,
				"pubkey": _b64_encode(s.pubkey_raw),
				"sig": _b64_encode(s.sig_raw),
			}
			for s in sidecar.signatures
		],
	}
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(_canonical_json(obj) + b"\n")


def read_attestation_sidecar(path: Path) -> SourceAttestationSidecar:
	"""Load a `.source-attestation` sidecar from disk.

	Validates structural integrity (format/version, body schema,
	signature shape) but does NOT verify the signature — call
	`verify_attestation` for that.  The returned sidecar's
	`body_sha256_hex` is recomputed from the on-disk `body` and
	cross-checked against the declared `body_sha256` field; any
	mismatch raises immediately so downstream verification can rely
	on body and body_sha256 being self-consistent.
	"""
	try:
		obj = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as err:
		raise ValueError(f"source attestation sidecar invalid JSON: {err}") from err
	if not isinstance(obj, dict):
		raise ValueError("source attestation sidecar must be a JSON object")
	if obj.get("format") != SOURCE_ATTESTATION_SIDECAR_FORMAT:
		raise ValueError(
			f"unexpected source attestation format: {obj.get('format')!r} "
			f"(expected {SOURCE_ATTESTATION_SIDECAR_FORMAT!r})"
		)
	if obj.get("version") != SOURCE_ATTESTATION_SIDECAR_VERSION:
		raise ValueError(
			f"unsupported source attestation sidecar version: {obj.get('version')} "
			f"(expected {SOURCE_ATTESTATION_SIDECAR_VERSION})"
		)
	body_obj = obj.get("body")
	body = _body_from_json(body_obj)
	declared_sha = obj.get("body_sha256")
	validate_sha256_hex_id(declared_sha, field="source attestation sidecar 'body_sha256'")
	declared_sha_hex = declared_sha.split("sha256:", 1)[1]
	actual_sha_hex = _sha256_hex(canonical_body_bytes(body))
	if declared_sha_hex != actual_sha_hex:
		raise ValueError(
			"source attestation body_sha256 does not match canonical body — "
			"sidecar is corrupt or hand-edited"
		)
	sigs_obj = obj.get("signatures")
	if not isinstance(sigs_obj, list) or not sigs_obj:
		raise ValueError("source attestation sidecar missing 'signatures' list")
	signatures: list[AttestationSignature] = []
	for s in sigs_obj:
		if not isinstance(s, dict):
			raise ValueError("source attestation signature entry must be an object")
		algo = s.get("algo")
		kid = s.get("kid")
		pub_b64 = s.get("pubkey")
		sig_b64 = s.get("sig")
		if algo != "ed25519":
			raise ValueError(f"unsupported source attestation signature algo: {algo!r}")
		if not isinstance(kid, str) or not kid.startswith("ed25519:"):
			raise ValueError("source attestation signature missing 'kid' (ed25519:...)")
		if not isinstance(pub_b64, str):
			raise ValueError("source attestation signature missing 'pubkey'")
		if not isinstance(sig_b64, str):
			raise ValueError("source attestation signature missing 'sig'")
		try:
			pub_raw = _b64_decode(pub_b64)
			sig_raw = _b64_decode(sig_b64)
		except Exception as err:
			raise ValueError(f"source attestation signature has invalid base64: {err}") from err
		if len(pub_raw) != 32:
			raise ValueError("ed25519 pubkey must be 32 bytes")
		if len(sig_raw) != 64:
			raise ValueError("ed25519 signature must be 64 bytes")
		expected_kid = _ed25519_kid(pub_raw)
		if kid != expected_kid:
			raise ValueError(
				f"source attestation signature kid does not match pubkey: "
				f"declared {kid!r}, expected {expected_kid!r}"
			)
		signatures.append(AttestationSignature(
			algo=algo, kid=kid, pubkey_raw=pub_raw, sig_raw=sig_raw,
		))
	return SourceAttestationSidecar(
		body=body,
		body_sha256_hex=actual_sha_hex,
		signatures=signatures,
	)


def verify_attestation(
	sidecar: SourceAttestationSidecar,
	*,
	expected_signer_kid: str | None = None,
) -> None:
	"""Verify at least one signature on the attestation envelope.

	If `expected_signer_kid` is given, requires a valid signature from
	that specific kid (the lock's recorded source_attestation_key).
	If None, requires a valid signature from at least one carried
	pubkey (used for self-check during emit).

	Raises `ValueError` on verification failure.
	"""
	envelope = _build_envelope(sidecar.body_sha256_hex)
	if expected_signer_kid is not None:
		matched = [s for s in sidecar.signatures if s.kid == expected_signer_kid]
		if not matched:
			raise ValueError(
				f"source attestation has no signature from expected signer "
				f"{expected_signer_kid!r}"
			)
		candidates = matched
	else:
		candidates = list(sidecar.signatures)
	for s in candidates:
		try:
			pub = Ed25519PublicKey.from_public_bytes(s.pubkey_raw)
			pub.verify(s.sig_raw, envelope)
			return
		except (InvalidSignature, ValueError):
			continue
	raise ValueError("source attestation signature verification failed")
