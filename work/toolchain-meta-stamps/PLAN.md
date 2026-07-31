# toolchain-meta-stamps: compiler-owned build info (drift-build-info/v1 JSON)

Status: v4.2 — W1 Stage A IMPLEMENTED (frameless section contract,
backend-neutral lang/driftc/build_info.py, both codegen paths wired,
strict schema/canonicality reader, BuildInfoError CLI boundary,
DRIFTC_VERSION 0.33.93; 33 pins green incl. the G1 package-consume
stamp pin — pulled forward from W3 per review round 3). W3 retains the
build/deploy lane-divergence and post-deploy extraction tests. W1's
--version half: LANDED (human lines, --version --json on both CLIs,
parse_toolchain_info fail-closed incl. the exactly-one-newline and
identity-floor contracts, pipe parser deleted, deploy consumer
hard-fails). W1 complete pending review confirmation.
Origin: drift-query proposal
/tmp/drift-announce/2026-07-31T073017Z-driftquery-toolchain-version-facts-proposal.md
(cc build-orchestrator, drift-workflows, drift-web, drift-mariadb-client,
drift-net-tls). Precedent: the 0.33.92 certify-lane absorption
(`drift lock emit --source-rebuild`).
Toolchain at time of writing: 0.33.92 / ABI 22 (staged, entering cert).
This slice targets the NEXT release; it must not ride 0.33.92.

Revision history:
- v1: generic `--meta`; deps/artifact_version as convention keys;
  std.cli auto-render.
- v2: deps compiler-derived; dedicated `--artifact-version`;
  percent-escaped pipe grammar; mandatory `meta_v`; additive
  `parse_with_builtins()`.
- v3: manifest identity allowlist; canonical JSON drift-build-info/v1;
  NO backward compat (compiler_info family replaced, pipe grammar
  retired everywhere).
- v4.2: FRAMELESS section contract (team decision, supersedes the
  magic/version/length/NUL framing — no backward compat): the section
  IS the canonical JSON; reader hardening kept in full (schema +
  canonicality + cap-before-decode + exactly-one-section). Shared
  module moved to backend-neutral lang/driftc/build_info.py.
  BuildInfoError surfaces oversized inputs as normal CLI diagnostics.
  G1 CLI-integration pin (validated --dep pins visible in the emitted
  stamp of a real package consume): LANDED in Stage A (review round 3
  pulled it forward from W3).
- v4 (this document): (P1) `toolchain` identity split from BUILD-
  INSTANCE facts (`word`/`profile`/`utc` move to a `build` section);
  `--version --json` gets its own `drift-toolchain-info/v1` schema on
  BOTH `driftc` and `drift`; (P1) a supported EXTERNAL stamp reader —
  stable binary section + `drift inspect build-info <binary> --json` —
  pinned post-link and post-deploy; artifact input made ATOMIC (all
  four flags or none; unstamped = `"artifact": null`); canonical JSON =
  the repo's existing convention (sort_keys, not bespoke ordering);
  `extra` values specified; std.cli precedence resolved via three
  explicit modes; `--artifact-version` accepts the manifest's arbitrary
  non-empty string (no new validation); executables-only stamping
  stated; W2 requires full compile/link/run coverage.

## 1. Problem (from the proposal, verified against our tree)

Every pool repo hand-carries facts the toolchain knows at build time
(artifact identity, driftc version, runtime ABI, resolved dep exacts)
with per-repo consistency schemes: drift-query's five pinned constants +
~200-line checker + justfile-header regex; drift-workflows'
comment-enforced RUNNER_VERSION + `0.0.0` sentinel manifests. Under the
0.33.92 certify lane, gate deps FLOAT in-range against the run pool, so
source-embedded dep exacts are wrong by construction in certify builds.
Facts stamped from the ACTUAL compiled-against inputs dissolve the
copy-skew class entirely.

In-tree starting point: `std.meta.compiler_info()` proves the mechanism
(compile-time-baked constant, zero runtime I/O) — it and its pipe
grammar are RETIRED by this slice, not extended.

