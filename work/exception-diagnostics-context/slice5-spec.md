# Slice 5 Spec — `pub error` and the JSON-text Diagnostic Surface

**Status:** LOCKED v1 — 2026-05-03. K signed off on direction with corrections (DiagnosticParse scope-cut, ResultError raw-JSON-splice rule, collection projectability rules, all six `std.core` helpers in 0.32.0, reuse existing xxHash64 event-code scheme with package-level duplicate detection, strict `or_throw()` enforcement in 0.32.0 with global `Result<T, E>` strictness deferred). Ready for test-drafting (step 2 of §15 sequencing). Implementation slice 1 (parser/checker shape + std.core helpers) landed 2026-05-03 — see §20.

**Companion docs:**
- `work/exception-diagnostics-context/slice5-preflight.md` — inventory, decision history, K sign-off (§12).
- `memory/project_dv_to_json_diagnostics.md` — slice-by-slice landed history through 0.31.53.

**Target release:** 0.32.0 (compiler), ABI 13 (runtime).

---

## 0. Decisions Locked (K, 2026-05-03)

This spec treats the following as **confirmed**, not open:

1. **`pub exception` is a transitional alias for `pub error`** — compiles for one release; deprecation diagnostic if practical; canonical docs/spec use `pub error`. Hard-error deferred to 0.33.0. Suppression uses the existing diagnostic system; no new pragma.
2. **`Result<T, E: error>` is the long-term strict rule.** Slice 5 enforces strict on `or_throw()` (Phase 5a) and cleans all stdlib `Result<T, E>` Errs. Global hard enforcement on all `Result<T, E>` positions is deferred to 0.33.0 unless cheap to land in 0.32.0. Strict is the target end-state. Global Result strictness MUST NOT block DV removal in 0.32.0.
3. **`pub error` is a distinct language kind**, not desugar to `struct + implement Error`. Internally it MAY lower to existing exception metadata, but semantically it is the datatype category valid for `Result` Err, throw, catch, event identity, and synthesized diagnostics.
4. **Synthesis field ordering: lex-utf8.** Independent of source declaration order; survives field reordering refactors.
5. **Synthesis succeeds when every field is `Diagnostic`/JSON-text-projectable.** Built-in primitives and `pub error` types are projectable by default. Collection rules: `Optional<U>`, `Array<U>`, and `Map<String, V>` are auto-projectable when contained values are projectable. `Map<K, V>` with non-`String` `K` is NOT projectable (rejected). Pointer / function / opaque types are NOT auto-projectable. Non-projectable field → targeted diagnostic.
6. **Catch-by-supertype / marker is explicitly out of Slice 5.** Typed catch by concrete error identity only.
7. **`ResultError` is demoted, not centered.** Prefer delete if stdlib no longer needs it. If retained: do NOT model raw JSON as a normal `String` field (a normal field gets quoted via `diagnostic_json_string`, which is wrong for raw-JSON splice). The raw JSON carrier MUST be a private/internal field with a compiler-special raw-splice rule, exposed only through `from_diagnostic`-style helpers. No public raw constructor.

**DiagnosticParse scope cut (K, 2026-05-03):** Slice 5 v1 does NOT introduce a public `DiagnosticParse` trait. Synthesized `pub error` projections come with synthesized reverse projection — `catch ParseError(e)` typed-binder works automatically. Manual `Diagnostic.to_json_text` overrides are allowed for envelope/log output, but typed catch binding on a manually-projected `pub error` is rejected with a clear diagnostic in v1. Manual reverse parsing is a deliberate follow-up design track. Users do NOT write `DiagnosticParse` impls in Slice 5.

**`std.core` JSON helpers (K, 2026-05-03):** all six (`diagnostic_json_string`, `diagnostic_json_null`, `diagnostic_json_bool`, `diagnostic_json_int`, `diagnostic_json_uint`, `diagnostic_json_float`) ship in 0.32.0.

**Empty payload (K, 2026-05-03):** synthesized projection for `pub error E {}` returns `"{}"`. The envelope's `params` is ALWAYS a JSON object, never `null`, never omitted.

**`event_code` algorithm:** reuse the existing exception event-code scheme. The current scheme (verified at `lang/driftc/core/event_codes.py`) is xxHash64 of the fully-qualified `module:Name` with a 4-bit domain tag in the high bits (low 60 bits carry the hash payload). The spec previously said "FNV-1a 64-bit" — that was incorrect; the actual scheme is xxHash64. The spec defers algorithm replacement to a future slice if it ever becomes useful; Slice 5 does not block on this. Per-package duplicate detection is in place via the existing payload-collision check in `_build_exception_catalog` (the diagnostic name does not yet match `E_EVENT_CODE_DUPLICATE` from §13.2 — coded-diagnostic rename is deferred).

**Runtime boundary:** canonical JSON String only. `std.json` is allowed inside user projection code, but `JsonNode` / `JsonObject` / `JsonHandle` do NOT cross into the runtime exception envelope.

---

## 1. Goals & Non-Goals

### 1.1 Goals

- Replace `pub exception` with `pub error` as the user-facing canonical error datatype.
- Eliminate `DiagnosticValue` from the user surface entirely.
- Make `Result<T, E>.or_throw()` throw `E` directly, so catch-by-type stays precise.
- Provide synthesized JSON projection for `pub error` types so users don't write boilerplate `Diagnostic` impls for typical cases.
- Provide a small, sanctioned `std.core` JSON-text helper surface for users who do write manual projections.
- Separate `Debuggable` (logging) from `Diagnostic` (errors); both move to JSON text with distinct method names so future redaction rules can diverge.
- Lock the runtime exception envelope at canonical JSON String; ABI 13 drops legacy DV storage from `DriftError`.

### 1.2 Non-goals (explicit deferrals)

