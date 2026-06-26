# stdlib bootstrap/core split — research & design thread

**Status:** RESEARCH + PLAN ONLY. No compiler, stdlib, or orchestrator code changes.
**Opened:** 2026-06-26
**Origin:** build-orchestrator design note —
`/tmp/drift-announce/20260626T173735Z-build-orchestrator-stdlib-split-note.md`
(observation + proposal from the orchestrator/consumer vantage; explicitly "not a request").

## The feature in one sentence

Split today's implicitly-trusted, in-toolchain stdlib into two disjoint surfaces:

1. **bootstrap / `core`** — the minimal substrate the compiler + runtime + every compiled
   binary must link to exist at all. Stays baked into the toolchain, part of the TCB / root
   of trust. No independent cert leg — it *is* the certifier's own substrate. This is today's
   model, just made explicit and minimal.
2. **user-land stdlib** — the higher-level surface (containers, concurrency, codecs, net,
   crypto, json, …) promoted to **certified package artifact(s)**: built by the staged
   compiler, author-claim + cert-claim signed with the Foundation key, run through
   test/stress/perf gates, snapshot-pinned, consumed downstream via `DRIFT_PACKAGE_ROOT`
   like any other pool package.

### Why it's attractive (from the orchestrator's note)
- **Smaller TCB.** Only `core` stays implicitly trusted; the high-surface, high-complexity
  stdlib comes under the same explicit gate + evidence + signing regime as everything else.
- **ABI coupling stops being special.** user-land-stdlib → toolchain becomes an honest
  `depends_on` edge with a Lock v2 ABI-pinned range, expressed like `drift-net-tls` /
  `drift-web` already do.
- **Security story, not just packaging hygiene.** The stdlib's heaviest surface gets
  provenance + author/cert legs + reproducible gates instead of "trusted because it shipped
  in the toolchain tarball."

### Honest framing / caveats already identified (see PROGRESS "confirmed facts")
- The trust **anchor** for `std.*` does NOT move: `std.*`/`lang.*`/`drift.*` are reserved
  namespaces governed by the toolchain-bundled `core_trust_v1.json`, not by consumer project
  trust stores. So the win is "the heavy surface gets gated/signed/reproducible builds," NOT
  "consumers choose the stdlib's trust root."
- The TCB that *remains* (`core`: arc/box/drop/string_arc) is the highest-consequence,
  most-bug-prone code in the tree. "Smaller TCB" is true by surface area; the scariest code
  stays implicitly trusted by necessity.
- Concurrency only *half*-leaves the TCB: its C runtime substrate (reactor/executor/scheduler/
  swapcontext/carriers) stays baked in regardless; only the Drift-source API can be certified.

## Required research plan

Each item is tracked in `PROGRESS.md` with findings. Order is roughly dependency-first.

1. **Audit compiler/self-host dependencies.** What stdlib modules does `driftc` / the runtime
   archive / the build actually require? Distinguish (a) modules the *compiler implementation*
   needs, (b) modules every *compiled binary* links unconditionally, (c) import-only modules.
   (NB: `driftc` is implemented in Python — stage0/1/2 — so "self-host" here means the runtime
   + per-binary link surface, not a Drift-implemented compiler.)
2. **Define "core" technically.** Intrinsic substrate, runtime boundary, compiler-required
   modules, bootstrapping minimum. Produce a concrete module list and the rule that defines
   membership (not a vibe — a testable predicate: "linked by every binary" / "referenced by
   lowering/codegen intrinsics" / "required by the ownership-drop runtime").
3. **Define "user-land stdlib".** Containers, concurrency, higher-level modules — anything not
   needed to build/link the compiler or a bare binary. Produce the complementary module list.
4. **Feasibility: cycles & duplicate authority.** Confirm the two sets are disjoint and acyclic
   (core never imports user-land; user-land imports core only downward). Identify any current
   edges that violate this and what it would take to cut them. Confirm no module would end up
   authoritative in both halves.
5. **Import / package-root behavior changes.** What changes (if any) in module resolution,
   `DRIFT_TOOLCHAIN_ROOT` vs `DRIFT_PACKAGE_ROOT`, reserved-namespace handling, and the
   resolve-time collision guard needed so published user-land can never shadow a core module.
6. **Packaging shape.** One `drift-stdlib` package with multiple artifacts (cf. the drift-web
   4-artifact repo) vs separate packages (`drift-containers`, `drift-concurrency`, …). Trade
   atomic release unit + single ABI range vs independent versioning cadence.
7. **ABI / version compatibility model.** How the certified user-land stdlib declares its
   compatibility range against the toolchain/runtime ABI (`DRIFT_RT_ABI`, 18 today) — Lock v2
   author-trust ranges. What happens on an ABI bump: forced re-cert? range widening?
8. **Risks.** Bootstrap chicken-and-egg; shadowing/overlap with core; source-identity (SCI)
   churn from the move; certification / re-cert cost; downstream migration for consumers that
   currently get the stdlib implicitly from the toolchain root.

## Explicit non-goals (for now)
- No compiler changes. No stdlib reorganization. No orchestrator / `orchestration.json` edits.
- No decision on the cut line yet — the audit (items 1–3) must precede it.
- This thread produces a **design doc + feasibility findings**, then (only if greenlit) a
  separate implementation slice with its own plan.

## Pointers
- Orchestrator note: `/tmp/drift-announce/20260626T173735Z-build-orchestrator-stdlib-split-note.md`
- 0.33.61 app-cert release note (context for why this came up):
  `/tmp/drift-announce/20260626T161311Z-drift-lang-release-notes.md`
- Reserved-namespace governance: `lang/driftc/packages/trust_v1.py::load_core_trust_store`,
  `lang/drift/trust.py::_namespace_is_reserved`, `lang/driftc/packages/core_trust_v1.json`
- stdlib tree: `stdlib/std/`, `stdlib/std/core/`, `stdlib/lang/`
