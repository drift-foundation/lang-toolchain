# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`ConstructIfaceBorrowed` participates in MIR validation (0.33.77 review
finding: lowering emitted the op but the validators only recognized
`ConstructIface`/`ConstructIfaceValue`, leaving the borrowed-view boundary
shape outside the contract layer — a bad `data_ref` or `iface_ty` would
skip validation and surface later in codegen)."""
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.mir_validate import validate_mir_basic_hygiene
from lang.driftc.stage2 import (
	AddrOfLocal,
	BasicBlock,
	ConstructIfaceBorrowed,
	MirFunc,
	Return,
	StoreLocal,
)


def _iface_ty(table: TypeTable) -> int:
	return table.declare_interface("m", "Greeter", [])


def _mk_fn(fn_id: FunctionId, instructions: list, locals_: list[str]) -> MirFunc:
	return MirFunc(
		fn_id=fn_id,
		name=fn_id.name,
		params=[],
		locals=list(locals_),
		blocks={
			"entry": BasicBlock(
				name="entry",
				instructions=instructions,
				terminator=Return(value=None),
			)
		},
		entry="entry",
	)


def test_borrowed_iface_valid_shape_passes_hygiene() -> None:
	table = TypeTable()
	ify = _iface_ty(table)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	mir = _mk_fn(
		fn_id,
		[
			AddrOfLocal(dest="p", local="src", is_mut=False),
			ConstructIfaceBorrowed(dest="v", iface_ty=ify, data_ref="p", value_ty=table.ensure_int()),
			StoreLocal(local="view", value="v"),
		],
		["src", "view"],
	)
	validate_mir_basic_hygiene({fn_id: mir})


def test_borrowed_iface_undefined_data_ref_rejected() -> None:
	"""`data_ref` must be a defined SSA value — an undefined pointer into
	the view is exactly the class of mistake the contract layer exists to
	catch before codegen."""
	table = TypeTable()
	ify = _iface_ty(table)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	mir = _mk_fn(
		fn_id,
		[
			ConstructIfaceBorrowed(dest="v", iface_ty=ify, data_ref="never_defined", value_ty=table.ensure_int()),
			StoreLocal(local="view", value="v"),
		],
		["view"],
	)
	with pytest.raises(AssertionError):
		validate_mir_basic_hygiene({fn_id: mir})
