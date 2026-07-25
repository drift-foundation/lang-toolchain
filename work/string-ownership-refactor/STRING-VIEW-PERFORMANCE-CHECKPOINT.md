# STRING-VIEW-PERFORMANCE — design checkpoint (report-only, rev 3)

Phase: string-view-performance, opened from mainline after B5/ABI-22
merge (branch `string-view-performance`, base `1d92e0b6`).  Extends
the SAME 0.33.88/ABI-22 candidate; ends in ONE combined certification
with the B5 work — one consolidated design → implementation →
performance → certification chunk, not split into release slices.
This checkpoint is REPORT-ONLY and ends at **STOP** for review.

Rev 2 folded in the first eight review corrections (bulk-window
placement probed + measured, honest performance tiers, closed public
API, corrected allocation claims, corrected adoption list, the
numeric-parser offset discrepancy, count-exact acceptance evidence,
corpus lifecycle wording).  Rev 3 folds in the second round: the
window-size crossover measurement with explicit per-token-bulk
guidance, the REAL wrappable counting symbols (drift_string_new_copy
is static and unwrappable), index_of naming + the missing shipping
signatures + split_views parity pins, the provenance-safe JSON
flagship, and the exact numeric-offset table with its history-record
requirement.

Goal: a first-class performance story for parsing and substring-heavy
String workloads — today allocation-free processing exists only
through `string_byte_at`, offset pairs, and callback-scoped
`std.ffi.with_bytes`; there is no safe, general, STORABLE
substring/byte view, and `std.text.substring` allocates.

---

## 1. Surface & consumer re-audit

### Primitives

| surface | signature | contract |
|---|---|---|
| `core.string_byte_at` | `@intrinsic pub fn string_byte_at(s: &String, i: Int) -> Byte` (core.drift:676) | bounds-checked, throws `std.err:IndexError`; per-call observe guard |
| `String.byte_at` | method wrapper (core.drift:718) | delegates to the intrinsic |
| `String.byte_length` | `nothrow -> Int` (core.drift:712) | bytes, not scalars |
| `std.ffi.with_bytes` | `with_bytes<T,F>(s: &String, body: F) nothrow -> T require F is core.Fn2<mem.Ptr<Byte>, Int, T>` (ffi.drift:69) + `_throw` variant (:75) | base pointer computed ONCE via std.ffi-gated `string_bytes_base`; pointer invalid after the callback returns |
| `std.text.substring` | `substring(s: &String, start, len) nothrow -> Result<String, TextError>` (text.drift:305) | ALLOCATES — fresh storage; the doc comment at text.drift:294-302 already tells parsers to keep one backing `&String` + offset spans and reach for `with_bytes` |

Module dependency check (for §5 placement): `std.ffi` imports only
`std.core` + `std.mem`; `std.text` imports `std.core`/`std.io`/
`std.mem` — so `std.text` MAY import `std.ffi` with no cycle.

### Consumers (real parser paths)

* **SourceCursor** (source.drift:58) — UTF-8-validated scalar cursor:
  one retained `src: String` + byte offset; scans via `string_byte_at`
  (`_decode_at` :253); materializes ONLY on `slice`/`slice_span`
  (:438/:457 → `text.substring`).  Consumed by `std.parse` tokens
  (`Token<K>.span: source.SourceSpan`).
* **std.json** — recursive descent over `text: &String` + `&mut Int`
  byte cursor, `string_byte_at` everywhere.  Span sidecar exists
  (`JsonByteSpan{start,end}` :1711, `_SpanTree`, `JsonDoc{root, spans,
  text}` — the closest existing "retained view" shape).  Allocation
  hot spots: `_parse_string` (:1876) builds values via PER-BYTE
  `out = out + _byte_to_string(b)` (:1959) even for escape-free
  strings — the single worst path; `_parse_number` materializes
  `raw` per number via `_slice_string` (:2013/:1669) into
  `JsonNode::Number(raw: String)`; JSON-pointer segmentation
  allocates a String per path segment (`_slice_bytes` :1593) and
  looks keys up via `HashMap<String, JsonNode>.get(&String)`.
* **std.parse** — String-entry parsers plus byte-range entry points
  `parse_int_bytes`/`parse_uint_bytes(bytes: &Array<Byte>, start,
  end)` (parse.drift:514/:563) — the existing allocation-free model,
  but over `&Array<Byte>`, NOT over String storage (callers must copy
  String → Array first via `string_to_utf8_bytes`).  A pre-existing
  contract defect lives here — see §6a.
* **Iterators** — `std.iter.StringBytesIter{s: String, idx, len}`
  (iter.drift:83): already retains a String HANDLE + offsets (struct
  fields cannot be references) — a proto-view; byte-level only, no
  scalar iterator outside SourceCursor.
* **std.regex** — zero-copy SPAN results (`RegexMatch{start, end}`,
  regex.drift:66; no materialization on match; only `replace`
  allocates) — but NO retained backing and NO view-input support:
  a match cannot outlive knowledge of its haystack, and matching a
  substring requires materializing it first.  §11 closes exactly that
  gap.
* **std.codec** — byte-indexed decode, bulk `string_from_utf8_bytes`
  encode; no substring use.
* **`text.substring` production callers**: exactly 3 —
  `SourceCursor.slice` (source.drift:447) and two `std.meta`
  `_parse_tag` sites (meta.drift:130/:135).  `std.text.split`
  byte-copies each field directly (`_push_slice` :906) — same
  allocation pressure without going through `substring`.
* **Second JSON scanner in core.drift** (:1053 `JsonCursor` +
  `_json_parse_*_at`): byte-offset diagnostics-params reader
  (`ErrorParamsView.get`), materializes owned Strings per field read.

Key observation: every span/token type in the tree is already
`{start, end}` byte offsets — but NONE pairs the offsets with a
retained backing handle into a self-carrying view.  The missing piece
is exactly one type.

## 2. Measured parser-shaped workload comparison

Benchmark: tokenize a 2,097,180-byte comma-separated input
(279,653 tokens, mean token ~6.5 B) computing an identical checksum
(token count + length sum + first-byte sum); all variants
checksum-equal.  driftc 0.33.88/ABI 22 at `1d92e0b6`, `--dev`, clang
-O2 link, AMD Ryzen 9 9950X3D, quiet machine, 7 iterations, medians;
spread within a variant <±3% (w3/w4 first-iteration warmup excluded
by median).  Bench source: scratch `svperf/bench.drift` (report-only;
becomes a committed gate during implementation).

