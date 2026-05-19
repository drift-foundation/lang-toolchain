# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for `lang.driftc.packages.trust_v1`.

Pure-Python TrustStore v1 behavior: load/dump round-trip, role-tagged
namespace lookups (authors vs certifiers), longest-prefix-wins
matching, revocation handling, merge semantics, and strict rejection
of pre-v1 / malformed shapes.

Plan reference: `work/drift-trust-model-audit/plan.md` §2 + §11
(decisions O1-O8).  Slice 1 of the trust-v1 implementation.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lang.driftc.packages.trust_v1 import (
	NamespaceRoles,
	TrustStore,
	TrustedKey,
	dump_trust_store_json,
	load_trust_store_json,
	merge_trust_stores,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _kid(label: str) -> str:
	"""Synth a kid string from a label; useful for readable test data."""
	return f"ed25519:{base64.b64encode(label.encode().ljust(32, b'_')[:32]).decode()}"


def _pub32(label: str) -> bytes:
	"""Synth a deterministic 32-byte pubkey for a label."""
	return label.encode().ljust(32, b"_")[:32]


def _pub32_b64(label: str) -> str:
	return base64.b64encode(_pub32(label)).decode("ascii")


def _v1_doc(
	*,
	keys: dict | None = None,
	namespaces: dict | None = None,
	revoked: list | None = None,
) -> dict:
	return {
		"format": "drift-trust",
		"version": 1,
		"keys": keys or {},
		"namespaces": namespaces or {},
		"revoked": revoked or [],
	}


# ── Load: happy path ────────────────────────────────────────────────


def test_load_minimal_v1(tmp_path: Path) -> None:
	"""A minimal valid v1 trust store loads cleanly."""
	doc = _v1_doc()
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	store = load_trust_store_json(p)
	assert store.keys_by_kid == {}
	assert store.roles_by_namespace == {}
	assert store.revoked_kids == frozenset()


def test_load_v1_with_keys_and_roles(tmp_path: Path) -> None:
	"""Load a v1 store with role-tagged namespace entries."""
	author = _kid("foundation_author")
	certifier = _kid("foundation_certifier")
	doc = _v1_doc(
		keys={
			author: {
				"algo": "ed25519",
				"pubkey": _pub32_b64("foundation_author"),
				"label": "Drift Foundation author",
			},
			certifier: {
				"algo": "ed25519",
				"pubkey": _pub32_b64("foundation_certifier"),
			},
		},
		namespaces={
			"std.*": {"authors": [author], "certifiers": [certifier]},
			"mariadb.rpc.*": {"authors": [author], "certifiers": [certifier]},
		},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	store = load_trust_store_json(p)
	assert author in store.keys_by_kid
	assert store.keys_by_kid[author].label == "Drift Foundation author"
	assert store.keys_by_kid[certifier].label == ""
	# Role-tagged lookups.
	assert store.allowed_authors_for_module("std.io") == {author}
	assert store.allowed_certifiers_for_module("std.io") == {certifier}
	assert store.allowed_authors_for_module("mariadb.rpc.managed") == {author}


# ── Load: strict rejection of pre-v1 / malformed shapes ────────────


def test_reject_v0_version(tmp_path: Path) -> None:
	"""Pre-v1 trust stores (version: 0) are rejected with a clear
	diagnostic.  No fallback loader (per plan §0)."""
	doc = {
		"format": "drift-trust",
		"version": 0,
		"keys": {},
		"namespaces": {},
		"revoked": [],
	}
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="expected v1"):
		load_trust_store_json(p)


def test_reject_flat_namespace_list(tmp_path: Path) -> None:
	"""v0's flat list shape `"<pattern>": ["<kid>"]` is rejected.
	The v1 shape requires the {authors, certifiers} dict per
	namespace."""
	author = _kid("a")
	doc = _v1_doc(
		keys={author: {"algo": "ed25519", "pubkey": _pub32_b64("a")}},
		namespaces={"acme.*": [author]},   # v0 flat list shape
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="v0 flat list shape"):
		load_trust_store_json(p)


def test_reject_wrong_format_tag(tmp_path: Path) -> None:
	doc = {"format": "not-drift-trust", "version": 1, "keys": {}, "namespaces": {}}
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="format"):
		load_trust_store_json(p)


def test_reject_non_object_root(tmp_path: Path) -> None:
	p = tmp_path / "t.json"
	p.write_text(json.dumps(["not", "an", "object"]))
	with pytest.raises(ValueError, match="JSON object"):
		load_trust_store_json(p)


