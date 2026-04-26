# Drift Standard Library Spec (Draft)

## Scope
This document specifies the MVP surface for standard library modules used by the
compiler and core tooling. It is the source of truth for iterator traits,
collections, algorithms, and error events referenced by language lowering.

## Modules
- `std.iter`: iterator traits and `for` lowering hooks.
- `std.containers`: core container types.
- `std.algo`: algorithms (functions only).
- `std.core.cmp`: comparison traits and operator lowering paths.
- `std.err`: standard error/exception events used by stdlib APIs.
- `std.mem`: unsafe pointer primitives and trusted raw storage helpers.
- `std.sync`: atomics and memory ordering primitives.

## std.iter

### Iterable
```drift
module std.iter;

trait Iterable<Src, Item, Iter> {
	fn iter(src: Src) returns Iter
	require Iter is SinglePassIterator<Item>
}
```

Resolution:
- `std.iter.Iterable.iter(expr)` is trait dispatch (UFCS), not a static function lookup.
- Coherence: for any concrete `Src`, at most one applicable `Iterable<Src, Item, Iter>` impl may exist.

### Iterators
```drift
trait SinglePassIterator<T> {
	fn next(self: &mut Self) returns Optional<T>
}

trait MultiPassIterator<T> require Self is SinglePassIterator<T> {
	require Self is Copy
}

trait BidirectionalIterator<T> require Self is MultiPassIterator<T> {
	fn prev(self: &mut Self) returns Optional<T>
}

trait RandomAccessReadable<T> {
	fn len(self: &Self) returns Int
	fn compare_at(self: &Self, i: Int, j: Int) returns Int
}

trait RandomAccessPermutable<T> require Self is RandomAccessReadable<T> {
	fn swap(self: &mut Self, i: Int, j: Int) returns Void
}
```

Contracts:
- MultiPass independence: copying a MultiPassIterator yields an independent cursor.
  Advancing one copy must not affect the other.
- Bounds: if `i < 0` or `j < 0`, raise `std.err:IndexError`. Otherwise require
  `i < len` and `j < len`; out-of-range raises `IndexError`.
- Invalidation: any method (`len`, `compare_at`, `swap`) on an invalidated
  iterator/range raises `std.err:IteratorInvalidated(container_id, op_id)`.
- Stability: `len()` is stable for the duration of any `std.algo` operation on `&Self` / `&mut Self`.

### `for` lowering
Pinned lowering shape (fully-qualified, no shadowing):

```drift
val __src = expr
var __it = std.iter.Iterable.iter(__src)

loop {
	val __opt = std.iter.SinglePassIterator.next(&mut __it)
	match __opt {
		None => break
		Some(x) => { body }
	}
}
```

Rules:
- `expr` is evaluated exactly once.
- `Iterable.iter` is called exactly once.
- `SinglePassIterator.next` drives the loop.
- If `Iterable.iter` is missing: error category "type is not iterable".
- If `iter()` returns a type not implementing `SinglePassIterator`: error
  category "iter() result is not an iterator".

## std.containers

Pinned MVP container set:
- `Array<T>`
- `HashMap<K, V>`
- `TreeMap<K, V>`
- `HashSet<T>`
- `TreeSet<T>`
- `List<T>`
- `Queue<T>`
- `Deque<T>`

TreeMap Entry API (MVP):
- `entry_mut(&K) -> TreeMapEntryMut<K, V>` provides a single-lookup mutation handle.
- Entry methods do not return references; mutation is performed inside the call.
- To avoid moving from a projected field under MVP rules, `insert`/`or_insert` take the key again:
  - `insert(key: K, value: V) -> Optional<V>`
  - `or_insert(key: K, value: V) -> Bool`
  - `remove() -> Optional<V>`

TreeSet Entry API (MVP):
- No Entry API in MVP; use `insert`, `remove`, and `contains`.

Array API (MVP):
- Indexing:
  - `arr[i]` is a place expression. In value context it yields a copy **only if `T is Copy`** (throws `IndexError` on OOB).
  - `&arr[i]` yields `&T` (throws `IndexError` on OOB).
  - `&mut arr[i]` yields `&mut T` (throws `IndexError` on OOB; subject to borrow rules).
  - `arr.get(i) -> Optional<&T>` returns `None` on OOB.
