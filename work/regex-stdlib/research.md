# std.regex Phase 1 Research — Primitive Inventory & Gap Analysis

Status: **complete**
Date: 2026-02-27

## 1. Existing Primitives Inventory

### 1.1 Core string intrinsics (`stdlib/std/core/copy.drift`)

| Function | Signature | Notes |
|---|---|---|
| `string_byte_at` | `@intrinsic pub fn string_byte_at(s: &String, i: Int) -> Byte` | Random access; core lexer primitive |
| `string_from_utf8_bytes` | `@intrinsic pub fn string_from_utf8_bytes(ptr: mem.Ptr<Byte>, len: Int) nothrow -> String` | Construct string from raw pointer + length |
| `byte_length` | `pub fn byte_length(self: &String) nothrow -> Int` | Method on String; intrinsic under the hood |

Compiler-recognized (not exported as pub intrinsic, but available via operators/traits):
- `string_eq(String, String) -> Bool` — equality via `Equatable` impl in `std.core.cmp`
- `string_concat(String, String) -> String` — concatenation

### 1.2 Text module (`stdlib/std/text.drift`)

| Function/Type | Signature | Notes |
|---|---|---|
| `substring` | `pub fn substring(s: &String, start: Int, len: Int) nothrow -> core.Result<String, TextError>` | **Allocates** — copies bytes to new buffer |
| `utf8_from_bytes` | `pub fn utf8_from_bytes(bytes: &Array<Byte>) nothrow -> core.Result<String, Utf8Error>` | Validates UTF-8 then constructs String |
| `utf8_from_bytes_range` | `pub fn utf8_from_bytes_range(bytes: &Array<Byte>, start: Int, end: Int) nothrow -> core.Result<String, Utf8Error>` | Bounded variant |
| `TokenizeAction` | `pub variant { Continue, Stop }` | Copy; callback flow control |
| `TokenSpan` | `pub struct { pub start: Int, pub end: Int }` | Copy; byte range |
| `TokenConsumer<Tok, Err>` | `pub interface` | Callback tokenizer contract |
| `TextError` | `pub struct { pub tag: String, pub offset: Int }` | Implements `Diagnostic` |
| `Utf8Error` | `pub struct { pub tag: String, pub offset: Int }` | Implements `Diagnostic` |

### 1.3 Iteration (`stdlib/std/iter/iter.drift`)

| Function/Type | Signature | Notes |
|---|---|---|
| `StringBytesIter` | struct | Sequential byte iterator |
| `String.bytes` | `pub fn bytes(self: &String) nothrow -> StringBytesIter` | Factory method |

`StringBytesIter` implements `SinglePassIterator<Byte>`.

### 1.4 Buffer / memory (`stdlib/std/io/io.drift`, `stdlib/std/mem/mem.drift`)

| Function | Source | Notes |
|---|---|---|
| `buffer(Int) -> Buffer` | `std.io` | Allocate scratch buffer |
| `buffer_write(&mut Buffer, Int, Byte)` | `std.io` | Write byte at index |
| `buffer_ptr(&Buffer) -> Ptr<Byte>` | `std.io` | Pointer for `string_from_utf8_bytes` |
| `buffer_len(&Buffer) -> Int` | `std.io` | Filled length |
| `RawBuffer<T>`, `Ptr<T>`, `ptr_offset`, `ptr_read`, `ptr_write` | `std.mem` | Unsafe pointer ops |

### 1.5 Private duplicated helpers (not public)

| Helper | Locations | Body |
|---|---|---|
| `_is_digit(Byte) -> Bool` | `std.parse:27`, `std.time:248`, `std.json:470` | `b >= cast<Byte>(48) and b <= cast<Byte>(57)` |
| `_is_ws(Byte) -> Bool` | `std.json:466` | SP/TAB/LF/CR check |
| `_find_byte(&String, Byte) -> Int` | `std.cli:78` | Linear scan; returns index or -1 pattern |
| `_eq_byte(&String, Int, Byte) -> Bool` | `std.cli:66` | Single byte comparison at index |
| `_starts_with_dash*` | `std.cli:70,74` | Hardcoded prefix checks |

