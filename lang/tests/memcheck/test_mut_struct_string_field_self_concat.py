# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG carrier: `&mut struct` String field self-concat assignment.

**Shape.**

	struct Ctx { pub s: String }

	fn mutate(ctx: &mut Ctx) nothrow -> Void {
		ctx.s = ctx.s + "A";
	}

	pub fn main() nothrow -> Int {
		var ctx = Ctx(s = fmt.format_int(7));   // heap-seeded, not literal
		mutate(&mut ctx);
		if ctx.s.byte_length() == 0 { return 1; }
		return 0;
	}

**Origin.**  Surfaced 2026-04-27 in the build-orchestrator drift-web
test (`run 20260427-154225`) as a UAF / leak inside
`web.rest.tests.unit.dispatch_test::scenario_filter_order_preserved`.
The dispatch_test scenario uses two filters wired through explicit
`core.callback2(_filter_a/_filter_b)`, each of which assigns
`ctx.principal_sub = ctx.principal_sub + "X"` through `&mut Context`.
Reduction to the minimal carrier (this test) showed that the callback
indirection is **not** load-bearing: a single `&mut struct` String
field self-concat is enough, provided the field already holds a
heap-allocated String (literal initialisers like `""` or `"seed"` are
static and `drift_string_release` is a no-op on them, masking the
bug).

**Symptoms (broken state).**
- `definitely lost: 19+ bytes` — the new concat result allocation is
  leaked.
- 5 errors / 3 contexts — `Invalid read of size 1`, `Invalid read of
  size 8`, then `SIGABRT` from `drift_string_release` on the freed
  old-slot value (double release).
- Plain run on the two-call shape additionally aborts with
  `malloc(): unaligned tcache chunk detected`.

**Root cause (landed fix).**  `_emit_assign_store_ref` in
`lang/driftc/stage2/hir_to_mir.py` previously emitted

	LoadRef(old_val, ptr, inner_ty)
	ZeroValue(zero_val, inner_ty)
	StoreRef(ptr, value=zero_val, inner_ty)      # zero-tombstone
	DropValue(old_val, ty=inner_ty)
	StoreRef(ptr, value=new_val, inner_ty)

For `inner_ty == String`, `overwrite_cleanup` (Slice B1,
2026-07-20; the StoreRef overwrite release moved out of `string_arc`)
rewrites every `StoreRef` to a String place as
`LoadRef + StringRelease + StoreRef` — so the zero-tombstone
`StoreRef` became a real release of the
old slot value, and the explicit `DropValue` then released the same
allocation a second time via the SSA snapshot loaded BEFORE the
tombstone.  Two real releases of the same allocation; the new
concat value leaked because the runtime aborted on the second
release before main's scope-drop could run.

**Fixed lowering (canonical replace-store sequence).**  Assignment
to an owning place is replacement: evaluate RHS, transfer the old
occupant out of the place, drop that old occupant, then store the
new occupant.  `_emit_assign_store_ref` now emits

	MoveFromRef(local=__assign_old_*, ptr, inner_ty)   # atomic tombstone + capture old
	MoveOut(old_val, __assign_old_*, ty=inner_ty)      # drain to SSA
	DropValue(old_val, ty=inner_ty)                    # single release of old occupant
	StoreRef(ptr, value=new_val, inner_ty)             # store new into tombstoned slot

The old-value release authority is the explicit `DropValue` on the
moved-out SSA — exactly one release per assignment, regardless of
type.  For String places `overwrite_cleanup`'s rewrite of the final
`StoreRef` still synthesizes a `LoadRef + StringRelease`, but it
fires against the **already-tombstoned** slot (null bytes);
`drift_string_release(null)` is a documented runtime no-op (see
`stage2/string_arc.py:1097-1099`), so the rewrite is correct
without changes.

**Self-referential RHS is safe at this layer.**
`_visit_stmt_HAssign` lowers the RHS into `value` BEFORE invoking
`_emit_assign_store_ref` (`val = self._lower_owning_consume(stmt.value, ...)`
at `hir_to_mir.py:6808`), so `ctx.s = ctx.s + "A"` has its
self-load fully materialised in `value` by the time the slot is
tombstoned.  Tombstoning at this point cannot race a still-pending
RHS read.

