# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Certifier / distributor claim — `drift-cert-claim` v1.

The cert claim binds a CERTIFIER (or distributor) to a specific
BUILD of a package: artifact bytes, toolchain identity, full
resolved dep graph, and certification-suite result.  Together with
a trusted `.author-claim` (sharing the same `source_content_id`),
a cert claim is one of two artifact-acceptance paths the consumer
may take; the other is `--self-verify` (rebuild from source).

Per O3 sign-off: `dep_graph` is the FULL RESOLVED TRANSITIVE
CLOSURE that was present at build/cert time.  The certifier's
signature commits to exactly which upstream identities they
accepted — direct-deps-only would let an attacker swap a
transitive dep without invalidating the cert claim.

Per O4 sign-off: `cert_suite.id` is verifier-addressable.
Consumers can pin `drift verify --require-cert-suite <id>` so a
release-gate signature cannot be confused with a smoke-only
signature.

Per O6 sign-off: artifact_sha256 binding lives EXCLUSIVELY here
(never in `.author-claim`).  "Direct author distribution" is
represented by the author signing a cert claim with their own
key; the consumer trusts that kid in both `authors` and
`certifiers` role lists.

Per O1 sign-off: per-certifier sidecar filename
`<pkg>.cert-claim.<kid>.json` (full kid, no short-prefix
collision risk).  Multiple sidecars coexist when multiple
certifiers attest the same release.

