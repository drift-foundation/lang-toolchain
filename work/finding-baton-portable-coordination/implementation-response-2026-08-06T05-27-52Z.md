# Baton message

Timestamp: 2026-08-06T05-27-52Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_review
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T05-25-02Z-02c7b688981c-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T05-25-02Z`
Phase-1 design challenge complete — full record in implementer-owned
PROGRESS.md.  Bottom line: the config/mailbox/participant/root/reference
separation is the right and sufficient abstraction (all three target
cases walk cleanly, including the non-repository root), and I found no
smaller model worth trading for it.  The coupling inventory grew by ten
items the finding omitted, one of which is a LIVE v5 defect the v6
design must fix rather than merely generalize:

- Receipts already live in wipeable /tmp keyed by repo-root hash; claims
  persist in the mailbox but reply/close hard-fails without the receipt,
  so a reboot mid-claim permanently strands the claim — and the notice
  author-receipt loss triggers the protocol's human-recovery clause via
  mere tmpwatch.  v6 receipts must move to a durable user-state dir
  (~/.local/state/baton) keyed by mailbox identity + participant
  instance, never /tmp, never a content root — which also fixes the
  second omission (receipts bind the literal repo_root string today, so
  moving a checkout invalidates live claims).

Other notable inventory items: the launcher's script-relative
parents[2] discovery is a third implicit authority beyond the two env
vars; the finding-* rule is a single-site deletion (respond guard only);
same-dir atomic staging + renameat2 is already cross-fs-safe and is a
KEEP constraint; no locking exists anywhere — single-winner is entirely
rename-noreplace, so doctor should warn on network filesystems where
those semantics quietly die; content_sha256 already exists on claim
receipts and should be PROMOTED into the envelope rather than invented;
and the dotted-address grammar interacts with filename parsing —
segments must stay hyphen-free with an enforced max length (NAME_MAX
leaves ~80 chars/address worst-case).

Positions taken on every open question (details in PROGRESS): keep
mailbox.id and make it VERIFIABLE via an init-time immutable
mailbox.json stamp checked on every open (the mapping-drift guard);
envelope sha256+size at publication subsumes any config-generation
counter; separate per-project configs sharing one mailbox must work;
participant declarations required on BOTH from and to at send time
(undeclared address = error — silent misdelivery is the worst shared-
mailbox failure); detail_prefix stays participant config; mandatory
absolute --config is fine for agents with wrappers for humans and an
env fallback explicitly rejected; v6.0 always creates details itself
(no existing-file references without future hash pinning); symlink
policy is resolve+containment per root with regular-nonsymlink finals;
doctor gains config-aware report-only checks including --assert-empty
for the migration's "verify actually empty" step.

Five questions are queued for Slawomir's Phase-2 ruling (stamp file,
undeclared-address severity, receipts location, existing-file-reference
exclusion, config-path convention), and a seed Phase-3 test matrix of
~16 cases is recorded.  No code was touched.
