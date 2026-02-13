# Registry Singleton Manager Work Progress

## Goal

Provide a standard, runtime-backed singleton registry for long-lived process-wide services without language-level mutable globals.

Primary first user: `std.log` global runtime state.

## Pinned Decision

- Registry is separate infrastructure work (not embedded in logger implementation).
- `std.log` global state will be implemented through this registry.
- Keep MVP small: global registry only (thread-local deferred).
- Compiler primitive pinned: `type_id<T>()` (canonical runtime type tag token).

## MVP Scope

1. New stdlib module: `std.runtime`.
2. Process-wide type-keyed global registry API:
   - `global_registry() -> &GlobalRegistry`
   - `set<T>(value: T) -> Bool` (set-once)
   - `contains<T>() -> Bool`
   - `get<T>() -> Optional<&T>`
3. Constraints:
   - `T is Unborrowed + Send + Sync`
   - no remove/replace in MVP
   - thread-safe for concurrent readers and first-writer race
4. Runtime backing:
   - stable type-key identity
   - set-once semantics enforced atomically
   - process-lifetime ownership (drop at process shutdown only if supported safely)

## Type Tag Pin

- `type_id<T>() -> Uint64` is the pinned intrinsic for runtime type tags.
- Token must represent canonical type identity (`package::module::Type<args...>` equivalent identity).
- Registry typed access checks tag equality before downcast.

## Downcast Naming Pin

- Downcast API is reference-oriented by definition in this design.
- Names:
  - `downcast<T>() -> Optional<&T>`
  - `expect_downcast<T>(tag: String) -> &T`
- No by-value downcast in MVP.
- Mutable variant may be added later as `downcast_mut<T>() -> Optional<&mut T>` if policy allows.

## Non-Goals (MVP)

1. Thread-local registry.
2. Mutable reference return (`&mut T`).
3. Delete/unregister/replace APIs.
4. Policy layers (TTL, eviction, namespaced keys).

## API Draft

```drift
module std.runtime

struct GlobalRegistry

fn global_registry() -> &GlobalRegistry

implement GlobalRegistry {
    fn set<T>(self: &GlobalRegistry, value: T) nothrow -> Bool require T is Unborrowed, T is Send, T is Sync
    fn contains<T>(self: &GlobalRegistry) nothrow -> Bool require T is Unborrowed, T is Send, T is Sync
    fn get<T>(self: &GlobalRegistry) nothrow -> Optional<&T> require T is Unborrowed, T is Send, T is Sync
}
```

## Implementation Plan

1. Add driver regression tests for API typing/contracts:
   - accepts `Unborrowed + Send + Sync` types
   - rejects borrowed/non-send/non-sync cases
2. Add e2e set/get/contains correctness:
   - set then get same type
   - second set returns false
   - unrelated type lookup returns None
3. Add e2e concurrency race test:
   - many threads attempting first `set<T>`; exactly one success
4. Implement runtime registry backing and lang intrinsics needed by `std.runtime`.
5. Wire `stdlib/std/runtime/runtime.drift` to runtime.
6. Run targeted + ASAN + alloc-track sweeps.

## Logger Dependency Hook

After this plan lands:
1. `std.log` stores `LoggerRuntimeState` via `global_registry().set<LoggerRuntimeState>(...)`.
2. `log.*` fetches singleton via `get<LoggerRuntimeState>()`.
3. Logger queue path migrates to `std.sync::MpscQueue` with singleton ownership via registry.

## Validation Matrix

1. Driver:
   - std.runtime API compile/type regressions
2. e2e:
   - basic set/get/contains
   - set-once race
   - cross-type isolation
3. Sanitizers:
   - `DRIFT_ASAN=1`
   - `DRIFT_ALLOC_TRACK=1`

## Landed (This Branch)

1. Compiler primitive `std.core.type_id<T>() -> Uint64` implemented and pinned.
2. Deterministic token generation wired in stage2 from canonical type key identity.
3. Regressions added:
   - `lang/tests/codegen/e2e/std_core_type_id_basic`
   - `lang/tests/codegen/e2e/std_core_type_id_cross_module`
4. Validation:
   - both regressions pass in normal mode
   - both regressions pass with `DRIFT_ASAN=1` + `DRIFT_ALLOC_TRACK=1`
5. LANGUAGE_BUG fix: explicit `captures()` no longer falsely requires lambda params/locals.
   - Regression: `lang/tests/driver/test_explicit_capture_diagnostics.py::test_explicit_empty_captures_allows_lambda_param_usage`
6. LANGUAGE_BUG fix: generic function type-application function values preserve throwness and fnptr-const metadata.
   - Regression: `lang/tests/driver/test_callback_generic_typeapp_nothrow.py`
7. LANGUAGE_BUG fix: callback function-pointer refs from generic type application inside generic functions now codegen/link correctly.
   - Regression: `lang/tests/driver/test_callback_generic_typeapp_codegen.py`
8. LANGUAGE_BUG fix: `type_id<type T>()` now resolves concrete `T` in instantiated generic bodies during MIR lowering.
   - Regression: `lang/tests/codegen/e2e/std_core_type_id_generic_instantiation`
