# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""P5.2 — exact-code diagnostic table for control-flow-rvalue borrows
(work/control-flow-rvalue-ownership).

Pins the FROZEN P5.1 contract by DIAGNOSTIC CODE (the e2e runner matches message
text only; codes need a driver pin). Full 36-cell rejection matrix:
**4 producers** (match / ternary / try / unsafe-block) × **3 subject shapes**
(whole / field / index) × **3 reject modes**:

- shared, source-written `&` → **exactly** `{E_REDUNDANT_ARG_BORROW}` (deleting
  `&` yields the accepted canonical bare spelling; remedy names "pass directly");
- mutable, BARE and source-written `&mut` → **exactly**
  `{E_MUT_RVALUE_ARG_BINDING_REQUIRED}` (one stable category for both spellings,
  0.34.1), and **no** diagnostic message offers a "pass directly" fix-it.

Assertions require the COMPLETE error-code set to equal the expected singleton —
"code present" is not enough. match/ternary/try compile in-process; unsafe-block
cells need `--allow-unsafe`, so they go through a subprocess.

The accepted SHARED-BARE forms are NOT all covered yet — the cfrv_* e2e set does
not span all twelve producer/shape cells; the missing accepted cells are filled
by the P5.3 runtime matrix (base+ASan+memcheck). This file pins only REJECTIONS.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


_PRELUDE = """
module m;
import std.core as core;

struct S { s: String, }
fn mkStr() nothrow -> String { return "aa" + ""; }
fn mkS() nothrow -> S { return S(s = "aa" + ""); }
fn mkArr() nothrow -> Array<String> { var a: Array<String> = []; a.push("aa" + ""); return move a; }
fn mkStr_t() throws -> String { return "aa" + ""; }
fn mkS_t() throws -> S { return S(s = "aa" + ""); }
fn mkArr_t() throws -> Array<String> { var a: Array<String> = []; a.push("aa" + ""); return move a; }
fn take(x: &String) nothrow -> Int { return x.byte_length(); }
fn takem(x: &mut String) nothrow -> Int { return x.byte_length(); }
"""

_PRODUCERS = ("match", "ternary", "try", "unsafe")
_SHAPES = ("whole", "field", "index")


def _cf(src: str, prod: str) -> str:
	if src == "match":
		return f"match true {{ true => {{ {prod}() }}, false => {{ {prod}() }} }}"
	if src == "ternary":
		return f"(true ? {prod}() : {prod}())"
	if src == "try":
		return f"(try {prod}_t() catch {{ {prod}() }})"
	if src == "unsafe":
		return f"unsafe {{ {prod}() }}"
	raise AssertionError(src)


def _proj(cf: str, shape: str) -> str:
	if shape == "whole":
		return f"({cf})"
	if shape == "field":
		return f"({cf}).s"
	if shape == "index":
		return f"({cf})[0]"
	raise AssertionError(shape)


def _producer(shape: str) -> str:
	return {"whole": "mkStr", "field": "mkS", "index": "mkArr"}[shape]


def _main(body: str) -> str:
	# `main` MUST be nothrow (entrypoint rule); the `try` cells fully catch, so
	# the body never propagates an exception.
	return _PRELUDE + f"pub fn main() nothrow -> Int {{\n\treturn {body} - 2;\n}}\n"


def _errors_inprocess(tmp_path: Path, source: str) -> list[tuple[str, str]]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exc, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src], module_paths=[tmp_path], stdlib_root=stdlib_root(), test_build_only=True)
	assert parse_diags == [], [str(d) for d in parse_diags]
	func_hirs, signatures, _ = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs, signatures=signatures, exc_env=exc, type_table=type_table,
		module_exports=module_exports, module_deps=module_deps,
		enforce_entrypoint=True, entry="m::main")
	return [(d.code, d.message) for d in checked.diagnostics if d.severity == "error"]


_CODE_RE = re.compile(r"error:\s*(?P<msg>.*?)\s*\[(?P<code>E[_-][A-Z0-9_-]+)\]")


def _errors_subprocess(tmp_path: Path, source: str) -> list[tuple[str, str]]:
	"""For unsafe-block cells: the in-process helper cannot enable unsafe mode,
	so compile via a subprocess with --allow-unsafe and parse error lines."""
	src = tmp_path / "main.drift"
	src.write_text(source)
	env = dict(os.environ)
	env["PYTHONPATH"] = "." + os.pathsep + env.get("PYTHONPATH", "")
	proc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", str(src),
		 "--entry", "m::main", "--stdlib-root", str(stdlib_root()),
		 "--allow-unsafe", "-o", str(tmp_path / "out")],
		cwd=Path.cwd(), env=env, capture_output=True, text=True, timeout=120)
	# These cells are all REJECTIONS — the compile must fail (no binary emitted).
	assert proc.returncode != 0, f"expected compile failure, got rc=0\n{proc.stdout}\n{proc.stderr}"
	out = proc.stdout + proc.stderr
	return [(m.group("code"), m.group("msg")) for m in _CODE_RE.finditer(out)]


def _errors(tmp_path: Path, src_kind: str, source: str) -> list[tuple[str, str]]:
	return _errors_subprocess(tmp_path, source) if src_kind == "unsafe" else _errors_inprocess(tmp_path, source)


_SHARED_EXPLICIT = [(src, shape) for src in _PRODUCERS for shape in _SHAPES]
_MUT_CELLS = [(src, shape, spell) for src in _PRODUCERS for shape in _SHAPES for spell in ("bare", "explicit")]


@pytest.mark.parametrize("src,shape", _SHARED_EXPLICIT)
def test_shared_explicit_borrow_is_exactly_redundant(tmp_path: Path, src: str, shape: str) -> None:
	arg = "&" + _proj(_cf(src, _producer(shape)), shape)
	errs = _errors(tmp_path, src, _main(f"take({arg})"))
	codes = {c for c, _ in errs}
	# COMPLETE code set — not mere membership — AND exactly one diagnostic
	# (a duplicate same-code diagnostic would pass a set check).
	assert len(errs) == 1, (src, shape, errs)
	assert codes == {"E_REDUNDANT_ARG_BORROW"}, (src, shape, errs)


@pytest.mark.parametrize("src,shape,spell", _MUT_CELLS)
def test_mutable_rvalue_is_exactly_bind_first_no_pass_directly(
	tmp_path: Path, src: str, shape: str, spell: str
) -> None:
	proj = _proj(_cf(src, _producer(shape)), shape)
	arg = ("&mut " + proj) if spell == "explicit" else proj
	errs = _errors(tmp_path, src, _main(f"takem({arg})"))
	codes = {c for c, _ in errs}
	assert len(errs) == 1, (src, shape, spell, errs)
	assert codes == {"E_MUT_RVALUE_ARG_BINDING_REQUIRED"}, (src, shape, spell, errs)
	# The fix-it contract: a mutable rvalue is NEVER "pass directly".
	assert not any("pass directly" in msg for _, msg in errs), (src, shape, spell, errs)
