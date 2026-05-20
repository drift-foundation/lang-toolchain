# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_drift(argv: list[str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run([sys.executable, "-m", "lang.drift", *argv], text=True, capture_output=True)


def _write_trust_store(path: Path) -> Path:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps({"format": "drift-trust", "version": 1, "namespaces": {}, "keys": {}, "revoked": []}), encoding="utf-8")
	return path


def test_drift_doctor_json_is_strict_json_only_and_sorted(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir(parents=True, exist_ok=True)
	(repo / "index.json").write_text(json.dumps({"format": "drift-index", "version": 0, "packages": {}}), encoding="utf-8")

	sources = tmp_path / "drift" / "sources.json"
	sources.parent.mkdir(parents=True, exist_ok=True)
	sources.write_text(
		json.dumps(
			{
				"format": "drift-sources",
				"version": 0,
				"sources": [{"kind": "dir", "id": "repo", "priority": 0, "path": str(repo)}],
			}
		),
		encoding="utf-8",
	)

	trust = tmp_path / "drift" / "trust.json"
	trust.parent.mkdir(parents=True, exist_ok=True)
	trust.write_text(json.dumps({"format": "drift-trust", "version": 1, "namespaces": {}, "keys": {}, "revoked": []}), encoding="utf-8")

	lock = tmp_path / "drift" / "sources.lock.json"
	lock.parent.mkdir(parents=True, exist_ok=True)
	lock.write_text(json.dumps({"format": "drift-lock", "version": 0, "packages": {}}), encoding="utf-8")

	cp = _run_drift(["doctor", "--sources", str(sources), "--trust-store", str(trust), "--lock", str(lock), "--json"])
	assert cp.returncode == 0
	assert (cp.stderr or "").strip() == ""
	assert cp.stdout.lstrip().startswith("{")
	report = json.loads(cp.stdout)
	assert report["ok"] is True
	assert isinstance(report["checks"], list)
	check_ids = [c["check_id"] for c in report["checks"]]
	assert check_ids == sorted(check_ids)


def test_drift_doctor_json_failure_missing_package_file_deep(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir(parents=True, exist_ok=True)
	(repo / "index.json").write_text(
		json.dumps(
			{
				"format": "drift-index",
				"version": 0,
				"packages": {
					"lib": {
						"package_version": "0.0.0",
						"target": "test-target",
						"sha256": "sha256:" + ("00" * 32),
						"filename": "lib-0.0.0-test-target.dmp",
						"signers": [],
						"signed": False,
					}
				},
			}
		),
		encoding="utf-8",
	)
	sources = tmp_path / "drift" / "sources.json"
	sources.parent.mkdir(parents=True, exist_ok=True)
	sources.write_text(
		json.dumps(
			{
				"format": "drift-sources",
				"version": 0,
				"sources": [{"kind": "dir", "id": "repo", "priority": 0, "path": str(repo)}],
			}
		),
		encoding="utf-8",
	)
	trust = tmp_path / "drift" / "trust.json"
	trust.parent.mkdir(parents=True, exist_ok=True)
	trust.write_text(json.dumps({"format": "drift-trust", "version": 1, "namespaces": {}, "keys": {}, "revoked": []}), encoding="utf-8")
	lock = tmp_path / "drift" / "sources.lock.json"
	lock.parent.mkdir(parents=True, exist_ok=True)
	lock.write_text(json.dumps({"format": "drift-lock", "version": 0, "packages": {}}), encoding="utf-8")

	cp = _run_drift(
		["doctor", "--sources", str(sources), "--trust-store", str(trust), "--lock", str(lock), "--deep", "--json"]
	)
	assert cp.returncode == 2
	assert (cp.stderr or "").strip() == ""
	report = json.loads(cp.stdout)
	assert report["ok"] is False
	index_check = next(c for c in report["checks"] if c["check_id"] == "indexes")
	assert index_check["status"] == "fatal"
	assert any(f["reason_code"] == "INDEX_MISSING_PACKAGE_FILE" for f in index_check["findings"])


def test_drift_doctor_exit_code_degraded_vs_fatal(tmp_path: Path) -> None:
	# Missing sources file is degraded by default.
	trust = tmp_path / "drift" / "trust.json"
	trust.parent.mkdir(parents=True, exist_ok=True)
	trust.write_text(json.dumps({"format": "drift-trust", "version": 1, "namespaces": {}, "keys": {}, "revoked": []}), encoding="utf-8")
	lock = tmp_path / "drift" / "sources.lock.json"
	lock.parent.mkdir(parents=True, exist_ok=True)
	lock.write_text(json.dumps({"format": "drift-lock", "version": 0, "packages": {}}), encoding="utf-8")

	sources = tmp_path / "drift" / "sources.json"
	sources.parent.mkdir(parents=True, exist_ok=True)
	assert not sources.exists()

	cp = _run_drift(["doctor", "--sources", str(sources), "--trust-store", str(trust), "--lock", str(lock), "--json", "--fail-on", "fatal"])
	assert cp.returncode == 0
	report = json.loads(cp.stdout)
	assert report["degraded_count"] >= 1

	cp = _run_drift(
		["doctor", "--sources", str(sources), "--trust-store", str(trust), "--lock", str(lock), "--json", "--fail-on", "degraded"]
	)
	assert cp.returncode == 1


# `test_drift_doctor_vendor_missing_artifact_deep` and
# `test_drift_doctor_cache_divergence_detected` exercised the v0
# `drift fetch` + `drift vendor` distribution pipeline that the
# trust-v1 cutover removes.  Coverage for doctor's vendor / cache
# checks against a v1 distribution flow will be reintroduced when
# the v1 distribution slice replaces the v0 fetch/vendor commands.
