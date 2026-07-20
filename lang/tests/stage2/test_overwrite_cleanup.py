# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice B1 — overwrite_cleanup pass pins (R2 String overwrite releases
+ R7 Array overwrite drops), string-arc-endgame-cleanup-authority.

The pass runs AFTER string_arc; these unit pins exercise it directly
on hand-built MIR (post-string_arc shapes).  Coverage: PROVENANCE
(marked synthetic zero-back skipped; UNMARKED input ZeroValue store
still releases its live old value), exactly-one authored cleanup with
the canonical sequence + order, the structural validator's TEETH,
self-alias retain<release<store order, all four R2 kinds, R7 array,
and the strict counted-only recorder (allow-list + positive-int).
"""
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2 import overwrite_cleanup as OC
from lang.driftc.stage2.overwrite_cleanup import insert_overwrite_cleanup


def _make_func(name, *, params, locals_, types):
	return M.MirFunc(
		name=f"test::{name}",
		params=list(params),
		locals=list(locals_),
		fn_id=FunctionId(module="test", name=name, ordinal=0),
		local_types=dict(types),
	)


def _seq(func):
	return [(bn, type(ins).__name__, ins)
		for bn, blk in func.blocks.items() for ins in blk.instructions]


def _kinds(func):
	return [t for _b, t, _i in _seq(func)]


def _releases(func):
	return [ins for _b, t, ins in _seq(func) if t == "StringRelease"]


def _arraydrops(func):
	return [ins for _b, t, ins in _seq(func) if t == "ArrayDrop"]


def _marked_zero(store):
	setattr(store, "synthetic_zero_back", True)
	return store


# ── PROVENANCE (finding #1, both directions) ──────────────────────────


def test_marked_synthetic_zeroback_is_skipped() -> None:
	"""A string_arc-marked synthetic zero-back StoreLocal is NOT an
	overwrite — the pass skips it (no release, no load-before-store)."""
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("mz", params=[], locals_=["x"], types={"x": sty})
	entry = M.BasicBlock(name="entry")
	zb = _marked_zero(M.StoreLocal(local="x", value="%z"))
	entry.instructions = [M.ZeroValue(dest="%z", ty=sty), zb]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	insert_overwrite_cleanup(func, type_table=tt)
	assert _releases(func) == [], _kinds(func)


def test_unmarked_input_zerovalue_string_store_still_releases() -> None:
	"""The decisive provenance case (review): an UNMARKED input
	`ZeroValue(String) -> StoreLocal` into a live slot IS a real
	overwrite — the pass must release the old value (shape alone is
	NOT provenance)."""
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("uz", params=[], locals_=["x"], types={"x": sty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%c", value="live"),
		M.StoreLocal(local="x", value="%c"),          # x now live
		M.ZeroValue(dest="%z", ty=sty),
		M.StoreLocal(local="x", value="%z"),          # UNMARKED zero store — real overwrite
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	insert_overwrite_cleanup(func, type_table=tt)
	# BOTH stores are input overwrites → two releases.
	assert len(_releases(func)) == 2, _kinds(func)


def test_unmarked_input_zerovalue_array_store_still_drops() -> None:
	"""Array analog: an unmarked input `ZeroValue(Array) -> StoreLocal`
	overwrite still drops the old array."""
	tt = TypeTable()
	sty = tt.ensure_string()
	arr_ty = tt.new_array(sty)
	func = _make_func("uza", params=[], locals_=["a"], types={"a": arr_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ArrayLit(dest="%lit", elem_ty=sty, elements=[]),
		M.StoreLocal(local="a", value="%lit"),        # a live
		M.ZeroValue(dest="%z", ty=arr_ty),
		M.StoreLocal(local="a", value="%z"),          # UNMARKED — real overwrite
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	insert_overwrite_cleanup(func, type_table=tt)
	assert len(_arraydrops(func)) == 2, _kinds(func)


# ── canonical sequence + order ────────────────────────────────────────


def test_r2_storelocal_canonical_sequence_and_order() -> None:
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("sl", params=[], locals_=["x"], types={"x": sty})
	entry = M.BasicBlock(name="entry")
	zb = _marked_zero(M.StoreLocal(local="x", value="%z"))
	entry.instructions = [
		M.ZeroValue(dest="%z", ty=sty), zb,          # synthetic init — skipped
		M.ConstString(dest="%c", value="hi"),
		M.StoreLocal(local="x", value="%c"),         # user overwrite
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	insert_overwrite_cleanup(func, type_table=tt)
	assert _kinds(func) == [
		"ZeroValue", "StoreLocal",                    # untouched synthetic init
		"ConstString",
		"LoadLocal", "ZeroValue", "StoreLocal", "StringRelease",  # cleanup
		"StoreLocal",                                 # user store, AFTER cleanup
	], _kinds(func)


def test_r2_self_alias_retain_before_release_before_store() -> None:
	"""Self-alias `x = x`: exact order CopyValue (upstream retain) <
	StringRelease (old) < user StoreLocal.  Guards against the release
	moving before the retain (which would UAF)."""
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("sa", params=["x"], locals_=["x"], types={"x": sty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.CopyValue(dest="%cp", value="x", ty=sty),  # upstream store-value retain
		M.StoreLocal(local="x", value="%cp"),        # user self-store
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	insert_overwrite_cleanup(func, type_table=tt)
	kinds = _kinds(func)
	i_copy = kinds.index("CopyValue")
	i_rel = kinds.index("StringRelease")
	i_store = len(kinds) - 1 - kinds[::-1].index("StoreLocal")  # the USER store
	assert i_copy < i_rel < i_store, kinds


def test_r7_canonical_sequence_and_order() -> None:
	tt = TypeTable()
	sty = tt.ensure_string()
	arr_ty = tt.new_array(sty)
	func = _make_func("r7", params=[], locals_=["a"], types={"a": arr_ty})
	entry = M.BasicBlock(name="entry")
	zb = _marked_zero(M.StoreLocal(local="a", value="%z"))
	entry.instructions = [
		M.ZeroValue(dest="%z", ty=arr_ty), zb,        # synthetic init — skipped
		M.ArrayLit(dest="%lit", elem_ty=sty, elements=[]),
		M.StoreLocal(local="a", value="%lit"),        # user overwrite
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	insert_overwrite_cleanup(func, type_table=tt)
	assert _kinds(func) == [
		"ZeroValue", "StoreLocal",                    # untouched synthetic init
		"ArrayLit",
		"LoadLocal", "ZeroValue", "StoreLocal", "ArrayDrop",  # cleanup
		"StoreLocal",                                 # user store, AFTER cleanup
	], _kinds(func)


def test_r2_all_four_instruction_kinds() -> None:
	tt = TypeTable()
	sty = tt.ensure_string()
	arr_ty = tt.new_array(sty)
	func = _make_func("k4", params=["p", "arr"], locals_=["x", "p", "arr"],
		types={"x": sty, "p": tt.ensure_int(), "arr": arr_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%c", value="v"),
		M.StoreLocal(local="x", value="%c"),
		M.MoveFromRef(local="x", ptr="p", inner_ty=sty),
		M.StoreRef(ptr="p", value="%c", inner_ty=sty),
		M.ArrayIndexStore(elem_ty=sty, array="arr", index="p", value="%c"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	insert_overwrite_cleanup(func, type_table=tt)
	assert len(_releases(func)) == 4, _kinds(func)


# ── validator TEETH (finding #2) ──────────────────────────────────────


def _one_store_func():
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("v", params=[], locals_=["x"], types={"x": sty})
	store = M.StoreLocal(local="x", value="%c")
	return tt, sty, func, store


def _canonical_release(store, sty, *, tag=True, old="%old", zero="%z", release_val=None,
	zero_ty=None, zstore_val=None):
	"""Build a canonical StoreLocal-release quadruple ending in the tagged release."""
	load = M.LoadLocal(dest=old, local=store.local)
	zv = M.ZeroValue(dest=zero, ty=(zero_ty if zero_ty is not None else sty))
	zstore = M.StoreLocal(local=store.local, value=(zstore_val if zstore_val is not None else zero))
	rel = M.StringRelease(value=(release_val if release_val is not None else old))
	if tag:
		setattr(rel, "ow_authored_for", id(store))
	return [load, zv, zstore, rel]


def test_validator_missing_authoring_raises() -> None:
	"""Inventoried store with NO authored cleanup → fail-closed."""
	tt, sty, func, store = _one_store_func()
	entry = M.BasicBlock(name="entry")
	entry.instructions = [M.ConstString(dest="%c", value="v"), store]  # bare store, no cleanup
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	inv = {id(store): (OC._K_STORE_LOCAL, store)}
	with pytest.raises(AssertionError, match="received no authored cleanup"):
		OC._validate(func, tt, inv)


def test_validator_duplicate_authoring_raises() -> None:
	"""TWO tagged cleanups for one site → fail-closed (not exactly one)."""
	tt, sty, func, store = _one_store_func()
	entry = M.BasicBlock(name="entry")
	entry.instructions = (
		_canonical_release(store, sty, old="%o1", zero="%z1")
		+ _canonical_release(store, sty, old="%o2", zero="%z2")
		+ [store]
	)
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	inv = {id(store): (OC._K_STORE_LOCAL, store)}
	with pytest.raises(AssertionError, match="duplicate authoring"):
		OC._validate(func, tt, inv)


def test_validator_orphan_authoring_raises() -> None:
	"""A tagged cleanup targeting no inventoried site → fail-closed."""
	tt, sty, func, store = _one_store_func()
	rel = M.StringRelease(value="%old")
	setattr(rel, "ow_authored_for", 999999)  # no such site
	entry = M.BasicBlock(name="entry")
	entry.instructions = _canonical_release(store, sty) + [store, rel]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	inv = {id(store): (OC._K_STORE_LOCAL, store)}
	with pytest.raises(AssertionError, match="orphan authoring"):
		OC._validate(func, tt, inv)


def test_validator_broken_zerovalue_type_link_raises() -> None:
	"""Cleanup whose ZeroValue.ty is not String → type mismatch,
	fail-closed."""
	tt, sty, func, store = _one_store_func()
	entry = M.BasicBlock(name="entry")
	# zero_ty = Int (wrong) instead of String
	entry.instructions = _canonical_release(store, sty, zero_ty=tt.ensure_int()) + [store]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	inv = {id(store): (OC._K_STORE_LOCAL, store)}
	with pytest.raises(AssertionError, match="operand/type mismatch"):
		OC._validate(func, tt, inv)


def test_validator_broken_release_operand_link_raises() -> None:
	"""Cleanup whose StringRelease.value is not the LoadLocal.dest →
	operand mismatch, fail-closed."""
	tt, sty, func, store = _one_store_func()
	entry = M.BasicBlock(name="entry")
	entry.instructions = _canonical_release(store, sty, release_val="%unrelated") + [store]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	inv = {id(store): (OC._K_STORE_LOCAL, store)}
	with pytest.raises(AssertionError, match="operand/type mismatch"):
		OC._validate(func, tt, inv)


def test_validator_storeref_wrong_inner_ty_raises() -> None:
	"""StoreRef cleanup whose LoadRef.inner_ty ≠ the store's → mismatch."""
	tt = TypeTable()
	sty = tt.ensure_string()
	func = _make_func("srx", params=["p"], locals_=["p"], types={"p": tt.ensure_int()})
	store = M.StoreRef(ptr="p", value="%c", inner_ty=sty)
	load = M.LoadRef(dest="%old", ptr="p", inner_ty=tt.ensure_int())  # WRONG inner_ty
	rel = M.StringRelease(value="%old")
	setattr(rel, "ow_authored_for", id(store))
	entry = M.BasicBlock(name="entry")
	entry.instructions = [load, rel, store]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	inv = {id(store): (OC._K_STORE_REF, store)}
	with pytest.raises(AssertionError, match="operand/type mismatch"):
		OC._validate(func, tt, inv)


