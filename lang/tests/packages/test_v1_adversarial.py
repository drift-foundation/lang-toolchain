# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Adversarial v1 trust suite — security source of truth.

Each test names a SPECIFIC attack the trust-v1 model claims to
prevent and asserts the verifier rejects it.  This file replaces
the v0-mechanic test coverage that pinned `.sig` envelope shape
and `trust.json` v0 fields; the contracts moved to v1's role-
tagged claims, so the regressions move with them.

The 10 attacks K specified, with the v1 gate that catches each:

  1. **Repo substitution** — attacker replaces `.dmp` bytes while
     keeping a valid author claim.  Cert claim's
     `artifact_sha256` no longer matches the on-disk bytes.
     Gate: `compose_verify` artifact-hash check.

  2. **Cert claim replay** — valid cert claim from package@A
     dropped next to package@B.  Cert body's package_id/version/
     SCI no longer match the package_identity.
     Gate: `compose_verify` cert-pinning gates.

  3. **Author claim replay** — old author claim retained when
     source changes.  Body's SCI no longer matches manifest stamp.
     Gate: `compose_verify` author SCI gate (G1).

  4. **Transitive dep swap** — direct deps unchanged but a
     transitive dep's bytes/SCI swapped.  Cert claim's dep_graph
     entry for that dep mismatches the consumer's closure.
     Gate: `check_dep_graph_covers` (O3).

  5. **Weak-suite substitution** — certifier signs with a smoke
     suite, consumer requires a release-gate suite.
     Gate: `--require-cert-suite` (O4).

  6. **Wrong-role key** — certifier kid signs the author claim
     (or vice versa) and consumer trust lists the kid in only
     one role.
     Gate: role-tagged trust store lookup.

  7. **Namespace shadowing** — more-specific namespace grant
     tries to redirect trust away from the actual signer.
     Gate: longest-prefix-wins matching in `TrustStore.allowed_*`.

  8. **Unsigned metadata injection** — attacker adds
     `artifact_sha256` or other fields to the author claim body
     (which by O6 must NOT bind artifact bytes) hoping they get
     honored.
     Gate: strict v1 unknown-key rejection at load.

  9. **Multi-signer confusion** — claim body carries one valid
     trusted signature plus one bogus / wrong-body signature.
     Gate: every signature is verified against the SAME canonical
     body bytes; an alien sig doesn't authorize anything.

 10. **Self-verify false claim** — binary `.dmp` carries SCI
     matching the author claim, but local source rebuild computes
     a different SCI.
     Gate: `compose_verify(self_verify=True, self_verify_sci=...)`.

The tests run entirely against in-memory primitives + JSON
sidecar text, so they're fast and don't depend on subprocess /
fixture state.  This makes them THE security source of truth
that future trust-model changes must keep green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.drift.crypto import (
	b64_encode,
	compute_ed25519_kid,
	ed25519_sign_from_seed,
)
from lang.driftc.packages.author_claim_v1 import (
	make_author_claim_body,
	AuthorClaim,
	AuthorClaimBody,
	AuthorSignature,
	RequiredDep,
	body_signing_bytes as author_body_signing_bytes,
	dump_author_claim_json,
	load_author_claim_json,
	make_author_claim,
	sign_body as author_sign_body,
)
from lang.driftc.packages.cert_claim_v1 import (
	make_cert_claim_body,
	CertClaim,
	CertClaimBody,
	CertSignature,
	CertSuite,
	DepGraphEntry,
	ResolvedDep,
	Toolchain,
	body_signing_bytes as cert_body_signing_bytes,
	dump_cert_claim_json,
	load_cert_claim_json,
	make_cert_claim,
	sign_body as cert_sign_body,
)
from lang.driftc.packages.trust_v1 import (
	NamespaceRoles,
	TrustStore,
	TrustedKey,
)
from lang.driftc.packages.verify_v1 import (
	PackageIdentity,
	compose_verify,
)


# ── Fixture helpers ───────────────────────────────────────────────


def _seed(byte: int) -> bytes:
	return bytes([byte] * 32)


def _pub(seed: bytes) -> bytes:
	_, pub = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return pub


