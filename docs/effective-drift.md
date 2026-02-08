# Effective Drift

Common idioms for programs that won’t ghost you in prod.

## Shared state + callbacks (Arc + Mutex)

When you need multiple handlers that all mutate the same receiver object, put
the receiver in an `Arc` and wrap it in a `Mutex`. Each handler captures a
clone, takes the lock, and mutates through a guard. This avoids ownership
conflicts and keeps the emitter dumb.

```drift
import std.core as core;
import std.concurrency as conc;

struct StateMachine { state: Int }

implement StateMachine {
    fn on_signal_x(self: &mut Self, e: &Event) -> Void { self.state = self.state + 1; }
    fn on_signal_y(self: &mut Self, e: &Event) -> Void { self.state = self.state - 1; }
    fn on_signal_z(self: &mut Self, e: &Event) -> Void { self.state = 0; }
}

fn register_handlers(bus: &mut EventBus) -> Void {
    var sm: conc.Arc<conc.Mutex<StateMachine>> = conc.arc(conc.mutex(StateMachine(state = 0)));
    var sm_x: conc.Arc<conc.Mutex<StateMachine>> = sm.clone();
    var cb_x: core.Callback1<&Event, Void> = core.callback1(|e: &Event| captures(move sm_x) => {
        var guard = conc.lock(sm_x);
        guard.get_mut().on_signal_x(e);
        return;
    });
    bus.on_x(move cb_x);
    var sm_y: conc.Arc<conc.Mutex<StateMachine>> = sm.clone();
    var cb_y: core.Callback1<&Event, Void> = core.callback1(|e: &Event| captures(move sm_y) => {
        var guard = conc.lock(sm_y);
        guard.get_mut().on_signal_y(e);
        return;
    });
    bus.on_y(move cb_y);
    var sm_z: conc.Arc<conc.Mutex<StateMachine>> = sm.clone();
    var cb_z: core.Callback1<&Event, Void> = core.callback1(|e: &Event| captures(move sm_z) => {
        var guard = conc.lock(sm_z);
        guard.get_mut().on_signal_z(e);
        return;
    });
    bus.on_z(move cb_z);
}
```

Notes

- This works with owned callbacks and does not rely on borrowed captures.

## Read a file

Use `file_builder(...).read(true).write(false)` and keep timeout on the
configured handle.

```drift
import std.concurrent as conc;
import std.core as core;
import std.io as io;
use trait core.Try;

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
use trait core.Try;

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

pub fn main() nothrow -> Int {
    val cfg_builder = log.config_builder();
    cfg_builder.sink(log.stderr_sink());
    cfg_builder.min_level(log.Level::Info());
    val cfg = cfg_builder.build();
    log.init(cfg);

    log.info("auth-failed", {"user": "alice", "reason": "bad-password", "src": meta.caller()});
    log.error("db-timeout", {"host": "db-main", "retryable": true, "src": meta.caller()});
    log.flush();
    return 0;
}
```

For formatter customization, see `lang/examples/logging/pluggable_formatter.drift`.

## Atomic ordering defaults (`std.sync`)

Use the weakest ordering that proves correctness:
- counters and telemetry: `Relaxed`
- read-modify-write ownership/state transitions: `AcqRel` (failure usually `Acquire` or `Relaxed`)
- producer/consumer handoff: producer `Release` store, consumer `Acquire` load

```drift
import std.sync as sync;

fn bump(counter: &sync.AtomicInt) -> Int {
    return counter.fetch_add(1, sync.MemoryOrder::Relaxed());
}

fn claim_once(flag: &sync.AtomicBool) -> Bool {
    var expected = false;
    return flag.compare_exchange(&mut expected, true, sync.MemoryOrder::AcqRel(), sync.MemoryOrder::Acquire());
}

fn publish(ready: &sync.AtomicBool) -> Void {
    // Write shared data first, then publish availability.
    ready.store(true, sync.MemoryOrder::Release());
}

fn wait_until_ready(ready: &sync.AtomicBool) -> Bool {
    return ready.load(sync.MemoryOrder::Acquire());
}
```

## UDP ping (self‑send)

```drift
import std.concurrent as conc;
import std.core as core;
import std.io as io;
import std.net as net;
use trait core.Try;

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
use trait core.Try;

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
