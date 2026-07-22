# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B2+C S6 teeth — R8 materialized-release recognition re-homed onto the
pre-string_arc PLANNING WINDOW.

The recognition (`build_fnwide_producers` + `compute_string_temp_liveness`
+ per-block `recognize_materialized_releases`) is computed ONCE at the
plan window over the ORIGINAL MIR and CONSUMED by string_arc's rewrite
loop.  These pins lock:
  * the plan-window wrapper == the direct per-block analysis (equivalence);
  * string_arc CONSUMING the frozen recognition == the bare-invocation
    FALLBACK (which recomputes via the same single entry point) — identical
    output MIR (copy-through arm still correct);
  * string_arc no longer OWNS recognition (source pin: its body invokes
    none of the three underlying analyses);
  * fail-closed on a wrong-function recognition.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.string_arc import insert_string_arc
from lang.driftc.stage2.string_ownership_analysis import (
	R8Recognition,
	build_fnwide_producers,
	compute_recognized_releases,
	compute_string_temp_liveness,
	recognize_materialized_releases,
	seed_string_dest_types,
)


def _materialized_release_func(tt: TypeTable, name: str = "mr") -> M.MirFunc:
	"""`%a`, `%b` are family (ConstString) temps drained at a StringEq, each
	with an IN-CONTRACT materialized StringRelease immediately after the
	drain — the exact shape `materialize_lastuse_releases` produces."""
	string_ty = tt.ensure_string()
	fn_id = FunctionId(module="test", name=name, ordinal=0)
	func = M.MirFunc(
		name=f"test::{name}", params=[], locals=[], fn_id=fn_id,
		local_types={"%a": string_ty, "%b": string_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions = [
		M.ConstString(dest="%a", value="a"),
		M.ConstString(dest="%b", value="b"),
		M.StringEq(dest="%e", left="%a", right="%b"),   # last use of %a and %b
		M.StringRelease(value="%a"),                     # materialized (in-contract)
		M.StringRelease(value="%b"),
	]
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	func.entry = "entry"
	return func


def test_r8_plan_window_matches_direct_recognition():
	"""The plan-window wrapper's per-block set equals the direct
	`recognize_materialized_releases` output over the seeded-copy inputs."""
	tt = TypeTable()
	string_ty = tt.ensure_string()
	func = _materialized_release_func(tt)
	r8 = compute_recognized_releases(func, type_table=tt, fn_infos={})
	assert isinstance(r8, R8Recognition)
	assert r8.fn_name == func.name

	# Reconstruct the direct analysis exactly as the wrapper does internally.
	lt = dict(func.local_types)
	seed_string_dest_types([func.blocks["entry"]], lt, fn_infos={}, type_table=tt)
	producers = build_fnwide_producers([func.blocks["entry"]])
	live = compute_string_temp_liveness(
		func.blocks, ["entry"], local_types=lt, string_ty=string_ty)
	direct = recognize_materialized_releases(
		func.blocks["entry"], local_types=lt, fn_infos={}, type_table=tt,
		live_out_names=live.get("entry", set()), producers_fnwide=producers)
	assert r8.for_block("entry") == frozenset(direct)
	assert r8.for_block("entry") == frozenset({"%a", "%b"}), r8.for_block("entry")
	# S6 closure: an ABSENT block is a contract violation, never "nothing
	# recognized" — recognition records an explicit empty frozenset for
	# every release-free block of its function.
	with pytest.raises(AssertionError, match="fail closed"):
		r8.for_block("nonexistent")


def _kinds_and_releases(func: M.MirFunc):
	kinds = []
	releases = []
	for blk in func.blocks.values():
		for ins in blk.instructions:
			kinds.append(type(ins).__name__)
			if isinstance(ins, M.StringRelease):
				releases.append(ins.value)
	return kinds, releases


def test_r8_consume_equals_fallback_identical_output():
	"""string_arc CONSUMING a frozen plan-window recognition produces the
	IDENTICAL output MIR to the bare-invocation FALLBACK (which recomputes
	via the same single entry point) — the copy-through arm preserves the
	materialized releases either way."""
	tt = TypeTable()
	func_fallback = _materialized_release_func(tt, "fb")
	func_consume = _materialized_release_func(tt, "cn")
	# Freeze recognition for the consume func BEFORE string_arc mutates it.
	r8 = compute_recognized_releases(func_consume, type_table=tt, fn_infos={})
	# Rebind fn_name so the wrong-function guard accepts it for `func_consume`.
	assert r8.fn_name == func_consume.name

	insert_string_arc(func_fallback, type_table=tt, fn_infos={})           # fallback recomputes
	insert_string_arc(func_consume, type_table=tt, fn_infos={}, r8_recognition=r8)  # consumes frozen

	k_fb, r_fb = _kinds_and_releases(func_fallback)
	k_cn, r_cn = _kinds_and_releases(func_consume)
	assert k_fb == k_cn, (k_fb, k_cn)
	assert r_fb == r_cn, (r_fb, r_cn)
	# Copy-through correctness: both preserved the two materialized releases.
	assert set(r_cn) == {"%a", "%b"}, r_cn


def test_string_arc_no_longer_owns_r8_recognition_source_pin():
	"""Structural pin (mirrors the S5 sole-finalize pin): string_arc.py
	INVOKES none of the three R8 analyses and does not import them — the
	single recognition entry point is `compute_recognized_releases`."""
	import lang.driftc.stage2.string_arc as _sa_mod
	src = Path(_sa_mod.__file__).read_text()
	for fn in (
		"recognize_materialized_releases(",
		"build_fnwide_producers(",
		"compute_string_temp_liveness(",
	):
		assert fn not in src, f"string_arc must not invoke {fn!r} (S6: recognition re-homed)"
	# And it must not import them (only the single entry point + the seeder).
	assert "compute_recognized_releases" in src
	assert "\tbuild_fnwide_producers," not in src
	assert "\tcompute_string_temp_liveness," not in src
	assert "\trecognize_materialized_releases," not in src


def test_r8_wrong_function_recognition_fails_closed():
	"""A frozen recognition whose fn_name does not match the func fails
	closed at consumption."""
	tt = TypeTable()
	func = _materialized_release_func(tt, "wf")
	bogus = R8Recognition(fn_name="test::SOMEONE_ELSE", recognized_by_block={})
	with pytest.raises(AssertionError, match="wrong-function recognition"):
		insert_string_arc(func, type_table=tt, fn_infos={}, r8_recognition=bogus)


# ── S6 closure — genuinely-immutable, complete, fail-closed vessel ────


def test_r8_vessel_is_genuinely_immutable():
	"""The frozen vessel's mapping is read-only AND copied at construction:
	neither direct mutation nor mutation through the caller's input-dict
	alias can change the recognition after freeze."""
	src = {"entry": frozenset({"%x"})}
	r8 = R8Recognition(fn_name="test::imm", recognized_by_block=src)
	with pytest.raises(TypeError):
		r8.recognized_by_block["entry"] = frozenset()  # type: ignore[index]
	with pytest.raises(TypeError):
		r8.recognized_by_block["new"] = frozenset()  # type: ignore[index]
	# Alias mutation after construction must not reach the vessel.
	src["entry"] = frozenset()
	src["ghost"] = frozenset({"%y"})
	assert r8.for_block("entry") == frozenset({"%x"})
	assert set(r8.recognized_by_block.keys()) == {"entry"}


def test_r8_malformed_value_rejected_at_construction():
	"""A non-frozenset value (or non-str key) is rejected when the vessel
	is built — malformed recognition can never exist frozen."""
	with pytest.raises(AssertionError, match="malformed entry"):
		R8Recognition(fn_name="test::bad", recognized_by_block={"entry": {"%a"}})
	with pytest.raises(AssertionError, match="malformed entry"):
		R8Recognition(fn_name="test::bad", recognized_by_block={"entry": ["%a"]})


def test_r8_missing_block_rejected_at_consumption():
	"""A supplied vessel whose block-key set is MISSING a function block is
	rejected before any rewrite (a missing block must never read as
	'nothing recognized')."""
	tt = TypeTable()
	func = _materialized_release_func(tt, "mb")
	hollow = R8Recognition(fn_name=func.name, recognized_by_block={})
	with pytest.raises(AssertionError, match="block set != function block set"):
		insert_string_arc(func, type_table=tt, fn_infos={}, r8_recognition=hollow)


def test_r8_extra_block_rejected_at_consumption():
	"""A supplied vessel carrying an EXTRA block (wrong/stale function
	shape) is rejected before any rewrite."""
	tt = TypeTable()
	func = _materialized_release_func(tt, "xb")
	good = compute_recognized_releases(func, type_table=tt, fn_infos={})
	padded = R8Recognition(
		fn_name=func.name,
		recognized_by_block={**dict(good.recognized_by_block), "ghost": frozenset()},
	)
	with pytest.raises(AssertionError, match="block set != function block set"):
		insert_string_arc(func, type_table=tt, fn_infos={}, r8_recognition=padded)
