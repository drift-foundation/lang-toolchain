# Fiber stack-overflow diagnostics

## Status and scope

This is a queued, top-level runtime/UX finding. It is independent of the currently active compiler finding and must not interrupt serial work already in flight.

The material in this folder is reviewer research, not an implementation specification. The implementer must revalidate every code-path claim against the then-current tree and may replace the proposed design when contrary evidence or a safer design emerges. Repository safety contracts and the acceptance criteria below remain binding; hypotheses and implementation shapes do not.

No language-specification change is proposed. This is a user-visible POSIX runtime behavior change and therefore requires the repository's normal compiler-version decision when implemented.

## Problem

Drift fibers already receive a protected page below their usable stack on the Linux executor path. A genuine fiber-stack overflow therefore normally becomes `SIGSEGV`, but the runtime does not distinguish the guard-page fault from an unrelated invalid memory access. In production, operators may see only a generic segmentation fault and must inspect a core file or debugger state merely to discover that a task recursed too deeply or exhausted its configured fiber stack.

This is both an operability problem and a security problem: attacker-controlled recursion or extreme call depth can be a denial-of-service vector, while the emitted failure currently gives no immediate evidence that stack exhaustion was the cause.

The desired contract is deliberately narrow:

1. A fault whose address lies in the guard region of the fiber currently running on the faulting OS thread is classified as a fiber stack overflow.
2. The process emits one fixed, recognizable, async-signal-safe diagnostic to standard error and terminates promptly.
3. An ordinary `SIGSEGV` outside that exact active guard region is not relabeled. The pre-existing/default signal behavior must be preserved or chained.
4. Detection works on every executor worker, including under ASAN, without materially regressing the context-switch hot path.

Suggested fixed text, subject to final review:

```text
drift: fatal: fiber stack overflow
```

The diagnostic must not include dynamically formatted addresses, task names, stack sizes, or backtraces. Those would make the signal path less reliable and are not needed to answer the immediate production question.

## Required outcome

- Fiber stacks have a real inaccessible guard region; a fiber must not silently run on an unguarded `malloc` fallback when guard setup fails.
- The active fiber's guard bounds, and preferably its usable stack bounds for invariant checking, are available in direct thread-local runtime state that a `SIGSEGV` handler can read without locks, allocation, pthread APIs, or other non-async-signal-safe calls.
- Every worker that can execute a fiber has a usable alternate signal stack installed with `sigaltstack` before it enters user fiber code.
- The process `SIGSEGV` action uses `SA_SIGINFO | SA_ONSTACK` and retains the prior disposition for unrelated faults.
- Only `si_addr` inside the active guard interval is classified as a Drift fiber-stack overflow.
- The overflow path uses a fixed buffer and `write(2)`, then terminates through an async-signal-safe path.
- Unrelated segmentation faults retain ordinary/default, custom-handler, or sanitizer behavior rather than receiving the Drift overflow label.
- Subprocess regressions cover a real stack overflow, an unrelated segmentation fault, multiple workers, and ASAN compatibility.
- The context-switch cost of maintaining the active guard descriptor is measured before sign-off.

## Why exact guard classification matters

The runtime must not infer “overflow” from `SIGSEGV` merely because a fiber was active. Null dereferences, use-after-free faults, wild pointers, code-generation bugs, and native-library faults can all occur while a fiber owns the carrier. Relabeling these as stack overflow would actively hide the root cause.

The safe classification predicate is intentionally conservative:

```text
active_fiber_guard && guard_low <= si_addr < guard_high
```

If that predicate is false or cannot be evaluated confidently, the handler must preserve the ordinary `SIGSEGV` path. A large stack frame can theoretically jump past a one-page guard when stack probing is absent; that limitation must not be papered over by classifying nearby addresses heuristically. Guard width or stack-clash probing can be investigated separately if a reproducible jump-over defect appears.

## Classification

This is a runtime defect/UX gap and should be handled under the repository's `LANGUAGE_BUG` process when implementation begins:

- add a minimal failing subprocess regression first;
- confirm current behavior lacks the fixed overflow diagnostic;
- fix the POSIX runtime root cause;
- prove the regression and negative controls;
- avoid source/stdlib workarounds that merely increase stacks or rewrite recursion.

The scan of `doc/refactor_triggers.md` performed while filing this finding found no registered trigger matching POSIX signal handling, fiber guards, or alternate signal stacks. That result must be repeated when work begins because the registry and runtime may change.

## Non-goals

- Recovering from stack overflow or unwinding the overflowing fiber.
- Turning overflow into a catchable Drift error.
- Producing a backtrace from the signal handler.
- Diagnosing the root cause of arbitrary `SIGSEGV` faults.
- Relabeling faults merely because their address is near a fiber stack.
- Adding test-only public runtime exports.
- Changing Drift language semantics.

## Open design decisions

These require implementation-time proof rather than assumption:

1. **Termination after a classified overflow.** `_exit(128 + SIGSEGV)` gives deterministic, signal-safe behavior and preserves the diagnostic, but a restored/default re-raise preserves signal termination/core semantics. The chosen contract must be explicit and pinned.
2. **Chaining a previous custom or ASAN handler.** Directly invoking the saved `sa_sigaction` may not reproduce kernel signal-mask/reset semantics; restoring and re-delivering introduces process-global races. The implementation should choose the smallest POSIX-correct strategy supported by the runtime and add a prior-handler/ASAN regression.
3. **Existing alternate stacks.** ASAN or an embedding application may already have installed one. Reusing an adequate existing stack may be safer than replacing it; replacing it requires exact restoration and ownership bookkeeping. This must be tested, not inferred.
4. **Worker initialization failure.** `sigaltstack`, `mmap`, `mprotect`, and current unchecked `pthread_create` calls can fail. Reliable diagnostics are incompatible with silently starting a worker that lacks the required guard or alternate stack. A fail-closed startup contract may require a worker-start barrier or another initialization acknowledgment.
5. **Valgrind path.** Valgrind uses `ucontext` instead of raw context assembly, but it still uses executor-owned fiber stacks. Confirm whether the same guard and signal-handler contract should operate under Valgrind or be explicitly gated.

