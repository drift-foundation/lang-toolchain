import json,collections
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
ALL=json.load(open(S+'/results3.json'))
R=[r for r in ALL if r['fam']!='builtin']
def sub(r):
    p=r['path']
    if p.startswith('lang/tests/codegen/e2e/'): return 'lang-tests: codegen/e2e'
    if p.startswith('lang/tests/'): return 'lang-tests: other .drift' if r['area']=='lang-tests-drift' else 'py-embed'
    return r['area']
print('--- lang/tests split ---')
c=collections.Counter(sub(r) for r in R)
for k,v in c.most_common(): print('  %-30s %d'%(k,v))
print()
print('--- & vs &mut by area ---')
t=collections.defaultdict(collections.Counter)
for r in R: t[r['area']][r['op']]+=1
for a in sorted(t): print('  %-20s &=%5d  &mut=%5d'%(a,t[a]['&'],t[a]['&mut']))
print()
print('--- rvalue by area & family ---')
t2=collections.Counter((r['area'],r['fam']) for r in R if r['opkind']=='rvalue')
for k,v in t2.most_common(12): print('  ',k,v)
print()
print('--- family x operand kind ---')
t3=collections.defaultdict(collections.Counter)
for r in R: t3[r['fam']][r['opkind']]+=1
for f in t3: print('  %-8s lvalue=%5d rvalue=%5d'%(f,t3[f]['lvalue'],t3[f]['rvalue']))
print()
print('--- family x typevar ---')
t4=collections.defaultdict(collections.Counter)
for r in R: t4[r['fam']]['tv' if r['tv'] else 'con']+=1
for f in t4: print('  %-8s concrete=%5d  &-over-TV=%5d'%(f,t4[f]['con'],t4[f]['tv']))
print()
print('--- confidence tiers ---')
c=collections.Counter(r['conf'] for r in R)
for k,v in c.most_common(): print('  %-26s %d (%.1f%%)'%(k,v,100*v/len(R)))
print()
print('--- ambiguous(overload) sites resolved as FIRING ---', sum(1 for r in R if r['ambiguous']))
