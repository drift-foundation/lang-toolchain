# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Ownership-invariant pin for `captures(share x)` lowering.

The lattice-level contract: `share x` produces a SECOND owner without
consuming the original.  Concretely, the MIR shape

    StoreLocal(app, t_arc)                 # app: LIVE  (initial owner — stake #1)
    AddrOfLocal(t_ref, app, is_mut=False)  # app: LIVE  (borrow, not consume)
    Call(t_share, share_fn, [t_ref])       # app: LIVE  (Call doesn't transition args)
    ConstructStruct(env_v, [t_share])
    StoreLocal(env, env_v)                 # env: LIVE  (now owns stake #2)
    ...
    Return

must satisfy at the function-exit point:

  - `app` is LIVE → its scope-exit cleanup drops stake #1.
  - `env` is LIVE → its scope-exit cleanup drops stake #2 (via the
    env's Arc field).

This is a regression carrier on the principle "share creates a new
owner without consuming the old one."  If a future patch accidentally
emits a `MoveOut(_, app, _)` on the SHARE path (or makes some other
transition that marks `app` as MOVED_OUT), this test fires.
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import (
	DropVerdict,
	LiveState,
	build_ledger,
	classify,
)


# Sentinel TypeIds — the ledger does not consult the type table for
# transition rules at this level (only for `verdict_at`'s `needs_drop`
# axis, which the caller supplies).
_TY_ARC_APP = 401      # Arc<App> — non-Copy, refcounted, has_drop=True
_TY_ENV_STRUCT = 402   # __lambda_env_… — has_drop because it owns Arc<App>


def _drop_policy_stub(ty: int) -> object:
	"""Mimic `compute_drop_policy(...).needs_drop` for the synthetic
	type ids used here.  Both Arc<App> and the env struct require drop."""
	return type("_DP", (), {"needs_drop": ty in (_TY_ARC_APP, _TY_ENV_STRUCT)})()


def _build_share_capture_fn() -> M.MirFunc:
	"""Build a MIR function modeling the SHARE-capture lowering shape
	emitted by `_lower_share_capture` + immediate-lambda env build.
	"""
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	share_fn_id = FunctionId(
		module="std.concurrent",
		name="Arc<T>::std.core.shareable.Share::share",
		ordinal=0,
	)
	hidden_lambda_id = FunctionId(module="main", name="__lambda_main_0_0", ordinal=0)

	entry = M.BasicBlock(name="entry")
	# t_arc materializes via a `arc(App)` call (modeled as Call dest).
	entry.instructions.append(M.Call(
		dest="t_arc",
		fn_id=FunctionId(module="std.concurrent", name="arc", ordinal=0),
		args=[],
		can_throw=False,
	))
	# The original owner.  app: LIVE after this StoreLocal.
	entry.instructions.append(M.StoreLocal(local="app", value="t_arc"))
	# AddrOfLocal is a borrow.  Must NOT transition `app` to MOVED_OUT.
	entry.instructions.append(M.AddrOfLocal(dest="t_ref", local="app", is_mut=False))
	# Share::share consumes only the ref, returning a new owner.
	# Call does not transition arg locals in `_apply`.
	entry.instructions.append(M.Call(
		dest="t_share",
		fn_id=share_fn_id,
		args=["t_ref"],
		can_throw=False,
	))
	# Move the new owner into the env struct.
	entry.instructions.append(M.ConstructStruct(
		dest="t_env",
		struct_ty=_TY_ENV_STRUCT,
		args=["t_share"],
	))
	# env: LIVE (owns stake #2 via its Arc field).
	entry.instructions.append(M.StoreLocal(local="env", value="t_env"))
	# Hidden lambda invocation passes &env.
	entry.instructions.append(M.AddrOfLocal(dest="t_env_ref", local="env", is_mut=False))
	entry.instructions.append(M.Call(
		dest="t_result",
		fn_id=hidden_lambda_id,
		args=["t_env_ref"],
		can_throw=False,
	))
	entry.instructions.append(M.StoreLocal(local="direct", value="t_result"))
	entry.terminator = M.Return(value=None)

	return M.MirFunc(
		name="main",
		params=[],
		locals=["app", "env", "direct"],
		fn_id=fn_id,
		blocks={"entry": entry},
		entry="entry",
		local_types={
			"app": _TY_ARC_APP,
			"env": _TY_ENV_STRUCT,
			"direct": 0,
			"t_arc": _TY_ARC_APP,
			"t_ref": 0,
			"t_share": _TY_ARC_APP,
			"t_env": _TY_ENV_STRUCT,
			"t_env_ref": 0,
			"t_result": 0,
		},
	)


def _last_instr_index(func: M.MirFunc, block_name: str) -> int:
	return len(func.blocks[block_name].instructions) - 1


def test_share_capture_does_not_consume_original_local() -> None:
	"""After the SHARE lowering's AddrOfLocal + Call, `app` must remain
	LIVE — it still owns stake #1 and is dropped at scope exit.  If a
	future patch accidentally emits a MoveOut for `app` on the SHARE
	path, this assertion fires."""
	func = _build_share_capture_fn()
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Post-Call (Share::share) state for `app`.
	post_share_call = ledger.post_instr[("entry", 3)]
	assert post_share_call["app"] is LiveState.LIVE, (
		"share-capture must not consume the original local — "
		f"app went to {post_share_call['app']}"
	)
	# Post-Return (function-exit) cleanup verdict.
	app_verdict = classify(post_share_call["app"], needs_drop=True)
	assert app_verdict is DropVerdict.MUST_DROP, (
		f"original `app` must drop at scope exit (stake #1) — got {app_verdict}"
	)


def test_share_capture_env_owns_returned_share_stake() -> None:
	"""The env struct (carrying the share-result) must be LIVE after
	construction so its scope-exit cleanup drops stake #2 via the env's
	Arc field."""
	func = _build_share_capture_fn()
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	# Post-StoreLocal(env, ...) — env is LIVE.
	post_env_store = ledger.post_instr[("entry", 5)]
	assert post_env_store["env"] is LiveState.LIVE
	# At function exit `env` must drop (carries the captured Arc owner).
	last_ins = _last_instr_index(func, "entry")
	exit_state = ledger.post_instr[("entry", last_ins)]
	env_verdict = classify(exit_state["env"], needs_drop=True)
	assert env_verdict is DropVerdict.MUST_DROP, (
		f"env (carrying captured share-result) must drop at scope exit (stake #2) — "
		f"got {env_verdict}"
	)


def test_share_capture_both_owners_drop_at_function_exit() -> None:
	"""Combined invariant: BOTH the original `app` and the env (with
	captured share-result) must be MUST_DROP at the function-exit
	point.  This is the load-bearing fact the user-facing semantic
	rests on:

	  - `share a` does NOT consume `a`  → original a's stake is released later.
	  - `share a` returns a NEW owner   → captured copy's stake is released later.

	If either side flips to MUST_NOT_DROP, refcount accounting goes
	wrong (leak or double-decrement)."""
	func = _build_share_capture_fn()
	ledger = build_ledger(func, drop_policy=_drop_policy_stub)
	last_ins = _last_instr_index(func, "entry")
	exit_state = ledger.post_instr[("entry", last_ins)]
	assert classify(exit_state["app"], needs_drop=True) is DropVerdict.MUST_DROP
	assert classify(exit_state["env"], needs_drop=True) is DropVerdict.MUST_DROP
