# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang2.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_drift(argv: list[str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run([sys.executable, "-m", "lang2.drift", *argv], text=True, capture_output=True)


def _write_trust_store(path: Path) -> Path:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps({"format": "drift-trust", "version": 0, "namespaces": {}, "keys": {}, "revoked": {}}), encoding="utf-8")
	return path


def test_drift_doctor_json_is_strict_json_only_and_sorted(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir(parents=True, exist_ok=True)
	(repo / "index.json").write_text(json.dumps({"format": "drift-index", "version": 0, "packages": {}}), encoding="utf-8")

	sources = tmp_path / "drift-sources.json"
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
	trust.write_text(json.dumps({"format": "drift-trust", "version": 0, "namespaces": {}, "keys": {}, "revoked": {}}), encoding="utf-8")

	lock = tmp_path / "drift.lock.json"
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
						"unsigned": True,
					}
				},
			}
		),
		encoding="utf-8",
	)
	sources = tmp_path / "drift-sources.json"
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
	trust.write_text(json.dumps({"format": "drift-trust", "version": 0, "namespaces": {}, "keys": {}, "revoked": {}}), encoding="utf-8")
	lock = tmp_path / "drift.lock.json"
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
	trust.write_text(json.dumps({"format": "drift-trust", "version": 0, "namespaces": {}, "keys": {}, "revoked": {}}), encoding="utf-8")
	lock = tmp_path / "drift.lock.json"
	lock.write_text(json.dumps({"format": "drift-lock", "version": 0, "packages": {}}), encoding="utf-8")

	sources = tmp_path / "drift-sources.json"
	assert not sources.exists()

	cp = _run_drift(["doctor", "--sources", str(sources), "--trust-store", str(trust), "--lock", str(lock), "--json", "--fail-on", "fatal"])
	assert cp.returncode == 0
	report = json.loads(cp.stdout)
	assert report["degraded_count"] >= 1

	cp = _run_drift(
		["doctor", "--sources", str(sources), "--trust-store", str(trust), "--lock", str(lock), "--json", "--fail-on", "degraded"]
	)
	assert cp.returncode == 1


def test_drift_doctor_vendor_missing_artifact_deep(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

pub fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				"--package-id",
				"lib",
				"--package-version",
				"0.0.0",
				"--package-target",
				"test-target",
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	repo = tmp_path / "repo"
	cp = _run_drift(["publish", "--dest-dir", str(repo), "--allow-unsigned", str(pkg)])
	assert cp.returncode == 0, cp.stderr

	sources = tmp_path / "drift-sources.json"
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
	trust = _write_trust_store(tmp_path / "drift" / "trust.json")
	cache = tmp_path / "cache" / "driftpm"
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache)])
	assert cp.returncode == 0, cp.stderr

	vendor_dir = tmp_path / "vendor" / "driftpkgs"
	lock = tmp_path / "drift.lock.json"
	cp = _run_drift(
		["vendor", "--cache-dir", str(cache), "--dest-dir", str(vendor_dir), "--lock", str(lock), "--json"]
	)
	assert cp.returncode == 0, cp.stderr

	lock_obj = json.loads(lock.read_text(encoding="utf-8"))
	vendored_path = vendor_dir / lock_obj["packages"]["lib"]["path"]
	assert vendored_path.exists()
	vendored_path.unlink()

	cp = _run_drift(
		[
			"doctor",
			"--sources",
			str(sources),
			"--trust-store",
			str(trust),
			"--lock",
			str(lock),
			"--cache-dir",
			str(cache),
			"--vendor-dir",
			str(vendor_dir),
			"--json",
			"--deep",
			"--fail-on",
			"degraded",
		]
	)
	assert cp.returncode == 1
	assert (cp.stderr or "").strip() == ""
	report = json.loads(cp.stdout)
	vendor_check = next(c for c in report["checks"] if c["check_id"] == "vendor_consistency")
	assert vendor_check["status"] == "degraded"
	assert any(f["reason_code"] == "VENDOR_MISSING_ARTIFACT" and f["identity"]["package_id"] == "lib" for f in vendor_check["findings"])


def test_drift_doctor_cache_divergence_detected(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

pub fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				"--package-id",
				"lib",
				"--package-version",
				"0.0.0",
				"--package-target",
				"test-target",
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)

	repo = tmp_path / "repo"
	cp = _run_drift(["publish", "--dest-dir", str(repo), "--allow-unsigned", str(pkg)])
	assert cp.returncode == 0, cp.stderr

	sources = tmp_path / "drift-sources.json"
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
	trust = _write_trust_store(tmp_path / "drift" / "trust.json")
	cache = tmp_path / "cache" / "driftpm"
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache)])
	assert cp.returncode == 0, cp.stderr

	vendor_dir = tmp_path / "vendor" / "driftpkgs"
	lock = tmp_path / "drift.lock.json"
	cp = _run_drift(
		["vendor", "--cache-dir", str(cache), "--dest-dir", str(vendor_dir), "--lock", str(lock), "--json"]
	)
	assert cp.returncode == 0, cp.stderr

	lock_obj = json.loads(lock.read_text(encoding="utf-8"))
	cache_pkg = cache / "pkgs" / lock_obj["packages"]["lib"]["path"]
	data = bytearray(cache_pkg.read_bytes())
	assert data, "cache package should not be empty"
	data[-1] ^= 0xFF
	cache_pkg.write_bytes(bytes(data))

	common_args = [
		"doctor",
		"--sources",
		str(sources),
		"--trust-store",
		str(trust),
		"--lock",
		str(lock),
		"--cache-dir",
		str(cache),
		"--vendor-dir",
		str(vendor_dir),
		"--json",
		"--deep",
	]

	cp = _run_drift(common_args)
	assert cp.returncode == 0
	assert (cp.stderr or "").strip() == ""
	report = json.loads(cp.stdout)
	cache_check = next(c for c in report["checks"] if c["check_id"] == "cache_consistency")
	assert cache_check["status"] == "degraded"
	assert any(f["reason_code"] == "LOCK_CACHE_DIVERGENCE" for f in cache_check["findings"])

	cp_fail = _run_drift([*common_args, "--fail-on", "degraded"])
	assert cp_fail.returncode == 1
	assert (cp_fail.stderr or "").strip() == ""
