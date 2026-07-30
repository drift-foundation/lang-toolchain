# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
`mem.replace<type T>` for non-Copy / refcount types — memcheck
carriers covering the original String UAF and the fix's
generalisation across container shapes.

**History.**  A use-after-free in `mem.replace<type String>` was
surfaced 2026-04-26 during the Array audit follow-up while
attempting to write a `&mut a[i]` disjoint-index drop/refcount
carrier:

\t\tvar s: String = fmt.format_int(700);
\t\tval new_s: String = fmt.format_int(800);
\t\tval old_s = mem.replace<type String>(&mut s, move new_s);
\t\t# old_s.byte_length() reads freed memory.

Valgrind reported `Invalid read` in `drift_string_retain` /
`drift_string_release` against the address that `mem.replace`'s
synthesized drop-before-overwrite had just freed.  Reduces below
`Array<String>` slot or struct field — the bug was in the
`mem.replace` lowering itself, not in the place expression.

**Root cause** (verified 2026-04-26): `mem.replace(place, v)`
lowering at `lang/driftc/stage2/hir_to_mir.py` emitted

\t\tLoadRef(old_val, ptr=…)        # raw pointer copy, no retain
\t\t…lower(new_val)…
\t\tStoreRef(ptr=…, value=new_val)
\t\treturn old_val

`overwrite_cleanup`'s StoreRef rewrite (moved out of string_arc in Slice B1)
intercepts every `StoreRef` to a String place and synthesizes
`LoadRef + StringRelease` of the prior slot value
(drop-before-overwrite).  For non-Copy refcount types this released
the original allocation while `old_val` still held the now-dangling
pointer.

**Fix landed** (same day, this branch):
`hir_to_mir.py::IntrinsicKind.REPLACE` rewritten as

\t\tptr = address(place)            # may evaluate index / address parts
\t\tnew_val = lower/consume(new)    # MUST happen before the slot mutates
\t\tMoveFromRef(local=__replace_old_*, ptr=ptr, inner_ty=T)
\t\tStoreRef(ptr=ptr, value=new_val, inner_ty=T)
\t\told_val = MoveOut(local=__replace_old_*, ty=T)
\t\treturn old_val

Ordering matters.  `MoveFromRef` is mutating (it tombstones `*ptr`),
so lowering / consuming the replacement value must complete before
the destination is touched.  An aborted replacement-expression
lowering (throwing or otherwise abandoned) would otherwise leave
the place tombstoned.

`MoveFromRef` itself is the existing match-cleanup-authoring
ownership-transfer primitive (see `mir_nodes.py::MoveFromRef`).
String-arc's `StoreRef` rewrite still runs but now reads the
tombstoned (zero) bytes and does `drift_string_release(null)` — a
documented runtime no-op (see the MoveFromRef contract in mir_nodes.py).

**Carriers in this file.**  One per shape, exercising the same
fix path through different place-expression and element-type
surfaces:

  1. **String var** — the original repro.  Plain `String` local
     read / written via `&mut s`.
  2. **`Array<String>` slot** — `mem.replace<type String>(&mut arr[i], …)`,
     the shape that surfaced the bug initially.
  3. **`Arc<T>` var** — refcount-bearing user-level type with its
     own retain/release protocol; tests that the fix transfers
     the +1 stake correctly without bumping refcount twice.
  4. **Struct-with-String-field var** — destructible struct
     replaced via `&mut`.  Per `MoveFromRef`'s docstring
     (`mir_nodes.py:621-633`), tombstone bytes are NOT drop-safe
     under user-Destructible struct destructors.  This carrier
     exercises the standard "no user destructor; structural drop
     of String fields only" path; if a separate tombstone /
     destructor contract problem exists it would surface here.

If carrier #4 fails, freeze it and report as a separate
LANGUAGE_BUG (per the guidance from the fix-authorisation memo).

**The disjoint-index swap question (recorded for reference).**

The original audit asked whether a `&mut a[i]` disjoint-index
runtime carrier exists via `mem.swap(&mut arr[i], &mut arr[j])`.
The borrow checker rejects that shape at compile time
(`E-AUTO-0215fdfe`: conflicting borrows in the same statement),
regardless of whether `i != j` is provable.  No realistic runtime
carrier exists for disjoint-index swap on the same array; the e2e
tests at `borrow_array_elem_mut` /
`borrow_array_elem_shared_then_mut_disjoint_ok` /
`borrow_struct_array_disjoint_index_write_ok` cover the
single-borrow shape and that is the available coverage.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