Per the v1 product-boundary directive: this module accepts
EXACTLY `format: "drift-cert-claim"` with `version: 1`.  No v0
fallback.  Unknown keys at any nesting level are rejected so
unsigned-but-visible data cannot ride inside a signed-looking
claim.
"""

from __future__ import annotations

import json
import re
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


_FORMAT_TAG = "drift-cert-claim"
_FORMAT_VERSION = 1
_BODY_SCHEMA_VERSION = 1


# ── Strict v1 key sets ─────────────────────────────────────────────


_ENVELOPE_KEYS = frozenset({"format", "version", "body", "signatures"})
_BODY_KEYS = frozenset({
	"schema_version",
	"package_id",
	"version",
	"artifact_sha256",
	"source_content_id",
	"target",
	"toolchain",
	"dep_graph",
	"cert_suite",
	"run_id",
	"run_started_utc",
	"evidence_sha256",
})
_TOOLCHAIN_KEYS = frozenset({
	"driftc_version",
	"drift_rt_abi",
	"driftc_commit",
})
_DEP_GRAPH_ENTRY_KEYS = frozenset({
	"package_id",
	"version",
	"artifact_sha256",
	"source_content_id",
	"author_kid",
	"cert_kid",
	"dep_kind",
})
_CERT_SUITE_KEYS = frozenset({
	"id",
	"version",
	"result",
	"result_evidence_sha256",
})
_SIGNATURE_KEYS = frozenset({"algo", "kid", "sig"})

_DEP_KINDS = frozenset({"direct", "transitive"})
_CERT_RESULTS = frozenset({"pass", "fail"})


def _reject_unknown_keys(obj: dict, allowed: frozenset[str], *, context: str) -> None:
	"""Raise ValueError naming any key in `obj` not in `allowed`.

	Strict v1 rejects unknown keys at every nesting level to
	prevent unsigned data from riding inside a signed-looking
	claim.  Centralized for diagnostic consistency with
	`author_claim_v1`.
	"""
	unknown = set(obj.keys()) - allowed
	if unknown:
		raise ValueError(
			f"{context}: unknown field(s) {sorted(unknown)!r}; "
			f"v1 accepts exactly {sorted(allowed)!r}.  Strict-v1 "
			f"rejects unknown keys to prevent unsigned data from "
			f"riding inside a signed-looking claim."
		)


# ── Public dataclasses ─────────────────────────────────────────────


@dataclass(frozen=True)
class Toolchain:
	"""The toolchain identity recorded in a cert claim.

	`driftc_version` is the compiler version that produced the
	artifact; `drift_rt_abi` pins the runtime ABI contract;
	`driftc_commit` is the git sha of the toolchain build (or
	empty if not stamped at toolchain-deploy time).
	"""
	driftc_version: str
	drift_rt_abi: int
	driftc_commit: str


@dataclass(frozen=True)
class DepGraphEntry:
	"""One entry in the full resolved transitive dep graph.

	`author_kid` / `cert_kid` may be `None` for deps the certifier
	consumed via self-verify (no cert claim available) or for the
	rare case of an author-only chain.  When present, the certifier
	is committing that EXACTLY those upstream kids signed the
	corresponding claims at cert time.

	`dep_kind` is `"direct"` or `"transitive"`; informational only
	for diagnostics — the verifier does not branch on it.
	"""
	package_id: str
	version: str
	artifact_sha256: str       # "sha256:<hex>"
	source_content_id: str     # "sha256:<hex>"
	author_kid: Optional[str]
	cert_kid: Optional[str]
	dep_kind: str              # "direct" | "transitive"


@dataclass(frozen=True)
class CertSuite:
	"""Identity + result of the certification suite the certifier ran.

	`id` is a free-form namespaced string (recommended:
	`<authority>/<suite-name>`).  Per O4, consumers may pin
	`--require-cert-suite <id>` to distinguish release-gate from
	smoke-only signatures.

	`result` is `"pass"` or `"fail"`.  A `result == "fail"` claim
	is well-formed but rejected by default at verify time.

	`result_evidence_sha256` is the hash of the suite's evidence
	bundle (test logs, output diffs, etc.).  Bundle remains
	unsigned-but-bound via the hash.
	"""
	id: str
	version: str
	result: str                       # "pass" | "fail"
	result_evidence_sha256: str       # "sha256:<hex>"


@dataclass(frozen=True)
class CertClaimBody:
	"""Signed payload of a cert claim."""
	schema_version: int          # always 1
	package_id: str
	version: str
	artifact_sha256: str         # "sha256:<hex>"
	source_content_id: str       # "sha256:<hex>"
	target: str
	toolchain: Toolchain
	dep_graph: tuple[DepGraphEntry, ...]
	cert_suite: CertSuite
	run_id: str
	run_started_utc: str         # ISO 8601
	evidence_sha256: str         # "sha256:<hex>"


@dataclass(frozen=True)
class CertSignature:
	"""One ed25519 signature on a cert claim body."""
	algo: str   # "ed25519"
	kid: str
	sig_raw: bytes  # 64-byte raw signature


@dataclass(frozen=True)
class CertClaim:
	"""Full cert claim — body plus one or more signatures.

	Multiple signatures in this array are reserved for key
	rotation / multi-region orch under a single certifier
	identity.  Multiple INDEPENDENT certifiers attesting the same
	package release each emit a SEPARATE sidecar file (per O1).
	"""
	body: CertClaimBody
	signatures: tuple[CertSignature, ...]


# ── Body canonicalization (signed bytes) ───────────────────────────


def _toolchain_to_canonical_dict(tc: Toolchain) -> dict[str, Any]:
	return {
		"driftc_version": tc.driftc_version,
		"drift_rt_abi": int(tc.drift_rt_abi),
		"driftc_commit": tc.driftc_commit,
	}


def _cert_suite_to_canonical_dict(cs: CertSuite) -> dict[str, Any]:
	return {
		"id": cs.id,
		"version": cs.version,
		"result": cs.result,
		"result_evidence_sha256": cs.result_evidence_sha256,
	}


def _dep_entry_to_canonical_dict(e: DepGraphEntry) -> dict[str, Any]:
	return {
		"package_id": e.package_id,
		"version": e.version,
		"artifact_sha256": e.artifact_sha256,
		"source_content_id": e.source_content_id,
		"author_kid": e.author_kid,
		"cert_kid": e.cert_kid,
		"dep_kind": e.dep_kind,
	}


def validate_body_shape(body: CertClaimBody) -> None:
	"""Enforce v1 value-shape rules on a dataclass-constructed body.

	The JSON loader (`_parse_body` + friends) runs equivalent checks
	when parsing an untrusted document.  But hand-built dataclasses
	bypass the loader: `make_cert_claim(CertClaimBody(...))` would
	otherwise let a caller sign canonical bytes that the loader
	would later refuse (e.g. `cert_suite.result='maybe'`,
	`dep_kind='indirect'`, malformed SCI strings, empty required
	fields).  That breaks the "emit and load enforce the same
	contract" invariant.

	This function is called from `body_signing_bytes` (so sign,
	dump, and verify-via-bytes all hit it) and from
	`_body_to_canonical_dict` (so any callable that touches the
	canonical shape sees the same gate).
	"""
	# Header.
	if body.schema_version != _BODY_SCHEMA_VERSION:
		raise ValueError(
			f"cert claim body.schema_version must be {_BODY_SCHEMA_VERSION}; "
			f"got {body.schema_version!r}"
		)
	if not isinstance(body.package_id, str) or not body.package_id:
		raise ValueError("cert claim body.package_id must be a non-empty string")
	if not isinstance(body.version, str) or not body.version:
		raise ValueError("cert claim body.version must be a non-empty string")
	if not isinstance(body.target, str) or not body.target:
		raise ValueError("cert claim body.target must be a non-empty string")
	if not isinstance(body.run_id, str) or not body.run_id:
		raise ValueError("cert claim body.run_id must be a non-empty string")
	if not isinstance(body.run_started_utc, str) or not body.run_started_utc:
		raise ValueError("cert claim body.run_started_utc must be a non-empty string")
	# SCIs.
	validate_sci(body.artifact_sha256, field="cert claim body.artifact_sha256")
	validate_sci(body.source_content_id, field="cert claim body.source_content_id")
	validate_sci(body.evidence_sha256, field="cert claim body.evidence_sha256")
	# Toolchain.
	tc = body.toolchain
	if not isinstance(tc.driftc_version, str) or not tc.driftc_version:
		raise ValueError(
			"cert claim body.toolchain.driftc_version must be a non-empty string"
		)
	if not isinstance(tc.drift_rt_abi, int) or isinstance(tc.drift_rt_abi, bool):
		raise ValueError("cert claim body.toolchain.drift_rt_abi must be an integer")
	if not isinstance(tc.driftc_commit, str):
		raise ValueError(
			"cert claim body.toolchain.driftc_commit must be a string (may be empty)"
		)
	# Cert suite.
	cs = body.cert_suite
	if not isinstance(cs.id, str) or not cs.id:
		raise ValueError("cert claim body.cert_suite.id must be a non-empty string")
	if not isinstance(cs.version, str) or not cs.version:
		raise ValueError("cert claim body.cert_suite.version must be a non-empty string")
	if cs.result not in _CERT_RESULTS:
		raise ValueError(
			f"cert claim body.cert_suite.result must be one of "
			f"{sorted(_CERT_RESULTS)!r}; got {cs.result!r}"
		)
	validate_sci(
		cs.result_evidence_sha256,
		field="cert claim body.cert_suite.result_evidence_sha256",
	)
	# Dep graph entries.
	for idx, e in enumerate(body.dep_graph):
		if not isinstance(e.package_id, str) or not e.package_id:
			raise ValueError(
				f"cert claim body.dep_graph[{idx}].package_id must be a non-empty string"
			)
		if not isinstance(e.version, str) or not e.version:
			raise ValueError(
				f"cert claim body.dep_graph[{idx}].version must be a non-empty string"
			)
		validate_sci(
			e.artifact_sha256,
			field=f"cert claim body.dep_graph[{idx}].artifact_sha256",
		)
		validate_sci(
			e.source_content_id,
			field=f"cert claim body.dep_graph[{idx}].source_content_id",
		)
		if e.author_kid is not None and (not isinstance(e.author_kid, str) or not e.author_kid):
			raise ValueError(
				f"cert claim body.dep_graph[{idx}].author_kid must be a non-empty string or None"
			)
		if e.cert_kid is not None and (not isinstance(e.cert_kid, str) or not e.cert_kid):
			raise ValueError(
				f"cert claim body.dep_graph[{idx}].cert_kid must be a non-empty string or None"
			)
		if e.dep_kind not in _DEP_KINDS:
			raise ValueError(
				f"cert claim body.dep_graph[{idx}].dep_kind must be one of "
				f"{sorted(_DEP_KINDS)!r}; got {e.dep_kind!r}"
			)


def _body_to_canonical_dict(body: CertClaimBody) -> dict[str, Any]:
	"""Convert the body dataclass to a canonical-shaped dict.

	Runs `validate_body_shape` first so emit and load enforce the
	same value contract.  `dep_graph` is then sorted by
	`(package_id, version)`; duplicate tuples are rejected (canonical
	sort tie-break would otherwise be input-order-sensitive).
	"""
	validate_body_shape(body)
	seen: set[tuple[str, str]] = set()
	for e in body.dep_graph:
		key = (e.package_id, e.version)
		if key in seen:
			raise ValueError(
				f"cert claim body.dep_graph: duplicate entry for "
				f"(package_id={e.package_id!r}, version={e.version!r}) "
				f"— each (pkg, version) pair must appear at most once"
			)
		seen.add(key)
	dep_graph_dicts = sorted(
		[_dep_entry_to_canonical_dict(e) for e in body.dep_graph],
		key=lambda d: (d["package_id"], d["version"]),
	)
	return {
		"schema_version": int(body.schema_version),
		"package_id": body.package_id,
		"version": body.version,
		"artifact_sha256": body.artifact_sha256,
		"source_content_id": body.source_content_id,
		"target": body.target,
		"toolchain": _toolchain_to_canonical_dict(body.toolchain),
		"dep_graph": dep_graph_dicts,
		"cert_suite": _cert_suite_to_canonical_dict(body.cert_suite),
		"run_id": body.run_id,
		"run_started_utc": body.run_started_utc,
		"evidence_sha256": body.evidence_sha256,
	}


def body_signing_bytes(body: CertClaimBody) -> bytes:
	"""Return the exact bytes the certifier signs over."""
	return canonical_json_bytes(_body_to_canonical_dict(body))


# ── Sign / make / add_signature ────────────────────────────────────


def sign_body(body: CertClaimBody, priv_seed32: bytes) -> CertSignature:
	"""Sign a body with an Ed25519 seed.  Returns the signature record."""
	msg = body_signing_bytes(body)
	sig_raw, pubkey_raw = ed25519_sign_from_seed(priv_seed32=priv_seed32, message=msg)
	kid = compute_ed25519_kid(pubkey_raw)
	return CertSignature(algo="ed25519", kid=kid, sig_raw=sig_raw)


def make_cert_claim(body: CertClaimBody, priv_seed32: bytes) -> CertClaim:
	"""Construct a cert claim with ONE signature from the given seed."""
	sig = sign_body(body, priv_seed32)
	return CertClaim(body=body, signatures=(sig,))


def add_signature(claim: CertClaim, priv_seed32: bytes) -> CertClaim:
	"""Return a new cert claim with an additional signature co-signing
	the same body.  Use case: key rotation / multi-region orch under
	a single certifier identity.  Independent certifiers emit
	separate sidecar files instead."""
	new_sig = sign_body(claim.body, priv_seed32)
	return CertClaim(
		body=claim.body,
		signatures=claim.signatures + (new_sig,),
	)


# ── JSON load / dump ───────────────────────────────────────────────


def dump_cert_claim_json(claim: CertClaim) -> str:
	"""Serialize a cert claim to canonical sidecar JSON.

	Deterministic output: keys sorted, signatures sorted by kid.
	Trailing newline.
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


