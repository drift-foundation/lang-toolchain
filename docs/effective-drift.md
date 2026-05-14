# Effective Drift

Common idioms for programs that won’t ghost you in prod.

## copy vs. move vs. share — when each spelling is right

Drift's three by-value capture spellings are not synonyms. The
distinction is load-bearing for code review:

| Spelling | Type requirement | Semantic | When to use |
|---|---|---|---|
| `captures(copy x)` | `T: Copy` | Value-like duplicate. The new owner is independent; aliasing is invisible. | `Int`, `String`, `Optional<T>` over Copy. The user is asking for a value, not a handle. |
| `captures(move x)` | `T: !Copy` | Ownership transfer. The original `x` binding is consumed and unusable after the lambda is constructed. | One-owner-only types (`MutexGuard<T>`, `VirtualThread<T>`); also works for `Arc<T>` if you do not need `x` to survive the closure. |
| `captures(share x)` | `T: Share` | A second OWNER of the same underlying resource. The original `x` remains usable; both owners hold the resource alive. **Aliasing is real**: mutations through either owner are observable through the other. | `Arc<T>` (and any user type implementing `std.core.shareable.Share`) when you need both the closure and the outer scope to keep using `x`. |

Two rules to internalize:

1. `share x` is **warning-bearing**. The keyword carries a contract:
   you have just accepted aliasing. If the closure escapes to a
   thread, task, or callback that mutates through the captured
   owner, **you** are responsible for synchronization (e.g., wrap
   the resource in `Mutex<T>` or use atomics).
2. **`String` is `Copy`, not `Share`.** Even though `String` is
   internally refcounted, the user-facing semantic is value-like
   duplication — write `captures(copy s)`, not `captures(share s)`.
   `share` is reserved for types whose aliasing is part of the
   programming model (e.g., `Arc<T>`).

`Arc.clone()` remains available as a method and produces the same
runtime result as `Share::share(&arc)`. Prefer `captures(share x)`
in capture lists: it keeps the aliasing contract visible at the
capture site and avoids needing a named clone-temp.

### `share x` at the call boundary (expression form, 0.31.20)

`share x` is also available as an **expression**, symmetric with
the capture-list form, for passing a second owner to a callee
without consuming the outer binding:

```drift
val app: conc.Arc<AppHandle> = conc.arc(make_handle());

try {
    val _ = serve(share app, port);   // adds an owner, app stays usable
    return 0;
} catch e {
    val r = app.get();                 // app is still LIVE here
    r.logger.error("startup-failed", { ... });
    return 1;
}
```

The expression form has the same Share-trait constraint as the
capture form (`T: Share`) and emits the same warning-bearing
contract: you have just accepted aliasing. It lowers to
`Share::share(&x)` — refcount bump on `Arc<T>`, identical runtime
cost to `app.clone()` or the explicit trait call.

Two ergonomic gains over `move x` / `app.clone()` at call sites:

1. The original binding stays usable after the call, so subsequent
   code can reference `app` directly without a named keepalive
   (`var log_app = app.clone()` becomes unnecessary).
2. **Outstanding borrows survive the call, including the unwind
   path of a throwing callee.** `share x` is a refcount bump on
   the owner, NOT a mutation of the binding — so `val r = app.get();`
   taken before a `try { f(share app); }` remains valid in the
   `catch` arm. No re-`.get()` after every share call.

Restrictions in v1:

- The subject must be a NAME (local binding). Bind first if you
  need a more complex expression: `val a = compute(); share a;`.
  Diagnostic: `E-SHARE-EXPR-SUBJECT-NOT-LOCAL`.
- Subject type must implement `std.core.shareable.Share`. For
  `Copy` types (`Int`, `String`, ...), use `copy x`; for non-Share
  non-Copy types, use `move x`. Diagnostic:
  `E-SHARE-EXPR-NOT-SHARE` (parallel to
  `E-CAPTURE-SHARE-NOT-SHARE`).

ABI-neutral; source-only feature. The lowering desugars to the same
trait dispatch the capture form already uses, so codegen and the
borrow checker see identical IR.

## Callback arity surface (`Callback0`..`Callback6`, 0.31.21)

Drift ships boxed-callable interfaces and constructor intrinsics for arities **0 through 6**, in both nothrow and throwing flavors:

| Param count | Nothrow iface / ctor | Throwing iface / ctor |
|---|---|---|
| 0 | `Callback0<R>` / `callback0(f)` | `CallbackThrow0<R>` / `callback_throw0(f)` |
| 1 | `Callback1<A, R>` / `callback1(f)` | `CallbackThrow1<A, R>` / `callback_throw1(f)` |
| 2 | `Callback2<A, B, R>` / `callback2(f)` | `CallbackThrow2<A, B, R>` / `callback_throw2(f)` |
| 3 | `Callback3<A, B, C, R>` / `callback3(f)` | `CallbackThrow3<A, B, C, R>` / `callback_throw3(f)` |
| 4 | `Callback4<A, B, C, D, R>` / `callback4(f)` | `CallbackThrow4<A, B, C, D, R>` / `callback_throw4(f)` |
| 5 | `Callback5<A, B, C, D, E, R>` / `callback5(f)` | `CallbackThrow5<A, B, C, D, E, R>` / `callback_throw5(f)` |
| 6 | `Callback6<A, B, C, D, E, F, R>` / `callback6(f)` | `CallbackThrow6<A, B, C, D, E, F, R>` / `callback_throw6(f)` |

Each constructor is `@intrinsic` and requires the input function to satisfy the matching `Fn{N}` (nothrow) or `FnThrow{N}` (throwing) trait. Implicit-wrap dispatch covers all arities — bare lambdas, `captures(share x)` lambdas, and named-fn refs all wrap correctly into the matching boxed form when the parameter type is concrete.

**The cap is 6 in v1, on purpose.** If you find yourself wanting `Callback7<...>`, the right answer is to **pack arguments into a struct** rather than extending the family:

```drift
struct DispatchCtx { req: Request, ctx: Context, depth: Int, deadline: Instant, /* ... */ }

fn middleware(handler: core.CallbackThrow1<DispatchCtx, Response>) nothrow -> Int { ... }
```

Why the cap: every additional arity adds a row to `_CALLBACK_ROWS` (compiler) + `IntrinsicKind` enum + `call_contract` spec + four stdlib declarations. The benefit is bounded — past 6 params, the call site reads as a struct anyway. Capping at 6 keeps the compiler's central-table refactor self-contained and stops the "just one more arity" creep without forcing a real design choice (struct-pack) onto users.

## Shared state + callbacks (Arc + Mutex)

When you need multiple handlers that all mutate the same receiver object, put
the receiver in an `Arc` and wrap it in a `Mutex`. Each handler **shares** the
Arc into its environment, takes the lock, and mutates through a guard. This
avoids ownership conflicts and keeps the emitter dumb.

```drift
import std.core as core;
import std.concurrent as conc;

struct StateMachine { state: Int }

implement StateMachine {
    fn on_signal_x(self: &mut Self, e: &Event) -> Void { self.state = self.state + 1; }
    fn on_signal_y(self: &mut Self, e: &Event) -> Void { self.state = self.state - 1; }
    fn on_signal_z(self: &mut Self, e: &Event) -> Void { self.state = 0; }
}

fn register_handlers(bus: &mut EventBus) -> Void {
    var sm: conc.Arc<conc.Mutex<StateMachine>> = conc.arc(conc.mutex(StateMachine(state = 0)));
    var cb_x: core.Callback1<&Event, Void> = core.callback1(|e: &Event| captures(share sm) => {
        var guard = conc.lock(sm);
        guard.get_mut().on_signal_x(e);
        return;
    });
    bus.on_x(move cb_x);
    var cb_y: core.Callback1<&Event, Void> = core.callback1(|e: &Event| captures(share sm) => {
        var guard = conc.lock(sm);
        guard.get_mut().on_signal_y(e);
        return;
    });
    bus.on_y(move cb_y);
    var cb_z: core.Callback1<&Event, Void> = core.callback1(|e: &Event| captures(share sm) => {
        var guard = conc.lock(sm);
        guard.get_mut().on_signal_z(e);
        return;
    });
    bus.on_z(move cb_z);
}
```

Notes

- `captures(share sm)` evaluates `Share::share(&sm)` once at each
  callback's construction site, then move-captures the produced
  owner into the closure environment. The outer `sm` binding
  remains usable across all three callback constructions — no
  named `sm.clone()` temporaries needed.
- Aliasing is real: all three callbacks (and `sm` in
  `register_handlers`) hold owners of the same underlying
  state-machine. The `Mutex<StateMachine>` is what makes
  cross-thread mutation safe; `share` alone says nothing about
  thread-safety.
- This works with owned callbacks and does not rely on borrowed
  captures.

### The pre-`share` idiom (older spelling, less preferred in capture lists)

Before `captures(share x)` existed, the same shape was written with
a named clone temp per capture:

```drift
var sm_x: conc.Arc<conc.Mutex<StateMachine>> = sm.clone();
var cb_x: core.Callback1<&Event, Void> = core.callback1(|e: &Event| captures(move sm_x) => {
    /* ... */
});
```

