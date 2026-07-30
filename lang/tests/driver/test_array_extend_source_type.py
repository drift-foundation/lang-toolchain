# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Array<T>.extend() source-type pins (reject-redundant-call-borrows,
review round 2 finding 3 + the LANGUAGE_BUG found in D2 review).

The e2e harness subset-matches diagnostics, so it cannot prove the
ABSENCE of a wrong diagnostic — these driver assertions pin that a
wrong-typed source reports a TYPE MISMATCH in BOTH spellings, and that
the explicit spelling is specifically NOT misread as a redundant borrow
(the D2 formal is declaration-derived, not actual-derived).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_COMMON = """\
module main;

pub fn main() nothrow -> Int {
	var dest: Array<Int> = [1, 2];
	val wrong: Array<String> = ["a"];
	dest.extend(%s);
	return dest.len;
}
"""


def _compile(tmp_path: Path, arg: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(_COMMON % arg)
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(tmp_path / "x.bin")],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


def test_bare_wrong_source_is_type_mismatch(tmp_path: Path) -> None:
	res = _compile(tmp_path, "wrong")
	err = res.stderr + res.stdout
	assert res.returncode != 0
	assert "extend() source element type mismatch" in err, err[-900:]


def test_explicit_wrong_source_is_type_mismatch_not_redundant(tmp_path: Path) -> None:
	"""`dest.extend(&wrong)`: deletion would NOT type-check, so the
	explicit borrow is not redundant — the diagnostic must be the type
	mismatch, and E_REDUNDANT_ARG_BORROW must be ABSENT."""
	res = _compile(tmp_path, "&wrong")
	err = res.stderr + res.stdout
	assert res.returncode != 0
	assert "extend() source element type mismatch" in err, err[-900:]
	assert "E_REDUNDANT_ARG_BORROW" not in err, err[-900:]
