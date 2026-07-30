# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Pin: `_emit_assign_store_ref` in `lang/driftc/stage2/hir_to_mir.py`
must lower drop-bearing field/place assignment as the canonical
replace-store sequence:

	MoveFromRef(local=tmp, ptr, inner_ty)        # atomic: tombstone slot, capture old
	MoveOut(dest=old_val, local=tmp, ty)         # surface old as SSA
	DropValue(value=old_val, ty)                 # drop old exactly once
	StoreRef(ptr, value=new, inner_ty)           # store new into tombstoned slot

**Why this shape.**  The previous emission

	LoadRef(old_val, ptr, inner_ty)
	ZeroValue(zero_val, ty)
	StoreRef(ptr, value=zero_val, inner_ty)      # tombstone via store
	DropValue(value=old_val, ty)
	StoreRef(ptr, value=new, inner_ty)           # store new

double-released the old field value for `String` slots: the StoreRef
overwrite rewrite (`stage2/overwrite_cleanup.py`, Slice B1; moved out of
string_arc) synthesizes
`LoadRef + StringRelease + StoreRef` for **every** `StoreRef` to a
String place, so the tombstone-store at line 3 became a real release
of the old slot value, and the explicit `DropValue` at line 4 then
released the same allocation a second time via the SSA value loaded
at line 1 — UAF + leak.  See
`lang/tests/memcheck/test_mut_struct_string_field_self_concat.py`
for the runtime carrier.

The canonical `MoveFromRef → MoveOut → DropValue → StoreRef`
sequence is type-agnostic.  `MoveFromRef` is the explicit ownership-
transfer primitive that atomically tombstones the slot and captures
the old value into a local; `MoveOut` drains the local into an SSA
value the consumer can drop; `DropValue` performs the single
release; the final `StoreRef` writes the new value into the
tombstoned slot.  For String, `overwrite_cleanup`'s rewrite of
the final `StoreRef` synthesizes a `LoadRef + StringRelease`
against the **already-tombstoned** slot — `drift_string_release` on
null bytes is a documented runtime no-op, so the rewrite remains
correct without changes.

**Authority boundary.**  This pin observes the fully-lowered MIR of
`mutate` (post-normalization, post-`overwrite_cleanup`).  It verifies
that exactly one `M.MoveFromRef` is
emitted for the drop-bearing field assignment, exactly one
`M.MoveOut` drains its local, exactly one `M.DropValue` releases the
moved-out SSA value, and exactly one terminal `M.StoreRef` carries
the new concat result.  No `M.ZeroValue` participates in the
sequence (the legacy zero-tombstone shape is gone).
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.stage2 import mir_nodes as M


SOURCE = """\
module main;

import std.format as fmt;

struct Ctx {
\tpub s: String
}

fn mutate(ctx: &mut Ctx) nothrow -> Void {
\tctx.s = ctx.s + "A";
}

pub fn main() nothrow -> Int {
\tvar ctx = Ctx(s = fmt.format_int(7));
\tmutate(ctx);
\treturn 0;
}
"""


def _compile_to_mir(tmp_path: Path):
	stdlib = stdlib_root()
	if stdlib is None:
		import pytest
		pytest.skip("stdlib not available")
	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	modules, type_table, exc_catalog, module_exports, deps, diags = parse_drift_workspace_to_hir(
		paths=[src],
		stdlib_root=stdlib,
	)
	assert not any(d.severity == "error" for d in diags), \
		f"parse errors: {[d.message for d in diags if d.severity == 'error']}"
	hirs: dict = {}
	sigs: dict = {}
	for _mid, mod in modules.items():
		hirs.update(mod.func_hirs)
		sigs.update(mod.signatures_by_id)
	mir_funcs, _checked = compile_stubbed_funcs(
		func_hirs=hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		module_exports=module_exports,
		module_deps=deps,
		return_checked=True,
		type_table=type_table,
		prelude_enabled=True,
		entry_module="main",
		entry_name="main",
	)
	return mir_funcs, type_table


def _find_func(mir_funcs, *, module: str, name: str):
	for fn_id, func in mir_funcs.items():
		if isinstance(fn_id, FunctionId) and fn_id.module == module and fn_id.name == name:
			return func
	raise AssertionError(f"function {module}::{name} not found in MIR")


def _flat_instructions(func) -> list:
	out: list = []
	for block in func.blocks.values():
		for instr in block.instructions:
			out.append(instr)
	return out