This is equivalent at runtime and **`Arc.clone()` remains
available** as a method — it is **not deprecated** and is the
right spelling when you need a named clone outside a capture list
(e.g., to hand to a non-capture callee, or to build an
intermediate handle that survives independently of any closure).
In **capture lists** specifically, `captures(share sm)` is
preferred: it is shorter, makes the aliasing contract visible at
the capture site, and avoids per-callback temporaries.

## Shared service, multiple interface faces (`Arc.as_interface`)

When a single service object needs to be handed to several subsystems under
different interfaces — resolver here, emitter there, shutdown hook over
there — put the service in one `Arc` and hand each subsystem an
`Arc<Interface>` **view** over that same allocation. There is one service,
one control block, one refcount. Each interface view dispatches through the
appropriate `implement I for Service` block against the same underlying
bytes.

```drift
import std.core as core;
import std.concurrent as conc;
import std.log as log;
// Hypothetical subsystems on the same page as std.log.
import app.metrics as metrics;
import app.lifecycle as lifecycle;

struct AppService {
    req_counter: conc.AtomicInt,
    last_event: conc.Mutex<String>,
    // ...whatever state the service actually needs
}

implement log.ContextResolver for AppService {
    pub fn resolve(self: &AppService) nothrow -> Optional<&log.LogContext> {
        // consult internal state, return borrow or None
    }
}

implement metrics.Emitter for AppService {
    pub fn emit(self: &AppService, name: &String, v: Int) nothrow -> Void {
        // increment counters via conc.AtomicInt, enqueue via an MPSC queue, etc.
    }
}

implement lifecycle.ShutdownHook for AppService {
    pub fn on_shutdown(self: &AppService) nothrow -> Void {
        // drain queues, close files, signal workers
    }
}

fn install(builder: &mut log.LoggerConfigBuilder,
           reg: &metrics.Registry,
           lc: &mut lifecycle.Runtime) nothrow -> Void {
    val app = conc.arc(AppService(
        req_counter = conc.atomic_int(0),
        last_event  = conc.mutex(""),
    ));

    val log_face = app.as_interface<type log.ContextResolver>();
    val metrics_face = app.as_interface<type metrics.Emitter>();
    val shutdown_face = app.as_interface<type lifecycle.ShutdownHook>();

    builder.context_resolver(log_face);
    reg.register(metrics_face);
    lc.on_shutdown(shutdown_face);
}
```

The conversion is spelled as a method on `Arc<T>`. The user names only the
target face (`<type log.ContextResolver>` etc.); the source concrete type
(`T = AppService`) is inferred from the `Arc<T>` receiver. This matches
the rule that users should spell only what the compiler can't infer — and
it keeps the code scannable: a reader sees `as_interface<I>()` and knows
exactly which face is being produced without parsing a two-type-argument
form.

What actually happens here:

- **One `AppService` allocation, not three.** `conc.arc(AppService(...))`
  performs the only heap allocation. `as_interface<...>()` does not
  allocate — it bumps the existing control block's strong count and
  returns a fat `Arc<Interface>` handle over the same allocation.
- **Shared control block.** `app`, `log_face`, `metrics_face`, and
  `shutdown_face` all reference the same strong count. Calling `.clone()`
  on any of them increments that shared count; dropping any of them
  decrements it. The refcount after `install` returns is exactly equal to
  the number of handles still reachable (subsystems plus any retained
  local).
- **State changes observed across faces.** A `metrics.emit(...)` call
  reaches a receiver that shares a single set of bytes with the receiver
  inside `log_face.get().resolve(...)` — counters bumped through one face
  are visible to the other. This is the point of the pattern.
- **Destructor runs once.** When the last face drops (could be any of them
  — order depends on which subsystem tears down last), the shared strong
  count hits zero and `AppService`'s `Destructible::destroy` runs exactly
  once. Individual interfaces do not have independent destructors for a
  shared concrete; the concrete type owns destruction.
- **Mutation requires interior mutability inside the service.** `Arc` —
  including its interface views — is shared ownership, not unique mutable
  access. Mutable fields in `AppService` must be protected with
  `conc.Mutex`, `conc.AtomicInt` / `AtomicBool` / etc., or a lock-free
  queue. Two subsystems holding different interface views over the same
  allocation must not assume exclusive access to the service's bytes.
- **Explicit and auditable.** The two-step pattern `val app = conc.arc(...);
  val face = app.as_interface<type I>();` makes the ownership transfer
  visible at every use site. A reader can see exactly which interface face
  is being produced, named right at the call, without parsing a two-type-
  argument form or inferring where the allocation lives.

Use this pattern whenever a single service implements several interfaces
and those interfaces need to be handed to different subsystems. One
allocation, one refcount, one destructor — and each subsystem holds the
face it needs, with the concrete type inferred from the `Arc<T>` receiver.

## Runtime registry patterns (`std.runtime`)

Use `global_registry()` for process-wide singletons. Reads are typed and safe via
`contains<T>(&reg)` / `get<T>(&reg)`, and strict retrieval is available via
`expect<T>(&reg, "missing-tag")` which throws `std.runtime:RegistryError`
with `tag: String`.

```drift
import std.runtime as rt;

struct AppConfig {
    name: String,
    max_workers: Int
}

fn init_config() nothrow -> Bool {
    val reg = rt.global_registry();
    val cfg = AppConfig(name = "main", max_workers = 8);
    return reg.set(move cfg);
}

fn read_config() nothrow -> Int {
    val reg = rt.global_registry();
    match rt.get<type AppConfig>(reg) {
        Some(cfg) => {
            if cfg.max_workers != 8 { return 1; }
            return 0;
        },
        None => { return 2; }
    }
}
```

For per-thread state in MVP (before a dedicated thread-local registry), use a
global singleton that stores a map keyed by `vt_current()` and guard it with
`Arc<Mutex<...>>`.

```drift
import std.concurrent as conc;
import std.containers as containers;
import std.runtime as rt;
import lang.thread as thread;

fn init_slots() nothrow -> Bool {
    val reg = rt.global_registry();
    var by_tid: containers.HashMap<Int, Int> = containers.hash_map<type Int, Int>();
    var map_mutex: conc.Mutex<containers.HashMap<Int, Int>> = conc.mutex(move by_tid);
    var map_arc: conc.Arc<conc.Mutex<containers.HashMap<Int, Int>>> = conc.arc(move map_mutex);
    return reg.set(move map_arc);
}

fn write_current_thread_slot() nothrow -> Bool {
    val reg = rt.global_registry();
    match rt.get<type conc.Arc<conc.Mutex<containers.HashMap<Int, Int>>>>(reg) {
        Some(shared_ref) => {
            var shared: conc.Arc<conc.Mutex<containers.HashMap<Int, Int>>> = (*shared_ref).clone();
            var g = conc.lock(shared);
            val slots = g.get_mut();
            val tid = thread.vt_current();
            val _ = slots.insert(tid, 1);
            return true;
        },
        None => { return false; }
    }
}
```

Matching runnable examples:
- `examples/runtime_registry/global_singleton.drift`
- `examples/runtime_registry/per_thread_slots.drift`

## CLI arguments (`std.cli`)

Use `std.cli` to define flags/options/positionals once, then parse `argv` and
consume typed values.

```drift
import std.cli as cli;
import std.core as core;

fn main(argv: Array<String>) nothrow -> Int {
    var p = cli.parser("backup-tool", "0.1.0", "Create backups.");
    val _ = p.flag("verbose", "v", "verbose mode");
    val _ = p.option_int("port", "p", "PORT", "control plane port", true);
    val _ = p.positional("target", "target directory", true, false);

    match p.parse(&argv) {
        core.Result::Ok(parsed) => {
            var port = 3306;
            var target = "";
            if parsed.has_flag("verbose", &p) {
                // ...
            }
            match parsed.get_int("port", &p) {
                Some(v) => { port = v; },
                None => { return 2; }
            }
            match parsed.positional_at(0) {
                Some(v) => { target = *v; },
                None => { return 3; }
            }
            return 0;
        },
        core.Result::Err(err) => {
            if err.tag == "cli-help-requested" { return 0; }
            return 2;
        }
    }
}
```

Matching runnable example: `examples/cli/main.drift`.

## Read a file

Use `file_builder(...).read(true).write(false)` and keep timeout on the
configured handle.

```drift
import std.concurrent as conc;
import std.core as core;
import std.io as io;

pub fn main() nothrow -> Int {
	return try run_main() catch { 1 };
}

fn run_main() throws -> Int {
	val t = conc.Duration(millis = 5000);
	val f = io.file_builder("example.txt").read(true).write(false).timeout(t).build().or_throw();
	var buf = io.buffer(1024);
	val n = f.read(&mut buf).or_throw();
	val _s = core.string_from_utf8_bytes(io.buffer_ptr(&buf), n);
	f.close().or_throw();
	return 0;
}
```

## Write a file

Use `file_builder(...).write(true).create(true).truncate(true)` for replace
semantics.

