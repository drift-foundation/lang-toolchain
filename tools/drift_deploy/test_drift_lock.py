# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for `drift lock emit` (landed 0.31.7).

Supported contract for test runners / ad-hoc compile paths that need
the resolved dependency graph:

    DEP_FLAGS=$(drift lock emit --artifact <name>)
    driftc $DEP_FLAGS --package-root <lib> tests/foo.drift -o build/foo

Tests pin:
  - happy path emits sorted, space-separated `--dep` flags on stdout
  - multi-artifact manifest emits only the requested artifact's graph
  - empty resolved graph emits empty line (still exit 0)
  - missing lock, missing artifact, unknown artifact all exit 1 with
    actionable error text pointing at `drift prepare`
  - CLI dispatch via `drift lock emit` reaches the subcommand
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest


def _write_manifest(tmp_path: Path, artifacts: list[dict]) -> Path:
	manifest_dir = tmp_path / "drift"
	manifest_dir.mkdir(exist_ok=True)
	p = manifest_dir / "manifest.json"
	p.write_text(json.dumps({
		"schema_version": 2,
		"project": {
			"name": "test-proj",
			"license": "MIT",
			"author_profile": "test.author-profile",
		},
		"artifacts": artifacts,
	}))
	return p


def _write_lock(tmp_path: Path, artifacts_resolved: dict[str, dict[str, dict]]) -> Path:
	"""`artifacts_resolved` is {artifact_name → {pkg_id → {version, ...}}}."""
	manifest_dir = tmp_path / "drift"
	manifest_dir.mkdir(exist_ok=True)
	p = manifest_dir / "lock.json"
	p.write_text(json.dumps({
		"schema_version": 4,
		"artifacts": {
			a_name: {"resolved": {
				pid: {
					"version": entry.get("version", "1.0.0"),
					"sha256": entry.get("sha256", "a" * 64),
					"author_key": entry.get("author_key", "ed25519:test"),
					"source_content_id": entry.get("source_content_id", "sha256:" + "a" * 64),
					"source_attestation_key": entry.get("source_attestation_key", "ed25519:test"),
					"dep_type": entry.get("dep_type", "direct"),
				}
				for pid, entry in resolved.items()
			}}
			for a_name, resolved in artifacts_resolved.items()
		},
	}))
	return p


def _library_artifact(name: str, version: str, deps: list[tuple[str, str]]) -> dict:
	return {
		"kind": "library", "name": name, "version": version,
		"description": "test", "license": "MIT",
		"entry_module": "src/lib.drift", "modules": ["src/lib.drift"],
		"module_namespace": name.replace("-", "_"),
		"package_deps": [{"name": n, "version": v} for n, v in deps],
	}


