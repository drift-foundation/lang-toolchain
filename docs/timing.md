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

**Invocation counts.** Every phase line now carries `count=N` (in
text mode) and a sibling `timings.counts` dict (in JSON mode) — the
number of times `events.phase_start(<label>)` fired during the
compile. Lets you distinguish one slow call from many small ones
without re-instrumenting:

  - `smoke.compile = 40s  count=2` → likely a retry/double-invoke.
  - `normalize_hir = 2s   count=500` → per-function overhead.
  - `trust_verify_loop = 3s  count=1` → one large pass.
  - `package_discovery = 0.8s  count=12` → repeated discovery,
    not a single slow walk.

For subprocess-wrapper paths (`drift build` / `drift deploy`),
child driftc counts merge additively into the wrapper sink under
the prefix used for the timings (e.g. child `codegen.lower`
count=1 becomes parent `compile.codegen.lower` count += 1).

### Text mode (the default — read these in your terminal / CI logs)

The **outermost command owns the timing session**: `driftc` reports
compiler phases, `drift build` reports a build session, `drift
deploy` reports a deploy session per artifact. Wrapper sessions
include nested compiler phases (merged from `driftc` subprocess) and
wrapper-level steps (cert emit, smoke, publish, etc.).

#### `driftc --timing` (compiler session)

```
[drift:timing] total_wall=4.213s
[drift:timing]   parse                    =   1.852s   43.9%  count=1
[drift:timing]   trust_pre_pass           =   0.107s    2.5%  count=1
[drift:timing]   trust_verify_loop        =   0.094s    2.2%  count=1
[drift:timing]   codegen                  =   1.512s   35.9%  count=1
[drift:timing]   link                     =   0.624s   14.8%  count=1
```

The percent column is each label's elapsed as a percent of
`total_wall`. **Percentages can sum above 100%** because nested
labels overlap — read each as "percent of total wall represented by
this label," not a partition. (A 3.2s phase reads very differently
in a 6s compile vs a 60s one; the percent makes that obvious.)

#### `drift build --timing` (build session)

Wrapper owns `total_wall`. Compiler phases nested under `compile.*`:

```
[drift:timing][web-client] total_wall=4.213s
[drift:timing][web-client]   compile.total_wall           =   4.120s   97.8%  count=1
[drift:timing][web-client]   compile.parse                =   1.852s   43.9%  count=1
[drift:timing][web-client]   compile.codegen              =   1.512s   35.9%  count=1
[drift:timing][web-client]   compile.link                 =   0.624s   14.8%  count=1
[drift:timing][web-client]   compile.trust_pre_pass       =   0.107s    2.5%  count=1
[drift:timing][web-client]   compile.trust_verify_loop    =   0.094s    2.2%  count=1
```

#### `drift deploy --timing` (deploy session, per artifact)

Wrapper owns `total_wall` for the whole artifact pipeline (build +
sidecars + smoke + publish). Compiler phases for the build land
under `build.compile.*`; smoke-side compiler phases land under
`smoke.compile.*`; wrapper-level Python steps are flat.

