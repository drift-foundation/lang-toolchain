# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Planning round-2 probes (review-2026-08-05T01-36-27Z, items 1-4)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.method_registry import CallableRegistry
from lang.driftc.parser import stdlib_root
import lang.driftc.type_checker as TC

ROOT = Path(__file__).resolve().parents[2]


def _check(statements):
	table = TypeTable()
	checker = TC.TypeChecker(table)
	result = checker.check_function(
		FunctionId(module="main", name="main", ordinal=0),
		H.HBlock(statements=statements),
		callable_registry=CallableRegistry(),
		visible_modules=(0,),
	)
	return table, checker, result


def _p(title, result):
	print(f"\n=== {title} ===")
	for d in result.diagnostics:
		print(f"  [{getattr(d, 'severity', '?')}] {getattr(d, 'code', None) or ''} {d.message}")


def test_r2q1_poisoned_move_and_ternary() -> None:
	# move bad -> m; m()
	stmts = [
		H.HLet(name="bad", value=H.HVar(name="missing_name")),
		H.HLet(name="m", value=H.HMove(subject=H.HVar(name="bad"))),
		H.HExprStmt(expr=H.HCall(fn=H.HVar(name="m"), args=[], kwargs=[])),
	]
	_t, _c, result = _check(stmts)
	_p("R2Q1a poisoned move (bad=missing; m=move bad; m())", result)
	# literal-selected ternary -> t; t()
	stmts2 = [
		H.HLet(name="bad", value=H.HVar(name="missing_name")),
		H.HLet(name="t", value=H.HTernary(cond=H.HLiteralBool(value=True), then_expr=H.HVar(name="bad"), else_expr=H.HVar(name="bad"))),
		H.HExprStmt(expr=H.HCall(fn=H.HVar(name="t"), args=[], kwargs=[])),
	]
	_t2, _c2, result2 = _check(stmts2)
	_p("R2Q1b poisoned literal ternary (t=(true?bad:bad); t())", result2)


def _driver(tmp: Path, name: str, source: str) -> None:
	d = tmp / name
	d.mkdir()
	fp = d / "main.drift"
	fp.write_text(source, encoding="utf-8")
	cmd = [sys.executable, "-m", "lang.driftc.driftc", str(fp), "--entry", "repro::main", "--target-word-bits", "64", "-o", str(d / "out")]
	sr = stdlib_root()
	if sr is not None:
		cmd.extend(["--stdlib-root", str(sr)])
	r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
	print(f"\n=== {name}: exit={r.returncode} ===")
	for line in (r.stdout + r.stderr).splitlines():
		if "error" in line:
			print(f"  {line.split('error:')[-1][:170]}")
	if r.returncode == 0:
		run = subprocess.run([str(d / "out")], capture_output=True, text=True, timeout=60)
		print(f"  run exit={run.returncode}")


def test_r2q2_bare_pending_hvar_argument(tmp_path: Path) -> None:
	_driver(tmp_path, "r2q2_bare_arg_callback_param", """
module repro;
import std.core as core;
fn take_cb(cb: core.Callback0<Int>) nothrow -> Int {
	return cb.call();
}
pub fn main() nothrow -> Int {
	val f = || => { 7 };
	return take_cb(f) - 7;
}
""")


def test_r2q3_borrow_contract_controls(tmp_path: Path) -> None:
	_driver(tmp_path, "r2q3_borrow_finalized", """
module repro;
pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val a = f();
	val r = &f;
	return a - 7;
}
""")
	_driver(tmp_path, "r2q3_borrow_named_fn", """
module repro;
fn seven() nothrow -> Int { return 7; }
pub fn main() nothrow -> Int {
	val r = &seven;
	return 0;
}
""")


def test_r2q4_typed_callback_let_structural(tmp_path: Path) -> None:
	# Capture main's check result for the direct typed-Callback HLet.
	captured = {}
	orig = TC.TypeChecker.check_function

	def wrap(self, fn_id, body, **kw):
		res = orig(self, fn_id, body, **kw)
		captured[(getattr(fn_id, "module", "?"), getattr(fn_id, "name", "?"))] = (res, self, body)
		return res

	TC.TypeChecker.check_function = wrap
	try:
		src = tmp_path / "main.drift"
		src.write_text("""
module repro;
import std.core as core;
pub fn main() nothrow -> Int {
	val g: core.Callback1<Int, Int> = | x: Int | => x;
	return g.call(3) - 3;
}
""", encoding="utf-8")
		sys.argv = ["driftc", "--dev", "--stdlib-root", str(ROOT / "stdlib"), str(src), "--entry", "repro::main", "-o", str(tmp_path / "bin")]
		from lang.driftc import driftc as driver
		try:
			rc = driver.main()
		except SystemExit as e:
			rc = int(e.code or 0)
	finally:
		TC.TypeChecker.check_function = orig
	print(f"\n=== R2Q4 typed-callback let structural: rc={rc} ===")
	res, checker, body = captured.get(("repro", "main"), (None, None, None))
	if res is None:
		print("  main not captured")
		return
	t = checker.type_table
	typed = res.typed_fn
	g_bids = [bid for bid, name in typed.binding_names.items() if name == "g"]
	for bid in g_bids:
		ty = typed.binding_types.get(bid)
		print(f"  g binding {bid}: type={t.get(ty).name if ty else None} kind={t.get(ty).kind if ty else None}")
	for stmt in typed.body.statements:
		if isinstance(stmt, H.HLet) and stmt.name == "g":
			print(f"  HLet g initializer node: {type(stmt.value).__name__}")
	print(f"  diagnostics: {[d.message[:80] for d in res.diagnostics]}")
