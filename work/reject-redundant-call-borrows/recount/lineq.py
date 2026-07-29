import json,collections
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
R=[r for r in json.load(open(S+'/results3.json')) if r['fam']!='builtin']
cache={}
bad=collections.Counter(); tot=collections.Counter()
for r in R:
    p='/home/sl/src/drift-lang/'+r['path']
    if p not in cache:
        try: cache[p]=open(p,errors='replace').read().split('\n')
        except Exception: cache[p]=[]
    L=cache[p]; ln=r['line']+r['off']
    tot[r['area']]+=1
    ok = 0<ln<=len(L) and '&' in L[ln-1]
    if not ok: bad[r['area']]+=1
for a in sorted(tot): print('%-20s total=%5d line-attribution-miss=%4d (%.1f%%)'%(a,tot[a],bad[a],100*bad[a]/tot[a]))
print('OVERALL miss %d / %d = %.2f%%'%(sum(bad.values()),sum(tot.values()),100*sum(bad.values())/sum(tot.values())))
