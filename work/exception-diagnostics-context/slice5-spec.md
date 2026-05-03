# Slice 5 Spec — `pub error` and the JSON-text Diagnostic Surface

**Status:** DRAFT v0 — 2026-05-03. Spec-first per K's confirmed sequencing. Awaiting K's review/lock before any code, test, or stdlib edits begin.

**Companion docs:**
- `work/exception-diagnostics-context/slice5-preflight.md` — inventory, decision history, K sign-off (§12).
- `memory/project_dv_to_json_diagnostics.md` — slice-by-slice landed history through 0.31.53.

**Target release:** 0.32.0 (compiler), ABI 13 (runtime).

---

## 0. Decisions Locked (K, 2026-05-03)

This spec treats the following as **confirmed**, not open:

1. **`pub exception` is a transitional alias for `pub error`** — compiles for one release; deprecation diagnostic if practical; canonical docs/spec use `pub error`. Hard-error deferred to 0.33.0.
2. **`Result<T, E: error>` is the long-term strict rule.** Spec defines strict; implementation MAY stage through `or_throw()`-only enforcement first if full strict enforcement risks too much compiler churn. Strict is the target end-state.
3. **`pub error` is a distinct language kind**, not desugar to `struct + implement Error`. Internally it MAY lower to existing exception metadata, but semantically it is the datatype category valid for `Result` Err, throw, catch, event identity, and synthesized diagnostics.
4. **Synthesis field ordering: lex-utf8.** Independent of source declaration order; survives field reordering refactors.
5. **Synthesis succeeds when every field is `Diagnostic`/JSON-text-projectable.** Built-in primitives and `pub error` types are projectable by default. Non-projectable field → targeted diagnostic.
6. **Catch-by-supertype / marker is explicitly out of Slice 5.** Typed catch by concrete error identity only.
7. **`ResultError` is demoted, not centered.** If retained, deprecated `pub error` with JSON-text payload, hidden behind `from_diagnostic` / `or_throw`-style APIs. No `DiagnosticValue`. No raw/`_trusted` constructors. If unneeded after the migration, delete or mark legacy.

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

### 2.4 Event code and event_fqn

- `event_fqn` is the fully-qualified type name in the format `<package>:<TypeName>` (e.g., `my.pkg:ParseError`). Generated at declaration time; stable per release of the declaring package.
- `event_code` is a u64 stable routing identifier:
  - **Explicit form:** `pub error CodecError(0x4543) { ... }` — user pins the code.
  - **Auto-assigned form:** when omitted, the compiler hashes `event_fqn` (FNV-1a 64-bit or equivalent existing scheme) and uses the result. The spec pins the algorithm so cross-package consumers can compute the same code.
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

### 3.2 Implementation staging clause (K answer 2)

If full strict enforcement creates excessive compiler churn (deep stdlib, downstream package rebuilds, generic-bounds machinery edits), the implementation MAY stage in this order:

1. **Phase 5a (must land in 0.32.0):** `or_throw()` strictly requires `E: pub error`. Error message points to the migration.
2. **Phase 5b (must land in 0.32.0):** All STDLIB uses of `Result<T, E>` migrate to `pub error` Errs. (Stdlib is the easiest blast-radius bound — must be clean.)
3. **Phase 5c (may land in 0.32.0 or 0.33.0):** Strict global enforcement on `Result<T, E>` constructions and signatures. If deferred to 0.33.0, the 0.32.0 release ships a `W_RESULT_ERR_NOT_ERROR_TYPE` warning at non-error Err positions; 0.33.0 promotes it to error.

The spec end-state is strict; the staging is an implementation kindness, not a spec relaxation.

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

`e` binds the materialized `ParseError` value with full field access. The compiler reconstructs the bound `pub error` value from the runtime envelope; field types must be reconstructible from the envelope's `params_json` (this is automatic for synthesized projections; manual `to_json_text` impls require a parallel `from_json` story — see §7.5 for the rule).

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

For `catch ParseError(e) { ... }`, `e` is materialized from the envelope's `params_json` by inverse projection:

- For synthesized `Diagnostic`: the compiler ALSO synthesizes a `from_json_text(s: &String) nothrow -> Optional<E>` parser at the declaration site, mirroring the synthesized projection. Field-by-field: parse the JSON object, look up each declared field name, parse its value via the field type's inverse parser.
- For manual `Diagnostic`: the user MUST also provide a manual reverse parser if they want to use the type with `catch E(e)`. Spec-level rule:
  > A user-overridden `Diagnostic` impl for a `pub error` type MUST be paired with a user-overridden inverse parser implementing trait `DiagnosticParse`:
  > ```drift
  > pub trait DiagnosticParse {
  >     fn from_json_text(s: &String) nothrow -> Optional<Self>;
  > }
  > ```
  > Lacking it, `catch E(e)` over a manually-projected `pub error` is a compile error pointing at the missing parser.