# Shape 1: plain String var, no array, no struct.  The smallest
# reproducer; was the regression-first carrier for the original
# fix.
MEM_REPLACE_STRING_VAR_SOURCE = """\
module main;

import std.mem as mem;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar s: String = fmt.format_int(700);
\tval new_s: String = fmt.format_int(800);
\tval old_s = mem.replace<type String>(s, move new_s);
\tval total = old_s.byte_length() + s.byte_length();
\treturn total;
}
"""


# Shape 2: Array<String> slot, addressed via &mut arr[i].  This is
# the shape that surfaced the bug originally.  Walks every slot of
# a populated Array<String>, replacing each with a fresh
# heap-allocated String; collects the old strings for byte_length
# accounting (forces them to stay live until the drop point).
MEM_REPLACE_ARRAY_STRING_SLOT_SOURCE = """\
module main;

import std.mem as mem;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar arr: Array<String> = [];
\tarr.push(fmt.format_int(700));
\tarr.push(fmt.format_int(701));
\tarr.push(fmt.format_int(702));
\tarr.push(fmt.format_int(703));
\tvar total = 0;
\tvar i = 0;
\twhile i < arr.len {
\t\tval new_s = fmt.format_int(i + 800);
\t\tval old_s = mem.replace<type String>(arr[i], move new_s);
\t\ttotal = total + old_s.byte_length();
\t\ti = i + 1;
\t\t// old_s drops at end of iteration; arr now holds rotated
\t\t// strings (the 800-series).
\t}
\treturn total;
\t// arr drops here, releasing the four 800-series strings.
}
"""


# Shape 3: Arc<T>.  Refcount-bearing user-level type; the fix must
# preserve the +1 stake transfer without bumping or dropping any
# refcount.  Two separate Arcs exist before replace; afterwards the
# slot holds the new Arc and old_val holds the original — each
# with refcount 1.  Both decrement to 0 at scope-exit.
#
# Inner type carries a String to make any leaked allocation
# visible (Int alone wouldn't show up under definitely-lost — Arc's
# inline storage might be re-used; the String allocation is the
# witness).
MEM_REPLACE_ARC_SOURCE = """\
module main;

import std.mem as mem;
import std.concurrent as conc;
import std.format as fmt;

struct Payload {
\ts: String,
\ttag: Int
}

pub fn main() nothrow -> Int {
\tvar a = conc.arc(Payload(s = fmt.format_int(700), tag = 7));
\tval b = conc.arc(Payload(s = fmt.format_int(800), tag = 8));
\tval old_a = mem.replace<type conc.Arc<Payload>>(a, move b);
\tval old_payload_ref = old_a.get();
\tval new_payload_ref = a.get();
\treturn old_payload_ref.tag + new_payload_ref.tag;
\t// a (now holding old b's Arc) drops → refcount 1 → 0 → frees
\t//   Payload (and its String).
\t// old_a drops → refcount 1 → 0 → frees original Payload (and
\t//   its String).
}
"""


# Shape 4: Struct with a String field, no user destructor.  The
# struct's drop is structural (per-field): the String field gets
# StringRelease on drop, but no user destructor runs on the
# tombstoned slot.  Per MoveFromRef's docstring this is the safe
# shape — caller's contract is honoured because the slot is
# overwritten by StoreRef before any drop runs on it.
MEM_REPLACE_DESTRUCTIBLE_STRUCT_SOURCE = """\
module main;

import std.mem as mem;
import std.format as fmt;

struct Wrap {
\ts: String,
\ttag: Int
}

pub fn main() nothrow -> Int {
\tvar w: Wrap = Wrap(s = fmt.format_int(700), tag = 7);
\tval new_w: Wrap = Wrap(s = fmt.format_int(800), tag = 8);
\tval old_w = mem.replace<type Wrap>(w, move new_w);
\tval total = old_w.s.byte_length() + w.s.byte_length() + old_w.tag + w.tag;
\treturn total;
\t// old_w drops here (structural drop releases its String).
\t// w drops at scope-exit (structural drop releases its String).
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str, int]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text, error_count)."""
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
			f"  - `lang/driftc/stage2/hir_to_mir.py` (REPLACE intrinsic lowering)\n"
			f"  - `lang/driftc/stage2/overwrite_cleanup.py` (StoreRef overwrite release)\n"
			f"  - `lang/driftc/stage2/mir_nodes.py::MoveFromRef` (tombstone contract)\n"
			f"Valgrind error count: {errors}\n\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)


