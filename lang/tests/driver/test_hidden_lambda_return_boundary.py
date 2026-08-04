# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Hidden-lambda return boundary: value tails must reach return authority.

A lambda body is re-checked as its own hidden function (HiddenLambdaSpec /
LambdaFnSpec).  Reconstruction used to convert only EXPRESSION bodies to
`HReturn`; a BLOCK body's trailing value stayed an `HExprStmt`, so the
standalone `check_function(return_type=...)` typed it as a discarded
statement: interface returns never recorded their coercion (hidden MIR
lacked `ConstructIfaceValue` and full compilation failed on an SSA
Dog-vs-Speaker signature contract), a stored throwing value-match silently
returned 0 instead of its arm value, and a stored terminal-`throws` tail
overwrote the declared return with Unknown (LLVM `FnResult ok type
UNKNOWN` traceback).

These pins hold the repaired contract: one shared body normalizer converts
genuine value tails to `HReturn` (so the primary return authority types
and coerces them), while preserving statement-form matches, terminal
tails, Void, and empty bodies; and a concrete spec return type is never
overwritten by the raw tail type.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import lang.driftc.stage2.mir_nodes as M
from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.core.function_id import function_symbol
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root

ROOT = Path(__file__).resolve().parents[3]

SPEAKER_PRELUDE = """
pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}

pub struct Dog {
	pub n: Int
}

implement Speaker for Dog {
	pub fn speak(self: &Dog) nothrow -> Int {
		return self.n;
	}
}
"""

CB_BLOCK_TAIL = (
	"module repro;\n\nimport std.core as core;\n" + SPEAKER_PRELUDE + """
pub fn main() nothrow -> Int {
	val cb: core.Callback0<Speaker> = core.callback0(|| => {
		Dog(n = 7)
	});
	val speaker = cb.call();
	return speaker.speak() - 7;
}
"""
)

CB_EXPLICIT_RETURN = (
	"module repro;\n\nimport std.core as core;\n" + SPEAKER_PRELUDE + """
pub fn main() nothrow -> Int {
	val cb: core.Callback0<Speaker> = core.callback0(|| => {
		return Dog(n = 7);
	});
	val speaker = cb.call();
	return speaker.speak() - 7;
}
"""
)

CB_EXPR_BODY = (
	"module repro;\n\nimport std.core as core;\n" + SPEAKER_PRELUDE + """
pub fn main() nothrow -> Int {
	val cb: core.Callback0<Speaker> = core.callback0(|| => Dog(n = 7));
	val speaker = cb.call();
	return speaker.speak() - 7;
}
"""
)

IIFE_BLOCK_TAIL = (
	"module repro;\n" + SPEAKER_PRELUDE + """
pub fn main() nothrow -> Int {
	val speaker = (|| -> Speaker => {
		Dog(n = 7)
	})();
	return speaker.speak() - 7;
}
"""
)

CB_MOVED_LOCAL_TAIL = (
	"module repro;\n\nimport std.core as core;\n" + SPEAKER_PRELUDE + """
pub fn main() nothrow -> Int {
	val cb: core.Callback0<Speaker> = core.callback0(|| => {
		val dog = Dog(n = 7);
		move dog
	});
	val speaker = cb.call();
	return speaker.speak() - 7;
}
"""
)

CB_NONIMPLEMENTING_TAIL = """
module repro;

import std.core as core;

pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}

pub struct Cat {
	pub n: Int
}

pub fn main() nothrow -> Int {
	val cb: core.Callback0<Speaker> = core.callback0(|| => {
		Cat(n = 7)
	});
	val speaker = cb.call();
	return speaker.speak() - 7;
}
"""

STORED_THROWING_VALUE_MATCH = """
module repro;

pub error ExcA { kind: Int }

fn might(k: Int) -> Int {
	if k == 0 {
		throw ExcA(kind = 1);
	}
	return k;
}

pub fn main() nothrow -> Int {
	val f = |k: Int| -> Int => {
		match k == 0 {
			true => { might(0) },
			false => { (k + 1) },
		}
	};
	val b = try f(4) catch { 99 };
	return b;
}
"""

STORED_TERMINAL_CALL_TAIL = """
module repro;

pub error ExcA { kind: Int }

fn fail(code: Int) throws {
	throw ExcA(kind = code);
}

pub fn main() nothrow -> Int {
	val t4 = |n: Int| -> Int => { fail(n); };
	val d = try t4(7) catch { 40 };
	return d - 40;
}
"""


def _build(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess, Path]:
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