```drift
import std.concurrent as conc;
import std.core as core;
import std.io as io;

pub fn main() nothrow -> Int {
	return try run_main() catch { 1 };
}

fn run_main() throws -> Int {
	val t = conc.Duration(millis = 5000);
	val f = io.file_builder("example.txt").read(false).write(true).create(true).truncate(true).timeout(t).build().or_throw();
	var buf = io.buffer(6);
	io.buffer_write(&mut buf, 0, cast<Byte>(72));
	io.buffer_write(&mut buf, 1, cast<Byte>(101));
	io.buffer_write(&mut buf, 2, cast<Byte>(108));
	io.buffer_write(&mut buf, 3, cast<Byte>(108));
	io.buffer_write(&mut buf, 4, cast<Byte>(111));
	io.buffer_write(&mut buf, 5, cast<Byte>(10));
	val n = f.write(&buf).or_throw();
	if n != 6 { return 2; }
	f.close().or_throw();
	return 0;
}
```

## Structured event logging (JSON-first)

Prefer event names plus key-value attrs over prose strings. Keep payloads
machine-friendly and use explicit source metadata when you need it.
Default formatter is JSON and includes `tm` as ISO-8601 UTC
(`YYYY-MM-DDTHH:mm:ss.sssZ`).

```drift
import std.log as log;
import std.meta as meta;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
    val cfg_builder = log.config_builder();
    cfg_builder.sink(log.stderr_sink());
    cfg_builder.min_level(log.Level::Info());
    val cfg = cfg_builder.build();
    val logger = log.create_logger("main", cfg);

    logger.info("auth-failed", {"user": "alice", "reason": "bad-password", "src": meta.caller()});
    logger.error("db-timeout", {"host": "db-main", "retryable": true, "src": meta.caller()});
    logger.flush(conc.Duration(millis = 1000));
    return 0;
}
```

For formatter customization, see `examples/logging/pluggable_formatter.drift`.

### Ambient context via a resolver

For request- or task-scoped attributes, install a context resolver on the
builder once. Every bare `info(ev)` / `info(ev, attrs)` call (and the
`debug` / `error` siblings) then consults the resolver at emit time and
merges the returned context into the record. No app-side wrapper, no
per-call `match current_context()` block.

A resolver is any value implementing the `log.ContextResolver` interface.
`resolve` returns `Optional<&log.LogContext>` — a **borrow** into storage
the resolver already holds, not a fresh owned context per emit. The
logger reads the borrowed context synchronously during record formatting,
serializes its attrs into the owned payload_json, and does not retain the
borrow after the emit call returns. That contract makes the natural app
pattern — request context in a `rt.ScopedStack<log.LogContext>` inside a
thread-local registry — zero-copy:

```drift
import std.log as log;
import std.runtime as rt;
import std.concurrent as conc;

// App-defined request state: a scoped stack of LogContexts, installed
// once per thread into the thread registry and pushed/popped around
// each request.
pub struct RequestContextState {
    pub ctx: rt.ScopedStack<log.LogContext>
}

pub fn request_context_state() nothrow -> RequestContextState {
    return RequestContextState(ctx = rt.scoped_stack<type log.LogContext>());
}

// App-defined resolver service.  Peeks the top of the scoped stack
// out of the thread registry and hands the logger a borrow into it.
pub struct AppResolver { }

implement log.ContextResolver for AppResolver {
    pub fn resolve(self: &AppResolver) nothrow -> Optional<&log.LogContext> {
        val _ = self;
        val reg = rt.thread_registry();
        match rt.get<type RequestContextState>(reg) {
            Some(st) => { return st.ctx.peek(); },
            None     => { return Optional<&log.LogContext>::None(); }
        }
    }
}

fn install_logger() nothrow -> log.Logger {
    val reg = rt.thread_registry();
    val _ = reg.set<type RequestContextState>(request_context_state());

    // Allocate the concrete resolver once, then create the
    // `log.ContextResolver` face over the same allocation.  The fat
    // `Arc<log.ContextResolver>` is what the builder takes.
    val resolver = conc.arc(AppResolver())
        .as_interface<type log.ContextResolver>();

    var b = log.config_builder();
    b.context_resolver(resolver);
    return log.create_logger("svc", b.build());
}
```

Request scopes then just push/pop a `LogContext` onto the stack; the
logger sees the top of the stack on every emit without an allocation:

```drift
match rt.get_mut<type RequestContextState>(rt.thread_registry()) {
    Some(st) => {
        var ctx = log.log_context();
        ctx.put("request_id", req.id.clone());
        val guard = st.ctx.push(move ctx);
        // ... handle the request; logger.info(...) picks up request_id
        //     via the resolver with no per-emit clone.
        val _ = move guard;   // popped when the guard drops
    },
    None => { /* registry not installed */ }
}
```

At call sites the resolver is invisible:

```drift
logger.info("task-submitted");                                  // ambient ctx via resolver
logger.info("task-submitted", {"size": 42});                    // ambient ctx + attrs (caller wins on key collision)
logger.info("task-submitted", &caller_ctx);                     // explicit ctx — resolver SUPPRESSED
logger.info("task-submitted", &caller_ctx, {"k": dv_value});    // explicit ctx + DV-typed override
logger.info("task-submitted", &log.log_context());              // per-call opt-out: explicit empty context
```

The rule: passing an explicit `&LogContext` (even an empty one) suppresses
the resolver. Use `&log.log_context()` when an event genuinely should
carry no contextual attrs.

`context_resolver` takes a `conc.Arc<log.ContextResolver>`. The app
allocates the concrete resolver with `conc.arc(...)` and then creates
the resolver face with `.as_interface<type log.ContextResolver>()`;
both handles refer to the same allocation and the same strong count.
Sub-loggers (`derive`, `with_min_level`) clone that Arc face to share
the resolver service — one atomic refcount per sub-logger creation,
never per emit. Per-emit dispatch borrows `&log.ContextResolver`
through the stored Arc and calls `resolve()` via the vtable — no Arc
retain/release on the hot path. Passing an explicit `&LogContext`
still suppresses the resolver; `&log.log_context()` remains the
explicit empty-context opt-out.

## Graceful shutdown on SIGINT/SIGTERM (`std.concurrent::await_signal`)

Long-running services should handle SIGINT/SIGTERM cleanly: stop accepting new
work, drain in-flight work, flush logs, and exit with a deterministic status.
`std.concurrent::await_signal()` is the building block. The contract is in the
stdlib reference; the rules that matter for app code are:

- Linux only.
- Only `SIGINT` and `SIGTERM` are observable.
- **Exactly one** virtual thread may be blocked in `await_signal()` at a time.
  Treat it as the shutdown coordinator and call it from a single place — almost
  always near `main()`.
- The call is `nothrow`; misuse aborts. Don't try to "race" two waiters.

The pattern: a `running` flag the workers consult, a single coordinator that
blocks on `await_signal()` and flips the flag, then an orderly drain.

```drift
module main;

import std.concurrent as conc;
import std.log as log;
import std.sync as sync;

pub fn main() nothrow -> Int {
    val cfg_builder = log.config_builder();
    cfg_builder.sink(log.stderr_sink());
    cfg_builder.min_level(log.Level::Info());
    val logger = log.create_logger("svc", cfg_builder.build());

    val running = sync.atomic_bool(true);

    // Workers / accept loops poll `running` between iterations and exit
    // promptly when it flips to false. Spawn them here.
    // val server = start_accept_loop(running, logger);

    logger.info("svc-started", {});

    // Single shutdown coordinator: blocks until SIGINT or SIGTERM.
    val sig = conc.await_signal();
    match sig {
        conc.ProcessSignal::Interrupt => {
            logger.info("shutdown-signal", {"signal": "SIGINT"});
        }
        conc.ProcessSignal::Terminate => {
            logger.info("shutdown-signal", {"signal": "SIGTERM"});
        }
    }

    // 1. Stop accepting new work. Workers observe this on their next poll.
    running.store(false);

    // 2. Drain in-flight work. Join the accept loop / worker pool here.
    // server.join();

    // 3. Flush the logger so the shutdown line actually reaches the sink.
    logger.info("shutdown-complete", {});
    logger.flush(conc.Duration(millis = 1000));

    return 0;
}
```

Notes:

- Keep the coordinator in `main` (or a function called only from `main`). Do
  **not** call `await_signal()` from worker tasks — that violates the
  single-waiter rule and aborts.
- The `running` flag is the contract between the coordinator and the workers;
  `await_signal()` itself does not interrupt or cancel any task.
- Always `flush()` the logger after the final shutdown event. Process exit
  does not guarantee asynchronous sinks have drained.
- To react to repeated signals (e.g. a second Ctrl-C forcing immediate exit),
  call `await_signal()` again after the first return.

## JSON API + error tags (`std.json`)

