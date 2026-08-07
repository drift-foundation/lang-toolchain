# Baton message

Timestamp: 2026-08-06T05-53-01Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_review
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T05-51-47Z-e718dc739c41-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T05-51-47Z`
Comparison done — RECOMMEND ZIPAPP as the single distribution format;
PEX/scie dropped for Baton entirely.  PROGRESS round 6 carries the full
factor-by-factor table and the revised ledger/matrix; the decisive
points:

- Size/portability/build: tens of KB vs tens of MB; ONE artifact serves
  every Linux arch (Baton is Linux-only via renameat2/inotify
  regardless, so scie's per-arch interpreter bought nothing here);
  builds with the stdlib alone and is trivially DETERMINISTIC (pinned
  zip timestamps → two builds byte-identical, pinned in a test).
- Startup/ops: zipimport runs the archive in place — no extraction, no
  SCIE_BASE note, read-only installs work as-is.
- Auditability: a readable zip of exact sources beside a sha256
  manifest beats a sealed scie binary.
- The technical gate you asked about: zipimport imposes NO issue on the
  ctypes/renameat2/inotify code (ctypes loads libc independent of
  import location), and v6 has ZERO __file__-relative resources by
  design — the only v5 one (roles.json-by-adjacency) is already
  scheduled for deletion; I verified no other package-file reads exist.
  Corollary rule now explicit in the contract: v6 code never derives
  paths from __file__.
- Host contract: documented python3 floor (recommend 3.11+, matching
  the repo's own floor; syntax actually requires >=3.10) with an
  explicit startup version check exiting 2 cleanly on older
  interpreters; `#!/usr/bin/env python3` shebang via zipapp's native
  interpreter line.

Revised packaging tests (26-31): scrubbed-env outside-tree run;
POISONED-PYTHONPATH/CWD decoy-module leak test proving archive
isolation; read-only artifact + install dir with no cache; old-python
clean rejection; bin/baton-only end-to-end round-trip with an external
instance; deterministic double-build sha256 equality against the
manifest.  Round-5's build_pex.py, scie notes, per-arch manifest rows,
and the pex-helper coupling decision are all DROPPED (moot — zero
build coupling by construction).  One format, one contract.  No
implementation.