| variant | median | ns/byte | vs w2 |
|---|---|---|---|
| w1 `string_byte_at` indexed scan (today's span idiom) | 2,199 µs | 1.05 | 5.2× |
| w2 `with_bytes` base-once scan | 419 µs | 0.20 | 1.0× |
| w3 `substring` per-token materialization | 28,987 µs | 13.8 | 69× |
| w4 retained-view prototype, view constructed per token | 13,947 µs | 6.7 | 33× |
| w5 same view, constructed once, offsets mutated per token | 2,350 µs | 1.12 | 5.6× |
| w6 COMPOSED bulk window over a view (std.text shape, §5) | 405 µs | 0.19 | ≈1.0× |

Derived unit costs (subtracting the shared w1 scan):

* **materialization cost ≈ 96 ns/token** (w3−w1 over 279,653) —
  alloc + byte copy + release per token;
* **view construct+destroy ≈ 42 ns/token** (w4−w1) — one retain, one
  release, drop glue; NO allocation (2.1× cheaper than substring at
  the identical code shape);
* **view read path ≈ byte_at + 7%** (w5/w1) — one offset add + field
  loads; per-token retain traffic, not reads, is the entire w4 gap;
* **the composed bulk window loses nothing at scan size** (w6 ≈ w2):
  routing `with_view_bytes` through `ffi.with_bytes` with a nested
  callback measured 405 µs vs 419 µs direct.  The composition also
  COMPILE-PROVES in the real generic signature
  (`with_view_bytes<T, F> require F is core.Fn2<…>` with
  `captures(move body, copy start, copy vlen)` — probe binary runs
  correctly).

### Performance tiers — stated honestly

The view gives Drift THREE tiers, and the safe tier is NOT the fast
tier:

1. **Safe indexed reads** (`StringByteView.byte_at`): approximately
   today's guarded `string_byte_at` path (~1.05 ns/B + 7% offset
   composition).  Safe, bounds-checked, no pointers.
2. **Bulk pointer window** (`with_view_bytes`): base-once speed
   (~0.20 ns/B, 5.2× tier 1) — but the pointer arithmetic and
   `ptr_read` INSIDE the callback remain `unsafe`, and the window is
   callback-scoped exactly like `with_bytes` today.
3. **Materialization** (`to_string`): ~96 ns/token — now explicit
   and reserved for escape points.

What the view SOLVES is safe STORAGE and LIFETIME — a substring you
can hold, pass, and store in O(1) without allocation and without a
dangling pointer.  It does NOT make the fastest raw-pointer loop
safe; that loop keeps its unsafe, scoped contract.

### Window-size crossover — bulk windows are NOT for tokens

The single 2 MB window overgeneralizes; a per-window sweep over the
same input (windows of S bytes processed end-to-end; safe per-byte
view-path reads vs a composed bulk window CONSTRUCTED PER WINDOW —
subview + boxed callback per window, as a per-token bulk user would
write; byte-sum aggregate, 5 iterations, medians) gives:

| window | safe reads | bulk-per-window | bulk vs safe |
|---|---|---|---|
| 8 B | 2,008 µs | 14,274 µs | **7.1× WORSE** |
| 32 B | 1,911 µs | 3,662 µs | **1.9× worse** |
| 128 B | 2,025 µs | 1,030 µs | 2.0× better |
| 512 B | 1,941 µs | 402 µs | 4.8× better |
| 4 KB | 1,900 µs | 226 µs | 8.4× better |
| 64 KB | 1,892 µs | 191 µs | 9.9× better |
| 2 MB | 1,914 µs | 189 µs | 10.1× better |

Fixed per-window cost ≈ 54 ns (boxed-callback env allocation + view
retain/release + `with_bytes` entry + dispatch), amortized only by
window LENGTH.  **Crossover ≈ 64 B**; typical parser tokens (this
corpus: ~6.5 B) sit far below it.

PINNED API GUIDANCE (ships in the with_view_bytes doc comment and the
Effective Drift section): per-token bulk windows are an ANTIPATTERN —
use safe `byte_at` reads for token-sized work; reach for
`with_view_bytes` only for scan-sized windows (hundreds of bytes and
up), ideally one window per parse/scan pass.

Design consequences pinned by the numbers: reads through the view
need no new intrinsic (tier 1 measured); the bulk window is mandatory
to reach tier-2 speed at scan size and composition costs nothing
there (w6); per-token bulk is measurably counterproductive (sweep);
parsers should scan with offsets/subviews and construct views at
ESCAPE boundaries, where they construct owned Strings today.

## 3. Type (settled)

```drift
pub struct StringByteView {
    backing: String,   // retained handle — keeps storage alive
    start: Int,        // byte offset into backing
    len: Int           // byte length of the view
}
```

* O(1) construction: one handle copy (ARC retain, measured 42 ns
  incl. eventual release) + two ints.  NO byte copies, NO allocation.
* Storable: an ordinary stdlib value; fields private (offsets must
  not be forged — construction is the only bounds gate).
* No lifetime syntax, no stored raw pointer: storage liveness is
  carried by the retained `String` handle exactly like
  `StringBytesIter` and `JsonDoc.text` already do.
* NOT Copy (verified on 0.33.88: a String-bearing struct is move-only
  — `E-AUTO-e8f17b8b`), so passing a view by value is a move;
  duplication is EXPLICIT (`dup`).  Matches the no-implicit-copy
  doctrine and makes retain traffic visible in source.

## 4. Byte-boundary semantics — decided

Arbitrary byte ranges are valid: parser spans (JSON, regex, source
cursors, HTTP framing) are byte extents, and the backing String's own
contract is UTF-8-conventioned BYTES (substring today does not
validate boundaries either).  Therefore the honest name ships:
**`StringByteView`**.  `StringView` is RESERVED for a future
UTF-8-boundary-validated view (natural construction site:
`SourceCursor`, which already validates on entry); it is NOT part of
this phase.  `to_string(view)` mirrors `substring` semantics —
byte-exact copy, no validation.

## 5. Public API — CLOSED (review blockers resolved)

