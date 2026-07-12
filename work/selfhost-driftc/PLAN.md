# Self-hosting driftc: phased Drift port of the compiler

Status: PLAN (decision recorded, no code). Decision 2026-07-12: **the rewrite target
language is Drift** — self-bootstrapping is the goal; C/Rust rejected (C is hostile to
compiler-shaped code; Drift buys strategic credibility and dogfooding no other target
can). The migration is **phased, never big-bang**: a mixed Python/Drift compiler is
the normal operating state for the entire multi-year effort, with clean process-level
separation and corpus-signed authority swaps per stage.

## 0. Goals / non-goals

Goals:
- Replace driftc's stages with native binaries written in MINI-DRIFT (a
  generously-chosen, versioned DIALECT of Drift that does not track the language's
  evolution — §4b), one stage at a time, each independently shippable, pausable,
  and reversible.
- Keep the release cadence: language evolution never freezes; a change lands twice
  only for a stage currently mid-shadow (keep shadow windows short).
- End state: the whole compiler is mini-Drift, built by the permanent two-link
  bootstrap (`cc -> CPython -> mini-driftc.py -> sources`), validated by corpus
  diff against the Python implementation it replaced.

Non-goals:
- Porting the Python driver/orchestration early (it may live indefinitely — Phase 5
  is optional).
- Any semantic change to the language as part of a port phase (ports are
  byte-identical-output refactors by definition).
- Writing compiler sources in FULL Drift — §4b pins them to mini-Drift permanently.
- Keeping the full 126k-line Python compiler forever: it is deleted stage by stage
  as ported; what remains permanently is mini-driftc.py (~10-20k lines).

## 1. Architecture of the mixed compiler

**Python driver = orchestrator and bootstrap root (stage0).** Each ported stage is a
native binary written in Drift, compiled by the CURRENT certified toolchain, invoked
by the driver as a subprocess with serialized artifacts at the seams. No FFI between
Python and Drift — process boundary + interchange files only. That is the clean
separation: every stage has a CLI contract (input artifact, output artifact, exit
codes, diagnostics stream), and either implementation can stand on either side of it.

```
source ──▶ [parse] ──▶ HIR dump ──▶ [check] ──▶ checked-HIR dump ──▶ [lower] ──▶ DMIR ──▶ [codegen] ──▶ LLVM .ll ──▶ clang
             P|D                      P|D                              P|D                  P|D
                        (P = Python in-process, D = Drift subprocess; per-stage authority flag)
```

**Authority swaps use the B-arch discipline** (the same audit-then-swap pattern that
landed the destructible consultation and release elision):
1. SHADOW: Python stage stays authoritative; the Drift stage runs alongside on the
   same input; outputs diffed byte-for-byte; divergence is fail-closed loud (build
   error in CI lanes, warning+telemetry in dev).
2. FLIP: after zero diffs across the full corpus + full suite + at least one real
   downstream build (DriftQuery), authority flips; Python path stays behind
   `DRIFT_STAGE0_<stage>=1` for one release.
3. RETIRE: flag removed; Python code for that stage is deleted or frozen as
   stage0-bootstrap-only.

## 2. What exists vs what must be built (the "make it real" list)

Seam assets that EXIST today:
- **DMIR serialization** at the MIR seam (already the package interchange format).
- **Deterministic textual LLVM IR** at the codegen output — byte-diffable.
- **The corpus methodology** (543 compiles, identical universe, arithmetically exact
  deltas) — the validation harness for every phase. Promoting the runner/aggregator
  into `tools/` is already item 8 of the string cleanup review; it is a hard
  prerequisite here.
- Packages/signing/trust, `-g` debug info, the shared parallel test runner.

