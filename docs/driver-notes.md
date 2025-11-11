# Drift Language Prototype Driver

This repository now includes a minimal Python implementation of the Drift language described in `docs/drift-draft.md`. It provides parsing (via Lark), static type-checking with basic effect tracking, and an interpreter with a small builtin runtime so you can experiment with the syntax and semantics quickly.

## Running code

```
./drift.py examples/hello.drift
```

Drift source files use the `.drift` extension; the driver happily reads stdin too, but sticking to that suffix keeps playground/examples organized and makes future tooling easier.

- The driver parses the source file, performs static analysis, and then runs the program through the interpreter.
- By default it invokes the `main` function after executing module-level statements. Use `--entry name` to call a different function.
- Errors are reported with location information for parse/type failures and with the domain/message for runtime `error` values.

## Language surface supported

- `val` / `var` bindings with optional `mut` (no re-assignment yet).
- Functions with typed parameters/returns, optional `throws{domain}` declarations.
- Expressions with literals (`i64`, `f64`, `str`, `bool`), arithmetic/logic operators, and function calls.
- `return`, `raise domain ...`, and expression statements.
- Builtins: `sys.console.out.writeln(str)` plus legacy `print(str)`, and `error(message: String, domain=?, code=?, attrs=?)`.
- Effect checking: functions declared `throws{...}` are validated against `raise` statements and callee declarations.

## Examples

`examples/hello.drift` demonstrates module `let` bindings and a simple `main`. `examples/fail.drift` shows throwing/propagating an `error`. For focused syntax samples you can live-edit, check `playground/`, e.g.

- `playground/basics.drift` – strings + module `let`
- `playground/functions.drift` – functions calling functions
- `playground/effects.drift` – effect annotations with `raise`
- `playground/mutable_bindings.drift` – `let mut` declarations
- `playground/logic.drift` – boolean expressions

## Limitations / next steps

- No assignments or mutation semantics beyond `let` storage.
- Only primitive types are implemented; structs/enums/ownership markers are stubs for future work.
- Keyword arguments are only honored by builtin functions.
- The interpreter uses a straightforward tree walk and raises on the first runtime error.

See `docs/drift-draft.md` for the broader design goals.
