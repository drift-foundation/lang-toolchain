# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression (bug #3, bookkeeper team): a `nothrow` lambda whose body uses a
STATEMENT-form `try { f() } catch unexpected { }` (a catch-all) was wrongly
analysed as may-throw, while the equivalent SINGLE-EXPRESSION form
`try f() catch { 0 }` cleared the effect.  The two try shapes were treated
inconsistently in the lambda can-throw analysis.

Root cause: `stmt_can_throw` for statement-form `HTry` counted the try body's
throws unconditionally, missing the catch-all check the expression-form
`HTryExpr` already had.  A catch-all arm (`catch _` / `catch unexpected` /
bare `catch`, i.e. event_fqn is None) swallows the body's throws.

Inside a `conc.spawn` nothrow closure this surfaced as
"lambda is declared nothrow but may throw" plus cascades
("callback0 expects a function value", "cannot infer spawn T").
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

_PRELUDE = (
	"module repro;\n"
	"import std.core as core;\n"
	"pub error MyExc { kind: Int }\n"
	"fn risky() -> Int { throw MyExc(kind = 1); }\n"
	"fn run_cb(cb: core.Callback0<Int>) nothrow -> Int { return cb.call(); }\n"
)


def _compile(tmp_path: Path, src: str, *, out: str, extra_imports: str = "") -> subprocess.CompletedProcess:
	full = _PRELUDE.replace("import std.core as core;\n", "import std.core as core;\n" + extra_imports) + src
	p = tmp_path / "main.drift"
	p.write_text(full, encoding="utf-8")
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(p), "--entry", "repro::main", "--target-word-bits", "64",
		"-o", str(tmp_path / out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)


def _cb_main(body: str) -> str:
	return f"fn main() nothrow -> Int {{\n\treturn run_cb(|| => {{ {body} }});\n}}\n"


def test_stmt_form_try_catch_unexpected_clears_effect(tmp_path: Path) -> None:
	r = _compile(tmp_path, _cb_main("try { risky(); } catch unexpected { } return 0;"), out="u")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "u")]).returncode == 0


def test_stmt_form_try_catch_underscore_clears_effect(tmp_path: Path) -> None:
	r = _compile(tmp_path, _cb_main("try { risky(); } catch _ { } return 0;"), out="w")
	assert r.returncode == 0, r.stderr


def test_single_expr_try_still_clears_effect(tmp_path: Path) -> None:
	# Control: the form that already worked must keep working.
	r = _compile(tmp_path, _cb_main("val y = try risky() catch { 0 }; return y;"), out="s")
	assert r.returncode == 0, r.stderr


def test_typed_catch_does_not_clear_effect(tmp_path: Path) -> None:
	# A typed (non-catch-all) catch does NOT prove all throws are handled, so the
	# lambda must stay may-throw — the fix must not over-clear.
	r = _compile(tmp_path, _cb_main("try { risky(); } catch MyExc(e) { } return 0;"), out="t")
	assert r.returncode != 0
	assert "expected to be nothrow" in r.stderr, r.stderr


def test_throwing_catch_body_does_not_clear_effect(tmp_path: Path) -> None:
	# Catch-all swallows the body throw, but a throw in the CATCH body propagates.
	r = _compile(
		tmp_path,
		_cb_main("try { risky(); } catch unexpected { throw MyExc(kind = 2); } return 0;"),
		out="c",
	)
	assert r.returncode != 0
	assert "expected to be nothrow" in r.stderr, r.stderr


def test_conc_spawn_nothrow_closure_stmt_try_compiles_and_runs(tmp_path: Path) -> None:
	# The app team's exact shape: stmt-form try/catch unexpected inside a
	# conc.spawn nothrow closure must compile (effect cleared) and run.
	main = (
		"fn main() nothrow -> Int {\n"
		"\tvar vt = conc.spawn(|| => { try { risky(); } catch unexpected { } return 7; });\n"
		"\tval r = match vt.join() {\n"
		"\t\tcore.Result::Ok(v) => { v },\n"
		"\t\tcore.Result::Err(_) => { 0 }\n"
		"\t};\n"
		"\treturn r - 7;\n}\n"
	)
	r = _compile(tmp_path, main, out="spawn", extra_imports="import std.concurrent as conc;\n")
	assert r.returncode == 0, r.stderr
	# No effect-analysis cascade in the diagnostics.
	assert "expected to be nothrow" not in r.stderr
	assert "infer spawn" not in r.stderr
	assert subprocess.run([str(tmp_path / "spawn")]).returncode == 0
