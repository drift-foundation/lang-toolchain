# Compile timing: how to enable it and what to send back

This release adds **per-phase wall-clock timings** to every Drift compile.
Goal: gather real data from real workloads (your normal builds and tests)
so the toolchain team can decide where to optimise first. **No
optimisations have landed yet** — this release is data-gathering only.

If you run Drift builds, please flip the `--timing` flag on for a few of
your normal commands and paste the output back to the toolchain team
(see [What to send back](#what-to-send-back) below).

## TL;DR

```bash
# Your normal build, with timings on stderr.
drift build --timing

# Your normal deploy, per-artifact timings on stderr.
drift deploy --timing

# Direct compiler invocation (rare for app teams).
driftc --timing <source>

# Machine-readable output (one JSON object on stdout, timings inside).
# JSON mode is on driftc only today; the drift wrappers print stderr
# text summaries.  File an issue if you need wrapper JSON mode.
driftc --json --timing <source>
```

## What you get

### Text mode (the default — read these in your terminal / CI logs)

The **outermost command owns the timing session**: `driftc` reports
compiler phases, `drift build` reports a build session, `drift
deploy` reports a deploy session per artifact. Wrapper sessions
include nested compiler phases (merged from `driftc` subprocess) and
wrapper-level steps (cert emit, smoke, publish, etc.).

#### `driftc --timing` (compiler session)

```
[drift:timing] total_wall=4.213s
[drift:timing]   parse                    = 1.852s
[drift:timing]   trust_pre_pass           = 0.107s
[drift:timing]   trust_verify_loop        = 0.094s
[drift:timing]   codegen                  = 1.512s
[drift:timing]   link                     = 0.624s
```

#### `drift build --timing` (build session)

Wrapper owns `total_wall`. Compiler phases nested under `compile.*`:

```
[drift:timing][web-client] total_wall=4.213s
[drift:timing][web-client]   compile.total_wall       = 4.120s
[drift:timing][web-client]   compile.parse            = 1.852s
[drift:timing][web-client]   compile.codegen          = 1.512s
[drift:timing][web-client]   compile.link             = 0.624s
[drift:timing][web-client]   compile.trust_pre_pass   = 0.107s
[drift:timing][web-client]   compile.trust_verify_loop = 0.094s
```

#### `drift deploy --timing` (deploy session, per artifact)

Wrapper owns `total_wall` for the whole artifact pipeline (build +
sidecars + smoke + publish). Compiler phases for the build land
under `build.compile.*`; smoke-side compiler phases land under
`smoke.compile.*`; wrapper-level Python steps are flat.

```
[drift:timing][web-client] total_wall=8.213s
[drift:timing][web-client]   build.compile.total_wall  = 4.120s
[drift:timing][web-client]   build.compile.parse       = 1.852s
[drift:timing][web-client]   build.compile.codegen     = 1.512s
[drift:timing][web-client]   build.compile.link        = 0.624s
[drift:timing][web-client]   cert_emit                 = 0.140s
[drift:timing][web-client]   attach_author_claim       = 0.022s
[drift:timing][web-client]   smoke.compile.total_wall  = 2.010s
[drift:timing][web-client]   smoke.compile.parse       = 0.812s
[drift:timing][web-client]   smoke.compile.codegen     = 0.704s
[drift:timing][web-client]   smoke.run                 = 0.080s
[drift:timing][web-client]   smoke.custom              = 0.000s
[drift:timing][web-client]   publish                   = 0.210s
```

### Reading the numbers

- **`total_wall` is the only authoritative wall-time.** For wrapper
  sessions, it's measured by the wrapper around the whole pipeline
  — not by summing the child phase lines (subprocess phases can be
  nested, incomplete, or miss wrapper work).
- **Per-phase numbers are explanatory.** They may sum *above*
  `total_wall` because some phases overlap (e.g. nested checker
  work inside parse). Use the phase breakdown to spot dominators;
  use `total_wall` to compare runs.
- **`*.total_wall` rows** report the subprocess's own measured wall
  time. Useful for "how much of my deploy is the compile vs the
  wrapper steps."
- **Only the outermost command prints.** When `drift deploy`
  invokes `driftc` with `--timing`, the driftc subprocess emits its
  structured timings via a side-channel file (`--timing-out`)
  instead of printing — the wrapper consolidates and prints one
  block per artifact.

### JSON mode (for tooling)

`--json --timing` appends a `timings` field to the standard payload:

```json
{
  "exit_code": 0,
  "diagnostics": [],
  "timings": {
    "total_wall": 4.213,
    "phases": {
      "parse": 1.852,
      "trust_pre_pass": 0.107,
      "trust_verify_loop": 0.094,
      "codegen": 1.512,
      "link": 0.624
    }
  }
}
```

Same invariants as before: stdout is **exactly one JSON object** per
compile. Human-readable timing notes don't leak to stdout in JSON mode
— they stay on stderr only.

## Instrumented phases

The phase labels you'll see today. Coverage is intentionally coarse;
finer breakdowns will follow once the first round of data shows where
to zoom in.

| Phase | What it measures |
|---|---|
| `package_discovery` | `os.walk` of every `--package-root`, finding `.zdmp` / `.dmp` candidates. |
| `trust_pre_pass` | First scan over discovered package candidates: format-only load, manifest peek, SCI-conflict / envelope-variance resolution. |
| `trust_verify_loop` | Per-candidate trust-gate run: cert-claim / author-claim verification, closure walk. |
| `parse` | `parse_drift_workspace_to_hir` — tokenizer + LALR parse + initial HIR construction across the workspace. |
| `normalize_hir`, `typecheck`, `checker`, `borrow_check`, `mir_validate`, `drop_flags`, `ledger_rebuild_post_drop_flags`, `string_arc`, `ssa`, `throw_checks` | Pre-existing inner phases inside `compile_stubbed_funcs`. Each one may nest, so their sum can exceed the outer `check`/`mir` block they sit inside — that's expected. |
| `codegen` | `lower_module_to_llvm` — SSA / MIR → LLVM IR text. |
| `link` | The `clang` subprocess that produces the final binary. |
| `emit_package` | The `.dmp` / `.zdmp` write at the end of `--emit-package` mode. |

Phases that aren't reached on a given compile (e.g. `link` for
`--test-build-only`) simply don't appear in the `phases` dict.

## Cost when disabled

`--timing` is **opt-in**. When not set, the in-process event sink is
not installed; every `events.timed(...)` call is one
`ContextVar.get()` returning `None` plus a bare `yield` — no
allocations, no clock reads, no I/O. The default compile path is
unchanged.

## What to send back

The toolchain-perf team needs your **representative** workloads, not
synthetic ones. The most useful data points:

1. **One `drift build --timing`** of your normal app or library build
   (whichever one you build most often).
2. **One `drift deploy --timing`** of a typical deploy you'd run
   weekly.
3. **Your test suite** with timings: if your tests invoke `drift
   build` / `drift deploy` as part of `just test` or similar, add
   `--timing` to those wrapper invocations.

For each one, paste the **whole `[drift:timing]` stderr block** into
your message to the toolchain team. Don't trim phases — even the
sub-second ones tell us which compile shape you're running. Include:

- The command you ran.
- A one-line description of what's being built (e.g. "bookkeeper, ~40
  modules, dep on mariadb-rpc + 3 PushCoin libs").
- Whether this was a cold run (first build of the day) or a warm one
  (repeated `just test`).
- Roughly how many times per day you run this.

Example message:

> `drift build --timing` on bookkeeper@0.4.1 — 40 modules, deps on
> mariadb-rpc 0.5.0 + 3 PushCoin libs. Cold run (first of the day).
> I do this ~12 times/day during active work.
>
> ```
> [drift:timing][bookkeeper] total_wall=8.213s
> [drift:timing][bookkeeper]   compile.total_wall       = 8.110s
> [drift:timing][bookkeeper]   compile.parse            = 3.852s
> [drift:timing][bookkeeper]   compile.trust_verify_loop = 1.521s
> [drift:timing][bookkeeper]   compile.codegen          = 1.812s
> [drift:timing][bookkeeper]   compile.link             = 0.624s
> [drift:timing][bookkeeper]   compile.trust_pre_pass   = 0.207s
> ```

That's enough for the team to start prioritising.

## FAQ

**Does `--timing` change compile output?** No. Same binary bytes, same
diagnostics, same exit codes. The sink only observes; it doesn't
influence.

**Will the phases stay stable?** Current top-level phase labels
(`parse`, `codegen`, `link`, `trust_pre_pass`, `trust_verify_loop`,
`emit_package`, the inner `compile_stubbed_funcs` labels) and wrapper
labels (`compile.*`, `build.compile.*`, `smoke.compile.*`,
`smoke.run`, `smoke.custom`, `smoke.app`, `cert_emit`,
`attach_author_claim`, `publish`) are the ones we plan to keep. New
labels may appear (finer breakdowns inside existing phases). Removed
labels would be a deliberate breaking change and would land with a
release note. JSON consumers should treat `phases` as an open dict —
present keys are stable, but new ones may show up.

**Can I always-on it?** Yes — wrap your normal commands. Cost is
negligible (single-digit microseconds per phase boundary). When no
sink is installed, `events.timed` is a single `ContextVar.get()`
returning `None` plus a bare yield — no allocations, no clock reads.

**`--json-lines` / NDJSON?** Not yet. The event sink supports
progressive emission internally, so we can add `--json-lines` later
if a use case (long-running pipelines, IDE progress) actually needs
it. File an issue if you do.

**Why do wrapper sessions show `compile.total_wall` AND individual
`compile.parse` etc. rows?** `compile.total_wall` is the child
driftc's own measured wall time (so you can see "how much of the
wrapper session was the compile vs other steps"). The
`compile.parse`, `compile.codegen`, etc. rows are the child's phase
breakdown, prefixed so they don't collide with wrapper-level
labels. Both surface — the totals row is for at-a-glance
attribution; the breakdown is for diving in.

**Wrapper JSON mode (`drift build --json --timing`)?** Not yet. The
drift wrappers print stderr text summaries today. `driftc --json
--timing` is the JSON path for now. File an issue if you need
wrapper JSON output.

**Will the timings be different inside CI vs my laptop?** Yes,
obviously — please report what you see; "my CI takes 4 minutes" is
informative even without normalisation.
