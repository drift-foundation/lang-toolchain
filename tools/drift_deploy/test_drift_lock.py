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
		"kind": "package", "name": name, "version": version,
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


# ── `drift lock emit --source-rebuild` (0.33.92) ─────────────────────
# The consumer cert-gate contract (drift-workflows / build-orchestrator
# 2026-07-31 ask): gates exec the toolchain binary for source-rebuild
# dep derivation instead of sys.path-importing a drift-lang source
# checkout.  Pinned here:
#   * stdout IS the flags contract: exactly the --dep list from the
#     authority's fresh graph; evidence/diagnostics on stderr only
#   * missing snapshot (flag AND env) → exit 1, empty stdout
#   * DRIFT_RUN_SNAPSHOT env honoured; CLI flag wins
#   * authority errors / snapshot-mismatch index failure → exit 1,
#     EMPTY stdout
#   * strict lane inert under DRIFT_CERT_MODE=certify (env alone never
#     flips lock emit — unlike build/deploy/prepare)
#   * --run-snapshot / --package-root without --source-rebuild rejected
#   * --json v0 shape in both lanes


def _sr_world(tmp_path, *, lock_version: str = "1.0.1"):
	"""Manifest (ext.lib range '1.0'), lock pinning `lock_version`,
	disk pool with ext.lib-1.0.1.dmp, snapshot authorising 1.0.1.
	Returns (manifest_path, pkg_root, snapshot_path, patch_ctx)."""
	from types import SimpleNamespace
	from unittest.mock import patch as _patch
	from contextlib import ExitStack
	from tools.drift_deploy.run_snapshot import (
		SnapshotEntry,
		write_run_snapshot,
	)

	manifest_path = _write_manifest(tmp_path, [
		_library_artifact("my.pkg", "0.1.0", [("ext.lib", "1.0")]),
	])
	scid = "sha256:" + "a" * 64
	ak = "ed25519:orch-sig-kid"
	sak = "ed25519:orch-sak-kid"
	_write_lock(tmp_path, {
		"my.pkg": {
			"ext.lib": {
				"version": lock_version,
				"sha256": "lock-bytes",
				"author_key": ak,
				"source_content_id": scid,
				"source_attestation_key": sak,
			},
		},
	})
	pkg_root = tmp_path / "pkg_root"
	pkg_root.mkdir()
	(pkg_root / "ext.lib-1.0.1.dmp").write_bytes(b"fake-ext-lib-1.0.1")
	(pkg_root / "ext.lib-1.0.1.sig").write_text("{}")
	snapshot_path = tmp_path / "run-snapshot.json"
	write_run_snapshot(
		snapshot_path,
		run_id="20260731-lock-emit-test",
		entries={
			("ext.lib", "1.0.1"): SnapshotEntry(
				source_content_id=scid,
				author_key=ak,
				source_attestation_key=sak,
			),
		},
	)
	disk_manifest = {
		"package_id": "ext.lib",
		"package_version": "1.0.1",
		"modules": [{"module_id": "ext_lib"}],
		"required_deps": [],
	}
	stack = ExitStack()
	stack.enter_context(_patch(
		"tools.drift_deploy.resolver._read_author_key", return_value=ak))
	stack.enter_context(_patch(
		"tools.drift_deploy.resolver._read_source_attestation_meta",
		return_value=(scid, sak)))
	stack.enter_context(_patch(
		"lang.driftc.packages.dmir_pkg_v0.load_dmir_pkg_v0",
		return_value=SimpleNamespace(manifest=disk_manifest)))
	return manifest_path, pkg_root, snapshot_path, stack


