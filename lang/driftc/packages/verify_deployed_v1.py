# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Deployed-package verification facade (operator-facing).

`verify_deployed_package` is the standalone, post-deploy analog of the
consumer-side `provider_v1` load gate: given a deployed package
DIRECTORY (the `drift deploy` output layout), it verifies the artifact +
sidecars + provenance as one consistent set, reusing the existing
`verify_v1` composition verifier. It exists so `drift trust
verify-package` (the CLI) has ONE stable surface to call instead of
reaching into the verifier engine / trust loader / container reader
directly — the CLI layer is only permitted to couple to this facade
(see `test_import_boundaries.py`), not to those internals.

This is NOT new crypto and NOT a looser trust path: the acceptance
decision is entirely `verify_v1.compose_verify`'s. The facade only adds
orchestration (locate + decompress the artifact, build the package
identity from the manifest stamp, resolve trust material) plus two
checks that are inherently outside per-module claim verification: the
provenance artifact-hash cross-check and the `--expect-*` assertions.

The consumer dep-graph closure check (O3) is intentionally NOT run — it
is only meaningful against a specific consumer's resolved closure, so
`resolved_closure=[]` is passed (the same vacuous form the deploy
index-time gate uses).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The dev/no-evidence sentinel: `drift deploy --cert-suite-no-evidence`
# records sha256("") as the cert suite's result_evidence_sha256 to mean
# "this suite legitimately produced no evidence" (dev lane). We surface
# it so a green verify isn't mistaken for a real certification.
_EMPTY_EVIDENCE_SHA = "sha256:" + hashlib.sha256(b"").hexdigest()


class VerifyPackageUsageError(ValueError):
	"""The command was INVOKED wrong — not "the package failed to verify."

	Raised only for invocation/layout problems that are independent of the
	deployed package's byte contents: the path is not a directory or is a
	bare `.zdmp`, the directory holds zero or several `.zdmp` artifacts, no
	trust source was supplied, trust flags conflict, or a CLI-supplied
	trust input (`--trust-store` / `--author-pubkey-b64` / the profile's
	key) is unreadable or malformed.  The CLI maps this to argparse usage
	(exit 2).

	Everything that is a property of the package itself — corrupt artifact
	bytes, a malformed manifest, bad/missing sidecars, a failed signature
	or trust check, a provenance mismatch — is a verification OUTCOME, not
	misuse: it is folded into the returned report as `ok=false` with an
	`errors[]` entry (exit 1), never raised.  Subclasses `ValueError` so
	older `except ValueError` callers keep working.
	"""


def new_report(package_dir: Path | str) -> dict[str, Any]:
	"""An empty verification report with EVERY standard key present.

	The single source of the report schema.  `verify_deployed_package`
	builds on it, and the CLI's unexpected-error backstop uses it too, so
	a backstop result is the same shape a normal failure is — CI consumers
	see one stable set of keys regardless of how a run failed.
	"""
	return {
		"ok": False,
		"package_dir": str(package_dir),
		"package_id": None, "version": None,
		"source_content_id": None, "artifact_sha256": None,
		"trust_source": None, "mode": None,
		"author_kid": None, "certifier_kid": None, "certifier_kids": [],
		"provenance_ok": None, "no_evidence_sentinel": False,
		"modules": [], "warnings": [], "errors": [],
	}


def error_report(package_dir: Path | str, *, code: str, message: str) -> dict[str, Any]:
	"""A full-schema `ok=false` report carrying a single error.  Used by
	the CLI backstop for an exception the facade did not anticipate, so the
	emitted JSON still has the standard keys."""
	report = new_report(package_dir)
	report["errors"].append({"code": code, "message": message})
	return report


@dataclass(frozen=True)
class VerifyPackageOptions:
	"""Inputs for deployed-package verification.

	`package_dir` is the deployed package DIRECTORY (not a `.zdmp`): the
	sidecars / provenance the verifier needs live beside the artifact, so
	a bare `.zdmp` cannot be verified in isolation.

	Trust resolution is one of:
	  - `trust_store_path` — a v1 trust store JSON;
	  - `author_pubkey_b64` (+ optional `author_namespaces`) — synthesize
	    a single-key store granting that key author+certifier for the
	    given namespaces (default: the package's own modules). The CLI
	    feeds the `--author-profile` form in through here, passing the
	    profile's pubkey + declared namespaces.
	If none of the three is given it is a usage error, UNLESS
	`allow_bundled_pubkey=True`, which opts into verifying against the
	package's OWN bundled `<pkg>.author-pubkey.b64` (self-consistency:
	proves integrity + self-signature, NOT third-party trust). The opt-in
	exists so a CI gate cannot mistake "no key supplied" for "trusted".
	"""
	package_dir: Path
	trust_store_path: Path | None = None
	author_pubkey_b64: str | None = None
	author_namespaces: list[str] | None = None
	allow_bundled_pubkey: bool = False
	expect_version: str | None = None
	expect_sci: str | None = None


def _decode_ed25519_pubkey(pubkey_b64: str) -> tuple[bytes, str]:
	"""Decode + validate an Ed25519 pubkey (raises `ValueError` if it is not
	valid base64 or not 32 bytes). Returns `(pub_raw, kid)`.

	Split out from `_synth_single_key_trust_store` so the CLI-supplied
	pubkey can be VALIDATED before any artifact IO (a bad `--author-pubkey-b64`
	is an invocation error) while the store itself is BUILT later, once the
	manifest's module ids are known as default namespaces.

	This GUARANTEES it only ever raises `ValueError`: `base64.b64decode`
	signals invalid input with `binascii.Error` (and non-ASCII with
	`UnicodeEncodeError`) — both happen to subclass `ValueError` today, but
	we don't rely on that, so callers can catch a single type to map a bad
	pubkey to a usage error.
	"""
	from lang.drift.crypto import b64_decode, compute_ed25519_kid
	try:
		pub_raw = b64_decode(pubkey_b64.strip())
	except Exception as err:
		raise ValueError(f"not valid base64: {err}") from err
	if len(pub_raw) != 32:
		raise ValueError(
			f"author pubkey must decode to 32 bytes (Ed25519), got {len(pub_raw)}"
		)
	return pub_raw, compute_ed25519_kid(pub_raw)


