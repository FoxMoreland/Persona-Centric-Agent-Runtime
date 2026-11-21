"""Bind lookup statements to local read receipts before publishing the writer's prose."""
import json
import re

from .archive import fingerprint
from .public_lookup import saved


VERSION = 'lookup-reply-provenance-v1'
NODE = 'lookup.attribution'
OPTIONS = ('grounded','unbound')


def scope(db, turn, research):
    """Use the existing retrieval, topic and task decisions to recognize a continuation."""
    from .private_lookup import intent
    current=intent(db,turn['id'])
    active=bool(current and current['choice']=='retrieve')
    if not active:
        previous=db.execute("""SELECT t.id,t.reply_event_id,i.choice FROM turns t
            LEFT JOIN private_lookup_intents i ON i.turn_id=t.id
            WHERE t.persona_id=? AND t.session_id=? AND t.channel='desktop' AND t.cancelled_at IS NULL
            AND t.rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC LIMIT 1""",
            (turn['persona_id'],turn['session_id'],turn['id'])).fetchone()
        if previous and (previous['choice']=='retrieve' or research.get('facts')):
            def decisions(tid):
                return {node:json.loads(value) for node,value in db.execute("""SELECT node,result_json
                    FROM private_reply_nodes WHERE turn_id=? AND draft=0 AND state='received'
                    AND node IN ('J2.action','J2.task','J3')""",(tid,))}
            now=decisions(turn['id'])
            prior=decisions(previous['id'])
            anchor=now.get('J3')
            shared_event=anchor and anchor==prior.get('J3') and db.execute(
                'SELECT 1 FROM events WHERE id=? AND persona_id=? AND session_id=?',
                (anchor,turn['persona_id'],turn['session_id'])).fetchone()
            active=now.get('J2.action') in ('respond','clarify') and (
                now.get('J2.task')=='open' and (prior.get('J2.task')=='open' or bool(research.get('facts')))
                or bool(shared_event) or bool(anchor and anchor==previous['reply_event_id']))
    if not active:
        return None
    fresh=[]
    if research.get('lookup_outcome')=='studied':
        received=db.execute("""SELECT c.fact_ids,r.fact_ids FROM knowledge_study_commits c
            JOIN knowledge_study_reads r ON r.turn_id=c.turn_id AND r.outcome='hit'
            WHERE c.turn_id=? AND c.status='committed' AND EXISTS (
                SELECT 1 FROM private_lookup_calls p WHERE p.turn_id=c.turn_id AND p.state='received'
                AND p.step IN ('native','search','fetch') AND json_array_length(p.result_json,'$.sources')>0
                AND (p.step!='native' OR json_extract(p.result_json,'$.tool_calls')>0))""",(turn['id'],)).fetchone()
        if received:
            acquired=set(json.loads(received[0])).intersection(json.loads(received[1]))
            fresh=[f['id'] for f in research['facts'] if f['id'] in acquired]
    return {'version':VERSION,'fresh_fact_ids':fresh}


def unpack(raw, long, *, action=None, mode=None):
    """Keep malformed received output available for one writer repair, outside the timeline."""
    def unique(pairs):
        if len(dict(pairs))!=len(pairs):
            raise ValueError()
        return dict(pairs)
    fields={'body','claims'} | ({'detail'} if long else set())
    try:
        value=json.loads(raw,object_pairs_hook=unique)
        if isinstance(value,dict):
            for field,planned in (('action',action),('mode',mode)):
                if planned is not None and value.get(field)==planned:
                    value.pop(field)
            if not long and value.get('detail')=='':
                value.pop('detail')
        if (not isinstance(value,dict) or set(value)!=fields or not isinstance(value['claims'],list)
                or len(value['claims'])>64 or not all(isinstance(value[k],str) and value[k].strip()
                for k in fields-{'claims'})):
            raise ValueError()
    except (ValueError,TypeError,RecursionError):
        return raw or '{}',{'error':'format'}
    claims=value.pop('claims')
    return (value if long else value['body']),{'claims':claims}


def references(body,detail,annotation,facts):
    """Return source and statement offsets in the exact saved versions."""
    fields={'body':body,'detail':detail}
    offered={f['id']:f for f in facts}
    if not isinstance(annotation,dict) or not isinstance(annotation.get('claims'),list):
        return None
    bindings=[]
    for item in annotation['claims']:
        if (not isinstance(item,dict) or set(item)!={'field','claim','fact','quote'}
                or not all(isinstance(v,str) and v.strip() for v in item.values())):
            return None
        output=fields.get(item['field'])
        fact=offered.get(item['fact'])
        quote,claim=item['quote'],item['claim']
        if not output or not fact:
            return None
        offset=output.find(claim)
        start=fact['text'].find(quote)
        if offset<0 or start<0 or output.find(claim,offset+1)>=0 or fact['text'].find(quote,start+1)>=0:
            return None
        bindings.append({'field':item['field'],'statement':[offset,offset+len(claim)],'fact':fact['id'],
                         'source_hash':fingerprint(fact),'passage':[start,start+len(quote)]})
    return bindings