`std.json` is JSON-first and machine-oriented:
- parse: `json.parse(&text) -> Result<JsonNode, JsonErrorData>`
- encode: `json.encode(...)`, `json.encode_compact(...)`, and `..._with_config(...)`
- key ordering policy: `JsonKeyOrder::Unordered()` (default) or `JsonKeyOrder::OrderedLexUtf8()`
- parse duplicate keys: keep-last
- shape mutation is wrapper-only:
`json.new_array()/json.new_object()` with `JsonArray.push(...)` and `JsonObject.set(...)`
- navigation: `get(&key)`, `get_path(&Array<String>)`, `entries()`
- extractors:
`as_bool/as_int/as_uint/as_float/as_string/as_number_raw/as_array/as_object` return `Optional`
`expect_*` throws `std.json:JsonError` with machine fields (`tag`, `offset`, `line`, `col`, `path`, `key`)

```drift
import std.console as console;
import std.core as core;
import std.json as json;

fn main() nothrow -> Int {
    return try run() catch { 99 };
}

fn run() -> Int {
    val text = "{\"users\":[{\"id\":42},{\"id\":7}]}";
    var node = json.JsonNode::Null();
    match json.parse(&text) {
        core.Result::Ok(v) => { node = move v; },
        core.Result::Err(e) => {
            // machine tag + positional context
            console.println(e.tag);
            return 1;
        }
    }

    var cfgb = json.config_builder();
    cfgb.key_order(json.JsonKeyOrder::OrderedLexUtf8());
    val cfg = cfgb.build();
    console.println(json.encode_with_config(&node, &cfg));

    var users_path = ["users"];
    val users_node = node.expect_path(&users_path);
    val users = users_node.expect_array("users", "users");
    if users.len != 2 { return 2; }
    return 0;
}
```

Matching runnable example: `examples/json/effective_drift_json_api.drift`.

Error tag contract:
- stable, machine-readable kebab-case tags
- current parse/data tags include:
`invalid-syntax`, `invalid-escape`, `invalid-datatype`, `missing-path`, `internal-error`
- `JsonErrorData` carries structured context (`tag`, `offset`, `line`, `col`, `path`, `key`)

Loop ergonomics note:
- use `for val x : source { ... }` when `source` is iterable (for example arrays)
- preferred JSON array iteration is `for val item : users { ... }` after `expect_array(...)`
- manual iterator form is also valid, but trait methods require trait scope:
`use trait iter.Iterable; use trait iter.SinglePassIterator;`

See also: **Result to throwing flow** below — `json.parse(&text)` returns
`Result<JsonNode, JsonErrorData>`, and the recommended idiom is to convert
it to throwing flow with `.or_throw()` and catch once at the `nothrow`
boundary, rather than nesting `match` per call.  `JsonErrorData` is a
`pub error`, so the compiler-synthesized `core.Throw` impl makes
`.or_throw()` throw `JsonErrorData` directly — the caller catches it as
`catch json:JsonErrorData(e)` with `e.tag` / `e.offset` / `e.line` /
`e.col` / `e.path` / `e.key` typed-projected from the envelope.

## The `throws` keyword

Drift has two distinct `throws` forms on function signatures. They share
the keyword but select different semantics based on whether a return type
is present.

### Auto-try form: `throws -> T`

A function declared `fn f(...) throws -> T` returns a value of type `T`
and enables a **body-wide auto-try context**. The same context is
opened by a `try { ... }` block in any function. The contract below
applies inside both.

The function still has a return obligation — it must return `T` on at
least one path. The `throws` marker is about the error-flow context
inside the body, not about the return shape.

#### The auto-try contract

Inside an auto-try context, a `Result<T, E>`-producing expression is
**eagerly unwrapped to `T`** via compiler-synthesized `or_throw()`
whenever possible. The four positions where this happens:

1. **Unannotated local binding** — `val q = fallible();` binds `q` as
   `T`, not as `Result<T, E>`.
2. **Annotated local binding with a non-Result type** — `val q: T = fallible();`
   also unwraps to `T`.
3. **Return expression** — `return fallible();` unwraps to match the
   declared return type.
4. **Discarded expression statement** — `fallible();` (no binding) auto-
   propagates the `Err` arm and throws away the `Ok` value.

Auto-try is **compiler-owned**. It does not require any lexical trait
import — `use trait core.Try;` is not part of the contract (and is no
longer a valid spelling, see "Obsolete forms" below).

```drift
fn handle_request(req: &Request) throws -> Response {
    val q = rest.require_query_param(req, &"q");
    //  q has type String, not Result<String, RestError>.
    val order = repo.load(&q);
    //  order has type Order; the Result<Order, DbError> is unwrapped.
    return rest.json_response(200, order.to_json());
}
```

#### The opt-out: explicit `Result<T, E>` annotation

If you need to keep the `Result` value — to pattern-match on it, pass
it across a boundary, or call `.or_throw()` explicitly later — annotate
the binding with the full `Result<T, E>` type. The annotation suppresses
auto-try.

```drift
fn handle_request(req: &Request) throws -> Response {
    val r: core.Result<String, rest.RestError> = rest.require_query_param(req, &"q");
    //  r has type core.Result<String, rest.RestError>.
    match r {
        core.Result::Ok(q) => { return repo.lookup(&q); },
        core.Result::Err(_) => { return rest.json_response(400, ...); },
    }
}
```

`Result<T, E>` annotation is the *only* opt-out. There is no other way
to keep the `Result` shape on a binding inside an auto-try context.

#### The preferred explicit form

When you don't need to name the `Result`, write the explicit unwrap
inline on the rvalue. This is the cleanest spelling and works in any
context (auto-try or not):

```drift
val q = rest.require_query_param(req, &"q").or_throw();
```

`or_throw()` is the single user-facing explicit-unwrap operation on
`Result<T, E>`.  Under Phase 5a strict enforcement, `E` MUST be a
`pub error` type — the compiler synthesizes the `core.Throw` impl, and
`or_throw()` consumes `self` by value.

#### Common pitfall: bound local + explicit `or_throw()`

Code that worked under earlier auto-try semantics may now fail because
the binding is unwrapped before the explicit call:

```drift
// PITFALL: this no longer compiles inside a throws function.
fn handle(req: &Request) throws -> String {
    val r = rest.require_query_param(req, &"q");
    val q = (move r).or_throw();
    //                ^^^^^^^^^^ error: no matching method 'or_throw'
    //                           for receiver String
    return q;
}
```

`r` is bound as `String` (eager auto-unwrap), so the subsequent
`.or_throw()` is being called on a `String`, not a `Result`. Two ways
to fix it:

```drift
// Fix 1: drop the intermediate binding, use the inline explicit form.
fn handle(req: &Request) throws -> String {
    return rest.require_query_param(req, &"q").or_throw();
}

// Fix 2: opt out of auto-try with an explicit Result annotation.
fn handle(req: &Request) throws -> String {
    val r: core.Result<String, rest.RestError> = rest.require_query_param(req, &"q");
    return (move r).or_throw();
}
```

Prefer Fix 1 unless you have a reason to keep `r` named (a separate
match arm, passing the `Result` through a `&` boundary, etc.).

#### Design rationale

Inside `throws` and `try {}`, the **default** behavior is propagation
with zero ceremony. The vast majority of `Result`-returning calls in a
throws context want their `Ok` value used immediately — eager unwrap
makes that the spelling without ceremony. Preserving the `Result`
object is the less-common case, so the language requires a deliberate
annotation to opt in.

This matches Drift's broader "explicit over implicit" rule applied at a
lower altitude: the explicit thing is *opting out* of the safe default,
not opting *in* to it.

#### Obsolete forms

The `core.Try` trait and the `into_try()` method are removed from the
language surface as of 0.27.199. Any source that contains
`use trait core.Try;` will fail to resolve, and `r.into_try()` is
rejected with "no matching method". Auto-try synthesizes `or_throw()`
internally; the method itself does not exist as a user-callable
operation.

### Terminal form: `throws` (no return type)

A function declared `fn f(...) throws` (bare, no `-> T`) is a
**terminal** function: it **never returns normally**.  Every control-flow
path must end in `throw` or a tail call to another terminal-`throws`
function.  The checker rejects `return` statements and fallthrough.

```drift
pub error ServiceDown {
    reason: String,
}

fn cannot_recover(why: String) throws {
    throw ServiceDown(reason = why);
}
```

The compiler synthesizes `implement core.Throw for ServiceDown` for
every `pub error`; user code never writes a `Throw` impl.

Terminal-`throws` calls are **terminators** for missing-return analysis.
A match arm or if-branch whose only statement is a call to a
terminal-`throws` function counts as locally terminal, even inside a
non-`throws` caller:

```drift
fn handle(r: Result<Int, ServiceDown>) -> Int {
    match r {
        Ok(v) => { return v; },
        Err(e) => { cannot_recover(e.reason); },
    }
}
```

### Four legal signature shapes

| Form | Meaning |
|---|---|
| `fn f(...) -> T` | Value-returning, may throw, no auto-try |
| `fn f(...) nothrow -> T` | Value-returning, cannot throw |
| `fn f(...) throws -> T` | Value-returning, body-wide auto-try |
| `fn f(...) throws` | Terminal — never returns normally |

`nothrow` is mutually exclusive with both `throws` forms.

## Result to throwing flow

