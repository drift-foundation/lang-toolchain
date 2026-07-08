# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Middle Intermediate Representation (MIR).

Pipeline placement:
  AST (lang/stage0/ast.py) → HIR (lang/stage1/hir_nodes.py) → MIR (this file) → SSA → LLVM/obj

This MIR sits between HIR (sugar-free AST) and SSA construction. It is explicit:
- No surface sugar.
- Explicit locals, loads/stores, calls, and control flow.
- No SSA yet; φ nodes are represented structurally and added during SSA.

Use this file as a reference for what MIR can express. There are **no semantics**
baked in here; it is just a typed tree of instructions/terminators/blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional, Union

from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import TypeId
from lang.driftc.stage1 import UnaryOp, BinaryOp


class MNode:
	"""Base class for MIR nodes (instructions and terminators)."""
	pass


class MInstr(MNode):
	"""Base class for MIR instructions (non-terminators)."""
	pass


class MTerminator(MNode):
	"""Base class for MIR terminators (end of a basic block).

	Every terminator defines the CFG-successor contract below.  This is the
	single authoritative answer to "which basic blocks may this terminator branch
	to" — all CFG users (drop/liveness dataflow, cleanup authoring, SSA, dominance)
	must consult it rather than hand-rolling `isinstance` dispatch, so that adding
	or changing a terminator updates successor semantics in exactly one place.

	The base raises `NotImplementedError` on purpose: a new terminator that forgets
	to implement `successors()` fails LOUDLY here instead of silently reporting no
	successors (which would make dataflow/cleanup/SSA treat reachable code as dead —
	a class of silent miscompile this contract exists to prevent).
	"""

	def successors(self) -> "list[str]":
		"""Block names this terminator may branch to, in a stable order."""
		raise NotImplementedError(
			f"{type(self).__name__} must implement successors() (MIR CFG-successor contract)"
		)

	def successor_edges(self) -> "list[tuple[str, str]]":
		"""`(target_block, edge_label)` pairs, in the same order as `successors()`.

		Edge labels let edge-sensitive passes (e.g. cleanup edge-splitting)
		distinguish *which* outgoing edge reaches a block.  Default derives
		anonymous labels from `successors()`; terminators with meaningful edge
		identity (e.g. if-then/if-else) override this."""
		return [(t, "succ") for t in self.successors()]

	def value_uses(self) -> "list[str]":
		"""ValueIds this terminator READS (e.g. an `if`'s condition, a `switch`'s
		scrutinee, a `return`'s value).  Block-name targets are NOT values and are
		excluded.  Liveness/use analyses consult this so a value consumed only by a
		terminator stays live to the block end.  Base raises so a new terminator
		can't silently drop its value uses (which would let that value be freed
		early — a UAF)."""
		raise NotImplementedError(
			f"{type(self).__name__} must implement value_uses() (MIR terminator value-use contract)"
		)

	def remap_targets(self, mapping: "dict[str, str]") -> None:
		"""Rewrite every target block name in place through *mapping* (absent keys
		unchanged).  The single owner of "this terminator's target fields"; callers
		that rename/redirect blocks use this instead of poking `then_target` etc.
		directly.  Base raises so a new terminator's targets can't be silently
		missed by a block-rename pass."""
		raise NotImplementedError(
			f"{type(self).__name__} must implement remap_targets() (MIR terminator target contract)"
		)

	def redirect_edge(self, edge_label: str, new_target: str) -> None:
		"""Redirect the single outgoing edge identified by *edge_label* (a label
		from `successor_edges()`) to *new_target*, in place.  Used by cleanup
		edge-splitting to insert a block on one specific edge.  Base raises."""
		raise NotImplementedError(
			f"{type(self).__name__} must implement redirect_edge() (MIR terminator target contract)"
		)


# Locals and values

LocalId = str  # simple alias for now; can be made richer later
ValueId = str


# Instructions

@dataclass
class ConstInt(MInstr):
	"""dest = constant integer"""
	dest: ValueId
	value: int


@dataclass
class ConstUint(MInstr):
	"""dest = constant unsigned integer"""
	dest: ValueId
	value: int


@dataclass
class ConstUint64(MInstr):
	"""dest = constant unsigned 64-bit integer"""
	dest: ValueId
	value: int


@dataclass
class ConstByte(MInstr):
	"""dest = constant byte (u8)"""
	dest: ValueId
	value: int


@dataclass
class IntFromUint(MInstr):
	"""dest = cast Int from Uint (isize/usize conversion)."""
	dest: ValueId
	value: ValueId


@dataclass
class UintFromInt(MInstr):
	"""dest = cast Uint from Int (usize/isize conversion)."""
	dest: ValueId
	value: ValueId


@dataclass
class CastScalar(MInstr):
	"""dest = cast scalar value to another scalar type."""
	dest: ValueId
	value: ValueId
	src_ty: TypeId
	dst_ty: TypeId


@dataclass
class ConstBool(MInstr):
	"""dest = constant bool"""
	dest: ValueId
	value: bool


@dataclass
class ConstVoid(MInstr):
	"""dest = constant void value (placeholder)."""
	dest: ValueId


@dataclass
class ConstString(MInstr):
	"""dest = constant string (UTF-8 bytes as-is)."""
	dest: ValueId
	value: str


@dataclass
class ConstFloat(MInstr):
	"""
	dest = constant Float

	In lang v1, `Float` is IEEE-754 double precision and maps to LLVM `double`.
	This instruction carries the Python `float` value that should be emitted as a
	`double` constant in LLVM IR.
	"""
	dest: ValueId
	value: float


@dataclass
class FnPtrConst(MInstr):
	"""dest = function pointer constant."""
	dest: ValueId
	fn_ref: "FunctionRefId"
	call_sig: "CallSig"


@dataclass
class ConstructIface(MInstr):
	"""dest = interface value from a function pointer (callback)."""
	dest: ValueId
	iface_ty: TypeId
	fn_ref: "FunctionRefId"
	call_sig: "CallSig"
	data: ValueId | None = None
	data_ty: TypeId | None = None
	env_ty: TypeId | None = None


@dataclass
class ConstructIfaceValue(MInstr):
	"""dest = interface value by boxing a concrete value."""
	dest: ValueId
	iface_ty: TypeId
	value: ValueId
	value_ty: TypeId


@dataclass
class ConstructIfaceBorrowed(MInstr):
	"""dest = NON-OWNING interface view over caller-owned storage.

	`data_ref` is a pointer (`&Concrete` / `&mut Concrete`) into storage the
	caller keeps owning; the fat value's flag byte carries the BORROWED bit
	so its drop is a complete no-op (no payload destroy, no free). Emitted
	only for checker-recorded `borrowed_iface_coercions` — the view temp is
	compiler-synthesized and used exclusively in `&temp` argument position,
	so it can never escape its constructing frame as an owned value
	(interfaces are non-Copy; no move-through-ref)."""
	dest: ValueId
	iface_ty: TypeId
	data_ref: ValueId
	value_ty: TypeId


