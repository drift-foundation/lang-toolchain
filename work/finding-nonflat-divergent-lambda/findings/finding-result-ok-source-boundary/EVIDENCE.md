# Evidence: `Ok` / `HResultOk` source boundary

Snapshot: 2026-08-03.

## Commands and observations

Current full-suite failure reproduced with:

```sh
./.venv/bin/python3 -m pytest -q \
  lang/tests/driver/test_const_share_phase5_implicit_duplication.py::test_phase5_result_ok_payload_duplicates
```

Observed: one failure, diagnostic `return type 'FnResult' does not match
declared type 'ConstArc'`.

Intended local-shape probe compiled with:

```sh
./.venv/bin/python3 -m lang.driftc.driftc --stdlib-root stdlib \
  work/finding-nonflat-divergent-lambda/findings/finding-result-ok-source-boundary/repro_const_share_result_ok_local.drift \
  --entry repro::main -o /tmp/repro-const-share-result-ok-local
```

Observed: exit 1 with a Python traceback ending in:

```text
NotImplementedError: LLVM codegen v1: ok payload type mismatch for
ConstructResultOk in repro::make_ok: have ConstArc, expected Int
```

## Why the parent test is not enough

The parent test is `--test-build-only`, asserts only `rc == 0`, uses
`return Ok(a)`, and does not read `a` after the `HResultOk.value` slot. It
therefore cannot establish the compile/run or reuse claim in its docstring.
The post-walker suppression-mark assertion may make the wrapper structurally
load-bearing, but that should be tested directly rather than inferred through a
source form whose return contract is deliberately rejected.

## Current-tree facts to re-check

- Public spec §10.3: unqualified `Ok`/`Err` are Result variant constructors.
- Parser/AST-to-HIR: unqualified one-arg `Ok` is intercepted as `HResultOk`.
- TypeChecker: `HResultOk` records an internal `FnResult` type.
- Phase-2 checker: `HResultOk` reports the current function's user return type.
- Stage2: `HResultOk` constructs FnResult, while throwing HReturn also wraps a
  normal value in FnResult.

These are direct code observations. The conclusion that the AST rewrite should
be deleted is a proposal requiring implementer validation.