# ── strict counted-only recorder ──────────────────────────────────────


def test_recorder_exact_key_delta_and_isolation() -> None:
	"""The recorder folds EXACTLY `events` + `site_class:overwrite_release`
	into the aggregate — never `fns`, `skipped_no_ledger`, or any
	C1/C2/C3 key."""
	from lang.driftc.stage2 import ownership_ledger_reporter as R
	before = dict(R._GLOBAL_AGGREGATE)
	R.record_counted_only(R.SITE_CLASS_OVERWRITE_RELEASE, 5)
	after = dict(R._GLOBAL_AGGREGATE)
	delta = {k: after.get(k, 0) - before.get(k, 0)
		for k in set(before) | set(after)}
	nonzero = {k: v for k, v in delta.items() if v != 0}
	assert nonzero == {"events": 5, "site_class:overwrite_release": 5}, nonzero
	for forbidden in ("fns", "skipped_no_ledger", "c1_agree",
		"c2_invisible_stake", "c3_moveout_owned"):
		assert delta.get(forbidden, 0) == 0
	# restore
	R._GLOBAL_AGGREGATE.clear()
	R._GLOBAL_AGGREGATE.update(before)


def test_recorder_allow_list_and_positive_int() -> None:
	from lang.driftc.stage2 import ownership_ledger_reporter as R
	with pytest.raises(AssertionError):
		R.record_counted_only(R.SITE_CLASS_SCOPE_EXIT_RELEASE, 1)  # not allow-listed
	for bad in (0, -1, 1.5, True):
		with pytest.raises(AssertionError):
			R.record_counted_only(R.SITE_CLASS_OVERWRITE_RELEASE, bad)


