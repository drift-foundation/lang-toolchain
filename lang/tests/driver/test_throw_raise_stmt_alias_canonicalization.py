# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Parser/workspace-level pin (LANGUAGE_BUG): a qualified type inside a
`throw` (`RaiseStmt`) operand must be alias-canonicalized.

Directly pins the walker omission behind the cross-package missing-CallInfo ICE:
source `throw ...` parses to `parser_ast.RaiseStmt`, and the workspace alias
walker `_resolve_types_in_block` must traverse `RaiseStmt.value` (not only the
unused `ThrowStmt.expr`).  Without it, a nested qualified ctor inside the throw
operand keeps the raw import alias on its `base_type_expr.module_id`.

This is a fast, signing-free pin (no package boundary): it asserts the lowered
HIR's nested ctor base_type_expr carries the REAL module id, not the alias.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root

A_PKG = """\
module a_pkg;
import std.core as core;
use trait core.Diagnostic;
export { K, E };
pub variant K { Bad(detail: String), @tombstone Tomb }
implement core.Diagnostic for K {
\tpub fn to_json_text(self: &K) nothrow -> String {
\t\treturn match self { Bad(d) => { core.diagnostic_json_int(1) }, default => { core.diagnostic_json_int(-1) } };
\t}
}
pub error E { kind: K, tag: String }
"""

CONSUMER = """\
module consumer;
import a_pkg as a;
fn trigger() throws a.E -> Int { throw a.E(kind = a.K::Bad(detail = "x"), tag = "t"); }
fn main() nothrow -> Int { return 0; }
"""


def _collect_qualified_members(node, seen=None, out=None):
	"""Recursively collect HQualifiedMember nodes reachable through the throw
	operand path (block/stmt/expr containers)."""
	if seen is None:
		seen, out = set(), []
	if node is None or id(node) in seen:
		return out
	seen.add(id(node))
	if hasattr(node, "member") and hasattr(node, "base_type_expr"):
		out.append(node)
	if isinstance(node, (list, tuple)):
		for el in node:
			_collect_qualified_members(el, seen, out)
		return out
	d = getattr(node, "__dict__", None)
	if d:
		for v in d.values():
			if isinstance(v, (list, tuple)) or hasattr(v, "__dict__"):
				_collect_qualified_members(v, seen, out)
	return out


def test_qualified_ctor_in_throw_operand_is_alias_canonicalized(tmp_path: Path) -> None:
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")
	(tmp_path / "a_pkg.drift").write_text(A_PKG)
	(tmp_path / "consumer.drift").write_text(CONSUMER)

	modules, _tt, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir(
		[tmp_path / "a_pkg.drift", tmp_path / "consumer.drift"],
		module_paths=[tmp_path],
		stdlib_root=stdlib,
	)
	assert not [d for d in diags if getattr(d, "severity", None) == "error"], diags

	consumer = modules["consumer"]
	qmems = []
	for block in consumer.func_hirs.values():
		qmems.extend(_collect_qualified_members(block))
	bad = [q for q in qmems if getattr(q, "member", None) == "Bad"]
	assert bad, "did not find the nested `a.K::Bad` qualified-member ctor in consumer HIR"
	for q in bad:
		bte = q.base_type_expr
		mod_id = getattr(bte, "module_id", None)
		# After RaiseStmt-operand canonicalization the import alias `a` is
		# resolved to the real module id `a_pkg`.  The bug left it raw
		# (module_id=None / module_alias='a').
		assert mod_id == "a_pkg", (
			f"nested ctor base_type_expr not alias-canonicalized: "
			f"module_id={mod_id!r} module_alias={getattr(bte, 'module_alias', None)!r}"
		)
