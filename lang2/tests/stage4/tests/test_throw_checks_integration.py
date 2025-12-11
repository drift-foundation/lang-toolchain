# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Integration: stage2 → stage3 → stage4 throw checks.
Ensures the throw summary plus checker-inferred declared_can_throw map drive the invariants.
"""

from __future__ import annotations

import pytest

from lang2 import stage1 as H
from lang2.stage1 import normalize_hir
from lang2.stage2 import HIRToMIR, MirBuilder, mir_nodes as M
from lang2.stage3.throw_summary import ThrowSummaryBuilder
from lang2.stage4 import run_throw_checks
from lang2.checker import FnSignature
from lang2.test_support import declared_from_signatures


def _lower_fn(name: str, hir_block: H.HBlock) -> tuple[str, object]:
	builder = MirBuilder(name=name)
	HIRToMIR(builder).lower_block(hir_block)
	return name, builder.func


def test_can_throw_function_passes_checks():
	"""A declared can-throw function that throws should pass all invariants."""
	fn_name, mir_fn = _lower_fn(
		"f_can",
		H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="X", args=[]))]),
	)
	mir_funcs = {fn_name: mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={})
	declared_can_throw = declared_from_signatures(
		{fn_name: FnSignature(name=fn_name, return_type="FnResult<Int, Error>")}
	)

	infos = run_throw_checks(mir_funcs, summaries, declared_can_throw)
	assert fn_name in infos
	assert infos[fn_name].declared_can_throw is True


def test_non_can_throw_function_violates_invariant():
	"""A function that throws but is not declared can-throw should fail checks."""
	fn_name, mir_fn = _lower_fn(
		"f_non_can",
		H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="X", args=[]))]),
	)
	mir_funcs = {fn_name: mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={})
	declared_can_throw = declared_from_signatures(
		{fn_name: FnSignature(name=fn_name, return_type="Int")}
	)

	with pytest.raises(RuntimeError):
		run_throw_checks(mir_funcs, summaries, declared_can_throw)


def test_can_throw_try_catch_and_return_ok_shape():
	"""
	Can-throw function with try/catch and an explicit FnResult.Ok return passes all invariants.
	The try/catch itself does not terminate the function; we append a ConstructResultOk + Return.
	"""
	fn_name, mir_fn = _lower_fn(
		"f_ok",
		H.HBlock(
			statements=[
				H.HTry(
					body=H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="Evt", args=[]))]),
					catches=[H.HCatchArm(event_name="Evt", binder="e", block=H.HBlock(statements=[]))],
				)
			]
		),
	)

	# Manually append an Ok return in the try_cont block so return-shape checks pass.
	try_cont_blocks = [name for name in mir_fn.blocks if name.startswith("try_cont")]
	assert try_cont_blocks, "expected a try_cont block to append return"
	cont = mir_fn.blocks[try_cont_blocks[0]]
	ok_val = "ok_val"
	ok_res = "ok_res"
	cont.instructions.extend(
		[
			M.ConstInt(dest=ok_val, value=1),
			M.ConstructResultOk(dest=ok_res, value=ok_val),
		]
	)
	cont.terminator = M.Return(value=ok_res)

	mir_funcs = {fn_name: mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={"Evt": 1})
	declared_can_throw = declared_from_signatures(
		{fn_name: FnSignature(name=fn_name, return_type="FnResult<Int, Error>")}
	)

	infos = run_throw_checks(mir_funcs, summaries, declared_can_throw)
	assert fn_name in infos
	assert infos[fn_name].declared_can_throw is True


def test_can_throw_fnresult_forwarding_currently_rejected():
	"""
	Structural FnResult check still rejects forwarding/aliasing:
	a can-throw function that returns a value not produced by ConstructResultOk/Err
	should fail for now (type-aware checks will relax this later).
	"""
	# MIR: entry -> return param "p" (no ConstructResultOk/Err defines it).
	entry = M.BasicBlock(name="entry", instructions=[], terminator=M.Return(value="p"))
	mir_fn = M.MirFunc(
		name="f_forward",
		params=[],
		locals=["p"],
		blocks={"entry": entry},
		entry="entry",
	)
	mir_funcs = {"f_forward": mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={})
	declared_can_throw = declared_from_signatures(
		{"f_forward": FnSignature(name="f_forward", return_type=("FnResult", "Ok", "Err"))}
	)

	with pytest.raises(RuntimeError):
		run_throw_checks(mir_funcs, summaries, declared_can_throw)


def test_can_throw_without_throw_and_ok_return_passes():
	"""
	A can-throw function that never throws but returns a ConstructResultOk value
	should clear all invariants.
	"""
	entry = M.BasicBlock(
		name="entry",
		instructions=[
			M.ConstInt(dest="v_ok", value=42),
			M.ConstructResultOk(dest="r_ok", value="v_ok"),
		],
		terminator=M.Return(value="r_ok"),
	)
	mir_fn = M.MirFunc(
		name="f_ok_only",
		params=[],
		locals=[],
		blocks={"entry": entry},
		entry="entry",
	)
	mir_funcs = {"f_ok_only": mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={})
	declared_can_throw = declared_from_signatures(
		{"f_ok_only": FnSignature(name="f_ok_only", return_type="FnResult<Int, Error>")}
	)

	infos = run_throw_checks(mir_funcs, summaries, declared_can_throw)
	assert "f_ok_only" in infos
	assert infos["f_ok_only"].declared_can_throw is True


def test_fnresult_forwarding_aliasing_expected_fail():
	"""
	Structural FnResult check still fails for forwarding/aliasing.
	This documents the limitation; it should be relaxed once the type-aware check is available.
	"""
	# MIR: ConstructResultOk -> StoreLocal alias -> Return alias
	entry = M.BasicBlock(
		name="entry",
		instructions=[
			M.ConstInt(dest="v_ok", value=7),
			M.ConstructResultOk(dest="r0", value="v_ok"),
			M.StoreLocal(local="alias", value="r0"),
		],
		terminator=M.Return(value="alias"),
	)
	mir_fn = M.MirFunc(
		name="f_alias",
		params=[],
		locals=["alias"],
		blocks={"entry": entry},
		entry="entry",
	)
	mir_funcs = {"f_alias": mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={})
	declared_can_throw = declared_from_signatures(
		{"f_alias": FnSignature(name="f_alias", return_type="FnResult<Int, Error>")}
	)

	with pytest.raises(RuntimeError):
		run_throw_checks(mir_funcs, summaries, declared_can_throw)


def test_try_result_sugar_with_try_catch_clears_throw_checks():
	"""
	HTryResult sugar + try/catch, normalized, should pass throw checks for can-throw fns.
	"""
	# HIR with result-driven sugar inside a try/catch; catch uses an event name that
	# falls back to code 0 (unknown events) so it matches the throw from unwrap_err.
	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[H.HExprStmt(expr=H.HTryResult(expr=H.HVar(name="res")))]),
				catches=[
					H.HCatchArm(event_name="Evt", binder="e", block=H.HBlock(statements=[])),
				],
			),
		]
	)

	# Normalize sugar away before lowering.
	hir_norm = normalize_hir(hir)

	# Lower to MIR.
	fn_name, mir_fn = _lower_fn("f_sugar", hir_norm)

	# Append an Ok return in the try_cont block so return-shape checks pass.
	try_cont_blocks = [name for name in mir_fn.blocks if name.startswith("try_cont")]
	assert try_cont_blocks, "expected a try_cont block to append return"
	cont = mir_fn.blocks[try_cont_blocks[0]]
	ok_val = "ok_val"
	ok_res = "ok_res"
	cont.instructions.extend(
		[
			M.ConstInt(dest=ok_val, value=1),
			M.ConstructResultOk(dest=ok_res, value=ok_val),
		]
	)
	cont.terminator = M.Return(value=ok_res)

	mir_funcs = {fn_name: mir_fn}
	summaries = ThrowSummaryBuilder().build(mir_funcs, code_to_exc={})
	declared_can_throw = declared_from_signatures(
		{fn_name: FnSignature(name=fn_name, return_type="FnResult<Int, Error>")}
	)

	infos = run_throw_checks(mir_funcs, summaries, declared_can_throw)
	assert fn_name in infos
	assert infos[fn_name].declared_can_throw is True
