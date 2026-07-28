# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Build a temp stdlib whose `std.json` carries the PRESERVED RECURSIVE
parser as a test oracle (`_oracle_parse_with_config` / `_oracle_parse_located`).

The oracle is retained ONLY for differential-parity and performance-baseline
testing of the iterative parser.  It is deliberately kept OUT of the
production stdlib — so it never ships and never contributes to the ownership
corpus — and appended here to a throwaway copy only for the tests that need
it.  Source fragment: `lang/tests/fixtures/json_recursive_oracle.drift.frag`.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from lang.driftc.parser import stdlib_root

_ROOT = Path(__file__).resolve().parents[3]
_FRAG = _ROOT / "lang" / "tests" / "fixtures" / "json_recursive_oracle.drift.frag"
_MARKER = "fn _encode_string(s: String) nothrow -> String {"


def build_oracle_stdlib(tmp_path: Path) -> Path:
	"""Copy the stdlib and append the recursive-parser oracle to std.json;
	return the temp stdlib root to pass as `--stdlib-root`."""
	real = stdlib_root() or (_ROOT / "stdlib")
	dest = tmp_path / "stdlib_oracle"
	if (dest / "std" / "json" / "json.drift").exists():
		return dest   # already built for this tmp_path (idempotent across compiles)
	shutil.copytree(real, dest, dirs_exist_ok=True)
	jp = dest / "std" / "json" / "json.drift"
	s = jp.read_text()
	if _MARKER not in s:
		raise AssertionError("std.json encode marker not found; cannot splice oracle")
	frag = _FRAG.read_text().rstrip("\n")
	s = s.replace(_MARKER, frag + "\n\n\n" + _MARKER, 1)
	jp.write_text(s)
	return dest
