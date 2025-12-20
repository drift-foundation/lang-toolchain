# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import json
import subprocess
import sys
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
		json.dumps({"format": "drift-sources", "version": 0, "sources": [{"kind": "dir", "path": str(repo)}]}),
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
	assert any(e["package_id"] == "lib" for e in lock["packages"])
