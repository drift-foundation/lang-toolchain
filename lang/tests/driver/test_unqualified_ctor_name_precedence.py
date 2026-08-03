# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Unqualified variant-arm SPELLINGS must not preempt ordinary symbols.

The constructor-context fallback (E-CTOR-EXPECTED-TYPE for `Ok(v)` /
`Some(x)` used without a variant expectation) may fire only when NO ordinary
candidate owns the call: a user `fn Some(...)` or `pub struct Some` resolves
normally even though `Some` is also a std.core Optional arm name.  An early
version of the fallback reserved those spellings globally, regressing both
certified-positive shapes below.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, src: str, *, out: str) -> subprocess.CompletedProcess:
	p = tmp_path / "main.drift"
	p.write_text(src, encoding="utf-8")
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(p), "--entry", "repro::main", "--target-word-bits", "64",
		"-o", str(tmp_path / out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180))


def _compile_and_run(tmp_path: Path, src: str, *, out: str) -> None:
	r = _compile(tmp_path, src, out=out)
	assert r.returncode == 0, r.stderr
	assert "E-CTOR-EXPECTED-TYPE" not in r.stderr, r.stderr
	rr = subprocess.run([str(tmp_path / out)], capture_output=True, timeout=sanitizer_timeout(60))
	assert rr.returncode == 0, rr.stderr


def test_free_function_named_some_resolves(tmp_path: Path) -> None:
	src = (
		"module repro;\n"
		"import std.core as core;\n"
		"fn Some(x: Int) nothrow -> Int {\n"
		"\treturn x;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\treturn Some(0);\n}\n"
	)
	_compile_and_run(tmp_path, src, out="fnsome")


def test_free_function_named_ok_resolves(tmp_path: Path) -> None:
	# The strongest historical case: `Ok` was hijacked twice (first by the
	# deleted legacy source seam, then by the over-eager fallback); a user
	# function with that name must simply resolve.
	src = (
		"module repro;\n"
		"import std.core as core;\n"
		"fn Ok(x: Int) nothrow -> Int {\n"
		"\treturn x;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\treturn Ok(0);\n}\n"
	)
	_compile_and_run(tmp_path, src, out="fnok")


def test_struct_named_some_resolves(tmp_path: Path) -> None:
	src = (
		"module repro;\n"
		"import std.core as core;\n"
		"pub struct Some {\n"
		"\tpub value: Int\n"
		"}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval x: Some = Some(value = 0);\n"
		"\treturn x.value;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="structsome")


def test_no_context_ok_still_gets_ctor_diagnostic(tmp_path: Path) -> None:
	# The fallback still owns the genuinely unowned call: no user symbol, no
	# variant expectation — one clean constructor-context rejection.
	src = (
		"module repro;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval r = Ok(1);\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="noctx")
	assert r.returncode != 0
	assert "E-CTOR-EXPECTED-TYPE" in r.stderr, r.stderr
