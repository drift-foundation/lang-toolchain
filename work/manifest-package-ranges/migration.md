# 0.29.0 package-versioning migration

Guidance for downstream teams consuming Drift after the 0.29.0 toolchain
bump.  This note is operational — the full design rationale lives in
`docs/history.md` under the 2026-04-19 entry.

## What changed

The manifest format now separates declared ranges from exact locks.
Three names, each with exactly one job:

| Surface                              | Who writes it                 | What it carries                                                                    |
|--------------------------------------|-------------------------------|------------------------------------------------------------------------------------|
| `drift/manifest.json::package_deps`  | you, by hand                  | **owner-declared acceptable range** — `"M"` or `"M.N"` only                        |
| `.dmp::required_deps`                | `driftc --emit-package`       | the range above, copied into the published package                                 |
| `drift/lock.json::artifacts.resolved`| `drift prepare`               | **the exact graph resolved at prepare time** — `M.N.P` + `sha256` + `author_key`   |

- Authored versions are owner-declared acceptable ranges.  `"0.3"` means
  "I accept any `0.3.x` release"; `"1"` means "any `1.x.x`".  Drift does
  not decide compatibility — the owner does, by choosing the range.
- The lock is the exact graph resolved at prepare time.  It is NOT a
  constraint that crosses the package boundary to downstream consumers;
  only the producer's `required_deps` ranges do.
- Patch and minor movement happens **only** in `drift prepare`.
  `drift build`, `drift deploy`, and `driftc` all consume the lock
  verbatim.  Upgrading a transitive means running `drift prepare`, not
  editing the manifest.

## Clean break — no compatibility shim

Packages published with a pre-0.29 toolchain carry the old
`package_deps` key on their `.dmp`, not `required_deps`.  The 0.29+
consumer-side loader rejects those with a diagnostic naming the
toolchain gate.  **Republish packages on 0.29+ before downstream
builds can consume them.**

Pre-0.29 `drift/lock.json` files (v1 or v2) are likewise rejected at
load — no silent reinterpretation of ranges as pins.  Run
`drift prepare` to regenerate a v3 lock.

## Migration steps (downstream teams)

Run these in order.  Every step is explicit; nothing mutates authored
files without the command that mutates it.

1. **Update to the 0.29.0 toolchain.**

2. **Migrate the authored manifest** (one time, per project):

   ```
   drift manifest migrate
   ```

   - Converts each `package_deps[].version` from `M.N.P` to `M.N`.
   - Leaves `M` and `M.N` entries unchanged.
   - Bumps `schema_version` to 2.
   - Rejects `^` / `~` / garbage versions with a per-dep diagnostic
     and writes nothing — fix those by hand and re-run.
   - Running twice is a no-op (file mtime preserved).
   - `--dry-run` prints the plan without touching the file.

3. **Regenerate the lock:**

   ```
   drift prepare
   ```

   Walks `package_deps` + every package's `required_deps`, resolves
   the full transitive graph to the highest-in-range candidate per
   package, and writes `drift/lock.json` v3 with `M.N.P` + `sha256` +
   `author_key` per entry.

4. **Republish every package you produce** so its `.dmp` carries
   `required_deps` (the producer-side range declaration that
   downstream `drift prepare` will consume).  Cut a normal release
   with:

   ```
   drift deploy
   ```

   The emitter copies your authored `package_deps` into
   `.dmp::required_deps` automatically.

5. **Re-run certification / build / deploy** using the generated
   lock:

   ```
   drift prepare --check     # CI gate: is the lock up-to-date?
   drift build               # consumes the lock exactly
   drift deploy              # consumes the lock exactly
   ```

## If a version in your manifest is not accepted

`drift manifest migrate` validates every dep version before writing
anything.  Likely causes and fixes:

| Authored form     | v2 shape to use | Notes                                                      |
|-------------------|-----------------|------------------------------------------------------------|
| `"0.3.14"`        | `"0.3"`         | migrator rewrites this automatically                       |
| `"0.3"`           | unchanged       | already v2                                                 |
| `"1"`             | unchanged       | already v2 — means any `1.x.x`                             |
| `"^0.3.0"`        | fix by hand     | operator vocabulary removed; pick `"0.3"` or `"0"`         |
| `"~0.3.14"`       | fix by hand     | operator vocabulary removed; pick `"0.3"`                  |
| `"0.3.14-beta"`   | fix by hand     | authored versions are numeric `M` / `M.N`                  |
| `">= 0.3.0"`      | fix by hand     | operator vocabulary removed                                |

