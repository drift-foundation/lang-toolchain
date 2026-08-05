# Proposed implementation and verification plan

This is a reviewer-prepared execution plan. It is intentionally detailed to reduce rediscovery, but it is not authoritative. The implementer should record corrections, rejected hypotheses, and the actual chosen design in implementer-owned `PROGRESS.md` after the finding is activated.

## Gate 0 — refresh and establish the red baseline

1. Read the whole finding folder and re-check the current `AGENTS.md` and `doc/refactor_triggers.md`.
2. Re-inventory all Linux scheduler-to-fiber switch paths. Confirm that ordinary worker resume and direct reactor resume remain the complete set; search for both `swapcontext` and `drift_swapcontext` rather than relying on line numbers here.
3. Confirm how runtime C sources are assembled in normal, ASAN, and memcheck lanes and whether adding a new C translation unit would require build-list changes.
4. Add the smallest real-overflow subprocess regression before runtime changes. It should fail because the current process dies generically without the exact fixed diagnostic, not because compilation is unreliable.
5. Run it in debug and optimized configurations if frame behavior differs. Preserve the failing output in `PROGRESS.md`.

If a stable genuine overflow cannot be produced, stop and fix the test mechanism before changing runtime behavior. A synthetic `raise(SIGSEGV)` at a guessed address is not proof of the feature.

## Gate 1 — make guarded stacks a reliable invariant

Candidate patch shape, subject to revalidation:

1. Introduce explicit mapping/guard bounds or enough allocation metadata that allocation, handler classification, and cleanup share one representation.
2. Validate `stack_bytes`, page alignment, addition overflow, and minimum usable size.
3. Prefer mapping the full range inaccessible and enabling only usable pages.
4. Remove the Linux `malloc` fallback for executor fiber stacks. On `mmap`/`mprotect` failure, return a clean initialization/submission failure or fail runtime startup according to an explicitly chosen contract.
5. Keep raw-context and Valgrind-context stack setup on the same guarded allocation authority.
6. Add allocation/cleanup unit coverage where practical, including failure injection if the runtime already has a test-hook convention.

Acceptance at this gate:

- no Linux executor VT begins execution without a recorded inaccessible guard region;
- cleanup unmaps exactly the allocation it owns;
- malformed/overflowing stack sizes cannot wrap mapping arithmetic;
- no unrelated OS-thread stack is mistaken for an executor fiber stack.

## Gate 2 — add per-worker alternate stacks and process action

1. Define a per-worker alternate-stack ownership record containing the previous `stack_t`, the currently used mapping if runtime-owned, and whether restoration is required.
2. At worker entry, query the existing alternate stack.
3. Reuse an adequate existing enabled stack when that is demonstrably compatible; otherwise allocate and install a runtime-owned stack.
4. Install the process-wide `SIGSEGV` action once, saving the complete prior `struct sigaction`.
5. Use `SA_SIGINFO | SA_ONSTACK`; preserve any flags required by the selected chaining contract.
6. Tear down in safe order at worker exit: stop executing fibers, restore/disable the prior altstack, then unmap only runtime-owned memory.
7. Resolve executor-start failure propagation. Do not allow a partially initialized worker pool to masquerade as successful.

Potential implementation boundaries:

- A small new `fiber_fault_runtime.c/.h` can make the async-signal-safe surface auditable, but only if the build and internal API stay simple.
- A contained section of `thread_runtime.c` avoids a new internal interface but increases an already large file.

The implementer should choose based on actual build/link constraints rather than file-size preference.

## Gate 3 — publish active guard state at the switch authority

1. Add direct per-thread active-stack state readable without pthread APIs.
2. Store guard and usable-stack bounds before marking the record active.
3. Activate immediately before each scheduler-to-fiber switch.
4. Deactivate immediately after the scheduler regains control.
5. Do not derive activation from `drift_vt_tls_set`; the fiber entry clears that liveness TLS before its final switch while still using the fiber stack.
6. Ensure activation is correct for first entry, park/resume, yield, direct reactor resume, cancellation/unwind, completion, and Valgrind's `ucontext` path.
7. Consider debug assertions outside signal context that the current stack pointer lies within the published usable bounds at activation/return. Do not add signal-path formatting or unsafe checks.

The hot path should be only a handful of direct TLS stores and no allocation, locks, pthread TLS calls, or syscalls.

## Gate 4 — classify and terminate safely

1. Handler receives `SIGSEGV`, `siginfo_t *`, and the context through `SA_SIGINFO`.
2. Read only the direct active guard record.
3. Convert/compare `si_addr` using a representation whose behavior has been checked for the target C implementation.
4. Classify only the exact half-open guard interval.
5. On classification, write the fixed diagnostic to fd 2 using `write(2)` and a compile-time byte count.
6. Terminate using the selected and documented async-signal-safe contract.
7. On non-classification, preserve the saved action without printing the Drift label.

Before choosing the chaining implementation, enumerate and test at least `SIG_DFL`, an `SA_SIGINFO` custom handler, and the ASAN handler. Avoid recursion if the saved action happens to point back to Drift's handler.