def literal_citations(db,tid,body,detail,research,declared=()):
    """Project exact quoted text from an already verified coverage witness."""
    from . import research_contract
    retained=research.get('retained_read')
    stage='readback' if retained or research.get('lookup_outcome')=='studied' else 'local'
    coverage=research_contract.coverage(db,retained['origin'] if retained else tid,stage) or {}
    offered={f['id']:f for f in research.get('facts',[])}
    matches=[]
    for witness in coverage.get('witnesses',[]):
        fact=offered.get(witness['id'])
        if fact and fingerprint(fact)==witness['revision']:
            start,end=witness['span']
            matches.append((fact,fact['text'][start:end]))
    if not matches:
        return []
    extra=[]
    marked=[item['claim'] for item in declared]
    for text in (body,detail):
        for quote in re.finditer(r'“([^”\r\n]+)”|「([^」\r\n]+)」',text or ''):
            literal=quote[1] or quote[2]
            if not literal.strip() or any(literal in claim for claim in marked):
                continue
            candidates=[(f,passage) for f,passage in matches if literal in passage]
            if len(candidates)==1:
                fact,passage=candidates[0]
                extra.append({'claim':literal,'quote':passage,'title':fact['title'],'url':fact['url']})
                marked.append(literal)
    return extra


def saved_failure(db,attempt):
    return (saved(db,'private.reply.attribution.'+attempt) or {}).get('failure')


async def check(flow,turn,base,material,proof,attempt):
    from . import lookup_target, public_lookup, research_contract
    from .private_lookup import validate_context
    from .models import RuntimeErrorBase
    research=proof['research']
    wire=proof['provenance']
    facts=research['facts']
    annotation=json.loads(attempt['usage_json'] or '{}').get('lookup_attribution')
    bindings=references(attempt['body'],attempt['detail'],annotation,facts)
    key='private.reply.attribution.'+attempt['id']
    verdict='unbound'
    if bindings is not None:
        by_id={f['id']:f for f in facts}
        sources=[{k:f[k] for k in ('id','title','text','date','qualification','qualifications','kind') if k in f}
                 for f in (by_id[ident] for ident in dict.fromkeys(b['fact'] for b in bindings))]
        state={'question':[material['catalog'][i]['text'] for i in material['current']],
               'body':attempt['body'],'detail':attempt['detail'],'claims':annotation['claims'],
               'sources':sources,'fresh_fact_ids':wire['fresh_fact_ids']}
        with flow.store.connection() as db:
            resolution=lookup_target.for_query(db,turn['id']) or {}
            state['resolved_question']=(resolution.get('task') or {}).get('question')
            if not state['resolved_question'] and (carried:=research.get('retained_read')):
                prior=public_lookup.saved(db,public_lookup.key(carried['origin'],'order'))
                if prior:
                    state['resolved_question']=prior['query']['question']
        def verify(db):
            if not base['consent']['enabled']:
                raise RuntimeErrorBase('private_cloud_provider_not_authorized')
            validate_context(db,turn,base,research,config=flow.entry.config)
            if scope(db,turn,research)!=wire:
                raise RuntimeErrorBase('private_reply_source_changed')
        verdict=await flow.choice(turn['id'],proof['draft'],NODE,state,OPTIONS,base,material,check_evidence=verify)
    with flow.store.connection(write=True) as db:
        node=db.execute("""SELECT attempt_id FROM private_reply_nodes WHERE turn_id=? AND draft=? AND node=?
            AND state='received' AND result_json=?""",
            (turn['id'],proof['draft'],NODE,json.dumps(verdict))).fetchone() if bindings is not None else None
        if bindings is not None and not node:
            raise RuntimeErrorBase('private_reply_unreviewed')
        receipt={'version':VERSION,'body_hash':attempt['body_sha256'],
                 'detail_hash':fingerprint(attempt['detail']),'bindings':bindings,
                 'fresh_fact_ids':wire['fresh_fact_ids'],'review_attempt':node[0] if node else None,'verdict':verdict}
        if research_contract.active(db,turn['id']) and verdict!='grounded':
            if (annotation or {}).get('error')=='format':
                receipt['failure']='lookup_reply_format_invalid'
            elif bindings is None:
                receipt['failure']='lookup_reply_citation_invalid'
            else:
                receipt['failure']='lookup_reply_unbound'
        public_lookup.freeze(db,key,receipt)
    return verdict,key
