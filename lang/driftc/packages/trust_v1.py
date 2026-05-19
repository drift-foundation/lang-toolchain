# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Trust store v1 — role-tagged namespace policy.

The trust model splits acceptance into two roles per namespace:

- **authors**: kids trusted to sign a package's `.author-claim`,
  which binds source identity and release intent (namespace,
  package_id, version, source_content_id, declared deps).  An
  author claim is ALWAYS required for consumer acceptance.

- **certifiers**: kids trusted to sign a package's `.cert-claim`,
  which binds artifact bytes (`artifact_sha256`), toolchain
  identity, the full resolved dep graph, and the cert suite
  result.  A trusted cert claim is one of two artifact-acceptance
  paths; the other is consumer self-verify (rebuild from source).

The verifier composes both roles per namespace: the author claim
proves "who authorized this source release"; the cert claim
proves "who vouches for this concrete artifact".  Same actor MAY
hold both roles, but the policy states them separately.

Pre-v1 trust stores (`version: 0`) are NOT accepted.  Drift is
pre-release; there is no migration path.  Carrying both v0 and
v1 loaders would create permanent format ambiguity for zero
user benefit.  See `work/drift-trust-model-audit/plan.md` §0.

Namespace matching: longest-prefix-wins (exact match beats prefix
of the same length; ties union the role lists).  The matched
entry's `authors` and `certifiers` lists are authoritative for
that module_id — there is no cross-entry composition per role
beyond what the matched entry declares.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _b64_decode(text: str, *, context: str) -> bytes:
	"""Strict base64 decode; wrap decode errors with trust-store-specific context.

	`context` is a short identifier (e.g. the kid being parsed) so the
	resulting ValueError points at the offending field in a config
	file rather than surfacing a bare `binascii.Error`.
	"""
	try:
		ascii_bytes = text.encode("ascii")
	except UnicodeEncodeError as err:
		raise ValueError(
			f"trust store {context}: base64 field contains non-ASCII characters: {err}"
		) from err
	try:
		return base64.b64decode(ascii_bytes, validate=True)
	except (ValueError, base64.binascii.Error) as err:
		raise ValueError(
			f"trust store {context}: invalid base64 payload: {err}"
		) from err


@dataclass(frozen=True)
class TrustedKey:
	"""A public key entry in the trust store.

	`label` is an optional human-readable hint (e.g. "Drift Foundation
	author") and carries no trust significance.  Role assignment is
	declared per-namespace in `TrustStore.allowed_*_by_namespace`, not
	on the key itself — a kid MAY play different roles in different
	namespaces in principle, though the recommended practice is one
	role per kid.
	"""
	algo: str       # "ed25519" in v1
	kid: str
	pubkey_raw: bytes  # raw bytes (32 for Ed25519)
	label: str = ""


@dataclass(frozen=True)
class NamespaceRoles:
	"""Per-namespace role policy.

	Both lists are independent.  An entry that omits one role grants
	no trust in that role for the namespace; consumers requiring the
	missing role must self-verify (for `certifiers`) or have no
	acceptance path at all (for `authors`).
	"""
	authors: frozenset[str]
	certifiers: frozenset[str]


