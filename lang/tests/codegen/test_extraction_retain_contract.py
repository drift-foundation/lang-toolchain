# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Owned-at-extraction static contract pin (cleanup slice 1a).

THE INVARIANT: any codegen extraction lowering that RETAINS the
extracted value (`_emit_copy_value` on the loaded element/field) must be
a TERMINAL producer in the stake pass — never a stakeable view.
Violating it is the B-arch-1d leak shape: the stake copies from an
already-owned dest and orphans the codegen +1, one leaked ref per
extraction (caught 2026-07-10 by the heap-string e2e fixtures only
because static literals mask the imbalance).

AUTHORITY MODEL (review round 2): the classification authority is the
AST-INFERRED INSTRUCTION CONTEXT of each `_emit_copy_value` call site —
the innermost enclosing `isinstance(instr, <Node>)` branch, or the
enclosing lowering function's `instr: <Node>` parameter annotation, or
the helper-function identity when no instruction context exists.  The
`# owned-at-extraction:` / `# copy-construction:` marker comments merely
DOCUMENT the contract at each site and must AGREE with the inferred
context's classification — a mislabeled marker fails; a new copy site in
an unclassified context fails with a STOP/REPORT message regardless of
what marker someone wrote.

Classification table (edits here are the reviewed decision point):
- extraction (retains an element/field read out of an existing
  aggregate; MUST be terminal in string_stakes): ArrayIndexLoad,
  ArrayIndexLoadUnchecked, VariantGetField — three node names, two
  conceptual families.
- construction (the copy feeds a NEW value/aggregate): the CopyValue
  instruction itself, ArrayLit element stakes, and the copy machinery's
  own helpers (_emit_array_dup_value, _emit_copy_value_inner).
Adding a NEW context to this table — especially an instruction-shaped
one — requires the slice-1a STOP/REPORT protocol: a report and a
heap-string valgrind probe BEFORE normalization (see
work/string-ownership-refactor/CLEANUP-EXECUTION-PLAN.md).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CODEGEN = ROOT / "lang" / "codegen" / "llvm" / "llvm_codegen.py"
STAKES = ROOT / "lang" / "driftc" / "stage2" / "string_stakes.py"

# context key: ("instr", NodeName) or ("helper", function_name)
EXTRACTION_CONTEXTS = {
	("instr", "ArrayIndexLoad"),
	("instr", "ArrayIndexLoadUnchecked"),
	("instr", "VariantGetField"),
}
CONSTRUCTION_CONTEXTS = {
	("instr", "CopyValue"),
	("instr", "ArrayLit"),
	("helper", "_emit_array_dup_value"),
	("helper", "_emit_copy_value_inner"),
}
EXPECTED_EXTRACTION_SET = {n for kind, n in EXTRACTION_CONTEXTS if kind == "instr"}

_MARKER_RE = re.compile(
	r"#\s*(owned-at-extraction:\s*(?P<node>\w+)|copy-construction:)"
)
_MARKER_LOOKBACK_LINES = 3


def _copy_value_call_lines(tree: ast.AST) -> list[int]:
	lines: list[int] = []
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		fn = node.func
		name = fn.attr if isinstance(fn, ast.Attribute) else (
			fn.id if isinstance(fn, ast.Name) else None)
		if name == "_emit_copy_value":
			lines.append(node.lineno)
	return sorted(lines)


def _isinstance_names(test: ast.expr) -> set[str]:
	"""Node names from an `isinstance(instr, X)` / `(X, Y)` test expr."""
	if not (isinstance(test, ast.Call) and isinstance(test.func, ast.Name)
	        and test.func.id == "isinstance" and len(test.args) == 2):
		return set()
	subj, ty = test.args
	subj_name = subj.id if isinstance(subj, ast.Name) else None
	if subj_name != "instr":
		return set()
	parts = list(ty.elts) if isinstance(ty, ast.Tuple) else [ty]
	names: set[str] = set()
	for p in parts:
		if isinstance(p, ast.Attribute):
			names.add(p.attr)
		elif isinstance(p, ast.Name):
			names.add(p.id)
	return names