```
[drift:timing][web-client] total_wall=8.213s
[drift:timing][web-client]   build.compile.total_wall    =   4.120s   50.2%  count=1
[drift:timing][web-client]   build.compile.parse         =   1.852s   22.5%  count=1
[drift:timing][web-client]   build.compile.codegen       =   1.512s   18.4%  count=1
[drift:timing][web-client]   build.compile.link          =   0.624s    7.6%  count=1
[drift:timing][web-client]   cert_emit                   =   0.140s    1.7%  count=1
[drift:timing][web-client]   attach_author_claim         =   0.022s    0.3%  count=1
[drift:timing][web-client]   smoke.compile.total_wall    =   2.010s   24.5%  count=1
[drift:timing][web-client]   smoke.compile.parse         =   0.812s    9.9%  count=1
[drift:timing][web-client]   smoke.compile.codegen       =   0.704s    8.6%  count=1
[drift:timing][web-client]   smoke.run                   =   0.080s    1.0%  count=1
[drift:timing][web-client]   smoke.custom                =   0.000s    0.0%  count=1
[drift:timing][web-client]   publish                     =   0.210s    2.6%  count=1
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
    },
    "counts": {
      "parse": 1,
      "trust_pre_pass": 1,
      "trust_verify_loop": 1,
      "codegen": 1,
      "link": 1
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
| `pkg_resolve` | Post-trust: version selection, transitive sanity, dedup, ABI checks, external module / signature / schema extraction over the loaded package set. |
| `link_pkg_types` | Heavy O(packages × modules × types) graph walk: deserialise every loaded package's type table + build the consumer-side TypeId remap. Often the largest bucket on multi-package consumer builds. |
| `parse` | `parse_drift_workspace_to_hir` — tokenizer + LALR parse + initial HIR construction across the workspace. |
| `flatten_post_parse` | `flatten_modules` + `_inject_prelude` + per-function origin-path collection. Single-pass over the parsed module tree. |
| `pkg_sig_import` | Per-loaded-package signature import: TypeExpr resolution, impl_type_param canonicalization, template HIR decode + normalize, fingerprint checks. Scales (packages × modules × sigs); often the second-largest bucket on dep-rich consumer builds after `link_pkg_types`. |
| `normalize_hirs_cli` | The per-function `normalize_hir` dict comprehension over all source functions (CLI-side, distinct from the inner `normalize_hir` label that fires inside `compile_stubbed_funcs`). |
| `type_checker_init` | `TypeChecker` construction + the two `_register_signatures_in_callable_registry` passes that intern every source + external signature. Scales with total signature count. |
| `typecheck_funcs` | The per-function `type_checker.check_function()` loop over normalized HIRs (CLI-side, distinct from the inner `typecheck` label inside `compile_stubbed_funcs`). CPU-bound; dominant on type-heavy code. |
| `post_check_analysis` | Post-typecheck infrastructure: `analyze_non_retaining_params`, stdlib escape annotations, lambda escape validation, `Checker.run_by_id`, `_install_destructor_fns`, struct-requires enforcement, variant re-finalization, intrinsic-call validation. Type-table heavy; scales with trait/impl count. **Does NOT include borrow checking** (see `borrow_check_cli`). |
| `borrow_check_cli` | The per-function CLI-side `BorrowChecker.from_typed_fn` + `check_block` pass. Distinct from the inner `borrow_check` label that fires inside `compile_stubbed_funcs` (MIR-side check on a different shape). |
| `pre_csf_setup` | (Consumer-build path only) combined-exports merge, destructor install, visibility provenance build, and `Pass1State` construction just before `compile_stubbed_funcs`. |
| `csf_entry_setup` | CSF-internal setup before `normalize_hir`: signature-map normalization, module-info processing, the upfront type-table dance every CSF call performs. Stable bucket — present on every CSF invocation. |
| `generic_instantiation` | Template HIR collection, method-wrapper synthesis, and three `_drain_instantiations()` rounds — the work between `typecheck` and `checker` inside CSF. Often a large bucket on generic-heavy code. |
| `hir_to_mir` | The per-function HIR → MIR lowering loop inside CSF. Scales with source function count; one of the largest CSF buckets on real builds. |
| `hidden_lambda_lowering` | Hidden-lambda body lowering (closures synthesized during HIR→MIR). Sized to lambda density in the source. |
| `cleanup_authoring` | Per-function cleanup-emission pass + post-author ledger rebuild. Runs after `ledger_rebuild_post_drop_flags`, before `string_arc`. On `docs/perf-analysis-bookkeeper-profile.md`'s workload this was ~17s cumulative — biggest single source of previously-unattributed CSF time. |
| `normalize_hir`, `typecheck`, `checker`, `borrow_check`, `mir_validate`, `drop_flags`, `ledger_rebuild_post_drop_flags`, `string_arc`, `ssa`, `throw_checks` | Pre-existing inner phases inside `compile_stubbed_funcs`. Each one may nest, so their sum can exceed the outer `check`/`mir` block they sit inside — that's expected. |
| `codegen` | Outer codegen scope (wraps `codegen.lower` + `codegen.render` + small wrapper-emit work). |
| `codegen.lower` | `lower_module_to_llvm` — MIR / SSA → in-memory LLVM module. |
| `codegen.render` | `module.render()` — in-memory LLVM module → IR text. Distinct from `codegen.lower` so stdlib-heavy builds can see how much is text assembly vs lowering. |
| `write_ir` | Writing generated LLVM IR to disk — either the explicit `--emit-ir` output, or the normal link path's temporary `.ll` file that clang consumes. The two paths are mutually exclusive per compile; operators get one bucket either way. |
| `runtime_archive_build` | `build_runtime_archive` — synchronous build of `libdrift_rt*.a`. Cold-cache cost can dominate first build of the day; warm runs are sub-100ms. |
| `link` | The `clang` subprocess that produces the final binary. |
| `emit_package` | The `.dmp` / `.zdmp` write at the end of `--emit-package` mode. |

Phases that aren't reached on a given compile (e.g. `link` for
`--test-build-only`) simply don't appear in the `phases` dict.

## Cost when disabled

`--timing` is **opt-in**. When not set, the in-process event sink is
not installed; every `events.timed(...)` call is one
`ContextVar.get()` returning `None` and then returning a
module-level singleton no-op context manager — no allocations, no
clock reads, no I/O. The default compile path is unchanged.

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
> [drift:timing][bookkeeper]   compile.total_wall          =   8.110s   98.7%  count=1
> [drift:timing][bookkeeper]   compile.parse               =   3.852s   46.9%  count=1
> [drift:timing][bookkeeper]   compile.trust_verify_loop   =   1.521s   18.5%  count=1
> [drift:timing][bookkeeper]   compile.codegen             =   1.812s   22.0%  count=1
> [drift:timing][bookkeeper]   compile.link                =   0.624s    7.6%  count=1
> [drift:timing][bookkeeper]   compile.trust_pre_pass      =   0.207s    2.5%  count=1
> ```

