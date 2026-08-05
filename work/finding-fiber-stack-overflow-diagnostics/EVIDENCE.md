# Evidence and runtime map

Epistemic labels in this document are intentional:

- **Observed**: directly visible or reproduced on the current tree while filing.
- **Confirmed**: a current code-path fact established by static inspection.
- **Inferred**: the best current explanation, still requiring implementation-time validation.
- **Proposed**: a candidate design or test shape, not an instruction to follow blindly.
- **Open**: unresolved and important to decide before sign-off.

## Current stack allocation

**Confirmed:** `DriftVt` in `lang/language_runtime/posix/thread_runtime.c` stores:

- `void *stack` — the usable stack base;
- `size_t stack_size` — usable bytes;
- `int stack_is_mmap` — whether cleanup must derive and unmap the guarded mapping;
- Linux context state for the raw and Valgrind paths.

**Confirmed:** first execution of an executor VT allocates `stack_size + page_size`, maps it read/write, marks the lowest page `PROT_NONE`, and sets the usable base to `map + page_size`. The existing comment says this is intended to turn overflow into `SIGSEGV` instead of corrupting adjacent heap data.

**Confirmed:** the current path silently falls back to `malloc(stack_size)` if either `mmap` or `mprotect` fails. That fallback has no inaccessible guard region and therefore cannot support reliable address-based classification.

**Inferred:** the diagnostic contract should make guard creation a prerequisite for starting a Linux executor fiber. A likely robust allocation shape is:

1. validate and page-align the usable size with checked arithmetic;
2. `mmap` the full mapping as `PROT_NONE`;
3. `mprotect` only the usable pages to `PROT_READ | PROT_WRITE`;
4. record the mapping/guard bounds explicitly rather than re-deriving them in multiple consumers;
5. fail task or executor initialization cleanly if any step fails.

This avoids a transient fully writable mapping and makes the guard interval an explicit runtime fact. It may require changing cleanup fields; revalidate the ABI rule before changing any exported layout. `DriftVt` currently appears internal.

**Open:** the present allocator accepts arbitrary `stack_bytes`, while context setup and mapping arithmetic assume a usable byte range above one page. Minimum size, alignment, integer-overflow handling, and invalid user configuration need explicit outcomes.

## Scheduler-to-fiber switch points

**Confirmed:** there are two Linux scheduler paths that resume executor fibers:

1. The ordinary worker path in `drift_exec_worker`: set VT TLS, call `swapcontext` or `drift_swapcontext`, then clear VT TLS when the scheduler regains control.
2. The single-worker reactor's direct-resume path: the same set/switch/clear sequence around an immediately resumed VT.

**Proposed:** activate a signal-safe guard descriptor immediately before each scheduler-to-fiber switch and deactivate it immediately after control returns. Centralize that sequence in a tiny helper if doing so does not obscure the raw/ucontext variants. Both paths must use the same authority.

**Confirmed:** fiber-side park/yield functions switch back to the scheduler without clearing the existing VT TLS first; the scheduler clears it after the switch returns. This naturally keeps a separately activated guard descriptor live for the entire period in which the fiber stack is executing.

**Confirmed:** `drift_vt_fiber_entry` currently calls `drift_vt_tls_set(NULL)` before its final switch back to the scheduler.

**Inferred:** signal guard activation must not be coupled mechanically to `drift_vt_tls_set`. Otherwise the final fiber-entry cleanup can clear the guard while code is still executing on the fiber stack, leaving a small but real unclassified-overflow window. Activating/deactivating at the carrier's two switch chokepoints avoids that problem and separates crash classification from liveness ownership bookkeeping.

## Existing TLS is unsuitable inside the handler

**Confirmed:** current VT TLS is a `pthread_key_t` initialized by `pthread_once`, read through `pthread_getspecific`, and written through `pthread_setspecific`. The setter also performs liveness bookkeeping and atomic state updates.

**Confirmed:** pthread-specific lookup and the surrounding liveness machinery are not part of the async-signal-safe surface required for a fatal `SIGSEGV` handler.

**Proposed:** add direct, internal per-thread scalar state dedicated to crash classification, for example:

```c
typedef struct {
	uintptr_t guard_low;
	uintptr_t guard_high;
	uintptr_t stack_low;
	uintptr_t stack_high;
	volatile sig_atomic_t active;
} DriftActiveFiberStack;

static __thread DriftActiveFiberStack drift_active_fiber_stack;
```

The exact representation requires C/platform validation. Bounds should be written before publishing `active = 1`; deactivate first on return. Avoid pthread calls, allocation, locks, logging helpers, and non-lock-free atomics in the handler. If C atomics are used, prove the relevant types are lock-free on every supported target rather than assuming their signal-handler safety.

**Open:** compiler reordering, `volatile sig_atomic_t` scope, and portability of `__thread` versus `_Thread_local` must be evaluated against supported C compilers and platforms. This finding is POSIX/Linux-focused because the guarded fiber implementation is under `#ifdef __linux__`.

## Signal infrastructure already present

**Confirmed:** the POSIX runtime currently routes `SIGINT`, `SIGTERM`, and `SIGUSR1` through `signalfd`, and uses a blocked `SIGUSR2` plus a `sigwait` thread for liveness reports. It does not install a `SIGSEGV` handler.

**Confirmed:** `lang/language_runtime/posix/liveness_runtime.c` includes formatted and structured reporting paths. Despite some raw `write(2)` use, it is not an appropriate handler to reuse wholesale for fatal synchronous signals.

