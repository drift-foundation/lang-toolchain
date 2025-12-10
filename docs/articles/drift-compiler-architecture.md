# Drift Compiler Architecture (lang2)

The Drift compiler is a **pipeline of explicit, isolated stages**.
Each stage has one responsibility and transforms the program into a simpler, more structured form.

## Acronyms used in this doc
- **AST**: Abstract Syntax Tree (stage0, surface syntax)
- **HIR**: High-level IR (stage1, sugar-free, still expression-structured)
- **MIR**: Mid-level IR (stage2, explicit ops + CFG)
- **SSA**: Static Single Assignment form (stage4)
- **CFG**: Control-Flow Graph
- **DF**: Dominance Frontier
- **DV**: DiagnosticValue (error payload)
- **FnResult**: Result-like return type `<T, Error>` used for can-throw functions
- **TypeEnv**: Type environment protocol used by typed throw checks (is_fnresult/fnresult_parts)
- **IR**: Intermediate Representation (generic term; refers to HIR/MIR/SSA in this doc)

**Full end-to-end pipeline:**

> **Drift source → AST → HIR → (Signatures + Checker) → MIR → SSA → TypeEnv → Throw-checks → LLVM → clang → machine code**

Every stage is testable on its own and contributes specific invariants that later stages rely on.

---

# 1. Drift Source → AST (Stage 0)

### Purpose

Turn raw source text into a syntactic tree with no sugar removed and no semantics applied.

### Responsibilities

* Lexing & parsing.
* Structural representation of the program exactly as written.
* Nodes correspond 1:1 to syntax constructs.
* No type information, no desugaring, no CFG.

### Output

**AST** — Syntax-tree only; a direct mirror of the source.

---

# 2. AST → HIR (Stage 1)

### Purpose

Normalize the language into a **sugar-free, explicit** representation.

### Responsibilities

* Convert surface constructs into canonical forms:

  * Method sugar → `HMethodCall`
  * Indexing/field sugar → `HIndex` / `HField`
  * DV constructors → explicit `HDVInit`
  * Loop/if expressions → `HLoop`, `HIf`
  * Try/throw → `HTry`, `HThrow`
* Eliminate placeholders and implicit receivers.
* Maintain structure but eliminate ambiguity.

### Output

**HIR** — A clean, sugarless representation ready for CFG construction.

---

# 3. HIR → Signatures + Checker (Stage 1.5)

### Purpose

Resolve declared types into TypeIds, build canonical function signatures, and perform shallow HIR validation.

### Responsibilities

* **Type resolution**: convert declared param/return types into `TypeId`s on a shared `TypeTable` (scalars, `Array<T>`, `FnResult<T,Error>`, `String`).
* Build `FnSignature`/`FnInfo` maps (name, param types, return type, throws intent).
* Shallow HIR checks:
  * Catch-arm shape/unknown events (when catalog provided).
  * Array/indexing rules (index must be Int, subject must be Array).
  * `.len/.cap` typing (Array/String → Uint).
  * Uint-only bitwise ops; Bool-only conditions.
  * Duplicate function definitions rejected.
* Produce a `CheckedProgram` with signatures, shared `TypeTable`, diagnostics; no full type checking yet.

### Output

**Signatures + CheckedProgram** — canonical types per function, diagnostics, shared TypeTable for downstream stages.

---

# 4. HIR → MIR (Stage 2)

### Purpose

Lower HIR to a **control-flow-explicit intermediate representation** with clear operational semantics.

### Responsibilities

* Emit MIR instructions:

  * `LoadLocal`, `StoreLocal`, `Call`, `MethodCall`
  * `ConstructDV`, `ConstructError`, `ConstructResultOk/Err`
  * `ErrorEvent`, `AssignSSA` (pre-SSA helper), `Phi`
* Build explicit **basic blocks** and CFG with terminators:

  * `Goto`, `IfTerminator`, `Return`
* Lower try/throw into runtime-accurate dispatch:

  * Throws produce `Error`
  * Dispatch compares event codes
  * Catch blocks bind error or fall back to catch-all
  * Unmatched errors unwind to the nearest outer try
  * Only at function top-level do errors become `FnResult.Err`
* Validate catch-arm structure:

  * At most one catch-all
  * Catch-all must be last

### Output

**MIR** — Concrete operations + explicit CFG, but still untyped and without SSA normalization.

---

# 5. MIR → Pre-analysis (Stage 3)

### Purpose

Extract facts required by type-aware and throw-safety stages.

### Responsibilities

* Determine:

  * Address-taken locals
  * May-fail sites (calls, DV construction, errors)
  * Error-construction sites (event codes, DV payloads)
* Build **ThrowSummary** per function:

  * Builds error-propagation metadata
  * Tracks whether any error is constructed

### Output

**ThrowSummary** + **MIR annotations** feeding stage4.

---

# 6. MIR → SSA (Stage 4A)

### Purpose

Normalize MIR into **Static Single Assignment** form with deterministic block ordering.

### Responsibilities

* Compute dominators and dominance frontiers.
* Insert φ-nodes for multi-block locals.
* Rewrite locals into SSA variables via renaming.
* Reject loops/backedges unless SSA supports them (current v1 accepts straight-line + if/else).
* Produce a deterministic reverse-postorder `block_order`.

### Output

**SSA** — Each variable assigned exactly once; CFG ready for type checking and codegen.

---

# 7. Build TypeEnv (Stage 4B)

