# Implementation Plan: Cross-Package Narrow-Throws Contract

## 0. Framing — contract first, plumbing second

**This is not a metadata round-trip slice. It is a contract-enforcement slice.**

The principle: `pub fn f() throws E -> T` is a binding contract that f can
escape ONLY events of type E (or a subtype/alias canonicalizing to E).
Consumers may trust the producer's narrow-throws declaration **only because
producer package compilation made lying impossible**. If producer-side
enforcement is incomplete, emitting `declared_throws_event_fqns` into
package metadata propagates a lie — and a consumer that catches `E` will
silently miss an escaping `F` at runtime. That is worse than today's
generic-throws fallback, which forces catch-all coverage.

**Stop-the-line clause:** if Phase 0 below shows the producer checker does
not fully enforce the narrow-throws contract for `pub fn ... throws E`, do
NOT land Phases 1–3. The two acceptable paths are:

1. Fix producer enforcement first as its own slice. Then land this slice on
   top.
2. Remove the `throws E` narrow-list syntax from the language entirely.
   Functions are either `nothrow` or generic-throws. Catch arms must be
   catch-all or runtime-typed only. The maria-rpc team takes catch-all.

There is no third option where consumers honor a contract the producer
doesn't enforce.

Scope-narrowing note: even with enforcement honored, the slice fixes the
producer→consumer round-trip of `FnSignature.declared_throws_event_fqns`
only. It does **not** add narrow event-FQN metadata to interface/trait
method schemas — that's a separate slice if/when a real carrier surfaces.

## 0a. Phase 0 — verify producer-side enforcement (PREREQUISITE)

Before any emit/decode work, audit the producer-side checker and answer
these questions. Each must be **YES** to proceed. Each **NO** is either a
prerequisite fix or a kill-switch for the slice.

### Q0.1 — Body coverage: does the producer reject a `throws E` body that escapes F?

Given:

```drift
pub error E { tag: String }
pub error F { tag: String }
pub fn f() throws E -> Int {
    throw F(tag = "wrong");   // escapes F, not E
}
```

The producer compilation must reject with a diagnostic like
`E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET` (or whichever code).
Investigation site: `checker/__init__.py` `_function_may_throw` and
related — there is a `may_throw` flag and a `nothrow`-violation diagnostic
at line 957, but I have NOT confirmed there's an analogous
"escapes-event-outside-declared-set" diagnostic for narrow `throws E`
declarations. **This is the linchpin question.** If absent, write the
diagnostic before anything else.

### Q0.2 — Call coverage: does the producer reject a `throws E` body that calls a generic-throws callee without catch-all?

Given:

```drift
pub fn g() -> Int { throw SomeOtherEvent(...); }  // generic throws

pub fn f() throws E -> Int {
    g();   // generic-throws callee, no catch-all surrounding
    return 0;
}
```

