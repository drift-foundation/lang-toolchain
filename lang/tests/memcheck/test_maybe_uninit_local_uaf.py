# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Memcheck carriers for the standalone-local `MaybeUninit<T>`
pattern landed at 0.31.16.

The pattern under test is

\tunsafe {
\t\tvar slot = mem.maybe_uninit<type T>();
\t\tmem.maybe_write<type T>(&mut slot, move v);
\t\tval out = mem.maybe_assume_init_read<type T>(&mut slot);
\t\t…use out…
\t}

Pre-0.31.16, `mem.maybe_uninit` lowering was a hard
`NotImplementedError`, so the standalone-local form could not be
exercised end-to-end against valgrind.  These carriers pin the
heap shape for the two payload classes that exercise the
non-trivial-Drop axes:

* **String** — refcount-bearing built-in type with `string_arc`
  late-rewrite (drop-before-overwrite on `StoreRef`).  The
  carrier writes a heap-allocated String into the slot, reads it
  back out, and consumes it normally.  Any leak / UAF here
  surfaces as `definitely lost` bytes or `Invalid read/write` in
  the valgrind log.
* **Arc<T>** — refcount-bearing user-level type with structural
  drop and runtime helpers (`drift_arc_release`).  The carrier
  uses an inner `Payload { s: String, tag: Int }` so any leaked
  Arc shows up as a definitely-lost String allocation (the Arc
  inline storage alone might be reused by the allocator and
  evade the leak summary).

Both carriers run under standard memcheck flags
(`--leak-check=full --show-leak-kinds=definite,indirect`) and
require `definitely lost == 0` AND no `Invalid …` reports.

Why these carriers matter for this patch
----------------------------------------
The lowering itself is one MIR op (`ZeroValue(MaybeUninit<T>)`),
but it composes with three other intrinsics (`maybe_write`,
`maybe_assume_init_read`) that each do their own pointer-write /
zero-fill / move-out steps.  The String case exercises
`overwrite_cleanup`'s StoreRef rewrite against the slot tombstone;
the Arc case exercises the Arc destructor against the +1 stake
transfer through the slot.  Both are direct heap-touching paths
through the ledger and codegen layers.

Ownership / refcount changes require memcheck coverage.  Even
though this patch is not formally a site-3 /
`skip_cleanup_locals` change, it introduces a new local-storage
pattern that interacts with the cleanup authoring path, so the
String + Arc carriers ride in the standard verification gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

import pytest


_ROOT = Path(__file__).resolve().parents[3]


_STRING_SOURCE = """\
module main;

import std.mem as mem;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar s: String = fmt.format_int(700);
\tunsafe {
\t\tvar slot = mem.maybe_uninit<type String>();
\t\tmem.maybe_write<type String>(&mut slot, move s);
\t\tval out: String = mem.maybe_assume_init_read<type String>(&mut slot);
\t\treturn out.byte_length();
\t\t// `out` drops at scope exit, releasing the String allocation.
\t\t// `slot` is `MaybeUninit<String>` (no-drop type) — no scope
\t\t// destructor; `mem.maybe_assume_init_read` already zeroed it.
\t}
}
"""