`Result<T, E>` is a value-level error type. Throwing flow is better when the
caller is not going to recover locally and a framework or top-level boundary
already knows how to map domain exceptions to user-facing outcomes.

Use this pattern for app and framework code that wants a straight-line happy
path:

```drift
fn get_work_order(req: &rest.Request, ctx: &mut rest.Context) throws -> rest.Response {
    val id = rest.path_param(req, &"workOrderId").or_throw();
    val order = repo.load_work_order(&id).or_throw();

    return rest.json_response(200, order.to_json());
}
```

The handler reads as: get the value or leave through the framework's exception
path. The route body does not need nested `match` blocks for cases the framework
already owns, such as missing path params, invalid request bodies, auth failures,
or not-found responses.

### The contract: `or_throw()` consumes a `Result`

`or_throw()` is an inherent method on `Result<T, E>`.  Phase 5a strict
enforcement requires `E` to be a `pub error` type — the compiler
synthesizes `implement core.Throw for E` for every `pub error E`, so the
`require E is Throw` clause is satisfied automatically.  Calling
`or_throw()` on a `Result<T, E>` where `E` is not a `pub error` (scalar,
`pub struct`, `pub variant`, private `error`) is rejected at compile time
with `E_OR_THROW_NOT_ERROR_TYPE`.  The unwrap path is:

1. `result.or_throw()` consumes the owned `Result`.
2. The `Ok` arm returns the value.
3. The `Err` arm throws the typed `pub error` directly via the
   synthesized `core.Throw` impl.
4. The throw is terminal — `or_throw()` never returns normally on the
   `Err` arm.

The corresponding catch arm receives the typed event:

```drift
try result.or_throw() catch my:ParseError(e) { ... e.offset ... }
```

The supported API is the method form:

```drift
val value = result.or_throw();
```

The old free helper `core.or_throw(result)` is not the supported spelling.

`or_throw()` is owned-only. Do not call it on `&Result`. A direct inline
producer works because it returns an owned temporary:

```drift
val id = rest.path_param(req, &"workOrderId").or_throw();
```

A named local is also consumed by a by-value receiver:

```drift
val id_result = rest.path_param(req, &"workOrderId");
val id = id_result.or_throw();
```

After that call, `id_result` has been moved. If you want to make the ownership
transfer visually explicit, write the same operation with `move`:

```drift
val id_result = rest.path_param(req, &"workOrderId");
val id = (move id_result).or_throw();
```

This is often useful in examples and refactors, but it is not required for the
first consuming use of a named local.

### `pub error` carries the contract

In current Drift (Slice 5, Phase 5a), the throwable + `or_throw`-compatible
shape is `pub error E { ... }`.  The compiler synthesizes
`implement core.Throw for E` and `implement core.Diagnostic for E`
automatically — there is no user-written `Throw` impl, no struct/exception
twin, no manual remap.  The `core.Throw` trait still exists internally for
the synthesized impls; user code should not write it.

Here is a minimal custom error:

```drift
import std.core as core;

pub error ServiceDown {
    service: String,
    reason: String,
}
```

Now `Result<T, ServiceDown>.or_throw()` throws `ServiceDown` directly.  The
caller catches the domain event:

```drift
fn call_service() throws -> Response {
    return internal_rpc().or_throw();
}

fn main() nothrow -> Int {
    val resp = try call_service() catch ServiceDown(e) {
        return 1;
    };

    return 0;
}
```

The typed-catch binder `e` exposes both the schema fields declared on the
`pub error` (typed-projected from the envelope's params JSON; supported
scalars are `Int / Uint / Bool / Float / String`) and the Error envelope
methods (`e.encode_compact()`, `e.params.get(k)`, `e.context.encode_compact()`).

### Framework errors: one `pub error` per dispatcher arm

Frameworks declare one `pub error` per dispatcher arm and throw it directly
from the failing operation — no central `RestError + Throw impl` mapper is
needed, because synthesis lands the typed event in the right catch arm
without an intermediate trait body.

```drift
import std.core as core;

pub error RestBadRequest {
    tag: String,
    message: String,
}

pub error RestUnauthorized {
    tag: String,
    message: String,
}

pub error RestNotFound {
    tag: String,
    message: String,
}

pub error RestInternal {
    tag: String,
    message: String,
}
```

The framework helper functions that produce these errors can either return
`Result<T, RestBadRequest>` (for one-arm operations) or `throw RestNotFound(...)`
directly from a `throws` body when no Result staging is needed.  App code
stays small:

```drift
fn get_work_order(req: &rest.Request, ctx: &mut rest.Context) throws -> rest.Response {
    val id = rest.path_param(req, &"workOrderId").or_throw();
    val order = rest.load_work_order(ctx, &id).or_throw();

    return rest.json_response(200, order.to_json());
}
```

The dispatcher catches each typed event and maps it to the right transport
response:

```drift
fn dispatch(req: &rest.Request, ctx: &mut rest.Context) nothrow -> rest.Response {
    return try get_work_order(req, ctx) catch RestBadRequest(e) {
        return rest.error_response(400, e.tag, e.message);
    } catch RestUnauthorized(e) {
        return rest.error_response(401, e.tag, e.message);
    } catch RestNotFound(e) {
        return rest.error_response(404, e.tag, e.message);
    } catch RestInternal(e) {
        return rest.error_response(500, e.tag, e.message);
    };
}
```

The important UX property: app authors use `.or_throw()` at the point where
type information is still available, framework catch arms receive the event
they already understand, and the compiler — not a hand-written Throw impl —
carries the routing.

### Structured exception attrs with `pub error` + manual `Diagnostic`

When a framework exception needs to carry structured field-level detail (e.g.
validation errors per input field), declare a `pub error` with the fields you
need and provide a manual `core.Diagnostic` impl that projects the structured
shape to canonical JSON text.  The throw envelope's `params` JSON is what
catch-side code reads via `e.params.get(key).as_*()`.

> Background: `DiagnosticValue` / `DiagnosticEntry` / `core.diagnostic_entry`
> / `e.attrs["..."]` were the pre-Slice 7a path.  Slice 7a removed the public
> `DiagnosticValue` surface and Slice 7b retired the `e.attrs[k]` reader; user
> code should not reference them.  Use `pub error` + `core.Diagnostic` instead.

#### Throw side: declare the error and project to JSON

```drift
import std.core as core;
import std.json as json;

use trait core.Diagnostic;

pub error RestBadRequest {
    tag: String,
    message: String,
    fields: Array<FieldError>,
}

pub error RestInternal {
    tag: String,
    message: String,
}

pub struct FieldError {
    pub field: String,
    pub code: String,
}

// `Array<FieldError>` is not auto-projectable, so RestBadRequest carries a
// MANUAL Diagnostic impl that owns the JSON shape.  The projectability gate
// at the decl site (`E_PUB_ERROR_FIELD_NOT_PROJECTABLE`) requires this once
// you reach for any non-scalar field.
implement core.Diagnostic for RestBadRequest {
    pub fn to_json_text(self: &RestBadRequest) nothrow -> String {
        var fields_json = "[";
        var i = 0;
        while i < self.fields.len {
            if i != 0 { fields_json = fields_json + ","; }
            val f = &self.fields[i];
            fields_json = fields_json
                + "{\"field\":" + core.diagnostic_json_string(&f.field)
                + ",\"code\":" + core.diagnostic_json_string(&f.code)
                + "}";
            i = i + 1;
        }
        fields_json = fields_json + "]";
        return "{"
            + "\"fields\":" + fields_json
            + ",\"message\":" + core.diagnostic_json_string(&self.message)
            + ",\"tag\":" + core.diagnostic_json_string(&self.tag)
            + "}";
    }
}
```

Two things to note:

1. **Lex-utf8 key order matters for canonical envelopes.**  The
   compiler-synthesized impl for a `pub error` whose fields are all
   projectable lex-sorts keys; manual impls should follow the same convention
   so test fixtures pinning `e.params.encode_compact()` round-trip stably.
2. **Use the `core.diagnostic_json_*` helpers**
   (`diagnostic_json_string`, `_int`, `_uint`, `_bool`, `_float`,
   `_null`) — they emit canonical JSON text and handle string escaping for
   you.  Concatenate the pieces; do not re-quote already-canonical numeric
   output.

#### App code throws the error directly

```drift
fn validate_signup(body: &SignupRequest) -> Result<Account, RestBadRequest> {
    var fields: Array<FieldError> = [];

    if !is_valid_email(&body.email) {
        fields.push(FieldError(field = "email", code = "invalid-format"));
    }
    if body.age < 0 {
        fields.push(FieldError(field = "age", code = "must-be-non-negative"));
    }

    if fields.len > 0 {
        return Err(RestBadRequest(
            tag = "validation",
            message = "invalid input",
            fields = move fields,
        ));
    }

    return create_account(body);
}
```

Inside a `throws` function, `validate_signup(body).or_throw()` raises the
typed exception directly — every `pub error` gets a compiler-synthesized
`core.Throw for E` impl, so no extra trait wiring is needed.

#### Catch side: read fields via the params JSON cursor