## 2. Design

### 2.1 The contract: `drift-build-info/v1` canonical JSON

`std.meta.build_info()` returns one canonical JSON object baked at
codegen time. Illustrative (field order is canonicalization-defined,
see below):

    {
      "format": "drift-build-info/v1",
      "toolchain": {
        "driftc": "0.33.93", "abi": 22, "git": "…",
        "vendor": "…", "license": "…"
      },
      "build": {"word": 64, "profile": "optimized", "utc": "…"},
      "artifact": {
        "name": "mfrunner", "version": "0.7.4",
        "description": "Microflows coordinator runner",
        "license": "Apache-2.0"
      },
      "dependencies": [
        {"name": "mariadb-rpc", "version": "0.8.1"},
        {"name": "net-tls", "version": "0.6.3"}
      ],
      "extra": {}
    }

Sections:
- `toolchain` — TOOLCHAIN IDENTITY only (`driftc`, `abi`, `git`,
  `vendor`, `license`): facts true of the toolchain independent of any
  compile. COMPILER-GENERATED from today's sources (lang.versions
  constants, `_toolchain_git_sha`). Never caller-settable. ALL keys in
  every section are REQUIRED with string values (`abi`/`word` are JSON
  numbers); a fact that is unavailable (e.g. `git` in a tree with no
  stamp and no repo) is `""` — never null, never omitted. The schema
  leaves nothing to implementations.
- `build` — BUILD-INSTANCE facts that exist only during codegen
  (`word`, `profile`, `utc`). COMPILER-GENERATED
  (`_resolve_build_profile`, word size, stamp time). Never
  caller-settable.
- `dependencies` — COMPILER-GENERATED from the invocation's effective
  `--dep` set, sorted by package name. Never caller-settable; skew
  against what was actually compiled is inexpressible. `[]` when no
  `--dep` flags. Version strings are COMPILER-ACCEPTED, NON-EMPTY
  EXACT IDENTITY STRINGS — the `--dep` parser checks only
  `name@version` shape with non-empty halves and matches versions
  literally (verified: lang/driftc/driftc.py:9677); this slice adds NO
  semver validation (consistent with the artifact-version decision;
  version-shape policy belongs to piece 3).
- `artifact` — ATOMIC: `drift build`/`deploy` pass ALL FOUR
  `--artifact-*` flags or none; a partial set is a hard error at flag
  parse, and every flag VALUE must be NON-EMPTY (an empty value is the
  same hard error — atomicity is about the identity, not the argv
  shape). Unstamped compiles carry `"artifact": null` — no ambiguous
  partial identities.
- `extra` — generic user metadata via `--meta`; values are JSON
  STRINGS; empty-string values are ACCEPTED (a key's presence may
  itself be the signal). Structurally isolated: collisions with
  authoritative sections are impossible.

The compiler assembles the final object; it never accepts a
caller-provided JSON blob for any compiler-generated section.

Canonicalization: the REPOSITORY'S EXISTING JSON convention — UTF-8,
`sort_keys=True`, `ensure_ascii=False`, compact separators, no trailing
newline. No bespoke field ordering (one meaning of "canonical" in the
tree). Order inside `dependencies` (an array, untouched by sort_keys)
is sorted by package name; `extra` keys sort with the rest.

### 2.2 driftc inputs

- `--artifact-name S`, `--artifact-version S`,
  `--artifact-description S`, `--artifact-license S` — dedicated
  semantic flags for the manifest identity ALLOWLIST; all four or none
  (atomicity, §2.1). `--artifact-version` accepts the manifest's
  ARBITRARY NON-EMPTY string — the neutral manifest loader does not
  enforce `M.N.P` today, and this slice adds no new validation there
  (version-shape policy belongs to the deferred piece 3). Only these
  four fields: source paths, smoke commands, assets, author profiles,
  and build instructions are irrelevant or potentially sensitive and
  are NOT stamped.
