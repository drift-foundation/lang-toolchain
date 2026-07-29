# Behavior probes — reject-redundant-call-borrows

Every claim in PLAN.md about *current* compiler behavior is pinned by one of these
single-file programs, compiled against the in-tree toolchain (0.33.90 / ABI 22):

```
bin/driftc --dev --stdlib-root stdlib probes/<name>.drift --entry main::main -o /tmp/<name>.bin
```

| Probe | Question | Result (0.33.90) |
|---|---|---|
| `e1_replace_bare` | `mem.replace(x, 5)` bare place | **error** `replace requires a concrete element type` — inference leans on the ref-typed arg |
| `e1b_replace_explicit` | `mem.replace(&mut x, 5)` | OK, runs (exit 15) |
| `e1c_replace_bare_typearg` | `mem.replace<type Int>(x, 5)` bare + explicit type arg | **error** `replace expects &mut T as the first argument` — no auto-borrow on the intrinsic path even with T pinned |
| `e2_swap_bare` | `mem.swap(a, b)` bare | **error** `swap requires a concrete element type` (fails before the structural `&mut` check) |
| `e3_concrete_beats_generic_freefn` | free-fn set `pick(&String)`+`pick<T>(T)`, bare call | **error** `ambiguous call` — no concrete-beats-generic tiebreak for free fns |
| `e3b_pick_explicit` | same set, explicit `pick(&s)` | **error** `ambiguous call` — mixed free-fn sets are pre-broken in BOTH spellings |
| `e4_iface_bare_arg` | interface method `k.write(s)` bare at declared `rec: &String` | **error** `Sink.write argument 1 type mismatch` — interface dispatch has no auto-borrow |
| `e5_rvalue_bare_shared` | `read("alice")` bare literal at `&String` | OK, runs (exit 5) |
| `e5b_rvalue_bare_mut` | `edit(mk())` bare rvalue at `&mut String` | **error** `borrow requires an addressable place; bind to a local first` |
| `e6a_rvalue_explicit_life` | drop timing of `probe(&mk(&mut sess))` temp (Destructible counter) | exit 1 → temp alive after the statement, drops at scope end |
| `e6b_rvalue_bare_life` | drop timing of `probe(mk(&mut sess))` temp | exit 1 → **identical**: scope-end drop in both spellings |
| `e7_method_concrete_pair_bare` | concrete method pair `pick(&String)`/`pick(String)`, bare call | **error** `ambiguous method 'pick'` — bare cannot disambiguate; R2 ban is forced |
| `e8a/e8b_fnptr_*` | (superseded — can-throw `Fn` type in a nothrow `main`; probe bug, kept for the record) | both fail on throw-mode, not on the argument |
| `e8c_fnptr_nothrow_explicit` | `val f: Fn(&String) nothrow -> Int = read_len; f(&s)` | OK, runs (exit 5) |
| `e8d_fnptr_nothrow_bare` | same, bare `f(s)` | **MISCOMPILE** — type checker accepts, clang rejects the emitted IR (`'%.t3' defined with type '%DriftString' but expected 'ptr'`). Latent defect independent of this proposal; see PLAN.md adjacent-defect note |
| `e9_iface_bare_concrete` | `drive(f)` bare concrete `FileSink` at declared `&Sink` param | **error** `no matching overload ... with args [FileSink]` — no auto-borrow+widen; `drive(&f)` is today's only spelling (decision D7) |
| `e10_shared_mut_overload_bare` | free-fn pair `peek(&Int)`/`peek(&mut Int)`, bare `peek(x)` with `var x` | **error** `ambiguous call` — supports the mode-erasure form of R2 |
| `e11_fnty_typevar` | can fn-pointer types carry typevars? `fn apply_it<T>(f: Fn(T) nothrow -> T, v: T)` | OK, runs (exit 42) — **R-7 = yes**; `FUNCTION` TypeIds with REF params are not always provenance-decidable (feeds D8) |
