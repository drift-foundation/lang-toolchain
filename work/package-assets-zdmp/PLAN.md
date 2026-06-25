# PLAN — cryptographically-verified package assets (pack into `.zdmp` + `drift unpack`)

**Classification:** FEATURE / package-format + CLI boundary change (signed-artifact
surface). NOT a LANGUAGE_BUG. Falls under AGENTS.md "Boundary Contract Guardrails".
**Requested by:** drift-workflows (Singular / Microflows) — gating item for shipping
DB schema assets with certified packages; bookkeeper clean/cert-host provisioning is
blocked until it lands.
**Generality:** generic package-asset work — not DB-specific, no Drift "DB contract",
no Mariachi changes. Equally serves drift-web / mariadb-client doc assets.

## Goal
DB-backed (and any asset-shipping) certified packages must ship their assets **inside
the cryptographically-validated `.zdmp`**, and consumers must obtain them through a
**verify-gated, fail-closed `drift unpack`**. No loose, unverified asset files as the
trusted path.

Consumer flow target:
```
drift unpack "$DRIFT_PKG_ROOT/singular/0.5.0" --dest "$t"
mariachi --schema-template "$t/assets/singular/db" apply --schema singular_5 …
```

## Key facts established (grounded in code, 2026-06-24)
1. **Asset content is already in the signed identity.** `compute_source_content_id`
   folds every asset's `(path, sha256)` into the SCI
   (`lang/driftc/packages/source_content_id.py:256-262`), rejecting absolute / `..`
   paths at hash time (`_normalize_canonical_path`, ~L116-145). SCI is bound three-ways
   across the `.dmp` manifest, the **author-claim**, and the **cert-claim** — all
   Ed25519-signed. So asset bytes are *already* committed by content hash under both
   signatures. We are NOT inventing an integrity scheme.
2. **Assets are already declared + already staged loosely.** `manifest.py:105-126`
   (`Artifact.assets: list[str]`); `drift deploy` copies them to a loose
   `staged_install/assets/` tree via `_stage_assets()`
   (`tools/drift_deploy/drift_deploy.py:1572`). They do NOT currently travel inside the
   `.dmp`; `artifact_sha256` (cert-claim, over the uncompressed `.dmp`) does NOT cover
   them today.
3. **The container loader is additively tolerant.** DMIR-PKG v0 enforces manifest
   `.blobs` ↔ TOC consistency + per-blob sha
   (`lang/driftc/packages/dmir_pkg_v0.py:429,441,443`), but blob `type` is a free `u16`
   with no "every blob must be a module blob" rule; modules load only from
   `manifest.modules`. New asset blobs declared in `manifest.blobs` are tolerated and
   ignored by a code-only consumer → **no forced ecosystem upgrade**.
4. **No `drift unpack` exists yet.** Decompress/load/verify substrate is all present:
   `zdmp.decompress_zdmp` (`zdmp.py:56`), `dmir_pkg_v0` loader,
   `verify_deployed_v1.verify_deployed_package` (`verify_deployed_v1.py:183`). CLI
   dispatch pattern: `lang/drift/cli.py:745-807`.

## The two deliverables

### D1 — `drift deploy` packs declared assets into the `.dmp`/`.zdmp`
- **Add each `artifacts[].assets[]` file's bytes to the existing `manifest.blobs` map +
  TOC** (content-addressed by sha256), tagged with a new blob type `asset` (next free
  `u16`, e.g. `3`), and reference them from a new manifest section `manifest.assets:
  [{path, blob: "sha256:<hex>", len}]`. They go through the SAME blobs/TOC bookkeeping as
  module/interface blobs — the loader requires every TOC blob to be referenced by
  `manifest.blobs` (`dmir_pkg_v0.py:443`), so **do NOT create unreferenced asset blobs.**
- Result: assets are covered by the cert-claim's `artifact_sha256` (it hashes the
  uncompressed `.dmp`) **and** the pre-existing SCI commitment.
