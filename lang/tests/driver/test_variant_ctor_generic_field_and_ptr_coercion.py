# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Strict variant-constructor payload validation: generic struct field lowering
(LANGUAGE_BUG A) and the narrow unsafe `Ptr<T> -> &T` coercion.

These are the two producer defects exposed by the strict constructor boundary
added for the typed-catch double-free fix
(`test_typed_catch_binder_native_ctor_field.py`):

A. `resolve_variant_ctor._lower_generic_expr` instantiated generic VARIANT
   field types but returned the bare base for generic STRUCT fields, so a field
   declared `Pair<Int, String>` collapsed to `Pair<Unknown, Unknown>`.  The
   strict gate then saw a spurious mismatch against the concrete argument.

The narrow `Ptr<T> -> &T` / `Ptr<T> -> &mut T` coercion replaces a blanket
pointer-class exemption: it is permitted ONLY with identical canonical pointees,
inside an unsafe context, recorded as an explicit (non-equivalence) coercion.
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


def _compile(tmp_path: Path, source: str, *, allow_unsafe: bool = False) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	argv = [sys.executable, "-m", "lang.driftc.driftc", "--dev", "--stdlib-root", str(_stdlib())]
	if allow_unsafe:
		argv.append("--allow-unsafe")
	argv += [str(src), "--entry", "main::main", "-o", str(out_bin)]
	return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60))


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run([str(tmp_path / "bin")], capture_output=True, text=True, timeout=sanitizer_timeout(20))


def _error_codes(tmp_path: Path, source: str, *, allow_unsafe: bool = False) -> list[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	argv = [sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root", str(_stdlib())]
	if allow_unsafe:
		argv.append("--allow-unsafe")
	argv += ["--test-build-only", str(src), "--json"]
	out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(40))
	payload = json.loads(out.stdout) if out.stdout.strip() else {}
	return [d.get("code") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]


# ---------------------------------------------------------------------------
# A: generic STRUCT field is instantiated, not collapsed to Unknown.
# ---------------------------------------------------------------------------

_PAIR_PRELUDE = """\
module main;
struct Pair<K, V> { k: K, v: V }
variant Holder { Has(p: Pair<Int, String>), Nil }
"""


def test_generic_struct_field_matches_concrete_arg(tmp_path: Path) -> None:
	"""`Holder::Has(Pair<Int,String>)` compiles: the field type instantiates to
	exactly `Pair<Int,String>` and matches the concrete argument under the strict
	gate (pre-fix it was `Pair<Unknown,Unknown>` and only the removed tolerance
	let it through)."""
	src = _PAIR_PRELUDE + """\
fn mk(p: Pair<Int, String>) -> Holder { return Holder::Has(move p); }
pub fn main() nothrow -> Int { return 0; }
"""
	res = _compile(tmp_path, src)
	assert res.returncode == 0, f"generic struct field should compile:\n{res.stderr[-800:]}"


def test_generic_struct_field_rejects_mismatched_concrete_arg(tmp_path: Path) -> None:
	"""STRUCTURAL pin: the field is EXACTLY `Pair<Int,String>`, not
	`Pair<Unknown,Unknown>`.  A `Pair<Int,Bool>` argument is rejected — which
	could only happen if the field args are concrete (an `Unknown` field would
	match anything)."""
	src = _PAIR_PRELUDE + """\
fn mk(p: Pair<Int, Bool>) -> Holder { return Holder::Has(move p); }
pub fn main() nothrow -> Int { return 0; }
"""
	codes = _error_codes(tmp_path, src)
	assert "E_VARIANT_CTOR_ARG_TYPE" in codes, codes


# ---------------------------------------------------------------------------
# Narrow unsafe Ptr<T> -> &T / &mut T coercion (identical canonical pointee).
# ---------------------------------------------------------------------------


def test_ptr_to_ref_coercion_in_unsafe_compiles_and_runs(tmp_path: Path) -> None:
	"""A genuine `Ptr<S>` (from `mem.ptr_from_ref`, which returns `Ptr<T>`) into
	an `Optional<&S>::Some` payload, in an unsafe block, with identical pointee —
	accepted via the narrow coercion, compiles and runs.  (`mem.ptr_at_ref`
	returns `&T` already, so it would be an exact match and NOT exercise the
	coercion — `ptr_from_ref` is what makes the source a raw `Ptr<S>`.)"""
	src = """\
module main;
import std.mem as mem;
struct S { x: Int }
fn wrap(r: &S) nothrow -> Optional<&S> {
	unsafe { val p = mem.ptr_from_ref<type S>(r); return Optional<&S>::Some(p); }
}
pub fn main() nothrow -> Int {
	val v = S(x = 7);
	val o = wrap(v);
	var r = 1;
	match o { Optional::Some(s) => { r = s.x; }, Optional::None() => { r = 0; } }
	return r;
}
"""
	res = _compile(tmp_path, src, allow_unsafe=True)
	assert res.returncode == 0, f"unsafe Ptr->&T coercion should compile:\n{res.stderr[-1000:]}"
	run = _run(tmp_path)
	assert run.returncode == 7, f"expected 7 (S.x), got {run.returncode}: {run.stderr[-400:]}"


def test_ptr_to_ref_without_unsafe_is_rejected(tmp_path: Path) -> None:
	"""The same `Ptr<S> -> &S` conversion OUTSIDE an unsafe context is rejected:
	the coercion is unsafe-gated, not a type equivalence."""
	src = """\
module main;
import std.mem as mem;
struct S { x: Int }
variant H { Has(r: &S), Nil }
fn mk(p: mem.Ptr<S>) -> H { return H::Has(p); }
pub fn main() nothrow -> Int { return 0; }
"""
	codes = _error_codes(tmp_path, src, allow_unsafe=True)
	assert "E_VARIANT_CTOR_ARG_TYPE" in codes, codes


def test_ptr_to_ref_different_pointee_rejected(tmp_path: Path) -> None:
	"""`Ptr<Int> -> &String` (different canonical pointees) is rejected even in
	an unsafe context."""
	src = """\
module main;
import std.mem as mem;
variant H { Has(r: &String), Nil }
fn mk(h: Uint) nothrow -> H { unsafe { val p = cast<mem.Ptr<Int> >(h); return H::Has(p); } }
pub fn main() nothrow -> Int { return 0; }
"""
	codes = _error_codes(tmp_path, src, allow_unsafe=True)
	assert "E_VARIANT_CTOR_ARG_TYPE" in codes, codes


def test_ref_to_ref_pointee_mismatch_rejected(tmp_path: Path) -> None:
	"""`&Int -> &String` variant payload is rejected (pointees differ)."""
	src = """\
module main;
variant H { Has(r: &String), Nil }
fn mk(i: &Int) -> H { return H::Has(i); }
pub fn main() nothrow -> Int { return 0; }
"""
	codes = _error_codes(tmp_path, src)
	assert "E_VARIANT_CTOR_ARG_TYPE" in codes, codes


def test_shared_ref_into_mut_ref_field_rejected(tmp_path: Path) -> None:
	"""`&S -> &mut S` variant payload is rejected: the coercion permits only
	`Ptr<T> -> &T`/`&mut T`, never `&T -> &mut T`."""
	src = """\
module main;
struct S { x: Int }
variant H { Has(r: &mut S), Nil }
fn mk(s: &S) -> H { return H::Has(s); }
pub fn main() nothrow -> Int { return 0; }
"""
	codes = _error_codes(tmp_path, src)
	assert "E_VARIANT_CTOR_ARG_TYPE" in codes, codes
