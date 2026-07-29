import sys; sys.path.insert(0,'/home/sl/src/drift-lang'); sys.path.insert(0,'/home/sl/src/drift-lang/lang')
import os,re,ast as pyast,pickle,collections
sys.setrecursionlimit(200000)
from lang.driftc.parser.parser import parse_program
ROOT='/home/sl/src/drift-lang'
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
EXCL={'.git','build','.venv','dist','work','.mypy_cache','.pytest_cache','__pycache__','lang-obsolete','node_modules','assets'}
def area_of(rel):
    if rel.startswith('stdlib/'): return 'stdlib'
    if rel.startswith('examples/'): return 'examples'
    if rel.startswith('tools/'): return 'tools'
    if rel.startswith('issues/'): return 'issues'
    if rel.startswith('lang/tests/'): return 'lang-tests-drift'
    if rel.startswith('tests/'): return 'legacy-tests-drift'
    if rel.startswith('doc/'): return 'doc'
    return 'other'
units=[]  # (path, area, kind, src, line_off)
for dp,dns,fns in os.walk(ROOT):
    dns[:]=[d for d in dns if d not in EXCL]
    for f in sorted(fns):
        p=os.path.join(dp,f); rel=os.path.relpath(p,ROOT)
        if f.endswith('.drift'):
            units.append([rel,area_of(rel),'drift',open(p,encoding='utf-8',errors='replace').read(),0])
DRIFTISH=re.compile(r'(^|\n)[ \t]*(module|import|pub |fn |struct |variant |implement |interface |trait |val |var |const |error |exception )')
def looks_drift(s):
    if 'fn ' not in s and 'module ' not in s and 'val ' not in s and 'var ' not in s: return False
    return bool(DRIFTISH.search(s))
def py_strings(path):
    txt=open(path,encoding='utf-8',errors='replace').read()
    try: tree=pyast.parse(txt)
    except Exception: return []
    out=[]
    inner=set()
    for node in pyast.walk(tree):
        if isinstance(node,pyast.JoinedStr):
            for v in pyast.walk(node):
                if v is not node: inner.add(id(v))
    for node in pyast.walk(tree):
        if id(node) in inner: continue
        if isinstance(node,pyast.Constant) and isinstance(node.value,str):
            out.append((node.value, node.lineno))
        elif isinstance(node,pyast.JoinedStr):
            parts=[]
            for v in node.values:
                if isinstance(v,pyast.Constant) and isinstance(v.value,str): parts.append(v.value)
                else: parts.append('__HOLE__')
            out.append((''.join(parts), node.lineno))
    return out
_seen_py=set()
for base in ('lang/tests','tests'):
    b=os.path.join(ROOT,base)
    files=[]
    if os.path.isfile(b): files=[b]
    else:
        for dp,dns,fns in os.walk(b):
            dns[:]=[d for d in dns if d not in EXCL]
            files += [os.path.join(dp,f) for f in sorted(fns) if f.endswith('.py')]
    for p in files:
        rel=os.path.relpath(p,ROOT)
        if not rel.startswith(('lang/tests','tests/')): continue
        if rel in _seen_py: continue
        _seen_py.add(rel)
        for s,ln in py_strings(p):
            if '\n' not in s: continue
            if not looks_drift(s): continue
            units.append([rel,'py-embed','py-embed',s,ln-1])
FENCE=re.compile(r'```drift\n(.*?)```',re.S)
for dp,dns,fns in os.walk(os.path.join(ROOT,'doc')):
    dns[:]=[d for d in dns if d not in EXCL]
    for f in sorted(fns):
        if not f.endswith('.md'): continue
        p=os.path.join(dp,f); rel=os.path.relpath(p,ROOT)
        txt=open(p,encoding='utf-8',errors='replace').read()
        for m in FENCE.finditer(txt):
            units.append([rel,'doc','doc',m.group(1),txt.count('\n',0,m.start(1))])
print('units',len(units))
WRAP_PRE='module __w;\nfn __wrap() nothrow -> Void {\n'
WRAP_POST='\n}\n'
res=[]
stat=collections.Counter()
for rel,area,kind,src,off in units:
    prog=None; err=None; mode='direct'
    try: prog=parse_program(src,filename=rel)
    except Exception as e:
        err=type(e).__name__+': '+str(e)[:120]
        # fallback: wrap as function body
        try:
            prog=parse_program(WRAP_PRE+src+WRAP_POST,filename=rel); mode='wrapped'; off=off-2; err=None
        except Exception as e2:
            # fallback2: prepend module decl only
            try:
                prog=parse_program('module __w;\n'+src,filename=rel); mode='modonly'; off=off-1; err=None
            except Exception as e3:
                pass
    stat[(area,'ok' if prog is not None else 'fail')]+=1
    stat[(area,mode)] += (1 if prog is not None else 0)
    res.append((rel,area,kind,off,err,mode,prog,src))
for k in sorted(stat): print(k,stat[k])
pickle.dump(res,open(S+'/units2.pkl','wb'))
