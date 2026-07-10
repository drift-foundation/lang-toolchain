# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-arch-0 pins: the string_arc differential stake reporter
(Scope B §11.2, `DRIFT_STRING_ARC_AUDIT=1`).

Three contracts:
  1. OFF by default — `string_arc_audit_enabled()` is False in a clean
     env and `insert_string_arc` constructs no audit object (the
     behavior-neutrality of the disabled path; the IR-level proof lives
     in the driver-level neutrality check run during the slice, seeded
     off-vs-on identical modulo build timestamp).
  2. ON — events are collected at tagged emission sites, the closed
     site_class enumeration holds (no UNTAGGED), and the C1-C4
     classification runs against L_pre/L_post with per-fn JSONL going
     to DRIFT_STRING_ARC_AUDIT_FILE.
  3. The classifier itself: C2 counts retains as invisible stakes; C3
     flags MoveOut of a non-Owned local. The old C4 allowlist is
     RETIRED (2026-07-11, post release-elision acceptance): the
     `c4_allowlisted` constant is retained only for historical
     aggregate compatibility, and any NEW occurrence of either retired
     face (a release at a MOVED_OUT return boundary, or a site-3 return
     retain) classifies as UNCLASSIFIED — the hard corpus gate — with a
     `*_retired_c4` triage kind (pinned below).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2 import ownership_ledger_reporter as R
from lang.driftc.stage2.ownership_ledger import build_ledger
from lang.driftc.stage2.string_arc import insert_string_arc


