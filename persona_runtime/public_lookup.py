"""Frozen public work orders and their desktop delivery receipts."""
import json

from . import lookup_target, session_privacy
from .archive import encoded, find_turn, fingerprint
from .knowledge_service import read
from .models import RuntimeErrorBase
from .scene_policy import read as saved
from .turn_input import load_turn_input

VERSION = 'public-lookup-v1'
PREFIX = 'private.public.lookup.'


def key(tid, kind):
    return PREFIX+tid+'.'+kind


def freeze(db, name, value):
    previous = saved(db,name)
    if previous is None:
        db.execute('INSERT INTO knowledge_control VALUES(?,?)',(name,encoded(value)))
    elif previous!=value:
        raise RuntimeErrorBase('private_lookup_scope_changed')
    return value


def active(db, tid):
    row = db.execute('SELECT source_json FROM private_reply_source_sets WHERE turn_id=?',(tid,)).fetchone()
    return bool(row and json.loads(row[0]).get('public_lookup_version')==VERSION)


def link_uses_session_scope(db, tid):
    return db.execute("""SELECT 1 FROM private_reply_source_sets WHERE turn_id=?
        AND json_extract(source_json,'$.public_link_scope')=1""",(tid,)).fetchone() is not None


def request(db, turn):
    members = load_turn_input(db,turn)
    selections = {}
    for item in members:
        span = saved(db,'private.lookup.span.'+item['id'])
        legacy = read(db,'native.public_event.'+item['id'],{})['data']
        if span:
            if span['session']!=turn['session_id'] or span['hash']!=fingerprint(item['text']):
                raise RuntimeErrorBase('private_lookup_scope_changed')
            selections[item['id']] = span['text']
        elif legacy:
            if (legacy['session_id'],legacy['persona_id'],legacy['input_hash'])!=(
                    turn['session_id'],turn['persona_id'],fingerprint(item['text'])):
                raise RuntimeErrorBase('private_lookup_scope_changed')
            selections[item['id']] = legacy['task']['question']
    decision = db.execute("""SELECT attempt_id,request_hash,result_json FROM private_reply_nodes WHERE turn_id=?
        AND node='J4' AND draft=0 AND state='received'""",(turn['id'],)).fetchone()
    return {'intent':'button' if selections else 'conversation','selection':selections,
            'inputs':[[m['id'],fingerprint(m['text'])] for m in members],
            'j4':[decision[0],decision[1],json.loads(decision[2])] if decision else None}


def public_subject_origin(db,turn,consent,subject):
    """Only one surviving public topic may vouch for an inferred subject."""
    if not subject.strip():
        return None
    rows=db.execute("""SELECT t.id FROM turns t JOIN private_lookup_records r ON r.turn_id=t.id
        WHERE t.persona_id=? AND t.session_id=? AND t.channel='desktop' AND t.status='completed'
        AND t.cancelled_at IS NULL AND r.outcome='acquired'
        AND t.rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC LIMIT 12""",
        (turn['persona_id'],turn['session_id'],turn['id']))
    matches={}
    for (origin,) in rows:
        revision=saved(db,key(origin,'revision'))
        if not revision or not saved(db,key(origin,'order')):
            continue
        fields=lookup_target._research(db,turn,consent,origin)
        if any(f['kind']=='title' and (subject==f['text'] or len(subject)>1 and subject in f['text'])
               for f in fields):
            matches.setdefault(revision['topic'],origin)
    return next(iter(matches.values())) if len(matches)==1 else None