- `--meta key=value` (repeatable) — populates `extra`. Key grammar
  `[a-z0-9_.]+` (non-empty); duplicate key = hard error; value = any
  string incl. empty. No reserved-key check needed: `extra` is
  structurally isolated.
- `dependencies`: no input exists — derived from the effective `--dep`
  set (version strings as accepted by the `--dep` parser; see §2.1).

### 2.3 Surfacing (std.meta) — REPLACEMENT, no compat

REMOVED: `compiler_info()`, `compiler_info_pairs()`, `CompilerTag`.
ADDED:
- `@intrinsic pub fn build_info() nothrow -> String` — the §2.1 JSON.
- Typed accessors so most applications never parse JSON:
    * `toolchain_version() -> String`
    * `runtime_abi() -> Int`
    * `artifact_name() -> Optional<String>`
    * `artifact_version() -> Optional<String>`
    * `artifact_description() -> Optional<String>`
    * `artifact_license() -> Optional<String>`
    * `dep_versions() -> Array<DependencyVersion>`
- `pub struct DependencyVersion(name: String, version: String)` (exact
  shape per stdlib conventions at implementation).
Accessors are compile-time-cheap (sibling baked constants or parsed
once from the baked JSON via std.json — decide at W2; the public
contract is the signatures, not the mechanism).

MIGRATION (same change, enumerated at W2 start by grep, known so far):
the std.meta module itself, e2e fixtures `std_meta_compiler_info` /
`std_meta_compiler_info_pairs` (retire/replace), stdlib/doc/example
references, every Python-side test asserting the pipe format.

### 2.4 External stamp access: binary section + `drift inspect`

The `.drift_build_info` executable section contains EXACTLY the
canonical UTF-8 drift-build-info/v1 JSON document — NO magic, framing
version, length prefix, or NUL terminator (v4.2 supersession: the
section header already supplies identity, offset, and exact byte
length; the JSON `format` discriminator supplies the schema version).
Emitter cap: 1 MiB (BuildInfoError → normal CLI diagnostic in plain
and --json modes, never a traceback).

Supported extractor:

    drift inspect build-info <binary> [--json]

SELF-CONTAINED (guardrail G2): parses the executable format directly —
never readelf/objdump, never executing the target. Fail-closed
contract (gate-facing; pinned):
- exactly ONE `.drift_build_info` section; missing or duplicate →
  exit 1, EMPTY stdout, stderr diagnostic;
- the 1 MiB limit enforced BEFORE decoding;
- UTF-8, JSON syntax, the COMPLETE v1 schema (required keys/types per
  section via validate_build_info_doc), and CANONICAL encoding all
  validated — the re-serialization comparison rejects duplicate keys
  and any trailing/leading bytes;
- empty, oversized, malformed, noncanonical content → exit 1, EMPTY
  stdout, stderr diagnostic;
- `--json` success: stdout is the section's exact canonical bytes plus
  one CLI newline.
Shared contract module: lang/driftc/build_info.py (backend-neutral —
LLVM only places the opaque JSON bytes into the section; the intrinsic
and the extractor consume the same document contract).

BINTOOLS COMPATIBILITY (a contract feature, ratified by maintainer
preference — simplicity, bintools reuse, minimal LLVM involvement):
because the section is pure JSON, standard binutils are a legitimate
manual read path with zero Drift tooling, e.g.
`objcopy -O binary --only-section=.drift_build_info <bin> /dev/stdout`
(parseable JSON) or `readelf -p .drift_build_info <bin>` — verified
working. Documented in W5. The SUPPORTED gate reader remains the
self-contained `drift inspect` (G2): certification gates must not
inherit a host-binutils presence/version dependency — same
ambient-environment principle as the 0.33.92 lock-emit work. (An
objcopy-backed extractor option was briefly floated and RETRACTED in
review round 4: G2's self-contained reader was already ratified;
binutils remain a manual convenience only, never the supported gate
path.)

Pinned by tests: extraction succeeds AFTER LINKING and AFTER normal
deployment processing (the deploy pipeline's smoke/sign/publish steps
must not strip the section — e2e runs the extractor on a
deploy-produced binary). This is what makes the orchestrator/piece-3
promise real: gates read stamps from binaries without executing them.