def _build_run_expect(tmp_path: Path, source: str, expected_exit: int) -> None:
	build, out = _build(tmp_path, source)
	assert build.returncode == 0, build.stderr
	assert "Traceback" not in build.stderr, build.stderr
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == expected_exit, (run.returncode, run.stderr)


IIFE_DIVERGENT_DEAD_BREAK = """
module repro;

pub error ExcA { kind: Int }

pub fn main() nothrow -> Int {
	val a = try (|n: Int| -> Int => {
		while true {
			if n > 0 { throw ExcA(kind = 1); }
			try { continue; } catch { break; }
		}
	})(1) catch { 10 };
	return a - 10;
}
"""


def test_iife_divergent_dead_break_body_finalizes(tmp_path: Path) -> None:
	# Immediate-lambda (HiddenLambdaSpec) twin of the divergent dead-break
	# shape — the THIRD `_body_is_divergent` finalize route.  Pre-fix this
	# failed at the hidden-lambda finalizer with "hidden lambda block must
	# end with a value or return" (verified against the pre-fix tree).
	_build_run_expect(tmp_path, IIFE_DIVERGENT_DEAD_BREAK, 0)


NAMED_DIVERGENT_DEAD_BREAK = """
module repro;

pub error ExcA { kind: Int }

fn f(n: Int) -> Int { while true { if n > 0 { throw ExcA(kind = 9); } try { continue; throw ExcA(kind = 1); } catch { break; } } }

pub fn main() nothrow -> Int {
	val a = try f(1) catch { 10 };
	return a - 10;
}
"""


def test_named_divergent_dead_break_body_finalizes(tmp_path: Path) -> None:
	# Named-fn twin of the dead-break divergent lambda shapes: the
	# reachability-refined checker accepts the body as divergent (the
	# `break` sits in a dead catch arm), but MIR finalize used to treat the
	# structurally-emitted after-loop block as a missing return ("missing
	# return reached MIR lowering" internal contract failure).  Divergent
	# non-Void bodies now seal that block with `Unreachable`.
	_build_run_expect(tmp_path, NAMED_DIVERGENT_DEAD_BREAK, 0)


def test_hidden_body_normalizer_structural_matrix() -> None:
	"""Structural pin for `_hidden_lambda_body` (PLAN matrix item 9):
	expression body converts, ordinary block value tail converts,
	statement-form match does not, terminal-call tail does not, and
	empty/value-less bodies do not."""
	import lang.driftc.stage1 as H
	from lang.driftc.driftc import _hidden_lambda_body

	def _lam(**kw):
		return H.HLambda(params=[], **kw)

	def _terminal_only_call(expr):
		return isinstance(expr, H.HCall) and getattr(expr.fn, "name", None) == "fail"

	# Expression body -> single HReturn.
	body = _hidden_lambda_body(
		_lam(body_expr=H.HLiteralInt(value=1)),
		wrap_value_tail=True, is_terminal_call=lambda e: False,
	)
	assert len(body.statements) == 1 and isinstance(body.statements[0], H.HReturn)

	# Ordinary block value tail -> ONLY the last statement becomes HReturn.
	block = H.HBlock(statements=[
		H.HLet(name="x", value=H.HLiteralInt(value=1)),
		H.HExprStmt(expr=H.HLiteralInt(value=2)),
	])
	body = _hidden_lambda_body(
		_lam(body_block=block), wrap_value_tail=True, is_terminal_call=lambda e: False,
	)
	assert isinstance(body.statements[0], H.HLet)
	assert isinstance(body.statements[1], H.HReturn)
	# Input block untouched (helper never mutates).
	assert isinstance(block.statements[1], H.HExprStmt)

	# Statement-form match tail -> preserved (parser flag authority).
	stmt_match = H.HMatchExpr(
		scrutinee=H.HLiteralBool(value=True),
		arms=[],
		statement_form=True,
	)
	body = _hidden_lambda_body(
		_lam(body_block=H.HBlock(statements=[H.HExprStmt(expr=stmt_match)])),
		wrap_value_tail=True, is_terminal_call=lambda e: False,
	)
	assert isinstance(body.statements[-1], H.HExprStmt)

	# Terminal-`throws` call tail -> preserved (CallInfo-predicate authority).
	term_call = H.HCall(fn=H.HVar(name="fail"), args=[])
	body = _hidden_lambda_body(
		_lam(body_block=H.HBlock(statements=[H.HExprStmt(expr=term_call)])),
		wrap_value_tail=True, is_terminal_call=_terminal_only_call,
	)
	assert isinstance(body.statements[-1], H.HExprStmt)

	# Same call with a non-terminal predicate -> converts (proves the
	# predicate, not the spelling, decides).
	body = _hidden_lambda_body(
		_lam(body_block=H.HBlock(statements=[H.HExprStmt(expr=H.HCall(fn=H.HVar(name="fail"), args=[]))])),
		wrap_value_tail=True, is_terminal_call=lambda e: False,
	)
	assert isinstance(body.statements[-1], H.HReturn)

	# Empty body, existing HReturn, and non-expression tail -> preserved.
	empty = H.HBlock(statements=[])
	assert _hidden_lambda_body(_lam(body_block=empty), wrap_value_tail=True, is_terminal_call=lambda e: False) is empty
	ret_block = H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=3))])
	assert _hidden_lambda_body(_lam(body_block=ret_block), wrap_value_tail=True, is_terminal_call=lambda e: False) is ret_block
	let_tail = H.HBlock(statements=[H.HLet(name="x", value=H.HLiteralInt(value=1))])
	assert _hidden_lambda_body(_lam(body_block=let_tail), wrap_value_tail=True, is_terminal_call=lambda e: False) is let_tail

	# Void/Unknown-return specs (wrap_value_tail=False) -> preserved.
	void_tail = H.HBlock(statements=[H.HExprStmt(expr=H.HLiteralInt(value=2))])
	assert _hidden_lambda_body(_lam(body_block=void_tail), wrap_value_tail=False, is_terminal_call=lambda e: False) is void_tail