def _kid(seed: bytes) -> str:
	return compute_ed25519_kid(_pub(seed))


_PKG_ID = "demo.lib"
_VERSION = "1.0.0"
_SCI = "sha256:" + ("a" * 64)
_ARTIFACT_SHA = "sha256:" + ("b" * 64)


def _author_body(
	*,
	package_id: str = _PKG_ID,
	version: str = _VERSION,
	sci: str = _SCI,
	required_deps: tuple = (),
) -> AuthorClaimBody:
	return make_author_claim_body(
		package_id=package_id,
		version=version,
		artifact_kind="package",
		namespaces=(package_id, f"{package_id}.*"),
		source_content_id=sci,
		required_deps=required_deps,
		release_utc="2026-05-19T00:00:00Z",
	)


def _cert_body(
	*,
	package_id: str = _PKG_ID,
	version: str = _VERSION,
	sci: str = _SCI,
	artifact_sha: str = _ARTIFACT_SHA,
	dep_graph: tuple[DepGraphEntry, ...] = (),
	cert_suite_id: str = "drift.foundation/default",
) -> CertClaimBody:
	return make_cert_claim_body(
		package_id=package_id,
		version=version,
		artifact_kind="package",
		artifact_path=f"{package_id}.zdmp",
		artifact_sha256=artifact_sha,
		source_content_id=sci,
		target="linux-x86_64",
		toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit=""),
		dep_graph=dep_graph,
		cert_suite=CertSuite(
			id=cert_suite_id, version="1.0", result="pass",
			result_evidence_sha256="sha256:" + ("f" * 64),
		),
		run_id="adv-test",
		run_started_utc="2026-05-19T00:00:00Z",
		evidence_sha256="sha256:" + ("0" * 64),
	)


def _identity(
	*,
	package_id: str = _PKG_ID,
	version: str = _VERSION,
	sci: str = _SCI,
	artifact_sha: str = _ARTIFACT_SHA,
) -> PackageIdentity:
	return PackageIdentity(
		package_id=package_id, version=version,
		source_content_id=sci, artifact_sha256=artifact_sha,
	)


def _trust(
	*,
	authors_by_ns: dict[str, set[str]] | None = None,
	certifiers_by_ns: dict[str, set[str]] | None = None,
	revoked: set[str] | None = None,
	seeds: dict[str, bytes] | None = None,
) -> TrustStore:
	"""Build a TrustStore from role mappings.  `seeds` lets the
	caller specify kid → seed so the helper can compute the matching
	pubkey for each declared kid; missing kids in `seeds` are
	registered with a placeholder pubkey (treated as "trusted but
	signature will not verify")."""
	authors_by_ns = authors_by_ns or {}
	certifiers_by_ns = certifiers_by_ns or {}
	seeds = seeds or {}
	all_kids: set[str] = set()
	for ks in authors_by_ns.values():
		all_kids |= ks
	for ks in certifiers_by_ns.values():
		all_kids |= ks
	keys: dict[str, TrustedKey] = {}
	for k in all_kids:
		# Find seed matching this kid; fall back to a placeholder.
		seed = next((s for s, kid in ((s, _kid(s)) for s in seeds.values()) if kid == k), None)
		if seed is None:
			pub = b"\x00" * 32
		else:
			pub = _pub(seed)
		keys[k] = TrustedKey(algo="ed25519", kid=k, pubkey_raw=pub, label="adv-test")
	all_ns = set(authors_by_ns) | set(certifiers_by_ns)
	roles = {
		ns: NamespaceRoles(
			authors=frozenset(authors_by_ns.get(ns, set())),
			certifiers=frozenset(certifiers_by_ns.get(ns, set())),
		)
		for ns in all_ns
	}
	return TrustStore(
		keys_by_kid=keys,
		roles_by_namespace=roles,
		revoked_kids=frozenset(revoked or set()),
	)