This is an additive contract — synthesized projections come with a synthesized inverse for free; manual projections opt into providing both halves.

**Open implementation question:** v1 may simplify by NOT supporting catch-bind on manually-projected `pub error` types, requiring users to use `catch * { e.params.get(...).as_*() }` access for those cases. This is a reasonable initial scope cut. The spec leaves it to the implementer; whichever choice ships must be accompanied by a clear diagnostic.

### 7.6 Manual override

To override synthesis, the user writes their own impl in the same package as the declaration:

```drift
pub error SecretError {
    user_id: Int,
    secret_token: String,
}

implement Diagnostic for SecretError {
    pub fn to_json_text(self: &SecretError) nothrow -> String {
        // Redacted projection — secret_token never appears.
        return "{\"user_id\":" + format_int(self.user_id) + "}";
    }
}

implement DiagnosticParse for SecretError {
    pub fn from_json_text(s: &String) nothrow -> Optional<SecretError> {
        // Parser that reconstructs SecretError from redacted form,
        // filling secret_token with empty string.
        ...
    }
}
```

The compiler detects the explicit `Diagnostic` impl and skips synthesis.

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

New public surface in `std.core`:

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
```

**Optional follow-on helpers** (may land in Slice 5 or a follow-up; the three above are the minimum):

```drift
/// Format an Int as JSON number.
pub fn diagnostic_json_int(n: Int) nothrow -> String;

/// Format a Uint as JSON number.
pub fn diagnostic_json_uint(n: Uint) nothrow -> String;

/// Format a Float as JSON number per format_float canonical form.
pub fn diagnostic_json_float(f: Float) nothrow -> String;
```

(These three are mostly conveniences over existing `format_int` / `format_uint` / `format_float` since JSON's number grammar accepts those forms unchanged. Not strictly required, but reduce user error.)

**Implementation note:** `_json_quote_string` already exists privately in `std.core` (used by Slice 1's `_dv_to_json_text`). `diagnostic_json_string` is `_json_quote_string` promoted to public + renamed.

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

## 11. `ResultError` Disposition (K answer 7)

### 11.1 Demoted

`ResultError` is no longer the universal catchall. Primary user path is `or_throw()` throws concrete `pub error` types directly.

### 11.2 If retained: deprecated `pub error` shape

If `ResultError` is needed for compatibility (e.g., generic adapters wrapping arbitrary `Diagnostic` values without a specific `pub error`), the form is:

```drift
@deprecated("use concrete pub error types via or_throw")
pub error ResultError {
    error_json: String,  // JSON value text — NOT a quoted string of the error
}
```

**Rules:**
- `error_json` semantics: the value is a JSON value (object / array / scalar) per the projected error's `to_json_text` output. NOT a quoted string of it.
- Spliced verbatim into `params.error_json` of the envelope (no double-quoting).
- Constructed only via sanctioned helpers:
  ```drift
  pub fn ResultError::from_diagnostic(d: &impl Diagnostic) nothrow -> ResultError;
  ```
  (or a free function `result_error_from(d: &impl Diagnostic)` if trait static methods aren't supported in v1).
- NO public raw constructor (no `ResultError(error_json = ...)` user form). The synthesized struct constructor is hidden from user code via existing `@internal` mechanism, OR the field is named with a leading underscore convention (`_error_json`) and the constructor is private to `std.err`.
- `_trusted` JSON constructors are NOT exposed.

### 11.3 If unneeded: deleted

If after the migration no stdlib code requires the catchall, `ResultError` is REMOVED entirely. This is preferred. The spec authorizes either outcome; the implementation track decides based on stdlib audit during Slice 5 implementation.

### 11.4 Catchall still works via wildcard

`catch *` (and `catch * as e`, if added) covers cases users would have used `catch ResultError(e)` for previously. The recommended migration:

```drift
// Before:
try { ... } catch ResultError(e) { log(e.attrs["message"].as_string().unwrap()); }

