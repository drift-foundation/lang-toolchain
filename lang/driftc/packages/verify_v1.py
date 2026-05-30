# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Trust-v1 package verifier composition.

Composes the three v1 building blocks (trust store, author claim,
cert claim) into a single accept/reject decision per package load:

    accept package P for module M iff
      author claim covers M for the expected (package_id, version)
      AND author claim is signed by a trusted author-role kid
      AND author claim's SCI matches the package manifest stamp
      AND (
            certifier-shortcut: some cert claim for THIS (pkg, ver)
              binds artifact_sha256 + source_content_id + dep graph
              and is signed by a trusted certifier-role kid
        OR  self-verify: the consumer rebuilt source whose SCI
              matches the author claim
      )

Two strict invariants from the plan, enforced here:

  G1.  Normal verification compares stamped SCIs across the
       three places they appear (author claim, package manifest,
       cert claim).  It NEVER recomputes SCI from the binary
       package bytes -- there is no source available.  Self-verify
       mode is the ONLY path that recomputes SCI from local source
       and compares to the author claim.  The result diagnostic
       must state which mode it ran in so the caller can't be
       misled into thinking source identity was independently
       proven.

  G3.  Artifact-hash binding flows EXCLUSIVELY through cert
       claims.  The author claim never binds artifact bytes.  An
       author who wants to distribute a binary directly signs a
       cert claim with their own key; the consumer trusts that
       kid in both 'authors' and 'certifiers' role lists.  There
       is no "author-direct" verifier path in the code; that case
       is just certifier-shortcut with one kid in two roles.

Slice 4 of the trust-v1 implementation; companion docstrings in
`trust_v1`, `author_claim_v1`, `cert_claim_v1`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lang.driftc.packages.author_claim_v1 import (
	AuthorClaim,
	load_author_claim_json,
	verify_author_claim_for_module,
)
from lang.driftc.packages.cert_claim_v1 import (
	CertClaim,
	ResolvedDep,
	load_cert_claim_json,
	verify_cert_claim_for_module,
)
from lang.driftc.packages.sidecar_naming import (
	author_claim_filename,
	cert_claim_filename_prefix,
)
from lang.driftc.packages.trust_v1 import TrustStore


# ── Public types ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PackageIdentity:
	"""The package's self-stated identity from its on-disk stamps.

	`source_content_id` is read directly from the package manifest
	(NOT recomputed from binary bytes; that would be a phantom
	proof per guardrail G1).  `artifact_sha256` is the sha256 of
	the decompressed `.dmp` payload, also computed once at load
	time by the caller.
	"""
	package_id: str
	version: str
	source_content_id: str   # "sha256:<hex>" — stamped in pkg manifest
	artifact_sha256: str     # "sha256:<hex>" — sha256(decompressed .dmp)


@dataclass(frozen=True)
class VerifyResult:
	"""Structured outcome of `compose_verify`.

	`mode`:
	  - "certifier-shortcut" — accepted via author + trusted cert claim.
	  - "self-verify"        — accepted via author + consumer rebuild.
	  - "rejected"           — `ok` is False; see `reason`.

	`author_kid` is set iff the author claim was successfully
	verified (regardless of overall outcome); `certifier_kid` is
	set iff a cert claim was accepted.  Useful for diagnostics
	and for logging which exact trust paths were used.

	`accepted_cert_claim` is the EXACT cert claim the verifier
	accepted on the certifier-shortcut path (None on self-verify or
	rejection).  Callers that need to act on the accepted claim — e.g.
	binding a provenance bundle to its signed `body.evidence_sha256`,
	or surfacing the dev/no-evidence sentinel — MUST read it from here
	rather than re-locating a claim by signer kid: a kid may sign more
	than one cert claim in the sidecar dir, and only the verifier knows
	which one actually passed every gate.

	The mode string MUST be present in the diagnostic so callers
	(and audit logs) cannot misread a stamped-SCI comparison as
	an independent source-identity proof.  See G1.
	"""
	ok: bool
	mode: str
	author_kid: Optional[str]
	certifier_kid: Optional[str]
	reason: str   # empty on ok
	accepted_cert_claim: Optional[CertClaim] = None


