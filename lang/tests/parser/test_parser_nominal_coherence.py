# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Module-level nominal namespace coherence: source struct, source
variant, source interface, source trait, and source `error` /
`pub error` decls all share one type-name namespace.  Two decls
under different kinds with the same name MUST be rejected with a
clean user diagnostic — not silently coexist, and not leak a raw
type-table contract violation (`field list mismatch` /
`schema mismatch` / `ValueError`).

Background (LANGUAGE_BUG, 2026-05-06): K's review-loop matrix
sweep flagged that cross-kind decls sharing a name silently
coexisted (e.g. `pub struct X` + `pub variant X`), producing two
type-table entries under different `TypeKind`s with downstream
resolution picking ambiguously.  Same-kind duplicates for variants
and interfaces leaked raw "schema mismatch" text from the
type-table contract instead of a clean user diagnostic.

This file exhaustively probes the 5×5 collision space:

  ┌─────────────┬────────────────────────────────────────────────────┐
  │ first kind  │ second kind                                        │
  ├─────────────┼────────────────────────────────────────────────────┤
  │ struct      │ struct (handled in Path-A slice)                   │
  │ variant     │ variant                                            │
  │ interface   │ interface                                          │
  │ trait       │ trait                                              │
  │ error       │ error (handled in pub-error duplicate slice)       │
  ├─────────────┼────────────────────────────────────────────────────┤
  │ struct      │ variant                                            │
  │ struct      │ interface                                          │
  │ struct      │ trait                                              │
  │ struct      │ error / pub error  (handled in Path-A slice)       │
  │ variant     │ struct                                             │
  │ variant     │ interface                                          │
  │ variant     │ trait                                              │
  │ variant     │ error                                              │
  │ interface   │ struct                                             │
  │ interface   │ variant                                            │
  │ interface   │ trait                                              │
  │ interface   │ error                                              │
  │ trait       │ struct                                             │
  │ trait       │ variant                                            │
  │ trait       │ interface                                          │
  │ trait       │ error                                              │
  │ error       │ struct (handled in Path-A slice)                   │
  │ error       │ variant                                            │
  │ error       │ interface                                          │
  │ error       │ trait                                              │
  └─────────────┴────────────────────────────────────────────────────┘

Each test asserts:
  1. A user-facing diagnostic with the expected `code`.
  2. NO raw `field list mismatch` / `schema mismatch` /
     `define_*` / `ValueError` text leaks into any diagnostic.
"""

from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir


_LEAKED_CONTRACT_TEXT = (
	"field list mismatch",
	"schema mismatch",
	"define_struct_schema_fields",
	"define_variant_schema",
	"define_interface_schema_methods",
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


def _has_code(diags, code: str) -> bool:
	return any(d.code == code for d in diags)


# ── Same-kind duplicates ──────────────────────────────────────────────


def test_dup_variant_same_name(tmp_path: Path) -> None:
	diags = _compile(tmp_path, "vv", """
module main;
pub variant X { @tombstone Tombstone, A, B }
pub variant X { @tombstone Tombstone, C(value: Int) }
fn main() nothrow -> Int { return 0; }
""")
	assert _has_code(diags, "E_DUP_SOURCE_VARIANT_NAME"), \
		f"expected E_DUP_SOURCE_VARIANT_NAME; got: {[d.code for d in diags]}"


def test_dup_interface_same_name(tmp_path: Path) -> None:
	diags = _compile(tmp_path, "ii", """
module main;
pub interface X { fn f(self: &Self) nothrow -> Int; }
pub interface X { fn g(self: &Self) nothrow -> Int; }
fn main() nothrow -> Int { return 0; }
""")
	assert _has_code(diags, "E_DUP_SOURCE_INTERFACE_NAME"), \
		f"expected E_DUP_SOURCE_INTERFACE_NAME; got: {[d.code for d in diags]}"


def test_dup_trait_same_name(tmp_path: Path) -> None:
	diags = _compile(tmp_path, "tt", """
