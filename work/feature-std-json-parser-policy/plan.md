# Slice 2 — `std.json` Parser Policy + Located Decoder Surface

**Status:** LANDED (as-built) — implemented and verified after several static-review rounds.
**Module:** `std.json` (`stdlib/std/json/json.drift`), additive (+ `HashMap.insert_if_absent` in `std.containers`).
**Target:** driftc **0.33.28** (patch); **ABI 16 unchanged** (pure-Drift stdlib).

> This plan was the design record; the implementation matches it except for the
> deviations noted inline (e.g. `require` → `require_field` — reserved keyword;
> public-but-opaque span types; RFC string rules + `unescaped-control` added per
> review). See `history.md` (0.33.28) for the as-built summary and the test list.

> Two deliverables, kept distinct:
> 1. **Parser policy** — orthogonal policies that select **standard JSON or a
>    stricter subset** (never a superset, never a value reinterpretation — with
>    one documented exception: duplicate-key resolution, §3).
> 2. **Located decoder surface** — a source-location-preserving document/cursor
>    on which the **object-contract helpers** (required fields, unknown-field
>    rejection, string discriminators, path+location-aware type mismatches) are
>    built. These are part of Slice 2, not deferred.
>
> Canonical *encoding* is a third, separate concern with its own fixed contract
> and its **own** result-propagating implementation (§11). Value-semantic
> decoding that users layer on top (ISO dates, flexible booleans, numeric
> ranges, domain rules) stays out (§12).

**Revision note (static review):** object-contract helpers are in-scope via a new
located surface (§F1); `allow_leading_zeros` is removed from the public surface —
leading-zero lenience survives only through legacy `parse()` via a *private*
compatibility profile (§F3); duplicate-key resolution is documented as the sole
interpretation policy (§F2); `encode_canonical` is a fresh result-propagating
encoder with stable error tags, **not** the existing `"{}"`-swallowing path (§F4);
`signed_ir()` carries no bundled limits or top-level restriction (§F5). Resolved
Q1–Q8 folded in (§15).

---

## 0. Scope

**In scope:**
- `JsonParseConfig` + orthogonal policies: duplicate-key, top-level, numeric
  (fractions / exponents / negative-zero), optional limits.
- `JsonParseConfigBuilder` (shipped this slice — Q4).
- Built-in profiles: `permissive`, `strict`, `signed_ir` (all select standard
  JSON or a subset); one **private** legacy profile for `parse()`.
- `parse_with_config` / `parse_strict`; `parse()` unchanged externally.
- **Located decoder surface**: `parse_located` → `JsonDoc` → `LocatedCursor`, and
  the object-contract helpers (required / unknown / discriminator / typed
  getters) producing path+location-aware `JsonErrorData`.
- `encode_canonical` with a fixed signed-IR canonical contract and a stable
  encode-error code set (§11).
- Stable error codes + offset semantics for every parse-policy, decoder, and
  canonical-encode failure (§9).
- Test matrix (§13).

**Out of scope:**
- Value-semantic decoders users build *on top of* the located surface: ISO
  dates, flexible booleans, numeric ranges, cross-field/domain validation. The
  located surface provides the primitives; these specific decoders do not ship in
  Slice 2.
- Rewriting the byte-index parser onto the Slice-1 `std.parse` token stream. The
  recursive descent stays; we thread a `_ParseCtx` (§10). `std.source.SourceSpan`
  *may* be used by the located surface internally/additively (Q8), but the error
  type stays `JsonErrorData`.

---

## 1. Baseline: `parse()` today (preserved EXACTLY via a private legacy profile)

`pub fn parse(text: &String) nothrow -> core.Result<JsonNode, JsonErrorData>`
(`json.drift:178`). Behavior:

- **Top level:** any single JSON value; surrounding whitespace ok; trailing
  non-whitespace ⇒ `Err("invalid-syntax")`.
- **Duplicate keys:** silently **last-wins** (`HashMap.insert` overwrite).
- **Numbers:** lenient vs RFC 8259 — accepts **leading zeros** (`0123`, `-00`)
  and **negative zero**; fractions/exponents accepted. Lexeme stored verbatim as
  `JsonNode::Number(raw)` (no parse-time conversion — §5 inspects `raw`).
- **Errors:** `JsonErrorData { tag, offset, line, col, path, key }`; `line`/`col`
  recomputed O(n) from `offset` (`_err_parse_line_col`, `json.drift:892`).

**Compatibility contract:** `parse()` is re-pointed at the unified parser using a
**private** profile `_legacy()` (§7) — the *only* profile that accepts
leading-zeros (a JSON superset). The delegation ships **only after** a differential
test proves `parse(x)` is byte-identical to `parse_with_config(x, _legacy())` for a
broad corpus (same Ok node / same Err tag+offset). Until then `parse()` is left
untouched. (Q5.)

---

## 2. `JsonParseConfig` (orthogonal policy bundle)

All policy types are `Copy` value types. Illustrative spelling:

```drift
pub variant DuplicateKeyPolicy { Reject, KeepFirst, KeepLast }   // Copy (§3)
pub variant TopLevelPolicy     { AnyValue, ObjectOrArray, ObjectOnly }  // Copy

/// Independent numeric toggles. Each selects a SUBSET of valid JSON (turning one
/// off only restricts). There is NO public leading-zeros toggle: leading zeros
/// are invalid JSON and are rejected by every public config (§F3).
pub struct JsonNumberPolicy {
    pub allow_fractions: Bool,       // a '.' fractional part
    pub allow_exponents: Bool,       // an 'e'/'E' exponent
    pub allow_negative_zero: Bool,   // -0, -0.0, -0e0 … (RFC-valid; togglable)
}   // Copy

/// Optional limits — `None` = unlimited (no sentinel integers in the public API).
/// A negative `Some(n)` (n < 0) is NOT silently treated as unlimited: it is an
/// invalid config (§F6) — the parse entry returns `Err("invalid-config")` before
/// parsing, naming the offending limit. (Optional<Uint> was considered to make
/// negatives unrepresentable, but Int keeps `Some(1024)` literals ergonomic and
/// gives a defined, testable failure — finding 4 / §15.)
pub struct JsonLimits {
    pub max_document_bytes: Optional<Int>,
    pub max_depth:          Optional<Int>,   // combined array+object nesting
    pub max_string_bytes:   Optional<Int>,   // decoded byte length of one string
    pub max_number_bytes:   Optional<Int>,   // lexeme byte length of one number
    pub max_array_items:    Optional<Int>,
    pub max_object_fields:  Optional<Int>,
}   // Copy (Optional<Int> is Copy)

pub struct JsonParseConfig {
    pub duplicate_keys: DuplicateKeyPolicy,
    pub top_level:      TopLevelPolicy,
    pub numbers:        JsonNumberPolicy,
    pub limits:         JsonLimits,
}   // Copy
```

`JsonParseConfigBuilder` ships this slice (the config is too large for ergonomic
struct literals — Q4): fluent setters mirroring the existing `JsonConfigBuilder`
style, starting from a chosen profile and overriding fields. **Frozen build
signature** (finding 5):

```drift
pub fn build(self: &JsonParseConfigBuilder) nothrow -> core.Result<JsonParseConfig, JsonErrorData>;
```

`build()` validates and returns `Err(tag="invalid-config", key=<limit name>)` for
a negative limit (§F6); on success `Ok(cfg)`. Configs constructed directly as
struct literals (bypassing the builder) are **still** validated by the parse entry
points (§F6), so validation cannot be skipped either way.

**Orthogonality invariant:** every field is independent; every combination is
valid; a profile is just a preset. No field's meaning depends on another's.

**No-reinterpretation invariant (with one documented exception):** for any input
that two configs both *accept*, they produce the **same** `JsonNode` — **except**
duplicate-key resolution (§3), which by definition selects which same-key value
survives. Policies otherwise only add early `Err` returns; they never change a
value, alter a number's representation, or admit non-JSON syntax.

---

## 3. Duplicate-key policy — the SOLE explicit interpretation policy

A duplicate key is the one case where accepting configs can yield different nodes,
so it is called out as the single explicit exception to no-reinterpretation
(§F2). Detected in `_parse_object_throwing` (`json.drift:1133`) before insert:

- Capture `key_start = *idx` **before** `_parse_string` (span = the key's opening
  `"`).

**Probe strategy (≈one probe per field, by policy):**

| Policy | Probe | Behavior |
|---|---|---|
| **KeepLast** | `insert()` directly (1 probe) | overwrite; the returned old value is dropped. Today's behavior, no `contains_key`. |
| **Reject** | `contains_key()` **after the key, before the value** (1 probe) | on a hit, fail **immediately** at the second key: `Err(tag="duplicate-key", offset=key_start, key=<the key>)` — the value is **not** parsed. Reports the first duplicate, then stops. |
| **KeepFirst** | `insert_if_absent()` (1 probe; 2 only when growth rehashes) | the value **is** parsed (to advance the cursor), then inserted iff absent; on a present key the supplied key+value are dropped, the existing entry kept. |