### 2.5 `--version` outputs (toolchain's own CLIs)

`driftc --version` and `drift --version` keep a concise HUMAN line but
drop the `|` grammar (no output of either tool uses `|` after this
slice). BOTH tools support `--version --json`, emitting the dedicated
toolchain-identity schema (truthful without a build instance — the P1
fix; no fake `build`/`artifact` fields):

    {
      "format": "drift-toolchain-info/v1",
      "toolchain": {"driftc": "…", "abi": 22, "git": "…",
                    "vendor": "…", "license": "…"}
    }

(same canonicalization as §2.1). Deploy/cert tooling that parses the
current pipe format migrates in the same change (W-sweep: grep for
`| abi` / `abi ` parsers across tools/, certifier probes, docs).

### 2.6 std.cli: additive opt-in builtins — `parse()` untouched

`parse()` remains byte-for-byte unchanged: recognizes `--version`/`-V`,
reports `cli-version-requested`, prints nothing, chooses no policy.

NEW additive `parse_with_builtins()` + `ParseOutcome` owns `--help` and
`--version` output and terminal exit semantics. `--version` output has
THREE EXPLICIT MODES (precedence ambiguity resolved):

1. DEFAULT (parser `version` == ""): the stamped block. Display name
   is ALWAYS the parser's `app` argument (explicitly supplied by the
   caller; `artifact.name` is machine-facing and never used for
   display):

       <app> <artifact.version>
       <artifact.description>
       driftc <version>, abi <n>
       license: <artifact.license>
       deps: <name@exact> …

   (description/license/deps lines iff stamped/non-empty; unstamped
   fallback: `<app> (unstamped)` + the driftc line, always available.)
2. SIMPLE (parser `version` non-empty): exactly `<app> <version>` —
   one line, the version string is a VERSION, not a block.
3. VERBATIM (new explicit setter, e.g. `p.version_output(text)`): the
   full output printed as given; overrides modes 1-2.

Pinned by tests: all three modes; unstamped fallback; `parse()`
byte-for-byte unchanged; `--help`/`--version` terminal exit semantics.