@dataclass(frozen=True)
class TrustStore:
	"""Resolved trust store.

	- `keys_by_kid` provides public keys for verification.
	- `roles_by_namespace` pins which keys play which role per
	  namespace.
	- `revoked_kids` is a local revocation set; revoked kids are
	  excluded from BOTH role lookups regardless of namespace.
	"""
	keys_by_kid: dict[str, TrustedKey]
	roles_by_namespace: dict[str, NamespaceRoles]
	revoked_kids: frozenset[str] = field(default_factory=frozenset)

	def allowed_authors_for_module(self, module_id: str) -> set[str]:
		"""Return author-role kids trusted to sign `.author-claim` for
		this module's namespace.

		Uses longest-prefix-wins matching; revoked kids are excluded.
		"""
		return self._allowed_for_role(module_id, role="authors")

	def allowed_certifiers_for_module(self, module_id: str) -> set[str]:
		"""Return certifier-role kids trusted to sign `.cert-claim` for
		this module's namespace.

		Uses longest-prefix-wins matching; revoked kids are excluded.
		"""
		return self._allowed_for_role(module_id, role="certifiers")

	def _allowed_for_role(self, module_id: str, *, role: str) -> set[str]:
		"""Internal: longest-prefix-wins lookup parameterized by role.

		Mirrors v0's `allowed_kids_for_module` shape (exact > prefix at
		same length; ties union) but reads from the matched
		`NamespaceRoles` entry's role list.
		"""
		best_len = -1
		out: set[str] = set()
		for ns, roles in self.roles_by_namespace.items():
			if ns.endswith(".*"):
				pfx = ns[:-2]
				if module_id == pfx or module_id.startswith(pfx + "."):
					match_len = len(pfx)
				else:
					continue
			else:
				if module_id != ns:
					continue
				match_len = len(ns)
			kids = roles.authors if role == "authors" else roles.certifiers
			if match_len > best_len:
				best_len = match_len
				out = set(kids)
			elif match_len == best_len:
				out |= set(kids)
		# Exclude any revoked kid from the result regardless of
		# matching depth.  Revocation overrides namespace policy.
		return out - self.revoked_kids


# ──────────────────────────────────────────────────────────────────
# Load / serialize
# ──────────────────────────────────────────────────────────────────


_FORMAT_TAG = "drift-trust"
_FORMAT_VERSION = 1


