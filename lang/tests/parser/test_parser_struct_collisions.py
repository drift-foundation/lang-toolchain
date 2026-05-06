# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Collision matrix for struct / error / pub error decls sharing a
name in one module.

Background (LANGUAGE_BUG, 2026-05-06): the introduction of
synthesized `pub error E` Path-A struct faces (slice 5, 0.31.54)
created several name-collision crash classes that leaked raw
`field list mismatch` ValueError text into user diagnostics
instead of producing clean user-facing errors.  K's review-loop
spec mandates a 3-by-3 collision matrix sweep: source struct E,
source error E, synthesized struct face for error E.  Each
ordered pair must produce a deterministic user diagnostic with
no raw internal contract-violation text.

The 3-by-3 enumeration:

  ┌────────────────────────┬────────────────────────────────────┐
  │ first                  │ second                             │
  ├────────────────────────┼────────────────────────────────────┤
  │ source struct E        │ source struct E (same fields)      │
  │ source struct E        │ source struct E (different fields) │
  │ source struct E        │ pub error E (different fields)     │
  │ source struct E        │ error E (different fields)         │
  │ pub error E            │ pub error E (different fields)     │
  │ pub error E            │ source struct E (different fields) │
  │ error E                │ error E (different fields)         │
  └────────────────────────┴────────────────────────────────────┘

Each test asserts:
  1. A user-facing diagnostic with the expected `code`.
  2. NO raw `field list mismatch` / `define_struct_schema_fields`
     / `ValueError` text leaks into any diagnostic message.
"""

from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir


_LEAKED_CONTRACT_TEXT = (
	"field list mismatch",
	"define_struct_schema_fields",
	"ValueError",
)


def _assert_no_internal_leak(diagnostics) -> None:
	for d in diagnostics:
		for needle in _LEAKED_CONTRACT_TEXT:
			assert needle not in d.message, (
				f"internal contract violation leaked into user diagnostic "
				f"({needle!r} found in {d.message!r})"
			)


def _compile(tmp_path: Path, name: str, src: str):
	p = tmp_path / f"{name}.drift"
	p.write_text(src)
	_module, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(p)
	_assert_no_internal_leak(diagnostics)
	return diagnostics


# ── source struct E + source struct E ─────────────────────────────────


def test_dup_source_struct_same_fields(tmp_path: Path) -> None:
	diags = _compile(tmp_path, "dup_struct_same", """
module main;
pub struct Boom { pub msg: String }
pub struct Boom { pub msg: String }
fn main() -> Int { return 0; }
""")
	assert any(d.code == "E_DUP_SOURCE_STRUCT_NAME" for d in diags), \
		f"expected E_DUP_SOURCE_STRUCT_NAME; got: {[d.code for d in diags]}"


def test_dup_source_struct_different_fields(tmp_path: Path) -> None:
	"""K's reported sibling LANGUAGE_BUG (2026-05-06): pre-fix this
	leaked `struct 'main::Boom' field list mismatch: ['msg'] vs
	['code']` from the type-table contract.  Post-fix produces a
	clean `E_DUP_SOURCE_STRUCT_NAME`."""
	diags = _compile(tmp_path, "dup_struct_diff", """
module main;
pub struct Boom { pub msg: String }
pub struct Boom { pub code: Int }
fn main() -> Int { return 0; }
""")
	assert any(d.code == "E_DUP_SOURCE_STRUCT_NAME" for d in diags), \
		f"expected E_DUP_SOURCE_STRUCT_NAME; got: {[d.code for d in diags]}"


# ── source struct E + (pub) error E ───────────────────────────────────


def test_struct_then_pub_error(tmp_path: Path) -> None:
	diags = _compile(tmp_path, "struct_then_puberror", """
module main;
pub struct Boom { pub msg: String }
pub error Boom { code: Int }
fn main() -> Int { return 0; }
""")
	assert any(d.code == "E_DUP_TYPE_NAME_ERROR_VS_STRUCT" for d in diags), \
		f"expected E_DUP_TYPE_NAME_ERROR_VS_STRUCT; got: {[d.code for d in diags]}"


def test_struct_then_nonpub_error(tmp_path: Path) -> None:
	diags = _compile(tmp_path, "struct_then_error", """
module main;
pub struct Boom { pub msg: String }
error Boom { code: Int }
fn main() -> Int { return 0; }
""")
	assert any(d.code == "E_DUP_TYPE_NAME_ERROR_VS_STRUCT" for d in diags), \
		f"expected E_DUP_TYPE_NAME_ERROR_VS_STRUCT; got: {[d.code for d in diags]}"


# ── (pub) error E + source struct E (reverse order) ───────────────────


def test_pub_error_then_struct(tmp_path: Path) -> None:
	"""Reverse declaration order from `test_struct_then_pub_error`.
	Pre-fix this leaked `field list mismatch` because the
	synthesized struct face registered first and the source struct
	then tried to register a different schema under the same name."""
	diags = _compile(tmp_path, "puberror_then_struct", """
module main;
pub error Boom { code: Int }
pub struct Boom { pub msg: String }
fn main() -> Int { return 0; }
""")
	assert any(d.code == "E_DUP_TYPE_NAME_ERROR_VS_STRUCT" for d in diags), \
		f"expected E_DUP_TYPE_NAME_ERROR_VS_STRUCT; got: {[d.code for d in diags]}"


def test_nonpub_error_then_struct(tmp_path: Path) -> None:
	diags = _compile(tmp_path, "error_then_struct", """
module main;
error Boom { code: Int }
pub struct Boom { pub msg: String }
fn main() -> Int { return 0; }
""")
	assert any(d.code == "E_DUP_TYPE_NAME_ERROR_VS_STRUCT" for d in diags), \
		f"expected E_DUP_TYPE_NAME_ERROR_VS_STRUCT; got: {[d.code for d in diags]}"
