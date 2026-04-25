# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Site 3 strings/arrays return-source — regression carrier for the
alias-walk skip in `string_arc.py::Return-terminator branch`.

**The shape under test.**  When a function returns a value derived
from a heap-`String` (or `Array<…>`) local, site 3's alias-walk
recognises the LoadLocal that feeds the Return and adds the source
local to `skip_cleanup_locals`.  Without this skip, the function's
exit cleanup would release the local's stake while the caller is
holding the same bytes — refcount → 0, buffer freed, caller does
UAF or double-free on subsequent use / drop.

Lives at `lang/driftc/stage2/string_arc.py:1466-1491` (the inline
`for prev in reversed(new_instrs)` loop that walks `Return.value`
back through `AssignSSA` chains to a single `LoadLocal` and adds
`prev.local` to `skip_cleanup_locals` if it's in `string_locals`
or `array_locals`).

**Why this carrier exists.**  The Phase 4 sub-step 1 ledger
consultation for return-source suppression was intentionally
narrowed to `destructible_locals`; the strings/arrays alias-walk
remains as a named residual (see `feature/site3-strings-arrays-tier1`
kickoff).  Broader consultation (folding strings/arrays into the
ledger consultation loop) previously broke
`test_pkg_map_literal_string_leak` and `test_scope_drop_conditional_move`
under memcheck.

This carrier pins the alias-walk's load-bearing role so any future
attempt to remove or replace it has a focused, valgrind-level
gate:

  - PASS today (alias-walk active).
  - FAIL if the alias-walk is removed without a ledger-equivalent
    that transitions String / Array return-source locals to
    MOVED_OUT at the LoadLocal index — symptom is double-release
    UAF caught by valgrind ("Invalid read" / "definitely lost"
    / aborted exit code).

The carrier exercises two String shapes:

  1. **Direct String return** — `return s;` where `s: String`.
  2. **Aliased String return** — `val r = s; return r;` (forces
     the alias-walk to traverse an `AssignSSA` chain).

Each is exercised across multiple invocations so any per-call leak
shows up as a multi-block valgrind report.

**Note on Array<…> return-source.**  Arrays are non-Copy: returning
one requires explicit `return move arr;`, which lowers to
`MoveOut(t, arr) + Return(t)`.  The lattice's Phase 4 Return-as-move
recognises this shape and transitions `arr` to MOVED_OUT at the
MoveOut index — without consulting the alias-walk.  The
`array_locals` branch in the alias-walk
(`if prev.local in array_locals: skip_cleanup_locals.add(...)`) is
therefore not exercised by the natural array-return pattern;
constructing a shape that demonstrably depends on it requires a
non-trivial generic / borrow path that the next site-3 patch can
add as a focused pin if/when the migration touches the
`array_locals` branch.  For Patch 1 we pin the load-bearing
String shapes only.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


# Direct String return: function allocates a heap String via
# `format_int`, returns it.  Caller drops it.  Refcount accounting:
#   alloc → +1; return transfers; caller drop → -1.  Balanced.
# Without the alias-walk:
#   alloc → +1; function-exit cleanup → -1 (refcount=0, freed);
#   caller drop → -1 on freed memory → UAF.
DIRECT_STRING_SOURCE = """\
module main;

import std.format as fmt;

fn produce(n: Int) nothrow -> String {
\tval s: String = fmt.format_int(n);
\treturn s;
}

pub fn main() nothrow -> Int {
\tval a: String = produce(1);
\tval b: String = produce(22);
\tval c: String = produce(333);
\treturn a.byte_length() + b.byte_length() + c.byte_length();
}
"""


# Aliased String return: same as above but the return value is an
# alias of the local (forces the alias-walk through the AssignSSA
# chain to land on the LoadLocal).
ALIASED_STRING_SOURCE = """\
module main;

import std.format as fmt;

fn produce_aliased(n: Int) nothrow -> String {
\tval s: String = fmt.format_int(n);
\tval r: String = s;
\treturn r;
}

pub fn main() nothrow -> Int {
\tval a: String = produce_aliased(1);
\tval b: String = produce_aliased(22);
\tval c: String = produce_aliased(333);
\treturn a.byte_length() + b.byte_length() + c.byte_length();
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text)."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1000]}"
	assert out_bin.exists(), f"[{label}] binary not produced"

	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
		["valgrind", "--tool=memcheck", "--leak-check=full",
		 "--show-leak-kinds=definite,indirect",
		 "--errors-for-leak-kinds=definite,indirect",
		 "--error-exitcode=97",
		 f"--log-file={vg_log}",
		 str(out_bin)],
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	return definitely_lost, vg_output


def _assert_valgrind_clean(lost: int, vg_log: str, *, label: str, broken_state_hint: str) -> None:
	"""Assert zero definitely-lost bytes AND no valgrind errors.

	`broken_state_hint` describes the symptom this carrier should
	produce if the alias-walk is removed without ledger replacement
	— surfaced in the failure message so the next site-3 patch can
	read it directly from CI output.
	"""
	assert lost == 0, (
		f"[{label}] LANGUAGE_BUG: site-3 alias-walk regression — "
		f"{lost} bytes definitely lost.\n"
		f"Expected symptom if the alias-walk in `string_arc.py:1466-1491` "
		f"was removed/weakened without a ledger-equivalent: {broken_state_hint}\n"
		f"Touch points: `_collect_return_source_locals`, "
		f"`skip_cleanup_locals`, and the inline LoadLocal walk in the "
		f"Return-terminator branch.\n\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	# Also assert no UAF / Invalid-read errors (the more likely
	# symptom for double-release).
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access — "
			f"likely double-release from the function-exit cleanup "
			f"firing on a return-source local.\n\n{vg_log[-1500:]}"
		)


def test_site3_direct_string_return_source_no_leak(tmp_path: Path) -> None:
	"""Direct String return: `return s;` where `s: String` is a
	heap-allocated local (`fmt.format_int(n)`).  Site 3's alias-walk
	must add `s` to `skip_cleanup_locals` so the function-exit
	`_release_all_locals` does NOT release `s` — the caller now
	owns the +1.

	Carrier shape mirrors the canonical "factory function returns
	a String" pattern that pervades stdlib (`format_int`,
	`String::from`, etc.).
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, DIRECT_STRING_SOURCE, label="direct_string"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="direct_string",
		broken_state_hint=(
			"function-exit `_release_all_locals` releases `s` while "
			"caller holds the returned String → refcount underflow → "
			"caller's release fires on freed buffer → Invalid read / "
			"definitely lost (~24 bytes per call)."
		),
	)


def test_site3_aliased_string_return_source_no_leak(tmp_path: Path) -> None:
	"""Aliased String return: `val r = s; return r;` — forces the
	alias-walk to traverse an `AssignSSA` chain (`r` is `s` aliased
	at MIR level) before landing on the `LoadLocal(_, s)` that
	feeds the Return.

	If the alias-walk's chain traversal regresses to a single-step
	check, `r` would be the apparent return-source and `s` would
	NOT be added to `skip_cleanup_locals` — function-exit cleanup
	would release `s`'s underlying buffer.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, ALIASED_STRING_SOURCE, label="aliased_string"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="aliased_string",
		broken_state_hint=(
			"alias-walk's AssignSSA chain traversal stopped recognising "
			"`r` as an alias of `s`; `s` left out of "
			"`skip_cleanup_locals` and double-released at function exit."
		),
	)


