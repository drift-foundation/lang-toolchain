# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Tests for drift/manifest.json manifest loading and validation.
"""

from __future__ import annotations

import json
import tempfile
from lang.test_support.drift_tmp import session_root
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), _minimal_manifest())
			m = load_manifest(path)
			assert m.schema_version == 2
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.project.author_profile == "acme.author-profile"

	def test_author_profile_empty_string_rejected(self) -> None:
		manifest = _minimal_manifest()
		manifest["project"]["author_profile"] = ""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="author_profile"):
				load_manifest(path)

	def test_kind_library_deprecated_to_package(self, capsys) -> None:
		"""Legacy 'kind: library' is accepted, normalized to package, and warns (v2)."""
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["kind"] = "library"
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.artifacts[0].kind == "package"
			captured = capsys.readouterr()
			assert "deprecated" in captured.err
			assert "package" in captured.err

	def test_kind_package_accepted_directly(self, capsys) -> None:
		"""Canonical kind: package is accepted without a deprecation warning (v2)."""
		manifest = _minimal_manifest()
		assert manifest["artifacts"][0]["kind"] == "package"
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.artifacts[0].kind == "package"
			captured = capsys.readouterr()
			assert "deprecated" not in captured.err

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
				"assets": ["doc/"],
				"smoke_command": ["just", "smoke-net-tls"],
			}
		]
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert len(m.artifacts) == 2
			assert m.artifacts[0].kind == "package"
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			m = load_manifest(path)
			assert m.artifacts[0].entry_point == ""


class TestManifestInvalid:
	def test_missing_file(self) -> None:
		with pytest.raises(ManifestError, match="not found"):
			load_manifest(Path("/nonexistent/drift/manifest.json"))

	def test_wrong_schema_version(self) -> None:
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), {
				"schema_version": 99,
				"project": {"name": "x", "license": "MIT"},
				"artifacts": [],
			})
			with pytest.raises(ManifestError, match="schema_version"):
				load_manifest(path)

	def test_missing_project(self) -> None:
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), {
				"schema_version": 2,
				"artifacts": [{"kind": "package", "name": "x", "version": "1.0.0",
					"description": "x", "entry_module": "x.drift", "modules": ["x/"]}],
			})
			with pytest.raises(ManifestError, match="project"):
				load_manifest(path)

	def test_empty_artifacts(self) -> None:
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="kind"):
				load_manifest(path)

	def test_duplicate_artifact_name(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"].append(manifest["artifacts"][0].copy())
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="cannot depend on app"):
				load_manifest(path)

	def test_empty_smoke_command(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["smoke_command"] = []
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), manifest)
			with pytest.raises(ManifestError, match="smoke_command"):
				load_manifest(path)

	def test_entry_point_on_package_rejected(self) -> None:
		manifest = _minimal_manifest()
		manifest["artifacts"][0]["entry_point"] = "my.pkg::main"
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("0.3"))
			m = load_manifest(path)
			assert m.artifacts[0].package_deps[0].version == "0.3"

	def test_major_only_range_accepted(self) -> None:
		"""`"1"` (major-only range — any 1.x.x) loads cleanly."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("1"))
			m = load_manifest(path)
			assert m.artifacts[0].package_deps[0].version == "1"

	def test_three_part_exact_pin_rejected(self) -> None:
		"""`"0.3.14"` → targeted diagnostic pointing at `"0.3"`."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("^0.3.0"))
			with pytest.raises(ManifestError) as exc:
				load_manifest(path)
			msg = str(exc.value)
			assert "^" in msg
			assert "0.3" in msg

	def test_tilde_range_rejected(self) -> None:
		"""`"~0.3.14"` → targeted diagnostic about `~` removal."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("~0.3.14"))
			with pytest.raises(ManifestError) as exc:
				load_manifest(path)
			msg = str(exc.value)
			assert "~" in msg

	def test_garbage_version_rejected(self) -> None:
		"""Non-matching string → generic rejection with guidance."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("latest"))
			with pytest.raises(ManifestError, match="declared acceptable range"):
				load_manifest(path)

	def test_four_part_rejected(self) -> None:
		"""`"0.3.14.1"` must be rejected."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("0.3.14.1"))
			with pytest.raises(ManifestError, match="declared acceptable range"):
				load_manifest(path)

	def test_dotted_zero_rejected(self) -> None:
		"""`"0."` (trailing dot) must be rejected."""
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
			path = _write_manifest(Path(tmpdir), self._manifest_with_dep_version("0."))
			with pytest.raises(ManifestError, match="declared acceptable range"):
				load_manifest(path)

	def test_no_compatibility_framing_in_diagnostics(self) -> None:
		"""Diagnostics must not imply Drift enforces compatibility.
		Drift only enforces the owner's declared range; semantic
		compatibility is the owner's choice.  Keep 'compatible' /
		'compatibility' out of the authored-manifest error text."""
		for ver in ("0.3.14", "^0.3.0", "latest"):
			with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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
		with tempfile.TemporaryDirectory(dir=str(session_root())) as tmpdir:
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


