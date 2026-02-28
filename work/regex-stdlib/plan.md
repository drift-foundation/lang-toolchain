# std.regex Plan (stdlib)

## 0) Objective
Add practical regex capability to stdlib with explicit safety/performance bounds, starting with a research gate on `std.text` parser primitives.

## 1) Phase-1 Gate: Research + Gap Review (Klaudia)
Status: `complete`

### 1.1 Required deliverable
Create `work/regex-stdlib/research.md` before implementation starts.

Must include:
- Primitive inventory with file/function references (`std.text`, `std.core`, `std.iter`, `std.io`, `std.mem`).
- Missing primitive matrix ranked by severity: blocker/high/medium/deferred.
- Answers to all gate questions in §1.2.
- Recommendation: tokenizer-first vs direct parser-over-bytes.
- Explicit go/no-go for Phase A.

### 1.2 Gate questions (must be answered in research.md)
- Can tokenizer/parser operate with index-only byte scans (no substring extraction) to avoid O(n^2) allocations?
- Is `substring` sufficient for v1, and where does it become a bottleneck?
- Which helper APIs are required before regex coding begins?
- What error type/domain split should be used (`RegexError` vs reusing `TextError`)?
- What is the plan for escapes/classes parsing with current primitives?

### 1.3 Agreed baseline from review
- Tokenizer/parser can run with `string_byte_at + index` (no slice allocations required).
- `substring` is acceptable for v1 parser/tokenizer, but may bottleneck `replace_all`.
- `RegexError` stays distinct from `TextError` (separate error domains).

## 2) MVP Scope (v1)
Status: `complete`

### 2.1 Included syntax
- Literals
- Dot `.`
- Anchors `^` and `$`
- Character classes `[abc]`, ranges `[a-z]`, negation `[^...]`
- Escapes for metachar literals and common classes (`\\d`, `\\w`, `\\s`) if feasible
- Grouping `(...)`
- Alternation `|`
- Quantifiers `*`, `+`, `?`

### 2.2 Out of scope
- Backreferences
- Lookahead/lookbehind
- Named groups
- Unicode property classes

## 3) API Contract (updated per review)
Status: `complete`

Module target: `stdlib/std/regex/regex.drift`

- `pub struct Regex`
- `pub struct RegexMatch { start: Int, end: Int }`
- `pub struct RegexError { tag: String, offset: Int }`
- `pub fn compile(pattern: &String) nothrow -> core.Result<Regex, RegexError>`
- `pub fn is_match(re: &Regex, input: &String) nothrow -> Bool`
- `pub fn find_first(re: &Regex, input: &String) nothrow -> Optional<RegexMatch>`
- `pub fn replace_first(re: &Regex, input: &String, repl: &String) nothrow -> String`
- `pub fn replace_all(re: &Regex, input: &String, repl: &String) nothrow -> String`

Notes:
- `compile` is `nothrow`; pattern errors are reported via `Result::Err(RegexError)`.
- Runtime matcher/replacement APIs do not return `RegexError`; they consume already-compiled regex.
- `replace_*` in v1 performs literal replacement only (no capture interpolation).
- `find_all` is explicitly deferred to v2 unless Phase D requires public exposure.

## 4) Semantics to freeze before Phase C
Status: `complete` — frozen and tested in std_regex_zero_length_progress + std_regex_replace

### 4.1 Empty-match contract (must be documented + tested)
- `compile("")` is valid.
- `is_match(compile(""), input)` returns `true` for any input.
- `find_first` may return zero-length matches; when present, `start <= end` and `end - start` may be `0`.
- `replace_all` with patterns that can match empty strings must guarantee forward progress (no infinite loop).

### 4.2 Match indexing
- `start` inclusive, `end` exclusive.
- Byte offsets (UTF-8 byte semantics), not codepoint offsets.

### 4.3 Match selection rule (frozen — pinned in std_regex_gotchas_greediness)
- `find_first` returns the **leftmost** match position. At that position, the **longest** possible match is selected (greedy). This is the "leftmost-longest" rule.
- Start positions are tried left-to-right (0, 1, 2, ...).
- At the first start that yields any match, `_try_match_at` returns the longest match end.
- Shorter matches at the same start are never returned.
- Matches at later start positions are never considered once an earlier start matches (even if zero-length).
- **Alternation**: Thompson NFA explores all branches simultaneously and returns the longest overall match, not the first-branch match. Branch order does not affect which match is selected.

### 4.4 Greediness contract (frozen — pinned in std_regex_gotchas_greediness)
- `*`, `+`, `?` are all greedy. There are no non-greedy/lazy quantifiers in v1.
- `Split(a=body, b=skip)` tries body first (greedy preference).
- `a.*b` on input with multiple b's matches to the **last** b (not the first).
- `a?` prefers matching `a` over zero-length when `a` is available.

### 4.5 Character class edge semantics (frozen — pinned in std_regex_gotchas_class_edges)
- `-` at start of class → literal hyphen.
- `-` at end of class (before `]`) → literal hyphen.
- `-` between two chars → range (lo-hi).
- `\-` anywhere → literal hyphen (escaped).
- `]` as first character after `[` or `[^` → literal `]`.
- `\]` anywhere → literal `]`.
- `^` immediately after `[` → negation.
- `^` elsewhere in class → literal caret.

### 4.6 Byte-offset guarantees (frozen — pinned in std_regex_gotchas_utf8_offsets)
- All offsets (`RegexMatch.start`, `RegexMatch.end`) are byte offsets into the UTF-8 encoded string, NOT codepoint indices.
- Dot (`.`) matches one byte, not one codepoint.
- Character classes match single bytes.
- `replace_*` preserves byte-offset accounting; replacement text is spliced at byte boundaries.
- Operating on multibyte characters may produce invalid UTF-8 (this is by design — regex operates on raw bytes).

