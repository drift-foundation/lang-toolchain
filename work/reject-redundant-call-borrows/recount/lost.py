import sys; sys.path.insert(0,'/home/sl/src/drift-lang')
import pickle,collections,re,os
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
raw=pickle.load(open(S+'/units.pkl','rb'))
# need the source text; re-extract
import importlib.util
TRIPLE=re.compile(r'(?:[rRbBfFuU]{0,2})("""|\'\'\')(.*?)\1', re.S)
ROOT='/home/sl/src/drift-lang'
EXCL={'.git','build','.venv','dist','work','.mypy_cache','.pytest_cache','__pycache__','lang-obsolete','node_modules','assets'}
srcs={}
uid_map={}
# rebuild in same order as analyze.py
uid=0
for dp,dns,fns in os.walk(ROOT):
    dns[:]=[d for d in dns if d not in EXCL]
    for f in sorted(fns):
        if f.endswith('.drift'):
            p=os.path.join(dp,f)
            try: srcs[uid]=open(p,encoding='utf-8').read()
            except Exception: srcs[uid]=''
            uid+=1
def collect_py(rootdir):
    global uid
    for dp,dns,fns in os.walk(rootdir):
        dns[:]=[d for d in dns if d not in EXCL]
        for f in sorted(fns):
            if not f.endswith('.py'): continue
            p=os.path.join(dp,f)
            try: txt=open(p,encoding='utf-8').read()
            except Exception: continue
            for m in TRIPLE.finditer(txt):
                body=m.group(2)
                if not re.search(r'(^|\n)\s*(pub\s+)?fn\s+\w', body) and not re.search(r'(^|\n)\s*module\s+\w', body):
                    continue
                srcs[uid]=body; uid+=1
collect_py(os.path.join(ROOT,'lang','tests')); collect_py(os.path.join(ROOT,'tests'))
FENCE=re.compile(r'```drift\n(.*?)```', re.S)
for dp,dns,fns in os.walk(os.path.join(ROOT,'doc')):
    dns[:]=[d for d in dns if d not in EXCL]
    for f in sorted(fns):
        if not f.endswith('.md'): continue
        txt=open(os.path.join(dp,f),encoding='utf-8',errors='replace').read()
        for m in FENCE.finditer(txt): srcs[uid]=m.group(1); uid+=1

BORROW_ARG=re.compile(r'[(,]\s*&(?:mut\s+)?[A-Za-z_"(*]')
stats=collections.Counter()
examples=collections.defaultdict(list)
for u,path,area,kind,off,err,prog in raw:
    s=srcs.get(u,'')
    n=len(BORROW_ARG.findall(s))
    key=(area,'fail' if err else 'ok')
    stats[key+('units',)]+=1
    stats[key+('borrowarg-regex',)]+=n
    if err and n:
        examples[area].append((path,n,err[:60]))
for k in sorted(stats): print(k,stats[k])
print()
for a,v in examples.items():
    v.sort(key=lambda x:-x[1])
    print('== FAILED units with regex-borrow-args:',a,'count units=',len(v),'total args=',sum(x[1] for x in v))
    for e in v[:12]: print('   ',e)