def load_trust_store_json(path: Path) -> TrustStore:
	"""Load a v1 trust store file.

	Format:

	    {
	      "format": "drift-trust",
	      "version": 1,
	      "keys": {
	        "<kid>": {
	          "algo": "ed25519",
	          "pubkey": "<base64 raw bytes>",
	          "label": "<optional>"
	        }
	      },
	      "namespaces": {
	        "<pattern>": {
	          "authors":    ["<kid>", ...],
	          "certifiers": ["<kid>", ...]
	        }
	      },
	      "revoked": ["<kid>", ...]
	    }

	Anything other than `version: 1` (including the pre-v1 v0 store
	shape) is rejected with a clear "unsupported format version;
	expected v1" diagnostic.  No v0 loader exists.  No flat
	`namespaces: { "<pattern>": ["<kid>", ...] }` fallback.
	"""
	obj = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(obj, dict):
		raise ValueError("trust store must be a JSON object")

	fmt = obj.get("format")
	ver = obj.get("version")
	if fmt != _FORMAT_TAG:
		raise ValueError(
			f"unsupported trust store format: expected {_FORMAT_TAG!r}, got {fmt!r}"
		)
	if ver != _FORMAT_VERSION:
		raise ValueError(
			f"unsupported trust store version: expected v{_FORMAT_VERSION}, "
			f"got v{ver!r}.  Pre-v1 trust stores are not accepted; "
			f"regenerate the trust file in v1 shape."
		)

	keys_obj = obj.get("keys")
	if not isinstance(keys_obj, dict):
		raise ValueError("trust store 'keys' must be a JSON object")
	keys_by_kid: dict[str, TrustedKey] = {}
	for kid, kobj in keys_obj.items():
		if not isinstance(kid, str):
			raise ValueError(f"trust store key id must be a string; got {kid!r}")
		if not isinstance(kobj, dict):
			raise ValueError(f"trust store entry for {kid!r} must be a JSON object")
		algo = kobj.get("algo")
		if algo != "ed25519":
			raise ValueError(
				f"trust store entry for {kid!r} must declare algo='ed25519' "
				f"(got {algo!r})"
			)
		pub_b64 = kobj.get("pubkey")
		if not isinstance(pub_b64, str):
			raise ValueError(f"trust store entry for {kid!r} missing 'pubkey'")
		pub_raw = _b64_decode(pub_b64, context=f"key {kid!r} 'pubkey'")
		if len(pub_raw) != 32:
			raise ValueError(
				f"trust store entry for {kid!r}: ed25519 pubkey must be 32 bytes, "
				f"got {len(pub_raw)}"
			)
		label_raw = kobj.get("label", "")
		label = label_raw if isinstance(label_raw, str) else ""
		keys_by_kid[kid] = TrustedKey(
			algo=algo, kid=kid, pubkey_raw=pub_raw, label=label,
		)

	ns_obj = obj.get("namespaces")
	if not isinstance(ns_obj, dict):
		raise ValueError("trust store 'namespaces' must be a JSON object")
	roles_by_namespace: dict[str, NamespaceRoles] = {}
	for ns, entry in ns_obj.items():
		if not isinstance(ns, str) or not ns:
			raise ValueError(f"trust store namespace pattern must be a non-empty string; got {ns!r}")
		if not isinstance(entry, dict):
			raise ValueError(
				f"trust store namespace {ns!r} must be an object with "
				f"'authors' and/or 'certifiers' lists; got {type(entry).__name__}.  "
				f"v0 flat list shape (`{ns!r}: [<kid>, ...]`) is NOT accepted."
			)
		authors_raw = entry.get("authors", [])
		certifiers_raw = entry.get("certifiers", [])
		if not isinstance(authors_raw, list):
			raise ValueError(
				f"trust store namespace {ns!r}: 'authors' must be a list of kid strings"
			)
		if not isinstance(certifiers_raw, list):
			raise ValueError(
				f"trust store namespace {ns!r}: 'certifiers' must be a list of kid strings"
			)
		authors = frozenset(_validated_kid(k, ns, "authors") for k in authors_raw)
		certifiers = frozenset(_validated_kid(k, ns, "certifiers") for k in certifiers_raw)
		roles_by_namespace[ns] = NamespaceRoles(authors=authors, certifiers=certifiers)

	revoked_raw = obj.get("revoked", [])
	if not isinstance(revoked_raw, list):
		raise ValueError(
			"trust store 'revoked' must be a flat list of kid strings"
		)
	revoked_kids = frozenset(_validated_kid(k, "<revoked>", "revoked") for k in revoked_raw)

	# Fail-closed cross-check: every kid in any namespace's authors or
	# certifiers list MUST have a corresponding entry in `keys` with a
	# resolvable pubkey.  Otherwise `allowed_authors_for_module()`
	# could later return a kid for which `keys_by_kid` has no
	# verification material -- a verifier would have to silently drop
	# it or fail much later with a confusing error.  v1 rejects at
	# load time.  `revoked` is exempt: revocation may name kids whose
	# keys are not present (e.g. a kid revoked from an upstream policy
	# the local store never imported).
	for ns, roles in roles_by_namespace.items():
		for kid in roles.authors:
			if kid not in keys_by_kid:
				raise ValueError(
					f"trust store namespace {ns!r}: 'authors' references "
					f"kid {kid!r} which has no entry in 'keys'.  Every "
					f"role-list kid must have a matching public key "
					f"declared; otherwise the verifier cannot validate "
					f"signatures by that kid."
				)
		for kid in roles.certifiers:
			if kid not in keys_by_kid:
				raise ValueError(
					f"trust store namespace {ns!r}: 'certifiers' references "
					f"kid {kid!r} which has no entry in 'keys'.  Every "
					f"role-list kid must have a matching public key "
					f"declared; otherwise the verifier cannot validate "
					f"signatures by that kid."
				)

	return TrustStore(
		keys_by_kid=keys_by_kid,
		roles_by_namespace=roles_by_namespace,
		revoked_kids=revoked_kids,
	)


