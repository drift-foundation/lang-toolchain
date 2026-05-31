# `--sanitize` flag — explicit sanitizer selection (replace env-driven `DRIFT_ASAN`)

**Origin:** drift-web team request — `/tmp/toolchain-request-sanitize-flag.md`.
Make sanitizer selection an explicit `driftc` flag instead of the ambient
`DRIFT_ASAN` / `DRIFT_UBSAN` environment variables, with the env vars kept as
deprecated aliases for ≥1 release and the flag winning on conflict.

**Scheduling note:** assessed as a *next-cycle* feature, not a cut-day change —
it is additive with a deprecation path, nothing is broken, and drift-web is
unblocked either way (they build against the env overlay today and swap one job
line when the flag lands). Proceeding now at explicit owner direction.

---

## Decisions (resolved)

1. **Two layers, clear owner.** Per the "users go through `drift`, not `driftc`"
   direction:
   - `driftc --sanitize=<list>` is the real flag and the single validation +
     consumption point (the compiler is where `-fsanitize` + the matching
     runtime-archive variant are actually selected).
   - `drift build --sanitize=<list>` forwards the flag verbatim to the `driftc`
     subprocess. This is the surface drift-web's plan/runner should emit, so
     build variation stays in explicit argv (their stated principle) **and**
     goes through `drift`.

2. **`--sanitize` is authoritative when given.** It fully specifies the
   sanitizer set; when present, `DRIFT_ASAN` / `DRIFT_UBSAN` are ignored
   entirely (explicit > ambient). When absent, fall back to the env vars
   (deprecated alias path, unchanged behavior). `--sanitize=address` → asan
   only, even if `DRIFT_UBSAN=1` is exported.

3. **Token vocabulary:** `address`, `undefined`, and `none`. Comma-separated.
   `none` is the explicit "no sanitizers" spelling and may not be combined with
   others. Unknown tokens → argparse usage error (exit 2). This generalizes to
   `tsan`/`msan` later without a format change (the request's extensibility
   point).

4. **Flag → canonical env normalization (implementation mechanism).** The
   compiler already reads sanitizer state from `DRIFT_ASAN`/`DRIFT_UBSAN` at
   three points: `_resolve_build_profile` (profile label), the runtime-archive
   `variant` selection, and the `-fsanitize` link flags. Rather than re-thread a
   resolved tuple through all three (larger diff, easy to miss a site in a
   certified path), the flag is normalized **once** at the `_run_compile_cli`
   entry into those same env vars (`os.environ["DRIFT_ASAN"]="1"/"0"`). Every
   downstream reader then sees one canonical source of truth. This is the exact
   pattern `drift build --debug` already uses (`--debug` → `DRIFT_DEBUG=1` on the
   driftc subprocess). The *interface* is the flag; env is the deprecated alias.

5. **The runtime-archive variant is part of the contract.** `--sanitize=address`
   is NOT just `-fsanitize=address` on the link line — it must also select the
   `asan` runtime-archive variant (`runtime_archive_variant`), or the user
   binary links an instrumented frontend against an uninstrumented runtime.
   Because the variant is computed from the same `asan_enabled`/`ubsan_enabled`
   that the normalized env drives, normalization (Decision 4) gets this for free.

---

## Files & exact sites

**`lang/driftc/driftc.py`**
- `~line 8478` (argparse block, near `--timing` / `-g`): add
  `--sanitize` with a custom `type=` parser that validates tokens and returns a
  `frozenset[str]` (or `None` when omitted). Help text documents tokens + the
  env-deprecation + "flag wins."
- `~line 8566` (right after `debug_style_runtime = _env_true("DRIFT_DEBUG")`):
  resolve `(asan, ubsan)` from `args.sanitize` (authoritative) else env, and
  normalize back into `os.environ` so the three downstream readers are
  consistent. Comment referencing the `--debug`/`DRIFT_DEBUG` precedent.
- Sites at 94 / 13175–13176 / 13190 are **left unchanged** — they read the
  now-canonical env. (Verified these are the only sanitizer-state consumers.)

**`tools/drift_deploy/drift_build.py`**
- `~line 106` (after `--debug`): add `--sanitize` (raw string passthrough).
- `~line 651` / combined_extra assembly: when `args.sanitize` is set,
  `combined_extra.extend(["--sanitize", args.sanitize])`. Forwarded as an
  explicit driftc flag (NOT normalized to env — drift-web wants argv-explicit).
  Validation is delegated to driftc (single source of truth).

**No change** to `bin/driftc` / `bin/drift` wrappers: the env-incompatibility
guard there (asan vs memcheck/massif) still fires correctly, and the runner-only
memcheck rejection is unaffected.

---

## Tests

- `lang/tests/driver/test_driftc_wrapper_env_modes.py`:
  - `--sanitize=address` adds `-fsanitize=address` to the link (parallel to the
    existing env-driven `test_driftc_wrapper_asan_adds_sanitize_flags`).
  - flag-wins: `--sanitize=none` with `DRIFT_ASAN=1` in env → no
    `-fsanitize=address`.
  - `--sanitize=undefined` adds `-fsanitize=undefined`.
  - unknown token → exit 2 (usage), helpful message.
- `tools/drift_deploy/test_build.py`: `build_app_cmd` / run forwards
  `--sanitize` into the driftc cmd list.
- `driftc --help` lists `--sanitize` (covered implicitly; add an assertion if a
  help-surface test exists).

## Out of scope (decouple, separate RFCs)

- Promoting drift-web's generic job-runner + plan format into the toolchain
  alongside `flocker` — that's its own design review (ownership, format
  stability, overlap with `tools/pytest_jobs.py` and `flocker`). The
  `--sanitize` flag stands alone and does not depend on it.
- `tsan`/`msan` runtime-archive variants — the token vocabulary is forward-
  compatible, but new variants need their own archive build wiring.

## Verification

- Build a tiny program `driftc --sanitize=address -o bin x.drift`; assert it
  links (`-fsanitize=address` present) and the `asan` runtime variant is used.
- `drift build --sanitize=address` end-to-end forwards and produces an asan
  binary.
- Existing `just ownership-matrix-asan` (env path) still works unchanged.
- No version/ABI bump: pure driver/CLI surface addition, no
  compiler/runtime-boundary shape change. (`DRIFT_RT_ABI_VERSION` unchanged.)
