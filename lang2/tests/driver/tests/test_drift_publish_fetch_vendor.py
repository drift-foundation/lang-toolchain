# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import json
import subprocess
import sys
import shutil
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lang2.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_drift(argv: list[str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run([sys.executable, "-m", "lang2.drift", *argv], text=True, capture_output=True)


def test_drift_publish_fetch_vendor_round_trip(tmp_path: Path) -> None:
	# Build a tiny unsigned package.
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
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

	# Sign it (publisher role).
	priv = Ed25519PrivateKey.generate()
	seed = priv.private_bytes_raw()
	key_seed = tmp_path / "key.seed"
	key_seed.write_text(base64.b64encode(seed).decode("ascii") + "\n", encoding="utf-8")
	cp = _run_drift(["sign", str(pkg), "--key", str(key_seed)])
	assert cp.returncode == 0, cp.stderr
	assert Path(str(pkg) + ".sig").exists()

	# Publish to a local directory repository.
	repo = tmp_path / "repo"
	cp = _run_drift(["publish", "--dest-dir", str(repo), str(pkg)])
	assert cp.returncode == 0, cp.stderr
	index_path = repo / "index.json"
	assert index_path.exists()
	index = json.loads(index_path.read_text(encoding="utf-8"))
	assert index["format"] == "drift-index"
	assert "lib" in index["packages"]

	# Fetch into project-local cache.
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
	cache = tmp_path / "cache" / "driftpm"
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache)])
	assert cp.returncode == 0, cp.stderr
	assert (cache / "index.json").exists()

	# Vendor from cache and write a lockfile.
	vendor_dir = tmp_path / "vendor" / "driftpkgs"
	lock_path = tmp_path / "drift.lock.json"
	cp = _run_drift(
		[
			"vendor",
			"--cache-dir",
			str(cache),
			"--dest-dir",
			str(vendor_dir),
			"--lock",
			str(lock_path),
		]
	)
	assert cp.returncode == 0, cp.stderr
	assert lock_path.exists()
	lock = json.loads(lock_path.read_text(encoding="utf-8"))
	assert lock["format"] == "drift-lock"
	assert "lib" in lock["packages"]
	assert lock["packages"]["lib"]["pkg_sha256"].startswith("sha256:")

	# Lock is authoritative: delete cache and reproduce exactly.
	shutil.rmtree(cache)
	cache.mkdir(parents=True, exist_ok=True)
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache), "--lock", str(lock_path)])
	assert cp.returncode == 0, cp.stderr
	rebuilt = (cache / "pkgs").glob("*.dmp")
	pkgs = list(rebuilt)
	assert len(pkgs) == 1
	pkg_bytes = pkgs[0].read_bytes()
	assert lock["packages"]["lib"]["pkg_sha256"] == "sha256:" + hashlib.sha256(pkg_bytes).hexdigest()


def test_drift_fetch_selects_deterministically_across_sources(tmp_path: Path) -> None:
	"""
When two sources provide the same package id, fetch must pick deterministically
by (priority, source_id), not by file order or scan order.
	"""

	def _build_pkg(lib_body: str, out_pkg: Path) -> tuple[str, bytes]:
		_write_file(
			tmp_path / "lib" / "lib.drift",
			f"""
module lib

export {{ add }}

fn add(a: Int, b: Int) returns Int {{
	{lib_body}
}}
""".lstrip(),
		)
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
					str(out_pkg),
				]
			)
			== 0
		)
		pkg_bytes = out_pkg.read_bytes()
		return hashlib.sha256(pkg_bytes).hexdigest(), pkg_bytes

	# Two packages with the same identity but different contents.
	pkg_a = tmp_path / "lib_a.dmp"
	pkg_b = tmp_path / "lib_b.dmp"
	sha_a, bytes_a = _build_pkg("return a + b", pkg_a)
	sha_b, _bytes_b = _build_pkg("return a + b + 1", pkg_b)
	assert sha_a != sha_b

	# Publish to two repos; both will have the same deterministic filename but
	# different sha256.
	repo_a = tmp_path / "repo_a"
	repo_b = tmp_path / "repo_b"
	cp = _run_drift(["publish", "--dest-dir", str(repo_a), "--allow-unsigned", str(pkg_a)])
	assert cp.returncode == 0, cp.stderr
	cp = _run_drift(["publish", "--dest-dir", str(repo_b), "--allow-unsigned", str(pkg_b)])
	assert cp.returncode == 0, cp.stderr

	# Sources are listed in the opposite order from the deterministic winner.
	# Both have the same priority; tie-break is source_id.
	sources = tmp_path / "drift-sources.json"
	sources.write_text(
		json.dumps(
			{
				"format": "drift-sources",
				"version": 0,
				"sources": [
					{"kind": "dir", "id": "b", "priority": 0, "path": str(repo_b)},
					{"kind": "dir", "id": "a", "priority": 0, "path": str(repo_a)},
				],
			}
		),
		encoding="utf-8",
	)

	cache = tmp_path / "cache" / "driftpm"
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache)])
	assert cp.returncode == 0, cp.stderr

	# Cache must contain repo_a's bytes (source_id "a" wins tie-break).
	cache_pkgs = list((cache / "pkgs").glob("*.dmp"))
	assert len(cache_pkgs) == 1
	got = cache_pkgs[0].read_bytes()
	assert hashlib.sha256(got).hexdigest() == sha_a
	assert got == bytes_a

	cache_index = json.loads((cache / "index.json").read_text(encoding="utf-8"))
	assert cache_index["packages"]["lib"]["source_id"] == "a"


