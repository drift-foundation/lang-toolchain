# Phase 0 — Producer-Side Narrow-Throws Enforcement Audit

Date: 2026-05-18
Scope: read-only investigation. Does NOT modify code.

## STATUS — gaps closed in the same slice (2026-05-18)

The findings below describe the **pre-slice** state. After the audit ran,
the user opted for a combined slice that closes Q0.1, Q0.2, Q0.3, and Q0.5
in the same change set as the metadata round-trip (instead of landing them
as a separate prerequisite slice).  See `plan.md` Section 0 for the
chicken-and-egg rationale behind combining the two.

Mapping audit-finding → implementation step:

- **Q0.1 / Q0.2 / Q0.3** (no body-coverage diagnostic) →
  `checker/__init__.py` per-event escape-set tracker plus
  `E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET` diagnostic. Plan Step A.
- **Q0.4** (catch coverage event-strict) → already correct; left alone.
- **Q0.5** (alias on declaration side) →
  `type_resolver.py:_resolve_declared_throws_types` consults
  `type_aliases` via `_resolve_alias_chain_to_pub_error`. Plan Step B.
- **Q0.6** (canonical FQN form) → invariant preserved by Step B.

The "do NOT proceed" recommendation at the end of this report applied to
*landing only the metadata round-trip with no enforcement*.  The combined
slice landed the enforcement first inside the same change, so the
recommendation was honored by addressing the gaps rather than deferring
the slice.

## Summary verdict

**Three of six questions are NO and one (Q0.5) is NO with a stricter shape.** Q0.1, Q0.2, Q0.3 all share a single structural gap: the producer-side checker has **no diagnostic that compares a function's body escape set against its own `declared_throws_event_fqns`.** The only "may throw" diagnostic in the inference loop fires for explicit `nothrow` violations (`checker/__init__.py:957`). A narrow `pub fn f() throws E -> Int` has `declared_can_throw = True` and is therefore exempt from that check; nothing else takes its place. Q0.5 is also NO: `_resolve_declared_throws_types` only consults `exception_schemas` (which contains underlying pub-errors, not aliases), so `pub fn f() throws Alias` would produce `E_THROWS_NOT_ERROR_TYPE`, not an alias-resolved canonical FQN. Q0.4 is YES (catch coverage is event-strict). Q0.6 is YES for the simple-name case but moot for the alias case (which Q0.5 already rejects).

**Recommendation: do NOT proceed to Phase 1.** The slice would propagate `declared_throws_event_fqns` metadata that the producer never verifies the body honors — exactly the "metadata lies cross packages" scenario the plan's Section 0 forbids. The honest paths are (a) land producer-side body-coverage enforcement as a prerequisite slice (close Q0.1/Q0.2/Q0.3), and as a sub-step canonicalize aliases in the declaration resolver (close Q0.5), then build this slice on top; or (b) take Section 6's kill-switch.

## Q0.1 Body coverage

**Verdict:** NO

**Citations:**
- `lang/driftc/checker/__init__.py:909-941` — the can-throw inference loop calls `_function_may_throw` once per function. The return is a single `may_throw: bool`; the per-event escape set is never lifted out of `_function_may_throw`.
- `lang/driftc/checker/__init__.py:944-962` — the only diagnostic emitted in this pass is gated by `if explicit is False and info.inferred_may_throw` (i.e. `nothrow` declared, body throws). For a function declared narrow `throws E`, `explicit` is `True` (not `False`), so the diagnostic does not fire regardless of what the body throws.
- `lang/driftc/checker/__init__.py:1102-1508` — `_function_may_throw` is invoked with the default initial state `caught=None, catch_all=False` (`walk_block(block)` at line 1507). It never seeds an "implicit catch set" from the enclosing function's own `declared_throws_event_fqns`. At line 1444-1454, an `HThrow` of event F sets `may_throw=True` but the event identity is discarded once the walker returns.
- `lang/driftc/type_checker.py` — `grep -in "declared_throws_event_fqns" type_checker.py` returns zero matches. No declaration-side narrow-throws check lives there either.
- `grep -rn "declared_throws_event_fqns" lang/` shows the field is read only in three places: the dataclass field definition (`checker/__init__.py:171`), the producer-side resolver that populates it (`type_resolver.py:177,189,191,248`), and the **callee-side** reader inside `_function_may_throw` (`checker/__init__.py:1132-1156`). It is never read on the declaration-of-the-current-function side.

