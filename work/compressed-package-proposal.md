# Compressed Package Distribution (.zdmp)

Investigation and design proposal for first-class compressed package distribution.

## 1. Representative package sizes and compression payoff

| Package | Raw .dmp | zstd -3 (default) | Ratio | zstd -19 (max) | Ratio |
|---------|----------|-------------------|-------|----------------|-------|
| std | 22.9 MB | 984 KB | 23.8x | 506 KB | 46.3x |
| web-rest | 12.6 MB | 301 KB | 42.9x | 134 KB | 96.4x |
| net-tls | 6.6 MB | 198 KB | 34.2x | 72 KB | 94.7x |
| web-jwt | 5.1 MB | 98 KB | 54.1x | 64 KB | 83.0x |
| repro_pkg (small) | 1.4 MB | 54 KB | 27.5x | 38 KB | 39.6x |

Observations:

- Every package compresses to 1-4% of raw size at default zstd.
- zstd -19 roughly halves the default result but is much slower to compress. The default level already captures the dominant win.
- Even the smallest package (1.4 MB) compresses to 54 KB — a 27x reduction.
- A shared library root with std + net-tls + web-jwt + web-rest goes from ~47 MB to ~1.6 MB at default zstd.

## 2. What dominates size

The DMIR-PKG v0 container has three regions:

| Region | Typical share | Notes |
|--------|--------------|-------|
| Header + manifest + TOC | <0.2% | Binary header (160 B), JSON manifest (~27 KB for std), TOC (80 B × blob count) |
| Interface blobs (type: exports) | ~2% | Per-module export metadata, JSON-encoded |
| Payload blobs (type: dmir) | ~98% | Full MIR, type tables, signatures, schemas — JSON-serialized via `provisional_dmir_v0.py` |

The payload blobs dominate overwhelmingly. They are JSON-serialized compiler IR (`_to_jsonable()` converts dataclasses, enums, and MIR instructions to nested JSON objects). This produces highly repetitive text with massive key-name redundancy (`"_type"`, `"_enum"`, `"name"`, `"args"`, etc.), which is exactly what dictionary-based compressors like zstd excel at.

Top modules by payload size in std.dmp:

| Module | Payload | Interface |
|--------|---------|-----------|
| std.containers | 2.63 MB | 60 KB |
| std.crypto | 1.45 MB | 11 KB |
| std.regex | 1.10 MB | 18 KB |
| std.concurrent | 1.06 MB | 31 KB |
| std.codec | 1.04 MB | 13 KB |

The compressibility is an inherent property of JSON-serialized IR and will remain excellent regardless of package content. Even a hypothetical future binary IR format would likely compress well due to repeated opcode/type patterns, though at lower ratios.

## 3. Format and tooling model

### 3.1 Published artifact pair

```
<package>.zdmp     # zstd-compressed DMIR-PKG v0 container
<package>.sig      # Ed25519 signature sidecar (JSON, uncompressed)
```

The `.zdmp` file is the standard published form. Raw `.dmp` is a build intermediate — never published, never stored in package roots.

### 3.2 Signature semantics (pinned)

The signature covers the canonical uncompressed DMIR-PKG v0 byte stream. This is an internal verification rule. The public-facing artifact pair is `.zdmp` + `.sig`.

Rationale: signing the uncompressed payload ensures that signature validity is independent of compression parameters, algorithm version, or transport encoding. The uncompressed bytes are the canonical identity of the package.

### 3.3 Container format version

The DMIR-PKG v0 container format does not change. The `.zdmp` file is:

```
zstd_frame( dmir_pkg_v0_bytes )
```

No additional framing, no custom headers, no envelope. A `.zdmp` is a standard zstd frame whose decompressed content is a valid DMIR-PKG v0 container. This means `zstd -d net-tls.zdmp -o net-tls.dmp` produces a valid `.dmp` for debugging.

### 3.4 Package format version indicator

The manifest field `"format_version": 0` remains unchanged. The compression wrapper is orthogonal to the container format version — it is a distribution-layer concern, not a container-layer concern. Discovery code distinguishes `.zdmp` from `.dmp` by file extension, not by manifest content.

### 3.5 Producer flow

```
driftc --emit-package ...
  → writes <name>.dmp (build intermediate, in build dir)

drift deploy (or signing tool):
  1. Read raw .dmp bytes
  2. Sign: Ed25519(uncompressed_bytes) → .sig sidecar
  3. Compress: zstd(uncompressed_bytes) → .zdmp
  4. Publish: copy .zdmp + .sig to destination
  5. Raw .dmp is not published
```

The signing step MUST happen before or concurrently with compression — the signer needs the uncompressed bytes. The `.sig` references `sha256(uncompressed_bytes)`.

### 3.6 Consumer flow (driftc --package-root)

