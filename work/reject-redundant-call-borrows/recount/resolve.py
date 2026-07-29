import sys; sys.path.insert(0,'/home/sl/src/drift-lang'); sys.path.insert(0,'/home/sl/src/drift-lang/lang')
import pickle, collections, os, re, dataclasses, json
sys.setrecursionlimit(200000)
from lang.driftc.parser import ast as A
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
ROOT='/home/sl/src/drift-lang'
raw=pickle.load(open(S+'/units.pkl','rb'))

class U: pass
units=[]
for uid,path,area,kind,off,err,prog in raw:
    u=U(); u.uid=uid; u.path=path; u.area=area; u.kind=kind; u.off=off; u.err=err; u.prog=prog
    u.dirkey=os.path.dirname(path)
    units.append(u)
UBY={u.uid:u for u in units}
good=[u for u in units if u.prog is not None]

# ---------------- global indices ----------------
TYPE_NAMES=set()          # struct/variant/exception/interface/trait/alias names
VARIANT_ARMS=set()        # arm ctor names
INTERFACE_NAMES=set()
IFACE_METHODS=collections.defaultdict(list)   # name -> [(ifacename, sig, unit)]
STRUCT_FIELDS={}          # (structname) -> {field: TypeExpr}
ALIASES={}                # alias name -> target TypeExpr

for u in good:
    p=u.prog
    u.module = p.module
    u.imports = {}
    for im in p.imports:
        al = im.alias or (im.path[-1] if im.path else None)
        if al: u.imports[al]='.'.join(im.path)
    for s in p.structs:
        TYPE_NAMES.add(s.name)
        STRUCT_FIELDS.setdefault(s.name,{})
        for f in s.fields: STRUCT_FIELDS[s.name][f.name]=f.type_expr
    for v in p.variants:
        TYPE_NAMES.add(v.name)
        for a in v.arms: VARIANT_ARMS.add(a.name)
    for e in p.exceptions: TYPE_NAMES.add(e.name)
    for t in p.traits: TYPE_NAMES.add(t.name)
    for i in p.interfaces:
        TYPE_NAMES.add(i.name); INTERFACE_NAMES.add(i.name)
        for m in i.methods: IFACE_METHODS[m.name].append((i.name,m,u))
    for ta in p.type_aliases:
        TYPE_NAMES.add(ta.name); ALIASES[ta.name]=ta.target

# decl record
class Decl:
    __slots__=('name','params','tparams','unit','kind','owner','has_self','loc')
    def __init__(self,name,params,tparams,unit,kind,owner,loc):
        self.name=name; self.params=list(params); self.tparams=set(tparams)
        self.unit=unit; self.kind=kind; self.owner=owner; self.loc=loc
        self.has_self = bool(self.params) and self.params[0].name=='self'

FREE=collections.defaultdict(list)     # name -> [Decl]
METH=collections.defaultdict(list)     # name -> [Decl]  (impl methods + iface sigs + trait sigs)
IMPL_METH=collections.defaultdict(list)
ASSOC=collections.defaultdict(list)    # (typename, name) -> [Decl]

for u in good:
    p=u.prog
    for f in p.functions:
        FREE[f.name].append(Decl(f.name,f.params,f.type_params,u,'free',None,f.loc))
    for im in p.implements:
        tgt = im.target.name if im.target else None
        tp = list(im.type_params)
        for m in im.methods:
            d=Decl(m.name,m.params,tp+list(m.type_params),u,'impl',tgt,m.loc)
            if d.has_self:
                METH[m.name].append(d); IMPL_METH[m.name].append(d)
            else:
                ASSOC[(tgt,m.name)].append(d)
    for i in p.interfaces:
        for m in i.methods:
            d=Decl(m.name,m.params,list(i.type_params)+list(m.type_params),u,'iface',i.name,m.loc)
            METH[m.name].append(d)
    for t in p.traits:
        for m in t.methods:
            d=Decl(m.name,m.params,list(t.type_params)+list(m.type_params),u,'trait',t.name,m.loc)
            if d.has_self: METH[m.name].append(d)
            else: ASSOC[(t.name,m.name)].append(d)

MODULE_UNITS=collections.defaultdict(list)
for u in good:
    if u.module: MODULE_UNITS[u.module].append(u)

MEM_INTRINSICS={'swap','rawbuffer_ptr','rawbuffer_cap','write','read','ptr_at_ref','ptr_at_mut','replace',
                'maybe_uninit','maybe_write','maybe_assume_init_ref','maybe_assume_init_mut','maybe_assume_init_read',
                'alloc_uninit','dealloc','capacity','rawbuffer_from_parts','forget','size_of','align_of'}
