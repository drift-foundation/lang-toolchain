# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for drift build — manifest-driven local artifact builds."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest import mock

import pytest

from tools.drift_deploy.build_cmd import (
	UserPath,
	build_app_cmd,
	build_package_cmd,
	build_source_args,
	resolve_driftc,
)
from tools.drift_deploy.manifest import Artifact, NativeDep, PackageDep
from tools.drift_deploy.resolver import ResolvedDep


# ── Fixtures ─────────────────────────────────────────────────────────


def _make_artifact(**overrides) -> Artifact:
	defaults = dict(
		kind="package",
		name="my-pkg",
		version="0.1.0",
		description="test",
		license="MIT",
		entry_module="src/lib.drift",
		modules=["src/lib.drift", "src/util.drift"],
		package_deps=[],
		native_deps=[],
		assets=[],
		smoke_command=None,
		unsafe=False,
		module_namespace="my_pkg",
		entry_point="",
	)
	defaults.update(overrides)
	return Artifact(**defaults)


MINIMAL_MANIFEST = {
	"schema_version": 1,
	"project": {"name": "test-project", "license": "MIT"},
	"artifacts": [
		{
			"kind": "package",
			"name": "my-pkg",
			"version": "0.1.0",
			"description": "A test package",
			"entry_module": "src/lib.drift",
			"modules": ["src/lib.drift", "src/util.drift"],
		}
	],
}


def _write_manifest(tmp_path: Path, data: dict | None = None) -> Path:
	drift_dir = tmp_path / "drift"
	drift_dir.mkdir(exist_ok=True)
	manifest_path = drift_dir / "manifest.json"
	manifest_path.write_text(
		json.dumps(data or MINIMAL_MANIFEST, indent=2),
		encoding="utf-8",
	)
	return manifest_path


def _write_lock(tmp_path: Path, artifacts: dict, *, author_key: str = "ed25519:test") -> Path:
	from tools.drift_deploy.resolver import version_compat_range
	drift_dir = tmp_path / "drift"
	drift_dir.mkdir(exist_ok=True)
	lock_path = drift_dir / "lock.json"
	lock_obj = {"schema_version": 2, "artifacts": {}}
	for art_name, deps in artifacts.items():
		resolved = {}
		for pkg_id, ver in deps.items():
			resolved[pkg_id] = {
				"version": version_compat_range(ver),
				"package_id": pkg_id,
				"author_key": author_key,
				"dep_type": "direct",
			}
		lock_obj["artifacts"][art_name] = {"resolved": resolved}
	lock_path.write_text(json.dumps(lock_obj, indent=2), encoding="utf-8")
	return lock_path



# ── build_source_args tests ──────────────────────────────────────────


class TestBuildSourceArgs:
	def test_entry_first_dedup(self):
		art = _make_artifact(
			entry_module="src/lib.drift",
			modules=["src/lib.drift", "src/util.drift"],
		)
		result = build_source_args(art, Path("/proj"))
		assert result == ["/proj/src/lib.drift", "/proj/src/util.drift"]

	def test_entry_not_in_modules_still_first(self):
		art = _make_artifact(
			entry_module="src/lib.drift",
			modules=["src/util.drift", "src/other.drift"],
		)
		result = build_source_args(art, Path("/proj"))
		assert result[0] == "/proj/src/lib.drift"
		assert "/proj/src/util.drift" in result
		assert "/proj/src/other.drift" in result

	def test_single_module_same_as_entry(self):
		art = _make_artifact(
			entry_module="src/lib.drift",
			modules=["src/lib.drift"],
		)
		result = build_source_args(art, Path("/proj"))
		assert result == ["/proj/src/lib.drift"]


# ── build_package_cmd tests ──────────────────────────────────────────


class TestBuildPackageCmd:
	def test_basic_package_cmd(self):
		art = _make_artifact()
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		assert cmd[0] == "/usr/bin/driftc"
		assert "--emit-package" in cmd
		assert "/build/my-pkg.dmp" in cmd
		assert "--package-id" in cmd
		idx = cmd.index("--package-id")
		assert cmd[idx + 1] == "my-pkg"

	def test_package_cmd_with_deps(self):
		art = _make_artifact(
			package_deps=[PackageDep(name="dep-a", version="1.0.0")],
		)
		resolved = {"dep-a": ResolvedDep(version="1.0.0", integrity="", dep_type="direct", package_id="dep-a", author_key="ed25519:test")}
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps=resolved,
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[Path("/lib")],
		)
		assert "--package-dep" in cmd
		dep_idx = cmd.index("--package-dep")
		assert cmd[dep_idx + 1] == "dep-a=1.0.0"
		assert "--dep" in cmd
		dep_flag_idx = cmd.index("--dep")
		assert cmd[dep_flag_idx + 1] == "dep-a@1.0.0"
		assert "--package-root" in cmd

	def test_package_cmd_unsafe(self):
		art = _make_artifact(unsafe=True)
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		assert "--allow-unsafe" in cmd

	def test_package_cmd_native_deps(self):
		art = _make_artifact(
			native_deps=[NativeDep(lib="ssl"), NativeDep(lib="crypto")],
		)
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
			native_lib_paths=[Path("/usr/lib")],
		)
		assert "--native-link-lib" in cmd
		assert "--link-search" in cmd

	def test_package_cmd_extra_flags(self):
		art = _make_artifact()
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
			extra_flags=["--verbose", "--debug"],
		)
		assert "--verbose" in cmd
		assert "--debug" in cmd

	def test_package_cmd_with_trust_store(self):
		"""trust_store is forwarded to driftc for co-artifact verification."""
		art = _make_artifact()
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
			trust_store=Path("/stage/drift/trust.json"),
		)
		assert "--trust-store" in cmd
		idx = cmd.index("--trust-store")
		assert cmd[idx + 1] == "/stage/drift/trust.json"

	def test_package_cmd_without_trust_store(self):
		"""No --trust-store when trust_store is None."""
		art = _make_artifact()
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		assert "--trust-store" not in cmd


# ── build_app_cmd tests ──────────────────────────────────────────────


