# Fix design — PathDependent drop_before_overwrite (Option A, 8 directives)

Reviewer ruling: the lattice is CORRECT to return PathDependent (this
state genuinely depends on the runtime branch).  Do NOT force
MUST_DROP/MUST_NOT_DROP.  Emit the right cleanup per ownership class.

## Confirmed mechanism (code-grounded)

* ICE site: `destructible_authority.site4_verdict` raises on
  PathDependent (proof-obligation tripwire; comment asserts
  "Unreached across 1031 e2e cases").  Reachable from valid source
  (conditional move then overwrite).  Not a regression (certified
  0.33.87 ICEs too); throwing-independent.
* `cleanup_authoring` (site-1 scope drops) ALREADY handles the same
  PathDependent situation with four shapes: UNGUARDED (zero-safe),
  per-arm EDGE_ELABORATED and flag-guarded FALLBACK (zero-unsafe +
  flag-managed), and SKIP/tripwire (zero-unsafe + not flag-managed).
  Site-4 must route through the SAME authority (directive 3).
* `drop_flags` already flags locals that are live-at-exit or
  zero-unsafe-PD at a CleanupHook (criterion 2b; its comment even
  says omitting this "leav[es] the site-4 drop-before-overwrite
  verdict to crash").  It does NOT yet have a criterion for
  zero-unsafe-PD at an OVERWRITE (site-4) point → directive 2b needs
  a mirrored criterion so every such local is flagged.
* `zero_storage_drop_safe(ty, type_table)` = True for variants (tag-0
  dispatch) and arrays (zeroed header).  `Optional<T>` is a variant →
  the json parser's `pending_span` takes the UNCONDITIONAL path (no
  flag needed).

## Implementation (incremental, tested at each step)

1. **Regression-first driver test** (directive 1): compile+run a
   matrix — conditional move then overwrite, in loop / straight-line,
   throwing / nothrow, zero-safe (variant) and zero-unsafe
   (struct-with-String) locals, repeated overwrites — asserting
   correct output AND exactly-once destruction (memcheck twin,
   directive 8).  Fails now (ICE).

2. **Typed Site4 disposition** (directive 4): extend `Site4Payload`
   with `Site4Disposition ∈ {NO_DROP, UNCONDITIONAL, FLAG_GUARDED}`
   + `flag_local: Optional[str]`.  Keep it frozen; reject bare/unknown.
   NO_DROP = MUST_NOT_DROP; UNCONDITIONAL = MUST_DROP OR zero-safe
   PathDependent; FLAG_GUARDED = zero-unsafe PathDependent +
   flag-managed.

3. **Route PathDependent** in `site4_verdict` (rename →
   `site4_disposition`): pass flag metadata (`_drop_flag_managed_locals`
   /`_drop_flag_for_local`); on PathDependent → zero-safe? UNCONDITIONAL
   : (flag-managed? FLAG_GUARDED(flag_local) : FAIL-CLOSED tripwire,
   directive 6).  Planner (`destructible_planner`) builds the typed
   payload; census counts the new disposition.

4. **drop_flags selection** (directive 2b-overwrite): add
   `_has_zero_storage_unsafe_path_dependent_at_overwrite` mirroring the
   cleanup-hook criterion, so every zero-unsafe-PD-at-overwrite local
   is flag-managed (else step 3's tripwire fires — fail-closed, but we
   want the guarded emission).

5. **Emission** (directive 2/3/5): in `overwrite_cleanup`,
   UNCONDITIONAL → existing inline Load→Zero→Store(zero)→Drop
   (timing preserved: immediately before the store).  FLAG_GUARDED →
   reuse an EXTRACTED shared guarded-splitter (pulled out of
   `cleanup_authoring`'s flag-guarded branch; cleanup_authoring rewired
   to call it too — no second implementation): split at the store
   anchor, LoadLocal(flag)→IfTerminator(flag, drop_blk, post_blk);
   drop_blk = Load→Zero→Store(zero)→Drop→flag-clear→Goto(post_blk);
   post_blk runs the original StoreLocal.  Preserve plan-anchor
   identity/order, zero-added-ledger-builds, no dynamic MIR metadata
   (directive 5).

6. **Fail-closed tripwire retained** (directive 6): zero-unsafe PD +
   no valid flag metadata → hard error (not ICE-with-retired-fallback
   text; a clear contract failure), reachable only if step 4 missed a
   shape.

7. **Fix the false-unreachable contract** (directive 7):
   `test_destructible_planner.py` (Site4Payload PATH_DEPENDENT
   unconstructible) and `test_drop_before_overwrite_swap.py`
   (PathDependent "Unreached"/tripwire pins) — update to the new
   disposition contract.

8. **Full matrix** (directive 8): compile/run + memcheck for
   moved/live branches, loops, throwing+nothrow, zero-safe variants,
   zero-unsafe destructibles, repeated overwrites, exact once-only
   destruction; then the ownership corpus (expect a modal delta;
   measure→attribute→governed re-promotion) + broad suite.

Then re-land the UNCHANGED iterative parser
(`iterative-parser-block.drift.wip`) on the fixed compiler and resume
the consolidated 0.33.89 phase (tasks 3–7).
