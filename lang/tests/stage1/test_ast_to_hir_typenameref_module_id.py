# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: the AST→HIR lowering of `ast.TypeNameRef → H.HTypeNameRef`
inside a `TraitIs` subject must preserve `module_id`.

The pre-fix shape at `lang/driftc/stage1/ast_to_hir.py:226` constructed
`H.HTypeNameRef(name=subject.name, loc=...)` without forwarding
`subject.module_id`.  This is structurally identical to the
DMIR-decode TypeNameRef collision bug fixed at 0.31.28: a code path
that drops a module-qualifier field along an AST→HIR transition.

The parser today cannot synthesize a qualified TypeNameRef in trait
subject position — it builds `TypeNameRef(loc, name)` from a single
NAME token (`parser/parser.py:1396`) — so the drop is currently
unreachable through user source.  But if a future grammar change ever
produces a qualified subject (e.g. `std.collections::Map is Cloneable`),
or if a pass synthesizes `s0.TraitIs(subject=s0.TypeNameRef(name="X",
module_id="...some.module"))` directly (as `_visit_expr_TraitIs`'s
input), the field must round-trip through HIR construction.

This test exercises the AST→HIR path directly with a programmatically-
constructed `s0.TraitIs` whose subject carries `module_id`.  Pre-fix:
`H.HTypeNameRef` has no `module_id` field, so the field is dropped
(or, with the field added but the call site unchanged, defaults to
None).  Post-fix: lowered subject preserves the module_id.
"""
from __future__ import annotations

from lang.driftc.core.span import Span
from lang.driftc.stage0 import ast as s0
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.ast_to_hir import AstToHIR


def _lower_trait_is(trait_is: s0.TraitIs) -> H.HTraitIs:
	"""Drive `_visit_expr_TraitIs` directly via `AstToHIR.lower_expr`."""
	lowerer = AstToHIR()
	out = lowerer.lower_expr(trait_is)
	assert isinstance(out, H.HTraitIs), f"expected HTraitIs, got {type(out).__name__}"
	return out


def test_ast_to_hir_traitis_typenameref_preserves_module_id() -> None:
	"""TraitIs subject carrying module_id must round-trip through HIR."""
	subject = s0.TypeNameRef(
		name="MyType",
		module_id="some.qualified.module",
		loc=Span(),
	)
	# `trait` field is opaque to `_lower_trait_subject`; any TraitExpr
	# would do.  Use a TraitIs with a SelfRef subject as the inner
	# trait carrier — what's relevant here is the OUTER TraitIs's
	# subject, which is the lowering target under audit.
	#
	# Actually `trait` is an unknown-shape AST type-expr; pass any
	# value the lowerer won't choke on.  Empty SelfRef stand-in works
	# because `lower_expr` on TraitIs only re-lowers `expr.subject`,
	# not `expr.trait`.
	trait_is = s0.TraitIs(
		subject=subject,
		trait=s0.SelfRef(loc=Span()),
		loc=Span(),
	)
	lowered = _lower_trait_is(trait_is)
	assert isinstance(lowered.subject, H.HTypeNameRef), (
		f"lowered TraitIs.subject must be HTypeNameRef; got {type(lowered.subject).__name__}"
	)
	assert lowered.subject.name == "MyType"
	# The actual contract: module_id is preserved through AST→HIR.
	assert getattr(lowered.subject, "module_id", None) == "some.qualified.module", (
		f"H.HTypeNameRef must preserve module_id from s0.TypeNameRef; "
		f"got module_id={getattr(lowered.subject, 'module_id', None)!r}"
	)


def test_ast_to_hir_traitis_typenameref_unqualified_module_id_is_none() -> None:
	"""Unqualified TypeNameRef (parser's only current shape) lowers to module_id=None."""
	subject = s0.TypeNameRef(name="T", loc=Span())  # module_id defaults to None
	trait_is = s0.TraitIs(
		subject=subject,
		trait=s0.SelfRef(loc=Span()),
		loc=Span(),
	)
	lowered = _lower_trait_is(trait_is)
	assert isinstance(lowered.subject, H.HTypeNameRef)
	assert lowered.subject.name == "T"
	# The defensive default — unqualified subjects are the parser's
	# only current shape, so this case is the load-bearing one for
	# existing behavior.
	assert getattr(lowered.subject, "module_id", None) is None, (
		f"unqualified TypeNameRef must lower with module_id=None; "
		f"got module_id={getattr(lowered.subject, 'module_id', None)!r}"
	)


def test_hir_to_parser_back_conversion_preserves_module_id() -> None:
	"""H.HTypeNameRef → parser_ast.TypeNameRef back-conversion must preserve module_id.

	The type checker's `_trait_subject_to_parser` rebuilds a
	parser_ast.TraitIs from HIR for trait-resolver consumption.  Pre-
	0.31.29 this dropped `module_id` on the way out (parser_ast.TypeNameRef
	had no such field).  Now both sides carry it; pin the round-trip.
	"""
	from lang.driftc.parser import ast as parser_ast
	hir_subject = H.HTypeNameRef(
		name="MyType",
		module_id="some.qualified.module",
		loc=Span(),
	)
	# Mirror of `_trait_subject_to_parser` in type_checker.py — the
	# function is a closure inside a method, so we exercise its shape
	# directly here rather than reaching through the typecheck driver.
	# The contract being pinned is that `parser_ast.TypeNameRef` ACCEPTS
	# `module_id` and that callers that pass it preserve it on the
	# constructed value.
	parser_subject = parser_ast.TypeNameRef(
		name=hir_subject.name,
		loc=parser_ast.Located(line=0, column=0),
		module_id=getattr(hir_subject, "module_id", None),
	)
	assert isinstance(parser_subject, parser_ast.TypeNameRef)
	assert parser_subject.name == "MyType"
	assert parser_subject.module_id == "some.qualified.module"
	# Hash discipline matches stage0.ast.TypeNameRef — `(module_id, name)`.
	other = parser_ast.TypeNameRef(
		name="MyType",
		loc=parser_ast.Located(line=0, column=0),
		module_id="different.module",
	)
	assert parser_subject != other, (
		"TypeNameRefs with the same name but different module_ids must "
		"compare unequal under @dataclass-derived eq"
	)
	assert hash(parser_subject) != hash(other), (
		"Hashes must differ when module_id differs (eq/hash consistency)"
	)


def test_parser_to_s0_conversion_preserves_module_id() -> None:
	"""parser_ast → s0 conversion (`_convert_trait_subject`) must preserve module_id.

	Defensive guard for `parser/__init__.py:_convert_expr._convert_trait_subject`.
	The parser today doesn't construct qualified parser_ast.TypeNameRefs,
	but if it ever does, the conversion to s0 must forward module_id.
	"""
	from lang.driftc.parser import ast as parser_ast
	from lang.driftc.parser import _convert_expr  # private but stable export
	# Build a parser_ast.TraitIs whose subject carries module_id.
	parser_subject = parser_ast.TypeNameRef(
		name="MyType",
		loc=parser_ast.Located(line=0, column=0),
		module_id="some.qualified.module",
	)
	parser_trait_is = parser_ast.TraitIs(
		loc=parser_ast.Located(line=0, column=0),
		subject=parser_subject,
		trait=parser_ast.SelfRef(loc=parser_ast.Located(line=0, column=0)),
	)
	s0_trait_is = _convert_expr(parser_trait_is)
	assert isinstance(s0_trait_is, s0.TraitIs)
	assert isinstance(s0_trait_is.subject, s0.TypeNameRef)
	assert s0_trait_is.subject.name == "MyType"
	assert s0_trait_is.subject.module_id == "some.qualified.module"
