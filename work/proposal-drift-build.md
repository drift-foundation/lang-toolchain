# Proposal: `drift build` — manifest-driven local artifact builds

## Summary

Add a `drift build <artifact-name>` command that reads `drift-manifest.json`
and invokes `driftc` for one declared artifact.

The MVP is intentionally narrow:

- artifact-driven only
- no ad hoc source-file mode
- no package fetching or dependency solving
- no automatic native `--link-search` resolution
- no ambient home-directory package-root default

The goal is to eliminate the two recurring sources of drift in local build
scripts:

- duplicated source file lists
- duplicated `--dep name@version` pins

This is a manifest-aware wrapper over `driftc`, not a replacement for it.

This proposal also assumes `drift init` is the first-run bootstrap path for a
new publishable project. For MVP, `drift init` should be able to scaffold a
minimal `drift-manifest.json` when one does not already exist.

The reproducibility contract should be:

- `drift-manifest.json` may express author intent, including dependency ranges
- resolution chooses exact versions
- published package metadata records the exact resolved dependency set actually
  used for the build

For MVP, `drift build` should consume existing exact dependency state. It
should not become a second general-purpose resolution workflow.

## Problem

Today, `drift-manifest.json` already describes the build-relevant structure of
each declared artifact:

- artifact identity
- source file list (`modules`)
- entry module (`entry_module`)
- package dependency pins (`package_deps`)

But local development builds still restate that structure manually in `just`
recipes, shell scripts, or CI glue.

Example:

```bash
driftc --target-word-bits 64 \
    --package-root "$DRIFT_PACKAGE_ROOT" \
    --dep web-rest@0.2.2 \
    --dep web-jwt@0.2.2 \
    --dep net-tls@0.3.5 \
    -o my_server \
    packages/my-server/src/main.drift \
    packages/my-server/src/router.drift \
    packages/my-server/src/config.drift
```

This creates two real failure modes:

1. dependency versions drift between `drift-manifest.json` and local build
   recipes
2. source file lists diverge from the manifest and silently stop matching the
   artifact definition used by deploy

The recent `net-tls` version drift in downstream web build scripts is exactly
the kind of issue this should eliminate.

There is also a new-user bootstrap gap today:

- `drift init` sets up publisher identity
- but a new project still has to hand-author `drift-manifest.json` before
  `drift build`, `drift prepare`, or `drift deploy` can do anything useful

For MVP, the onboarding story should be:

- `drift init` can create the initial manifest scaffold
- `drift build` can then build the declared artifact from that manifest

## Proposed UX

### Primary command

Build one named artifact from `drift-manifest.json`:

```bash
drift build web-client
drift build my-server
```

`drift build`:

1. reads `drift-manifest.json`
2. resolves the named artifact
3. chooses the build shape from the artifact kind
4. translates manifest fields into the corresponding `driftc` invocation
5. invokes `driftc` with the artifact's:
   - `modules`
   - `entry_module`
   - exact resolved dependency information derived from `package_deps`

The wrapper should translate manifest structure into existing compiler flags.
It should not invent new compiler concepts.

For example:

- manifest `entry_module` becomes the appropriate `driftc` entry/input wiring
- manifest `package_deps` may be ranges or other author-declared intent in
  project source
- `drift build` consumes exact versions from existing project state before
  invoking `driftc`
- package builds should emit only the exact resolved dependency versions
  actually used
- app builds consume exact `--dep name@version` pins for compilation
- manifest `modules` become the source inputs

Artifact kind matters:

- package artifact:
  - output is a `.dmp`
  - uses the package-oriented `driftc` flags (`--emit-package`,
    `--package-id`, `--package-version`, `--package-target`,
    `--package-dep`, `--native-link-lib`, `--allow-unsafe` when declared)
- app artifact:
  - output is a local executable
  - uses the app-oriented `driftc` flags (`-o`, `--target`, `--link-lib`,
    `--allow-unsafe` when declared)

So `drift build` is not one uniform compiler call. It is one command that
selects the correct build path from the artifact kind already declared in the
manifest.

### Entry module wiring

The proposal should pin the current source ordering contract rather than leave
it implicit.

`drift build` should pass source inputs the same way deploy does today:

1. first positional source input = `entry_module`
2. then append the remaining `modules`
3. deduplicate so `entry_module` is not passed twice if it already appears in
   `modules`

That ordering should be shared with deploy rather than reimplemented
independently.

### Output path

For local builds, the command should have a stable default output convention
with an explicit override.

Default outputs:

- package artifact: `build/<artifact-name>.dmp`
- app artifact: `build/<artifact-name>`

Override:

- accept `-o/--output` for explicit local output path

### Package target for local package builds

Package builds also need a `--package-target` value.

For MVP:

- `drift build` should accept an explicit target override
- otherwise local package builds should default to `drift-dev`, matching the
  current deploy default target

### Dependency metadata in published packages

Published package metadata should record exact resolved dependencies, not the
original selection expression from project source.

That means:

- `drift-manifest.json` may express flexible author intent such as ranges
- lock/resolution chooses concrete versions
- the emitted package should carry only the exact resolved dependency versions
  actually used to produce that artifact

