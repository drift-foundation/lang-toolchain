"""
Integration test: use decl-like stubs to build signatures/catalog and run driver.
"""

from __future__ import annotations

from dataclasses import dataclass

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.test_support import (
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


def test_driver_accepts_decl_based_signatures_and_catalog():
	"""
	Build signatures/exception catalog from decl-like objects and ensure the
	driver threads them through the checker/throw checks (happy path).
	"""
	decls = [
		FakeDecl(name="f_can", return_type="FnResult<Int, Error>", throws=("EvtA",)),
		FakeDecl(name="g_plain", return_type="Int", throws=None),
	]
	signatures = signatures_from_decl_nodes(decls)
	exc_catalog = exception_catalog_from_decls(decls)

	# HIR bodies: can-throw function throws; plain function returns an int.
	hirs = {
		"f_can": H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtA", args=[]))]),
		"g_plain": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))]),
	}

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		build_ssa=True,
		return_checked=True,
	)

	assert set(mir_funcs.keys()) == {"f_can", "g_plain"}
	assert checked.diagnostics == []

	f_info = checked.fn_infos["f_can"]
	assert f_info.declared_can_throw is True
	assert f_info.declared_events == frozenset({"EvtA"})
	assert f_info.return_type_id is not None

	g_info = checked.fn_infos["g_plain"]
	assert g_info.declared_can_throw is False
	assert g_info.declared_events is None
	assert g_info.return_type_id is not None


def test_driver_reports_decl_based_mismatch_diagnostics():
	"""
	Decl says g_plain does not throw, but HIR throws an unknown event: expect diagnostics.
	"""
	decls = [
		FakeDecl(name="g_plain", return_type="Int", throws=None),
	]
	signatures = signatures_from_decl_nodes(decls)
	exc_catalog = exception_catalog_from_decls(decls)  # empty catalog

	hirs = {
		"g_plain": H.HBlock(statements=[H.HThrow(value=H.HDVInit(dv_type_name="EvtX", args=[]))]),
	}

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		build_ssa=True,
		return_checked=True,
	)

	assert "g_plain" in mir_funcs
	assert checked.diagnostics, "expected diagnostics for throw mismatch"
	msgs = [d.message for d in checked.diagnostics]
	assert any("non-can-throw" in m or "can-throw" in m for m in msgs)
