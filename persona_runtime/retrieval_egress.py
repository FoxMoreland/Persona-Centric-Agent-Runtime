"""Reviewed semantic public-scope decisions, one text-only request per missing batch."""
import hashlib
import json
import re

from . import persona_world, semantic_knowledge, single_retrieval as retrieval
from .archive import encoded, find_turn, fingerprint
from .knowledge_native import NO_QUERY, SENSITIVE_QUERY
from .models import RuntimeErrorBase

DIGEST='b41346634dce96ddf7a14b41a15f9e375a3af0fbf5f6d546098ba88f8d316929'
REASONS=('allow','private','outside','uncertain')
PROTECTED=re.compile(r'(?:\bsk[-_A-Za-z0-9]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|'
    r'\bBearer\s+[A-Za-z0-9_./+=-]{8,}|(?:密码|密钥|口令|password|api[_ -]?key)\s*[:=：]\s*\S+|'
    r'https?://[^/\s]+:[^/\s]+@|(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dXx](?!\w))',re.I)


def load_manifest(bundle,config,primary):
    raw=bundle.get('egress_manifest')
    if not primary or not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=DIGEST:
        raise RuntimeErrorBase('private_chat_review_version_changed')
    spec=json.loads(raw)
    parent=(config.paths.home/'private/ordinary-reply'/(spec['baseline_isolated_bundle']+'.json')).read_bytes()
    if (hashlib.sha256(parent).hexdigest()!=spec['baseline_isolated_bundle']
            or json.loads(parent)!={k:v for k,v in bundle.items() if k not in ('egress_manifest','translation_manifest')}
            or spec['approved_retrieval_manifest']!=retrieval.DIGEST or spec['recipient']!=primary['recipient']
            or spec['scope']['instance_id']!=primary['instance_id'] or spec['scope']['persona_ids']!=primary['persona_ids']):
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    return spec


def review_basis(db,turn,base,plan,anchor):
    from .local_lookup_route import answer_receipt, has_local_material

    context={'ref':anchor['ref'],'use':'context_only','text':anchor['text']}
    eid=plan.get('anchor_events',{}).get(str(anchor['ref']))
    owner=find_turn(db,'event',eid) if eid else None
    if not owner or (owner['session_id'],owner['persona_id'])!=(turn['session_id'],turn['persona_id']):
        return context
    live=persona_world._live_privacy(db,base['instance'])
    if not live:
        return context
    receipt=answer_receipt(db,owner['id']) if owner['reply_event_id']==eid and owner['status']=='completed' else None
    if has_local_material(receipt):
        selected=json.loads(receipt['fact_ids'])
        eligible=set(persona_world.eligible_fact_ids(db,turn,base,selected))
        origins=db.execute("""SELECT DISTINCT json_extract(spec_json,'$.turn_source[0]')
            FROM local_knowledge_facts WHERE id IN (SELECT value FROM json_each(?))""",(encoded(selected),))
        for (origin,) in origins:
            if origin:
                eligible.update(e['fact_id'] for e in persona_world.inspect_turn(
                    db,base['instance'],turn['persona_id'],origin,live=live))
        facts=semantic_knowledge.materials(db,turn,base,anchor['text'],ids=[i for i in selected if i in eligible])
        found={f['id']:f['text'] for f in facts if not PROTECTED.search(f['text'])}
        snippets=list(dict.fromkeys(found[i] for i in selected if i in found))
        if snippets:
            return {'ref':anchor['ref'],'use':'public_basis','text':'\n'.join(snippets)}
    entries=persona_world.inspect_turn(db,base['instance'],turn['persona_id'],owner['id'],live=live)
    ids=[e['fact_id'] for e in entries]
    facts=semantic_knowledge.materials(db,turn,base,anchor['text'],ids=ids)
    if any(anchor['text'] in (f['title'],f['text']) for f in facts):
        return context|{'use':'public_basis'}
    return context


def projection(db,turn,base,plan,query):
    refs=set(query['refs'])
    current=[{'ref':r['ref'],'text':r['text']} for r in plan['current'] if r['ref'] in refs]
    if not current:
        current=[{'ref':r['ref'],'text':r['text']} for r in plan['current']]
    anchors=[r for r in plan['anchors'] if r['ref'] in refs]
    if NO_QUERY.search(query['text']) or any(PROTECTED.search(r['text']) for r in [query,*current,*anchors]):
        return 'private',None
    public_input={'as_of':turn['created_at'],'current':plan['current'],'history':[],'references':[]}
    cold=fingerprint(public_input)==plan['context'] and not any(SENSITIVE_QUERY.search(r['text']) for r in plan['current'])
    exact_current=not anchors and any(query['text']==r['text'] for r in current)
    if (cold or exact_current) and not any(SENSITIVE_QUERY.search(r['text']) for r in [query,*current]):
        return 'direct',None
    basis=[review_basis(db,turn,base,plan,r) for r in anchors]
    return 'review',{'current':current,'basis':basis,'queries':[query]}


