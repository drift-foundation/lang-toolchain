import sys; sys.path.insert(0,'/home/sl/src/drift-lang')
import pickle, collections, os, re
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
u=pickle.load(open(S+'/units.pkl','rb'))
c=collections.Counter()
for uid,path,area,kind,off,err,prog in u:
    if err and kind=='drift':
        c[os.path.dirname(path)]+=1
for k,v in c.most_common(30): print(v,k)
