# LANGUAGE_BUG Report: io.buffer_read returns zero/corrupted byte on live TCP path

  ## Classification

  LANGUAGE_BUG candidate (stdlib/runtime I/O boundary path).
  Current evidence points to stdlib/runtime std.io buffer extraction path, not protocol decode logic.

  ## Environment

  - Compiler: driftc 0.1.0-dev | abi 1 | git 1caae7ad
  - Runtime: rebuilt libdrift_rt.a from same toolchain main
  - Repro repo: drift-mariadb-client
  - Server: MariaDB live instance reachable on 127.0.0.1:34114

  ## Symptom

  Live networking tests fail with nonzero exit codes, while fixture decode tests pass.

  Pinned diagnostic from live path:

  - net.connect() succeeds
  - stream.read(&mut buf, timeout) returns Ok(n) with n > 0
  - io.buffer_read(&mut buf, 0) returns 0x00
  - Expected first handshake byte is 0x5b (91), confirmed via Python TCP read from same server
  - Repro test exits 36 when hdr[0] == 0

  ## Repro artifact

  - packages/mariadb-wire-proto/tests/e2e/live_io_path_diag_test.drift
  - Exit contract:
      - 36 => first header byte read as zero from io.buffer_read

  ## Key narrowing already established

  - decode_handshake_hello is correct on fixture/unit paths:
      - handshake_decode_test passes
      - handshake_fixture_replay_test passes
  - Failure is in I/O data path before decode:
      - net.TcpStream.read -> io.buffer_read -> header byte extraction

  ## Suspected subsystem

  Primary:

  - stdlib/std/io/io.drift buffer read/extraction logic (buffer_read, related len/cap/index handling)

  Secondary:

  - runtime posix/io_runtime.c interaction with buffer layout/len updates
  - any ABI mismatch between stream.read result and buffer state mutation

  ## Required execution protocol (regression-first)

  1. Add minimal failing regression in toolchain repo (prefer e2e) that pins:
      - bytes written into buffer via read path
      - buffer_read(..., 0) yields wrong value (or equivalent corruption evidence)
  2. Confirm fail on current compiler/runtime.
  3. Fix root cause in stdlib/runtime/toolchain (no user-lib workaround).
  4. Confirm regression pass.
  5. Re-run relevant std.io/std.net and mariadb live smoke checks.

  ## Test requirements

  Positive:

  - buffer_read returns exact byte values at known indices after successful read.

  Negative/contract:

  - out-of-range/invalid index still yields current expected diagnostics/behavior.
  - no regression in existing std_io_buffer_* tests.

  Cross-check:

  - compare first packet bytes from Drift path vs Python baseline for one live handshake.

  ## Non-goals

  - Do not modify mariadb decode logic as workaround.
  - Do not “mask” by changing protocol expectations.

  ## Completion criteria

  - Pinned regression in compiler/runtime repo
  - root-cause fix landed
  - regression green
  - no contradictory docs/tests about buffer semantics

