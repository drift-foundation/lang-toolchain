# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase D — `ownership_normalization.normalize_ownership_mir` pins.

Covers the pass's four permanent contracts:
  * R1 entry zero-storage initialization (coverage + group order +
    exclusions);
  * R5 MoveOut expansion (shape, seeding, audit note anchoring incl. the
    `moveout_feeds_drop` pairing);
  * identity pass-through (every non-MoveOut instruction survives BY
    OBJECT IDENTITY; Return terminators untouched);
  * the TABLE-DRIVEN `local_types` seeding contract — every inventoried
    instruction family, exact delta, unchanged existing bindings.
Plus the R8 closed-vessel fail-closed teeth at the new consumer and the
dirty-mark-iff-changed contract.
"""
from __future__ import annotations

import pytest

from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_normalization import normalize_ownership_mir
from lang.driftc.stage2.string_ownership_analysis import (
	R8Recognition,
	compute_recognized_releases,
)


def _make_func(name, *, params=(), locals_=(), types=None):
	fn_id = FunctionId(module="test", name=name, ordinal=0)
	return M.MirFunc(
		name=f"test::{name}", params=list(params), locals=list(locals_),
		fn_id=fn_id, local_types=dict(types or {}),
	)


def _droppable_struct(tt: TypeTable) -> int:
	string_ty = tt.ensure_string()
	tid = tt.declare_struct(module_id="test", name="DropMe", field_names=["inner"])
	tt.define_struct_fields(tid, field_types=[string_ty])
	tt.destructor_fns = {tid: FunctionId(module="test", name="DropMe::destroy", ordinal=0)}
	non_copy = {tid}
	tt._copy_query = lambda t: False if t in non_copy else None  # type: ignore[attr-defined]
	return tid


def _nullsafe_struct(tt: TypeTable) -> int:
	"""String-bearing struct WITHOUT a destructor entry → destructible via
	the transitive String field, null-safe drop shape."""
	string_ty = tt.ensure_string()
	tid = tt.declare_struct(module_id="test", name="NullSafe", field_names=["s"])
	tt.define_struct_fields(tid, field_types=[string_ty])
	non_copy = {tid}
	tt._copy_query = lambda t: False if t in non_copy else None  # type: ignore[attr-defined]
	return tid


# ── R1 — entry zero-storage initialization ────────────────────────────


def test_r1_entry_zero_init_groups_order_and_exclusions() -> None:
	tt = TypeTable()
	string_ty = tt.ensure_string()
	arr_ty = tt.new_array(string_ty)
	ns_ty = _nullsafe_struct(tt)
	int_ty = tt.ensure_int()
	func = _make_func(
		"r1",
		params=["p"],
		# declaration order deliberately interleaved: groups must come out
		# as (strings, arrays, nullsafe-destructibles), each in func.locals
		# order, params excluded, POD untouched.
		locals_=["p", "n1", "a1", "s2", "s1", "i", "a0"],
		types={
			"p": string_ty, "n1": ns_ty, "a1": arr_ty, "s2": string_ty,
			"s1": string_ty, "i": int_ty, "a0": arr_ty,
		},
	)
	entry = M.BasicBlock(name="entry")
	orig = M.ConstInt(dest="%c", value=1) if hasattr(M, "ConstInt") else M.ConstString(dest="%c", value="x")
	entry.instructions = [orig]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	normalize_ownership_mir(func, type_table=tt, fn_infos={})

	ins = func.blocks["entry"].instructions
	# Zero-init pairs: strings (s2, s1 — locals order), arrays (a1, a0),
	# nullsafe (n1) — then the original instruction, BY IDENTITY.
	zeroed = []
	i = 0
	while i + 1 < len(ins) and isinstance(ins[i], M.ZeroValue) and isinstance(ins[i + 1], M.StoreLocal):
		assert ins[i + 1].value == ins[i].dest
		zeroed.append((ins[i + 1].local, ins[i].ty))
		i += 2
	assert zeroed == [
		("s2", string_ty), ("s1", string_ty),
		("a1", arr_ty), ("a0", arr_ty),
		("n1", ns_ty),
	], zeroed
	assert ins[i] is orig, "original instruction must survive by identity"
	# Param and POD local excluded.
	assert all(l != "p" and l != "i" for l, _ in zeroed)
	# Zero temps seeded.
	for k in range(len(zeroed)):
		assert func.local_types[f"__arc{k + 1}"] == zeroed[k][1]


def test_r1_non_nullsafe_destructible_not_zero_inited() -> None:
	"""A destructor-bearing (non-null-safe) destructible is NOT zero-inited
	— its scope-exit cleanup is flag-managed/authored."""
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("r1n", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	normalize_ownership_mir(func, type_table=tt, fn_infos={})
	assert func.blocks["entry"].instructions == []


# ── R5 — MoveOut expansion ────────────────────────────────────────────


def test_r5_moveout_expansion_shape_and_seeding() -> None:
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)
	func = _make_func("r5", locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	store = M.StoreLocal(local="x", value="t0")
	mo = M.MoveOut(dest="%m", local="x", ty=drop_ty)
	drop = M.DropValue(value="%m", ty=drop_ty)
	entry.instructions = [store, mo, drop]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	normalize_ownership_mir(func, type_table=tt, fn_infos={})
	ins = func.blocks["entry"].instructions
	assert ins[0] is store
	assert isinstance(ins[1], M.LoadLocal) and ins[1].dest == "%m" and ins[1].local == "x"
	assert isinstance(ins[2], M.ZeroValue) and ins[2].ty == drop_ty
	zb = ins[3]
	assert isinstance(zb, M.StoreLocal) and zb.local == "x" and zb.value == ins[2].dest
	assert getattr(zb, "synthetic_zero_back", False) is True
	assert ins[4] is drop
	assert func.local_types["%m"] == drop_ty
	assert func.local_types[ins[2].dest] == drop_ty


def test_r5_audit_note_original_anchor_and_pairing() -> None:
	"""The `moveout_expansion` note anchors at the ORIGINAL source index
	and snapshots the next-instruction DropValue pairing from the SOURCE
	stream — paired vs unpaired distinguished."""
	from lang.driftc.stage2 import ownership_ledger_reporter as R
	tt = TypeTable()
	drop_ty = _droppable_struct(tt)

	def run(paired: bool):
		func = _make_func("r5a", locals_=["x"], types={"x": drop_ty})
		entry = M.BasicBlock(name="entry")
		mo = M.MoveOut(dest="%m", local="x", ty=drop_ty)
		tail = [M.DropValue(value="%m", ty=drop_ty)] if paired else []
		entry.instructions = [M.StoreLocal(local="x", value="t0"), mo] + tail
		entry.terminator = M.Return(value=None)
		func.blocks = {"entry": entry}
		func.entry = "entry"
		audit = R.StringArcAudit(func.name)
		normalize_ownership_mir(func, type_table=tt, fn_infos={}, audit_collector=audit)
		evs = [e for e in audit.events if e.site_class == R.SITE_CLASS_MOVEOUT_EXPANSION]
		assert len(evs) == 1
		return evs[0]

	ev = run(True)
	assert ev.pre_point == ("entry", 1) and ev.moveout_feeds_drop is True
	ev = run(False)
	assert ev.pre_point == ("entry", 1) and ev.moveout_feeds_drop is False


# ── identity pass-through ─────────────────────────────────────────────


def test_identity_pass_through_and_return_untouched() -> None:
	"""Every non-MoveOut instruction survives BY OBJECT IDENTITY — never
	reconstructed — and the Return terminator object is untouched."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	fn_id = FunctionId(module="test", name="callee", ordinal=0)
	func = _make_func("ident", locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	originals = [
		M.ConstString(dest="%a", value="x"),
		M.Call(dest="%r", fn_id=fn_id, args=["%a"], can_throw=False),
		M.StringConcat(dest="%c", left="%a", right="%a"),
		M.StoreRef(ptr="%p", value="%c", inner_ty=string_ty),
	]
	entry.instructions = list(originals)
	term = M.Return(value="%r")
	entry.terminator = term
	func.blocks = {"entry": entry}
	func.entry = "entry"
	func.local_types.update({"%a": string_ty, "%c": string_ty, "%r": string_ty, "%p": string_ty})
	normalize_ownership_mir(func, type_table=tt, fn_infos={})
	assert [id(i) for i in func.blocks["entry"].instructions] == [id(i) for i in originals]
	assert func.blocks["entry"].terminator is term


# ── table-driven local_types seeding contract ─────────────────────────


def _seed_case_load_local(tt, st):
	return [M.StoreLocal(local="l", value="%v"), M.LoadLocal(dest="%d", local="l")], {"l": st}, {"%d": st}


def _seed_case_load_ref(tt, st):
	return [M.LoadRef(dest="%d", ptr="%p", inner_ty=st)], {}, {"%d": st}


def _seed_case_struct_get_field_nonstring(tt, st):
	int_ty = tt.ensure_int()
	return [M.StructGetField(dest="%d", subject="%o", struct_ty=st, field_index=0, field_ty=int_ty)], {}, {"%d": int_ty}


def _seed_case_variant_get_field(tt, st):
	return [M.VariantGetField(dest="%d", variant="%o", variant_ty=st, ctor="A", field_index=0, field_ty=st)], {}, {"%d": st}


def _seed_case_array_index_load(tt, st):
	return [M.ArrayIndexLoad(dest="%d", elem_ty=st, array="%arr", index="%i")], {}, {"%d": st}


def _seed_case_array_index_load_unchecked(tt, st):
	return [M.ArrayIndexLoadUnchecked(dest="%d", elem_ty=st, array="%arr", index="%i")], {}, {"%d": st}


def _seed_case_array_elem_take(tt, st):
	return [M.ArrayElemTake(dest="%d", elem_ty=st, array="%arr", index="%i")], {}, {"%d": st}


def _seed_case_ptr_read(tt, st):
	return [M.PtrRead(dest="%d", ptr="%p", elem_ty=st)], {}, {"%d": st}


def _seed_case_raw_buffer_read(tt, st):
	return [M.RawBufferRead(dest="%d", buffer="%b", raw_ty=st, elem_ty=st, index="%i")], {}, {"%d": st}


def _seed_case_moveout(tt, st):
	drop_ty = _droppable_struct(tt)
	return [M.MoveOut(dest="%d", local="x", ty=drop_ty)], {"x": drop_ty}, {"%d": drop_ty}


def _seed_case_prescan_zero_value_string(tt, st):
	return [M.ZeroValue(dest="%d", ty=st)], {}, {"%d": st}


def _seed_case_prescan_array_load_string_gap(tt, st):
	# dest ABSENT from local_types: the prescan (only-if-missing) fills it.
	return [M.ArrayIndexLoadUnchecked(dest="%gap", elem_ty=st, array="%arr", index="%i")], {}, {"%gap": st}


_SEED_CASES = [
	("load_local", _seed_case_load_local),
	("load_ref", _seed_case_load_ref),
	("struct_get_field_all_types", _seed_case_struct_get_field_nonstring),
	("variant_get_field", _seed_case_variant_get_field),
	("array_index_load", _seed_case_array_index_load),
	("array_index_load_unchecked", _seed_case_array_index_load_unchecked),
	("array_elem_take", _seed_case_array_elem_take),
	("ptr_read", _seed_case_ptr_read),
	("raw_buffer_read", _seed_case_raw_buffer_read),
	("moveout_dest", _seed_case_moveout),
	("prescan_zero_value_string", _seed_case_prescan_zero_value_string),
	("prescan_array_load_string_gap", _seed_case_prescan_array_load_string_gap),
]


@pytest.mark.parametrize("label,case", _SEED_CASES, ids=[l for l, _ in _SEED_CASES])
def test_local_types_seeding_table(label, case) -> None:
	"""TABLE-DRIVEN seeding contract, ABSENT-destination axis: for every
	inventoried instruction family the pass seeds EXACTLY the expected
	`local_types` delta (beyond its own `__arc*` zero temps) when the
	destination has no pre-existing binding, and leaves the fixtures'
	OTHER pre-existing bindings unchanged.  (The pre-existing-destination
	behavior — unconditional OVERWRITE for the instruction-carried
	families vs only-if-missing PRESERVATION for the prescan families —
	is pinned separately below.)"""
	tt = TypeTable()
	st = tt.ensure_string()
	instrs, pre_types, expected_delta = case(tt, st)
	func = _make_func(f"seed_{label}", locals_=list(pre_types), types=pre_types)
	entry = M.BasicBlock(name="entry")
	entry.instructions = list(instrs)
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	before = dict(func.local_types)
	normalize_ownership_mir(func, type_table=tt, fn_infos={})
	after = dict(func.local_types)
	# The fixtures' non-destination pre-existing bindings are unchanged.
	for k, v in before.items():
		assert after[k] == v, (label, k)
	# Exact delta (modulo the pass's own __arc zero temps).
	delta = {k: v for k, v in after.items() if k not in before and not k.startswith("__arc")}
	assert delta == expected_delta, (label, delta, expected_delta)


def _run_seed_probe(tt, instrs, pre_types):
	"""Compile a one-block fixture through the pass and return the final
	local_types.  `unrelated` is a noninterference control binding every
	probe carries."""
	int_ty = tt.ensure_int()
	types = dict(pre_types)
	types["unrelated"] = int_ty
	func = _make_func("seed_probe", locals_=list(types), types=types)
	entry = M.BasicBlock(name="entry")
	entry.instructions = list(instrs)
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	normalize_ownership_mir(func, type_table=tt, fn_infos={})
	assert func.local_types["unrelated"] == int_ty, (
		"noninterference control: an unrelated pre-existing binding moved"
	)
	return dict(func.local_types)


def test_seeding_instruction_carried_families_overwrite_stale_binding() -> None:
	"""OVERWRITE axis: the instruction-carried families assign the
	destination type UNCONDITIONALLY — a stale pre-existing destination
	binding is corrected to the instruction-carried type (the historical
	rewrite-loop semantics, now a first-class contract)."""
	tt = TypeTable()
	st = tt.ensure_string()
	int_ty = tt.ensure_int()
	# StructGetField carries field_ty=Int; the dest arrives STALE-bound
	# to String and must be overwritten to Int.
	after = _run_seed_probe(tt, [
		M.StructGetField(dest="%d", subject="%o", struct_ty=st, field_index=0, field_ty=int_ty),
	], {"%d": st})
	assert after["%d"] == int_ty, "StructGetField must overwrite a stale dest binding"
	# LoadRef: inner_ty=String over a stale Int binding.
	after = _run_seed_probe(tt, [
		M.LoadRef(dest="%d", ptr="%p", inner_ty=st),
	], {"%d": int_ty})
	assert after["%d"] == st, "LoadRef must overwrite a stale dest binding"
	# LoadLocal copies the LOCAL's current type over a stale dest binding.
	after = _run_seed_probe(tt, [
		M.StoreLocal(local="l", value="%v"),
		M.LoadLocal(dest="%d", local="l"),
	], {"l": st, "%d": int_ty})
	assert after["%d"] == st, "LoadLocal must overwrite a stale dest binding"


def test_seeding_prescan_families_preserve_existing_binding() -> None:
	"""ONLY-IF-MISSING axis: the prescan families (String `ZeroValue`,
	String array-load registration) fill a MISSING destination binding but
	PRESERVE an existing one — they are gap-fillers, not correctors.

	(A String-typed ZeroValue dest pre-bound to another type is the probe:
	the prescan's `dest not in local_types` guard must leave it alone.
	Note the ArrayIndexLoad* families appear in BOTH axes: the prescan
	fills gaps early, and the rewrite-loop arm later overwrites
	unconditionally — so only ZeroValue can pin preservation end-to-end.)"""
	tt = TypeTable()
	st = tt.ensure_string()
	int_ty = tt.ensure_int()
	# Pre-bound dest: the String-ZeroValue prescan must NOT overwrite it.
	after = _run_seed_probe(tt, [
		M.ZeroValue(dest="%z", ty=st),
	], {"%z": int_ty})
	assert after["%z"] == int_ty, (
		"String-ZeroValue prescan must preserve an existing dest binding "
		"(only-if-missing)"
	)
	# Missing dest: the same prescan fills the gap.
	after = _run_seed_probe(tt, [
		M.ZeroValue(dest="%z", ty=st),
	], {})
	assert after["%z"] == st, "String-ZeroValue prescan must fill a missing binding"


# ── R8 closed vessel at the new consumer ──────────────────────────────


def _mr_func(tt, name="mr"):
	string_ty = tt.ensure_string()
	func = _make_func(name, locals_=[], types={"%a": string_ty, "%b": string_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%a", value="a"),
		M.ConstString(dest="%b", value="b"),
		M.StringEq(dest="%e", left="%a", right="%b"),
		M.StringRelease(value="%a"),
		M.StringRelease(value="%b"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	return func


def test_r8_copy_through_identity_and_note() -> None:
	from lang.driftc.stage2 import ownership_ledger_reporter as R
	tt = TypeTable()
	func = _mr_func(tt)
	rel_ids = [id(i) for i in func.blocks["entry"].instructions if isinstance(i, M.StringRelease)]
	r8 = compute_recognized_releases(func, type_table=tt, fn_infos={})
	assert r8.for_block("entry") == frozenset({"%a", "%b"})
	audit = R.StringArcAudit(func.name)
	normalize_ownership_mir(func, type_table=tt, fn_infos={}, audit_collector=audit, r8_recognition=r8)
	out_rel_ids = [id(i) for i in func.blocks["entry"].instructions if isinstance(i, M.StringRelease)]
	assert out_rel_ids == rel_ids, "recognized releases must pass through by identity"
	notes = [e for e in audit.events if e.site_class == R.SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE]
	assert len(notes) == 2 and {e.subject for e in notes} == {"%a", "%b"}


def test_r8_vessel_fail_closed_at_new_consumer() -> None:
	tt = TypeTable()
	func = _mr_func(tt, "vf")
	with pytest.raises(AssertionError, match="wrong-function recognition"):
		normalize_ownership_mir(func, type_table=tt, fn_infos={},
			r8_recognition=R8Recognition(fn_name="test::OTHER", recognized_by_block={}))
	with pytest.raises(AssertionError, match="block set != function block set"):
		normalize_ownership_mir(func, type_table=tt, fn_infos={},
			r8_recognition=R8Recognition(fn_name=func.name, recognized_by_block={}))


# ── dirty-mark iff changed ────────────────────────────────────────────


def test_ledger_dirty_iff_stream_changed() -> None:
	tt = TypeTable()
	string_ty = tt.ensure_string()
	# No strings/arrays/nullsafe locals, no MoveOut → NOTHING changes; the
	# ledger must stay clean (no fake mutation for metadata/audit work).
	func = _make_func("clean", locals_=[], types={})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.ConstString(dest="%a", value="x")]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	setattr(func, "_ownership_ledger", object())
	setattr(func, "_ledger_dirty_reason", None)
	normalize_ownership_mir(func, type_table=tt, fn_infos={})
	assert getattr(func, "_ledger_dirty_reason") is None
	# A String local (R1 zero-init) DOES change the stream → dirty.
	func2 = _make_func("dirty", locals_=["s"], types={"s": string_ty})
	e2 = M.BasicBlock(name="entry")
	e2.terminator = M.Return(value=None)
	func2.blocks = {"entry": e2}
	func2.entry = "entry"
	setattr(func2, "_ownership_ledger", object())
	setattr(func2, "_ledger_dirty_reason", None)
	normalize_ownership_mir(func2, type_table=tt, fn_infos={})
	assert getattr(func2, "_ledger_dirty_reason") == "ownership_normalization.rewrite_block"


# ── Phase D — deletion pins ───────────────────────────────────────────


def test_string_arc_module_is_gone() -> None:
	"""Phase D file-absence pin: `lang/driftc/stage2/string_arc.py` does
	not exist and the module cannot be imported — the normalization pass
	is its sole permanent successor."""
	import importlib.util
	from pathlib import Path
	import lang.driftc.stage2.ownership_normalization as _norm
	stage2 = Path(_norm.__file__).resolve().parent
	assert not (stage2 / "string_arc.py").exists(), (
		"string_arc.py must stay deleted (Phase D)"
	)
	assert importlib.util.find_spec("lang.driftc.stage2.string_arc") is None, (
		"lang.driftc.stage2.string_arc must not be importable"
	)


def test_no_production_string_arc_import() -> None:
	"""Phase D residual-reference pin: no production module under
	lang/driftc or lang/codegen imports (or names in an import) the
	deleted `string_arc` module.  Prose/comment mentions and the retained
	historical tags (`StringArcAudit`, `DRIFT_STRING_ARC_AUDIT`,
	`string_arc_managed`, `string_arc_return`) are allowed; imports are
	not."""
	import ast
	from pathlib import Path
	import lang.driftc as _ld
	roots = [
		Path(_ld.__file__).resolve().parent,
		Path(_ld.__file__).resolve().parent.parent / "codegen",
	]
	offenders = []
	visited = 0
	for root in roots:
		for py in root.rglob("*.py"):
			if "tests" in py.parts:
				continue
			visited += 1
			tree = ast.parse(py.read_text())
			for node in ast.walk(tree):
				if isinstance(node, ast.ImportFrom):
					mod = node.module or ""
					if mod.split(".")[-1] == "string_arc":
						offenders.append(f"{py.name}:{node.lineno}")
					elif any(a.name == "string_arc" for a in node.names):
						offenders.append(f"{py.name}:{node.lineno}")
				elif isinstance(node, ast.Import):
					if any(a.name.split(".")[-1] == "string_arc" for a in node.names):
						offenders.append(f"{py.name}:{node.lineno}")
	assert visited > 50, f"sweep visited only {visited} files (wrong roots?)"
	assert not offenders, f"production string_arc imports remain: {offenders}"
