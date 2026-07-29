import os, sys, json
ROOT='/home/sl/src/drift-lang'
EXCL={'.git','build','.venv','dist','work','.mypy_cache','.pytest_cache','__pycache__','lang-obsolete','node_modules','assets'}
files=[]
for dp,dns,fns in os.walk(ROOT):
    dns[:]=[d for d in dns if d not in EXCL]
    for f in fns:
        if f.endswith('.drift'):
            files.append(os.path.join(dp,f))
files.sort()
print(len(files))
json.dump(files, open('/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad/files.json','w'))  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
from collections import Counter
c=Counter()
for f in files:
    r=os.path.relpath(f,ROOT)
    c['/'.join(r.split('/')[:2])]+=1
for k,v in c.most_common(): print(v,k)
