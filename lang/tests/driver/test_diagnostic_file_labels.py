# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""CLI diagnostics must name the REAL source file, not `<source>`.

`driftc.py::_source_label()` was hardcoded to return `"<source>"` and every
text-output path printed diagnostics through it — so even compiles of real
files rendered `<source>:19:67: error: ...` while the span (and the JSON
`file` field) carried the real path all along. Text output now prefers
`diag.span.file` when present, falls back to the user-provided primary
source path, and only prints `<source>` when the compiler truly has no file
(synthetic/unit sources, direct harness calls that request normalized
labels — the parser's relabel maps keep their normalizing default for those
callers).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_TYPECHECK_ERROR_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
\tval s: String = 42;
\treturn 0;
}
"""

_PARSE_ERROR_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
\tval = ;
}
"""


def _compile(tmp_path: Path, source: str, *extra: str) -> tuple[subprocess.CompletedProcess[str], Path]:
	src = tmp_path / "real_file_name.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	return res, src


def test_text_typecheck_diagnostic_names_real_file(tmp_path: Path) -> None:
	"""A typecheck diagnostic with a real span must render the actual file
	path in text output — not the `<source>` placeholder."""
	res, src = _compile(tmp_path, _TYPECHECK_ERROR_SOURCE)
	assert res.returncode != 0
	err = res.stderr + res.stdout
	assert str(src) in err, f"expected real path {src} in diagnostics; got:\n{err[-1200:]}"
	assert "<source>:" not in err, f"placeholder label leaked into text output:\n{err[-1200:]}"


def test_json_typecheck_diagnostic_carries_real_file(tmp_path: Path) -> None:
	"""The JSON `file` field must be the real path for the same case."""
	res, src = _compile(tmp_path, _TYPECHECK_ERROR_SOURCE, "--json")
	assert res.returncode != 0
	payload = json.loads((res.stdout or res.stderr).strip().splitlines()[-1])
	diags = payload["diagnostics"]
	assert diags, payload
	files = {d.get("file") for d in diags}
	assert str(src) in files, f"expected real path in JSON file field; got files={files}"
	assert "<source>" not in files, f"placeholder leaked into JSON file field: {files}"


def test_text_parse_diagnostic_names_real_file(tmp_path: Path) -> None:
	"""Parse-phase diagnostics from a real CLI compile must also name the
	real file (the parser's normalizing relabel is for harness callers, not
	the CLI)."""
	res, src = _compile(tmp_path, _PARSE_ERROR_SOURCE)
	assert res.returncode != 0
	err = res.stderr + res.stdout
	assert str(src) in err, f"expected real path {src} in parse diagnostics; got:\n{err[-1200:]}"
	assert "<source>:" not in err, f"placeholder label leaked into parse output:\n{err[-1200:]}"