def load_cert_claim_json(text: str) -> CertClaim:
	"""Parse and shape-validate a cert-claim sidecar.

	Strict v1 only.  Any deviation raises `ValueError` with
	context.  Unknown keys at any level are rejected (per
	`_reject_unknown_keys`).

	Does NOT verify signatures or check `artifact_sha256` /
	`source_content_id` against any external state — that's
	`verify_cert_claim_for_module`'s job in slice 4 composition.
	"""
	obj = json.loads(text)
	if not isinstance(obj, dict):
		raise ValueError("cert claim must be a JSON object")
	_reject_unknown_keys(obj, _ENVELOPE_KEYS, context="cert claim envelope")

	fmt = obj.get("format")
	ver = obj.get("version")
	if fmt != _FORMAT_TAG:
		raise ValueError(
			f"unsupported cert claim format: expected {_FORMAT_TAG!r}, got {fmt!r}"
		)
	if ver != _FORMAT_VERSION:
		raise ValueError(
			f"unsupported cert claim version: expected v{_FORMAT_VERSION}, "
			f"got v{ver!r}"
		)

	body_raw = obj.get("body")
	if not isinstance(body_raw, dict):
		raise ValueError("cert claim 'body' must be a JSON object")
	body = _parse_body(body_raw)

	sigs_raw = obj.get("signatures")
	if not isinstance(sigs_raw, list):
		raise ValueError("cert claim 'signatures' must be a list")
	if not sigs_raw:
		raise ValueError("cert claim 'signatures' must contain at least one signature")
	signatures = tuple(_parse_signature(s, idx) for idx, s in enumerate(sigs_raw))

	return CertClaim(body=body, signatures=signatures)


