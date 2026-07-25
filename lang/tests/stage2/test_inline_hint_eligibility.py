# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Unit teeth for the STRUCTURAL inlinehint predicate
(lang.codegen.llvm.llvm_codegen._inline_hint_eligible): SMALL hot path
AND an accessor shape (variant return OR cold-failure block) — with
EXACT threshold-boundary coverage.  Deliberately NOT a compiler-wide
"inline all small functions" policy: an ordinary small hot function
without the shape is ineligible."""
from __future__ import annotations

from lang.codegen.llvm.llvm_codegen import _inline_hint_eligible
from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.mir_nodes import function_symbol


class _TD:
	def __init__(self, kind):
		self.kind = kind


class _TT:
	def __init__(self, kind):
		self._kind = kind

	def get(self, _tid):
		return _TD(self._kind)


def _fn(hot_instrs: int, cold_block: bool) -> M.MirFunc:
	fn_id = FunctionId(module="t", name="probe", ordinal=0)
	blocks = {}
	per_block = 16
	made = 0
	i = 0
	while made < hot_instrs:
		n = min(per_block, hot_instrs - made)
		instrs = [M.ConstInt(dest=f"%c{made + j}", value=j) for j in range(n)]
		made += n
		name = f"b{i}"
		blocks[name] = M.BasicBlock(name=name, instructions=instrs,
			terminator=M.Return(value=None))
		i += 1
	if not blocks:
		blocks["b0"] = M.BasicBlock(name="b0", instructions=[], terminator=M.Return(value=None))
	if cold_block:
		blocks["cold"] = M.BasicBlock(name="cold",
			instructions=[M.ConstInt(dest="%x", value=0)],
			terminator=M.Unreachable())
	return M.MirFunc(name=function_symbol(fn_id), params=[], locals=[],
		fn_id=fn_id, blocks=blocks, entry="b0")


from lang.driftc.core.types_core import TypeKind  # noqa: E402


def test_boundary_48_with_cold_block_eligible() -> None:
	assert _inline_hint_eligible(_fn(48, cold_block=True), None, None) is True


def test_boundary_49_with_cold_block_ineligible() -> None:
	assert _inline_hint_eligible(_fn(49, cold_block=True), None, None) is False


def test_small_ordinary_hot_function_ineligible() -> None:
	"""Small but WITHOUT the shape: not hinted (tiny functions inline
	on LLVM's own cost model; no blanket small-function policy)."""
	assert _inline_hint_eligible(_fn(10, cold_block=False), None, None) is False
	assert _inline_hint_eligible(_fn(10, cold_block=False), _TT(TypeKind.SCALAR), object()) is False


def test_variant_return_small_eligible() -> None:
	"""Result/Optional-returning accessors: the error/None arm RETURNS
	(not Unreachable-cold), so the variant return type is the shape."""
	assert _inline_hint_eligible(_fn(20, cold_block=False), _TT(TypeKind.VARIANT), object()) is True


def test_variant_return_boundary_49_ineligible() -> None:
	assert _inline_hint_eligible(_fn(49, cold_block=False), _TT(TypeKind.VARIANT), object()) is False


def test_cold_failure_small_eligible() -> None:
	assert _inline_hint_eligible(_fn(20, cold_block=True), None, None) is True


def test_cold_blocks_are_discounted_from_hot_size() -> None:
	"""48 hot + an arbitrarily large cold arm stays eligible: cold
	instructions are discounted, hot ones are not."""
	fn = _fn(48, cold_block=True)
	fn.blocks["cold"].instructions = [M.ConstInt(dest=f"%z{j}", value=j) for j in range(200)]
	assert _inline_hint_eligible(fn, None, None) is True