Must be BUILT (enablers, mostly Phase 0/1 work):
1. **Stage CLI contracts.** One page per seam: artifact format + version stamp, exit
   codes, diagnostic format (JSON lines matching the driver's diag model), and a
   `--dump`/`--consume` mode on the PYTHON side of every seam first — Python must be
   able to export/import its own seam artifacts before any Drift code exists,
   because that is also how shadow-diffing works.
2. **Canonical AST/HIR serialization.** DMIR covers the MIR seam; the parse and
   check seams have no serialized form today. Define a versioned, canonical (sorted,
   offset-stable) dump — golden-file friendly. This is the largest new
   infrastructure item.
3. **Determinism hardening.** Byte-identical reruns of every stage output (stable
   iteration orders, no timestamps/pids in artifacts). Audit + pin, like the
   fresh-hint scan.
4. **DMIR reader/writer in Drift.** A Drift library for the DMIR format (and the new
   AST/HIR dumps). This is also the natural first real Drift library of the project
   and a stdlib dogfood (byte buffers, maps, serialization).
5. **Drift-side test story.** Stage binaries need in-language unit tests plus the
   golden/corpus harness. Decide early: a tiny Drift test runner (assert + exit
   codes, driven by pytest as today) is enough; do not build a framework.
6. **mini-driftc.py itself.** The permanent bootstrap compiler (§4b) — derived
   from the existing Python driftc by SUBSETTING, not from scratch: reuse the lark
   grammar (restricted to the subset), skip the checker entirely (non-checking by
   design), and write a thin direct lowering to textual IR. Built early (Phase
   0/1) because it is also the subset's definition-by-implementation and the CI
   gate that every ported stage stays inside mini-Drift: from the first ported
   stage onward, CI builds all mini-Drift sources with BOTH the certified full
   driftc (authoritative validation) and mini-driftc.py (subset conformance + the
   growing two-link bootstrap).
7. **Build integration.** justfile recipes: stage binaries are built by the current
   CERTIFIED toolchain (`just build-selfhost`), rebuilt on toolchain cert, and the
   driver discovers them via `DRIFT_SELFHOST_BIN_DIR`. Stage binaries version-stamp
   their artifacts; the driver refuses mismatches (same ABI-stamp discipline as
   runtime archives).

## 3. Drift-language readiness (gaps the port itself must close)

Have and sufficient: variants + match, generics, Result/or_throw error model, RAII +
Destructible + Arc/Box, String/Array/HashMap, file IO, argv/env, packages, FFI,
`-g` debug info.

Gaps to close (each is a normal language/stdlib slice, filed via the usual process):
- **Deep recursion:** compilers recurse on trees; VT stacks default 256 KiB
  (configurable). Either size stage-binary stacks explicitly or prefer explicit
  worklists in hot walkers. Decide per stage; pin with a deep-nesting fixture.
- **String building at scale:** codegen emits megabytes of text. Need a rope/builder
  (or `Array<Byte>` + final join) with measured throughput — B-repr/B5 interacts
  here; measure, don't assume.
- **Binary IO / byte buffers:** DMIR reader/writer needs clean byte-level IO
  (RawBuffer exists; a small `std.bytes` reader/writer API may be warranted).
- **Hash-map iteration order:** canonical dumps need deterministic iteration
  (sort-on-emit is acceptable; document the rule).
- **Profiling story for Drift binaries:** perf against stage binaries (symbols exist
  via `-g`; validate the workflow once in Phase 1).
- Anything else discovered = the dogfood dividend: every gap the port hits is a
  real-user bug report against the language, with the compiler team as reporter.

## 4. Phases

Ordering principle: (stability of the stage) × (cleanliness of validation). Gates
reference `work/string-ownership-refactor/FOLLOW-UP-CLEANUP-REVIEW.md`.

**Phase 0 — enablers (~weeks; valuable even if the port stalls).**
Profile the corpus (parse/check/lower/codegen wall-clock split — unknown today);
promote corpus tooling into `tools/`; determinism audit + pins; write the seam CLI
contracts; add Python-side `--dump-hir`/`--dump-checked-hir` (canonical form).
Exit: contracts reviewed; corpus tool in repo; profile published (it also decides
whether Phase 1 or Phase 2 goes first on perf grounds).

**Phase 1 — mini-driftc.py + parser in mini-Drift (~3–5 pm). The go/no-go experiment.**
Scope: (a) mini-driftc.py by subsetting the existing Python compiler (§2 item 6);
(b) the production parser stage written in mini-Drift: source → canonical HIR dump,
matching Python's dump byte-for-byte. Grammar is effectively frozen; zero ownership
semantics; self-contained. Also delivers the first mini-Drift serialization library,
exercises strings/trees/maps hard, and stands up the subset-conformance CI gate.
Exit: shadow-clean over corpus + full suite; authority flip; a written verdict on
Drift-as-compiler-language (perf numbers, ergonomics gaps filed). If the verdict is
negative, STOP — the sunk cost is one parser and the enablers, all still useful.

**Phase 2 — MIR → LLVM codegen in Drift (~4–8 pm). GATE: B-repr/B5 landed.**
Cleanest validation in the project (byte-diff of .ll text over the corpus) and the
main perf prize (native single-threaded is expected to be a large multiple of the
Python baseline — parallel codegen is explicitly a non-goal; decision 2026-07-12).
Porting it before B-repr means porting the String layout twice; the gate is firm.

**Phase 3 — MIR passes in Drift (~8–12 pm). GATE: string-authority cleanup done**
(string_arc deleted/collapsed, C3 decided, drift absorbed into B-repr planning).
The subtlest code in the tree; its prerequisite list is literally the cleanup
review. Port order within the phase: ledger/verdicts first (pure analysis,
corpus-verifiable via the audit reporter), rewrite passes last.

**Phase 4 — checker/type system in Drift (~12–18 pm). GATE: spec churn quiets**
(coercion/require-bounds threads resolved). Largest chunk (type_checker 14k +
checker 12.6k + call_resolver 7k). Validation: checked-HIR dumps + the full
diagnostics corpus (diagnostic text is part of the contract).

**Phase 5 — driver/packaging/signing (optional, ~3–6 pm).**
Python orchestration may live indefinitely. Port only for single-binary deployment;
signing/trust port needs its own security review.

**Full self-host milestone (after 1–4):** every stage is mini-Drift;
mini-driftc.py builds the entire compiler in the two-link bootstrap; the result is
corpus-diffed against the certified toolchain's output. The full Python compiler
is gone; mini-driftc.py remains, per §4b.

### 4b. Bootstrap policy (REVISED 2026-07-12): compiler sources are mini-Drift, permanently

**Decision revision:** the compiler is written in **mini-Drift** — a DIALECT of
Drift — and STAYS in it. Full Drift is never used to build the next Drift, and
(refinement 2026-07-12) **full Drift does not need to understand mini-Drift**:
the dialect starts as a subset of today's Drift and simply does not track the
language's evolution — divergence by standing still, not by design. This insulates
the compiler sources from Drift's semantic churn permanently (no mass-edits when
the language tightens), at the price of the validation story below. Precedent is
mainstream (LLVM is written in a banned-exceptions C++ subset).

