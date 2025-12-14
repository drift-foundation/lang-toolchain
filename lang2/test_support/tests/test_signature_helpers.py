from __future__ import annotations

from dataclasses import dataclass

from lang2.test_support import (
	build_exception_catalog,
	declared_from_signatures,
	exception_catalog_from_decls,
	make_signatures,
	signatures_from_decl_nodes,
)


@dataclass
class FakeDecl:
	"""Minimal function decl stub for helper tests."""

	name: str
	return_type: str | tuple
	throws: tuple[str, ...] | None = None


def test_make_signatures_and_declared_can_throw():
	sigs = make_signatures(
		{"f": "Int", "g": "Int"},
		{"f": ("EvtA",)},
		declared_can_throw={"f": True},
	)
	declared = declared_from_signatures(sigs)

	assert sigs["f"].throws_events == ("EvtA",)
	assert sigs["g"].throws_events == ()
	assert declared == {"f": True, "g": False}


def test_signatures_from_decl_nodes_and_exception_catalog_from_decls():
	decls = [
		FakeDecl(name="f", return_type="Int", throws=("EvtA", "EvtB")),
		FakeDecl(name="g", return_type="Int", throws=None),
		FakeDecl(name="h", return_type=("InternalResult", "Ok", "Err"), throws=("EvtB",)),
	]

	sigs = signatures_from_decl_nodes(decls)
	assert set(sigs.keys()) == {"f", "g", "h"}
	assert sigs["f"].throws_events == ("EvtA", "EvtB")
	assert sigs["g"].throws_events == ()
	assert sigs["h"].return_type == ("InternalResult", "Ok", "Err")

	catalog = exception_catalog_from_decls(decls)
	assert catalog["EvtA"] == 1
	assert catalog["EvtB"] == 2

	# Augment a base catalog
	base = build_exception_catalog({"Pre": 5})
	augmented = exception_catalog_from_decls(decls, base=base)
	assert augmented["Pre"] == 5
	assert augmented["EvtA"] == 6
	assert augmented["EvtB"] == 7
