# LANGUAGE_BUG: internal destructible-plan ICE — PathDependent at drop_before_overwrite

Classification: **LANGUAGE_BUG (compiler ICE on valid source).**
Surfaced by: the std.json iterative-parser implementation
(work/json-iterative-parser/), release-blocking for 0.33.89.
Status: **regression-first repro filed; compiler fix pending.**

## Symptom

Compiling valid Drift source aborts with an internal contract
failure (not a user diagnostic):

```
error: internal: destructible plan contract failure (drop_before_overwrite:
ledger returned PathDependent at (fn=driver, block=if_join1, idx=1,
local=pspan).  Tier-1 promotion retired the `initialized_destructibles`
fallback — if PathDependent is now reachable, either tighten the lattice
or restore a flag-guarded path here before re-landing.) [E-AUTO-299a94e5]
```

Emission site: `lang/driftc/stage2/destructible_authority.py:279` — a
deliberate `raise RuntimeError` tripwire whose comment asserts "the
observe re-run said the lattice never yields MaybeUninit at
drop_before_overwrite points."  That assumption is FALSE.

## Minimal repro (`repro.drift`, ~25 lines)

A **destructible local conditionally moved inside an `if`, then
overwritten** afterward:

```drift
var pspan: Optional<Sp> = Optional::Some(Sp::Leaf(x = 1));  // Sp: destructible variant
...
if locate {
    match move pspan { Some(s) => { ... }, None() => { } }   // moved only on this path
}
pspan = Optional<Sp>::None();                                // overwrite -> drop_before_overwrite
```

At the overwrite the compiler must drop the old value IF still live;
liveness is PathDependent (moved on the `locate` path, live
otherwise), so the ledger returns PathDependent and the retired
fallback tripwire fires.

## Scope of the defect (measured)

* **Not a regression**: ICEs on BOTH the current tree AND certified
  driftc 0.33.87 — a long-standing latent defect, never hit only
  because no shipped code exercised this exact shape.
* **Independent of throwing**: `repro_nothrow.drift` (a `nothrow`
  function) ICEs identically — the throwing context is not required.
* **The pattern is ordinary and valid**: conditionally consuming a
  destructible then reassigning it is normal code; the language's
  own drop-flag machinery is meant to cover exactly this.

## Root cause

The ownership-ledger "Tier-1 promotion" retired the
`initialized_destructibles` fallback that previously emitted a
flag-guarded drop for a path-dependently-live destructible at an
overwrite, on the assumption PathDependent was unreachable there.
It is reachable.

## Fix direction (per the tripwire's own text; decision-1 compliant)

Restore a flag-guarded drop for PathDependent at
`drop_before_overwrite` (emit a runtime drop-flag and drop iff live),
OR tighten the lattice so this case resolves to a definite
MUST_DROP / MUST_NOT_DROP.  This is in the gated ownership-lattice
area (feedback_ownership_lattice_change_bar) and needs its own
regression-first cycle + review.  Do NOT work around it by reshaping
std.json (the iterative-parser code that triggers it is valid).

## Regression-first gate

`repro.drift` and `repro_nothrow.drift` here must COMPILE (and run)
once fixed.  The std.json iterative parser (WIP saved as
`iterative-parser-block.drift.wip`) then lands on top of the fixed
compiler.
