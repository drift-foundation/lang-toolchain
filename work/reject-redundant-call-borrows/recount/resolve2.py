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
    u.dirkey=os.path.dirname(path); units.append(u)
good=[u for u in units if u.prog is not None]

TYPE_NAMES=set(); VARIANT_ARMS=set(); INTERFACE_NAMES=set(); STRUCT_FIELDS={}; ALIASES={}
for u in good:
    p=u.prog; u.module=p.module; u.imports={}
    for im in p.imports:
        al=im.alias or (im.path[-1] if im.path else None)
        if al: u.imports[al]='.'.join(im.path)
    for s in p.structs:
        TYPE_NAMES.add(s.name); STRUCT_FIELDS.setdefault(s.name,{})
        for f in s.fields: STRUCT_FIELDS[s.name][f.name]=f.type_expr
    for v in p.variants:
        TYPE_NAMES.add(v.name)
        for a in v.arms: VARIANT_ARMS.add(a.name)
    for e in p.exceptions: TYPE_NAMES.add(e.name)
    for t in p.traits: TYPE_NAMES.add(t.name)
    for i in p.interfaces:
        TYPE_NAMES.add(i.name); INTERFACE_NAMES.add(i.name)
    for ta in p.type_aliases:
        TYPE_NAMES.add(ta.name); ALIASES[ta.name]=ta.target

class Decl:
    __slots__=('name','params','tparams','unit','kind','owner','has_self','loc')
    def __init__(s,name,params,tparams,unit,kind,owner,loc):
        s.name=name; s.params=list(params); s.tparams=set(tparams); s.unit=unit
        s.kind=kind; s.owner=owner; s.loc=loc
        s.has_self=bool(s.params) and s.params[0].name=='self'
    def nargs(s, method_call):
        return len(s.params)-1 if (method_call and s.has_self) else len(s.params)

FREE=collections.defaultdict(list); METH=collections.defaultdict(list); ASSOC=collections.defaultdict(list)
IFACE_DECL=collections.defaultdict(list)
for u in good:
    p=u.prog
    for f in p.functions: FREE[f.name].append(Decl(f.name,f.params,f.type_params,u,'free',None,f.loc))
    for im in p.implements:
        tgt=im.target.name if im.target else None; tp=list(im.type_params)
        for m in im.methods:
            d=Decl(m.name,m.params,tp+list(m.type_params),u,'impl',tgt,m.loc)
            (METH[m.name] if d.has_self else ASSOC[(tgt,m.name)]).append(d)
    for i in p.interfaces:
        for m in i.methods:
            d=Decl(m.name,m.params,list(i.type_params)+list(m.type_params),u,'iface',i.name,m.loc)
            METH[m.name].append(d); IFACE_DECL[m.name].append(d)
    for t in p.traits:
        for m in t.methods:
            d=Decl(m.name,m.params,list(t.type_params)+list(m.type_params),u,'trait',t.name,m.loc)
            (METH[m.name] if d.has_self else ASSOC[(t.name,m.name)]).append(d)

MEM_INTRINSICS={'swap','rawbuffer_ptr','rawbuffer_cap','write','read','ptr_at_ref','ptr_at_mut','replace',
 'maybe_uninit','maybe_write','maybe_assume_init_ref','maybe_assume_init_mut','maybe_assume_init_read',
 'alloc_uninit','dealloc','capacity','rawbuffer_from_parts','forget','size_of','align_of'}

# builtin (compiler-internal) callees whose formal at index i is a reference.
# name -> {argindex: 'ref'|'val'} ; owner class noted for reporting
BUILTIN_REF = {
  ('method','extend'): {0:'&Array<T>'},
  ('method','get_mut'): {0:'&K'},
  ('method','get'): {0:'&K'},
  ('method','contains'): {0:'&K'},
  ('method','remove'): {0:'&K'},
  ('free','byte_length'): {0:'&String'},
  ('free','string_bytes_base'): {0:'&String'},
  ('free','string_byte_at'): {0:'&String'},
  ('free','string_eq'): {0:'&String',1:'&String'},
  ('free','string_concat'): {0:'&String',1:'&String'},
}

def is_ref_te(te): return te is not None and te.name in ('&','&mut')
def inner_of(te): return te.args[0] if te and te.args else None
def strip_ref(te):
    while te is not None and te.name in ('&','&mut'): te=te.args[0] if te.args else None
    return te
