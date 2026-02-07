from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int
    column: int

    def is_kind(self, *kinds: str) -> bool:
        return self.kind in kinds

