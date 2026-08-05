# PROGRESS: fast-or-fail promotion (implementer-owned)

Actor: K.  Released 2026-08-05T21-05-36Z (legacy promotion completed);
plan + full test-migration ledger pre-approved.

## Implementation — COMPLETE

tools/drift_corpus_check.py:
- HANDOFF_SCHEMA_VERSION=2, CANDIDATE_KIND="verified_fresh_observation".
- `_export_handoff` builds the schema-2 candidate: kind, origin, FULL
  run-snapshot object, verbatim verify run_meta (same fields _emit_run
  writes, duration MEASURED at publication), universe, projections,
  counters, observed/projected, payload_sha256 seal over canonical JSON.
- `_publish_fresh_candidate` gains started/jobs and builds run_meta;
  both verify publication sites updated.
- run_check is REPORT-ONLY: canonical export removed; message points at
  report.json.
- `_validate_handoff` (schema 2): kind check; digest seal verified FIRST
  (SUPERSEDED in round 2: the seal covers the canonical PARSED payload —
  semantic mutation, not literal byte flips); snapshot-object and
  run_meta structural checks (SUPERSEDED in round 2: upgraded to the
  central fingerprint authority + strict value validation);
  origin-vs-snapshot composite cross-check; all prior
  universe/bucket/partition/counter checks retained.
- `run_promote` REWRITTEN fast-or-fail: candidate load + full validation
  + exhaustive-observed/empty-projected + zero-hard-gates, all BEFORE
  tree work; then current-universe discovery + toolchain + snapshot
  (SUPERSEDED in rounds 2/4: "source hashing only" missed the
  runtime-archive build path — the probe is now the PASSIVE
  toolchain_fingerprint_passive with content-identity staleness) +
  exact identity;
  then `_equals_current_baseline` no-op or `_staged_install` from the
  candidate with snapshot + run_meta VERBATIM.  Zero `_compile_set`
  calls, no scratch, no retained actual, no fallback.  `jobs` removed
  from its signature.
- `_expectation_from_handoff` deleted (dead).  (`_expectation_from_
  baseline`/`_expectation_consistency` retained — verify still uses
  them; an over-wide splice during editing briefly removed them, caught
  by the suite and restored from HEAD.)
- CLI: `-j/--jobs` default None; explicit jobs with `--promote` rejected
  (exit 2); default resolved post-parse for check/verify.

