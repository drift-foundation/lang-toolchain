# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pending-lambda mutation-site transaction barrier (probe-rollback leak).

A deferred call probe's `CheckerStateTxn` snapshots the owned tables and
the PROBED SUBTREE only.  A probe-admitted candidate can nevertheless
semantically reach a pending stored lambda through its callee binding —
and resolving it used to mutate state the rollback could not restore: the
stored HLambda node elsewhere in the function body, the plain frame dicts
(`binding_types`, `binding_for_var`, the pending map), and the
checker-global `_lambda_fn_specs` registry (whose spec retains the LIVE
frame call-info map object).  Proven by the forcing audit below, which
failed on the pre-barrier tree with exactly those channels leaking.

The fix: `PendingLambdaOwner` — one explicit owner per check_function for
registration / exact-binding resolution / consumption / drain, whose
mutating operations raise the private `PendingLambdaBarrier`
(BaseException) while a probe transaction is active, BEFORE any external
mutation.  The probe machinery rolls back; nested probes re-raise; only
the OUTERMOST converts the signal to the ordinary silent expected-type
deferral (no diagnostic, no `_defer_probe_hard_error`, no
`rollbacks_exception` increment) counted as `deferrals_pending_barrier`
(nested plumbing separately as `pending_barrier_nested`).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import lang.driftc.checker.call_resolver as CR
import lang.driftc.type_checker as TC
from lang.driftc import driftc as driver
from lang.driftc import stage1 as H
from lang.driftc.checker.call_resolver import PendingLambdaBarrier
from lang.driftc.type_checker import PendingLambdaOwner

ROOT = Path(__file__).resolve().parents[3]

SRC_FORCING = """module main;

fn dflt2<T>(k: Int) nothrow -> Array<T> {
	val xs: Array<T> = [];
	return move xs;
}

struct Holder {
	n: Int
}

implement Holder {
	pub fn put(self: &Holder, xs: Array<Int>) nothrow -> Int {
		return xs.len() + self.n;
	}
}

pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val h = Holder(n = 0);
	return h.put(dflt2(f()));
}
"""

SRC_COMPLETE_CONTROL = """module main;

fn pass(k: Int) nothrow -> Int {
	return k;
}

struct Holder {
	n: Int
}

implement Holder {
	pub fn put2(self: &Holder, k: Int) nothrow -> Int {
		return k + self.n;
	}
}

pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val h = Holder(n = 0);
	return h.put2(pass(f())) - 7;
}
"""


# --- independent state auditor (same technique as the transaction tooth) ---

class _Audit:
	def __init__(self) -> None:
		self.rollbacks = 0
		self.commits = 0
		self.audited_rollbacks = 0
		self.mismatches: list[str] = []


def _find_check_function_frame():
	f = sys._getframe()
	while f is not None:
		if f.f_code.co_name == "check_function":
			return f
		f = f.f_back
	return None


_FRAME_SCALARS = (int, float, bool, str, bytes, type(None))


def _frame_state_dump(frame) -> str:
	parts = []
	for name, val in sorted(frame.f_locals.items()):
		if isinstance(val, (dict, list, set)) or isinstance(val, _FRAME_SCALARS):
			parts.append(f"{name}={TC._stable_state_repr(val)}")
	return "\n".join(parts)


def _diff_lines(before: str, after: str) -> str:
	b, a = before.splitlines(), after.splitlines()
	out = []
	for i in range(max(len(b), len(a))):
		lb = b[i] if i < len(b) else "<missing>"
		la = a[i] if i < len(a) else "<missing>"
		if lb != la:
			out.append(f"- {lb[:300]}\n+ {la[:300]}")
	return "\n".join(out[:20])


