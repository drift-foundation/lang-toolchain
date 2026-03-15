# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
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


def test_conflict_detection_respects_reexport_visibility(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	root = tmp_path / "mods"
	_write_file(
		root / "m_types" / "lib.drift",
		"""
module m_types;

export { Box };

pub struct Box { pub value: Int }
""",
	)
	_write_file(
		root / "m_impl_b" / "lib.drift",
		"""
module m_impl_b;

import m_types;

pub const MARK: Int = 1;

export { MARK };

implement m_types.Box {
	pub fn tag(self: m_types.Box) -> Int { return 1; }
}
""",
	)
	_write_file(
		root / "m_impl_c" / "lib.drift",
		"""
module m_impl_c;

import m_types;

implement m_types.Box {
	pub fn tag(self: m_types.Box) -> Int { return 2; }
}
""",
	)
	_write_file(
		root / "m_a" / "lib.drift",
		"""
module m_a;

export { m_impl_b.* };
""",
	)
	_write_file(
		root / "m_main" / "main.drift",
		"""
module m_main;

import m_a;
import m_impl_c;

fn main() nothrow -> Int{
	return 0;
}
""",
	)
	paths = sorted(root.rglob("*.drift"))
	argv = ["-M", str(root), *[str(p) for p in paths]]
	rc, payload = _run_driftc_json(argv, capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	msgs = [d.get("message", "") for d in diags]
	assert any("duplicate inherent method" in m for m in msgs)


def test_trait_reexport_visibility_includes_impl_module(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	root = tmp_path / "mods"
	_write_file(
		root / "m_types" / "lib.drift",
		"""
module m_types;

export { Box };

pub struct Box { pub value: Int }
""",
	)
	_write_file(
		root / "m_impl" / "lib.drift",
		"""
module m_impl;

import m_types;

export { Show };

pub trait Show { fn show(self: m_types.Box) -> Int }

implement Show for m_types.Box {
	pub fn show(self: m_types.Box) -> Int { return self.value; }
}
""",
	)
	_write_file(
		root / "m_api" / "lib.drift",
		"""
module m_api;

export { m_impl.* };
""",
	)
	_write_file(
		root / "m_main" / "main.drift",
		"""
module m_main;

import m_api;
import m_types;

use trait m_api.Show;

fn main() nothrow -> Int{
	val b: m_types.Box = m_types.Box(value = 1);
	try {
		return b.show();
	} catch {
		return 0;
	}
}
""",
	)
	paths = sorted(root.rglob("*.drift"))
	argv = ["-M", str(root), *[str(p) for p in paths]]
	rc, payload = _run_driftc_json(argv, capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