This gives the published artifact a stronger reproducibility story:

- someone can inspect the emitted metadata later
- reconstruct the same build inputs
- and reproduce the same output without needing the original range expressions

For MVP, this should work as follows:

- if `drift-lock.json` exists, `drift build` should use the exact locked
  versions from that file
- if no lockfile exists, `drift build` should require exact versions in
  `package_deps`
- if a dependency declaration is a range and no lockfile is present,
  `drift build` should fail with a clear message directing the user to prepare
  the project state first

So `drift build` may consume lock state, but it should not own lock updates or
open-ended dependency solving as part of the MVP.

### Single-artifact shortcut

If the manifest declares exactly one artifact, the name may be omitted:

```bash
drift build
```

If the manifest declares multiple artifacts and no name is provided,
`drift build` should fail and require an explicit artifact name.

### Explicit machine-local overrides

`drift build` may still accept a limited set of explicit pass-through or
override flags for machine-local or debugging concerns, for example:

```bash
drift build web-client --package-root /opt/drift/libs
drift build web-rest -- --emit-llvm-ir
```

But those are escape hatches. The core artifact definition remains manifest
driven.

## New-project bootstrap

For MVP, `drift init` should be more than publisher-identity setup. If
`drift-manifest.json` does not exist in the project root, `drift init` should
offer to create a minimal manifest scaffold.

That prompt flow should collect:

- project/package name
- kind
- version
- initial artifact name
- entry module path
- module namespace

This is intentionally modest. The goal is not to solve every future manifest
shape up front. The goal is to get a new user from:

- empty repo or fresh project directory

to:

- a valid `drift-manifest.json`
- publisher identity initialized
- one declared artifact that `drift build` can compile

So the MVP onboarding flow becomes:

1. run `drift init`
2. if needed, generate or resolve signing key and author profile
3. if `drift-manifest.json` is missing, scaffold a minimal manifest
4. run `drift build <artifact>` against the generated manifest

This should be treated as a separate implementation unit from `drift build`
itself:

- `drift build` only requires a valid existing manifest
- `drift init` manifest scaffolding is onboarding work, not a prerequisite for
  implementing manifest-driven builds

## Explicit non-goals for MVP

This proposal does **not** include:

1. ad hoc source-file compilation through `drift build`

   Not proposed:

   ```bash
   drift build --entry main::main -o my_server my_server.drift
   ```

   That mode is underspecified and creates ambiguity around:

   - which artifact's dependency set applies
   - which source list is authoritative
   - how multi-artifact manifests should behave
   - how local one-off builds relate to deploy-defined artifact identity

   For one-off/manual compilation, use `driftc` directly.

2. `drift test`

   Test-runner design is separate work. This proposal is only about local
   artifact compilation.

3. automatic native `--link-search` resolution

   `native_deps` carry library names, not machine-local search paths.
   Native search paths remain external configuration.

4. package fetching, solving, or lockfile updates

   `drift build` is not a package manager and not a release-preparation step.
   It consumes already-available package roots and existing exact dependency
   state.

5. replacing `driftc`

   `drift build` is a manifest-aware wrapper. `driftc` remains the underlying
   compiler interface for low-level and ad hoc use.

6. full project scaffolding beyond a minimal manifest

   `drift init` should create enough manifest structure for a new user to get
   started, but it does not need to solve every advanced multi-artifact or
   deployment layout at project-creation time.

7. smoke/test validation builds during `drift build`

   `drift build` is a local build command, not a smoke/deploy pipeline.

   For MVP:

   - building a package artifact produces its `.dmp`
   - building an app artifact produces its executable
   - no extra `--test-build-only` smoke pass is implied

   Deploy keeps owning publish-time smoke behavior.

## MVP boundary: what the manifest owns vs. what stays external

The proposal needs to be honest about the current boundary.

### Manifest-owned: what to build

`drift-manifest.json` is the source of truth for:

- artifact identity
- source file list (`modules`)
- entry module (`entry_module`)
- author-declared dependency intent (`package_deps`)
- artifact/package metadata
- `unsafe`

This is what `drift build` should consume directly.

### External/project config: how and where to build

For MVP, these remain outside artifact metadata:

- package root location
- native library search paths
- target word-bits / target selection
- debug/build-mode flags
- other machine-local compiler options

Those are still configured through explicit CLI flags, environment variables,
or project-local config files.

That is not a weakness in the MVP. It is an intentional boundary:

- `drift-manifest.json` defines the artifact
- external/project-local config defines the machine/runtime environment

## Package-root resolution

`drift build` still needs to know where already-published packages live.

The proposal should not use an ambient default like `~/opt/drift/libs`.
That reintroduces the kind of hidden state we have been removing elsewhere.

For MVP, package-root resolution should be explicit and visible:

Priority order:

1. `--package-root` on the command line
2. project-local config (`drift-deploy-config.json`)
3. `DRIFT_PACKAGE_ROOT` environment variable

This gives us:

- explicitness
- checked-in project defaults when desired
- no per-invocation repetition when a project wants stable local config
- no user-home magic