**Validation story (the obligation divergence creates):** once the dialect
diverges, full driftc no longer statically checks the compiler's own sources.
Resolution, both parts adopted:
1. **The dialect's ownership model is REGIONS, not values** (refined 2026-07-12).
   Value-granular ownership — even a primitive unique-owner form — is rejected: it
   puts drop insertion / move-out tracking / conditional cleanup (the subtlest
   machinery in the current compiler; the 0.27.145 / AIL-leak / phantom-destroy
   bug class) inside the UNCHECKED trusted root, for no batch-compiler benefit.
   Instead: phase-scoped arenas — allocations belong to a region (parse arena,
   per-fn lowering arena, ...), freed wholesale at region end. A genuine primitive
   ownership discipline (ownership by region lifetime), trivially sound without
   per-value checking, nearly free in mini-driftc's codegen (bump-allocate +
   free-all), and it bounds high-water memory beyond plain leak-at-exit. No
   Destructible, no enforced move discipline, immutable strings.
   **Ownership as style:** `move` stays in the dialect's grammar, accepted and
   ignored by mini — sources remain written in ownership-respecting Drift style,
   so the migration era validates real ownership under real driftc while the
   common subset holds, and any future re-convergence has its style debt pre-paid.
2. **Behavioral validation replaces static checking:** mini-built compiler
   binaries run the corpus under the repo's standing ASAN/Valgrind lanes — the
   existing memcheck discipline, applied to the compiler itself, the same way
   every C compiler survives without a borrow checker.
Divergence is a lazily-opened one-way door: during the migration phases, sources
sit inside the common subset (validated by the certified driftc of that era at
cut time); the dialect freezes against Drift's drift when the first breaking
language change arrives, not by proclamation.

The bootstrap, for every release, forever:

    cc -> CPython -> mini-driftc.py -> driftc sources (mini-Drift) -> driftc

Two links from source. No release-chain replay, no frozen rungs, no re-cutting, no
generated seeds. Superseded by this policy: chain bootstrapping (release N builds
N+1 still works operationally, but is never LOAD-BEARING for reconstruction) and
the IR-seed root.

Costs, stated honestly:
- **mini-driftc.py is a small LIVING artifact, not a frozen one** (~10-20k lines,
  non-checking: parse -> type-lite -> lower -> textual IR; compiler sources are
  validated by the full driftc in CI, so mini never checks). It changes ONLY when
  the subset revs. This is the residual answer to "do we maintain two ports": yes —
  one is 10-20k lines of naive Python, touched on rare, team-controlled subset
  revisions.
- **Subset revisions are versioned and deliberately rare** (target: << yearly).
  Each revision = update mini-driftc.py + revalidate the two-link bootstrap in CI.
