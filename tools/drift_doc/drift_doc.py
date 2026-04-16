#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift-doc — API documentation generator for Drift modules.

Extracts public API surface from .drift source files using the compiler's
parser and combines it with declaration-adjacent ``///`` doc comments to
produce Markdown reference documentation.

Usage (standalone):
    python3 -m tools.drift_doc.drift_doc stdlib/std/text.drift -o doc/stdlib/
    python3 -m tools.drift_doc.drift_doc stdlib/std/ -o doc/stdlib/

Usage (from deploy pipeline):
    from tools.drift_doc.drift_doc import generate_docs
    generate_docs(source_root=Path("stdlib/std"), output_dir=dist / "doc" / "stdlib")
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from lang.driftc.parser.parser import parse_program
from lang.driftc.parser import ast as A


# ── Type formatting ──────────────────────────────────────────────────

def _format_type(te: A.TypeExpr | None) -> str:
	"""Render a TypeExpr back to surface syntax."""
	if te is None:
		return "?"
	name = te.name

	# Reference types: &T, &mut T
	if name == "&":
		if te.args:
			return f"&{_format_type(te.args[0])}"
		return "&?"
	if name == "&mut":
		if te.args:
			return f"&mut {_format_type(te.args[0])}"
		return "&mut ?"

	# Function types: Fn(A, B) -> R or Fn(A, B) nothrow -> R
	if name == "fn":
		if not te.args:
			return "Fn()"
		params = te.args[:-1]
		ret = te.args[-1]
		param_str = ", ".join(_format_type(p) for p in params)
		nothrow = " nothrow" if not te.fn_throws else ""
		return f"Fn({param_str}){nothrow} -> {_format_type(ret)}"

	# Generic types: Array<T>, Result<T, E>, etc.
	if te.args:
		args_str = ", ".join(_format_type(a) for a in te.args)
		prefix = ""
		if te.module_alias:
			prefix = f"{te.module_alias}."
		return f"{prefix}{name}<{args_str}>"

	# Simple types with module qualifier
	if te.module_alias:
		return f"{te.module_alias}.{name}"

	return name


def _format_param(p: A.Param) -> str:
	"""Render a function parameter."""
	mut = "var " if p.mutable else ""
	if p.type_expr is None:
		return f"{mut}{p.name}"
	return f"{mut}{p.name}: {_format_type(p.type_expr)}"


def _format_fn_signature(fn: A.FunctionDef) -> str:
	"""Render a function's full signature (without body)."""
	type_params = ""
	if fn.type_params:
		type_params = f"<{', '.join(fn.type_params)}>"

	params_str = ", ".join(_format_param(p) for p in fn.params)

	throws = ""
	if fn.declared_nothrow:
		throws = " nothrow"
	elif fn.declared_throws:
		throws = " throws"

	ret = _format_type(fn.return_type)
	if ret == "Void" and fn.declared_nothrow:
		return f"{fn.name}{type_params}({params_str}){throws} -> Void"

	return f"{fn.name}{type_params}({params_str}){throws} -> {ret}"


# ── Doc comment extraction ───────────────────────────────────────────

def _extract_doc_comments(source: str) -> dict[int, str]:
	"""Extract ``///`` doc comments keyed by the line number they precede.

	Returns a dict mapping 1-based line number of the *declaration* to
	the combined doc comment text (with ``/// `` prefixes stripped).
	"""
	lines = source.splitlines()
	result: dict[int, str] = {}
	i = 0
	while i < len(lines):
		block: list[str] = []
		while i < len(lines) and lines[i].lstrip().startswith("///"):
			raw = lines[i].lstrip()
			# Strip "/// " or "///" prefix
			if raw.startswith("/// "):
				block.append(raw[4:])
			else:
				block.append(raw[3:])
			i += 1
		if block:
			# Skip blank lines and `@annotation` lines between the comment
			# and the declaration we're keying to.  Annotations like
			# `@test_build_only` and `@intrinsic` sit on their own line
			# above the `pub fn`/`pub struct`/etc. declaration; without
			# this skip, the doc comment is keyed to the annotation line
			# and the renderer never finds it.
			while i < len(lines) and (
				not lines[i].strip() or lines[i].lstrip().startswith("@")
			):
				i += 1
			if i < len(lines):
				# Map to 1-based line number
				result[i + 1] = "\n".join(block)
		i += 1
	return result


# ── Module doc extraction ────────────────────────────────────────────

class DocEntry:
	"""A documented API entry."""
	__slots__ = ("kind", "name", "signature", "doc", "children")

	def __init__(self, kind: str, name: str, signature: str = "",
				 doc: str = "", children: list[DocEntry] | None = None):
		self.kind = kind
		self.name = name
		self.signature = signature
		self.doc = doc
		self.children = children or []


