# std.cli (MVP) – work progress

## Goal
- Add a minimal, deterministic command-line argument parser to stdlib so Drift apps do not reinvent common CLI parsing patterns.
- Keep API strict and explicit for MVP; no hidden env/config fallback behavior.

## Why now
- CLI parsing is foundational for real apps.
- Current gap forces every app/library to hand-roll `--long`/`-s`/positional/help/version parsing.

## Scope (MVP)

### In scope
- Raw argv exposure from runtime.
- Declarative parser in stdlib with:
  - flags (`--verbose`, `-v`)
  - value options (`--port 3306`, `--port=3306`, `-p 3306`)
  - positional args
- Auto help/version handling.
- Deterministic diagnostics and usage text.

### Out of scope (post-MVP)
- Subcommands.
- Optional-value flags (e.g. `--color[=auto]`).
- Repeated options accumulation lists.
- Shell completion generation.
- Env/config integration.

## Proposed API shape

```drift
module std.cli

import std.core as core;

pub struct CliError {
        tag: String,      // e.g. "cli-unknown-option"
        arg: String,      // offending token (or "")
        message: String,  // stable human-readable detail
        index: Int        // argv index for deterministic pinpointing
}

pub struct ParsedArgs { ... } // opaque in MVP

pub struct ArgParser { ... } // builder-style

pub fn parser(app: String, version: String, about: String) nothrow -> ArgParser;

implement ArgParser {
        pub fn flag(var self, long: String, short: String, help: String) nothrow -> ArgParser;
        pub fn option_string(var self, long: String, short: String, value_name: String, help: String, required: Bool) nothrow -> ArgParser;
        pub fn option_int(var self, long: String, short: String, value_name: String, help: String, required: Bool) nothrow -> ArgParser;
        pub fn positional(var self, name: String, help: String, required: Bool, multiple: Bool) nothrow -> ArgParser;
        pub fn parse(self, argv: &Array<String>) nothrow -> core.Result<ParsedArgs, CliError>;
        pub fn help_text(&self) nothrow -> String;
}

implement ParsedArgs {
        pub fn has_flag(&self, long: String) nothrow -> Bool;
        pub fn get_string(&self, long: String) nothrow -> core.Option<String>;
        pub fn get_int(&self, long: String) nothrow -> core.Option<Int>;
        pub fn positional_at(&self, idx: Int) nothrow -> core.Option<String>;
        pub fn positional_len(&self) nothrow -> Int;
}
```

Notes:
- `short` is `""` when omitted (MVP simplification; no `Option<String>` noise in schema).
- Long names are stored without `--` prefix (`"verbose"`, `"port"`).
- `option_*` keys are resolved by long name only in getters.

## Parsing contract (strict)

### Accepted forms
- Long flag: `--verbose`
- Long option: `--port=3306`, `--port 3306`
- Short flag: `-v`
- Short option: `-p 3306`
- Combined short flags (`-abc`): **not supported in MVP** (error with clear tag).

### Terminator
- `--` ends option parsing; remaining tokens are positional.

### Errors (deterministic tags)
- `cli-unknown-option`
- `cli-missing-option-value`
- `cli-invalid-int`
- `cli-missing-required-option`
- `cli-missing-required-positional`
- `cli-duplicate-option`
- `cli-invalid-schema` (builder misuse such as duplicate long/short declarations)
- `cli-unsupported-short-cluster` (for `-abc` in MVP)

### Help/version behavior
- Parser reserves:
  - `--help`, `-h`
  - `--version`, `-V`
- If present, `parse` returns `Err` with tags:
  - `cli-help-requested`
  - `cli-version-requested`
- Caller decides print/exit behavior (keeps stdlib side effect free).

## Runtime dependency
- Use runtime argv plumbing already present via std/process surface.
- Add convenience in std.cli examples with explicit `argv` passing (no hidden globals).

## Implementation plan

1. Create `stdlib/std/cli/cli.drift` with parser skeleton and schema validation. ✅
2. Implement strict tokenizer/parser loop with deterministic index tracking. ✅
3. Add typed conversion path for `option_int`. ✅
4. Implement stable `help_text()` formatting. ✅
5. Wire stdlib exports. ✅ (module import path active as `std.cli`)
6. Add e2e + ASAN + alloc-track coverage. ✅ (initial matrix subset completed)

## Current implemented API (actual)
- `std.cli::parser(app, version, about) -> ArgParser`
- `ArgParser`:
  - `flag(long, short, help)`
  - `option_string(long, short, value_name, help, required)`
  - `option_int(long, short, value_name, help, required)`
  - `positional(name, help, required, multiple)`
  - `help_text()`
  - `parse(argv) -> Result<ParsedArgs, CliError>`
- `ParsedArgs`:
  - `has_flag(long, parser)`
  - `get_string(long, parser) -> Optional<&String>`
  - `get_int(long, parser) -> Optional<Int>`
  - `positional_at(idx) -> Optional<&String>`
  - `positional_len() -> Int`

Note:
- `ParsedArgs` getters currently take `parser` as an argument to resolve schema indices.

## Regression matrix (must-have tests)

### Happy path
- `std_cli_basic_flags_options_positionals`
- `std_cli_double_dash_terminator`
- `std_cli_help_requested`

### Error path
- `std_cli_unknown_option`
- `std_cli_invalid_int_option`
- `std_cli_missing_required_option`
- `std_cli_missing_required_positional`
- `std_cli_duplicate_option`
- `std_cli_short_cluster_rejected`

### Determinism/memory
- Run key e2e set under `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`
- Run one memcheck pass over representative parser cases

Completed validation:
- normal e2e:
  - `std_cli_basic_flags_options_positionals`
  - `std_cli_double_dash_terminator`
  - `std_cli_help_requested`
  - `std_cli_unknown_option`
  - `std_cli_missing_required_option`
  - `std_cli_missing_required_positional`
  - `std_cli_duplicate_option`
  - `std_cli_invalid_int_option`
  - `std_cli_short_cluster_rejected`
- `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`:
  - `std_cli_basic_flags_options_positionals`
  - `std_cli_double_dash_terminator`
  - `std_cli_help_requested`
  - `std_cli_unknown_option`
  - `std_cli_missing_required_option`
  - `std_cli_missing_required_positional`
  - `std_cli_duplicate_option`
  - `std_cli_invalid_int_option`
  - `std_cli_short_cluster_rejected`
- `DRIFT_MEMCHECK=1`:
  - `std_cli_basic_flags_options_positionals`
  - `std_cli_help_requested`

## Open decisions (pinned)
- Keep parser strict by default: **yes**.
- Return help/version as tagged parse errors instead of printing/exiting internally: **yes**.
- Support short flag clusters in MVP: **no** (explicitly rejected with dedicated tag).
- Subcommands in MVP: **no**.

## Pending follow-up (LANGUAGE_BUG)
- `&String` equality on ordinary expression paths is still inconsistent in checker/type resolution (`*tok == "x"` and `&String == String` forms in this module path).
- Current `std.cli` uses local helpers `_string_eq_ref` / `_string_eq_value` as a temporary safe path.
- Required follow-up after this branch:
  1. Add minimal regression for `&String` equality forms expected to work.
  2. Fix checker/operator resolution root cause.
  3. Remove `_string_eq_ref` / `_string_eq_value` from `std.cli`.
