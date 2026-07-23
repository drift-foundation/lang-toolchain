# Phase D — string_arc.py deletion checkpoint — CLOSED (IMPLEMENTED 2026-07-22/23)

> **STATUS: IMPLEMENTED AND GATED.**  The combined sweep executed under
> the GO of 2026-07-22T185411Z with these binding amendments to this
> checkpoint's proposals:
>   * the pass landed as **`ownership_normalization.py::
>     normalize_ownership_mir`** (driver phase `ownership_normalization`)
>     — NOT the `moveout_zeroing.py` name proposed below (rejected as too
>     narrow for the permanent contract);
>   * the R8 freeze stays **UNCONDITIONAL** — it is a production
>     fail-closed release-placement validator, NOT telemetry; §2.2's
>     "audit-gating as a post-D optimization" remark is RETRACTED;
>   * site-3 observation re-homed through the structured
>     **`Site3Decision`** returned by `site3_return_decision` — one
>     authority result shared by the plan payload and the debug records
>     (never inferred from the drop tuple);
>   * the `local_types` seeding carried as a first-class table-pinned
>     contract (overwrite vs only-if-missing axes), and every
>     non-MoveOut instruction passes through BY OBJECT IDENTITY;
>   * a corpus-wide old-vs-new **shadow differential** gated the
>     deletion (zero divergences across ~1.107M fns; the one sanctioned
>     delta: legacy reconstruction LOST dynamic `span`/`debug_name`
>     metadata that the identity pass-through preserves).
> `string_arc.py` is deleted; final gates: 924 corpus all 14 counters
> +0, memcheck 105/1skip/0 leaks, om matrices 51/51, broad pytest
> battery 3,946 passed.  See `SLICE-B2C-PROGRESS.md` (Phase D section)
> and `doc/history.md` (0.33.87 entry) for the completed architecture;
> the certification full suite remains the single release boundary.
> The body below is preserved as the PRE-IMPLEMENTATION analysis.

Branch `string-arc-endgame-cleanup-authority`; tree = committed S7+S8
closure (S9 CLEAR 2026-07-22T152813Z).  Candidate 0.33.87 / ABI 21.
This was the FINAL architectural checkpoint of the endgame — one
implementation chunk after review, NO intermediate certification, ending
in the single full suite + 0.33.87/ABI-21 certification.  (At writing
time no implementation had been performed; see the closure banner above
for the implemented outcome.)  STOP triggers during implementation:
genuine architectural fork, failed invariant, unexpected
population/counter, LANGUAGE_BUG, ABI/runtime implication.

---

## 1. Complete re-inventory of string_arc.py (1,504 lines, current tree)

Production import surface: **exactly one** — `driftc.py:166
from lang.driftc.stage2.string_arc import insert_string_arc` (every other
production "reference" is comment/docstring prose; §5.4).  Exports:
`insert_string_arc` (`__all__`) + module-level `variant_zero_tag_drop_safe`
(compat shim, test-only consumer).

### 1.1 OUTPUT-BEARING responsibilities (change emitted MIR)

R-labels per the endgame plan; everything else in the file is
output-neutral (proof in §1.3).

* **R1 — entry-block zero-storage initialization** (lines 439–478).
  At entry-block head, for every non-param local, in THREE ordered
  groups, each in `func.locals` order:
  (a) String locals, (b) Array locals, (c) null-safe destructible
  locals — emit `ZeroValue + StoreLocal[synthetic_zero_back]` and seed
  `local_types[zero]`.  THE zero-safety foundation: uninit-path releases
  /drops (R3 null-safe releases, unguarded zero-safe drops, array-header
  drops, null-safe destructible drops) are runtime no-ops ONLY because
  this pass zeroed the storage first.  Non-null-safe destructibles are
  deliberately NOT zero-inited (flag-managed / authored cleanup).
* **R5 — MoveOut expansion** (lines 645–704).  In-place rewrite
  `MoveOut(dest, local, ty)` → `LoadLocal(dest, local) + ZeroValue(zero,
  ty) + StoreLocal(local, zero)[synthetic_zero_back]`, `local_types`
  seeding for dest/zero, String-dest owned/move-only bookkeeping
  (output-neutral), and the AUDIT note `moveout_expansion` with
  `pre_point=(block, ORIGINAL idx)`, `post_point`, and the
  `moveout_feeds_drop` pairing snapshot taken from the SOURCE stream
  (next-instruction DropValue of the dest).  Feeds C3 classification
  (owned/zero-safe/flag-guarded/unreachable ladder) in the reporter.
