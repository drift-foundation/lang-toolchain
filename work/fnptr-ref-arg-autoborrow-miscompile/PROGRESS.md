# Progress — fnptr-ref-arg-autoborrow-miscompile

- [x] 2026-07-29 Slice opened. Branch creation blocked at permission layer (git writes
      denied to agent); proceeding on working tree — user to run
      `git checkout -b fix/fnptr-ref-arg-autoborrow-miscompile` before commit.
- [x] 2026-07-29 Step 1: minimal repro recorded (repro_minimal.drift, from probe e8d).
      PLAN.md written incl. refactor_triggers.md conclusion (no trigger matches).
- [x] 2026-07-29 Step 2: regression lang/tests/driver/test_fnptr_ref_arg_autoborrow.py
      written; on current tree 4 FAIL / 5 PASS exactly as expected — bare shared, bare
      &mut, wrong-inner-LOCAL, and ASAN row all die at clang with the pinned
      `%DriftString`/`ptr` (or i64/ptr) error; explicit forms and both generic Fn(T)
      instantiation controls pass; immutable-at-&mut already rejected.
      (User approved running tests directly; branch stays as-is per direction.)
- [x] 2026-07-29 Step 3: ROOT CAUSE — "binding call" branch of resolve_call_expr
      (checker/call_resolver.py:6091-6113): HCall on an HVar bound to a FUNCTION-typed
      local checks ONLY arity; no argument-type check, no auto-borrow; records CallInfo
      and returns. Shallow validator (checker/__init__.py:3117 check_call_signature)
      only catches inferable literal args, so local args reach codegen unchecked.
- [x] 2026-07-29 Step 4: FIX in checker/call_resolver.py binding-call branch: compute
      place-typed arg_types (was never assigned — latent UnboundLocalError on the
      arity-error path, also fixed), run ctx.apply_autoborrow_args (structural HBorrow,
      same engine as direct calls), then strict want!=have verification with the
      standard &mut→& reborrow escape, message "function value argument type mismatch"
      matching the sibling .call()/HInvoke paths. No codegen changes.
- [x] 2026-07-29 Step 5: controls C1-C5 all covered in the regression file (explicit
      shared/&mut, generic Fn(T) at &String and at Int, wrong-inner LOCAL now a driftc
      diagnostic, immutable-at-&mut rejected).
- [x] 2026-07-29 Step 6a: unfiltered suites — checker/stage1/stage2/method_registry
      745 passed; driver 2200 passed / 10 skipped / 3 errors (all three = std.json
      perf gate refusing xdist, "run just perf-protocols serially" — environmental,
      covered by the full gate); lang/tests/codegen 18 passed (e2e corpus runs via
      `just test` inside the full gate).
- [x] 2026-07-29 Step 7: DRIFTC_VERSION 0.33.90 → 0.33.91 (lang/versions.py); ABI
      stays 22 (typed-checker argument handling only; fixed program's IR identical to
      the explicit-borrow spelling; no boundary change found). doc/history.md 0.33.91
      entry written (root cause, fix, pinned semantics, versioning).
- [x] 2026-07-29 Step 6b first full-gate run: ownership-corpus OK (no partition
      flips), perf-protocols OK (serial), then FAIL in the memcheck-lane `just test`
      at repo_audits — NOT the compiler fix: test_tmp_root_compliance flagged
      hard-coded scratchpad /tmp paths in the 24 archived research scripts I copied
      to work/reject-redundant-call-borrows/recount/. Fixed by the audit's sanctioned
      per-line ` # drift-tmp-root-audit: allow <reason>` annotation (archived
      provenance copies, not executed); audit re-verified green (3 passed).
- [x] 2026-07-29 Step 6b rerun: corpus OK, perf OK, MEMCHECK lane OK; ASAN lane failed
      ONLY test_std_fs_read_dir.py::test_multicarrier_no_reentrant_execution — program
      printed the CORRECT "done:480" then an ASan stack-overflow on a carrier thread
      during teardown, then hung into the test's HARDCODED timeout=30 (violates the
      standing sanitizer_timeout() rule — test bug, noted for follow-up, not bundled).
      Test source contains no Fn-typed values (fix path not involved). Reruns 3/3
      PASS serially in ASAN lane on the fixed tree → load-dependent flake in a
      deliberately race-widened scheduler test; adjacent to the open
      executor-fiber-stack-under-sanitizer sizing thread.
- [x] 2026-07-29 Full ASAN lane rerun: GREEN end to end ("lang tests: Success.").
      GATE SUMMARY on the fixed tree: ownership-corpus zero-delta OK · perf-protocols
      OK · MEMCHECK lane OK · ASAN lane OK (single earlier failure = unrelated
      load-dependent flake, documented above).
- [x] 2026-07-29 Announcement finalized (local only, per policy); current path is in
      the round-2 entry below.
- [x] 2026-07-29 REVIEW ROUND 2 (request-changes) addressed:
      (1) fn-value KWARGS: regression C6 added first — confirmed failing (today a
      zero-arg fn value + kwarg dies as "internal: kwargs survived typed mode
      (checker bug)" instead of a user diagnostic); binding branch now rejects with
      "keyword arguments are not supported on function values in v1". (2) Arity path
      pinned (C7: f() at Fn(&String) → clean "no matching overload", no traceback).
      Regression file now 11/11 green. (3) Process: PLAN branch line corrected
      (current branch, no switching); stale PROGRESS checklist line resolved;
      announcement renamed to convention →
      /tmp/drift-announce/2026-07-29T133025Z-drift-lang-release-notes.md. history.md
      0.33.91 entry extended with the kwargs + arity fixes.
- [x] SLICE COMPLETE — awaiting review. Not committed (git reserved to user; branch
      handling per user direction). Follow-ups surfaced, NOT bundled: (1)
      test_std_fs_read_dir.py hardcoded timeout=30 → should use sanitizer_timeout();
      (2) teardown ASan stack-overflow flake in multicarrier test — adjacent to the
      open executor-fiber-stack sizing thread; (3) .call()/HInvoke strict paths left
      as-is (rejections) — unification tracked by reject-redundant-call-borrows W0.
- [x] 2026-07-29 Announcement drafted, finalized after full gate, and renamed to the
      required convention: /tmp/drift-announce/2026-07-29T133025Z-drift-lang-release-notes.md
      (stale duplicate checklist line removed in review round 2).
