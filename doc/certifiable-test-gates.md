# Test gates for certifiable packages: compile/run phase split

Status: **proposed recommended practice — validated by a reference
implementation.** Every normative claim here has been exercised end-to-end across
all three gate shapes below (unit/e2e with instrumentation dedup, an idle-host
performance gate, and an exclusive-resource stress gate). Handed to the toolchain
team as a candidate standard for any Foundation package. Project-agnostic — it
describes the methodology and the toolchain primitives it rests on (`flocker`,
`driftc --sanitize`), not any one package's layout.

For the compiler repo's own certification-only gate — the full
ownership-audit corpus with its reviewed baseline and the approval-file
promotion process — see [ownership-corpus-gate.md](ownership-corpus-gate.md).

## Validation notes

This methodology was validated against a reference conversion on the staged
`0.33.12+abi15` toolchain:

- **Unit/e2e gate:** parallel compile, instrumentation dedup, and explicit
  sanitizer builds via `driftc --sanitize=address`.
- **Performance gate:** parallel compile of all measured binaries, followed by
  serial measurement on an idle-host `flocker -j 1` key.
- **Stress gate:** parallel compile, then parallel independent scenarios with
  exclusive-resource scenarios serialized by `flocker -j 1 --key <resource>`.

Two caveats from that validation do not change the methodology:

- One staged unit/e2e lane was blocked by an unrelated staged `net-tls 0.5.2`
  rebuild issue: its embedded `std.log:LogEnvelope` schema collided with the
  staged toolchain's `std.log` import. Certified `net-tls` is clean.
- Performance mechanics were validated, but the measured numbers were taken on a
  loaded host and should not be treated as a clean baseline.

## Principle

A package's test gate (its `test`, `perf`, `stress`, … targets) is built from
**one primitive — a job**: a command plus a declaration of *how it wants to run*.
Jobs are organized into **two ordered phases**:

1. **Compile** — produce every binary the gate needs, at **maximum parallelism**,
   bounded only by a host-global slot pool. No runs here.
2. **Run** — execute the binaries. **Parallel by default**; **serial only where an
   isolation constraint demands it.**

Two rules make this correct and fast:

- **Compile is isolation-agnostic.** Building a binary never needs an idle host or
  exclusive access to anything, so compilation should *always* saturate the
  machine. Only *runs* can carry isolation constraints. A gate that measures
  performance still compiles in parallel — only its measurement *runs* go serial.
- **Don't rebuild identical binaries.** Dedup across instrumentation lanes (below).

These two rules are the whole point: compilation typically dominates a gate's
wall-clock, so a gate that interleaves serial compiles with runs, or that
recompiles the same binary per lane, leaves most of the machine idle.

## The toolchain primitive: a host-global slot pool (`flocker`)

Everything parallel routes through one host-local, key-scoped slot pool
(`flocker`). This is what lets the methodology **compose** without a central
scheduler:

```
flocker --key K -j N -- COMMAND…
```

- **Parallel pool:** `-j N` on a shared key bounds *total* concurrent jobs under
  that key across every caller on the host — a private run and a certification
  orchestrator contend on the same pool, so the machine is never oversubscribed,
  with no coordination between them.
- **Serial / exclusive resource:** `-j 1 --key <resource>` is a named mutex. A
  job that needs exclusive access to a host (performance measurement) or to a
  shared external resource (a database, a server, a fixed port) takes `-j 1` on a
  key naming that resource. Jobs on *different* keys still run concurrently.
- **Watchdog liveness:** an opt-in `--heartbeat SECS` makes the wrapper emit a
  periodic status line to stdout while a long, silent job runs — enough to satisfy
  a stdout-inactivity watchdog (see below). Off by default; zero overhead absent.

A gate therefore expresses *both* its parallelism and its serialization through
the same primitive: `-j N` for the parallel pool, `-j 1 --key R` for an exclusive
resource. Run-mode is a property each **job** declares, never an assumption baked
into a phase.

## Binary dedup across instrumentation lanes

Most gates run the same logic under several instrumentations. Build only as many
distinct binaries as there are distinct *compile-time* variants:

- **Run-time instrumentation** (a dynamic memory/leak checker, a profiler — the
  tool wraps the process at run time): the lane **reuses the base binary**. The
  binary is byte-identical to the uninstrumented build, so there is **no extra
  compile job** — only an extra *run* job that wraps the base binary in the tool.
