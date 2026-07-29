import json,os,collections
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
R=json.load(open(S+'/results3.json'))
m=json.load(open('/home/sl/src/drift-lang/lang/tests/ownership_corpus/reviewed-baseline/manifest.json'))
U=m['universe']
fixtures={f['name'] for f in U['fixtures']}
compiled_ok=set(U['compiled_ok']); failed=set(U['failed'])
excluded={e['name'] for e in U['excluded']}
E2E='lang/tests/codegen/e2e/'
allc=[r for r in R]
core=[r for r in R if r['fam']!='builtin']
def dirs(items):
    d=collections.Counter()
    for r in items:
        p=r['path']
        if p.startswith(E2E):
            rest=p[len(E2E):]
            name=rest.split('/')[0]
            if name.startswith('__'): continue
            d[name]+=1
    return d
dc=dirs(core); dall=dirs(allc)
print('e2e fixture dirs on disk:', len([x for x in os.listdir('/home/sl/src/drift-lang/lang/tests/codegen/e2e') if os.path.isdir('/home/sl/src/drift-lang/lang/tests/codegen/e2e/'+x) and not x.startswith('__')]))
print('manifest fixtures universe:',len(fixtures),' compiled_ok:',len(compiled_ok),' failed:',len(failed),' excluded:',len(excluded))
print()
print('DIRS with >=1 scoped firing site (source-declared & formals):',len(dc))
print('   of those in manifest fixtures universe:',len(set(dc)&fixtures))
print('   of those in compiled_ok partition      :',len(set(dc)&compiled_ok))
print('   of those in failed partition           :',len(set(dc)&failed))
print('   of those in excluded partition         :',len(set(dc)&excluded))
print('   of those NOT in manifest at all        :',len(set(dc)-fixtures-excluded))
print()
print('DIRS incl. compiler-builtin-& formals:',len(dall),' in universe:',len(set(dall)&fixtures))
print()
# rvalue-only dirs
lv=dirs([r for r in core if r['opkind']=='lvalue']); rv=dirs([r for r in core if r['opkind']=='rvalue'])
print('dirs with lvalue firing:',len(lv),'dirs with rvalue firing:',len(rv),'rvalue-ONLY dirs:',len(set(rv)-set(lv)))
print('total firing sites inside e2e dirs:',sum(dc.values()))
print('top dirs:',dc.most_common(8))
json.dump({'dirs':dict(dc)},open(S+'/e2edirs.json','w'))