@dataclass
class IfaceUpcast(MInstr):
	"""dest = interface value with vtable pointer retargeted to parent segment."""
	dest: ValueId
	iface: ValueId
	slot_offset: int


@dataclass
class ArcAsInterface(MInstr):
	"""Fat `Arc<I>` construction from a thin `&Arc<T=concrete>` and a
	target interface `I`.

	Semantics (ownership/view conversion, not plain struct construction):
	- Read `self.buf` (`RawBuffer<ArcBox<T>>`) from the thin Arc<T>.
	- Compute `ctrl = rawbuffer_ptr(&self.buf)` — the base of the
	  ArcBox<T> allocation (strong count at offset 0).
	- Atomic fetch-add +1 on `ctrl.strong` — one strong-count bump
	  shared across every fat view deriving from this allocation.
	- Compute `data = &(ArcBox<T>)ctrl.value` — concrete payload
	  address computed from the *actual* ArcBox<T> struct layout
	  for this T (alignment padding is T-dependent; do NOT assume
	  `ctrl + sizeof(ArcHeader)`).
	- Resolve `vtable` via the existing
	  `_ensure_interface_vtable(I, T)` hook — no Arc-specific
	  vtable namespace.
	- Construct the fat `Arc<I>` struct `{ctrl, data, vtable}`.

	The invariants this op encodes (documented here because they
	drive LLVM lowering correctness):
	- exactly ONE allocation / ONE control block;
	- exactly ONE atomic strong bump per conversion;
	- `data` points inside the concrete `ArcBox<T>` allocation, not
	  a separate copy of the payload;
	- last drop runs the concrete T's destructor via the
	  `drop_thunk` captured at `arc<T=concrete>(value)` time,
	  independent of which fat view holds the final strong
	  reference.

	See `doc/history.md` 2026-04-18 (fat `Arc<Interface>`, 0.28.0,
	ABI 10) for the representation soundness argument.
	"""

	dest: ValueId
	src_arc_ref: ValueId
	"""Pointer to the thin `Arc<T>` struct (i.e. the receiver value
	after auto-borrow)."""

	src_arc_ty: TypeId
	"""Thin `Arc<T>` concrete struct TypeId — carries the
	`{buf: RawBuffer<ArcBox<T>>}` layout the op reads from."""

	concrete_ty: TypeId
	"""T — used to compute `ArcBox<T>` layout and the
	`_ensure_interface_vtable(I, T)` lookup."""

	iface_ty: TypeId
	"""I — target interface TypeId for the vtable lookup and
	for the result type's layout identity."""

	result_ty: TypeId
	"""Fat `Arc<I>` struct TypeId (fields: `{ctrl, data, vtable}`,
	all `mem.Ptr<Byte>`) — what the op writes into `dest`."""


@dataclass
class ArcFatGet(MInstr):
	"""Fat `Arc<I>.get()` lowering — borrowed interface reference
	from a fat Arc's already-resolved `{data, vtable}` pair.

	Semantics:
	- Read `data` (field 1) and `vtable` (field 2) from the fat
	  `Arc<I>`.
	- Construct the standard borrowed-interface shape
	  (`DRIFT_IFACE_TYPE` = `{data_ptr, vtable_ptr, inline_flag}`)
	  directly from those pointers.
	- No refcount touch.  No new vtable lookup — the vtable
	  was resolved once at `ARC_AS_INTERFACE` time and has been
	  carried in the fat handle ever since.
	- Result is a borrowed `&I` reference; lifetime is tied to
	  the fat Arc<I> receiver's borrow scope by the type checker.

	Distinct from `ConstructIface` (fn-pointer callback shape)
	and `ConstructIfaceValue` (boxed-value-interface shape) —
	fat ARC_GET is the "borrowed interface from an existing
	`{data, vtable}` pair" shape, which neither of those
	expresses cleanly.
	"""

	dest: ValueId
	src_arc_ref: ValueId
	"""Pointer to the fat `Arc<I>` struct."""

	src_arc_ty: TypeId
	"""Fat `Arc<I>` struct TypeId (`{ctrl, data, vtable}`)."""

	iface_ty: TypeId
	"""I — the interface being borrowed."""

	result_ref_ty: TypeId
	"""`&I` reference TypeId."""


@dataclass
class ZeroValue(MInstr):
	"""
	dest = 0-value of a type (zero / null / zero-initialized aggregate).

	This instruction exists primarily to support `move <place>` semantics in a
	way that is:
	- ABI-safe (moved-from storage is reset to a known safe value), and
	- allocation-free (unlike constructing an empty `String` via runtime helpers).

	Codegen contract:
	- For scalars, this should be a cheap constant.
	- For aggregates, this should be constructed without calling into the runtime.
	"""
	dest: ValueId
	ty: TypeId


@dataclass
class TombstoneValue(MInstr):
	"""
	dest = DROP-SAFE TOMBSTONE VALUE for a storage slot of type `ty`.

	Produces bytes whose subsequent `DropValue(dest, ty)` is a no-op
	for the SPECIFIC TYPE CLASSES Drift currently has a tombstone
	model for:

	- **Variants** (droppable) — reserved `__drift_internal_tombstone`
	  tag, or a user-declared `@tombstone` ctor.  The variant
	  drop-dispatch switch routes that tag directly to `done_block`,
	  skipping all payload destructors.  SAFE.
	- **String / Array** — release-on-null is a runtime no-op; zero
	  bytes represent an empty/released slot.  SAFE.
	- **Interface** — null fat-pointer drop is a runtime no-op.
	  SAFE.
	- **Plain aggregates of the above** (structs containing only
	  tombstone-safe fields) — composed of SAFE parts.  SAFE.

	**NOT safe** for structs that dispatch to a USER
	`core.Destructible` destroy fn — i.e. those registered in
	`type_table.destructor_fns`.  Example: `struct Token { session:
	&mut Session }` with a user `destroy(self: Token)` impl.  Zero
	/ tombstone field bytes yield a null-bearing receiver, and the
	user-authored destroy reads those nulls (e.g.
	`self.session.drops = …`) → SEGV.  Drift has no per-slot drop
	flags and no tombstone-tag for structs, so there is no byte
	pattern that is universally drop-safe for every custom
	Destructible struct.

	Enforcement is narrow and explicit: the LLVM backend raises
	`AssertionError` at the MIR-instruction boundary iff the
	`TombstoneValue.ty` is a STRUCT whose `destructor_fns[ty]`
	entry is set — i.e. drop would dispatch to USER code.
	Structs with NO `destructor_fns` entry are accepted: their
	drop falls through to the generic field-by-field path in
	`_emit_drop_value`, which is safe on tombstoned bytes because
	each field is itself tombstoned to a drop-safe pattern by the
	recursion in `_emit_tombstone_value`.  Callers emitting MIR
	`TombstoneValue` for registered-user-destructor structs is a
	hard internal bug, not a silent unsafe emission.

	Note: the shared `_emit_tombstone_value` helper is ALSO called
	from the `ArrayElemTake` slot-neutralize path, where per-
	element drop legitimately calls the user destructor on the
	tombstoned bytes.  That path's safety is a contract of
	`ArrayElemTake` (callers accept that user destroy runs on the
	tombstoned slot), not of this MIR node — hence the enforcement
	is at the MIR `TombstoneValue` boundary, not inside the shared
	helper.

	Intended use: store the result back into an owning storage slot
	that has JUST been `MoveOut`'d so that any later scope-drop on
	that slot is a provable no-op.  This is the `Array.pop` /
	`Array.remove` pattern, generalized to match-scrutinee lowering
	via `_ensure_arm_scrut_ptr`.

	Codegen contract:
	- LLVM backend MUST route this through `_emit_tombstone_value(ty)`
	  (not `_emit_zero_value`), which consults the variant's
	  `internal_tombstone_tag` / user `@tombstone` declaration.
	"""
	dest: ValueId
	ty: TypeId


