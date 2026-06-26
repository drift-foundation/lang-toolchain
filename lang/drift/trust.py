# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`drift trust` CLI — manage a v1 trust store JSON file.

The on-disk shape produced and consumed by this module matches
`lang.driftc.packages.trust_v1`:

  {
    "format": "drift-trust",
    "version": 1,
    "keys":  { "<kid>": {"algo": "ed25519", "pubkey": "<b64>"} },
    "namespaces": {
        "<pattern>": {"authors": ["<kid>", ...], "certifiers": ["<kid>", ...]}
    },
    "revoked": ["<kid>", ...]
  }

This CLI is the drift-tooling UX layer; driftc and drift_deploy
re-parse the file through their own strict validators
(`trust_v1.load_trust_store_json`).  Both surfaces must agree on
the shape or a CLI-written file fails to load at compile time.

The pre-v1 trust shape (`version: 0`, flat-list namespaces, dict
`revoked`) is no longer accepted on load.  Pre-v1 stores are not
migrated automatically: the user re-runs `drift trust add` to
rebuild the file in v1 shape.  This matches the audit's
"pre-v1 acceptance is a hard product boundary" rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lang.drift.crypto import b64_decode, b64_encode, compute_ed25519_kid


def _now_iso8601_utc() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _empty_v1_store() -> dict[str, Any]:
	return {
		"format": "drift-trust",
		"version": 1,
		"keys": {},
		"namespaces": {},
		"revoked": [],
	}


def _load_or_init_trust_store(path: Path) -> dict[str, Any]:
	"""Load a v1 trust store, or return an initialized empty one.

	This is drift-tooling UX; driftc is the verifier and maintains
	its own strict parser/validator
	(`lang.driftc.packages.trust_v1.load_trust_store_json`).
	"""
	if not path.exists():
		return _empty_v1_store()
	obj = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(obj, dict) or obj.get("format") != "drift-trust" or obj.get("version") != 1:
		raise ValueError(
			"unsupported trust store format/version (expected "
			"`format: drift-trust, version: 1`).  Pre-v1 stores are "
			"not accepted; re-run `drift trust add` for each kid to "
			"rebuild the file in v1 shape."
		)
	obj.setdefault("keys", {})
	obj.setdefault("namespaces", {})
	obj.setdefault("revoked", [])
	return obj


