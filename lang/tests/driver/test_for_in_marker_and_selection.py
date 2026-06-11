# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regressions for the for-in lowering fix (by-value `Iterable`).

Two halves:

1. **HIR-boundary marker preservation** (the bug that made the first attempts
   drift): the for-in desugar marks the borrow of a freshly-bound, compiler-owned
   iterable temporary with `HBorrow.for_iter_owned_temp=True`.  Earlier the
   `HBorrow` reconstructors in `normalize_hir` (place canonicalization / borrow
   materialization) rebuilt the node by hand and silently dropped the flag, so it
   never reached type checking.  This test pins that, after `normalize_hir`,
   `for e in node.entries()` still carries `HCall.origin == "for_iter"` and an
   `HBorrow.for_iter_owned_temp == True` whose place references the generated
   `__for_iterable` binding.

2. **Trait-constrained selection diagnostics**: for-in must resolve strictly
   through `std.iter.Iterable` — an inherent/unrelated `iter()` must NOT satisfy
   it, and a non-Copy bound local (no borrow-mode Iterable, not moved) is a
   typecheck-phase error, not a codegen crash.

Run/behavioral coverage (Copy copy, non-Copy move, borrow-only array, generic
by-value, `move v`) is pinned end-to-end under memcheck by the e2e cases.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from lang.driftc import driftc as _driftc_mod
from lang.driftc.driftc import main as driftc_main
from lang.driftc.stage1 import hir_nodes as H


def _walk(node, fn) -> None:
	if node is None:
		return
	fn(node)
	if isinstance(node, (list, tuple)):
		for x in node:
			_walk(x, fn)
		return
	d = getattr(node, "__dict__", None)
	if not d:
		return
	for v in d.values():
		if isinstance(v, (list, tuple)):
			for x in v:
				if hasattr(x, "__dict__") or isinstance(x, (list, tuple)):
					_walk(x, fn)
		elif hasattr(v, "__dict__") and type(v).__name__.startswith("H"):
			_walk(v, fn)


_FOR_IN_RVALUE = """
module main;

import std.json as json;
import std.core as core;

fn cnt(n: &json.JsonNode) nothrow -> Int {
	var c = 0;
	for e in n.entries() { if e.key.byte_length() > 0 { c = c + 1; } }
	return c;
}

fn main() nothrow -> Int { return 0; }
""".lstrip()


def test_for_iter_owned_temp_marker_survives_normalize(tmp_path, monkeypatch) -> None:
	"""After `normalize_hir`, the `for e in node.entries()` borrow still carries
	`for_iter_owned_temp=True` (place → `__for_iterable…`) and the iter() call
	keeps `origin == "for_iter"`."""
	# Inspect the block IMMEDIATELY after normalize_hir, inside the spy — later
	# phases (the for_iter resolver) rewrite this borrow into an HMove in place,
	# so a post-compile walk would miss it.
	owned_borrows: list[H.HBorrow] = []
	for_iter_calls: list[object] = []
	called = {"n": 0}
	orig = _driftc_mod.normalize_hir

	def _spy(block):
		out = orig(block)
		called["n"] += 1

		def visit(n) -> None:
			if type(n).__name__ == "HBorrow" and getattr(n, "for_iter_owned_temp", False):
				owned_borrows.append(n)
			if type(n).__name__ == "HCall" and getattr(n, "origin", None) == "for_iter":
				for_iter_calls.append(n)

		_walk(out, visit)
		return out

	monkeypatch.setattr(_driftc_mod, "normalize_hir", _spy)

	src = tmp_path / "main.drift"
	src.write_text(_FOR_IN_RVALUE, encoding="utf-8")
	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])

	assert called["n"], "normalize_hir was never called"
	assert for_iter_calls, "no HCall with origin=='for_iter' after normalize"
	assert owned_borrows, (
		"the for-in owned-temp borrow lost for_iter_owned_temp through normalize_hir "
		"(HBorrow reconstructors must preserve the flag, e.g. via dataclasses.replace)"
	)
	# The borrow's place must reference the generated `__for_iterable` binding,
	# and the canonical subject must be a place expression (post-normalize).
	for b in owned_borrows:
		subj = getattr(b, "subject", None)
		assert subj is not None
		names: list[str] = []
		_walk(subj, lambda x: names.append(x.name) if getattr(x, "name", None) else None)
		assert any(nm.startswith("__for_iterable") for nm in names), (
			f"owned-temp borrow subject does not reference __for_iterable: names={names}"
		)


def _compile_codes(tmp_path: Path, capsys, source: str) -> list[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return [d.get("code") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]


_INHERENT_ITER = """
module main;

pub struct Bag { items: Array<Int> }

implement Bag {
	// Inherent iter() returning an iterable Array — but Bag does NOT implement
	// std.iter.Iterable.  for-in must reject it (trait-constrained selection).
	pub fn iter(self: &Bag) nothrow -> Array<Int> { return [1, 2]; }
}

fn main() nothrow -> Int {
	val b = Bag(items = [1, 2]);
	for x in b { return 1; }
	return 0;
}
""".lstrip()


_NON_COPY_LOCAL_NO_MOVE = """
module main;

import std.json as json;
import std.core as core;

fn cnt(n: &json.JsonNode) nothrow -> Int {
	var it = n.entries();        // JsonEntriesIter is non-Copy
	var c = 0;
	for x in it { c = c + 1; }   // bound local, no move, no borrow-mode Iterable
	return c;
}

fn main() nothrow -> Int { return 0; }
""".lstrip()


def test_inherent_iter_does_not_satisfy_for_in(tmp_path, capsys) -> None:
	"""An inherent `iter()` (even one returning an iterable) is not an
	`std.iter.Iterable` impl, so for-in reports `E-NOT-ITERABLE`."""
	codes = _compile_codes(tmp_path, capsys, _INHERENT_ITER)
	assert "E-NOT-ITERABLE" in codes, codes


def test_non_copy_bound_local_without_move_is_typecheck_error(tmp_path, capsys) -> None:
	"""A non-Copy iterator bound to a local, iterated without `move` and with no
	borrow-mode Iterable, is rejected at typecheck (`E-NOT-ITERABLE`) — not a
	clang/codegen crash."""
	codes = _compile_codes(tmp_path, capsys, _NON_COPY_LOCAL_NO_MOVE)
	assert "E-NOT-ITERABLE" in codes, codes


_UNRELATED_TRAIT_ITER = """
module main;

// A different trait that happens to define a method named `iter` — NOT
// std.iter.Iterable.  for-in must resolve strictly through Iterable, so this
// unrelated-trait `iter` must not satisfy it (required_trait_key excludes it).
pub trait Faux { fn iter(self: &Self) nothrow -> Int; }

pub variant V { A, B(n: Int) }

implement Faux for V {
	pub fn iter(self: &V) nothrow -> Int { return 0; }
}

fn mk() nothrow -> V { return V::B(1); }

fn main() nothrow -> Int {
	for x in mk() { return 1; }
	return 0;
}
""".lstrip()


def test_unrelated_trait_iter_does_not_satisfy_for_in(tmp_path, capsys) -> None:
	"""An `iter()` from an UNRELATED trait (not `std.iter.Iterable`) must not
	satisfy for-in — selection is constrained to the Iterable trait identity, so
	this reports `E-NOT-ITERABLE` (companion to the inherent-`iter()` negative)."""
	codes = _compile_codes(tmp_path, capsys, _UNRELATED_TRAIT_ITER)
	assert "E-NOT-ITERABLE" in codes, codes
