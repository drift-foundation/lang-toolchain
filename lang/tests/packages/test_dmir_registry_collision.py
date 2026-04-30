# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Unit test for the DMIR ``build_dataclass_registry`` defensive collision
check.  The assertion fires if two dataclasses share a bare ``__name__``
but carry differing field sets — the silent-data-loss shape that motivated
the 0.31.28 ``stage0_ast`` registration-order workaround.

See ``docs/refactor_triggers.md`` § "Promote DMIR ``_to_jsonable``
discriminators to module-qualified names" and § "Defensive collision check
for DMIR registry".
"""
from __future__ import annotations

import dataclasses
import types

import pytest

from lang.driftc.packages.provisional_dmir_v0 import build_dataclass_registry


def _module(name: str, **attrs: object) -> types.ModuleType:
	mod = types.ModuleType(name)
	for k, v in attrs.items():
		setattr(mod, k, v)
	return mod


def test_distinct_classes_with_differing_fields_collide() -> None:
	@dataclasses.dataclass
	class TypeNameRef:
		name: str

	mod_a = _module("mod_a", TypeNameRef=TypeNameRef)

	@dataclasses.dataclass
	class TypeNameRef:  # noqa: F811 — intentional name shadow simulating cross-module collision.
		name: str
		module_id: str | None = None

	mod_b = _module("mod_b", TypeNameRef=TypeNameRef)

	with pytest.raises(AssertionError) as exc:
		build_dataclass_registry(mod_a, mod_b)
	msg = str(exc.value)
	assert "DMIR registry collision" in msg
	assert "TypeNameRef" in msg
	assert "module_id" in msg  # diverging field is named in the diagnostic
	assert "refactor_triggers.md" in msg


def test_same_class_referenced_twice_is_fine() -> None:
	@dataclasses.dataclass
	class Same:
		x: int

	mod_a = _module("mod_a", Same=Same)
	mod_b = _module("mod_b", Same=Same)  # re-export of the same class object

	out = build_dataclass_registry(mod_a, mod_b)
	assert out["Same"] is Same


def test_distinct_classes_with_identical_fields_are_tolerated() -> None:
	# Same bare name + identical field tuple → registration-order wins,
	# but no AssertionError.  This is the benign duplicate shape; the
	# collision check is gated on diverging field sets so it doesn't fire
	# on harmless re-declarations.
	@dataclasses.dataclass
	class Ident:
		x: int
		y: int

	first = Ident
	mod_a = _module("mod_a", Ident=first)

	@dataclasses.dataclass
	class Ident:  # noqa: F811
		x: int
		y: int

	second = Ident
	mod_b = _module("mod_b", Ident=second)

	out = build_dataclass_registry(mod_a, mod_b)
	# Last registration wins — but the assertion did not fire.
	assert out["Ident"] is second
