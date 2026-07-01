# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG regression: dynamic dispatch through a borrowed interface
receiver — `(&iface).method()` and the `arc.get().method()` shape — must
work, including from a stored `Arc<Interface>` that the caller wants to
keep without paying per-call retain/release.

Current behaviour (pre-fix):
  - Checker rejects with "interface method call requires a value
    receiver (remove '&')" at lang/driftc/checker/call_resolver.py:1597.
  - Workaround would be to clone the interface out of Arc per call,
    paying an atomic refcount on every dispatch — unacceptable on a
    logger hot path.

Fix surface:
  1. Checker: allow REF<INTERFACE> receiver, route to interface
     method-resolution.
  2. HIR→MIR: emit CallIface for &Interface receivers (not a
     function-value Call).
  3. LLVM `_lower_call_iface` already handles pointer-to-fat-pointer
     loads (line 6183) — no change expected.

This file holds three tests:
  - test_call_method_on_borrowed_interface_value: bare `(&iface).method()`
    where `iface: SomeInterface` is an owned local.
  - test_call_method_through_arc_get: `arc.get().method()` where
    `arc: conc.Arc<SomeInterface>` — the std.log resolver shape.
  - test_arc_get_dispatch_has_no_atomic_refcount_ops: IR-level
    hot-path assertion that `arc.get().method()` lowers to a pure
    vtable indirect call — no `atomic_fetch_add` / `atomic_fetch_sub`
    / `arc_clone` on each dispatch.

The first two tests check the binary's exit code, so they actually
exercise runtime dispatch (not just typecheck).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main, compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_and_compile(
	tmp_path: Path,
	source: str,
	capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict, Path]:
	"""Compile `source` to an executable; return (driftc rc, json payload, exe path).
	Caller runs the binary if compilation succeeded."""
	mod_root = tmp_path / "mods"
	main_src = mod_root / "main" / "main.drift"
	_write_file(main_src, source)
	exe = tmp_path / "out"
	root = stdlib_root()
	args = [
		"-M", str(mod_root),
		str(main_src),
		"-o", str(exe),
		"--dev",
		"--json",
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload, exe


_BORROWED_IFACE_DIRECT = """
module main;

pub interface Counter {
	fn value(self: &Self) nothrow -> Int;
}

pub struct Cell {
	pub n: Int
}

implement Counter for Cell {
	pub fn value(self: &Cell) nothrow -> Int {
		return self.n;
	}
}

fn read_via_borrow(c: &Counter) nothrow -> Int {
	return c.value();
}

pub fn main() nothrow -> Int {
	var owner: Counter = Cell(n = 42);
	return read_via_borrow(&owner);
}
""".lstrip()


_BORROWED_IFACE_VIA_ARC_GET = """
module main;

import std.concurrent as conc;

pub interface Counter {
	fn value(self: &Self) nothrow -> Int;
}

pub struct Cell {
	pub n: Int
}

implement Counter for Cell {
	pub fn value(self: &Cell) nothrow -> Int {
		return self.n;
	}
}

struct Holder {
	arc: conc.Arc<Counter>
}

fn read_via_arc(h: &Holder) nothrow -> Int {
	return h.arc.get().value();
}

pub fn main() nothrow -> Int {
	val arc = conc.arc(Cell(n = 42)).as_interface<type Counter>();
	val h = Holder(arc = move arc);
	return read_via_arc(&h);
}
""".lstrip()


def test_call_method_on_borrowed_interface_value(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Calling `c.value()` where `c: &Counter` (a borrowed interface
	receiver) must dispatch through the vtable and return 42."""
	rc, payload, exe = _run_and_compile(tmp_path, _BORROWED_IFACE_DIRECT, capsys)
	diagnostics = payload.get("diagnostics", [])
	assert rc == 0, f"compile failed: rc={rc} diags={diagnostics}"
	assert diagnostics == [], f"unexpected diagnostics: {diagnostics}"
	result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert result.returncode == 42, (
		f"borrowed iface dispatch returned {result.returncode}, expected 42 "
		f"(stdout={result.stdout!r} stderr={result.stderr!r})"
	)


def test_call_method_through_arc_get(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""The std.log resolver shape: `arc.get().method()` where
	`arc.get()` returns `&Interface`. Must dispatch via vtable and
	must NOT touch the Arc's refcount per call."""
	rc, payload, exe = _run_and_compile(tmp_path, _BORROWED_IFACE_VIA_ARC_GET, capsys)
	diagnostics = payload.get("diagnostics", [])
	assert rc == 0, f"compile failed: rc={rc} diags={diagnostics}"
	assert diagnostics == [], f"unexpected diagnostics: {diagnostics}"
	result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert result.returncode == 42, (
		f"arc.get().value() dispatch returned {result.returncode}, expected 42 "
		f"(stdout={result.stdout!r} stderr={result.stderr!r})"
	)


def _compile_ir(tmp_path: Path, source: str) -> str:
	"""Compile `source` through the full pipeline and return the LLVM IR string."""
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="main::main",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], f"unexpected codegen errors: {errors}"
	return ir


def _extract_function_body(ir: str, fn_name: str) -> str | None:
	"""Extract the IR body of the function whose define line contains `fn_name`."""
	lines = ir.split("\n")
	body: list[str] = []
	in_fn = False
	for line in lines:
		if "define " in line and fn_name in line:
			in_fn = True
		if in_fn:
			body.append(line)
			if line.startswith("}"):
				return "\n".join(body)
	return None


def test_arc_get_dispatch_has_no_atomic_refcount_ops(tmp_path: Path) -> None:
	"""Hot-path contract: `arc.get().method()` on a borrowed
	`Arc<Interface>` stored in a struct field must NOT emit any
	atomic refcount increment/decrement (arc_clone, atomic_fetch_add,
	atomic_fetch_sub) inside the dispatch function body.

	This pins the performance contract that the std.log resolver
	depends on: per-emit dispatch borrows through the stored Arc and
	dispatches via vtable without retain/release."""
	ir = _compile_ir(tmp_path, _BORROWED_IFACE_VIA_ARC_GET)
	body = _extract_function_body(ir, "read_via_arc")
	assert body is not None, (
		"read_via_arc function not found in emitted IR"
	)
	# Must not contain any atomic refcount operations.
	atomic_ops = re.findall(
		r"(drift_atomic_fetch_add_int|drift_atomic_fetch_sub_int|arc.*clone)",
		body,
		re.IGNORECASE,
	)
	assert atomic_ops == [], (
		f"read_via_arc contains atomic refcount operations that violate "
		f"the no-per-call-retain/release contract: {atomic_ops}\n"
		f"Function body:\n{body}"
	)
	# Must contain a vtable dispatch (indirect call through loaded fn ptr).
	assert "call_slot" in body or "call_ptr" in body, (
		"read_via_arc does not contain expected vtable dispatch pattern"
	)
