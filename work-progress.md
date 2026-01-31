## Work progress

## Trait import for method resolution (use trait)

- Plan:
  - Add regression driver test: `use trait` enables method resolution for trait methods (and missing `use trait` fails).
  - Parser: accept `use trait <module.path.TraitName>;` as a distinct import form.
  - Resolver: register trait in scope for method resolution when `use trait` is present.
  - Update diagnostics to mention missing `use trait` when a trait method exists but isn’t in resolution scope.
  - Add a negative test for invalid `use trait` target (non-trait).
### Interfaces (dynamic dispatch)
- Fixed iface e2e failures by preferring declared type in HLet lowering (enables iface coercion and avoids CallIndirect on iface values).
- MIR iface init validator now treats iface-producing instructions as initialized (ConstructIface*, CallIface/Call*).
- Method-call lowering now consults typed expr types when receiver inference is missing (keeps iface calls on CallIface path).
- LLVM size/align modeling now treats TypeKind.INTERFACE as full iface layout (prevents under-allocation for env structs holding interface values).
- Interface inline flag is now a bitfield (bit0 inline, bit1 owns heap); runtime uses bit0 and iface drop frees only when owns-heap is set.

### Compiler infra (invariants plan)
- Added explicit plan to enforce MIR invariants at a single boundary: no unresolved types, no missing call metadata, and no by-value use of non-Copy locals without MoveOut.
- Next steps: add MIR validator for by-value non-Copy call args and centralize interface layout rules to avoid runtime/layout drift.
- Implemented MIR validator enforcing MoveOut for non-Copy by-value call args; added driver test `test_mir_invariants.py` to assert MoveOut is used when calling `take(String)`.
- Moved MIR validators into `lang2/driftc/mir_validate.py` and updated stage2 tests to import `validate_mir_*` helpers from the new module.
- Stage2 call lowering now uses `_lower_call_arg` for direct calls and method receivers/args to enforce MoveOut for non-Copy by-value parameters.

### Concurrency (FutureGroup)
- Resolved `FutureGroup.join_all` codegen failure by substituting impl type args in MIR lowering (stage2 now resolves declared types using impl target type args).
- Array literal lowering now falls back to impl-type substitution when element type is forward-nominal.

### MIR invariants / coverage
- Added MIR validation for unresolved layout types (TypeVar/ForwardNominal/Unknown) across array/rawbuffer/ptr/struct/variant/iface/typed ops.
- Added e2e `generic_impl_array_literal` to exercise generic impl + array literal lowering with concrete instantiations.
- Added driver fuzz-style test `test_generic_impl_array_literal_fuzz_fixed_seed` to catch forward-nominal leakage in generic impls.

### Concurrency (callback inference)
- Fixed `std.core.callbackN` inference for lambdas without expected type by allowing return type inference (prevents forward-nominal fallback in spawn tests).
- `spawn_cb` call paths now move non-Copy locals when passed by value (prevents callback env double-free/use-after-free).
- Updated eventfd reactor e2e to capture fd by copy (move capture zeroed fd, preventing wakeup).
- Added a timeout option to the codegen e2e runner and ensured `concurrent_reactor_timerfd_unpark` registers a deadline before parking (prevents hangs).

### Concurrency (test sweep)
- Removed explicit local type annotations in concurrency/future e2e tests (kept behavior by improving match inference in checker).

### MIR invariants
- Fixed PtrWrite validation to avoid requiring ptr_ty (PtrWrite only carries elem_ty); unblocked cell_counter_fn0 e2e.
- Wrapping-u64 validator now resolves existing scalar TypeIds (avoids creating fresh Uint64/Int/etc. ids); fixes false failures in wrapping_u64_ops/hash_wrap_overflow.

### Containers (e2e cleanup)
- Removed explicit local type annotations in treemap/hashmap/treeset e2e cases (match results + literals), kept interface coercion types intact.

### E2E inference sweep (Int/Bool)
- Removed explicit `: Int` / `: Bool` local annotations across many e2e cases (interfaces/fnptr/iter/try/etc.) to push inference; kept coercion-critical types.

