# STRING-ARC ENDGAME — resume checkpoint (report-only, rev 2)

Status: STOPPED before implementation. Rev 2 folds in the six
corrections from the 2026-07-20T060714Z static review (R4 retired
retain-wrap premise; R5 ordering contradiction resolved; R6 marked
unmeasured with a baseline gate; consumer graph corrected and made
reproducible; 2b under LANGUAGE_BUG process; flocker premise
refreshed). Per the review, no new endgame arm-selection review is
needed; the first implementation candidate remains a separate
checkpoint after certification state is recorded. This document is the resume
point for the string_arc endgame after the array-sweep and
flag-retirement slices. It deliberately does NOT build on
`NEXT-PHASE-PLAN.md` — that is the COMPLETED Scope-A/0.33.70-era plan,
historical record only; the standing operational frame is
`CLEANUP-EXECUTION-PLAN.md`'s "B-repr handoff / entry criteria"
section plus the slice log in PROGRESS.md.

## 1. Compiler / ABI / certification assumptions (refreshed 2026-07-20)

- CERTIFIED toolchain: **0.33.85 / ABI 21** — deployment run
  20260719-153613-drift-lang-79bbad3 (commit 79bbad34, array-sweep
  retirement). 0.33.86/ABI 21 (Arm B, 4c7767d6) is NOT yet
  certified; it gates MERGE/RELEASE of downstream branches, not
  development (process compression, 2026-07-20).
- In-tree candidate: **0.33.86 / ABI 21**, containing two
  commit-cleared, statically reviewed slices stacked on 0.33.84:
  0.33.85 (Return-boundary array sweep retirement +
  `zero_storage_drop_safe` extraction + A1 guard deletion) and
  0.33.86 (zero-storage-safe drop-flag retirement, Arm B). The FULL
  SERIAL SUITE is RUNNING at checkpoint time; certification of
  0.33.86 is the working assumption for everything below. Corpus
  phase reference: `build/tmp/flagret` (+ `flagret-struct` for the
  structural table).
- Test infra: flocker **0.2.1** (corrupt-completed-pool fail-closed,
  TEST_INFRA_BUG) is COMMITTED ON MAINLINE — implementation commit
  0b5a483b (bin/flocker + bin/flocker_test.sh + doc/flocker.md);
  648749ed is the PROGRESS.md log entry only. It gates the
  certification runner's lanes and stays classified as test
  infrastructure — outside compiler payload and versioning.
- ABI: 21 throughout the endgame — every remaining responsibility
  migration below is compiler-internal. The FIRST ABI movement on
  this track is B-repr(B5) itself (21 → 22 + pool recert +
  DriftQuery recompile coordination), per the pinned B5 decisions in
  SCOPE-B-PLAN.md §10.2.1.

## 2. The three small recorded follow-ups — individual dispositions

### 2a. Projected-place scrutinee audit
Origin: 0.33.83 E-fix — by-value match of a non-Copy PLACE scrutinee
became TRACKED-consuming, but projected scrutinees (`match
self.field`) kept prior behavior under the partial-move rule and were
flagged for audit.
DISPOSITION: its own report-only audit checkpoint (checker/borrow
surface — zero coupling to string_arc). Not an endgame blocker; can
run any time after 0.33.86 certifies. Deliverable: enumerate
projected-scrutinee shapes in stdlib+fixtures, classify against the
no-partial-moves doctrine, recommend keep/reject per shape.
STOP-AND-CLASSIFY rule: if the audit exposes a semantic defect, it
is a LANGUAGE_BUG on the spot — stop, classify, and hand it to a
regression-first fix slice with a `doc/refactor_triggers.md` scan
(0.33.83 taught that this class hides zero-read bugs AND wide
stdlib fallout).

### 2b. ConstShare synthesis visibility
Origin: E-fix side-finding (probe-verified,
E-POPULATION-TRIAGE.md): ConstShare synthesis qualifies fields
against the DECLARING module's import-visible world — an import-less
module silently cannot derive ConstShare for its error types.
DISPOSITION: a **LANGUAGE_BUG** (probe-verified silent capability
loss) — full repository bug process applies: the slice BEGINS
regression-first (failing pins from the recorded probe), scans
`doc/refactor_triggers.md` for trigger applicability before coding,
and files/updates its intake. Priority is the maintainer's.
Unrelated to string_arc; NOT sequenced on the endgame. Recommended
timing: alongside 2a as a small checker batch.

### 2c. Spec wording for the match-consume exception
Origin: the 0.33.83 ruling — bare `match r` is the language's ONE
deliberate implicit-consume position (recorded in
doc/refactor_triggers.md; user-visible semantics shipped and
MIGRATION-documented in history.md).
DISPOSITION: doc-only; the smallest of the three. Write the
effective-drift/spec prose (no version/ABI stamps in spec text, per
the standing convention — provenance stays in release notes +
history). Can ride any next slice or land standalone; zero risk.
RECOMMENDED FIRST — it closes the follow-up list's paper debt
immediately.

