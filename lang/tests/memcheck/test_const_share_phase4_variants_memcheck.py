# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 4 ConstShare structural synthesis — variant memcheck carriers.

Synthesized variant `const_share` body is a real HIR `match self`
that reconstructs the same case with per-payload-field
transformation (`.const_share()` for ConstShare-path fields,
borrowed-Copy auto-copy for Copy+Frozen-path fields).  Carriers
exercise:

  1. `Wrap(handle: ConstArc<String>)` payload — borrowed match
     binder calls `ConstArc::const_share` on the inner Arc;
     refcount on the ArcBox<String> must be balanced across the
     extra owner's full lifetime.

  2. Mixed-arms variant carrying both an `Int` arm and a
     `ConstArc<String>` arm — the matched arm dispatches the
     correct field-path lowering (Copy+Frozen vs ConstShare).

  3. Nested variant containing a struct that itself contains a
     `ConstArc<String>` — synthesized variant body's payload
     transformation calls the synthesized struct `const_share`
     recursively.

If any test fails, the regression is in:
  - `_build_const_share_hir_variant` arm reconstruction
    (`lang/driftc/const_share_synth.py`);
  - HIR→MIR lowering of value-form match arms with payload
    binders (binder borrow shape, ctor reconstruction);
  - per-arm field-path dispatch (ConstShare vs Copy+Frozen vs
    None-blocking).
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


SYNTH_VARIANT_CONST_ARC_LIFECYCLE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub variant Carrier {
\tEmpty,
\tWrap(handle: core.ConstArc<String>)
}

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval c = Carrier::Wrap(core.const_arc<type String>(move s));
\tval c2 = c.const_share();
\treturn 0;
\t// c and c2 each hold an independent owner of the inner
\t// ArcBox<String>; both must release on scope exit so refcount
\t// drops to 0 and the String + ArcBox are freed exactly once.
}
"""


SYNTH_VARIANT_MIXED_ARMS_LIFECYCLE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub variant Multi {
\tEmpty,
\tNumber(n: Int),
\tText(handle: core.ConstArc<String>)
}

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval m = Multi::Text(core.const_arc<type String>(move s));
\tval m2 = m.const_share();
\tval n = Multi::Number(42);
\tval n2 = n.const_share();
\treturn 0;
\t// Text arm exercises the ConstShare path; Number arm exercises
\t// the Copy+Frozen path on Int.  Both lifecycles must balance.
}
"""


SYNTH_VARIANT_NESTED_STRUCT_LIFECYCLE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub struct Inner {
\tpub a: core.ConstArc<String>
}

pub variant Outer {
\tEmpty,
\tHas(inner: Inner)
}

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval inner = Inner(a = core.const_arc<type String>(move s));
\tval o = Outer::Has(inner);
\tval o2 = o.const_share();
\treturn 0;
\t// Outer's synthesized body calls Inner.const_share on the
\t// matched binder (Inner is itself synthesized — Phase 1).
\t// Two levels of synthesis must compose without leaking.
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
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
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


def test_synth_variant_const_arc_lifecycle_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, SYNTH_VARIANT_CONST_ARC_LIFECYCLE,
		label="synth_var_cs_string",
	)
	_assert_clean(
		lost, vg, errors,
		label="synth_var_cs_string",
		hint="synthesized variant `const_share` over Wrap arm must "
		     "call ConstArc::const_share on the borrowed payload "
		     "binder; refcount imbalance leaks or double-frees the "
		     "ArcBox<String>.",
	)


def test_synth_variant_mixed_arms_lifecycle_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, SYNTH_VARIANT_MIXED_ARMS_LIFECYCLE,
		label="synth_var_mixed",
	)
	_assert_clean(
		lost, vg, errors,
		label="synth_var_mixed",
		hint="mixed-arms variant: each arm dispatches its own "
		     "field-path lowering.  Number arm Copy+Frozen on Int "
		     "must not leak; Text arm ConstShare on ConstArc must "
		     "retain/release in lockstep.",
	)


def test_synth_variant_nested_struct_lifecycle_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, SYNTH_VARIANT_NESTED_STRUCT_LIFECYCLE,
		label="synth_var_nested_struct",
	)
	_assert_clean(
		lost, vg, errors,
		label="synth_var_nested_struct",
		hint="nested variant arm carries a struct (Inner) that is "
		     "itself synthesized.  Outer's variant `const_share` "
		     "must dispatch to Inner's struct `const_share`; both "
		     "synthesis layers must compose retain/release "
		     "correctly.",
	)