# ── Sidecar discovery ──────────────────────────────────────────────
#
# Discovery uses the SAME escape helpers as the emit side
# (`sidecar_naming.author_claim_filename`,
# `sidecar_naming.cert_claim_filename_prefix`) so a package id
# containing `/`, `:`, spaces, or any other character outside
# `[A-Za-z0-9._-]` is found by the discoverer the same way the
# emitter wrote it.  Before the v1 alignment, discovery searched
# with the raw package id and silently missed any file whose name
# had been percent-encoded by the emitter.


def discover_author_claim_path(
	sidecar_dir: Path, *, package_id: str,
) -> Optional[Path]:
	"""Find the canonical author-claim sidecar in `sidecar_dir`.

	Per O8, the author claim is a per-release singleton.  Filename
	is `<safe_pkg>.author-claim` (with `.json` accepted as an
	alternative suffix).  Returns the path or `None` if absent.
	"""
	canonical = author_claim_filename(package_id)
	candidates = [
		sidecar_dir / canonical,
		sidecar_dir / f"{canonical}.json",
	]
	for c in candidates:
		if c.is_file():
			return c
	return None


def discover_cert_claim_paths(
	sidecar_dir: Path, *, package_id: str,
) -> list[Path]:
	"""Find every cert-claim sidecar for `package_id` in `sidecar_dir`.

	Per O1, cert claims are per-certifier; multiple may coexist for
	the same package release.  The prefix is computed via the
	shared `cert_claim_filename_prefix` helper so it matches the
	emit-side escape rules exactly.  Returns the sorted list of
	matching paths (deterministic discovery order; the verifier
	tries them one by one).
	"""
	prefix = cert_claim_filename_prefix(package_id)
	out: list[Path] = []
	if not sidecar_dir.is_dir():
		return out
	for entry in sorted(sidecar_dir.iterdir()):
		if not entry.is_file():
			continue
		name = entry.name
		if name.startswith(prefix) and name.endswith(".json"):
			out.append(entry)
	return out


def load_sidecar_claims(
	sidecar_dir: Path, *, package_id: str,
) -> tuple[Optional[AuthorClaim], list[CertClaim]]:
	"""Read and parse all sidecar claims for a package in a directory.

	Returns `(author_claim_or_None, [cert_claims...])`.  Strict-v1
	parsing applies; any malformed claim raises `ValueError` (the
	loader is fail-closed by design — a corrupt sidecar is not a
	silent miss).
	"""
	ac_path = discover_author_claim_path(sidecar_dir, package_id=package_id)
	author_claim: Optional[AuthorClaim] = None
	if ac_path is not None:
		author_claim = load_author_claim_json(ac_path.read_text(encoding="utf-8"))
	cert_claims: list[CertClaim] = []
	for cc_path in discover_cert_claim_paths(sidecar_dir, package_id=package_id):
		cert_claims.append(load_cert_claim_json(cc_path.read_text(encoding="utf-8")))
	return author_claim, cert_claims


# ── Composition verifier ──────────────────────────────────────────


