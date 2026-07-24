# B-repr(B5) — RcBytes String representation, ABI 21→22 (DESIGN CHECKPOINT, REPORT-ONLY)

Date: 2026-07-23.  Status: **DESIGN ONLY — STOP at end for review; no
implementation.**  Baseline authority: the CURRENT post-Phase-D mainline —
**0.33.87 / ABI 21, certified and deployed 2026-07-23** (pool run
`20260723-120948-drift-lang-3d48b7f`).  `string_arc.py` is deleted; the
ownership pipeline is: frozen `CleanupPlan` at the plan slot →
`ownership_normalization` (R1/R5/R8) → `return_cleanup_emitter` →
`overwrite_cleanup`; `string_stakes`/`string_releases` author stakes and
last-use releases upstream.  B5 touches NONE of that authority
architecture — it is a pure representation cutover.  Older Scope-B ABI
numbers ("20→21", `libdrift_rt_abi21.a`) in `SCOPE-B-PLAN.md` are
HISTORICAL; B5 is **ABI 21 → 22**.  The §10.2.1 maintainer pinning
(2026-07-15) is the semantic base; this checkpoint refines it to exact
layouts, rules, inventories, and sequencing.

Execution shape (per direction): ONE coordinated branch, ONE ABI-22
certification at the end — no independently certified micro-slices.

---

## 0. BINDING DECISIONS (maintainer, 2026-07-23) — normative overlay

The ten decisions below are BINDING and override any conflicting earlier
prose in this checkpoint (conflicts are corrected inline and noted):

1. **Release target: compiler 0.33.88, runtime ABI 22.**  No ABI-21
   compatibility shim, no dual-layout runtime; ALL dependent artifacts
   rebuild.  (Supersedes the earlier 0.34.0 proposal; §9(a) is RESOLVED.)
2. **Handle/allocation**: two-word internal ABI handle
   `{ drift_isize len; DriftRcBytes *storage; }`; `storage` → 16-byte
   offset-zero header followed by EXACTLY `len + 1` bytes.  The
   language-level length type is PRESERVED (no signedness/API change
   rides B5); `len >= 0` is an invariant, enforced not retyped.
3. **Header**: `_Atomic uint64_t strong` + `_Atomic uint64_t flags` +
   byte tail.  Flags are ATOMIC because interior-NUL knowledge is cached
   lazily on immutable allocations shared across threads.  Exact bits,
   unknown state, mutual exclusion, orderings, refcount overflow/
   underflow, size/alignment/offsets pinned in §2.3–2.4.
4. **Empty vs zero state (CORRECTED same-day)**: source-level empty
   String canonicalizes to the immortal static empty singleton (len 0,
   non-null NUL pointer, C-string conversion succeeds).  The all-zero
   `{0, NULL}` remains ONLY a drop-safe internal TOMBSTONE — a
   drop-only sentinel, NOT a second valid empty representation:
   `release`/`free`/drop accept it as a no-op (zero-storage drop
   safety), but ALL value observation — C `len`/`data`, comparison,
   hashing, concatenation, C-string borrowing, and (pending the §2.6
   path audit) `retain` — FAILS CLOSED with a runtime contract failure
   rather than silently reading as "".  Masking a compiler
   use-after-move as empty is the failure mode this forbids.
   `{len != 0, NULL}` and negative lengths always fail closed.  B5 must
   prove no legitimate program passes a tombstone to an observation
   boundary.  (The earlier "retire `{0, NULL}`" claim AND the earlier
   "accessors treat tombstone as empty" draft are both superseded by
   §2.6.)
5. **Static storage**: empty-singleton symbol/linkage and the exact
   LLVM/C literal layout are pinned (§2.6–2.7); literal flags include
   STATIC + compile-time interior-NUL knowledge.  Pointer identity is
   NOT String semantics.
6. **C accessors**: exact signatures in §3.1, read-only pointers.  ONLY
   the String runtime and the compiler's three-lowering layout authority
   (literal emitters, `StringByteAt`, the `string_bytes_base` intrinsic)
   may inspect storage/header layout; every other C consumer uses
   accessors, enforced by a source audit (§3.1).
7. **C-string ownership**: `drift_string_to_cstr` is NOT changed from
   allocating/owned to borrowed under the same name — its disposition
   is pinned explicitly (§3.3).  Borrowed callback APIs and allocated
   handoff APIs have distinct names and documented lifetimes/free
   functions.
8. **Drift API**: exact signatures + effect/lifetime behavior for
   `with_bytes`, checked `with_cstr{1..4}`, unsafe no-scan variants,
   `CStringScope`, `OwnedCStr`, `OwnedCBytes` (§3.2–3.4).  Checked
   multi-argument helpers validate LEFT-TO-RIGHT and report BOTH the
   argument ordinal and the byte index; borrowed pointers MAY
   syntactically escape (Ptr is Copy) but become INVALID when the
   callback returns — use afterward is unsafe (§3.2; no new lifetime
   feature rides B5).
9. **String semantics**: interior NUL remains legal; B5 adds NO UTF-8
   validation and does not change accepted byte content.  Constructor
   behavior for NULL input, negative length, allocation overflow, OOM,
   and `len + header + 1` overflow is pinned in §2.5 — invalid inputs
   NEVER silently become empty.
10. **Scope closure**: every API promised by this plan either ships in
    the ABI-22 chunk or is explicitly struck during design review — no
    undefined follow-on String phase (§10 table).

---

## 1. Current representation (B0, as-built — verified against 0.33.87)

**Handle** (`string_runtime.h:10`): `DriftString { drift_isize len; char *data; }`
— two words, by value everywhere (all ~15 helpers take/return by value).
**Heap block** (`string_runtime.c:56-72`):
`DriftStringHeader { _Atomic uint64_t refcount; uint64_t flags; }` (16 B,
`_Static_assert`ed size/offset), located BEHIND the data pointer
(`header = (DriftStringHeader*)(data - 16)`); bytes follow the header;
ctors write a trailing NUL.  **Flags**: `DRIFT_STRING_FLAG_STATIC = 1`
only.  **Alloc** (`drift_string_alloc`): `malloc(16 + len + 1)`, rc=1,
flags=0; `len < 0` → NULL; malloc failure → `abort()`; NO explicit
overflow guard on `16 + len + 1`.  **Empty string**: `{0, NULL}` special
case, branched in every helper.  **Retain/release**: `data == NULL` →
no-op (both); static flag → no-op; heap → explicit
`fetch_add(relaxed)` / `fetch_sub(release)` + acquire fence before
free; release UNDERFLOW (`prev == 0`) aborts UNCONDITIONALLY;
`DRIFT_STR_TRACE` diagnostic hooks.  **Static literals** (codegen
`_lower_const_string` / `_emit_string_literal_value`,
`llvm_codegen.py:4012/4040`): private constant
`{ i64 1 /*rc*/, i64 1 /*flags=STATIC*/, [N+1 x i8] c"…\00" }`, handle =
`{len, GEP(field 2) /*→bytes*/}`; per-module literal cache.  **LLVM**:
`%DriftString = type { i64, ptr }` (`DRIFT_STRING_TYPE`,
llvm_codegen.py:220).  **C conventions**: by-value Convention A
(caller-transferred stake, `DRIFT_OWNED_STRING` cleanup macro) /
Convention B (borrowed pass-through, allow-marker), enforced by
`test_drift_owned_string_audit.py`.  **ABI guard**: link-time versioned
symbol `__drift_rt_abi_version_21` (compiler emits a required reference,
`abi_version_stamp.c`; driver prints a hint on unresolved-symbol link
failure, driftc.py:14380), regression-pinned by
`test_abi_version_mismatch_link_failure` + `test_abi_mismatch_driver_hint`.