class TestLockEmitHappyPath:
	def test_emits_sorted_dep_flags(self, tmp_path, capsys) -> None:
		"""Basic: lock with two resolved deps → sorted `--dep PKG@M.N.P`
		pairs on stdout, space-separated, trailing newline."""
		from tools.drift_deploy.drift_lock import run

		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("singular", "0.2.0", [
				("mariadb-rpc", "0.3"),
				("mariadb-wire-proto", "0.3"),
			]),
		])
		_write_lock(tmp_path, {
			"singular": {
				"mariadb-rpc": {"version": "0.3.0"},
				"mariadb-wire-proto": {"version": "0.3.0"},
			},
		})

		rc = run(["--manifest", str(manifest_path), "--artifact", "singular"])
		assert rc == 0
		out = capsys.readouterr().out
		# Sorted by pkg id → mariadb-rpc first, mariadb-wire-proto second.
		assert out.strip() == "--dep mariadb-rpc@0.3.0 --dep mariadb-wire-proto@0.3.0"

	def test_multi_artifact_manifest_emits_only_named_graph(
		self, tmp_path, capsys,
	) -> None:
		"""Multi-artifact manifest: emit flags only for the named
		artifact's resolved graph, not the other artifacts."""
		from tools.drift_deploy.drift_lock import run

		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("singular", "0.2.0", [("mariadb-rpc", "0.3")]),
			_library_artifact("another-lib", "0.1.0", [("net-tls", "0.4")]),
		])
		_write_lock(tmp_path, {
			"singular": {"mariadb-rpc": {"version": "0.3.0"}},
			"another-lib": {"net-tls": {"version": "0.4.1"}},
		})

		rc = run(["--manifest", str(manifest_path), "--artifact", "singular"])
		assert rc == 0
		out = capsys.readouterr().out
		assert "mariadb-rpc@0.3.0" in out
		assert "net-tls" not in out, (
			"multi-artifact emit must not leak the other artifact's deps"
		)

	def test_empty_resolved_graph_emits_empty(self, tmp_path, capsys) -> None:
		"""Artifact with no package_deps → empty resolved graph →
		empty stdout line, still exit 0.  This is what the caller's
		`$DEP_FLAGS` shell expansion expects for leaf artifacts."""
		from tools.drift_deploy.drift_lock import run

		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("leaf", "0.1.0", []),
		])
		_write_lock(tmp_path, {"leaf": {}})

		rc = run(["--manifest", str(manifest_path), "--artifact", "leaf"])
		assert rc == 0
		assert capsys.readouterr().out == "\n", (
			"empty graph emits just a newline; no `--dep` flags"
		)

	def test_co_artifacts_are_emitted(self, tmp_path, capsys) -> None:
		"""Contract pin (0.31.7): `drift lock emit` emits EVERY
		resolved entry, including entries with
		`dep_type: "co-artifact"` (peer library artifacts in the
		same manifest).  This matches the flag list `drift build`
		passes to `driftc` internally — no filtering, no special-
		casing.  If this test breaks and the emission behaviour was
		intentionally changed, the module docstring, CLI
		description, and history entry all need updating
		simultaneously.

		Caller responsibility note (documented in the module
		header): the runner must ensure co-artifact packages are
		visible under its `--package-root` — typically by building
		them first, or by running after `drift deploy` publishes
		them to a shared `lib/` tree.  Single-artifact libraries
		(the common case) never have co-artifact entries and don't
		hit this."""
		from tools.drift_deploy.drift_lock import run

		# Two-artifact manifest: web-client depends on or-throw-probe.
		# or-throw-probe is a co-artifact from web-client's
		# perspective (same manifest, built in the same deploy run).
		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("or-throw-probe", "0.0.1", []),
			_library_artifact("web-client", "0.3.1", [
				("or-throw-probe", "0.0"),
				("net-tls", "0.4"),
			]),
		])
		_write_lock(tmp_path, {
			"web-client": {
				# External dep — real sha + signer.
				"net-tls": {
					"version": "0.4.1",
					"dep_type": "direct",
				},
				# Co-artifact — empty sha/signer in the on-disk lock
				# (signing happens later in the same deploy run).
				# The v4 reader accepts empty fields iff dep_type is
				# "co-artifact".
				"or-throw-probe": {
					"version": "0.0.1",
					"sha256": "",
					"author_key": "",
					"source_content_id": "",
					"source_attestation_key": "",
					"dep_type": "co-artifact",
				},
			},
		})

		rc = run([
			"--manifest", str(manifest_path),
			"--artifact", "web-client",
		])
		assert rc == 0
		out = capsys.readouterr().out.strip()
		# Both the external dep AND the co-artifact MUST be in the
		# output.  Sorted: net-tls < or-throw-probe.
		assert out == "--dep net-tls@0.4.1 --dep or-throw-probe@0.0.1", (
			f"co-artifact must be emitted alongside external deps "
			f"(drift-build parity contract); got: {out!r}"
		)

	def test_sort_order_is_deterministic(self, tmp_path, capsys) -> None:
		"""Multiple deps must emit in sorted pkg_id order regardless of
		authored or resolved dict iteration order.  Runners that hash
		the output for cache keys rely on this."""
		from tools.drift_deploy.drift_lock import run

		# Deliberately unsorted authored order.
		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("app", "0.1.0", [
				("zeta", "1.0"), ("alpha", "2.0"), ("mid", "1.5"),
			]),
		])
		_write_lock(tmp_path, {
			"app": {
				"zeta": {"version": "1.0.0"},
				"alpha": {"version": "2.0.0"},
				"mid": {"version": "1.5.0"},
			},
		})

		rc = run(["--manifest", str(manifest_path), "--artifact", "app"])
		assert rc == 0
		out = capsys.readouterr().out.strip()
		# alpha < mid < zeta.
		assert out == "--dep alpha@2.0.0 --dep mid@1.5.0 --dep zeta@1.0.0"


