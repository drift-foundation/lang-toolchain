# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B2+C frozen decision plan — anchor-lifecycle container.

The string-arc endgame's combined B2+C chunk computes ONE immutable
per-function decision plan from the ORIGINAL MIR and the fresh
`rebuild_after_cleanup_authoring` ledger (ledger A), BEFORE any B2/C
mutation, then lets narrow emitters consume it. Architecture decided
in the B2+C R6 architectural review (0.33.86-0.33.87 series; see the
string_arc-endgame entries in doc/history.md).

This module is ONLY the anchor-lifecycle container + its fail-closed
contract. Site-specific decision COMPUTATION (S2) and EMISSION
(S3/S4/S5) live elsewhere; the plan carries opaque `payload`s (which
S2 must supply as immutable/frozen site data).

Lifecycle
---------
1. BUILD: `add(...)` registers a decision against an original anchor
   object. Registration records the immutable proof coordinate
   `(block, original_index, kind)` and a semantic-field snapshot.
2. FINALIZE: `validate_and_freeze(func)` proves every anchor actually
   occupies its recorded coordinate in `func` (INSTR at
   `instructions[orig_index]`, TERM as `block.terminator` with
   `orig_index == len(instructions)`), that no two distinct objects
   collide on one coordinate, and that objects shared across sites carry
   identical coordinates + compatible field snapshots. Consumption is
   forbidden until this succeeds.
3. CONSUME: `open_session(func)` scans the (possibly mutated) function
   ONCE, building an identity→location index and validating anchor
   relative order — `O(MIR + decisions)`, not `O(decisions × MIR)`.
   `session.locate(dec)` is then `O(1)`. `mark_consumed` / consumption
   state is PLAN-PRIVATE (keyed by a plan-owned token), so a caller
   cannot forge consumption by mutating a decision. `assert_all_consumed`
   closes the bijection across every emitter phase.

Plan-time proof coordinate vs consumption-time location
-------------------------------------------------------
`(block, original_index)` is the IMMUTABLE PROOF COORDINATE for the
ledger-A query — validated at BUILD/FINALIZE and never re-required to
equal the numerical index afterward. Return emissions and earlier
overwrite emissions legitimately shift current indices, so CONSUMPTION
is object-identity based: exact object present EXACTLY ONCE, in the SAME
block, semantic fields unchanged, relative order preserved. A changed
current numerical index is ALLOWED; everything else fails closed via
`PlanContractError`.

