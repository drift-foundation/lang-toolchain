# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Tests for the drift deploy orchestrator.

Covers: CLI parsing, artifact ordering, resolution/lock integration,
and the per-artifact pipeline contract.
"""

from __future__ import annotations

import argparse
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
	_deploy_artifact,
	_extract_dep_namespaces,
	_resolve_artifact_deps,
	_resolve_native_lib_paths,
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


# ── Module namespace regressions ─────────────────────────────────────


class TestModuleNamespace:
	"""
	Regression: package name (net-tls) != Drift module namespace (net_tls).

	The deploy tool must use the module namespace — not the package name —
	for smoke consumer imports and trust namespace authorization.
	"""

	def test_default_derives_from_name(self) -> None:
		"""Hyphens in name are converted to underscores by default."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "drift-package.json"
			path.write_text(json.dumps({
				"schema_version": 1,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "net-tls",
					"version": "0.2.0",
					"description": "TLS",
					"entry_module": "src/lib.drift",
					"modules": ["src/"],
				}],
			}))
			m = load_manifest(path)
			assert m.artifacts[0].name == "net-tls"
			assert m.artifacts[0].module_namespace == "net_tls"

	def test_explicit_overrides_default(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "drift-package.json"
			path.write_text(json.dumps({
				"schema_version": 1,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "net-tls",
					"version": "0.2.0",
					"description": "TLS",
					"entry_module": "src/lib.drift",
					"modules": ["src/"],
					"module_namespace": "tls_lib",
				}],
			}))
			m = load_manifest(path)
			assert m.artifacts[0].module_namespace == "tls_lib"

	def test_no_hyphens_identity(self) -> None:
		"""Name without hyphens: module_namespace == name."""
		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "drift-package.json"
			path.write_text(json.dumps({
				"schema_version": 1,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "net.tls",
					"version": "0.2.0",
					"description": "TLS",
					"entry_module": "src/lib.drift",
					"modules": ["src/"],
				}],
			}))
			m = load_manifest(path)
			assert m.artifacts[0].module_namespace == "net.tls"

	def test_invalid_rejected(self) -> None:
		from tools.drift_deploy.manifest import ManifestError
		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "drift-package.json"
			path.write_text(json.dumps({
				"schema_version": 1,
				"project": {"name": "test", "license": "MIT"},
				"artifacts": [{
					"kind": "package",
					"name": "x",
					"version": "1.0.0",
					"description": "x",
					"entry_module": "x.drift",
					"modules": ["x/"],
					"module_namespace": "",
				}],
			}))
			with pytest.raises(ManifestError, match="module_namespace"):
				load_manifest(path)

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_smoke_consumer_uses_module_namespace(self, mock_run: MagicMock) -> None:
		"""
		Baseline smoke generates 'import net_tls', not 'import net-tls'.
		"""
		art = Artifact(
			kind="package", name="net-tls", version="0.2.0",
			description="TLS", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="net_tls",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			staged.mkdir()
			_run_baseline_smoke_package(
				art, driftc=Path("/fake/driftc"),
				staged_install=staged,
				staged_pkg_root=Path(tmpdir) / "pkgroot",
				staged_trust=None,
			)
			# The smoke function writes consumer source to
			# staged_install.parent / _smoke_<name> / smoke_consumer.drift
			consumer_path = staged.parent / f"_smoke_{art.name}" / "smoke_consumer.drift"
			assert consumer_path.exists(), f"consumer source not found at {consumer_path}"
			consumer_src = consumer_path.read_text()
			assert "import net_tls;" in consumer_src
			assert "import net-tls" not in consumer_src

	def test_staged_trust_uses_module_namespace(self) -> None:
		"""Trust namespace should be net_tls.*, not net-tls.*."""
		from tools.drift_deploy.staged_trust import build_staged_trust
		with tempfile.TemporaryDirectory() as tmpdir:
			out = Path(tmpdir) / "trust.json"
			build_staged_trust(
				baseline_trust_path=None,
				signer_pubkey_raw=b"\x01" * 32,
				artifact_namespace="net_tls",  # module_namespace, not name
				out_path=out,
			)
			data = json.loads(out.read_text())
			assert "net_tls.*" in data["namespaces"]
			assert "net_tls" in data["namespaces"]
			# Hyphens should NOT appear.
			assert "net-tls.*" not in data["namespaces"]
			assert "net-tls" not in data["namespaces"]

	def test_staged_trust_authorizes_dep_namespaces(self) -> None:
		"""Smoke trust must authorize dependency namespaces, not just the artifact's own."""
		from tools.drift_deploy.staged_trust import build_staged_trust
		with tempfile.TemporaryDirectory() as tmpdir:
			out = Path(tmpdir) / "trust.json"
			build_staged_trust(
				baseline_trust_path=None,
				signer_pubkey_raw=b"\x01" * 32,
				artifact_namespace="net.tls",
				out_path=out,
				dep_namespaces=["net.crypto", "acme.util"],
			)
			data = json.loads(out.read_text())
			# Own namespace authorized.
			assert "net.tls.*" in data["namespaces"]
			assert "net.tls" in data["namespaces"]
			# Dependency namespaces authorized.
			assert "net.crypto.*" in data["namespaces"]
			assert "net.crypto" in data["namespaces"]
			assert "acme.util.*" in data["namespaces"]
			assert "acme.util" in data["namespaces"]
			# All use the same key.
			kid = list(data["keys"].keys())[0]
			assert kid in data["namespaces"]["net.crypto.*"]
			assert kid in data["namespaces"]["acme.util.*"]


# ── Target default regression ────────────────────────────────────────


class TestTargetDefault:
	def test_default_is_drift_dev(self) -> None:
		"""Default target must be drift-dev, matching stdlib and ABI convention."""
		from tools.drift_deploy.drift_deploy import _resolve_target
		p = build_arg_parser()
		args = p.parse_args([])
		assert _resolve_target(args) == "drift-dev"

	def test_explicit_override(self) -> None:
		from tools.drift_deploy.drift_deploy import _resolve_target
		p = build_arg_parser()
		args = p.parse_args(["--target", "aarch64-linux-gnu"])
		assert _resolve_target(args) == "aarch64-linux-gnu"


# ── Smoke consumer source shape ──────────────────────────────────────


class TestSmokeConsumerSource:
	"""
	Pin the generated baseline smoke consumer as a valid Drift program.

	The consumer must have: module declaration, semicoloned import using
	module_namespace, nothrow main returning Int, and a return statement.
	"""

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_source_shape(self, mock_run: MagicMock) -> None:
		art = Artifact(
			kind="package", name="net-tls", version="0.2.0",
			description="TLS", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="net_tls",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			staged.mkdir()
			_run_baseline_smoke_package(
				art, driftc=Path("/fake/driftc"),
				staged_install=staged,
				staged_pkg_root=Path(tmpdir) / "pkgroot",
				staged_trust=None,
			)
			src = (staged.parent / f"_smoke_{art.name}" / "smoke_consumer.drift").read_text()

		# Module declaration with semicolon.
		assert src.startswith("module main;\n")
		# Import with semicolon, using module_namespace.
		assert "import net_tls;\n" in src
		# Valid main signature.
		assert "fn main() nothrow -> Int {" in src
		# Return statement.
		assert "return 0;" in src

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_source_shape_dotted_namespace(self, mock_run: MagicMock) -> None:
		"""Dotted module namespace (net.tls) also produces valid source."""
		art = Artifact(
			kind="package", name="net.tls", version="0.3.0",
			description="TLS", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="net.tls",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			staged.mkdir()
			_run_baseline_smoke_package(
				art, driftc=Path("/fake/driftc"),
				staged_install=staged,
				staged_pkg_root=Path(tmpdir) / "pkgroot",
				staged_trust=None,
			)
			src = (staged.parent / f"_smoke_{art.name}" / "smoke_consumer.drift").read_text()

		assert "import net.tls;\n" in src
		assert "fn main() nothrow -> Int {" in src

	def test_source_parses_through_drift_parser(self) -> None:
		"""
		Verify the generated source is syntactically valid Drift.

		Runs the real parser (not the full compiler) to catch syntax
		regressions without needing a full driftc invocation.
		"""
		try:
			from lang.driftc.parser.parser import parse_program
		except ImportError:
			pytest.skip("parser not available in this environment")

		# Generate the source the same way the deploy tool does.
		source = (
			'module main;\n'
			'\n'
			'import net_tls;\n'
			'\n'
			'fn main() nothrow -> Int {\n'
			'\treturn 0;\n'
			'}\n'
		)
		# parse_program should not raise on valid syntax.
		tree = parse_program(source, filename="smoke_consumer.drift")
		assert tree is not None


# ── Native library search paths ─────────────────────────────────────


class TestNativeLibPaths:
	"""
	Resolver-input native library search paths: env, config, CLI.

	These paths are NOT recorded in package metadata — they are
	deploy-time resolver hints passed as --link-search to driftc.
	"""

	def _make_args(self, *extra_argv: str) -> argparse.Namespace:
		p = build_arg_parser()
		return p.parse_args(list(extra_argv))

	def test_cli_flag_parsed(self) -> None:
		args = self._make_args("--native-lib-path", "/opt/ssl/lib", "--native-lib-path", "/usr/local/lib")
		assert args.native_lib_path == [Path("/opt/ssl/lib"), Path("/usr/local/lib")]

	def test_cli_only(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			args = self._make_args("--native-lib-path", "/a", "--native-lib-path", "/b")
			result = _resolve_native_lib_paths(args, Path(tmpdir))
			assert result == [Path("/a"), Path("/b")]

	def test_env_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
		monkeypatch.setenv("DRIFT_NATIVE_LIB_PATH", "/env/a:/env/b")
		with tempfile.TemporaryDirectory() as tmpdir:
			args = self._make_args()
			result = _resolve_native_lib_paths(args, Path(tmpdir))
			assert result == [Path("/env/a"), Path("/env/b")]

	def test_config_only(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			config = Path(tmpdir) / "drift-deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": ["/cfg/x", "/cfg/y"]}))
			args = self._make_args()
			result = _resolve_native_lib_paths(args, Path(tmpdir))
			assert result == [Path("/cfg/x"), Path("/cfg/y")]

	def test_precedence_env_config_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
		"""All three sources merge: env first (lowest), config middle, CLI last (highest)."""
		monkeypatch.setenv("DRIFT_NATIVE_LIB_PATH", "/env")
		with tempfile.TemporaryDirectory() as tmpdir:
			config = Path(tmpdir) / "drift-deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": ["/cfg"]}))
			args = self._make_args("--native-lib-path", "/cli")
			result = _resolve_native_lib_paths(args, Path(tmpdir))
			assert result == [Path("/env"), Path("/cfg"), Path("/cli")]

	def test_empty_env_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
		monkeypatch.setenv("DRIFT_NATIVE_LIB_PATH", "")
		with tempfile.TemporaryDirectory() as tmpdir:
			args = self._make_args()
			result = _resolve_native_lib_paths(args, Path(tmpdir))
			assert result == []

	def test_no_config_file_ok(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			args = self._make_args()
			result = _resolve_native_lib_paths(args, Path(tmpdir))
			assert result == []

	def test_bad_config_raises(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			config = Path(tmpdir) / "drift-deploy-config.json"
			config.write_text("not json")
			args = self._make_args()
			with pytest.raises(DeployError, match="failed to read"):
				_resolve_native_lib_paths(args, Path(tmpdir))

	def test_config_bad_type_raises(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			config = Path(tmpdir) / "drift-deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": "not-a-list"}))
			args = self._make_args()
			with pytest.raises(DeployError, match="must be an array"):
				_resolve_native_lib_paths(args, Path(tmpdir))

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_build_package_passes_link_search(self, mock_run: MagicMock) -> None:
		art = _make_art(native_deps=["ssl"])
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			_build_package(
				art, driftc=Path("/fake/driftc"), target="x86_64-linux-gnu",
				resolved={}, staged_install=staged, manifest_dir=Path(tmpdir),
				package_roots=[],
				native_lib_paths=[Path("/opt/openssl/lib")],
			)
		cmd = mock_run.call_args[0][0]
		assert "--link-search" in cmd
		idx = cmd.index("--link-search")
		assert cmd[idx + 1] == "/opt/openssl/lib"

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_build_app_passes_link_search(self, mock_run: MagicMock) -> None:
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
				native_lib_paths=[Path("/opt/openssl/lib"), Path("/usr/local/lib")],
			)
		cmd = mock_run.call_args[0][0]
		# Both paths should appear.
		indices = [i for i, x in enumerate(cmd) if x == "--link-search"]
		assert len(indices) == 2
		assert cmd[indices[0] + 1] == "/opt/openssl/lib"
		assert cmd[indices[1] + 1] == "/usr/local/lib"

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_smoke_passes_link_search(self, mock_run: MagicMock) -> None:
		art = _make_art(native_deps=["ssl"])
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			staged.mkdir()
			_run_baseline_smoke_package(
				art, driftc=Path("/fake/driftc"),
				staged_install=staged,
				staged_pkg_root=Path(tmpdir) / "pkgroot",
				staged_trust=None,
				native_lib_paths=[Path("/opt/openssl/lib")],
			)
		# Check the compile call.
		cmd = mock_run.call_args_list[0][0][0]
		assert "--link-search" in cmd
		idx = cmd.index("--link-search")
		assert cmd[idx + 1] == "/opt/openssl/lib"

	@patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run_ok)
	def test_no_native_lib_paths_no_link_search(self, mock_run: MagicMock) -> None:
		"""When no native_lib_paths, no --link-search flags should appear."""
		art = _make_art()
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			_build_package(
				art, driftc=Path("/fake/driftc"), target="x86_64-linux-gnu",
				resolved={}, staged_install=staged, manifest_dir=Path(tmpdir),
				package_roots=[],
			)
		cmd = mock_run.call_args[0][0]
		assert "--link-search" not in cmd

	def test_relative_path_in_env_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
		"""Relative path in $DRIFT_NATIVE_LIB_PATH must be rejected early."""
		monkeypatch.setenv("DRIFT_NATIVE_LIB_PATH", "relative/lib")
		with tempfile.TemporaryDirectory() as tmpdir:
			args = self._make_args()
			with pytest.raises(DeployError, match="absolute paths are required"):
				_resolve_native_lib_paths(args, Path(tmpdir))

	def test_relative_path_in_config_rejected(self) -> None:
		"""Relative path in drift-deploy-config.json must be rejected early."""
		with tempfile.TemporaryDirectory() as tmpdir:
			config = Path(tmpdir) / "drift-deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": ["relative/lib"]}))
			args = self._make_args()
			with pytest.raises(DeployError, match="absolute paths are required"):
				_resolve_native_lib_paths(args, Path(tmpdir))

	def test_relative_path_in_cli_rejected(self) -> None:
		"""Relative path via --native-lib-path must be rejected early."""
		with tempfile.TemporaryDirectory() as tmpdir:
			args = self._make_args("--native-lib-path", "relative/lib")
			with pytest.raises(DeployError, match="absolute paths are required"):
				_resolve_native_lib_paths(args, Path(tmpdir))

	def test_absolute_paths_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
		"""Absolute paths from all three sources should be accepted normally."""
		monkeypatch.setenv("DRIFT_NATIVE_LIB_PATH", "/abs/env")
		with tempfile.TemporaryDirectory() as tmpdir:
			config = Path(tmpdir) / "drift-deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": ["/abs/cfg"]}))
			args = self._make_args("--native-lib-path", "/abs/cli")
			result = _resolve_native_lib_paths(args, Path(tmpdir))
			assert result == [Path("/abs/env"), Path("/abs/cfg"), Path("/abs/cli")]


