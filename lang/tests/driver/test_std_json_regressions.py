# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	root = stdlib_root()
	args = list(argv)
	if root:
		args += ["--stdlib-root", str(root)]
	args += ["--dev"]
	args += ["--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_std_json_hashmap_object_model_compiles_without_noncopy_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.json as json;
import std.containers as containers;

pub fn main() -> Int {
	var m = containers.hash_map<type String, json.JsonNode>();
	m.insert("a", json.JsonNode::Number("1"));
	val n = json.JsonNode::Object(move m);
	val k = "a";
	match n.get(k) {
		Some(_v) => {
		},
		None => {
			return 2;
		}
	}
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_std_json_legacy_node_mutation_helpers_are_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.json as json;

pub fn main() -> Int {
	var arr = json.JsonNode::new_array();
	arr.array_push(json.JsonNode::Number("1"));
	var obj = json.JsonNode::new_object();
	obj.object_set("k", move arr);
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	msgs = [str(d.get("message") or "") for d in payload.get("diagnostics", [])]
	assert any("new_array" in m for m in msgs)
	assert any("array_push" in m for m in msgs)
	assert any("new_object" in m for m in msgs)
	assert any("object_set" in m for m in msgs)
