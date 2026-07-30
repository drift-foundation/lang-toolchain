#!/usr/bin/env python3
"""Extraction-aware sweeper for Drift sources embedded in Python test files.

For each string literal in the target .py files that looks like a Drift
program, compile it with driftc --json, collect E_REDUNDANT_ARG_BORROW
diagnostics, and delete exactly the `&`/`&mut ` tokens — mapping the
diagnostic's (line, column) in the *decoded value* back to byte offsets in
the *source literal* through escape sequences, so `\t`-escaped and
triple-quoted styles are both edited precisely. Never regex over source.

Usage (repo root):
  .venv/bin/python work/reject-redundant-call-borrows/sweep/embedded_sweep.py FILE.py [FILE.py ...]
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

CODE = "E_REDUNDANT_ARG_BORROW"
ROOT = Path.cwd()

SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "0": "\0", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def literal_value_map(src: str, node: ast.Constant) -> tuple[str, list[int]] | None:
	"""Return (value, map value_index -> absolute source index) for a plain
	string literal. None for f-strings/concats/unsupported escapes."""
	seg = ast.get_source_segment(src, node)
	if seg is None:
		return None
	# absolute offset of the segment
	lines = src.splitlines(keepends=True)
	abs_off = sum(len(l) for l in lines[: node.lineno - 1]) + node.col_offset
	i = 0
	prefix = ""
	while i < len(seg) and seg[i] not in "\"'":
		prefix += seg[i].lower()
		i += 1
	if i >= len(seg) or "f" in prefix or "b" in prefix:
		return None
	is_raw = "r" in prefix
	if seg[i : i + 3] in ('"""', "'''"):
		quote = seg[i : i + 3]
	else:
		quote = seg[i]
	body_start = i + len(quote)
	body_end = len(seg) - len(quote)
	value_chars: list[str] = []
	idx_map: list[int] = []
	j = body_start
	while j < body_end:
		ch = seg[j]
		if ch == "\\" and not is_raw and j + 1 < body_end:
			nxt = seg[j + 1]
			if nxt == "\n":
				j += 2
				continue
			if nxt in SIMPLE_ESCAPES:
				value_chars.append(SIMPLE_ESCAPES[nxt])
				idx_map.append(abs_off + j)
				j += 2
				continue
			if nxt in "xuU" or nxt.isdigit():
				return None  # numeric escapes: bail rather than mis-map
			value_chars.append(ch)
			idx_map.append(abs_off + j)
			j += 1
			continue
		value_chars.append(ch)
		idx_map.append(abs_off + j)
		j += 1
	return "".join(value_chars), idx_map


def _collect_module_consts(src: str, tree: ast.Module) -> dict[str, tuple[str, list[int]]]:
	"""Module-level `NAME = "literal"` string assignments, with maps."""
	out: dict[str, tuple[str, list[int]]] = {}
	for stmt in tree.body:
		if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
			if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
				m = literal_value_map(src, stmt.value)
				if m is not None:
					out[stmt.targets[0].id] = m
	return out


def _chain_parts(node: ast.expr) -> list[ast.expr] | None:
	"""Flatten a `a + b + c` chain of Constants/Names; None if unsupported."""
	if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
		left = _chain_parts(node.left)
		right = _chain_parts(node.right)
		if left is None or right is None:
			return None
		return left + right
	if isinstance(node, (ast.Constant, ast.Name)):
		return [node]
	return None