@dataclass
class StringRetain(MInstr):
	"""dest = retain(value) (String only)."""
	dest: ValueId
	value: ValueId


@dataclass
class StringRelease(MInstr):
	"""release(value) (String only)."""
	value: ValueId


@dataclass
class CopyValue(MInstr):
	"""dest = copy(value) (semantic copy for Copy types)."""
	dest: ValueId
	value: ValueId
	ty: TypeId


@dataclass
class DropValue(MInstr):
	"""drop(value) (semantic drop for destructible values)."""
	value: ValueId
	ty: TypeId


@dataclass
class CleanupHook(MInstr):
	"""Phase 4 site-1 cleanup-authoring marker (patch 1 — function-exit
	scope-drop migration).

	Placed by HIR→MIR at every source-scope-exit point that previously
	emitted inline `MoveOut + DropValue` pairs via
	`_emit_scope_drops(scope_index)` (function-exit, `lower_function_body`
	/ `lower_block` fall-through, lambda-block exits, and `HBreak` /
	`HContinue`).  Carries the candidate list the legacy emission
	would have considered: a sequence of `(local, ty)` pairs in
	legacy emission order (reversed scopes, reversed locals).

	Consumed by `lang/driftc/stage2/cleanup_authoring.py` after
	`build_ledger`: each candidate is queried via `verdict_at`, real
	`MoveOut + DropValue` pairs are emitted for `MUST_DROP` (and for
	`PathDependent` on variant types whose tag-0 destructor is a
	no-op — the same widening policy site 3 sub-step 3 introduced
	via `variant_zero_tag_drop_safe`).  The marker is removed after
	authoring; downstream passes (`drop_flags`, `string_arc`) see
	only the canonical drop sequences they already understand.

	`scope_id` is a per-function counter used for telemetry
	correlation only — the authoring pass does not consume it for
	emission decisions.
	"""
	scope_id: int
	# (local_name, type_id) pairs in legacy emission order:
	# reversed(scope_stack), then reversed(locals_in_each_scope).
	candidates: List[tuple]  # tuple[LocalId, TypeId]


@dataclass
class MatchCleanupHook(MInstr):
	"""Phase 4 site-2 per-field cleanup-authoring marker (patch 5).

	Placed by HIR→MIR at the per-arm partial-move cleanup point that
	legacy site 2 captured as `_cleanup_point` — immediately before
	the legacy `VariantGetFieldAddr + LoadRef + StoreLocal +
	arm-end MoveOut + DropValue` chain.

	HIR→MIR pre-allocates one `__match_partial_drop_N` local per
	surviving candidate (after the legacy `moved_field_indices` /
	`_needs_runtime_drop` filter), registers it via
	`_register_drop_local` so later site-1 CleanupHooks in the same
	scope see it as a candidate, then emits this hook carrying the
	`(drop_tmp_local, field_index, field_ty)` triples plus the
	arm-end program point.

	Consumed by `lang/driftc/stage2/match_cleanup_authoring.py` BEFORE
	`cleanup_authoring` (site 1) runs, with a ledger rebuild in
	between so site 1 sees the authored per-field transitions:

	  - For each candidate, `field_verdict_at((ctor, field_index),
	    ...)` is queried.  On MUST_DROP, the canonical chain is
	    authored: `VariantGetFieldAddr + LoadRef + StoreLocal(drop_tmp)`
	    at the hook position, `MoveOut(drop_tmp) + DropValue` at the
	    `arm_end_block / arm_end_index`.  On MUST_NOT_DROP or
	    PathDependent, NO chain is emitted; `drop_tmp` stays `UNINIT`
	    and site-1 hooks in the arm body see
	    `classify(UNINIT, needs_drop=True) = MUST_NOT_DROP` — no
	    spurious drop.

	Authority boundary for patch 5: HIR→MIR still decides the
	candidate SET (legacy filter), the ledger decides emit-vs-skip
	for carried candidates.  Broadening to full unfiltered
	consideration is a separate follow-up outside patch 5.
	"""
	scope_id: int
	arm_scrut_local: str
	arm_scrut_ptr_local: ValueId
	variant_ty: TypeId
	ctor: str
	# (drop_tmp_local, field_index, field_ty) triples in emission order.
	candidates: List[tuple]
	arm_end_block: str
	arm_end_index: int


@dataclass
class MoveOut(MInstr):
	"""dest = move local (read local, then reset storage to zero)."""
	dest: ValueId
	local: LocalId
	ty: TypeId


@dataclass
class StringFromInt(MInstr):
	"""
	dest = String(value)

	Converts an `Int` value to a `String` using the runtime's canonical formatting.
	This is used by f-string interpolation and other compiler-driven formatting.
	"""
	dest: ValueId
	value: ValueId


@dataclass
class StringFromBool(MInstr):
	"""
	dest = String(value)

	Converts a `Bool` value to a `String` (`"true"` / `"false"`).
	"""
	dest: ValueId
	value: ValueId


@dataclass
class StringFromUint(MInstr):
	"""
	dest = String(value)

	Converts a `Uint` value to a decimal `String`.
	"""
	dest: ValueId
	value: ValueId


@dataclass
class StringFromFloat(MInstr):
	"""
	dest = String(value)

	Converts a `Float` (`double`) value to a decimal `String` using the runtime's
	canonical formatting.

	This is used by f-string interpolation. The runtime implementation is
	deterministic (Ryu-based) so codegen does not depend on libc `snprintf`.
	"""
	dest: ValueId
	value: ValueId


@dataclass
class LoadLocal(MInstr):
	"""dest = locals[local]"""
	dest: ValueId
	local: LocalId


@dataclass
class AddrOfLocal(MInstr):
	"""
	dest = &locals[local] (address-taking).

	`is_mut` records whether the borrow was `&mut` at the surface level. LLVM
	lowering uses the same pointer representation for `&T` and `&mut T` (both are
	`ptr` in v1), but the type system and borrow checker need to preserve the
	distinction for mutability rules.
	"""
	dest: ValueId
	local: LocalId
	is_mut: bool = False


@dataclass
class AddrOfArrayElem(MInstr):
	"""
	dest = &array[index] (address of an array element).

	This is the MIR primitive backing `&arr[i]` / `&mut arr[i]`.

	Lowering responsibility:
	- Codegen must perform bounds checks when computing the element address, so
	  subsequent `LoadRef` / `StoreRef` do not need to re-check bounds.
	- `inner_ty` identifies the element type for typed pointer computation in
	  LLVM IR (`T*`).
	"""
	dest: ValueId
	array: ValueId
	index: ValueId
	inner_ty: TypeId
	is_mut: bool = False