- Mutation:
  - `push(value: T) -> Void` appends to the tail.
  - `pop() -> Optional<T>` removes and returns the tail element.
  - `insert(index: Int, value: T) -> Void` inserts at `index` (shifts right).
  - `remove(index: Int) -> T` removes and returns element at `index` (shifts left).
  - `swap_remove(index: Int) -> T` removes element at `index` by swapping with tail (order not preserved).
  - `set(index: Int, value: T) -> Void` overwrites element at `index`.
  - `clear() -> Void` drops all elements (capacity unchanged).
  - `reserve(additional: Int) -> Void` ensures capacity for `len + additional`.
  - `shrink_to_fit() -> Void` reduces capacity to `len`.
- Length/capacity:
  - `len` and `cap` return `Int` and are never negative.

Pinned iterator capability matrix (per iter form):

- Array:
  - `iter(self: T)`: SinglePass
  - `iter(self: &T)`: SinglePass, MultiPass, Bidirectional
  - `iter(self: &mut T)`: deferred (not implemented; borrow-safety enforcement pending)
- List:
  - `iter(self: T)`: SinglePass, MultiPass, Bidirectional
  - `iter(self: &T)`: SinglePass, MultiPass, Bidirectional
  - `iter(self: &mut T)`: SinglePass
- Deque (RandomAccessReadable/Permutable available via DequeRange/DequeRangeMut in MVP):
  - `iter(self: T)`: SinglePass, MultiPass, Bidirectional
  - `iter(self: &T)`: SinglePass, MultiPass, Bidirectional
  - `iter(self: &mut T)`: SinglePass
- Queue:
  - `iter(self: T)`: SinglePass
  - `iter(self: &T)`: SinglePass
  - `iter(self: &mut T)`: SinglePass
- TreeMap/TreeSet (in-order):
  - `iter(self: T)`: SinglePass, MultiPass, Bidirectional
  - `iter(self: &T)`: SinglePass, MultiPass, Bidirectional
  - `iter(self: &mut T)`: SinglePass
- HashMap/HashSet:
  - `iter(self: T)`: SinglePass
  - `iter(self: &T)`: SinglePass
  - `iter(self: &mut T)`: SinglePass

Structural mutation (MVP):
- Defined as any operation that can change length, capacity, or layout.

Container invalidation contract (MVP):
- Ranges/iterators must define and enforce a validity rule (e.g., a `gen` snapshot) and throw `IteratorInvalidated` when violated.
- The compiler does not update container state for user-defined containers; container implementers are responsible for maintaining their own invalidation mechanism.

Deque implementation note (MVP):
- Deque uses a ring buffer internally (O(1) front/back ops) while keeping the public API stable.
- Element writes (including swap/set) do not invalidate iterators/ranges.
- `gen` increments only when an operation actually changes array structure (length or capacity changes, or backing storage is reallocated/moved). No-op reserves/shrinks do not increment `gen`; element-only updates (`set`, `swap`) do not invalidate.

## std.core.hash

Hash mixing uses explicit wrapping `Uint64` math (via wrapping add/mul intrinsics).
Overflow must never trap, even in debug builds, so hash behavior stays stable.

`String` is Copy by cheap handle/retain in MVP (O(1)). APIs like `String.bytes()` rely on this.

## std.core.cmp

### Comparison traits
```drift
module std.core.cmp;

trait Equatable {
	fn eq(self: &Self, other: &Self) returns Bool
}

trait Comparable require Self is Equatable {
	fn cmp(self: &Self, other: &Self) returns Int
}
```

Rules:
- `Comparable` implies `Equatable` for the same type.
- `compare_at` must reflect `T is Comparable` ordering.
- Ordering law: `compare_at` must satisfy sign symmetry + transitivity + totality:
  - `cmp(i,j) == 0` iff `cmp(j,i) == 0`
  - `cmp(i,j) < 0` iff `cmp(j,i) > 0`
  - transitive and total (no unordered cases in MVP)

## std.algo

### Algorithms (signatures deferred)
Algorithm signatures are defined when implemented. Capability requirements are
tracked below for MVP:

| Algorithm | Capability requirement |
| --- | --- |
| `for_each` | `SinglePassIterator<T>` |
| `find` | `SinglePassIterator<T>` |
| `any` | `SinglePassIterator<T>` |
| `all` | `SinglePassIterator<T>` |
| `count` | `SinglePassIterator<T>` |
| `fold` | `SinglePassIterator<T>` |
| `min` / `max` | `SinglePassIterator<T>` + `T is Comparable` |
| `equal` | `SinglePassIterator<T>` + `T is Equatable` (consumes both) |
| `sort_in_place` | `RandomAccessPermutable<T>` |
| `binary_search` | `BinarySearchable<T>` + `T is Comparable` (key passed by `&T`) |

### Algorithm-specific capability traits
```drift
module std.algo;

trait BinarySearchable<T> require Self is std.iter.RandomAccessReadable<T> {
	fn compare_key(self: &Self, i: Int, key: &T) returns Int
}
```

Contracts:
- Bounds/invalidation rules:
- if `i < 0` -> `std.err:IndexError(container_id, i)`
  - else require `i < len()` -> otherwise `IndexError`
- invalidated -> `std.err:IteratorInvalidated(container_id, IteratorOpId::CompareKey)`
- `compare_key` must be coherent with `compare_at`/`Comparable` ordering.

## std.runtime

`std.runtime` provides registry-backed, long-lived shared state without language
globals. It defines a process-wide global registry and a per-thread registry.

Core API sketch:

```drift
module std.runtime;

struct GlobalRegistry
struct ThreadLocalRegistry

fn global_registry() -> &GlobalRegistry
fn thread_local() -> &ThreadLocalRegistry

fn get<T: Unborrowed + Send + Sync>() -> Optional<&T>
fn set<T: Unborrowed + Send + Sync>(value: T) -> Optional<&T>
fn contains<T: Unborrowed + Send + Sync>() -> Bool
fn expect<T: Unborrowed + Send + Sync>(msg: String) -> &T

implement GlobalRegistry {
	fn set<T: Unborrowed + Send + Sync>(value: T) -> Optional<&T>
	fn get<T: Unborrowed + Send + Sync>() -> Optional<&T>
	fn contains<T: Unborrowed + Send + Sync>() -> Bool
	fn expect<T: Unborrowed + Send + Sync>(msg: String) -> &T
}

implement ThreadLocalRegistry {
	fn set<T: Unborrowed>(value: T) -> Optional<&T>
	fn get<T: Unborrowed>() -> Optional<&T>
	fn contains<T: Unborrowed>() -> Bool
	fn expect<T: Unborrowed>(msg: String) -> &T
}
```

Semantics and invariants:
- Type-keyed by canonical type identity (package + module + name + args).
- Set-once / store-forever: values are inserted at most once and never removed.
- `GlobalRegistry` lookups are thread-safe; values require `Send + Sync`.
- `ThreadLocalRegistry` is per-thread; no `Send`/`Sync` required.
- Registries return shared references only; mutable state must be explicit in `T`.

## std.sync

`std.sync` is the stable stdlib surface for atomics and memory ordering.

### MemoryOrder

```drift
module std.sync;

enum MemoryOrder {
    Relaxed,
    Acquire,
    Release,
    AcqRel,
    SeqCst,
}
```

Semantics:
- `Relaxed`: atomicity only, no synchronization edges.
- `Acquire`: prevents later memory operations from moving before the atomic read.
- `Release`: prevents earlier memory operations from moving after the atomic write.
- `AcqRel`: acquire + release for read-modify-write operations.
- `SeqCst`: single total order across seq-cst operations.

### Atomic types

Pinned MVP types:
- `AtomicBool`
- `AtomicInt`
- `AtomicUint`
- `AtomicUint64` (ABI/runtime surface pinned; user-level source availability may be gated by language support)

### AtomicBool

```drift
struct AtomicBool

fn atomic_bool_new(value: Bool) -> AtomicBool

implement AtomicBool {
    fn load(self: &AtomicBool, order: MemoryOrder) -> Bool
    fn store(self: &AtomicBool, value: Bool, order: MemoryOrder) -> Void
    fn exchange(self: &AtomicBool, value: Bool, order: MemoryOrder) -> Bool
    fn compare_exchange(self: &AtomicBool, expected: Bool, desired: Bool, success: MemoryOrder, failure: MemoryOrder) -> Bool
    fn compare_exchange_observed(self: &AtomicBool, expected: Bool, desired: Bool, success: MemoryOrder, failure: MemoryOrder) -> Bool
}
```

### AtomicInt

