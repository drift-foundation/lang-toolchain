from __future__ import annotations

from pathlib import Path
import re


def test_no_new_call_info_nodeid_usage() -> None:
	root = Path(__file__).resolve().parents[4]
	tests_dir = root / "lang" / "tests"
	offenders: list[str] = []
	needle = "call_info_by_" + "node_id"
	pat_nodeid_dict = re.compile(r"\." + "node" + r"_id\s*:\s*CallInfo\s*\(")
	pat_nodeid_assign = re.compile(r"\." + "node" + r"_id\s*\]\s*=\s*CallInfo\s*\(")
	for path in tests_dir.rglob("*.py"):
		rel = path.relative_to(tests_dir).as_posix()
		text = path.read_text(encoding="utf-8")
		if needle not in text and "CallInfo(" not in text and ".node" not in text:
			continue
		for m in pat_nodeid_dict.finditer(text):
			offenders.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
		for m in pat_nodeid_assign.finditer(text):
			offenders.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
		if needle in text:
			for idx, line in enumerate(text.splitlines(), start=1):
				if needle in line:
					offenders.append(f"{rel}:{idx}")
	if offenders:
		raise AssertionError(f"unexpected {needle} usage:\n" + "\n".join(offenders))
