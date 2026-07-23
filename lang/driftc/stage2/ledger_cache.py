# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Ledger cache safety — dirty-bit enforcement for `func._ownership_ledger`.

The stage2 ownership/cleanup passes (`cleanup_authoring`,
`match_cleanup_authoring`, the destructible planner) read the attached ledger
via `func._ownership_ledger` to drive emission decisions
(`verdict_at`, `_DropVerdict`).  Other passes (`string_stakes`,
`overwrite_cleanup`) do NOT consult the ledger, but they MUTATE the
MIR the ledger is cached against, so they follow the same dirty-bit
discipline: any `(block, idx)`-shifting rewrite must
`mark_ledger_dirty(func, ...)` so the next consumer rebuilds instead
of reading stale cached state.  The driver (`driftc.py`) and
in-pass code rebuild that ledger at well-known points
(post-drop_flags, post-cleanup_authoring, etc).  Between rebuilds,
any direct MIR mutation can invalidate the ledger silently:
program-point keys shift when instructions are inserted, new
blocks lack entries, terminator rewrites change CFG edges the
ledger encoded.

Pre-this-module: invalidation was a discipline rule documented in
`cleanup_authoring.py`'s pipeline-order docstring.  Forget the
rebuild → stale verdict → wrong-codegen bug surfacing only as a
specific failing e2e somewhere downstream.  Recent commits
`fdd1461b`, `849f00b1`, `c3344d86`, `fe8ca104` were all variants
of this failure mode.

This module turns the discipline into a runtime assertion:

  * Every direct MIR mutation in the scoped files calls
    `mark_ledger_dirty(func, reason)` immediately after.
  * Every read of `func._ownership_ledger` routes through either
    `require_fresh_ledger` (hard-assert) or `maybe_fresh_ledger`
    (soft, returns Optional, for documented optional-pass
    semantics only).
  * Rebuilds go through `build_and_attach_ledger`, which clears
    the dirty bit as a side effect.

A companion static audit test
(`test_ledger_cache_safety_mutation_audit.py`) scans the four
scoped files for mutation patterns and requires a nearby
`mark_ledger_dirty` call or an inline allow marker
(`# ledger-cache-safety-audit: allow <reason>`).  That covers the
"forgot the dirty mark" gap that the runtime assertion alone
cannot.