def _validated_kid(k: Any, ns: str, field_name: str) -> str:
	"""Type-and-shape check for a kid string entry."""
	if not isinstance(k, str) or not k:
		raise ValueError(
			f"trust store namespace {ns!r} field {field_name!r}: "
			f"kid must be a non-empty string; got {k!r}"
		)
	return k


def load_core_trust_store() -> TrustStore:
	"""Load the toolchain-shipped core trust store (v1).

	This store is authoritative for reserved namespaces (`lang.*`,
	`std.*`, `drift.*`) and is not influenced by user/project trust
	files.  Per O2 there is no "Foundation special case": stdlib
	gets the same role-tagged shape as any other namespace.  In the
	bootstrap window Foundation MAY reuse the same kid in both
	`authors` and `certifiers` lists, but both lists must be
	present.

	Reads from `core_trust_v1.json` (separate file from the legacy
	`core_trust.json` consumed by v0 loaders).  Slice 1's additive
	constraint keeps v0 files undisturbed; slice 4's sweep renames
	the v1 file into place and deletes the v0 file.
	"""
	default_path = Path(__file__).with_name("core_trust_v1.json")
	if not default_path.exists():
		raise ValueError(f"core trust store not found: {default_path}")
	return load_trust_store_json(default_path)


def merge_trust_stores(primary: TrustStore, secondary: TrustStore) -> TrustStore:
	"""Merge two trust stores deterministically.

	- `keys_by_kid`: primary entries win on conflict.
	- `roles_by_namespace`: per-namespace authors and certifiers
	  lists union across primary and secondary.  Namespaces present
	  in only one store carry through as-is.
	- `revoked_kids`: union.

	Notably, primary's `roles_by_namespace` does NOT shadow
	secondary's at the namespace level; the role lists union.  This
	preserves additive trust composition while keeping key-identity
	conflicts deterministic (primary wins).
	"""
	keys = dict(secondary.keys_by_kid)
	keys.update(primary.keys_by_kid)

	merged_ns: dict[str, NamespaceRoles] = {}
	all_namespaces = set(primary.roles_by_namespace) | set(secondary.roles_by_namespace)
	for ns in all_namespaces:
		p = primary.roles_by_namespace.get(ns)
		s = secondary.roles_by_namespace.get(ns)
		authors = (p.authors if p else frozenset()) | (s.authors if s else frozenset())
		certifiers = (p.certifiers if p else frozenset()) | (s.certifiers if s else frozenset())
		merged_ns[ns] = NamespaceRoles(authors=authors, certifiers=certifiers)

	revoked = primary.revoked_kids | secondary.revoked_kids
	return TrustStore(
		keys_by_kid=keys,
		roles_by_namespace=merged_ns,
		revoked_kids=revoked,
	)


def dump_trust_store_json(store: TrustStore) -> str:
	"""Serialize a TrustStore to canonical v1 JSON.

	Deterministic ordering: keys sorted by kid, namespaces sorted
	by pattern, role lists sorted.  Useful for tests and for
	`drift trust apply` writeback.
	"""
	keys_obj: dict[str, dict[str, Any]] = {}
	for kid in sorted(store.keys_by_kid):
		key = store.keys_by_kid[kid]
		entry: dict[str, Any] = {
			"algo": key.algo,
			"pubkey": base64.b64encode(key.pubkey_raw).decode("ascii"),
		}
		if key.label:
			entry["label"] = key.label
		keys_obj[kid] = entry

	ns_obj: dict[str, dict[str, list[str]]] = {}
	for ns in sorted(store.roles_by_namespace):
		roles = store.roles_by_namespace[ns]
		ns_obj[ns] = {
			"authors": sorted(roles.authors),
			"certifiers": sorted(roles.certifiers),
		}

	out = {
		"format": _FORMAT_TAG,
		"version": _FORMAT_VERSION,
		"keys": keys_obj,
		"namespaces": ns_obj,
		"revoked": sorted(store.revoked_kids),
	}
	return json.dumps(out, indent=2, ensure_ascii=False) + "\n"
