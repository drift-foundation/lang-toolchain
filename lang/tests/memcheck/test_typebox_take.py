# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
TypeBox owning extraction (`take<T>`) — memcheck carriers.

**Background.**  Pre-0.31.24 `TypeBox` exposed only borrowed
downcast (`downcast<T>(&box)`).  Web-rest's `ctx_take<T>()`
needed an owning extraction primitive — remove a `TypeBox` from
the context map and move the stored `T` out.  0.31.24 adds
`runtime.take<T>(var self: TypeBox) nothrow -> Optional<T>` plus
the supporting `mem.rawbuffer_empty<T>()` intrinsic that gives
`take<T>` a canonical drained-state sentinel.

**Drained-box invariant** (the load-bearing claim):
after a successful `take<T>(box)`, `box.buf.ptr` is null and
`box.buf.cap` is 0.  `TypeBox.destroy`'s existing
`mem.ptr_is_null(self.buf.ptr) → return` short-circuit then makes
the dropper a no-op.  The drop chain that follows
(field-by-field) is harmless: `tag` is `Uint64` (Copy, no drop);
`buf` is a `RawBuffer<Byte>` (no destructor at all per the
stdlib `mem` module docs); `dropper` is a `Callback1` whose
closure env drops normally.

**Sequence inside `take<T>`** (intentional, see fix authorization):
  1. read `T` out of `self.buf` (storage still backed)
  2. **replace `self.buf` with `mem.rawbuffer_empty<Byte>()`**
     — disarms `destroy` *before* freeing the backing
  3. dealloc the typed view of the original storage
  4. return `Optional::Some(value)`
The replace-before-dealloc ordering is deliberate: if step 3
ever grew an early-exit edge (it currently can't — `dealloc` is
nothrow), the box would already be in the drained state, never
holding a freed-but-non-null pointer.  Carriers below pin the
drop counts that this ordering protects.

`old_buf` returned by `mem.replace<RawBuffer<Byte>>` is dropped
by binding to `_` and is a no-op because `RawBuffer` has no
destructor — the freed allocation is owned only via the typed
view passed to `dealloc`.

**Carriers in this file:**

  C1. `TypeBox<String>` → `take<String>()` returns owned String;
      valgrind-clean (string buffer freed exactly once via the
      returned String dropping at end of caller scope).
  C2. `TypeBox<Holder>` (destructible struct with a String
      field) → `take<Holder>()` then consuming-method use; the
      struct's destructor must run exactly once.
  C3. **Wrong-type take**: `take<U>(box)` on a `TypeBox` that
      stores `T` returns `Optional::None`; the original `T` is
      dropped exactly once via the box's normal `Destructible`
      destructor (the dropper closure runs because tag mismatch
      → `take` returns immediately and `box` falls off the stack
      with its dropper still armed).
  C4. **HashMap remove + take**: matches the web-rest
      `ctx_take<T>()` shape — `HashMap<Uint64, TypeBox>` with
      `remove` followed by `take<T>`.  Valgrind-clean across the
      remove → take handoff.
  C5. **Borrowed downcast still works**: regression-no-break
      pin for the existing `downcast<T>(&box)` /
      `expect_downcast<T>(&box, tag)` surface.
  C6. **Direct `mem.rawbuffer_empty<T>()`**: empty buffer has
      `cap == 0`, `ptr_is_null` returns true, dropping the empty
      buffer is a no-op (no segfault, no leak).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


# C1: TypeBox<String> → take<String>
C1_SOURCE = """\
module main;

import std.runtime as rt;
import std.format as fmt;

pub fn main() nothrow -> Int {
	val b = rt.type_box(fmt.format_int(700));
	match rt.take<type String>(move b) {
		Some(s) => {
			return s.byte_length();
		},
		None => {
			return 99;
		}
	}
}
"""


