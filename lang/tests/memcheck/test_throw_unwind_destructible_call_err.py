# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG #102 (auto-unwind edge): a Destructible local in
scope at a CAN-THROW call site whose FnResult.Err auto-unwinds to
the surrounding try's dispatch must be dropped on that edge.

Backstory:
The first fix (commit 07048d9b) plugged the explicit-`throw` edge
inside a try body — i.e. when the user writes `throw E(...)` and the
local typed catch does not match.  That covered the shape in
`test_throw_unwind_destructible_drop.py` (in-package) and
`test_throw_unwind_cross_pkg_destructible_drop.py` (cross-package),
and matched the singular gateway shape at first read.

But 0.32.12's IR showed the singular leak comes from a FOURTH exit
path the first fix did not cover.  In `complete()`:

	try {
		var lease = _acquire_lease(self);                       // Destructible
		val conn = lease.conn().or_throw();                     // can-throw
		...
		val row = _call_operation_sp(conn, &sp, move args);     // can-throw
		if row.result_code == 1 { return ... }
		else if row.result_code == 2 { return ... }
		else { throw SingularException(...); }                  // explicit throw
	} catch managed:ManagedError(e) { ... }

The user-written `throw SingularException(...)` at the explicit
else-branch emits `lease.destroy()` correctly (commit 07048d9b).
But `_call_operation_sp` is can-throw — its FnResult.Err is lowered
into a `__bb_call_err3` block that branches to `__bb_try_dispatch`
WITHOUT emitting cleanup for `lease`.

When the stored procedure signals SingularLeaseMismatch (wrong-owner),
the RPC layer surfaces it as FnResult::Err — the auto-unwind path
fires, jumps to dispatch, the typed catch arm doesn't match
ManagedError vs SingularException, falls through to function-exit
propagation — but `lease` was never dropped on this edge.

Fix site: `_lower_can_throw_call_value` / `_lower_can_throw_call_stmt`
(both forms) in `lang/driftc/stage2/hir_to_mir.py` — before the
`Goto(dispatch)` in the `call_err` block, emit a CleanupHook scoped
to `ctx.scope_index_at_entry`, identical to the explicit-throw fix.

Repro shape: a Destructible local, then a can-throw callee whose
Err return rides the auto-unwind to a non-matching typed catch.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


_PRELUDE = """\
module main;

import std.core as core;
import std.console as console;

pub error CaughtKind {}
pub error CalleeFailed { code: Int }

pub struct Lease {
\tpub label: String,
\tpub payload: Array<Byte>,
}

implement core.Destructible for Lease {
\tpub fn destroy(var self: Lease) nothrow -> Void {
\t\tconsole.print("LEASE_DESTROYED\\n");
\t\treturn;
\t}
}

fn _make_payload() nothrow -> Array<Byte> {
\tvar a: Array<Byte> = [];
\tvar i = 0;
\twhile i < 64 {
\t\ta.push(cast<Byte>(i));
\t\ti = i + 1;
\t}
\treturn move a;
}

fn _acquire() nothrow -> Lease {
\treturn Lease(
\t\tlabel = "ACQUIRED_LABEL_PAYLOAD",
\t\tpayload = _make_payload()
\t);
}
"""


# Shape A — expression form: `val row: Int = _maybe_fail(code);`
# Pins `_lower_can_throw_call_value` (hir_to_mir.py site at line ~10267).
# This is the exact shape singular's IR showed in __bb_call_err3
# (the result of a can-throw call bound to a local via `val row = ...`).
SOURCE_EXPR_FORM = _PRELUDE + """
fn _maybe_fail(code: Int) -> Int {
\tif code == 99 {
\t\tthrow CalleeFailed(code = code);
\t}
\treturn code;
}

fn inner(code: Int) -> Int {
\ttry {
\t\tvar lease = _acquire();
\t\tval row: Int = _maybe_fail(code);
\t\treturn row + 1;
\t} catch CaughtKind(_e) {
\t}
\treturn 0;
}

pub fn main() nothrow -> Int {
\ttry {
\t\tval _r = inner(99);
\t} catch {
\t}
\treturn 0;
}
"""