MEM_UNIT_PATHS={'stdlib/std/mem/mem.drift'}

# ---------------- helpers ----------------
def is_ref_te(te):
    return te is not None and te.name in ('&','&mut')

def inner_of(te):
    return te.args[0] if te and te.args else None

def strip_ref(te):
    while te is not None and te.name in ('&','&mut'):
        te = te.args[0] if te.args else None
    return te

def dc_children(node):
    for f in dataclasses.fields(node):
        yield getattr(node,f.name)

def walk(node, out):
    """collect all Expr and Stmt & Block nodes"""
    if isinstance(node,(list,tuple)):
        for x in node: walk(x,out)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node,type):
        if isinstance(node,A.TypeExpr) or isinstance(node,A.Located): return
        out.append(node)
        for f in dataclasses.fields(node):
            v=getattr(node,f.name)
            if isinstance(v,(A.TypeExpr,A.Located)) or v is None: continue
            walk(v,out)

# operand classification
LVALUE_NODES=(A.Name,A.Attr,A.Index,A.QualifiedMember,A.SelfRef)
def operand_kind(e):
    if isinstance(e,A.Unary):
        if e.op=='*': return 'lvalue'
        return 'rvalue'
    if isinstance(e,LVALUE_NODES):
        return 'lvalue'
    if isinstance(e,(A.Move,A.Copy,A.Share)):
        return 'lvalue'
    return 'rvalue'

def rvalue_sub(e):
    if isinstance(e,(A.Literal,A.UintLiteral,A.Uint64Literal,A.FString)): return 'literal'
    if isinstance(e,(A.Call,A.MacroCall,A.TypeApp)): return 'call'
    if isinstance(e,A.Lambda): return 'lambda'
    if isinstance(e,(A.ArrayLiteral,A.MapLiteral)): return 'container-literal'
    return 'other-expr'

# ---------------- callee resolution ----------------
def unwrap_callee(fexpr):
    """return (kind, name, qualifier_expr_or_typename)"""
    while isinstance(fexpr,A.TypeApp):
        fexpr=fexpr.func
    if isinstance(fexpr,A.Name):
        return ('free',fexpr.ident,None)
    if isinstance(fexpr,A.Attr):
        return ('attr',fexpr.attr,fexpr.value)
    if isinstance(fexpr,A.QualifiedMember):
        return ('qual',fexpr.member,fexpr.base_type)
    if isinstance(fexpr,A.Lambda):
        return ('lambda',None,fexpr)
    return ('other',None,fexpr)

def pick(cands, unit):
    """narrow candidate decls by locality"""
    for pred in (lambda d: d.unit is unit,
                 lambda d: unit.module and d.unit.module==unit.module,
                 lambda d: d.unit.dirkey==unit.dirkey):
        sub=[d for d in cands if pred(d)]
        if sub: return sub
    return cands

def formal_for(d, idx, kwname, is_method_call):
    ps=list(d.params)
    if is_method_call and d.has_self: ps=ps[1:]
    if kwname is not None:
        for p in ps:
            if p.name==kwname: return p
        return None
    if idx < len(ps): return ps[idx]
    return None

# local type env for a function body: name -> TypeExpr
def build_env(fn_params, block):
    env={}
    for p in fn_params:
        if p.type_expr is not None: env[p.name]=p.type_expr
    nodes=[]
    walk(block,nodes)
    for n in nodes:
        if isinstance(n,A.LetStmt) and n.type_expr is not None: env[n.name]=n.type_expr
        elif isinstance(n,A.LocalConstStmt) and n.type_expr is not None: env[n.name]=n.type_expr
        elif isinstance(n,A.ForStmt) and n.var_type_expr is not None: env[n.var]=n.var_type_expr
        elif isinstance(n,A.Lambda):
            for p in n.params:
                if p.type_expr is not None: env.setdefault(p.name,p.type_expr)
    return env

def recv_type_name(recv, env, self_type_name):
    if isinstance(recv,A.Name):
        if recv.ident=='self' and self_type_name: return self_type_name
        te=env.get(recv.ident)
        if te is not None:
            t=strip_ref(te)
            if t is not None:
                n=t.name
                while n in ALIASES and ALIASES[n] is not None:
                    nn=strip_ref(ALIASES[n])
                    if nn is None or nn.name==n: break
                    n=nn.name
                return n
    if isinstance(recv,A.Attr):
        base=recv_type_name(recv.value,env,self_type_name)
        if base and base in STRUCT_FIELDS:
            te=STRUCT_FIELDS[base].get(recv.attr)
            if te is not None:
                t=strip_ref(te)
                if t is not None: return t.name
    return None

