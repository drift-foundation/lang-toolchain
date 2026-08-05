# Reviewed ownership-corpus baseline

The authoritative golden state `just ownership-corpus-verify` (CI/cert)
checks against, and the seed for a clean clone's `just ownership-corpus-check`.
The exact universe and counters live in the machine files beside this note;
`projections.json` holds the per-fixture ownership projections used for fast
clean-clone seeding and exact per-fixture verification.

## Provenance

Produced ONLY by `just ownership-corpus-promote` (fast-or-fail, ZERO
compiles): the complete fresh observation came from a single
`ownership-corpus-verify` run — stable start==end toolchain fingerprint,
every hard gate at zero — published as the digest-sealed candidate,
reviewed, validated against the then-current tree identity, and installed
via staged writes with the verify run's snapshot and metadata verbatim.
CI/cert (`ownership-corpus-verify`) NEVER writes this baseline.  The Git
commit that lands these files IS the approval; reviewer identity and date
come from Git history.

| field | value |
|---|---|
| driftc / ABI | **0.35.0** / **ABI 22** |
| run snapshot composite | `0b9b0a242e4d8df15c80cef69a810cc612cdd98652bcb821f4bbd07d6f6c640d` |
| toolchain composite | `d9ac0d8ea5d4fd655a84b0a2587945196b08501e0e38ad5fb29255913a9ea371` |
| discovered fixtures | 1338 included / 49 rule-excluded |
| counter keys | 14 |

## Distinct from the ownership matrix

The 51-fixture ownership **matrix** (`just ownership-matrix-check`, inside
`just test`) and this full-corpus **audit** are separate gates.  Earlier
version-by-version provenance is in `doc/history.md`; the process is documented
in `doc/ownership-corpus-gate.md`.
