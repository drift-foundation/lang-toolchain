# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Immutable, frozen site payloads for the B2+C decision plan (S2).

Each `CleanupPlan` decision carries one of these as its opaque `payload`.
They are frozen dataclasses carrying enough data — the local name AND
the frozen EXPECTED type — for a later emitter to validate the
local/type relationship without trusting the (mutable) `func.local_types`
mapping at emission time. Architecture decided in the B2+C R6
architectural review (0.33.86-0.33.87 series; see the string_arc-endgame
entries in doc/history.md) and `cleanup_plan.py`.

Sites:
  * site-3  — Return-boundary destructible drops. ONE decision per
    Return anchor, carrying an immutable ORDERED tuple of the locals to
    drop (the `sorted(destructible_locals)` order), possibly empty
    (empty coverage proves the Return was planned).
  * site-4  — drop-before-overwrite at a destructible `StoreLocal`. One
    decision per eligible non-null-safe destructible store, carrying its
    ledger verdict; only `MUST_DROP` emits (the 14), but `MUST_NOT_DROP`
    decisions are recorded so the full authority population is accounted.
  * nullsafe — unconditional destructible-overwrite drop at a null-safe
    destructible `StoreLocal`. One decision per eligible store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple, TYPE_CHECKING

from .ownership_ledger import DropVerdict

if TYPE_CHECKING:
	from lang.driftc.core.types_core import TypeId


from enum import Enum


class Site4Disposition(Enum):
	"""The EXPLICIT, TYPED disposition of a drop-before-overwrite (site-4)
	decision — never an ambiguous boolean and never a bare
	PATH_DEPENDENT.  The authority (`site4_disposition`) resolves the
	ledger verdict + ownership class into exactly one of these:

	* ``NO_DROP``       — MUST_NOT_DROP: the store overwrites a
	  moved-out / uninitialized slot; emit nothing.
	* ``UNCONDITIONAL`` — MUST_DROP, OR zero-storage-drop-safe
	  PATH_DEPENDENT (variants via tag-0, arrays via the zeroed
	  header): emit the canonical Load→Zero→Store(zero)→Drop at the
	  store, unconditionally (dropping moved-out zero storage is safe).
	* ``FLAG_GUARDED``  — zero-storage-UNSAFE PATH_DEPENDENT with a
	  drop-flag: emit the canonical cleanup guarded by the local's
	  runtime drop flag at the overwrite point.  Carries ``flag_local``.
	"""
	NO_DROP = "no_drop"
	UNCONDITIONAL = "unconditional"
	FLAG_GUARDED = "flag_guarded"


_EMITTABLE_VERDICTS = frozenset({DropVerdict.MUST_DROP, DropVerdict.MUST_NOT_DROP})


@dataclass(frozen=True)
class Site3Drop:
	"""One destructible local to drop at a Return, with its frozen expected
	type (as classified at planning time on the original MIR)."""
	local: str
	ty: "TypeId"


@dataclass(frozen=True)
class Site3ReturnPayload:
	"""All site-3 drops planned for ONE Return anchor, in
	`sorted(destructible_locals)` order. `drops` may be empty — an empty
	payload still records that the Return was planned (explicit coverage).
	"""
	drops: Tuple[Site3Drop, ...]

	@property
	def local_count(self) -> int:
		return len(self.drops)


