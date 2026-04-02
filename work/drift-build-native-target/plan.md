# drift build: Native Target + Artifact Kind Cleanup

## Status: plan (review before execution)
## Filed by: app team (pushcoin-bookkeeper)

---

## 1. Artifact Kind Naming: `package` → `library`

### Problem

`package` is overloaded:
- noun: a reusable compiled artifact (`.dmp`)
- verb: the act of packaging/distributing
- source of confusion when discussing target/build semantics

The app team's defect partly stems from this: `kind: package` on an app
artifact (pushcoin-bookkeeper) produces package-oriented output, not
an executable. The naming reinforces the wrong mental model.

### Current state

- All production manifests use `kind: package` (drift-web, net-tls,
  mariadb-client, pushcoin-bookkeeper, singular)
- No production manifests use `kind: app`
- `kind: app` exists only in test fixtures (~5 tests)
- 132 references to `"package"` / `"app"` / `.kind` in deploy tooling

### Recommendation

Rename `kind: package` → `kind: library` in the manifest schema.

**Implementation approach**: accept both `package` and `library` during
a transition period, normalize to `library` internally.

```python
# manifest.py
if kind == "package":
    kind = "library"  # normalize legacy
if kind not in ("library", "app"):
    raise ManifestError(...)
```

**Scope of change**:
- `manifest.py`: schema validation + normalization (~5 lines)
- `drift_build.py`: dispatch on `art.kind == "library"` (~10 lines)
- `build_cmd.py`: function names / comments (~cosmetic)
- `drift_deploy.py`: same dispatch changes (~10 lines)
- `test_build.py` / `test_deploy.py`: update fixtures (~20 lines)
- `test_manifest.py`: add `library` acceptance test (~5 lines)
- Downstream manifests: update `kind` field (5 repos, 1 line each)

**Transition endpoint**: `kind: package` acceptance is temporary. After
all downstream manifests are updated (Phase 3), the normalization is
removed and `package` becomes an error. Timeline: remove compat within
one release cycle after Phase 3 lands. The compat clause must have a
deprecation warning from day one:

```
warning: 'kind: package' is deprecated; use 'kind: library'
```

**Not changed**: compiler internals, package format, `.dmp` files,
`--package-id`, `--emit-package` flags. Those use "package" in the
compiler sense (the artifact format), which is a different concept from
the manifest's artifact kind.

---

## 2. Target Defaults by Artifact Kind

### Contract

| Kind | `--target` omitted | `--target native` | `--target drift-dev` | Other targets |
|------|-------------------|-------------------|---------------------|---------------|
| `app` | Host-native executable | Host-native executable | Error: "app builds produce executables; use --target native or omit --target" | Error: unsupported |
| `library` | `drift-dev` package | Error: "library builds require a package target; omit --target for drift-dev" | `drift-dev` package | Target-specific package |

Key decisions:
- **App defaults to host-native.** No `--target` needed. `--target native`
  is accepted as explicit equivalent.
- **`drift-dev` is not a valid app target.** Apps produce executables, not
  packages. If you want a package artifact, use `kind: library`.
- **Library defaults to `drift-dev`.** Current behavior preserved.
- **`native` is not a valid library target.** Libraries produce packages,
  not executables.
- **Cross-compilation for apps is out of scope.** Only `native` (host) is
  supported. Other targets produce a clear error.

### Why `--target native` exists alongside the default

`--target native` is accepted for apps as an explicit opt-in for
documentation/scripting clarity. It is semantically identical to omitting
`--target` for app artifacts. It is NOT required.

### Unsupported targets fail, never silently map

`--target linux-x86_64`, `--target x86_64`, or any unrecognized target
for an app artifact is a hard error with a clear message. It does NOT
silently fall back to host-native. The error must name the unsupported
target and state what is supported.

---

## 3. ABI Compatibility Rule

### The narrow rule

When an app build (`kind: app`, `--target native` or default) consumes
library packages published with `target: drift-dev`:

- The compiler's ABI fingerprint check compares the app's compilation
  target against the packages' declared target
- `drift-dev` packages were compiled without a specific native target
- The app build uses `--target-word-bits <host>` (e.g., 64)

**Resolution**: the build tooling resolves this, not the compiler.

Option A (preferred): the build tool passes `--package-target drift-dev`
alongside `--target-word-bits 64` for app builds. This tells the compiler
"match packages against drift-dev" while producing native code. The
compiler's ABI check sees `drift-dev` on both sides → no mismatch.

Option B: the compiler's ABI check is relaxed for `drift-dev` packages
consumed by native app builds. **Not recommended** — this weakens a
global compiler contract for a tooling concern.

**Verification needed**: does driftc accept both `--package-target` and
`--target-word-bits` simultaneously? If not, Option A needs a compiler
change to support this combination for app builds.

---

## 4. Concrete Questions Answered

**Should app builds support only host-native for now?**
Yes. Cross-compilation is out of scope.

**Should `--target native` still exist for apps, or just be equivalent
to the default?**
Both accepted. Default = native. `--target native` = explicit equivalent.

**What should happen for unsupported app targets?**
Clear error: `"unsupported app target '<x>'; app builds produce host-native executables"`

**How should library target behavior remain unchanged?**
Library keeps `drift-dev` default and all existing `--target` behavior.
The only change is the `kind` name (`package` → `library` with compat).

---

## 5. Implementation Phases

### Phase 1: Manifest kind normalization

- Accept `library` alongside `package` in manifest schema
- Normalize `package` → `library` internally
- Update test fixtures
- No behavior change

**Size**: ~20 lines across manifest.py + tests

### Phase 2: App native build support

- App artifacts default to host-native (`--target-word-bits <host>`)
- `build_app_cmd` plumbs `--target-word-bits`
- `drift_build.py` validates target per kind
- Error messages for invalid targets
- Resolve ABI fingerprint interaction (Option A or verification)

**Regressions**:
- Positive: app with no `--target` → linked executable
- Positive: app with `--target native` → linked executable
- Negative: app with `--target drift-dev` → clear error
- Negative: app with `--target linux-x86_64` → clear error naming the
  unsupported target
- Positive: library with no `--target` → drift-dev package (unchanged)
- Positive: app consuming drift-dev libraries → no ABI error

**Size**: ~30 lines across drift_build.py + build_cmd.py

### Phase 3: Downstream manifest updates

- Update production manifests: `kind: package` → `kind: library`
- Update downstream CI/scripts if they reference `kind`
- Can happen in parallel with Phase 2

**Size**: 1 line per manifest, 5 repos

---

## 6. Non-Goals

- No cross-compilation support for apps
- No compiler ABI relaxation
- No change to `.dmp` package format or `--emit-package` semantics
- No change to the compiler's `--package-target` flag behavior
- No renaming of compiler-internal "package" concepts (package provider,
  package consumer, `.dmp`) — those refer to the artifact format, not
  the manifest kind