def _install_audit_txn(monkeypatch) -> _Audit:
	audit = _Audit()
	base = TC.CheckerStateTxn

	class _AuditTxn(base):
		def __init__(self, state, probe_expr) -> None:
			super().__init__(state, probe_expr)
			self._chk_frame = _find_check_function_frame()
			fn_id = self._chk_frame.f_locals.get("fn_id") if self._chk_frame else None
			self._audited = getattr(fn_id, "module", None) == "main" and getattr(fn_id, "name", None) == "main"
			if self._audited:
				self._begin_fp = state.state_fingerprint()
				self._begin_frame = _frame_state_dump(self._chk_frame)
				self._begin_body = TC._stable_state_repr(self._chk_frame.f_locals.get("body"))
				self._checker = self._chk_frame.f_locals.get("self")
				self._begin_specs = self._spec_snapshot()
				self._begin_pending = self._pending_snapshot()

		def _pending_snapshot(self):
			# The NEW owner is exactly the channel this audit must pin:
			# exact binding ids, lambda OBJECT identity, and structural
			# HLambda state (an owner-only loss/replacement must fail the
			# identity assertion even though the owner is not a plain
			# frame dict).
			owner = self._chk_frame.f_locals.get("pending_lambdas")
			backing = getattr(owner, "_by_binding", None)
			if not isinstance(backing, dict):
				return None
			return {
				bid: (id(lam), TC._stable_state_repr(lam))
				for bid, lam in backing.items()
			}

		def _spec_snapshot(self):
			# FULL LambdaFnSpec snapshot: EVERY dataclass field — registry
			# key, spec fn_id, origin_fn_id, lambda OBJECT identity plus
			# structural lambda state, param/return/throw, and the
			# call-info map's identity, alias bit, AND structure.  (The
			# independent FnCheckState fingerprint separately owns the live
			# map's content; this channel additionally pins that each spec
			# still references the same objects with the same structure.)
			specs = getattr(self._checker, "_lambda_fn_specs", None)
			if specs is None:
				return None
			frame_ci = self._chk_frame.f_locals.get("call_info_by_callsite_id")
			out = []
			for fid, s in list(specs.items()):
				ci = getattr(s, "call_info_by_callsite_id", None)
				lam = getattr(s, "lambda_expr", None)
				out.append((
					str(fid),
					str(getattr(s, "fn_id", None)),
					str(getattr(s, "origin_fn_id", None)),
					id(lam),
					TC._stable_state_repr(lam),
					TC._stable_state_repr((s.param_types, s.return_type, s.can_throw)),
					id(ci),
					ci is frame_ci,  # live-map alias identity
					TC._stable_state_repr(dict(ci) if isinstance(ci, dict) else ci),
				))
			return out

		def commit(self) -> None:
			audit.commits += 1
			super().commit()

		def rollback(self) -> None:
			super().rollback()
			audit.rollbacks += 1
			if self._audited:
				audit.audited_rollbacks += 1
				after_fp = self._state.state_fingerprint()
				if after_fp != self._begin_fp:
					audit.mismatches.append("OWNER mismatch:\n" + _diff_lines(self._begin_fp, after_fp))
				after_frame = _frame_state_dump(self._chk_frame)
				if after_frame != self._begin_frame:
					audit.mismatches.append("FRAME-LOCALS mismatch:\n" + _diff_lines(self._begin_frame, after_frame))
				after_body = TC._stable_state_repr(self._chk_frame.f_locals.get("body"))
				if after_body != self._begin_body:
					audit.mismatches.append("BODY-HIR mismatch:\n" + _diff_lines(self._begin_body, after_body))
				after_specs = self._spec_snapshot()
				if after_specs != self._begin_specs:
					audit.mismatches.append(f"_lambda_fn_specs mismatch: {self._begin_specs} -> {after_specs}")
				after_pending = self._pending_snapshot()
				if after_pending != self._begin_pending:
					audit.mismatches.append(
						f"pending-owner mismatch: {self._begin_pending and sorted(self._begin_pending)} -> {after_pending and sorted(after_pending)}"
					)

	monkeypatch.setattr(TC, "CheckerStateTxn", _AuditTxn)
	return audit


