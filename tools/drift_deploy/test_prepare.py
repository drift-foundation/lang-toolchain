# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for drift prepare."""

from __future__ import annotations

import hashlib
import json
import tempfile
from lang.test_support.drift_tmp import session_root
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.drift_deploy.drift_prepare import (
	PrepareError,
	build_arg_parser,
	_run_impl,
)
from tools.drift_deploy.lockfile import read_lock
from tools.drift_deploy.manifest import (
	Artifact,
	Manifest,
	PackageDep,
	Project,
)
from tools.drift_deploy.resolver import PackageEntry, ResolvedDep
from tools.drift_deploy.semver import parse_version


def _drift_subdir(tmpdir) -> Path:
	"""Create and return ``<tmpdir>/drift`` for staging drift-owned metadata."""
	d = Path(tmpdir) / "drift"
	d.mkdir(exist_ok=True)
	return d


def _art(name: str, *, kind: str = "package", deps: list[PackageDep] | None = None) -> Artifact:
	return Artifact(
		kind=kind, name=name, version="0.1.0",
		description="", license="MIT", entry_module=f"{name}.drift",
		modules=[f"{name}.drift"], module_namespace=name.replace("-", "_"),
		package_deps=deps or [],
	)


class TestCLI:
	def test_defaults(self) -> None:
		p = build_arg_parser()
		args = p.parse_args([])
		assert args.manifest == Path("drift") / "manifest.json"
		assert args.dest is None
		assert args.package_root is None

	def test_all_flags(self) -> None:
		p = build_arg_parser()
		args = p.parse_args([
			"--manifest", "custom.json",
			"--dest", "/deploy",
			"--package-root", "/pr1",
			"--package-root", "/pr2",
		])
		assert args.manifest == Path("custom.json")
		assert args.dest == Path("/deploy")
		assert args.package_root == [Path("/pr1"), Path("/pr2")]