## 3. string_arc responsibility & consumer audit (the endgame ledger)

Quantities from the current candidate's corpus reference
(build/tmp/flagret, 924 fixtures). Every responsibility below must be
re-homed, retired-with-proof, or explicitly modeled under another
authority before `string_arc.py` can be deleted (B5 entry criterion 1).

| # | Responsibility | Corpus footprint | Re-home candidate / notes |
|---|---|---|---|
| R1 | Entry-block zero-init: String locals, Array locals, nullsafe destructibles | every fn with such locals | THE zero-safety cornerstone — B-M/B-U and the flag retirement's unguarded drops all cite it. Must move (HIR→MIR lowering or a dedicated init pass) BEFORE file deletion; deleting it un-proves the zero-storage no-op chain. |
| R2 | String overwrite releases at StoreLocal/StoreRef/ArrayIndexStore (+ MoveFromRef old-value release) | overwrite_release 233,519 | The largest live emission family. Candidate: pre-pass sibling of string_stakes (release half of the same boundary) or ledger-driven authoring. Own predicted-delta slice; memcheck heavy. |
| R3 | Scope-exit String releases + MUST_NOT_DROP elision fold + `_release_all_locals` | scope_exit_release 68,562 | The "strings to ledger authority" track — attempted 2026-04-25 and REVERTED (0.27.145-class); the String alias-walk skip (R4) is load-bearing here. Migrate together with R4/R8. |
| R4 | String return-source alias walk + consume bookkeeping at Return (`can_move_from_skipped_local`, `_can_move_creator_return`, move approval) | per-Return | String-only since the array half's removal. CORRECTED (rev 2): the old "return-value retain-wrap" premise is RETIRED — `_ensure_owned` is an identity pass-through and `return_retain_site3` is structurally extinct and fail-closed in the reporter; no retain is emitted at Return. The live surface is the alias-walk SKIP (keeps the returned local out of scope-exit release) and move-approval bookkeeping. The 0.27.145 re-proof for any R3 migration restates as: with stakes materialized UPSTREAM (string_stakes) and ownership ledger-visible, show that replacing the alias-walk skip with a ledger verdict cannot re-release a returned local on any path (the historical breaker was the pre-B-arch late-retain model; the proof must be made against the CURRENT model, not the retired one). Adjacent STALE retain-wrap comments in string_arc.py (e.g. the "Why strings/arrays are NOT here" block claiming a StringRetain wrap at return) are recorded as cleanup OWNED BY the R3/R4 slice. |
| R5 | MoveOut expansion (LoadLocal + ZeroValue + StoreLocal + audit note + `moveout_feeds_drop` snapshot) | moveout_expansion 1,852,137 | Structural backbone; every pass and the C3 ladder depend on its shape. ORDER RESOLVED (rev 2): R5 migrates LATE — after R3/R8 — so the recognition/C3 machinery it feeds is already re-homed and stable when the expansion moves. HARD GATES for the R5 slice: `string_releases` must keep seeing the PRE-expansion `MoveOut` producer (TLR-8 family semantics); materialized-release recognition/placement, the `moveout_feeds_drop` snapshot, and the C3 audit shape must be byte-identical. "Promote into HIR→MIR" is NOT pre-approved — the R5 checkpoint must carry an explicit pipeline-shape proof before any home is chosen. |
| R6 | Destructible Return-boundary drops (site 3, `_drop_all_destructibles`) + zero-storage widening + site-4 drop_before_overwrite + `_drop_destructible_local` | site4 14; **site-3 UNMEASURED** — the Return-boundary destructible drops have NO aggregate counter today | Already pure ledger authority (Tier 1). Natural fold into cleanup_authoring (it owns hooks + the ledger). CORRECTED (rev 2): "smallest emission family" is NOT established — only site 4 is measured. The FIRST GATE of the R6 checkpoint is an exact site-3 baseline (counter + per-(fn, local, block) itemization, bijection-grade, the arraydrop-measurement pattern) before any migration claim. |
| R7 | Array overwrite drop (`_drop_array_local`, StoreLocal path) | per overwrite | Sibling of R2; last array responsibility besides R1. Folds with R2 or into cleanup/ledger authority. |
| R8 | Materialized-release recognition/copy-through + fn-wide producers + temp liveness | materialized_lastuse_release 618,744 | Pairs with string_releases (the pass authors; string_arc recognizes). When R3 migrates, recognition moves to the new String authority; the shared analyses go to R10's library. |
| R9 | Dead-but-greppable surfaces: `_ensure_owned` identity funnel (13 call sites, site-class taxonomy), `variant_zero_tag_drop_safe` compat shim (tests-only, AST-pinned). (`consumes_string_operand` RETIRED in Slice A — deleted; its dispositions-contract prose migrated to the R10 library.) | none | Delete WITH the file; the AST pins + the four retain hard gates outlive them in the reporter/tool. |
| R10 | **DISCHARGED (Slice A, 2026-07-20)** — the eight shared analyses (+ `_analyze_lastuse_block`, `_is_semantic_string_tid`, `DISPOSITION_*`, `DRIFT_STRING_HELPER_SYMBOLS`) EXTRACTED verbatim to `string_ownership_analysis.py` | n/a | Neutral library; string_arc + string_releases both consume it, neither imports the other. Corpus +0, IR byte-identical, two fail-closed AST pins (module-import + attribute-access escapes closed). |