## 5) Architecture + Ownership
Status: `complete`

### 5.1 Execution model
- Pattern parse -> AST
- AST lower -> compact NFA-like instruction array
- Execute via bounded-state simulation (no recursive backtracking engine)

### 5.2 Regex ownership model
- `Regex` will own compiled instruction/state arrays (heap-backed `Array<...>`).
- Default structural drop expected to be sufficient unless profiling/failure indicates otherwise.
- If custom destruction is introduced later, add dedicated regression coverage for drop lifecycle.

## 6) Implementation Phases

### Phase A: std.text helper batch + tokenizer
Status: `complete`
- Add approved helper primitives to `std.text` (initial agreed batch):
  - `is_digit(Byte) -> Bool`
  - `is_alpha(Byte) -> Bool`
  - `is_alnum(Byte) -> Bool`
  - `is_space(Byte) -> Bool`
- Evaluate (not mandatory for A1): `index_of_byte`, `starts_with`, `contains`.
- Implement tokenizer with offset tracking.
- Add tokenizer tests for escapes, classes, malformed tokens.

### Phase B: parser + AST
Status: `complete`
- Implement precedence-aware parser (`|`, concat, quantifiers).
- Emit `RegexError` with exact byte offset.
- Add valid/invalid pattern tests.

### Phase C: compiler + matcher
Status: `complete`
- Lower AST to executable program.
- Implement `is_match` and `find_first`.
- Freeze and test empty-match behavior from §4.

### Phase D: replacement
Status: `complete`
- Implemented `replace_first` and `replace_all` with literal replacement semantics.
- Forward-progress guard for zero-length matches: advance cursor by 1 byte on empty match.
- `find_all` stays deferred to v2.
- Added `_find_from` (search from offset) and `_substr` (nothrow substring via io.buffer) helpers.

### Phase E: hardening
Status: `complete`
- All 9 regex e2e suites + charclass helpers pass clean under ASAN (`DRIFT_ASAN=1`).
- All 9 regex e2e suites + charclass helpers pass clean under valgrind memcheck (`DRIFT_MEMCHECK=1`).
- Stress guards added to `std_regex_replace`: 200-byte replace_all, .+ greedy on long input, 100-repeat two-char replace, empty-pattern on 50 bytes (51 insertions), a* zero-length on 100 bytes, replace_first on 200 bytes.
- v1 exclusions documented in §2.2; complexity is O(n*m) backtracking engine with no pathological-input guard beyond forward-progress for empty matches.

## 7) Testing Plan (expanded)
Status: `complete`

### 7.1 Regex tests (actual suites)
- `lang/tests/codegen/e2e/std_regex_compile_valid/` — valid pattern compilation
- `lang/tests/codegen/e2e/std_regex_compile_errors/` — invalid pattern error tags + offsets
- `lang/tests/codegen/e2e/std_regex_is_match_semantics/` — is_match across pattern types
- `lang/tests/codegen/e2e/std_regex_find_first_offsets/` — find_first byte offset correctness
- `lang/tests/codegen/e2e/std_regex_anchor_behavior/` — ^/$ anchor semantics
- `lang/tests/codegen/e2e/std_regex_quantifier_behavior/` — *, +, ? greedy semantics
- `lang/tests/codegen/e2e/std_regex_class_escape_behavior/` — char classes + escape sequences
- `lang/tests/codegen/e2e/std_regex_zero_length_progress/` — empty-match forward progress
- `lang/tests/codegen/e2e/std_regex_parser_corners/` — parser corner cases via node inspection
- `lang/tests/codegen/e2e/std_regex_replace/` — replace_first + replace_all + stress guards
- `lang/tests/codegen/e2e/std_regex_gotchas_greediness/` — greediness tie-break + match-selection contract pin
- `lang/tests/codegen/e2e/std_regex_gotchas_class_edges/` — character class parser edge cases (-, ], ^, escapes)
- `lang/tests/codegen/e2e/std_regex_gotchas_utf8_offsets/` — UTF-8 byte-offset correctness for find/replace
- `lang/tests/codegen/e2e/std_regex_stress_compile_growth/` — compile-time growth (large alternation/concat)
- `lang/tests/codegen/e2e/std_regex_stress_adversarial/` — adversarial pattern/input pairs for regression detection

### 7.2 std.text helper tests
- `lang/tests/codegen/e2e/std_text_charclass_helpers/`
- Covers positive/negative samples for `is_digit/is_alpha/is_alnum/is_space`.

### 7.3 Compiler regression tests (discovered during regex work)
- `lang/tests/codegen/e2e/array_push_move_non_copy_implicit/` — non-Copy Array.push/insert move semantics
- `lang/tests/codegen/e2e/match_qualified_binder_local/` — match binder rename in loops/casts

## 8) Versioning + docs
Status: `complete`

- DRIFTC_VERSION bumped from 0.8.0-dev → 0.9.0-dev (in `lang/driftc/driftc_versions.py`).
- `docs/history.md` updated with all fixes and std.regex stdlib entry.

## 9) Completion Criteria
- [x] `research.md` completed and approved (Phase-1 gate).
- [x] Phase A helper APIs + tests merged.
- [x] Regex MVP APIs implemented with frozen empty-match semantics.
- [x] ASAN/MEMCHECK clean for regex and new text-helper suites.
- [x] History/version updates completed per policy.

**All completion criteria met. std.regex v1 is ready for review.**