Home module: **std.text** (owns the type AND the fields — including
the bulk window, which composes through `std.ffi.with_bytes`; no
cross-module field access anywhere).  Style decision: **methods** on
the view for everything with a `self` (matching `String.byte_at`/
`byte_length` and SourceCursor idiom); **free functions** only for
construction and the bulk window.

Errors: **`TextError` reused** for all fallible construction
(symmetry with `substring`); `std.err:IndexError` for indexed read
violations, with canonical container id **`std.text:StringByteView`**.

Bounds form (everywhere, overflow-safe subtraction form — verbatim
the `substring` predicate): valid iff
`start >= 0 && len >= 0 && start <= n && len <= n - start`.
Error offsets (pinned): construction/subview failures return
`TextError(tag = "out-of-bounds", offset = start)` — exactly
`substring`'s current choice; `byte_at` IndexError carries the
requested VIEW-RELATIVE index.

```drift
// ── construction (free functions) ──────────────────────────────────
pub fn byte_view(s: &String, start: Int, len: Int) nothrow
    -> core.Result<StringByteView, TextError>;
pub fn byte_view_all(s: &String) nothrow -> StringByteView;   // infallible

// ── methods ─────────────────────────────────────────────────────────
pub fn byte_length(self: &StringByteView) nothrow -> Int;
pub fn is_empty(self: &StringByteView) nothrow -> Bool;
pub fn byte_at(self: &StringByteView, i: Int) -> Byte;         // IndexError, view-relative
pub fn subview(self: &StringByteView, start: Int, len: Int) nothrow
    -> core.Result<StringByteView, TextError>;                 // O(1), one retain
pub fn dup(self: &StringByteView) nothrow -> StringByteView;   // explicit copy, one retain

// search/comparison — BOTH forms (view/view and view/String);
// returned indexes are VIEW-RELATIVE.  Names REUSE the existing
// std.text vocabulary — the String search entry point is `index_of`
// (text.drift:534), so the view ships index_of/index_of_view and
// introduces NO competing `find` name.  Empty-needle semantics are
// the existing text ones (index_of -> 0, starts_with/ends_with ->
// true); index_of returns -1 when absent (text convention).
pub fn eq_view(self: &StringByteView, other: &StringByteView) nothrow -> Bool;
pub fn eq_string(self: &StringByteView, s: &String) nothrow -> Bool;
pub fn starts_with_view(self: &StringByteView, prefix: &StringByteView) nothrow -> Bool;
pub fn starts_with(self: &StringByteView, prefix: &String) nothrow -> Bool;
pub fn ends_with_view(self: &StringByteView, suffix: &StringByteView) nothrow -> Bool;
pub fn ends_with(self: &StringByteView, suffix: &String) nothrow -> Bool;
pub fn index_of_view(self: &StringByteView, needle: &StringByteView) nothrow -> Int;
pub fn index_of(self: &StringByteView, needle: &String) nothrow -> Int;

// materialization — the only String-storage allocator on the view
pub fn to_string(self: &StringByteView) nothrow -> String;

// ── iteration ───────────────────────────────────────────────────────
// CONSUMES the view (move-in): iteration adds NO hidden retain; a
// caller needing the view afterward writes `v.dup().bytes()`.
pub fn bytes(var self: StringByteView) nothrow -> ViewBytesIter;
pub struct ViewBytesIter { /* view + idx; next() -> Optional<Byte> */ }

// ── bulk window (std.text, composed through std.ffi) ───────────────
pub fn with_view_bytes<T, F>(v: &StringByteView, var body: F) nothrow -> T
    require F is core.Fn2<mem.Ptr<Byte>, Int, T>;
pub fn with_view_bytes_throw<T, F>(v: &StringByteView, var body: F) -> T
    require F is core.FnThrow2<mem.Ptr<Byte>, Int, T>;
```

Bulk-window placement (review item 1, resolved by probe): the
implementation lives in **std.text** and composes through
`std.ffi.with_bytes` over `v.backing`, invoking `body` with
`(base + start, len)` from a nested callback that captures
`move body, copy start, copy len`.  Both the monomorphic and the real
GENERIC signature compile and run (probe binaries exit 0), and the
composition measured **405 µs vs 419 µs direct** (w6 vs w2) — the
base-once tier survives composition; one boxed-callback allocation
per WINDOW (not per byte) is the only added cost.  Callback contract
verbatim from `with_bytes`: the pointer is INVALID after `body`
returns; no other API exposes a pointer.

Empty view: any `len == 0` with `0 <= start <= n` is valid;
`byte_at` on it throws IndexError, `to_string` yields `""` (the
runtime empty singleton — NO allocation), searches follow the
empty-haystack text semantics.

Adoption-surface signatures (SHIPPING, pinned here — §9 carries the
rationale):

```drift
// std.text — split parity family.  Semantics MIRROR text.split
// exactly, element type aside (pinned parity cases below):
pub fn split_views(s: &String, delimiter: &String) nothrow -> Array<StringByteView>;
//   * empty delimiter  -> one single-byte view per input byte
//                         (split's per-byte semantics, zero copies);
//   * absent delimiter -> [byte_view_all(s)] (one whole-input view);
//   * empty input      -> [empty view]  (split returns [""] — parity);
//   * empty fields     -> preserved ("a,,b" -> 3 views, middle empty;
//                         trailing delimiter yields a trailing empty
//                         view — exactly split's field structure).
// Allocation statement: ZERO substring allocations / ZERO byte
// copies; allocates the OUTPUT ARRAY + one backing retain per element.

// std.source — view slice beside the allocating slice:
pub fn slice_view(self: &SourceCursor, start: SourcePos, end: SourcePos) nothrow
    -> core.Result<StringByteView, SourceError>;
//   bounds/order checks IDENTICAL to slice (same SourceError codes);
//   returns a view over the cursor's retained src instead of copying.

// std.json — provenance-safe flagship + explicit byte-range form:
pub fn raw_view(self: &LocatedCursor) nothrow -> StringByteView;
//   infallible: the cursor's span was produced by the parser over
//   THIS document's retained text (in-bounds by construction).
pub fn byte_range_view(self: &JsonDoc, start: Int, len: Int) nothrow
    -> core.Result<StringByteView, text.TextError>;
//   EXPLICITLY a numeric byte-range operation over the document's
//   retained text — it neither takes nor implies span provenance;
//   delegates verbatim to text.byte_view(&self.text, start, len)
//   (same TextError, same subtraction-form bounds, offset = start).

// std.parse — after the §6a contract fix lands:
pub fn parse_int_view(v: &StringByteView) nothrow -> core.Result<Int, ParseError>;
pub fn parse_uint_view(v: &StringByteView) nothrow -> core.Result<Uint, ParseError>;
//   digit/sign/overflow semantics of parse_int_bytes/parse_uint_bytes;
//   error offsets VIEW-RELATIVE per the §6a table.
```