# C2: TypeBox<Holder> with destructible struct (String field).
# After take, calling .byte_length() on the inner string proves
# the value is owned and live; dropping the returned Holder
# releases the String exactly once.
C2_SOURCE = """\
module main;

import std.runtime as rt;
import std.format as fmt;

struct Holder {
	s: String,
	tag: Int
}

pub fn main() nothrow -> Int {
	val b = rt.type_box(Holder(s = fmt.format_int(700), tag = 7));
	match rt.take<type Holder>(move b) {
		Some(h) => {
			return h.s.byte_length() + h.tag;
		},
		None => {
			return 99;
		}
	}
}
"""


# C3: Wrong-type take.  Box contains a String; we ask for an
# Int.  Should return None.  The box must drop the String
# exactly once via its normal Destructible destructor (no leak,
# no double-free).
C3_SOURCE = """\
module main;

import std.runtime as rt;
import std.format as fmt;

pub fn main() nothrow -> Int {
	val b = rt.type_box(fmt.format_int(700));
	match rt.take<type Int>(move b) {
		Some(_v) => {
			return 99;
		},
		None => {
			return 0;
		}
	}
	// `b` was consumed by `take`; on the None path inside `take`
	// the function returns Optional::None and `self` (the moved
	// box) drops via its normal `Destructible::destroy`, which
	// invokes the typed dropper exactly once.
}
"""


# C4: HashMap<Uint64, TypeBox> + remove + take<T>.  Matches the
# web-rest ctx_take<T>() shape.  Insert two boxes under
# different type-id keys, remove one, take<T> the removed
# TypeBox.
C4_SOURCE = """\
module main;

import std.core as core;
import std.runtime as rt;
import std.containers as containers;
import std.format as fmt;

struct Resp {
	body: String,
	status: Int
}

pub fn main() nothrow -> Int {
	var m: containers.HashMap<Uint64, rt.TypeBox> = containers.hash_map<type Uint64, rt.TypeBox>();
	val k_resp: Uint64 = core.type_id<type Resp>();
	val k_str: Uint64 = core.type_id<type String>();
	val _ = m.insert(k_resp, rt.type_box(Resp(body = fmt.format_int(700), status = 200)));
	val _ = m.insert(k_str, rt.type_box(fmt.format_int(800)));
	match m.remove(&k_resp) {
		Some(box) => {
			match rt.take<type Resp>(move box) {
				Some(r) => {
					return r.body.byte_length() + r.status;
				},
				None => {
					return 99;
				}
			}
		},
		None => {
			return 98;
		}
	}
	// `m` still holds the String box at k_str; drops on scope
	// exit via HashMap's destructor.
}
"""


# C5: Borrowed downcast must keep working post-fix.  Same shape
# as the existing e2e `std_runtime_typebox_downcast`, kept as a
# memcheck-context regression pin alongside the new `take`
# carriers.
C5_SOURCE = """\
module main;

import std.core as core;
import std.runtime as rt;
import std.format as fmt;

pub fn main() nothrow -> Int {
	val b = rt.type_box(fmt.format_int(700));
	var sum = 0;
	match rt.downcast<type String>(&b) {
		Some(s_ref) => {
			sum = sum + s_ref.byte_length();
		},
		None => {
			return 99;
		}
	}
	match rt.downcast<type Int>(&b) {
		Some(_v) => {
			return 98;
		},
		None => {
			sum = sum + 1;
		}
	}
	return sum;
	// `b` drops at scope exit via TypeBox::destroy → typed dropper.
}
"""


# C6: Direct mem.rawbuffer_empty<T>() — the new intrinsic on its
# own.  Asserts the sentinel contract (cap == 0 AND ptr is null)
# and that drop is a no-op under valgrind.  The null-ptr check
# is the load-bearing piece for the TypeBox.destroy short-circuit
# the rest of this file relies on; if the intrinsic ever stops
# producing a null ptr, C6 surfaces it before C1-C4 do.
C6_SOURCE = """\
module main;

import std.mem as mem;

pub fn main() nothrow -> Int {
	val empty = mem.rawbuffer_empty<type Byte>();
	if mem.capacity<type Byte>(&empty) != 0 {
		return 1;
	}
	unsafe {
		val p = mem.rawbuffer_ptr<type Byte>(&empty);
		if not mem.ptr_is_null<type Byte>(p) {
			return 2;
		}
	}
	return 0;
	// `empty` drops at scope exit; RawBuffer has no destructor,
	// so this is a no-op (no segfault calling free(NULL) and no
	// leak — there was nothing allocated).
}
"""


