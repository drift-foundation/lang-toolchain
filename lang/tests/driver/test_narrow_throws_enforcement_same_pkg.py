# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Producer-side enforcement of `pub fn f() throws E -> T` narrow-throws
contracts in single-module compilation.

Carriers Q0.1 (body throws event outside declared set) and Q0.2 (body
calls a generic-throws callee, no catch-all). Both should be rejected
with `E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET` by the producer.

Audit reference: work/cross-pkg-narrow-throws-metadata/phase0-enforcement-audit.md
Plan reference: work/cross-pkg-narrow-throws-metadata/plan.md, Step A.
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


_DIAG_CODE = "E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET"


def test_q01_body_throws_event_outside_declared_set(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Q0.1: `pub fn f() throws E` whose body literally `throw F(...)`
	must be rejected by the producer. Audit-empirically confirmed (2026-
	05-18) to compile pre-slice with no diagnostic."""
	source = """
module m_main;

pub error E { tag: String }
pub error F { tag: String }

pub fn f() throws E -> Int {
	throw F(tag = "wrong");
}

pub fn main() nothrow -> Int {
	try {
		val n = f();
		return 99;
	} catch m_main:E(e) {
		return 0;
	} catch m_main:F(e) {
		return 1;
	}
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0, "producer must reject narrow `throws E` with body that escapes F"
	codes = [d.get("code") for d in payload.get("diagnostics", [])]
	assert _DIAG_CODE in codes, (
		f"expected {_DIAG_CODE} in producer diagnostics; got {codes}"
	)


def test_q02_body_calls_generic_callee_without_catch_all(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Q0.2: `pub fn f() throws E` whose body calls a same-pkg generic-
	throws function without a wrapping catch-all must be rejected by the
	producer."""
	source = """
module m_main;

pub error E { tag: String }
pub error OutOfScope { tag: String }

pub fn g() -> Int {
	throw OutOfScope(tag = "leak");
}

pub fn f() throws E -> Int {
	val n = g();
	return n;
}

pub fn main() nothrow -> Int {
	try {
		val n = f();
		return 99;
	} catch m_main:E(e) {
		return 0;
	} catch m_main:OutOfScope(e) {
		return 1;
	}
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0, (
		"producer must reject narrow `throws E` whose body calls a "
		"generic-throws callee without a wrapping catch-all"
	)
	codes = [d.get("code") for d in payload.get("diagnostics", [])]
	assert _DIAG_CODE in codes, (
		f"expected {_DIAG_CODE} in producer diagnostics; got {codes}"
	)


def test_q01_q02_positive_control_body_only_escapes_declared(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive control: `pub fn f() throws E` whose body throws ONLY E
	must compile. Pins that the enforcement (when it lands) doesn't
	accidentally reject legitimate narrow declarations.

	No xfail mark — this is expected to pass both pre- and post-slice."""
	source = """
module m_main;

pub error E { tag: String }

pub fn f() throws E -> Int {
	throw E(tag = "ok");
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
		"positive control failed; the enforcement must not reject "
		f"legitimate `throws E` declarations. Diagnostics: "
		f"{payload.get('diagnostics', [])}"
	)


def test_q02_positive_control_catch_all_around_generic_call(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive control: `pub fn f() throws E` whose body calls a
	generic-throws callee BUT wraps the call in a `catch _` catch-all
	must compile. The catch-all absorbs the generic throws; the
	function's declared narrow set is honored.

	No xfail mark — expected to pass both pre- and post-slice."""
	source = """
module m_main;

pub error E { tag: String }
pub error OutOfScope { tag: String }

pub fn g() -> Int {
	throw OutOfScope(tag = "leak");
}

pub fn f() throws E -> Int {
	try {
		val n = g();
		return n;
	} catch _ {
		throw E(tag = "rewrapped");
	}
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
		"positive control failed; `try { g(); } catch (e) { throw E(...) }` "
		"inside a `throws E` body must compile -- the catch-all converts "
		f"generic escape to declared E. Diagnostics: {payload.get('diagnostics', [])}"
	)
