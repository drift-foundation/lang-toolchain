# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Differential unit pins for `destructible_authority` (Milestone A
extraction, 2026-07-20).

These exercise the extracted DECISION authority directly on hand-built
MIR + a real ownership ledger, independent of the emitters:

  * `DropClassifier` — String / Arc-struct / error / nullsafe-struct /
    ref classifications.
  * `classify_destructible_locals` — the destructible / nullsafe split,
    including the `__`-hidden exclusion and the string/array exclusions.
  * `site4_verdict` — MUST_DROP (live overwrite), MUST_NOT_DROP (first
    store of uninit), and the missing-ledger tripwire.
  * `compute_store_defs` / `compute_assigned_in` — definite-assignment
    dataflow.
  * `site3_return_decision` — sorted drop order, plus the three skip
    channels (moved-out, explicitly-dropped, ledger MUST_NOT_DROP).
"""

from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_ledger import DropVerdict, build_ledger
from lang.driftc.stage2.drop_policy_compute import (
	compute_drop_policy,
	zero_storage_drop_safe,
)
from lang.driftc.stage2.destructible_authority import (
	DropClassifier,
	ReturnMoveState,
	classify_destructible_locals,
	compute_assigned_in,
	compute_return_move_state,
	compute_store_defs,
	flag_managed_at_return,
	site3_return_decision,
	site4_verdict,
)


# ── type-table fixtures ───────────────────────────────────────────────


def _droppable_struct(type_table: TypeTable, name: str = "DropMe") -> int:
	"""Struct with a String field + a destructor_fns entry → destructible
	AND non-nullsafe (a real destructor means the drop is not null-safe)."""
	string_ty = type_table.ensure_string()
	tid = type_table.declare_struct(module_id="test", name=name, field_names=["inner"])
	type_table.define_struct_fields(tid, field_types=[string_ty])
	destroy_fn = FunctionId(module="test", name=f"{name}::destroy", ordinal=0)
	dfns = dict(getattr(type_table, "destructor_fns", None) or {})
	dfns[tid] = destroy_fn
	type_table.destructor_fns = dfns
	non_copy = set(getattr(type_table, "_nc", set()))
	non_copy.add(tid)
	type_table._nc = non_copy  # type: ignore[attr-defined]
	type_table._copy_query = lambda t: False if t in non_copy else None  # type: ignore[attr-defined]
	return tid


def _nullsafe_struct(type_table: TypeTable, name: str = "PlainStr") -> int:
	"""Struct with a String field but NO destructor → destructible AND
	nullsafe (String field drop is a null-safe release)."""
	string_ty = type_table.ensure_string()
	tid = type_table.declare_struct(module_id="test", name=name, field_names=["inner"])
	type_table.define_struct_fields(tid, field_types=[string_ty])
	return tid


def _make_func(name, *, params, locals_, types):
	return M.MirFunc(
		name=f"test::{name}",
		params=list(params),
		locals=list(locals_),
		fn_id=FunctionId(module="test", name=name, ordinal=0),
		local_types=dict(types),
	)


# ── DropClassifier ────────────────────────────────────────────────────


def test_classifier_string_is_destructible_and_nullsafe() -> None:
	tt = TypeTable()
	string_ty = tt.ensure_string()
	clf = DropClassifier(tt)
	assert clf.type_needs_drop(string_ty) is True
	assert clf.is_destructible_tid(string_ty) is True
	assert clf.is_error_tid(string_ty) is False
	assert clf.is_nullsafe_drop(string_ty) is True


def test_classifier_arc_struct_destructible_not_nullsafe() -> None:
	tt = TypeTable()
	string_ty = tt.ensure_string()
	drop_ty = _droppable_struct(tt)
	clf = DropClassifier(tt)
	assert clf.is_destructible_tid(drop_ty) is True
	# Real destructor → drop is NOT null-safe.
	assert clf.is_nullsafe_drop(drop_ty) is False


def test_classifier_error_tid() -> None:
	tt = TypeTable()
	string_ty = tt.ensure_string()
	err_ty = tt.ensure_error()
	clf = DropClassifier(tt)
	assert clf.is_error_tid(err_ty) is True
	assert clf.type_needs_drop(err_ty) is True
	assert clf.is_destructible_tid(err_ty) is True
	# ERROR is null-safe to drop.
	assert clf.is_nullsafe_drop(err_ty) is True


def test_classifier_nullsafe_struct() -> None:
	tt = TypeTable()
	string_ty = tt.ensure_string()
	ns_ty = _nullsafe_struct(tt)
	clf = DropClassifier(tt)
	assert clf.is_destructible_tid(ns_ty) is True
	assert clf.is_nullsafe_drop(ns_ty) is True


def test_classifier_ref_and_none() -> None:
	tt = TypeTable()
	string_ty = tt.ensure_string()
	ref_ty = tt.ensure_ref(string_ty)
	clf = DropClassifier(tt)
	assert clf.type_needs_drop(ref_ty) is False
	assert clf.is_destructible_tid(ref_ty) is False
	assert clf.is_destructible_tid(None) is False
	assert clf.is_error_tid(None) is False


# ── classify_destructible_locals ──────────────────────────────────────


def test_classify_destructible_locals_split() -> None:
	tt = TypeTable()
	string_ty = tt.ensure_string()
	drop_ty = _droppable_struct(tt)
	ns_ty = _nullsafe_struct(tt)
	err_ty = tt.ensure_error()
	ref_ty = tt.ensure_ref(string_ty)
	clf = DropClassifier(tt)
	types = {
		"s": string_ty,             # String → excluded (in string_locals)
		"d": drop_ty,               # destructible, non-nullsafe → included
		"ns": ns_ty,                # destructible, nullsafe → included + nullsafe
		"__match_binder_e": err_ty,  # error binder → included (name + error)
		"r": ref_ty,                # ref → excluded (not destructible)
		"__hidden": drop_ty,        # hidden non-binder → excluded by name filter
	}
	func = _make_func(
		"split",
		params=[],
		locals_=["s", "d", "ns", "__match_binder_e", "r", "__hidden"],
		types=types,
	)
	dest, nullsafe = classify_destructible_locals(
		func,
		clf,
		local_types=types,
		string_locals={"s"},
		array_locals=set(),
	)
	assert dest == {"d", "ns", "__match_binder_e"}
	# ns is the only nullsafe member; error is nullsafe too.
	assert nullsafe == {"ns", "__match_binder_e"}
	assert "d" not in nullsafe


# ── site4_verdict ─────────────────────────────────────────────────────


def _attach_ledger(func: M.MirFunc):
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)
	return ledger


def test_site4_verdict_must_drop_on_live_overwrite() -> None:
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("ov", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.StoreLocal(local="x", value="t_new"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = _attach_ledger(func)
	# At idx 1 (the second store) x is LIVE → MUST_DROP. The authority
	# computes needs_drop internally and returns (verdict, needs_drop).
	_v, _nd = site4_verdict(
		ledger,
		fn_name=func.name, block_name="entry", instr_idx=1,
		local="x", local_ty=drop_ty, type_table=tt,
	)
	assert _v is DropVerdict.MUST_DROP
	assert _nd is True


def test_site4_verdict_must_not_drop_on_first_store() -> None:
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("first", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = _attach_ledger(func)
	# At idx 0 x is UNINIT (pre-state) → MUST_NOT_DROP.
	_v, _nd = site4_verdict(
		ledger,
		fn_name=func.name, block_name="entry", instr_idx=0,
		local="x", local_ty=drop_ty, type_table=tt,
	)
	assert _v is DropVerdict.MUST_NOT_DROP


def test_site4_verdict_missing_ledger_raises() -> None:
	with pytest.raises(RuntimeError, match="without an attached ownership ledger"):
		site4_verdict(
			None,
			fn_name="test::x", block_name="entry", instr_idx=0,
			local="x", local_ty=None, type_table=None,
		)


# ── dataflow + site3_return_drops ─────────────────────────────────────


def test_compute_store_defs_and_assigned_in() -> None:
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("df", params=["p"], locals_=["p", "a"], types={"p": drop_ty, "a": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="a", value="t0"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	store_defs = compute_store_defs(func)
	assert store_defs["entry"] == {"a"}
	assigned_in = compute_assigned_in(func, store_defs)
	# Entry's assigned_in is the param set.
	assert assigned_in["entry"] == {"p"}


def test_site3_return_drops_sorted_and_skips() -> None:
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	string_ty = tt.ensure_string()
	clf = DropClassifier(tt)
	# locals deliberately out of alphabetical order to prove sorting.
	names = ["c", "a", "b", "m", "e", "z"]
	types = {n: drop_ty for n in names}
	func = _make_func("ret", params=[], locals_=names, types=types)
	entry = M.BasicBlock(name="entry")
	for n in names:
		entry.instructions.append(M.StoreLocal(local=n, value=f"t_{n}"))
	# z is moved out via MIR → ledger sees it MovedOut/Tombstoned at return.
	entry.instructions.append(M.MoveOut(dest="t_z_out", local="z", ty=drop_ty))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = _attach_ledger(func)

	dest, _ns = classify_destructible_locals(
		func, clf, local_types=types, string_locals=set(), array_locals=set()
	)
	assert dest == set(names)
	store_defs = compute_store_defs(func)
	assigned_in = compute_assigned_in(func, store_defs)

	drops = site3_return_decision(
		func,
		entry,
		ledger=ledger,
		type_table=tt,
		destructible_locals=dest,
		local_types=types,
		move_state=ReturnMoveState(
			moved_out=frozenset({"m"}),          # skip channel: moved-out bookkeeping
			explicitly_dropped=frozenset({"e"}),  # skip channel: explicit drop
		),
		assigned_in=assigned_in,
		store_defs=store_defs,
		flag_managed=set(),
	)
	# a,b,c dropped in sorted order; m/e skipped by the passed sets;
	# z skipped by the ledger MUST_NOT_DROP verdict (MoveOut'd).
	assert drops.drops == ("a", "b", "c")
	# Structured facts (Phase D): m/e are generic skips (silent in observe);
	# z joins via the ledger MUST_NOT_DROP fold; none are flag-managed.
	assert {"m", "e", "z"} <= set(drops.generic_skips)
	assert drops.flag_managed == frozenset()
	assert drops.point == ("entry", len(entry.instructions))


def test_site3_return_drops_flag_managed_skip() -> None:
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	string_ty = tt.ensure_string()
	clf = DropClassifier(tt)
	types = {"a": drop_ty, "b": drop_ty}
	func = _make_func("flag", params=[], locals_=["a", "b"], types=types)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="a", value="ta"))
	entry.instructions.append(M.StoreLocal(local="b", value="tb"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ledger = _attach_ledger(func)
	dest, _ns = classify_destructible_locals(
		func, clf, local_types=types, string_locals=set(), array_locals=set()
	)
	store_defs = compute_store_defs(func)
	assigned_in = compute_assigned_in(func, store_defs)
	drops = site3_return_decision(
		func, entry, ledger=ledger, type_table=tt,
		destructible_locals=dest, local_types=types,
		move_state=ReturnMoveState(moved_out=frozenset(), explicitly_dropped=frozenset()),
		assigned_in=assigned_in, store_defs=store_defs,
		flag_managed={"b"},  # b owned by drop-flag plumbing → skip
	)
	assert drops.drops == ("a",)
	# Flag ownership is a DISTINCT observe fact, not a generic skip.
	assert drops.flag_managed == frozenset({"b"})
	assert "b" not in drops.generic_skips


# ── compute_return_move_state (Amendment 2 differential teeth) ─────────


def test_return_move_state_branch_intersection_not_moved_at_join() -> None:
	"""A local moved-out on ONE predecessor path but not the other is NOT
	in moved_out at the join (intersection fixpoint)."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	drop_ty = _droppable_struct(tt)
	func = _make_func("br", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="c", then_target="bthen", else_target="belse")
	bthen = M.BasicBlock(name="bthen")
	bthen.instructions.append(M.MoveOut(dest="t_out", local="x", ty=drop_ty))
	bthen.terminator = M.Goto(target="join")
	belse = M.BasicBlock(name="belse")
	belse.terminator = M.Goto(target="join")
	join = M.BasicBlock(name="join")
	join.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "bthen": bthen, "belse": belse, "join": join}
	ms = compute_return_move_state(
		func, destructible_locals={"x"}, string_ty=string_ty
	)
	assert ms["bthen"].moved_out == frozenset({"x"})
	assert ms["belse"].moved_out == frozenset()
	# Intersection over predecessors → x is NOT moved-out at the join.
	assert ms["join"].moved_out == frozenset()