def test_reject_bad_algo(tmp_path: Path) -> None:
	doc = _v1_doc(
		keys={"ed25519:x": {"algo": "rsa", "pubkey": _pub32_b64("x")}},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="ed25519"):
		load_trust_store_json(p)


def test_reject_wrong_pubkey_length(tmp_path: Path) -> None:
	doc = _v1_doc(
		keys={"ed25519:x": {"algo": "ed25519", "pubkey": base64.b64encode(b"short").decode()}},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="32 bytes"):
		load_trust_store_json(p)


def test_reject_authors_not_list(tmp_path: Path) -> None:
	doc = _v1_doc(
		namespaces={"acme.*": {"authors": "not-a-list", "certifiers": []}},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="'authors' must be a list"):
		load_trust_store_json(p)


def test_reject_certifiers_not_list(tmp_path: Path) -> None:
	doc = _v1_doc(
		namespaces={"acme.*": {"authors": [], "certifiers": 42}},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="'certifiers' must be a list"):
		load_trust_store_json(p)


def test_reject_empty_namespace_pattern(tmp_path: Path) -> None:
	doc = _v1_doc(
		namespaces={"": {"authors": [], "certifiers": []}},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="non-empty string"):
		load_trust_store_json(p)


# ── Load: fail-closed on unknown role-list kids ─────────────────────


def test_reject_authors_kid_missing_from_keys(tmp_path: Path) -> None:
	"""A trust store that names a kid in `authors` but never
	declares its public key in `keys` is rejected at load time.
	v1 fails closed -- the verifier cannot later validate signatures
	by a kid it has no key for, so the policy is malformed."""
	doc = _v1_doc(
		keys={},  # no keys declared
		namespaces={"acme.*": {"authors": ["ed25519:missing"], "certifiers": []}},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="'authors' references kid 'ed25519:missing'"):
		load_trust_store_json(p)


def test_reject_certifiers_kid_missing_from_keys(tmp_path: Path) -> None:
	"""Same fail-closed rule for certifier-role kids."""
	declared_author = _kid("a")
	doc = _v1_doc(
		keys={
			declared_author: {"algo": "ed25519", "pubkey": _pub32_b64("a")},
		},
		namespaces={
			"acme.*": {
				"authors": [declared_author],
				"certifiers": ["ed25519:missing_certifier"],
			},
		},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError, match="'certifiers' references kid"):
		load_trust_store_json(p)