9. `std.runtime` TypeBox e2e restored and passing:
   - `lang/tests/codegen/e2e/std_runtime_typebox_downcast`
   - `lang/tests/codegen/e2e/std_runtime_typebox_expect_tag`
10. Validation:
   - Normal: targeted driver + e2e regressions pass
   - Sanitizers: `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1` targeted e2e regressions pass

## Current Blocker (Pinned)

None for callback/type_id compile-path issue. Registry mechanics resumed.

## Landed (This Slice)

1. Added runtime-backed `GlobalRegistry` in `std.runtime`:
   - `global_registry() -> &GlobalRegistry`
   - `GlobalRegistry.set<T>(value: T) -> Bool` (set-once)
   - module-level typed readers:
     - `contains<T>(reg: &GlobalRegistry) -> Bool`
     - `get<T>(reg: &GlobalRegistry) -> Optional<&T>`
2. Added runtime intrinsics in `lang.thread`:
   - `runtime_global_registry_ptr()`
   - `runtime_registry_set(type_tag, ptr, dropper)`
   - `runtime_registry_contains(type_tag)`
   - `runtime_registry_get(type_tag)`
3. Added LLVM lowering + declarations for the runtime registry intrinsics.
4. Added POSIX runtime registry implementation:
   - process-global type-tag map
   - mutex-protected set/contains/get
   - set-once first-writer semantics
   - process-exit cleanup via `atexit`
   - typed value cleanup via stored dropper callback
5. Added e2e coverage:
   - `lang/tests/codegen/e2e/std_runtime_global_registry_basic`
   - `lang/tests/codegen/e2e/std_runtime_global_registry_set_once_race`
6. Added LANGUAGE_BUG regression + fix:
   - Regression: `lang/tests/driver/test_method_require_copy_receiver_args_regression.py`
   - Fix: guard `receiver_args` empty-list path in `call_resolver.py` to avoid internal `IndexError`.

## Pinned Language Limitation

Generic method calls with explicit type arguments are not currently usable for this API shape in practice (`reg.contains<T>()`/`reg.get<T>()` with no value args). Current registry read API uses module-level generic functions (`contains<T>(reg)`, `get<T>(reg)`) on top of non-generic internal methods.

## Landed (This Slice 2)

1. Added registry miss helper API in `std.runtime`:
   - `RegistryError(tag: String)`
   - `expect<T>(reg: &GlobalRegistry, tag: String) -> &T` (throws `RegistryError` on miss).
2. Added regression coverage for generic throw with string exception fields:
   - `lang/tests/driver/test_exception_string_generic_throw_regression.py`
   - includes direct generic throw and optional-ref/match path before throw.
3. Added registry e2e for `expect` success+miss tag behavior:
   - `lang/tests/codegen/e2e/std_runtime_global_registry_expect_tag`.
4. Added registry examples + effective-drift docs:
   - `examples/runtime_registry/global_singleton.drift`
   - `examples/runtime_registry/per_thread_slots.drift`
   - `docs/effective-drift.md` registry section.
5. Validation:
   - e2e `std_runtime_global_registry_expect_tag`: passing
   - targeted driver subset: passing (`13 passed`).

## Pinned Limitation (Catch Binder Shape)

- Catch binders currently lower as `Error` values, not concrete exception structs.
- Field projection like `catch Mod:Exc(e) { e.tag }` is not yet supported in MIR.
- Current supported form in catch bodies is `e.attrs["field"]` (+ `as_*` extractors).

## Landed (This Slice 3)

1. Fixed LANGUAGE_BUG in runtime-registry callback/drop ABI at LLVM boundary:
   - Root cause: `drift_runtime_registry_set` was declared/called as taking `%DriftIface` by-value in generated LLVM, while C ABI lowers this parameter as `byval` pointer.
   - Effect: runtime observed null/invalid dropper vtable in registry cleanup, so payload drop callbacks were not invoked.
2. Fixed LLVM codegen/runtime ABI shape:
   - `%DriftIface` emitted with explicit ABI padding (`{ i8*, i8*, [4 x i64], i8, [7 x i8] }`) to match runtime layout.
   - `drift_runtime_registry_set` declaration switched to byval-pointer form:
     - `(..., %DriftIface* byval(%DriftIface) align word)`
   - call lowering for `lang.thread::runtime_registry_set` now spills iface to stack and passes byval pointer.
3. Removed stale skipped e2e placeholder:
   - deleted empty `lang/tests/codegen/e2e/catch_typed_binder_field_projection`.
4. Validation:
   - `DRIFT_ALLOC_TRACK=1`:
     - `std_runtime_global_registry_arc_payload`: pass
     - `std_runtime_global_registry_get_concurrent_stress`: pass
     - `std_runtime_global_registry_nontrivial_payload`: pass
   - `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1` same subset: pass
   - full registry subset (`basic`, `expect_tag`, `set_once_race`, plus above): pass with alloc tracking.
