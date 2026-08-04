# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Inferred-lambda return reconciliation at the PRIMARY typing authority.

An unannotated/uncontextual lambda's return type is inferred from its body
(spec §22.1.1), which entails reconciling ALL returning paths.  The primary
`TypeChecker` used to select the candidate (value tail, else first valued
return via a post-scope AST walk) and silently ignore incompatible earlier
or later explicit returns.  Two historical rejection routes must not be
conflated: a CONTEXTUAL annotated call result (`val r: Int = f(...)`)
was already rejected by the declared-return authority at the return's
original visit; only the genuinely UNCONTEXTUAL result was accepted by
the primary inference pass and caught downstream by the 0.35.0
hidden-function re-check, against the already-wrong inferred CallInfo.

Now every `HReturn`'s ONE-PASS effective type is captured (per-lambda
collector, nested lambdas isolated by their own collectors) and reconciled
against the deterministic candidate with the stable diagnostic
`E-LAMBDA-INFERRED-RETURN-MISMATCH`.  Validation only: no re-typing, no
late coercion or LUB; Unknown suppresses (poison).

The direct tests here call `TypeChecker.check_function` ONCE and assert the
conflict is reported THERE — a driver-only negative is green for the wrong
authority (the hidden re-check).  Driver companions pin end-to-end behavior
including the absence of a duplicate hidden-pass diagnostic.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.parser import stdlib_root
from lang.driftc.type_checker import TypeChecker

ROOT = Path(__file__).resolve().parents[3]

MISMATCH_MARK = "does not match inferred lambda return type"


def _check_direct_call(lam: H.HLambda, *, arg: H.HExpr | None = None) -> tuple[TypeTable, object, H.HCall]:
	table = TypeTable()
	call = H.HCall(fn=lam, args=[arg if arg is not None else H.HLiteralBool(value=False)], kwargs=[])
	body = H.HBlock(statements=[H.HExprStmt(expr=call)])
	result = TypeChecker(table).check_function(
		FunctionId(module="main", name="main", ordinal=0),
		body,
	)
	return table, result, call


def _mismatch_messages(result: object) -> list[str]:
	return [d.message for d in result.diagnostics if MISMATCH_MARK in d.message]


def _bool_param() -> H.HParam:
	return H.HParam(name="b", type=TypeExpr(name="Bool"))


def test_prefix_return_is_reconciled_with_value_tail_in_primary_typecheck() -> None:
	lam = H.HLambda(
		params=[_bool_param()],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HIf(
				cond=H.HVar(name="b"),
				then_block=H.HBlock(statements=[
					H.HReturn(value=H.HLiteralString(value="x")),
				]),
				else_block=None,
			),
			H.HExprStmt(expr=H.HLiteralInt(value=1)),
		]),
	)
	table, result, call = _check_direct_call(lam)
	# Authority placement: the call stays inferred as Int AND the primary
	# checker emits exactly one stable mismatch.
	assert table.get(result.typed_fn.expr_types[call.node_id]).name == "Int"
	assert _mismatch_messages(result) == [
		"return type 'String' does not match inferred lambda return type 'Int'"
	]


def test_all_statement_returns_are_reconciled_in_primary_typecheck() -> None:
	lam = H.HLambda(
		params=[_bool_param()],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HIf(
				cond=H.HVar(name="b"),
				then_block=H.HBlock(statements=[
					H.HReturn(value=H.HLiteralInt(value=1)),
				]),
				else_block=H.HBlock(statements=[
					H.HReturn(value=H.HLiteralString(value="x")),
				]),
			),
		]),
	)
	table, result, call = _check_direct_call(lam)
	assert table.get(result.typed_fn.expr_types[call.node_id]).name == "Int"
	assert _mismatch_messages(result) == [
		"return type 'String' does not match inferred lambda return type 'Int'"
	]


