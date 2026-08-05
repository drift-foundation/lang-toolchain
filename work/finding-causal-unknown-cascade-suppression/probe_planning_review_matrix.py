# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Planning-review probes for the five challenge questions
(review-2026-08-05T01-28-12Z).  Work-only; narrow; characterization.
"""
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


def test_q1_poisoned_call_result_chain() -> None:
	# bad = missing_name; x = bad(); x();
	stmts = [
		H.HLet(name="bad", value=H.HVar(name="missing_name")),
		H.HLet(name="x", value=H.HCall(fn=H.HVar(name="bad"), args=[], kwargs=[])),
		H.HExprStmt(expr=H.HCall(fn=H.HVar(name="x"), args=[], kwargs=[])),
	]
	_t, _c, result = _check(stmts)
	_p("Q1 poisoned call-result chain (bad=missing; x=bad(); x())", result)


def test_q2_value_positions_move_borrow_discard(tmp_path: Path) -> None:
	cases = {
		"move_pending": """
module repro;
pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val g = move f;
	return g() - 7;
}
""",
		"borrow_pending": """
module repro;
pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val r = &f;
	return 0;
}
""",
		"discarded_pending": """
module repro;
pub fn main() nothrow -> Int {
	val f = || => { 7 };
	f;
	return 0;
}
""",
		"return_arg_pending_fn_param": """
module repro;
fn takes_cb(k: Int) nothrow -> Int { return k; }
pub fn main() nothrow -> Int {
	val f = || => { 7 };
	return takes_cb(f());
}
""",
	}
	for name, src in cases.items():
		d = tmp_path / name
		d.mkdir()
		fp = d / "main.drift"
		fp.write_text(src, encoding="utf-8")
		cmd = [sys.executable, "-m", "lang.driftc.driftc", str(fp), "--entry", "repro::main", "--target-word-bits", "64", "-o", str(d / "out")]
		sr = stdlib_root()
		if sr is not None:
			cmd.extend(["--stdlib-root", str(sr)])
		r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
		print(f"\n=== Q2 {name}: exit={r.returncode} ===")
		for line in (r.stdout + r.stderr).splitlines():
			if "error" in line:
				print(f"  {line.split('error:')[-1][:150]}")


def test_q3_callback_context_direct_vs_alias(tmp_path: Path) -> None:
	cases = {
		"callback_direct_lambda": """
module repro;
import std.core as core;
pub fn main() nothrow -> Int {
	val g: core.Callback1<Int, Int> = | x: Int | => x;
	return g.call(3) - 3;
}
""",
		"callback_alias_of_pending": """
module repro;
import std.core as core;
pub fn main() nothrow -> Int {
	val f = | x: Int | => x;
	val g: core.Callback1<Int, Int> = f;
	return g.call(3) - 3;
}
""",
	}
	for name, src in cases.items():
		d = tmp_path / name
		d.mkdir()
		fp = d / "main.drift"
		fp.write_text(src, encoding="utf-8")
		cmd = [sys.executable, "-m", "lang.driftc.driftc", str(fp), "--entry", "repro::main", "--target-word-bits", "64", "-o", str(d / "out")]
		sr = stdlib_root()
		if sr is not None:
			cmd.extend(["--stdlib-root", str(sr)])
		r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
		print(f"\n=== Q3 {name}: exit={r.returncode} ===")
		for line in (r.stdout + r.stderr).splitlines():
			if "error" in line:
				print(f"  {line.split('error:')[-1][:150]}")
		if r.returncode == 0:
			run = subprocess.run([str(d / "out")], capture_output=True, text=True, timeout=60)
			print(f"  run exit={run.returncode}")


def test_q5_unconstrained_flush_spec_abi() -> None:
	# val f = | x | => x;  (never invoked) — does the flush publish a
	# LambdaFnSpec with Unknown ABI types today?
	lam = H.HLambda(
		params=[H.HParam(name="x", type=None)],
		body_expr=H.HVar(name="x"),
	)
	stmts = [H.HLet(name="f", value=lam)]
	table, checker, result = _check(stmts)
	_p("Q5 unconstrained uninvoked stored lambda", result)
	unknown = table.ensure_unknown()
	for fid, spec in checker._lambda_fn_specs.items():
		bad_params = [p for p in spec.param_types if p == unknown]
		print(f"  spec {fid}: params_unknown={len(bad_params)} ret_unknown={spec.return_type == unknown}")