## If `drift prepare` fails

Almost every prepare-time failure is "no version satisfies the
requested range across the graph".  Typical shapes:

- Two direct deps pull in the same transitive under disjoint ranges
  (e.g. `deplib = "0.1"` vs `deplib = "0.2"`).  Fix by bumping one
  side to a range that covers the same transitive version the other
  side was built against.
- A transitive's published `required_deps` demands a version that
  is not present in your package roots.  Install the missing
  version, or accept a wider range.

The diagnostic names every constraint source — use it to identify
which direct dep is pulling in the conflicting transitive.

## If `drift build` / `drift deploy` fails

0.29+ build/deploy are strict-exact consumers of the lock.  They
**never** resolve ranges at build time.  Typical diagnostics and
fixes:

| Error                                 | Meaning                                                                 | Fix                            |
|---------------------------------------|-------------------------------------------------------------------------|--------------------------------|
| "no `drift/lock.json` exists"         | artifact declares `package_deps` without a lock                         | run `drift prepare`            |
| "schema v1 / v2 is not supported"     | stale lock from a pre-0.29 toolchain                                    | run `drift prepare`            |
| "version … is not an exact M.N.P pin" | lock was hand-edited or truncated                                        | run `drift prepare`            |
| "not found under package roots"       | lock pins a version the package roots don't have                         | install the pin, or re-prepare |
| "sha256 mismatch"                     | the `.dmp` at the pinned version was rebuilt                             | run `drift prepare`            |
| "signing key changed"                 | the signer rotated since the lock was written                            | run `drift prepare`            |

In every case the diagnostic cites `drift prepare` by name; when in
doubt, that is the one command you run to bring the lock back into
sync with the world.

## driftc-level constraints (advanced)

If you invoke `driftc` directly (not via `drift build` / `drift
deploy`), the exact-loader contract applies to you too:

- Every `required_deps` entry on every loaded package must either
  name the self-package or have an exact `--dep PKG@M.N.P` that
  satisfies the declared range.
- Missing `--dep`: `"no --dep is pinned for it; driftc is an exact
  loader and cannot invent a version from a range.  Run
  `drift prepare` / `drift build`."
- Range violation: `"transitive dependency version conflict for
  'PKG': the --dep pin 'X' does not satisfy the required_deps range
  'Y' declared by package 'Z'.  driftc is an exact loader and will
  not pick a different version from the package roots."

In practice: let `drift prepare` build the pin list.  It sorts
every transitive, emits the exact versions driftc expects, and
never loses entries.

## FAQ

**Q. Why not a compatibility shim?**
A. The two-layer model is structural, not cosmetic: producer ranges
flow to consumers, locks stay local.  A shim that reinterpreted a
pre-0.29 exact `package_deps` as a minor range would silently rewrite
intent — and pre-0.29 was unreleased / internal transition work, so
there is no user-visible contract to preserve.

**Q. Does `drift prepare` rewrite my manifest?**
A. No.  Normal reads never mutate authored files.  Only
`drift manifest migrate` rewrites the manifest, and only when you
invoke it explicitly.  `drift prepare` refuses v1 manifests and
points at the migrator.

**Q. What is the `.dmp::required_deps` field for?**
A. It is the producer's published dependency requirements, in the
owner-declared-range vocabulary — copied from your manifest
`package_deps` at emit time.  Downstream consumers' `drift prepare`
reads these to build the full transitive graph.  It is NOT a
constraint you edit directly; it always mirrors your manifest.

**Q. How do I upgrade a single transitive to a patch release?**
A. Run `drift prepare`.  The resolver picks the highest version
satisfying every declared range in the graph — so a new patch
release available under the pinned roots will flow in automatically
on the next prepare.  No manifest edit, no intermediate library
republish.

**Q. How do I lock to a specific patch for reproducibility?**
A. That is what `drift/lock.json` already does — pins the exact
`M.N.P` for every package in the transitive graph.  Commit the lock
alongside the manifest.