def test_mismatch_diagnostic_code_and_span_are_stable() -> None:
	# The advertised contract: stable code E-LAMBDA-INFERRED-RETURN-MISMATCH,
	# exact message, and the diagnostic anchored at the OFFENDING return's
	# own span (not the lambda's).
	from lang.driftc.core.span import Span

	ret_span = Span(file="pin.drift", line=41, column=7)
	lam = H.HLambda(
		params=[_bool_param()],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HIf(
				cond=H.HVar(name="b"),
				then_block=H.HBlock(statements=[
					H.HReturn(value=H.HLiteralString(value="x"), loc=ret_span),
				]),
				else_block=None,
			),
			H.HExprStmt(expr=H.HLiteralInt(value=1)),
		]),
	)
	_table, result, _call = _check_direct_call(lam)
	hits = [d for d in result.diagnostics if MISMATCH_MARK in d.message]
	assert len(hits) == 1, [d.message for d in result.diagnostics]
	diag = hits[0]
	assert diag.code == "E-LAMBDA-INFERRED-RETURN-MISMATCH", diag
	assert diag.message == "return type 'String' does not match inferred lambda return type 'Int'"
	assert (diag.span.file, diag.span.line, diag.span.column) == ("pin.drift", 41, 7), diag.span


def test_bare_return_conflicts_with_valued_tail() -> None:
	lam = H.HLambda(
		params=[_bool_param()],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HIf(
				cond=H.HVar(name="b"),
				then_block=H.HBlock(statements=[
					H.HReturn(value=None),
				]),
				else_block=None,
			),
			H.HExprStmt(expr=H.HLiteralInt(value=1)),
		]),
	)
	_table, result, _call = _check_direct_call(lam)
	assert _mismatch_messages(result) == [
		"return type 'Void' does not match inferred lambda return type 'Int'"
	]


def test_unknown_return_suppresses_reconciliation_cascade() -> None:
	# The returned name does not resolve: the upstream unknown-name
	# diagnostic is preserved and NO inferred-return cascade is added.
	lam = H.HLambda(
		params=[_bool_param()],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HIf(
				cond=H.HVar(name="b"),
				then_block=H.HBlock(statements=[
					H.HReturn(value=H.HVar(name="no_such_name")),
				]),
				else_block=None,
			),
			H.HExprStmt(expr=H.HLiteralInt(value=1)),
		]),
	)
	_table, result, _call = _check_direct_call(lam)
	msgs = [d.message for d in result.diagnostics]
	assert any("unknown name 'no_such_name'" in m for m in msgs), msgs
	assert _mismatch_messages(result) == [], msgs


def test_nested_lambda_returns_do_not_enter_outer_collector() -> None:
	# Inner lambda genuinely returns String; the outer lambda's tail is
	# Int.  The inner observation must stay in the inner collector: the
	# outer lambda reconciles clean.
	inner = H.HLambda(
		params=[],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HReturn(value=H.HLiteralString(value="s")),
		]),
	)
	lam = H.HLambda(
		params=[_bool_param()],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HLet(name="g", value=inner),
			H.HExprStmt(expr=H.HLiteralInt(value=1)),
		]),
	)
	table, result, call = _check_direct_call(lam)
	assert _mismatch_messages(result) == [], [d.message for d in result.diagnostics]
	assert table.get(result.typed_fn.expr_types[call.node_id]).name == "Int"


# --- driver companions (real source through driftc) -----------------------

STMT_FORM_MATCH_MISMATCH = """
module repro;

pub fn main() nothrow -> Int {
	val f = | b: Bool | => {
		match b {
			true => { val s = "x"; return s; },
			default => { return 1; },
		}
	};
	val r = f(false);
	return 0;
}
"""

DRIVER_MIXED_PREFIX_INFERRED = """
module repro;

pub fn main() nothrow -> Int {
	val f = | b: Bool | => {
		if b { return "x"; }
		1
	};
	val result = f(false);
	return result - 1;
}
"""

DRIVER_MIXED_PREFIX_CONTEXTUAL = """
module repro;

pub fn main() nothrow -> Int {
	val f = | b: Bool | => {
		if b { return "x"; }
		1
	};
	val result: Int = f(false);
	return result - 1;
}
"""

POSITIVE_PREFIX_AGREES = """
module repro;

pub fn main() nothrow -> Int {
	val f = | b: Bool | => {
		if b { return 7; }
		1
	};
	return f(true) - 7 + f(false) - 1;
}
"""

