# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for drift prepare."""

from __future__ import annotations

import hashlib
import json
import tempfile
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
			"ext.lib": ResolvedDep(version="1.0.0", sha256="aabbcc", dep_type="direct", package_id="ext.lib", author_key="ed25519:test"),
		}
		with tempfile.TemporaryDirectory() as tmpdir:
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
		with tempfile.TemporaryDirectory() as tmpdir:
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
			"ext.lib": ResolvedDep(version="2.0.0", sha256="aabbcc", dep_type="direct", package_id="ext.lib", author_key="ed25519:test"),
		}
		with tempfile.TemporaryDirectory() as tmpdir:
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
				"ext.lib": ResolvedDep(version="1.0.0", sha256="aabbcc", dep_type="direct", package_id="ext.lib", author_key="ed25519:test"),
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
		with tempfile.TemporaryDirectory() as tmpdir:
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
		deplib_020 = PackageEntry(
			package_id="deplib", version=parse_version("0.2.0"),
			path=Path("/fake/deplib-0.2.0.dmp"), sha256="d020",
			required_deps=[], author_key="ed25519:test",
		)
		deplib_0214 = PackageEntry(
			package_id="deplib", version=parse_version("0.2.14"),
			path=Path("/fake/deplib-0.2.14.dmp"), sha256="d0214",
			required_deps=[], author_key="ed25519:test",
		)
		deplib_030 = PackageEntry(
			package_id="deplib", version=parse_version("0.3.0"),
			path=Path("/fake/deplib-0.3.0.dmp"), sha256="d030",
			required_deps=[], author_key="ed25519:test",
		)
		mylib = PackageEntry(
			package_id="mylib", version=parse_version("1.0.0"),
			path=Path("/fake/mylib-1.0.0.dmp"), sha256="m100",
			required_deps=[("deplib", "0.2")], author_key="ed25519:test",
		)
		index = {
			"deplib": [deplib_020, deplib_0214, deplib_030],
			"mylib": [mylib],
		}

		with tempfile.TemporaryDirectory() as tmpdir:
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
			"dep.a": ResolvedDep(version="1.0.0", sha256="aabbcc", dep_type="direct", package_id="dep.a", author_key="ed25519:test"),
		}
		with tempfile.TemporaryDirectory() as tmpdir:
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
				"dep.x": ResolvedDep(version="9.0.0", sha256="aabbcc", dep_type="direct", package_id="dep.x", author_key="ed25519:test"),
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
		with tempfile.TemporaryDirectory() as tmpdir:
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
		with tempfile.TemporaryDirectory() as tmpdir:
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
		with tempfile.TemporaryDirectory() as tmpdir:
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
		with tempfile.TemporaryDirectory() as tmpdir:
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
			),
		}
		with tempfile.TemporaryDirectory() as tmpdir:
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
				package_id="ext.lib", author_key="ed25519:test")},
			{"ext.lib": ResolvedDep(version="1.0.5", sha256="ddeeff", dep_type="direct",
				package_id="ext.lib", author_key="ed25519:test")},
		]
		with tempfile.TemporaryDirectory() as tmpdir:
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

	def test_check_fails_when_lock_absent(self) -> None:
		"""--check without a lock on disk is a hard error, not a
		silent success.  The contract is "lock matches current
		resolution"; "no lock" is not "matches"."""
		with tempfile.TemporaryDirectory() as tmpdir:
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
						package_id="ext.lib", author_key="ed25519:test"),
				}
				assert _run_impl(p.parse_args(["--manifest", str(manifest_path), "--check"])) == 1