def _parse_toolchain(obj: Any) -> Toolchain:
	if not isinstance(obj, dict):
		raise ValueError("cert claim body.toolchain must be a JSON object")
	_reject_unknown_keys(obj, _TOOLCHAIN_KEYS, context="cert claim body.toolchain")
	dv = obj.get("driftc_version")
	abi = obj.get("drift_rt_abi")
	commit = obj.get("driftc_commit")
	if not isinstance(dv, str) or not dv:
		raise ValueError("cert claim body.toolchain.driftc_version must be a non-empty string")
	if not isinstance(abi, int) or isinstance(abi, bool):
		raise ValueError("cert claim body.toolchain.drift_rt_abi must be an integer")
	if not isinstance(commit, str):
		raise ValueError("cert claim body.toolchain.driftc_commit must be a string (may be empty)")
	return Toolchain(driftc_version=dv, drift_rt_abi=abi, driftc_commit=commit)


def _parse_cert_suite(obj: Any) -> CertSuite:
	if not isinstance(obj, dict):
		raise ValueError("cert claim body.cert_suite must be a JSON object")
	_reject_unknown_keys(obj, _CERT_SUITE_KEYS, context="cert claim body.cert_suite")
	cs_id = obj.get("id")
	cs_ver = obj.get("version")
	result = obj.get("result")
	evidence = obj.get("result_evidence_sha256")
	if not isinstance(cs_id, str) or not cs_id:
		raise ValueError("cert claim body.cert_suite.id must be a non-empty string")
	if not isinstance(cs_ver, str) or not cs_ver:
		raise ValueError("cert claim body.cert_suite.version must be a non-empty string")
	if result not in _CERT_RESULTS:
		raise ValueError(
			f"cert claim body.cert_suite.result must be one of "
			f"{sorted(_CERT_RESULTS)!r}; got {result!r}"
		)
	validate_sci(evidence, field="cert claim body.cert_suite.result_evidence_sha256")
	return CertSuite(id=cs_id, version=cs_ver, result=result, result_evidence_sha256=evidence)


