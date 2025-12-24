from __future__ import annotations

from lang2.driftc.parser import ast
from lang2.driftc.parser.parser import parse_program


def test_parse_program_collects_imports_exports_and_functions():
	source = """
module a.b
export { foo, Bar, c.d.* }
import c.d as cd

pub fn foo() returns Int { return 1; }
pub struct Bar { }
"""
	prog = parse_program(source)

	assert prog.module == "a.b"

	assert len(prog.exports) == 1
	export_items = prog.exports[0].items
	assert [type(i) for i in export_items] == [ast.ExportName, ast.ExportName, ast.ExportModuleStar]
	assert [i.name for i in export_items if isinstance(i, ast.ExportName)] == ["foo", "Bar"]
	star_item = next(i for i in export_items if isinstance(i, ast.ExportModuleStar))
	assert star_item.module_path == ["c", "d"]

	assert len(prog.imports) == 1
	imp = prog.imports[0]
	assert imp.path == ["c", "d"]
	assert imp.alias == "cd"

	assert [fn.name for fn in prog.functions] == ["foo"]
	assert prog.functions[0].is_pub is True
	assert [s.name for s in prog.structs] == ["Bar"]
	assert prog.structs[0].is_pub is True