## 2. B5 target design — exact layouts

### 2.1 Native (Drift language) model — unchanged semantics
```
pub final type String {            // compiler/runtime-specialized
    len: <current length type>     // byte length, inline (hot field) —
                                   // decision 2: the language-level
                                   // length type is UNCHANGED by B5
                                   // (len >= 0 invariant; no signedness
                                   // or API change rides this chunk)
    storage: RcBytes               // refcounted immutable byte block
}
internal final type RcBytes {
    strong: AtomicUint
    flags:  RcBytesFlags
    bytes:  [Byte]                 // tail payload, ALWAYS followed by one hidden NUL
}
```
Source-visible semantics are IDENTICAL to today: String stays immutable
UTF-8, Copy = retain-copy, Drop = release, no SSO, no views, no interning
change; mutation stays in a future `StringBuilder`.  Native code observes
operations, never fields.

### 2.2 Runtime-C layout
```c
typedef struct DriftRcBytes {
        _Atomic uint64_t strong;       /* offset 0 */
        _Atomic uint64_t flags;        /* offset 8 — ATOMIC (decision 3):
                                          interior-NUL knowledge is cached
                                          lazily on immutable blocks shared
                                          across threads */
        /* unsigned char bytes[];         offset 16 (tail), EXACTLY len+1
                                          bytes; bytes[len] == 0 */
} DriftRcBytes;
_Static_assert(sizeof(DriftRcBytes) == 16, ...);
_Static_assert(_Alignof(DriftRcBytes) == 8, ...);
_Static_assert(offsetof(DriftRcBytes, strong) == 0, ...);
_Static_assert(offsetof(DriftRcBytes, flags) == 8, ...);

typedef struct DriftString {
        drift_isize   len;             /* inline byte length (excludes hidden NUL) */
        DriftRcBytes *storage;         /* -> header at OFFSET 0; bytes at +16 */
} DriftString;
```
Handle stays TWO WORDS by value — every helper signature keeps its shape
(the ABI still breaks: the pointer's meaning and the block layout change,
which is exactly what the version stamp exists for).  The
`data-16` behind-the-pointer aliasing trick is RETIRED.
`bytes(s) = (char*)(s.storage + 1)`; C reaches bytes ONLY via accessors
(§3).  Storage is exact (no offset/slice inside the handle).

### 2.3 `RcBytesFlags` (u64, conservative start; all other bits reserved-zero)
| bit | name | semantics |
|---|---|---|
| 0 | `STATIC` | retain/release are no-ops; block is compiler- or runtime-emitted immortal data |
| 1 | `IMMORTAL` | runtime-owned never-freed block (the empty singleton, future runtime immortals); retain/release no-ops.  MUTUALLY EXCLUSIVE with STATIC (ruling): compiler literals use STATIC only; the singleton uses IMMORTAL only; both bits set = UNCONDITIONAL `drift_contract_fail` (§2.4 validate).  Distinct so tooling can tell compiler rodata from runtime immortals |
| 2 | `NUL_SCANNED` | interior-NUL cache is VALID (bit 3 meaningful) |
| 3 | `HAS_INTERIOR_NUL` | valid only under bit 2: some `bytes[i] == 0` for `i < len` |

Pinned state machine (decision 3):
* **Unknown state**: bits (2,3) = (0,0) — interior-NUL not scanned;
  readers must scan (and may cache).
* **Scanned states**: (1,0) = scanned, NO interior NUL — the zero-copy
  `with_cstr` fast path; (1,1) = scanned, HAS interior NUL.
* **(0,1) is ILLEGAL** (mutual exclusion: bit 3 is meaningful only
  under bit 2) — observing it is an UNCONDITIONAL `drift_contract_fail`
  (§2.4 validate; never NDEBUG-gated).
* **Monotonic, write-once**: the only legal transition is one
  `atomic_fetch_or_explicit(&flags, NUL_SCANNED|maybe HAS_INTERIOR_NUL,
  memory_order_relaxed)` from the unknown state; concurrent scanners
  race benignly to identical bits (the bytes are immutable).  Reads use
  `memory_order_relaxed` loads — a stale "unknown" read only costs a
  redundant scan, never correctness.
* **STATIC/IMMORTAL bits are construction-time only** — never mutated;
  STATIC literals get bits 2/3 computed AT COMPILE TIME by the literal
  emitter (the compiler knows the bytes); the runtime empty singleton
  and any runtime immortals are constructed fully-flagged.
* All other bits reserved-zero; a set reserved bit is an UNCONDITIONAL
  `drift_contract_fail` (§2.4 validate).

### 2.4 Retain / release / free
```
/* validate returns BOTH the state and the loaded flags — callers never
 * re-read or hold a stale local (finding 4). */
struct DriftStringCheck { enum { TOMBSTONE, LIVE } state; uint64_t flags; };
validate(s) -> DriftStringCheck:              /* shared prologue, ALL builds */
    if (s.storage == NULL) {
        if (s.len != 0) drift_contract_fail("malformed String handle: nonzero len, NULL storage");
        return { TOMBSTONE, 0 };              /* EXACTLY {0, NULL} */
    }
    if (s.len < 0) drift_contract_fail("malformed String handle: negative len");
    f = atomic_load_explicit(&s.storage->flags, memory_order_relaxed);
    if (f & RESERVED_BITS)                    drift_contract_fail("String flags: reserved bit set");
    if ((f & (STATIC|IMMORTAL)) == (STATIC|IMMORTAL))
                                              drift_contract_fail("String flags: STATIC+IMMORTAL");
    if ((f & (NUL_SCANNED|HAS_INTERIOR_NUL)) == HAS_INTERIOR_NUL)
                                              drift_contract_fail("String flags: HAS_INTERIOR_NUL without NUL_SCANNED");
    return { LIVE, f };
/* Illegal flag combinations and reserved bits go through the
 * UNCONDITIONAL contract path — never NDEBUG-gated. */

release(s): chk = validate(s);
            if (chk.state == TOMBSTONE) return;        /* drop-only no-op */
            if (chk.flags & (STATIC|IMMORTAL)) return;
            prev = atomic_fetch_sub_explicit(&strong, 1, memory_order_release);
            if (prev == 0) abort();                    /* underflow — unconditional, as B0 */
            if (prev == 1) { atomic_thread_fence(memory_order_acquire); free(s.storage); }

retain(s):  chk = validate(s);
            if (chk.state == TOMBSTONE)
                drift_contract_fail("retain of String tombstone");  /* PROPOSED — see §2.4 gate */
            if (chk.flags & (STATIC|IMMORTAL)) return s;
            prev = atomic_fetch_add_explicit(&strong, 1, memory_order_relaxed);
            if (prev >= DRIFT_RC_MAX_LIVE)             /* = UINT64_MAX / 2; >= not > */
                drift_contract_fail("String refcount overflow");
            return s;
```
The ordering protocol is CARRIED VERBATIM from B0 (the current runtime
already uses relaxed increment / release decrement + acquire fence).
Underflow aborts unconditionally (B0 behavior kept).  The overflow
guard is unconditional fail-closed at `prev >= DRIFT_RC_MAX_LIVE` —
it fires ~2^63 increments before any wrap, in normal AND NDEBUG
builds.  Malformed handles (`{len != 0, NULL}`, `len < 0`) fail closed
in EVERY entry point including release — only the exact all-zero
tombstone takes the drop-only no-op.

