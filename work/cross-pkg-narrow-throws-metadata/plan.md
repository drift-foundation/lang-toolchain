# Implementation Plan: Cross-Package Narrow-Throws Contract

## STATUS — LANDED (2026-05-18)

All five implementation steps (A: producer enforcement, B: declaration-side
alias canonicalization, C: producer emit, D/E: consumer decode) shipped
together. `DRIFTC_VERSION` bumped to `0.31.106`; no ABI bump.

**Proof matrix:**

| # | Case | Status |
| - | ---- | ------ |
| 1 | Same-pkg positive (V1, positive controls) | ✅ |
| 2 | Same-pkg negative (Q0.1, Q0.2) | ✅ rejects with `E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET` |
| 3 | Cross-pkg positive (V0, V2) | ✅ |
| 4 | Cross-pkg producer-side negative | ✅ producer build rejects |
| 5 | Cross-pkg consumer-side negative (typed catch around generic) | ✅ rejects (existing behavior pinned) |
| 6 | Alias case (V3, V4) | ✅ no longer xfailing |

One xfail kept: the Case 4 *positive* control (`catch _ { throw E(...) }`
inside a producer package build) hits an **orthogonal infrastructure gap**
unrelated to this slice — `missing trait metadata for 'std.core.Diagnostic'`
during package-mode type checking. Same pattern works in single-module
compilation. Marked `pytest.mark.xfail(strict=True)` with the explanation
inline; investigate separately.

## 0. Framing — contract first, plumbing second

**This is a contract-enforcement slice with the metadata round-trip folded
in as the second half.**

The principle: `pub fn f() throws E -> T` is a binding contract that f can
escape ONLY events of type E (or an alias canonicalizing to E). Consumers
may trust the producer's narrow-throws declaration **only because producer
package compilation made lying impossible**. If producer-side enforcement
is incomplete, emitting `declared_throws_event_fqns` into package metadata
propagates a lie — and a consumer that catches `E` will silently miss an
escaping `F` at runtime. That is worse than today's generic-throws
fallback, which forces catch-all coverage.

**Phase 0 audit (2026-05-18) found three NOs and one stricter-NO.** See
`phase0-enforcement-audit.md` for the full report with file:line citations.
Summary: the producer-side checker has no diagnostic comparing a function's
body escape set against its own `declared_throws_event_fqns` (Q0.1, Q0.2,
Q0.3); and `_resolve_declared_throws_types` doesn't consult the alias chain
on the declaration side (Q0.5).

**Decision (2026-05-18):** combined slice. Land enforcement + metadata
round-trip as one atomic change. Rationale: a sequential "enforcement first,
metadata second" sequence has a chicken-and-egg window — post-enforcement
but pre-metadata, any `pub fn f() throws E` whose body calls a cross-pkg
callee would falsely fail enforcement because the foreign signature's
narrow throws are still dropped at the metadata boundary. The combined
slice avoids that window. The kill-switch path (Section 6) remains a real
written-down option for the team if the combined-slice scope is judged too
large.

Scope-narrowing note: the slice fixes the producer→consumer round-trip of
`FnSignature.declared_throws_event_fqns` and the producer-side enforcement
that makes that field trustworthy. It does **not** add narrow event-FQN
metadata to interface/trait method schemas — that's a separate slice if/when
a real carrier surfaces.

## 0a. Phase 0 — producer-enforcement audit (COMPLETE)

Findings recorded in `phase0-enforcement-audit.md`. Quick reference:

| Q | Question | Verdict |
| - | -------- | ------- |
| 0.1 | Body escapes event outside declared set | **NO** — no diagnostic exists |
| 0.2 | Body calls same-pkg generic callee without catch-all | **NO** — same root |
| 0.3 | Body calls cross-pkg generic callee without catch-all | **NO** — same root |
| 0.4 | Typed catch arm covers only its event | YES |
| 0.5 | `throws Alias` canonicalizes via alias chain on decl side | **NO** — emits `E_THROWS_NOT_ERROR_TYPE` |
| 0.6 | Declared FQNs are canonical underlying form | YES (vacuously for Q0.5 case) |

Root cause of Q0.1/Q0.2/Q0.3 is shared: `checker/__init__.py:_function_may_throw`
collects only `may_throw: bool`, never the per-event escape set. The
nothrow-violation diagnostic at line 957 gates on `explicit is False`, so
narrow `throws E` declarations are exempt and no other diagnostic takes its
place.