def _baseline_setup() -> tuple[bytes, bytes, AuthorClaim, CertClaim, TrustStore]:
	"""Standard valid claim pair + trust that ACCEPTS.  Each
	adversarial test takes this as starting point and perturbs ONE
	piece."""
	author_seed = _seed(0x01)
	cert_seed = _seed(0x02)
	author_claim = make_author_claim(_author_body(), author_seed)
	cert_claim = make_cert_claim(_cert_body(), cert_seed)
	trust = _trust(
		authors_by_ns={_PKG_ID: {_kid(author_seed)}, f"{_PKG_ID}.*": {_kid(author_seed)}},
		certifiers_by_ns={_PKG_ID: {_kid(cert_seed)}, f"{_PKG_ID}.*": {_kid(cert_seed)}},
		seeds={"author": author_seed, "cert": cert_seed},
	)
	return author_seed, cert_seed, author_claim, cert_claim, trust


def test_baseline_accepts() -> None:
	"""Sanity: the unperturbed baseline accepts.  If this regresses
	every adversarial test below is testing the wrong thing."""
	_, _, ac, cc, trust = _baseline_setup()
	r = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
	)
	assert r.ok, f"baseline must accept: {r.reason}"
	assert r.mode == "certifier-shortcut"


# ── Attack 1: Repo substitution ───────────────────────────────────


def test_attack1_repo_substitution_rejected() -> None:
	"""Attacker replaces `.dmp` bytes (new artifact_sha256) but
	keeps the valid author claim.  Cert claim's artifact_sha256 no
	longer matches the on-disk bytes."""
	_, _, ac, cc, trust = _baseline_setup()
	tampered_identity = _identity(artifact_sha="sha256:" + ("9" * 64))
	r = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=tampered_identity, module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
	)
	assert not r.ok
	assert "artifact_sha256" in r.reason.lower() or "artifact" in r.reason.lower()


# ── Attack 2: Cert claim replay ───────────────────────────────────


def test_attack2_cert_claim_replay_rejected_on_version_drop() -> None:
	"""Attacker copies a valid cert claim from `demo.lib@1.0.0`
	next to `demo.lib@1.0.1`.  Cert body's `version` no longer
	matches the package_identity."""
	_, cert_seed, ac, _, trust = _baseline_setup()
	# Re-sign the author claim for v1.0.1 source; cert claim still
	# pins v1.0.0 (the replay scenario).
	ac_v101 = make_author_claim(_author_body(version="1.0.1"), _seed(0x01))
	cc_v100 = make_cert_claim(_cert_body(version="1.0.0"), cert_seed)
	r = compose_verify(
		author_claim=ac_v101, cert_claims=[cc_v100],
		package_identity=_identity(version="1.0.1"),
		module_id=_PKG_ID, trust=trust, resolved_closure=[],
	)
	assert not r.ok
	assert "version" in r.reason.lower()


def test_attack2_cert_claim_replay_rejected_on_package_drop() -> None:
	"""Attacker drops a valid cert claim for `demo.lib` next to a
	different package `evil.pkg`.  Cert body's `package_id` no
	longer matches the package_identity."""
	_, cert_seed, _, _, _ = _baseline_setup()
	cc_for_demo = make_cert_claim(_cert_body(package_id="demo.lib"), cert_seed)
	# Trust now grants for `evil.pkg` (the attacker's target).
	author_seed = _seed(0x01)
	ac_for_evil = make_author_claim(_author_body(package_id="evil.pkg"), author_seed)
	trust = _trust(
		authors_by_ns={"evil.pkg": {_kid(author_seed)}, "evil.pkg.*": {_kid(author_seed)}},
		certifiers_by_ns={"evil.pkg": {_kid(cert_seed)}, "evil.pkg.*": {_kid(cert_seed)}},
		seeds={"a": author_seed, "c": cert_seed},
	)
	r = compose_verify(
		author_claim=ac_for_evil, cert_claims=[cc_for_demo],
		package_identity=_identity(package_id="evil.pkg"),
		module_id="evil.pkg", trust=trust, resolved_closure=[],
	)
	assert not r.ok
	assert "package_id" in r.reason.lower() or "package" in r.reason.lower()


# ── Attack 3: Author claim replay ─────────────────────────────────