def test_return_move_state_moveout_then_store_clears_moved() -> None:
	"""MoveOut then StoreLocal in the same block: the StoreLocal clears the
	moved-out state (not in moved_out at block end)."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	drop_ty = _droppable_struct(tt)
	func = _make_func("mos", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.MoveOut(dest="t", local="x", ty=drop_ty))
	entry.instructions.append(M.StoreLocal(local="x", value="v"))
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	ms = compute_return_move_state(
		func, destructible_locals={"x"}, string_ty=string_ty
	)
	assert ms["entry"].moved_out == frozenset()


def test_return_move_state_movefromref_clears_moved_and_dropped() -> None:
	"""MoveFromRef of a local clears prior moved AND explicitly-dropped
	state for that local."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	drop_ty = _droppable_struct(tt)
	func = _make_func("mfr", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.MoveOut(dest="t", local="x", ty=drop_ty))       # moved-out={x}
	entry.instructions.append(M.LoadLocal(dest="v", local="x"))
	entry.instructions.append(M.DropValue(value="v", ty=drop_ty))               # dropped={x}
	entry.instructions.append(M.MoveFromRef(local="x", ptr="p", inner_ty=drop_ty))  # clears both
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	ms = compute_return_move_state(
		func, destructible_locals={"x"}, string_ty=string_ty
	)
	assert ms["entry"].moved_out == frozenset()
	assert ms["entry"].explicitly_dropped == frozenset()