def _compile(tmp_path: Path, src: str, monkeypatch) -> int:
	f = tmp_path / "main.drift"
	f.write_text(src)
	monkeypatch.setattr(sys, "argv", [
		"driftc", "--dev",
		"--stdlib-root", str(ROOT / "stdlib"),
		str(f), "--entry", "main::main", "-o", str(tmp_path / "bin"),
	])
	try:
		rc = driver.main()
	except SystemExit as e:
		rc = int(e.code or 0)
	return int(rc or 0)


def test_probe_rollback_preserves_full_state_identity(tmp_path, monkeypatch, capsys) -> None:
	"""The forcing shape: a probed candidate resolves a pending lambda and
	then needs the expected type.  Pre-barrier this leaked the pending map,
	binding metadata, the external HLambda, and a `_lambda_fn_specs`
	publication across the rollback (this test FAILED); the barrier now
	fires before any mutation, so every audited rollback restores exact
	state, the candidate defers via `deferrals_pending_barrier`, and the
	retry emits the one real diagnostic."""
	audit = _install_audit_txn(monkeypatch)
	stats_before = dict(CR._DEFER_PROBE_STATS)
	rc = _compile(tmp_path, SRC_FORCING, monkeypatch)
	err = capsys.readouterr().err
	assert audit.audited_rollbacks >= 1, "forcing shape must roll back at least one audited probe"
	assert audit.mismatches == [], "\n\n".join(audit.mismatches)
	assert CR._DEFER_PROBE_STATS.get("deferrals_pending_barrier", 0) > stats_before.get("deferrals_pending_barrier", 0)
	assert CR._DEFER_PROBE_STATS.get("rollbacks_exception", 0) == stats_before.get("rollbacks_exception", 0)
	assert rc != 0, "v1 cannot infer T from the expected return; compile must fail"
	assert err.count("cannot infer type arguments for 'dflt2'") == 1, err[:2000]


