# RESOLVED — tripwires deleted after a clean cert cycle (2026-07-18)

The dead-stake tripwires this intake fronted (slice 4a, 2026-07-13:
the three store arms; slice 4b, 2026-07-14: the central
`_ensure_owned` retain arm) NEVER FIRED on any corpus, staging, or
certification run across their lifetime.

- 0.33.84 / ABI 21 CERTIFIED 2026-07-18 with all tripwire families
  armed: zero firings through the full suite.  Certified run
  `20260719-001008-drift-lang-99a68ee` also exercised drift-workflows
  `0251b24` (staging + normal/debug test + stress + perf) with zero
  tripwire log matches.
- Per the standing deletion schedule (one clean cert cycle,
  RELEASE-ARM-TRIPWIRE-DESIGN.md §8), the branches were DELETED in the
  tripwire-deletion slice (2026-07-19; see the tripwire-deletion entry
  in doc/history.md, 0.33.85 series): the
  three 4a store fallbacks collapsed into the unconditional
  retain-free consume, and `_ensure_owned` became an identity
  pass-through (staking is owned upstream by `string_stakes`).
- The four retain site classes stay HARD GATES in
  `tools/drift_corpus_audit.py` — a nonzero counter fails any corpus
  run regardless of how the events were produced.  That, plus the
  memcheck suite, is the surviving guard for this defect class.

Folder retained for historical reference; no repro was ever filed.