Root cause of Q0.5: `type_resolver.py:_resolve_declared_throws_types` looks up
throws-clause names against `exception_schemas` only; `type_aliases` is never
consulted on the declaration side. The §B fix that landed alias
canonicalization on the catch side did not symmetrically cover the
declaration side.

Detail kept in the audit report.


## 0b. Six-case proof matrix (acceptance gates for the combined slice)

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

**If any of cases 2, 4, 5 cannot be made to pass given the implementation,
something is wrong with enforcement** — the slice is not done.

## 1. Root cause summary (full audit in `phase0-enforcement-audit.md`)

### 1a. Enforcement gap (Q0.1/Q0.2/Q0.3)

`checker/__init__.py:_function_may_throw` (line 1102) collects only
`may_throw: bool`, never the per-event escape set. The nothrow-violation
diagnostic at line 957 gates on `explicit is False`, so narrow `throws E`
declarations are exempt. No declaration-coverage check exists for the
declaring function's body. Same root for same-pkg (Q0.2) and cross-pkg
(Q0.3) call-coverage: the body walker decides "may_throw" from the call's
narrow-set vs catch coverage but never compares the resulting escape set
against the function's own `declared_throws_event_fqns`.

### 1b. Declaration-side alias gap (Q0.5)

`type_resolver.py:_resolve_declared_throws_types` (lines 403-449) looks up
throws-clause names against `exception_schemas` only. `pub type Alias = E`
lives in `type_aliases` (not `exception_schemas`), so `throws Alias`
falls through to `E_THROWS_NOT_ERROR_TYPE`. The §B fix landed alias
canonicalization on the catch side
(`checker/__init__.py:_alias_to_pub_error_fqn`) but did not symmetrically
cover the declaration side.

### 1c. Metadata round-trip gap (independent — pure plumbing)

`FnSignature.declared_throws_event_fqns` is set on the producer side by
`_resolve_declared_throws_types` (`type_resolver.py:177,248`). When the
producer emits its module payload via `encode_signatures`
(`provisional_dmir_v0.py:926`), it emits `declared_can_throw`,
`declared_throws`, `declared_terminal_throws`, etc. (lines 1098-1104), but
**never** emits `declared_throws_event_fqns`. The consumer rebuilds foreign
`FnSignature` instances in `driftc.py:9349` and `:10014`; neither site
reads the field. With the field defaulting to `None` for every foreign
sig, `_call_narrow_throws_fqns` returns `None` for every cross-package
callee — treating the call as generic-throws, defeating typed-catch
coverage. The typed-catch coverage logic itself
(`checker/__init__.py:_is_call_throws_covered`, line 1183, with §B alias
canonicalization at lines 1390/1459) is already correct — V2 (same-module)
passes precisely because no metadata round-trip is involved.

## 2. Fix shape

Four coordinated changes:

- **A. Producer enforcement (body-coverage):** extend
  `_function_may_throw` to track a per-event escape set; add diagnostic
  `E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET` in the inference loop tail
  when a narrow declaration doesn't cover the body's escape set. Closes
  Q0.1/Q0.2/Q0.3.
- **B. Declaration-side alias canonicalization:** extend
  `_resolve_declared_throws_types` to consult `type_aliases` before
  emitting `E_THROWS_NOT_ERROR_TYPE`. Mirror the §B walker. Closes Q0.5.
  Preserves Q0.6 invariant (FQNs always-canonical).
- **C. Producer emit:** serialize `declared_throws_event_fqns` in
  `encode_signatures` as a JSON `list[str]|null` next to `declared_throws`.
- **D. Consumer decode:** read the field at both decode sites with
  fail-closed shape validation (list[str] or null, anything else raises
  `ValueError`).

Order matters during implementation: A+B must compile (and reject the new
negative carriers) BEFORE C+D land — otherwise C+D would emit metadata that
the producer hasn't yet enforced. Once A+B are in place, C+D are the
plumbing that lets consumers trust the now-enforced contract.

## 3. Step-by-step implementation

### Step A — Producer enforcement: per-event escape set + diagnostic

**File:** `lang/driftc/checker/__init__.py`
**Functions:** `_function_may_throw` (line 1102) and the inference-loop
tail (lines 909-962).

**Approach (minimal disruption):** keep the existing `may_throw: bool` for
backward callsite compat; ADD an `escape_events: set[Optional[str]]`
accumulator that is populated everywhere `may_throw = True` is set. Each
escape is annotated with the event FQN if known; `None` (sentinel) means
"untyped/generic escape — covered only by catch-all".