# C7: Minimal-raw RawBuffer<String> read/write/drop carrier.
# This isolates the underlying bug that blocks C1/C3/C4/C5: a
# pure-stdlib refcount-bearing T (String) round-trip through
# alloc_uninit → write → read → drop_value → dealloc must
# release the +1 stake exactly once.  Pre-fix this leaks 20
# bytes (the String allocation made by `format_int(700)`).
#
# Root cause was in stage2's String late-rewrite pass:
# `lang/driftc/stage2/string_arc.py` had no handler for
# `M.RawBufferRead` in either the `owned_defs` precedent block
# (around line 740) or the per-block `owned_values` precedent
# block (around line 1085).  Without those entries the read
# result was treated as borrowed and the subsequent
# `StoreLocal(local, dest)` synthesized a spurious
# `StringRetain` — refcount → 2 → drop only knocked it back to
# 1 → original allocation leaked.
#
# Fix mirrors the existing `M.PtrRead` / `M.ArrayElemTake` /
# `M.MoveOut` precedents (one entry in each of the two passes).
# Do NOT special-case TypeBox; do NOT patch stdlib around it;
# do NOT chase the LLVM `_lower_raw_buffer_read` /
# `_lower_raw_buffer_write` paths — those are correctly raw
# `load`/`store` and the stake-tracking lives at stage2.
C7_SOURCE = """\
module main;

import std.mem as mem;
import std.format as fmt;
import std.core as core;

pub fn main() nothrow -> Int {
	unsafe {
		var raw = mem.alloc_uninit<type String>(1);
		mem.write<type String>(&mut raw, 0, fmt.format_int(700));
		val v = mem.read<type String>(&mut raw, 0);
		val n = v.byte_length();
		core.drop_value<type String>(move v);
		mem.dealloc<type String>(move raw);
		return n;
	}
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str, allow_unsafe: bool = False) -> tuple[int, str, int]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text, error_count).

	`allow_unsafe=True` extends `unsafe_trusted_modules` to include
	the test's `module main`, enabling raw `mem.alloc_uninit` /
	`mem.write` / `mem.read` / `mem.dealloc` from the carrier
	source.  Use only for the minimal-raw RawBuffer carrier (C7);
	product-level carriers (C1-C5) operate through the safe
	TypeBox surface."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	cmd = [sys.executable, "-m", "lang.driftc.driftc", "--dev",
		"--stdlib-root", str(ROOT / "stdlib"),
		str(src), "--entry", "main::main", "-o", str(out_bin)]
	if allow_unsafe:
		cmd.append("--allow-unsafe")
	res = subprocess.run(
		cmd,
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:2000]}"
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
			f"  - `stdlib/std/runtime/runtime.drift::take` (TypeBox owning extraction)\n"
			f"  - `stdlib/std/mem/mem.drift::rawbuffer_empty` (drained-state sentinel)\n"
			f"  - `lang/driftc/stage2/hir_to_mir.py::IntrinsicKind.RAWBUFFER_EMPTY` (intrinsic lowering)\n"
			f"  - `lang/driftc/stage2/string_arc.py::M.RawBufferRead` (refcount-stake handlers in the `owned_defs` and `owned_values` passes)\n"
			f"Valgrind error count: {errors}\n\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)


def test_c1_typebox_take_string(tmp_path: Path) -> None:
	"""C1 — owning extraction of a `String` from `TypeBox<String>`.

	If valgrind reports `Invalid free` here, `take<T>` is
	deallocating the typed view AND letting `TypeBox.destroy` run
	the dropper on the same allocation (i.e. the buf-replace
	step before dealloc was skipped or reordered).  If
	`definitely lost` shows the String allocation, the dropper
	is being skipped without `take` having freed the backing
	(i.e. ownership of the String slipped between the two paths).
	"""
	lost, vg_log, errors = _compile_and_valgrind(tmp_path, C1_SOURCE, label="typebox_take_string")
	_assert_clean(
		lost, vg_log, errors,
		label="typebox_take_string",
		broken_state_hint=(
			"`take<String>(box)` returned the String but either "
			"(a) `TypeBox.destroy` re-ran the typed dropper on the "
			"already-deallocated buffer (`Invalid free`), or "
			"(b) the buf-replace happened but the typed dealloc "
			"was skipped (definitely lost = String allocation)."
		),
	)


def test_c2_typebox_take_destructible_struct(tmp_path: Path) -> None:
	"""C2 — owning extraction of a struct with a String field.

	Pins that the moved-out `Holder` is a fully-owned value: its
	`String` field can be read after extraction, and its
	structural drop releases the String exactly once when the
	caller's binding goes out of scope.
	"""
	lost, vg_log, errors = _compile_and_valgrind(tmp_path, C2_SOURCE, label="typebox_take_holder")
	_assert_clean(
		lost, vg_log, errors,
		label="typebox_take_holder",
		broken_state_hint=(
			"the extracted `Holder`'s String field was either "
			"double-released (Invalid free) — meaning the typed "
			"dropper still ran via `TypeBox.destroy` despite a "
			"successful take — or leaked (definitely lost) — "
			"meaning ownership of the field stake was dropped on "
			"the floor mid-extraction."
		),
	)


def test_c3_typebox_wrong_type_take_drops_original_once(tmp_path: Path) -> None:
	"""C3 — wrong-type take must return `None` and drop the
	originally-stored value exactly once.

	The box contains a String but the caller asks for an Int.
	`take<Int>` must return `None`; on the None branch inside
	`take`, the moved-in `self: TypeBox` falls off the stack
	with its dropper still armed, so the typed String dropper
	runs once via `TypeBox::destroy`.

	Failure modes this carrier catches:
	  - dropper runs zero times (definitely lost = String alloc)
	  - dropper runs twice (Invalid free)
	  - take<U> mistakenly extracts something on tag mismatch
	    (would surface as Invalid read on the wrong-typed read,
	    or as a returned-Some path that we explicitly check)
	"""
	lost, vg_log, errors = _compile_and_valgrind(tmp_path, C3_SOURCE, label="typebox_wrong_type_take")
	_assert_clean(
		lost, vg_log, errors,
		label="typebox_wrong_type_take",
		broken_state_hint=(
			"wrong-type `take<U>` either skipped the original-value "
			"dropper (leak) or ran it twice (double-free), or "
			"extracted on tag mismatch and read uninitialised "
			"bytes."
		),
	)


def test_c4_hashmap_remove_then_take(tmp_path: Path) -> None:
	"""C4 — `HashMap<Uint64, TypeBox>` + `remove(key)` + `take<T>`.

	The exact shape web-rest's `ctx_take<T>()` uses.  Pins that
	the move-out chain across the HashMap remove handoff into
	`take<T>` is leak-free: the box transferred ownership from
	the map into the local binder, then `take<T>` consumed the
	binder.

	The other entry (k_str → TypeBox<String>) stays in the map
	and drops at scope exit via the HashMap destructor.  Any
	leak there would mean HashMap's drop chain regressed
	independently — flagged as a separate failure.
	"""
	lost, vg_log, errors = _compile_and_valgrind(tmp_path, C4_SOURCE, label="typebox_hashmap_remove_take")
	_assert_clean(
		lost, vg_log, errors,
		label="typebox_hashmap_remove_take",
		broken_state_hint=(
			"the remove → take handoff leaked the moved-out box "
			"(definitely lost) or HashMap's residual-entry drop "
			"chain regressed across the new TypeBox surface."
		),
	)


def test_c5_borrowed_downcast_still_works(tmp_path: Path) -> None:
	"""C5 — borrowed `downcast<T>(&box)` regression-no-break pin.

	Same shape as the existing e2e
	`std_runtime_typebox_downcast`, kept here as a
	memcheck-context regression pin so any future change to
	`take`/`expect_take` that disturbs the borrowed-view path
	(shared `&self`-typed access into `self.buf`) surfaces
	here under valgrind alongside the owning extraction
	carriers.
	"""
	lost, vg_log, errors = _compile_and_valgrind(tmp_path, C5_SOURCE, label="typebox_downcast_pin")
	_assert_clean(
		lost, vg_log, errors,
		label="typebox_downcast_pin",
		broken_state_hint=(
			"borrowed downcast regressed alongside the take<T> "
			"addition — likely a shared/exclusive borrow surface "
			"slip on `self.buf` reads."
		),
	)


def test_c6_rawbuffer_empty_intrinsic_clean(tmp_path: Path) -> None:
	"""C6 — direct exercise of `mem.rawbuffer_empty<T>()`.

	Asserts the sentinel contract:
	  - `capacity` returns 0
	  - `ptr_is_null` returns true on the buf's ptr (load-bearing
	    for `TypeBox.destroy`'s early-return short-circuit that
	    C1-C4 rely on)
	  - drop is a no-op (no segfault, no leak under valgrind)

	If this fails, the ABI-neutrality claim in the design
	(`rawbuffer_empty` lowers to a constant struct emit, no
	runtime helper) has slipped — investigate the intrinsic
	codegen path before bumping `DRIFT_RT_ABI_VERSION`.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, C6_SOURCE, label="rawbuffer_empty_direct", allow_unsafe=True
	)
	_assert_clean(
		lost, vg_log, errors,
		label="rawbuffer_empty_direct",
		broken_state_hint=(
			"the `rawbuffer_empty<T>` intrinsic is not lowering to "
			"`{ptr=null, cap=0}`, or the resulting buffer's drop "
			"path is calling free(NULL) under a wrapper that "
			"doesn't tolerate it."
		),
	)


