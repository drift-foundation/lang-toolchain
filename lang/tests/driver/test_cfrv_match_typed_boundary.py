# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Checker-BOUNDARY pin for control-flow-value typing
(work/control-flow-rvalue-ownership, P3 final-audit).

`record_expr()` overwrites `expr_types[node_id]` on EVERY visit, and several
later passes (post-resolution/autoborrow receiver typing, generic-require
receiver typing, method-argument retyping with expected parameter types, and
the checker-synthesized `HBorrow` subject) used to re-type nodes with
`used_as_value=False` — collapsing an already-correct `match` result to Void in
the FINAL typed HIR.  Stage2's `_cfg_result_type` arm-type fallback then
recovered the type at lowering, so runtime fixtures passed while the typed HIR
was silently wrong.

This pin exercises and STRUCTURALLY identifies each changed path, then inspects
`TypedFn.expr_types` AFTER the whole method/call path:

  (A) a DIRECT match method receiver — `(match …).size()` — HMethodCall.receiver
      resolves to the match itself (not a projected field), and stays Node;
  (B) a match ARGUMENT to a `&Node` method parameter — `s.absorb(match …)` — the
      checker synthesizes `HBorrow(subject=HMatchExpr(...))`, exercised by the
      method-arg expected-parameter retyping, and the wrapped match stays Node;
  (C) a match method receiver of a GENERIC-impl method — `(match …).peek()` on
      `Box<Int>` — exercises the generic-require receiver retyping branch, and
      stays the owned `Box<…>` struct.

Each context is asserted by exact shape and type, not a generic "all matches are
Node", so the stage2 arm fallback cannot mask another checker-boundary
regression, and a disappearance of the synthesized HBorrow would fail (B).

Verified properties (checked while authoring):
  * (B) is adversarially LOAD-BEARING: reverting the HBorrow rvalue-subject fix
    (type_checker.py ~9400) leaves the program COMPILING yet fails this test on
    the Void in expr_types — the stage2 fallback can no longer hide it.
  * the changed method-arg retyping (~10499) and generic-require receiver
    retyping (~10421) sites are both REACHED by (B)/(C) (confirmed by
    reachability instrumentation); for these shapes they delegate typing through
    the fixed HBorrow handler / the initial `_type_user_arg` value-typing, so the
    final expr_types stays correct.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.impl_index import GlobalImplIndex
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex
from lang.driftc.type_checker import TypeChecker
from lang.driftc.core.types_core import TypeKind


_SRC = """
module m_main;
import std.core as core;

struct Node { text: String, }
implement Node {
	pub fn size(self: &Node) nothrow -> Int { return self.text.byte_length(); }
}

struct Sink { tag: Int, }
implement Sink {
	pub fn absorb(self: &Sink, n: &Node) nothrow -> Int { return n.size(); }
}

struct Box<T> { item: T, }
implement<T> Box<T> {
	// The `require` clause routes the receiver through the generic-require
	// receiver retyping branch (type_checker.py ~10421).
	pub fn peek(self: &Box<T>) nothrow -> Int require T is core.Copy { return 7; }
}

fn make_a() nothrow -> Node { return Node(text = "aa" + ""); }
fn make_b() nothrow -> Node { return Node(text = "bb" + ""); }
fn box_a() nothrow -> Box<Int> { return Box(item = 1); }
fn box_b() nothrow -> Box<Int> { return Box(item = 2); }

pub fn main() nothrow -> Int {
	// (A) DIRECT match method receiver.
	val r1 = (match true { true => { make_a() }, false => { make_b() } }).size();
	// (B) match ARGUMENT to a `&Node` method parameter (autoborrow -> HBorrow).
	val s = Sink(tag = 0);
	val r2 = s.absorb(match true { true => { make_a() }, false => { make_b() } });
	// (C) match method receiver of a GENERIC-impl method.
	val r3 = (match true { true => { box_a() }, false => { box_b() } }).peek();
	return r1 + r2 + r3 - 11;
}
"""