## 2. Missing Primitives — Severity Matrix

| Primitive | Severity | Needed by | Rationale |
|---|---|---|---|
| `is_digit(b: Byte) -> Bool` | **Blocker (tokenizer)** | Phase A | Triplicated private; blocks `\d` class and quantifier/escape parsing |
| `is_alpha(b: Byte) -> Bool` | **Blocker (tokenizer)** | Phase A | Not implemented anywhere; blocks `\w` class definition |
| `is_alnum(b: Byte) -> Bool` | **Blocker (tokenizer)** | Phase A | Depends on `is_digit` + `is_alpha`; blocks `\w` |
| `is_space(b: Byte) -> Bool` | **Blocker (tokenizer)** | Phase A | `_is_ws` exists in json only; blocks `\s` class |
| `index_of_byte(&String, Byte) -> Int` | High (ergonomics) | Phase A–C | `_find_byte` exists in cli; avoids repeated scan boilerplate. **Defer to Phase A review** — tokenizer can use `string_byte_at` loops directly |
| `starts_with(&String, &String) -> Bool` | Medium | Phase D | Not needed for byte-level tokenizer; useful for replacement/utility |
| `contains(&String, &String) -> Bool` | Medium | Phase D | Ad-hoc in e2e tests; useful but not blocking |
| Zero-copy string slice/view | Deferred | Phase D+ | `substring` allocates; perf issue for `replace_all` but acceptable for v1 |

## 3. Answers to §1.3 Gate Questions

### Q1: Do we have enough byte/string slicing/search for lexing without O(n²) copying?

**Yes, for tokenizer and parser.** The regex tokenizer and parser operate on byte offsets only — they consume `string_byte_at(pattern, i)` in a single forward pass and track positions as `Int` pairs. No substring extraction is needed during lexing/parsing. The `substring` allocating path is only hit when the user extracts match results, which is O(k) per match, not O(n²).

### Q2: Is current `substring` enough, or do we need cheap slice/view semantics?

**`substring` is sufficient for v1.** Match extraction (`find_first` returning `RegexMatch { start, end }`) returns offsets; the caller decides whether to extract. `replace_all` will need multiple `substring` calls to build output, but for v1 this is acceptable — each call is O(segment_length) and total work is O(input_length). A zero-copy view type is a worthwhile optimization for v2 but is not a gate blocker.

### Q3: Do we need `is_digit`, `is_alpha`, `is_alnum`, `is_space`?

**Yes — all four are blockers.** `is_digit` is already triplicated as private `_is_digit` across three modules. The regex tokenizer needs all four to implement `\d`, `\w`, `\s` character classes and to classify pattern characters during lexing. These should be added to `std.text` as public functions.

### Q4: How are errors best represented for regex parse errors?

**`RegexError { tag: String, offset: Int }` with `Diagnostic` impl.** This mirrors the existing `TextError` and `Utf8Error` conventions exactly. Keep `RegexError` as a distinct type from `TextError` — they represent different error domains (pattern syntax vs. text operations) and consumers will `match` on the type to distinguish them.

### Q5: Can we parse escapes/classes/quantifiers with existing APIs cleanly?

**Yes.** Escape parsing is `string_byte_at(pattern, i)` followed by a byte-value switch. Character class parsing (`[a-z]`) is a state machine over bytes. Quantifier parsing (`*`, `+`, `?`) is single-byte lookahead. All of these are clean with `string_byte_at` + index arithmetic. The only missing piece is the character classification helpers for recognizing `\d`/`\w`/`\s` classes — which is the A1 helper batch.

## 4. Decision: Tokenizer Strategy