Sites that record into `escape_events`:

| Site | Line(s) | What to record |
| ---- | ------- | -------------- |
| `HCall` uncovered | 1269-1274 | narrow set if known via `_call_narrow_throws_fqns`, else `None` |
| `HMethodCall` uncovered | 1339-1344 | same as above |
| `HInvoke` uncovered | 1371-1376 | same as above |
| `HIndex` w/ `indexing_throws` | 1417-1422 | `None` (generic — index errors don't carry an event FQN at this layer) |
| `HRethrow` outside catch-all | 1438-1442 | `None` (rethrowing unknown captured Error) |
| `HThrow` w/ `HExceptionInit` uncovered | 1444-1454 | `stmt.value.event_fqn` (known) |
| `HThrow` non-init uncovered | 1444-1454 | `None` |
| Missing-CallInfo conservative | 1290, 1306 | `None` |

**Return shape:** `(may_throw, first_span, first_note, missing_callinfo_diags, escape_events)`. `may_throw` becomes `bool(escape_events)` to preserve the boolean.

**Diagnostic site** (inference-loop tail, after line 941):

```python
declared = info.signature.declared_throws_event_fqns
if declared is not None:
    declared_set = set(declared)
    excess = {e for e in info.escape_events if e is None or e not in declared_set}
    if excess:
        notes = []
        if None in excess:
            notes.append("body may escape a generic/untyped event; declare `throws` (no list) or wrap the offending call in a catch-all")
        named = sorted(e for e in excess if e is not None)
        if named:
            notes.append(f"body may escape events outside declared set: {', '.join(named)}")
        diagnostics.append(_chk_diag(
            message=f"function {function_symbol(fn_id)} declares narrow throws but body may escape events outside the declared set",
            code="E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET",
            severity="error",
            span=first_throw_span_by_fn.get(fn_id, Span()),
            notes=notes,
        ))
```

`FnInfo` needs an `escape_events` field (next to `inferred_may_throw` at
line 263) to plumb the set from `_function_may_throw` to the diagnostic
emission site.

**Subtleties:**

- **Fixed-point convergence.** The existing inference loop iterates until
  `inferred_may_throw` stops changing (this is where `_call_may_throw`
  reads other functions' inferred state). The escape set must also reach
  a fixed point. For correctness, the loop's convergence condition should
  also check that `escape_events` is unchanged — otherwise a late escape
  through a previously-believed-nothrow callee could be missed.
- **Boundary calls.** Lines 1235-1248 handle boundary/cross-module calls
  with the existing `declared_can_throw` check. Make sure these still
  produce the right entry in the escape set (likely `None` since boundary
  calls without narrow metadata are generic).

### Step B — Declaration-side alias canonicalization

**File:** `lang/driftc/type_resolver.py`
**Function:** `_resolve_declared_throws_types` (line 403-449)

**Change:** Before line 446's `E_THROWS_NOT_ERROR_TYPE` fallthrough,
consult `table.type_aliases` (or the equivalent — check the §B walker's
input map). If the name resolves through the alias chain to an entry in
`exception_schemas`, use the canonical underlying FQN.

Reference walker: `checker/__init__.py:_alias_to_pub_error_fqn` (line 446
in checker, distinct from line 446 here). Mirror the same alias-chain
traversal with a guard against cycles.

Test the cycle guard: `pub type A = B; pub type B = A;` must not infinite-
loop; emit `E_THROWS_NOT_ERROR_TYPE` (or a dedicated cycle diagnostic) and
move on.

### Step C — Producer emit `declared_throws_event_fqns`

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

### Step D — Consumer decode (primary path)

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

### Step E — Consumer decode (parallel path)

**File:** `lang/driftc/driftc.py`
**Function:** second decode site at line ~10014
**Change:** Same validate-then-construct shape as Step D.

Both paths must round-trip the field or asymmetric test failures will surface.

### Step F — Back-compat: no payload version bump needed

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

### Step G — Sanity-check `type_aliases` round-trip (no code change expected)

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

### 4a-1. NEW: Case 2 negative carrier — same-pkg `throws E` calls generic `g()` without catch-all

New driver test `test_narrow_throws_enforcement_same_pkg.py`. Must produce
`E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET` from the producer:

```drift
module main;
import std.core as core;

pub error E { tag: String }
pub error OutOfScope { tag: String }

pub fn g() -> Int { throw OutOfScope(tag = "leak"); }

pub fn f() throws E -> Int {
	val n = g();
	return n;
}
```

Pre-slice: compiles (predicted from audit). Post-slice: rejected on the
`g()` call site, citing the `throws E` declaration. Use the test as
acceptance for Step A.

Variant 2b — same shape but the body uses a direct `throw F(...)` (the
Q0.1 shape). Same expected outcome.

### 4a-2. NEW: Case 4 negative carrier — cross-pkg `throws E` calls imported generic `g()` without catch-all

New driver test `test_narrow_throws_enforcement_cross_pkg.py`. Producer
declares `throws E` body that calls an imported package's generic-throws
function. Producer **package build** must reject:

```drift
// dep_pkg.drift
module dep_pkg;
import std.core as core;
export { Boom, g };

pub error Boom { tag: String }

pub fn g() -> Int { throw Boom(tag = "leak"); }
```

```drift
// producer.drift
module producer_pkg;
import std.core as core;
import dep_pkg as dep_pkg;
export { E, f };

pub error E { tag: String }

pub fn f() throws E -> Int {
	val n = dep_pkg.g();
	return n;
}
```

Note: the producer package's build is what gets rejected (not the
consumer). This is the assertion that proves bad metadata cannot be
emitted into a `.dmp`.

### 4a-3. NEW: Case 5 negative carrier — consumer catches E around generic call

New driver test `test_narrow_throws_consumer_generic_callee.py`. Consumer
imports a package whose function has no narrow declaration (or is built
with an old pre-slice driftc, simulated by emitting a package without the
new field). Consumer's `try { dep.g(); } catch E(e) {}` must NOT compile —
catch E doesn't cover generic:

```drift
// consumer
module main;
import std.core as core;
import dep_pkg as dep_pkg;

pub error E { tag: String }

pub fn main() nothrow -> Int {
	try {
		val n = dep_pkg.g();   // generic-throws callee
		return n;
	} catch main:E(e) {
		return 0;
	}
}
```

Expected: rejection — main remains may-throw because catch E doesn't cover
a generic-throws call. (This is existing behavior; the test pins it stays
that way after the slice. If the slice accidentally promoted generic to
narrow somehow, this test catches it.)

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
containing a non-string) must raise `ValueError`. Pins the Step D/E shape
validation.