_ARC_SOURCE = """\
module main;

import std.mem as mem;
import std.concurrent as conc;
import std.format as fmt;

struct Payload {
\ts: String,
\ttag: Int
}

pub fn main() nothrow -> Int {
\tval p = conc.arc(Payload(s = fmt.format_int(700), tag = 7));
\tunsafe {
\t\tvar slot = mem.maybe_uninit<type conc.Arc<Payload>>();
\t\tmem.maybe_write<type conc.Arc<Payload>>(&mut slot, move p);
\t\tval out = mem.maybe_assume_init_read<type conc.Arc<Payload>>(&mut slot);
\t\tval ref_payload = out.get();
\t\treturn ref_payload.tag;
\t\t// `out` drops at scope exit, decrementing the Arc strong
\t\t// refcount to 0 and freeing Payload (and its String).
\t}
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str, int]:
	"""Compile under raw stdlib with --allow-unsafe, run under
	valgrind.  Returns (definitely_lost_bytes, valgrind_log_text,
	error_count)."""
	if shutil.which("valgrind") is None:
		pytest.skip("valgrind not available")

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin),
		 "--allow-unsafe"],
		cwd=_ROOT, capture_output=True, text=True, timeout=120,
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


def _assert_clean(lost: int, vg_log: str, errors: int, *, label: str, broken_state_hint: str) -> None:
	assert lost == 0, (
		f"[{label}] {lost} bytes definitely lost.\n"
		f"Expected symptom: {broken_state_hint}\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access — "
			f"{broken_state_hint}\n"
			f"Touch points:\n"
			f"  - `lang/driftc/stage2/hir_to_mir.py::IntrinsicKind.MAYBE_UNINIT`\n"
			f"  - `lang/driftc/stage2/hir_to_mir.py::IntrinsicKind.MAYBE_WRITE`\n"
			f"  - `lang/driftc/stage2/hir_to_mir.py::IntrinsicKind.MAYBE_ASSUME_INIT_READ`\n"
			f"  - `lang/driftc/stage2/overwrite_cleanup.py` (StoreRef overwrite release)\n"
			f"Valgrind error count: {errors}\n\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)


def test_maybe_uninit_local_string_round_trip_no_uaf(tmp_path: Path) -> None:
	"""String payload — refcount-bearing built-in type with the
	`overwrite_cleanup` StoreRef overwrite-release path.

	The carrier writes a heap-allocated String into a standalone
	`MaybeUninit<String>` slot via `maybe_write` (which lowers to
	`PtrFromRef + PtrWrite`), reads it back out via
	`maybe_assume_init_read` (which lowers to `PtrFromRef + PtrRead
	+ ZeroValue + PtrWrite`), and lets `out` drop normally at
	scope exit.

	Failure modes the carrier guards against:

	* **definitely lost**: the constructor's `ZeroValue(slot)` did
	  not actually zero the slot bytes, leaving stale bytes that
	  the read-side decoded as a refcount that never reached 0;
	  or the read's tombstone failed and the scope-exit path
	  (which doesn't run for the slot since it's no-drop) also
	  did not release.
	* **Invalid read/write**: the slot's tombstone bytes leaked
	  through into the consumer (`out`), or the `string_arc`
	  StoreRef rewrite double-released against the freshly written
	  bytes.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, _STRING_SOURCE, label="maybe_uninit_local_string"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="maybe_uninit_local_string",
		broken_state_hint=(
			"either `mem.maybe_uninit` no longer zero-initializes "
			"the slot, `mem.maybe_write` no longer transfers "
			"ownership of the String value into the slot, or "
			"`mem.maybe_assume_init_read` no longer moves the "
			"value out and leaves the slot tombstoned to zero "
			"bytes (so `string_arc`'s implicit drop-before-"
			"overwrite would touch live bytes)."
		),
	)


def test_maybe_uninit_local_arc_round_trip_no_uaf(tmp_path: Path) -> None:
	"""`Arc<T>` payload — refcount-bearing user-level type with
	structural drop and runtime helpers
	(`drift_arc_retain`/`drift_arc_release`).  The +1 strong
	stake must transfer through the slot exactly once: from the
	original local into the slot, then out into `out`.

	Failure modes:

	* **definitely lost ≈ sizeof(Arc inline) + sizeof(Payload) +
	  sizeof(String allocation)**: the +1 stake went into the
	  slot and never came out, OR `out` failed to take ownership
	  on read.
	* **Invalid free**: the +1 stake was double-released, e.g.
	  if the read-side tombstone fired through the Arc destructor
	  protocol against zero bytes.

	The Payload carries a String to surface any leaked allocation
	even if Arc inline storage is allocator-reused.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, _ARC_SOURCE, label="maybe_uninit_local_arc"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="maybe_uninit_local_arc",
		broken_state_hint=(
			"Arc strong-refcount stake leaked or double-released "
			"through the standalone-local MaybeUninit pattern.  "
			"Inspect whether the `MAYBE_WRITE` lowering still "
			"routes the value arg through `_lower_owning_consume` "
			"(emitting `MoveOut` for non-Copy locals), and whether "
			"the `MAYBE_ASSUME_INIT_READ` chain "
			"`PtrRead + ZeroValue + PtrWrite` still fires in that "
			"order against the slot's bytes."
		),
	)