**`drift_contract_fail` (new runtime primitive — pinned):**
```c
/* string_runtime.h — visible to the inline accessors */
_Noreturn void drift_contract_fail(const char *what);
/* exported runtime symbol; prints "[drift:contract] <what>\n" to
 * stderr and abort()s.  UNCONDITIONAL — identical behavior in the
 * normal and NDEBUG/release runtime variants (tombstone/malformed-
 * handle failures are NEVER debug-only).  Subprocess teeth run against
 * BOTH runtime builds (dual-runtime sentinel infrastructure, §7). */
```

**Tombstone `retain` (open empirical question, maintainer-flagged):**
B0 retain silently no-ops on `{0, NULL}`; whether any LEGITIMATE
compiler path retains a tombstone (e.g. a `CopyValue` stake
materialized on a PATH_DEPENDENT flow over zeroed storage, or an
env-slot zero-back later copied) is unproven either way.  PINNED PLAN:
implement retain-of-tombstone as FAIL-CLOSED, and as a mandatory
implementation-time gate run the full suite + 924 corpus + memcheck
with that trap armed in the staged runtime.  Zero hits → fail-closed
ships.  Any hit → the path is documented, the decision returns to
review (either the path is a compiler bug to fix, or retain's no-op is
restored for that documented class only).  `DRIFT_STR_TRACE` hooks
carry over against the new field names.

### 2.5 Allocation + overflow rules (all pinned, closing today's gap)
```
DRIFT_STRING_MAX_LEN = PTRDIFF_MAX - sizeof(DriftRcBytes) - 1
alloc(len):
  len <  0                    -> abort("negative String length")    [contract]
  len >  DRIFT_STRING_MAX_LEN -> abort("String length overflow")    [new, explicit]
  malloc(16 + len + 1) fail   -> abort                              [as today]
  strong=1, flags=0, bytes[len]=0
concat(a,b): if (b.len > DRIFT_STRING_MAX_LEN - a.len)
                  drift_contract_fail("String concat overflow");
             total = a.len + b.len;
  /* the subtraction form cannot wrap (a.len ≤ MAX); the earlier draft's
     "two valid lengths cannot overflow when added" claim was WRONG and
     is withdrawn — two lengths each ≤ MAX can sum past it. */
from_cstr(p): size_t n = strlen(p);
             if (n > (size_t)DRIFT_STRING_MAX_LEN)
                  drift_contract_fail("String length overflow");
             /* validate BEFORE the size_t → drift_isize conversion */
```
Every constructor (from_cstr/from_utf8_bytes/from_int64/uint64/f64/bool/
concat/literal) reserves EXACTLY `len + 1` tail bytes and writes the
hidden NUL — the zero-copy borrowed-C-string invariant (§3).

**Constructor edge behavior (decision 9 — invalid input NEVER silently
becomes empty; behavioral deltas vs B0 are flagged):**
| input | B5 behavior | B0 today | delta? |
|---|---|---|---|
| `from_cstr(NULL)` | `abort("NULL cstr")` — contract violation | silently returns `{0, NULL}` | **YES — tightening**; every in-tree caller audited in §6.3a (e.g. `env_runtime` guards unset BEFORE constructing; `getenv` NULL results must stay guarded) |
| `from_utf8_bytes(NULL, len>0)` | `abort` | silently returns `{0, NULL}` | **YES — tightening** |
| `from_utf8_bytes(_, 0)` / empty literal / `""` | canonical empty singleton (§2.6) | `{0, NULL}` | representation only |
| `len < 0` | `abort("negative String length")` | returns NULL buffer → `{0, NULL}` | **YES — tightening** |
| `len > DRIFT_STRING_MAX_LEN` (incl. `len + 16 + 1` wrap) | `abort("String length overflow")` | unguarded | **YES — new guard** |
| `malloc` failure (OOM) | `abort` | `abort` | none |
| interior NUL in input bytes | ACCEPTED (legal content; cache bits may record it) | accepted | none — and B5 adds NO UTF-8 validation anywhere (decision 9) |

### 2.6 Empty string vs the reserved zero tombstone (decision 4)

TWO distinct states, both pinned:

**(a) Canonical empty String** — every SOURCE-LEVEL empty value
(`""` literal, empty constructor results, empty concat) resolves to ONE
hidden runtime-owned singleton (ruling: an actual singleton symbol, not
per-module constants — the earlier internal-linkage + per-module-
freedom draft contradicted itself and is withdrawn):
```c
/* string_runtime.c — the DEFINITION (the header carries the extern
 * declaration; shown here as defined) */
struct DriftEmptyString { DriftRcBytes hdr; unsigned char nul[1]; };
__attribute__((visibility("hidden")))
const struct DriftEmptyString __drift_rt_string_empty = {
        { 1, DRIFT_RCBYTES_IMMORTAL | DRIFT_RCBYTES_NUL_SCANNED },
        { 0 } };                                    /* ABI-22 symbol */
DriftString drift_string_empty(void);   /* {0, (DriftRcBytes*)&__drift_rt_string_empty} */
```
`__drift_rt_string_empty` has EXTERNAL linkage with HIDDEN visibility —
part of the ABI-22 runtime archive, referenceable by CODEGEN (empty
literals lower to `{0, @__drift_rt_string_empty}` instead of emitting a
per-module constant) and by the C accessor layer, but not a public API
symbol.  Flags: `IMMORTAL | NUL_SCANNED` (NOT STATIC — mutual
exclusion ruling; compiler rodata literals are the STATIC population).
Pointer identity remains NON-semantic (nothing may compare storage
pointers for equality semantics), but the implementation now has ONE
empty block by construction.

**(b) Reserved internal zero tombstone `{0, NULL}` — a DROP-ONLY
sentinel, NOT a second empty representation (corrected ruling).**
The all-zero handle remains reserved and load-bearing: the ownership
pipeline's zero-storage doctrine writes it via scalar
`ZeroValue(String)` (R1 entry zero-init, R5 zero-backs, Return/
overwrite emitter zero-backs, hir_to_mir env zero-backs) AND via
NESTED zeroing (aggregate `zeroinitializer`, `TombstoneValue`).  B5
does not attempt to replace those paths.  Pinned contract, BOTH
directions:
* `release`/`free`/drop on `{0, NULL}`: NO-OP (checked before any
  header dereference) — the zero-storage drop-safety proofs carry over
  UNCHANGED.
* ALL VALUE OBSERVATION FAILS CLOSED with a runtime contract failure
  (`drift_contract_fail`, abort-with-diagnostic): C `drift_string_len`
  / `drift_string_data`, eq/cmp, hashing, concat, byte-at, `to_cstr`,
  and every borrowed C-string API.  A tombstone reaching observation is
  a compiler use-after-move — silently reading it as "" would MASK the
  bug; failing closed surfaces it.
* `retain`: PROPOSED fail-closed, subject to the §2.4 reachability
  gate (B0 currently no-ops; a proven legitimate copying path reopens
  the decision for that documented class only).