Considered and EXCLUDED from the initial surface: `view_split`
(delivered as std.text `split_views` adoption instead, §9), any API
returning interior pointers, any whole-string zero-copy C-string
special case (§7).

## 6. Ownership behavior — pinned

* Moving, returning, or storing a view keeps its backing alive: the
  retained `String` field is dropped only when the view is dropped
  (ARC).  Pin: build views into an `Array<StringByteView>`, drop the
  original String binding, read views — bytes intact.
* `dup`/`subview`/`byte_view*` copy NO bytes and allocate NOTHING;
  each performs EXACTLY ONE retain (proof mechanism in §10).
* Ordinary reads (`byte_at`, `byte_length`, comparisons, searches,
  iteration STEPS) take `&StringByteView` / `&self` and perform ZERO
  retains/releases (w5 ≈ w1 + 7% is the performance witness; §10 adds
  the counting proof).  Iterator CONSTRUCTION consumes the view, so
  it too adds no retain beyond what the caller explicitly dup()s.
* No raw pointer survives outside a `with_view_bytes` callback
  window; the view type itself never stores one.

### 6a. Pre-existing defect to resolve FIRST: numeric byte-range offsets

`parse_int_bytes` (parse.drift:514) DOCUMENTS "offset in any returned
`ParseError` is relative to `start`" but the implementation returns
ABSOLUTE indexes (`offset = i` mid-scan; also `invalid-range` returns
`offset = start`, and the local `val len = end;` naming invites
off-by-reading).  `parse_uint_bytes` mirrors the same shape.  This is
a contract bug independent of views — and `parse_*_view` must not
inherit an ambiguous contract.

Pinned resolution (regression-first, inside this phase's
implementation): the DOCUMENTED contract wins — offsets are RELATIVE
to `start` (equivalently: view-relative), consistent across all three
families (String forms: offsets from 0 = trivially consistent;
byte-range forms: relative to `start`; view forms: view-relative).

EXACT offset table (pinned; `k` = the absolute input index where the
scan stopped; all reported offsets = `k - start` except invalid-range,
where "relative to start" is undefined because the range itself is
invalid):

| case | tag | pinned offset |
|---|---|---|
| invalid start/end (`start < 0`, `end < start`, `end > len`) | `invalid-range` | **0** — no content position exists; documented as positionless |
| empty range (`start == end`, range valid) | `invalid-syntax` | 0 (position of the missing first character) |
| sign-only (`"+"` / `"-"` then end) | `invalid-syntax` | 1 (position of the missing digit after the sign) |
| invalid digit at absolute `k` | `invalid-digit` | `k - start` |
| overflow at absolute `k` | `overflow` | `k - start` |
| underflow at absolute `k` (signed) | `underflow` | `k - start` |
| negative input to unsigned (`"-…"`) | `invalid-datatype` | 0 (position of the sign) |

Today's implementation returns ABSOLUTE `k` for the digit/overflow/
underflow cases and absolute `start`/`i` for the syntax cases —
callers may currently OBSERVE absolute offsets, so this is a
documented BEHAVIOR CORRECTION, not just a doc fix: it is RECORDED in
the 0.33.88 `doc/history.md` entry when the fix lands.  The fix lands
with regression pins for every row of this table in all three
families (String, byte-range, view) BEFORE `parse_*_view` is added.

## 7. C interoperability — stated honestly

An arbitrary view is NOT NUL-terminated at `start + len`, so
C-string conversion NORMALLY COPIES: the supported route is
`to_string(v)` → existing `std.ffi` checked/owned surfaces.  NO
zero-copy C-string promise is made for views — not even for
whole-string views (a `len == byte_length` special case would be an
invisible performance cliff; if evidence justifies it later it comes
back through its own review).  Zero-copy access for C stays exactly
what it is today: `with_bytes`/`with_view_bytes` windows (pointer +
explicit length, no NUL contract) and the checked `with_cstr*`
family on whole Strings.

## 8. ABI verdict

`StringByteView` is an ORDINARY STDLIB VALUE on ABI 22:

* Composition: `{String, Int, Int}` — existing field kinds only; no
  new runtime type, no header knowledge, no layout authority change
  (the B5 layout audit continues to pass untouched).
* Every operation lowers to EXISTING primitives: `string_byte_at`,
  `byte_length`, String retain/release, and — only through the
  composed `std.ffi.with_bytes` call — the std.ffi-gated
  `string_bytes_base`.  No new intrinsic, no checker special case,
  no codegen change; the std.ffi module gate is untouched (std.text
  never calls `string_bytes_base` directly — probed).
* Therefore NO version/ABI review is triggered: ABI stays 22, riding
  the stamped 0.33.88 candidate.  Deliberately REJECTED for this
  phase: a fused `view_byte_at` intrinsic (double bounds check costs
  ~7%, not worth a compiler-boundary change) and any B-repr-style
  representation work.  If the implementation phase discovers it
  cannot stay stdlib-only, work STOPS at the mandated version/ABI
  review gate before any boundary change.

## 9. Adoption in the same implementation chunk (corrected)

Genuine VIEW adoptions (each with before/after allocation + timing
evidence):

1. **`LocatedCursor.raw_view()`** — the flagship genuine adoption,
   chosen for PROVENANCE SAFETY: `JsonByteSpan` carries no source
   identity, so a `span_view(span)` API could silently interpret a
   span from ANOTHER document (rejected).  The cursor's span was
   produced by the parser over this document's retained `text`, so
   `raw_view()` is infallible and cannot cross documents.  The
   companion `JsonDoc.byte_range_view(start, len)` is EXPLICITLY a
   numeric byte-range operation (name says so; no span parameter, no
   provenance implication) delegating verbatim to `text.byte_view`
   with its exact `TextError` semantics — it does NOT use the
   `JsonErrorData` result surface because it is not a JSON-domain
   operation (signatures pinned in §5).
