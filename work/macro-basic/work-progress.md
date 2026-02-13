# Macro Basic Work Progress

## Goal

Add minimal, reliable macro support to validate logger call-site ergonomics early, before deeper logger mechanics.

Primary first user: logging calls with caller metadata injection and lazy attrs construction.

## Pinned Decision

- Macro MVP lands before logger macro-dependent APIs.
- Keep macro support intentionally small and compiler-owned in MVP.
- No user-defined macros in MVP.
- Focus on one shape needed by logger usage.

## MVP Scope

1. Syntax: `qualified.name!(args...)`.
2. Placement: macro invocation is an expression-form construct.
3. Expansion phase: after parse, before typecheck.
4. Macro catalog: compiler-owned built-ins only.
5. First built-in family: logging-oriented macros (`#log.debug`, `#log.info`, `#log.error`).

## Syntax Pin

- Accepted form:
  - `log.info!(app.log.security, "auth-failed", {"user": user})`
- Macro path is qualified-name compatible (`a.b.c`).
- Invocation marker is postfix `!` on the resolved path.
- MVP delimiter is parentheses only (`(...)`).
- `#...` macro syntax is not part of MVP.

## Macro Resolution Pin

1. Macros resolve through normal module/path scope rules (same lookup model as values/types).
2. No implicit global macro namespace in user code.
3. Both styles are supported:
   - imported alias path:
     - `import std.log as log;`
     - `log.info!(...)`
   - fully qualified path:
     - `std.log.info!(...)`
4. This is pinned now to avoid future breaking changes when user-defined macros are introduced.

## Semantics Pin

1. Expand-before-typecheck:
   - expanded result is ordinary AST/HIR that typechecks normally.
2. Nested macros:
   - allowed in expression positions.
   - expansion order is inside-out.
3. No statement-only macro category in MVP.
4. No hygiene system in MVP beyond lexical expansion correctness.

## Logger-Driven Expansion Requirements

For `#log.<level>(factory, ev, attrs)` expansion must support:

1. Fetch logger from provided accessor/factory expression.
2. Level gate before attrs materialization/evaluation.
3. Caller metadata injection via `std.meta.caller()`.
4. Emit ordinary logger call on enabled path.
5. No-throw friendly disabled path (no-op).

## Non-Goals (MVP)

1. User-defined/declarative macros.
2. Token-tree macros.
3. Pattern-matching macro systems.
4. Full hygiene/renaming framework.
5. Compile-time eval surface beyond this expansion path.

## Open Decisions To Pin Early

1. Exact built-in macro argument contract:
   - positional arity and attrs shape.
2. Expansion diagnostics:
   - unknown macro path error code/message shape.
   - wrong-arity error code/message shape.
3. Macro namespace ownership:
   - reserved prefix policy (`log.*` built-ins).
4. Whether macro invocations are allowed in type-level contexts (default: no).

## Implementation Plan

1. Parser/AST support
   - add macro invocation node for `qualified.name!(args...)`.
   - add parser regressions for valid/invalid forms.

2. Expansion pass skeleton
   - new compiler pass: AST macro expansion (pre-typecheck).
   - keep pass deterministic and side-effect free.

3. Built-in macro dispatcher
   - map macro path -> expander implementation.
   - implement unknown macro + arity diagnostics.

4. Logging macro MVP
   - implement `#log.debug/info/error` expansion shape.
   - inject `std.meta.caller()`.
   - enforce lazy attrs creation on enabled path.

5. Validation and hardening
   - parser tests (positive/negative).
   - stage/unit tests for expansion shape.
   - e2e logger macro smoke + nested macro case.
   - ASAN + alloc-track targeted sweep.

## Regression-First Test Matrix

1. Parser
   - accepts: `log.info!(a, b, c)`.
   - rejects: missing `(`, bad path, malformed nested call.

2. Expansion
   - verifies expanded AST contains:
     - caller injection,
     - level gate branch,
     - deferred attrs expression evaluation.

3. E2E
   - macro logger call compiles and runs.
   - disabled level path does not evaluate attrs side effects.
   - nested macro expression expansion order behaves deterministically.

