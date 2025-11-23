# Drift development history

## 2025-11-20
- Captured the `lang.core.source_location` helper in the spec as a zero-cost intrinsic that lowers to the current file/line. Kept the data shape explicit (`SourceLocation` struct) so callsites can choose when to capture site metadata, thread it through `^` context bindings, or pass it into exceptions; avoided auto-injecting locations in the runtime to keep logging/telemetry opt-in. (Prototype interpreter still needs the intrinsic wired in.)
- Hardened comment and error conventions: grammar now allows both `//` line comments and `/* ... */` block comments (non-nesting) so we can annotate examples without fighting terminator insertion. Documented a standard `IndexError(container, index)` event for out-of-bounds accesses to make future bounds checks report consistent payloads instead of ad-hoc errors.
- Elevated error declarations to first-class language items with an `exception` keyword, aligning them with structs so constructors are typed and usable from the interpreter. Fixed the parser to ignore non-Tree nodes when assembling parameter lists, preventing stray tokens/comments from polluting function signatures. Added playground coverage for captures and structs so the sample suite exercises the new constructs.
- Tightened tooling guardrails: the draft linter now enforces tabs (default) vs spaces and checks snake_case/PascalCase across functions, parameters, bindings, structs, and exceptions to keep examples consistent with the style guide. The `just` recipes parse both `playground/` and `examples/` to catch grammar regressions immediately; we deliberately stayed with a lightweight custom linter instead of a full formatter while the syntax is still in flux.
- Worked through module signing requirements and concluded the pipeline should canonically sign an ANF-like DMIR and lower to SSA MIR for optimization/codegen; added an overview of that split to `docs/design-first-afm-then-ssa.md`.
- Adopted a policy of fully monomorphizing generics (no shared reified bodies) so DMIR/SSA always see concrete types; watch for code-size blowups in heavily polymorphic code, but favor optimizer simplicity and performance first.
## 2025-11-23
- Added a DMIR draft spec and cleaned up primitive notes (ConsoleOut treated as runtime-provided only). Expanded control surface with ternary `?:`, plus try/catch and inline try/else support wired through grammar, parser, checker, interpreter, linter, and new runtime tests (including a ternary test case in `tests/`).
- Runtime now enforces array bounds with `IndexError(container, index)` and prints errors in the spec’s structured format with a simple call-stack capture; added runtime tests for out-of-bounds and error reporting.
- Documented DMIR canonicalization rules (naming, ordering, kwarg normalization) and added surface→DMIR examples for ternary, try/else, and constructors to stabilize the signing format. Approved SSA MIR control-flow model and value/ownership rules (monomorphized, move-only by default, explicit error edges, drops in MIR).
- Documented the SSA MIR instruction palette (const/move/copy/call with normal+error edges, struct/array ops, unary/binary, drop) in `docs/dmir-spec.md`; TODO updated accordingly.
- Added end-to-end surface→DMIR→SSA MIR examples (ternary, inline try/else with fallback) to ground the IR design.
- Added SSA MIR terminology/conventions (block labels, params-as-φ, SSA defs, explicit call successors, ownership rules).
- Added a CFG block notation alongside the ternary SSA example to visualize control flow and φ-like params.
- Added CFG notation to the try/else SSA example for readability.
- Added verifier expectations to the SSA MIR terminology section (SSA dominance, types, ownership, drops, terminators).
- Added a MIR verifier checklist to the DMIR spec so readers know the invariants to enforce before optimizations/codegen.
- Added initial MIR data structures (`lang/mir.py`) to model SSA blocks, instructions, edges, and programs; tests still pass.
- Added a skeleton MIR verifier (`lang/mir_verifier.py`) covering SSA def/use, ownership moves/drops, edge/param arity, and basic terminator checks.
- Clarified dominance in the SSA terminology (defs must appear on every path to their uses).
- Documented the verifier implementation sketch (input, steps, output) in the DMIR spec.
- Enriched MIR nodes with source locations and wired the verifier to report locations on errors.
- Added initial MIR data structures (`lang/mir.py`) to model SSA blocks, instructions, edges, and programs; tests still pass.
