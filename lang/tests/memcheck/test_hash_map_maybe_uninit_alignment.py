# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Memcheck pins for the `RawBuffer<MaybeUninit<T>>` container path
through `std.containers.HashMapCore`.

The MaybeUninit-as-first-class-local landing at 0.31.16 added
standalone-local memcheck carriers for `String` / `Arc<T>`
payloads (`test_maybe_uninit_local_uaf.py`).  The container case
(buffer-internal `MaybeUninit<T>` slots) had end-to-end coverage
only via the PEX e2e fixture
`lang/tests/codegen/e2e/maybe_assume_init_read_moves_out_no_leak/`,
which runs through a different harness and is not part of the
standard `lang/tests/memcheck` gate.

These pins close that gap so the local-slot and raw-buffer-slot
behaviors stay aligned in the same verification suite.
HashMapCore is the only stdlib user of `RawBuffer<MaybeUninit<T>>`
(see `stdlib/std/containers/array.drift::HashMapCore`); a leak or
UAF in either the per-slot `mem.maybe_write` /
`mem.maybe_assume_init_read` lowerings or the buffer-level
`mem.alloc_uninit` / `mem.dealloc` would surface here.

Ownership / refcount changes require memcheck coverage; a
container-level slot model is the natural symmetry point with
the standalone-local carriers.

Scope of each carrier
---------------------
* **String → String**: insert, get-back, remove, then drop on
  scope exit.  Exercises `maybe_write` (insert), `maybe_assume_init_ref`
  (get), `maybe_assume_init_read` (remove), and the
  `HashMapCore::destroy` walk over `HASH_MAP_STATE_FULL` slots.
* **Arc<Payload> → Arc<Payload>**: same cycle with Arc-wrapped
  payload that carries a String — any leaked Arc surfaces as a
  definitely-lost String allocation regardless of allocator
  reuse of inline Arc bytes.
* **Rehash stress**: insert enough entries to force at least one
  rehash (`ensure_capacity` reallocates buffers and re-inserts
  via `_insert_into_buffers`); pins that the rehash path moves
  values cleanly without leaking the source slots.
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


_HM_STRING_SOURCE = """\
module main;

import std.core as core;
import std.containers as containers;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar m = containers.hash_map<type String, String>();
\tvar i = 0;
\twhile i < 8 {
\t\tval k = "k-" + fmt.format_int(i);
\t\tval v = "v-" + fmt.format_int(i);
\t\tval prev = m.insert(move k, move v);
\t\tmatch prev {
\t\t\tOptional::Some(p) => { core.drop_value<type String>(move p); },
\t\t\tOptional::None() => {}
\t\t}
\t\ti = i + 1;
\t}
\t// Read-back via maybe_assume_init_ref through `get(&k)`.
\tval probe = "k-3";
\tvar total = 0;
\tmatch m.get(probe) {
\t\tOptional::Some(v_ref) => { total = total + v_ref.byte_length(); },
\t\tOptional::None() => {}
\t}
\t// Move-out via maybe_assume_init_read through `remove(&k)`.
\tvar j = 0;
\twhile j < 4 {
\t\tval rk = "k-" + fmt.format_int(j);
\t\tmatch m.remove(rk) {
\t\t\tOptional::Some(v) => { core.drop_value<type String>(move v); },
\t\t\tOptional::None() => {}
\t\t}
\t\tj = j + 1;
\t}
\treturn total;
\t// `m` drops at scope exit — HashMapCore::destroy walks the
\t// remaining HASH_MAP_STATE_FULL slots (k-4..k-7) and releases
\t// each String.  Empty / tombstone slots are skipped.
}
"""


_HM_ARC_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.containers as containers;
import std.format as fmt;

struct Payload {
\ts: String,
\ttag: Int
}

pub fn main() nothrow -> Int {
\tvar m = containers.hash_map<type String, conc.Arc<Payload>>();
\tvar i = 0;
\twhile i < 6 {
\t\tval k = "k-" + fmt.format_int(i);
\t\tval p = conc.arc(Payload(s = fmt.format_int(700 + i), tag = i));
\t\tval prev = m.insert(move k, move p);
\t\tmatch prev {
\t\t\tOptional::Some(old) => { core.drop_value<type conc.Arc<Payload>>(move old); },
\t\t\tOptional::None() => {}
\t\t}
\t\ti = i + 1;
\t}
\t// Take one out and use it; the +1 strong stake transfers
\t// from the slot into `taken`.
\tval kk = "k-2";
\tvar total = 0;
\tmatch m.remove(kk) {
\t\tOptional::Some(taken) => {
\t\t\ttotal = total + taken.get().tag;
\t\t\tcore.drop_value<type conc.Arc<Payload>>(move taken);
\t\t},
\t\tOptional::None() => {}
\t}
\treturn total;
\t// `m` drops; remaining 5 Arcs are released by HashMapCore's
\t// destructor walk; final-release frees Payload + String for
\t// each.
}
"""


_HM_REHASH_SOURCE = """\
module main;