- **Compile-time instrumentation** (a sanitizer — the compiler instruments the
  output): a **distinct binary**, built once and run directly. It MUST be selected
  by an **explicit compiler flag**, not an ambient environment variable (see
  Reproducibility).

So three lanes (e.g. plain / run-time-checker / sanitizer) need only **two**
compile jobs: the base (shared by plain and the run-time checker) and the
sanitizer variant. Eliminating the redundant third compile removes ~⅓ of the
gate's compile work when compile dominates.

## Reproducibility: variation in flags, never ambient env

Every build job MUST be fully determined by its argv. Build-affecting state in an
ambient environment variable is a certification hazard — identical argv producing
different binaries depending on the caller's environment undermines cert evidence
and build caching. Select instrumentation, target, and options with explicit
flags (e.g. `driftc --sanitize=address`, which also selects the matching
instrumented runtime). Reserve an explicit per-job env overlay only for a tool
that offers no flag — and treat that as a defect to push upstream, not a pattern.

This rule governs **build-affecting** variation — anything that changes compile
inputs. A per-job `env` overlay that selects a **run-time fixture namespace**
(see *Fixture isolation for stateful backends*) does not influence the build and
is a separate, sanctioned use, not the defect this rule warns against.

## Recipe isolation

Each top-level gate target is **idempotent and isolated**: it gets its own work
directory and does **not** reuse another gate's compiled artifacts. This is what
lets a certification orchestrator **machine-split** gates across hosts, and what
makes a single gate safe to re-run. Never share a binary cache *between* gates.

## Fixture isolation for stateful backends

Serialization (`-j 1 --key <resource>`) orders *access* to a shared resource; it
does not isolate *state*. When a gate runs the same logic under several lanes
(base / sanitizer / run-time checker) against a **stateful** backend — especially
an append-only or immutable-record store with no delete path — serializing the
lanes is not enough: lane 2 inherits whatever state lane 1 left behind, and a
re-run inherits the previous run's. A claim→complete→reclaim test that passes in
isolation then fails on lanes 2–3 ("already done") within a single gate run.

Isolate the *fixture*; do not drop the lane. The sanctioned idiom is an
**emitter-minted per-job `env` namespace**:

- The **emitter** (the policy layer) mints a namespace and injects it as a
  per-run-job `env` overlay — e.g. `<GATE>_FIXTURE=<gate>-<nonce>-<lane>`. The
  test reads it and scopes all its keys/records under that namespace, so every
  lane runs on virgin space.
- The nonce is **per-invocation** (minted at emit time), so a *re-run* also gets
  fresh space — essential when the backend has no delete API. The `<lane>`
  component keeps base / sanitizer / checker from colliding within one run.
- **Keep every lane.** Do not retreat to "only base hits the backend;
  sanitizer/checker run on backend-free units" — that discards memory-safety
  coverage of the exact backend round-trip (connection, buffer, and
  record-object lifetimes), which is the highest-value checker target a
  backend-backed gate has.
- No teardown is needed when the namespace is unique per run. (A *mutable*
  backend may instead prefer a teardown step in the gate harness; a fresh
  namespace is simpler and re-run-safe.)

This is **orthogonal** to the serial-resource key: the key prevents resource
contention on the backend; the namespace prevents state contamination across
lanes. A shared, stateful backend usually needs **both**.

The rule is general, not backend-specific: any append-only / immutable-record /
no-cleanup backend — event stores, ledgers, lease- or idempotency-coordinators —
hits it the moment more than one lane runs against it, and serializing the
resource silently does not fix it.

## Satisfying a stdout-inactivity watchdog

Certification orchestrators commonly kill a gate whose stdout goes silent for too
long. The silent stretches are exactly the long jobs the slot pool already wraps
(a compile, a checker run). Satisfy the contract with the pool's own
`--heartbeat`, not a hand-rolled loop:

- Run **one** heartbeat monitor for the gate, on a **dedicated key** (so it never
  consumes a work slot), with its stdout going to the gate's stdout. It emits a
  liveness line at an interval comfortably under the watchdog window and is torn
  down when the gate exits (the wrapper forwards termination signals).