The orchestrator gets NO pool-wide stdout shape guarantee; the
gate-grade uniform surface is the stamps, read via §2.4 (future
`drift version check` reads binaries' build_info, not stdout).

### 2.7 `drift build` / `drift deploy` wiring

Both pass the four `--artifact-*` flags (atomically) from the selected
manifest artifact. `dependencies` needs NO wiring — derived from the
`--dep` flags build/deploy already pass (strict lane: lock; certify
lane: `resolve_source_rebuild` graph) — both lanes correct by
construction. `drift lock emit` untouched.

### 2.8 Materialization + scope of stamping (verified in-tree)

Extension point: the provenance channel — CLI resolves
`provenance_git_sha`/`provenance_build_profile`, threads through
`_emit_codegen(...)` into `lower_module_to_llvm(...)`
(lang/driftc/driftc.py:1582-1631), where the info constant is assembled
during LLVM lowering; the JSON constant + §2.4 section emission ride
the same channel.

EXPLICIT SCOPE: the JSON stamp applies to EXECUTABLE CODEGEN OUTPUTS
ONLY. `.dmp` packages are DMIR (pre-codegen): they remain unstamped,
byte-untouched, and keep their EXISTING package/cert metadata
(manifest, author claim, cert claim sidecars) as their identity story.
No package-format or provisional-dmir change, no payload bump.

### 2.9 Decisions (all ratified)

- `--meta` spelling; `std.meta` namespace.
- JSON contracts: `drift-build-info/v1` (stamp) and
  `drift-toolchain-info/v1` (`--version --json`, both CLIs); `format`
  discriminators mandatory.
- NO backward compat: compiler_info family replaced; pipe grammar
  retired everywhere; in-tree migration rides the same change; release
  notes MUST carry the explicit compat-break callout (consumers of
  `std.meta.compiler_info*` and old `--version` parsers must migrate).
- toolchain identity vs build-instance facts: separate sections.
- Artifact identity allowlist = name/version/description/license only;
  input atomic; unstamped = `"artifact": null`; version string
  arbitrary non-empty (no new manifest validation; piece 3 owns
  version-shape policy).
- App display name: parser `app` argument, always; manifest
  display_name deferred.
- External access: documented binary section + `drift inspect
  build-info` extractor, pinned post-link + post-deploy.

## 3. Out of scope

- Proposal piece 3 (`drift version check`, lockstep groups,
  `version_authority`, `0.0.0` sentinel retirement): DEFERRED —
  manifest-schema governance; when it lands it reads binaries' STAMPS
  via §2.4.
- RELEASE_NOTES discipline / bump semantics: repo-side.
- Non-Drift artifacts: unchanged.
- Manifest `display_name`: deferred.
- Retro-stamping already-built artifacts: none.

## 4. Work plan

- W1 driftc: `--artifact-*` (atomicity check) + `--meta` (validation
  matrix); dependencies derivation from effective `--dep` set;
  canonical-JSON assembly (repo convention) at the materialization
  point; `build_info` intrinsic emission; `.drift_build_info` section
  emission (mechanics with linker in the loop); `--version` de-piping +
  `--version --json` (drift-toolchain-info/v1) on BOTH driftc and
  drift. Unit tests: canonicalization determinism, JSON escaping
  round-trip (names/descriptions with `|`, `,`, `@`, unicode,
  newlines), deps == --dep set, partial `--artifact-*` rejection,
  unstamped shape (`artifact: null`), duplicate/malformed --meta keys,
  empty-string extra values.
- W2 stdlib: `build_info` + typed accessors + `DependencyVersion`;
  REMOVE compiler_info family; migrate ALL in-tree callers/fixtures
  (enumerate by grep first). COVERAGE BAR: build_info is a NEW
  LOWERING-VISIBLE INTRINSIC — full compile/link/RUN e2e fixtures
  (stamped + unstamped + accessor round-trips executing on the built
  binary), not checker/IR-only assertions.
- W3 drift build/deploy + inspect: pass the four `--artifact-*` flags;
  `drift inspect build-info` extractor; tests pin strict-lane vs
  certify-lane dependency stamps diverging when the pool moved (reuse
  test_prepare's snapshot world) AND extractor-on-deploy-output
  (post-link, post-deploy pinning); SWEEP deploy/cert tooling for
  pipe-format `--version` parsers and migrate them.
- W4 std.cli: `parse_with_builtins()` + `ParseOutcome` +
  `version_output()`; the three modes + unstamped fallback + parse()
  byte-unchanged + terminal semantics; e2e fixtures for the rendered
  block and a `parse()` app proving no output change.
- W5 docs + comms: effective-drift section (stamps are the contract,
  stdout is app policy), history entry with the EXPLICIT compat-break
  callout, reply announce to drift-query (accept + v4 design + phasing
  + piece-3 deferral + stdout caveat + migration note + the inspect
  extractor as the orchestrator's read path); drift-query as first
  adopter.

Versioning: next release after 0.33.92 (compiler version bump; a
SOURCE/TOOLING compat break, not an ABI break — runtime boundary and
layouts unchanged, ABI stays 22; verify at W1 review). Gate: standard
suites; corpus: the std_meta fixture retirements/replacements + new
fixtures are universe changes — enumerate for the reviewed promotion.

## 5. What downstream deletes (success criteria)

- drift-query: five version.drift constants, justfile-header pin +
  regex, the manifest/binary/lock legs of tools/version_check.py.
- drift-workflows: RUNNER_VERSION bump-together protocol (0.0.0
  sentinel retirement waits on piece 3).
- build-orchestrator: per-repo `--version` probe knowledge → `drift
  inspect build-info <binary> --json` (one format contract, no
  execution of the probed binary); stdout stays per-app policy unless a
  repo opts into the `parse_with_builtins()` default.
