# Progress — toolchain-meta-stamps

- [x] 2026-07-31 Slice opened: PLAN.md drafted from the drift-query proposal + maintainer design session (`--meta` channel, reserved vs convention keys). D1/D2/D3 pending ratification; W1-W5 not started. Awaiting branch creation by user (mirrors work-dir name).
- [x] 2026-07-31 Pre-review verification pass (claims a reviewer would
      drill into, checked against the tree): (1) ALL eight reserved
      fields are compiler-internal today — versions.py constants,
      env-derived _resolve_build_profile, _toolchain_git_sha build
      stamp; NONE caller-supplied, so the reserved/hard-error design
      holds. (2) Materialization point located and named in PLAN §2.5:
      provenance channel through _emit_codegen → lower_module_to_llvm
      (driftc.py:1582-1631); --meta rides it as provenance_meta.
      (3) Package-mode answered: stamps are codegen-time; .dmp payload
      untouched, no format bump; unstamped = any --meta-less codegen
      invocation. PLAN ready for review.
- [x] 2026-07-31 PLAN v2 — team-review revisions folded in, maintainer-
      concurred: (1) `deps` COMPILER-DERIVED from the effective --dep
      set and reserved (caller-supplied dep metadata = expressible skew,
      the exact class this kills); (2) `artifact_version` via dedicated
      --artifact-version input, reserved stamp; --meta = application-
      defined fields only; (3) lossless encoding contract — percent-
      escaping (%, |, control; names additionally , and @), version =
      strict semver so entry parse is rsplit('@',1); (4) meta_v
      mandatory, compiler-reserved, first in ordering; (5) std.cli
      parse() BYTE-UNCHANGED and policy-free; additive opt-in
      parse_with_builtins() owns --help/--version (stamped default /
      unstamped fallback / explicit override / terminal semantics, five
      pinned behaviors); orchestrator gets NO pool-wide stdout shape —
      gate-grade surface is the stamps. D1 (--meta) and D2 (std.meta)
      RATIFIED; D3 resolved as mandatory meta_v.
- [x] 2026-07-31 PLAN v3 — second team round ratified, maintainer-
      concurred: (1) manifest ARTIFACT IDENTITY allowlist stamped
      (name/version/description/license via dedicated --artifact-*
      flags; namespaced, never bare `license`; explicit allowlist —
      paths/smoke/assets/author profiles excluded as irrelevant or
      sensitive); (2) machine contract is CANONICAL JSON
      drift-build-info/v1 {format, toolchain, artifact, dependencies,
      extra} — compiler assembles, toolchain+dependencies never caller-
      settable, extra isolates --meta, JSON escaping deletes the whole
      v2 percent-escaping apparatus; (3) NO BACKWARD COMPAT:
      compiler_info()/compiler_info_pairs()/CompilerTag REPLACED by
      build_info() + typed accessors + DependencyVersion; pipe grammar
      retired from BOTH --version outputs (driftc/drift) with
      --version --json added; all in-tree callers/fixtures/deploy-
      tooling parsers migrate in the same change; release notes must
      carry the explicit compat-break callout; (4) format discriminator
      mandatory (supersedes meta_v); (5) app display name =
      artifact.name default + std.cli override, manifest display_name
      deferred. Source/tooling break, NOT an ABI break — ABI stays 22.
      Corpus note added (std_meta fixture universe changes → reviewed
      promotion). W1 not started.
- [x] 2026-07-31 PLAN v4 — review round on v3 resolved:
      * P1-1 toolchain/build-instance split: word/profile/utc move to a
        `build` section; --version --json emits its own
        drift-toolchain-info/v1 (truthful, no fake build/artifact) on
        BOTH driftc and drift.
      * P1-2 external stamp access: documented `.drift_build_info`
        binary section (framing part of the v1 contract) + supported
        `drift inspect build-info <binary> [--json]` extractor, pinned
        post-link AND post-deploy (deploy must not strip it);
        orchestrator success criterion now backed by a real read path.
      * Artifact input ATOMIC (all four flags or none; unstamped =
        "artifact": null); canonical JSON = repo convention (sort_keys,
        ensure_ascii=False, compact, no bespoke ordering); extra values
        = JSON strings, empty accepted; std.cli precedence via THREE
        explicit modes (stamped default with parser `app` as display
        name always / SIMPLE "<app> <version>" / VERBATIM
        version_output() setter); --artifact-version accepts arbitrary
        non-empty manifest string (no new loader validation — piece 3
        owns version-shape policy); executables-only stamping explicit
        (.dmp keeps existing package/cert metadata identity story);
        W2 coverage bar raised to full compile/link/RUN e2e (new
        lowering-visible intrinsic).
      ABI stance unchanged and reviewer-confirmed: compiler bump, ABI 22.