- **Catch-by-supertype/marker:** Slice 5 supports typed catch by concrete error identity only. A future slice can add marker-trait or supertype dispatch.
- **`e.context.get(...)` typed cursor over frames:** deferred to Slice 4B (separate from Slice 5's removal pass).
- **Stack-trace runtime support:** envelope continues to emit `"stack": null`; ABI 13 does NOT add a `stack` C-layout field.
- **Generic-error coercion:** removing `ResultError` does not introduce a synthesized `GenericError`. Code that today relies on universal `catch ResultError(e)` must migrate to per-error-type catches or a top-level `catch *`/`catch Error` shape (also out of scope here; today `catch *` already exists, see §5.4).
- **Migration of non-stdlib downstream packages:** the spec defines compiler diagnostics that point users at the migration; downstream package rebuilds are operational work, not spec.

---

## 2. `pub error` Declaration

### 2.1 Syntax

```drift
pub error ParseError {
    message: String,
    offset: Int,
}

// Optional explicit event code (otherwise auto-assigned, see §2.4).
pub error CodecError(0x4543) {
    kind: String,
    detail: String,
}

// Empty-payload error type.
pub error ConnectionLost {}
```

**Grammar additions** (informal; final form during implementation):

- New keyword **`error`** (in modifier position; `pub error`, `error` for module-private).
- `error_decl`: `pub? "error" IDENT ("(" UINT_LIT ")")? "{" field_decl* "}"`.
- Same field syntax as struct/exception today.

**`pub exception` transitional alias** (per K answer 1):
- Continues to parse and compile in 0.32.0.
- Lowers to `pub error` semantics — same trait shape, same throw/catch behavior, same synthesized projection.
- Compiler emits `W_PUB_EXCEPTION_DEPRECATED` warning at the declaration site (a warning, not error). Wording suggestion:
  > `warning: 'pub exception' is deprecated; use 'pub error' (will be removed in 0.33.0)`
- The warning may be suppressible with a per-module pragma if needed for staged migrations of large codebases (e.g., `@allow(deprecated_exception)`); exact mechanism per existing diagnostic-suppression conventions.
- `pub exception` becomes a hard error in 0.33.0 (separate slice / release).

### 2.2 Type semantics — distinct kind, not desugar (K answer 3)

`pub error E { ... }` introduces a **new type kind** `Error<E>` with these properties:

1. **Value type.** Constructable, copyable per its field types' Copy semantics, passable by value, returnable, storable in fields, storable in collections. Does NOT have to be thrown.
2. **Constructor.** Same syntax as struct: `ParseError(message = "bad", offset = 12)`. Positional construction allowed when fields support it.
3. **Field access.** `e.message`, `e.offset` work as for struct fields. Read-only by default; `var` rebinding rules per existing struct conventions.
4. **Copy / Frozen / ConstShare.** Synthesized following the same composition rule as struct: if every field is Copy, the error is Copy; if every field is Frozen, the error is Frozen; if every field is ConstShare, the error is ConstShare. (These are the exact rules already in place for struct; `pub error` reuses them.)
5. **Throwable.** `throw E(...)` is well-typed iff `E` is an error datatype.
6. **Catchable by type.** `catch E(e) { ... }` matches throws of `E` exactly (not subtypes — see §5.3).
7. **`Result<T, E>` Err position.** `E` is valid as an Err type for `Result<T, E>` constructors, `or_throw`, etc. — see §3.
8. **Event identity.** Each `pub error` type carries a stable `event_code` (u64) and `event_fqn` (string) — see §2.4.
9. **Diagnostic capability.** `pub error` types are `Diagnostic` automatically (synthesized impl) when all fields are projectable; users may override — see §6 / §7.

**Internal lowering note (informational, not normative):** the compiler MAY lower `pub error E { ... }` to the existing exception metadata path internally (event-code table, throw runtime, etc.). The spec does not constrain the lowering, only the user-visible semantics. The "distinct kind" guarantee is at the type-system / source-language level — `is`/`as` queries, trait resolution, and diagnostic kinds treat `pub error E` as an error datatype, not a generic struct.

### 2.3 Module visibility

- `pub error E { ... }` — exported from the declaring module.
- `error E { ... }` — module-private. Throwable + catchable within the module; not a valid `Result` Err type for cross-module APIs.
- A `pub error` field of a non-`pub` type is rejected (existing struct visibility rule applies).

### 2.3.1 Visibility coherence rule (K, 2026-05-03)

A private `error E` MUST NOT leak through any exported signature. When the enclosing function / binding is `pub`, every error type referenced in any of the following positions MUST itself be `pub error`:

- Function `throws` clause: `pub fn f() throws E -> T`.
- `Result<T, E>` return type: `pub fn f() -> Result<T, E>`.
- Function parameter types or any nested error type within them.
- Public struct / variant / error field type.
- Any other exported signature surface (re-exported type aliases, public trait method signatures, etc.).

**Concrete rejected forms:**

```drift
error InternalError {}

pub fn f() throws InternalError -> Int { ... }
//                ^^^^^^^^^^^^^
// error: public function 'f' exposes private error 'InternalError' in throws clause
```

```drift
error InternalError {}

pub fn f() -> Result<Int, InternalError> { ... }
//                        ^^^^^^^^^^^^^
// error: public function 'f' exposes private error 'InternalError' in Result Err position
```

**Private-only use stays valid:**

```drift
error InternalError {}

fn helper() -> Result<Int, InternalError> { ... }   // OK — not exported
```

**Why:** without this rule, the `error` vs `pub error` distinction is incoherent — thrown / returned errors are part of the API surface, so the visibility checker MUST enforce that internal-only error types do not leak.

**Diagnostic code (placeholder):** `E_PRIVATE_ERROR_LEAKED_VIA_PUB`. The message includes the offending public symbol's name, the private error type's name, and the leak position (throws clause / Result Err / field / etc.).

**Implementation timing:** the rule is part of the checker pass — best landed alongside the `throws` / `Result` Err strict enforcement (Phase 5a per §3.2). Deferred to a follow-up implementation slice; slice 1 (parser/checker shape + std.core helpers) does NOT enforce visibility coherence yet.

### 2.4 Event code and event_fqn

- `event_fqn` is the fully-qualified type name in the format `<package>:<TypeName>` (e.g., `my.pkg:ParseError`). Generated at declaration time; stable per release of the declaring package.
- `event_code` is a u64 stable routing identifier:
  - **Explicit form:** `pub error CodecError(0x4543) { ... }` — user pins the code.
  - **Auto-assigned form:** when omitted, the compiler computes the code via the existing exception event-code scheme — currently xxHash64 of the fully-qualified `module:Name` with a 4-bit domain tag in the high 4 bits (`lang/driftc/core/event_codes.py`). Cross-package consumers compute the same code via the same scheme.
- `event_code` and `event_fqn` are read at the runtime boundary (see §10) and used by `catch E(e)` dispatch (see §5).
- Field naming `event_*` is preserved at the runtime/ABI level even though docs may use "error_*" wording (per K, §12.2 of preflight). No churn for renaming runtime fields.

---

## 3. `Result<T, E>` Constraint

### 3.1 Spec rule (target end-state)

`Result<T, E>` REQUIRES `E` to be an error datatype:
> `error[E0XYZ]: Result<T, E> requires E to be a 'pub error' type; got '<type>'`

This applies to:
- All `Result<T, E>` constructions: `Result::Ok(...)` for the Err type, `Result::Err(e)` for `e: E`, function signatures returning `Result<T, E>`, fields of type `Result<T, E>`.
- All `Result` method usages: `or_throw()`, `unwrap()`, `unwrap_or()`, etc. — but see §3.3.

### 3.2 Implementation staging (K, 2026-05-03)

K pinned the staging:

1. **Phase 5a (LANDS in 0.32.0, strict):** `or_throw()` strictly requires `E: pub error`. Diagnostic `E_OR_THROW_NOT_ERROR_TYPE` at non-error `Result<T, E>` `or_throw()` call sites.
2. **Phase 5b (LANDS in 0.32.0):** All STDLIB uses of `Result<T, E>` migrate to `pub error` Errs. Stdlib must be clean before release.
3. **Phase 5c (warning in 0.32.0; error in 0.33.0):** Non-error `Result<T, E>` positions outside stdlib emit `W_RESULT_ERR_NOT_ERROR_TYPE` if practical. Promoted to `E_RESULT_ERR_NOT_ERROR_TYPE` in 0.33.0. If implementing the warning is cheap, ship it; if it requires generic-bounds machinery work, defer the warning too — but the strict `or_throw` enforcement at 5a is non-negotiable and **MUST NOT be blocked by Phase 5c work**.

The spec end-state is strict global enforcement; the staging is an implementation kindness, not a spec relaxation.

### 3.3 `or_throw()` semantics

`Result<T, E>.or_throw()` throws `E` directly (NOT `ResultError(dv = ...)`):

```drift
fn parse_int(s: &String) -> Result<Int, ParseError> { ... }

fn main() throws ParseError -> Int {
    val n = parse_int(&"42").or_throw();  // throws ParseError on failure
    return n;
}
```

The throw event has:
- `event_code`: `ParseError`'s event_code.
- `event_fqn`: `"<pkg>:ParseError"`.
- `params`: synthesized JSON projection of the `ParseError` value (see §7) OR user override (see §6).
- `context`: `^`-capture frames (unchanged from Slice 2).
- `stack`: `null` (deferred).

Catch:
```drift
try {
    val n = parse_int(&input).or_throw();
} catch ParseError(e) {
    println("parse failed at offset ", e.offset, ": ", e.message);
}
```

`e` binds the materialized `ParseError` value with full field access. **Scope cut for v1 (K, 2026-05-03):** typed catch-binding works automatically for `pub error` types using the synthesized `Diagnostic` projection. For `pub error` types with a manual `to_json_text` override, typed catch-binding is REJECTED at compile time with `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED` — see §7.5. Manual reverse parsing (`DiagnosticParse`) is a follow-up design track, not Slice 5.

### 3.4 Other `Result` methods

- `unwrap()`: panics on Err; spec is unchanged.
- `unwrap_or(default)`: returns Ok value or default; spec is unchanged.
- `map`/`map_err`: unchanged; `map_err: F where F: Fn(E1) -> E2 where E2: pub error` — `E2` must be an error type per §3.1.
- `ok()`/`err()`: returns `Optional<T>` / `Optional<E>`; unchanged.
- New: explicit conversion path from a `pub error` value to a thrown event without wrapping in `Result` first — see `throw e` directly, no helper needed (already supported, per §4).

---

## 4. `throw` Statement

```drift
throw ParseError(message = "bad input", offset = 12);
```

`throw E(...)` requires `E` to be a `pub error` type. The throw lowers to:
1. Construct the `E` value.
2. Project to `params_json` via synthesized or user `Diagnostic.to_json_text` impl.
3. Project `^`-capture frames into `context_json` (unchanged from Slice 2).
4. Allocate `DriftError` with `event_code`, `event_fqn`, `params_json`, `context_json`.
5. Unwind.

A user MAY also `throw` a previously-constructed `pub error` value:
```drift
val e = ParseError(message = "bad", offset = 12);
// ... possibly stash, log, etc. ...
throw e;
```

The value is moved into the throw site. After throw, the local is consumed.

**Existing `throw <expr>` for non-error types becomes a compile error** (was already a constraint via `Throw` trait in 0.31.53; tightens to specifically require `pub error`):
> `error[E0XYZ]: cannot throw value of type '<T>'; only 'pub error' types are throwable`

---

## 5. Catch Routing

### 5.1 Catch syntax

```drift
try {
    risky_op();
} catch ParseError(e) {
    // e: ParseError
    log_error(e.message, e.offset);
} catch CodecError(e) {
    log_error(e.kind, e.detail);
} catch * {
    // wildcard — catches any thrown pub error not matched above
    // bound name optional in this slice; consider `catch * as e` later
}
```

### 5.2 Type matching

- `catch E(e)` matches if and only if the thrown value's `event_fqn` equals the fully-qualified name of `E` declared in scope.
- The compiler emits the `event_code` for `E` at compile time and dispatches via `event_code` equality (existing runtime path).
- `e` is bound to a fresh local of type `E`, materialized from the envelope's `params_json` per the inverse of the projection (see §7.5).

### 5.3 No catch-by-supertype (K answer 6)

Slice 5 does NOT support:
- `catch SomeMarker(e)` — catching all `pub error` types implementing a marker trait.
- `catch BaseError(e)` — catching a parent error type.

These are explicit non-goals. A future slice may add them.

### 5.4 Wildcard `catch *`

`catch *` continues to mean "catch any thrown value." In Slice 5 this is "catch any thrown `pub error`." The bound binder (if any) sees the value as an opaque `pub error` for which only the envelope accessors (`e.encode_compact()`, `e.params.get(...)`, `e.context`) are well-defined; field-typed access via `e.<field>` is not available because the static type is unknown.

(Dynamic field access via `e.params.get("message").as_string()` is available — that's what §6.4 of the existing public surface gives.)

### 5.5 Re-throw

```drift
catch ParseError(e) {
    log(e.message);
    throw e;  // re-throws ParseError with the original event identity
}
```

Re-throw of a caught `pub error` preserves `event_code`, `event_fqn`, `params_json`, and `context_json`. The compiler MAY re-project (re-call `to_json_text`) or pass through stored JSON; the spec requires only that the re-thrown envelope is observably equivalent to the caught one. Implementation choice.

---

## 6. The `Diagnostic` Trait

### 6.1 Definition

```drift
pub trait Diagnostic {
    fn to_json_text(self: &Self) nothrow -> String;
}
```

Located in `std.core` (where the legacy `Diagnostic` trait already lives — same module path, same name; method renamed and return type changed).

### 6.2 Contract

The returned `String` MUST be a valid RFC-8259 JSON value (one of):
- A JSON object: `{"key":"value",...}`
- A JSON array: `[1,2,3]`
- A JSON string: `"escaped text"` (note the surrounding quotes are part of the value)
- A JSON number: `42`, `-1.5`, `1.5e10`
- A JSON boolean: `true` / `false`
- A JSON null: `null`

Returning malformed JSON corrupts the exception envelope. The compiler does NOT validate the returned text at compile time. Users SHOULD use the `std.core` helpers (§9) for primitive escaping.

`nothrow` is required: projection MUST NOT throw, since it runs on the throw path. A projection that wants to fail produces a JSON value indicating failure (e.g., `"<projection error>"`).

### 6.3 Built-in primitive impls

Stdlib provides `Diagnostic` impls for primitives:

| Type | `to_json_text` returns |
|---|---|
| `Int` | decimal text via `format_int` |
| `Uint` | decimal text via `format_uint` |
| `Bool` | `"true"` / `"false"` literal |
| `Float` | `format_float(self)` (current canonical decimal form) |
| `String` | `diagnostic_json_string(self)` — RFC-8259 escaped + quoted |
| `pub error` types | synthesized impl — see §7 |

### 6.4 User-defined types

User types (struct, variant, generic) MAY implement `Diagnostic` manually:

```drift
implement Diagnostic for MyId {
    pub fn to_json_text(self: &MyId) nothrow -> String {
        return diagnostic_json_string(&self.value);
    }
}
```

For complex hierarchical projections, users MAY use `std.json` inside the impl body to build a `JsonNode` tree and serialize it:

```drift
implement Diagnostic for ComplexValue {
    pub fn to_json_text(self: &ComplexValue) nothrow -> String {
        val node = json.object();
        node.set("name", json.string(&self.name));
        node.set("count", json.int(self.count));
        return node.encode_compact();
    }
}
```

`std.json` is allowed inside the projection — the resulting `String` is the boundary, NOT the `JsonNode`.

---

## 7. Synthesized `Diagnostic` for `pub error` Types

### 7.1 When synthesis fires (K answer 5)

The compiler synthesizes `implement Diagnostic for E` for every `pub error E` declaration UNLESS:
1. The user provides an explicit `implement Diagnostic for E` impl in the same package as the declaration (manual override), OR
2. Any field of `E` is not `Diagnostic`-projectable (synthesis fails closed — see §7.4).

Synthesis fires at declaration time (compile-time, not runtime).

### 7.2 Field projectability

A field's type `T` is projectable if any of:
- `T` is a built-in primitive (`Int`, `Uint`, `Bool`, `Float`, `String`).
- `T` is a `pub error` (recursively projectable via its own synthesized or manual impl).
- `T` has an `implement Diagnostic for T` in scope.
- `T` is a struct/variant where every field type is projectable AND the struct/variant has a synthesized or explicit `Diagnostic` impl (composition rule — recursive).
- `T` is `Optional<U>` where `U` is projectable. Projection: `null` for `none`, `<U-projection>` for `some(u)`.
- `T` is `Array<U>` where `U` is projectable. Projection: JSON array `[<u1>, <u2>, ...]`.

**NOT projectable by default:**
- `RawPtr<T>`, `Ptr<T>` (pointer types — meaningless to serialize).
- `Map<K, V>` where `K != String` (JSON object keys must be strings).
- Function/lambda/callback types.
- `TypeBox` (opaque).

Users can make any of these projectable by writing a manual `implement Diagnostic` for the relevant carrier type.

### 7.3 Synthesis algorithm

For `pub error E { f1: T1, f2: T2, ... }` with all fields projectable:

```drift
implement Diagnostic for E {
    pub fn to_json_text(self: &E) nothrow -> String {
        // Pseudo-code; lex-utf8 sorted (K answer 4)
        // Final emitted form is a series of String concats with
        // pre-quoted keys + per-field to_json_text(&self.f).
        return "{" +
            "\"<sorted_key_1>\":" + Diagnostic.to_json_text(&self.<sorted_key_1>) + "," +
            "\"<sorted_key_2>\":" + Diagnostic.to_json_text(&self.<sorted_key_2>) + ... +
        "}";
    }
}
```

**Field ordering: lex-utf8** (K answer 4). The output JSON object key order is the sorted byte-order of the field names. This:
- Matches the existing throw-side params build order from Slice 1.
- Survives field reordering refactors at the source level — JSON output is byte-identical.
- Cross-package consumers can compute expected output without consulting source declaration order.

**Empty-field error** (`pub error E {}`): synthesizes `to_json_text` returning the literal `"{}"`.

### 7.4 Non-projectable field diagnostic

If any field's type is not projectable AND the user hasn't provided a manual impl:

```
error[E0XYZ]: cannot synthesize Diagnostic for 'pub error MyError'
              because field 'bad_field' (type 'RawPtr<Byte>') is not projectable
  --> src/foo.drift:10:5
   |
10 |     bad_field: RawPtr<Byte>,
   |     ^^^^^^^^^^^^^^^^^^^^^^^
   = help: either implement 'Diagnostic for RawPtr<Byte>' (see std.core),
           or implement 'Diagnostic for MyError' manually,
           or change the field type to a projectable type.
```

The diagnostic is emitted at the `pub error` declaration site (not at use sites — fail-closed at definition).

### 7.5 Reverse projection (envelope → `pub error` for catch binding)

For `catch ParseError(e) { ... }`, `e` is materialized from the envelope's `params_json` by inverse projection.

**Slice 5 v1 scope (K-locked 2026-05-03):**

- **Synthesized projection ⇒ synthesized inverse.** When the compiler synthesizes `Diagnostic for E`, it ALSO synthesizes a private inverse parser (internally — not exposed as a user-implementable trait) that mirrors the synthesized projection. Field-by-field: parse the JSON object, look up each declared field name (lex-utf8 sort matches the projection ordering), parse its value via the field type's inverse parser. This makes `catch E(e)` typed-binding "just work" for the dominant product path.

- **Manual projection ⇒ NO synthesized inverse.** When the user provides an explicit `implement Diagnostic for E` override, the compiler does NOT auto-synthesize a reverse parser. Catch-binding of `E` with a typed binder is REJECTED with diagnostic `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED`:

  ```
  error[E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED]: typed catch binding for 'SecretError' requires synthesized Diagnostic projection
    --> src/foo.drift:42:9
     |
  42 |     catch SecretError(e) { ... }
     |           ^^^^^^^^^^^^
     = note: 'SecretError' has a manual 'implement Diagnostic for SecretError' impl
     = help: SecretError values can still be caught by event identity:
             catch SecretError { ... e.params.get("...") ... }
             with the binder removed.
             Manual reverse parsing (DiagnosticParse) is planned for a follow-up release.
  ```

- Manually-projected `pub error` types remain CATCHABLE BY EVENT IDENTITY (the `catch X { ... }` form WITHOUT the typed binder), with envelope access via `e.params.get(...)`. Only the typed-binder form is rejected.

- **No public `DiagnosticParse` trait in 0.32.0.** Users do NOT write reverse parsers in Slice 5. The `DiagnosticParse` concept is reserved for a future design track; the spec mentions it only as a planned follow-up so reviewers can see where the door is left.

**Why this scope cut:** the dominant product use case is `pub error E { ...primitive fields... }` with synthesized projection — that path gets full typed catch-binding for free. Custom-projection use cases (redaction, verbose dumps, std.json composition) tend to also want custom catch logic, which the user can write today via `catch E { e.params.get(...) }`. Punting manual reverse parsing to its own track keeps Slice 5 focused on the language model + DV removal.

### 7.6 Manual override

To override synthesis, the user writes their own `Diagnostic` impl in the same package as the declaration:

```drift
pub error SecretError {
    user_id: Int,
    secret_token: String,
}

implement Diagnostic for SecretError {
    pub fn to_json_text(self: &SecretError) nothrow -> String {
        // Redacted projection — secret_token never appears in logs/envelope.
        return "{\"user_id\":" + diagnostic_json_int(self.user_id) + "}";
    }
}

// NO DiagnosticParse impl needed in v1 — typed catch-binding is unavailable
// for SecretError because it has a manual projection. Catch by event identity:
//
//     try { ... } catch SecretError {
//         val uid = e.params.get("user_id").as_int().unwrap_or(0);
//         log("auth failed for user", uid);
//     }
//
// (The implicit 'e' binder in the catch arm is the opaque envelope handle,
// NOT a typed SecretError value. Use e.params.get(...) for field access.)
```

The compiler detects the explicit `Diagnostic` impl and skips synthesis. Per §7.5, typed-binder catch (`catch SecretError(e)`) is rejected with `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED`; only the no-binder form (`catch SecretError { ... }`) is accepted for manually-projected errors in v1.

---

## 8. `Debuggable` Trait

### 8.1 Definition (separate from `Diagnostic`)

```drift
pub trait Debuggable {
    fn to_debug_json_text(self: &Self) nothrow -> String;
}
```

Located in `std.log` (current location of `Debuggable`).

### 8.2 Why separate

- Different audiences: error diagnostics (for catchers/structured logging) vs debug logs (for human readers / dev consoles). May diverge on redaction, verbosity, formatting in the future.
- Different method names avoid dispatch ambiguity if a type implements both.
- Same JSON-text contract — the boundary is identical to `Diagnostic`'s.

### 8.3 Migration of stdlib `Debuggable` impls

Seven existing stdlib `Debuggable` impls (Int / Uint / Bool / Float / String / DiagnosticValue / etc.) migrate:
- Method name: `to_debug` → `to_debug_json_text`.
- Return type: `DiagnosticValue` → `String`.
- Body: returns canonical JSON text (same form as `Diagnostic` for primitives).
- The DV-specific impl (`Debuggable for DiagnosticValue`) is REMOVED — DV is no longer public.

### 8.4 Public log API impact

Existing log APIs accepting `HashMap<String, DiagnosticValue>` migrate to `HashMap<String, String>` where the values are JSON value text:

```drift
// Before:
log.warn("event", attrs = map_of([("key", DV::String(s))]))

// After:
log.warn("event", attrs = map_of([("key", diagnostic_json_string(&s))]))
```

Or — preferred new shape — `attrs: impl Debuggable`:
```drift
log.warn("event", attrs = my_value)  // my_value: impl Debuggable
```

Exact API redesign is a `std.log` decision; spec requires only that no public `std.log` API exposes `DiagnosticValue`.

---

## 9. `std.core` JSON-text Helpers

New public surface in `std.core` — **all six ship in 0.32.0** (K, 2026-05-03):

```drift
/// Quote a String as an RFC-8259 JSON string value.
/// Returns the String surrounded by double quotes with all
/// characters escaped per JSON rules. Result is a complete
/// JSON value ready to splice into a JSON object/array.
pub fn diagnostic_json_string(s: &String) nothrow -> String;

/// Returns the literal "null".
pub fn diagnostic_json_null() nothrow -> String;

/// Returns "true" or "false".
pub fn diagnostic_json_bool(v: Bool) nothrow -> String;

/// Format an Int as JSON number text (decimal, no quotes).
pub fn diagnostic_json_int(n: Int) nothrow -> String;

/// Format a Uint as JSON number text (decimal, no quotes).
pub fn diagnostic_json_uint(n: Uint) nothrow -> String;

/// Format a Float as JSON number text per format_float canonical form
/// (handles infinities/NaN by emitting valid JSON-tolerant
/// substitutes, e.g. very-large-magnitude finite literals or quoted
/// sentinels — exact non-finite handling is implementation-defined
/// but MUST produce parseable JSON).
pub fn diagnostic_json_float(f: Float) nothrow -> String;
```

**Implementation note:** `_json_quote_string` already exists privately in `std.core` (used by Slice 1's `_dv_to_json_text`). `diagnostic_json_string` is `_json_quote_string` promoted to public + renamed. The number helpers are thin wrappers over existing `format_int` / `format_uint` / `format_float` (JSON's number grammar accepts those forms unchanged); the wrappers exist so users don't import formatting modules just for diagnostic projection.

**No `diagnostic_json_field(key, value_json)` in v1.** Users compose objects via `String` concat or via `std.json`. A field-builder helper can be added later if patterns warrant.

---

## 10. Runtime Envelope (ABI 13)

### 10.1 `DriftError` C layout

```c
struct DriftError {
    drift_error_code_t code;          // u64; field 0 — matches event_code
    struct DriftString event_fqn;     // field 1 — fully-qualified type name
    struct DriftString params_json;   // field 2 — canonical JSON object text
    struct DriftString context_json;  // field 3 — canonical JSON array text
};
```

**Removed from ABI 12:** `attrs`, `attr_count`, `frames`, `frame_count` (4 fields, 32 bytes on x86_64).

**No `stack` field** — envelope continues to emit `"stack": null` as a literal in `e.encode_compact()`. Stack capture is a separate future track.

### 10.2 Envelope JSON shape (unchanged from Slice 3)

```json
{
  "event_code": 12345,
  "event_fqn": "my.pkg:ParseError",
  "params": {"message":"bad input","offset":12},
  "context": [{"fn":"my.pkg:parse_inner","locals":{...}}, ...],
  "stack": null
}
```

`params` and `context` are spliced from `params_json` and `context_json` as already-canonical JSON segments (no re-quoting, no parse-and-re-emit). Existing Slice 3 `Error.encode_compact()` lowering carries over unchanged.

### 10.3 ABI stamp

`DRIFT_RT_ABI_VERSION` 12 → **13**. Compiler emits ABI-13 stamp; runtime exports ABI-13 stamp; mismatched pairs fail at link time with the existing stamp-check mechanism. Slice 5 is a clean ABI break — ABI 12 consumers (currently 0.31.48 through 0.31.53) MUST rebuild.

---

## 11. `ResultError` Disposition (K answer 7, corrected 2026-05-03)

### 11.1 Demoted; prefer delete

`ResultError` is no longer the universal catchall. Primary user path is `or_throw()` throws concrete `pub error` types directly.

**Preferred outcome: delete entirely.** The implementation track audits stdlib during Slice 5; if no stdlib code requires a catchall after `or_throw()` migration to direct-type throw, `ResultError` is REMOVED. Users who want a generic catch use `catch *` (see §11.4).

### 11.2 If retained: NOT a normal `pub error` with `String` field

K's correction (2026-05-03): the naive shape `pub error ResultError { error_json: String }` is **wrong**. A normal `String` field would be projected via the synthesized `Diagnostic` impl, which calls `diagnostic_json_string(&self.error_json)` and wraps the value in JSON-string quotes. That double-quotes the carrier's JSON content, corrupting the envelope.

**Correct retention shape (if `ResultError` survives):**

```drift
@deprecated("use concrete pub error types via or_throw")
pub error ResultError {
    // Internal raw-JSON-splice carrier. NOT a normal field.
    // Compiler treats this field specially: its String value is a
    // canonical JSON value (object/array/scalar) and is spliced verbatim
    // into the envelope's params slot, NOT quoted via diagnostic_json_string.
    @raw_json_splice  // hypothetical attribute marking the special-case
    _error_json: String,
}
```

**Rules:**
- The raw-JSON-splice carrier field is INTERNAL to `std.err` — not user-nameable, not user-constructible. Mechanism: leading underscore + module-private constructor + the `@raw_json_splice` attribute (or whatever the existing internal-marker convention is) telling the synthesized projection to splice rather than quote.
- The compiler MUST recognize the splice attribute and skip `diagnostic_json_string` wrapping for that field. Spec-level rule: the synthesized projection for a `pub error` containing a `@raw_json_splice` field emits the field's value verbatim (after a runtime well-formedness assert if cheap; otherwise trust the constructor).
- Public constructors are sanctioned helpers ONLY:
  ```drift
  // Wrap an arbitrary Diagnostic value into a ResultError.
  pub fn ResultError::from_diagnostic(d: &impl Diagnostic) nothrow -> ResultError;
  ```
  (Or a free function `result_error_from(d: &impl Diagnostic)` if trait static methods aren't supported in v1.)
- NO public raw constructor — no user-facing `ResultError(_error_json = ...)`. The synthesized struct constructor for `ResultError` is hidden from user code (module-private declaration of the field, or existing `@internal`-equivalent mechanism).
- `_trusted` JSON constructors are NOT exposed publicly.

### 11.3 Implementation choice point

The implementation track decides between §11.1 (delete) and §11.2 (retain with raw-splice carrier) based on the stdlib audit during Slice 5. Spec authorizes either; recommendation is delete unless concrete stdlib need is found.

If §11.2 is taken, the `@raw_json_splice` attribute (or equivalent) is a NEW compiler feature added in Slice 5. It is not exposed to users — only `std.err:ResultError` may use it in 0.32.0. Future expansion of raw-splice to user types is out of scope.

### 11.4 Catchall still works via wildcard

`catch *` covers cases users would have used `catch ResultError(e)` for previously. Recommended migration:

```drift
// Before:
try { ... } catch ResultError(e) { log(e.attrs["message"].as_string().unwrap()); }

// After:
try { ... } catch * {
    // Use envelope accessors via the implicit binder:
    log(e.params.get("message").as_string().unwrap_or("(no message)"));
}
```

(Existing `catch *` already binds `e` to the envelope handle — no spec change needed for the binder mechanism.)

---

## 12. `std.log` Migration

In addition to the trait migration in §8:

- Public log functions (`log.info`, `log.warn`, `log.error`, etc.) that previously accepted `HashMap<String, DiagnosticValue>` migrate to `HashMap<String, String>` where values are JSON value text.
- Recommended new shape: `attrs: impl Debuggable` so users pass a typed value and the function calls `to_debug_json_text(&v)` internally. (Concrete API choice is `std.log`'s call.)
- All 7+ existing `Debuggable` impls migrate (see §8.3).
- The `Debuggable` impl for `DiagnosticValue` is DELETED.
- Any internal log helper that constructs DV literals migrates to JSON text or `Debuggable` calls.

### 12.1 Backward compatibility note

`std.log` API breakage is unavoidable. Migration documentation accompanies the 0.32.0 release. Downstream consumers using `log.X(attrs = ...)` rebuild against the new signature.

---

## 13. Removed Surfaces & Diagnostic Codes

### 13.1 Surfaces removed in 0.32.0

| Surface | Status | Replacement |
|---|---|---|
| `pub trait Diagnostic { fn to_diag(...) -> DiagnosticValue }` | METHOD CHANGED to `to_json_text -> String`; trait name preserved | §6 |
| `DiagnosticValue` (variant + builders) | REMOVED from public surface | use `Diagnostic.to_json_text` |
| `DiagnosticEntry` / `diagnostic_entry` helper | REMOVED | not replaced; build JSON with `std.core` helpers |
| `e.attrs[k]` indexer | REMOVED | `e.params.get(k).as_*()` |
| `e.captures[fr][k]` indexer | REMOVED | `e.context.encode_compact()` (Slice 5); cursor in 4B |
| `pub trait Debuggable { fn to_debug(...) -> DiagnosticValue }` | METHOD CHANGED to `to_debug_json_text -> String` | §8 |
| `DriftError` C-struct fields `attrs` / `frames` / counts | REMOVED (ABI 13) | `params_json` / `context_json` |
| Compiler MIR ops: `ConstructDV`, `ErrorAddAttrDV`, `ErrorAddLocalDV`, `ErrorAttrsGetDV`, `ErrorCapturesGetDV`, `DVAs*`, `DVKind`, `DVIndex`, `DVGetField`, `DVLen`, `DVEntries` | REMOVED | (none — DV path gone) |
| Runtime helpers: `drift_dv_*`, `drift_error_add_attr_dv`, `drift_error_add_local_dv`, `drift_error_get_attr`, `__exc_attrs_get*`, `__exc_captures_get_dv`, `drift_error_new_with_payload` | REMOVED | (none) |
| `_dv_to_json_text` transitional helper | REMOVED | Direct `Diagnostic.to_json_text` calls |
| `std.err:ResultError(dv: DiagnosticValue)` | DEMOTED — see §11 | concrete `pub error` types |

### 13.2 Diagnostic codes (proposed)

| Code | Trigger | Message shape |
|---|---|---|
| `E_DV_PUBLIC_REMOVED` | User code names `DiagnosticValue` / `DiagnosticEntry` / `diagnostic_entry` | "DiagnosticValue is removed in 0.32.0; use the JSON-text Diagnostic surface — see migration guide" |
| `E_EXC_ATTRS_REMOVED` | User code uses `e.attrs[k]` | "e.attrs[k] is removed; use e.params.get(k).as_*()" |
| `E_EXC_CAPTURES_REMOVED` | User code uses `e.captures[fr][k]` | "e.captures[fr][k] is removed; use e.context.encode_compact() or wait for the cursor surface" |
| `E_TO_DIAG_DEPRECATED` | User impl `to_diag(&self) -> DiagnosticValue` | "Diagnostic.to_diag is replaced by Diagnostic.to_json_text(&Self) -> String" |
| `E_TO_DEBUG_DEPRECATED` | User impl `to_debug(&self) -> DiagnosticValue` | "Debuggable.to_debug is replaced by Debuggable.to_debug_json_text(&Self) -> String" |
| `E_PUB_ERROR_FIELD_NOT_PROJECTABLE` | Synthesis fails on a non-projectable field | See §7.4 example |
| `E_PUB_EXCEPTION_DEPRECATED` (warning) | `pub exception` declaration | "'pub exception' is deprecated; use 'pub error' (will be removed in 0.33.0)" |
| `E_RESULT_ERR_NOT_ERROR_TYPE` | `Result<T, E>` Err type is not a `pub error` (Phase 5c; warning if staged, error if strict) | "Result<T, E> requires E to be a 'pub error' type; got '<type>'" |
| `E_OR_THROW_NOT_ERROR_TYPE` | `or_throw()` on `Result<T, E>` where E is not a `pub error` (Phase 5a — strict from day one) | "or_throw requires the Err type to be a 'pub error'" |
| `E_THROW_NOT_ERROR_TYPE` | `throw <expr>` where `expr`'s type is not a `pub error` | "cannot throw value of type '<T>'; only 'pub error' types are throwable" |
| `E_CATCH_NOT_ERROR_TYPE` | `catch X(e)` where `X` is not a `pub error` | "catch requires a 'pub error' type; got '<type>'" |
| `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED` | `catch E(e)` (typed binder) on a `pub error` with manual `Diagnostic` impl | See §7.5 — manual reverse parsing is a follow-up track; v1 supports binder-less `catch E { ... }` for these |
| `E_PRIVATE_ERROR_LEAKED_VIA_PUB` | private `error E` referenced in any exported signature (throws clause, Result Err, field, etc.) when enclosing decl is `pub` | See §2.3.1 — message includes public symbol name, private error name, leak position |

Codes are placeholders; final code allocation per the existing diagnostic-code registry.

---

## 14. Migration Guide (User-Facing)

### 14.1 Minimum-keystrokes migration

**Before** (0.31.x):
```drift
pub exception ParseError {
    message: String,
    offset: Int,
}

implement Diagnostic for ParseError {
    pub fn to_diag(self: &ParseError) nothrow -> DiagnosticValue {
        return DiagnosticValue::Object(map_of([
            ("message", DiagnosticValue::String(self.message)),
            ("offset", DiagnosticValue::Int(self.offset)),
        ]));
    }
}
```

**After** (0.32.0):
```drift
pub error ParseError {
    message: String,
    offset: Int,
}
// No Diagnostic impl needed — synthesized.
```

**Even simpler** (0.32.0 with `pub exception` transitional alias):
```drift
pub exception ParseError {  // warns: prefer pub error
    message: String,
    offset: Int,
}
// No Diagnostic impl needed — synthesized via the pub-error lowering.
```

### 14.2 Catch-side migration

**Before:**
```drift
try { ... } catch ParseError(e) {
    val msg = e.attrs["message"].as_string().unwrap_or("");
    val off = e.attrs["offset"].as_int().unwrap_or(0);
    log(msg, off);
}
```

**After:**
```drift
try { ... } catch ParseError(e) {
    log(e.message, e.offset);  // direct field access on bound value
}
```

(Or, if the catch arm needs the JSON view: `e.params.get("message").as_string()` continues to work — same access pattern as Slice 4A.)

### 14.3 Custom redaction

**Before:** custom `to_diag` returning a redacted DV object.

**After:** custom `to_json_text` returning a redacted JSON object (see §7.6 example). Note v1 limitation: a `pub error` with a manual `Diagnostic` impl loses typed catch-binding — `catch SecretError(e)` with field access is rejected with `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED`. Use the binder-less form `catch SecretError { e.params.get("...") ... }` instead. Manual reverse parsing (`DiagnosticParse`) is a planned follow-up.

### 14.4 Universal `catch ResultError(e)`

**Before:**
```drift
try { ... } catch ResultError(e) { handle_any(e); }
```

**After (recommended):** migrate to per-error-type catches:
```drift
try { ... }
catch ParseError(e) { ... }
catch CodecError(e) { ... }
catch * { ... }  // fallback
```

**Or** keep using `ResultError` if it survives migration (§11.2):
```drift
try { ... } catch ResultError(e) {
    log(e.error_json);  // raw JSON value text
}
```

---

## 15. Sequencing (Implementation Order)

Per K's confirmed top-down spec-first:

1. **Spec lock** (this document, after K review).
2. **Failing positive tests** for the new public model:
   - `pub error E { ... }` declaration parses + type-checks.
   - `Result<T, ParseError>` with `or_throw()` throws `ParseError` directly.
   - `catch ParseError(e)` binds with field access.
   - Synthesized JSON projection produces lex-utf8 sorted output.
   - Manual `Diagnostic` override skips synthesis.
   - `std.core` JSON-text helpers escape correctly.
3. **Failing negative tests** for removed public DV surfaces:
   - `DiagnosticValue::Int(...)` from user code → `E_DV_PUBLIC_REMOVED`.
   - `DiagnosticEntry` / `diagnostic_entry` from user code → `E_DV_PUBLIC_REMOVED`.
   - User `to_diag(...) -> DiagnosticValue` impl → `E_TO_DIAG_DEPRECATED`.
   - `e.attrs[...]` → `E_EXC_ATTRS_REMOVED`.
   - `e.captures[...]` → `E_EXC_CAPTURES_REMOVED`.
   - Non-projectable field → `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.
   - `Result<T, NonError>` `or_throw` → `E_OR_THROW_NOT_ERROR_TYPE`.
   - `pub exception E { ... }` declaration → `E_PUB_EXCEPTION_DEPRECATED` warning.
4. **Trait + stdlib migration:**
   - `Diagnostic` trait shape change.
   - `Debuggable` trait shape change.
   - 13+ stdlib `Diagnostic` impls migrated.
   - 7 stdlib `Debuggable` impls migrated.
   - `std.core` JSON-text helpers added.
   - Stdlib `pub exception` declarations rewritten as `pub error`.
   - `std.err:ResultError` migrated per §11.
   - `std.log` API surface migrated per §12.
5. **Compiler grammar + checker:**
   - `pub error` parsing.
   - `pub exception` deprecation alias.
   - Synthesized `Diagnostic` projection generator (forward direction).
   - Synthesized internal reverse parser for typed catch-binding on synthesized `pub error` types (private; not a user-implementable trait — see §7.5).
   - Typed-catch-binding rejection diagnostic for manually-projected `pub error` types (`E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED`).
   - `Result<T, E: pub error>` constraint enforcement (Phase 5a strict on `or_throw`; Phase 5c warning at non-error positions if practical, error in 0.33.0 — see §3.2).
   - Type-checker rejection diagnostics (§13.2).
   - Per-package `event_code` duplicate detection (when auto-assigned codes collide via the xxHash64 scheme — same surface that catches explicit-code duplicates).
   - If `ResultError` is retained: `@raw_json_splice` (or equivalent) compiler attribute; restricted to `std.err` use only.
6. **Compiler lowering:**
   - HIR→MIR rewrite of throw-side params projection: replace `_dv_to_json_text` chain with direct `Diagnostic.to_json_text(&field)` calls.
   - `_emit_captured_locals` rewrite (same).
   - `e.attrs[k]` / `e.captures[fr][k]` HIndex special-case removal.
   - DV intrinsic dispatch removal.
   - DV MIR op removal (cascading codegen / string_arc / dispatch).
7. **Runtime:**
   - `DriftError` ABI 13 layout (drop legacy DV fields).
   - DV runtime helper deletion.
   - ABI stamp bump 12 → 13.
8. **Cleanup:**
   - Codegen LLVM declaration cleanup.
   - `std.core` exports cleanup (drop `DiagnosticEntry`, `diagnostic_entry`).
   - Test migration — rewrite ~25-30 files, delete obsolete e2e Drift sources, add negative tests.
   - Doc update: `drift-lang-spec.md`, `drift-lang-abi.md`, `dmir-spec.md`, `effective-drift.md`, `articles/drift_vs_rust_error_handling.md`, `articles/drift-compiler-architecture.md`.
   - `history.md` 2026-05-XX entry.
   - Memory: `project_dv_to_json_diagnostics.md` final entry; close out the migration project memory.

Compiler version: 0.31.53 → **0.32.0** at the end of step 7.

---

## 16. Risk List & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Catch-bind reverse projection (§7.5) | Resolved (scope-cut) | Synthesized-only typed binder in v1; manual projections use binder-less `catch E { ... }` with `e.params.get(...)`. Diagnostic `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED` makes the limitation explicit. Manual `DiagnosticParse` deferred to a follow-up track. |
| Stdlib migration is large (~13+7+stdlib internals) | Medium | Mechanical edits; bulk via review. Per-module testable. |
| `Result<T, E>` strict enforcement churn | Medium | §3.2 staging clause permits `or_throw`-only enforcement in 0.32.0, full strict in 0.33.0. |
| `pub exception` warning suppression | Low | Use existing diagnostic-suppression mechanism if needed; otherwise rely on the warning being eventually fixable. |
| Auto-assigned event_code collision | Low | xxHash64 collision space is large (60-bit payload after domain tag). Existing catalog already detects payload collisions and emits a diagnostic recommending explicit form. |
| Downstream package rebuild cost | High (operationally) | ABI 13 break is unavoidable for Slice 5's removal scope. Coordinated with downstream consumers (drift-web, net.tls, MariaDB, etc.) per the existing compiler-bump protocol. |
| `_json_quote_string` private→public name collision | Low | Already private; rename to `diagnostic_json_string` and re-export. Internal callers update. |

---

## 17. Answers Locked (K, 2026-05-03)

The original v0 draft posed 7 open questions; K answered all of them. Recorded here for reference:

1. **Collection projectability:** `Optional<U>`, `Array<U>`, and `Map<String, V>` are auto-projectable when contained values are projectable. `Map<K, V>` with `K != String` is rejected. Pointer / function / opaque types are NOT auto-projectable. — see §7.2.
2. **`DiagnosticParse`:** NOT a public trait in 0.32.0. Synthesized inverse parsing happens internally for synthesized projections; manual projections lack typed catch-binding (binder-less `catch E { ... }` only). Manual reverse parsing is a follow-up design track. — see §7.5.
3. **Catch-bind on manually-projected `pub error`:** scope-cut to synthesized-only in v1. Diagnostic `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED` for the typed-binder form on manually-projected errors. — see §7.5.
4. **`event_code` algorithm:** reuse the existing exception event-code scheme — verified at `lang/driftc/core/event_codes.py` to be xxHash64 of the FQN with a 4-bit domain tag in the high bits. Per-package duplicate detection already in place via the existing payload-collision check. — see §2.4.
5. **`pub exception` warning suppression:** leave to existing diagnostic system; no new pragma in this slice. — see §2.1.
6. **`std.core` JSON helpers:** all six ship in 0.32.0 (`diagnostic_json_string`, `diagnostic_json_null`, `diagnostic_json_bool`, `diagnostic_json_int`, `diagnostic_json_uint`, `diagnostic_json_float`). — see §9.
7. **Empty payload:** synthesized projection for `pub error E {}` returns `"{}"`. Envelope's `params` is ALWAYS a JSON object, never `null`, never omitted. — see §7.3.

**Plus K's structural correction (2026-05-03):**
- `ResultError` retention shape uses an internal `@raw_json_splice` carrier field (not a normal `String` field, which would get quoted by synthesized projection). Public API is `from_diagnostic`-style helpers only. Prefer delete entirely if stdlib audit allows. — see §11.

---

## 18. Success Criteria

Slice 5 is done when:

1. **Public API:** no occurrence of `DiagnosticValue`, `DiagnosticEntry`, `to_diag`, `to_debug`, `e.attrs[`, or `e.captures[` in any `pub` symbol in stdlib or in user-facing docs/articles.
2. **`pub error`** is the canonical user form; `pub exception` is a deprecated alias warning at every declaration site.
3. **`Result<T, E: pub error>.or_throw()`** throws `E` directly; round-trips through `catch E(e)` with field access.
4. **Synthesized `Diagnostic`** for `pub error` types fires by default; manual override works; non-projectable-field diagnostic fires correctly.
5. **`std.core`** exports `diagnostic_json_string` / `diagnostic_json_null` / `diagnostic_json_bool`; users have a sanctioned escape path.
6. **`std.log`** has no `DiagnosticValue` in its public surface; `Debuggable` returns JSON text via `to_debug_json_text`.
7. **`DriftError` ABI 13** drops legacy DV fields; ABI stamp checks pass.
8. **Negative tests** pass for every removed surface in §13.2.
9. **Positive tests** pass for: `pub error` declaration, throw, catch-by-type, synthesized JSON, manual override, `Result.or_throw`, `std.core` helper escape correctness.
10. **Full regression matrix** clean: stage2, driver, codegen e2e, memcheck, package roundtrip, ASan.
11. **Compiler version 0.32.0**, ABI 13 stamped.
12. **Docs** updated: `drift-lang-spec.md`, `drift-lang-abi.md`, `dmir-spec.md`, `effective-drift.md`, articles, `history.md` 2026-05-XX entry.
13. **Memory** `project_dv_to_json_diagnostics.md` final entry recorded; migration project closed.

---

## 19. Bridge to Step 2 — Test Drafting

Spec is locked (K, 2026-05-03). Next deliverable is the **failing-test set** for §15 step 2 + step 3 — positive tests for the new public model + negative tests for removed/restricted surfaces.

**Proposed test layout** (under `lang/tests/driver/`):

- `test_pub_error_decl.py` — declaration parses, type-checks, value type semantics (construct, copy where applicable, pass by value).
- `test_pub_error_throw_catch.py` — `throw E(...)` + `catch E(e)` typed binder + field access; precise routing by event identity.
- `test_pub_error_or_throw.py` — `Result<T, E: pub error>.or_throw()` throws `E` directly; `E_OR_THROW_NOT_ERROR_TYPE` for non-error Errs.
- `test_pub_error_synthesized_diagnostic.py` — synthesized projection produces lex-utf8 sorted JSON; primitives + Optional + Array + Map<String,V>; empty-payload `"{}"`.
- `test_pub_error_manual_diagnostic.py` — manual `Diagnostic` override skips synthesis; binder-less catch works; typed-binder catch fails with `E_TYPED_CATCH_BIND_REQUIRES_SYNTHESIZED`.
- `test_pub_error_non_projectable_field.py` — `RawPtr<T>` / `Map<Int, V>` / function-type fields → `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.
- `test_pub_exception_deprecated.py` — `pub exception` declaration emits `W_PUB_EXCEPTION_DEPRECATED` warning; still compiles; round-trips through throw/catch.
- `test_diagnostic_json_helpers.py` — all six `std.core` helpers produce correct RFC-8259 output (escapes, edge cases: empty string, surrogate pairs, large numbers, NaN/Inf for float).
- `test_debuggable_migration.py` — `to_debug_json_text` for primitives + user types; `to_debug` rejected.
- `test_exception_envelope_pub_error.py` — `e.encode_compact()` over a thrown `pub error` matches the expected envelope shape (event_code, event_fqn, params, context, stack=null).
- `test_dv_public_removed.py` — negative tests: `DiagnosticValue::Int(...)`, `DiagnosticEntry`, `e.attrs[...]`, `e.captures[...]`, `to_diag(...) -> DiagnosticValue` user impl all rejected with the corresponding diagnostics from §13.2.
- `test_event_code_collision.py` — two `pub error` types in same package with colliding auto-assigned codes → diagnostic recommending explicit assignment.

**Test-drafting plan:**
1. Write tests as **strict-xfail** (or use the same `_PENDING` decorator pattern from Slices 1-3) since the implementation is not yet in place.
2. Land tests as a separate commit BEFORE any implementation, so the spec→test linkage is auditable in git.
3. Implementation slices flip xfail decorators to live as each phase lands.

**No code changes to the live tree** until K confirms the test layout. Once layout is OK, I draft `test_pub_error_decl.py` first (the most foundational), get review on its shape, then mass-produce the rest using the same pattern.

---

## 20. Implementation Slice 1 — Landed 2026-05-03

**Scope (per K):** parser/checker shape for `pub error` + `pub exception` brace alias + `std.core` JSON-text helper surface. Defers Diagnostic trait shape change, synthesis machinery, throw/catch routing for `pub error`, `or_throw` strict enforcement, deprecation warning, DV deletion, and ABI 13.

### 20.1 What landed

**Grammar** (`lang/driftc/parser/grammar.lark`):
- New `ERROR.2: /error\b/` token.
- New `error_def` rule: `ERROR NAME ( "(" INT ")" )? block_struct TERMINATOR?` — supports `pub error E { ... }` and `pub error E(0x1234) { ... }`.
- `exception_def` extended to accept brace body: `EXCEPTION NAME ( "(" [exception_params] ")" | block_struct ) TERMINATOR?`.
- `error_def` added to `?item:` and `pub_item:` alternatives.
- `ERROR` added to contextual-keyword positions (`ident`, `attr_suffix`, `leading_dot`) so existing identifiers like `Logger.error` keep working.

**AST** (`lang/driftc/parser/ast.py`):
- `ExceptionDef` extended with `kind: str = "exception"` and `explicit_event_code: Optional[int] = None`.

**Parser** (`lang/driftc/parser/parser.py`):
- New `_build_error_def` extracts NAME + optional INT event_code + brace body, produces `ExceptionDef(kind="error")`.
- `_build_exception_def` extended: brace-body form produces `ExceptionDef(kind="error")` (transitional alias); paren form keeps `kind="exception"` (legacy).
- Top-level dispatch: for any `kind="error"` decl (whether from `pub error` or brace `pub exception`), the parser also produces a parallel `StructDef` (Path A — value-type machinery via existing struct path).
- New helper `_struct_from_error_decl` constructs the parallel StructDef from the ExceptionDef args.
- `_unwrap_ident` accepts `ERROR` token alongside `NAME/MOVE/COPY/SHARE`.

**Catalog** (`lang/driftc/parser/__init__.py`):
- `_build_exception_catalog` honors `explicit_event_code` when set; otherwise uses the existing xxHash64-based scheme.
- Existing payload-collision check covers both auto-assigned and explicit duplicates.

**stdlib** (`stdlib/std/core/core.drift`):
- New public helpers: `diagnostic_json_string` (promoted from existing private `_json_quote_string`), `diagnostic_json_null`, `diagnostic_json_bool`, `diagnostic_json_int`, `diagnostic_json_uint`, `diagnostic_json_float`. All `nothrow -> String`. All exported.

### 20.2 Tests flipped (12 of 42)

| File | Probes flipped | Probes still xfailed |
|---|---|---|
| `test_pub_error_decl.py` | 7 (all) | — |
| `test_pub_exception_deprecated.py` | 1 (alias parses) | 2 (throws-with-error-type syntax + warning plumbing — slice 2) |
| `test_event_code_collision.py` | 1 (distinct codes positive control) | 1 (explicit duplicate rejection — diagnostic-code rename deferred) |
| `test_diagnostic_json_helpers.py` | 3 (all) | — |

### 20.3 Tests staying xfailed (30 of 42)

All probes in:
- `test_pub_error_throw_catch.py` (throws-with-error-type syntax + typed catch routing — slice 2).
- `test_pub_error_or_throw.py` (or_throw strict enforcement — slice 2).
- `test_pub_error_synthesized_diagnostic.py` (synthesis machinery — slice 2+).
- `test_pub_error_manual_diagnostic.py` (Diagnostic trait shape change — slice 2+).
- `test_pub_error_non_projectable_field.py` (synthesis projectability check — slice 2+).
- `test_debuggable_migration.py` (Debuggable trait shape change — slice 2+).
- `test_exception_envelope_pub_error.py` (throw/catch over `pub error` + envelope accessors — slice 2+).
- `test_dv_public_removed.py` (DV deletion — slice ABI 13).

### 20.4 Path A pinned

`pub error E { ... }` (and brace-form `pub exception E { ... }`) lower to BOTH:
- A `StructDef` with the same name and fields (value-type machinery — constructor, field access, pass-by-value, Copy/Frozen/ConstShare composition via existing struct path).
- An `ExceptionDef` with `kind="error"` (event-identity catalog registration).

Paren-form `pub exception E(...)` keeps legacy throw-only semantics: `kind="exception"`, no struct co-registration.

### 20.5 What slice 1 deliberately did NOT touch

- `Diagnostic` trait shape (still `to_diag → DiagnosticValue`).
- Stdlib `Diagnostic` impls (none migrated).
- `Debuggable` trait shape.
- `throw E()` routing for `pub error` types (existing exception path in stage1+ doesn't yet recognize `kind="error"`).
- `catch E(e)` typed binder routing for `pub error`.
- `Result<T, E>.or_throw()` strict enforcement.
- `pub exception` deprecation warning (would emit on every existing stdlib decl — deferred).
- `pub error` synthesis (the Diagnostic auto-impl).
- DV public-surface deletion.
- ABI 13.
- Visibility coherence rule (§2.3.1 — checker pass deferred).

### 20.6 Files touched

```
lang/versions.py                                 (DRIFTC_VERSION 0.31.53 → 0.31.54)
lang/driftc/parser/grammar.lark
lang/driftc/parser/parser.py
lang/driftc/parser/ast.py
lang/driftc/parser/__init__.py
stdlib/std/core/core.drift
lang/tests/driver/test_pub_error_decl.py            (7 decorators flipped)
lang/tests/driver/test_pub_exception_deprecated.py  (1 decorator flipped)
lang/tests/driver/test_event_code_collision.py      (1 decorator flipped + FNV→xxHash64 docstring correction)
lang/tests/driver/test_diagnostic_json_helpers.py   (3 decorators flipped + 42u/0u/4294967295u Uint literal corrections)
work/exception-diagnostics-context/slice5-spec.md   (this section + xxHash64 correction + §2.3.1 visibility coherence)
```

**Version bump:** DRIFTC_VERSION 0.31.53 → **0.31.54** (per repo rule: behavior-changing compiler/toolchain change without ABI break still bumps compiler version). ABI 12 unchanged. No ABI stamp update needed.