def _synth_single_key_trust_store(*, pubkey_b64: str, namespaces: list[str]):
	"""Build an in-memory TrustStore granting one key BOTH roles for
	`namespaces` (foundation-bootstrap shape). Returns (TrustStore, kid)."""
	from lang.driftc.packages.trust_v1 import (
		NamespaceRoles, TrustStore, TrustedKey,
	)
	pub_raw, kid = _decode_ed25519_pubkey(pubkey_b64)
	key = TrustedKey(
		algo="ed25519", kid=kid, pubkey_raw=pub_raw,
		label="verify-package (synthesized, not persisted)",
	)
	roles = {
		ns: NamespaceRoles(authors=frozenset({kid}), certifiers=frozenset({kid}))
		for ns in namespaces
	}
	return TrustStore(keys_by_kid={kid: key}, roles_by_namespace=roles,
		revoked_kids=frozenset()), kid


def _load_provenance_fields(prov_path: Path) -> dict[str, Any]:
	"""Extract the inner `provenance` object (schema v4) from a compressed
	bundle (zstd + JSON).  Raises ValueError if the bundle is malformed."""
	import zstandard
	raw = zstandard.ZstdDecompressor().decompress(prov_path.read_bytes())
	bundle = json.loads(raw)
	if not isinstance(bundle, dict):
		raise ValueError("provenance bundle is not a JSON object")
	prov = bundle.get("provenance")
	if not isinstance(prov, dict):
		raise ValueError("provenance bundle missing 'provenance' object")
	return prov


def _provenance_artifact_sha256(prov_path: Path) -> str:
	"""Extract `provenance.artifact_sha256` from a compressed provenance
	bundle (zstd + JSON)."""
	prov = _load_provenance_fields(prov_path)
	sha = prov.get("artifact_sha256")
	if not isinstance(sha, str) or not sha.startswith("sha256:"):
		raise ValueError("provenance 'artifact_sha256' missing or malformed")
	return sha


def _cross_check_provenance(
	prov: dict[str, Any], report: dict[str, Any], *,
	expected_kind: str, artifact_sha: str, sci: str,
	pkg_id: str, pkg_ver: str,
) -> bool:
	"""Enforce the v4 provenance inner fields agree with the package identity
	+ claims (artifact_kind, artifact_sha256, source_content_id, artifact_name,
	artifact_version).  Appends one report error per failure; returns True iff
	all pass.  NO two-way fallback: a missing/malformed provenance
	source_content_id is a hard failure (provenance is the third SCI leg)."""
	from lang.driftc.packages.source_content_id import validate_sci
	ok = True

	def _fail(code: str, msg: str) -> None:
		nonlocal ok
		ok = False
		report["errors"].append({"code": code, "message": msg})

	# v4 clean break: a certified artifact's provenance MUST be schema v4.
	# A legacy v3 bundle is rejected even if it happens to carry the new
	# fields (no quiet acceptance of pre-v4 provenance).
	psv = prov.get("schema_version")
	if psv != 4:
		_fail("provenance-schema-version",
			f"provenance schema_version {psv!r} != 4 (v4 is required)")
	pk = prov.get("artifact_kind")
	if pk != expected_kind:
		_fail("provenance-kind-mismatch",
			f"provenance artifact_kind {pk!r} != {expected_kind!r}")
	psha = prov.get("artifact_sha256")
	if psha != artifact_sha:
		_fail("provenance-artifact-mismatch",
			f"provenance artifact_sha256 {psha!r} != on-disk artifact {artifact_sha}")
	psci = prov.get("source_content_id")
	sci_shape_ok = True
	try:
		validate_sci(psci, field="provenance source_content_id")
	except Exception as e:
		sci_shape_ok = False
		_fail("provenance-sci-invalid",
			f"provenance source_content_id missing or malformed: {e}")
	if sci_shape_ok and psci != sci:
		_fail("provenance-sci-mismatch",
			f"provenance source_content_id {psci} != package identity SCI {sci}")
	pname = prov.get("artifact_name")
	if pname != pkg_id:
		_fail("provenance-name-mismatch",
			f"provenance artifact_name {pname!r} != package id {pkg_id!r}")
	pver = prov.get("artifact_version")
	if pver != pkg_ver:
		_fail("provenance-version-mismatch",
			f"provenance artifact_version {pver!r} != package version {pkg_ver!r}")
	return ok