def drift_candidates(src: str) -> list[tuple[str, list[int]]]:
	tree = ast.parse(src)
	consts = _collect_module_consts(src, tree)
	out = []
	seen_spans: set[tuple[int, int]] = set()

	def _resolve(part: ast.expr) -> tuple[str, list[int]] | None:
		if isinstance(part, ast.Constant) and isinstance(part.value, str):
			return literal_value_map(src, part)
		if isinstance(part, ast.Name):
			return consts.get(part.id)
		return None

	for node in ast.walk(tree):
		parts = None
		if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
			parts = _chain_parts(node)
		elif isinstance(node, ast.Constant) and isinstance(node.value, str):
			parts = [node]
		if not parts:
			continue
		resolved = [_resolve(p) for p in parts]
		if any(r is None for r in resolved):
			continue
		value = "".join(r[0] for r in resolved)
		if not (("fn " in value or "module main" in value) and "&" in value and len(value) > 40):
			continue
		idx_map: list[int] = []
		for r in resolved:
			idx_map.extend(r[1])
		key = (idx_map[0] if idx_map else -1, idx_map[-1] if idx_map else -1)
		if key in seen_spans:
			continue
		seen_spans.add(key)
		out.append((value, idx_map))
	return out


def compile_value(value: str, tmpdir: str, tag: int) -> list[dict]:
	prog = value if "module " in value else "module main;\n" + value
	src_path = Path(tmpdir) / f"emb{tag}.drift"
	src_path.write_text(prog)
	res = subprocess.run(
		["bin/driftc", "--dev", "--stdlib-root", "stdlib", str(src_path),
		 "--entry", "main::main", "-o", str(Path(tmpdir) / f"emb{tag}.bin"),
		 "--json", "--allow-unsafe"],
		capture_output=True, text=True, cwd=ROOT,
	)
	diags: list[dict] = []
	for stream in (res.stdout, res.stderr):
		for chunk in stream.splitlines():
			chunk = chunk.strip()
			if not (chunk.startswith("{") or chunk.startswith("[")):
				continue
			try:
				data = json.loads(chunk)
			except json.JSONDecodeError:
				continue
			if isinstance(data, dict):
				diags.extend(data.get("diagnostics", []) or [])
	wrapped = prog is not value
	out = []
	for d in diags:
		if d.get("code") != CODE:
			continue
		if str(d.get("file", "")) != str(src_path):
			continue  # stdlib site — handled by the stdlib/corpus sweepers
		line = int(d.get("line") or 0)
		col = int(d.get("column") or 0)
		if wrapped:
			line -= 1  # drop the injected module line
		out.append((line, col))
	return out


def sweep_file(py_path: Path) -> int:
	total = 0
	for _iteration in range(10):
		src = py_path.read_text()
		edits: list[tuple[int, int]] = []  # absolute (start, end) deletions
		with tempfile.TemporaryDirectory(prefix="emb-sweep-") as tmpdir:  # drift-tmp-root-audit: allow one-shot manual sweep tool, not part of any test lane
			for tag, (value, idx_map) in enumerate(drift_candidates(src)):
				sites = compile_value(value, tmpdir, tag)
				if not sites:
					continue
				vlines = value.splitlines(keepends=True)
				for line, col in sites:
					if line < 1 or line > len(vlines):
						continue
					voff = sum(len(l) for l in vlines[: line - 1]) + (col - 1)
					if voff >= len(value) or value[voff] != "&":
						print(f"SKIP {py_path}:{line}:{col}: not '&' in value")
						continue
					vend = voff + 1
					if value[vend : vend + 3] == "mut" and (vend + 3 >= len(value) or not (value[vend + 3].isalnum() or value[vend + 3] == "_")):
						vend += 3
					while vend < len(value) and value[vend] in " \t":
						vend += 1
					start = idx_map[voff]
					end = idx_map[vend] if vend < len(idx_map) else idx_map[-1] + 1
					edits.append((start, end))
		if not edits:
			return total
		buf = src
		for start, end in sorted(set(edits), reverse=True):
			buf = buf[:start] + buf[end:]
		py_path.write_text(buf)
		total += len(set(edits))
		print(f"{py_path}: applied {len(set(edits))} (iter {_iteration + 1})")
	return total


def main() -> int:
	files = [Path(a) for a in sys.argv[1:]]
	if not files:
		print("usage: embedded_sweep.py FILE.py [...]", file=sys.stderr)
		return 2
	grand = 0
	for f in files:
		grand += sweep_file(f)
	print(f"total embedded edits: {grand}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