**Confirming snippet:**
```drift
module main;
import std.core as core;

pub error E { tag: String }
pub error F { tag: String }

pub fn f() throws E -> Int {
	throw F(tag = "wrong");
}

pub fn main() nothrow -> Int {
	try {
		val n = f();
		return 99;
	} catch main:E(e) {
		return 0;
	} catch main:F(e) {
		return 1;
	}
}
```

**Notes:** Expected behavior on a correctly-enforcing producer: `E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET` (or equivalent) on the `throw F(...)` site, citing the `throws E` declaration. Actual behavior (predicted from code, would need to run to confirm): compile + run, binary returns 1 (the F arm). The user will need to actually compile this to publish-the-lie confirm, but I see no code path in `lang/driftc/checker` or `lang/driftc/type_checker.py` that would produce a rejection.

## Q0.2 Same-pkg call coverage

**Verdict:** NO

**Citations:**
- Same root gap as Q0.1: `checker/__init__.py:944-962` only diagnoses `nothrow` violations, not narrow-throws body-coverage.
- `checker/__init__.py:1183-1202` — `_is_call_throws_covered` does correctly identify that a call to a generic-throws same-pkg callee from inside a `try { ... }` arm without catch-all is uncovered (returns False at line 1195 because `narrow is None`). Consequently `may_throw = True` is set (line 1269 for `HCall`). But `may_throw` only flips `info.declared_can_throw` to `True` — it never triggers a diagnostic for a narrow declaration.
- `checker/__init__.py:1156-1158` — when looking at the callee, `_call_narrow_throws_fqns` reads `decl = getattr(sig, "declared_throws_event_fqns", None)`. For a same-pkg `pub fn g() -> Int` declared without a `throws TYPE_LIST`, `decl is None`. This is the "generic throws" branch; producer-side coverage on the *caller's* declaration is still absent.

**Confirming snippet:**
```drift
module main;
import std.core as core;

pub error E { tag: String }
pub error OutOfScope { tag: String }

pub fn g() -> Int {
	throw OutOfScope(tag = "leak");
}

pub fn f() throws E -> Int {
	val n = g();
	return n;
}

pub fn main() nothrow -> Int {
	try {
		val n = f();
		return 99;
	} catch main:E(e) {
		return 0;
	} catch main:OutOfScope(e) {
		return 1;
	}
}
```

**Notes:** Predicted: compiles and runs, returns 1. Expected on a correctly-enforcing producer: rejection on the `g()` call inside `f`'s body — "call to generic-throws function may escape outside `f`'s declared throws set `{main:E}`". No such diagnostic exists today.

## Q0.3 Cross-pkg call coverage

**Verdict:** NO (same root cause as Q0.2)

**Citations:**
- `lang/driftc/driftc.py:10123-10127` — `signatures_by_id_all = ChainMap(external_signatures_by_id, derived_signatures_by_id, base_signatures_by_id)` puts foreign sigs into the same lookup map the checker consumes via `self._signatures_by_id`.
- `lang/driftc/driftc.py:9349-9384` — foreign `FnSignature` constructor (first decode site). The kwargs passed include `declared_throws`, `declared_terminal_throws`, etc., but NOT `declared_throws_event_fqns`. The field defaults to `None` on every cross-pkg sig today (pre-slice). This is the exact gap the plan's Section 1c calls out.
- `lang/driftc/checker/__init__.py:1141-1158` — `_call_narrow_throws_fqns` does fall back to `self._signatures_by_id.get(callee_id)` when `fn_infos.get(callee_id)` is None (the foreign-callee path). Reads `getattr(sig, "declared_throws_event_fqns", None)`. With the field always-None for foreign sigs, every cross-pkg callee looks generic-throws.
- `_is_call_throws_covered` therefore correctly returns False (line 1195) for a cross-pkg call from a typed-catch-only context. `may_throw = True` flips, but as in Q0.1/Q0.2, **no narrow-declaration diagnostic fires** to surface the violation.

