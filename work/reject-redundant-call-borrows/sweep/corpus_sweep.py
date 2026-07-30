#!/usr/bin/env python3
"""Parallel corpus sweeper for the reject-redundant-call-borrows migration.

Drives compiles across every single-file e2e fixture (16-way), collects
E_REDUNDANT_ARG_BORROW diagnostics (span-driven, never regex), applies the
one-token deletions, and iterates to a fixpoint. Sweeps BOTH the fixture
files and any stdlib sites surfaced by that fixture's instantiation set
(generic bodies are policy-checked only under instantiation, so stdlib's
fixpoint is relative to the corpus, not one probe).

Exclusions (D5 dispositions that are NOT plain token deletions, applied
manually afterwards): A7 reborrow_mut_to_shared_call_site (ref-value
repurpose), A12-A15 method_overload_param_type_* (arm deletions), om_*
(regenerated via __ownership_matrix__/_gen.py), issues/ (D3 archival).

Usage (repo root):
  .venv/bin/python work/reject-redundant-call-borrows/sweep/corpus_sweep.py [--jobs 16]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CODE = "E_REDUNDANT_ARG_BORROW"
ROOT = Path.cwd()
E2E = ROOT / "lang" / "tests" / "codegen" / "e2e"

MANUAL_DISPOSITION_DIRS = {
	"reborrow_mut_to_shared_call_site",
	"method_overload_param_type_two_way",
	"method_overload_param_type_three_way",
	"method_overload_param_type_cross_module",
	"method_overload_param_type_concrete_beats_generic",
}


def fixture_dirs() -> list[Path]:
	out = []
	for d in sorted(E2E.iterdir()):
		if not d.is_dir() or d.name.startswith("__") or d.name.startswith("om_"):
			continue
		if d.name in MANUAL_DISPOSITION_DIRS:
			continue
		main = d / "main.drift"
		if not main.exists():
			continue
		exp = d / "expected.json"
		if exp.exists():
			try:
				meta = json.loads(exp.read_text())
			except json.JSONDecodeError:
				meta = {}
			if meta.get("module_paths") or meta.get("c_sources"):
				continue  # multi-unit shapes need runner flags; handled separately
		out.append(d)
	return out


def compile_fixture(d: Path, tmpdir: str) -> list[dict]:
	out_bin = Path(tmpdir) / (d.name + ".bin")
	res = subprocess.run(
		["bin/driftc", "--dev", "--stdlib-root", "stdlib", str(d / "main.drift"),
		 "--entry", "main::main", "-o", str(out_bin), "--json", "--allow-unsafe"],
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
			elif isinstance(data, list):
				diags.extend(x for x in data if isinstance(x, dict))
	return diags


def in_sweep_scope(path: str) -> bool:
	rel = str(Path(path).resolve())
	if "/issues/" in rel:
		return False
	if "/__ownership_matrix__/" in rel or "/om_" in rel.replace(str(E2E), ""):
		return False
	for d in MANUAL_DISPOSITION_DIRS:
		if f"/{d}/" in rel:
			return False
	return ("/stdlib/" in rel) or ("/lang/tests/" in rel) or ("/examples/" in rel) or ("/tools/" in rel)


def apply_edits(sites: set[tuple[str, int, int]]) -> tuple[int, int]:
	by_file: dict[str, list[tuple[int, int]]] = defaultdict(list)
	for f, line, col in sites:
		by_file[f].append((line, col))
	applied = skipped = 0
	for path, entries in by_file.items():
		p = Path(path)
		try:
			lines = p.read_text().splitlines(keepends=True)
		except OSError:
			skipped += len(entries)
			continue
		for line_no, col in sorted(set(entries), reverse=True):
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
	ap.add_argument("--jobs", type=int, default=16)
	ap.add_argument("--max-iters", type=int, default=12)
	args = ap.parse_args()
	dirs = fixture_dirs()
	print(f"corpus: {len(dirs)} single-file fixtures")
	for it in range(1, args.max_iters + 1):
		sites: set[tuple[str, int, int]] = set()
		with tempfile.TemporaryDirectory(prefix="corpus-sweep-") as tmpdir:  # drift-tmp-root-audit: allow one-shot manual sweep tool, not part of any test lane
			with ThreadPoolExecutor(max_workers=args.jobs) as pool:
				for diags in pool.map(lambda d: compile_fixture(d, tmpdir), dirs):
					for dg in diags:
						if dg.get("code") != CODE:
							continue
						f, line, col = dg.get("file"), dg.get("line"), dg.get("column")
						if f and line and col and in_sweep_scope(f):
							sites.add((str(Path(f).resolve()), int(line), int(col)))
		if not sites:
			print(f"iter {it}: clean")
			return 0
		applied, skipped = apply_edits(sites)
		print(f"iter {it}: sites={len(sites)} applied={applied} skipped={skipped}")
		if applied == 0:
			print("no progress; stopping")
			return 1
	print("max iters reached")
	return 1


if __name__ == "__main__":
	sys.exit(main())