4. Diagnostics
   - unknown macro path.
   - wrong argument count.
   - macro use in disallowed context (if any pinned).

## Completion Criteria

1. `#log.debug/info/error(...)` works for pinned logger use case.
2. Logger macro path is validated by e2e and sanitizer runs.
3. Parser/expansion diagnostics are stable and covered.
4. No stdlib workaround required to achieve macro call-site UX.

## Landed (This Slice)

1. Macro invocation syntax MVP switched and implemented as path-postfix form:
   - `qualified.path!(args...)`
   - parser/AST support added for `MacroCall`.
2. Macro resolution contract pinned to module/path scope:
   - imported alias path (`import std.log as log; log.info!(...)`)
   - fully-qualified path (`std.log.info!(...)`).
3. Stage1 macro expansion implemented (pre-typecheck):
   - `log.info!/debug!/error!` rewrite to `log.__macro_info/__macro_debug/__macro_error`.
4. Temporary std.log helper stubs added for coexistence with future logger mechanics:
   - `__macro_info`, `__macro_debug`, `__macro_error`.
5. New tests:
   - parser: `lang/tests/parser/test_parser_macro_call_basic.py`
   - stage1 expansion: macro rewrite + arity rejection in `lang/tests/stage1/test_ast_to_hir.py`
   - driver diagnostics: `lang/tests/driver/test_macro_basic_diagnostics.py`
   - e2e smoke: `lang/tests/codegen/e2e/macro_log_registry_stub_smoke`
6. Lowering hardening:
   - parser->HIR pipeline now surfaces stage1 macro expansion `ValueError` as diagnostics instead of uncaught compiler exceptions.
7. Macro dispatch centralized:
   - new dedicated stage1 module `lang/driftc/stage1/macro_expander.py` owns built-in macro routing/rewrite.
   - `AstToHIR` now delegates macro handling to this module.
   - direct unit coverage added in `lang/tests/stage1/test_macro_expander.py`.
8. Caller metadata injection (MVP path) landed:
   - macro expansion now appends a hidden caller argument to rewritten calls.
   - caller carrier is `std.meta.caller()` (not a span-string literal).
   - `std.meta` module added with intrinsic `caller()` and `Caller` struct.
   - `Caller` exposes `module_id()`, `file()`, `line()`, plus `module_id_len()` / `file_len()`.
   - e2e coverage added for direct caller usage: `lang/tests/codegen/e2e/std_meta_caller_basic`.
9. LANGUAGE_BUG fixed (regression-first):
   - bug: repeated discard bindings (`val _ = ...`) in one function reused the same local slot, causing type corruption and invalid LLVM drop IR (`extractvalue` on `%self` pointer).
   - pinned regression: `lang/tests/codegen/e2e/discard_binding_rebind_noncopy_ir_stable`.
   - fix: MIR canonical local naming now gives `binding_id=None` discard bindings (`_`) unique hidden locals (`__discard*`) and avoids aliasing `_` type metadata back into the function local type map.
   - validated by targeted e2e including macro smoke + meta caller, plus ASAN + alloc-track subset.
10. String byte-length API cleanup landed:
   - Public API pinned to `String.byte_length()`; migrated touched tests/examples/callers to method form.
   - Global `byte_length(...)` now rejected for non-`std.*` modules with explicit diagnostic:
     - `"global byte_length(...) is not exposed; use s.byte_length()"`.
   - Added regression coverage:
     - e2e negative: `lang/tests/codegen/e2e/byte_length_global_rejected`
     - driver negative: `lang/tests/driver/test_string_byte_length_api.py`
11. Receiver autoborrow policy clean-up:
   - Removed method-name hardcoding from checker policy.
   - Shared `&self` receiver autoborrow now follows generic method-call rule (rvalue receiver allowed for shared borrow; `&mut self` still requires place).
   - Added/updated driver coverage in `lang/tests/driver/test_autoborrow_receiver_place.py` and `lang/tests/driver/test_method_call_nothrow_resolution.py`.