**Confirming snippet:**
```drift
// producer.drift
module dep_pkg;
import std.core as core;
export { Boom, g };

pub error Boom { tag: String }

pub fn g() -> Int {
	throw Boom(tag = "leak");
}
```

```drift
// consumer.drift
module main;
import std.core as core;
import dep_pkg as dep_pkg;

pub error E { tag: String }

pub fn f() throws E -> Int {
	val n = dep_pkg.g();
	return n;
}

pub fn main() nothrow -> Int {
	try {
		val n = f();
		return 99;
	} catch main:E(e) {
		return 0;
	} catch dep_pkg:Boom(e) {
		return 1;
	}
}
```

**Notes:** Expected on a correctly-enforcing producer: rejection on the `dep_pkg.g()` call inside `f`. Predicted actual: compiles and returns 1 (Boom arm). The "declared `throws E` is a lie" scenario plan-Section 0 forbids. Would need to actually compile this to confirm, but the code path tracing is unambiguous: same diagnostic gap as Q0.2, applied to a foreign-callee lookup.

## Q0.4 Catch coverage

**Verdict:** YES

**Citations:**
- `lang/driftc/checker/__init__.py:1444-1454` — for an `HThrow` of an event-init expression with `event_fqn`, the walker continues (no `may_throw` flip) only if `catch_all or (caught is not None and event_fqn in caught)`. Strict membership in the caught-events set; unrelated events propagate.
- `lang/driftc/checker/__init__.py:1457-1466` — the `caught_events` set is built per `HTry` from each arm's canonicalized `event_fqn` (alias-resolved via `_canonical_event_fqn`). A `catch E(e)` arm only puts `E` into the set, never an unrelated event.
- `lang/tests/driver/test_typed_catch_through_pub_type_alias.py:218-253` (V1 carrier) and `:259-308` (V2 carrier) already pin this behavior in-source: V1 throws `Inner` from a `throws Inner` body, catches `main:Inner(e)`, and the binary returns 0 — the arm runs.

**Confirming snippet:**
```drift
module main;
import std.core as core;

pub error E { tag: String }
pub error Other { tag: String }

pub fn maybe() -> Int { throw Other(tag = "u"); }

pub fn main() nothrow -> Int {
	try {
		val n = maybe();
		return 99;
	} catch main:E(e) {
		return 1;
	}
}
```

**Notes:** Predicted: rejection — the call to `maybe()` (generic throws) is not covered by a typed `catch E(e)` arm; the producer requires a catch-all here. V1 in the test file is the positive control. No additional confirmation needed.

## Q0.5 Alias canonicalization on the DECLARATION side

**Verdict:** NO

**Citations:**
- `lang/driftc/type_resolver.py:403-449` — `_resolve_declared_throws_types`. The lookup logic is:
  - `schemas = getattr(table, "exception_schemas", {})` (line 427).
  - For each throws-clause `TypeExpr`, compute `fqn = f"{mod_id}:{name}"` (line 438).
  - If `fqn not in schemas` (line 439), try a single fallback: `alt_keys = [k for k in schemas.keys() if k.endswith(f":{name}")]` (line 442). If exactly one match, use it; otherwise emit `E_THROWS_NOT_ERROR_TYPE` (line 446) and skip.