def test_complete_control_preserves_retry_metadata_and_runs(tmp_path, monkeypatch, capsys) -> None:
	"""B5 preservation: the otherwise-complete nested call through a
	pending lambda keeps its accepted outcome — the barred probe defers and
	the expected-context retry resolves the SAME typed program.  Pins the
	finalized metadata, not only runtime: the stored lambda's function
	type, the indirect `f()` CallInfo, and the direct `pass(...)` CallInfo
	all carry the concrete Int contract after the retry."""
	from lang.driftc.core.types_core import TypeKind as CoreTypeKind
	from lang.driftc.stage1.call_info import CallTargetKind

	_audit = _install_audit_txn(monkeypatch)
	captured: dict[tuple[str, str], object] = {}
	checkers: dict[tuple[str, str], object] = {}
	orig_check = TC.TypeChecker.check_function

	def _capturing_check(self, fn_id, body, **kwargs):
		res = orig_check(self, fn_id, body, **kwargs)
		captured[(getattr(fn_id, "module", "?"), getattr(fn_id, "name", "?"))] = res
		checkers[(getattr(fn_id, "module", "?"), getattr(fn_id, "name", "?"))] = self
		return res

	monkeypatch.setattr(TC.TypeChecker, "check_function", _capturing_check)
	rc = _compile(tmp_path, SRC_COMPLETE_CONTROL, monkeypatch)
	_ = capsys.readouterr()
	assert rc == 0
	res = captured[("main", "main")]
	table = checkers[("main", "main")].type_table
	typed = res.typed_fn
	# The stored lambda's finalized function type: () -> Int, nothrow.
	f_bids = [bid for bid, name in typed.binding_names.items() if name == "f"]
	assert len(f_bids) == 1, typed.binding_names
	f_ty = typed.binding_types[f_bids[0]]
	f_def = table.get(f_ty)
	assert f_def.kind is CoreTypeKind.FUNCTION, f_def
	assert list(f_def.param_types) == [table.ensure_int()], f_def.param_types
	assert not f_def.can_throw()
	# CallInfo parity after the retry: f() is INDIRECT on exactly that
	# binding with ret Int / no params / nothrow; pass(...) is DIRECT with
	# (Int) -> Int; the outer method call carries ret Int.
	infos = list(typed.call_info_by_callsite_id.values())
	f_calls = [i for i in infos if i.target.kind is CallTargetKind.INDIRECT and i.target.callee_node_id == f_bids[0]]
	assert len(f_calls) == 1, [(i.target.kind, i.target.callee_node_id) for i in infos]
	assert f_calls[0].sig.user_ret_type == table.ensure_int()
	assert list(f_calls[0].sig.param_types) == []
	assert not f_calls[0].sig.can_throw
	pass_calls = [i for i in infos if i.target.kind is CallTargetKind.DIRECT and getattr(i.target.symbol, "name", None) == "pass"]
	assert len(pass_calls) == 1, [(i.target.kind, getattr(i.target.symbol, "name", None)) for i in infos]
	assert pass_calls[0].sig.user_ret_type == table.ensure_int()
	assert list(pass_calls[0].sig.param_types) == [table.ensure_int()]
	assert not pass_calls[0].sig.can_throw
	# The OUTER method call: DIRECT on Holder::put2 with the established
	# receiver-inclusive parameter layout (&Holder, Int) -> Int, nothrow.
	put_calls = [i for i in infos if i.target.kind is CallTargetKind.DIRECT and getattr(i.target.symbol, "name", None) == "Holder::put2"]
	assert len(put_calls) == 1, [(i.target.kind, getattr(i.target.symbol, "name", None)) for i in infos]
	_put_sig = put_calls[0].sig
	assert _put_sig.user_ret_type == table.ensure_int()
	assert not _put_sig.can_throw
	assert len(_put_sig.param_types) == 2, _put_sig.param_types
	assert table.get(_put_sig.param_types[0]).kind is CoreTypeKind.REF, _put_sig.param_types
	assert _put_sig.param_types[1] == table.ensure_int()
	# Runtime companion.
	run = subprocess.run([str(tmp_path / "bin")], capture_output=True, text=True, timeout=120)
	assert run.returncode == 0, run.stderr


def test_barrier_consume_authority_production_branch() -> None:
	"""The production catch path delegates to
	`CR._consume_pending_barrier`; exercise that exact authority over REAL
	FnCheckState/CheckerStateTxn objects:
	- nested probe: inner rollback + False (re-raise) + nested counter;
	- outermost: rollback + True (silent deferral) + outer counter;
	- unsupported transaction object: FAIL CLOSED — rollback, False, NO
	  counter movement;
	- no diagnostic/hard-error/exception-counter side effects and exact
	  owner-fingerprint identity after both rollbacks."""
	checker = TC.TypeChecker()
	state = TC.FnCheckState(checker)
	base_fp = state.state_fingerprint()
	stats_before = dict(CR._DEFER_PROBE_STATS)
	dummy = H.HLiteralInt(value=0)
	outer = TC.CheckerStateTxn(state, dummy)
	inner = TC.CheckerStateTxn(state, dummy)
	# Inner probe: nested → re-raise decision.
	assert CR._consume_pending_barrier(inner) is False
	assert CR._DEFER_PROBE_STATS["pending_barrier_nested"] == stats_before["pending_barrier_nested"] + 1
	assert state._txn_depth == 1
	# Outermost: converted.
	assert CR._consume_pending_barrier(outer) is True
	assert CR._DEFER_PROBE_STATS["deferrals_pending_barrier"] == stats_before["deferrals_pending_barrier"] + 1
	assert state._txn_depth == 0
	assert state.state_fingerprint() == base_fp, "rollbacks must restore exact owner state"
	assert CR._DEFER_PROBE_STATS["rollbacks_exception"] == stats_before["rollbacks_exception"]

	class _LegacyTxn:
		def __init__(self) -> None:
			self.rolled_back = False

		def rollback(self) -> None:
			self.rolled_back = True

	legacy = _LegacyTxn()
	mid = dict(CR._DEFER_PROBE_STATS)
	assert CR._consume_pending_barrier(legacy) is False, "unsupported txn must fail closed (propagate)"
	assert legacy.rolled_back
	assert CR._DEFER_PROBE_STATS == mid, "fail-closed path must move no counter"