def test_return_move_state_explicit_drop_recognition() -> None:
	"""`LoadLocal(v, x) ; DropValue(v)` on a destructible `x` records `x`;
	a string DropValue does NOT; a DropValue of a non-loaded SSA value
	records nothing."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	drop_ty = _droppable_struct(tt)
	func = _make_func("ed", params=[], locals_=["x", "y"], types={"x": drop_ty, "y": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.LoadLocal(dest="v0", local="x"))
	entry.instructions.append(M.DropValue(value="v0", ty=drop_ty))    # x recorded
	entry.instructions.append(M.LoadLocal(dest="v1", local="y"))
	entry.instructions.append(M.DropValue(value="v1", ty=string_ty))  # string → NOT recorded
	entry.instructions.append(M.DropValue(value="v_unloaded", ty=drop_ty))  # not loaded → nothing
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	ms = compute_return_move_state(
		func, destructible_locals={"x", "y"}, string_ty=string_ty
	)
	assert ms["entry"].explicitly_dropped == frozenset({"x"})


def test_return_move_state_reinit_after_drop_clears() -> None:
	"""StoreLocal after an explicit drop clears explicitly_dropped for that
	local."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	drop_ty = _droppable_struct(tt)
	func = _make_func("rad", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.LoadLocal(dest="v", local="x"))
	entry.instructions.append(M.DropValue(value="v", ty=drop_ty))  # dropped={x}
	entry.instructions.append(M.StoreLocal(local="x", value="w"))  # reinit clears
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	ms = compute_return_move_state(
		func, destructible_locals={"x"}, string_ty=string_ty
	)
	assert ms["entry"].explicitly_dropped == frozenset()


