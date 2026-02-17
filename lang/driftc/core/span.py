# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Lightweight source span representation used by diagnostics.

This is intentionally minimal: a Span can wrap whatever parser/location
object the front-end provides via the `raw` field while also carrying
optional file/line/column info when available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Span:
	"""Represents a source span (best-effort file/line/column plus raw parser loc)."""

	file: Optional[str] = None
	file_id: Optional[int] = None
	line: Optional[int] = None
	column: Optional[int] = None
	end_line: Optional[int] = None
	end_column: Optional[int] = None
	start_pos: Optional[int] = None
	end_pos: Optional[int] = None
	raw: Any = None

	@classmethod
	def from_loc(cls, loc: Any) -> "Span":
		"""
		Construct a Span from an existing parser/location object.

		If `loc` is already a Span, it is returned unchanged; otherwise the
		parser-specific object is stored in `raw` so downstream consumers
		can recover richer data when available.
		"""
		if loc is None:
			return cls()
		if isinstance(loc, cls):
			if loc.line is None and loc.column is None and loc.raw is not None:
				raw_span = cls.from_loc(loc.raw)
				return cls(
					file=loc.file or raw_span.file,
					file_id=loc.file_id if loc.file_id is not None else raw_span.file_id,
					line=loc.line if loc.line is not None else raw_span.line,
					column=loc.column if loc.column is not None else raw_span.column,
					end_line=loc.end_line if loc.end_line is not None else raw_span.end_line,
					end_column=loc.end_column if loc.end_column is not None else raw_span.end_column,
					start_pos=loc.start_pos if loc.start_pos is not None else raw_span.start_pos,
					end_pos=loc.end_pos if loc.end_pos is not None else raw_span.end_pos,
					raw=loc.raw,
				)
			return loc
		# Best-effort extraction of common location fields; keep the raw object
		# so richer renderers can still recover parser-specific details.
		return cls(
			file=getattr(loc, "file", None) or getattr(loc, "filename", None) or None,
			file_id=getattr(loc, "file_id", None),
			line=getattr(loc, "line", None) or getattr(loc, "start_line", None),
			column=getattr(loc, "column", None) or getattr(loc, "start_column", None),
			end_line=getattr(loc, "end_line", None),
			end_column=getattr(loc, "end_column", None),
			start_pos=getattr(loc, "start_pos", None),
			end_pos=getattr(loc, "end_pos", None),
			raw=loc,
		)


__all__ = ["Span"]
