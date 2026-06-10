# Slice 1 — `std.source` + `std.parse` Frontend Toolkit (AS BUILT)

**Status:** LANDED. **Toolchain:** driftc **0.33.27** (patch); **ABI 16 unchanged**
(pure-Drift stdlib, no compiler↔runtime boundary change).

> This document records what actually shipped. It supersedes the earlier draft
> plan (which mandated a single `std.parse` module and a pluggable `Lexer<K>`
> existential with a blocking "SP-1" feasibility spike). The delivered design
> diverges from that draft on three points, all per the authoritative 3-slice
> spec — see §"Divergences from the original draft".

---

## 0. Goal

A foundation for hand-written lexers and recursive-descent parsers that parse
hot-loaded source **at runtime in Drift** (no `driftc`), with:

- precise, **stable** diagnostics;
- **durable** source locations that survive into persisted, versioned IR;
- arbitrary valid UTF-8 from configured files.

Invariants:

- **Byte offsets are authoritative** for slicing/hashing.
- **Line/column are diagnostic coordinates only** (column counts Unicode scalar
  values, never bytes).
- **`source_id` is logical/configured**, never an absolute build path; persistable.
- **Deterministic** across machines and reloads: same bytes + same `source_id`
  ⇒ byte-identical spans, slices, and diagnostics.

**Non-goals:** parser combinators, a parser generator, grammar DSL,
incremental/streaming reparse, grapheme awareness, and any pluggable-lexer
abstraction (dropped — see §Divergences).

---

## 1. Module layout (as built)

Two modules, acyclic (`std.parse → std.source → {core, text, io}`):

- **`std.source`** (`stdlib/std/source/source.drift`) — source-location
  primitives + the UTF-8 scalar cursor + a source-level error.
- **`std.parse`** (`stdlib/std/parse/parse.drift`) — token, token-stream, and
  parser diagnostic, **additive** on top of the existing scalar parsers
  (`parse_int`, …, `*_bytes`, `ParseError`), which are unchanged and re-exported
  verbatim.

Two error types by design — no module cycle, no re-export needed:

| Type | Module | Carries | Produced by |
|---|---|---|---|
| `SourceError` | `std.source` | `code`, zero-width `span` | cursor construction / slicing |
| `ParseDiagnostic` | `std.parse` | `code`, `span`, `expected[]`, `found?` | parser layer (`expect`, callers) |

A nothrow `Deque.get(i) -> Optional<&T>` was added to `std.containers`
(`array.drift`) — the bounds-checked, borrow-returning accessor `peek` needs,
because `Deque.at` throws and a `nothrow` fn cannot call a throwing fn (only
`@intrinsic`s like `string_byte_at` are exempt).

---

## 2. `std.source` public surface

```drift
pub struct SourcePos  { pub byte_offset: Int, pub line: Int, pub column: Int }  // Copy
pub struct SourceSpan { pub source_id: String, pub start: SourcePos, pub end: SourcePos }  // Copy
pub struct SourceError { pub code: String, pub span: SourceSpan }  // span is ALWAYS zero-width

pub fn pos_zero() nothrow -> SourcePos                       // {0, 1, 1}
pub fn span_byte_len(&SourceSpan) nothrow -> Int
pub fn span_is_empty(&SourceSpan) nothrow -> Bool

pub fn source_cursor(&Array<Byte>, source_id: String) nothrow
    -> core.Result<SourceCursor, SourceError>               // eager UTF-8 validation
pub fn source_cursor_from_string(String, source_id: String) nothrow -> SourceCursor

implement SourceCursor {                                     // all nothrow
    position() -> SourcePos
    at_end()   -> Bool
    peek()     -> Int    // current scalar value, -1 at EOF
    advance()  -> Int    // decode + consume, -1 at EOF
    mark()     -> SourcePos
    span_from(start: SourcePos) -> SourceSpan
    span_here() -> SourceSpan                                // zero-width at current pos
    source_id() -> String
    byte_length() -> Int
    slice(start: Int, end: Int) -> core.Result<String, SourceError>
    slice_span(&SourceSpan)     -> core.Result<String, SourceError>
}
```

Semantics (frozen):

- **Eager validation, single O(n) pass.** `source_cursor` validates UTF-8 once
  (`_validate_utf8`, computing line/column for the error path), then builds the
  `String` **without re-validating** (`_string_from_validated_bytes` via
  `io.buffer` + `core.string_from_utf8_bytes`). After success every offset the
  cursor yields sits on a scalar boundary, so `peek`/`advance` are infallible
  (`-1` only at EOF). Invalid input → `Err(SourceError{code="invalid-utf8"})`
  with a zero-width span at the offending byte; line/column name the start of
  the offending sequence.
- **LF / CRLF (two-step).** Only `LF` (`0x0A`) advances the line; `CR` (`0x0D`)
  is an ordinary scalar; `CRLF` is two `advance` calls and the line advances
  exactly once, on the `\n`. The cursor never hides a byte.
- **Column counts scalars.** A 4-byte scalar advances `column` by 1 and
  `byte_offset` by 4.
- **Slicing rejects** (all return a **zero-width, coherent** `SourceError.span`):
  out-of-range / inverted ranges → `invalid-slice-range` (the reported offset is
  clamped to `[0, len]` so byte_offset and line/column always agree);
  an offset that splits a multibyte scalar (continuation byte `0x80–0xBF` at
  `start`/`end`) → `slice-not-char-boundary` (line/column name the **containing**
  scalar, no overshoot); `slice_span` of a span whose `source_id` differs →
  `span-source-mismatch` (foreign span collapsed to a point at its start).