That's enough for the team to start prioritising.

## FAQ

**Does `--timing` change compile output?** No. Same binary bytes, same
diagnostics, same exit codes. The sink only observes; it doesn't
influence.

**Will the phases stay stable?** Current top-level phase labels
(`package_discovery`, `parse`, `codegen`, `codegen.lower`,
`codegen.render`, `link`, `trust_pre_pass`, `trust_verify_loop`,
`pkg_resolve`, `link_pkg_types`, `write_ir`, `runtime_archive_build`,
`emit_package`, `flatten_post_parse`, `pkg_sig_import`,
`normalize_hirs_cli`, `type_checker_init`, `typecheck_funcs`,
`post_check_analysis`, `borrow_check_cli`, `pre_csf_setup`,
`csf_entry_setup`, `generic_instantiation`, `hir_to_mir`,
`hidden_lambda_lowering`, `cleanup_authoring`, the inner
`compile_stubbed_funcs`
labels) and wrapper labels (`compile.*`, `build.compile.*`,
`smoke.compile.*`, `smoke.run`, `smoke.custom`, `smoke.app`,
`cert_emit`, `attach_author_claim`, `publish`) are the ones we
plan to keep. New
labels may appear (finer breakdowns inside existing phases). Removed
labels would be a deliberate breaking change and would land with a
release note. JSON consumers should treat `phases` as an open dict —
present keys are stable, but new ones may show up.

**Can I always-on it?** Yes — wrap your normal commands. Cost is
negligible (single-digit microseconds per phase boundary). When no
sink is installed, `events.timed` is a single `ContextVar.get()`
returning `None` and then returning a module-level singleton no-op
context manager — no allocations, no clock reads.

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

