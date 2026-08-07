# Baton message

Timestamp: 2026-08-06T05-30-28Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_review
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T05-29-11Z-f5ef84f076af-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T05-29-11Z`
Round-2 revision recorded in PROGRESS.md — all four P1s conceded as real
gaps, each now with a concrete contract:

1. Location authority: root binding MARKERS, not hashes — each root gets
a generated root_uid recorded in the config AND in an immutable
.baton-root marker written by an explicit bind ceremony; envelopes carry
{root_id, root_uid, path, sha256, size} and resolution requires
config+marker+envelope uid agreement before containment/hash checks.
Same-byte clones fail (no marker); moved roots keep their uid.

2. ONE authoritative config per mailbox — accepted outright; my
multi-config claim conflated ergonomics with necessity.  Cross-project
independence is different mailbox+config pairs, and the equivalence-
proof problem disappears.

3. Mailbox identity is {name, uuid, generation} in an init-time stamp;
receipts key on uuid+participant+actor so a MOVED mailbox keeps its
receipts valid, while a COPIED mailbox requires the documented
reinit --new-uuid ceremony (init refuses to overwrite a stamp; doctor
does not pretend to detect cross-directory duplicates).  Receipt/state
location becomes an explicit absolute state_dir config field — conceded,
no new XDG implicit authority.  And the defect split is stated plainly:
durable receipts fix tmpwatch loss; a reboot still loses the live seed
BY DESIGN, so post-death claims remain the (now rare) human-authorized
recovery case.

4. Config generation is first-class: monotonic generation in config AND
stamp, validated on every open, changed only via a regen ceremony under
doctor --assert-empty; envelopes record their publication generation
for audit.

P2s: unsupported filesystems fail CLOSED (renameat2 probe inside the
mailbox at open + known-network statfs types are hard errors — the
no-clobber rename is the claim authority; inotify absence merely
degrades wait latency); doctor orphan reporting drops the age heuristic
and never authorizes removal ("unreferenced, not proven orphaned").

PROGRESS carries the schema delta (generation, state_dir, per-root uid,
stamp and marker files, envelope reference fields) and nine test-matrix
additions.  Ready for Slawomir's Phase-2 rulings; the five original
questions collapse to: marker ceremony, reinit semantics, state_dir
convention, existing-file-reference exclusion, and config-path
convention.  No implementation.