**Direct parser over pattern bytes.** The regex pattern language is simple enough that a dedicated tokenizer pass is unnecessary overhead. A single-pass recursive-descent parser consuming `string_byte_at(pattern, i)` directly will produce the AST. The `TokenConsumer` interface in `std.text` is designed for multi-token source-level lexing and is overengineered for regex patterns.

This matches how production regex engines work: the pattern "parser" is really a single-pass reader that produces AST nodes directly.

## 5. Proposed Additions — Phase A1 Helper Batch

Target file: `stdlib/std/text.drift`

All four functions are `nothrow`, operate on raw `Byte`, return `Bool`, and use ASCII-only semantics. No UTF-8 codepoint awareness — these classify individual bytes.

```drift
pub fn is_digit(b: Byte) nothrow -> Bool {
	return b >= cast<Byte>(48) and b <= cast<Byte>(57);
}

pub fn is_alpha(b: Byte) nothrow -> Bool {
	return (b >= cast<Byte>(65) and b <= cast<Byte>(90)) or (b >= cast<Byte>(97) and b <= cast<Byte>(122));
}

pub fn is_alnum(b: Byte) nothrow -> Bool {
	return is_digit(b) or is_alpha(b);
}

pub fn is_space(b: Byte) nothrow -> Bool {
	return b == cast<Byte>(32) or b == cast<Byte>(9) or b == cast<Byte>(10) or b == cast<Byte>(13);
}
```

Rationale:
- ASCII-only is correct for regex v1 (unicode property classes explicitly out of scope).
- `is_space` covers SP (32), TAB (9), LF (10), CR (13) — matches JSON `_is_ws` semantics.
- `is_alpha` covers `[A-Za-z]`; `is_alnum` is `is_alpha || is_digit` — standard `\w` minus underscore; underscore can be added in the regex `\w` class definition itself.
- All four are `Copy`-safe pure functions, no allocation, no side effects.

Export list update for `std.text`:
```drift
export {
	TokenizeAction,
	TokenSpan,
	TokenConsumer,
	Utf8Error,
	utf8_from_bytes,
	utf8_from_bytes_range,
	TextError,
	substring,
	is_digit,
	is_alpha,
	is_alnum,
	is_space
};
```

## 6. Module Placement — std.regex

Convention: most stdlib modules use `stdlib/std/<name>/<name>.drift` (directory-based). Two exceptions (`std.text`, `std.float`) are flat files. Since `std.regex` will grow to multiple files (parser, compiler, executor), use the directory convention:

- **Module file:** `stdlib/std/regex/regex.drift`
- **Module declaration:** `module std.regex`
- **Imports:** `std.core`, `std.text`, `std.io` (for buffer ops during replacement)

Future file split candidates (not in v1, but the directory structure accommodates them):
- `stdlib/std/regex/nfa.drift` — NFA instruction types and compiler
- `stdlib/std/regex/exec.drift` — NFA executor

## 7. E2E Test Plan for A1

New test directory: `lang/tests/codegen/e2e/std_text_charclass_helpers/`

Coverage:
- `is_digit`: ASCII digits 0–9 positive; letters, symbols, 0xFF negative
- `is_alpha`: A–Z, a–z positive; digits, symbols negative
- `is_alnum`: union of above
- `is_space`: SP/TAB/LF/CR positive; other whitespace-like bytes (e.g. 0x0B VT) negative to confirm exact contract

## 8. Phase 1 Gate — Go/No-Go

**GO.**

All five gate questions answered. The existing primitive surface is sufficient for tokenizer, parser, and matcher phases. Four character classification helpers are the only hard prerequisite and they are trivial, side-effect-free additions to `std.text`. No compiler changes, no ABI changes, no new intrinsics needed.

Proceed to Phase A:
1. A1: Add `is_digit`/`is_alpha`/`is_alnum`/`is_space` to `std.text` + e2e test.
2. A2: Implement regex tokenizer (separate patch or immediate follow-up).
3. Update `docs/history.md` when A1 lands.
4. DRIFTC_VERSION bump deferred to feature integration checkpoint (after Phase C minimum).
