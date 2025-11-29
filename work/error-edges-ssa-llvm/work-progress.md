### Status: SSA→LLVM call-with-edges stubbed; try/else e2e green

- SSA backend now accepts `mir.Call` terminators with `normal`/`error` edges and threads edge args into successor PHIs, but it **hard-stubs the error path**: it calls the callee, binds the value, fabricates `err == null` via `i1 0`, and always branches to the normal edge. Calls with edges in any other shape still hard-error.
- Added run-mode e2e `try_call` (try/else over a call that succeeds) and it passes via SSA→LLVM; all existing run-mode e2e remain green.
- Checker now expects `Int` for array indices in both read and write paths (aligned with word-sized Int).

### TODO

- Implement the real `{T, Error*}` ABI in SSA→LLVM call-with-edges: extract the pair `{val, err}`, branch on `err != null`, pass `err` to the error successor, and only take the normal edge when `err` is null.
- Add a failing-path e2e once the real error branching works.
- Keep rejecting any unsupported call-with-edges shapes until the ABI is fully wired.
