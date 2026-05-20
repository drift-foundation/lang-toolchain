# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Package provider (v1) — discovers, loads, and verifies packages
via the trust-v1 model.

  - Package format (`.dmp` / `.zdmp` bytes) is UNCHANGED.  This
    module reuses `dmir_pkg_v0`'s loader and `package_validate`'s
    interface checks.
  - Trust binding is carried by `<pkg>.author-claim` +
    `<pkg>.cert-claim.<kid>.json` sidecars.  Verification flows
    through `verify_v1.compose_verify` per module.

Why per-module verification: role-tagged trust maps
`module_id → {authors, certifiers}` per namespace, and a package
MAY declare modules across multiple namespaces.  Author + cert
sidecars are per-package (not per-module), but the consumer must
re-check that each declared module's namespace is covered by the
same sidecars.

Pre-v1 acceptance is a hard product boundary.  There is no
untagged-trust fallback and no role-agnostic kid lookup.  If a
package on disk lacks v1 sidecars, loading fails (unless its
filesystem location is explicitly allow-listed by the policy as
an unverified dev root).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lang.driftc.packages.cert_claim_v1 import ResolvedDep
from lang.driftc.packages.dmir_pkg_v0 import (
	LoadedPackage,
	load_dmir_pkg_v0,
	load_dmir_pkg_v0_from_bytes,
)
from lang.driftc.packages.package_validate import (
	collect_external_exports,
	validate_package_interfaces,
)
from lang.driftc.packages.trust_v1 import TrustStore
from lang.driftc.packages.verify_v1 import (
	PackageIdentity,
	VerifyResult,
	verify_package_from_sidecars,
)


__all__ = [
	"PackageTrustPolicy",
	"PackageIdentity",
	"VerifyResult",
	"ResolvedDep",
	"collect_external_exports",
	"discover_package_files",
	"load_package_v1",
	"load_package_v1_with_policy",
]


# ── Discovery ──────────────────────────────────────────────────────


def discover_package_files(package_roots: list[Path]) -> list[Path]:
	"""Discover package artifacts under package roots.

	Accepts both `.zdmp` (compressed) and `.dmp` (uncompressed)
	files.  When both exist for the same stem in the same directory,
	`.zdmp` takes priority; the `.dmp` is excluded from the result.

	Deterministic sort order; `followlinks=True` so symlinked
	directories (as created by `drift deploy`'s staged build roots)
	are traversed correctly.
	"""
	out: set[Path] = set()
	for root in package_roots:
		if not root.exists():
			continue
		if root.is_file():
			if root.suffix in (".zdmp", ".dmp"):
				out.add(root)
			continue
		for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
			for fname in filenames:
				if fname.endswith(".zdmp") or fname.endswith(".dmp"):
					out.add(Path(dirpath) / fname)

	zdmp_stems: set[tuple[str, str]] = set()
	for p in out:
		if p.suffix == ".zdmp":
			zdmp_stems.add((str(p.parent), p.stem))
	out = {
		p for p in out
		if p.suffix != ".dmp" or (str(p.parent), p.stem) not in zdmp_stems
	}
	return sorted(out)


# ── Format-only load (no trust gate) ───────────────────────────────


def load_package_v1(path: Path) -> LoadedPackage:
	"""Load a package's bytes into a `LoadedPackage`.

	No trust gate; callers that consume packages in the compiler
	pipeline should use `load_package_v1_with_policy` instead.
	This helper exists for tests that exercise the format layer
	directly and for tooling that needs to peek at a package
	without enforcing trust.
	"""
	if path.suffix == ".zdmp":
		from lang.driftc.packages.zdmp import load_zdmp_cached
		raw_bytes = load_zdmp_cached(path, expected_sha256=None)
		return load_dmir_pkg_v0_from_bytes(raw_bytes, source_path=path)
	return load_dmir_pkg_v0(path)


def _load_package_bytes(
	path: Path, pkg_bytes: Optional[bytes],
) -> tuple[LoadedPackage, bytes, Path]:
	"""Load + decompress; return (pkg, decompressed_bytes, canonical_path).

	`canonical_path` differs from `path` only when the loader fell
	back from a corrupt `.zdmp` to its `.dmp` sibling -- callers use
	`canonical_path` for sidecar discovery so the discovery rejoins
	the actual on-disk artifact name.
	"""
	if path.suffix == ".zdmp":
		from lang.driftc.packages.zdmp import load_zdmp_cached
		try:
			import zstandard as _zstd
			_ZstdError: type = _zstd.ZstdError
		except (ImportError, AttributeError):
			_ZstdError = type(None)
		try:
			raw_bytes = load_zdmp_cached(path, expected_sha256=None)
			pkg = load_dmir_pkg_v0_from_bytes(raw_bytes, source_path=path)
			return pkg, raw_bytes, path
		except _ZstdError:
			# Narrow fallback: zstd frame is corrupt but a .dmp
			# sibling exists.  Other failures (sha mismatch, header
			# parse) propagate as real errors.
			dmp_sibling = path.with_suffix(".dmp")
			if not dmp_sibling.exists():
				raise
			pkg = load_dmir_pkg_v0(dmp_sibling)
			data = pkg_bytes if pkg_bytes is not None else dmp_sibling.read_bytes()
			return pkg, data, dmp_sibling
	pkg = load_dmir_pkg_v0(path)
	data = pkg_bytes if pkg_bytes is not None else path.read_bytes()
	return pkg, data, path


