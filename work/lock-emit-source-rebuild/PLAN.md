# drift lock emit --source-rebuild (downstream cert-gate contract)

Request: /tmp/drift-announce/2026-07-31T015625Z-drift-workflows-toolchain-must-ship-source-rebuild-cli.md
(drift-workflows, formalizing build-orchestrator 013936Z). Consumer cert
gates must never import resolver source from a drift-lang checkout
(DRIFT_LANG_SRC stopgap); the toolchain binary must expose the
source-rebuild dep derivation.

## Contract (from the announce, verbatim intent)
1. `drift lock emit --artifact <name> --source-rebuild
   [--run-snapshot <path>] [--package-root <dir>]...` resolves via
   `resolve_source_rebuild` (the single authority build/deploy/
   prepare-check already use). Certify = pure consumer; snapshot
   exemptions only under DRIFT_CERT_MODE=stage (mirrors prepare).
   Missing snapshot in source-rebuild mode = hard fail.
2. stdout IS the flags contract: exactly the `--dep name@M.N.P` list
   (expand_to_dep_flags shape), nothing else. Evidence + diagnostics
   → stderr.
3. Non-empty authority errors ⇒ non-zero exit, nothing on stdout.
4. Strict lane byte-for-byte unchanged without the flag — INCLUDING
   under DRIFT_CERT_MODE=certify env (unlike build/deploy/prepare,
   the env alone must NOT flip lock emit: gate recipes select the
   lane explicitly; a read-only inspection command silently changing
   its stdout contract on ambient env is the exact bug class the ask
   is closing).
5. `--json`: schema drift-lock-emit/v0 for structured consumers.

## Design
- All changes in tools/drift_deploy/drift_lock.py + an `out=` param
  on source_rebuild.print_evidence (default sys.stdout, existing
  callers unchanged; lock emit passes sys.stderr).
- Source-rebuild lane mirrors drift_prepare._run_impl: snapshot from
  --run-snapshot else DRIFT_RUN_SNAPSHOT; co-artifact overlay entries
  from the manifest's package-kind artifacts; exemptions =
  co-artifact names iff producer_output_exemption_active().
- Existing lock is EVIDENCE ONLY (read if present; absent lock is
  fine — the certify pool is candidate-only and locks are evidence).
- ResolutionError from index build (snapshot mismatch/missing entry)
  → stderr + exit 1, empty stdout.
- --run-snapshot / --package-root without --source-rebuild → error
  (exit 1): they have no strict-lane meaning.

## Tests (test_drift_lock.py)
Happy path (flags==authority graph, evidence on stderr only), missing
snapshot, env-var snapshot, authority errors → rc1 + empty stdout,
snapshot-mismatch index failure → rc1 + empty stdout, strict lane
inert under DRIFT_CERT_MODE=certify, --json both lanes, flag-misuse
guards, co-artifact manifest shape.
