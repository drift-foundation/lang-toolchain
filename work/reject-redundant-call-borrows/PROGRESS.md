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
- [ ] BLOCKED ON D8 (per reviewer) + remaining decisions D1, D1b, D2, D4-D7, D9. No
      implementation started; no repo source, stdlib, test, or doc files touched (all
      artifacts under work/ + session scratchpad).