@dataclass
class LoadRef(MInstr):
	"""
	dest = *ptr (load through a reference).

	This is the MIR-level primitive for reading via `&T` / `&mut T`.

	We keep `inner_ty` as a TypeId so downstream stages can:
	  - compute the correct LLVM element type for the `load`, and
	  - validate that dereference is only used on reference-typed values.
	"""
	dest: ValueId
	ptr: ValueId
	inner_ty: TypeId


@dataclass
class StoreRef(MInstr):
	"""
	*ptr = value (store through a mutable reference).

	This is the MIR-level primitive for `*p = v` where `p: &mut T`.
	`inner_ty` is the element TypeId for LLVM lowering and basic validation.
	"""
	ptr: ValueId
	value: ValueId
	inner_ty: TypeId


@dataclass
class MoveFromRef(MInstr):
	"""
	Atomic ownership transfer from `*ptr` into `local`.

	Three operations performed atomically by codegen:
	  1. Load the value at `ptr` (bitwise read of `*ptr`).
	  2. Tombstone `*ptr` (write drop-safe bytes via `_emit_tombstone_value`),
	     so any later `DropValue` on the source slot is a runtime no-op.
	  3. Transfer the loaded value into `local` (the local's storage
	     receives the bytes; ownership of the value moves with them).

	**The whole point**: this is a TRANSFER, not a Copy.  `string_arc`
	(and any other ownership-aware pass) must recognise `MoveFromRef`
	as moving the source's ownership stake into `local` — NO
	`StringRetain` / equivalent retain is inserted on this store.  The
	caller is responsible for releasing the transferred stake exactly
	once via a tail chain that drains `local` (typically
	`MoveOut(dest, local) + DropValue(dest)`, or `MoveOut(dest, local)`
	to surface the value as the expression's SSA result).

	**Why**: the per-field match-arm cleanup chain previously emitted
	`LoadRef + StoreLocal(drop_tmp, ...)`, which `string_arc.StoreLocal`
	expanded into `LoadRef + StringRetain(...) + StoreLocal(drop_tmp, ...)`
	— i.e. a Copy with retain.  The subsequent `DropValue` then released
	the retained stake, leaving the source slot's original stake
	un-released → leak.  `MoveFromRef` lets authoring express "this
	StoreLocal is a transfer" without a name-based or annotation-based
	hack on `StoreLocal`.

	**Callers (the slot-overwrite contract is per-call-site).**
	Every `MoveFromRef` caller is responsible for guaranteeing the
	tombstoned slot is never subsequently dropped (see Codegen
	contract below).  Three caller shapes exist today:
	  1. `match_cleanup_authoring`'s partial-move branch — pairs
	     `MoveFromRef(local=drop_tmp, ptr, T)` with arm-end
	     `MoveOut(dest, drop_tmp) + DropValue(dest)`.  The variant
	     scrutinee's whole-value `DropValue` is suppressed (per-field
	     cleanup IS the drop authority); the slot is only ever
	     re-read via the suppressed drop path, which is a no-op.
	  2. `IntrinsicKind.REPLACE` lowering in
	     `hir_to_mir.py::IntrinsicKind.REPLACE` — pairs
	     `MoveFromRef(local=__replace_old_*, ptr, T)` with an
	     immediate `StoreRef(ptr, new_val, T)` (overwriting the
	     tombstone with the replacement value before any drop can
	     reach it), then `MoveOut(old_val, __replace_old_*, T)` to
	     return the prior owner as the expression's SSA result.
	     Caller-side ordering: the replacement value is fully
	     lowered/consumed BEFORE the `MoveFromRef` mutates `*ptr`,
	     so an aborted replacement-expression lowering cannot leave
	     the slot tombstoned.
	  3. `_emit_assign_store_ref` in `hir_to_mir.py` — the
	     replace-store lowering used for every `&mut`-place
	     assignment whose `inner_ty` needs runtime drop (String,
	     Arc, Destructible struct, ...).  Pairs
	     `MoveFromRef(local=__assign_old_*, ptr, T)` with
	     `MoveOut(old_val, __assign_old_*, T) + DropValue(old_val) +
	     StoreRef(ptr, new_val, T)`.  Like REPLACE, the new value is
	     fully lowered/consumed by `_visit_stmt_HAssign` BEFORE the
	     helper runs, so self-referential RHS like
	     `ctx.s = ctx.s + "A"` is materialised in `new_val` before
	     the slot is tombstoned.  The legacy
	     `LoadRef + ZeroValue + StoreRef(zero) + DropValue` shape
	     this replaced double-released the old value via
	     `string_arc.py:1108-1121`'s StoreRef rewrite — see the
	     LANGUAGE_BUG carrier at
	     `lang/tests/memcheck/test_mut_struct_string_field_self_concat.py`
	     and the contract pin at
	     `lang/tests/stage2/test_assign_store_ref_drop_bearing_lowering.py`.

	**Codegen contract**:
	- Routes through `_emit_tombstone_value(inner_ty)` to produce the
	  tombstone bytes — same kind-by-kind dispatch (zero bytes for
	  SCALAR/ARRAY/INTERFACE/DV; tombstone tag for VARIANT; recursive
	  for STRUCT-without-user-destructor).
	- **No codegen-level guard for user-Destructible structs.**  Unlike
	  `TombstoneValue` (which produces drop-safe bytes for a slot that
	  WILL still get `DropValue`'d), `MoveFromRef` transfers ownership
	  AWAY from the slot — the safety contract is that callers
	  guarantee the tombstoned slot is never subsequently dropped.
	  For user-Destructible struct fields the tombstone bytes are NOT
	  drop-safe under that destructor, but each caller's own contract
	  must preclude the destructor from running on them: the
	  match-cleanup caller suppresses the whole-variant `DropValue`;
	  the REPLACE caller's `StoreRef` immediately overwrites the
	  tombstone before any drop can reach it.  Adding a codegen-level
	  guard would refuse the legitimate Token-field carrier
	  (`lang/tests/codegen/e2e/match_subset_bind_leaves_unbound_fields_dropped`).

	**Ledger contract** (`_apply_field_state` in `ownership_ledger.py`):
	- When `ptr` traces through a `VariantGetFieldAddr` to a tracked
	  named local, the variant field transitions to `MovedOut` AT this
	  instruction.  This is a parallel detection rule alongside the
	  legacy `LoadRef → StoreLocal → MoveOut(local)` chain (which
	  remains valid for HIRToMIR's binder-loop MOVE branch where the
	  binder name is used directly).
	"""
	local: LocalId
	ptr: ValueId
	inner_ty: TypeId


@dataclass
class StoreLocal(MInstr):
	"""locals[local] = value"""
	local: LocalId
	value: ValueId


