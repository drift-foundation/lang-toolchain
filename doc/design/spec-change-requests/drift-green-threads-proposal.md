Motivation

Drift targets systems programming with scalable concurrency. Blocking OS threads for IO, process management, or networking would undermine this goal. Introducing green threads and a reactor early allows realistic testing of concurrency semantics, IO, and process control while keeping the programming model synchronous and readable.

Scope

This proposal covers:

* Green threads (cooperative, user-space scheduled)
* A single-threaded reactor based on epoll
* Nonblocking file descriptor IO
* Integration with pipes, sockets, and process management

Out of scope for MVP:

* Preemptive scheduling
* Multiple schedulers per process
* Windows support

Core runtime model

All blocking operations are expressed as parking the current green thread until an external condition is met. The runtime provides three primitives:

* park_read(fd)
* park_write(fd)
* park_timer(deadline)

Calling these yields the current green thread and resumes it only when the condition becomes ready.

Reactor backend

The reactor is built on Linux primitives:

* epoll_create1
* epoll_wait
* timerfd (for sleep and timeouts)
* pidfd (for process lifecycle notifications)

The scheduler and reactor run on the same OS thread in MVP, avoiding locking complexity.

Scheduler structure

The scheduler maintains:

* a queue of runnable green threads
* maps of file descriptors to parked threads (read/write)
* a timer structure for sleeping threads

Execution proceeds by running runnable threads until none remain, then blocking in epoll_wait until an event occurs, at which point parked threads are resumed.

Nonblocking IO discipline

All file descriptors used with green threads must be O_NONBLOCK. Stdlib IO wrappers translate EAGAIN into park_read/park_write calls. Blocking syscalls in user code are forbidden.

Process integration

Process spawning uses fork/exec in a minimal runtime layer. Parent-side pipes and pidfds are nonblocking and registered with the reactor.

Waiting for process exit is implemented by parking on pidfd readiness and harvesting status via waitid(WNOHANG).

Stdout/stderr capture uses separate green threads draining pipes concurrently, preventing deadlocks.

Illustrative pseudo-code

spawn_process():
create pipes (O_NONBLOCK | O_CLOEXEC)
fork
child: dup2, close_range, execveat, _exit
parent: pidfd_open

wait_process(proc):
while not exited:
park_read(proc.pidfd)
check waitid

Benefits

* Unified mechanism for files, sockets, timers, and processes
* Early validation of concurrency and IO design
* Scales naturally to HTTP and network services

Risks and mitigations

* Increased runtime complexity: mitigated by strict MVP limits (single scheduler, one waiter per fd)
* Debuggability: mitigated by deterministic scheduling and explicit parking points
