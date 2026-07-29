# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression pins: thin fn-pointer values with `&T`/`&mut T`
parameters, called with a bare place argument, miscompiled.

Minimal repro (work/fnptr-ref-arg-autoborrow-miscompile/repro_minimal.drift):

	val f: Fn(&String) nothrow -> Int = read_len;
	val s: String = "hello";
	return f(s);        // checker accepted; clang rejected the emitted IR:
	                    // '%.t3' defined with type '%DriftString' but expected 'ptr'

Root cause: the "binding call" branch of `resolve_call_expr`
(lang/driftc/checker/call_resolver.py, HCall whose callee is an HVar bound
to a local of FUNCTION type) validated ONLY arity — it never compared
argument types against the fn-type's parameters and never ran
parameter-directed auto-borrow, so a bare `T` argument at a `&T` slot was
recorded as resolved and lowered by value into an indirect call expecting
`ptr`.  The later shallow validator (`check_call_signature`) only catches
args it can infer (literals), so local-variable arguments slipped through
to codegen entirely unchecked — both the missing-borrow shape (this bug)
and plain wrong-type locals.

Pinned semantics (bug fix only — NOT the redundant-borrow proposal):
  * bare `f(place)` at a `&T`/`&mut T` fn-pointer param AUTO-BORROWS,
    structurally (an HBorrow node in HIR, same as direct calls), compiles,
    and runs;
  * explicit `f(&s)` / `g(&mut x)` remain legal and behave identically;
  * generic `Fn(T)` instantiated at a reference type is unaffected (the
    fix keys on the resolved FUNCTION TypeId, no source-provenance
    assumptions);
  * genuinely mismatched arguments (wrong inner type; immutable place at
    `&mut`) are rejected by driftc with a diagnostic — never by clang.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_SHARED_PRELUDE = """\
module main;

fn read_len(arg: &String) nothrow -> Int {
	return arg.byte_length();
}
"""

# THE regression: bare place at Fn(&String).
_BARE_SHARED = _SHARED_PRELUDE + """
pub fn main() nothrow -> Int {
	val f: Fn(&String) nothrow -> Int = read_len;
	val s: String = "hello";
	if f(s) == 5 { return 0; }
	return 1;
}
"""

# Control C1: explicit borrow stays legal, same behavior.
_EXPLICIT_SHARED = _SHARED_PRELUDE + """
pub fn main() nothrow -> Int {
	val f: Fn(&String) nothrow -> Int = read_len;
	val s: String = "hello";
	if f(&s) == 5 { return 0; }
	return 1;
}
"""

_MUT_PRELUDE = """\
module main;

fn bump(p: &mut Int) nothrow -> Int {
	*p = *p + 1;
	return *p;
}
"""

# THE regression, &mut flavor: bare mutable place at Fn(&mut Int).
_BARE_MUT = _MUT_PRELUDE + """
pub fn main() nothrow -> Int {
	val g: Fn(&mut Int) nothrow -> Int = bump;
	var x: Int = 4;
	if g(x) == 5 and x == 5 { return 0; }
	return 1;
}
"""

# Control C1b: explicit &mut stays legal.
_EXPLICIT_MUT = _MUT_PRELUDE + """
pub fn main() nothrow -> Int {
	val g: Fn(&mut Int) nothrow -> Int = bump;
	var x: Int = 4;
	if g(&mut x) == 5 and x == 5 { return 0; }
	return 1;
}
"""

# Control C3: generic Fn(T) instantiated at a reference type — the fix
# must key on the resolved FUNCTION TypeId, not on how the type was
# written, so this behaves exactly like the concrete Fn(&String) case.
_GENERIC_REF_INST = _SHARED_PRELUDE + """
fn apply_it<T>(f: Fn(T) nothrow -> Int, v: T) nothrow -> Int {
	return f(v);
}

pub fn main() nothrow -> Int {
	val s: String = "hello";
	if apply_it<type &String>(read_len, &s) == 5 { return 0; }
	return 1;
}
"""

# Control C3b: same generic at a non-reference instantiation.
_GENERIC_VALUE_INST = """\
module main;

fn twice(x: Int) nothrow -> Int {
	return x * 2;
}

fn apply_it<T>(f: Fn(T) nothrow -> Int, v: T) nothrow -> Int {
	return f(v);
}

pub fn main() nothrow -> Int {
	if apply_it<type Int>(twice, 21) == 42 { return 0; }
	return 1;
}
"""

# Control C5a: wrong inner type from a LOCAL must be a driftc diagnostic,
# never a clang failure.  (Literals were already caught by the shallow
# validator; locals were not.)
_WRONG_INNER_LOCAL = _SHARED_PRELUDE + """
pub fn main() nothrow -> Int {
	val f: Fn(&String) nothrow -> Int = read_len;
	val n: Int = 3;
	return f(n);
}
"""

# Control C5b: immutable place at Fn(&mut Int) must be rejected by driftc.
_IMMUTABLE_AT_MUT = _MUT_PRELUDE + """
pub fn main() nothrow -> Int {
	val g: Fn(&mut Int) nothrow -> Int = bump;
	val y: Int = 4;
	return g(y);
}
"""

