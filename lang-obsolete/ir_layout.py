from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .types import Type


@dataclass(frozen=True)
class StructLayout:
    name: str
    field_names: List[str]
    field_types: List[Type]

    @property
    def index_by_name(self) -> dict[str, int]:
        return {n: i for i, n in enumerate(self.field_names)}
