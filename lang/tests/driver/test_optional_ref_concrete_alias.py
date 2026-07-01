# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_concrete_optional_ref_instantiation_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	root = tmp_path / "mods"
	_write_file(
		root / "main" / "main.drift",
		"""
module main;

variant Opt<T> {
	None,
	Some(value: T),
	@tombstone Tombstone
}

struct Box(value: Int);

fn as_ref(b: &Box) nothrow -> Opt<&Box> {
	return Opt::Some(b);
}

pub fn main() nothrow -> Int {
	val b = Box(value = 7);
	val got = as_ref(&b);
	match got {
		Opt::Some(_v) => { return 0; },
		Opt::None => { return 1; }
	}
}
""".lstrip(),
	)
	paths = sorted(root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["--no-prelude", "-M", str(root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_alias_type_in_variant_field_schema_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	root = tmp_path / "mods"
	_write_file(
		root / "main" / "main.drift",
		"""
module main;

variant Opt<T> {
	None,
	Some(value: T),
	@tombstone Tombstone
}

struct Pair(a: Int, b: Int);
type PairAlias = Pair;

variant Holder {
	Item(value: PairAlias),
	@tombstone Tombstone
}

implement Holder {
	fn as_item(self: &Holder) nothrow -> Opt<&PairAlias> {
		return match self {
			Item(v) => { Opt::Some(v) },
			default => { Opt<&PairAlias>::None() }
		};
	}
}

pub fn main() nothrow -> Int {
	val h = Holder::Item(Pair(a = 1, b = 2));
	match h.as_item() {
		Opt::Some(v) => {
			if v.a == 1 and v.b == 2 {
				return 0;
			}
			return 2;
		},
		Opt::None => { return 1; }
	}
}
""".lstrip(),
	)
	paths = sorted(root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["--no-prelude", "-M", str(root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