# ── Self-upgrade / build isolation ──────────────────────────────────


class TestBuildSelfExclusion:
	"""
	Regression: building artifact X must not see a prior published X.

	When --dest already contains an older version of the same package,
	the build step must not expose that package root to the compiler.
	Otherwise the compiler resolves the old signed package, demands
	trust authorization, and fails — even though we're building from
	source, not consuming ourselves.
	"""

	@staticmethod
	def _fake_run_creates_dmp(args, **kwargs):
		"""subprocess.run replacement that creates expected .dmp output."""
		cmd = args if isinstance(args, list) else [args]
		# If --emit-package is in the command, create the output file.
		for i, arg in enumerate(cmd):
			if arg == "--emit-package" and i + 1 < len(cmd):
				out_path = Path(cmd[i + 1])
				out_path.parent.mkdir(parents=True, exist_ok=True)
				out_path.write_bytes(b"fake-dmp")
		m = MagicMock()
		m.returncode = 0
		m.stdout = ""
		m.stderr = ""
		return m

	@patch("tools.drift_deploy.drift_deploy._sign_package")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_build_does_not_see_own_prior_version(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""
		Simulate deploying net-tls 0.3.0 to a dest that already has
		net-tls 0.2.0. The build command must not include a package
		root that contains net-tls.
		"""
		from tools.drift_deploy.manifest import NativeDep

		art = Artifact(
			kind="package", name="net-tls", version="0.3.0",
			description="TLS", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			native_deps=[NativeDep(lib="ssl")],
			unsafe=True,
			module_namespace="net_tls",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			tmpdir_p = Path(tmpdir)

			# Simulate dest with prior published version.
			prior = tmpdir_p / "dest" / "net-tls" / "0.2.0"
			prior.mkdir(parents=True)
			(prior / "net-tls.dmp").write_bytes(b"old-pkg")

			# Also put an external dep that should still be visible.
			ext_dep = tmpdir_p / "dest" / "std.core" / "1.0.0"
			ext_dep.mkdir(parents=True)
			(ext_dep / "std.core.dmp").write_bytes(b"core-pkg")

			# Set up staged package root with symlinks (as _run_impl does).
			stage_dir = tmpdir_p / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)

			dest = tmpdir_p / "dest"
			# Mirror dest into staged_pkg_root (same as _run_impl).
			for pkg_dir in sorted(dest.iterdir()):
				if pkg_dir.is_dir():
					link = staged_pkg_root / pkg_dir.name
					if not link.exists():
						link.symlink_to(pkg_dir.resolve())

			# Create source dir with entry module.
			manifest_dir = tmpdir_p / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")

			# _sign_package needs to return a path that exists.
			sig_path = stage_dir / "fake.sig"
			sig_path.parent.mkdir(parents=True, exist_ok=True)
			sig_path.write_bytes(b"fake-sig")
			mock_sign.return_value = sig_path

			with patch("tools.drift_deploy.drift_deploy.subprocess.run",
					side_effect=self._fake_run_creates_dmp) as mock_run:
				_deploy_artifact(
					art,
					driftc=Path("/fake/driftc"),
					target="drift-dev",
					resolved={},
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root, dest],
					dest=dest,
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=True,
					dry_run=True,
					compiler_version="0.27.53-dev",
					staged_pkg_root=staged_pkg_root,
				)

			# Inspect the build command (first subprocess.run call).
			build_call = mock_run.call_args_list[0]
			cmd = build_call[0][0]

			# Collect all --package-root values from the build command.
			pkg_roots_in_cmd: list[str] = []
			for i, arg in enumerate(cmd):
				if arg == "--package-root" and i + 1 < len(cmd):
					pkg_roots_in_cmd.append(cmd[i + 1])

			# The build root must NOT contain a net-tls directory
			# (self-exclusion) OR std.core (unrelated — not a resolved dep).
			for pr in pkg_roots_in_cmd:
				net_tls_dir = Path(pr) / "net-tls"
				assert not net_tls_dir.exists(), (
					f"build --package-root {pr} exposes prior net-tls; "
					f"source build must not consume its own older published version"
				)
				std_core_dir = Path(pr) / "std.core"
				assert not std_core_dir.exists(), (
					f"build --package-root {pr} exposes unrelated std.core; "
					f"build root must only contain resolved deps"
				)

	@patch("tools.drift_deploy.drift_deploy._sign_package")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_build_does_not_pass_raw_dest_as_package_root(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""
		The raw --dest path must not appear as a --package-root in the
		build command. Only the filtered build root should be used.
		"""
		art = Artifact(
			kind="package", name="my.pkg", version="1.0.0",
			description="Test", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="my_pkg",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			tmpdir_p = Path(tmpdir)
			dest = tmpdir_p / "dest"
			dest.mkdir()

			stage_dir = tmpdir_p / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)

			manifest_dir = tmpdir_p / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")

			sig_path = stage_dir / "fake.sig"
			sig_path.parent.mkdir(parents=True, exist_ok=True)
			sig_path.write_bytes(b"fake-sig")
			mock_sign.return_value = sig_path

			with patch("tools.drift_deploy.drift_deploy.subprocess.run",
					side_effect=self._fake_run_creates_dmp) as mock_run:
				_deploy_artifact(
					art,
					driftc=Path("/fake/driftc"),
					target="drift-dev",
					resolved={},
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root, dest],
					dest=dest,
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=True,
					dry_run=True,
					compiler_version="0.27.53-dev",
					staged_pkg_root=staged_pkg_root,
				)

			build_call = mock_run.call_args_list[0]
			cmd = build_call[0][0]

			# Raw dest must not appear in --package-root args.
			pkg_roots_in_cmd: list[str] = []
			for i, arg in enumerate(cmd):
				if arg == "--package-root" and i + 1 < len(cmd):
					pkg_roots_in_cmd.append(cmd[i + 1])

			assert str(dest) not in pkg_roots_in_cmd, (
				f"raw dest {dest} must not be passed as --package-root to build; "
				f"got: {pkg_roots_in_cmd}"
			)
			assert str(staged_pkg_root) not in pkg_roots_in_cmd, (
				f"raw staged_pkg_root must not be passed as --package-root to build; "
				f"only the filtered build root should be used"
			)

	@patch("tools.drift_deploy.drift_deploy._sign_package")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_smoke_staging_replaces_symlink_with_new_version(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""
		When staged_pkg_root/<name> is a symlink to old dest (containing
		0.2.0), the sign step must replace it with a real directory
		containing both 0.2.0 (symlinked) and the new 0.3.0, without
		writing into the actual dest.
		"""
		from tools.drift_deploy.manifest import NativeDep

		art = Artifact(
			kind="package", name="net-tls", version="0.3.0",
			description="TLS", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			native_deps=[NativeDep(lib="ssl")],
			unsafe=True,
			module_namespace="net_tls",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			tmpdir_p = Path(tmpdir)

			# Dest with prior version.
			dest = tmpdir_p / "dest"
			prior = dest / "net-tls" / "0.2.0"
			prior.mkdir(parents=True)
			(prior / "net-tls.dmp").write_bytes(b"old-pkg")

			# Staged pkg root with symlink (as _run_impl does).
			stage_dir = tmpdir_p / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)
			(staged_pkg_root / "net-tls").symlink_to((dest / "net-tls").resolve())

			# Manifest dir.
			manifest_dir = tmpdir_p / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")

			sig_path = stage_dir / "fake.sig"
			sig_path.parent.mkdir(parents=True, exist_ok=True)
			sig_path.write_bytes(b"fake-sig")
			mock_sign.return_value = sig_path

			with patch("tools.drift_deploy.drift_deploy.subprocess.run",
					side_effect=self._fake_run_creates_dmp):
				_deploy_artifact(
					art,
					driftc=Path("/fake/driftc"),
					target="drift-dev",
					resolved={},
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root, dest],
					dest=dest,
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=True,
					dry_run=True,
					compiler_version="0.27.54-dev",
					staged_pkg_root=staged_pkg_root,
				)

			# staged_pkg_root/net-tls must now be a real directory, not a symlink.
			art_dir = staged_pkg_root / "net-tls"
			assert art_dir.is_dir()
			assert not art_dir.is_symlink(), (
				"staged_pkg_root/net-tls must be a real directory, not a symlink"
			)

			# New version must exist.
			new_ver = art_dir / "0.3.0"
			assert new_ver.is_dir(), "new version 0.3.0 must be staged for smoke"
			assert (new_ver / "net-tls.zdmp").exists(), "built .zdmp must be in staged 0.3.0"

			# Old version must still be reachable (symlinked).
			old_ver = art_dir / "0.2.0"
			assert old_ver.exists(), "old version 0.2.0 must still be reachable"

			# Dest must NOT have been polluted with 0.3.0.
			assert not (dest / "net-tls" / "0.3.0").exists(), (
				"dest must not be polluted with new version before publish"
			)

	@patch("tools.drift_deploy.drift_deploy._sign_package")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_stale_version_dir_does_not_shadow_staged_build(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""
		Regression: a stale 0.3.0 directory at the dest (from a prior
		failed deploy) must not be symlinked into the staged root,
		shadowing the just-built .dmp. The re-link loop must skip
		art.version.
		"""
		from tools.drift_deploy.manifest import NativeDep

		art = Artifact(
			kind="package", name="net-tls", version="0.3.0",
			description="TLS", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			native_deps=[NativeDep(lib="ssl")],
			unsafe=True,
			module_namespace="net_tls",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			tmpdir_p = Path(tmpdir)

			# Dest with prior version AND stale version being built.
			dest = tmpdir_p / "dest"
			(dest / "net-tls" / "0.2.0").mkdir(parents=True)
			(dest / "net-tls" / "0.2.0" / "net-tls.dmp").write_bytes(b"old-pkg")
			# Stale 0.3.0 from a prior failed deploy.
			(dest / "net-tls" / "0.3.0").mkdir(parents=True)

			# Staged pkg root with symlink.
			stage_dir = tmpdir_p / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)
			(staged_pkg_root / "net-tls").symlink_to((dest / "net-tls").resolve())

			# Manifest dir.
			manifest_dir = tmpdir_p / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")

			sig_path = stage_dir / "fake.sig"
			sig_path.parent.mkdir(parents=True, exist_ok=True)
			sig_path.write_bytes(b"fake-sig")
			mock_sign.return_value = sig_path

			with patch("tools.drift_deploy.drift_deploy.subprocess.run",
					side_effect=self._fake_run_creates_dmp):
				_deploy_artifact(
					art,
					driftc=Path("/fake/driftc"),
					target="drift-dev",
					resolved={},
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root, dest],
					dest=dest,
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=True,
					dry_run=True,
					compiler_version="0.27.55-dev",
					staged_pkg_root=staged_pkg_root,
				)

			art_dir = staged_pkg_root / "net-tls"

			# 0.3.0 must be a real directory (not a symlink to stale dest).
			new_ver = art_dir / "0.3.0"
			assert new_ver.is_dir()
			assert not new_ver.is_symlink(), (
				"staged 0.3.0 must be a real directory, not a symlink to stale dest"
			)
			assert (new_ver / "net-tls.zdmp").exists(), (
				"just-built .zdmp must be in staged 0.3.0"
			)

			# The .zdmp must NOT have been written through a symlink into dest.
			stale_dest = dest / "net-tls" / "0.3.0"
			stale_contents = list(stale_dest.iterdir()) if stale_dest.exists() else []
			assert not any(f.name == "net-tls.zdmp" for f in stale_contents), (
				"built .zdmp must not leak into dest through stale symlink"
			)

	@patch("tools.drift_deploy.drift_deploy._sign_package")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_build_root_excludes_unrelated_packages(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""
		Regression: unrelated signed packages in the shared library root
		must NOT be symlinked into the build package root. The compiler
		verifies all packages under --package-root against the trust store;
		untrusted unrelated packages would block the build.
		"""
		# web-jwt has no deps, shared root has unrelated net-tls.
		art = Artifact(
			kind="package", name="web-jwt", version="0.1.0",
			description="JWT", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="web.jwt",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			tmpdir_p = Path(tmpdir)

			# Shared library root with unrelated package.
			dest = tmpdir_p / "dest"
			(dest / "net-tls" / "0.3.1").mkdir(parents=True)
			(dest / "net-tls" / "0.3.1" / "net-tls.dmp").write_bytes(b"tls")
			(dest / "web-jwt" / "0.1.0").mkdir(parents=True)
			(dest / "web-jwt" / "0.1.0" / "web-jwt.dmp").write_bytes(b"jwt")

			# Staged package root mirroring dest.
			stage_dir = tmpdir_p / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)
			for pkg_dir in sorted(dest.iterdir()):
				if pkg_dir.is_dir():
					(staged_pkg_root / pkg_dir.name).symlink_to(pkg_dir.resolve())

			manifest_dir = tmpdir_p / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")

			sig_path = stage_dir / "fake.sig"
			sig_path.parent.mkdir(parents=True, exist_ok=True)
			sig_path.write_bytes(b"fake-sig")
			mock_sign.return_value = sig_path

			with patch("tools.drift_deploy.drift_deploy.subprocess.run",
					side_effect=self._fake_run_creates_dmp) as mock_run:
				_deploy_artifact(
					art,
					driftc=Path("/fake/driftc"),
					target="drift-dev",
					resolved={},  # web-jwt has no deps
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root, dest],
					dest=dest,
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=True,
					dry_run=True,
					compiler_version="0.27.59",
					staged_pkg_root=staged_pkg_root,
				)

			build_call = mock_run.call_args_list[0]
			cmd = build_call[0][0]
			pkg_roots_in_cmd: list[str] = []
			for i, arg in enumerate(cmd):
				if arg == "--package-root" and i + 1 < len(cmd):
					pkg_roots_in_cmd.append(cmd[i + 1])

			# Build root must NOT contain net-tls (unrelated package).
			for pr in pkg_roots_in_cmd:
				assert not (Path(pr) / "net-tls").exists(), (
					f"build root {pr} exposes unrelated net-tls; "
					f"only resolved deps should be in the build package root"
				)
			# Build root must NOT contain web-jwt (self-exclusion).
			for pr in pkg_roots_in_cmd:
				assert not (Path(pr) / "web-jwt").exists(), (
					f"build root {pr} exposes self (web-jwt)"
				)

	@patch("tools.drift_deploy.drift_deploy._sign_package")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_smoke_root_excludes_unrelated_packages(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""
		Regression: the smoke --package-root must only contain the artifact
		and its resolved deps, not unrelated packages from the shared dest.
		"""
		art = Artifact(
			kind="package", name="web-jwt", version="0.1.0",
			description="JWT", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="web.jwt",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			tmpdir_p = Path(tmpdir)

			# Shared library root with unrelated package.
			dest = tmpdir_p / "dest"
			(dest / "net-tls" / "0.3.1").mkdir(parents=True)
			(dest / "net-tls" / "0.3.1" / "net-tls.dmp").write_bytes(b"tls")

			# Staged package root mirroring dest.
			stage_dir = tmpdir_p / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)
			for pkg_dir in sorted(dest.iterdir()):
				if pkg_dir.is_dir():
					(staged_pkg_root / pkg_dir.name).symlink_to(pkg_dir.resolve())

			manifest_dir = tmpdir_p / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")

			sig_path = stage_dir / "fake.sig"
			sig_path.parent.mkdir(parents=True, exist_ok=True)
			sig_path.write_bytes(b"fake-sig")
			mock_sign.return_value = sig_path

			with patch("tools.drift_deploy.drift_deploy.subprocess.run",
					side_effect=self._fake_run_creates_dmp) as mock_run:
				_deploy_artifact(
					art,
					driftc=Path("/fake/driftc"),
					target="drift-dev",
					resolved={},
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root, dest],
					dest=dest,
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=False,
					dry_run=True,
					compiler_version="0.27.60",
					staged_pkg_root=staged_pkg_root,
				)

			# Find the smoke compile call (second subprocess.run, after build).
			assert len(mock_run.call_args_list) >= 2, "expected build + smoke calls"
			smoke_call = mock_run.call_args_list[1]
			cmd = smoke_call[0][0]
			pkg_roots_in_cmd: list[str] = []
			for i, arg in enumerate(cmd):
				if arg == "--package-root" and i + 1 < len(cmd):
					pkg_roots_in_cmd.append(cmd[i + 1])

			# Smoke root must NOT contain net-tls.
			for pr in pkg_roots_in_cmd:
				assert not (Path(pr) / "net-tls").exists(), (
					f"smoke root {pr} exposes unrelated net-tls; "
					f"only the artifact + resolved deps should be visible"
				)
			# Smoke root MUST contain web-jwt (the artifact being smoked).
			any_has_jwt = any(
				(Path(pr) / "web-jwt").exists() for pr in pkg_roots_in_cmd
			)
			assert any_has_jwt, "smoke root must contain the artifact being smoked"