See `work/ledger-cache-safety/plan.md` for the full design and
inventory; `feature/ownership-authority-finale` predecessor work
in 0.31.9 is the architectural context.
"""
from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

from .ownership_ledger import LiveStateMap, build_ledger

if TYPE_CHECKING:
	from . import mir_nodes as M
	from lang.driftc.core.types_core import TypeId
	from .hir_to_mir import DropPolicy


# Sentinel-free internal representation: a fresh attached ledger
# has `_ledger_dirty_reason is None`.  A dirty ledger has a
# non-None reason string.  No ledger attached → both attrs absent
# (or the ledger attr is None, depending on history).


def build_and_attach_ledger(
	func: "M.MirFunc",
	*,
	drop_policy: "Callable[[TypeId], DropPolicy]",
	reason: str = "fresh-build",
) -> LiveStateMap:
	"""Standard driver path: build the ledger AND attach to
	`func._ownership_ledger`, clearing the dirty bit.  Returns
	the new ledger.

	`reason` is recorded on the function (under
	`_last_ledger_build_reason`) for diagnostics only; it does
	NOT mark the ledger dirty — the ledger is fresh on return.

	Use this anywhere driftc.py's old pattern appeared:

	    ledger = build_ledger(func, drop_policy=drop_policy)
	    setattr(func, "_ownership_ledger", ledger)

	becomes

	    ledger = build_and_attach_ledger(func,
	                                     drop_policy=drop_policy,
	                                     reason="driftc.initial_build")
	"""
	ledger = build_ledger(func, drop_policy=drop_policy)
	_attach(func, ledger, reason)
	return ledger


def attach_ledger(
	func: "M.MirFunc",
	ledger: LiveStateMap,
	*,
	reason: str = "external-attach",
) -> None:
	"""Lower-level: attach an externally-built ledger.  Clears
	the dirty bit.

	Prefer `build_and_attach_ledger` for the driver path.  This
	helper exists for tests and for pass-local cases where the
	ledger was built separately for inspection and the caller
	wants to expose it as the function-attached one.
	"""
	_attach(func, ledger, reason)


def _attach(func: "M.MirFunc", ledger: LiveStateMap, reason: str) -> None:
	"""Internal: do the attach + clear-dirty in one place so
	`attach_ledger` and `build_and_attach_ledger` cannot drift."""
	setattr(func, "_ownership_ledger", ledger)
	setattr(func, "_ledger_dirty_reason", None)
	setattr(func, "_last_ledger_build_reason", reason)


def mark_ledger_dirty(func: "M.MirFunc", reason: str) -> None:
	"""Mark `func._ownership_ledger` as stale.

	Call IMMEDIATELY AFTER any direct MIR mutation in the stage2
	ownership/cleanup passes (drop_flags, cleanup_authoring,
	match_cleanup_authoring, ownership_normalization, string_stakes,
	overwrite_cleanup).  No-op if no ledger
	is attached.

	`reason` is free-text and appears in the staleness
	assertion message.  Convention is
	`"<pass_name>.<action>"` (e.g.
	`"drop_flags.insert_flag_store"`,
	`"cleanup_authoring.emit_guarded_drop"`).  Tests must not
	match on the exact string — it's reviewer/diagnostic info
	only.
	"""
	if getattr(func, "_ownership_ledger", None) is None:
		# Nothing attached → mutation cannot make anything stale.
		# This is the normal state during initial HIR→MIR construction.
		return
	# Record the FIRST reason since the last attach.  Subsequent
	# mutations in the same dirty window keep the first reason —
	# that's the one closest to "what mutated me" in source order.
	if getattr(func, "_ledger_dirty_reason", None) is None:
		setattr(func, "_ledger_dirty_reason", reason)


def require_fresh_ledger(
	func: "M.MirFunc", consumer: str
) -> LiveStateMap:
	"""Hard-assert: the ledger is attached AND not dirty.  Return it.

	`consumer` is the caller's name (e.g.
	`"driftc.observe_reporter"`); it appears in the assertion
	message alongside the dirty reason so the diagnostic points
	at both ends.

	Raises `AssertionError` on:
	  * no ledger attached
	  * ledger present but `_ledger_dirty_reason is not None`

	Use this in production driver paths where a missing or
	stale ledger represents a real bug.  For paths with
	documented optional-pass semantics (the ledger may
	legitimately not be attached yet), use
	`maybe_fresh_ledger` instead, with an inline justification
	comment.
	"""
	ledger = getattr(func, "_ownership_ledger", None)
	if ledger is None:
		raise AssertionError(
			f"ledger-cache-safety: consumer {consumer!r} requires an "
			f"attached ledger on func {getattr(func, 'symbol', '<unknown>')!r}, "
			f"but `_ownership_ledger` is None.  Build via "
			f"`build_and_attach_ledger(func, drop_policy=...)` before this consumer."
		)
	dirty_reason = getattr(func, "_ledger_dirty_reason", None)
	if dirty_reason is not None:
		last_build = getattr(func, "_last_ledger_build_reason", "<unknown>")
		raise AssertionError(
			f"ledger-cache-safety: consumer {consumer!r} read a STALE ledger "
			f"on func {getattr(func, 'symbol', '<unknown>')!r}.  "
			f"Ledger was built with reason {last_build!r}; "
			f"a subsequent MIR mutation flagged it dirty with reason "
			f"{dirty_reason!r}.  Rebuild via "
			f"`build_and_attach_ledger(...)` before this consumer."
		)
	return ledger


def maybe_fresh_ledger(
	func: "M.MirFunc", consumer: str
) -> Optional[LiveStateMap]:
	"""Soft-assert variant: return the ledger if attached AND
	fresh, else None.

	For passes that legitimately no-op when no ledger is
	attached (pass-entry guard pattern used by
	`cleanup_authoring`, `match_cleanup_authoring`,
	the legacy string_arc).  Every non-test use of this helper requires
	an inline justification comment explaining the
	optional-pass semantics — otherwise reviewers should prefer
	`require_fresh_ledger`.

	Distinct from `require_fresh_ledger` in ONE way only: a
	missing ledger returns None instead of asserting.  A
	*dirty* ledger still asserts — the soft form is about
	"the ledger may not exist yet," not "the ledger may be
	stale."  A stale ledger is always a bug.
	"""
	ledger = getattr(func, "_ownership_ledger", None)
	if ledger is None:
		return None
	dirty_reason = getattr(func, "_ledger_dirty_reason", None)
	if dirty_reason is not None:
		last_build = getattr(func, "_last_ledger_build_reason", "<unknown>")
		raise AssertionError(
			f"ledger-cache-safety: consumer {consumer!r} read a STALE ledger "
			f"on func {getattr(func, 'symbol', '<unknown>')!r}.  "
			f"Ledger was built with reason {last_build!r}; "
			f"a subsequent MIR mutation flagged it dirty with reason "
			f"{dirty_reason!r}.  Rebuild via "
			f"`build_and_attach_ledger(...)` before this consumer."
		)
	return ledger


__all__ = (
	"build_and_attach_ledger",
	"attach_ledger",
	"mark_ledger_dirty",
	"require_fresh_ledger",
	"maybe_fresh_ledger",
)
