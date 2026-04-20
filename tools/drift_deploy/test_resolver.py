# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression tests for the drift deploy resolver and lock layer.

Core correctness tests:
- Constraint-aggregation + highest-satisfying-all resolution
- Conflict detection as hard build failure (no partial lock)
- Semver constraint matching
- Lock file round-trip
- --dep flag expansion
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tools.drift_deploy.lockfile import (
	expand_to_dep_flags,
	read_lock,
	verify_lock_compatibility,
	write_lock,
)
from tools.drift_deploy.resolver import (
	PackageEntry,
	ResolutionError,
	ResolvedDep,
	_read_source_attestation_meta,
	build_package_index,
	resolve_artifact,
)
from tools.drift_deploy.semver import (
	Constraint,
	SemVer,
	parse_constraint,
	parse_version,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_entry(
	pkg_id: str,
	version: str,
	sha: str = "aabbccdd",
	deps: list[tuple[str, str]] | None = None,
	author_key: str = "",
) -> PackageEntry:
	return PackageEntry(
		package_id=pkg_id,
		version=parse_version(version),
		path=Path(f"/fake/{pkg_id}-{version}.dmp"),
		sha256=sha,
		required_deps=deps or [],
		author_key=author_key,
	)


def _index(*entries: PackageEntry) -> dict[str, list[PackageEntry]]:
	idx: dict[str, list[PackageEntry]] = {}
	for e in entries:
		idx.setdefault(e.package_id, []).append(e)
	return idx


# ── SemVer parsing ───────────────────────────────────────────────────


class TestSemVer:
	def test_parse_valid(self) -> None:
		v = parse_version("1.2.3")
		assert v == SemVer(1, 2, 3)

	def test_parse_invalid(self) -> None:
		with pytest.raises(ValueError):
			parse_version("1.2")

	def test_ordering(self) -> None:
		assert parse_version("0.9.0") < parse_version("1.0.0")
		assert parse_version("1.2.3") < parse_version("1.2.4")
		assert parse_version("1.2.3") < parse_version("1.3.0")

	def test_str(self) -> None:
		assert str(parse_version("1.2.3")) == "1.2.3"


# ── Constraint matching ─────────────────────────────────────────────


class TestConstraint:
	def test_exact(self) -> None:
		c = parse_constraint("1.2.3")
		assert c.satisfies(parse_version("1.2.3"))
		assert not c.satisfies(parse_version("1.2.4"))

	def test_caret(self) -> None:
		c = parse_constraint("^1.2.3")
		assert c.satisfies(parse_version("1.2.3"))
		assert c.satisfies(parse_version("1.9.0"))
		assert not c.satisfies(parse_version("2.0.0"))
		assert not c.satisfies(parse_version("1.2.2"))

	def test_caret_zero_major(self) -> None:
		c = parse_constraint("^0.2.3")
		assert c.satisfies(parse_version("0.2.3"))
		assert c.satisfies(parse_version("0.2.9"))
		assert not c.satisfies(parse_version("0.3.0"))

	def test_tilde(self) -> None:
		c = parse_constraint("~1.2.3")
		assert c.satisfies(parse_version("1.2.3"))
		assert c.satisfies(parse_version("1.2.9"))
		assert not c.satisfies(parse_version("1.3.0"))


# ── Resolver: basic resolution ──────────────────────────────────────


class TestResolverBasic:
	def test_single_direct_dep(self) -> None:
		idx = _index(_make_entry("net.tls", "0.3.0", sha="aa"))
		result = resolve_artifact("myapp", [("net.tls", "^0.3.0")], idx)
		assert "net.tls" in result
		assert result["net.tls"].version == "0.3.0"
		assert result["net.tls"].package_id == "net.tls"
		assert result["net.tls"].dep_type == "direct"

	def test_highest_satisfying_selected(self) -> None:
		"""Multiple versions available — highest matching wins."""
		idx = _index(
			_make_entry("net.tls", "0.3.0", sha="a0"),
			_make_entry("net.tls", "0.3.1", sha="a1"),
			_make_entry("net.tls", "0.3.2", sha="a2"),
		)
		result = resolve_artifact("myapp", [("net.tls", "^0.3.0")], idx)
		assert result["net.tls"].version == "0.3.2"

	def test_transitive_dep_resolved(self) -> None:
		"""Transitive dep marked as such."""
		idx = _index(
			_make_entry("net.tls", "0.3.0", sha="aa", deps=[("acme.crypto", "^0.9.0")]),
			_make_entry("acme.crypto", "0.9.1", sha="bb"),
		)
		result = resolve_artifact("myapp", [("net.tls", "^0.3.0")], idx)
		assert result["net.tls"].dep_type == "direct"
		assert result["acme.crypto"].dep_type == "transitive"
		assert result["acme.crypto"].version == "0.9.1"

	def test_no_version_satisfies(self) -> None:
		"""No version matches constraint → ResolutionError."""
		idx = _index(_make_entry("net.tls", "0.2.0", sha="aa"))
		with pytest.raises(ResolutionError, match="not satisfied"):
			resolve_artifact("myapp", [("net.tls", "^0.3.0")], idx)

	def test_package_not_found(self) -> None:
		"""Package not in index → ResolutionError."""
		with pytest.raises(ResolutionError, match="not satisfied"):
			resolve_artifact("myapp", [("net.tls", "^0.3.0")], {})


# ── Resolver: conflict detection (core correctness) ─────────────────


class TestResolverConflict:
	"""
	Core correctness: dependency conflict = hard build failure.

	A conflicting dependency graph must abort resolution before any
	compile/link step begins. No partial lock output.
	"""

	def test_incompatible_transitive_constraints(self) -> None:
		"""
		Two direct deps require incompatible versions of a shared transitive.

		artifact 'myapp':
		  depends on net.tls ^0.3.0
		  depends on net.http ^1.0.0

		net.tls 0.3.0 depends on acme.crypto ^0.9.0   (>=0.9.0, <0.10.0)
		net.http 1.0.0 depends on acme.crypto ^1.0.0   (>=1.0.0, <2.0.0)

		These constraints on acme.crypto are incompatible → must fail.
		"""
		idx = _index(
			_make_entry("net.tls", "0.3.0", sha="aa", deps=[("acme.crypto", "^0.9.0")]),
			_make_entry("net.http", "1.0.0", sha="bb", deps=[("acme.crypto", "^1.0.0")]),
			_make_entry("acme.crypto", "0.9.5", sha="c1"),
			_make_entry("acme.crypto", "1.1.0", sha="c2"),
		)

		with pytest.raises(ResolutionError) as exc_info:
			resolve_artifact(
				"myapp",
				[("net.tls", "^0.3.0"), ("net.http", "^1.0.0")],
				idx,
			)

		err = str(exc_info.value)
		# Diagnostics must identify the conflicting package.
		assert "acme.crypto" in err
		# Diagnostics must identify constraint sources.
		assert "net.tls" in err or "net.http" in err

	def test_conflict_no_lock_output(self) -> None:
		"""
		When resolution fails for an artifact, no lock file entry is produced.

		Multi-artifact scenario: artifact A resolves fine, artifact B has
		a conflict. The lock file must contain A but NOT B.
		"""
		good_idx = _index(
			_make_entry("util.log", "1.0.0", sha="dd"),
		)
		# Resolve artifact A (succeeds).
		result_a = resolve_artifact("app-a", [("util.log", "^1.0.0")], good_idx)
		assert "util.log" in result_a

		# Resolve artifact B (fails — incompatible transitive).
		bad_idx = _index(
			_make_entry("net.tls", "0.3.0", sha="aa", deps=[("acme.crypto", "^0.9.0")]),
			_make_entry("net.http", "1.0.0", sha="bb", deps=[("acme.crypto", "^1.0.0")]),
			_make_entry("acme.crypto", "0.9.5", sha="c1"),
			_make_entry("acme.crypto", "1.1.0", sha="c2"),
		)
		with pytest.raises(ResolutionError):
			resolve_artifact(
				"app-b",
				[("net.tls", "^0.3.0"), ("net.http", "^1.0.0")],
				bad_idx,
			)

		# Only artifact A goes into the lock — B must not appear.
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			artifacts = {"app-a": result_a}
			# B is NOT added because resolution raised.
			write_lock(lock_path, artifacts)
			lock_data = json.loads(lock_path.read_text())
			assert "app-a" in lock_data["artifacts"]
			assert "app-b" not in lock_data["artifacts"]

	def test_conflict_diagnostics_show_constraint_provenance(self) -> None:
		"""Error message includes which packages impose conflicting constraints."""
		idx = _index(
			_make_entry("pkg.a", "1.0.0", sha="a1", deps=[("shared", "^1.0.0")]),
			_make_entry("pkg.b", "2.0.0", sha="b1", deps=[("shared", "^2.0.0")]),
			_make_entry("shared", "1.5.0", sha="s1"),
			_make_entry("shared", "2.0.0", sha="s2"),
		)

		with pytest.raises(ResolutionError) as exc_info:
			resolve_artifact(
				"myapp",
				[("pkg.a", "^1.0.0"), ("pkg.b", "^2.0.0")],
				idx,
			)

		err = str(exc_info.value)
		assert "shared" in err
		# Should mention both sources of conflicting constraints.
		assert "pkg.a" in err
		assert "pkg.b" in err

	def test_direct_conflict(self) -> None:
		"""Two direct deps on same package with incompatible constraints."""
		idx = _index(
			_make_entry("net.tls", "0.3.0", sha="aa"),
			_make_entry("net.tls", "1.0.0", sha="bb"),
		)
		# Exact 0.3.0 and exact 1.0.0 on same package → conflict.
		with pytest.raises(ResolutionError):
			resolve_artifact(
				"myapp",
				[("net.tls", "0.3.0"), ("net.tls", "1.0.0")],
				idx,
			)


# ── Resolver: determinism ───────────────────────────────────────────


class TestResolverDeterminism:
	def test_lexicographic_order_independent(self) -> None:
		"""Resolution is order-independent: same result regardless of dep order."""
		idx = _index(
			_make_entry("aaa", "1.0.0", sha="a1"),
			_make_entry("zzz", "1.0.0", sha="z1"),
		)
		r1 = resolve_artifact("app", [("aaa", "^1.0.0"), ("zzz", "^1.0.0")], idx)
		r2 = resolve_artifact("app", [("zzz", "^1.0.0"), ("aaa", "^1.0.0")], idx)
		assert r1 == r2


# ── Lock file ─────────────────────────────────────────────────────


class TestLockFile:
	"""Lock v4 (0.30.0+) — exact resolved artifact + source identity.

	Each entry answers two questions: "what exact artifact did I
	build against?" (version M.N.P + sha256 + author_key) AND
	"what source did the owner attest produced this artifact?"
	(source_content_id + source_attestation_key).  Verify is
	strict: any mismatch against the on-disk package or its
	`.source-attestation` sidecar is a build-time error, and
	`drift prepare` is the only sanctioned writer.  No range field,
	no file-level integrity, no silent patch float.
	"""

	def test_write_read_roundtrip_v4_exact(self) -> None:
		"""v4 round-trip preserves exact version, sha256, author_key,
		dep_type, AND the source-identity half (source_content_id +
		source_attestation_key).  No range field, no file-level
		integrity, no redundant `package_id` inside each entry."""
		_scid_a = "sha256:" + "a" * 64
		_scid_b = "sha256:" + "b" * 64
		_scid_c = "sha256:" + "c" * 64
		deps_a = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="aabbccdd", dep_type="direct",
				package_id="net.tls", author_key="ed25519:abc",
				source_content_id=_scid_a,
				source_attestation_key="ed25519:abc-src",
			),
			"acme.crypto": ResolvedDep(
				version="0.9.3", sha256="eeff0011", dep_type="transitive",
				package_id="acme.crypto", author_key="ed25519:def",
				source_content_id=_scid_b,
				source_attestation_key="ed25519:def-src",
			),
		}
		deps_b = {
			"util.log": ResolvedDep(
				version="1.0.7", sha256="22334455", dep_type="direct",
				package_id="util.log", author_key="ed25519:ghi",
				source_content_id=_scid_c,
				source_attestation_key="ed25519:ghi-src",
			),
		}

		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			write_lock(lock_path, {"app-a": deps_a, "app-b": deps_b})

			# Inspect the serialized JSON shape directly — format
			# is user-visible and part of the contract.
			raw = json.loads(lock_path.read_text(encoding="utf-8"))
			assert raw["schema_version"] == 4
			# No file-level integrity field in v4.
			assert "integrity" not in raw
			tls_entry = raw["artifacts"]["app-a"]["resolved"]["net.tls"]
			# Exact version stored, not a range.
			assert tls_entry["version"] == "0.3.15"
			assert tls_entry["sha256"] == "aabbccdd"
			assert tls_entry["author_key"] == "ed25519:abc"
			assert tls_entry["dep_type"] == "direct"
			# v4 source-identity half emitted verbatim.
			assert tls_entry["source_content_id"] == _scid_a
			assert tls_entry["source_attestation_key"] == "ed25519:abc-src"
			# No redundant `package_id` (map key already carries it)
			# and no `range` field (range lives in the manifest).
			assert "package_id" not in tls_entry
			assert "range" not in tls_entry

			result = read_lock(lock_path)

		assert set(result.keys()) == {"app-a", "app-b"}
		# Round-trip preserves both halves of the v4 identity.
		assert result["app-a"]["net.tls"].version == "0.3.15"
		assert result["app-a"]["net.tls"].sha256 == "aabbccdd"
		assert result["app-a"]["net.tls"].dep_type == "direct"
		assert result["app-a"]["net.tls"].author_key == "ed25519:abc"
		assert result["app-a"]["net.tls"].source_content_id == _scid_a
		assert result["app-a"]["net.tls"].source_attestation_key == "ed25519:abc-src"
		assert result["app-a"]["acme.crypto"].version == "0.9.3"
		assert result["app-a"]["acme.crypto"].dep_type == "transitive"
		assert result["app-a"]["acme.crypto"].source_content_id == _scid_b
		# Reader fills `package_id` from the map key for in-memory use.
		assert result["app-b"]["util.log"].package_id == "util.log"
		assert result["app-b"]["util.log"].source_content_id == _scid_c

	def test_expand_to_dep_flags_exact(self) -> None:
		"""Expanded --dep flags carry exact M.N.P.  driftc stays a flat
		exact loader — ranges never reach it."""
		resolved = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="aa", dep_type="direct",
				package_id="net.tls",
			),
			"acme.crypto": ResolvedDep(
				version="0.9.3", sha256="bb", dep_type="transitive",
				package_id="acme.crypto",
			),
		}
		flags = expand_to_dep_flags(resolved)
		# Sorted by package_id → acme.crypto first.
		assert flags == [
			"--dep", "acme.crypto@0.9.3",
			"--dep", "net.tls@0.3.15",
		]

	def test_verify_exact_match_accepted(self) -> None:
		"""Exact version + exact sha256 + signer match → accepted."""
		lock_deps = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="aabbcc", dep_type="direct",
				package_id="net.tls", author_key="ed25519:abc",
			),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.15", sha="aabbcc", author_key="ed25519:abc")],
		}
		errors = verify_lock_compatibility(lock_deps, pkg_index)
		assert errors == []

	def test_verify_patch_mismatch_rejected(self) -> None:
		"""Same package, DIFFERENT patch on disk than locked → rejected.

		Under v2 the lock stored a range and silently picked the
		highest in range; under v3 the lock is exact and any patch
		mismatch is a build-time error with a pointer to
		`drift prepare`.  Patch movement happens ONLY in prepare.
		"""
		lock_deps = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="aa", dep_type="direct",
				package_id="net.tls", author_key="ed25519:abc",
			),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.14", sha="xx", author_key="ed25519:abc")],
		}
		errors = verify_lock_compatibility(lock_deps, pkg_index)
		assert len(errors) == 1
		assert "pins version '0.3.15'" in errors[0]
		assert "0.3.14" in errors[0]
		assert "drift prepare" in errors[0]

	def test_verify_sha256_mismatch_rejected(self) -> None:
		"""Same exact version on disk but different sha256 → rejected.

		The lock records the exact sha of the .dmp the producer
		signed off on; a different sha at the same version means the
		artifact was rebuilt or replaced, which invalidates the lock.
		Reproducibility is the whole point of the exact lock.
		"""
		lock_deps = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="original_sha", dep_type="direct",
				package_id="net.tls", author_key="ed25519:abc",
			),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.15", sha="rebuilt_sha", author_key="ed25519:abc")],
		}
		errors = verify_lock_compatibility(lock_deps, pkg_index)
		assert len(errors) == 1
		assert "sha256 mismatch" in errors[0]
		assert "original_sha" in errors[0]
		assert "rebuilt_sha" in errors[0]
		assert "drift prepare" in errors[0]

	def test_verify_key_rotation_rejected(self) -> None:
		"""Signer change at the same exact version → rejected with a
		pointer to `drift prepare`.  Trust changes are not silent."""
		lock_deps = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="aa", dep_type="direct",
				package_id="net.tls", author_key="ed25519:old_key",
			),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.15", sha="aa", author_key="ed25519:new_key")],
		}
		errors = verify_lock_compatibility(lock_deps, pkg_index)
		assert len(errors) == 1
		assert "signing key changed" in errors[0]
		assert "ed25519:old_key" in errors[0]
		assert "ed25519:new_key" in errors[0]
		assert "drift prepare" in errors[0]

	def test_verify_unsigned_opt_in_skips_key_check(self) -> None:
		"""`author_key = "unsigned"` is the explicit dev opt-in: verify
		skips the signer check (and --allow-unsigned-from must be
		passed downstream).  Version and sha checks still apply."""
		lock_deps = {
			"dev.lib": ResolvedDep(
				version="0.1.0", sha256="aa", dep_type="direct",
				package_id="dev.lib", author_key="unsigned",
			),
		}
		pkg_index = {
			"dev.lib": [_make_entry("dev.lib", "0.1.0", sha="aa", author_key="")],
		}
		errors = verify_lock_compatibility(lock_deps, pkg_index)
		assert errors == []

	def test_verify_skips_co_artifact_deps(self) -> None:
		"""Co-artifact deps have no published .dmp; verify skips them.
		Their build comes from sibling sources in the same project."""
		lock_deps = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="aa", dep_type="direct",
				package_id="net.tls", author_key="ed25519:abc",
			),
			"web.jwt": ResolvedDep(
				version="0.2.0", sha256="", dep_type="co-artifact",
				package_id="web.jwt",
			),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.15", sha="aa", author_key="ed25519:abc")],
			# web.jwt NOT in index — would fail if not skipped.
		}
		errors = verify_lock_compatibility(lock_deps, pkg_index)
		assert errors == []

	def test_verify_empty_sha_in_lock_rejected_for_direct_dep(self) -> None:
		"""An in-memory `ResolvedDep(sha256="")` for a non-co-artifact
		dep must fail verify — the strict contract is "both sides
		carry a real digest and they match".  Without this, a
		programmatically-constructed lock could bypass the
		reproducibility check entirely.
		"""
		lock_deps = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="", dep_type="direct",
				package_id="net.tls", author_key="ed25519:abc",
			),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.15", sha="aa", author_key="ed25519:abc")],
		}
		errors = verify_lock_compatibility(lock_deps, pkg_index)
		assert len(errors) == 1
		assert "empty sha256 in the lock" in errors[0]
		assert "drift prepare" in errors[0]

	def test_verify_empty_sha_on_disk_rejected_for_direct_dep(self) -> None:
		"""An on-disk `PackageEntry` with empty sha256 must fail
		verify for non-co-artifact deps.  `build_package_index`
		should always compute sha256; if it didn't, that's an
		internal error and builds must not silently pass."""
		lock_deps = {
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="aa", dep_type="direct",
				package_id="net.tls", author_key="ed25519:abc",
			),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.15", sha="", author_key="ed25519:abc")],
		}
		errors = verify_lock_compatibility(lock_deps, pkg_index)
		assert len(errors) == 1
		assert "on-disk" in errors[0]
		assert "empty sha256" in errors[0]

	def test_read_lock_rejects_unknown_dep_type(self) -> None:
		"""K Finding 2 regression: a lock with a `dep_type` that is
		not one of the three recognised values must be rejected at
		load.  Unknown values cannot silently default to `direct` —
		that would let a typo or forward-compat value from a future
		schema slip past every downstream check."""
		bad_lock = {
			"schema_version": 4,
			"artifacts": {
				"app": {
					"resolved": {
						"dep.a": {
							"version": "1.2.3",
							"sha256": "aa",
							"author_key": "ed25519:x",
							"source_content_id": "sha256:" + "a"*64,
							"source_attestation_key": "ed25519:x",
							"dep_type": "vendored",  # not recognised
						},
					}
				}
			}
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			lock_path.write_text(json.dumps(bad_lock))
			with pytest.raises(ValueError) as exc:
				read_lock(lock_path)
			msg = str(exc.value)
			assert "vendored" in msg
			assert "dep_type" in msg
			assert "drift prepare" in msg

	def test_verify_rejects_co_artifact_not_in_manifest(self) -> None:
		"""K Finding 2 regression: a lock that marks an external
		dependency as `dep_type: "co-artifact"` — attempting to
		bypass sha/signer verification — must be rejected when the
		caller passes an explicit `allowed_co_artifacts` set that
		does NOT include that dep.

		Without this guard, a hand-edited lock could strip the sha
		and author_key of any external dep and escape the
		strict-exact re-check by simply flipping the dep_type field.
		"""
		lock_deps = {
			# Attacker-shaped lock entry: external dep claiming
			# co-artifact status with empty sha/author_key.
			"net.tls": ResolvedDep(
				version="0.3.15", sha256="", dep_type="co-artifact",
				package_id="net.tls", author_key="",
			),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.15",
				sha="real-sha", author_key="ed25519:legit")],
		}
		# Manifest declares no co-artifacts by that name.
		errors = verify_lock_compatibility(
			lock_deps, pkg_index,
			allowed_co_artifacts=set(),
		)
		assert len(errors) == 1
		assert "net.tls" in errors[0]
		assert "co-artifact" in errors[0]
		assert "not a co-artifact in the current manifest" in errors[0]
		assert "drift prepare" in errors[0]

	def test_verify_accepts_legit_co_artifact_in_manifest(self) -> None:
		"""Positive side of Finding 2: when the lock's co-artifact
		entry IS named in `allowed_co_artifacts`, verification still
		skips sha/signer re-check (because those are not yet known
		at prepare time for same-manifest siblings)."""
		lock_deps = {
			"web.jwt": ResolvedDep(
				version="0.2.0", sha256="", dep_type="co-artifact",
				package_id="web.jwt", author_key="",
			),
		}
		# web.jwt is not on disk — legit co-artifacts are built in
		# this deploy run, so the index lookup should be skipped.
		errors = verify_lock_compatibility(
			{}, {},
		)  # baseline: None allowlist preserves old trust-the-lock behaviour
		errors2 = verify_lock_compatibility(
			lock_deps, {},
			allowed_co_artifacts={"web.jwt", "web.rest"},
		)
		assert errors == []
		assert errors2 == []

	def test_verify_none_allowlist_preserves_old_behaviour(self) -> None:
		"""Passing `allowed_co_artifacts=None` keeps the historical
		"trust the lock" behaviour — co-artifact entries are skipped
		unconditionally.  Lets callers that don't yet know the
		manifest context (or explicitly opt out) keep working,
		while build / deploy pass the real allowlist."""
		lock_deps = {
			"anything": ResolvedDep(
				version="0.1.0", sha256="", dep_type="co-artifact",
				package_id="anything", author_key="",
			),
		}
		errors = verify_lock_compatibility(lock_deps, {})
		assert errors == []

	def test_co_artifact_round_trip_serialization(self) -> None:
		"""v3 co-artifact lock entry round-trips cleanly: written with
		empty sha256 and empty author_key, read back preserves
		`dep_type="co-artifact"` and accepts the empty fields
		without rejection.  Documents the intentional sentinel
		shape for co-artifact deps (where the .dmp is built later
		in the same deploy run, not yet hashable)."""
		deps = {
			"web.jwt": ResolvedDep(
				version="0.2.0", sha256="", dep_type="co-artifact",
				package_id="web.jwt", author_key="",
			),
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			write_lock(lock_path, {"app": deps})

			# On-disk shape: both sha256 and author_key present as
			# empty strings, dep_type carries the co-artifact marker.
			raw = json.loads(lock_path.read_text(encoding="utf-8"))
			entry = raw["artifacts"]["app"]["resolved"]["web.jwt"]
			assert entry["version"] == "0.2.0"
			assert entry["sha256"] == ""
			assert entry["author_key"] == ""
			assert entry["dep_type"] == "co-artifact"

			result = read_lock(lock_path)
		# Round-trip preserves the co-artifact dep; reader does not
		# reject empty sha / author_key for co-artifact entries.
		assert result["app"]["web.jwt"].dep_type == "co-artifact"
		assert result["app"]["web.jwt"].version == "0.2.0"
		assert result["app"]["web.jwt"].sha256 == ""
		assert result["app"]["web.jwt"].author_key == ""

	def test_v2_lock_rejected_with_prepare_pointer(self) -> None:
		"""v2 locks (range + author_key) must be rejected at load, not
		silently reinterpreted as exact pins.  A v2 `"0.3"` range that
		got treated as an exact `"0.3"` pin would never match any
		disk entry and produce a misleading error."""
		v2_lock = {
			"schema_version": 2,
			"artifacts": {
				"app": {
					"resolved": {
						"net.tls": {
							"version": "0.3",  # v2 range
							"package_id": "net.tls",
							"author_key": "ed25519:abc",
							"dep_type": "direct",
						},
					}
				}
			}
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			lock_path.write_text(json.dumps(v2_lock))
			with pytest.raises(ValueError) as exc:
				read_lock(lock_path)
			msg = str(exc.value)
			assert "schema v2" in msg
			assert "drift prepare" in msg
			# Must explain why: ranges can't be reinterpreted as exact pins.
			assert "reinterpret" in msg.lower() or "safely" in msg.lower()

	def test_v1_lock_rejected_with_clear_message(self) -> None:
		"""Schema v1 lock must be rejected with a message to run prepare."""
		v1_lock = {
			"schema_version": 1,
			"artifacts": {
				"app": {
					"resolved": {
						"net.tls": {"version": "0.3.0", "integrity": "sha256:aa", "dep_type": "direct"},
					}
				}
			}
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			lock_path.write_text(json.dumps(v1_lock))
			with pytest.raises(ValueError, match="schema v1.*prepare"):
				read_lock(lock_path)

	def test_v3_lock_rejected_with_clear_message(self) -> None:
		"""v3 locks (sha256+author_key only, no source identity) must be
		rejected at load with a republish/regenerate diagnostic.
		Silently treating a v3 lock as v4 would let source-rebuild
		mode pass against a lock that never recorded a source identity
		to verify against."""
		v3_lock = {
			"schema_version": 3,
			"artifacts": {
				"app": {
					"resolved": {
						"net.tls": {
							"version": "0.3.15",
							"sha256": "aabbcc",
							"author_key": "ed25519:abc",
							"dep_type": "direct",
						},
					}
				}
			}
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			lock_path.write_text(json.dumps(v3_lock))
			with pytest.raises(ValueError) as exc:
				read_lock(lock_path)
			msg = str(exc.value)
			assert "schema v3" in msg
			assert "drift prepare" in msg
			assert "source_content_id" in msg

	def test_unsigned_lock_rejected(self) -> None:
		"""Empty author_key in a v4 lock entry is rejected — packages
		must be signed before locking (or explicitly flagged
		`"unsigned"` with --allow-unsigned-from downstream)."""
		v4_lock = {
			"schema_version": 4,
			"artifacts": {
				"app": {
					"resolved": {
						"net.tls": {
							"version": "0.3.15",
							"sha256": "aabbcc",
							"author_key": "",
							"source_content_id": "sha256:" + "a"*64,
							"source_attestation_key": "ed25519:abc",
							"dep_type": "direct",
						},
					}
				}
			}
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			lock_path.write_text(json.dumps(v4_lock))
			with pytest.raises(ValueError, match="author_key"):
				read_lock(lock_path)

	def test_missing_source_content_id_rejected(self) -> None:
		"""v4 lock entries require source_content_id for every non-co-
		artifact dep; absence is a hard error with republish guidance.
		Without it source-rebuild mode would have nothing to verify
		the rebuilt artifact's source identity against."""
		v4_lock = {
			"schema_version": 4,
			"artifacts": {
				"app": {
					"resolved": {
						"net.tls": {
							"version": "0.3.15",
							"sha256": "aabbcc",
							"author_key": "ed25519:abc",
							# source_content_id omitted
							"source_attestation_key": "ed25519:abc",
							"dep_type": "direct",
						},
					}
				}
			}
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			lock_path.write_text(json.dumps(v4_lock))
			with pytest.raises(ValueError, match="source_content_id"):
				read_lock(lock_path)

	def test_missing_source_attestation_key_rejected(self) -> None:
		"""v4 lock entries require source_attestation_key for every
		non-co-artifact dep; absence collapses the source-rebuild
		trust root to "trust the rebuilder", which is exactly what
		source-mode exists to prevent."""
		v4_lock = {
			"schema_version": 4,
			"artifacts": {
				"app": {
					"resolved": {
						"net.tls": {
							"version": "0.3.15",
							"sha256": "aabbcc",
							"author_key": "ed25519:abc",
							"source_content_id": "sha256:" + "a"*64,
							# source_attestation_key omitted
							"dep_type": "direct",
						},
					}
				}
			}
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			lock_path.write_text(json.dumps(v4_lock))
			with pytest.raises(ValueError, match="source_attestation_key"):
				read_lock(lock_path)

	def test_missing_sha256_rejected(self) -> None:
		"""v4 lock entries require sha256 for every non-co-artifact dep;
		absence is a hard error pointing at `drift prepare`."""
		v4_lock = {
			"schema_version": 4,
			"artifacts": {
				"app": {
					"resolved": {
						"net.tls": {
							"version": "0.3.15",
							# sha256 omitted — v4 requires it
							"author_key": "ed25519:abc",
							"source_content_id": "sha256:" + "a"*64,
							"source_attestation_key": "ed25519:abc",
							"dep_type": "direct",
						},
					}
				}
			}
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "lock.json"
			lock_path.write_text(json.dumps(v4_lock))
			with pytest.raises(ValueError, match="sha256"):
				read_lock(lock_path)


# ── Package index: symlink dedup ─────────────────────────────────────


class TestBuildPackageIndexDedup:
	"""
	Regression: when staged root symlinks into dest and dest is also
	a package root, the same physical .dmp is discoverable through
	both paths.  build_package_index must index it once, not raise
	a duplicate-package error.
	"""

	def test_symlinked_roots_dedup_same_physical_file(self) -> None:
		"""Same .dmp reachable via symlink and direct path → indexed once."""
		import os

		with tempfile.TemporaryDirectory() as tmpdir:
			base = Path(tmpdir)
			dest = base / "dest"
			staged = base / "staged"
			dest.mkdir()
			staged.mkdir()

			# Create a real .dmp in dest.
			dmp = dest / "net-tls-0.2.0.dmp"
			dmp.write_bytes(b"fake")  # content doesn't matter, we mock loader

			# Staged root symlinks the .dmp into itself.
			link = staged / "net-tls-0.2.0.dmp"
			os.symlink(str(dmp), str(link))

			manifests_returned = []

			def fake_loader(path: Path) -> dict:
				manifests_returned.append(path)
				return {
					"package_id": "net.tls",
					"package_version": "0.2.0",
					"required_deps": [],
				}

			# Both roots index the same physical file.
			idx = build_package_index([staged, dest], load_manifest=fake_loader)

			# Should be indexed exactly once (not raise duplicate-package error).
			assert "net.tls" in idx
			assert len(idx["net.tls"]) == 1
			assert idx["net.tls"][0].version == parse_version("0.2.0")
			# Loader called only once (second path skipped by physical dedup).
			assert len(manifests_returned) == 1

	def test_real_duplicates_in_same_root_still_error(self) -> None:
		"""Two distinct physical files with same pkg@version in same root → error."""
		with tempfile.TemporaryDirectory() as tmpdir:
			base = Path(tmpdir)
			root = base / "packages"
			root.mkdir()

			# Two distinct physical files for same package+version.
			dmp1 = root / "net-tls-a.dmp"
			dmp2 = root / "net-tls-b.dmp"
			dmp1.write_bytes(b"file1")
			dmp2.write_bytes(b"file2")

			def fake_loader(path: Path) -> dict:
				return {
					"package_id": "net.tls",
					"package_version": "0.2.0",
					"required_deps": [],
				}

			with pytest.raises(ResolutionError, match="duplicate package"):
				build_package_index([root], load_manifest=fake_loader)

	def test_same_pkg_version_in_different_roots_first_wins(self) -> None:
		"""Same pkg@version in two roots (distinct files) → first root wins, no error."""
		with tempfile.TemporaryDirectory() as tmpdir:
			base = Path(tmpdir)
			root1 = base / "root1"
			root2 = base / "root2"
			root1.mkdir()
			root2.mkdir()

			dmp1 = root1 / "net-tls.dmp"
			dmp2 = root2 / "net-tls.dmp"
			dmp1.write_bytes(b"first")
			dmp2.write_bytes(b"second")

			call_count = [0]

			def fake_loader(path: Path) -> dict:
				call_count[0] += 1
				return {
					"package_id": "net.tls",
					"package_version": "0.2.0",
					"required_deps": [],
				}

			idx = build_package_index([root1, root2], load_manifest=fake_loader)
			assert len(idx["net.tls"]) == 1
			# Both files loaded (different physical paths), but second skipped by root priority.
			assert call_count[0] == 2

	def test_corrupt_zdmp_fails_even_with_valid_dmp_sibling(self) -> None:
		"""A `.zdmp` is the published compressed artifact.  If it
		exists but fails to load, the resolver MUST fail loudly —
		even when a perfectly valid `.dmp` with the same stem is
		sitting right next to it.  The pre-0.29 behaviour of
		silently falling back to the `.dmp` let a bad deploy
		masquerade as good because the uncompressed local build
		artifact was still usable while the published shape was
		broken.  Fail early instead.
		"""
		from lang.driftc.packages.dmir_pkg_v0 import write_dmir_pkg_v0

		with tempfile.TemporaryDirectory() as tmpdir:
			root = Path(tmpdir)

			# Write a valid .dmp using the real container format.
			manifest_obj = {
				"format": "dmir-pkg",
				"format_version": 0,
				"package_id": "acme.lib",
				"package_version": "1.0.0",
				"target": "test-target",
				"abi_fingerprint": "test",
				"unsigned": True,
				"unstable_format": True,
				"payload_kind": "provisional-dmir",
				"payload_version": 0,
				"modules": [],
				"blobs": {},
			}
			dmp = root / "lib.dmp"
			write_dmir_pkg_v0(dmp, manifest_obj=manifest_obj, blobs={}, blob_types={}, blob_names={})

			# Place a corrupt .zdmp with the same stem — this is the
			# exact "bad published artifact" shape we want to catch.
			zdmp = root / "lib.zdmp"
			zdmp.write_bytes(b"NOT VALID ZSTD DATA")

			with pytest.raises(ResolutionError) as exc_info:
				build_package_index([root])
			msg = str(exc_info.value)
			assert str(zdmp) in msg
			assert ".zdmp" in msg
			# Diagnostic must name the remediation explicitly.
			assert "republish" in msg.lower() or "reinstall" in msg.lower()
			# And state that the .dmp sibling is NOT used.
			assert "fallback" in msg.lower() or "will NOT" in msg

	def test_pre_029_zdmp_fails_with_republish_diagnostic_even_with_dmp_sibling(self) -> None:
		"""A `.zdmp` carrying pre-0.29 metadata (legacy
		`package_deps` key) with a valid `.dmp` sibling beside it
		must still surface the metadata/republish diagnostic — the
		`.dmp` is NOT used as a workaround.  Same principle as the
		corrupt-zdmp case: bad published metadata is the user's
		problem to fix by republishing, not something the resolver
		routes around.
		"""
		from lang.driftc.packages.dmir_pkg_v0 import write_dmir_pkg_v0
		from lang.driftc.packages.zdmp import compress_to_zdmp

		with tempfile.TemporaryDirectory() as tmpdir:
			root = Path(tmpdir)

			# Valid .dmp sibling (v3-shape metadata).
			valid_manifest = {
				"format": "dmir-pkg",
				"format_version": 0,
				"package_id": "legacy.lib",
				"package_version": "1.0.0",
				"target": "test-target",
				"abi_fingerprint": "test",
				"unsigned": True,
				"unstable_format": True,
				"payload_kind": "provisional-dmir",
				"payload_version": 0,
				"modules": [],
				"blobs": {},
			}
			dmp = root / "lib.dmp"
			write_dmir_pkg_v0(dmp, manifest_obj=valid_manifest, blobs={}, blob_types={}, blob_names={})

			# Pre-0.29 .zdmp — same container format, but the manifest
			# still carries the legacy `package_deps` key.  Build the
			# raw .dmp first, then compress in place.
			legacy_manifest = dict(valid_manifest)
			legacy_manifest["package_deps"] = [{"name": "other.lib", "version": "0.3.14"}]
			raw_dmp = root / "lib-legacy.raw.dmp"
			write_dmir_pkg_v0(raw_dmp, manifest_obj=legacy_manifest, blobs={}, blob_types={}, blob_names={})
			zdmp = root / "lib.zdmp"
			zdmp.write_bytes(compress_to_zdmp(raw_dmp.read_bytes()))
			raw_dmp.unlink()  # only the compressed form stays next to the .dmp

			with pytest.raises(ResolutionError) as exc_info:
				build_package_index([root])
			msg = str(exc_info.value)
			# Must name the bad .zdmp, not the healthy .dmp sibling.
			assert str(zdmp) in msg
			assert str(dmp) not in msg
			# Must preserve the existing metadata/republish framing.
			assert "legacy `package_deps`" in msg or "pre-0.29" in msg

	def test_only_dmp_present_still_loads_normally(self) -> None:
		"""Baseline: if only a `.dmp` exists (no `.zdmp` present),
		normal package loading still works.  The strict `.zdmp`
		rule only fires when a `.zdmp` IS present and fails — it
		does not penalise the plain-`.dmp`-only workflow used by
		local development and older test fixtures."""
		from lang.driftc.packages.dmir_pkg_v0 import write_dmir_pkg_v0

		with tempfile.TemporaryDirectory() as tmpdir:
			root = Path(tmpdir)
			manifest_obj = {
				"format": "dmir-pkg",
				"format_version": 0,
				"package_id": "plain.lib",
				"package_version": "2.0.0",
				"target": "test-target",
				"abi_fingerprint": "test",
				"unsigned": True,
				"unstable_format": True,
				"payload_kind": "provisional-dmir",
				"payload_version": 0,
				"modules": [],
				"blobs": {},
			}
			dmp = root / "lib.dmp"
			write_dmir_pkg_v0(dmp, manifest_obj=manifest_obj, blobs={}, blob_types={}, blob_names={})
			# No .zdmp beside it.

			idx = build_package_index([root])
			assert "plain.lib" in idx
			assert len(idx["plain.lib"]) == 1
			assert idx["plain.lib"][0].version == parse_version("2.0.0")

	def test_real_pre_cut_dmp_surfaces_republish_guidance(self) -> None:
		"""K Finding 1 regression: a real .dmp carrying the legacy
		`package_deps` metadata key must surface the "republish with
		0.29" guidance via the real loader path — not be silently
		swallowed as "unreadable".

		Previous behaviour: the loader raised inside
		`_parse_required_deps`, `build_package_index`'s broad
		``except Exception`` swallowed it, and the user saw a
		generic "dep not satisfied" downstream.  Now the loader
		raises `PackageMetadataError`, which the index distinguishes
		from true I/O / container corruption and re-raises as a
		`ResolutionError` pointing at the pre-0.29 toolchain.
		"""
		from lang.driftc.packages.dmir_pkg_v0 import write_dmir_pkg_v0

		with tempfile.TemporaryDirectory() as tmpdir:
			root = Path(tmpdir)
			# Construct a valid v0 container whose MANIFEST still
			# carries the pre-0.29 `package_deps` key.
			manifest_obj = {
				"format": "dmir-pkg",
				"format_version": 0,
				"package_id": "legacy.lib",
				"package_version": "1.0.0",
				"target": "test-target",
				"abi_fingerprint": "test",
				"unsigned": True,
				"unstable_format": True,
				"payload_kind": "provisional-dmir",
				"payload_version": 0,
				"modules": [],
				"blobs": {},
				# THE offending key.  A 0.29+ container uses
				# `required_deps`; `package_deps` is the pre-cut form.
				"package_deps": [{"name": "dep-a", "version": "0.3.14"}],
			}
			dmp = root / "legacy.dmp"
			write_dmir_pkg_v0(dmp, manifest_obj=manifest_obj, blobs={}, blob_types={}, blob_names={})

			with pytest.raises(ResolutionError) as exc_info:
				build_package_index([root])
			msg = str(exc_info.value)
			assert str(dmp) in msg
			assert "legacy `package_deps`" in msg
			assert "pre-0.29" in msg
			assert "republished" in msg

	def test_real_malformed_required_deps_surfaces_republish_guidance(self) -> None:
		"""A real .dmp whose `required_deps` entry carries an invalid
		version shape (exact `M.N.P`, which is not an owner-declared
		range) must likewise surface a hard `ResolutionError` instead
		of being silently skipped.  Ranges are the only valid
		published-metadata shape — anything else is a malformed
		producer and has to be republished."""
		from lang.driftc.packages.dmir_pkg_v0 import write_dmir_pkg_v0

		with tempfile.TemporaryDirectory() as tmpdir:
			root = Path(tmpdir)
			manifest_obj = {
				"format": "dmir-pkg",
				"format_version": 0,
				"package_id": "malformed.lib",
				"package_version": "1.0.0",
				"target": "test-target",
				"abi_fingerprint": "test",
				"unsigned": True,
				"unstable_format": True,
				"payload_kind": "provisional-dmir",
				"payload_version": 0,
				"modules": [],
				"blobs": {},
				# Invalid: required_deps entries must be "M" or "M.N".
				"required_deps": [{"name": "dep-a", "version": "0.3.14"}],
			}
			dmp = root / "malformed.dmp"
			write_dmir_pkg_v0(dmp, manifest_obj=manifest_obj, blobs={}, blob_types={}, blob_names={})

			with pytest.raises(ResolutionError) as exc_info:
				build_package_index([root])
			msg = str(exc_info.value)
			assert str(dmp) in msg
			assert "0.3.14" in msg


# ── 0.29.0 two-layer model: required_deps ranges vs. producer lock pins ──
#
# Core semantic shift: consumer resolution uses the PUBLISHED range
# constraint from a package's metadata (`required_deps` drawn from the
# producer's manifest), NOT the producer's local lock pin.  This lets
# patch bumps flow silently through the graph without requiring every
# intermediate library to republish.
#
# The in-memory `PackageEntry.required_deps` field carries range
# strings (not exact pins), mirroring the `required_deps` semantics
# in the v3 .dmp format.  These tests pin the scenario end-to-end
# so a regression where someone wires the consumer resolver to a
# producer's lock pin (instead of its published range) is caught.


class TestTwoLayerVersioning:
	"""0.29.0 regression: patch movement flows through producer
	`required_deps` ranges, not producer lock pins."""

	def test_transitive_patch_float_through_range(self) -> None:
		"""**Downstream-constraint rule.**  For `app → pkg2 → pkg1`,
		the constraint that crosses the package boundary is pkg2's
		**manifest-declared acceptable range** for pkg1, NOT pkg2's
		lock's exact pkg1 version.  Pkg2's lock pin is local to pkg2
		and must not leak downstream.

		Concrete example (the orch-team report shape):

		- pkg2 (lib-2) manifest declares pkg1 (lib-1) acceptable
		  range as "0.3".
		- pkg2's own lock may carry pkg1 "0.3.14" — what pkg2 itself
		  was prepared/tested against.  LOCAL.
		- Package roots contain pkg1 0.3.14 AND pkg1 0.3.15.
		- app depends on pkg2 "1.2".

		app's prepare-time resolution MUST pick pkg1@0.3.15, the
		highest version satisfying pkg2's exported "0.3" range.  The
		older pkg2-local pin (0.3.14) does NOT restrict app.
		Without this rule, exact pins leak down the graph and
		recreate the patch-churn problem one level lower.
		"""
		idx = _index(
			_make_entry("lib-1", "0.3.14", sha="a14"),
			_make_entry("lib-1", "0.3.15", sha="a15"),
			_make_entry(
				"lib-2", "1.2.7", sha="b127",
				# required_deps — range, not pin
				deps=[("lib-1", "0.3")],
			),
		)
		result = resolve_artifact(
			"app",
			[("lib-2", "1.2")],  # app's manifest range
			idx,
		)
		# lib-1 picked at the highest version satisfying "0.3" —
		# lib-2's internal lock pin (whatever it was) is irrelevant.
		assert result["lib-1"].version == "0.3.15", (
			f"expected app to pick lib-1@0.3.15 through lib-2's exported "
			f"range; got {result['lib-1'].version}.  If this regresses, "
			f"patch bumps no longer flow silently — every upstream bump "
			f"forces manual manifest edits in every consumer."
		)
		assert result["lib-1"].dep_type == "transitive"
		assert result["lib-2"].version == "1.2.7"
		assert result["lib-2"].dep_type == "direct"

	def test_patch_bump_flows_without_intermediate_republish(self) -> None:
		"""
		Bump scenario: lib-1 goes from 0.3.14-only to 0.3.14+0.3.15.
		lib-2 is untouched (same version, same required_deps range).
		app prepare picks up lib-1@0.3.15 automatically.

		This is the anti-regression pin for the drift-web / net-tls
		friction: a net-tls patch bump must not require drift-web to
		edit its manifest nor require any intermediate library to
		republish.
		"""
		# Before bump: only 0.3.14 on disk.
		idx_before = _index(
			_make_entry("lib-1", "0.3.14", sha="a14"),
			_make_entry(
				"lib-2", "1.2.7", sha="b127",
				deps=[("lib-1", "0.3")],
			),
		)
		before = resolve_artifact("app", [("lib-2", "1.2")], idx_before)
		assert before["lib-1"].version == "0.3.14"

		# After bump: 0.3.15 published; lib-2 and app unchanged.
		idx_after = _index(
			_make_entry("lib-1", "0.3.14", sha="a14"),
			_make_entry("lib-1", "0.3.15", sha="a15"),
			_make_entry(
				"lib-2", "1.2.7", sha="b127",
				deps=[("lib-1", "0.3")],  # unchanged
			),
		)
		after = resolve_artifact("app", [("lib-2", "1.2")], idx_after)
		assert after["lib-1"].version == "0.3.15", (
			"lib-1 patch bump did NOT flow into app's resolution — this "
			"regresses the two-layer model.  lib-2 must not need to "
			"republish for its consumers to pick up upstream patches."
		)
		# lib-2 version unchanged — consumer graph picked up the
		# upstream patch without any intermediate library moving.
		assert after["lib-2"].version == before["lib-2"].version

	def test_exported_range_honored_across_minor_boundary(self) -> None:
		"""
		Negative control: the range is `major.minor`, not `major`.
		lib-2 exports lib-1 "0.3"; lib-1 0.4.0 must NOT satisfy it.
		"""
		idx = _index(
			_make_entry("lib-1", "0.3.14", sha="a14"),
			_make_entry("lib-1", "0.4.0", sha="a40"),
			_make_entry(
				"lib-2", "1.2.7", sha="b127",
				deps=[("lib-1", "0.3")],
			),
		)
		result = resolve_artifact("app", [("lib-2", "1.2")], idx)
		# lib-1 must stay on the "0.3" line even though 0.4.0 exists.
		assert result["lib-1"].version == "0.3.14"

	def test_manifest_range_major_minor_only_model(self) -> None:
		"""
		Document expected semantics: the resolver accepts a
		`"major.minor"` range directly for direct (manifest-level)
		deps.  The caller (drift prepare) passes what the manifest
		declares — `"0.3"` means any `0.3.x`.
		"""
		idx = _index(
			_make_entry("lib-1", "0.3.14", sha="a14"),
			_make_entry("lib-1", "0.3.15", sha="a15"),
		)
		result = resolve_artifact("app", [("lib-1", "0.3")], idx)
		assert result["lib-1"].version == "0.3.15"

	def test_major_only_range_accepts_any_minor_and_patch(self) -> None:
		"""v2 manifest may declare `"1"` for any 1.x.x.  The owner is
		saying "I accept any 1-series release" — resolver picks the
		highest trusted `1.x.x` and leaves 2.x.x alone."""
		idx = _index(
			_make_entry("lib-a", "1.0.0", sha="a100"),
			_make_entry("lib-a", "1.4.7", sha="a147"),
			_make_entry("lib-a", "1.9.0", sha="a190"),
			_make_entry("lib-a", "2.0.0", sha="a200"),
		)
		result = resolve_artifact("app", [("lib-a", "1")], idx)
		# Highest satisfying — 1.9.0, not the 2.0.0 major bump.
		assert result["lib-a"].version == "1.9.0"

	def test_major_only_range_rejects_major_boundary(self) -> None:
		"""`"1"` must NOT match any 2.x.x."""
		idx = _index(
			_make_entry("lib-a", "2.0.0", sha="a200"),
			_make_entry("lib-a", "2.3.1", sha="a231"),
		)
		with pytest.raises(ResolutionError, match="not satisfied"):
			resolve_artifact("app", [("lib-a", "1")], idx)

	def test_two_root_packages_disagree_on_transitive_range(self) -> None:
		"""Conflict at prepare/resolution time (the tooling layer, NOT
		inside driftc): two directly-depended packages publish
		disjoint owner-declared ranges for the same transitive, and no
		transitive version satisfies both → `ResolutionError`.

		Specifically mirrors the pre-0.29 driver-layer
		"two roots disagree on deplib" fixture, restated in the
		post-0.29 vocabulary:

		- `liba.required_deps` → `deplib = "0.1"` (any 0.1.x).
		- `libb.required_deps` → `deplib = "0.2"` (any 0.2.x).
		- deplib 0.1.x and 0.2.x both present on disk; no one version
		  lies in both `"0.1"` and `"0.2"` simultaneously.

		Resolution must abort with a conflict diagnostic before a lock
		is written — this is the same behaviour driftc relies on when
		it insists on a complete, self-consistent `--dep` graph.
		"""
		idx = _index(
			_make_entry("deplib", "0.1.0", sha="d010"),
			_make_entry("deplib", "0.2.0", sha="d020"),
			_make_entry(
				"liba", "1.0.0", sha="la100",
				deps=[("deplib", "0.1")],
			),
			_make_entry(
				"libb", "1.0.0", sha="lb100",
				deps=[("deplib", "0.2")],
			),
		)
		with pytest.raises(ResolutionError) as exc_info:
			resolve_artifact(
				"app",
				[("liba", "1.0"), ("libb", "1.0")],
				idx,
			)
		err = str(exc_info.value)
		assert "deplib" in err
		# Diagnostic must surface both sides of the disagreement so
		# the user can see which packages are in conflict.
		assert "liba" in err or "libb" in err


class TestReadSourceAttestationMeta:
	"""Phase B.1 trust gate at the resolver layer.  Sidecars must:
	1. structurally load (delegated to read_attestation_sidecar);
	2. carry a verifying signature (verify_attestation);
	3. cross-bind to the .dmp manifest they sit next to (package_id,
	   version, target_class, required_deps, source_content_id stamp).

	Any failure → empty result + stderr warning, so unrelated builds
	can still discover other packages in the same root.  drift_prepare
	turns the empty result into a fail-fast republish-required error
	for resolved non-co-artifact deps."""

	@staticmethod
	def _make_dmp(tmpdir: Path, pkg_id: str = "net-tls", version: str = "0.4.0",
			target: str = "linux-x86_64", required_deps: list | None = None,
			source_content_id: str | None = None) -> tuple[Path, dict]:
		"""Write a fake .dmp file (just placeholder bytes) and return
		(path, manifest dict).  build_package_index passes the
		manifest dict to _read_source_attestation_meta; we don't need
		the .dmp to be parseable for these helper unit tests."""
		dmp_path = tmpdir / f"{pkg_id}.dmp"
		dmp_path.write_bytes(b"fake-dmp")
		manifest = {
			"package_id": pkg_id,
			"package_version": version,
			"target": target,
			"required_deps": required_deps or [],
		}
		if source_content_id is not None:
			manifest["source_content_id"] = source_content_id
		return dmp_path, manifest

	@staticmethod
	def _write_sidecar(dmp_path: Path, body_overrides: dict | None = None,
			signing_seed: bytes = bytes(range(32))) -> str:
		"""Write a real .source-attestation sidecar next to dmp_path.
		Returns the signer kid the sidecar was signed with."""
		from tools.drift_deploy.source_attestation import (
			RequiredDepEntry,
			SourceAttestationBody,
			SOURCE_ATTESTATION_BODY_SCHEMA_VERSION,
			sign_attestation,
			write_attestation_sidecar,
			_ed25519_kid,
		)
		from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
		from cryptography.hazmat.primitives import serialization
		body_defaults = {
			"schema_version": SOURCE_ATTESTATION_BODY_SCHEMA_VERSION,
			"package_id": "net-tls",
			"version": "0.4.0",
			"source_content_id": "sha256:" + "a"*64,
			"required_deps": [],
			"target_class": "linux-x86_64",
		}
		body_defaults.update(body_overrides or {})
		body = SourceAttestationBody(**body_defaults)
		sidecar = sign_attestation(body, signing_key_seed=signing_seed)
		sidecar_path = dmp_path.with_suffix(".source-attestation")
		write_attestation_sidecar(sidecar_path, sidecar)
		# Compute the kid for assertion convenience.
		priv = Ed25519PrivateKey.from_private_bytes(signing_seed)
		pub = priv.public_key().public_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PublicFormat.Raw,
		)
		return _ed25519_kid(pub)

	def test_absent_sidecar_returns_empty(self) -> None:
		"""Legacy package with no sidecar at all → empty (drift prepare
		surfaces this as fail-fast for non-co-artifact deps)."""
		with tempfile.TemporaryDirectory() as tmp:
			dmp_path, manifest = self._make_dmp(Path(tmp))
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""

	def test_matching_sidecar_returns_scid_and_kid(self) -> None:
		"""All cross-binding fields match → returns body's source_content_id
		and the verified signer kid."""
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			scid_value = "sha256:" + "a"*64
			dmp_path, manifest = self._make_dmp(tmp_p, source_content_id=scid_value)
			expected_kid = self._write_sidecar(dmp_path)
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == scid_value
			assert kid == expected_kid

	def test_mismatched_package_id_returns_empty(self, capsys) -> None:
		"""Sidecar body says it's for package-X, .dmp manifest says
		package-Y → cross-binding fails, empty result, stderr warning."""
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			dmp_path, manifest = self._make_dmp(tmp_p, pkg_id="net-tls")
			self._write_sidecar(dmp_path, body_overrides={"package_id": "other-pkg"})
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""
			err = capsys.readouterr().err
			assert "package_id" in err

	def test_mismatched_version_returns_empty(self, capsys) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			dmp_path, manifest = self._make_dmp(tmp_p, version="0.4.0")
			self._write_sidecar(dmp_path, body_overrides={"version": "0.4.1"})
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""
			assert "version" in capsys.readouterr().err

	def test_mismatched_target_class_returns_empty(self, capsys) -> None:
		"""Cross-target attestation substitution → caught."""
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			dmp_path, manifest = self._make_dmp(tmp_p, target="linux-x86_64")
			self._write_sidecar(dmp_path, body_overrides={"target_class": "linux-aarch64"})
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""
			assert "target_class" in capsys.readouterr().err

	def test_mismatched_source_content_id_stamp_returns_empty(self, capsys) -> None:
		"""When the .dmp manifest carries a source_content_id stamp
		(Phase A producer wired), it must equal the sidecar body's
		value — otherwise the producer was inconsistent."""
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			dmp_path, manifest = self._make_dmp(tmp_p, source_content_id="sha256:" + "a"*64)
			self._write_sidecar(dmp_path, body_overrides={
				"source_content_id": "sha256:" + "b"*64,
			})
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""
			assert "source_content_id" in capsys.readouterr().err

	def test_mismatched_required_deps_returns_empty(self, capsys) -> None:
		"""Sidecar attests one required_deps set, .dmp manifest declares
		another → caught (one of the two is lying about what the
		package depends on).  Note: stamp must be present (Phase B.2
		requires it) for the helper to reach the required_deps check."""
		from tools.drift_deploy.source_attestation import RequiredDepEntry
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			dmp_path, manifest = self._make_dmp(
				tmp_p,
				required_deps=[{"name": "drift-core", "version": "0.27"}],
				source_content_id="sha256:" + "a"*64,  # matches sidecar default
			)
			self._write_sidecar(dmp_path, body_overrides={
				"required_deps": [RequiredDepEntry(name="drift-net", version="0.4")],
			})
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""
			assert "required_deps" in capsys.readouterr().err

	def test_missing_dmp_stamp_returns_empty_even_with_valid_sidecar(self, capsys) -> None:
		"""Phase B.2 trust gate: an old `.dmp` lacking the v4
		`source_content_id` stamp must NOT be retroactively upgraded
		into source-mode by adjacency to a validly signed sidecar.
		The artifact itself must declare its source identity for the
		v4 contract to hold; otherwise an attacker (or a careless
		operator) could ship a legacy package alongside a sidecar
		stolen from another release and have it pass.

		This is the asymmetric case: structural load passes, signature
		verifies, all body fields cross-bind to the manifest fields
		that DO exist — but the manifest has no source_content_id
		stamp, so the helper rejects."""
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			# Note: NO source_content_id passed → manifest stamp absent.
			dmp_path, manifest = self._make_dmp(tmp_p)
			assert "source_content_id" not in manifest
			self._write_sidecar(dmp_path)
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""
			err = capsys.readouterr().err
			assert "source_content_id" in err
			assert "republish" in err.lower()

	def test_malformed_dmp_stamp_returns_empty(self, capsys) -> None:
		"""A `.dmp` stamp present but malformed (uppercase hex, wrong
		length, non-`sha256:` prefix, etc.) is also rejected — the
		strict-shape validator at the trust boundary refuses to let
		programmatic callers smuggle a non-canonical id into a
		signed lock entry."""
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			dmp_path, manifest = self._make_dmp(
				tmp_p,
				source_content_id="sha256:" + "A"*64,  # uppercase → invalid
			)
			self._write_sidecar(dmp_path)
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""
			assert "lowercase hex" in capsys.readouterr().err

	def test_signature_verify_failure_returns_empty(self, capsys) -> None:
		"""Sidecar that cannot signature-verify → empty result, no kid
		recorded.  Edit the on-disk JSON to flip a body field AND its
		body_sha256 (so the body_sha256 self-check passes) — the
		signature verification then fails."""
		from tools.drift_deploy.source_attestation import (
			SOURCE_ATTESTATION_SIDECAR_FORMAT,
			SOURCE_ATTESTATION_SIDECAR_VERSION,
		)
		import hashlib as _hl
		with tempfile.TemporaryDirectory() as tmp:
			tmp_p = Path(tmp)
			dmp_path, manifest = self._make_dmp(tmp_p)
			self._write_sidecar(dmp_path)
			sidecar_path = dmp_path.with_suffix(".source-attestation")
			obj = json.loads(sidecar_path.read_text(encoding="utf-8"))
			# Flip a non-binding field (schema_version is bound, so use
			# package_id and update both body and body_sha256 to keep
			# the structural self-check happy).  Crucially leave
			# package_id matching the manifest so we'd fall through
			# to the signature check.
			obj["body"]["package_id"] = "net-tls"  # still matches
			# Replace the signature with garbage of correct length.
			import base64 as _b64
			obj["signatures"][0]["sig"] = _b64.b64encode(b"\x00" * 64).decode("ascii")
			# Recompute body_sha256 in case body changed.
			canon = json.dumps(obj["body"], sort_keys=True, separators=(",", ":")).encode("utf-8")
			obj["body_sha256"] = "sha256:" + _hl.sha256(canon).hexdigest()
			sidecar_path.write_text(json.dumps(obj), encoding="utf-8")
			scid, kid = _read_source_attestation_meta(dmp_path, manifest)
			assert scid == "" and kid == ""
			assert "signature" in capsys.readouterr().err