- `lang/driftc/parser/__init__.py:5645-5650` — `exception_schemas[fqn]` is populated ONLY for `pub error`/`exception` declarations (`for exc in prog.exceptions`). `pub type Alias = E` does NOT add anything to `exception_schemas`; it goes to `type_aliases` via `core/types_core.py:541-544`.
- `lang/driftc/type_resolver.py` does not import or call `lookup_type_alias`, `_alias_to_pub_error_fqn`, `_canonical_event_fqn`, or any alias-chain walker. The §B canonicalization fix lives entirely in `checker/__init__.py:_alias_to_pub_error_fqn` (line 446) and `_canonical_event_fqn` (line 433), and is only invoked from the catch-arm side at lines 1390 and 1459.
- Consequence: a producer that writes `pub fn f() throws Alias -> Int` where `pub type Alias = E` would have `_resolve_declared_throws_types` look up `<mod>:Alias` (not in `exception_schemas`), then `endswith(":Alias")` (no schema entry with that suffix), then emit `E_THROWS_NOT_ERROR_TYPE`. The producer rejects the declaration *for the wrong reason* — not because the body lies, but because it never even understood the alias as a valid throws-type.

**Confirming snippet:**
```drift
module main;
import std.core as core;

pub error E { tag: String }
pub type Alias = E;

pub fn f() throws Alias -> Int {
	throw E(tag = "x");
}

pub fn main() nothrow -> Int {
	try {
		val n = f();
		return 99;
	} catch main:E(e) {
		return 0;
	}
}
```

**Notes:** Predicted: rejection with `E_THROWS_NOT_ERROR_TYPE` on the `throws Alias` clause. The shape that passes today: write the underlying `pub error` name directly (`throws E`). The shape that slips through (the failing case for this question): write the alias and get a misleading diagnostic instead of either acceptance-with-canonicalization OR a clear "alias resolved to underlying — proceed."

If §B-equivalent alias canonicalization were added to `_resolve_declared_throws_types`, the right shape would be: consult `type_aliases` first; if `name` resolves through the alias chain to a known pub-error FQN, populate `declared_throws_event_fqns` with that canonical underlying FQN (matching the form the §B-canonicalized `caught_events` set uses on the consumer). The plan's Q0.5 expectation is that the producer emits `declared_throws_event_fqns=["<inner>:E"]` for `throws Alias`; pre-fix, it emits `[]` plus a hard diagnostic.

## Q0.6 Canonical FQN form

**Verdict:** PARTIAL (YES for the simple case that compiles; the alias case is mooted by Q0.5)

**Citations:**
- `lang/driftc/type_resolver.py:437-448` — for an accepted throws-clause TypeExpr, the resulting FQN is either `f"{mod_id}:{name}"` matched directly against `exception_schemas` (line 438-439) OR a unique `alt_keys[0]` from `schemas.keys()` (lines 442-444). Both forms are keys of `exception_schemas`, which are populated as `fqn = f"{module_id}:{exc.name}"` for each `pub error`/`exception` declaration in the defining module (`parser/__init__.py:5646-5648`). So every FQN that enters `declared_throws_event_fqns` is keyed at the underlying pub-error's defining-module form — the same canonical form `exception_schemas` uses and the same form the consumer's `caught_events` set uses after `_canonical_event_fqn` resolution.
- The "user wrote a different syntactic form" cases collapse:
  - User wrote `throws E` (bare name, current module defines E): `mod_id` = current module → exact `exception_schemas` hit → canonical.
  - User wrote `throws producer.api.SomeError` (qualified, but the qualifier is a re-export module): `<api>:SomeError` not in schemas → `alt_keys` ends-with fallback finds the unique `<inner>:SomeError` key → canonical underlying FQN used.
  - User wrote `throws Alias` (a `pub type` alias): falls through to `E_THROWS_NOT_ERROR_TYPE`, never enters `declared_throws_event_fqns`. So the "invariant" holds vacuously in this case (no entry to violate it), but only because Q0.5 rejects it outright rather than canonicalizing.

