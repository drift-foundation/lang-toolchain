# Drift Language Prototype Driver

This repository originally included a minimal tree-walk interpreter. The compiler has since moved fully to SSA + LLVM; the interpreter has been removed. These notes remain as historical background only.

## Running code

The old `drift.py` entrypoint has been deleted. To run programs, use the SSA/LLVM toolchain via `lang/driftc.py` or the e2e runner (`tests/e2e_runner.py`), both of which target the compiled backend.

## Language surface supported

- `val` / `var` bindings with optional `^capture` metadata (no re-assignment yet). Type annotations are optional when the right-hand expression determines the type.
- Functions with typed parameters/returns (using the `returns` keyword).
- Expressions with literals (`Int64`, `Float64`, `String`, `Bool`), arithmetic/logic operators, and function calls.
- Array literals `[expr, ...]` that produce `Array<T>` values (all elements must share a type, and empty literals are not supported yet).
- Square-bracket indexing for reads (`val first = nums[0]`) and writes (`nums[1] = 42`) when the binding is declared with `var`.
- `return`, `raise domain ...`, and expression statements.
- Builtins: `sys.console.out.writeln(Displayable)` plus legacy `print(Displayable)`, and `error(message: String, domain=?, code=?, attrs=?)`.

## Examples

`examples/hello.drift` demonstrates simple console output inside `main`. `examples/fail.drift` shows throwing/propagating an `error`. For more samples, see the `examples/` directory or the e2e programs under `tests/e2e/`.

## Limitations / next steps

- No assignments or mutation semantics beyond the initial binding.
- Only primitive types are implemented; structs/enums/ownership markers are stubs for future work.
- Keyword arguments are only honored by builtin functions.
- The removed interpreter used a straightforward tree walk and raised on the first runtime error; current execution goes through SSA + LLVM instead.

See `docs/drift-draft.md` for the broader design goals.
