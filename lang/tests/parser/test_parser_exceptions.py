# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from pathlib import Path

import pytest

from lang.driftc.core.event_codes import event_code
from lang.driftc.parser import parse_drift_to_hir


def test_exception_decl_yields_event_code(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module foo.bar;

error EvtA { code: Int }
fn main() -> Int { return 0; }
"""
	)
	_module, _type_table, exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert "foo.bar:EvtA" in exc_catalog
	assert exc_catalog["foo.bar:EvtA"] == event_code("foo.bar:EvtA")


def test_duplicate_exception_reports_diagnostic(tmp_path: Path) -> None:
	src = tmp_path / "dupe.drift"
	src.write_text(
		"""
error Boom { msg: String }
error Boom { code: Int }
fn main() -> Int { return 0; }
"""
	)
	_module, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("duplicate exception" in d.message for d in diagnostics)


def test_exception_code_collision_reports_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""
	Force a payload collision by monkeypatching the hash to a constant, ensuring
	that collisions are diagnosed and the colliding entries are skipped.
	"""
	from lang.driftc.core import event_codes

	monkeypatch.setattr(event_codes, "hash64", lambda data: 42)
	src = tmp_path / "collide.drift"
	src.write_text(
		"""
error Boom { msg: String }
error Zoom { code: Int }
fn main() -> Int { return 0; }
"""
	)
	_module, _type_table, exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("exception code collision" in d.message for d in diagnostics)
	# Colliding entries should not both be present.
	assert len(exc_catalog) <= 1


def test_struct_and_error_same_name_reports_clean_diagnostic(tmp_path: Path) -> None:
	"""LANGUAGE_BUG (2026-05-06): a `pub struct Boom` followed by
	`pub error Boom` produces a source StructDef plus a synthesized
	error StructDef with mismatched field schemas.  Pre-fix this
	leaked a raw `field list mismatch` ValueError instead of a
	user-facing diagnostic.  Post-fix the parser emits
	`E_DUP_TYPE_NAME_ERROR_VS_STRUCT` at the error decl site and
	skips the synthesized struct face so downstream registration
	completes cleanly."""
	src = tmp_path / "name_collision.drift"
	src.write_text(
		"""
module main;
pub struct Boom { pub msg: String }
pub error Boom { code: Int }
fn main() -> Int { return 0; }
"""
	)
	_module, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics, "expected duplicate-name diagnostic"
	assert any(
		d.code == "E_DUP_TYPE_NAME_ERROR_VS_STRUCT" for d in diagnostics
	), f"expected E_DUP_TYPE_NAME_ERROR_VS_STRUCT; got: {[d.code for d in diagnostics]}"
	# No raw ValueError text should leak through as a user diagnostic.
	assert not any(
		"field list mismatch" in d.message
		or "define_struct_schema_fields" in d.message
		for d in diagnostics
	), "internal type-table contract violation leaked into user diagnostics"


def test_duplicate_pub_error_does_not_cascade_into_synth_impls(tmp_path: Path) -> None:
	"""LANGUAGE_BUG (2026-05-06): two `pub error Boom { ... }` decls
	in the same module previously emitted both `duplicate exception`
	(catalog) AND `duplicate impl for trait 'std.core.Throw' on
	'main.Boom'` / `'std.core.Diagnostic'` (synthesis cascade).
	Post-fix, auto-Throw / auto-Diagnostic synthesis dedupes by
	error name so only the catalog diagnostic surfaces."""
	src = tmp_path / "dupe_no_cascade.drift"
	src.write_text(
		"""
module main;
pub error Boom { msg: String }
pub error Boom { code: Int }
fn main() -> Int { return 0; }
"""
	)
	_module, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert any("duplicate exception" in d.message for d in diagnostics)
	assert not any(
		"duplicate impl" in d.message and "Throw" in d.message
		for d in diagnostics
	), "auto-Throw synthesis must not double-emit on duplicate `error` decls"
	assert not any(
		"duplicate impl" in d.message and "Diagnostic" in d.message
		for d in diagnostics
	), "auto-Diagnostic synthesis must not double-emit on duplicate `error` decls"
