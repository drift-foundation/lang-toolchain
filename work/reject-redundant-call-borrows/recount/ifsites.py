import json
S='/tmp/claude-1000/-home-sl-src-drift-lang/a3650d41-affa-4fca-bc2e-d412027a1a2a/scratchpad'  # drift-tmp-root-audit: allow archived research provenance copy (reject-redundant-call-borrows survey), not executed by tests
R=json.load(open(S+'/results2.json'))
for r in R:
    if r['fam']=='iface':
        ln=r['line']+r['off']
        src=open('/home/sl/src/drift-lang/'+r['path'],errors='replace').read().split('\n')[ln-1].strip()
        print('%s:%d  callee=%s owner=%s formal=%s<%s>  | %s'%(r['path'],ln,r['callee'],r['decl_owner'],r['formal'],r['inner'],src[:100]))
print('--- decl_kind==iface (resolved to interface sig) ---')
for r in R:
    if r['decl_kind']=='iface':
        ln=r['line']+r['off']
        src=open('/home/sl/src/drift-lang/'+r['path'],errors='replace').read().split('\n')[ln-1].strip()
        print('%s:%d fam=%s callee=%s owner=%s | %s'%(r['path'],ln,r['fam'],r['callee'],r['decl_owner'],src[:100]))