```drift
struct AtomicInt

fn atomic_int_new(value: Int) -> AtomicInt

implement AtomicInt {
    fn load(self: &AtomicInt, order: MemoryOrder) -> Int
    fn store(self: &AtomicInt, value: Int, order: MemoryOrder) -> Void
    fn exchange(self: &AtomicInt, value: Int, order: MemoryOrder) -> Int
    fn compare_exchange(self: &AtomicInt, expected: Int, desired: Int, success: MemoryOrder, failure: MemoryOrder) -> Bool
    fn compare_exchange_observed(self: &AtomicInt, expected: Int, desired: Int, success: MemoryOrder, failure: MemoryOrder) -> Int
    fn fetch_add(self: &AtomicInt, value: Int, order: MemoryOrder) -> Int
    fn fetch_sub(self: &AtomicInt, value: Int, order: MemoryOrder) -> Int
}
```

### AtomicUint / AtomicUint64

```drift
struct AtomicUint
struct AtomicUint64

fn atomic_uint_new(value: Uint) -> AtomicUint
fn atomic_uint64_new(value: Uint64) -> AtomicUint64

implement AtomicUint {
    fn load(self: &AtomicUint, order: MemoryOrder) -> Uint
    fn store(self: &AtomicUint, value: Uint, order: MemoryOrder) -> Void
    fn exchange(self: &AtomicUint, value: Uint, order: MemoryOrder) -> Uint
    fn compare_exchange(self: &AtomicUint, expected: Uint, desired: Uint, success: MemoryOrder, failure: MemoryOrder) -> Bool
    fn compare_exchange_observed(self: &AtomicUint, expected: Uint, desired: Uint, success: MemoryOrder, failure: MemoryOrder) -> Uint
    fn fetch_add(self: &AtomicUint, value: Uint, order: MemoryOrder) -> Uint
    fn fetch_sub(self: &AtomicUint, value: Uint, order: MemoryOrder) -> Uint
}

implement AtomicUint64 {
    fn load(self: &AtomicUint64, order: MemoryOrder) -> Uint64
    fn store(self: &AtomicUint64, value: Uint64, order: MemoryOrder) -> Void
    fn exchange(self: &AtomicUint64, value: Uint64, order: MemoryOrder) -> Uint64
    fn compare_exchange(self: &AtomicUint64, expected: Uint64, desired: Uint64, success: MemoryOrder, failure: MemoryOrder) -> Bool
    fn compare_exchange_observed(self: &AtomicUint64, expected: Uint64, desired: Uint64, success: MemoryOrder, failure: MemoryOrder) -> Uint64
    fn fetch_add(self: &AtomicUint64, value: Uint64, order: MemoryOrder) -> Uint64
    fn fetch_sub(self: &AtomicUint64, value: Uint64, order: MemoryOrder) -> Uint64
}
```

### compare_exchange contracts

`compare_exchange` returns `true` on success and `false` on failure.
- Success: stores `desired`.
- Failure: does not store `desired`.

`compare_exchange_observed` returns the observed value at the target location.
- On success, returned value equals `expected`.
- On failure, returned value is the current value and is used by retry loops.

Failure-order validity rule:
- Invalid failure orders (`Release`, `AcqRel`) are rejected by the API guard.
- Current guard behavior is non-throwing and returns `false`.

### Fences

```drift
fn thread_fence(order: MemoryOrder) -> Void
fn signal_fence(order: MemoryOrder) -> Void
```

Semantics:
- `thread_fence`: inter-thread ordering fence.
- `signal_fence`: compiler reordering fence for signal-handler style boundaries.

### Handle-based atomics

Pinned lock-free carrier surface:

```drift
struct Handle<T>
struct AtomicHandle<T>

fn handle<T>(raw: Uint) -> Handle<T>
fn null_handle<T>() -> Handle<T>
fn atomic_handle<T>(v: Handle<T>) -> AtomicHandle<T>

implement<T> AtomicHandle<T> {
    fn load(self: &AtomicHandle<T>, order: MemoryOrder) -> Handle<T>
    fn store(self: &AtomicHandle<T>, value: Handle<T>, order: MemoryOrder) -> Void
    fn exchange(self: &AtomicHandle<T>, value: Handle<T>, order: MemoryOrder) -> Handle<T>
    fn compare_exchange(self: &AtomicHandle<T>, expected: Handle<T>, desired: Handle<T>, success: MemoryOrder, failure: MemoryOrder) -> Bool
    fn compare_exchange_observed(self: &AtomicHandle<T>, expected: Handle<T>, desired: Handle<T>, success: MemoryOrder, failure: MemoryOrder) -> Handle<T>
}
```

