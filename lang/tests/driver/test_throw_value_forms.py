# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: `throw` operand forms (bug #1, bookkeeper team).

Before this fix `throw` only parsed an unqualified inline constructor
`throw E(...)`.  Now it also accepts:

  - a module-qualified inline constructor: same-module `throw mod.E(...)`
    and cross-module `throw alias.E(...)`;
  - a bare local exception VALUE: `val e = E(...); throw e;` — consumed by
    an implicit move (so a heap-owning payload round-trips leak-free).

Deliberately still rejected (clear diagnostics, not silent):

  - a non-exception payload `throw <int>`;
  - a projected place `throw obj.field` (partial move-out of an aggregate is
    not a v1 feature — bind to a local first).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _env_true(name: str) -> bool:
	return os.environ.get(name, "").lower() in ("1", "true", "yes")

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, files: list[tuple[str, str]], *, entry: str, out: str) -> subprocess.CompletedProcess:
	srcs = []
	for name, text in files:
		p = tmp_path / name
		p.write_text(text, encoding="utf-8")
		srcs.append(str(p))
	stdlib = stdlib_root()
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		*srcs,
		"--entry", entry,
		"--target-word-bits", "64",
		"-o", str(tmp_path / out),
	]
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180))


_HEAP_ERR = "pub error MyExc { kind: Int, msg: String }\n"


def test_control_unqualified_inline_ctor_runs(tmp_path: Path) -> None:
	src = (
		"module repro;\n"
		"pub error MyExc { kind: Int }\n"
		"fn boom(n: Int) -> Int { throw MyExc(kind = n); }\n"
		"fn run(n: Int) nothrow -> Int { val x = try boom(n) catch { 7 }; return x; }\n"
		"fn main() nothrow -> Int { return run(3) - 7; }\n"
	)
	r = _compile(tmp_path, [("main.drift", src)], entry="repro::main", out="ctrl")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "ctrl")]).returncode == 0


def test_same_module_qualified_ctor_runs(tmp_path: Path) -> None:
	src = (
		"module repro;\n"
		"pub error MyExc { kind: Int }\n"
		"fn boom(n: Int) -> Int { throw repro.MyExc(kind = n); }\n"
		"fn run(n: Int) nothrow -> Int { val x = try boom(n) catch { 7 }; return x; }\n"
		"fn main() nothrow -> Int { return run(3) - 7; }\n"
	)
	r = _compile(tmp_path, [("main.drift", src)], entry="repro::main", out="q")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "q")]).returncode == 0


def test_cross_module_alias_qualified_ctor_runs(tmp_path: Path) -> None:
	errs = "module errs;\npub error NetError { code: Int, detail: String }\n"
	app = (
		"module app;\n"
		"import errs as e;\n"
		"fn boom(n: Int) -> Int { throw e.NetError(code = n, detail = \"down\"); }\n"
		"fn run(n: Int) nothrow -> Int { val x = try boom(n) catch { 7 }; return x; }\n"
		"fn main() nothrow -> Int { return run(3) - 7; }\n"
	)
	r = _compile(tmp_path, [("errs.drift", errs), ("app.drift", app)], entry="app::main", out="x")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "x")]).returncode == 0


def test_bare_local_value_heap_field_leak_free(tmp_path: Path) -> None:
	"""`throw e` of a String-owning error value: runs correctly AND, under
	valgrind, is leak/double-free clean (the implicit-move consume semantics)."""
	src = (
		"module repro;\n" + _HEAP_ERR +
		"fn boom(n: Int) -> Int { val e = MyExc(kind = n, msg = \"boom\"); throw e; }\n"
		"fn safe(n: Int) nothrow -> Int { val x = try boom(n) catch { 99 }; return x; }\n"
		"fn main() nothrow -> Int { var i = 0; var acc = 0; while i < 3 { acc = acc + safe(i); i = i + 1; } return acc - 297; }\n"
	)
	r = _compile(tmp_path, [("main.drift", src)], entry="repro::main", out="bare")
	assert r.returncode == 0, r.stderr
	binary = tmp_path / "bare"
	# The plain run (under ASan in the sanitizer lanes) already pins
	# leak-freeness via ASan's own exit-time leak check.
	assert subprocess.run([str(binary)]).returncode == 0
	# A sanitizer-instrumented binary CANNOT run under valgrind: ASan's
	# shadow-memory range interleaves with valgrind's mappings and the
	# process aborts at startup ("Shadow memory range interleaves with an
	# existing memory mapping. ASan cannot proceed correctly. ABORTING."),
	# returning non-zero before `main`.  The valgrind leak check below is for
	# the normal / DRIFT_MEMCHECK lanes; the sanitizer lanes cover the same
	# claim directly on the instrumented binary above.
	if _env_true("DRIFT_ASAN") or _env_true("DRIFT_UBSAN"):
		pytest.skip(
			"sanitizer-instrumented binary cannot run under valgrind "
			"(ASan shadow-memory interleave aborts at startup); the "
			"normal / memcheck lanes pin leak-freeness via valgrind"
		)
	if shutil.which("valgrind") is None:
		pytest.skip("valgrind not available")
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full", "--error-exitcode=97", str(binary)),
		capture_output=True, text=True, timeout=sanitizer_timeout(180),
	)
	assert vg.returncode == 0, f"valgrind found leaks/errors:\n{vg.stderr}"


def test_negative_non_error_payload_rejected(tmp_path: Path) -> None:
	src = (
		"module repro;\n"
		"fn boom() -> Int { val x = 5; throw x; }\n"
		"fn main() nothrow -> Int { return 0; }\n"
	)
	r = _compile(tmp_path, [("main.drift", src)], entry="repro::main", out="ne")
	assert r.returncode != 0
	assert "throw payload must be an exception value" in r.stderr, r.stderr


def test_negative_projected_place_throw_rejected(tmp_path: Path) -> None:
	src = (
		"module repro;\n" + _HEAP_ERR +
		"struct Holder { cached: MyExc }\n"
		"fn boom(h: Holder) -> Int { throw h.cached; }\n"
		"fn main() nothrow -> Int { return 0; }\n"
	)
	r = _compile(tmp_path, [("main.drift", src)], entry="repro::main", out="pp")
	assert r.returncode != 0
	assert "throw of a projected place is not supported in v1" in r.stderr, r.stderr
