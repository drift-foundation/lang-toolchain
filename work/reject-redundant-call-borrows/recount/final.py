import json,collections,os
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
R=json.load(open(S+'/results3.json'))
core=[r for r in R if r['fam']!='builtin']
bi=[r for r in R if r['fam']=='builtin']
AREAS=['stdlib','examples','tools','lang-tests-drift','py-embed','issues','doc']
def M(items,keyfn):
    t=collections.defaultdict(collections.Counter)
    for r in items: t[r['area']][keyfn(r)]+=1
    return t
print('=== TABLE 1: total by area ===')
tot=collections.Counter(r['area'] for r in core)
for a in AREAS: print('%-20s %d'%(a,tot.get(a,0)))
print('TOTAL %d'%len(core))
print()
print('=== TABLE 2: operand kind x area ===')
t=M(core,lambda r:r['opkind'])
print('%-20s %8s %8s'%('area','lvalue','rvalue'))
for a in AREAS: print('%-20s %8d %8d'%(a,t[a]['lvalue'],t[a]['rvalue']))
print('%-20s %8d %8d'%('TOTAL',sum(t[a]['lvalue'] for a in AREAS),sum(t[a]['rvalue'] for a in AREAS)))
print(' rvalue subkinds:',collections.Counter(r['opsub'] for r in core if r['opkind']=='rvalue').most_common())
print()
print('=== TABLE 3: formal kind x area ===')
t=M(core,lambda r:'tv' if r['tv'] else 'concrete')
print('%-20s %10s %10s'%('area','concrete&T','&-over-TV'))
for a in AREAS: print('%-20s %10d %10d'%(a,t[a]['concrete'],t[a]['tv']))
print('%-20s %10d %10d'%('TOTAL',sum(t[a]['concrete'] for a in AREAS),sum(t[a]['tv'] for a in AREAS)))
print(' top &-over-typevar callees:',collections.Counter((r['callee'],r['formal']+'<'+str(r['inner'])+'>') for r in core if r['tv']).most_common(14))
print()
print('=== TABLE 4: callee family x area ===')
fams=['free','method','assoc','mem','iface','iife']
t=M(core,lambda r:r['fam'])
print('%-20s '%'area'+' '.join('%8s'%f for f in fams))
for a in AREAS: print('%-20s '%a+' '.join('%8d'%t[a][f] for f in fams))
print('%-20s '%'TOTAL'+' '.join('%8d'%sum(t[a][f] for a in AREAS) for f in fams))
print()
print('=== mem intrinsic breakdown ===')
print(collections.Counter(r['callee'] for r in core if r['fam']=='mem').most_common())
print(' by area:',collections.Counter(r['area'] for r in core if r['fam']=='mem').most_common())
print(' mem &-over-typevar:',sum(1 for r in core if r['fam']=='mem' and r['tv']))
print()
print('=== iife (immediately-invoked lambda) sites ===')
for r in core:
    if r['fam']=='iife':
        ln=r['line']+r['off']
        try: src=open('/home/sl/src/drift-lang/'+r['path'],errors='replace').read().split('\n')[ln-1].strip()
        except Exception: src='?'
        print('  %s:%d  %s'%(r['path'],ln,src[:110]))
print()
print('=== issues/ per file ===')
c=collections.Counter(r['path'] for r in core if r['area']=='issues')
for k,v in c.most_common(): print('  %-70s %d'%(k,v))
print()
print('=== interface-dispatch upper bound ===')
IF=json.load(open(S+'/ifacemethods.json'))
ub=[r for r in core if r['callee'] in IF]
print(' method-call firing sites whose callee name is declared in some interface with &-formal:',len([r for r in ub if r['fam'] in ('method','iface')]))
print(' confirmed iface-typed receiver:',len([r for r in core if r['fam']=='iface']))
print(' by callee:',collections.Counter(r['callee'] for r in ub if r['fam'] in ('method','iface')).most_common())
print()
print('=== builtin (compiler-internal & formals, NOT source-declared) ===')
c=collections.Counter((r['callee'],r['formal']) for r in bi); print(' ',c.most_common())
print(' by area:',collections.Counter(r['area'] for r in bi).most_common())
print()
print('=== stdlib per-file top ===')
print(collections.Counter(r['path'] for r in core if r['area']=='stdlib').most_common(12))
print()
print('=== py-embed top files ===')
print(collections.Counter(r['path'] for r in core if r['area']=='py-embed').most_common(10))
print(' distinct py files:',len(set(r['path'] for r in core if r['area']=='py-embed')))
print()
print('=== doc files ===')
print(collections.Counter(r['path'] for r in core if r['area']=='doc').most_common())
