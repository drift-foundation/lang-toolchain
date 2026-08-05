# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Preflight hypothesis probes (work-only; run by explicit path).

Authorized by review-2026-08-04T21-40-37Z: probes stay in this folder, may
invoke the compiler, and must not touch shared files.  These CHARACTERIZE
current behavior for the open hypotheses; they are not red/green contracts.
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
from lang.driftc.type_checker import TypeChecker

ROOT = Path(__file__).resolve().parents[2]

PENDING_VALUE_READ = """
module repro;

pub fn main() nothrow -> Int {
	val x = 1;
	val f = | | => { x };
	val alias = f;
	return 0;
}
"""


def _driver_diags(tmp: Path, source: str) -> str:
	src = tmp / "main.drift"
	src.write_text(source, encoding="utf-8")
	cmd = [sys.executable, "-m", "lang.driftc.driftc", str(src), "--entry", "repro::main", "--target-word-bits", "64", "-o", str(tmp / "out")]
	sr = stdlib_root()
	if sr is not None:
		cmd.extend(["--stdlib-root", str(sr)])
	r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
	return f"exit={r.returncode}\n" + r.stdout + r.stderr


def test_probe_pending_value_read_diagnostic_order(tmp_path: Path) -> None:
	out = _driver_diags(tmp_path, PENDING_VALUE_READ)
	print("\n=== PENDING VALUE READ ===")
	print(out)
	assert "exit=0" not in out  # sanity: some rejection must occur


def _check(statements, preseed=None):
	table = TypeTable()
	kwargs = dict(callable_registry=CallableRegistry(), visible_modules=(0,))
	if preseed:
		kwargs.update(preseed)
	result = TypeChecker(table).check_function(
		FunctionId(module="main", name="main", ordinal=0),
		H.HBlock(statements=statements),
		**kwargs,
	)
	return table, result


def test_probe_alias_hop_current_behavior() -> None:
	# f diagnosed via bad initializer (copy of non-place) -> f Unknown with
	# a primary; alias g inherits Unknown with NO new diagnostic; use g.
	stmts = [
		H.HLet(name="f", value=H.HCopy(subject=H.HLiteralInt(value=1))),
		H.HLet(name="g", value=H.HVar(name="f")),
		H.HExprStmt(expr=H.HCall(fn=H.HVar(name="g"), args=[], kwargs=[])),
	]
	_table, result = _check(stmts)
	print("\n=== ALIAS HOP (diagnosed f -> alias g -> call g) ===")
	for d in result.diagnostics:
		print(f"  [{getattr(d, 'severity', '?')}] {d.message}")


def test_probe_hinvoke_parity_current_behavior() -> None:
	# Same diagnosed-Unknown binding consumed via HInvoke(callee=HVar):
	# does the unconditional fallback double-diagnose today?
	stmts = [
		H.HLet(name="f", value=H.HCopy(subject=H.HLiteralInt(value=1))),
		H.HExprStmt(expr=H.HInvoke(callee=H.HVar(name="f"), args=[], kwargs=[])),
	]
	_table, result = _check(stmts)
	print("\n=== HINVOKE PARITY (diagnosed f -> HInvoke f) ===")
	for d in result.diagnostics:
		print(f"  [{getattr(d, 'severity', '?')}] {d.message}")


def test_probe_hcall_same_binding_current_behavior() -> None:
	# Control: same shape through HCall(fn=HVar) — today suppressed by the
	# global predicate.
	stmts = [
		H.HLet(name="f", value=H.HCopy(subject=H.HLiteralInt(value=1))),
		H.HExprStmt(expr=H.HCall(fn=H.HVar(name="f"), args=[], kwargs=[])),
	]
	_table, result = _check(stmts)
	print("\n=== HCALL SAME BINDING (diagnosed f -> call f) ===")
	for d in result.diagnostics:
		print(f"  [{getattr(d, 'severity', '?')}] {d.message}")


def test_probe_concrete_recovery_current_behavior() -> None:
	# Pending captureless lambda resolves concrete on first call; later use
	# must be clean (no stale anything).
	stmts = [
		H.HLet(name="f", value=H.HLambda(params=[], body_expr=None, body_block=H.HBlock(statements=[H.HExprStmt(expr=H.HLiteralInt(value=7))]))),
		H.HExprStmt(expr=H.HCall(fn=H.HVar(name="f"), args=[], kwargs=[])),
		H.HExprStmt(expr=H.HCall(fn=H.HVar(name="f"), args=[], kwargs=[])),
	]
	_table, result = _check(stmts)
	print("\n=== CONCRETE RECOVERY (pending f -> call f twice) ===")
	for d in result.diagnostics:
		print(f"  [{getattr(d, 'severity', '?')}] {d.message}")
	assert result.diagnostics == [], [d.message for d in result.diagnostics]
