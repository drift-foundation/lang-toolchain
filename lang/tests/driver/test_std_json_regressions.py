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
	# Every removed legacy helper gets its own INDEPENDENT primary
	# diagnostic — the migration contract.  The removed associated
	# constructors are exercised directly; the removed mutation METHODS
	# are called on VALID `json.new_array()` / `json.new_object()`
	# receivers with independent argument values, so each rejection is a
	# primary naming the method — never a cascade over a poisoned
	# receiver.  (Historically array_push/object_set were surfaced only
	# by "no matching method ... for receiver Unknown" cascades over the
	# bindings poisoned by the constructor rejections; exact causal
	# suppression correctly withholds those, so the cascade shape is
	# pinned OUT below.)
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.json as json;

pub fn main() -> Int {
	val bad_arr = json.JsonNode::new_array();
	val bad_obj = json.JsonNode::new_object();
	var arr = json.new_array();
	arr.array_push(json.JsonNode::Number("1"));
	var obj = json.new_object();
	obj.object_set("k", json.JsonNode::Null());
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	msgs = [str(d.get("message") or "") for d in payload.get("diagnostics", [])]
	assert any("new_array" in m for m in msgs), msgs
	assert any("array_push" in m for m in msgs), msgs
	assert any("new_object" in m for m in msgs), msgs
	assert any("object_set" in m for m in msgs), msgs
	# Exactly the four primaries — and no receiver-Unknown cascade noise.
	assert len(msgs) == 4, msgs
	assert not any("receiver Unknown" in m for m in msgs), msgs
