# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Qualified-generic type arguments resolve at the USE SITE, not the outer
nominal's module (LANGUAGE_BUG in `resolve_opaque_type`).

For a type expression like `mem.Ptr<S>` written in module `t`, the outer nominal
`Ptr` is correctly resolved in its own qualifier module (`std.mem`), but the
UNQUALIFIED inner argument `S` is lexical and must resolve in the use-site module
(`t`).  The old code resolved supplied arguments in the OUTER nominal's module
(`origin_mod`), so `mem.Ptr<S>` produced a pointer to a phantom `std.mem.S`
instead of the real `t.S`.  This surfaced when the strict variant-constructor
boundary compared `Ptr<std.mem.LoggerRuntimeState>` (arg) against
`&std.log.LoggerRuntimeState` (field) — different canonical pointees.

Fix: resolve caller-supplied arguments in `arg_module = module_id if module_id
is not None else origin_mod`, while keeping the outer nominal / alias bodies in
`origin_mod`.  An explicitly-qualified argument keeps its own module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import (
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.core.type_resolve_common import resolve_opaque_type


@dataclass
class TE:
	"""Minimal TypeExpr-like (duck-typed on name/args/module_id)."""
	name: str
	args: list = field(default_factory=list)
	module_id: Optional[str] = None


def _mods(table: TypeTable, tid: int) -> str | None:
	return table.get(tid).module_id


def _setup() -> TypeTable:
	t = TypeTable()
	t.declare_struct("t", "S", [])
	t.declare_struct("other", "S", [])
	t.declare_struct("third", "S", [])
	t.declare_variant(
		"other", "Generic", ["T"],
		[VariantArmSchema(name="Wrap", fields=[VariantFieldSchema(name="v", type_expr=GenericTypeExpr.param(0))])],
	)
	return t


def test_mem_ptr_inner_resolves_at_use_site() -> None:
	"""`mem.Ptr<S>` written in `t` → `Ptr<t.S>` (pointee in the use-site module,
	not `std.mem`)."""
	t = _setup()
	ptr = TE("Ptr", [TE("S")], module_id="std.mem")
	tid = resolve_opaque_type(ptr, t, module_id="t", allow_generic_base=True)
	pointee = t.get(tid).param_types[0]
	assert _mods(t, pointee) == "t", f"expected pointee in 't', got {_mods(t, pointee)}"


def test_qualified_outer_unqualified_inner_resolves_in_caller_module() -> None:
	"""`other.Generic<S>` in `t` → outer `Generic` in `other`, inner `S` in `t`."""
	t = _setup()
	g = TE("Generic", [TE("S")], module_id="other")
	tid = resolve_opaque_type(g, t, module_id="t", allow_generic_base=True)
	assert _mods(t, tid) == "other", f"outer should be in 'other', got {_mods(t, tid)}"
	inner = t.get(tid).param_types[0]
	assert _mods(t, inner) == "t", f"inner S should resolve in caller 't', got {_mods(t, inner)}"


def test_explicitly_qualified_inner_argument_is_preserved() -> None:
	"""`other.Generic<third.S>` keeps the inner at `third.S` regardless of the
	outer module or the use site."""
	t = _setup()
	g = TE("Generic", [TE("S", module_id="third")], module_id="other")
	tid = resolve_opaque_type(g, t, module_id="t", allow_generic_base=True)
	inner = t.get(tid).param_types[0]
	assert _mods(t, inner) == "third", f"explicit third.S must be preserved, got {_mods(t, inner)}"


def test_qualified_alias_unqualified_local_argument_resolves_at_use_site() -> None:
	"""A qualified generic alias applied to an unqualified local argument resolves
	that argument at the use site (alias BODY stays in the alias module)."""
	t = _setup()
	# alias `lib.Box<T> = mem.Ptr<T>` declared in module `lib`.
	t.define_type_alias(
		module_id="lib", name="Box", type_params=["T"],
		target=TE("Ptr", [TE("T")], module_id="std.mem"),
	)
	box = TE("Box", [TE("S")], module_id="lib")
	tid = resolve_opaque_type(box, t, module_id="t", allow_generic_base=True)
	# `Box<S>` expands to `mem.Ptr<S>`; the supplied `S` is the caller's `t.S`.
	pointee = t.get(tid).param_types[0]
	assert _mods(t, pointee) == "t", f"alias arg S should resolve at use-site 't', got {_mods(t, pointee)}"