No dynamic MIR attributes: the plan keys on `id(obj)` held in plan-owned
tables, never a `setattr` on the instruction (the transient-attribute
anti-pattern retired in B1 cleanup debt #2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from . import mir_nodes as M


class PlanContractError(AssertionError):
	"""A frozen-plan invariant was violated. Subclasses AssertionError so
	the driver's boundary-containment (`_append_boundary_contract_diag`)
	surfaces it as a clean `internal:` diagnostic rather than a traceback.
	(Containment is proven end-to-end once driver wiring lands — S3/S4/S5.)
	"""


# Anchor kinds.
ANCHOR_INSTR = "instr"   # anchored to a MInstr in block.instructions
ANCHOR_TERM = "term"     # anchored to a block.terminator (e.g. Return)
_VALID_KINDS = (ANCHOR_INSTR, ANCHOR_TERM)


@dataclass(frozen=True)
class AnchorCoord:
	"""The immutable proof coordinate, fixed at plan-build time.

	`orig_index` for an INSTR anchor is its index in the original
	`block.instructions`; for a TERM anchor it is
	`len(original block.instructions)` — the end-of-block point the
	site-3 ledger query uses (`(block, len(instructions))`)."""
	block: str
	orig_index: int
	anchor_kind: str  # ANCHOR_INSTR | ANCHOR_TERM


@dataclass(frozen=True)
class Decision:
	"""One planned emission, bound to an original anchor object.

	Immutable: a frozen dataclass with a tuple `fields` snapshot, so a
	caller cannot rewrite the coordinate, site, kind, fields, or payload,
	and cannot forge consumption (consumption state is plan-private,
	keyed by `token`). `payload` must be supplied immutable by S2.
	"""
	token: int                       # plan-assigned identity within its plan
	coord: AnchorCoord
	obj: Any                         # the exact MInstr / MTerminator (identity)
	site: str                        # "site3" | "site4" | "nullsafe" | "r3" | "r4" | "r8"
	kind_name: str                   # type(obj).__name__ — expected node kind
	fields: Tuple[Tuple[str, Any], ...]   # expected semantic operands/fields, immutable
	# Immutable `local -> expected TypeId` snapshot, checked against the
	# (mutable) `func.local_types` at BOTH validate_and_freeze and locate, so
	# planned type relationships are ENFORCED, not merely carried.
	type_bindings: Tuple[Tuple[str, Any], ...]
	payload: Any                     # site-specific emission data (S2 supplies immutable)


class ConsumptionSession:
	"""One PHASE-SCOPED consumption pass over the CURRENT function.

	Constructed by `CleanupPlan.open_session(func)`: scans the function
	ONCE (`scan_count == 1`), building an identity→location index and
	per-object occurrence counts, and validates anchor relative order.
	`locate(dec)` / `consume(dec)` are then O(1). One session serves
	arbitrarily many calls without rescanning — proven by `scan_count`.

	The session is STALE-SAFE: every `locate`/`consume` re-confirms (O(1))
	that the anchor object is STILL at its scanned location in the current
	MIR, so a session opened before a mutation cannot validate/consume an
	anchor that the mutation moved or removed.

	PRODUCTION EMITTERS MUST NOT call `consume()` directly. `consume()`
	marks a decision consumed against the CURRENT (pre-rewrite) view, which
	a later rewrite in the same phase can invalidate. Production must use
	`CleanupPlan.begin_phase(func)` — preflight `stage()` → rewrite →
	`mark_rewritten()` → fresh postflight `commit()` — which re-validates
	against the MUTATED MIR before marking anything consumed. A fail-closed
	AST pin (`test_production_consumes_via_emitter_phase_not_session_bypass`)
	forbids production modules from calling `consume`/`_mark_consumed`.
	`consume()`/`locate()` remain for low-level/test use and for
	`EmitterPhase`'s own read-validation:

	    phase = plan.begin_phase(func)             # preflight session
	    for dec in decisions: phase.stage(dec)
	    ... rewrite (preserve anchor objects, incl. the Return) ...
	    phase.mark_rewritten(); phase.commit()     # postflight validate + consume
	"""

	def __init__(self, plan: "CleanupPlan", func: "M.MirFunc") -> None:
		self._plan = plan
		self._func = func
		self._open = True
		self.scan_count = 0
		# id(obj) -> (block_name, index, is_terminator)
		self._loc: Dict[int, Tuple[str, int, bool]] = {}
		# id(obj) -> total occurrences across the whole function
		self._count: Dict[int, int] = {}
		self._scan()
		self._validate_relative_order()

	# context-manager: a session is a phase; leaving the phase closes it.
	def __enter__(self) -> "ConsumptionSession":
		return self

	def __exit__(self, *exc: Any) -> None:
		self.close()

	def close(self) -> None:
		self._open = False

	def _require_open(self) -> None:
		if not self._open:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: session is closed; "
				f"open a fresh session against the current MIR"
			)

	def _scan(self) -> None:
		self.scan_count += 1
		loc = self._loc
		count = self._count
		for bname, blk in self._func.blocks.items():
			for i, instr in enumerate(blk.instructions):
				oid = id(instr)
				count[oid] = count.get(oid, 0) + 1
				loc[oid] = (bname, i, False)
			term = blk.terminator
			if term is not None:
				oid = id(term)
				count[oid] = count.get(oid, 0) + 1
				loc[oid] = (bname, len(blk.instructions), True)

	def _validate_relative_order(self) -> None:
		# For each block, INSTR anchors must appear in current index order
		# matching their orig_index order (non-anchor insertions shift but
		# never reorder; a moved/reordered anchor is caught here).
		by_block: Dict[str, List[Decision]] = {}
		for dec in self._plan._decisions:
			if dec.coord.anchor_kind != ANCHOR_INSTR:
				continue
			by_block.setdefault(dec.coord.block, []).append(dec)
		for bname, decs in by_block.items():
			ordered = sorted(decs, key=lambda d: d.coord.orig_index)
			last = -1
			for dec in ordered:
				entry = self._loc.get(id(dec.obj))
				if entry is None or entry[0] != bname:
					raise PlanContractError(
						f"cleanup_plan[{self._plan._fn_name}]: anchor for site "
						f"{dec.site!r} missing from block {bname!r} during "
						f"relative-order check"
					)
				ci = entry[1]
				if ci <= last:
					raise PlanContractError(
						f"cleanup_plan[{self._plan._fn_name}]: anchor relative "
						f"order changed in block {bname!r} (site {dec.site!r} "
						f"now at index {ci}, expected after {last})"
					)
				last = ci

	def locate(self, dec: "Decision") -> int:
		"""O(1) validated location of `dec`'s anchor in the current
		function. Returns the CURRENT index (INSTR: instruction index;
		TERM: end-of-block index). Fails closed on: closed/stale session,
		foreign decision, disappearance/duplication/movement, kind change,
		field drift, or the anchor no longer being at its scanned location
		(a mutation since scan)."""
		self._require_open()
		self._plan._require_owned(dec)
		oid = id(dec.obj)

		if type(dec.obj).__name__ != dec.kind_name:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: anchor object kind "
				f"changed ({dec.kind_name} → {type(dec.obj).__name__}) for "
				f"site {dec.site!r} at {dec.coord.block}"
			)
		for name, expected in dec.fields:
			actual = getattr(dec.obj, name, _MISSING)
			if actual != expected:
				raise PlanContractError(
					f"cleanup_plan[{self._plan._fn_name}]: anchor field "
					f"{name!r} changed ({expected!r} → {actual!r}) for site "
					f"{dec.site!r} at {dec.coord.block}"
				)
		# Type bindings (`local -> expected TypeId`) re-checked against the
		# current `func.local_types` — catches post-freeze type drift.
		self._plan._check_type_bindings(dec, self._func, when="consume")

		entry = self._loc.get(oid)
		total = self._count.get(oid, 0)
		if entry is None or total != 1:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: anchor object for site "
				f"{dec.site!r} at {dec.coord.block}:{dec.coord.orig_index} "
				f"must be present exactly once; found {total} occurrence(s) "
				f"(disappearance/duplication/movement fails closed)"
			)
		bname, index, is_term = entry
		if bname != dec.coord.block:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: anchor for site "
				f"{dec.site!r} moved from block {dec.coord.block!r} to "
				f"{bname!r}"
			)
		if dec.coord.anchor_kind == ANCHOR_TERM and not is_term:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: TERM anchor for site "
				f"{dec.site!r} is no longer the block terminator"
			)
		if dec.coord.anchor_kind == ANCHOR_INSTR and is_term:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: INSTR anchor for site "
				f"{dec.site!r} became a terminator"
			)

		# STALE-SAFE re-confirmation against the CURRENT MIR (O(1)): the
		# object must STILL be exactly at its scanned location. If the
		# function mutated since this session's scan (indices shifted, the
		# anchor removed/moved), this fails — forcing a fresh session
		# rather than trusting stale scan data.
		block = self._func.blocks.get(bname)
		if block is None:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: stale session — block "
				f"{bname!r} vanished since scan (site {dec.site!r})"
			)
		if is_term:
			if block.terminator is not dec.obj:
				raise PlanContractError(
					f"cleanup_plan[{self._plan._fn_name}]: stale session — "
					f"terminator of {bname!r} changed since scan (site "
					f"{dec.site!r}); reopen a session"
				)
			return len(block.instructions)
		if not (0 <= index < len(block.instructions)
		        and block.instructions[index] is dec.obj):
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: stale session — anchor "
				f"for site {dec.site!r} is no longer at {bname}:{index} "
				f"(the MIR mutated since this session's scan); reopen a session"
			)
		return index

	def consume(self, dec: "Decision") -> int:
		"""Validate `dec`'s anchor against the current MIR (via `locate`)
		and mark it consumed. Returns the current index. Consumption is
		ONLY available through a validated session — there is no way to
		mark a decision consumed without its anchor being located first."""
		index = self.locate(dec)
		self._plan._mark_consumed(dec)
		return index