CONSUMERS (import graph, current — reproducible; rev 2 corrects
direction and counts):
CURRENT (post-Slice-A) import graph:
- `string_ownership_analysis.py` (NEW neutral library) — imported by
  BOTH `string_arc.py` (back-imports the six names its remaining
  emission code references) and `string_releases.py:76` (one import
  statement, five unique names); the module imports only
  {mir_nodes, cfg, types_core, checker, function_id, typing} and
  MUST NOT import string_arc (AST-pinned).
- `string_arc.py` — imported by `driftc.py:166` (`insert_string_arc`
  + the boundary-wrap containment, pinned) and the six test files.
  `string_releases` NO LONGER imports string_arc (fully decoupled by
  Slice A). `consumes_string_operand` deleted.
- string_arc's own DEPENDENCIES: `string_ownership_analysis` (R10
  library), `drop_flags.is_flag_managed`,
  `drop_policy_compute.{compute_drop_policy, zero_storage_drop_safe}`,
  ledger_cache/ownership_ledger/reporter modules.
- Tests, with the queries: files importing string_arc or naming its
  module path
  (`grep -rl 'from .string_arc import\|from lang.driftc.stage2.string_arc import\|stage2.string_arc' lang/tests --include='*.py'`)
  = **15 files**; files containing the string "string_arc" at all =
  **44** (docstrings/comments included). The prior "27" was not
  reproducible and is retired.
- The reporter has NO string_arc import (audit objects are
  constructed inside the pass).
Also carried forward as B-repr planning input:
`pre_post_verdict_drift` 48,178 (modeling artifact, characterized
2026-07-11) and `c1_path_dependent` 20,384 (kept unconditional —
no string drop-flags, standing decision).

## 4. B-repr(B5) entry criteria — scoreboard

1. **string_arc deleted / responsibilities explicitly modeled** — IN
   PROGRESS: §3 is the explicit model; **R10 DISCHARGED** (Slice A,
   2026-07-20 — analysis library extracted, string_releases
   decoupled, consumes_string_operand retired); R1-R9 remain, driven
   by the compressed B/C/D sequence in §5.
2. **C3/E closed; `c3_moveout_not_owned` true zero + HARD GATE** —
   **DISCHARGED** (2026-07-13; shapes 1-2 source-fixed in 0.33.83 +
   shape-3 reporter rule; gate promoted in tool v1.5.0; zero through
   every corpus run since).
3. **Array release-elision landed or consciously deferred** —
   **DISCHARGED AND SUPERSEDED**: landed 2026-07-14 (shipped in
   0.33.84), then the whole Return-boundary sweep was retired in
   0.33.85 — arrays now sit entirely under cleanup_authoring's
   authority with the flag retirement (0.33.86) completing the
   zero-storage doctrine.
4. **Memcheck + ownership matrices clean on the phase-exit tree** —
   STANDING (memcheck 105+1skip on the 0.33.86 candidate; matrices
   green at their last gates). Re-verify at actual phase exit, as
   specified.

## 5. Recommended resume order (proposal only — nothing started)

1. Follow-up 2c (spec prose) rides the next doc touch; 2a/2b
   scheduled as an independent checker batch at maintainer priority.
2. 0.33.86 full suite → certification (in flight).
3. COMPRESSED SEQUENCE (maintainer, 2026-07-20 — supersedes the
   original per-R ladder; certification happens at
   BEHAVIOR-CHANGING release boundaries, not after every mechanical
   refactor):
   - **A**: R10 extraction + dead-analysis/comment cleanup
     (consumes_string_operand deletion, stale late-retain authority
     wording) — THIS slice (mechanical; no cert cycle).
   - **B**: R6 + R2/R7 cleanup-authority migration (FIRST GATE
     unchanged: the site-3 baseline measurement; behavior-changing →
     release boundary).
   - **C**: R3/R4/R8 String return/scope authority (the 0.27.145
     re-proof against the CURRENT upstream-stake/ledger model;
     behavior-changing → release boundary).
   - **D**: R5 + R1 + final `string_arc.py` deletion — ONLY if D's
     own checkpoint proves that bundle safe (R5 keeps its
     pipeline-shape gates; R1 re-home keeps the zero-safety chain
     intact through the transition).
4. B-repr(B5) DESIGN-FIRST entry per the pinned §10.2.1 decisions
   (ABI 21 → 22, pool recert, DriftQuery coordination), with
   `pre_post_verdict_drift` as the clean modeling input.

Each numbered step above is its own checkpoint→review→implement
cycle with predicted-delta acceptance; none is authorized by this
document.

## 6. STOP

Report-only. No implementation, no code changes, no reordering of
the maintainer's queue. Awaiting review.