def _make_func(name: str, *, params: list[str], locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="test", name=name, ordinal=0)
	return M.MirFunc(
		name=f"test::{name}",
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _attach_ledger(func: M.MirFunc) -> None:
	setattr(func, "_ownership_ledger", build_ledger(func, drop_policy=lambda _t: None))


def _string_shuffle_func(type_table: TypeTable) -> M.MirFunc:
	"""fn f() { var x: String; var y: String; x = "a"; y = <same temp>;
	val m = move x; return; }

	Shapes exercised: store_value_retain (the const temp is stored
	twice, so the second store must retain), overwrite_release +
	scope_exit_release (string locals), moveout_expansion.
	"""
	string_ty = type_table.ensure_string()
	func = _make_func(
		"f",
		params=[],
		locals_=["x", "y", "m"],
		types={"x": string_ty, "y": string_ty, "m": string_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%c", value="a"),
		M.StoreLocal(local="x", value="%c"),
		M.StoreLocal(local="y", value="%c"),
		M.MoveOut(dest="%m0", local="x", ty=string_ty),
		M.StoreLocal(local="m", value="%m0"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	return func


def test_audit_disabled_by_default(monkeypatch) -> None:
	monkeypatch.delenv("DRIFT_STRING_ARC_AUDIT", raising=False)
	assert not R.string_arc_audit_enabled()
	# And the pass runs exactly as before with no audit machinery.
	tt = TypeTable()
	func = _string_shuffle_func(tt)
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})


def test_audit_env_gate(monkeypatch) -> None:
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	assert R.string_arc_audit_enabled()


def test_audit_collects_tags_and_classifies(monkeypatch, tmp_path: Path) -> None:
	out = tmp_path / "audit.jsonl"
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_VERBOSE", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(out))
	tt = TypeTable()
	func = _string_shuffle_func(tt)
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	assert out.exists(), "audit file must receive per-fn JSONL"
	recs = [json.loads(line.split("] ", 1)[1]) for line in out.read_text().splitlines()]
	fn_recs = [r for r in recs if r.get("record") == "fn" and r.get("fn") == "test::f"]
	assert fn_recs, recs
	agg = fn_recs[0]
	# Closed enumeration: every event tagged, nothing UNTAGGED.
	assert "untagged" not in agg, agg
	assert not any(k.startswith("site_class:UNTAGGED") for k in agg), agg
	# The shapes we constructed appear under their tags.
	assert agg.get("site_class:moveout_expansion", 0) >= 1, agg
	assert agg.get("site_class:store_value_retain", 0) >= 1, agg
	assert agg.get("site_class:scope_exit_release", 0) >= 1, agg
	# C2: the store retain is an invisible stake (the ledger has no
	# event model for StringRetain).
	assert agg.get("c2_invisible_stake", 0) >= 1, agg
	# C3: the MoveOut source was Owned (stored just above) — counted as
	# owned, not flagged.
	assert agg.get("c3_moveout_owned", 0) >= 1, agg
	assert agg.get("c3_moveout_not_owned", 0) == 0, agg
	# No UNCLASSIFIED divergences in this fixture.
	assert agg.get("unclassified", 0) == 0, agg


def test_classifier_c3_flags_moveout_of_uninit(monkeypatch, tmp_path: Path) -> None:
	"""MoveOut of a local the ledger sees as UNINIT at that point →
	c3_moveout_not_owned."""
	out = tmp_path / "audit.jsonl"
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_VERBOSE", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(out))
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("g", params=[], locals_=["x", "m"], types={"x": string_ty, "m": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.MoveOut(dest="%m0", local="x", ty=string_ty),
		M.StoreLocal(local="m", value="%m0"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	recs = [json.loads(line.split("] ", 1)[1]) for line in out.read_text().splitlines()]
	agg = [r for r in recs if r.get("record") == "fn" and r.get("fn") == "test::g"][0]
	assert agg.get("c3_moveout_not_owned", 0) >= 1, agg


def test_untagged_note_is_a_finding() -> None:
	audit = R.StringArcAudit("test::u")
	audit.note(R.STAKE_RETAIN, "%v", "not_a_real_site", pre_point=("b", 0), post_point=("b", 0))
	assert audit.untagged == 1
	assert audit.events[0].site_class.startswith("UNTAGGED:")


def test_missing_l_post_is_hard_counted(monkeypatch, tmp_path: Path) -> None:
	"""Review pin (B-arch-0 acceptance): a fn whose L_post snapshot could
	not be built must surface `post_ledger_build_failed` in its aggregate
	AND force-emit the per-fn record past the volume guard — the corpus
	gate fails on any nonzero count; a silent skip would let
	UNCLASSIFIED=0 be reported without the post-snapshot half."""
	out = tmp_path / "audit.jsonl"
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	monkeypatch.delenv("DRIFT_STRING_ARC_AUDIT_VERBOSE", raising=False)
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(out))
	tt = TypeTable()
	func = _string_shuffle_func(tt)
	_attach_ledger(func)
	l_pre = getattr(func, "_ownership_ledger")
	audit = R.StringArcAudit("test::h")
	agg = audit.finalize(l_pre=l_pre, l_post=None, needs_drop=lambda _l: True)
	assert agg.get(R.DIV_POST_LEDGER_BUILD_FAILED) == 1, agg
	recs = [json.loads(line.split("] ", 1)[1]) for line in out.read_text().splitlines()]
	fn_recs = [r for r in recs if r.get("record") == "fn" and r.get("fn") == "test::h"]
	assert fn_recs, "hard failure must force per-fn emission despite empty details"
	assert fn_recs[0].get("post_ledger_build_failed") == 1, fn_recs[0]


def test_retired_c4_moved_out_release_fails_loudly(monkeypatch, tmp_path: Path) -> None:
	"""C4 retirement pin (2026-07-11): a scope-exit release at a
	MOVED_OUT return boundary — impossible with release-elision live —
	must classify as UNCLASSIFIED (the hard corpus gate), not the
	retired counted-never-failed c4_allowlisted bucket."""
	out = tmp_path / "audit.jsonl"
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_VERBOSE", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(out))
	tt = TypeTable()
	string_ty = tt.ensure_string()
	# x is stored then MOVED OUT — its boundary state is MOVED_OUT.
	func = _make_func("r", params=[], locals_=["x", "m"], types={"x": string_ty, "m": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%c", value="a"),
		M.StoreLocal(local="x", value="%c"),
		M.MoveOut(dest="%m0", local="x", ty=string_ty),
		M.StoreLocal(local="m", value="%m0"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_attach_ledger(func)
	l_pre = getattr(func, "_ownership_ledger")
	boundary = ("entry", len(entry.instructions))
	audit = R.StringArcAudit("test::r")
	# Simulate the forbidden emission: a scope-exit release of the
	# moved-out local at the boundary.
	audit.note(R.STAKE_RELEASE, "x", R.SITE_CLASS_SCOPE_EXIT_RELEASE,
		pre_point=boundary, post_point=boundary)
	audit.note_return_boundary(boundary, string_locals=["m", "x"], skipped=[])
	agg = audit.finalize(l_pre=l_pre, l_post=None, needs_drop=lambda _l: True)
	assert agg.get(R.DIV_UNCLASSIFIED, 0) >= 1, agg
	assert agg.get(R.DIV_C4_ALLOWLISTED, 0) in (0, None) or R.DIV_C4_ALLOWLISTED not in agg, agg
	recs = [json.loads(line.split("] ", 1)[1]) for line in out.read_text().splitlines()]
	fn_recs = [r for r in recs if r.get("record") == "fn" and r.get("fn") == "test::r"]
	assert fn_recs, "per-fn record expected (unclassified forces emission via details)"
	kinds = [d.get("kind") for d in fn_recs[0].get("details", [])]
	assert "moved_out_release_regression_retired_c4" in kinds, fn_recs[0]
