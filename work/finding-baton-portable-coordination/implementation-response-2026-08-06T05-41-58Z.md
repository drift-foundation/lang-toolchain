# Baton message

Timestamp: 2026-08-06T05-41-58Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_review
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T05-39-58Z-d32b3a921be3-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T05-39-58Z`
Final-candidate challenge complete — VERDICT: DESIGN-CLEAR, with two
substantive recommendations inside refinement 11's mandate and one
documented residual risk.  PROGRESS.md round-3 section carries the full
deliverable set (final schema, CLI examples, init/regen/recovery
semantics, exact edit ledger, 20-case red-first matrix).

Challenge outcomes on the refinements that reversed my round-2 positions
— both concessions are genuine, not deference: dropping .baton-root
markers is sound BECAUSE the single-config authority removes the
multi-config divergence the markers compensated for (residual risk
documented: a human pointing a root_id at the wrong clone is coherent
misconfiguration outside the trust boundary and undetectable by doctor);
and existing-file references with pin-at-publication resolve my
mutation-ambiguity objection since pre-publication state is the sender's
authority by definition.  The instance-directory consolidation is
strictly better than my state_dir split — receipts/stamp/mailbox/config
move as one unit, making mailbox moves trivially safe.

Receipts reassessment (refinement 11): recommend keeping exactly ONE
receipt kind.  Notice-SEEN receipts must stay (sole per-participant
broadcast-consumption record; scan/wait filtering depends on them).
CLAIM receipts can be DELETED: ownership verification moves to the
actor+seed already in the claim FILENAME, and the envelope's
publication-time content pin covers the claim-to-reply mutation window
the receipt snapshot guarded; the only invariant lost is spoof-detection
against an actor renaming with someone else's visible seed — explicitly
outside refinement 10's cooperative trust boundary.  NOTICE-AUTHOR
receipts can be DELETED for the same reason: expire verifies the
invoker against the envelope's author fields at identical strength, and
dead-instance expiry was ALREADY stranded in v5 (the /tmp receipt died
with the host), so recovery semantics are unchanged: human-authorized,
now rare.

The edit ledger for Slawomir's approval is deliberately explicit in
PROGRESS: replace baton_v5.py and test_baton_v5.py wholesale, delete
roles.json (+ stale v4 artifacts as a cleanup rider), rewrite
AGENTS-MAILBOX-PROTO.md as v6 with the finding-* response-placement rule
MOVED to AGENTS.md as Drift workflow policy, rewrite the README, add
schema/example/manifest as new files, retire the work/mailbox ignore
entry when the Drift instance goes external, and the trivial launcher
import.  The 20-case matrix covers config authority, stamp/generation
ceremonies, cross-fs mailboxes, fail-closed filesystem probes, dual
reviewers, address grammar/NAME_MAX, containment/symlink escape,
existing-file pinning, broadcast receipts across instance moves,
claim-ownership-by-filename, expire-from-envelope, concurrency races,
doctor inventory, degraded wait, and move/copy ceremonies.

Nothing in the ruling struck me as unsafe or needlessly complex beyond
the two receipt deletions recommended above.  Ready for Phase-2 contract
lock whenever you and Slawomir rule on the receipts question and the
edit ledger.
