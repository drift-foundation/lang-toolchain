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
		assert args.manifest == Path("drift-manifest.json")
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
		"""drift prepare resolves deps and writes drift-lock.json."""
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"ext.lib": ResolvedDep(version="1.0.0", integrity="sha256:aabb", dep_type="direct"),
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
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
					"package_deps": [{"name": "ext.lib", "version": "^1.0.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock_path = Path(tmpdir) / "drift-lock.json"
			assert lock_path.exists()
			lock = read_lock(lock_path)
			assert "my.pkg" in lock
			assert "ext.lib" in lock["my.pkg"]
			assert lock["my.pkg"]["ext.lib"].version == "1.0.0"

	def test_no_deps_is_noop(self) -> None:
		"""drift prepare with no package_deps does nothing."""
		with tempfile.TemporaryDirectory() as tmpdir:
			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
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

			lock_path = Path(tmpdir) / "drift-lock.json"
			assert not lock_path.exists()

	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_overwrites_existing_lock(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		"""Re-running prepare overwrites the existing lock."""
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"ext.lib": ResolvedDep(version="2.0.0", integrity="sha256:ccdd", dep_type="direct"),
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
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
					"package_deps": [{"name": "ext.lib", "version": "^2.0.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			# Write an old lock.
			from tools.drift_deploy.lockfile import write_lock
			lock_path = Path(tmpdir) / "drift-lock.json"
			write_lock(lock_path, {"my.pkg": {
				"ext.lib": ResolvedDep(version="1.0.0", integrity="sha256:old", dep_type="direct"),
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
			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
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
					"package_deps": [{"name": "nonexistent.pkg", "version": "^1.0.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			with pytest.raises(PrepareError, match="not satisfied"):
				_run_impl(args)


class TestFullPrepareReplace:
	@patch("tools.drift_deploy.drift_prepare.build_package_index")
	@patch("tools.drift_deploy.drift_prepare.resolve_artifact")
	def test_prepare_replaces_entire_lock(
		self, mock_resolve: MagicMock, mock_index: MagicMock,
	) -> None:
		"""Prepare always replaces the full lock — stale entries are dropped."""
		mock_index.return_value = {}
		mock_resolve.return_value = {
			"dep.a": ResolvedDep(version="1.0.0", integrity="sha256:aa", dep_type="direct"),
		}
		with tempfile.TemporaryDirectory() as tmpdir:
			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package", "name": "pkg.a", "version": "0.1.0",
					"description": "test", "license": "MIT",
					"entry_module": "a.drift", "modules": ["a.drift"],
					"module_namespace": "pkg.a",
					"package_deps": [{"name": "dep.a", "version": "^1.0.0"}],
				}],
			}
			manifest_path.write_text(json.dumps(manifest))

			# Old lock with an artifact no longer in manifest.
			lock_path = Path(tmpdir) / "drift-lock.json"
			from tools.drift_deploy.lockfile import write_lock
			write_lock(lock_path, {"stale.pkg": {
				"dep.x": ResolvedDep(version="9.0.0", integrity="sha256:xx", dep_type="direct"),
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
			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
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
						"package_deps": [{"name": "web-jwt", "version": "^0.2.0"}],
					},
				],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock = read_lock(Path(tmpdir) / "drift-lock.json")
			assert "web-rest" in lock
			dep = lock["web-rest"]["web-jwt"]
			assert dep.version == "0.2.3"
			assert dep.dep_type == "co-artifact"
			assert dep.integrity == "sha256:co-artifact"

	def test_co_artifact_with_external_dep(self) -> None:
		"""Co-artifact + external dep: both resolved correctly."""
		with tempfile.TemporaryDirectory() as tmpdir:
			# Create a fake external package.
			pkg_root = Path(tmpdir) / "pkgs"
			pkg_root.mkdir()
			ext_dmp = pkg_root / "ext.crypto-1.0.0.dmp"
			ext_dmp.write_bytes(b"fake-dmp-content")

			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
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
							{"name": "my.auth", "version": "^0.1.0"},
							{"name": "ext.crypto", "version": "^1.0.0"},
						],
					},
				],
			}
			manifest_path.write_text(json.dumps(manifest))

			# Mock external package resolution; co-artifact should be synthetic.
			ext_entry = PackageEntry(
				package_id="ext.crypto", version=parse_version("1.0.0"),
				path=ext_dmp, sha256="aabbccdd", package_deps=[],
			)
			with patch("tools.drift_deploy.drift_prepare.build_package_index") as mock_idx:
				mock_idx.return_value = {"ext.crypto": [ext_entry]}

				p = build_arg_parser()
				args = p.parse_args(["--manifest", str(manifest_path)])
				rc = _run_impl(args)
				assert rc == 0

			lock = read_lock(Path(tmpdir) / "drift-lock.json")
			assert "my.app" in lock
			# Co-artifact dep.
			auth_dep = lock["my.app"]["my.auth"]
			assert auth_dep.dep_type == "co-artifact"
			assert auth_dep.version == "0.1.0"
			# External dep.
			crypto_dep = lock["my.app"]["ext.crypto"]
			assert crypto_dep.dep_type == "direct"
			assert crypto_dep.version == "1.0.0"
			assert crypto_dep.integrity == "sha256:aabbccdd"

	def test_co_artifact_version_mismatch_errors(self) -> None:
		"""Co-artifact exists but version doesn't satisfy constraint."""
		with tempfile.TemporaryDirectory() as tmpdir:
			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
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
						"package_deps": [{"name": "web-jwt", "version": "^0.2.0"}],
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
			manifest_path = Path(tmpdir) / "drift-manifest.json"
			manifest = {
				"schema_version": 1,
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
						"package_deps": [{"name": "my.base", "version": "^0.1.0"}],
					},
					{
						"kind": "app", "name": "my.app", "version": "0.1.0",
						"description": "app", "license": "MIT",
						"entry_module": "app.drift", "modules": ["app.drift"],
						"module_namespace": "my.app",
						"package_deps": [{"name": "my.mid", "version": "^0.1.0"}],
					},
				],
			}
			manifest_path.write_text(json.dumps(manifest))

			p = build_arg_parser()
			args = p.parse_args(["--manifest", str(manifest_path)])
			rc = _run_impl(args)
			assert rc == 0

			lock = read_lock(Path(tmpdir) / "drift-lock.json")
			assert "my.app" in lock
			app_deps = lock["my.app"]
			# Direct co-artifact dep.
			assert app_deps["my.mid"].dep_type == "co-artifact"
			# Transitive co-artifact dep (my.mid depends on my.base).
			assert app_deps["my.base"].dep_type == "co-artifact"
			assert app_deps["my.base"].version == "0.1.0"