---

## 3. `std.parse` frontend surface (additive)

```drift
pub trait TokenKind require Self is cmp.Equatable {
    fn describe(&Self) nothrow -> String
}
pub struct Token<K> { pub kind: K, pub span: source.SourceSpan }
pub struct ParseDiagnostic {
    pub code: String, pub span: source.SourceSpan,
    pub expected: Array<String>, pub found: Optional<String>   // None = structured EOF
}
pub struct TokenStream<K> require K is TokenKind { /* owns Deque<Token<K>> */ }  // Destructible

pub fn parse_diagnostic(code, span, expected, found) nothrow -> ParseDiagnostic
pub fn token_stream<K>(var tokens: Array<Token<K>>, eof_span: source.SourceSpan)
    nothrow -> TokenStream<K> require K is TokenKind

implement<K> TokenStream<K> require K is TokenKind {          // all nothrow
    peek(n: Int) -> Optional<&Token<K>>     // n-th remaining token; lookahead bounded only by tokens left
    current()    -> Optional<&Token<K>>     // peek(0)
    advance()    -> Optional<Token<K>>      // consume front by move
    at_end()     -> Bool
    expect(&K, expected_name: String) -> core.Result<Token<K>, ParseDiagnostic>
}
```

- `expect` consumes the next token iff its kind `eq`s the expected kind; on
  mismatch returns `unexpected-token` (span = offending token, `found` = its
  `describe()`) **without consuming**; at EOF returns `unexpected-eof`
  (span = `eof_span`, `found = None`). It extracts the matched/describe/span
  values inside the immutable-borrow scope, then releases the borrow before the
  mutating `pop_front`, so the lookahead borrow and the consume never overlap.
- **Token source:** `TokenStream` wraps a **fully pre-lexed** `Array<Token<K>>`.
  There is no pluggable lexer; the caller lexes (typically with a `SourceCursor`)
  into the array, then constructs the stream.
- **Diagnostic stability:** the `code` set and field names/types are the contract
  (`"unexpected-token"`, `"unexpected-eof"`, plus the `std.source` codes). `found
  = None` is the structured EOF encoding. Descriptor prose inside
  `expected`/`found` is human-facing and **not** stable — tests assert `code` +
  span + `found.is_none()`, never prose.

---

## 4. Divergences from the original draft (and why)

1. **Separate `std.source` module** (draft kept everything in `std.parse`). Per
   the authoritative spec; the cursor/spans are a cohesive cluster and a
   second consumer is anticipated.
2. **No pluggable `Lexer<K>` interface; "SP-1" feasibility spike dropped.** The
   spec's `TokenStream` is `peek/current/advance/at_end/expect` over a pre-lexed
   sequence — no lazy lexing, so the owned-generic-existential-field risk that
   SP-1 was meant to probe does not exist. (The residual risk — a generic struct
   owning a `Deque<Token<K>>`, mutated and dropped leak-clean — was probed
   separately and passed under memcheck.)
3. **`pub struct ParseDiagnostic`, not `pub error`** (draft proposed `pub error`).
   Per the spec. Consequently it is not `or_throw`-able; callers match it.

Also: lexer-error propagation through `peek`/`advance`/`at_eof` as `Result`
(draft §7) is **gone** — it only existed to serve lazy lexing. With a pre-lexed
stream, `peek`/`current`/`advance`/`at_end` are infallible navigation returning
`Optional`; only `expect` produces a `ParseDiagnostic`.

---

## 5. Test coverage (committed with the feature)

e2e (compile + run under `DRIFT_MEMCHECK=1`), `lang/tests/codegen/e2e/`:

- `std_source_cursor_basics` — ASCII walk, multibyte scalar-columns, two-step
  CRLF (both `\r` and `\n` observed; one line advance), EOF.
- `std_source_invalid_utf8` — rejection at the exact offending byte with
  line/column; valid multibyte accepted.
- `std_source_slice` — valid slice, out-of-range (clamped/coherent span),
  inverted range, mid-scalar rejection (containing-scalar column, zero-width
  span), cross-source `slice_span` rejection (zero-width span from a non-empty
  input span), same-source `slice_span` success.
- `std_parse_token_stream` — peek lookahead + idempotency, `current`, `advance`,
  `at_end`, `expect` Ok, `expect` mismatch (no-consume, `unexpected-token`,
  expected/found), `expect` at EOF (`unexpected-eof`, `eof_span`, `found=None`),
  and a separate stream dropped with a token still buffered (Destructible leak
  gate).

driver, `lang/tests/driver/test_std_source_parse_frontend_api.py`:

- `test_public_types_compile` — every exported type/fn named, zero diagnostics.
- `test_tokenstream_expect_and_drop` — `expect` over a user `TokenKind` + owned
  drop with a buffered token.

Regression gates kept green: existing scalar `std_parse_*` e2e, `std.json`
regressions (json imports `std.parse`), userland container e2e, ABI version
stamp.

---

## 6. Version / docs

- `DRIFTC_VERSION` 0.33.26 → **0.33.27** (additive stdlib surface, patch).
  `DRIFT_RT_ABI_VERSION` stays **16** (no boundary change).
- `doc/design/drift-stdlib-spec.md` — `## std.source` + `## std.parse (frontend
  toolkit)` sections.
- `history.md` — Slice 1 entry. No version/provenance stamps in module bodies
  (house style).
