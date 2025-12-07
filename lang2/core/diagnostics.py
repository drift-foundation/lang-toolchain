"""
Common diagnostic structure for checker/driver passes.

This is deliberately minimal for now: a message plus optional span/metadata.
The real compiler can extend this with richer location/types as needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Diagnostic:
    """Represents a compiler diagnostic (error/warning/etc.)."""

    message: str
    severity: str = "error"
    span: Optional[Any] = None  # Placeholder until a real Span type exists.
    notes: list[str] = field(default_factory=list)

