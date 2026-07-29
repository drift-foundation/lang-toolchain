import json,random,os
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
R=json.load(open(S+'/results2.json'))
random.seed(7)
core=[r for r in R if r['fam']!='builtin']
for r in random.sample(core,30):
    p='/home/sl/src/drift-lang/'+r['path']
    ln=r['line']+r['off']
    try: src=open(p,errors='replace').read().split('\n')[ln-1].strip()
    except Exception: src='??'
    dl=''
    if r['decl_path']:
        try: dl=open('/home/sl/src/drift-lang/'+r['decl_path'],errors='replace').read().split('\n')[r['decl_line']-1].strip()[:100]
        except Exception: dl='?'
    print('%s:%d [%s/%s/%s tv=%s conf=%s] %s'%(r['path'],ln,r['fam'],r['op'],r['opkind'],r['tv'],r['conf'],src[:110]))
    print('      DECL %s:%d  %s'%(r['decl_path'],r['decl_line'],dl))