- [x] 2026-07-31 PLAN v4.1 — W1 APPROVED (reviewer: "I approve
      proceeding with W1", ABI 22 stance re-confirmed). Tightenings
      folded as W1 acceptance criteria:
      * Fail-closed extractor contract in §2.4: full hostile-input
        matrix (missing/duplicate/truncated/oversized section, bad
        framing/UTF-8/JSON/discriminator) → exit 1 + EMPTY stdout +
        stderr diag; bounded max payload; inspected binary NEVER
        executed; --json success = embedded canonical doc + exactly one
        trailing newline; section name/magic/int-width/byte-order
        documented before W1's emitter+reader count as complete.
      * P2 correction: --dep does NOT guarantee M.N.P (driftc.py:9677
        checks only @ + non-empty halves, literal match) — dependency
        versions are now specified as compiler-accepted non-empty exact
        identity strings; no semver validation added (piece 3 owns
        version-shape policy). §2.1/§2.2 claims fixed.
      * All four --artifact-* VALUES must be non-empty (empty value =
        the atomicity hard error).
      * Schema nullability resolved: required keys everywhere, "" when
        a fact is unavailable (e.g. toolchain.git); never null/omitted.
      NEXT: W1 implementation on this branch.
- [x] 2026-07-31 W1 GO confirmed (v4.1 verified by reviewer). Two
      implementation guardrails recorded as binding:
      (G1) dependency stamps derive from the VALIDATED effective pin
      map, never raw args.dep; (G2) `drift inspect` is SELF-CONTAINED —
      parses the binary format directly, no readelf/objdump/execution.
      Framing constants + malformed-input behavior documented and
      pinned before the emitter/reader half of W1 counts as complete.
- [x] 2026-07-31 W1 STAGE A LANDED (flags → stamp → section, end to end):
      * lang/codegen/llvm/build_info.py — NEW pure core, single source
        of truth for emitter AND future reader: schema/framing constants
        (.drift_build_info, magic DRIFTBI\0, u32 LE framing version=1,
        u32 LE length, payload, NUL; cap 1 MiB — all documented in the
        module header per the W1 acceptance criteria),
        assemble_build_info (canonical repo-convention JSON; artifact
        atomicity asserted; deps sorted; ""-for-unavailable),
        frame/unframe (unframe = the full fail-closed matrix),
        parse_artifact_flags / parse_meta_flags (pure, unit-tested).
      * llvm_codegen.py — emit_build_info beside emit_compiler_provenance
        (shares its build_utc: one wall-clock instant per invocation);
        `build_info` intrinsic arm beside compiler_info;
        lower_module_to_llvm gained provenance_build_info and ALWAYS
        emits the stamp (unstamped = artifact null/deps []/extra {}).
      * driftc.py — --artifact-* (atomic, non-empty values) + --meta
        flags; validation FIRST after parse_args via the pure helpers
        (standard diagnostic shape, json+stderr); G1 honored:
        _stamp_dep_pins.update(_version_pins) AFTER --dep validation;
        threaded through BOTH codegen entries — _emit_codegen (package
        path) and compile_to_llvm_ir_for_tests (single-file CLI path;
        first probe caught that this branch, not _emit_codegen, serves
        plain compiles — test now pins both).
      * Tests: lang/tests/codegen/test_build_info_stamp.py 23 passed
        (assembly canonicalization/determinism, hostile-value round
        trip, framing fail-closed matrix incl. UTF-8/JSON/discriminator
        rows, flag helpers, CLI integration extracting + unframing the
        stamp from rendered IR). Regressions: codegen unit 41,
        checker+stage2+fnptr driver 670 — all green.
      * Post-link smoke: stamped binary links, RUNS rc 0, section name
        + framed magic present in the executable, stamp round-trips
        from the raw binary bytes (artifact + extra intact).
      REMAINING IN W1: --version de-piping + --version --json
      (drift-toolchain-info/v1) on both CLIs + pipe-parser sweep.
- [x] 2026-07-31 STAGE-A REVIEW ROUND 2 + FRAMELESS SUPERSESSION landed:
      * Module moved to backend-neutral lang/driftc/build_info.py
        (codegen imports function-locally; LLVM only places opaque
        bytes).
      * BuildInfoError: frame/cap violations now surface as normal CLI
        diagnostics at BOTH codegen boundaries (package + single-file
        paths), plain and --json — pinned with 10×115KiB --meta args
        (under per-arg execve limit, over the 1 MiB cap), no-traceback
        asserted.
      * Reader hardening: full v1 schema validation
        (validate_build_info_doc — exact keys/types per section,
        non-empty artifact identity, name-sorted dep records) +
        CANONICAL-encoding enforcement via re-serialization compare
        (also kills duplicate keys and trailing/leading bytes).
      * v4.2 FRAMELESS section contract (team decision, no compat):
        .drift_build_info contains EXACTLY the canonical JSON — magic/
        framing-version/length/NUL deleted (the section header is the
        framing; `format` is the schema version). frame/unframe →
        check_payload_size / validate_build_info_payload; cap enforced
        BEFORE decode on the read side and at emit.
      * DRIFTC_VERSION → 0.33.93 (review gate: this behavior must not
        ride 0.33.92).
      * G1 CLI-integration pin recorded as explicit BLOCKING W3 test
        (real package consume → dep pins visible in extracted stamp).
      * Tests: test_build_info_stamp.py 26 passed (payload-validation
        matrix, schema matrix, canonicality/dup-key, oversized CLI both
        modes); codegen unit 44 passed; post-link smoke on the
        FRAMELESS binary: runs rc 0, section present, payload located
        and validate_build_info_payload-verified from raw bytes.
      REMAINING IN W1: --version de-piping + --version --json on both
      CLIs + pipe-parser sweep.
- [x] 2026-07-31 Maintainer design ratification: frameless model
      CONFIRMED preferred (simplicity, bintools reuse, minimal LLVM
      involvement). Verified live: objcopy --only-section dumps
      parseable JSON; readelf -p shows the clean document. PLAN §2.4
      records bintools compatibility as a contract feature + the W3
      reader-shape option (default: self-contained per G2; alternative:
      validator over objcopy output — maintainer to pick at W3).
- [x] 2026-07-31 STAGE-A ROUND-3 FINDINGS CLOSED (32/32 pins):
      * P1 schema strictness: duplicate dependency names rejected
        (sorted-but-equal no longer passes); extra keys must match the
        --meta grammar [a-z0-9_.]+ — canonical documents the compiler
        cannot produce now fail validation. Both pinned.
      * P1 RecursionError: hostile deep-nested sub-cap JSON (200k
        brackets) → BuildInfoError, not a traceback. Pinned.
      * P1 G1 END-TO-END LANDED (pulled forward from W3): real
        emit-package → --package-root/--dep consume (unsigned dev path
        per test_box pattern) → validated pin appears in the extracted
        IR stamp. W3 keeps lane-divergence + post-deploy extraction.
      * P2: encode_build_info wraps UnicodeEncodeError (lone-surrogate
        argv) → normal diagnostic; pinned at unit level AND via raw
        invalid-UTF-8 bytes argv through the CLI (no traceback).
      * Cleanup: residual framed/framing language removed from
        emit_build_info docstring + test module docstring; PLAN header
        now v4.2/implemented.
      NEXT (spec ratified by maintainer): W1 --version half — human
      line "<tool> X (ABI N)" both CLIs, --version --json via the
      shared module (drift-toolchain-info/v1 + one newline), migrate
      ALL machine consumers to --json with fail-closed schema checks,
      DELETE the pipe parser outright, sweep tests/docs/probes/fixtures
      for `|` dependence, pin everything.
- [x] 2026-07-31 W1 --VERSION HALF LANDED (maintainer spec, clean break):
      * Shared module: toolchain_info_json(git_sha) — canonical
        drift-toolchain-info/v1 — and parse_toolchain_info() — the
        fail-closed machine-consumer parse (JSON/discriminator/exact
        keys+types/canonical; BuildInfoError, no fallbacks).
      * driftc --version and drift --version: concise human line
        "<tool> X (ABI N)", no pipe anywhere; --version --json emits
        the canonical document + exactly one newline on BOTH CLIs
        (drift keeps its dev-tree git probe, feeding the json git
        field).
      * MACHINE CONSUMER MIGRATED: drift_deploy._get_compiler_info now
        execs --version --json and hard-fails (DeployError) on run
        failure, nonzero exit, or rejected output — the
        "unknown/abi=0" silent fallback is GONE (provenance records
        real identity or the deploy stops).
      * PIPE PARSER DELETED: provenance.parse_compiler_info removed
        outright (+ unused re import); CompilerInfo docstring updated;
        retirement pinned (module lacks the attr; no "| abi" in
        source).
      * SWEEP: only genuine pipe-pin was
        test_abi_version_stamp.test_driftc_version_output — migrated
        to human-line-exact + --json leg (license/vendor assertions
        moved to the machine contract). compiler_info grammar tests
        untouched BY DESIGN (intrinsic payload = W2's migration).
        Other "--version" hits were unrelated (cert/author CLIs' own
        version args, pytest --version).
      * Pins: tools/drift_deploy/test_toolchain_version.py 13 (both
        CLIs human+json, canonical+single-newline, parse fail-closed
        matrix incl. pipe-era output, deploy consumer json+hard-fail,
        pipe-parser retirement). Affected suites 85 passed; full
        drift_deploy + packages regression in flight.
      W1 COMPLETE pending that regression: flags+stamp+section (stage
      A) and --version break both landed. NEXT: W2 (compiler_info
      family replacement in std.meta + fixture migration).
- [x] 2026-07-31 W1 ROUND-4 FINDINGS CLOSED (33 + 15 pins green):
      * P1 newline: parse_toolchain_info requires EXACTLY one trailing
        newline (rstrip gone); zero and multiple pinned; existing pins
        + deploy-consumer mock updated to carry the newline.
      * P1 quadratic dupes: Counter one-pass reporting (hostile
        many-duplicate sub-cap sections no longer force O(n²)).
      * P2 identity floors: non-empty toolchain.driftc + positive abi
        enforced in BOTH parse_toolchain_info and
        validate_build_info_doc (shared _check_toolchain_identity_
        floors); pinned in both validators.
      * Cleanup: framed/reframe wording purged from the test module
        docstring + _payload rename; "schema/framing" → "schema/
        section" in llvm_codegen; PLAN header shows the --version half
        LANDED.
      * PLAN §2.4: the objcopy-backed extractor option RETRACTED (G2
        self-contained reader already ratified; binutils = manual
        convenience only).
- [x] 2026-07-31 W1 SIGNED OFF (round-5 mechanical cleanup): last
      framed/unframe/framing wording purged from the test file
      (extractor docstring, test_stamped_compile_emits_raw_json_section
      rename, section-cap comment, schema-class docstring — zero "fram"
      hits remain); PLAN pin count 32→33. 33/33 green. W2 begins.
- [x] 2026-07-31 W2 LANDED (compiler_info family → build_info, all
      guardrails honored):
      * stdlib/std/meta/meta.drift: compiler_info/CompilerTag/
        compiler_info_pairs DELETED; build_info() + toolchain_version/
        runtime_abi/artifact_* (Optional<String>; "" sentinel PRIVATE
        behind _bi_* intrinsics + _optional_field) + DependencyVersion
        + dep_versions() (strict std.json parse, canonical order
        preserved, EVERY parse/shape failure asserts — never a silent
        []). std.meta now imports std.json (no cycle; std.log→std.meta
        dependents verified).
      * codegen: compiler_info arm + emit_compiler_provenance +
        @__drift_compiler_build DELETED; scalar arms (_bi_*) read from
        self._build_info_doc — the parsed SAME assembled document the
        build_info() payload comes from (guardrail: never re-derived
        from flags/constants); build_utc generation moved into
        emit_build_info; _bi_runtime_abi emits an Int literal arm.
      * driftc.py comment refs migrated to build_info.
      * Coverage: meta_probe compile+RUN green first try; e2e
        std_meta_build_info_unstamped (runs through the real runner;
        asserts accessors + v1 discriminator via std.json in-program);
        driver RUN tests — stamped binary (hostile chars through
        accessors), unstamped binary, and dep_versions() at RUNTIME
        via a real package consume (runlib@0.2.5 printed by the
        running binary). Stamped-run coverage lives driver-side BY
        NECESSITY: the e2e compile+run path is in-process and cannot
        pass stamp flags (recorded).
      * Provenance tests migrated (explicitly approved): the four
        pipe-global tests → section contract, incl. a SELF-CONTAINED
        ELF64 section-table reader asserting EXACTLY ONE
        .drift_build_info section by NAME post-link with
        validator-checked content.
      * Totality pins: every @intrinsic in std.meta has a lowering arm;
        compiler_info arm/global/decl asserted GONE.
      * Suites: stamp file 38/38; abi_version_stamp 22/22; e2e
        std.meta/log/json dependents 5/5; checker+stage2+codegen-unit+
        fnptr+toolchain-version 741 passed.
      CORPUS ENUMERATION for the next reviewed promotion: universe
      -2 (std_meta_compiler_info, std_meta_compiler_info_pairs)
      +1 (std_meta_build_info_unstamped); stdlib content changed
      (std.meta rewrite + std.json dependency) → expect a uniform
      stdlib-driven per-fixture counter modal + prehash deltas across
      the corpus, same class as the json._encode_node deletion in the
      0.33.91 promotion.
      REMAINING IN SLICE: W3 (drift build/deploy --artifact-* wiring,
      drift inspect build-info self-contained reader, lane-divergence
      + post-deploy extraction tests), W4 (std.cli parse_with_builtins),
      W5 (docs + history + reply announce).
- [x] 2026-07-31 W2 REVIEW FIXES + json.parse() CLEAN BREAK (team-
      ratified rider on 0.33.93):
      * P1 meta.drift: dep_versions asserts format ==
        drift-build-info/v1 BEFORE consuming dependencies; _dep_record
        asserts each record is an object with EXACTLY two fields
        (as_object().len() == 2). Probe re-verified compile+run.
      * Cleanup: lang/versions.py vendor/license comment migrated off
        meta.compiler_info()/provenance-constant wording.
      * std.json: parse() IS now the strict entry point (== strict():
        duplicate keys rejected, no leading zeros, \u decoded,
        unescaped controls rejected, depth cap). parse_strict DELETED
        (impl + export + doc); the PRIVATE legacy mode deleted wholesale
        (_ParseCtx.legacy field + all five branches: leading-zero
        acceptance, \u rejection, unescaped-control acceptance ×2,
        ctor sites). strict()/permissive()/signed_ir() +
        parse_with_config retained.
      * MIGRATION (all deliberate, diagnostic/empirically-driven — ran
        all 60 json fixtures, 8 failed, each repurposed):
        - duplicate family ×4 → explicit permissive() (their subject is
          node/drop semantics), basic_duplicate_keys ALSO pins parse()
          rejecting with duplicate-key;
        - encode_determinism_duplicate_reencode → permissive();
        - parse_policy §1 flipped to strict pins (number-leading-zero +
          duplicate-key tags on parse());
        - rfc_strings_limits: parse() now DECODES \u and REJECTS the
          unescaped control (two legacy-compat legs flipped);
        - canonical case 13: NO parser produces Number("01") anymore —
          node hand-built via new_object()/JsonNode::Number to keep
          pinning encode_canonical's rejection + path.
        - driver: test_std_json_parser_policy_api parse_strict→parse;
          doc/design/drift-stdlib-spec.md entry-points paragraph
          rewritten;
        - differential oracle: fragment's _ParseCtx ctors dropped the
          legacy field; review-pin hash re-pinned
          (18329796b26c…, from c3714429c75d…) — parity 2/2 green.
      * Suites: 63-fixture e2e sweep green; json drivers (minus the
        serial-only perf gate) + stamp + checker 140 passed/9 skipped.
      * CORPUS: +8 fixture content deltas (the migrated json fixtures)
        on top of W2's -2/+1 universe change + the stdlib-wide modal.
- [x] 2026-07-31 W2 REVIEW ROUND 2 CLOSED:
      * P1 raw-intrinsic runtime: TestRawIntrinsicRuntime — stamped AND
        unstamped binaries CALL build_info() at runtime; the stamped
        runtime string validates AND is BYTE-IDENTICAL to the
        externally extracted .drift_build_info section of the same
        binary (self-contained ELF64 reader, deliberately duplicated
        across test roots until W3's drift inspect); artifact + extra
        fields compared against the flags. 2/2 green.
      * P1 docs: effective-drift "keep-last" bullet and stdlib-spec
        "legacy parse() preserved exactly" paragraph rewritten to the
        four-part strict contract.
      * P2 comments: json.drift "parse() tolerates them" and
        parse_policy fixture "accepted by parse() only" corrected.
      * W5 NOTE (binding): release notes must spell out ALL FOUR
        parse() changes — rejects duplicates, rejects leading zeros,
        DECODES \uXXXX, rejects unescaped controls — PLUS the
        parse_strict() removal with the migration line "replace
        json.parse_strict(x) with json.parse(x)", and state that
        explicit permissive() restores duplicate keep-last ONLY, never
        invalid-JSON forms.
- [x] 2026-07-31 W2 SIGNED OFF (round-3 text fixes): stale stdlib-spec
      string-rules sentence ("Legacy parse() is bug-compatible...")
      DELETED — every policy follows RFC 8259; remaining "legacy" hits
      in the spec are the intentional clean-break statements only. W5
      binding note extended with the parse_strict() removal + the
      "replace json.parse_strict(x) with json.parse(x)" migration
      line. Reviewer confirmed the runtime tests sound (both lanes
      execute the raw intrinsic, canonical validation, named-section
      extraction, exactly-one-section, byte compare). W3 NEXT: drift
      inspect build-info (self-contained ELF walk per G2) +
      build/deploy --artifact-* wiring + lane-divergence and
      post-deploy extraction tests.
- [x] 2026-07-31 W3 CORE LANDED (reader + CLI + wiring; reviewer
      refinements honored):
      * SHARED PRODUCTION READER in lang/driftc/build_info.py:
        read_build_info_section (self-contained ELF64 walk per G2 —
        bounds-checked headers/string table/content, fail-closed on
        non-ELF/class/endian/truncation/out-of-bounds, MISSING section,
        DUPLICATE sections — exactly one is the contract) +
        extract_build_info (read → the EXISTING
        validate_build_info_payload, per the refinement: the extractor
        is centered on the payload validator).
      * drift inspect build-info <binary> [--json]
        (tools/drift_deploy/drift_inspect.py + lang/drift/cli.py
        dispatch + help entry): every failure row exits 1 with EMPTY
        stdout + stderr diag; --json success = the section's exact
        canonical bytes + one newline; default pretty-prints; the
        binary is NEVER executed. End-to-end smoke green.
      * build/deploy wiring: build_package_cmd + build_app_cmd pass the
        four --artifact-* flags atomically from the manifest artifact
        (loader guarantees non-empty; harmless on package-emit —
        stamps only materialize in executable codegen). Argv pinned
        for both builders in test_build.py.
      * DUPLICATED TEST READERS REMOVED per refinement: both
        test_build_info_stamp and test_abi_version_stamp now use the
        shared reader (survives-link test = the same code path drift
        inspect uses; dedup initially swallowed _link_flags_for_lib —
        restored, file 22/22).
      * tools/drift_deploy/test_inspect_build_info.py 9/9: exact-bytes
        --json, pretty default, missing section (objcopy-crafted),
        DUPLICATE section (hand-synthesized minimal ELF64 — objcopy
        refuses to create duplicates), corrupted payload, non-ELF,
        truncated, nonexistent/directory, reader unit matrix.
      * Suites: test_build.py 145; stamp+abi+full drift_deploy 376
        total green after the helper restore.
      REMAINING IN W3: lane-divergence dependency-stamp test (strict
      lock vs certify fresh-resolve visible in stamps when the pool
      moved) + post-deploy extraction pin (drift inspect on a
      deploy-produced binary).
- [x] 2026-07-31 W3 CORE REVIEW FINDINGS CLOSED (24/24 inspect pins):
      * P1 section-type discipline: reader now requires the string
        table to be SHT_STRTAB and .drift_build_info to be
        SHT_PROGBITS, rejects SHF_COMPRESSED, validates EI_VERSION and
        e_ehsize — a hostile SHT_NOBITS section aliasing unrelated
        file bytes can no longer be served.
      * P1 byte-exact --json: sys.stdout.buffer.write(utf8 + b"\n")
        replaces print(); pinned with a Unicode document under
        PYTHONIOENCODING=ascii (exact bytes, rc 0).
      * P1 full CLI hostile matrix: factored _synth_elf synthesizer
        (valid-by-default, one knob per mutation) + 18 parameterized
        CLI rows (missing/duplicate/class/endian/ident-version/table/
        strtab/content bounds/strtab-type/NOBITS/compressed/empty/
        oversized/UTF-8/JSON/discriminator/schema/noncanonical) each
        proving exit 1 + EMPTY stdout + stderr diag; objcopy dependency
        REMOVED from the suite entirely (deterministic, no skips);
        synthetic-valid-ELF success control added.
      * P2: read_bytes() wrapped — reader raises BuildInfoError only
        (pinned incl. an unreadable-permissions file).
      * Cleanup: app-builder stamp comment no longer mentions
        package-emit.
      REMAINING IN W3 (unchanged): lane-divergence dependency-stamp
      test + post-deploy extraction pin.
- [x] 2026-07-31 W3 residuals closed (25/25 inspect pins): pretty mode
      writes through the binary UTF-8 stream too (hostile-encoding pin
      covers BOTH modes); unreadable-file regression now mocks
      Path.read_bytes with PermissionError (deterministic under root/
      CAP_DAC_OVERRIDE); e_ehsize rejection pinned via a new
      synthesizer knob + matrix row (19 hostile rows total).
- [x] 2026-07-31 W3 FINAL GATES LANDED (test_stamp_lane_integration.py
      2/2, confirmed twice):
      * LANE DIVERGENCE on production paths per review: two DISTINCT
        pools model actual movement (strict = only 1.0.0, certify =
        only 1.0.1, snapshot authorizes only 1.0.1 — no
        double-authorization); lock written by REAL drift prepare
        against the strict pool; BOTH lanes resolve through
        drift_build._resolve_deps (strict: lock +
        verify_lock_compatibility; certify: source-rebuild authority
        with the stale lock as evidence); binaries via production
        build_app_cmd; stamps extracted FROM THE BINARIES diverge
        (1.0.0 vs 1.0.1) with identical artifact identity. First
        attempt's hand-built strict graph + both-versions-one-pool
        design REPLACED per review (the initial failure was certify's
        index gate correctly rejecting the unsnapshotted 1.0.0 —
        the gate working as specified).
      * POST-DEPLOY: real stamped build → production smoke
        (_run_baseline_smoke_app executes it) → cert-claim signing
        (_emit_cert_claim_for_artifact, real sidecar) → publish
        (_publish_app copytree) → PUBLIC drift inspect build-info CLI
        reads the PUBLISHED binary; output byte-identical to the
        staged stamp (processing never perturbs the section).
      W3 COMPLETE. W4 (std.cli) + W5 (closeout) remain.
- [x] 2026-07-31 W4 LANDED (std.cli builtins; 8/8 driver pins + 9/9
      std_cli fixtures):
      * cli.drift: ParseOutcome variant (Args/Terminal/Err),
        version_output() verbatim setter (field version_output_v, ctor
        kwargs follow field order — v1 ctor constraint hit and fixed),
        parse_with_builtins() owning --help/--version output + terminal
        semantics; _version_render with the THREE-mode precedence
        (verbatim > simple "<app> <version>" > stamped default block:
        "<app> <artifact.version>" / description / "driftc <v>, abi
        <n>" / "license:" / "deps:" — unstamped fallback "<app>
        (unstamped)" + compiler line); module-level _int_decimal_digits
        (Int→decimal; implement-block free fns aren't bare-callable —
        hoisted); imports std.meta + std.console (no cycle).
      * parse() BYTE-UNCHANGED and pinned behaviorally: plainparse mode
        proves empty stdout + cli-version-requested tag.
      * Pins: one dispatching program, three compiles (unstamped /
        stamped / stamped+real-.dmp-dep) — stamped default block exact,
        deps: line from a real consume, unstamped fallback exact,
        simple mode, verbatim override wins, --help terminal
        (Usage: prefix, exit 0), Err passthrough (unknown-option,
        empty stdout).
      W5 NEXT (revised scope per maintainer): docs/history/migration
      notes/corpus enumeration/closeout gates; /tmp/drift-announce
      release note + DriftQuery reply DEFERRED to certification
      readiness.
- [x] 2026-07-31 W5 DOCS LANDED:
      * doc/history.md: full dated 0.33.93 entry — the stamp (schema,
        inputs, frameless section, .dmp exclusion), the read paths
        (std.meta accessors + drift inspect contract), std.cli
        builtins with the no-pool-stdout-shape caveat, and the THREE
        numbered COMPAT BREAKS with migration lines (compiler_info
        family → build_info; --version pipe grammar → human line +
        --json with fail-closed validation; json.parse strict — all
        FOUR behavior changes + "replace json.parse_strict(x) with
        json.parse(x)" + permissive-scope clarification). Corpus
        enumeration embedded. ABI 22 stated.
      * doc/effective-drift.md: "Version facts" section (stamp is the
        contract / stdout is app policy, typed-accessor-first
        guidance, parse_with_builtins pattern, inspect + --version
        --json machine paths).
      * DEFERRED per maintainer: /tmp/drift-announce release note +
        DriftQuery reply wait for certification readiness.
- [x] 2026-07-31 W5 CLOSEOUT RECORDED: PLAN §6 carries the consolidated
      corpus enumeration (net -1 universe, 8 json content deltas,
      stdlib-wide modal) and the four release gates (run-all-tests,
      reviewed promotion, certification with the three downstream
      migration surfaces, then the deferred announce/reply). SLICE
      IMPLEMENTATION COMPLETE — W1-W5 all landed; commits, gates, and
      promotion remain with the maintainer.
- [x] 2026-07-31 W3/W4 REVIEW ROUND CLOSED (79 pins green in the final
      wave; 26 inspect / 2 gates / 40 stamp / 11 cli-driver + e2e
      fixture ok):
      * P1 bounded reader: read_build_info_section rewritten — never
        loads the whole file (fstat + bounded seek/read with short-read
        detection; section table capped via e_shentsize sanity bound;
        shstrtab capped at 1 MiB; payload cap enforced from the
        section HEADER before any copy — pinned via a new
        oversized-declared-size matrix row with a lying sh_size);
        failure-injection pin re-pointed from Path.read_bytes to
        builtins.open (the reader streams now).
      * P1 version_output(""): presence tracked via
        version_output_set — the empty verbatim override prints
        exactly nothing (pinned driver + e2e).
      * P1 unstamped fallback purity: description/license/deps render
        ONLY in the stamped branch — an unstamped dependency-bearing
        binary prints EXACTLY "<app> (unstamped)" + the compiler line
        (pinned with a 4th fixture binary: deps without stamp).
      * P2: examples/build_info.drift CREATED (compiles+runs; the
        meta.drift doc link is real now); CliError/parse() docstrings
        list cli-version-requested; examples/cli migrated to
        parse_with_builtins (compiles; --version renders through the
        builtins); hand-rolled _int_decimal_digits REPLACED with
        std.format.format_int (one spelling per meaning).
      * Args path pinned at runtime (driver mode + e2e); PLANNED E2E
        COVERAGE ADDED: std_cli_parse_with_builtins fixture (unstamped
        default/simple/verbatim/empty-verbatim/Args/Err in one run;
        stamped modes remain driver-side by recorded necessity — the
        in-process runner cannot pass stamp flags). Real Err tag is
        cli-unknown-option (fixture pins it exactly; driver pin had
        matched by substring).
      * Corpus enumeration updated in PLAN §6 + history: universe now
        NET 0 (-2 +2 with the new cli fixture).
- [x] 2026-07-31 DOC-TRUTHFULNESS ROUND CLOSED (39 pins green: 28
      inspect incl. two NEW hostile rows — oversized e_shentsize +
      lying oversized shstrtab sh_size — and 11 cli driver):
      * P1: CliError docstring enumerates the TWELVE actual stable
        tags (cli-* prefixed); invented names removed.
      * P2: both independent reader caps (section-entry size, string
        table) now represented in the CLI hostile matrix.
      * _version_render docstring states real per-mode newline
        semantics; effective-drift CLI pattern migrated to
        parse_with_builtins (matches examples/cli again); driver
        docstring says four compiles.
- [x] 2026-07-31 PROMOTE-TOOL GAP FIXED (surfaced by THIS slice's
      promotion — the first ever to REMOVE compiled fixtures): apply-
      time attribution re-proof failed RESIDUAL NONZERO on c1_agree
      (+23370 aggregate vs +25408 explained; the 2038 gap == exactly
      the two deleted std_meta fixtures' c1_agree). Root cause:
      attribution modeled added-fixture contributions but never
      SUBTRACTED removed ones. Fix in drift_corpus_promote.py: draft
      records removed_fixture_contributions (from the predecessor
      evidence); verify_attribution re-derives them, compares to the
      approval (backward-compat: absent field == {}), and SUBTRACTS in
      the residual; call site passes compiled_removed; baseline_md
      attribution mentions withdrawals. Teeth: 46/46 — removal world
      round-trips draft→approve→apply, and a facts-tamper (dropping
      the removed entry — the pre-fix bug shape) fails closed.
      Also corrected my own verification failure: the earlier "dry-run
      passes" claim had tailed 2 lines and missed the attribution
      FAIL — this round's dry-run was checked in FULL with rc.
      RECORD REGENERATED (stale pre-fix record deleted):
      0.33.93-build-info-stamps drafted with removed contribs
      (std_meta_compiler_info 1035 + _pairs 1003 c1_agree), full
      dry-run rc 0, attribution OK "2 new / 2 removed fixtures,
      residual ZERO". Awaiting re-approval by rename + --apply.
