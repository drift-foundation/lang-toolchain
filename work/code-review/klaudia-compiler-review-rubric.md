# Klaudia Compiler Review Rubric

Purpose: catch adjacent-path and boundary bugs during review of compiler, runtime, package, deploy, and FFI patches before downstream teams do.

Use this rubric for every Klaudia report that changes compiler behavior, runtime behavior, package behavior, deploy artifact behavior, or stdlib behavior that depends on compiler/runtime boundaries.

## Review posture

- Do not review only the reported symptom.
- Review the whole bug class and its neighboring boundary paths.
- Default assumption: if one path was fixed, the adjacent reconstruction/import/deploy/destructor/consumer path may still be broken until proven otherwise.
- Require explicit evidence for widened support claims.

## Required report sections

Before clearing a patch, require the report to include:

- Minimal repro path
- Failing regression path
- Root cause
- Exact files changed
- Boundary sweep summary
- Validation summary
- Explicit list of unchecked adjacent paths, if any

If any of those are missing, treat the review as incomplete.

## Core review questions

For every patch, answer:

1. What exact boundary or invariant failed?
2. Where else is the same metadata/type/name/id reconstructed or lowered?
3. What is the nearest adjacent path likely to share the same bug class?
4. Did the patch widen support beyond the original repro?
5. If support widened, is every widened shape pinned by regression?

## Boundary matrix review

When a bug touches one side of a boundary, explicitly check the mirror paths.

### Source vs package

If a fix touches source compilation, check:

- package serialization
- package deserialization
- package-consumer lowering/codegen
- signed-package/deployed package-root path

If a fix touches package consumption, check:

- source path
- package path with one package
- package path with stdlib package also loaded
- package path with two user packages loaded

### Single vs multi

If a fix touches ids, names, registries, or metadata interning, check:

- single module
- multi-module package
- single package
- multi-package load

### Simple vs constructed types

If a fix touches type support, check:

- builtin scalar
- constructed builtin (`RawPtr<T>`, `Array<T>`, `Ref<T>`, `FnResult`, etc.)
- nested constructed types
- recursive types if relevant

### Ordinary path vs special impl path

If a fix touches types or lowering, check:

- plain function
- method
- trait impl
- inherent impl
- `Destructible`
- generated wrappers/entrypoints if applicable

### Normal run vs deploy/deployed-package

If a fix touches package loading, stdlib loading, or toolchain behavior, check:

- in-repo path (`--stdlib-root` / source stdlib)
- deployed/package-root path (`std.dmp`)
- PEX/deployed artifact path if relevant

## Hot-zone checklists

Use the relevant checklist depending on patch type.

### Package-boundary checklist

- serialize field added?
- deserialize field restored at all reconstruction sites?
- id remap updated?
- builtin-type tables updated consistently?
- host-key / canonicalization / link-time maps updated?
- consumer path tested with stdlib package loaded?
- consumer path tested with second package loaded?
- trait/impl metadata path checked?
- destructor/generated impl path checked?
- imported codegen path checked?

### FFI checklist

- source-file path checked?
- package-consumer path checked?
- non-main module path checked?
- cross-module import/export path checked?
- bare C symbol identity preserved everywhere?
- fixed-width ABI types used in regressions/examples?
- void-return path checked?
- pointer/raw-pointer path checked?
- link flags/package deploy path checked if relevant?

### Ownership/lowering checklist

- bitcopy Copy type checked?
- non-bitcopy Copy type checked?
- non-Copy type checked?
- borrowed source checked?
- owned source checked?
- recursive type checked if relevant?
- memcheck checked if ownership/drop is involved?
- call, constructor, return, bind/reassign, and match transfer points checked if relevant?

### Deploy/deployed-toolchain checklist

- in-repo path checked?
- staged artifact path checked?
- published/deployed path checked if relevant?
- read-only install-tree path checked?
- no ambient Python/package dependency?
- package-root signed stdlib path checked?

## Regression rules

- The directly reported repro must be pinned.
- If the patch fixes adjacent shapes too, at least one regression must pin each widened category.
- Do not accept "latent fix" claims without regression evidence.
- Prefer e2e for end-to-end language/runtime/package behavior.
- Use driver tests for package/import/codegen/load path isolation where e2e is unnecessarily heavy.

## Pushback rules during review

Push back if any of the following are true:

- The patch claims support for a new shape but the regression does not exercise it.
- The patch updates one boundary stage but leaves other boundary tables/maps/comments stale.
- The patch fixes source path only and does not address package/deployed path for the same metadata.
- The patch fixes single-package behavior but not multi-package behavior for ids/registries.
- The patch adds examples/tests with ABI-sloppy extern signatures.
- The patch widens support but leaves negative or contract tests contradictory.

## Review output format

When reviewing a Klaudia report, answer in this order:

1. Findings first, ordered by severity
2. Open questions or unverified adjacent paths
3. Brief change summary only after findings

If there are no findings, still mention:

- what boundary cells you spot-checked
- residual risks or unpinned adjacent paths

## Common failure patterns to look for

- Source-path fix not mirrored on package-consumer path
- Source stdlib path clean but packaged stdlib path broken
- One reconstruction site updated, another left stale
- Bare-name preservation fixed in one loader, still mangled in another
- New scalar type added to checker/codegen but omitted from package builtin lists
- Package-local ids incorrectly treated as globally unique
- Simple scalar remapped, nested constructed type left at package-local or UNKNOWN sentinel ids
- Inherent impl path fixed, trait/destructor/generated path still broken
- Normal mode passes, memcheck/deployed/package-root path fails

## Standing expectation for Klaudia reports

For compiler/runtime/package/deploy work, Klaudia should proactively include:

- the boundary matrix cells checked
- the adjacent path most likely to still be broken
- whether stdlib-as-source and stdlib-as-package were both exercised
- whether single-package and multi-package paths were both exercised when ids/metadata are involved

If that material is absent, the patch should be assumed under-reviewed until checked.
