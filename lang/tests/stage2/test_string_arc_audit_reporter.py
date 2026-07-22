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


def _run_pipeline(func: M.MirFunc, tt: TypeTable, fn_infos=None) -> None:
	"""Production-faithful pipeline for unit pins: materialize last-use
	releases → fresh ledger → insert_string_arc.  Bare insert_string_arc
	is not a valid configuration for MIR containing family temps that
	drain non-consumingly — string_arc authors no last-use releases of
	its own (the in-pass arm was deleted with the tripwire-deletion
	slice, 2026-07-18), so bare use silently under-releases such
	temps."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	materialize_lastuse_releases(func, type_table=tt, fn_infos=fn_infos or {})
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos=fn_infos or {})


def _string_shuffle_func(type_table: TypeTable) -> M.MirFunc:
	"""fn f() { var x = "a"; var y = "b"; val m = move x; return; }

	Shapes exercised: overwrite_release + scope_exit_release (string
	locals), moveout_expansion.  Each store consumes its OWN owned
	producer — store staking is owned upstream by string_stakes, and
	the store paths consume their source exactly once, retain-free
	(the slice-4a fail-closed fallback that guarded this contract was
	deleted with the tripwire-deletion slice, 2026-07-18; the
	store_value_retain class stays a hard corpus gate)."""
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
	# store_value_retain is a retired stake class (4a fail-closed, arm
	# deleted 2026-07-18) and a hard corpus gate — it must NEVER appear
	# in a pass run.
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
	# Shape-3 rule (E-triage close-out): MOVED_OUT at drop_x's
	# post-MoveOut point — x was moved by drop_x[0], so at index 1 it is
	# MOVED_OUT.  WITH the immediate-DropValue pairing it is the
	# compiler-authored dead drop of zero-backed storage → zero-safe.
	paired_moved_out = run(True, True, raw_point=("drop_x", 1))
	assert paired_moved_out.get(R.AGREE_C3_ZERO_SAFE, 0) == 1, paired_moved_out
	assert paired_moved_out.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 0, paired_moved_out
	# WITHOUT the pairing (a store/call/scrutinee consumer — the
	# shapes-1/2 value-corruption class) it stays DIVERGENT: the drop
	# pairing is load-bearing, and the zero-safety predicate cannot
	# substitute for it (zero_safe=True here).
	unpaired_moved_out = run(False, True, raw_point=("drop_x", 1))
	assert unpaired_moved_out.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 1, unpaired_moved_out
	assert unpaired_moved_out.get(R.AGREE_C3_ZERO_SAFE, 0) == 0, unpaired_moved_out


# test_arraydrop_measurement_mix_and_inertness RETIRED with the
# review-closure round of string-arc-endgame-array-sweep (2026-07-19):
# its subject — the `note_array_drop` direct API and the arraydrop
# counter aggregation — was deleted with the reporter note surface
# (the sweep it measured died in B-U; no compatibility consumer of
# the counters exists).  A resurrected `scope_exit_arraydrop` tag now
# counts UNTAGGED, which is a hard corpus gate.


def test_arraydrop_note_site_covers_return_sweep(monkeypatch, tmp_path: Path) -> None:
	"""B-U RETIREMENT pin (string-arc-endgame-array-sweep, 2026-07-19;
	checkpoint §6 pin 6, unit half — formerly the Array
	release-elision pin, reworked when the sweep it pinned was
	DELETED): insert_string_arc over real Array locals at a Return
	boundary AUTHORS NOTHING for them —

	- ZERO scope_exit_arraydrop notes (the note site died with the
	  sweep; the class would be a regression);
	- ZERO Return-boundary ArrayDrop emissions for ANY local, LIVE
	  `sink` included — scope-exit array drops are cleanup_authoring's
	  sole authority (hook-authored; this bare carrier has no hooks,
	  which is exactly why arc must not backstop it);
	- R7 array overwrite drops (a_live/a_moved/sink StoreLocal path)
	  MOVED to overwrite_cleanup (Slice B1, 2026-07-20) — string_arc
	  emits ZERO ArrayDrops now (pinned here); the overwrite drops are
	  pinned in test_overwrite_cleanup.py."""
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
	# The sweep and its note site are GONE: no scope_exit_arraydrop
	# key may appear — for any state, any verdict.
	assert not any(
		k.startswith(("site_class:scope_exit_arraydrop", "arraydrop_"))
		for k in agg
	), agg
	# string_arc emits ZERO ArrayDrops — the R7 overwrite drop authority
	# moved to overwrite_cleanup (which runs after string_arc).
	arraydrops = [
		ins for blk in func.blocks.values() for ins in blk.instructions
		if type(ins).__name__ == "ArrayDrop"
	]
	assert not arraydrops, f"string_arc must emit no ArrayDrop post-B1: {arraydrops}"


def test_array_elision_keeps_path_dependent_drop(monkeypatch, tmp_path: Path) -> None:
	"""B-U rework (formerly: PATH_DEPENDENT sweep drop KEPT — the
	first-slice elision discipline; that sweep is DELETED): a
	PATH_DEPENDENT array at the Return boundary now gets NO drop from
	string_arc at all — its drop is authored upstream by
	cleanup_authoring's unguarded zero-storage branch at the
	CleanupHook (pinned in test_cleanup_authoring.py::
	test_authoring_emits_unguarded_drop_for_path_dependent_array; this
	bare carrier has no hook, so the correct arc output is NOTHING)."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	arr_ty = tt.new_array(tt.ensure_string())
	func = _make_func("apd", params=[], locals_=["arr"], types={"arr": arr_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.ConstBool(dest="%c", value=True)]
	entry.terminator = M.IfTerminator(cond="%c", then_target="init", else_target="join")
	init = M.BasicBlock(name="init")
	init.instructions = [
		M.ZeroValue(dest="%z", ty=arr_ty),
		M.StoreLocal(local="arr", value="%z"),
		M.MoveOut(dest="%m", local="arr", ty=arr_ty),
		M.StoreLocal(local="arr", value="%m"),
	]
	init.terminator = M.Goto(target="join")
	join = M.BasicBlock(name="join")
	join.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "init": init, "join": join}
	func.entry = "entry"
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::apd")
	# arr at the join boundary: LIVE on the init path, UNINIT on the
	# else path → MAYBE_UNINIT → PATH_DEPENDENT.  Post-B-U the sweep
	# is gone: no arraydrop key, and no ArrayDrop in the join block.
	assert not any(
		k.startswith(("site_class:scope_exit_arraydrop", "arraydrop_"))
		for k in agg
	), agg
	join_drops = [
		ins for ins in func.blocks["join"].instructions
		if type(ins).__name__ == "ArrayDrop"
	]
	assert not join_drops, join_drops


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
	# Overwrite releases MOVED to overwrite_cleanup (Slice B1,
	# 2026-07-20) — string_arc no longer emits them; this test now
	# pins only the C2/store-value classification.  The release side
	# is pinned in test_overwrite_cleanup.py.
	assert agg.get("site_class:overwrite_release", 0) == 0, agg


def test_string_arc_boundary_wrap_contains_assertions(tmp_path: Path, monkeypatch) -> None:
	"""The driver's string_arc boundary converts pass AssertionErrors
	into a clean `internal:` diagnostic (best-effort span, phased) — an
	operator never sees a Python traceback.  Generalized from the
	tripwire-era pin (tripwire-deletion slice, 2026-07-18): the wrap is
	a user-facing containment contract independent of any particular
	in-tree assertion source, so this pin survives the tripwires it was
	born fronting.  Injected via monkeypatch because the pass's
	remaining fail-closed checks are unreachable from real source."""
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
				"string_arc contract failure: injected-for-pin"
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
	assert errors, "injected contract failure must surface as a diagnostic"
	msgs = [d.message for d in errors]
	# Phase/internal diagnostic: the wrap's stable prefix carries the
	# string_arc phase identity and the injected payload verbatim.
	assert any(
		"internal: string ownership stake contract failure" in m
		and "injected-for-pin" in m
		for m in msgs
	), msgs
	assert any(getattr(d, "phase", None) == "string_arc" for d in errors), [
		(d.message, getattr(d, "phase", None)) for d in errors
	]
	# No traceback: the compile RETURNED with a diagnostic instead of
	# propagating the AssertionError (reaching this line is the proof).
	# Empty IR: containment aborts emission for the unit.
	assert ir == "", "compile must not produce IR after a contract failure"


def test_c2_invisible_stake_classifier_still_covered() -> None:
	"""C2 coverage moved off the store fallback (fail-closed in 4a,
	deleted 2026-07-18): a RETAIN of an untracked SSA temp in a
	non-extinct site class is an invisible stake."""
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


