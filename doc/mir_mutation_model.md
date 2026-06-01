## MIR Mutation Model

Drift MIR is SSA for scalar values and explicit about aggregate mutations.

- SSA invariants apply to value names: params, lets, temporaries, call results, φ-params, etc. Each SSA name is defined once, dominated by its def, and immutable as a value.
- FieldSet and ArraySet are explicit memory writes:
  - They never define a new SSA name and are never φ sources.
  - They mutate an existing aggregate “in place”.
  - They may alias: the same aggregate can be reachable through multiple SSA names; optimizations must assume a store can clobber any alias unless proven otherwise by alias/memory analysis.
  - Scalar SSA optimizations (CSE, constant folding, DCE) must treat these stores as potential barriers for values that depend on the mutated aggregate unless a later pass proves it safe to reorder/eliminate.
- Loads (FieldGet/ArrayGet) are pure reads of the current aggregate state; their operands must be defined and dominated by their defs.

This model mirrors common SSA IRs (e.g., LLVM, Rust MIR): SSA for scalars with explicit memory ops for aggregates. More aggressive memory SSA can be added later if needed; for now, treat stores as explicit side effects.