def test_revoked_may_reference_absent_kid(tmp_path: Path) -> None:
	"""`revoked` is exempt from the keys-coverage check.  Revocation
	may name kids that were never in the local keys map (e.g.
	revoked by an upstream policy the local store never imported).
	"""
	doc = _v1_doc(
		keys={},
		namespaces={},
		revoked=["ed25519:never_imported_but_revoked"],
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	# Should load cleanly.
	store = load_trust_store_json(p)
	assert "ed25519:never_imported_but_revoked" in store.revoked_kids


def test_role_kid_covered_by_keys_loads_clean(tmp_path: Path) -> None:
	"""Sanity: when every role-list kid IS in `keys`, the load succeeds."""
	author = _kid("a")
	certifier = _kid("c")
	doc = _v1_doc(
		keys={
			author: {"algo": "ed25519", "pubkey": _pub32_b64("a")},
			certifier: {"algo": "ed25519", "pubkey": _pub32_b64("c")},
		},
		namespaces={
			"acme.*": {"authors": [author], "certifiers": [certifier]},
		},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	store = load_trust_store_json(p)
	assert store.allowed_authors_for_module("acme.foo") == {author}
	assert store.allowed_certifiers_for_module("acme.foo") == {certifier}


# ── Load: base64 / encoding errors wrap cleanly ────────────────────


def test_invalid_base64_pubkey_wraps_as_value_error(tmp_path: Path) -> None:
	"""Malformed base64 in a pubkey field surfaces as a ValueError
	with trust-store-specific context, not a bare binascii.Error."""
	doc = _v1_doc(
		keys={"ed25519:bad": {"algo": "ed25519", "pubkey": "!!! not base64 !!!"}},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc))
	with pytest.raises(ValueError) as excinfo:
		load_trust_store_json(p)
	msg = str(excinfo.value)
	assert "ed25519:bad" in msg
	assert "invalid base64" in msg


def test_non_ascii_pubkey_wraps_as_value_error(tmp_path: Path) -> None:
	"""Non-ASCII characters in a pubkey field surface as a ValueError
	with trust-store-specific context.  Python's base64.b64decode
	would otherwise raise a less obvious encoding error."""
	doc = _v1_doc(
		keys={"ed25519:utf8": {"algo": "ed25519", "pubkey": "AAAAéAAAA"}},
	)
	p = tmp_path / "t.json"
	p.write_text(json.dumps(doc), encoding="utf-8")
	with pytest.raises(ValueError) as excinfo:
		load_trust_store_json(p)
	msg = str(excinfo.value)
	assert "ed25519:utf8" in msg
	# The non-ASCII path triggers either the encode catch or the
	# validate=True base64 catch depending on byte position; either
	# wrapped message identifies the offending field.
	assert ("non-ASCII" in msg) or ("invalid base64" in msg)


# ── Role-tagged lookups ────────────────────────────────────────────


def _store_with_roles(
	*roles: tuple[str, list[str], list[str]],
	revoked: frozenset[str] | None = None,
) -> TrustStore:
	"""Build a TrustStore in-memory from (pattern, authors, certifiers) tuples."""
	return TrustStore(
		keys_by_kid={},  # keys not needed for pure-policy lookups
		roles_by_namespace={
			ns: NamespaceRoles(authors=frozenset(authors), certifiers=frozenset(certifiers))
			for ns, authors, certifiers in roles
		},
		revoked_kids=revoked or frozenset(),
	)


def test_lookup_author_role_only() -> None:
	a = "ed25519:a"
	store = _store_with_roles(("acme.*", [a], []))
	assert store.allowed_authors_for_module("acme.foo") == {a}
	assert store.allowed_certifiers_for_module("acme.foo") == set()


def test_lookup_certifier_role_only() -> None:
	c = "ed25519:c"
	store = _store_with_roles(("acme.*", [], [c]))
	assert store.allowed_authors_for_module("acme.foo") == set()
	assert store.allowed_certifiers_for_module("acme.foo") == {c}


def test_lookup_both_roles_independent() -> None:
	a = "ed25519:a"
	c = "ed25519:c"
	store = _store_with_roles(("acme.*", [a], [c]))
	assert store.allowed_authors_for_module("acme.foo") == {a}
	assert store.allowed_certifiers_for_module("acme.foo") == {c}


def test_lookup_same_kid_in_both_roles() -> None:
	"""Author-as-distributor pattern (per O6): the same kid may
	be in both lists when an author acts in the certifier role too."""
	k = "ed25519:dual"
	store = _store_with_roles(("acme.*", [k], [k]))
	assert store.allowed_authors_for_module("acme.foo") == {k}
	assert store.allowed_certifiers_for_module("acme.foo") == {k}


def test_lookup_no_match_returns_empty() -> None:
	a = "ed25519:a"
	store = _store_with_roles(("acme.*", [a], [a]))
	assert store.allowed_authors_for_module("other.foo") == set()
	assert store.allowed_certifiers_for_module("other.foo") == set()


# ── Longest-prefix-wins matching ────────────────────────────────────


def test_longest_prefix_wins_authors() -> None:
	broad_a = "ed25519:broad_a"
	specific_a = "ed25519:specific_a"
	store = _store_with_roles(
		("acme.*", [broad_a], []),
		("acme.crypto.*", [specific_a], []),
	)
	# Specific wins for modules under the deeper prefix.
	assert store.allowed_authors_for_module("acme.crypto.foo") == {specific_a}
	# Broad wins for modules under the shallower prefix only.
	assert store.allowed_authors_for_module("acme.io") == {broad_a}


def test_exact_match_vs_prefix_at_same_length() -> None:
	"""Exact match `acme.crypto` (length 11) and prefix `acme.crypto.*`
	(prefix length 11) tie — role lists union per the longest-prefix-wins
	rule's tie-break."""
	exact_a = "ed25519:exact_a"
	prefix_a = "ed25519:prefix_a"
	store = _store_with_roles(
		("acme.crypto", [exact_a], []),
		("acme.crypto.*", [prefix_a], []),
	)
	# At "acme.crypto" the exact entry (len 11) ties with the prefix
	# entry's pfx "acme.crypto" (also len 11).  Union.
	assert store.allowed_authors_for_module("acme.crypto") == {exact_a, prefix_a}
	# Under the deeper module only the prefix applies.
	assert store.allowed_authors_for_module("acme.crypto.foo") == {prefix_a}


def test_prefix_must_be_dot_terminated_or_equal() -> None:
	"""`acme.*` (prefix `acme`) matches `acme` itself and `acme.x`,
	but NOT `acmex.foo` (no dot boundary)."""
	a = "ed25519:a"
	store = _store_with_roles(("acme.*", [a], []))
	assert store.allowed_authors_for_module("acme") == {a}
	assert store.allowed_authors_for_module("acme.x") == {a}
	assert store.allowed_authors_for_module("acmex.foo") == set()


# ── Revocation overrides namespace policy ─────────────────────────


def test_revoked_kid_excluded_from_authors() -> None:
	a = "ed25519:a"
	store = _store_with_roles(("acme.*", [a], []), revoked=frozenset({a}))
	assert store.allowed_authors_for_module("acme.foo") == set()


def test_revoked_kid_excluded_from_certifiers() -> None:
	c = "ed25519:c"
	store = _store_with_roles(("acme.*", [], [c]), revoked=frozenset({c}))
	assert store.allowed_certifiers_for_module("acme.foo") == set()


def test_revocation_overrides_both_roles_for_dual_kid() -> None:
	k = "ed25519:k"
	store = _store_with_roles(("acme.*", [k], [k]), revoked=frozenset({k}))
	assert store.allowed_authors_for_module("acme.foo") == set()
	assert store.allowed_certifiers_for_module("acme.foo") == set()


def test_revocation_does_not_affect_other_kids() -> None:
	revoked = "ed25519:revoked"
	live = "ed25519:live"
	store = _store_with_roles(("acme.*", [revoked, live], []), revoked=frozenset({revoked}))
	assert store.allowed_authors_for_module("acme.foo") == {live}


# ── merge_trust_stores semantics ────────────────────────────────────


def test_merge_unions_role_lists_per_namespace() -> None:
	a1 = "ed25519:a1"
	a2 = "ed25519:a2"
	primary = _store_with_roles(("acme.*", [a1], []))
	secondary = _store_with_roles(("acme.*", [a2], []))
	merged = merge_trust_stores(primary, secondary)
	assert merged.allowed_authors_for_module("acme.foo") == {a1, a2}


def test_merge_keys_primary_wins() -> None:
	# Same kid, different keys — primary wins.
	kid = "ed25519:dup"
	p_key = TrustedKey(algo="ed25519", kid=kid, pubkey_raw=b"P" * 32, label="primary")
	s_key = TrustedKey(algo="ed25519", kid=kid, pubkey_raw=b"S" * 32, label="secondary")
	primary = TrustStore(
		keys_by_kid={kid: p_key}, roles_by_namespace={}, revoked_kids=frozenset(),
	)
	secondary = TrustStore(
		keys_by_kid={kid: s_key}, roles_by_namespace={}, revoked_kids=frozenset(),
	)
	merged = merge_trust_stores(primary, secondary)
	assert merged.keys_by_kid[kid].label == "primary"
	assert merged.keys_by_kid[kid].pubkey_raw == b"P" * 32


def test_merge_namespaces_present_in_only_one_carry_through() -> None:
	a = "ed25519:a"
	c = "ed25519:c"
	primary = _store_with_roles(("acme.*", [a], []))
	secondary = _store_with_roles(("other.*", [], [c]))
	merged = merge_trust_stores(primary, secondary)
	assert merged.allowed_authors_for_module("acme.foo") == {a}
	assert merged.allowed_certifiers_for_module("other.foo") == {c}


def test_merge_revoked_unions() -> None:
	r1 = "ed25519:r1"
	r2 = "ed25519:r2"
	primary = TrustStore({}, {}, frozenset({r1}))
	secondary = TrustStore({}, {}, frozenset({r2}))
	merged = merge_trust_stores(primary, secondary)
	assert merged.revoked_kids == frozenset({r1, r2})


# ── dump round-trip ────────────────────────────────────────────────


def test_dump_round_trip(tmp_path: Path) -> None:
	"""Serialize and reload — round-trips to an equal store."""
	author = _kid("a")
	certifier = _kid("c")
	original = TrustStore(
		keys_by_kid={
			author: TrustedKey(algo="ed25519", kid=author, pubkey_raw=_pub32("a"), label="author A"),
			certifier: TrustedKey(algo="ed25519", kid=certifier, pubkey_raw=_pub32("c"), label=""),
		},
		roles_by_namespace={
			"acme.*": NamespaceRoles(authors=frozenset({author}), certifiers=frozenset({certifier})),
		},
		revoked_kids=frozenset({"ed25519:revoked"}),
	)
	serialized = dump_trust_store_json(original)
	p = tmp_path / "round.json"
	p.write_text(serialized)
	reloaded = load_trust_store_json(p)
	# Note: revoked_kids includes "ed25519:revoked" but no matching
	# key entry — that's allowed (revocation is identity-only).
	assert reloaded.roles_by_namespace == original.roles_by_namespace
	assert reloaded.revoked_kids == original.revoked_kids
	assert set(reloaded.keys_by_kid) == set(original.keys_by_kid)
	for kid in reloaded.keys_by_kid:
		assert reloaded.keys_by_kid[kid].pubkey_raw == original.keys_by_kid[kid].pubkey_raw
		assert reloaded.keys_by_kid[kid].label == original.keys_by_kid[kid].label


def test_dump_is_deterministic() -> None:
	"""Sort order in the serialized form is stable across runs."""
	author1 = _kid("z_author")  # would sort last alphabetically
	author2 = _kid("a_author")
	store = TrustStore(
		keys_by_kid={
			author1: TrustedKey(algo="ed25519", kid=author1, pubkey_raw=_pub32("z"), label=""),
			author2: TrustedKey(algo="ed25519", kid=author2, pubkey_raw=_pub32("a"), label=""),
		},
		roles_by_namespace={
			"z.*": NamespaceRoles(authors=frozenset({author1}), certifiers=frozenset()),
			"a.*": NamespaceRoles(authors=frozenset({author2}), certifiers=frozenset()),
		},
	)
	out1 = dump_trust_store_json(store)
	out2 = dump_trust_store_json(store)
	assert out1 == out2
	# Sorted by kid / pattern, so "a_..." entries appear before "z_..." entries.
	first_idx_a = out1.index(author2)
	first_idx_z = out1.index(author1)
	assert first_idx_a < first_idx_z
	# Namespaces sorted too: "a.*" before "z.*".
	assert out1.index('"a.*"') < out1.index('"z.*"')


# ── load_core_trust_store ──────────────────────────────────────────


def test_load_core_trust_store_finds_v1_file() -> None:
	"""The toolchain-shipped `core_trust_v1.json` loads without error.
	Slice 1 ships an empty file; slice 6 will populate it with the
	Foundation bootstrap kid material."""
	from lang.driftc.packages.trust_v1 import load_core_trust_store
	store = load_core_trust_store()
	assert isinstance(store, TrustStore)
	# Empty in slice 1; this assertion will need updating in slice 6
	# when Foundation kid material is added.
	assert store.keys_by_kid == {}
	assert store.roles_by_namespace == {}


# ── Drift-web / PushCoin shape spot-checks ──────────────────────────


def test_pushcoin_shape() -> None:
	"""PushCoin owns `singular.*` (author + certifier).  Foundation
	owns `mariadb.*` (author + certifier).  Cross-publisher trust
	resolves cleanly: pushcoin kid never appears for mariadb.*, and
	foundation kid never appears for singular.*."""
	pc_author = "ed25519:pc_author"
	pc_certifier = "ed25519:pc_certifier"
	fdn_author = "ed25519:fdn_author"
	fdn_certifier = "ed25519:fdn_certifier"
	store = _store_with_roles(
		("singular.*", [pc_author], [pc_certifier]),
		("mariadb.rpc.*", [fdn_author], [fdn_certifier]),
	)
	# Singular modules: PushCoin keys only.
	assert store.allowed_authors_for_module("singular.api") == {pc_author}
	assert store.allowed_certifiers_for_module("singular.api") == {pc_certifier}
	assert fdn_author not in store.allowed_authors_for_module("singular.api")
	# MariaDB modules: Foundation keys only.  Critical negative:
	# pushcoin kid never appears here.
	assert store.allowed_authors_for_module("mariadb.rpc.managed") == {fdn_author}
	assert store.allowed_certifiers_for_module("mariadb.rpc.managed") == {fdn_certifier}
	assert pc_author not in store.allowed_authors_for_module("mariadb.rpc.managed")
	assert pc_certifier not in store.allowed_certifiers_for_module("mariadb.rpc.managed")


def test_stdlib_shape_role_tagged_per_o2() -> None:
	"""Per O2: reserved namespaces (std.*, lang.*, drift.*) get
	role-tagged author + certifier entries.  No 'Foundation special
	case' / 'authors-only' shortcut for stdlib.  During bootstrap,
	the same kid MAY appear in both lists, but both lists are
	present."""
	fdn = "ed25519:fdn_bootstrap"
	# Bootstrap shape: same kid in both roles.
	store = _store_with_roles(
		("std.*", [fdn], [fdn]),
		("lang.*", [fdn], [fdn]),
		("drift.*", [fdn], [fdn]),
	)
	assert store.allowed_authors_for_module("std.io") == {fdn}
	assert store.allowed_certifiers_for_module("std.io") == {fdn}
	assert store.allowed_authors_for_module("lang.test") == {fdn}
	assert store.allowed_certifiers_for_module("drift.rt") == {fdn}
