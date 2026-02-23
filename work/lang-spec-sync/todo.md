Findings from a focused spec drift review (`docs/design/drift-lang-spec.md`):

1. **High: stale limitation on Fn1-bounded borrowed captures** — RESOLVED
- Added Fn-bounded SCOPED inference exception to §22.2.3 (escape level rule 4).
- Added limitation note: non-Fn trait bounds do not trigger the relaxation.

2. **Medium: statement-terminator section omits bare block statements** — RESOLVED (prior session)
- Line 198 already includes standalone block statements as self-terminating.

3. **Medium: `Void` bindability semantics are not explicitly documented** — RESOLVED (prior session)
- §3.1.3 covers Void bindability, assignment in generic contexts, Copy, no-destructor, typed-context rejection.

4. **Medium: iterator throw/invalidation contract appears under-specified** — RESOLVED (prior session)
- §8.3 has normative "Iterator throw contract" block: next()/prev() are nothrow, invalidation is deterministic abort.

All items closed.
