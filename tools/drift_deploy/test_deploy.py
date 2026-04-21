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
	build_arg_parser,
)
from tools.drift_deploy.lockfile import write_lock
from tools.drift_deploy.provenance import CompilerInfo
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


def _drift_subdir(tmpdir) -> Path:
	"""Create and return ``<tmpdir>/drift`` for staging drift-owned metadata.

	Mirrors the post-rename layout: every drift-owned root metadata file
	(manifest.json, lock.json, deploy-config.json) lives under the
	``drift/`` subdirectory.  Tests that need to write any of these files
	should use this helper instead of constructing the path inline.
	"""
	d = Path(tmpdir) / "drift"
	d.mkdir(exist_ok=True)
	return d


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
		assert args.manifest == Path("drift") / "manifest.json"
		assert args.dest is None
		assert args.app_dest is None
		assert args.skip_smoke is False
		assert args.dry_run is False

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
			"--skip-smoke",
			"--dry-run",
		])
		assert args.manifest == Path("custom.json")
		assert args.dest == Path("/deploy")
		assert args.app_dest == Path("/apps")
		assert args.package_root == [Path("/pr1"), Path("/pr2")]
		assert args.artifact == ["net.tls", "tls-tool"]
		assert args.target == "aarch64-linux-gnu"
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
		"""Artifact with no package_deps returns empty resolution."""
		art = _art("my.pkg")
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			result = _resolve_artifact_deps(
				art,
				package_roots=[],
				lock_path=lock_path,
				existing_lock=None,
			)
			assert result == {}

	def test_missing_lock_raises(self) -> None:
		"""Artifact with deps but no lock → error directing to drift prepare."""
		art = _art("my.pkg", deps=[PackageDep("ext.lib", "^1.0.0")])
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			with pytest.raises(DeployError, match="drift prepare"):
				_resolve_artifact_deps(
					art,
					package_roots=[],
					lock_path=lock_path,
					existing_lock=None,
				)

	def test_missing_lock_entry_raises(self) -> None:
		"""Artifact in manifest but not in lock → error directing to drift prepare."""
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			write_lock(lock_path, {"other.pkg": {
				"dep.x": ResolvedDep(version="1.0.0", sha256="aa", dep_type="direct"),
			}})

			art = _art("my.pkg", deps=[PackageDep("dep.x", "^1.0.0")])
			existing_lock = {"other.pkg": {
				"dep.x": ResolvedDep(version="1.0.0", sha256="aa", dep_type="direct"),
			}}

			with pytest.raises(DeployError, match="drift prepare"):
				_resolve_artifact_deps(
					art,
					package_roots=[],
					lock_path=lock_path,
					existing_lock=existing_lock,
				)

	def test_missing_dep_in_lock_raises(self) -> None:
		"""Dep declared in manifest but missing from lock entry → error."""
		art = _art("my.pkg", deps=[
			PackageDep("dep.a", "^1.0.0"),
			PackageDep("dep.b", "^2.0.0"),
		])
		existing_lock = {"my.pkg": {
			"dep.a": ResolvedDep(version="1.0.0", sha256="aa", dep_type="direct"),
			# dep.b missing
		}}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			with pytest.raises(DeployError, match="dep.b.*drift prepare"):
				_resolve_artifact_deps(
					art,
					package_roots=[],
					lock_path=lock_path,
					existing_lock=existing_lock,
				)

	def test_sha_mismatch_points_to_drift_prepare(self) -> None:
		"""Lock-vs-disk sha256 mismatch at deploy time is a hard error
		with a `drift prepare` pointer.  Pins the strict-exact deploy
		contract: never silently accept a rebuilt/replaced artifact."""
		from tools.drift_deploy.resolver import PackageEntry
		import hashlib
		art = _art("my.pkg", deps=[PackageDep("dep.a", "0.1")])
		locked_sha = hashlib.sha256(b"dep.a@0.1.3 locked").hexdigest()
		ondisk_sha = hashlib.sha256(b"rebuilt-different-bytes").hexdigest()
		existing_lock = {"my.pkg": {
			"dep.a": ResolvedDep(
				version="0.1.3", sha256=locked_sha,
				dep_type="direct", package_id="dep.a",
				author_key="ed25519:test",
			),
		}}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			dmp = Path(tmpdir) / "dep.a-0.1.3.dmp"
			dmp.write_bytes(b"fake")
			with patch("tools.drift_deploy.drift_deploy.build_package_index") as mock_idx:
				mock_idx.return_value = {
					"dep.a": [PackageEntry(
						package_id="dep.a", version=parse_version("0.1.3"),
						path=dmp, sha256=ondisk_sha, required_deps=[],
						author_key="ed25519:test",
					)],
				}
				with pytest.raises(DeployError) as exc_info:
					_resolve_artifact_deps(
						art, package_roots=[Path(tmpdir)],
						lock_path=lock_path, existing_lock=existing_lock,
					)
		err = str(exc_info.value)
		assert "dep.a" in err
		assert "sha256" in err
		assert "drift prepare" in err, (
			f"sha-mismatch deploy error must cite drift prepare; got:\n{err}"
		)

	def test_author_key_mismatch_points_to_drift_prepare(self) -> None:
		"""Lock signer vs on-disk signer mismatch is a hard deploy
		error with a `drift prepare` pointer.  Pins signer re-check
		on the deploy path."""
		from tools.drift_deploy.resolver import PackageEntry
		import hashlib
		art = _art("my.pkg", deps=[PackageDep("dep.a", "0.1")])
		same_sha = hashlib.sha256(b"dep.a@0.1.3").hexdigest()
		existing_lock = {"my.pkg": {
			"dep.a": ResolvedDep(
				version="0.1.3", sha256=same_sha,
				dep_type="direct", package_id="dep.a",
				author_key="ed25519:OLD_KEY",
			),
		}}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			dmp = Path(tmpdir) / "dep.a-0.1.3.dmp"
			dmp.write_bytes(b"fake")
			with patch("tools.drift_deploy.drift_deploy.build_package_index") as mock_idx:
				mock_idx.return_value = {
					"dep.a": [PackageEntry(
						package_id="dep.a", version=parse_version("0.1.3"),
						path=dmp, sha256=same_sha, required_deps=[],
						author_key="ed25519:NEW_KEY",
					)],
				}
				with pytest.raises(DeployError) as exc_info:
					_resolve_artifact_deps(
						art, package_roots=[Path(tmpdir)],
						lock_path=lock_path, existing_lock=existing_lock,
					)
		err = str(exc_info.value)
		assert "dep.a" in err
		assert "key" in err.lower()
		assert "drift prepare" in err, (
			f"author-key mismatch deploy error must cite drift prepare; "
			f"got:\n{err}"
		)

	def test_missing_ondisk_package_points_to_drift_prepare(self) -> None:
		"""Lock pins a version that is absent from the package roots
		(e.g., the user cleaned their cache between prepare and deploy)
		→ hard error pointing at `drift prepare`."""
		import hashlib
		art = _art("my.pkg", deps=[PackageDep("dep.a", "0.1")])
		existing_lock = {"my.pkg": {
			"dep.a": ResolvedDep(
				version="0.1.3",
				sha256=hashlib.sha256(b"dep.a@0.1.3").hexdigest(),
				dep_type="direct", package_id="dep.a",
				author_key="ed25519:test",
			),
		}}
		with tempfile.TemporaryDirectory() as tmpdir:
			lock_path = _drift_subdir(tmpdir) / "lock.json"
			with patch("tools.drift_deploy.drift_deploy.build_package_index") as mock_idx:
				mock_idx.return_value = {}  # empty — dep.a not on disk
				with pytest.raises(DeployError) as exc_info:
					_resolve_artifact_deps(
						art, package_roots=[Path(tmpdir)],
						lock_path=lock_path, existing_lock=existing_lock,
					)
		err = str(exc_info.value)
		assert "dep.a" in err and "0.1.3" in err
		assert "not found" in err
		assert "drift prepare" in err, (
			f"missing-ondisk-package deploy error must cite drift prepare; "
			f"got:\n{err}"
		)


# ── Provenance ───────────────────────────────────────────────────────