@dataclass
class ConstructStruct(MInstr):
	"""
	dest = StructName(field0, field1, ...)

	This instruction constructs a struct value by positional field order.

	Design notes:
	- `struct_ty` is the nominal TypeId of the struct. Field names and field
	  types are looked up in the shared `TypeTable` downstream.
	- This is a pure value constructor (no allocation); it maps naturally to
	  LLVM `insertvalue` chains into an `undef` aggregate.
	"""

	dest: ValueId
	struct_ty: TypeId
	args: List[ValueId]


@dataclass
class ConstructVariant(MInstr):
	"""
	dest = Ctor(args...) for a variant value.

	`variant_ty` is the concrete instantiated variant TypeId (e.g. Optional<Int>).
	`ctor` is the constructor name (e.g. "Some", "None").

	Design notes:
	- Variants are *compiler-private ABI* in v1. Lowering/codegen treat the
	  shared `TypeTable`'s `VariantInstance` data as authoritative for:
	    - tag values (declaration order),
	    - field types and arity per constructor.
	- This instruction is pure value construction; it maps to building a struct
	  value in LLVM with tag + payload bytes.
	"""

	dest: ValueId
	variant_ty: TypeId
	ctor: str
	args: List[ValueId]


@dataclass
class VariantTag(MInstr):
	"""
	dest = tag(variant) as Uint (0..N-1).

	MIR exposes the tag as a `Uint` (usize) for simplicity; LLVM lowers the stored
	tag byte (i8) to a word-sized integer via zero-extension.
	"""

	dest: ValueId
	variant: ValueId
	variant_ty: TypeId


@dataclass
class VariantTagRef(MInstr):
	"""
	dest = tag(*variant_ref) as Uint (0..N-1), without materializing a by-value copy.

	Use this when scrutinee is borrowed; it avoids ownership-changing loads of
	non-Copy variants.
	"""

	dest: ValueId
	variant_ref: ValueId
	variant_ty: TypeId


@dataclass
class VariantGetField(MInstr):
	"""
	dest = variant.<ctor>.<field_index>

	Extract the value of a constructor field from a variant payload.

	Contract:
	- The caller must ensure `variant` currently holds the constructor `ctor`
	  (typically by checking `VariantTag`).
	- `field_ty` is carried for downstream codegen typing.
	"""

	dest: ValueId
	variant: ValueId
	variant_ty: TypeId
	ctor: str
	field_index: int
	field_ty: TypeId


@dataclass
class VariantGetFieldAddr(MInstr):
	"""
	dest = &variant_ref.<ctor>.<field_index>

	Project the address of a constructor field from a *referenced* variant.

	Contract:
	- `variant_ref` must be a reference/pointer to `variant_ty`.
	- The caller must ensure the active constructor is `ctor` (usually via tag check).
	- `field_ty` is the pointee type of the produced reference.
	"""

	dest: ValueId
	variant_ref: ValueId
	variant_ty: TypeId
	ctor: str
	field_index: int
	field_ty: TypeId


@dataclass
class StructGetField(MInstr):
	"""
	dest = subject.<field_index> (struct field read).

	We encode the field selection by index (not name) so MIR is independent of
	string-based name resolution once lowering has validated schemas.
	"""

	dest: ValueId
	subject: ValueId
	struct_ty: TypeId
	field_index: int
	field_ty: TypeId


@dataclass
class AddrOfField(MInstr):
	"""
	dest = &base_ptr.<field_index> (address of a struct field).

	This is the MIR primitive backing field borrows and field assignments via
	reference operations (`LoadRef` / `StoreRef`).

	Inputs:
	  - `base_ptr` must be a pointer to a struct value (`struct_ty*` in LLVM IR).
	  - `struct_ty` is the nominal TypeId of that struct.
	  - `field_index` selects the field by positional order.
	  - `field_ty` is the TypeId of the selected field (for typed pointer
	    computation downstream).

	`is_mut` records whether the originating borrow/assignment was mutable at the
	surface level; LLVM does not encode mutability, but the checker/borrow-checker
	do.
	"""

	dest: ValueId
	base_ptr: ValueId
	struct_ty: TypeId
	field_index: int
	field_ty: TypeId
	is_mut: bool = False


@dataclass
class LoadField(MInstr):
	"""dest = subject.field (struct field read)"""
	dest: ValueId
	subject: ValueId
	field: str


@dataclass
class StoreField(MInstr):
	"""subject.field = value (struct field write)"""
	subject: ValueId
	field: str
	value: ValueId


@dataclass
class LoadIndex(MInstr):
	"""dest = subject[index] (array/map-like read)"""
	dest: ValueId
	subject: ValueId
	index: ValueId


@dataclass
class StoreIndex(MInstr):
	"""subject[index] = value (array/map-like write)"""
	subject: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ConstArray(MInstr):
	"""dest = const array from compile-time scalar data.
	Codegen emits a read-only LLVM global + DriftArrayHeader."""
	dest: ValueId
	elem_ty: TypeId
	values: list  # Python scalars (int/float/bool)


@dataclass
class ArrayLit(MInstr):
	"""dest = Array literal of the given element type."""
	dest: ValueId
	elem_ty: TypeId
	elements: List[ValueId]


@dataclass
class ArrayAlloc(MInstr):
	"""
	dest = allocate Array buffer with len=0/cap and uninitialized elements.

	`length` is reserved for future use and must be zero in v1; callers must set
	the final length via ArraySetLen after initializing elements.
	"""
	dest: ValueId
	elem_ty: TypeId
	length: ValueId
	cap: ValueId


@dataclass
class ArrayElemInit(MInstr):
	"""array[index] = value (initialize uninitialized slot)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ArrayElemInitUnchecked(MInstr):
	"""array[index] = value (initialize slot without bounds checks)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ArrayElemAssign(MInstr):
	"""array[index] = value (drop old element, then init new)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ArrayElemDrop(MInstr):
	"""drop array[index] (destroy element in place)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId


@dataclass
class ArrayElemTake(MInstr):
	"""dest = take array[index] (move element out; slot becomes uninitialized)."""
	dest: ValueId
	elem_ty: TypeId
	array: ValueId
	index: ValueId


@dataclass
class ArrayDrop(MInstr):
	"""drop all elements and free array backing store."""
	elem_ty: TypeId
	array: ValueId


@dataclass
class ArrayDup(MInstr):
	"""dest = dup(array) with element-wise copy."""
	dest: ValueId
	elem_ty: TypeId
	array: ValueId


@dataclass
class ArrayIndexLoad(MInstr):
	"""dest = array[index] (typed array load)."""
	dest: ValueId
	elem_ty: TypeId
	array: ValueId
	index: ValueId


@dataclass
class ArrayIndexLoadUnchecked(MInstr):
	"""dest = array[index] (no bounds check)."""
	dest: ValueId
	elem_ty: TypeId
	array: ValueId
	index: ValueId


