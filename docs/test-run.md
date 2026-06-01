# `drift_test_run` — shared parallel job executor

`drift_test_run.py` is the scenario-agnostic job executor that package teams
compose to run test / perf / stress gates, instead of each maintaining a
~500–700 line shell fork of the same parallel-compile / phased-run plumbing.

It implements **mechanism, never scenario policy**: it executes a project-
supplied *plan* of jobs and knows nothing about databases, servers, queues,
lanes, sanitizers, or what any gate means. Those live entirely in the plan
content and in the team's own harness that brackets execution.

## Where it lives

It is a **CI tool, not an installed user-facing binary** — it is not on `PATH`
and (unlike the PEX `driftc`/`drift` and the bash `flocker` in `bin/`) it
requires a host `python3`.

| Context | Path |
|---|---|
| Source checkout | `tools/drift_test_run.py` (budget helper: `tools/pytest_jobs.py`) |
| Deployed toolchain | `lib/tools/drift_test_run.py` (budget helper: `lib/tools/drift_pytest_jobs.py`) |

Run it with the host interpreter, e.g. `python3 <root>/lib/tools/drift_test_run.py …`.
From either location it locates `bin/{flocker,driftc,drift}` by walking up to the
toolchain/distribution root, so no path flags are needed in the common case.

See also: `docs/flocker.md` (the concurrency primitive), `docs/certifiable-test-gates.md`
(the methodology this implements), and the budget helper above (the host
concurrency-budget contract: `DRIFT_TEST_JOBS`, else `ceil(nproc/2)`).

## Model

- A **plan** is an ordered list of **phases**; a phase is a list of **jobs**.
- Phases run in order, with a **barrier** between them: a phase fully completes
  before the next starts. Cross-phase ordering (e.g. a run job that needs a
  build job) is expressed by putting them in different phases.
- Within a phase:
  - **parallel** jobs run concurrently, bounded by the flocker pool;
  - **serial** jobs run one-at-a-time per named group (in `order`); distinct
    groups run concurrently with each other and with the parallel pool.
- A phase whose only jobs are one serial group ⇒ nothing else runs during it
  (the idle-box perf case falls out — no special flag).

## Concurrency budget (load-bearing)

The parallel pool size `N` is sourced from the **budget-helper protocol**
(`DRIFT_TEST_JOBS`, else `ceil(nproc/2)`) — never hardcoded in a plan. Every job
is wrapped in `flocker --key <pool> -j N`, so several concurrent runs or lanes on
one host stay bounded by the *single* host-global flocker semaphore rather than
multiplying past RAM. Override with `--jobs N` only as a deliberate operator
choice.

## Usage

```
drift_test_run.py --plan PATH --work-dir DIR [options]

  --jobs N           Parallel pool size (default: budget-helper protocol).
  --driftc PATH      Path for the {driftc} placeholder (default: bin/driftc | PATH).
  --drift PATH       Path for the {drift} placeholder.
  --flocker PATH     Path to flocker (default: bin/flocker | PATH).
  --pool-key KEY     flocker key for the parallel pool (default: drift-jobs).
  --heartbeat SECS   Emit an executor heartbeat line every SECS (default: off).
  --report PATH      Write a JSON per-job result report.
  --keep-going       Run all phases even after a phase fails (default: stop).
  --dry-run          Print the resolved flocker argv per job; execute nothing.
```

Exit code is `0` iff every job succeeded, else `1`. Plan/usage errors exit `2`.
Per-job stdout+stderr is captured to `<work-dir>/logs/<id>.log`; the tail of each
failed job's log is printed in the summary.

## Plan format (JSON)

```json
{
  "name": "test",
  "phases": [
    {
      "name": "build",
      "jobs": [
        {
          "id": "sign_verify#plain",
          "cmd": ["{driftc}", "--target-word-bits", "64",
                  "--entry", "pkg.tests.sign_verify::main",
                  "src/lib.drift", "tests/sign_verify.drift",
                  "-o", "{work}/bins/sign_verify#plain"],
          "out": "{work}/bins/sign_verify#plain"
        },
        {
          "id": "sign_verify#asan",
          "cmd": ["{driftc}", "--sanitize", "address",
                  "--entry", "pkg.tests.sign_verify::main",
                  "src/lib.drift", "tests/sign_verify.drift",
                  "-o", "{work}/bins/sign_verify#asan"],
          "out": "{work}/bins/sign_verify#asan"
        }
      ]
    },
    {
      "name": "check",
      "jobs": [
        {"id": "run#plain",    "cmd": ["{work}/bins/sign_verify#plain"], "needs": ["sign_verify#plain"]},
        {"id": "run#memcheck", "cmd": ["{work}/bins/sign_verify#plain"], "needs": ["sign_verify#plain"], "wrap": "memcheck"},
        {"id": "run#asan",     "cmd": ["{work}/bins/sign_verify#asan"],  "needs": ["sign_verify#asan"]},
        {"id": "bench",        "cmd": ["{work}/bins/sign_verify#plain", "--bench"],
         "mode": "serial", "group": "measure", "order": 0}
      ]
    }
  ]
}
```

