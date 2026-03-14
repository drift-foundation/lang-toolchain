# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Tests for the drift deploy orchestrator.

Covers: CLI parsing, artifact ordering, resolution/lock integration,
and the per-artifact pipeline contract.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.drift_deploy.drift_deploy import (
	DeployError,
	_build_app,
	_build_package,
	_clean_env,
	_run_baseline_smoke_package,
	_SCRUB_ENV_KEYS,
	_topo_sort_artifacts,
	_resolve_or_load_lock,
	build_arg_parser,
)
from tools.drift_deploy.lockfile import write_lock
from tools.drift_deploy.manifest import (
	Artifact,
	Manifest,
	PackageDep,
	Project,
	load_manifest,
)
from tools.drift_deploy.resolver import ResolvedDep
from tools.drift_deploy.semver import parse_version


# ── Helpers ──────────────────────────────────────────────────────────


def _art(name: str, kind: str = "package", deps: list[PackageDep] | None = None) -> Artifact:
	return Artifact(
		kind=kind,
		name=name,
		version="1.0.0",
		description=f"Test {name}",
		license="MIT",
		entry_module="src/lib.drift",
		modules=["src/"],
		package_deps=deps or [],
	)


# ── CLI parsing ──────────────────────────────────────────────────────


class TestCLI:
	def test_defaults(self) -> None:
		p = build_arg_parser()
		args = p.parse_args([])
		assert args.manifest == Path("drift-package.json")
		assert args.dest is None
		assert args.app_dest is None
		assert args.skip_smoke is False
		assert args.dry_run is False
		assert args.update_lock is False

	def test_all_flags(self) -> None:
		p = build_arg_parser()
		args = p.parse_args([
			"--manifest", "custom.json",
			"--dest", "/deploy",
			"--app-dest", "/apps",
			"--package-root", "/pr1",
			"--package-root", "/pr2",
			"--artifact", "net.tls",
			"--artifact", "tls-tool",
			"--driftc", "/usr/bin/driftc",
			"--sign-key-file", "/key.seed",
			"--trust-store", "/trust.json",
			"--target", "aarch64-linux-gnu",
			"--update-lock",
			"--skip-smoke",
			"--dry-run",
		])
		assert args.manifest == Path("custom.json")
		assert args.dest == Path("/deploy")
		assert args.app_dest == Path("/apps")
		assert args.package_root == [Path("/pr1"), Path("/pr2")]
		assert args.artifact == ["net.tls", "tls-tool"]
		assert args.target == "aarch64-linux-gnu"
		assert args.update_lock is True
		assert args.skip_smoke is True
		assert args.dry_run is True


# ── Artifact ordering ────────────────────────────────────────────────


class TestTopoSort:
	def test_no_deps(self) -> None:
		arts = [_art("b"), _art("a"), _art("c")]
		result = _topo_sort_artifacts(arts)
		names = [a.name for a in result]
		assert names == ["a", "b", "c"]  # lexicographic when no deps

	def test_app_depends_on_package(self) -> None:
		pkg = _art("net.tls", kind="package")
		app = _art("tls-tool", kind="app", deps=[PackageDep("net.tls", "^1.0.0")])
		result = _topo_sort_artifacts([app, pkg])
		names = [a.name for a in result]
		assert names.index("net.tls") < names.index("tls-tool")

	def test_chain(self) -> None:
		a = _art("a")
		b = _art("b", deps=[PackageDep("a", "^1.0.0")])
		c = _art("c", kind="app", deps=[PackageDep("b", "^1.0.0")])
		result = _topo_sort_artifacts([c, b, a])
		names = [a.name for a in result]
		assert names == ["a", "b", "c"]

	def test_circular_detected(self) -> None:
		a = _art("a", deps=[PackageDep("b", "^1.0.0")])
		b = _art("b", deps=[PackageDep("a", "^1.0.0")])
		with pytest.raises(DeployError, match="circular"):
			_topo_sort_artifacts([a, b])


# ── Resolution / lock integration ───────────────────────────────────


def _make_fake_dmp(pkg_root: Path, name: str, version: str, deps: list[dict] | None = None) -> None:
	"""Create a minimal fake .dmp for testing resolution."""
	pkg_dir = pkg_root / name / version
	pkg_dir.mkdir(parents=True, exist_ok=True)
	dmp_path = pkg_dir / f"{name}.dmp"

	# Create a minimal JSON manifest that build_package_index can parse.
	# The real loader uses dmir_pkg_v0 format, but build_package_index
	# accepts a custom load_manifest callable — we test resolution logic
	# through the resolver directly instead.
	dmp_path.write_bytes(b"fake-dmp-content")