def context_authority(db, config, turn, base, proof, requested):
    """Trace each outgoing fragment to its public source or the player's selected range."""
    from .private_lookup import evidence
    scope = session_privacy.public_scope(db,config,base['instance'],turn['persona_id'],turn['session_id'],base['consent'])
    if not scope['enabled']:
        return None
    pins = proof['pins']
    origins = list(dict.fromkeys(p['ref'].split('/',2)[1] for p in pins if p['ref'].startswith('r/')))
    current = {event for event,_ in requested['inputs']}
    quoted = proof.get('quoted') and lookup_target.frozen_version(db,turn['id'])==lookup_target.QUOTED_TARGET_VERSION
    if (scope['protocol']==session_privacy.PUBLIC_QUESTION_SCOPE and quoted and pins
            and all(pin['ref'] in current and pin.get('role') in ('subject','attribute','qualifier') for pin in pins)):
        witnesses = [{'event':pin['ref'],'span':[pin['start'],pin['end']]} for pin in pins]
        return {'kind':'current_query','scope':scope,'witnesses':witnesses,'topics':[]}
    if quoted and pins and lookup_target.is_world_title(pins[0]['ref']):
        from .persona_world_topics import fields as world_fields
        subject=pins[0]
        allowed_roles={'attribute','qualifier'} if scope['protocol']==session_privacy.PUBLIC_QUESTION_SCOPE else {'attribute'}
        if (scope['protocol'] not in (session_privacy.PUBLIC_ATTRIBUTE_SCOPE,session_privacy.PUBLIC_QUESTION_SCOPE)
                or subject.get('role')!='subject' or len(origins)!=1 or len(pins)<2
                or any(p['ref'] not in current or p.get('role') not in allowed_roles for p in pins[1:])):
            return None
        source=next((f for f in world_fields(db,turn,base['consent']) if f['ref']==subject['ref']
                     and f['revision']==subject['revision']),None)
        if not source:
            return None
        witness={'public_title':source['ref'],'revision':source['revision'],'span':[subject['start'],subject['end']]}
        return {'kind':'context','scope':scope,'witnesses':[witness,*[
            {'event':p['ref'],'span':[p['start'],p['end']],'topic':origins[0]} for p in pins[1:]]],'topics':origins}
    inherited_subject=False
    if (not origins and quoted and len(pins)>1 and scope['protocol'] in
            (session_privacy.PUBLIC_ATTRIBUTE_SCOPE,session_privacy.PUBLIC_QUESTION_SCOPE)
            and all(p.get('role')=='attribute' and p['ref'] in current for p in pins[1:])):
        from . import lookup_continuation
        if cited:=lookup_continuation.public_subject(db,turn,base,proof,config=config):
            current_witnesses=[{'event':p['ref'],'span':[p['start'],p['end']],'topic':cited['origin']} for p in pins[1:]]
            return {'kind':'context','scope':scope,'witnesses':[cited['witness'],*current_witnesses],
                    'topics':[cited['origin']]}
    if (not origins and quoted and pins and scope['protocol'] in
            (session_privacy.PUBLIC_ATTRIBUTE_SCOPE,session_privacy.PUBLIC_QUESTION_SCOPE)
            and pins[0].get('role')=='subject' and pins[0]['ref'] not in current
            and all(p.get('role')=='attribute' and p['ref'] in current for p in pins[1:])):
        event=db.execute("SELECT text FROM events WHERE id=? AND role='user'",(pins[0]['ref'],)).fetchone()
        if event:
            subject=event[0][pins[0]['start']:pins[0]['end']]
            origin=public_subject_origin(db,turn,base['consent'],subject)
            if origin:
                origins=[origin]
                inherited_subject=True
    # Separate subjects in a comparison require the player's exact per-turn projection.
    if len(origins)>1:
        return None
    witnesses = []
    available = []
    if origins:
        origin = origins[0]
        fields = lookup_target._research(db,turn,base['consent'],origin)
        if not fields or any(f['ref']==p['ref'] and f['kind']=='correction' for f in fields for p in pins):
            return None
        data = evidence(db,turn['persona_id'],turn['session_id'],origin)
        if not data:
            return None
        source_rows = data['sources']
        if not db.execute('SELECT 1 FROM private_lookup_records WHERE turn_id=?',(origin,)).fetchone():
            record = read(db,'conversation.record.'+origin,{})['data']
            native=record.get('native',{})
            attempt=native.get('native_attempt_id')
            received=db.execute("""SELECT t.id,t.payload_json FROM organization_tasks t JOIN organization_attempts a
                ON a.task_id=t.id WHERE a.id=? AND a.status='completed' AND t.status='completed'
                AND t.channel='desktop' AND t.persona_id=? AND t.session_id=?""",
                (attempt,turn['persona_id'],turn['session_id'])).fetchone()
            if not received:
                return None
            spec=json.loads(received['payload_json']).get('knowledge_processing',{})
            if spec.get('root_turn_id')!=origin or spec.get('stage')!='conversation_native':
                return None
            old_task=spec['input'].get('public_task')
            if not old_task or not old_task.get('question'):
                return None
            witnesses.append({'acquisition':received['id'],'attempt':attempt,'public_task_hash':fingerprint(old_task)})
            current_ids = {s.get('id') for s in native.get('sources',[])}
            source_rows = [s for s in source_rows if s['id'] in current_ids]
        excluded=lookup_target.excluded_sources(db,origin)
        available = [s for s in lookup_target.visible_sources(db,turn,source_rows) if s.get('kind')!='user_correction' and s['id'] not in excluded]
    for index,pin in enumerate(pins):
        ref = pin['ref']
        if ref.startswith('r/'):
            continue
        row = db.execute('SELECT text,role FROM events WHERE id=?',(ref,)).fetchone()
        if not row:
            return None
        value = row[0][pin['start']:pin['end']].strip()
        if value in requested['selection'].get(ref,''):
            witnesses.append({'event':ref,'span':[pin['start'],pin['end']]})
            continue
        if (scope['protocol'] in (session_privacy.PUBLIC_ATTRIBUTE_SCOPE,session_privacy.PUBLIC_QUESTION_SCOPE) and quoted
                and pin.get('role')=='attribute' and (inherited_subject or pins[0]['ref'].startswith('r/')) and available
                and row['role']=='user' and ref in current):
            witnesses.append({'event':ref,'span':[pin['start'],pin['end']],'topic':origin})
            continue
        admission=saved(db,key(origin,'admission')) if origins else None
        def supports(source):
            body=source.get('content') or source.get('summary') or ''
            if admission is None:
                return value in body or value in (source.get('title') or '')
            spans=admission.get(source['id'],{}).get('spans',[])
            return any(value in body[start:end] for start,end in spans) or bool(spans and value in source['title'])
        source = next((s for s in available if supports(s)),None)
        if source:
            witnesses.append({'source':[origin,source['id']],'revision':fingerprint(source)})
            continue
        # The separately granted scope includes a resolver-selected attribute in an actually displayed reply.
        # Its public subject must be bound first. Keep the exact range and presentation witness.
        if index and available and pins[0]['ref'].startswith('r/') and row['role']=='persona' and value!=row['text'].strip():
            from .ordinary_materials import _confirmed
            owner=find_turn(db,'event',ref)
            if owner and _confirmed(db,owner['id'],{'id':ref,'text':row['text']}):
                witnesses.append({'event':ref,'turn':owner['id'],'span':[pin['start'],pin['end']],
                                  'visible_revision':pin['revision'],'topic':origin})
                continue
        return None
    return {'kind':'context','scope':scope,'witnesses':witnesses,'topics':origins}


