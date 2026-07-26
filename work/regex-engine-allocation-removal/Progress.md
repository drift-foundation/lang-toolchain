# regex-engine-allocation-removal — Progress

## 2026-07-26

- [x] Slice opened (README.md with the full 12-point phase contract).
- [x] Engine source read end-to-end (`stdlib/std/regex/regex.drift`,
      1193 lines).  Allocation sites identified statically:
      `_make_bitmap` (regex.drift:944) + initial clist (:945) once
      per anchored attempt; `seeds` (:958) + replacement clist (:969)
      once per consumed byte; `_clear_bitmap` O(prog) writes per
      byte; recursive `_add_state` (:858).  `_find_from_range`
      (:1084) and `is_match` (:1054) repeat the whole attempt per
      candidate start.  Compile-time allocations (parser AST arrays,
      `_nfa_compile` ops/ranges) are separate.
- [x] Prior-phase evidence located: stock engine 171,325 µs vs DFA
      proxy 2,519 µs on the 2 MB carrier; bulk-read engine variant
      only 1.9% faster (checkpoint §11) — bookkeeping-dominated.
- [x] Wrap-count mechanism studied
      (lang/tests/driver/test_string_byte_view_counts.py): cross-TU
      `-Wl,--wrap` counting, window-by-subtraction, heap-backed
      subject requirement.  Carrier recipe from tools/perf/
      tier_bench.drift (`"alpha,bravo12,charlie345,dd,echo_echo_echo,f,"`).
- [x] Harness built and green: bench/ops.drift (timing, RESULT+CHECK
      lines), bench/counts.drift + driver.c (wrap counts, twin
      subtraction), bench/probe.drift + probe_driver.c (allocator-call
      calibration), bench/model.py (faithful replica + affine
      per-chunk prediction, periodicity verified k=5), run_bench.py
      (heartbeats, multi-launch medians, reconciliation).
- [x] Replica fidelity: model scan-all checksum 932082097159 ==
      real binary CHECK line; short-token spans equal.
- [x] CALIBRATION (probe): each Array construction = 2
      drift_alloc_array calls (storage + box) and 2 real frees;
      move/replace tombstones add NO-OP free calls; engine shape =
      +5/attempt +3/byte.  First reconciliation run FAILED (model
      assumed 1 call/array) — fixed by measuring, not fitting: the
      2x and the +5/+3 verified residual-zero across all 10 windows
      (4 patterns x 3 input families x 2 sizes).
- [x] Measurements complete: quiet-machine timing (canonical) +
      under-load collection (noted); counts residual ZERO on every
      window (results/ + checkpoint §2 tables).  Headline: 36.07M
      alloc calls + 67.3M free calls for ONE 2 MiB no-match scan;
      ~98.6% of engine time is bookkeeping; allocator growth exactly
      affine in input length (x31.988 = chunk ratio).
- [x] REGEX-ENGINE-ALLOCATION-CHECKPOINT.md written — allocation
      model (§1), harness+tables (§2), architecture comparison (§3),
      recommended _NfaScratch design: epochs + direct next-closure +
      iterative worklist (§4), overflow-reset pin (§6), semantics
      matrix (§7), gate numbers incl. <1.5x STOP rule (§9),
      size/boundary plan (§10), consolidated implementation plan
      (§11), corpus expectations (§12).  STOP — review received.

## 2026-07-26 — review round: design APPROVED, 9 blockers + workload correction

Reviewer: epochs + direct next-closure + iterative DFS-preserving
worklist + function-local scratch approved in principle; proceed to
implementation after blockers, no further arm-selection review.

- [x] B9 records: branch baseline is COMMITTED mainline 32d676bb
      ("pre-cert"; string-view phase committed as 1f934862); 0.33.88
      certification in flight; bench/__pycache__ removed.
- [x] B1 allocator-model correction CONFIRMED by classified probe
      (rev 2 driver: wrapper calls / real / sentinel-noop + exact
      live-pointer set): Array::with_capacity = 2 wrapper calls =
      1 REAL posix_memalign (reserve) + 1 SENTINEL no-op (the
      zero-capacity empty-init); drop = 1 REAL free; all other free
      calls are no-ops (sentinel/NULL tombstones).  Real allocator
      work = 1 malloc + 1 free per array — HALF the earlier claim;
      "both real" and allocator-dominance claims RETRACTED (checkpoint
      rev 2 pending); DFA gap proves bookkeeping dominance only.
      live_end=0 in every probe/window (no leaks).
- [x] B3 packed workspace probe: packed single Array<Int> (4 regions)
      = 1 real alloc/free per scratch vs FOUR for separate arrays,
      1/4 the wrapper traffic (200 vs 800 calls per 100 constructs).
      Packed is the design.  Timing twins added to ops.drift
      (scratch_four vs scratch_packed x200000).
- [x] B2 harness hardening: content-addressed build cache (source +
      stdlib-tree + commit/dirty + versions.py + flags key — stale
      reuse impossible); provenance block (commit, driftc/ABI, source
      hashes, command, timestamp, host, per-phase loadavg); release
      pins added (String 0/0, view +1/+1); canonical
      baseline-quiet.json written only with REGEX_BENCH_SET_BASELINE=1
      AND max load < 1.0; loaded runs labeled run-<ts>-loaded.json;
      interleaved ABAB compare mode (REGEX_BENCH_COMPARE) for the
      final gate.
