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
	verify_lock_integrity,
	write_lock,
)
from tools.drift_deploy.resolver import (
	PackageEntry,
	ResolutionError,
	ResolvedDep,
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
) -> PackageEntry:
	return PackageEntry(
		package_id=pkg_id,
		version=parse_version(version),
		path=Path(f"/fake/{pkg_id}-{version}.dmp"),
		sha256=sha,
		package_deps=deps or [],
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
		assert result["net.tls"].integrity == "sha256:aa"
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
			lock_path = Path(tmpdir) / "drift-lock.json"
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


# ── Lock file round-trip ────────────────────────────────────────────


class TestLockFile:
	def test_write_read_roundtrip(self) -> None:
		deps_a = {
			"net.tls": ResolvedDep(version="0.3.2", integrity="sha256:aabb", dep_type="direct"),
			"acme.crypto": ResolvedDep(version="0.9.0", integrity="sha256:ccdd", dep_type="transitive"),
		}
		deps_b = {
			"util.log": ResolvedDep(version="1.0.0", integrity="sha256:eeff", dep_type="direct"),
		}

		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "drift-lock.json"
			write_lock(lock_path, {"app-a": deps_a, "app-b": deps_b})
			result = read_lock(lock_path)

		assert set(result.keys()) == {"app-a", "app-b"}
		assert result["app-a"]["net.tls"].version == "0.3.2"
		assert result["app-a"]["net.tls"].dep_type == "direct"
		assert result["app-a"]["acme.crypto"].dep_type == "transitive"
		assert result["app-b"]["util.log"].integrity == "sha256:eeff"

	def test_expand_to_dep_flags(self) -> None:
		resolved = {
			"net.tls": ResolvedDep(version="0.3.2", integrity="sha256:aa", dep_type="direct"),
			"acme.crypto": ResolvedDep(version="0.9.0", integrity="sha256:bb", dep_type="transitive"),
		}
		flags = expand_to_dep_flags(resolved)
		# Sorted by package_id → acme.crypto first.
		assert flags == [
			"--dep", "acme.crypto@0.9.0",
			"--dep", "net.tls@0.3.2",
		]

	def test_verify_integrity_pass(self) -> None:
		lock_deps = {
			"net.tls": ResolvedDep(version="0.3.0", integrity="sha256:aa", dep_type="direct"),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.0", sha="aa")],
		}
		errors = verify_lock_integrity(lock_deps, pkg_index)
		assert errors == []

	def test_verify_integrity_mismatch(self) -> None:
		lock_deps = {
			"net.tls": ResolvedDep(version="0.3.0", integrity="sha256:aa", dep_type="direct"),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.3.0", sha="ff")],
		}
		errors = verify_lock_integrity(lock_deps, pkg_index)
		assert len(errors) == 1
		assert "integrity mismatch" in errors[0]

	def test_verify_integrity_version_missing(self) -> None:
		lock_deps = {
			"net.tls": ResolvedDep(version="0.3.0", integrity="sha256:aa", dep_type="direct"),
		}
		pkg_index = {
			"net.tls": [_make_entry("net.tls", "0.4.0", sha="aa")],
		}
		errors = verify_lock_integrity(lock_deps, pkg_index)
		assert len(errors) == 1
		assert "not found" in errors[0]


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
					"package_deps": [],
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
					"package_deps": [],
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
					"package_deps": [],
				}

			idx = build_package_index([root1, root2], load_manifest=fake_loader)
			assert len(idx["net.tls"]) == 1
			# Both files loaded (different physical paths), but second skipped by root priority.
			assert call_count[0] == 2

	def test_corrupt_zdmp_falls_back_to_dmp_sibling(self) -> None:
		"""Corrupt .zdmp + valid .dmp sibling → resolver indexes from .dmp."""
		from lang.driftc.packages.dmir_pkg_v0 import write_dmir_pkg_v0, canonical_json_bytes

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

			# Place a corrupt .zdmp with the same stem.
			zdmp = root / "lib.zdmp"
			zdmp.write_bytes(b"NOT VALID ZSTD DATA")

			# Default loader (no mock): exercises real zdmp fallback.
			idx = build_package_index([root])
			assert "acme.lib" in idx
			assert len(idx["acme.lib"]) == 1
			assert idx["acme.lib"][0].version == parse_version("1.0.0")
