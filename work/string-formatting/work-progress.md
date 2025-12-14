# String interpolation (`f"..."`) — work tracker (lang2)

This tracker follows `docs/design/spec-change-requests/feature-string-interpolation.md` and captures what’s left to deliver production-quality f-strings in `lang2/`.

## Locked decisions (this MVP)

- f-strings are a **compiler feature** for now (no `std.fmt` module yet).
- Only `f"..."` is supported; `'` is reserved for single-character input.
- Float formatting will be deterministic via **Ryu**, but **`Float` is not implemented end-to-end in lang2 yet**, so float holes are deferred.

## Scope and invariants (MVP)

- Surface syntax: `f"..."` with holes `{ <expr> [ ":" <spec> ] }` and brace escaping via `{{` / `}}`.
- Evaluation order: holes evaluate left-to-right, exactly once.
- Typing (MVP): hole expressions must be one of `Bool`, `Int`/`Uint`, `String`.
  - Float is deferred until `Float` exists as a first-class surface type.
- Spec string: compile-time substring (no nested `{}` in MVP); non-empty spec currently yields a compile-time error.
- Lowering contract (current implementation):
  - The compiler lowers f-strings into explicit string literals + per-hole formatting + concatenation.
  - No public `std.fmt.interpolate` surface exists yet.

## Work items

### 0) Spec and docs
- [ ] Update `docs/design/drift-lang-spec.md` to document f-strings (syntax, escaping, typing, diagnostics, and current compiler lowering strategy).
- [ ] Add/adjust `docs/design/drift-lang-abi.md` if/when we standardize a public formatting ABI (deferred until module support / `std.fmt` exists).

### 1) Lexer + parser
- [x] Lexer: recognize `f` immediately followed by `"` as an f-string start (no whitespace).
- [x] Parser: parse f-string body into segments (text vs holes) with spans.
- [x] Parser errors:
  - [x] `E-FSTR-UNBALANCED-BRACE` for unescaped `{`/`}`.
  - [x] `E-FSTR-EMPTY-HOLE` for `{}`.
  - [x] `E-FSTR-NESTED` for `{`/`}` inside `:spec` (MVP disallow).

### 2) AST / stage0 adapter
- [x] Add AST node(s) for f-strings (`FString(parts, holes)` + `FStringHole`).
- [x] Ensure f-string holes carry structured `Span` for diagnostics.
- [x] Adapter: lower parser AST f-string into stage0 representation.

### 3) AST→HIR lowering (stage1)
- [x] Keep f-strings as a dedicated HIR node (`HFString`) until stage2.
- [x] Preserve evaluation order: hole expressions are lowered left-to-right.

### 4) Type checking and diagnostics (lang2 type checker)
- [x] Enforce allowed hole value types (MVP set).
- [x] Emit `E-FSTR-UNSUPPORTED-TYPE` when unsupported.
- [x] Validate `:spec` presence:
  - [x] `E-FSTR-BAD-SPEC` when spec is non-empty (MVP).
- [x] Ensure all diagnostics are span-anchored (no `None` spans).

### 5) Standard library surface (`std.fmt`) (deferred)
- [ ] Implement `std.fmt` as a real Drift module once module support exists.

### 6) Runtime support (C) for formatting primitives
- [x] Int/Uint:
  - [x] `drift_string_from_int64` / `drift_string_from_uint64`.
- [x] Bool:
  - [x] `drift_string_from_bool`.
- [x] Float (prepared for later):
  - [x] Vendor Ryu and expose `drift_string_from_f64` (not used until `Float` exists in lang2).
- [x] Ownership: returned `DriftString` uses the existing allocation rules.

### 7) Codegen / linking
- [x] Ensure e2e runner links runtime C sources needed for formatting (`ryu_d2s.c` included for future Float).
- [x] Lowering: stage2 expands f-strings into `StringConcat` and `StringFrom*` MIR ops.
- [x] LLVM codegen: lowers `StringFromInt/Uint/Bool` to runtime helpers.

### 8) Tests (parser → checker → e2e)
- [x] Parser tests:
  - [x] Brace error cases (`E-FSTR-UNBALANCED-BRACE`).
  - [x] Empty hole (`E-FSTR-EMPTY-HOLE`).
  - [x] Nested braces in `:spec` (`E-FSTR-NESTED`).
- [ ] Checker tests:
  - [ ] `f"{1}"` OK.
  - [ ] `f"{some_struct}"` => `E-FSTR-UNSUPPORTED-TYPE`.
  - [ ] `f"{1:wat}"` => `E-FSTR-BAD-SPEC`.
- [ ] E2E tests (Drift → binary):
  - [x] Basic: `fstring_basic` (holes for Int/Bool/String and `{{}}` escaping).
  - [x] Negative: `fstring_bad_spec` expects `E-FSTR-BAD-SPEC`.
  - [x] Negative: `fstring_bad_type` expects `E-FSTR-UNSUPPORTED-TYPE`.
  - [ ] Spec formatting (hex/width) deferred (requires real spec parser/formatter policy).
  - [ ] Float formatting deferred until `Float` exists in lang2.

## Current status

- Implemented:
  - Parsing + AST + HIR for `f"..."` with holes and brace escaping.
  - Type-checking: holes type to `String`, and non-empty spec errors (`E-FSTR-BAD-SPEC`).
  - MIR + LLVM lowering: concat + primitive-to-string helpers.
  - E2E coverage (basic + bad spec).
  - Ryu vendored into runtime for future deterministic Float formatting.
- In progress:
  - Add parser-only unit tests (brace/empty-hole/nested-spec errors).
- Deferred:
  - `std.fmt` module surface.
  - `Float` surface type + float holes.