2. **SourceCursor** — add `slice_view(start, end) ->
   Result<StringByteView, SourceError>` beside the allocating `slice`
   (which stays, documented as the materializing form); std.parse
   token flows can then defer materialization to token-escape points.
3. **std.text `split_views(s, sep) -> Array<StringByteView>`** —
   ZERO substring allocations and ZERO byte copies; honestly stated:
   it allocates the OUTPUT ARRAY and performs one backing retain per
   element (§4-correction — "allocation-free" only in the
   substring/byte-copy sense).
4. **std.parse `parse_int_view` / `parse_uint_view`** — closes the
   String→Array<Byte> copy gap for JSON/HTTP-style parsers (lands
   AFTER the §6a contract fix).

Adjacent parser work in the same chunk (NOT view-dependent — stated
honestly per review):

5. **std.json `_parse_string` escape-free fast path** — it must
   return an OWNED String (JsonNode stores String), so the fix is a
   span scan + ONE direct range copy replacing per-byte
   concatenation.  A temporary view buys nothing here; one is used
   ONLY if measurement beats the direct range copy (not expected).
   This remains the biggest allocation win in std.json and ships in
   this chunk as adjacent work.
6. **std.json `_parse_number`** — `JsonNode::Number(raw: String)`
   REQUIRES one materialization per number (fractions/exponents rule
   out integer-view parsing as a replacement).  What views/spans
   offer is validation over the span before the single required
   materialization; the allocation count does not change.  Kept only
   as that honest, bounded improvement.

DROPPED from the adoption list (review item 5): JSON-pointer segment
migration — lookup goes through `HashMap<String, JsonNode>.get(&String)`,
and a view cannot replace segment Strings without a designed,
measured hash-compatible heterogeneous lookup; linear scanning would
be a REGRESSION.  Out of scope; may return as its own designed map
change with evidence.

std.regex IS integrated — via the §11 surface (conversions +
view-input matching); its `replace` paths and engine internals are
untouched.  Explicitly NOT migrated: std.codec (bulk converters),
the core.drift diagnostics JSON reader (correctness-sensitive,
cold), `StringBytesIter` internals (already view-shaped).

Documentation ships in the same chunk: Effective Drift gains a
"String views for parsers" section (the three performance tiers of
§2 stated honestly, view-first idiom, when to materialize, the
with_view_bytes window contract), and the `substring`/text.drift
guidance comment is updated to point at the view as the standard
answer.

## 10. Acceptance criteria (one final certification)

* **Exact retain/allocation-count evidence** (review item 7 — timing
  and memcheck are NOT count proofs).  Mechanism — REAL wrappable
  symbols only (`drift_string_new_copy` is a `static` shared
  constructor body in string_runtime.c and CANNOT be wrapped): a
  driver test compiles count fixtures and links them with a C
  counting shim via the B5 custom-link technique, wrapping
  - `-Wl,--wrap=drift_string_retain` and
    `-Wl,--wrap=drift_string_release` — exported (string_runtime.h
    :157/:158) — for retain/release counts;
  - `-Wl,--wrap=drift_string_from_utf8_bytes` — exported (:149), the
    materialization entry `to_string` lowers through — for nonempty
    String-storage allocation counts (empty goes to the singleton and
    never reaches it);
  - `-Wl,--wrap=drift_alloc_array` — the exported allocator that
    boxed-callback environments go through (`drift_cb_env_free` is
    its paired free) — for the separate boxed-callback-env count.
    Arrays share this allocator, so counting uses strict
    marker-window discipline: the fixture performs ONLY the operation
    under test between markers.
  The shim counts calls between test markers; the fixture asserts
  EXACT numbers.  Proof obligations:
  - `byte_view`/`byte_view_all`/`dup`/`subview`: EXACTLY ONE
    `drift_string_retain` each; zero releases in-window; zero
    `drift_string_from_utf8_bytes`; zero `drift_alloc_array`;
  - `byte_at`/`byte_length`/`eq_*`/`index_of*`/`starts_with*`/
    iterator STEPS: ZERO retains, ZERO releases, ZERO allocations;
  - nonempty `to_string`: EXACTLY ONE `drift_string_from_utf8_bytes`;
  - empty `to_string`: the runtime singleton — ZERO calls to any
    wrapped constructor/allocator;
  - `with_view_bytes`: zero `drift_string_from_utf8_bytes`; EXACTLY
    ONE `drift_alloc_array` (the boxed inner-callback env), asserted
    separately;
  - **forced-throw `with_view_bytes_throw`**: a callback that throws
    mid-window must unwind with NO leaked retain and NO leaked env —
    retain/release and env alloc/free balance to zero across the
    throw (memcheck-clean under the same fixture).
  Secondary evidence: emitted-IR call accounting on the compiled
  fixture (no retain/release call sites inside read-path loops).
* **Semantic pins**: bounds failures (construction, subview,
  byte_at incl. the `std.text:StringByteView` container id and
  view-relative index), empty-view behavior, `to_string` ==
  `substring` byte-equality on a boundary-and-interior-NUL fixture
  matrix, search/eq parity with the String forms, empty-needle
  semantics.
* **Lifetime pins**: views outlive their source binding
  (Array<StringByteView> + dropped original), memcheck-clean under
  forced drops.
* **§6a regression pins**: offset contract across String, byte-range,
  and view numeric parsers.
* **Parser correctness**: std.json full suite equivalence on the
  touched paths (same node trees, same spans, same diagnostics);
  SourceCursor/std.parse suites green.
* **Performance evidence**: the §2 benchmark committed as a
  repeatable harness with the view/bulk variants asserting their
  tier (guard-band, not exact numbers), plus before/after numbers on
  the migrated JSON string path.
* **Corpus lifecycle** (per the established promotion policy): run
  `just ownership-corpus-check` against the promoted reviewed
  baseline; new/changed stdlib functions are EXPECTED to shift
  `fns`/event counters (std.ffi precedent: +26 fns/+20 events per
  fixture).  Any intentional delta is MEASURED, ATTRIBUTED (residual
  zero), REVIEWED, APPROVED, and PROMOTED into the baseline BEFORE
  the final full runner and certification handoff — the strict
  zero-delta gate itself never changes.