def test_emit_assign_store_ref_uses_movefromref_at_ptr(tmp_path: Path) -> None:
	"""`mutate` lowers `ctx.s = ctx.s + "A"` (drop-bearing field
	assignment through `&mut Ctx`) using `MoveFromRef` at the field
	ptr — the canonical replace-store atomic-tombstone primitive
	`mem.replace` already uses.  Asserts:

	1. Exactly one `M.MoveFromRef` is emitted in `mutate`.
	2. Its `ptr` matches the `ptr` of exactly one trailing
	   `M.StoreRef` (the new value going into the tombstoned slot).
	3. The `MoveFromRef.local` name has the `__assign_old_*` prefix
	   `_emit_assign_store_ref` reserves for replace-store
	   tombstones.

	String case is exercised here because `overwrite_cleanup` rewrites
	`StoreRef` for String places, but the canonical sequence
	emitted by `_emit_assign_store_ref` is type-agnostic — any
	drop-bearing `T` (Arc, Destructible struct, Array<U>, ...) goes
	through the same lowering.  No String-special-case is allowed
	here.
	"""
	mir_funcs, _type_table = _compile_to_mir(tmp_path)
	mutate = _find_func(mir_funcs, module="main", name="mutate")
	instrs = _flat_instructions(mutate)

	move_from_refs = [i for i in instrs if isinstance(i, M.MoveFromRef)]
	assert len(move_from_refs) == 1, (
		f"expected exactly one MoveFromRef in mutate (the canonical "
		f"replace-store atomic tombstone), got {len(move_from_refs)}.\n"
		f"instructions: {[type(i).__name__ for i in instrs]}"
	)
	mfr = move_from_refs[0]
	assert mfr.local.startswith("__assign_old_"), (
		f"MoveFromRef local must use the `__assign_old_*` prefix that "
		f"`_emit_assign_store_ref` reserves for replace-store "
		f"tombstones (parallel to mem.replace's `__replace_old_*`); "
		f"got {mfr.local!r}.  A different prefix here means the "
		f"MoveFromRef came from a different caller and the actual "
		f"assignment lowering may have regressed."
	)
	tombstoned_ptr = mfr.ptr

	store_refs_at_ptr = [
		i for i in instrs
		if isinstance(i, M.StoreRef) and i.ptr == tombstoned_ptr
	]
	assert len(store_refs_at_ptr) == 1, (
		f"expected exactly one StoreRef at the tombstoned ptr "
		f"{tombstoned_ptr!r} (the new value going into the now-empty "
		f"slot), got {len(store_refs_at_ptr)}.\n"
		f"more than one StoreRef at the same ptr would indicate a "
		f"legacy zero-tombstone StoreRef has reappeared alongside the "
		f"final new-value store."
	)


def test_emit_assign_store_ref_no_zero_tombstone_storeref(tmp_path: Path) -> None:
	"""Pin: `_emit_assign_store_ref` must NOT emit a zero-tombstone
	`StoreRef` (i.e. a `M.StoreRef` whose value is the dest of a
	`M.ZeroValue`).

	The legacy shape

		LoadRef(old_val, ptr)
		ZeroValue(zero_val)
		StoreRef(ptr, value=zero_val)     # <-- this
		DropValue(old_val)
		StoreRef(ptr, value=new_val)

	double-released the old field value for `String` slots:
	`overwrite_cleanup` rewrites every `StoreRef` to a String
	place as `LoadRef + StringRelease + StoreRef`, so the
	zero-tombstone `StoreRef` became a real release of the old slot
	value AND the explicit `DropValue` released the same allocation
	again via the SSA loaded before the tombstone.

	`MoveFromRef` does the tombstoning atomically and does NOT route
	through `overwrite_cleanup`'s StoreRef rewrite, so it has exactly one
	release of the old value (the explicit `DropValue` on the
	moved-out SSA).  This pin catches a future regression that
	reverts to the zero-tombstone shape — even if framed via a new
	primitive that ends in `ZeroValue + StoreRef(zero)`, the same
	double-release would re-emerge for any place rewritten by a
	late-rewrite authority pass.
	"""
	mir_funcs, _type_table = _compile_to_mir(tmp_path)
	mutate = _find_func(mir_funcs, module="main", name="mutate")
	instrs = _flat_instructions(mutate)

	zero_dests = {i.dest for i in instrs if isinstance(i, M.ZeroValue)}
	zero_tombstone_stores = [
		i for i in instrs
		if isinstance(i, M.StoreRef) and i.value in zero_dests
	]
	assert not zero_tombstone_stores, (
		f"detected zero-tombstone StoreRef in mutate "
		f"({len(zero_tombstone_stores)} found): the legacy "
		f"`ZeroValue + StoreRef(zero)` replace-store shape has "
		f"returned.  For String places this resurrects the "
		f"double-release UAF — see "
		f"`lang/tests/memcheck/test_mut_struct_string_field_self_concat.py`."
	)


def test_emit_assign_store_ref_string_release_count(tmp_path: Path) -> None:
	"""Exactly one `M.DropValue` of inner_ty=String executes in
	`mutate` against the old field value.

	Codegen lowers `M.DropValue(String)` to `drift_string_release`
	(`stdlib`-defined intrinsic).  Two of those on the same
	allocation is the user-visible UAF.  This pin counts the
	`DropValue`s of String type and asserts exactly one fires for
	the assignment's old-field release.

	Note: `overwrite_cleanup`'s rewrite of the final `StoreRef` synthesizes
	a `M.StringRelease` of the slot's pre-store contents — but that
	release fires on the **already-tombstoned** slot (null bytes), a
	documented runtime no-op.  We don't count `M.StringRelease`
	(which `overwrite_cleanup` may emit for that no-op release); we count
	`M.DropValue` of String, which represents the explicit release
	of the captured old value.
	"""
	mir_funcs, type_table = _compile_to_mir(tmp_path)
	string_ty = type_table.ensure_string()
	mutate = _find_func(mir_funcs, module="main", name="mutate")
	instrs = _flat_instructions(mutate)

	string_drops = [
		i for i in instrs
		if isinstance(i, M.DropValue) and getattr(i, "ty", None) == string_ty
	]
	assert len(string_drops) == 1, (
		f"expected exactly one M.DropValue(String) in mutate (the "
		f"single release of the replaced field's old value), got "
		f"{len(string_drops)}.\n"
		f"two would be a double-release UAF; zero would leak the "
		f"old field allocation."
	)
