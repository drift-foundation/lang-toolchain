from __future__ import annotations

from pathlib import Path
import re


def test_llvm_codegen_avoids_insertvalue_undef_seeds() -> None:
	root = Path(__file__).resolve().parents[3]
	path = root / "lang" / "codegen" / "llvm" / "llvm_codegen.py"
	text = path.read_text(encoding="utf-8")
	assert re.search(r"insertvalue\s+\{?[^\n]*\sundef,", text) is None, str(path)
