# std.regex Plan (stdlib)

## 0) Objective
Add practical regex capability to stdlib with explicit safety/performance bounds, starting with a research gate on `std.text` parser primitives.

## 1) Phase-1 Gate: Research + Gap Review (Klaudia)
Status: `in_progress`

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
Status: `pending`

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
Status: `pending`

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
Status: `pending`

### 4.1 Empty-match contract (must be documented + tested)
- `compile("")` is valid.
- `is_match(compile(""), input)` returns `true` for any input.
- `find_first` may return zero-length matches; when present, `start <= end` and `end - start` may be `0`.
- `replace_all` with patterns that can match empty strings must guarantee forward progress (no infinite loop).

### 4.2 Match indexing
- `start` inclusive, `end` exclusive.
- Byte offsets (UTF-8 byte semantics), not codepoint offsets.

## 5) Architecture + Ownership
Status: `pending`

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
Status: `pending`
- Add approved helper primitives to `std.text` (initial agreed batch):
  - `is_digit(Byte) -> Bool`
  - `is_alpha(Byte) -> Bool`
  - `is_alnum(Byte) -> Bool`
  - `is_space(Byte) -> Bool`
- Evaluate (not mandatory for A1): `index_of_byte`, `starts_with`, `contains`.
- Implement tokenizer with offset tracking.
- Add tokenizer tests for escapes, classes, malformed tokens.

### Phase B: parser + AST
Status: `pending`
- Implement precedence-aware parser (`|`, concat, quantifiers).
- Emit `RegexError` with exact byte offset.
- Add valid/invalid pattern tests.

### Phase C: compiler + matcher
Status: `pending`
- Lower AST to executable program.
- Implement `is_match` and `find_first`.
- Freeze and test empty-match behavior from §4.

### Phase D: replacement
Status: `pending`
- Implement `replace_first` and `replace_all`.
- Add forward-progress guard for zero-length matches in replace loops.
- Decide whether `find_all` stays deferred or gets added.

### Phase E: hardening
Status: `pending`
- ASAN/MEMCHECK for regex suites.
- Stress/perf guards for adversarial inputs.
- Document complexity and v1 exclusions.

## 7) Testing Plan (expanded)
Status: `pending`

### 7.1 Regex tests
- `lang/tests/codegen/e2e/std_regex_compile_errors/`
- `lang/tests/codegen/e2e/std_regex_is_match_basic/`
- `lang/tests/codegen/e2e/std_regex_find_first/`
- `lang/tests/codegen/e2e/std_regex_replace/`
- `lang/tests/codegen/e2e/std_regex_empty_match_contract/`
- `lang/tests/codegen/e2e/std_regex_stress_guard/`

### 7.2 std.text helper tests (new public API coverage)
- `lang/tests/codegen/e2e/std_text_charclass_helpers/`
- Cover positive/negative samples for `is_digit/is_alpha/is_alnum/is_space`.

## 8) Versioning + docs
Status: `pending`

- Public stdlib API expansion in `std.text`/`std.regex` requires `DRIFTC_VERSION` minor bump when landed.
- ABI bump not expected unless compiler/runtime boundary shape changes.
- Update `docs/history.md` for each landed phase.

## 9) Completion Criteria
- `research.md` completed and approved (Phase-1 gate).
- Phase A helper APIs + tests merged.
- Regex MVP APIs implemented with frozen empty-match semantics.
- ASAN/MEMCHECK clean for regex and new text-helper suites.
- History/version updates completed per policy.