def _parse_dep_entry(obj: Any, idx: int) -> DepGraphEntry:
	if not isinstance(obj, dict):
		raise ValueError(f"cert claim body.dep_graph[{idx}] must be a JSON object")
	_reject_unknown_keys(
		obj, _DEP_GRAPH_ENTRY_KEYS,
		context=f"cert claim body.dep_graph[{idx}]",
	)
	package_id = obj.get("package_id")
	version_str = obj.get("version")
	artifact_sha = obj.get("artifact_sha256")
	sci = obj.get("source_content_id")
	author_kid = obj.get("author_kid")
	cert_kid = obj.get("cert_kid")
	dep_kind = obj.get("dep_kind")

	if not isinstance(package_id, str) or not package_id:
		raise ValueError(
			f"cert claim body.dep_graph[{idx}].package_id must be a non-empty string"
		)
	if not isinstance(version_str, str) or not version_str:
		raise ValueError(
			f"cert claim body.dep_graph[{idx}].version must be a non-empty string"
		)
	validate_sci(artifact_sha, field=f"cert claim body.dep_graph[{idx}].artifact_sha256")
	validate_sci(sci, field=f"cert claim body.dep_graph[{idx}].source_content_id")

	if author_kid is not None:
		if not isinstance(author_kid, str) or not author_kid:
			raise ValueError(
				f"cert claim body.dep_graph[{idx}].author_kid must be a string or null"
			)
	if cert_kid is not None:
		if not isinstance(cert_kid, str) or not cert_kid:
			raise ValueError(
				f"cert claim body.dep_graph[{idx}].cert_kid must be a string or null"
			)
	if dep_kind not in _DEP_KINDS:
		raise ValueError(
			f"cert claim body.dep_graph[{idx}].dep_kind must be one of "
			f"{sorted(_DEP_KINDS)!r}; got {dep_kind!r}"
		)
	return DepGraphEntry(
		package_id=package_id,
		version=version_str,
		artifact_sha256=artifact_sha,
		source_content_id=sci,
		author_kid=author_kid,
		cert_kid=cert_kid,
		dep_kind=dep_kind,
	)


def _parse_body(obj: dict) -> CertClaimBody:
	"""Strict body parse.  Unknown keys at every nesting level
	are rejected (toolchain, cert_suite, each dep_graph entry).
	Duplicate (package_id, version) tuples in dep_graph are
	rejected (canonical sort tie-break would be order-dependent)."""
	_reject_unknown_keys(obj, _BODY_KEYS, context="cert claim body")

	schema_ver = obj.get("schema_version")
	if schema_ver != _BODY_SCHEMA_VERSION:
		raise ValueError(
			f"cert claim body schema_version: expected {_BODY_SCHEMA_VERSION}, "
			f"got {schema_ver!r}"
		)
	package_id = obj.get("package_id")
	version_str = obj.get("version")
	if not isinstance(package_id, str) or not package_id:
		raise ValueError("cert claim body.package_id must be a non-empty string")
	if not isinstance(version_str, str) or not version_str:
		raise ValueError("cert claim body.version must be a non-empty string")

	validate_sci(obj.get("artifact_sha256"), field="cert claim body.artifact_sha256")
	validate_sci(obj.get("source_content_id"), field="cert claim body.source_content_id")

	target = obj.get("target")
	if not isinstance(target, str) or not target:
		raise ValueError("cert claim body.target must be a non-empty string")

	toolchain = _parse_toolchain(obj.get("toolchain"))

	dep_graph_raw = obj.get("dep_graph")
	if not isinstance(dep_graph_raw, list):
		raise ValueError("cert claim body.dep_graph must be a list")
	dep_entries: list[DepGraphEntry] = []
	seen_keys: set[tuple[str, str]] = set()
	for idx, d in enumerate(dep_graph_raw):
		entry = _parse_dep_entry(d, idx)
		key = (entry.package_id, entry.version)
		if key in seen_keys:
			raise ValueError(
				f"cert claim body.dep_graph[{idx}]: duplicate "
				f"(package_id={entry.package_id!r}, version={entry.version!r}); "
				f"each (pkg, version) pair must appear at most once"
			)
		seen_keys.add(key)
		dep_entries.append(entry)

	cert_suite = _parse_cert_suite(obj.get("cert_suite"))

	run_id = obj.get("run_id")
	run_started_utc = obj.get("run_started_utc")
	if not isinstance(run_id, str) or not run_id:
		raise ValueError("cert claim body.run_id must be a non-empty string")
	if not isinstance(run_started_utc, str) or not run_started_utc:
		raise ValueError("cert claim body.run_started_utc must be a non-empty string")
	validate_sci(obj.get("evidence_sha256"), field="cert claim body.evidence_sha256")

	return CertClaimBody(
		schema_version=schema_ver,
		package_id=package_id,
		version=version_str,
		artifact_sha256=obj["artifact_sha256"],
		source_content_id=obj["source_content_id"],
		target=target,
		toolchain=toolchain,
		dep_graph=tuple(dep_entries),
		cert_suite=cert_suite,
		run_id=run_id,
		run_started_utc=run_started_utc,
		evidence_sha256=obj["evidence_sha256"],
	)