### 4d. Coverage matrix after the slice

| Test | Before slice | After slice |
| --- | --- | --- |
| V1 (same-module, no alias) | pass | pass |
| V2 (same-module, alias) | pass | pass |
| **V0 NEW (cross-pkg, no alias)** | **n/a** | **pass (binary returns 0)** |
| V3 (cross-pkg, single-module producer, alias) | runtime-xfail | pass (binary returns 0) |
| V4 (cross-pkg, facade-module producer, alias) | runtime-xfail | pass (binary returns 0) |
| Case 2 negative (same-pkg, escape outside set) | n/a | pass (producer rejects with `E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET`) |
| Case 4 negative (cross-pkg, escape outside set) | n/a | pass (producer rejects) |
| Case 5 negative (consumer catches E around generic call) | already rejects | still rejects |
| Alias on declaration side — `throws Alias` (Q0.5) | rejects with `E_THROWS_NOT_ERROR_TYPE` | accepts, canonicalizes to underlying FQN |
| metadata round-trip inspection | n/a | pass |
| malformed-metadata decode | n/a | pass (raises `ValueError`) |

This slice adds producer enforcement + a new optional metadata field — no
codegen change.

## 5. Risks and out-of-scope

**Explicitly OUT of scope:**

- **Effect-system overhaul.** Single-field round-trip only.
- **Generic-throws inference changes.** Functions without a `throws TYPE_LIST` still emit `None`; catch-all-required behavior preserved.
- **Interface / trait method narrow-throws metadata.** The fix targets `FnSignature` package round-trip only. Interface/trait method schemas (the per-method declared-throws metadata that travels with interface descriptors) are a separate carrier and not touched here. If/when a cross-pkg interface method with narrow throws becomes a blocker, that's its own slice.
- **§B alias canonicalization.** Already landed in `60d91873` — do NOT touch `_alias_to_pub_error_fqn`, `_canonical_event_fqn`, `_canonical_pub_error_fqn`, or `_canonical_event_fqn_for_alias`.
- **Producer-side alias-in-throws-clause** (e.g., `throws producer_pkg.api.ManagedError`). V0/V3/V4 carriers use the underlying name. Note as a follow-up if a real producer hits it.
- **`or_throw()` cross-pkg narrowing.** Special-case path at `checker/__init__.py:1159-1180` is independent of `declared_throws_event_fqns`.