# ── driver-boundary containment ───────────────────────────────────────


def test_overwrite_cleanup_boundary_wrap_contains_assertions(tmp_path, monkeypatch) -> None:
	"""An AssertionError from overwrite_cleanup surfaces as a clean
	`internal:` diagnostic (phase overwrite_cleanup, empty MIR, no
	traceback), never a Python traceback."""
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc import driftc as D
	from lang.driftc.core.function_id import function_symbol

	src = tmp_path / "main.drift"
	src.write_text("module main;\n\npub fn main() nothrow -> Int {\n\tvar s = \"a\";\n\ts = \"b\";\n\tif s.byte_length() > 0 { return 0; }\n\treturn 1;\n}\n")
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	origin = {}
	for m in modules.values():
		origin.update(m.origin_by_fn_id)

	import lang.driftc.stage2.overwrite_cleanup as OCmod
	_real = OCmod.insert_overwrite_cleanup
	def _boom(func, **kw):
		if getattr(func, "name", "") == "main":
			raise AssertionError("overwrite cleanup contract failure: injected-for-pin")
		return _real(func, **kw)
	monkeypatch.setattr(OCmod, "insert_overwrite_cleanup", _boom)

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
	assert errors, "injected overwrite_cleanup failure must surface as a diagnostic"
	msgs = [d.message for d in errors]
	assert any("internal: overwrite cleanup contract failure" in m and "injected-for-pin" in m for m in msgs), msgs
	assert any(getattr(d, "phase", None) == "overwrite_cleanup" for d in errors), [
		(d.message, getattr(d, "phase", None)) for d in errors
	]
	assert ir == "", "compile must not produce IR after a contract failure"