### E2E inference sweep (strings/iter/methods)
- Removed explicit `Optional<&Int>` / `String` / `Array<String>` / local `Point` annotations where inference is clear (iterator, string, and method call e2e cases).

### Concurrency (join_timeout/cancel)
- Ensured join_timeout checks cancellation before zero-timeout to return Cancelled when appropriate.
- Added e2e `concurrent_future_join_timeout_nonzero_ok` to cover non-zero timeout success for Future.
- Runtime cancel/drop now drops callback env without executing user code when not started.

### Call resolution (module alias)
- Added regression test to ensure `module_alias.fn()` resolves as a free call (not a method call on a value).

### Match patterns
- Added qualified constructor patterns in match arms (e.g., `mod.Type::Ctor()`), with module-alias resolution.
- Added e2e `match_qualified_ctor_pattern` to lock qualified ctor matching.

### Match/try value position
- Match/try arms now yield values only in value position; statement matches no longer treat trailing nested match/try as arm results.
- Added e2e `match_nested_expr_value` to lock nested match as value in value position and fixed `match_stmt_nested_match_last_stmt` regression.

### Entrypoint diagnostics
- Entry validation messages now include the entry name (e.g., `entrypoint main must return Int`), restoring expected diagnostics for entrypoint e2e tests.
- Fixed OS entry wrapper to call the correct symbol for main-module entries (avoid `main::drift_main` undefined reference); added regression `test_entry_wrapper_symbol`.

### Call resolver refactor (CallIntent)
- Introduced a minimal `CallIntent` and propagated expected arg types for method calls after resolution (first step toward explicit expected-type plumbing).
- Extended expected-type propagation to free/UFCS calls in `resolve_call_expr` and added driver regression `test_expected_type_propagation_method_arg.py`.
- Added deferred-arg typing for nested calls: initial arg calls may return Unknown without diagnostic, then retyped with expected parameter types after candidate selection.
- Inference now ignores incompatible expected-return shapes (but preserves nominal base matches and bare typevars), fixing nested expected-return inference (e.g., spawn_future inside add).
- UFCS trait calls on concrete receivers now resolve to direct impl targets (avoids trait call targets in typed mode).
- Deferred local lambda inference from call sites: untyped lambda bindings are typed on first call with arg-driven param types, inferred return type, and inferred can-throw (used to allow `val fp = |x| => x + 1; fp(3)` without annotation).

### Traits (enforce_fn_requires)
- Fixed lambda-based generic inference for Fn0 bounds in trait enforcement (binds type params from TypeExpr args like `Fn0<T>`).
- Fixed UFCS trait resolution for direct impls: resolve candidates by trait + receiver base (non-struct types included), enforce visibility and `require` predicates, and improve requirement diagnostics to include the expected trait label.
- UFCS trait impl visibility now treats pub as globally visible (private only within defining module) to allow generic stdlib calls to resolve user impls.

### Concurrency (Void result)
- Added MIR/LLVM support for Void values so `VirtualThread<Void>` can store and return Void safely (ConstVoid + zero-value handling).

### Concurrency (executor plumbing)
- Added `exec_create` runtime hook and `build_executor` API; default executor is lazily created with single-thread policy; `Executor`/`ExecutorPolicy` are `Copy`.
- Added `std.io`/`std.net` boundary helpers (internal waits via `std.concurrent.block_on_io`).
- Added Phase‑3 correctness e2e tests for spawn/join ordering, join‑twice error path, sleep timing, and IO deadline timeout.
- Added Phase‑3 correctness e2e tests for vt_current behavior, IO readiness before deadline, repeated IO waits, many short tasks, and non‑VT park.
- Added Phase‑3 correctness e2e test for join_timeout after completion.
- Added Phase‑3 correctness e2e test for cancel then join_timeout(0).
- Added Phase‑3 correctness e2e tests for executor queue limit and default executor override.

