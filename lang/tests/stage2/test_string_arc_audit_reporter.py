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
	"""fn f() { var x = "a"; var y = "b"; val m = move x; return; }

	Shapes exercised: overwrite_release + scope_exit_release (string
	locals), moveout_expansion.  Each store consumes its OWN owned
	producer — since slice 4a the store_value_retain fallback is a
	fail-closed tripwire, so a store of a non-owned/multi-use value
	aborts the pass (pinned separately in
	test_dead_store_value_stake_tripwire_fires)."""
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
		M.ConstString(dest="%c2", value="b"),
		M.StoreLocal(local="y", value="%c2"),
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
	assert agg.get("site_class:scope_exit_release", 0) >= 1, agg
	# Slice 4a: the store_value_retain fallback is a fail-closed
	# tripwire — the class must NEVER appear in a successful pass run.
	assert agg.get("site_class:store_value_retain", 0) == 0, agg
	assert agg.get("c2_invisible_stake", 0) == 0, agg
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


def _diamond_flag_func(tt: TypeTable, *, cond_loads_flag_of: str) -> M.MirFunc:
	"""Slice 2 Part 2 population-A carrier: `x` initialized on one arm of
	a diamond (MAYBE_UNINIT at the join), then the authored guarded
	cleanup shape — join loads a drop flag and branches to a drop block
	whose instruction 0 is `MoveOut(x)` feeding a DropValue.

	`cond_loads_flag_of` selects WHICH local's flag the IfTerminator
	condition loads: "x" builds the genuine shape; anything else builds
	the out-of-shape teeth variant (a different local's flag guards the
	branch, so the structural check must refuse)."""
	string_ty = tt.ensure_string()
	bool_ty = tt.ensure_bool()
	func = _make_func(
		"fg",
		params=[],
		locals_=["x", "__drop_flag_x", "__drop_flag_other"],
		types={"x": string_ty, "__drop_flag_x": bool_ty, "__drop_flag_other": bool_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.ConstBool(dest="%c0", value=True)]
	entry.terminator = M.IfTerminator(cond="%c0", then_target="init", else_target="skip")
	init = M.BasicBlock(name="init")
	init.instructions = [
		M.ConstString(dest="%s", value="a"),
		M.StoreLocal(local="x", value="%s"),
	]
	init.terminator = M.Goto(target="join")
	skip = M.BasicBlock(name="skip")
	skip.terminator = M.Goto(target="join")
	join = M.BasicBlock(name="join")
	loaded_flag = "__drop_flag_x" if cond_loads_flag_of == "x" else "__drop_flag_other"
	join.instructions = [M.LoadLocal(dest="%f", local=loaded_flag)]
	join.terminator = M.IfTerminator(cond="%f", then_target="drop_x", else_target="post")
	drop_x = M.BasicBlock(name="drop_x")
	drop_x.instructions = [
		M.MoveOut(dest="%t", local="x", ty=string_ty),
		M.DropValue(value="%t", ty=string_ty),
		M.ConstBool(dest="%z", value=False),
		M.StoreLocal(local="__drop_flag_x", value="%z"),
	]
	drop_x.terminator = M.Goto(target="post")
	post = M.BasicBlock(name="post")
	post.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "init": init, "skip": skip, "join": join, "drop_x": drop_x, "post": post}
	func.entry = "entry"
	setattr(func, "_drop_flag_managed_locals", {"x"})
	setattr(func, "_drop_flag_for_local", {"x": "__drop_flag_x"})
	return func


def _fn_agg(out: Path, fn: str) -> dict:
	recs = [json.loads(line.split("] ", 1)[1]) for line in out.read_text().splitlines()]
	matches = [r for r in recs if r.get("record") == "fn" and r.get("fn") == fn]
	assert matches, recs
	return matches[0]


def _audit_env(monkeypatch, out: Path) -> None:
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_VERBOSE", "1")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", str(out))


def test_c3_flag_guarded_cleanup_is_agree_class(monkeypatch, tmp_path: Path) -> None:
	"""Population A (Slice 2 Part 2): the guarded cleanup MoveOut —
	MAYBE_UNINIT to the flag-blind lattice, ownership proven by the
	runtime flag — classifies c3_moveout_flag_guarded, not divergent."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	func = _diamond_flag_func(tt, cond_loads_flag_of="x")
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::fg")
	assert agg.get(R.AGREE_C3_FLAG_GUARDED, 0) == 1, agg
	assert agg.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 0, agg


def test_c3_flag_guard_wrong_flag_stays_divergent(monkeypatch, tmp_path: Path) -> None:
	"""Teeth (retired-C4 discipline): the IDENTICAL block shape guarded
	by a DIFFERENT local's flag must NOT structurally qualify — it stays
	c3_moveout_not_owned."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	func = _diamond_flag_func(tt, cond_loads_flag_of="other")
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::fg")
	assert agg.get(R.AGREE_C3_FLAG_GUARDED, 0) == 0, agg
	assert agg.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 1, agg


