# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pins for the STRICT per-fixture projection parser
(drift_corpus_audit._fixture_projection).  The projection is the reusable unit
the resumable check caches and the sum that IS the run aggregate, so a
malformed/duplicated/truncated audit file must ABORT as an infrastructure error
— never be silently absorbed as {} or double-counted.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
	"drift_corpus_audit", ROOT / "tools" / "drift_corpus_audit.py")
_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audit)

Err = _audit.CorpusProjectionError


def _write(tmp_path, *lines) -> Path:
	p = tmp_path / "audit.jsonl"
	p.write_text("".join(l + "\n" for l in lines))
	return p


def _agg(body: str) -> str:
	return f"[t] {body}"


def test_single_aggregate_parsed(tmp_path):
	f = _write(tmp_path,
	           _agg('{"record": "fn", "name": "x"}'),
	           _agg('{"record": "aggregate", "c1": 3, "c2": 7}'))
	assert _audit._fixture_projection(f) == {"c1": 3, "c2": 7}


def test_zero_aggregate_aborts(tmp_path):
	f = _write(tmp_path, _agg('{"record": "fn", "name": "x"}'))
	with pytest.raises(Err, match="no aggregate record"):
		_audit._fixture_projection(f)


def test_duplicate_aggregate_aborts(tmp_path):
	f = _write(tmp_path,
	           _agg('{"record": "aggregate", "c": 1}'),
	           _agg('{"record": "aggregate", "c": 2}'))
	with pytest.raises(Err, match="duplicate would double-count"):
		_audit._fixture_projection(f)


def test_malformed_json_on_audit_line_aborts(tmp_path):
	# brace-shaped (so the audit-line regex matches) but invalid JSON — a
	# corrupt/interleaved write must ABORT, not be silently skipped.
	f = _write(tmp_path, _agg('{"record": "aggregate" "c": 1}'))   # missing comma
	with pytest.raises(Err, match="malformed audit JSON"):
		_audit._fixture_projection(f)


def test_bool_counter_value_aborts(tmp_path):
	f = _write(tmp_path, _agg('{"record": "aggregate", "c": true}'))
	with pytest.raises(Err, match="not a non-bool int"):
		_audit._fixture_projection(f)


def test_non_int_counter_value_aborts(tmp_path):
	f = _write(tmp_path, _agg('{"record": "aggregate", "c": "3"}'))
	with pytest.raises(Err, match="not a non-bool int"):
		_audit._fixture_projection(f)


def test_missing_file_aborts(tmp_path):
	with pytest.raises(Err, match="cannot read audit file"):
		_audit._fixture_projection(tmp_path / "nope.jsonl")


def test_non_audit_lines_are_ignored(tmp_path):
	f = _write(tmp_path,
	           "some plain log line without the bracket-json shape",
	           _agg('{"record": "aggregate", "c": 5}'))
	assert _audit._fixture_projection(f) == {"c": 5}
