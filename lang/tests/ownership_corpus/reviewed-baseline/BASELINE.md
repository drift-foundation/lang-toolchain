# Reviewed ownership-corpus baseline

The authoritative golden state `just ownership-corpus-verify` (CI/cert)
checks against, and the seed for a clean clone's `just ownership-corpus-check`.
The exact universe and counters live in the machine files beside this note;
`projections.json` holds the per-fixture ownership projections used for fast
clean-clone seeding and exact per-fixture verification.

## Provenance

Produced ONLY by `just ownership-corpus-promote`: a fresh full compile that
EXACTLY reproduced a reviewed developer candidate (the projection handoff),
under a stable start==end toolchain fingerprint with every hard gate at zero,
then installed via staged writes.  CI/cert (`ownership-corpus-verify`) NEVER
writes this baseline.  The Git commit that lands these files IS the approval;
reviewer identity and date come from Git history.

| field | value |
|---|---|
| driftc / ABI | **0.34.2** / **ABI 22** |
| run snapshot composite | `8b100d170f4f34ef12217e38472d112da027ee178946f7f37a8f300c57a3bb95` |
| toolchain composite | `59a61c8e6333ddbe61b365e4631666cfa1d257ed46b05c3e8b5c64fc338a5e33` |
| discovered fixtures | 1338 included / 49 rule-excluded |
| counter keys | 14 |

## Distinct from the ownership matrix

The 51-fixture ownership **matrix** (`just ownership-matrix-check`, inside
`just test`) and this full-corpus **audit** are separate gates.  Earlier
version-by-version provenance is in `doc/history.md`; the process is documented
in `doc/ownership-corpus-gate.md`.