def test_c3_tombstoned_move_is_zero_safe(monkeypatch, tmp_path: Path) -> None:
	"""Population D: zero-init-as-empty-value immediately moved — raw
	TOMBSTONED carries the lattice's own drop-safe-bytes guarantee, so
	the move is a zero-safe byte copy (agree), independent of any type
	predicate."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("tz", params=[], locals_=["x", "m"], types={"x": string_ty, "m": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ZeroValue(dest="%z", ty=string_ty),
		M.StoreLocal(local="x", value="%z"),
		M.MoveOut(dest="%t", local="x", ty=string_ty),
		M.StoreLocal(local="m", value="%t"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::tz")
	assert agg.get(R.AGREE_C3_ZERO_SAFE, 0) == 1, agg
	assert agg.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 0, agg


def test_c3_unreachable_block_event_is_observational(monkeypatch, tmp_path: Path) -> None:
	"""Population C: a MoveOut in a block the CFG walk never reaches
	(dead catch machinery) — state_pre's UNINIT there is a fallback, not
	a verdict; classifies c3_moveout_unreachable_block."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("ur", params=[], locals_=["x"], types={"x": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	dead = M.BasicBlock(name="dead_catch")
	dead.instructions = [
		M.MoveOut(dest="%t", local="x", ty=string_ty),
		M.DropValue(value="%t", ty=string_ty),
	]
	dead.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "dead_catch": dead}
	func.entry = "entry"
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::ur")
	assert agg.get(R.OBS_C3_UNREACHABLE_BLOCK, 0) == 1, agg
	assert agg.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 0, agg


def test_c3_zero_safe_ladder_requires_drop_pairing_and_predicate() -> None:
	"""Population B's rule via direct finalize: MAYBE_UNINIT classifies
	zero-safe ONLY with BOTH legs — the authored MoveOut→DropValue
	pairing AND a true zero-safety predicate.  Either leg missing →
	divergent.  And population E's raw MOVED_OUT re-move stays divergent
	even with both legs present (triage before normalizing)."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	bool_ty = tt.ensure_bool()
	func = _diamond_flag_func(tt, cond_loads_flag_of="x")
	# Strip the flag metadata so the A rule cannot fire; the join-block
	# state for x is MAYBE_UNINIT.
	setattr(func, "_drop_flag_for_local", {})
	setattr(func, "_drop_flag_managed_locals", set())
	_attach_ledger(func)
	l_pre = getattr(func, "_ownership_ledger")
	point = ("join", 0)  # x is MAYBE_UNINIT at the join's entry

	def run(feeds_drop: bool, zero_safe: bool, subject: str = "x", raw_point=point):
		audit = R.StringArcAudit("test::zl")
		audit.note(
			R.STAKE_MOVEOUT_EXPANSION, subject, R.SITE_CLASS_MOVEOUT_EXPANSION,
			pre_point=raw_point, post_point=raw_point,
			moveout_feeds_drop=feeds_drop,
		)
		return audit.finalize(
			l_pre=l_pre, l_post=None, needs_drop=lambda _l: True,
			func=func, zero_safe_ty=lambda _t: zero_safe,
		)

	both = run(True, True)
	assert both.get(R.AGREE_C3_ZERO_SAFE, 0) == 1, both
	no_drop = run(False, True)
	assert no_drop.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 1, no_drop
	not_safe = run(True, False)
	assert not_safe.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 1, not_safe
	# Population E: MOVED_OUT at drop_x's post-MoveOut point — x was
	# moved by drop_x[0], so at index 1 it is MOVED_OUT.  Both legs true
	# must NOT bless it.
	e_shape = run(True, True, raw_point=("drop_x", 1))
	assert e_shape.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 1, e_shape
	assert e_shape.get(R.AGREE_C3_ZERO_SAFE, 0) == 0, e_shape


def test_arraydrop_measurement_mix_and_inertness() -> None:
	"""Slice 3 (report-only): `note_array_drop` records the return-
	boundary Array sweep into a SEPARATE inventory — the reporter derives
	the raw-state and verdict mix, and the string-side counters stay
	byte-identical by construction (`events` counts string stake events
	only; array drops must not touch it)."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func(
		"ad", params=[],
		locals_=["x", "y", "m", "u"],
		types={"x": string_ty, "y": string_ty, "m": string_ty, "u": string_ty},
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
	_attach_ledger(func)
	l_pre = getattr(func, "_ownership_ledger")
	boundary = ("entry", len(entry.instructions))
	audit = R.StringArcAudit("test::ad")
	audit.note_array_drop("y", point=boundary, needs_drop=True)   # LIVE -> must_drop
	audit.note_array_drop("x", point=boundary, needs_drop=True)   # MOVED_OUT -> must_not_drop
	audit.note_array_drop("u", point=boundary, needs_drop=True)   # UNINIT -> must_not_drop
	audit.note_array_drop("m", point=boundary, needs_drop=False)  # LIVE but drop-free type -> must_not_drop
	agg = audit.finalize(l_pre=l_pre, l_post=None, needs_drop=lambda _l: True)
	# The mix.
	assert agg.get("site_class:scope_exit_arraydrop") == 4, agg
	assert agg.get("arraydrop_state:live") == 2, agg
	assert agg.get("arraydrop_state:moved_out") == 1, agg
	assert agg.get("arraydrop_state:uninit") == 1, agg
	assert agg.get("arraydrop_verdict:must_drop") == 1, agg
	assert agg.get("arraydrop_verdict:must_not_drop") == 3, agg
	# Inertness: no string stake events were involved — the string-side
	# event counter and divergence classes are untouched.
	assert agg.get("events") == 0, agg
	assert agg.get(R.DIV_UNCLASSIFIED, 0) == 0, agg


