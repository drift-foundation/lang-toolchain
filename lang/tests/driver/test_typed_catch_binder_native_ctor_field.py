# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Typed-catch binder fed into a native variant-constructor field (LANGUAGE_BUG).

A typed `catch ErrType(e)` binder `e` has canonical type `Error` — a read-only
projection *view* over the in-flight error envelope, never the native error
struct.  `Result<T, E>::Err(e)` (or `Err(move e)`), however, expects the native
struct `E`.  The qualified variant-constructor path accepted whatever
`instantiate_sig` unified and applied only the `&T → T` rewrite, so the
`Error → E` mismatch slipped through; LLVM's `ConstructVariant` pointer autoload
then masked it by loading a struct value out of the `Error` view, producing a
double `drift_string_release` and a SIGSEGV on unwind.

The fix adds a strict post-instantiation validation at the constructor boundary
(after the `&T → T` rewrite, before `record_call_info`) using the same canonical
type-equivalence helper the struct constructor uses.  When the rejected argument
is *provably* a current-function typed-catch binder used as a whole value, the
diagnostic is specialized to `E_TYPED_CATCH_BINDER_NOT_VALUE` (advising
field-reconstruction); any other payload mismatch gets the ordinary
`E_VARIANT_CTOR_ARG_TYPE`.  The LLVM autoload is additionally restricted to
pointers proven to originate from `VariantGetFieldAddr` (defense in depth).

NOTE on the original report: it claimed recursive value types / mid-construction
were required to trigger the crash.  Reduction proved recursion is IRRELEVANT —
the minimal trigger is a typed catch + `Err(move e)` + a droppable error field.
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


def _compile(tmp_path: Path, source: str, entry: str = "main::main") -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_stdlib()),
		 str(src), "--entry", entry, "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	res = _compile(tmp_path, source)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(tmp_path / "bin")], capture_output=True, text=True, timeout=sanitizer_timeout(20))


def _error_diags(tmp_path: Path, source: str) -> list[dict]:
	"""Compile-only and return the list of error-severity diagnostics (JSON)."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root",
		 str(_stdlib()), "--test-build-only", str(src), "--json"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(40),
	)
	payload = json.loads(out.stdout) if out.stdout.strip() else {}
	return [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]


# ---------------------------------------------------------------------------
# The bug source: a typed-catch binder moved/copied into a native `E` field.
# ---------------------------------------------------------------------------

_BUG_SRC_MOVE = """\
module main;
import std.core as core;
error E { msg: String }
fn boom() -> Int { throw E(msg = "boom"); }
fn try_typed() -> core.Result<Int, E> {
	try { val t = boom(); return core.Result<Int, E>::Ok(t); }
	catch E(e) { return core.Result<Int, E>::Err(move e); }
}
pub fn main() nothrow -> Int {
	var code = 2;
	try { match try_typed() {
		core.Result::Ok(_) => { code = 1; },
		core.Result::Err(_) => { code = 0; }
	} } catch { code = 3; }
	return code;
}
"""

# Same shape but without an explicit `move` (`Err(e)`).
_BUG_SRC_PLAIN = _BUG_SRC_MOVE.replace("::Err(move e)", "::Err(e)")

# The supported remedy: reconstruct the native `E` from the binder's fields.
_FIX_SRC = _BUG_SRC_MOVE.replace("::Err(move e)", "::Err(E(msg = e.msg))")


def test_typed_catch_binder_move_into_native_field_rejected(tmp_path: Path) -> None:
	"""`Err(move e)`: rejected with the provenance-specific code, at the typecheck
	phase, error severity, pointing at the binder's span (not the whole call)."""
	diags = _error_diags(tmp_path, _BUG_SRC_MOVE)
	specific = [d for d in diags if d.get("code") == "E_TYPED_CATCH_BINDER_NOT_VALUE"]
	assert specific, f"expected E_TYPED_CATCH_BINDER_NOT_VALUE; got {[d.get('code') for d in diags]}"
	d = specific[0]
	assert d.get("phase") == "typecheck", d
	assert d.get("severity") == "error", d
	# span: the binder use on the catch-arm line (line 7), at the `move e` arg.
	assert d.get("line") == 7, d
	assert isinstance(d.get("column"), int) and d.get("column") > 0, d


def test_typed_catch_binder_plain_into_native_field_rejected(tmp_path: Path) -> None:
	"""`Err(e)` (no explicit move) is rejected the same way."""
	diags = _error_diags(tmp_path, _BUG_SRC_PLAIN)
	codes = [d.get("code") for d in diags]
	assert "E_TYPED_CATCH_BINDER_NOT_VALUE" in codes, codes