# ---------------- main scan ----------------
results=[]
unresolved=collections.Counter()
unresolved_sites=[]
ambiguous_sites=[]
excluded=collections.Counter()
total_borrow_args=0

def scan_fn_like(u, fname_ctx, params, block, tparams, self_type_name):
    global total_borrow_args
    if block is None: return
    env=build_env(params, block)
    nodes=[]
    walk(block,nodes)
    for n in nodes:
        if not isinstance(n,A.Call): continue
        pairs=[]
        for i,a in enumerate(n.args): pairs.append((i,None,a))
        for kw in n.kwargs: pairs.append((None,kw.name,kw.value))
        borrows=[(i,k,a) for (i,k,a) in pairs if isinstance(a,A.Unary) and a.op in ('&','&mut')]
        if not borrows: continue
        total_borrow_args+=len(borrows)
        ck,cname,qual = unwrap_callee(n.func)
        line = (n.loc.line if n.loc else 0)
        ctx=dict(unit=u.uid,path=u.path,area=u.area,kind=u.kind,off=u.off,line=line,fn=fname_ctx)

        # --- exclusions ---
        if ck=='lambda':
            # immediately invoked lambda
            lam=qual
            for (i,k,a) in borrows:
                p=None
                ps=list(lam.params)
                if k is not None:
                    for pp in ps:
                        if pp.name==k: p=pp
                elif i<len(ps): p=ps[i]
                if p is not None and is_ref_te(p.type_expr):
                    record(ctx,a,p,None,'iife',set(),i,k)
                else:
                    excluded['iife-nonref-formal']+=1
            continue
        if ck=='other':
            for _ in borrows: unresolved['callee-expr-not-name']+=1
            unresolved_sites.append((u.path,line,'callee-expr'))
            continue
        if cname=='call':
            excluded['dot-call-fn-value']+=len(borrows); continue
        if ck=='qual':
            tname = qual.name if isinstance(qual,A.TypeExpr) else None
            if cname in VARIANT_ARMS or (tname and tname in TYPE_NAMES and cname in VARIANT_ARMS):
                excluded['variant-ctor-qualified']+=len(borrows); continue
            cands = ASSOC.get((tname,cname),[]) + [d for d in METH.get(cname,[]) if d.owner==tname]
            if not cands:
                cands=[d for (k,v) in ASSOC.items() if k[1]==cname for d in v]
            if not cands:
                for _ in borrows: unresolved['qual-assoc-unknown:'+str(tname)+'::'+str(cname)]+=1
                unresolved_sites.append((u.path,line,'qual::'+str(tname)+'::'+str(cname)))
                continue
            cands=pick(cands,u)
            for (i,k,a) in borrows:
                classify(ctx,a,cands,i,k,False,'assoc',cname,None)
            continue
        if ck=='free':
            if cname in TYPE_NAMES or cname in VARIANT_ARMS:
                excluded['ctor-typename']+=len(borrows); continue
            if cname and cname[0].isupper():
                excluded['ctor-capitalized']+=len(borrows); continue
            cands=FREE.get(cname,[])
            if not cands:
                # maybe a local fn-typed variable being called, or a lambda binding
                te=env.get(cname)
                if te is not None:
                    excluded['fn-typed-local-var-call']+=len(borrows); continue
                for _ in borrows: unresolved['free-unknown:'+str(cname)]+=1
                unresolved_sites.append((u.path,line,'free:'+str(cname)))
                continue
            cands=pick(cands,u)
            for (i,k,a) in borrows:
                classify(ctx,a,cands,i,k,False,'free',cname,None)
            continue
        # ck == 'attr'
        recv=qual
        # module-qualified free call?  `mod.fn(...)`
        modname=None
        if isinstance(recv,A.Name) and recv.ident in u.imports and recv.ident not in env:
            modname=u.imports[recv.ident]
        if modname is not None:
            if cname in TYPE_NAMES or (cname and cname[0].isupper()):
                excluded['ctor-module-qualified']+=len(borrows); continue
            mcands=[d for d in FREE.get(cname,[]) if d.unit.module==modname]
            if not mcands: mcands=[d for d in FREE.get(cname,[]) if d.unit.module and d.unit.module.endswith(modname)]
            if not mcands: mcands=FREE.get(cname,[])
            if not mcands:
                for _ in borrows: unresolved['modfn-unknown:'+str(modname)+'.'+str(cname)]+=1
                unresolved_sites.append((u.path,line,'mod:'+str(modname)+'.'+str(cname)))
                continue
            fam='mem' if (modname.endswith('std.mem') or modname=='mem') and cname in MEM_INTRINSICS else 'free'
            for (i,k,a) in borrows:
                classify(ctx,a,mcands,i,k,False,fam,cname,None)
            continue
        # true method call
        rtn = recv_type_name(recv, env, self_type_name)
        cands=METH.get(cname,[])
        if not cands:
            for _ in borrows: unresolved['method-unknown:'+str(cname)]+=1
            unresolved_sites.append((u.path,line,'method:'+str(cname)))
            continue
        narrowed=[d for d in cands if rtn and d.owner==rtn]
        iface_dispatch = bool(rtn and rtn in INTERFACE_NAMES)
        if narrowed: use=narrowed
        else: use=pick(cands,u)
        fam='iface' if iface_dispatch else 'method'
        for (i,k,a) in borrows:
            classify(ctx,a,use,i,k,True,fam,cname,rtn)