* MALFORMED handles fail closed everywhere: `{len != 0, NULL}` and
  `len < 0` are contract failures in every helper and accessor —
  including release (a nonzero-length NULL-storage handle is not a
  legal tombstone; note the R1/R5 zero-backs always write BOTH words
  zero, so the legal tombstone is exactly all-zero).
* The earlier claim ("retire `{0, NULL}`, remove every branch") and the
  intermediate draft ("accessors treat tombstone as empty") are both
  WITHDRAWN; helpers keep exactly one guard — the tombstone check —
  whose two arms are no-op (release-family) or contract-failure
  (observation-family).
* PROOF OBLIGATION: B5 must demonstrate (corpus + full suite + memcheck
  with the observation traps armed) that no legitimate program passes a
  tombstone to an observation boundary — the same instrument as the
  §2.4 retain gate.

STOP (§8.4): if implementation shows the singleton + drop-only-
tombstone model cannot preserve the existing zero-storage safety proofs
verbatim (R1 zero-init, null-safe releases, MoveFromRef tombstones,
PATH_DEPENDENT widening), OR the observation/retain traps fire on a
legitimate path that cannot be resolved as a compiler fix, B5 halts for
re-review.

### 2.7 LLVM layouts
- `%DriftString = type { i64, ptr }` — UNCHANGED type shape; field 1 now
  a header pointer.
- Static literal constant — SAME 3-field spelling, NEW field semantics
  and NEW GEP target:
  ```
  @.strN = private unnamed_addr constant { i64, i64, [N+1 x i8] }
           { i64 1, i64 <STATIC|NUL_SCANNED|maybe HAS_INTERIOR_NUL>, c"…\00" }
  handle = { i64 len, ptr GEP(@.strN, 0, 0) }      ; field 0 = HEADER (was field 2 = bytes)
  ```
  The literal cache key/shape logic is unchanged; the emitted flags
  constant is computed per literal from its bytes.
- Inline byte access (`StringByteAt` lowering, llvm_codegen.py:2966):
  today extracts field 1 and GEPs bytes directly; under B5 it becomes
  `bytes = getelementptr i8, ptr storage, i64 16` then index — one of
  exactly THREE codegen layout-authority lowerings (with the literal
  emitters and the §3.3 `string_bytes_base` intrinsic), inventoried in
  §4.
- `StringLen` stays `extractvalue …, 0` — the hot path is untouched.

### 2.8 Performance posture — parsers and bulk byte access (pinned)

Maintainer follow-up (2026-07-23): performance-sensitive string code
(parsers, scanners, protocol decoders) operates on BORROWED spans/
cursors over ONE backing String allocation — it does not manufacture
substring Strings.  Pinned for B5:

* **The low-allocation pattern is normative**: keep one borrowed
  `&String`; represent tokens/matches as byte ranges `{start, end}`;
  scan via `byte_length()` + `string_byte_at()` (or bulk `with_bytes`);
  materialize an owned substring ONLY when it must escape; construct
  output through `StringBuilder` (out of B5 scope, §10).  Precedent
  already in-tree: the regex API's `RegexMatch` is a copyable
  byte-offset span, not an allocated substring.
* **B5 must make bulk borrowed byte access efficient**: `with_bytes`
  compiles to the accessor load (`storage + 16`) once, then raw pointer
  arithmetic in the body — no per-byte accessor calls, no retain
  traffic inside the borrow.
* **`substring() -> String` continues to allocate exact storage** (no
  offset/slice views inside the String handle — §2.2 stands).
* **`StringView { storage, start, len }` is a separate FUTURE API**,
  explicitly not part of B5 and not another String representation
  phase: a retained zero-copy view for spans that must escape;
  converting it to `String` COPIES, and its C-string conversion may
  also copy (the view's end need not coincide with the backing
  allocation's trailing NUL, so the hidden-NUL zero-copy promise does
  NOT extend to views).
* **`string_byte_at` cost, stated precisely (corrected)**: each lowered
  access gains one header-offset GEP (`+16`) when computing the bytes
  base — LLVM typically hoists/folds it for loop-invariant handles at
  -O, but that is an OPTIMIZER outcome, not a guarantee; repeated
  `string_byte_at` does not promise base-once.  `with_bytes` DOES
  guarantee base-once by construction and is the recommended bulk
  path.  The BEFORE/AFTER performance gate stands: representative
  byte-scan carriers (parser-shaped e2e loops) measured on 0.33.87 vs
  the B5 branch; regression beyond noise = STOP §8.6.

## 3. C interop surface (explicit borrowed/owned; fields are private)

### 3.1 C accessors — exact signatures (decisions 6)
```c
/* string_runtime.h — read-only views over LIVE handles only.
 * Canonical empty: len() == 0, data() == non-null pointer to the
 * singleton's trailing NUL; C-string conversion succeeds.
 * Tombstone {0, NULL}: CONTRACT FAILURE (drift_contract_fail) — a
 * drop-only sentinel is never a readable value (§2.6).
 * Malformed ({len != 0, NULL}, len < 0): contract failure. */
static inline drift_isize          drift_string_len (DriftString s);
static inline const unsigned char *drift_string_data(DriftString s);
/* on live handles: data(s) == (const unsigned char *)(s.storage + 1);
 * bytes[len] == 0 always (hidden NUL). */
```
`drift_string_data` is THE bytes accessor name (the plan's
`drift_string_bytes` alias is struck — one name, decision 10; the
only other layout-touching export is the §3.3 cache bridge).  Layout
knowledge is restricted to TWO parties — `string_runtime.{h,c}` on the
C side, and the COMPILER CODEGEN LAYOUT AUTHORITY on the compiler
side, which is exactly THREE lowerings: (1) the literal emitters
(`_lower_const_string`/`_emit_string_literal_value`), (2) the
`StringByteAt` lowering (+16 bytes-base), (3) the §3.3 private
`string_bytes_base` intrinsic lowering.  All three are named in the
codegen layout inventory (§4) and covered by boundary tests; nothing
else in the compiler may spell the header layout.  ENFORCEMENT: a new source audit
(`test_string_layout_audit.py`, modeled on the owned-string audit)
fails CI on any other C file touching `->strong`, `->flags`,
`(… + 1)`-style tail arithmetic on `DriftRcBytes*`, or the
`__drift_rt_string_empty` symbol; all ~63 direct `.len`/`.data`
member reads in runtime C (§6.audit) migrate to the two accessors.

### 3.2 Drift-side borrowed APIs — COMPILE-PROVEN signatures (decision 8)

All signatures below are the REAL repository syntax (`require F is
core.FnN…`, by-value `body: F`, `pub struct` / `implement`,
`core.FnThrow1..4` throwing traits) and **EVERY promised family is
compile-proven end to end (compile + link + run) by the PERMANENT
regression** `lang/tests/driver/test_b5_ffi_signature_probes.py`:
`with_bytes` (nothrow + throw), checked `with_cstr1..4`, checked
THROWING 1..4, unsafe 1..4, both scope forms, owned/released types with
getters — the probes stay in-tree per the review requirement; the
earlier pseudocode (`CPtr`, `Fn(...)`, brace-struct syntax) is
withdrawn.

