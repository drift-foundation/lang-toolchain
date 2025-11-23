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
- Names are unique per scope (α-renamed if needed). Stable, deterministic naming scheme:
  - User locals keep their spelled identifiers when unique.
  - Compiler-introduced temps use `_t{n}` numbering per function/block in first-appearance order.
  - No gaps in numbering; renumber after desugaring to keep order deterministic.
- Ordering:
  - Declarations (structs, exceptions, functions) serialized in source order after imports.
  - Fields/params listed in declared order.
  - Catch clauses serialized in source order.
- `let` bindings appear in evaluation order (ANF sequence); do not reorder or DCE in DMIR.
- Module-level statements are serialized in source order.
- Keyword arguments are reordered to positional order based on the callee’s parameter list; duplicate/missing args are already rejected by the typechecker.
- Dead-code removal is not part of canonicalization; keep all user-visible semantics intact.

## Errors & exceptions
- Single `Error` type; exceptions are event + args lowered to an `Error` value.
- `throw Event(args...)` lowers to `raise <Error>`.
- `try/catch` is retained structurally; lowering to SSA will turn it into explicit control-flow edges carrying the `Error`.

## Ownership & drops
- Values move by default; `move` nodes mark ownership transfer. Using a moved-from value is a verifier error in MIR (same intent as Rust’s move semantics: once moved, the source is invalid).
- DMIR does not insert drops; those are added during SSA/MIR lowering when liveness is known. (Rationale: keep DMIR stable/signable and let the optimizer compute exact drop points.)
- `copy` is only permitted for types marked copyable (primitives and structs implementing `Copy`); otherwise moves are required.

## SSA MIR value/ownership model
- Monomorphized types only: all generics are specialized before MIR (like C++/Rust template/monomorphization; no shared generic bodies at MIR time).
- Move-only by default; `move` consumes the value. MIR verifier enforces no use-after-move.
- Drops are explicit MIR instructions; inserted post-liveness; verifier enforces at-most-once drop per owned value.
- Calls/ops can raise; error edges carry the `Error` value. `raise` terminates the function with the error path.

## SSA MIR instruction set (typed, SSA)
- `const <value>` — literals (ints/floats/bools/strings).
- `move <v>` — consumes `v`; using `v` afterward is invalid.
- `copy <v>` — only for copyable types (primitives/`Copy` structs).
- `call <fn>(args) normal bbN(args) error bbE(err)` — direct call with explicit normal and error successors; builtins/constructors follow the same shape.
- `struct_init <Type>(args)` — positional args in field order.
- `field_get <base>.<field>` — read-only access to struct field.
- `array_init [v0, v1, ...]` — arrays are values; element type is concrete.
- `array_get base, index` — includes bounds check that raises `Error` on OOB.
- `array_set base, index, value` — bounds check then write; only on mutable arrays.
- `unary <op> v` and `binary <op> lhs, rhs` — typed arithmetic/logic.
- `drop <v>` — explicit destructor/drop; inserted after liveness.
- Terminators (`br`, `condbr`, `return`, `raise`) and block params act as φ-nodes; no separate φ instruction.

## Serialization
- Textual, deterministic format (one binding/stmt per line, ordered declarations). Binary envelope may wrap it for signing, but the textual form is canonical for hashing.
- Includes DMIR version in the header so verifiers can enforce compatibility.

## Examples (surface → DMIR sketch)

Surface ternary:
```drift
val x = cond ? a() : b()
```
DMIR (ANF-ish):
```
let _t1 = cond
let _t2 = a()
let _t3 = b()
let x = if _t1 { _t2 } else { _t3 }
```

Surface try/else:
```drift
val fallback = try parse(input) else default_value
```
DMIR:
```
let _t1 = parse(input) try_else default_value
let fallback = _t1
```
(`try_else` desugars to the structured try/catch form with a catch-all that yields `default_value`.)

Surface struct/exception constructors:
```drift
struct Point { x: Int64, y: Int64 }
exception Invalid(kind: String)

val p = Point(x = 1, y = 2)
throw Invalid(kind = "bad")
```
DMIR:
```
let p = Point(1, 2)
raise Invalid
```
(Args are reordered to positional order; `raise` wraps the event into `Error` as part of DMIR lowering.)

## Lowering to SSA MIR
- DMIR is the input to the SSA builder:
  - Structured control → CFG + φ.
  - `let` bindings → SSA value definitions.
  - Drops/destructors inserted based on SSA liveness/ownership analysis.
  - Error edges become explicit basic blocks carrying the `Error`.