```
1. discover_package_files() finds *.zdmp files in package roots
2. For each .zdmp:
   a. Check local decompressed cache (see §4)
   b. If cache hit: load .dmp from cache, verify sha256 matches .sig
   c. If cache miss:
      - Decompress .zdmp → raw bytes (in memory or to cache)
      - Verify sha256(raw_bytes) matches .sig sidecar
      - Verify Ed25519 signature against raw_bytes
      - Write raw bytes to cache as .dmp
      - Load package from cached .dmp
3. Proceed with type table linking, MIR merge, codegen
```

### 3.7 Deploy resolver flow

`build_package_index()` currently reads manifests from `.dmp` files. With `.zdmp`:

```
1. Walk package roots, find *.zdmp files
2. For each .zdmp:
   a. Check manifest cache (lightweight: just manifest JSON, not full decompression)
   b. If cache miss: decompress, extract manifest, cache manifest
   c. Index by package_id + version
3. Resolution proceeds as before
4. Integrity field in lock file uses sha256 of uncompressed bytes
   (same canonical identity that signatures reference)
```

The resolver only needs the manifest for indexing — it does not need full decompression. A manifest-only cache avoids decompressing packages that are never selected by resolution.

### 3.8 Discovery: extension-based, not guessing

Package discovery accepts `.zdmp` files. The file extension is authoritative:

- `.zdmp` → zstd-compressed DMIR-PKG v0
- `.dmp` → raw DMIR-PKG v0 (build intermediate only, not published)

There is no magic-byte sniffing, no "try both" heuristic. If a file has the wrong extension, it is an error.

During transition (if needed), discovery could accept both extensions with `.zdmp` taking priority when both exist for the same package. Post-transition, `.dmp` files in package roots are ignored or warned.

## 4. Local decompressed cache

### 4.1 Purpose

Repeated compiler invocations (edit-compile-test cycles, CI builds) should not pay decompression cost on every run. The cache stores verified, decompressed `.dmp` files keyed by content identity.

### 4.2 Cache location

```
$DRIFT_CACHE_DIR/pkg/v0/          # default: ~/.cache/drift/pkg/v0/
  <sha256_hex>.dmp                 # decompressed package, named by content hash
```

`$DRIFT_CACHE_DIR` defaults to `~/.cache/drift` (XDG-compatible). Overridable by environment variable.

### 4.3 Cache key

The cache key is `sha256(uncompressed_bytes)` — the same value used in the `.sig` sidecar's `package_sha256` field and in lock file integrity.

This means:
- Cache validity is self-verifying: `sha256(cached_file) == filename`
- No separate metadata files needed
- Cache is content-addressed: identical packages from different sources share the same cache entry
- Cache invalidation is trivial: delete the file, or delete the whole directory

### 4.4 Cache population

```python
def _cache_path_for(sha_hex: str) -> Path:
    cache_dir = Path(os.environ.get("DRIFT_CACHE_DIR", Path.home() / ".cache" / "drift"))
    return cache_dir / "pkg" / "v0" / f"{sha_hex}.dmp"

def load_zdmp_cached(zdmp_path: Path, sig_path: Path) -> tuple[bytes, Path]:
    """Load a .zdmp, using cache if available. Returns (raw_bytes, cache_path)."""
    # 1. Read .sig to get expected sha256.
    sig = load_sig_sidecar(sig_path)
    expected_sha = sig.package_sha256_hex

    # 2. Check cache.
    cached = _cache_path_for(expected_sha)
    if cached.exists():
        raw = cached.read_bytes()
        if sha256_hex(raw) == expected_sha:
            return raw, cached
        # Cache corruption — fall through to decompress.
        cached.unlink()

    # 3. Decompress.
    import zstandard
    compressed = zdmp_path.read_bytes()
    raw = zstandard.ZstdDecompressor().decompress(compressed)

    # 4. Verify.
    actual_sha = sha256_hex(raw)
    if actual_sha != expected_sha:
        raise ValueError(f"zdmp content hash mismatch: expected {expected_sha}, got {actual_sha}")

    # 5. Populate cache.
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(raw)
    return raw, cached
```

### 4.5 Cache lifetime

- No automatic expiration. Cache entries are valid forever (content-addressed).
- `drift cache clean` (or manual `rm -rf ~/.cache/drift/pkg/`) clears the cache.
- CI environments may pre-warm the cache or use a shared cache directory.
- Cache is strictly a performance optimization — correctness never depends on it.

### 4.6 Unsigned packages

During development, packages may be unsigned (no `.sig` file). In that case:
- Cache key is computed from the decompressed bytes directly (`sha256(decompress(zdmp))`)
- No signature verification step
- Same cache directory, same content-addressed scheme

## 5. Algorithm choice

### 5.1 zstd is the right default