def classify(ctx,a,cands,i,k,is_method_call,fam,cname,rtn):
    formals=[]
    for d in cands:
        p=formal_for(d,i,k,is_method_call)
        formals.append((d,p))
    votes=set()
    for d,p in formals:
        if p is None: votes.add(None)
        else: votes.add(bool(is_ref_te(p.type_expr)))
    hit=[(d,p) for d,p in formals if p is not None and is_ref_te(p.type_expr)]
    miss=[(d,p) for d,p in formals if p is None or not is_ref_te(p.type_expr)]
    if hit and miss:
        ambiguous_sites.append((ctx['path'],ctx['line'],cname,len(hit),len(miss)))
        # majority/locality: prefer hit if any same-unit/module decl in hit
        record(ctx,a,hit[0][1],hit[0][0],fam,set(hit[0][0].tparams),i,k,ambiguous=True)
        return
    if hit:
        d,p=hit[0]
        record(ctx,a,p,d,fam,set(d.tparams),i,k)
        return
    if not formals or all(p is None for _,p in formals):
        unresolved['arity-mismatch:'+str(cname)]+=1
        unresolved_sites.append((ctx['path'],ctx['line'],'arity:'+str(cname)))
        return
    excluded['formal-not-ref']+=1

def record(ctx,a,formal,decl,fam,tparams,i,k,ambiguous=False):
    te=formal.type_expr
    inner=inner_of(te)
    innername = inner.name if inner is not None else None
    is_tv = bool(innername and (innername in tparams or (len(innername)<=2 and innername[0].isupper() and innername.isalnum())))
    r=dict(ctx)
    r.update(dict(op=a.op, opkind=operand_kind(a.operand), opsub=rvalue_sub(a.operand) if operand_kind(a.operand)=='rvalue' else None,
                  formal=te.name, inner=innername, tv=is_tv, fam=fam,
                  callee=(decl.name if decl else 'lambda'),
                  decl_kind=(decl.kind if decl else 'lambda'),
                  decl_owner=(decl.owner if decl else None),
                  decl_path=(decl.unit.path if decl else ctx['path']),
                  decl_line=(decl.loc.line if decl and decl.loc else 0),
                  argidx=i, kw=k, ambiguous=ambiguous))
    results.append(r)

# drive
for u in good:
    p=u.prog
    for f in p.functions:
        scan_fn_like(u, f.name, list(f.params), f.body, f.type_params, None)
    for im in p.implements:
        tgt=im.target.name if im.target else None
        for m in im.methods:
            scan_fn_like(u, (tgt or '?')+'.'+m.name, list(m.params), m.body, list(im.type_params)+list(m.type_params), tgt)
    # top-level statements (scripts)
    if p.statements:
        blk=A.Block(statements=list(p.statements))
        scan_fn_like(u,'<toplevel>',[],blk,[],None)

json.dump(results, open(S+'/results.json','w'))
json.dump({'unresolved':dict(unresolved),'excluded':dict(excluded),
           'total_borrow_args':total_borrow_args,
           'ambiguous':len(ambiguous_sites),
           'unresolved_sites':unresolved_sites[:80],
           'ambiguous_sites':ambiguous_sites[:80]}, open(S+'/diag.json','w'))
print('total borrow-args seen in call positions:',total_borrow_args)
print('FIRING:',len(results))
print('excluded:',sum(excluded.values()),dict(excluded))
print('unresolved:',sum(unresolved.values()))
print('ambiguous(resolved-as-fire):',len(ambiguous_sites))