module main;
pub trait X { fn f(self: &Self) -> Int }
pub trait X { fn g(self: &Self) -> Int }
fn main() nothrow -> Int { return 0; }
""")
	assert _has_code(diags, "E_DUP_SOURCE_TRAIT_NAME"), \
		f"expected E_DUP_SOURCE_TRAIT_NAME; got: {[d.code for d in diags]}"


# ── Cross-kind collisions ─────────────────────────────────────────────


_CROSS_KIND_CASES = [
	# (label, expected_first_kind, source).  Each emits exactly one
	# E_DUP_NOMINAL_NAME diagnostic.  `expected_first_kind` is the
	# kind named in the diagnostic's "already declared as <X>" prose,
	# which must always match the FIRST decl in source order — pinned
	# by ordered pairs like `variant_error` (variant first, error
	# second) where the diagnostic must read "already declared as a
	# variant", not "already declared as an error".
	#
	# Forward-order pairs (struct/error first):
	("struct_variant",    "struct",    "pub struct X { pub a: Int }\npub variant X { @tombstone Tombstone, A }"),
	("struct_interface",  "struct",    "pub struct X { pub a: Int }\npub interface X { fn f(self: &Self) nothrow -> Int; }"),
	("struct_trait",      "struct",    "pub struct X { pub a: Int }\npub trait X { fn f(self: &Self) -> Int }"),
	("error_variant",     "error",     "pub error X { code: Int }\npub variant X { @tombstone Tombstone, A }"),
	("error_interface",   "error",     "pub error X { code: Int }\npub interface X { fn f(self: &Self) nothrow -> Int; }"),
	("error_trait",       "error",     "pub error X { code: Int }\npub trait X { fn f(self: &Self) -> Int }"),
	("variant_interface", "variant",   "pub variant X { @tombstone Tombstone, A }\npub interface X { fn f(self: &Self) nothrow -> Int; }"),
	("variant_trait",     "variant",   "pub variant X { @tombstone Tombstone, A }\npub trait X { fn f(self: &Self) -> Int }"),
	("interface_trait",   "interface", "pub interface X { fn f(self: &Self) nothrow -> Int; }\npub trait X { fn f(self: &Self) -> Int }"),
	# Reverse-order pairs (variant/interface/trait first) — pin
	# K's review-loop finding that the per-kind pre-fill walked
	# structs/errors first, producing wrong "already declared as
	# <X>" labels for these cases.
	("variant_struct",    "variant",   "pub variant X { @tombstone Tombstone, A }\npub struct X { pub a: Int }"),
	("variant_error",     "variant",   "pub variant X { @tombstone Tombstone, A }\npub error X { code: Int }"),
	("interface_struct",  "interface", "pub interface X { fn f(self: &Self) nothrow -> Int; }\npub struct X { pub a: Int }"),
	("interface_error",   "interface", "pub interface X { fn f(self: &Self) nothrow -> Int; }\npub error X { code: Int }"),
	("interface_variant", "interface", "pub interface X { fn f(self: &Self) nothrow -> Int; }\npub variant X { @tombstone Tombstone, A }"),
	("trait_struct",      "trait",     "pub trait X { fn f(self: &Self) -> Int }\npub struct X { pub a: Int }"),
	("trait_error",       "trait",     "pub trait X { fn f(self: &Self) -> Int }\npub error X { code: Int }"),
	("trait_variant",     "trait",     "pub trait X { fn f(self: &Self) -> Int }\npub variant X { @tombstone Tombstone, A }"),
	("trait_interface",   "trait",     "pub trait X { fn f(self: &Self) -> Int }\npub interface X { fn f(self: &Self) nothrow -> Int; }"),
]


def test_cross_kind_collisions_all_emit_clean_dup_nominal_diagnostic(tmp_path: Path) -> None:
	"""Cross-kind collision matrix (forward + reverse).  Each pair must:
	  - emit `E_DUP_NOMINAL_NAME` (exactly one diagnostic with that
	    code surviving past the first-claim winner).
	  - cite the FIRST kind in source order in the diagnostic prose
	    (e.g. for `pub variant X` then `pub error X`, the message
	    must read "already declared as a variant", not "as an error").
	  - NOT leak `schema mismatch` / `field list mismatch` /
	    `ValueError` text into user diagnostics.
	"""
	for label, expected_first_kind, body in _CROSS_KIND_CASES:
		src = (
			"module main;\n"
			+ body
			+ "\nfn main() nothrow -> Int { return 0; }\n"
		)
		diags = _compile(tmp_path, label, src)
		_dup = [d for d in diags if d.code == "E_DUP_NOMINAL_NAME"]
		assert _dup, (
			f"{label}: expected E_DUP_NOMINAL_NAME; got: "
			f"{[d.code for d in diags]} / messages: "
			f"{[d.message for d in diags]}"
		)
		# Diagnostic prose must cite the FIRST decl kind in source
		# order, not whatever kind happens to be processed first
		# in the parser's per-kind walks.
		_msg = _dup[0].message
		_needle = f"already declared as a {expected_first_kind}"
		_needle_an = f"already declared as an {expected_first_kind}"
		assert _needle in _msg or _needle_an in _msg, (
			f"{label}: diagnostic must cite first-in-source-order "
			f"kind '{expected_first_kind}'; got: {_msg!r}"
		)
