# Progress — reject-redundant-call-borrows

- [x] 2026-07-28 Research sweep (4 parallel passes: auto-borrow mechanics; generics/
      overloads/interfaces/Fn-values; corpus + spec survey; diagnostics/versioning/ABI).
- [x] 2026-07-28 PLAN.md v1 — REJECTED in review (narrowed the rule, voted on the
      narrowed version, unscoped impact numbers).
- [x] 2026-07-28 Compiler probes round 1 (e1-e7) + parser-driven scoped recount
      (≈5,100 ± 60 firing sites; interface dispatch 4 sites; 431 e2e dirs / 415 in
      frozen corpus universe).
- [x] 2026-07-28 PLAN.md v2 — BLOCKED in review: row-19 silent narrowing (iface views),
      thin fn pointers missing, enforcement not centralized, R2 missing &T/&mut T,
      package provenance not implementable, counts/labels/artifacts issues.
- [x] 2026-07-28 Probes round 2 (e8-e10): thin fn-pointer bare call MISCOMPILES (e8d —
      latent defect, adjacent bug to file); bare concrete at &Interface fails (e9);
      &T/&mut T pair bare ambiguous (e10). Mode-erasure overload scan: 3 T-vs-& pairs
      (all known), 0 &-vs-&mut pairs repo-wide. Exact &mut-rvalue count: 2 (both in the
      pinning test).
- [x] 2026-07-28 Published reproducibility artifacts: probes/ (17 programs + results
      README), recount/ (site dataset results3.json, method README, clean R2 scan
      script, collection scripts).
- [x] 2026-07-28 PLAN.md v3 published: W0 centralized policy, D7 (interface views) and
      D8 (fn pointers) as explicit policy forks, D9 package policy (provenance stripped
      at emission; old packages valid by decode-default), R2 mode-erasure form, D1b
      mutable-rvalue choice, totals relabeled as estimates, full-suite responsibility
      assigned to implementer. Recommendation: conditional up-vote on D1-D9.
- [x] 2026-07-29 D3 RESOLVED by user: no compiler/path exemption for issues/; archival
      snapshots preserved verbatim (192 sites, never compiled → sweep skips them);
      active regression-test inputs migrate. Verified: 3 issues/ files are live inputs
      via test_drift_query_slice12_ices.py:48-50; only repro_single_file.drift fires
      (2 sites → migrate). Standing rule recorded: archived source promoted to active
      input must first be updated to current syntax. PLAN.md D3/§3/§7/§12 updated.
- [x] 2026-07-29 Reviewer bookkeeping fixes applied: §12 migration total ≈4,900 (after
      D3 archival exclusion); interface-dispatch impact restated as census 4 / 1 active
      migration site (repro_single_file.drift:211), W2 kept as language-correctness
      work. R-7 confirmed YES by probe e11 (Fn(T) typevar fn type compiles, exit 42);
      D8 rewritten accordingly — no total-provenance variant exists; e8d miscompile
      routed to the separate LANGUAGE_BUG regression-first process, W-FP treats it as a
      precondition only.
- [x] 2026-07-29 D8 RESOLVED → (b) (fn-pointer invokes exempt; provenance-honest
      after R-7=yes) and remaining review corrections applied (validator = policy-
      classification invariant; R2 = non-receiver mode erasure; e8d routed to its own
      LANGUAGE_BUG slice — since fixed as 0.33.91, committed 453a2f52).
- [x] 2026-07-29 FULL DECISION SLATE RATIFIED by user: D1 in-scope, D1b(b), D2
      include, D4 mode-erasure, D6 alias transparency, D7(a), D9 source-only. PLAN.md
      updated (decisions marked resolved; matrix rows 7/16; §9 row-11 removal +
      policy-classification wording; §12 rewritten — D5 is the single remaining
      gate).
- [x] 2026-07-29 Plan-consistency cleanup per review: header → "policy approved;
      awaiting D5"; release-ordering statement consolidated into §7 (bug fix landed
      first internally as 0.33.91/453a2f52, rule ships in the SAME 0.33.91 release —
      §12 now defers to §7); W7 retains DRIFTC_VERSION 0.33.91 / ABI 22 and specifies
      ONE combined history entry + ONE combined release-notes file (extend existing,
      no second announcement); W4 fixed to resolved D1b(b); §9 fn-pointer bullet →
      acceptance pins for BOTH spellings; O5/R-6 conditional wording removed.
- [x] 2026-07-29 D5-test-changes.md PUBLISHED (exact per-test dispositions, source-
      verified): 0 e2e retirements (15 repurposed in place), 2 Python retirements
      (selector tests → replaced by R2 pins), 19 negative fixtures in scope (not 26 —
      survey corrected; 7 additions, 4 capture-list fixtures reclassified
      UNAFFECTED), corpus promotion = 426 deltas + 0 removals + exactly 20
      enumerated additions (13 failed + 7 compiled_ok; universe 1,269 → 1,289), 0
      expected partition flips. Two soundness gates flagged (B19 same-stmt conflict
      via synthesized borrows; C4 capture-overlap via synthesized borrows). Sweep-
      exclusion list + W3 six at-risk messages + diagnostic-wording unification
      recorded. Three open reviewer choices (A3, A7, B15/B16 handling).
- [x] 2026-07-29 D5 round-1 review: existing dispositions + 2 Python retirements
      APPROVED; A3 repurpose-to-rvalue, A7 repurpose (keep reborrow coverage),
      B15/B16 finalize-at-W3 under user-facing + present-before-change constraints.
      Five corrections applied: (1) D1b(b) recorded as the rule's SOLE
      non-redundancy rejection — new MUT_RVALUE_BINDING classification +
      E_MUT_RVALUE_ARG_BINDING_REQUIRED diagnostic (never E_REDUNDANT_ARG_BORROW /
      "pass directly"); PLAN §1/W0/W4/row-7/§9/§12 updated. (2) Rvalue A/B gate
      baseline = programmatic-HIR driver test (source_written=False), since the
      explicit-baseline source is itself rejected post-rule; e2e keeps only
      rvalue_arg_temp_drop_bare. (3) Additions enumerated exactly: 20 named
      fixtures (13 failed + 7 compiled_ok) → universe 1,269 → 1,289 exact.
      (4) Blanket diagnostic unification withdrawn — distinct failures keep
      distinct messages; only the two equivalent D1b spellings share a remedy
      phrase. (5) Summary count fixed: 13 positive repurposes.
- [x] 2026-07-29 D5 round-2 revision applied: A/B baseline constraint recorded (the
      programmatic-HIR half MUST run full HIR→MIR→LLVM and execute under
      memcheck/ASAN; borrow-checker-only style insufficient); R2 fixture #13
      expanded to all four ratified shapes as four uniquely-named overload sets
      (defeats subset-matching aliasing); stale approximate corpus block removed
      (exact 426+20+0 / 1,269→1,289 authoritative); A3 stale alternative removed;
      PLAN §7 sweep wording fixed (one-token EXCEPT the two D1b mutable-rvalue
      sites, which take the binding/repurpose treatment).
- [ ] AWAITING D5 FINAL APPROVAL — the single remaining gate. No implementation
      until D5-test-changes.md is approved.
