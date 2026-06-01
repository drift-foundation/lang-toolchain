# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression for `drift author verify` — the keyless, read-only "is my
committed author claim stale?" check.

Exercises the CLI through `cli.main(["verify", ...])` (internal surface)
and `run_author_subcommand(["verify", ...])` (public `drift author`
surface).  Confirms:

  - in-sync claim ⇒ exit 0;
  - source mutated since signing ⇒ exit 1, reason `source_content_id`;
  - manifest version bumped since signing ⇒ exit 1, reason `version`;
  - no committed claim ⇒ exit 1, status `missing_claim`;
  - the check is **keyless** (no --key-file/--key-text) and
    **side-effect-free** (writes nothing);
  - `--artifact` selects in a multi-library manifest;
  - `--json` carries a machine-readable verdict for the orchestrator.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lang.driftc.packages.sidecar_naming import author_claim_filename
from tools.drift_author.cli import main as author_cli_main
from tools.drift_author.cli import run_author_subcommand


def _seed_b64() -> str:
	return base64.b64encode(bytes(range(32))).decode("ascii")


def _layout(tmp_path: Path, *, name: str = "myrepo", version: str = "0.1.0") -> Path:
	"""Build `<repo>/drift/manifest.json` + sources + asset; return the
	manifest dir (`<repo>/drift`)."""
	project_root = tmp_path / name
	drift = project_root / "drift"
	drift.mkdir(parents=True)
	(project_root / "src").mkdir()
	(project_root / "src" / "lib.drift").write_text(f"module {name};\n", encoding="utf-8")
	(project_root / "src" / "util.drift").write_text(f"module {name}.util;\n", encoding="utf-8")
	(project_root / "assets").mkdir()
	(project_root / "assets" / "cfg.toml").write_text("[a]\nb=1\n", encoding="utf-8")
	manifest = {
		"schema_version": 2,
		"project": {"name": name, "license": "MIT"},
		"artifacts": [{
			"kind": "library",
			"name": name,
			"version": version,
			"description": "demo",
			"entry_module": "src/lib.drift",
			"modules": ["src/lib.drift", "src/util.drift"],
			"assets": ["assets/cfg.toml"],
			"native_deps": [{"lib": "ssl"}],
			"package_deps": [{"name": "core.foo", "version": "0.3"}],
		}],
	}
	(drift / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
	return drift


def _seed_file(tmp_path: Path) -> Path:
	seed = tmp_path / "k.seed"
	seed.write_text(_seed_b64() + "\n", encoding="utf-8")
	return seed


def _mint(drift: Path, seed: Path, *, extra: list[str] | None = None) -> None:
	rc = author_cli_main(
		["publish", "--manifest", str(drift / "manifest.json"), "--key-file", str(seed)]
		+ (extra or [])
	)
	assert rc == 0, "fixture mint should succeed"


def test_verify_in_sync_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	drift = _layout(tmp_path)
	_mint(drift, _seed_file(tmp_path))
	capsys.readouterr()  # drop mint output
	rc = author_cli_main(["verify", "--manifest", str(drift / "manifest.json")])
	assert rc == 0
	assert "in sync" in capsys.readouterr().out


def test_verify_detects_source_change(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	drift = _layout(tmp_path)
	_mint(drift, _seed_file(tmp_path))
	capsys.readouterr()
	# Mutate a declared source after signing — the SCI must shift.
	(drift.parent / "src" / "util.drift").write_text(
		"module myrepo.util;\n// changed after signing\n", encoding="utf-8",
	)
	rc = author_cli_main(["verify", "--manifest", str(drift / "manifest.json")])
	assert rc == 1
	err = capsys.readouterr().err
	assert "source_content_id" in err and "changed since the author signed" in err


def test_verify_detects_version_bump(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	drift = _layout(tmp_path)
	_mint(drift, _seed_file(tmp_path))
	capsys.readouterr()
	# Bump the manifest artifact version after signing.
	mf = json.loads((drift / "manifest.json").read_text())
	mf["artifacts"][0]["version"] = "0.2.0"
	(drift / "manifest.json").write_text(json.dumps(mf), encoding="utf-8")
	rc = author_cli_main(["verify", "--manifest", str(drift / "manifest.json")])
	assert rc == 1
	assert "binds version" in capsys.readouterr().err


def test_verify_missing_claim_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	drift = _layout(tmp_path)  # no mint → no claim
	rc = author_cli_main(["verify", "--manifest", str(drift / "manifest.json")])
	assert rc == 1
	assert "no author claim" in capsys.readouterr().err


def test_verify_is_keyless(tmp_path: Path) -> None:
	"""The verify surface accepts no key material — passing one is an error."""
	drift = _layout(tmp_path)
	_mint(drift, _seed_file(tmp_path))
	with pytest.raises(SystemExit):
		author_cli_main([
			"verify", "--manifest", str(drift / "manifest.json"),
			"--key-file", str(_seed_file(tmp_path)),
		])


def test_verify_writes_nothing(tmp_path: Path) -> None:
	"""Side-effect-free: the on-disk tree is byte-identical after verify."""
	drift = _layout(tmp_path)
	_mint(drift, _seed_file(tmp_path))
	before = {p: p.read_bytes() for p in sorted((drift.parent).rglob("*")) if p.is_file()}
	rc = author_cli_main(["verify", "--manifest", str(drift / "manifest.json")])
	assert rc == 0
	after = {p: p.read_bytes() for p in sorted((drift.parent).rglob("*")) if p.is_file()}
	assert before == after, "verify must not create, delete, or modify any file"


def test_verify_json_shape_ok_and_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	drift = _layout(tmp_path)
	_mint(drift, _seed_file(tmp_path))
	capsys.readouterr()
	rc = author_cli_main(["verify", "--manifest", str(drift / "manifest.json"), "--json"])
	assert rc == 0
	ok = json.loads(capsys.readouterr().out)
	assert ok["status"] == "ok" and ok["mismatch"] == []
	assert ok["package_id"] == "myrepo"
	assert ok["source_content_id"]["computed"] == ok["source_content_id"]["claim"]

	(drift.parent / "src" / "util.drift").write_text("module myrepo.util;\n//x\n", encoding="utf-8")
	rc = author_cli_main(["verify", "--manifest", str(drift / "manifest.json"), "--json"])
	assert rc == 1
	stale = json.loads(capsys.readouterr().out)
	assert stale["status"] == "stale" and "source_content_id" in stale["mismatch"]
	assert stale["source_content_id"]["computed"] != stale["source_content_id"]["claim"]


def test_verify_multi_library_requires_artifact(tmp_path: Path) -> None:
	drift = _layout(tmp_path)
	# Add a second library so selection is ambiguous.
	mf = json.loads((drift / "manifest.json").read_text())
	second = dict(mf["artifacts"][0])
	second["name"] = "myrepo_extra"
	mf["artifacts"].append(second)
	(drift / "manifest.json").write_text(json.dumps(mf), encoding="utf-8")
	with pytest.raises(SystemExit):
		author_cli_main(["verify", "--manifest", str(drift / "manifest.json")])
	# With --artifact naming a lib that has no claim → clean rc=1 (missing).
	rc = author_cli_main(
		["verify", "--manifest", str(drift / "manifest.json"), "--artifact", "myrepo_extra"]
	)
	assert rc == 1


def test_verify_public_drift_author_surface(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""`drift author verify` (run_author_subcommand) dispatches to the
	same handler as the internal `… verify`."""
	drift = _layout(tmp_path)
	_mint(drift, _seed_file(tmp_path))
	capsys.readouterr()
	rc = run_author_subcommand(["verify", "--manifest", str(drift / "manifest.json")])
	assert rc == 0
	assert "in sync" in capsys.readouterr().out
