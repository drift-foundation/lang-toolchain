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

- `val` / `var` bindings with optional `^capture` metadata (no re-assignment yet). Type annotations are optional when the right-hand expression determines the type.
- Functions with typed parameters/returns (using the `returns` keyword).
- Expressions with literals (`Int64`, `Float64`, `String`, `Bool`), arithmetic/logic operators, and function calls.
- Array literals `[expr, ...]` that produce `Array<T>` values (all elements must share a type, and empty literals are not supported yet).
- Square-bracket indexing for reads (`val first = nums[0]`) and writes (`nums[1] = 42`) when the binding is declared with `var`.
- `return`, `raise domain ...`, and expression statements.
- Builtins: `sys.console.out.writeln(Displayable)` plus legacy `print(Displayable)`, and `error(message: String, domain=?, code=?, attrs=?)`.

## Examples

`examples/hello.drift` demonstrates simple console output inside `main`. `examples/fail.drift` shows throwing/propagating an `error`. For focused syntax samples you can live-edit, check `playground/`, e.g.

- `playground/basics.drift` – local `val` bindings and console output
- `playground/functions.drift` – functions calling functions
- `playground/effects.drift` – throwing `error(...)` values
- `playground/mutable_bindings.drift` – `var` declarations (mutation semantics will land later)
- `playground/logic.drift` – boolean expressions
- `playground/arrays.drift` – array literals backing `Array<T>` bindings

## Limitations / next steps

- No assignments or mutation semantics beyond the initial binding.
- Only primitive types are implemented; structs/enums/ownership markers are stubs for future work.
- Keyword arguments are only honored by builtin functions.
- The interpreter uses a straightforward tree walk and raises on the first runtime error.

See `docs/drift-draft.md` for the broader design goals.
