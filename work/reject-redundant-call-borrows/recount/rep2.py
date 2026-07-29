import json, collections
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
R=json.load(open(S+'/results2.json')); D=json.load(open(S+'/diag2.json'))
print('ALL RECORDS:',len(R))
core=[r for r in R if r['fam']!='builtin']
bi=[r for r in R if r['fam']=='builtin']
print('  source-declared-& formals:',len(core))
print('  compiler-builtin ref formals:',len(bi))
def tab(t,f,items):
    c=collections.Counter(f(r) for r in items); print('--',t)
    for k,v in c.most_common(): print('   %-42s %d'%(k,v))
tab('area (core)',lambda r:r['area'],core)
tab('operand kind (core)',lambda r:r['opkind'],core)
tab('rvalue sub (core)',lambda r:str(r['opsub']),[r for r in core if r['opkind']=='rvalue'])
tab('formal typevar? (core)',lambda r:'ref-over-typevar' if r['tv'] else 'concrete-&T',core)
tab('family (core)',lambda r:r['fam'],core)
tab('& vs &mut (core)',lambda r:r['op'],core)
tab('confidence (core)',lambda r:r['conf'],core)
tab('decl kind (core)',lambda r:r['decl_kind'],core)
tab('builtin formals',lambda r:str(r['formal']),bi)
tab('builtin area',lambda r:r['area'],bi)
print()
print('unresolved:',D['unresolved'])
print('excluded:',D['excluded'])
print('ambiguous n=',len(D['ambiguous_sites']))
c=collections.Counter(a[2] for a in D['ambiguous_sites']); print('ambiguous callees:',c.most_common(15))
