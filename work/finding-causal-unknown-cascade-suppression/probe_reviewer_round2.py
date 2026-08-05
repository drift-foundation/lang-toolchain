"""Reviewer-only red probes for causal suppression traversal completeness."""
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _messages(tmp_path: Path, capsys, body: str) -> list[str]:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	main_path.parent.mkdir(parents=True, exist_ok=True)
	main_path.write_text(
		"module m_main;\n"
		"pub fn main() nothrow -> Int {\n"
		+ body
		+ "\n\treturn 0;\n}\n",
		encoding="utf-8",
	)
	driftc_main(["-M", str(root), str(main_path), "--json"])
	payload = json.loads(capsys.readouterr().out)
	return [d.get("message", "") for d in payload.get("diagnostics", [])]


def test_caused_hcall_still_checks_independent_argument(tmp_path: Path, capsys) -> None:
	msgs = _messages(
		tmp_path,
		capsys,
		"\tval bad = missing_receiver;\n\tbad(missing_argument);",
	)
	assert any("unknown name 'missing_receiver'" in m for m in msgs), msgs
	assert any("unknown name 'missing_argument'" in m for m in msgs), msgs


def test_caused_method_receiver_still_checks_independent_argument(tmp_path: Path, capsys) -> None:
	msgs = _messages(
		tmp_path,
		capsys,
		"\tval bad = missing_receiver;\n\tbad.no_such_method(missing_argument);",
	)
	assert any("unknown name 'missing_receiver'" in m for m in msgs), msgs
	assert any("unknown name 'missing_argument'" in m for m in msgs), msgs


def test_all_caused_match_arms_do_not_add_downstream_cascade(tmp_path: Path, capsys) -> None:
	msgs = _messages(
		tmp_path,
		capsys,
		(
			"\tval a = missing_a;\n"
			"\tval b = missing_b;\n"
			"\tval joined = match true { true => { a }, false => { b } };\n"
			"\tjoined();"
		),
	)
	assert sum("unknown name" in m for m in msgs) == 2, msgs
	assert len(msgs) == 2, msgs


def test_all_caused_try_results_do_not_add_downstream_cascade(tmp_path: Path, capsys) -> None:
	msgs = _messages(
		tmp_path,
		capsys,
		(
			"\tval attempted = missing_attempt;\n"
			"\tval recovered = missing_catch;\n"
			"\tval joined = try attempted catch { recovered };\n"
			"\tjoined();"
		),
	)
	assert sum("unknown name" in m for m in msgs) == 2, msgs
	assert len(msgs) == 2, msgs
