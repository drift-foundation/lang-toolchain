# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Tests for drift/manifest.json manifest loading and validation.
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
	drift_dir = tmpdir / "drift"
	drift_dir.mkdir(exist_ok=True)
	path = drift_dir / "manifest.json"
	path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
	return path


def _minimal_manifest(**overrides: object) -> dict:
	base = {
		"schema_version": 2,
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
			assert m.schema_version == 2
			assert m.project.name == "test-project"
			assert m.project.license == "MIT"
			assert m.project.author_profile is None
			assert len(m.artifacts) == 1
			art = m.artifacts[0]
			assert art.kind == "library"
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

	def test_kind_package_deprecated_to_library(self, capsys) -> None:
		"""Legacy 'kind: package' is accepted, normalized to library, and warns."""
		manifest = _minimal_manifest()
		assert manifest["artifacts"][0]["kind"] == "package"
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.artifacts[0].kind == "library"
			captured = capsys.readouterr()
			assert "deprecated" in captured.err
			assert "library" in captured.err

	def test_kind_library_accepted_directly(self) -> None:
		"""kind: library is accepted without deprecation warning."""
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["kind"] = "library"
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.artifacts[0].kind == "library"

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
					{"name": "acme.crypto", "version": "0.9"},
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
			assert art.package_deps[0].version == "0.9"
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
				"package_deps": [{"name": "net.tls", "version": "0.3"}],
			},
		]
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert len(m.artifacts) == 2
			assert m.artifacts[0].kind == "library"
			assert m.artifacts[1].kind == "app"

	def test_app_entry_point(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"] = [
			{
				"kind": "app",
				"name": "bookkeeper",
				"version": "1.0.0",
				"description": "Bookkeeper app",
				"entry_module": "src/lib.drift",
				"modules": ["src/"],
				"entry_point": "pushcoin.bookkeeper::main",
			}
		]
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.artifacts[0].entry_point == "pushcoin.bookkeeper::main"

	def test_app_default_entry_point_empty(self) -> None:
		"""App without entry_point defaults to empty (driftc uses main::main)."""
		manifest = _minimal_manifest()
		manifest["artifacts"] = [
			{
				"kind": "app",
				"name": "my-app",
				"version": "1.0.0",
				"description": "An app",
				"entry_module": "src/main.drift",
				"modules": ["src/"],
			}
		]
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.artifacts[0].entry_point == ""


class TestManifestInvalid:
	def test_missing_file(self) -> None:
		with pytest.raises(ManifestError, match="not found"):
			load_manifest(Path("/nonexistent/drift/manifest.json"))

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
				"schema_version": 2,
				"artifacts": [{"kind": "package", "name": "x", "version": "1.0.0",
					"description": "x", "entry_module": "x.drift", "modules": ["x/"]}],
			})
			with pytest.raises(ManifestError, match="project"):
				load_manifest(path)

	def test_empty_artifacts(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), {
				"schema_version": 2,
				"project": {"name": "x", "license": "MIT"},
				"artifacts": [],
			})
			with pytest.raises(ManifestError, match="non-empty"):
				load_manifest(path)

	def test_invalid_kind(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["kind"] = "unknown_kind"
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
				"package_deps": [{"name": "my-app", "version": "1.0"}],
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

	def test_entry_point_on_package_rejected(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["entry_point"] = "my.pkg::main"
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="only valid for app"):
				load_manifest(path)

	def test_entry_point_missing_double_colon(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"] = [
			{
				"kind": "app",
				"name": "my-app",
				"version": "1.0.0",
				"description": "An app",
				"entry_module": "src/main.drift",
				"modules": ["src/"],
				"entry_point": "just_a_function",
			}
		]
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="module::fn"):
				load_manifest(path)


# ── 0.29.0 manifest v2: owner-declared acceptable range only ────────


