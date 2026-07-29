import sys; sys.path.insert(0,'/home/sl/src/drift-lang'); sys.path.insert(0,'/home/sl/src/drift-lang/lang')
import pickle,collections,os,json
from lang.driftc.parser import ast as A
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
raw=pickle.load(open(S+'/units.pkl','rb'))
# interfaces with &-declared formals (non-self)
ifm=collections.defaultdict(list)
ifaces={}
for uid,path,area,kind,off,err,prog in raw:
    if prog is None: continue
    for i in prog.interfaces:
        ifaces[i.name]=path
        for m in i.methods:
            ps=list(m.params); ps2=ps[1:] if ps and ps[0].name=='self' else ps
            refidx=[k for k,p in enumerate(ps2) if p.type_expr is not None and p.type_expr.name in ('&','&mut')]
            if refidx:
                ifm[m.name].append((i.name,path,m.loc.line,refidx,[ (p.name, p.type_expr.name+'<'+(p.type_expr.args[0].name if p.type_expr.args else '?')+'>') for p in ps2]))
print('interfaces total:',len(ifaces))
print('interface methods with &-formals:',sum(len(v) for v in ifm.values()))
for n,v in sorted(ifm.items()):
    for e in v: print('  %-24s iface=%-24s %s:%d refidx=%s %s'%(n,e[0],e[1],e[2],e[3],e[4]))
json.dump({k:[ (e[0],e[1],e[2],e[3]) for e in v] for k,v in ifm.items()}, open(S+'/ifacemethods.json','w'))