# ── Trust policy ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PackageTrustPolicy:
	"""Trust policy used when loading packages from a package root.

	v1 model:
	  - `trust_store` is the consumer's project/user trust
	    (role-tagged authors/certifiers per namespace).
	  - `core_trust_store` is the toolchain-shipped trust used for
	    reserved namespaces (`std.*`, `lang.*`, `drift.*`).  Per O2
	    it has the same role-tagged shape as `trust_store` -- there
	    is no "Foundation special case".
	  - `require_signatures` mirrors the v0 flag spelling so the CLI
	    bridge is minimal during the cutover.  In v1, silent-skip
	    is not a supported mode: when False, the policy errors at
	    load time unless the package is under `allow_unverified_roots`.
	  - `allow_unverified_roots` lists filesystem prefixes whose
	    packages may be loaded WITHOUT sidecar verification.  Used
	    for local build outputs / dev fixtures during the cutover;
	    production policies set this to `[]`.
	  - `require_certifier` / `require_cert_suite` (O7 / O4) pin a
	    specific certifier kid / cert-suite id that must accept the
	    package.  When unset, any trusted certifier path works.
	  - `self_verify` enables the consumer-rebuild acceptance path.
	    Mutually exclusive with `require_certifier`/`require_cert_suite`
	    (`verify_v1` enforces this at the API boundary).
	"""

	trust_store: TrustStore
	core_trust_store: TrustStore
	require_signatures: bool = True
	allow_unverified_roots: list[Path] = field(default_factory=list)
	require_certifier: Optional[str] = None
	require_cert_suite: Optional[str] = None
	self_verify: bool = False


# ── Verification gate ──────────────────────────────────────────────


_RESERVED_NAMESPACE_PREFIXES = ("std.", "lang.", "drift.")


def _module_is_reserved(module_id: str) -> bool:
	"""Reserved namespaces verify against `core_trust_store`.

	`std`, `lang`, `drift` (exact match) and any dotted child
	(`std.io`, `lang.compiler`, `drift.rpc`) qualify.
	"""
	if module_id in ("std", "lang", "drift"):
		return True
	return any(module_id.startswith(p) for p in _RESERVED_NAMESPACE_PREFIXES)


def _trust_for_module(policy: PackageTrustPolicy, module_id: str) -> TrustStore:
	if _module_is_reserved(module_id):
		return policy.core_trust_store
	return policy.trust_store


def _path_is_under(p: Path, root: Path) -> bool:
	try:
		p.resolve().relative_to(root.resolve())
		return True
	except (ValueError, OSError):
		return False


def _iter_trust_module_ids(pkg: LoadedPackage) -> list[str]:
	"""Return module ids that must satisfy trust for this package.

	Excludes `*.__instantiations` and other internal suffixes that
	aren't user-visible modules.
	"""
	modules = pkg.manifest.get("modules", [])
	out: list[str] = []
	if not isinstance(modules, list):
		return out
	for m in modules:
		if isinstance(m, dict):
			mid = m.get("module_id")
			if isinstance(mid, str) and mid and not mid.endswith(".__instantiations"):
				out.append(mid)
	return out


def _package_identity(pkg: LoadedPackage, decompressed_bytes: bytes) -> PackageIdentity:
	"""Build PackageIdentity from manifest stamps + computed artifact sha.

	G1: SCI is read from the manifest (NEVER recomputed from binary
	bytes in normal mode -- there is no source available here).
	"""
	pkg_id = pkg.manifest.get("package_id")
	pkg_ver = pkg.manifest.get("package_version")
	sci = pkg.manifest.get("source_content_id")
	if not isinstance(pkg_id, str) or not pkg_id:
		raise ValueError("package manifest missing package_id")
	if not isinstance(pkg_ver, str) or not pkg_ver:
		raise ValueError("package manifest missing package_version")
	if not isinstance(sci, str) or not sci.startswith("sha256:"):
		raise ValueError(
			f"package manifest missing source_content_id stamp for "
			f"{pkg_id!r}@{pkg_ver!r}; v1 packages MUST carry the SCI in "
			f"the manifest so the verifier can compare stamps (G1)"
		)
	artifact_sha = "sha256:" + hashlib.sha256(decompressed_bytes).hexdigest()
	return PackageIdentity(
		package_id=pkg_id,
		version=pkg_ver,
		source_content_id=sci,
		artifact_sha256=artifact_sha,
	)


