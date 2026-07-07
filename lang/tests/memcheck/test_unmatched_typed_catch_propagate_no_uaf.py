# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG (2026-05-18, app-team singular gateway): heap corruption
after a thrown event crosses an inner unmatched typed catch arm and
propagates to an outer catch.

Repro shape:

  pub error A {}
  pub error B {}

  fn inner() -> Int {
      try { throw A(); } catch B(e) { return 999; }
  }

  pub fn main() nothrow -> Int {
      return try inner() catch { 0 };
  }

Root cause: the MIR lowering of `try { ... } catch SomeEvent(e) { ... }`
for the no-match propagation path used `LoadLocal` (snapshot, not
consume) of the hidden `error_local` slot, then called
`_propagate_error` which emits a function-exit `CleanupHook`.  The
hook saw `error_local` as still-owning and emitted a `DropValue` that
called `drift_error_release` on the envelope -- but the SAME pointer
was simultaneously wrapped into the returned `FnResult.Err` for the
caller, who then called `drift_error_release` on the freed envelope.
Surface: `malloc(): unaligned tcache chunk detected` /
`double free or corruption (fasttop)` /
`tcache_thread_shutdown(): unaligned tcache chunk detected`.

Fix in `lang/driftc/stage2/hir_to_mir.py::_visit_stmt_HTry` (both the
event-arms-final-else and the no-arms-with-no-catch-all branches):
emit `M.MoveOut` of `error_local` at the propagation site so the
function-exit cleanup hook sees it as consumed and skips the drop.
The same pointer is then handed to `_propagate_error` (which either
stores it into an outer try's error_local or wraps it into
`FnResult.Err`) for single-owner transfer.

This test pins the no-UAF / no-double-free guarantee under valgrind.
Companion e2e fixture at
`lang/tests/codegen/e2e/unmatched_typed_catch_propagate_no_uaf/`
pins the exit-code-0 behavioral check.

The carrier exercises the smallest shape that exhibits the bug; the
upstream app-team report (singular gateway probe10) uses the same
shape with destructible payload and a helper frame, both of which
are not load-bearing for the bug.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

import pytest

ROOT = Path(__file__).resolve().parents[3]

_MINIMAL_SOURCE = """\
module main;

pub error A {}
pub error B {}

fn inner() -> Int {
\ttry { throw A(); } catch B(e) { return 999; }
}

pub fn main() nothrow -> Int {
\treturn try inner() catch { 0 };
}
"""

# Variant carriers exercising the same dispatch shape with non-empty
# payloads -- proves the fix covers Strings, Ints, and variant
# payloads (the original singular gateway report carried a
# `pub variant ErrKind { WithStr(String), Bare }` payload but the
# bug was independent of payload shape).
_STRING_PAYLOAD_SOURCE = """\
module main;

pub error MyExn { message: String }
pub error UnrelatedExn { tag: String }

fn inner() -> Int {
\ttry {
\t\tthrow MyExn(message = "boom-payload");
\t} catch UnrelatedExn(e) {
\t\treturn 999;
\t}
}

pub fn main() nothrow -> Int {
\treturn try inner() catch { 0 };
}
"""

_INT_PAYLOAD_SOURCE = """\
module main;

pub error MyExn { code: Int }
pub error UnrelatedExn { tag: Int }

fn inner() -> Int {
\ttry { throw MyExn(code = 42); } catch UnrelatedExn(e) { return 999; }
}

pub fn main() nothrow -> Int {
\treturn try inner() catch { 0 };
}
"""

# Expression-form try/catch carrier: same bug class as the statement-
# form, but lowered by `_visit_expr_HTryExpr` instead of
# `_visit_stmt_HTry`.  Both call sites needed the MoveOut fix; without
# the expression-form patch, `val x = try fail() catch B(e) { 999 }`
# would still UAF when the thrown event doesn't match the typed arm.
_EXPRESSION_FORM_SOURCE = """\
module main;

pub error A {}
pub error B {}

fn fail() -> Int { throw A(); }

fn inner() -> Int {
\tval x = try fail() catch B(e) { 999 };
\treturn x;
}

pub fn main() nothrow -> Int {
\treturn try inner() catch { 0 };
}
"""


def _build_and_check(tmp_path: Path, source: str, label: str) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / "test_bin"
	build = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert build.returncode == 0, (
		f"compile failed ({label}):\n{build.stderr[-1500:]}"
	)
	vg_log = tmp_path / "valgrind.log"
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
	# Capture the binary exit code -- pre-fix this would be a SIGABRT
	# (negative or non-zero exit code from the binary's abort), and
	# memcheck would also report Invalid read/write of the freed
	# envelope.
	assert vg.returncode != 97, (
		f"valgrind detected an error ({label}):\n{vg_output[-1500:]}"
	)
	# `errors from N contexts` should be 0; non-zero means UAF / double-
	# free / other memory errors were reported.
	err_match = re.search(r"ERROR SUMMARY: (\d+) errors", vg_output)
	error_count = int(err_match.group(1)) if err_match else -1
	assert error_count == 0, (
		f"valgrind reported {error_count} memory errors ({label}):\n"
		f"{vg_output[-1500:]}"
	)


def test_minimal_empty_pub_errors_no_double_free(tmp_path: Path) -> None:
	"""Minimal carrier: two empty `pub error` types, inner unmatched
	typed catch, outer binder-less catch-all.  Independent of payload
	shape -- this is the canonical regression."""
	_build_and_check(tmp_path, _MINIMAL_SOURCE, "minimal empty pub errors")


def test_string_payload_no_double_free(tmp_path: Path) -> None:
	"""Pub-error with String field -- exercises the original singular
	gateway report shape (modulo the variant wrapping)."""
	_build_and_check(tmp_path, _STRING_PAYLOAD_SOURCE, "string payload")


def test_int_payload_no_double_free(tmp_path: Path) -> None:
	"""Pub-error with Int field -- proves the fix covers
	non-heap-allocated payload types too."""
	_build_and_check(tmp_path, _INT_PAYLOAD_SOURCE, "int payload")


def test_expression_form_try_no_double_free(tmp_path: Path) -> None:
	"""Expression-form `val x = try fail() catch B(e) { 999 }` exhibits
	the same double-free as the statement-form when the typed catch arm
	doesn't match the thrown event.  The fix mirrors the statement-form
	patch in `_visit_expr_HTryExpr` -- both no-match propagation
	branches MoveOut the hidden error_local before calling
	`_propagate_error`."""
	_build_and_check(tmp_path, _EXPRESSION_FORM_SOURCE, "expression-form try")
