# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Compile-level pins for the std.json Slice-2 surface (parser policy + located
decoder + canonical encoding).

Runtime behavior + the stable error-tag contract are exercised end-to-end under
memcheck by the e2e cases (lang/tests/codegen/e2e/std_json_parse_policy,
_located, _canonical, _located_invariant).  This driver test pins that the full
public surface names + type-checks with zero diagnostics, so an accidental export
or signature change is caught cheaply.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[dict]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


def test_parser_policy_surface_compiles(tmp_path, capsys) -> None:
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.json as json;
import std.core as core;

pub fn main() nothrow -> Int {
	// profiles + builder
	val _p: json.JsonParseConfig = json.permissive();
	val _s: json.JsonParseConfig = json.strict();
	val _si: json.JsonParseConfig = json.signed_ir();
	var b = json.parse_config_builder();
	b.duplicate_keys(json.DuplicateKeyPolicy::Reject());
	b.top_level(json.TopLevelPolicy::ObjectOnly());
	b.numbers(json.JsonNumberPolicy(allow_fractions = false, allow_exponents = false, allow_negative_zero = false));
	val _cfg: core.Result<json.JsonParseConfig, json.JsonErrorData> = b.build();

	val src = "{\\"a\\":1}";
	val _r1: core.Result<json.JsonNode, json.JsonErrorData> = json.parse_with_config(src, _p);
	val _r2: core.Result<json.JsonNode, json.JsonErrorData> = json.parse_strict(src);

	// located surface
	match json.parse_located(src, _s) {
		core.Result::Ok(doc) => {
			val c: json.LocatedCursor = doc.cursor();
			val _sp: json.JsonByteSpan = c.span();
			val _ptr: String = c.pointer();
			val _f = c.require_field("a");
			val _o = c.optional("a");
			var allowed: Array<String> = ["a"];
			val _u = c.forbid_unknown(allowed);
			val _d = c.discriminant("a");
			val _ai = c.as_int();
			val _ap = doc.at_pointer("/a");
		},
		core.Result::Err(_) => { }
	}
	return 0;
}
""".lstrip())
	assert rc == 0, errs
	assert errs == [], errs


def test_canonical_encode_surface_compiles(tmp_path, capsys) -> None:
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.json as json;
import std.core as core;

pub fn main() nothrow -> Int {
	val src = "{\\"a\\":1}";
	match json.parse(src) {
		core.Result::Ok(node) => {
			val _r: core.Result<String, json.JsonErrorData> = json.encode_canonical(node);
		},
		core.Result::Err(_) => { }
	}
	return 0;
}
""".lstrip())
	assert rc == 0, errs
	assert errs == [], errs