* **R8 copy-through arm** (lines 599–616).  Recognized pre-materialized
  `StringRelease`s (frozen `R8Recognition`, S6) are copied through
  VERBATIM (same object) with the audit note
  `materialized_lastuse_release` at the original pre_point.  Post-B2+C
  this arm no longer changes output (the release is already in the input
  MIR); its remaining effect is the audit note + the prescan-exclusion
  bookkeeping (output-neutral).

### 1.2 Non-output side effects that MUST survive

* **`local_types` seeding.**  (a) `_seed_dest_types()` →
  `seed_string_dest_types` (library; shared with the plan-window
  recognition wrapper); (b) prescan registration of String
  `ZeroValue`/`ArrayIndexLoad*` dests (518–525); (c) UNCONDITIONAL
  per-arm seeding in the rewrite loop: `LoadLocal` (copies load_ty),
  `LoadRef`, `StructGetField` (ALL types, not only String),
  `VariantGetField`, `ArrayIndexLoad/Unchecked`, `ArrayElemTake`,
  `PtrRead`, `RawBufferRead`, `MoveOut`, plus R1/R5 zero temps.
  Downstream SSA/LLVM lowering may rely on any of these — the
  replacement pass carries the seeding sweep VERBATIM; corpus `.ll`
  equivalence + the full suite prove sufficiency.  **Implementation
  verification item: if codegen turns out to depend on any OTHER
  incidental string_arc effect, STOP (architectural fork).**
* **Audit notes** (only two classes remain authored here):
  `moveout_expansion` (R5) and `materialized_lastuse_release` (R8
  copy-through).  Everything else already migrated (site-4 → counted-only
  in overwrite_cleanup; scope-exit releases + C1 boundaries → frozen C1 +
  driver finalize synthesis).
