import json,collections,random
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
R=[r for r in json.load(open(S+'/results3.json')) if r['fam']!='builtin']
def line(r):
    ln=r['line']+r['off']
    try: return open('/home/sl/src/drift-lang/'+r['path'],errors='replace').read().split('\n')[ln-1].strip()
    except Exception: return '?'
def show(title,pred,n=6):
    items=[r for r in R if pred(r)]
    print('== %s (n=%d)'%(title,len(items)))
    random.seed(3)
    for r in (random.sample(items,min(n,len(items)))):
        print('   %s:%d  | %s'%(r['path'],r['line']+r['off'],line(r)[:105]))
show('rvalue: string/num literal',lambda r:r['opsub']=='literal')
show('rvalue: call result',lambda r:r['opsub']=='call')
show('rvalue: container literal',lambda r:r['opsub']=='container-literal',5)
show('lvalue: deref &*p',lambda r:r['opkind']=='lvalue',6)
show('&-over-typevar formals',lambda r:r['tv'],8)
show('assoc (Type::fn)',lambda r:r['fam']=='assoc',5)
show('mem intrinsics',lambda r:r['fam']=='mem',6)
show('method (concrete impl)',lambda r:r['fam']=='method',6)