def _enclosing_function(tree: ast.AST, line: int) -> ast.FunctionDef | None:
	best = None
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef) and node.lineno <= line <= (node.end_lineno or 0):
			if best is None or node.lineno > best.lineno:
				best = node
	return best


def _infer_context(tree: ast.AST, line: int) -> tuple[str, str] | None:
	"""The AST-derived instruction context of a call site: the innermost
	`isinstance(instr, X)` branch BODY containing the line; else the
	enclosing function's `instr: <Node>` annotation; else the enclosing
	helper function's identity."""
	# Innermost isinstance(instr, ...) branch whose BODY spans the line.
	best: tuple[int, set[str]] | None = None
	for node in ast.walk(tree):
		if not isinstance(node, ast.If):
			continue
		names = _isinstance_names(node.test)
		if not names:
			continue
		body_start = node.body[0].lineno
		body_end = max(getattr(n, "end_lineno", n.lineno) or n.lineno for n in node.body)
		if body_start <= line <= body_end:
			if best is None or body_start > best[0]:
				best = (body_start, names)
	if best is not None:
		names = best[1]
		if len(names) == 1:
			return ("instr", next(iter(names)))
		# A multi-node isinstance branch is ambiguous for classification;
		# treat as unclassified so it surfaces for review.
		return ("instr", "+".join(sorted(names)))
	fn = _enclosing_function(tree, line)
	if fn is None:
		return None
	for arg in fn.args.args:
		if arg.arg == "instr" and arg.annotation is not None:
			ann = arg.annotation
			if isinstance(ann, ast.Attribute):
				return ("instr", ann.attr)
			if isinstance(ann, ast.Name):
				return ("instr", ann.id)
	return ("helper", fn.name)


def _marker_for_line(src_lines: list[str], call_line: int) -> re.Match | None:
	lo = max(0, call_line - 1 - _MARKER_LOOKBACK_LINES)
	for i in range(call_line - 1, lo - 1, -1):
		m = _MARKER_RE.search(src_lines[i])
		if m:
			return m
	return None


def _scan() -> tuple[set[str], list[str]]:
	"""Returns (AST-inferred extraction node set, violations)."""
	src = CODEGEN.read_text()
	tree = ast.parse(src)
	src_lines = src.splitlines()
	extraction: set[str] = set()
	violations: list[str] = []
	call_lines = _copy_value_call_lines(tree)
	assert call_lines, "scan self-check: no _emit_copy_value call sites found — scanner broken?"
	for line in call_lines:
		fn = _enclosing_function(tree, line)
		if fn is not None and fn.name == "_emit_copy_value":
			continue  # the public wrapper delegating to _inner: plumbing
		ctx = _infer_context(tree, line)
		if ctx is None:
			violations.append(f"llvm_codegen.py:{line}: _emit_copy_value at module "
			                  f"scope — unclassifiable context")
			continue
		marker = _marker_for_line(src_lines, line)
		if ctx in EXTRACTION_CONTEXTS:
			extraction.add(ctx[1])
			if marker is None or marker.group("node") != ctx[1]:
				violations.append(
					f"llvm_codegen.py:{line}: context {ctx} is EXTRACTION but the "
					f"site marker is {'missing' if marker is None else 'wrong'} — "
					f"must be `# owned-at-extraction: {ctx[1]}`"
				)
		elif ctx in CONSTRUCTION_CONTEXTS:
			if marker is None or marker.group("node") is not None:
				violations.append(
					f"llvm_codegen.py:{line}: context {ctx} is CONSTRUCTION but the "
					f"site marker is {'missing' if marker is None else 'mislabeled as extraction'} — "
					f"must be `# copy-construction: <reason>`"
				)
		else:
			violations.append(
				f"llvm_codegen.py:{line}: STOP/REPORT — _emit_copy_value in "
				f"UNCLASSIFIED context {ctx}.  If this is a new retaining "
				f"extraction lowering, it is a candidate live leak-class (the "
				f"B-arch-1d shape): write a report and a heap-string valgrind "
				f"probe BEFORE adding the context to the classification table, "
				f"and make the node terminal in string_stakes.  A marker comment "
				f"alone cannot classify it — the table in this test is the "
				f"reviewed decision point (CLEANUP-EXECUTION-PLAN.md slice 1a)."
			)
	return extraction, violations


