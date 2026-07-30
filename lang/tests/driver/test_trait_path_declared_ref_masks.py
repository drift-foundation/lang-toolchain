# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""W0 declaration-origin classification on the TRAIT call paths
(reject-redundant-call-borrows, review round 2 finding 1).

The require-bound dispatch and trait-qualified static paths must use the
same declaration-origin rule as every other family — via the shared
`declared_ref_formal` classifier — not a literal `&`-token check:

- an ALIAS-declared reference formal (`type Handle = &String`) counts as
  declared (D6 transparency): bare auto-borrows, explicit rejects;
- a bare generic trait parameter (`v: V`) stays generic-by-value even
  when instantiated at a reference type: the explicit borrow is the
  argument value, exempt.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_ALIAS_TRAIT_COMMON = """\
module main;

type Handle = &String;

trait Measurer {
	fn measure(self: &Self, h: Handle) nothrow -> Int;
}

struct Ruler { tag: Int }

implement Measurer for Ruler {
	fn measure(self: &Ruler, h: Handle) nothrow -> Int {
		return h.byte_length();
	}
}

"""

# (a) alias formal through REQUIRE-BOUND dispatch, in the supported
# idiom: the trait-qualified call inside a require-bounded generic fn
# (stdlib pattern: `cmp.Comparable::cmp(&self.arr[i], self.arr[j])`).
_ALIAS_REQUIRE_BARE = _ALIAS_TRAIT_COMMON + """
fn drive<M>(m: M) nothrow -> Int require M is Measurer {
	val s: String = "hello";
	return Measurer::measure(&m, s);
}

pub fn main() nothrow -> Int {
	val r = Ruler(tag = 1);
	if drive(move r) == 5 { return 0; }
	return 1;
}
"""

_ALIAS_REQUIRE_EXPLICIT = _ALIAS_REQUIRE_BARE.replace("Measurer::measure(&m, s)", "Measurer::measure(&m, &s)")

# (b) alias formal through a GENERIC TRAIT-QUALIFIED call.
_ALIAS_TQ_BARE = _ALIAS_TRAIT_COMMON + """
pub fn main() nothrow -> Int {
	val r = Ruler(tag = 1);
	val s: String = "hello";
	if Measurer::measure(r, s) == 5 { return 0; }
	return 1;
}
"""

_ALIAS_TQ_EXPLICIT = _ALIAS_TQ_BARE.replace("Measurer::measure(r, s)", "Measurer::measure(r, &s)")

# (c) generic-by-value formal instantiated at a reference type, through
# REQUIRE-BOUND dispatch (the stdlib ffi pattern): `Fn1<&String, Int>`
# declares `call(self: &Self, a: A)` — `A` is a bare interface type
# parameter, so the explicit borrow IS the argument value: exempt,
# stays legal.  (A user-defined parameterized trait exercised through
# the qualified form hits a pre-existing impl-lookup gap —
# `no implementation ... on receiver Ref<T>` — unrelated to this rule.)
_GENERIC_BYVALUE_REF_INST = """\
module main;

import std.core as core;

fn read_len(arg: &String) nothrow -> Int {
	return arg.byte_length();
}

fn drive<F>(body: F) nothrow -> Int require F is core.Fn1<&String, Int> {
	val s: String = "hello";
	return body.call(&s);
}

pub fn main() nothrow -> Int {
	if drive(read_len) == 5 { return 0; }
	return 1;
}
"""


def _compile(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


def _run_ok(tmp_path: Path, source: str) -> None:
	res = _compile(tmp_path, source)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	run = subprocess.run([str(tmp_path / "test_bin")], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"exit {run.returncode}"


def test_alias_formal_require_bound_bare_autoborrows(tmp_path: Path) -> None:
	_run_ok(tmp_path, _ALIAS_REQUIRE_BARE)


def test_alias_formal_require_bound_explicit_rejected(tmp_path: Path) -> None:
	res = _compile(tmp_path, _ALIAS_REQUIRE_EXPLICIT)
	assert res.returncode != 0
	assert "E_REDUNDANT_ARG_BORROW" in (res.stderr + res.stdout), (res.stderr + res.stdout)[-900:]


def test_alias_formal_trait_qualified_bare_autoborrows(tmp_path: Path) -> None:
	_run_ok(tmp_path, _ALIAS_TQ_BARE)


def test_alias_formal_trait_qualified_explicit_rejected(tmp_path: Path) -> None:
	res = _compile(tmp_path, _ALIAS_TQ_EXPLICIT)
	assert res.returncode != 0
	assert "E_REDUNDANT_ARG_BORROW" in (res.stderr + res.stdout), (res.stderr + res.stdout)[-900:]


def test_generic_byvalue_trait_param_ref_instantiation_exempt(tmp_path: Path) -> None:
	"""`v: V` instantiated at `&String`: the borrow is the value — legal."""
	_run_ok(tmp_path, _GENERIC_BYVALUE_REF_INST)


# (d) the ORIGINAL user-defined parameterized-trait shape for (c) — restored
# now that the underlying normalize_type_key LANGUAGE_BUG is fixed (see
# LANGUAGE_BUGS-found-during-implementation.md §3): a generic trait formal
# `v: V` instantiated at `&String` stays exempt through the qualified call.
_GENERIC_USER_TRAIT_REF_INST = """\
module main;

trait Taker<V> {
	fn take(self: &Self, v: V) nothrow -> Int;
}

struct Sink { tag: Int }

implement Taker<&String> for Sink {
	fn take(self: &Sink, v: &String) nothrow -> Int {
		return v.byte_length();
	}
}

pub fn main() nothrow -> Int {
	val k = Sink(tag = 1);
	val s: String = "hello";
	if Taker<&String>::take(k, &s) == 5 { return 0; }
	return 1;
}
"""


def test_generic_user_trait_param_ref_instantiation_exempt(tmp_path: Path) -> None:
	_run_ok(tmp_path, _GENERIC_USER_TRAIT_REF_INST)