```drift
fn dispatch(req: &rest.Request, ctx: &mut rest.Context) nothrow -> rest.Response {
    return try handle_signup(req, ctx) catch RestBadRequest(e) {
        // Typed scalar projection from the typed catch binder works for
        // declared scalar fields:
        val tag = e.tag;          // String
        val message = e.message;  // String

        // For the structured `fields` array, reach into the params JSON
        // (the params object only — call `e.encode_compact()` if you want
        // the full envelope shape `{event_code, event_fqn, params, ...}`).
        val params_json = e.params.encode_compact();
        return rest.validation_response_from_json(400, tag, message, &params_json);
    } catch RestInternal(e) {
        return rest.error_response(500, e.tag, e.message);
    };
}
```

The key envelope-side surfaces:

- `e.<declared_scalar_field>` — typed projection from params JSON for
  declared `Int` / `Uint` / `Bool` / `Float` / `String` fields on the
  matched event schema.  Variant / Array / nested-struct fields are NOT
  scalar-projectable; reach for the params JSON cursor instead.
- `e.params.get("k").as_int()` / `.as_string()` / `.as_bool()` /
  `.as_float()` — returns `Optional<T>` for the scalar accessor; `None` on
  missing key or wrong type.
- `e.params.encode_compact()` — full params JSON object as a String, for
  callers that want to forward the structured payload (e.g. into a JSON
  serializer downstream) without re-projecting key by key.
- `e.context.encode_compact()` — `^`-capture frames as a JSON array
  (innermost frame first).
- `e.encode_compact()` — full envelope JSON
  (`{event_code, event_fqn, params, context, stack}`).

### Auto-try regions: `throws -> T`

`fn f(...) throws -> T` is a body-wide auto-try region.  Inside the function,
`Result<X, E>` expressions where the expected type is `X` are auto-unwrapped
via `or_throw()`.  Phase 5a strict: `E` MUST be a `pub error` type — the
compiler synthesizes its `core.Throw` impl, which routes the typed event
into the surrounding exception flow.  Auto-try is compiler-owned — no
trait import required.

Use this for route handlers, request pipelines, and command handlers where most
intermediate `Result` errors should leave through the same exception boundary.

```drift
fn create_order(req: &rest.Request, ctx: &mut rest.Context) throws -> rest.Response {
    val body: CreateOrder = rest.json_body(req);
    val account: Account = rest.require_account(ctx);
    val order: Order = rest.create_order(ctx, &account, &body);

    return rest.json_response(201, order.to_json());
}
```

The explicit `.or_throw()` form is still useful when it makes the control-flow
conversion clearer, especially in examples or mixed code:

```drift
fn create_order(req: &rest.Request, ctx: &mut rest.Context) throws -> rest.Response {
    val body = rest.json_body(req).or_throw();
    val account = rest.require_account(ctx).or_throw();
    val order = rest.create_order(ctx, &account, &body).or_throw();

    return rest.json_response(201, order.to_json());
}
```

Pick one style per function. Use auto-try for dense straight-line pipelines; use
`.or_throw()` when readers benefit from seeing the conversion point.

### Stdlib behavior

Every stdlib error type is a `pub error`, so the compiler synthesizes its
`core.Throw` impl: `or_throw()` and auto-try unwrap the error type directly
into a typed catch arm.  For example, `Result<JsonNode, json.JsonErrorData>`
unwraps as `catch json:JsonErrorData(e)`; `Result<TcpStream, net.NetError>`
unwraps as `catch net:NetError(e)` with `e.kind` / `e.code` projection.

There is no longer a "wrap in `std.err:ResultError`" bridge in the stdlib
surface — every public Result error carrier is its own catchable event.

Example with JSON:

```drift
import std.json as json;

fn extract_status(payload: &String) -> String {
    val root = json.parse(payload).or_throw();

    return root.expect()
        .field("meta")
        .field("callback")
        .field("status")
        .string();
}

fn main() nothrow -> Int {
    val payload = "{\"meta\":{\"callback\":{\"status\":\"ok\"}}}";

    val status = try extract_status(&payload) catch json:JsonErrorData(e) {
        ""
    } catch json:JsonPathError(e) {
        ""
    } catch {
        ""
    };

    return 0;
}
```

A few things to notice:

- `extract_status` is not `nothrow`. Throwing methods (`or_throw`, the strict
  cursor’s `field/string/...`) propagate naturally through the call stack.
- `main` is `nothrow` because it has a `try ... catch` boundary that handles
  the exceptions it cares about and a catch-all for the rest.
