from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_arc_mutex_callback_stub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.concurrency as conc;

struct State { count: Int }

fn bump(s: &mut State, v: Int) -> Int {
	s.count = s.count + v;
	return s.count;
}

fn on_signal(var sm: conc.Arc<conc.Mutex<State>>, v: Int) -> Int {
	var m = sm.get_mut();
	var g = conc.lock(m);
	return bump(conc.mutex_guard_get_mut(&mut g), v);
}

fn main() -> Int {
	var m = conc.mutex<type State>(State(count = 0));
	var sm = conc.arc(m);
	var sm1 = sm.clone();
	var sm2 = sm.clone();
	val a = on_signal(sm1, 2);
	val b = on_signal(sm2, 3);
	return a + b;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(
		["-M", str(mod_root), "--stdlib-root", str(stdlib_root()), "--dev", *map(str, paths)],
		capsys,
	)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