# Shape B — statement form (return value discarded):
# `_maybe_fail_void(code);`  No `val` binding.  Pins the non-terminal
# branch of `_lower_can_throw_call_stmt` (hir_to_mir.py site at line ~10385).
SOURCE_STMT_FORM = _PRELUDE + """
fn _maybe_fail_void(code: Int) -> Void {
\tif code == 99 {
\t\tthrow CalleeFailed(code = code);
\t}
\treturn;
}

fn inner(code: Int) -> Int {
\ttry {
\t\tvar lease = _acquire();
\t\t_maybe_fail_void(code);
\t\treturn 0;
\t} catch CaughtKind(_e) {
\t}
\treturn 0;
}

pub fn main() nothrow -> Int {
\ttry {
\t\tval _r = inner(99);
\t} catch {
\t}
\treturn 0;
}
"""

# Shape C — terminal-throws callee: `fn f(...) throws` (no return type).
# The callee never returns normally — every invocation exits via
# exception.  Pins the `is_terminal_throws=True` branch of
# `_lower_can_throw_call_stmt` (hir_to_mir.py site at line ~10353).
SOURCE_TERMINAL_THROWS = _PRELUDE + """
fn _always_throws(code: Int) throws {
\tthrow CalleeFailed(code = code);
}

fn inner(code: Int) -> Int {
\ttry {
\t\tvar lease = _acquire();
\t\t_always_throws(code);
\t} catch CaughtKind(_e) {
\t}
\treturn 0;
}

pub fn main() nothrow -> Int {
\ttry {
\t\tval _r = inner(99);
\t} catch {
\t}
\treturn 0;
}
"""


def _build_and_memcheck(tmp_path: Path, source: str, tag: str) -> None:
	"""Compile `source`, run under valgrind, assert destroy() ran and no leaks."""
	src = tmp_path / f"main_{tag}.drift"
	src.write_text(source)
	out_bin = tmp_path / f"test_bin_{tag}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"[{tag}] compile failed: {res.stderr[:800]}"

	vg_log = tmp_path / f"valgrind_{tag}.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	indir_match = re.search(r"indirectly lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	indirectly_lost = int(indir_match.group(1).replace(",", "")) if indir_match else 0

	assert "LEASE_DESTROYED" in vg.stdout, (
		f"[{tag}] Destructible::destroy() did not run on auto-unwind.\n"
		f"stdout: {vg.stdout!r}\nstderr: {vg.stderr[-300:]!r}\n"
		f"definitely lost: {definitely_lost}; indirectly lost: {indirectly_lost}"
	)
	assert vg.returncode != 97, (
		f"[{tag}] Valgrind detected leaks on can-throw auto-unwind edge.\n"
		f"definitely lost: {definitely_lost}; indirectly lost: {indirectly_lost}\n"
		f"valgrind log (tail):\n{vg_output[-1200:]}"
	)
	assert definitely_lost == 0, f"[{tag}] definitely lost: {definitely_lost} bytes"
	assert indirectly_lost == 0, f"[{tag}] indirectly lost: {indirectly_lost} bytes"


def test_throw_unwind_destructible_call_err_expr_form(tmp_path: Path) -> None:
	"""Shape A — expression form: `val row = throws_callee(...)`.

	Pins `_lower_can_throw_call_value` (hir_to_mir.py auto-unwind edge).
	This is the IR shape singular's `__bb_call_err3` block showed."""
	assert shutil.which("valgrind") is not None, "valgrind required"
	_build_and_memcheck(tmp_path, SOURCE_EXPR_FORM, "expr")


def test_throw_unwind_destructible_call_err_stmt_form(tmp_path: Path) -> None:
	"""Shape B — statement form: `throws_callee(...);` no binding.

	Pins `_lower_can_throw_call_stmt` non-terminal branch."""
	assert shutil.which("valgrind") is not None, "valgrind required"
	_build_and_memcheck(tmp_path, SOURCE_STMT_FORM, "stmt")


def test_throw_unwind_destructible_call_err_terminal_throws(tmp_path: Path) -> None:
	"""Shape C — terminal-throws callee: `fn f(...) throws`.

	Pins `_lower_can_throw_call_stmt` `is_terminal_throws=True` branch.
	The callee never returns normally; every invocation exits via
	exception, so the ok block is unreachable."""
	assert shutil.which("valgrind") is not None, "valgrind required"
	_build_and_memcheck(tmp_path, SOURCE_TERMINAL_THROWS, "terminal")