- The JSON parse catch arm matches `json:JsonErrorData` (the synthesized
  throw of the Result's Err carrier).  Strict-cursor failures still raise
  `json:JsonPathError` directly.

### Designing a good `Throw` impl

Keep `Throw` impls boring and centralized. The impl is the module's default
policy for converting an error value into exception flow.

- Throw a domain exception when the caller can do something useful with the
  exception type, such as selecting an HTTP status or retry policy.
- Preserve stable machine-readable fields: tags, status codes, paths, service
  names, operation names, and retryability flags.
- Avoid parsing or generating prose as control flow. Human-readable `message`
  fields are fine, but catch arms should not have to parse them.
- Keep the mapping near the error type definition. Framework users should not
  need to learn a separate helper API just to get typed exception behavior.
- Do not add a `Throw` impl if the error has no sensible process-wide default.
  Use explicit `match` or `on_error` at the call site instead.

### Customized form: `Result.on_error(...)`

`on_error` gives the caller full control over what exception is thrown, without
requiring a `Throw` impl on the error type. Use it when the error type is not
yours or when this specific call needs extra context:

```drift
import std.json as json;

pub error ParseFailed {
    tag: String,
    path: String,
    request_id: String,
}

fn parse_required(payload: &String, request_id: String) -> json.JsonNode {
    return json.parse(payload).on_error(|e: json.JsonErrorData| => {
        throw ParseFailed(tag = e.tag, path = e.path, request_id = request_id);
    });
}

fn main() nothrow -> Int {
    val payload = "{...}";

    val root = try parse_required(&payload, "req-123") catch ParseFailed(e) {
        return 1;
    };

    return 0;
}
```

Reach for `on_error` instead of `or_throw` when:

- the caller wants a different event name than the Err carrier's
  synthesized throw (`or_throw()` throws the carrier directly under
  Phase 5a)
- the caller wants to add local context such as request id, tenant id, or input
  source
- the caller wants to project, rename, or redact fields before throwing
- the Result's Err type is a `pub error` from another package whose
  synthesized throw target isn't the event the caller wants to catch

### When `match` is still the right answer

Throwing flow is not the default for all `Result` values. Use `match` when the
error is a normal branch in local logic:

- The error case has a useful non-error continuation, such as parse failure
  falling back to a default config.
- The caller must return `Result` to its own caller.
- The error is part of a larger decision the function is already matching on.
- The function is `nothrow` and should stay that way.

The `or_throw` / `on_error` idiom is for the common app path where the user just
wants the value and failure should leave through a well-defined exception
boundary.

## Atomic ordering defaults (`std.sync`)

Use the weakest ordering that proves correctness:
- counters and telemetry: `Relaxed`
- read-modify-write ownership/state transitions: start with `AcqRel`, then relax case-by-case (for example Arc-style refcount: inc `Relaxed`, dec `Release` + acquire-on-zero before destroy)
- producer/consumer handoff: producer `Release` store, consumer `Acquire` load

```drift
import std.sync as sync;

fn bump(counter: &sync.AtomicInt) -> Int {
    return counter.fetch_add(1, sync.MemoryOrder::Relaxed());
}

fn claim_once(flag: &sync.AtomicBool) -> Bool {
    return flag.compare_exchange(false, true, sync.MemoryOrder::AcqRel(), sync.MemoryOrder::Acquire());
}

fn publish(ready: &sync.AtomicBool) -> Void {
    // Write shared data first, then publish availability.
    ready.store(true, sync.MemoryOrder::Release());
}

fn wait_until_ready(ready: &sync.AtomicBool) -> Bool {
    return ready.load(sync.MemoryOrder::Acquire());
}
```

## MPSC queue pattern (`std.sync::MpscQueue`)

Simple producer/consumer shape:

```drift
import std.concurrent as conc;
import std.core as core;
import std.sync as sync;

struct Event { id: Int }

fn producer(q: conc.Arc<sync.MpscQueue<Event>>, n: Int) nothrow -> Int {
    var i = 0;
    while i < n {
        val qr = q.get();
        while not qr.push(sync.handle<type Event>(cast<Uint>(i))) {
        }
        i = i + 1;
    }
    return n;
}

fn main() nothrow -> Int {
    val total = 100;
    var q = conc.arc(sync.mpsc_queue<type Event>(64));
    var qp = q.clone();
    var t = conc.spawn_cb(core.callback0(| | captures(move qp, copy total) => { return producer(move qp, total); }));
    var seen = 0;
    while seen < total {
        val qr = q.get();
        match qr.pop() {
            Some(_) => { seen = seen + 1; },
            default => {}
        }
    }
    match t.join() {
        Ok(v) => { if v != total { return 1; } },
        default => { return 2; }
    }
    return 0;
}
```

Notes:
- `MpscQueue` is many-producer / single-consumer.
- Current lock-free payload is `Handle<T>` (`sync.handle(...)`).

Matching runnable example: `examples/sync_mpsc_queue/main.drift`.

## UDP ping (self‑send)

```drift
import std.concurrent as conc;
import std.core as core;
import std.io as io;
import std.net as net;

pub fn main() nothrow -> Int {
	return try run_main() catch { 1 };
}

fn run_main() throws -> Int {
	val t = conc.Duration(millis = 5000);
	var addr = net.socket_addr("127.0.0.1", 0);
	val sock = net.udp_bind(&addr).or_throw();
	val port = sock.local_port();
	var to = net.socket_addr("127.0.0.1", port);
	var buf = io.buffer(4);
	io.buffer_write(&mut buf, 0, cast<Byte>(80));
	io.buffer_write(&mut buf, 1, cast<Byte>(73));
	io.buffer_write(&mut buf, 2, cast<Byte>(78));
	io.buffer_write(&mut buf, 3, cast<Byte>(71));
	val _ = sock.send_to(&to, &buf, t).or_throw();
	var from = net.socket_addr("127.0.0.1", 0);
	val _ = sock.recv_from(&mut from, &mut buf, t).or_throw();
	sock.close(t).or_throw();
	return 0;
}
```

## TCP echo (single client)

```drift
import std.concurrent as conc;
import std.core as core;
import std.io as io;
import std.net as net;

pub fn main() nothrow -> Int {
	return try run_main() catch { 1 };
}

fn run_main() throws -> Int {
	val t = conc.Duration(millis = 5000);
	var addr = net.socket_addr("127.0.0.1", 0);
	val listener = net.listen(&addr, t).or_throw();
	val port = listener.local_port();
	var server = conc.spawn(| | captures(move listener, copy t) => {
		return try (| | => {
			val s = net.accept(&listener, t).or_throw();
			var buf = io.buffer(16);
			val _ = s.read(&mut buf, t).or_throw();
			val _ = s.write(&buf, t).or_throw();
			s.close(t).or_throw();
			return 0;
		})() catch { 1 };
	});
	var caddr = net.socket_addr("127.0.0.1", port);
	val c = net.connect(&caddr, t).or_throw();
	var wbuf = io.buffer(5);
	io.buffer_write(&mut wbuf, 0, cast<Byte>(72));
	io.buffer_write(&mut wbuf, 1, cast<Byte>(101));
	io.buffer_write(&mut wbuf, 2, cast<Byte>(108));
	io.buffer_write(&mut wbuf, 3, cast<Byte>(108));
	io.buffer_write(&mut wbuf, 4, cast<Byte>(111));
	val _ = c.write(&wbuf, t).or_throw();
	var rbuf = io.buffer(5);
	val _ = c.read(&mut rbuf, t).or_throw();
	c.close(t).or_throw();
	val _ = server.join().or_throw();
	return 0;
}
```

## Iterate, then mutate

Many container iterators are invalidated by mutation. When you need to delete or
update entries while scanning, collect keys first and apply changes afterward.

```drift
fn prune(map: &mut HashMap<String, Int>) -> Void {
    var dead = Array<String>::new();
    for (k, v) in map.iter() {
        if *v == 0 { dead.push(k.clone()); }
    }
    for k in dead.iter() {
        map.remove(k);
    }
}
```

This keeps iterator invalidation out of your way and makes the intent obvious.

## Keep borrows small and explicit

When a function gets complex, it helps to shrink the lifetime of borrows by
using local scopes. This avoids accidental conflicts and reads clearly.

```drift
fn update_user(db: &mut Db, id: Int, patch: Patch) -> Bool {
    {
        val u = db.get_mut(id);
        if u.is_none() { return false; }
        u.unwrap().apply(patch);
    }
    db.mark_dirty(id);
    true
}
```

The inner block makes it obvious when the borrow ends.

## Moving a field out of a struct

Drift does not have Rust-style partial-field moves. Writing `move s.field`
is rejected at compile time:

```text
move of a projected place is not supported in v1;
move a local/param or use swap/replace
```

You have two idiomatic patterns, depending on whether the field is
*sometimes* taken or *always* swapped.

### Pattern A — `Optional<T>` field with `take()` for "sometimes taken"

Model fields that are conceptually take-once-then-empty as `Optional<T>`.
Taking the value out leaves `Optional::None`; later code can branch on
absence cleanly, and the struct's destructor still runs over a fully-formed
field.

```drift
import std.core as core;
import std.mem as mem;

struct Token { /* ... */ }

struct Session
    require Self is core.Destructible
{
    token: Optional<Token>,
    /* ... other fields ... */
}

// Typed helper for the replacement value. Constructing the niladic
// variant in a slot where inference is weak (e.g. the second argument
// of `mem.replace`) needs an explicit type — either `Optional<T>::None()`
// at the call site or, more readably, a `nothrow -> Optional<T>` helper
// whose return slot supplies the type.
fn _none_token() nothrow -> Optional<Token> {
    return Optional<Token>::None();
}

implement Session {
    pub fn take_token(self: &mut Session) -> Optional<Token> {
        // Explicit generic on `mem.replace` avoids inference ambiguity
        // with the `&mut Optional<Token>` first argument.
        return mem.replace<type Optional<Token> >(&mut self.token, _none_token());
    }
}

implement core.Destructible for Session {
    pub fn destroy(var self: Session) nothrow -> Void {
        // Runs over a fully-formed Session. self.token is either Some(_) or
        // None — both are valid; no special "this field was moved" branch.
        return;
    }
}
```

> **Note on Optional construction.** `Optional` is the prelude-level
> generic — use `Optional<T>` directly, not `core.Optional<T>`. For
> *value* construction of the niladic variant, prefer `Optional::None()`
> when the surrounding slot's type is known (e.g. a `return` in a
> `nothrow -> Optional<T>` function), or `Optional<T>::None()` /
> a typed helper when inference is weak (function arguments,
> `mem.replace` second arg, struct literals with multiple Optional
> fields). In **pattern position** — `match`, `catch`, destructuring —
> the bare form `Optional::None` is fine; the pattern resolver has the
> scrutinee's type to work with.

This is what the diagnostic on a `Destructible` aggregate is steering you
toward:

```text
cannot move field 'token' out of 'Session': Session has a custom destructor
hint: store the field as Optional<Token> and use take()
hint: or swap a replacement value in with std.mem.replace
```

### Pattern B — `mem.replace` for "always swapped" fields

If the field always has a meaningful value and you just want to lift the
old one out and put a fresh one in, call `std.mem.replace` directly. This
works on any field type and has zero runtime overhead beyond the swap.

```drift
import std.mem as mem;

fn rotate_buffer(s: &mut MySession, fresh: ByteBuffer) -> ByteBuffer {
    return mem.replace(&mut s.buf, fresh);
}
```

Use this when the field has no natural "absent" state and you don't want
to pay for an `Optional` discriminant on every read.

### When `Arc<T>` is *not* the answer

`Arc<T>` solves shared ownership across multiple owners. It is **not** a
workaround for "I want to move one field out of a locally-owned struct."
For consume-self patterns (e.g. `throw_self`, builder finalizers), there is
no second owner to share with — adding `Arc` only buys a heap allocation
and a refcount on every field read.

Reach for `Arc` when the field genuinely needs multiple live owners
(e.g. shared configuration, registries, callbacks that outlive the
constructing scope). For everything else, `Optional<T>` (Pattern A) or
`mem.replace` (Pattern B) is the right tool.

### Don't reason about "what's left in the field"

After a swap or `take()`, the static state of the field is what it says:
`None` for an `Optional`, the swapped-in value for `mem.replace`. Don't
write code that assumes a moved-out slot holds any particular value —
runtime neutralization is type-specific (see spec §4.13.4) and is
implementation machinery, not API. Test for the values you put there.

## Prefer structs for product data; use variants to tag meaning

If a type is just “some fields grouped together,” use a `struct`. If you want a
named constructor, pattern-matching boundary, or future extensibility, use a
`variant` (even with a single arm).

```drift
struct Point { x: Int, y: Int }
// Construct: Point(x = 2, y = 3)

variant UserId {
    UserId(Int),
}
// Construct: UserId(42); match on UserId(...)
```

Single-arm variants are useful for semantic tagging (“this Int is a UserId”),
and they keep the door open to add more cases later without changing the type’s
name or its call sites.

## Prefer “own the data” for long-lived callbacks

If a callback will live longer than the scope it was created in, capture owned
data (or an `Arc`) instead of borrowing. This keeps lifetimes simple and avoids
surprises later.

```drift
fn install(bus: &mut EventBus, cfg: Config) -> Void {
    var cfg = conc.arc(cfg);
    var cfg2 = cfg.clone();
    bus.on_tick(core.callback0(|| => {
        cfg2.refresh();
    }));
}
```

It’s slightly more verbose up front, but it scales well as the program grows.

## Method overload resolution by parameter type

A struct or builtin type can declare multiple methods with the same name but
different non-receiver parameter types. The compiler picks the right overload
based on the call’s argument types.