```
// std.ffi (ruling: std.ffi)
pub variant CStringError {
	InteriorNul(arg: Int, index: Int)      // 1-based ordinal, byte offset
}

pub fn with_bytes<T, F>(s: &String, body: F) nothrow -> T
	require F is core.Fn2<mem.Ptr<Byte>, Int, T>
pub fn with_bytes_throw<T, F>(s: &String, body: F) -> T
	require F is core.FnThrow2<mem.Ptr<Byte>, Int, T>

pub fn with_cstr<T, F>(a: &String, body: F) nothrow -> core.Result<T, CStringError>
	require F is core.Fn1<mem.Ptr<Byte>, T>
pub fn with_cstr2<T, F>(a: &String, b: &String, body: F) nothrow -> core.Result<T, CStringError>
	require F is core.Fn2<mem.Ptr<Byte>, mem.Ptr<Byte>, T>
pub fn with_cstr3<T, F>(a: &String, b: &String, c: &String, body: F) nothrow -> core.Result<T, CStringError>
	require F is core.Fn3<mem.Ptr<Byte>, mem.Ptr<Byte>, mem.Ptr<Byte>, T>
pub fn with_cstr4<T, F>(a: &String, b: &String, c: &String, d: &String, body: F) nothrow -> core.Result<T, CStringError>
	require F is core.Fn4<mem.Ptr<Byte>, mem.Ptr<Byte>, mem.Ptr<Byte>, mem.Ptr<Byte>, T>
// throwing checked variants: same shapes with core.FnThrow1..FnThrow4,
// no `nothrow` on the helper (probe-proven at ALL arities 1..4, both
// the nothrow and throwing families instantiated end-to-end).
// unsafe no-scan variants: with_cstr_unsafe(+2/3/4) — same bounds,
// return T directly (probe-proven at all arities 1..4).
// Validation is LEFT-TO-RIGHT; the FIRST failing argument reports
// (arg ordinal, byte index) before body runs.
```

**Escape semantics (no new type-system feature):** `mem.Ptr<T>` is Copy
and storable; the pointer MAY syntactically escape but becomes INVALID
when the callback returns (the borrow of `s` ends); later use is
UNSAFE — the `ptr_from_ref` contract, documented + §7 valid-window
teeth.

