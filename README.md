# Drift

Drift is a systems programming language focused on deterministic resource management, explicit ownership, and ergonomic concurrency. It combines C++-style RAII with Rust-like borrowing rules, while keeping the syntax compact and readable.

- **Predictable lifetimes** – values are dropped exactly once; `val` and `var` communicate ownership.
- **No hidden runtime** – everything is explicit: imports, errors, effects, memory.
- **Virtual-thread concurrency** – synchronous-looking code scales via lightweight threads and structured scopes.

📖 **Full specification:** [docs/drift-lang-spec.md](docs/drift-lang-spec.md)

## Quick Tour

### Hello Drift

```drift
import sys.console.out

fn main() returns Void {
    val greeting = "hello, drift"
    out.writeln(greeting)
}
```

### Structs, ownership, and methods

```drift
struct Point { x: Int64, y: Int64 }

implement Point {
    fn move_by(ref mut self, dx: Int64, dy: Int64) returns Void {
        self.x += dx
        self.y += dy
    }
}

fn translate(ref p: Point, dx: Int64, dy: Int64) returns Void {
    p.x += dx
    p.y += dy
}
```

### Collection literals with type inference

```drift
fn numbers() returns Array<Int64> {
    val xs = [1, 2, 3]          // inferred Array<Int64>
    var ys: Array<Int64> = [4, 5, 6]
    ys[1] = 42                 // requires `var`
    return xs + ys
}
```

### Concurrency at eye level

```drift
import std.concurrent as conc

fn main() returns Void {
    conc.scope(fn(scope: conc.Scope) returns Void {
        val user = scope.spawn(fn() returns User { load_user(42) })
        val data = scope.spawn(fn() returns Data { fetch_data() })
        render(user.join(), data.join())
    })
}
```

## Getting Started

The repository ships with a prototype interpreter. From the repo root:

```bash
./drift.py examples/hello.drift
```

See the full language specification in [docs/drift-lang-spec.md](docs/drift-lang-spec.md) for grammar, semantics, and additional examples.