def _stakes_view_isinstance_names() -> set[str]:
	src = STAKES.read_text()
	tree = ast.parse(src)
	target: ast.FunctionDef | None = None
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef) and node.name == "_is_string_value_view":
			target = node
			break
	assert target is not None, "string_stakes._is_string_value_view not found — contract anchor moved?"
	names: set[str] = set()
	for node in ast.walk(target):
		if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"):
			continue
		if len(node.args) != 2:
			continue
		ty_arg = node.args[1]
		parts = [ty_arg] if not isinstance(ty_arg, ast.Tuple) else list(ty_arg.elts)
		for p in parts:
			if isinstance(p, ast.Attribute):
				names.add(p.attr)
			elif isinstance(p, ast.Name):
				names.add(p.id)
	assert names, "scan self-check: no isinstance() tests found in _is_string_value_view"
	return names


def test_every_copy_site_classified_and_marker_agrees() -> None:
	_, violations = _scan()
	assert not violations, "\n".join(violations)


def test_extraction_set_matches_expected() -> None:
	extraction, _ = _scan()
	assert extraction == EXPECTED_EXTRACTION_SET, (
		f"AST-inferred extraction set {sorted(extraction)} != expected "
		f"{sorted(EXPECTED_EXTRACTION_SET)} — see the STOP/REPORT protocol in "
		f"the module docstring before changing either."
	)


def test_extraction_nodes_are_terminal_in_stake_pass() -> None:
	extraction, _ = _scan()
	view_names = _stakes_view_isinstance_names()
	overlap = extraction & view_names
	assert not overlap, (
		f"owned-at-extraction node(s) {sorted(overlap)} appear in "
		f"string_stakes._is_string_value_view isinstance tests — staking an "
		f"already-owned extraction dest orphans the codegen +1 (the 1d leak). "
		f"They must remain TERMINAL producers."
	)
	stakes_src = STAKES.read_text()
	for node in sorted(extraction):
		assert f"# owned-at-extraction: {node}" in stakes_src, (
			f"string_stakes.py is missing the `# owned-at-extraction: {node}` "
			f"marker at its terminal-list site"
		)


def test_mislabeled_marker_is_caught(tmp_path, monkeypatch) -> None:
	"""The teeth of the authority model: relabel the ArrayIndexLoad site
	as `# copy-construction:` in a doctored copy — the AST-inferred
	context must still classify it as extraction and FAIL on the marker
	disagreement (pre-review-round, the marker alone was authority and
	this passed silently)."""
	src = CODEGEN.read_text()
	doctored = src.replace(
		"# owned-at-extraction: ArrayIndexLoad\n",
		"# copy-construction: mislabeled by a future editor\n",
		1,
	)
	assert doctored != src, "doctoring anchor missing — marker text moved?"
	fake = tmp_path / "llvm_codegen.py"
	fake.write_text(doctored)
	import lang.tests.codegen.test_extraction_retain_contract as mod
	monkeypatch.setattr(mod, "CODEGEN", fake)
	extraction, violations = mod._scan()
	# Context authority still sees the extraction...
	assert "ArrayIndexLoad" in extraction
	# ...and the mislabeled marker is a reported violation.
	assert any("EXTRACTION but the" in v and "ArrayIndexLoad" in v for v in violations), violations