class TestLockEmitErrors:
	def test_missing_lock_exits_1_with_prepare_pointer(
		self, tmp_path, capsys,
	) -> None:
		"""Manifest exists but lock missing → exit 1, error text
		naming the missing path AND pointing at `drift prepare`."""
		from tools.drift_deploy.drift_lock import run

		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("singular", "0.2.0", []),
		])
		# No lock written.

		rc = run(["--manifest", str(manifest_path), "--artifact", "singular"])
		assert rc == 1
		err = capsys.readouterr().err
		assert "lock.json" in err
		assert "drift prepare" in err

	def test_missing_manifest_exits_1(self, tmp_path, capsys) -> None:
		"""No manifest at the given path → exit 1 with the path in
		the message.  Standard pre-resolve failure mode."""
		from tools.drift_deploy.drift_lock import run

		rc = run([
			"--manifest", str(tmp_path / "drift" / "manifest.json"),
			"--artifact", "singular",
		])
		assert rc == 1
		err = capsys.readouterr().err
		assert "manifest" in err

	def test_unknown_artifact_exits_1_lists_known(
		self, tmp_path, capsys,
	) -> None:
		"""Manifest loads but the named artifact isn't declared → exit
		1 with the known artifact names listed so the caller can see
		what was available."""
		from tools.drift_deploy.drift_lock import run

		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("singular", "0.2.0", []),
			_library_artifact("other", "0.1.0", []),
		])
		_write_lock(tmp_path, {"singular": {}, "other": {}})

		rc = run(["--manifest", str(manifest_path), "--artifact", "ghost"])
		assert rc == 1
		err = capsys.readouterr().err
		assert "'ghost'" in err
		assert "singular" in err and "other" in err

	def test_artifact_not_in_lock_exits_1_prepare_pointer(
		self, tmp_path, capsys,
	) -> None:
		"""Manifest declares the artifact but the lock doesn't have an
		entry for it → stale lock.  Exit 1, point at `drift prepare`
		with the "stale" framing so the caller knows the fix is
		regenerate, not edit."""
		from tools.drift_deploy.drift_lock import run

		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("singular", "0.2.0", [("dep", "1.0")]),
			_library_artifact("newer", "0.1.0", [("dep", "1.0")]),
		])
		# Lock has singular but not newer — simulates a stale lock
		# where the manifest was edited but prepare wasn't re-run.
		_write_lock(tmp_path, {
			"singular": {"dep": {"version": "1.0.0"}},
		})

		rc = run(["--manifest", str(manifest_path), "--artifact", "newer"])
		assert rc == 1
		err = capsys.readouterr().err
		assert "'newer'" in err
		assert "drift prepare" in err
		assert "stale" in err.lower() or "refresh" in err.lower()

	def test_v1_v2_v3_lock_rejected(self, tmp_path, capsys) -> None:
		"""Old-schema lock → read_lock raises; emit surfaces the
		message with the standard `drift prepare` guidance from
		the lockfile module."""
		from tools.drift_deploy.drift_lock import run

		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("singular", "0.2.0", []),
		])
		# Hand-write a v2 lock.
		(tmp_path / "drift" / "lock.json").write_text(json.dumps({
			"schema_version": 2,
			"artifacts": {},
		}))

		rc = run(["--manifest", str(manifest_path), "--artifact", "singular"])
		assert rc == 1
		err = capsys.readouterr().err
		assert "v4" in err or "v2" in err
		assert "drift prepare" in err


class TestLockEmitCLIDispatch:
	def test_drift_lock_emit_via_top_level_cli(
		self, tmp_path, monkeypatch, capsys,
	) -> None:
		"""Smoke test for the dispatch wiring in `lang/drift/cli.py::
		main`: `drift lock emit --artifact X` reaches the subcommand
		runner."""
		from lang.drift.cli import main

		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("singular", "0.2.0", [("dep", "1.0")]),
		])
		_write_lock(tmp_path, {
			"singular": {"dep": {"version": "1.0.0"}},
		})

		rc = main([
			"lock", "emit",
			"--manifest", str(manifest_path),
			"--artifact", "singular",
		])
		assert rc == 0
		assert "--dep dep@1.0.0" in capsys.readouterr().out

	def test_drift_lock_bare_prints_subcommand_help(
		self, capsys,
	) -> None:
		"""`drift lock` with no subcommand prints a brief help listing
		known subcommands and exits non-zero (mirroring the
		`drift manifest` dispatcher shape)."""
		from lang.drift.cli import main

		rc = main(["lock"])
		assert rc != 0
		err = capsys.readouterr().err
		assert "emit" in err

	def test_drift_lock_unknown_subcommand_errors_cleanly(
		self, capsys,
	) -> None:
		"""`drift lock bogus` prints a clear error and exits 1 without
		confusing the user with argparse internals."""
		from lang.drift.cli import main

		rc = main(["lock", "bogus"])
		assert rc == 1
		err = capsys.readouterr().err
		assert "bogus" in err
		assert "emit" in err