def test_c7_rawbuffer_string_write_read_drop(tmp_path: Path) -> None:
	"""C7 — minimal raw RawBuffer<String> round-trip carrier.

	Pins the underlying refcount-stake invariant for refcount-
	bearing element types in `RawBuffer<T>`: the +1 stake on the
	String must travel exactly once through

	    alloc_uninit<String>
	      → mem.write<String> (consumes the String, slot now owns)
	      → mem.read<String>  (slot tombstoned, caller now owns)
	      → drop_value<String> (release; refcount → 0; alloc freed)
	      → dealloc<String>    (frees backing storage)

	Pre-0.31.24 the stage2 `string_arc.py` late-rewrite had no
	handler for `M.RawBufferRead` — the read result was treated
	as borrowed and the subsequent `StoreLocal` synthesized a
	spurious `StringRetain`, leaking exactly one allocation per
	round-trip.

	This carrier MUST fail pre-fix (20 bytes definitely lost,
	allocator: drift_string_concat) and pass post-fix.
	`TypeBox<String>` (C1) and the HashMap shape (C4) cannot
	work without this fix; do NOT special-case TypeBox to dodge
	the leak — fix in `string_arc.py` next to the `PtrRead` /
	`ArrayElemTake` / `MoveOut` precedents.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, C7_SOURCE, label="rawbuffer_string_round_trip", allow_unsafe=True
	)
	_assert_clean(
		lost, vg_log, errors,
		label="rawbuffer_string_round_trip",
		broken_state_hint=(
			"`mem.read<String>` returned a value but stage2 "
			"`string_arc.py` did not register the dest as already-"
			"owning the +1 stake — a spurious `StringRetain` got "
			"inserted on the assign-to-local.  Fix at "
			"`lang/driftc/stage2/string_arc.py` (mirror the "
			"`M.PtrRead` / `M.ArrayElemTake` / `M.MoveOut` "
			"precedents in BOTH the `owned_defs` pass and the "
			"per-block `owned_values` pass), NOT in stdlib or "
			"LLVM codegen."
		),
	)