@dataclass
class ArrayIndexStore(MInstr):
	"""array[index] = value (typed array store)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ArraySetLen(MInstr):
	"""dest = array with updated len field."""
	dest: ValueId
	array: ValueId
	length: ValueId


@dataclass
class ArraySetGen(MInstr):
	"""dest = array with updated gen field."""
	dest: ValueId
	array: ValueId
	gen: ValueId


@dataclass
class ArrayLen(MInstr):
	"""dest = len(array) as Int."""
	dest: ValueId
	array: ValueId


@dataclass
class ArrayCap(MInstr):
	"""dest = cap(array) as Int."""
	dest: ValueId
	array: ValueId


@dataclass
class ArrayGen(MInstr):
	"""dest = gen(array) as Int."""
	dest: ValueId
	array: ValueId


@dataclass
class RawBufferAlloc(MInstr):
	"""dest = allocate RawBuffer for element type with given capacity."""
	dest: ValueId
	raw_ty: TypeId
	elem_ty: TypeId
	cap: ValueId


@dataclass
class RawBufferDealloc(MInstr):
	"""deallocate RawBuffer."""
	buffer: ValueId
	raw_ty: TypeId


@dataclass
class RawBufferPtrAt(MInstr):
	"""dest = &mut elem at index in RawBuffer (no bounds check)."""
	dest: ValueId
	buffer: ValueId
	raw_ty: TypeId
	elem_ty: TypeId
	index: ValueId


@dataclass
class RawBufferWrite(MInstr):
	"""write value into RawBuffer slot (no bounds check)."""
	buffer: ValueId
	raw_ty: TypeId
	elem_ty: TypeId
	index: ValueId
	value: ValueId


@dataclass
class RawBufferRead(MInstr):
	"""read value from RawBuffer slot (moves out, slot becomes uninit)."""
	dest: ValueId
	buffer: ValueId
	raw_ty: TypeId
	elem_ty: TypeId
	index: ValueId


@dataclass
class PtrFromRef(MInstr):
	"""dest = raw pointer from &T or &mut T."""
	dest: ValueId
	src: ValueId
	ptr_ty: TypeId


@dataclass
class PtrOffset(MInstr):
	"""dest = ptr + offset (element offset)."""
	dest: ValueId
	ptr: ValueId
	ptr_ty: TypeId
	elem_ty: TypeId
	offset: ValueId


@dataclass
class PtrRead(MInstr):
	"""dest = *ptr (raw read, no drop)."""
	dest: ValueId
	ptr: ValueId
	elem_ty: TypeId


@dataclass
class PtrWrite(MInstr):
	"""*ptr = value (raw write)."""
	ptr: ValueId
	value: ValueId
	elem_ty: TypeId


@dataclass
class PtrIsNull(MInstr):
	"""dest = (ptr == null)."""
	dest: ValueId
	ptr: ValueId
	ptr_ty: TypeId


@dataclass
class PtrAsMutRef(MInstr):
	"""dest = ptr as &mut T (interior mutability cast)."""
	dest: ValueId
	src: ValueId
	ref_ty: TypeId


@dataclass
class StringLen(MInstr):
	"""dest = len(string) as Int."""
	dest: ValueId
	value: ValueId


@dataclass
class StringByteAt(MInstr):
	"""dest = string_byte_at(value, index) as Byte."""
	dest: ValueId
	value: ValueId
	index: ValueId


@dataclass
class StringEq(MInstr):
	"""dest = (left == right) for strings; result is Bool."""
	dest: ValueId
	left: ValueId
	right: ValueId


@dataclass
class StringCmp(MInstr):
	"""
	dest = string_cmp(left, right) (Int).

	This is a deterministic, locale-independent lexicographic comparison of the
	underlying UTF-8 byte sequences (unsigned byte ordering).

	Contract:
	  - dest < 0 if left < right
	  - dest == 0 if left == right
	  - dest > 0 if left > right
	"""

	dest: ValueId
	left: ValueId
	right: ValueId


@dataclass
class StringConcat(MInstr):
	"""dest = left + right for strings."""
	dest: ValueId
	left: ValueId
	right: ValueId


@dataclass
class AssertLoc(MInstr):
	cond: ValueId
	file: ValueId
	line: ValueId
	expr: ValueId
	msg: ValueId


@dataclass
class Call(MInstr):
	"""
	dest = fn(args...) (plain function call; dest may be None for void returns).
	"""
	dest: Optional[ValueId]  # None for void calls
	fn_id: FunctionId
	args: List[ValueId]
	can_throw: bool


@dataclass
class CallIndirect(MInstr):
	"""
	dest = callee(args...) via a function value (dest may be None for void returns).
	"""
	dest: Optional[ValueId]  # None for void calls
	callee: ValueId
	args: List[ValueId]
	param_types: List[TypeId]
	user_ret_type: TypeId
	can_throw: bool


@dataclass
class CallIface(MInstr):
	"""
	dest = iface.call(args...) via interface vtable (dest may be None for void returns).
	"""
	dest: Optional[ValueId]
	iface: ValueId
	args: List[ValueId]
	param_types: List[TypeId]
	user_ret_type: TypeId
	can_throw: bool
	slot_index: int


# Slice 7c-2 (ABI 14, 2026-05-06): `ConstructDV`, `ErrorAddAttrDV`,
# `ErrorAddLocalDV`, `ErrorAttrsGetDV`, `ErrorCapturesGetDV`,
# `DVAs{Int,Bool,Float,String,Object}`, `DVKind`, `DVIndex`,
# `DVLen`, `DVEntries`, `DVGetField` MIR ops are deleted along
# with `H.HDVInit` and the runtime DV exports.  Slice 7c-1 retired
# the runtime/codegen wire; this slice retires the compiler-
# internal substrate.


@dataclass
class ConstructError(MInstr):
	"""Construct an Error value from an event code.

	`code` is the 64-bit event code (see drift-abi-exceptions).
	`event_fqn` is the canonical FQN label (for logging/telemetry;
	not used for matching).  Slice 7c-1 (ABI 14) retired the
	`payload` / `attr_key` legacy DV-attachment shape — at ABI 14
	the only valid form is `payload=None, attr_key=None` and
	params flow through `ExcSetParamsJson`.  Fields kept for
	backward source compat at the dataclass level; codegen ICEs
	if either is non-None.
	"""
	dest: ValueId
	code: ValueId
	event_fqn: ValueId
	payload: ValueId | None
	attr_key: ValueId | None


@dataclass
class ErrorRaise(MInstr):
	"""Raise an error via drift_error_raise (noreturn). Used in nothrow context."""
	error: ValueId


@dataclass
class ConstructResultOk(MInstr):
	"""
	Construct FnResult.Ok(value).

	In the surface language, functions may be "can-throw" (exceptional control
	flow) while still declaring `-> T`. Internally, the compiler lowers
	can-throw functions to return `FnResult<T, Error>`.

	For `T = Void`, there is no surface value to carry. In that case `value`
	must be `None` and codegen will synthesize a dummy ok payload in the
	internal ABI slot.
	"""
	dest: ValueId
	value: ValueId | None


@dataclass
class ConstructResultErr(MInstr):
	"""Construct FnResult.Err(error)."""
	dest: ValueId
	error: ValueId


@dataclass
class ResultIsErr(MInstr):
	"""dest = result.is_err (Bool)."""

	dest: ValueId
	result: ValueId


@dataclass
class ResultOk(MInstr):
	"""dest = result.ok (undefined if result is Err)."""

	dest: ValueId
	result: ValueId


@dataclass
class ResultErr(MInstr):
	"""dest = result.err (Error handle; undefined if result is Ok)."""

	dest: ValueId
	result: ValueId


@dataclass
class ExcGetParamsJson(MInstr):
	"""dest = drift_error_get_params_json(error) (returns retained
	canonical JSON String; caller owns and releases).

	Phase 1+ DV→JSON migration — used by `<error>.params` field-access
	lowering to retrieve the stored canonical JSON document for the
	`ErrorParamsView` constructor.  See ABI spec §2.3.
	"""

	dest: ValueId
	error: ValueId


@dataclass
class ExcSetParamsJson(MInstr):
	"""drift_error_set_params_json(error, json_text) — runtime takes
	ownership of `json_text`; replacement releases the prior value.

	Phase 1+ DV→JSON migration — emitted by throw-lowering after the
	canonical-params JSON String is built.  See ABI spec §2.3.
	"""

	error: ValueId
	json_text: ValueId


@dataclass
class ExcGetContextJson(MInstr):
	"""dest = drift_error_get_context_json(error) (returns retained
	canonical context JSON array String; caller owns and releases).

	Slice 2 DV→JSON — used by `<error>.context` field-access lowering
	to retrieve the stored canonical JSON array document for the
	`ErrorContextView` constructor.  See ABI spec §2.3.
	"""

	dest: ValueId
	error: ValueId


@dataclass
class ExcAppendContextFrame(MInstr):
	"""drift_error_append_context_frame(error, frame_json) — runtime
	takes ownership of `frame_json` and rebuilds an owned
	`context_json` with the frame appended.  Frame bytes preserved
	verbatim inside the merged array (ABI §2.2 fastpath guarantee).

	Slice 2 DV→JSON — emitted by `^`-capture unwind lowering for
	each function frame on the throw path that has captured locals.
	Frame ordering is innermost-first (matches unwind observation
	order).  See ABI spec §2.3.
	"""

	error: ValueId
	frame_json: ValueId


# Slice 7c-2 (ABI 14, 2026-05-06): DVAs* / DVKind / DVIndex /
# DVLen / DVEntries / DVGetField MIR ops deleted alongside the
# `drift_dv_*` runtime exports they wrapped.


@dataclass
class ErrorEvent(MInstr):
	"""
	Project the event code from an Error value.

	The concrete layout is defined by the runtime ABI; this just captures the
	"extract the event code" operation so later passes (catch/dispatch) can use
	it without knowing the Error struct shape here.
	"""

	dest: ValueId
	error: ValueId


@dataclass
class ErrorEventFqn(MInstr):
	"""Project the canonical event FQN String from an Error value.

	Mirrors `ErrorEvent` (which extracts field 0 / event_code).  Codegen
	loads the Error struct, extracts field 1 (the event_fqn DriftString),
	and retains the result so `dest` is an OWNED String the caller is
	responsible for releasing.

	Slice 3 DV→JSON migration — used by `<Error>.encode_compact()`
	envelope assembly to splice the (JSON-quoted) event FQN into the
	canonical envelope.  No new runtime surface; reuses
	`drift_string_retain` which is already in the runtime.
	"""

	dest: ValueId
	error: ValueId


@dataclass
class UnaryOpInstr(MInstr):
	"""dest = op operand (unary numeric/logical/bit ops)."""
	dest: ValueId
	op: UnaryOp
	operand: ValueId


@dataclass
class BinaryOpInstr(MInstr):
	"""dest = left op right (binary numeric/logical/bit ops).

	`signed` records operand signedness for narrow fixed-width integer operands
	(`Int32` = True, `Uint32` = False) whose LLVM type (`i32`) does not by itself
	encode signedness — codegen needs it to select signed (`icmp s*`) vs unsigned
	(`icmp u*`) ordering comparisons.  `None` means "not provided / not a narrow
	int" (the `i64` Int/Uint path encodes signedness in its own value-type tag,
	and equality comparisons are signedness-agnostic)."""
	dest: ValueId
	op: BinaryOp
	left: ValueId
	right: ValueId
	signed: Optional[bool] = None


@dataclass
class WrappingAddU64(MInstr):
	"""dest = wrapping_add_u64(left, right)."""
	dest: ValueId
	left: ValueId
	right: ValueId


@dataclass
class WrappingMulU64(MInstr):
	"""dest = wrapping_mul_u64(left, right)."""
	dest: ValueId
	left: ValueId
	right: ValueId


@dataclass
class AssignSSA(MInstr):
	"""
	SSA move/copy used during SSA construction.

	This is introduced by the SSA pass when rewriting LoadLocal/StoreLocal into
	pure SSA value flow. It carries explicit dest/src ValueIds.
	"""

	dest: ValueId
	src: ValueId


@dataclass
class Phi(MInstr):
	"""Phi node (added/used during SSA construction)."""
	dest: ValueId
	incoming: Dict[str, ValueId]  # block name -> value


# Terminators

@dataclass
class Goto(MTerminator):
	"""Unconditional branch to another basic block."""
	target: str

	def successors(self) -> "list[str]":
		return [self.target]

	def successor_edges(self) -> "list[tuple[str, str]]":
		return [(self.target, "goto")]

	def value_uses(self) -> "list[str]":
		return []

	def remap_targets(self, mapping: "dict[str, str]") -> None:
		self.target = mapping.get(self.target, self.target)

	def redirect_edge(self, edge_label: str, new_target: str) -> None:
		if edge_label != "goto":
			raise AssertionError(f"Goto has no edge {edge_label!r}")
		self.target = new_target


@dataclass
class IfTerminator(MTerminator):
	"""Conditional branch to then/else blocks."""
	cond: ValueId
	then_target: str
	else_target: str

	def successors(self) -> "list[str]":
		return [self.then_target, self.else_target]

	def successor_edges(self) -> "list[tuple[str, str]]":
		return [(self.then_target, "if_then"), (self.else_target, "if_else")]

	def value_uses(self) -> "list[str]":
		return [self.cond]

	def remap_targets(self, mapping: "dict[str, str]") -> None:
		self.then_target = mapping.get(self.then_target, self.then_target)
		self.else_target = mapping.get(self.else_target, self.else_target)

	def redirect_edge(self, edge_label: str, new_target: str) -> None:
		if edge_label == "if_then":
			self.then_target = new_target
		elif edge_label == "if_else":
			self.else_target = new_target
		else:
			raise AssertionError(f"IfTerminator has no edge {edge_label!r}")


@dataclass
class Return(MTerminator):
	"""Function return with optional value."""
	value: Optional[ValueId]

	def successors(self) -> "list[str]":
		return []

	def successor_edges(self) -> "list[tuple[str, str]]":
		return []

	def value_uses(self) -> "list[str]":
		return [self.value] if self.value is not None else []

	def remap_targets(self, mapping: "dict[str, str]") -> None:
		return None

	def redirect_edge(self, edge_label: str, new_target: str) -> None:
		raise AssertionError(f"Return has no edge {edge_label!r}")


@dataclass
class Unreachable(MTerminator):
	"""
	Terminator for an unreachable control-flow path.

	This is used as a defensive invariant marker when earlier stages guarantee
	that a path cannot be taken (e.g., "uncaught error reaches a non-can-throw
	function"). Lowering should not crash the compiler in these cases; instead
	we encode the invariant into MIR and let LLVM emit `unreachable`.
	"""

	def successors(self) -> "list[str]":
		return []

	def successor_edges(self) -> "list[tuple[str, str]]":
		return []

	def value_uses(self) -> "list[str]":
		return []

	def remap_targets(self, mapping: "dict[str, str]") -> None:
		return None

	def redirect_edge(self, edge_label: str, new_target: str) -> None:
		raise AssertionError(f"Unreachable has no edge {edge_label!r}")


@dataclass
class SwitchTerminator(MTerminator):
	"""Multi-way branch on an integer scrutinee (scalar `match` lowering).

	Compares `scrutinee` against each case value (exact equality, by bit pattern —
	signedness does not matter; `cases` values are the checker-validated canonical
	signed ints) and branches to the matching case block, or to `default_target`
	if none match.  `cases` is in source order.  Lowers to an LLVM `switch`.
	"""
	scrutinee: ValueId
	cases: List[tuple]  # list[tuple[int case_value, str target_block]], source order
	default_target: str

	def successors(self) -> "list[str]":
		# Case targets in source order, then the default.
		return [t for (_v, t) in self.cases] + [self.default_target]

	def successor_edges(self) -> "list[tuple[str, str]]":
		# Labels are INDEX-based (`switch_case:<i>`), not value-based, so each
		# outgoing edge has a unique identity even if two cases shared a value or a
		# target (malformed / future MIR).  Index order matches `successors()`.
		return [(t, f"switch_case:{i}") for i, (_v, t) in enumerate(self.cases)] + [
			(self.default_target, "switch_default")
		]

	def value_uses(self) -> "list[str]":
		return [self.scrutinee]

	def remap_targets(self, mapping: "dict[str, str]") -> None:
		self.cases = [(v, mapping.get(t, t)) for (v, t) in self.cases]
		self.default_target = mapping.get(self.default_target, self.default_target)

	def redirect_edge(self, edge_label: str, new_target: str) -> None:
		if edge_label == "switch_default":
			self.default_target = new_target
			return
		if edge_label.startswith("switch_case:"):
			idx = int(edge_label[len("switch_case:"):])
			v, _t = self.cases[idx]
			self.cases[idx] = (v, new_target)
			return
		raise AssertionError(f"SwitchTerminator has no edge {edge_label!r}")



# Containers

@dataclass
class BasicBlock:
	"""
	Basic block: a list of instructions followed by a single terminator.

	No control flow leaves this block except via the terminator.
	"""
	name: str
	instructions: List[MInstr] = field(default_factory=list)
	terminator: Optional[MTerminator] = None


@dataclass
class MirFunc:
	"""
	MIR function: collection of blocks plus parameter/local declarations.

	Blocks are stored in a dict keyed by block name; `entry` names the entry block.
	"""
	name: str
	params: List[LocalId]
	locals: List[LocalId]
	fn_id: FunctionId
	blocks: Dict[str, BasicBlock] = field(default_factory=dict)
	entry: str = "entry"
	local_types: Dict[str, TypeId] = field(default_factory=dict)
	debug_local_names: Dict[str, str] = field(default_factory=dict)
	# Per-param drop status recorded by HIR-to-MIR lowering.
	# Keys are param names. Values are one of:
	#   "scope_exit_drop"     — _emit_scope_drops will emit MoveOut+DropValue
	#   "forwarded_to_callee" — ownership transferred (move into call)
	#   "moved"               — consumed by user-level move expression
	#   "no_drop"             — Copy type or has_drop=False at lowering time
	# Empty dict means status was not recorded (e.g. synthesized wrappers).
	param_drop_status: Dict[str, str] = field(default_factory=dict)

	def __post_init__(self) -> None:
		if self.name != function_symbol(self.fn_id):
			raise AssertionError(
				f"MirFunc name '{self.name}' must match fn_id symbol '{function_symbol(self.fn_id)}'"
			)


__all__ = [
	"MNode",
	"MInstr",
	"MTerminator",
	"UnaryOp",
	"BinaryOp",
	"ConstInt",
	"ConstUint",
	"ConstUint64",
	"ConstByte",
	"IntFromUint",
	"UintFromInt",
	"CastScalar",
	"ConstBool",
	"ConstVoid",
	"ConstString",
	"ConstFloat",
	"FnPtrConst",
	"ConstructIface",
	"ConstructIfaceValue",
	"ConstructIfaceBorrowed",
	"IfaceUpcast",
	"ZeroValue",
	"StringRetain",
	"StringRelease",
	"CopyValue",
	"DropValue",
	"MoveOut",
	"StringFromInt",
	"StringFromBool",
	"StringFromUint",
	"StringFromFloat",
	"StringLen",
	"StringByteAt",
	"StringEq",
	"StringCmp",
	"StringConcat",
	"AssertLoc",
	"LoadLocal",
	"AddrOfLocal",
	"AddrOfArrayElem",
	"LoadRef",
	"StoreRef",
	"StoreLocal",
	"ConstructStruct",
	"ConstructVariant",
	"VariantTag",
	"VariantTagRef",
	"VariantGetField",
	"VariantGetFieldAddr",
	"StructGetField",
	"AddrOfField",
	"LoadField",
	"StoreField",
	"LoadIndex",
	"StoreIndex",
	"ConstArray",
	"ArrayLit",
	"ArrayAlloc",
	"ArrayElemInit",
	"ArrayElemInitUnchecked",
	"ArrayElemAssign",
	"ArrayElemDrop",
	"ArrayElemTake",
	"ArrayDrop",
	"ArrayDup",
	"ArrayIndexLoad",
	"ArrayIndexLoadUnchecked",
	"ArrayIndexStore",
	"ArraySetLen",
	"ArrayLen",
	"ArrayCap",
	"ArrayGen",
	"RawBufferAlloc",
	"RawBufferDealloc",
	"RawBufferPtrAt",
	"RawBufferWrite",
	"RawBufferRead",
	"PtrFromRef",
	"PtrOffset",
	"PtrRead",
	"PtrWrite",
	"PtrIsNull",
	"Call",
	"CallIndirect",
	"CallIface",
	"ConstructError",
	"ErrorRaise",
	"ConstructResultOk",
	"ConstructResultErr",
	"ResultIsErr",
	"ResultOk",
	"ResultErr",
	"ExcGetParamsJson",
	"ExcSetParamsJson",
	"ExcGetContextJson",
	"ExcAppendContextFrame",
	"ErrorEvent",
	"ErrorEventFqn",
	"UnaryOpInstr",
	"BinaryOpInstr",
	"WrappingAddU64",
	"WrappingMulU64",
	"Phi",
	"Goto",
	"IfTerminator",
	"Return",
	"Unreachable",
	"BasicBlock",
	"MirFunc",
]