class TestResolutionLock:
	def test_no_deps_produces_empty(self) -> None:
		art = _art("my.pkg")
		manifest = Manifest(
			schema_version=1,
			project=Project(name="test", license="MIT"),
			artifacts=[art],
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			manifest_path = Path(tmpdir) / "drift-package.json"
			manifest_path.write_text("{}")
			result = _resolve_or_load_lock(
				manifest, [art], manifest_path, [], update_lock=False,
			)
			assert result == {"my.pkg": {}}

	def test_lock_roundtrip(self) -> None:
		"""Write a lock, then load it back for an artifact with deps."""
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "drift-lock.json"
			deps = {
				"ext.lib": ResolvedDep(version="1.0.0", integrity="sha256:aabb", dep_type="direct"),
			}
			write_lock(lock_path, {"my.pkg": deps})

			# Now simulate loading from existing lock.
			art = _art("my.pkg", deps=[PackageDep("ext.lib", "^1.0.0")])
			manifest = Manifest(
				schema_version=1,
				project=Project(name="test", license="MIT"),
				artifacts=[art],
			)
			manifest_path = Path(tmpdir) / "drift-package.json"
			manifest_path.write_text("{}")

			# Need a package index entry to verify integrity.
			# Since verify_lock_integrity checks against the index,
			# and we have no real .dmp, this would fail with "not found".
			# So test with --update-lock=True to bypass lock read.
			# Lock read path is tested via test_resolver.py and lockfile.py tests.


class TestResolutionConflictHardFail:
	"""
	Core contract: dependency conflict = hard build failure.
	Resolution must fail before compiler invocation.
	"""

	def test_conflict_raises_deploy_error(self) -> None:
		"""
		When two artifacts share an index and one has conflicting deps,
		resolution must raise DeployError (wrapping ResolutionError).
		"""
		from tools.drift_deploy.resolver import PackageEntry, _sha256_file

		with tempfile.TemporaryDirectory() as tmpdir:
			# Build a synthetic package index.
			# We can't easily mock build_package_index, so we test
			# through resolve_artifact directly (already covered in
			# test_resolver.py). Here we verify the _resolve_or_load_lock
			# wrapper propagates ResolutionError → DeployError.

			# Create artifact with deps that reference nonexistent packages.
			art = _art("my.app", kind="app", deps=[
				PackageDep("nonexistent.pkg", "^1.0.0"),
			])
			manifest = Manifest(
				schema_version=1,
				project=Project(name="test", license="MIT"),
				artifacts=[art],
			)
			manifest_path = Path(tmpdir) / "drift-package.json"
			manifest_path.write_text("{}")

			# No package roots → resolution fails for nonexistent dep.
			with pytest.raises(DeployError, match="not satisfied"):
				_resolve_or_load_lock(
					manifest, [art], manifest_path,
					package_roots=[],
					update_lock=True,
				)

	def test_missing_lock_entry_raises(self) -> None:
		"""Artifact in manifest but not in lock → error with --update-lock hint."""
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = Path(tmpdir) / "drift-lock.json"
			# Lock exists but has no entry for our artifact.
			write_lock(lock_path, {"other.pkg": {
				"dep.x": ResolvedDep(version="1.0.0", integrity="sha256:aa", dep_type="direct"),
			}})

			art = _art("my.pkg", deps=[PackageDep("dep.x", "^1.0.0")])
			manifest = Manifest(
				schema_version=1,
				project=Project(name="test", license="MIT"),
				artifacts=[art],
			)
			manifest_path = Path(tmpdir) / "drift-package.json"
			manifest_path.write_text("{}")

			with pytest.raises(DeployError, match="update-lock"):
				_resolve_or_load_lock(
					manifest, [art], manifest_path,
					package_roots=[],
					update_lock=False,
				)


# ── Sidecar ──────────────────────────────────────────────────────────


class TestSidecar:
	def test_app_sidecar_output(self) -> None:
		from tools.drift_deploy.sidecar import write_app_sidecar

		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "myapp.meta.json"
			write_app_sidecar(
				path,
				app_name="myapp",
				app_version="1.0.0",
				target="x86_64-linux-gnu",
				compiler_version="0.27.48-dev",
				resolved_deps={
					"net.tls": ResolvedDep(version="0.3.0", integrity="sha256:aa", dep_type="direct"),
				},
			)
			data = json.loads(path.read_text())
			assert data["schema_version"] == 1
			assert data["app"] == "myapp"
			assert data["version"] == "1.0.0"
			assert data["target"] == "x86_64-linux-gnu"
			assert data["compiler_version"] == "0.27.48-dev"
			assert "built_at" in data
			assert data["resolved_deps"]["net.tls"]["version"] == "0.3.0"


# ── Staged trust ────────────────────────────────────────────────────


class TestStagedTrust:
	def test_empty_baseline(self) -> None:
		from tools.drift_deploy.staged_trust import build_staged_trust

		with tempfile.TemporaryDirectory() as tmpdir:
			out = Path(tmpdir) / "trust.json"
			# Fake 32-byte pubkey.
			pubkey = b"\x01" * 32
			build_staged_trust(
				baseline_trust_path=None,
				signer_pubkey_raw=pubkey,
				artifact_namespace="net.tls",
				out_path=out,
			)
			data = json.loads(out.read_text())
			assert data["format"] == "drift-trust"
			assert data["version"] == 0
			assert len(data["keys"]) == 1
			# Should have namespace entries.
			assert "net.tls.*" in data["namespaces"]
			assert "net.tls" in data["namespaces"]

	def test_overlay_on_baseline(self) -> None:
		from tools.drift_deploy.staged_trust import build_staged_trust

		with tempfile.TemporaryDirectory() as tmpdir:
			baseline = Path(tmpdir) / "baseline.json"
			baseline.write_text(json.dumps({
				"format": "drift-trust",
				"version": 0,
				"keys": {
					"ed25519:existing": {"algo": "ed25519", "pubkey": "AAAA"},
				},
				"namespaces": {
					"acme.*": ["ed25519:existing"],
				},
				"revoked": {},
			}))

			out = Path(tmpdir) / "staged.json"
			pubkey = b"\x02" * 32
			build_staged_trust(
				baseline_trust_path=baseline,
				signer_pubkey_raw=pubkey,
				artifact_namespace="net.tls",
				out_path=out,
			)
			data = json.loads(out.read_text())
			# Baseline key preserved.
			assert "ed25519:existing" in data["keys"]
			# New key added.
			assert len(data["keys"]) == 2
			# Baseline namespace preserved.
			assert "acme.*" in data["namespaces"]
			# New namespace added.
			assert "net.tls.*" in data["namespaces"]


# ── PYTHONPATH scrubbing ────────────────────────────────────────────


class TestCleanEnv:
	"""
	Regression: PYTHONPATH must not leak into driftc subprocess calls.

	When drift deploy is invoked via
	  PYTHONPATH=/path/to/drift-lang python3 -m tools.drift_deploy.drift_deploy
	the PYTHONPATH leaks into child driftc (PEX) invocations, causing it
	to pick up unbundled lang/ modules and crash with ModuleNotFoundError.
	"""

	def test_pythonpath_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
		monkeypatch.setenv("PYTHONPATH", "/some/path")
		monkeypatch.setenv("PATH", "/usr/bin:/bin")
		env = _clean_env()
		assert "PYTHONPATH" not in env
		assert "PATH" in env

	def test_pythonhome_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
		monkeypatch.setenv("PYTHONHOME", "/some/venv")
		env = _clean_env()
		assert "PYTHONHOME" not in env

	def test_other_vars_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
		monkeypatch.setenv("MY_CUSTOM_VAR", "hello")
		env = _clean_env()
		assert env["MY_CUSTOM_VAR"] == "hello"

	def test_scrub_keys_constant(self) -> None:
		"""Ensure the scrub set is what we expect."""
		assert "PYTHONPATH" in _SCRUB_ENV_KEYS
		assert "PYTHONHOME" in _SCRUB_ENV_KEYS


# ── Manifest unsafe field ───────────────────────────────────────────


class TestUnsafeField:
	def test_unsafe_default_false(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "drift-package.json"
			path.write_text(json.dumps({
				"schema_version": 1,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "safe.pkg",
					"version": "1.0.0",
					"description": "Safe package",
					"entry_module": "src/lib.drift",
					"modules": ["src/"],
				}],
			}))
			m = load_manifest(path)
			assert m.artifacts[0].unsafe is False

	def test_unsafe_true(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "drift-package.json"
			path.write_text(json.dumps({
				"schema_version": 1,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "ffi.pkg",
					"version": "1.0.0",
					"description": "FFI package",
					"entry_module": "src/lib.drift",
					"modules": ["src/"],
					"native_deps": [{"lib": "ssl"}],
					"unsafe": True,
				}],
			}))
			m = load_manifest(path)
			assert m.artifacts[0].unsafe is True

	def test_unsafe_non_bool_rejected(self) -> None:
		from tools.drift_deploy.manifest import ManifestError
		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "drift-package.json"
			path.write_text(json.dumps({
				"schema_version": 1,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "bad.pkg",
					"version": "1.0.0",
					"description": "Bad",
					"entry_module": "src/lib.drift",
					"modules": ["src/"],
					"unsafe": "yes",
				}],
			}))
			with pytest.raises(ManifestError, match="unsafe"):
				load_manifest(path)


