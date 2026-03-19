# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Tests for drift-manifest.json manifest loading and validation.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tools.drift_deploy.manifest import (
	Artifact,
	Manifest,
	ManifestError,
	NativeDep,
	PackageDep,
	load_manifest,
)


def _write_manifest(tmpdir: Path, obj: dict) -> Path:
	path = tmpdir / "drift-manifest.json"
	path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
	return path


def _minimal_manifest(**overrides: object) -> dict:
	base = {
		"schema_version": 1,
		"project": {"name": "test-project", "license": "MIT"},
		"artifacts": [
			{
				"kind": "package",
				"name": "test.pkg",
				"version": "1.0.0",
				"description": "Test package",
				"entry_module": "src/lib.drift",
				"modules": ["src/"],
			}
		],
	}
	base.update(overrides)
	return base


class TestManifestValid:
	def test_minimal_package(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), _minimal_manifest())
			m = load_manifest(path)
			assert m.schema_version == 1
			assert m.project.name == "test-project"
			assert m.project.license == "MIT"
			assert m.project.author_profile is None
			assert len(m.artifacts) == 1
			art = m.artifacts[0]
			assert art.kind == "package"
			assert art.name == "test.pkg"
			assert art.version == "1.0.0"
			assert art.license == "MIT"  # inherited
			assert art.entry_module == "src/lib.drift"
			assert art.modules == ["src/"]
			assert art.package_deps == []
			assert art.native_deps == []
			assert art.assets == []
			assert art.smoke_command is None

	def test_author_profile_parsed(self) -> None:
		manifest = _minimal_manifest()
		manifest["project"]["author_profile"] = "acme.author-profile"
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.project.author_profile == "acme.author-profile"

	def test_author_profile_empty_string_rejected(self) -> None:
		manifest = _minimal_manifest()
		manifest["project"]["author_profile"] = ""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="author_profile"):
				load_manifest(path)

	def test_full_artifact(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"] = [
			{
				"kind": "package",
				"name": "net.tls",
				"version": "0.3.0",
				"description": "TLS library",
				"license": "Apache-2.0",
				"entry_module": "src/lib.drift",
				"modules": ["src/net_tls/"],
				"package_deps": [
					{"name": "acme.crypto", "version": "^0.9.0"},
				],
				"native_deps": [
					{"lib": "ssl"},
					{"lib": "crypto"},
				],
				"assets": ["docs/"],
				"smoke_command": ["just", "smoke-net-tls"],
			}
		]
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			art = m.artifacts[0]
			assert art.license == "Apache-2.0"  # overridden
			assert len(art.package_deps) == 1
			assert art.package_deps[0].name == "acme.crypto"
			assert art.package_deps[0].version == "^0.9.0"
			assert len(art.native_deps) == 2
			assert art.native_deps[0].lib == "ssl"
			assert art.smoke_command == ["just", "smoke-net-tls"]

	def test_multi_artifact(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"] = [
			{
				"kind": "package",
				"name": "net.tls",
				"version": "0.3.0",
				"description": "TLS library",
				"entry_module": "src/lib.drift",
				"modules": ["src/"],
			},
			{
				"kind": "app",
				"name": "tls-tool",
				"version": "0.3.0",
				"description": "TLS CLI tool",
				"entry_module": "src/main.drift",
				"modules": ["src/"],
				"package_deps": [{"name": "net.tls", "version": "^0.3.0"}],
			},
		]
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert len(m.artifacts) == 2
			assert m.artifacts[0].kind == "package"
			assert m.artifacts[1].kind == "app"


class TestManifestInvalid:
	def test_missing_file(self) -> None:
		with pytest.raises(ManifestError, match="not found"):
			load_manifest(Path("/nonexistent/drift-manifest.json"))

	def test_wrong_schema_version(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), {
				"schema_version": 99,
				"project": {"name": "x", "license": "MIT"},
				"artifacts": [],
			})
			with pytest.raises(ManifestError, match="schema_version"):
				load_manifest(path)

	def test_missing_project(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), {
				"schema_version": 1,
				"artifacts": [{"kind": "package", "name": "x", "version": "1.0.0",
					"description": "x", "entry_module": "x.drift", "modules": ["x/"]}],
			})
			with pytest.raises(ManifestError, match="project"):
				load_manifest(path)

	def test_empty_artifacts(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), {
				"schema_version": 1,
				"project": {"name": "x", "license": "MIT"},
				"artifacts": [],
			})
			with pytest.raises(ManifestError, match="non-empty"):
				load_manifest(path)

	def test_invalid_kind(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["kind"] = "library"
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="kind"):
				load_manifest(path)

	def test_duplicate_artifact_name(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"].append(manifest["artifacts"][0].copy())
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="duplicate"):
				load_manifest(path)

	def test_package_depends_on_app(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"] = [
			{
				"kind": "app",
				"name": "my-app",
				"version": "1.0.0",
				"description": "An app",
				"entry_module": "main.drift",
				"modules": ["src/"],
			},
			{
				"kind": "package",
				"name": "my.pkg",
				"version": "1.0.0",
				"description": "A package",
				"entry_module": "lib.drift",
				"modules": ["src/"],
				"package_deps": [{"name": "my-app", "version": "^1.0.0"}],
			},
		]
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="cannot depend on app"):
				load_manifest(path)

	def test_empty_smoke_command(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["smoke_command"] = []
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="smoke_command"):
				load_manifest(path)