`insert_if_absent` (`HashMapCore`) is the KeepFirst primitive: inserts + returns
`true` when absent, else drops the supplied key/value and returns `false`.  It
probes **presence first** (no `ensure_capacity`, hence no mutation/rehash, when the
key is present — preserving any outstanding iterator's generation); on the absent
path it grows only if needed and reuses the presence-probe slot unless the
capacity changed (a rehash), re-probing only then.  So it is one `find_slot` for a
present key and for the common absent-without-growth case, and two only when growth
rehashes.  An entry-API that holds a slot across recursive value parsing was
rejected for ownership/invalidation complexity.  Numbers in offset references above
are illustrative
(pre-implementation line numbers).

---

## 4. Top-level policy

Enforced **after** the root value parses, so the diagnostic is about root shape:

- `AnyValue` — no restriction (RFC 8259).
- `ObjectOrArray` — root must be Object/Array, else
  `Err("top-level-not-object-or-array", offset=<root start>)`.
- `ObjectOnly` — root must be Object, else
  `Err("top-level-not-object", offset=<root start>)`.

`<root start>` = byte offset of the first non-whitespace byte (captured before
dispatch).

---

## 5. Numeric controls — inspect the ORIGINAL lexeme; subset-only

`JsonNode::Number(raw)` keeps the verbatim lexeme, so numeric policy is pure
inspection of `raw` **before** any conversion (requirement). Classify `raw`:

- **has_fraction** — contains `.`.
- **has_exponent** — contains `e`/`E`.
- **is_negative_zero** — sign `-` and all significand digits `0` (`-0`, `-0.0`,
  `-0.0e9`); structural, no float conversion.

Each disabled shape ⇒ `Err` with its code (§9), `offset` = number start.
Precedence when several apply (deterministic for tests): negative-zero → fraction
→ exponent.

**Leading zeros (not a public toggle — §F3):** leading zeros are **invalid JSON**.
Every *public* config rejects them with `Err("number-leading-zero", offset=number
start)`. The base number parser therefore rejects `0123`/`-00` by default; only
the **private** `_legacy()` profile sets an internal `accept_leading_zeros` flag
on `_ParseCtx` (true) to preserve `parse()`'s historical lenience. That flag is
never exposed.

---

## 6. Optional limits

Threaded via `_ParseCtx` (§10); `None` = unlimited.

| Limit | Where enforced | Offset on failure | Code |
|---|---|---|---|
| `max_document_bytes` | up front (`byte_length()`) | `0` | `limit-document-bytes` |
| `max_depth` | entering each array/object | the `[`/`{` | `limit-depth` |
| `max_string_bytes` | `_parse_string` (decoded length) | string opening `"` | `limit-string-bytes` |
| `max_number_bytes` | `_parse_number` (`raw` length) | number start | `limit-number-bytes` |
| `max_array_items` | `_parse_array` (count > max) | offending element start | `limit-array-items` |
| `max_object_fields` | `_parse_object` (count > max) | offending key start | `limit-object-fields` |

Checked as parsing proceeds (fail fast); `max_document_bytes` is a cheap
pre-check. `max_depth` counts combined array+object nesting (root = depth 1).

### F6. Invalid config (negative limits)

`Optional<Int>` can hold `Some(n)` with `n < 0`. Such a limit is **not** treated
as unlimited (`None` is the only "unlimited"). Instead, `parse_with_config` /
`parse_located` validate limits **before** parsing and return
`Err(tag="invalid-config", offset=-1, key=<offending limit name>)` (e.g.
`"max-depth"`) — a stable, testable failure rather than silent misbehavior
(finding 4). `JsonParseConfigBuilder.build()` returns the **same** `invalid-config`
`Err` (frozen signature `build() -> Result<JsonParseConfig, JsonErrorData>`, §2),
so a builder-produced config is validated at build time and a struct-literal config
is validated at the parse entry — validation is unskippable either way.
(Alternative considered: `Optional<Uint>` to make negatives unrepresentable;
`Optional<Int>` + `invalid-config` was chosen for literal ergonomics — §15.)

---

## 7. Built-in profiles (all select standard JSON or a subset)

Free functions returning `JsonParseConfig`. `Some(n)`/`None` denote limits.

| Field | `permissive()` | `strict()` | `signed_ir()` | `_legacy()` *(private)* |
|---|---|---|---|---|
| `duplicate_keys` | `KeepLast` | `Reject` | `Reject` | `KeepLast` |
| `top_level` | `AnyValue` | `AnyValue` | `AnyValue` *(Q3)* | `AnyValue` |
| `allow_fractions` | `true` | `true` | **`false`** | `true` |
| `allow_exponents` | `true` | `true` | **`false`** | `true` |
| `allow_negative_zero` | `true` | `true` *(Q1)* | **`false`** | `true` |
| leading zeros | rejected | rejected | rejected | **accepted** *(legacy only)* |
| limits | all `None` | all `None` | all `None` *(Q3, §F5)* | all `None` |

- **`permissive()`** — most lenient *standard* JSON: keep-last dups, all RFC-valid
  number forms (incl. `-0`), no limits. Rejects leading zeros (invalid JSON).
- **`strict()`** — standard JSON, **reject** duplicate keys; `-0` allowed
  (RFC-valid, Q1). `parse_strict(text)` ≡ `parse_with_config(text, strict())`.
- **`signed_ir()`** — deterministic integer profile: integers only
  (`0 | -?[1-9][0-9]*` ⇒ no fractions/exponents/`-0`/leading zeros), reject dups.
  **No** bundled limits and **no** top-level restriction — those are orthogonal
  deployment policies the caller composes explicitly (§F5).
- **`_legacy()`** — PRIVATE; the exact historical `parse()` behavior (the only
  profile accepting leading zeros). Used solely by `parse()` (§1, Q5).

---

## F1. Located decoder surface (object-contract helpers — IN SCOPE)

`JsonNode` has no source locations, so the decoder helpers run on a new
**location-preserving** surface produced by a located parse. `parse_strict()` and
friends keep their `JsonNode` return; the decoder layer uses `parse_located`.

```drift
/// A parsed document plus a source-span sidecar and the source text (retained
/// so error line/col derive on demand via the existing _err_parse_line_col).
pub struct JsonDoc {
    root: JsonNode,
    spans: _SpanTree,   // private; mirrors `root`'s shape (§F1.2)
    text: String,       // Arc-cheap; for line/col derivation
}

pub fn parse_located(text: &String, cfg: &JsonParseConfig)
    nothrow -> core.Result<JsonDoc, JsonErrorData>;

implement JsonDoc {
    pub fn root(self: &JsonDoc) nothrow -> &JsonNode;
    pub fn cursor(self: &JsonDoc) nothrow -> LocatedCursor;   // at the root
    /// Resolve an ABSOLUTE JSON Pointer from the document root (RFC 6901).
    /// On `JsonDoc` (not the cursor) because absolute resolution needs the
    /// root. Per-segment failures use the navigation error rules (§F1.3).
    pub fn at_pointer(self: &JsonDoc, ptr: &String) nothrow -> core.Result<LocatedCursor, JsonErrorData>;
}
```

**`at_pointer` is absolute** (from the document root), resolving §F1.0 pointer
segments in order. Relative single-step navigation is `LocatedCursor.child` /
`.index`. (Finding 1: "absolute or relative" → absolute, hence on `JsonDoc`.)

### F1.0 Path model — JSON Pointer (RFC 6901), NOT dotted

All located-decoder and canonical-encode error `path` values are **JSON Pointers**
(`/a/0/c`), never dotted strings — dotted paths are ambiguous for keys containing
`.` or numeric names (finding 1 / N1). `_split_dotted_path` is **not** reused.

- Root pointer = `""` (the whole document, per RFC 6901).
- Descend into object key `k`: append `/` + `_jp_escape(k)`.
- Descend into array index `i`: append `/` + `i` (decimal).
- `_jp_escape`: replace `~` → `~0`, then `/` → `~1` (in that order). A private
  helper; the inverse (`at_pointer` parsing) un-escapes `~1`→`/`, `~0`→`~`.

### F1.1 `JsonByteSpan` + `LocatedCursor` (concrete)

```drift
/// Half-open byte extent [start, end) of a value (or key) in the source.
pub struct JsonByteSpan { pub start: Int, pub end: Int }   // Copy

implement core.Copy for JsonByteSpan {}

/// A located position. Holds borrows into one `JsonDoc` (its node + parallel
/// span node + retained source text) plus the JSON Pointer to here. Mirrors the
/// existing `JsonCursor` shape (`node: &JsonNode`), so child cursors borrow into
/// the same tree and derive from `&self` (MVP escape rule).
pub struct LocatedCursor {
    node: &JsonNode,     // current value
    span: &_SpanTree,    // parallel span node (private type, §F1.2)
    text: &String,       // doc's retained source, for on-demand line/col
    pointer: String,     // JSON Pointer to the current position ("" at root)
}

implement LocatedCursor {
    // position
    pub fn span(self: &LocatedCursor) nothrow -> JsonByteSpan;          // current value extent
    pub fn pointer(self: &LocatedCursor) nothrow -> String;            // JSON Pointer here

    // navigation (relative single-step; each returns a cursor borrowing the doc)
    pub fn child(self: &LocatedCursor, key: &String) nothrow -> core.Result<LocatedCursor, JsonErrorData>;
    pub fn index(self: &LocatedCursor, i: Int) nothrow       -> core.Result<LocatedCursor, JsonErrorData>;

    // object-contract helpers
    pub fn require(self: &LocatedCursor, key: &String) nothrow -> core.Result<LocatedCursor, JsonErrorData>; // == child
    // Ok(Some) present, Ok(None) absent key, Err only on a real fault (non-object
    // ⇒ type-mismatch-object). Absent-vs-non-object are NOT conflated (finding 2).
    pub fn optional(self: &LocatedCursor, key: &String) nothrow -> core.Result<Optional<LocatedCursor>, JsonErrorData>;
    pub fn forbid_unknown(self: &LocatedCursor, allowed: &Array<String>) nothrow -> core.Result<Void, JsonErrorData>;
    pub fn discriminant(self: &LocatedCursor, key: &String) nothrow -> core.Result<String, JsonErrorData>;

    // typed terminal getters (type-mismatch-* carry pointer + location)
    pub fn as_string(self: &LocatedCursor) nothrow -> core.Result<String, JsonErrorData>;
    pub fn as_int(self: &LocatedCursor) nothrow    -> core.Result<Int, JsonErrorData>;
    pub fn as_uint(self: &LocatedCursor) nothrow   -> core.Result<Uint, JsonErrorData>;
    pub fn as_float(self: &LocatedCursor) nothrow  -> core.Result<Float, JsonErrorData>;
    pub fn as_bool(self: &LocatedCursor) nothrow   -> core.Result<Bool, JsonErrorData>;
    pub fn as_object(self: &LocatedCursor) nothrow -> core.Result<LocatedCursor, JsonErrorData>;  // identity if object
    pub fn as_array_len(self: &LocatedCursor) nothrow -> core.Result<Int, JsonErrorData>;
}
```

**All located navigation + typed getters are `nothrow`** (they return `Err`, never
throw — finding 3). `parse_with_config` / `parse_strict` / `parse_located` /
`encode_canonical` and `JsonDoc.at_pointer` are likewise `nothrow`.

`JsonDoc.cursor(self: &JsonDoc) -> LocatedCursor` builds the root cursor
(`pointer=""`, borrows `&self.root`, `&self.spans`, `&self.text`). All helpers
return `JsonErrorData` with `offset`/`line`/`col` from the located span
(`offset = span.start`; line/col via `_err_parse_line_col(self.text, span.start)`)
and `path` = the JSON Pointer. (`StrictJsonCursor` stays as the span-less peer.)

- **required fields** → `require(key)`: object missing the key ⇒ `"missing-field"`
  (offset = the object's `{`; `path` = the **object's** pointer; `key` = missing
  key).
- **unknown-field rejection** → `forbid_unknown(allowed)`: report the **earliest**
  occurrence (lowest `key_span.start`) whose key ∉ `allowed` ⇒ `"unknown-field"`
  (offset = that key's `"`; `path` = the unknown field's pointer; `key` = the
  key). Earliest-in-source-order is well-defined via the ordered occurrence list
  (§F1.2 / finding 2 / N3).
- **string discriminators** → `discriminant(key)`: `require` + `as_string`; caller
  branches. Errors are the underlying missing-field / type-mismatch-string at the
  discriminant's location.
- **type mismatches** → typed getters: wrong shape ⇒ `"type-mismatch-<kind>"`
  (offset = the value's `span.start`; `path` = value pointer). Integer-vs-float
  decided by inspecting `Number(raw)` (a `.`/`e` makes `as_int` a mismatch),
  matching `StrictJsonCursor.int()/float()`.

### F1.3 Navigation error semantics (exact)

Shared by `JsonDoc.at_pointer`, `LocatedCursor.child` / `.index` / `.require` /
`.optional` / `.forbid_unknown`. `path`/`offset` describe **where traversal
failed** (the deepest node reached), not the requested target.

| Condition | tag | `path` | `offset` | `key` |
|---|---|---|---|---|
| `at_pointer` arg is malformed (no leading `/`, bad `~` escape) | `invalid-pointer` | the offending pointer arg | `-1` (no source) | `""` |
| `child`/`require`/`at_pointer`-step: key absent in object | `missing-field` | the **object's** pointer | object's `{` | missing key |
| `child`/`require`/`optional`/`at_pointer`-step on a **non-object** | `type-mismatch-object` | the node's pointer | node's `span.start` | `""` |
| `index`/`at_pointer`-step on a **non-array** | `type-mismatch-array` | the node's pointer | node's `span.start` | `""` |
| `at_pointer`-step: array segment fails grammar `0 \| [1-9][0-9]*` (`foo`, `01`, `+1`, `-`) or overflows `Int` | `invalid-array-index` | the **array's** pointer | array's `span.start` | raw segment |
| `index(i)` with `i < 0`/`i >= len`, or `at_pointer` valid index `>= len` | `index-out-of-range` | the **array's** pointer | array's `span.start` | requested index (decimal) |

**Array index segments (`at_pointer`)** — when a pointer segment is applied to an
**array** node it must match the RFC-6901 array-index grammar `0 | [1-9][0-9]*`:
no leading zeros (`01` → `invalid-array-index`), no sign (`+1`, `-1` → invalid),
not the append token `-` (invalid for read navigation), non-numeric (`foo` →
invalid), and a value exceeding `Int` range → `invalid-array-index` (key = the raw
segment, never a truncated/overflowed number). A grammar-valid index that is
simply `>= len` is `index-out-of-range` (distinct: well-formed but absent). The
programmatic `LocatedCursor.index(i: Int)` takes an `Int` (no grammar), so it only
ever yields `index-out-of-range` (for `i < 0` or `i >= len`), never
`invalid-array-index`.

- `require(key)` is exactly `child(key)` (alias) — both yield `missing-field` on
  an absent key and `type-mismatch-object` on a non-object.
- `optional(key)`: `Ok(Some(c))` if present, `Ok(None)` if the key is **absent in
  an object**, `Err(type-mismatch-object)` if the current node is not an object
  (finding 2 — absent ≠ non-object).
- `at_pointer` resolves segments left→right; the first failing segment yields the
  row above for the node reached so far (so a `/a/b/c` into a string at `/a/b`
  reports `type-mismatch-object` at pointer `/a/b`).
- A numeric pointer segment is an **array index** when the node is an array and an
  **object key** when the node is an object (no type guessing from the segment
  text); `"0"` as an object key and `0` as an array index are distinct and both
  reachable.

### F1.2 Span sidecar (`_SpanTree`, private)

Built **only** in located mode (pay-for-what-you-use; `parse`/`parse_with_config`
build no sidecar). Mirrors `JsonNode`. Objects keep **both** an ordered occurrence
list (source order, including duplicates) **and** a keyed value-span map (the
surviving value per key) — a keyed map alone cannot express source order or
duplicate occurrences (finding 2 / N3):

```drift
struct _KeyOccurrence { key: String, key_span: JsonByteSpan }   // one occurrence

variant _SpanTree {
    Leaf(span: JsonByteSpan),                      // null/bool/number/string
    Arr(span: JsonByteSpan, items: Array<_SpanTree>),
    Obj(span: JsonByteSpan,
        // EVERY key occurrence in source order (duplicates included) — drives
        // forbid_unknown's "earliest unknown" and keeps order the HashMap loses.
        occurrences: Array<_KeyOccurrence>,
        // surviving value span per key, mirroring JsonNode.Object.fields exactly.
        values: containers.HashMap<String, _SpanTree>),
}
```

**Retained-span consistency (must match the duplicate-key policy so node and span
agree):**
- `KeepFirst` → `values[k]` is the **first** occurrence's value span.
- `KeepLast` → `values[k]` is the **last** occurrence's value span.
- `forbid_unknown` → scans `occurrences` and reports the **earliest** (lowest
  `key_span.start`) occurrence whose key ∉ `allowed`.

The `LocatedCursor` walks `JsonNode` + `_SpanTree` in lockstep (by key via
`values` for objects, by index for arrays). Errors map `JsonByteSpan.start` →
`JsonErrorData.offset`, line/col via `_err_parse_line_col(self.text, start)`.
(`std.source.SourceSpan` may be used additively — Q8; `JsonByteSpan` keeps the
sidecar light and reuses the existing line/col path on the error path only.)

The unified parser (§10) records `_SpanTree` nodes alongside `JsonNode` nodes when
`_ParseCtx` carries a span sink; otherwise it does not.

---

## 8. Public API (additive; existing exports retained verbatim)

```drift
// parser policy
DuplicateKeyPolicy, TopLevelPolicy, JsonNumberPolicy, JsonLimits,
JsonParseConfig, JsonParseConfigBuilder,
permissive, strict, signed_ir, parse_config_builder,
parse_with_config,   // (&String, &JsonParseConfig) -> Result<JsonNode, JsonErrorData>
parse_strict,        // (&String) -> Result<JsonNode, JsonErrorData>  (== strict())
// located decoder surface
JsonDoc, LocatedCursor, JsonByteSpan, parse_located,
// canonical encoding (separate, fixed contract — §11)
encode_canonical     // (&JsonNode) -> Result<String, JsonErrorData>
```

`parse`, `encode`, `JsonConfig`, `StrictJsonCursor`, etc. unchanged.
`JsonParseConfig` (parse) is distinct from `JsonConfig` (encode) — no shared field
or builder. `_legacy()` is **not** exported.

---

## 9. Stable error codes + offset semantics (the contract)

All failures return `JsonErrorData { tag, offset, line, col, path, key }`. Existing
`"invalid-syntax"`/`"internal-error"` unchanged. The **set/spelling of tags** and
the **offset semantics** below are public contract.

**Parse-policy** (offset = byte offset in source; line/col derived; `path=""`):

| tag | offset | key |
|---|---|---|
| `duplicate-key` | second key's `"` | the key |
| `top-level-not-object` / `top-level-not-object-or-array` | root value start | `""` |
| `number-leading-zero` / `number-negative-zero` / `number-fraction` / `number-exponent` | number start | `""` |
| `limit-document-bytes` / `limit-depth` / `limit-string-bytes` / `limit-number-bytes` / `limit-array-items` / `limit-object-fields` | per §6 | `""` |
| `invalid-config` | `-1` (pre-parse; no source) | offending limit name (e.g. `"max-depth"`) |

`invalid-config` is returned by `parse_with_config` / `parse_located` **before
parsing** when a limit is `Some(n < 0)` (§F6), and is also returned by
`JsonParseConfigBuilder.build()` (frozen `-> Result`, §2) for the same condition.

**Located decoder** (offset/line/col from the node's span; `path` = JSON Pointer,
§F1.0):

| tag | offset | key |
|---|---|---|
| `missing-field` | parent object `{` | missing key |
| `unknown-field` | unknown key's `"` | unknown key |
| `type-mismatch-string` / `-int` / `-uint` / `-float` / `-bool` / `-object` / `-array` | value start | `""` |
| `invalid-pointer` | `-1` (caller arg, no source) | `""` (`path` = the bad pointer) |
| `invalid-array-index` | array value start | raw array segment |
| `index-out-of-range` | array value start | requested index (decimal) |

(Full navigation `path`/`offset` semantics for these — including which node a
failed `at_pointer` segment reports — are in §F1.3.)

**Canonical encode** (no source ⇒ `offset=line=col=-1`, consistent with `_err_data`;
`path` = JSON Pointer (§F1.0) to the offending node; `key` = the offending raw
lexeme):

| tag | meaning |
|---|---|
| `canonical-number-leading-zero` | `Number(raw)` has a leading zero |
| `canonical-number-negative-zero` | `Number(raw)` is `-0` |
| `canonical-number-fraction` | `Number(raw)` has a fractional part |
| `canonical-number-exponent` | `Number(raw)` has an exponent |
| `canonical-number-invalid` | `Number(raw)` is not a well-formed number (defensive) |
| `canonical-invalid-node` | `JsonNode::Tombstone` / non-representable node (`key` = node-kind) |

Numeric precedence within one number (parse and canonical): leading-zero →
negative-zero → fraction → exponent. **Across** nodes, the canonical encoder
returns the first error in canonical emit order (§11), not by precedence.

---

## 10. Implementation threading (`_ParseCtx` — Q6)

A single core parser, threaded by a mutable `_ParseCtx` passed `&mut` through the
private helpers (replacing the bare `idx: &mut Int`):

```drift
struct _ParseCtx {
    cfg: JsonParseConfig,        // Copy
    idx: Int,                    // byte cursor (was the &mut Int)
    depth: Int,                  // current nesting depth
    accept_leading_zeros: Bool,  // INTERNAL; true only under _legacy()
    spans: Optional<_SpanSink>,  // present only for parse_located (§F1.2)
}
```

- `parse()` → core with `_legacy()` (accept_leading_zeros=true, no sidecar), after
  the §1 differential passes.
- `parse_with_config(text, cfg)` → core with `cfg` (no sidecar).
- `parse_located(text, cfg)` → core with `cfg` + a span sink, returning `JsonDoc`.
- Per-array/per-object counters are locals; `max_document_bytes` is an entry
  pre-check. No value reinterpretation — accepted input yields the same
  `JsonNode` regardless of sidecar.

`_ParseCtx` is justified (Q6) by the combination of config + counters + optional
location recording that would otherwise be many threaded params.

---

## 11. Canonical encoding (fixed signed-IR contract; result-propagating — §F4)

**Must not reuse the existing `_encode_node`/object path**, which catches failures
and silently returns `"{}"` — unacceptable for a canonical/​signing surface. A
**new** result-propagating encoder:

`encode_canonical(node: &JsonNode) nothrow -> core.Result<String, JsonErrorData>`
(fixed canonical form, no profile arg — Q7):

- UTF-8 output; **no** insignificant whitespace.
- Object keys recursively sorted by **UTF-8 byte** order (reuse `OrderedLexUtf8`
  ordering + `_string_lex_cmp`), but via the new result-propagating walker.
- **Frozen string escaping** (one canonical form per character; signing-stable —
  finding 2). The **same** escaper applies to object keys and string values:
  - `"` → `\"`, `\` → `\\`.
  - `U+0008 U+000C U+000A U+000D U+0009` → short forms `\b \f \n \r \t`.
  - other controls `U+0000–U+001F` not covered above → lowercase `\u00xx` (e.g. `U+0001` → `\u0001`, `U+001F` → `\u001f`).
  - `/` (solidus) → **unescaped** literal `/`.
  - all other scalars, including non-ASCII → **unescaped** UTF-8 bytes verbatim.

  This table is exactly what the existing `_encode_string` (`json.drift:1219`)
  already produces (`_hex_nibble` emits lowercase `a–f`; `/` at 0x2F ≥ 0x20 is
  passed through; bytes ≥ 0x20 emitted verbatim) — the canonical encoder reuses
  that escaping discipline, but wrapped in the result-propagating walker (it
  must NOT inherit the object path's `"{}"` swallow). No `\uXXXX` for non-ASCII,
  no surrogate-pair expansion: scalars are emitted as their source UTF-8.
- **Signed-IR number grammar on emit** (`0 | -?[1-9][0-9]*`): a `Number(raw)` that
  is `-0`, has leading zeros, a decimal, or an exponent is **rejected** with the
  matching `canonical-number-*` tag (§9) — never silently normalized. (Producers
  cannot sign a representation a consumer would read differently.)
- **Unsupported node state:** a `JsonNode::Tombstone` (or any future
  non-representable variant) is **rejected** with `canonical-invalid-node` — never
  emitted as a fallback representation (finding 3). `key` = the node-kind name.
- **Deterministic error selection (finding 4):** the encoder traverses in the
  exact canonical emit order — array elements in **index order**, object members
  in **UTF-8-byte-sorted key order** — and returns the **first** error encountered
  in that traversal. So a document with multiple offenders always fails on the
  same one (the canonically-first), independent of `HashMap` iteration order.
- Caller hashes the exact emitted bytes (hashing stays caller-side — Q7).
- Errors carry `path` (JSON Pointer, §F1.0) + `key` (the offending raw or
  node-kind); `offset=-1` (no source).

Canonical-encode config stays separate from parser config; field-range and
domain enforcement is the typed-decode layer's job, not the encoder's.

---

## 12. User-layered semantic decoders — out of scope (primitives provided)

The located surface (§F1) gives the structural primitives. Value-semantic
decoders users compose on top — ISO-8601 dates, flexible boolean spellings,
numeric range checks, cross-field/domain rules — are **not** shipped in Slice 2.
They are expressible with `require` / `forbid_unknown` / `discriminant` / typed
getters + `parse_int`/`parse_float` (std.parse). Recorded so the stdlib surface
stays minimal.

---

## 13. Test matrix

Driver + compile/run e2e (policy outcomes machine-checked via `Result` tags /
stdout).

**13.1 Per-policy (each independent):** duplicate-key (Reject asserts
`duplicate-key` + **second** key offset/line/col + `key`; KeepFirst=first;
KeepLast=last); top-level (the three modes accept/reject + codes + root offset);
numeric (`1.5`/`1e3`/`-0` accepted/rejected per toggle, code + number-start
offset); **leading-zero rejected by every public profile**, accepted only via
`parse()`; limits (×6: over-limit code+offset, at-limit passes).

**13.2 Profile composition:** `permissive`/`strict`/`signed_ir` over a corpus,
asserting exact accept/reject + codes (e.g. `signed_ir` rejects `1.5`,`1e3`,`-0`,
`0123`,dup keys; accepts `0`,`-7`,`42`; imposes no depth/size/top-level limit).

**13.3 Located decoder:** required (`missing-field` JSON-Pointer path + offset),
unknown-field (asserts the **earliest** unknown occurrence's key + pointer +
offset, with a key duplicated/reordered to pin source-order), discriminator
dispatch, each `type-mismatch-*` with **JSON Pointer** path + the value's
offset/line/col. Pointer-escaping cases: a key containing `/` (`~1`), `~` (`~0`),
and a numeric-string key (`"0"`) vs an array index `0` produce **distinct**,
correct pointers. Multibyte keys carry byte-accurate offsets. Retained-span
consistency: under KeepFirst the surviving value span is the first occurrence's,
under KeepLast the last's.

**13.3b Navigation errors (§F1.3):** each row of the navigation table —
`invalid-pointer` (malformed `at_pointer` arg; `path` = the bad pointer; offset
−1), `missing-field` via `child`/`at_pointer`, `type-mismatch-object` (object op
on a non-object) and `type-mismatch-array` (index on a non-array) reporting the
**failed node's** pointer/offset, and `index-out-of-range` (negative + `>=len`,
`key` = index, `path` = array pointer). **Array-index segment grammar** (finding
1): `at_pointer` on an array with `/a/foo`, `/a/01`, `/a/+1`, `/a/-`, and an
overflowing segment each ⇒ `invalid-array-index` (key = raw segment, path/offset =
array); a grammar-valid `/a/9` on a 3-element array ⇒ `index-out-of-range` (the
two are distinct). `at_pointer` absoluteness: the same pointer resolves
identically regardless of any intermediate cursor. `optional`: `Ok(Some)` present,
`Ok(None)` absent key, `Err(type-mismatch-object)` on a non-object — the three are
distinct (finding 2).

**13.4 Compatibility:** `parse(x)` == `parse_with_config(x, _legacy())` over a
corpus incl. leading-zeros, dup keys, nesting, malformed inputs (same Ok node /
same Err tag+offset). Gate for the §1 delegation.

**13.4b Located/config equivalence (sidecar is value-neutral):** for **every**
public config (`permissive`/`strict`/`signed_ir`) over the corpus,
`parse_located(x, cfg).root` equals `parse_with_config(x, cfg)` (same Ok node —
identical duplicate resolution and values — or same Err tag+offset). Pins that
span-sidecar generation never changes parsing (finding 5).

**13.4c Invalid config:** a limit set to `Some(-1)` ⇒ `Err("invalid-config")` with
the offending limit name in `key`, from `parse_with_config`, `parse_located`, and
`JsonParseConfigBuilder.build()`.

**13.5 UTF-8:** multibyte keys/strings parse; `encode_canonical` key sort is by
UTF-8 bytes; `max_string_bytes` counts **bytes**; located offsets byte-accurate.

**13.6 Canonical encoding:** determinism (same node ⇒ byte-identical output;
recursively sorted keys); rejects each non-integer `Number` shape with its
`canonical-number-*` tag (NOT `"{}"`); `JsonNode::Tombstone` ⇒
`canonical-invalid-node` (not a fallback); **deterministic error selection** — a
document with **multiple** offenders fails on the canonically-first (array index
order / UTF-8-sorted keys) regardless of `HashMap` iteration order; parse→
`encode_canonical` round-trip on integer/object corpora; explicit test that a
deeply-nested encode error propagates (does not collapse to `"{}"`).
**Frozen escaping vectors** (finding 2): `"`/`\` escaped; `\b\f\n\r\t` short forms; a `U+0001`/`U+001F` control ⇒ lowercase `\u0001`/`\u001f`; `/` emitted literally; a multibyte scalar (e.g. `€`, `😀`) emitted as verbatim UTF-8 (no `\uXXXX`); the **same** output whether the string is an object key or a value.

**13.6b Builder:** `build()` returns `Ok(cfg)` for valid limits and
`Err("invalid-config", key=<limit>)` for a negative limit; a builder-built config
parses identically to the equivalent struct-literal config.

**13.7 Limits stress:** depth-limit on deep nesting; document-byte limit on a
large input; at-limit boundary cases.

---

## 14. Docs / version / ABI

- `doc/design/drift-stdlib-spec.md` — extend `## std.json`: parser-policy config +
  profiles, the located decoder surface + object-contract helpers, the canonical
  encode contract, and the stable code table (§9). Note user-layered semantic
  decoders are separate.
- `history.md` — one entry; no version stamps in the module body.
- **ABI unchanged**; toolchain **patch** bump at impl. `parse()` and all existing
  exports unchanged (gated by §13.4).

---

## 15. Resolved decisions (from static review)

- **Q1 leading zeros / `-0`:** leading zeros are **not** a public toggle — invalid
  JSON, rejected by all public configs; only legacy `parse()` accepts them via the
  private `_legacy()`. `strict()` rejects leading zeros but permits RFC-valid
  `-0`; `signed_ir()` rejects both. (§5, §7, §F3.)
- **Q2 limits:** `Optional<Int>` (no sentinels in the public API). (§2.)
- **Q3 `signed_ir()`:** `AnyValue` top-level, all limits unset — no bundled
  operational policy. (§7, §F5.)
- **Q4 builder:** ship `JsonParseConfigBuilder` now. (§2, §8.)
- **Q5 one implementation:** `parse()` delegates through the private `_legacy()`
  profile after the §13.4 differential passes. (§1, §10.)
- **Q6 threading:** `_ParseCtx` (config + counters + optional span sink). (§10.)
- **Q7 `encode_canonical`:** fixed signed-IR canonical contract (no profile arg);
  hashing caller-side; result-propagating with stable error tags. (§11.)
- **Q8 span model:** error type stays `JsonErrorData{offset,line,col}`. The
  located surface stores byte spans internally (`std.source.SourceSpan` permitted
  additively) and maps them into `JsonErrorData` on the error path. (§F1.2.)

### Resolved located-design sub-questions (review round 2)

- **N1 paths:** **JSON Pointer** (RFC 6901, `/a/0/c`, `~0`/`~1` escaping) for all
  located-decoder and canonical errors; `_split_dotted_path` not reused. (§F1.0.)
- **N2 line/col:** `JsonDoc` **retains the source `String`** (Arc copy is cheap);
  line/col computed on demand on the error path. (§F1, §F1.2.)
- **N3 unknown ordering:** keep an **ordered occurrence list** (`occurrences:
  Array<_KeyOccurrence>`) beside the keyed value-span map; `forbid_unknown`
  reports the **earliest** (lowest `key_span.start`) unknown occurrence. (§F1.2.)

### Findings folded in (review round 2)

- **F-1 → §F1.0:** JSON Pointer paths everywhere new.
- **F-2 → §F1.2:** ordered occurrence list + keyed value spans; KeepFirst=first /
  KeepLast=last retained span; forbid_unknown=earliest unknown.
- **F-3 → §F1.1:** concrete public `JsonByteSpan`, concrete `LocatedCursor` fields,
  `self: &LocatedCursor` signatures, `span()` returns `JsonByteSpan`.
- **F-4 → §2/§F6/§9:** negative limits ⇒ stable `invalid-config` (offset −1, key =
  limit name), validated pre-parse / at `build()`.
- **F-5 → §13.4b:** `parse_located(x, cfg).root == parse_with_config(x, cfg)` for
  every public config.

### Findings folded in (review round 3)

- **F-1 → §F1.3 + §9:** exact navigation error table — `invalid-pointer`,
  `missing-field`, `type-mismatch-object`/`-array`, `index-out-of-range`, with
  defined `path`/`offset`/`key`; `at_pointer` is **absolute** (on `JsonDoc`).
- **F-2 → §F1.1:** `optional` is `Result<Optional<LocatedCursor>, JsonErrorData>`
  — `Ok(None)` only for an absent key, `Err(type-mismatch-object)` on a
  non-object.
- **F-3 → §11 + §9:** `canonical-invalid-node` for `Tombstone`/non-representable
  nodes (no fallback representation).
- **F-4 → §11:** canonical error selection is deterministic — array index order +
  UTF-8-sorted keys, first error returned.
- **F-5 → §2 + §F6:** frozen `build() -> Result<JsonParseConfig, JsonErrorData>`;
  parse entries still validate struct-literal configs.

### Findings folded in (review round 4)

- **F-1 → §F1.3 + §9:** array-index segment grammar for `at_pointer`
  (`0 | [1-9][0-9]*`); `invalid-array-index` (bad grammar / sign / leading zero /
  `-` / `Int` overflow; key = raw segment) vs `index-out-of-range` (valid index,
  `>= len`).
- **F-2 → §11 + §13.6:** frozen canonical escape table (short forms; lowercase
  `\u00xx`; `/` and non-ASCII verbatim; same for keys and values) — matches the
  existing `_encode_string`.
- **F-3 → §F1.1 + §F1:** all located navigation + typed getters (and
  `at_pointer`) are `nothrow` returning `Result`.