def test_return_move_state_non_destructible_drop_not_recorded() -> None:
	"""A non-string DropValue of a loaded local that is NOT in
	destructible_locals records nothing."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	drop_ty = _droppable_struct(tt)
	func = _make_func("nd", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.LoadLocal(dest="v", local="x"))
	entry.instructions.append(M.DropValue(value="v", ty=drop_ty))
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	ms = compute_return_move_state(
		func, destructible_locals=set(), string_ty=string_ty
	)
	assert ms["entry"].explicitly_dropped == frozenset()


# ── flag_managed_at_return ────────────────────────────────────────────


def test_flag_managed_at_return_reads_metadata_set() -> None:
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("fm", params=[], locals_=["a", "b"], types={"a": drop_ty, "b": drop_ty})
	# insert_drop_flags attaches this metadata set; is_flag_managed reads it.
	setattr(func, "_drop_flag_managed_locals", {"b"})
	assert flag_managed_at_return(func, {"a", "b"}) == {"b"}
	# No metadata → nothing is flag-managed.
	func2 = _make_func("fm2", params=[], locals_=["a"], types={"a": drop_ty})
	assert flag_managed_at_return(func2, {"a"}) == set()


# ── PATH_DEPENDENT teeth (Amendment 3) ────────────────────────────────


class _StubLedger:
	"""Deterministic ledger stub returning a fixed verdict at every point."""

	def __init__(self, verdict: DropVerdict):
		self._verdict = verdict

	def verdict_at(self, point, local, needs_drop=None):
		return self._verdict


def test_site4_verdict_path_dependent_tripwire() -> None:
	"""A PATH_DEPENDENT verdict at the store point fires the exact
	proof-obligation tripwire RuntimeError."""
	ledger = _StubLedger(DropVerdict.PATH_DEPENDENT)
	with pytest.raises(RuntimeError, match="returned PathDependent"):
		site4_verdict(
			ledger,
			fn_name="test::pd", block_name="entry", instr_idx=3,
			local="x", local_ty=None, type_table=None,
		)


def test_site3_path_dependent_widens_zero_safe_only() -> None:
	"""A zero-storage-drop-safe local with a PATH_DEPENDENT return verdict
	IS widened into the ordered drop set; a zero-storage-UNSAFE local with
	PATH_DEPENDENT is NOT."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	arr_ty = tt.new_array(string_ty)   # ARRAY → zero-storage-drop-safe
	struct_ty = _droppable_struct(tt)  # STRUCT w/ destructor → NOT zero-storage-safe
	# Real types drive zero_storage_drop_safe; the stub controls the verdict.
	assert zero_storage_drop_safe(arr_ty, tt) is True
	assert zero_storage_drop_safe(struct_ty, tt) is False
	types = {"arr": arr_ty, "st": struct_ty}
	func = _make_func("pd3", params=[], locals_=["arr", "st"], types=types)
	entry = M.BasicBlock(name="entry")
	# Neither local is stored/assigned → not in initialized_at_return; the
	# ONLY path into the drop set is the PATH_DEPENDENT zero-safe widening.
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	store_defs = compute_store_defs(func)
	assigned_in = compute_assigned_in(func, store_defs)
	drops = site3_return_decision(
		func, entry,
		ledger=_StubLedger(DropVerdict.PATH_DEPENDENT),
		type_table=tt,
		destructible_locals={"arr", "st"},
		local_types=types,
		move_state=ReturnMoveState(moved_out=frozenset(), explicitly_dropped=frozenset()),
		assigned_in=assigned_in,
		store_defs=store_defs,
		flag_managed=set(),
	)
	# Only the zero-storage-safe array is widened into the drop set.
	assert drops.drops == ("arr",)
	# The widening lands in the FINAL initialized set the decision carries.
	assert "arr" in drops.initialized and "st" not in drops.initialized


