# TODO

## MVP
[String]
- Expose/route any user-facing string printing helper once available.
- Keep expanding test coverage as features land (print, more negative cases).

[I/O]
- Move `print`/`println`/`eprint`/`eprintln` from the prelude hack into real `std.console` / `std.io` APIs (no lang.core special-casing).

[Logging]
- Add an MVP logging library with thread-safe logging semantics (safe multi-threaded producers, deterministic sink ownership/serialization).

[FFI / ABI]
- Document “current ABI intent” now; freeze later:
  - Variant layout intent (tag width rules, payload alignment, field order) and what is stable vs internal.
  - Calling convention assumptions at ABI boundaries and current string/array/buffer representations.
  - Add an explicit NOT YET STABLE banner + checklist for freezing.

[Traits]
- Completed in MVP:
  - `Copy` trait gating post-typecheck.
  - Trait-based iteration surface (`std.iter` with `Iterable`/`SinglePassIterator`) replacing iterator-intrinsic usage.
  - `Destructible` end-to-end (trait/checker `DropValue`/codegen lowering/interface owned-vtable drop-slot contract).

[Operators]
- Pin operator overloading MVP: define operator->trait desugar rules (e.g., `a + b` -> `Add::add(...)`), scope/prelude policy, and by-ref/by-value signature contract; add tests. Keep this aligned with the "free function vs receiver" resolution decision.

[Concurrency]
- Concurrency primitives + runtime MVP:
  - scheduler (virtual threads + carrier threads), reactor integration, and stack management.
  - blocking I/O boundary helpers for std.io/std.net (park/unpark on would-block).
  - std.concurrent public API per virtual_threads_concurrency_spec.md (spawn/join/scope/sleep).

## Post MVP
[Concurrency]
- Add ReentrantMutex (distinct from Mutex); define semantics and API surface.

[Containers]
- Start migrating Array into stdlib (define `struct Array<T>` and move compiler lowering to that ABI).

[Traits]
- Dynamic dispatch and trait bounds: pin surface syntax and type rules for trait bounds / trait objects.
- `Array<String>.dup()` should require `String.dup()` and then lift `Array<T>.dup()` to `T: Dup` (out of MVP scope).

[Error handling]
  - Captures (`^`): implement unwind-time frame capture (locals per frame + frames list), runtime representation, lowering/codegen, and e2e tests.
  - DiagnosticValue payloads: design/implement a stable ownership/handle model for opaque/object/array payload kinds so they can be stored in `Error.attrs` without ABI/lifetime churn.

[Variants]
  - Module-qualified constructor syntax: consider `Optional.Some(...)` ergonomics once namespacing rules are pinned (keep current `TypeRef::Ctor(...)`).
  - Variant pattern ergonomics: consider rest/wildcard patterns and richer exhaustiveness diagnostics (named-field construction + named binders are implemented in MVP).
  - Variant external ABI: freeze and document a stable ABI in `docs/design/drift-lang-abi.md` once FFI/packages demand it (currently compiler-private).

[Tooling / Packages]
- Phase 5 polish (highest leverage):
  - Lockfile authoritative by default: `drift build` honors `drift.lock.json`; only `drift update` changes resolution.
  - Multi-source deterministic selection rules (stable source ordering + tie-break + precedence) so identical inputs resolve identically.
  - Sharper index/identity mismatch errors (print claimed vs observed identity, signer, source id, mismatch axis).
  - `drift doctor` (sources, index sanity, trust graph, lock/cache consistency, toolchain compatibility).
  - `drift fetch --json` (machine-readable resolution/verification report for CI/IDE).
