from __future__ import annotations

from pathlib import Path
import re


def test_no_blank_span_fallbacks_in_hardened_checker_modules() -> None:
	root = Path(__file__).resolve().parents[3]
	targets = [
		root / "lang" / "driftc" / "type_checker.py",
		root / "lang" / "driftc" / "checker" / "call_resolver.py",
	]
	for path in targets:
		text = path.read_text(encoding="utf-8")
		assert "span=Span()" not in text, str(path)


def test_no_blank_span_fallbacks_in_driftc_boundary_diagnostics() -> None:
	root = Path(__file__).resolve().parents[3]
	path = root / "lang" / "driftc" / "driftc.py"
	text = path.read_text(encoding="utf-8")
	patterns = [
		r"message=f\"internal: MIR lowering contract failure \(\{err\}\)\",\n\s+severity=\"error\",\n\s+span=Span\(\),",
		r"message=f\"internal: MIR validation contract failure \(\{err\}\)\",\n\s+severity=\"error\",\n\s+span=Span\(\),",
		r"message=f\"internal: LLVM lowering contract failure \(\{err\}\)\",\n\s+severity=\"error\",\n\s+span=Span\(\),",
	]
	for pattern in patterns:
		assert re.search(pattern, text) is None, str(path)


def test_boundary_contract_diagnostics_use_central_helper_in_driftc() -> None:
	root = Path(__file__).resolve().parents[3]
	path = root / "lang" / "driftc" / "driftc.py"
	text = path.read_text(encoding="utf-8")
	forbidden = [
		r"Diagnostic\(\s*message=f\"internal: MIR lowering contract failure",
		r"Diagnostic\(\s*message=f\"internal: MIR validation contract failure",
		r"Diagnostic\(\s*message=f\"internal: LLVM lowering contract failure",
	]
	for pattern in forbidden:
		assert re.search(pattern, text, flags=re.DOTALL) is None, str(path)
	assert text.count("_append_boundary_contract_diag(") >= 6, str(path)