**Confirming snippet:** none needed beyond Q0.5's snippet — Q0.6 is a downstream observation about the FQN form for cases that successfully resolve.

**Notes — invariant statement:**

For any `FnSignature` whose `declared_throws_event_fqns` is non-None, every entry is the underlying pub-error's defining-module FQN in `"module_id:Name"` form, identical to the corresponding key in `type_table.exception_schemas`. This invariant is established by `_resolve_declared_throws_types` (`lang/driftc/type_resolver.py:438-448`), which only ever stores values that already exist as keys in `exception_schemas`. The invariant matches the canonical form the consumer's catch-coverage path uses after §B alias resolution (`checker/__init__.py:1390,1459`) — emit and decode round-tripping a list of these strings would land the producer's narrow declaration and the consumer's catch-arm canonicalization in the same string-space, which is what the slice's subset check at `checker/__init__.py:1202` requires. **Caveat:** the invariant only covers cases the producer accepts. The alias case from Q0.5 doesn't produce an entry at all (today), so it doesn't break the invariant — but it also can't carry a `throws Alias` declaration across the package boundary even after this slice lands.

## Recommendation (historical — see STATUS at top of file for what actually landed)

**Pre-slice recommendation: do NOT proceed to Phase 1 of the cross-pkg narrow-throws metadata slice as a metadata-only change.** Q0.1, Q0.2, and Q0.3 all fail for the same structural reason: there is no producer-side diagnostic comparing a narrow `throws E` declaration against the body's actual escape set. Emitting `declared_throws_event_fqns` into package metadata would propagate a producer claim ("f only throws E") that the producer never verified — exactly the contract-violation scenario the plan's Section 0 stop-the-line clause names.

**What actually landed:** option 1 below, folded into the same slice as the metadata round-trip (combined-slice decision documented in `plan.md` Section 0).

Two acceptable next moves (option 1 chosen):

1. **Prerequisite slice — close producer-side enforcement first.** This requires:
   - **Body-coverage check** in `lang/driftc/checker/__init__.py`. Extend `_function_may_throw` (or add a sibling analysis) to return the per-function escape set of event FQNs, not just a boolean. Then in the loop at lines 944-962, add a diagnostic for `info.signature.declared_throws_event_fqns is not None and not body_escape_set.issubset(set(info.signature.declared_throws_event_fqns))`. Cite the offending throw/call site as the span. Suggested diagnostic code: `E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET`.
   - **Alias canonicalization on the declaration side** in `lang/driftc/type_resolver.py:_resolve_declared_throws_types`. Before falling through to `E_THROWS_NOT_ERROR_TYPE`, consult the alias chain (mirror `checker/__init__.py:_alias_to_pub_error_fqn`'s walker against `table.type_aliases`) and resolve to the underlying pub-error FQN. Closes Q0.5 and preserves the Q0.6 invariant.
   - Add the new negative carriers (plan cases 2 and 4) as driver tests in `lang/tests/driver/` and use them as acceptance gates for the enforcement slice itself.
   - Only then is it safe to land this slice (which becomes "emit + decode a field whose invariant is genuinely enforced").

2. **Section 6 kill-switch — remove `throws E` narrow-list syntax.** If body-coverage enforcement is judged structurally too expensive (e.g., the body-escape set is hard to compute precisely because of generic-throws callees, virtual dispatch through trait methods, and `or_throw()` chains), the honest move is to make all non-nothrow functions generic-throws. Consumers fall back to catch-all. The maria-rpc team accepts the precision loss. Plan Section 6 is the written-down path.

The choice between (1) and (2) belongs to the team. From the audit alone, (1) looks tractable for the in-source cases (Q0.1 is a few-dozen lines in `_function_may_throw`; Q0.5 is a copy of the `_alias_to_pub_error_fqn` walker), but the team should weigh whether interface/trait/`or_throw` interactions inflate the scope before committing.
