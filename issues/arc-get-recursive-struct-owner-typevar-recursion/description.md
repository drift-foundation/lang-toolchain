Summary: `RecursionError` in `call_resolver._has_owner_typevar` when resolving `Arc<T>.get()` for a `T` that recursively contains itself through an `Array<T>` field

Classification
- Compiler bug (correctness, fail-fast crash — Python `RecursionError`, not a diagnosed compile error)
- Priority: high (crashes on a minimal, dependency-free repro; blocks any real program that shares a
  tree-shaped value across fibers via `std.concurrent.Arc`, which is the obvious/idiomatic way to do
  it)
- Found on driftc 0.33.78 | abi 20 (certified snapshot `20260710-151831-drift-workflows-712b1d0`)

Symptom
- Compiling any program that calls `.get()` (or, going by the trace, presumably any generic method) on
  a `std.concurrent.Arc<T>` where `T` is a struct with a field of type `Array<T>` (or, transitively, a
  struct containing such a field) crashes the compiler with:

  ```
  File ".../checker/call_resolver.py", line 3736, in resolve_method_call
      if any(_has_owner_typevar(t) for t in param_type_ids) or _has_owner_typevar(ret_id):
  File ".../checker/call_resolver.py", line 3733, in _has_owner_typevar
      if _has_owner_typevar(sub):
  File ".../checker/call_resolver.py", line 3733, in _has_owner_typevar
      if _has_owner_typevar(sub):
  [Previous line repeated 32744 more times]
  File ".../checker/call_resolver.py", line 3729, in _has_owner_typevar
      td = ctx.type_table.get(tid)
  RecursionError: maximum recursion depth exceeded
  ```

- This is a raw Python `RecursionError` surfaced through driftc's CLI wrapper (full interpreter
  traceback printed to stderr) — not a diagnosed `error: ...` with a source location. Any real program
  hitting this path gets an opaque crash, no source pointer.

Minimal reproduction (no project dependencies, 18 lines, dependency-free — copy verbatim to a file and
compile with `driftc --target-word-bits 64 -o /tmp/out repro.drift`):

```drift
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

struct Node { kind: Int, children: Array<Node> }
struct Holder { root: Node }

fn mknode() nothrow -> Node {
    var kids: Array<Node> = [];
    return Node(kind = 1, children = move kids);
}

pub fn main() nothrow -> Int {
    val h = Holder(root = mknode());
    val a = conc.arc(h);
    val p = a.get();
    console.println("ok");
    return 0;
}
```

- The crash reproduces identically whether `Arc` wraps the self-referential struct (`Node`) directly or
  wraps it one level removed through a non-generic, non-recursive wrapper struct (`Holder`) — both
  shapes were tried and both crash the same way. What matters is that `T` (or something reachable from
  `T`) recursively contains `T` via an `Array<T>` field — i.e. any ordinary tree/AST node type.

- This was originally discovered in a real project (`drift-query`) trying to share a
  `dqc.runtime.RuntimeProgram` value (which transitively holds a parsed-plan tree,
  `PNode { ..., children: Array<PNode> }`) across worker fibers via `conc.Arc<RuntimeProgram>` +
  `.get()`. The exact same crash reproduces there; the 18-line repro above isolates it down to just the
  self-referential-struct-through-`Array`-field shape, with zero project-specific dependencies.

Why this is a real bug, not test infrastructure
- The error is a Python `RecursionError` raised by the compiler itself during type-checking, not a
  test/assertion failure or a diagnosed user-facing error.
- It is a fail-fast interpreter crash (full Python traceback on stderr) — there is no way for a user to
  work around it other than avoiding `Arc<T>.get()` (or presumably any generic method call) entirely for
  any `T` shaped like a tree/AST node — which is one of the most ordinary struct shapes there is, and
  `Arc` is the standard/idiomatic way to share an immutable value across `std.concurrent.spawn`ed
  fibers.
- Deterministic — reproduces every time on the minimal repro above, no flake.

