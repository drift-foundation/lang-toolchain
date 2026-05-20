# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Trust-agnostic unit tests for the deploy resolver and semver layer.

These tests cover behaviors orthogonal to the trust-v1 contract:

  - `tools/drift_deploy/semver.py` parsing + ordering + range matching
  - `tools/drift_deploy/resolver.resolve_artifact` selection +
    conflict-detection invariants

The trust-v1 cutover removed the broader `test_resolver.py` file
because it was deeply intertwined with v0 `.sig` / `.source-attestation`
fixtures.  The pure resolver/semver unit cases below were salvaged
from that file's pre-cutover state because they don't depend on the
trust model at all -- they verify the dependency-graph correctness
contract that any trust model has to be built on top of.

Lock-file behaviors and v4-shape verification live in
`tools/drift_deploy/test_drift_lock.py` and
`tools/drift_deploy/test_build.py::TestLockCompatibility`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.drift_deploy.resolver import (
	PackageEntry,
	ResolutionError,
	resolve_artifact,
)
from tools.drift_deploy.semver import (
	SemVer,
	parse_constraint,
	parse_version,
)


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
		required_deps=deps or [],
		author_key="",
		source_content_id="",
		source_attestation_key="",
	)


def _index(*entries: PackageEntry) -> dict[str, list[PackageEntry]]:
	idx: dict[str, list[PackageEntry]] = {}
	for e in entries:
		idx.setdefault(e.package_id, []).append(e)
	return idx


class TestSemVer:
	def test_parse_valid(self) -> None:
		assert parse_version("1.2.3") == SemVer(1, 2, 3)

	def test_parse_invalid(self) -> None:
		with pytest.raises(ValueError):
			parse_version("1.2")

	def test_ordering(self) -> None:
		assert parse_version("0.9.0") < parse_version("1.0.0")
		assert parse_version("1.2.3") < parse_version("1.2.4")
		assert parse_version("1.2.3") < parse_version("1.3.0")

	def test_str(self) -> None:
		assert str(parse_version("1.2.3")) == "1.2.3"


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


class TestResolverBasic:
	def test_single_direct_dep(self) -> None:
		idx = _index(_make_entry("net.tls", "0.3.0"))
		result = resolve_artifact("myapp", [("net.tls", "^0.3.0")], idx)
		assert "net.tls" in result
		assert result["net.tls"].version == "0.3.0"
		assert result["net.tls"].dep_type == "direct"

	def test_highest_satisfying_selected(self) -> None:
		idx = _index(
			_make_entry("net.tls", "0.3.0", sha="a0"),
			_make_entry("net.tls", "0.3.1", sha="a1"),
			_make_entry("net.tls", "0.3.2", sha="a2"),
		)
		result = resolve_artifact("myapp", [("net.tls", "^0.3.0")], idx)
		assert result["net.tls"].version == "0.3.2"

	def test_transitive_dep_resolved(self) -> None:
		idx = _index(
			_make_entry("net.tls", "0.3.0", deps=[("acme.crypto", "^0.9.0")]),
			_make_entry("acme.crypto", "0.9.1"),
		)
		result = resolve_artifact("myapp", [("net.tls", "^0.3.0")], idx)
		assert result["net.tls"].dep_type == "direct"
		assert result["acme.crypto"].dep_type == "transitive"
		assert result["acme.crypto"].version == "0.9.1"

	def test_no_version_satisfies(self) -> None:
		idx = _index(_make_entry("net.tls", "0.2.0"))
		with pytest.raises(ResolutionError, match="not satisfied"):
			resolve_artifact("myapp", [("net.tls", "^0.3.0")], idx)

	def test_package_not_found(self) -> None:
		with pytest.raises(ResolutionError, match="not satisfied"):
			resolve_artifact("myapp", [("net.tls", "^0.3.0")], {})


class TestResolverConflict:
	"""Dependency conflict is a hard build failure -- no partial lock."""

	def test_incompatible_transitive_constraints(self) -> None:
		idx = _index(
			_make_entry("net.tls", "0.3.0", deps=[("acme.crypto", "^0.9.0")]),
			_make_entry("net.http", "1.0.0", deps=[("acme.crypto", "^1.0.0")]),
			_make_entry("acme.crypto", "0.9.5"),
			_make_entry("acme.crypto", "1.1.0"),
		)
		with pytest.raises(ResolutionError) as exc:
			resolve_artifact(
				"myapp",
				[("net.tls", "^0.3.0"), ("net.http", "^1.0.0")],
				idx,
			)
		err = str(exc.value)
		assert "acme.crypto" in err
		assert "net.tls" in err or "net.http" in err

	def test_conflict_diagnostics_show_constraint_provenance(self) -> None:
		idx = _index(
			_make_entry("pkg.a", "1.0.0", deps=[("shared", "^1.0.0")]),
			_make_entry("pkg.b", "2.0.0", deps=[("shared", "^2.0.0")]),
			_make_entry("shared", "1.5.0"),
			_make_entry("shared", "2.0.0"),
		)
		with pytest.raises(ResolutionError) as exc:
			resolve_artifact(
				"myapp",
				[("pkg.a", "^1.0.0"), ("pkg.b", "^2.0.0")],
				idx,
			)
		err = str(exc.value)
		assert "shared" in err
		assert "pkg.a" in err
		assert "pkg.b" in err

	def test_direct_conflict(self) -> None:
		idx = _index(
			_make_entry("net.tls", "0.3.0"),
			_make_entry("net.tls", "1.0.0"),
		)
		with pytest.raises(ResolutionError):
			resolve_artifact(
				"myapp",
				[("net.tls", "0.3.0"), ("net.tls", "1.0.0")],
				idx,
			)


class TestResolverDeterminism:
	def test_lexicographic_order_independent(self) -> None:
		idx = _index(
			_make_entry("aaa", "1.0.0"),
			_make_entry("zzz", "1.0.0"),
		)
		r1 = resolve_artifact("app", [("aaa", "^1.0.0"), ("zzz", "^1.0.0")], idx)
		r2 = resolve_artifact("app", [("zzz", "^1.0.0"), ("aaa", "^1.0.0")], idx)
		assert r1 == r2
