# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""FORCING probe: pending-lambda resolution inside a NEEDS_EXPECTED rollback.

Work-only.  Reuses the independent audit technique of
lang/tests/checker/test_defer_probe_state_transaction.py (read-only import
of its idea, no shared file edited): an audit CheckerStateTxn subclass
snapshots the owner fingerprint, the raw check_function frame locals
(plain dict/list/set/scalar), and the whole-body HIR before each audited
probe, and diffs them after rollback.

Candidate shape: `h.put(dflt2(f()))` — the method-argument probe wraps the
candidate `dflt2(f())`; typing it FIRST resolves the pending stored lambda
`f` (mutating binding_types / pending_lambda_by_binding / the external
HLambda), THEN fails to infer `T` for `dflt2` without the expected type,
forcing THAT candidate's transaction to roll back.

The probe records, per audited rollback, whether the frame/owner/body
state matches the pre-probe snapshot.  Mismatches here CONFIRM the child
finding (leak manifest); a clean audit REJECTS/REVISES it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import lang.driftc.type_checker as TC
import lang.driftc.checker.call_resolver as CR
from lang.driftc import driftc as driver

ROOT = Path(__file__).resolve().parents[4]

SRC_FORCING = """module main;

import std.core as core;

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


class _Audit:
	def __init__(self) -> None:
		self.rollbacks = 0
		self.commits = 0
		self.audited_rollbacks = 0
		self.mismatches: list[str] = []
		self.pending_deltas: list[str] = []


def _diff_lines(before: str, after: str) -> str:
	b, a = before.splitlines(), after.splitlines()
	out = []
	for i in range(max(len(b), len(a))):
		lb = b[i] if i < len(b) else "<missing>"
		la = a[i] if i < len(a) else "<missing>"
		if lb != la:
			out.append(f"- {lb[:300]}\n+ {la[:300]}")
	return "\n".join(out[:24])


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
				_pend = self._chk_frame.f_locals.get("pending_lambda_by_binding")
				self._begin_pending = sorted(_pend) if isinstance(_pend, dict) else None
				_bt = self._chk_frame.f_locals.get("binding_types")
				self._begin_bt = TC._stable_state_repr(_bt) if isinstance(_bt, dict) else None
				# Checker-global publication channel (review item 1): the
				# TypeChecker instance's lambda-spec registry, snapshotted
				# by (fn_id, id(call_info map)) so a leaked spec AND a
				# leaked live-map alias are both visible.
				self._checker = self._chk_frame.f_locals.get("self")
				self._begin_specs = self._spec_snapshot()

		def _spec_snapshot(self):
			specs = getattr(self._checker, "_lambda_fn_specs", None)
			if specs is None:
				return None
			out = []
			for s in list(specs.values()):
				out.append((str(getattr(s, "fn_id", "?")), id(getattr(s, "call_info_by_callsite_id", None))))
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
				_pend = self._chk_frame.f_locals.get("pending_lambda_by_binding")
				after_pending = sorted(_pend) if isinstance(_pend, dict) else None
				if after_pending != self._begin_pending:
					audit.pending_deltas.append(
						f"pending_lambda_by_binding: {self._begin_pending} -> {after_pending}"
					)
				after_specs = self._spec_snapshot()
				if after_specs != self._begin_specs:
					_new = [s for s in (after_specs or []) if s not in (self._begin_specs or [])]
					_frame_ci = self._chk_frame.f_locals.get("call_info_by_callsite_id")
					_alias = [fid for fid, mid in _new if _frame_ci is not None and mid == id(_frame_ci)]
					audit.pending_deltas.append(
						f"_lambda_fn_specs LEAKED across rollback: new={_new}; "
						f"aliases-live-frame-call-info-map={_alias}"
					)

	monkeypatch.setattr(TC, "CheckerStateTxn", _AuditTxn)
	return audit


def test_forcing_rollback_after_pending_resolution(tmp_path, monkeypatch, capsys) -> None:
	audit = _install_audit_txn(monkeypatch)
	stats_before = dict(CR._DEFER_PROBE_STATS)
	f = tmp_path / "main.drift"
	f.write_text(SRC_FORCING)
	monkeypatch.setattr(sys, "argv", [
		"driftc", "--dev",
		"--stdlib-root", str(ROOT / "stdlib"),
		str(f), "--entry", "main::main", "-o", str(tmp_path / "bin"),
	])
	try:
		rc = driver.main()
	except SystemExit as e:
		rc = int(e.code or 0)
	err = capsys.readouterr().err
	delta = {k: CR._DEFER_PROBE_STATS[k] - stats_before.get(k, 0) for k in CR._DEFER_PROBE_STATS if CR._DEFER_PROBE_STATS[k] != stats_before.get(k, 0)}
	print("\n=== FORCING PROBE RESULT ===")
	print(f"compile rc={rc}")
	print(f"stats delta: {delta}")
	print(f"audited main::main probes: commits={audit.commits} rollbacks={audit.rollbacks} audited_rollbacks={audit.audited_rollbacks}")
	print(f"pending-map deltas across audited rollbacks: {audit.pending_deltas}")
	for m in audit.mismatches:
		print(f"--- {m[:1500]}")
	print(f"diagnostic tail: {err[-500:] if err else '<none>'}")
	# Characterization requirement: the enclosing probe must actually have
	# rolled back for the evidence to be meaningful.
	assert audit.audited_rollbacks >= 1, "forcing shape failed to produce an audited rollback — revise the shape"


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


def test_complete_control_pending_resolution_in_committed_probe(tmp_path, monkeypatch, capsys) -> None:
	# POSITIVE control (review item 2): an otherwise-complete nested call
	# through a pending lambda — the method-argument probe of `pass(f())`
	# resolves the pending lambda and COMPLETE-commits; the program
	# compiles AND runs.  Any barrier design must preserve this outcome
	# (via the expected-context retry if the probe is barred).
	import subprocess
	audit = _install_audit_txn(monkeypatch)
	stats_before = dict(CR._DEFER_PROBE_STATS)
	f = tmp_path / "main.drift"
	f.write_text(SRC_COMPLETE_CONTROL)
	monkeypatch.setattr(sys, "argv", [
		"driftc", "--dev",
		"--stdlib-root", str(ROOT / "stdlib"),
		str(f), "--entry", "main::main", "-o", str(tmp_path / "bin"),
	])
	try:
		rc = driver.main()
	except SystemExit as e:
		rc = int(e.code or 0)
	_ = capsys.readouterr()
	delta = {k: CR._DEFER_PROBE_STATS[k] - stats_before.get(k, 0) for k in CR._DEFER_PROBE_STATS if CR._DEFER_PROBE_STATS[k] != stats_before.get(k, 0)}
	print("\n=== COMPLETE CONTROL RESULT ===")
	print(f"compile rc={rc}")
	print(f"stats delta: {delta}")
	print(f"audited commits={audit.commits} rollbacks={audit.rollbacks}")
	assert rc == 0
	run = subprocess.run([str(tmp_path / "bin")], capture_output=True, text=True, timeout=60)
	print(f"run exit={run.returncode}")
	assert run.returncode == 0, run.stderr
