# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
End-to-end CLI regression for `drift-deploy cert publish` / `cosign`.

Mirrors `test_drift_author_cli.py` but for the certifier side.
Confirms:

  - `publish` produces a sidecar at the canonical per-kid name
    with a well-formed claim and the supplied dep_graph;
  - the seven-field `--dep` shape parses correctly (including the
    `-` sentinels for absent author/cert kids);
  - `cosign` appends a rotation co-signature against the named kid;
  - `--cert-suite-result` outside `{pass, fail}` is refused by
    argparse before any signing happens.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lang.drift.crypto import compute_ed25519_kid, ed25519_sign_from_seed
from lang.driftc.packages.cert_claim_v1 import load_cert_claim_json
from lang.driftc.packages.sidecar_naming import cert_claim_filename
from tools.drift_deploy.cert_cli import main as cert_cli_main


def _seed_b64() -> str:
	return base64.b64encode(bytes(range(32))).decode("ascii")


def _seed_b64_b() -> str:
	return base64.b64encode(
		bytes((b ^ 0xFF) for b in bytes(range(32)))
	).decode("ascii")


def _publish_argv(tmp_path: Path, key_text: str) -> list[str]:
	return [
		"publish",
		"--sidecar-dir", str(tmp_path),
		"--package-id", "demo.lib",
		"--version", "1.0.0",
		"--artifact-sha256", "sha256:" + ("c" * 64),
		"--source-content-id", "sha256:" + ("a" * 64),
		"--target", "linux-x86_64",
		"--driftc-version", "0.31.0",
		"--drift-rt-abi", "1",
		"--cert-suite-id", "anthropic/release-gate",
		"--cert-suite-version", "1.0",
		"--cert-suite-result", "pass",
		"--cert-suite-evidence-sha256", "sha256:" + ("f" * 64),
		"--run-id", "run-001",
		"--run-started-utc", "2026-05-19T00:00:00Z",
		"--evidence-sha256", "sha256:" + ("0" * 64),
		"--dep",
		# Format: PKG,VER,ART_SHA,SCI,AUTHOR_KID|-,CERT_KID|-,KIND
		"std,1.0.0,sha256:" + ("d" * 64) + ",sha256:" + ("e" * 64) + ",-,-,direct",
		"--key-text", key_text,
	]


def _expected_kid_for(key_text_b64: str) -> str:
	seed = base64.b64decode(key_text_b64)
	_, pub = ed25519_sign_from_seed(priv_seed32=seed, message=b"")
	return compute_ed25519_kid(pub)


def test_publish_writes_canonical_per_kid_sidecar(tmp_path: Path) -> None:
	rc = cert_cli_main(_publish_argv(tmp_path, _seed_b64()))
	assert rc == 0
	expected_kid = _expected_kid_for(_seed_b64())
	written = tmp_path / cert_claim_filename("demo.lib", expected_kid)
	assert written.is_file()
	claim = load_cert_claim_json(written.read_text(encoding="utf-8"))
	assert claim.body.package_id == "demo.lib"
	assert claim.body.cert_suite.result == "pass"
	assert len(claim.body.dep_graph) == 1
	dep = claim.body.dep_graph[0]
	assert dep.package_id == "std"
	assert dep.author_kid is None
	assert dep.cert_kid is None
	assert dep.dep_kind == "direct"


def test_publish_json_emits_sidecar_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	rc = cert_cli_main(_publish_argv(tmp_path, _seed_b64()) + ["--json"])
	assert rc == 0
	out = json.loads(capsys.readouterr().out)
	assert Path(out["sidecar"]).is_file()


def test_publish_requires_exactly_one_key(tmp_path: Path) -> None:
	argv_both = _publish_argv(tmp_path, _seed_b64()) + ["--key-file", "/tmp/x.seed"]
	with pytest.raises(SystemExit):
		cert_cli_main(argv_both)


def test_publish_rejects_invalid_cert_suite_result(tmp_path: Path) -> None:
	argv = _publish_argv(tmp_path, _seed_b64())
	i = argv.index("--cert-suite-result")
	argv[i + 1] = "maybe"
	with pytest.raises(SystemExit):
		cert_cli_main(argv)


def test_publish_dep_must_have_seven_fields(tmp_path: Path) -> None:
	argv = _publish_argv(tmp_path, _seed_b64())
	i = argv.index("--dep")
	# Strip one field.
	argv[i + 1] = "std,1.0.0,sha256:" + ("d" * 64) + ",sha256:" + ("e" * 64) + ",-,-"
	with pytest.raises(SystemExit):
		cert_cli_main(argv)


def test_publish_dep_dash_sentinels_become_none(tmp_path: Path) -> None:
	"""The `-` sentinel for author_kid/cert_kid maps to None in the
	parsed DepGraphEntry (not the literal string `"-"`)."""
	rc = cert_cli_main(_publish_argv(tmp_path, _seed_b64()))
	assert rc == 0
	expected_kid = _expected_kid_for(_seed_b64())
	claim = load_cert_claim_json(
		(tmp_path / cert_claim_filename("demo.lib", expected_kid)).read_text(encoding="utf-8")
	)
	dep = claim.body.dep_graph[0]
	assert dep.author_kid is None and dep.cert_kid is None


def test_independent_certifiers_via_cli_dont_collide(tmp_path: Path) -> None:
	"""Two `cert publish` invocations with different keys on the
	same release produce TWO distinct sidecars (per O1).  The CLI
	does not need --overwrite for the second; it's a separate
	certifier identity."""
	assert cert_cli_main(_publish_argv(tmp_path, _seed_b64())) == 0
	assert cert_cli_main(_publish_argv(tmp_path, _seed_b64_b())) == 0
	files = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
	assert len(files) == 2


def test_cosign_appends_rotation_signature(tmp_path: Path) -> None:
	seed_a = _seed_b64()
	seed_b = _seed_b64_b()
	assert cert_cli_main(_publish_argv(tmp_path, seed_a)) == 0
	kid_a = _expected_kid_for(seed_a)
	rc = cert_cli_main([
		"cosign",
		"--sidecar-dir", str(tmp_path),
		"--package-id", "demo.lib",
		"--current-certifier-kid", kid_a,
		"--key-text", seed_b,
	])
	assert rc == 0
	claim = load_cert_claim_json(
		(tmp_path / cert_claim_filename("demo.lib", kid_a)).read_text(encoding="utf-8")
	)
	assert len(claim.signatures) == 2
