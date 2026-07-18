# Production firings — drift-workflows on staged drift-0.33.83+abi21 (2026-07-17)

Three distinct firings while aligning drift-workflows with staged 0.33.83.
All are family=False, producer=MoveOut, use_count=0, consume=False,
live_out=False — the non-family-owned-producer class. Every site matches
the pinned minimal repro (`repro-move-operand-string-concat.drift`): a
`move`d String operand in a `+` concatenation. driftc aborts at the first
firing per invocation; a repo-wide sweep finds **15 such sites** across the
same three files (list below), so expect serial re-firings as each is
cleared.

All three compiling sources are in `/home/sl/src/drift-workflows` (worktree
at commit afb01f9 plus the in-progress 0.33.83 move-sweep, which does not
touch the firing lines).

## Firing 1 — microflows.runner.ir::validate_graph

Source line: `microflows/runner/src/ir.drift:1496`
(`"duplicate local binding name: " + move nm` in a match arm).

```
/home/sl/src/drift-workflows/microflows/runner/src/ir.drift:1428:5: error: internal: string ownership stake contract failure (string_arc release-arm tripwire [lastuse_release_arm]: fn 'microflows.runner.ir::validate_graph', block 'match_arm_0'[0], value '.t284', producer=MoveOut (family=False), use_count=0, consume=False, live_out=False. The in-pass last-use release arm is corpus-zero after TLR-7 (every family is pass-materialized and recognition-suppressed); a firing means either a stale unmigrated family temp or a non-family owned producer reaching a non-consuming drain. File issues/string-arc-release-arm-tripwire/ with the compiling source and this full message.) [E-AUTO-a52e7143]
```

## Firing 2 — microflows.participant_stub::_string_join

Source line: `microflows/participant-stub/src/app.drift:968`
(`joined + _dup(&sep) + move p` in the else branch of a match arm).

```
/home/sl/src/drift-workflows/microflows/participant-stub/src/app.drift:950:1: error: internal: string ownership stake contract failure (string_arc release-arm tripwire [lastuse_release_arm]: fn 'microflows.participant_stub::_string_join', block 'if_else1'[0], value '.t105', producer=MoveOut (family=False), use_count=0, consume=False, live_out=False. The in-pass last-use release arm is corpus-zero after TLR-7 (every family is pass-materialized and recognition-suppressed); a firing means either a stale unmigrated family temp or a non-family owned producer reaching a non-consuming drain. File issues/string-arc-release-arm-tripwire/ with the compiling source and this full message.) [E-AUTO-bbc7294f]
```

## Firing 3 — microflows.runner::_validate_manifest_calls

Source line: `microflows/runner/src/runner.drift:1232`
(`... + "': " + move m` inside a match arm of a throw). Surfaced once
net-tls was bumped to 0.6.2 (the 0.6.1 copy-gate rejection previously
masked it in full-runner builds).

```
/home/sl/src/drift-workflows/microflows/runner/src/runner.drift:1218:1: error: internal: string ownership stake contract failure (string_arc release-arm tripwire [lastuse_release_arm]: fn 'microflows.runner::_validate_manifest_calls', block 'match_arm_0'[0], value '.t156', producer=MoveOut (family=False), use_count=0, consume=False, live_out=False. The in-pass last-use release arm is corpus-zero after TLR-7 (every family is pass-materialized and recognition-suppressed); a firing means either a stale unmigrated family temp or a non-family owned producer reaching a non-consuming drain. File issues/string-arc-release-arm-tripwire/ with the compiling source and this full message.) [E-AUTO-45e3a0a1]
```

## Full `+ move` site inventory in drift-workflows (15)

```
microflows/participant-stub/src/app.drift:968
microflows/runner/src/ir.drift:1496
microflows/runner/src/ir.drift:1921
microflows/runner/src/ir.drift:2184
microflows/runner/src/ir.drift:2258
microflows/runner/src/runner.drift:1232
microflows/runner/src/runner.drift:1662
microflows/runner/src/runner.drift:1685
microflows/runner/src/runner.drift:1697
microflows/runner/src/runner.drift:1709
microflows/runner/src/runner.drift:2234
microflows/runner/src/runner.drift:2329
microflows/runner/src/runner.drift:2441
microflows/runner/src/runner.drift:2444
microflows/runner/src/runner.drift:2542
```

Per the intake triage we are NOT working around these in app code;
drift-workflows' 0.33.83 alignment is blocked on the string_releases /
string_arc fix. Blocked build jobs: microflows-runner, uflowsd,
participant-stub, runner-ir_graph_test, runner-ir_exec_test (base+asan).
The other 16 build jobs in the combined plan compile clean on 0.33.83.