- [x] Workload correction: SMALL-SUBJECT suite generated
      (gen_small.py -> generated/{ops_small.drift, counts_small.drift,
      driver_small.c, small_meta.json}; single source of truth shared
      with model.py): 5 scenarios (early/late/nomatch/anchored/alt6)
      x 6 sizes (64..4096) + view forms + compile+match rows = 38
      timing rows, 22 count windows x100 reps; ns/search + searches/s
      reporting.  Smoke: late_256 ~46.9 us/search (~185 ns/attempt —
      per-attempt allocation overhead), early_256 ~168 ns/search
      fixed cost, 4 KiB nomatch = 40,970 REAL allocations per search.
- [x] B7 export-surface audit RECORDED: swept every workspace under
      ~/src (workflows, web, net-tls, mariadb-client, query, mariachi,
      pushcoin, build-orchestrator, testing checkouts): ZERO consumers
      of the regex exported-internals outside drift-lang stdlib
      copies themselves; in-tree sweep: no users of _make_bitmap/
      _clear_bitmap/_add_state outside regex.drift (not even tests).
      Plan: DELETE those three (history compatibility note; honest
      "exported-internal source-surface change"), KEEP _try_match_at
      + _find_from as one-scratch-per-invocation compat wrappers.
- [x] New-engine draft written (scratchpad new_executor.drift.txt):
      packed _NfaScratch, epoch marks + deterministic overflow reset
      (gen ceiling 2^63-2, reset sweep, increment-before-first-use),
      iterative mark-on-push closure (Split pushes b before a),
      direct next-closure with region-role swap, scratch lifetimes
      per B4 (one per top-level entry; one across whole replace ops;
      compat wrappers one per invocation), assert()-pinned
      scratch/program size, _find_from_gen_saturated tooth hook.
- [x] Release classification added after first two-suite run flagged
      view windows at release 2 vs pin 1: the extra call is a
      NULL-TOMBSTONE release (move machinery).  driver.c now splits
      release_real (storage != NULL) vs release_null; pins:
      String 0 real + 0 null, view exactly 1 retain + 1 REAL release
      (+1 null reported).
- [x] Full two-suite orchestrator run: **residual ZERO on all 32
      windows** with the corrected classified model (real allocs =
      arrays; sentinel = arrays; free_real = arrays via live-set,
      live_end 0 everywhere).  Headline per-search REAL allocations
      today: 256 B late hit = 3,118; 4 KiB no-match = 40,970;
      anchored 256 B = 514; even zw = 4.
- [x] Dual-engine differential harness (gen_diff.py + legacy
      snapshot @ 32d676bb, 1000 seeded cases incl. 40 invalid
      patterns comparing error tag+offset): SELF-VALIDATED at 0
      mismatches pre-rewrite, 23 ms runtime.