def order(db, config, turn, base, it):
    turn = dict(turn)
    tid = turn['id']
    requested = request(db,turn)
    if requested!=saved(db,key(tid,'request')) or not requested['j4'] or requested['j4'][2]!='retrieve':
        raise RuntimeErrorBase('private_lookup_scope_changed')
    proof = lookup_target.for_query(db,tid)
    lookup_target.validate(db,turn,base['consent'],proof)
    resolution = proof.get('task')
    query = json.loads(it['query_json']) if it['query_json'] else resolution
    if not query:
        return None
    if it['origin']=='confirmed' and it['confirmation_id']:
        authority = {'kind':'confirmed','operation':it['confirmation_id']}
    elif proof['status']=='direct' and resolution==query:
        authority = {'kind':'selection' if requested['selection'] else 'link','target':proof['target']}
        if not requested['selection'] and link_uses_session_scope(db,tid):
            scope=session_privacy.public_scope(db,config,base['instance'],turn['persona_id'],turn['session_id'],base['consent'])
            if not scope['enabled'] or scope['protocol']!=session_privacy.PUBLIC_QUESTION_SCOPE:
                return None
            authority['scope']=scope
    elif (proof['status']=='resolved' and resolution==query and query['question'] in requested['selection'].values()
          and all(p['ref'] in requested['selection'] for p in proof['pins'])):
        authority = {'kind':'selection','events':[e for e,s in requested['selection'].items() if s==query['question']]}
    elif proof['status']=='resolved' and resolution==query:
        authority = context_authority(db,config,turn,base,proof,requested)
        if not authority:
            return None
    else:
        raise RuntimeErrorBase('private_lookup_scope_changed')
    work={'version':VERSION,'instance':base['instance'],'persona':turn['persona_id'],'session':turn['session_id'],
            'turn':tid,'request_hash':fingerprint(requested),'j4':requested['j4'][0],
            'resolver':proof.get('attempt_id'),'target_hash':fingerprint(proof),'pins':proof.get('pins',[]),
            'query':query,'as_of':turn['created_at'],'recipients':base['consent']['recipient_hash'],
            'consent_revision':base['consent']['revision'],'authority':authority}
    from . import research_contract
    if need:=research_contract.request(db,tid):
        research_contract.validate_request(db,turn,base,json.loads(it['target_json']))
        work['research_request']=fingerprint(need)
    return work