### Restricted atomic-reference tokens

Pinned non-reference model (not ordinary `&T`):

```drift
struct RefToken<T>
struct AtomicRef<T>

fn ref_token<T>(raw: Uint) -> RefToken<T>
fn null_ref_token<T>() -> RefToken<T>
fn atomic_ref<T>(v: RefToken<T>) -> AtomicRef<T>

implement<T> AtomicRef<T> {
    fn load(self: &AtomicRef<T>, order: MemoryOrder) -> RefToken<T>
    fn store(self: &AtomicRef<T>, value: RefToken<T>, order: MemoryOrder) -> Void
    fn exchange(self: &AtomicRef<T>, value: RefToken<T>, order: MemoryOrder) -> RefToken<T>
    fn compare_exchange(self: &AtomicRef<T>, expected: RefToken<T>, desired: RefToken<T>, success: MemoryOrder, failure: MemoryOrder) -> Bool
    fn compare_exchange_observed(self: &AtomicRef<T>, expected: RefToken<T>, desired: RefToken<T>, success: MemoryOrder, failure: MemoryOrder) -> RefToken<T>
}
```

Rules:
- No implicit coercions between `RefToken<T>` and references.
- No mutable-reference derivation from token loads.

### Lock-free queue and reclamation baseline

```drift
struct MpscQueue<T>
fn mpsc_queue<T>(capacity: Int) -> MpscQueue<T>

implement<T> MpscQueue<T> {
    fn capacity(self: &MpscQueue<T>) -> Int
    fn push(self: &MpscQueue<T>, value: Handle<T>) -> Bool
    fn pop(self: &MpscQueue<T>) -> Optional<Handle<T>>
}

struct EpochDomain
struct EpochParticipant
fn epoch_domain() -> EpochDomain
fn epoch_participant() -> EpochParticipant
fn epoch_enter(d: &EpochDomain, p: &mut EpochParticipant) -> Void
fn epoch_leave(d: &EpochDomain, p: &mut EpochParticipant) -> Void
fn epoch_retire(d: &EpochDomain, p: &EpochParticipant) -> Void
fn epoch_try_advance(d: &EpochDomain) -> Int
fn epoch_current(d: &EpochDomain) -> Uint
fn epoch_pending(d: &EpochDomain) -> Uint
fn epoch_reclaimed(d: &EpochDomain) -> Uint
```

### fetch_add / fetch_sub overflow semantics

`fetch_add` and `fetch_sub` use wrapping modular arithmetic and never trap.
- Unsigned atomics (`AtomicUint`, `AtomicUint64`): modulo `2^N`.
- Signed atomics (`AtomicInt`): two's-complement modular wrap.

### Behavior and guarantees

- All `std.sync` atomic methods are nothrow.
- Operations are lock-free when the target/runtime supports lock-free atomics for that width; otherwise runtime fallback may be used.
- For handoff patterns, correctness requires matching `Release` publisher and `Acquire` consumer on the same synchronization variable.
- Recommended defaults:
  - counters/telemetry: `Relaxed`
  - RMW state transitions: `AcqRel` (failure path `Acquire`)
  - handoff publish/consume: `Release` / `Acquire`

## std.err

Standard exception events used by stdlib:
- `IndexError(container_id: String, index: Int)`
- `IteratorInvalidated(container_id: String, op_id: IteratorOpId)`

Iterator op ids:
- `IteratorOpId::Next`
- `IteratorOpId::Prev`
- `IteratorOpId::Len`
- `IteratorOpId::CompareAt`
- `IteratorOpId::Swap`
- `IteratorOpId::CompareKey`

Notes:
- `container_id` is the base nominal key (package + module + name) with no type arguments.
- `IndexError` is raised per the RandomAccess bounds rule (negative -> error; otherwise `i < len`).
- IteratorOpId numeric tags are ABI-stable; values are append-only (no reordering).

## std.mem

`std.mem` provides unsafe pointer operations and trusted-only raw storage primitives.

### Unsafe pointer surface (user-unsafe)