* **Suites**: memcheck + ASAN `just test` green; full broad suite
  (maintainer-run) green.
* **ONE final certification** for the combined
  B5 + string-view-performance candidate — no intermediate
  certification.

LANGUAGE_BUG policy rides unchanged: any compiler defect exposed by
the implementation follows regression-first + refactor-trigger
process, stopping only at the mandated policy gates.

## 11. std.regex integration (review addition — pinned)

Framing pinned first: **`RegexMatch` stays exactly what it is** — a
cheap `Copy` span `{pub start, pub end}` with PUBLIC fields and NO
source identity.  `StringByteView` supplies storage and direct reads
ONLY when explicitly requested.  Because a `RegexMatch` is trivially
fabricable (public fields, `Copy`), every conversion below is
bounds-CHECKED against the haystack it is given and carries NO
provenance claim beyond that check — same doctrine as
`JsonDoc.byte_range_view` (§9.1).

### Conversions (std.regex — which ALREADY imports std.text, so no
new dependency at all)

Declarations are COMPILE-REAL for std.regex's aliased import
(`import std.text as text;` — cross-module aliased struct types in
params and `core.Result` payloads probe-verified on 0.33.88: a
`core.Result<text.TokenSpan, text.TextError>` signature with a
`text.TextError` constructor compiles and runs).  All four functions
are ADDED TO std.regex's `export { … }` block in the same change —
an unexported pub fn is invisible across the module boundary.

```drift
// Checked, opt-in: match over a String -> retained view of it.
pub fn match_view(m: RegexMatch, s: &String) nothrow
    -> core.Result<text.StringByteView, text.TextError>;
//   malformed (m.start < 0 or m.end < m.start) and out-of-range
//   spans -> Err(text.TextError(tag = "out-of-bounds", offset = m.start));
//   otherwise delegates to text.byte_view(s, m.start, m.end - m.start).

// Checked: match over a VIEW -> subview (offsets compose; one retain).
pub fn match_subview(m: RegexMatch, v: &text.StringByteView) nothrow
    -> core.Result<text.StringByteView, text.TextError>;
//   m is VIEW-RELATIVE against v; delegates to
//   v.subview(m.start, m.end - m.start) — identical error shape.
```

### Matching over views — DECISION: SHIP

```drift
pub fn is_match_view(re: &Regex, v: &text.StringByteView) nothrow -> Bool;
pub fn find_first_view(re: &Regex, v: &text.StringByteView) nothrow
    -> Optional<RegexMatch>;
```

(Both likewise added to the `export` block; the export list follows
the file's existing single-entry-per-line style with no trailing
comma.)

Rationale (honest, from the measurements below): the value is
avoiding materialization of the SUBJECT substring — today "regex over
a slice" costs a full `substring` copy (~96 ns/token + O(n) bytes)
before matching even starts; over a view it costs zero allocations.
The engine reads through the composed view path (backing,
start-offset) — same guarded reads it does today.

Pinned semantics:
* Match offsets in results from `*_view` forms are **VIEW-RELATIVE**
  (consistent with §5's view-relative convention; composes directly
  with `match_subview`).
* Anchors `^`/`$` anchor at the VIEW's boundaries — the view IS the
  subject string.
* Existing String entry points are untouched.

### One matcher authority (pinned; compile-proven)

String and view entry points share ONE engine body — no duplication
of `_find_from`/`_try_match_at` logic.  The authority is
parameterized as a RANGE TRIPLE `(s: &String, base: Int, len: Int)`:

* `_try_match_at`/`_find_from` become range-triple internals (reads
  at `base + pos`, positions relative to the range);
* String entries pass `(input, 0, byte_length)` — bit-identical
  behavior, and the existing zero-retain property is regression-pinned
  (§10);
* view entries pass a `text._StringByteSource` (EXPORTED-INTERNAL
  plumbing per the repository underscore convention) — a borrowed,
  PRIVATE-FIELD byte window (`{bytes: &String, base, len}`, borrow
  fields per the LocatedCursor precedent) with a range-guarded,
  fail-closed `read` — built by `_byte_source(v)`; String entries use
  `_byte_source_all(s)` (borrow, zero retains).  REVISED in the
  post-checkpoint review round: the earlier `backing_ref()` accessor
  was rejected as a capability leak (a NARROW view could expose its
  ENTIRE backing) — the source abstraction keeps the backing private
  and bounds every read to the window, at zero cost (probe: zero
  retain/release/alloc in IR; reads are one guarded intrinsic call
  with a composed index).

COMPILE-PROVEN (probe binary runs; emitted IR inspected): a
borrow-returning accessor (`-> &String` from `&self`) compiles and
runs on 0.33.88, and the shared core + view entry + accessor contain
ZERO `drift_string_retain`/`drift_string_release`/`drift_alloc_array`
call sites — no retain, no boxed callback, no allocation anywhere in
the abstraction.  Had this shape not been expressible, the pinned
answer was STOP, not engine cloning.

### Regex-shaped benchmark (measured)

Two measurements.  First, a 3-state DFA proxy (`[a-z]+[0-9]+`:
per-byte class test + state transition) over the same 2 MB input
documents the read-bound ceiling only:

| variant | median | vs indexed |
|---|---|---|
| DFA via indexed `string_byte_at` | 2,519 µs | 1.0× |
| DFA via ONE bulk window per search | 744 µs | 3.4× faster |

Second — because the proxy comparison alone cannot bound the ENGINE's
read share (different read counts) — a TEMPORARY REAL-ENGINE
BULK-READ VARIANT was built and measured: a verbatim local replica of
the NFA executor (compiled from the exported `Regex.root`; identical
`_add_state`/`_byte_matches`/`_try_match_at` logic) run with its byte
reads switched between `string_byte_at` and raw pointer reads inside
one `with_bytes` window per search pass.  All three checksum-equal on
the 2 MB input (3 iterations, medians):

| variant | median | note |
|---|---|---|
| stock engine (`_find_from` loop) | 171,325 µs | baseline |
| replica, `string_byte_at` reads | 169,012 µs | fidelity check: within 1.4% of stock |
| replica, bulk-window reads | 165,827 µs | **1.9% faster than the replica** |

