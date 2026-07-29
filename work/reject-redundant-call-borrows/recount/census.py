import sys; sys.path.insert(0,'/home/sl/src/drift-lang'); sys.path.insert(0,'/home/sl/src/drift-lang/lang')
import pickle,collections,dataclasses,json
sys.setrecursionlimit(200000)
from lang.driftc.parser import ast as A
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
raw=pickle.load(open(S+'/units.pkl','rb'))
cnt=collections.Counter(); byarea=collections.Counter()
def isb(e): return isinstance(e,A.Unary) and e.op in ('&','&mut')
def visit(n,area,parentkind):
    if isinstance(n,(list,tuple)):
        for x in n: visit(x,area,parentkind)
        return
    if not dataclasses.is_dataclass(n) or isinstance(n,type): return
    if isinstance(n,(A.TypeExpr,A.Located)): return
    if isinstance(n,A.Call):
        for a in n.args:
            if isb(a): cnt['call-arg']+=1; byarea[(area,'call-arg')]+=1
        for kw in n.kwargs:
            if isb(kw.value): cnt['call-kwarg']+=1; byarea[(area,'call-kwarg')]+=1
        if isb(n.func): cnt['callee-borrow']+=1
    elif isinstance(n,A.MacroCall):
        for a in list(n.args)+[k.value for k in n.kwargs]:
            if isb(a): cnt['macro-arg']+=1; byarea[(area,'macro-arg')]+=1
    elif isinstance(n,A.ExceptionCtor):
        for f in dataclasses.fields(n):
            v=getattr(n,f.name)
            for x in (v if isinstance(v,(list,tuple)) else [v]):
                if isb(x): cnt['exception-ctor-arg']+=1; byarea[(area,'exception-ctor-arg')]+=1
                if isinstance(x,A.KwArg) and isb(x.value): cnt['exception-ctor-arg']+=1; byarea[(area,'exception-ctor-arg')]+=1
    elif isinstance(n,A.LetStmt):
        if isb(n.value): cnt['let-init']+=1; byarea[(area,'let-init')]+=1
    elif isinstance(n,A.AssignStmt):
        if isb(n.value): cnt['assign-rhs']+=1; byarea[(area,'assign-rhs')]+=1
    elif isinstance(n,A.ReturnStmt):
        if getattr(n,'value',None) is not None and isb(n.value): cnt['return']+=1; byarea[(area,'return')]+=1
        for f in dataclasses.fields(n):
            v=getattr(n,f.name)
            if isinstance(v,A.Unary) and v.op in ('&','&mut') and f.name!='value': cnt['return']+=1
    elif isinstance(n,A.ForStmt):
        if isb(n.iter_expr): cnt['for-in']+=1; byarea[(area,'for-in')]+=1
    elif isinstance(n,A.MatchExpr):
        if isb(getattr(n,'subject',None) or getattr(n,'value',None)): cnt['match-subject']+=1; byarea[(area,'match-subject')]+=1
    elif isinstance(n,A.Index):
        if isb(n.value): cnt['index-base']+=1
        if isb(n.index): cnt['index-idx']+=1
    elif isinstance(n,A.Attr):
        if isb(n.value): cnt['attr-base']+=1
    elif isinstance(n,A.Binary):
        for x in (n.left,n.right):
            if isb(x): cnt['binary-operand']+=1
    elif isinstance(n,A.Cast):
        if isb(n.expr): cnt['cast']+=1
    elif isinstance(n,(A.ArrayLiteral,)):
        for f in dataclasses.fields(n):
            v=getattr(n,f.name)
            if isinstance(v,(list,tuple)):
                for x in v:
                    if isb(x): cnt['array-literal-elem']+=1
    for f in dataclasses.fields(n):
        v=getattr(n,f.name)
        if v is None or isinstance(v,(A.TypeExpr,A.Located)): continue
        visit(v,area,type(n).__name__)
tot=0
for uid,path,area,kind,off,err,prog in raw:
    if prog is None: continue
    visit(prog,area,'root')
    # captures
for uid,path,area,kind,off,err,prog in raw:
    if prog is None: continue
    ns=[]
    def w(n):
        if isinstance(n,(list,tuple)):
            for x in n: w(x)
            return
        if not dataclasses.is_dataclass(n) or isinstance(n,type) or isinstance(n,(A.TypeExpr,A.Located)): return
        ns.append(n)
        for f in dataclasses.fields(n):
            v=getattr(n,f.name)
            if v is None or isinstance(v,(A.TypeExpr,A.Located)): continue
            w(v)
    w(prog)
    for n in ns:
        if isinstance(n,A.LambdaCapture) and n.kind in ('ref','ref_mut'):
            cnt['lambda-capture-ref']+=1; byarea[(area,'lambda-capture-ref')]+=1
        if isinstance(n,A.Unary) and n.op in ('&','&mut'): cnt['TOTAL-&-Unary-nodes']+=1; byarea[(area,'TOTAL')]+=1
for k,v in cnt.most_common(): print('%-26s %d'%(k,v))
print()
for k,v in sorted(byarea.items()): print(k,v)
