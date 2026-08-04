# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Out-of-scope names must diagnose as unknown, never rebind through history.

Phase-1 name resolution used to carry a function-wide `binding_names`
fallback: when active lexical lookup failed, it scanned every binding the
function had EVER created and rebound the unresolved HVar to the first
name match — resolving popped catch binders (and any other dead scope's
locals) to their stale types.  With the shared return authority that stale
type surfaced as a bogus `return type 'Error' does not match declared type
'Int'` INSTEAD of the required `unknown name` diagnostic (surfaced by the
codegen e2e case catch_binder_scope_leak; fixed 2026-08-04).

These pins hold the contract: binding identity or ACTIVE lexical scope
only.  Out-of-scope uses get the primary unknown-name diagnostic and no
return-mismatch cascade; in-scope binders keep resolving with their real
types.
"""
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile(tmp_path: Path, capsys, source: str) -> tuple[int, dict]:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc = driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _messages(payload: dict) -> list[str]:
	return [d.get("message", "") for d in payload.get("diagnostics", [])]


def test_post_catch_binder_is_unknown_not_stale_error(tmp_path: Path, capsys) -> None:
	# The e2e shape (catch_binder_scope_leak) pinned at the driver layer:
	# the popped catch binder must NOT rebind via function-wide history.
	rc, payload = _compile(
		tmp_path,
		capsys,
		"""
module m_main;

error EvTest {}
fn fail() -> Int {
	throw EvTest();
}

pub fn main() nothrow -> Int {
	try {
		val _ = fail();
	} catch EvTest(e) {
		val moved = move e;
	} catch {
	}
	return e;
}
""",
	)
	assert rc != 0
	msgs = _messages(payload)
	assert any("unknown name 'e'" in m for m in msgs), msgs
	# No stale-type cascade: the binder's Error type must not leak into a
	# return-compatibility diagnostic.
	assert not any("does not match declared type" in m for m in msgs), msgs


def test_block_local_after_scope_is_unknown(tmp_path: Path, capsys) -> None:
	# Non-catch companion: an ordinary block-local used after its lexical
	# scope must be unknown, not rebound to the dead binding's Int type
	# (which would compile cleanly and silently change meaning).
	rc, payload = _compile(
		tmp_path,
		capsys,
		"""
module m_main;

pub fn main() nothrow -> Int {
	if true {
		val t = 5;
		val _ = t;
	}
	return t;
}
""",
	)
	assert rc != 0
	msgs = _messages(payload)
	assert any("unknown name 't'" in m for m in msgs), msgs


def test_in_scope_catch_binder_still_resolves_as_error(tmp_path: Path, capsys) -> None:
	# In-scope positive: the typed catch binder keeps its concrete Error
	# type inside the arm, including field projection.
	rc, payload = _compile(
		tmp_path,
		capsys,
		"""
module m_main;

error EvTest {
	code: Int,
}
fn fail() -> Int {
	throw EvTest(code = 3);
}

pub fn main() nothrow -> Int {
	try {
		val _ = fail();
	} catch EvTest(e) {
		return e.code;
	} catch {
	}
	return 0;
}
""",
	)
	assert rc == 0, payload


def test_shadowing_inner_binder_wins_then_outer_restored(tmp_path: Path, capsys) -> None:
	# Shadowing positive: the inner block's `x` shadows the outer one, and
	# after the block the OUTER binding (not function-wide history order)
	# resolves.  Runs are compile-only here; type correctness is the pin.
	rc, payload = _compile(
		tmp_path,
		capsys,
		"""
module m_main;

pub fn main() nothrow -> Int {
	val x = 1;
	if true {
		val x = 2;
		val _ = x;
	}
	return x;
}
""",
	)
	assert rc == 0, payload