---

## Workload vector

Elapsed time alone doesn't tell you whether one compile is "harder"
than another. A small file that triggers heavy generic specialisation
can take longer than a large file that doesn't, and elapsed time
across machines (or across releases that re-tune internal phases)
isn't directly comparable. The **workload vector** is a parallel set
of machine-neutral counters that lets you reason about *what changed*
in a compile, not just *how long it took*.

`--timing` populates a `workload` map alongside `phases`/`counts` in
both the JSON payload and the text summary. A `workload_schema`
integer ships with it so consumers can detect a semantic break before
parsing keys.

### Why a vector, not a single complexity score

A single weighted score would mix parser, generic-expansion,
dependency, and codegen costs using assumptions that change between
compiler releases. The vector keeps the dimensions separate so you
can read which one moved:

- Different `source.input.parse_tree_tokens`, similar
  `mir.processed.instructions` → source/parsing complexity moved,
  but the actual MIR pipeline saw the same shape of work.
- Similar tokens, very different `generics.instances_emitted` or
  `mir.processed.instructions` → specialisation expansion moved.
- Similar MIR, very different `llvm.ir.utf8_bytes` → lowering /
  rendering expansion moved.
- Similar workload on different machines → elapsed-time delta is
  primarily host/environment.

### Snapshot vs processed-work counters

Two write modes coexist, mirroring how each metric is generated:

- **Snapshot** (`events.set_workload(key, value)`): describes the
  compilation unit or final artifact once. Last writer wins.
  Examples: `source.input.utf8_bytes`, `hir.functions`,
  `llvm.ir.utf8_bytes`.
- **Processed work** (`events.add_workload(key, delta)`): counts
  work completed by a timed pass, and **accumulates if the pass
  runs more than once in a session**. The matching phase time also
  accumulates — pairing them keeps per-unit elapsed comparable
  across retries. Examples: `generics.instances_emitted`,
  `mir.processed.instructions`, `codegen.input_mir.instructions`.

Snapshot keys describe the *thing* being compiled. Processed-work
keys describe the *work attempted on it* — and both must scale
together when the work runs twice, otherwise the per-unit
denominator is wrong.

### v1 key inventory (workload_schema = 1)

Source-side (snapshot). The `source.input.*` / `source.implicit_stdlib.*`
split is the load-bearing classification: a small file against a
large stdlib would otherwise look identical to a large file with
no stdlib backdrop.

- `source.input.files` — explicit compile-input files read
- `source.input.utf8_bytes` — UTF-8 bytes of explicit inputs
- `source.input.parse_tree_tokens` — parser-visible tokens from
  successfully parsed explicit inputs
- `source.implicit_stdlib.files` — files added by `--stdlib-root`
  expansion
- `source.implicit_stdlib.utf8_bytes` — UTF-8 bytes of those files
- `source.implicit_stdlib.parse_tree_tokens` — tokens from those
  files

HIR / packages (snapshot). Combined user+stdlib for v1; the
source-side split above already exposes the per-origin
breakdown of bytes/tokens.

- `hir.modules`, `hir.functions`, `hir.signatures` — post-flatten
  compilation-unit shape
- `packages.pre_resolve.artifacts`, `packages.pre_resolve.modules`
  — loaded packages and their dependency-module counts as accepted
  by trust verification, captured BEFORE `pkg_resolve` runs version
  selection / deduplication. These measure trust-verification input,
  not the post-dedup compilation input — a future
  `packages.post_resolve.*` slice can be added under additive new
  keys without bumping the schema if duplicate-input scenarios
  become relevant.

Processed work (accumulating):