def test_drift_fetch_rejects_sha_mismatch_between_index_and_bytes(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
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

	# Corrupt the bytes after publishing without updating the index.
	repo_pkg = repo / "lib-0.0.0-test-target.dmp"
	data = bytearray(repo_pkg.read_bytes())
	data[-1] ^= 0xFF
	repo_pkg.write_bytes(bytes(data))

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
	cache = tmp_path / "cache" / "driftpm"
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache)])
	assert cp.returncode != 0
	assert "sha256 mismatch" in (cp.stderr or cp.stdout)


def test_drift_fetch_rejects_identity_mismatch_in_index(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	# Build a package with version 0.0.1, but index will claim 0.0.0.
	pkg = tmp_path / "lib-0.0.0-test-target.dmp"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				str(tmp_path / "lib" / "lib.drift"),
				"--package-id",
				"lib",
				"--package-version",
				"0.0.1",
				"--package-target",
				"test-target",
				"--emit-package",
				str(pkg),
			]
		)
		== 0
	)
	repo = tmp_path / "repo"
	repo.mkdir(parents=True, exist_ok=True)
	shutil.copyfile(pkg, repo / "lib-0.0.0-test-target.dmp")
	pkg_sha = hashlib.sha256((repo / "lib-0.0.0-test-target.dmp").read_bytes()).hexdigest()
	(repo / "index.json").write_text(
		json.dumps(
			{
				"format": "drift-index",
				"version": 0,
				"packages": {
					"lib": {
						"package_version": "0.0.0",
						"target": "test-target",
						"sha256": "sha256:" + pkg_sha,
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
	cache = tmp_path / "cache" / "driftpm"
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache)])
	assert cp.returncode != 0
	assert "identity mismatch" in (cp.stderr or cp.stdout)


def test_drift_fetch_rejects_malformed_index_json(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir(parents=True, exist_ok=True)
	(repo / "index.json").write_text(
		json.dumps({"format": "drift-index", "version": 0, "packages": {"lib": {"filename": "x"}}}),
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
	cache = tmp_path / "cache" / "driftpm"
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache)])
	assert cp.returncode != 0
	assert "invalid index entry" in (cp.stderr or cp.stdout)


def test_drift_fetch_rejects_lock_with_unknown_source_id_when_ambiguous(tmp_path: Path) -> None:
	"""
The lockfile must be authoritative. A legacy/placeholder source_id must not
silently re-enable heuristic selection when multiple sources provide the same
package id.
	"""

	def _build_pkg(lib_body: str, out_pkg: Path) -> None:
		_write_file(
			tmp_path / "lib" / "lib.drift",
			f"""
module lib

export {{ add }}

fn add(a: Int, b: Int) returns Int {{
	{lib_body}
}}
""".lstrip(),
		)
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
					str(out_pkg),
				]
			)
			== 0
		)

	pkg_a = tmp_path / "lib_a.dmp"
	pkg_b = tmp_path / "lib_b.dmp"
	_build_pkg("return a + b", pkg_a)
	_build_pkg("return a + b + 1", pkg_b)

	repo_a = tmp_path / "repo_a"
	repo_b = tmp_path / "repo_b"
	cp = _run_drift(["publish", "--dest-dir", str(repo_a), "--allow-unsigned", str(pkg_a)])
	assert cp.returncode == 0, cp.stderr
	cp = _run_drift(["publish", "--dest-dir", str(repo_b), "--allow-unsigned", str(pkg_b)])
	assert cp.returncode == 0, cp.stderr

	sources = tmp_path / "drift-sources.json"
	sources.write_text(
		json.dumps(
			{
				"format": "drift-sources",
				"version": 0,
				"sources": [
					{"kind": "dir", "id": "a", "priority": 0, "path": str(repo_a)},
					{"kind": "dir", "id": "b", "priority": 0, "path": str(repo_b)},
				],
			}
		),
		encoding="utf-8",
	)

	# Manually create a legacy/broken lockfile with source_id 'unknown'.
	lock_path = tmp_path / "drift.lock.json"
	lock_path.write_text(
		json.dumps(
			{
				"format": "drift-lock",
				"version": 0,
				"packages": {
					"lib": {
						"version": "0.0.0",
						"target": "test-target",
						"pkg_sha256": json.loads((repo_a / "index.json").read_text(encoding="utf-8"))["packages"]["lib"][
							"sha256"
						],
						"sig_sha256": None,
						"sig_kids": [],
						"modules": ["lib"],
						"source_id": "unknown",
						"path": "lib-0.0.0-test-target.dmp",
					}
				},
			}
		),
		encoding="utf-8",
	)

	cache = tmp_path / "cache" / "driftpm"
	cp = _run_drift(["fetch", "--sources", str(sources), "--cache-dir", str(cache), "--lock", str(lock_path)])
	assert cp.returncode != 0
	assert "missing source_id" in (cp.stderr or cp.stdout)