* **Observe-mode records** (debug-gated, `DRIFT_COMPILER_DEBUG`
  ownership_ledger): the site-3 `string_arc_return` per-destructible
  Return-boundary records (1415–1466), incl. `REASON_DROP_FLAG_OWNED`
  for flag-managed locals — the last observe surface in the file
  (site-4's was re-homed to the planner in S7+S8).
* **`mark_ledger_dirty`** on rewritten blocks/terminators
  ("string_arc.rewrite_block" / "string_arc.rewrite_return_terminator");
  mutation-audit `SCOPED_FILES` lists string_arc.py.
* **`maybe_fresh_ledger(func, "string_arc")`** consumer registration —
  the ledger is consulted ONLY by the Return-branch bookkeeping that
  feeds observe records (post-B2+C); the replacement pass needs NO
  ledger.

### 1.3 Output-NEUTRAL machinery (deleted with the file; corpus proves)

Post-B2+C, `_ensure_owned` is an identity pass-through and every rewrite
branch appends an instruction with IDENTICAL operand values (many as
fresh objects — object-identity churn only; no plan anchor lives on any
of them, see §2.3).  Therefore ALL of the following is bookkeeping with
zero output effect:

* `owned_defs`/`move_only_defs` fn-wide prepass and per-block
  `owned_values`/`move_only_values`/`use_counts`/`producers`;
  `_note_use`, `_can_move_owned_once`, `_is_string_creator`,
  `_can_move_creator_return`; the whole move-vs-"retain" branch pairs in
  StoreLocal/StoreRef/ArrayIndexStore/ArrayElemInit*/ArrayLit/
  Construct*/Exc*/Call*/Return-value arms (both branches emit the same
  instruction).
* Return-branch residuals: `skip_cleanup_locals` /
  `initialized_at_return` rebuild, the destructible MUST_NOT_DROP fold,
  the PATH_DEPENDENT zero-storage widening, `can_move_from_skipped_local`
  alias walk — ALL feed only (a) move bookkeeping and (b) the observe
  records; the emitting authorities (planner `site3_return_drops` /
  `string_return_releases`) already own these decisions via the SAME
  authority functions.  `term.value` can no longer change (identity), so
  the in-place Return update + its dirty mark are structurally dead.
* Dead code (no callers): `storage_locals`, `addr_taken_locals`,
  `_collect_return_source_locals`, `_is_local_name` (constant False),
  the `drift_debug("ssa")` Call prints (debug-only; delete).
* `variant_zero_tag_drop_safe` — compat shim, test-only
  (`test_zero_storage_drop_safe.py`), documented to die with the file.

### 1.4 Test import surface (10 files)

`test_r8_recognition_plan_window`, `test_string_ownership_analysis_
extraction`, `test_string_arc_recursive_type_guard`,
`test_drop_before_overwrite_swap`, `test_zero_storage_drop_safe`,
`test_string_arc_audit_reporter`, `test_move_from_ref_string_arc_
contract`, `test_string_arc_return_swap`,
`codegen/.../test_llvm_codegen_debug_return_span`,
`codegen/.../test_llvm_codegen_string`.  Disposition in §5.3.

---

## 2. Permanent homes

**One new module** (PROPOSAL — landed as `ownership_normalization.py`,
see the closure banner) `lang/driftc/stage2/moveout_zeroing.py` exposing
`expand_zero_init_and_moveouts(func, *, type_table, fn_infos,
audit_collector=None, r8_recognition=None)` — R1 + R5 + the R8
copy-through audit note + the verbatim `local_types` seeding sweep, in
ONE traversal reproducing string_arc's exact emission order.  Rationale:
R1 and R5 are the two surviving mutations, share the zero-back idiom and
the classification inputs, and must run in the SAME driver slot (§3), so
one pass keeps output order trivially byte-identical.

### 2.1 R1 — entry zero-storage initialization

* Population from the SAME classifiers (`classify_string_array_locals`
  + `classify_destructible_locals`/`DropClassifier`) — pure type
  classification: **no ledger, no plan, no anchors needed**.  Emission:
  prepend at entry-block head, three groups in the current order,
  `func.locals` iteration order, params excluded,
  `synthetic_zero_back=True` (stripped later by overwrite_cleanup as
  today).
* Zero-safety proof preserved: same predicate sources
  (`drop_policy_compute.zero_storage_drop_safe` untouched;
  nullsafe classification via `DropClassifier.is_nullsafe_drop`), same
  storage zeroed before any release/drop authority runs, and the pass
  adds **no ledger build** (S7 gate extended — §3/§6).
* Frozen plan anchors unaffected: entry-head insertion shifts indices
  only; anchors are identity-based, consumption accepts changed indices,
  and `validate_unconsumed` runs after the pass (§3).

### 2.2 R5 — MoveOut expansion

* Same in-place rewrite, same emitted quad, same `local_types` seeding,
  same `synthetic_zero_back` provenance.
* **Pre-expansion recognition preserved**: the pass CONSUMES the frozen
  `R8Recognition` (driver-supplied, computed at the plan window over
  original MIR — unchanged S6 slot), with the same closed-vessel
  validation (block-set equality, frozensets, wrong-function) and the
  bare-invocation fallback via the single entry point.  Recognized
  releases are copied through verbatim (same objects) and noted
  (`materialized_lastuse_release`) — recognition's post-D role is
  telemetry + the structural walk; **decision: keep the per-fn driver
  computation UNCONDITIONAL in D**.  (SUPERSEDED at review: R8 is a
  PRODUCTION fail-closed release validator, not merely telemetry — the
  "audit-gating as a post-D optimization" framing here is retracted;
  do not audit-gate it.)
* **Audit placement preserved**: notes carry `pre_point=(block, ORIGINAL
  index)` from a source-stream enumeration (the pass iterates original
  `block.instructions` exactly like string_arc), `post_point=(block,
  len(new_instrs))`, and the `moveout_feeds_drop` next-instruction
  DropValue pairing — so C3's ladder (incl.
  `_is_flag_guarded_cleanup_moveout`, which reads `pre_point[1] == 0` +
  `func._drop_flag_for_local` + CFG preds) classifies identically.
* **Output order preserved**: one traversal, same branch order
  (recognized-release copy-through first, then MoveOut arm, then
  passthrough).  MUST run BEFORE the unified Return authority appends
  the cleanup bands (else original-index bookkeeping and the audit
  pre_points would shift) and BEFORE the audit l_post build (l_post must
  see expanded MoveOuts, as today).

### 2.3 What guards the anchors

R5 inserts instructions and R1 prepends at entry-head; neither removes,
duplicates, reorders, or moves any original anchor object (nullsafe/
site-4 `StoreLocal`s pass through untouched — the destructible arms are
deleted along with all other rebuild churn since the replacement pass
copies EVERY non-MoveOut instruction through by reference, ending the
fresh-object churn entirely).  The driver's `validate_unconsumed` runs
right after the pass (the current post-string_arc call relocates), so a
regression fails closed before the Return authority consumes.