def verify_deployed_package(opts: VerifyPackageOptions) -> dict[str, Any]:
	"""Verify a deployed package directory end to end. Returns a report:

	    {
	      "ok": bool,
	      "package_dir": str,
	      "package_id"/"version"/"source_content_id"/"artifact_sha256": str,
	      "trust_source": str,            # how key material was resolved
	      "mode": str|None,               # certifier-shortcut / self-verify
	      "author_kid"/"certifier_kid": str|None,
	      "certifier_kids": [str],        # every accepted certifier kid (deduped)
	      "provenance_ok": bool|None,     # None = no provenance present
	      "no_evidence_sentinel": bool,   # dev/no-evidence cert accepted
	      "modules": [{module_id, ok, mode, author_kid, certifier_kid, reason}],
	      "warnings": [str], "errors": [{code, message, ...}],
	    }

	`ok` is the AND of every module's author+cert verification, the
	provenance cross-check (when present), and the `--expect-*`
	assertions.

	Raises `VerifyPackageUsageError` ONLY for command-invocation problems
	(not a directory, zero/many `.zdmp`, no/conflicting trust source, an
	unreadable CLI-supplied trust input) so the CLI maps those to exit 2.
	Problems with the package ITSELF — corrupt artifact bytes, a malformed
	manifest, bad/missing sidecars, a failed signature/trust check, a
	provenance mismatch — are NOT raised: they are returned as an
	`ok=false` report with `errors[]`, which the CLI renders and maps to
	exit 1 (including under `--json`).
	"""
	from lang.driftc.packages.dmir_pkg_v0 import load_dmir_pkg_v0_from_bytes
	from lang.driftc.packages.verify_harness_v1 import (
		build_package_identity, iter_trust_module_ids, module_is_reserved,
		verify_package_modules,
	)
	from lang.driftc.packages.zdmp import decompress_zdmp

	report = new_report(opts.package_dir)

	# 1. Input: a directory, never a bare artifact path.  These are
	#    invocation/layout problems (how the command was called), not
	#    properties of a package's contents, so they are usage errors.
	d = opts.package_dir
	if d.is_file() and d.suffix in (".zdmp", ".dmp"):
		raise VerifyPackageUsageError(
			f"{d} is an artifact file; pass the package DIRECTORY (its "
			f"parent), not the .zdmp — the sidecars and provenance the "
			f"verifier needs live beside it"
		)
	if not d.is_dir():
		raise VerifyPackageUsageError(f"not a directory: {d}")
	zdmps = sorted(d.glob("*.zdmp"))
	if not zdmps:
		raise VerifyPackageUsageError(
			f"no .zdmp artifact found in {d}; expected a drift-deploy "
			f"output directory"
		)
	if len(zdmps) > 1:
		raise VerifyPackageUsageError(
			f"multiple .zdmp artifacts in {d}: "
			f"{[p.name for p in zdmps]}; expected exactly one"
		)
	zdmp_path = zdmps[0]

	# 1b. The trust SOURCE — which flag was passed, and whether a
	#     CLI-supplied input is itself well-formed — is purely a property of
	#     the INVOCATION, not of the package, so resolve/validate it BEFORE
	#     reading the artifact.  Otherwise a corrupt package could mask bad
	#     invocation input (a missing trust flag, an unreadable --trust-store,
	#     a malformed --author-pubkey-b64) as a malformed-artifact
	#     verification failure (exit 1), making the exit code for bad
	#     invocation non-deterministic.  We LOAD --trust-store and VALIDATE
	#     --author-pubkey-b64 here; only namespace defaulting (which needs
	#     the manifest's module ids) is deferred to step 3.  The bundled
	#     pubkey is part of the PACKAGE, so it stays in step 3 (its absence
	#     is a package-completeness failure, exit 1, not a usage error).
	store = None  # set here for --trust-store, else built in step 3
	# Exactly one trust source.  The CLI puts these in a mutually-exclusive
	# argparse group, but this facade is now the sanctioned integration
	# surface, so it enforces the invariant itself rather than trusting
	# every caller to — accepting `allow_bundled_pubkey` alongside an
	# explicit source and silently preferring one would be a quiet trust
	# surprise for a direct API caller.
	_selected = [
		name for name, on in (
			("trust_store_path", opts.trust_store_path is not None),
			("author_pubkey_b64", opts.author_pubkey_b64 is not None),
			("allow_bundled_pubkey", opts.allow_bundled_pubkey),
		) if on
	]
	if len(_selected) > 1:
		raise VerifyPackageUsageError(
			f"exactly one trust source may be given, but multiple were: "
			f"{_selected}.  trust_store_path / author_pubkey_b64 / "
			f"allow_bundled_pubkey are mutually exclusive"
		)
	if opts.trust_store_path is not None:
		from lang.driftc.packages.trust_v1 import load_trust_store_json
		try:
			store = load_trust_store_json(opts.trust_store_path)
		except Exception as err:
			raise VerifyPackageUsageError(
				f"--trust-store {opts.trust_store_path} could not be "
				f"loaded: {err}"
			) from err
		report["trust_source"] = f"trust-store:{opts.trust_store_path}"
	elif opts.author_pubkey_b64 is not None:
		try:
			_decode_ed25519_pubkey(opts.author_pubkey_b64)
		except (ValueError, OSError) as err:
			raise VerifyPackageUsageError(
				f"author pubkey supplied on the command line is invalid: {err}"
			) from err
		# store built in step 3 (namespaces may default to the manifest's
		# module ids); trust_source set there too.
	elif not opts.allow_bundled_pubkey:
		raise VerifyPackageUsageError(
			"no trust source given; pass one of --trust-store / "
			"--author-pubkey-b64 / --author-profile (or "
			"--allow-bundled-pubkey for a self-consistency check against "
			"the package's own bundled author pubkey, which is NOT a trust "
			"decision)"
		)

	# 2. Decompress + manifest + identity.  The identity (artifact_sha256
	#    from the decompressed bytes; SCI/version/pkg_id from the manifest
	#    stamp, never recomputed from binary per G1) and the trust-module
	#    enumeration come from the SHARED harness, so this CLI cannot drift
	#    from how the consumer-load / index-time gates read a package.
	#    Corrupt artifact bytes or a malformed/unstamped manifest are
	#    properties of the PACKAGE, not the invocation: report them as
	#    ok=false (exit 1) rather than raising a usage error.
	try:
		pkg_bytes = decompress_zdmp(zdmp_path.read_bytes())
		pkg = load_dmir_pkg_v0_from_bytes(pkg_bytes, source_path=zdmp_path)
		manifest = pkg.manifest
		identity = build_package_identity(manifest, pkg_bytes)
	except Exception as err:
		report["errors"].append({
			"code": "malformed-artifact",
			"message": (
				f"{zdmp_path.name}: could not read/parse the deployed "
				f"artifact or its manifest: {err}"
			),
		})
		return report
	pkg_id = identity.package_id
	pkg_ver = identity.version
	sci = identity.source_content_id
	artifact_sha = identity.artifact_sha256
	report.update(package_id=pkg_id, version=pkg_ver,
		source_content_id=sci, artifact_sha256=artifact_sha)
	module_ids = iter_trust_module_ids(manifest)

	overall_ok = True

	# 3. BUILD the trust material that needs the manifest.  --trust-store
	#    was already loaded in step 1b, and --author-pubkey-b64 was already
	#    validated there; the only work left is the author-pubkey store
	#    build (its default namespaces are the manifest's module ids) and
	#    the bundled-pubkey path (package content -> ok=false on absence).
	if opts.trust_store_path is not None:
		pass  # store + trust_source already resolved in step 1b
	elif opts.author_pubkey_b64 is not None:
		ns = opts.author_namespaces if opts.author_namespaces is not None else module_ids
		# The pubkey was validated in step 1b, so this cannot raise for a
		# malformed key; build the single-key store over the namespaces.
		store, _kid = _synth_single_key_trust_store(
			pubkey_b64=opts.author_pubkey_b64, namespaces=ns,
		)
		report["trust_source"] = (
			"author-profile" if opts.author_namespaces is not None
			else "author-pubkey-b64"
		)
	elif opts.allow_bundled_pubkey:
		# Opt-in self-consistency: verify against the package's OWN bundled
		# `<pkg>.author-pubkey.b64`.  Proves the artifact + claims are
		# intact and self-signed, NOT that any third party trusts the
		# signer.  Gated behind an explicit flag so a CI gate cannot
		# silently treat "no trust material supplied" as "trusted".  The
		# bundled key is part of the package, so its absence / malformation
		# is a verification outcome (ok=false), not a usage error.
		pubs = sorted(d.glob("*.author-pubkey.b64"))
		if not pubs:
			report["errors"].append({
				"code": "bundled-pubkey-missing",
				"message": (
					"--allow-bundled-pubkey given but no bundled "
					"<pkg>.author-pubkey.b64 is present in the package "
					"directory"
				),
			})
			return report
		if len(pubs) > 1:
			report["errors"].append({
				"code": "bundled-pubkey-ambiguous",
				"message": (
					f"multiple *.author-pubkey.b64 in {d}: "
					f"{[p.name for p in pubs]}; cannot pick a bundled key"
				),
			})
			return report
		try:
			store, _kid = _synth_single_key_trust_store(
				pubkey_b64=pubs[0].read_text(encoding="utf-8"), namespaces=module_ids,
			)
		except (ValueError, OSError) as err:
			report["errors"].append({
				"code": "bundled-pubkey-malformed",
				"message": f"{pubs[0].name}: {err}",
			})
			return report
		report["trust_source"] = f"bundled-pubkey:{pubs[0].name}"
		report["warnings"].append(
			"verified against the package's OWN bundled author pubkey "
			"(self-consistency: proves artifact integrity + self-signature, "
			"NOT third-party trust). Pass --trust-store for a trust decision."
		)
	else:  # pragma: no cover - presence guaranteed by step 1b
		raise AssertionError(
			"unreachable: trust-source presence validated in step 1b"
		)

	# 4. Per-module author + cert verification (checks 1, 2, 3, 5, 6),
	#    delegated to the SHARED harness so reserved-namespace routing
	#    (std.* / lang.* / drift.* -> core trust) and the per-module engine
	#    call are identical to the consumer-load and deploy index-time
	#    gates -- the facade adds NO verification of its own.  Reserved
	#    modules MUST verify against the toolchain-shipped core trust
	#    store, never the caller-supplied or synthesized one, so a
	#    non-Foundation key cannot bless a `std.*` package; core trust is
	#    loaded only when a reserved module is actually present.
	# Collect the EXACT cert claims the verifier accepted (it reports
	# them on `VerifyResult.accepted_cert_claim`) rather than re-locating
	# claims by signer kid: a kid may sign more than one cert claim in the
	# sidecar dir, and only the verifier knows which one passed every gate.
	# A multi-module package can be accepted through several certifiers,
	# and each accepted claim independently pins the provenance bytes, so
	# we keep all distinct (kid, signed-evidence) bindings.
	accepted_certs: list[tuple[str, Any]] = []  # (kid, CertClaim), deduped, in order
	_seen_cert_keys: set[tuple[str, str]] = set()
	if not module_ids:
		overall_ok = False
		report["errors"].append({
			"code": "no-modules",
			"message": "manifest declares no modules to verify",
		})
	if any(module_is_reserved(mid) for mid in module_ids):
		from lang.driftc.packages.trust_v1 import load_core_trust_store
		core_trust = load_core_trust_store()
	else:
		# No reserved module in play; the harness never consults the core
		# store for non-reserved modules, so the user store is a harmless
		# placeholder (matches the resolver's core==user fallback).
		core_trust = store
	try:
		module_results = verify_package_modules(
			sidecar_dir=d, identity=identity, module_ids=module_ids,
			trust_store=store, core_trust_store=core_trust, resolved_closure=[],
		)
	except Exception as err:
		# A present-but-malformed author/cert sidecar makes the strict-v1
		# loader raise (fail-closed; a missing sidecar instead returns a
		# rejected result, not a raise).  A bad sidecar is a property of the
		# PACKAGE, so fold it into the report as ok=false rather than letting
		# it escape as an exception.  Sidecars are per-package, so one bad
		# sidecar fails the whole package -> a single report error is right.
		report["errors"].append({
			"code": "malformed-sidecar",
			"message": f"could not read/parse a package sidecar claim: {err}",
		})
		return report
	for mr in module_results:
		res = mr.result
		report["modules"].append({
			"module_id": mr.module_id, "ok": res.ok, "mode": res.mode,
			"reserved": mr.reserved,
			"author_kid": res.author_kid, "certifier_kid": res.certifier_kid,
			"reason": res.reason,
		})
		if res.ok:
			report["author_kid"] = res.author_kid
			report["certifier_kid"] = res.certifier_kid
			report["mode"] = res.mode
			cc = res.accepted_cert_claim
			if cc is not None and res.certifier_kid:
				# Dedup by (kid, signed evidence digest): two modules
				# accepted through the same cert claim must not double-
				# report or double-check it, while genuinely distinct
				# bindings (different kid or different evidence) are all
				# kept so provenance must satisfy every one of them.
				key = (res.certifier_kid, cc.body.evidence_sha256)
				if key not in _seen_cert_keys:
					_seen_cert_keys.add(key)
					accepted_certs.append((res.certifier_kid, cc))
		else:
			overall_ok = False
			report["errors"].append({
				"code": "verify-failed", "module_id": mr.module_id,
				"message": res.reason,
			})

	report["certifier_kids"] = [kid for kid, _cc in accepted_certs]

	# 4b. v2 cross-checks (PACKAGE).  The schema loaders already guarantee
	#     each field is present + canonical; here we enforce AGREEMENT across
	#     the author claim, every accepted cert claim, and the deployed
	#     artifact.  This facade verifies PACKAGE artifacts (it located a
	#     `.zdmp`), so the attested kind MUST be "package"; an app artifact
	#     goes through the separate `verify-app` adapter.
	_EXPECTED_KIND = "package"
	# Author claim's artifact_kind (the per-release singleton sidecar).
	# compose_verify already verified its signature + SCI; we re-read it only
	# for the kind.  Only EXPECTED load/parse failures are folded into the
	# report (a programmer error — e.g. a bad call — must surface, not be
	# silently swallowed into a skipped check).
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.verify_v1 import discover_author_claim_path
	author_kind: Optional[str] = None
	if accepted_certs:
		_acp = discover_author_claim_path(d, package_id=pkg_id)
		if _acp is None:
			# An accepted cert implies a verified author claim existed in step 4;
			# its absence here is a package inconsistency, not a skip.
			overall_ok = False
			report["errors"].append({
				"code": "author-claim-missing",
				"message": "no author-claim sidecar found for the verified package",
			})
		else:
			try:
				author_kind = load_author_claim_json(
					_acp.read_text(encoding="utf-8")).body.artifact_kind
			except (ValueError, OSError) as err:
				overall_ok = False
				report["errors"].append({
					"code": "malformed-sidecar",
					"message": f"author claim {_acp.name}: {err}",
				})
		if author_kind is not None and author_kind != _EXPECTED_KIND:
			overall_ok = False
			report["errors"].append({
				"code": "artifact-kind-mismatch",
				"message": (
					f"author claim artifact_kind {author_kind!r} != {_EXPECTED_KIND!r} "
					f"(this is a deployed package; apps use `verify-app`)"
				),
			})
		for kid, cc in accepted_certs:
			b = cc.body
			if b.artifact_kind != _EXPECTED_KIND:
				overall_ok = False
				report["errors"].append({
					"code": "artifact-kind-mismatch", "certifier_kid": kid,
					"message": (
						f"cert claim artifact_kind {b.artifact_kind!r} != {_EXPECTED_KIND!r} "
						f"(certifier {kid})"
					),
				})
			elif author_kind is not None and b.artifact_kind != author_kind:
				overall_ok = False
				report["errors"].append({
					"code": "artifact-kind-disagreement", "certifier_kid": kid,
					"message": (
						f"author artifact_kind {author_kind!r} != cert artifact_kind "
						f"{b.artifact_kind!r} (certifier {kid})"
					),
				})
			# Signed locator must name the on-disk artifact exactly.
			if b.artifact_path != zdmp_path.name:
				overall_ok = False
				report["errors"].append({
					"code": "artifact-path-mismatch", "certifier_kid": kid,
					"message": (
						f"cert artifact_path {b.artifact_path!r} != deployed artifact "
						f"filename {zdmp_path.name!r} (certifier {kid})"
					),
				})
			# Artifact hash + SCI agreement (compose_verify already binds these
			# to the manifest identity; re-assert so a future loosening there is
			# caught here too).
			if b.artifact_sha256 != artifact_sha:
				overall_ok = False
				report["errors"].append({
					"code": "artifact-sha-mismatch", "certifier_kid": kid,
					"message": (
						f"cert artifact_sha256 {b.artifact_sha256} != deployed "
						f"artifact {artifact_sha} (certifier {kid})"
					),
				})
			if b.source_content_id != sci:
				overall_ok = False
				report["errors"].append({
					"code": "source-content-id-mismatch", "certifier_kid": kid,
					"message": (
						f"cert source_content_id {b.source_content_id} != package "
						f"identity SCI {sci} (certifier {kid})"
					),
				})

	# 5. Provenance binding (check 4).  The authoritative pin is the SIGNED
	#    `cert.body.evidence_sha256`, set by deploy to sha256(<on-disk
	#    .provenance.zst bytes>) (drift_deploy.py:1539).  EVERY accepted
	#    cert claim independently pins those bytes, so the on-disk bundle
	#    must satisfy ALL of them (a multi-module package may be accepted
	#    through several certifiers).  Checking only the bundle's unsigned
	#    inner `artifact_sha256` field would let a hostile mirror swap the
	#    bundle for attacker-chosen contents keeping that field, so we bind
	#    the bundle BYTES to each signed digest.
	prov_candidates = [p for p in sorted(d.glob("*.zst")) if "provenance" in p.name]
	if not prov_candidates:
		prov_candidates = sorted(d.glob("provenance.zst"))
	prov_path = prov_candidates[0] if prov_candidates else None

	if accepted_certs:
		if prov_path is None:
			overall_ok = False
			report["provenance_ok"] = False
			report["errors"].append({
				"code": "provenance-missing",
				"message": (
					"accepted cert claim(s) bind evidence_sha256 to a "
					"provenance bundle, but no provenance.zst is present in "
					"the package directory"
				),
			})
		else:
			actual_digest = "sha256:" + hashlib.sha256(prov_path.read_bytes()).hexdigest()
			mismatched = [
				(kid, cc.body.evidence_sha256)
				for kid, cc in accepted_certs
				if cc.body.evidence_sha256 != actual_digest
			]
			if mismatched:
				overall_ok = False
				report["provenance_ok"] = False
				for kid, signed in mismatched:
					report["errors"].append({
						"code": "provenance-evidence-mismatch",
						"certifier_kid": kid,
						"message": (
							f"sha256({prov_path.name}) {actual_digest} != cert "
							f"body.evidence_sha256 {signed} (certifier {kid}); "
							"the on-disk provenance bundle is not the one this "
							"certifier signed"
						),
					})
			else:
				report["provenance_ok"] = True
				# v4 three-leg cross-check: the provenance bundle's signed-bound
				# inner fields MUST agree with the package identity + claims.
				# No two-way fallback — a missing/malformed provenance SCI is a
				# HARD failure (provenance is the third SCI leg).
				try:
					prov = _load_provenance_fields(prov_path)
				except Exception as err:
					overall_ok = False
					report["provenance_ok"] = False
					report["errors"].append({
						"code": "provenance-unreadable",
						"message": f"{prov_path.name}: {err}",
					})
				else:
					if not _cross_check_provenance(
						prov, report,
						expected_kind=_EXPECTED_KIND,
						artifact_sha=artifact_sha, sci=sci,
						pkg_id=pkg_id, pkg_ver=pkg_ver,
					):
						overall_ok = False
						report["provenance_ok"] = False
	elif prov_path is not None:
		# No cert-accepted module to bind against (self-verify / rejected),
		# but a bundle is present: only the inner artifact field is
		# checkable. Flag that the strong binding was not exercised.
		report["warnings"].append(
			"no accepted cert claim to bind the provenance bundle against; "
			"only the bundle's unsigned inner artifact_sha256 was checked"
		)
		try:
			inner = _provenance_artifact_sha256(prov_path)
		except Exception as err:
			overall_ok = False
			report["provenance_ok"] = False
			report["errors"].append({
				"code": "provenance-unreadable",
				"message": f"{prov_path.name}: {err}",
			})
		else:
			report["provenance_ok"] = (inner == artifact_sha)
			if inner != artifact_sha:
				overall_ok = False
				report["errors"].append({
					"code": "provenance-artifact-mismatch",
					"message": (
						f"provenance inner artifact_sha256 {inner} != on-disk "
						f"artifact {artifact_sha}"
					),
				})
	else:
		report["provenance_ok"] = None
		report["warnings"].append(
			"no provenance bundle (*.zst) found; provenance check skipped"
		)

	# 6. --expect-* assertions.
	if opts.expect_version is not None and pkg_ver != opts.expect_version:
		overall_ok = False
		report["errors"].append({
			"code": "expect-version",
			"message": f"version {pkg_ver!r} != --expect-version {opts.expect_version!r}",
		})
	if opts.expect_sci is not None and sci != opts.expect_sci:
		overall_ok = False
		report["errors"].append({
			"code": "expect-sci",
			"message": f"source_content_id {sci!r} != --expect-sci {opts.expect_sci!r}",
		})

	# Dev/no-evidence sentinel surfacing across ALL accepted cert claims.
	sentinel_kids = [
		kid for kid, cc in accepted_certs
		if cc.body.cert_suite.result_evidence_sha256 == _EMPTY_EVIDENCE_SHA
	]
	if sentinel_kids:
		report["no_evidence_sentinel"] = True
		report["warnings"].append(
			"accepted cert claim(s) are dev/no-evidence sentinels "
			f"(cert_suite.result_evidence_sha256 == sha256('')): {sentinel_kids}; "
			"signatures + integrity verified, but the cert suite attests NO "
			"evidence"
		)

	report["ok"] = overall_ok
	return report


