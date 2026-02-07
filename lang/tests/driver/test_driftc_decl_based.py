"""
Integration test: use decl-like stubs to build signatures/catalog and run driver.
"""

from __future__ import annotations

from dataclasses import dataclass

from lang.driftc import stage1 as H
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.core.function_id import function_symbol
from lang.driftc.core.types_core import TypeTable
from lang.test_support import (
	build_exception_catalog,
	signatures_from_decl_nodes,
	exception_catalog_from_decls,
)


@dataclass
class FakeDecl:
	"""Minimal decl stub for building signatures and exception catalogs."""

	name: str
	return_type: str | tuple
	throws: tuple[str, ...] | None = None
	declared_can_throw: bool | None = None


def test_driver_accepts_decl_based_signatures_and_catalog():
	"""
	Build signatures/exception catalog from decl-like objects and ensure the
	driver threads them through the checker/throw checks (happy path).
	"""
	decls = [
		FakeDecl(name="f_can", return_type="Int", throws=("m:EvtA",), declared_can_throw=True),
		FakeDecl(name="g_plain", return_type="Int", throws=None),
	]
	signatures = signatures_from_decl_nodes(decls)
	exc_catalog = exception_catalog_from_decls(decls)

	# HIR bodies: can-throw function throws; plain function -> an int.
	hirs = {
		"f_can": H.HBlock(
			statements=[
				H.HThrow(value=H.HExceptionInit(event_fqn="m:EvtA", pos_args=[], kw_args=[]))
			]
		),
		"g_plain": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))]),
	}
	type_table = TypeTable()
	type_table.exception_schemas = {"m:EvtA": ("m:EvtA", [])}

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		type_table=type_table,
		build_ssa=True,
		return_checked=True,
	)

	assert {function_symbol(fn_id) for fn_id in mir_funcs.keys()} == {"f_can", "g_plain"}
	assert checked.diagnostics == []

	f_info = next(info for info in checked.fn_infos_by_id.values() if info.name == "f_can")
	assert f_info.declared_can_throw is True
	assert f_info.declared_events == frozenset({"m:EvtA"})
	assert f_info.return_type_id is not None

	g_info = next(info for info in checked.fn_infos_by_id.values() if info.name == "g_plain")
	# Unannotated functions default to can-throw; nothrow must be explicit.
	assert g_info.declared_can_throw is True
	assert g_info.declared_events is None
	assert g_info.return_type_id is not None


def test_driver_reports_decl_based_mismatch_diagnostics():
	"""
	Decl says g_plain does not throw, but HIR throws an unknown event: expect diagnostics.
	"""
	decls = [
		FakeDecl(name="g_plain", return_type="Int", throws=None, declared_can_throw=False),
	]
	signatures = signatures_from_decl_nodes(decls)
	exc_catalog = exception_catalog_from_decls(decls)  # empty catalog

	hirs = {
		"g_plain": H.HBlock(
			statements=[
				H.HThrow(value=H.HExceptionInit(event_fqn="m:EvtX", pos_args=[], kw_args=[]))
			]
		),
	}
	type_table = TypeTable()
	type_table.exception_schemas = {"m:EvtX": ("m:EvtX", [])}

	_, checked = compile_stubbed_funcs(
		func_hirs=hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		type_table=type_table,
		build_ssa=True,
		return_checked=True,
	)

	assert checked.diagnostics, "expected diagnostics for throw mismatch"
	msgs = [d.message for d in checked.diagnostics]
	assert any("declared nothrow but may throw" in m for m in msgs)
