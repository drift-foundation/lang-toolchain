# Finding: clean durable references after removing the bare-temp field-projection work folder

Date: 2026-08-03

Classification: documentation/housekeeping cleanup.  Not a new `LANGUAGE_BUG`
and not a source-semantics change.

## Situation

The completed `work/bare-temp-field-projection-uaf/` research folder was
removed.  The fix itself is already preserved in repository history and the
durable release narrative, but five source/test comments still name the removed
work path.

Those references are now dangling and also defeat the earlier durable-tree
cleanup policy that `work/` is temporary coordination state, not permanent
documentation.

## Exact inventory

Three compiler comments:

1. `lang/driftc/stage2/hir_to_mir.py:3371`
2. `lang/driftc/stage2/hir_to_mir.py:3522`
3. `lang/driftc/stage2/hir_to_mir.py:3677`

One driver-test module docstring:

4. `lang/tests/driver/test_rvalue_arg_temp_drop_ab.py:4`

One e2e fixture comment:

5. `lang/tests/codegen/e2e/autoborrow_owned_rvalue_field_method_drops_once/main.drift:6`

No other durable-tree references to the removed folder were found at the time
this finding was created.

## Durable replacement

Use the existing history entry instead of another `work/` path:

`doc/history.md`, section dated **2026-07-31**, beginning near line 121:

> 0.34.1: LANGUAGE_BUG — field projection of an rvalue temp at a declared-&
> formal double-freed

That entry records:

- the original double-free/UAF shape;
- the single-owner/address-projection repair;
- the 0.33.91–0.33.93 unsafe window;
- the 0.33.94 development and withdrawn 0.34.0 interim behavior; and
- the final 0.34.1 generalized control-flow-rvalue contract.

It is therefore the stable authority for historical references.  Do not create
a replacement long-form work folder or copy the deleted research back into the
tree.

## Version wording correction

The three MIR comments currently call this the “0.34.0” fix.  That candidate
was withdrawn; the durable accepted contract is 0.34.1.  When touching these
comments, describe it as the **0.34.1 field-projection UAF fix**, optionally
noting the withdrawn 0.34.0 candidate only where historical sequencing matters.

This is comment/documentation correction only.  It requires no compiler or ABI
version change.
