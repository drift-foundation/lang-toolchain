# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""End-to-end regression for robustness matrix row #2: nested if/else.

The row #2 fix touched **six** sequential mutually-recursive `walk`/`walk_value`
walker pairs across stage1, the type checker, and the driftc top-level driver.
The synthetic stage1 unit tests in `lang/tests/stage1/test_node_ids_deep_recursion.py`
only cover three of the six (the `node_ids.py` walkers). The other three live in
`type_checker.py` and `driftc.py` and are only reachable via the full compiler
pipeline.

This file pins the boundary through the full driver path:

- d=256 nested if/else: compiles cleanly (all 6 walkers must traverse without
  Python `RecursionError`)
- d=257 nested if/else: rejected with the row #1 parser block-nesting
  diagnostic (`block nesting depth exceeds 256`)

If any of the iterative walker conversions in row #2 silently regresses to the
recursive form, the d=256 compile will start failing with a `RecursionError`
traceback in stderr. If the row #1 boundary moves, the d=257 case will report
the wrong threshold.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _gen_nested_if(n: int) -> str:
	body = "return 0;"
	for _ in range(n):
		body = "if true {\n" + body + "\n} else { return 1; }"
	return f"module main;\npub fn main() nothrow -> Int {{\n{body}\n}}\n"


def _compile(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)


def test_nested_if_at_published_limit_compiles_cleanly(tmp_path: Path) -> None:
	"""256 nested if/else through the full pipeline must compile cleanly.

	This exercises the row #2 iterative walker conversions at every site:
	- the three `node_ids.py` walkers (via the shared `_iter_hir_walk` helper)
	- the local iterative walker in `type_checker.py::_collect_callsite_ids`
	- the two local iterative walkers in `driftc.py` (`_collect_call_nodes_by_id`
	  and the HCast scanner)

	A regression to recursive form in any of these surfaces here as a Python
	`RecursionError` traceback in stderr.
	"""
	res = _compile(tmp_path, _gen_nested_if(256))
	assert res.returncode == 0, (
		f"d=256 nested if/else should compile cleanly, got rc={res.returncode}\n"
		f"stderr (last 800 chars): {res.stderr[-800:]}"
	)
	assert "Traceback" not in res.stderr, (
		f"unexpected Python traceback in stderr — a recursive walker has "
		f"regressed somewhere in the row #2 fix sites:\n{res.stderr[-800:]}"
	)
	assert "RecursionError" not in res.stderr


def test_nested_if_one_above_limit_emits_clean_diagnostic(tmp_path: Path) -> None:
	"""257 nested if/else must hit the row #1 parser block-nesting limit.

	Pins both the row #1 boundary (still at 256) and the row #2 iterative
	walker chain (no Python traceback).
	"""
	res = _compile(tmp_path, _gen_nested_if(257))
	assert res.returncode != 0, "d=257 should be rejected"
	assert "Traceback" not in res.stderr, (
		f"unexpected Python traceback in stderr at d=257:\n{res.stderr[-800:]}"
	)
	assert "RecursionError" not in res.stderr
	assert "block nesting depth exceeds 256" in res.stderr, (
		f"expected row #1 nesting-limit diagnostic, got:\n{res.stderr[-800:]}"
	)
