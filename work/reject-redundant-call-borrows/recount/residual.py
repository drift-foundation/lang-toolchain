import sys; sys.path.insert(0,'/home/sl/src/drift-lang')
import pickle,re,collections
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
raw=pickle.load(open(S+'/units2.pkl','rb'))
BA=re.compile(r'[(,]\s*&(?:mut\s+)?[A-Za-z_"(*\[]')
st=collections.Counter(); ex=collections.defaultdict(list)
for rel,area,kind,off,err,mode,prog,src in raw:
    n=len(BA.findall(src))
    st[(area,'ok' if prog is not None else 'FAIL','units')]+=1
    st[(area,'ok' if prog is not None else 'FAIL','regex-borrowargs')]+=n
    if prog is None and n: ex[area].append((rel,n))
for k in sorted(st): print(k,st[k])
print()
for a,v in ex.items():
    v.sort(key=lambda x:-x[1])
    print('UNPARSED w/ borrow-args:',a,'units=',len(v),'args=',sum(x[1] for x in v))
    for e in v[:8]: print('   ',e)