@dataclass(frozen=True)
class Site4Payload:
	"""Drop-before-overwrite decision at a destructible `StoreLocal`.

	`disposition` is the EXPLICIT TYPED `Site4Disposition` (never an
	ambiguous boolean, never a bare PATH_DEPENDENT).  `verdict` is the
	raw ledger `DropVerdict` retained for provenance/telemetry.
	`needs_drop` is the canonical DropPolicy axis; `ty` is the frozen
	expected local type.  `flag_local` is the drop-flag local name and
	is REQUIRED iff `disposition is FLAG_GUARDED` (forbidden otherwise).

	PATH_DEPENDENT is a valid input here (the lattice is correct to
	return it); the authority classifies it into UNCONDITIONAL
	(zero-storage-safe) or FLAG_GUARDED (zero-storage-unsafe +
	flag-managed) before this payload is built.
	"""
	local: str
	ty: "TypeId"
	needs_drop: bool
	verdict: "DropVerdict"
	disposition: "Site4Disposition"
	flag_local: "str | None" = None

	def __post_init__(self) -> None:
		if not isinstance(self.disposition, Site4Disposition):
			raise ValueError(
				f"Site4Payload.disposition must be a Site4Disposition, "
				f"got {self.disposition!r}"
			)
		if not isinstance(self.verdict, DropVerdict):
			raise ValueError(
				f"Site4Payload.verdict must be a DropVerdict, "
				f"got {self.verdict!r}"
			)
		# ── Fail-closed verdict ↔ disposition cross-validation ──
		# The ONLY admissible (verdict, disposition) pairs, so a
		# mis-derived disposition can never be frozen into a plan:
		#   * NO_DROP        iff  raw verdict is MUST_NOT_DROP
		#   * FLAG_GUARDED   iff  raw verdict is PATH_DEPENDENT (+ flag)
		#   * UNCONDITIONAL  iff  raw verdict is MUST_DROP OR PATH_DEPENDENT
		#                         (the latter = zero-storage-drop-safe class)
		# The zero-safe-vs-unsafe split inside PATH_DEPENDENT is the
		# authority's (`site4_disposition`); here we pin the coarse
		# verdict→disposition legality that holds regardless of class.
		if self.disposition is Site4Disposition.NO_DROP:
			if self.verdict is not DropVerdict.MUST_NOT_DROP:
				raise ValueError(
					f"Site4Payload NO_DROP requires verdict MUST_NOT_DROP, "
					f"got {self.verdict}"
				)
		elif self.disposition is Site4Disposition.FLAG_GUARDED:
			if self.verdict is not DropVerdict.PATH_DEPENDENT:
				raise ValueError(
					f"Site4Payload FLAG_GUARDED requires verdict "
					f"PATH_DEPENDENT, got {self.verdict}"
				)
		else:  # UNCONDITIONAL
			if self.verdict not in (
				DropVerdict.MUST_DROP,
				DropVerdict.PATH_DEPENDENT,
			):
				raise ValueError(
					f"Site4Payload UNCONDITIONAL requires verdict MUST_DROP "
					f"or PATH_DEPENDENT (zero-safe), got {self.verdict}"
				)
		# ── flag_local presence: required IFF FLAG_GUARDED ──
		if self.disposition is Site4Disposition.FLAG_GUARDED:
			if not isinstance(self.flag_local, str) or not self.flag_local:
				raise ValueError(
					"Site4Payload FLAG_GUARDED disposition requires a "
					f"flag_local name (got {self.flag_local!r})"
				)
		elif self.flag_local is not None:
			raise ValueError(
				f"Site4Payload.flag_local must be None unless FLAG_GUARDED "
				f"(disposition={self.disposition}, flag_local={self.flag_local!r})"
			)

	@property
	def emit(self) -> bool:
		"""True iff this decision authors a cleanup (unconditional OR
		flag-guarded).  NO_DROP authors nothing."""
		return self.disposition is not Site4Disposition.NO_DROP

	@property
	def guarded(self) -> bool:
		return self.disposition is Site4Disposition.FLAG_GUARDED


@dataclass(frozen=True)
class NullsafePayload:
	"""Unconditional destructible-overwrite drop at a null-safe destructible
	`StoreLocal`. Always emits."""
	local: str
	ty: "TypeId"


@dataclass(frozen=True)
class StringReleasePayload:
	"""All R3/R4 String scope-exit releases planned for ONE Return anchor,
	in `string_return_releases` order (= `sorted(string_locals)` minus the
	R3/R4 skip).  `locals` may be empty — an empty payload still records
	that the Return was planned for the string-release site (explicit
	coverage, symmetric with `Site3ReturnPayload`).

	The per-local expected `String` TypeId is carried on the `Decision`'s
	`type_bindings` (each local -> `string_ty`), so the emitter validates
	the local/type relationship through the frozen plan without trusting
	the mutable `func.local_types` at emission time — the same discipline
	`Site3Drop.ty` provides for the destructible tail."""
	locals: Tuple[str, ...]

	@property
	def local_count(self) -> int:
		return len(self.locals)


__all__ = (
	"DropVerdict",
	"Site3Drop",
	"Site3ReturnPayload",
	"Site4Payload",
	"NullsafePayload",
	"StringReleasePayload",
)
