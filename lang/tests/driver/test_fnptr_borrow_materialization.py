# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Borrowing a named function (`&seven`) must materialize the fnptr constant.

The checker's fnptr rewrite (`_apply_fnptr_consts`) replaces a name
expression used as a function value with `HFnPtrConst` — INCLUDING when
the name is the base of the canonical borrow place (`HBorrow.subject`
= `HPlaceExpr(base=HFnPtrConst, projections=[])`).  `_lower_addr_of_place`
assumed every place base has local storage and read `expr.base.name`:

	AttributeError: 'HFnPtrConst' object has no attribute 'name'
	  at stage2/hir_to_mir.py _lower_addr_of_place (pre-fix ~12643)

— an ICE on valid v1 source (`val r = &seven;`).  A function constant has
no storage to address, so the fix materializes it into an owned temp via
`_materialize_owned_temp_for_borrow` (the HBorrow-rvalue-fallback helper)
and returns the temp's address — the same semantics as `val f = seven; &f`.
`&mut` of a function constant stays rejected: the checker diagnoses it as
a non-addressable borrow operand, and lowering fail-closes if one ever
slips through.

Structural tests drive `_lower_addr_of_place` directly with the
checker-rewritten shape (the HPlaceExpr/HFnPtrConst transition), and every
accepted surface shape carries a compile/run pin.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId, FunctionRefId, FunctionRefKind
from lang.driftc.core.types_core import TypeTable
from lang.driftc.parser import stdlib_root
from lang.driftc.stage1.call_info import CallSig
from lang.driftc.stage2 import (
	AddrOfLocal,
	FnPtrConst,
	HIRToMIR,
	StoreLocal,
	make_builder,
)

ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Structural: the HPlaceExpr(base=HFnPtrConst) transition at the crash site.
# ---------------------------------------------------------------------------

def _fnptr_place() -> tuple[TypeTable, H.HPlaceExpr]:
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_id = FunctionId(module="repro", name="seven", ordinal=0)
	call_sig = CallSig(param_types=(), user_ret_type=int_ty, can_throw=False)
	fn_ref = FunctionRefId(fn_id=fn_id, kind=FunctionRefKind.IMPL, has_wrapper=False)
	fnptr = H.HFnPtrConst(fn_ref=fn_ref, call_sig=call_sig)
	place = H.HPlaceExpr(base=fnptr, projections=[])
	return table, place


def test_place_with_fnptr_const_base_materializes_borrow_temp() -> None:
	# Pre-fix: AttributeError ('HFnPtrConst' object has no attribute
	# 'name') — the place walker read `expr.base.name` unconditionally.
	table, place = _fnptr_place()
	builder = make_builder(FunctionId(module="repro", name="test_fn", ordinal=0))
	lower = HIRToMIR(builder, type_table=table, call_info_by_callsite_id={})

	addr, fn_ty = lower._lower_addr_of_place(place, is_mut=False)

	# The constant is materialized into an owned temp and its ADDRESS is
	# the borrow result: FnPtrConst → StoreLocal(__borrow_tmp…) →
	# AddrOfLocal(shared) over the SAME local.
	instrs = list(builder.func.blocks[builder.func.entry].instructions)
	fnptr_instrs = [i for i in instrs if isinstance(i, FnPtrConst)]
	assert fnptr_instrs, instrs
	stores = [i for i in instrs if isinstance(i, StoreLocal)]
	assert stores and stores[0].local.startswith("__borrow_tmp"), instrs
	assert stores[0].value == fnptr_instrs[0].dest
	addrs = [i for i in instrs if isinstance(i, AddrOfLocal)]
	assert addrs and addrs[0].local == stores[0].local
	assert addrs[0].is_mut is False
	assert addrs[0].dest == addr

	# The returned place type is the function type derived from the
	# constant's call signature, not Unknown.
	expected_fn_ty = table.ensure_function([], table.ensure_int(), can_throw=False)
	assert fn_ty == expected_fn_ty


def test_mut_borrow_of_fnptr_const_base_fails_closed() -> None:
	# The checker rejects `&mut seven` (non-addressable operand); if a
	# mutable borrow of a constant ever reaches lowering it is a checker
	# bug and lowering must fail closed, not mint mutable storage.
	table, place = _fnptr_place()
	builder = make_builder(FunctionId(module="repro", name="test_fn", ordinal=0))
	lower = HIRToMIR(builder, type_table=table, call_info_by_callsite_id={})
	with pytest.raises(AssertionError, match="mutable borrow of a function constant"):
		lower._lower_addr_of_place(place, is_mut=True)


# ---------------------------------------------------------------------------
# E2E pins: every accepted surface shape compiles AND runs.
# ---------------------------------------------------------------------------

BORROW_NAMED_FN = """
module repro;
fn seven() nothrow -> Int { return 7; }
pub fn main() nothrow -> Int {
	val r = &seven;
	return (*r)() - 7;
}
"""

BORROW_FINALIZED_BINDING = """
module repro;
fn seven() nothrow -> Int { return 7; }
pub fn main() nothrow -> Int {
	val f = seven;
	val r = &f;
	return f() - 7;
}
"""

MUT_BORROW_NAMED_FN = """
module repro;
fn seven() nothrow -> Int { return 7; }
pub fn main() nothrow -> Int {
	val r = &mut seven;
	return 0;
}
"""


def _compile(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess, Path]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "repro"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc", str(src),
		"--entry", "repro::main", "--target-word-bits", "64", "-o", str(out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	build = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	return build, out


def _build_run(tmp_path: Path, source: str) -> None:
	build, out = _compile(tmp_path, source)
	err = build.stdout + build.stderr
	assert build.returncode == 0, err
	assert "Traceback" not in err, err
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, (run.returncode, run.stderr)


def test_borrow_of_named_function_compiles_and_runs(tmp_path: Path) -> None:
	# The minimal ICE shape (`&seven`), strengthened to CONSUME the
	# borrow: calling through `*r` proves the materialized borrowed value
	# is a usable function value, not just an address that lowers.
	_build_run(tmp_path, BORROW_NAMED_FN)


def test_borrow_of_finalized_fn_binding_compiles_and_runs(tmp_path: Path) -> None:
	# Finalize-and-accept: `val f = seven` finalizes the binding to the
	# fnptr constant; `&f` then borrows the LOCAL (ordinary place path).
	# Pins that binding finalization and the borrow path compose.
	_build_run(tmp_path, BORROW_FINALIZED_BINDING)


def test_mut_borrow_of_named_function_is_rejected(tmp_path: Path) -> None:
	# `&mut seven` never reaches lowering: the checker diagnoses the
	# non-addressable operand (a function constant is not mutable storage).
	build, _out = _compile(tmp_path, MUT_BORROW_NAMED_FN)
	err = build.stdout + build.stderr
	assert build.returncode != 0, err
	assert "borrow operand must be an addressable place" in err, err
	assert "Traceback" not in err, err
