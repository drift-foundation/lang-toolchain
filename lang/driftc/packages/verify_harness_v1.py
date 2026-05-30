# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Shared v1 package-verification harness.

This is the ONE place that turns "a package's manifest + decompressed
bytes + the trust material" into "per-module verification results."
Every caller that verifies a v1 package against role-tagged trust —
the consumer load gate (`provider_v1.load_package_v1_with_policy`), the
deploy index-time gate (`resolver._load_verifier`), and the operator
CLI facade (`verify_deployed_v1.verify_deployed_package`) — funnels
through here.

Why this exists as one module: the verification *engine*
(`verify_v1.compose_verify` / `verify_package_from_sidecars`) was
already shared, but the *caller harness* around it was copy-pasted three
times: build `PackageIdentity` from the manifest stamps, enumerate the
trust-relevant module ids, route reserved (`std.*`/`lang.*`/`drift.*`)
namespaces to the core trust store, and call the engine per module with
the standalone/index-time `resolved_closure=[]` contract. Three copies
of *context setup* is as dangerous as three copies of crypto: the bugs
found in review (reserved-namespace trust routing, per-module accepted
certs collapsed into one, provenance binding) were all caller-context
drift. Centralising the setup makes that class of drift impossible.

This module does NOT make trust decisions or touch crypto: acceptance is
entirely `verify_v1`'s. It only supplies identity, modules, trust
routing, and the collected per-module results.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from lang.driftc.packages.cert_claim_v1 import ResolvedDep
from lang.driftc.packages.trust_v1 import TrustStore
from lang.driftc.packages.verify_v1 import (
	PackageIdentity,
	VerifyResult,
	verify_package_from_sidecars,
)


# ── Reserved-namespace routing (canonical home) ────────────────────
#
# Reserved namespaces verify against the toolchain-shipped core trust
# store, never a caller-supplied or synthesized one — otherwise a
# non-Foundation key could bless a `std.*` package.  This is THE
# definition; `provider_v1` re-exports it so older callers keep their
# spelling.

RESERVED_NAMESPACE_PREFIXES = ("std.", "lang.", "drift.")
RESERVED_NAMESPACE_EXACT = ("std", "lang", "drift")


def module_is_reserved(module_id: str) -> bool:
	"""True for `std`/`lang`/`drift` (exact) and any dotted child
	(`std.io`, `lang.compiler`, `drift.rpc`)."""
	if module_id in RESERVED_NAMESPACE_EXACT:
		return True
	return any(module_id.startswith(p) for p in RESERVED_NAMESPACE_PREFIXES)


# ── Identity + module enumeration from a manifest ──────────────────


def iter_trust_module_ids(manifest: dict[str, Any]) -> list[str]:
	"""Module ids that must satisfy trust for this package.

	Excludes `*.__instantiations` and other internal suffixes that
	aren't user-visible modules.  Operates on the raw manifest dict so
	every caller (LoadedPackage / decompressed bytes / on-disk dir)
	enumerates modules identically.
	"""
	modules = manifest.get("modules", [])
	out: list[str] = []
	if not isinstance(modules, list):
		return out
	for m in modules:
		if isinstance(m, dict):
			mid = m.get("module_id")
			if isinstance(mid, str) and mid and not mid.endswith(".__instantiations"):
				out.append(mid)
	return out


def build_package_identity(
	manifest: dict[str, Any], decompressed_bytes: bytes
) -> PackageIdentity:
	"""Build `PackageIdentity` from manifest stamps + sha256 of the
	decompressed payload.

	G1: `source_content_id` is read from the manifest stamp (NEVER
	recomputed from binary bytes — that would be a phantom proof);
	`artifact_sha256` is sha256 of the decompressed `.dmp` payload.
	Raises `ValueError` if the manifest lacks the required stamps.
	"""
	pkg_id = manifest.get("package_id")
	pkg_ver = manifest.get("package_version")
	sci = manifest.get("source_content_id")
	if not isinstance(pkg_id, str) or not pkg_id:
		raise ValueError("package manifest missing package_id")
	if not isinstance(pkg_ver, str) or not pkg_ver:
		raise ValueError("package manifest missing package_version")
	if not isinstance(sci, str) or not sci.startswith("sha256:"):
		raise ValueError(
			f"package manifest missing source_content_id stamp for "
			f"{pkg_id!r}@{pkg_ver!r}; v1 packages MUST carry the SCI in the "
			f"manifest so the verifier can compare stamps (G1)"
		)
	return PackageIdentity(
		package_id=pkg_id,
		version=pkg_ver,
		source_content_id=sci,
		artifact_sha256="sha256:" + hashlib.sha256(decompressed_bytes).hexdigest(),
	)


# ── Per-module verification ────────────────────────────────────────


@dataclass(frozen=True)
class ModuleVerifyResult:
	"""One module's verification outcome.

	`reserved` records which trust store the module routed to (core vs
	the caller-supplied store) so diagnostics can show it without
	re-deriving the predicate.
	"""
	module_id: str
	reserved: bool
	result: VerifyResult


def verify_package_modules(
	*,
	sidecar_dir: Path,
	identity: PackageIdentity,
	module_ids: list[str],
	trust_store: TrustStore,
	core_trust_store: TrustStore,
	resolved_closure: list[ResolvedDep],
	require_certifier: Optional[str] = None,
	require_cert_suite: Optional[str] = None,
	self_verify: bool = False,
	self_verify_sci: Optional[str] = None,
) -> list[ModuleVerifyResult]:
	"""Verify each module against the appropriate trust store.

	Reserved namespaces (`module_is_reserved`) route to
	`core_trust_store`; everything else to `trust_store`.  Callers with
	no separate core store (and no reserved modules in play) may pass
	`trust_store` for both — `core_trust_store` is consulted only for
	reserved modules.

	Runs every module (no fail-fast) and returns the full list so
	callers can collect ALL accepted certifiers / failures; a caller
	wanting fail-fast inspects `first_failure(...)`.  `verify_v1` owns
	the acceptance decision; this loop only routes trust and collects
	results.
	"""
	out: list[ModuleVerifyResult] = []
	for module_id in module_ids:
		reserved = module_is_reserved(module_id)
		trust = core_trust_store if reserved else trust_store
		res = verify_package_from_sidecars(
			sidecar_dir=sidecar_dir,
			package_identity=identity,
			module_id=module_id,
			trust=trust,
			resolved_closure=resolved_closure,
			require_certifier=require_certifier,
			require_cert_suite=require_cert_suite,
			self_verify=self_verify,
			self_verify_sci=self_verify_sci,
		)
		out.append(ModuleVerifyResult(module_id=module_id, reserved=reserved, result=res))
	return out


def first_failure(results: list[ModuleVerifyResult]) -> Optional[ModuleVerifyResult]:
	"""First module whose verification was rejected, or None if all
	passed.  Convenience for the fail-fast callers (consumer load /
	index-time gate) that raise on the first rejection."""
	for m in results:
		if not m.result.ok:
			return m
	return None