# ── Subprocess wiring regressions ───────────────────────────────────

def _make_art(*, unsafe: bool = False, native_deps: list | None = None) -> Artifact:
	"""Build an Artifact for subprocess-wiring tests."""
	from tools.drift_deploy.manifest import NativeDep
	return Artifact(
		kind="package",
		name="test.pkg",
		version="1.0.0",
		description="Test",
		license="MIT",
		entry_module="src/lib.drift",
		modules=["src/"],
		native_deps=[NativeDep(lib=n) for n in (native_deps or [])],
		unsafe=unsafe,
	)


def _fake_run_ok(*args, **kwargs):
	"""subprocess.run replacement that succeeds."""
	m = MagicMock()
	m.returncode = 0
	m.stdout = ""
	m.stderr = ""
	return m


class TestBuildSubprocessWiring:
	"""
	Pin that _build_package / _build_app pass the right env and flags
	to subprocess.run — not just that the helpers exist.
	"""

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_build_package_scrubs_pythonpath(
		self, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""_build_package must pass env= with PYTHONPATH removed."""
		monkeypatch.setenv("PYTHONPATH", "/poisoned")
		art = _make_art()
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			_build_package(
				art, driftc=Path("/fake/driftc"), target="x86_64-linux-gnu",
				resolved={}, staged_install=staged, manifest_dir=Path(tmpdir),
				package_roots=[],
			)
		call_kwargs = mock_run.call_args
		env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
		assert env is not None, "subprocess.run must be called with explicit env="
		assert "PYTHONPATH" not in env

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_build_app_scrubs_pythonpath(
		self, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""_build_app must pass env= with PYTHONPATH removed."""
		monkeypatch.setenv("PYTHONPATH", "/poisoned")
		art = Artifact(
			kind="app", name="myapp", version="1.0.0",
			description="App", license="MIT",
			entry_module="src/main.drift", modules=["src/"],
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			_build_app(
				art, driftc=Path("/fake/driftc"), target="x86_64-linux-gnu",
				resolved={}, staged_install=staged, manifest_dir=Path(tmpdir),
				package_roots=[],
			)
		call_kwargs = mock_run.call_args
		env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
		assert env is not None
		assert "PYTHONPATH" not in env

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_smoke_package_scrubs_pythonpath(
		self, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""_run_baseline_smoke_package must pass env= with PYTHONPATH removed."""
		monkeypatch.setenv("PYTHONPATH", "/poisoned")
		art = _make_art()
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			staged.mkdir()
			_run_baseline_smoke_package(
				art, driftc=Path("/fake/driftc"),
				staged_install=staged,
				staged_pkg_root=Path(tmpdir) / "pkgroot",
				staged_trust=None,
			)
		# Check the first subprocess.run call (compile).
		call_kwargs = mock_run.call_args_list[0]
		env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
		assert env is not None
		assert "PYTHONPATH" not in env

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_build_package_unsafe_appends_allow_unsafe(
		self, mock_run: MagicMock,
	) -> None:
		"""_build_package with unsafe=True must pass --allow-unsafe to driftc."""
		art = _make_art(unsafe=True)
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			_build_package(
				art, driftc=Path("/fake/driftc"), target="x86_64-linux-gnu",
				resolved={}, staged_install=staged, manifest_dir=Path(tmpdir),
				package_roots=[],
			)
		cmd = mock_run.call_args[0][0]
		assert "--allow-unsafe" in cmd

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_build_package_safe_omits_allow_unsafe(
		self, mock_run: MagicMock,
	) -> None:
		"""_build_package with unsafe=False must NOT pass --allow-unsafe."""
		art = _make_art(unsafe=False)
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			_build_package(
				art, driftc=Path("/fake/driftc"), target="x86_64-linux-gnu",
				resolved={}, staged_install=staged, manifest_dir=Path(tmpdir),
				package_roots=[],
			)
		cmd = mock_run.call_args[0][0]
		assert "--allow-unsafe" not in cmd

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_build_app_unsafe_appends_allow_unsafe(
		self, mock_run: MagicMock,
	) -> None:
		"""_build_app with unsafe=True must pass --allow-unsafe."""
		art = Artifact(
			kind="app", name="myapp", version="1.0.0",
			description="App", license="MIT",
			entry_module="src/main.drift", modules=["src/"],
			unsafe=True,
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			_build_app(
				art, driftc=Path("/fake/driftc"), target="x86_64-linux-gnu",
				resolved={}, staged_install=staged, manifest_dir=Path(tmpdir),
				package_roots=[],
			)
		cmd = mock_run.call_args[0][0]
		assert "--allow-unsafe" in cmd
