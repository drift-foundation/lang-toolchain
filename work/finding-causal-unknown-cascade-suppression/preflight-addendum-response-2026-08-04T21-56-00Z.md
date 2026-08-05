# Preflight addendum response — transaction boundary + value-position inventory

Timestamp: 2026-08-04T21-56-00Z
Author: K (implementer)

Responds to `review-2026-08-04T21-48-05Z.md`.  This is an immutable
addendum-response detail: `PROGRESS.md` is frozen under the standing
`IMPL-PENDING-2026-08-04T21-51-21Z` and was not edited.  Probe source:
`probe_txn_and_value_positions.py` (work-only).

## Transaction-boundary probe (`id(f())` — pending lambda call as a generic-call argument)

- STATIC: `H.HVar` (and `H.HCall`) ARE in `_DEFER_PROBE_SAFE_NODES`
  (call_resolver.py:4707+), and the pending pre-resolution at
  type_checker.py:9986-10001 mutates `binding_types` and pops
  `pending_lambda_by_binding` — NEITHER is in `FnCheckState.OWNED_TABLES`.
  The reviewer's leak mechanism is therefore structurally real: a probed
  subtree containing a call through a pending lambda executes those
  mutations under an open `CheckerStateTxn`, and a rollback would restore
  the owned tables but leak the pending-resolution writes.
- EMPIRICAL (in-process compile reading `_DEFER_PROBE_STATS` deltas):
  the shape compiles CLEAN today — delta `probes: 57,
  commits_complete: 57`, zero rollbacks, zero errors.  So probes DO open
  during this compile and every one committed; the leak did not MANIFEST
  in this source because no probe rolled back around the pending
  resolution.  The rollback paths (NEEDS_EXPECTED / exception) remain
  reachable in principle; the risk stands as a design constraint even
  though this minimal shape cannot demonstrate the leak.
- CONSEQUENCE ACCEPTED: "totalize at first semantic value use" must NOT
  be an unconditional mutating hook in generic `type_expr(HVar)`.  The
  design must either (a) finalize at STATEMENT-level parents that cannot
  be probed (`HLet` initializer position — the confirmed alias shape;
  probe gate excludes statements by construction), preserving the
  existing pre-txn placement of the `HCall`/`HInvoke` pre-resolutions,
  or (b) bring `binding_types` + `pending_lambda_by_binding` (and any
  capture metadata the resolution touches) under the explicit owner as a
  real state-owner refactor.  (a) is the narrower, evidence-matched
  start; its coverage limits are inventoried below and any residual
  value positions stay tripwires rather than silent acceptance.

## Value-position inventory (driver compiles, full streams)

- `return f;` (pending captureless): single `E-COPY-UNKNOWN` at the
  return — same cascade family as the HLet alias; a return-position
  finalization site would be needed for totality (or the case stays a
  diagnosed tripwire under the causal design).
- `sink(f)` (argument position, concrete `Int` param): presents as
  `no matching overload for function 'sink' with args [Unknown]` — a
  THIRD presentation shape (overload failure naming Unknown), no copy
  error.  Argument positions route the pending Unknown through overload
  resolution, not `_require_copy_value`; any first-value-use design must
  either finalize before arg typing or accept the overload message as
  the (already clean, single) presentation there.
- Combined with the earlier matrix: HLet-alias/copy, return, and
  argument positions each surface pending-Unknown DIFFERENTLY today.
  An HLet-only finalization patch must therefore be labeled partial
  coverage explicitly; total first-value-use coverage requires the
  inventory-driven site list (HLet initializer, return value, call
  argument) or the ownership refactor (b).

## Bottom line for the implementation slice

The addendum's caution is confirmed and adopted.  Recommended sequencing
when Slawomir opens implementation: (1) the diagnosed-Unknown cause map
(`_TxnDict` in OWNED_TABLES) with HLet-from-diagnosed-HVar propagation —
independent of pending finalization and fixes the original two red
probes; (2) pending finalization at the HLet-initializer site (heals the
valid captureless alias child) with the pre-txn placement proof; (3)
return/argument positions as explicit follow-up scope with pinned
current presentations.  Each stage keeps unmarked Unknown as a tripwire.