class TestManifestV2DepVersions:
	"""v2 authored manifests accept the package owner's declared
	acceptable range in dep versions, as either `"M"` (any M.x.x)
	or `"M.N"` (any M.N.x).  Exact pins, `^`/`~` operators, and
	other shapes are hard-rejected at load time.

	The broader `parse_constraint` vocabulary in `semver.py` keeps
	`^`/`~`/exact for strictly internal use (lock-v3 exact entries,
	v1→v2 manifest migration, resolver unit tests).  That vocabulary
	must NOT leak into v2 authored-manifest validation; pre-cut
	packages without v2 `required_deps` are rejected at consume
	time (Phase 4), not silently accepted via a legacy shim.  These
	tests pin the authored-manifest boundary.
	"""

	def _manifest_with_dep_version(self, ver: str) -> dict:
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["package_deps"] = [
			{"name": "some.dep", "version": ver},
		]
		return manifest

	def test_mn_range_accepted(self) -> None:
		"""`"0.3"` (M.N range) loads cleanly."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("0.3"))
			m = load_manifest(path)
			assert m.artifacts[0].package_deps[0].version == "0.3"

	def test_major_only_range_accepted(self) -> None:
		"""`"1"` (major-only range — any 1.x.x) loads cleanly."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("1"))
			m = load_manifest(path)
			assert m.artifacts[0].package_deps[0].version == "1"

	def test_three_part_exact_pin_rejected(self) -> None:
		"""`"0.3.14"` → targeted diagnostic pointing at `"0.3"`."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("0.3.14"))
			with pytest.raises(ManifestError) as exc:
				load_manifest(path)
			msg = str(exc.value)
			assert "exact version '0.3.14'" in msg
			assert "'0.3'" in msg  # suggested replacement
			assert "drift/lock.json" in msg  # points at correct layer
			assert "declared acceptable range" in msg  # positive framing

	def test_caret_range_rejected(self) -> None:
		"""`"^0.3.0"` → targeted diagnostic about `^` removal."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("^0.3.0"))
			with pytest.raises(ManifestError) as exc:
				load_manifest(path)
			msg = str(exc.value)
			assert "^" in msg
			assert "0.3" in msg

	def test_tilde_range_rejected(self) -> None:
		"""`"~0.3.14"` → targeted diagnostic about `~` removal."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("~0.3.14"))
			with pytest.raises(ManifestError) as exc:
				load_manifest(path)
			msg = str(exc.value)
			assert "~" in msg

	def test_garbage_version_rejected(self) -> None:
		"""Non-matching string → generic rejection with guidance."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("latest"))
			with pytest.raises(ManifestError, match="declared acceptable range"):
				load_manifest(path)

	def test_four_part_rejected(self) -> None:
		"""`"0.3.14.1"` must be rejected."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("0.3.14.1"))
			with pytest.raises(ManifestError, match="declared acceptable range"):
				load_manifest(path)

	def test_dotted_zero_rejected(self) -> None:
		"""`"0."` (trailing dot) must be rejected."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("0."))
			with pytest.raises(ManifestError, match="declared acceptable range"):
				load_manifest(path)

	def test_no_compatibility_framing_in_diagnostics(self) -> None:
		"""Diagnostics must not imply Drift enforces compatibility.
		Drift only enforces the owner's declared range; semantic
		compatibility is the owner's choice.  Keep 'compatible' /
		'compatibility' out of the authored-manifest error text."""
		for ver in ("0.3.14", "^0.3.0", "latest"):
			with tempfile.TemporaryDirectory() as tmpdir:
				path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version(ver))
				with pytest.raises(ManifestError) as exc:
					load_manifest(path)
				msg = str(exc.value).lower()
				assert "compatible" not in msg, (
					f"diagnostic for '{ver}' uses 'compatible' framing; "
					f"Drift does not enforce compatibility — use 'declared "
					f"range' / 'acceptable range' instead.\nmsg: {msg}"
				)
				assert "compatibility" not in msg, (
					f"diagnostic for '{ver}' uses 'compatibility' framing; "
					f"Drift does not enforce compatibility — use 'declared "
					f"range' / 'acceptable range' instead.\nmsg: {msg}"
				)


class TestManifestV1Rejection:
	"""v1 manifests are no longer directly loadable.  The `drift
	manifest migrate` subcommand (Phase 7) is the only path that
	accepts v1; normal `load_manifest` reads reject with a pointer
	to that subcommand.  This prevents silent acceptance of the old
	exact-pin form."""

	def test_v1_schema_version_rejected_with_migrate_pointer(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = _write_manifest(Path(tmpdir), {
				"schema_version": 1,
				"project": {"name": "x", "license": "MIT"},
				"artifacts": [{"kind": "package", "name": "x", "version": "1.0.0",
					"description": "x", "entry_module": "x.drift", "modules": ["x/"]}],
			})
			with pytest.raises(ManifestError) as exc:
				load_manifest(path)
			msg = str(exc.value)
			assert "schema v1" in msg
			assert "drift manifest migrate" in msg
			assert "drift/lock.json" in msg
