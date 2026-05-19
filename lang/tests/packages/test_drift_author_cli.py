# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
End-to-end CLI regression for `drift-author publish` / `cosign`.

Exercises the argparse layer directly via `cli.main(argv)` so we
don't fork a subprocess in the unit tier.  Confirms:

  - `publish` produces a sidecar at the canonical name with a
    well-formed claim;
  - mutually-exclusive `--key-file` / `--key-text` is enforced;
  - missing required body fields exit non-zero;
  - `cosign` appends a co-author signature against an existing
    sidecar;
  - `--json` emits a machine-readable handle to the written path.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lang.driftc.packages.author_claim_v1 import load_author_claim_json
from lang.driftc.packages.sidecar_naming import author_claim_filename
from tools.drift_author.cli import main as author_cli_main


def _seed_b64() -> str:
	return base64.b64encode(bytes(range(32))).decode("ascii")


def _publish_argv(tmp_path: Path, key_text: str) -> list[str]:
	return [
		"publish",
		"--sidecar-dir", str(tmp_path),
		"--package-id", "demo.lib",
		"--version", "1.0.0",
		"--namespace", "demo.lib",
		"--source-content-id", "sha256:" + ("a" * 64),
		"--target-class", "library",
		"--release-utc", "2026-05-19T00:00:00Z",
		"--required-dep", "std=^1",
		"--key-text", key_text,
	]


def test_publish_writes_canonical_sidecar(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	rc = author_cli_main(_publish_argv(tmp_path, _seed_b64()))
	assert rc == 0
	written = tmp_path / author_claim_filename("demo.lib")
	assert written.is_file()
	claim = load_author_claim_json(written.read_text(encoding="utf-8"))
	assert claim.body.package_id == "demo.lib"
	assert claim.body.version == "1.0.0"
	assert len(claim.signatures) == 1


def test_publish_json_emits_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	rc = author_cli_main(_publish_argv(tmp_path, _seed_b64()) + ["--json"])
	assert rc == 0
	out = json.loads(capsys.readouterr().out)
	assert Path(out["sidecar"]).is_file()


def test_publish_requires_exactly_one_key(tmp_path: Path) -> None:
	"""Both --key-file and --key-text together must fail; neither
	must also fail."""
	argv_both = _publish_argv(tmp_path, _seed_b64()) + ["--key-file", "/tmp/x.seed"]
	with pytest.raises(SystemExit):
		author_cli_main(argv_both)

	argv_neither = [a for a in _publish_argv(tmp_path, _seed_b64())
		if a != "--key-text" and a != _seed_b64()]
	with pytest.raises(SystemExit):
		author_cli_main(argv_neither)


def test_publish_refuses_overwrite_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	assert author_cli_main(_publish_argv(tmp_path, _seed_b64())) == 0
	# Second publish without --overwrite should exit non-zero with a
	# helpful message routed to stderr.
	rc = author_cli_main(_publish_argv(tmp_path, _seed_b64()))
	assert rc != 0
	captured = capsys.readouterr()
	assert "cosign" in captured.err or "overwrite" in captured.err


def test_publish_overwrite_replaces(tmp_path: Path) -> None:
	"""Per `--overwrite`, the CLI should let the user replace an
	existing sidecar (e.g. correcting a wrong-body publish before
	any consumers fetched it).  Replacement discards prior signatures."""
	assert author_cli_main(_publish_argv(tmp_path, _seed_b64())) == 0
	rc = author_cli_main(_publish_argv(tmp_path, _seed_b64()) + ["--overwrite"])
	assert rc == 0
	claim = load_author_claim_json(
		(tmp_path / author_claim_filename("demo.lib")).read_text(encoding="utf-8")
	)
	assert len(claim.signatures) == 1


def test_publish_requires_namespace(tmp_path: Path) -> None:
	argv = _publish_argv(tmp_path, _seed_b64())
	# Strip --namespace and its value.
	while "--namespace" in argv:
		i = argv.index("--namespace")
		del argv[i:i + 2]
	with pytest.raises(SystemExit):
		author_cli_main(argv)


def test_cosign_appends_signature(tmp_path: Path) -> None:
	seed_a_b64 = _seed_b64()
	seed_b_b64 = base64.b64encode(
		bytes((b ^ 0xFF) for b in bytes(range(32)))
	).decode("ascii")
	assert author_cli_main(_publish_argv(tmp_path, seed_a_b64)) == 0
	rc = author_cli_main([
		"cosign",
		"--sidecar-dir", str(tmp_path),
		"--package-id", "demo.lib",
		"--key-text", seed_b_b64,
	])
	assert rc == 0
	claim = load_author_claim_json(
		(tmp_path / author_claim_filename("demo.lib")).read_text(encoding="utf-8")
	)
	assert len(claim.signatures) == 2
	# Two distinct kids confirm two distinct signers.
	assert len({s.kid for s in claim.signatures}) == 2


def test_required_dep_must_be_name_equals_range(tmp_path: Path) -> None:
	argv = _publish_argv(tmp_path, _seed_b64())
	# Replace good dep spec with malformed one.
	i = argv.index("--required-dep")
	argv[i + 1] = "no-equals-sign"
	with pytest.raises(SystemExit):
		author_cli_main(argv)
