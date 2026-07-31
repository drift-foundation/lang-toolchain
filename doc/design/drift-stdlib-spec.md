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
- `std.io`: files, streams, buffers (regular-file and socket I/O).
- `std.net`: TCP/UDP sockets and connect-by-hostname.
- `std.fs`: filesystem queries (`read_dir`).

## Stdlib IO contract (applies to `std.io`, `std.net`, `std.fs`)

All public IO in these modules MUST satisfy the following contract. It is the
source of truth for how blocking, deadlines, cancellation, saturation, and error
classification behave across the IO surface. The mechanisms that implement it are
specified in [drift-concurrency.md](drift-concurrency.md) ("Internal blocking
boundary": pollable-descriptor retry vs. bounded blocking-pool offload).

1. **No carrier blocking.** Public IO must **never block a virtual-thread carrier
   thread.** An operation that can wait either (a) does a nonblocking syscall and
   parks the VT on reactor readiness (pollable fds), or (b) is offloaded as one job
   to the bounded blocking-syscall pool while the VT parks (non-pollable syscalls
   such as filesystem operations and `getaddrinfo`).
2. **Explicit deadline.** Every potentially-waiting operation must support an
   explicit deadline (timeout). No unbounded implicit waits.
3. **Prompt resumption.** Deadline expiry or cancellation must resume the caller
   **promptly** — it must not wait for the underlying operation to finish.
4. **Best-effort physical cancellation.** Physically cancelling an in-flight kernel
   operation is best-effort and often impossible; **logical** cancellation may
   *abandon* the operation. The abandoned operation runs to completion in the
   background and its eventual result is discarded.
5. **Bounded admission + backpressure.** Work admission and queues must be bounded.
   Saturation must surface as an explicit **backpressure error** (e.g. `EAGAIN`),
   never an unbounded queue or unbounded thread growth.
6. **Independent job ownership.** A job must own its arguments and results
   **independently** of any caller-side value, so that an abandoned operation stays
   memory-safe and its eventual result is freed exactly once regardless of which
   side (caller or worker) finishes last.
7. **Distinguish the failure modes.** APIs must let callers distinguish
   **timeout**, **cancellation**, **saturation**, and **underlying IO errors**
   (errno). They are not collapsed into a single opaque failure.

**Conformance status.**
- `std.fs.read_dir(path, timeout)` (Slice 3) — **conforms**: offloaded to the
  bounded blocking pool; takes an explicit `timeout` (clause 2) and abandons
  promptly on expiry (clause 3); abandon-safe via a refcounted job that owns its
  path + snapshot independently (clauses 4, 6); saturation surfaces as a distinct
  `"saturated"` kind (clause 5); and the failure modes are distinct `IoError.kind`s
  — `"timeout"`, `"cancelled"`, `"saturated"`, `"errno"`, `"invalid-utf8"` (clause 7).
- `std.net` (TCP/UDP, connect-by-hostname) — **conforms** for the socket retry
  path and the DNS offload path.
- `std.io` **regular-file** APIs (`open`/`read`/`write`) — **non-conforming today**
  (a pre-existing condition): they run the syscall inline on the carrier. A regular
  file fd is not made nonblocking by epoll, so a stalling filesystem can block the
  carrier. **Follow-up audit (tracked):** route blocking regular-file syscalls
  through the same bounded blocking pool, or document an explicit, justified
  exception. Until then these APIs do not meet clause 1 for slow/networked
  filesystems.

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

## std.source

Source-location primitives and a UTF-8 scalar cursor for hand-written
frontends (lexers, recursive-descent parsers).  Byte offsets are
authoritative for slicing and hashing; line/column are diagnostic
coordinates only.  `source_id` is logical/configured (never an absolute
build path) and may be persisted into IR.  Same bytes + same `source_id`
produce byte-identical spans, slices, and diagnostics across machines and
reloads.

Types:
- `SourcePos { byte_offset: Int, line: Int, column: Int }` — `Copy`.
  `byte_offset` is 0-based and authoritative; `line`/`column` are 1-based,
  and `column` counts **Unicode scalar values**, not bytes.
- `SourceSpan { source_id: String, start: SourcePos, end: SourcePos }` —
  `Copy`; a half-open byte range `[start, end)`.
- `SourceError { code: String, span: SourcePos-bearing SourceSpan }` —
  construction/slicing failure.  `code` is one of `"invalid-utf8"`,
  `"invalid-slice-range"`, `"slice-not-char-boundary"`,
  `"span-source-mismatch"` (stable, kebab-case).
- `SourceCursor` — a forward, eagerly-validated scalar cursor (owns the
  source as an Arc-backed `String`).

Free functions:
- `pos_zero() -> SourcePos` — the canonical start `{0, 1, 1}` (no
  `source_id`; `SourcePos` carries no source identity).
- `span_byte_len(&SourceSpan) -> Int`, `span_is_empty(&SourceSpan) -> Bool`.
- `source_cursor(&Array<Byte>, source_id) -> Result<SourceCursor, SourceError>`
  — validates UTF-8 in one O(n) pass; on the first invalid byte returns
  `Err` with a zero-width span at that byte.  After success every offset
  the cursor yields sits on a scalar boundary, so decode is infallible.
- `source_cursor_from_string(String, source_id) -> SourceCursor` — no
  re-validation (a Drift `String` is already valid UTF-8).

Cursor methods (all `nothrow`): `position()`, `at_end()`, `peek() -> Int`
(scalar value, `-1` at EOF), `advance() -> Int` (decode+consume, `-1` at
EOF), `mark()`, `span_from(start)`, `span_here()`, `source_id()`,
`byte_length()`, `slice(start, end) -> Result<String, SourceError>`,
`slice_span(&SourceSpan) -> Result<String, SourceError>`.

LF / CRLF and column semantics (frozen):
- Only `LF` (`0x0A`) advances the line (`line += 1`, `column = 1`).
- `CR` (`0x0D`) is an ordinary scalar (`column += 1`, line unchanged).
- `CRLF` is therefore **two** `advance` calls — the `\r` and `\n` are each
  individually observable, and the line advances exactly once, on the
  `\n`.  The cursor never hides a byte; a lexer that wants `\r\n` as one
  logical newline consumes the `\r` then the `\n` explicitly.
- A 4-byte scalar advances `column` by 1 and `byte_offset` by 4.

Slicing rejects (with `SourceError`): out-of-range / inverted ranges
(`invalid-slice-range`), offsets that split a multibyte scalar — a
continuation byte `0x80–0xBF` at `start` or `end` (`slice-not-char-boundary`),
and `slice_span` of a span whose `source_id` differs from the cursor's
(`span-source-mismatch`).

## std.parse (frontend toolkit)

Builds a pull-style token stream and a structured diagnostic on top of
`std.source`.  The existing scalar parsers (`parse_int`, `parse_uint`,
`parse_float`, `parse_bool`, `*_bytes`, and `ParseError`) are unchanged and
re-exported as before; the frontend surface below is purely additive.  No
parser combinators or generator — a foundation for hand-written
recursive-descent parsing only.

- `trait TokenKind require Self is cmp.Equatable { fn describe(&Self) -> String }`
  — a user token-kind type.  `Equatable` lets `expect` compare kinds;
  `describe` supplies the human-facing descriptor used in diagnostics.
- `Token<K> { kind: K, span: source.SourceSpan }`.
- `ParseDiagnostic { code: String, span: source.SourceSpan,
  expected: Array<String>, found: Optional<String> }` — `code` is stable,
  kebab-case (toolkit base set: `"unexpected-token"`, `"unexpected-eof"`);
  `found = None` is the structured end-of-input encoding (never a magic
  prose value).  Descriptor prose inside `expected`/`found` is human-facing
  and **not** part of the stable contract — pin `code` + span +
  `found.is_none()`, not prose.
- `TokenStream<K> require K is TokenKind` — owns its tokens
  (`containers.Deque<Token<K>>`); `Destructible` (dropping it drops every
  unconsumed token).

Free functions:
- `parse_diagnostic(code, span, expected, found) -> ParseDiagnostic`.
- `token_stream<K>(var tokens: Array<Token<K>>, eof_span: SourceSpan) -> TokenStream<K>`
  — moves `tokens` into the lookahead buffer in order; `eof_span` is the
  zero-width span reported for `unexpected-eof`.

`TokenStream<K>` methods (all `nothrow`):
- `peek(n) -> Optional<&Token<K>>` — borrow the n-th lookahead token
  (0 = next to consume); `None` at/after end of input.
- `current() -> Optional<&Token<K>>` — `peek(0)`.
- `advance() -> Optional<Token<K>>` — consume the front by move.
- `at_end() -> Bool`.
- `expect(&K, expected_name: String) -> Result<Token<K>, ParseDiagnostic>`
  — consume the next token if its kind equals the expected kind; otherwise
  return a diagnostic **without consuming** (`unexpected-eof` at end of
  input, else `unexpected-token` with the offending token's span and its
  `describe()` as `found`).

## std.json (parser policy + located decoder + canonical encoding)

Slice 2 makes "strict JSON" an orthogonal parser-policy surface and adds a
source-location-preserving decoder and a canonical encoder. As of 0.33.93
(clean break) `parse()` IS the strict entry point — the legacy/permissive
default and `parse_strict` are gone: `parse()` rejects duplicate keys,
rejects leading zeros, decodes `\uXXXX` escapes, and rejects unescaped
control bytes. Policies select **standard JSON or a stricter subset** —
never a superset, never a value reinterpretation (sole exception:
duplicate-key resolution, where explicit `permissive()` restores
keep-last).

### Parser policy

- `JsonParseConfig { duplicate_keys, top_level, numbers, limits }` (all `Copy`):
  - `DuplicateKeyPolicy` = `Reject | KeepFirst | KeepLast`.
  - `TopLevelPolicy` = `AnyValue | ObjectOrArray | ObjectOnly`.
  - `JsonNumberPolicy { allow_fractions, allow_exponents, allow_negative_zero }`
    — independent toggles; each disables a *valid-JSON* number shape. There is
    **no** leading-zeros toggle (leading zeros are invalid JSON, rejected by
    every policy — including `parse()` itself since the 0.33.93 clean break).
  - `JsonLimits { max_document_bytes, max_depth, max_string_bytes,
    max_number_bytes, max_array_items, max_object_fields }` — each
    `Optional<Int>`; `None` = unlimited; a negative `Some(n)` ⇒ `"invalid-config"`.
- Profiles: `permissive()` (most lenient standard JSON, keep-last), `strict()`
  (reject dups; `-0` allowed), `signed_ir()` (integer-only `0 | -?[1-9][0-9]*`,
  reject dups; no bundled limits/top-level). `JsonParseConfigBuilder` with frozen
  `build() -> Result<JsonParseConfig, JsonErrorData>` (validates limits).
- Entry points: `parse(&String)` — THE strict entry point (== `strict()`;
  0.33.93 clean break: the legacy/permissive default and the
  `parse_strict` alias are gone) — and
  `parse_with_config(&String, &JsonParseConfig)` for explicit policy
  (`permissive()`, `signed_ir()`, or a builder config).
- Numeric policy inspects the verbatim `JsonNode::Number(raw)` lexeme before any
  conversion; per-number precedence: leading-zero → negative-zero → fraction →
  exponent. Duplicate-key `Reject` reports the **second** key's opening-quote
  offset.
- **String rules**: every policy follows RFC 8259 — `\uXXXX` (incl. surrogate
  pairs) decodes to UTF-8, and unescaped control bytes `U+0000–U+001F` are
  rejected (`unescaped-control`).
- **Limits are pre-consumption**: array-item / object-field limits reject the
  offending element/key before parsing its value; `max_string_bytes` /
  `max_number_bytes` are enforced incrementally; `max_object_fields` counts member
  occurrences (duplicates included), not unique keys.

### Located decoder surface

- `parse_located(&String, &JsonParseConfig) -> Result<JsonDoc, JsonErrorData>` —
  parses under the policy AND retains a source-span sidecar. Same node as
  `parse_with_config` (sidecar is value-neutral).
- `JsonDoc.cursor() -> LocatedCursor`; `JsonDoc.at_pointer(&String)` resolves an
  **absolute** RFC-6901 JSON Pointer (`~0`/`~1` escaping; array-index grammar
  `0 | [1-9][0-9]*`).
- `LocatedCursor` (all `nothrow`, `Result`-returning): `child` / `index` /
  `require_field` (the contract `require`; `require` is a reserved keyword) /
  `optional` (`Result<Optional<LocatedCursor>, …>` — `Ok(None)` only for an absent
  key, `Err(type-mismatch-object)` on a non-object) / `forbid_unknown` (reports
  the earliest unknown key in source order) / `discriminant` / `as_string` /
  `as_int` / `as_uint` / `as_float` / `as_bool` / `as_object` / `as_array_len` /
  `span() -> JsonByteSpan` / `pointer() -> String`.

### Object entry enumeration

`JsonNode.entries(&self) nothrow -> JsonEntriesIter` enumerates an object's
`(key, value)` pairs **by borrow** (no key/value cloning). `JsonEntriesIter` is
both `Iterable` and a `SinglePassIterator` over
`containers.HashMapItemRef<String, JsonNode>`; each `item` exposes
`item.key: &String` and `item.value: &JsonNode`.

```drift
for entry in node.entries() {          // for-in works directly (no extra imports)
    // entry.key: &String, entry.value: &JsonNode
}
```
To drive it manually with `.next()`, bring the trait into scope:
`import std.iter as iter; use trait iter.SinglePassIterator;`.

- A **non-object** node (and an empty object) yields **no** entries — there is no
  separate "not an object" signal (the two are indistinguishable through this
  API).
- **Iteration order is unspecified** (backing-`HashMap` order) and must **not** be
  relied upon — it is not canonical; use `encode_canonical` for byte-stable
  output.
- Standard `HashMap` borrowing / iterator-invalidation rules apply: the source
  cannot be mutated while an iterator is live.

### Canonical encoding

- `encode_canonical(&JsonNode) -> Result<String, JsonErrorData>` — a separate
  result-propagating encoder (does **not** reuse the failure-swallowing
  `_encode_node` object path). Fixed form: UTF-8, no insignificant whitespace,
  object keys recursively sorted by UTF-8 bytes, the frozen escape table for keys
  and values (short forms `\b\f\n\r\t`; other controls lowercase `\u00xx`; `/`
  and non-ASCII verbatim). Signed-IR integer grammar on emit — rejects `-0`,
  leading zeros, fractions, exponents. Deterministic first-error in canonical
  emit order. Hashing is caller-side over the emitted bytes.

### Stable error codes (`JsonErrorData.tag`)

Parse policy: `duplicate-key`, `top-level-not-object`,
`top-level-not-object-or-array`, `number-leading-zero`, `number-negative-zero`,
`number-fraction`, `number-exponent`, `limit-document-bytes`, `limit-depth`,
`limit-string-bytes`, `limit-number-bytes`, `limit-array-items`,
`limit-object-fields`, `invalid-config`, `unescaped-control` (+ `invalid-escape`
for malformed string escapes). Located decoder: `missing-field`,
`unknown-field`, `type-mismatch-{string,int,uint,float,bool,object,array}`,
`invalid-pointer`, `invalid-array-index`, `index-out-of-range`. Canonical encode:
`canonical-number-{leading-zero,negative-zero,fraction,exponent,invalid}`,
`canonical-invalid-node`. (Existing `invalid-syntax` / `internal-error`
unchanged.)

Value-semantic decoders users layer on top (ISO dates, flexible booleans, numeric
ranges, domain rules) are out of scope — built with the located surface +
`std.parse` scalar parsers.