### Phase‑3 stdlib IO/net (planned)
- std.io surface: File/OpenOptions/IoError + open/read/write/close (blocking; VT‑aware).
- Added trait-based `or_throw` for Result (throws `std.err:ResultError`); current payload is a placeholder DiagnosticValue::Int(0) until generic error payloads can be threaded (TODO).
- std.net surface: TcpListener/TcpStream/SocketAddr/NetError + listen/accept/connect + read/write + block_on_*.
- Tests: std_io roundtrip + would‑block + timeout; std_net listen/accept/connect + roundtrip + timeout.

### Phase‑3 stdlib IO/net (in progress)
- std.io: added OpenOptions, WouldBlock error, and Result-returning block_on_*; updated std_io e2e cases and removed fd exposure.
- std.net: replaced fd-based test constructors with test-only helpers and updated std_net e2e block_on tests.
- std.net read/write roundtrip test: use Byte literal instead of string_byte_at to avoid borrow-from-rvalue (string_byte_at requires &String place).

### Result.on_error (throwing lambdas)
- Added FnThrow0/1/2 traits and Result.on_error in std.core; on_error uses FnThrow1<E, T> and returns Ok(v) or calls the throwing handler on Err.
- Added e2e: result_on_error_throw (throws) + result_on_error_recover (returns), and driver regression for throwing lambda rejected for Fn1 bounds.
- Call resolver now retypes lambda args after resolution when requirements imply Fn*/FnThrow* (uses signature param typevars to map subjects).
- on_error method call special-case no longer marks lambdas as capture-invoke; captureless lambdas now coerce to function pointers and lower correctly.

### MIR validation + e2e runner
- MIR: `_infer_expr_type` now consults typed `expr_types` for all expressions, fixing missing local types for casts used by wrapping_u64 ops.
- MIR: `_infer_expr_type` no longer overrides known local types for `HVar` (avoids treating `self` as scalar in ArrayRange methods).
- E2E runner: added per-test timeout enforcement in ordered/single-thread runs so hangs report the exact case name.

### Concurrency runtime (VT scheduling)
- Fixed double-free in Linux fiber path: `drift_thread_join`/`join_timeout` no longer free VT stacks (worker owns them).
- Fixed VT park/unpark race for timer/park: re-check park token after marking PARKED and after timer registration.
- Fixed fiber scheduler: only free VT stack on FINISHED/CANCELLED after swapcontext (not on PARKED yields).

### Package type-table linking
- Normalized package ids for std/lang modules in type-table linking (`lang.*`/`std.*` resolve to `std`, `lang.core` stays `lang.core`), preventing schema/TypeDef mismatches for toolchain-provided types.
- Added interface instance handling in type key instantiation during package link.
- Added RAW_PTR type key handling in package linker.

### Package root + stdlib method resolution
- Filtered external signatures for reserved toolchain modules (`std.*`, `lang.*`, `drift.*`) at package-load time so they never enter `signatures_by_id_all` (prevents duplicate stdlib method entries when packages are present).
- Registry now skips registering external signatures whose fn_id already exists; also skips registering external std/lang/drift signatures.
- Method registry de-dupes identical inherent/trait method signatures per (type, name, self_mode) to avoid ambiguity from duplicate entries.
- Added regression: `test_package_root_does_not_duplicate_std_methods` (package-root + stdlib Deque methods resolve without ambiguity).
- Package emission now only omits reserved modules when they originate under `stdlib_root`; user-defined `std.*`/`lang.*`/`drift.*` modules stay in the manifest so unsigned reserved namespaces are rejected as required.
- Workspace parser now skips stdlib root when module roots include reserved-namespace modules (e.g., test-only std.mem stubs), preventing stdlib/override collisions in dev tests.
- ODR instantiation test now compiles IR to object (`clang -c`) instead of linking; avoids runtime symbol requirements while still validating symbol dedup via `nm`.

### Implicit Fn → Callback coercion
- Added e2e: `concurrent_spawn_cb_implicit_callback` to cover implicit coercion at callsite.
- Added e2e: `implicit_callback_borrowed_capture_rejected` to ensure borrowed captures are still rejected when coercion is implicit.