Verification
- Reproduced directly against the certified toolchain snapshot
  `~/opt/drift/certified/current/toolchain` (driftc 0.33.78, abi 20) via `driftc --version`.
- Confirmed with the full project repro (`RuntimeProgram` sharing in `drift-query`'s new
  `dqc.invoke` module) first, then narrowed to the 18-line dependency-free repro above by binary-search
  (stripped project imports/types one at a time until only `Node`/`Holder`/`Arc.get()` remained and the
  crash still reproduced identically, same recursion depth ~32744).
- Also confirmed the crash is specific to the *recursive-through-Array* shape, not merely "a struct
  wrapped in Arc" — an earlier, non-recursive repro attempt (`struct Big { a: Int, b: String,
  c: Array<Int> }` wrapped the same way, called via `.get()` inside similarly deep nested
  if/match blocks) compiled and ran fine with no crash.

Likely cause
- `_has_owner_typevar` (`call_resolver.py:3729-3736`) walks a type's substitution chain (`sub`) looking
  for an "owner" typevar, presumably to decide whether a generic method's parameter/return types still
  depend on unresolved generics. For a self-referential struct type (`Node` containing `Array<Node>`),
  the type-table entry for `Node` (or a substituted/instantiated copy of it created while resolving
  `Arc<Node>::get`) appears to end up with a `sub` chain that points back to itself — i.e. `type_table
  ["Node"].sub` (or an instantiated variant thereof) is (transitively) itself, so the walk in
  `_has_owner_typevar` never terminates. The walk has no visited-set / cycle guard, so any genuinely
  cyclic type (any tree-shaped struct is exactly this, by definition, once you look through `Array<T>`)
  sends it into infinite recursion until Python's interpreter recursion limit trips.

Pointers for fix
- `lang/driftc/checker/call_resolver.py`, function `_has_owner_typevar` (around line 3729-3736 in the
  0.33.78/abi20 build).
- Add a visited-`tid`-set guard to the walk (the standard fix for any "walk a substitution/alias chain"
  loop that can legitimately be cyclic — struct self-reference through a container type is completely
  ordinary and must be supported, not rejected): if `tid` is already in the visited set when
  `_has_owner_typevar` is entered, return `False` (or whatever the sane base case is) instead of
  recursing again.
- Given the container is `Array<T>` (not `T` directly — Drift doesn't have unboxed self-reference
  without a container, so this is the *only* shape self-referential structs take), the fix should be
  general: any recursive walk over `type_table`/substitution chains anywhere near generic method
  resolution should be audited for the same missing-cycle-guard pattern, not just this one call site —
  this exact class of struct (tree/AST node with `children: Array<Self>`) is extremely common in real
  programs (parsers, ASTs, DOM-like trees, etc.), so any other unguarded recursive walk over the same
  data is a similarly-shaped latent crash waiting for someone to wrap such a type in `Arc`/`Mutex`/etc.
  and call a generic method on it.

Test plan
- Add the 18-line repro above (or a close variant) as a new compiler test exercising:
  `Arc<T>.get()` where `T` (or a field's element type) is a self-referential struct via `Array<Self>`.
  Assert successful compilation (and, ideally, successful execution printing `ok`).
- Regression-worthy: this is a minimal, general-purpose shape (not tied to any project-specific type),
  so it belongs in the core `std.concurrent`/generics test suite, not just a one-off project regression.

Owner
- Unassigned. Slot into the type-checker / call-resolver / generics queue.

Cross-references
- Discovered 2026-07-10 while implementing `drift-query`'s `work/write-activity-api` Slice 10
  (`src/dqc/invoke.drift`), which needs to share a `dqc.runtime.RuntimeProgram` (itself holding a parsed
  plan tree, `PNode { kind, text, ty, alias, src, ival, ref, children: Array<PNode> }`) across worker
  fibers spawned per write-scope via `conc.Arc<RuntimeProgram>` + `.get()`. Working around this in that
  project by avoiding `Arc<RuntimeProgram>.get()` entirely (see that project's own notes/commit history
  for the workaround shape actually shipped).
