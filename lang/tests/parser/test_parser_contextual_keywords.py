# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression coverage for the contextual keywords `copy`, `move`, and
`share` — all three remain legal identifiers outside grammar
positions where the keyword spelling is itself meaningful (e.g.
`captures(copy x)`, `captures(share x)`, the `move` operator).

The grammar enumerates the path-segment / identifier set in a single
constant `_PATH_SEG_ALTS` (parser.py).  These tests pin that set's
behavior across the four user-visible naming contexts so a future
extension of the contextual-keyword set can't silently break the
existing identifier surface.
"""
from __future__ import annotations

import pytest
from lark.exceptions import UnexpectedInput

from lang.driftc.parser import ast
from lang.driftc.parser.parser import parse_program


# ---------------------------------------------------------------------------
# Module paths containing contextual keywords
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", ["copy", "move", "share"])
def test_module_path_segment_is_contextual_keyword(kw: str) -> None:
	src = f"""
module m;
import a.{kw}.b;
fn main() -> Int {{ return 0; }}
"""
	prog = parse_program(src)
	assert len(prog.imports) == 1
	assert prog.imports[0].path == ["a", kw, "b"]


@pytest.mark.parametrize("kw", ["copy", "move", "share"])
def test_use_trait_module_path_is_contextual_keyword(kw: str) -> None:
	src = f"""
module m;
use trait {kw}.SomeTrait;
fn main() -> Int {{ return 0; }}
"""
	prog = parse_program(src)
	assert len(prog.used_traits) == 1
	assert prog.used_traits[0].module_path == [kw]
	assert prog.used_traits[0].name == "SomeTrait"


def test_use_trait_share_dot_share_pins_design_path() -> None:
	# Concrete shape that appears in stdlib once Share lands:
	# `use trait shareable.Share` after `import std.core.shareable as shareable`.
	src = """
module m;
import std.core.shareable as shareable;
use trait shareable.Share;
fn main() -> Int { return 0; }
"""
	prog = parse_program(src)
	tr = prog.used_traits[0]
	assert tr.module_path == ["shareable"]
	assert tr.name == "Share"


# ---------------------------------------------------------------------------
# Local-variable names — contextual keywords must remain legal identifiers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", ["copy", "move", "share"])
def test_local_var_named_contextual_keyword(kw: str) -> None:
	src = f"""
module m;
fn main() -> Int {{
	val {kw}: Int = 1;
	return {kw};
}}
"""
	prog = parse_program(src)
	assert prog.functions[0].name == "main"


# ---------------------------------------------------------------------------
# Functions / exports named after a contextual keyword
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", ["copy", "move", "share"])
def test_pub_fn_and_export_with_keyword_name(kw: str) -> None:
	src = f"""
module m;
export {{ {kw} }};
pub fn {kw}() -> Int {{ return 0; }}
"""
	prog = parse_program(src)
	# Function defined with the keyword as its name.
	assert any(fn.name == kw for fn in prog.functions)
	# Exported under the same name.
	export_names = [
		item.name
		for stmt in prog.exports
		for item in stmt.items
		if isinstance(item, ast.ExportName)
	]
	assert kw in export_names


# ---------------------------------------------------------------------------
# Import alias — current grammar restricts the alias to NAME, so a
# contextual keyword IS NOT accepted there.  Pin the limitation so any
# future grammar relaxation is intentional.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", ["copy", "move", "share"])
def test_import_alias_rejects_contextual_keyword(kw: str) -> None:
	"""`import_alias: "as" NAME` — alias position is NAME-only today.

	`import x as <NAME>` works (test_import_alias_with_plain_name).
	`import x as <contextual-keyword>` raises a parse error.  Pin the
	current behavior; flip these expectations if/when the grammar
	relaxes the alias rule.
	"""
	src = f"""
module m;
import std.core.{kw} as {kw};
fn main() -> Int {{ return 0; }}
"""
	with pytest.raises(UnexpectedInput):
		parse_program(src)


def test_import_alias_with_plain_name_allows_keyword_path() -> None:
	# Path segment may be a contextual keyword; alias must be a plain NAME.
	src = """
module m;
import std.core.share as sh;
fn main() -> Int { return 0; }
"""
	prog = parse_program(src)
	imp = prog.imports[0]
	assert imp.path == ["std", "core", "share"]
	assert imp.alias == "sh"
