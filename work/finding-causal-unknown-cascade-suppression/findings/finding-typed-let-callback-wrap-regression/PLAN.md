# Plan: restore lowering-visible Callback slot construction

1. Preserve K's localized raw-HLambda reproducer and record the exact current
   checker/MIR output.
2. Confirm current post-check HLet.value remains HLambda while its recorded
   type is Callback.
3. Add new red full compile/run tests and structural HIR pins for both direct
   typed HLet and pending-HVar argument forms.
4. Trace `_try_callback_wrap_for_iface_slot` at typed HLet and prove equality
   bypasses it after contextual HLambda typing.
5. Restore routing through the existing WRAPPED/REJECTED/SKIP authority before
   raw equality/interface handling can accept the initializer. Construct the
   wrapper before typing its inner lambda so capture-capable Callback context
   reaches the callback intrinsic authority.
6. Prove explicit wraps are not wrapped twice and borrowed captures still
   reject once with the established message.
7. Run the existing implicit-callback-wrap module unchanged plus the new full
   compile/run regression and relevant MIR boundary tests.
8. Coordinate with pending-lambda alias/fn-argument finalization so an HVar is
   stored as a thin fnptr and every Callback consumer inserts a real wrapper.
9. Audit free/static/method argument coercion for the same label-only bypass;
   close any route reached by the regression matrix through the shared
   authority rather than per-route TypeId stamping.
10. Make only the existing-test comment/docstring edits listed in the parent
    authorization ledger after Slawomir approves that exact list.
11. Fold the history note into pending 0.35.0; ABI remains 22.

Shared edits begin only after the active full suite is clean and the parent
plan receives terminal planning acceptance.