def test_reconstruct_from_fields_compiles_and_runs(tmp_path: Path) -> None:
	"""The remedy `Err(E(msg = e.msg))` compiles and runs cleanly (Err branch → 0)."""
	run = _compile_and_run(tmp_path, _FIX_SRC)
	assert run.returncode == 0, f"expected 0 (Err branch), got {run.returncode}: {run.stderr[-400:]}"


def test_result_with_error_field_err_move_binder_valid(tmp_path: Path) -> None:
	"""`Result<T, Error>::Err(move e)` — when the field type IS `Error`, the
	binder type matches and the construction remains valid (compiles + runs)."""
	src = _BUG_SRC_MOVE.replace("core.Result<Int, E>", "core.Result<Int, Error>")
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


# ---------------------------------------------------------------------------
# Characterization: the strict gate must NOT regress legitimate payloads, and
# must classify non-catch mismatches with the ordinary diagnostic.
# ---------------------------------------------------------------------------


def test_ref_string_variant_payload_accepted(tmp_path: Path) -> None:
	"""`Node::Tagged(s)` with `s: &String` and a `String` payload field — the
	existing `&T → T` rewrite still fires BEFORE the gate, so it is accepted."""
	src = """\
module main;
variant Node { Tagged(s: String), Empty }
fn mk(s: &String) -> Node { return Node::Tagged(s); }
pub fn main() nothrow -> Int {
	val owned = "hi";
	val n = mk(&owned);
	var r = 1;
	match n { Node::Tagged(_) => { r = 0; }, Node::Empty => { r = 2; } }
	return r;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_exact_interface_value_into_interface_field_accepted(tmp_path: Path) -> None:
	"""An exact interface-typed value into an interface variant field matches by
	type and is accepted (no spurious mismatch from the new gate)."""
	src = """\
module main;
interface Shape { fn area(self: &Self) nothrow -> Int; }
struct Sq { side: Int }
implement Shape for Sq { fn area(self: &Sq) nothrow -> Int { return self.side * self.side; } }
variant Holder { Has(s: Shape), Nothing }
fn wrap(s: Shape) -> Holder { return Holder::Has(move s); }
pub fn main() nothrow -> Int { return 0; }
"""
	res = _compile(tmp_path, src)
	assert res.returncode == 0, f"exact interface payload should compile:\n{res.stderr[-800:]}"


def test_concrete_implementer_into_interface_field_rejected(tmp_path: Path) -> None:
	"""A concrete implementer where the variant field is the interface is now
	rejected with the ORDINARY payload-mismatch diagnostic (not the typed-catch
	one): concrete→interface boxing is not a recorded variant coercion in v1."""
	src = """\
module main;
interface Shape { fn area(self: &Self) nothrow -> Int; }
struct Sq { side: Int }
implement Shape for Sq { fn area(self: &Sq) nothrow -> Int { return self.side * self.side; } }
variant Holder { Has(s: Shape), Nothing }
fn wrap(sq: Sq) -> Holder { return Holder::Has(sq); }
pub fn main() nothrow -> Int { return 0; }
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_VARIANT_CTOR_ARG_TYPE" in codes, codes
	assert "E_TYPED_CATCH_BINDER_NOT_VALUE" not in codes, (
		f"concrete→interface must use the ordinary diagnostic, not catch-specific: {codes}"
	)


def test_cross_function_binding_id_reuse_no_catch_provenance(tmp_path: Path) -> None:
	"""Binding ids reset per function, so `_typed_catch_binders` is function-
	scoped: a mismatch in a function WITHOUT a catch must get the ordinary
	diagnostic even though an earlier function registered a catch binder (whose
	id may numerically collide with the second function's locals)."""
	src = """\
module main;
import std.core as core;
error E { msg: String }
fn boom() -> Int { throw E(msg = "x"); }
fn with_catch() -> core.Result<Int, E> {
	try { val t = boom(); return core.Result<Int, E>::Ok(t); }
	catch E(e) { return core.Result<Int, E>::Err(E(msg = e.msg)); }
}
variant Box { Hold(n: Int), Nil }
fn other() -> Box {
	val s = "hello";
	return Box::Hold(s);
}
pub fn main() nothrow -> Int { return 0; }
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_VARIANT_CTOR_ARG_TYPE" in codes, codes
	assert "E_TYPED_CATCH_BINDER_NOT_VALUE" not in codes, (
		f"a non-catch function's mismatch must not inherit catch provenance: {codes}"
	)