def load_package_v1_with_policy(
	path: Path,
	*,
	policy: PackageTrustPolicy,
	pkg_bytes: Optional[bytes] = None,
	resolved_closure: Optional[list[ResolvedDep]] = None,
) -> LoadedPackage:
	"""Load a package and enforce v1 trust policy.

	Procedure:
	  1. Load + decompress the package bytes (`.zdmp` cache or `.dmp`).
	  2. Build a `PackageIdentity` from manifest stamps + sha256 of
	     the decompressed bytes.
	  3. If the package is under any `allow_unverified_roots`, skip
	     verification (transitional escape hatch for in-tree dev
	     packages that don't yet carry v1 sidecars).
	  4. Otherwise, for EACH module declared by the package, run
	     `verify_package_from_sidecars` against the appropriate
	     trust store (`core_trust_store` for reserved namespaces,
	     `trust_store` otherwise).  Any module-level rejection
	     raises ValueError.
	  5. Validate package interfaces (format-level, trust-agnostic).

	`resolved_closure` is the consumer's resolved dep closure for
	this package's own deps (used by the cert-claim dep_graph
	closure check, per O3).  Pass an empty list for leaf packages
	with no deps; the check then passes vacuously.
	"""
	pkg, data, canonical_path = _load_package_bytes(path, pkg_bytes)

	under_unverified_root = any(
		_path_is_under(canonical_path, root) for root in policy.allow_unverified_roots
	)

	# Reserved namespaces (std.*, lang.*, drift.*) MUST be verified
	# against `core_trust_store` even when the package sits inside an
	# `allow_unverified_roots` prefix.  Otherwise an unsigned drop-in
	# under `build/drift/localpkgs` (or any explicit `--allow-unsigned-from`
	# path) could provide a reserved module and shadow stdlib trust
	# entirely -- the v0 invariant ("unsigned std.* is rejected") would
	# silently weaken.  Detect reserved modules from the manifest
	# BEFORE the bypass fires, and refuse to bypass when any are
	# present.  The package then either presents v1 sidecars and
	# verifies through `core_trust_store`, or fails closed.
	reserved_modules = [
		mid for mid in _iter_trust_module_ids(pkg) if _module_is_reserved(mid)
	]
	if under_unverified_root and reserved_modules:
		raise ValueError(
			f"unsigned package is not permitted for reserved module namespace "
			f"{reserved_modules[0]!r}: the unsigned-roots bypass cannot apply "
			f"to reserved namespaces (std.*, lang.*, drift.*).  Either provide "
			f"v1 sidecars + a populated core_trust_v1.json so the package "
			f"verifies against the core trust store, or move the package out "
			f"of the unverified-roots path.  (package at {path})"
		)

	if under_unverified_root:
		# Dev / transitional path: format checks still apply, trust
		# verification is bypassed by explicit policy.  We do NOT
		# require `source_content_id` on this branch -- in-tree dev
		# packages built before SCI stamping is wired everywhere
		# would otherwise be unloadable.  Production policies leave
		# `allow_unverified_roots` empty so this branch never fires.
		validate_package_interfaces(pkg)
		return pkg

	if not policy.require_signatures:
		raise ValueError(
			"v1 trust policy: require_signatures=False is not "
			"supported.  Use allow_unverified_roots to scope an "
			"unverified bypass to specific directories."
		)

	# Fail closed on missing closure for packages with deps (O3).
	# `resolved_closure=None` is interpreted as "caller did not
	# compute a closure" -- silently substituting `[]` would mean the
	# cert-claim `dep_graph` cover check never runs, which is the
	# whole point of O3.  Callers MUST explicitly pass `[]` for leaf
	# packages with no `required_deps`.
	declared_deps = bool(getattr(pkg, "required_deps", []))
	if resolved_closure is None:
		if declared_deps:
			raise ValueError(
				f"v1 trust: package {path.name} declares required_deps "
				f"but the caller did not pass resolved_closure.  The "
				f"cert-claim dep_graph closure check (O3) cannot run "
				f"without it; refusing to load."
			)
		closure: list[ResolvedDep] = []
	else:
		closure = resolved_closure

	identity = _package_identity(pkg, data)
	sidecar_dir = canonical_path.parent

	module_ids = _iter_trust_module_ids(pkg)
	if not module_ids:
		raise ValueError(
			f"package {identity.package_id!r}@{identity.version!r} "
			f"declares no user-visible modules; refusing to accept "
			f"without at least one module to bind trust to"
		)

	for module_id in module_ids:
		trust = _trust_for_module(policy, module_id)
		result = verify_package_from_sidecars(
			sidecar_dir=sidecar_dir,
			package_identity=identity,
			module_id=module_id,
			trust=trust,
			resolved_closure=closure,
			require_certifier=policy.require_certifier,
			require_cert_suite=policy.require_cert_suite,
			self_verify=policy.self_verify,
		)
		if not result.ok:
			raise ValueError(
				f"package {identity.package_id!r}@{identity.version!r} "
				f"failed v1 trust verification for module {module_id!r}: "
				f"{result.reason}"
			)

	validate_package_interfaces(pkg)
	return pkg
