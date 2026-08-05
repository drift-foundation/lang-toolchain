# Ownership corpus drift on the pending 0.35.0 train

Status: open; promotion is blocked pending independent reviewer and implementer agreement.

## Trigger

After a clean `run-all-tests.sh` (`perf-protocols`, full memcheck, and full ASAN all passed), Slawomir ran:

```text
just ownership-corpus-verify
```

The read-only verification retained `build/tmp/ownership-corpus-actual`, which means the fresh full compile did not exactly match the committed reviewed baseline.

## Working question

Determine whether every difference is an expected consequence of the already-reviewed pending 0.35.0 compiler and fixture changes, with no unintended ownership-semantic regression. Do not promote merely because the differences look small. Reviewer and implementer should inspect independently, record uncertainty and competing explanations, and agree before asking Slawomir to run promotion.

## Constraints

- Do not mutate `lang/tests/ownership_corpus/reviewed-baseline` during analysis.
- Do not run `ownership-corpus-promote` until Slawomir approves after both reviews agree.
- Treat the retained actual as evidence, not authority.
- If the one-function projection reduction is not fully explained, investigate the compiler producer/lowering change rather than normalizing it away.
- `PROGRESS.md`, if created, is implementer-owned. Reviewer input belongs in immutable `review-*.md` files.

## Initial evidence

See `review-2026-08-05T14-52-14Z.md` for the reviewer's first-pass comparison. The central semantic delta currently appears to be exactly one fewer audited function in `closures_share_capture_eval_order`; that attribution is provisional until independently explained.