**Type-agnostic.**  The same `MoveFromRef → MoveOut → DropValue →
StoreRef` sequence applies to any drop-bearing `T` behind a place
(`String`, `Arc<U>`, Destructible struct, ...).  `String` exposed
the legacy bug only because `string_arc` rewrites `StoreRef` for
String places — for other drop-bearing types the legacy redundancy
was silent but still wrong shape.  No String-special-case lives in
the helper.

The contract pin at
`lang/tests/stage2/test_assign_store_ref_drop_bearing_lowering.py`
asserts the canonical post-string_arc shape: exactly one
`M.MoveFromRef` with an `__assign_old_*`-prefixed local at the
field ptr, no `M.StoreRef` whose value is a `M.ZeroValue` dest
(catches a legacy zero-tombstone resurrection under any naming),
and exactly one `M.DropValue(String)`.

**Carriers.**
1. `heap_seeded_one_call` — the irreducible carrier.  Field
   initialised with `fmt.format_int(7)` (heap), one self-concat.
2. `literal_seeded_two_calls` — masking-defeated form: two
   sequential self-concats; the first turns `""` into a heap value,
   the second trips the bug.  Mirrors the dispatch_test shape.
3. `literal_seeded_one_call` — control / negative-mask check.  One
   self-concat into a literal-initialised field is currently clean
   (the `drift_string_release(static)` no-op masks the bug).  Pinned
   here so a future change that *removes* literal masking and
   surfaces the bug on this case as well is caught explicitly.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


HEAP_SEEDED_ONE_CALL_SOURCE = """\
module main;

import std.format as fmt;

struct Ctx {
\tpub s: String
}

fn mutate(ctx: &mut Ctx) nothrow -> Void {
\tctx.s = ctx.s + "A";
}

pub fn main() nothrow -> Int {
\tvar ctx = Ctx(s = fmt.format_int(7));
\tmutate(&mut ctx);
\tif ctx.s.byte_length() == 0 { return 1; }
\treturn 0;
}
"""


LITERAL_SEEDED_TWO_CALLS_SOURCE = """\
module main;

struct Ctx {
\tpub s: String
}

fn filter_a(ctx: &mut Ctx) nothrow -> Void {
\tctx.s = ctx.s + "A";
}

fn filter_b(ctx: &mut Ctx) nothrow -> Void {
\tctx.s = ctx.s + "B";
}

pub fn main() nothrow -> Int {
\tvar ctx = Ctx(s = "");
\tfilter_a(&mut ctx);
\tfilter_b(&mut ctx);
\tif ctx.s.byte_length() != 2 { return 1; }
\treturn 0;
}
"""


