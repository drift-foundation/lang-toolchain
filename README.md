# Drift

<img src="assets/drift.svg" alt="Drift" width="240" align="right" />

Drift is a systems programming language focused on deterministic resource management, explicit ownership, and scalable concurrency. It combines C++-style RAII with Rust-like borrowing rules, while keeping the syntax compact and readable.

- **Safety-first design** – deterministic ownership, explicit moves, and no raw pointers in userland.
- **Escape hatches on demand** – you opt into `lang.abi` / `@unsafe` only when you really need low-level control.
- **Zero-cost abstractions** – traits, interfaces, and concurrency compile down to what you’d hand-write.
- **Virtual-thread concurrency** – synchronous-looking code scales via lightweight threads and structured scopes.
- **Interop without foot-guns** – precise binary layouts and opaque ABI handles keep FFI predictable.
- **Signed modules** – compiled modules are cryptographically signed so imports can be verified everywhere.

📖 **Full specification:** [doc/design/drift-lang-spec.md](doc/design/drift-lang-spec.md)
📜 **Formal grammar:** [doc/design/drift-lang-grammar.md](doc/design/drift-lang-grammar.md)

## Release-1 objectives
- Modules/imports
- Structs + enums + match
- Generics (Vec, Map, Optional, Result)
- Error propagation (Result + exceptions)
- Deterministic destruction (implicit nothrow destructors)
- Green threads + reactor (epoll-based scheduler)
- Nonblocking IO primitives (files, pipes, sockets)
- Process spawning + capture (POSIX, pidfd-based)
- Strings + bytes + formatting
- File/path IO + argv/env
- Hash map/set + sorting
- RAII destruction
- FFI to C

## References

- Error handling comparison for Rustaceans: [doc/articles/drift_vs_rust_error_handling.md](doc/articles/drift_vs_rust_error_handling.md)
- Effective Drift: [doc/effective-drift.md](doc/effective-drift.md)
- Build/package quickstart workflow: [doc/toolchain-build-workflow.md](doc/toolchain-build-workflow.md)
- Tooling/build/package ecosystem: [doc/design/drift-tooling-and-packages.md](doc/design/drift-tooling-and-packages.md) — compiler and tooling responsibilities, offline builds, package distribution, and trust model.
- DMIR/SSA design: [doc/articles/design-first-afm-then-ssa.md](doc/articles/design-first-afm-then-ssa.md)
- DMIR specification: [doc/design/dmir-spec.md](doc/design/dmir-spec.md)
- Tooling, build system, and packages: [doc/design/drift-tooling-and-packages.md](doc/design/drift-tooling-and-packages.md) — module/package inputs, build targets, repositories, and deterministic offline builds.
- Borrowing/reference model revision: [doc/design/drift_borrowing_and_reference_model_revision.md](doc/design/drift_borrowing_and_reference_model_revision.md)
- Drift concurrency: [doc/design/drift-concurrency.md](doc/design/drift-concurrency.md)
- Runtime liveness interrogator (diagnosing a stuck process via `kill -USR2`): [doc/liveness.md](doc/liveness.md)
- Virtual threads/concurrency spec change: [doc/design/spec-change-requests/virtual_threads_concurrency_spec.md](doc/design/spec-change-requests/virtual_threads_concurrency_spec.md) — proposal for lightweight threads, schedulers, and structured scopes.
- Module merge/artifact generation: [doc/design/spec-change-requests/module_merge_and_artifact_generation.md](doc/design/spec-change-requests/module_merge_and_artifact_generation.md) — design for merging multi-file modules, enforcing duplicate rules, and emitting executables vs signed modules.
- Iteration model: [doc/design/drift-loops-and-iterators.md](doc/design/drift-loops-and-iterators.md)
- String runtime plan: [doc/design/drift-string-impl.md](doc/design/drift-string-impl.md)
- Tuple destructuring notes: [doc/design/drift-tuple-destructuring.md](doc/design/drift-tuple-destructuring.md)
- Driver/runtime notes: [doc/articles/driver-notes.md](doc/articles/driver-notes.md)
- Compiler architecture overview: [doc/articles/drift-compiler-architecture.md](doc/articles/drift-compiler-architecture.md)
- Development history: [doc/history.md](doc/history.md)
- Project TODO/roadmap: [TODO.md](TODO.md)
- Toolchain:
  - `lang/driftc.py` — Drift → MIR/SSA → LLVM driver (emits LLVM IR/object via llvmlite/LLVM).
  - `just test-e2e` — runs e2e programs through the SSA backend and compares outputs.
  - `just mir-codegen` — lowers simple MIR samples to an object, links with clang, and runs the binary.
  - `lang/codegen/codegen_runner.py` — next compiler e2e runner using `lang.driftc` (`--json` for compile errors, `-o` for run-mode) against cases in `tests/lang-e2e` by default (configurable with `--root`).

## Quick Tour

### Hello Drift

```drift
fn main() -> Int {
    println("hello, drift")
    return 0
}
```

### Structs, ownership, and methods

```drift
struct Point { x: Int64, y: Int64 }

implement Point {
    fn move_by(self: &mut Point, dx: Int, dy: Int) -> Void {
        self.x += dx
        self.y += dy
    }
}

fn translate(p: &mut Point, dx: Int, dy: Int) -> Void {
    p.x += dx
    p.y += dy
}
```

### Collection literals with type inference

```drift
fn numbers() -> Array<Int> {
    val xs = [1, 2, 3]          // inferred Array<Int>
    var ys: Array<Int> = [4, 5, 6]
    ys[1] = 42                 // requires `var`
    return xs + ys
}
```

### Concurrency at eye level

```drift
import std.concurrent as conc

fn main() -> Void {
    conc.scope(Fn(scope: conc.Scope) -> Void {
        val user = scope.spawn(Fn() -> User { load_user(42) })
        val data = scope.spawn(Fn() -> Data { fetch_data() })
        render(user.join(), data.join())
    })
}
```

## Getting Started

Use the MIR+LLVM prototype to lower and run a sample:

```bash
just mir-codegen
```

## Prerequisites

Build/test requirements (Linux):

- Python 3.13+
- `python3-venv`
- LLVM/Clang (`clang` on PATH; clang-20 recommended)
- `just` (task runner)
- `pkg-config`
- `binutils-gold` (`ld.gold`)
- `libdw-dev` (elfutils), `libunwind-dev`, `libelf-dev`
- `ripgrep` (`rg`) for stdlib package publish/build recipes
- `pex` (Python package; deploy only) — install into the project venv: `./.venv/bin/pip install pex`

After installing those, create the venv and run `just deps-check` to verify the machine is fully wired for the current runtime/test flow.

See the full language specification in [doc/design/drift-lang-spec.md](doc/design/drift-lang-spec.md) for semantics and examples. The full formal grammar lives in [doc/design/drift-lang-grammar.md](doc/design/drift-lang-grammar.md).
