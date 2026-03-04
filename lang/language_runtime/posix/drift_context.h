#ifndef DRIFT_CONTEXT_H
#define DRIFT_CONTEXT_H
#include <stddef.h>
#include <stdint.h>

typedef struct DriftContext {
	void *rsp;   /* only field the asm touches */
} DriftContext;

/* Prepare ctx so that the first drift_swapcontext(..., ctx) starts
   entry(arg) on the given stack.  stack_top = (char *)base + size.
   entry() must not return — it must drift_swapcontext back to the
   scheduler before exiting.  If it does return, the trampoline aborts. */
void drift_makecontext(DriftContext *ctx, void *stack_top,
                       void (*entry)(uintptr_t), uintptr_t arg);

/* Save caller's callee-saved regs into *from, restore *to, jump. */
void drift_swapcontext(DriftContext *from, DriftContext *to);

#endif