- `generics.instances_emitted` — count of generic FUNCTION
  instantiations that the `generic_instantiation` phase actually
  synthesized into concrete bodies (entries with status `"emitted"`
  in the phase's `inst_cache`). Pending / failed / never-drained
  template requests are excluded. This is NOT the post-phase
  population of the type table's instance dicts: that population
  also grows from non-generic struct/variant/interface use
  elsewhere and would overstate what the phase emitted.
- `mir.processed.functions`, `mir.processed.blocks`,
  `mir.processed.instructions` — MIR shape produced by each
  successful `compile_stubbed_funcs` invocation. Denominator for
  the MIR pipeline phases (`hir_to_mir`, `cleanup_authoring`,
  `mir_validate`, etc.).
- `codegen.input_mir.functions`, `codegen.input_mir.blocks`,
  `codegen.input_mir.instructions` — what is actually being passed
  to LLVM lowering. Separate from `mir.processed.*` because
  reachability filtering / wrapper synthesis between MIR and
  codegen can change the unit. Denominator for `codegen.lower`.

Final artifact (snapshot):

- `llvm.ir.utf8_bytes` — UTF-8 byte length of the rendered IR text.

### Token definition

`source.*.parse_tree_tokens` counts every `lark.Token` leaf in the
parse tree returned by the Drift parser, after Drift post-lex
processing and Lark filtering. It is parser-visible work, not raw
lexical tokens — counts are compiler/grammar-version-scoped (the
counter is meaningful for comparing two compiles with the same
toolchain, not for cross-release comparison). A file that fails to
parse contributes its bytes/files but not its tokens, since the
parser raised before producing a tree.

### Cross-process aggregation

`drift build` and `drift deploy` collect the child driftc's workload
the same way they collect phase timings: via the `--timing-out` JSON
side-channel, merged into the wrapper sink under a prefix.

- `drift build` merges under `compile.*` (e.g.
  `compile.mir.processed.instructions`).
- `drift deploy` merges build and smoke compiles separately under
  `build.compile.*` and `smoke.compile.*`.

Merge is additive under each prefix. If a child compile is invoked
more than once under the same prefix in one wrapper session (e.g.
a retried compile that the wrapper re-runs), both its phase time
and its processed-work denominators accumulate together —
per-unit elapsed stays comparable across the retried invocations.

The build and smoke compiles inside `drift deploy` are
deliberately NOT retries of each other — they land under
**separate** prefixes (`build.compile.*` vs `smoke.compile.*`) so
the two compiles' workload stays distinguishable in the
consolidated summary. The additive-accumulation behavior described
above only applies WITHIN one prefix.

### Schema bumps

Adding a new key under the existing semantics is additive and does
NOT bump `workload_schema`. Removing a key, renaming one, or
redefining what a key counts requires bumping the schema and
shipping a release note.

### Output shape

JSON (`driftc --json --timing`):

```
{
  "timings": {
    "total_wall": 4.213,
    "phases": { "...": 0.0 },
    "counts": { "...": 1 },
    "workload_schema": 1,
    "workload": {
      "source.input.parse_tree_tokens": 1840,
      "source.implicit_stdlib.parse_tree_tokens": 84165,
      "hir.functions": 1226,
      "generics.instances_emitted": 103,
      "mir.processed.instructions": 58806,
      "codegen.input_mir.instructions": 58808,
      "llvm.ir.utf8_bytes": 5591847
    }
  }
}
```

Text (`driftc --timing`):

```
[drift:timing] total_wall=4.213s
[drift:timing]   parse  =   1.852s   43.9%  count=1
...
[drift:workload] workload_schema=1
[drift:workload]   codegen.input_mir.instructions=58808
[drift:workload]   generics.instances_emitted=103
[drift:workload]   hir.functions=1226
...
```

Wrapper (`drift build --timing` / `drift deploy --timing`):

```
[drift:timing][app] total_wall=12.041s
[drift:timing][app]   compile.parse  =   1.852s   15.4%  count=1
...
[drift:workload][app] workload_schema=1
[drift:workload][app]   build.compile.mir.processed.instructions=58806
[drift:workload][app]   smoke.compile.mir.processed.instructions=214
...
```

Workload lines are sorted alphabetically by key so CI greps stay
stable across releases (new keys slot in without disturbing
existing ones).

### Schema validation across processes

When a wrapper (`drift build` / `drift deploy`) merges a child
compile's workload, it forwards the child's `workload_schema` field
explicitly. The merge has four possible outcomes, each producing
a stable observable in the wrapper's workload output:

**Valid matching schema** — child's `workload_schema` is an
integer equal to the parent sink's `WORKLOAD_SCHEMA`. Child
counters merge under `<prefix>.<key>` and no marker is published.
This is the normal path.

**Schema mismatch** — child's `workload_schema` is a positive
integer (`>= 1`) but does not match the parent. The merge is
REFUSED and the wrapper publishes:

```
<prefix>.workload_schema_mismatch=<child_schema>
```

The value is the child's reported schema, so operators can identify
which toolchain version produced the dropped data. Semantics may
have shifted between toolchain versions; mislabeling counters under
the wrong schema would contaminate dashboards. To merge across
schemas deliberately (e.g. backfill from older releases), compare
schema floors in the analysis code rather than calling the merge
API.

**Schema missing or malformed** — child's `workload_schema` is
absent, `null`, not an integer (string, bool, float, dict,
list — including coercible values like `"1"`, `True`, `1.5`,
which the strict-type check rejects to prevent silent
mislabeling), or an integer `< 1` (zero, negative — schemas start
at 1 and we don't support pre-v1 or negative schema range; such
values are corruption, not a meaningful alternate schema, and
routing them to the unknown marker keeps negative numbers out
of the workload output). The merge is REFUSED and the wrapper
publishes:

```
<prefix>.workload_schema_unknown=1
```

The value is always `1` (a presence flag, not a schema value, since
no valid schema was reported). This marker signals a wrapper-side
diagnostic problem, not a normal cross-version situation — every
producer in this toolchain stamps a valid integer
`workload_schema`, so an unknown marker indicates either a corrupt
`--timing-out` JSON, a wrapper bug, or a downstream tool that
forgot to forward the field. The child's counters were NOT
merged. Investigate the child's summary JSON when this marker
appears.

Empty child workload (no counter keys) merges as a no-op with no
markers, regardless of schema — this is the legitimate "child ran
but did no workload instrumentation" case.

**Malformed counter value (matching schema)** — the child's
schema is valid, but one or more counter VALUES are invalid: not
a strict `int` (`"58806"`, `True`, `1.5`, dict, list, null), or
an `int` that is negative. Every v1 counter is a count or a byte
size, so neither type-coerced-int values nor negative values are
legitimate. The valid entries in the payload merge as usual, the
invalid entries are dropped, and the wrapper publishes:

```
<prefix>.workload_payload_invalid=<count>
```

The value counts merge events that had at least one invalid
counter (NOT the number of bad counters in a single payload), and
it accumulates additively across multiple corrupt merges under
the same prefix — a wrapper that retried a corrupt child twice
shows `=2`. The marker exists so that a published vector with
some keys missing is distinguishable from a child that
legitimately measured fewer dimensions. Without it, a child
summary like `{"source.input.files": "1",
"mir.processed.instructions": 58806}` would publish as a valid
vector with one dimension silently lost — the same
silent-mislabeling failure mode that schema-level rejection
guards against. Investigate the child's summary JSON when this
marker appears; valid producers in this toolchain emit only
non-negative integer counter values.

### Cost when disabled

Without `--timing`, no sink is installed and the workload path is
gated at each production collection site: the per-token tally in
the parser walk, per-source UTF-8 byte counting in
`parse_drift_workspace_to_hir`, the MIR-shape traversals in
`compile_stubbed_funcs` and the codegen sites, and the IR UTF-8
re-encode in `_emit_codegen` all check `events.current_sink()` and
skip the compute when no sink is installed. The check itself is a
single `ContextVar.get()` returning `None`. No traversals, no
allocations, no UTF-8 re-encodes happen on the no-`--timing` path.
