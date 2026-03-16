# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lang.tests.driver.driver_cli_helpers import with_target_word_bits


def _run_drift(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	return subprocess.run([sys.executable, "-m", "lang.drift", *argv], text=True, capture_output=True, env=env)


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_drift_key_list_inspect_match_signer(tmp_path: Path) -> None:
	keys_dir = tmp_path / "keys"
	keys_dir.mkdir(parents=True, exist_ok=True)
	key_a = keys_dir / "default.seed"
	key_b = keys_dir / "ci.seed"

	cp = _run_drift(["keygen", "--out", str(key_a), "--print-kid"])
	assert cp.returncode == 0, cp.stderr
	kid_a = (cp.stdout or "").strip()
	assert kid_a.startswith("ed25519:")

	cp = _run_drift(["keygen", "--out", str(key_b), "--print-kid"])
	assert cp.returncode == 0, cp.stderr
	kid_b = (cp.stdout or "").strip()
	assert kid_b.startswith("ed25519:")
	assert kid_a != kid_b

	env = dict(os.environ)
	env["DRIFT_SIGN_KEY_FILE"] = str(key_a)

	cp = _run_drift(["key", "list", "--keys-dir", str(keys_dir), "--json"], env=env)
	assert cp.returncode == 0, cp.stderr
	obj = json.loads(cp.stdout or "{}")
	keys = obj.get("keys") or []
	assert len(keys) == 2
	by_name = {str(k["name"]): k for k in keys}
	assert by_name["default"]["kid"] == kid_a
	assert by_name["ci"]["kid"] == kid_b
	assert bool(by_name["default"]["default"]) is True
	assert bool(by_name["ci"]["default"]) is False

	cp = _run_drift(["key", "inspect", "default", "--keys-dir", str(keys_dir), "--json"])
	assert cp.returncode == 0, cp.stderr
	info = json.loads(cp.stdout or "{}")
	assert info.get("kid") == kid_a
	assert str(info.get("path", "")).endswith("default.seed")

	cp = _run_drift(["key", "match-signer", kid_b, "--keys-dir", str(keys_dir), "--json"])
	assert cp.returncode == 0, cp.stderr
	match = json.loads(cp.stdout or "{}")
	matches = match.get("matches") or []
	assert len(matches) == 1
	assert str(matches[0].get("path", "")).endswith("ci.seed")

	cp = _run_drift(["key", "match-signer", "ed25519:not-found", "--keys-dir", str(keys_dir), "--json"])
	assert cp.returncode != 0
	match = json.loads(cp.stdout or "{}")
	assert match.get("matches") == []


def test_drift_package_inspect_signers_sig_dmp_index(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) -> Int {
	return a + b;
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	build = subprocess.run(
		with_target_word_bits(
			[
				sys.executable,
				"-m",
				"lang.driftc.driftc",
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
				"--json",
			]
		),
		check=False,
		capture_output=True,
		text=True,
	)
	assert build.returncode == 0, build.stderr

	key = tmp_path / "default.seed"
	cp = _run_drift(["keygen", "--out", str(key), "--print-kid"])
	assert cp.returncode == 0, cp.stderr
	kid = (cp.stdout or "").strip()
	assert kid.startswith("ed25519:")

	cp = _run_drift(["sign", str(pkg), "--key", str(key), "--include-pubkey"])
	assert cp.returncode == 0, cp.stderr
	sig = pkg.with_suffix(".sig")
	assert sig.exists()

	cp = _run_drift(["package", "inspect-signers", str(sig), "--json"])
	assert cp.returncode == 0, cp.stderr
	obj = json.loads(cp.stdout or "{}")
	assert obj.get("source") == "sidecar"
	assert obj.get("signers") == [kid]

	cp = _run_drift(["package", "inspect-signers", str(pkg), "--json"])
	assert cp.returncode == 0, cp.stderr
	obj = json.loads(cp.stdout or "{}")
	assert obj.get("source") == "package-sidecar"
	assert obj.get("signers") == [kid]

	repo = tmp_path / "repo"
	cp = _run_drift(["publish", "--dest-dir", str(repo), str(pkg)])
	assert cp.returncode == 0, cp.stderr

	cp = _run_drift(["package", "inspect-signers", str(repo / "index.json"), "--package-id", "lib", "--json"])
	assert cp.returncode == 0, cp.stderr
	obj = json.loads(cp.stdout or "{}")
	assert obj.get("source") == "index"
	assert obj.get("package_id") == "lib"
	assert obj.get("signers") == [kid]
	assert obj.get("signed") is True