class TestManifestMigrate:
	"""Phase 7: explicit v1 → v2 migration via ``drift manifest migrate``.

	Contract (pinned by these tests):

	- migration is opt-in; normal reads never rewrite the file.
	- per-dep rewrite: ``M.N.P`` → ``M.N``; ``M``/``M.N`` unchanged.
	- unsupported shapes (``^``/``~``/garbage/4-part) fail all-or-nothing.
	- running twice is a no-op.
	- ``drift prepare`` does not silently migrate.
	"""

	def _v1(self, *, artifacts: list[dict]) -> dict:
		return {
			"schema_version": 1,
			"project": {"name": "p", "license": "MIT"},
			"artifacts": artifacts,
		}

	def _art(self, name: str, *, deps: list[dict] | None = None) -> dict:
		out = {
			"kind": "library", "name": name, "version": "1.0.0",
			"description": name, "entry_module": f"{name}.drift",
			"modules": [f"{name}/"],
		}
		if deps is not None:
			out["package_deps"] = deps
		return out

	def test_v1_mnp_collapses_to_mn(self, tmp_path: Path) -> None:
		"""Primary regression pin: `"net-tls": "0.3.14"` → `"0.3"`."""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, self._v1(artifacts=[
			self._art("app", deps=[{"name": "net-tls", "version": "0.3.14"}]),
		]))
		assert migrate(["--manifest", str(path)]) == 0
		migrated = json.loads(path.read_text())
		assert migrated["schema_version"] == 2
		assert migrated["artifacts"][0]["package_deps"][0]["version"] == "0.3"
		# Re-reading through the normal loader must now succeed.
		manifest = load_manifest(path)
		assert manifest.artifacts[0].package_deps[0].version == "0.3"

	def test_v1_major_only_preserved(self, tmp_path: Path) -> None:
		"""Already-valid owner-declared ranges survive migration byte-identical."""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, self._v1(artifacts=[
			self._art("app", deps=[
				{"name": "dep-a", "version": "1"},       # owner range
				{"name": "dep-b", "version": "2.7"},    # owner range
				{"name": "dep-c", "version": "0.3.14"}, # v1 exact
			]),
		]))
		assert migrate(["--manifest", str(path)]) == 0
		deps = json.loads(path.read_text())["artifacts"][0]["package_deps"]
		# M preserved; M.N preserved; M.N.P collapsed.
		assert [d["version"] for d in deps] == ["1", "2.7", "0.3"]

	def test_v2_exact_mnp_rejected_by_normal_reads(self, tmp_path: Path) -> None:
		"""Migration writes the v2 shape; a manifest with a residual
		`M.N.P` is still rejected by the normal loader — the migrator
		is the ONLY path that rewrites exact pins.  Guards against
		`load_manifest` silently accepting exact pins."""
		path = _write_manifest(tmp_path, _minimal_manifest(artifacts=[
			{
				"kind": "library", "name": "p", "version": "1.0.0",
				"description": "p", "entry_module": "p.drift",
				"modules": ["p/"],
				"package_deps": [{"name": "dep-a", "version": "0.3.14"}],
			},
		]))
		with pytest.raises(ManifestError) as exc:
			load_manifest(path)
		assert "0.3.14" in str(exc.value)
		assert "0.3" in str(exc.value)  # suggestion

	def test_drift_prepare_does_not_migrate(self, tmp_path: Path) -> None:
		"""`drift prepare` must refuse a v1 manifest, not rewrite it.
		Pins the "normal reads never mutate authored files" rule."""
		from tools.drift_deploy.drift_prepare import run as prepare
		from tools.drift_deploy.drift_prepare import PrepareError  # noqa: F401
		path = _write_manifest(tmp_path, self._v1(artifacts=[
			self._art("app", deps=[{"name": "dep-a", "version": "0.3.14"}]),
		]))
		raw_before = path.read_text()
		rc = prepare(["--manifest", str(path)])
		assert rc == 1
		# File unchanged byte-for-byte.
		assert path.read_text() == raw_before

	def test_migration_is_idempotent(self, tmp_path: Path) -> None:
		"""Running migrate twice → second run is a no-op (exit 0, file
		untouched)."""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, self._v1(artifacts=[
			self._art("app", deps=[{"name": "dep-a", "version": "0.3.14"}]),
		]))
		assert migrate(["--manifest", str(path)]) == 0
		after_first = path.read_text()
		mtime_first = path.stat().st_mtime_ns
		# Small sleep not needed — we check byte-identity AND compare
		# mtimes, which the migrator leaves alone on a no-op.
		assert migrate(["--manifest", str(path)]) == 0
		assert path.read_text() == after_first
		assert path.stat().st_mtime_ns == mtime_first, (
			"second migrate must not touch the file (mtime changed)"
		)

	def test_unsupported_version_aborts_without_rewrite(self, tmp_path: Path,
	                                                    capsys) -> None:
		"""An unsupported version in ANY dep aborts the whole
		migration with a clear error.  The file must be byte-identical
		after the failed run — the all-or-nothing rewrite guarantee."""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, self._v1(artifacts=[
			self._art("app", deps=[
				{"name": "ok-1", "version": "0.3.14"},   # would convert
				{"name": "bad",  "version": "^1.0.0"},  # forbidden
				{"name": "ok-2", "version": "2.7"},     # already v2
			]),
		]))
		raw_before = path.read_text()
		rc = migrate(["--manifest", str(path)])
		assert rc == 1
		# File untouched — no partial rewrite.
		assert path.read_text() == raw_before
		err = capsys.readouterr().err
		assert "bad" in err
		assert "^1.0.0" in err
		assert "not been modified" in err or "NOT been modified" in err

	def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
		"""`--dry-run` prints the plan but must not touch the file."""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, self._v1(artifacts=[
			self._art("app", deps=[{"name": "dep-a", "version": "0.3.14"}]),
		]))
		raw_before = path.read_text()
		assert migrate(["--manifest", str(path), "--dry-run"]) == 0
		assert path.read_text() == raw_before

	def test_v2_idempotent_no_op(self, tmp_path: Path) -> None:
		"""Running migrate on an already-v2 manifest (no rewrites
		needed) is exit-0 and leaves the file alone."""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, _minimal_manifest())
		raw_before = path.read_text()
		mtime_before = path.stat().st_mtime_ns
		assert migrate(["--manifest", str(path)]) == 0
		assert path.read_text() == raw_before
		assert path.stat().st_mtime_ns == mtime_before

	def test_bogus_schema_version_rejected(self, tmp_path: Path) -> None:
		"""Only v1 and v2 are accepted inputs to the migrator."""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, {
			"schema_version": 99,
			"project": {"name": "p", "license": "MIT"},
			"artifacts": [self._art("app")],
		})
		raw_before = path.read_text()
		rc = migrate(["--manifest", str(path)])
		assert rc == 1
		assert path.read_text() == raw_before

	def test_missing_manifest_errors(self, tmp_path: Path) -> None:
		from tools.drift_deploy.drift_manifest import run as migrate
		missing = tmp_path / "does-not-exist.json"
		assert migrate(["--manifest", str(missing)]) == 1

	def test_duplicate_artifact_names_do_not_cross_contaminate(
		self, tmp_path: Path,
	) -> None:
		"""Regression pin for K Finding 1: a malformed v1 manifest
		with two artifacts of the same name must NOT have rewrites
		from one leak into the other.  Migration applies positionally
		(by ``artifact_index``), not by artifact name — the name is
		only ever used for diagnostics.

		The normal manifest loader rejects duplicate names (separately
		tested), but the migrator runs before that validation and must
		not silently scramble a partly-duplicated file.  Each artifact
		must see exactly its own rewrites.
		"""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, {
			"schema_version": 1,
			"project": {"name": "p", "license": "MIT"},
			"artifacts": [
				# Same artifact name at positions [0] and [1], with
				# DIFFERENT dep versions.  If the migrator keyed by
				# name it would apply both rewrites to both slots —
				# scrambling the second artifact's "0.5.7" into the
				# first's "0.3.14" slot, or vice versa.
				{
					"kind": "library", "name": "twin", "version": "1.0.0",
					"description": "a", "entry_module": "a.drift",
					"modules": ["a/"],
					"package_deps": [{"name": "dep", "version": "0.3.14"}],
				},
				{
					"kind": "library", "name": "twin", "version": "2.0.0",
					"description": "b", "entry_module": "b.drift",
					"modules": ["b/"],
					"package_deps": [{"name": "dep", "version": "0.5.7"}],
				},
			],
		})
		assert migrate(["--manifest", str(path)]) == 0
		migrated = json.loads(path.read_text())
		# Each artifact keeps its own rewrite; no cross-contamination.
		assert migrated["artifacts"][0]["package_deps"][0]["version"] == "0.3"
		assert migrated["artifacts"][1]["package_deps"][0]["version"] == "0.5"
		# Version field of the artifacts themselves untouched — only
		# `package_deps[].version` is rewritten.
		assert migrated["artifacts"][0]["version"] == "1.0.0"
		assert migrated["artifacts"][1]["version"] == "2.0.0"

	def test_unrelated_fields_preserved(self, tmp_path: Path) -> None:
		"""Non-dep fields survive the round-trip unchanged."""
		from tools.drift_deploy.drift_manifest import run as migrate
		path = _write_manifest(tmp_path, {
			"schema_version": 1,
			"project": {"name": "my-proj", "license": "Apache-2.0",
				"author_profile": "profiles/me.author-profile"},
			"artifacts": [{
				"kind": "library", "name": "lib", "version": "1.2.3",
				"description": "a thing", "entry_module": "lib.drift",
				"modules": ["lib/", "lib/util/"],
				"unsafe": False,
				"module_namespace": "my_lib",
				"assets": ["assets/logo.png"],
				"package_deps": [{"name": "dep-a", "version": "0.3.14"}],
				"native_deps": [{"lib": "ssl"}],
			}],
		})
		assert migrate(["--manifest", str(path)]) == 0
		migrated = json.loads(path.read_text())
		art = migrated["artifacts"][0]
		assert migrated["project"]["name"] == "my-proj"
		assert migrated["project"]["license"] == "Apache-2.0"
		assert migrated["project"]["author_profile"] == "profiles/me.author-profile"
		assert art["version"] == "1.2.3"
		assert art["modules"] == ["lib/", "lib/util/"]
		assert art["assets"] == ["assets/logo.png"]
		assert art["native_deps"] == [{"lib": "ssl"}]
		assert art["module_namespace"] == "my_lib"
		# The only thing that changed: the dep version and schema_version.
		assert art["package_deps"] == [{"name": "dep-a", "version": "0.3"}]
		assert migrated["schema_version"] == 2
