# B-arch-0 inventory — string_arc stake audit, e2e compile corpus

**Source:** `DRIFT_STRING_ARC_AUDIT=1` over the e2e case corpus
(`lang/tests/codegen/e2e/*/`), two rounds: single-file (528 compiles) + `-M`
multi-module retry (+15) = **543 compiles audited** of 1,317 cases. The 774
non-compiling remainder are subdir-module layouts (`acme/*.drift` under the case
dir), `--allow-unsafe` cases, and package-consuming cases — a cheap corpus
extension (better case-shape detection in the runner script), not a reporter
limitation. Each compile audits the full fn set including stdlib —
**counts are compile-occurrences, not unique source sites** (stdlib recompiles ~528×);
the ranking is what matters, and per-compile averages are given where useful.

## 1. Headline

- **1,966,675 events across 647,943 audited fns — UNTAGGED = 0, UNCLASSIFIED = 0.**
  The closed site_class enumeration and the C1–C4 classification model both held over
  the whole corpus. No classification-model revision needed before B-arch-1.
- **Leak candidates (`c1_must_drop_without_release`): 0.** Not one point in the corpus
  where the ledger says MUST_DROP and site 3 emitted no release.
- No genuine bug filed out of this run (nothing reproduced as misbehavior; every
  divergence class has a structural explanation below).

## 2. Aggregate (both rounds, 543 compiles)

| counter | total | ~per compile |
|---|---|---|
| events | 1,966,675 | 3,622 |
| c1_agree | 289,258 | 533 |
| c1_release_without_must_drop | 136,407 | 251 |
| c1_must_drop_without_release | **0** | 0 |
| c1_path_dependent | 11,951 | 22 |
| c2_invisible_stake | 114,107 | 210 |
| c2_visible_stake | 0 | 0 |
| c3_moveout_owned | 1,080,687 | 1,990 |
| c3_moveout_not_owned | 11,441 | 21 |
| c4_allowlisted | 106,620 | 196 |
| pre_post_verdict_drift | 4,373 | 8 |

| site_class | events |
|---|---|
| moveout_expansion | 1,092,128 |
| temp_lastuse_release | 363,409 |
| scope_exit_release | 259,351 |
| overwrite_release | 137,680 |
| call_arg_retain | 58,680 |
| value_position_retain | 47,803 |
| store_value_retain | 7,624 |
| return_retain_site3 | **0** — shape structurally extinct (see §4 revision) |
| drop_before_overwrite_site4 | 0 in this corpus* |
| destructor_self | 0 (structurally retired, Phase 4 site-3 sub-step 2) |

\* site 4 fires on destructible StoreLocal overwrites reaching MUST_DROP; the compiled
corpus subset produced none (its unit coverage lives in the site-4 Tier-1 tests, and
it keeps its own Tier-1 ledger reporter regardless). A driver-corpus extension would
populate it.

## 3. Per-class interpretation

### C1 `release_without_must_drop` — 136,407 (100% raw_state=uninit in sampled details)
Site 3 releases EVERY string local at scope exit; the ledger says MUST_NOT_DROP for
locals that are UNINIT on that path (match binders on unbound arms are the dominant
example: `__match_binder_*` in `std.cli::_slice_string` et al.). These are runtime
no-ops by design (zero-init storage + null-safe `drift_string_release`), so they are
NOT bugs — they are the ledger-elidable release inventory: once B-arch-1 lets site 3
consult the ledger for strings, each becomes a skipped emission (code-size/runtime
win) rather than a null-safe no-op.

### C2 `invisible_stake` — 114,107: THE B-arch-1 migration inventory, by shape
| shape | count | migration note |
|---|---|---|
| call_arg_retain | 58,680 | retain for a by-value String arg — the canonical copy-stake the ledger cannot see (no CopyValue in pre-MIR; string_arc invents the copy late) |
| value_position_retain | 47,803 | ctor fields (ConstructStruct/Variant), Result/Error payloads, exc-ABI strings, array elems |
| store_value_retain | 7,624 | second-use stores (`y = x` where x stays live) |

`c2_visible_stake = 0` confirms the plan's premise exactly: NO retain the pass emits
corresponds to a ledger-visible copy-consume. Every stake is invisible; B-arch-1's
job is to move these stakes into ledger-visible MIR (CopyValue-style) shape by shape.

