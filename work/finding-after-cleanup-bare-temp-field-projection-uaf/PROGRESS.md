# Progress: after-cleanup bare-temp field-projection references

Last updated: 2026-08-03

- [x] Confirmed the old work folder has been removed.
- [x] Enumerated five dangling durable-tree references.
- [x] Located the stable replacement in `doc/history.md`.
- [x] Identified stale “0.34.0 fix” wording in the MIR comments.
- [x] Wrote the bounded comment-only cleanup plan.
- [ ] Replace the three MIR comment references.
- [ ] Replace the driver-test docstring reference.
- [ ] Replace the e2e fixture comment reference.
- [ ] Confirm the repository-wide reference search returns no matches outside
  `work/`.
- [ ] Run `git diff --check` and review the five comment-only edits.

No compiler, runtime, stdlib, test, or version file was changed while creating
this finding.
