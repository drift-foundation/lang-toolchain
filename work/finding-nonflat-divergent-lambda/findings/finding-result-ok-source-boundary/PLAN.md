# Plan: resolve the `Ok` / `HResultOk` source boundary

This plan is reviewer guidance. Revalidate and replace any disproven theory in
implementer-owned `PROGRESS.md`.

1. Re-read the whole parent and child folders; scan
   `doc/refactor_triggers.md` again.
2. Reproduce both boundaries independently:
   - direct `return Ok(value)` clean rejection;
   - local `val r = Ok(value)` LLVM payload-mismatch ICE.
3. Inventory every production `HResultOk` producer and consumer, separating
   parsed source, synthesized compiler nodes, synthetic unit tests, public
   `Result` variant construction, and internal `FnResult` ABI values.
4. Trace the historical/spec authority for unqualified `Ok(...)`. If the
   intended meaning is not unambiguous, stop and request Slawomir's ruling;
   language-spec changes are forbidden without it.
5. Add the minimal failing end-to-end regression before the root-cause fix.
6. Implement one coherent source/type/lowering contract. Do not retain dual
   legacy modes under the pre-1.0 compatibility policy.
7. Repair the parent Phase 5 test without weakening its structural
   `ConstShare` payload guarantee. Obtain explicit approval before editing the
   existing test.
8. Run focused parser/stage1/type-checker/checker/stage2/driver/e2e gates,
   including full compile/run and ownership/drop cases.
9. Record version/ABI decisions and all corrections to this research in the
   child's `PROGRESS.md`; synchronize the parent status, then hand the parent
   back with one timestamped `IMPL-PENDING-*` token.
