from __future__ import annotations

from pathlib import Path


def test_no_blank_span_fallbacks_in_hardened_checker_modules() -> None:
	root = Path(__file__).resolve().parents[3]
	targets = [
		root / "lang" / "driftc" / "type_checker.py",
		root / "lang" / "driftc" / "checker" / "call_resolver.py",
	]
	for path in targets:
		text = path.read_text(encoding="utf-8")
		assert "span=Span()" not in text, str(path)
