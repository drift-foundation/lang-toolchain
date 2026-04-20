# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: DILocation.column must be clamped to 16-bit unsigned max.

LLVM stores `DILocation.column` as a 16-bit unsigned integer (max 65535).
Pathological single-line inputs (e.g. machine-generated long expression
chains, robustness probes like `gen_else_if_chain` at d≥2000) can produce
column counts that exceed this. Without clamping, the LLVM IR text emission
in `lang/codegen/llvm/llvm_codegen.py::LLVMCodegen.get_di_location` produced
a `column: <overflow>` value that the LLVM IR parser rejects with:

    error: value for 'column' too large, limit is 65535

The fix clamps `column` to 65535 before both the cache key and the IR text
emission, so the resulting debug info is lossy at the line's tail end but
the compile succeeds.

Surfaced by a robustness-matrix triage walk; filed as
`issues/llvm-debuginfo-column-overflow/`.
"""
from __future__ import annotations

from lang.driftc.core.span import Span


def test_di_location_clamps_column_at_16_bit_unsigned_max() -> None:
	"""Direct unit test of the clamp logic in `get_di_location`.

	We exercise the codegen module's `get_di_location` helper with a span
	whose column exceeds 65535 and verify:
	  1. it does not raise
	  2. the emitted IR metadata uses 65535 (not the original column)
	"""
	from lang.codegen.llvm.llvm_codegen import LlvmModuleBuilder

	cg = LlvmModuleBuilder(word_bits=64)
	# Force debug emission on for this test.
	cg.debug_enabled = True
	# Seed a synthetic scope id; the helper requires a non-None scope.
	scope_id = cg._dbg_new_id()

	# Column WAY above the 16-bit limit.
	span_overflow = Span(file="<test>", line=4, column=70_000)
	loc_id = cg.get_di_location(span_overflow, scope_id)
	assert loc_id is not None, "expected a location id"

	# Find the metadata line we just appended.
	matching = [
		line for line in cg._dbg_metadata
		if line.startswith(f"!{loc_id} = !DILocation")
	]
	assert len(matching) == 1, f"expected exactly one DILocation entry, got {matching}"
	emitted = matching[0]
	# The clamped column must appear; the original 70000 must not.
	assert "column: 65535" in emitted, f"expected clamped column, got: {emitted}"
	assert "70000" not in emitted, f"original overflow column leaked: {emitted}"


def test_di_location_passes_normal_columns_through_unchanged() -> None:
	"""Sanity: columns at or below the 16-bit limit are unchanged."""
	from lang.codegen.llvm.llvm_codegen import LlvmModuleBuilder

	cg = LlvmModuleBuilder(word_bits=64)
	cg.debug_enabled = True
	scope_id = cg._dbg_new_id()

	# Normal column.
	span_ok = Span(file="<test>", line=10, column=42)
	loc_id_ok = cg.get_di_location(span_ok, scope_id)
	assert loc_id_ok is not None
	emitted_ok = next(
		line for line in cg._dbg_metadata
		if line.startswith(f"!{loc_id_ok} = !DILocation")
	)
	assert "column: 42" in emitted_ok

	# Exactly at the limit.
	span_max = Span(file="<test>", line=10, column=65535)
	loc_id_max = cg.get_di_location(span_max, scope_id)
	assert loc_id_max is not None
	emitted_max = next(
		line for line in cg._dbg_metadata
		if line.startswith(f"!{loc_id_max} = !DILocation")
	)
	assert "column: 65535" in emitted_max


def test_di_location_clamp_dedups_overflow_distinct_columns() -> None:
	"""Two spans on the same line with different overflow columns must
	share a DILocation entry after clamping (both map to 65535).

	This pins the cache-key behavior: the clamp happens BEFORE the cache
	key is computed, so dedup works on the clamped value.
	"""
	from lang.codegen.llvm.llvm_codegen import LlvmModuleBuilder

	cg = LlvmModuleBuilder(word_bits=64)
	cg.debug_enabled = True
	scope_id = cg._dbg_new_id()

	span_a = Span(file="<test>", line=4, column=70_000)
	span_b = Span(file="<test>", line=4, column=80_000)
	loc_a = cg.get_di_location(span_a, scope_id)
	loc_b = cg.get_di_location(span_b, scope_id)
	assert loc_a == loc_b, (
		"two overflow columns on the same line should share a DILocation "
		"entry after clamping (cache key uses the clamped column)"
	)
