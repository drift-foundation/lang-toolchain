# PROGRESS — stdlib bootstrap/core split

Living log. Newest entries on top. Status only: **RESEARCH/PLAN** (no code changes).

---

## Status table

| Research item                                   | State        |
|-------------------------------------------------|--------------|
| 1. Audit compiler/self-host deps                | NOT STARTED  |
| 2. Define "core" technically                    | NOT STARTED  |
| 3. Define "user-land stdlib"                    | NOT STARTED  |
| 4. Feasibility: cycles & duplicate authority    | NOT STARTED  |
| 5. Import / package-root behavior changes       | NOT STARTED  |
| 6. Packaging shape                              | NOT STARTED  |
| 7. ABI / version compatibility model            | NOT STARTED  |
| 8. Risks                                        | OPEN (seeded)|

---

## Log

### 2026-06-26 — thread opened
- Captured the orchestrator's design note into `README.md`. No code touched.
- Seeded confirmed facts + open questions below from a first reconnaissance of the tree
  (directory listing + reserved-namespace grep only — NOT a dependency audit yet).
- Next safe step recorded at bottom.

---

## Links
- Orchestrator proposal: `/tmp/drift-announce/20260626T173735Z-build-orchestrator-stdlib-split-note.md`
- 0.33.61 app-cert release note: `/tmp/drift-announce/20260626T161311Z-drift-lang-release-notes.md`
- Trust governance code: `lang/driftc/packages/trust_v1.py`, `lang/drift/trust.py`,
  `lang/driftc/packages/core_trust_v1.json`
- Cert/SCI machinery (for re-cert cost analysis): `lang/driftc/packages/source_content_id.py`,
  `lang/driftc/packages/author_claim_v1.py`, `lang/driftc/packages/cert_claim_v1.py`

---

## Confirmed facts (reconnaissance-level; each to be re-verified during the audit)
- **stdlib tree:** `stdlib/std/` holds ~29 modules: `algo, cli, codec, concurrent, console,
  containers, core, crypto, env, err, float, format, fs, io, iter, json, log, mem, meta, net,
  parse, random, regex, runtime, source, sync, text, time, uuid`. `stdlib/lang/` holds
  `atomic.drift, thread.drift`.
- **`stdlib/std/core/` contents:** `arc, box, copy, const_arc, shareable, cmp, hash, num, core`
  — the ownership/value-semantics substrate. This cluster is the leading `core` candidate.
- **Reserved namespaces:** `std.*` / `lang.*` / `drift.*` are reserved; project trust stores
  are FORBIDDEN from granting them (`lang/drift/trust.py::_namespace_is_reserved`). The sole
  authority is the toolchain-bundled `core_trust_v1.json`
  (`trust_v1.py::load_core_trust_store`). ⇒ a certified user-land stdlib under `std.*` still
  anchors trust in the bundled core_trust, not a consumer project store.
- **Current distribution model:** stdlib rides inside the toolchain (deployed to
  `toolchain_root`, resolved via `DRIFT_TOOLCHAIN_ROOT`); pool packages resolve via
  `DRIFT_PACKAGE_ROOT`. (per orchestrator note — to be confirmed in build/resolver code.)
- **Compiler is Python:** `driftc` is implemented in Python (stage0/1/2), not self-hosted in
  Drift. So "self-host dependency" ≠ "Drift modules the compiler imports"; it's the runtime
  archive + the per-binary link surface. (Re-verify the exact link set in the audit.)
- **Runtime archive coupling:** concurrency/atomics/threads (`std/concurrent`, `std/sync`,
  `lang/atomic`, `lang/thread`) sit on the C runtime archive (reactor/executor/scheduler/
  swapcontext/carriers). That substrate stays TCB regardless of any Drift-source split.
- **ABI today:** `DRIFT_RT_ABI = 18` (`lang/versions.py`).

## Open questions
- **Q-CUT:** Exact membership rule for `core`. Candidate predicate: "linked by every compiled
  binary OR referenced by lowering/codegen intrinsics OR required by the ownership-drop
  runtime." Needs to be made testable against the actual link/codegen surface.
- **Q-CONTAINERS-FIRST:** Is `containers` the clean first move candidate (Array/Map over core's
  RawBuffer/Box, no extra C substrate)? Leaning yes — pilot here before concurrency.
- **Q-CONC-SUBSTRATE:** How much of concurrency can actually certify given the C runtime
  substrate stays in core? Likely: Drift-source API certifies, hard intrinsic dep on core.
- **Q-TRUST-ANCHOR:** Does certified user-land stdlib's author/cert kid go into
  `core_trust_v1.json` (Foundation key, reserved-namespace authority)? If so, the trust root
  stays toolchain-bundled — confirm that's the intended/acceptable model.
- **Q-SHADOW:** Resolve-time guard so published user-land can never shadow a core module name
  (core authoritative). Where does module resolution decide toolchain-root vs package-root, and
  can a reserved-prefix collision check live there?
- **Q-SCI-CHURN:** Moving modules out of the toolchain changes their packaging/SCI inputs —
  what re-cert is forced, and for whom (only the stdlib, or transitively its consumers)?
- **Q-PACKAGING:** One `drift-stdlib` (N artifacts) vs separate `drift-containers` /
  `drift-concurrency`. Leaning one-package-N-artifacts to start (atomic release, one ABI range).
- **Q-ABI-BUMP:** On a `DRIFT_RT_ABI` bump, does the certified stdlib force re-cert, or does a
  Lock v2 range absorb it? Interacts with the ABI-policy memo (bump vs fix-and-keep).

## Decisions / non-decisions
- **NON-DECISION:** The cut line is NOT decided. Items 1–3 (audit) must precede it.
- **NON-DECISION:** Packaging shape not chosen (leaning one-package-N-artifacts, unconfirmed).
- **DECISION (process):** Split the *release unit* (a separate `drift-stdlib` repo/manifest)
  rather than dual-role `drift-lang` — avoids the orchestrator's toolchain-skip mislabel and
  needs zero orchestrator code change. (Endorsed both sides; revisit only if a strong reason
  to keep it in-repo emerges.)
- **DECISION (scope):** No implementation this thread. Research → design doc → (if greenlit)
  separate slice.

## Next safe step
Item 1 — **dependency audit, read-only.** Trace what the runtime archive + a bare compiled
binary link unconditionally, and which `stdlib/std/*` modules are import-only, to turn the
`std/core/*` directory hunch into a testable `core` membership list. Pure investigation; no
edits. Record findings here and flip item 1 to IN PROGRESS when started.
