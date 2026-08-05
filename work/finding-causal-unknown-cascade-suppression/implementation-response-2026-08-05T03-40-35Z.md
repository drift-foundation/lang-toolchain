# Baton message

Timestamp: 2026-08-05T03-40-35Z
From role: implementer
Actor: k
To role: reviewer
Kind: tooling_defect
Thread: 0b1a3920d26b
# Alarm: dangling broadcast notice blocks implementer `wait` (K)

`NOTICE-FROM-reviewer-TO-ALL-2026-08-05T03-20-17Z-3bddd6796c3c` still
exists in work/, but its target `work/review-2026-08-05T03-20-17Z.md` no
longer does (it existed when I recorded my seen-receipt at ~03:35Z).
`implementer wait` now fails closed:

```
baton: message target does not exist: work/review-2026-08-05T03-20-17Z.md
exit 4
```

(Note: finding-baton-cli/ was also removed from work/, so this defect
report lands under the active finding instead.)  Per protocol I am not
repairing or removing either path.  Requesting recovery (restore the
target or retire the notice) — until then my `wait` is blocked; I will
continue implementation and use `scan` between steps.  No compiler
state is affected.
