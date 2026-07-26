# RESOLVED — TLR-8: MoveOut joins the materialized-release family

Status: fixed in-tree as **0.33.84** (2026-07-17; ABI stays 21 — no
boundary contract change), pending user-run full suite + staging.
History entry: doc/history.md 2026-07-17.

## Diagnosis

All three drift-workflows firings (and the pinned minimal repro) are the
same class: firing class 2 of the release-arm tripwire — a NON-FAMILY
owned producer reaching a non-consuming drain.  `MoveOut`'s String dest
inherits the storage local's +1 stake verbatim (the expansion
zero-stores the local, so the dest is the sole holder), and a
concatenation only borrows its operands (generic-fallthrough USE
disposition).  Pre-tripwire, string_arc's in-pass last-use release arm
released the temp after the concat; the TLR ladder never migrated the
class because the toolchain corpus has ZERO `+ move s` sites — the TLR
measurement could not see it.  drift-workflows has 15.  The tripwire
did exactly its job: surfaced the unmigrated population as a clean ICE
instead of letting the fail-closed arm mask or leak it.

## Fix (TLR-8, the TLR-6 shape)

- `stage2/string_arc.py` — `is_materialized_release_family_producer`:
  `M.MoveOut` added as an unconditional member (dest String-typedness
  remains the caller's `_is_family_temp` check).
- `stage2/string_arc.py` — `seed_string_dest_types`: MoveOut arm added
  (the instruction carries its type; the family analysis must not
  depend on upstream metadata completeness).
- `stage2/string_arc.py` — MoveOut expansion arm: the owned/move-only
  registration is now guarded on `recognized_released` (the TLR-6
  lesson: a live rewrite-loop re-add after the per-block
  `owned_values -= recognized_released` subtraction would re-own the
  externally-released temp and trip `_note_use` at the drain).
- `stage2/string_releases.py` — header family list updated.

## Verification

- Pinned repro compiles, runs (`t.len == 8`), valgrind clean
  (13 allocs / 13 frees / 0 errors).
- All three production firing shapes covered: plain `"lit" + move s`,
  match-binder concat in a value-producing arm, chained
  `a + sep + move p`, plus the throw-path
  `throw E(what = ... + move m)` — all compile, run correctly,
  valgrind clean.  All four are PINNED as rows of the memcheck
  fixture (the throw-path row exercises the error edge through the
  try/catch fallback).
- New pins: `test_tlr8_moveout_family`, `test_tlr8_moveout_guard_teeth`,
  `test_tlr8_cross_block_moveout`,
  `test_tlr8_move_operand_concat_end_to_end` (driver-level, real
  source) in `lang/tests/stage2/test_string_arc_audit_reporter.py`;
  memcheck row `lang/tests/memcheck/test_move_operand_concat_release.py`.
- Batteries: stage2 373/373 (includes reporter 60/60); standalone
  memcheck suite run separately.

Consumed-move dests (`return move x`, by-value call args, stores) are
unaffected: a CONSUME disposition disqualifies at the calculator, same
as every other family member.

## Closure (tripwire-deletion slice, 2026-07-18)

- 0.33.84 / ABI 21 CERTIFIED 2026-07-18 with the release-arm tripwire
  (and the 4a/4b dead-stake tripwires) armed: zero firings through the
  full suite.  Certified run `20260719-001008-drift-lang-99a68ee`
  additionally exercised drift-workflows `0251b24` — the corpus that
  produced this issue's 15 firings — through staging plus normal/debug
  test, stress, and perf lanes with ZERO tripwire log matches.
- The deletion condition of RELEASE-ARM-TRIPWIRE-DESIGN.md §8 ("one
  clean cert cycle with zero firings") is therefore MET, and the arm,
  its tripwire, and the 4a/4b dead-stake branches were DELETED
  (tripwire-deletion slice, 2026-07-19 — doc/history.md, 0.33.85
  series).
  `SITE_CLASS_TEMP_LASTUSE_RELEASE` retired from the closed reporter
  enumeration (future notes count UNTAGGED — hard corpus gate).
- The TLR-8 semantic regressions this issue produced remain pinned:
  `test_tlr8_moveout_family` / `test_tlr8_cross_block_moveout` /
  `test_tlr8_move_operand_concat_end_to_end` and memcheck
  `test_move_operand_concat_release.py` (all four production shapes).
  `test_tlr8_moveout_guard_teeth` retired WITH the guard it pinned
  (the rewrite-loop `recognized_released` re-add guard is dead once no
  release arm consumes re-owned state — maintainer-confirmed).
- Folder retained as the historical record of the tripwire's one
  production catch.