def verify_deployed_app(opts: VerifyPackageOptions) -> dict[str, Any]:
	"""Verify a deployed APP artifact directory (verify only — NO exec).

	Same trust model as `verify_deployed_package`, but the artifact is the
	runnable BINARY (located by the cert claim's SIGNED `artifact_path`), there
	is no container / manifest / modules, and the trust subject is a single
	synthetic id derived from the author claim's declared namespace (D-3/D-5).

	Enforces (app three-leg agreement):
	  - author/cert/provenance artifact_kind all == "app";
	  - cert artifact_path names the on-disk binary exactly;
	  - sha256(binary) == cert.artifact_sha256 == provenance.artifact_sha256;
	  - author.sci == cert.sci == provenance.sci (no two-way fallback);
	  - provenance artifact_name/version == the verified app identity;
	  - author + cert signatures verify against trusted kids for the namespace.

	Raises `VerifyPackageUsageError` only for invocation problems (not a
	directory, a package dir passed by mistake, no/conflicting trust source).
	Package-content problems are folded into the report as `ok=false`.
	"""
	from hashlib import sha256 as _sha256
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.cert_claim_v1 import load_cert_claim_json
	from lang.driftc.packages.verify_v1 import (
		PackageIdentity, compose_verify, discover_author_claim_path,
		discover_cert_claim_paths,
	)
	from lang.driftc.packages.verify_harness_v1 import module_is_reserved

	report = new_report(opts.package_dir)
	report["artifact_kind"] = "app"
	d = opts.package_dir
	_EXPECTED_KIND = "app"

	# 1. Layout / invocation.
	if d.is_file():
		raise VerifyPackageUsageError(f"{d} is a file; pass the app DIRECTORY")
	if not d.is_dir():
		raise VerifyPackageUsageError(f"not a directory: {d}")
	if sorted(d.glob("*.zdmp")) or sorted(d.glob("*.dmp")):
		raise VerifyPackageUsageError(
			f"{d} contains a .zdmp/.dmp container; this is a package directory — "
			f"use verify-package, not verify-app"
		)

	# 1b. Trust SOURCE is an INVOCATION property — validate it BEFORE reading
	#     the artifact/sidecars, so a malformed/missing author claim cannot
	#     mask a bad invocation (no trust source, unreadable --trust-store,
	#     malformed --author-pubkey-b64) as a verification failure.  Only the
	#     namespace-defaulted store CONSTRUCTION (which needs the author
	#     claim's namespaces) is deferred to step 3.
	_selected = [
		name for name, on in (
			("trust_store_path", opts.trust_store_path is not None),
			("author_pubkey_b64", opts.author_pubkey_b64 is not None),
			("allow_bundled_pubkey", opts.allow_bundled_pubkey),
		) if on
	]
	if len(_selected) > 1:
		raise VerifyPackageUsageError(
			f"exactly one trust source may be given, but multiple were: {_selected}"
		)
	store = None
	if opts.trust_store_path is not None:
		from lang.driftc.packages.trust_v1 import load_trust_store_json
		try:
			store = load_trust_store_json(opts.trust_store_path)
		except Exception as err:
			raise VerifyPackageUsageError(
				f"--trust-store {opts.trust_store_path} could not be loaded: {err}"
			) from err
		report["trust_source"] = f"trust-store:{opts.trust_store_path}"
	elif opts.author_pubkey_b64 is not None:
		try:
			_decode_ed25519_pubkey(opts.author_pubkey_b64)
		except (ValueError, OSError) as err:
			raise VerifyPackageUsageError(
				f"author pubkey supplied on the command line is invalid: {err}"
			) from err
		# store built in step 3 (namespaces default to the author claim's).
	elif not opts.allow_bundled_pubkey:
		raise VerifyPackageUsageError(
			"no trust source given; pass --trust-store / --author-pubkey-b64 / "
			"--author-profile (or --allow-bundled-pubkey for a self-consistency check)"
		)

	# 2. Author claim (the per-release singleton) — carries the app id /
	#    version / SCI / namespace and the artifact_kind.
	acs = sorted(d.glob("*.author-claim")) + sorted(d.glob("*.author-claim.json"))
	if not acs:
		report["errors"].append({
			"code": "author-claim-missing",
			"message": f"no *.author-claim sidecar in {d}",
		})
		return report
	if len(acs) > 1:
		raise VerifyPackageUsageError(
			f"multiple *.author-claim sidecars in {d}: {[p.name for p in acs]}"
		)
	try:
		author_claim = load_author_claim_json(acs[0].read_text(encoding="utf-8"))
	except (ValueError, OSError) as err:
		report["errors"].append({"code": "malformed-sidecar", "message": f"author claim: {err}"})
		return report
	abody = author_claim.body
	app_id = abody.package_id
	version = abody.version
	sci = abody.source_content_id
	report.update(package_id=app_id, version=version, source_content_id=sci)
	overall_ok = True
	if abody.artifact_kind != _EXPECTED_KIND:
		overall_ok = False
		report["errors"].append({
			"code": "artifact-kind-mismatch",
			"message": (
				f"author claim artifact_kind {abody.artifact_kind!r} != {_EXPECTED_KIND!r} "
				f"(verify-app expects an app; packages use verify-package)"
			),
		})
	if not abody.namespaces:
		report["errors"].append({
			"code": "author-namespace-missing",
			"message": "author claim declares no namespace for the app trust subject",
		})
		return report
	# Synthetic trust subject: the declared namespace prefix (covered by the
	# claim's namespace AND grantable by the trust store under the same rule).
	_ns0 = abody.namespaces[0]
	subject = _ns0[:-2] if _ns0.endswith(".*") else _ns0

	# 3. BUILD the namespace-defaulted store (author-pubkey / bundled) — the
	#    invocation was already validated in step 1b; here we only need the
	#    author claim's namespaces.
	if opts.trust_store_path is not None:
		pass  # already loaded in 1b
	elif opts.author_pubkey_b64 is not None:
		ns = opts.author_namespaces if opts.author_namespaces is not None else list(abody.namespaces)
		store, _kid = _synth_single_key_trust_store(pubkey_b64=opts.author_pubkey_b64, namespaces=ns)
		report["trust_source"] = (
			"author-profile" if opts.author_namespaces is not None else "author-pubkey-b64"
		)
	elif opts.allow_bundled_pubkey:
		pubs = sorted(d.glob("*.author-pubkey.b64"))
		if len(pubs) != 1:
			report["errors"].append({
				"code": "bundled-pubkey-missing" if not pubs else "bundled-pubkey-ambiguous",
				"message": f"expected exactly one *.author-pubkey.b64 in {d}, found {len(pubs)}",
			})
			return report
		try:
			store, _kid = _synth_single_key_trust_store(
				pubkey_b64=pubs[0].read_text(encoding="utf-8"), namespaces=list(abody.namespaces),
			)
		except (ValueError, OSError) as err:
			report["errors"].append({"code": "bundled-pubkey-malformed", "message": f"{pubs[0].name}: {err}"})
			return report
		report["trust_source"] = f"bundled-pubkey:{pubs[0].name}"
		report["warnings"].append(
			"verified against the app's OWN bundled author pubkey (self-consistency, "
			"NOT third-party trust)"
		)

	# 4. Cert claims (per-certifier).  All must agree on the signed locator.
	cert_paths = discover_cert_claim_paths(d, package_id=app_id)
	try:
		cert_claims = [load_cert_claim_json(p.read_text(encoding="utf-8")) for p in cert_paths]
	except (ValueError, OSError) as err:
		report["errors"].append({"code": "malformed-sidecar", "message": f"cert claim: {err}"})
		return report
	if not cert_claims:
		report["errors"].append({
			"code": "cert-claim-missing",
			"message": f"no cert-claim sidecar for app {app_id!r} in {d}",
		})
		return report
	locator_set = {cc.body.artifact_path for cc in cert_claims}
	if len(locator_set) != 1:
		overall_ok = False
		report["errors"].append({
			"code": "artifact-path-disagreement",
			"message": f"cert claims name different artifact_path values: {sorted(locator_set)}",
		})
	bin_rel = cert_claims[0].body.artifact_path

	# 5. Locate + hash the binary by the SIGNED locator.  The verified locator
	#    MUST resolve to a REGULAR, non-symlink file inside the app dir — this
	#    report is meant to let orchestration run that exact artifact, so a
	#    symlink (which could point at bytes outside the dir / change after
	#    verify) is rejected outright.
	bin_path = d / bin_rel
	if bin_path.is_symlink():
		overall_ok = False
		report["errors"].append({
			"code": "artifact-symlink",
			"message": (
				f"cert artifact_path {bin_rel!r} is a symlink; the verified app "
				f"binary must be a regular file (no symlinks)"
			),
		})
		report["ok"] = False
		return report
	if not bin_path.is_file():
		overall_ok = False
		report["errors"].append({
			"code": "artifact-missing",
			"message": f"cert artifact_path {bin_rel!r} does not name a file in {d}",
		})
		report["ok"] = False
		return report
	binary_sha = "sha256:" + _sha256(bin_path.read_bytes()).hexdigest()
	report["artifact_sha256"] = binary_sha
	identity = PackageIdentity(
		package_id=app_id, version=version, source_content_id=sci, artifact_sha256=binary_sha,
	)

	# 6. Trust routing (reserved namespaces -> core trust, same as packages;
	#    apps should not be reserved, but route defensively).
	if module_is_reserved(subject):
		from lang.driftc.packages.trust_v1 import load_core_trust_store
		trust_for_verify = load_core_trust_store()
	else:
		trust_for_verify = store

	# 7. Crypto via compose_verify with the synthetic subject.
	res = compose_verify(
		author_claim=author_claim, cert_claims=cert_claims,
		package_identity=identity, module_id=subject,
		trust=trust_for_verify, resolved_closure=[],
	)
	report["modules"].append({
		"module_id": subject, "ok": res.ok, "mode": res.mode, "reserved": module_is_reserved(subject),
		"author_kid": res.author_kid, "certifier_kid": res.certifier_kid, "reason": res.reason,
	})
	accepted_certs: list[tuple[str, Any]] = []
	if res.ok:
		report["author_kid"] = res.author_kid
		report["certifier_kid"] = res.certifier_kid
		report["mode"] = res.mode
		if res.accepted_cert_claim is not None and res.certifier_kid:
			accepted_certs.append((res.certifier_kid, res.accepted_cert_claim))
	else:
		overall_ok = False
		report["errors"].append({"code": "verify-failed", "module_id": subject, "message": res.reason})
	report["certifier_kids"] = [kid for kid, _cc in accepted_certs]

	# 8. App cross-checks on every accepted cert (kind/path/sha/sci).
	for kid, cc in accepted_certs:
		b = cc.body
		if b.artifact_kind != _EXPECTED_KIND:
			overall_ok = False
			report["errors"].append({
				"code": "artifact-kind-mismatch", "certifier_kid": kid,
				"message": f"cert artifact_kind {b.artifact_kind!r} != {_EXPECTED_KIND!r} (certifier {kid})",
			})
		elif b.artifact_kind != abody.artifact_kind:
			overall_ok = False
			report["errors"].append({
				"code": "artifact-kind-disagreement", "certifier_kid": kid,
				"message": f"author kind {abody.artifact_kind!r} != cert kind {b.artifact_kind!r}",
			})
		if b.artifact_path != bin_path.name:
			overall_ok = False
			report["errors"].append({
				"code": "artifact-path-mismatch", "certifier_kid": kid,
				"message": f"cert artifact_path {b.artifact_path!r} != app binary filename {bin_path.name!r}",
			})
		if b.artifact_sha256 != binary_sha:
			overall_ok = False
			report["errors"].append({
				"code": "artifact-sha-mismatch", "certifier_kid": kid,
				"message": f"cert artifact_sha256 {b.artifact_sha256} != sha256(binary) {binary_sha}",
			})
		if b.source_content_id != sci:
			overall_ok = False
			report["errors"].append({
				"code": "source-content-id-mismatch", "certifier_kid": kid,
				"message": f"cert source_content_id {b.source_content_id} != author SCI {sci}",
			})

	# 9. Provenance binding + v4 three-leg cross-check (app).
	prov_candidates = [p for p in sorted(d.glob("*.zst")) if "provenance" in p.name]
	prov_path = prov_candidates[0] if prov_candidates else None
	if accepted_certs:
		if prov_path is None:
			overall_ok = False
			report["provenance_ok"] = False
			report["errors"].append({
				"code": "provenance-missing",
				"message": "accepted cert binds a provenance bundle, but none is present",
			})
		else:
			actual_digest = "sha256:" + hashlib.sha256(prov_path.read_bytes()).hexdigest()
			mismatched = [(kid, cc.body.evidence_sha256) for kid, cc in accepted_certs
				if cc.body.evidence_sha256 != actual_digest]
			if mismatched:
				overall_ok = False
				report["provenance_ok"] = False
				for kid, signed in mismatched:
					report["errors"].append({
						"code": "provenance-evidence-mismatch", "certifier_kid": kid,
						"message": (
							f"sha256({prov_path.name}) {actual_digest} != cert evidence_sha256 "
							f"{signed} (certifier {kid})"
						),
					})
			else:
				report["provenance_ok"] = True
				try:
					prov = _load_provenance_fields(prov_path)
				except Exception as err:
					overall_ok = False
					report["provenance_ok"] = False
					report["errors"].append({"code": "provenance-unreadable", "message": f"{prov_path.name}: {err}"})
				else:
					if not _cross_check_provenance(
						prov, report, expected_kind=_EXPECTED_KIND,
						artifact_sha=binary_sha, sci=sci, pkg_id=app_id, pkg_ver=version,
					):
						overall_ok = False
						report["provenance_ok"] = False
	elif prov_path is None:
		report["provenance_ok"] = None
		report["warnings"].append("no provenance bundle (*.zst) found; provenance check skipped")

	# 10. --expect-* assertions.
	if opts.expect_version is not None and version != opts.expect_version:
		overall_ok = False
		report["errors"].append({
			"code": "expect-version",
			"message": f"version {version!r} != --expect-version {opts.expect_version!r}",
		})
	if opts.expect_sci is not None and sci != opts.expect_sci:
		overall_ok = False
		report["errors"].append({
			"code": "expect-sci",
			"message": f"source_content_id {sci!r} != --expect-sci {opts.expect_sci!r}",
		})

	sentinel_kids = [kid for kid, cc in accepted_certs
		if cc.body.cert_suite.result_evidence_sha256 == _EMPTY_EVIDENCE_SHA]
	if sentinel_kids:
		report["no_evidence_sentinel"] = True
		report["warnings"].append(
			f"accepted cert claim(s) are dev/no-evidence sentinels: {sentinel_kids}"
		)

	report["ok"] = overall_ok
	return report
