# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""R10 analysis-library extraction — fail-closed AST import pins
(string-arc-endgame-r10-extraction, 2026-07-20).

Two structural contracts, both enforced by AST walks (immune to
aliasing, relative/absolute forms, and multiline imports — a textual
`.string_arc` scan is insufficient per review):

1. `string_ownership_analysis` is NEUTRAL: it must never import
   string_arc (that edge would recreate the cycle the extraction
   exists to prevent).
2. No production or test module imports a MOVED R10 member from
   string_arc — every consumer goes through the neutral module.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

MOVED = {
	"iter_used_values",
	"seed_string_dest_types",
	"is_materialized_release_family_producer",
	"build_fnwide_producers",
	"compute_lastuse_release_points",
	"recognize_materialized_releases",
	"compute_string_temp_liveness",
	"string_operand_dispositions",
	"DISPOSITION_CONSUME",
	"DISPOSITION_USE",
	"DISPOSITION_IGNORE",
	"DRIFT_STRING_HELPER_SYMBOLS",
	"_analyze_lastuse_block",
	"_is_semantic_string_tid",
}


def _imports_of(tree: ast.AST):
	"""Yield (module_str, [names], lineno) for every ImportFrom and
	(module_str, lineno) for every plain Import — module_str WITHOUT
	relative dots (level captured separately by the caller's check)."""
	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom):
			yield ("from", node.module or "", [a.name for a in node.names], node.lineno)
		elif isinstance(node, ast.Import):
			for a in node.names:
				yield ("import", a.name, [], node.lineno)


def test_neutral_module_never_imports_string_arc() -> None:
	p = ROOT / "lang" / "driftc" / "stage2" / "string_ownership_analysis.py"
	tree = ast.parse(p.read_text())
	offenders = []
	for kind, mod, names, lineno in _imports_of(tree):
		# `import ...string_arc` / `from ...string_arc import X`.
		if mod.split(".")[-1] == "string_arc" or mod.endswith(".string_arc"):
			offenders.append(f"line {lineno}: {kind} {mod}")
		# `from . import string_arc` / `from ...stage2 import string_arc`
		# — the module is the PACKAGE and `string_arc` is an imported
		# NAME (node.module is empty for the bare-package form).
		elif kind == "from" and "string_arc" in names:
			offenders.append(f"line {lineno}: from {mod or '.'} import string_arc")
	assert not offenders, (
		"string_ownership_analysis must stay neutral — it may never "
		"import string_arc:\n" + "\n".join(offenders)
	)


def _is_string_arc_module(mod: str) -> bool:
	return bool(mod) and mod.split(".")[-1] == "string_arc"


def test_no_moved_member_reachable_from_string_arc() -> None:
	"""No production/test module may reach a moved R10 member through
	string_arc — closing BOTH escapes:

	1. ImportFrom — relative (`from .string_arc import ...` at any
	   level) or absolute (`from lang.driftc.stage2.string_arc import
	   ...`), aliased or not, single-line or multiline — naming a
	   moved member.
	2. Module import + attribute access — `import
	   lang.driftc.stage2.string_arc as sa` (or plain, or
	   `from lang.driftc.stage2 import string_arc [as sa]`) followed
	   by `sa.<MOVED>` / `string_arc.<MOVED>`.  The pin binds every
	   local name that resolves to the string_arc module, then flags
	   any `Attribute` whose value is such a name and whose attr is a
	   moved member.

	If this fails: STOP — retarget to `string_ownership_analysis`
	instead of relaxing the scan."""
	offenders = []
	for base in (ROOT / "lang", ROOT / "tools"):
		for py in sorted(base.rglob("*.py")):
			if "__pycache__" in py.parts:
				continue
			if py.name == "string_ownership_analysis.py":
				continue
			tree = ast.parse(py.read_text())
			rel = py.relative_to(ROOT)
			# Local names bound to the string_arc MODULE.
			module_aliases: set[str] = set()
			for node in ast.walk(tree):
				if isinstance(node, ast.Import):
					# import a.b.string_arc [as sa]
					for a in node.names:
						if _is_string_arc_module(a.name):
							module_aliases.add(a.asname or a.name.split(".")[0])
				elif isinstance(node, ast.ImportFrom):
					mod = node.module or ""
					if _is_string_arc_module(mod):
						# from ...string_arc import <MOVED> — escape 1.
						for a in node.names:
							if a.name in MOVED:
								offenders.append(
									f"{rel}:{node.lineno}: import "
									f"{a.name}"
									+ (f" as {a.asname}" if a.asname else "")
								)
					else:
						# from ...stage2 import string_arc [as sa]
						for a in node.names:
							if a.name == "string_arc":
								module_aliases.add(a.asname or a.name)
			# Attribute access on any string_arc-module alias — escape 2.
			if module_aliases:
				for node in ast.walk(tree):
					if (
						isinstance(node, ast.Attribute)
						and isinstance(node.value, ast.Name)
						and node.value.id in module_aliases
						and node.attr in MOVED
					):
						offenders.append(
							f"{rel}:{node.lineno}: "
							f"{node.value.id}.{node.attr}"
						)
	assert not offenders, (
		"moved R10 member(s) still reachable through string_arc — "
		"retarget to string_ownership_analysis:\n" + "\n".join(offenders)
	)