**Probe-discovered LANGUAGE_BUG — FIXED (regression-first, per
policy):** the "helper-returned `mem.Ptr<Byte>` fails require-bound
unification" symptom was a checker defect, not API debt.  Root cause
(call-resolution subsystem, `checker/call_resolver.py`): the
argument-position `defer_infer_diag` deferral returned Unknown BEFORE
attempting resolution, silently un-typing ANY nested call — however
non-generic — in method-call argument position.  Fixed at the root
with an EXPLICIT-OWNER state transaction: `FnCheckState`
(type_checker.py) owns the per-function mutable checker state — the
recorder side tables (transaction-aware `_TxnDict`/`_TxnList`
undo-log overlays, aliased into `check_function`'s locals) plus the
`next_node_id`/`next_callsite_id`/`_next_binding_id` allocator cells
— and `CheckerStateTxn` is a watermark-scoped undo-log transaction
over that owner plus an explicit per-node attribute log for the
probed HIR subtree (descendant node identities preserved; no frame
introspection anywhere in production).  A fail-closed shape gate
(`_defer_probe_shape_safe`, explicit HIR-node allowlist) admits only
subtrees whose typing writes exclusively through the owned state;
lambdas, match/block expressions, statements, and any unknown node
kind take the legacy silent deferral.  The probe models THREE
outcomes classified by STRUCTURED diagnostic codes (E-CTOR-EXPECTED-
TYPE, E-INFER-UNDERDETERMINED, E-INFER-EXPECTED-LITERAL — set at
emission; inference CONFLICTS are hard; no message parsing):
COMPLETE (live resolution stands, commit), NEEDS_EXPECTED (rollback —
no diagnostics, HIR rewrites, callsite metadata, expression types,
coercions, instantiations, or allocator movement remain — then the
enclosing retry runs exactly as pre-fix), HARD_ERROR (live resolution
with its REAL diagnostics committed; node marked so the retry cannot
duplicate).  Unexpected exceptions ROLL BACK AND RE-RAISE (normal ICE
containment).  Pinned behaviors: non-generic success,
argument-inferable generic success, expected-return-dependent generic
retry, hard-error diagnostic preservation (exactly once) on both the
free-call and interface-argument paths; state identity across
rollbacks — owner fingerprint, an INDEPENDENT frame-locals auditor,
whole-body HIR, allocator cells, and the exception path — is proven
by the invariant tooth
`lang/tests/checker/test_defer_probe_state_transaction.py`.
Measured (final, post-round-8 corrections — complete _TxnList
coverage, canonical-predicate gate, detached result outputs, stated
mixed-is-hard rule): 102 probes across a full stdlib compile (all
COMPLETE); 924-corpus, clean isolated sequential runs: +0.45% wall /
+0.36% CPU vs pre-fix HEAD (1510.7s/23,314s user vs
1503.9s/23,230s user), all 14 audit counters +0, identical
universe.  Protocol followed: minimal failing regression
FIRST (`lang/tests/driver/test_nested_call_arg_defer_infer_regression.py`
— confirmed failing pre-fix, passing post-fix, PERMANENT), suspected
subsystem recorded, `doc/refactor_triggers.md` scanned (no matching
trigger).  The fix rides the 0.33.88/ABI-22 branch and shares its
single certification.  The probes now exercise the helper-returned
path directly (no workaround).

### 3.3 Call boundary: private intrinsic + pointer-taking C helpers (finding 2 — CHOSEN)

`static inline` C accessors export no symbols and ordinary by-value
`String` arguments may materialize stakes — so the Drift↔C boundary is
pinned as (recommended option, adopted):

* **Private compiler intrinsic** (std.ffi-internal, not `pub`):
  ```
  @intrinsic fn string_bytes_base(s: &String) nothrow -> mem.Ptr<Byte>;
  ```
  Lowers to `extractvalue …, 1` + `getelementptr i8, ptr, 16` — a
  BORROW with NO retain (no stake materialization; the enclosing
  helper holds `s` live).  Used by `with_bytes` / `with_cstr*` /
  scope pins.  Joins the §4 codegen layout inventory and gets
  dedicated boundary tests (§7).
* **Pointer-taking C helpers** — declared to Drift as `&String`
  (borrowed; no by-value stake protocol at all):
  ```c
  drift_isize drift_string_interior_nul_index(const DriftString *s);
      /* first interior-NUL index, or -1; owns the §2.3 cache protocol
       * (relaxed load; one relaxed fetch_or publish; existence cached,
       * index re-scanned on the error path).  Tombstone/malformed →
       * drift_contract_fail. */
  char *drift_string_to_owned_cstr(const DriftString *s, drift_isize *nul_index_out);
      /* interior NUL → returns NULL, *nul_index_out = index;
       * else malloc'd NUL-terminated copy, *nul_index_out = -1 */
  void drift_cstr_free(char *p);                    /* pairs to_owned_cstr */
  typedef struct DriftCBytes { unsigned char *ptr; drift_isize len; } DriftCBytes;
  DriftCBytes drift_string_to_owned_cbytes(const DriftString *s);   /* infallible copy */
  void drift_cbytes_free(DriftCBytes b);            /* pairs to_owned_cbytes */
  ```
  Allocator identity stays inside the runtime (malloc/free); C
  libraries taking ownership via `.release()` must be malloc/free-
  compatible (documented; mismatch is on the taker).
* **The inline by-value accessors (§3.1) remain FOR C CONSUMERS** —
  they are the C-side read API; Drift never calls them.

### 3.3.1 Owned/scope types — complete, probe-proven shapes (finding 3)

```
pub struct OwnedCStr {
	ptr: mem.Ptr<Byte>
}
implement core.Destructible for OwnedCStr {
	pub fn destroy(var self: OwnedCStr) nothrow -> Void {
		// ptr_is_null guard → no-op after release(); else drift_cstr_free
	}
}
implement OwnedCStr {
	pub fn get(self: &OwnedCStr) nothrow -> mem.Ptr<Byte>       // borrowed view
	pub fn release(var self: OwnedCStr) nothrow -> mem.Ptr<Byte>
	// nullable-field pattern (probe-proven; NO forget/into_raw intrinsic
	// needed): mem.replace swaps self.ptr with a null Ptr; destroy's
	// ptr_is_null guard then no-ops — release-then-drop is safe and
	// double release is impossible (self was consumed by `var self`).
}
pub fn to_owned_cstr(s: &String) nothrow -> core.Result<OwnedCStr, CStringError>

pub struct ReleasedCBytes {                 // exact released-value type
	ptr: mem.Ptr<Byte>,
	len: Int
}
implement core.Copy for ReleasedCBytes {
}
implement ReleasedCBytes {                   // PUBLIC getters (review:
	pub fn data(self: &ReleasedCBytes) nothrow -> mem.Ptr<Byte>   // fields are
	pub fn size(self: &ReleasedCBytes) nothrow -> Int             // private)
}

pub struct OwnedCBytes {
	ptr: mem.Ptr<Byte>,
	len: Int
}
implement core.Destructible for OwnedCBytes { /* same guard, drift_cbytes_free */ }
implement OwnedCBytes {
	pub fn get(self: &OwnedCBytes) nothrow -> ReleasedCBytes     // borrowed view {ptr,len}
	pub fn release(var self: OwnedCBytes) nothrow -> ReleasedCBytes
	// returns BOTH pointer and length — "same pattern as OwnedCStr" was
	// insufficient (finding 3) and is replaced by this exact type.
}
pub fn to_owned_cbytes(s: &String) nothrow -> OwnedCBytes        // infallible

pub struct CArgv {                           // COPY — a NON-OWNING scope view
	argv: mem.Ptr<mem.Ptr<Byte> >,           // NULL-terminated (argv[argc]==NULL)
	argc: Int
}
implement core.Copy for CArgv {
}
implement CArgv {                            // PUBLIC getters (probe-proven)
	pub fn vector(self: &CArgv) nothrow -> mem.Ptr<mem.Ptr<Byte> >
	pub fn count(self: &CArgv) nothrow -> Int
}
// CArgv owns NOTHING: the vector + its cstrings belong to the scope and
// die with it; copies of CArgv are copies of the view only.

pub struct CStringScope { /* internal: pinned Strings + owned blocks */ }
implement core.Destructible for CStringScope { /* releases pins, frees blocks */ }
implement CStringScope {
	pub fn cstr(self: &mut CStringScope, s: &String) nothrow -> core.Result<mem.Ptr<Byte>, CStringError>
	pub fn cstr_unsafe(self: &mut CStringScope, s: &String) nothrow -> mem.Ptr<Byte>
	pub fn argv(self: &mut CStringScope, xs: &Array<String>) nothrow -> core.Result<CArgv, CStringError>
	// checked, LEFT-TO-RIGHT over xs; ordinal = array index + 1
}
pub fn with_cstring_scope<T, F>(body: F) nothrow -> T
	require F is core.Fn1<&mut CStringScope, T>
pub fn with_cstring_scope_throw<T, F>(body: F) -> T
	require F is core.FnThrow1<&mut CStringScope, T>
// The HELPER retains ownership of the scope; the body receives
// `&mut CStringScope` (maintainer ruling — probe-proven in both the
// nothrow and throwing forms).  The scope's Destructible cleanup runs
// in the helper AFTER the body returns — every pointer/CArgv handed
// out is invalid once the body returns (§3.2 escape contract).
```

### 3.4 Conventions
Conventions A/B and `DRIFT_OWNED_STRING` are UNCHANGED (by-value handle,
same stake protocol); the owned-string audit keeps enforcing them, and
the new layout audit (§3.1) joins it.

## 4. Complete consumer inventory (what the cutover must touch/re-verify)

**Compiler (lang/codegen/llvm/llvm_codegen.py — 151 `DriftString`/
`DRIFT_STRING_TYPE` references):**
1. `_lower_const_string` + `_emit_string_literal_value` — literal
   constant flags + GEP field 2→0 (the two emitters + their cache).
2. `StringByteAt` lowering — the single inline bytes-pointer computation
   (add the +16 header offset).
3. `StringLen` — unchanged (`extractvalue 0`).
3b. NEW: the §3.3 private `string_bytes_base` intrinsic lowering
   (extractvalue 1 + GEP +16, borrow, no retain) — the third and final
   member of the codegen layout authority; dedicated boundary tests.
4. `_emit_copy_value(String)` → `drift_string_retain`,
   `_emit_drop_value(String)` → `drift_string_release` — symbol calls,
   unchanged.
5. ~30 helper call sites (`drift_string_concat/eq/cmp/from_*` etc.) —
   by-value signatures unchanged; recompile-only.
6. FFI-site literals (`_ensure_ffi_site` C-string globals) — plain C
   strings, NOT DriftStrings; unaffected (verify).
7. DI/debug-info: `%DriftString` member naming (`data`→`storage`) in the
   debug type description, if spelled.

**Compiler (lang/driftc):** representation-blind by design — the MIR
vocabulary (`ConstString`/`StringRelease`/`StringRetain`/`CopyValue`…)
and the whole ownership pipeline carry no layout knowledge.  Verify-only
(grep pins in §7).  `DRIFT_STRING_HELPER_SYMBOLS`
(string_ownership_analysis) is a SYMBOL list — unchanged.

**Runtime C (15 files reference `DriftString`):**
`string_runtime.{h,c}` (full rewrite of block layout + accessors + the
15 exported helpers), `console_runtime.{h,c}` (4 `.data` reads),
`array_runtime.{h,c}` (3), `env_runtime.{h,c}`, `argv_runtime.c`,
`posix/{io,fs,assert,thread}_runtime.*`, plus `alloc_track`/codec/random
signature-only users.  the measured member-read census (43 `.data` reads + 32 `.len` reads
across 63 source lines in 6 files — supersedes the stale Scope-B "~27"
figure) migrates to `drift_string_data` / `drift_string_len` (both
`static inline` in the header — zero-cost).  `abi_version_stamp.c` — no change (macro re-expands to
`__drift_rt_abi_version_22`).

**Tests/goldens:** `test_abi_version_stamp.py` (stamp symbol 21→22 in
expectations), codegen goldens embedding the literal constant shape,
DI/layout tests, `test_drift_owned_string_audit.py` (unchanged rules; new
accessor sites join the audit), memcheck 38 files, om matrix 51 rows
(+ pkgb lane), ~120 string e2e fixtures (behavioral — should pass
unmodified; goldens that quote IR literal shape regenerate).

**External:** §6.

## 5. Atomic ABI 21→22 migration sequence (one branch)

All steps land on ONE branch; nothing intermediate is certified.  Order
chosen so the tree is never mixed-layout:

1. **Runtime**: new `DriftRcBytes` + handle, accessors, helper bodies,
   empty singleton, overflow rules, NUL-cache, conventions prose;
   `_Static_assert` battery for every §2 constant.
2. **Codegen**: literal emitters (flags + GEP 0), `StringByteAt` offset,
   DI member naming; regenerate affected goldens.
3. **Versioning** (same commit as 1+2 reaching the driver):
   `lang/versions.py` — `DRIFT_RT_ABI_VERSION = 22`;
   `DRIFTC_VERSION = "0.33.88"` (BINDING, decision 1).  No ABI-21
   shim, no dual-layout runtime — stale artifacts fail at link.  The
   link-time guard flips automatically (`__drift_rt_abi_version_22`);
   `doc/history.md` gains the ABI 21→22 entry: representation-only —
   **no valid-source semantic change** — with the APPROVED invalid-state
   hardening named explicitly (NULL/negative constructor inputs and
   tombstone observation now fail closed instead of silently reading as
   empty).
4. **Mismatch regression**: extend `test_abi_version_mismatch_link_failure`
   / `test_abi_mismatch_driver_hint` to prove an ABI-21 object cannot
   link against the ABI-22 runtime (and vice versa) with the driver hint
   naming both versions.
5. **In-tree gates** (§7 matrix) on the branch.
6. **Stage** the ABI-22 toolchain (staged root; never touching
   `~/opt/drift/{staged,certified}` promoted artifacts except via the
   deploy tooling), then §6 pool/downstream rebuild.
7. **ONE full certification**: run-all.sh + corpus + pool recert +
   deploy as the single ABI-22 boundary.

## 6. Pool rebuild, DriftQuery, external FFI audit

**6.1 Certified pool** (as deployed 2026-07-23): 8 packages —
mariadb-rpc, mariadb-wire-proto, microflows, net-tls, singular,
web-client, web-jwt, web-rest — + 2 apps — mariadb-failpoint-proxy,
uflowsd.  ABI changes ⇒ per the standing ABI policy this is the
bump-and-recertify path (no same-ABI fix-and-keep shortcut): full
rebuild against the staged ABI-22 toolchain, recert, redeploy.  The
link-time stamp makes any stale artifact fail deterministically.

**6.2 DriftQuery coordination** (external, currently unblocked on
0.33.80+ blocking-FFI): schedule their recompile against ABI-22;
deliverable to their team = this checkpoint's §2/§3 + the accessor API;
their C externs audited per 6.3.  Their `_exit()`-era workarounds are
already retired; no known blocking coupling, but their sign-off BLOCKS the ABI-22
certification/deployment (ruling §9e).

**6.3 External FFI audit — PERFORMED AT DESIGN TIME (2026-07-23),
result CLOSED** (finding 6: the audit is a design deliverable, not a
staging-time follow-up).  Swept the local workspaces of every pool
package + downstream consumer (drift-mariadb-client, drift-net-tls,
drift-query, drift-web, drift-workflows, mariachi, pushcoin):
* **ZERO downstream C files reference `DriftString`** — no external C
  code sees the struct layout at all.
* **ZERO downstream `extern` declarations take `String` by value** —
  downstream C interop marshals through stdlib buffers/pointers
  (e.g. DriftQuery's `cstr(s: String) -> io.Buffer` before its LMDB
  externs; net-tls declares raw-pointer externs in `ssl.drift`).
* Conclusion: the by-value `DriftString` FFI surface is confined to the
  IN-TREE runtime/stdlib (§6.4e).  Downstream ABI-22 impact is
  RECOMPILE-ONLY, enforced by the link stamp.
* Residual obligation: re-run this sweep mechanically at staging
  (cheap re-verification against then-current downstream heads), and
  DriftQuery's recompile carries a per-extern re-check.  A NEW external
  consumer appearing after this audit = STOP §8.3.

### 6.4 COMPLETE consumer audit (maintainer directive; measured on 0.33.87)

**(a) `ZeroValue(String)` / zero-handle producers (the tombstone class):**
| producer | sites | shape |
|---|---|---|
| `hir_to_mir.py` | 9 `M.ZeroValue(` emissions (env-slot zero-backs, match-cleanup tombstones, mem.replace shapes) | scalar zero handles |
| `ownership_normalization.py` | 4 (R1 entry zero-init ×3 groups + R5 MoveOut zero-back) | scalar |
| `return_cleanup_emitter.py` | 2 (string-release band + site-3 tail zero-backs) | scalar |
| `overwrite_cleanup.py` | 3 (R2 `_emit_local_release`, plan-phase drops, R7) | scalar |
| codegen aggregate zeroing | `zeroinitializer` stores + insertvalue chains (llvm_codegen 2420/3316/3322/3377/3557) + `TombstoneValue` | NESTED zero Strings inside structs/variants — the reason decision 4 keeps the tombstone reserved |
| codegen `_emit_zero_value(String)` | llvm_codegen 8683 arm | lowers scalar `ZeroValue(String)` to `{i64 0, ptr null}` — UNCHANGED under B5 |

Observation-reachability PROOF OBLIGATION (§2.6): with the value-
observation + retain traps armed in the staged runtime, the 924 corpus,
full suite, and memcheck must show ZERO tombstone observations — the
audit that proves the drop-only contract matches reality.

**(b) LLVM `zeroinitializer` for `%DriftString`:** via the aggregate
paths above; no site constructs a partial handle — all-zero or fully
built.  B5 keeps `{0, null}` as the legal tombstone bit pattern, so NO
codegen zeroing site changes.

**(c) Direct `.len`/`.data` member reads (runtime C — measured):**
43 `.data` + 32 `.len` reads on 63 source lines: string_runtime
(20 data / 15 len) · posix/thread (15/2) · array (3/6) · console (4/4) ·
argv (1/2) · posix/assert (0/3).  (env/io/fs use only
from_cstr/to_cstr — no member reads.)
`.len` reads stay VALID (field unchanged) but migrate to
`drift_string_len` anyway under the §3.1 audit; `.data` reads change
MEANING and all migrate to `drift_string_data`.

**(d) Static-literal consumers:** the two codegen emitters
(`_lower_const_string`, `_emit_string_literal_value` + cache) — GEP
field 2→0 + compile-time flag computation; codegen goldens embedding the
constant spelling; `_ensure_ffi_site` C-string globals are PLAIN char
arrays (verified — not DriftStrings, unaffected); no external linker
consumer of `@.str*` is known (STOP §8.2 if found).

**(e) By-value `DriftString` FFI surface:** 27 extern signatures across
the runtime headers (console 4, env 2, argv, array String ops, posix
io/fs/assert/thread, exec/vt observability, error/exc JSON helpers) —
signatures UNCHANGED (two-word by-value handle); bodies re-audited for
member reads per (c); Convention A/B classifications unchanged.
`drift_string_literal` (runtime-side literal ctor) gains the offset-0
header.  External by-value externs (pool + DriftQuery) enumerated per
§6.3 before staging.

**(f) Owned/borrowed C-string consumers (`to_cstr`/`from_cstr`, 22
sites, 7 files):** io_runtime 9 · env 3 · assert 3 · string 3 · argv 2 ·
fs 1 · thread 1.  All are the allocating/copying pair — semantics
preserved (§3.3); each site re-audited for the §2.5 NULL-input
tightening (unset-env and similar "absent" flows must guard BEFORE
constructing, as env_runtime already does).

## 7. Acceptance matrix (all on the ONE branch; single certification)

| gate | pin |
|---|---|
| Representation pins | `_Static_assert` battery (header size/offsets/flag values/MAX_LEN); unit teeth: hidden NUL at `bytes[len]` for every ctor; empty-singleton identity + UNCONDITIONAL malformed-handle/tombstone-observation contract failures (both runtime builds) + zero-storage tombstone drop-only no-op; NUL-cache monotonicity + literal-emitted cache bits; overflow aborts (len<0, >MAX, concat sum); retain/release ordering probe under TSAN-style stress carrier |
| API signature probes | `test_b5_ffi_signature_probes.py` (PERMANENT): every §3.2/§3.3 Drift signature compiles+links+runs against the current substrate — already in-tree and green pre-GO |
| Bytes-base intrinsic | when introduced: POSITIVE full compile/run probes AND NEGATIVE boundary-contract tests (borrow with no retain / stake-free lowering, +16 base correctness, layout-inventory pin, misuse rejections) |
| Layout audit | `test_string_layout_audit.py`: only string_runtime + the literal emitter touch storage/header layout; all runtime member reads migrated to accessors |
| API teeth | `with_cstr*` left-to-right (arg ordinal + byte index) teeth; pointer-invalid-after-return contract pins (valid-window teeth; escape is documented-unsafe, no checker claimed); tombstone FAIL-CLOSED pins (observation + retain traps; release no-op pin; malformed-handle pins); canonical-empty accessor pins (len 0 / non-null NUL / cstr succeeds); to_cstr owned-copy pin; OwnedCStr/OwnedCBytes drop/release teeth; armed-trap corpus/suite/memcheck reachability gate |
| Literal shape | codegen golden: `{ i64 1, i64 flags, [N+1 x i8] }` + GEP-to-header; literal-cache dedup unchanged; UTF-8 escape goldens |
| Perf before/after gate | byte-scan carriers (parser-shaped loops incl. `string_byte_at` and `with_bytes` bulk access) measured 0.33.87 vs B5 branch; beyond-noise regression = STOP §8.6 |
| Mismatch regression | ABI-21 obj × ABI-22 runtime (both directions) link-fail + driver hint |
| e2e | full `lang/tests/codegen/e2e` (~120 string fixtures among them) |
| Ownership matrices | om 51/51 + `ownership-matrix-asan` + pkgb consumer lane |
| memcheck | full `lang/tests/memcheck/` — 0 leaks, 0 invalid access (the 38 string files are the core carriers) |
| ASAN | full suite under `DRIFT_ASAN=1` (run-all.sh half) |
| Corpus | 924 audit vs the accepted 0.33.87 baseline — universe hashes are SOURCE-side, so the baseline stays comparable; **all 14 counters +0 expected** (representation is invisible to the ownership pipeline; any delta = STOP) |
| Full suite | `./run-all.sh` (both modes) green on the final branch tree |
| Certification | pool rebuild (6.1) + DriftQuery sign-off (6.2) + FFI audit closed (6.3) → single ABI-22 cert + deploy |

## 8. STOP conditions (implementation halts and returns for review)

1. Any VALID-SOURCE semantic change surfaces: Copy/immutability/UTF-8
   contract, comparison/hash behavior, interning, or a helper whose
   observable result differs on valid inputs (beyond pointer identity)
   — B5 is representation-only FOR VALID PROGRAMS.  (The §2.5 NULL/
   negative constructor tightenings and the §2.6 tombstone-observation
   contract failures are INTENTIONAL hardening of invalid states, and
   the history entry must say so in exactly those terms.)
2. An incompatible literal/linkage assumption: anything outside the
   compiler links/aliases `@.str*` globals, assumes the `data-16` header
   trick, or takes the address of literal BYTES expecting field-2 layout.
3. An unaccounted external consumer: any by-value `DriftString` extern
   (pool, DriftQuery, or elsewhere) that cannot be migrated to the
   accessor API, or any FFI site discovered outside the §6.3 inventory
   after it closes.
4. The singleton + reserved-tombstone model (§2.6) cannot preserve the
   existing zero-storage safety proofs verbatim — including any
   memcheck/e2e carrier showing a zeroed handle observed where the
   proofs say it is unreachable, or any nested/aggregate zeroing path
   the tombstone contract does not cover.
5. Corpus counter delta ≠ 0, or a memcheck/ASAN finding traceable to the
   representation (not a pre-existing latent bug — those file as their
   own regression-first slices, as always).
6. A measured hot-path regression class — the §2.8 before/after gate
   (byte-scan carriers incl. `string_byte_at` and `with_bytes` bulk
   access) shows a beyond-noise regression — pause for a measurement
   review, not silent acceptance.
7. Refcount-ordering questions: any evidence the carried
   relaxed/release+acquire protocol (identical to B0's explicit
   orderings) behaves observably differently on a supported target
   after the reshape.

## 9. Open decision points for the reviewer

(a) RESOLVED (decision 1): 0.33.88 / ABI 22.
(b) RESOLVED: `IMMORTAL` stays a separate bit, MUTUALLY EXCLUSIVE with
    `STATIC` (literals = STATIC; singleton = IMMORTAL; both = illegal).
(c) RESOLVED: `std.ffi`.
(d) RESOLVED: sentinel/contract-failure teeth REQUIRED in BOTH the
    normal and release/NDEBUG runtime configurations (subprocess teeth
    against both variants).
(e) RESOLVED: DriftQuery sign-off BLOCKS the ABI-22 certification/
    deployment.

## 10. Scope closure (decision 10 — ships in ABI-22 or struck NOW)

| promised API | disposition |
|---|---|
| `drift_string_len` / `drift_string_data` accessors | SHIP |
| `drift_string_empty()` + internal singleton | SHIP |
| `with_bytes` | SHIP |
| `with_cstr` / `with_cstr2/3/4` + `CStringError::InteriorNul(arg, index)` | SHIP |
| `with_cstr_unsafe` (+2/3/4) | SHIP |
| `CStringScope` (`scope.cstr`, `scope.cstr_unsafe`, `scope.argv` → `CArgv`) | SHIP |
| `OwnedCStr` (`to_owned_cstr`, Drop/free/release) | SHIP |
| `OwnedCBytes` (`to_owned_cbytes`, Drop/free/release) | SHIP |
| `drift_string_to_cstr` / `from_cstr` | RETAINED, unchanged names + owned-copy semantics (§3.3) |
| `drift_string_bytes` accessor-name alias | STRUCK (one name: `drift_string_data`) |
| `StringBuilder` / `BytesBuilder` | OUT OF SCOPE (pre-existing separate-type direction; not a B5 promise) |
| `StringView` shared substrings | OUT OF SCOPE — separate FUTURE API (`{storage, start, len}` retained view; to-String copies; C-string conversion may copy since the view end need not hit the backing trailing NUL); explicitly NOT another String representation phase (§2.8) |

No String phase remains implied beyond this chunk; anything not in this
table is not promised.

**STOP — end of checkpoint.  Report-only; no implementation performed.
Awaiting design review of the amended (binding-decisions) version.**