### C3 `moveout_not_owned` — 11,441 (raw_state=maybe_uninit in sampled details)
All sampled instances sit in drop-flag cleanup blocks
(`*_cleanup_drop_<local>` — 3C's flag-guarded MoveOut+DropValue). The runtime flag
guarantees the path is taken only when initialized; the ledger, path-insensitive at
that point, says MAYBE_UNINIT. Structural, not a bug: B-arch-1's event model must
treat flag-guarded cleanup MoveOuts as conditionally-owned (or key them to the 3C
flag), or this class stays as an accepted allowlist entry.

### C4 `allowlisted` — 106,620 (REVISED interpretation, 2026-07-09)
Detail-level split: **100% `return_move_blind_release`, 0 `return_retain_site3`** —
string_arc emitted no late return retain anywhere in the corpus (the plain-return
shape was already migrated by Phase 4's alias-walk move + ledger consultation; field
returns copy upstream of string_arc, probe-verified). What remains is the release
face: String locals consumed into RETURN-REACHING COMPOSITES (ctor/call args) that
the ledger models as moved (Return-as-move through ConstructStruct et al.) while
string_arc copies at the value position and correctly releases the still-owned local.
Every C4 entry is therefore the downstream shadow of a C2 stake — see the revised §4.

### `pre_post_verdict_drift` — 4,373
L_pre vs L_post disagree at return boundaries almost entirely because the MoveOut →
Load+ZeroValue+StoreLocal expansion reads to the post-ledger as re-initialization
(StoreLocal = def). Quantifies exactly how much the ledger's MIR-visibility contract
degrades across the string_arc rewrite — the number B-repr(B5) should drive to ~0
when stakes become first-class MIR.

### C1 `path_dependent` — 11,951
String locals whose scope-exit verdict is PATH_DEPENDENT. Site 3 releases them
unconditionally (null-safe). Merges into the same ledger-elidable bucket as
release_without_must_drop once flags/edge-elaboration cover strings.

## 4. Ranked B-arch-1 worklist — REVISED 2026-07-09 (B-arch-1 stop finding)

**The original rank-1 ("C4 / site-3 return retain") is structurally extinct**: the corpus
contains ZERO `return_retain_site3` events — Phase 4's alias-walk move + ledger-authored
return-source suppression already migrated plain returns, and field returns materialize
their copy upstream of string_arc (probe-verified). C4's 106,620 divergences are 100% the
release face, and every one is the downstream shadow of a C2 stake feeding a
return-reaching composite. There is no isolated return-stake slice. Full evidence:
`/tmp/drift-announce/2026-07-09T170000Z-barch1-stop-rescope-return-stake.md`.

PROGRESS (2026-07-09): **B-arch-1a DONE** — call_arg_retain 58,680 → 0 (zero residuals).
**B-arch-1b DONE** — value_position_retain 47,803 → 20,103; C4 106,620 → 82,728 with the
−23,892 converting exactly into c1_agree (boundary releases now ledger-agreed). Remaining:

1. **Field-extraction producers** (20,103 value_position residuals — `throw_self` envelope
   builders + `StrictJsonCursor::field`-style shapes whose String operands come from
   `self`-field reads, not LoadLocal) — extend the producer criterion; closes most of the
   remaining C4 (82,728, same mechanism).
2. **store_value_retain** (7,624) — the deferred store slice.
3. **C1 uninit-release elision** (136,407 + 11,951 path-dependent) — after stakes are done.
4. **C3 flag-guarded cleanup MoveOut modeling** (11,441) — may stay allowlisted.

## 5. Corpus caveats

- Coverage: 543/1,317 e2e cases across both rounds (see Source note). Driver/memcheck test sources are embedded in pytest files and are NOT
  directly compiled by this corpus; their shapes overlap e2e heavily. Extending the
  corpus is cheap (env + file, any driftc invocation).
- Counts are compile-occurrences (stdlib × ncases). Unique-site dedupe would need
  fn-name+point keying across processes — deliberately out of scope for the bounded
  reporter.
- Detail records are capped at 50/class per fn-audit (anti-telemetry bound); all
  COUNTS are exact.
