# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Consume-via-intrinsic ownership invariant — Path Y named bug class.

Invariant: **consume-by-value intrinsics must route non-Copy locals
through `_lower_owning_consume` / `MoveOut` before cleanup authoring
queries scope-exit state.**  Without it, the source local stays
`LIVE` in the ledger after the intrinsic call; cleanup_authoring
sees `LIVE` + `needs_drop=true` and authors a redundant scope-exit
destructor — running the user's destructor twice on the same
moved-from storage.

Background.  When an intrinsic consumes a value-by-value argument
(e.g. `mem.write(&mut buf, i, v)`, `mem.ptr_write(p, v)`,
`mem.maybe_write(slot, v)`, `mem.replace(&mut place, v)`), the
intrinsic-specific HIR→MIR lowering used to call `lower_expr` on the
consume operand directly, producing a bare `LoadLocal + <intrinsic>`
sequence with no MIR-level ownership transition for the source local.
Patch-1 / patch-3 `cleanup_authoring` then queries `verdict_at(local)`
at scope exit, sees state `LIVE`, returns `MUST_DROP`, and authors a
redundant drop — double-running the destructor.

The fix routes each consume operand through `_lower_owning_consume`
(same helper the `core.drop_value` lowering was switched to), which
emits `MoveOut(t, local, ty)` for non-Copy HVar / projection-free
HPlaceExpr arguments and falls back to `lower_expr` otherwise.

This file pins one driver-style lowering contract per intrinsic.
The end-to-end heap-corruption gate is in
`lang/tests/memcheck/test_consume_intrinsic_uaf_carrier.py`
(modeled on `test_patch3_nested_scope_uaf_regression.py`).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]


_FIXTURE_PROLOGUE = """\
module main;

import std.core as core;
import std.mem as mem;

pub struct Box {
\tpub n: Int,
}

implement core.Destructible for Box {
\tpub fn destroy(var self: Box) nothrow -> Void {
\t\treturn;
\t}
}
"""


_FIXTURE_MEM_WRITE_BODY = """\
pub fn main() nothrow -> Int {
\tvar b = Box(n = 1);
\tunsafe {
\t\tvar raw: mem.RawBuffer<Box> = mem.alloc_uninit<type Box>(1);
\t\tmem.write<type Box>(&mut raw, 0, b);
\t\tvar b2 = mem.read<type Box>(&mut raw, 0);
\t\tcore.drop_value<type Box>(b2);
\t\tmem.dealloc<type Box>(raw);
\t}
\treturn 0;
}
"""


_FIXTURE_PTR_WRITE_BODY = """\
pub fn main() nothrow -> Int {
\tvar b = Box(n = 1);
\tvar slot = Box(n = 0);
\tunsafe {
\t\tvar p = mem.ptr_from_ref_mut<type Box>(&mut slot);
\t\tmem.ptr_write<type Box>(p, b);
\t}
\treturn 0;
}
"""


_FIXTURE_REPLACE_BODY = """\
pub fn main() nothrow -> Int {
\tvar slot = Box(n = 1);
\tvar fresh = Box(n = 2);
\tvar old = mem.replace<type Box>(&mut slot, fresh);
\tcore.drop_value<type Box>(old);
\treturn 0;
}
"""


_FIXTURE_MAYBE_WRITE_BODY = """\
pub fn main() nothrow -> Int {
\tval b = Box(n = 1);
\tunsafe {
\t\tvar slot = mem.maybe_uninit<type Box>();
\t\tmem.maybe_write<type Box>(&mut slot, b);
\t\tval b2 = mem.maybe_assume_init_read<type Box>(&mut slot);
\t\tcore.drop_value<type Box>(b2);
\t}
\treturn 0;
}
"""


_DESTROY_RE = re.compile(r'call void @"Box::std.core.Destructible::destroy"')
_DRIFT_MAIN_RE = re.compile(r"define i64 @drift_main\(\) \{(.*?)^}", re.DOTALL | re.MULTILINE)


def _compile_to_ir(tmp_path: Path, source: str, *, allow_unsafe: bool) -> str:
	src = tmp_path / "main.drift"
	src.write_text(source)
	ir_path = tmp_path / "out.ll"
	cmd = [sys.executable, "-m", "lang.driftc.driftc", "--dev",
		"--stdlib-root", str(_ROOT / "stdlib"),
		str(src), "--entry", "main::main",
		"--emit-ir", str(ir_path)]
	if allow_unsafe:
		cmd.append("--allow-unsafe")
	res = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True, timeout=120)
	assert res.returncode == 0, f"compile failed: {res.stderr[:500]}"
	return ir_path.read_text()


def _count_destroys_in_drift_main(ir: str) -> int:
	m = _DRIFT_MAIN_RE.search(ir)
	assert m, "could not locate drift_main body"
	return len(_DESTROY_RE.findall(m.group(1)))


