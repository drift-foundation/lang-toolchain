# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Tests for the drift-doc API documentation generator."""
from __future__ import annotations

import tempfile
from pathlib import Path

from tools.drift_doc.drift_doc import extract_module_doc, generate_docs, render_module_markdown


def test_extract_function_with_doc_comment() -> None:
	"""Exported function with /// doc comment is extracted."""
	source = '''\
module test.mod;

export { greet };

/// Says hello to the given name.
///
/// Returns a greeting string.
pub fn greet(name: &String) nothrow -> String {
	return "hello";
}
'''
	module_name, entries = extract_module_doc(source)
	assert module_name == "test.mod"
	assert len(entries) == 1
	e = entries[0]
	assert e.kind == "function"
	assert e.name == "greet"
	assert "name: &String" in e.signature
	assert "nothrow" in e.signature
	assert "Says hello" in e.doc
	assert "Returns a greeting" in e.doc


def test_extract_function_without_doc_comment() -> None:
	"""Exported function without doc comment still appears (signature-only)."""
	source = '''\
module test.mod;

export { add };

pub fn add(a: Int, b: Int) nothrow -> Int {
	return a + b;
}
'''
	_, entries = extract_module_doc(source)
	assert len(entries) == 1
	assert entries[0].doc == ""
	assert "a: Int" in entries[0].signature


def test_unexported_function_excluded() -> None:
	"""Non-exported functions do not appear in docs."""
	source = '''\
module test.mod;

export { pub_fn };

pub fn pub_fn() nothrow -> Int { return 0; }

fn private_fn() nothrow -> Int { return 1; }
'''
	_, entries = extract_module_doc(source)
	names = {e.name for e in entries}
	assert "pub_fn" in names
	assert "private_fn" not in names


def test_struct_only_pub_fields() -> None:
	"""Struct shows only public fields, not private ones."""
	source = '''\
module test.mod;

export { MyStruct };

/// A documented struct.
pub struct MyStruct {
	pub name: String,
	secret: Int
}
'''
	_, entries = extract_module_doc(source)
	assert len(entries) == 1
	st = entries[0]
	assert st.kind == "struct"
	assert st.doc == "A documented struct."
	assert len(st.children) == 1
	assert st.children[0].name == "name"


def test_variant_cases() -> None:
	"""Variant arms are listed as cases."""
	source = '''\
module test.mod;

export { Color };

/// Represents a color.
pub variant Color {
	Red,
	Green,
	Blue,
}
'''
	_, entries = extract_module_doc(source)
	assert len(entries) == 1
	vt = entries[0]
	assert vt.kind == "variant"
	assert len(vt.children) == 3
	assert vt.children[0].name == "Red"


def test_generate_docs_produces_index_and_module_file() -> None:
	"""generate_docs creates index.md and per-module .md files."""
	source = '''\
module test.lib;

export { helper };

/// A helper function.
pub fn helper() nothrow -> Int {
	return 0;
}
'''
	with tempfile.TemporaryDirectory() as tmp:
		src_dir = Path(tmp) / "src"
		src_dir.mkdir()
		(src_dir / "lib.drift").write_text(source, encoding="utf-8")

		out_dir = Path(tmp) / "doc"
		modules = generate_docs(source_root=src_dir, output_dir=out_dir)

		assert "test.lib" in modules
		assert (out_dir / "index.md").exists()
		assert (out_dir / "test_lib.md").exists()

		# Index references the module file
		index_text = (out_dir / "index.md").read_text()
		assert "test.lib" in index_text
		assert "test_lib.md" in index_text

		# Module doc contains the function
		mod_text = (out_dir / "test_lib.md").read_text()
		assert "helper" in mod_text
		assert "A helper function." in mod_text


def test_generate_docs_for_real_std_text() -> None:
	"""generate_docs works on the real stdlib std.text module."""
	stdlib_text = Path("stdlib/std/text.drift")
	if not stdlib_text.exists():
		return  # skip if not in repo root

	with tempfile.TemporaryDirectory() as tmp:
		out_dir = Path(tmp) / "doc"
		modules = generate_docs(source_root=stdlib_text, output_dir=out_dir)

		assert "std.text" in modules
		doc_path = out_dir / "std_text.md"
		assert doc_path.exists()

		text = doc_path.read_text()
		# Verify key new functions appear
		assert "contains" in text
		assert "starts_with" in text
		assert "split" in text
		assert "index_of" in text
		assert "lower" in text
		assert "trim" in text
		assert "join" in text
		assert "replace" in text

		# Verify existing functions appear
		assert "substring" in text
		assert "string_builder" in text
		assert "sb_append_string" in text

		# Verify types appear
		assert "StringBuilder" in text
		assert "TextError" in text
		assert "TokenizeAction" in text

		# Verify doc comments are present
		assert "Returns `true` if `haystack` contains `needle`." in text

		# Verify private fields are NOT shown
		# StringBuilder has private fields buf, cap, len
		# They should not appear in the docs as field entries
		lines = text.splitlines()
		field_lines = [l for l in lines if "| `buf`" in l or "| `cap`" in l]
		assert len(field_lines) == 0, "private fields should not appear in docs"


