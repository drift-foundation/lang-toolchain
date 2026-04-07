Summary: opaque clang failure on very long single-line input (~8000+ else-ifs / very large IR)

Classification
- Codegen / clang interaction (downstream)
- Priority: low (only affects pathological inputs; row #5 already pinned that no Python crash occurs)
- Surfaced by: row #5 deep-depth driver test during the robustness Tier 1 work, 2026-04-07

Symptom
- Compiling a Drift source with ~5000–8000 chained `else if` clauses on a single line (the row #5 `gen_else_if_chain(N)` probe) **intermittently** fails with this stderr:

  ```
  <source>:?:?: error: clang failed: clang: warning: argument unused during compilation: '-fuse-ld=gold' [-Wunused-command-line-argument]
  warning: overriding the module target triple with x86_64-pc-linux-gnu [-Woverride-module]
  ```

- That is the *entire* stderr — only two warnings, no actual error message, but driftc reports rc=1 and "clang failed".
- The IR file at this depth is large (~12 MB at d=8000 from a one-shot probe).
- **Load-dependent.** In solo runs the d=5000 case completes cleanly in ~54s with rc=0. Under high parallel pytest load (16-way `just test` with `--dist=worksteal`) the same input intermittently produces the failure shown above. This points strongly toward a memory-pressure or timing-sensitive condition in clang itself, not a deterministic limit in the IR shape.

Why this is not a Python crash and not a row #5 / #11 / #12 regression
- No `Traceback`, no `RecursionError`, no `value for 'column' too large` (the LLVM debug-info column overflow already fixed in 0.27.164).
- The row #5 driver tests `test_else_if_chain_5000_no_python_crash` and `test_else_if_chain_8000_fails_cleanly_no_python_traceback` both pin the absence of Python crashes and column overflows at these depths. The d=5000 test was originally strengthened in 0.27.164 to assert rc=0, then weakened in 0.27.171 back to "no Python crash" after this issue was observed under parallel load.
- The robustness contract is satisfied: failure mode is a downstream clang error, not a compiler crash. Users get rc=1 with a (currently uninformative) clang failure.

Why this is still worth investigating
- Two hypotheses for the silent clang failure:
  1. **clang OOM or internal limit**: at IR sizes ≥ ~10–12 MB on highly repetitive single-line inputs, clang may be hitting an internal memory or parser limit and aborting without printing a stack frame to stderr. The two warnings suggest clang at least starts, parses some flags, and then fails after.
  2. **clang exit-without-message bug**: clang may be exiting with non-zero status but producing no actual error text on stderr. The driver wraps this as `<source>:?:?: error: clang failed: <captured-stderr>`, which produces the misleading "clang failed: <warnings>" message.
- Neither hypothesis is confirmed. The minimum useful next step is to capture clang's exit code and any stdout output (driftc currently captures only stderr) and to try running the same `.ll` file through clang manually to see if a longer-form error appears.
- The bigger user-facing problem: driftc's "clang failed" wrapper presents two clang **warnings** as if they were the error message. This is misleading regardless of the underlying clang behavior — the wrapper should distinguish "clang exited non-zero with no error" from "clang exited non-zero with this error message" and surface the distinction.

Reproducer
- `work/robustness/probe.py::gen_else_if_chain` at d=8000.
- Or directly:

  ```python
  parts = [f"if x == {i} {{ return {i}; }}" for i in range(8000)]
  body = " else ".join(parts) + " else { return -1; }"
  src = f"module main;\npub fn main() nothrow -> Int {{\n\tvar x = 0;\n\t{body}\n}}\n"
  ```

  Compile with `python -m lang.driftc.driftc --dev --stdlib-root stdlib /tmp/x.drift --entry main::main -o /tmp/x.bin`.

Reproducer is slow: ~60s wall-clock at d=8000, dominated by Tier 3 type-checker scaling on the way through the pipeline. The row #5 driver test
`test_else_if_chain_8000_fails_cleanly_no_python_traceback` already runs this case in CI and pins the contract; this issue is about *understanding* the failure, not pinning it.

Investigation pointers
- `lang/driftc/driftc.py` has the clang invocation site (search for `clang failed`). Capture both stdout and stderr and exit code; surface them distinctly in the diagnostic instead of concatenating warnings into the "error" message.
- Run clang directly on the IR file dumped by driftc to see if the manual run produces a different output than the subprocess wrapper.
- Check whether the failure goes away with `-O0` vs `-O2` (clang's optimizer might be the actual choke point on the deep linear basic-block chain).
- Check whether the failure is wall-clock-bounded (clang internal timeout) or memory-bounded (OOM kill from cgroup).

Owner
- Unassigned. Low priority — row #5 is satisfied by the existing "no Python crash" contract.

Cross-references
- `lang/tests/driver/test_else_if_chain_pipeline.py::test_else_if_chain_8000_fails_cleanly_no_python_traceback` — the pinning regression
- `work/robustness/robustness-matrix.md` row #5 — explicitly notes this as out-of-scope downstream
- `history.md` 0.27.162 / 0.27.163 — row #5 closeout text mentions "downstream concerns separate from row #5"