def _assert_consume_intrinsic_no_double_destroy(
	*, intrinsic_label: str, fixture_body: str, expected_destroys: int,
	tmp_path: Path, allow_unsafe: bool,
) -> None:
	"""Compile a fixture that consumes a non-Copy `Box` local through
	the intrinsic WITHOUT explicit `move`, then counts
	`Box::destroy` call sites in `drift_main`.

	`expected_destroys` is the POST-FIX count (the legitimate
	destructor runs: e.g. explicit `core.drop_value` calls +
	scope-exit drops of locals that still legitimately own a Box).
	Pre-fix, `cleanup_authoring` would author one additional
	redundant drop for the consumed local, so the count would be
	`expected_destroys + 1`."""
	source = _FIXTURE_PROLOGUE + fixture_body
	ir = _compile_to_ir(tmp_path, source, allow_unsafe=allow_unsafe)
	count = _count_destroys_in_drift_main(ir)
	assert count == expected_destroys, (
		f"consume-via-intrinsic regression for `{intrinsic_label}`: "
		f"drift_main must contain EXACTLY {expected_destroys} Box "
		f"destroy call(s).  Got {count}.  Pre-fix lowering for "
		f"`{intrinsic_label}` left the consumed local LIVE in the "
		f"ledger; cleanup_authoring authored a redundant scope-exit "
		f"drop (expected count + 1).  Invariant: consume-by-value "
		f"intrinsics must route non-Copy locals through "
		f"`_lower_owning_consume` / `MoveOut` before cleanup "
		f"authoring queries scope-exit state."
	)


def test_lowering_mem_write_emits_moveout_for_non_copy_local(tmp_path: Path) -> None:
	"""Post-fix: 1 destroy (explicit `core.drop_value(b2)` on the
	value read back out).  `b` is MOVED_OUT of at `mem.write`."""
	_assert_consume_intrinsic_no_double_destroy(
		intrinsic_label="mem.write (RAW_WRITE)",
		fixture_body=_FIXTURE_MEM_WRITE_BODY,
		expected_destroys=1,
		tmp_path=tmp_path,
		allow_unsafe=True,
	)


def test_lowering_mem_ptr_write_emits_moveout_for_non_copy_local(tmp_path: Path) -> None:
	"""Post-fix: 1 destroy (scope-exit drop of `slot`, which now
	holds `b`'s data after `mem.ptr_write`).  `b` is MOVED_OUT at
	the write."""
	_assert_consume_intrinsic_no_double_destroy(
		intrinsic_label="mem.ptr_write (PTR_WRITE)",
		fixture_body=_FIXTURE_PTR_WRITE_BODY,
		expected_destroys=1,
		tmp_path=tmp_path,
		allow_unsafe=True,
	)


def test_lowering_mem_replace_emits_moveout_for_non_copy_local(tmp_path: Path) -> None:
	"""Post-fix: 2 destroys — `core.drop_value(old)` on the returned
	previous value, and the scope-exit drop of `slot` (which now
	holds `fresh`'s data).  `fresh` is MOVED_OUT at the replace."""
	_assert_consume_intrinsic_no_double_destroy(
		intrinsic_label="mem.replace (REPLACE)",
		fixture_body=_FIXTURE_REPLACE_BODY,
		expected_destroys=2,
		tmp_path=tmp_path,
		allow_unsafe=False,
	)


def test_lowering_mem_maybe_write_emits_moveout_for_non_copy_local(tmp_path: Path) -> None:
	"""Post-fix: 1 destroy (explicit `core.drop_value(b2)` on the
	value read back out of the slot).  `b` is MOVED_OUT at the
	`mem.maybe_write` call; `slot` is `MaybeUninit<Box>` (no-drop
	type) so its scope-exit emits no destructor.  After
	`mem.maybe_assume_init_read`, the slot is tombstoned to zero
	bytes by the intrinsic itself; `b2` carries the +1 ownership
	stake until `drop_value` consumes it.

	`mem.maybe_uninit` lowering landed alongside this test
	(`hir_to_mir.py::IntrinsicKind.MAYBE_UNINIT` → single
	`ZeroValue(MaybeUninit<T>)`).  Pre-fix this test was skipped
	with `NotImplementedError`; the bug class is the shared
	consume-via-intrinsic invariant pinned by the three sibling
	tests in this file."""
	_assert_consume_intrinsic_no_double_destroy(
		intrinsic_label="mem.maybe_write (MAYBE_WRITE)",
		fixture_body=_FIXTURE_MAYBE_WRITE_BODY,
		expected_destroys=1,
		tmp_path=tmp_path,
		allow_unsafe=True,
	)
