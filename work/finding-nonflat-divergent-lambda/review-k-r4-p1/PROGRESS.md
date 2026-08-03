# Progress: static review of K's R4 / P1 patch

Last updated: 2026-08-03

- [x] Read `work/value-block-lambda-return-inference/{PLAN,PROGRESS}.md`.
- [x] Checked `/tmp/drift-announce`; no announcement directory was present.
- [x] Reviewed the source diff across checker, call resolver, HIR, and MIR.
- [x] Reviewed the three new driver files and tracked test updates.
- [x] Confirmed the named-return implements check mirrors the HLet relation
  policy and Unknown suppression precedes interface processing.
- [x] Confirmed the direct checker fallback was removed in favor of CallInfo.
- [x] Confirmed HLambda source and alpha-renamed producers preserve `loc`.
- [x] Confirmed statement HCall/HInvoke fast paths exclude lambda literals.
- [x] Identified blocking semantic-masking and hidden-coercion findings.
- [x] Identified boundary-test/source reachability and diagnostic-suppression
  follow-ups.
- [x] Wrote final review findings with exact line references.

No compiler, runtime, stdlib, or in-tree test file has been edited.  No tests
were run in this review pass.

---

# ROUND INDEX (K, 2026-08-03) — single source of "what is being worked"

Exactly ONE finding is IN PROGRESS at any time; everything else is QUEUED or
DONE.  Statuses live in each finding's own PROGRESS.md; this table is the map.

| Review item | Tracked in | Status |
|---|---|---|
| P1-1 non-flat divergent lambda (masking) | work/finding-nonflat-divergent-lambda/ | IN PROGRESS |
| P1-2 lambda-tail coercion positive | work/finding-lambda-tail-coercion-positive/ | QUEUED (user ruled: separate, do not start yet) |
| P1-3 CallInfo inference / dead branch | work/finding-p13-callinfo-inference/ | QUEUED (user ruled: separate, do not start yet) |
| P2-4 causal poison markers | this round, no folder | QUEUED (after P1-1) |
| P2-5 true statement-position throwing IIFE pin | this round, no folder | QUEUED (after P1-1) |
| Comment cleanup ×2 | this round, no folder | QUEUED (after P1-1) |
| return-reconciliation | work/finding-lambda-return-reconciliation/ | QUEUED (pre-existing queue) |