**Risks:**

- **Existing `throws E` source breakage.** The audit found only 7 narrow-throws declarations in `.drift` files, all in test fixtures, all same-module bodies throwing their declared event directly. They should all pass enforcement. Downstream (maria-rpc) producers may have patterns that fail enforcement once exercised — that's the slice doing its job, but plan for inbound questions when the new diagnostic fires.
- **Fixed-point convergence regression.** Step A changes the inference loop's convergence shape (escape set vs. boolean). Verify the loop still terminates in obvious cases (mutual recursion between two `throws E` functions). Add at least one mutual-recursion fixture if not already present.
- **JSON canonicalization & determinism.** Adding a field changes per-signature canonical bytes → payload sha256 shifts. Precedent: terminal-throws Phase 3 did the same. Verify CI doesn't depend on a golden hash; regenerate if so.
- **List ordering.** Preserve source order (decision per review). The set conversion at `checker/__init__.py:1202` makes ordering coverage-neutral, but ordering still affects payload hashes — accepted, same as terminal-throws Phase 3.
- **Decode hardening.** Shape validation in Steps D/E fails closed on malformed input. Old packages emitted before this slice simply omit the key and decode cleanly via the `None` branch; only actively-corrupt or maliciously-crafted payloads trigger the `ValueError`.
- **In-flight maria-rpc build.** Old `.dmp` files keep working with degraded (catch-all-required) coverage — no hard break. Maria-rpc producer must rebuild with new `driftc` to see narrow-throws coverage. If maria-rpc's producer code has any Case-4-style patterns, those will fail enforcement and need fixing on their side first.

**Sequencing:** Slice is small and orthogonal to the maria-rpc keepalive
blocker. Land independently; flag the rebuild requirement to the maria-rpc
team.

## Critical files

- `lang/driftc/checker/__init__.py` — Step A: per-event escape set, diagnostic, FnInfo plumbing
- `lang/driftc/type_resolver.py` — Step B: alias canonicalization in `_resolve_declared_throws_types`
- `lang/driftc/packages/provisional_dmir_v0.py` — Step C: producer emit
- `lang/driftc/driftc.py` — Steps D/E: consumer decode (two sites, with shape validation)
- `lang/versions.py` — `DRIFTC_VERSION` bump (no ABI bump)
- `lang/tests/driver/test_typed_catch_through_pub_type_alias.py` — add V0 carrier, drop V3/V4 xfails
- `lang/tests/driver/test_narrow_throws_enforcement_same_pkg.py` — NEW (Case 2)
- `lang/tests/driver/test_narrow_throws_enforcement_cross_pkg.py` — NEW (Case 4)
- `lang/tests/driver/test_narrow_throws_consumer_generic_callee.py` — NEW (Case 5)
- `lang/tests/driver/test_pkg_round_trip_narrow_throws.py` — NEW (metadata round-trip + malformed-input decode)
- `lang/tests/driver/test_throws_alias_decl.py` — NEW (Q0.5: `throws Alias` accepted, canonicalizes)

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

- ✅ **Contract-first framing.** Phase 0 producer-enforcement audit run
  (`phase0-enforcement-audit.md`); found gaps; combined-slice decision
  taken instead of sequential.
- ✅ **Combined slice (2026-05-18 decision).** Enforcement (Steps A+B)
  and metadata round-trip (Steps C+D+E) land atomically to avoid the
  chicken-and-egg window where post-enforcement-pre-metadata would falsely
  reject cross-pkg `throws E` consumers.
- ✅ **Six-case proof matrix** (1–6) gates slice acceptance. Cases 2,
  4, 5 are new negative carriers proving the enforcement actually fires
  and bad metadata cannot be emitted.
- ✅ **Kill-switch path** (Section 6) explicitly documented: remove
  `throws E` entirely if enforcement can't be honored.
- ✅ Add V0 cross-pkg no-alias carrier — decouples narrow-throws metadata from §B.
- ✅ Decode validates shape (list[str] or null), fail-closed on malformed.
- ✅ Interface/trait methods explicitly out of scope.
- ✅ Preserve source order in emitted list (no sort).
- ✅ Bump `DRIFTC_VERSION` only; no `DRIFT_RT_ABI_VERSION` bump.
