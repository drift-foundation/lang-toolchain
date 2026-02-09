# Stdlib Parse Work Progress

## Goal

Provide shared scalar parsing/conversion APIs for Drift stdlib consumers (`std.json`, config/env loaders, protocol/client libraries).

## MVP Scope

1. `std.parse` module with machine-friendly parse surface.
2. Parsers for `Bool`, `Int`, `Uint`, `Float`.
3. Structured parse error event with stable machine tags.
4. Deterministic, locale-independent behavior.

## Draft API

- `parse_bool(s: &String) -> Result<Bool, ParseError>`
- `parse_int(s: &String) -> Result<Int, ParseError>`
- `parse_uint(s: &String) -> Result<Uint, ParseError>`
- `parse_float(s: &String) -> Result<Float, ParseError>`

## Error Model

- Single event: `std.parse:ParseError`
- Required `tag` field (kebab-case, append-only namespace), examples:
  - `invalid-syntax`
  - `invalid-digit`
  - `overflow`
  - `underflow`
  - `invalid-datatype`
- Structured context fields (`offset`, `input`, `target`, `reason`).

## Semantics Pin

- Non-panicking parsing surface.
- No locale-sensitive behavior.
- No acceptance of trailing junk unless an explicit API is added later.
- `std.json` numeric/bool extractors must delegate to this module.
- `parse_bool` contract (MVP):
  - accepted true tokens (case-insensitive): `true`, `yes`, `on`, `1`
  - all other exact inputs evaluate to `false`
  - no whitespace trimming in parser scope.

## Regression-First Test Plan

### Unit

1. Valid bool/int/uint/float parses.
2. Boundary numeric values.
3. Rejection of malformed and trailing-junk inputs.

### Negative

1. Overflow/underflow tags.
2. Invalid syntax/digit tags.

## Exit Criteria

- `std.parse` APIs stable and covered.
- `std.json` can depend on these parsers without duplicating numeric logic.

## Progress Update

- Implemented `std.parse` module at `stdlib/std/parse/parse.drift`.
- Implemented and validated:
  - `parse_bool(s: String) -> Bool`
  - `parse_int(s: String) -> Result<Int, ParseError>`
  - `parse_uint(s: String) -> Result<Uint, ParseError>`
  - `parse_float(s: String) -> Result<Float, ParseError>`
- Added `ParseError` with public machine fields:
  - `tag: String`
  - `offset: Int`
- Added/validated regression coverage:
  - driver: `lang/tests/driver/test_std_parse_api.py`
  - e2e: `std_parse_bool_contract`, `std_parse_numeric_contract`
- Unblocked float arithmetic path in LLVM lowering:
  - `BinaryOp` float ops (`fadd/fsub/fmul/fdiv`, `fcmp`)
  - unary float negation (`fsub 0.0, x`)
