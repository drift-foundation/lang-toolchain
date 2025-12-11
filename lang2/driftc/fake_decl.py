from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass
class FakeDecl:
	name: str
	params: Sequence[Any]
	return_type: Any
	throws_events: tuple[str, ...] = ()
	loc: Any = None
	is_extern: bool = False
	is_intrinsic: bool = False
	is_method: bool = False
	self_mode: str | None = None
	impl_target: Any = None
	method_name: str | None = None
	module: str | None = None