import std.core as core;
import std.containers as containers;
import std.format as fmt;

pub fn main() nothrow -> Int {
\t// Start at default capacity (0) and force several rehashes.
\tvar m = containers.hash_map<type String, String>();
\tvar i = 0;
\twhile i < 64 {
\t\tval k = "key-" + fmt.format_int(i);
\t\tval v = "val-" + fmt.format_int(i);
\t\tval prev = m.insert(move k, move v);
\t\tmatch prev {
\t\t\tOptional::Some(p) => { core.drop_value<type String>(move p); },
\t\t\tOptional::None() => {}
\t\t}
\t\ti = i + 1;
\t}
\treturn m.len();
\t// `m` drops at scope exit; all 64 slots released structurally.
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str, int]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text, error_count)."""
	if shutil.which("valgrind") is None:
		pytest.skip("valgrind not available")

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
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
		capture_output=True, text=True, timeout=180,
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
			f"  - `stdlib/std/containers/array.drift::HashMapCore` (insert/remove/destroy)\n"
			f"  - `lang/driftc/stage2/hir_to_mir.py::IntrinsicKind.MAYBE_*`\n"
			f"  - `lang/driftc/stage2/overwrite_cleanup.py` (StoreRef overwrite release)\n"
			f"Valgrind error count: {errors}\n\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)


def test_hash_map_string_string_insert_get_remove_drop_clean(tmp_path: Path) -> None:
	"""HashMap<String, String> end-to-end through the
	`RawBuffer<MaybeUninit<String>>` slot model: insert (which
	uses `mem.maybe_write` per key/value), `get` (which uses
	`mem.maybe_assume_init_ref`), `remove` (which uses
	`mem.maybe_assume_init_read`), and final scope-exit drop
	(which walks only `HASH_MAP_STATE_FULL` slots via
	`HashMapCore::destroy`).

	A regression in any of those buffer-side intrinsic lowerings
	or in the destructor's slot walk would surface as either a
	leaked String allocation (definitely lost) or an Invalid
	read/write against a tombstoned slot.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, _HM_STRING_SOURCE, label="hm_string_string"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="hm_string_string",
		broken_state_hint=(
			"either an inserted String slot was not released by "
			"`HashMapCore::destroy` (likely a regression in the "
			"slot-state walk, the per-slot `mem.maybe_assume_init_read` "
			"lowering, or the StoreRef drop-before-overwrite path), "
			"or a removed value's stake was double-released (the "
			"slot tombstone fired the String release twice)."
		),
	)


def test_hash_map_string_arc_payload_clean(tmp_path: Path) -> None:
	"""HashMap<String, Arc<Payload>> exercises the +1 Arc strong-
	stake transfer through `mem.maybe_write` (insert) and
	`mem.maybe_assume_init_read` (remove), plus structural drop
	through `HashMapCore::destroy` for entries left in the map.

	Payload carries a String so a leaked Arc surfaces as a
	definitely-lost String allocation — Arc's inline bytes alone
	might be allocator-reused and evade the leak summary.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, _HM_ARC_SOURCE, label="hm_string_arc"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="hm_string_arc",
		broken_state_hint=(
			"Arc strong-refcount stake leaked or double-released "
			"through the buffer-internal MaybeUninit slot model.  "
			"Inspect `HashMapCore::insert`'s `mem.maybe_write` per-"
			"slot path, `HashMapCore::remove`'s "
			"`mem.maybe_assume_init_read` per-slot path, and the "
			"destructor's slot walk over `HASH_MAP_STATE_FULL`."
		),
	)


def test_hash_map_rehash_no_leak(tmp_path: Path) -> None:
	"""Insert 64 entries against an initial capacity of 0, forcing
	`HashMapCore::ensure_capacity` to allocate fresh buffers and
	re-insert via `_insert_into_buffers` several times.  Each
	rehash moves values from the old buffer (`mem.maybe_assume_init_read`)
	into the new buffer (`mem.maybe_write`); the old buffer is
	then `mem.dealloc`-ed with all slots already moved out.

	A regression in the rehash move-or-drop chain would surface
	as either a leaked old slot or a double-release on the
	moved value.  The 64-entry size makes per-rehash leaks
	visible in the summary.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, _HM_REHASH_SOURCE, label="hm_rehash"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="hm_rehash",
		broken_state_hint=(
			"the rehash path leaked slot contents or double-"
			"released them.  Inspect "
			"`HashMapCore::ensure_capacity` (`array.drift:1012`), "
			"`_insert_into_buffers` (`array.drift:960`), and the "
			"old-buffer dealloc tail of the rehash."
		),
	)
