# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Static pin: `_fresh(hint)` names are `%{hint}{counter}` with NO
separator, so two hints where one equals the other followed by digits
can collide across different counter values — `_fresh("raw1")` at
counter 16 and `_fresh("raw")` at counter 116 both produce `%raw116`
("multiple definition of local value" clang failure; hit live in
test_channel_close_race_conservation on 2026-07-10 when the callback
env flag fields shifted temp counts). This scan keeps the hint
namespace prefix-unambiguous so the class cannot recur.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CODEGEN = ROOT / "lang" / "codegen" / "llvm" / "llvm_codegen.py"


def test_fresh_hints_are_prefix_unambiguous() -> None:
	src = CODEGEN.read_text()
	hints = set(re.findall(r'_fresh\(\s*"([a-zA-Z0-9_]+)"\s*\)', src))
	assert hints, "no _fresh hints found — scan regex broken?"
	ambiguous = sorted(
		(shorter, longer)
		for longer in hints
		for shorter in hints
		if longer != shorter
		and longer.startswith(shorter)
		and longer[len(shorter):].isdigit()
	)
	assert not ambiguous, (
		"prefix-ambiguous _fresh hints (shorter+counter can collide with "
		f"longer+counter): {ambiguous} — rename the digit-suffixed hint "
		"to end in a letter (e.g. raw0 -> raw_a)"
	)
