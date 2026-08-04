# Ownership-corpus verification drift — reviewer evidence

Epistemic state: commands and file comparisons below are **Observed**; the promotion recommendation is **Proposed** pending independent implementer confirmation and Slawomir's final authorization.

## Verification result in scope

`just ownership-corpus-verify` performed a fresh full compile and retained its actual at `build/tmp/ownership-corpus-actual/`. It rejected the committed baseline only for universe/source-hash identity drift and left the baseline untouched.

Fresh-run metadata:

- 1,338 included fixtures
- 969 `compiled_ok`
- 369 expected compile/check failures
- 49 exclusions
- 16 jobs
- 2,429.5 seconds
- driftc `0.34.2`, ABI `22`, corpus tool `1.7.1`

Baseline environment was driftc `0.34.1`, ABI `22`, corpus tool `1.7.1`.

## Exact structural comparison

Using a name-keyed `jq` comparison of the baseline and retained manifests:

- fixture counts are equal: 1,338 / 1,338;
- fixture-name sets are equal;
- inclusion rules are equal;
- ordered `compiled_ok` buckets are equal;
- ordered `failed` buckets are equal;
- exclusions and reasons are equal (49 / 49);
- exactly three fixture hashes differ.

The three changed fixture hashes are:

1. `bitwise_uint_ops`
   - baseline: `2e0316760b4f1879e6aa4624dc083bfdbdfebf903f63e991a537dc1d632b1ea0`
   - actual: `d61e0d85497e973b470b3c640482f403090dca6998fa0589e95f080d3d3e7f34`
2. `closures_explicit_captures_move_use_after_move_rejected`
   - baseline: `5a6e8a3cf1188572e8b8d8d20f85fea20ccc50539cbf22d62a6e703539c89b70`
   - actual: `14e538bc050f260714053d5b0d90ec398fa2a49ed352f532610d0c6536fb327c`
3. `fnptr_lambda_capture_rejected`
   - baseline: `3722fd0feb9bfef443db1010b29f98ec8fed1ce536e349924a6abc670e6ce51b`
   - actual: `7ae6a6314e31b5cb018e892c328178baefcd0c9cddd5a0066176115a2de61865`

## Semantic/audit comparison

The aggregate files are byte-identical:

`63c429102f57ea7386a589d936731d98253664c44eb35d5413dc038fecd1eb60`

The complete per-fixture projection files are byte-identical:

`1ad6cb450054e1341f1cfc31cb2ab9c4a23c68dab34644742db1a537a9def32e`

Therefore every recorded ownership projection and all 14 aggregate counters are unchanged. The fresh verifier reported universe drift, not projection, aggregate, bucket, exclusion, or hard-gate drift.

## Attribution of the three fixture hashes

`git diff e211863c..HEAD` (the current reviewed-baseline promotion commit through current HEAD) accounts for all three hashes:

1. `bitwise_uint_ops/main.drift`: `return x` became `return cast<Int>(x)`. This is the Slawomir-approved POSIX/C-like `main -> Int` correction for a test whose internal computation remains `Uint`; expected exit remains 254.
2. `closures_explicit_captures_move_use_after_move_rejected/main.drift`: the now-invalid bare stored capturing lambda was migrated to the approved `core.callback0(...)` representation. It still moves `x` at construction and still expects the same borrow-checker `use after move of 'x'` failure.
3. `fnptr_lambda_capture_rejected/expected.json`: source is unchanged; the expected diagnostic now distinguishes the implicit borrowed capture (`closures with borrowed captures are non-escaping in v0`) from the separately pinned value-capture/fn-pointer diagnostic.

These fixture changes preserve their positive/negative bucket classifications, which the retained manifest independently confirms.

## Fingerprint attribution

- `static_universe_digest` changes because the three fixture hashes changed.
- `compile_source` changes because this branch changes compiler source.
- `audit_tool` changes because `tools/drift_corpus_check.py` has one comment-only edit removing the obsolete claim that promotion is never wired into `run-all-tests.sh`; there is no corpus algorithm change since the baseline.
- Composite toolchain/run-snapshot hashes consequently change.
- ABI remains 22 as expected for internal compiler/checker/lowering behavior without a runtime boundary-shape change.

## Promotion prerequisite

`build/tmp/ownership-corpus-projection.json` exists but is stale (mtime `2026-08-02 23:31:53 -0600`) and predates this fresh verification/current fingerprint. The promotion command is designed to reject a stale handoff.

Before promotion, run the non-promoting developer lane once:

```text
just ownership-corpus-check
```

It should export a current projection candidate. Because the fresh verifier already compiled the complete universe, this developer check is only the required handoff-generation step; its candidate must still be compared against the retained actual and the facts above. Do not run `ownership-corpus-promote` until both reviewer and implementer agree and Slawomir authorizes the promotion.

## Reviewer conclusion

**Proposed:** approve re-baselining after independent implementer confirmation and generation/review of a current projection handoff. Current evidence shows identity/fingerprint refresh only, with no ownership-counter or per-fixture semantic drift.