tools/drift_corpus_audit.py: `_emit_run(..., metadata=None)` — when
given, metadata.json is written VERBATIM (fast promote passes the
candidate's run_meta; the review gap never leaks into duration_s).

justfile: promote recipe loses `-j`; corpus comment block + verify
two-run wording updated to one-compile lifecycle.
doc/ownership-corpus-gate.md: lifecycle diagram, promote section
(fast-or-fail), re-baseline recipe.  Tool docstring: all three lanes.
doc/history.md: 0.35.0 tooling paragraph extended (fast-or-fail
promotion, schema-2 candidate, report-only check, rejected promote
jobs, ONE full compile).

## Approved test migrations — APPLIED

test_drift_corpus_check.py:
- DELETED: test_handoff_exported_and_cache_independent (replaced by
  test_check_is_report_only_never_writes_candidate),
  test_promote_unexpected_flip_fails_without_mutation,
  test_promote_start_finish_mismatch_aborts.
- `_write_handoff` → schema-2 (+ digest); new `_seal` helper; the
  invalid-handoff matrix reseals after each mutation so targeted
  branches are exercised, plus NEW cases: wrong kind, snapshot/origin
  disagreement, bad run_meta, and unsealed digest corruption
  (test_corrupted_candidate_digest_rejected).
- All promote consumers → `run_promote(base, extra=[])`, zero-compile
  assertions (expectation_installs asserts h.compiles == []);
  hard-gate candidate now exit 2 with no staging dir; dev-lane
  universe-drift test asserts report-only.

test_corpus_verify_candidate.py:
- two-full-runs pin INVERTED to one-full-run
  (test_drift_then_promote_one_full_run_total, promote compiles zero);
  bootstrap pin promote-compiles-zero.
- NEW: test_promote_preserves_verify_run_metadata (installed
  metadata.json == candidate run_meta, byte-for-field),
  test_tree_change_after_verify_fails_fast_no_compile (toolchain moved
  after verify → exit 2, zero compiles, baseline byte-identical),
  test_promote_rejects_explicit_jobs (-j and --jobs → exit 2).

## Verification

pytest lang/tests/tools/ = **185 passed** (twice: after test migration
and after doc/justfile/history edits).  git diff --check clean.
Remaining completion evidence deliberately Slawomir-owned: the real
verify→promote production smoke (matrix case 10).

## Round 2 (review 21-28-33Z) — all three P1s + sweep DONE

- P1 runtime build: NEW `_fp.resolve_runtime_identity` (passive twin of
  prebuild_runtime — resolves + hashes the EXISTING archive via
  runtime_archive_path/_needs_rebuild; absent/stale raises toward a
  fresh verify, never builds) + `toolchain_fingerprint_passive`
  (identical output shape/composite on an unchanged tree);
  `collect_toolchain_components` accepts a precomputed runtime identity.
  Promote probes via `_toolchain_passive` (InfraError wrapper).  PINS:
  passive path fingerprints a real artifact with prebuild AND
  build_runtime_archive monkeypatched to explode; promote succeeds with
  the BUILDING `_toolchain` exploding; missing artifact through the REAL
  passive probe → exit 2, zero compiles, baseline byte-identical, build
  authority exploding untouched.
- P1 snapshot validation: `_validate_handoff` routes the embedded
  snapshot through `_fp.validate_fingerprint` (exact key sets, component
  shapes, RECOMPUTED composites) + kind check.  `_valid_handoff` builds
  a genuinely valid snapshot; NEW resealed nested-tamper cases (mutated
  toolchain component under old composite; missing snapshot key).
- P1 run_meta values: finite non-boolean started_unix>0, duration_s>=0,
  jobs>0 int, nonempty strings; repo_root/python stay HISTORICAL verify
  provenance by design (documented in-line — a moved checkout must not
  reject a valid delayed promotion).  NEW value-mutation cases
  (boolean/inf/negative/zero-jobs/empty-string).
- P1 diagnostics: check completion message directs at verify
  (report-only); verify-drift message says fast-or-fail zero-compile
  promote; --promote argparse help rewritten; `_baseline_md` provenance
  describes the verify-observation → reviewed → fast-install chain.
- P2 sweep: fingerprint module docstring (verify prebuilds / promote
  passive split); gate doc intro (CI approval wording, per-lane
  scratch/compile claims) + verify-section reproduce claim; justfile
  check block report-only; both test module docstrings; digest comment
  softened to canonical-parsed-payload semantics; HISTORY COLLAPSED into
  one final-contract paragraph (no interim two-compile state).
- Harness: `_toolchain_passive` mocked alongside `_toolchain`.

FINAL: pytest lang/tests/tools/ = **195 passed**; git diff --check
clean.  Real verify/promote production smoke remains Slawomir-owned.

## Round 3 (review 21-47-01Z) — contradiction sweep + boundary pin

- All seven cited spots corrected: gate-doc check bullet (report-only,
  no candidate export); the two run_check comments (reviewed-gate claim
  → verify as the fresh authority; hard-gate NOTE no longer claims a
  promotion candidate exists); both test module docstrings/comments
  (check header, bootstrap "second full run", digest-test byte-flip
  wording); PROGRESS round-1 record annotated with SUPERSEDED markers
  (byte-flip wording; "source hashing only" corrected to include the
  passive runtime-artifact resolution).
- P2 pin added: test_promote_rejects_invalid_metadata_candidate_at_
  boundary — a RESEALED invalid-jobs candidate through run_promote:
  exit 2, the passive identity probe monkeypatched to explode (proving
  rejection happens BEFORE any tree work), baseline bytes unchanged.

FINAL: pytest lang/tests/tools/ = **196 passed**; git diff --check
clean.

## Round 4 (review 21-53-32Z) — passive-probe determinism + content identity

- `resolve_runtime_identity` takes the CONTENT-IDENTITY route (reviewer
  option 1): the mtime-freshness private copies (_needs_rebuild/
  _runtime_deps) are GONE — absence is the only local failure; staleness
  is decided by the full composite (compile-source/stdlib digests cover
  the runtime sources; archive bytes hash joins them), so
  content-identical mtime changes and deployed read-only archives
  promote fine.  Docstring states the rationale.
- test_passive_fingerprint_never_builds → ..._and_matches_building:
  cold-cache deterministic (the ordinary BUILDING fingerprint runs FIRST
  and prebuilds the archive; only then are both build entry points
  monkeypatched to explode) and pins EXACT components+composite equality
  between passive and building fingerprints.
- NEW test_passive_identity_ignores_archive_mtime: content-identical
  archive with a far-past mtime yields the identical composite
  (mtime restored in finally).
- PROGRESS round-1 bullets now carry ADJACENT "SUPERSEDED in round N"
  markers (the round-3 annotation had been lost to an aborted edit
  batch; verified present this time).

FINAL: pytest lang/tests/tools/ = **197 passed**; git diff --check
clean.

## Round 5 (review 22-00-21Z) — final wording alignment

All five active statements of the superseded absent/stale rule corrected
to the content-identity contract (passive docstrings ×2, promote-lane
docstring "source hashing only", run_promote comment, history paragraph,
missing-artifact test comment).  pytest lang/tests/tools/ = **197
passed**; git diff --check clean.

## Round 6 (review 22-06-21Z) — shared-state hygiene + hash fail-closed

- test_passive_identity_ignores_archive_mtime rebuilt: copies the
  established archive into a PRIVATE tmp cache tree at the same
  cache-relative path, monkeypatches runtime_archive_cache_root +
  runtime_archive_path to it, mutates only the copy's mtime, and pins
  exact components+composite equality with the building fingerprint.
  The shared runtime cache is never touched.
- resolve_runtime_identity computes the archive hash ONCE and raises
  RuntimeError unless it is a valid hex digest (existing-but-unreadable/
  racing artifact fails closed; no None identity in the composite).
  NEW pin: _file_sha256 → None ⇒ RuntimeError, build authority
  monkeypatched to explode and untouched.

FINAL: pytest lang/tests/tools/ = **198 passed**; git diff --check
clean.

## PRODUCTION SMOKE — COMPLETE (matrix case 10)

Slawomir's `just ownership-corpus-promote` installed the reviewed fresh
candidate in seconds with zero compilation/builds: installed
fingerprint.json exactly equals the candidate snapshot, metadata.json
exactly equals the candidate run_meta (verbatim provenance contract
verified LIVE), git diff --check clean, and the tracked projection delta
remains the jointly reviewed 0.35.0 identity-only change.  Finding
FINISHED; ready to commit.