def extract_module_doc(source: str, filename: str = "<source>") -> tuple[str, list[DocEntry]]:
	"""Parse a .drift source file and extract its public API surface.

	Returns (module_name, entries) where entries are the exported public
	declarations with their doc comments.
	"""
	program = parse_program(source, filename=filename)
	doc_comments = _extract_doc_comments(source)

	module_name = program.module or "<unknown>"

	# Collect exported names
	exported: set[str] = set()
	has_star_reexports = False
	for exp in program.exports:
		for item in exp.items:
			if isinstance(item, A.ExportName):
				exported.add(item.name)
			elif isinstance(item, A.ExportModuleStar):
				has_star_reexports = True

	entries: list[DocEntry] = []

	# Functions (top-level, non-method, exported)
	for fn in program.functions:
		if fn.name not in exported:
			continue
		if fn.is_method:
			continue
		doc = doc_comments.get(fn.loc.line, "")
		sig = _format_fn_signature(fn)
		entries.append(DocEntry("function", fn.name, sig, doc))

	# Structs
	for st in program.structs:
		if st.name not in exported:
			continue
		doc = doc_comments.get(st.loc.line, "")
		type_params = ""
		if st.type_params:
			type_params = f"<{', '.join(st.type_params)}>"
		children = []
		for f in st.fields:
			if f.is_pub:
				children.append(DocEntry("field", f.name, _format_type(f.type_expr)))
		entries.append(DocEntry("struct", f"{st.name}{type_params}", "", doc, children))

	# Variants
	for vt in program.variants:
		if vt.name not in exported:
			continue
		doc = doc_comments.get(vt.loc.line, "")
		type_params = ""
		if vt.type_params:
			type_params = f"<{', '.join(vt.type_params)}>"
		children = []
		for arm in vt.arms:
			if arm.fields:
				fields_str = ", ".join(f"{f.name}: {_format_type(f.type_expr)}" for f in arm.fields)
				children.append(DocEntry("case", arm.name, f"({fields_str})"))
			else:
				children.append(DocEntry("case", arm.name))
		entries.append(DocEntry("variant", f"{vt.name}{type_params}", "", doc, children))

	# Interfaces
	for iface in program.interfaces:
		if iface.name not in exported:
			continue
		doc = doc_comments.get(iface.loc.line, "")
		type_params = ""
		if iface.type_params:
			type_params = f"<{', '.join(iface.type_params)}>"
		children = []
		for m in iface.methods:
			params_str = ", ".join(_format_param(p) for p in m.params)
			throws = ""
			if m.declared_nothrow:
				throws = " nothrow"
			elif m.declared_throws:
				throws = " throws"
			ret = _format_type(m.return_type)
			method_sig = f"{m.name}({params_str}){throws} -> {ret}"
			method_doc = doc_comments.get(m.loc.line, "")
			children.append(DocEntry("method", m.name, method_sig, method_doc))
		entries.append(DocEntry("interface", f"{iface.name}{type_params}", "", doc, children))

	# Exceptions
	for exc in program.exceptions:
		if exc.name not in exported:
			continue
		doc = doc_comments.get(exc.loc.line, "")
		children = []
		for arg in exc.args:
			children.append(DocEntry("field", arg.name, _format_type(arg.type_expr)))
		entries.append(DocEntry("exception", exc.name, "", doc, children))

	# Constants
	for c in program.consts:
		if c.name not in exported:
			continue
		doc = doc_comments.get(c.loc.line, "")
		entries.append(DocEntry("constant", c.name, _format_type(c.type_expr), doc))

	# Implement blocks — extract public methods for exported or builtin types
	_BUILTIN_TYPES = {"String", "Int", "Uint", "Float", "Bool", "Byte", "Uint64"}
	for impl in program.implements:
		if impl.trait is not None:
			continue  # trait impls are internal
		target_name = _format_type(impl.target) if impl.target else ""
		# Strip generic parameters (`Arc<T>` -> `Arc`) when comparing
		# against the export set, since exports list the bare type
		# name.  Without this, generic impl blocks like
		# `implement<T> Arc<T> { ... }` would be silently filtered out.
		target_base = target_name.split("<", 1)[0] if "<" in target_name else target_name
		if target_base not in exported and target_base not in _BUILTIN_TYPES:
			continue
		for m in impl.methods:
			if not m.is_pub:
				continue
			# Only include methods not already exported as top-level functions
			if m.name in exported:
				continue
			doc = doc_comments.get(m.loc.line, "")
			sig = _format_fn_signature(m)
			# Tag with target type for grouped rendering — use the base
			# name (without generic params) to keep section headings
			# clean.
			entry = DocEntry("method", m.name, sig, doc)
			entry.kind = f"method:{target_base}"
			entries.append(entry)

	return module_name, entries


