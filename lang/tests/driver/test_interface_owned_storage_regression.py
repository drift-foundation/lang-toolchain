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


def test_interface_field_ctor_accepts_concrete_impl_by_move(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

interface Sink {
	fn write(self: &Self) nothrow -> Int;
}

struct StdErrSink {
}

implement Sink for StdErrSink {
	pub fn write(self: &StdErrSink) nothrow -> Int {
		return 7;
	}
}

struct Holder {
	sink: Sink
}

fn build() nothrow -> Holder {
	return Holder(sink = StdErrSink());
}

fn main() nothrow -> Int {
	val h = build();
	return h.sink.write();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_interface_field_assignment_accepts_moved_interface_value(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

interface Sink {
	fn write(self: &Self) nothrow -> Int;
}

struct StdErrSink {
}

implement Sink for StdErrSink {
	pub fn write(self: &StdErrSink) nothrow -> Int {
		return 9;
	}
}

struct Holder {
	sink: Sink
}

implement Holder {
	pub fn set_sink(self: &mut Holder, sink: Sink) nothrow -> Void {
		self.sink = move sink;
		return;
	}
}

fn main() nothrow -> Int {
	var h = Holder(sink = StdErrSink());
	h.set_sink(StdErrSink());
	return h.sink.write();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