def _parse_signature(obj: Any, idx: int) -> CertSignature:
	"""Strict per-signature parse.  Unknown keys rejected."""
	if not isinstance(obj, dict):
		raise ValueError(f"cert claim signatures[{idx}] must be a JSON object")
	_reject_unknown_keys(
		obj, _SIGNATURE_KEYS,
		context=f"cert claim signatures[{idx}]",
	)
	algo = obj.get("algo")
	if algo != "ed25519":
		raise ValueError(
			f"cert claim signatures[{idx}]: algo must be 'ed25519'; got {algo!r}"
		)
	kid = obj.get("kid")
	if not isinstance(kid, str) or not kid:
		raise ValueError(f"cert claim signatures[{idx}]: kid must be a non-empty string")
	sig_b64 = obj.get("sig")
	if not isinstance(sig_b64, str):
		raise ValueError(f"cert claim signatures[{idx}]: 'sig' must be a base64 string")
	try:
		sig_raw = b64_decode(sig_b64)
	except Exception as err:
		raise ValueError(
			f"cert claim signatures[{idx}]: 'sig' is not valid base64: {err}"
		) from err
	if len(sig_raw) != 64:
		raise ValueError(
			f"cert claim signatures[{idx}]: ed25519 signature must be 64 bytes; "
			f"got {len(sig_raw)}"
		)
	return CertSignature(algo=algo, kid=kid, sig_raw=sig_raw)


# ── Sidecar filename (O1) ──────────────────────────────────────────


# A kid character that is safe in any reasonable filename.  ed25519
# kids look like `ed25519:<base64-with-padding>`; the `:` and `=`
# need filesystem-safe escaping on some hosts.  For the canonical
# v1 filename we URL-encode the offending bytes so the FS sees a
# benign string while readers parse the trust kid from the file
# body, not from the filename.
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _filename_escape(s: str) -> str:
	"""Percent-encode any character outside the filesystem-safe set
	`[A-Za-z0-9._-]`.  Pure-ASCII alphanumerics + `._-` pass through
	for readability; everything else becomes `%HH`.  Defense-in-depth
	for filenames so package ids or kids containing `/`, `:`, `=`,
	spaces, or path traversal characters can't escape the
	intended directory.
	"""
	def _enc(c: str) -> str:
		return f"%{ord(c):02X}"
	return "".join(_enc(c) if _FILENAME_UNSAFE.match(c) else c for c in s)


def cert_claim_filename(package_id: str, certifier_kid: str) -> str:
	"""Canonical per-certifier sidecar filename: `<pkg>.cert-claim.<kid>.json`.

	BOTH `package_id` and `certifier_kid` are URL-encoded for
	filesystem safety so characters like `/`, `:`, `=`, or spaces
	cannot turn the filename into a path or break naming on hosts
	that reject them.  Pure-ASCII alphanumerics + `._-` pass
	through unchanged.

	`<kid>` is the FULL kid (no short prefix) so per-certifier
	files for different certifiers cannot collide.  Readers MUST
	parse the canonical kid and package_id from the file body —
	the filename is purely a disambiguator for multiple certifiers
	on disk, not a trust input.
	"""
	if not isinstance(package_id, str) or not package_id:
		raise ValueError("cert_claim_filename: package_id must be a non-empty string")
	if not isinstance(certifier_kid, str) or not certifier_kid:
		raise ValueError("cert_claim_filename: certifier_kid must be a non-empty string")
	safe_pkg = _filename_escape(package_id)
	safe_kid = _filename_escape(certifier_kid)
	return f"{safe_pkg}.cert-claim.{safe_kid}.json"