```drift
pub struct Box {
    v: Int
}

implement Box {
    pub fn pick(self: &Box, k: &String) nothrow -> Int {
        return self.v + 100;
    }
    pub fn pick(self: &Box, k: &Array<String>) nothrow -> Int {
        return self.v + 200 + k.len;
    }
    pub fn pick(self: &Box, k: Int) nothrow -> Int {
        return self.v + 300 + k;
    }
}

fn main() nothrow -> Int {
    val b = Box(v = 10);
    val a = b.pick("hello");           // → 110 (String overload)
    val segs: Array<String> = ["x", "y"];
    val c = b.pick(segs);              // → 212 (Array<String> overload)
    val d = b.pick(42);                // → 352 (Int overload)
    return 0;
}
```

A common pattern is a concrete overload plus a generic fallback. The concrete
one wins for arguments it can match exactly; the generic one catches everything
else.

```drift
implement Box {
    pub fn pick(self: &Box, k: &String) -> Int { return 100; }
    pub fn pick<T>(self: &Box, k: T) -> Int { return 999; }
}
b.pick("hello")  // → 100 (concrete &String overload)
b.pick(42)       // → 999 (generic fallback)
```

Resolution rules (v1):

1. Filter by arity.
2. Filter by receiver compatibility (`self` mode, with optional auto-borrow).
3. Prefer exact non-receiver parameter matches over arity-only matches.
4. Within exact matches, prefer methods *without* their own type parameters
   over method-level generic fallbacks.
5. Multiple exact matches → `ambiguous method` error.
6. No exact match across multiple candidates → `no matching overload for
   method '…'` error (the diagnostic includes the call’s argument types).

Limitations:

- Impl-block specificity is not ranked. `implement<T> Box<T>` and
  `implement Box<Int>` declaring the same method are ambiguous, not
  most-specific-wins.
- Method-level generics with their own `<T>` type parameters fall back to the
  existing generic dispatch path; trait-bound disambiguation
  (`require T is Trait`) still works there.

## Call-site auto-borrow for `&T` parameters

When a function or method parameter is declared `&T` (a shared borrow), the
call site does not need an explicit `&` prefix. Both forms compile, and the
bare form is the preferred style:

```drift
fn greet(name: &String) nothrow -> Void { ... }

greet("alice")     // preferred — auto-borrow
greet(&"alice")    // also valid — explicit borrow

b.pick("hello")    // preferred
b.pick(&"hello")   // also valid
```

This applies only to shared borrows. `&mut T` parameters still need an
explicit `&mut` at the call site so mutation is visible at the use site.

## Call-site auto-dup for value parameters from `&T` / `&mut T`

The inverse coercion is also permitted: when a function parameter is
declared `T` (a value) and the argument is `&T` or `&mut T`, the call
site auto-dereferences (and, for non-Copy `ConstShare` types, also
auto-shares) so callers don't have to spell `.clone()` at every
boundary.

```drift
fn take(s: String) nothrow -> Int { return s.byte_length(); }

fn caller(borrowed: &String) nothrow -> Int {
    return take(borrowed);     // preferred — auto-dup
}
```

The rule only fires when `T` is `Copy` or proves `ConstShare`. Types
that satisfy neither (e.g. a struct with a user `Destructible` impl
and no `ConstShare`) still require an explicit conversion at the
call site — there is no implicit move out of the referent.

For `String`, the dup is the same O(1) refcount bump that `clone()`
produces. This is auto-dup of `Copy` / `ConstShare` owners — distinct
from the expression-form `share x`, which is the explicit form for
types implementing `Share`. The two coexist: auto-dup is implicit and
triggered by the `&T → T` parameter shape; `share x` is explicit and
required where `Share` semantics differ from `Copy` / `ConstShare`.
Mirrors the existing field-projection auto-dup that already lets you
read a `Copy` / `ConstShare` field through a borrowed struct.

## Cheap `String` clone

`String` is ARC-backed. To produce an owned `String` from a borrowed
`&String`, call `clone()`:

```drift
fn extract(borrowed: &String) nothrow -> String {
    return borrowed.clone();
}
```

`clone()` is an O(1) refcount increment (`drift_string_retain`), not a
byte-by-byte copy. Don’t write manual byte-rebuild helpers — they allocate
a new buffer for no benefit.

## Named patterns: what looks like a restriction but is a design choice

Three rules below look like "the compiler won't let me do X" but are
deliberate shape choices in the language. Each has a small, local
pattern that captures the intent. These are migration heuristics —
when you hit one of these compile errors, the workaround isn't a
workaround, it's the supported shape.

### Typed-catch binders are projection views, not addressable structs

In a typed catch arm (`catch ParseError(e) { … }`), the binder `e` is
*not* an in-memory struct. It's a thin view over the error envelope
plus the declared schema: each field access lowers to a compiler-owned
helper that decodes the relevant slice of the canonical params JSON
on demand. There is no offset / tag / line slot to take the address
of — those values are computed at the moment of access.

Valid: read scalar fields by value, including the envelope methods
that *do* exist on the Error itself.

```drift
try {
    ...
} catch ParseError(e) {
    val line = e.line;
    val tag  = e.tag;
    val env  = e.encode_compact();   // envelope JSON method
    log.error("parse-failed", {"tag": tag, "line": line, "env": env});
}
```

Invalid: take the address of the binder or one of its declared
fields.

```drift
try {
    ...
} catch ParseError(e) {
    val line_ref: &Int    = &e.line;    // no addressable storage
    val binder_ref: &ParseError = &e;   // binder is not a struct
}
```

Pattern when you need a borrow: bind to a local first. The local has
real storage, the borrow is well-defined.

```drift
} catch ParseError(e) {
    val line = e.line;                  // materialize
    val line_ref: &Int = &line;         // borrow the local
    consume(line_ref);
}
```

### A function returning `&T` must derive that `&T` from a `&T` parameter (MVP escape policy)

Drift's MVP doesn't have a full lifetime model. Instead, ref-escape
soundness is enforced by a simpler rule: when a function returns
`&T`, the returned reference must be traceable back to a `&T` (or
`&mut T`) *parameter* of the same function. A `&T` constructed from
a global / a local / a registry lookup has no parameter to bound its
lifetime to, so the compiler rejects the return.

Valid: the returned ref comes from a ref param.

```drift
fn first_byte(s: &String) nothrow -> &Byte {
    return s.byte_at(0);                // derived from `&s`
}

fn project(req: &Request) nothrow -> &String {
    return &req.body;                   // derived from `&req`
}
```

Invalid: return a `&T` synthesized inside the function with no ref
parameter to anchor it.

```drift
fn current_config() nothrow -> &Config {
    val cfg = global_registry().get<type Config>();
    return &cfg;                        // no &param to bound this ref
}
```

Patterns when you can't pass a `&Registry` (or similar) as a
parameter:

- **Return by value** for small `Copy` types. The caller owns the
  value; lifetime is its own problem.

  ```drift
  fn current_log_level() nothrow -> log.Level {
      return global_registry().get<type log.Level>();
  }
  ```

- **Pass the source ref in** when the caller already holds it.

  ```drift
  fn timeout_from(cfg: &Config) nothrow -> &conc.Duration {
      return &cfg.request_timeout;
  }
  ```

- **Hand back an Arc / handle** for shared ownership of non-Copy
  data. The Arc carries its own lifetime.

  ```drift
  fn current_logger() nothrow -> conc.Arc<log.Logger> {
      return global_registry().get_arc<type log.Logger>();
  }
  ```

A full lifetime model that would lift this restriction is a planned
follow-up, not something teams should code around with workarounds
today.

### Expression-position `match` arms are value-only

`match` in expression position is Drift's multi-way value
expression — shaped like a ternary, generalized over patterns.
Every arm must evaluate to a normal value. Control-flow statements
(`return`, `throw`, `break`, `continue`, `rethrow`) are not
permitted inside expression-position arms; if a branch needs to
exit the enclosing function, use statement-position `match`
instead.

Valid: every arm produces a value of the match's result type.

```drift
val x: Int = match opt {
    Some(v) => { v },
    None    => { 0 }
};
```

Invalid: an arm exits via control flow instead of producing a value.

```drift
val x: Int = match opt {
    Some(v) => { v },
    None    => { return 0; }            // not allowed
};

val y: Int = match r {
    Ok(v)  => { v },
    Err(e) => { throw e; }              // not allowed
};
```

Pattern for control flow in arms: use **statement-position `match`**.

```drift
match opt {
    Some(v) => { return v; },
    None    => { return 0; }
}

match r {
    Ok(v)  => { return v; },
    Err(e) => { throw e; }
}
```

Pattern for "assign in some arms, control-flow in others" — combine
statement-position `match` with a `var` declared before it.

```drift
var x: Int = 0;
match r {
    Ok(v)  => { x = v; },
    Err(e) => { throw e; }
}
return x;
```

The mental model: expression `match` is a *value* (like a ternary,
or a `let … in` in expression-oriented languages); statement
`match` is a control-flow construct. Don't try to make one play
the other's role.