### 2.4 Observe-mode site-3 records (re-home, not preserve-in-place)

The debug-gated `string_arc_return` records re-home into
`destructible_planner`'s Return arm (exactly like site-4's records in
S7+S8): the planner already computes `site3_return_drops`, the skip
fold, flag-managed set, and PATH_DEPENDENT widening through the SAME
authority functions, so the record axes (verdict / reason incl.
`REASON_DROP_FLAG_OWNED` / point at the original return coordinate) are
derivable at the plan slot.  Site tag `string_arc_return` KEPT verbatim
(observe-tooling continuity; the constant's name is historical, like
`drop_before_overwrite`).  Debug-only — no corpus impact; parity pinned
by a unit tooth (§6).

---

## 3. Exact driver sequence (pinned) + zero added ledger builds

Per-fn ownership pipeline after Phase D (changes marked ►):

```
 1. initial ledger build                     (driftc.initial_build)
 2. match_cleanup_authoring  → rebuild      (rebuild_after_match_cleanup_authoring)
 3. drop_flags planning       → rebuild      (rebuild_after_drop_flags; + its internal direct build)
 4. cleanup_authoring (+ in-pass rebuilds)   (cleanup_authoring.in_pass_rebuild)
 5. LEDGER A rebuild                         (rebuild_after_cleanup_authoring)
 6. destructible_plan slot: build_destructible_plan (plan + C1 freeze +
    crosscheck) + R8 freeze + audit collectors; observe site-4 records;
    ► observe site-3 records re-homed here
 7. table-completeness guard (plans/R8/collectors/C1; value type-checks)
 8. ► moveout_zeroing (R1 + R5 + R8 copy-through notes; consumes frozen
    R8Recognition; marks ledger dirty; NO ledger read, NO build)
 9. plan.validate_unconsumed (anchor survival — relocated from the
    string_arc loop; same `destructible_plan`-adjacent containment)
10. return_cleanup (unified Return authority; site3 + string_release)
11. string_arc_audit_finalize (audit only: ONE l_post per fn — sees the
    expanded MoveOuts AND the Return cleanup bands, as today)
12. overwrite_cleanup (nullsafe + site4 + R2/R7; strips transient attrs)
13. ssa → throw_checks → codegen
```

Zero-added-ledger-builds proof: `moveout_zeroing` consults NO ledger
(the only ledger reads string_arc still performed fed the observe
records, which re-home to the plan slot where ledger A is already
fresh).  The S7 instrumented gate is UPDATED in the same chunk: the
consumer zero-delta set swaps `insert_string_arc` →
`expand_zero_init_and_moveouts`, the frozen reason set is UNCHANGED, and
the source pin swaps `string_arc.py` → `moveout_zeroing.py` in the
8-module allowlist.  Boundary containment: the `string_arc` phase wrap
is replaced by a `moveout_zeroing` phase with the same clean-`internal:`
containment; `ledger_cache` consumer name and mutation-audit
`SCOPED_FILES` retarget to the new file.

---

## 4. Counter disposition table

| counter / surface | disposition | mechanism after D | expected 924 delta |
|---|---|---|---|
| `site_class:materialized_lastuse_release` (618,744) | PRESERVED | R8 copy-through note in moveout_zeroing (author-independent, same pre_points) | +0 |
| `site_class:moveout_expansion` (1,852,137) | PRESERVED | R5 note in moveout_zeroing (same pre_point/pairing) | +0 |
| `c3_moveout_owned` / `c3_moveout_zero_safe` / `c3_moveout_flag_guarded` / `c3_moveout_unreachable_block` (1,831,715 / 13,950 / 4,620 / 1,852) | PRESERVED | same notes + unchanged reporter ladder over L_pre | +0 each |
| `site_class:scope_exit_release` (68,562), `c1_agree` (882,371), `c1_path_dependent` (20,384) | PRESERVED | already frozen-C1 + driver-finalize synthesis (S5) — untouched by D | +0 each |
| `pre_post_verdict_drift` (48,178) | PRESERVED | frozen pre-verdict vs the same l_post build point | +0 |
| `site_class:drop_before_overwrite_site4` (14), `site_class:overwrite_release` (233,519) | PRESERVED | overwrite_cleanup (S4/B1) — untouched | +0 |
| `events` (2,772,976), `fns` (1,107,693) | PRESERVED | sums of the above; single finalize per fn | +0 |
| `untagged`, `skipped_no_ledger`, `post_ledger_build_failed`, all DIV_* hard gates | PRESERVED (must stay ZERO) | unchanged reporter | 0 |
| `return_retain_site3` / `call_arg_retain` / `value_position_retain` site classes | RETIRED AUTHORS (already corpus-zero) | the `_ensure_owned` funnel naming them is deleted; reporter constants + fail-closed C2/UNCLASSIFIED classification KEPT (a reappearance still fails loudly) | 0 (unchanged) |
| observe records `string_arc_return`, `drop_before_overwrite` | PRESERVED (debug-only) | planner-slot emission; tags kept verbatim | n/a (off in corpus) |
| `variant_zero_tag_drop_safe` shim semantics | RETIRED | test retargets to `zero_storage_drop_safe` (§5.3) | n/a |
| ledger build reasons | PRESERVED — frozen 5-reason set, zero additional | S7 gate updated to the new pass | n/a (gate) |