# ── Signature verification (low-level) ─────────────────────────────


def verify_cert_claim_signatures(
	claim: CertClaim,
	trust: TrustStore,
) -> set[str]:
	"""Return the set of trust-known kids whose signatures verify.

	Crypto only — does NOT consult the namespace allow-lists.  Use
	`verify_cert_claim_for_module` for the full composition check.
	A signature whose kid is not in `trust.keys_by_kid` is skipped
	(the consumer cannot validate signatures by an unknown key).
	A signature that fails ed25519 verification is also skipped.
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


# ── Dep-graph closure check (O3) ───────────────────────────────────


@dataclass(frozen=True)
class ResolvedDep:
	"""Consumer's view of one dep currently being loaded.  The
	verifier asserts the cert claim's dep_graph covers every entry
	in the consumer's resolved closure."""
	package_id: str
	version: str
	artifact_sha256: str       # "sha256:<hex>"
	source_content_id: str     # "sha256:<hex>"


def find_dep_entry(claim: CertClaim, *, package_id: str, version: str) -> Optional[DepGraphEntry]:
	"""Locate a dep_graph entry by (package_id, version).

	Returns None if not present.  Used by the composition verifier
	in slice 4 to assert closure coverage.
	"""
	for e in claim.body.dep_graph:
		if e.package_id == package_id and e.version == version:
			return e
	return None


def check_dep_graph_covers(
	claim: CertClaim,
	resolved_closure: list[ResolvedDep],
) -> Optional[str]:
	"""Verify the cert claim's dep_graph fully covers
	`resolved_closure`.  Returns None on success, or a one-line
	human-readable reason string on failure.

	"Covers" means: every dep in `resolved_closure` appears in
	`claim.body.dep_graph` with matching `artifact_sha256` and
	`source_content_id`.

	A dep in the dep_graph but NOT in the consumer's resolved
	closure is permitted (the certifier may have certified a
	larger graph than this consumer loaded; e.g. dev-only deps).
	Only the consumer's actual closure must be covered.

	Per O3: if changing any package in the consumer's resolved
	graph changes what the certifier signed, the cert claim is
	meaningful.  This check is the gate that enforces it.
	"""
	for dep in resolved_closure:
		entry = find_dep_entry(claim, package_id=dep.package_id, version=dep.version)
		if entry is None:
			return (
				f"cert claim dep_graph missing entry for "
				f"({dep.package_id!r}, version={dep.version!r}); "
				f"consumer loaded this dep but the certifier did not "
				f"attest it"
			)
		if entry.artifact_sha256 != dep.artifact_sha256:
			return (
				f"cert claim dep_graph entry for "
				f"({dep.package_id!r}, {dep.version!r}): "
				f"artifact_sha256 mismatch.  Consumer sees "
				f"{dep.artifact_sha256!r}; certifier attested "
				f"{entry.artifact_sha256!r}"
			)
		if entry.source_content_id != dep.source_content_id:
			return (
				f"cert claim dep_graph entry for "
				f"({dep.package_id!r}, {dep.version!r}): "
				f"source_content_id mismatch.  Consumer sees "
				f"{dep.source_content_id!r}; certifier attested "
				f"{entry.source_content_id!r}"
			)
	return None


# ── Full composition verifier (used by slice 4) ─────────────────────


@dataclass(frozen=True)
class CertClaimVerifyResult:
	"""Outcome of `verify_cert_claim_for_module`.

	`ok=True` iff every gate passed.  `accepted_kid` is the kid that
	satisfied trust.  `reason` carries a human-readable explanation
	on failure.
	"""
	ok: bool
	accepted_kid: Optional[str]
	reason: str


