# Logger Work Progress

## 1. Pin MVP Contract First (Short Spec Note)

- `std.log` levels: `debug`, `info`, `error`.
- Global default logger plus optional named logger instances.
- Thread-safe producer API with single-owner sink serialization.
- Non-throwing, best-effort logging path for app code (`nothrow`-friendly).
- Backpressure policy pinned before implementation: choose one of `block-with-timeout`, `drop-oldest`, or `drop-newest`.
- Backpressure policy is configured via builder: `backpressure_policy(...)`.
- Default backpressure policy is `block_with_timeout`.
- Root logger default backpressure policy is `block_with_timeout`; derived loggers inherit it unless explicitly overridden at creation.
- Any dropping policy must expose dropped-record telemetry (for example counters/health metrics) so loss is visible, not silent.
- Pinned: logging path is async via bounded MPSC queue so caller path is not stalled on sink I/O.
- Pinned: sink I/O and JSON serialization are owned by a single consumer thread/task.
- Pinned: producer path is enqueue-only and non-throwing; queue-full behavior follows the pinned backpressure policy.

## 2. API Surface (Small, Stable)

- Structured JSON is the default formatter.
- Default timestamp field is `tm` with ISO-8601 UTC format: `YYYY-MM-DDTHH:mm:ss.sssZ`.
- Logs are events, not prose:
  - `log.debug(ev, attrs)`
  - `log.info(ev, attrs)`
  - `log.error(ev, attrs)`
- Default global logger name is `main`; plain `log.*(...)` routes through that logger.
- Default sink for logger `main` is `stderr` (`stdout` is opt-in via config).
- Canonical shape target:
  - `ev` (string event name, e.g. `auth-failed`)
  - `level` (`debug` | `info` | `error`)
  - `logger` (logger/facility name; default logger is `main`)
  - `tid` (thread id)
  - `attrs` (object; nested objects allowed)
- Attribute passing stays machine-friendly and key-value oriented (exact call syntax depends on final language ergonomics).
- MVP call shape is positional: `log.<level>(ev, attrs)`.
- `attrs` is a map/object value; target ergonomic form is literal `{ key: value, ... }` once map literals land.
- Introduce `Debuggable` trait so non-primitive values can project safe structured fields instead of dumping raw object internals.
- `Debuggable` output may be nested under the attribute key to preserve structure in JSON logs.
- User-pluggable formatter hook is supported by config builder (default remains JSON).
- Source metadata (`file`, `line`, `fn`, `module`) is not auto-injected in MVP to avoid compiler magic/special treatment.
- If source metadata is needed, it should be provided explicitly via attrs until a non-special mechanism is available.
- Pinned: generic compiler primitive for callsite metadata is `std.meta.caller()`.

## 3. Init / Config Builder

- Provide logger initialization via a small config builder.
- Root logger carries default settings; additional loggers are derived from an existing logger via builder overlays.
- Deriving/building a logger always creates a new parent logger instance; existing loggers are unchanged.
- Builder fields (MVP):
  - `sink`: `stdout` | `stderr` | `file(path)` | `custom(sink)`
  - `min_level`: `debug` | `info` | `error`
  - `backpressure_policy`: `block_with_timeout` | `drop_oldest` | `drop_newest`
  - `queue_capacity`: bounded queue size
  - `write_timeout`: max sink write wait
  - `enqueue_timeout`: max producer enqueue wait (if policy can block)
- Custom sinks are user-implementable and supported through builder-provided `custom(sink)`.
- Fanout is modeled as a custom sink (multi-sink forwarding), not a distinct logger-core feature.
- File sink default policy:
  - append mode by default
  - create file if missing
  - on write failure: do not throw into app path; emit internal logger error to `stderr` (best-effort) and increment sink-failure telemetry

## 4. Logger Scoping (Sub-Loggers)

- A class/module can create a scoped sub-logger from an existing logger handle.
- Sub-loggers are cheap clones/children and share the same backend pipeline (queue + sink worker).
- Sub-loggers carry scope metadata (for example `facility`, `module`, or component name).
- Sub-loggers may override `min_level`.
- Sub-loggers do not override sink, queue capacity, timeout, or backpressure policy in MVP.
- Sub-loggers are shareable handles across files/modules so library code can log through one library-level scoped logger.
- Hierarchy model: applications configure a root logger once, then libraries/modules derive logger handles from that root (or from other derived loggers), inheriting defaults plus explicit builder overrides.

## 5. Lifecycle Guarantees

- `init` is idempotent for equivalent config (repeat init is a no-op).
- Re-init with a different config is rejected; config mutation requires creating a new logger instance/name.
- Logger instances are immutable after creation (no in-place config edits).
- `flush(timeout)` is supported and attempts to drain accepted records to sink within timeout.
- No `shutdown` API in MVP.
- Logging remains available for process lifetime once initialized.

## 6. Test Matrix (Before Legacy Cleanup)

- Unit:
  - level filtering
  - format correctness
  - queue overflow/backpressure behavior
- Concurrency:
  - many producer threads
  - no record corruption
  - all accepted records serialized deterministically
- E2E:
  - stdout/stderr sink logging
  - file sink logging
  - timeout paths
  - flush drain guarantees

## 7. Follow-Ups

- Fix examples under `lang/examples/logging/` to valid Drift call syntax (named args use `name = expr`, not `"key": value`), with attrs passed through a proper `attrs` value/builder.
- Near-term prerequisite feature: add map/object literals (similar ergonomics to array literals), then standardize logger calls as `log.<level>(ev, attrs)` with `{ key: value }` attrs.
- Separate future feature track (non-logging-specific): macro system. Logging macros can later improve ergonomics and lazy attrs/source injection, but macro design should live in its own branch/feature.