def walk(node,out):
    if isinstance(node,(list,tuple)):
        for x in node: walk(x,out)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node,type):
        if isinstance(node,(A.TypeExpr,A.Located)): return
        out.append(node)
        for f in dataclasses.fields(node):
            v=getattr(node,f.name)
            if v is None or isinstance(v,(A.TypeExpr,A.Located)): continue
            walk(v,out)
LV=(A.Name,A.Attr,A.Index,A.QualifiedMember,A.SelfRef)
def operand_kind(e):
    if isinstance(e,A.Unary): return 'lvalue' if e.op=='*' else 'rvalue'
    if isinstance(e,LV): return 'lvalue'
    if isinstance(e,(A.Move,A.Copy,A.Share)): return 'lvalue'
    return 'rvalue'
def rsub(e):
    if isinstance(e,(A.Literal,A.UintLiteral,A.Uint64Literal)): return 'literal'
    if isinstance(e,A.FString): return 'fstring'
    if isinstance(e,(A.Call,A.MacroCall,A.TypeApp)): return 'call'
    if isinstance(e,A.Lambda): return 'lambda'
    if isinstance(e,(A.ArrayLiteral,A.MapLiteral)): return 'container-literal'
    if isinstance(e,A.Binary): return 'binary'
    if isinstance(e,A.Cast): return 'cast'
    return 'other-expr'
def unwrap_callee(f):
    while isinstance(f,A.TypeApp): f=f.func
    if isinstance(f,A.Name): return ('free',f.ident,None)
    if isinstance(f,A.Attr): return ('attr',f.attr,f.value)
    if isinstance(f,A.QualifiedMember): return ('qual',f.member,f.base_type)
    if isinstance(f,A.Lambda): return ('lambda',None,f)
    return ('other',None,f)
def build_env(params,block):
    env={}
    for p in params:
        if p.type_expr is not None: env[p.name]=p.type_expr
    nodes=[]; walk(block,nodes)
    for n in nodes:
        if isinstance(n,A.LetStmt) and n.type_expr is not None: env[n.name]=n.type_expr
        elif isinstance(n,A.LocalConstStmt) and n.type_expr is not None: env[n.name]=n.type_expr
        elif isinstance(n,A.ForStmt) and n.var_type_expr is not None: env[n.var]=n.var_type_expr
        elif isinstance(n,A.ForCountStmt) and n.init_type_expr is not None and n.init_name: env[n.init_name]=n.init_type_expr
        elif isinstance(n,A.Lambda):
            for p in n.params:
                if p.type_expr is not None: env.setdefault(p.name,p.type_expr)
    return env
def resolve_alias(n,depth=0):
    while n in ALIASES and ALIASES[n] is not None and depth<8:
        t=strip_ref(ALIASES[n])
        if t is None or t.name==n: break
        n=t.name; depth+=1
    return n
def recv_type_name(recv,env,selft):
    if isinstance(recv,A.Name):
        if recv.ident=='self' and selft: return selft
        te=env.get(recv.ident)
        if te is not None:
            t=strip_ref(te)
            if t is not None: return resolve_alias(t.name)
    if isinstance(recv,A.SelfRef) and selft: return selft
    if isinstance(recv,A.Attr):
        b=recv_type_name(recv.value,env,selft)
        if b and b in STRUCT_FIELDS:
            te=STRUCT_FIELDS[b].get(recv.attr)
            if te is not None:
                t=strip_ref(te)
                if t is not None: return resolve_alias(t.name)
    return None

results=[]; unresolved=collections.Counter(); unresolved_sites=[]; excluded=collections.Counter()
macro_borrows=0; total_borrow_args=0; ambiguous_sites=[]

def formal_for(d,idx,kw,mc):
    ps=list(d.params)
    if mc and d.has_self: ps=ps[1:]
    if kw is not None:
        for p in ps:
            if p.name==kw: return p
        return None
    return ps[idx] if idx<len(ps) else None

def narrow(cands,unit,nargs,mc,owner=None):
    tier='global-multi'
    if owner:
        sub=[d for d in cands if d.owner==owner]
        if sub: return sub,'exact-owner'
    ar=[d for d in cands if d.nargs(mc)==nargs]
    if ar: cands=ar; tier='arity'
    for pred,name in ((lambda d: d.unit is unit,'same-unit'),
                      (lambda d: unit.module and d.unit.module==unit.module,'same-module'),
                      (lambda d: d.unit.dirkey==unit.dirkey,'same-dir')):
        sub=[d for d in cands if pred(d)]
        if sub: return sub,name
    if len(cands)==1: return cands,'global-unique'
    return cands,tier

