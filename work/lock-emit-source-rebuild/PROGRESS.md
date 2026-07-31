# Progress — lock-emit-source-rebuild

- [x] 2026-07-31 Slice opened; PLAN.md written from the announce.
- [x] 2026-07-31 IMPLEMENTED + VERIFIED:
      * source_rebuild.print_evidence grew `out=` (default stdout —
        existing build/deploy/prepare callers byte-unchanged; lock emit
        passes stderr).
      * drift_lock.py: --source-rebuild / --run-snapshot /
        --package-root (repeatable) / --json; _run_source_rebuild
        mirrors drift_prepare's snapshot loading, co-artifact overlay
        entries, and stage-mode exemption (certify/unset = pure
        consumer, exemptions None); lock read leniently as evidence
        only; ResolutionError + authority errors + CertModeError →
        stderr + rc 1 + EMPTY stdout; _emit is the single stdout
        writer for both lanes (flags line or drift-lock-emit/v0 JSON).
      * DELIBERATE divergence, documented + pinned: DRIFT_CERT_MODE
        env alone does NOT flip lock emit (contract #4 of the ask —
        strict-lane recipes keep their stdout contract inside cert
        envs).
      * Tests: 11 new pins in test_drift_lock.py (stdout-is-flags,
        evidence-on-stderr, missing-snapshot, env snapshot, snapshot
        mismatch, missing package-root, strict-inert-under-certify-env,
        flag misuse, --json both lanes, missing-lock-ok); full
        tools/drift_deploy suite 281 passed; `drift lock emit --help`
        smoke through the real lang.drift.cli dispatcher OK.
      * doc/history.md: consumer-contract section added to the 0.33.91
        entry (rides the same uncertified release — no version bump).
      Remaining for user: commit; announce reply to drift-workflows so
      the DRIFT_LANG_SRC stopgap can be deleted the release this ships.
- [x] 2026-07-31 REVIEW FINDINGS RESOLVED (P4 withdrawn by reviewer —
      breaking change, no compat requirement; conditional --json
      evidence shape kept and documented in --help):
      * P1: DRIFTC_VERSION 0.33.91 → 0.33.92 (behavior-changing
        toolchain addition per AGENTS.md versioning rule; 0.33.91 was
        frozen/staged/announced at ef7ebd14 — same-version replacement
        would recreate the skew class this feature closes). ABI stays
        22. history.md: section MOVED out of the 0.33.91 entry into
        its own dated 0.33.92 entry with versioning rationale.
      * P2: DRIFT_PKG_ROOT is now the --package-root default
        (os.pathsep-separated; explicit flags win) — the announced
        flagless invocation works under the documented cert env
        contract. Docstring/--help/history updated.
      * P3: six new pins — DRIFT_PKG_ROOT default + flag-over-env,
        --run-snapshot flag-over-env, authority-errors branch (mocked
        at the authority module: the structural gate is defence-in-
        depth unreachable through a well-formed snapshot, documented
        in the test), invalid DRIFT_CERT_MODE → rc1 + empty stdout,
        stage-mode co-artifact overlay (peer pin lands in flags),
        --package-root-without-flag misuse.
      Suite: test_drift_lock.py 30 passed; full tools/drift_deploy
      287 passed. Trigger scan: remaining "0.33.91" strings are
      accurate historical citations only.
- [x] 2026-07-31 ROUND-2 REVIEW RESOLVED: (1) --source-rebuild help no
      longer claims --package-root is required — "a candidate pool
      (--package-root or DRIFT_PKG_ROOT)". (2) Stage-exemption now
      GENUINELY pinned: replaced the overlay-only test (which passed in
      any mode) with a two-artifact world carrying an ON-DISK
      co-artifact .dmp absent from the snapshot —
      test_stage_exemption_admits_disk_co_artifact (stage: admitted via
      snapshot_exempt_ids, peer pin in flags) +
      test_certify_fails_closed_on_unsnapshotted_disk_co_artifact
      (certify: index gate rejects, rc 1, EMPTY stdout). Loader mock
      keys on .dmp path so each package reads its own manifest.
      test_drift_lock.py 31 passed; full tools/drift_deploy suite green.
