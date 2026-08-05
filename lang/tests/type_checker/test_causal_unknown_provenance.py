# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Causal Unknown provenance: exact-cause suppression, tripwire preserved.

Pre-fix the checker suppressed Unknown-cascade diagnostics with a GLOBAL
`any(error)` scan: any earlier, unrelated diagnostic silenced the
E-COPY-UNKNOWN / call-target tripwires for EVERY Unknown in the function,
so independent un-diagnosed Unknowns could pass silently.  The fix owns
cause provenance in FnCheckState (`unknown_cause_by_binding` /
`unknown_cause_by_node`, probe-transaction covered): a consumer suppresses
its cascade diagnostic ONLY when the specific binding/expression it reads
carries a recorded cause; everything else fails toward the tripwire.

Propagation is explicit and shape-proven: caused-binding HVar reads,
`move` of a caused subject, reachability-aware ternary joins (ALL
reachable Unknown arms must be caused), and causally suppressed call
results.  A compound join's decision is authoritative: the HLet
diagnostic watermark must never re-mark a mixed-cause compound
(review-2026-08-05T03-42-22Z P1-2).
"""
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import main as driftc_main
from lang.driftc.type_checker import TypeChecker

_UNKNOWN_BINDING = 41
_UNKNOWN_NAME = "independent_unknown"


# ---------------------------------------------------------------------------
# Tripwire pins (unit harness): a preseeded Unknown binding with NO cause is
# an un-diagnosed hole — its consumers MUST diagnose even after an earlier,
# unrelated error.  These two were the original red probes: the global-scan
# suppression silenced both.
# ---------------------------------------------------------------------------

def _check(statements: list[H.HStmt]):
	table = TypeTable()
	unknown = table.ensure_unknown()
	result = TypeChecker(table).check_function(
		FunctionId(module="probe", name="f", ordinal=0),
		H.HBlock(statements=statements),
		preseed_binding_types={_UNKNOWN_BINDING: unknown},
		preseed_binding_names={_UNKNOWN_BINDING: _UNKNOWN_NAME},
		preseed_scope_env={_UNKNOWN_NAME: unknown},
		preseed_scope_bindings={_UNKNOWN_NAME: _UNKNOWN_BINDING},
	)
	return result.diagnostics


def _unrelated_invalid_copy() -> H.HExprStmt:
	# Stable, unrelated first diagnostic (explicit copy of a non-place);
	# neither reads nor writes _UNKNOWN_BINDING.
	return H.HExprStmt(expr=H.HCopy(subject=H.HLiteralInt(1)))


def test_unrelated_error_does_not_suppress_independent_copy_unknown() -> None:
	diagnostics = _check(
		[
			_unrelated_invalid_copy(),
			H.HLet(
				name="sink",
				value=H.HVar(_UNKNOWN_NAME, binding_id=_UNKNOWN_BINDING),
				declared_type_expr=None,
			),
		]
	)
	assert any(d.code == "E-COPY-UNKNOWN" for d in diagnostics), [d.message for d in diagnostics]


def test_unrelated_error_does_not_suppress_independent_unknown_callee() -> None:
	diagnostics = _check(
		[
			_unrelated_invalid_copy(),
			H.HExprStmt(
				expr=H.HCall(
					fn=H.HVar(_UNKNOWN_NAME, binding_id=_UNKNOWN_BINDING),
					args=[],
				)
			),
		]
	)
	assert sum(d.message == "call target is not a function value" for d in diagnostics) == 1, [d.message for d in diagnostics]


# ---------------------------------------------------------------------------
# Causal suppression pins (full driver diagnostics): reads of a binding whose
# Unknown IS causally explained produce exactly the one primary.
# ---------------------------------------------------------------------------

def _compile(tmp_path: Path, capsys, source: str) -> list[str]:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	main_path.parent.mkdir(parents=True, exist_ok=True)
	main_path.write_text(source)
	driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return [d.get("message", "") for d in payload.get("diagnostics", [])]


def _errors(msgs: list[str]) -> list[str]:
	return msgs


def test_unknown_name_binding_read_and_call_single_primary(tmp_path: Path, capsys) -> None:
	# The exact-binding cause: the unknown-name primary explains every
	# later read/call of `bad` — no E-COPY-UNKNOWN, no call-target noise.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val bad = missing_name;
	bad();
	return 0;
}
""",
	)
	assert sum("unknown name 'missing_name'" in m for m in msgs) == 1, msgs
	assert len(msgs) == 1, msgs


def test_alias_hop_propagates_cause_single_primary(tmp_path: Path, capsys) -> None:
	# `val b2 = bad` re-binds a caused Unknown: the cause must hop with it.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val bad = missing_name;
	val b2 = bad;
	b2();
	return 0;
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "unknown name 'missing_name'" in msgs[0], msgs


def test_move_of_caused_subject_single_primary(tmp_path: Path, capsys) -> None:
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val bad = missing_name;
	val m = move bad;
	m();
	return 0;
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "unknown name 'missing_name'" in msgs[0], msgs