### Placeholders (substituted in every `cmd` token, and in `out`)
- `{work}` — the `--work-dir`.
- `{driftc}` / `{drift}` — resolved tool paths. Ad-hoc / test-file compiles use
  `{driftc}` directly (it compiles exactly what's on argv); manifest-driven
  artifact builds use `{drift} build`. The two are co-equal — pick per workflow.
- `{jobs}` — the resolved pool size `N`.

### Job fields
- `id` (required) — unique within the plan; the target of `needs`.
- `cmd` (required) — full argv. **All build variation lives here as explicit
  flags** (`--sanitize address`, not `DRIFT_ASAN=1`); `env` is the rare escape
  hatch. `--sanitize` on `driftc` also selects the matching runtime archive, so a
  sanitizer build is a plain `{driftc}` job — no driver hop needed.
- `mode` — `parallel` (default) or `serial`.
- `group` (serial only; `key` accepted as an alias) — jobs sharing a group run
  one-at-a-time on that named resource. Default: the job's own id.
- `order` (serial only) — integer sequence within the group.
- `needs` — job ids that must finish first. The executor uses phase barriers as
  the dependency mechanism, so a `needs` target must live in an **earlier phase**
  (a same-phase `needs` is a plan error with guidance).
- `env` — explicit per-job env overlay (string→string). Applied to that
  invocation only; overrides the sanitizer-option defaults below.
- `wrap` — `none` (default), `memcheck`, or `massif`. Expands to the
  **executor-owned canonical valgrind** invocation (`--error-exitcode=97`,
  `--leak-check=full`, `--fair-sched=yes`), so leak/sanitizer policy lives in one
  place instead of in each team's fork.
- `out` — output path for **dedup**: two jobs with the same resolved `out` are
  compiled once (the first runs, later ones are skipped).

### Naming `id` and `out` — namespace by artifact, not bare filename
The runner dedups on the resolved `out` path *by design*, so making `out` (and
`id`) unique is the **plan author's** responsibility. The common footgun:
the same test leaf-name living in two roots — e.g. `middleware_test` or
`error_tags_test` under both `web-jwt` and `web-rest`. If you key `id`/`out` on
the bare filename, the two collide, and a colliding `out` **mis-dedups**: one is
compiled and the other is silently skipped (you run the same binary twice and
think you covered both). Namespace by the owning artifact-leaf:

```
web-jwt.middleware_test#plain      web-rest.middleware_test#plain      ✓ distinct
middleware_test#plain              middleware_test#plain               ✗ collide → mis-dedup
```

Use **dashes, not dots, inside the namespacing if you target old toolchains**:
driftc < 0.33.16 derived its scratch IR path from `-o` by replacing the last
dot-segment, so `a.b.x#plain` and `a.b.x#asan` collapsed to a shared `a.b.ll`
and concurrent compiles clobbered each other (corrupt-IR). Fixed in 0.33.16
(scratch paths now append, not replace), so dots in `out` are safe on current
toolchains — but dashes remain a portable habit.

### Sanitizer env defaults
Run jobs get `ASAN_OPTIONS=detect_leaks=0:halt_on_error=1` and
`UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1` unless the job's `env` (or the
ambient environment) already sets them. Harmless for non-sanitized binaries.

## What a team keeps vs. what it drops

- **Drops:** the flocker-wrap / slot-wait / source-resolution / valgrind-line /
  heartbeat-loop plumbing.
- **Keeps:** a small **plan emitter** (which files/deps/lanes — the plan content)
  and any **harness that brackets `execute`** to set up and tear down its own
  resources (a DB, an HTTPS server, a queue). The executor never knows about
  those — it just runs the jobs the harness sandwiches.

The plan format is the stable contract; forks can migrate one at a time.
