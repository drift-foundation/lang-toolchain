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
	above does not exercise it) AND of the Array release-elision fold:
	insert_string_arc over a func with real Array locals reaching the
	return-boundary sweep, audit env on.

	- `a_uninit` (never written → UNINIT) and `a_live` (zero-init-stored
	  → TOMBSTONED) have MUST_NOT_DROP boundary verdicts → their sweep
	  drops are ELIDED and nothing is recorded for them;
	- `sink` (holds a moved-in array → LIVE, MUST_DROP) keeps its sweep
	  drop and is the recorded row — the live-direction guard;
	- `a_moved` is moved out IN the return block, so string_arc's own
	  `moved_out_locals` fold puts it in skip_cleanup_locals → the sweep
	  skips it and it must NOT be recorded;
	- the OUTPUT-MIR ArrayDrop counts prove the elision in emission, not
	  just the audit view (drop-before-overwrite drops are out of the
	  elision's scope and remain)."""
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
	# ARRAY RELEASE ELISION (emission slice): MUST_NOT_DROP boundary
	# verdicts are now ELIDED from the sweep — a_uninit (UNINIT) and
	# a_live (zero-init-stored → TOMBSTONED) no longer emit or record a
	# drop.  Only `sink` (holds the moved-in array — LIVE, MUST_DROP)
	# is swept; a_moved stays skipped via the moved_out_locals fold.
	# This doubles as the LIVE-direction pin: a live array's sweep drop
	# must never be elided.
	assert agg.get("site_class:scope_exit_arraydrop") == 1, agg
	assert agg.get("arraydrop_state:live") == 1, agg
	assert agg.get("arraydrop_verdict:must_drop") == 1, agg
	assert agg.get("arraydrop_state:uninit") is None, agg
	assert agg.get("arraydrop_state:tombstoned") is None, agg
	assert agg.get("arraydrop_state:moved_out") is None, agg
	assert agg.get("arraydrop_verdict:must_not_drop") is None, agg
	# The elision is real in the OUTPUT MIR, not just the audit view.
	# Count ArrayDrops per SOURCE local (via the LoadLocal feeding each
	# drop).  a_live/a_moved keep exactly ONE drop each — the
	# drop-before-overwrite emitted at their StoreLocal, which is OUT of
	# this slice's scope — but their RETURN-SWEEP drop is gone (pre-
	# elision a_live had 2).  sink keeps its sweep drop (LIVE);
	# a_uninit has none at all.
	loaded_by = {}
	drop_counts: dict = {}
	for blk in func.blocks.values():
		for ins in blk.instructions:
			if type(ins).__name__ == "LoadLocal":
				loaded_by[ins.dest] = ins.local
			elif type(ins).__name__ == "ArrayDrop":
				src = loaded_by.get(getattr(ins, "array", None))
				drop_counts[src] = drop_counts.get(src, 0) + 1
	# sink = 2: its own StoreLocal's drop-before-overwrite + the KEPT
	# sweep drop (LIVE at the boundary).
	assert drop_counts.get("sink", 0) == 2, drop_counts
	assert drop_counts.get("a_uninit", 0) == 0, drop_counts
	assert drop_counts.get("a_live", 0) == 1, drop_counts
	assert drop_counts.get("a_moved", 0) == 1, drop_counts


def test_array_elision_keeps_path_dependent_drop(monkeypatch, tmp_path: Path) -> None:
	"""First-slice discipline: a PATH_DEPENDENT array boundary verdict
	keeps today's unconditional null-safe drop — only MUST_NOT_DROP is
	elided."""
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
	# else path → MAYBE_UNINIT → PATH_DEPENDENT → drop KEPT.
	assert agg.get("site_class:scope_exit_arraydrop") == 1, agg
	assert agg.get("arraydrop_state:maybe_uninit") == 1, agg
	assert agg.get("arraydrop_verdict:path_dependent") == 1, agg


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


def _view_prelude(tt: TypeTable):
	"""[ConstString %c, StoreLocal x %c, LoadLocal %v x] — `%v` is a
	proven-String VIEW (LoadLocal dests are discarded from owned_values),
	so any late-retain consumer of it reaches `_ensure_owned`'s
	fail-closed retain arm."""
	string_ty = tt.ensure_string()
	instrs = [
		M.ConstString(dest="%c", value="a"),
		M.StoreLocal(local="x", value="%c"),
		M.LoadLocal(dest="%v", local="x"),
	]
	return string_ty, instrs


def _expect_tripwire(func, tt, site_class: str) -> str:
	_attach_ledger(func)
	try:
		insert_string_arc(func, type_table=tt, fn_infos={})
	except AssertionError as err:
		msg = str(err)
		assert f"string_arc dead-stake tripwire [{site_class}]" in msg, msg
		assert "issues/string-arc-dead-stake-tripwire/" in msg, msg
		return msg
	raise AssertionError(f"{site_class} late-retain arm did not trip")


def test_dead_call_arg_stake_tripwire_fires(monkeypatch) -> None:
	"""Slice 4b: a proven-String VIEW at a by-value String call arg —
	the call_arg_retain fallback — is fail-closed."""
	monkeypatch.delenv("DRIFT_STRING_ARC_AUDIT", raising=False)
	tt = TypeTable()
	string_ty, instrs = _view_prelude(tt)
	func = _make_func("twc", params=[], locals_=["x"], types={"x": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = instrs + [
		M.CallIndirect(dest=None, callee="%f", args=["%v"],
			param_types=[string_ty], user_ret_type=tt.ensure_void(),
			can_throw=False),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_expect_tripwire(func, tt, "call_arg_retain")


def test_dead_value_position_stake_tripwire_fires(monkeypatch) -> None:
	"""Slice 4b: a proven-String VIEW as an array-literal element — a
	value_position_retain (default-class) fallback — is fail-closed."""
	monkeypatch.delenv("DRIFT_STRING_ARC_AUDIT", raising=False)
	tt = TypeTable()
	string_ty, instrs = _view_prelude(tt)
	arr_ty = tt.new_array(string_ty)
	func = _make_func("twv", params=[], locals_=["x"], types={"x": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = instrs + [
		M.ArrayLit(dest="%a", elem_ty=string_ty, elements=["%v"]),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_expect_tripwire(func, tt, "value_position_retain")


def test_dead_return_site3_stake_tripwire_fires(monkeypatch) -> None:
	"""Slice 4b: a proven-String VIEW as the returned value that no
	move rule approves — the structurally-extinct return_retain_site3
	fallback — is fail-closed."""
	monkeypatch.delenv("DRIFT_STRING_ARC_AUDIT", raising=False)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("twr", params=[], locals_=["x"], types={"x": string_ty})
	entry = M.BasicBlock(name="entry")
	# A LoadRef view: the site-3 alias walk only approves plain
	# LoadLocal chain endpoints (can_move_from_skipped_local), so a
	# ref-loaded String value falls through to the retain arm.
	entry.instructions = [
		M.ConstString(dest="%c", value="a"),
		M.StoreLocal(local="x", value="%c"),
		M.AddrOfLocal(dest="%p", local="x", is_mut=False),
		M.LoadRef(dest="%v", ptr="%p", inner_ty=string_ty),
	]
	entry.terminator = M.Return(value="%v")
	func.blocks = {"entry": entry}
	func.entry = "entry"
	_expect_tripwire(func, tt, "return_retain_site3")


def test_destructor_self_tag_is_untagged() -> None:
	"""Slice 4b enumeration retirement: `destructor_self` has no
	emission site anywhere; a note() carrying it now lands in UNTAGGED —
	already a hard corpus gate — instead of a dead accepted tag."""
	audit = R.StringArcAudit("test::ds")
	audit.note(R.STAKE_RETAIN, "%v", R.SITE_CLASS_DESTRUCTOR_SELF,
		pre_point=("b", 0), post_point=("b", 0))
	assert audit.untagged == 1
	assert audit.events[0].site_class.startswith("UNTAGGED:")


def test_tlr1_shim_splits_and_emission_is_identical(monkeypatch, tmp_path: Path) -> None:
	"""TLR-1 option-B shim, both split directions + emission identity:
	- ConstString temps last-used NON-consumingly (StringEq operands, a
	  generic-fallthrough consumer) → `materialized_lastuse_release`;
	- a StringConcat-result temp used the same way → ALSO
	  `materialized_lastuse_release` (TLR-3: StringConcat joined
	  MATERIALIZED_RELEASE_FAMILY);
	- a ConstString produced in ANOTHER block, last-used here → stays
	  `temp_lastuse_release` (the per-block producers map resolves it to
	  none — the cross-block tail is NOT claimed by the shim);
	- each StringRelease sits immediately after its temp's last-use
	  instruction — the same positions the pre-shim code emitted."""
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	bool_ty = tt.ensure_bool()
	func = _make_func("tlr", params=[], locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%x1", value="a"),   # cross-block producer
	]
	entry.terminator = M.Goto(target="body")
	body = M.BasicBlock(name="body")
	body.instructions = [
		M.ConstString(dest="%c1", value="a"),
		M.ConstString(dest="%c2", value="b"),
		M.StringEq(dest="%e1", left="%c1", right="%c2"),      # last use of %c1, %c2
		M.ConstString(dest="%c3", value="c"),
		M.ConstString(dest="%c4", value="d"),
		M.StringConcat(dest="%cc", left="%c3", right="%c4"),  # consumes-by-fallthrough %c3, %c4
		M.StringEq(dest="%e2", left="%cc", right="%x1"),      # last use of %cc (Concat) and %x1 (cross-block)
	]
	body.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "body": body}
	func.entry = "entry"
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::tlr")
	# Split: %c1..%c4 (ConstString) AND %cc (Concat, family since TLR-3)
	# → materialized; %x1 (cross-block producer) → temp_lastuse.
	assert agg.get("site_class:materialized_lastuse_release") == 5, agg
	assert agg.get("site_class:temp_lastuse_release") == 1, agg
	# Closed/counted-only: nothing UNTAGGED, nothing UNCLASSIFIED.
	assert "untagged" not in agg, agg
	assert agg.get("unclassified", 0) == 0, agg
	# Emission identity: each temp's StringRelease sits immediately after
	# its last-use instruction in the OUTPUT MIR.
	out_body = func.blocks["body"].instructions
	def _release_follows(last_use_pred, subjects):
		for i, ins in enumerate(out_body):
			if last_use_pred(ins):
				trailing = set()
				j = i + 1
				while j < len(out_body) and type(out_body[j]).__name__ == "StringRelease":
					trailing.add(out_body[j].value)
					j += 1
				assert subjects <= trailing, (subjects, trailing, out_body)
				return
		raise AssertionError("last-use instruction not found in output MIR")
	_release_follows(lambda ins: type(ins).__name__ == "StringEq" and getattr(ins, "dest", None) == "%e1", {"%c1", "%c2"})
	_release_follows(lambda ins: type(ins).__name__ == "StringEq" and getattr(ins, "dest", None) == "%e2", {"%cc", "%x1"})


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
	from lang.driftc.stage2.string_arc import compute_lastuse_release_points
	out = tmp_path / "audit.jsonl"
	_audit_env(monkeypatch, out)
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _make_func("cf", params=[], locals_=["x"], types={"x": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
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
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"

	points = compute_lastuse_release_points(
		entry, local_types=dict(func.local_types) | {
			"%q": string_ty, "%r": string_ty, "%s": string_ty,
			"%c3": string_ty, "%c4": string_ty, "%cc": string_ty, "%ig": string_ty,
		},
		fn_infos={}, type_table=tt, live_out_names=set(),
	)
	# %q AND %cc (family since TLR-3) drain at the first StringEq (idx 7);
	# %r at the repeated-operand StringEq (idx 8) — ONE point despite two
	# occurrences; %c3/%c4 drain at the concat (idx 5).  %s (consumed) and
	# %ig (IGNORE) have no points.
	assert points == {"%q": 7, "%cc": 7, "%r": 8, "%c3": 5, "%c4": 5}, points

	# live pass agreement: seed the temp types the calculator was given.
	for k, v in {"%q": string_ty, "%r": string_ty, "%s": string_ty,
	             "%c3": string_ty, "%c4": string_ty, "%cc": string_ty, "%ig": string_ty}.items():
		func.local_types[k] = v
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
	agg = _fn_agg(out, "test::cf")
	# materialized = exactly the calculator's points (5, incl. %cc since
	# TLR-3); %s and %ig produce none.
	assert agg.get("site_class:materialized_lastuse_release") == 5, agg
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


def test_tlr2a_prescan_exclusion_contract(monkeypatch) -> None:
	"""TLR-2b prescan-exclusion contract, pinned at the calculator now:
	an in-contract pre-materialized StringRelease contributes NO
	occurrence to any count — every OTHER temp's release point is
	unchanged versus the same block without it, and the released temp is
	excluded from the result."""
	from lang.driftc.stage2.string_arc import compute_lastuse_release_points
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
	from lang.driftc.stage2.string_arc import compute_lastuse_release_points
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
	from lang.driftc.stage2.string_arc import (
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
	# Live-pass agreement on the SAME un-seeded func.
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
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
	from lang.driftc.stage2.string_arc import compute_lastuse_release_points
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
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos={})
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
	CONTRACT DRIFT: `consumes_string_operand` would lie relative to the
	live arm and future users of the predicate would decide wrongly.
	Covers direct Call (fn_infos
	signature), CallIndirect, and CallIface (instruction-carried
	param_types); `%u` is the control proving real points still emit."""
	from lang.driftc.checker import FnInfo, FnSignature
	from lang.driftc.stage2.string_arc import (
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
	_attach_ledger(func)
	insert_string_arc(func, type_table=tt, fn_infos=fn_infos)
	out_instrs = func.blocks["entry"].instructions
	released = [i.value for i in out_instrs if type(i).__name__ == "StringRelease"]
	assert released == ["%u"], released
	agg = _fn_agg(out, "test::sp")
	assert agg.get("site_class:materialized_lastuse_release") == 1, agg
	assert agg.get("site_class:temp_lastuse_release", 0) == 0, agg


def test_tlr2b_pass_plus_arc_equals_arc_only(monkeypatch, tmp_path: Path) -> None:
	"""TLR-2b A/B equivalence pin: for the migrated family the
	materialization pass + string_arc must produce the BYTE-IDENTICAL
	instruction stream string_arc-alone produced, with the same audit
	counters (`materialized_lastuse_release` keeps its author-independent
	meaning — noted at the recognition arm in B, at the TLR-1 shim in A).
	Shapes: qualified temp; same-instruction TWO-temp group (drain-order
	rule); repeated-operand temp (ONE release); consumed single-use
	ConstString (no release from either author); Concat-produced temp
	(IN the family since TLR-3 — materialized in config B, shim-classified
	materialized in config A, same position)."""
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

	# Config A: string_arc only (in-pass emission, TLR-1 shim).
	fa = build("ab_a")
	_attach_ledger(fa)
	insert_string_arc(fa, type_table=tt, fn_infos={})
	# Config B: materialization pass first (driver order: pass → ledger
	# build → string_arc), then recognition.
	fb = build("ab_b")
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	assert fb.blocks["entry"].instructions == fa.blocks["entry"].instructions
	agg_a = _fn_agg(out, "test::ab_a")
	agg_b = _fn_agg(out, "test::ab_b")
	for key in (
		"site_class:materialized_lastuse_release",
		"site_class:temp_lastuse_release",
		"events",
	):
		assert agg_a.get(key) == agg_b.get(key), (key, agg_a, agg_b)
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
	out-of-contract input releases.  Three carriers:
	- SHAPE: a release of a StringFromInt-produced temp at the correct
	  position (StringFrom* is NOT in MATERIALIZED_RELEASE_FAMILY — the
	  TLR-2b Concat carrier became in-contract when TLR-3 extended the
	  family, so the shape case moved to the next non-member);
	- PLACEMENT, misplaced: a Concat-temp release BEFORE a later use;
	- PLACEMENT, duplicated: two releases of one Concat temp."""
	import pytest
	tt = TypeTable()
	string_ty = tt.ensure_string()
	int_ty = tt.ensure_int()

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
		M.ConstInt(dest="%n", value=1),
		M.StringFromInt(dest="%sf", value="%n"),
		M.StringEq(dest="%e", left="%sf", right="%sf"),
		M.StringRelease(value="%sf"),  # correct position, WRONG family/shape
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
	"""Cross-block ConstString temps are OUT of the TLR-2 family (the
	producing block sees them live-out; the using block has no in-block
	producer): the pass emits nothing, and string_arc's own bookkeeping
	still releases them as temp_lastuse in both configs."""
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
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is False
	fa = build("xb_a")
	_attach_ledger(fa)
	insert_string_arc(fa, type_table=tt, fn_infos={})
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	for bname in ("entry", "next"):
		assert fb.blocks[bname].instructions == fa.blocks[bname].instructions
	agg_a = _fn_agg(out, "test::xb_a")
	assert agg_a.get("site_class:materialized_lastuse_release", 0) == 0, agg_a


def test_tlr3_concat_chain_ab_byte_identity(monkeypatch, tmp_path: Path) -> None:
	"""TLR-3 chain A/B pin: `a + b + c` — two Concats — with the
	CROSS-FAMILY same-drain group the design called out: the second
	Concat drains `%c1` (Concat temp) AND `%d` (ConstString temp)
	together; releases are consecutive in drain order (`%c1` then `%d`,
	last-occurrence positions in the draining instruction's operand
	walk).  Pass+arc must equal arc-only byte-for-byte, with all five
	releases materialized."""
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

	fa = build("ch_a")
	_attach_ledger(fa)
	insert_string_arc(fa, type_table=tt, fn_infos={})
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
	assert fb.blocks["entry"].instructions == fa.blocks["entry"].instructions
	agg_a = _fn_agg(out, "test::ch_a")
	agg_b = _fn_agg(out, "test::ch_b")
	for key in ("site_class:materialized_lastuse_release",
			"site_class:temp_lastuse_release", "events"):
		assert agg_a.get(key) == agg_b.get(key), (key, agg_a, agg_b)
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
	fa = build("mc_a")
	_attach_ledger(fa)
	insert_string_arc(fa, type_table=tt, fn_infos={})
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	assert fb.blocks["entry"].instructions == fa.blocks["entry"].instructions
	agg_b = _fn_agg(out, "test::mc_b")
	# %a, %b, %c, %dd, %mu materialized (5); %cs consumed → none.
	assert agg_b.get("site_class:materialized_lastuse_release") == 5, agg_b
	assert agg_b.get("site_class:temp_lastuse_release", 0) == 0, agg_b


def test_tlr3_cross_block_concat_untouched(monkeypatch, tmp_path: Path) -> None:
	"""TLR-3: a Concat temp produced in one block and last-used in
	another stays OUT of the family (per-block producer map): the pass
	emits nothing for it, and string_arc's own bookkeeping still releases
	it as temp_lastuse in both configs."""
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
	# The pass DOES touch the block: %a/%b (ConstString) drain at the
	# in-block Concat and materialize; %cc itself must not.
	assert materialize_lastuse_releases(fb, type_table=tt, fn_infos={}) is True
	rel = [i.value for i in fb.blocks["entry"].instructions
		if type(i).__name__ == "StringRelease"]
	assert "%cc" not in rel and set(rel) == {"%a", "%b"}, rel
	assert not any(type(i).__name__ == "StringRelease"
		for i in fb.blocks["next"].instructions)
	fa = build("xc_a")
	_attach_ledger(fa)
	insert_string_arc(fa, type_table=tt, fn_infos={})
	_attach_ledger(fb)
	insert_string_arc(fb, type_table=tt, fn_infos={})
	for bname in ("entry", "next"):
		assert fb.blocks[bname].instructions == fa.blocks[bname].instructions
	agg_b = _fn_agg(out, "test::xc_b")
	# %a, %b materialized; %cc cross-block → temp_lastuse (both configs).
	assert agg_b.get("site_class:materialized_lastuse_release") == 2, agg_b
	assert agg_b.get("site_class:temp_lastuse_release") == 1, agg_b


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