# C6: keyword arguments on a function value.  A zero-argument fn value
# passes the positional arity check, so an ignored keyword previously
# slipped through this branch entirely (same defect family: the branch
# validated arity only).  Must get the sibling paths' "not supported"
# diagnostic, not a silent drop.
_KWARG_ON_FN_VALUE = """\
module main;

fn zero() nothrow -> Int {
	return 7;
}

pub fn main() nothrow -> Int {
	val f: Fn() nothrow -> Int = zero;
	return f(ignored = 1);
}
"""

# C7: arity mismatch pins the repaired error path — this branch's arity
# diagnostic read a never-assigned `arg_types` (UnboundLocalError: the
# compiler died with a Python traceback instead of a diagnostic).
_ARITY_MISMATCH = _SHARED_PRELUDE + """
pub fn main() nothrow -> Int {
	val f: Fn(&String) nothrow -> Int = read_len;
	return f();
}
"""


def _compile(tmp_path: Path, source: str, *extra: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


def _run_ok(tmp_path: Path, source: str, *extra: str) -> None:
	res = _compile(tmp_path, source, *extra)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"


def _rejected_by_driftc(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	res = _compile(tmp_path, source)
	assert res.returncode != 0, "expected a compile error, got success"
	assert "clang failed" not in res.stderr, (
		f"mismatch reached codegen instead of the checker:\n{res.stderr[-1500:]}"
	)
	return res


def test_bare_shared_ref_arg_compiles_and_runs(tmp_path: Path) -> None:
	"""THE regression: bare `f(s)` at Fn(&String) auto-borrows and runs."""
	_run_ok(tmp_path, _BARE_SHARED)


def test_bare_mut_ref_arg_compiles_and_runs(tmp_path: Path) -> None:
	"""&mut flavor: bare `g(x)` at Fn(&mut Int) auto-borrows mutably; the
	write through the parameter is visible in the caller's `x`."""
	_run_ok(tmp_path, _BARE_MUT)


def test_explicit_shared_ref_arg_still_legal(tmp_path: Path) -> None:
	"""C1: explicit `f(&s)` stays legal (redundancy policy is out of scope
	for this bug slice)."""
	_run_ok(tmp_path, _EXPLICIT_SHARED)


def test_explicit_mut_ref_arg_still_legal(tmp_path: Path) -> None:
	"""C1b: explicit `g(&mut x)` stays legal."""
	_run_ok(tmp_path, _EXPLICIT_MUT)


def test_generic_fn_type_ref_instantiation_unaffected(tmp_path: Path) -> None:
	"""C3: `Fn(T)` instantiated at `&String`, called with an existing
	`&String` — no provenance assumptions in the fix."""
	_run_ok(tmp_path, _GENERIC_REF_INST)


def test_generic_fn_type_value_instantiation_unaffected(tmp_path: Path) -> None:
	"""C3b: `Fn(T)` at a non-reference instantiation keeps working."""
	_run_ok(tmp_path, _GENERIC_VALUE_INST)


def test_wrong_inner_type_local_rejected_by_checker(tmp_path: Path) -> None:
	"""C5a: `f(n)` with `n: Int` at Fn(&String) is a driftc diagnostic,
	not a clang failure."""
	res = _rejected_by_driftc(tmp_path, _WRONG_INNER_LOCAL)
	assert "type mismatch" in res.stderr or "expected" in res.stderr, res.stderr[-800:]


def test_immutable_place_at_mut_param_rejected(tmp_path: Path) -> None:
	"""C5b: `g(y)` with immutable `y` at Fn(&mut Int) is rejected by
	driftc (cannot auto-borrow as &mut)."""
	_rejected_by_driftc(tmp_path, _IMMUTABLE_AT_MUT)


def test_kwarg_on_fn_value_rejected(tmp_path: Path) -> None:
	"""C6: `f(ignored = 1)` on a zero-arg fn value is rejected with the
	function-value keyword diagnostic — not silently dropped."""
	res = _rejected_by_driftc(tmp_path, _KWARG_ON_FN_VALUE)
	assert "keyword arguments are not supported on function values" in res.stderr, res.stderr[-800:]


def test_arity_mismatch_clean_diagnostic(tmp_path: Path) -> None:
	"""C7: `f()` at Fn(&String) yields a clean overload diagnostic, no
	Python traceback (the branch's arity path previously raised
	UnboundLocalError)."""
	res = _rejected_by_driftc(tmp_path, _ARITY_MISMATCH)
	assert "Traceback" not in res.stderr, res.stderr[-1500:]
	assert "no matching overload" in res.stderr, res.stderr[-800:]


def test_bare_shared_ref_arg_asan(tmp_path: Path) -> None:
	"""ASAN row: the auto-borrow lends `s` — no transfer, no double
	release of the String on either side of the indirect call."""
	res = _compile(tmp_path, _BARE_SHARED, "--sanitize=address,undefined")
	assert res.returncode == 0, res.stderr[-1500:]
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, run.stderr[-800:]
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-800:]
