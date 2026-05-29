# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Memcheck pin for shared by-ref variant match: drop-bearing payloads
must drop exactly once, with no double-free and no leak.

Spec recap (A.1 release): `match &Variant` is non-consuming.  Arm
binders are shared `&Field` borrows.  Binders do not own or drop —
drop responsibility stays with the original scrutinee.

Memcheck contract:
  - When the original variant goes out of scope, its drop-bearing
    payload (here, `String` fields inside `Resp`) is released exactly
    once via the original's destructor.
  - The arm body's read of `x.msg.clone()` produces a fresh String
    via `.clone()`; that clone has its own drop responsibility but
    does *not* alias or share storage with the original payload's
    String at the runtime level — the clone is a separate allocation.
  - No dangling pointer from a binder survives past arm exit (binders
    are shared borrows, not owners).

The fixture exercises the canonical app shape (heap-seeded String
fields so `drift_string_release` is not a no-op), then runs under
valgrind --leak-check=full.

Failure modes this carrier catches:
  - Binder treated as owning copy (would double-release the original
    payload at scope exit + binder cleanup).
  - Replace-store lowering regressing under by-ref match arm bodies.
  - String_arc tombstone-then-release path mismatching the variant
    payload reference shape.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


_BY_REF_DROP_BEARING_SOURCE = """\
module main;

import std.core as core;
import std.format as fmt;

struct Resp { pub status: Int, pub msg: String }
struct AppErr { pub code: Int, pub tag: String }

fn make_ok() nothrow -> core.Result<Resp, AppErr> {
\t// Heap-seed the String fields so drift_string_release on the
\t// original payload is real, not a literal-static no-op.
\treturn core.Result::Ok(Resp(status = 1, msg = fmt.format_int(7)));
}

pub fn main() nothrow -> Int {
\tval r = make_ok();
\tval first: Int = match &r {
\t\tcore.Result::Ok(x) => { x.status },
\t\tcore.Result::Err(_) => { 0 }
\t};
\t// Repeated match — non-consuming; original `r` still owns the
\t// payload.  If binders were owning, the second match would
\t// either fail compile or double-drop the payload at end of
\t// arm 1.
\tval cloned: String = match &r {
\t\tcore.Result::Ok(x) => { x.msg.clone() },
\t\tcore.Result::Err(_) => { fmt.format_int(0) }
\t};
\tif first != 1 { return 10; }
\tif cloned.byte_length() == 0 { return 11; }
\t// `r` and `cloned` both go out of scope here; `r`'s payload
\t// (one String) and `cloned` (separate String) each release
\t// exactly once.
\treturn 0;
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str, int]:
	if shutil.which("valgrind") is None:
		import pytest
		pytest.skip("valgrind not available")
	src = tmp_path / f"{label}.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), f"[{label}] binary not produced"
	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
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
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	err_match = re.search(r"ERROR SUMMARY: (\d+) errors", vg_output)
	error_count = int(err_match.group(1)) if err_match else 0
	return definitely_lost, vg_output, error_count


def test_by_ref_match_drop_bearing_payload_no_leak_no_uaf(tmp_path: Path) -> None:
	"""Shared by-ref variant match over a drop-bearing payload (String
	fields inside `Resp`).  Repeated `match &r`, then both `r` and the
	cloned String go out of scope.  Each payload allocation must
	release exactly once."""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, _BY_REF_DROP_BEARING_SOURCE, label="by_ref_match_drop_bearing"
	)
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			"by-ref variant match arm caused invalid memory access "
			"on a drop-bearing payload. Most likely cause: arm binder "
			"is being treated as owning rather than shared borrow.\n"
			"Touch points:\n"
			"  - `lang/driftc/type_checker.py` HMatchExpr arm binder "
			"typing (`scrut_ref_mut` propagation, lines ~7176-7177)\n"
			"  - `lang/driftc/stage2/hir_to_mir.py` match-arm cleanup "
			"authoring (binders must not contribute drops)\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)
	assert lost == 0, (
		f"by-ref variant match leaked {lost} bytes. Most likely cause: "
		"original payload's String release was suppressed because the "
		"arm binder claimed ownership without actually being a drop "
		"site.\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	assert errors == 0, (
		f"valgrind reported {errors} errors but no invalid access / "
		"definite leak — likely a non-fatal ordering issue worth "
		f"investigating.\nLog tail:\n{vg_log[-1500:]}"
	)