```drift
module std.mem;

type Ptr<T>

fn ptr_from_ref<T>(r: &T) -> Ptr<T> unsafe
fn ptr_from_ref_mut<T>(r: &mut T) -> Ptr<T> unsafe
fn ptr_offset<T>(p: Ptr<T>, n: Int) -> Ptr<T> unsafe
fn ptr_read<T>(p: Ptr<T>) -> T unsafe
fn ptr_write<T>(p: Ptr<T>, v: T) -> Void unsafe
fn ptr_is_null<T>(p: Ptr<T>) -> Bool unsafe
```

Rules:
- `Ptr<T>` requires `T` to be sized.
- Pointer operations are allowed only in `unsafe` contexts and only when the compiler is invoked with `--allow-unsafe`.

### Trusted-only raw storage (stdlib internals)

Raw storage primitives are restricted to toolchain-trusted modules (`std.*`, `lang.*`, `drift.*`).

```drift
pub struct RawBuffer<T> { /* opaque */ }

@intrinsic fn alloc_uninit<T>(cap: Int) -> RawBuffer<T>;
@intrinsic fn dealloc<T>(buf: RawBuffer<T>) -> Void;
@intrinsic fn ptr_at_ref<T>(buf: &RawBuffer<T>, i: Int) -> &T;
@intrinsic fn ptr_at_mut<T>(buf: &mut RawBuffer<T>, i: Int) -> &mut T;
@intrinsic fn read<T>(buf: &mut RawBuffer<T>, i: Int) -> T;
@intrinsic fn write<T>(buf: &mut RawBuffer<T>, i: Int, v: T) -> Void;
fn capacity<T>(buf: &RawBuffer<T>) -> Int;
```

Rules:
- `RawBuffer` operations are not available to user code; only trusted stdlib modules may call them.
- `capacity` is a normal stdlib function (not an intrinsic fast-path).

### `MaybeUninit<T>` (user-unsafe)

```drift
pub struct MaybeUninit<T> { /* phantom wrapper */ }

@intrinsic fn maybe_uninit<T>() -> MaybeUninit<T> unsafe;
@intrinsic fn maybe_write<T>(slot: &mut MaybeUninit<T>, v: T) -> &mut T unsafe;
@intrinsic fn maybe_assume_init_ref<T>(slot: &MaybeUninit<T>) -> &T unsafe;
@intrinsic fn maybe_assume_init_mut<T>(slot: &mut MaybeUninit<T>) -> &mut T unsafe;
@intrinsic fn maybe_assume_init_read<T>(slot: &mut MaybeUninit<T>) -> T unsafe;
```

`MaybeUninit<T>` is a phantom wrapper for a `T`-shaped slot of storage that may or may not hold a live `T`.  Available to user code under `unsafe` (`--allow-unsafe`).  Two usage shapes are supported:

- **Container-internal**: `RawBuffer<MaybeUninit<T>>` for per-slot initialization in trusted stdlib data structures (`std.containers.HashMapCore` is the reference user).
- **Standalone local**: `var slot = mem.maybe_uninit<type T>()` in user `unsafe` code that needs a `T`-shaped slot without an initial value.

Layout:
- `sizeof(MaybeUninit<T>) == sizeof(T)` and `alignof(MaybeUninit<T>) == alignof(T)`.  The wrapper has no runtime overhead.

Safe usage pattern (inside `unsafe`):

```drift
var slot = mem.maybe_uninit<type T>();
mem.maybe_write<type T>(&mut slot, move v);
val out = mem.maybe_assume_init_read<type T>(&mut slot);
```

See `examples/maybe_uninit_local.drift` for the full compileable example.

Rules:
- All five `mem.maybe_*` operations are `unsafe`; calls require a surrounding `unsafe { ... }` block and `--allow-unsafe`.
- `maybe_uninit` zero-initializes the slot's bytes as a tombstone; the slot is *semantically* uninitialized — reading via `maybe_assume_init_*` before a corresponding `maybe_write` is undefined behavior.
- `maybe_write` transfers ownership of `v` into the slot; for non-Copy `T`, `v` is moved-out at the call site.
- `maybe_assume_init_read` moves the value out and zeroes the slot's bytes; the slot is uninitialized again after the call.
- The compiler does not perform path-sensitive read-before-write detection or leak-on-drop hinting on `MaybeUninit<T>` locals.  `MaybeUninit<T>` itself has no destructor — abandoning a written-but-never-read slot leaks the inner value silently.  Tracking initialization state is the caller's contract under `unsafe`.
