# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1 ConstShare structural synthesis — memcheck carriers.

Synthesized `const_share` method bodies must produce balanced
retain/release for every field path:
  - ConstShare-path fields (`core.ConstArc<U>`): per-call
    `Arc::clone` retain on the inner allocation;
  - Copy+Frozen-path fields: existing borrowed-Copy auto-copy
    machinery handles the deref + duplicate (refcount-bump for
    `String`, bitwise for primitives).

Carriers exercise:
  1. Single-field `ConstArc<String>` struct lifecycle.
  2. Mixed `ConstArc<String>` + `String` + `Int` struct lifecycle.
  3. Nested same-module struct (`Outer { inner: Inner }`).

If any test fails, the regression is in:
  - the synthesized HIR body (`_build_const_share_hir` in
    `lang/driftc/const_share_synth.py`);
  - the synthesized FnSignature shape (param/return type
    mismatch);
  - registration of synthesized impls into linked_world /
    callable_registry / module_exports;
  - HIR→MIR lowering of the synthesized body.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


SYNTH_CONST_ARC_STRING_LIFECYCLE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub struct Holder {
\tpub handle: core.ConstArc<String>
}

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval h = Holder(handle = core.const_arc<type String>(move s));
\tval h2 = h.const_share();
\tval r1: &String = h.handle.get();
\tval r2: &String = h2.handle.get();
\treturn r1.byte_length() + r2.byte_length();
\t// h2 drops first -> ConstArc release -> count 2 -> 1.
\t// h drops next   -> count 1 -> 0 -> free String + ArcBox.
}
"""


SYNTH_MIXED_LIFECYCLE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub struct Mixed {
\tpub handle: core.ConstArc<String>,
\tpub tag: Int,
\tpub label: String
}

pub fn main() nothrow -> Int {
\tval s = fmt.format_int(700);
\tval lab = fmt.format_int(800);
\tval m = Mixed(
\t\thandle = core.const_arc<type String>(move s),
\t\ttag = 7,
\t\tlabel = move lab
\t);
\tval m2 = m.const_share();
\tval total = *(&m.tag) + *(&m2.tag);
\treturn total;
\t// Both Mixed structs drop at scope exit.  String fields
\t// (handle's inner String, label) refcount via string_arc.
\t// ConstArc handles refcount independently.
}
"""


SYNTH_NESTED_LIFECYCLE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub struct Inner {
\tpub a: core.ConstArc<String>
}

pub struct Outer {
\tpub inner: Inner,
\tpub tag: Int
}

pub fn main() nothrow -> Int {
\tval s = fmt.format_int(700);
\tval i = Inner(a = core.const_arc<type String>(move s));
\tval o = Outer(inner = i, tag = 1);
\tval o2 = o.const_share();
\tval r1: &String = o.inner.a.get();
\tval r2: &String = o2.inner.a.get();
\treturn r1.byte_length() + r2.byte_length();
\t// Outer.const_share dispatches to synthesized Outer body,
\t// which calls Inner.const_share recursively (synthesized).
\t// Inner.const_share calls ConstArc::const_share on `a`,
\t// which bumps refcount.  All releases must balance.
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


def test_synth_const_arc_string_lifecycle_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, SYNTH_CONST_ARC_STRING_LIFECYCLE, label="synth_cs_string"
	)
	_assert_clean(
		lost, vg, errors,
		label="synth_cs_string",
		hint="synthesized const_share on Holder must produce one "
		     "Arc retain + two releases; an imbalance leaks or "
		     "double-frees the ArcBox<String>.",
	)


def test_synth_mixed_lifecycle_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, SYNTH_MIXED_LIFECYCLE, label="synth_cs_mixed"
	)
	_assert_clean(
		lost, vg, errors,
		label="synth_cs_mixed",
		hint="mixed Copy+Frozen + ConstArc fields — String (Copy "
		     "via string_arc) and Int (bitwise) must duplicate "
		     "correctly while ConstArc retain/release stays balanced.",
	)


def test_synth_nested_lifecycle_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, SYNTH_NESTED_LIFECYCLE, label="synth_cs_nested"
	)
	_assert_clean(
		lost, vg, errors,
		label="synth_cs_nested",
		hint="nested Outer.const_share must call Inner.const_share "
		     "recursively (both synthesized).  Inner's ConstArc<String> "
		     "must retain/release in lockstep with the structural drop.",
	)