def test_mem_replace_string_var_no_uaf(tmp_path: Path) -> None:
	"""Shape 1 — `mem.replace<type String>(&mut s, move new_s)` on a
	plain String var.  This was the original regression-first carrier;
	it pins the fix at `hir_to_mir.py::IntrinsicKind.REPLACE`.

	If this test fails, the lowering has regressed: either the
	`MoveFromRef + StoreRef + MoveOut` chain no longer tombstones
	the slot before `overwrite_cleanup`'s StoreRef rewrite, or one of the
	three instructions has been reordered to mutate the destination
	before the replacement value is fully lowered.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, MEM_REPLACE_STRING_VAR_SOURCE, label="mem_replace_string_var"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="mem_replace_string_var",
		broken_state_hint=(
			"`old_s` returned by mem.replace dangles because the "
			"synthesized drop-before-overwrite at "
			"`overwrite_cleanup` released the original String "
			"before the caller could use the returned value."
		),
	)


def test_mem_replace_array_string_slot_no_uaf(tmp_path: Path) -> None:
	"""Shape 2 — `mem.replace<type String>(&mut arr[i], move new_s)`.

	This is the place-expression that surfaced the bug originally.
	The address-of-place evaluator must produce a `&mut` to the
	indexed slot; the rest of the lowering chain is identical to
	shape 1.  Per-iteration leaks surface as multi-block reports
	because the loop runs four times.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, MEM_REPLACE_ARRAY_STRING_SLOT_SOURCE, label="mem_replace_array_string_slot"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="mem_replace_array_string_slot",
		broken_state_hint=(
			"either the indexed-slot &mut lowering regressed, or the "
			"slot-level MoveFromRef → StoreRef chain leaked / "
			"double-released the per-slot String stake."
		),
	)


def test_mem_replace_arc_no_uaf(tmp_path: Path) -> None:
	"""Shape 3 — `mem.replace<type Arc<Payload>>(&mut a, move b)`.

	Refcount-bearing user-level type.  Each Arc holds a +1 strong
	reference; after replace, the slot has b's Arc (+1) and old_val
	has a's original Arc (+1) — total +2 stakes across two values
	pointing at two distinct allocations.  Both decrement to 0
	independently at scope-exit.

	A leak here would mean the +1 stake transfer was lost (extra
	free) or duplicated (extra retain).  An Invalid read / free
	would mean the slot's tombstone bytes were dropped through the
	Arc destructor protocol — which `MoveFromRef`'s contract (no
	user-Destructible-struct guard) explicitly disclaims for
	user-destructible types.  Arc<T> is a stdlib-defined struct
	with a structural drop chain (ArcHeader.drop_thunk runs on
	final release); the tombstone is null and the structural drop
	never runs on it because StoreRef immediately overwrites it.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, MEM_REPLACE_ARC_SOURCE, label="mem_replace_arc"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="mem_replace_arc",
		broken_state_hint=(
			"Arc strong-refcount stake transfer regressed: either the "
			"old Arc stake was double-released (Invalid free) or the "
			"slot's Arc was leaked (definitely lost = sizeof(Payload) + "
			"sizeof(String allocation))."
		),
	)


def test_mem_replace_destructible_struct_no_uaf(tmp_path: Path) -> None:
	"""Shape 4 — `mem.replace<type Wrap>(&mut w, move new_w)` where
	`Wrap` is a struct with a String field and no user destructor
	(structural drop only).

	Per `MoveFromRef`'s docstring (`mir_nodes.py:621-633`), tombstone
	bytes are NOT drop-safe under user-Destructible struct
	destructors.  This carrier covers the structural-drop case (no
	user destructor): the tombstone is per-field zero, and StoreRef
	immediately overwrites it before any drop runs.  Result should
	be clean.

	**If this test fails**, freeze it and report as a separate
	LANGUAGE_BUG — the fix authorisation explicitly carved this
	shape out as something to investigate independently if a
	tombstone / destructor contract problem surfaces.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, MEM_REPLACE_DESTRUCTIBLE_STRUCT_SOURCE, label="mem_replace_destructible_struct"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="mem_replace_destructible_struct",
		broken_state_hint=(
			"either the struct's String field stake was leaked "
			"(definitely lost ~24 bytes per missed StringRelease) or "
			"the per-field structural drop fired on tombstoned bytes "
			"(Invalid free / Invalid read)."
		),
	)
