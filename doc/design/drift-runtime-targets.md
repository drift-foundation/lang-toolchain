# Drift VT Runtime Target Support

This document records the current runtime target boundary for the Drift virtual
thread backend and explains why the current enforcement is temporary.

## Supported target

The custom VT runtime backend supports exactly one target:

- **x86_64 Linux**

This covers the custom context-switch assembly (`drift_context.S`), the
`epoll`-based reactor, worker-side polling, and fiber stack allocation via
`mmap` with guard pages.

No other architecture or OS is supported by the production VT path.

## How support is enforced today

`lang/language_runtime/__init__.py` gates runtime compilation with a
host-based check:

```python
def _check_supported_target() -> None:
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError(
            "Drift VT runtime requires x86_64 Linux "
            f"(current: {sys.platform}/{platform.machine()})"
        )
```

This runs before any runtime source is compiled. Unsupported hosts fail early
with a clear error message. The check uses `sys.platform` and
`platform.machine()` — both describe the **host** machine, not a compilation
target.

## What this means in practice

- On x86_64 Linux the full VT backend is available: custom asm context switch,
  `epoll` reactor, worker-side polling, fiber stacks with guard pages.
- On any other host the runtime build fails at the gating check. There is no
  fallback to `ucontext`, `kqueue`, or any other platform primitive.
- Under Valgrind (`RUNNING_ON_VALGRIND` detected at executor creation) the
  runtime falls back to glibc `getcontext`/`makecontext`/`swapcontext`. This
  is a **tooling compatibility path** for memcheck, not a portability layer.
  It is available only on x86_64 Linux where glibc `ucontext` exists.

## Why this is incomplete

The current gating is **host-based**, not **target-based**. `sys.platform`
reports the machine running the compiler, not the machine that will run the
compiled binary. This distinction does not matter today because Drift is only
compiled natively, but it is not sufficient for:

- **Cross-compilation**: compiling on x86_64 Linux for aarch64 Linux (or any
  other target) would pass the host check but produce a binary that links
  x86_64-only assembly and Linux-only syscall wrappers.
- **Multi-target CI**: a build matrix that targets multiple architectures from
  a single x86_64 host would incorrectly pass the gate for all targets.

The gating also does not separate backend selection (which context-switch
implementation, which reactor) from target validation (is the target
supportable at all). These are currently conflated because there is only one
backend.

## Future requirement

Before adding a second supported target (ARM Linux is the most likely next
candidate), the runtime build must move to **target-triple-based selection**:

1. The compiler receives or infers a target triple (e.g.
   `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`).
2. Runtime source selection, context-switch backend, and reactor implementation
   are chosen based on the target triple, not the host.
3. The host-based gate in `__init__.py` is replaced (or supplemented) by a
   target-based gate that rejects unsupported triples at compile time.
4. `drift_context.S` is extended with an aarch64 implementation (or a
   separate `.S` file is selected per target).

Until this work is done, the runtime is x86_64 Linux only.

## Files

| File | Role |
|------|------|
| `lang/language_runtime/__init__.py` | Host-based gate, runtime source list, archive build |
| `lang/language_runtime/posix/drift_context.S` | x86_64 custom context switch |
| `lang/language_runtime/posix/drift_context.h` | Context switch header |
| `lang/language_runtime/posix/thread_runtime.c` | VT scheduler, reactor, worker poll |
