# Borrow Checker Escape Context Model — Active TODO

Owner: Klaudia
Maintainer: core compiler team
Status: active-only view (completed phases removed)

## Current State

- A5 phases 0–5 are complete.
- Callable coercion phases (V1–V4.8) are complete.
- Void binding generic-instantiation defect is fixed.

## Active Items

No open execution items at this time.

## If New Work Opens

1. Add only new, actionable tasks here.
2. Keep completed execution detail in `work-progress.md` (do not re-copy into this file).
3. Use regression-first flow for any LANGUAGE_BUG:
   - add failing repro
   - confirm fail
   - fix root cause
   - confirm pass
4. Apply Boundary Contract Guardrails on boundary-shape changes:
   - positive regression
   - negative regression
   - stale test/docs/message alignment

## Handoff Rule

- This file is instruction-only for active work.
- Keep it short; remove done items promptly.