**Proposed:** keep the overflow crash path in a small, auditable POSIX runtime unit or a tightly isolated section of `thread_runtime.c`. The handler's transitive call graph should be short enough to inspect manually:

- read saved `errno` if restoration is desired;
- read direct TLS scalars;
- compare `si_addr` as an integer against the active guard half-open interval;
- write one compile-time literal with `write(STDERR_FILENO, ...)` (retry only on `EINTR` if desired);
- terminate by the selected signal-safe contract;
- otherwise chain/preserve the prior disposition.

Forbidden handler behavior should include `malloc/free`, mutexes, condition variables, stdio, `printf`/`dprintf`, pthread TLS, symbolization, stack walking, and dynamically formatted addresses.

## Alternate signal stacks

**Confirmed:** alternate signal stacks are per OS thread. Installing one on the process-creating thread does not equip executor workers.

**Confirmed:** all executor workers enter through `drift_exec_worker`, and worker threads are created in `drift_exec_create_internal`.

**Proposed:** set up each worker's alternate stack at the start of `drift_exec_worker`, before it can execute a VT, and restore/disable it at worker exit. Install the process-wide `sigaction` once and save the prior action.

**Open:** if `sigaltstack(NULL, &old)` reports an already enabled, sufficiently sized alternate stack, especially under ASAN, the runtime likely should use it without claiming ownership. If it installs its own mapping, it must remember that ownership and restore the previous state before unmapping. Minimum sizing should account for `MINSIGSTKSZ`, `SIGSTKSZ`, sanitizer inflation, and supported libc/kernel behavior.

**Observed:** executor startup currently loops over `pthread_create` without checking return values or waiting for per-worker initialization acknowledgment.

**Inferred:** a fail-closed alternate-stack requirement may need a startup handshake so `drift_exec_create_internal` cannot report success while some carriers cannot diagnose overflow. A process-fatal startup failure is another possible contract, but silently continuing is inconsistent with the reliability goal. The implementer should scope this carefully rather than bolting an unchecked `sigaltstack` call onto worker entry.

## Ordinary SIGSEGV preservation

**Required:** a fault outside the active guard interval must never receive the Drift stack-overflow label.

**Open:** exact prior-action chaining is subtle and should be treated as a correctness problem, not boilerplate:

- `SIG_DFL`: restore default and re-deliver to the current thread, or use another method that retains genuine `SIGSEGV` termination semantics.
- `SIG_IGN`: determine whether preserving ignore is appropriate for synchronous `SIGSEGV` under the supported platform; POSIX behavior is constrained and implementations may differ.
- `SA_SIGINFO` custom action: preserve the handler's expected `(signal, siginfo, ucontext)` inputs and relevant mask/reset/nodefer semantics.
- classic `sa_handler`: preserve the one-argument action.
- ASAN: it commonly owns a custom `SIGSEGV` handler and may own an alternate stack. The Drift handler must not swallow unrelated ASAN reports.

**Proposed:** add a purpose-built subprocess/C-harness pin that installs a custom handler before Drift runtime initialization and proves an unrelated fault reaches it without the overflow label. This is stronger than relying solely on whatever ASAN happens to print on one host.

## Existing tests and configuration surfaces

**Confirmed:** `std.concurrent.ExecutorPolicyBuilder.stack_bytes` exposes the fiber stack size. The standard default is 262144 bytes (256 KiB), and the C runtime also defaults non-positive `stack_bytes` to 262144.

**Confirmed:** `lang/tests/driver/test_std_json_deep_input_fiber_no_signal.py` is an existing security regression that runs deep input on a default 256 KiB VT and proves the iterative JSON parser returns a bounded error rather than reaching the guard. It is a valuable no-false-positive companion but is not an overflow-diagnostic test.

**Confirmed:** `std.concurrent.yield_now()` provides a cheap cooperative scheduling point and is suitable for a production-path context-switch benchmark.

## Reliable test-driver concerns

**Inferred:** a Drift recursion repro alone may be optimized into a loop or otherwise vary with debug/release frame layout. A robust test should use a no-inline native helper or an equivalent source shape with:

- a volatile stack pad per frame;
- a non-tail recursive dependency after the recursive call;
- a call originating from a deliberately small Drift executor fiber;
- enough depth to cross the guard quickly without consuming test-runner time.

Likewise, an unrelated fault can be generated by a tiny linked C helper performing a volatile invalid access. This keeps the negative control structurally separate from the compiler's ability to express unsafe memory access. Avoid permanent test-only public runtime exports.

**Open:** determine the repo's cleanest precedent for compiling a temporary C helper into a driver-test executable. Prefer a subprocess test that builds all artifacts under `tmp_path` and leaves no runtime symbols or repository binaries behind.

## Refactor-trigger scan

**Observed:** `doc/refactor_triggers.md` was scanned while this finding was created. No entry mentions fiber stacks, guard pages, POSIX crash signals, alternate signal stacks, or runtime crash reporting. No existing larger refactor is triggered by the present shape.

This is a point-in-time result, not a waiver. Repeat the scan when the LANGUAGE_BUG is started.

## Version and ABI assessment

**Confirmed repository rule:** a new production diagnostic and termination behavior is user-visible, so it requires a compiler SemVer minor bump unless folded into an already pending, unreleased minor train.

**Inferred:** adding internal guard bounds, TLS, handlers, or worker initialization state does not by itself alter the exported compiler/runtime ABI. ABI 22 should remain unless implementation changes an exported helper signature, externally consumed layout, calling convention, or another compiler/runtime boundary. Reassess after the patch shape is known.