SRC_SHADOWED_LINKING = None  # linking pin is synthetic-HIR below (source shadowing of `f` would re-parse to distinct names anyway)


def test_unlinked_shadowed_hcall_links_exact_inner_binding() -> None:
	"""Exact-linking pin through the ACTUAL checker path: the call's HVar
	arrives UNLINKED (binding_id=None); two same-named pending lambdas are
	in scope (outer returns String, inner shadows with Int).  The live
	lexical scope must select the INNER binding id before pending
	resolution — a name-history authority would see the outer first."""
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc.core.types_core import TypeTable
	from lang.driftc.method_registry import CallableRegistry
	from lang.driftc.stage1.call_info import CallTargetKind

	outer_lam = H.HLambda(params=[], body_expr=H.HLiteralString(value="s"))
	inner_lam = H.HLambda(params=[], body_expr=H.HLiteralInt(value=2))
	inner_let = H.HLet(name="f", value=inner_lam)
	call = H.HCall(fn=H.HVar(name="f"), args=[], kwargs=[])  # UNLINKED
	body = H.HBlock(statements=[
		H.HLet(name="f", value=outer_lam),
		H.HIf(
			cond=H.HLiteralBool(value=True),
			then_block=H.HBlock(statements=[
				inner_let,
				H.HExprStmt(expr=call),
			]),
			else_block=None,
		),
	])
	table = TypeTable()
	result = TC.TypeChecker(table).check_function(
		FunctionId(module="main", name="main", ordinal=0),
		body,
		callable_registry=CallableRegistry(),
		visible_modules=(0,),
	)
	assert result.diagnostics == []
	assert isinstance(call.fn.binding_id, int)
	assert call.fn.binding_id == inner_let.binding_id, (call.fn.binding_id, inner_let.binding_id)
	info = result.typed_fn.call_info_by_callsite_id[call.callsite_id]
	assert info.target.kind is CallTargetKind.INDIRECT
	assert info.target.callee_node_id == inner_let.binding_id
	assert info.sig.user_ret_type == table.ensure_int(), "outer String lambda must NOT be selected"
	assert list(info.sig.param_types) == []
	assert not info.sig.can_throw
	assert result.typed_fn.expr_types[call.node_id] == table.ensure_int()


def test_hinvoke_consumer_resolves_pending_through_owner() -> None:
	"""The explicit HInvoke pending consumer (synthetic-HIR contract —
	source stored calls parse as HCall) resolves through the same owner
	authority and records the concrete CallInfo."""
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc.core.types_core import TypeTable
	from lang.driftc.method_registry import CallableRegistry
	from lang.driftc.stage1.call_info import CallTargetKind

	lam = H.HLambda(params=[], body_expr=H.HLiteralInt(value=9))
	let = H.HLet(name="f", value=lam, binding_id=91)
	invoke = H.HInvoke(callee=H.HVar(name="f", binding_id=91), args=[], kwargs=[])
	body = H.HBlock(statements=[let, H.HExprStmt(expr=invoke)])
	table = TypeTable()
	result = TC.TypeChecker(table).check_function(
		FunctionId(module="main", name="main", ordinal=0),
		body,
		callable_registry=CallableRegistry(),
		visible_modules=(0,),
	)
	assert result.diagnostics == []
	info = result.typed_fn.call_info_by_callsite_id[invoke.callsite_id]
	assert info.target.kind is CallTargetKind.INDIRECT
	# HInvoke's indirect target carries the CALLEE NODE id by contract
	# (pinned the same way in test_lambda_callinfo_inference_boundary);
	# binding-level identity is pinned separately: the callee binding
	# named `f` resolved from pending to the concrete () -> Int function
	# type through the owner.
	assert info.target.callee_node_id == invoke.callee.node_id
	assert result.typed_fn.binding_names.get(invoke.callee.binding_id) == "f"
	callee_ty_def = table.get(result.typed_fn.binding_types[invoke.callee.binding_id])
	from lang.driftc.core.types_core import TypeKind as CoreTypeKind
	assert callee_ty_def.kind is CoreTypeKind.FUNCTION
	assert list(callee_ty_def.param_types) == [table.ensure_int()]
	assert info.sig.user_ret_type == table.ensure_int()
	assert list(info.sig.param_types) == []
	assert not info.sig.can_throw
	assert result.typed_fn.expr_types[invoke.node_id] == table.ensure_int()


