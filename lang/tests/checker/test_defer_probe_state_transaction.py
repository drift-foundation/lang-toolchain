# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Invariant tooth for the deferred-call probe's EXPLICIT-OWNER state
transaction (`FnCheckState` / `CheckerStateTxn` in type_checker.py;
consumed by the `defer_infer_diag` block in `checker/call_resolver.py`).

The driver pins (`lang/tests/driver/test_nested_call_arg_defer_infer_
regression.py`) prove the end-to-end behaviors; THIS file proves the
transaction and its scope:

  * after every NEEDS_EXPECTED rollback the owner state is EXACTLY what
    it was at probe begin: the owner's own `state_fingerprint()` (a
    full-VALUE dump of the owned tables + allocator cells — independent
    of the undo-log mechanism, so a buggy or missing undo entry shows
    as a diff), PLUS two auditors independent of the owner's own
    enumeration:
      - a raw frame-introspection dump of `check_function`'s container
        and scalar locals (catches state that lives outside the owner
        yet is mutated by a probe), and
      - a structural dump of the ENTIRE function body HIR (catches
        descendant rewrites anywhere in the tree);
  * diagnostics: the probed failure's message appears EXACTLY ONCE in
    the final output (emitted by the enclosing expected-type retry;
    the probe's own copy was rolled back);
  * UNEXPECTED exceptions inside the probe ROLL BACK (with identity)
    and RE-RAISE — normal ICE containment, a probe must never convert
    a compiler defect into a silent retry;
  * the fail-closed shape gate: subtrees containing binding/scope-
    writing nodes (lambdas, match expressions, ...) — or any node kind
    not in the explicit allowlist — are never probed.

Probe shape: `h.put(dflt())` — a struct-METHOD argument (method-call
pre-typing defers every nested HCall argument), where `fn dflt<T>()`
is resolvable neither from its (zero) arguments nor, in v1, from the
expected return (pre-fix HEAD produces the identical single
diagnostic — verified 2026-07-23).  This makes the NEEDS_EXPECTED
rollback + expected-type retry deterministic.

Auditing wraps CheckerStateTxn via monkeypatch and fingerprints only
functions of the user module ("main") to keep stdlib compile cost sane.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import lang.driftc.type_checker as TC
import lang.driftc.checker.call_resolver as CR
from lang.driftc import stage1 as H
from lang.driftc import driftc as driver

ROOT = Path(__file__).resolve().parents[3]

SRC_NEEDS_EXPECTED = """module main;

import std.core as core;

fn dflt<T>() nothrow -> Array<T> {
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
	val h = Holder(n = 0);
	return h.put(dflt());
}
"""

SRC_HARD_ERROR = """module main;

import std.core as core;
import std.mem as mem;

fn make_ptr(b: &Byte) nothrow -> mem.Ptr<Byte> {
	return unsafe { mem.ptr_from_ref<type Byte>(b) };
}

pub fn main() nothrow -> Int {
	val cb: core.Callback1<mem.Ptr<Byte>, Int> = core.callback1(|p: mem.Ptr<Byte>| => { 7 });
	return cb.call(make_ptr(42));
}
"""


class _Audit:
	def __init__(self) -> None:
		self.rollbacks = 0
		self.commits = 0
		self.audited_rollbacks = 0
		self.mismatches: list[str] = []


def _diff_lines(before: str, after: str) -> str:
	b, a = before.splitlines(), after.splitlines()
	out = []
	for i in range(max(len(b), len(a))):
		lb = b[i] if i < len(b) else "<missing>"
		la = a[i] if i < len(a) else "<missing>"
		if lb != la:
			out.append(f"- {lb[:400]}\n+ {la[:400]}")
	return "\n".join(out[:20])


def _find_check_function_frame():
	"""INDEPENDENT auditor: locate the live check_function frame by name.
	Deliberately NOT how production defines the transaction scope (that is
	the explicit FnCheckState owner) — this cross-checks the owner's
	enumeration against the raw frame."""
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


def _install_audit_txn(monkeypatch) -> _Audit:
	audit = _Audit()
	base = TC.CheckerStateTxn

	class _AuditTxn(base):
		def __init__(self, state, probe_expr) -> None:
			super().__init__(state, probe_expr)
			self._chk_frame = _find_check_function_frame()
			fn_id = self._chk_frame.f_locals.get("fn_id") if self._chk_frame else None
			self._audited = getattr(fn_id, "module", None) == "main"
			if self._audited:
				self._begin_fp = state.state_fingerprint()
				self._begin_frame = _frame_state_dump(self._chk_frame)
				self._begin_body = TC._stable_state_repr(self._chk_frame.f_locals.get("body"))

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
					audit.mismatches.append(
						"owner-state mismatch after rollback:\n" + _diff_lines(self._begin_fp, after_fp)
					)
				after_frame = _frame_state_dump(self._chk_frame)
				if after_frame != self._begin_frame:
					audit.mismatches.append(
						"INDEPENDENT frame-locals mismatch after rollback:\n"
						+ _diff_lines(self._begin_frame, after_frame)
					)
				after_body = TC._stable_state_repr(self._chk_frame.f_locals.get("body"))
				if after_body != self._begin_body:
					audit.mismatches.append(
						"HIR body mismatch after rollback:\n" + _diff_lines(self._begin_body, after_body)
					)

	monkeypatch.setattr(TC, "CheckerStateTxn", _AuditTxn)
	return audit


def _compile(tmp_path: Path, src: str, monkeypatch) -> int:
	f = tmp_path / "main.drift"
	f.write_text(src)
	out_bin = tmp_path / "bin"
	monkeypatch.setattr(sys, "argv", [
		"driftc", "--dev", "--allow-unsafe",
		"--stdlib-root", str(ROOT / "stdlib"),
		str(f), "--entry", "main::main", "-o", str(out_bin),
	])
	try:
		rc = driver.main()
	except SystemExit as e:
		rc = int(e.code or 0)
	return int(rc or 0)


def test_needs_expected_rollback_restores_exact_state(tmp_path, monkeypatch, capsys) -> None:
	"""NEEDS_EXPECTED probe of `dflt()`: exact owner-state + independent
	frame-locals + whole-body-HIR identity after rollback, and the retry
	emits the single real diagnostic (probe copy rolled back — no
	duplicate, no swallow)."""
	audit = _install_audit_txn(monkeypatch)
	stats_before = dict(CR._DEFER_PROBE_STATS)
	rc = _compile(tmp_path, SRC_NEEDS_EXPECTED, monkeypatch)
	err = capsys.readouterr().err
	assert audit.audited_rollbacks >= 1, "the deferred generic call must have forced a rollback"
	assert not audit.mismatches, "\n\n".join(audit.mismatches)
	assert CR._DEFER_PROBE_STATS["rollbacks_needs_expected"] > stats_before["rollbacks_needs_expected"]
	assert rc != 0, "v1 cannot infer T from the expected return; compile must fail"
	assert err.count("cannot infer type arguments for 'dflt'") == 1, (
		f"the retry must emit the diagnostic exactly once:\n{err[:2000]}"
	)


def test_fingerprint_scope_covers_allocators_and_side_tables(tmp_path, monkeypatch) -> None:
	"""The owner fingerprint must contain the allocator cells and the core
	side tables — guarding against tables silently leaving the owner."""
	seen: dict[str, str] = {}
	base = TC.CheckerStateTxn

	class _ScopeProbe(base):
		def __init__(self, state, probe_expr) -> None:
			super().__init__(state, probe_expr)
			if not seen:
				frame = _find_check_function_frame()
				fn_id = frame.f_locals.get("fn_id") if frame else None
				if getattr(fn_id, "module", None) == "main":
					seen["fp"] = state.state_fingerprint()

	monkeypatch.setattr(TC, "CheckerStateTxn", _ScopeProbe)
	_compile(tmp_path, SRC_NEEDS_EXPECTED, monkeypatch)
	fp = seen.get("fp")
	assert fp is not None, "no probe fired in module main"
	for required in (
		"next_node_id=", "next_callsite_id=", "next_binding_id=",
		"expr_types=", "diagnostics=", "iface_coercions=",
		"call_info_by_callsite_id=", "instantiations_by_callsite_id=",
	):
		assert required in fp, f"fingerprint lost required state channel {required!r}"


def test_exception_during_probe_rolls_back_and_reraises(tmp_path, monkeypatch, capsys) -> None:
	"""ICE containment: a RuntimeError raised INSIDE the probe's recursive
	resolution must roll back (exact identity, recorded by the audit) and
	then RE-RAISE — never converted into a silent retry."""
	audit = _install_audit_txn(monkeypatch)
	orig = CR.resolve_call_expr
	state = {"armed": True, "raised": 0}

	def _boom(ctx, expr, expected_type=None, **kw):
		if (
			state["armed"]
			and expected_type is None
			and not getattr(expr, "defer_infer_diag", True)
			and getattr(getattr(expr, "fn", None), "name", None) == "dflt"
		):
			state["armed"] = False
			state["raised"] += 1
			raise RuntimeError("forced probe failure (invariant tooth)")
		return orig(ctx, expr, expected_type, **kw)

	monkeypatch.setattr(CR, "resolve_call_expr", _boom)
	with pytest.raises(RuntimeError, match="forced probe failure"):
		_compile(tmp_path, SRC_NEEDS_EXPECTED, monkeypatch)
	assert state["raised"] == 1, "the forced exception must have fired inside exactly one probe"
	assert audit.audited_rollbacks >= 1, "rollback must run before the re-raise"
	assert not audit.mismatches, "\n\n".join(audit.mismatches)


def test_hard_error_commits_and_never_rolls_back_its_diagnostic(tmp_path, monkeypatch, capsys) -> None:
	"""HARD_ERROR probe outcome commits the live resolution: the real
	diagnostic survives exactly once and the compile fails."""
	audit = _install_audit_txn(monkeypatch)
	stats_before = dict(CR._DEFER_PROBE_STATS)
	rc = _compile(tmp_path, SRC_HARD_ERROR, monkeypatch)
	err = capsys.readouterr().err
	assert rc != 0, "invalid program must fail to compile"
	assert err.count("no matching overload for function 'make_ptr'") == 1, err[:2000]
	assert CR._DEFER_PROBE_STATS["commits_hard_error"] > stats_before["commits_hard_error"]
	assert not audit.mismatches, "\n\n".join(audit.mismatches)


def test_shape_gate_is_fail_closed() -> None:
	"""The probe gate admits only the explicit allowlist: lambdas / match
	expressions (binding- and scope-writing shapes) — and, fail-closed,
	ANY node kind outside the allowlist — must never be probed."""
	span_kwargs: dict = {}
	var = H.HVar(name="x")
	lit = H.HLiteralInt(value=1)
	simple = H.HCall(fn=H.HVar(name="f"), args=[var, lit], **span_kwargs)
	assert CR._defer_probe_shape_safe(simple) is True

	lam = H.HLambda(params=[], ret_type=None, body_expr=None, body_block=None)
	with_lambda = H.HCall(fn=H.HVar(name="f"), args=[lam])
	assert CR._defer_probe_shape_safe(with_lambda) is False

	# Fail-closed for any non-allowlisted HIR node kind (here: HMatchExpr,
	# a scope/binding-writing shape).
	match_node = H.HMatchExpr(scrutinee=var, arms=[])
	with_match = H.HCall(fn=H.HVar(name="f"), args=[match_node])
	assert CR._defer_probe_shape_safe(with_match) is False


def test_txnlist_slice_delete_rollback_exact() -> None:
	"""_TxnList completeness: production deletes diagnostic SLICES
	(`del diagnostics[start:]` in call_resolver and two checker sites).
	A rollback across any interleaving of appends, slice deletes, index
	deletes, item assignment, insert/pop/remove/clear must restore the
	exact original contents."""

	class _StubChecker:
		_next_binding_id = 1

	state = TC.FnCheckState(_StubChecker())
	lst = state.diagnostics
	lst.append("a")
	lst.append("b")
	lst.append("c")

	txn = state.begin_txn(H.HVar(name="x"))
	lst.append("d")
	del lst[1:]              # the production slice-delete pattern
	lst.append("e")
	lst.insert(0, "f")
	lst[0] = "g"
	lst.pop()
	lst.extend(["h", "i"])
	lst.remove("h")
	del lst[0]
	assert list(lst) != ["a", "b", "c"]
	txn.rollback()
	assert list(lst) == ["a", "b", "c"], f"slice-delete rollback corrupted the list: {list(lst)}"

	# Nested-transaction discipline: inner commit keeps entries so an
	# outer rollback still reverts the inner mutations.
	outer = state.begin_txn(H.HVar(name="y"))
	inner = state.begin_txn(H.HVar(name="z"))
	lst.append("inner")
	del lst[0:1]
	inner.commit()
	outer.rollback()
	assert list(lst) == ["a", "b", "c"]


def test_shape_gate_rejects_closures_module_metadata() -> None:
	"""The gate must use the CANONICAL HIR recognition predicate: a
	recognized HIR dataclass from stage1.closures (capture metadata) is
	not on the allowlist and must be rejected — not skipped by a
	module-name check."""
	from lang.driftc.stage1 import closures as HC

	cap = HC.HCapture(kind=HC.HCaptureKind.COPY, key=HC.HCaptureKey(root_local=1))
	with_capture = H.HCall(fn=H.HVar(name="f"), args=[cap])
	assert CR._defer_probe_shape_safe(with_capture) is False


def test_result_objects_detach_transaction_wrappers(tmp_path, monkeypatch) -> None:
	"""TypedFn / TypeCheckResult must NOT retain the _TxnDict/_TxnList
	transaction wrappers (each retains the FnCheckState owner, which
	retains every table and the checker): all owned-table outputs are
	detached into plain dict/list — exact types pinned here."""
	typed_fn_fields: list[tuple[str, type]] = []
	result_fields: list[tuple[str, type]] = []
	orig_typed_fn = TC.TypedFn
	orig_result = TC.TypeCheckResult

	def spy_typed_fn(**kw):
		for name in ("expr_types", "call_resolutions", "call_info_by_callsite_id",
				"instantiations_by_callsite_id", "instantiations_by_node_id",
				"iface_coercions", "borrowed_iface_coercions", "ptr_to_ref_coercions"):
			typed_fn_fields.append((name, type(kw[name])))
		return orig_typed_fn(**kw)

	def spy_result(**kw):
		result_fields.append(("diagnostics", type(kw["diagnostics"])))
		return orig_result(**kw)

	monkeypatch.setattr(TC, "TypedFn", spy_typed_fn)
	monkeypatch.setattr(TC, "TypeCheckResult", spy_result)
	rc = _compile(tmp_path, SRC_NEEDS_EXPECTED, monkeypatch)
	assert rc != 0  # the dflt() program still fails as pinned elsewhere
	assert typed_fn_fields and result_fields, "no results were constructed"
	bad = [(n, t) for n, t in typed_fn_fields if t is not dict]
	assert not bad, f"TypedFn retained non-plain-dict tables: {bad}"
	bad = [(n, t) for n, t in result_fields if t is not list]
	assert not bad, f"TypeCheckResult retained non-plain-list diagnostics: {bad}"


def test_mixed_expected_and_hard_errors_are_hard(tmp_path, monkeypatch, capsys) -> None:
	"""The STATED three-outcome contract: NEEDS_EXPECTED only when EVERY
	new error carries an expected-dependent code.  A MIXED probe failure
	(expected-dependent + hard in one probe) is HARD: the transaction
	commits, ALL its diagnostics survive exactly once, and the marker
	prevents any retry duplication."""
	orig = CR.resolve_call_expr
	state = {"armed": True}

	def _mixed(ctx, expr, expected_type=None, **kw):
		is_probe_target = (
			state["armed"]
			and expected_type is None
			and not getattr(expr, "defer_infer_diag", True)
			and getattr(getattr(expr, "fn", None), "name", None) == "dflt"
		)
		result = orig(ctx, expr, expected_type, **kw)
		if is_probe_target:
			state["armed"] = False
			# The real resolution just emitted E-INFER-UNDERDETERMINED;
			# add a HARD companion (no needs-expected code) to make the
			# probe's new-error set MIXED.
			ctx.diagnostics.append(ctx.tc_diag(
				message="synthetic hard companion (mixed-outcome tooth)",
				severity="error",
				span=None,
			))
		return result

	monkeypatch.setattr(CR, "resolve_call_expr", _mixed)
	stats_before = dict(CR._DEFER_PROBE_STATS)
	rc = _compile(tmp_path, SRC_NEEDS_EXPECTED, monkeypatch)
	err = capsys.readouterr().err
	assert rc != 0
	assert CR._DEFER_PROBE_STATS["commits_hard_error"] > stats_before["commits_hard_error"], (
		"mixed failure must take the HARD_ERROR outcome (commit), not roll back"
	)
	assert err.count("synthetic hard companion") == 1, err[:2000]
	assert err.count("cannot infer type arguments for 'dflt'") == 1, err[:2000]


def test_shape_gate_rejects_non_dataclass_hir_node() -> None:
	"""Guardrail: the gate's ONLY filter is the canonical predicate — a
	NON-dataclass H.HNode subclass is canonically recognized and must be
	rejected (an is_dataclass prefilter would silently accept it)."""
	from dataclasses import is_dataclass

	class _SyntheticExpr(H.HExpr):
		def __init__(self) -> None:
			pass

	synth = _SyntheticExpr()
	# Honest preconditions: not a dataclass, yet canonically recognized.
	assert not is_dataclass(synth)
	from lang.driftc.stage1.node_ids import default_should_descend
	assert default_should_descend(synth)

	with_synth = H.HCall(fn=H.HVar(name="f"), args=[synth])
	assert CR._defer_probe_shape_safe(with_synth) is False


def test_txn_containers_cover_inherited_mutators() -> None:
	"""Guardrail: the advertised complete mutator coverage includes the
	previously inherited operators — list `*=`, dict `|=`, and
	dict.popitem() must all roll back exactly."""

	class _StubChecker:
		_next_binding_id = 1

	state = TC.FnCheckState(_StubChecker())

	lst = state.diagnostics
	lst.append("a")
	lst.append("b")
	txn = state.begin_txn(H.HVar(name="x"))
	lst *= 3
	assert list(lst) == ["a", "b"] * 3
	txn.rollback()
	assert list(lst) == ["a", "b"], f"__imul__ rollback corrupted the list: {list(lst)}"

	d = state.expr_types
	d[1] = "one"
	d[2] = "two"
	txn = state.begin_txn(H.HVar(name="y"))
	d |= {2: "TWO", 3: "three"}
	k, v = d.popitem()
	assert dict(d) != {1: "one", 2: "two"}
	txn.rollback()
	assert dict(d) == {1: "one", 2: "two"}, f"__ior__/popitem rollback corrupted the dict: {dict(d)}"
