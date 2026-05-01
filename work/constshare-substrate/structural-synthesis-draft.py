# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""ConstShare auto-derive — Phase 1 (structs, concrete fields).

Discovers user struct definitions whose every owned field qualifies
under the v1 ConstShare composition rule and synthesizes a real
`implement ConstShare for X { fn const_share(...) ... }` block.
The synthesized AST goes through the normal HIR/MIR lowering
pipeline; no checker-only shortcuts.

**Qualification (purely prover-driven):**
A field type qualifies iff at least one of:
  - `prove_is(field_ty, ConstShare).status is PROVED`
  - `prove_is(field_ty, Copy).status is PROVED` AND
    `prove_is(field_ty, Frozen).status is PROVED`

No syntactic name checks (no `name == "ConstArc"`).  No hardcoded
primitive list.  All trait knowledge stays in the prover.

**Phase 1 scope:**
  - Structs only (no variants; deferred to phase 4).
  - Concrete fields only (no generic typevar fields; struct must
    have empty `type_params`).  Generic structs deferred to
    phase 3.
  - Same-module fixed-point: an auto-derived struct's synthesized
    impl is registered with the world before the next iteration,
    so a struct whose field is another auto-derived struct in the
    same module qualifies.
  - Cross-module / dependency-module / stdlib nominal types
    naturally qualify if the world already has impls for them
    (no special inter-module fixed-point — modules are processed
    in topological dep order, so dependency-side synthesis has
    already run by the time a consumer module's synthesis runs).
  - Skips structs that already have a hand-written
    `implement ConstShare for X` (the user-impl gate rejects those
    separately; synthesis must not produce a duplicate).

**Body shape (struct):**

    fn const_share(self: &Self) nothrow -> Self {
        return Self(
            f_cs   = self.f_cs.const_share(),   // ConstShare path
            f_cf   = self.f_cf,                 // Copy+Frozen path
            ...
        );
    }

For ConstShare-path fields: synthesize a `MethodCall` on the
borrowed field reference.  The trait method takes `&Self`, so the
borrowed field reference is the natural receiver — no
`pretend-owned` checker shortcut.

For Copy+Frozen-path fields: a plain `Attr(self, field_name)` read.
The reader's type is `&FieldT`; the constructor parameter expects
`FieldT`; Drift's existing borrow-of-Copy auto-copy machinery
handles the deref-and-copy at use site.  This is the same lowering
path that user-written `Self(f = self.f)` code goes through.
"""
from __future__ import annotations

from typing import List, Optional

from lang.driftc.parser import ast as parser_ast
from lang.driftc.traits.solver import Env, ProofStatus, prove_is
from lang.driftc.traits.world import (
	TraitKey,
	TraitWorld,
	TypeKey,
	type_key_from_expr,
)


# Trait keys referenced by qualification queries.  Package id is
# "std" once the path-vetted classifier in
# `parser/__init__.py::_is_stdlib_module` has run; for fresh in-tree
# stdlib builds (no --package-id) it is None.  Both spellings are
# tried so the synthesizer works in both modes — the prover's impl
# index uses the canonical form populated by `module_packages`.
_CONST_SHARE_TRAIT_NAME = "ConstShare"
_CONST_SHARE_TRAIT_MODULE = "std.core.shareable"
_COPY_TRAIT_NAME = "Copy"
_COPY_TRAIT_MODULE = "std.core.copy"
_FROZEN_TRAIT_NAME = "Frozen"
_FROZEN_TRAIT_MODULE = "std.core.shareable"


def _resolve_trait_key(
	world: TraitWorld,
	*,
	module: str,
	name: str,
	module_packages: dict,
) -> Optional[TraitKey]:
	"""Look up a trait key by canonical (module, name) — falls back
	to scanning the world's traits dict so we work in both
	package-mode (package_id resolved via module_packages) and
	in-tree-stdlib-build mode (package_id None)."""
	for key in getattr(world, "traits", {}).keys():
		if key.module == module and key.name == name:
			return key
	# Construct a candidate key the same way `trait_key_from_expr`
	# would.  Useful when the trait is defined in a not-yet-seen
	# module (e.g. tested via prove_is with cross-package impls).
	pkg = (module_packages or {}).get(module)
	return TraitKey(package_id=pkg, module=module, name=name)


def _field_qualifies(
	field_ty_key: TypeKey,
	*,
	world: TraitWorld,
	env: Env,
	const_share_key: TraitKey,
	copy_key: TraitKey,
	frozen_key: TraitKey,
) -> Optional[str]:
	"""Returns the qualification path the field takes:
	  - "const_share"  → field proves ConstShare directly; body must
	    call `field.const_share()`.
	  - "copy_frozen"  → field is Copy+Frozen; body reads field
	    directly via the standard borrowed-Copy path.
	  - None           → field does not qualify; struct cannot
	    auto-derive.
	"""
	# Path 1: ConstShare direct.
	cs = prove_is(world, env, {}, field_ty_key, const_share_key)
	if cs.status is ProofStatus.PROVED:
		return "const_share"
	# Path 2: Copy + Frozen.
	cp = prove_is(world, env, {}, field_ty_key, copy_key)
	fz = prove_is(world, env, {}, field_ty_key, frozen_key)
	if cp.status is ProofStatus.PROVED and fz.status is ProofStatus.PROVED:
		return "copy_frozen"
	return None


def _synthesize_const_share_method(
	struct_def: parser_ast.StructDef,
	field_paths: List[str],
) -> parser_ast.FunctionDef:
	"""Build the `fn const_share(self: &Self) nothrow -> Self` method
	body for a struct that auto-derives ConstShare.

	`field_paths[i]` is "const_share" or "copy_frozen", indicating
	which body shape each field uses.
	"""
	loc = struct_def.loc
	struct_name = struct_def.name
	# Self type expression: just the struct name (resolved by the
	# normal type-resolution pass; no module qualifier needed because
	# the synthesized impl lives in the same module as the struct).
	self_type = parser_ast.TypeExpr(name=struct_name, loc=loc)
	self_ref_type = parser_ast.TypeExpr(name="&", args=[self_type], loc=loc)
	# Build the constructor kwargs: one per field.
	kwargs: List[parser_ast.KwArg] = []
	for field, path in zip(struct_def.fields, field_paths):
		# `self.f` — borrowed field access on the receiver.
		self_name = parser_ast.Name(loc=loc, ident="self")
		field_attr = parser_ast.Attr(loc=loc, value=self_name, attr=field.name)
		if path == "const_share":
			# `self.f.const_share()`
			cs_attr = parser_ast.Attr(loc=loc, value=field_attr, attr="const_share")
			cs_call = parser_ast.Call(
				loc=loc,
				func=cs_attr,
				args=[],
				kwargs=[],
			)
			kwargs.append(parser_ast.KwArg(name=field.name, value=cs_call, loc=loc))
		else:  # "copy_frozen"
			# `self.f` — the borrowed-Copy auto-copy path handles
			# the rest at lowering time.
			kwargs.append(parser_ast.KwArg(name=field.name, value=field_attr, loc=loc))
	# Constructor call: `Self(f1=..., f2=..., ...)`.
	struct_ctor = parser_ast.Call(
		loc=loc,
		func=parser_ast.Name(loc=loc, ident=struct_name),
		args=[],
		kwargs=kwargs,
	)
	body = parser_ast.Block(statements=[
		parser_ast.ReturnStmt(loc=loc, value=struct_ctor),
	])
	# Self parameter — `self: &Self`.
	self_param = parser_ast.Param(
		name="self",
		type_expr=parser_ast.TypeExpr(name="&", args=[self_type], loc=loc),
		mutable=False,
	)
	method = parser_ast.FunctionDef(
		name="const_share",
		orig_name="const_share",
		type_params=[],
		params=[self_param],
		return_type=self_type,
		body=body,
		loc=loc,
		declared_nothrow=True,
		is_pub=True,
		is_method=True,
		self_mode="ref",
		impl_target=self_type,
	)
	return method


def _build_const_share_impl(
	struct_def: parser_ast.StructDef,
	method: parser_ast.FunctionDef,
) -> parser_ast.ImplementDef:
	"""Wrap the synthesized method in an ImplementDef pointing at
	the struct as target with `ConstShare` as the trait."""
	loc = struct_def.loc
	target = parser_ast.TypeExpr(name=struct_def.name, loc=loc)
	# Trait reference: `shareable.ConstShare`.  We use the explicit
	# `module_id` to bypass alias resolution — the synthesized impl
	# runs after the parser's alias-resolution pass, and we want
	# the canonical fully-qualified trait identity recorded.
	trait = parser_ast.TypeExpr(
		name=_CONST_SHARE_TRAIT_NAME,
		loc=loc,
		module_id=_CONST_SHARE_TRAIT_MODULE,
	)
	return parser_ast.ImplementDef(
		target=target,
		loc=loc,
		type_params=[],
		type_param_locs=[],
		trait=trait,
		require=None,
		methods=[method],
	)


def synthesize_const_share_impls(
	prog: parser_ast.Program,
	*,
	world: TraitWorld,
	env: Env,
	module_id: str,
	package_id: Optional[str],
	module_packages: dict,
) -> int:
	"""Discover auto-derivable structs in `prog` and append
	synthesized `ImplementDef`s to `prog.implements`.  Mutates
	`world` so subsequent prover queries within the same module
	see the synthesized impls (same-module fixed-point).

	Returns the number of impls synthesized.

	Phase 1: structs only, concrete fields only, no generics, no
	variants, no inter-module fixed-point.
	"""
	const_share_key = _resolve_trait_key(
		world,
		module=_CONST_SHARE_TRAIT_MODULE,
		name=_CONST_SHARE_TRAIT_NAME,
		module_packages=module_packages,
	)
	if const_share_key is None:
		# Trait not visible (e.g. consumer build that did not import
		# shareable) — nothing to synthesize.
		return 0
	copy_key = _resolve_trait_key(
		world,
		module=_COPY_TRAIT_MODULE,
		name=_COPY_TRAIT_NAME,
		module_packages=module_packages,
	)
	frozen_key = _resolve_trait_key(
		world,
		module=_FROZEN_TRAIT_MODULE,
		name=_FROZEN_TRAIT_NAME,
		module_packages=module_packages,
	)
	if copy_key is None or frozen_key is None:
		return 0

	# Track structs that already have a hand-written ConstShare impl;
	# skip synthesis to avoid registering a duplicate.  (The
	# user-impl gate at `world.py` rejects user-written ConstShare
	# impls separately; we never see those at synthesis time, but
	# be defensive.)
	existing_impls: set[str] = set()
	for impl in getattr(prog, "implements", []) or []:
		trait = getattr(impl, "trait", None)
		if trait is None:
			continue
		if (
			getattr(trait, "name", None) == _CONST_SHARE_TRAIT_NAME
			and (
				getattr(trait, "module_id", None) == _CONST_SHARE_TRAIT_MODULE
				or getattr(trait, "module_id", None) is None
			)
		):
			target = getattr(impl, "target", None)
			tname = getattr(target, "name", None) if target is not None else None
			if isinstance(tname, str):
				existing_impls.add(tname)

	candidates: List[parser_ast.StructDef] = []
	for s in getattr(prog, "structs", []) or []:
		# Phase 1: skip generic structs (type_params non-empty).
		if list(getattr(s, "type_params", []) or []):
			continue
		# Skip structs with no fields — empty struct construction
		# semantics are an open question; defer until a clear use
		# case appears.
		if not list(getattr(s, "fields", []) or []):
			continue
		if s.name in existing_impls:
			continue
		candidates.append(s)

	derived: dict[str, List[str]] = {}  # struct_name -> [field_paths]
	synthesized = 0
	# Same-module fixed-point: a struct whose field is another
	# candidate-derived struct should qualify once that other
	# struct's synthesized impl is registered with the world.
	# Iterate until no progress.
	changed = True
	while changed:
		changed = False
		for s in candidates:
			if s.name in derived:
				continue
			# Build TypeKey for each field type and qualify.
			field_paths: List[str] = []
			ok = True
			for field in s.fields:
				field_ty_key = type_key_from_expr(
					field.type_expr,
					default_module=module_id,
					default_package=package_id,
					module_packages=module_packages,
				)
				path = _field_qualifies(
					field_ty_key,
					world=world,
					env=env,
					const_share_key=const_share_key,
					copy_key=copy_key,
					frozen_key=frozen_key,
				)
				if path is None:
					ok = False
					break
				field_paths.append(path)
			if not ok:
				continue
			# Qualified — synthesize and register.
			method = _synthesize_const_share_method(s, field_paths)
			impl = _build_const_share_impl(s, method)
			prog.implements = list(getattr(prog, "implements", []) or []) + [impl]
			# Register with world so subsequent iteration sees it.
			# We mirror what `build_trait_world` does for impl
			# registration: build an `ImplRef` and append to
			# `world.impls_by_trait_target`.
			_register_synthesized_impl_with_world(
				world,
				impl=impl,
				struct_def=s,
				module_id=module_id,
				package_id=package_id,
				module_packages=module_packages,
				const_share_key=const_share_key,
			)
			derived[s.name] = field_paths
			synthesized += 1
			changed = True
	return synthesized


def _register_synthesized_impl_with_world(
	world: TraitWorld,
	*,
	impl: parser_ast.ImplementDef,
	struct_def: parser_ast.StructDef,
	module_id: str,
	package_id: Optional[str],
	module_packages: dict,
	const_share_key: TraitKey,
) -> None:
	"""Register an `ImplDef` for the synthesized impl into the world
	so the prover sees it in subsequent queries.  Mirrors the
	impl-registration code in `world.py::build_trait_world` (around
	lines 904-919): append to `world.impls` and update the three
	`impls_by_*` indices.
	"""
	from lang.driftc.traits.world import ImplDef
	target_key = type_key_from_expr(
		impl.target,
		default_module=module_id,
		default_package=package_id,
		module_packages=module_packages,
	)
	target_head = target_key.head()
	impl_id = len(world.impls)
	world.impls.append(
		ImplDef(
			trait=const_share_key,
			trait_args=(),
			target=target_key,
			target_head=target_head,
			methods=list(getattr(impl, "methods", []) or []),
			require=None,
			type_params=list(getattr(impl, "type_params", []) or []),
			loc=getattr(impl, "loc", None),
		)
	)
	world.impls_by_trait.setdefault(const_share_key, []).append(impl_id)
	world.impls_by_target_head.setdefault(target_head, []).append(impl_id)
	world.impls_by_trait_target.setdefault(
		(const_share_key, target_head), []
	).append(impl_id)