def test_attack3_author_claim_replay_old_sci_rejected() -> None:
	"""Attacker keeps an old author claim (SCI=X) while
	republishing source (manifest stamp = SCI=Y).  G1: stamp
	mismatch caught before any trust check."""
	author_seed, cert_seed, _, _, trust = _baseline_setup()
	old_sci = "sha256:" + ("a" * 64)
	new_sci = "sha256:" + ("c" * 64)
	ac_old = make_author_claim(_author_body(sci=old_sci), author_seed)
	cc_new = make_cert_claim(_cert_body(sci=new_sci), cert_seed)
	# Manifest stamp = new source's SCI.
	r = compose_verify(
		author_claim=ac_old, cert_claims=[cc_new],
		package_identity=_identity(sci=new_sci),
		module_id=_PKG_ID, trust=trust, resolved_closure=[],
	)
	assert not r.ok
	assert "source_content_id" in r.reason.lower() or "sci" in r.reason.lower()


# ── Attack 4: Transitive dep swap ─────────────────────────────────


def test_attack4_transitive_dep_swap_rejected() -> None:
	"""Cert claim attests transitive dep `D` at sha256:DDD..., but
	consumer's resolved closure pins `D` at sha256:EEE... (attacker
	swapped D's .dmp bytes).  Cover check rejects."""
	author_seed, cert_seed, _, _, trust = _baseline_setup()
	good_d = DepGraphEntry(
		package_id="D", version="1.0.0",
		artifact_sha256="sha256:" + ("d" * 64),
		source_content_id="sha256:" + ("e" * 64),
		author_kid="ed25519:author-of-D",
		cert_kid="ed25519:cert-of-D",
		dep_kind="transitive",
	)
	ac = make_author_claim(
		_author_body(required_deps=(RequiredDep(name="D", version_range="^1"),)),
		author_seed,
	)
	cc = make_cert_claim(_cert_body(dep_graph=(good_d,)), cert_seed)
	# Consumer's closure has the SAME (pkg, version) but the
	# attacker's bytes (artifact_sha256 mismatch from cover check).
	swapped_d = ResolvedDep(
		package_id="D", version="1.0.0",
		artifact_sha256="sha256:" + ("9" * 64),  # attacker bytes
		source_content_id="sha256:" + ("e" * 64),
	)
	r = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(),
		module_id=_PKG_ID, trust=trust,
		resolved_closure=[swapped_d],
	)
	assert not r.ok
	assert "artifact_sha256" in r.reason or "D" in r.reason or "dep_graph" in r.reason


# ── Attack 5: Weak-suite substitution ─────────────────────────────


def test_attack5_weak_suite_substitution_rejected() -> None:
	"""Certifier signs with smoke suite, consumer requires
	release-gate.  --require-cert-suite catches it."""
	author_seed, cert_seed, ac, _, trust = _baseline_setup()
	cc_smoke = make_cert_claim(
		_cert_body(cert_suite_id="pushcoin/smoke"), cert_seed,
	)
	r = compose_verify(
		author_claim=ac, cert_claims=[cc_smoke],
		package_identity=_identity(), module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
		require_cert_suite="drift.foundation/default",
	)
	assert not r.ok
	assert "cert_suite" in r.reason.lower() or "suite" in r.reason.lower()


# ── Attack 6: Wrong-role key ──────────────────────────────────────


def test_attack6_cert_kid_signs_author_claim_rejected() -> None:
	"""Certifier kid signs an author claim.  Trust store lists this
	kid only as a certifier (not author).  The verifier rejects
	because no trusted author kid covers the claim."""
	author_seed = _seed(0x01)
	cert_seed = _seed(0x02)
	# Sign author claim with the CERT seed (wrong role).
	ac_signed_by_cert = make_author_claim(_author_body(), cert_seed)
	cc = make_cert_claim(_cert_body(), cert_seed)
	# Trust: only the cert seed's kid in the certifier role; no
	# author-role kid registered.
	trust = _trust(
		authors_by_ns={_PKG_ID: set(), f"{_PKG_ID}.*": set()},
		certifiers_by_ns={_PKG_ID: {_kid(cert_seed)}, f"{_PKG_ID}.*": {_kid(cert_seed)}},
		seeds={"c": cert_seed},
	)
	r = compose_verify(
		author_claim=ac_signed_by_cert, cert_claims=[cc],
		package_identity=_identity(), module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
	)
	assert not r.ok
	# Diagnostic must say "no trusted author" or similar.
	assert "author" in r.reason.lower()