# ── R3/R4 string_return_releases (S5 decision) ────────────────────────

def _StubStrLedger(verdict_map):
	"""ledger returning a verdict per local at verdict_at."""
	class _L:
		def verdict_at(self, point, local, needs_drop=None):
			return verdict_map.get(local, DropVerdict.MUST_DROP)
	return _L()


def test_string_return_releases_basic_and_moved_out():
	from lang.driftc.stage2.destructible_authority import string_return_releases, ReturnMoveState
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("sr", params=[], locals_=["a", "b"], types={"a": sty, "b": sty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="a", value="va"), M.StoreLocal(local="b", value="vb")]
	entry.terminator = M.Return(value=None)   # not returning a or b
	func.blocks["entry"] = entry
	ms = ReturnMoveState(moved_out=frozenset({"b"}), explicitly_dropped=frozenset())
	rel = string_return_releases(func, entry, ledger=None, type_table=tt,
		string_locals={"a", "b"}, string_ty=sty, move_state=ms)
	assert rel == ["a"]   # b is moved-out → skipped; sorted order


def test_string_return_releases_r4_returned_source_skipped():
	"""R4: the returned String's source storage local is NOT released."""
	from lang.driftc.stage2.destructible_authority import string_return_releases, ReturnMoveState
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("sr4", params=[], locals_=["a", "b"], types={"a": sty, "b": sty})
	entry = M.BasicBlock(name="entry")
	# return a: LoadLocal(%r, a) ; Return(%r)
	entry.instructions = [
		M.StoreLocal(local="a", value="va"),
		M.StoreLocal(local="b", value="vb"),
		M.LoadLocal(dest="%r", local="a"),
	]
	entry.terminator = M.Return(value="%r")
	func.blocks["entry"] = entry
	ms = ReturnMoveState(moved_out=frozenset(), explicitly_dropped=frozenset())
	rel = string_return_releases(func, entry, ledger=None, type_table=tt,
		string_locals={"a", "b"}, string_ty=sty, move_state=ms)
	assert rel == ["b"]   # a is the returned source → R4 skip


def test_string_return_releases_r3_ledger_must_not_drop_elided():
	"""R3: a string with ledger MUST_NOT_DROP at the return point is elided."""
	from lang.driftc.stage2.destructible_authority import string_return_releases, ReturnMoveState
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("sr3", params=[], locals_=["a", "b"], types={"a": sty, "b": sty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.StoreLocal(local="a", value="va"), M.StoreLocal(local="b", value="vb")]
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	ms = ReturnMoveState(moved_out=frozenset(), explicitly_dropped=frozenset())
	ledger = _StubStrLedger({"a": DropVerdict.MUST_NOT_DROP})  # a elided
	rel = string_return_releases(func, entry, ledger=ledger, type_table=tt,
		string_locals={"a", "b"}, string_ty=sty, move_state=ms)
	assert rel == ["b"]   # a MUST_NOT_DROP → elided
