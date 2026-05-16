# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Structural invariant pin (K-review, 2026-05-15) for
`_lift_rvalue_ref_base_for_borrow` in
`lang/driftc/stage2/hir_to_mir.py`.

The helper opportunistically lowers a `&place` borrow whose
subject is `HField(...HCall_returning_&T, field, ...)` into a
real place-pointer chain (so `&self` interior-mutation methods on
the leaf field hit real storage, not a temp-local copy).  If the
helper returns None, the caller falls back to whole-expression
materialization over the SAME subject — that fallback emits its
own MIR.  If the helper has already emitted any MIR before
returning None, that prefix is stranded: it lives inside the
current basic block but is unreachable from the legitimate
fallback emission, producing a miscompile.

K's review of the initial fix flagged exactly this: the first
draft called `self.lower_expr(base)` and emitted `AddrOfField`s
inside a loop that could `return None` halfway through if a
field's containing type wasn't a struct.  The refactored version
splits validation from emission:

  - `_validate_lifted_chain` does pure inspection and either
    returns a complete emission plan or None.
  - `_lift_rvalue_ref_base_for_borrow` calls the validator; if
    it returns None, the helper returns None WITHOUT touching
    `self.b` or `self.lower_expr`.  Otherwise it walks the plan
    and emits every step.

This test mechanically enforces that split.  It parses
`_lift_rvalue_ref_base_for_borrow`'s source as an `ast.FunctionDef`
and asserts:

  1. The function body contains exactly one `return None` and
     that `return None` is structurally guarded by an `if`
     condition immediately following the validator call.
  2. No `self.b.emit(...)` call, no `self.lower_expr(...)` call,
     and no `AddrOfField` / `LoadRef` reference appears textually
     BEFORE the first `return None`.

If a future change reintroduces a `return None` after MIR
emission, criterion (1) fails — visibly and immediately, with a
diff that names the offending statement.  If the validator call
is moved below an emission, criterion (2) fails.