class TestBuildAppCmd:
	def test_basic_app_cmd(self):
		art = _make_artifact(kind="app", name="my-app")
		cmd = build_app_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-app"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		assert cmd[0] == "/usr/bin/driftc"
		assert "-o" in cmd
		assert "/build/my-app" in cmd

	def test_app_cmd_uses_link_lib(self):
		"""Apps use --link-lib, not --native-link-lib."""
		art = _make_artifact(
			kind="app", name="my-app",
			native_deps=[NativeDep(lib="ssl")],
		)
		cmd = build_app_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-app"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		assert "--link-lib" in cmd
		assert "--native-link-lib" not in cmd

	def test_app_cmd_passes_entry_point(self):
		"""App with entry_point emits --entry flag."""
		art = _make_artifact(
			kind="app", name="bookkeeper",
			entry_point="pushcoin.bookkeeper::main",
		)
		cmd = build_app_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/bookkeeper"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		idx = cmd.index("--entry")
		assert cmd[idx + 1] == "pushcoin.bookkeeper::main"

	def test_app_cmd_no_entry_when_default(self):
		"""App without entry_point does not emit --entry (uses driftc default)."""
		art = _make_artifact(kind="app", name="my-app")
		cmd = build_app_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-app"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		assert "--entry" not in cmd


# ── drift_build.run() tests ─────────────────────────────────────────


class TestDriftBuildRun:
	def test_default_manifest_path_is_drift_subdir(self, tmp_path, monkeypatch):
		"""drift build with no --manifest finds `drift/manifest.json` in cwd.

		Locks the post-rename layout: every drift-owned root metadata file
		lives under the `drift/` namespace.  The default `--manifest` path
		is `drift/manifest.json`, NOT `drift-manifest.json`.
		"""
		_write_manifest(tmp_path)
		(tmp_path / "src").mkdir()
		(tmp_path / "src" / "lib.drift").write_text("module my_pkg;")
		(tmp_path / "src" / "util.drift").write_text("module my_pkg.util;")
		monkeypatch.chdir(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([])

		assert result == 0
		mock_run.assert_called_once()

	def test_legacy_root_manifest_is_not_found(self, tmp_path, monkeypatch, capsys):
		"""drift build with only the legacy `drift-manifest.json` at root fails cleanly.

		There is no fallback to the legacy path.  A repo on the old layout
		gets a clear "manifest not found at drift/manifest.json" error,
		not a silent fall-through to the deprecated location.
		"""
		# Intentionally write to the LEGACY path; assert the new default
		# does not fall back to it.
		(tmp_path / "drift-manifest.json").write_text(
			json.dumps(MINIMAL_MANIFEST, indent=2),
			encoding="utf-8",
		)
		monkeypatch.chdir(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run([])

		assert result == 1
		err = capsys.readouterr().err
		# Loader should report the *new* path it expected, not the legacy one.
		assert "drift/manifest.json" in err or "manifest" in err.lower()

	def test_single_artifact_no_name(self, tmp_path):
		"""When manifest has one artifact, name is optional."""
		_write_manifest(tmp_path)
		# Create source files so manifest_dir resolves correctly.
		src_dir = tmp_path / "src"
		src_dir.mkdir()
		(src_dir / "lib.drift").write_text("module my_pkg;")
		(src_dir / "util.drift").write_text("module my_pkg.util;")

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		mock_run.assert_called_once()
		cmd = mock_run.call_args[0][0]
		assert cmd[0] == "/usr/bin/driftc"
		assert "--emit-package" in cmd

	def test_multi_artifact_no_name_error(self, tmp_path):
		"""When manifest has multiple artifacts, name is required."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "pkg-a",
					"version": "0.1.0",
					"description": "A",
					"entry_module": "a/lib.drift",
					"modules": ["a/lib.drift"],
				},
				{
					"kind": "app",
					"name": "app-b",
					"version": "0.1.0",
					"description": "B",
					"entry_module": "b/main.drift",
					"modules": ["b/main.drift"],
				},
			],
		}
		_write_manifest(tmp_path, manifest_data)

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_debug_flag_sets_drift_debug_env(self, tmp_path):
		"""`drift build --debug` sets DRIFT_DEBUG=1 in the driftc subprocess env."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "app",
					"name": "my-app",
					"version": "0.1.0",
					"description": "An app",
					"entry_module": "src/main.drift",
					"modules": ["src/main.drift"],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--debug",
			])

		assert result == 0
		# `--debug` is normalized to DRIFT_DEBUG=1 in the subprocess env;
		# the cmd vector itself stays unchanged.
		env = mock_run.call_args.kwargs["env"]
		assert env.get("DRIFT_DEBUG") == "1"

	def test_drift_debug_env_propagates_to_subprocess(self, tmp_path, monkeypatch):
		"""DRIFT_DEBUG=1 set in the parent env propagates without --debug."""
		_write_manifest(tmp_path)
		from tools.drift_deploy.drift_build import run

		monkeypatch.setenv("DRIFT_DEBUG", "1")
		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		env = mock_run.call_args.kwargs["env"]
		assert env.get("DRIFT_DEBUG") == "1"

	def test_default_does_not_set_drift_debug(self, tmp_path, monkeypatch):
		"""Default build (no --debug, no env) does not set DRIFT_DEBUG."""
		_write_manifest(tmp_path)
		monkeypatch.delenv("DRIFT_DEBUG", raising=False)
		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		env = mock_run.call_args.kwargs["env"]
		assert "DRIFT_DEBUG" not in env

	def test_app_artifact_build(self, tmp_path):
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "app",
					"name": "my-app",
					"version": "0.2.0",
					"description": "An app",
					"entry_module": "src/main.drift",
					"modules": ["src/main.drift"],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		assert "-o" in cmd
		assert "--emit-package" not in cmd

	def test_lockfile_consumption(self, tmp_path):
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "^1.0.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "1.2.3"}})

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		# Should use locked minor range, not manifest constraint ^1.0.0.
		assert "dep-a@1.2" in " ".join(cmd)

	def test_lockfile_transitive_deps_forwarded(self, tmp_path):
		"""Full locked graph (direct + transitive) must be passed to driftc."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "^1.0.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		# Lock contains dep-a (direct) AND dep-b (transitive).
		lock_obj = {
			"schema_version": 2,
			"artifacts": {
				"my-pkg": {
					"resolved": {
						"dep-a": {"version": "1.2", "package_id": "dep-a", "author_key": "unsigned", "dep_type": "direct"},
						"dep-b": {"version": "0.5", "package_id": "dep-b", "author_key": "unsigned", "dep_type": "transitive"},
					}
				}
			},
		}
		(tmp_path / "drift" / "lock.json").write_text(json.dumps(lock_obj, indent=2), encoding="utf-8")

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd_str = " ".join(mock_run.call_args[0][0])
		# Both direct and transitive deps must appear as --dep flags.
		assert "dep-a@1.2" in cmd_str
		assert "dep-b@0.5" in cmd_str

	def test_stale_lockfile_missing_artifact_errors(self, tmp_path):
		"""Lock exists but has no entry for this artifact → error."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "1.0.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		# Lock exists but for a different artifact.
		_write_lock(tmp_path, {"other-pkg": {"dep-a": "1.0.0"}})

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_stale_lockfile_missing_dep_errors(self, tmp_path):
		"""Lock exists for artifact but is missing a declared dep → error."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "^1.0.0"},
						{"name": "dep-b", "version": "^2.0.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		# Lock has dep-a but not dep-b.
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "1.2.3"}})

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_error_no_lockfile_range_dep(self, tmp_path):
		"""Range dep without lockfile should error."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "^1.0.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		# No lockfile written.

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_exact_dep_without_lockfile(self, tmp_path):
		"""Exact version dep without lockfile should succeed."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "1.0.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		assert "dep-a@1.0.0" in " ".join(cmd)

	def test_output_path_default_package(self, tmp_path):
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		idx = cmd.index("--emit-package")
		output = cmd[idx + 1]
		assert output.endswith("build/my-pkg.dmp")

	def test_output_path_default_app(self, tmp_path):
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "app",
					"name": "my-app",
					"version": "0.1.0",
					"description": "An app",
					"entry_module": "src/main.drift",
					"modules": ["src/main.drift"],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		idx = cmd.index("-o")
		output = cmd[idx + 1]
		assert output.endswith("build/my-app")

	def test_output_override(self, tmp_path):
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"-o", str(tmp_path / "custom-out.dmp"),
			])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		idx = cmd.index("--emit-package")
		assert cmd[idx + 1] == str(tmp_path / "custom-out.dmp")

	def test_manifest_default_filename(self, tmp_path):
		"""Default manifest filename is drift/manifest.json."""
		from tools.drift_deploy.drift_build import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args([])
		assert args.manifest == Path("drift") / "manifest.json"

	def test_passthrough_flags(self, tmp_path):
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--", "--verbose", "--some-flag",
			])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		assert "--verbose" in cmd
		assert "--some-flag" in cmd

	def test_unsafe_artifact(self, tmp_path):
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "ffi-pkg",
					"version": "0.1.0",
					"description": "FFI package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"unsafe": True,
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		assert "--allow-unsafe" in cmd

	def test_target_default_is_none(self):
		"""Target defaults to None (resolved per artifact kind)."""
		from tools.drift_deploy.drift_build import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args([])
		assert args.target is None

	def test_app_default_target_native(self, tmp_path):
		"""App artifact with no --target defaults to native and emits --target-word-bits."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-app", "license": "MIT"},
			"artifacts": [
				{
					"kind": "app",
					"name": "my-app",
					"version": "0.1.0",
					"description": "Test app",
					"entry_module": "src/main.drift",
					"modules": ["src/main.drift"],
					"entry_point": "my_app::main",
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		(tmp_path / "src").mkdir(exist_ok=True)
		(tmp_path / "src" / "main.drift").write_text("module my_app;\npub fn main() nothrow -> Int { return 0; }\n")

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		assert "--target-word-bits" in cmd, "app build must emit --target-word-bits"

	def test_app_explicit_target_native(self, tmp_path):
		"""App with --target native emits --target-word-bits."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-app", "license": "MIT"},
			"artifacts": [
				{
					"kind": "app",
					"name": "my-app",
					"version": "0.1.0",
					"description": "Test app",
					"entry_module": "src/main.drift",
					"modules": ["src/main.drift"],
					"entry_point": "my_app::main",
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		(tmp_path / "src").mkdir(exist_ok=True)
		(tmp_path / "src" / "main.drift").write_text("module my_app;\npub fn main() nothrow -> Int { return 0; }\n")

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json"), "--target", "native"])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		assert "--target-word-bits" in cmd

	def test_app_unsupported_target_rejected(self, tmp_path):
		"""App with unsupported --target produces clear error."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-app", "license": "MIT"},
			"artifacts": [
				{
					"kind": "app",
					"name": "my-app",
					"version": "0.1.0",
					"description": "Test app",
					"entry_module": "src/main.drift",
					"modules": ["src/main.drift"],
					"entry_point": "my_app::main",
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json"), "--target", "linux-x86_64"])

		assert result == 1, "unsupported app target must fail"

	def test_named_artifact_selection(self, tmp_path):
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "pkg-a",
					"version": "0.1.0",
					"description": "A",
					"entry_module": "a/lib.drift",
					"modules": ["a/lib.drift"],
				},
				{
					"kind": "app",
					"name": "app-b",
					"version": "0.1.0",
					"description": "B",
					"entry_module": "b/main.drift",
					"modules": ["b/main.drift"],
				},
			],
		}
		_write_manifest(tmp_path, manifest_data)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([
				"app-b",
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
			])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		assert "-o" in cmd  # app, not package


