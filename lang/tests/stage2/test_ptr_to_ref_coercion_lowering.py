# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""HIR→MIR consumption of the `Ptr<T> -> &T` coercion mark.

The checker records an unsafe, identical-pointee `Ptr<T> -> &T` / `Ptr<T> -> &mut T`
constructor-argument conversion in `ptr_to_ref_coercions` (node_id -> target ref
TypeId).  `HIRToMIR.lower_expr` consumes the mark by lowering the source pointer,
re-asserting the contract (RAW_PTR source, REF target, identical canonical
pointees), and re-binding the value as a reference-typed SSA value via
`AssignSSA` — explicit downstream, no representation change.

Positive: a real `Optional<&S>::Some(mem.ptr_from_ref(...))` program (whose
argument is a genuine `Ptr<S>`) is lowered; the `ConstructVariant("Some")`
payload argument is the `AssignSSA` dest, and that dest is typed as the target
reference.

Negative: a malformed mark (the marked source is not a `RAW_PTR`) raises an
internal lowering-contract failure.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeKind, TypeTable
from lang.driftc.parser import ast as parser_ast
from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root
from lang.driftc.stage1.node_ids import assign_node_ids
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage4 import ssa as _ssa_mod

ROOT = Path(__file__).resolve().parents[3]


PTR_REF_SOURCE = """\
module main;
import std.mem as mem;
struct S { x: Int }
fn wrap(r: &S) nothrow -> Optional<&S> {
	unsafe { val p = mem.ptr_from_ref<type S>(r); return Optional<&S>::Some(p); }
}
pub fn main() nothrow -> Int {
	val v = S(x = 7);
	val o = wrap(v);
	var r = 1;
	match o { Optional::Some(s) => { r = s.x; }, Optional::None() => { r = 0; } }
	return r;
}
"""


def test_ptr_to_ref_mark_reaches_construct_variant_as_ref_typed_ssa(tmp_path: Path, monkeypatch) -> None:
	"""The marked `Ptr<S>` argument reaches `ConstructVariant("Some")` as a
	reference-typed `AssignSSA` SSA value."""
	lowers: list[HIRToMIR] = []
	orig_lb = HIRToMIR.lower_block

	def _lb_spy(self, *a, **k):
		if self not in lowers:
			lowers.append(self)
		return orig_lb(self, *a, **k)

	monkeypatch.setattr(HIRToMIR, "lower_block", _lb_spy)

	# Snapshot the PRE-SSA primitives inside the MirToSSA.run spy: the SSA pass
	# rewrites the function in place (generating its own AssignSSA moves), so the
	# only reliable point to observe the HIR→MIR-emitted ptr->ref AssignSSA is at
	# the SSA pass's entry.
	snapshots: list[tuple] = []
	orig_ssa = _ssa_mod.MirToSSA.run

	def _ssa_spy(self, func):
		instrs = [i for blk in func.blocks.values() for i in blk.instructions]
		assign_dests = {i.dest for i in instrs if isinstance(i, M.AssignSSA)}
		some_ctor_args = [list(i.args) for i in instrs if isinstance(i, M.ConstructVariant) and i.ctor == "Some"]
		low = next((lw for lw in lowers if lw.b.func.fn_id == func.fn_id), None)
		ltypes = dict(low._local_types) if low is not None else {}
		tt = low._type_table if low is not None else None
		snapshots.append((assign_dests, some_ctor_args, ltypes, tt))
		return orig_ssa(self, func)

	monkeypatch.setattr(_ssa_mod.MirToSSA, "run", _ssa_spy)

	src = tmp_path / "main.drift"
	src.write_text(PTR_REF_SOURCE, encoding="utf-8")
	rc = driftc_main([
		"--allow-unsafe", "--stdlib-root", str(stdlib_root()),
		"--entry", "main", "-o", str(tmp_path / "out.bin"), str(src),
	])
	assert rc == 0, "ptr->ref program must compile"

	found = False
	for assign_dests, some_ctor_args, ltypes, tt in snapshots:
		if tt is None:
			continue
		for args in some_ctor_args:
			for arg in args:
				if arg in assign_dests:
					dest_ty = ltypes.get(arg)
					assert dest_ty is not None, "ptr->ref AssignSSA dest has no recorded type"
					assert tt.get(dest_ty).kind is TypeKind.REF, (
						f"ptr->ref AssignSSA dest feeding ConstructVariant must be REF-typed, "
						f"got {tt.get(dest_ty).kind}"
					)
					found = True
	assert found, "no ConstructVariant('Some') payload fed by a ptr->ref AssignSSA was found"