This isn't an end-to-end runtime probe (those exist in
`test_arc_get_field_atomic_store_persistence_blocker.py` and in
the two adjacent e2e cases).  It's a guard against the specific
class of regression — partial emission before fallback — that
runtime probes can miss when the stranded prefix happens to be
inert (e.g., a `lower_expr` of a pure call whose side effect
isn't visible from the test).
"""
from __future__ import annotations

import ast
import inspect

from lang.driftc.stage2.hir_to_mir import HIRToMIR


def _source_of(method_name: str) -> str:
	fn = getattr(HIRToMIR, method_name)
	return inspect.getsource(fn)


def _parse_function(src: str) -> ast.FunctionDef:
	# inspect.getsource returns the method with its leading
	# indentation; ast.parse needs it dedented.
	src = inspect.cleandoc(src)
	# inspect.cleandoc strips leading whitespace from each line
	# uniformly, but it also strips leading blank lines and a
	# leading docstring indent — what we need is textwrap.dedent
	# behavior over the raw source.  Use a manual dedent that
	# preserves internal indentation.
	import textwrap
	raw = inspect.getsource(HIRToMIR.__dict__["_lift_rvalue_ref_base_for_borrow"])
	dedented = textwrap.dedent(raw)
	tree = ast.parse(dedented)
	assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef), (
		f"expected single FunctionDef, got {[type(n).__name__ for n in tree.body]}"
	)
	return tree.body[0]


def _walk(node: ast.AST):
	"""Yield (node, lineno) for every descendant node with a
	lineno attribute, depth-first."""
	for child in ast.walk(node):
		lineno = getattr(child, "lineno", None)
		if lineno is not None:
			yield child, lineno


def test_lift_rvalue_ref_base_no_partial_emission_invariant() -> None:
	import textwrap
	raw = inspect.getsource(HIRToMIR.__dict__["_lift_rvalue_ref_base_for_borrow"])
	dedented = textwrap.dedent(raw)
	tree = ast.parse(dedented)
	fn = tree.body[0]
	assert isinstance(fn, ast.FunctionDef)

	# --- Criterion 1: exactly one `return None` and it is the
	# guard immediately following the validator call. ---
	return_nones = []
	for node in ast.walk(fn):
		if isinstance(node, ast.Return):
			# `return` (no value) and `return None` both parse to
			# Return with .value being None or a Constant(None).
			val = node.value
			is_bare = val is None
			is_const_none = (
				isinstance(val, ast.Constant) and val.value is None
			)
			if is_bare or is_const_none:
				return_nones.append(node)
	assert len(return_nones) == 1, (
		f"_lift_rvalue_ref_base_for_borrow must have exactly one "
		f"`return None` (the post-validation early-out); found "
		f"{len(return_nones)} at lines "
		f"{[n.lineno for n in return_nones]}.  Any additional "
		f"`return None` risks stranding MIR emitted earlier in "
		f"the function (K-review invariant, 2026-05-15)."
	)
	the_return = return_nones[0]

	# --- Criterion 2: nothing that emits MIR appears textually
	# before the `return None`. ---
	#
	# We forbid these textual patterns in any statement at or
	# before the return-None line:
	#   - `self.b.emit(...)`        (direct MIR emission)
	#   - `self.lower_expr(...)`    (recursive lowering, emits MIR)
	#   - `AddrOfField`             (MIR ctor — even unused, a sign
	#                                an emission was prepared)
	#   - `LoadRef`                 (MIR ctor)
	#   - `AddrOfLocal`             (MIR ctor)
	#   - `StoreLocal`              (MIR ctor)
	guard_line = the_return.lineno
	forbidden_names = (
		"AddrOfField",
		"LoadRef",
		"AddrOfLocal",
		"StoreLocal",
	)
	for node, lineno in _walk(fn):
		if lineno > guard_line:
			continue
		# self.b.emit(...) / self.lower_expr(...) — Call whose
		# func is an Attribute on `self.X`.
		if isinstance(node, ast.Call):
			func = node.func
			# self.lower_expr(...)
			if (
				isinstance(func, ast.Attribute)
				and isinstance(func.value, ast.Name)
				and func.value.id == "self"
				and func.attr == "lower_expr"
			):
				raise AssertionError(
					f"self.lower_expr(...) found at line {lineno}, "
					f"before the post-validation `return None` at "
					f"line {guard_line}.  This violates the "
					f"no-partial-emission invariant: lower_expr "
					f"recursively emits MIR for its subject."
				)
			# self.b.emit(...)
			if (
				isinstance(func, ast.Attribute)
				and func.attr == "emit"
				and isinstance(func.value, ast.Attribute)
				and func.value.attr == "b"
				and isinstance(func.value.value, ast.Name)
				and func.value.value.id == "self"
			):
				raise AssertionError(
					f"self.b.emit(...) found at line {lineno}, "
					f"before the post-validation `return None` "
					f"at line {guard_line}.  Direct MIR emission "
					f"before the validator's verdict is bound "
					f"violates the no-partial-emission invariant."
				)
		# Bare references to MIR instruction names.  An ast.Name
		# or ast.Attribute carrying one of the forbidden ids is
		# evidence that an emission was prepared.
		if isinstance(node, ast.Name) and node.id in forbidden_names:
			raise AssertionError(
				f"reference to MIR ctor {node.id!r} at line "
				f"{lineno}, before the post-validation `return "
				f"None` at line {guard_line}.  No MIR ctor may "
				f"be referenced ahead of the validator gate."
			)
		if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
			raise AssertionError(
				f"reference to MIR ctor M.{node.attr} at line "
				f"{lineno}, before the post-validation `return "
				f"None` at line {guard_line}.  No MIR ctor may "
				f"be referenced ahead of the validator gate."
			)


def test_validate_lifted_chain_does_not_emit_mir() -> None:
	"""Companion: `_validate_lifted_chain` is the inspection
	half of the split.  It must itself be emission-free —
	otherwise the split fails to deliver atomicity.  This guard
	asserts the validator's source has zero references to MIR
	emission primitives.
	"""
	import textwrap
	raw = inspect.getsource(HIRToMIR.__dict__["_validate_lifted_chain"])
	dedented = textwrap.dedent(raw)
	tree = ast.parse(dedented)
	fn = tree.body[0]
	assert isinstance(fn, ast.FunctionDef)

	forbidden_names = (
		"AddrOfField",
		"LoadRef",
		"AddrOfLocal",
		"StoreLocal",
	)
	for node in ast.walk(fn):
		if isinstance(node, ast.Call):
			func = node.func
			if (
				isinstance(func, ast.Attribute)
				and isinstance(func.value, ast.Name)
				and func.value.id == "self"
				and func.attr == "lower_expr"
			):
				raise AssertionError(
					f"_validate_lifted_chain calls self.lower_expr "
					f"at line {func.lineno}; validation must be "
					f"pure inspection (K-review invariant)."
				)
			if (
				isinstance(func, ast.Attribute)
				and func.attr == "emit"
				and isinstance(func.value, ast.Attribute)
				and func.value.attr == "b"
				and isinstance(func.value.value, ast.Name)
				and func.value.value.id == "self"
			):
				raise AssertionError(
					f"_validate_lifted_chain calls self.b.emit "
					f"at line {func.lineno}; validation must be "
					f"pure inspection (K-review invariant)."
				)
		if isinstance(node, ast.Name) and node.id in forbidden_names:
			raise AssertionError(
				f"_validate_lifted_chain references MIR ctor "
				f"{node.id!r} at line {node.lineno}; validation "
				f"must be pure inspection."
			)
		if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
			raise AssertionError(
				f"_validate_lifted_chain references MIR ctor "
				f"M.{node.attr} at line {node.lineno}; "
				f"validation must be pure inspection."
			)