// After:
try { ... } catch * {
    // Use envelope accessors:
    log(e.params.get("message").as_string().unwrap_or("(no message)"));
}
```

(Assumes `catch * as e` is supported; if not, the catch arm has access to the implicit `e` via a future binder mechanism. In v1 this may require explicit naming syntax — out of scope for this spec.)

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
| `E_DIAG_PARSE_MISSING` | `catch E(e)` over a `pub error` with manual `Diagnostic` but no manual `DiagnosticParse` | See §7.5 |

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

**After:** custom `to_json_text` returning a redacted JSON object (see §7.6 example). Add the matching `DiagnosticParse` impl if `catch SecretError(e)` is used.

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
   - Synthesized `Diagnostic` + `DiagnosticParse` impl generators.
   - `Result<T, E: pub error>` constraint enforcement (Phase 5a strict on `or_throw`; Phase 5c global per §3.2).
   - Type-checker rejection diagnostics (§13.2).
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
| Catch-bind reverse projection (§7.5) | High | v1 may scope-cut to "synthesized projections only get catch-bind"; manual projections require `catch * { e.params.get(...) }` access. Diagnostic `E_DIAG_PARSE_MISSING` makes the limitation explicit. |
| Stdlib migration is large (~13+7+stdlib internals) | Medium | Mechanical edits; bulk via review. Per-module testable. |
| `Result<T, E>` strict enforcement churn | Medium | §3.2 staging clause permits `or_throw`-only enforcement in 0.32.0, full strict in 0.33.0. |
| `pub exception` warning suppression | Low | Use existing diagnostic-suppression mechanism if needed; otherwise rely on the warning being eventually fixable. |
| Auto-assigned event_code collision | Low | FNV-1a 64-bit collision space is large. Compiler MAY emit a warning if two `pub error` types in the same package hash to the same code (and direct user to explicit form). |
| Downstream package rebuild cost | High (operationally) | ABI 13 break is unavoidable for Slice 5's removal scope. Coordinated with downstream consumers (drift-web, net.tls, MariaDB, etc.) per the existing compiler-bump protocol. |
| `_json_quote_string` private→public name collision | Low | Already private; rename to `diagnostic_json_string` and re-export. Internal callers update. |

---

## 17. Open Spec Questions (for K's review)

These are spec-level decisions I want K to confirm before drafting tests:

1. **Field projectability for collection types** (§7.2): I specified `Optional<U>` and `Array<U>` recursively projectable. Should `Map<String, V>` be similarly auto-projectable? (Recommend yes, for `V: Diagnostic`.) Other collection types?

2. **`DiagnosticParse` trait existence** (§7.5): is the right shape a separate `DiagnosticParse` trait, OR should it be a second method on `Diagnostic` itself (`fn from_json_text(s: &String) nothrow -> Optional<Self>`)? Two-method `Diagnostic` is simpler but couples projection and parsing more tightly. (Recommend separate trait so projection-only types — non-error `Diagnostic` impls used purely for log/trace/etc. — don't need to define a parser.)

3. **Catch-bind on manually-projected `pub error`** (§7.5 closing note): scope-cut to synthesized-only in v1, OR require `DiagnosticParse` for catch-bind? (Recommend scope-cut for simplicity; document as planned follow-up.)

4. **`event_code` auto-assign algorithm** (§2.4): FNV-1a 64 vs xxHash64 vs SHA256-truncated? FNV-1a is simplest; xxHash has lower collision risk. (Recommend FNV-1a 64 for simplicity unless an existing codebase part already uses xxHash.)

5. **`pub exception` deprecation warning suppression** (§2.1): should the spec mandate a suppression pragma, or leave it to the existing diagnostic system? (Recommend leave to existing; if migration scale demands suppression, add as a follow-up.)

6. **`std.core` JSON-text helper minimum set** (§9): the three (`diagnostic_json_string`, `diagnostic_json_null`, `diagnostic_json_bool`) — are the int/uint/float helpers worth including in 0.32.0 too, or defer? (Recommend include all six for symmetry; cost is minor.)

7. **`pub error` empty-payload form** (§2.1 last example, §7.3): synthesizes `to_json_text` returning literal `"{}"`. Confirm this is the right shape vs `null` or omitting `params` from envelope. (Recommend `"{}"` for consistency with non-empty.)

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

## 19. What I Need From K Before Drafting Tests

1. **Spec lock:** confirm or revise §1–§16 above. Spec is the contract subsequent tests are written against; revisions after tests land waste work.
2. **Answers to §17 open questions** (7 spec-level details; recommendations inline).
3. **Implementation staging preference for §3.2:** strict-on-`or_throw` only in 0.32.0 (Phase 5a) and defer global strict to 0.33.0 (Phase 5c)? Or push for global strict in 0.32.0?
4. **Catch-bind scope cut for §7.5:** synthesized-only in v1 (recommended), or require `DiagnosticParse` for any catch-bind?

After K's response on (1)–(4), I move to step 2 of the sequencing: draft failing positive tests.

No code changes to the live tree until K confirms.