MEASURED conclusion: converting the engine to bulk-window reads is
worth ~1.9% — the engine's time is per-byte NFA bookkeeping (bitmap
rebuilds and a per-byte seeds `Array` allocation in `_try_match_at`),
not byte reads.  Engine bulk conversion is therefore OUT OF SCOPE on
evidence, and the engine's per-byte allocation is noted as a
SEPARATE, so-far-unmeasured optimization opportunity for a future
phase.  The regex adoption value in THIS phase is the view surface
above (no subject materialization), not engine speed.

### Counting & pins (extends §10 obligations)

* Existing `is_match`/`find_first`/`_find_from` over Strings: ZERO
  retains/releases (wrap-harness regression on the unchanged paths).
* `is_match_view`/`find_first_view`: ZERO retains/releases (read
  through `&StringByteView`), zero allocations attributable to the
  view path (engine-internal allocations are asserted UNCHANGED
  between String and view forms on the same subject).
* `match_view`/`match_subview`: EXACTLY ONE `drift_string_retain`,
  zero allocations.
* Lifetime pin: a view from `match_view` outlives the haystack
  BINDING (backing retained; memcheck-clean after dropping the
  original String).  The haystack MUST be heap-backed, non-static
  storage (built at runtime, e.g. concat/builder output) — literal
  backings are STATIC/immortal, where retain/release are no-ops and
  the proof would pass vacuously.  The same heap-backed requirement
  applies to every §10 retain-count fixture.
* Fabricated/invalid `RegexMatch` pins: negative `start`,
  `end < start`, `end > len` — every combination returns the checked
  `TextError`; NEVER a panic, UB, or ICE; fuzz-shaped negative matrix
  in the fixture.
* Empty-match pin: `start == end` (valid position) converts to a
  VALID empty view (`to_string` → the singleton, zero allocations).
* Nested-view pin: match over a subview → `match_subview` offsets
  compose; `to_string` of the result byte-equals the absolute-range
  `substring` of the root backing.

§11 correction round closed (rev 4): real-engine bulk variant
measured (1.9% — engine conversion out of scope on evidence), one
matcher authority compile-proven (range triple + borrow-returning
accessor, zero retain/callback/alloc in IR), signatures compile-real
with the text alias + export additions, §1/§9 regex statements
corrected, lifetime pins heap-backed.  Per the review disposition,
implementation GO — consolidated chunk, no further arm-selection
review, ONE final certification.


---

## Post-checkpoint retiming addendum (2026-07-25)

The `string_byte_at` OOB LANGUAGE_BUG fix (guarded MIR expansion +
unchecked in-bounds load; see the slice ledger) removed the per-byte
C `drift_bounds_check` call from every guarded read.  Retimed at
512 KiB: raw indexed scans ~2.5x FASTER than the §2 measurements
(218 µs vs a scaled ~535 µs), narrowing the bulk-vs-indexed gap from
~5.2x to ~2.0-2.1x and placing view reads at ~2.7-2.8x of raw.  The
§2 tables remain the honest PRE-fix record; the committed tier gate
carries the retimed bands.  Tier ORDER and all design decisions are
unchanged; the bulk window's per-window fixed cost is unaffected, so
the per-token-bulk antipattern guidance stands (the crossover only
moves upward).


## Post-checkpoint addendum 2 (2026-07-25): binding Result API + final byte-read measurements

Public API (binding review decision): `String.byte_at` and
`StringByteView.byte_at` return `Result<Byte, std.err:IndexError>`
and are NOTHROW — bounds failure is data.  The String method lives in
std.text (`implement String`; std.core cannot import std.err — cycle),
so callers import std.text.  `core.string_byte_at` is the
documented-internal fail-closed primitive (assert-diagnostic abort on
OOB; gating away from user code judged infeasible — 71 e2e fixtures
call it).  Unchecked in-bounds loads are mechanically validated at the
MIR→codegen boundary (unchecked_load_validator + 9 stage2 teeth).

Final one-table (512 KiB tokenize, medians, current tree):

| tier | median | × raw |
|---|---|---|
| raw primitive scan (w1) | 212 µs | 1.0× |
| direct bulk window (w2) | 101 µs | 0.48× |
| composed view bulk (w6) | 113 µs | 0.53× |
| internal source reads (w5b) | 448 µs | 2.1× |
| PUBLIC Result byte_at reads (w5) | 2,211 µs | **10.4×** |
| per-token view construction (w4) | 3,361 µs | 15.9× |
| per-token substring (w3) | 6,224 µs | 29.4× |

TARGET MISS (flagged for review): the ≤2× target for the public
Result path is NOT met — the delta over the identical-read source
path (448→2,211 µs ≈ 3.4 ns/byte) is Result ENUM machinery
(construction + match + outlined enum-drop of the
String-bearing-Err-arm temp per read), not duplicated range checks.
Optimizing enum/match cleanup lowering collides with the standing
ownership-lattice change bar and is out of this phase's scope.
Sanctioned hot paths sit at 2.1× (internal source) and ~0.5× (bulk);
the Result method is the SAFE/converse surface, not the hot loop.

Whole-workload compile impact (legacy-vs-final lowering flip on the
full-stdlib-closure adoption program, 3 runs each): wall 22.73 →
22.92 s (+0.8%), pre-opt IR +3.7%, optimized binary size IDENTICAL
(716,128 B) — not material.

Window-size crossover (final implementation): break-even ≈ 216 bytes
(fixed ~54 ns/window against 0.33 ns/B safe reads); the ONE
recommendation everywhere is: bulk windows from ~256 bytes up.


## Post-checkpoint addendum 3 (2026-07-25): enum/match cleanup optimization — the ≤2x target is MET

Per the review directive, the Result-tier miss was closed by GENERAL
authority-level optimization (no byte_at special case, same 0.33.88
candidate):

1. single-value VARIANT drops: by-value `alwaysinline`
   `__drift_variant_drop_<key>` (no element loop, no caller
   alloca/store) — tag-dominated paths fold to nothing, so
   Result::Ok(Copy) never pays for the inactive destructible Err arm;
   arrays keep the loop helper; drops remain exactly-once (behavior +
   IR-shape teeth in test_variant_drop_inline.py, valgrind twin in
   memcheck);
