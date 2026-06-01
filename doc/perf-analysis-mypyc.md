# mypyc spike — perf analysis

A throw-away spike to see whether
[mypyc](https://mypyc.readthedocs.io/) could compile `driftc`
"mostly as-is" and whether the resulting native extensions would
deliver enough wall-clock reduction to justify productizing.

Run against driftc as of 0.32.22 timing-instrumentation HEAD.
Environment: Linux x86_64, Python 3.13, `mypy[mypyc]==2.1.0`
installed into the project venv (system needed
`python3.13-dev` for `Python.h`).

## Goal

Evaluate whether mypyc can compile driftc mostly as-is, and whether
the result looks worth productizing.

## Verdict

- **Technically viable on small / well-typed modules** — toolchain
  works on this codebase, produces native `.so` files, runtime
  behavior matches the `.py` source.
- **Not "mostly as-is" for the hot compiler files** — the modules
  with the biggest payoff potential need substantial mypy-strict
  typing cleanup before mypyc will compile them.
- **Productization not recommended right now.** The cleanup cost
  outweighs the expected speedup. Cheaper alternatives (pure-Python
  optimization, targeted PyO3) should be exhausted first.

## What worked

- **Install path**:
  `pip install 'mypy[mypyc]'` into the project venv. One-time
  system `apt install python3.13-dev` to get `Python.h`.
- **Small, well-typed modules compiled cleanly** on the first real
  attempt after one trivial fix:
  - `lang/driftc/_events.py`
  - `lang/driftc/env_flags.py`
  - `lang/driftc/core/function_id.py`
  - Total: ~473 LOC across the three.
  - One annotation fix needed: `__exit__(...) -> bool` → `-> None`
    (mypyc rejects `bool` for `__exit__` because it implies the
    method may return `True` and swallow exceptions; `None` /
    `Literal[False]` are the supported shapes). Generally-correct
    fix, kept in `lang/driftc/_events.py` regardless of mypyc.
- **Runtime equivalence**: with the three `.so` files overlaying the
  `.py` source, **16/16 timing tests pass** plus a broader sweep
  (`test_pkg_v1_duplicate_roots_resolved_closure`,
  `test_abi_version_stamp`) — **20/20** with the compiled extensions
  loaded.
- **Microbench on the compiled module**:
  `events.timed(...)` cheap path (no sink installed) went from
  **~150ns → ~71ns per call** — about 2x. The active-sink path
  (records into a sink) measured ~210ns/call. Consistent with
  mypyc's typical 1.5–3x speedup on type-hinted Python.

## What failed / cost

mypyc requires every file in its compile-graph to be **mypy strict-
clean** (zero errors). The hot compiler files are not.

Per-file local error counts (run with
`mypy --follow-imports=silent --ignore-missing-imports` to isolate
each file from its imports):

| File | LOC | Local errors | Density |
|---|---:|---:|---|
| `lang/driftc/type_checker.py` | 12,862 | 374 | 1 per ~34 LOC |
| `lang/driftc/stage2/hir_to_mir.py` | 11,481 | 134 | 1 per ~86 LOC |
| `lang/codegen/llvm/llvm_codegen.py` | 10,182 | 68 | 1 per ~150 LOC |
| `lang/driftc/parser/parser.py` | 3,924 | 64 | 1 per ~61 LOC |
| `lang/driftc/borrow_checker_pass.py` | 2,947 | 33 | 1 per ~89 LOC |
| `lang/driftc/stage2/string_arc.py` | 1,702 | 15 | 1 per ~113 LOC |

**Cross-file import expansion is the real killer.** When mypyc
compiles `type_checker.py` with its default (and required)
`--follow-imports=normal`, the local 374 errors balloon to **2,254**
because mypy then pulls in errors from every imported module
transitively. mypyc needs zero errors across the whole compile-graph
— there's no "compile one file in isolation" escape hatch when the
file has real imports of other project modules.

**Error mix (sample from `hir_to_mir.py`)**:

| Category | Count | Shape |
|---|---:|---|
| `arg-type` | 28 | `int | None` passed where `int` expected — Optional narrowing |
| `attr-defined` | 25 | Attribute access mypy can't statically prove exists |
| `assignment` | 18 | T → U widening / shadowing |
| `call-overload` | 8 | No overload matches — sometimes redesign-shaped |
| `return-value` | 7 | Return type mismatch |
| `name-defined` | 5 | Closure-scope / conditional-def issues |

Most are 1-line fixes (`assert x is not None`, `cast`, explicit
None-check, missing import). A minority (`call-overload`,
`valid-type`, `type-var`) likely need redesign.

**Estimated cleanup effort**: ~1-2 weeks of focused typing work per
hot file in isolation. Cross-file ripple makes the realistic estimate
**1-2 months for the top 3 files** (`type_checker.py` +
`hir_to_mir.py` + `llvm_codegen.py`), which together cover ~24% of
compile wall on bookkeeper.

## Expected payoff

Quantifying the upside before paying the cost:

- The hot files we'd realistically compile cover ~24% of compile
  wall (typecheck_funcs 9.6% + hir_to_mir 8.9% + codegen ~5%).
- Microbench suggests ~2x on the compiled hot loops.
- **Best-case net**: 24% × (1 − 1/2) = **~12% total wall reduction**.
- Upper bound if we cleaned up many more files: maybe **15-25%**.

That's not a step-function improvement. Bookkeeper at 36.8s would
land around 28-32s — useful, but not transformative, especially
weighed against the 1-2 months of typing refactoring.

## Productization risks

Listed for visibility; not investigated in the spike since the
verdict is "don't productize now."

- **PEX / scie native-extension packaging.** Current `bin/drift`
  bundles pure-Python via pex+scie. Native `.so` files need per-
  platform builds and the pex packaging story for compiled
  extensions is more involved (manylinux wheels, separate scie
  binaries per arch). Not insurmountable; new infra.
- **Platform wheel matrix.** At minimum Linux x86_64. Foundation
  release would need macOS (arm64 + x86_64) at least, and likely
  Linux arm64. Today's release is pure-Python: one PEX runs
  anywhere.
- **Rebuild-on-edit dev friction.** Every `.py` change to a
  compiled file means rebuilding the `.so`. Pre-commit, CI, and
  any in-tree `git pull` workflow needs a hook. Pure-Python edits
  iterate instantly.
- **Debuggability / stack traces.** Compiled `.so` frames are
  Python-level visible but less informative. `pdb` can't step into
  them Python-style. `python -c "..."` reproductions against
  compiled modules behave subtly differently from `.py`.
- **Cold subprocess startup still exists.** Every `driftc`
  invocation forks a fresh Python process — Python startup, import
  graph load, parser/Lark table rebuild dominate the first
  100-200ms regardless of whether the inner pipeline is mypyc-
  compiled. mypyc speeds up the inner work but not the cold-start
  floor.

## Recommendation

**Do not productize mypyc now.** The cost-to-payoff ratio doesn't
clear the bar with current data.

**Sequence cheaper wins first**:

1. **Profile-guided pure-Python optimization.** `__slots__` on the
   135 MIR node classes (currently dataclass-default `__dict__`),
   hoist invariants out of the per-function hot loops in
   `typecheck_funcs` / `hir_to_mir`, swap `list` for `set`/`dict`
   where lookups dominate, replace `getattr` with direct attribute
   access on hot paths. Realistic aggregate: **10-20% wall**
   reduction in **1-2 weeks**, fully reversible.
2. **Reduce subprocess cold-start.** Lark grammar `cache=True` (the
   parser-table rebuild burns 50-150ms per invocation). Pre-warm
   `~/.cache/drift/parser/` in the deploy bundle so users get a
   warm cache out of the box.
3. **PyO3 / Cython for one identified hotspot.** Pick the largest
   non-link CPU-bound phase after step 1's wins are measured.
   Likely `typecheck_funcs` (per-function check_function loop,
   clean input/output boundary). 1-3 months for one phase; ~5x
   ceiling on the compiled phase; opens phase-by-phase migration
   path without committing to a full rewrite.
4. **Revisit mypyc only if** the typing cleanup pays off as a
   side-effect of code-quality work, or if a future Python release
   substantially changes the cost-benefit (e.g. mypyc improves its
   strict-mode handling of common Optional patterns).

A **full C/Rust port of driftc** remains the *last* option, only if
the above sequence still leaves driftc uncompetitive on cold-start
+ inner-pipeline cost combined.

## Reproducing the spike

```bash
.venv/bin/pip install 'mypy[mypyc]'
# system: sudo apt install python3.13-dev  (one-time, for Python.h)

cat > /tmp/mypyc_spike/setup.py <<'PY'
from setuptools import setup
from mypyc.build import mypycify
import os, sys
DRIFT_ROOT = "/path/to/drift-lang"
sys.path.insert(0, DRIFT_ROOT)
os.chdir(DRIFT_ROOT)
setup(
    name="drift_mypyc_spike",
    ext_modules=mypycify([
        # Small, well-typed modules that compile cleanly:
        "lang/driftc/_events.py",
        "lang/driftc/env_flags.py",
        "lang/driftc/core/function_id.py",
    ]),
)
PY
cd /tmp/mypyc_spike && /path/to/.venv/bin/python setup.py build_ext --inplace
```

The compiled `.so` files land next to the `.py` source files (because
of the `os.chdir`) and Python imports them transparently. Delete them
to revert.

To check error counts on the hot files before attempting a wider
compile:

```bash
.venv/bin/mypy --no-incremental --follow-imports=silent \
    --ignore-missing-imports lang/driftc/type_checker.py
```

(Local error counts only. Mypyc's actual compile expands these via
import-following.)