class TestPrepareResolve:
	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_resolves_and_writes_lock(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		"""drift prepare resolves deps and writes drift/lock.json."""
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"ext.lib": ResolvedDep(version="1.0.0", sha256="aabbcc", dep_type="direct", package_id="ext.lib", author_key="ed25519:test", source_content_id="sha256:" + "a"*64, source_attestation_key="ed25519:test"),
		}
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "my.pkg",
					"version": "0.1.0",
					"description": "test",
					"license": "MIT",
					"entry_module": "my/pkg.drift",
					"modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
					"package_deps": [{"name": "ext.lib", "version": "1.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock_path = _drift_subdir(tmpdir) / "lock.json"
			assert lock_path.exists()
			lock = read_lock(lock_path)
			assert "my.pkg" in lock
			assert "ext.lib" in lock["my.pkg"]
			assert lock["my.pkg"]["ext.lib"].version == "1.0.0"

	def test_no_deps_is_noop(self) -> None:
		"""drift prepare with no package_deps does nothing."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "my.pkg",
					"version": "0.1.0",
					"description": "test",
					"license": "MIT",
					"entry_module": "my/pkg.drift",
					"modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock_path = _drift_subdir(tmpdir) / "lock.json"
			assert not lock_path.exists()

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_overwrites_existing_lock(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		"""Re-running prepare overwrites the existing lock."""
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"ext.lib": ResolvedDep(version="2.0.0", sha256="aabbcc", dep_type="direct", package_id="ext.lib", author_key="ed25519:test", source_content_id="sha256:" + "a"*64, source_attestation_key="ed25519:test"),
		}
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "my.pkg",
					"version": "0.1.0",
					"description": "test",
					"license": "MIT",
					"entry_module": "my/pkg.drift",
					"modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
					"package_deps": [{"name": "ext.lib", "version": "2.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			# Write an old lock.
			from tools.drift_deploy.lockfile import write_lock
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			write_lock(lock_path, {"my.pkg": {
				"ext.lib": ResolvedDep(version="1.0.0", sha256="aabbcc", dep_type="direct", package_id="ext.lib", author_key="ed25519:test", source_content_id="sha256:" + "a"*64, source_attestation_key="ed25519:test"),
			}})

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock = read_lock(lock_path)
			assert lock["my.pkg"]["ext.lib"].version == "2.0.0"

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	def test_resolution_error_raises_prepare_error(
		self, mock_index: MagicMock,
	) -> None:
		"""Resolution failure propagates as PrepareError."""
		mock_index.return_value = {}  # empty index → resolution will fail
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "app",
					"name": "my.app",
					"version": "0.1.0",
					"description": "test",
					"license": "MIT",
					"entry_module": "my/app.drift",
					"modules": ["my/app.drift"],
					"module_namespace": "my.app",
					"package_deps": [{"name": "nonexistent.pkg", "version": "1.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			with pytest.raises(PrepareError, match="not satisfied"):
				_run_impl(args)

	def test_transitive_lock_narrows_to_highest_in_declared_range(self) -> None:
		"""`drift prepare` resolves a transitive pulled in by a direct
		dep's `required_deps` to the HIGHEST available version that
		still satisfies the owner-declared range — never crossing the
		range boundary.

		Direct replacement for the pre-0.29 driver-layer
		"narrows-to-declared" fixture:

		- On-disk: deplib 0.2.0, 0.2.14, 0.3.0; mylib 1.0.0 with
		  `required_deps: deplib = "0.2"`.
		- App manifest: `package_deps: mylib = "1.0"`.

		Expected lock: `deplib = 0.2.14` (highest in `"0.2"`), NOT
		`0.3.0` (outside the declared range).  This is the resolver
		contract that driftc relies on — driftc itself is an exact
		loader and never picks versions; `drift prepare` owns that
		decision.
		"""
		# v4 fixtures: every PackageEntry that flows through write_lock
		# needs the source-identity half populated, otherwise read_lock
		# rejects the resulting v4 lock entry.
		_scid = "sha256:" + "a"*64
		_sak = "ed25519:test"
		deplib_020 = PackageEntry(
			package_id="deplib", version=parse_version("0.2.0"),
			path=Path("/fake/deplib-0.2.0.dmp"), sha256="d020",
			required_deps=[], author_key="ed25519:test",
			source_content_id=_scid, source_attestation_key=_sak,
		)
		deplib_0214 = PackageEntry(
			package_id="deplib", version=parse_version("0.2.14"),
			path=Path("/fake/deplib-0.2.14.dmp"), sha256="d0214",
			required_deps=[], author_key="ed25519:test",
			source_content_id=_scid, source_attestation_key=_sak,
		)
		deplib_030 = PackageEntry(
			package_id="deplib", version=parse_version("0.3.0"),
			path=Path("/fake/deplib-0.3.0.dmp"), sha256="d030",
			required_deps=[], author_key="ed25519:test",
			source_content_id=_scid, source_attestation_key=_sak,
		)
		mylib = PackageEntry(
			package_id="mylib", version=parse_version("1.0.0"),
			path=Path("/fake/mylib-1.0.0.dmp"), sha256="m100",
			required_deps=[("deplib", "0.2")], author_key="ed25519:test",
			source_content_id=_scid, source_attestation_key=_sak,
		)
		index = {
			"deplib": [deplib_020, deplib_0214, deplib_030],
			"mylib": [mylib],
		}

		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "app", "name": "my.app", "version": "0.1.0",
					"description": "app", "license": "MIT",
					"entry_module": "app.drift", "modules": ["app.drift"],
					"module_namespace": "my.app",
					"package_deps": [{"name": "mylib", "version": "1.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			with patch("tools.drift_deploy.drift_prepare.build_package_index") as mock_idx:
				mock_idx.return_value = index
				p = build_arg_parser()
				args = p.parse_args(["--manifest", str(manifest_path)])
				rc = _run_impl(args)
				assert rc == 0

			lock = read_lock(_drift_subdir(tmpdir) / "lock.json")
			assert "my.app" in lock
			app_deps = lock["my.app"]
			assert app_deps["deplib"].version == "0.2.14", (
				f"expected deplib narrowed to 0.2.14 (highest in declared "
				f"range \"0.2\"), got {app_deps['deplib'].version}.  If "
				f"this regresses, either the resolver crossed the range "
				f"boundary (picking 0.3.0) or failed to pick the highest "
				f"in-range patch (picking 0.2.0)."
			)
			assert app_deps["deplib"].dep_type == "transitive"
			assert app_deps["mylib"].version == "1.0.0"


class TestFullPrepareReplace:
	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_prepare_replaces_entire_lock(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		"""Prepare always replaces the full lock — stale entries are dropped."""
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"dep.a": ResolvedDep(version="1.0.0", sha256="aabbcc", dep_type="direct", package_id="dep.a", author_key="ed25519:test", source_content_id="sha256:" + "a"*64, source_attestation_key="ed25519:test"),
		}
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package", "name": "pkg.a", "version": "0.1.0",
					"description": "test", "license": "MIT",
					"entry_module": "a.drift", "modules": ["a.drift"],
					"module_namespace": "pkg.a",
					"package_deps": [{"name": "dep.a", "version": "1.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			# Old lock with an artifact no longer in manifest.
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			from tools.drift_deploy.lockfile import write_lock
			write_lock(lock_path, {"stale.pkg": {
				"dep.x": ResolvedDep(version="9.0.0", sha256="aabbcc", dep_type="direct", package_id="dep.x", author_key="ed25519:test", source_content_id="sha256:" + "a"*64, source_attestation_key="ed25519:test"),
			}})

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock = read_lock(lock_path)
			assert "stale.pkg" not in lock
			assert "pkg.a" in lock


class TestCoArtifactResolution:
	"""Co-artifacts in the same manifest satisfy each other's package_deps."""

	def test_co_artifact_resolves_without_published_dmp(self) -> None:
		"""web-rest depends on web-jwt; both in same manifest. No external packages needed."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "drift-web", "license": "MIT"},
				"artifacts": [
					{
						"kind": "package", "name": "web-jwt", "version": "0.2.3",
						"description": "JWT", "license": "MIT",
						"entry_module": "web/jwt.drift", "modules": ["web/jwt.drift"],
						"module_namespace": "web.jwt",
					},
					{
						"kind": "package", "name": "web-rest", "version": "0.2.3",
						"description": "REST", "license": "MIT",
						"entry_module": "web/rest.drift", "modules": ["web/rest.drift"],
						"module_namespace": "web.rest",
						"package_deps": [{"name": "web-jwt", "version": "0.2"}],
					},
				],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock = read_lock(_drift_subdir(tmpdir) / "lock.json")
			assert "web-rest" in lock
			dep = lock["web-rest"]["web-jwt"]
			assert dep.version == "0.2.3"
			assert dep.dep_type == "co-artifact"

	def test_co_artifact_with_external_dep(self) -> None:
		"""Co-artifact + external dep: both resolved correctly."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			# Create a fake external package.
			pkg_root = Path(tmpdir) / "pkgs"
			pkg_root.mkdir()
			ext_dmp = pkg_root / "ext.crypto-1.0.0.dmp"
			ext_dmp.write_bytes(b"fake-dmp-content")

			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [
					{
						"kind": "package", "name": "my.auth", "version": "0.1.0",
						"description": "auth", "license": "MIT",
						"entry_module": "auth.drift", "modules": ["auth.drift"],
						"module_namespace": "my.auth",
					},
					{
						"kind": "app", "name": "my.app", "version": "0.1.0",
						"description": "app", "license": "MIT",
						"entry_module": "app.drift", "modules": ["app.drift"],
						"module_namespace": "my.app",
						"package_deps": [
							{"name": "my.auth", "version": "0.1"},
							{"name": "ext.crypto", "version": "1.0"},
						],
					},
				],
			}
			manifest_path.write_text(json.dumps(manifest))

			# Mock external package resolution; co-artifact should be synthetic.
			ext_entry = PackageEntry(
				package_id="ext.crypto", version=parse_version("1.0.0"),
				path=ext_dmp, sha256="aabbccdd", required_deps=[],
				author_key="ed25519:test",
				source_content_id="sha256:" + "a"*64,
				source_attestation_key="ed25519:test",
			)
			with patch("tools.drift_deploy.drift_prepare.build_package_index") as mock_idx:
				mock_idx.return_value = {"ext.crypto": [ext_entry]}

				p = build_arg_parser()
				args = p.parse_args(["--manifest", str(manifest_path)])
				rc = _run_impl(args)
				assert rc == 0

			lock = read_lock(_drift_subdir(tmpdir) / "lock.json")
			assert "my.app" in lock
			# Co-artifact dep.
			auth_dep = lock["my.app"]["my.auth"]
			assert auth_dep.dep_type == "co-artifact"
			assert auth_dep.version == "0.1.0"
			# External dep.
			crypto_dep = lock["my.app"]["ext.crypto"]
			assert crypto_dep.dep_type == "direct"
			assert crypto_dep.version == "1.0.0"

	def test_co_artifact_version_mismatch_errors(self) -> None:
		"""Co-artifact exists but version doesn't satisfy constraint."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [
					{
						"kind": "package", "name": "web-jwt", "version": "0.1.0",
						"description": "JWT", "license": "MIT",
						"entry_module": "jwt.drift", "modules": ["jwt.drift"],
						"module_namespace": "web.jwt",
					},
					{
						"kind": "package", "name": "web-rest", "version": "0.2.0",
						"description": "REST", "license": "MIT",
						"entry_module": "rest.drift", "modules": ["rest.drift"],
						"module_namespace": "web.rest",
						"package_deps": [{"name": "web-jwt", "version": "0.2"}],
					},
				],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			with pytest.raises(PrepareError, match="not satisfied"):
				_run_impl(args)

	def test_co_artifact_transitive_deps(self) -> None:
		"""Co-artifact's own package_deps are resolved transitively."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest = {
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [
					{
						"kind": "package", "name": "my.base", "version": "0.1.0",
						"description": "base", "license": "MIT",
						"entry_module": "base.drift", "modules": ["base.drift"],
						"module_namespace": "my.base",
					},
					{
						"kind": "package", "name": "my.mid", "version": "0.1.0",
						"description": "mid", "license": "MIT",
						"entry_module": "mid.drift", "modules": ["mid.drift"],
						"module_namespace": "my.mid",
						"package_deps": [{"name": "my.base", "version": "0.1"}],
					},
					{
						"kind": "app", "name": "my.app", "version": "0.1.0",
						"description": "app", "license": "MIT",
						"entry_module": "app.drift", "modules": ["app.drift"],
						"module_namespace": "my.app",
						"package_deps": [{"name": "my.mid", "version": "0.1"}],
					},
				],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock = read_lock(_drift_subdir(tmpdir) / "lock.json")
			assert "my.app" in lock
			app_deps = lock["my.app"]
			# Direct co-artifact dep.
			assert app_deps["my.mid"].dep_type == "co-artifact"
			# Transitive co-artifact dep (my.mid depends on my.base).
			assert app_deps["my.base"].dep_type == "co-artifact"
			assert app_deps["my.base"].version == "0.1.0"


class TestPrepareCheck:
	"""`drift prepare --check` verifies the lock is up-to-date without
	mutating the working tree.  Used by CI to catch stale locks."""

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_check_passes_when_lock_matches(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"ext.lib": ResolvedDep(
				version="1.0.0", sha256="aabbcc", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:test",
				source_content_id="sha256:" + "a"*64,
				source_attestation_key="ed25519:test",
			),
		}
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package", "name": "my.pkg", "version": "0.1.0",
					"description": "test", "license": "MIT",
					"entry_module": "my/pkg.drift", "modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
					"package_deps": [{"name": "ext.lib", "version": "1.0"}],
				}],
			}))
			# First run writes the lock.
			p = build_arg_parser()
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			# --check re-runs: same inputs → same resolution → matching lock → exit 0.
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path), "--check"])) == 0

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_check_fails_when_lock_stale(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		mock_index.return_value = {}
		# Two different resolutions (e.g. after an upstream patch bump).
		mock_resolve.side_effect = [
			{"ext.lib": ResolvedDep(version="1.0.0", sha256="aabbcc", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:test",
				source_content_id="sha256:" + "a"*64, source_attestation_key="ed25519:test")},
			{"ext.lib": ResolvedDep(version="1.0.5", sha256="ddeeff", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:test",
				source_content_id="sha256:" + "b"*64, source_attestation_key="ed25519:test")},
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package", "name": "my.pkg", "version": "0.1.0",
					"description": "test", "license": "MIT",
					"entry_module": "my/pkg.drift", "modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
					"package_deps": [{"name": "ext.lib", "version": "1.0"}],
				}],
			}))
			p = build_arg_parser()
			# First run writes a lock at 1.0.0.
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			# Second run (--check): resolution now says 1.0.5 → lock stale → exit 1.
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path), "--check"])) == 1

	def test_check_passes_with_co_artifact_after_fresh_prepare(self) -> None:
		"""Regression: `--check` immediately after `drift prepare` on a
		manifest with a co-artifact dep used to falsely report stale.
		Cause was an asymmetric `ResolvedDep` construction — the
		co-artifact override in `_run_impl` omitted `package_id`
		(defaulting to ""), while `read_lock` reconstructed it with the
		map key.  Two dicts that serialised identically compared
		unequal.  drift-web hit this because its manifest has co-
		artifacts (web-jwt is a co-artifact of web-rest, etc.), which
		blocked them from wiring `drift prepare --check` into CI."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "drift-web", "license": "MIT"},
				"artifacts": [
					{
						"kind": "package", "name": "web-jwt", "version": "0.2.3",
						"description": "JWT", "license": "MIT",
						"entry_module": "web/jwt.drift", "modules": ["web/jwt.drift"],
						"module_namespace": "web.jwt",
					},
					{
						"kind": "package", "name": "web-rest", "version": "0.2.3",
						"description": "REST", "license": "MIT",
						"entry_module": "web/rest.drift", "modules": ["web/rest.drift"],
						"module_namespace": "web.rest",
						"package_deps": [{"name": "web-jwt", "version": "0.2"}],
					},
				],
			}))
			p = build_arg_parser()
			# Fresh prepare writes the lock.
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			lock_before = (_drift_subdir(tmpdir) / "lock.json").read_bytes()
			# --check must accept its own freshly-written lock.
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path), "--check"])) == 0
			# And must not have touched the file.
			lock_after = (_drift_subdir(tmpdir) / "lock.json").read_bytes()
			assert lock_before == lock_after

	def test_check_fails_when_lock_absent(self) -> None:
		"""--check without a lock on disk is a hard error, not a
		silent success.  The contract is "lock matches current
		resolution"; "no lock" is not "matches"."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package", "name": "my.pkg", "version": "0.1.0",
					"description": "test", "license": "MIT",
					"entry_module": "my/pkg.drift", "modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
				}],
			}))
			p = build_arg_parser()
			# No package_deps, so no resolution needed — but the
			# artifact also produced no lock, and --check is stricter:
			# we want prepare's behavior to mirror what --check asserts.
			# The no-deps path returns 0 without writing a lock, so
			# --check on a no-deps manifest also returns 0 (both sides
			# consistent).  Add a dep to force the lock-required path.
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package", "name": "my.pkg", "version": "0.1.0",
					"description": "test", "license": "MIT",
					"entry_module": "my/pkg.drift", "modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
					"package_deps": [{"name": "ext.lib", "version": "1.0"}],
				}],
			}))
			with patch("tools.drift_deploy.drift_prepare.build_package_index") as mi, \
				 patch("tools.drift_deploy.drift_prepare.resolve_artifact") as mr:
				mi.return_value = {}
				mr.return_value = {
					"ext.lib": ResolvedDep(version="1.0.0", sha256="aa", dep_type="direct",
						package_id="ext.lib", author_key="ed25519:test",
						source_content_id="sha256:" + "a"*64, source_attestation_key="ed25519:test"),
				}
				assert _run_impl(p.parse_args(["--manifest", str(manifest_path), "--check"])) == 1


