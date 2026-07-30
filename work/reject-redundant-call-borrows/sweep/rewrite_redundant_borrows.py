#!/usr/bin/env python3
"""One-off migration sweeper for the reject-redundant-call-borrows rule.

Compiler-span-driven (never regex over source): repeatedly runs a driftc
command with --json, collects E_REDUNDANT_ARG_BORROW diagnostics whose file
matches --only, and deletes exactly the `&` / `&mut` token the diagnostic's
line:column points at (plus following whitespace). Iterates until a compile
produces no matching diagnostics or --max-iters is hit.

Usage:
  .venv/bin/python work/reject-redundant-call-borrows/sweep/rewrite_redundant_borrows.py \
      --only stdlib/ -- bin/driftc --dev --stdlib-root stdlib prog.drift --entry main::main -o /tmp/x  # drift-tmp-root-audit: allow usage-example prose in docstring

Refuses to edit a site whose column is not an `&` (prints SKIP; those need
manual attention). D1b sites (E_MUT_RVALUE_ARG_BINDING_REQUIRED) are never
touched — they need the binding rewrite, not token deletion.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

CODE = "E_REDUNDANT_ARG_BORROW"


def run_compile(cmd: list[str]) -> list[dict]:
	res = subprocess.run(cmd + ["--json"], capture_output=True, text=True)
	diags: list[dict] = []
	for stream in (res.stdout, res.stderr):
		for chunk in stream.splitlines():
			chunk = chunk.strip()
			if not chunk.startswith("{") and not chunk.startswith("["):
				continue
			try:
				data = json.loads(chunk)
			except json.JSONDecodeError:
				continue
			if isinstance(data, dict):
				diags.extend(data.get("diagnostics", []) or [])
			elif isinstance(data, list):
				diags.extend(d for d in data if isinstance(d, dict))
	if not diags:
		# whole-stream JSON document fallback
		for stream in (res.stdout, res.stderr):
			try:
				data = json.loads(stream)
			except json.JSONDecodeError:
				continue
			if isinstance(data, dict):
				diags.extend(data.get("diagnostics", []) or [])
	return diags


def apply_edits(edits_by_file: dict[str, list[tuple[int, int]]]) -> tuple[int, int]:
	applied = skipped = 0
	for path, sites in edits_by_file.items():
		p = Path(path)
		lines = p.read_text().splitlines(keepends=True)
		for line_no, col in sorted(set(sites), reverse=True):
			if line_no < 1 or line_no > len(lines):
				skipped += 1
				continue
			line = lines[line_no - 1]
			i = col - 1
			if i < 0 or i >= len(line) or line[i] != "&":
				print(f"SKIP {path}:{line_no}:{col}: expected '&', found {line[i:i+4]!r}")
				skipped += 1
				continue
			j = i + 1
			if line[j:j + 3] == "mut" and (j + 3 >= len(line) or not (line[j + 3].isalnum() or line[j + 3] == "_")):
				j += 3
			while j < len(line) and line[j] in " \t":
				j += 1
			lines[line_no - 1] = line[:i] + line[j:]
			applied += 1
		p.write_text("".join(lines))
	return applied, skipped


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--only", action="append", default=[], help="path prefix filter (repeatable)")
	ap.add_argument("--max-iters", type=int, default=50)
	ap.add_argument("cmd", nargs=argparse.REMAINDER)
	args = ap.parse_args()
	cmd = args.cmd
	if cmd and cmd[0] == "--":
		cmd = cmd[1:]
	if not cmd:
		ap.error("compile command required after --")
	root = Path.cwd()
	total = 0
	for it in range(1, args.max_iters + 1):
		diags = run_compile(cmd)
		edits: dict[str, list[tuple[int, int]]] = defaultdict(list)
		for d in diags:
			if d.get("code") != CODE:
				continue
			f = d.get("file")
			line, col = d.get("line"), d.get("column")
			if not f or not line or not col:
				continue
			rel = str(Path(f).resolve())
			if args.only and not any(str(Path(o).resolve()) in rel or rel.startswith(str(root / o)) for o in args.only):
				continue
			edits[f].append((int(line), int(col)))
		if not edits:
			print(f"iter {it}: clean (no {CODE} in scope); total edits {total}")
			return 0
		applied, skipped = apply_edits(edits)
		total += applied
		print(f"iter {it}: applied {applied}, skipped {skipped}, files {len(edits)}")
		if applied == 0:
			print("no progress; stopping")
			return 1
	print(f"max iters reached; total edits {total}")
	return 1


if __name__ == "__main__":
	sys.exit(main())