- **The dialect discipline on compiler sources is permanent**: no
  exceptions/throw-catch (Result-only — also removes the can-throw ABI from mini),
  generics via a whitelisted builtin-container set with naive monomorphization
  (Array/HashMap/Optional/Result/Box), no VT concurrency (the compiler
  is single-threaded by design — parallel codegen is a NON-GOAL: native
  single-threaded speed over the Python baseline is the entire perf budget, and
  batch parallelism, if ever wanted, is the build system's job, not the
  compiler's), remaining cuts decided empirically from what the ported compiler
  actually needs. Because the
  subset is a permanent home, it is chosen GENEROUSLY: ergonomics the compiler
  lives in daily belong in; only expensive-to-implement AND rarely-needed features
  stay out.
- **Dogfooding narrows to the sequential core** (structs, variants+match,
  generics-lite, strings/arrays/maps, Result) — which is also the core most users
  exercise; concurrency dogfooding remains with the stdlib and downstream teams.

One authoritative implementation per stage still holds during the migration
(shadow -> flip -> retire, per §1); the Python FULL compiler is deleted stage by
stage as ported, and what remains permanently is only mini-driftc.py.

### 4c. Preserving the bootstrap root

With §4b, the reconstruction story is one root: **repo sources + a generic C/C++
toolchain**. `cc` builds CPython (portable C, the most-built codebase in
existence); CPython runs mini-driftc.py (pure Python; vendored pure-Python lark;
verified 2026-07-12: the compile path uses no native modules — llvmlite is
test-only, cryptography is signing-only); mini-driftc builds the compiler sources;
the backend is textual LLVM IR + any near-version clang (LLVM buildable from
source). No binary artifact is ever load-bearing.

**The Bootstrap Root Invariant:** every release must be buildable by
`cc -> CPython -> mini-driftc.py -> sources`, protected by:
- the two-link bootstrap replayed in CI per cert (and on every subset revision);
- an import-closure audit pin: mini-driftc.py's imports stay CPython-stdlib +
  vendored-pure-Python only (a native wheel can never become load-bearing);
- vendored lark source in-repo; llvmlite demoted to test-only requirements;
- a deep-nesting fixture pinning that mini-driftc handles compiler-scale inputs.

**The capsule (OCI image) remains, demoted to convenience:** a hash-pinned recipe
(no live-network fetches) + the built image exported (`docker save`) as a signed,
checksummed cert-bundle artifact, registry-independent. It makes bootstrap take
minutes instead of hours and freezes a known-good clang; it is never required.
Two CI cadences still apply: replay the two-link bootstrap per cert (root-rot);
rebuild the capsule from its recipe annually (recipe-rot). Known limits: capsule
frozen at x86_64-linux (QEMU for future hosts; add arm64 while the recipe is
alive); a binary image is the convenience path, sources the auditable path.

Historical note: earlier drafts carried three reconstruction roots (frozen FULL
Python driftc; a generated `compiler.ll` seed; a multi-rung ladder with re-cut
recovery). The §4b policy collapses them — the permanent two-link mini-Drift
bootstrap IS the ladder, reduced to its minimal form, and makes the other roots
redundant.

## 5. Risks and their controls

- **Moving target:** controlled by phase gates tied to the cleanup review, and by
  the rule that a change lands twice only during a stage's (short) shadow window.
- **Drift perf insufficient for a compiler:** Phase 1 measures it on real work
  before anything expensive is committed; B-repr is expected to matter and is
  gated ahead of the string-heavy phases.
- **Whitebox-test debt:** black-box driver/e2e tests port free via the CLI seams;
  Python whitebox tests (stage2 unit tests, mutation audits) are re-conceived
  per-phase as Drift unit tests + corpus properties — budgeted inside each phase
  estimate, and the reason the estimates are not smaller.
- **Two implementations diverging silently:** impossible by construction during
  shadow (fail-closed diff); after flip, the Python side is frozen or deleted.
- **Compiler-team velocity while dogfooding:** every Drift gap found becomes a
  language bug report through the normal process (this week's DriftQuery cycle
  shows the loop works at speed).

## 6. Immediate next steps (when this thread activates)

1. Phase 0 profile run + publish the stage split.
2. Corpus tooling → `tools/` (shared with the string cleanup thread).
3. Seam CLI contract doc + Python `--dump-hir` canonical dump.
4. Then the Phase 1 go/no-go: build the Drift parser.

Nothing here starts before the blocking-FFI facility certifies and the string
cleanup priorities (string_arc collapse, C3 decision) have their slices scheduled —
this plan deliberately consumes those outputs as gates rather than competing with
them.
