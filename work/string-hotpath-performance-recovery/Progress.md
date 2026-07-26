# string-hotpath-performance-recovery — Progress

> **SUPERSEDED-CLAIMS NOTE:** entries in the first section below
> contain claims corrected by the 2026-07-26 review and checkpoint
> rev 2.  Specifically SUPERSEDED: "36 getenv calls per request"
> (exact multiplier is 27 parse / 21 route — tombstone/static/immortal
> releases return before getenv); the "~80% / 15-20%" attribution
> split (the trace tax alone EXCEEDS 100% of the net regression;
> compiler-side improvements conceal part of it); branch-lean
> validation and length-first equality as recommendations (REJECTED —
> validation and eq/cmp stay exactly as-is); "Stage 2" SSO (DEFERRED
> as an optional future project, no implied certification); and any
> mention of a separate "0.33.88 recovery certification" (target is
> the COMBINED 0.33.89/ABI-22 regex + String candidate, one baseline
> promotion, one certification).

## 2026-07-26 (initial measurement round — see SUPERSEDED-CLAIMS NOTE)

- [x] Slice opened on the user-created branch (README.md with the
      full phase contract; tree carries the parked regex WIP commit
      75a7d53a — its 0.33.89 stamp is NOT this slice's).
- [x] Evidence ingested: drift-web pin_bench_test.drift (read from
      their checkout), orchestrator history, compiler-team
      correction (ABI 21 already atomic; allocation size unchanged;
      suspect = validation + lifecycle).
- [x] Both runtime versions read end-to-end: ABI-21
      (205a0120~1) vs current — ABI-22 adds per-op validate (atomic
      flags load + 3 invariant chains), eq/cmp validate-both-before-
      length-reject, retain overflow check, and a PER-CALL
      getenv("DRIFT_STR_TRACE") in release (ABI-21 release had no
      trace hook; retain had one in both).
- [x] Dual-toolchain bench built (bench/prim_bench.drift — compiles
      unchanged on certified 0.33.87 and mainline; API probe first).
      Reduced durable carriers imported: request parse
      (9 substring tokens + 3 header-name eqs per request) and route
      (split + 3 segment eqs), matching drift-web's idiom; controls:
      string_byte_at scan + Array<Byte> scan.
- [x] Ablation runtimes built from mechanical copies (recipes in the
      generator command history; artifacts in scratch): ab1 =
      getenv removed from retain/release; ab2 = ab1 + ABI-21-shaped
      lean checks on retain/release/eq/cmp/concat; ab3 = branch-lean
      FULL-validation control (env cached once; one combined cold
      branch; eq length-first).
- [x] 5-way interleaved matrix (5 launches, medians): full table in
      checkpoint §1.  ATTRIBUTION PINNED: ~80% of the lifecycle
      regression is the per-call getenv (clone+drop pair 37.6 ->
      7.7 ns removing it alone — 2.5x FASTER than 0.33.87's 19.5,
      which pays getenv in retain); validation is the ~15-20% rest;
      branch-lean control (ab3) keeps full fail-closed coverage
      within ~7% of fully-lean.  Carriers under ab3: parse 68.1 ms /
      route 164.4 ms vs certified 132.2 / 337.0 — ~2x BETTER than
      the recovery bar.  Byte-scan improvement preserved (3.2x).
      Carriers reproduce web's report (+57%/+69% vs their +56%/+73%).
- [x] Counts + histogram (wrap driver, bench/hist_driver.c +
      hist_ops.drift): parse = 9 materializations, 9 retains, 36
      RELEASES per request (the getenv multiplier); route = 3/10/19;
      empty split-segment reuses the singleton (keep).  Histogram:
      parse tokens 1..16 B (16 exactly once: "application/json"),
      route segs <=6 B; <=15 B covers 8/9 parse tokens + all route
      segs.
- [x] Representation head-to-head (bench/repr_proto.c, C floor,
      caveats stated): branch-lean 5.2 ns/token; tagged 5.0 (~3%);
      SSO-15 3.7 (~29% better, clone/eq of short strings ~free);
      SSO+tagged 3.6.  In-system dilution estimated to ~10-15%
      additional carrier win for SSO (runtime primitive is ~1/4 of
      in-system token cost).
- [x] STRING-HOTPATH-PERFORMANCE-CHECKPOINT.md WRITTEN — corrected
      attribution (§1), counts/histogram (§2), head-to-head (§3),
      recommendation (§4): Stage 1 = branch-lean ABI-22 recovery
      (trace-env cached, one-branch validate, eq length-first,
      pointer-identity eq fast path; runtime-only, ABI 22 retained)
      unblocks certification with ~2x margin; Stage 2 = SSO-15 +
      tagged heap (ABI 23) recommended as its OWN deferred phase
      with the full §5 closure list; acceptance gates (§6) incl.
      durable tools/perf protocol + corpus zero-delta; consolidated
      plan (§7) incl. regex-branch resumption and ONE final
      certification.  **STOP — awaiting design review.  No
      implementation, no version stamps.**

## 2026-07-26 — review round: 10 corrections, checkpoint rev 2

- [x] C1 trace accounting CORRECTED: classified wrap counts +
      --wrap=getenv keyed to DRIFT_STR_TRACE.  Parse: 9 heap retains
      + 18 heap releases + 18 TOMBSTONE releases (which return before
      getenv) => exact multiplier 27 getenv/request (not 36); route:
      21/call.  Implied ~18-20 ns per getenv on this host.
- [x] C2 attribution restated in ABSOLUTE terms that close: parse net
      +283 ns/req = trace +475 + cached-branch +4 + retained-
      validation +2 - concealed 0.33.88 wins -199 (route: +243 =
      +414 +7 +4 -183).  Trace tax alone EXCEEDS 100% of net; the
      "80%/15-20%" split is retracted.
- [x] C3 factorial 2x2 {cached,none} x {current,branchlean} + stock
      ends + lean_ref, 7 sides x 5 interleaved launches: validation
      restructuring shows NO carrier benefit at fixed trace policy
      (parse 67.3 vs 68.1 ms; route 164.1 vs 165.4 — slightly worse).
      WINNER = cached_current (launch-time trace cache, validation
      UNCHANGED — a ~3-line runtime diff).  Parse 1.86x / route 2.04x
      faster than certified 0.33.87.
- [x] C4 trace design pinned: constructor-time immutable init, doc
      note (set env before launch), FILTER on slow path only,
      enabled+disabled teeth, no per-call pthread_once.
- [x] C5/C6 eq contract: length-first WITHDRAWN (validate both
      operands first, everywhere); cmp length-first claim corrected
      (must compare shared prefix); pointer-identity fast path NOT
      added (measured ceiling ~0.5 ns/op on 71 B same-handle, none
      at 7 B); ALL legality checks incl. NUL-cache coherence stay —
      moot since validation is not restructured at all.
- [x] C7 evidence checked in: bench/gen_ablations.py + ablations/*.c
      + run_matrix.py (with provenance) + results/matrix-*.json +
      results/counts-*.txt + repr_proto.c + carriers.
- [x] C8 carrier claim reworded: relative regressions reproduced
      closely; absolute times differ; drift-web's own gate is part
      of final validation.
- [x] C9 SSO-15 + tagged heap: FUTURE OPTIONAL representation
      project, not a stage of this recovery.
- [x] C10 sequencing corrected: combined 0.33.89/ABI-22 candidate;
      corpus NOT globally zero-delta (regex modal delta + new pin
      fixture stand as already attributed); String runtime change
      must add ZERO delta vs current main (to be verified); regex
      HOLD closure + ONE combined baseline promotion + ONE
      certification.
- [x] Checkpoint rev 2 written.  **STOP — awaiting design review.
      No implementation, no version stamps.**

## 2026-07-26 — residuals closed + implementation GO

- [x] R6 records repaired: README supersession note (accepted design
      + combined 0.33.89 target); Progress SUPERSEDED-CLAIMS note.
- [x] R2 gen_ablations hardened: replace_exactly_once fail-closed on
      unexpected source shape; --check reproduces preserved ablation
      sources byte-for-byte; evidence base sha256 PINNED (post-fix
      the tree base legitimately differs; preserved sources stay the
      frozen decision evidence, reproducible from the evidence
      commit).
- [x] R1 run_matrix rev 3: builds EVERY side from checked-in sources
      into a configurable fresh temp dir; records both driftc
      identities, C compiler, exact build/link/run commands, sha256
      of all sources+binaries; fail-closed on missing sides/rows/
      hash drift; DRIFT_STR_TRACE{,_FILTER} scrubbed from timing
      env; per-launch side order deterministically shuffled with the
      seed + orders recorded.
- [x] R3/R4/R8 checkpoint wording: raw 27x1000+1 / 21x1000+1 with
      setup separated from normalized steady-state 27/21; the
      0.33.87-vs-lean term relabeled an AGGREGATE RESIDUAL (bulk
      substring plausible but not independently attributed); gates
      restated as same-host interleaved paired ratios (absolute
      timings evidence-only).
- [x] IMPLEMENTED the accepted winner in
      lang/language_runtime/string_runtime.c: drift_str_trace_on +
      constructor init; retain/release branch on the cached flag;
      documentation (presence semantics, set-before-launch, FILTER
      on slow path, regression provenance).  Validation, eq/cmp,
      layout, ABI 22: untouched.
- [x] R5 trace-contract teeth GREEN
      (lang/tests/driver/test_string_trace_cache.py, normal + debug
      archives): exactly ONE init lookup; launch-unset+setenv stays
      disabled; launch-set+unsetenv stays enabled; presence(=0)
      enables; filter only on enabled path; 4-thread retain/release
      hammer crash-free with exact final refcount.
- [x] Durable protocols: tools/perf/string_hotpath_bench.drift
      (allocation-heavy String carriers) + tools/perf/regex_bench.drift
      (RESUME-STATUS §5.2) wired into `just perf-protocols` (trace
      env scrubbed in the String rows).
- [ ] Acceptance matrix (reproducible rev-3 runner; fixed runtime =
      cur side) — pending after the regex hot-loop ablations finish
      (no concurrent load during timing).
- [ ] Regex HOLD §5.1 ablations in flight; §5.3 attribution hardened;
      §5.4 wording done.  Corpus + combined report after.
- [x] Combined-candidate corpus: fresh run
      ownership-corpus-combined-20260726-102955-3365314 (the §5.4
      fixture-comment fix invalidated the earlier retained run's
      universe hash).  Hardened fail-closed attribution PASSES:
      universe exactly baseline + the one pin fixture, populations
      unchanged, zero pre-existing hash changes, modal {-3,-3,-3} x
      924 with zero outliers, residual ZERO, hard gates zero —
      byte-identical attribution to the pre-String-fix run, proving
      the runtime change adds ZERO ownership delta vs current main.
- [x] Acceptance matrix (rev-3 reproducible runner; first attempt
      fail-closed on my own EXPECTED_ROWS miscount 19 vs 18 — fixed):
      implemented runtime tracks cached_current within noise on all
      rows; paired ratios vs certified 0.33.87: parse 0.533, route
      0.483, clone+drop 0.411, concat 0.434, construct 0.51-0.56,
      byte-scan 0.373 (win preserved), statics <=1.16, eq/cmp
      1.10-1.36 — ALL bands met EXCEPT eq_unequal_len 1.64 vs 1.45,
      FLAGGED for adjudication (identical-semantics build pair
      differs 30% on this sub-ns row in the same matrix = layout
      noise; validation code byte-unchanged).
      results/matrix-20260726T170248Z.json.
- [x] Focused teeth on the combined candidate: regex scratch counts +
      string-view counts + trace cache (normal+debug) 4/4; std_regex
      e2e 16/16 (incl. the comment-edited fixture).
- [x] FINAL combined static-review report:
      /tmp/drift-announce/2026-07-26T170719Z-combined-0.33.89-string-
      regex-review.md.  Asks: static review of the delta, adjudicate
      the eq_unequal_len flag, promote the combined corpus run, user
      runs run-all-tests.sh + drift-web validation, ONE 0.33.89/ABI-22
      certification.  Open items on my side: none.

## 2026-07-26 — final static-review round: 5 proof/test/doc blockers

CORRECTIONS to earlier entries in this file: the prior claims that
"--check reproduces byte-for-byte" (it was vacuous after the fix
landed), that "exact final refcount" was asserted (it was only a
comment), and that all gates were closed, were PREMATURE — closed for
real below.

- [x] B1 gen_ablations --check now ALWAYS enforces: the pre-fix
      runtime source is preserved at
      ablations/evidence_base_string_runtime.c, hash-verified
      (4135...14df) on every load; all five ablations regenerate from
      it and compare byte-for-byte regardless of tree state.
      NEGATIVE-TESTED: a tampered preserved file fails --check (rc 1).
- [x] B2 trace teeth rewritten and split: output-contract test uses a
      TINY workload (20-op churns) with a POST-CHANGE-ONLY marker
      String ("post-flip-marker" — required present after
      launch-set+unsetenv, required absent after launch-unset+setenv);
      the concurrency hammer runs trace-disabled AND
      enabled-with-nonmatching-filter (slow path exercised, output
      suppressed), asserts the final refcount ATOMICALLY in-driver
      (storage->strong == 1, exit 71 otherwise), and pins the filter
      lookups EXACTLY (1,600,001 = 2 x 4 x 200k + the final
      construction-stake release).  Wrap counters made _Atomic after
      the first run exposed 4-thread lost updates (907,826 counted vs
      1.6M actual).  4/4 pass in ~1 s (was 80 s with ~1.6M traced
      backtraces).
- [x] B3 attribute_corpus fully fail-closed: per-fixture loader
      rejects malformed/missing/multiple aggregate records; baseline
      RUN dir must match the checked-in baseline on aggregate AND
      manifest; excluded compared by name AND reason; modal delta
      must be EXACTLY {events:-3, c3_moveout_owned:-3,
      moveout_expansion:-3} on ALL shared fixtures with ZERO
      outliers; new-fixture contribution pinned to the reviewed
      dict.  Re-verified green on the combined run.
- [x] B4 hotloop runner hardened (seeded shuffled side order +
      recorded, trace env scrubbed, exact row/side-set validation,
      full per-launch results + provenance JSON saved to
      bench/results/) and RERUN to produce the preserved artifact;
      justfile perf-protocols regex rows now also scrub
      DRIFT_STR_TRACE{,_FILTER}.
- [x] B5 records: 0.33.89 history now carries the String trace-cache
      recovery entry with measured web-carrier impact; checkpoint
      rev 3 = IMPLEMENTED/CLOSED with the reviewer's eq_unequal_len
      adjudication RECORDED (drift_string_eq
      instruction-for-instruction identical, 0xf5-byte body, only
      placement differs; ~0.46 ns/op inside the +1 ns envelope; the
      1.45 ratio band RETIRED for sub-ns rows, +1 ns envelope +
      carrier gates retained; no alignment tweak); README layout
      section updated to the checked-in bench reality.
- [x] Prose closure round: checkpoint trailing STOP replaced with the
      IMPLEMENTED/CLOSED disposition; §8 sub-ns ratio band formally
      retired (absolute +1 ns envelope + carrier gates remain); §9
      marks the regex HOLD closed; evidence text names the preserved
      hash-pinned ablation base and BOTH matrices (decision
      155503Z, final acceptance 170248Z).  README live status +
      layout + tree-state note updated.  Regex Progress +
      RESUME-STATUS carry closure/supersession addenda.  Closure
      announcement published:
      /tmp/drift-announce/2026-07-26T190000Z-combined-0.33.89-
      closure.md (supersedes the 17:07 report and its resolved
      adjudication ask).

## 2026-07-26 — promotion tooling (pre-promotion, per maintainer)

- [x] tools/drift_corpus_promote.py: materializes an ALREADY-APPROVED
      promotion, cannot bless its own input.  Dry-run default,
      --apply required; run dir explicit + hash-pinned (never
      "latest", never generates); approval file pins predecessor +
      candidate artifact sha256s, exact universe change, exact
      nonzero counter deltas; fail-closed on stale predecessor,
      candidate mismatch, universe divergence, unexplained deltas,
      hard gates, malformed data; writes ONLY the four baseline
      files; BASELINE.md regenerated (provenance incl. approval-file
      hash + approver, predecessor, approved deltas, attribution);
      post-write exact zero-delta comparison mandatory.
- [x] Teeth: lang/tests/tools/test_ownership_corpus_promote.py —
      10/10 (dry-run writes nothing; apply writes only the four
      files + zero-delta; wrong run dir / stale predecessor /
      candidate hash mismatch / unexplained delta / unexpected
      universe / hard gate / malformed approval / zero-in-expected
      all rejected).  Full lang/tests/tools lane green.
- [x] just ownership-corpus-promote RUN_DIR APPROVAL *FLAGS — thin
      wrapper; NOT referenced by test/certify/run-all-tests.sh
      (verified).
- [x] DRAFT approval for the combined promotion written with all
      hashes + the 13 exact deltas computed:
      approval-combined-0.33.89-DRAFT.json (approved_by =
      PENDING-REVIEW placeholder — the approval act is the
      maintainer's).  DRY-RUN demonstrated GREEN on the real
      candidate via the just recipe.
- [x] Promotion-tool fail-closed hardening round (5 gaps):
      (1) explicit approval status — dry-run tolerates
      pending/placeholder with a warning; --apply requires
      status=approved + non-placeholder reviewer + non-DRAFT
      filename; (2) counter-KEY-SET identity enforced — zero-valued
      key additions/removals are schema changes requiring explicit
      counter_keys_added/removed approval; (3) universe exactness —
      inclusion_rule must be unchanged, excluded-population changes
      unsupported (excluded_changed:true itself rejected), fixture
      integrity checks (unique names, disjoint partitions,
      fixtures = compiled ∪ failed) on both sides; (4) staged +
      rollback-protected --apply (staged baseline must pass
      zero-delta BEFORE replacement; backup/restore on failure;
      interrupted-run residue fails closed); (5) wiring isolation is
      now a TOOTH (promote referenced only inside its own justfile
      section; absent from test:/certify: lines and
      run-all-tests.sh) plus malformed-corpus-data teeth (invalid
      JSON + schema-invalid universe).  Teeth: 23/23; full tools
      lane green.  doc/ownership-corpus-gate.md updated for the new
      schema and staging semantics.
- [x] Approval FINALIZED per reviewer instruction:
      approval-combined-0.33.89.json (status approved, approved_by
      sl@pushcoin.com, DRAFT deleted); final dry-run GREEN on the
      real candidate.  --apply awaits the maintainer's command.
- [x] Draft-generation mode added (maintainer directive):
      `just ownership-corpus-promotion-draft RUN_DIR OUT` /
      `drift_corpus_promote.py --draft` — validates both schemas +
      candidate hard gates, refuses unsupported changes
      (inclusion_rule / excluded population), computes all six
      hashes + exact universe/counter/key-set facts, writes a NEW
      non-overwriting pending draft with empty reviewer/date and
      <<HUMAN REVIEW REQUIRED>> baseline_md placeholders.  --apply
      additionally refuses unreviewed placeholders.  validate_approval
      relaxed to allow empty reviewer/date ONLY while pending (drafts
      are dry-runnable).  Teeth 28/28.  Demonstrated on the real
      candidate: generated facts EXACTLY match the finalized
      approval's.  doc/ownership-corpus-gate.md authoring section
      rewritten around the generator (facts / judgment / verification
      separation).
- [x] HOLD-round closures (3): shared check_universe_integrity()
      validator now runs in BOTH draft and promotion paths (draft
      teeth: duplicate names / partition overlap / orphan fixture all
      refuse with no draft written); BASELINE.md records the FULL
      approval sha256 (toothed); the premature approved_by stamp is
      REVERTED — approval-combined-0.33.89-DRAFT.json regenerated
      through the generator itself on the real candidate (facts
      machine-derived; reviewed attribution text carried over;
      status pending, identity/date empty, DRAFT name — --apply
      refuses on all three grounds).  Dry-run on the pending draft:
      all checks pass with the PENDING note.  Tools lane 56/56.
      HOLDING for the reviewer's GO: reviewer flips
      status/identity/date, renames out of DRAFT, then --apply +
      teeth + commit.
- [x] Clean-repo reproducibility gap (maintainer catch: build/* is
      empty on a clean clone): the candidate's three consulted
      artifacts are now CAPTURED in-repo at
      lang/tests/ownership_corpus/candidates/ownership-corpus-
      combined-20260726-102955-3365314/ (byte-identical to the
      retained run, verified); the DRAFT's run_dir repointed to the
      checked-in path; dry-run green from it.  Process doc gains the
      "Capturing the candidate" step — all promotion steps now
      operate on checked-in paths, so a clean clone can audit or
      execute the promotion end-to-end.
- [x] DURABLE PROMOTION RECORD model (maintainer design, agreed):
      lang/tests/ownership_corpus/promotions/<name>/ with
      predecessor/ + candidate/ artifact copies AND compact
      fixture-counters.json extractions (exactly-one-aggregate-record
      fail-closed extractor; raw multi-MB audit logs no longer need
      preserving).  Approval pins every evidence hash incl. both
      fixture-counters; promotion RE-PROVES attribution from the
      checked-in evidence on every run (modal, outliers, new-fixture
      contributions, residual zero) — tamper caught by hash pin,
      re-pinned tamper caught by residual.  Dry-run is dual-mode:
      proposed transition (live baseline == predecessor) or AUDIT
      MODE on a promoted checkout (live baseline == candidate,
      comparison sourced from the record's predecessor/); --apply
      requires the predecessor state.  Draft mode now generates the
      whole record (--predecessor-run required, identity-checked
      byte-equal to the live baseline).  Teeth 34/34 (evidence
      tamper, audit mode, extraction, record layout); tools lane
      58/58.  REAL record generated from the retained build/tmp runs
      BEFORE any wipe: machine attribution_facts = modal {-3,-3,-3}
      on 924/924, 0 outliers, 1 new fixture — exactly the reviewed
      analysis; reviewed baseline_md text carried; status pending,
      identity empty, DRAFT name.  Dry-run green from the record's
      own candidate dir.  Superseded artifacts removed (flat draft,
      candidates/ capture dir).  justfile + doc updated.
      => build/* is now SAFE TO WIPE.
- [x] PROMOTION APPLIED by the maintainer (approval.json approved by
      sl@pushcoin.com; baseline == candidate; BASELINE.md regenerated
      with the full approval hash).
- [x] Workflow-defect fix (maintainer directive): both promotion
      recipes now run the focused 35-tooth suite AUTOMATICALLY after
      every successful tool run (dry-run, apply, and draft
      generation) — no manual pytest step between promotion and
      commit; broader tools lane stays with just test /
      run-all-tests.sh.  Wiring tooth added proving both recipe
      bodies invoke the focused check AFTER the tool.  Verified live:
      post-apply dry-run entered AUDIT MODE (record predecessor
      sourcing, attribution re-proven residual-zero from checked-in
      evidence) and auto-ran the gate — 35/35.
      Workflow now: draft recipe -> review -> promote --apply ->
      commit -> run-all-tests.sh.
- [x] Stale-hardcode closure (5 items): the checked-in-baseline
      sanity test is now a DURABLE-RECORD check (live baseline must
      match EXACTLY ONE approved promotion record's candidate hashes;
      approval approved + real reviewer; expected universe counts vs
      manifest; hard gates zero; FULL approval sha256 present in
      BASELINE.md; policy needles whitespace-normalized) — no
      924-style historical hardcodes; justfile + test-header wording
      swept.  ownership-corpus-promote now runs the live-baseline
      sanity test AFTER the mechanics suite; wiring tooth
      strengthened to require both checks in order after the tool.
      Verified live on the promoted tree: audit-mode recipe ->
      mechanics 35/35 -> sanity 1/1; full tools lane 59/59.
- [x] FILENAME-AUTHORITY approval contract (maintainer directive):
      approval state is the exact filename — approval-DRAFT.json =
      pending (dry-run OK, apply refused), approval.json = approved
      (apply allowed); alternate names and both-present fail closed.
      status/approved_by/date removed from the schema (legacy records
      keep them as INERT history; the existing 0.33.89-combined
      approval.json + BASELINE.md bytes are untouched and remain
      valid).  Reviewer's ONLY mutation is the rename; identity/date
      come from the Git commit.  Draft generator now emits a COMPLETE
      approval (baseline_md mechanically composed from the recorded
      facts — no placeholders); future BASELINE.md template records
      the full approval sha256 + a Git-history pointer instead of
      duplicated identity.  Durable-record sanity moved to filename
      authority (+ no-DRAFT-sibling ambiguity check).  Teeth: flipped
      status/identity tests to inertness proofs; rename-enables-apply,
      alternate-name, both-present, complete-draft teeth added —
      36/36; sanity 16/16; legacy record verified end-to-end via the
      audit-mode recipe (auto-gates 36+1); tools lane 60/60.  Docs
      updated with the binding five-step workflow.
