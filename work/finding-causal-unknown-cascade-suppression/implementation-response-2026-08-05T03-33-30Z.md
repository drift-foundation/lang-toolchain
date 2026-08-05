# Baton message

Timestamp: 2026-08-05T03-33-30Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: b49addf2e8d5
# Interim implementation handoff: children B+C and parent A cores landed (K)

Author: K (implementer), actor `k`, seed `46dae92ab18cc9f2a91412ac92fee356`

Early handoff per the parallel-review workflow while the family battery
+ 5-suite smoke rerun runs; full detail in the parent PROGRESS.md
(sections "Child C ... CORE LANDED", "Child B ... CORE LANDED",
"Parent A ... CORE LANDED").  Highlights:

- Child C: typed-let/return Callback wrap restoration via slot-site
  PRE-WRAP (root cause: allow_capture_invoke stamp leak across re-check
  passes made the Site-5 authority unreachable); typed-let, return, AND
  the argument-slot shapes now compile and RUN.
- Child B: single pending finalizer (`_classify_and_type_pending` +
  `_finalize_pending_value_use`) through the owner barrier; captureless
  aliases/order-independence RUN; capturing/unconstrained single clean
  primaries; static-callback splice centralized in the sole wrap
  constructor (fn-typed HVar wraps its known HFnPtrConst).
- Parent A: transaction-owned binding+node cause state; four proven
  propagation shapes; three consumers swapped to exact causal
  predicates (incl. HInvoke parity); both former red tripwire probes
  now GREEN with all caused chains single-primary (11-probe evidence).

NOT yet in this diff: child D (named-fn borrow structural red + fix),
the four in-tree regression files, the two approved comment edits, the
history fold.  Gates: interim battery results will be appended to
PROGRESS; if review finds issues first the battery is stopped and
reworked.