class TestProvenanceInDeploy:
	def test_provenance_output(self) -> None:
		from tools.drift_deploy.provenance import build_provenance, write_provenance

		compiler = CompilerInfo(version="0.27.93", abi=6, commit="abc1234")
		with tempfile.TemporaryDirectory() as tmpdir:
			path = Path(tmpdir) / "myapp.provenance.json"
			prov_bytes = build_provenance(
				artifact_name="myapp",
				artifact_version="1.0.0",
				artifact_kind="app",
				artifact_sha256="0000000000000000000000000000000000000000000000000000000000000000",
				target="x86_64-linux-gnu",
				compiler=compiler,
				resolved_deps={
					"net.tls": {"version": "0.3.0", "sha256": "aa"},
				},
			)
			write_provenance(path, prov_bytes)
			data = json.loads(path.read_text())
			assert data["schema_version"] == 3
			assert data["artifact_name"] == "myapp"
			assert data["artifact_version"] == "1.0.0"
			assert data["artifact_kind"] == "app"
			assert data["target"] == "x86_64-linux-gnu"
			assert data["compiler_version"] == "0.27.93"
			assert data["compiler_commit"] == "abc1234"
			assert data["abi"] == 6
			assert "build_utc" in data
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

	When drift deploy is invoked via the drift-deploy PEX binary (or via
	legacy PYTHONPATH invocation), PYTHONPATH must not leak into child
	driftc (PEX) invocations, causing it to pick up unbundled lang/
	modules and crash with ModuleNotFoundError.
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
			path = _drift_subdir(tmpdir) / "manifest.json"
			path.write_text(json.dumps({
				"schema_version": 2,
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
			path = _drift_subdir(tmpdir) / "manifest.json"
			path.write_text(json.dumps({
				"schema_version": 2,
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
			path = _drift_subdir(tmpdir) / "manifest.json"
			path.write_text(json.dumps({
				"schema_version": 2,
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
			path = _drift_subdir(tmpdir) / "manifest.json"
			path.write_text(json.dumps({
				"schema_version": 2,
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
			path = _drift_subdir(tmpdir) / "manifest.json"
			path.write_text(json.dumps({
				"schema_version": 2,
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
			path = _drift_subdir(tmpdir) / "manifest.json"
			path.write_text(json.dumps({
				"schema_version": 2,
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
			path = _drift_subdir(tmpdir) / "manifest.json"
			path.write_text(json.dumps({
				"schema_version": 2,
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
			drift_dir = _drift_subdir(tmpdir)
			config = drift_dir / "deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": ["/cfg/x", "/cfg/y"]}))
			args = self._make_args()
			result = _resolve_native_lib_paths(args, drift_dir)
			assert result == [Path("/cfg/x"), Path("/cfg/y")]

	def test_precedence_env_config_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
		"""All three sources merge: env first (lowest), config middle, CLI last (highest)."""
		monkeypatch.setenv("DRIFT_NATIVE_LIB_PATH", "/env")
		with tempfile.TemporaryDirectory() as tmpdir:
			drift_dir = _drift_subdir(tmpdir)
			config = drift_dir / "deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": ["/cfg"]}))
			args = self._make_args("--native-lib-path", "/cli")
			result = _resolve_native_lib_paths(args, drift_dir)
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
			drift_dir = _drift_subdir(tmpdir)
			config = drift_dir / "deploy-config.json"
			config.write_text("not json")
			args = self._make_args()
			with pytest.raises(DeployError, match="failed to read"):
				_resolve_native_lib_paths(args, drift_dir)

	def test_config_bad_type_raises(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			drift_dir = _drift_subdir(tmpdir)
			config = drift_dir / "deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": "not-a-list"}))
			args = self._make_args()
			with pytest.raises(DeployError, match="must be an array"):
				_resolve_native_lib_paths(args, drift_dir)

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
		"""Relative path in drift/deploy-config.json must be rejected early."""
		with tempfile.TemporaryDirectory() as tmpdir:
			drift_dir = _drift_subdir(tmpdir)
			config = drift_dir / "deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": ["relative/lib"]}))
			args = self._make_args()
			with pytest.raises(DeployError, match="absolute paths are required"):
				_resolve_native_lib_paths(args, drift_dir)

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
			drift_dir = _drift_subdir(tmpdir)
			config = drift_dir / "deploy-config.json"
			config.write_text(json.dumps({"native_lib_paths": ["/abs/cfg"]}))
			args = self._make_args("--native-lib-path", "/abs/cli")
			result = _resolve_native_lib_paths(args, drift_dir)
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

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
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

			# _sign_artifact needs to return a path that exists.
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
					compiler_info=CompilerInfo(version="0.27.53", abi=6, commit="unknown"),
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

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
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
					compiler_info=CompilerInfo(version="0.27.53", abi=6, commit="unknown"),
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

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_smoke_staging_replaces_symlink_with_new_version(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""
		When staged_pkg_root/<name> is a symlink to old dest (containing
		0.2.0), the sign step must replace it with a real directory
		containing ONLY the new 0.3.0, without writing into the actual
		dest and without re-linking old self versions.
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
					compiler_info=CompilerInfo(version="0.27.54", abi=6, commit="unknown"),
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

			# Old version must NOT be visible — smoke root is self-version-isolated.
			old_ver = art_dir / "0.2.0"
			assert not old_ver.exists(), (
				"old version 0.2.0 must not be visible in smoke root"
			)

			# Dest must NOT have been polluted with 0.3.0.
			assert not (dest / "net-tls" / "0.3.0").exists(), (
				"dest must not be polluted with new version before publish"
			)

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
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
					compiler_info=CompilerInfo(version="0.27.55", abi=6, commit="unknown"),
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

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
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
					compiler_info=CompilerInfo(version="0.27.59", abi=6, commit="unknown"),
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

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
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
					compiler_info=CompilerInfo(version="0.27.60", abi=6, commit="unknown"),
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


class TestSmokeRootIncludesFullArtifactSet:
	"""
	Regression: the smoke package root must include the full authenticated
	artifact set (.zdmp + .sig + .provenance.zst + .author-profile) so the
	consumer verifier finds all required siblings.

	Bug: deploy 0.27.94 only copied .zdmp + .sig to the smoke root, causing
	the verifier to fail with 'provenance sidecar required but not found'.
	"""

	@staticmethod
	def _fake_run_creates_dmp(args, **kwargs):
		cmd = args if isinstance(args, list) else [args]
		for i, arg in enumerate(cmd):
			if arg == "--emit-package" and i + 1 < len(cmd):
				Path(cmd[i + 1]).write_bytes(b"fake-dmp")
		import subprocess
		return subprocess.CompletedProcess(cmd, 0, "", "")

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_smoke_root_includes_provenance_and_profile(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		art = Artifact(
			kind="package", name="web-jwt", version="0.1.0",
			description="JWT", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="web.jwt",
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

			# Create a minimal author profile so the full artifact set is produced.
			author_profile = tmpdir_p / "test.author-profile"
			import base64, hashlib as _hl
			fake_pub_raw = b"\x00" * 32
			fake_pub = base64.b64encode(fake_pub_raw).decode()
			fake_kid = "ed25519:" + base64.b64encode(_hl.sha256(fake_pub_raw).digest()).decode()
			author_profile.write_text(json.dumps({
				"format": "author-profile", "version": 0,
				"key": {"algo": "ed25519", "kid": fake_kid, "pubkey": fake_pub},
				"publisher": {"name": "t", "org": "t", "email": "t@t", "url": ""},
				"namespaces": ["web.jwt.*"],
			}))

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
					compiler_info=CompilerInfo(version="0.27.94", abi=6, commit="abc"),
					staged_pkg_root=staged_pkg_root,
					author_profile_path=author_profile,
				)

			# Find the smoke package root used in the smoke call.
			assert len(mock_run.call_args_list) >= 2
			smoke_cmd = mock_run.call_args_list[1][0][0]
			pkg_roots_in_cmd: list[str] = []
			for i, arg in enumerate(smoke_cmd):
				if arg == "--package-root" and i + 1 < len(smoke_cmd):
					pkg_roots_in_cmd.append(smoke_cmd[i + 1])
			assert pkg_roots_in_cmd, "smoke must have --package-root"

			# The smoke root must contain the full authenticated artifact set.
			smoke_root = Path(pkg_roots_in_cmd[0])
			version_dir = smoke_root / "web-jwt" / "0.1.0"
			assert (version_dir / "web-jwt.zdmp").exists() or (version_dir / "web-jwt.dmp").exists(), \
				"smoke root missing artifact"
			assert (version_dir / "web-jwt.provenance.zst").exists(), \
				"smoke root missing provenance bundle — verifier will reject the package"
			assert (version_dir / "web-jwt.author-profile").exists(), \
				"smoke root missing author profile — verifier will reject the package"


class TestSmokeRootExcludesOldSelfVersions:
	"""
	Regression: smoke root must not contain historical self versions from dest.

	Bug: net-tls deploy failed because the smoke root inherited an older
	net-tls version from --dest. That version had a v1 .sig referencing
	an author-profile that wasn't published as a sibling, causing the
	verifier to fail with 'author profile required but not found'.

	The fix: don't re-link old self versions into staged_pkg_root. For a
	package with no deps, smoke should only see the just-built version.
	"""

	@staticmethod
	def _fake_run_creates_dmp(args, **kwargs):
		cmd = args if isinstance(args, list) else [args]
		for i, arg in enumerate(cmd):
			if arg == "--emit-package" and i + 1 < len(cmd):
				Path(cmd[i + 1]).write_bytes(b"fake-dmp")
		import subprocess
		return subprocess.CompletedProcess(cmd, 0, "", "")

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x01" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_old_self_version_not_in_smoke_root(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""Dest has net-tls/0.3.5; deploying net-tls/0.3.6 — smoke must not see 0.3.5."""
		art = Artifact(
			kind="package", name="net-tls", version="0.3.6",
			description="TLS", license="MIT",
			entry_module="src/lib.drift", modules=["src/"],
			module_namespace="net.tls",
		)
		with tempfile.TemporaryDirectory() as tmpdir:
			tmpdir_p = Path(tmpdir)

			# Dest already has an older version with a v1-signed .sig.
			old_ver_dir = tmpdir_p / "dest" / "net-tls" / "0.3.5"
			old_ver_dir.mkdir(parents=True)
			(old_ver_dir / "net-tls.zdmp").write_bytes(b"old-pkg")
			(old_ver_dir / "net-tls.sig").write_bytes(b'{"format":"dmir-pkg-sig","version":0,"package_sha256":"sha256:aa","envelope_version":1,"author_profile_sha256":"sha256:bb","signatures":[]}')
			# NOTE: no .author-profile file — this is the old layout that triggers the failure.

			dest = tmpdir_p / "dest"
			stage_dir = tmpdir_p / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)
			# Mirror dest into staged_pkg_root (as deploy does).
			(staged_pkg_root / "net-tls").symlink_to((dest / "net-tls").resolve())

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
					resolved={},  # no deps
					stage_dir=stage_dir,
					manifest_dir=manifest_dir,
					package_roots=[staged_pkg_root, dest],
					dest=dest,
					app_dest=None,
					sign_key=Path("/fake.key"),
					baseline_trust=None,
					skip_smoke=False,
					dry_run=True,
					compiler_info=CompilerInfo(version="0.27.94", abi=6, commit="abc"),
					staged_pkg_root=staged_pkg_root,
				)

			# The smoke root must NOT contain the old 0.3.5 version.
			smoke_cmd = mock_run.call_args_list[1][0][0]
			pkg_roots_in_cmd: list[str] = []
			for i, arg in enumerate(smoke_cmd):
				if arg == "--package-root" and i + 1 < len(smoke_cmd):
					pkg_roots_in_cmd.append(smoke_cmd[i + 1])

			for pr in pkg_roots_in_cmd:
				tls_dir = Path(pr) / "net-tls"
				if tls_dir.exists():
					versions = [d.name for d in tls_dir.iterdir() if d.is_dir() or d.is_symlink()]
					assert "0.3.5" not in versions, (
						f"smoke root must not contain old self version 0.3.5; found: {versions}"
					)
					assert "0.3.6" in versions, (
						f"smoke root must contain the version being built (0.3.6); found: {versions}"
					)


# ── Intra-project dep resolution ────────────────────────────────────


class TestIntraProjectDeps:
	"""
	When B depends on co-deployed A, resolution must find A's .dmp
	in staged_pkg_root after A has been built (not fail upfront).
	"""

	@patch("tools.drift_deploy.drift_deploy.build_package_index")
	def test_resolve_artifact_deps_uses_lock(
		self, mock_index: MagicMock,
	) -> None:
		"""_resolve_artifact_deps reads deps from existing lock."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		with tempfile.TemporaryDirectory() as tmpdir:
			# Simulate a .dmp for integrity verification.
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
						required_deps=[],
						author_key="ed25519:test_key",
					),
				],
			}
			art = Artifact(
				kind="package", name="net-tls", version="0.1.0",
				description="", license="", entry_module="net.tls",
				modules=["net.tls"], module_namespace="net.tls",
				package_deps=[PackageDep(name="net-crypto", version="0.1")],
			)
			# v3 lock pins the exact version AND sha256 — both must
			# match the on-disk .dmp at verify time.  Use the real
			# sha of the test fixture so `verify_lock_compatibility`
			# accepts it.
			# Mark the lock entry unsigned so the v4 strict-mode
			# verifier skips both halves (no .source-attestation
			# sidecar exists for this synthetic on-disk fixture).
			existing_lock = {
				"net-tls": {
					"net-crypto": ResolvedDep(
						version="0.1.0",
						sha256=sha,
						dep_type="direct",
						package_id="net-crypto",
						author_key="unsigned",
					),
				},
			}
			resolved = _resolve_artifact_deps(
				art,
				package_roots=[Path(tmpdir)],
				lock_path=_drift_subdir(tmpdir) / "lock.json",
				existing_lock=existing_lock,
			)
			assert "net-crypto" in resolved
			assert resolved["net-crypto"].version == "0.1.0"


class TestDeploySourceRebuild:
	"""Phase D pin for the deploy path.  The orch workflow that
	started the source-rebuild track lives here, not in `drift build`:
	deploy resolves per artifact, walks staged_pkg_root + extra
	package roots, handles co-artifacts, and reports byte-drift
	evidence inside `_resolve_artifact_deps`.  Build-only tests would
	miss every regression in that surface."""

	@patch("tools.drift_deploy.drift_deploy.build_package_index")
	def test_deploy_source_rebuild_accepts_sha_drift_with_matching_source_identity(
		self, mock_index: MagicMock, capsys,
	) -> None:
		"""Deploy-level happy path: lock sha != rebuilt sha, but
		source_content_id + source_attestation_key match → accepted,
		byte-drift surfaced to stdout as run evidence.  The rebuilt
		artifact is ALSO signed by a different (rebuilder) key —
		tolerated because the trust root is the source-attestation
		key, not the rebuilt `.dmp`'s `.sig` signer."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib
		with tempfile.TemporaryDirectory() as tmpdir:
			dmp = Path(tmpdir) / "net-crypto.dmp"
			dmp.write_bytes(b"orch-rebuilt-bytes")
			rebuilt_sha = hashlib.sha256(b"orch-rebuilt-bytes").hexdigest()
			locked_sha = hashlib.sha256(b"author-original-bytes").hexdigest()
			assert locked_sha != rebuilt_sha
			matching_scid = "sha256:" + "a"*64
			matching_sak = "ed25519:original-author-src"
			mock_index.return_value = {
				"net-crypto": [PackageEntry(
					package_id="net-crypto",
					version=parse_version("0.1.0"),
					path=dmp,
					sha256=rebuilt_sha,
					required_deps=[],
					author_key="ed25519:rebuilder-not-author",  # tolerated
					source_content_id=matching_scid,
					source_attestation_key=matching_sak,
				)],
			}
			art = Artifact(
				kind="package", name="net-tls", version="0.1.0",
				description="", license="", entry_module="net.tls",
				modules=["net.tls"], module_namespace="net.tls",
				package_deps=[PackageDep(name="net-crypto", version="0.1")],
			)
			existing_lock = {
				"net-tls": {
					"net-crypto": ResolvedDep(
						version="0.1.0",
						sha256=locked_sha,
						dep_type="direct",
						package_id="net-crypto",
						author_key="ed25519:original-author-art",
						source_content_id=matching_scid,
						source_attestation_key=matching_sak,
					),
				},
			}
			from tools.drift_deploy.conftest import PermissiveRunSnapshot
			resolved = _resolve_artifact_deps(
				art,
				package_roots=[Path(tmpdir)],
				lock_path=_drift_subdir(tmpdir) / "lock.json",
				existing_lock=existing_lock,
				source_rebuild=True,
				run_snapshot=PermissiveRunSnapshot(),
			)
			assert "net-crypto" in resolved
			assert resolved["net-crypto"].version == "0.1.0"
			out = capsys.readouterr().out
			# 0.31.1 unified format: one evidence block per artifact
			# produced by `source_rebuild.print_evidence`; sha drift
			# appears as `sha256 'X' -> 'Y'`.
			assert "drift deploy --source-rebuild" in out
			assert "drift vs. lock" in out
			assert "sha256" in out
			assert "net-crypto" in out
			assert locked_sha in out
			assert rebuilt_sha in out

	@patch("tools.drift_deploy.drift_deploy.build_package_index")
	def test_deploy_source_rebuild_accepts_source_identity_drift_as_evidence(
		self, mock_index: MagicMock,
	) -> None:
		"""Policy as of `fix/source-rebuild-trust-anchor` (0.31.1):
		deploy-time source-rebuild accepts source_content_id drift as
		evidence, not a hard failure.  Orch selects source via run-
		all-latest.json; the downstream lock is evidence, not a
		rebuild pin.  Trust comes from the namespace-allowlist check
		at package-index time — per-dep scid equality here would
		stale every downstream lock on every compatible upstream
		patch, violating the Lock-v2 contract."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib
		with tempfile.TemporaryDirectory() as tmpdir:
			dmp = Path(tmpdir) / "net-crypto.dmp"
			dmp.write_bytes(b"rebuilt")
			rebuilt_sha = hashlib.sha256(b"rebuilt").hexdigest()
			locked_sha = hashlib.sha256(b"original").hexdigest()
			mock_index.return_value = {
				"net-crypto": [PackageEntry(
					package_id="net-crypto",
					version=parse_version("0.1.0"),
					path=dmp,
					sha256=rebuilt_sha,
					required_deps=[],
					author_key="ed25519:rebuilder",
					source_content_id="sha256:" + "9"*64,  # ≠ lock (drifted)
					source_attestation_key="ed25519:original-author-src",
				)],
			}
			art = Artifact(
				kind="package", name="net-tls", version="0.1.0",
				description="", license="", entry_module="net.tls",
				modules=["net.tls"], module_namespace="net.tls",
				package_deps=[PackageDep(name="net-crypto", version="0.1")],
			)
			existing_lock = {
				"net-tls": {
					"net-crypto": ResolvedDep(
						version="0.1.0",
						sha256=locked_sha,
						dep_type="direct",
						package_id="net-crypto",
						author_key="ed25519:original-author-art",
						source_content_id="sha256:" + "a"*64,
						source_attestation_key="ed25519:original-author-src",
					),
				},
			}
			# No longer raises — source identity drift is evidence.
			from tools.drift_deploy.conftest import PermissiveRunSnapshot
			resolved = _resolve_artifact_deps(
				art,
				package_roots=[Path(tmpdir)],
				lock_path=_drift_subdir(tmpdir) / "lock.json",
				existing_lock=existing_lock,
				source_rebuild=True,
				run_snapshot=PermissiveRunSnapshot(),
			)
			assert "net-crypto" in resolved
			assert resolved["net-crypto"].version == "0.1.0"

	@patch("tools.drift_deploy.drift_deploy.build_package_index")
	def test_deploy_default_strict_still_rejects_sha_drift_without_flag(
		self, mock_index: MagicMock,
	) -> None:
		"""Regression pin: deploy WITHOUT --source-rebuild must still
		reject sha drift even when source identity matches.  The
		certification opt-in does not become the silent default."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib
		with tempfile.TemporaryDirectory() as tmpdir:
			dmp = Path(tmpdir) / "net-crypto.dmp"
			dmp.write_bytes(b"different-bytes")
			drift_sha = hashlib.sha256(b"different-bytes").hexdigest()
			locked_sha = hashlib.sha256(b"original").hexdigest()
			matching_scid = "sha256:" + "a"*64
			matching_sak = "ed25519:src"
			mock_index.return_value = {
				"net-crypto": [PackageEntry(
					package_id="net-crypto",
					version=parse_version("0.1.0"),
					path=dmp,
					sha256=drift_sha,
					required_deps=[],
					author_key="ed25519:author",
					source_content_id=matching_scid,
					source_attestation_key=matching_sak,
				)],
			}
			art = Artifact(
				kind="package", name="net-tls", version="0.1.0",
				description="", license="", entry_module="net.tls",
				modules=["net.tls"], module_namespace="net.tls",
				package_deps=[PackageDep(name="net-crypto", version="0.1")],
			)
			existing_lock = {
				"net-tls": {
					"net-crypto": ResolvedDep(
						version="0.1.0",
						sha256=locked_sha,
						dep_type="direct",
						package_id="net-crypto",
						author_key="ed25519:author",
						source_content_id=matching_scid,
						source_attestation_key=matching_sak,
					),
				},
			}
			with pytest.raises(DeployError) as exc:
				# NOTE: source_rebuild defaults to False.
				_resolve_artifact_deps(
					art,
					package_roots=[Path(tmpdir)],
					lock_path=_drift_subdir(tmpdir) / "lock.json",
					existing_lock=existing_lock,
				)
			assert "sha256 mismatch" in str(exc.value)


def _deploy_scaffold(tmp_path):
	"""Write the minimum set of files needed for `drift deploy` to
	reach `_resolve_artifact_deps` so tests can pin selector /
	phase behaviour without duplicating scaffolding.

	Returns `(manifest_path, dest, sign_key_path, fake_driftc)`.
	"""
	import base64
	import hashlib as _hl

	manifest_dir = tmp_path / "drift"
	manifest_dir.mkdir()
	manifest_path = manifest_dir / "manifest.json"
	manifest_path.write_text(json.dumps({
		"schema_version": 2,
		"project": {
			"name": "test-proj",
			"license": "MIT",
			"author_profile": "test.author-profile",
		},
		"artifacts": [{
			"kind": "package", "name": "my.pkg", "version": "0.1.0",
			"description": "test", "license": "MIT",
			"entry_module": "lib.drift", "modules": ["lib.drift"],
			"module_namespace": "my.pkg",
			"package_deps": [{"name": "dep.a", "version": "0.1"}],
		}],
	}))
	author_profile = manifest_dir / "test.author-profile"
	fake_pub_raw = b"\x00" * 32
	fake_pub = base64.b64encode(fake_pub_raw).decode()
	fake_kid = "ed25519:" + base64.b64encode(_hl.sha256(fake_pub_raw).digest()).decode()
	author_profile.write_text(json.dumps({
		"format": "author-profile", "version": 0,
		"key": {"algo": "ed25519", "kid": fake_kid, "pubkey": fake_pub},
		"publisher": {"name": "t", "org": "t", "email": "t@t", "url": ""},
		"namespaces": ["my.pkg.*"],
	}))
	sign_key_path = tmp_path / "deploy.key"
	sign_key_path.write_text(base64.b64encode(bytes(range(32))).decode("ascii") + "\n")
	lock_path = manifest_dir / "lock.json"
	lock_path.write_text(json.dumps({
		"schema_version": 4,
		"artifacts": {
			"my.pkg": {
				"resolved": {
					"dep.a": {
						"version": "0.1.3",
						"sha256": "a" * 64,
						"author_key": "ed25519:test",
						"source_content_id": "sha256:" + "a" * 64,
						"source_attestation_key": "ed25519:test",
						"dep_type": "direct",
					},
				},
			},
		},
	}))
	dest = tmp_path / "dest"
	dest.mkdir()
	fake_driftc = tmp_path / "fake-driftc"
	fake_driftc.write_text("#!/bin/sh\necho 'driftc 0.30.1 | abi 10 | git test'\n")
	fake_driftc.chmod(0o755)
	return manifest_path, dest, sign_key_path, fake_driftc


@pytest.mark.usefixtures("permissive_run_snapshot")
class TestDeployCertMode:
	"""0.31.5 redesign: orch certification phase is named explicitly
	via `DRIFT_CERT_MODE=stage|certify`, replacing the brittle
	`DRIFT_SOURCE_REBUILD=1` selector that kept misrouting commands
	across phase boundaries.

	Semantics for `drift deploy`:
	  - unset / `stage`: normal producer path.  No snapshot required.
	    This is what TLS and any other team publishing locally see,
	    and what orch's certification-staging phase does.
	  - `certify`: orch certification lane (consumer).  Snapshot
	    required via `--run-snapshot` or `DRIFT_RUN_SNAPSHOT`.

	`--source-rebuild` CLI flag is a manual synonym for certify
	mode (still requires a snapshot).  The retired
	`DRIFT_SOURCE_REBUILD` env hard-errors with migration guidance."""

	def test_helper_matrix_cert_mode(self, monkeypatch) -> None:
		"""Unit pin for the uniform lane selector:
		  - unset cert_mode, no flag → False
		  - `stage`, no flag → False
		  - `certify`, no flag → True
		  - any cert_mode, --source-rebuild flag → True
		  - invalid cert_mode → CertModeError"""
		import argparse as _ap
		from tools.drift_deploy.build_cmd import CertModeError
		from tools.drift_deploy.drift_deploy import _source_rebuild_enabled
		off = _ap.Namespace(source_rebuild=False)
		on = _ap.Namespace(source_rebuild=True)

		monkeypatch.delenv("DRIFT_SOURCE_REBUILD", raising=False)
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		assert _source_rebuild_enabled(off) is False
		assert _source_rebuild_enabled(on) is True

		monkeypatch.setenv("DRIFT_CERT_MODE", "stage")
		assert _source_rebuild_enabled(off) is False, (
			"DRIFT_CERT_MODE=stage is the producer phase; deploy must "
			"not enter source-rebuild verification"
		)
		assert _source_rebuild_enabled(on) is True

		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		assert _source_rebuild_enabled(off) is True, (
			"DRIFT_CERT_MODE=certify is the consumer phase; deploy must "
			"enter source-rebuild verification"
		)
		assert _source_rebuild_enabled(on) is True

		monkeypatch.setenv("DRIFT_CERT_MODE", "bogus")
		with pytest.raises(CertModeError) as exc:
			_source_rebuild_enabled(off)
		assert "DRIFT_CERT_MODE" in str(exc.value)
		assert "'stage'" in str(exc.value) or "stage" in str(exc.value)

		# Env-validation order: the retired env and invalid
		# `DRIFT_CERT_MODE` must raise even when `--source-rebuild`
		# is also set.  A short-circuit on the flag would hide
		# stale env values in orch / CI shells that happen to pass
		# the flag explicitly, which is exactly the drift the
		# retirement is meant to catch.
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		monkeypatch.setenv("DRIFT_SOURCE_REBUILD", "1")
		with pytest.raises(CertModeError) as exc:
			_source_rebuild_enabled(on)
		assert "DRIFT_SOURCE_REBUILD" in str(exc.value)
		monkeypatch.delenv("DRIFT_SOURCE_REBUILD", raising=False)

		monkeypatch.setenv("DRIFT_CERT_MODE", "verify")  # retired spelling
		with pytest.raises(CertModeError) as exc:
			_source_rebuild_enabled(on)
		assert "DRIFT_CERT_MODE" in str(exc.value)
		assert "'verify'" in str(exc.value)

	def test_legacy_env_var_rejected(self, monkeypatch) -> None:
		"""`DRIFT_SOURCE_REBUILD` is retired in 0.31.5.  Any non-empty
		value must raise with a migration message pointing at
		`DRIFT_CERT_MODE`."""
		import argparse as _ap
		from tools.drift_deploy.build_cmd import CertModeError
		from tools.drift_deploy.drift_deploy import _source_rebuild_enabled

		monkeypatch.setenv("DRIFT_SOURCE_REBUILD", "1")
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		with pytest.raises(CertModeError) as exc:
			_source_rebuild_enabled(_ap.Namespace(source_rebuild=False))
		msg = str(exc.value)
		assert "DRIFT_SOURCE_REBUILD" in msg
		assert "retired" in msg.lower() or "0.31.5" in msg
		assert "DRIFT_CERT_MODE" in msg

	def test_stage_mode_deploy_stays_producer(
		self, monkeypatch, tmp_path,
	) -> None:
		"""Regression #2 from K's redesign: `drift deploy` under
		`DRIFT_CERT_MODE=stage` with no snapshot MUST proceed into
		normal producer mode and NOT fail at the snapshot-required
		gate.  This is the orch stage_packages shape."""
		from unittest.mock import patch as _patch
		from tools.drift_deploy.drift_deploy import DeployError, run
		manifest_path, dest, sign_key_path, fake_driftc = _deploy_scaffold(tmp_path)

		captured: dict = {}
		def _spy(art, *, source_rebuild, run_snapshot, **kwargs):
			captured["source_rebuild"] = source_rebuild
			captured["run_snapshot"] = run_snapshot
			raise DeployError("short-circuit from test spy")

		monkeypatch.setenv("DRIFT_CERT_MODE", "stage")
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		with _patch("tools.drift_deploy.drift_deploy._resolve_artifact_deps", side_effect=_spy):
			rc = run([
				"--manifest", str(manifest_path),
				"--dest", str(dest),
				"--sign-key-file", str(sign_key_path),
				"--driftc", str(fake_driftc),
			])
		assert rc == 1, "DeployError from spy should produce exit 1"
		assert "source_rebuild" in captured, (
			"deploy under DRIFT_CERT_MODE=stage must reach "
			"_resolve_artifact_deps — NOT bail at the pre-resolve "
			"snapshot-required gate.  stage is the producer phase; "
			"the snapshot does not exist yet."
		)
		assert captured["source_rebuild"] is False
		assert captured["run_snapshot"] is None

	def test_certify_mode_without_snapshot_hard_fails(
		self, monkeypatch, tmp_path, capsys,
	) -> None:
		"""Regression #4: `DRIFT_CERT_MODE=certify` without a
		snapshot (neither `DRIFT_RUN_SNAPSHOT` nor `--run-snapshot`)
		fails cleanly with snapshot-required."""
		from tools.drift_deploy.drift_deploy import run
		manifest_path, dest, sign_key_path, fake_driftc = _deploy_scaffold(tmp_path)

		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		rc = run([
			"--manifest", str(manifest_path),
			"--dest", str(dest),
			"--sign-key-file", str(sign_key_path),
			"--driftc", str(fake_driftc),
		])
		assert rc == 1
		err = capsys.readouterr().err
		assert "run snapshot" in err
		assert "DRIFT_RUN_SNAPSHOT" in err or "--run-snapshot" in err

	def test_certify_mode_with_snapshot_enters_source_rebuild(
		self, monkeypatch, tmp_path,
	) -> None:
		"""Regression #3: `DRIFT_CERT_MODE=certify` +
		`DRIFT_RUN_SNAPSHOT` (set by the `permissive_run_snapshot`
		class fixture) engages source-rebuild verification via the
		env form — no CLI flag needed."""
		from unittest.mock import patch as _patch
		from tools.drift_deploy.drift_deploy import DeployError, run
		manifest_path, dest, sign_key_path, fake_driftc = _deploy_scaffold(tmp_path)

		captured: dict = {}
		def _spy(art, *, source_rebuild, run_snapshot, **kwargs):
			captured["source_rebuild"] = source_rebuild
			captured["run_snapshot"] = run_snapshot
			raise DeployError("short-circuit from test spy")

		# `permissive_run_snapshot` already sets DRIFT_RUN_SNAPSHOT
		# to a sentinel and patches load_run_snapshot; we just add
		# DRIFT_CERT_MODE=certify.
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		with _patch("tools.drift_deploy.drift_deploy._resolve_artifact_deps", side_effect=_spy):
			rc = run([
				"--manifest", str(manifest_path),
				"--dest", str(dest),
				"--sign-key-file", str(sign_key_path),
				"--driftc", str(fake_driftc),
			])
		assert rc == 1
		assert captured.get("source_rebuild") is True, (
			f"DRIFT_CERT_MODE=certify + DRIFT_RUN_SNAPSHOT must thread "
			f"source_rebuild=True to _resolve_artifact_deps; got "
			f"{captured.get('source_rebuild')!r}"
		)
		assert captured.get("run_snapshot") is not None

	def test_invalid_cert_mode_fails_clearly(
		self, monkeypatch, tmp_path, capsys,
	) -> None:
		"""Regression #5: an invalid `DRIFT_CERT_MODE` value surfaces
		as a clean error with the allowed values listed."""
		from tools.drift_deploy.drift_deploy import run
		manifest_path, dest, sign_key_path, fake_driftc = _deploy_scaffold(tmp_path)

		monkeypatch.setenv("DRIFT_CERT_MODE", "staging")  # typo
		rc = run([
			"--manifest", str(manifest_path),
			"--dest", str(dest),
			"--sign-key-file", str(sign_key_path),
			"--driftc", str(fake_driftc),
		])
		assert rc == 1
		err = capsys.readouterr().err
		assert "DRIFT_CERT_MODE" in err
		assert "'staging'" in err
		assert "stage" in err and "certify" in err

	def test_retired_env_var_fails_clearly(
		self, monkeypatch, tmp_path, capsys,
	) -> None:
		"""`DRIFT_SOURCE_REBUILD=1` now hard-errors from the CLI path
		with a migration message pointing at `DRIFT_CERT_MODE`."""
		from tools.drift_deploy.drift_deploy import run
		manifest_path, dest, sign_key_path, fake_driftc = _deploy_scaffold(tmp_path)

		monkeypatch.setenv("DRIFT_SOURCE_REBUILD", "1")
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		rc = run([
			"--manifest", str(manifest_path),
			"--dest", str(dest),
			"--sign-key-file", str(sign_key_path),
			"--driftc", str(fake_driftc),
		])
		assert rc == 1
		err = capsys.readouterr().err
		assert "DRIFT_SOURCE_REBUILD" in err
		assert "DRIFT_CERT_MODE" in err

	def test_cli_flag_without_snapshot_hard_fails(
		self, monkeypatch, tmp_path, capsys,
	) -> None:
		"""Explicit `--source-rebuild` without a snapshot still
		hard-fails (same contract whether triggered via flag or
		env): source-rebuild consumer mode always requires a
		snapshot; no silent fallback."""
		from tools.drift_deploy.drift_deploy import run
		manifest_path, dest, sign_key_path, fake_driftc = _deploy_scaffold(tmp_path)

		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		rc = run([
			"--manifest", str(manifest_path),
			"--dest", str(dest),
			"--sign-key-file", str(sign_key_path),
			"--driftc", str(fake_driftc),
			"--source-rebuild",
		])
		assert rc == 1
		err = capsys.readouterr().err
		assert "run snapshot" in err
		assert "DRIFT_RUN_SNAPSHOT" in err or "--run-snapshot" in err

	def test_cli_flag_plus_retired_env_still_rejects(
		self, monkeypatch, tmp_path, capsys,
	) -> None:
		"""Env-validation order regression: `--source-rebuild` must
		NOT short-circuit env parsing.  When `DRIFT_SOURCE_REBUILD`
		is set (retired) in the same shell that passes the CLI
		flag, the retired env must still surface as
		`CertModeError` with the migration message.  This catches
		stale orch / CI envs that happen to also thread the flag
		explicitly during the transition."""
		from tools.drift_deploy.drift_deploy import run
		manifest_path, dest, sign_key_path, fake_driftc = _deploy_scaffold(tmp_path)

		monkeypatch.setenv("DRIFT_SOURCE_REBUILD", "1")
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		rc = run([
			"--manifest", str(manifest_path),
			"--dest", str(dest),
			"--sign-key-file", str(sign_key_path),
			"--driftc", str(fake_driftc),
			"--source-rebuild",
			"--run-snapshot", "/nonexistent.json",
		])
		assert rc == 1
		err = capsys.readouterr().err
		assert "DRIFT_SOURCE_REBUILD" in err
		assert "DRIFT_CERT_MODE" in err

	def test_cli_flag_plus_invalid_cert_mode_still_rejects(
		self, monkeypatch, tmp_path, capsys,
	) -> None:
		"""Env-validation order regression: an invalid
		`DRIFT_CERT_MODE` (including the retired `verify` spelling)
		must still raise when `--source-rebuild` is passed."""
		from tools.drift_deploy.drift_deploy import run
		manifest_path, dest, sign_key_path, fake_driftc = _deploy_scaffold(tmp_path)

		monkeypatch.delenv("DRIFT_SOURCE_REBUILD", raising=False)
		monkeypatch.setenv("DRIFT_CERT_MODE", "verify")  # retired spelling
		rc = run([
			"--manifest", str(manifest_path),
			"--dest", str(dest),
			"--sign-key-file", str(sign_key_path),
			"--driftc", str(fake_driftc),
			"--source-rebuild",
			"--run-snapshot", "/nonexistent.json",
		])
		assert rc == 1
		err = capsys.readouterr().err
		assert "DRIFT_CERT_MODE" in err
		assert "'verify'" in err


# ── Lock-exact passthrough at deploy time ───────────────────────────


class TestDeployLockRangeResolution:
	"""Deploy consumes the lock's exact version as-is; v3 eliminates
	the build-time range→exact step that v2 performed."""

	@patch("tools.drift_deploy.drift_deploy.build_package_index")
	def test_deploy_passes_lock_exact_version_through(
		self, mock_index: MagicMock,
	) -> None:
		"""v3 lock stores an exact version; deploy uses that exact
		version directly (the old v2 "resolve the range to exact"
		step is gone — patch movement is prepare-only)."""
		from tools.drift_deploy.resolver import PackageEntry
		from tools.drift_deploy.semver import parse_version
		import hashlib
		with tempfile.TemporaryDirectory() as tmpdir:
			dmp = Path(tmpdir) / "dep-a.dmp"
			dmp.write_bytes(b"fake-dmp-content")
			real_sha = hashlib.sha256(b"fake-dmp-content").hexdigest()
			mock_index.return_value = {
				"dep-a": [
					PackageEntry(
						package_id="dep-a",
						version=parse_version("0.1.3"),
						path=dmp,
						sha256=real_sha,
						required_deps=[],
						author_key="ed25519:test_key",
					),
				],
			}
			art = Artifact(
				kind="package", name="my-pkg", version="0.1.0",
				description="", license="", entry_module="my/pkg.drift",
				modules=["my/pkg.drift"], module_namespace="my.pkg",
				package_deps=[PackageDep(name="dep-a", version="0.1")],
			)
			# v4 lock: exact version + real sha256.  Mark unsigned
			# so v4 strict-mode verifier skips both halves (the
			# synthetic on-disk fixture has no `.source-attestation`
			# sidecar — this test pins lock-passthrough semantics,
			# not the trust-attestation pipeline).
			existing_lock = {
				"my-pkg": {
					"dep-a": ResolvedDep(
						version="0.1.3",
						sha256=real_sha,
						dep_type="direct",
						package_id="dep-a",
						author_key="unsigned",
					),
				},
			}
			resolved = _resolve_artifact_deps(
				art,
				package_roots=[Path(tmpdir)],
				lock_path=_drift_subdir(tmpdir) / "lock.json",
				existing_lock=existing_lock,
			)
			assert "dep-a" in resolved
			# Exact version passes through from lock to resolved output.
			assert resolved["dep-a"].version == "0.1.3", (
				f"deploy must use lock's exact version 0.1.3; "
				f"got {resolved['dep-a'].version}"
			)


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
			"net-tls": ResolvedDep(version="0.3.1", sha256="aa", dep_type="direct"),
			"acme.crypto": ResolvedDep(version="0.9.0", sha256="bb", dep_type="transitive"),
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

			with patch("tools.drift_deploy.drift_deploy._sign_artifact", return_value=sig_path), \
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
					compiler_info=CompilerInfo(version="0.27.62", abi=6, commit="unknown"),
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

			with patch("tools.drift_deploy.drift_deploy._sign_artifact", return_value=sig_path), \
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
					compiler_info=CompilerInfo(version="0.27.62", abi=6, commit="unknown"),
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


class TestAuthorProfilePublish:
	"""Verify deploy enforces and publishes the manifest-declared author profile.

	These tests exercise the real deploy entry point (run()) so the
	regression protects actual behavior, not a hand-restated copy.
	"""

	def _write_manifest(self, tmpdir: Path, *, author_profile: str | None = None) -> Path:
		"""Write a minimal drift/manifest.json with an optional author_profile."""
		project: dict = {"name": "test", "license": "MIT"}
		if author_profile is not None:
			project["author_profile"] = author_profile
		manifest = {
			"schema_version": 2,
			"project": project,
			"artifacts": [{
				"kind": "package",
				"name": "test.pkg",
				"version": "0.1.0",
				"description": "test",
				"license": "MIT",
				"entry_module": "lib.drift",
				"modules": ["lib.drift"],
			}],
		}
		drift_dir = tmpdir / "drift"
		drift_dir.mkdir(exist_ok=True)
		path = drift_dir / "manifest.json"
		import json as _json
		path.write_text(_json.dumps(manifest))
		return path

	def test_missing_profile_declaration_fails_deploy(self) -> None:
		"""No project.author_profile → deploy fails with clear message."""
		from tools.drift_deploy.drift_deploy import run
		with tempfile.TemporaryDirectory() as tmpdir:
			manifest_path = self._write_manifest(Path(tmpdir))
			dest = Path(tmpdir) / "dest"
			dest.mkdir()
			rc = run([
				"--manifest", str(manifest_path),
				"--dest", str(dest),
			])
			assert rc == 1  # DeployError caught by run()

	def test_declared_but_missing_file_fails_deploy(self) -> None:
		"""project.author_profile declared but file doesn't exist → deploy fails."""
		from tools.drift_deploy.drift_deploy import run
		with tempfile.TemporaryDirectory() as tmpdir:
			manifest_path = self._write_manifest(
				Path(tmpdir), author_profile="missing.author-profile",
			)
			dest = Path(tmpdir) / "dest"
			dest.mkdir()
			rc = run([
				"--manifest", str(manifest_path),
				"--dest", str(dest),
			])
			assert rc == 1  # DeployError: file not found

	def test_sign_artifact_with_profile_produces_bound_sidecar(self) -> None:
		"""_sign_artifact with author_profile_path produces envelope_version: 1."""
		from tools.drift_deploy.drift_deploy import _sign_artifact
		from lang.drift.author_profile import create_author_profile, write_author_profile
		from dataclasses import replace as _dc_replace

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)
			# Create a fake .dmp.
			dmp = td / "test.pkg.dmp"
			dmp.write_bytes(b"fake package bytes")
			# Create a key.
			import base64 as _b64, os as _os
			key_path = td / "key.seed"
			seed = _os.urandom(32)
			key_path.write_text(_b64.b64encode(seed).decode() + "\n")
			# Create a bound author profile.
			from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
			from lang.drift.crypto import ed25519_public_bytes_raw
			priv = Ed25519PrivateKey.from_private_bytes(seed)
			pub_raw = ed25519_public_bytes_raw(priv.public_key())
			profile = create_author_profile(pubkey_raw=pub_raw, name="Test", namespaces=["test.*"])
			bound = _dc_replace(profile, package="test.pkg")
			staged_profile = td / "test.pkg.author-profile"
			write_author_profile(bound, staged_profile)

			sig_path = _sign_artifact(dmp, sign_key=key_path, author_profile_path=staged_profile)

			import json as _json
			sc = _json.loads(sig_path.read_text())
			assert sc["envelope_version"] == 1
			assert "author_profile_sha256" in sc
			from lang.drift.crypto import sha256_hex
			assert sc["author_profile_sha256"] == f"sha256:{sha256_hex(staged_profile.read_bytes())}"

			# Verify the staged profile has the package field.
			from lang.drift.author_profile import load_author_profile
			loaded = load_author_profile(staged_profile)
			assert loaded.package == "test.pkg"

	def test_sign_artifact_without_profile_is_legacy(self) -> None:
		"""_sign_artifact without author_profile_path produces legacy v0 sidecar."""
		from tools.drift_deploy.drift_deploy import _sign_artifact

		with tempfile.TemporaryDirectory() as tmpdir:
			td = Path(tmpdir)
			dmp = td / "test.pkg.dmp"
			dmp.write_bytes(b"fake package bytes")
			import base64 as _b64, os as _os
			key_path = td / "key.seed"
			key_path.write_text(_b64.b64encode(_os.urandom(32)).decode() + "\n")

			sig_path = _sign_artifact(dmp, sign_key=key_path)

			import json as _json
			sc = _json.loads(sig_path.read_text())
			assert "envelope_version" not in sc
			assert "author_profile_sha256" not in sc


class TestDeployPexEntry:
	"""Verify the drift-deploy PEX entry point is importable and well-formed."""

	def test_entry_point_importable(self) -> None:
		"""deploy_pex_entry.main must be importable from the repo tree."""
		import importlib.util
		entry_path = Path(__file__).resolve().parents[2] / "tools" / "deploy" / "deploy_pex_entry.py"
		assert entry_path.exists(), f"entry point not found: {entry_path}"
		spec = importlib.util.spec_from_file_location("deploy_pex_entry", entry_path)
		assert spec is not None
		mod = importlib.util.module_from_spec(spec)
		# Don't execute — just verify the module loads and has main().
		spec.loader.exec_module(mod)  # type: ignore[union-attr]
		assert hasattr(mod, "main"), "deploy_pex_entry must define main()"
		assert callable(mod.main)

	def test_cli_deploy_subcommand(self) -> None:
		"""'drift deploy --help' works through lang.drift.cli dispatch."""
		from lang.drift.cli import main as cli_main
		with pytest.raises(SystemExit) as exc_info:
			cli_main(["deploy", "--help"])
		assert exc_info.value.code == 0

	def test_pex_step_module_exists(self) -> None:
		"""steps/pex.py must exist and export build_drift_pex."""
		mod_path = Path(__file__).resolve().parents[2] / "tools" / "deploy" / "steps" / "pex.py"
		assert mod_path.exists(), f"pex step module not found: {mod_path}"
		from tools.deploy.steps.pex import build_drift_pex
		assert callable(build_drift_pex)


class TestLoadSigningKeySeed:
	"""Pin the canonical 32-byte Ed25519 seed loader.  Decode goes
	through `lang.drift.crypto.b64_decode` (validate=True), so
	embedded non-base64 characters that Python's lax decoder would
	silently drop must be rejected here too — otherwise a corrupted
	key file would "decode" to a different seed than the producer
	intended.
	"""

	def test_canonical_base64_seed_loads(self) -> None:
		import base64
		from tools.drift_deploy.staged_trust import load_signing_key_seed
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "ok.key"
			p.write_text(base64.b64encode(bytes(range(32))).decode("ascii") + "\n")
			seed = load_signing_key_seed(p)
			assert seed == bytes(range(32))

	def test_raw_bytes_seed_rejected(self) -> None:
		"""Raw 32 bytes (not base64 text) → invalid base64."""
		from tools.drift_deploy.staged_trust import load_signing_key_seed
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "raw.key"
			p.write_bytes(bytes(range(32)))
			with pytest.raises(ValueError, match="signing key"):
				load_signing_key_seed(p)

	def test_embedded_non_base64_chars_rejected(self) -> None:
		"""Reviewer's regression: a key file with non-base64 garbage
		mixed in must be rejected, not silently stripped.  Python's
		default `base64.b64decode(text)` would accept and discard the
		`!` characters, producing a different seed than the producer
		intended.  `lang.drift.crypto.b64_decode` uses `validate=True`,
		which our loader must inherit."""
		from tools.drift_deploy.staged_trust import load_signing_key_seed
		import base64
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "mangled.key"
			# Take a real base64-encoded 32-byte seed, then splice in
			# `!` characters (not in the base64 alphabet).
			real = base64.b64encode(bytes(range(32))).decode("ascii")
			mangled = real[:8] + "!!!!" + real[8:]
			p.write_text(mangled)
			with pytest.raises(ValueError, match="signing key"):
				load_signing_key_seed(p)

	def test_wrong_decoded_length_rejected(self) -> None:
		"""Valid base64 but wrong byte length (not 32)."""
		from tools.drift_deploy.staged_trust import load_signing_key_seed
		import base64
		with tempfile.TemporaryDirectory() as tmpdir:
			p = Path(tmpdir) / "short.key"
			p.write_text(base64.b64encode(b"\x00" * 16).decode("ascii"))
			with pytest.raises(ValueError, match="32 bytes"):
				load_signing_key_seed(p)

	def test_missing_file_rejected(self) -> None:
		from tools.drift_deploy.staged_trust import load_signing_key_seed
		with pytest.raises(ValueError, match="unreadable"):
			load_signing_key_seed(Path("/nonexistent/key/path"))


class TestSourceAttestationEmission:
	"""Phase A.1 integration pin: a real signed library deploy emits the
	`.source-attestation` sidecar alongside `.zdmp` + `.sig`, with the
	body's `source_content_id` matching the value driftc was asked to
	stamp into the .dmp manifest, and `required_deps` mirroring the
	authored manifest `package_deps`.

	This is the contract the orchestrator-side trust model rests on:
	a downstream rebuild has a verifiable, author-signed source
	identity to compare against, independent of the rebuilt .dmp's
	exact bytes.

	Findings 1 + 3 from Phase A review: validates that the canonical
	base64-encoded ed25519 seed file format works end-to-end (the
	previous implementation read raw bytes and would have rejected
	every real deploy key), and that the integration contract
	is verified — not just the pure attestation primitives.
	"""

	@staticmethod
	def _fake_run_records_cmd(captured: list[list[str]]):
		"""subprocess.run replacement that records each invocation's
		argv (so the test can introspect the --source-content-id flag
		driftc was asked to stamp) and creates the expected .dmp
		output file."""
		def _impl(args, **kwargs):
			cmd = list(args) if isinstance(args, list) else [args]
			captured.append(cmd)
			for i, arg in enumerate(cmd):
				if arg == "--emit-package" and i + 1 < len(cmd):
					out_path = Path(cmd[i + 1])
					out_path.parent.mkdir(parents=True, exist_ok=True)
					out_path.write_bytes(b"fake-dmp")
			import subprocess
			return subprocess.CompletedProcess(cmd, 0, "", "")
		return _impl

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed")
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_signed_library_deploy_emits_source_attestation(
		self, _mock_trust: MagicMock,
		mock_pubkey: MagicMock,
		mock_sign: MagicMock,
	) -> None:
		"""End-to-end: real seed file (base64), real source files, real
		attestation signing.  Asserts:
		  - <name>.zdmp + <name>.sig + <name>.source-attestation all
		    present in smoke staging.
		  - sidecar.body.source_content_id == --source-content-id
		    flag driftc was asked to stamp.
		  - sidecar.body.required_deps == authored package_deps.
		  - sidecar.body.target_class == build target.
		  - sidecar signature verifies against the loaded seed's pubkey.
		"""
		import base64
		from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
		from cryptography.hazmat.primitives import serialization

		from tools.drift_deploy.source_attestation import (
			read_attestation_sidecar,
			verify_attestation,
			_ed25519_kid,
		)

		# Real Ed25519 seed in canonical base64 file format (the format
		# every existing Drift signing surface uses; finding 1 was that
		# attestation signing read raw bytes and rejected this).
		seed_raw = bytes(range(32))
		priv = Ed25519PrivateKey.from_private_bytes(seed_raw)
		pub = priv.public_key().public_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PublicFormat.Raw,
		)
		expected_kid = _ed25519_kid(pub)
		mock_pubkey.return_value = pub

		art = Artifact(
			kind="package", name="net-tls", version="0.4.0",
			description="TLS", license="MIT",
			entry_module="src/lib.drift",
			modules=["src/lib.drift", "src/handshake.drift"],
			package_deps=[
				PackageDep(name="drift-core", version="0.27"),
				PackageDep(name="drift-net", version="0.4"),
			],
			module_namespace="net_tls",
		)

		with tempfile.TemporaryDirectory() as tmpdir:
			tmpdir_p = Path(tmpdir)

			dest = tmpdir_p / "dest"
			dest.mkdir()
			stage_dir = tmpdir_p / "staging"
			staged_pkg_root = stage_dir / "_pkg_root"
			staged_pkg_root.mkdir(parents=True)

			# Real source files at the paths declared in modules[].
			# Without these, source_content_id computation would fail
			# over to None (Phase A graceful path) and no attestation
			# would be emitted — exactly what the test must rule out.
			manifest_dir = tmpdir_p / "src"
			manifest_dir.mkdir()
			(manifest_dir / "src").mkdir()
			(manifest_dir / "src" / "lib.drift").write_text("module lib;\n")
			(manifest_dir / "src" / "handshake.drift").write_text("module handshake;\n")

			# Real base64 sign-key file (the format _sign_artifact and
			# extract_pubkey_from_seed both expect; the previous Phase
			# A wiring read raw bytes here and would have thrown on
			# every real key file).
			sign_key_path = tmpdir_p / "deploy.key"
			sign_key_path.write_text(base64.b64encode(seed_raw).decode("ascii") + "\n")

			# author-profile is required by the signing block.
			author_profile = tmpdir_p / "test.author-profile"
			fake_kid_for_profile = "ed25519:" + base64.b64encode(
				__import__("hashlib").sha256(pub).digest()
			).decode()
			author_profile.write_text(json.dumps({
				"format": "author-profile", "version": 0,
				"key": {
					"algo": "ed25519",
					"kid": fake_kid_for_profile,
					"pubkey": base64.b64encode(pub).decode(),
				},
				"publisher": {"name": "t", "org": "t", "email": "t@t", "url": ""},
				"namespaces": ["net_tls.*"],
			}))

			# _sign_artifact is mocked (artifact-byte signing is not
			# under test here; attestation signing IS — and runs
			# through the real seed loader).
			fake_sig = stage_dir / "fake.sig"
			fake_sig.parent.mkdir(parents=True, exist_ok=True)
			fake_sig.write_bytes(b"fake-sig")
			mock_sign.return_value = fake_sig

			captured_cmds: list[list[str]] = []
			with patch("tools.drift_deploy.drift_deploy.subprocess.run",
					side_effect=self._fake_run_records_cmd(captured_cmds)):
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
					sign_key=sign_key_path,
					baseline_trust=None,
					skip_smoke=True,  # don't need a real driftc smoke run
					dry_run=True,
					compiler_info=CompilerInfo(version="0.30.0", abi=10, commit="abc"),
					staged_pkg_root=staged_pkg_root,
					author_profile_path=author_profile,
				)

			# ── Cross-check 1: --source-content-id flag was passed to
			# driftc with a strict-shape value (the .dmp manifest stamp).
			build_cmd = captured_cmds[0]
			assert "--source-content-id" in build_cmd, (
				"driftc was not invoked with --source-content-id; the .dmp "
				"manifest stamp would be missing"
			)
			scid_idx = build_cmd.index("--source-content-id")
			stamped_scid = build_cmd[scid_idx + 1]
			import re as _re
			assert _re.fullmatch(r"sha256:[0-9a-f]{64}", stamped_scid), (
				f"--source-content-id value is not strict-shape: {stamped_scid!r}"
			)

			# ── Cross-check 2: smoke staging contains all three files.
			smoke_root = staged_pkg_root / "net-tls" / "0.4.0"
			zdmp_path = smoke_root / "net-tls.zdmp"
			sig_sidecar = smoke_root / "fake.sig"  # mocked sign returned this name
			attestation_path = smoke_root / "net-tls.source-attestation"
			assert zdmp_path.exists(), "smoke root missing .zdmp"
			assert sig_sidecar.exists(), "smoke root missing .sig"
			assert attestation_path.exists(), (
				"smoke root missing .source-attestation — Phase A producer "
				"contract regressed"
			)

			# ── Cross-check 3: sidecar body fields.
			sidecar = read_attestation_sidecar(attestation_path)
			assert sidecar.body.package_id == "net-tls"
			assert sidecar.body.version == "0.4.0"
			assert sidecar.body.target_class == "drift-dev"
			assert sidecar.body.source_content_id == stamped_scid, (
				f"sidecar source_content_id ({sidecar.body.source_content_id}) "
				f"does not match .dmp stamp ({stamped_scid}) — orchestrator "
				f"would not be able to bind rebuilt artifact to authored source"
			)
			# required_deps must mirror authored manifest package_deps.
			assert {(d.name, d.version) for d in sidecar.body.required_deps} == {
				("drift-core", "0.27"),
				("drift-net", "0.4"),
			}, "sidecar required_deps must equal authored package_deps verbatim"

			# ── Cross-check 4: signature verifies against the seed's pubkey.
			verify_attestation(sidecar, expected_signer_kid=expected_kid)

	@patch("tools.drift_deploy.drift_deploy._sign_artifact")
	@patch("tools.drift_deploy.staged_trust.extract_pubkey_from_seed", return_value=b"\x00" * 32)
	@patch("tools.drift_deploy.staged_trust.build_staged_trust")
	def test_raw_byte_keyfile_rejected_with_clear_diagnostic(
		self, _mock_trust: MagicMock, _mock_pubkey: MagicMock, mock_sign: MagicMock,
	) -> None:
		"""Finding 1 regression: a raw-bytes (32-byte non-base64) key
		file must produce a clear DeployError, not a silent skip or a
		cryptography-internal traceback."""
		art = Artifact(
			kind="package", name="net-tls", version="0.4.0",
			description="TLS", license="MIT",
			entry_module="src/lib.drift",
			modules=["src/lib.drift"],
			module_namespace="net_tls",
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

			# Raw 32-byte file (NOT base64) — the legacy buggy format
			# Phase A briefly accepted.
			bad_key = tmpdir_p / "raw.key"
			bad_key.write_bytes(bytes(range(32)))

			fake_sig = stage_dir / "fake.sig"
			fake_sig.parent.mkdir(parents=True, exist_ok=True)
			fake_sig.write_bytes(b"fake-sig")
			mock_sign.return_value = fake_sig

			def _fake_run(args, **kwargs):
				cmd = list(args) if isinstance(args, list) else [args]
				for i, a in enumerate(cmd):
					if a == "--emit-package" and i + 1 < len(cmd):
						Path(cmd[i + 1]).write_bytes(b"fake-dmp")
				import subprocess
				return subprocess.CompletedProcess(cmd, 0, "", "")

			with patch("tools.drift_deploy.drift_deploy.subprocess.run", side_effect=_fake_run):
				with pytest.raises(DeployError, match="signing key"):
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
						sign_key=bad_key,
						baseline_trust=None,
						skip_smoke=True,
						dry_run=True,
						compiler_info=CompilerInfo(version="0.30.0", abi=10, commit="abc"),
						staged_pkg_root=staged_pkg_root,
					)
