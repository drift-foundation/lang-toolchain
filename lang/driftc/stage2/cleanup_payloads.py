# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Immutable, frozen site payloads for the B2+C decision plan (S2).

Each `CleanupPlan` decision carries one of these as its opaque `payload`.
They are frozen dataclasses carrying enough data — the local name AND
the frozen EXPECTED type — for a later emitter to validate the
local/type relationship without trusting the (mutable) `func.local_types`
mapping at emission time. See
`work/string-ownership-refactor/SLICE-B2-R6-ARCHITECTURAL-CHECKPOINT.md`
§4 and `cleanup_plan.py`.

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

if TYPE_CHECKING:
	from lang.driftc.core.types_core import TypeId


# Site-4 ledger verdicts (mirrors ownership_ledger DropVerdict names, kept
# as plain strings so payloads carry no live ledger references).
VERDICT_MUST_DROP = "MUST_DROP"
VERDICT_MUST_NOT_DROP = "MUST_NOT_DROP"


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

	`emit` is True iff `verdict == MUST_DROP` (the emission subset — 14
	across the corpus). `needs_drop` is the type-level DropPolicy axis fed
	to the ledger query. `ty` is the frozen expected local type.
	"""
	local: str
	ty: "TypeId"
	needs_drop: bool
	verdict: str            # VERDICT_MUST_DROP | VERDICT_MUST_NOT_DROP

	@property
	def emit(self) -> bool:
		return self.verdict == VERDICT_MUST_DROP


@dataclass(frozen=True)
class NullsafePayload:
	"""Unconditional destructible-overwrite drop at a null-safe destructible
	`StoreLocal`. Always emits."""
	local: str
	ty: "TypeId"


__all__ = (
	"VERDICT_MUST_DROP",
	"VERDICT_MUST_NOT_DROP",
	"Site3Drop",
	"Site3ReturnPayload",
	"Site4Payload",
	"NullsafePayload",
)