def record(ctx,a,formal_te,decl,fam,tparams,i,k,conf,amb=False,builtin=None):
    inner=inner_of(formal_te) if formal_te is not None else None
    iname=inner.name if inner is not None else None
    tv=bool(iname and iname in tparams)
    if not tv and iname and len(iname)<=2 and iname[0].isupper() and iname.isalnum() and iname not in TYPE_NAMES:
        tv=True
    r=dict(ctx); r.update(dict(op=a.op,opkind=operand_kind(a.operand),
        opsub=rsub(a.operand) if operand_kind(a.operand)=='rvalue' else None,
        formal=(formal_te.name if formal_te is not None else builtin),
        inner=iname, tv=tv, fam=fam, conf=conf, ambiguous=amb, argidx=i, kw=k,
        callee=(decl.name if decl else ('lambda' if fam=='iife' else 'builtin')),
        decl_kind=(decl.kind if decl else ('lambda' if fam=='iife' else 'builtin')),
        decl_owner=(decl.owner if decl else None),
        decl_path=(decl.unit.path if decl else None),
        decl_line=(decl.loc.line if decl and decl.loc else 0)))
    results.append(r)

def classify(ctx,a,cands,i,k,mc,fam,cname,conf):
    forms=[(d,formal_for(d,i,k,mc)) for d in cands]
    hit=[(d,p) for d,p in forms if p is not None and is_ref_te(p.type_expr)]
    miss=[(d,p) for d,p in forms if p is None or not is_ref_te(p.type_expr)]
    if hit and miss:
        ambiguous_sites.append((ctx['path'],ctx['line'],cname,len(hit),len(miss)))
        record(ctx,a,hit[0][1].type_expr,hit[0][0],fam,hit[0][0].tparams,i,k,conf,amb=True); return
    if hit:
        d,p=hit[0]; record(ctx,a,p.type_expr,d,fam,d.tparams,i,k,conf); return
    if all(p is None for _,p in forms):
        return 'arity'
    excluded['formal-not-ref:'+fam]+=1
    return None

