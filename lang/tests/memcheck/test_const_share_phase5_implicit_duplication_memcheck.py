# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 5 implicit ConstShare duplication — memcheck carriers.

Each carrier exercises a different value-flow site that should
auto-share without explicit `.const_share()`:

  1. Let-binding duplication of `ConstArc<String>` — the
     simplest case (HLet.value).

  2. Owned-arg passing of a Phase 1 synthesized ConstShare
     struct through a function call (HCall.args[i]).

  3. Owned-return of a Phase 4 synthesized ConstShare variant
     (HReturn.value).

If any test fails, the regression is in:
  - the post-typecheck implicit-share walker
    (`type_checker.py` `_walk_implicit_cs`);
  - call-resolution registration of the synthesized
    `HMethodCall(receiver=..., method_name="const_share")` —
    HIR→MIR needs the wrap fully resolved with call_info /
    callsite_id;
  - the underlying synthesized `const_share` body's refcount
    discipline (Phase 1 struct or Phase 4 variant).

Each carrier balances its retain/release:
  - the implicit `const_share()` adds one refcount bump;
  - the duplicated owner is dropped at scope end (the original
    binding ALSO drops at scope end — total of 2 drops match
    the original allocation + 1 share = refcount goes 1→2→1→0).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


CARRIER_LET_BINDING_DUP = """\
module main;

import std.core as core;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval a = core.const_arc<type String>(move s);
\tval b = a;
\tval r1: &String = a.get();
\tval r2: &String = b.get();
\treturn r1.byte_length() + r2.byte_length();
\t// implicit `b = a.const_share()` adds 1 retain.
\t// At scope end: drop b → count 2→1; drop a → count 1→0
\t// (free String + ArcBox).
}
"""


CARRIER_OWNED_ARG_DUP = """\
module main;

import std.core as core;
import std.format as fmt;

pub struct Holder {
\tpub handle: core.ConstArc<String>
}

pub fn take(h: Holder) nothrow -> Int {
\tval r: &String = h.handle.get();
\treturn r.byte_length();
}

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval a = Holder(handle = core.const_arc<type String>(move s));
\tval n1 = take(a);
\tval n2 = take(a);
\treturn n1 + n2;
\t// Each `take(a)` consumes a fresh share via implicit
\t// const_share — synth Holder.const_share bumps inner ConstArc
\t// (and the inner ArcBox<String>) refcount.  3 owners total
\t// (a, plus take's two h params) all drop their share so the
\t// inner allocation goes 1→2→3→ ... →0 cleanly.
}
"""


CARRIER_OWNED_RETURN_DUP = """\
module main;

import std.core as core;
import std.format as fmt;

pub variant Multi {
\tEmpty,
\tWrap(handle: core.ConstArc<String>)
}

pub fn dup(m: Multi) nothrow -> Multi {
\treturn m;
}

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval a = Multi::Wrap(core.const_arc<type String>(move s));
\tval b = dup(a);
\treturn 0;
\t// `dup(a)` consumes a fresh share of `a` (implicit
\t// const_share at the call boundary).  Inside dup, `return m`
\t// is a fresh share of m's payload (implicit const_share at
\t// HReturn.value).  Caller's `b` holds the shared owner.
\t// All 3 owners drop, refcount returns to 0.
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str, int]:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), f"[{label}] binary not produced"
	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
		["valgrind", "--tool=memcheck", "--leak-check=full",
		 "--show-leak-kinds=definite,indirect",
		 "--errors-for-leak-kinds=definite,indirect",
		 "--error-exitcode=97",
		 f"--log-file={vg_log}",
		 str(out_bin)],
		capture_output=True, text=True, timeout=180,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	err_match = re.search(r"ERROR SUMMARY: (\d+) errors", vg_output)
	error_count = int(err_match.group(1)) if err_match else 0
	return definitely_lost, vg_output, error_count


def _assert_clean(lost: int, vg_log: str, errors: int, *, label: str, hint: str) -> None:
	assert lost == 0, (
		f"[{label}] {lost} bytes definitely lost. {hint}\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access. {hint}\n"
			f"Valgrind error count: {errors}\n\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)


def test_phase5_let_binding_dup_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CARRIER_LET_BINDING_DUP, label="phase5_let_dup",
	)
	_assert_clean(
		lost, vg, errors,
		label="phase5_let_dup",
		hint="implicit `val b = a.const_share()` for ConstArc<String> "
		     "must produce one extra retain + two releases on scope "
		     "exit; an imbalance leaks or double-frees the ArcBox.",
	)


def test_phase5_owned_arg_dup_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CARRIER_OWNED_ARG_DUP, label="phase5_arg_dup",
	)
	_assert_clean(
		lost, vg, errors,
		label="phase5_arg_dup",
		hint="`take(a)` × 2 implicitly shares synthesized struct "
		     "Holder; each call consumes a fresh share.  Synth "
		     "Holder.const_share dispatches to ConstArc.const_share "
		     "for the handle field — refcount bump + drop must "
		     "balance for each call.",
	)


def test_phase5_owned_return_dup_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CARRIER_OWNED_RETURN_DUP, label="phase5_ret_dup",
	)
	_assert_clean(
		lost, vg, errors,
		label="phase5_ret_dup",
		hint="implicit duplication at HReturn.value for a Phase 4 "
		     "synthesized variant: `return m` shares the matched "
		     "arm's payload via const_share, caller's `b` owns the "
		     "fresh share, all owners drop cleanly.",
	)