def verify_cert_claim_for_module(
	claim: CertClaim,
	trust: TrustStore,
	module_id: str,
	*,
	expected_package_id: str,
	expected_version: str,
	artifact_sha256: str,
	expected_source_content_id: str,
	resolved_closure: list[ResolvedDep],
	require_certifier: Optional[str] = None,
	require_cert_suite: Optional[str] = None,
) -> CertClaimVerifyResult:
	"""Full cert-claim verification for one module_id.

	Gates (each must pass, evaluated in order):
	  1. `claim.body.package_id == expected_package_id` AND
	     `claim.body.version == expected_version` — pins this claim
	     to THIS package release.  Without this gate a cert claim
	     for an attacker-controlled package could pass verification
	     for an unrelated module if the other gates happen to align
	     (replay / substitution attack).
	  2. `claim.body.artifact_sha256 == artifact_sha256` — binds
	     the cert claim to the on-disk artifact bytes.
	  3. `claim.body.source_content_id == expected_source_content_id`
	     — binds the cert claim to the author claim's SCI.
	  4. `claim.body.cert_suite.result == "pass"` — refuse
	     known-failing certifications by default.
	  5. `dep_graph` covers `resolved_closure` (per
	     `check_dep_graph_covers`).
	  6. `require_certifier`, if set, must equal the verifying kid.
	  7. `require_cert_suite`, if set, must equal
	     `claim.body.cert_suite.id`.
	  8. At least one signature verifies AND the signer's kid is in
	     `trust.allowed_certifiers_for_module(module_id)`.

	Revocation is handled inside `allowed_certifiers_for_module`.
	"""
	# Gate 1: package identity pinning.  Caller MUST pass the
	# package_id/version they expect the claim to be for — the
	# verifier cannot infer it from `module_id` alone (one package
	# typically owns multiple modules).
	if claim.body.package_id != expected_package_id:
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"cert claim body.package_id ({claim.body.package_id!r}) "
				f"does not match expected package_id ({expected_package_id!r}); "
				f"this claim is for a different package"
			),
		)
	if claim.body.version != expected_version:
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"cert claim body.version ({claim.body.version!r}) does not "
				f"match expected version ({expected_version!r}); this claim is "
				f"for a different release of {expected_package_id!r}"
			),
		)

	# Gate 2: artifact_sha256.
	if claim.body.artifact_sha256 != artifact_sha256:
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"cert claim body.artifact_sha256 ({claim.body.artifact_sha256!r}) "
				f"does not match on-disk artifact hash ({artifact_sha256!r})"
			),
		)

	# Gate 3: source_content_id.
	if claim.body.source_content_id != expected_source_content_id:
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"cert claim body.source_content_id ({claim.body.source_content_id!r}) "
				f"does not match author claim's source_content_id "
				f"({expected_source_content_id!r})"
			),
		)

	# Gate 4: cert_suite.result.
	if claim.body.cert_suite.result != "pass":
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"cert claim body.cert_suite.result is "
				f"{claim.body.cert_suite.result!r}; only 'pass' is accepted"
			),
		)

	# Gate 5: dep_graph closure.
	closure_err = check_dep_graph_covers(claim, resolved_closure)
	if closure_err is not None:
		return CertClaimVerifyResult(ok=False, accepted_kid=None, reason=closure_err)

	# Gate 7: required cert suite id (per --require-cert-suite).
	if require_cert_suite is not None and claim.body.cert_suite.id != require_cert_suite:
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"cert claim body.cert_suite.id is {claim.body.cert_suite.id!r}; "
				f"required {require_cert_suite!r}"
			),
		)

	# Gate 8: signature + role lookup.
	verified = verify_cert_claim_signatures(claim, trust)
	if not verified:
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason="no signature on cert claim verifies against any key in the trust store",
		)
	allowed_certifiers = trust.allowed_certifiers_for_module(module_id)
	if not allowed_certifiers:
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"trust store authorizes no certifier-role kids for module "
				f"{module_id!r}; consumer must self-verify or add a certifier "
				f"to the trust store for this namespace"
			),
		)
	candidates = sorted(verified & allowed_certifiers)
	if not candidates:
		return CertClaimVerifyResult(
			ok=False,
			accepted_kid=None,
			reason=(
				f"cert claim signatures verified but none of the signer kids are "
				f"trusted in the 'certifiers' role for module {module_id!r}.  "
				f"Verified kids: {sorted(verified)!r}.  "
				f"Trusted certifiers for namespace: {sorted(allowed_certifiers)!r}"
			),
		)

	# Gate 6: --require-certifier (applied after we know who would have been accepted).
	if require_certifier is not None:
		if require_certifier not in candidates:
			return CertClaimVerifyResult(
				ok=False,
				accepted_kid=None,
				reason=(
					f"required certifier kid {require_certifier!r} did not sign this "
					f"cert claim or is not trusted for module {module_id!r}.  "
					f"Verified+trusted kids: {candidates!r}"
				),
			)
		accepted_kid = require_certifier
	else:
		accepted_kid = candidates[0]

	return CertClaimVerifyResult(ok=True, accepted_kid=accepted_kid, reason="")