The producer must reject. The narrow declaration `throws E` is a lie if `g`
can escape anything other than E. Same investigation site. Subset check
at `checker/__init__.py:1202` already exists for the catch-coverage path;
the question is whether the **declaration-coverage** path (does the
function's own narrow declaration cover everything its body may escape?)
fires the analogous check.

### Q0.3 — Cross-package call coverage: does the producer reject a `throws E` body that calls an imported package's generic-throws function without catch-all?

Given an imported `dep_pkg.g() -> Int` with no narrow declaration in its
package metadata:

```drift
pub fn f() throws E -> Int {
    dep_pkg.g();   // imported, generic-throws or unknown
    return 0;
}
```

The producer must reject. This is the case that proves bad metadata cannot
be emitted: a producer that depends on another package cannot launder an
unbounded throws-surface through a narrow declaration. (Equivalent to Q0.2
but across package boundaries — different code path because imported
callees come from the `signatures_by_id_all` ChainMap, not local fn_infos.)

### Q0.4 — Catch coverage in producer: does a `try { g(); } catch E(e) { ... }` arm cover only E, not unrelated events?

Already exists (this is exactly what V1/V2 pin in-source). Re-confirm in the
audit, but expect YES.

### Q0.5 — Alias canonicalization in declaration: does `pub fn f() throws Alias` where `pub type Alias = E` resolve to E in `declared_throws_event_fqns`?

The producer's `_resolve_declared_throws_types` (`type_resolver.py:403,
437`) must canonicalize. Investigate whether it does today for the alias
case. If it does NOT, that's a producer-side §B-equivalent bug for the
declaration side — fix as a sub-step (or, if it lands separately, declare
this slice depends on it).

### Q0.6 — Declared event FQNs use canonical form: are FQNs in `declared_throws_event_fqns` already alias-resolved to the underlying pub-error's defining module?

Stated in the original plan as a claim ("the FQNs the producer writes are
already the underlying pub-error FQNs"). Verify with a unit test (not just
code reading) before relying on it for emit.

### Phase 0 acceptance

Write the audit as a brief read-only report in
`work/cross-pkg-narrow-throws-metadata/phase0-enforcement-audit.md`. For
each question, cite file:line and a small in-source test confirming the
behavior. If any answer is NO, the report ends with one of:

- "Producer enforcement gap: <description>. Slice blocked on fixing this first."
- "Producer enforcement is structurally absent. Recommend removing `throws E` syntax — see Section 6."

Only proceed to Phase 1 once Q0.1–Q0.6 are all YES with citations.

## 0b. Six-case proof matrix (acceptance gates for Phase 3)

After implementation, ALL six must pass. Any failure stops the slice.

| # | Case | Carrier | Expected |
| - | ---- | ------- | -------- |
| 1 | Same-pkg positive | `f() throws E` body throws only E; typed catch E covers | compile + run, catch reached |
| 2 | Same-pkg negative | `f() throws E` calls generic `g()` without catch-all | producer rejects |
| 3 | Cross-pkg positive | Producer `f() throws E`; consumer typed catch E | compile + run, catch reached |
| 4 | Cross-pkg producer-side negative | Producer `f() throws E` calls imported pkg `g()` (generic) without catch-all | producer **package build** rejects — proves bad metadata cannot be emitted |
| 5 | Cross-pkg consumer-side negative | Consumer catches E around call to imported `g()` declared without `throws` (generic-throws or pre-slice old package) | consumer rejects (catch E does NOT cover generic) |
| 6 | Alias case | Consumer `catch api:Alias(e)` where `pub type Alias = inner.E`; producer's narrow list is canonical `[inner:E]` | covered, compile + run |

Cases 1, 3, 6 map to existing V1/V2, V0 (new), V3/V4. Cases 2, 4, 5 are
new negative carriers — write each as its own driver test.

**If any of cases 2, 4, 5 cannot be made to pass given the current
producer-side checker, return to Phase 0** — that is the signal that
producer enforcement is incomplete.

## 1. Root cause confirmed (assuming Phase 0 passes)

### 1a. Diagnostic site and coverage logic

The "is declared nothrow but may throw" diagnostic fires at
`lang/driftc/checker/__init__.py:957`, driven by `info.inferred_may_throw`
being set at line 940. The decision comes from `_function_may_throw`
(`checker/__init__.py:1102`) walking HIR and consulting
`_call_narrow_throws_fqns` (`checker/__init__.py:1132`) per call site to
decide if a typed catch arm covers the call.

The covered-decision in `_is_call_throws_covered`
(`checker/__init__.py:1183`):

- If catch is catch-all: covered.
- If the call has a narrow list and that list is a subset of `caught_events`: covered.
- Otherwise: NOT covered → `may_throw = True`.

`_call_narrow_throws_fqns` reads the callee's `declared_throws_event_fqns`
(`checker/__init__.py:1156`). For a foreign callee, `fn_infos.get(callee_id)`
returns `None`, so the fallback path `self._signatures_by_id.get(callee_id)`
(`checker/__init__.py:1146`) is taken. That map IS populated for foreign sigs
— `signatures_by_id_all = ChainMap(internal, external)` plumbed in at
`driftc.py:10688`. So the lookup mechanism is correct; the gap is exclusively
whether the foreign `FnSignature` carries the field.

### 1b. Producer emit: the field is dropped

`FnSignature.declared_throws_event_fqns` is set on the producer side by
`_resolve_declared_throws_types` (`lang/driftc/type_resolver.py:177`)
returning a list of `"module_id:Name"` FQNs, stored on the sig at line 248.

When the producer emits its module payload via `encode_signatures`
(`lang/driftc/packages/provisional_dmir_v0.py:926`), it emits
`declared_can_throw`, `declared_throws`, `declared_terminal_throws`, etc.
(lines 1098–1104), but **never** emits `declared_throws_event_fqns`:

```
$ grep -rn "declared_throws_event_fqns" lang/driftc/packages/
# (no matches)
```

The field is silently dropped at producer-emit time.

### 1c. Consumer decode: nothing to read

The consumer rebuilds foreign `FnSignature` instances in `lang/driftc/driftc.py:9349`
and at `:10014`. The constructors receive `declared_throws`,
`declared_terminal_throws`, etc., but not `declared_throws_event_fqns`. With
the field defaulting to `None`, `_call_narrow_throws_fqns` returns `None` for
every cross-package callee, treating the call as generic-throws — no catch
arm short of catch-all can claim coverage.

### 1d. Typed-catch coverage logic is fine

`_function_may_throw` already does the right thing once the field is present:

- `caught_events` is built with `_canonical_event_fqn`
  (`checker/__init__.py:1390`, `1459`) so the §B alias map is consulted —
  `api:ManagedError` → `producer_pkg.inner:ManagedError`.
- The producer's narrow list is already-canonical FQNs.
- Subset check at `checker/__init__.py:1202` compares those two canonical sets.

This is exclusively an emit + decode gap. The typed-catch coverage path
itself is correct. V2 (same-module) passes precisely because no metadata
round-trip is involved.

## 2. Fix shape

**Producer emit + consumer decode** — symmetric add of one optional field.

- **Producer:** in `encode_signatures` (`provisional_dmir_v0.py:1062`),
  serialize `declared_throws_event_fqns` as a JSON list when non-None.
- **Consumer:** in the decode at `driftc.py:9349` (and the parallel
  `:10014`), validate-then-read it back and pass to `FnSignature(...)`.

The FQN form is already canonical `"module_id:Name"` strings — the same form
`exception_schemas` keys use and the same form the canonicalized
`caught_events` set uses. The FQNs the producer writes are already the
underlying pub-error FQNs (not aliases), because `_resolve_declared_throws_types`
resolves through `exception_schemas` (`type_resolver.py:439`). The §B
alias-canonicalization only needs to run on the consumer's catch-arm side,
which it already does. **No additional alias resolution is required for the
V4 facade case.**

## 3. Step-by-step implementation (Phases 1–3)

**Gate:** Phase 0 audit committed and all questions YES.

### Step 1 — Emit the field on the producer side

**File:** `lang/driftc/packages/provisional_dmir_v0.py`
**Function:** `encode_signatures` (line 926)
**Change:** In the per-signature `entry` dict built around line 1062, add
next to `"declared_throws"` (line 1103):

```python
"declared_throws_event_fqns": (
    list(sig.declared_throws_event_fqns)
    if getattr(sig, "declared_throws_event_fqns", None) is not None
    else None
),
```

Emit `None` explicitly when the producer had no narrow declaration — the
decoder must distinguish `None` (generic throws) from `[]` (declared with
empty list).

**Ordering: preserve source order, do NOT sort.** Source order is
deterministic-from-source and better for diagnostics; the coverage check
converts to a set at `checker/__init__.py:1202` anyway, so ordering does not
affect correctness. (Decision per review.)

### Step 2 — Decode the field on the consumer side (primary path)

**File:** `lang/driftc/driftc.py`
**Function:** foreign-signature decoder at line ~9349
**Change:** Validate shape before constructing the list — never blindly
`list(sd[...])` on untrusted package metadata (a stray string would become a
list of characters):

```python
raw = sd.get("declared_throws_event_fqns")
if raw is None:
    declared_throws_event_fqns = None
elif isinstance(raw, list) and all(isinstance(x, str) for x in raw):
    declared_throws_event_fqns = list(raw)
else:
    raise ValueError(
        "invalid declared_throws_event_fqns in foreign signature "
        f"{sd.get('name', '?')}: expected list[str] or null"
    )
```

Then pass `declared_throws_event_fqns=declared_throws_event_fqns` to the
`FnSignature(...)` constructor, matching the pattern at lines 9363–9364.
Fail-closed on malformed input — this is package metadata, not user source.

### Step 3 — Decode in the parallel decode path

**File:** `lang/driftc/driftc.py`
**Function:** second decode site at line ~10014
**Change:** Same validate-then-construct shape as Step 2.

Both paths must round-trip the field or asymmetric test failures will surface.

### Step 4 — Back-compat: no payload version bump needed

The package format is `payload_kind=provisional-dmir`, `payload_version=0`,
`unstable_format=true` (`package_v0.py:148-150`). Precedent: terminal-throws
Phase 3 (`provisional_dmir_v0.py:830-835`, `:1099-1104`) added
`declared_throws`/`declared_terminal_throws` with the same forward-compat
rule — missing field → default `None`. Old packages decoded by new consumers
degrade to existing generic-throws behavior. New packages decoded by old
consumers ignore the unknown key.

**Compiler version: bump `DRIFTC_VERSION` in `lang/versions.py`** (currently
`0.31.105` → `0.31.106`). No `DRIFT_RT_ABI_VERSION` bump — this is a
compile-time metadata change, not a runtime/ABI boundary change.

### Step 5 — Sanity-check `type_aliases` round-trip (no code change expected)

V4's §B canonicalization map (built at `checker/__init__.py:446`) needs
`producer_pkg.api` aliases visible on the consumer. The `type_aliases`
round-trip via `provisional_dmir_v0.py:843-854` (emit) and
`type_table_link_v0.py:564-578` (decode). Should populate as expected —
verify during local validation. If cross-pkg aliases turn out to be filtered,
small follow-up to widen the alias map's input, but don't block on it.

## 4. Test plan

### 4a. NEW V0 carrier: cross-pkg, no alias (decouples metadata from §B)

Add a focused cross-package carrier that exercises **only** the
narrow-throws metadata round-trip, independent of `pub type` aliasing.
Suggested placement: a new test function in `lang/tests/driver/test_typed_catch_through_pub_type_alias.py`
named `test_v0_cross_package_catch_no_alias`, OR a new sibling file. (Same
file is preferable so the V0/V2/V3/V4 progression reads as one carrier
matrix.)

```drift
// producer (single module)
module producer_pkg;
import std.core as core;
export { Inner, do_throw };

pub error Inner { tag: String }

pub fn do_throw() throws Inner -> Int { throw Inner(tag = "hit"); }
```

```drift
// consumer
module main;
import std.core as core;
import producer_pkg as producer_pkg;

pub fn main() nothrow -> Int {
    try {
        val n = producer_pkg.do_throw();
        return 99;
    } catch producer_pkg:Inner(e) {
        if e.tag == "hit" { return 0; }
        return 1;
    }
}
```

**Post-slice expectation:** compile + run, binary returns 0. This pins the
metadata fix independently of `pub type Alias = Inner`. If V0 passes but V3
fails, the bug is on §B alias canonicalization; if V0 fails, the bug is on
the metadata round-trip. Clean separation.

### 4b. Acceptance gate: drop xfail from V3/V4

**File:** `lang/tests/driver/test_typed_catch_through_pub_type_alias.py`

After the slice lands:

- Lines 397–410 (V3): delete the comment block and the `if res.returncode != 0 and _KNOWN_CROSS_PKG_THROWS_GAP_MARKER in res.stderr: pytest.xfail(...)` block.
- Lines 506–510 (V4): same shape.
- Line 352: delete the now-unused `_KNOWN_CROSS_PKG_THROWS_GAP_MARKER` constant.

V3/V4 then assert `res.returncode == 0` and `run.returncode == 0` directly.
The §B-specific `E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA` assertions at `:387`
(V3) and `:496` (V4) stay — they continue to pin the §B fix.

V1, V2 pass unchanged.

### 4c. Metadata inspection test for the round-trip itself

Add `lang/tests/driver/test_pkg_round_trip_narrow_throws.py`. Match existing
format: `# vim: set noexpandtab: -*- indent-tabs-mode: t -*-` header + tab
indentation.

Shape:

1. Build a tiny producer module: `pub error E { tag: String }` + `pub fn f() throws E -> Int { throw E(tag = "x"); }`.
2. Emit a `.dmp` (signed or unsigned).
3. Load the package via the public consumer path and inspect the foreign `FnSignature` for `f`. Assert `sig.declared_throws_event_fqns == ["<producer_mod>:E"]`.

Pins the metadata round-trip independently of the catch-coverage analysis —
future regressions fail LOUD with a clear message instead of cascading into
"may throw".

Also add one negative test in the same file: a hand-crafted decode call with
a malformed `declared_throws_event_fqns` (e.g., a bare string, or a list
containing a non-string) must raise `ValueError`. Pins the Step 2/3 shape
validation.

### 4d. Coverage matrix after the slice

| Test | Before slice | After slice |
| --- | --- | --- |
| V1 (same-module, no alias) | pass | pass |
| V2 (same-module, alias) | pass | pass |
| **V0 NEW (cross-pkg, no alias)** | **n/a** | **pass (binary returns 0)** |
| V3 (cross-pkg, single-module producer, alias) | runtime-xfail | pass (binary returns 0) |
| V4 (cross-pkg, facade-module producer, alias) | runtime-xfail | pass (binary returns 0) |
| metadata round-trip inspection | n/a | pass |
| malformed-metadata decode | n/a | pass (raises `ValueError`) |

This slice is metadata-only — no codegen change.

## 5. Risks and out-of-scope

**Explicitly OUT of scope:**

- **Effect-system overhaul.** Single-field round-trip only.
- **Generic-throws inference changes.** Functions without a `throws TYPE_LIST` still emit `None`; catch-all-required behavior preserved.
- **Interface / trait method narrow-throws metadata.** The fix targets `FnSignature` package round-trip only. Interface/trait method schemas (the per-method declared-throws metadata that travels with interface descriptors) are a separate carrier and not touched here. If/when a cross-pkg interface method with narrow throws becomes a blocker, that's its own slice.
- **§B alias canonicalization.** Already landed in `60d91873` — do NOT touch `_alias_to_pub_error_fqn`, `_canonical_event_fqn`, `_canonical_pub_error_fqn`, or `_canonical_event_fqn_for_alias`.
- **Producer-side alias-in-throws-clause** (e.g., `throws producer_pkg.api.ManagedError`). V0/V3/V4 carriers use the underlying name. Note as a follow-up if a real producer hits it.
- **`or_throw()` cross-pkg narrowing.** Special-case path at `checker/__init__.py:1159-1180` is independent of `declared_throws_event_fqns`.

**Risks:**

- **JSON canonicalization & determinism.** Adding a field changes per-signature canonical bytes → payload sha256 shifts. Precedent: terminal-throws Phase 3 did the same. Verify CI doesn't depend on a golden hash; regenerate if so.
- **List ordering.** Preserve source order (decision per review). The set conversion at `checker/__init__.py:1202` makes ordering coverage-neutral, but ordering still affects payload hashes — accepted, same as terminal-throws Phase 3.
- **Decode hardening.** Shape validation in Steps 2/3 fails closed on malformed input. Old packages emitted before this slice simply omit the key and decode cleanly via the `None` branch; only actively-corrupt or maliciously-crafted payloads trigger the `ValueError`.
- **In-flight maria-rpc build.** Old `.dmp` files keep working with degraded (catch-all-required) coverage — no hard break. Maria-rpc producer must rebuild with new `driftc` to see narrow-throws coverage.

**Sequencing:** Slice is small and orthogonal to the maria-rpc keepalive
blocker. Land independently; flag the rebuild requirement to the maria-rpc
team.

## Critical files

- `lang/driftc/packages/provisional_dmir_v0.py` — emit
- `lang/driftc/driftc.py` — decode (two sites, with shape validation)
- `lang/versions.py` — `DRIFTC_VERSION` bump (no ABI bump)
- `lang/driftc/checker/__init__.py` — read-only sanity check
- `lang/driftc/type_resolver.py` — read-only sanity check (producer-side FQN format reference)
- `lang/tests/driver/test_typed_catch_through_pub_type_alias.py` — add V0 carrier, drop V3/V4 xfails
- `lang/tests/driver/test_pkg_round_trip_narrow_throws.py` — NEW (metadata round-trip + malformed-input decode)

## 6. Alternative: remove `throws E` entirely (kill-switch path)

If Phase 0 surfaces a producer-side enforcement gap that is structurally
hard to close — or if the team decides the engineering cost of bullet-proof
narrow-throws enforcement exceeds its value — the honest path is to remove
the narrow-list `throws E` syntax from the surface language, rather than
keep a half-enforced contract.

Shape:

- Surface: `pub fn f() throws -> T` (generic, no narrow list) and
  `pub fn f() nothrow -> T` remain. `pub fn f() throws E, F -> T` is
  parsed but emits a deprecation diagnostic (Phase A) then is removed
  (Phase B).
- Semantics: every non-nothrow function is generic-throws. Catch arms
  must be catch-all (`catch (e)` or `catch _`) or use runtime type tests
  on the caught value. No compile-time narrow-coverage check.
- Migration: existing `throws E` declarations are rewritten to `throws`;
  call sites that relied on narrow coverage gain catch-all arms.
- Removes the entire surface area covered by §B + this slice + Phase 0
  audit. Eliminates an entire class of "metadata lies cross packages"
  bugs.
- Cost: maria-rpc and any other narrow-catch consumer site loses the
  precision and rewrites to catch-all. Diagnostic quality drops at those
  sites.

**Decision point belongs to the team, not the plan.** This section exists
so the kill-switch is a real, written-down option rather than something
that comes up in review and stalls the slice.

## Review-decision summary (folded in)

- ✅ **Contract-first framing.** Phase 0 producer-enforcement audit is a
  hard prerequisite. Stop-the-line clause is binding.
- ✅ **Six-case proof matrix** (1–6) gates Phase 3 acceptance. Cases 2,
  4, 5 are new negative carriers proving bad metadata cannot be emitted
  or trusted.
- ✅ **Kill-switch path** (Section 6) explicitly documented: remove
  `throws E` entirely if enforcement can't be honored.
- ✅ Add V0 cross-pkg no-alias carrier — decouples narrow-throws metadata from §B.
- ✅ Decode validates shape (list[str] or null), fail-closed on malformed.
- ✅ Title scoped to `FnSignature` metadata; interface/trait methods explicitly out of scope.
- ✅ Preserve source order in emitted list (no sort).
- ✅ Bump `DRIFTC_VERSION` only; no `DRIFT_RT_ABI_VERSION` bump.