def test_generate_docs_for_real_std_core_includes_builtin_type_methods() -> None:
	"""generate_docs on std.core includes methods on builtin types like String."""
	stdlib_core = Path("stdlib/std/core/copy.drift")
	if not stdlib_core.exists():
		return  # skip if not in repo root

	with tempfile.TemporaryDirectory() as tmp:
		out_dir = Path(tmp) / "doc"
		modules = generate_docs(source_root=stdlib_core, output_dir=out_dir)

		assert "std.core" in modules
		doc_path = out_dir / "std_core.md"
		assert doc_path.exists()

		text = doc_path.read_text()

		# String methods section must exist
		assert "## `String` methods" in text, (
			"generated docs must include a String methods section"
		)

		# String.clone() — the newly added cheap ARC clone
		assert "clone(self: &String)" in text, (
			"String.clone() must appear in generated docs"
		)
		assert "ARC refcount increment" in text, (
			"String.clone() doc comment about cheap ARC path must be preserved"
		)

		# Pre-existing String methods
		assert "byte_length(self: &String)" in text, (
			"String.byte_length() must appear in generated docs"
		)
		assert "byte_at(self: &String" in text, (
			"String.byte_at() must appear in generated docs"
		)


def test_builtin_type_methods_in_synthetic_module() -> None:
	"""Implement blocks on builtin types produce method entries in docs."""
	source = '''\
module test.ext;

export { greet };

pub fn greet() nothrow -> Int { return 0; }

implement String {
	/// Returns the string reversed.
	pub fn flip(self: &String) nothrow -> String {
		return *self;
	}
}
'''
	module_name, entries = extract_module_doc(source)
	method_entries = [e for e in entries if e.kind.startswith("method:")]
	assert len(method_entries) == 1, (
		f"expected 1 method on String, got {len(method_entries)}"
	)
	assert method_entries[0].name == "flip"
	assert "String" in method_entries[0].kind
	assert "Returns the string reversed." in method_entries[0].doc


def test_drift_doc_cli_subcommand() -> None:
	"""The drift doc CLI subcommand works end-to-end."""
	from lang.drift.cli import main as cli_main

	stdlib_text = Path("stdlib/std/text.drift")
	if not stdlib_text.exists():
		return

	with tempfile.TemporaryDirectory() as tmp:
		out_dir = Path(tmp) / "doc"
		rc = cli_main(["doc", str(stdlib_text), "-o", str(out_dir)])
		assert rc == 0
		assert (out_dir / "std_text.md").exists()
		assert (out_dir / "index.md").exists()


def test_bundle_docs_produces_stdlib_docs() -> None:
	"""bundle_docs_and_examples generates doc/stdlib/ with real content."""
	from tools.deploy.steps.bundle import bundle_docs_and_examples

	with tempfile.TemporaryDirectory() as tmp:
		dist = Path(tmp) / "dist"
		dist.mkdir()
		bundle_docs_and_examples(dist)

		# Check doc/stdlib/ exists with index, authoring guide, and std_text
		stdlib_doc = dist / "doc" / "stdlib"
		assert stdlib_doc.is_dir(), "doc/stdlib/ should be generated"
		assert (stdlib_doc / "index.md").exists(), "index.md should exist"
		assert (stdlib_doc / "authoring.md").exists(), "authoring.md should exist"
		assert (stdlib_doc / "std_text.md").exists(), "std_text.md should exist"

		# Check that the index lists modules and links to authoring guide
		index = (stdlib_doc / "index.md").read_text()
		assert "std.text" in index
		assert "authoring.md" in index

		# Check authoring guide content
		authoring = (stdlib_doc / "authoring.md").read_text()
		assert "///" in authoring
		assert "Parameters" in authoring
		assert "Returns" in authoring
		assert "exported" in authoring

		# Check std_text.md has the new functions
		text_doc = (stdlib_doc / "std_text.md").read_text()
		assert "contains" in text_doc
		assert "split" in text_doc

		# `effective-drift.md` is the canonical idiom guide and must ship
		# under `doc/` so consumers can read it after deployment.  It is
		# the home for user-facing language-feature documentation that is
		# not part of any stdlib module's API surface.
		effective_md = dist / "doc" / "effective-drift.md"
		assert effective_md.exists(), "doc/effective-drift.md should ship in the toolchain"
		effective = effective_md.read_text()
		# Method overload resolution by parameter type — new compiler feature.
		assert "Method overload resolution by parameter type" in effective
		assert "concrete overload plus a generic fallback" in effective
		assert "no matching overload" in effective
		# Call-site auto-borrow style guidance.
		assert "Call-site auto-borrow" in effective
		# Cheap String.clone() ARC semantics.
		assert "Cheap `String` clone" in effective
		assert "drift_string_retain" in effective

		# std_json.md must show both `get_path` overloads (the dotted-string
		# overload and the segment-array overload) — the new method overload
		# resolution feature is what makes this possible.
		json_doc = (stdlib_doc / "std_json.md").read_text()
		assert "get_path(self: &JsonNode, path: &String)" in json_doc
		assert "get_path(self: &JsonNode, path: &Array<String>)" in json_doc
