# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Friendly use-move diagnostic for non-Copy by-value call args.

Before 0.31.70 the MIR validator (`validate_mir_call_byvalue_moves`)
caught the violation but reported it as
`internal: MIR validation contract failure (validate_mir_call_byvalue_moves)
(MIR invariant violation: by-value arg must MoveOut non-Copy local ...)`.
The framing made user errors look like compiler bugs.

After 0.31.70 the validator emits the same friendly format as the
type-checker's value-position gate
(`cannot copy NAME: type T is not Copy (use move NAME)`), so app
teams can fix their code without filing an internal-compiler bug.

The MIR validator stays as the developer-facing safety net — if
its diagnostic ever fires for code that should have compiled
cleanly, that's a separate compiler bug.
"""
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
	args += ["--dev", "--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, dict]:
	"""Compile via driftc with an explicit entry so MIR-lowering
	(and the by-value-move validator) actually runs.  Without
	`--entry`, driftc stops after type-checking and the validator
	never fires.
	"""
	mod_root = tmp_path / "mods"
	_write_file(mod_root / "main" / "main.drift", source)
	paths = sorted(mod_root.rglob("*.drift"))
	out_bin = tmp_path / "bin"
	return _run_driftc_json(
		["-M", str(mod_root), *map(str, paths),
		 "--entry", "main::main", "-o", str(out_bin)],
		capsys,
	)


def _assert_friendly_use_move(payload: dict, *, binder: str) -> None:
	"""Common assertions for the friendly diagnostic shape:
	- compile failed
	- at least one diagnostic mentions the binder + `use move`
	- no diagnostic contains internal-compiler jargon
	"""
	diags = payload.get("diagnostics") or []
	assert diags, payload
	messages = [str(d.get("message", "")) for d in diags]
	friendly = [m for m in messages if f"use move {binder}" in m and f"'{binder}'" in m]
	assert friendly, f"expected friendly use-move diagnostic for '{binder}', got: {messages}"
	for m in messages:
		assert "internal:" not in m, f"unexpected internal-compiler jargon: {m}"
		assert "MIR validation" not in m, f"unexpected MIR validation framing: {m}"
		assert "MIR invariant" not in m, f"unexpected MIR invariant framing: {m}"


def test_owned_call_arg_non_copy_local_emits_friendly_use_move(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""App-team minimal repro: non-Copy local passed by-value to an
	owned parameter without `move`.  Expected to fail compile with
	the friendly `cannot copy ... use move handler_obj` message,
	never the `internal: MIR validation contract failure` form.
	"""
	rc, payload = _compile(
		tmp_path, capsys,
		"""
module main;

import std.json as json;

fn _merge_into(target: &mut json.JsonObject, source: json.JsonObject) nothrow -> Void {
	return;
}

pub fn main() nothrow -> Int {
	var obj = json.new_object();
	val handler_obj = json.new_object();
	_merge_into(&mut obj, handler_obj);
	return 0;
}
""".lstrip(),
	)
	assert rc != 0, payload
	_assert_friendly_use_move(payload, binder="handler_obj")


def test_match_arm_binder_non_copy_call_arg_emits_friendly_use_move(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Match-arm binder shape from the original app report
	(`match maybe_object() { Some(handler_obj) => _merge_into(obj,
	handler_obj) }`).  The binder must surface in the diagnostic
	without internal `__match_binder_<n>_` mangling.
	"""
	rc, payload = _compile(
		tmp_path, capsys,
		"""
module main;

import std.core as core;
import std.json as json;

fn _merge_into(target: &mut json.JsonObject, source: json.JsonObject) nothrow -> Void {
	return;
}

fn maybe_object() nothrow -> Optional<json.JsonObject> {
	return Optional<json.JsonObject>::Some(json.new_object());
}

pub fn main() nothrow -> Int {
	var obj = json.new_object();
	match maybe_object() {
		Some(handler_obj) => {
			_merge_into(&mut obj, handler_obj);
		},
		default => { return 1; }
	}
	return 0;
}
""".lstrip(),
	)
	assert rc != 0, payload
	_assert_friendly_use_move(payload, binder="handler_obj")
	# Defense-in-depth from `test_match_binder_diagnostic_hygiene.py`:
	# the binder must never leak the internal `__match_binder_<n>_`
	# spelling in user-facing diagnostics.
	for d in payload.get("diagnostics", []):
		assert "__match_binder_" not in str(d.get("message", ""))


def test_owned_call_arg_with_move_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive companion: the suggested fix (`move <local>`)
	makes the call compile.  Pins that the diagnostic's
	prescription is sound.
	"""
	rc, payload = _compile(
		tmp_path, capsys,
		"""
module main;

import std.json as json;

fn _merge_into(target: &mut json.JsonObject, source: json.JsonObject) nothrow -> Void {
	return;
}

pub fn main() nothrow -> Int {
	var obj = json.new_object();
	val handler_obj = json.new_object();
	_merge_into(&mut obj, move handler_obj);
	return 0;
}
""".lstrip(),
	)
	assert rc == 0, payload
