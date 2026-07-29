import os, sys, json, re, dataclasses, pickle, traceback
sys.path.insert(0,'/home/sl/src/drift-lang'); sys.path.insert(0,'/home/sl/src/drift-lang/lang')
sys.setrecursionlimit(100000)
from lang.driftc.parser.parser import parse_program
from lang.driftc.parser import ast as A

ROOT='/home/sl/src/drift-lang'
SCRATCH='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
EXCL={'.git','build','.venv','dist','work','.mypy_cache','.pytest_cache','__pycache__','lang-obsolete','node_modules','assets'}

# ---------------- unit collection ----------------
class Unit:
    __slots__=('uid','path','area','kind','src','line_off','prog','err','module','imports','dirkey')
    def __init__(self,uid,path,area,kind,src,line_off):
        self.uid=uid; self.path=path; self.area=area; self.kind=kind; self.src=src
        self.line_off=line_off; self.prog=None; self.err=None
        self.module=None; self.imports={}; self.dirkey=None

def area_of(rel):
    if rel.startswith('stdlib/'): return 'stdlib'
    if rel.startswith('examples/'): return 'examples'
    if rel.startswith('tools/'): return 'tools'
    if rel.startswith('issues/'): return 'issues'
    if rel.startswith('lang/tests/') or rel.startswith('tests/'): return 'tests-drift'
    if rel.startswith('doc/'): return 'doc'
    return 'other:'+rel.split('/')[0]

units=[]
uid=0
for dp,dns,fns in os.walk(ROOT):
    dns[:]=[d for d in dns if d not in EXCL]
    for f in sorted(fns):
        p=os.path.join(dp,f)
        rel=os.path.relpath(p,ROOT)
        if f.endswith('.drift'):
            try: src=open(p,encoding='utf-8').read()
            except Exception: continue
            u=Unit(uid,rel,area_of(rel),'drift',src,0); uid+=1; units.append(u)

# ---- embedded drift in python strings under lang/tests (and tests/, conftest) ----
TRIPLE=re.compile(r'(?:[rRbBfFuU]{0,2})("""|\'\'\')(.*?)\1', re.S)
def collect_py(rootdir, area):
    global uid
    for dp,dns,fns in os.walk(rootdir):
        dns[:]=[d for d in dns if d not in EXCL]
        for f in sorted(fns):
            if not f.endswith('.py'): continue
            p=os.path.join(dp,f); rel=os.path.relpath(p,ROOT)
            try: txt=open(p,encoding='utf-8').read()
            except Exception: continue
            for m in TRIPLE.finditer(txt):
                body=m.group(2)
                if not re.search(r'(^|\n)\s*(pub\s+)?fn\s+\w', body) and not re.search(r'(^|\n)\s*module\s+\w', body):
                    continue
                line_off=txt.count('\n',0,m.start(2))
                u=Unit(uid,rel,area,'py-embed',body,line_off); uid+=1; units.append(u)
collect_py(os.path.join(ROOT,'lang','tests'),'py-embed')
collect_py(os.path.join(ROOT,'tests'),'py-embed')

# ---- doc fences ----
FENCE=re.compile(r'```drift\n(.*?)```', re.S)
for dp,dns,fns in os.walk(os.path.join(ROOT,'doc')):
    dns[:]=[d for d in dns if d not in EXCL]
    for f in sorted(fns):
        if not f.endswith('.md'): continue
        p=os.path.join(dp,f); rel=os.path.relpath(p,ROOT)
        txt=open(p,encoding='utf-8',errors='replace').read()
        for m in FENCE.finditer(txt):
            body=m.group(1)
            line_off=txt.count('\n',0,m.start(1))
            u=Unit(uid,rel,'doc','doc',body,line_off); uid+=1; units.append(u)

sys.stderr.write("units=%d\n"%len(units))

# ---------------- parse ----------------
ok=0
for u in units:
    try:
        u.prog=parse_program(u.src, filename=u.path)
        ok+=1
    except Exception as e:
        u.err=type(e).__name__+': '+str(e)[:120]
    u.dirkey=os.path.dirname(u.path)
sys.stderr.write("parsed ok=%d fail=%d\n"%(ok,len(units)-ok))
pickle.dump([(u.uid,u.path,u.area,u.kind,u.line_off,u.err,u.prog) for u in units], open(SCRATCH+'/units.pkl','wb'))
