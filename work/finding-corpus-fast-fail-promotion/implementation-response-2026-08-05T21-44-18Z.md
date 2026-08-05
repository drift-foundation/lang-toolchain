# Baton message

Timestamp: 2026-08-05T21-44-18Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: corpus_fast_fail_promotion

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T21-28-33Z-5df43ee43ae8-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T21-28-33Z`
Revision 2 handed back — all three P1s fixed with the requested boundary
pins, diagnostics corrected, and the one-contract sweep landed including
the collapsed history entry.  Tools suite: 195 passed; diff-check clean.
PROGRESS.md round-2 section has the full inventory; the essentials:

P1 runtime build: promotion's identity probe is now genuinely passive.
New `resolve_runtime_identity` resolves and hashes the EXISTING archive
(runtime_archive_path + _needs_rebuild; absent/stale raises toward a fresh
verify) and `toolchain_fingerprint_passive` yields the IDENTICAL composite
on an unchanged tree; verify's prebuild-before-fingerprint authority is
untouched.  Three pins: the passive path fingerprints the real artifact
with BOTH prebuild_runtime and build_runtime_archive monkeypatched to
explode; promote succeeds from a valid candidate with the BUILDING
_toolchain exploding; and a missing artifact through the REAL passive
probe (InfraError wrapper preserved) is a controlled exit 2 with zero
compiles, the baseline byte-identical, and the build authority never
touched.

P1 snapshot: the candidate validator now routes the embedded snapshot
through _fp.validate_fingerprint (exact key sets, component shapes,
recomputed composites) plus a kind check; the unit fixture builds a
genuinely valid snapshot, and resealed nested-tamper cases (component
mutation under the old composite; missing snapshot key) reject before any
tree work.

P1 run_meta: values validated — finite non-boolean started_unix > 0,
duration_s >= 0, integer jobs > 0, nonempty strings — with
boolean/inf/negative/zero/empty mutation cases through the validator; and
your design question answered in-line: repo_root/python remain HISTORICAL
verify provenance, shape-validated only, so a moved checkout never rejects
a valid delayed promotion.

Diagnostics: check's completion message now directs at verify for
promotable evidence (report-only wording), the verify-drift message
describes the zero-compile fast promote, --promote's argparse help is
rewritten, and _baseline_md's provenance section describes the actual
chain (single verify observation, digest-sealed candidate, reviewed, fast
validation/install with verify's snapshot+metadata verbatim) — so the
production smoke will install truthful provenance.

Sweep: fingerprint module docs now document the verify-prebuilds /
promote-passive split; the gate doc's CI/approval and per-lane
scratch/compile claims are corrected; the justfile check block says
report-only; both test module docstrings state the final contract; the
"any byte flip" claim is softened to canonical-parsed-payload semantics
(reseal-aware); and the pending 0.35.0 history now carries ONE collapsed
final-contract paragraph with no interim two-compile state.

Verification: pytest lang/tests/tools/ = 195 passed (full directory);
git diff --check clean.  No real corpus verify run, per your staleness
instruction — the production verify/promote smoke stays with Slawomir
after review clears.
