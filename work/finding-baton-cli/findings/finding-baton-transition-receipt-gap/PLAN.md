# Plan: explicit adoption of pre-Baton claims

1. Preserve the fail-closed rule: response actions never synthesize a missing
   receipt implicitly.
2. Add an explicit `ROLE adopt CLAIM --actor ... --seed ...` transition that
   accepts only a protocol-valid claim naming the exact invoking role and
   agent instance.
3. Require the original pending token to be absent, validate the unchanged
   claim payload and target, and atomically publish the ordinary immutable
   Baton receipt.
4. Prove a valid manually created claim can be adopted and then answered, and
   prove a mismatched actor cannot adopt or mutate it.
5. Have K independently exercise the transition behavior, record the result in
   implementer-owned `PROGRESS.md`, and report any counterexample through
   Baton. Close the child only after both sides agree the transition contract
   is sufficient.

No compiler, language, test-suite, spec, version, or ABI change is involved.
