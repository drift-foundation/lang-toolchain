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
  - `lang/driftc/` — the compiler driver package (Drift → HIR → MIR → LLVM); invoked as `bin/driftc` or `python -m lang.driftc`.
  - `bin/drift` — manifest-driven build/deploy front end (packages, signing, deploy).
  - `just test` — the full repo gate (uniform pytest, LLVM/driver/codegen suites, ownership matrices, deploy tests).
  - `just lang-codegen-test` — the Drift-source e2e suite (`lang/tests/codegen/e2e/`, one directory per case with `main.drift` + `expected.json`).

## Quick Tour

### Hello Drift

```drift
module main;

import std.console as console;

pub fn main() nothrow -> Int {
    console.println("hello, drift");
    return 0;
}
```

### Structs, ownership, and methods

```drift
struct Point { x: Int, y: Int }

implement Point {
    pub fn move_by(self: &mut Point, dx: Int, dy: Int) nothrow -> Void {
        self.x += dx;
        self.y += dy;
    }
}

fn translate(p: &mut Point, dx: Int, dy: Int) nothrow -> Void {
    p.x += dx;
    p.y += dy;
}
```

### Collection literals with type inference

```drift
fn numbers() nothrow -> Array<Int> {
    val xs = [1, 2, 3];         // inferred Array<Int>
    var ys: Array<Int> = [4, 5, 6];
    ys[1] = 42;                 // requires `var`
    ys.extend(&xs);             // element-wise copy from a borrow
    return move ys;             // ownership transfer is explicit
}
```

### Concurrency at eye level

```drift
import std.core as core;
import std.concurrent as conc;
import std.console as console;

pub fn main() nothrow -> Int {
    var user_vt = conc.spawn_cb(|| => { return load_user(42); });
    var data_vt = conc.spawn_cb(|| => { return fetch_data(); });
    match user_vt.join() {
        core.Result::Ok(user) => { console.println(user); },
        core.Result::Err(_) => { return 1; },
    }
    match data_vt.join() {
        core.Result::Ok(data) => { render(data); },
        core.Result::Err(_) => { return 1; },
    }
    return 0;
}
```

## Getting Started

Set up the environment, build the runtime archives, then compile and run a program:

```bash
just venv && just deps-check   # create the venv, verify the machine is wired
just build                     # build the runtime archives driftc links against

bin/driftc --dev --stdlib-root stdlib hello.drift --entry main::main -o hello
./hello
```

`driftc` is the compiler for ad-hoc/single-program compiles; `drift` (also under
`bin/`) is the manifest-driven build/deploy front end. `just test` runs the full
repo gate. See [doc/toolchain-build-workflow.md](doc/toolchain-build-workflow.md)
for the packaging/signing workflow.

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
