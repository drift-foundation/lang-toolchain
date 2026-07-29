import json,collections
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
D=json.load(open(S+'/diag.json'))
for p,l,w in D['unresolved_sites'][:70]:
    print('%-70s %5d %s'%(p,l,w))