def test_literal_ternary_all_reachable_caused_single_primary(tmp_path: Path, capsys) -> None:
	# Literal-true cond folds reachability: only the caused arm is
	# reachable, so the join marks the result caused (single primary).
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val bad = missing_name;
	val t = true ? bad : bad;
	t();
	return 0;
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "unknown name 'missing_name'" in msgs[0], msgs


def test_mixed_arm_nonliteral_ternary_keeps_downstream_tripwire(tmp_path: Path, capsys) -> None:
	# P1-2 pin (review-2026-08-05T03-42-22Z): non-literal cond, arm A is a
	# diagnosed unknown-name (caused), arm B is a SILENT uncaused Unknown
	# (field projection on a caused-Unknown subject emits no diagnostic
	# and carries no cause).  The join must leave the result UNCAUSED —
	# and the HLet watermark must NOT override the join with arm A's
	# primary — so the downstream call still trips the tripwire.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
fn cond() nothrow -> Bool { return true; }
pub fn main() nothrow -> Int {
	val bad = missing_name;
	val t = cond() ? missing_name2 : bad.field;
	t();
	return 0;
}
""",
	)
	assert sum("unknown name" in m for m in msgs) == 2, msgs
	assert any("call target is not a function value" in m or "E-COPY-UNKNOWN" in m or "cannot copy" in m for m in msgs), msgs


def test_invoke_parity_single_primary(tmp_path: Path, capsys) -> None:
	# Parenthesized callee routes through HInvoke; same causal suppression.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val bad = missing_name;
	(bad)();
	return 0;
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "unknown name 'missing_name'" in msgs[0], msgs


def test_concrete_recovery_clears_cause(tmp_path: Path, capsys) -> None:
	# A binding re-bound to a concrete type must not carry a stale cause:
	# the concrete write CLEARS, and later real errors surface normally.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val bad = missing_name;
	val bad2 = 3;
	return bad2 - 3;
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "unknown name 'missing_name'" in msgs[0], msgs


# ---------------------------------------------------------------------------
# Round-2 pins (review-2026-08-05T04-27-45Z): suppression must never swallow
# INDEPENDENT primaries — call/method arguments are still typed, and the
# match/try joins mirror the ternary contract on both sides.
# ---------------------------------------------------------------------------

def test_caused_hcall_still_checks_independent_argument(tmp_path: Path, capsys) -> None:
	# Suppressing the callee-derived call-target cascade must not skip
	# ordinary argument traversal: the argument's own unknown-name is an
	# independent primary.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val bad = missing_receiver;
	bad(missing_argument);
	return 0;
}
""",
	)
	assert any("unknown name 'missing_receiver'" in m for m in msgs), msgs
	assert any("unknown name 'missing_argument'" in m for m in msgs), msgs
	assert len(msgs) == 2, msgs


def test_caused_method_receiver_still_checks_independent_argument(tmp_path: Path, capsys) -> None:
	# Same contract at the method consumer: receiver-derived resolution
	# noise is suppressed AFTER the arguments were typed.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val bad = missing_receiver;
	bad.no_such_method(missing_argument);
	return 0;
}
""",
	)
	assert any("unknown name 'missing_receiver'" in m for m in msgs), msgs
	assert any("unknown name 'missing_argument'" in m for m in msgs), msgs
	assert len(msgs) == 2, msgs


def test_all_caused_match_arms_suppress_downstream(tmp_path: Path, capsys) -> None:
	# Every value-producing arm's Unknown is caused → the match result is
	# caused and the downstream call adds nothing.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val a = missing_a;
	val b = missing_b;
	val joined = match true { true => { a }, false => { b } };
	joined();
	return 0;
}
""",
	)
	assert sum("unknown name" in m for m in msgs) == 2, msgs
	assert len(msgs) == 2, msgs


def test_mixed_match_arm_keeps_downstream_tripwire(tmp_path: Path, capsys) -> None:
	# One caused arm, one SILENT uncaused Unknown arm (field projection on
	# a caused subject carries no cause): the join must NOT mark, so the
	# downstream call trips the tripwire.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val a = missing_a;
	val joined = match true { true => { a }, false => { a.field } };
	joined();
	return 0;
}
""",
	)
	assert any("call target is not a function value" in m or "cannot copy" in m for m in msgs), msgs


def test_all_caused_try_results_suppress_downstream(tmp_path: Path, capsys) -> None:
	# The attempt AND the value-producing catch arm are both caused → the
	# try result is caused and the downstream call adds nothing.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val attempted = missing_attempt;
	val recovered = missing_catch;
	val joined = try attempted catch { recovered };
	joined();
	return 0;
}
""",
	)
	assert sum("unknown name" in m for m in msgs) == 2, msgs
	assert len(msgs) == 2, msgs


def test_mixed_try_contributor_keeps_downstream_tripwire(tmp_path: Path, capsys) -> None:
	# Attempt caused, catch-arm value a SILENT uncaused Unknown: any
	# uncaused contributor keeps the tripwire.
	msgs = _compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val attempted = missing_attempt;
	val joined = try attempted catch { attempted.field };
	joined();
	return 0;
}
""",
	)
	assert any("call target is not a function value" in m or "cannot copy" in m for m in msgs), msgs
