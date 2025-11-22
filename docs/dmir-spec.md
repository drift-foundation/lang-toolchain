# Drift DMIR Specification (draft)

DMIR (Drift Module Intermediate Representation) is the canonical, signed, compiler-facing representation of Drift programs. It captures fully type-checked semantics in a normalized, ANF-like, structured form. DMIR is stable across compiler versions; SSA (Static Single Assignment) MIR and backend IR are free to change.

See also: `docs/design-first-afm-then-ssa.md` for the design path that led to this split (ANF-like DMIR for signing, SSA MIR for optimization/codegen).

## Goals
- Stable “semantic identity” for signing/distribution.
- Deterministic, canonical form (no formatting or naming drift).
- Structured control flow (no φ nodes) with explicit evaluation order.
- Single concrete `Error` path with event-based exceptions.
- Explicit ownership/move; no implicit copies.

## Scope & status
- Status: draft; format may still evolve before first signing milestone.
- Source of truth: this doc + `docs/design-first-afm-then-ssa.md`.
- Consumers: Drift toolchain, verifiers, bundlers, and third-party analyzers.

## Module shape
- Header: DMIR version, module name, dependencies (imported modules).
- Decls: functions, structs, exceptions (no macros/templates beyond what the typechecker already resolved).
- Top-level bindings are lowered into an implicit module body with canonical `let` bindings; DMIR treats them as immutable globals unless explicitly modeled as mutable cells (mirroring surface `var` if/when allowed at top level).

## Types
- Primitives: `Bool`, `Int64`, `Float64`, `String`, `Void`, `Error`, plus resolved user-defined structs/exceptions. (`ConsoleOut` is a temporary runtime-provided builtin for `cout`; it is not a language primitive.)
- Arrays: `Array[T]` (element type resolved, no empty-literal inference in DMIR).
- References/mutability: explicit `ref T` / `ref mut T` per the typechecker output.
- Generics: monomorphized per concrete instantiation (no shared reified bodies); DMIR only carries specialized instances produced by the typechecker.

## Terms & statements (ANF-ish)
- `let <name> = <value>`: DMIR uses immutable, single-assignment bindings for every intermediate. Surface `val` lowers directly to `let`; `var` lowers to a mutable cell representation plus `set`/`get` (or equivalent) in DMIR.
- Literals: ints, floats, strings, bools.
- Names: refer to locals, parameters, globals, builtins.
- Calls: direct function names or struct/exception constructors; kwargs made positional by canonicalization.
- Attribute access: struct fields or runtime-provided values that behave “as if” they were structs (e.g., `out.writeln`), resolved through the typechecker’s known members.
- Indexing: `array[index]`.
- Move: `<value->` becomes `move <value>` in DMIR.
- Raise: `raise <error>`; `error` is an `Error` value.
- Return: `return <value>` or `return` for `Void`.
- Expr statements: `expr` (for side effects only).

## Control flow (structured)
- `if <cond> { ... } else { ... }` with explicit blocks.
- `try { ... } catch <Event>(e) { ... } ...` (one or more catches). Catch-all uses `_` or a binder without event. Inline `try expr else fallback` is desugared to the structured form.
- `match`/loops not present yet; add when the surface language gains them.
- No φ functions; DMIR stays structured.

## Evaluation order & canonicalization
- Left-to-right evaluation; every intermediate bound via `let`.
- Keyword arguments reordered to the function’s parameter order.
- Capture/ownership: moves explicit; no implicit copies introduced.
- Names are unique per scope (α-renamed if needed). Stable, deterministic naming scheme (e.g., `_t1`, `_t2`, …) to ensure hashing/signing reproducibility.
- Dead-code removal is not part of canonicalization; keep all user-visible semantics intact.

## Errors & exceptions
- Single `Error` type; exceptions are event + args lowered to an `Error` value.
- `throw Event(args...)` lowers to `raise <Error>`.
- `try/catch` is retained structurally; lowering to SSA will turn it into explicit control-flow edges carrying the `Error`.

## Ownership & drops
- Values move by default; `move` nodes mark ownership transfer.
- DMIR does not insert drops; those are added during SSA lowering when liveness is known.

## Serialization
- Textual, deterministic format (one binding/stmt per line, ordered declarations). Binary envelope may wrap it for signing, but the textual form is canonical for hashing.
- Includes DMIR version in the header so verifiers can enforce compatibility.

## Lowering to SSA MIR
- DMIR is the input to the SSA builder:
  - Structured control → CFG + φ.
  - `let` bindings → SSA value definitions.
  - Drops/destructors inserted based on SSA liveness/ownership analysis.
  - Error edges become explicit basic blocks carrying the `Error`.

## Signing
- The canonical serialized DMIR is hashed and signed.
- Signature scope includes: DMIR version, module metadata, and full DMIR body.
- SSA/backends are not signed artifacts; they must verify against the signed DMIR.
