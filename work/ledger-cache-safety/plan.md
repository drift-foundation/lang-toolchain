# Ledger cache safety slice — implementation plan

**Status:** planned, NOT started.  Sequenced after mariadb v1 cert / Condvar acceptance.

**Outcome of the architectural read on 2026-05-16.**  See the chat
transcript referenced in `memory/project_ledger_cache_safety_slice.md`
for the discussion that produced this plan.

## Decision

Land a focused "ledger cache safety" slice as the next toolchain
hardening item.  **Do NOT** open a broad ownership refactor.

## Why

The recent bug pattern crossing `cleanup_authoring` /
`drop_flags` / `string_arc` / driver ledger rebuilds (commits
`fe8ca104`, `fdd1461b`, `849f00b1`, `c3344d86`) is concentrated
at the ledger-handoff boundary between passes, not inside any
single pass.  Splitting the modules differently will not move
the bug count.  The cheap, high-leverage fix is making ledger
staleness a runtime assertion instead of a discipline rule
documented in the `cleanup_authoring.py` pipeline-order
docstring.

## Sequencing rule

Do NOT put this in front of the current mariadb v1 acceptance.
Finish certifying the Condvar / ManagedConnection path first.
Land the dirty-bit guard as the NEXT toolchain hardening slice —
**unless** another stale-ledger bug appears in the mariadb cert
path first, in which case promote it.

## Target contract (K-approved framing, verbatim)

1. Any MIR mutation after a ledger build marks the function's
   ownership ledger dirty.
2. Any consumer that reads `func._ownership_ledger` must assert
   it is not dirty.
3. Rebuilding the ledger clears the dirty bit.
4. Direct-mutation paths that bypass `MirBuilder` either get
   wrapped or explicitly call the dirty-mark helper.

## Short implementation plan

### Inventory (audit before coding)

Grep for the mutation surface in these files:
`drop_flags.py`, `cleanup_authoring.py`, `string_arc.py`,
`hir_to_mir.py`, plus `match_cleanup_authoring.py` (also reads
the ledger).  Distinguish three categories per call site:

- `MirBuilder.<method>(...)` — wrap the builder so every mutating
  method marks dirty automatically.
- Direct `func.blocks[i].instrs.{insert,append,__delitem__,...}`
  — audit list and either route through a builder helper or call
  `mark_ledger_dirty(func, reason)` inline.
- Pass-local data structures (e.g.
  `func._drop_flag_managed_locals` set updates) — out of scope;
  they don't change MIR layout, only metadata read by later
  passes.

### Proposed helper surface

New module `lang/driftc/stage2/ledger_cache.py`:

```python
def attach_ledger(func: MirFunc, ledger: OwnershipLedger) -> None:
    """Attach a freshly-built ledger; clears the dirty bit."""

def mark_ledger_dirty(func: MirFunc, reason: str) -> None:
    """Called automatically by MirBuilder; can be called directly
    for sites that bypass the builder.  `reason` becomes part of
    the staleness assertion message."""

def require_fresh_ledger(func: MirFunc, consumer: str) -> OwnershipLedger:
    """Assert the ledger is attached AND not dirty, then return
    it.  AssertionError message names `consumer` and the last
    `reason` that marked dirty."""
```

`MirFunc` gains two attributes:
- `_ownership_ledger: Optional[OwnershipLedger]`
- `_ledger_dirty_reason: Optional[str]` (None when fresh;
  non-None ⇒ dirty)

### First-pass enforcement scope

Only the stage2 ownership/cleanup consumers:
- `cleanup_authoring.py` — every `verdict_at` read.
- `drop_flags.py` planning consultation site (currently reads
  ledger to identify path-dependent destructibles).
- `string_arc.py` — every `_DropVerdict` consultation.
- `match_cleanup_authoring.py` — same.

NOT yet:
- `hir_to_mir.py` (builds the initial MIR; no pre-existing
  ledger to consult).
- Codegen / SSA / mir_validate / unrelated modules — out of
  scope.

`MirBuilder` (likely in `mir_nodes.py` or a builder module)
gains the auto-dirty-mark hook on every mutating method.  Audit
list at plan time; estimate ~6-12 methods.

### Regression test

`lang/tests/driver/test_ledger_cache_safety_dirty_bit.py`:

1. Construct a small `MirFunc` (or load a pre-built fixture).
2. Build + attach an ownership ledger.
3. Mutate MIR via the `MirBuilder` emission API.
4. Call `require_fresh_ledger(func, "test_consumer")`.
5. Assert it raises `AssertionError` whose message names both
   `"test_consumer"` and the dirty reason recorded by the
   builder.

Plus a positive test: build → attach → `require_fresh_ledger`
→ returns ledger without error.

### Effort estimate

Total LOC: ~200 (helper module + MirBuilder hooks + ~5
consumer-site call sites + regression test).  No migration
risk — the dirty bit is additive; existing rebuild calls in
`driftc.py` continue to work and now also clear the bit.  Bugs
already in flight (any stale-ledger consultation in current
code) surface as assertions at test-time, which is the intended
outcome.

### Explicitly out of scope

- Wrapping every `func.blocks[i].instrs.list` access in an
  opaque type.  First pass uses explicit
  `mark_ledger_dirty(func, reason)` calls at the few
  direct-mutation sites; convert to wrappers in a follow-up only
  if drift continues.
- Static type-level enforcement (Python type system doesn't
  support "fresh vs dirty" as a type-level distinction).
  Runtime assertion is the contract.
- Extending the dirty bit to stage4 / SSA / codegen.  If
  evidence emerges that those also need it, expand later.
- "Finishing the Phase 4 migration" (strings/arrays
  return-source legacy alias-walk, moved_out_locals /
  explicitly_dropped_locals holdouts).  Independent track; do
  not couple.

### Open questions worth a 10-minute K conversation before coding

1. Should `mark_ledger_dirty` be allowed to detach the ledger
   entirely (set `_ownership_ledger = None`)?  Today's pattern
   is "rebuild after," so detach-on-mutation would force every
   downstream consumer to rebuild before reading — louder
   failure mode at the cost of more rebuild calls in the
   driver.  Default answer: don't detach; just mark dirty.
   Detach would be a stricter follow-up if drift persists.
2. Reason-string format: should it be a free-text label
   (`"drop_flags.insert_flag_store"`) or a typed enum?
   Free-text is cheaper and assertion-message-only; enum would
   let tests distinguish dirty causes.  Default: free-text.