class TestPrepareSourceAttestationGate:
	"""Phase B.1 trust gate: drift prepare must refuse to write a v4 lock
	whose non-co-artifact resolved deps lack a valid source attestation
	(missing sidecar, mismatched sidecar, or bad signature — all
	collapse to empty source identity at the resolver layer).  Without
	this fail-fast, drift prepare would emit a v4 lock that read_lock
	rejects on the next consume — the user would see the failure two
	steps removed from the cause."""

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_missing_attestation_for_direct_dep_fails_prepare(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		"""Resolved direct dep with empty source identity → PrepareError
		naming the package and pointing at republish."""
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"ext.lib": ResolvedDep(
				version="1.0.0", sha256="aabbcc", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:test",
				source_content_id="",  # missing → fail
				source_attestation_key="",
			),
		}
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package", "name": "my.pkg", "version": "0.1.0",
					"description": "test", "license": "MIT",
					"entry_module": "my/pkg.drift", "modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
					"package_deps": [{"name": "ext.lib", "version": "1.0"}],
				}],
			}))
			p = build_arg_parser()
			with pytest.raises(PrepareError) as exc:
				_run_impl(p.parse_args(["--manifest", str(manifest_path)]))
			msg = str(exc.value)
			assert "my.pkg -> ext.lib@1.0.0" in msg
			assert "republish" in msg.lower()
			assert "0.30.0" in msg
			# Lock file must NOT have been written.
			assert not (_drift_subdir(tmpdir) / "lock.json").exists()

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_missing_attestation_only_signer_kid_fails_prepare(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		"""Half-populated source identity (scid present, kid missing)
		also fails — both are required to anchor the trust chain."""
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"ext.lib": ResolvedDep(
				version="1.0.0", sha256="aabbcc", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:test",
				source_content_id="sha256:" + "a"*64,
				source_attestation_key="",  # half-empty → fail
			),
		}
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package", "name": "my.pkg", "version": "0.1.0",
					"description": "test", "license": "MIT",
					"entry_module": "my/pkg.drift", "modules": ["my/pkg.drift"],
					"module_namespace": "my.pkg",
					"package_deps": [{"name": "ext.lib", "version": "1.0"}],
				}],
			}))
			p = build_arg_parser()
			with pytest.raises(PrepareError, match="ext.lib"):
				_run_impl(p.parse_args(["--manifest", str(manifest_path)]))

	def test_co_artifact_dep_does_not_require_attestation(self) -> None:
		"""Co-artifacts legitimately have empty source identity at
		prepare time (the .source-attestation hasn't been built yet —
		it's emitted later in the same deploy run).  The fail-fast
		gate must skip them by `dep_type == "co-artifact"`."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "drift-web", "license": "MIT"},
				"artifacts": [
					{
						"kind": "package", "name": "web-jwt", "version": "0.2.3",
						"description": "JWT", "license": "MIT",
						"entry_module": "web/jwt.drift", "modules": ["web/jwt.drift"],
						"module_namespace": "web.jwt",
					},
					{
						"kind": "package", "name": "web-rest", "version": "0.2.3",
						"description": "REST", "license": "MIT",
						"entry_module": "web/rest.drift", "modules": ["web/rest.drift"],
						"module_namespace": "web.rest",
						"package_deps": [{"name": "web-jwt", "version": "0.2"}],
					},
				],
			}))
			p = build_arg_parser()
			rc = _run_impl(p.parse_args(["--manifest", str(manifest_path)]))
			assert rc == 0  # co-artifact deps with empty source identity OK
			lock = read_lock(_drift_subdir(tmpdir) / "lock.json")
			assert lock["web-rest"]["web-jwt"].dep_type == "co-artifact"
			assert lock["web-rest"]["web-jwt"].source_content_id == ""
			assert lock["web-rest"]["web-jwt"].source_attestation_key == ""


@pytest.mark.usefixtures("permissive_run_snapshot")
class TestPrepareCheckSourceRebuild:
	"""`drift prepare --check --source-rebuild` (and the matching
	`DRIFT_CERT_MODE=certify` env var) relaxes the comparison so a
	rebuilt-elsewhere artifact whose bytes/signer drifted but whose
	source identity (and everything else) still matches the lock passes
	check.  Needed because drift-web's `just test` runs lock-check via
	`drift prepare --check` and the source-rebuild certification lane
	legitimately produces new `.dmp` bytes + new `.sig` signer keys
	on every rebuild.  Default `--check` stays strict/exact."""

	_MANIFEST = {
		"schema_version": 2,
		"project": {"name": "test", "license": "MIT"},
		"artifacts": [{
			"kind": "package", "name": "my.pkg", "version": "0.1.0",
			"description": "test", "license": "MIT",
			"entry_module": "my/pkg.drift", "modules": ["my/pkg.drift"],
			"module_namespace": "my.pkg",
			"package_deps": [{"name": "ext.lib", "version": "1.0"}],
		}],
	}

	def _write_manifest(self, tmpdir) -> Path:
		p = _drift_subdir(tmpdir) / "manifest.json"
		p.write_text(json.dumps(self._MANIFEST))
		return p

	_SCID = "sha256:" + "a"*64

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.source_rebuild.resolve_artifact")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_source_rebuild_tolerates_sha_and_author_key_drift(
		self, mock_resolve: MagicMock, mock_sr_resolve: MagicMock,
		mock_index: MagicMock, capsys,
	) -> None:
		"""The committed lock pins sha=AAA / author=key-A; the resolver
		now sees a rebuilt artifact at sha=BBB / author=key-B but with
		the SAME version / dep_type / source_content_id /
		source_attestation_key — `--check --source-rebuild` passes and
		logs the drift as evidence."""
		mock_index.return_value = {}
		# Write + strict --check use drift_prepare.resolve_artifact.
		mock_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-A",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="BBB", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-B",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		# --check --source-rebuild uses source_rebuild.resolve_artifact
		# (the single authority dispatches there).
		mock_sr_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="BBB", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-B",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			# Write lock.
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			# --check (strict) should FAIL because sha/author drifted.
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path), "--check"])) == 1
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check", "--source-rebuild",
			])) == 0
			out = capsys.readouterr().out
			# 0.31.1 unified format: per-artifact evidence block
			# produced by `source_rebuild.print_evidence`.
			assert "drift vs. lock" in out
			assert "my.pkg" in out and "ext.lib" in out
			assert "sha256" in out
			assert "'AAA'" in out and "'BBB'" in out
			assert "author_key" in out
			assert "up-to-date" in out
			assert "source-rebuild" in out

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.source_rebuild.resolve_artifact")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_source_rebuild_accepts_source_content_id_drift_as_evidence(
		self, mock_resolve: MagicMock, mock_sr_resolve: MagicMock,
		mock_index: MagicMock, capsys,
	) -> None:
		"""Source-rebuild mode logs `source_content_id` drift as
		evidence and passes `--check`.  Rationale: orch's run-all-
		latest.json selects source commits for every member of the
		graph, so a compatible upstream patch landing in a dep
		legitimately shifts its scid without the downstream having
		touched its own lock.  The trust anchor for source-rebuild is
		the trust store's namespace allowlist (owner-continuity),
		which is verified at package-index time, NOT per-dep scid
		equality with the downstream lock.  Re-introducing an scid
		hard-gate here would stale every downstream lock on every
		compatible upstream patch — the exact Lock-v2 contract
		violation the 0.31.1 semantics fix reverses."""
		mock_index.return_value = {}
		scid_a = "sha256:" + "a"*64
		scid_b = "sha256:" + "b"*64
		# Write path: drift_prepare.resolve_artifact
		mock_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-A",
				source_content_id=scid_a,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		# --check --source-rebuild: source_rebuild.resolve_artifact
		mock_sr_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="BBB", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-B",
				source_content_id=scid_b,  # <- drifted
				source_attestation_key="ed25519:attest-key",
			)},
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check", "--source-rebuild",
			])) == 0
			out = capsys.readouterr().out
			assert "source_content_id" in out
			assert "ext.lib" in out
			assert scid_a in out and scid_b in out
			assert "up-to-date" in out

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.source_rebuild.resolve_artifact")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_source_rebuild_accepts_source_attestation_key_drift_as_evidence(
		self, mock_resolve: MagicMock, mock_sr_resolve: MagicMock,
		mock_index: MagicMock, capsys,
	) -> None:
		"""Source-rebuild mode logs `source_attestation_key` drift as
		evidence and passes `--check`.  Trust-root substitution (an
		unauthorised key re-signing a package) is caught at package-
		index time by `signature_v0.py::verify_package_signatures`
		against the trust store's namespace allowlist — NOT by
		per-dep lock equality here.  Enforcing equality at the
		downstream lock would mean every legitimate upstream rotation
		(kid retirement with a still-allowlisted successor) stales
		every downstream lock, violating the Lock-v2 compatible-patch
		contract."""
		mock_index.return_value = {}
		mock_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-A",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-A",
			)},
		]
		mock_sr_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="BBB", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-B",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-B",  # <- drifted
			)},
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check", "--source-rebuild",
			])) == 0
			out = capsys.readouterr().out
			assert "source_attestation_key" in out
			assert "ed25519:attest-A" in out
			assert "ed25519:attest-B" in out
			assert "up-to-date" in out

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.source_rebuild.resolve_artifact")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_source_rebuild_accepts_version_drift_as_evidence(
		self, mock_resolve: MagicMock, mock_sr_resolve: MagicMock,
		mock_index: MagicMock, capsys,
	) -> None:
		"""Source-rebuild mode logs `version` drift as evidence and
		passes `--check`.  Version is enforced-in-range at resolver
		time (the resolver wouldn't produce a graph if the downstream
		manifest's range didn't satisfy), so a shift from 1.0.0 to
		1.0.1 inside the locked range is a compatible upstream patch
		orch legitimately selected via run-all-latest.json.  The
		Lock-v2 contract explicitly decouples the lock's exact-
		version pin from artifact/source pinning for the source-
		rebuild lane."""
		mock_index.return_value = {}
		mock_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-A",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		mock_sr_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.1", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-A",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check", "--source-rebuild",
			])) == 0
			out = capsys.readouterr().out
			# New format: `~ ext.lib: version 1.0.0 -> 1.0.1`
			assert "version" in out
			assert "1.0.0" in out and "1.0.1" in out
			assert "up-to-date" in out

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.source_rebuild.resolve_artifact")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_source_rebuild_accepts_transitive_dep_set_drift_as_evidence(
		self, mock_resolve: MagicMock, mock_sr_resolve: MagicMock,
		mock_index: MagicMock, capsys,
	) -> None:
		"""Policy as of 0.31.1 (reviewer gate #2): transitive dep-
		set drift (new transitive dep appears, or old one disappears)
		is EVIDENCE in source-rebuild mode, not a hard failure.  A
		compatible upstream patch that adds / removes its transitive
		deps legitimately shifts the downstream's graph; gating on
		that would re-introduce the "every upstream patch stales
		every downstream lock" churn the 0.31.1 alignment is
		specifically meant to eliminate.  The resolver has already
		enforced every selected version against consumer ranges
		(direct deps) and producer `required_deps` (transitive), so
		a dep-set change here is upstream graph movement permitted
		by those ranges."""
		mock_index.return_value = {}
		mock_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-A",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		mock_sr_resolve.side_effect = [
			{
				"ext.lib": ResolvedDep(
					version="1.0.0", sha256="AAA", dep_type="direct",
					package_id="ext.lib", author_key="ed25519:key-A",
					source_content_id=self._SCID,
					source_attestation_key="ed25519:attest-key",
				),
				"ext.extra": ResolvedDep(  # <- new transitive
					version="0.5.0", sha256="CCC", dep_type="transitive",
					package_id="ext.extra", author_key="ed25519:key-A",
					source_content_id=self._SCID,
					source_attestation_key="ed25519:attest-key",
				),
			},
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check", "--source-rebuild",
			])) == 0
			out = capsys.readouterr().out
			# New format: `+ ext.extra@0.5.0 (new in resolved graph)`
			assert "new in resolved graph" in out
			assert "ext.extra" in out
			assert "up-to-date" in out

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.source_rebuild.resolve_artifact")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_source_rebuild_via_cert_mode_certify(
		self, mock_resolve: MagicMock, mock_sr_resolve: MagicMock,
		mock_index: MagicMock, capsys, monkeypatch,
	) -> None:
		"""`DRIFT_CERT_MODE=certify` (no `--source-rebuild` flag)
		switches `--check` into source-rebuild mode.  Regression #3:
		this is the path orch uses for source-from-commit
		certification runs so downstream `just test` / lock-check
		doesn't need to thread the flag through every
		`drift prepare --check` invocation."""
		mock_index.return_value = {}
		mock_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-A",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		mock_sr_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="BBB", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-B",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
			# NOTE: no --source-rebuild on the CLI; env var alone.
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check",
			])) == 0
			out = capsys.readouterr().out
			assert "source-rebuild" in out
			# 0.31.1 unified format: per-artifact evidence block.
			assert "drift vs. lock" in out

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.source_rebuild.resolve_artifact")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_cert_mode_stage_engages_source_rebuild_with_exemption(
		self, mock_resolve: MagicMock, mock_sr_resolve: MagicMock,
		mock_index: MagicMock, capsys, monkeypatch,
	) -> None:
		"""Refined 0.31.5 contract: `DRIFT_CERT_MODE=stage` engages
		source-rebuild on --check (same as certify for consumed
		deps) AND carries the producer-output exemption.  The
		distinction between stage and certify is not whether
		source-rebuild fires — it always does under any cert mode
		— but whether intra-manifest co-artifacts skip the snapshot
		gate.

		Concrete check: --check under stage must accept the same
		bytes/signer drift that --check under certify accepts (test
		fixture feeds differing shas to each path and asserts the
		source-rebuild path wins)."""
		mock_index.return_value = {}
		mock_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-A",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		mock_sr_resolve.side_effect = [
			{"ext.lib": ResolvedDep(
				version="1.0.0", sha256="BBB", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:key-B",
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)},
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			monkeypatch.setenv("DRIFT_CERT_MODE", "stage")
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check",
			])) == 0, (
				"DRIFT_CERT_MODE=stage engages source-rebuild on --check: "
				"the mock_sr_resolve path (bytes drifted) should succeed"
			)
			out = capsys.readouterr().out
			assert "source-rebuild" in out
			assert "drift vs. lock" in out

	def test_source_rebuild_helper_matrix(self, monkeypatch) -> None:
		"""Unit pin for the uniform lane selector (prepare side),
		refined 0.31.5 contract:
		  - unset cert_mode, no flag → False (normal local)
		  - `stage`, no flag → True (source-rebuild + exemption)
		  - `certify`, no flag → True (source-rebuild, no exemption)
		  - CLI flag wins regardless of cert_mode
		  - invalid cert_mode → CertModeError
		  - DRIFT_SOURCE_REBUILD set → CertModeError"""
		import argparse as _ap
		from tools.drift_deploy.build_cmd import (
			CertModeError,
			producer_output_exemption_active,
		)
		from tools.drift_deploy.drift_prepare import _source_rebuild_enabled
		off = _ap.Namespace(source_rebuild=False)
		on = _ap.Namespace(source_rebuild=True)

		monkeypatch.delenv("DRIFT_SOURCE_REBUILD", raising=False)
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		assert _source_rebuild_enabled(off) is False
		assert _source_rebuild_enabled(on) is True
		assert producer_output_exemption_active() is False

		monkeypatch.setenv("DRIFT_CERT_MODE", "stage")
		assert _source_rebuild_enabled(off) is True
		assert _source_rebuild_enabled(on) is True
		assert producer_output_exemption_active() is True

		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		assert _source_rebuild_enabled(off) is True
		assert _source_rebuild_enabled(on) is True
		assert producer_output_exemption_active() is False

		monkeypatch.setenv("DRIFT_CERT_MODE", "bogus")
		with pytest.raises(CertModeError):
			_source_rebuild_enabled(off)

		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		monkeypatch.setenv("DRIFT_SOURCE_REBUILD", "1")
		with pytest.raises(CertModeError) as exc:
			_source_rebuild_enabled(off)
		assert "DRIFT_CERT_MODE" in str(exc.value)

		# Env-validation order: CLI flag must NOT short-circuit env
		# parsing — retired env and invalid cert mode still raise.
		with pytest.raises(CertModeError) as exc:
			_source_rebuild_enabled(on)
		assert "DRIFT_SOURCE_REBUILD" in str(exc.value)
		monkeypatch.delenv("DRIFT_SOURCE_REBUILD", raising=False)

		monkeypatch.setenv("DRIFT_CERT_MODE", "verify")  # retired spelling
		with pytest.raises(CertModeError) as exc:
			_source_rebuild_enabled(on)
		assert "'verify'" in str(exc.value)

	def test_strict_check_still_catches_sha_drift_regression_pin(self) -> None:
		"""Regression pin: default `--check` (no flag, no env) must
		still catch sha256 drift.  Protects the default contract
		from an accidental refactor that always routed through the
		source-rebuild comparator."""
		with patch("tools.drift_deploy.drift_prepare.build_package_index") as mi, \
			 patch("tools.drift_deploy.drift_prepare.resolve_artifact") as mr:
			mi.return_value = {}
			mr.side_effect = [
				{"ext.lib": ResolvedDep(
					version="1.0.0", sha256="AAA", dep_type="direct",
					package_id="ext.lib", author_key="ed25519:key-A",
					source_content_id=self._SCID,
					source_attestation_key="ed25519:attest-key",
				)},
				{"ext.lib": ResolvedDep(
					version="1.0.0", sha256="ZZZ",  # <- drifted
					dep_type="direct",
					package_id="ext.lib", author_key="ed25519:key-A",
					source_content_id=self._SCID,
					source_attestation_key="ed25519:attest-key",
				)},
			]
			with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
				manifest_path = self._write_manifest(tmpdir)
				p = build_arg_parser()
				assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
				assert _run_impl(p.parse_args([
					"--manifest", str(manifest_path), "--check",
				])) == 1

	# ── Trust-root gate (fix for review finding #1) ──────────────────

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.source_rebuild.resolve_artifact")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_source_rebuild_rejects_unsigned_direct_dep(
		self, mock_resolve: MagicMock, mock_sr_resolve: MagicMock,
		mock_index: MagicMock, capsys,
	) -> None:
		"""Pin: a direct dep with `author_key: "unsigned"` on BOTH
		sides of the comparison must NOT pass `--check --source-
		rebuild` — unsigned packages have no source attestation, so
		there is no trust root.  Without this gate, two symmetric
		empty-identity entries would pass the dict-equality check
		and the unsigned-opt-in would become a general source-rebuild
		bypass — the exact trap the review flagged."""
		mock_index.return_value = {}
		unsigned_dep = ResolvedDep(
			version="1.0.0", sha256="AAA", dep_type="direct",
			package_id="ext.lib", author_key="unsigned",
			source_content_id="",
			source_attestation_key="",
		)
		# Write + strict --check take drift_prepare.resolve_artifact;
		# --check --source-rebuild takes source_rebuild.resolve_artifact.
		# Both resolve to the same unsigned dep (resolver is
		# deterministic).
		mock_resolve.return_value = {"ext.lib": unsigned_dep}
		mock_sr_resolve.return_value = {"ext.lib": unsigned_dep}
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			# Write path: unsigned opt-in still allowed (the prepare-
			# time trust gate only fails on signed-but-empty identity).
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			# --check (strict) passes because the dicts match.
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check",
			])) == 0
			# --check --source-rebuild MUST reject — unsigned has no
			# signed trust root.
			assert _run_impl(p.parse_args([
				"--manifest", str(manifest_path), "--check", "--source-rebuild",
			])) == 1
			err = capsys.readouterr().err
			assert "unsigned" in err
			assert "ext.lib" in err

	# ── source-rebuild structural trust gates (direct unit pins) ──
	# Under the 0.31.1 resolve-driven model, `drift prepare --check
	# --source-rebuild` delegates to `source_rebuild.apply_
	# structural_trust_gates` for per-dep rejection logic.  These
	# tests pin the helper directly (the unified entry point both
	# --check and build/deploy share).  The helper's input is a
	# resolved-side `dict[pkg_id, ResolvedDep]`; the lock is no
	# longer consulted by these gates.

	def test_source_rebuild_rejects_empty_disk_source_identity(self) -> None:
		"""Policy: signed non-co-artifact deps must have non-empty
		`source_content_id` AND `source_attestation_key` on the
		RESOLVED (disk) side.  Empty identity means the disk
		package has no attestation for the trust gate to verify —
		a missing-sidecar regression, not legitimate drift."""
		from tools.drift_deploy.source_rebuild import apply_structural_trust_gates
		empty_identity_dep = ResolvedDep(
			version="1.0.0", sha256="AAA", dep_type="direct",
			package_id="ext.lib", author_key="ed25519:key-A",
			source_content_id="",
			source_attestation_key="",
		)
		errors: list = []
		apply_structural_trust_gates(
			"my.pkg", {"ext.lib": empty_identity_dep},
			co_artifact_names=set(), errors=errors,
		)
		assert errors, "empty disk source identity must hard-fail"
		assert any("empty `source_content_id`" in e for e in errors), errors
		assert any("empty `source_attestation_key`" in e for e in errors), errors

	def test_source_rebuild_rejects_unsigned_resolved_dep(self) -> None:
		"""Pin: `author_key == "unsigned"` OR empty author_key on the
		resolved side triggers the trust-root rejection — unsigned
		packages have no `.sig` for the owner-namespace gate to
		verify against."""
		from tools.drift_deploy.source_rebuild import apply_structural_trust_gates
		for bad_kid in ("unsigned", ""):
			unsigned_dep = ResolvedDep(
				version="1.0.0", sha256="AAA", dep_type="direct",
				package_id="ext.lib", author_key=bad_kid,
				source_content_id=self._SCID,
				source_attestation_key="ed25519:attest-key",
			)
			errors: list = []
			apply_structural_trust_gates(
				"my.pkg", {"ext.lib": unsigned_dep},
				co_artifact_names=set(), errors=errors,
			)
			assert errors, (
				f"author_key={bad_kid!r} must trigger trust-root "
				f"rejection; got: {errors}"
			)

	def test_source_rebuild_co_artifact_exempt_from_trust_gate(self) -> None:
		"""Regression pin: co-artifact entries legitimately carry
		empty source identity and must NOT trip the trust gate.
		Their `.dmp` + sidecars are built later in the same deploy
		run; their structural emptiness at `--check` time is
		expected.  Without this exemption, drift-web's co-artifact-
		heavy manifests would fail --check --source-rebuild."""
		from tools.drift_deploy.source_rebuild import apply_structural_trust_gates
		co = ResolvedDep(
			version="0.2.3", sha256="", dep_type="co-artifact",
			package_id="web-jwt", author_key="",
			source_content_id="",
			source_attestation_key="",
		)
		errors: list = []
		apply_structural_trust_gates(
			"web-rest", {"web-jwt": co},
			co_artifact_names={"web-jwt"}, errors=errors,
		)
		assert errors == [], (
			f"co-artifact with empty source identity must not trip "
			f"the trust-root gate; got: {errors}"
		)

	# ── --source-rebuild without --check is fail-fast (review #2) ────

	def test_source_rebuild_without_check_fails_fast(self, capsys) -> None:
		"""Pin: passing `--source-rebuild` to the lock-writing path
		(no `--check`) is rejected fail-fast.  `--source-rebuild` is
		a verification-lane selector; the lock is always authoritative
		and strict.  Accepting it on write would let orch / humans
		believe they'd regenerated a 'source-rebuild-aware' lock when
		in fact the flag was silently ignored — review finding #2."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = self._write_manifest(tmpdir)
			p = build_arg_parser()
			# No --check.  Should raise PrepareError before doing any
			# resolution — the lock file must NOT be written.
			with pytest.raises(PrepareError) as exc:
				_run_impl(p.parse_args([
					"--manifest", str(manifest_path), "--source-rebuild",
				]))
			msg = str(exc.value)
			assert "--check" in msg
			assert "verification-lane" in msg or "lock-writing" in msg
			assert not (_drift_subdir(tmpdir) / "lock.json").exists()

	def test_cert_mode_certify_without_check_is_silent_noop(
		self, monkeypatch,
	) -> None:
		"""Pin: `DRIFT_CERT_MODE=certify` on the write path is silently
		ignored, NOT fail-fast.  Distinction from the CLI-flag path:
		orch may legitimately export the env var for the whole
		certification environment, and `drift prepare` (write) may
		still be invoked in that env — erroring would force every
		repo-owned `just release` / write-step invocation to unset
		the var locally.  Only an explicit CLI flag is the conscious-
		intent signal that deserves a fail-fast."""
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		with patch("tools.drift_deploy.drift_prepare.build_package_index") as mi, \
			 patch("tools.drift_deploy.drift_prepare.resolve_artifact") as mr:
			mi.return_value = {}
			mr.return_value = {
				"ext.lib": ResolvedDep(
					version="1.0.0", sha256="AAA", dep_type="direct",
					package_id="ext.lib", author_key="ed25519:key-A",
					source_content_id=self._SCID,
					source_attestation_key="ed25519:attest-key",
				),
			}
			with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
				manifest_path = self._write_manifest(tmpdir)
				p = build_arg_parser()
				# No --check, no --source-rebuild on CLI, env var set:
				# write path succeeds (env var ignored here).
				rc = _run_impl(p.parse_args(["--manifest", str(manifest_path)]))
				assert rc == 0
				assert (_drift_subdir(tmpdir) / "lock.json").exists()

	def test_source_rebuild_co_artifact_symmetric_empty_identity(self) -> None:
		"""Co-artifact entries carry "" for both sha256 and source
		identity on BOTH sides of the --check comparison (they haven't
		been built yet; the lock's entry is synthesised by
		_run_impl's co-artifact override).  Source-rebuild mode must
		not log spurious "drift" for these."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			manifest_path = _drift_subdir(tmpdir) / "manifest.json"
			manifest_path.write_text(json.dumps({
				"schema_version": 2,
				"project": {"name": "drift-web", "license": "MIT"},
				"artifacts": [
					{
						"kind": "package", "name": "web-jwt", "version": "0.2.3",
						"description": "JWT", "license": "MIT",
						"entry_module": "web/jwt.drift", "modules": ["web/jwt.drift"],
						"module_namespace": "web.jwt",
					},
					{
						"kind": "package", "name": "web-rest", "version": "0.2.3",
						"description": "REST", "license": "MIT",
						"entry_module": "web/rest.drift", "modules": ["web/rest.drift"],
						"module_namespace": "web.rest",
						"package_deps": [{"name": "web-jwt", "version": "0.2"}],
					},
				],
			}))
			p = build_arg_parser()
			assert _run_impl(p.parse_args(["--manifest", str(manifest_path)])) == 0
			import io, contextlib
			buf = io.StringIO()
			with contextlib.redirect_stdout(buf):
				rc = _run_impl(p.parse_args([
					"--manifest", str(manifest_path), "--check", "--source-rebuild",
				]))
			assert rc == 0
			out = buf.getvalue()
			assert "drift vs. lock" not in out, (
				"co-artifact entries with symmetric empty sha/author_key "
				"must not be flagged as drift evidence"
			)
			assert "up-to-date" in out


class TestPrepareCheckSourceRebuildOrchEndToEnd:
	"""End-to-end orch-model regression pin (0.31.3).

	Unlike `TestPrepareCheckSourceRebuild`, this class does NOT
	apply the `permissive_run_snapshot` opt-in fixture — the
	regression requires the REAL snapshot-loading path to run so
	the downstream flow proves it consumes the snapshot the way
	orch ships it.

	The scenario mirrors orch's MariaDB failure case (2026-04-21)
	in flipped form — exactly the shape the earlier unit tests and
	mocked-CLI tests did NOT cover:

	  * Downstream repo's `drift/lock.json` pins `ext.lib@1.0.0`.
	  * Disk has `ext.lib@1.0.1` — a compatible upstream patch
	    inside the consumer manifest's `"1.0"` range.
	  * Orch's run snapshot authorises `ext.lib@1.0.1` with the
	    `(scid, author_key, source_attestation_key)` triple
	    orch verified at staging time.  It does NOT authorise
	    `ext.lib@1.0.0`.
	  * `drift prepare --check --source-rebuild` must succeed,
	    compile against `1.0.1` (fresh graph), and log the
	    version drift `1.0.0 → 1.0.1` as evidence vs. the lock.

	This test exercises the WHOLE downstream CLI flow:
	`build_package_index(run_snapshot=...)` runs for real and
	hits the snapshot gate; the resolver picks the highest in-
	range version; `--check` compares fresh vs. lock and logs
	evidence.  The boundary helpers `_read_author_key`,
	`_read_source_attestation_meta`, and the `.dmp` manifest
	loader are stubbed because real signed artifacts aren't
	available in a unit-test context — but `build_package_index`
	itself, the snapshot gate, the resolver, and the prepare
	comparator all run unpatched.
	"""

	def test_stale_lock_newer_compatible_snapshot_authorised(
		self, monkeypatch, tmp_path, capsys,
	) -> None:
		from types import SimpleNamespace
		from unittest.mock import patch as _patch
		from tools.drift_deploy.drift_prepare import (
			_run_impl,
			build_arg_parser,
		)
		from tools.drift_deploy.run_snapshot import (
			SnapshotEntry,
			write_run_snapshot,
		)

		# ── Downstream manifest: range "1.0" accepts any 1.0.x ──
		manifest_dir = tmp_path / "drift"
		manifest_dir.mkdir()
		(manifest_dir / "manifest.json").write_text(json.dumps({
			"schema_version": 2,
			"project": {"name": "downstream", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my.pkg", "version": "0.1.0",
				"description": "downstream", "license": "MIT",
				"entry_module": "my.drift", "modules": ["my.drift"],
				"module_namespace": "my.pkg",
				"package_deps": [{"name": "ext.lib", "version": "1.0"}],
			}],
		}))

		# ── Stale lock: pins ext.lib@1.0.0 (old) ──
		(manifest_dir / "lock.json").write_text(json.dumps({
			"schema_version": 4,
			"artifacts": {
				"my.pkg": {
					"resolved": {
						"ext.lib": {
							"version": "1.0.0",
							"sha256": "old-bytes",
							"author_key": "ed25519:orch-sig-kid",
							"source_content_id": "sha256:" + "0"*64,  # old scid
							"source_attestation_key": "ed25519:orch-sak-kid",
							"dep_type": "direct",
						},
					},
				},
			},
		}))

		# ── Disk: ext.lib@1.0.1 (newer compatible) ──
		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		new_dmp = pkg_root / "ext.lib-1.0.1.dmp"
		new_dmp.write_bytes(b"fake-ext-lib-1.0.1")
		# Placeholder .sig so `_read_author_key` can return non-empty
		# (it's patched anyway; file just needs to exist for the
		# parent directory walk to complete unambiguously).
		(pkg_root / "ext.lib-1.0.1.sig").write_text("{}")

		# ── Orch run snapshot: authorises ext.lib@1.0.1 ──
		new_scid = "sha256:" + "a"*64
		new_ak = "ed25519:orch-sig-kid"
		new_sak = "ed25519:orch-sak-kid"
		snapshot_path = tmp_path / "run-snapshot.json"
		write_run_snapshot(
			snapshot_path,
			run_id="20260421-orch-regression",
			entries={
				("ext.lib", "1.0.1"): SnapshotEntry(
					source_content_id=new_scid,
					author_key=new_ak,
					source_attestation_key=new_sak,
				),
				# NOTE: no entry for ext.lib@1.0.0 — orch only
				# staged the newer version.
			},
		)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snapshot_path))
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")

		# ── Stub the sidecar / manifest loaders so the fake .dmp
		# reads cleanly.  `build_package_index`, the snapshot
		# gate, and the resolver run unpatched.
		disk_manifest = {
			"package_id": "ext.lib",
			"package_version": "1.0.1",
			"modules": [{"module_id": "ext_lib"}],
			"required_deps": [],
		}
		fake_pkg = SimpleNamespace(manifest=disk_manifest)
		with _patch(
			"tools.drift_deploy.resolver._read_author_key",
			return_value=new_ak,
		), _patch(
			"tools.drift_deploy.resolver._read_source_attestation_meta",
			return_value=(new_scid, new_sak),
		), _patch(
			"lang.driftc.packages.dmir_pkg_v0.load_dmir_pkg_v0",
			return_value=fake_pkg,
		):
			parser = build_arg_parser()
			rc = _run_impl(parser.parse_args([
				"--manifest", str(manifest_dir / "manifest.json"),
				"--package-root", str(pkg_root),
				"--check",
			]))

		# ── Assertions ──
		out = capsys.readouterr().out
		assert rc == 0, (
			f"stale lock + newer in-range + snapshot-authorised "
			f"must succeed under --check --source-rebuild, got rc={rc}; "
			f"stdout: {out!r}"
		)
		# Version drift must appear as evidence (NOT an error).
		assert "ext.lib" in out
		assert "1.0.0" in out and "1.0.1" in out
		# Snapshot-gated run is up-to-date from the orch perspective
		# — the lock is evidence, not a gate.
		assert "up-to-date" in out

	def test_stale_lock_but_snapshot_does_not_authorise_newer(
		self, monkeypatch, tmp_path, capsys,
	) -> None:
		"""Negative pin: when the disk has a newer version but the
		snapshot does NOT authorise it, the check fails.  Prevents
		the in-range-resolver relaxation from silently accepting a
		package orch never staged (same-version source swap risk,
		but with version bump)."""
		from types import SimpleNamespace
		from unittest.mock import patch as _patch
		from tools.drift_deploy.drift_prepare import (
			_run_impl,
			build_arg_parser,
		)
		from tools.drift_deploy.run_snapshot import write_run_snapshot

		manifest_dir = tmp_path / "drift"
		manifest_dir.mkdir()
		(manifest_dir / "manifest.json").write_text(json.dumps({
			"schema_version": 2,
			"project": {"name": "downstream", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my.pkg", "version": "0.1.0",
				"description": "downstream", "license": "MIT",
				"entry_module": "my.drift", "modules": ["my.drift"],
				"module_namespace": "my.pkg",
				"package_deps": [{"name": "ext.lib", "version": "1.0"}],
			}],
		}))
		(manifest_dir / "lock.json").write_text(json.dumps({
			"schema_version": 4,
			"artifacts": {
				"my.pkg": {
					"resolved": {
						"ext.lib": {
							"version": "1.0.0",
							"sha256": "old-bytes",
							"author_key": "ed25519:orch-sig",
							"source_content_id": "sha256:" + "0"*64,
							"source_attestation_key": "ed25519:orch-sak",
							"dep_type": "direct",
						},
					},
				},
			},
		}))
		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		(pkg_root / "ext.lib-1.0.1.dmp").write_bytes(b"fake")
		(pkg_root / "ext.lib-1.0.1.sig").write_text("{}")

		# Snapshot is EMPTY — no entry for ext.lib at any version.
		snap_path = tmp_path / "snap.json"
		write_run_snapshot(snap_path, run_id="empty-snap", entries={})
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap_path))
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")

		disk_manifest = {
			"package_id": "ext.lib",
			"package_version": "1.0.1",
			"modules": [{"module_id": "ext_lib"}],
			"required_deps": [],
		}
		fake_pkg = SimpleNamespace(manifest=disk_manifest)
		with _patch(
			"tools.drift_deploy.resolver._read_author_key",
			return_value="ed25519:orch-sig",
		), _patch(
			"tools.drift_deploy.resolver._read_source_attestation_meta",
			return_value=("sha256:" + "a"*64, "ed25519:orch-sak"),
		), _patch(
			"lang.driftc.packages.dmir_pkg_v0.load_dmir_pkg_v0",
			return_value=fake_pkg,
		):
			parser = build_arg_parser()
			# Expect PrepareError: the disk package is not in the
			# snapshot; build_package_index raises ResolutionError
			# → wrapped as PrepareError.
			with pytest.raises(PrepareError) as exc:
				_run_impl(parser.parse_args([
					"--manifest", str(manifest_dir / "manifest.json"),
					"--package-root", str(pkg_root),
					"--check",
				]))
		msg = str(exc.value)
		assert "not present in run snapshot" in msg
		assert "ext.lib" in msg

	def test_cert_mode_certify_without_run_snapshot_hard_fails_check(
		self, monkeypatch, tmp_path,
	) -> None:
		"""Regression #4: `DRIFT_CERT_MODE=certify` on
		`drift prepare --check` WITHOUT either `--run-snapshot` or
		`DRIFT_RUN_SNAPSHOT` must fail cleanly — no silent fallback
		to downstream trust-store verification, no cryptic
		traceback.  Unit tests cover `resolve_source_rebuild(
		run_snapshot=None)` directly; this one proves the prepare
		CLI enforces the same rule end-to-end through `_run_impl`."""
		from tools.drift_deploy.drift_prepare import (
			_run_impl,
			build_arg_parser,
		)
		manifest_dir = tmp_path / "drift"
		manifest_dir.mkdir()
		(manifest_dir / "manifest.json").write_text(json.dumps({
			"schema_version": 2,
			"project": {"name": "downstream", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my.pkg", "version": "0.1.0",
				"description": "downstream", "license": "MIT",
				"entry_module": "my.drift", "modules": ["my.drift"],
				"module_namespace": "my.pkg",
				"package_deps": [{"name": "ext.lib", "version": "1.0"}],
			}],
		}))
		# Lock exists so --check doesn't fail earlier on lock-
		# absence; snapshot absence is the specific gate we're
		# pinning.
		(manifest_dir / "lock.json").write_text(json.dumps({
			"schema_version": 4,
			"artifacts": {"my.pkg": {"resolved": {}}},
		}))
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		parser = build_arg_parser()
		with pytest.raises(PrepareError) as exc:
			_run_impl(parser.parse_args([
				"--manifest", str(manifest_dir / "manifest.json"),
				"--check",
			]))
		msg = str(exc.value)
		assert "run snapshot" in msg
		assert "DRIFT_RUN_SNAPSHOT" in msg or "--run-snapshot" in msg