## SSA MIR control-flow model
- Functions are CFGs of basic blocks.
- Block parameters represent φ-nodes (values incoming from predecessors).
- Terminators:
  - `br target(args)` — unconditional branch, passing block params.
  - `condbr cond, then(args), else(args)` — conditional branch.
  - `return value` — normal return.
  - `raise error` — exceptional return carrying `Error`.
- Calls:
  - Direct calls; each call has two successors: a normal edge and an error edge (both receive block params). The error edge carries the `Error` value and aligns with the `raise` path.
  - Builtins/constructors follow the same call shape for uniformity.
- Rationale: explicit error edges mirror the implicit `Result<T, Error>` model while keeping the CFG explicit for optimizations and verification.
- All control paths end in `return` or `raise`; no implicit fallthrough.

## SSA MIR terminology / conventions
- Block labels: use a simple `bb` prefix (e.g., `bb0`, `bb_then`, `bb_err`). Block parameters are listed in parentheses and act as φ-nodes.
- Instructions are SSA: each defines exactly one value; uses must be dominated by defs.
- Calls list both successors: `normal bbX(args)` and `error bbE(err)`.
- Types are concrete, monomorphized; `Error` is a concrete type on error edges.
- Ownership: `move` consumes, `copy` only for copyable types, `drop` explicit.
- No implicit fallthrough; every block ends in a terminator.
- Verifier expectations:
  - SSA form: each use dominated by its def; block params match predecessor arguments.
  - Type consistency: instruction and operand types align; call args match callee signature; block params typed.
  - Ownership: no use-after-move; `copy` only on copyable types; `drop` at most once per owned value.
  - Control flow: every block ends in `br`/`condbr`/`return`/`raise`; functions have at least one `return` or `raise` path.

## MIR verifier (what it checks)
- SSA/dominance: every use is dominated by its definition; block parameters align with incoming arguments.
- Type correctness: instruction result types match operand types; call arg/return types match signatures; terminators target existing blocks with correct arg counts/types.
- Ownership: track value states to catch use-after-move, copy of non-copyable types, and double-drop/move; `drop` at most once per owned value.
- Control flow: each block has a terminator; all paths end in `return` or `raise`; no edges to missing blocks.
- Error edges: calls’ normal/error successors are well-typed; error edges carry an `Error` value.

## Signing
- The canonical serialized DMIR is hashed and signed.
- Signature scope includes: DMIR version, module metadata, and full DMIR body.
- SSA/backends are not signed artifacts; they must verify against the signed DMIR.

## Surface → DMIR → SSA MIR examples (annotated)

### Example 1: ternary call
Surface:
```drift
fn pick(cond: Bool) returns Int64 {
    return cond ? a() : b()
}
```
DMIR:
```
let _t1 = cond
let _t2 = a()
let _t3 = b()
return if _t1 { _t2 } else { _t3 }
```
SSA MIR (blocks, params as φ):
```
bb0(cond: Bool):       // block labels use a `bb` prefix; block params = φ
  br bb1(cond)

bb1(c: Bool):
  condbr c, bb_then(), bb_else()

bb_then():
  a_res = call a()
  br bb_join(a_res)                   // forward the call result on the normal edge

bb_else():
  b_res = call b()
  br bb_join(b_res)                   // same pattern

bb_join(val: Int64):
  return val

bb_err(err: Error):
  raise err
```
(Calls have normal/error edges; join block models the ternary merge; block params act as φ.)

CFG (block notation):
```
bb0(cond) -> bb1(cond)
bb1(c)  -true-> bb_then()
        -false-> bb_else()
bb_then() -> bb_join(a_res)
bb_else() -> bb_join(b_res)
bb_join(val) -> return val
bb_err(err) -> raise err
```

### Example 2: try/else with struct init and error edge
Surface:
```drift
exception Invalid(kind: String)
struct Point { x: Int64, y: Int64 }

fn make(cond: Bool) returns Point {
    val p = try build(cond) else Point(x = 0, y = 0)
    return p
}
```
DMIR:
```
let _t1 = build(cond) try_else Point(0, 0)
let p = _t1
return p
```
SSA MIR:
```
bb0(cond: Bool):
  br bb1(cond)

bb1(c: Bool):
  v_build = call build(c) normal bb_ok(val) error bb_err(err)

bb_ok(val: Point):
  return val

bb_err(err: Error):
  // try-else fallback
  v_fallback = Point(0, 0)
  return v_fallback
```
(The inline try/else becomes a call with an error edge into a fallback block; struct init is positional; no drops shown here—those are inserted after liveness.)

CFG (block notation):
```
bb0(cond) -> bb1(cond)
bb1(c) -> bb_ok(val) on success
       -> bb_err(err) on error
bb_ok(val) -> return val
bb_err(err) -> return Point(0, 0)
```