**Expected corpus accounting: universe identical, ALL 14 counters +0,
hard gates zero.**  Any delta or partition change = STOP.

---

## 5. Complete deletion inventory

### 5.1 Files / code deleted
* `lang/driftc/stage2/string_arc.py` — entire file, incl.
  `insert_string_arc`, `variant_zero_tag_drop_safe`, the identity
  `_ensure_owned` funnel, all §1.3 machinery, and the module docstring's
  bare-use contract (superseded by the new pass's own contract prose).

### 5.2 Driver (`driftc.py`)
* line 166 import; the `with _timed("string_arc")` loop + its
  boundary-containment arm (phase string `"string_arc"` retired;
  replaced by `moveout_zeroing`); the in-loop
  `validate_unconsumed` relocates to step 9; the
  `audit_collector=`/`r8_recognition=` arguments move to the new pass
  call; `_timed` phase name updated.

### 5.3 Test surfaces (10 import sites + shim test)
* `test_drop_before_overwrite_swap`, `test_llvm_codegen_string`,
  `test_llvm_codegen_debug_return_span`, `test_string_arc_return_swap`,
  `test_move_from_ref_string_arc_contract`,
  `test_string_arc_recursive_type_guard`: pipeline helpers swap
  `insert_string_arc` → `expand_zero_init_and_moveouts` (files keep
  their contract semantics; `test_string_arc_*` file names renamed to
  the surviving contract, e.g. `test_return_swap_pipeline.py` — exact
  renames decided at implementation).
* `test_r8_recognition_plan_window`: consume==fallback + closed-vessel
  teeth retarget to the new pass; the "string_arc no longer owns
  recognition" source pin becomes "ONLY `compute_recognized_releases`
  invokes the three analyses anywhere in production".
* `test_string_arc_audit_reporter`: `_run_arc_audit`/`_run_pipeline`
  helpers retarget; the production-sweep finalize pin (driver sole
  caller) survives unchanged; string_arc-specific source-pin asserts
  (`_audit.finalize` absent in string_arc.py) drop with the file.
* `test_zero_storage_drop_safe`: the compat-shim half is DELETED with
  the shim; the production-predicate half (already targeting
  `drop_policy_compute.zero_storage_drop_safe`) survives, plus its
  "no production caller of the variant-only name" sweep flips to "the
  name no longer exists in lang/driftc".
* `test_string_ownership_analysis_extraction`: "library imports no
  string_arc" pin becomes "string_arc.py does not exist".
* S7 gate + mutation audit: §3.

### 5.4 Prose / comment retargets (residual-reference sweep)
Comment-only `string_arc` mentions in production files (grep at
checkpoint time: `mir_nodes.py`, `ledger_cache.py`,
`match_cleanup_authoring.py`, `ownership_ledger_events.py`,
`overwrite_cleanup.py`, `drop_policy_compute.py`,
`return_cleanup_emitter.py`, `cleanup_authoring.py`,
`string_stakes.py`, `destructible_authority.py`, `hir_to_mir.py`,
`cfg.py`, `drop_flags.py`, `ownership_ledger.py`,
`string_releases.py`, `ownership_ledger_reporter.py`,
`type_checker.py`, `string_ownership_analysis.py`, `cleanup_plan.py`,
`checker/__init__.py`, `llvm_codegen.py`, `destructible_planner.py`,
`driftc.py`) — each retargeted to the final authority
(moveout_zeroing / return_cleanup_emitter / overwrite_cleanup /
destructible_planner / string_releases) or marked explicitly
historical.  `SITE_STRING_ARC_RETURN` constant NAME kept (historical
tag continuity), with a comment noting the emitter is now the planner.
Doc-comment mentions in `doc/` stay historical (release notes /
history.md are provenance, per standing policy).

### 5.5 Explicitly NOT deleted
`string_ownership_analysis.py` (the neutral library), `string_releases`
/ `string_stakes` passes, `destructible_authority`, the frozen-plan
family, both reporters, `SITE_*` constants, the R8 driver freeze, and
the audit finalize lifecycle — Phase D removes the LAST string_arc
responsibilities, not the extracted authorities.

---

## 6. Fail-closed pins (all land WITH the implementation)

1. **R1 coverage/order**: unit tooth — entry-head sequence is exactly
   (strings, arrays, null-safe destructibles) in `func.locals` order,
   params excluded, `ZeroValue+StoreLocal` pairs typed correctly;
   negative: a non-null-safe destructible is NOT zero-inited.  Memcheck
   carriers (uninit-path release/drop classes) re-prove zero-safety at
   runtime.
2. **R5 shape + recognition**: teeth — expansion quad in place with
   dest/zero typing; recognized releases copied through by IDENTITY
   (same object) and never expanded/duplicated; consume==fallback
   identical output MIR; closed-vessel (missing/extra block, malformed,
   wrong-function) rejections; audit pre_point = ORIGINAL index and
   `moveout_feeds_drop` pairing tooth (paired vs unpaired).
3. **Plan-anchor survival**: driver `validate_unconsumed` immediately
   after moveout_zeroing (relocated call) + the existing
   replaced/moved/vanished-Return and store-anchor teeth now exercised
   across the new pass.
4. **Counter continuity**: the 924 exact-delta gate (all 14 counters
   +0, hard gates zero) vs the accepted `build/tmp/s7s8` baseline; plus
   the audit-reporter byte-identity suite (split-vs-monolithic) which is
   agnostic to the note author.
5. **No production string_arc import / file absence**: tooth asserting
   `lang/driftc/stage2/string_arc.py` does not exist AND
   `importlib.util.find_spec("lang.driftc.stage2.string_arc") is None`;
   production-tree sweep asserting zero `string_arc` import statements
   (prose mentions allowed only with the §5.4 retargets).
6. **S7 gate continuity**: updated consumer set (new pass wrapped,
   zero-delta), frozen 5-reason equality unchanged, source-pin allowlist
   swaps string_arc.py → moveout_zeroing.py; negative teeth unchanged.
7. **Observe parity** (debug-only): unit tooth on a fixture comparing
   the planner-emitted `string_arc_return` record fields
   (site/verdict/reason/point incl. a flag-managed local →
   `REASON_DROP_FLAG_OWNED`) against the expected axes.

---

## 7. Acceptance sequence (one chunk, in order)

1. Implementation + all §6 pins + focused touched battery green.
2. **Residual-reference sweep**: `grep -rn string_arc lang/` → zero
   import/call sites; every surviving mention is a §5.4-retargeted or
   explicitly-historical comment; `work/` references untouched
   (ephemeral).
3. **924 corpus audit** vs `build/tmp/s7s8`: exit 0, universe identical,
   **exact accounting — all 14 counters +0**, hard gates zero.
4. **Full memcheck** (105+1 expected, 0 leaks) **+ ownership matrices**
   (the standing matrix suites from the endgame gate set).
5. Static delta review of the Phase D chunk.
6. **The ONE full serial suite** (maintainer-run per standing policy) →
   **0.33.87 / ABI 21 certification** — the single endgame boundary; no
   version/ABI change beyond the already-staged 0.33.87 candidate
   number; ABI untouched (compiler-internal refactor only).

---

**Checkpoint CLOSED — the review GO (2026-07-22T185411Z) resolved the
decision points below and the sweep is implemented and gated (see the
banner at top).**  Open decision points surfaced for the reviewer:
(a) `moveout_zeroing.py` module/entry-point naming; (b) keeping the R8
driver freeze unconditional vs audit-gated (recommendation: keep
unconditional in D); (c) test-file rename map (§5.3) — proposals at
implementation time; (d) the §1.2 verification item (incidental
`local_types` seeding sufficiency) is a declared STOP trigger if codegen
depends on anything beyond the carried sweep.