def _walk(node, out):
	out.append(node)
	d = getattr(node, "__dict__", {})
	for v in d.values():
		if isinstance(v, H.HExpr):
			_walk(v, out)
		elif isinstance(v, H.HBlock):
			for st in v.statements:
				_walk_stmt(st, out)
		elif isinstance(v, (list, tuple)):
			for it in v:
				if isinstance(it, H.HExpr):
					_walk(it, out)
				else:
					_walk_container(it, out)


def _walk_stmt(st, out):
	if isinstance(st, H.HExpr):
		_walk(st, out)
	for v in getattr(st, "__dict__", {}).values():
		if isinstance(v, H.HExpr):
			_walk(v, out)
		elif isinstance(v, H.HBlock):
			for s2 in v.statements:
				_walk_stmt(s2, out)


def _walk_container(it, out):
	# match arms / kwargs carry nested exprs and blocks.
	for v in getattr(it, "__dict__", {}).values():
		if isinstance(v, H.HExpr):
			_walk(v, out)
		elif isinstance(v, H.HBlock):
			for s2 in v.statements:
				_walk_stmt(s2, out)


def _match_in(expr):
	"""The HMatchExpr reached through an optional synthesized HBorrow."""
	if isinstance(expr, H.HMatchExpr):
		return expr, None
	if isinstance(expr, H.HBorrow) and isinstance(getattr(expr, "subject", None), H.HMatchExpr):
		return expr.subject, expr
	return None, None


def _check_main(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(_SRC)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths, module_paths=[tmp_path], stdlib_root=stdlib_root())
	assert diagnostics == [], [str(d) for d in diagnostics]
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)

	registry = CallableRegistry()
	module_ids = {None: 0}
	next_id = 1
	for fn_id, sig in signatures.items():
		if sig.return_type_id is None or getattr(sig, "is_wrapper", False):
			continue
		module_name = getattr(fn_id, "module", None) or getattr(sig, "module", None)
		module_id = module_ids.setdefault(module_name, len(module_ids))
		if sig.is_method:
			if sig.impl_target_type_id is None or sig.self_mode is None:
				continue
			self_mode = {"value": SelfMode.SELF_BY_VALUE, "ref": SelfMode.SELF_BY_REF,
			             "ref_mut": SelfMode.SELF_BY_REF_MUT}.get(sig.self_mode)
			if self_mode is None:
				continue
			registry.register_inherent_method(
				callable_id=next_id, name=sig.method_name or sig.name, module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids or []), result_type=sig.return_type_id),
				fn_id=fn_id, impl_id=next_id, impl_target_type_id=sig.impl_target_type_id,
				self_mode=self_mode, is_generic=bool(sig.type_params or getattr(sig, "impl_type_params", [])))
		else:
			registry.register_free_function(
				callable_id=next_id, name=fn_id.name, module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids or []), result_type=sig.return_type_id),
				fn_id=fn_id, is_generic=bool(sig.type_params))
		next_id += 1

	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=module_exports, type_table=type_table, module_ids=module_ids)
	trait_index = GlobalTraitIndex.from_trait_worlds(getattr(type_table, "trait_worlds", None))
	trait_impl_index = GlobalTraitImplIndex.from_module_exports(
		module_exports=module_exports, type_table=type_table, module_ids=module_ids)
	trait_scope_by_module = {}
	if isinstance(module_exports, dict):
		for _mod_name, exp in module_exports.items():
			if isinstance(exp, dict):
				scope = exp.get("trait_scope", [])
				if isinstance(scope, list):
					trait_scope_by_module[_mod_name] = list(scope)
	linked_world, require_env = build_linked_world(type_table)

	main_id = next(fid for fid in signatures if fid.name == "main" and fid.module == "m_main")
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = tuple(sorted(module_ids.values()))
	visibility_provenance = {mid: (name if name is not None else "<unknown>",) for name, mid in module_ids.items()}
	result = TypeChecker(type_table=type_table).check_function(
		main_id, main_block, param_types={},
		return_type=main_sig.return_type_id, signatures_by_id=signatures,
		callable_registry=registry, impl_index=impl_index, trait_index=trait_index,
		trait_impl_index=trait_impl_index, trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world, require_env=require_env, visible_modules=visible_mods,
		current_module=current_mod, visibility_provenance=visibility_provenance)
	assert result.diagnostics == [], [str(d) for d in result.diagnostics]
	return result, main_block, type_table


