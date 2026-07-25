# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Sibling catch-binder identity: each catch entry marks ONLY its own
binding (the HCatchArm.binder_id contract), and the name-keyed lookup
is genuinely FALLBACK-ONLY — never consulted when the identity is
present.

Companion to lang/tests/driver/test_catch_binder_sibling_name_reuse.py
(the end-to-end regression); this tooth pins the MECHANISM: with two
sibling arms both named `e`, a name-keyed mark would count one arm's
binding VALID twice (once from its own entry, once spuriously from
the sibling's)."""
from __future__ import annotations

from pathlib import Path

from lang.driftc.borrow_checker_pass import BorrowChecker, PlaceState
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.type_checker import TypeChecker

SRC = """
module main;

error AlphaErr { code: Int, tag: Int }
error BetaErr { level: Int }

pub fn main() nothrow -> Int {
	var acc = 0;
	try {
		throw AlphaErr(code = 41, tag = 7);
	} catch AlphaErr(e) {
		acc = acc + e.code + e.tag;
	} catch {
		acc = acc + 1000;
	}
	try {
		throw BetaErr(level = 93);
	} catch BetaErr(e) {
		acc = acc + e.level;
	} catch {
		acc = acc + 1000;
	}
	if acc != 141 { return 1; }
	return 0;
}
"""


def _checked_bc(tmp_path: Path):
	path = tmp_path / "main.drift"
	path.write_text(SRC)
	module, type_table, _exc_env, diagnostics = parse_drift_to_hir(path)
	assert diagnostics == []
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	fn_id = fn_ids[0]
	block = normalize_hir(module.func_hirs[fn_id])
	tc = TypeChecker(type_table)
	res = tc.check_function(fn_id, block, param_types={}, return_type=None)
	assert res.diagnostics == []
	bc = BorrowChecker.from_typed_fn(
		res.typed_fn, type_table=type_table, signatures_by_id=None, enable_auto_borrow=True)
	return bc, res.typed_fn


def test_each_catch_entry_marks_only_its_own_binding(tmp_path: Path) -> None:
	bc, typed_fn = _checked_bc(tmp_path)

	# Instrument: the catch-entry marking emits, per entry visit, a
	# BURST — first the generic name place (local_id == -1), then the
	# concrete binding id(s) it decided to mark.  Group the VALID `e`
	# marks into bursts on the -1 sentinel; each burst is one entry
	# visit.  (`_catch_binders_by_block` is rebuilt INSIDE check_block,
	# so a dict wrapper cannot observe it — bursts need no wrapper.)
	# The dataflow revisits blocks to fixpoint, so burst COUNT is
	# unbounded; burst CONTENT is the contract.
	bursts: list[set[int]] = []
	orig_set_state = bc._set_state

	def spy_set_state(state, place, st):
		if st is PlaceState.VALID and getattr(place.base, "name", None) == "e":
			bid = int(place.base.local_id)
			if bid == -1:
				bursts.append(set())
			elif bursts:
				bursts[-1].add(bid)
		return orig_set_state(state, place, st)

	name_lookups: list[str] = []
	orig_lookup = bc._binding_ids_for_name_in_block

	def spy_lookup(block, name):
		name_lookups.append(name)
		return orig_lookup(block, name)

	bc._set_state = spy_set_state
	bc._binding_ids_for_name_in_block = spy_lookup
	diags = bc.check_block(typed_fn.body)
	errors = [d for d in diags if getattr(d, "severity", "error") == "error"]
	assert errors == [], [getattr(d, "message", d) for d in errors]

	# Two catch entries were registered, each carrying its OWN identity.
	entries = list(bc._catch_binders_by_block.values())
	e_entries = [ent for ent in entries if ent[0] == "e"]
	assert len(e_entries) == 2, e_entries
	ids = [ent[1] for ent in e_entries]
	assert all(i is not None for i in ids), f"binder_id missing: {e_entries}"
	assert ids[0] != ids[1], f"sibling arms share a binding identity: {ids}"

	# Each entry visit marked EXACTLY ONE binding, and both sibling
	# bindings were marked by their own visits:
	#   * a burst with 2 concrete ids  = an entry marked a SIBLING's
	#     binding alongside its own (the `|=` regression);
	#   * a binding id never appearing = an entry marked the WRONG
	#     (earliest-name) binding instead of its own (the pre-fix
	#     defect: arm 2's burst held arm 1's id, arm 2's id nowhere).
	assert bursts, "no catch-entry marking bursts observed"
	for burst in bursts:
		assert len(burst) == 1, (
			f"an entry visit marked multiple bindings {sorted(burst)} — "
			f"sibling identity leaked into the mark set"
		)
	marked_union = set().union(*bursts)
	assert marked_union == {int(ids[0]), int(ids[1])}, (
		f"marked bindings {sorted(marked_union)} != the two sibling "
		f"binder_ids {sorted(int(i) for i in ids)}"
	)

	# Exclusive path: the name-keyed lookup never ran for `e`.
	assert "e" not in name_lookups, (
		f"name-keyed lookup consulted despite recorded identities: {name_lookups}"
	)