def test_attack6_author_kid_signs_cert_claim_rejected() -> None:
	"""Author kid signs a cert claim.  Trust lists kid only as
	author (not certifier)."""
	author_seed = _seed(0x01)
	cert_seed = _seed(0x02)
	ac = make_author_claim(_author_body(), author_seed)
	# Sign cert claim with the AUTHOR seed (wrong role).
	cc_signed_by_author = make_cert_claim(_cert_body(), author_seed)
	trust = _trust(
		authors_by_ns={_PKG_ID: {_kid(author_seed)}, f"{_PKG_ID}.*": {_kid(author_seed)}},
		certifiers_by_ns={_PKG_ID: set(), f"{_PKG_ID}.*": set()},
		seeds={"a": author_seed},
	)
	r = compose_verify(
		author_claim=ac, cert_claims=[cc_signed_by_author],
		package_identity=_identity(), module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
	)
	assert not r.ok
	# Diagnostic must mention certifier trust failure.
	assert "cert" in r.reason.lower() or "certifier" in r.reason.lower()


# ── Attack 7: Namespace shadowing ─────────────────────────────────


def test_attack7_longest_prefix_wins_more_specific_grant() -> None:
	"""Trust has both `demo.*` (granting kid X) and `demo.lib`
	(granting kid Y exact).  A claim signed by Y for module
	`demo.lib` must be accepted via the EXACT match -- X's
	prefix grant does NOT block Y.  Longest-prefix-wins: exact
	beats prefix at the same length.

	This pins the namespace-resolution invariant the audit
	doc names; without it an attacker could exploit unclear
	prefix semantics to either smuggle in a different kid OR
	block a legitimate one."""
	author_seed = _seed(0x01)
	cert_seed = _seed(0x02)
	other_seed = _seed(0x03)
	ac = make_author_claim(_author_body(), author_seed)
	cc = make_cert_claim(_cert_body(), cert_seed)
	# `demo.*` grants `other_seed`'s kid as author/cert.
	# `demo.lib` (exact) grants the real author+cert kids.
	# Module being verified is `demo.lib` -- exact match wins.
	trust = _trust(
		authors_by_ns={
			"demo.*": {_kid(other_seed)},
			"demo.lib": {_kid(author_seed)},
		},
		certifiers_by_ns={
			"demo.*": {_kid(other_seed)},
			"demo.lib": {_kid(cert_seed)},
		},
		seeds={"a": author_seed, "c": cert_seed, "o": other_seed},
	)
	r = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id="demo.lib",
		trust=trust, resolved_closure=[],
	)
	assert r.ok, f"exact-match grant must win: {r.reason}"
	assert r.author_kid == _kid(author_seed)
	assert r.certifier_kid == _kid(cert_seed)


def test_attack7_prefix_grant_does_not_authorize_unrelated_signer() -> None:
	"""Trust has `demo.*` granting kid X.  A claim for module
	`demo.lib` signed by kid Y (not in either role list for ANY
	namespace) must be rejected.  Catches a regression where the
	prefix grant accidentally authorizes more kids than declared."""
	author_seed = _seed(0x01)
	cert_seed = _seed(0x02)
	rogue_seed = _seed(0x04)
	ac_signed_by_rogue = make_author_claim(_author_body(), rogue_seed)
	cc = make_cert_claim(_cert_body(), cert_seed)
	# `demo.*` grants the LEGITIMATE author kid, not the rogue's.
	trust = _trust(
		authors_by_ns={"demo.*": {_kid(author_seed)}},
		certifiers_by_ns={"demo.*": {_kid(cert_seed)}},
		seeds={"a": author_seed, "c": cert_seed, "r": rogue_seed},
	)
	r = compose_verify(
		author_claim=ac_signed_by_rogue, cert_claims=[cc],
		package_identity=_identity(), module_id="demo.lib",
		trust=trust, resolved_closure=[],
	)
	assert not r.ok
	assert "author" in r.reason.lower()


# ── Attack 8: Unsigned metadata injection ─────────────────────────


