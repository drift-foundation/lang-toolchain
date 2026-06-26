# Design note — splitting the stdlib: bootstrap `core` (in-toolchain) vs certified user-land

**Date:** 2026-06-26 (UTC)
**From:** build-orchestrator (Slawomir Lisznianski, sl@pushcoin.com)
**To:** drift-lang / toolchain team
**Status:** observation + proposal for your consideration — **not a request**. This is a
toolchain release-engineering decision; we're flagging it from the orchestrator/consumer
vantage while it's fresh. No action expected on your side beyond a think.

## Where this came from

While wiring the orchestrator up to app-cert (driftc 0.33.61), we worked through "is
`drift-lang` itself a certifiable artifact?" In the preflight, `drift-lang` is correctly
skipped — `kind: toolchain`, no author-claims — because the stdlib today rides *inside* the
toolchain (deployed to `toolchain_root`, resolved via `DRIFT_TOOLCHAIN_ROOT`), not as a
package in `libs_root`. That's the right call as things stand.

It did surface a latent question, though: **the whole stdlib is implicitly trusted today**
purely by virtue of being baked into the toolchain. There's no independent cert leg for any
of it — containers, concurrency, the lot. As the stdlib grows, that's a growing blob of
un-certified, implicitly-trusted surface inside the root of trust.

## The idea (yours to take or leave)

Split the stdlib along the **bootstrap line**:

- **`core` / internals** — the minimal surface the compiler links to self-host. Stays baked
  in the toolchain, part of the TCB / root of trust. No cert leg — it *is* the certifier's
  own substrate. Exactly today's model.
- **user-land** (containers, concurrency, …) — promoted to a normal **certified package**:
  built by the staged compiler, author-claim + cert-claim signed with the Foundation key,
  run through test/stress/perf gates, snapshot-pinned, consumed downstream via
  `DRIFT_PACKAGE_ROOT` like any other pool package.

The two are disjoint, so the bootstrap chicken-and-egg never arises — the compiler never
needs containers/concurrency to build itself, and there's no "two instances of the same
stdlib" ambiguity.

## Why it looks attractive from where we sit

- **Smaller TCB.** Only `core` stays implicitly trusted. The high-complexity, high-surface
  parts of the stdlib — exactly the code with the most interesting concurrency/stress/perf
  failure modes — come under the *same* explicit gate + evidence + signing regime as
  everything else in the pool. Smaller trusted base, more surface actually certified.
- **ABI coupling stops being special.** user-land-stdlib → toolchain becomes an honest
  `depends_on` edge with a Lock v2 compatibility range pinned to the ABI, expressed exactly
  like `drift-net-tls` or `drift-web` already express theirs. No bespoke handling.
- **It's a security story, not just packaging hygiene** — the stdlib's heaviest surface
  gets provenance + author/cert legs + reproducible gates instead of "trusted because it
  shipped in the toolchain tarball."

## Orchestrator-side cost: ~zero (if you split the release unit)

Modeled as its own `package_repo` (a split-out `drift-stdlib`, or a second manifest treated
as one), `depends_on: ["drift-lang"]`, the orchestrator handles it **with no code change**:

```
drift-lang (toolchain + core)
   └─ drift-stdlib (containers, concurrency, …)   ← new package_repo
        └─ drift-mariadb-client / drift-net-tls / drift-web
             └─ drift-workflows
```

Transitive-closure staging slots it right after the toolchain; reverse-invalidation reruns
downstream when it changes; preflight/snapshot pick it up because it's a vanilla
`package_repo`. Multiple stdlib modules can be separate **artifacts in one manifest** (the
`drift-web` repo already ships 4 artifacts this way), each with its own author-claim, all
staged together.

The only orchestrator refactor we'd otherwise need — keying preflight/staging on the
`stage_packages` *capability* rather than the `kind` label, so one repo could be both
toolchain and package publisher — is the **fallback** for keeping it inside the `drift-lang`
repo with dual recipes. Splitting the release unit avoids it entirely. (FWIW today the
preflight's toolchain skip would *mislabel* a toolchain-that-also-publishes as "no
author-claims" — so if you ever did go the in-repo dual-recipe route, give us a heads-up and
we'll do that refactor first.)

## Open questions that are genuinely yours

These are the hard parts, and they're toolchain calls, not wiring:

1. **Where's the cut line?** What's the minimal `core` the compiler truly needs to
   self-host vs. what can move to user-land? That boundary is the whole design.
2. **One stdlib package or several?** `drift-stdlib` with N artifacts, or `drift-containers`
   / `drift-concurrency` / … as separate units? (Orchestrator is fine with either.)
3. **Version ↔ ABI contract.** How does the published user-land stdlib declare its
   compatibility range against the toolchain ABI? (Lock v2 author-trust ranges.)
4. **Bootstrap authority.** If `core` and a future user-land package ever overlap, which is
   authoritative for what — and is `core` allowed to diverge from the published stdlib?

If you decide it's worth pursuing, we're happy to dry-run a `drift-stdlib` `package_repo`
entry against the current `orchestration.json` and the gate contracts before anything lands.
No rush, and no expectation — just didn't want the observation to evaporate.
