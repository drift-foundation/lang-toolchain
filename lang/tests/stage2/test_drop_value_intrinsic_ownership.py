# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
HIR→MIR lowering + ledger ownership pin for `core.drop_value(...)`
intrinsic.

Background (patch 3 diagnosis, 2026-04-24):
The previous lowering of statement-form `core.drop_value(local)`
emitted `LoadLocal + DropValue` and called `_mark_moved` as side
metadata.  HIR-side `_moved_locals` knew the local was consumed,
but MIR carried no `MoveOut` transition — the ledger saw the
local as still `LIVE` after the `DropValue`.  Patch-3 nested-scope
migration's `cleanup_authoring` then queried the ledger and
authored a redundant scope-exit drop, causing a heap-use-after-
free in the fat-Arc-interface-views driver carrier.

The fix is to lower statement-form `core.drop_value(local)` for a
non-Copy local through `_lower_owning_consume`, which emits an
explicit `MoveOut(dest, local, ty)` followed by `DropValue(dest,
ty)` for HVar / projection-free HPlaceExpr arguments.  HIR-side
`_mark_moved` is preserved as a side effect of
`_lower_owning_consume`.

This file pins both layers:

  1. **HIR→MIR lowering** — for a non-Copy local arg, MIR contains
     the `MoveOut + DropValue` pair (not bare `LoadLocal +
     DropValue`).
  2. **Ledger state** — given that MIR shape, `verdict_at` after
     the DropValue point returns `MUST_NOT_DROP` (state =
     `MOVED_OUT`).

The end-to-end memcheck carrier
(`lang/tests/memcheck/test_patch3_nested_scope_uaf_regression.py`)
remains the proof that this pair unblocks safe nested-scope
migration in patch 3.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import (
	DropVerdict,
	LiveState,
	build_ledger,
)


_TY_OWNED = 101


def _drop_policy_stub(_ty: int) -> None:
	return None


def _make_func(name: str, *, locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="main", name=name, ordinal=0)
	return M.MirFunc(
		name=name,
		params=[],
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


# -- Ledger pin: MoveOut + DropValue → MOVED_OUT after ------------------


def test_ledger_sees_drop_value_via_moveout_as_consumption() -> None:
	"""Given the post-fix MIR shape (`MoveOut(t, inner, ty) +
	DropValue(t, ty)`), the ledger's transfer function MUST
	transition `inner` to `MOVED_OUT` at the `MoveOut` index, so
	`verdict_at` at any subsequent program point reads
	`MUST_NOT_DROP`.

	Pre-fix MIR (`LoadLocal + DropValue`, no MoveOut) does NOT
	transition state — the ledger still reports `LIVE`, and any
	`cleanup_authoring`-style downstream consumer would mistakenly
	authorize a redundant drop.  This test pins the post-fix
	contract."""
	func = _make_func("dv_consume", locals_=["inner"], types={"inner": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="inner", value="t_init"))
	# Post-fix lowering shape: MoveOut + DropValue
	entry.instructions.append(M.MoveOut(dest="t_drop", local="inner", ty=_TY_OWNED))
	entry.instructions.append(M.DropValue(value="t_drop", ty=_TY_OWNED))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# After the MoveOut at index 1, inner state is MOVED_OUT.
	assert ledger.state_pre(("entry", 2), "inner") is LiveState.MOVED_OUT, (
		"ledger transfer regression: MoveOut(_, inner, _) must "
		"transition `inner` → MOVED_OUT in the per-instruction "
		"snapshot at the MoveOut index.  Without this, downstream "
		"`verdict_at` queries (e.g. cleanup_authoring under patch 3) "
		"would author redundant drops on already-consumed locals."
	)
	# verdict_at after the DropValue must be MUST_NOT_DROP.
	assert ledger.verdict_at(("entry", 3), "inner", needs_drop=True) is DropVerdict.MUST_NOT_DROP, (
		"verdict_at at the post-DropValue cursor must be "
		"MUST_NOT_DROP — `inner` was consumed by the MoveOut at "
		"index 1.  This is the patch-3 unblocker contract: "
		"cleanup_authoring queries this verdict and must not author "
		"a redundant scope-exit drop."
	)


def test_ledger_sees_bare_loadlocal_dropvalue_as_unsound_pre_fix_shape() -> None:
	"""Negative pin documenting the PRE-FIX shape that motivated
	the fix.  `LoadLocal + DropValue` (no MoveOut) is the
	misrepresentation: MIR-level state is unchanged on a `LoadLocal`,
	so the ledger reports `inner` as still `LIVE` after the
	DropValue.  cleanup_authoring querying `verdict_at` here
	returns `MUST_DROP` — the false-positive that triggered the
	patch-3 UAF.

	This test pins what would happen if the lowering ever
	regresses back to `LoadLocal + DropValue`.  The actual fix is
	at HIR→MIR; this test makes the regression cost visible at the
	stage2 layer."""
	func = _make_func("dv_naive", locals_=["inner"], types={"inner": _TY_OWNED})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="inner", value="t_init"))
	# Pre-fix lowering shape: LoadLocal + DropValue (no MoveOut)
	entry.instructions.append(M.LoadLocal(dest="t_load", local="inner"))
	entry.instructions.append(M.DropValue(value="t_load", ty=_TY_OWNED))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# The legacy MIR shape leaves `inner` LIVE — that's the BUG.
	assert ledger.state_pre(("entry", 3), "inner") is LiveState.LIVE
	# verdict_at returns MUST_DROP — the false-positive that
	# unblocked patch-3's UAF.  Pinned so a regression to the old
	# lowering shape would surface here.
	assert ledger.verdict_at(("entry", 3), "inner", needs_drop=True) is DropVerdict.MUST_DROP