def require_order(db, config, turn, base, it):
    previous = saved(db,key(turn['id'],'order'))
    if not previous or previous!=order(db,config,turn,base,it):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    return previous


def check_step(db, work, step, args):
    query = work['query']
    if step=='native':
        if {k:v for k,v in args.items() if k!='as_of'}==query:
            return
    elif step=='search':
        if args=={'query':query['question']} and len(query['question'])<=128:
            return
    elif step=='fetch' and set(args)=={'url'}:
        if query.get('url')==args['url']:
            return
        if not query.get('url'):
            from .knowledge_general import public_url
            row = db.execute("""SELECT result_json FROM private_lookup_calls WHERE turn_id=? AND step='native'
                AND state='received'""",(work['turn'],)).fetchone()
            if row:
                for source in json.loads(row[0]).get('sources',[]):
                    try:
                        url=public_url(source.get('url') if isinstance(source,dict) else None)
                    except RuntimeErrorBase:
                        continue
                    if url==args['url']:
                        return
    raise RuntimeErrorBase('private_lookup_scope_changed')


def result(db, tid):
    paper = db.execute('SELECT outcome FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()
    readback = db.execute('SELECT outcome FROM knowledge_study_reads WHERE turn_id=?',(tid,)).fetchone()
    from . import research_contract
    assessed=research_contract.coverage(db,tid,'readback')
    if readback and assessed:
        status={'complete':'supported','partial':'partial_evidence','insufficient':'no_relevant_evidence',
                'unconfirmed':'coverage_unconfirmed'}[assessed['outcome']]
    elif readback:
        status = 'supported' if readback[0]=='hit' else 'no_relevant_evidence'
        revision=saved(db,key(tid,'revision')) or {}
        if status=='supported' and revision.get('coverage')=='partial':
            status='partial_evidence'
    elif paper and paper[0]=='unconfirmed':
        status = 'unconfirmed'
    elif db.execute("SELECT 1 FROM private_lookup_calls WHERE turn_id=? AND state='failed'",(tid,)).fetchone():
        status = 'unavailable'
    else:
        status = 'no_relevant_evidence'
    work = saved(db,key(tid,'order'))
    return {'question':work['query']['question'],'outcome':status}


def details(db, store, turn):
    from . import local_lookup_route
    from .config import ConfigStore
    from .private_lookup import enabled, intent
    from .knowledge_native_memory import eligible
    tid,pid,sid=turn['id'],turn['persona_id'],turn['session_id']
    if not enabled(db,pid) or not eligible(db,tid):
        raise RuntimeErrorBase('retrieved_notes_unavailable')
    config=ConfigStore(store.paths)
    cfg=config.load()
    consent=session_privacy.current(db,config,cfg,pid,sid,session_privacy.recipients(config,cfg))
    base={'instance':cfg['instance_id'],'consent':consent}
    work=require_order(db,config,turn,base,intent(db,tid))
    status=result(db,tid)['outcome']
    revision=saved(db,key(tid,'revision'))
    if not revision or status not in ('supported','partial_evidence'):
        reason={'no_relevant_evidence':'本轮未取得能回答该问题的新资料。','unconfirmed':'工具结果待核对。',
                'coverage_unconfirmed':'已取得资料，目前无法确认能否回答本轮问题。',
                'unavailable':'本轮公开工具未完成查找。'}
        local=local_lookup_route.receipt(db,tid)
        if local and local['outcome']=='partial':
            facts=local_lookup_route.current_facts(db,turn,intent(db,tid),consent,local)
            return {'notes':reason.get(status,'本轮未取得可用资料。'),
                    'sources':[{'id':f['id'],'title':f['title'],'url':f['url'],'content':f['text'],
                                'qualification':f.get('qualification'),'date':f.get('date')} for f in facts]}
        return {'notes':reason.get(status,'本轮未取得可用资料。'),'sources':[]}
    rows=db.execute("""SELECT t.*,k.value rev FROM turns t JOIN knowledge_control k ON k.key=?||t.id||'.revision'
        WHERE t.persona_id=? AND t.session_id=? AND json_extract(k.value,'$.topic')=? AND
        t.rowid>=(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC""",(PREFIX,pid,sid,revision['topic'],tid))
    for candidate in rows:
        origin=candidate['id']
        if not eligible(db,origin):
            continue
        current=saved(db,key(origin,'order'))
        if origin!=tid:
            try:
                require_order(db,config,candidate,base,intent(db,origin))
            except RuntimeErrorBase:
                continue
        proof=local_lookup_route.answer_receipt(db,origin)
        if not proof or proof['outcome']!='hit':
            continue
        by_id={f['id']:f for f in local_lookup_route.research_passages(db,candidate,base,current['query']['question'],
                                  ids=json.loads(proof['fact_ids']),resolution=lookup_target.for_query(db,origin))}
        facts=[by_id[i] for i in json.loads(proof['fact_ids']) if i in by_id]
        if facts:
            answer={'updated_at':json.loads(candidate['rev'])['at'],'question':current['query']['question'],
                    'notes':'\n'.join(f['text'] for f in facts),
                    'sources':[{'id':f['source_ids'][0],'title':f['title'],'url':f['url'],'date':f['date'],
                                'content':f['text'],'qualification':f.get('qualification')} for f in facts]}
            if origin!=tid:
                from .local_knowledge import _turn_source
                prior=[]
                for fact in db.execute("""SELECT f.spec_json FROM local_knowledge_facts f
                        WHERE f.id IN (SELECT value FROM json_each(?)) AND f.withdrawn=0 AND NOT EXISTS
                        (SELECT 1 FROM knowledge_control WHERE key='private.study.withdrawn_document.'||f.document_id)""",
                        (encoded(revision['facts']),)):
                    spec=json.loads(fact[0])
                    source_turn,source_id,digest=spec['turn_source']
                    source=_turn_source(db,pid,sid,source_turn,source_id)
                    if source and fingerprint(source)==digest:
                        prior.append({'title':source['title'],'url':source['url'],'date':source.get('date'),
                                      'text':spec['quote'],'qualification':source.get('qualification')})
                answer['original']={'question':work['query']['question'],'at':revision['at'],'sources':prior}
            return answer
    return {'notes':'已保存的资料目前没有可用修订。','sources':[]}


def trace(db, tid):
    asked = saved(db,key(tid,'request'))
    if not asked:
        return None
    work = saved(db,key(tid,'order'))
    wire_prefix = key(tid,'wire.')
    calls = []
    for row in db.execute('SELECT value FROM knowledge_control WHERE key>=? AND key<? ORDER BY rowid',(wire_prefix,wire_prefix+'~')):
        wire = json.loads(row[0])
        attempt = wire['attempt']
        actual = db.execute('SELECT state,result_json FROM private_lookup_calls WHERE turn_id=? AND attempt_id=?',(tid,attempt)).fetchone()
        unsent = saved(db,'private.lookup.unsent.'+attempt)
        state = actual[0] if actual else 'unsent' if unsent and unsent.get('headers_sent') is False else 'unknown'
        response = json.loads(actual[1] or '{}') if actual else {}
        calls.append(wire|{'state':state,'results':len(response.get('sources',[])) if state=='received' else None,
                           'native_tool_calls':response.get('tool_calls') if state=='received' and wire['step']=='native' else None})
    readback = db.execute('SELECT outcome,fact_ids FROM knowledge_study_reads WHERE turn_id=?',(tid,)).fetchone()
    paper = db.execute('SELECT outcome FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()
    answer = db.execute("""SELECT a.id,a.state writer_state,a.body_sha256,d.contract_json,p.id publication,p.state,p.canonical_event_id
        FROM private_reply_attempts a JOIN private_reply_decisions d ON d.id=a.decision_id
        LEFT JOIN expression_speech_units u ON u.source_id=a.id AND u.phase='main' AND u.source_kind='maibot_private_reply'
        LEFT JOIN expression_speech_publications p ON p.utterance_id=u.id
        WHERE a.turn_id=? AND a.phase='main' ORDER BY a.draft DESC LIMIT 1""",(tid,)).fetchone()
    delivery = None
    unresolved = False
    if answer:
        bound = json.loads(answer['contract_json'])['ordinary']['research']
        unresolved = bound.get('lookup_outcome')=='target_unresolved'
        shown = [dict(r) for r in db.execute("""SELECT v.receipt_id,v.presented_at,i.body_hash FROM expression_speech_visible v
            JOIN expression_speech_items i ON i.id=v.fragment_id WHERE v.publication_id=? ORDER BY v.ordinal""",(answer['publication'],))]
        phase=answer['state'] or ('generated' if answer['writer_state']=='received' else answer['writer_state'])
        delivery = {'attempt':answer['id'],'body_sha256':answer['body_sha256'],'state':phase,
                    'event':answer['canonical_event_id'],'fragments':shown,'facts':[f['id'] for f in bound['facts']]}
    from .private_lookup import presented_notice
    turn=db.execute('SELECT status,error_code FROM turns WHERE id=?',(tid,)).fetchone()
    waiting=db.execute("SELECT 1 FROM private_lookup_intents WHERE turn_id=? AND state='waiting'",(tid,)).fetchone()
    reason=turn['error_code']
    if waiting and reason in (None,'private_lookup_confirmation_required'):
        proof=lookup_target.saved(db,tid) or {}
        reason='needs_public_scope' if proof.get('status')=='resolved' else proof.get('status') or 'needs_confirmation'
    elif unresolved:
        proof=lookup_target.saved(db,tid) or {}
        reason='no_public_scope' if proof.get('status') in ('resolved','direct') else proof.get('status')
    return {'version':VERSION,'intent':asked['intent'],'decision':asked['j4'][2] if asked['j4'] else None,
            'turn_state':turn['status'],'reason':reason,
            'request_hash':fingerprint(asked),'j4':asked['j4'][0] if asked['j4'] else None,
            'target':work['resolver'] if work else None,'authority':work['authority']['kind'] if work else None,
            'scope_revision':work['authority'].get('scope',{}).get('revision') if work else None,
            'ready':bool(work),'order_hash':fingerprint(work) if work else None,'calls':calls,
            'notice_event':presented_notice(db,tid),'outcome':result(db,tid)['outcome'] if paper and work and
                (readback or paper[0]!='acquired') else 'target_unresolved' if unresolved else None,
            'acquisition':paper[0] if paper else None,'evidence':readback[0] if readback else None,
            'facts':json.loads(readback[1]) if readback else [],'revision':saved(db,key(tid,'revision')),
            'delivery':delivery,'fallback':saved(db,key(tid,'fallback'))}