# ── Markdown rendering ───────────────────────────────────────────────

def render_module_markdown(module_name: str, entries: list[DocEntry]) -> str:
	"""Render a module's API entries to Markdown."""
	lines: list[str] = []
	lines.append(f"# {module_name}")
	lines.append("")

	# Group by kind
	functions = [e for e in entries if e.kind == "function"]
	structs = [e for e in entries if e.kind == "struct"]
	variants = [e for e in entries if e.kind == "variant"]
	interfaces = [e for e in entries if e.kind == "interface"]
	exceptions = [e for e in entries if e.kind == "exception"]
	constants = [e for e in entries if e.kind == "constant"]
	methods = [e for e in entries if e.kind == "method"]

	if functions:
		lines.append("## Functions")
		lines.append("")
		for fn in functions:
			lines.append(f"### `{fn.signature}`")
			lines.append("")
			if fn.doc:
				lines.append(fn.doc)
				lines.append("")

	if structs:
		lines.append("## Types")
		lines.append("")
		for st in structs:
			lines.append(f"### struct `{st.name}`")
			lines.append("")
			if st.doc:
				lines.append(st.doc)
				lines.append("")
			if st.children:
				lines.append("| Field | Type |")
				lines.append("|-------|------|")
				for f in st.children:
					lines.append(f"| `{f.name}` | `{f.signature}` |")
				lines.append("")

	if variants:
		for vt in variants:
			lines.append(f"### variant `{vt.name}`")
			lines.append("")
			if vt.doc:
				lines.append(vt.doc)
				lines.append("")
			if vt.children:
				lines.append("| Case | Fields |")
				lines.append("|------|--------|")
				for arm in vt.children:
					lines.append(f"| `{arm.name}` | {arm.signature or '—'} |")
				lines.append("")

	if interfaces:
		for iface in interfaces:
			lines.append(f"### interface `{iface.name}`")
			lines.append("")
			if iface.doc:
				lines.append(iface.doc)
				lines.append("")
			if iface.children:
				for m in iface.children:
					lines.append(f"- `{m.signature}`")
					if m.doc:
						lines.append(f"  {m.doc.splitlines()[0]}")
				lines.append("")

	if exceptions:
		for exc in exceptions:
			lines.append(f"### exception `{exc.name}`")
			lines.append("")
			if exc.doc:
				lines.append(exc.doc)
				lines.append("")
			if exc.children:
				lines.append("| Field | Type |")
				lines.append("|-------|------|")
				for f in exc.children:
					lines.append(f"| `{f.name}` | `{f.signature}` |")
				lines.append("")

	if constants:
		lines.append("## Constants")
		lines.append("")
		for c in constants:
			lines.append(f"### `{c.name}: {c.signature}`")
			lines.append("")
			if c.doc:
				lines.append(c.doc)
				lines.append("")

	# Methods grouped by target type
	method_entries = [e for e in entries if e.kind.startswith("method:")]
	if method_entries:
		# Group by target type
		by_target: dict[str, list[DocEntry]] = {}
		for m in method_entries:
			target = m.kind.split(":", 1)[1]
			by_target.setdefault(target, []).append(m)
		for target, meths in sorted(by_target.items()):
			lines.append(f"## `{target}` methods")
			lines.append("")
			for m in meths:
				lines.append(f"### `{m.signature}`")
				lines.append("")
				if m.doc:
					lines.append(m.doc)
					lines.append("")

	return "\n".join(lines)


# ── Authoring guide ──────────────────────────────────────────────────