## Gate 5 — subprocess regression matrix

All crash tests must run in subprocesses so they cannot kill pytest. Test the executable's return status/signal and stderr separately. The matrix should include:

### A. Genuine fiber overflow

- Configure a deliberately small but valid fiber stack.
- Run stable non-tail recursion with a real per-frame stack footprint.
- Assert the exact fixed Drift diagnostic appears once.
- Assert termination matches the chosen contract.
- Assert no Python/compiler traceback is involved.
- Exercise both raw context and, if practical, the Valgrind/ucontext selection boundary.

### B. Unrelated segmentation fault during an active fiber

- Fault at null or another address provably outside the active guard mapping.
- Assert the Drift overflow diagnostic is absent.
- Assert ordinary `SIGSEGV`, custom prior-handler behavior, or sanitizer behavior is retained.

This negative is mandatory: without it the feature can conceal arbitrary memory-safety bugs as stack exhaustion.

### C. Multiple workers

- Create an executor with a fixed pool greater than one, preferably four workers.
- Keep peer workers active with a barrier or deterministic workload before triggering overflow.
- Ensure overflow can occur on a carrier other than a privileged/first worker, or repeat enough times across subprocesses to prove per-worker initialization.
- Assert a single correct diagnostic and no deadlock/hang.

Because the first overflow terminates the process, this case needs a scheduling proof rather than attempting multiple overflows in one process. Avoid a test that passes merely because worker zero happened to own every task.

### D. ASAN compatibility

- Build/run the real-overflow case under the repository's ASAN lane and assert the fixed Drift diagnostic remains available.
- Run the unrelated-fault case under ASAN and assert no Drift overflow label; ASAN's ordinary report/termination must remain visible.
- Account only for already approved sanitizer warnings. Do not globally suppress `SIGSEGV` or stack-overflow reports to make the test pass.

### E. Prior-handler chaining

- Install a known custom `SIGSEGV` handler before Drift runtime initialization in a temporary C harness/helper.
- Trigger an unrelated fault from a fiber.
- Assert the prior handler receives control and Drift emits no overflow label.

### F. No-false-positive companion

- Keep or reference `test_std_json_deep_input_fiber_no_signal.py`, which proves very deep untrusted JSON is bounded without signal.
- Add a normal small-stack fiber workload with many yields to show activation/deactivation alone never emits diagnostics.

## Gate 6 — context-switch performance

Measure the production scheduling path, not only the raw assembly primitive.

Proposed benchmark protocol:

1. A fixed single-worker executor and two ready VTs repeatedly hand off using `std.concurrent.yield_now()`.
2. Warm up before timing.
3. Run enough iterations to dominate process startup and timer granularity.
4. Take multiple samples on the same otherwise-idle host/build; report median and p95 nanoseconds per completed handoff or per switch, defining the denominator precisely.
5. Capture pre-change and post-change results using the same compiler mode, CPU affinity/power conditions when available, iteration count, and binary.
6. Report absolute and percentage deltas. Establish an acceptable threshold from observed baseline variance; do not invent a brittle CI wall-clock limit before measuring noise.
7. Retain a reproducible benchmark tool or perf-marked test if it adds lasting value, but keep flaky timing assertions out of the ordinary parallel regression suite.

The expected steady-state addition is direct TLS bound publication/depublication around the two existing switches. Any syscall, pthread-specific lookup, allocation, or lock per switch should be treated as a design regression.

## Gate 7 — broader verification and release bookkeeping

Focused verification should include:

- new subprocess driver tests;
- executor/yield/park/unpark/direct-resume tests;
- cancellation and worker shutdown tests;
- liveness signal tests (`SIGUSR2`) and signalfd tests (`SIGINT`, `SIGTERM`, `SIGUSR1`);
- debug and optimized lanes;
- ASAN and memcheck/Valgrind lanes as applicable;
- the context-switch benchmark comparison.

Then run the normal full suite when the finding reaches that gate.

Bookkeeping:

- user-visible diagnostic/termination behavior requires a compiler minor bump unless folded into the current pending unreleased minor;
- keep the runtime ABI unchanged unless actual exported boundary shapes change;
- document the exact overflow diagnostic and termination behavior in history/release notes;
- do not edit the language specification for this runtime UX feature;
- do not leave references from permanent code/tests to this ephemeral finding folder.

## Review checklist

- [ ] Real overflow is red first and green after the runtime fix.
- [ ] Overflow classification is exact-address guard membership, not “fiber active.”
- [ ] Handler's complete transitive path is async-signal-safe.
- [ ] Handler runs on a per-worker alternate stack.
- [ ] No Linux executor fiber silently uses an unguarded fallback.
- [ ] Ordinary/default/custom/ASAN `SIGSEGV` behavior is preserved outside guards.
- [ ] All scheduler-to-fiber switch paths publish identical active bounds.
- [ ] Multi-worker coverage proves per-carrier initialization.
- [ ] ASAN positive and negative controls pass.
- [ ] Context-switch overhead is measured and reported.
- [ ] Version and ABI decisions match the actual final boundary.
- [ ] No spec edits or source-level overflow workarounds are introduced.