2. observation guard restructured: branch-lean hot path + ONE cold
   `noinline` fail dispatch preserving the six exact contract
   messages — the guard's inlined bulk had pushed every small String
   accessor past LLVM's inline threshold (byte_length: cost 260 vs
   225);
3. static allocas hoisted to entry blocks at emit_func (non-entry
   allocas made LLVM mark functions "never inline: dynamic alloca");
4. size-based `inlinehint` (MIR ≤ 64 instructions) so accessor-sized
   functions with a cold error arm inline into caller loops.

FINAL tier table (512 KiB, medians):
raw 215 | bulk 102/112 (0.5x) | source 247 (1.15x) |
StringByteView.byte_at 250 (1.16x) | String.byte_at 358 (1.66x) |
ViewBytesIter 342 (1.59x) | view/token 3,540 | substring 6,460.
BOTH public Result accessors meet the ≤2x target (HARD gate bands).

Whole-workload (full-stdlib-closure build): wall +0.8% vs legacy
lowering; optimized binary +7.0% (716,128 → 766,232 B) — the
inlining trade, recorded for review.  Validator hardened: the
unchecked load must be the FIRST instruction of its OK block
(release-insertion negative tooth added).


## Post-checkpoint addendum 4 (2026-07-25): soundness closures + ablation

Review closures on the optimization round:

1. **Alloca placement is now owning-site, not textual.**  The global
   textual hoist was removed (unsound in principle for any
   loop-local whose address semantics depend on per-iteration
   distinctness).  `_FuncBuilder._scratch_alloca` +
   the drop-helper generator's `scratch_alloca` register NONESCAPING
   transient slots (variant/struct pack-unpack, loop counters,
   callback slots — each fully re-stored before use, address never
   outliving its emitting lowering) for ENTRY placement; the iface
   drop helper's slot moved to its entry.  Teeth: module-wide
   no-non-entry-static-alloca scan + an ADDRESS-TAKEN loop-local
   CONTROL (the pointer is taken and consumed within its iteration —
   Drift's borrow rules forbid a genuinely iteration-escaping
   address from source; per-iteration values through the taken
   address stay correct).
2. **inlinehint narrowed structurally** — SUPERSEDED INTERIM
   description: this addendum's size-only rule (hot <= 48) was
   further narrowed in the follow-up round to SMALL + ACCESSOR SHAPE
   (variant return or cold-failure block; see _inline_hint_eligible
   and addendum 4's closing section) — the size-only rule and the
   758,320-byte table below are retained as interim evidence only.
3. **Guard order**: negative length now rejects BEFORE the flags
   dereference (a malformed {negative len, invalid non-NULL storage}
   handle produced a fault risk, not the pinned diagnostic);
   subprocess tooth with storage=0x8 added to the observation-guard
   battery.
4. **Variant controls**: user variant + Optional<String> with
   runtime-unknown tags (both arms, exactly-once, valgrind-proven)
   and an Array<String> loop-helper control (runtime-len call pinned
   in IR).

ABLATION — INTERIM EVIDENCE (measured on the superseded size-only
hint configuration whose FINAL artifact was 758,320 B; the final
small+shape configuration measures 746,040 B — see the closing
section) (full-stdlib-closure build; bytes / view-byte_at /
String-byte_at):

| configuration | binary | w5 view | w5a String |
|---|---|---|---|
| FINAL (entry allocas + lean guard + by-value drops + hint<=48-hot) | 758,320 | ~400 µs (1.9×) | ~352 µs (1.6×) |
| no inlinehint | 742,136 (−2.1%) | 866 (4.1×) | 973 (4.5×) |
| blanket <=64-total hint (REJECTED) | 766,232 (+1.0%) | 250 | ~356 |
| guard fail-arms inlined (pre-lean) | 766,464 (+1.1%) | 872 (4.0×) | 1,428 (6.5×) |
| variant drops via len=1 loop helper | 765,128 (+0.9%) | 664 (3.0×) | 353 |
| (alloca fix in isolation, earlier measurement) | — | 2,211 → 874 | — |

Whole-workload FINAL — BOTH baselines, always reported together
(measured on the final shape-narrowed configuration): wall
22.63–22.74 s (parity with legacy 22.73); binary 746,040 B —
vs the NO-HINT candidate: **+0.5%** (742,136); vs the
LEGACY-lowering artifact: **+4.2%** (716,128) — the full phase cost.
(The interim ≤48-size-only hint measured 758,320 = +2.2%/+5.9%; the
final small+shape predicate reclaimed most of that.)  Ablation
deltas: the no-hint row saves 2.1% from the interim point; other
single-dimension removals save at most 1.1% — while costing 2–6× on
an accessor tier.

Post-review protocol change: the tier gate uses SAME-LAUNCH
median/median ratios across several fresh launches and requires
EVERY launch to satisfy the hard bands (no minima selection — a
review correction; a slow launch mode must fail, not be filtered).
The earlier `String.byte_at` bimodality (~352/~440 µs) DISAPPEARED
with the final shape-narrowed inlinehint (small + variant-return /
cold-failure shape only — see _inline_hint_eligible and its boundary
teeth); 8/8 probe launches sat at 1.66–1.70×.  Final per-launch
tiers: view ~1.9×, String ~1.7×, iterator ~1.4×, source ~1.04×,
bulk ~0.48×.


## Post-checkpoint addendum 5 (2026-07-25): shipped-API crossover + second baseline promotion

The crossover protocol was migrated to the SHIPPED APIs
(StringByteView.byte_at Result unwrap as the safe tier; per-window
subview + with_view_bytes as the bulk tier, per-window
construction/drop preserved).  Measured: safe ~1.8-1.9 ms flat; bulk
12.9 ms @8 B -> 0.92 ms @128 B -> 372 us @512 B -> 186 us @64 K;
BREAK-EVEN ~64-96 bytes.  Guidance updated everywhere (maintainer-
approved) to "bulk from ~128 bytes"; the earlier raw-primitive-shaped
~216 B / 256 B guidance is superseded (it was conservative-safe, not
wrong).

Baseline promotion 2: run dir ownership-corpus-20260725-070420-2045579
promoted into reviewed-baseline (maintainer-approved, byte-exact
validated, residual-zero attribution — see BASELINE.md's promotion
record); comparison semantics unchanged; zero-delta proven against
the promoted artifacts and the corpus teeth pass.