def combine(parts):
    current={}
    basis={}
    queries=[]
    for part in parts:
        current.update((r['ref'],r) for r in part['current'])
        basis.update((r['ref'],r) for r in part['basis'])
        queries.extend(part['queries'])
    return {'current':[current[k] for k in sorted(current)],'basis':[basis[k] for k in sorted(basis)],'queries':queries}


def offer(db,turn,base,plan,gaps,limit):
    rows=[]
    parts=[]
    for item in gaps:
        query=plan['queries'][item-1]
        route,part=projection(db,turn,base,plan,query)
        if part is not None:
            if len(encoded(combine([*parts,part])))<=limit:
                parts.append(part)
            else:
                route='input_budget'
        rows.append({'id':item,'route':route,'projection_hash':fingerprint(part or query)})
    return rows,combine(parts)


def answers(raw,ids):
    accepted={}
    items=raw.get('items') if isinstance(raw,dict) and set(raw)=={'items'} else None
    if isinstance(items,list):
        for entry in items:
            if not isinstance(entry,dict) or type(entry.get('id')) is not int or entry['id'] not in ids:
                continue
            item=entry['id']
            if item in accepted or set(entry)!={'id','result'} or entry['result'] not in REASONS:
                accepted[item]='uncertain'
            else:
                accepted[item]=entry['result']
    return {i:accepted.get(i,'uncertain') for i in ids}


async def review(flow,turn,base,material,plan,gaps,scope):
    from . import private_lookup
    tid=turn['id']
    spec=flow.providers.egress_spec
    limit=spec['recipient']['max_input_chars_at_review']-len(spec['system'])
    with flow.store.connection() as db:
        rows,packet=offer(db,turn,base,plan,gaps,limit)
        previous=retrieval.saved(db,tid,'egress')
    wire_hash=fingerprint(packet)
    def check(db):
        retrieval.check_context(db,turn,base,material,plan)
        if (not private_lookup.enabled(db,turn['persona_id']) or retrieval.public_scope(flow,db,turn,base)!=scope or
                offer(db,turn,base,plan,gaps,limit)!=(rows,packet)):
            raise RuntimeErrorBase('private_lookup_scope_changed')
    if previous:
        with flow.store.connection() as db:
            check(db)
        if (previous['packet_hash']!=wire_hash or previous['plan_hash']!=fingerprint(plan)
                or previous['scope']!=scope):
            raise RuntimeErrorBase('private_lookup_scope_changed')
        return [r['id'] for r in previous['items'] if r['decision'] in ('direct','allow')]
    raw=None
    if packet['queries']:
        try:
            raw=await flow._decision(tid,0,'retrieval.egress',packet,(),base,material,
                                     structured='egress',check_evidence=check)
        except RuntimeErrorBase as exc:
            if str(exc)!='private_lookup_scope_changed':
                raise
    decisions=answers(raw,{q['id'] for q in packet['queries']})
    with flow.store.connection(write=True) as db:
        retrieval.check_context(db,turn,base,material,plan)
        current_scope=retrieval.public_scope(flow,db,turn,base)
        live_rows,_=offer(db,turn,base,plan,gaps,limit)
        for row,live in zip(rows,live_rows):
            item=row['id']
            decision=decisions.get(item,row['route'])
            unchanged=(row==live and current_scope==scope and private_lookup.enabled(db,turn['persona_id']))
            row['decision']=decision if unchanged else 'uncertain'
        receipt=db.execute("SELECT attempt_id,state FROM private_reply_nodes WHERE turn_id=? AND node='retrieval.egress' AND draft=0",(tid,)).fetchone()
        retrieval.freeze(db,tid,'egress',{'version':DIGEST,'plan_hash':fingerprint(plan),'scope':scope,
                         'packet_hash':wire_hash,'attempt':receipt[0] if receipt else None,
                         'attempt_state':receipt[1] if receipt else None,'items':rows})
    return [r['id'] for r in rows if r['decision'] in ('direct','allow')]


def validate(db,turn,base,plan,item,scope):
    proof=retrieval.saved(db,turn['id'],'egress')
    if (not proof or proof['version']!=DIGEST or proof['scope']!=scope or proof['plan_hash']!=fingerprint(plan)):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    row=next((r for r in proof['items'] if r['id']==item),None)
    query=plan['queries'][item-1]
    route,part=projection(db,turn,base,plan,query)
    if (not row or row['decision'] not in ('direct','allow') or row['route']!=route
            or row['projection_hash']!=fingerprint(part or query)
            or (row['decision']=='allow' and (not proof['attempt'] or proof['attempt_state']!='received'))):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    if row['decision']=='allow':
        receipt=db.execute("""SELECT result_json FROM private_reply_nodes WHERE turn_id=? AND node='retrieval.egress'
            AND draft=0 AND attempt_id=? AND state='received'""",(turn['id'],proof['attempt'])).fetchone()
        if not receipt or answers(json.loads(receipt[0]),{item})[item]!='allow':
            raise RuntimeErrorBase('private_lookup_scope_changed')
