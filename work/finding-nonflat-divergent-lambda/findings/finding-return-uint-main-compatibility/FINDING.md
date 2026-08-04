# Finding: `Uint` value returned from `main -> Int`

Parent: `work/finding-nonflat-divergent-lambda`

Discovered: 2026-08-03 full-suite memcheck codegen e2e gate, after the
shared return-compatibility authority began diagnosing every residual return
type mismatch.

Status: open child finding. `PROGRESS.md` is implementer-owned and is
intentionally not created by this review pass.

## Observed

The existing fixture
`lang/tests/codegen/e2e/bitwise_uint_ops/main.drift` now fails before codegen:

```text
return type 'Uint' does not match declared type 'Int'
```

The fixture declares `pub fn main() nothrow -> Int`, performs its bitwise work
in `Uint`, and ends with `return x` where `x: Uint`. The full gate reported:

```text
[codegen e2e] bitwise_uint_ops: FAIL (unexpected checker diagnostics)
```

A focused runner reproduction on the current committed tree reports the same
diagnostic.

## Confirmed contract evidence

- The checked-in spec's program-entry contract requires `main` to return
  `Int`; the only v1 signatures are `pub fn main() nothrow -> Int` and the
  argv-bearing equivalent (`doc/design/drift-lang-spec.md`, program-entry
  section).
- The spec distinguishes `Int` and `Uint`, requires `Uint` operands for
  bitwise operations, and provides explicit numeric `cast<T>(...)`.
- No reviewed spec text authorizes implicit conversion of a non-literal
  `Uint` value to `Int` at a return boundary.
- The shared `_type_return_value` authority now rejects this non-literal
  mismatch. That is consistent with the declared-return contract and with the
  existing explicit-cast surface.

## Current assessment

**Inferred, not authoritative:** this is probably a stale fixture rather than
a missing compiler coercion. Its real purpose is to exercise `Uint` bitwise
operators and obtain process exit 254; the final uncast return accidentally
relied on the old return-checking hole.

The narrow contract-preserving migration appears to be:

```drift
return cast<Int>(x);
```

This keeps all bitwise operations in `Uint`, preserves the expected exit code,
and makes the entrypoint boundary explicit.

Do **not** add a general `Uint -> Int` return coercion merely to retain this
fixture. If implementation evidence or an overlooked spec clause contradicts
this assessment, record it in `PROGRESS.md` and stop for Slawomir's semantic
ruling before changing compiler behavior.

## Approval gate

The proposed change edits an existing test fixture, so it requires explicit
Slawomir approval under `AGENTS.md`. The implementer should record the exact
proposal and create `APPROVAL-PENDING-<timestamp>` before editing it. The
current request to prepare a handoff is not itself approval of the test edit.

## Acceptance criteria

- The fixture still exercises the same `Uint` bitwise/augmented-assignment
  operations and compiles/runs with exit 254.
- The source explicitly crosses the `Uint -> Int` entrypoint boundary.
- Ordinary non-literal `Uint` returned from a declared `Int` function remains
  a clean type error; no implicit numeric return conversion is introduced.
- No language-spec edit is made without Slawomir's separate approval.

## Refactor-trigger result

`doc/refactor_triggers.md` was scanned on 2026-08-04 UTC. No registered trigger
matches a stale numeric-boundary fixture or ordinary return-type mismatch.