- [x] Checkpoint rev 2 WRITTEN: corrected allocator model +
      retractions (real work = 1 malloc + 1 free per array;
      allocator-dominance claim withdrawn), small-subject PRIMARY
      workload tables, packed-workspace decision (1 vs 4 real
      allocs), pinned iterative algorithm, exact scratch lifetimes +
      scan-all (#matches+1) formula, hard bands, audit record,
      records correction (baseline = committed mainline 32d676bb;
      0.33.88 cert in flight; target 0.33.89).
- [x] Canonical-baseline attempts: reconciliation residual-zero AGAIN
      on the classified rerun (32/32), but ambient load 0.9-1.1 trips
      the strict <1.0 quiet gate — canonical artifact deferred to a
      genuinely quiet window (final gate is interleaved ABAB anyway,
      which self-controls ambient load).  Pre-rewrite baseline
      binaries preserved at bench build cache-4fee90ca034fcf33.

## 2026-07-26 — IMPLEMENTATION (approved design)

- [x] ENGINE REWRITE applied to stdlib/std/regex/regex.drift:
      packed _NfaScratch (ONE Array<Int>, 4 regions, role-swapping
      list bases); epoch marks w/ deterministic overflow reset
      (ceiling 2^63-2, gen starts 0 + increment-before-use);
      iterative mark-on-push closure (Split pushes b before a, DFS
      parity; worklist bounded by prog_len; explicit consuming-op
      arms so tombstones never join the list); direct next-closure
      construction (seeds deleted); _make_bitmap/_clear_bitmap/
      _add_state DELETED from module + exports; _try_match_at +
      _find_from kept as one-workspace-per-invocation compat
      wrappers; _find_from_gen_saturated exported test hook;
      is_match/find_first/find_first_view one workspace per
      top-level call; replace_all ONE workspace for the entire
      operation; assert() pins workspace/program size match.
- [x] DIFFERENTIAL: legacy shadow (32d676bb snapshot) vs new engine,
      1000 seeded cases — **0 mismatches, first compile** (spans,
      is_match, view parity, compile error tag+offset).
- [x] SEMANTIC SUITE: all 15 std_regex_* e2e fixtures PASS on the
      new engine (runner exit 0).
- [x] COUNT CONTRACT VERIFIED (wrap windows, new engine): ONE
      top-level find = EXACTLY 1 real alloc/free at 64 KiB AND 2 MiB
      (was 563,818 / 18,035,578 arrays); scan-all = matches+1 EXACT
      (93,209 = 93,208+1 on the 2 MiB carrier); x100 ops all
      exactly 100; view 1 retain + 1 real release; live_end 0;
      checksums identical to legacy (932082097159).
- [x] In-tree teeth written: lang/tests/driver/
      test_regex_scratch_counts.py (classified wrap shim: 1-per-
      search pin, size-independence, scan formula, replace_all
      single-workspace-by-subtraction, retain/release_real pins,
      gen-saturated equality) — run pending.
- [x] Interleaved ABAB perf comparison — ALL HARD BANDS MET:
      small-subject primary suite: every one of 38 rows 2.0-2.45x
      faster, NOTHING slower (gate cases late/nomatch 256B & 4KiB
      ~2.35x; alt rows ~2.4x; anchored ~2.2x; early fixed-cost path
      2.0x -> ~85 ns/search); big suite: carrier 2.33x (177.4 ->
      76.1 ms), 2MiB no-match 2.46x (view 2.52x), 16-branch alt
      512KiB 1.61x (band 1.5x), zw/short/anchor 1.6-2.0x, compile
      parity 0.98-1.00.  STOP rule not triggered.
- [x] New tooth test_regex_scratch_counts.py PASSES (after
      per-pattern compile-twin fix: is_match's anchored pattern
      needed its own subtraction baseline — 98 vs 100 caught it).
- [x] New e2e pin fixture std_regex_view_offsets_alternation
      (view-relative offsets, view-boundary anchors, alternation
      arm-order independence/leftmost-longest, empty match at view
      end, match_subview composition): PASSES; full std_regex e2e
      set now 16/16.
- [x] Valgrind clean over the 1000-case differential binary.
- [x] Compile cost + size (proper both-sides measurement via a
      scratch stdlib copy w/ the legacy snapshot): wall 22.4s legacy
      vs 22.3s new (parity), ops.bin -32 B.  NOTE: first attempt at
      this measurement accidentally ran `git stash` (stray command
      fragment) — tree restored by the maintainer via git stash pop;
      no content lost (stash held only regex.drift; untracked files
      unaffected).
- [x] String-view-phase regex pins re-verified on the new engine:
      test_string_byte_view_counts.py + test_string_byte_view.py
      9/9 pass (zero-retain String matching, +1 view, conversions
      delta 201).
- [x] Docs: is_match/find_first/find_first_view docstrings now state
      the one-workspace allocation contract; history.md 0.33.89
      entry (engine, evidence, exported-internal surface note);
      lang/versions.py 0.33.88 -> 0.33.89 (ABI 22 unchanged).
- [x] Corpus measured (build/tmp/ownership-corpus-regex-20260726-
      082739-3276579, retained): 925/1269 compiled (344 failed / 49
      excluded — partition unchanged, +1 discovered = the new pin
      fixture).  ATTRIBUTION RESIDUAL ZERO (bench/attribute_corpus.py
      vs the retained promoted-baseline run): universe exactly +1
      fixture, zero pre-existing hash changes; ALL 924 shared
      fixtures carry ONE identical modal delta {events -3,
      c3_moveout_owned -3, moveout_expansion -3} (the engine's
      deleted per-byte array moves), ZERO outliers; new fixture's
      contribution itemized (fns +1254, events +3144, ...); every
      counter reconciles agg = modal*924 + new, residual 0; hard
      gates zero.  PROMOTION AWAITS MAINTAINER REVIEW (not done).
- [x] Checkpoint ADDENDUM 1 (implementation results) appended.
- [x] Final team code-review report written:
      /tmp/drift-announce/2026-07-26T145559Z-regex-engine-allocation-
      review.md (evidence table, exported-internal surface note,
      diff inventory, git-stash incident disclosure, asks: code
      review + promotion decision + user-run full suites + 0.33.89
      certification).

STATE: consolidated chunk COMPLETE pending review.  Tracked diff:
stdlib/std/regex/regex.drift, doc/history.md, lang/versions.py
(0.33.89).  New untracked: test_regex_scratch_counts.py, e2e
std_regex_view_offsets_alternation/, work/regex-engine-allocation-
removal/.  User owns: git, promotion, run-all-tests.sh, certification.

## 2026-07-26 — PAUSED after static review

The branch is intentionally paused before baseline promotion, full-suite
handoff, or certification.  `RESUME-STATUS.md` is the restart authority.  The
packed-workspace architecture is approved; the remaining HOLD is bounded to:
hot-loop ablation, a durable regex perf-protocol surface, fail-closed corpus
attribution, wording corrections, and combined release sequencing with the
pending pure-String hot-path recovery.  The existing retained corpus run is
valid only if no production success-path amendment is accepted.
