# Baton message

Timestamp: 2026-08-05T20-40-10Z
From role: implementer
Actor: k
To role: reviewer
Kind: planning_response
Thread: corpus_fast_fail_promotion

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T20-38-20Z-bc29f40c414e-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T20-38-20Z`
Planning review for finding-corpus-fast-fail-promotion.  Read-only pass (no
fingerprinted sources touched; legacy promotion still running).  Verdict:
the ruling and plan are sound and implementable on the current seam; I
concur with fail-closed fast promote, verify-only candidate production, and
report-only check.  Grounded findings, one metadata trap confirmed in code,
two design choices, and the exact migration ledger below.

CANDIDATE COMPLETENESS (verified against the emitters): rendering the
baseline needs, beyond the current schema-1 fields — (a) the FULL run
snapshot object (fingerprint.json is written verbatim from it; composites
alone cannot reconstruct it); (b) the complete metadata object.  TRAP
CONFIRMED: `_emit_run` computes `duration_s = time.time() - started` AT
EMISSION TIME, so a fast promote reusing verify's `started` would silently
absorb the human review gap into the baseline's metadata.  The candidate
must carry verify's MEASURED duration (and started_unix/jobs/python — I
recommend carrying the complete metadata object verify would have written,
installed verbatim), and staged emission must accept an explicit metadata
object instead of recomputing.  BASELINE.md renders from
snapshot+counters+universe — covered once the snapshot rides along.
(c) A producer marker (`verified_fresh_observation`) + schema bump — I
recommend schema_version 2 with the OLD schema explicitly rejected by
message ("re-run ownership-corpus-verify"), pre-1.0 no-compat.
(d) payload_sha256 over canonical JSON: yes — cheap, and the record layer
already has the exact `_payload_digest` pattern to reuse.

PRODUCER SEPARATION: agree with report-only check (option 3-cleaner).
check's work-dir report.json already carries observed/projected partitions
and deltas; a second, differently-named developer export would just be a
new ambiguity surface.  One consequence to accept explicitly: after this,
NOTHING but verify can mint a candidate — which is precisely the ruling.

FAIL-CLOSED VALIDATION in promote, ordered: lock → load+schema/digest/
producer/exhaustive-observed/empty-projected/zero-hard-gate/internal-
consistency validation (all BEFORE any tree work) → discover universe +
recompute toolchain/universe snapshot (hashes every fixture source — this
IS the staleness protection, still zero compiles) → exact identity with
the candidate's snapshot+universe → stage/install from candidate with the
existing post-install reload/equality/fingerprint proofs, metadata
verbatim.  Promote never creates compile scratch, never retains an actual,
and its only mutation path is the existing staged install.

REPRODUCTION COMMAND: defer entirely.  Promote's staged-install proofs
plus verify's freshness make it redundant today; adding it now would be
keeping old code alive under a new name.  If audit demand appears, it is a
clean follow-up finding.

-j ON PROMOTE: keep the shared argparse flag (smallest CLI change; it
still governs check/verify), drop `-j` from the justfile promote RECIPE
and scrub the promote help/doc text of worker/recompile claims.  Promote
silently ignoring -j is acceptable pre-1.0; rejecting it would complicate
the shared flag for no safety gain.

EXACT EXISTING-TEST MIGRATION LEDGER (for Slawomir's approval when the
implementation handoff comes) — lang/tests/tools/test_drift_corpus_check.py:
- DELETE test_handoff_exported_and_cache_independent (pins check's
  canonical export; replace with a NEW pin that check does NOT write
  HANDOFF_PATH).
- DELETE test_promote_unexpected_flip_fails_without_mutation (its scenario
  IS the second compile; the protection it encoded becomes the
  snapshot-identity rejection, pinned anew).
- DELETE test_promote_start_finish_mismatch_aborts (promote takes ONE
  snapshot now; no start/finish pair exists).
- MIGRATE the `_write_handoff` helper + its nine remaining promote
  consumers to the schema-2 candidate writer (rejects_different_toolchain,
  requires_handoff_no_baseline_fallback, byte_preserving_noop,
  handoff_expectation_installs, stale_handoff_fails, malformed_handoff,
  hard_gate_fails [becomes candidate-level rejection, zero compiles],
  missing/stale/current-fingerprint install trio) — same intents,
  zero-compile expectations, plus compile-count == 0 assertions.
- Header docstring promote-lane description rewrite.
Also in scope (my own just-landed file, listing for completeness):
test_corpus_verify_candidate.py's two-full-runs and bootstrap tests invert
to one-full-run + zero-compile-promote pins, and its candidate assertions
gain the schema-2 fields.  test_ownership_corpus_check.py: prose-comment
touch only (~line 159 promote description); no assertion changes expected.
Docs/justfile/history: gate doc lifecycle to one-compile-plus-fast-
promote, promote recipe loses -j, 0.35.0 history paragraph extended.

VALIDATION MATRIX: your ten cases are right; I would add (11) candidate
digest corruption (flip one byte → fail closed before snapshot work) and
(12) metadata-provenance pin (delayed promote installs verify's duration,
not the gap — directly pins the confirmed trap).

Standing by for the legacy promotion to exit + your go (and I will surface
the ledger above for Slawomir's explicit approval before touching the
existing tests).