# --- owner unit contract -------------------------------------------------

def _lam() -> H.HLambda:
	return H.HLambda(params=[], body_expr=H.HLiteralInt(value=1))


def test_owner_barrier_blocks_every_mutation_under_active_txn() -> None:
	active = {"on": False}
	owner = PendingLambdaOwner(txn_active=lambda: active["on"])
	owner.register(7, _lam())
	active["on"] = True
	with pytest.raises(PendingLambdaBarrier):
		owner.register(8, _lam())
	with pytest.raises(PendingLambdaBarrier):
		owner.begin_resolution(7)
	with pytest.raises(PendingLambdaBarrier):
		owner.retire(7)
	with pytest.raises(PendingLambdaBarrier):
		owner.drain()
	# Nothing mutated by refused operations; read-only peek stays open.
	assert len(owner) == 1
	assert owner.peek(7) is not None
	active["on"] = False
	assert owner.begin_resolution(7) is not None
	owner.retire(7)
	assert len(owner) == 0


def test_owner_exact_binding_identity_no_name_authority() -> None:
	# Two distinct binding ids (e.g. shadowed same-source-name bindings):
	# operations on one never touch the other; absent ids return None
	# WITHOUT raising even under an active transaction (no pending state
	# is reached, so no barrier is needed).
	active = {"on": False}
	owner = PendingLambdaOwner(txn_active=lambda: active["on"])
	inner, outer = _lam(), _lam()
	owner.register(1, outer)
	owner.register(2, inner)
	assert owner.begin_resolution(2) is inner
	owner.retire(2)
	assert owner.peek(1) is outer and owner.peek(2) is None
	active["on"] = True
	assert owner.begin_resolution(99) is None
	assert owner.begin_resolution(None) is None
	owner.retire(99)  # absent: no barrier, no effect
	assert owner.peek(1) is outer


def test_nested_transactions_gate_the_owner_until_outermost_closes() -> None:
	"""Nested-propagation pin over REAL FnCheckState/CheckerStateTxn
	objects: the owner stays barred while ANY probe transaction is open
	(inner rollback alone must NOT unbar it — the machinery re-raises to
	the outer probe on exactly this predicate), unbars only when the
	outermost closes, and refused operations mutate nothing."""
	checker = TC.TypeChecker()
	state = TC.FnCheckState(checker)
	owner = PendingLambdaOwner(txn_active=lambda: state._txn_depth > 0)
	owner.register(5, _lam())
	dummy = H.HLiteralInt(value=0)
	outer = TC.CheckerStateTxn(state, dummy)
	inner = TC.CheckerStateTxn(state, dummy)
	with pytest.raises(PendingLambdaBarrier):
		owner.begin_resolution(5)
	inner.rollback()
	assert state._txn_depth > 0, "outer probe still open"
	with pytest.raises(PendingLambdaBarrier):
		owner.begin_resolution(5)  # nested re-raise predicate: still barred
	outer.rollback()
	assert state._txn_depth == 0
	assert owner.begin_resolution(5) is not None, "unbarred after outermost close"
	assert len(owner) == 1, "refused operations mutated nothing"
