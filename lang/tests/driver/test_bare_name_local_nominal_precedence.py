# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""A module-local nominal outranks a unique cross-module re-export alias for an
unqualified type name (LANGUAGE_BUG).

`std.core` re-exports `core.Box<T>` via `export { std.core.box.* }`, which
registers `Box` as a unique cross-module alias.  The generic-expression
resolution paths consulted `find_unique_type_alias_by_name` whenever the
*origin module* had no exact alias of that name — BEFORE checking whether the
origin module declared its own nominal of that name.  So a user module that
imports `std.core` and declares its own `struct Box` / `variant Box<T>` had every
bare `Box` reference hijacked by the re-exported `core.Box`, yielding either a
spurious constructor-payload mismatch (`have Box, expected Box`) or an `Unknown`
return type that ICE'd at MIR lowering.

Fix: for an UNQUALIFIED name, consult the unique cross-module alias fallback only
when the origin module declares neither an exact alias NOR an exact struct /
variant / interface nominal of that name.  Applied at every
`find_unique_type_alias_by_name` site (call_resolver / type_checker
`_lower_generic_expr`, `TypeTable._eval_generic_type_expr`).  Precedence:
type param > exact local alias > exact local nominal > re-export alias > unique
nominal > forward/unknown.  Explicitly-qualified names resolve through their
qualifier only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _stdlib() -> Path:
	return stdlib_root() or (ROOT / "stdlib")