_MISSING = object()


class CleanupPlan:
	"""Per-function frozen decision plan with the anchor-lifecycle contract."""

	def __init__(self, fn_name: str) -> None:
		self._fn_name = fn_name
		self._decisions: List[Decision] = []
		self._tokens: Dict[int, Decision] = {}          # token -> authoritative Decision
		self._by_site_obj: Dict[Tuple[str, int], Decision] = {}   # (site, id(obj)) -> Decision
		# id(obj) -> (coord, kind_name, fields-dict) for cross-site + collision checks
		self._obj_meta: Dict[int, Tuple[AnchorCoord, str, Dict[str, Any]]] = {}
		# (block, orig_index, kind) -> id(obj), to reject two objects at one coord
		self._coord_owner: Dict[Tuple[str, int, str], int] = {}
		self._frozen = False
		self._next_token = 0
		self._consumed: set[int] = set()   # PLAN-PRIVATE consumption state (tokens)

	# ---- build phase -------------------------------------------------

	def add(
		self,
		*,
		obj: Any,
		coord: AnchorCoord,
		site: str,
		fields: Dict[str, Any],
		payload: Any,
		type_bindings: "Dict[str, Any] | None" = None,
	) -> Decision:
		"""Register a decision against an original anchor object.

		Immediate build-time checks: not-frozen; valid anchor kind;
		no duplicate (site, object); no two distinct objects at one
		`(block, orig_index, kind)`; objects shared across sites carry
		identical coord/kind and compatible (equal-on-overlap) field
		snapshots. Full occupancy validation is in `validate_and_freeze`.
		"""
		if self._frozen:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: add() after freeze()"
			)
		if coord.anchor_kind not in _VALID_KINDS:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: invalid anchor_kind "
				f"{coord.anchor_kind!r} (site {site!r})"
			)
		site_key = (site, id(obj))
		if site_key in self._by_site_obj:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: duplicate registration of "
				f"site {site!r} against the same object at {coord.block}:"
				f"{coord.orig_index}"
			)

		coord_key = (coord.block, coord.orig_index, coord.anchor_kind)
		existing_owner = self._coord_owner.get(coord_key)
		if existing_owner is not None and existing_owner != id(obj):
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: coordinate collision — two "
				f"distinct objects claim {coord.block}:{coord.orig_index}"
				f"/{coord.anchor_kind} (site {site!r})"
			)

		fields_dict = dict(fields)
		prior = self._obj_meta.get(id(obj))
		if prior is not None:
			p_coord, p_kind, p_fields = prior
			if p_coord != coord or p_kind != type(obj).__name__:
				raise PlanContractError(
					f"cleanup_plan[{self._fn_name}]: object shared across sites "
					f"has inconsistent coordinate/kind (site {site!r}: "
					f"{coord.block}:{coord.orig_index}/{coord.anchor_kind} vs "
					f"{p_coord.block}:{p_coord.orig_index}/{p_coord.anchor_kind})"
				)
			for k, v in fields_dict.items():
				if k in p_fields and p_fields[k] != v:
					raise PlanContractError(
						f"cleanup_plan[{self._fn_name}]: object shared across "
						f"sites has conflicting field {k!r} ({p_fields[k]!r} vs "
						f"{v!r}) at {coord.block}:{coord.orig_index}"
					)

		token = self._next_token
		self._next_token += 1
		dec = Decision(
			token=token,
			coord=coord,
			obj=obj,
			site=site,
			kind_name=type(obj).__name__,
			fields=tuple(sorted(fields_dict.items())),
			type_bindings=tuple(sorted((type_bindings or {}).items())),
			payload=payload,
		)
		self._decisions.append(dec)
		self._tokens[token] = dec
		self._by_site_obj[site_key] = dec
		self._coord_owner[coord_key] = id(obj)
		if prior is None:
			self._obj_meta[id(obj)] = (coord, type(obj).__name__, fields_dict)
		else:
			# merge field snapshots for later cross-site adds
			prior[2].update(fields_dict)
		return dec

	def validate_and_freeze(self, func: "M.MirFunc") -> "CleanupPlan":
		"""Prove every anchor occupies its recorded original coordinate in
		`func`, then freeze. Consumption is forbidden until this succeeds.

		Checks per unique anchor object:
		  * the plan belongs to `func` (`func.name == fn_name`);
		  * block exists, orig_index is nonnegative;
		  * INSTR → `isinstance(obj, MInstr)` and obj is exactly
		    `instructions[orig_index]`;
		  * TERM → `isinstance(obj, MTerminator)`, obj is `block.terminator`,
		    and `orig_index == len(instructions)`.
		(Coordinate collisions and cross-site inconsistency were rejected
		at `add` time.)
		"""
		if self._frozen:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: validate_and_freeze() twice"
			)
		if func.name != self._fn_name:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: plan does not belong to func "
				f"{func.name!r}"
			)
		seen_obj: set[int] = set()
		for dec in self._decisions:
			oid = id(dec.obj)
			if oid in seen_obj:
				continue
			seen_obj.add(oid)
			coord = dec.coord
			block = func.blocks.get(coord.block)
			if block is None:
				raise PlanContractError(
					f"cleanup_plan[{self._fn_name}]: anchor block "
					f"{coord.block!r} does not exist (site {dec.site!r})"
				)
			if coord.orig_index < 0:
				raise PlanContractError(
					f"cleanup_plan[{self._fn_name}]: negative orig_index "
					f"{coord.orig_index} at {coord.block} (site {dec.site!r})"
				)
			if coord.anchor_kind == ANCHOR_INSTR:
				if not isinstance(dec.obj, M.MInstr):
					raise PlanContractError(
						f"cleanup_plan[{self._fn_name}]: INSTR anchor is not a "
						f"MInstr ({type(dec.obj).__name__}) at {coord.block}:"
						f"{coord.orig_index}"
					)
				if not (0 <= coord.orig_index < len(block.instructions)
				        and block.instructions[coord.orig_index] is dec.obj):
					raise PlanContractError(
						f"cleanup_plan[{self._fn_name}]: INSTR anchor object is "
						f"not at {coord.block}:{coord.orig_index} (site "
						f"{dec.site!r})"
					)
			else:  # ANCHOR_TERM
				if not isinstance(dec.obj, M.MTerminator):
					raise PlanContractError(
						f"cleanup_plan[{self._fn_name}]: TERM anchor is not a "
						f"MTerminator ({type(dec.obj).__name__}) at {coord.block}"
					)
				if block.terminator is not dec.obj:
					raise PlanContractError(
						f"cleanup_plan[{self._fn_name}]: TERM anchor object is "
						f"not the terminator of {coord.block} (site {dec.site!r})"
					)
				if coord.orig_index != len(block.instructions):
					raise PlanContractError(
						f"cleanup_plan[{self._fn_name}]: TERM anchor orig_index "
						f"{coord.orig_index} != len(instructions) "
						f"{len(block.instructions)} at {coord.block}"
					)
		# Declared semantic-field snapshots must match the object NOW, at
		# build time — a mis-declared field is caught here, not deferred to
		# consumption. (Per-decision, since sites may declare different
		# field subsets for a shared object.)
		for dec in self._decisions:
			for name, value in dec.fields:
				actual = getattr(dec.obj, name, _MISSING)
				if actual != value:
					raise PlanContractError(
						f"cleanup_plan[{self._fn_name}]: declared field {name!r} "
						f"for site {dec.site!r} does not match the anchor object "
						f"at build ({value!r} != {actual!r}) at "
						f"{dec.coord.block}:{dec.coord.orig_index}"
					)
			# Declared type bindings (`local -> expected TypeId`) must match
			# `func.local_types` NOW — a mis-declared type is caught at build.
			self._check_type_bindings(dec, func, when="build")
		self._frozen = True
		return self

	def _check_type_bindings(self, dec: "Decision", func: "M.MirFunc", *, when: str) -> None:
		lt = func.local_types
		for local, expected_ty in dec.type_bindings:
			if local not in lt:
				raise PlanContractError(
					f"cleanup_plan[{self._fn_name}]: type binding local "
					f"{local!r} (site {dec.site!r}) is absent from "
					f"func.local_types at {when} ({dec.coord.block}:"
					f"{dec.coord.orig_index})"
				)
			if lt[local] != expected_ty:
				raise PlanContractError(
					f"cleanup_plan[{self._fn_name}]: type binding for local "
					f"{local!r} (site {dec.site!r}) changed ({expected_ty!r} -> "
					f"{lt[local]!r}) at {when} ({dec.coord.block}:"
					f"{dec.coord.orig_index})"
				)

	# ---- introspection (read-only views) -----------------------------

	def decisions_for_site(self, site: str) -> Tuple[Decision, ...]:
		return tuple(d for d in self._decisions if d.site == site)

	def all_decisions(self) -> Tuple[Decision, ...]:
		return tuple(self._decisions)

	def __len__(self) -> int:
		return len(self._decisions)

	# ---- consumption phase -------------------------------------------

	def open_session(self, func: "M.MirFunc") -> ConsumptionSession:
		"""Open a batch consumption session: ONE scan of `func`, then O(1)
		`locate`. Forbidden before finalization."""
		if not self._frozen:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: open_session() before "
				f"validate_and_freeze()"
			)
		if func.name != self._fn_name:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: session func {func.name!r} "
				f"does not belong to this plan"
			)
		return ConsumptionSession(self, func)

	def begin_phase(self, func: "M.MirFunc") -> "EmitterPhase":
		"""Open an emitter phase enforcing preflight → rewrite → postflight →
		consume. Forbidden before finalization."""
		if not self._frozen:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: begin_phase() before "
				f"validate_and_freeze()"
			)
		return EmitterPhase(self, func)

	def _require_owned(self, dec: "Decision") -> None:
		if not self._frozen:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: consumption before freeze"
			)
		if self._tokens.get(getattr(dec, "token", -1)) is not dec:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: foreign decision (not owned "
				f"by this plan)"
			)

	def _mark_consumed(self, dec: "Decision") -> None:
		"""Plan-private: called ONLY by `ConsumptionSession.consume` after a
		successful `locate`. There is no public way to mark a decision
		consumed without a session validating its anchor first."""
		self._require_owned(dec)
		if dec.token in self._consumed:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: decision for site "
				f"{dec.site!r} at {dec.coord.block}:{dec.coord.orig_index} "
				f"consumed twice"
			)
		self._consumed.add(dec.token)

	def is_consumed(self, dec: "Decision") -> bool:
		self._require_owned(dec)
		return dec.token in self._consumed

	def assert_all_consumed(self) -> None:
		"""Fail closed if any decision was never consumed (orphan)."""
		if not self._frozen:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: assert_all_consumed() before "
				f"freeze"
			)
		leftover = [d for d in self._decisions if d.token not in self._consumed]
		if leftover:
			sample = ", ".join(
				f"{d.site}@{d.coord.block}:{d.coord.orig_index}"
				for d in leftover[:8]
			)
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: {len(leftover)} unconsumed "
				f"decision(s) — every planned decision must be emitted exactly "
				f"once (orphan fails closed): {sample}"
			)

	def validate_unconsumed(self, func: "M.MirFunc", sites: "set[str] | None" = None) -> None:
		"""NON-CONSUMING postflight: prove every still-UNCONSUMED decision's
		anchor is intact in the CURRENT `func` (identity + same-block +
		exactly-once + unchanged kind/fields + type bindings), WITHOUT
		marking anything consumed.

		Opens one session and `locate`s each unconsumed decision (optionally
		filtered to `sites`). Used to prove a decision population SURVIVED a
		mutation phase it does NOT consume — e.g. site-3 Return anchors must
		outlive `ownership_normalization`'s block rewrite and `overwrite_cleanup`'s
		null-safe/site-4 insertions before the later Return authority (S5)
		consumes them. A replaced / disappeared / duplicated / moved /
		field-drifted anchor fails closed via `PlanContractError`.
		"""
		if not self._frozen:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: validate_unconsumed() before "
				f"freeze"
			)
		session = self.open_session(func)
		try:
			for dec in self._decisions:
				if dec.token in self._consumed:
					continue
				if sites is not None and dec.site not in sites:
					continue
				session.locate(dec)   # NON-consuming identity+location proof
		finally:
			session.close()

	def assert_sites_consumed(self, sites: "set[str]") -> None:
		"""Fail closed if any decision in `sites` is unconsumed.

		The plan intentionally survives MULTIPLE emitter phases (overwrite
		authority consumes nullsafe+site4; the unified Return authority later
		consumes site3+R3+R4+R8), so `assert_all_consumed()` cannot run until
		the LAST coordinated consumer. This site-scoped check lets an
		intermediate phase prove its OWN sites are fully consumed without
		weakening the final global orphan detection.
		"""
		if not self._frozen:
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: assert_sites_consumed() before "
				f"freeze"
			)
		leftover = [
			d for d in self._decisions
			if d.site in sites and d.token not in self._consumed
		]
		if leftover:
			sample = ", ".join(
				f"{d.site}@{d.coord.block}:{d.coord.orig_index}"
				for d in leftover[:8]
			)
			raise PlanContractError(
				f"cleanup_plan[{self._fn_name}]: {len(leftover)} unconsumed "
				f"decision(s) in sites {sorted(sites)} — this phase must consume "
				f"every decision of its sites (orphan fails closed): {sample}"
			)