| Property | zstd | gzip | lz4 | brotli |
|----------|------|------|-----|--------|
| Compression ratio (this workload) | 24-54x at default | ~10-15x | ~5-8x | ~25-50x |
| Decompression speed | Very fast (~1 GB/s) | Moderate | Fastest | Slow |
| Ecosystem support | Excellent (C lib, Python, Rust, Go) | Universal | Good | Good |
| Deterministic output | Yes (same version, same level, same input → same output) | Yes | Yes | Yes |
| Streaming support | Yes (frame format) | Yes | Yes | No (must buffer) |
| Dictionary support | Yes (for future use with small packages) | No | Yes | Yes |

zstd is the clear winner: best ratio on this workload, fastest decompression, deterministic output, and universal library availability. There is no reason to consider alternatives.

### 5.2 Compression settings

```
Algorithm:  zstd
Level:      3 (default)
Options:    --content-size (embed decompressed size in frame header)
            --single-thread (determinism: no worker thread variation)
```

**Level 3 rationale**: The compression ratio difference between level 3 and level 19 is ~2x (e.g. 984 KB vs 506 KB for std), but compression time at level 19 is ~50x slower. Level 3 already achieves 24-54x compression. The remaining factor of 2 is not worth the compression time penalty in the deploy pipeline.

**`--content-size` rationale**: Embedding the decompressed size allows the decompressor to allocate exactly once, avoiding realloc during streaming decompression. This is a ~10-20% decompression speed improvement for free.

**`--single-thread` rationale**: Multi-threaded zstd can produce different output depending on thread scheduling. Single-threaded compression is deterministic across runs and platforms. This matters because the compressed bytes must be reproducible for build verification (though the signature covers the uncompressed bytes, reproducible compressed output simplifies debugging and mirroring).

### 5.3 Python library

Use `zstandard` (python-zstandard by Gregory Szorc). It bundles the C library and is the standard Python binding.

```python
import zstandard

def compress_zdmp(raw_bytes: bytes) -> bytes:
    cctx = zstandard.ZstdCompressor(level=3, write_content_size=True, threads=0)
    return cctx.compress(raw_bytes)

def decompress_zdmp(compressed: bytes) -> bytes:
    dctx = zstandard.ZstdDecompressor()
    return dctx.decompress(compressed)
```

`threads=0` means single-threaded (no worker pool). `write_content_size=True` embeds decompressed size.

## 6. Migration path

### 6.1 Format version

This is not a new package format version. It is a distribution-layer change. The DMIR-PKG v0 container is unchanged. The `.zdmp` extension and compression are a transport/storage concern.

### 6.2 Transition strategy

**Phase 1: Dual-accept** (one release)
- Producer emits `.zdmp` + `.sig` (compressed is the published form)
- Consumer accepts both `.zdmp` and `.dmp` in package roots
- `.zdmp` takes priority when both exist for the same package
- Deploy tool publishes `.zdmp` + `.sig`

**Phase 2: zdmp-only** (subsequent release)
- `.dmp` files in package roots produce a warning
- All tooling assumes `.zdmp` as the standard form
- Raw `.dmp` is only a build intermediate in the build directory

### 6.3 Signature sidecar naming

Current: `<name>.dmp.sig` (sidecar names the file it covers)
New: `<name>.sig` (companion to `<name>.zdmp`)

The `.sig` file content is unchanged — it still references `sha256(uncompressed_bytes)`. Only the filename convention changes.

## 7. Touched files (implementation scope)

| File | Change |
|------|--------|
| `tools/drift_deploy/drift_deploy.py` | Compress after signing, publish `.zdmp` + `.sig` |
| `lang/driftc/packages/provider_v0.py` | `discover_package_files` accepts `.zdmp`; cache-aware loading |
| `lang/driftc/packages/signature_v0.py` | Sig sidecar lookup uses `.sig` (not `.dmp.sig`) |
| `tools/drift_deploy/resolver.py` | `build_package_index` discovers `.zdmp`, decompresses for manifest |
| New: `lang/driftc/packages/zdmp_cache.py` | Cache read/write/clean logic |
| `lang/driftc/driftc.py` | `--package-root` loading path uses cache-aware loader |

The compiler's `--emit-package` output remains `.dmp` (build intermediate). Compression is the deploy/publish tool's responsibility, not the compiler's.

## 8. Summary

| Aspect | Decision |
|--------|----------|
| Published form | `<name>.zdmp` + `<name>.sig` |
| Algorithm | zstd level 3, single-threaded, content-size embedded |
| Signature covers | Canonical uncompressed DMIR-PKG v0 bytes |
| Cache location | `~/.cache/drift/pkg/v0/<sha256>.dmp` |
| Cache key | `sha256(uncompressed_bytes)` (same as sig reference) |
| Format version | No change — DMIR-PKG v0 container is unchanged |
| Determinism | Same input → same compressed output (single-thread, fixed level) |
| Expected savings | 24-54x at default level (97-98% size reduction) |
