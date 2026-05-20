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


# Pre-v1 `drift sign` / `drift publish` / `drift package inspect-signers`
# were removed in the trust-v1 cutover.  Author-side signing now lives in
# `drift-author publish`; signer kids surface through the v1 author /
# cert claim sidecars instead of a CLI inspector.  The signer-inspect
# contract is covered by the adversarial suite in
# `lang/tests/packages/test_v1_adversarial.py`.