class EmitterPhase:
	"""Enforces the preflight → rewrite → postflight → consume lifecycle for
	one emitter phase over `func`.

	    phase = plan.begin_phase(func)
	    for dec in phase_decisions:
	        idx = phase.stage(dec)        # PREFLIGHT validate (pre-mutation)
	        ... capture emission from the pre-rewrite snapshot ...
	    ... rewrite func (insert drops before anchors, etc.) ...
	    phase.mark_rewritten()            # preflight snapshot now stale
	    phase.commit()                    # POSTFLIGHT fresh-validate + consume

	A decision is only marked consumed by `commit`, which FIRST re-validates
	every staged decision against the mutated MIR via a FRESH session — so a
	decision is never consumed on the strength of a stale preflight view, and
	`assert_all_consumed()` cannot pass unless the post-rewrite MIR still
	satisfies the anchor contract. `stage` is refused after rewrite; `commit`
	is refused before it. Cost is one preflight scan + one postflight scan +
	O(staged) — `O(MIR + decisions)` per phase.
	"""

	def __init__(self, plan: "CleanupPlan", func: "M.MirFunc") -> None:
		self._plan = plan
		self._func = func
		self._preflight: ConsumptionSession | None = plan.open_session(func)
		self._staged: List[Decision] = []
		self._staged_tokens: set[int] = set()
		self._rewritten = False
		self._committed = False

	def stage(self, dec: "Decision") -> int:
		"""PREFLIGHT: validate `dec` against the pre-rewrite MIR and stage it
		for consumption at commit. Returns the preflight index."""
		if self._rewritten:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: stage() after "
				f"mark_rewritten()"
			)
		assert self._preflight is not None
		index = self._preflight.locate(dec)
		if dec.token not in self._staged_tokens:
			self._staged.append(dec)
			self._staged_tokens.add(dec.token)
		return index

	def preflight_index(self, dec: "Decision") -> int:
		"""PREFLIGHT read-only validate (does not stage for consumption)."""
		if self._rewritten:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: preflight_index() after "
				f"mark_rewritten()"
			)
		assert self._preflight is not None
		return self._preflight.locate(dec)

	def mark_rewritten(self) -> None:
		if self._rewritten:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: mark_rewritten() twice"
			)
		self._rewritten = True
		assert self._preflight is not None
		self._preflight.close()
		self._preflight = None

	def commit(self) -> None:
		"""POSTFLIGHT: fresh-validate every staged decision against the
		mutated MIR, then mark them consumed. Fails closed (marking nothing)
		if any staged anchor no longer satisfies the contract."""
		if self._committed:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: commit() twice"
			)
		if not self._rewritten:
			raise PlanContractError(
				f"cleanup_plan[{self._plan._fn_name}]: commit() before "
				f"mark_rewritten()"
			)
		post = self._plan.open_session(self._func)
		for dec in self._staged:
			post.locate(dec)          # fresh postflight validation (fail closed)
		for dec in self._staged:
			self._plan._mark_consumed(dec)
		post.close()
		self._committed = True


def anchor_instr(block: str, orig_index: int) -> AnchorCoord:
	return AnchorCoord(block=block, orig_index=orig_index, anchor_kind=ANCHOR_INSTR)


def anchor_term(block: str, end_index: int) -> AnchorCoord:
	return AnchorCoord(block=block, orig_index=end_index, anchor_kind=ANCHOR_TERM)


__all__ = (
	"PlanContractError",
	"ANCHOR_INSTR",
	"ANCHOR_TERM",
	"AnchorCoord",
	"Decision",
	"ConsumptionSession",
	"EmitterPhase",
	"CleanupPlan",
	"anchor_instr",
	"anchor_term",
)
