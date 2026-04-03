# Bookkeeper Leak: Precise Root Cause Analysis

## Date: 2026-04-02
## Compiler: 0.27.143 (git e78440a8)

---

## Reproducer

```drift
module consumer;
import std.core as core;
import std.format as fmt;
import std.log as log;

pub fn main() nothrow -> Int {
    var cb = log.config_builder();
    cb.sink(log.stderr_sink());
    cb.min_level(log.Level::Debug());  // Info enabled — full emit path
    val cfg = cb.build();
    val logger = log.create_logger("test", cfg);
    val _ = logger.info("startup", {"port": fmt.format_int(18100)});
    return 0;
}
```

- Source mode (`--stdlib-root`): **clean** — Valgrind reports 0 leaks
- Package-consumer mode (`--package-root` + signed std.dmp): **leaks 22 bytes / 1 block**
- Test: `lang/tests/driver/test_pkg_map_literal_string_leak.py::test_map_literal_format_int_logger_emit`

## Refcount trace (instrumenting drift_string_retain / drift_string_release)

### PEX mode (leaks):
```
 #  op       rc     caller
 1  create    →1    format_int (via drift_string_concat)
 2  retain   1→2    drift_main              ← string_arc retain before insert
 3  retain   2→3    drift_dv_string         ← to_debug wraps string in DV
 4  retain   3→4    drift_dv_as_string      ← _dv_json extracts string from DV
 5  retain   4→5    _dv_json                ← codegen Optional<String> construction
 6  retain   5→6    _dv_json                ← string concat operand
 7  release  6→5    Optional_String drop    ← None arm cleanup
 8  retain   5→6    _dv_json                ← another concat operand
 9  release  6→5    _json_escape            ← escape consumes input
10  release  5→4    _dv_json                ← concat temp release
11  release  4→3    _dv_json                ← concat temp release
12  release  3→2    drift_dv_release_impl   ← DV drop releases embedded string
13  release  2→1    HashMapCore::clear      ← HashMap destroy releases value
14  release  1→0???  drift_main             ← scope-exit release of format_int temp
FINAL: rc=1 (step 14 should bring to 0, but trace shows 2→1)
```

6 retains + 1 create = 7.  Only 6 releases.  Missing one release.

### Source mode (clean):
```
 #  op       rc     caller
 1  create    →1    format_int__impl (nothrow direct, no FnResult wrapper)
                     — NO retain in drift_main (no string_arc retain needed)
 2  retain   1→2    drift_dv_string
 3  retain   2→3    drift_dv_as_string
 4  retain   3→4    _dv_json
 5  retain   4→5    _dv_json
 6  release  5→4    Optional_String drop
 7  retain   4→5    _dv_json
 8  release  5→4    _json_escape
 9  release  4→3    _dv_json
10  release  3→2    _dv_json
11  release  2→1    drift_dv_release_impl
12  release  1→0    HashMapCore::clear
FINAL: rc=0 — freed correctly
```

5 retains + 1 create = 6.  6 releases.  Balanced.

## The divergence mechanism

### What is NOT the cause
- Wrapper routing divergence (eliminated by the wrapper convergence refactor)
- HashMap destroy chain — identical and correct in both modes
- `_dv_json` function itself — identical IR, identical retain/release pattern
- `_emit<V>` / `_emit_throwing<V>` — identical in both modes
- `_attrs_json<V>` — identical in both modes

### What IS the cause

The divergence is in **drift_main** itself — specifically, how the
string_arc pass handles the format_int temporary when format_int is
called through the **can-throw FnResult ABI wrapper** (PEX mode) vs
the **nothrow direct call** (source mode).

**Source mode**: `format_int` is compiled alongside the consumer. The
compiler sees that `format_int__impl` is nothrow and calls it directly.
The String result is a temporary that goes straight to the HashMap
insert call. The string_arc pass sees a single-use owned value passed
to a function with a String param → consumed, no retain needed.