def _compile(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--stdlib-root", str(_stdlib()),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run([str(tmp_path / "bin")], capture_output=True, text=True, timeout=sanitizer_timeout(20))


def _error_codes(tmp_path: Path, source: str) -> list[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root", str(_stdlib()),
		 "--test-build-only", str(src), "--json"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(40),
	)
	payload = json.loads(out.stdout) if out.stdout.strip() else {}
	return [d.get("code") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]


def test_local_struct_box_wins_over_core_reexport(tmp_path: Path) -> None:
	"""A local non-generic `struct Box` is used despite `std.core` re-exporting
	`core.Box<T>` — no spurious `have Box, expected Box` mismatch."""
	src = """\
module main;
import std.core as core;
pub struct Box { pub n: Int }
variant Either { Some(b: Box), None }
fn main() nothrow -> Int {
	val e: Either = Either::Some(Box(n = 1));
	match e { Either::Some(b) => { return b.n; }, Either::None() => { return 0; } }
}
"""
	res = _compile(tmp_path, src)
	assert res.returncode == 0, f"local struct Box should win:\n{res.stderr[-800:]}"
	assert _run(tmp_path).returncode == 1


def test_local_generic_box_wins_compiles_and_runs(tmp_path: Path) -> None:
	"""Full compile/run AND package-consumer case: a consumer of the `std.core`
	package (which re-exports `core.Box<T>`) declares its OWN generic `Box<T>`
	used in a for-in; the local `Box<T>` wins and the program runs."""
	src = """\
module main;
import std.iter as iter;
import std.core as core;
use trait iter.SinglePassIterator;
pub variant Box<T> { Empty, One(v: T) }
implement<T> iter.SinglePassIterator<T> for Box<T> require T is core.Copy {
	pub fn next(self: &mut Box<T>) nothrow -> Optional<T> {
		match self { Box::One(x) => { val r = *x; *self = Box<T>::Empty(); return Optional::Some(r); }, default => { return Optional<T>::None(); } }
	}
}
implement<T> iter.Iterable<Box<T>, T, Box<T>> for Box<T> require T is core.Copy {
	pub fn iter(var self: Box<T>) nothrow -> Box<T> { return move self; }
}
fn mkbox() nothrow -> Box<Int> { return Box::One(5); }
fn main() nothrow -> Int {
	var a = 0;
	for x in mkbox() { a = a + x; }
	return a;
}
"""
	res = _compile(tmp_path, src)
	assert res.returncode == 0, f"local generic Box<T> should win:\n{res.stderr[-800:]}"
	assert _run(tmp_path).returncode == 5


def test_local_variant_box_wins(tmp_path: Path) -> None:
	"""A local `variant Box` (same name as the re-exported `core.Box`) wins."""
	src = """\
module main;
import std.core as core;
pub variant Box { A, B(n: Int) }
variant Holder { Has(b: Box), Nil }
fn main() nothrow -> Int {
	val h: Holder = Holder::Has(Box::B(3));
	match h { Holder::Has(b) => { match b { Box::B(n) => { return n; }, Box::A() => { return 0; } } }, Holder::Nil() => { return 9; } }
}
"""
	res = _compile(tmp_path, src)
	assert res.returncode == 0, f"local variant Box should win:\n{res.stderr[-800:]}"
	assert _run(tmp_path).returncode == 3


def test_local_interface_box_wins(tmp_path: Path) -> None:
	"""A local `interface Box` (same name as the re-exported `core.Box`) wins as
	a variant payload type."""
	src = """\
module main;
import std.core as core;
pub interface Box { fn area(self: &Self) nothrow -> Int; }
struct Sq { side: Int }
implement Box for Sq { pub fn area(self: &Sq) nothrow -> Int { return self.side * self.side; } }
variant Holder { Has(b: Box), Nil }
fn wrap(b: Box) -> Holder { return Holder::Has(move b); }
fn main() nothrow -> Int { return 0; }
"""
	res = _compile(tmp_path, src)
	assert res.returncode == 0, f"local interface Box should win:\n{res.stderr[-800:]}"


def test_local_alias_box_wins_over_reexport(tmp_path: Path) -> None:
	"""An exact local `type Box = Int` alias outranks the cross-module
	`core.Box` re-export alias."""
	src = """\
module main;
import std.core as core;
type Box = Int;
variant Holder { Has(b: Box), Nil }
fn main() nothrow -> Int {
	val h: Holder = Holder::Has(7);
	match h { Holder::Has(n) => { return n; }, Holder::Nil() => { return 0; } }
}
"""
	res = _compile(tmp_path, src)
	assert res.returncode == 0, f"local alias Box should win:\n{res.stderr[-800:]}"
	assert _run(tmp_path).returncode == 7


def test_explicit_qualified_core_box_resolves_to_stdlib(tmp_path: Path) -> None:
	"""An explicitly-qualified `core.Box<Int>` still resolves to the stdlib Box
	even when a local `Box` of a different kind exists."""
	src = """\
module main;
import std.core as core;
pub struct Box { pub n: Int }
fn main() nothrow -> Int {
	val b: core.Box<Int> = core.box<type Int>(7);
	return b.take();
}
"""
	res = _compile(tmp_path, src)
	assert res.returncode == 0, f"explicit core.Box<Int> should resolve to stdlib:\n{res.stderr[-800:]}"
	assert _run(tmp_path).returncode == 7


def test_bare_box_without_local_resolves_via_reexport_fallback(tmp_path: Path) -> None:
	"""With NO local declaration, the unique cross-module re-export alias path is
	still consulted for a bare `Box<Int>` variant payload field (the
	`_lower_generic_expr` path the fix guards): the field resolves to the same
	`core.Box<Int>` the `core.box(...)` argument produces, so the construction
	TYPE-CHECKS with no errors — proving the fix's guard does not suppress the
	fallback when there is no local nominal to outrank it.  (Asserted at
	typecheck; broader re-export-fallback coverage lives in
	`test_export_star_resolution.py` / `test_forward_nominal_reexport_instantiation.py`.)"""
	src = """\
module main;
import std.core as core;
variant Holder { Has(b: Box<Int>), Nil }
fn main() nothrow -> Int {
	val h: Holder = Holder::Has(core.box<type Int>(4));
	return 0;
}
"""
	# No errors ⇒ the bare `Box<Int>` field resolved (via the re-export fallback)
	# to the same type the argument has; a suppressed fallback would surface as
	# an Unknown / payload-mismatch error here.
	assert _error_codes(tmp_path, src) == [], "re-export fallback must still resolve bare Box<Int> when no local nominal exists"