def test_cb_block_tail_iface_coercion_reaches_hidden_mir(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(CB_BLOCK_TAIL, encoding="utf-8")
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		[src], module_paths=[tmp_path], stdlib_root=stdlib_root()
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], [d.message for d in errors]
	hidden = [
		func
		for fn_id, func in mir_funcs.items()
		if function_symbol(fn_id).split("::")[-1].startswith("__lambda_cb_")
	]
	assert len(hidden) == 1
	instructions = [instr for block in hidden[0].blocks.values() for instr in block.instructions]
	iface_ctors = [instr for instr in instructions if isinstance(instr, M.ConstructIfaceValue)]
	assert len(iface_ctors) == 1, instructions
	# The checker→MIR contract under review is the exact type pair.
	assert type_table.get(iface_ctors[0].iface_ty).name == "Speaker", iface_ctors
	assert type_table.get(iface_ctors[0].value_ty).name == "Dog", iface_ctors


def test_cb_block_tail_compiles_and_runs(tmp_path: Path) -> None:
	_build_run_expect(tmp_path, CB_BLOCK_TAIL, 0)


def test_cb_explicit_return_control(tmp_path: Path) -> None:
	_build_run_expect(tmp_path, CB_EXPLICIT_RETURN, 0)


def test_cb_expression_body_control(tmp_path: Path) -> None:
	_build_run_expect(tmp_path, CB_EXPR_BODY, 0)


def test_annotated_iife_block_tail(tmp_path: Path) -> None:
	_build_run_expect(tmp_path, IIFE_BLOCK_TAIL, 0)


def test_cb_block_tail_moved_local_control(tmp_path: Path) -> None:
	_build_run_expect(tmp_path, CB_MOVED_LOCAL_TAIL, 0)


def test_cb_nonimplementing_tail_one_clean_diagnostic(tmp_path: Path) -> None:
	build, _out = _build(tmp_path, CB_NONIMPLEMENTING_TAIL)
	assert build.returncode != 0
	err = build.stdout + build.stderr
	# EXACTLY one clean checker diagnostic — not a cascade.
	assert err.count("does not implement interface 'Speaker'") == 1, err
	assert "Traceback" not in err, err
	assert "SSA" not in err, err
	assert "contract failure" not in err, err


def test_stored_throwing_value_match_returns_arm_value(tmp_path: Path) -> None:
	# f(4) selects the false arm `(k + 1)` = 5.  The red behavior silently
	# discarded the match value (Void tail) and returned 0.
	_build_run_expect(tmp_path, STORED_THROWING_VALUE_MATCH, 5)


def test_stored_terminal_throws_tail_stays_statement_form(tmp_path: Path) -> None:
	# The lambda's declared Int spec stays authoritative; the terminal
	# `fail(n)` tail is a semantic exit, not a value.  Red behavior: the
	# spec return was overwritten with the raw Unknown tail type and LLVM
	# raised `FnResult ok type UNKNOWN` through a Python traceback.
	build, out = _build(tmp_path, STORED_TERMINAL_CALL_TAIL)
	assert build.returncode == 0, build.stderr
	err = build.stdout + build.stderr
	assert "Traceback" not in err, err
	assert "NotImplementedError" not in err, err
	assert "UNKNOWN" not in err, err
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, (run.returncode, run.stderr)
