"""
Common diagnostic structure for checker/driver passes.

This is deliberately minimal for now: a message plus optional span/metadata.
The real compiler can extend this with richer location/types as needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re

from .span import Span


_CODE_PREFIX_RE = re.compile(r"^(?P<code>[A-Z][A-Z0-9_-]*)(?:\\b|:)")
_QUOTED_RE = re.compile(r"([\"']).*?\\1")
_NUMBER_RE = re.compile(r"\\b\\d+(?:\\.\\d+)?\\b")
_HEX_RE = re.compile(r"\\b0x[0-9a-fA-F]+\\b")


def _normalize_diag_message(message: str) -> str:
	msg = _QUOTED_RE.sub("<str>", message)
	msg = _HEX_RE.sub("<hex>", msg)
	msg = _NUMBER_RE.sub("<num>", msg)
	return msg


def _auto_diag_code(message: str, severity: str | None) -> str:
	# Use explicit E-XYZ prefix if present.
	prefix = _CODE_PREFIX_RE.match(message or "")
	if prefix is not None:
		return prefix.group("code")
	sev = (severity or "error").lower()
	sev_tag = "W" if sev.startswith("warn") else "E" if sev.startswith("err") else "I"
	norm = _normalize_diag_message(message or "")
	digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
	return f"{sev_tag}-AUTO-{digest}"


@dataclass
class Diagnostic:
	"""Represents a compiler diagnostic (error/warning/etc.)."""

	message: str
	code: str | None = None
	# Optional diagnostic phase label.
	#
	# Most compiler code paths emit diagnostics into a phase-specific sink (parser,
	# typecheck, borrowcheck, codegen). Some early semantic validations happen
	# while still building HIR; in those cases, attaching an explicit phase makes
	# JSON output and test expectations unambiguous.
	phase: str | None = None
	severity: str = "error"
	span: Span = field(default_factory=Span)  # Source location (Span() denotes unknown).
	notes: list[str] = field(default_factory=list)

	def __post_init__(self) -> None:
		# Normalize missing spans to the sentinel Span() so downstream tooling
		# can rely on a structured object instead of None.
		if self.span is None:  # type: ignore[unreachable]
			self.span = Span()
		if self.code is None:
			self.code = _auto_diag_code(self.message, self.severity)