def test_c3_catch_binder_dead_cleanup_drop_is_zero_safe(monkeypatch, tmp_path: Path) -> None:
	"""E-triage shape 3, end to end through insert_string_arc: the
	catch-arm binder is materialized, MOVED BY THE USER (`val moved =
	move e`), and the compiler-authored end-of-arm cleanup still emits
	`MoveOut(e) → DropValue` — a dead drop of zero-backed storage.  The
	shape mirrors catch_binder_visible_in_arm's try_catch_0 block.  It
	must reclassify `c3_moveout_zero_safe`, leaving
	c3_moveout_not_owned at 0 for the fn."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	err_ty = tt.ensure_error()
	func = _make_func(
		"cb3", params=[],
		locals_=["__try_err.t1", "e", "moved"],
		types={"__try_err.t1": err_ty, "e": err_ty, "moved": err_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ZeroValue(dest="%z", ty=err_ty),
		M.StoreLocal(local="__try_err.t1", value="%z"),
		M.MoveOut(dest="%b", local="__try_err.t1", ty=err_ty),
		M.StoreLocal(local="e", value="%b"),
		M.MoveOut(dest="%m", local="e", ty=err_ty),           # user: val moved = move e
		M.StoreLocal(local="moved", value="%m"),
		M.MoveOut(dest="%c1", local="moved", ty=err_ty),      # authored cleanup of moved ✓
		M.DropValue(value="%c1", ty=err_ty),
		M.MoveOut(dest="%c2", local="e", ty=err_ty),          # authored cleanup of e — ALREADY MOVED
		M.DropValue(value="%c2", ty=err_ty),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::cb3")
	# The dead cleanup drop of the moved binder reclassifies zero-safe;
	# nothing in this fn stays divergent.  (The other MoveOuts: the
	# binder materialization from the zero-init'd __try_err is
	# TOMBSTONED→zero_safe; the two user/cleanup moves of live locals
	# are owned.)
	assert agg.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 0, agg
	assert agg.get(R.AGREE_C3_ZERO_SAFE, 0) >= 2, agg


# The slice-4a/4b dead-stake trigger pins (`_view_prelude` /
# `_expect_tripwire` / test_dead_{store_value,call_arg,value_position,
# return_site3}_stake_tripwire_fires) retired with the tripwire-deletion
# slice (2026-07-18): their subject — the fail-closed late-retain arms —
# was deleted after the clean 0.33.84 cert cycle.  The classes stay
# guarded by the corpus tool's four site-class HARD gates and the
# retired-tag → UNTAGGED pin below.


def test_retired_site_classes_are_untagged() -> None:
	"""Enumeration-retirement pin: `destructor_self` (slice 4b — no
	emission site anywhere) and `temp_lastuse_release` (tripwire-deletion
	slice, 2026-07-18 — emission arm deleted) are out of the closed set;
	a note() carrying either lands in UNTAGGED — already a hard corpus
	gate — instead of a dead accepted (or counted-only) tag."""
	audit = R.StringArcAudit("test::ds")
	audit.note(R.STAKE_RETAIN, "%v", R.SITE_CLASS_DESTRUCTOR_SELF,
		pre_point=("b", 0), post_point=("b", 0))
	assert audit.untagged == 1
	assert audit.events[0].site_class.startswith("UNTAGGED:")
	audit.note(R.STAKE_RELEASE, "%w", R.SITE_CLASS_TEMP_LASTUSE_RELEASE,
		pre_point=("b", 1), post_point=("b", 1))
	assert audit.untagged == 2
	assert audit.events[1].site_class.startswith("UNTAGGED:")


def test_record_counted_only_site4_exact_delta_isolation() -> None:
	"""`record_counted_only(SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4, n)` bumps
	ONLY the aggregate `events` by n and the
	`site_class:drop_before_overwrite_site4` key by n — `fns`, every C1/C3
	key, and unrelated keys are untouched (exact-delta)."""
	before = dict(R._GLOBAL_AGGREGATE)
	R.record_counted_only(R.SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4, 3)
	after = dict(R._GLOBAL_AGGREGATE)
	delta = {k: after.get(k, 0) - before.get(k, 0) for k in set(before) | set(after)}
	nonzero = {k: v for k, v in delta.items() if v != 0}
	assert nonzero == {
		"events": 3,
		"site_class:drop_before_overwrite_site4": 3,
	}, nonzero
	for forbidden in (
		"fns", "skipped_no_ledger",
		R.DIV_C1_RELEASE_WITHOUT_MUST_DROP, R.DIV_C1_MUST_DROP_WITHOUT_RELEASE,
		R.DIV_C1_PATH_DEPENDENT, "c3_moveout_owned", "c3_moveout_not_owned",
		"site_class:overwrite_release",
	):
		assert delta.get(forbidden, 0) == 0, (forbidden, delta.get(forbidden))
	# restore
	R._GLOBAL_AGGREGATE.clear()
	R._GLOBAL_AGGREGATE.update(before)


def test_materialized_lastuse_is_closed_counted_only() -> None:
	"""Reporter contract pin (review tightening): the new
	`materialized_lastuse_release` class is a member of the closed
	enumeration (not UNTAGGED) and is counted-only — it does not enter
	C1 (not a scope_exit_release), C2 (not RETAIN-kind), C3 (not
	MOVEOUT-kind), and never lands in UNCLASSIFIED."""
	assert R.SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE in R.STRING_ARC_SITE_CLASSES
	tt = TypeTable()
	func = _string_shuffle_func(tt)
	_attach_ledger(func)
	l_pre = getattr(func, "_ownership_ledger")
	audit = R.StringArcAudit("test::mcl")
	audit.note(
		R.STAKE_RELEASE, ".t7", R.SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE,
		pre_point=("entry", 0), post_point=("entry", 0),
	)
	# A return boundary so the C1 comparison actually runs.
	boundary = ("entry", len(func.blocks["entry"].instructions))
	audit.note_return_boundary(boundary, string_locals=["x"], skipped=["x"])
	agg = audit.finalize(l_pre=l_pre, l_post=None, needs_drop=lambda _l: True)
	assert audit.untagged == 0
	assert agg.get("site_class:materialized_lastuse_release") == 1, agg
	assert agg.get(R.DIV_UNCLASSIFIED, 0) == 0, agg
	assert agg.get(R.DIV_C2_INVISIBLE_STAKE, 0) == 0, agg
	assert agg.get("c3_moveout_not_owned", 0) == 0, agg
	assert agg.get("c3_moveout_owned", 0) == 0, agg
	# C1 saw only the boundary bookkeeping, not the release event.
	assert agg.get(R.DIV_C1_RELEASE_WITHOUT_MUST_DROP, 0) == 0, agg


def test_tlr2a_calculator_conforms_to_string_arc(monkeypatch, tmp_path: Path) -> None:
	"""TLR-2a conformance pin (contract 2 vs the live pass): run
	`compute_lastuse_release_points` AND `insert_string_arc` over one
	block containing every reviewed shape, and assert the calculator's
	points equal exactly where string_arc emits materialized releases:
	- `%q` — qualified simple temp (one StringEq use);
	- `%r` — REPEATED-OPERAND case `StringEq(%r, %r)` (multiplicity rule
	  §3a: ONE release, after the draining instruction);
	- `%s` — consumed single-use ConstString (stored → CONSUME
	  disposition → NO release by either author);
	- `%cc` — Concat-produced temp: IN the family since TLR-3 → in the
	  calculator's result and released as materialized (this pin is the
	  contract-level record of the family extension);
	- `%ig` — ConstString passed to an info-less Call (IGNORE
	  disposition: counted but never drained → NO release by either)."""
	from lang.driftc.stage2.string_ownership_analysis import compute_lastuse_release_points
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	from lang.driftc.checker import FnInfo, FnSignature
	qfid = FunctionId(module="m", name="mk", ordinal=0)
	fn_infos = {qfid: FnInfo(
		fn_id=qfid, name="mk", declared_can_throw=False,
		signature=FnSignature(name="mk", return_type_id=string_ty))}
	func = _make_func("cf", params=[], locals_=["x"], types={"x": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.Call(dest="%qc", fn_id=qfid, args=[], can_throw=False),
		M.ConstString(dest="%q", value="q"),
		M.ConstString(dest="%r", value="r"),
		M.ConstString(dest="%s", value="s"),
		M.ConstString(dest="%c3", value="c"),
		M.ConstString(dest="%c4", value="d"),
		M.StringConcat(dest="%cc", left="%c3", right="%c4"),
		M.ConstString(dest="%ig", value="i"),
		M.StringEq(dest="%e1", left="%q", right="%cc"),          # last use of %q and %cc
		M.StringEq(dest="%e2", left="%r", right="%r"),           # repeated operand
		M.StoreLocal(local="x", value="%s"),                     # consuming use of %s
		M.Call(dest=None, fn_id=FunctionId(module="t", name="opaque", ordinal=0),
			args=["%ig"], can_throw=False),                       # info-less call: IGNORE
		M.StringEq(dest="%e3", left="%qc", right="%qc"),          # last use of %qc (TLR-4 Call family)
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"

	points = compute_lastuse_release_points(
		entry, local_types=dict(func.local_types) | {
			"%q": string_ty, "%r": string_ty, "%s": string_ty, "%qc": string_ty,
			"%c3": string_ty, "%c4": string_ty, "%cc": string_ty, "%ig": string_ty,
		},
		fn_infos=fn_infos, type_table=tt, live_out_names=set(),
	)
	# %q AND %cc (family since TLR-3) drain at the first StringEq (idx 8);
	# %r at the repeated-operand StringEq (idx 9) — ONE point despite two
	# occurrences; %c3/%c4 drain at the concat (idx 6); %qc — the TLR-4
	# Call-family column (fn_infos-proven semantic-String, nothrow) — at
	# its repeated-operand StringEq (idx 12).  %s (consumed) and %ig
	# (IGNORE) have no points.
	assert points == {"%q": 8, "%cc": 8, "%r": 9, "%c3": 6, "%c4": 6, "%qc": 12}, points

	# live pass agreement: seed the temp types the calculator was given.
	for k, v in {"%q": string_ty, "%r": string_ty, "%s": string_ty,
	             "%c3": string_ty, "%c4": string_ty, "%cc": string_ty,
	             "%ig": string_ty, "%qc": string_ty}.items():
		func.local_types[k] = v
	# The live half runs the production pipeline (pass → arc): the
	# materialized releases come from the pass + recognition arm — same
	# positions, same counters (arc authors no last-use releases).
	_run_pipeline(func, tt, fn_infos)
	agg = _fn_agg(out, "test::cf")
	# materialized = exactly the calculator's points (6, incl. %cc since
	# TLR-3 and %qc since TLR-4); %s and %ig produce none.
	assert agg.get("site_class:materialized_lastuse_release") == 6, agg
	assert agg.get("site_class:temp_lastuse_release", 0) == 0, agg
	# and the emitted releases sit where the calculator said: for each
	# point, the temp's StringRelease appears in the release run
	# IMMEDIATELY following its draining instruction (located by shape —
	# string_arc's other insertions shift raw indices).
	out_instrs = func.blocks["entry"].instructions
	def _releases_right_after(pred):
		for i, ins in enumerate(out_instrs):
			if pred(ins):
				got = set()
				j = i + 1
				while j < len(out_instrs) and type(out_instrs[j]).__name__ == "StringRelease":
					got.add(out_instrs[j].value)
					j += 1
				return got
		raise AssertionError("draining instruction not found in output")
	after_concat = _releases_right_after(lambda i: type(i).__name__ == "StringConcat" and i.dest == "%cc")
	assert {"%c3", "%c4"} <= after_concat, after_concat
	after_eq1 = _releases_right_after(lambda i: type(i).__name__ == "StringEq" and i.dest == "%e1")
	assert {"%q", "%cc"} <= after_eq1, after_eq1  # cross-family same-drain group
	after_eq2 = _releases_right_after(lambda i: type(i).__name__ == "StringEq" and i.dest == "%e2")
	assert after_eq2 == {"%r"}, after_eq2  # ONE release for the repeated operand
	after_eq3 = _releases_right_after(lambda i: type(i).__name__ == "StringEq" and i.dest == "%e3")
	assert after_eq3 == {"%qc"}, after_eq3  # the Call-family release (TLR-4)


def test_tlr2a_prescan_exclusion_contract(monkeypatch) -> None:
	"""TLR-2b prescan-exclusion contract, pinned at the calculator now:
	an in-contract pre-materialized StringRelease contributes NO
	occurrence to any count — every OTHER temp's release point is
	unchanged versus the same block without it, and the released temp is
	excluded from the result."""
	from lang.driftc.stage2.string_ownership_analysis import compute_lastuse_release_points
	tt = TypeTable()
	string_ty = tt.ensure_string()
	lt = {"%a": string_ty, "%b": string_ty}
	base = [
		M.ConstString(dest="%a", value="a"),
		M.ConstString(dest="%b", value="b"),
		M.StringEq(dest="%e", left="%a", right="%b"),
	]
	blk_plain = M.BasicBlock(name="p")
	blk_plain.instructions = list(base)
	blk_plain.terminator = M.Return(value=None)
	blk_with = M.BasicBlock(name="w")
	blk_with.instructions = list(base) + [M.StringRelease(value="%a")]
	blk_with.terminator = M.Return(value=None)
	plain = compute_lastuse_release_points(
		blk_plain, local_types=lt, fn_infos={}, type_table=tt, live_out_names=set())
	withrel = compute_lastuse_release_points(
		blk_with, local_types=lt, fn_infos={}, type_table=tt, live_out_names=set())
	assert plain == {"%a": 2, "%b": 2}, plain
	# %a excluded (externally released); %b's point UNCHANGED (the
	# release contributed no occurrence — index still 2).
	assert withrel == {"%b": 2}, withrel


def test_tlr2a_misplaced_input_release_is_rejected(monkeypatch) -> None:
	"""TLR-2b recognition contract, placement half (review-hardened):
	shape recognition alone (block-local ConstString producer) would
	accept a StringRelease sitting BEFORE a later use of the same temp —
	excluded from counting, suppressing string_arc's own release, and
	leaving the later use reading freed memory.  Placement must be
	validated against the computed release point; a mis-placed release
	is REJECTED fail-closed, never silently recognized."""
	import pytest
	from lang.driftc.stage2.string_ownership_analysis import compute_lastuse_release_points
	tt = TypeTable()
	string_ty = tt.ensure_string()
	lt = {"%a": string_ty, "%b": string_ty}
	blk = M.BasicBlock(name="m")
	blk.instructions = [
		M.ConstString(dest="%a", value="a"),
		M.ConstString(dest="%b", value="b"),
		M.StringRelease(value="%a"),           # BEFORE %a's real last use
		M.StringEq(dest="%e", left="%a", right="%b"),
	]
	blk.terminator = M.Return(value=None)
	with pytest.raises(AssertionError, match="unexpected input release"):
		compute_lastuse_release_points(
			blk, local_types=lt, fn_infos={}, type_table=tt, live_out_names=set())
	# Duplicate releases of one temp are equally out of contract, even
	# when the first sits at the correct draining point.
	blk2 = M.BasicBlock(name="d")
	blk2.instructions = [
		M.ConstString(dest="%a", value="a"),
		M.ConstString(dest="%b", value="b"),
		M.StringEq(dest="%e", left="%a", right="%b"),
		M.StringRelease(value="%a"),
		M.StringRelease(value="%a"),
	]
	blk2.terminator = M.Return(value=None)
	with pytest.raises(AssertionError, match="unexpected input release"):
		compute_lastuse_release_points(
			blk2, local_types=lt, fn_infos={}, type_table=tt, live_out_names=set())


def test_tlr2a_seeder_closes_missing_metadata_gap(monkeypatch, tmp_path: Path) -> None:
	"""TLR-2a review finding 1: temps in production MIR may lack
	local_types entries (HIR lowering does not record every temp); the
	live pass seeds them internally via `_seed_dest_types`.  This pin
	uses the TLR-2b calling pattern — `seed_string_dest_types` on a COPY,
	then the calculator — with NO manual temp seeding, and asserts
	agreement with the live pass.  Without the seeder the calculator
	sees no String temps at all (the gap the review flagged)."""
	from lang.driftc.stage2.string_ownership_analysis import (
		compute_lastuse_release_points,
		seed_string_dest_types,
	)
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	# NOTE: no %-temp entries in local_types — production-like.
	func = _make_func("sg", params=[], locals_=["x"], types={"x": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%c1", value="a"),
		M.ConstString(dest="%c2", value="b"),
		M.StringEq(dest="%e", left="%c1", right="%c2"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"

	# Un-seeded: the calculator (documented contract: caller seeds) sees
	# no String-typed temps — the gap is real, not hypothetical.
	bare = compute_lastuse_release_points(
		entry, local_types=dict(func.local_types),
		fn_infos={}, type_table=tt, live_out_names=set())
	assert bare == {}, bare
	# The TLR-2b pattern: seed a copy, then compute.
	seeded = dict(func.local_types)
	seed_string_dest_types([entry], seeded, fn_infos={}, type_table=tt)
	points = compute_lastuse_release_points(
		entry, local_types=seeded, fn_infos={}, type_table=tt, live_out_names=set())
	assert points == {"%c1": 2, "%c2": 2}, points
	# Live-pass agreement on the SAME un-seeded func (pipeline-faithful:
	# pass → arc — arc authors no last-use releases of its own).
	_run_pipeline(func, tt)
	agg = _fn_agg(out, "test::sg")
	assert agg.get("site_class:materialized_lastuse_release") == 2, agg
	assert agg.get("site_class:temp_lastuse_release", 0) == 0, agg


def test_tlr2a_ignore_axis_conformance(monkeypatch, tmp_path: Path) -> None:
	"""TLR-2a review finding 2: the IGNORE disposition axis, pinned on
	the shapes constructible in well-typed MIR — via CallIndirect, whose
	param types live on the instruction:
	- `%rp` — ConstString passed at a &String (REF) param: IGNORE;
	- `%np` — ConstString passed at a non-String by-value param: IGNORE;
	- `%mx` — the MIXED case the review called out: one IGNORE occurrence
	  (ref param) AND a later USE occurrence (StringEq).  The prescan
	  counts 2 but only the USE drains → the live pass NEVER releases;
	  the calculator must not invent a point (any non-USE occurrence
	  disqualifies).
	(The ctor/Exc non-selected-operand and ErrorRaise IGNOREs exist in
	the disposition table for totality but are unreachable for String
	operands in well-typed MIR — a String value cannot occupy a
	non-String field/slot — so they are not constructible here.)"""
	from lang.driftc.stage2.string_ownership_analysis import compute_lastuse_release_points
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	ref_string = tt.ensure_ref(string_ty)
	int_ty = tt.ensure_int()
	func = _make_func("ig", params=[], locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%rp", value="r"),
		M.ConstString(dest="%np", value="n"),
		M.ConstString(dest="%mx", value="m"),
		M.ConstString(dest="%u", value="u"),
		M.CallIndirect(dest=None, callee="%f1", args=["%rp"],
			param_types=[ref_string], user_ret_type=tt.ensure_void(), can_throw=False),
		M.CallIndirect(dest=None, callee="%f2", args=["%np"],
			param_types=[int_ty], user_ret_type=tt.ensure_void(), can_throw=False),
		M.CallIndirect(dest=None, callee="%f3", args=["%mx"],
			param_types=[ref_string], user_ret_type=tt.ensure_void(), can_throw=False),
		M.StringEq(dest="%e", left="%mx", right="%u"),  # USE of %mx after its IGNORE
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	lt = dict(func.local_types)
	lt.update({"%rp": string_ty, "%np": string_ty, "%mx": string_ty, "%u": string_ty})
	points = compute_lastuse_release_points(
		entry, local_types=lt, fn_infos={}, type_table=tt, live_out_names=set())
	# Only %u (pure USE occurrences) gets a point; %rp/%np/%mx have an
	# IGNORE occurrence → disqualified.
	assert points == {"%u": 7}, points
	# Live-pass agreement: exactly one materialized release (%u); none
	# for the IGNORE temps.
	for k, v in lt.items():
		func.local_types.setdefault(k, v)
	_run_pipeline(func, tt)
	agg = _fn_agg(out, "test::ig")
	assert agg.get("site_class:materialized_lastuse_release") == 1, agg
	assert agg.get("site_class:temp_lastuse_release", 0) == 0, agg
	out_instrs = func.blocks["entry"].instructions
	released = [i.value for i in out_instrs if type(i).__name__ == "StringRelease"]
	assert released == ["%u"], released


def test_tlr2a_semantic_string_param_conformance(monkeypatch, tmp_path: Path) -> None:
	"""TLR-2a review finding: call-param classification must use the
	SEMANTIC String test (TypeKind.SCALAR + name == "String"), mirroring
	the live arms' `_param_is_string` — raw TypeId equality is not
	reliable for String params at the package/type-table boundary (the
	string_stakes lesson).  Carrier: `new_scalar("String")` — a String
	param TypeId that is NOT `ensure_string()`.  Under raw-equality
	classification these by-value args would be IGNORE while the live
	arms CONSUME them.  IGNORE still disqualifies the temp from
	release-point output (no phantom release today) — the real risk is
	CONTRACT DRIFT: `string_operand_dispositions` would lie relative to
	the live arm and future users of the predicate would decide wrongly.
	Covers direct Call (fn_infos
	signature), CallIndirect, and CallIface (instruction-carried
	param_types); `%u` is the control proving real points still emit."""
	from lang.driftc.checker import FnInfo, FnSignature
	from lang.driftc.stage2.string_ownership_analysis import (
		DISPOSITION_CONSUME,
		compute_lastuse_release_points,
		string_operand_dispositions,
	)
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	str_alias = tt.new_scalar("String")  # semantically String, non-canonical
	assert str_alias != string_ty
	fid = FunctionId(module="m", name="takes", ordinal=0)
	fn_infos = {fid: FnInfo(
		fn_id=fid, name="takes", declared_can_throw=False,
		signature=FnSignature(
			name="takes", param_type_ids=[str_alias],
			return_type_id=tt.ensure_void()))}
	func = _make_func("sp", params=[], locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%d", value="d"),
		M.ConstString(dest="%i", value="i"),
		M.ConstString(dest="%f", value="f"),
		M.ConstString(dest="%u", value="u"),
		M.Call(dest=None, fn_id=fid, args=["%d"], can_throw=False),
		M.CallIndirect(dest=None, callee="%cb", args=["%i"],
			param_types=[str_alias], user_ret_type=tt.ensure_void(), can_throw=False),
		M.CallIface(dest=None, iface="%ifc", args=["%f"],
			param_types=[str_alias], user_ret_type=tt.ensure_void(),
			can_throw=False, slot_index=0),
		M.StringEq(dest="%e", left="%u", right="%u"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	lt = dict(func.local_types)
	lt.update({"%d": string_ty, "%i": string_ty, "%f": string_ty, "%u": string_ty})
	# Sharpest assertion first: the disposition table itself classifies
	# the semantically-String by-value arg as CONSUME on all three arms.
	for idx in (4, 5, 6):
		arg = entry.instructions[idx].args[0]
		disps = string_operand_dispositions(
			entry.instructions[idx],
			local_types=lt, fn_infos=fn_infos, type_table=tt)
		assert (arg, DISPOSITION_CONSUME) in disps, (idx, disps)
	points = compute_lastuse_release_points(
		entry, local_types=lt, fn_infos=fn_infos, type_table=tt, live_out_names=set())
	# Only the control gets a point; no phantom releases after the
	# consumed call args.
	assert points == {"%u": 7}, points
	# Live-pass agreement: consumed args are moved, never released.
	for k, v in lt.items():
		func.local_types.setdefault(k, v)
	_run_pipeline(func, tt, fn_infos)
	out_instrs = func.blocks["entry"].instructions
	released = [i.value for i in out_instrs if type(i).__name__ == "StringRelease"]
	assert released == ["%u"], released
	agg = _fn_agg(out, "test::sp")
	assert agg.get("site_class:materialized_lastuse_release") == 1, agg
	assert agg.get("site_class:temp_lastuse_release", 0) == 0, agg


def test_tlr2b_pass_plus_arc_equals_arc_only(monkeypatch, tmp_path: Path) -> None:
	"""TLR-2b family pin (single-config since the release-arm tripwire
	retired the arc-only leg): the materialization pass + recognition
	must produce the expected releases and audit counters —
	`materialized_lastuse_release` is noted at the recognition arm as
	the pre-materialized releases are copied through.  Shapes: qualified
	temp; same-instruction TWO-temp group (drain-order rule);
	repeated-operand temp (ONE release); consumed single-use ConstString
	(no release); Concat-produced temp (IN the family since TLR-3)."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=["x"], types={"x": string_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstString(dest="%q", value="q"),
			M.ConstString(dest="%p", value="p"),
			M.ConstString(dest="%r", value="r"),
			M.ConstString(dest="%c", value="c"),
			M.ConstString(dest="%u1", value="u1"),
			M.ConstString(dest="%u2", value="u2"),
			M.StringEq(dest="%e1", left="%q", right="%q"),   # %q drains (repeat → ONE release)
			M.StringEq(dest="%e2", left="%p", right="%r"),   # %p,%r drain together (order rule)
			M.StringConcat(dest="%cc", left="%u1", right="%u2"),  # %u1,%u2 drain; %cc = Concat temp
			M.StringEq(dest="%e3", left="%cc", right="%cc"), # %cc drains → materialized (family since TLR-3)
			M.StoreLocal(local="x", value="%c"),             # %c CONSUMED → no release ever
		]
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		for t in ("%q", "%p", "%r", "%c", "%u1", "%u2", "%cc"):
			func.local_types[t] = string_ty
		return func

	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	fb = build("ab_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::ab_b")
	# The family: %q (1), %p + %r (2), %u1 + %u2 (2), %cc (Concat, TLR-3)
	# → 6 materialized; %c consumed → nothing.
	assert agg_b.get("site_class:materialized_lastuse_release") == 6, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr2b_pass_is_idempotent(monkeypatch, tmp_path: Path) -> None:
	"""Second run recognizes its own in-contract output and computes no
	new points — no duplicate releases, returns False."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("idem", params=[], locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%a", value="a"),
		M.ConstString(dest="%b", value="b"),
		M.StringConcat(dest="%cc", left="%a", right="%b"),  # Concat in family (TLR-3)
		M.StringEq(dest="%e", left="%cc", right="%cc"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	assert materialize_lastuse_releases(func, type_table=tt, fn_infos={}) is True
	once = list(func.blocks["entry"].instructions)
	assert [type(i).__name__ for i in once] == [
		"ConstString", "ConstString", "StringConcat", "StringRelease",
		"StringRelease", "StringEq", "StringRelease",
	], once
	assert materialize_lastuse_releases(func, type_table=tt, fn_infos={}) is False
	assert func.blocks["entry"].instructions == once


def test_tlr2b_out_of_contract_input_release_trips_string_arc(monkeypatch, tmp_path: Path) -> None:
	"""The handshake is a verified contract, not trust: insert_string_arc
	itself (via the shared analysis in its prescan) fails closed on
	out-of-contract input releases.  Carriers:
	- SHAPE: a release of a StringRetain-produced temp at the correct
	  position (StringRetain is NOT in the family — owned, but a retain
	  wrap, not a materialization boundary; the shape carrier has
	  migrated three times as the family grew: Concat joined in TLR-3,
	  StringFromInt in TLR-5, CopyValue in TLR-6);
	- PLACEMENT, misplaced: Concat / StringFromInt (TLR-5) / CopyValue
	  (TLR-6) temp releases BEFORE a later use;
	- PLACEMENT, duplicated: two releases of one Concat / StringFromInt
	  (TLR-5) / CopyValue (TLR-6) temp."""
	import pytest
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def _run(name, instrs, temps):
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = instrs
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		for t in temps:
			func.local_types[t] = string_ty
		_attach_ledger(func)
		with pytest.raises(AssertionError, match="unexpected input release"):
			insert_string_arc(func, type_table=tt, fn_infos={})

	_run("oc_shape", [
		M.ConstString(dest="%a", value="a"),
		M.StringRetain(dest="%rt", value="%a"),
		M.StringEq(dest="%e", left="%rt", right="%rt"),
		M.StringRelease(value="%rt"),  # correct position, WRONG family/shape
	], ("%a", "%rt"))
	_run("oc_cv_misplaced", [
		M.ConstString(dest="%a", value="a"),
		M.CopyValue(dest="%cv", value="%a", ty=string_ty),
		M.StringRelease(value="%cv"),  # BEFORE %cv's real last use
		M.StringEq(dest="%e", left="%cv", right="%cv"),
	], ("%a", "%cv"))
	_run("oc_cv_duplicated", [
		M.ConstString(dest="%a", value="a"),
		M.CopyValue(dest="%cv", value="%a", ty=string_ty),
		M.StringEq(dest="%e", left="%cv", right="%cv"),
		M.StringRelease(value="%cv"),
		M.StringRelease(value="%cv"),  # duplicate
	], ("%a", "%cv"))
	_run("oc_sf_misplaced", [
		M.ConstInt(dest="%n", value=1),
		M.StringFromInt(dest="%sf", value="%n"),
		M.StringRelease(value="%sf"),  # BEFORE %sf's real last use
		M.StringEq(dest="%e", left="%sf", right="%sf"),
	], ("%sf",))
	_run("oc_sf_duplicated", [
		M.ConstInt(dest="%n", value=1),
		M.StringFromInt(dest="%sf", value="%n"),
		M.StringEq(dest="%e", left="%sf", right="%sf"),
		M.StringRelease(value="%sf"),
		M.StringRelease(value="%sf"),  # duplicate
	], ("%sf",))
	_run("oc_misplaced", [
		M.ConstString(dest="%a", value="a"),
		M.ConstString(dest="%b", value="b"),
		M.StringConcat(dest="%cc", left="%a", right="%b"),
		M.StringRelease(value="%cc"),  # BEFORE %cc's real last use
		M.StringEq(dest="%e", left="%cc", right="%cc"),
	], ("%a", "%b", "%cc"))
	_run("oc_duplicated", [
		M.ConstString(dest="%a", value="a"),
		M.ConstString(dest="%b", value="b"),
		M.StringConcat(dest="%cc", left="%a", right="%b"),
		M.StringEq(dest="%e", left="%cc", right="%cc"),
		M.StringRelease(value="%cc"),
		M.StringRelease(value="%cc"),  # duplicate
	], ("%a", "%b", "%cc"))


def test_tlr2b_cross_block_temp_untouched(monkeypatch, tmp_path: Path) -> None:
	"""TLR-7 FLIP (carrier preserved): a cross-block ConstString temp is
	IN the family now — fn-wide producer resolution qualifies it and the
	pass places the release in the DRAIN block and it classifies
	materialized at the recognition arm."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [M.ConstString(dest="%x", value="x")]
		entry.terminator = M.Goto(target="next")
		nxt = M.BasicBlock(name="next")
		nxt.instructions = [M.StringEq(dest="%e", left="%x", right="%x")]
		nxt.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "next": nxt}
		func.entry = "entry"
		func.local_types["%x"] = string_ty
		return func

	fb = build("xb_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel_next = [i.value for i in fb.blocks["next"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel_next == ["%x"], rel_next  # release in the DRAIN block
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::xb_b")
	assert agg_b.get("site_class:materialized_lastuse_release") == 1, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr3_concat_chain_ab_byte_identity(monkeypatch, tmp_path: Path) -> None:
	"""TLR-3 chain pin (single-config since the release-arm tripwire
	retired the arc-only leg): `a + b + c` — two Concats — with the
	CROSS-FAMILY same-drain group the design called out: the second
	Concat drains `%c1` (Concat temp) AND `%d` (ConstString temp)
	together; releases are consecutive in drain order (`%c1` then `%d`,
	last-occurrence positions in the draining instruction's operand
	walk).  All five releases materialize via the pass +
	recognition."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstString(dest="%a", value="a"),
			M.ConstString(dest="%b", value="b"),
			M.ConstString(dest="%d", value="d"),
			M.StringConcat(dest="%c1", left="%a", right="%b"),   # drains %a, %b
			M.StringConcat(dest="%c2", left="%c1", right="%d"),  # drains %c1 (Concat) + %d (ConstString)
			M.StringEq(dest="%e", left="%c2", right="%c2"),      # drains %c2
		]
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		for t in ("%a", "%b", "%d", "%c1", "%c2"):
			func.local_types[t] = string_ty
		return func

	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	fb = build("ch_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	# Pass output layout: cross-family group order %c1 then %d.
	names = [(type(i).__name__, getattr(i, "dest", None) or getattr(i, "value", None))
		for i in fb.blocks["entry"].instructions]
	assert names == [
		("ConstString", "%a"), ("ConstString", "%b"), ("ConstString", "%d"),
		("StringConcat", "%c1"), ("StringRelease", "%a"), ("StringRelease", "%b"),
		("StringConcat", "%c2"), ("StringRelease", "%c1"), ("StringRelease", "%d"),
		("StringEq", "%e"), ("StringRelease", "%c2"),
	], names
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::ch_b")
	assert agg_b.get("site_class:materialized_lastuse_release") == 5, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr3_multiuse_and_consumed_concat(monkeypatch, tmp_path: Path) -> None:
	"""TLR-3: a multi-use Concat result releases EXACTLY ONCE, after its
	LAST use; a consumed Concat result (stored) emits NO last-use release
	from either author (string_arc moves it — the store path unchanged)."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=["x"], types={"x": string_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstString(dest="%a", value="a"),
			M.ConstString(dest="%b", value="b"),
			M.StringConcat(dest="%mu", left="%a", right="%b"),  # multi-use concat
			M.ConstString(dest="%c", value="c"),
			M.ConstString(dest="%dd", value="d"),
			M.StringConcat(dest="%cs", left="%c", right="%dd"), # consumed concat
			M.StringEq(dest="%e1", left="%mu", right="%mu"),    # use 1 of %mu
			M.StringEq(dest="%e2", left="%mu", right="%mu"),    # use 2 (LAST) of %mu
			M.StoreLocal(local="x", value="%cs"),               # consuming use of %cs
		]
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		for t in ("%a", "%b", "%mu", "%c", "%dd", "%cs"):
			func.local_types[t] = string_ty
		return func

	fb = build("mc_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel = [i.value for i in fb.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	# %mu exactly ONCE; %cs never; operand temps once each.
	assert rel.count("%mu") == 1 and "%cs" not in rel, rel
	# %mu's release sits after the SECOND StringEq.
	instrs = fb.blocks["entry"].instructions
	e2_idx = next(i for i, ins in enumerate(instrs)
		if type(ins).__name__ == "StringEq" and ins.dest == "%e2")
	assert type(instrs[e2_idx + 1]).__name__ == "StringRelease"
	assert instrs[e2_idx + 1].value == "%mu", instrs
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::mc_b")
	# %a, %b, %c, %dd, %mu materialized (5); %cs consumed → none.
	assert agg_b.get("site_class:materialized_lastuse_release") == 5, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr3_cross_block_concat_untouched(monkeypatch, tmp_path: Path) -> None:
	"""TLR-7 FLIP (carrier preserved): the cross-block Concat temp is IN
	the family — fn-wide producer resolution; release in the drain
	block; all three temps materialize."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstString(dest="%a", value="a"),
			M.ConstString(dest="%b", value="b"),
			M.StringConcat(dest="%cc", left="%a", right="%b"),
		]
		entry.terminator = M.Goto(target="next")
		nxt = M.BasicBlock(name="next")
		nxt.instructions = [M.StringEq(dest="%e", left="%cc", right="%cc")]
		nxt.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "next": nxt}
		func.entry = "entry"
		for t in ("%a", "%b", "%cc"):
			func.local_types[t] = string_ty
		return func

	fb = build("xc_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel = [i.value for i in fb.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	assert set(rel) == {"%a", "%b"}, rel
	rel_next = [i.value for i in fb.blocks["next"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel_next == ["%cc"], rel_next  # TLR-7: drain-block release
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::xc_b")
	# %a, %b AND %cc (cross-block, TLR-7) all materialized.
	assert agg_b.get("site_class:materialized_lastuse_release") == 3, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr4_call_family_ab_semantic_and_idempotence(monkeypatch, tmp_path: Path) -> None:
	"""TLR-4 Call family pin (single-config since the release-arm
	tripwire retired the arc-only leg): qualified call-result temps —
	fn_infos-proven with a NON-CANONICAL semantic-String return TypeId
	(`new_scalar("String")`, the finding-5 carrier), a drift_string_*
	helper-symbol call, a multi-use call result (ONE release, after the
	LAST use), and a consumed call result (no release from either
	author).  The pass is idempotent with Call temps in the family."""
	from lang.driftc.checker import FnInfo, FnSignature
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	str_alias = tt.new_scalar("String")  # semantically String, non-canonical
	assert str_alias != string_ty
	fid = FunctionId(module="m", name="mk", ordinal=0)
	fn_infos = {fid: FnInfo(
		fn_id=fid, name="mk", declared_can_throw=False,
		signature=FnSignature(name="mk", return_type_id=str_alias))}
	helper_fid = FunctionId(module="main", name="drift_string_concat", ordinal=0)

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=["x"], types={"x": string_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.Call(dest="%qc", fn_id=fid, args=[], can_throw=False),   # semantic-proven
			M.Call(dest="%hc", fn_id=helper_fid, args=[], can_throw=False),  # helper-symbol-proven
			M.Call(dest="%mu", fn_id=fid, args=[], can_throw=False),   # multi-use
			M.Call(dest="%cs", fn_id=fid, args=[], can_throw=False),   # consumed
			M.StringEq(dest="%e1", left="%qc", right="%hc"),   # drains %qc, %hc
			M.StringEq(dest="%e2", left="%mu", right="%mu"),   # use 1 of %mu
			M.StringEq(dest="%e3", left="%mu", right="%mu"),   # use 2 (LAST) of %mu
			M.StoreLocal(local="x", value="%cs"),              # consumes %cs
		]
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		for t in ("%qc", "%hc", "%mu", "%cs"):
			func.local_types[t] = string_ty
		return func

	fb = build("c4_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos=fn_infos) is True
	rel = [i.value for i in fb.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel.count("%mu") == 1 and "%cs" not in rel, rel
	assert set(rel) == {"%qc", "%hc", "%mu"}, rel
	once = list(fb.blocks["entry"].instructions)
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos=fn_infos) is False
	assert fb.blocks["entry"].instructions == once
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos=fn_infos)
	agg_b = _fn_agg(out, "test::c4_b")
	assert agg_b.get("site_class:materialized_lastuse_release") == 3, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr4_nonfamily_calls_stay_out(monkeypatch, tmp_path: Path) -> None:
	"""TLR-4 conservative-exclusion pins — all stay OUT of the family:
	- `%th` — can_throw=True Call with a (synthetically) String-typed
	  dest: the fail-closed guard for the structurally-unreachable case
	  (real can-throw dests are FnResult envelopes);
	- `%ni` — info-less Call with a String-typed dest: unproven →
	  conservatively out (pinned so a metadata regression cannot
	  silently widen the family);
	- `%xb` — cross-block call result: since TLR-7 fn-wide resolution
	  this one IS family (proven non-throw Call) and MATERIALIZES in its
	  drain block — kept here as the flip's record;
	- `%ti` — CallIndirect with can_throw=True and semantic-String
	  user_ret_type: throw guard wins.
	Since the tripwire-deletion slice (2026-07-18) string_arc authors
	no last-use releases: the stay-out temps drain with NO release at
	all — the pass's exclusion (only %xb materialized) IS the surviving
	contract, and arc must copy exactly that through, adding nothing.
	(This pin was the non-family tripwire carrier while the arm was
	fail-closed, 2026-07-16..18.)"""
	from lang.driftc.checker import FnInfo, FnSignature
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	fid = FunctionId(module="m", name="mk", ordinal=0)
	fn_infos = {fid: FnInfo(
		fn_id=fid, name="mk", declared_can_throw=False,
		signature=FnSignature(name="mk", return_type_id=string_ty))}
	noinfo_fid = FunctionId(module="m", name="mystery", ordinal=0)

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.Call(dest="%th", fn_id=fid, args=[], can_throw=True),
			M.Call(dest="%ni", fn_id=noinfo_fid, args=[], can_throw=False),
			M.Call(dest="%xb", fn_id=fid, args=[], can_throw=False),
			M.CallIndirect(dest="%ti", callee="%f", args=[],
				param_types=[], user_ret_type=string_ty, can_throw=True),
			M.StringEq(dest="%e1", left="%th", right="%ni"),
			M.StringEq(dest="%e2", left="%ti", right="%ti"),
		]
		entry.terminator = M.Goto(target="next")
		nxt = M.BasicBlock(name="next")
		nxt.instructions = [M.StringEq(dest="%e3", left="%xb", right="%xb")]
		nxt.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "next": nxt}
		func.entry = "entry"
		for t in ("%th", "%ni", "%xb", "%ti"):
			func.local_types[t] = string_ty
		return func

	fb = build("nf_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos=fn_infos) is True
	rel_next = [i.value for i in fb.blocks["next"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel_next == ["%xb"], rel_next  # only the family member
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos=fn_infos)
	# Arc adds NOTHING: the family member's pass-authored release is
	# copied through; the stay-out temps get no release from anywhere
	# (string_arc authors no last-use releases since 2026-07-18).
	rel_all = [i.value for b in fb.blocks.values() for i in b.instructions
		if type(i).__name__ == "StringRelease"]
	assert rel_all.count("%xb") == 1, rel_all
	assert not any(v in rel_all for v in ("%th", "%ni", "%ti")), rel_all
	agg = _fn_agg(out, "test::nf_b")
	assert agg.get("site_class:materialized_lastuse_release") == 1, agg
	assert agg.get("site_class:temp_lastuse_release", 0) == 0, agg


def test_tlr4_indirect_iface_user_ret_type_family(monkeypatch, tmp_path: Path) -> None:
	"""TLR-4: CallIndirect/CallIface results join the family via the
	instruction-carried semantic-String `user_ret_type` when not
	can_throw (population 0 in the corpus — future-proofing with an
	instruction-local proof)."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	str_alias = tt.new_scalar("String")

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.CallIndirect(dest="%ci", callee="%f", args=[],
				param_types=[], user_ret_type=str_alias, can_throw=False),
			M.CallIface(dest="%cf", iface="%ifc", args=[],
				param_types=[], user_ret_type=string_ty,
				can_throw=False, slot_index=0),
			M.StringEq(dest="%e1", left="%ci", right="%cf"),  # drains both
		]
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		for t in ("%ci", "%cf"):
			func.local_types[t] = string_ty
		return func

	fb = build("ii_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel = [i.value for i in fb.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	assert set(rel) == {"%ci", "%cf"}, rel
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::ii_b")
	assert agg_b.get("site_class:materialized_lastuse_release") == 2, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr4_out_of_contract_call_release_trips(monkeypatch, tmp_path: Path) -> None:
	"""TLR-4: misplaced/duplicated releases of a FAMILY call temp still
	fail closed through insert_string_arc's prescan."""
	import pytest
	from lang.driftc.checker import FnInfo, FnSignature
	tt = TypeTable()
	string_ty = tt.ensure_string()
	fid = FunctionId(module="m", name="mk", ordinal=0)
	fn_infos = {fid: FnInfo(
		fn_id=fid, name="mk", declared_can_throw=False,
		signature=FnSignature(name="mk", return_type_id=string_ty))}

	def _run(name, instrs):
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = instrs
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		func.local_types["%qc"] = string_ty
		_attach_ledger(func)
		with pytest.raises(AssertionError, match="unexpected input release"):
			insert_string_arc(func, type_table=tt, fn_infos=fn_infos)

	_run("c4_mis", [
		M.Call(dest="%qc", fn_id=fid, args=[], can_throw=False),
		M.StringRelease(value="%qc"),  # BEFORE the real last use
		M.StringEq(dest="%e", left="%qc", right="%qc"),
	])
	_run("c4_dup", [
		M.Call(dest="%qc", fn_id=fid, args=[], can_throw=False),
		M.StringEq(dest="%e", left="%qc", right="%qc"),
		M.StringRelease(value="%qc"),
		M.StringRelease(value="%qc"),  # duplicate
	])


def test_tlr5_stringfrom_and_exc_family(monkeypatch, tmp_path: Path) -> None:
	"""TLR-5 family pin (single-config since the release-arm tripwire
	retired the arc-only leg): all four StringFrom* kinds AND both Exc*
	kinds are
	unconditional family members — qualified temps materialize; a
	multi-use StringFrom* temp releases EXACTLY ONCE (after the LAST
	use); a consumed one emits nothing from either author; pass
	idempotent with the extended family."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=["x"], types={"x": string_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstInt(dest="%n", value=1),
			M.ConstBool(dest="%b", value=True),
			M.ConstUint(dest="%u", value=2),
			M.ConstFloat(dest="%f", value=3.5),
			M.StringFromInt(dest="%si", value="%n"),
			M.StringFromBool(dest="%sb", value="%b"),
			M.StringFromUint(dest="%su", value="%u"),
			M.StringFromFloat(dest="%sf", value="%f"),
			M.ExcGetParamsJson(dest="%ep", error="%err"),
			M.ExcGetContextJson(dest="%ec", error="%err"),
			M.StringFromInt(dest="%mu", value="%n"),   # multi-use
			M.StringFromInt(dest="%cs", value="%n"),   # consumed
			M.StringEq(dest="%e1", left="%si", right="%sb"),   # drains %si, %sb
			M.StringEq(dest="%e2", left="%su", right="%sf"),   # drains %su, %sf
			M.StringEq(dest="%e3", left="%ep", right="%ec"),   # drains %ep, %ec
			M.StringEq(dest="%e4", left="%mu", right="%mu"),   # use 1 of %mu
			M.StringEq(dest="%e5", left="%mu", right="%mu"),   # use 2 (LAST)
			M.StoreLocal(local="x", value="%cs"),              # consumes %cs
		]
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		for t in ("%si", "%sb", "%su", "%sf", "%ep", "%ec", "%mu", "%cs"):
			func.local_types[t] = string_ty
		return func

	fb = build("t5_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel = [i.value for i in fb.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel.count("%mu") == 1 and "%cs" not in rel, rel
	assert set(rel) == {"%si", "%sb", "%su", "%sf", "%ep", "%ec", "%mu"}, rel
	once = list(fb.blocks["entry"].instructions)
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is False
	assert fb.blocks["entry"].instructions == once
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::t5_b")
	# %si/%sb/%su/%sf/%ep/%ec/%mu → 7 materialized; %cs consumed → none.
	assert agg_b.get("site_class:materialized_lastuse_release") == 7, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr5_cross_block_stringfrom_untouched(monkeypatch, tmp_path: Path) -> None:
	"""TLR-7 FLIP (carrier preserved): the cross-block StringFrom* temp
	is IN the family — fn-wide producer resolution; drain-block release;
	materialized in both configs."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstInt(dest="%n", value=1),
			M.StringFromInt(dest="%sf", value="%n"),
		]
		entry.terminator = M.Goto(target="next")
		nxt = M.BasicBlock(name="next")
		nxt.instructions = [M.StringEq(dest="%e", left="%sf", right="%sf")]
		nxt.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "next": nxt}
		func.entry = "entry"
		func.local_types["%sf"] = string_ty
		return func

	fb = build("t5x_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel_next = [i.value for i in fb.blocks["next"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel_next == ["%sf"], rel_next
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::t5x_b")
	assert agg_b.get("site_class:materialized_lastuse_release") == 1, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


# test_tlr6_copyvalue_guard_teeth retired with the tripwire-deletion
# slice (2026-07-18): its subject — the CopyValue rewrite-loop
# `recognized_released` re-add guard — was deleted together with the
# release arm it protected (a re-owned recognized temp is inert
# block-local bookkeeping once no arm consumes that state).  The
# exactly-one-release output contract stays covered by
# test_tlr6_copyvalue_family and the CopyValue memcheck row.


def test_tlr6_copyvalue_family(monkeypatch, tmp_path: Path) -> None:
	"""TLR-6 family pin (single-config since the release-arm tripwire
	retired the arc-only leg): CopyValue temps (the view-source copy
	shape — the
	measured population is arr[i] value/field reads) are family members:
	qualified copies materialize; a multi-use copy releases EXACTLY ONCE
	after the LAST use; a consumed copy emits nothing from either
	author; pass idempotent."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=["src"], locals_=["x"],
			types={"src": string_ty, "x": string_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.LoadLocal(dest="%v", local="src"),               # borrowed view
			M.CopyValue(dest="%q", value="%v", ty=string_ty),  # qualified copy
			M.CopyValue(dest="%mu", value="%v", ty=string_ty), # multi-use copy
			M.CopyValue(dest="%cs", value="%v", ty=string_ty), # consumed copy
			M.StringEq(dest="%e1", left="%q", right="%q"),     # drains %q
			M.StringEq(dest="%e2", left="%mu", right="%mu"),   # use 1 of %mu
			M.StringEq(dest="%e3", left="%mu", right="%mu"),   # use 2 (LAST)
			M.StoreLocal(local="x", value="%cs"),              # consumes %cs
		]
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		for t in ("%v", "%q", "%mu", "%cs"):
			func.local_types[t] = string_ty
		return func

	fb = build("t6_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel = [i.value for i in fb.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel.count("%mu") == 1 and "%cs" not in rel, rel
	assert set(rel) == {"%q", "%mu"}, rel
	once = list(fb.blocks["entry"].instructions)
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is False
	assert fb.blocks["entry"].instructions == once
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::t6_b")
	assert agg_b.get("site_class:materialized_lastuse_release") == 2, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr6_cross_block_copyvalue_untouched(monkeypatch, tmp_path: Path) -> None:
	"""TLR-7 FLIP (carrier preserved): the cross-block CopyValue temp is
	IN the family — fn-wide producer resolution; drain-block release;
	materialized in both configs."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=["src"], locals_=[], types={"src": string_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.LoadLocal(dest="%v", local="src"),
			M.CopyValue(dest="%cv", value="%v", ty=string_ty),
		]
		entry.terminator = M.Goto(target="next")
		nxt = M.BasicBlock(name="next")
		nxt.instructions = [M.StringEq(dest="%e", left="%cv", right="%cv")]
		nxt.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "next": nxt}
		func.entry = "entry"
		for t in ("%v", "%cv"):
			func.local_types[t] = string_ty
		return func

	fb = build("t6x_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel_next = [i.value for i in fb.blocks["next"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel_next == ["%cv"], rel_next
	# Config-A (arc-only) retired with the release-arm tripwire: the
	# in-pass author is gone, so pass-output + recognition-counter
	# assertions below are the surviving contract.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::t6x_b")
	assert agg_b.get("site_class:materialized_lastuse_release") == 1, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr7_cfg_shapes_ab(monkeypatch, tmp_path: Path) -> None:
	"""TLR-7 CFG-shape pins, one func per shape (single-config since the
	release-arm tripwire retired the arc-only leg) — the pass output
	layout and recognition counters are the contract:
	- BRANCH JOIN: temp used ONLY at the join → single release there
	  (liveness keeps it live through both arms — no per-arm releases);
	- PATH-EXCLUSIVE DUAL DRAINS: temp used in BOTH arms, dead at the
	  join → one release point PER ARM; no execution path passes two
	  (the §3a path-exclusivity proof's teeth);
	- BYPASS PATH (blocking review addition — the §3c contract's teeth):
	  temp used/drained in ONE arm only, the other arm bypasses all
	  uses, dead at join → release ONLY in the use arm, NONE in the
	  bypass arm or at the join; TLR-7 mirrors today's drain points and
	  does not "fix" bypasses."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	bool_ty = tt.ensure_bool()

	def _diamond(name, then_instrs, else_instrs, join_instrs, temps):
		func = _make_func(name, params=["c"], locals_=[], types={"c": bool_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstString(dest="%x", value="x"),
			M.LoadLocal(dest="%cv", local="c"),
		]
		entry.terminator = M.IfTerminator(cond="%cv", then_target="then", else_target="els")
		then_b = M.BasicBlock(name="then")
		then_b.instructions = list(then_instrs)
		then_b.terminator = M.Goto(target="join")
		els_b = M.BasicBlock(name="els")
		els_b.instructions = list(else_instrs)
		els_b.terminator = M.Goto(target="join")
		join_b = M.BasicBlock(name="join")
		join_b.instructions = list(join_instrs)
		join_b.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "then": then_b, "els": els_b, "join": join_b}
		func.entry = "entry"
		for t in temps:
			func.local_types[t] = string_ty
		return func

	def _ab(name, then_i, else_i, join_i, temps, want):
		fb = _diamond(name + "_b", then_i, else_i, join_i, temps)
		assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
		got = {bn: [i.value for i in fb.blocks[bn].instructions
			if type(i).__name__ == "StringRelease"] for bn in fb.blocks}
		assert got == want, (name, got)
		# Config-A retired with the release-arm tripwire; the pass-output
		# `want` layout above plus recognition counters are the contract.
		_attach_ledger(fb)
		insert_string_arc(fb, type_table=tt, fn_infos={})
		agg_b = _fn_agg(out, f"test::{name}_b")
		assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, (name, agg_b)

	# BRANCH JOIN: %x used only at the join.
	_ab("j7",
		[], [],
		[M.StringEq(dest="%e", left="%x", right="%x")],
		("%x",),
		{"entry": [], "then": [], "els": [], "join": ["%x"]})
	# PATH-EXCLUSIVE DUAL DRAINS: %x used in both arms, dead at join.
	_ab("d7",
		[M.StringEq(dest="%e1", left="%x", right="%x")],
		[M.StringEq(dest="%e2", left="%x", right="%x")],
		[],
		("%x",),
		{"entry": [], "then": ["%x"], "els": ["%x"], "join": []})
	# BYPASS: %x used in then only; els bypasses; dead at join.
	_ab("b7",
		[M.StringEq(dest="%e1", left="%x", right="%x")],
		[],
		[],
		("%x",),
		{"entry": [], "then": ["%x"], "els": [], "join": []})


def test_tlr7_loop_backedge_ab(monkeypatch, tmp_path: Path) -> None:
	"""TLR-7 loop/backedge pins (review requirement):
	- POSITIVE (the measured 7,392 shape): a Concat temp produced in one
	  loop-body block and drained in a LATER body block, fresh each
	  iteration → release inside the iteration, in the drain block;
	- NEGATIVE control: a temp used again NEXT iteration (live through
	  the backedge) → NO release anywhere inside the loop from either
	  author (the §3a proof relies on live_out seeing backedge uses)."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	bool_ty = tt.ensure_bool()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=["c"], locals_=[], types={"c": bool_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstString(dest="%seed", value="s"),  # NEGATIVE: crosses the backedge
		]
		entry.terminator = M.Goto(target="head")
		head = M.BasicBlock(name="head")
		head.instructions = [
			M.ConstString(dest="%a", value="a"),
			M.StringConcat(dest="%it", left="%a", right="%seed"),  # per-iteration Concat
			M.LoadLocal(dest="%cv", local="c"),
		]
		head.terminator = M.IfTerminator(cond="%cv", then_target="body2", else_target="exit")
		body2 = M.BasicBlock(name="body2")
		body2.instructions = [
			M.StringEq(dest="%e", left="%it", right="%it"),  # drain of %it (cross-block, intra-loop)
		]
		body2.terminator = M.Goto(target="head")  # backedge: %seed stays live
		exit_b = M.BasicBlock(name="exit")
		exit_b.instructions = [
			M.StringEq(dest="%e2", left="%seed", right="%seed"),  # %seed drains after the loop
		]
		exit_b.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "head": head, "body2": body2, "exit": exit_b}
		func.entry = "entry"
		for t in ("%seed", "%a", "%it"):
			func.local_types[t] = string_ty
		return func

	fb = build("lp_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	got = {bn: [i.value for i in fb.blocks[bn].instructions
		if type(i).__name__ == "StringRelease"] for bn in fb.blocks}
	# POSITIVE: %it released in body2 (its drain block, inside the
	# iteration); %a released in head (drains at the Concat).
	# NEGATIVE: NO release of %seed inside the loop (live through the
	# backedge); it drains in exit.
	assert got == {"entry": [], "head": ["%a"], "body2": ["%it"],
		"exit": ["%seed"]}, got
	# Config-A retired with the release-arm tripwire.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	agg_b = _fn_agg(out, "test::lp_b")
	assert agg_b.get("site_class:materialized_lastuse_release") == 3, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr7_consumed_and_terminator_cases(monkeypatch, tmp_path: Path) -> None:
	"""TLR-7: consumed-before-exit — a cross-block temp CONSUMED (stored)
	in a later block gets NO release from either author; and
	live-out-to-terminator — a cross-block temp last-used by a later
	block's NON-Return terminator gets its release at the END of that
	block's instruction list."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def build(name: str) -> M.MirFunc:
		func = _make_func(name, params=[], locals_=["x"], types={"x": string_ty})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [
			M.ConstString(dest="%cs", value="c"),   # consumed-before-exit carrier
			M.ConstString(dest="%tm", value="t"),   # terminator-use carrier
		]
		entry.terminator = M.Goto(target="mid")
		mid = M.BasicBlock(name="mid")
		mid.instructions = [
			M.StoreLocal(local="x", value="%cs"),   # consumes %cs cross-block
		]
		# NON-Return terminator using %tm (synthetic: exercises the
		# term_used → len(instructions) path).
		mid.terminator = M.IfTerminator(cond="%tm", then_target="exit", else_target="exit")
		exit_b = M.BasicBlock(name="exit")
		exit_b.instructions = []
		exit_b.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "mid": mid, "exit": exit_b}
		func.entry = "entry"
		for t in ("%cs", "%tm"):
			func.local_types[t] = string_ty
		return func

	fb = build("ct_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	got = {bn: [i.value for i in fb.blocks[bn].instructions
		if type(i).__name__ == "StringRelease"] for bn in fb.blocks}
	# %cs consumed → none; %tm terminator-drained → release at the END
	# of mid's instructions (after the StoreLocal).
	assert got == {"entry": [], "mid": ["%tm"], "exit": []}, got
	assert type(fb.blocks["mid"].instructions[-1]).__name__ == "StringRelease"
	# Config-A retired with the release-arm tripwire.
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})


def test_tlr7_cross_block_out_of_contract_and_dup_producer(monkeypatch, tmp_path: Path) -> None:
	"""TLR-7 fail-closed pins: a misplaced/duplicated CROSS-BLOCK release
	trips the recognition contract in the drain block; and a duplicate
	SSA dest trips the fn-wide producer builder."""
	import pytest
	from lang.driftc.stage2.string_ownership_analysis import build_fnwide_producers
	tt = TypeTable()
	string_ty = tt.ensure_string()

	def _run(name, mid_instrs):
		func = _make_func(name, params=[], locals_=[], types={})
		entry = M.BasicBlock(name="entry")
		entry.instructions = [M.ConstString(dest="%x", value="x")]
		entry.terminator = M.Goto(target="mid")
		mid = M.BasicBlock(name="mid")
		mid.instructions = mid_instrs
		mid.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "mid": mid}
		func.entry = "entry"
		func.local_types["%x"] = string_ty
		_attach_ledger(func)
		with pytest.raises(AssertionError, match="unexpected input release"):
			insert_string_arc(func, type_table=tt, fn_infos={})

	_run("x7_mis", [
		M.StringRelease(value="%x"),  # BEFORE the cross-block last use
		M.StringEq(dest="%e", left="%x", right="%x"),
	])
	_run("x7_dup", [
		M.StringEq(dest="%e", left="%x", right="%x"),
		M.StringRelease(value="%x"),
		M.StringRelease(value="%x"),  # duplicate
	])
	# Duplicate SSA dest → the producer builder fails closed.
	b1 = M.BasicBlock(name="b1")
	b1.instructions = [M.ConstString(dest="%d", value="a")]
	b2 = M.BasicBlock(name="b2")
	b2.instructions = [M.ConstString(dest="%d", value="b")]
	with pytest.raises(AssertionError, match="duplicate SSA dest"):
		build_fnwide_producers([b1, b2])


def test_tlr8_moveout_family(monkeypatch, tmp_path: Path) -> None:
	"""TLR-8 family pin: MoveOut temps are family members — the dest
	inherits the storage local's +1 stake verbatim (the expansion
	zero-stores the local), so a move dest draining non-consumingly gets
	its release materialized by the pass.  The motivating population is
	the drift-workflows release-arm tripwire firing (`"lit" + move s` —
	a moved String operand at a non-consuming concat;
	issues/string-arc-release-arm-tripwire/).  Qualified move
	materializes ONCE after the last use; a consumed move dest emits
	nothing from either author; pass idempotent."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("t8", params=["s", "t", "u"], locals_=["x", "y"],
		types={n: string_ty for n in ("s", "t", "u", "x", "y")})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.MoveOut(dest="%m", local="s", ty=string_ty),      # qualified: drains at the concat (USE)
		M.ConstString(dest="%lit", value="x: "),
		M.StringConcat(dest="%c", left="%lit", right="%m"), # the repro shape
		M.StoreLocal(local="x", value="%c"),                # consumes %c
		M.MoveOut(dest="%mu", local="t", ty=string_ty),     # multi-use move
		M.StringEq(dest="%e1", left="%mu", right="%mu"),
		M.StringEq(dest="%e2", left="%mu", right="%mu"),    # LAST use
		M.MoveOut(dest="%cs", local="u", ty=string_ty),     # consumed move
		M.StoreLocal(local="y", value="%cs"),               # consumes %cs
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	for t in ("%m", "%lit", "%c", "%mu", "%cs"):
		func.local_types[t] = string_ty
	assert materialize_lastuse_releases(func, type_table=tt, fn_infos={}) is True
	rel = [i.value for i in func.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel.count("%m") == 1 and rel.count("%mu") == 1, rel
	assert "%cs" not in rel and "%c" not in rel, rel
	assert set(rel) == {"%lit", "%m", "%mu"}, rel
	once = list(func.blocks["entry"].instructions)
	assert materialize_lastuse_releases(func, type_table=tt, fn_infos={}) is False
	assert func.blocks["entry"].instructions == once
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::t8")
	assert agg.get("site_class:materialized_lastuse_release") == 3, agg
	assert agg.get("site_class:temp_lastuse_release", 0) == 0, agg


# test_tlr8_moveout_guard_teeth retired with the tripwire-deletion
# slice (2026-07-18) — same rationale as the TLR-6 teeth retirement
# above (the MoveOut expansion arm's guard deleted with the release
# arm).  The production shape stays covered by test_tlr8_moveout_family,
# the end-to-end pin below, and memcheck
# test_move_operand_concat_release.py.


def test_tlr8_cross_block_moveout(monkeypatch, tmp_path: Path) -> None:
	"""TLR-8 × TLR-7: a cross-block MoveOut temp qualifies via fn-wide
	producer resolution; the release lands in the DRAIN block.  The
	producer-block expansion arm re-adds the dest to owned/move-only
	there (its per-block recognized set is empty) — harmless by
	construction: the temp has no producer-block uses, so `_note_use`
	never consults it before the drain block's subtraction suppresses
	it."""
	from lang.driftc.stage2.string_releases import materialize_lastuse_releases
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("t8x", params=["s"], locals_=[], types={"s": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.MoveOut(dest="%m", local="s", ty=string_ty)]
	entry.terminator = M.Goto(target="next")
	nxt = M.BasicBlock(name="next")
	nxt.instructions = [M.StringEq(dest="%e", left="%m", right="%m")]
	nxt.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "next": nxt}
	func.entry = "entry"
	func.local_types["%m"] = string_ty
	assert materialize_lastuse_releases(func, type_table=tt, fn_infos={}) is True
	rel_entry = [i.value for i in func.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	rel_next = [i.value for i in func.blocks["next"].instructions
		if type(i).__name__ == "StringRelease"]
	assert rel_entry == [] and rel_next == ["%m"], (rel_entry, rel_next)
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::t8x")
	assert agg.get("site_class:materialized_lastuse_release") == 1, agg
	assert agg.get("site_class:temp_lastuse_release", 0) == 0, agg


def test_tlr8_move_operand_concat_end_to_end(tmp_path: Path) -> None:
	"""TLR-8 end-to-end pin on the PRODUCTION shape (drift-workflows
	regression, issues/string-arc-release-arm-tripwire/): real source
	with a `move`d String operand in a concatenation must compile clean
	through the real driver pipeline — before TLR-8 the release-arm
	tripwire aborted the compile (family=False, producer=MoveOut)."""
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc import driftc as D
	from lang.driftc.core.function_id import function_symbol

	src = tmp_path / "main.drift"
	src.write_text(
		"module main;\n\n"
		"fn tag(s: String) nothrow -> String {\n"
		"\treturn \"x: \" + move s;\n"
		"}\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval t = tag(\"hello\");\n"
		"\tif t.len == 8 { return 0; }\n"
		"\treturn 1;\n}\n"
	)
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	origin = {}
	for m in modules.values():
		origin.update(m.origin_by_fn_id)
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
	assert not errors, [d.message for d in errors]
	assert ir


# The three release-arm tripwire pins (stale_family_temp /
# nonfamily_producer / driver_diagnostic) retired with the
# tripwire-deletion slice (2026-07-18): the arm and its tripwire were
# deleted after the clean 0.33.84 cert cycle (zero firings; the
# certified run also exercised the drift-workflows corpus that
# produced TLR-8).  Bare insert_string_arc on unmaterialized family
# MIR now silently under-releases instead of raising — the pipeline
# precondition lives in the string_arc module doc, and the driver
# wrap's containment contract stays pinned by
# test_string_arc_boundary_wrap_contains_assertions above.


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


def test_c3_paired_maybe_uninit_array_moveout_is_zero_safe() -> None:
	"""B-M pin (maintainer spec pin 3): a PAIRED (moveout_feeds_drop)
	MAYBE_UNINIT ARRAY MoveOut classifies `c3_moveout_zero_safe`
	through the PRODUCTION predicate (`zero_storage_drop_safe` — the
	Arm M authored-cleanup shape once arrays take unguarded
	authoring)."""
	from lang.driftc.stage2.drop_policy_compute import zero_storage_drop_safe
	tt = TypeTable()
	string_ty = tt.ensure_string()
	arr_ty = tt.new_array(string_ty)
	bool_ty = tt.ensure_bool()
	func = _make_func("zarr", params=["b"], locals_=["b", "a"],
		types={"b": bool_ty, "a": arr_ty})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="b", then_target="thn", else_target="join")
	thn = M.BasicBlock(name="thn")
	thn.instructions = [
		M.ArrayLit(dest="%t", elem_ty=string_ty, elements=[]),
		M.StoreLocal(local="a", value="%t"),
	]
	thn.terminator = M.Goto(target="join")
	join = M.BasicBlock(name="join")
	join.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "thn": thn, "join": join}
	func.entry = "entry"
	_attach_ledger(func)
	l_pre = getattr(func, "_ownership_ledger")

	def run(feeds_drop: bool) -> dict:
		audit = R.StringArcAudit("test::zarr")
		audit.note(
			R.STAKE_MOVEOUT_EXPANSION, "a", R.SITE_CLASS_MOVEOUT_EXPANSION,
			pre_point=("join", 0), post_point=("join", 0),
			moveout_feeds_drop=feeds_drop,
		)
		return audit.finalize(
			l_pre=l_pre, l_post=None, needs_drop=lambda _l: True,
			func=func, zero_safe_ty=lambda t: zero_storage_drop_safe(t, tt),
		)

	paired = run(True)
	assert paired.get(R.AGREE_C3_ZERO_SAFE, 0) == 1, paired
	assert paired.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 0, paired
	# Pin 4 (maintainer spec): the UNPAIRED Array MoveOut — a
	# store/call consumer of maybe-uninit array storage, the
	# value-corruption class — remains DIVERGENT and hard-gated; the
	# predicate cannot substitute for the drop pairing.
	unpaired = run(False)
	assert unpaired.get(R.DIV_C3_MOVEOUT_NOT_OWNED, 0) == 1, unpaired
	assert unpaired.get(R.AGREE_C3_ZERO_SAFE, 0) == 0, unpaired