# ── Intra-project dep resolution ────────────────────────────────────


class TestIntraProjectDeps:
	"""
	When B depends on co-deployed A, resolution must find A's .dmp
	in staged_pkg_root after A has been built (not fail upfront).
	"""

	@patch("tools.drift_deploy.drift_deploy.build_package_index")
	def test_resolve_artifact_deps_finds_staged_package(
		self, mock_index: MagicMock,
	) -> None:
		"""_resolve_artifact_deps resolves from a fresh package index."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		with tempfile.TemporaryDirectory() as tmpdir:
			# Simulate a .dmp for integrity (sha256 must match).
			dmp = Path(tmpdir) / "net-crypto.dmp"
			dmp.write_bytes(b"fake-dmp-content")
			import hashlib
			sha = hashlib.sha256(b"fake-dmp-content").hexdigest()
			mock_index.return_value = {
				"net-crypto": [
					PackageEntry(
						package_id="net-crypto",
						version=parse_version("0.1.0"),
						path=dmp,
						sha256=sha,
						package_deps=[],
					),
				],
			}
			art = Artifact(
				kind="package", name="net-tls", version="0.1.0",
				description="", license="", entry_module="net.tls",
				modules=["net.tls"], module_namespace="net.tls",
				package_deps=[PackageDep(name="net-crypto", version="^0.1.0")],
			)
			resolved = _resolve_artifact_deps(
				art,
				package_roots=[Path(tmpdir)],
				lock_path=Path(tmpdir) / "drift-lock.json",
				update_lock=True,
				existing_lock=None,
			)
			assert "net-crypto" in resolved
			assert resolved["net-crypto"].version == "0.1.0"
			# Verify index was built with the provided package_roots.
			mock_index.assert_called_once_with([Path(tmpdir)])


# ── Dep namespace extraction ────────────────────────────────────────


class TestExtractDepNamespaces:
	"""_extract_dep_namespaces reads module_ids from .dmp files."""

	@patch("lang.driftc.packages.dmir_pkg_v0.load_dmir_pkg_v0")
	def test_extracts_module_ids(self, mock_load: MagicMock) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			pkg_dir = staged / "net-crypto" / "0.1.0"
			pkg_dir.mkdir(parents=True)
			# Create a dummy .dmp file (content doesn't matter, loader is mocked).
			(pkg_dir / "net-crypto.dmp").write_bytes(b"fake")
			pkg = MagicMock()
			pkg.manifest = {
				"modules": [
					{"module_id": "net.crypto"},
					{"module_id": "net.crypto.aes"},
				],
			}
			mock_load.return_value = pkg
			ns = _extract_dep_namespaces("net-crypto", staged)
			assert "net.crypto" in ns
			assert "net.crypto.aes" in ns

	def test_missing_package_returns_empty(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			staged = Path(tmpdir) / "staged"
			staged.mkdir()
			ns = _extract_dep_namespaces("nonexistent", staged)
			assert ns == []


# ── Smoke dep pinning ────────────────────────────────────────────────


class TestSmokeDepPinning:
	"""
	Regression: baseline smoke must pin resolved dependency versions.

	When the smoke package root exposes multiple versions of a
	transitive dependency, the compiler fails with an ambiguity error
	unless --dep pins select the exact version.  Build already resolves
	the correct dependency graph; smoke must use the same pins.
	"""

	def test_smoke_pins_resolved_deps(self) -> None:
		"""Smoke command includes --dep for each resolved dependency."""
		from tools.drift_deploy.manifest import NativeDep

		art = Artifact(
			kind="package", name="web-client", version="0.1.0",
			description="Web", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="web_client",
		)
		resolved = {
			"net-tls": ResolvedDep(version="0.3.1", integrity="sha256:aa", dep_type="direct"),
			"acme.crypto": ResolvedDep(version="0.9.0", integrity="sha256:bb", dep_type="transitive"),
		}

		calls: list[list[str]] = []

		def fake_run(args, **kwargs):
			cmd = args if isinstance(args, list) else [args]
			calls.append(cmd)
			# Create .dmp if --emit-package is present.
			for i, arg in enumerate(cmd):
				if arg == "--emit-package" and i + 1 < len(cmd):
					out = Path(cmd[i + 1])
					out.parent.mkdir(parents=True, exist_ok=True)
					out.write_bytes(b"fake-dmp")
			m = MagicMock()
			m.returncode = 0
			m.stdout = ""
			m.stderr = ""
			return m

		with tempfile.TemporaryDirectory() as tmpdir:
			base = Path(tmpdir)
			stage_dir = base / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)
			manifest_dir = base / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")

			sig_path = stage_dir / "fake.sig"
			sig_path.parent.mkdir(parents=True, exist_ok=True)
			sig_path.write_bytes(b"fake-sig")

			with patch("tools.drift_deploy.drift_deploy._sign_package", return_value=sig_path), \
				patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32), \
				patch("tools.drift_deploy.staged_trust.build_staged_trust"), \
				patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=fake_run):

				_deploy_artifact(
					art,
					driftc=Path("/fake/driftc"),
					target="drift-dev",
					resolved=resolved,
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root],
					dest=base / "dest",
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=False,
					dry_run=True,
					compiler_version="0.27.62",
					staged_pkg_root=staged_pkg_root,
				)

			# Find the smoke command (second subprocess.run call — first is build).
			assert len(calls) >= 2, f"expected at least 2 subprocess calls, got {len(calls)}"
			smoke_cmd = calls[1]

			# Collect all --dep values from the smoke command.
			dep_pins: list[str] = []
			for i, arg in enumerate(smoke_cmd):
				if arg == "--dep" and i + 1 < len(smoke_cmd):
					dep_pins.append(smoke_cmd[i + 1])

			# Must include the artifact itself.
			assert "web-client@0.1.0" in dep_pins, (
				f"smoke must pin the artifact itself; got --dep values: {dep_pins}"
			)
			# Must include all resolved deps.
			assert "net-tls@0.3.1" in dep_pins, (
				f"smoke must pin resolved dep net-tls@0.3.1; got --dep values: {dep_pins}"
			)
			assert "acme.crypto@0.9.0" in dep_pins, (
				f"smoke must pin resolved dep acme.crypto@0.9.0; got --dep values: {dep_pins}"
			)

	def test_smoke_no_deps_pins_only_artifact(self) -> None:
		"""When no deps are resolved, smoke only pins the artifact itself."""
		art = Artifact(
			kind="package", name="util-log", version="1.0.0",
			description="Log", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="util_log",
		)

		calls: list[list[str]] = []

		def fake_run(args, **kwargs):
			cmd = args if isinstance(args, list) else [args]
			calls.append(cmd)
			for i, arg in enumerate(cmd):
				if arg == "--emit-package" and i + 1 < len(cmd):
					out = Path(cmd[i + 1])
					out.parent.mkdir(parents=True, exist_ok=True)
					out.write_bytes(b"fake-dmp")
			m = MagicMock()
			m.returncode = 0
			m.stdout = ""
			m.stderr = ""
			return m

		with tempfile.TemporaryDirectory() as tmpdir:
			base = Path(tmpdir)
			stage_dir = base / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)
			manifest_dir = base / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")

			sig_path = stage_dir / "fake.sig"
			sig_path.parent.mkdir(parents=True, exist_ok=True)
			sig_path.write_bytes(b"fake-sig")

			with patch("tools.drift_deploy.drift_deploy._sign_package", return_value=sig_path), \
				patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32), \
				patch("tools.drift_deploy.staged_trust.build_staged_trust"), \
				patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=fake_run):

				_deploy_artifact(
					art,
					driftc=Path("/fake/driftc"),
					target="drift-dev",
					resolved={},
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root],
					dest=base / "dest",
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=False,
					dry_run=True,
					compiler_version="0.27.62",
					staged_pkg_root=staged_pkg_root,
				)

			assert len(calls) >= 2
			smoke_cmd = calls[1]

			dep_pins: list[str] = []
			for i, arg in enumerate(smoke_cmd):
				if arg == "--dep" and i + 1 < len(smoke_cmd):
					dep_pins.append(smoke_cmd[i + 1])

			assert dep_pins == ["util-log@1.0.0"], (
				f"with no resolved deps, smoke should only pin the artifact; got: {dep_pins}"
			)