def test_attack8_extra_field_in_author_body_rejected_at_load() -> None:
	"""Attacker adds an `artifact_sha256` field to the author claim
	body (which by O6 must NOT bind artifact bytes), hoping the
	consumer's verifier honors it.  v1 strict-v1 unknown-key
	rejection refuses to load the claim at all."""
	# Build a syntactically valid claim, then mutate the JSON to
	# inject the bogus field, then attempt to load.
	author_seed = _seed(0x01)
	ac = make_author_claim(_author_body(), author_seed)
	raw = json.loads(dump_author_claim_json(ac))
	# Inject the bogus binding the attacker hopes will be honored.
	raw["body"]["artifact_sha256"] = "sha256:" + ("9" * 64)
	with pytest.raises(ValueError) as exc:
		load_author_claim_json(json.dumps(raw))
	# Diagnostic must name the unknown field so the user knows
	# where the injection happened.
	assert "artifact_sha256" in str(exc.value) or "unknown" in str(exc.value).lower()


def test_attack8_extra_top_level_field_rejected_at_load() -> None:
	"""Same shape but the injection is at the claim envelope
	level (outside body).  Strict-v1 rejects unknown keys at
	every nesting level."""
	author_seed = _seed(0x01)
	ac = make_author_claim(_author_body(), author_seed)
	raw = json.loads(dump_author_claim_json(ac))
	raw["evil_field"] = "shenanigans"
	with pytest.raises(ValueError) as exc:
		load_author_claim_json(json.dumps(raw))
	assert "evil_field" in str(exc.value) or "unknown" in str(exc.value).lower()


# ── Attack 9: Multi-signer confusion ──────────────────────────────


def test_attack9_alien_signature_does_not_authorize() -> None:
	"""Author claim carries TWO signatures: one valid + trusted,
	one from a kid that's not in the trust store.  The valid sig
	signs the legitimate body bytes.  Composition: verify accepts
	ONLY because of the trusted sig; the alien sig is ignored.

	A regression where the verifier picks the alien sig (or
	misattributes the accept to it) would surface here -- the
	accepted_kid must be the trusted author, not the alien."""
	author_seed = _seed(0x01)
	cert_seed = _seed(0x02)
	alien_seed = _seed(0x05)
	# Build claim with two sigs: trusted author + alien.
	body = _author_body()
	trusted_sig = author_sign_body(body, author_seed)
	alien_sig = author_sign_body(body, alien_seed)
	ac = AuthorClaim(body=body, signatures=(trusted_sig, alien_sig))
	cc = make_cert_claim(_cert_body(), cert_seed)
	# Trust grants ONLY the author seed (not the alien).
	trust = _trust(
		authors_by_ns={_PKG_ID: {_kid(author_seed)}, f"{_PKG_ID}.*": {_kid(author_seed)}},
		certifiers_by_ns={_PKG_ID: {_kid(cert_seed)}, f"{_PKG_ID}.*": {_kid(cert_seed)}},
		seeds={"a": author_seed, "c": cert_seed},
	)
	r = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
	)
	assert r.ok
	# Critically: the accepted kid is the trusted author, NOT the alien.
	assert r.author_kid == _kid(author_seed)
	assert r.author_kid != _kid(alien_seed)


def test_attack9_only_alien_signature_rejected() -> None:
	"""Author claim carries only an alien signature (no trusted
	sig).  Verifier rejects: no trusted author covers the claim."""
	cert_seed = _seed(0x02)
	alien_seed = _seed(0x05)
	body = _author_body()
	ac = AuthorClaim(body=body, signatures=(author_sign_body(body, alien_seed),))
	cc = make_cert_claim(_cert_body(), cert_seed)
	# Trust does NOT include alien.
	trust = _trust(
		authors_by_ns={_PKG_ID: {_kid(_seed(0x01))}, f"{_PKG_ID}.*": {_kid(_seed(0x01))}},
		certifiers_by_ns={_PKG_ID: {_kid(cert_seed)}, f"{_PKG_ID}.*": {_kid(cert_seed)}},
		seeds={"a": _seed(0x01), "c": cert_seed},
	)
	r = compose_verify(
		author_claim=ac, cert_claims=[cc],
		package_identity=_identity(), module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
	)
	assert not r.ok