POSITIVE_STMT_ONLY_AGREES = """
module repro;

pub fn main() nothrow -> Int {
	val f = | b: Bool | => {
		if b { return 3; } else { return 4; }
	};
	return f(true) + f(false) - 7;
}
"""

POSITIVE_STMT_MATCH_AGREES = """
module repro;

pub fn main() nothrow -> Int {
	val f = | b: Bool | => {
		match b {
			true => { val s = 5; return s; },
			default => { return 2; },
		}
	};
	return f(true) + f(false) - 7;
}
"""

POSITIVE_NESTED_ISOLATION = """
module repro;

pub fn main() nothrow -> Int {
	val outer = | b: Bool | => {
		val s = (|| => { return "s"; })();
		(s.byte_length() - 1)
	};
	return outer(true);
}
"""

POSITIVE_ALL_BARE_VOID = """
module repro;

pub fn main() nothrow -> Int {
	var n = 0;
	val f = | b: Bool | => {
		if b { return; }
		return;
	};
	f(true);
	f(false);
	return n;
}
"""


def _driver_build(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess, Path]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "repro"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc", str(src),
		"--entry", "repro::main", "--target-word-bits", "64", "-o", str(out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	build = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	return build, out


def _driver_run_expect_zero(tmp_path: Path, source: str) -> None:
	build, out = _driver_build(tmp_path, source)
	assert build.returncode == 0, build.stderr
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, (run.returncode, run.stderr)


def _assert_single_primary_mismatch(build: subprocess.CompletedProcess) -> None:
	assert build.returncode != 0
	err = build.stdout + build.stderr
	assert err.count(MISMATCH_MARK) == 1, err
	# The primary authority owns the rejection: no hidden-pass duplicate,
	# no internal failure.
	assert "does not match declared type" not in err, err
	assert "Traceback" not in err, err
	assert "MIR" not in err, err
	assert "contract failure" not in err, err


def test_driver_mixed_prefix_inferred_single_primary_diagnostic(tmp_path: Path) -> None:
	# No annotation anywhere: genuinely inferred, so the PRIMARY
	# reconciliation owns the single rejection.
	build, _out = _driver_build(tmp_path, DRIVER_MIXED_PREFIX_INFERRED)
	_assert_single_primary_mismatch(build)


def test_driver_mixed_prefix_contextual_single_declared_diagnostic(tmp_path: Path) -> None:
	# `val result: Int = f(false)` supplies a CONTEXTUAL expected return to
	# the pending lambda, so this is contextual typing, not inference: the
	# valued return is checked by `_type_return_value` against Int at its
	# original visit (single declared-type diagnostic), and the inferred-
	# return reconciliation must NOT add a duplicate.
	build, _out = _driver_build(tmp_path, DRIVER_MIXED_PREFIX_CONTEXTUAL)
	assert build.returncode != 0
	err = build.stdout + build.stderr
	assert err.count("does not match declared type") == 1, err
	assert MISMATCH_MARK not in err, err
	assert "Traceback" not in err, err


def test_driver_statement_form_match_mismatch_arm_local(tmp_path: Path) -> None:
	# The mismatching String return flows through an ARM-LOCAL binding, so
	# its effective type must have been captured before the arm scope
	# popped.
	build, _out = _driver_build(tmp_path, STMT_FORM_MATCH_MISMATCH)
	_assert_single_primary_mismatch(build)


def test_driver_positive_prefix_agrees_runs(tmp_path: Path) -> None:
	_driver_run_expect_zero(tmp_path, POSITIVE_PREFIX_AGREES)


def test_driver_positive_statement_only_agrees_runs(tmp_path: Path) -> None:
	_driver_run_expect_zero(tmp_path, POSITIVE_STMT_ONLY_AGREES)


def test_driver_positive_statement_match_agrees_runs(tmp_path: Path) -> None:
	_driver_run_expect_zero(tmp_path, POSITIVE_STMT_MATCH_AGREES)


def test_driver_positive_nested_isolation_runs(tmp_path: Path) -> None:
	_driver_run_expect_zero(tmp_path, POSITIVE_NESTED_ISOLATION)


def test_driver_positive_all_bare_returns_infer_void_runs(tmp_path: Path) -> None:
	_driver_run_expect_zero(tmp_path, POSITIVE_ALL_BARE_VOID)
