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
	"schema_version": 2,
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


def _fake_scid(pkg_id: str, version: str) -> str:
	"""Deterministic fake source_content_id for tests, matching the
	formula `_write_lock` uses internally so a mocked PackageEntry
	can declare a matching SCID without re-deriving the secret."""
	import hashlib as _hl
	return "sha256:" + _hl.sha256(f"src:{pkg_id}@{version}".encode()).hexdigest()


def _write_lock(tmp_path: Path, artifacts: dict, *, author_key: str = "ed25519:test") -> Path:
	"""Write a v4 lock for tests.  `deps.items()` carries exact
	`M.N.P` versions; this helper adds a deterministic fake sha256
	+ source_content_id per entry, both derived from `(pkg_id,
	version)` so re-running the same test produces byte-identical
	locks.  No range field, no file-level integrity, no redundant
	`package_id` inside entries."""
	import hashlib
	drift_dir = tmp_path / "drift"
	drift_dir.mkdir(exist_ok=True)
	lock_path = drift_dir / "lock.json"
	lock_obj = {"schema_version": 4, "artifacts": {}}
	for art_name, deps in artifacts.items():
		resolved = {}
		for pkg_id, ver in deps.items():
			# Deterministic fake values so tests are stable; real
			# builds derive these from the .dmp bytes / .source-
			# attestation sidecar respectively.
			fake_sha = hashlib.sha256(f"{pkg_id}@{ver}".encode()).hexdigest()
			fake_scid = "sha256:" + hashlib.sha256(
				f"src:{pkg_id}@{ver}".encode()
			).hexdigest()
			resolved[pkg_id] = {
				"version": ver,
				"sha256": fake_sha,
				"author_key": author_key,
				"source_content_id": fake_scid,
				"source_attestation_key": author_key,
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


class TestModulesDirectoryEntries:
	"""v2 `modules[]` entries may be directories (scanned recursively for
	`.drift`) as well as explicit files.  The expansion is shared between
	the compile path (`build_source_args`) and the SCI path
	(`compute_artifact_sci`), so a directory listing and the equivalent
	file listing over the same tree compile the same sources AND sign the
	same `source_content_id`."""

	def _tree(self, tmp_path: Path):
		root = tmp_path / "proj"
		(root / "src" / "handlers").mkdir(parents=True)
		(root / "src" / "app.drift").write_text("module myapp;\n", encoding="utf-8")
		(root / "src" / "util.drift").write_text("module myapp.util;\n", encoding="utf-8")
		(root / "src" / "handlers" / "h.drift").write_text(
			"module myapp.handlers.h;\n", encoding="utf-8")
		(root / "src" / "notes.txt").write_text("not drift\n", encoding="utf-8")  # ignored
		return root

	def test_directory_entry_expands_recursively_sorted(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_module_files
		root = self._tree(tmp_path)
		got = resolve_module_files(["src/"], source_root=root)
		assert got == [
			"src/app.drift",
			"src/handlers/h.drift",
			"src/util.drift",
		]  # recursive, sorted, .txt ignored

	def test_build_source_args_expands_directory(self, tmp_path: Path):
		root = self._tree(tmp_path)
		art = _make_artifact(entry_module="src/app.drift", modules=["src/"])
		result = build_source_args(art, root / "drift")  # manifest_dir → project root is `root`
		# entry_module first, every .drift under src/ present, no dup of app.drift.
		assert result[0] == str(root / "src" / "app.drift")
		assert str(root / "src" / "util.drift") in result
		assert str(root / "src" / "handlers" / "h.drift") in result
		assert result.count(str(root / "src" / "app.drift")) == 1

	def test_file_and_directory_listings_yield_same_sci(self, tmp_path: Path):
		"""Signing-compat: switching `["src/.."]` files ↔ `["src/"]` over the
		same tree must NOT change the author-claim SCI (trust-v1 §3.5)."""
		from lang.driftc.packages.manifest import compute_artifact_sci
		root = self._tree(tmp_path)
		mdir = root / "drift"
		art_dir = _make_artifact(entry_module="src/app.drift", modules=["src/"])
		art_files = _make_artifact(
			entry_module="src/app.drift",
			modules=["src/app.drift", "src/util.drift", "src/handlers/h.drift"],
		)
		assert compute_artifact_sci(art_dir, manifest_dir=mdir) == \
			compute_artifact_sci(art_files, manifest_dir=mdir)

	def test_mixed_file_and_directory_dedups(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_module_files
		root = self._tree(tmp_path)
		got = resolve_module_files(["src/app.drift", "src/"], source_root=root)
		assert got.count("src/app.drift") == 1
		assert "src/handlers/h.drift" in got

	def test_empty_directory_is_clean_error(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_module_files, ManifestError
		root = tmp_path / "proj"
		(root / "empty").mkdir(parents=True)
		with pytest.raises(ManifestError, match="contains no `.drift`"):
			resolve_module_files(["empty/"], source_root=root)

	def test_nonexistent_path_passes_through(self, tmp_path: Path):
		# Preserves the downstream missing-source diagnostic instead of a
		# raw IsADirectoryError / silent drop.
		from lang.driftc.packages.manifest import resolve_module_files
		root = tmp_path / "proj"; root.mkdir()
		assert resolve_module_files(["src/gone.drift"], source_root=root) == ["src/gone.drift"]

	# ── source-root containment (same threat the SCI path guards) ──

	def test_parent_directory_entry_rejected(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_module_files, ManifestError
		root = self._tree(tmp_path)
		(tmp_path / "outside").mkdir()
		(tmp_path / "outside" / "evil.drift").write_text("module evil;\n", encoding="utf-8")
		with pytest.raises(ManifestError, match="escape the tree"):
			resolve_module_files(["../outside/"], source_root=root)

	def test_symlinked_directory_escaping_root_rejected(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_module_files, ManifestError
		root = self._tree(tmp_path)
		outside = tmp_path / "outside"; outside.mkdir()
		(outside / "evil.drift").write_text("module evil;\n", encoding="utf-8")
		link = root / "src" / "link-to-outside"
		try:
			link.symlink_to(outside, target_is_directory=True)
		except (OSError, NotImplementedError):
			pytest.skip("symlinks not supported on this platform")
		with pytest.raises(ManifestError, match="escape the tree"):
			resolve_module_files(["src/link-to-outside/"], source_root=root)

	def test_symlinked_file_under_dir_escaping_root_rejected(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_module_files, ManifestError
		root = self._tree(tmp_path)
		outside = tmp_path / "outside"; outside.mkdir()
		(outside / "evil.drift").write_text("module evil;\n", encoding="utf-8")
		link = root / "src" / "aliased.drift"
		try:
			link.symlink_to(outside / "evil.drift")
		except (OSError, NotImplementedError):
			pytest.skip("symlinks not supported on this platform")
		with pytest.raises(ManifestError, match="escape the tree"):
			resolve_module_files(["src/"], source_root=root)

	def test_in_tree_symlink_to_in_tree_file_allowed(self, tmp_path: Path):
		# Consistent with SCI policy: a symlink whose target stays under the
		# root is permitted; the LOGICAL (symlink) path is what's recorded.
		from lang.driftc.packages.manifest import resolve_module_files
		root = self._tree(tmp_path)
		link = root / "src" / "alias.drift"
		try:
			link.symlink_to(root / "src" / "util.drift")
		except (OSError, NotImplementedError):
			pytest.skip("symlinks not supported on this platform")
		got = resolve_module_files(["src/"], source_root=root)
		assert "src/alias.drift" in got  # logical path kept, target is in-tree


# ── asset directory + symlink expansion (resolve_asset_files) ────────


class TestAssetDirectoryEntries:
	"""`resolve_asset_files` mirrors `resolve_module_files` but matches ALL
	files (any extension) and applies the explicit asset symlink policy."""

	def _tree(self, tmp_path: Path) -> Path:
		root = tmp_path / "proj"
		(root / "assets" / "db").mkdir(parents=True)
		(root / "assets" / "db" / "0001_init.sql").write_text("CREATE TABLE t();\n")
		(root / "assets" / "db" / "0002_add.sql").write_text("ALTER TABLE t;\n")
		(root / "assets" / "README.md").write_text("# assets\n")
		return root

	def test_directory_expands_recursively_all_extensions(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_asset_files
		root = self._tree(tmp_path)
		got = resolve_asset_files(["assets/"], source_root=root)
		assert got == [
			"assets/README.md",
			"assets/db/0001_init.sql",
			"assets/db/0002_add.sql",
		]  # recursive, sorted, ALL extensions (not just .drift)

	def test_file_and_directory_listings_yield_same_sci(self, tmp_path: Path):
		"""Switching a directory entry ↔ its explicit files must NOT change the
		SCI — the packing path and the signed identity stay in lock-step."""
		from lang.driftc.packages.manifest import compute_artifact_sci
		root = self._tree(tmp_path)
		# A module is required for a library SCI; reuse the asset tree's root.
		(root / "src").mkdir()
		(root / "src" / "lib.drift").write_text("module p;\n")
		mdir = root / "drift"
		art_dir = _make_artifact(
			entry_module="src/lib.drift", modules=["src/lib.drift"], assets=["assets/"])
		art_files = _make_artifact(
			entry_module="src/lib.drift", modules=["src/lib.drift"],
			assets=["assets/README.md", "assets/db/0001_init.sql", "assets/db/0002_add.sql"])
		assert compute_artifact_sci(art_dir, manifest_dir=mdir) == \
			compute_artifact_sci(art_files, manifest_dir=mdir)

	def test_empty_directory_is_clean_error(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_asset_files, ManifestError
		root = tmp_path / "proj"
		(root / "assets").mkdir(parents=True)
		with pytest.raises(ManifestError, match="contains no files"):
			resolve_asset_files(["assets/"], source_root=root)

	def test_in_tree_file_symlink_allowed(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_asset_files
		root = self._tree(tmp_path)
		link = root / "assets" / "db" / "alias.sql"
		try:
			link.symlink_to(root / "assets" / "db" / "0001_init.sql")
		except (OSError, NotImplementedError):
			pytest.skip("symlinks not supported on this platform")
		got = resolve_asset_files(["assets/"], source_root=root)
		assert "assets/db/alias.sql" in got  # logical (symlink) path kept

	def test_escaping_file_symlink_rejected(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_asset_files, ManifestError
		root = self._tree(tmp_path)
		outside = tmp_path / "outside"; outside.mkdir()
		(outside / "evil.sql").write_text("DROP TABLE users;\n")
		link = root / "assets" / "db" / "evil.sql"
		try:
			link.symlink_to(outside / "evil.sql")
		except (OSError, NotImplementedError):
			pytest.skip("symlinks not supported on this platform")
		with pytest.raises(ManifestError, match="escape the tree"):
			resolve_asset_files(["assets/"], source_root=root)

	def test_dangling_symlink_rejected(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_asset_files, ManifestError
		root = self._tree(tmp_path)
		link = root / "assets" / "db" / "gone.sql"
		try:
			link.symlink_to(root / "assets" / "db" / "does-not-exist.sql")
		except (OSError, NotImplementedError):
			pytest.skip("symlinks not supported on this platform")
		with pytest.raises(ManifestError, match="dangling symlink"):
			resolve_asset_files(["assets/"], source_root=root)

	def test_symlink_to_directory_rejected(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_asset_files, ManifestError
		root = self._tree(tmp_path)
		(root / "real_extra").mkdir()
		(root / "real_extra" / "x.sql").write_text("SELECT 1;\n")
		link = root / "assets" / "linkdir"
		try:
			link.symlink_to(root / "real_extra", target_is_directory=True)
		except (OSError, NotImplementedError):
			pytest.skip("symlinks not supported on this platform")
		with pytest.raises(ManifestError, match="symlink to a directory"):
			resolve_asset_files(["assets/"], source_root=root)

	def test_nonexistent_file_passes_through(self, tmp_path: Path):
		from lang.driftc.packages.manifest import resolve_asset_files
		root = tmp_path / "proj"; root.mkdir()
		assert resolve_asset_files(["assets/x.sql"], source_root=root) == ["assets/x.sql"]


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
		resolved = {"dep-a": ResolvedDep(version="1.0.0", sha256="aabbcc", dep_type="direct", package_id="dep-a", author_key="ed25519:test")}
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

	def test_package_cmd_with_source_content_id(self):
		"""--source-content-id is plumbed through when caller supplies it."""
		art = _make_artifact()
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
			source_content_id="sha256:" + "a" * 64,
		)
		assert "--source-content-id" in cmd
		idx = cmd.index("--source-content-id")
		assert cmd[idx + 1] == "sha256:" + "a" * 64

	def test_package_cmd_omits_source_content_id_when_absent(self):
		"""Default invocation does not emit --source-content-id (Phase A
		keeps it optional; Phase C will require it for source-mode)."""
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
		assert "--source-content-id" not in cmd

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

	def test_app_cmd_forwards_sanitize_via_extra_flags(self):
		"""`drift build --sanitize=address` appends `--sanitize address` to the
		extra flags; build_app_cmd must carry it through to driftc verbatim
		(driftc owns token validation + runtime-variant selection)."""
		art = _make_artifact(kind="app", name="my-app")
		cmd = build_app_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=Path("/build/my-app"),
			manifest_dir=Path("/proj"),
			package_roots=[],
			extra_flags=["--sanitize", "address"],
		)
		idx = cmd.index("--sanitize")
		assert cmd[idx + 1] == "address"


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
			"schema_version": 2,
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
			"schema_version": 2,
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
			"schema_version": 2,
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
			"schema_version": 2,
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
						{"name": "dep-a", "version": "1.0"},
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
			"schema_version": 2,
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
						{"name": "dep-a", "version": "1.0"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		# Lock contains dep-a (direct) AND dep-b (transitive).  v4
		# shape: exact version + sha256 + source identity per entry.
		_scid_a = "sha256:" + "a"*64
		_scid_b = "sha256:" + "b"*64
		lock_obj = {
			"schema_version": 4,
			"artifacts": {
				"my-pkg": {
					"resolved": {
						"dep-a": {"version": "1.2.7", "sha256": "aa", "author_key": "unsigned", "source_content_id": _scid_a, "source_attestation_key": "ed25519:test", "dep_type": "direct"},
						"dep-b": {"version": "0.5.3", "sha256": "bb", "author_key": "unsigned", "source_content_id": _scid_b, "source_attestation_key": "ed25519:test", "dep_type": "transitive"},
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
		# Both direct and transitive deps must appear as exact --dep flags.
		assert "dep-a@1.2.7" in cmd_str
		assert "dep-b@0.5.3" in cmd_str

	def test_stale_lockfile_missing_artifact_errors(self, tmp_path):
		"""Lock exists but has no entry for this artifact → error."""
		manifest_data = {
			"schema_version": 2,
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
			"schema_version": 2,
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
						{"name": "dep-a", "version": "1.0"},
						{"name": "dep-b", "version": "2.0"},
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

	def test_error_no_lockfile_range_dep(self, tmp_path, capsys):
		"""No lock + any declared dep → hard fail pointing at `drift prepare`.

		Pins the strict-exact build contract: `drift build` never
		resolves owner-declared ranges itself.  Every artifact with
		`package_deps` MUST have a v3 lock.  The diagnostic must name
		the artifact, list the missing deps, and send the user to
		`drift prepare`.
		"""
		manifest_data = {
			"schema_version": 2,
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
						{"name": "dep-a", "version": "1.0"},
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
		err = capsys.readouterr().err
		assert "my-pkg" in err
		assert "dep-a" in err
		assert "drift prepare" in err, (
			f"missing-lock diagnostic must point at drift prepare; got:\n{err}"
		)

	def test_exact_pin_in_manifest_rejected_at_parse(self, tmp_path):
		"""Exact 3-part pins are not valid v2 manifest dep versions.

		Under the 0.29 two-layer model, the manifest carries the
		owner's declared acceptable range (`"M"` or `"M.N"`); exact
		resolved versions live in drift/lock.json.  A manifest
		containing `"1.0.0"` as a dep version fails at parse time —
		there is no "exact dep without lockfile" path, because the
		exact pin itself is not accepted.  (Prior to 0.29 the parser
		accepted exact pins and this test pinned the pre-lock-flow
		build behavior; that shape is obsolete.)
		"""
		manifest_data = {
			"schema_version": 2,
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

		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		# Non-zero exit because manifest load fails at parse.
		assert result != 0

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
			"schema_version": 2,
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
			"schema_version": 2,
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
			"schema_version": 2,
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
			"schema_version": 2,
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
			"schema_version": 2,
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
			"schema_version": 2,
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
	def test_package_dep_emits_manifest_range_not_lock_exact(self):
		"""--package-dep emits the manifest's owner-declared range,
		NOT the lock's exact resolved version.  Published .dmp
		metadata carries the PRODUCER's declared constraint so
		downstream consumers can pick up patch bumps without the
		producer republishing.

		Under v3 the lock's exact pin stays local to the producer;
		only --dep (compiler exact-loader input) uses it.
		"""
		art = _make_artifact(
			package_deps=[PackageDep(name="dep-a", version="1.0")],
		)
		resolved = {"dep-a": ResolvedDep(version="1.2.3", sha256="aabbcc", dep_type="direct", package_id="dep-a", author_key="ed25519:test")}
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps=resolved,
			output_path=Path("/build/my-pkg.dmp"),
			manifest_dir=Path("/proj"),
			package_roots=[],
		)
		# --package-dep carries the manifest range "1.0".
		dep_idx = cmd.index("--package-dep")
		assert cmd[dep_idx + 1] == "dep-a=1.0"
		joined = " ".join(cmd)
		# --dep separately carries the lock exact "1.2.3" for the compiler.
		assert "dep-a@1.2.3" in joined
		# Stale `^`/`~` vocabulary must never appear.
		assert "^" not in joined
		assert "~" not in joined

	def test_package_dep_falls_back_to_manifest_version(self):
		"""When dep is not in resolved_deps, manifest version is used.
		Under v3, `--package-dep` always uses the manifest range —
		this test pins that the fallback path (no resolved entry)
		reaches the same emission."""
		art = _make_artifact(
			package_deps=[PackageDep(name="dep-a", version="1.0")],
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
		assert cmd[dep_idx + 1] == "dep-a=1.0"

	def test_transitive_deps_excluded_from_package_dep(self):
		"""--package-dep emits only DIRECT manifest deps (with
		manifest-range versions); transitive deps are excluded from
		--package-dep and appear only via --dep (exact, from lock)
		for compiler version selection."""
		art = _make_artifact(
			package_deps=[PackageDep(name="dep-a", version="1.0")],
		)
		resolved = {
			"dep-a": ResolvedDep(version="1.2.3", sha256="aabbcc", dep_type="direct", package_id="dep-a", author_key="ed25519:test"),
			"dep-b": ResolvedDep(version="0.5.0", sha256="aabbcc", dep_type="transitive", package_id="dep-b", author_key="ed25519:test"),
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
		# --package-dep contains only the direct dep, with the
		# manifest's range (not the lock's exact).
		package_dep_values = []
		for i, flag in enumerate(cmd):
			if flag == "--package-dep":
				package_dep_values.append(cmd[i + 1])
		assert package_dep_values == ["dep-a=1.0"]
		# --dep contains BOTH direct and transitive, both exact.
		dep_values = []
		for i, flag in enumerate(cmd):
			if flag == "--dep":
				dep_values.append(cmd[i + 1])
		assert "dep-a@1.2.3" in dep_values
		assert "dep-b@0.5.0" in dep_values

	def test_manifest_range_in_package_dep_metadata(self, tmp_path):
		"""--package-dep carries the MANIFEST's declared range (the
		producer's exported constraint), NOT the lock's exact pin.
		This is how consumers pick up patch bumps without requiring
		intermediate libraries to republish.

		Shape: manifest says dep-a "1.0" (owner accepts any 1.0.x);
		lock pins dep-a to 1.2.3; published --package-dep declares
		dep-a=1.0 (the manifest constraint), not 1.2.3 (the lock pin).

		The lock's exact 1.2.3 still appears in --dep for driftc's
		own exact-loader contract; this assertion is scoped to the
		--package-dep metadata channel.
		"""
		manifest_data = {
			"schema_version": 2,
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
						{"name": "dep-a", "version": "1.0"},  # owner's declared range
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		# Lock pins dep-a exact at 1.2.3 (what prepare resolved).
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "1.2.3"}})

		from tools.drift_deploy.drift_build import run

		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 0
		cmd = mock_run.call_args[0][0]
		dep_idx = cmd.index("--package-dep")
		# --package-dep carries the manifest's range, not the lock's pin.
		assert cmd[dep_idx + 1] == "dep-a=1.0"
		joined = " ".join(cmd)
		# --dep separately carries the lock's exact for the compiler.
		assert "dep-a@1.2.3" in joined
		# v1-style `^` / `~` vocabulary never appears in the emitted command.
		assert "^" not in joined
		assert "~" not in joined


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


@pytest.mark.usefixtures("permissive_run_snapshot")
class TestLockCompatibility:
	def test_lock_compatibility_checked_against_package_roots(self, tmp_path):
		"""Lock compatibility mismatch against package roots produces early error."""
		manifest_data = {
			"schema_version": 2,
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
						{"name": "dep-a", "version": "1.0"},
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

	def test_lock_exact_version_forwarded_to_driftc(self, tmp_path):
		"""v3 lock stores exact M.N.P; build passes the exact version
		straight through to driftc --dep.  The v2 "resolve lock range
		to exact at build time" step is gone; patch movement happens
		only in `drift prepare`."""
		manifest_data = {
			"schema_version": 2,
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
						{"name": "dep-a", "version": "0.1"},
					],
				}
			],
		}
		_write_manifest(tmp_path, manifest_data)
		(tmp_path / "src").mkdir(parents=True, exist_ok=True)
		(tmp_path / "src" / "lib.drift").write_text("module my.pkg;\n")

		# Package root has exact version 0.1.3.
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib
		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()

		# v4 lock: exact version + sha256 + source identity.  sha
		# AND source identity must match the fixture in the mocked
		# package index (strict v4 verify re-checks both halves of
		# the identity against on-disk).
		fixture_sha = hashlib.sha256(b"dep-a@0.1.3").hexdigest()
		fixture_scid = _fake_scid("dep-a", "0.1.3")
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})

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
						sha256=fixture_sha,
						required_deps=[],
						author_key="ed25519:test",
						source_content_id=fixture_scid,
						source_attestation_key="ed25519:test",
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
		# Lock's exact version flows straight through to --dep.
		assert "dep-a@0.1.3" in cmd_str, (
			f"build must forward lock's exact version 0.1.3 to driftc; "
			f"got: {cmd_str}"
		)
		assert "dep-a@0.1 " not in cmd_str and not cmd_str.endswith("dep-a@0.1"), (
			f"raw lock range must not reach driftc; got: {cmd_str}"
		)

	def test_build_rejects_non_mnp_lock_version(self, tmp_path, capsys):
		"""Lock v3 must carry exact `M.N.P` for every entry.  A range
		or constraint shape in the lock is treated as corruption — the
		loader refuses to interpret it and redirects the user to
		`drift prepare`."""
		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		# Hand-write a corrupted lock (range in version field).
		import hashlib
		bad_sha = hashlib.sha256(b"dep-a@0.1").hexdigest()
		lock_obj = {
			"schema_version": 4,
			"artifacts": {
				"my-pkg": {
					"resolved": {
						"dep-a": {
							"version": "0.1",  # range, not exact — corruption
							"sha256": bad_sha,
							"author_key": "ed25519:test",
							"source_content_id": "sha256:" + "a"*64,
							"source_attestation_key": "ed25519:test",
							"dep_type": "direct",
						},
					},
				},
			},
		}
		(tmp_path / "drift" / "lock.json").write_text(
			json.dumps(lock_obj, indent=2), encoding="utf-8",
		)

		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run(["--manifest", str(tmp_path / "drift" / "manifest.json")])

		assert result == 1
		err = capsys.readouterr().err
		assert "0.1" in err
		assert "M.N.P" in err or "exact" in err.lower()
		assert "drift prepare" in err, (
			f"corrupt-lock diagnostic must point at drift prepare; got:\n{err}"
		)

	def test_build_rejects_missing_ondisk_package(self, tmp_path, capsys):
		"""Lock pins an exact version, but the on-disk package at that
		version is not present under the package roots → build fails
		with a `drift prepare` pointer."""
		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()

		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {}  # no dep-a on disk
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 1
		err = capsys.readouterr().err
		assert "dep-a" in err and "0.1.3" in err
		assert "not found" in err
		assert "drift prepare" in err, (
			f"missing-package diagnostic must point at drift prepare; got:\n{err}"
		)

	def test_build_rejects_sha_mismatch(self, tmp_path, capsys):
		"""Lock's sha256 differs from the sha of the on-disk `.dmp` at
		the pinned version → build fails with a `drift prepare` pointer.

		Pins the reproducibility re-check: a rebuild or replacement of
		the package bytes invalidates the lock."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		# Lock records the deterministic helper sha.
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		locked_sha = hashlib.sha256(b"dep-a@0.1.3").hexdigest()
		ondisk_sha = hashlib.sha256(b"rebuilt-different-bytes").hexdigest()
		assert locked_sha != ondisk_sha

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()

		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {
				"dep-a": [PackageEntry(
					package_id="dep-a", version=parse_version("0.1.3"),
					path=pkg_root / "dep-a-0.1.3.dmp", sha256=ondisk_sha,
					required_deps=[], author_key="ed25519:test",
				)],
			}
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 1
		err = capsys.readouterr().err
		assert "dep-a" in err
		assert "sha256 mismatch" in err or "sha256" in err
		assert "drift prepare" in err, (
			f"sha-mismatch diagnostic must point at drift prepare; got:\n{err}"
		)

	def test_source_rebuild_accepts_sha_drift_when_source_identity_matches(self, tmp_path, capsys):
		"""Phase D: `drift build --source-rebuild` accepts a rebuilt
		`.dmp` with different bytes from the lock as long as the
		source-attestation half (`source_content_id` +
		`source_attestation_key`) re-verifies.  Per-package sha drift
		reported to stdout as run evidence."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		locked_sha = hashlib.sha256(b"dep-a@0.1.3").hexdigest()
		# orchestrator-rebuilt bytes — different sha but same source.
		rebuilt_sha = hashlib.sha256(b"rebuilt-by-orch").hexdigest()
		assert locked_sha != rebuilt_sha
		# `_write_lock` derives source_content_id deterministically;
		# replicate it exactly so the disk side matches the lock.
		matching_scid = _fake_scid("dep-a", "0.1.3")

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("subprocess.run") as mock_run, \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {
				"dep-a": [PackageEntry(
					package_id="dep-a", version=parse_version("0.1.3"),
					path=pkg_root / "dep-a-0.1.3.dmp",
					sha256=rebuilt_sha,  # ← drift!
					required_deps=[],
					author_key="ed25519:rebuilder-not-author",  # tolerated in source-rebuild
					source_content_id=matching_scid,
					source_attestation_key="ed25519:test",  # matches lock
				)],
			}
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
				"--source-rebuild",
			])

		assert result == 0, capsys.readouterr().err
		out = capsys.readouterr().out
		# Run-evidence reporting: per-package locked → rebuilt sha pair
		# must surface to stdout so the operator sees what diverged.
		# 0.31.1 unified format: `drift vs. lock` + `sha256 'X' -> 'Y'`.
		assert "source-rebuild" in out
		assert "drift vs. lock" in out
		assert "sha256" in out
		assert "dep-a" in out
		assert locked_sha in out
		assert rebuilt_sha in out

	def test_source_rebuild_accepts_source_identity_drift_as_evidence(self, tmp_path, capsys):
		"""Policy as of `fix/source-rebuild-trust-anchor` (0.31.1):
		source_content_id drift is EVIDENCE in source-rebuild mode,
		not a hard failure.  Orch selects source via run-all-latest.
		json; the downstream lock records what the repo author last
		prepared against.  A compatible upstream patch legitimately
		shifts the dep's scid without the downstream having touched
		its own lock.  Trust comes from the namespace-allowlist check
		at package-index time (v1 `provider_v1` / `verify_v1.
		compose_verify`), not per-dep scid equality with the lock.
		See `doc/history.md` 2026-04-21 for the full rationale."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		rebuilt_sha = hashlib.sha256(b"rebuilt").hexdigest()

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx, \
			 mock.patch("tools.drift_deploy.drift_build.subprocess.run") as mock_sub:
			mock_sub.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			mock_idx.return_value = {
				"dep-a": [PackageEntry(
					package_id="dep-a", version=parse_version("0.1.3"),
					path=pkg_root / "dep-a-0.1.3.dmp",
					sha256=rebuilt_sha,
					required_deps=[],
					author_key="ed25519:rebuilder",
					source_content_id="sha256:" + "9"*64,  # ← drifted
					source_attestation_key="ed25519:test",
				)],
			}
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
				"--source-rebuild",
			])

		# Source identity drift no longer fails the build.  The sha-
		# drift evidence line is still printed; we don't assert on a
		# per-field scid evidence line here because the build-time
		# caller plumbs only sha_drift_log (signer evidence is an
		# optional follow-up — `drift prepare --check` already surfaces
		# version/source-identity drift on the certification path).
		assert result == 0

	def test_source_rebuild_rejects_missing_source_attestation(self, tmp_path, capsys):
		"""Phase D: source-rebuild mode hard-fails when on-disk has no
		valid source attestation.  No silent fallback to byte-only
		verification — that would defeat the whole trust boundary."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		rebuilt_sha = hashlib.sha256(b"rebuilt").hexdigest()

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {
				"dep-a": [PackageEntry(
					package_id="dep-a", version=parse_version("0.1.3"),
					path=pkg_root / "dep-a-0.1.3.dmp",
					sha256=rebuilt_sha,
					required_deps=[],
					author_key="ed25519:rebuilder",
					source_content_id="",  # missing sidecar on disk
					source_attestation_key="",
				)],
			}
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
				"--source-rebuild",
			])

		assert result == 1
		err = capsys.readouterr().err
		assert "source-rebuild requires" in err
		# v1 wording: the diagnostic now names the v1 sidecar emitters.
		assert "drift author" in err or "drift-deploy" in err

	def test_default_strict_still_rejects_sha_drift_without_flag(self, tmp_path, capsys):
		"""Phase D regression: WITHOUT `--source-rebuild`, the default
		strict mode still rejects sha drift even when source identity
		matches.  Source-mode is opt-in; it must not become the silent
		default for regular package consumers."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		matching_scid = _fake_scid("dep-a", "0.1.3")
		drift_sha = hashlib.sha256(b"different-bytes").hexdigest()

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {
				"dep-a": [PackageEntry(
					package_id="dep-a", version=parse_version("0.1.3"),
					path=pkg_root / "dep-a-0.1.3.dmp",
					sha256=drift_sha,
					required_deps=[],
					author_key="ed25519:test",
					source_content_id=matching_scid,
					source_attestation_key="ed25519:test",
				)],
			}
			# NOTE: no --source-rebuild flag.
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 1
		err = capsys.readouterr().err
		assert "sha256 mismatch" in err

	def test_source_rebuild_via_cert_mode_certify(self, tmp_path, capsys, monkeypatch):
		"""0.31.5: `DRIFT_CERT_MODE=certify` (paired with the snapshot
		the permissive fixture sets) enables source-rebuild
		verification without the CLI flag.  This is the orch
		certification-verification shape: after staging emits the
		snapshot, orch exports `DRIFT_CERT_MODE=certify` plus
		`DRIFT_RUN_SNAPSHOT=<path>`, and repo-owned `just test` →
		`drift build` picks up the lane automatically."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		rebuilt_sha = hashlib.sha256(b"rebuilt-by-orch").hexdigest()
		matching_scid = _fake_scid("dep-a", "0.1.3")

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("subprocess.run") as mock_run, \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {
				"dep-a": [PackageEntry(
					package_id="dep-a", version=parse_version("0.1.3"),
					path=pkg_root / "dep-a-0.1.3.dmp",
					sha256=rebuilt_sha,
					required_deps=[],
					author_key="ed25519:rebuilder",
					source_content_id=matching_scid,
					source_attestation_key="ed25519:test",
				)],
			}
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			# NOTE: no --source-rebuild on the CLI; env var alone.
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 0, capsys.readouterr().err
		out = capsys.readouterr().out
		# Run-evidence emission proves source-rebuild lane was active.
		# 0.31.1 unified format: `drift vs. lock` + `sha256 'X' -> 'Y'`.
		assert "source-rebuild" in out
		assert "drift vs. lock" in out
		assert "sha256" in out
		assert "dep-a" in out

	def test_cert_mode_unset_means_default_strict_mode(self, tmp_path, capsys, monkeypatch):
		"""Pin: `DRIFT_CERT_MODE` unset leaves default strict mode in
		force (plain local `drift build` behaviour — must not silently
		drift into source-rebuild for general consumers).

		Under the refined 0.31.5 contract, `stage` ENGAGES source-
		rebuild (see
		`test_stage_mode_engages_source_rebuild_with_exemption` on
		the deploy side).  Only `unset` means strict."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		matching_scid = _fake_scid("dep-a", "0.1.3")
		drift_sha = hashlib.sha256(b"different-bytes").hexdigest()

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		# Explicitly clear both env vars in case ambient set.
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		monkeypatch.delenv("DRIFT_SOURCE_REBUILD", raising=False)
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {
				"dep-a": [PackageEntry(
					package_id="dep-a", version=parse_version("0.1.3"),
					path=pkg_root / "dep-a-0.1.3.dmp",
					sha256=drift_sha,
					required_deps=[],
					author_key="ed25519:test",
					source_content_id=matching_scid,
					source_attestation_key="ed25519:test",
				)],
			}
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 1
		err = capsys.readouterr().err
		assert "sha256 mismatch" in err

	def test_source_rebuild_without_run_snapshot_hard_fails(self, tmp_path, capsys, monkeypatch):
		"""CLI-path regression: explicit `drift build --source-rebuild`
		WITHOUT either `--run-snapshot` or `DRIFT_RUN_SNAPSHOT` must
		fail cleanly — no silent fallback to downstream trust-store
		verification, no cryptic traceback.  Triggered via the CLI
		flag now (0.31.5: ambient env-only triggers are CertModeError
		for DRIFT_SOURCE_REBUILD and no-op for DRIFT_CERT_MODE=stage)."""
		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		monkeypatch.delenv("DRIFT_SOURCE_REBUILD", raising=False)
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--source-rebuild",
			])
		assert result == 1
		err = capsys.readouterr().err
		assert "run snapshot" in err
		assert "DRIFT_RUN_SNAPSHOT" in err or "--run-snapshot" in err

	def test_cert_mode_certify_without_snapshot_hard_fails(self, tmp_path, capsys, monkeypatch):
		"""Regression #4: `DRIFT_CERT_MODE=certify` with no snapshot
		(neither `DRIFT_RUN_SNAPSHOT` nor `--run-snapshot`) fails
		cleanly with snapshot-required."""
		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
			])
		assert result == 1
		err = capsys.readouterr().err
		assert "run snapshot" in err
		assert "DRIFT_RUN_SNAPSHOT" in err or "--run-snapshot" in err

	def test_invalid_cert_mode_fails_clearly(self, tmp_path, capsys, monkeypatch):
		"""Regression #5: an invalid `DRIFT_CERT_MODE` value surfaces
		as a clean error with the allowed values listed.  Protects
		against typos like `staging` / `verif` / `VERIFY`."""
		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		monkeypatch.setenv("DRIFT_CERT_MODE", "verif")  # typo
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
			])
		assert result == 1
		err = capsys.readouterr().err
		assert "DRIFT_CERT_MODE" in err
		assert "'verif'" in err
		assert "stage" in err and "certify" in err

	def test_retired_env_var_fails_clearly(self, tmp_path, capsys, monkeypatch):
		"""`DRIFT_SOURCE_REBUILD=1` hard-errors on the build CLI path
		with a migration message pointing at `DRIFT_CERT_MODE`."""
		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}})
		monkeypatch.setenv("DRIFT_SOURCE_REBUILD", "1")
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"):
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
			])
		assert result == 1
		err = capsys.readouterr().err
		assert "DRIFT_SOURCE_REBUILD" in err
		assert "DRIFT_CERT_MODE" in err

	def test_source_rebuild_helper_matrix(self, monkeypatch) -> None:
		"""Unit pin for the uniform selector (build side), refined
		0.31.5 contract:
		  - unset cert_mode, no flag → False (normal local)
		  - `stage`, no flag → True (source-rebuild + exemption)
		  - `certify`, no flag → True (source-rebuild, no exemption)
		  - any cert_mode, --source-rebuild flag → True
		  - invalid cert_mode → CertModeError
		  - DRIFT_SOURCE_REBUILD set → CertModeError"""
		import argparse as _ap
		from tools.drift_deploy.build_cmd import (
			CertModeError,
			producer_output_exemption_active,
		)
		from tools.drift_deploy.drift_build import _source_rebuild_enabled
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

	def test_build_rejects_author_key_mismatch(self, tmp_path, capsys):
		"""Lock's author_key differs from the on-disk signer at the
		pinned version → build fails with a `drift prepare` pointer.

		Pins the signer re-check: a rotated or compromised signing
		key forces explicit re-preparation; builds never silently
		accept a different signer."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "0.1"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		_write_lock(tmp_path, {"my-pkg": {"dep-a": "0.1.3"}},
			author_key="ed25519:OLD_KEY")
		locked_sha = hashlib.sha256(b"dep-a@0.1.3").hexdigest()

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()

		from tools.drift_deploy.drift_build import run
		with mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			# Same bytes (sha matches), DIFFERENT signer.
			mock_idx.return_value = {
				"dep-a": [PackageEntry(
					package_id="dep-a", version=parse_version("0.1.3"),
					path=pkg_root / "dep-a-0.1.3.dmp", sha256=locked_sha,
					required_deps=[], author_key="ed25519:NEW_KEY",
				)],
			}
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 1
		err = capsys.readouterr().err
		assert "dep-a" in err
		# Signer-mismatch wording from verify_lock_compatibility.
		assert "signing key changed" in err or "key" in err.lower()
		assert "drift prepare" in err, (
			f"author-key mismatch diagnostic must point at drift prepare; "
			f"got:\n{err}"
		)

	def test_build_full_transitive_graph_reaches_driftc(self, tmp_path):
		"""Direct + every transitive in the lock reaches driftc as an
		exact `--dep PKG@M.N.P` flag.  Pins the "no resolution at build
		time" contract from the consumer side: driftc sees a complete,
		self-consistent exact graph and never has to expand anything."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib

		manifest_data = {
			"schema_version": 2,
			"project": {"name": "test-project", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "my-pkg", "version": "0.1.0",
				"description": "A test package",
				"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
				"package_deps": [{"name": "dep-a", "version": "1.0"}],
			}],
		}
		_write_manifest(tmp_path, manifest_data)
		(tmp_path / "src").mkdir(parents=True, exist_ok=True)
		(tmp_path / "src" / "lib.drift").write_text("module my.pkg;\n")

		# Lock: one direct + two transitives, all exact.
		direct_sha = hashlib.sha256(b"dep-a@1.2.7").hexdigest()
		t1_sha = hashlib.sha256(b"dep-b@0.5.3").hexdigest()
		t2_sha = hashlib.sha256(b"dep-c@2.0.1").hexdigest()
		direct_scid = "sha256:" + hashlib.sha256(b"src:dep-a@1.2.7").hexdigest()
		t1_scid = "sha256:" + hashlib.sha256(b"src:dep-b@0.5.3").hexdigest()
		t2_scid = "sha256:" + hashlib.sha256(b"src:dep-c@2.0.1").hexdigest()
		lock_obj = {
			"schema_version": 4,
			"artifacts": {
				"my-pkg": {
					"resolved": {
						"dep-a": {"version": "1.2.7", "sha256": direct_sha,
							"author_key": "ed25519:test",
							"source_content_id": direct_scid,
							"source_attestation_key": "ed25519:test",
							"dep_type": "direct"},
						"dep-b": {"version": "0.5.3", "sha256": t1_sha,
							"author_key": "ed25519:test",
							"source_content_id": t1_scid,
							"source_attestation_key": "ed25519:test",
							"dep_type": "transitive"},
						"dep-c": {"version": "2.0.1", "sha256": t2_sha,
							"author_key": "ed25519:test",
							"source_content_id": t2_scid,
							"source_attestation_key": "ed25519:test",
							"dep_type": "transitive"},
					},
				},
			},
		}
		(tmp_path / "drift" / "lock.json").write_text(
			json.dumps(lock_obj, indent=2), encoding="utf-8",
		)

		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()

		from tools.drift_deploy.drift_build import run
		with mock.patch("subprocess.run") as mock_run, \
			 mock.patch("shutil.which", return_value="/usr/bin/driftc"), \
			 mock.patch("tools.drift_deploy.drift_build.build_package_index") as mock_idx:
			mock_idx.return_value = {
				"dep-a": [PackageEntry(package_id="dep-a",
					version=parse_version("1.2.7"),
					path=pkg_root / "dep-a-1.2.7.dmp", sha256=direct_sha,
					required_deps=[], author_key="ed25519:test",
					source_content_id=direct_scid,
					source_attestation_key="ed25519:test")],
				"dep-b": [PackageEntry(package_id="dep-b",
					version=parse_version("0.5.3"),
					path=pkg_root / "dep-b-0.5.3.dmp", sha256=t1_sha,
					required_deps=[], author_key="ed25519:test",
					source_content_id=t1_scid,
					source_attestation_key="ed25519:test")],
				"dep-c": [PackageEntry(package_id="dep-c",
					version=parse_version("2.0.1"),
					path=pkg_root / "dep-c-2.0.1.dmp", sha256=t2_sha,
					required_deps=[], author_key="ed25519:test",
					source_content_id=t2_scid,
					source_attestation_key="ed25519:test")],
			}
			mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
			result = run([
				"--manifest", str(tmp_path / "drift" / "manifest.json"),
				"--package-root", str(pkg_root),
			])

		assert result == 0
		cmd_str = " ".join(mock_run.call_args[0][0])
		# Every locked package, direct and transitive, forwarded as
		# an exact M.N.P pin.
		assert "dep-a@1.2.7" in cmd_str
		assert "dep-b@0.5.3" in cmd_str
		assert "dep-c@2.0.1" in cmd_str


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

	def test_cli_manifest_migrate_dispatched(self, tmp_path: Path) -> None:
		"""`drift manifest migrate` routes through the top-level CLI
		to the migrator.  Pins the new subcommand wiring."""
		from lang.drift.cli import main as cli_main
		drift_dir = tmp_path / "drift"
		drift_dir.mkdir()
		path = drift_dir / "manifest.json"
		path.write_text(json.dumps({
			"schema_version": 1,
			"project": {"name": "p", "license": "MIT"},
			"artifacts": [{
				"kind": "package", "name": "p", "version": "1.0.0",
				"description": "p", "entry_module": "p.drift",
				"modules": ["p/"],
				"package_deps": [{"name": "dep-a", "version": "0.3.14"}],
			}],
		}), encoding="utf-8")
		rc = cli_main(["manifest", "migrate", "--manifest", str(path)])
		assert rc == 0
		data = json.loads(path.read_text())
		assert data["schema_version"] == 2
		assert data["artifacts"][0]["package_deps"][0]["version"] == "0.3"

	def test_cli_manifest_help(self, capsys) -> None:
		"""`drift manifest --help` lists the `migrate` subcommand."""
		from lang.drift.cli import main as cli_main
		rc = cli_main(["manifest", "--help"])
		assert rc == 0
		out = capsys.readouterr().out
		assert "migrate" in out

	def test_cli_manifest_unknown_subcommand_errors(self, capsys) -> None:
		"""`drift manifest bogus` is a hard error naming the valid
		subcommands, not a silent success."""
		from lang.drift.cli import main as cli_main
		rc = cli_main(["manifest", "bogus"])
		assert rc == 1
		err = capsys.readouterr().err
		assert "bogus" in err
		assert "migrate" in err


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
		args = p.parse_args(["--package-root=~/lib"])
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
		args = p.parse_args(["--dest=~/opt/drift/lib"])
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
		args = p.parse_args(["--dest", "~/opt/drift/lib"])
		assert "~" not in str(args.dest)


class TestTrustStoreResolution:
	"""Pin the exists-before-injecting contract for `_resolve_trust_store`.

	Triggered by the cert-host net-tls staging failure: `drift deploy`
	had been silently forwarding `--trust-store <missing-path>` to the
	driftc subprocess, causing every package build to fail on a clean
	host (no `~/.config/drift/trust.json`).  Contract per cert-team
	spec:

	  - explicit `--trust-store <missing>` -> DeployError (loud).
	  - `$DRIFT_TRUST_STORE` set, path missing -> DeployError (loud).
	  - both unset -> return None; driftc itself merges user trust
	    from `~/.config/drift/trust.json` (gated on exists inside
	    `lang/driftc/driftc.py`).
	"""

	def _parse(self, *argv: str):
		from tools.drift_deploy.drift_deploy import build_arg_parser
		return build_arg_parser().parse_args(list(argv))

	def test_explicit_missing_path_raises(self, tmp_path: Path, monkeypatch):
		"""--trust-store <missing> -> DeployError, NOT silent forward."""
		from tools.drift_deploy.drift_deploy import _resolve_trust_store, DeployError
		monkeypatch.delenv("DRIFT_TRUST_STORE", raising=False)
		missing = tmp_path / "trust.json"
		assert not missing.exists()
		args = self._parse(f"--trust-store={missing}")
		with pytest.raises(DeployError, match="does not exist"):
			_resolve_trust_store(args)

	def test_explicit_existing_path_returned(self, tmp_path: Path, monkeypatch):
		"""--trust-store <existing> -> path returned untouched."""
		from tools.drift_deploy.drift_deploy import _resolve_trust_store
		monkeypatch.delenv("DRIFT_TRUST_STORE", raising=False)
		trust_path = tmp_path / "trust.json"
		trust_path.write_text('{"format": "drift-trust", "version": 1}', encoding="utf-8")
		args = self._parse(f"--trust-store={trust_path}")
		assert _resolve_trust_store(args) == trust_path

	def test_env_missing_path_raises(self, tmp_path: Path, monkeypatch):
		"""$DRIFT_TRUST_STORE <missing> -> DeployError; env was explicit intent."""
		from tools.drift_deploy.drift_deploy import _resolve_trust_store, DeployError
		missing = tmp_path / "no_such.json"
		assert not missing.exists()
		monkeypatch.setenv("DRIFT_TRUST_STORE", str(missing))
		args = self._parse()
		with pytest.raises(DeployError, match=r"DRIFT_TRUST_STORE.*does not exist"):
			_resolve_trust_store(args)

	def test_env_existing_path_returned(self, tmp_path: Path, monkeypatch):
		"""$DRIFT_TRUST_STORE <existing> -> path returned."""
		from tools.drift_deploy.drift_deploy import _resolve_trust_store
		trust_path = tmp_path / "trust.json"
		trust_path.write_text('{"format": "drift-trust", "version": 1}', encoding="utf-8")
		monkeypatch.setenv("DRIFT_TRUST_STORE", str(trust_path))
		args = self._parse()
		assert _resolve_trust_store(args) == trust_path

	def test_no_explicit_no_env_returns_none(self, monkeypatch):
		"""Neither flag nor env -> None; do NOT default to ~/.config/drift/trust.json."""
		from tools.drift_deploy.drift_deploy import _resolve_trust_store
		monkeypatch.delenv("DRIFT_TRUST_STORE", raising=False)
		args = self._parse()
		assert _resolve_trust_store(args) is None

	def test_clean_host_no_trust_store_flag_in_driftc_cmd(
		self, tmp_path: Path, monkeypatch,
	):
		"""End-to-end regression: clean HOME + no env -> no `--trust-store`
		token in the driftc cmd assembled by `build_package_cmd`.

		Simulates a no-dep library deploy on a fresh cert host (the
		net-tls staging scenario): HOME points at an empty tmp dir
		(no `~/.config/drift/trust.json`) and `DRIFT_TRUST_STORE` is
		unset.  `_resolve_trust_store` must return `None`, and the
		driftc cmd must NOT carry `--trust-store`.
		"""
		from tools.drift_deploy.drift_deploy import _resolve_trust_store
		from tools.drift_deploy.build_cmd import build_package_cmd
		clean_home = tmp_path / "home"
		clean_home.mkdir()
		monkeypatch.setenv("HOME", str(clean_home))
		monkeypatch.delenv("DRIFT_TRUST_STORE", raising=False)
		# Sanity: clean host really is clean.
		assert not (clean_home / ".config" / "drift" / "trust.json").exists()
		args = self._parse()
		resolved = _resolve_trust_store(args)
		assert resolved is None, (
			f"clean host with no flag + no env must yield None; "
			f"got {resolved!r}"
		)
		art = _make_artifact()
		cmd = build_package_cmd(
			art,
			driftc=Path("/usr/bin/driftc"),
			target="drift-dev",
			resolved_deps={},
			output_path=tmp_path / "out.dmp",
			manifest_dir=tmp_path,
			package_roots=[],
			trust_store=resolved,
		)
		assert "--trust-store" not in cmd, (
			f"clean host: driftc cmd must not include --trust-store; got: {cmd}"
		)


class TestAttachAuthorClaimLookupPath:
	"""Regression: `_attach_author_claim_to_artifact` used to join
	`manifest_dir` with an extra `"drift"` segment.  Under the
	canonical layout (`<repo>/drift/manifest.json`) `manifest_dir` is
	already `<repo>/drift`, so the bogus join produced
	`<repo>/drift/drift/<pkg>.author-claim` and rejected every
	correctly-placed claim.  Cert team flagged this on the net-tls
	staging run; the diagnostic said
	`drift/drift/net-tls.author-claim`.

	Pin:
	  - lookup finds `<manifest_dir>/<pkg>.author-claim`;
	  - lookup does NOT find a claim placed under the bogus
	    `<manifest_dir>/drift/<pkg>.author-claim` (no v0 fallback);
	  - missing-file diagnostic names the correct path (no double
	    `drift/drift/`).
	"""

	def _sign_claim_at(
		self, target_path: Path, *,
		package_id: str, version: str, sci: str,
	) -> None:
		"""Sign an author claim and write it directly at `target_path`
		(not through `sidecar_dir`-based helpers — the test owns
		layout choice)."""
		import base64
		from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
		from cryptography.hazmat.primitives import serialization
		from lang.driftc.packages.author_claim_v1 import (
	make_author_claim_body,
			AuthorClaimBody, dump_author_claim_json, make_author_claim,
		)
		priv = Ed25519PrivateKey.generate()
		seed = priv.private_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PrivateFormat.Raw,
			encryption_algorithm=serialization.NoEncryption(),
		)
		body = make_author_claim_body(
			artifact_kind="package", package_id=package_id, version=version,
			namespaces=(package_id.replace("-", "_") + ".*",),
			source_content_id=sci, required_deps=(),
			release_utc="2026-05-20T21:00:00Z",
		)
		claim = make_author_claim(body, seed)
		target_path.write_text(dump_author_claim_json(claim), encoding="utf-8")

	def _layout(self, tmp_path: Path) -> Path:
		"""Build the canonical `<repo>/drift/manifest.json` layout and
		return `manifest_dir = <repo>/drift/`."""
		manifest_dir = tmp_path / "myrepo" / "drift"
		manifest_dir.mkdir(parents=True)
		(manifest_dir / "manifest.json").write_text("{}", encoding="utf-8")
		return manifest_dir

	def test_lookup_finds_claim_next_to_manifest(self, tmp_path: Path):
		"""Canonical placement: `<manifest_dir>/<pkg>.author-claim`."""
		from tools.drift_deploy.drift_deploy import _attach_author_claim_to_artifact
		from lang.driftc.packages.sidecar_naming import author_claim_filename
		manifest_dir = self._layout(tmp_path)
		staged_install = tmp_path / "stage"
		sci = "sha256:" + "a" * 64
		claim_path = manifest_dir / author_claim_filename("net-tls")
		self._sign_claim_at(
			claim_path, package_id="net-tls", version="0.5.0", sci=sci,
		)
		dst = _attach_author_claim_to_artifact(
			package_id="net-tls",
			package_version="0.5.0",
			source_content_id=sci,
			manifest_dir=manifest_dir,
			staged_install=staged_install,
		)
		assert dst == staged_install / author_claim_filename("net-tls")
		assert dst.is_file()

	def test_lookup_ignores_legacy_double_drift_location(self, tmp_path: Path):
		"""No fallback to `<manifest_dir>/drift/<pkg>.author-claim`.

		A claim placed at the bogus nested-`drift/` path (the location
		the buggy code used to probe) must NOT satisfy the lookup --
		otherwise we'd be honoring a layout the v1 contract never
		blessed and masking misconfigured client repos.
		"""
		from tools.drift_deploy.drift_deploy import _attach_author_claim_to_artifact, DeployError
		from lang.driftc.packages.sidecar_naming import author_claim_filename
		manifest_dir = self._layout(tmp_path)
		bogus_dir = manifest_dir / "drift"
		bogus_dir.mkdir()
		sci = "sha256:" + "b" * 64
		self._sign_claim_at(
			bogus_dir / author_claim_filename("net-tls"),
			package_id="net-tls", version="0.5.0", sci=sci,
		)
		with pytest.raises(DeployError, match="pre-signed author claim not found"):
			_attach_author_claim_to_artifact(
				package_id="net-tls",
				package_version="0.5.0",
				source_content_id=sci,
				manifest_dir=manifest_dir,
				staged_install=tmp_path / "stage",
			)

	def test_missing_diagnostic_names_correct_path(self, tmp_path: Path):
		"""Diagnostic must point at `<manifest_dir>/<pkg>.author-claim`,
		NOT `<manifest_dir>/drift/<pkg>.author-claim`.
		"""
		from tools.drift_deploy.drift_deploy import _attach_author_claim_to_artifact, DeployError
		from lang.driftc.packages.sidecar_naming import author_claim_filename
		manifest_dir = self._layout(tmp_path)
		expected_path = manifest_dir / author_claim_filename("net-tls")
		with pytest.raises(DeployError) as excinfo:
			_attach_author_claim_to_artifact(
				package_id="net-tls",
				package_version="0.5.0",
				source_content_id="sha256:" + "c" * 64,
				manifest_dir=manifest_dir,
				staged_install=tmp_path / "stage",
			)
		msg = str(excinfo.value)
		assert str(expected_path) in msg, (
			f"diagnostic must name the correct expected path "
			f"{expected_path}; got: {msg}"
		)
		# The historical double-drift/ shape must NOT appear in the
		# diagnostic.  Catching this as a substring is exact: any
		# `<...>/drift/drift/...` path printed here would mean the
		# bug regressed.
		assert "/drift/drift/" not in msg, (
			f"diagnostic must not reference the bogus double-drift/ "
			f"path; got: {msg}"
		)


class TestTildeExpansionPrepare:
	def test_prepare_dest_tilde(self):
		from tools.drift_deploy.drift_prepare import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--dest=~/opt/drift/lib"])
		assert "~" not in str(args.dest)

	def test_prepare_package_root_tilde(self):
		from tools.drift_deploy.drift_prepare import build_arg_parser
		p = build_arg_parser()
		args = p.parse_args(["--package-root=~/lib"])
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
pub fn main() nothrow -> Int {
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
		"schema_version": 2,
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
				package_deps=[{"name": "test-dep", "version": "0.1"}],
			),
		])
		_write_e2e_manifest(consumer_dir, consumer_manifest)

		# Write v4 lockfile with exact resolved version + sha256 + source identity.
		dep_sha = hashlib.sha256(dep_dmp.read_bytes()).hexdigest()
		dep_scid = "sha256:" + hashlib.sha256(b"src:test-dep@0.1.0").hexdigest()
		_write_e2e_lock(consumer_dir, {
			"schema_version": 4,
			"artifacts": {
				"test-consumer": {
					"resolved": {
						"test-dep": {
							"version": "0.1.0",
							"sha256": dep_sha,
							"author_key": "unsigned",
							"source_content_id": dep_scid,
							"source_attestation_key": "ed25519:test",
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

		# Verify package metadata: test-dep appears as a direct dep
		# with the manifest's declared range (not the lock's exact
		# pin).  Published .dmp metadata exports the producer's
		# authored constraint so downstream consumers can pick up
		# patch bumps without the producer republishing.
		from lang.driftc.packages.dmir_pkg_v0 import load_dmir_pkg_v0
		pkg = load_dmir_pkg_v0(consumer_dmp)
		dep_names = [d.name for d in pkg.required_deps]
		assert "test-dep" in dep_names
		dep_entry = next(d for d in pkg.required_deps if d.name == "test-dep")
		# Manifest declared "0.1"; that's what's exported.
		assert dep_entry.version == "0.1"

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
				package_deps=[{"name": "test-leaf", "version": "0.1"}],
			)]),
		)
		# test-dep needs a lockfile for its dep on test-leaf.
		leaf_sha = hashlib.sha256(leaf_dmp.read_bytes()).hexdigest()
		leaf_scid = "sha256:" + hashlib.sha256(b"src:test-leaf@0.1.0").hexdigest()
		_write_e2e_lock(dep_dir, {
			"schema_version": 4,
			"artifacts": {"test-dep": {"resolved": {
				"test-leaf": {"version": "0.1.0", "sha256": leaf_sha, "author_key": "unsigned", "source_content_id": leaf_scid, "source_attestation_key": "ed25519:test", "dep_type": "direct"},
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
				package_deps=[{"name": "test-dep", "version": "0.1"}],
			)]),
		)
		# Lockfile: test-dep is direct, test-leaf is transitive.
		dep_scid = "sha256:" + hashlib.sha256(b"src:test-dep@0.1.0").hexdigest()
		_write_e2e_lock(consumer_dir, {
			"schema_version": 4,
			"artifacts": {"test-consumer": {"resolved": {
				"test-dep": {"version": "0.1.0", "sha256": dep_sha, "author_key": "unsigned", "source_content_id": dep_scid, "source_attestation_key": "ed25519:test", "dep_type": "direct"},
				"test-leaf": {"version": "0.1.0", "sha256": leaf_sha, "author_key": "unsigned", "source_content_id": leaf_scid, "source_attestation_key": "ed25519:test", "dep_type": "transitive"},
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
		declared_dep_names = sorted(d.name for d in pkg.required_deps)
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
				package_deps=[{"name": "some-dep", "version": "1.0"}],
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


# ── drift-build-info/v1 identity stamp flags (W3, 0.33.93) ───────────


class TestArtifactStampFlags:
	"""Both cmd builders pass the four --artifact-* identity flags,
	atomically, from the manifest artifact (loader guarantees non-empty
	values)."""

	def _flag_pairs(self, cmd: list[str]) -> dict[str, str]:
		return {cmd[i]: cmd[i + 1] for i in range(len(cmd) - 1)
		        if cmd[i].startswith("--artifact-")}

	def test_package_cmd_carries_identity_stamp(self):
		art = _make_artifact()
		cmd = build_package_cmd(
			art, driftc=Path("/usr/bin/driftc"), target="drift-dev",
			resolved_deps={}, output_path=Path("/b/p.dmp"),
			manifest_dir=Path("/proj"), package_roots=[],
		)
		assert self._flag_pairs(cmd) == {
			"--artifact-name": art.name,
			"--artifact-version": art.version,
			"--artifact-description": art.description,
			"--artifact-license": art.license,
		}

	def test_app_cmd_carries_identity_stamp(self):
		art = _make_artifact(kind="app")
		cmd = build_app_cmd(
			art, driftc=Path("/usr/bin/driftc"), target="native",
			resolved_deps={}, output_path=Path("/b/app"),
			manifest_dir=Path("/proj"), package_roots=[],
		)
		assert self._flag_pairs(cmd) == {
			"--artifact-name": art.name,
			"--artifact-version": art.version,
			"--artifact-description": art.description,
			"--artifact-license": art.license,
		}
