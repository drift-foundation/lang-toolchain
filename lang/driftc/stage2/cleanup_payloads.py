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


# The only two verdicts constructible as an emission payload. PATH_DEPENDENT
# is deliberately absent — a drop-before-overwrite that returns PATH_DEPENDENT
# fires the site-4 tripwire in the authority and is never turned into a
# payload (an emitter must never silently treat it as "do not emit").
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

	`verdict` is the TYPED `DropVerdict` member (MUST_DROP / MUST_NOT_DROP)
	— never a bare string whose unknown value could make `emit` silently
	False. PATH_DEPENDENT is rejected at construction (the tripwire fires
	upstream). `emit` is True iff `verdict is DropVerdict.MUST_DROP` (the
	emission subset — 14 across the corpus). `needs_drop` is the canonical
	DropPolicy axis; `ty` is the frozen expected local type.
	"""
	local: str
	ty: "TypeId"
	needs_drop: bool
	verdict: "DropVerdict"

	def __post_init__(self) -> None:
		if self.verdict not in _EMITTABLE_VERDICTS:
			raise ValueError(
				f"Site4Payload verdict must be MUST_DROP or MUST_NOT_DROP, "
				f"got {self.verdict!r} (PATH_DEPENDENT is never a payload)"
			)

	@property
	def emit(self) -> bool:
		return self.verdict is DropVerdict.MUST_DROP


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