def test_malformed_ptr_to_ref_mark_non_rawptr_source_raises() -> None:
	"""A malformed mark whose source is NOT a `RAW_PTR` (here an `Int` literal)
	raises the internal lowering-contract failure instead of silently emitting a
	mis-typed reference."""
	table = TypeTable()
	s = table.declare_struct("main", "S", [])
	ref_s = table.ensure_ref(s)

	arg = H.HLiteralInt(0)
	block = H.HBlock(statements=[H.HExprStmt(expr=arg)])
	assign_node_ids(block)

	builder = make_builder(FunctionId(module="main", name="t", ordinal=0))
	lower = HIRToMIR(
		builder,
		type_table=table,
		typed_mode="recover",
		expr_types={arg.node_id: table.ensure_int()},
		ptr_to_ref_coercions={arg.node_id: ref_s},
	)
	with pytest.raises(AssertionError, match="not RAW_PTR"):
		lower.lower_block(block)


def test_ptr_to_ref_canonically_equal_alias_pointee_does_not_assert(monkeypatch) -> None:
	"""Lowering's pointee check uses the checker's CANONICAL equivalence
	(`_ctor_same_type`), not raw TypeId equality: a `Ptr<A>` whose pointee `A` is
	a distinct forward-nominal aliased to `S` (so `A != S` as TypeIds, yet they
	are canonically equal) coerces into a `&S` field WITHOUT asserting — matching
	what the checker accepted.  With raw TypeId equality this would spuriously
	raise, i.e. a program could pass type-checking and then assert at lowering."""
	table = TypeTable()
	s = table.declare_struct("main", "S", [])
	# Zero-param alias `A = S`, plus a DISTINCT forward-nominal TypeId for `A`
	# (which `_ctor_dealias_zero_param` resolves back to `S`).
	table.define_type_alias(module_id="main", name="A", type_params=[], target=parser_ast.TypeExpr(name="S", module_id="main"))
	forward_a = table.ensure_named("A", module_id="main")
	assert forward_a != s, "test setup: forward-nominal A must be a distinct TypeId from S"
	ptr_a = table.new_ptr(forward_a, module_id="main")  # Ptr<A>, pointee = forward_a
	ref_s = table.ensure_ref(s)

	arg = H.HLiteralInt(0)
	block = H.HBlock(statements=[H.HExprStmt(expr=arg)])
	assign_node_ids(block)
	builder = make_builder(FunctionId(module="main", name="t", ordinal=0))
	lower = HIRToMIR(
		builder,
		type_table=table,
		typed_mode="recover",
		ptr_to_ref_coercions={arg.node_id: ref_s},
	)
	# Inject the (alias-divergent) source pointer type so the mark consumption
	# sees `Ptr<A>` while the target field is `&S`.
	orig_infer = lower._infer_expr_type

	def _fake_infer(e):
		if getattr(e, "node_id", None) == arg.node_id:
			return ptr_a
		return orig_infer(e)

	monkeypatch.setattr(lower, "_infer_expr_type", _fake_infer)
	lower.lower_block(block)  # must NOT raise — pointees are canonically equal

	instrs = [i for blk in builder.func.blocks.values() for i in blk.instructions]
	assigns = [i for i in instrs if isinstance(i, M.AssignSSA)]
	assert assigns, "aliased-pointee ptr->ref coercion should still emit an AssignSSA"
	assert lower._type_table.get(lower._local_types[assigns[-1].dest]).kind is TypeKind.REF
