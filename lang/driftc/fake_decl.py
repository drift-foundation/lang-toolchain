from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage1.call_info import IntrinsicKind

@dataclass
class FakeDecl:
	name: str
	params: Sequence[Any]
	return_type: Any
	fn_id: FunctionId | None = None
	throws_events: tuple[str, ...] = ()
	loc: Any = None
	is_extern: bool = False
	is_intrinsic: bool = False
	intrinsic_kind: IntrinsicKind | None = None
	is_method: bool = False
	self_mode: str | None = None
	impl_target: Any = None
	method_name: str | None = None
	module: str | None = None