For MVP, this proposal should not introduce a second build-only config file.
If project-local config is needed, `drift build` should reuse
`drift-deploy-config.json` rather than invent `drift-build-config.json`.

## Native library paths

MVP should not claim that `drift build` can infer native `--link-search`
paths from package metadata.

Current honest model:

- package/artifact metadata can describe required native libraries
- machine-local search paths remain external

So native library paths should remain sourced from:

- explicit CLI flags
- environment
- `drift-deploy-config.json`

This is acceptable because the repeated downstream bugs are in dependency pin
duplication, not in native search-path inference.

## Stdlib root

`--stdlib-root` is not manifest-owned artifact structure. It should remain a
machine-local compiler concern, handled the same way other low-level `driftc`
inputs are handled.

For MVP:

- `drift build` should not invent stdlib metadata in the manifest
- if callers need to pass `--stdlib-root`, they should be able to do so
  explicitly
- otherwise `drift build` should rely on the normal `driftc` behavior/defaults

## Relationship to `drift prepare` and `drift deploy`

`drift build` should reuse the same artifact interpretation as deploy where
possible, but it is not part of the release-state mutation/publish workflow.

The model should be:

- `drift build <artifact>` = local compile from one manifest-declared artifact
- `drift prepare` = update lockfile/release state
- `drift deploy` = build, sign, smoke, and publish committed state

So:

- `drift build` is not a deploy shortcut
- `drift build` does not mutate tracked release files
- `drift build` should align with artifact structure already used by deploy
- `drift build` should share manifest parsing and artifact-to-command
  translation with deploy where possible

## Example manifest

Example artifact snippet:

```json
{
  "kind": "package",
  "name": "web-client",
  "module_namespace": "web.client",
  "version": "0.2.2",
  "entry_module": "packages/web-client/src/lib.drift",
  "modules": [
    "packages/web-client/src/lib.drift",
    "packages/web-client/src/request.drift",
    "packages/web-client/src/response.drift",
    "packages/web-client/src/errors.drift",
    "packages/web-client/src/transport.drift"
  ],
  "package_deps": [
    {"name": "net-tls", "version": "0.3.5"}
  ]
}
```

This is enough for `drift build` to eliminate duplicated:

- source file lists
- `--dep net-tls@0.3.5`

It is **not** enough to eliminate all other build flags, and the proposal
should not claim otherwise.

## Benefits

1. Single source of truth for local artifact structure

   Source file lists and dependency pins live in `drift-manifest.json`, not in
   duplicated build recipes.

2. Manifest-only dependency version updates

   When a dependency version changes in `drift-manifest.json`, local build
   scripts do not also need manual `--dep` updates.

3. Better alignment between local builds and deploy-defined artifacts

   The same artifact definition drives both local build and deploy.

4. Lower onboarding friction

   New contributors can build a declared artifact without reverse-engineering
   a large recipe full of repeated source paths and dependency pins.

5. Smaller build recipes

   Build scripts can become thin wrappers around:

   ```bash
   drift build <artifact-name>
   ```

   plus only the machine-local concerns that genuinely remain external.

## Example: before vs. after

### Before

```just
net_tls_dep := "net-tls@0.3.5"

build-client:
    driftc \
      --package-root {{env_var("DRIFT_PACKAGE_ROOT")}} \
      --dep {{net_tls_dep}} \
      packages/web-client/src/lib.drift \
      packages/web-client/src/request.drift \
      packages/web-client/src/response.drift
```

### After

```just
build-client:
    drift build web-client
```

If machine-local configuration is still needed, that remains explicit:

```just
build-client:
    DRIFT_PACKAGE_ROOT=$HOME/opt/drift/libs drift build web-client
```

## Implementation notes

The wrapper should be simple and explicit:

- load `drift-manifest.json`
- resolve one artifact by name
- select package-vs-app build shape from artifact kind
- pass `entry_module` first, then append remaining `modules` with deduplication
- resolve manifest dependency declarations to exact versions before invoking
  `driftc`
- use `drift-lock.json` when present as the source of exact pinned versions
- if no lockfile exists, require exact versions in manifest dependency
  declarations
- if no exact version is available for a dependency, fail clearly instead of
  performing a second implicit resolution flow
- pass app artifact deps as exact `--dep name@version`
- pass package artifact dependency metadata in a form that records the exact
  resolved versions used for the build
- map package artifacts to package-oriented `driftc` flags
- map app artifacts to app-oriented `driftc` flags
- map manifest `unsafe` to `--allow-unsafe` when declared
- consume package-root and other machine-local settings from explicit flag,
  `drift-deploy-config.json`, or environment
- allow extra raw `driftc` flags after `--`

It should reuse existing manifest parsing and artifact interpretation wherever
possible instead of inventing a second incompatible model.

In practice, that likely means extracting shared helpers from the existing
deploy code rather than re-encoding artifact translation a second time.

## Proposed MVP contract

`drift build` in v1 means exactly:

- build one manifest-declared artifact
- from `drift-manifest.json`
- using manifest-owned source files and dependency pins
- while leaving machine-local concerns explicit and external

That is enough to solve the current version-drift and file-list duplication
problem without overclaiming what the manifest already knows.