- **Container `format_version` stays `0`** — defensible because current loaders ignore
  unknown manifest fields (`manifest.assets`) and unknown blob types (`asset`), so a
  code-only consumer still compiles (fact #3). **This tolerance MUST be pinned with a
  regression** — it is the load-bearing backward-compat assumption.
- **Eager-blob-read caveat (must address):** the current loader eagerly reads ALL blobs
  on package load, so large assets would make *ordinary* package consumption (every code
  consumer, every compile) heavier — not just `drift unpack`. Before shipping large
  assets, either (a) make asset blobs **lazy** (load module/interface blobs eagerly,
  defer `asset`-type blobs until `drift unpack` requests them), or (b) add a **size
  guard** / declared-total cap. DB schema is small; drift-web doc assets are not — so
  this is a real cost, not hypothetical. Decide before enabling large-asset packages.
- **Stop publishing the loose `assets/` folder** — RESOLVED, see Decision D-2.

### D2 — `drift unpack <pkg-dir> --dest <dir>` (fail-closed)
- **Verify FIRST, write NOTHING on failure.** Reuse `verify_deployed_package()`
  (`verify_deployed_v1.py:183`): author + cert signatures + SCI three-way + dep-graph
  closure. Abort before any filesystem write if anything fails.
- **Trust source (explicit, no silent fallback):** mirror `drift trust verify-package`'s
  trust-source flags (`--trust-store`, `--no-user-trust-store`,
  `--allow-unsigned-from`, dev/core-trust handling) OR define a single clear default —
  but it **MUST NOT silently fall back to bundled/self trust.** An unspecified trust
  source is an error or a loudly-stated explicit default, never an implicit self-trust.
- On full pass: decompress `.zdmp`, extract asset blobs to a **temp dir**, re-check each
  blob's sha256 against the manifest, **re-sanitize every asset path at extract time**
  (no absolute, no `..`, no symlinks — reuse `_normalize_canonical_path`; verified
  author paths still get sanitized), then **atomic rename temp → dest**.
- **`--dest` MUST NOT already exist (v1).** Refuse to write into / over an existing
  directory. Replacement is deferred to an explicit later `--replace` (implemented as
  temp + swap/delete with careful failure behavior); v1 does not silently merge or
  overwrite.
- Output layout writes `<dest>/assets/…` to match the consumer flow above.
- A tampered / unsigned package can never materialize a byte (fail-closed).

## Versioning / blast radius
- **Author-claims: unchanged** — SCI already includes asset content; identity stable; no
  re-authoring.
- **Cert-claims: re-issued** for asset-shipping packages — moving asset bytes inside the
  `.dmp` changes `artifact_sha256`; re-deploy through cert once (matches ABI policy:
  artifact-format change → rebuild through cert). Packages with NO assets are byte-identical → unaffected.
- **`DRIFTC_VERSION` bumps** (behavior-changing tooling). **Runtime `DRIFT_RT_ABI`
  untouched** — package tooling, not a runtime boundary.
- Per AGENTS.md boundary rules: positive regression (asset round-trips, verifies,
  extracts, runs) + negative regression (tampered asset / unsigned / wrong-sha →
  fail-closed, nothing written) + a backward-compat regression (current loader tolerates
  asset blobs). Update any stale "assets are external / not packaged" contract comments.

## Security invariants (must hold)
- Path traversal: every asset path sanitized at BOTH pack and unpack (no abs, no `..`,
  no empty segments; reject symlinks at extract).
- Blob integrity rechecked at extract (don't trust the manifest len/sha blindly — verify
  bytes).
- Atomicity: temp + fsync + atomic rename; partial dest never visible.
- Optional size guard / declared-total cap to bound extraction (defer unless needed).

## Explicitly NOT doing
- No Drift "DB contract" concept; no separate `singular-db` artifact.
- No per-asset signed manifest / "verified loose assets" scheme (generic tools won't run
  the verify → theater). Reuse SCI + existing claims.
- No Mariachi changes.
- No container `format_version` bump (additive blob; keep code-load backward-compatible).

## Decisions
- **D-2 — RESOLVED (2026-06-24, user):** **drop the loose deployed `assets/` folder
  outright.** Do NOT add `--unverified-assets` — it preserves the exact ambiguous trust
  path we are trying to kill.
- **D-3 — RESOLVED (2026-06-24, user):** `drift unpack` MUST NOT silently fall back to
  bundled/self trust; mirror `drift trust verify-package` trust-source flags or a clear
  explicit default.
- **D-4 — RESOLVED (2026-06-24, user):** `--dest` must not exist for v1; replacement is a
  later explicit `--replace` (temp + swap/delete).

### Still open (gate implementation — see PROGRESS)
- **D-1:** spec-doc-first (`doc/design/package-assets-v0.md` → sign-off) vs implement now.
- **D-5:** large-asset strategy — lazy `asset`-blob loading vs size guard (the eager-read
  caveat under D1). Needed before enabling large-asset (drift-web) packages; DB-schema
  packages can land before this is settled.

## Implementation steps (once gated)
1. `doc/design/package-assets-v0.md` spec (if D-1 = spec-first).
2. Manifest: `manifest.assets` section + asset-blob plumbing; keep `Artifact.assets`
   author surface unchanged.
3. Deploy: pack assets as `type=asset` blobs; register in `manifest.blobs`; remove (or
   gate) loose `_stage_assets()` publish.
4. `drift unpack` subcommand in `lang/drift/cli.py` (dispatch) → new
   `tools/drift_deploy/drift_unpack.py`: verify → temp-extract → sanitize → atomic rename.
5. Regressions: round-trip (driver/e2e), negative/tamper, backward-compat loader.
6. Version bump + release note to `/tmp/drift-announce`.

## Validation criteria (definition of done)
- Asset-bearing package deploys; `artifact_sha256` covers assets; verifies end-to-end.
- `drift unpack` materializes `<dest>/assets/…`; consumer (`mariachi --schema-template`)
  flow works against extracted tree.
- Tamper/unsigned/wrong-sha → `drift unpack` writes nothing, non-zero exit, clear diag.
- Current toolchain still loads an asset-bearing package for compilation (assets ignored).
- No-asset packages unchanged byte-for-byte (no spurious re-cert churn).