def scan(u,fname,params,block,tparams,selft):
    global total_borrow_args,macro_borrows
    if block is None: return
    env=build_env(params,block); nodes=[]; walk(block,nodes)
    for n in nodes:
        if isinstance(n,A.MacroCall):
            mb=[a for a in list(n.args)+[kw.value for kw in n.kwargs] if isinstance(a,A.Unary) and a.op in ('&','&mut')]
            macro_borrows+=len(mb); continue
        if not isinstance(n,A.Call): continue
        pairs=[(i,None,a) for i,a in enumerate(n.args)]+[(None,kw.name,kw.value) for kw in n.kwargs]
        bor=[(i,k,a) for i,k,a in pairs if isinstance(a,A.Unary) and a.op in ('&','&mut')]
        if not bor: continue
        total_borrow_args+=len(bor)
        nargs=len(n.args)+len(n.kwargs)
        ck,cname,qual=unwrap_callee(n.func)
        line=n.loc.line if n.loc else 0
        ctx=dict(unit=u.uid,path=u.path,area=u.area,kind=u.kind,off=u.off,line=line,fn=fname)
        if ck=='lambda':
            ps=list(qual.params)
            for i,k,a in bor:
                p=None
                if k is not None:
                    for pp in ps:
                        if pp.name==k: p=pp
                elif i<len(ps): p=ps[i]
                if p is not None and is_ref_te(p.type_expr):
                    record(ctx,a,p.type_expr,None,'iife',set(),i,k,'exact')
                else: excluded['iife-nonref']+=1
            continue
        if ck=='other':
            unresolved['callee-expr-not-name']+=len(bor); unresolved_sites.append((u.path,line,'callee-expr','')); continue
        if cname=='call':
            excluded['dot-call-fn-value']+=len(bor); continue
        if ck=='qual':
            tname=qual.name if isinstance(qual,A.TypeExpr) else None
            if cname in VARIANT_ARMS:
                excluded['variant-ctor-qualified']+=len(bor); continue
            c=ASSOC.get((tname,cname),[]) or [d for d in METH.get(cname,[]) if d.owner==tname]
            if not c: c=[d for kk,v in ASSOC.items() if kk[1]==cname for d in v]
            if not c:
                unresolved['qual:'+str(tname)+'::'+str(cname)]+=len(bor); unresolved_sites.append((u.path,line,'qual',str(tname)+'::'+str(cname))); continue
            c,conf=narrow(c,u,nargs,False,owner=tname if ASSOC.get((tname,cname)) else None)
            for i,k,a in bor:
                if classify(ctx,a,c,i,k,False,'assoc',cname,conf)=='arity':
                    unresolved['arity-qual:'+str(cname)]+=1; unresolved_sites.append((u.path,line,'arity-qual',str(cname)))
            continue
        if ck=='free':
            if cname in TYPE_NAMES or cname in VARIANT_ARMS:
                excluded['ctor-typename']+=len(bor); continue
            if cname and cname[0].isupper(): excluded['ctor-capitalized']+=len(bor); continue
            c=FREE.get(cname,[])
            if not c:
                bi=BUILTIN_REF.get(('free',cname))
                if bi is not None:
                    for i,k,a in bor:
                        if i in bi: record(ctx,a,None,None,'builtin',set(),i,k,'builtin',builtin=bi[i])
                        else: excluded['builtin-nonref']+=1
                    continue
                if cname in env: excluded['fn-typed-local-var-call']+=len(bor); continue
                unresolved['free:'+str(cname)]+=len(bor); unresolved_sites.append((u.path,line,'free',str(cname))); continue
            c,conf=narrow(c,u,nargs,False)
            for i,k,a in bor:
                if classify(ctx,a,c,i,k,False,'free',cname,conf)=='arity':
                    unresolved['arity-free:'+str(cname)]+=1; unresolved_sites.append((u.path,line,'arity-free',str(cname)))
            continue
        recv=qual
        modname=None
        if isinstance(recv,A.Name) and recv.ident in u.imports and recv.ident not in env: modname=u.imports[recv.ident]
        if modname is not None:
            if cname in TYPE_NAMES or (cname and cname[0].isupper()):
                excluded['ctor-module-qualified']+=len(bor); continue
            mc_=[d for d in FREE.get(cname,[]) if d.unit.module==modname] or \
                [d for d in FREE.get(cname,[]) if d.unit.module and d.unit.module.endswith(modname)] or FREE.get(cname,[])
            if not mc_:
                unresolved['modfn:'+str(modname)+'.'+str(cname)]+=len(bor); unresolved_sites.append((u.path,line,'modfn',str(modname)+'.'+str(cname))); continue
            ismem=(modname.endswith('std.mem') or modname=='mem' or modname.endswith('.mem')) and cname in MEM_INTRINSICS
            fam='mem' if ismem else 'free'
            mc2,conf=narrow(mc_,u,nargs,False)
            for i,k,a in bor:
                if classify(ctx,a,mc2,i,k,False,fam,cname,conf)=='arity':
                    unresolved['arity-mod:'+str(cname)]+=1; unresolved_sites.append((u.path,line,'arity-mod',str(cname)))
            continue
        rtn=recv_type_name(recv,env,selft)
        cands=METH.get(cname,[])
        iface = bool(rtn and rtn in INTERFACE_NAMES)
        pending=[]
        if cands:
            c,conf=narrow(cands,u,nargs,True,owner=rtn)
            fam='iface' if iface else 'method'
            for i,k,a in bor:
                if classify(ctx,a,c,i,k,True,fam,cname,conf)=='arity': pending.append((i,k,a))
        else:
            pending=list(bor)
        if pending:
            bi=BUILTIN_REF.get(('method',cname))
            if bi is not None:
                for i,k,a in pending:
                    if i in bi: record(ctx,a,None,None,'builtin',set(),i,k,'builtin',builtin=bi[i])
                    else: excluded['builtin-nonref']+=1
            else:
                unresolved['method:'+str(cname)]+=len(pending); unresolved_sites.append((u.path,line,'method',str(cname)))

for u in good:
    p=u.prog
    for f in p.functions: scan(u,f.name,list(f.params),f.body,f.type_params,None)
    for im in p.implements:
        tgt=im.target.name if im.target else None
        for m in im.methods: scan(u,(tgt or '?')+'.'+m.name,list(m.params),m.body,list(im.type_params)+list(m.type_params),tgt)
    if p.statements: scan(u,'<toplevel>',[],A.Block(statements=list(p.statements)),[],None)

json.dump(results,open(S+'/results2.json','w'))
json.dump({'unresolved':dict(unresolved),'excluded':dict(excluded),'total_borrow_args':total_borrow_args,
 'macro_borrows':macro_borrows,'ambiguous_sites':ambiguous_sites,'unresolved_sites':unresolved_sites},open(S+'/diag2.json','w'))
print('borrow-args in Call arg position:',total_borrow_args)
print('borrow-args in MacroCall args (excluded):',macro_borrows)
print('FIRING:',len(results))
print('excluded:',dict(excluded))
print('unresolved:',sum(unresolved.values()))
print('ambiguous:',len(ambiguous_sites))
