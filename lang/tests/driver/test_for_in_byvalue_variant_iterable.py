# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression for the for-in lowering fix: a by-value `Iterable` whose source
is a VARIANT (e.g. a self-iterating iterator like `std.json.JsonEntriesIter`).

The bug: the for-in desugar wrapped an owned rvalue-temp iterable in a shared
borrow and passed the alloca pointer to `iter()`, which for a variant takes the
value BY VALUE (an LLVM value, not a by-pointer aggregate like a struct/array) —
clang then rejected `ptr` where the variant value was expected.  The fix moves
the owned temp into `iter()` so the by-value impl receives the right
representation; bound lvalue sources keep borrow-iteration.

The positive runtime behavior (sum 3+2+1 == 6, and the std.json `for e in
node.entries()` cases) is pinned end-to-end under memcheck by the e2e cases
`for_in_byvalue_variant_iterable`, `std_json_entries_for_in`.  This driver test
pins the NEGATIVE contract: genuinely non-iterable shapes still produce the
clear `E-NOT-ITERABLE` diagnostic rather than a confusing codegen failure.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	codes = [d.get("code") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, codes


_NO_ITERABLE = """
module main;

pub variant NoIter { A, B(n: Int) }

fn mk() nothrow -> NoIter { return NoIter::B(1); }

fn main() nothrow -> Int {
	for x in mk() { return 1; }
	return 0;
}
""".lstrip()


_NEXT_BUT_NO_ITERABLE = """
module main;

import std.iter as iter;

// Implements SinglePassIterator (has next()) but NOT Iterable: for-in must
// still reject it clearly — having next() alone does not make it for-in-able.
pub variant OnlyNext { Done, N(n: Int) }

implement iter.SinglePassIterator<Int> for OnlyNext {
	pub fn next(self: &mut OnlyNext) nothrow -> Optional<Int> {
		match self {
			OnlyNext::N(v) => { val c = *v; if c <= 0 { return Optional<Int>::None(); } *v = c - 1; return Optional::Some(c); },
			default => { return Optional<Int>::None(); }
		}
	}
}

fn mk() nothrow -> OnlyNext { return OnlyNext::N(2); }

fn main() nothrow -> Int {
	for x in mk() { return 1; }
	return 0;
}
""".lstrip()


def test_non_iterable_variant_reports_not_iterable(tmp_path, capsys) -> None:
	"""A by-value variant with no `Iterable` impl: for-in fails with the clear
	`E-NOT-ITERABLE` diagnostic (not a clang/codegen type error)."""
	rc, codes = _compile(tmp_path, capsys, _NO_ITERABLE)
	assert rc != 0
	assert "E-NOT-ITERABLE" in codes, codes


def test_singlepass_without_iterable_reports_not_iterable(tmp_path, capsys) -> None:
	"""Implementing `SinglePassIterator` (next()) WITHOUT `Iterable` is not
	enough for for-in — it still reports `E-NOT-ITERABLE` rather than silently
	accepting or crashing codegen."""
	rc, codes = _compile(tmp_path, capsys, _NEXT_BUT_NO_ITERABLE)
	assert rc != 0
	assert "E-NOT-ITERABLE" in codes, codes