def compose_verify(
	*,
	author_claim: Optional[AuthorClaim],
	cert_claims: list[CertClaim],
	package_identity: PackageIdentity,
	module_id: str,
	trust: TrustStore,
	resolved_closure: list[ResolvedDep],
	require_certifier: Optional[str] = None,
	require_cert_suite: Optional[str] = None,
	self_verify: bool = False,
	self_verify_sci: Optional[str] = None,
) -> VerifyResult:
	"""Compose author + cert verification for one module load.

	Args:
	  author_claim: parsed author-claim sidecar (or None if absent).
	  cert_claims: list of parsed cert-claim sidecars (may be empty).
	  package_identity: stamped (pkg_id, version, sci, artifact_sha)
	    from the package on disk -- callers extract these from the
	    `.dmp` manifest and bytes WITHOUT recomputing SCI from binary.
	  module_id: the module the package claims to provide.
	  trust: the consumer's trust store (role-tagged).
	  resolved_closure: the consumer's actual resolved dep closure
	    for this package -- used to check cert claim dep_graph
	    coverage (O3).
	  require_certifier: if set, the cert claim must be signed by
	    this exact kid (O7 --require-certifier).
	  require_cert_suite: if set, cert_suite.id must equal this
	    string (O4 --require-cert-suite).
	  self_verify: True if the caller has rebuilt source for this
	    package and is asking the verifier to take the self-verify
	    acceptance path.
	  self_verify_sci: required when self_verify=True; the SCI the
	    consumer computed from local source.  Compared to
	    author_claim.body.source_content_id.  This is the ONLY
	    place SCI is recomputed (per G1).

	Returns a `VerifyResult` carrying the decision, the
	acceptance mode (so callers can audit which path was taken
	per G1), the accepted kids, and a reason string on failure.

	Rejections name the specific gate that failed and the
	offending values.  Diagnostics distinguish:
	  - missing author claim
	  - author claim package/version pinning
	  - author claim SCI mismatch with package manifest stamp
	  - author claim namespace coverage
	  - author claim signature / kid trust
	  - missing cert claim (and not in self-verify mode)
	  - cert claim package/version pinning
	  - cert claim artifact_sha256 mismatch
	  - cert claim SCI mismatch (with author claim's SCI)
	  - cert claim dep_graph closure mismatch
	  - cert claim cert_suite.result not "pass"
	  - --require-certifier / --require-cert-suite mismatches
	  - cert claim signature / kid trust / wrong-role kid
	  - self-verify SCI mismatch with author claim
	"""
	# ── Gate: policy-flag/mode coherence ──────────────────────────
	#
	# `--require-certifier` and `--require-cert-suite` (per O4 / O7)
	# exist to prove a specific certifier path was used.  They are
	# structurally incompatible with `self_verify`: a self-verify
	# accept never consults a cert claim, so pretending the
	# certifier/suite was used would be dishonest.  Reject the
	# contradictory combination at the API boundary rather than
	# silently letting self-verify accept and ignoring the flags.
	#
	# The right CI invocation is either:
	#   drift verify --self-verify              (self-verify only)
	#   drift verify --require-certifier <kid> --require-cert-suite <id>
	#                                           (certifier-shortcut only)
	# never both.
	if self_verify and (require_certifier is not None or require_cert_suite is not None):
		return VerifyResult(
			ok=False, mode="rejected", author_kid=None, certifier_kid=None,
			reason=(
				"self_verify=True is incompatible with --require-certifier / "
				"--require-cert-suite: those flags pin the certifier-shortcut "
				"path and cannot be honored under self-verify.  Pass either "
				"self_verify=True (no cert claim consulted) OR the require_* "
				"flags (cert claim required), not both."
			),
		)

	# ── Gate: author claim is always required ─────────────────────
	if author_claim is None:
		return VerifyResult(
			ok=False, mode="rejected", author_kid=None, certifier_kid=None,
			reason=(
				f"no .author-claim sidecar found for package "
				f"{package_identity.package_id!r}; an author claim is "
				f"REQUIRED for every package load"
			),
		)

	# ── Gate: author claim's SCI matches package manifest stamp ──
	#
	# G1: compare stamps; do NOT recompute SCI from binary.  The
	# caller passed `package_identity.source_content_id` extracted
	# from the package manifest at load time.  If author_claim.body
	# disagrees, the package on disk is not the one the author
	# released (either tampered or wrong sidecar paired with
	# package).
	if author_claim.body.source_content_id != package_identity.source_content_id:
		return VerifyResult(
			ok=False, mode="rejected", author_kid=None, certifier_kid=None,
			reason=(
				f"author claim body.source_content_id ({author_claim.body.source_content_id!r}) "
				f"does not match package manifest stamp ({package_identity.source_content_id!r}); "
				f"the on-disk package is not the source release this author claim describes "
				f"(stamps compared; SCI not recomputed from binary -- normal mode)"
			),
		)

	# ── Author-claim verification: package/version pin + namespace + signature ──
	author_result = verify_author_claim_for_module(
		author_claim, trust, module_id,
		expected_package_id=package_identity.package_id,
		expected_version=package_identity.version,
	)
	if not author_result.ok:
		return VerifyResult(
			ok=False, mode="rejected", author_kid=None, certifier_kid=None,
			reason=f"author claim rejected: {author_result.reason}",
		)
	author_kid = author_result.accepted_kid

	# ── Acceptance path ──────────────────────────────────────────

	if self_verify:
		# Self-verify is the ONLY path that recomputes SCI from
		# local source.  The caller asserts they rebuilt and
		# computed self_verify_sci themselves; we compare it to
		# the author claim's SCI (which we already verified
		# matches the package manifest stamp).
		if self_verify_sci is None:
			return VerifyResult(
				ok=False, mode="rejected",
				author_kid=author_kid, certifier_kid=None,
				reason=(
					"self_verify=True but self_verify_sci is None; the consumer "
					"must compute SCI from rebuilt source and pass it to the "
					"verifier"
				),
			)
		if self_verify_sci != author_claim.body.source_content_id:
			return VerifyResult(
				ok=False, mode="rejected",
				author_kid=author_kid, certifier_kid=None,
				reason=(
					f"self-verify SCI mismatch: rebuilt source produced "
					f"{self_verify_sci!r} but the author claim attests "
					f"{author_claim.body.source_content_id!r}.  The consumer's "
					f"local source is NOT the source the author released "
					f"(mode: self-verify -- SCI recomputed from local source)"
				),
			)
		return VerifyResult(
			ok=True, mode="self-verify",
			author_kid=author_kid, certifier_kid=None,
			reason="",
		)

	# Certifier-shortcut path.
	if not cert_claims:
		return VerifyResult(
			ok=False, mode="rejected",
			author_kid=author_kid, certifier_kid=None,
			reason=(
				f"no .cert-claim sidecar found for package "
				f"{package_identity.package_id!r}, and self-verify mode "
				f"not requested.  A trusted cert claim or consumer "
				f"self-verify is required for artifact acceptance"
			),
		)

	# Try each cert claim; first one that passes all gates wins.
	# Collect per-claim failure reasons so the aggregate diagnostic
	# tells the caller which signers and gates were attempted.
	failure_reasons: list[str] = []
	for idx, cc in enumerate(cert_claims):
		cert_result = verify_cert_claim_for_module(
			cc, trust, module_id,
			expected_package_id=package_identity.package_id,
			expected_version=package_identity.version,
			artifact_sha256=package_identity.artifact_sha256,
			expected_source_content_id=author_claim.body.source_content_id,
			resolved_closure=resolved_closure,
			require_certifier=require_certifier,
			require_cert_suite=require_cert_suite,
		)
		if cert_result.ok:
			return VerifyResult(
				ok=True, mode="certifier-shortcut",
				author_kid=author_kid,
				certifier_kid=cert_result.accepted_kid,
				reason="",
				accepted_cert_claim=cc,
			)
		# Tag the failure with which certifier was being tried for
		# clearer diagnostics when multiple cert claims exist.
		signing_kids = sorted({s.kid for s in cc.signatures})
		failure_reasons.append(
			f"cert claim #{idx} (signers {signing_kids!r}): {cert_result.reason}"
		)

	# No cert claim passed.
	return VerifyResult(
		ok=False, mode="rejected",
		author_kid=author_kid, certifier_kid=None,
		reason=(
			f"no cert claim accepted for {package_identity.package_id!r}@"
			f"{package_identity.version!r} on module {module_id!r} "
			f"(mode: certifier-shortcut).  Attempted: " + "; ".join(failure_reasons)
		),
	)


# ── Higher-level convenience: verify a directory of sidecars ────────


def verify_package_from_sidecars(
	*,
	sidecar_dir: Path,
	package_identity: PackageIdentity,
	module_id: str,
	trust: TrustStore,
	resolved_closure: list[ResolvedDep],
	require_certifier: Optional[str] = None,
	require_cert_suite: Optional[str] = None,
	self_verify: bool = False,
	self_verify_sci: Optional[str] = None,
) -> VerifyResult:
	"""Discover + load + compose-verify in one call.

	Convenience wrapper around `load_sidecar_claims` +
	`compose_verify`.  Used by the higher-level package loader
	once it has computed `package_identity` from the package on
	disk.
	"""
	author_claim, cert_claims = load_sidecar_claims(
		sidecar_dir, package_id=package_identity.package_id,
	)
	return compose_verify(
		author_claim=author_claim,
		cert_claims=cert_claims,
		package_identity=package_identity,
		module_id=module_id,
		trust=trust,
		resolved_closure=resolved_closure,
		require_certifier=require_certifier,
		require_cert_suite=require_cert_suite,
		self_verify=self_verify,
		self_verify_sci=self_verify_sci,
	)
