# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`--version` clean break (0.33.93): human line + drift-toolchain-info/v1.

Pins: both CLIs' human and JSON outputs; canonical single-newline JSON;
fail-closed machine-consumer rejection of malformed/wrong-schema output;
the deploy consumer's hard failure (no "unknown" fallback); and the
retirement of the pipe parser — no in-tree parser depends on `|`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.build_info import (
	BuildInfoError,
	TOOLCHAIN_INFO_FORMAT,
	canonical_json,
	parse_toolchain_info,
	toolchain_info_json,
)

ROOT = Path(__file__).resolve().parents[2]


def _run(mod: str, args: list[str]) -> subprocess.CompletedProcess:
	if mod == "drift":
		code = ("import sys\nfrom lang.drift.cli import main\n"
		        "sys.exit(main(sys.argv[1:]))")
		cmd = [sys.executable, "-c", code] + args
	else:
		cmd = [sys.executable, "-m", "lang.driftc.driftc"] + args
	return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT,
	                      timeout=60)


class TestCliOutputs:
	@pytest.mark.parametrize("tool", ["driftc", "drift"])
	def test_human_line(self, tool) -> None:
		from lang.versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
		res = _run(tool, ["--version"])
		assert res.returncode == 0
		assert res.stdout == (
			f"{tool} {DRIFTC_VERSION} (ABI {DRIFT_RT_ABI_VERSION})\n")
		assert "|" not in res.stdout

	@pytest.mark.parametrize("tool", ["driftc", "drift"])
	def test_json_output(self, tool) -> None:
		from lang.versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
		res = _run(tool, ["--version", "--json"])
		assert res.returncode == 0
		# Exactly the canonical document + one newline.
		assert res.stdout.endswith("\n") and not res.stdout.endswith("\n\n")
		tc = parse_toolchain_info(res.stdout)
		assert tc["driftc"] == DRIFTC_VERSION
		assert tc["abi"] == DRIFT_RT_ABI_VERSION
		doc = json.loads(res.stdout)
		assert doc["format"] == TOOLCHAIN_INFO_FORMAT
		assert canonical_json(doc) + "\n" == res.stdout


class TestParseToolchainInfoFailClosed:
	def test_round_trip(self) -> None:
		tc = parse_toolchain_info(toolchain_info_json(git_sha="abc") + "\n")
		assert tc["git"] == "abc"

	def test_newline_contract_exact(self) -> None:
		"""Exactly one trailing newline: zero and multiple rejected."""
		doc = toolchain_info_json()
		with pytest.raises(BuildInfoError, match="exactly one newline"):
			parse_toolchain_info(doc)
		with pytest.raises(BuildInfoError, match="exactly one newline"):
			parse_toolchain_info(doc + "\n\n")

	def test_identity_floors(self) -> None:
		"""Empty driftc version / non-positive ABI are not identities."""
		base = json.loads(toolchain_info_json())
		empty_v = dict(base)
		empty_v["toolchain"] = dict(base["toolchain"], driftc="")
		zero_abi = dict(base)
		zero_abi["toolchain"] = dict(base["toolchain"], abi=0)
		for doc, expect in ((empty_v, "non-empty version"),
		                    (zero_abi, "must be positive")):
			with pytest.raises(BuildInfoError, match=expect):
				parse_toolchain_info(canonical_json(doc) + "\n")

	@pytest.mark.parametrize("text,expect", [
		("driftc 0.33.92 | abi 22 | git x", "not valid JSON"),
		('{"format":"other/v9","toolchain":{}}', "discriminator"),
		('{"format":"drift-toolchain-info/v1"}', "exactly the keys"),
		('{"format":"drift-toolchain-info/v1","toolchain":{"driftc":"x"}}',
		 "keys must be exactly"),
		('{"format":"drift-toolchain-info/v1","toolchain":{"abi":"22",'
		 '"driftc":"x","git":"","license":"","vendor":""}}',
		 "toolchain.abi"),
	])
	def test_rejections(self, text, expect) -> None:
		with pytest.raises(BuildInfoError, match=expect):
			parse_toolchain_info(text + "\n")

	def test_noncanonical_rejected(self) -> None:
		doc = json.loads(toolchain_info_json())
		pretty = json.dumps(doc, indent=1, sort_keys=True)
		with pytest.raises(BuildInfoError, match="canonically"):
			parse_toolchain_info(pretty + "\n")


class TestDeployConsumer:
	def test_get_compiler_info_uses_json_and_fails_closed(
		self, monkeypatch, tmp_path,
	) -> None:
		from types import SimpleNamespace
		from tools.drift_deploy import drift_deploy as dd

		captured: dict = {}
		def fake_run(cmd, **kw):
			captured["cmd"] = cmd
			return SimpleNamespace(returncode=0,
			                       stdout=toolchain_info_json(git_sha="ff1") + "\n",
			                       stderr="")
		monkeypatch.setattr(dd.subprocess, "run", fake_run)
		info = dd._get_compiler_info(tmp_path / "driftc")
		assert captured["cmd"][-2:] == ["--version", "--json"]
		assert info.commit == "ff1" and info.abi > 0

		# Pipe-era output must now be a hard DeployError — no
		# "unknown" fallback.
		def pipe_run(cmd, **kw):
			return SimpleNamespace(returncode=0,
			                       stdout="driftc 0.33.92 | abi 22 | git x\n",
			                       stderr="")
		monkeypatch.setattr(dd.subprocess, "run", pipe_run)
		with pytest.raises(dd.DeployError, match="rejected"):
			dd._get_compiler_info(tmp_path / "driftc")

	def test_pipe_parser_is_gone(self) -> None:
		"""Retirement pin: no in-tree parser depends on `|`."""
		import tools.drift_deploy.provenance as prov
		assert not hasattr(prov, "parse_compiler_info")
		src = Path(prov.__file__).read_text()
		assert "| abi" not in src
