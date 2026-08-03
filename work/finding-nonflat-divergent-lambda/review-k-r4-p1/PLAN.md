# Plan: static review of K's R4 / P1 patch

Date: 2026-08-03

1. Identify the exact uncommitted source and test changes.
2. Trace `_type_return_value` through named returns, lambda tails, CallInfo,
   hidden-function reconstruction, MIR, and SSA.
3. Review the stored-capturing-lambda diagnostic changes, including location
   preservation and Unknown-cascade suppression.
4. Review statement-position IIFE routing for Void, owned, and throwing results.
5. Check new tests against the actual HIR producers and changed branches.
6. Record only actionable findings, with current line references and required
   closure criteria.

This is a static review.  Do not edit compiler/runtime/stdlib/in-tree tests and
do not run K's gates.  The version bump is explicitly out of scope per the user.