def test_arraydrop_note_site_covers_return_sweep(monkeypatch, tmp_path: Path) -> None:
	"""End-to-end coverage of the string_arc NOTE SITE (the direct-API pin
	above does not exercise it): insert_string_arc over a func with real
	Array locals reaching the return-boundary sweep, audit env on.

	- `a_uninit` (never written) and `a_live` (stored) are swept →
	  recorded with their raw states and verdicts;
	- `a_moved` is moved out IN the return block, so string_arc's own
	  `moved_out_locals` fold puts it in skip_cleanup_locals → the sweep
	  skips it and it must NOT be recorded."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	arr_ty = tt.new_array(tt.ensure_string())
	func = _make_func(
		"asw", params=[],
		locals_=["a_uninit", "a_live", "a_moved", "sink"],
		types={"a_uninit": arr_ty, "a_live": arr_ty, "a_moved": arr_ty, "sink": arr_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ZeroValue(dest="%z", ty=arr_ty),
		M.StoreLocal(local="a_live", value="%z"),
		M.ZeroValue(dest="%z2", ty=arr_ty),
		M.StoreLocal(local="a_moved", value="%z2"),
		M.MoveOut(dest="%mv", local="a_moved", ty=arr_ty),
		M.StoreLocal(local="sink", value="%mv"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::asw")
	# Swept: a_uninit + a_live + sink (holds the moved-in array, LIVE at
	# the boundary); skipped: a_moved (moved_out_locals fold).
	assert agg.get("site_class:scope_exit_arraydrop") == 3, agg
	assert agg.get("arraydrop_state:uninit") == 1, agg
	# a_live was zero-init-stored → the walker records the zero-store as
	# TOMBSTONED; sink holds a real moved-in value → LIVE.  Both states
	# recorded; a_moved contributes nothing.
	assert agg.get("arraydrop_state:tombstoned") == 1, agg
	assert agg.get("arraydrop_state:live") == 1, agg
	assert agg.get("arraydrop_state:moved_out") is None, agg
	# Verdicts recorded for every swept drop (uninit/tombstoned →
	# must_not_drop; live Array<String> → must_drop).
	assert agg.get("arraydrop_verdict:must_not_drop") == 2, agg
	assert agg.get("arraydrop_verdict:must_drop") == 1, agg


def test_zerovalue_store_needs_no_stake(monkeypatch, tmp_path: Path) -> None:
	"""C2-singleton fix (2026-07-13): storing a fresh ZeroValue String —
	the `captures(move <String>)` env-slot zero-back shape — must NOT
	stake: zeroed String bytes are a valid owned empty value (retain and
	release are both runtime no-ops).  Pre-fix, the StoreRef path's
	`_ensure_owned` emitted a dead retain here — the last
	c2_invisible_stake / store_value_retain residual."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	# "%z" MUST be in func.local_types (review finding): the store path's
	# `_ensure_owned` gates on `_is_string_value(val)` =
	# `local_types.get(val) is String`.  Production HIR lowering records
	# ZeroValue dest types in local_types (the wild carrier's `.t10` has
	# it), and the regression depends on that metadata — without it the
	# pin passes VACUOUSLY (early return before the stake decision) and
	# proves nothing about the ZeroValue-owned classification.
	func = _make_func(
		"zb", params=[], locals_=["x"],
		types={"x": string_ty, "%z": string_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%c", value="owned-by-slot"),
		M.StoreLocal(local="x", value="%c"),
		M.AddrOfLocal(dest="%p", local="x", is_mut=True),
		M.ZeroValue(dest="%z", ty=string_ty),
		M.StoreRef(ptr="%p", value="%z", inner_ty=string_ty),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::zb")
	# The zero-back must not stake — no store_value_retain, no C2.
	assert agg.get("site_class:store_value_retain", 0) == 0, agg
	assert agg.get(R.DIV_C2_INVISIBLE_STAKE, 0) == 0, agg
	# Overwrite releases are still emitted (one for the initial
	# StoreLocal over x's uninit slot, one for the StoreRef over the
	# slot's old value) — the fix touches ONLY the stored-value
	# classification, never the release side.
	assert agg.get("site_class:overwrite_release", 0) == 2, agg


def test_dead_store_value_stake_tripwire_fires(monkeypatch) -> None:
	"""Slice 4a: the store_value_retain fallback is FAIL-CLOSED.  A store
	of a non-owned multi-use String value (the pre-B-arch double-store
	shape, unreachable from real HIR since string_stakes owns store
	staking) must abort the pass with the STRUCTURED tripwire message —
	site-class, fn, block/index, value, target, producer, report path —
	so the failure mode and wording stay stable."""
	monkeypatch.delenv("DRIFT_STRING_ARC_AUDIT", raising=False)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("tw", params=[], locals_=["x", "y"], types={"x": string_ty, "y": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%c", value="a"),
		M.StoreLocal(local="x", value="%c"),
		M.StoreLocal(local="y", value="%c"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_attach_ledger(func)
	try:
		insert_string_arc(func, type_table=tt, fn_infos={})
	except AssertionError as err:
		msg = str(err)
		assert "string_arc dead-stake tripwire [store_value_retain]" in msg, msg
		assert "fn 'test::tw'" in msg, msg
		# %c is consumed twice, so `_can_move_owned_once` already fails
		# at the FIRST store — the fallback (and thus the wire) trips at
		# entry[1], not at the second store.
		assert "block 'entry'[1]" in msg, msg
		assert "value '%c'" in msg, msg
		assert "StoreLocal 'x'" in msg, msg
		assert "producer=ConstString" in msg, msg
		assert "issues/string-arc-dead-stake-tripwire/" in msg, msg
	else:
		raise AssertionError("dead store_value stake fallback did not trip")


def test_tripwire_surfaces_as_clean_internal_diagnostic(tmp_path: Path, monkeypatch) -> None:
	"""The driver's string_arc boundary converts pass AssertionErrors
	into a clean `internal:` diagnostic (best-effort span, phased) — an
	operator never sees a Python traceback.  Injected via monkeypatch
	because no real source can reach the tripwire (that is the point of
	fail-closed)."""
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc import driftc as D
	from lang.driftc.core.function_id import function_symbol

	src = tmp_path / "main.drift"
	src.write_text("module main;\n\npub fn main() nothrow -> Int {\n\tval s = \"x\" + \"y\";\n\tif s.byte_length() > 0 { return 0; }\n\treturn 1;\n}\n")
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	origin = {}
	for m in modules.values():
		origin.update(m.origin_by_fn_id)

	_orig = D.insert_string_arc
	def _boom(func, **kw):
		if getattr(func, "name", "") == "main":
			raise AssertionError(
				"string_arc dead-stake tripwire [store_value_retain]: injected-for-pin"
			)
		return _orig(func, **kw)
	monkeypatch.setattr(D, "insert_string_arc", _boom)

	ir, checked = D.compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc,
		entry=function_symbol(main_id),
		type_table=type_table,
		module_exports=mexp,
		module_deps=mdeps,
		origin_by_fn_id=origin,
		enforce_entrypoint=True,
		reserved_namespace_policy=D.ReservedNamespacePolicy.ALLOW_DEV,
	)
	errors = [d for d in getattr(checked, "diagnostics", []) if getattr(d, "severity", None) == "error"]
	assert errors, "injected tripwire must surface as a diagnostic"
	msgs = [d.message for d in errors]
	assert any(
		"internal: string ownership stake contract failure" in m
		and "dead-stake tripwire" in m
		for m in msgs
	), msgs
	# Clean surface: a diagnostic, not a propagated exception — and the
	# compile returned instead of raising (we got here), with no IR.
	assert ir == "", "compile must not produce IR after the tripwire"


def test_c2_invisible_stake_classifier_still_covered() -> None:
	"""C2 coverage moved off the (now fail-closed) store fallback: a
	RETAIN of an untracked SSA temp in a non-extinct site class is an
	invisible stake."""
	tt = TypeTable()
	func = _string_shuffle_func(tt)
	_attach_ledger(func)
	l_pre = getattr(func, "_ownership_ledger")
	audit = R.StringArcAudit("test::c2")
	audit.note(
		R.STAKE_RETAIN, ".t99", R.SITE_CLASS_VALUE_POSITION_RETAIN,
		pre_point=("entry", 0), post_point=("entry", 0),
	)
	agg = audit.finalize(l_pre=l_pre, l_post=None, needs_drop=lambda _l: True)
	assert agg.get(R.DIV_C2_INVISIBLE_STAKE, 0) == 1, agg


def _ail_store_func(tt: TypeTable, *, unchecked: bool) -> M.MirFunc:
	"""A String element extraction feeding a store: `x = arr[i]` at the
	MIR level.  The AIL/AILU dest `%e` is DELIBERATELY NOT pre-seeded in
	func.local_types — production metadata can omit SSA temps, and the
	slice-4a pre-scan must register the type from the INSTRUCTION."""
	string_ty = tt.ensure_string()
	arr_ty = tt.new_array(string_ty)
	func = _make_func(
		"ail_u" if unchecked else "ail_c",
		params=[],
		locals_=["arr", "x"],
		types={"arr": arr_ty, "x": string_ty},
	)
	load_cls = M.ArrayIndexLoadUnchecked if unchecked else M.ArrayIndexLoad
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ZeroValue(dest="%a", ty=arr_ty),
		M.StoreLocal(local="arr", value="%a"),
		M.ConstInt(dest="%i", value=0),
		load_cls(dest="%e", elem_ty=string_ty, array="arr", index="%i"),
		M.StoreLocal(local="x", value="%e"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	return func


def test_array_index_load_dest_is_owned_at_extraction(monkeypatch, tmp_path: Path) -> None:
	"""Slice 4a review pin (BLOCKING finding): string_arc's producer
	chain must classify ArrayIndexLoad String dests OWNED AT EXTRACTION
	(codegen retains the loaded element — the B-arch-1d contract the
	slice-1 static pin enforces for codegen+string_stakes).  The pre-fix
	VIEW classification (`owned_values.discard`) sent the single-use
	store to the fallback's retain arm — i.e. straight into the slice-4a
	dead-stake tripwire."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	func = _ail_store_func(tt, unchecked=False)
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})  # must not trip
	agg = _fn_agg(out, "test::ail_c")
	assert agg.get("site_class:store_value_retain", 0) == 0, agg
	assert agg.get(R.DIV_C2_INVISIBLE_STAKE, 0) == 0, agg


def test_array_index_load_unchecked_dest_is_owned_at_extraction(monkeypatch, tmp_path: Path) -> None:
	"""Sibling pin for ArrayIndexLoadUnchecked, which the pre-fix chain
	did not handle AT ALL: the dest stayed untyped, so the store took
	the silent pass-through — no tripwire and no retain either way,
	which is why this pin ALSO asserts the observable the fix adds: the
	pre-scan registers the dest's String type from the instruction
	(local_types is the metadata every downstream ownership decision
	keys on)."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _ail_store_func(tt, unchecked=True)
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})  # must not trip
	agg = _fn_agg(out, "test::ail_u")
	assert agg.get("site_class:store_value_retain", 0) == 0, agg
	assert agg.get(R.DIV_C2_INVISIBLE_STAKE, 0) == 0, agg
	# Teeth for the unhandled-classification half: post-fix the dest is
	# instruction-typed and owned (moved into the store); pre-fix it was
	# invisible to the ownership machinery entirely.
	assert func.local_types.get("%e") == string_ty, dict(func.local_types)


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
