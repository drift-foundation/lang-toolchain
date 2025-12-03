## Overview

This workstream enhances Drift exceptions so that:

* Exceptions support **any number of arguments**, each of arbitrary type.
* All arguments undergo **diagnostic formatting**, not `Display`.
* Args and `^` locals are stored as **diagnostic strings** (no “first payload” special case).
* Catch-time access uses **typed keys** instead of stringly lookup.
* **Dot-shortcut** sugar improves ergonomic, type-safe access to exception argument keys.

This file tracks work across three steps:

* **Step 1 — Spec cleanup** *(complete)*
* **Step 2 — Diagnostic trait formalization** *(complete)*
* **Step 3 — Key-indexing & dot-shortcut sugar** *(done)*

---

## Step 1 — Spec cleanup (complete)

* Removed the “first payload” special case; all exception args and `^` locals are Diagnostic→String.
* Catch semantics clarified; `Error.args` defined as `Map<String, String>`.

## Step 2 — Diagnostic trait (spec-only) (complete)

* Added `Diagnostic`/`DiagnosticCtx` in the spec; exceptions and `^` locals require Diagnostic.

## Step 3 — Key-indexing & dot-shortcut sugar (done)

### What’s implemented

* **Parser/grammar**: leading-dot syntax inside `[]` is accepted (`e.args[.a]`), desugared to an `Index` whose index is `Attr(base, "a")`.
* **Checker**: 
  * Per-exception ArgKey/ArgsView structs synthesized; args helpers (`view.a()`, etc.) registered.
  * `e.args` in typed catches resolves to the per-exception ArgsView; indexing expects the ArgKey type and returns `Option<String>`.
  * Leading-dot indexes are treated as ArgKey-based lookups (no string indexing).
* **Lowering/SSA**:
  * `e.args` lowers to the synthetic ArgsView wrapper.
  * ArgsView indexing (including leading-dot forms) extracts the ArgKey name and calls `__exc_args_get(err, name)` returning `Option<String>`.
  * `ExceptionCtor` seeds all declared args: first via `drift_error_new_dummy`, rest via `drift_error_add_arg`.
* **Runtime**:
  * `DriftError` carries an args array; `drift_error_add_arg` appends; `__exc_args_get` returns `Option<String>` (`{i8 is_some, DriftString}`) over `drift_error_get_arg`.
* **Tests**:
  * SSA: `exception_args_lookup.drift` (ArgKey helpers), `exception_args_dot_lookup.drift` (dot-style usage), `exception_args_unknown_key.drift` (negative key).
  * Runtime: `runtime_error_args_none.c` exercises the None branch of `__exc_args_get`; `runtime_error_dummy_raw.c` still checks code packing.

### Status: **DONE**

Leading-dot grammar + ArgKey-based lowering are implemented and tested. Future work (if needed) would focus on generic `Error` access or more ergonomic unwrap helpers, but dot-shortcut and typed-key args views are complete.