**PEX mode**: `format_int` comes from the stdlib package. It's called
through the can-throw ABI wrapper, returning `FnResult<String, Error>`.
The result is unwrapped: `%t52 = extractvalue FnResult, 1` (the OK
String). This `%t52` is then used as the value argument to
`HashMap::insert`. But the string_arc pass inserts a `drift_string_retain`
before the insert call and a `drift_string_release` at scope exit.

The retain/release pair in drift_main should be balanced (net zero).
The missing release is somewhere in the _dv_json serialization chain:
the _dv_json function has identical IR in both modes, but the
**format_int string enters the chain at a different refcount** (rc=2 in
PEX vs rc=1 in source), and one release in the chain targets a different
alias of the same string.

### The precise mechanism: off-by-one in the retain/release chain

The chain for the "18100" string through `_dv_json`:

1. `to_debug(&String)` loads the String value from the HashMap entry
   reference and passes it to `drift_dv_string`, which **retains** it.
2. `dv.as_string()` calls `drift_dv_as_string`, which **retains** the
   string again (extracting an owned copy from the DV).
3. The codegen constructs `Optional<String>::Some(retained_copy)` with
   **yet another retain** (`opt_owned61`).
4. The match arm extracts `v` from the Optional and passes it to
   `_json_escape(v)`.

Steps 1-3 add 3 retains. The matching releases are:
- Step 3's Optional is dropped (1 release) on the None arm — but on the
  Some arm, `v` is consumed by `_json_escape`, so the Optional's payload
  is moved out, not released.
- Step 1's DV is dropped via `drift_dv_release_impl` (1 release).
- The HashMap clear releases the value (1 release).

In source mode: 3 retains from steps 1-3, 3 releases from the above = balanced.
In PEX mode: the drift_main retain adds +1 and drift_main release subtracts
-1, which should balance. But the chain has **one path where the
Optional<String> scrutinee cleanup releases a zeroed-out value instead of
the actual string** — this is the missing release.

## Remaining architecture seam

The wrapper convergence eliminated the **routing** divergence (which
module's wrapper to call, which return type to use). But it did NOT
eliminate the **FnResult ABI wrapping** divergence:

- Source mode: nothrow functions are called directly, returning bare
  values. The string_arc pass sees a simple owned temp.
- PEX mode: even nothrow functions are called through can-throw ABI
  wrappers when they cross a package boundary. The FnResult unwrap
  creates a new SSA value (`extractvalue`). The string_arc pass treats
  this as a different temp that needs its own retain/release pair.

This extra retain/release pair introduces a **refcount offset** into
the downstream chain. The downstream functions (`_dv_json`, etc.) are
identical, but they receive the string at a different base refcount.
The bug manifests because the total retain/release accounting across
the full chain is off by exactly one.

**This is the FnResult-unwrap string_arc seam** — a divergence that
lives below wrapper routing and was not in scope for the wrapper
convergence plan (which focused on making the routing decision
mode-independent, not on the string lifecycle of unwrapped FnResult
values).

## Fix direction

Two possible approaches:

1. **Eliminate the extra retain/release in drift_main**: when the
   string_arc pass processes a FnResult unwrap (`extractvalue` of the
   OK field), it should recognize that the extracted String value is
   already owned and doesn't need an additional retain before being
   passed to a consuming call. This is the most targeted fix.

2. **Fix the double-retain in the dv.as_string() codegen**: the
   `drift_dv_as_string` C function retains the string, then the
   codegen adds another retain for the Optional<String> construction.
   This double-retain exists in both modes but is harmless in source
   mode because the base refcount is lower. The real fix should ensure
   the codegen knows that `as_string` returns an already-retained value.

Approach 1 is the mode convergence fix. Approach 2 is the correctness
fix for the double-retain pattern that happens to be masked in source
mode. Both should be investigated.

## Files involved

- `lang/driftc/stage2/string_arc.py` — string_arc pass (retain/release insertion)
- `lang/driftc/stage2/hir_to_mir.py` — FnResult unwrap lowering
- `lang/compiler_infra/diagnostic_runtime.c:209-217` — `drift_dv_as_string` retains
- `stdlib/std/log/log.drift:921-961` — `_dv_json` match on dv.as_string()
- `lang/tests/driver/test_pkg_map_literal_string_leak.py` — reproducer test