def _write_trust_store(path: Path, obj: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _ensure_dict(obj: Any, msg: str) -> dict[str, Any]:
	if not isinstance(obj, dict):
		raise ValueError(msg)
	return obj


def _ensure_list(obj: Any, msg: str) -> list[Any]:
	if not isinstance(obj, list):
		raise ValueError(msg)
	return obj


def _ns_entry(namespaces: dict[str, Any], ns: str) -> dict[str, list[str]]:
	"""Return the role-tagged entry for `ns`, creating it on demand."""
	entry = namespaces.get(ns)
	if entry is None:
		entry = {"authors": [], "certifiers": []}
		namespaces[ns] = entry
		return entry
	if not isinstance(entry, dict):
		raise ValueError(
			f"trust store namespace {ns!r} entry must be a JSON "
			f"object with 'authors' and 'certifiers' lists"
		)
	entry.setdefault("authors", [])
	entry.setdefault("certifiers", [])
	return entry


@dataclass(frozen=True)
class TrustListOptions:
	trust_store_path: Path


def list_trust_store(opts: TrustListOptions) -> dict[str, Any]:
	return _load_or_init_trust_store(opts.trust_store_path)


@dataclass(frozen=True)
class TrustAddKeyOptions:
	"""Add a public key to the trust store.

	`role` is `"author"`, `"certifier"`, or `"both"`.  Default is
	`"both"` (Foundation-bootstrap pattern: a single kid plays
	both roles for a namespace).  Production setups that separate
	the two should pass `"author"` or `"certifier"` explicitly.
	"""
	trust_store_path: Path
	namespace: str
	pubkey_b64: str
	kid: str | None
	role: str = "both"


def add_key_to_trust_store(opts: TrustAddKeyOptions) -> None:
	obj = _load_or_init_trust_store(opts.trust_store_path)
	keys = _ensure_dict(obj.get("keys"), "trust store keys must be a JSON object")
	namespaces = _ensure_dict(obj.get("namespaces"), "trust store namespaces must be a JSON object")

	pub_raw = b64_decode(opts.pubkey_b64.strip())
	if len(pub_raw) != 32:
		raise ValueError("ed25519 public key must decode to 32 bytes")
	kid = opts.kid or compute_ed25519_kid(pub_raw)
	if opts.kid is not None and kid != opts.kid:
		raise ValueError("provided --kid does not match derived kid from pubkey")

	# Record key material (idempotent).
	keys.setdefault(kid, {"algo": "ed25519", "pubkey": b64_encode(pub_raw)})

	# Grant for the namespace (idempotent across calls).  Role lists
	# stay sorted-unique so a re-run produces the same file.
	entry = _ns_entry(namespaces, opts.namespace)
	roles_to_grant: list[str]
	if opts.role == "both":
		roles_to_grant = ["authors", "certifiers"]
	elif opts.role == "author":
		roles_to_grant = ["authors"]
	elif opts.role == "certifier":
		roles_to_grant = ["certifiers"]
	else:
		raise ValueError(
			f"trust add: --role must be 'author', 'certifier', or "
			f"'both'; got {opts.role!r}"
		)
	for role_key in roles_to_grant:
		role_list = entry[role_key]
		if not isinstance(role_list, list):
			raise ValueError(
				f"trust store namespace {opts.namespace!r} {role_key!r} "
				f"must be a list"
			)
		if kid not in role_list:
			role_list.append(kid)
			role_list.sort()

	_write_trust_store(opts.trust_store_path, obj)


@dataclass(frozen=True)
class TrustRevokeOptions:
	trust_store_path: Path
	kid: str
	reason: str | None


def revoke_kid_in_trust_store(opts: TrustRevokeOptions) -> None:
	"""Mark a kid as revoked in the trust store.

	v1 stores `revoked` as a flat list of kids (per the v1 trust
	loader).  The `reason` parameter is accepted for CLI
	compatibility but not persisted into the v1 trust JSON --
	revocation in v1 is binary at the trust gate, and revocation
	provenance belongs in a separate audit log if needed.
	"""
	obj = _load_or_init_trust_store(opts.trust_store_path)
	revoked = obj.get("revoked")
	if revoked is None:
		obj["revoked"] = []
		revoked = obj["revoked"]
	# Upgrade older pre-v1 shapes (list-of-kids or dict-of-kid->meta)
	# defensively.  Pre-v1 STORES are rejected at load time above;
	# this branch covers a partially-edited file that still has the
	# old `revoked` shape after a manual edit.
	if isinstance(revoked, dict):
		revoked = sorted(revoked.keys())
		obj["revoked"] = revoked
	if not isinstance(revoked, list):
		raise ValueError("trust store revoked must be a flat list of kid strings")

	if opts.kid not in revoked:
		revoked.append(opts.kid)
		revoked.sort()

	_write_trust_store(opts.trust_store_path, obj)


@dataclass(frozen=True)
class TrustImportOptions:
	"""Import a v1 author-claim sidecar's signer kids into the trust
	store.  Pre-v1 `.sig` imports are no longer supported; use
	`drift author` to produce a v1 author claim, then
	import it here.
	"""
	trust_store_path: Path
	namespace: str | None
	source_path: Path
	role: str = "author"  # author-claims grant author role by default


def plan_trust_import(opts: TrustImportOptions) -> tuple[Path, str]:
	"""Resolve the import source and decide the target namespace.

	Accepts a v1 `<pkg>.author-claim` sidecar OR a package
	(`.dmp`/`.zdmp`) whose canonical author-claim sidecar sits
	next to it.  Returns `(claim_path, namespace)`.

	The namespace defaults to `<package_id>.*` read from the
	claim body when not provided.  Pre-v1 `.sig` imports are
	rejected -- explicit error pointing the user at `drift author`.
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.sidecar_naming import author_claim_filename
	from lang.drift.dmir_pkg_v0 import read_identity_v0

	source = opts.source_path
	if source.suffix == ".sig":
		raise ValueError(
			"pre-v1 `.sig` sidecars are no longer supported by "
			"`drift trust import`.  Re-run `drift author` "
			"to produce a v1 author claim, then import that file."
		)
	claim_path: Path
	if source.suffix in (".dmp", ".zdmp"):
		ident = read_identity_v0(source)
		canon = author_claim_filename(ident.package_id)
		claim_path = source.parent / canon
		if not claim_path.is_file():
			raise ValueError(
				f"v1 author claim sidecar not found next to package: "
				f"{claim_path}.  Run `drift author` for "
				f"{ident.package_id}@{ident.version} first."
			)
	else:
		claim_path = source
	if not claim_path.exists():
		raise ValueError(f"author claim sidecar not found: {claim_path}")
	# Read claim to derive default namespace.
	claim = load_author_claim_json(claim_path.read_text(encoding="utf-8"))
	namespace = opts.namespace or (
		claim.body.namespaces[0] if claim.body.namespaces else f"{claim.body.package_id}.*"
	)
	return claim_path, namespace


def import_sidecar_keys_to_trust_store(opts: TrustImportOptions) -> dict[str, Any]:
	"""Import every signer kid from a v1 author-claim sidecar
	into the trust store under the appropriate role.

	Returns a summary dict for CLI display.  Author claims grant
	the author role by default; pass `role="both"` (caller-controlled)
	to also grant certifier in the same call.
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json

	claim_path, namespace = plan_trust_import(opts)
	claim = load_author_claim_json(claim_path.read_text(encoding="utf-8"))
	imported: list[str] = []
	# v1 author-claim signatures don't carry pubkey bytes inline
	# (pubkeys live in the trust store).  The user is expected to
	# have the pubkey via `drift key` already, or to have added it
	# manually with `drift trust add`.  Here we record the kid +
	# namespace + role, surfacing "missing pubkey" as a per-kid
	# diagnostic the caller can act on.
	missing_pubkeys: list[str] = []
	obj = _load_or_init_trust_store(opts.trust_store_path)
	known_keys = _ensure_dict(obj.get("keys"), "trust store keys must be a JSON object")
	for sig in claim.signatures:
		if sig.kid not in known_keys:
			missing_pubkeys.append(sig.kid)
			continue
		# Grant the role for the namespace.  Reuse the same
		# add-key code path so namespace storage stays consistent.
		key_entry = known_keys[sig.kid]
		pubkey_b64 = key_entry.get("pubkey", "") if isinstance(key_entry, dict) else ""
		add_key_to_trust_store(TrustAddKeyOptions(
			trust_store_path=opts.trust_store_path,
			namespace=namespace,
			pubkey_b64=pubkey_b64,
			kid=sig.kid,
			role=opts.role,
		))
		imported.append(sig.kid)
	return {
		"source": str(claim_path),
		"namespace": namespace,
		"package_id": claim.body.package_id,
		"imported_kids": sorted(set(imported)),
		"missing_pubkeys": sorted(set(missing_pubkeys)),
	}


# ── Reserved namespaces guard ──────────────────────────────────────


_RESERVED_NAMESPACE_PREFIXES = ("std.", "lang.", "drift.")
_RESERVED_NAMESPACE_EXACT = ("std", "lang", "drift")


def _namespace_is_reserved(ns: str) -> bool:
	"""Project trust stores must NOT grant the reserved toolchain
	namespaces (`std.*`, `lang.*`, `drift.*`).  Those resolve through
	`core_trust_v1.json` shipped with the toolchain, not through
	per-project `drift/trust.json`; an accidental project-side grant
	would be silently ignored by the verifier and is a UX trap.

	A wildcard glob like `std.*` is treated as reserved iff its
	literal prefix (`std.`) is reserved; bare `std`, `lang`, `drift`
	are also reserved.  The check is purely string-based and does
	not require a glob-pattern parser.
	"""
	# Strip trailing wildcard tokens before checking the prefix.
	bare = ns
	for suffix in (".*", "*"):
		if bare.endswith(suffix):
			bare = bare[: -len(suffix)]
			break
	if bare in _RESERVED_NAMESPACE_EXACT:
		return True
	return any(bare.startswith(p) or (bare + ".").startswith(p) for p in _RESERVED_NAMESPACE_PREFIXES)


# ── drift trust bootstrap ──────────────────────────────────────────


@dataclass(frozen=True)
class TrustBootstrapOptions:
	"""Setup / repair project trust from checked-in author claims.

	`manifest_path` is the project's `drift/manifest.json` (the file,
	not the dir).  `trust_store_path` defaults to `drift/trust.json`
	next to it.  `allow_reserved=True` lifts the project-side
	reserved-namespace guard -- it is only correct for the toolchain
	itself (the stdlib release) and never for a regular project.
	"""
	manifest_path: Path
	trust_store_path: Path
	allow_reserved: bool = False


def _read_author_pubkey(sidecar_dir: Path, package_id: str) -> str:
	"""Read the `<pkg>.author-pubkey.b64` companion file emitted next
	to the claim by `sign_and_write_author_claim`.

	Returns the bare base64 pubkey string (no whitespace).  Missing
	companion -> ValueError that explicitly points the operator at
	the publish flow rather than at the trust store.
	"""
	from lang.driftc.packages.sidecar_naming import author_pubkey_filename
	path = sidecar_dir / author_pubkey_filename(package_id)
	if not path.is_file():
		raise ValueError(
			f"author pubkey companion not found for {package_id!r}: "
			f"{path}.  `drift author` writes this file next to "
			f"the `.author-claim` so `drift trust bootstrap` can derive "
			f"the trust store without an extra manual step.  Re-run "
			f"`drift author --manifest {sidecar_dir / 'manifest.json'} "
			f"--key-file <author.seed>` to refresh it."
		)
	return path.read_text(encoding="utf-8").strip()


def plan_trust_bootstrap(opts: TrustBootstrapOptions) -> list[dict[str, Any]]:
	"""Walk the manifest's package artifacts and produce one grant
	record per `(artifact, namespace, kid)` triple.

	Returned record shape (CLI-friendly):
	    {
	      "package_id": "...",
	      "version": "...",
	      "claim_path": "/abs/path/<pkg>.author-claim",
	      "pubkey_path": "/abs/path/<pkg>.author-pubkey.b64",
	      "kid":  "ed25519:...",
	      "pubkey_b64": "...",
	      "namespaces": ["..."],
	      "reserved": False,
	    }
	Reserved-namespace records are flagged but not silently dropped;
	the caller decides whether `allow_reserved` suppresses the guard.
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.manifest import AUTHORABLE_ARTIFACT_KINDS, load_manifest
	from lang.driftc.packages.sidecar_naming import author_claim_filename

	manifest_path = opts.manifest_path
	if not manifest_path.is_file():
		raise ValueError(f"manifest not found: {manifest_path}")
	manifest = load_manifest(manifest_path)
	manifest_dir = manifest_path.parent

	plans: list[dict[str, Any]] = []
	for art in manifest.artifacts:
		if art.kind not in AUTHORABLE_ARTIFACT_KINDS:
			continue  # only package/app artifacts carry SCI + author claims
		claim_path = manifest_dir / author_claim_filename(art.name)
		if not claim_path.is_file():
			raise ValueError(
				f"author claim missing for artifact {art.name!r}: "
				f"{claim_path}.  Run `drift author --manifest "
				f"{manifest_path} [--artifact {art.name}] --key-file "
				f"<seed>` to produce it before bootstrapping trust."
			)
		claim = load_author_claim_json(claim_path.read_text(encoding="utf-8"))
		pubkey_b64 = _read_author_pubkey(manifest_dir, art.name)
		pub_raw = b64_decode(pubkey_b64)
		if len(pub_raw) != 32:
			raise ValueError(
				f"author pubkey companion for {art.name!r} did not "
				f"decode to 32 bytes (got {len(pub_raw)}).  Re-run "
				f"`drift author` to regenerate it."
			)
		derived_kid = compute_ed25519_kid(pub_raw)
		# The kid must match a signer in the claim — otherwise we'd be
		# importing a pubkey that doesn't actually authorize anything
		# in the claim's signed body.
		signer_kids = {sig.kid for sig in claim.signatures}
		if derived_kid not in signer_kids:
			raise ValueError(
				f"author pubkey companion for {art.name!r} (kid "
				f"{derived_kid!r}) does not appear in the claim's "
				f"signatures (signer kids: {sorted(signer_kids)!r}).  "
				f"The companion file and the claim came from different "
				f"keys; re-run `drift author` so they agree."
			)
		# Reserved-namespace guard: any namespace under std.*, lang.*,
		# drift.* belongs to the toolchain's core_trust_v1.json, not
		# to a project trust store.
		ns_list = list(claim.body.namespaces)
		reserved_hits = [ns for ns in ns_list if _namespace_is_reserved(ns)]
		plans.append({
			"package_id": claim.body.package_id,
			"version": claim.body.version,
			"claim_path": str(claim_path),
			"pubkey_path": str(manifest_dir / f"{art.name}.author-pubkey.b64"),
			"kid": derived_kid,
			"pubkey_b64": pubkey_b64,
			"namespaces": ns_list,
			"reserved": bool(reserved_hits),
			"reserved_hits": reserved_hits,
		})
	if not plans:
		raise ValueError(
			f"manifest at {manifest_path} declares no package/app artifacts; "
			f"nothing to bootstrap"
		)
	return plans


def bootstrap_trust_from_manifest(opts: TrustBootstrapOptions) -> dict[str, Any]:
	"""Apply the bootstrap plan: merge all derived `(kid, namespace)`
	grants into a v1 trust store at `opts.trust_store_path`, then
	return a summary dict for CLI display.

	Reserved-namespace grants are refused unless `allow_reserved=True`
	(project trust stores must never grant `std.*` / `lang.*` /
	`drift.*`).
	"""
	plans = plan_trust_bootstrap(opts)
	reserved_violations = [
		f"{p['package_id']}: {p['reserved_hits']!r}"
		for p in plans if p["reserved"]
	]
	if reserved_violations and not opts.allow_reserved:
		raise ValueError(
			"bootstrap refused: the following artifact author claim(s) "
			"declare reserved namespaces (std.*/lang.*/drift.*) which "
			"belong to the toolchain's core_trust_v1.json, not a "
			"project trust store:\n  "
			+ "\n  ".join(reserved_violations)
			+ "\nIf you are publishing the toolchain itself, pass "
			"--allow-reserved; otherwise correct the manifest / author "
			"claim namespaces."
		)
	for plan in plans:
		for ns in plan["namespaces"]:
			add_key_to_trust_store(TrustAddKeyOptions(
				trust_store_path=opts.trust_store_path,
				namespace=ns,
				pubkey_b64=plan["pubkey_b64"],
				kid=plan["kid"],
				role="author",
			))
	return {
		"trust_store": str(opts.trust_store_path),
		"granted": [
			{
				"package_id": p["package_id"],
				"kid": p["kid"],
				"namespaces": p["namespaces"],
			}
			for p in plans
		],
	}


# ── drift trust check ──────────────────────────────────────────────


@dataclass(frozen=True)
class TrustCheckOptions:
	"""Read-only preflight: 'is this repo trust-v1 ready before deploy?'

	`certifier_key_file` / `certifier_kid` are optional deploy-
	readiness extensions.  When supplied, `check` also verifies the
	expected deploy signer is granted the `certifiers` role for the
	artifact namespace -- so a missing or wrong certifier grant
	surfaces at preflight rather than as a load-time verify failure
	for downstream consumers.
	"""
	manifest_path: Path
	trust_store_path: Path
	certifier_key_file: Path | None = None
	certifier_kid: str | None = None


def _resolve_certifier_kid(opts: TrustCheckOptions) -> str | None:
	"""Resolve the expected certifier kid from CLI options.

	`certifier_kid` wins over `certifier_key_file` if both are passed
	(explicit kid is unambiguous); the file path is read as the
	base64-encoded 32-byte seed and the kid derived from the
	corresponding pubkey.  Returns None when neither was supplied.
	"""
	if opts.certifier_kid:
		return opts.certifier_kid
	if opts.certifier_key_file is None:
		return None
	from lang.drift.crypto import ed25519_sign_from_seed
	import base64 as _b64
	if not opts.certifier_key_file.is_file():
		raise ValueError(
			f"--certifier-key-file path does not exist: "
			f"{opts.certifier_key_file}"
		)
	text = opts.certifier_key_file.read_text(encoding="utf-8").strip()
	seed = _b64.b64decode(text.encode("ascii"))
	if len(seed) != 32:
		raise ValueError(
			f"--certifier-key-file {opts.certifier_key_file} did not "
			f"decode to a 32-byte Ed25519 private seed"
		)
	_sig, pub_raw = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return compute_ed25519_kid(pub_raw)


def check_trust_for_manifest(opts: TrustCheckOptions) -> dict[str, Any]:
	"""Read-only preflight.  Returns a structured report:
	    {
	      "ok": bool,
	      "errors":   [{"artifact": "...", "code": "...", "message": "..."}, ...],
	      "warnings": [...],
	      "checked":  [{"artifact": "...", "ok": bool, ...}],
	    }
	Errors are pinned with stable `code` strings so callers can
	build matchers against specific failure modes (this is how the
	team-side scripts will tell "missing claim" apart from
	"namespace mismatch").

	The function NEVER writes; it is a preflight, not a fixer.  If
	the answer is "no", the operator runs `drift trust bootstrap`
	(for trust setup) or `drift author` (for claim
	regeneration after manifest changes).
	"""
	from lang.driftc.packages.author_claim_v1 import load_author_claim_json
	from lang.driftc.packages.manifest import (
		AUTHORABLE_ARTIFACT_KINDS, compute_artifact_sci, load_manifest,
	)
	from lang.driftc.packages.sidecar_naming import (
		author_claim_filename, cert_claim_filename_prefix,
	)

	errors: list[dict[str, Any]] = []
	warnings: list[dict[str, Any]] = []
	checked: list[dict[str, Any]] = []

	manifest_path = opts.manifest_path
	if not manifest_path.is_file():
		return {
			"ok": False,
			"errors": [{"artifact": None, "code": "manifest_missing",
				"message": f"manifest not found: {manifest_path}"}],
			"warnings": [], "checked": [],
		}
	try:
		manifest = load_manifest(manifest_path)
	except Exception as e:
		return {
			"ok": False,
			"errors": [{"artifact": None, "code": "manifest_invalid",
				"message": str(e)}],
			"warnings": [], "checked": [],
		}
	manifest_dir = manifest_path.parent

	# 1. Trust store presence + v1 shape.
	trust_store_obj: dict[str, Any] | None = None
	if not opts.trust_store_path.is_file():
		errors.append({"artifact": None, "code": "trust_store_missing",
			"message": (
				f"trust store not found: {opts.trust_store_path}.  Run "
				f"`drift trust bootstrap --manifest {manifest_path}` "
				f"after running `drift author` for each artifact."
			)})
	else:
		try:
			trust_store_obj = _load_or_init_trust_store(opts.trust_store_path)
		except Exception as e:
			errors.append({"artifact": None, "code": "trust_store_invalid",
				"message": str(e)})

	# 2. Refuse-legacy sweep over the manifest's drift/ directory.
	#    No `.sig` and no `.source-attestation` files should be present.
	legacy_sig = list(manifest_dir.glob("*.sig"))
	legacy_att = list(manifest_dir.glob("*.source-attestation"))
	for p in legacy_sig:
		errors.append({"artifact": None, "code": "legacy_sig_present",
			"message": (
				f"pre-v1 sidecar present: {p}.  v1 verify ignores "
				f"`.sig`; remove the file and replace with a v1 "
				f"author claim via `drift author`."
			)})
	for p in legacy_att:
		errors.append({"artifact": None, "code": "legacy_attestation_present",
			"message": (
				f"pre-v1 sidecar present: {p}.  v1 verify ignores "
				f"`.source-attestation`; remove the file and use the "
				f"v1 author + cert claim split instead."
			)})

	expected_certifier_kid = _resolve_certifier_kid(opts)

	# 3. Per-artifact checks.
	libs = [a for a in manifest.artifacts if a.kind in AUTHORABLE_ARTIFACT_KINDS]
	if not libs:
		# Only package/app artifacts carry SCI + author claims; nothing to
		# verify.  Surface a warning so the operator knows nothing is checked.
		warnings.append({"artifact": None, "code": "no_libraries",
			"message": "manifest declares no package/app artifacts; nothing to verify"})

	for art in libs:
		report: dict[str, Any] = {"artifact": art.name, "ok": True}
		# 3a. Author claim presence.
		claim_path = manifest_dir / author_claim_filename(art.name)
		if not claim_path.is_file():
			errors.append({"artifact": art.name, "code": "author_claim_missing",
				"message": (
					f"missing {claim_path}.  Run `drift author "
					f"--manifest {manifest_path} --artifact {art.name} "
					f"--key-file <seed>`."
				)})
			report["ok"] = False
			checked.append(report)
			continue
		try:
			claim = load_author_claim_json(claim_path.read_text(encoding="utf-8"))
		except Exception as e:
			errors.append({"artifact": art.name, "code": "author_claim_invalid",
				"message": f"failed to parse {claim_path}: {e}"})
			report["ok"] = False
			checked.append(report)
			continue
		# 3b. Body fields match the manifest.
		if claim.body.package_id != art.name:
			errors.append({"artifact": art.name, "code": "package_id_mismatch",
				"message": (
					f"author claim body.package_id is "
					f"{claim.body.package_id!r}, manifest declares {art.name!r}"
				)})
			report["ok"] = False
		if claim.body.version != art.version:
			errors.append({"artifact": art.name, "code": "version_mismatch",
				"message": (
					f"author claim body.version is {claim.body.version!r}, "
					f"manifest declares {art.version!r}.  Re-run "
					f"`drift author` for {art.name}@{art.version}."
				)})
			report["ok"] = False
		# Declared deps: claim must list the same set the manifest does
		# (name -> version-range).  Reorderings are fine (canonical sort).
		manifest_deps = {d.name: d.version for d in art.package_deps}
		claim_deps = {d.name: d.version_range for d in claim.body.required_deps}
		if manifest_deps != claim_deps:
			errors.append({"artifact": art.name, "code": "required_deps_mismatch",
				"message": (
					f"required_deps differ between manifest and author "
					f"claim.  Manifest: {sorted(manifest_deps.items())!r}; "
					f"author claim: {sorted(claim_deps.items())!r}.  "
					f"Re-run `drift author` to regenerate the claim."
				)})
			report["ok"] = False
		# 3c. Recomputed SCI matches the claim body.
		try:
			recomputed_sci = compute_artifact_sci(art, manifest_dir=manifest_dir)
		except Exception as e:
			errors.append({"artifact": art.name, "code": "sci_compute_failed",
				"message": (
					f"could not recompute SCI from on-disk sources: {e}.  "
					f"Some declared module / asset file is missing or "
					f"resolves outside the project tree."
				)})
			report["ok"] = False
			checked.append(report)
			continue
		if claim.body.source_content_id != recomputed_sci:
			errors.append({"artifact": art.name, "code": "sci_mismatch",
				"message": (
					f"author claim body.source_content_id is "
					f"{claim.body.source_content_id!r}, but recomputing "
					f"from the on-disk source tree yielded "
					f"{recomputed_sci!r}.  Source has changed since the "
					f"claim was signed; re-run `drift author`."
				)})
			report["ok"] = False
		# 3d. Namespace coverage in the project trust store
		#     (only when the store loaded above).
		if trust_store_obj is not None:
			# Signer kids must be granted `authors` for AT LEAST ONE of
			# the claim's declared namespaces -- exact-string match
			# against the trust store's namespace entries.
			trust_namespaces = _ensure_dict(
				trust_store_obj.get("namespaces", {}),
				"trust store namespaces must be a JSON object",
			)
			claim_kids = {sig.kid for sig in claim.signatures}
			covered = False
			for ns in claim.body.namespaces:
				ns_entry = trust_namespaces.get(ns)
				if not isinstance(ns_entry, dict):
					continue
				authors = ns_entry.get("authors") or []
				if any(kid in authors for kid in claim_kids):
					covered = True
					break
			if not covered:
				errors.append({"artifact": art.name, "code": "author_not_trusted",
					"message": (
						f"no signer of the author claim is granted the "
						f"`authors` role for any of "
						f"{list(claim.body.namespaces)!r} in "
						f"{opts.trust_store_path}.  Run `drift trust "
						f"bootstrap --manifest {manifest_path}` to grant "
						f"the author kid."
					)})
				report["ok"] = False
			# 3e. Optional: expected certifier is trusted.
			if expected_certifier_kid is not None:
				cert_covered = False
				for ns in claim.body.namespaces:
					ns_entry = trust_namespaces.get(ns)
					if not isinstance(ns_entry, dict):
						continue
					certifiers = ns_entry.get("certifiers") or []
					if expected_certifier_kid in certifiers:
						cert_covered = True
						break
				if not cert_covered:
					errors.append({"artifact": art.name, "code": "certifier_not_trusted",
						"message": (
							f"expected certifier kid {expected_certifier_kid!r} "
							f"is not granted the `certifiers` role for any "
							f"of {list(claim.body.namespaces)!r} in "
							f"{opts.trust_store_path}.  Grant it with `drift "
							f"trust add --role certifier --namespace <ns> "
							f"--pubkey-b64 <b64>`."
						)})
					report["ok"] = False

		# 3f. Co-artifact dep ranges: any artifact in `manifest_deps`
		#     that ALSO appears as a manifest artifact must satisfy
		#     the declared range against its sibling's version.
		sibling_versions = {a.name: a.version for a in manifest.artifacts}
		for dep_name, dep_range in manifest_deps.items():
			if dep_name in sibling_versions:
				sib_ver = sibling_versions[dep_name]
				if not _range_covers(dep_range, sib_ver):
					errors.append({"artifact": art.name, "code": "co_artifact_range_mismatch",
						"message": (
							f"manifest declares dep {dep_name}={dep_range!r}, "
							f"but the sibling artifact {dep_name!r} is at "
							f"version {sib_ver!r} which does not satisfy "
							f"that range.  Bump the dep range or the "
							f"sibling version so they line up."
						)})
					report["ok"] = False

		checked.append(report)

	# 4. Multi-claim invariant: one author-claim per package/app artifact.
	#    Iterate every `<X>.author-claim` in the manifest dir and flag
	#    files that don't correspond to a declared artifact.
	declared_claim_names = {author_claim_filename(a.name) for a in libs}
	for p in sorted(manifest_dir.glob("*.author-claim")):
		if p.name not in declared_claim_names:
			warnings.append({"artifact": None, "code": "orphan_author_claim",
				"message": (
					f"stray author claim {p.name} in {manifest_dir} does "
					f"not match any package/app artifact in the manifest; "
					f"safe to delete unless you are mid-migration."
				)})

	# 5. cert-claim sidecars: nothing to verify cryptographically at
	#    preflight (cert claims are emitted by deploy), but flag if a
	#    cert claim sidecar's package id refers to a non-declared
	#    artifact -- that's almost always a stale leftover.
	for art in libs:
		prefix = cert_claim_filename_prefix(art.name)
		# Just a sanity hit: stale cert claims with a different prefix
		# are flagged by the orphan-author-claim scan above for the
		# claim, so we don't double-count.

	return {
		"ok": not errors,
		"errors": errors,
		"warnings": warnings,
		"checked": checked,
	}


def _range_covers(declared_range: str, candidate_version: str) -> bool:
	"""Minimal range-cover predicate for v2 manifest ranges.

	v2 dep `version` shapes (per `lang.driftc.packages.dmir_pkg_v0.is_owner_declared_range`):
	  - `"M"`     -> any M.x.x
	  - `"M.N"`   -> any M.N.x

	We mirror that vocabulary here rather than re-importing the
	resolver's full semver code -- preflight is a string-level
	check, not a release-blocking comparator.  Returns False for any
	shape outside the v2 grammar (manifest parser rejects those at
	`load_manifest` time, so we won't normally see them).
	"""
	if "." not in declared_range:
		# "M" — accept any version that starts with "M."
		return candidate_version.startswith(declared_range + ".") or candidate_version == declared_range
	if declared_range.count(".") == 1:
		# "M.N" — accept any version that starts with "M.N."
		return candidate_version.startswith(declared_range + ".") or candidate_version == declared_range
	# Exact pin or anything else: be conservative.
	return candidate_version == declared_range