# -- HIR→MIR lowering pin (driver-level) -----------------------------------


_LOWERING_SOURCE = """\
module main;

import std.core as core;

pub struct Box {
\tpub n: Int,
}

implement core.Destructible for Box {
\tpub fn destroy(var self: Box) nothrow -> Void {
\t\treturn;
\t}
}

fn make_box() nothrow -> Box {
\treturn Box(n = 1);
}

pub fn main() nothrow -> Int {
\tvar inner = make_box();
\tcore.drop_value<type Box>(inner);
\treturn 0;
}
"""


def test_lowering_drop_value_emits_moveout_for_non_copy_local(tmp_path: Path) -> None:
	"""Compile a fixture that calls `core.drop_value(non_copy_local)`
	for a function-level local and assert the LLVM IR contains
	EXACTLY ONE destroy call for the Box type.

	Pre-fix lowering (`LoadLocal + DropValue`, no MoveOut) leaves
	the ledger seeing `inner` as LIVE after the DropValue.  Patch-1
	cleanup_authoring (function-exit) then queries `verdict_at(inner)`,
	gets MUST_DROP, and emits a redundant scope-exit drop —
	resulting in TWO destroy calls in the IR.  This is a latent
	UAF (only manifested at runtime when the destructor touches
	heap; harmless for empty destructors).  See
	`work/ownership-ledger/patch-3-diagnosis.md`.

	Post-fix lowering (`MoveOut + DropValue`) transitions `inner`
	to MOVED_OUT in the ledger, so cleanup_authoring's verdict_at
	returns MUST_NOT_DROP and no redundant drop is authored —
	exactly ONE destroy call survives in the IR.

	Test assertion: count `Box::std.core.Destructible::destroy`
	call sites in `drift_main`.  Pre-fix: 2.  Post-fix: 1.
	"""
	root = Path(__file__).resolve().parents[3]
	src = tmp_path / "main.drift"
	src.write_text(_LOWERING_SOURCE)
	ir_path = tmp_path / "out.ll"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(root / "stdlib"),
		 str(src), "--entry", "main::main",
		 "--emit-ir", str(ir_path)],
		cwd=root, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:500]}"
	ir = ir_path.read_text()
	import re
	main_match = re.search(r"define i64 @drift_main\(\)[^{]*\{(.*?)^}", ir, re.DOTALL | re.MULTILINE)
	assert main_match, "could not locate drift_main body"
	body = main_match.group(1)
	destroy_calls = re.findall(
		r'call void @"Box::std.core.Destructible::destroy"', body
	)
	assert len(destroy_calls) == 1, (
		f"HIR→MIR lowering regression for `core.drop_value(non_copy_local)`: "
		f"drift_main must contain EXACTLY ONE Box destroy call (the "
		f"explicit `drop_value`).  Got {len(destroy_calls)}.  Pre-fix "
		f"lowering (`LoadLocal + DropValue`, no MoveOut) leaves the "
		f"ledger seeing `inner` as LIVE, so patch-1 cleanup_authoring "
		f"emits a redundant scope-exit drop.  See "
		f"`work/ownership-ledger/patch-3-diagnosis.md`."
	)
