import json,collections
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
D=json.load(open(S+'/diag2.json'))
seen=collections.Counter()
for p,l,c,h,m in D['ambiguous_sites']:
    k=(c,)
    if seen[c]<3:
        try: src=open('/home/sl/src/drift-lang/'+p,errors='replace').read().split('\n')[l-1].strip()
        except Exception: src='?'
        print('%-24s %-60s:%d hit=%d miss=%d | %s'%(c,p,l,h,m,src[:90]))
    seen[c]+=1
print(seen.most_common())
