# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Q0.5: alias canonicalization on the DECLARATION side.

`pub fn f() throws Alias -> Int` where `pub type Alias = E` (and `E` is
a `pub error`) must canonicalize to the underlying pub-error FQN, the
same way the §B fix canonicalizes catch-arm event_fqns.

Audit reference: work/cross-pkg-narrow-throws-metadata/phase0-enforcement-audit.md
Plan reference: work/cross-pkg-narrow-throws-metadata/plan.md, Step B.

Pre-slice (audit prediction): rejected with E_THROWS_NOT_ERROR_TYPE.
Post-slice: accepted; `f`'s signature has `declared_throws_event_fqns =
["m_main:E"]` (underlying, not alias).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_single_module(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> tuple[int, dict]:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	argv = ["-M", str(root), str(main_path)]
	return _run_driftc_json(argv, capsys)


def test_q05_throws_alias_resolves_to_underlying(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""`pub type Alias = E` then `pub fn f() throws Alias -> Int` must
	compile. `_resolve_declared_throws_types` consults `type_aliases`
	after the direct + bare-name fallbacks miss, walks the alias chain,
	and resolves to the underlying pub-error's canonical FQN."""
	source = """
module m_main;

pub error E { tag: String }
pub type Alias = E;

pub fn f() throws Alias -> Int {
	throw E(tag = "x");
}

pub fn main() nothrow -> Int {
	try {
		val n = f();
		return 99;
	} catch m_main:E(e) {
		return 0;
	}
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	if rc != 0:
		codes = [d.get("code") for d in payload.get("diagnostics", [])]
		assert "E_THROWS_NOT_ERROR_TYPE" not in codes, (
			"the alias case should be accepted post-slice -- got "
			f"E_THROWS_NOT_ERROR_TYPE: {payload.get('diagnostics', [])}"
		)
	assert rc == 0, (
		f"declaration `throws Alias` (where `pub type Alias = E`) must "
		f"compile. Diagnostics: {payload.get('diagnostics', [])}"
	)


def test_q05_positive_control_throws_underlying_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive control: writing the underlying pub-error name directly
	in the throws clause compiles today and must continue to. Locks the
	failure axis to the alias case specifically."""
	source = """
module m_main;

pub error E { tag: String }
pub type Alias = E;

pub fn f() throws E -> Int {
	throw E(tag = "x");
}

pub fn main() nothrow -> Int {
	try {
		val n = f();
		return 99;
	} catch m_main:E(e) {
		return 0;
	}
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc == 0, (
		f"positive control failed -- `throws E` (underlying name) must "
		f"compile. Diagnostics: {payload.get('diagnostics', [])}"
	)