- Do **not** enable `--heartbeat` on a job whose stdout you capture as data — the
  heartbeat line would pollute the capture. Per-gate monitor, not per-job.
- **Capturing wrappers:** if the gate captures an inner step's stdout (e.g. a
  measurement script whose output is parsed), the monitor MUST run at the
  *capturing* level on **live** stdout — a heartbeat *inside* the captured step is
  swallowed into the capture and never reaches the watchdog. Start the monitor
  before the captured step, tear it down after.

## Gate shapes (generic)

- **Unit / e2e gate.** Compile phase: base + sanitizer variants, all parallel.
  Run phase: base (parallel), run-time checker on the base binary (parallel),
  sanitizer binary (parallel). No serial jobs.
- **Performance gate.** Compile phase: build *every* measured binary up front in
  parallel — including comparison baselines from other toolchains, which are just
  build jobs with their own command. Run phase: **serial on an idle host**
  (`-j 1` on a measurement key, nothing else running). The parallel compile does
  not violate measurement isolation — only the measurement *runs* must be serial.
- **Stress gate.** Compile phase: all scenario binaries in parallel. Run phase:
  scenarios that share an exclusive resource run serial under one `-j 1 --key
  <resource>`; independent scenarios run in parallel.

## What the executor can't do — the harness brackets it

A parallel job executor runs a fixed set of jobs and reports their exit codes.
Two things sit outside that contract, and a gate that needs either must keep that
part in its own harness, *wrapped around* the executor — not pushed into the plan:

- **Threading one job's output into another job's input.** A performance gate
  often measures a Drift binary *relative to* a baseline (another toolchain's
  build, a prior commit). The baseline's result (rps, ns/op) becomes an input —
  an env var, a threshold — to the Drift measurement. The executor won't pipe one
  job's stdout into another's environment; it only launches jobs. So the harness
  computes/threads the derived value; the executor still owns the parallel
  *compile* of every binary (baselines included — they're just build jobs).
- **Owning an external resource's lifecycle.** A stress gate needs a server / DB
  / queue *started, waited-ready, and torn down* around the runs. Start-ready-stop
  is orchestration with ordering and health-checks the executor doesn't model.
  The harness owns that lifecycle; the executor runs the scenario binaries inside
  it (parallel where independent, `mode: serial` on a shared `--key` where they
  contend for the resource).

The split is the same in both cases: **compilation is the executor's** (always
parallelizable, always worth fanning out); the **measurement/lifecycle wrapper is
the harness's**. Bracket, don't inline — putting a server boot or a cross-job env
hand-off "into the plan" is the anti-pattern.

## Authoring shape

A gate is three separable steps so a private run can chain them and an
orchestrator can interpose at any boundary:

1. **Declare** — one place per gate lists its compile jobs (sources, entry,
   explicit flags, output name). Side-effect-free; emit to a caller-given path,
   never stdout (stdout carries tool noise).
2. **Compile** — launch every build job under the shared `flocker` pool; dedup
   identical outputs.
3. **Run** — launch run jobs; parallel ones share the pool, serial/exclusive ones
   take `-j 1 --key R`; one `--heartbeat` monitor feeds the watchdog.

Hand-written recipes (just/make/scripts) implement this today. A generic engine
that consumes a declared job list is a possible future toolchain tool (its own
RFC); the methodology does not depend on it.

## Conformance checklist for a certifiable package

- [ ] Compile and run are distinct phases; no compiling inside a run/measurement path.
- [ ] All build jobs run in parallel under one host-global `flocker` pool.
- [ ] Identical binaries are built once; run-time-only instrumentation reuses the base binary.
- [ ] Build variation is in explicit flags, not ambient env.
- [ ] Runs are parallel unless an isolation constraint applies; serialization is expressed as `-j 1 --key <resource>`, not as a serial *phase*.
- [ ] A stateful shared backend gets **disjoint fixtures** across lanes and re-runs via an emitter-minted per-job `env` namespace (per-invocation nonce + per-lane) — not a serial phase, and not by dropping instrumentation lanes.
- [ ] Each top-level gate is isolated (own work dir) and machine-split-safe.
- [ ] The stdout-inactivity watchdog is fed by a single `flocker --heartbeat` monitor, not a bespoke loop.