_AUTHORING_GUIDE = """\
# Writing Doc Comments

`drift doc` extracts API documentation from declaration-adjacent `///`
comments in `.drift` source files and combines them with parsed
signatures to produce Markdown reference pages.

## Basics

Place `///` comments immediately before a declaration. The comment
content is Markdown.

```drift
/// Returns `true` if `haystack` contains `needle`.
///
/// Empty needle returns `true`.
pub fn contains(haystack: &String, needle: &String) nothrow -> Bool {
```

- The first line is the **summary** — keep it to one sentence.
- A blank `///` line separates the summary from the extended description.
- Everything after the `///` prefix (and one optional space) is preserved
  as-is, so standard Markdown formatting works: `code spans`, **bold**,
  lists, links, etc.

## Recommended sections

For richer documentation that future renderers (HTML, IDE tooltips) can
parse into structured output, use these heading labels inside the doc
comment:

```drift
/// Splits `s` on every non-overlapping occurrence of `delimiter`.
///
/// ## Parameters
///
/// - `s` — the string to split.
/// - `delimiter` — the separator to split on.
/// - `max` — maximum number of splits (the last element contains
///   the unsplit remainder).
///
/// ## Returns
///
/// An array of substrings. If `delimiter` is not found, returns a
/// single-element array containing `s`.
///
/// ## Errors
///
/// This function does not return errors.
///
/// ## Examples
///
/// ```drift
/// val parts = text.split(&"a,b,c", &",");
/// // parts == ["a", "b", "c"]
/// ```
///
/// ## See also
///
/// - `split_limit` — split with a maximum number of splits.
pub fn split(s: &String, delimiter: &String) nothrow -> Array<String> {
```

These sections are optional. When present, `drift doc` preserves them
in the generated Markdown, and future HTML generators can render them
with distinct styling.

| Section | When to use |
|---------|-------------|
| **Parameters** | Describe each parameter beyond what the type conveys |
| **Returns** | Describe the return value, especially edge cases |
| **Errors** | Document error conditions (`Result::Err`, exceptions) |
| **Notes** | Implementation notes, performance, caveats |
| **Examples** | Short usage examples in fenced code blocks |
| **See also** | Cross-references to related functions or types |

## What gets documented

- Only **exported** symbols (those listed in the `export { ... }` block)
  appear in the generated docs.
- For structs, only **public fields** (`pub` modifier) are shown.
  Private fields are omitted.
- Functions, types, variants, interfaces, exceptions, and constants are
  all extracted.
- Declarations without a `///` comment still appear with their full
  signature — no prose, but discoverable.

## Running the generator

```bash
# Document a single file
drift doc path/to/module.drift -o doc/

# Document all .drift files under a directory
drift doc stdlib/std/ -o doc/stdlib/

# The output directory will contain:
#   index.md          — module listing with links
#   std_text.md       — per-module reference (one file per module)
#   authoring.md      — this guide
```
"""


# ── Batch generation ─────────────────────────────────────────────────

def generate_docs(source_root: Path, output_dir: Path) -> list[str]:
	"""Generate Markdown docs for all .drift files under source_root.

	Returns list of module names that were documented.
	"""
	output_dir.mkdir(parents=True, exist_ok=True)
	modules: list[tuple[str, Path]] = []

	drift_files: list[Path]
	if source_root.is_file():
		drift_files = [source_root]
	else:
		drift_files = sorted(source_root.rglob("*.drift"))

	for drift_file in drift_files:
		source = drift_file.read_text(encoding="utf-8")
		try:
			module_name, entries = extract_module_doc(source, filename=str(drift_file))
		except Exception as e:
			print(f"[drift-doc] warning: failed to parse {drift_file}: {e}", file=sys.stderr)
			continue

		if not entries:
			continue

		md = render_module_markdown(module_name, entries)
		out_name = module_name.replace(".", "_") + ".md"
		out_path = output_dir / out_name
		out_path.write_text(md, encoding="utf-8")
		modules.append((module_name, out_path))
		print(f"[drift-doc] {module_name} -> {out_path}", flush=True)

	# Generate authoring guide
	(output_dir / "authoring.md").write_text(_AUTHORING_GUIDE, encoding="utf-8")

	# Generate index
	if modules:
		index_lines = ["# Drift Standard Library API Reference", ""]
		index_lines.append("| Module | Reference |")
		index_lines.append("|--------|-----------|")
		for mod_name, mod_path in sorted(modules):
			link = mod_path.name
			index_lines.append(f"| `{mod_name}` | [{link}]({link}) |")
		index_lines.append("")
		index_lines.append("---")
		index_lines.append("")
		index_lines.append("[Doc comment authoring guide](authoring.md)")
		index_lines.append("")
		(output_dir / "index.md").write_text("\n".join(index_lines), encoding="utf-8")
		print(f"[drift-doc] index -> {output_dir / 'index.md'}", flush=True)

	return [m for m, _ in modules]


# ── CLI ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(
		prog="drift-doc",
		description="Generate API reference documentation from Drift source files.",
	)
	parser.add_argument(
		"source",
		type=Path,
		help="A .drift file or directory of .drift files to document",
	)
	parser.add_argument(
		"-o", "--output",
		type=Path,
		default=Path("doc/stdlib"),
		help="Output directory for generated Markdown (default: doc/stdlib/)",
	)
	args = parser.parse_args(argv)

	modules = generate_docs(args.source, args.output)
	if not modules:
		print("[drift-doc] no modules documented", file=sys.stderr)
		return 1
	print(f"[drift-doc] documented {len(modules)} module(s)", flush=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