def test_attack9_trusted_sig_over_different_body_rejected() -> None:
	"""Attacker glues a trusted-kid signature over a DIFFERENT
	body onto a malicious body and claims it's authorization.
	The sigs in a claim ALL sign the same canonical body bytes
	(`body_signing_bytes(body)`); a sig made over a different
	body simply doesn't verify."""
	author_seed = _seed(0x01)
	cert_seed = _seed(0x02)
	good_body = _author_body()
	evil_body = _author_body(sci="sha256:" + ("9" * 64))
	# Trusted sig BUT over the evil body's bytes.  Glue this sig
	# into a claim envelope whose body is the GOOD body.
	bad_sig_raw, _ = ed25519_sign_from_seed(
		priv_seed32=author_seed,
		message=author_body_signing_bytes(evil_body),
	)
	bad_sig = AuthorSignature(
		algo="ed25519", kid=_kid(author_seed), sig_raw=bad_sig_raw,
	)
	ac_mismatched = AuthorClaim(body=good_body, signatures=(bad_sig,))
	cc = make_cert_claim(_cert_body(), cert_seed)
	trust = _trust(
		authors_by_ns={_PKG_ID: {_kid(author_seed)}, f"{_PKG_ID}.*": {_kid(author_seed)}},
		certifiers_by_ns={_PKG_ID: {_kid(cert_seed)}, f"{_PKG_ID}.*": {_kid(cert_seed)}},
		seeds={"a": author_seed, "c": cert_seed},
	)
	r = compose_verify(
		author_claim=ac_mismatched, cert_claims=[cc],
		package_identity=_identity(), module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
	)
	assert not r.ok


# ── Attack 10: Self-verify false claim ────────────────────────────


def test_attack10_self_verify_sci_mismatch_rejected() -> None:
	"""The on-disk artifact carries an SCI stamp matching the
	author claim (so normal mode accepts), but the consumer's
	local source rebuild computes a DIFFERENT SCI.  Self-verify
	mode catches the lie -- it's the only mode that recomputes
	SCI from source.

	G1's stamp-comparison can be fooled by a coordinated
	attacker who controls both the binary and the author claim;
	self-verify breaks that coupling by computing SCI
	independently from local source."""
	author_seed = _seed(0x01)
	ac = make_author_claim(_author_body(sci="sha256:" + ("a" * 64)), author_seed)
	trust = _trust(
		authors_by_ns={_PKG_ID: {_kid(author_seed)}, f"{_PKG_ID}.*": {_kid(author_seed)}},
		certifiers_by_ns={},
		seeds={"a": author_seed},
	)
	# Note: cert_claims=[] because self-verify mode doesn't consult
	# cert claims.  package_identity.source_content_id matches the
	# author claim's stamp (normal-mode gate passes).  But the
	# consumer's recomputed SCI from local source is DIFFERENT.
	r = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(sci="sha256:" + ("a" * 64)),
		module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
		self_verify=True,
		self_verify_sci="sha256:" + ("9" * 64),  # consumer rebuild
	)
	assert not r.ok
	assert "self_verify" in r.reason.lower() or "source_content_id" in r.reason.lower() or "sci" in r.reason.lower()


def test_attack10_self_verify_matching_sci_accepts() -> None:
	"""Positive control: when local-rebuild SCI matches the author
	claim, self-verify accepts and reports `mode='self-verify'`."""
	author_seed = _seed(0x01)
	sci = "sha256:" + ("a" * 64)
	ac = make_author_claim(_author_body(sci=sci), author_seed)
	trust = _trust(
		authors_by_ns={_PKG_ID: {_kid(author_seed)}, f"{_PKG_ID}.*": {_kid(author_seed)}},
		certifiers_by_ns={},
		seeds={"a": author_seed},
	)
	r = compose_verify(
		author_claim=ac, cert_claims=[],
		package_identity=_identity(sci=sci),
		module_id=_PKG_ID,
		trust=trust, resolved_closure=[],
		self_verify=True,
		self_verify_sci=sci,
	)
	assert r.ok, f"self-verify with matching SCI must accept: {r.reason}"
	assert r.mode == "self-verify"
	assert r.certifier_kid is None  # self-verify doesn't consult cert claims
