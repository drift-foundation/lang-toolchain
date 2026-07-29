import sys; sys.path.insert(0,'/home/sl/src/drift-lang')
import pickle, collections
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
u=pickle.load(open(S+'/units.pkl','rb'))
c=collections.Counter(); t=collections.Counter()
errs=collections.Counter()
for uid,path,area,kind,off,err,prog in u:
    t[(area,kind)]+=1
    if err: c[(area,kind)]+=1; errs[err.split(':')[0]]+=1
for k in sorted(t): print(k, 'total',t[k],'fail',c.get(k,0))
print(errs.most_common(6))
# sample failures for drift files
n=0
for uid,path,area,kind,off,err,prog in u:
    if err and kind=='drift':
        print('DRIFTFAIL',path,err[:150]); n+=1
        if n>15: break
