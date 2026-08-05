# PROGRESS: Baton CLI trial

Implementer: reviewer-root for the tooling prototype

Status: local prototype complete; protocol integration/distribution remains a
separate decision.

Implemented under `tools/baton/`:

- one-shot scan, doctor, exact claim, and claim-next operations;
- inotify-backed wait-next with a 60-second defensive rescan;
- Linux `renameat2(RENAME_NOREPLACE)` publication and single-winner claims;
- immutable claim/target receipts and exact claimant-instance response checks;
- reviewer, implementer, and seedless singular-human response flows from a
  file or stdin;
- independent reviewer/implementer announcements for initial work orders and
  immutable addenda;
- response-before-pop ordering, nonterminal acknowledgment, and terminal
  signoff without a pending token.

Validation: 15/15 isolated tests pass, including a concurrent two-process claim,
inotify wake before a two-second fallback, target-tamper refusal, wrong-agent
response refusal, independent announcement, seedless human stdin approval, and
both positive and wrong-owner pre-Baton claim adoption.

Live trial: Baton claimed
`IMPL-PENDING-2026-08-04T22-23-12Z`, exposed the frozen target only after the
claim, published
`finding-causal-unknown-cascade-suppression/findings/finding-pending-lambda-probe-rollback/review-2026-08-04T22-38-58Z.md`
and `REVIEW-PENDING-2026-08-04T22-38-58Z`, then popped only its exact incoming
claim. `doctor` reports the remaining live token valid.

Adoption announcement: Baton published
`finding-baton-cli/review-2026-08-04T22-41-33Z.md` and the corresponding
`REVIEW-PENDING-2026-08-04T22-41-33Z` for K, including her current actor/seed,
inotify usage, fail-closed behavior, and defect-reporting route.

Trial gaps deliberately not folded into protocol v3 yet:

- the singular human has no seed in Baton, per Slawomir's UX ruling, while the
  prose v3 identity grammar still describes every claimant as seeded;
- Baton is Linux-specific (`renameat2` and inotify);
- protocol v3 remains the authority until a tool-backed revision is explicitly
  approved; peer repositories must not receive this trial implicitly.

First reported defect: K's manually claimed pre-adoption handoff had no private
Baton receipt, so `handoff` failed closed with exit 4. The chosen correction is
an explicit `adopt` action that validates and snapshots a protocol-valid manual
claim. Response actions retain their strict receipt requirement; there is no
implicit compatibility fallback. Implementation and 12/12 local tests are
complete; K's independent verification and child `PROGRESS.md` remain pending.

Second trial UX gap: an informational implementation handoff waiting on an
external gate cannot use `reply` without creating an acknowledgment loop, and
cannot use `signoff` without falsely closing the finding. `reviewer ack` now
publishes an immutable nonterminal acknowledgment and no outgoing token. This
distinction awaits formalization in the eventual tool-backed protocol revision.

Nested-finding and transition hardening added during closure review:
`adopt` now rejects a second claim for the same original token as well as a
surviving pending original, and response actions accept `--destination` only
within the incoming target's top-level finding tree. This keeps child review
journals in the child without permitting cross-finding reroutes. Both paths are
covered in the 15/15 suite.