class TestLockEmitSourceRebuild:
	def test_happy_path_stdout_is_exactly_the_flags(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--run-snapshot", str(snap),
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.1"

	def test_evidence_goes_to_stderr_never_stdout(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""Lock pins 1.0.0; fresh resolves 1.0.1 → version drift is
		EVIDENCE on stderr; stdout still exactly the flags."""
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(
			tmp_path, lock_version="1.0.0")
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--run-snapshot", str(snap),
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.1"
		assert "1.0.0 -> 1.0.1" in cap.err
		assert "drift lock emit --source-rebuild" in cap.err

	def test_missing_snapshot_hard_fails_empty_stdout(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_RUN_SNAPSHOT", raising=False)
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 1
		assert cap.out == ""
		assert "run snapshot" in cap.err

	def test_env_snapshot_honoured(self, tmp_path, capsys, monkeypatch) -> None:
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.1"

	def test_snapshot_mismatch_exits_nonzero_empty_stdout(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""Disk package not authorised by the snapshot → index-time
		hard fail: rc 1, stdout EMPTY (nothing for $(...) to eat)."""
		from tools.drift_deploy.drift_lock import run
		from tools.drift_deploy.run_snapshot import (
			SnapshotEntry,
			write_run_snapshot,
		)
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		# Overwrite the snapshot: authorises a DIFFERENT source id.
		write_run_snapshot(
			snap,
			run_id="20260731-lock-emit-mismatch",
			entries={
				("ext.lib", "1.0.1"): SnapshotEntry(
					source_content_id="sha256:" + "f" * 64,
					author_key="ed25519:orch-sig-kid",
					source_attestation_key="ed25519:orch-sak-kid",
				),
			},
		)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 1
		assert cap.out == ""

	def test_missing_package_root_rejected(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
			])
		cap = capsys.readouterr()
		assert rc == 1
		assert cap.out == ""
		assert "--package-root" in cap.err

	def test_strict_lane_inert_under_certify_env(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""DRIFT_CERT_MODE=certify + DRIFT_RUN_SNAPSHOT set, NO
		--source-rebuild flag → the committed lock is read verbatim
		(env alone never flips lock emit, unlike build/deploy/
		prepare)."""
		from tools.drift_deploy.drift_lock import run
		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("my.pkg", "0.1.0", [("ext.lib", "1.0")]),
		])
		_write_lock(tmp_path, {
			"my.pkg": {"ext.lib": {"version": "1.0.0"}},
		})
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(tmp_path / "nonexistent.json"))
		rc = run(["--manifest", str(manifest_path), "--artifact", "my.pkg"])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.0"

	def test_snapshot_flag_without_source_rebuild_rejected(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("my.pkg", "0.1.0", [("ext.lib", "1.0")]),
		])
		_write_lock(tmp_path, {"my.pkg": {"ext.lib": {"version": "1.0.0"}}})
		rc = run([
			"--manifest", str(manifest_path), "--artifact", "my.pkg",
			"--run-snapshot", str(tmp_path / "snap.json"),
		])
		cap = capsys.readouterr()
		assert rc == 1
		assert cap.out == ""
		assert "--source-rebuild" in cap.err

	def test_json_v0_source_rebuild(self, tmp_path, capsys, monkeypatch) -> None:
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(
			tmp_path, lock_version="1.0.0")
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild", "--json",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		payload = json.loads(cap.out)
		assert payload["schema"] == "drift-lock-emit/v0"
		assert payload["mode"] == "source-rebuild"
		assert payload["artifact"] == "my.pkg"
		assert payload["dep_flags"] == ["--dep", "ext.lib@1.0.1"]
		assert payload["evidence"]["version_changed"] == [["ext.lib", "1.0.0", "1.0.1"]]

	def test_json_v0_strict(self, tmp_path, capsys, monkeypatch) -> None:
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("my.pkg", "0.1.0", [("ext.lib", "1.0")]),
		])
		_write_lock(tmp_path, {"my.pkg": {"ext.lib": {"version": "1.0.0"}}})
		rc = run([
			"--manifest", str(manifest_path), "--artifact", "my.pkg", "--json",
		])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		payload = json.loads(cap.out)
		assert payload["schema"] == "drift-lock-emit/v0"
		assert payload["mode"] == "strict"
		assert payload["dep_flags"] == ["--dep", "ext.lib@1.0.0"]
		assert "evidence" not in payload

	def test_missing_lock_is_fine_in_source_rebuild(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""Certify pool is candidate-only; the lock is evidence.  No
		lock on disk → emit still succeeds (no drift evidence)."""
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		(manifest_path.parent / "lock.json").unlink()
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.1"

	def test_pkg_root_env_default_and_flag_precedence(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""No --package-root → DRIFT_PKG_ROOT supplies the pool (the
		announced flagless invocation); an explicit flag WINS over a
		bogus env value."""
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		# env-only: the documented cert-env-contract invocation.
		monkeypatch.setenv("DRIFT_PKG_ROOT", str(pkg_root))
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.1"
		# flag wins: env points at an EMPTY dir, flag at the real pool.
		empty = tmp_path / "empty_pool"
		empty.mkdir()
		monkeypatch.setenv("DRIFT_PKG_ROOT", str(empty))
		(tmp_path / "second").mkdir()
		manifest_path2, pkg_root2, snap2, stack2 = _sr_world(
			tmp_path / "second")
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap2))
		with stack2:
			rc = run([
				"--manifest", str(manifest_path2), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root2),
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.1"

	def test_snapshot_flag_wins_over_env(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""--run-snapshot beats DRIFT_RUN_SNAPSHOT: env points at a
		nonexistent file; the flag's valid snapshot is used."""
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(tmp_path / "no-such.json"))
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--run-snapshot", str(snap),
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.1"

	def test_authority_errors_exit_nonzero_empty_stdout(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""Non-empty SourceRebuildResult.errors -> rc 1, EMPTY stdout,
		error text on stderr.  Distinct from the index-time
		ResolutionError path.  The structural gate producing such
		errors is defence-in-depth that a well-formed snapshot cannot
		reach through the CLI (the loader rejects malformed entries,
		the index gate exact-matches the rest), so the authority is
		mocked at its module to pin THIS command's error branch."""
		from unittest.mock import patch as _patch
		from tools.drift_deploy.drift_lock import run
		from tools.drift_deploy.source_rebuild import (
			SourceRebuildEvidence,
			SourceRebuildResult,
		)
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		synthetic = SourceRebuildResult(
			resolved_graph={},
			evidence=SourceRebuildEvidence(),
			errors=["artifact 'my.pkg' dep 'ext.lib': no verifiable signer (synthetic)"],
		)
		with stack, _patch(
			"tools.drift_deploy.source_rebuild.resolve_source_rebuild",
			return_value=synthetic,
		):
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 1
		assert cap.out == ""
		assert "no verifiable signer" in cap.err

	def test_invalid_cert_mode_exits_nonzero_empty_stdout(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		from tools.drift_deploy.drift_lock import run
		manifest_path, pkg_root, snap, stack = _sr_world(tmp_path)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		monkeypatch.setenv("DRIFT_CERT_MODE", "bogus-mode")
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 1
		assert cap.out == ""
		assert "DRIFT_CERT_MODE" in cap.err

	def _two_artifact_world_with_disk_co_artifact(self, tmp_path, monkeypatch):
		"""Manifest: my.pkg (deps ext.lib@1.0 + peer my.util@0.5) and
		co-artifact my.util@0.5.2.  Disk pool: ext.lib-1.0.1.dmp
		(snapshot-authorised) AND my.util-0.5.2.dmp (NOT in the
		snapshot — a producer output sitting in the pool).  Loader
		mock keys on the .dmp path so each package reads its own
		manifest."""
		from types import SimpleNamespace
		from unittest.mock import patch as _patch
		from contextlib import ExitStack
		from tools.drift_deploy.run_snapshot import (
			SnapshotEntry,
			write_run_snapshot,
		)
		art = _library_artifact("my.pkg", "0.1.0", [
			("ext.lib", "1.0"), ("my.util", "0.5"),
		])
		manifest_path = _write_manifest(tmp_path, [
			art, _library_artifact("my.util", "0.5.2", []),
		])
		scid = "sha256:" + "a" * 64
		ak = "ed25519:orch-sig-kid"
		sak = "ed25519:orch-sak-kid"
		pkg_root = tmp_path / "pkg_root"
		pkg_root.mkdir()
		for stem in ("ext.lib-1.0.1", "my.util-0.5.2"):
			(pkg_root / f"{stem}.dmp").write_bytes(b"fake-" + stem.encode())
			(pkg_root / f"{stem}.sig").write_text("{}")
		snap = tmp_path / "run-snapshot.json"
		write_run_snapshot(
			snap,
			run_id="20260731-stage-exemption",
			entries={
				# NOTE: no entry for my.util — the exemption under test.
				("ext.lib", "1.0.1"): SnapshotEntry(
					source_content_id=scid,
					author_key=ak,
					source_attestation_key=sak,
				),
			},
		)
		monkeypatch.setenv("DRIFT_RUN_SNAPSHOT", str(snap))
		manifests = {
			"ext.lib-1.0.1": {
				"package_id": "ext.lib", "package_version": "1.0.1",
				"modules": [{"module_id": "ext_lib"}], "required_deps": [],
			},
			"my.util-0.5.2": {
				"package_id": "my.util", "package_version": "0.5.2",
				"modules": [{"module_id": "my_util"}], "required_deps": [],
			},
		}
		def _load_by_path(path, *a, **kw):
			return SimpleNamespace(manifest=manifests[Path(path).stem])
		stack = ExitStack()
		stack.enter_context(_patch(
			"tools.drift_deploy.resolver._read_author_key", return_value=ak))
		stack.enter_context(_patch(
			"tools.drift_deploy.resolver._read_source_attestation_meta",
			return_value=(scid, sak)))
		stack.enter_context(_patch(
			"lang.driftc.packages.dmir_pkg_v0.load_dmir_pkg_v0",
			side_effect=_load_by_path))
		return manifest_path, pkg_root, stack

	def test_stage_exemption_admits_disk_co_artifact(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""DRIFT_CERT_MODE=stage: the on-disk co-artifact .dmp absent
		from the snapshot is admitted via snapshot_exempt_ids (producer
		output of this run) — emit succeeds, peer pin in the flags."""
		from tools.drift_deploy.drift_lock import run
		manifest_path, pkg_root, stack = \
			self._two_artifact_world_with_disk_co_artifact(tmp_path, monkeypatch)
		monkeypatch.setenv("DRIFT_CERT_MODE", "stage")
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 0, cap.err
		assert cap.out.strip() == "--dep ext.lib@1.0.1 --dep my.util@0.5.2"

	def test_certify_fails_closed_on_unsnapshotted_disk_co_artifact(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		"""Same world, DRIFT_CERT_MODE=certify (pure consumer, NO
		exemptions): the unsnapshotted disk .dmp fails the index gate
		— rc 1, EMPTY stdout.  Proves the exemption is stage-ONLY."""
		from tools.drift_deploy.drift_lock import run
		manifest_path, pkg_root, stack = \
			self._two_artifact_world_with_disk_co_artifact(tmp_path, monkeypatch)
		monkeypatch.setenv("DRIFT_CERT_MODE", "certify")
		with stack:
			rc = run([
				"--manifest", str(manifest_path), "--artifact", "my.pkg",
				"--source-rebuild",
				"--package-root", str(pkg_root),
			])
		cap = capsys.readouterr()
		assert rc == 1
		assert cap.out == ""
		assert "my.util" in cap.err

	def test_package_root_without_source_rebuild_rejected(
		self, tmp_path, capsys, monkeypatch,
	) -> None:
		from tools.drift_deploy.drift_lock import run
		monkeypatch.delenv("DRIFT_CERT_MODE", raising=False)
		manifest_path = _write_manifest(tmp_path, [
			_library_artifact("my.pkg", "0.1.0", [("ext.lib", "1.0")]),
		])
		_write_lock(tmp_path, {"my.pkg": {"ext.lib": {"version": "1.0.0"}}})
		rc = run([
			"--manifest", str(manifest_path), "--artifact", "my.pkg",
			"--package-root", str(tmp_path),
		])
		cap = capsys.readouterr()
		assert rc == 1
		assert cap.out == ""
		assert "--source-rebuild" in cap.err
