/* drift_context.S — lightweight context switch for x86-64 SysV ABI.
 * No signal-mask manipulation (unlike glibc swapcontext). */

#ifdef __x86_64__

.text

/* void drift_swapcontext(DriftContext *from, DriftContext *to)
 *   from = %rdi, to = %rsi
 *
 * Save callee-saved registers onto the current stack, store %rsp into
 * from->rsp, load %rsp from to->rsp, restore callee-saved registers,
 * and ret (pops the saved return address). */
.globl drift_swapcontext
.type  drift_swapcontext, @function
.p2align 4
drift_swapcontext:
	pushq %rbx
	pushq %rbp
	pushq %r12
	pushq %r13
	pushq %r14
	pushq %r15
	movq  %rsp, (%rdi)    /* from->rsp = rsp */
	movq  (%rsi), %rsp    /* rsp = to->rsp   */
	popq  %r15
	popq  %r14
	popq  %r13
	popq  %r12
	popq  %rbp
	popq  %rbx
	ret
.size drift_swapcontext, .-drift_swapcontext

/* void drift_makecontext(DriftContext *ctx, void *stack_top,
 *                        void (*entry)(uintptr_t), uintptr_t arg)
 *   ctx       = %rdi
 *   stack_top = %rsi
 *   entry     = %rdx
 *   arg       = %rcx
 *
 * Build a synthetic frame on the new stack so that the first
 * drift_swapcontext(..., ctx) will "return" into _drift_trampoline,
 * which calls entry(arg).  entry() must not return — it must
 * drift_swapcontext back to the scheduler itself.  If entry does
 * return, the trampoline executes ud2. */
.globl drift_makecontext
.type  drift_makecontext, @function
.p2align 4
drift_makecontext:
	/* Align stack_top down to 16 bytes */
	andq  $-16, %rsi

	/* Build the frame that drift_swapcontext will pop.
	 * Layout (high→low):
	 *   [return address for ret = _drift_trampoline]
	 *   [rbx = entry]
	 *   [rbp = arg]
	 *   [r12 = 0]
	 *   [r13 = 0]
	 *   [r14 = 0]
	 *   [r15 = 0]
	 *
	 * After drift_swapcontext pops r15..rbx and does ret,
	 * we land in _drift_trampoline with rbx = entry, rbp = arg. */

	/* ret addr for the final 'ret' in drift_swapcontext */
	leaq  _drift_trampoline(%rip), %rax
	movq  %rax, -8(%rsi)

	/* callee-saved slots (popped in order: r15 r14 r13 r12 rbp rbx) */
	movq  %rdx, -16(%rsi)  /* rbx slot = entry */
	movq  %rcx, -24(%rsi)  /* rbp slot = arg   */
	movq  $0,   -32(%rsi)  /* r12 = 0 */
	movq  $0,   -40(%rsi)  /* r13 = 0 */
	movq  $0,   -48(%rsi)  /* r14 = 0 */
	movq  $0,   -56(%rsi)  /* r15 = 0 */

	/* ctx->rsp = stack_top - 56 (points at the r15 slot) */
	leaq  -56(%rsi), %rax
	movq  %rax, (%rdi)
	ret
.size drift_makecontext, .-drift_makecontext

/* Internal trampoline — entered via ret from drift_swapcontext.
 * At this point: rbx = entry, rbp = arg.
 * rsp = stack_top (16-byte aligned).  ABI requires rsp % 16 == 0
 * before a call instruction, which is already satisfied.
 *
 * entry() must drift_swapcontext back to the scheduler before
 * returning.  If it does return, we abort. */
.p2align 4
_drift_trampoline:
	movq  %rbp, %rdi       /* arg → first parameter */
	callq *%rbx             /* entry(arg) — must not return */
	ud2
.size _drift_trampoline, .-_drift_trampoline

#else
#error "drift_context.S: unsupported architecture (only x86-64 implemented)"
#endif