### Purpose

Provide type information for functions and SSA values.

### Responsibilities

* Checker creates canonical **FnSignature** for each function.
* TypeEnv resolves:

  * Scalar types (Int, Bool)
  * FnResult types (`Result<T, Error>`)
  * Error payload structure
* Integrates with SSA:

  * Tags each SSA value with a type
  * Exposes queries (`is_fnresult`, `fnresult_parts`)

### Output

**TypeEnv** — complete typing needed for semantic enforcement.

---

# 8. Throw-checks (Stage 4C)

### Purpose

Enforce correctness rules about error construction and FnResult usage.

### Responsibilities

Using **ThrowSummary** + **TypeEnv** + **FnSignature**, the checker enforces:

1. **Non–can-throw functions**

   * Must not construct errors.
   * Must not return FnResult.
2. **Can-throw functions**

   * Must not contain bare `return;` (value required).
   * Must return a value of type `FnResult<T, Error>`.
3. **Type-aware FnResult validation**

   * Structural checks replaced by type-driven checks when TypeEnv is present.
4. **Catch-arm validation**

   * Unknown events and duplicates flagged.
   * Invariants established for codegen.

### Output

Either a clean program with invariants satisfied,
or a **CheckedProgram** containing diagnostics.

---

# 9. Borrow checker (in progress)

### Purpose

Prevent use-after-move and conflicting borrows before codegen. Runs on typed HIR with a CFG (pre-MIR/SSA) and will become a mandatory stage once the typed checker/driver is in place (no flags).

### Current behavior (Phase 2 scaffold)

* **Places**: `PlaceBase(kind, id, name)` + projections; whole-place overlap for conflicts.
* **Moves**: UNINIT/VALID/MOVED per place; use-after-move diagnostics; moves blocked while a loan exists; assignments drop overlapping loans.
* **Borrows**: explicit `HBorrow` (`&`/`&mut`) only from lvalues; reject borrows from moved/uninit/rvalues; shared-vs-mut conflicts enforced.
* **Lifetime approximation**: loans carry `region_id`; explicit `HBorrow` lets compute block-liveness via def/use reachability, others are function-long; temporary borrows in expr/conds and auto-borrows are dropped after use (coarse NLL approximation).
* **Auto-borrow scaffold**: optional flag treats call args/receivers as shared, call-scoped borrows; signature-driven shared vs mut is still TODO.

### Next steps

* Region analysis (last-use–based) to drop loans when their region ends; region-aware merging instead of union.
* Signature-driven auto-borrow for params/receivers typed as `&T` / `&mut T`, with call-scoped regions.
* Wire into pipeline/CLI with spans in diagnostics; refine overlap precision (field/slice) if needed.

### Planned integration (no flags, full enforcement)

* Replace the stub checker with a typed checker that emits TypedFnHIR + TypeEnv, with ParamId/LocalId and TypeIds on every binding/expr; extend TypeKind to include reference types. TypedFnHIR shape: name, params, locals, HIR body, expr_types map, binding map.
* Thread binding IDs into HIR and base_lookup so borrow checker uses identities, not names.
* Driver pipeline becomes: parse → type check → **borrow check (mandatory)** → lowering/codegen; single diagnostics sink, any error aborts.
* Auto-borrow becomes signature-driven (param/receiver `&T` / `&mut T`) once ref types exist.
* Region-aware loan liveness (NLL) replaces union-of-function loans before enforcement goes live.

---

# 10. SSA → LLVM IR (Codegen)

### Purpose

Lower well-typed SSA to LLVM IR using the v1 Drift ABI.

### Responsibilities

* Emit `%DriftError` and `%FnResult_Int_Error` types.
* Lower ops:

  * Scalars → `i64` / `i1`
  * FnResult → struct `{ i1 is_err, i64 ok, %DriftError err }`
  * Calls: choose correct ABI based on callee signature
  * Extractors for FnResult fields
  * Branches and φ-nodes using SSA metadata
* Reject unsupported patterns (e.g., loops in v1).
* Preserve deterministic block order from SSA.

### Output

**LLVM IR text** suitable for:

* `lli` execution
* `clang -x ir` compilation
* Integration in larger modules / linking

---

# 11. LLVM IR → clang → Machine Code

### Purpose

Produce actual executable machine code.

### Responsibilities

* Wrap Drift entry function (`drift_main`) in a C-ABI `main()`:

  * Call `@drift_main`
  * Truncate the result to `i32`
  * Return as process exit code
* Invoke:

  ```bash
  clang -x ir ir.ll -o a.out
  ```
* Run the resulting binary and observe:

  * Exit code
  * Stdout/stderr

### Output

**Machine code**, with behavior proven to match the semantics of the Drift program.

---

# Summary: The Unified Pipeline

```
Drift source
    ↓ parse
AST (syntax only)
    ↓ desugar
HIR (sugar-free)
    ↓ signatures + shallow checks
Signatures + CheckedProgram
    ↓ explicit CFG + ops (shared TypeTable)
MIR
    ↓ pre-analysis
Throw summaries
    ↓ CFG→SSA
SSA
    ↓ typing
TypeEnv
    ↓ invariants
Throw-checks
    ↓ lowering
LLVM IR
    ↓ clang
Machine code (native executable)
```

Each stage builds on the previous one and enforces strict invariants, guaranteeing that by the time LLVM sees the program, **all semantic correctness has already been proven**.

---