def test_match_contexts_typed_at_checker_boundary(tmp_path: Path) -> None:
	result, main_block, type_table = _check_main(tmp_path)
	expr_types = result.typed_fn.expr_types

	node_ty = type_table.get_nominal(kind=TypeKind.STRUCT, module_id="m_main", name="Node")
	assert node_ty is not None

	def _ty(n):
		return expr_types.get(getattr(n, "node_id", None))

	def _kind(ty):
		return type_table.get(ty).kind if ty is not None else None

	nodes = []
	_walk(main_block, nodes)
	method_calls = [n for n in nodes if isinstance(n, H.HMethodCall)]
	by_name = {}
	for mc in method_calls:
		by_name.setdefault(mc.method_name, []).append(mc)

	# --- (A) direct match method receiver: `(match ...).size()` ---
	size_calls = by_name.get("size", [])
	assert len(size_calls) == 1, f"expected exactly one .size() call, found {len(size_calls)}"
	a_match, _ = _match_in(size_calls[0].receiver)
	assert a_match is not None, (
		f".size() receiver must resolve to the match itself, got "
		f"{type(size_calls[0].receiver).__name__}")
	a_ty = _ty(a_match)
	assert a_ty is not None and _kind(a_ty) is not TypeKind.VOID, "receiver match collapsed to Void"
	assert a_ty == node_ty, f"receiver match typed {type_table.get(a_ty).name}, expected Node"

	# --- (B) match argument to a `&Node` method param: synthesized HBorrow ---
	absorb_calls = by_name.get("absorb", [])
	assert len(absorb_calls) == 1, f"expected exactly one .absorb() call, found {len(absorb_calls)}"
	arg0 = absorb_calls[0].args[0]
	# STRUCTURAL: the checker must have synthesized HBorrow(subject=HMatchExpr).
	assert isinstance(arg0, H.HBorrow), (
		f"match argument to &Node param must be wrapped in a synthesized HBorrow, "
		f"got {type(arg0).__name__}")
	assert isinstance(arg0.subject, H.HMatchExpr), (
		f"synthesized HBorrow.subject must be the match, got {type(arg0.subject).__name__}")
	b_ty = _ty(arg0.subject)
	assert b_ty is not None and _kind(b_ty) is not TypeKind.VOID, (
		"HBorrow-wrapped match collapsed to Void after expected-parameter retyping")
	assert b_ty == node_ty, f"argument match typed {type_table.get(b_ty).name}, expected Node"

	# --- (C) generic-impl method receiver: `(match ...).peek()` on Box<Int> ---
	peek_calls = by_name.get("peek", [])
	assert len(peek_calls) == 1, f"expected exactly one .peek() call, found {len(peek_calls)}"
	c_match, _ = _match_in(peek_calls[0].receiver)
	assert c_match is not None, (
		f".peek() receiver must resolve to the match itself, got "
		f"{type(peek_calls[0].receiver).__name__}")
	c_ty = _ty(c_match)
	assert c_ty is not None and _kind(c_ty) is not TypeKind.VOID, (
		"generic-impl receiver match collapsed to Void after generic-require retyping")
	c_def = type_table.get(c_ty)
	assert c_def.kind is TypeKind.STRUCT and c_def.name == "Box", (
		f"generic receiver match typed {c_def.kind.name}:{c_def.name}, expected struct Box")

	# --- exact count: three source matches, each identified above ---
	matches = [n for n in nodes if isinstance(n, H.HMatchExpr)]
	assert len(matches) == 3, f"expected exactly 3 match nodes, found {len(matches)}"
	assert {id(a_match), id(arg0.subject), id(c_match)} == {id(m) for m in matches}, (
		"the three identified match contexts must be exactly the three source matches")
