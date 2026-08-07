# Baton message

Timestamp: 2026-08-06T05-51-39Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_review
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T05-50-15Z-4060635692c8-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T05-50-15Z`
Packaging design folded in — PROGRESS round 5 has the full deliverable:
layout, build command/entry point, exact distribution set, portability/
cache limits, ledger delta, and six focused packaging tests (matrix now
31 cases).  The expiry dead-receipt recovery correction is formally in
the locked contract text (expire-first ordering, dead-receipt
recognition, re-run-to-recover).

Boundary positions, challenged as asked:
- Accept PEX --scie eager exactly on the bin/driftc pattern (pinned pex
  + --scie-python-version + staged entry point).  Baton is stdlib-only,
  so the artifact carries just the embedded interpreter — no wheels.
- ONE coupling decision flagged for Slawomir: factor a generic
  build_scie_pex() helper out of tools/deploy/steps/pex.py (behavior-
  preserving; driftc/drift builds keep their tests) and have a
  standalone tools/baton/build_pex.py call it — Baton's RUNTIME stays
  fully decoupled from Drift deploy code either way; the alternative is
  duplicating ~30 builder lines for zero build-time coupling too.
- Source/tests distribution: recommend NOT vendoring to peers —
  executables + protocol + schema/example + manifest only; source and
  tests stay canonical in the Drift tree, with the manifest's
  sha256/version rows as the audit anchor.  Vendored source beside a
  sealed binary invites divergence.
- Portability limits documented plainly: per-platform artifact
  (manifest names it), SCIE_BASE required on read-only installs,
  per-user extraction cache, and the existing Linux-only
  renameat2/inotify constraint now also a platform row.

Packaging tests mirror the driftc precedent: scrubbed-environment run,
outside-all-trees round-trip with an external instance, read-only
artifact + external SCIE_BASE, malformed-config parity with the dev
launcher, manifest self-report incl. sha256, and packed-vs-dev parity
smoke.  No implementation.