LITERAL_SEEDED_ONE_CALL_SOURCE = """\
module main;

struct Ctx {
\tpub s: String
}

fn mutate(ctx: &mut Ctx) nothrow -> Void {
\tctx.s = ctx.s + "A";
}

pub fn main() nothrow -> Int {
\tvar ctx = Ctx(s = "seed");
\tmutate(&mut ctx);
\tif ctx.s.byte_length() != 5 { return 1; }
\treturn 0;
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
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access — "
			f"{broken_state_hint}\n"
			f"Touch points:\n"
			f"  - `lang/driftc/stage2/hir_to_mir.py::_emit_assign_store_ref` "
			f"(replace-store lowering — must emit `MoveFromRef → MoveOut → "
			f"DropValue → StoreRef`, not the legacy `LoadRef + ZeroValue + "
			f"StoreRef(zero) + DropValue + StoreRef(new)` shape)\n"
			f"  - `lang/driftc/stage2/mir_nodes.py::MoveFromRef` (atomic "
			f"tombstone + capture-into-local primitive)\n"
			f"  - `lang/driftc/stage2/string_arc.py:1108-1121` (String StoreRef "
			f"rewrite — its release of the post-tombstone slot is a "
			f"`drift_string_release(null)` no-op, NOT the authoritative drop)\n"
			f"Stage2 contract pin: "
			f"`lang/tests/stage2/test_assign_store_ref_drop_bearing_lowering.py`\n"
			f"Valgrind error count: {errors}\n\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)
	assert lost == 0, (
		f"[{label}] {lost} bytes definitely lost.\n"
		f"Expected symptom: {broken_state_hint}\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)


def test_heap_seeded_one_call_no_uaf(tmp_path: Path) -> None:
	"""Carrier 1 — irreducible.  Field heap-seeded via
	`fmt.format_int(7)`, one `mutate(&mut ctx)` call performing
	`ctx.s = ctx.s + "A"`.

	The fixed lowering at `_emit_assign_store_ref` produces exactly
	one user-visible release of the old field value: the explicit
	`DropValue` on the SSA drained out of the `MoveFromRef`
	tombstone-and-capture local.  `string_arc`'s rewrite of the
	final `StoreRef` does add a `LoadRef + StringRelease` against
	the slot, but the slot has already been tombstoned to null bytes
	by `MoveFromRef`, so that release is a `drift_string_release(null)`
	runtime no-op.

	Failure modes this carrier catches:
	- A regression that reverts `_emit_assign_store_ref` to the
	  legacy `LoadRef + ZeroValue + StoreRef(zero) + DropValue`
	  shape — the zero-tombstone `StoreRef` plus the explicit
	  `DropValue` would re-emit two real releases of the old value
	  via `overwrite_cleanup`'s String StoreRef rewrite (moved out of
	  string_arc in Slice B1) (`Invalid read` / `SIGABRT` from
	  `drift_string_release`'s tcache check).
	- A regression that drops the `MoveFromRef` so the slot is no
	  longer tombstoned before the final `StoreRef` — the final
	  rewrite's release would then fire on the still-live old value
	  while the explicit `DropValue` already released it.
	- A regression that loses the explicit `DropValue` — the old
	  value would leak (`definitely lost`).
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, HEAP_SEEDED_ONE_CALL_SOURCE, label="heap_seeded_one_call"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="heap_seeded_one_call",
		broken_state_hint=(
			"`ctx.s = ctx.s + \"A\"` through `&mut Ctx` is no longer "
			"lowered as `MoveFromRef → MoveOut → DropValue → StoreRef`. "
			"Either the slot is not tombstoned before the final "
			"`StoreRef` (so `string_arc`'s rewrite fires a real release "
			"on the live old value alongside the explicit `DropValue`), "
			"or the explicit `DropValue` is gone (old-value leak)."
		),
	)


def test_literal_seeded_two_calls_no_uaf(tmp_path: Path) -> None:
	"""Carrier 2 — masking-defeated form.  Two sequential filter
	functions; first turns the literal-seeded `""` into a heap `"A"`,
	second trips the bug on the heap value.

	This shape is what dispatch_test's
	`scenario_filter_order_preserved` exercises (minus the
	`Callback2` indirection, which the reduction matrix proved is
	incidental to the bug).
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, LITERAL_SEEDED_TWO_CALLS_SOURCE, label="literal_seeded_two_calls"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="literal_seeded_two_calls",
		broken_state_hint=(
			"two sequential self-concats on the same `&mut Ctx.s`: the "
			"second mutate trips the same double-release / leak as "
			"carrier 1, because by then the field holds a heap value."
		),
	)


def test_literal_seeded_one_call_no_uaf(tmp_path: Path) -> None:
	"""Carrier 3 — negative mask.  One self-concat into a
	literal-initialised field (`"seed"`).  Currently clean because
	`drift_string_release(static)` is a no-op so the
	double-release-of-old has no observable effect.

	Pinned so that any future change that lifts the static-literal
	masking — e.g. switching String literals to a refcounted
	allocation — surfaces this carrier failing immediately rather
	than producing surprise UAFs in apps that initialise fields with
	literals.
	"""
	lost, vg_log, errors = _compile_and_valgrind(
		tmp_path, LITERAL_SEEDED_ONE_CALL_SOURCE, label="literal_seeded_one_call"
	)
	_assert_clean(
		lost, vg_log, errors,
		label="literal_seeded_one_call",
		broken_state_hint=(
			"one self-concat into a literal-seeded field — currently "
			"clean only because `drift_string_release` on a static "
			"String is a no-op, masking the double-release.  If this "
			"fails, the masking is gone and the bug is now visible on "
			"every `&mut struct` String field assignment."
		),
	)