# ── Finding 1: --package-dep uses resolved versions ──────────────────


class TestPackageDepUsesResolvedVersions:
	def test_package_dep_uses_resolved_not_manifest_range(self):
		"""--package-dep must emit exact resolved version, not manifest range."""
		art = _make_artifact(
			package_deps=[PackageDep(name="dep-a", version="^1.0.0")],
		)
		resolved = {"dep-a": ResolvedDep(version="1.2.3", integrity="", dep_type="direct", package_id="dep-a", author_key="ed25519:test")}
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps=resolved,
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		# --package-dep should use the resolved 1.2.3, not the manifest ^1.0.0.
		dep_idx = cmd.index("--package-dep")
		assert cmd[dep_idx + 1] == "dep-a=1.2.3"
		# Manifest range should NOT appear anywhere in the command.
		joined = " ".join(cmd)
		assert "^1.0.0" not in joined

	def test_package_dep_falls_back_to_manifest_version(self):
		"""When dep is not in resolved_deps, manifest version is used."""
		art = _make_artifact(
			package_deps=[PackageDep(name="dep-a", version="1.0.0")],
		)
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		dep_idx = cmd.index("--package-dep")
		assert cmd[dep_idx + 1] == "dep-a=1.0.0"

	def test_transitive_deps_excluded_from_package_dep(self):
		"""--package-dep must only emit direct deps, not transitives."""
		art = _make_artifact(
			package_deps=[PackageDep(name="dep-a", version="^1.0.0")],
		)
		resolved = {
			"dep-a": ResolvedDep(version="1.2.3", integrity="", dep_type="direct", package_id="dep-a", author_key="ed25519:test"),
			"dep-b": ResolvedDep(version="0.5.0", integrity="", dep_type="transitive", package_id="dep-b", author_key="ed25519:test"),
		}
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps=resolved,
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		# --package-dep should only have dep-a (direct), not dep-b (transitive).
		package_dep_values = []
		for i, flag in enumerate(cmd):
			if flag == "--package-dep":
				package_dep_values.append(cmd[i + 1])
		assert package_dep_values == ["dep-a=1.2.3"]
		# But --dep should have BOTH (compiler version selection).
		dep_values = []
		for i, flag in enumerate(cmd):
			if flag == "--dep":
				dep_values.append(cmd[i + 1])
		assert "dep-a@1.2.3" in dep_values
		assert "dep-b@0.5.0" in dep_values

	def test_lockfile_version_in_package_dep_metadata(self, tmp_path):
		"""End-to-end: locked version appears in --package-dep, not manifest range."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "^1.0.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "1.2.3"}})

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		dep_idx = cmd.index("--package-dep")
		assert cmd[dep_idx + 1] == "dep-a=1.2"
		joined = " ".join(cmd)
		assert "^1.0.0" not in joined


# ── Finding 2: subprocess env scrubbing ──────────────────────────────


class TestSubprocessEnvScrubbing:
	def test_build_scrubs_pythonpath(self, tmp_path):
		"""drift build must scrub PYTHONPATH from driftc subprocess env."""
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch.dict(os.environ, {"PYTHONPATH": "/bad/path"}):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		call_kwargs = mock_run.call_args[1]
		assert "env" in call_kwargs
		assert "PYTHONPATH" not in call_kwargs["env"]

	def test_build_scrubs_pythonhome(self, tmp_path):
		"""drift build must scrub PYTHONHOME from driftc subprocess env."""
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch.dict(os.environ, {"PYTHONHOME": "/bad/home"}):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		call_kwargs = mock_run.call_args[1]
		assert "env" in call_kwargs
		assert "PYTHONHOME" not in call_kwargs["env"]

	def test_build_preserves_other_env_vars(self, tmp_path):
		"""Non-scrubbed env vars pass through to driftc."""
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch.dict(os.environ, {"MY_CUSTOM_VAR": "keep_me"}, clear=False):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		call_kwargs = mock_run.call_args[1]
		assert call_kwargs["env"].get("MY_CUSTOM_VAR") == "keep_me"


# ── Finding 3: config validation parity ──────────────────────────────


class TestConfigValidation:
	def test_relative_native_lib_path_in_env_rejected(self, tmp_path):
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch.dict(os.environ, {"DRIFT_NATIVE_LIB_PATH": "relative/path"}, clear=False):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_relative_native_lib_path_in_config_rejected(self, tmp_path):
		_write_manifest(tmp_path)
		config = {"native_lib_paths": ["relative/path"]}
		(tmp_path / "drift" / "deploy-config.json").write_text(json.dumps(config))

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_relative_native_lib_path_in_cli_rejected(self, tmp_path):
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--native-lib-path", "relative/path",
			])

		assert result == 1

	def test_relative_package_root_in_env_rejected(self, tmp_path):
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch.dict(os.environ, {"DRIFT_PACKAGE_ROOT": "relative/root"}, clear=False):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_relative_package_root_in_config_rejected(self, tmp_path):
		_write_manifest(tmp_path)
		config = {"package_roots": ["relative/root"]}
		(tmp_path / "drift" / "deploy-config.json").write_text(json.dumps(config))

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_relative_package_root_in_cli_rejected(self, tmp_path):
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", "relative/root",
			])

		assert result == 1

	def test_malformed_config_top_level_rejected(self, tmp_path):
		_write_manifest(tmp_path)
		(tmp_path / "drift" / "deploy-config.json").write_text('"not an object"')

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_malformed_config_native_lib_paths_type_rejected(self, tmp_path):
		_write_manifest(tmp_path)
		config = {"native_lib_paths": "not-an-array"}
		(tmp_path / "drift" / "deploy-config.json").write_text(json.dumps(config))

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1

	def test_absolute_paths_accepted(self, tmp_path):
		_write_manifest(tmp_path)

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", "/absolute/root",
				"--native-lib-path", "/absolute/lib",
			])

		assert result == 0


# ── Lock compatibility validation ────────────────────────────────────


class TestLockCompatibility:
	def test_lock_compatibility_checked_against_package_roots(self, tmp_path):
		"""Lock compatibility mismatch against package roots produces early error."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "^1.0.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "1.2.3"}})

		# Package root exists but has no dep-a package → integrity fail.
		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()

		from tools.drift_deploy.drift_build import run

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 1

	def test_lock_range_resolved_to_exact_version(self, tmp_path):
		"""v2 lock stores major.minor; build must resolve to exact version for --dep."""
		manifest_data = {
			"schema_version": 1,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [
				{
					"kind": "package",
					"name": "my-pkg",
					"version": "0.1.0",
					"description": "A test package",
					"entry_module": "src/lib.drift",
					"modules": ["src/lib.drift"],
					"package_deps": [
						{"name": "dep-a", "version": "^0.1.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		(tmp_path / "src").mkdir(parents=True, exist_ok=True)
		(tmp_path / "src" / "lib.drift").write_text("module my.pkg;\n")
		# Lock stores major.minor range.
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})

		# Package root has exact version 0.1.3.
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {
				"dep-a": [
					PackageEntry(
						package_id="dep-a",
						version=parse_version("0.1.3"),
						path=pkg_root / "dep-a-0.1.3.dmp",
						sha256="aabb",
						package_deps=[],
						author_key="ed25519:test",
					),
				],
			}
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 0
		cmd_str = " ".join(mock_run.call_args[0][0])
		# Must use exact version 0.1.3, not range 0.1.
		assert "dep-a@0.1.3" in cmd_str, (
			f"build must resolve lock range 0.1 to exact version 0.1.3; "
			f"got: {cmd_str}"
		)
		assert "dep-a@0.1 " not in cmd_str and not cmd_str.endswith("dep-a@0.1"), (
			f"raw lock range must not reach driftc; got: {cmd_str}"
		)


# ── CLI dispatch (toolchain integration) ─────────────────────────────


class TestCLIDispatch:
	def test_cli_build_subcommand(self) -> None:
		"""'drift build --help' works through lang.drift.cli dispatch."""
		from lang.drift.cli import main as cli_main
		with pytest.raises(SystemExit) as exc_info:
			cli_main(["build", "--help"])
		assert exc_info.value.code == 0

	def test_drift_build_module_importable(self) -> None:
		"""drift_build.run must be importable."""
		from tools.drift_deploy.drift_build import run
		assert callable(run)

	def test_build_cmd_module_importable(self) -> None:
		"""build_cmd shared helpers must be importable."""
		from tools.drift_deploy.build_cmd import (
			build_app_cmd,
			build_package_cmd,
			build_source_args,
		)
		assert callable(build_app_cmd)
		assert callable(build_package_cmd)
		assert callable(build_source_args)


# ── driftc resolution ────────────────────────────────────────────────


class TestResolveDriftc:
	def test_explicit_path_wins(self, tmp_path):
		"""Explicit --driftc always takes precedence."""
		driftc = tmp_path / "my-driftc"
		driftc.write_text("#!/bin/sh\n", encoding="utf-8")
		driftc.chmod(0o755)
		result = resolve_driftc(driftc)
		assert result == driftc

	def test_explicit_path_missing_raises(self, tmp_path):
		"""Explicit --driftc that doesn't exist raises ValueError."""
		missing = tmp_path / "no-such-driftc"
		with pytest.raises(ValueError, match="does not exist"):
			resolve_driftc(missing)

	def test_sibling_found(self, tmp_path):
		"""Sibling driftc next to the running executable is used."""
		fake_bin = tmp_path / "bin"
		fake_bin.mkdir()
		# Create sibling driftc.
		sibling = fake_bin / "driftc"
		sibling.write_text("#!/bin/sh\n", encoding="utf-8")
		sibling.chmod(0o755)
		# Simulate drift running from fake_bin/drift.
		with mock.patch("sys.argv", [str(fake_bin / "drift")]), \
			 mock.patch("shutil.which", return_value=None):
			result = resolve_driftc(None)
		assert result == sibling

	def test_sibling_through_symlink(self, tmp_path):
		"""Symlinked drift resolves sibling from the real target directory."""
		# Real layout: real_bin/drift + real_bin/driftc
		real_bin = tmp_path / "real_bin"
		real_bin.mkdir()
		(real_bin / "drift").write_text("#!/bin/sh\n", encoding="utf-8")
		(real_bin / "drift").chmod(0o755)
		sibling = real_bin / "driftc"
		sibling.write_text("#!/bin/sh\n", encoding="utf-8")
		sibling.chmod(0o755)
		# Symlink: link_bin/drift → real_bin/drift
		link_bin = tmp_path / "link_bin"
		link_bin.mkdir()
		(link_bin / "drift").symlink_to(real_bin / "drift")

		with mock.patch("sys.argv", [str(link_bin / "drift")]), \
			 mock.patch("shutil.which", return_value=None):
			result = resolve_driftc(None)
		assert result == sibling

	def test_path_fallback(self, tmp_path):
		"""Falls back to PATH when no sibling exists."""
		# No sibling in fake bin dir.
		fake_bin = tmp_path / "bin"
		fake_bin.mkdir()
		(fake_bin / "drift").write_text("#!/bin/sh\n", encoding="utf-8")
		(fake_bin / "drift").chmod(0o755)
		# driftc is on PATH.
		path_driftc = tmp_path / "path_driftc"
		path_driftc.write_text("#!/bin/sh\n", encoding="utf-8")
		path_driftc.chmod(0o755)

		with mock.patch("sys.argv", [str(fake_bin / "drift")]), \
			 mock.patch("shutil.which", return_value=str(path_driftc)):
			result = resolve_driftc(None)
		assert result == path_driftc

	def test_none_when_not_found(self, tmp_path):
		"""Returns None when no driftc can be found anywhere."""
		fake_bin = tmp_path / "bin"
		fake_bin.mkdir()
		(fake_bin / "drift").write_text("#!/bin/sh\n", encoding="utf-8")
		(fake_bin / "drift").chmod(0o755)

		with mock.patch("sys.argv", [str(fake_bin / "drift")]), \
			 mock.patch("shutil.which", return_value=None):
			result = resolve_driftc(None)
		assert result is None

	def test_explicit_beats_sibling(self, tmp_path):
		"""Explicit --driftc takes precedence over sibling."""
		fake_bin = tmp_path / "bin"
		fake_bin.mkdir()
		sibling = fake_bin / "driftc"
		sibling.write_text("#!/bin/sh\necho sibling\n", encoding="utf-8")
		sibling.chmod(0o755)
		explicit = tmp_path / "explicit-driftc"
		explicit.write_text("#!/bin/sh\necho explicit\n", encoding="utf-8")
		explicit.chmod(0o755)

		with mock.patch("sys.argv", [str(fake_bin / "drift")]):
			result = resolve_driftc(explicit)
		assert result == explicit

	def test_sibling_not_executable_skipped(self, tmp_path):
		"""Non-executable sibling file is ignored."""
		fake_bin = tmp_path / "bin"
		fake_bin.mkdir()
		sibling = fake_bin / "driftc"
		sibling.write_text("not executable", encoding="utf-8")
		# Don't chmod +x.

		with mock.patch("sys.argv", [str(fake_bin / "drift")]), \
			 mock.patch("shutil.which", return_value=None):
			result = resolve_driftc(None)
		assert result is None


# ── Tilde expansion (UserPath) ───────────────────────────────────────


class TestUserPath:
	def test_tilde_expanded(self):
		result = UserPath("~/some/path")
		assert "~" not in str(result)
		assert str(result).startswith(str(Path.home()))

	def test_no_tilde_passthrough(self):
		result = UserPath("/absolute/path")
		assert str(result) == "/absolute/path"

	def test_relative_passthrough(self):
		result = UserPath("relative/path")
		assert str(result) == "relative/path"

	def test_returns_path(self):
		result = UserPath("~/foo")
		assert isinstance(result, Path)


class TestTildeExpansionBuild:
	def test_build_manifest_tilde(self, tmp_path):
		"""drift build --manifest=~/... expands tilde."""
		from tools.drift_deploy.drift_build import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--manifest=~/my-manifest.json"])
		assert "~" not in str(args.manifest)

	def test_build_output_tilde(self, tmp_path):
		from tools.drift_deploy.drift_build import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["-o", "~/build/out.dmp"])
		assert "~" not in str(args.output)

	def test_build_driftc_tilde(self, tmp_path):
		from tools.drift_deploy.drift_build import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--driftc=~/bin/driftc"])
		assert "~" not in str(args.driftc)

	def test_build_package_root_tilde(self, tmp_path):
		from tools.drift_deploy.drift_build import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--package-root=~/libs"])
		assert "~" not in str(args.package_root[0])

	def test_build_native_lib_path_tilde(self, tmp_path):
		from tools.drift_deploy.drift_build import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--native-lib-path=~/lib"])
		assert "~" not in str(args.native_lib_path[0])


class TestTildeExpansionDeploy:
	def test_deploy_dest_tilde(self):
		"""drift deploy --dest=~/... expands tilde."""
		from tools.drift_deploy.drift_deploy import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--dest=~/opt/drift/libs"])
		assert "~" not in str(args.dest)

	def test_deploy_manifest_tilde(self):
		from tools.drift_deploy.drift_deploy import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--manifest=~/project/drift/manifest.json"])
		assert "~" not in str(args.manifest)

	def test_deploy_app_dest_tilde(self):
		from tools.drift_deploy.drift_deploy import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--app-dest=~/opt/drift/apps"])
		assert "~" not in str(args.app_dest)

	def test_deploy_sign_key_file_tilde(self):
		from tools.drift_deploy.drift_deploy import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--sign-key-file=~/.config/drift/keys/default.seed"])
		assert "~" not in str(args.sign_key_file)

	def test_deploy_trust_store_tilde(self):
		from tools.drift_deploy.drift_deploy import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--trust-store=~/project/drift/trust.json"])
		assert "~" not in str(args.trust_store)

	def test_deploy_dest_space_separated_tilde(self):
		"""drift deploy --dest ~/... (space-separated) also expands."""
		from tools.drift_deploy.drift_deploy import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--dest", "~/opt/drift/libs"])
		assert "~" not in str(args.dest)


class TestTildeExpansionPrepare:
	def test_prepare_dest_tilde(self):
		from tools.drift_deploy.drift_prepare import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--dest=~/opt/drift/libs"])
		assert "~" not in str(args.dest)

	def test_prepare_package_root_tilde(self):
		from tools.drift_deploy.drift_prepare import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--package-root=~/libs"])
		assert "~" not in str(args.package_root[0])


# ── End-to-end toolchain tests ───────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIFTC_BIN = REPO_ROOT / "bin" / "driftc"

_PKG_SRC = """\
module test_pkg;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
\treturn a + b;
}
"""

_APP_SRC = """\
fn main() nothrow -> Int {
\treturn 42;
}
"""

_DEP_SRC = """\
module test_dep;

export { zero };

pub fn zero() nothrow -> Int {
\treturn 0;
}
"""

_LEAF_SRC = """\
module test_leaf;

export { one };

pub fn one() nothrow -> Int {
\treturn 1;
}
"""

_CONSUMER_SRC = """\
module test_consumer;

import test_dep;

export { wrapped_zero };

pub fn wrapped_zero() nothrow -> Int {
\treturn test_dep.zero();
}
"""


def _skip_no_driftc():
	if not DRIFTC_BIN.exists():
		pytest.skip("bin/driftc not found")


def _write_e2e_manifest(parent: Path, manifest: dict) -> Path:
	"""Create `parent/drift/` and write `manifest.json` inside it.

	Returns the manifest path.  Mirrors the post-rename layout: every
	drift-owned root metadata file lives under the `drift/` subdirectory.
	"""
	drift_dir = parent / "drift"
	drift_dir.mkdir(exist_ok=True)
	path = drift_dir / "manifest.json"
	path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
	return path


def _write_e2e_lock(parent: Path, lock_obj: dict) -> Path:
	"""Create `parent/drift/` and write `lock.json` inside it.

	Returns the lock path.  Same layout convention as `_write_e2e_manifest`.
	"""
	drift_dir = parent / "drift"
	drift_dir.mkdir(exist_ok=True)
	path = drift_dir / "lock.json"
	path.write_text(json.dumps(lock_obj, indent=2), encoding="utf-8")
	return path


def _e2e_manifest(artifacts: list[dict], **project_extra) -> dict:
	project = {"name": "e2e-test", "license": "MIT"}
	project.update(project_extra)
	return {
		"schema_version": 1,
		"project": project,
		"artifacts": artifacts,
	}


def _pkg_artifact(name: str, entry: str, modules: list[str], **extra) -> dict:
	art = {
		"kind": "package",
		"name": name,
		"version": "0.1.0",
		"description": "e2e test package",
		"entry_module": entry,
		"modules": modules,
	}
	art.update(extra)
	return art


def _app_artifact(name: str, entry: str, modules: list[str], **extra) -> dict:
	art = {
		"kind": "app",
		"name": name,
		"version": "0.1.0",
		"description": "e2e test app",
		"entry_module": entry,
		"modules": modules,
	}
	art.update(extra)
	return art


class TestE2E:
	"""End-to-end toolchain tests — real driftc compilation."""

	def test_package_build_single_artifact(self, tmp_path):
		"""Package build through CLI path, single artifact, no name needed."""
		_skip_no_driftc()

		src = tmp_path / "src" / "lib.drift"
		src.parent.mkdir(parents=True)
		src.write_text(_PKG_SRC, encoding="utf-8")

		manifest = _e2e_manifest([
			_pkg_artifact("test-pkg", "src/lib.drift", ["src/lib.drift"]),
		])
		_write_e2e_manifest(tmp_path, manifest)

		from lang.drift.cli import main as cli_main
		rc = cli_main([
			"build",
			"--manifest", str(tmp_path / "drift" / "manifest.json"),
			"--driftc", str(DRIFTC_BIN),
		])

		assert rc == 0
		dmp = tmp_path / "build" / "test-pkg.dmp"
		assert dmp.exists(), f"expected output at {dmp}"
		assert dmp.stat().st_size > 0

	def test_package_build_entry_dedup(self, tmp_path):
		"""Entry module in modules list is deduplicated, build succeeds."""
		_skip_no_driftc()

		src = tmp_path / "src" / "lib.drift"
		src.parent.mkdir(parents=True)
		src.write_text(_PKG_SRC, encoding="utf-8")

		manifest = _e2e_manifest([
			_pkg_artifact(
				"test-pkg", "src/lib.drift",
				["src/lib.drift", "src/lib.drift"],  # duplicate entry
			),
		])
		_write_e2e_manifest(tmp_path, manifest)

		from lang.drift.cli import main as cli_main
		rc = cli_main([
			"build",
			"--manifest", str(tmp_path / "drift" / "manifest.json"),
			"--driftc", str(DRIFTC_BIN),
		])

		assert rc == 0
		assert (tmp_path / "build" / "test-pkg.dmp").exists()

	def test_app_build_single_artifact(self, tmp_path):
		"""App build through CLI path, produces binary."""
		_skip_no_driftc()

		src = tmp_path / "src" / "main.drift"
		src.parent.mkdir(parents=True)
		src.write_text(_APP_SRC, encoding="utf-8")

		manifest = _e2e_manifest([
			_app_artifact("test-app", "src/main.drift", ["src/main.drift"]),
		])
		_write_e2e_manifest(tmp_path, manifest)

		from lang.drift.cli import main as cli_main
		rc = cli_main([
			"build",
			"--manifest", str(tmp_path / "drift" / "manifest.json"),
			"--driftc", str(DRIFTC_BIN),
		])

		assert rc == 0
		binary = tmp_path / "build" / "test-app"
		assert binary.exists(), f"expected binary at {binary}"
		assert os.access(str(binary), os.X_OK), "binary should be executable"

	def test_multi_artifact_requires_name(self, tmp_path):
		"""Multi-artifact manifest without name → error."""
		_skip_no_driftc()

		for name, src_text in [("src/lib.drift", _PKG_SRC), ("src/main.drift", _APP_SRC)]:
			p = tmp_path / name
			p.parent.mkdir(parents=True, exist_ok=True)
			p.write_text(src_text, encoding="utf-8")

		manifest = _e2e_manifest([
			_pkg_artifact("test-pkg", "src/lib.drift", ["src/lib.drift"]),
			_app_artifact("test-app", "src/main.drift", ["src/main.drift"]),
		])
		_write_e2e_manifest(tmp_path, manifest)

		from lang.drift.cli import main as cli_main
		rc = cli_main([
			"build",
			"--manifest", str(tmp_path / "drift" / "manifest.json"),
			"--driftc", str(DRIFTC_BIN),
		])

		assert rc == 1

	def test_multi_artifact_selects_by_name(self, tmp_path):
		"""Multi-artifact manifest with explicit name → correct artifact built."""
		_skip_no_driftc()

		for name, src_text in [("src/lib.drift", _PKG_SRC), ("src/main.drift", _APP_SRC)]:
			p = tmp_path / name
			p.parent.mkdir(parents=True, exist_ok=True)
			p.write_text(src_text, encoding="utf-8")

		manifest = _e2e_manifest([
			_pkg_artifact("test-pkg", "src/lib.drift", ["src/lib.drift"]),
			_app_artifact("test-app", "src/main.drift", ["src/main.drift"]),
		])
		_write_e2e_manifest(tmp_path, manifest)

		from lang.drift.cli import main as cli_main

		# Build the package artifact.
		rc = cli_main([
			"build", "test-pkg",
			"--manifest", str(tmp_path / "drift" / "manifest.json"),
			"--driftc", str(DRIFTC_BIN),
		])
		assert rc == 0
		assert (tmp_path / "build" / "test-pkg.dmp").exists()
		assert not (tmp_path / "build" / "test-app").exists()

	def test_locked_dep_forwarding(self, tmp_path):
		"""Build with locked deps: direct + transitive forwarded, only direct in metadata."""
		_skip_no_driftc()

		# Step 1: Build the dependency package.
		dep_dir = tmp_path / "dep_project"
		dep_dir.mkdir()
		dep_src = dep_dir / "src" / "lib.drift"
		dep_src.parent.mkdir(parents=True)
		dep_src.write_text(_DEP_SRC, encoding="utf-8")

		dep_manifest = _e2e_manifest([
			_pkg_artifact("test-dep", "src/lib.drift", ["src/lib.drift"]),
		])
		_write_e2e_manifest(dep_dir, dep_manifest)

		from lang.drift.cli import main as cli_main

		rc = cli_main([
			"build",
			"--manifest", str(dep_dir / "drift" / "manifest.json"),
			"--driftc", str(DRIFTC_BIN),
		])
		assert rc == 0
		dep_dmp = dep_dir / "build" / "test-dep.dmp"
		assert dep_dmp.exists()

		# Step 2: Set up a package root with the built dep.
		pkg_root = tmp_path / "pkg_root" / "test-dep" / "0.1.0"
		pkg_root.mkdir(parents=True)
		import hashlib
		import shutil
		shutil.copy2(str(dep_dmp), str(pkg_root / "test-dep.dmp"))

		# Step 3: Build the consumer package with a lockfile.
		consumer_dir = tmp_path / "consumer_project"
		consumer_dir.mkdir()
		consumer_src = consumer_dir / "src" / "lib.drift"
		consumer_src.parent.mkdir(parents=True)
		consumer_src.write_text(_CONSUMER_SRC, encoding="utf-8")

		consumer_manifest = _e2e_manifest([
			_pkg_artifact(
				"test-consumer", "src/lib.drift", ["src/lib.drift"],
				package_deps=[{"name": "test-dep", "version": "^0.1.0"}],
			),
		])
		_write_e2e_manifest(consumer_dir, consumer_manifest)

		# Write lockfile with compatibility range from the built dep.
		_write_e2e_lock(consumer_dir, {
			"schema_version": 2,
			"artifacts": {
				"test-consumer": {
					"resolved": {
						"test-dep": {
							"version": "0.1",
							"package_id": "test-dep",
							"author_key": "unsigned",
							"dep_type": "direct",
						},
					},
				},
			},
		})

		# Unsigned test packages — disable signature enforcement
		# in both bin/driftc wrapper and driftc itself.
		with mock.patch.dict(os.environ, {"DRIFT_REQUIRE_SIGNATURES": "0"}):
			rc = cli_main([
				"build",
				"--manifest", str(consumer_dir / "drift" / "manifest.json"),
				"--driftc", str(DRIFTC_BIN),
				"--package-root", str(tmp_path / "pkg_root"),
				"--", "--allow-unsigned-from", str(tmp_path / "pkg_root"),
			])
		assert rc == 0
		consumer_dmp = consumer_dir / "build" / "test-consumer.dmp"
		assert consumer_dmp.exists()

		# Verify package metadata: test-dep is declared as a direct dep.
		from lang.driftc.packages.dmir_pkg_v0 import load_dmir_pkg_v0
		pkg = load_dmir_pkg_v0(consumer_dmp)
		dep_names = [d.name for d in pkg.package_deps]
		assert "test-dep" in dep_names
		dep_entry = next(d for d in pkg.package_deps if d.name == "test-dep")
		assert dep_entry.version == "0.1.0"

	def test_transitive_dep_in_lockfile(self, tmp_path):
		"""
		Real two-level dep chain: test-leaf → test-dep → test-consumer.

		Lockfile has test-dep (direct) and test-leaf (transitive).
		Both must appear as --dep (build succeeds because driftc can
		resolve both). Only test-dep appears in emitted package metadata.
		"""
		_skip_no_driftc()
		import hashlib
		import shutil
		from lang.drift.cli import main as cli_main

		pkg_root = tmp_path / "pkg_root"

		# ── Build test-leaf (no deps) ──
		leaf_dir = tmp_path / "leaf_project"
		leaf_dir.mkdir()
		leaf_src = leaf_dir / "src" / "lib.drift"
		leaf_src.parent.mkdir(parents=True)
		leaf_src.write_text(_LEAF_SRC, encoding="utf-8")
		_write_e2e_manifest(leaf_dir,
			_e2e_manifest([_pkg_artifact("test-leaf", "src/lib.drift", ["src/lib.drift"])]),
		)

		rc = cli_main(["build", "--manifest", str(leaf_dir / "drift" / "manifest.json"), "--driftc", str(DRIFTC_BIN)])
		assert rc == 0
		leaf_dmp = leaf_dir / "build" / "test-leaf.dmp"
		assert leaf_dmp.exists()

		# Stage test-leaf into package root.
		leaf_pkg = pkg_root / "test-leaf" / "0.1.0"
		leaf_pkg.mkdir(parents=True)
		shutil.copy2(str(leaf_dmp), str(leaf_pkg / "test-leaf.dmp"))
		leaf_sha = hashlib.sha256(leaf_dmp.read_bytes()).hexdigest()

		# ── Build test-dep (depends on test-leaf) ──
		dep_src_text = (
			"module test_dep;\n\n"
			"import test_leaf;\n\n"
			"export { leaf_one };\n\n"
			"pub fn leaf_one() nothrow -> Int {\n"
			"\treturn test_leaf.one();\n"
			"}\n"
		)
		dep_dir = tmp_path / "dep_project"
		dep_dir.mkdir()
		dep_src = dep_dir / "src" / "lib.drift"
		dep_src.parent.mkdir(parents=True)
		dep_src.write_text(dep_src_text, encoding="utf-8")
		_write_e2e_manifest(dep_dir,
			_e2e_manifest([_pkg_artifact(
				"test-dep", "src/lib.drift", ["src/lib.drift"],
				package_deps=[{"name": "test-leaf", "version": "0.1.0"}],
			)]),
		)
		# test-dep needs a lockfile for its dep on test-leaf.
		_write_e2e_lock(dep_dir, {
			"schema_version": 2,
			"artifacts": {"test-dep": {"resolved": {
				"test-leaf": {"version": "0.1", "package_id": "test-leaf", "author_key": "unsigned", "dep_type": "direct"},
			}}},
		})

		with mock.patch.dict(os.environ, {"DRIFT_REQUIRE_SIGNATURES": "0"}):
			rc = cli_main([
				"build", "--manifest", str(dep_dir / "drift" / "manifest.json"),
				"--driftc", str(DRIFTC_BIN),
				"--package-root", str(pkg_root),
				"--", "--allow-unsigned-from", str(pkg_root),
			])
		assert rc == 0
		dep_dmp = dep_dir / "build" / "test-dep.dmp"
		assert dep_dmp.exists()

		# Stage test-dep into package root.
		dep_pkg = pkg_root / "test-dep" / "0.1.0"
		dep_pkg.mkdir(parents=True)
		shutil.copy2(str(dep_dmp), str(dep_pkg / "test-dep.dmp"))
		dep_sha = hashlib.sha256(dep_dmp.read_bytes()).hexdigest()

		# ── Build test-consumer (depends on test-dep; test-leaf is transitive) ──
		consumer_src_text = (
			"module test_consumer;\n\n"
			"import test_dep;\n\n"
			"export { use_leaf };\n\n"
			"pub fn use_leaf() nothrow -> Int {\n"
			"\treturn test_dep.leaf_one();\n"
			"}\n"
		)
		consumer_dir = tmp_path / "consumer_project"
		consumer_dir.mkdir()
		consumer_src = consumer_dir / "src" / "lib.drift"
		consumer_src.parent.mkdir(parents=True)
		consumer_src.write_text(consumer_src_text, encoding="utf-8")
		_write_e2e_manifest(consumer_dir,
			_e2e_manifest([_pkg_artifact(
				"test-consumer", "src/lib.drift", ["src/lib.drift"],
				package_deps=[{"name": "test-dep", "version": "^0.1.0"}],
			)]),
		)
		# Lockfile: test-dep is direct, test-leaf is transitive.
		_write_e2e_lock(consumer_dir, {
			"schema_version": 2,
			"artifacts": {"test-consumer": {"resolved": {
				"test-dep": {"version": "0.1", "package_id": "test-dep", "author_key": "unsigned", "dep_type": "direct"},
				"test-leaf": {"version": "0.1", "package_id": "test-leaf", "author_key": "unsigned", "dep_type": "transitive"},
			}}},
		})

		with mock.patch.dict(os.environ, {"DRIFT_REQUIRE_SIGNATURES": "0"}):
			rc = cli_main([
				"build", "--manifest", str(consumer_dir / "drift" / "manifest.json"),
				"--driftc", str(DRIFTC_BIN),
				"--package-root", str(pkg_root),
				"--", "--allow-unsigned-from", str(pkg_root),
			])
		assert rc == 0
		consumer_dmp = consumer_dir / "build" / "test-consumer.dmp"
		assert consumer_dmp.exists()

		# ── Verify package metadata ──
		from lang.driftc.packages.dmir_pkg_v0 import load_dmir_pkg_v0
		pkg = load_dmir_pkg_v0(consumer_dmp)
		declared_dep_names = sorted(d.name for d in pkg.package_deps)
		# Only the DIRECT dep should be in package metadata.
		assert "test-dep" in declared_dep_names
		# Transitive dep must NOT be declared as a package dep.
		assert "test-leaf" not in declared_dep_names

	def test_range_dep_no_lockfile_errors(self, tmp_path):
		"""Range dep without lockfile → clear error through real CLI."""
		_skip_no_driftc()

		src = tmp_path / "src" / "lib.drift"
		src.parent.mkdir(parents=True)
		src.write_text(_PKG_SRC, encoding="utf-8")

		manifest = _e2e_manifest([
			_pkg_artifact(
				"test-pkg", "src/lib.drift", ["src/lib.drift"],
				package_deps=[{"name": "some-dep", "version": "^1.0.0"}],
			),
		])
		_write_e2e_manifest(tmp_path, manifest)

		from lang.drift.cli import main as cli_main
		rc = cli_main([
			"build",
			"--manifest", str(tmp_path / "drift" / "manifest.json"),
			"--driftc", str(DRIFTC_BIN),
		])
		assert rc == 1

	def test_unsafe_package_build(self, tmp_path):
		"""Package with unsafe: true propagates --allow-unsafe and compiles."""
		_skip_no_driftc()

		src = tmp_path / "src" / "lib.drift"
		src.parent.mkdir(parents=True)
		src.write_text(_PKG_SRC, encoding="utf-8")

		manifest = _e2e_manifest([
			_pkg_artifact(
				"test-pkg", "src/lib.drift", ["src/lib.drift"],
				unsafe=True,
			),
		])
		_write_e2e_manifest(tmp_path, manifest)

		from lang.drift.cli import main as cli_main
		rc = cli_main([
			"build",
			"--manifest", str(tmp_path / "drift" / "manifest.json"),
			"--driftc", str(DRIFTC_BIN),
		])
		assert rc == 0
		assert (tmp_path / "build" / "test-pkg.dmp").exists()
